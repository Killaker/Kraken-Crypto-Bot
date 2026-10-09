"""
strategies/synthetic_arb.py
Estrategia — Two-Hop Synthetic Arbitrage.

Lógica de trading:
══════════════════
En un two-hop BASE→BRIDGE→TARGET, el precio "sintético" de TARGET expresado
en BASE es: synthetic_rate = bridge_price(BASE→BRIDGE) × target_price(BRIDGE→TARGET).

Esta estrategia compara ese synthetic_rate actual contra su media móvil
reciente (proxy de "valor justo" de corto plazo). Cuando el rate actual se
desvía significativamente por debajo de la media, se interpreta como que
TARGET está temporalmente barato en términos sintéticos vía el bridge, y se
abre LONG esperando la reversión del spread sintético hacia su media.

No es arbitraje sin riesgo (no hay tercer mercado directo TARGET/BASE para
cerrar el triángulo instantáneamente en Kraken) — es una versión "soft" que
opera el two-hop existente cuando el spread sintético está estirado, con la
misma protección de rentabilidad neta que el resto de estrategias.

LONG:
  - synthetic_rate actual < media móvil − sa_min_deviation_pct
  - Confirmación: el spread lleva desviado más de 1 vela (evita ruido de 1 tick)

EXIT:
  - synthetic_rate vuelve a converger con la media (dentro de la mitad del umbral)
  - Stop Loss: ATR × multiplier, igual que el resto de estrategias
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
from utils.indicators import atr

logger = get_logger(__name__)


class SyntheticArbStrategy(BaseStrategy):
    strategy_id = "SYNTHETIC_ARB"

    _EXIT_BAND_FRACTION = 0.5  # sale cuando la desviación cae a la mitad del umbral

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._min_deviation = settings.sa_min_deviation_pct
        self._lookback = settings.sa_lookback
        self._atr_period = settings.atr_period
        self._atr_multiplier = settings.atr_multiplier

        self.min_candles = max(self._lookback, self._atr_period) + 20

        logger.info(
            "strategy.initialized",
            strategy=self.strategy_id,
            min_deviation=self._min_deviation,
            lookback=self._lookback,
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

        _, highs, lows, closes, _ = self._to_arrays(closed)

        # El "synthetic_rate" en este bot es simplemente el precio TARGET/BRIDGE
        # de la serie de candles (ya viene expresado en unidades del bridge,
        # que es la referencia que necesitamos para el two-hop). Usamos una
        # media móvil simple como proxy de valor justo de corto plazo.
        if len(closes) < self._lookback + 1:
            return None

        window = closes[-self._lookback:]
        mean_rate = float(np.mean(window))
        current_rate = float(closes[-1])

        if mean_rate <= 0:
            return None

        deviation = (current_rate - mean_rate) / mean_rate  # negativo = barato

        atr_vals = atr(highs, lows, closes, self._atr_period)
        atr_value = float(atr_vals[-1]) if not np.isnan(atr_vals[-1]) else 0.0
        if atr_value <= 0:
            return None

        current_price = Decimal(str(current_rate))
        atr_dist = Decimal(str(atr_value)) * Decimal(str(self._atr_multiplier))

        # ── SALIDA: convergencia del spread sintético ─────────────────────────
        if position and position.is_open:
            if abs(deviation) < self._min_deviation * self._EXIT_BAND_FRACTION:
                return Signal(
                    strategy_id=self.strategy_id,
                    symbol=symbol,
                    direction=SignalDirection.CLOSE_LONG,
                    entry_price=current_price,
                    stop_loss=current_price,
                    confidence=0.8,
                    metadata={
                        "exit_reason": "synthetic_spread_converged",
                        "deviation": deviation,
                    },
                )
            return None

        # ── ENTRADA LONG: rate sintético por debajo de su media ────────────────
        if deviation <= -self._min_deviation:
            # Confirmar persistencia: la vela anterior también debe estar
            # por debajo de la media (evita reaccionar a un solo tick de ruido).
            prev_window = closes[-self._lookback - 1:-1]
            prev_mean = float(np.mean(prev_window)) if len(prev_window) >= self._lookback else mean_rate
            prev_rate = float(closes[-2]) if len(closes) > 1 else current_rate
            prev_deviation = (prev_rate - prev_mean) / prev_mean if prev_mean > 0 else 0.0

            if prev_deviation > -self._min_deviation * 0.5:
                # No hay persistencia suficiente todavía
                return None

            stop_loss = current_price - atr_dist
            # TP: reversión hasta la media, con suelo en coste mínimo two-hop
            # Se aplica un multiplicador 1.3x sobre el coste mínimo para
            # garantizar que el TP tiene margen real sobre el umbral de
            # rentabilidad del execution engine (reward > total_cost).
            min_tp_dist = self._min_tp_distance(current_price)
            reversion_dist = Decimal(str(mean_rate)) - current_price
            if reversion_dist < min_tp_dist:
                logger.info(
                    "strategy.signal_skipped.edge_below_cost",
                    strategy=self.strategy_id,
                    symbol=symbol,
                    deviation=f"{deviation:.4f}",
                )
                return None
            take_profit = current_price + reversion_dist

            if take_profit <= current_price:
                return None

            confidence = min(abs(deviation) / (self._min_deviation * 2.0), 1.0)
            strength = (
                SignalStrength.STRONG if abs(deviation) >= self._min_deviation * 1.5
                else SignalStrength.MODERATE
            )

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
                    "deviation": deviation,
                    "mean_rate": mean_rate,
                    "atr": atr_value,
                },
            )
            logger.info(
                "strategy.signal_generated",
                strategy=self.strategy_id,
                symbol=symbol,
                direction="LONG",
                deviation=f"{deviation:.4f}",
                entry=str(current_price),
                stop=str(stop_loss),
                tp=str(take_profit),
            )
            return signal

        return None
