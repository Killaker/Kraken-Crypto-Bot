"""
strategies/cross_currency_arb.py
Estrategia — Two-Hop Cross-Currency Arbitrage.

Lógica de trading:
══════════════════
Compara, para un símbolo TARGET dado, su tasa observada TARGET/BRIDGE contra
la tasa "implícita" derivada de un tercer símbolo de referencia (REF) que
también cotiza contra el mismo BRIDGE:

    implied_rate = price(TARGET/BRIDGE) esperado si TARGET y REF mantuvieran
                   su ratio histórico medio (ventana móvil)

En vez de un tercer mercado real TARGET/REF (que Kraken no siempre ofrece de
forma líquida), se usa el símbolo con mayor volumen de `trading_symbols`
como referencia estructural: si TARGET se mueve mucho más que el mercado de
referencia en poco tiempo sin justificación de tendencia propia, se
interpreta como ineficiencia de precio cross-currency temporal y se opera la
reversión — igual filosofía que un arbitraje triangular clásico, adaptado a
que el bot no puede abrir el tercer leg directo.

LONG:
  - ratio TARGET/REF actual < media móvil del ratio − cca_min_edge_pct
  - REF no está en tendencia bajista fuerte (evita "atrapar cuchillos" si
    ambos caen y el ratio es ruido, no ineficiencia)

EXIT:
  - ratio converge a la media (dentro de la mitad del umbral)
  - Stop Loss: ATR × multiplier
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


class CrossCurrencyArbStrategy(BaseStrategy):
    strategy_id = "CROSS_CURRENCY_ARB"

    _EXIT_BAND_FRACTION = 0.5
    _REF_TREND_EMA = 20

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._min_edge = settings.cca_min_edge_pct
        self._lookback = settings.cca_lookback
        self._atr_period = settings.atr_period
        self._atr_multiplier = settings.atr_multiplier
        self._data_engine = None  # inyectado por StrategyEngine

        symbols = settings.trading_symbols
        # Referencia: el primer símbolo configurado que no sea el propio target
        # se resuelve dinámicamente por símbolo en generate_signal.
        self._all_symbols = list(symbols)

        self.min_candles = max(self._lookback, self._atr_period, self._REF_TREND_EMA * 2) + 20

        logger.info(
            "strategy.initialized",
            strategy=self.strategy_id,
            min_edge=self._min_edge,
            lookback=self._lookback,
        )

    def set_data_engine(self, data_engine) -> None:
        self._data_engine = data_engine

    def _pick_reference(self, symbol: str) -> Optional[str]:
        for s in self._all_symbols:
            if s != symbol:
                return s
        return None

    async def generate_signal(
        self,
        symbol: str,
        candles: list[Candle],
        ticker: Optional[Ticker] = None,
        position: Optional[Position] = None,
    ) -> Optional[Signal]:
        if self._data_engine is None:
            return None

        ref_symbol = self._pick_reference(symbol)
        if ref_symbol is None:
            return None

        closed = [c for c in candles if c.is_closed]
        ref_candles = [c for c in self._data_engine.get_candles(ref_symbol) if c.is_closed]

        if not self.has_enough_data(closed) or len(ref_candles) < self.min_candles:
            return None

        n = min(len(closed), len(ref_candles), self._lookback + self._REF_TREND_EMA + 5)
        closes = np.array([float(c.close) for c in closed[-n:]], dtype=np.float64)
        ref_closes = np.array([float(c.close) for c in ref_candles[-n:]], dtype=np.float64)

        if np.any(ref_closes == 0) or len(closes) < self._lookback:
            return None

        ratio = closes / ref_closes
        window = ratio[-self._lookback:]
        mean_ratio = float(np.mean(window))
        if mean_ratio == 0:
            return None

        current_ratio = float(ratio[-1])
        deviation = (current_ratio - mean_ratio) / mean_ratio

        # Filtro de tendencia en la referencia: evitar operar si REF está en
        # caída fuerte (el "abaratamiento" de TARGET podría ser contagio real,
        # no ineficiencia temporal).
        ref_ema_arr = ema(ref_closes, self._REF_TREND_EMA)
        ref_trend_down = False
        if not np.isnan(ref_ema_arr[-1]) and len(ref_ema_arr) > 5:
            ref_trend_down = ref_closes[-1] < ref_ema_arr[-1] * 0.98

        _, highs, lows, closes_full, _ = self._to_arrays(closed)
        atr_vals = atr(highs, lows, closes_full, self._atr_period)
        atr_value = float(atr_vals[-1]) if not np.isnan(atr_vals[-1]) else 0.0

        current_price = Decimal(str(closes_full[-1]))

        # ── SALIDA: ratio converge ───────────────────────────────────────────
        if position and position.is_open:
            if abs(deviation) < self._min_edge * self._EXIT_BAND_FRACTION:
                return Signal(
                    strategy_id=self.strategy_id,
                    symbol=symbol,
                    direction=SignalDirection.CLOSE_LONG,
                    entry_price=current_price,
                    stop_loss=current_price,
                    confidence=0.8,
                    metadata={"exit_reason": "cross_currency_converged", "deviation": deviation},
                )
            return None

        # ── ENTRADA LONG ─────────────────────────────────────────────────────
        if deviation <= -self._min_edge and not ref_trend_down and atr_value > 0:
            atr_dec = Decimal(str(atr_value))
            stop_loss = current_price - atr_dec * Decimal(str(self._atr_multiplier))

            min_tp_dist = self._min_tp_distance(current_price)
            reversion_dist = current_price * Decimal(str(min(abs(deviation), 0.05)))
            if reversion_dist < min_tp_dist:
                logger.info(
                    "strategy.signal_skipped.edge_below_cost",
                    strategy=self.strategy_id,
                    symbol=symbol,
                    deviation=f"{deviation:.4f}",
                )
                return None
            take_profit = current_price + reversion_dist

            confidence = min(abs(deviation) / (self._min_edge * 2.0), 1.0)
            strength = (
                SignalStrength.STRONG if abs(deviation) >= self._min_edge * 1.5
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
                    "ref_symbol": ref_symbol,
                    "deviation": deviation,
                    "ratio": current_ratio,
                    "ratio_mean": mean_ratio,
                },
            )
            logger.info(
                "strategy.signal_generated",
                strategy=self.strategy_id,
                symbol=symbol,
                direction="LONG",
                ref=ref_symbol,
                deviation=f"{deviation:.4f}",
                entry=str(current_price),
                stop=str(stop_loss),
                tp=str(take_profit),
            )
            return signal

        return None
