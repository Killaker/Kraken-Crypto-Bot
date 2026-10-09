"""
utils/filters.py
Filtros de precisión de Binance: lot size, tick size, notional mínimo.
Aplica las reglas del exchange para que las órdenes no sean rechazadas.
"""
from __future__ import annotations

import math
from decimal import ROUND_DOWN, Decimal
from typing import Optional

from core.logger import get_logger

logger = get_logger(__name__)


def _step_precision(step: Decimal) -> int:
    """Retorna el número de decimales del step size."""
    if step >= 1:
        return 0
    return abs(int(math.floor(math.log10(float(step)))))


def round_step_size(value: Decimal, step: Decimal) -> Decimal:
    """
    Redondea `value` hacia abajo al múltiplo más cercano de `step`.
    Usado para cantidad (LOT_SIZE) y precio (PRICE_FILTER).
    """
    if step <= 0:
        return value
    precision = _step_precision(step)
    # floor al múltiplo de step
    rounded = (value // step) * step
    return rounded.quantize(Decimal(10) ** -precision, rounding=ROUND_DOWN)


def apply_lot_size_filter(
    qty: Decimal,
    min_qty: Decimal,
    max_qty: Decimal,
    step_size: Decimal,
) -> Optional[Decimal]:
    """
    Aplica el filtro LOT_SIZE de Binance.
    Retorna la cantidad ajustada o None si queda por debajo del mínimo.
    """
    adjusted = round_step_size(qty, step_size)
    if adjusted < min_qty:
        logger.warning(
            "lot_size_filter.below_minimum",
            qty=str(qty),
            adjusted=str(adjusted),
            min_qty=str(min_qty),
        )
        return None
    if adjusted > max_qty:
        adjusted = round_step_size(max_qty, step_size)
    return adjusted


def apply_price_filter(
    price: Decimal,
    min_price: Decimal,
    max_price: Decimal,
    tick_size: Decimal,
) -> Optional[Decimal]:
    """
    Aplica el filtro PRICE_FILTER de Binance.
    Retorna el precio ajustado al tick o None si está fuera de rango.
    """
    adjusted = round_step_size(price, tick_size)
    if adjusted < min_price or adjusted > max_price:
        logger.warning(
            "price_filter.out_of_range",
            price=str(price),
            adjusted=str(adjusted),
            min_price=str(min_price),
            max_price=str(max_price),
        )
        return None
    return adjusted


def apply_notional_filter(
    qty: Decimal,
    price: Decimal,
    min_notional: Decimal,
) -> bool:
    """
    Verifica el filtro MIN_NOTIONAL / NOTIONAL.
    Retorna True si el notional >= mínimo requerido.
    """
    notional = qty * price
    if notional < min_notional:
        logger.warning(
            "notional_filter.below_minimum",
            notional=str(notional),
            min_notional=str(min_notional),
        )
        return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
#  Cálculo de tamaño de posición
# ─────────────────────────────────────────────────────────────────────────────

def calculate_position_size(
    account_balance: Decimal,
    risk_fraction: float,
    entry_price: Decimal,
    stop_loss: Decimal,
    leverage: int = 1,
) -> Decimal:
    """
    Calcula la cantidad a operar basada en riesgo fijo.

    Fórmula: qty = (balance × risk) / (|entry - stop| × leverage)

    Args:
        account_balance: Capital disponible en USDT.
        risk_fraction:   Fracción del balance a arriesgar (e.g. 0.01 = 1%).
        entry_price:     Precio estimado de entrada.
        stop_loss:       Precio del stop loss.
        leverage:        Apalancamiento (1 = sin apalancamiento).

    Returns:
        Cantidad en unidades base (ej. BTC), sin filtros aplicados.
    """
    risk_amount = account_balance * Decimal(str(risk_fraction))
    price_risk = abs(entry_price - stop_loss)

    if price_risk == 0 or entry_price == 0:
        logger.error("calculate_position_size.zero_price_risk")
        return Decimal("0")

    qty = (risk_amount * leverage) / price_risk
    return qty
