"""
strategies/delta_neutral_arb.py
Estrategia C — Delta-Neutral Arbitrage (Funding rates).

Lógica de trading:
══════════════════
Esta estrategia ejecuta la pata de Futuros de un arbitraje delta-neutral (Cash and Carry).
Busca capturar tasas de financiación (funding rates) extremas.
Para que la posición sea verdaderamente delta-neutral, se debe cubrir la exposición
comprando/vendiendo el activo subyacente en el mercado Spot (externamente o mediante
otra instancia del bot).

LONG:
  - Funding rate < -dna_funding_threshold (la tasa es muy negativa, los shorts pagan a los longs).
  - Se abre posición LONG en futuros para recibir los pagos de funding.

SHORT:
  - Funding rate > dna_funding_threshold (la tasa es muy positiva, los longs pagan a los shorts).
  - Se abre posición SHORT en futuros para recibir los pagos.

EXIT:
  - El funding rate se neutraliza (cruza 0 o cae por debajo de un umbral de salida).
  - O se alcanza el Stop Loss (para proteger contra movimientos bruscos de precio si la cobertura spot no es perfecta).
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

import numpy as np

from core.config import Settings
from core.logger import get_logger
from models.market_data import Candle, Ticker
from models.order import Position, PositionSide
from models.signal import Signal, SignalDirection, SignalStrength
from strategies.base_strategy import BaseStrategy
from utils.indicators import atr

logger = get_logger(__name__)


class DeltaNeutralArbStrategy(BaseStrategy):
    strategy_id = "DELTA_NEUTRAL_ARB"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._funding_threshold = Decimal(str(getattr(settings, "dna_funding_threshold", 0.001)))
        self._exit_threshold = self._funding_threshold / Decimal("2")  # Salir cuando baje a la mitad
        self._atr_period = settings.atr_period
        self._atr_multiplier = settings.atr_multiplier
        
        self.min_candles = self._atr_period + 10
        
        logger.info(
            "strategy.initialized",
            strategy=self.strategy_id,
            funding_threshold=str(self._funding_threshold),
        )

    async def generate_signal(
        self,
        symbol: str,
        candles: list[Candle],
        ticker: Optional[Ticker] = None,
        position: Optional[Position] = None,
    ) -> Optional[Signal]:
        if not ticker or ticker.funding_rate is None:
            # Solo funciona en futuros con funding rate
            return None

        closed = [c for c in candles if c.is_closed]
        if not self.has_enough_data(closed):
            return None

        current_price = ticker.last_price
        funding_rate = ticker.funding_rate

        # Calcular ATR para Stop Loss de emergencia
        _, highs, lows, closes, _ = self._to_arrays(closed)
        atr_vals = atr(highs, lows, closes, self._atr_period)
        atr_value = Decimal(str(atr_vals[-1])) if not np.isnan(atr_vals[-1]) else Decimal("0")
        
        atr_dist = atr_value * Decimal(str(self._atr_multiplier))

        # ── SEÑAL EXIT ────────────────────────────────────────────────────────
        # Si tenemos una posición, verificamos si debemos salir porque el funding rate se neutralizó
        if position and position.is_open:
            should_exit = False
            
            if position.side == PositionSide.LONG and funding_rate > -self._exit_threshold:
                should_exit = True
            elif position.side == PositionSide.SHORT and funding_rate < self._exit_threshold:
                should_exit = True
                
            if should_exit:
                direction = SignalDirection.CLOSE_LONG if position.side == PositionSide.LONG else SignalDirection.CLOSE_SHORT
                return Signal(
                    strategy_id=self.strategy_id,
                    symbol=symbol,
                    direction=direction,
                    entry_price=current_price,
                    stop_loss=current_price,
                    confidence=1.0,
                    metadata={
                        "exit_reason": "funding_rate_neutralized",
                        "funding_rate": str(funding_rate),
                    }
                )
            
            # Si ya tenemos posición y no debemos salir, no generamos nuevas entradas
            return None

        # ── SEÑAL LONG (Funding negativo -> Shorts pagan a Longs) ─────────────
        if funding_rate <= -self._funding_threshold:
            stop_loss = current_price - atr_dist
            
            return Signal(
                strategy_id=self.strategy_id,
                symbol=symbol,
                direction=SignalDirection.LONG,
                entry_price=current_price,
                stop_loss=stop_loss,
                confidence=0.8,
                strength=SignalStrength.STRONG,
                metadata={
                    "funding_rate": str(funding_rate),
                    "threshold": str(-self._funding_threshold)
                }
            )

        # ── SEÑAL SHORT (Funding positivo -> Longs pagan a Shorts) ────────────
        if funding_rate >= self._funding_threshold:
            stop_loss = current_price + atr_dist
            
            return Signal(
                strategy_id=self.strategy_id,
                symbol=symbol,
                direction=SignalDirection.SHORT,
                entry_price=current_price,
                stop_loss=stop_loss,
                confidence=0.8,
                strength=SignalStrength.STRONG,
                metadata={
                    "funding_rate": str(funding_rate),
                    "threshold": str(self._funding_threshold)
                }
            )

        return None
