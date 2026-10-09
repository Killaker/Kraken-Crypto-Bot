"""
models/order.py
Modelos para órdenes, posiciones y fills.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional
import uuid


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP_MARKET = "STOP_MARKET"
    STOP_LIMIT = "STOP_LIMIT"
    TRAILING_STOP = "TRAILING_STOP"


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class PositionSide(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    BOTH = "BOTH"  # Hedge mode off


@dataclass(slots=True)
class Order:
    """Orden enviada al exchange o simulada en modo PAPER."""
    symbol: str
    side: OrderSide
    order_type: OrderType
    qty: Decimal
    price: Optional[Decimal] = None        # None para MARKET
    stop_price: Optional[Decimal] = None   # Para STOP_*
    trailing_delta: Optional[Decimal] = None  # Para TRAILING_STOP (bps)
    position_side: PositionSide = PositionSide.BOTH
    reduce_only: bool = False
    time_in_force: str = "GTC"
    strategy_id: str = ""
    signal_id: str = ""

    # Campos llenados por el exchange / paper engine
    order_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    client_order_id: str = field(default_factory=lambda: f"bot_{uuid.uuid4().hex[:12]}")
    status: OrderStatus = OrderStatus.PENDING
    filled_qty: Decimal = field(default=Decimal("0"))
    avg_fill_price: Optional[Decimal] = None
    commission: Decimal = field(default=Decimal("0"))
    commission_asset: str = "USDT"
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    exchange_order_id: Optional[str] = None

    @property
    def is_filled(self) -> bool:
        return self.status == OrderStatus.FILLED

    @property
    def is_active(self) -> bool:
        return self.status in (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED)

    @property
    def remaining_qty(self) -> Decimal:
        return self.qty - self.filled_qty

    def __repr__(self) -> str:
        return (
            f"Order({self.symbol} {self.side.value} {self.order_type.value} "
            f"qty={self.qty} price={self.price} status={self.status.value})"
        )


@dataclass(slots=True)
class Fill:
    """Ejecución parcial o total de una orden."""
    order_id: str
    symbol: str
    side: OrderSide
    price: Decimal
    qty: Decimal
    commission: Decimal
    commission_asset: str
    timestamp: datetime = field(default_factory=datetime.utcnow)
    trade_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    is_maker: bool = False


@dataclass(slots=True)
class Position:
    """
    Posición abierta en el exchange o en el paper engine.
    Actualizada en tiempo real con cada fill.
    """
    symbol: str
    side: PositionSide
    strategy_id: str

    qty: Decimal = field(default=Decimal("0"))
    entry_price: Decimal = field(default=Decimal("0"))
    current_price: Decimal = field(default=Decimal("0"))
    unrealized_pnl: Decimal = field(default=Decimal("0"))
    realized_pnl: Decimal = field(default=Decimal("0"))
    leverage: int = 1

    # Precio de 1 unidad de la cripto en EUR, congelado en el instante de
    # apertura (entry_price TARGET/BTC × BTC/EUR de ese momento). El
    # dashboard lo muestra como "Entrada" y NUNCA se recalcula cuando
    # BTC/EUR se mueve — si no, el usuario veía variar el precio de
    # entrada de una posición ya abierta.
    entry_eur: Optional[Decimal] = None
    entry_btc_eur_rate: Optional[Decimal] = None

    # Risk management
    stop_loss: Optional[Decimal] = None
    take_profit: Optional[Decimal] = None
    trailing_stop_price: Optional[Decimal] = None
    trailing_stop_delta: Optional[Decimal] = None  # En precio (no %)
    # Precio mínimo (LONG) / máximo (SHORT) al que el precio debe llegar
    # antes de que el trailing pueda empezar a armarse. Ver nota en
    # update_trailing_stop.
    trailing_activation_price: Optional[Decimal] = None

    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=datetime.utcnow)
    signal_id: str = ""

    # CORREGIDO: antes el trailing usaba como delta la distancia completa
    # entry↔stop (ATR de riesgo, p.ej. -1.4%). Con el trailing ya armado
    # tras cubrir el coste (~+0.95%), un retroceso de RUIDO NORMAL (0.5%)
    # cerraba la posición con bruto ≈ +0.5% — por debajo del round-trip
    # fee (0.52%) — es decir, PÉRDIDA NETA ASEGURADA. Visto en log: "8 de
    # los últimos 10 cierres" cortados así. El delta ahora es independiente
    # del stop: solo retrocesos realmente anormales disparan el cierre, y
    # cualquier cierre por trailing queda garantizado en beneficio neto
    # (ver ExecutionEngine._paper_open / _execute_direct_entry).
    trailing_stop_delta_profit: Optional[Decimal] = None

    @property
    def notional(self) -> Decimal:
        return self.qty * self.current_price

    @property
    def pnl_pct(self) -> float:
        if self.entry_price == 0:
            return 0.0
        return float((self.current_price - self.entry_price) / self.entry_price)

    @property
    def is_open(self) -> bool:
        return self.qty > Decimal("0")

    def update_trailing_stop(self, price: Decimal) -> bool:
        """
        Actualiza el trailing stop. Retorna True si el stop fue movido.
        Solo funciona si trailing_stop_delta está configurado.

        CORREGIDO (2ª vuelta): el fix anterior evitaba que el trailing
        empeorase el stop original, pero seguía ARMÁNDOSE en cuanto el
        precio subía UN TICK por encima del entry — mucho antes de que esa
        subida cubriera el coste real del round-trip two-hop (~1.04% en
        comisiones + slippage). Ejemplo real del log: entry sube +0.544%,
        el trailing se arma justo debajo de ese pico, el precio retrocede
        un poco (ruido normal) y cierra con ganancia BRUTA de ~0.25% —
        pero las comisiones (1.04%) se comen esa ganancia y el resultado
        neto es pérdida. Esto pasó en 8 de los últimos 10 cierres.
        Ahora el trailing no puede empezar a armarse hasta que el precio
        alcanza `trailing_activation_price` (fijado al abrir la posición
        = entry × (1 + coste_total_two_hop × margen)), garantizando que
        cualquier cierre disparado por el trailing ya cubre comisiones y
        deja beneficio neto real. Antes de esa activación, el stop_loss
        original (ATR) sigue siendo la única protección, vía is_stop_hit().
        """
        if self.trailing_stop_delta is None:
            return False

        # Delta del trailing: el nuevo "profit delta" si está configurado
        # (garantiza beneficio neto al cortar), con fallback al delta
        # antiguo por compatibilidad.
        delta = self.trailing_stop_delta_profit
        if delta is None or delta <= 0:
            delta = self.trailing_stop_delta

        moved = False
        if self.side == PositionSide.LONG:
            if self.trailing_activation_price is not None and price < self.trailing_activation_price:
                return False
            new_stop = price - delta
            floor = self.stop_loss if self.stop_loss is not None else new_stop
            if new_stop > floor and (self.trailing_stop_price is None or new_stop > self.trailing_stop_price):
                self.trailing_stop_price = new_stop
                moved = True
        elif self.side == PositionSide.SHORT:
            if self.trailing_activation_price is not None and price > self.trailing_activation_price:
                return False
            new_stop = price + delta
            ceiling = self.stop_loss if self.stop_loss is not None else new_stop
            if new_stop < ceiling and (self.trailing_stop_price is None or new_stop < self.trailing_stop_price):
                self.trailing_stop_price = new_stop
                moved = True
        return moved

    def is_stop_hit(self, price: Decimal) -> bool:
        """
        True si el precio actual tocó el stop activo (trailing si ya se
        activó a favor, si no el stop_loss original).

        CORREGIDO: antes solo comprobaba trailing_stop_price; mientras
        éste fuera None (trailing sin activar) NINGÚN stop protegía la
        posición — el stop_loss calculado por la estrategia (dimensionado
        según risk_per_trade) se guardaba pero nunca se comprobaba contra
        el precio, dejando la posición sin protección real de pérdidas
        hasta que el precio subiera al menos una vez.
        """
        if self.trailing_stop_price is not None:
            if self.side == PositionSide.LONG:
                return price <= self.trailing_stop_price
            return price >= self.trailing_stop_price

        if self.stop_loss is not None:
            if self.side == PositionSide.LONG:
                return price <= self.stop_loss
            return price >= self.stop_loss

        return False

    def __repr__(self) -> str:
        return (
            f"Position({self.symbol} {self.side.value} qty={self.qty} "
            f"entry={self.entry_price} pnl={self.unrealized_pnl:.4f})"
        )
