"""
models/market_data.py
Dataclasses inmutables para datos de mercado.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Optional


@dataclass(frozen=True, slots=True)
class Candle:
    """OHLCV kline data."""
    symbol: str
    timeframe: str
    open_time: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    quote_volume: Decimal
    trades: int
    is_closed: bool = True

    @property
    def typical_price(self) -> Decimal:
        return (self.high + self.low + self.close) / Decimal("3")

    @property
    def spread(self) -> Decimal:
        return self.high - self.low

    def __repr__(self) -> str:
        return (
            f"Candle({self.symbol} {self.timeframe} "
            f"O={self.open} H={self.high} L={self.low} C={self.close} "
            f"V={self.volume})"
        )


@dataclass(frozen=True, slots=True)
class OrderBookLevel:
    """Un nivel del order book (precio, cantidad)."""
    price: Decimal
    qty: Decimal


@dataclass(frozen=True, slots=True)
class OrderBook:
    """Snapshot del order book."""
    symbol: str
    timestamp: datetime
    bids: tuple[OrderBookLevel, ...]  # Ordenados de mayor a menor precio
    asks: tuple[OrderBookLevel, ...]  # Ordenados de menor a mayor precio

    @property
    def best_bid(self) -> Optional[OrderBookLevel]:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> Optional[OrderBookLevel]:
        return self.asks[0] if self.asks else None

    @property
    def mid_price(self) -> Optional[Decimal]:
        if self.best_bid and self.best_ask:
            return (self.best_bid.price + self.best_ask.price) / Decimal("2")
        return None

    @property
    def spread(self) -> Optional[Decimal]:
        if self.best_bid and self.best_ask:
            return self.best_ask.price - self.best_bid.price
        return None


@dataclass(frozen=True, slots=True)
class Ticker:
    """Datos de ticker en tiempo real."""
    symbol: str
    timestamp: datetime
    last_price: Decimal
    bid: Decimal
    ask: Decimal
    volume_24h: Decimal
    price_change_pct_24h: float
    funding_rate: Optional[Decimal] = None          # Solo Futures
    next_funding_time: Optional[datetime] = None    # Solo Futures
    open_interest: Optional[Decimal] = None         # Solo Futures


@dataclass(slots=True)
class AggTrade:
    """Trade agregado del exchange."""
    symbol: str
    timestamp: datetime
    price: Decimal
    qty: Decimal
    is_buyer_maker: bool  # True => trade bajista (vendedor agresivo)

    @property
    def side_delta(self) -> Decimal:
        """Positivo = compra agresiva, negativo = venta agresiva."""
        return self.qty if not self.is_buyer_maker else -self.qty
