"""
core/exceptions.py
Excepciones custom del bot de trading.
"""
from __future__ import annotations


class TradingBotError(Exception):
    """Excepción base del bot."""


class KillSwitchTriggered(TradingBotError):
    """Se disparó el kill-switch por drawdown diario."""

    def __init__(self, drawdown: float, limit: float) -> None:
        self.drawdown = drawdown
        self.limit = limit
        super().__init__(
            f"Kill-switch activado: drawdown {drawdown:.2%} >= límite {limit:.2%}"
        )


class RiskLimitExceeded(TradingBotError):
    """El riesgo calculado de una orden supera el límite permitido."""


class InsufficientDataError(TradingBotError):
    """No hay suficientes candles/histórico para calcular una estrategia o indicador."""


class InsufficientBalanceError(TradingBotError):
    """Balance insuficiente para ejecutar la orden."""


class OrderError(TradingBotError):
    """Error genérico al colocar/gestionar una orden en el exchange."""


class RateLimitError(TradingBotError):
    """El exchange rechazó la solicitud por rate limit."""


class WebSocketDisconnectedError(TradingBotError):
    """El WebSocket se desconectó inesperadamente."""


class MaxReconnectsExceeded(TradingBotError):
    """Se superó el número máximo de intentos de reconexión WS."""
