"""
strategies/base_strategy.py
Clase base abstracta para todas las estrategias.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Optional

import numpy as np

from models.market_data import Candle, Ticker
from models.order import Position
from models.signal import Signal


class BaseStrategy(ABC):
    """
    ABC de la que heredan todas las estrategias de trading.

    Cada estrategia implementa `generate_signal()` que recibe
    el array de candles cerrados y retorna un Signal o None.
    """

    strategy_id: str = "base"
    min_candles: int = 50  # Mínimo de velas necesarias

    @abstractmethod
    async def generate_signal(
        self,
        symbol: str,
        candles: list[Candle],
        ticker: Optional[Ticker] = None,
        position: Optional[Position] = None,
    ) -> Optional[Signal]:
        """
        Analiza los candles y retorna una señal de trading o None.

        Args:
            symbol:  Par de trading (e.g. "BTCUSDT").
            candles: Lista de candles cerrados, ordenados de antiguo a nuevo.

        Returns:
            Signal con dirección, entry, SL, TP y metadata; o None.
        """
        ...

    def _to_arrays(
        self,
        candles: list[Candle],
    ) -> tuple[
        np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray
    ]:
        """Convierte candles a arrays numpy (OHLCV) de float64."""
        opens = np.array([float(c.open) for c in candles], dtype=np.float64)
        highs = np.array([float(c.high) for c in candles], dtype=np.float64)
        lows = np.array([float(c.low) for c in candles], dtype=np.float64)
        closes = np.array([float(c.close) for c in candles], dtype=np.float64)
        volumes = np.array([float(c.volume) for c in candles], dtype=np.float64)
        return opens, highs, lows, closes, volumes

    def has_enough_data(self, candles: list[Candle]) -> bool:
        return len(candles) >= self.min_candles

    def _min_tp_distance(self, price: Decimal) -> Decimal:
        """
        Distancia mínima de TP para que, tras 2 legs de comisión + slippage
        + margen de beneficio neto, la operación no sea un break-even
        disfrazado. Si el edge estadístico de la señal no llega aquí,
        no hay que abrir: inflar el TP por encima del edge real solo
        produce timeouts con pérdida de fees (visto en TRIANGULAR_ARB).
        """
        settings = getattr(self, "_settings", None)
        if settings is None or price <= 0:
            return Decimal("0")
        # AJUSTADO: el margen ×1.4 por encima de coste+beneficio mínimo ya
        # incluye un 0.6% de beneficio neto en la base (min_net_profit_pct)
        # — el ×1.4 es margen EXTRA sobre eso. En el log, 176 señales de
        # TRIANGULAR_ARB se descartaron por no llegar a este umbral
        # (2.058%), muchas bastante cerca (1.8-1.9%) — edge estadístico
        # real que se perdía por un margen de seguridad extra ya cubierto
        # por min_net_profit_pct. Bajado a ×1.15.
        return price * Decimal(str(
            settings.round_trip_fee_pct
            + settings.min_net_profit_pct
            + settings.slippage_buffer_pct
        )) * Decimal("1.15")
