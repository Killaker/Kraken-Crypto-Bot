"""
strategies/portfolio_rebalance.py
Estrategia — Two-Hop Portfolio Rebalancing.

Lógica de trading:
══════════════════
No predice dirección de precio: mantiene una cartera de varios TARGETs cerca
de unos pesos objetivo (`pr_target_weights`, en fracción del balance total
disponible en BRIDGE). Cuando el peso actual de un símbolo se desvía de su
peso objetivo más allá de una banda de tolerancia, genera una señal LONG
(comprar más, vía two-hop) o CLOSE_LONG (vender el exceso) para volver a
aproximarlo al objetivo.

Es una estrategia de bajo turnover — solo actúa cada `pr_check_interval_bars`
velas para no generar ruido excesivo de rebalanceo, y respeta siempre la
validación de rentabilidad neta two-hop del ExecutionEngine (si el
rebalanceo no es rentable tras comisiones, no se ejecuta esa vez).

Si `pr_target_weights` está vacío, la estrategia reparte el peso a partes
iguales entre todos los `trading_symbols` configurados (rebalanceo pasivo
equal-weight), que es el caso de uso más simple y seguro por defecto.

LONG:
  - peso_actual(symbol) < peso_objetivo(symbol) − pr_rebalance_band_pct
  - Han pasado ≥ pr_check_interval_bars desde el último chequeo

EXIT (CLOSE_LONG parcial simplificado a cierre total, dado el modelo de
posición single-qty del bot):
  - peso_actual(symbol) > peso_objetivo(symbol) + pr_rebalance_band_pct
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


class PortfolioRebalanceStrategy(BaseStrategy):
    strategy_id = "PORTFOLIO_REBALANCE"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._band = settings.pr_rebalance_band_pct
        self._check_interval = max(settings.pr_check_interval_bars, 1)
        self._atr_period = settings.atr_period
        self._atr_multiplier = settings.atr_multiplier
        self._risk_manager = None  # inyectado por StrategyEngine
        self._bar_counters: dict[str, int] = {}

        symbols = settings.trading_symbols
        configured = settings.pr_target_weights
        if configured:
            total = sum(configured.values()) or 1.0
            self._target_weights = {k: v / total for k, v in configured.items()}
        elif symbols:
            equal = 1.0 / len(symbols)
            self._target_weights = {s: equal for s in symbols}
        else:
            self._target_weights = {}

        self.min_candles = self._atr_period + 20

        logger.info(
            "strategy.initialized",
            strategy=self.strategy_id,
            target_weights=self._target_weights,
            band=self._band,
        )

    def set_risk_manager(self, risk_manager) -> None:
        """Inyectado externamente: necesita ver balance y posiciones abiertas."""
        self._risk_manager = risk_manager

    async def generate_signal(
        self,
        symbol: str,
        candles: list[Candle],
        ticker: Optional[Ticker] = None,
        position: Optional[Position] = None,
    ) -> Optional[Signal]:
        if symbol not in self._target_weights or self._risk_manager is None:
            return None

        closed = [c for c in candles if c.is_closed]
        if not self.has_enough_data(closed):
            return None

        # Solo evaluar cada N velas para mantener bajo el turnover
        count = self._bar_counters.get(symbol, 0) + 1
        self._bar_counters[symbol] = count
        if count % self._check_interval != 0:
            return None

        current_price = Decimal(str(closed[-1].close))
        target_weight = self._target_weights[symbol]

        total_balance = self._risk_manager._account_balance
        if total_balance <= 0:
            return None

        current_position = self._risk_manager.get_open_position(symbol, self.strategy_id)
        current_notional = current_position.notional if current_position else Decimal("0")

        # Aproximación: el "total de cartera" es el balance libre + notional
        # de todas las posiciones abiertas gestionadas por el risk manager.
        all_positions_notional = sum(
            (p.notional for p in self._risk_manager.get_all_positions()), Decimal("0")
        )
        portfolio_total = total_balance + all_positions_notional
        if portfolio_total <= 0:
            return None

        current_weight = float(current_notional / portfolio_total)
        deviation = current_weight - target_weight  # negativo = infraponderado

        _, highs, lows, closes, _ = self._to_arrays(closed)
        atr_vals = atr(highs, lows, closes, self._atr_period)
        atr_value = float(atr_vals[-1]) if not np.isnan(atr_vals[-1]) else 0.0
        if atr_value <= 0:
            return None
        atr_dec = Decimal(str(atr_value))

        # ── SOBREPONDERADO: vender exceso (cierre de la posición) ──────────────
        if current_position and current_position.is_open and deviation > self._band:
            logger.info(
                "strategy.rebalance_exit",
                strategy=self.strategy_id,
                symbol=symbol,
                current_weight=f"{current_weight:.4f}",
                target_weight=f"{target_weight:.4f}",
            )
            return Signal(
                strategy_id=self.strategy_id,
                symbol=symbol,
                direction=SignalDirection.CLOSE_LONG,
                entry_price=current_price,
                stop_loss=current_price,
                confidence=0.7,
                metadata={
                    "exit_reason": "overweight",
                    "current_weight": current_weight,
                    "target_weight": target_weight,
                },
            )

        # ── INFRAPONDERADO: comprar para acercar al objetivo ───────────────────
        if deviation < -self._band and (current_position is None or not current_position.is_open):
            stop_loss = current_price - atr_dec * Decimal(str(self._atr_multiplier))

            min_tp_dist = current_price * Decimal(str(
                self._settings.round_trip_fee_pct
                + self._settings.min_net_profit_pct
                + self._settings.slippage_buffer_pct
            ))
            take_profit = current_price + min_tp_dist * Decimal("2")  # margen holgado, no es direccional

            confidence = min(abs(deviation) / (self._band * 3.0), 1.0)
            strength = SignalStrength.MODERATE

            signal = Signal(
                strategy_id=self.strategy_id,
                symbol=symbol,
                direction=SignalDirection.LONG,
                entry_price=current_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                strength=strength,
                confidence=round(max(confidence, 0.2), 4),
                metadata={
                    "current_weight": current_weight,
                    "target_weight": target_weight,
                    "deviation": deviation,
                },
            )
            logger.info(
                "strategy.signal_generated",
                strategy=self.strategy_id,
                symbol=symbol,
                direction="LONG",
                current_weight=f"{current_weight:.4f}",
                target_weight=f"{target_weight:.4f}",
                entry=str(current_price),
                stop=str(stop_loss),
                tp=str(take_profit),
            )
            return signal

        return None
