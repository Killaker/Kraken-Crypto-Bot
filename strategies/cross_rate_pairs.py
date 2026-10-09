"""
strategies/cross_rate_pairs.py
Estrategia — Cross-Rate Pairs Trading (two-hop).

Lógica de trading:
══════════════════
Toma dos símbolos TARGET correlacionados (ambos cotizados contra el mismo
BRIDGE, p.ej. ETH/BTC y SOL/BTC) y calcula su cross-rate implícito:
    cross_rate = price(symbol_a/BRIDGE) / price(symbol_b/BRIDGE)

Cuando el z-score de ese cross-rate se aleja significativamente de su media
móvil, se asume una divergencia temporal entre ambos activos que tiende a
revertir. Como el bot no puede ir corto en SPOT, esta versión solo opera el
lado LONG del par que está relativamente barato (symbol_a infravalorado
frente a symbol_b), comprándolo vía two-hop y cerrando cuando el z-score
revierte a la zona neutra — igual que Mean Reversion pero sobre el cross-rate
en vez del precio absoluto.

Necesita datos de AMBOS símbolos, así que requiere que `set_data_engine()`
sea llamado tras la construcción (lo hace StrategyEngine al cargarla) para
poder leer las velas del "otro" símbolo del par, ya que `generate_signal`
solo recibe las velas del símbolo que dispara el callback.

LONG (symbol_a):
  - z-score del cross-rate < -crp_zscore_entry (symbol_a barato vs symbol_b)

EXIT:
  - z-score vuelve dentro de ±crp_zscore_exit
  - Stop Loss: ATR × multiplier sobre el precio absoluto de symbol_a
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


class CrossRatePairsStrategy(BaseStrategy):
    strategy_id = "CROSS_RATE_PAIRS"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lookback = settings.crp_lookback
        self._zscore_entry = settings.crp_zscore_entry
        self._zscore_exit = settings.crp_zscore_exit
        self._atr_period = settings.atr_period
        self._atr_multiplier = settings.atr_multiplier
        self._data_engine = None  # inyectado por StrategyEngine

        symbols = settings.trading_symbols
        self._symbol_a = settings.crp_pair_symbol_a or (symbols[0] if len(symbols) > 0 else "")
        self._symbol_b = settings.crp_pair_symbol_b or (symbols[1] if len(symbols) > 1 else "")

        self.min_candles = max(self._lookback, self._atr_period) + 20

        logger.info(
            "strategy.initialized",
            strategy=self.strategy_id,
            symbol_a=self._symbol_a,
            symbol_b=self._symbol_b,
            lookback=self._lookback,
        )

    def set_data_engine(self, data_engine) -> None:
        """Inyectado externamente: permite leer velas del símbolo compañero."""
        self._data_engine = data_engine

    async def generate_signal(
        self,
        symbol: str,
        candles: list[Candle],
        ticker: Optional[Ticker] = None,
        position: Optional[Position] = None,
    ) -> Optional[Signal]:
        if not self._symbol_a or not self._symbol_b or self._symbol_a == self._symbol_b:
            return None

        # Esta estrategia solo opera el símbolo A del par; ignora callbacks
        # de cualquier otro símbolo (incluido symbol_b, que solo se usa como
        # referencia de cross-rate).
        if symbol != self._symbol_a:
            return None

        if self._data_engine is None:
            return None

        candles_a = [c for c in candles if c.is_closed]
        candles_b = [c for c in self._data_engine.get_candles(self._symbol_b) if c.is_closed]

        if not self.has_enough_data(candles_a) or len(candles_b) < self.min_candles:
            return None

        n = min(len(candles_a), len(candles_b), self._lookback + 5)
        closes_a = np.array([float(c.close) for c in candles_a[-n:]], dtype=np.float64)
        closes_b = np.array([float(c.close) for c in candles_b[-n:]], dtype=np.float64)

        if np.any(closes_b == 0):
            return None

        cross_rate = closes_a / closes_b
        if len(cross_rate) < self._lookback:
            return None

        window = cross_rate[-self._lookback:]
        mean = float(np.mean(window))
        std = float(np.std(window))
        if std == 0:
            return None

        current_cross = float(cross_rate[-1])
        zscore = (current_cross - mean) / std

        _, highs_a, lows_a, closes_a_full, _ = self._to_arrays(candles_a)
        atr_vals = atr(highs_a, lows_a, closes_a_full, self._atr_period)
        atr_value = float(atr_vals[-1]) if not np.isnan(atr_vals[-1]) else 0.0

        current_price = Decimal(str(closes_a_full[-1]))

        # ── SALIDA: z-score vuelve a zona neutra ────────────────────────────────
        if position and position.is_open:
            if abs(zscore) < self._zscore_exit:
                return Signal(
                    strategy_id=self.strategy_id,
                    symbol=symbol,
                    direction=SignalDirection.CLOSE_LONG,
                    entry_price=current_price,
                    stop_loss=current_price,
                    confidence=0.8,
                    metadata={"exit_reason": "cross_rate_reversion", "zscore": zscore},
                )
            return None

        # ── ENTRADA LONG: symbol_a infravalorado vs symbol_b ────────────────────
        if zscore <= -self._zscore_entry and atr_value > 0:
            atr_dec = Decimal(str(atr_value))
            stop_loss = current_price - atr_dec * Decimal(str(self._atr_multiplier))

            # CORREGIDO: el suelo de TP no llevaba colchón de seguridad
            # (a diferencia de mean_reversion/synthetic_arb/cross_currency_arb,
            # que aplican 1.3x). El execution_engine ahora exige un margen del
            # 1.4x sobre el coste total antes de ejecutar cualquier señal — sin
            # este colchón, el suelo de esta estrategia quedaba sistemáticamente
            # por debajo de ese requisito y las señales eran rechazadas en
            # silencio (o, si pasaban justo, sin margen real de beneficio).
            min_tp_dist = current_price * Decimal(str(
                self._settings.round_trip_fee_pct
                + self._settings.min_net_profit_pct
                + self._settings.slippage_buffer_pct
            )) * Decimal("1.4")
            # Proyección de reversión proporcional a la magnitud del z-score
            reversion_dist = current_price * Decimal(str(min(abs(zscore) * std / mean, 0.05))) if mean > 0 else min_tp_dist
            take_profit = current_price + max(reversion_dist, min_tp_dist)

            confidence = min(abs(zscore) / (self._zscore_entry * 2.0), 1.0)
            strength = (
                SignalStrength.STRONG if abs(zscore) >= self._zscore_entry * 1.5
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
                    "zscore": zscore,
                    "cross_rate": current_cross,
                    "cross_rate_mean": mean,
                    "pair": f"{self._symbol_a}/{self._symbol_b}",
                },
            )
            logger.info(
                "strategy.signal_generated",
                strategy=self.strategy_id,
                symbol=symbol,
                direction="LONG",
                pair=f"{self._symbol_a}/{self._symbol_b}",
                zscore=f"{zscore:.3f}",
                entry=str(current_price),
                stop=str(stop_loss),
                tp=str(take_profit),
            )
            return signal

        return None
