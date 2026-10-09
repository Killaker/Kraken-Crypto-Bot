"""
strategies/dip_rebound.py
Estrategia I — Dip Rebound.

Lógica de trading:
══════════════════
LONG (compra):
  - Tendencia de fondo bajista: EMA rápida < EMA lenta.
  - El precio ha caído >= dr_drop_pct desde el máximo de las últimas
    dr_lookback velas (caída puntual dentro de la tendencia bajista).

EXIT:
  - Take Profit: objetivo de beneficio NETO (ya descontadas ambas
    comisiones taker + slippage) = dr_target_net_pct. El precio bruto
    necesario para lograrlo es entry × (1 + target_net + coste_round_trip),
    de forma que el +2% quede en el bolsillo después de comisiones.
  - Stop Loss: ATR × dr_stop_atr_mult por debajo de la entrada.
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


class DipReboundStrategy(BaseStrategy):
    """
    Compra caídas puntuales (>= dr_drop_pct desde el máximo reciente)
    dentro de una tendencia bajista confirmada, apostando al rebote
    técnico de corto plazo. Cierra al alcanzar un beneficio NETO objetivo
    (comisiones + slippage ya descontados) o por stop ATR.
    """

    strategy_id = "DIP_REBOUND"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lookback = settings.dr_lookback
        self._drop_pct = settings.dr_drop_pct
        self._target_net_pct = settings.dr_target_net_pct
        self._ema_fast_period = settings.dr_ema_fast
        self._ema_slow_period = settings.dr_ema_slow
        self._atr_period = settings.atr_period
        self._stop_atr_mult = settings.dr_stop_atr_mult

        self.min_candles = max(
            self._lookback, self._ema_slow_period, self._atr_period
        ) + 20

        logger.info(
            "strategy.initialized",
            strategy=self.strategy_id,
            lookback=self._lookback,
            drop_pct=self._drop_pct,
            target_net_pct=self._target_net_pct,
            ema_fast=self._ema_fast_period,
            ema_slow=self._ema_slow_period,
        )

    # ─────────────────────────────────────────────────────────────────
    #  Interfaz pública
    # ─────────────────────────────────────────────────────────────────

    async def generate_signal(
        self,
        symbol: str,
        candles: list[Candle],
        ticker: Optional[Ticker] = None,
        position: Optional[Position] = None,
    ) -> Optional[Signal]:
        closed = [c for c in candles if c.is_closed]

        if not self.has_enough_data(closed):
            logger.debug(
                "strategy.insufficient_data",
                strategy=self.strategy_id,
                symbol=symbol,
                available=len(closed),
                required=self.min_candles,
            )
            return None

        _, highs, lows, closes, _ = self._to_arrays(closed)

        # ── SALIDA: ya gestionada por RiskManager.update_trailing_stops
        # (comprueba take_profit/stop_loss en cada tick real de precio,
        # más preciso que esperar al cierre de vela). Aquí solo evaluamos
        # entradas cuando no hay posición abierta de esta estrategia.
        if position is not None and position.is_open:
            return None

        ema_fast = ema(closes, self._ema_fast_period)
        ema_slow = ema(closes, self._ema_slow_period)

        if np.isnan(ema_fast[-1]) or np.isnan(ema_slow[-1]):
            return None

        # Tendencia de fondo bajista.
        if ema_fast[-1] >= ema_slow[-1]:
            return None

        recent_high = float(np.max(highs[-self._lookback:]))
        current_price_f = float(closes[-1])
        if recent_high <= 0:
            return None

        drop_pct = (current_price_f - recent_high) / recent_high  # negativo

        if drop_pct > -self._drop_pct:
            # Todavía no ha caído lo suficiente.
            return None

        # CORREGIDO: entrar en el mismo instante en que se toca el -1.5%
        # es "atrapar el cuchillo cayendo" — en las dos únicas operaciones
        # reales registradas (XRP, DOT) el precio siguió cayendo y ambas
        # cerraron por stop-loss (-1.6% / -1.7%), nunca llegaron a
        # rebotar. Se exige una primera señal de reversión antes de
        # comprar, para no entrar mientras la caída sigue en marcha.
        #
        # AJUSTADO: la versión original exigía cierre por encima del
        # mínimo de las DOS velas anteriores a la vez Y una vela alcista
        # — tres condiciones a la vez sobre la vela justa de la
        # confirmación, además de ya requerir tendencia bajista de fondo
        # y la caída del 1.5%. En 37h con 11 símbolos no coincidió ni una
        # vez. Ahora basta con UNA vela alcista que no haga mínimo nuevo
        # (cierre por encima del mínimo de la vela anterior) — sigue
        # filtrando la caída libre pura, pero ya no exige un patrón tan
        # específico.
        if not (closes[-1] > lows[-2] and closes[-1] >= closes[-2]):
            return None

        atr_vals = atr(highs, lows, closes, period=self._atr_period)
        atr_value = atr_vals[-1]
        if np.isnan(atr_value):
            return None

        current_price = Decimal(str(current_price_f))
        atr_dec = Decimal(str(atr_value))
        stop_loss = current_price - atr_dec * Decimal(str(self._stop_atr_mult))
        if stop_loss <= 0:
            return None

        # Precio bruto necesario para que, tras 2 legs de comisión taker +
        # slippage, el beneficio NETO sea dr_target_net_pct.
        total_cost_pct = (
            self._settings.round_trip_fee_pct
            + self._settings.slippage_buffer_pct
        )
        take_profit = current_price * (
            Decimal("1") + Decimal(str(self._target_net_pct)) + Decimal(str(total_cost_pct))
        )

        strength = (
            SignalStrength.STRONG
            if drop_pct <= -self._drop_pct * 1.5
            else SignalStrength.MODERATE
        )
        confidence = min(0.5 + abs(drop_pct) / self._drop_pct * 0.2, 0.95)

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
                "drop_pct": drop_pct,
                "recent_high": recent_high,
                "ema_fast": float(ema_fast[-1]),
                "ema_slow": float(ema_slow[-1]),
                "atr": float(atr_value),
            },
        )
        logger.info(
            "strategy.signal_generated",
            strategy=self.strategy_id,
            symbol=symbol,
            direction="LONG",
            drop_pct=f"{drop_pct * 100:.3f}",
            entry=str(current_price),
            stop=str(stop_loss),
            tp=str(take_profit),
        )
        return signal
