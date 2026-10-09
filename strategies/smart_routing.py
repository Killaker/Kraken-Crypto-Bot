"""
strategies/smart_routing.py
Estrategia — Two-Hop Smart Order Routing.

Lógica de trading:
══════════════════
No es una estrategia direccional clásica: en vez de predecir dirección de
precio, evalúa la CALIDAD DE EJECUCIÓN del two-hop en el momento actual y
solo deja pasar señales de las otras estrategias (o genera las suyas propias
sobre pullbacks de tendencia) cuando el routing es favorable:

  - Spread bid/ask de la leg de TARGET dentro de un máximo tolerable
    (spreads anchos destruyen cualquier ventaja two-hop).
  - Momentum de corto plazo (EMA rápida vs EMA lenta) a favor de LONG.
  - "Edge" estimado (distancia al TP menos coste total) por encima de un
    mínimo — solo enruta la orden si el edge esperado neto es positivo con
    margen, no solo positivo a secas.

En la práctica actúa como filtro de calidad de ejecución + señal de
momentum simple, pensada para operar en condiciones donde el spread es
estrecho y hay tendencia de corto plazo — exactamente el escenario donde el
two-hop tiene más probabilidad de ser rentable neto.

LONG:
  - Spread actual (ask-bid)/mid <= sr_max_spread_pct
  - EMA rápida > EMA lenta (momentum alcista de corto plazo)
  - Edge esperado (proyección ATR) > sr_min_edge_pct

EXIT:
  - EMA rápida cruza por debajo de la EMA lenta (momentum se agota)
  - Trailing stop ATR (gestionado por RiskManager)
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
from utils.indicators import atr, ema

logger = get_logger(__name__)


class SmartRoutingStrategy(BaseStrategy):
    strategy_id = "SMART_ROUTING"

    _EMA_FAST = 9
    _EMA_SLOW = 21

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._max_spread = settings.sr_max_spread_pct
        self._min_edge = settings.sr_min_edge_pct
        self._atr_period = settings.atr_period
        self._atr_multiplier = settings.atr_multiplier

        self.min_candles = max(self._EMA_SLOW * 2, self._atr_period) + 20

        logger.info(
            "strategy.initialized",
            strategy=self.strategy_id,
            max_spread=self._max_spread,
            min_edge=self._min_edge,
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

        if ticker is None or ticker.bid <= 0 or ticker.ask <= 0:
            return None

        _, highs, lows, closes, _ = self._to_arrays(closed)

        mid = (ticker.bid + ticker.ask) / Decimal("2")
        if mid <= 0:
            return None
        spread_pct = float((ticker.ask - ticker.bid) / mid)

        ema_fast_arr = ema(closes, self._EMA_FAST)
        ema_slow_arr = ema(closes, self._EMA_SLOW)
        if np.isnan(ema_fast_arr[-1]) or np.isnan(ema_slow_arr[-1]):
            return None

        ema_fast, ema_slow = float(ema_fast_arr[-1]), float(ema_slow_arr[-1])
        ema_fast_prev = float(ema_fast_arr[-2]) if len(ema_fast_arr) > 1 else ema_fast
        ema_slow_prev = float(ema_slow_arr[-2]) if len(ema_slow_arr) > 1 else ema_slow

        atr_vals = atr(highs, lows, closes, self._atr_period)
        atr_value = float(atr_vals[-1]) if not np.isnan(atr_vals[-1]) else 0.0

        current_price = Decimal(str(closes[-1]))

        # ── SALIDA: momentum se agota (cruce bajista de EMAs) ──────────────────
        if position and position.is_open:
            bearish_cross = ema_fast_prev >= ema_slow_prev and ema_fast < ema_slow
            if bearish_cross:
                return Signal(
                    strategy_id=self.strategy_id,
                    symbol=symbol,
                    direction=SignalDirection.CLOSE_LONG,
                    entry_price=current_price,
                    stop_loss=current_price,
                    confidence=0.75,
                    metadata={"exit_reason": "ema_bearish_cross"},
                )
            return None

        # ── FILTRO DE CALIDAD DE ROUTING ────────────────────────────────────────
        if spread_pct > self._max_spread:
            logger.debug(
                "strategy.spread_too_wide",
                strategy=self.strategy_id,
                symbol=symbol,
                spread=f"{spread_pct:.4f}",
                max=self._max_spread,
            )
            return None

        if atr_value <= 0:
            return None

        # ── ENTRADA LONG: momentum a favor + edge neto positivo con margen ────
        # AJUSTADO: antes exigía que el cruce alcista ocurriera EN ESTA
        # MISMA VELA (ema_fast_prev <= ema_slow_prev) — una ventana de
        # exactamente 5 minutos para que además el spread fuera estrecho
        # Y el edge superara el mínimo, los tres a la vez. En 37h con 11
        # símbolos no coincidió ni una vez. Ahora basta con que el
        # momentum siga siendo alcista (no hace falta pillar el instante
        # exacto del cruce) — el filtro de spread+edge sigue aplicando
        # igual de estricto, y la posición ya abierta bloquea duplicados.
        bullish = ema_fast > ema_slow
        if not bullish:
            return None

        atr_dec = Decimal(str(atr_value))
        stop_loss = current_price - atr_dec * Decimal(str(self._atr_multiplier))
        take_profit = current_price + atr_dec * Decimal(str(self._atr_multiplier)) * Decimal("1.5")

        total_cost = Decimal(str(
            self._settings.round_trip_fee_pct
            + self._settings.min_net_profit_pct
            + self._settings.slippage_buffer_pct
        )) + Decimal(str(spread_pct))  # el spread de ejecución también es coste

        expected_reward_pct = (take_profit - current_price) / current_price
        edge = expected_reward_pct - total_cost

        if edge < Decimal(str(self._min_edge)):
            logger.debug(
                "strategy.edge_below_minimum",
                strategy=self.strategy_id,
                symbol=symbol,
                edge=str(edge),
                required=str(self._min_edge),
            )
            return None

        confidence = min(float(edge) / (self._min_edge * 3.0), 1.0)
        strength = SignalStrength.STRONG if edge >= Decimal(str(self._min_edge)) * 2 else SignalStrength.MODERATE

        signal = Signal(
            strategy_id=self.strategy_id,
            symbol=symbol,
            direction=SignalDirection.LONG,
            entry_price=current_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            strength=strength,
            confidence=round(max(confidence, 0.1), 4),
            metadata={
                "spread_pct": spread_pct,
                "edge_pct": float(edge),
                "ema_fast": ema_fast,
                "ema_slow": ema_slow,
                "atr": atr_value,
            },
        )
        logger.info(
            "strategy.signal_generated",
            strategy=self.strategy_id,
            symbol=symbol,
            direction="LONG",
            spread=f"{spread_pct:.4f}",
            edge=str(edge),
            entry=str(current_price),
            stop=str(stop_loss),
            tp=str(take_profit),
        )
        return signal
