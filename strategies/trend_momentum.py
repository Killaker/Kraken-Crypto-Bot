"""
strategies/trend_momentum.py
Estrategia H — Trend Momentum (two-hop).

Lógica de trading:
══════════════════
Todas las demás estrategias del bot (Mean Reversion, Synthetic Arb,
Cross-Rate Pairs, Cross-Currency Arb, Portfolio Rebalance, Smart Routing)
asumen, de una forma u otra, que el precio REVIERTE hacia un valor de
referencia (VWAP, media móvil, cross-rate medio, peso objetivo). Eso las
hace vulnerables al mismo problema en el mismo tipo de mercado: cuando el
precio no revierte sino que TIENDE de forma sostenida en una dirección,
todas pierden por igual, porque todas están apostando lo contrario.

Trend Momentum hace lo opuesto: solo entra cuando detecta una tendencia
alcista genuina, recién confirmada, y deja correr la posición mientras el
momentum se mantenga — igual filosofía de two-hop y misma protección de
rentabilidad neta que el resto de estrategias, pero diversificando el tipo
de mercado en el que el bot puede ganar.

Confirmación de tendencia (las tres deben cumplirse a la vez):
  1. Cruce alcista de EMAs: la EMA rápida cruza por ENCIMA de la EMA lenta
     en la vela actual (no basta con que ya esté por encima — se exige el
     cruce reciente para entrar al principio del movimiento, no perseguirlo
     ya extendido).
  2. ADX > tm_adx_threshold: fuerza de tendencia suficiente. Filtra
     mercados laterales/ruidosos, donde un cruce de EMAs es solo ruido y
     no el comienzo de un movimiento real. Además +DI > -DI confirma que
     la fuerza de tendencia detectada es alcista y no bajista.
  3. OBV (On-Balance Volume) en subida durante la ventana `tm_obv_lookback`:
     el movimiento de precio viene acompañado de volumen real, no es un
     falso quiebre con poca participación.

LONG:
  - Cruce alcista de EMA(tm_ema_fast) sobre EMA(tm_ema_slow) en esta vela.
  - ADX(tm_adx_period) > tm_adx_threshold y +DI > -DI.
  - OBV(-1) > OBV(-tm_obv_lookback) (volumen acompañando la subida).

EXIT:
  - Cruce bajista de EMAs (el momentum que motivó la entrada se ha
    agotado o revertido) — igual que el resto de estrategias, esta salida
    pasa por el filtro de rentabilidad neta del execution_engine: si el
    precio aún no ha cubierto el coste del round-trip, la posición no se
    cierra por esta señal y sigue protegida por su stop/take-profit real.
  - Stop Loss: ATR × tm_stop_atr_mult (más ancho que el resto: una
    tendencia necesita margen para pullbacks normales sin ser expulsada
    por ruido).
  - Take Profit: proyección ATR × tm_tp_atr_mult (deja correr el
    beneficio más lejos que una reversión típica), con el mismo suelo de
    rentabilidad neta (coste round-trip + margen) que todas las demás
    estrategias — nunca por debajo de él.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

import numpy as np

from core.config import Settings
from core.logger import get_logger
from models.market_data import Candle, Ticker
from models.order import Position
from models.signal import Signal, SignalDirection, SignalStrength
from strategies.base_strategy import BaseStrategy
from utils.indicators import adx, atr, ema, obv

logger = get_logger(__name__)


class TrendMomentumStrategy(BaseStrategy):
    strategy_id = "TREND_MOMENTUM"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._ema_fast_period = settings.tm_ema_fast
        self._ema_slow_period = settings.tm_ema_slow
        self._adx_period = settings.tm_adx_period
        self._adx_threshold = settings.tm_adx_threshold
        self._obv_lookback = settings.tm_obv_lookback
        self._stop_atr_mult = settings.tm_stop_atr_mult
        self._tp_atr_mult = settings.tm_tp_atr_mult

        # ADX necesita ~2×period de datos para dejar de ser NaN (ver
        # utils.indicators.adx); se añade margen extra sobre eso y sobre
        # la ventana de OBV/EMA lenta.
        self.min_candles = max(
            self._adx_period * 2 + 5,
            self._ema_slow_period * 2,
            self._obv_lookback + 5,
        ) + 20

        logger.info(
            "strategy.initialized",
            strategy=self.strategy_id,
            ema_fast=self._ema_fast_period,
            ema_slow=self._ema_slow_period,
            adx_threshold=self._adx_threshold,
            obv_lookback=self._obv_lookback,
        )

    async def generate_signal(
        self,
        symbol: str,
        candles: list[Candle],
        ticker: Optional[Ticker] = None,
        position: Optional[Position] = None,
    ) -> Optional[Signal]:
        closed = [c for c in candles if c.is_closed]
        if not self.has_enough_data(closed):
            return None

        _, highs, lows, closes, volumes = self._to_arrays(closed)

        ema_fast_arr = ema(closes, self._ema_fast_period)
        ema_slow_arr = ema(closes, self._ema_slow_period)
        if np.isnan(ema_fast_arr[-1]) or np.isnan(ema_slow_arr[-1]) or len(ema_fast_arr) < 2:
            return None

        ema_fast, ema_slow = float(ema_fast_arr[-1]), float(ema_slow_arr[-1])
        ema_fast_prev = float(ema_fast_arr[-2])
        ema_slow_prev = float(ema_slow_arr[-2])

        atr_vals = atr(highs, lows, closes, self._settings.atr_period)
        atr_value = float(atr_vals[-1]) if not np.isnan(atr_vals[-1]) else 0.0

        current_price = Decimal(str(closes[-1]))

        bullish_cross = ema_fast_prev <= ema_slow_prev and ema_fast > ema_slow
        bearish_cross = ema_fast_prev >= ema_slow_prev and ema_fast < ema_slow

        # ── SALIDA: el momentum que motivó la entrada se agota ────────────────
        if position and position.is_open:
            if bearish_cross:
                return Signal(
                    strategy_id=self.strategy_id,
                    symbol=symbol,
                    direction=SignalDirection.CLOSE_LONG,
                    entry_price=current_price,
                    stop_loss=current_price,
                    confidence=0.75,
                    metadata={"exit_reason": "trend_momentum_faded"},
                )
            return None

        if atr_value <= 0:
            return None

        # ── ENTRADA LONG: cruce alcista + tendencia fuerte + volumen ──────────
        if not bullish_cross:
            return None

        adx_arr, plus_di_arr, minus_di_arr = adx(highs, lows, closes, self._adx_period)
        if np.isnan(adx_arr[-1]) or np.isnan(plus_di_arr[-1]) or np.isnan(minus_di_arr[-1]):
            return None

        adx_value = float(adx_arr[-1])
        if adx_value < self._adx_threshold or plus_di_arr[-1] <= minus_di_arr[-1]:
            return None

        obv_vals = obv(closes, volumes)
        if len(obv_vals) <= self._obv_lookback:
            return None
        if not (obv_vals[-1] > obv_vals[-self._obv_lookback]):
            # El precio sube pero el volumen no acompaña: alto riesgo de
            # falso quiebre, se descarta la entrada.
            return None

        atr_dec = Decimal(str(atr_value))
        stop_loss = current_price - atr_dec * Decimal(str(self._stop_atr_mult))

        # Suelo de rentabilidad neta idéntico al resto de estrategias: el
        # TP nunca puede quedar por debajo del coste real del round-trip
        # (comisiones + slippage) con margen — igual criterio que
        # mean_reversion/synthetic_arb/cross_currency_arb/cross_rate_pairs.
        min_tp_dist = current_price * Decimal(str(
            self._settings.round_trip_fee_pct
            + self._settings.min_net_profit_pct
            + self._settings.slippage_buffer_pct
        )) * Decimal("1.4")
        trend_tp_dist = atr_dec * Decimal(str(self._tp_atr_mult))
        take_profit = current_price + max(trend_tp_dist, min_tp_dist)

        adx_strength = (adx_value - self._adx_threshold) / self._adx_threshold
        confidence = min(max(adx_strength, 0.1), 1.0)
        strength = SignalStrength.STRONG if adx_value >= self._adx_threshold * 1.5 else SignalStrength.MODERATE

        signal = Signal(
            strategy_id=self.strategy_id,
            symbol=symbol,
            direction=SignalDirection.LONG,
            entry_price=current_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            strength=strength,
            confidence=round(confidence, 4),
            metadata={
                "adx": adx_value,
                "plus_di": float(plus_di_arr[-1]),
                "minus_di": float(minus_di_arr[-1]),
                "ema_fast": ema_fast,
                "ema_slow": ema_slow,
            },
        )
        logger.info(
            "strategy.signal_generated",
            strategy=self.strategy_id,
            symbol=symbol,
            direction="LONG",
            adx=f"{adx_value:.2f}",
            entry=str(current_price),
            stop=str(stop_loss),
            tp=str(take_profit),
        )
        return signal
