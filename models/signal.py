"""
models/signal.py
Señales de trading generadas por las estrategias.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional
import uuid


class SignalDirection(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    CLOSE_LONG = "CLOSE_LONG"
    CLOSE_SHORT = "CLOSE_SHORT"
    NEUTRAL = "NEUTRAL"


class SignalStrength(str, Enum):
    WEAK = "WEAK"
    MODERATE = "MODERATE"
    STRONG = "STRONG"


@dataclass(slots=True)
class Signal:
    """
    Señal de trading producida por una estrategia.
    Contiene toda la información necesaria para que el RiskManager
    y el ExecutionEngine procesen la orden.
    """
    strategy_id: str
    symbol: str
    direction: SignalDirection
    entry_price: Decimal                    # Precio estimado de entrada
    stop_loss: Decimal                      # Stop loss calculado
    take_profit: Optional[Decimal] = None   # Take profit (puede ser None si trailing)
    strength: SignalStrength = SignalStrength.MODERATE
    confidence: float = 0.5                 # [0.0, 1.0]
    timestamp: datetime = field(default_factory=datetime.utcnow)
    signal_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    metadata: dict = field(default_factory=dict)  # Indicadores, Z-scores, etc.

    @property
    def risk_reward(self) -> Optional[float]:
        """Ratio riesgo/beneficio. None si no hay take_profit."""
        if self.take_profit is None:
            return None
        risk = abs(self.entry_price - self.stop_loss)
        reward = abs(self.take_profit - self.entry_price)
        if risk == 0:
            return None
        return float(reward / risk)

    @property
    def is_entry(self) -> bool:
        return self.direction in (SignalDirection.LONG, SignalDirection.SHORT)

    @property
    def is_exit(self) -> bool:
        return self.direction in (
            SignalDirection.CLOSE_LONG,
            SignalDirection.CLOSE_SHORT,
        )

    def __repr__(self) -> str:
        return (
            f"Signal({self.strategy_id} {self.symbol} "
            f"{self.direction.value} @ {self.entry_price} "
            f"SL={self.stop_loss} conf={self.confidence:.2f})"
        )
