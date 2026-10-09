"""
engines/strategy_engine.py
Orquestador de estrategias. Escucha eventos del DataEngine y
despacha señales al ExecutionEngine a través del RiskManager.

CORRECCIONES:
- Cooldown por símbolo+dirección para evitar signal spam (misma señal cada 6s)
- Deduplicación: si una señal idéntica ya está en cola, no se encola otra
- Conflicto entre estrategias: si ya hay posición abierta por estrategia X,
  solo esa estrategia (o trailing stop) puede cerrarla — otra estrategia no
  puede emitir CLOSE_LONG sobre una posición que no abrió ella
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

from core.config import ActiveStrategy, Settings
from core.logger import get_logger
from engines.data_engine import DataEngine
from engines.execution_engine import ExecutionEngine
from engines.risk_manager import RiskManager
from models.market_data import Candle, Ticker
from models.signal import Signal, SignalDirection
from strategies.base_strategy import BaseStrategy
from strategies.mean_reversion import MeanReversionStrategy
from strategies.volatility_breakout import VolatilityBreakoutStrategy
from strategies.synthetic_arb import SyntheticArbStrategy
from strategies.smart_routing import SmartRoutingStrategy
from strategies.cross_rate_pairs import CrossRatePairsStrategy
from strategies.portfolio_rebalance import PortfolioRebalanceStrategy
from strategies.cross_currency_arb import CrossCurrencyArbStrategy
from strategies.trend_momentum import TrendMomentumStrategy
from strategies.dip_rebound import DipReboundStrategy
from strategies.triangular_arb import TriangularArbStrategy
from utils.indicators import atr

logger = get_logger(__name__)

# Cooldown en segundos entre señales del mismo símbolo+dirección
_SIGNAL_COOLDOWN_S = 30.0
# Cooldown para señales rechazadas (más largo para no reintentar en bucle)
_REJECTED_COOLDOWN_S = 60.0
# AJUSTADO: antes era una lista de EXCEPCIONES corta (solo DIP_REBOUND)
# sobre un bloqueo que por defecto aplicaba a TODAS las estrategias — pero
# la premisa de MEAN_REVERSION, SYNTHETIC_ARB, CROSS_CURRENCY_ARB,
# TRIANGULAR_ARB, CROSS_RATE_PAIRS y SMART_ROUTING es exactamente la
# misma que la de DIP_REBOUND: comprar la caída esperando que revierta.
# Bloquearlas en tendencia bajista las anulaba casi por completo (en una
# sesión de ~6h, 0 señales para 5 de esas 6 estrategias y solo 2
# posiciones totales en toda la noche). El filtro tiene sentido de verdad
# solo para estrategias que siguen momentum/tendencia — ahí sí "comprar
# mientras cae" es alimentar una posición en contra del movimiento
# dominante. Se invierte la lista: ahora es un bloqueo explícito solo
# para esas, no una excepción corta sobre un bloqueo universal.
_DOWNTREND_BLOCKED_STRATEGIES = frozenset({"TREND_MOMENTUM", "VOLATILITY_BREAKOUT"})


class StrategyEngine:
    """
    Orquestador de estrategias.

    - Registra y gestiona múltiples estrategias.
    - Se subscribe a los eventos del DataEngine.
    - Despacha señales al ExecutionEngine para ejecución.
    - Maneja señales de trailing stop generadas por el RiskManager.
    - Implementa cooldowns y deduplicación para evitar signal spam.
    """

    def __init__(
        self,
        settings: Settings,
        data_engine: DataEngine,
        risk_manager: RiskManager,
        execution_engine: ExecutionEngine,
    ) -> None:
        self._settings = settings
        self._data_engine = data_engine
        self._risk_manager = risk_manager
        self._execution_engine = execution_engine
        self._strategies: dict[str, BaseStrategy] = {}
        self._signal_queue: asyncio.Queue[Signal] = asyncio.Queue(maxsize=100)
        self._running = False
        # Cooldowns: (strategy_id, symbol, direction) -> timestamp del último envío
        self._signal_cooldowns: dict[tuple[str, str, str], float] = {}
        self._trend_watcher = None

    # ── Inicialización ────────────────────────────────────────────────────

    # Mapa único de construcción usado tanto en la carga inicial como en
    # set_strategy_enabled, para no duplicar la lógica de instanciación.
    _STRATEGY_FACTORY: dict[str, type] = {
        "MEAN_REVERSION": MeanReversionStrategy,
        "VOLATILITY_BREAKOUT": VolatilityBreakoutStrategy,
        "SYNTHETIC_ARB": SyntheticArbStrategy,
        "SMART_ROUTING": SmartRoutingStrategy,
        "CROSS_RATE_PAIRS": CrossRatePairsStrategy,
        "PORTFOLIO_REBALANCE": PortfolioRebalanceStrategy,
        "CROSS_CURRENCY_ARB": CrossCurrencyArbStrategy,
        "TREND_MOMENTUM": TrendMomentumStrategy,
        "DIP_REBOUND": DipReboundStrategy,
        "TRIANGULAR_ARB": TriangularArbStrategy,
    }

    def _instantiate_strategy(self, strategy_id: str) -> Optional[BaseStrategy]:
        cls = self._STRATEGY_FACTORY.get(strategy_id)
        if cls is None:
            return None
        instance = cls(self._settings)
        # Inyección de dependencias adicionales para estrategias que
        # necesitan ver más de un símbolo o el estado de riesgo/cartera.
        if hasattr(instance, "set_data_engine"):
            instance.set_data_engine(self._data_engine)
        if hasattr(instance, "set_risk_manager"):
            instance.set_risk_manager(self._risk_manager)
        return instance

    def _load_strategies(self) -> None:
        """Instancia las estrategias activas según la configuración."""
        for strat_enum in self._settings.active_strategies:
            sid = strat_enum.value
            instance = self._instantiate_strategy(sid)
            if instance is not None:
                self._strategies[sid] = instance
                logger.info("strategy_engine.loaded", strategy=sid)
            else:
                logger.warning("strategy_engine.unimplemented_strategy", strategy=sid)

        if not self._strategies:
            raise RuntimeError("No hay estrategias activas configuradas")

    # ── Ciclo de vida ──────────────────────────────────────────────────────

    async def start(self) -> None:
        """
        Inicia el StrategyEngine:
        1. Carga estrategias.
        2. Registra callbacks en el DataEngine.
        3. Inicia el loop de procesamiento de señales.
        """
        self._load_strategies()
        self._running = True

        # Suscribirse a eventos del DataEngine
        self._data_engine.on_candle(self._on_candle)
        self._data_engine.on_ticker(self._on_ticker)

        # Loop de procesamiento de señales en background
        asyncio.create_task(self._signal_processor_loop(), name="signal_processor")

        logger.info(
            "strategy_engine.started",
            strategies=list(self._strategies.keys()),
            symbols=self._data_engine._symbols,
        )

    async def stop(self) -> None:
        self._running = False
        logger.info("strategy_engine.stopped")

    def set_trend_watcher(self, trend_watcher) -> None:
        self._trend_watcher = trend_watcher

    def get_active_strategy_ids(self) -> list[str]:
        return list(self._strategies.keys())

    def _save_active_strategies_to_env(self) -> None:
        """Guarda las estrategias activas en el archivo .env."""
        try:
            active_list = list(self._strategies.keys())
            active_str = ",".join(active_list)
            
            import os, re
            env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
            if os.path.exists(env_path):
                with open(env_path, "r", encoding="utf-8") as f:
                    content = f.read()
                
                if "ACTIVE_STRATEGIES=" in content:
                    content = re.sub(r"ACTIVE_STRATEGIES=.*", f"ACTIVE_STRATEGIES={active_str}", content)
                else:
                    content += f"\nACTIVE_STRATEGIES={active_str}\n"
                    
                with open(env_path, "w", encoding="utf-8") as f:
                    f.write(content)
        except Exception as exc:
            logger.error("strategy_engine.env_save_error", error=str(exc))

    def set_strategy_enabled(self, strategy_id: str, enabled: bool) -> bool:
        sid = strategy_id.upper()
        if enabled:
            if sid in self._strategies:
                return True
            instance = self._instantiate_strategy(sid)
            if instance is None:
                logger.warning("strategy_engine.unknown_strategy", strategy=sid)
                return False
            self._strategies[sid] = instance
            logger.info("strategy_engine.strategy_enabled", strategy=sid)
            self._save_active_strategies_to_env()
            return True
        else:
            if sid not in self._strategies:
                return True
            del self._strategies[sid]
            logger.info("strategy_engine.strategy_disabled", strategy=sid)
            self._save_active_strategies_to_env()
            return True

    # ── Cooldown de señales ───────────────────────────────────────────────

    def _is_on_cooldown(self, strategy_id: str, symbol: str, direction: str) -> bool:
        """True si esta combinación (strategy, symbol, direction) está en cooldown."""
        key = (strategy_id, symbol, direction)
        last = self._signal_cooldowns.get(key, 0.0)
        return (time.monotonic() - last) < _SIGNAL_COOLDOWN_S

    def _set_cooldown(self, strategy_id: str, symbol: str, direction: str,
                      duration: float = _SIGNAL_COOLDOWN_S) -> None:
        """Registra el timestamp actual para el cooldown."""
        key = (strategy_id, symbol, direction)
        self._signal_cooldowns[key] = time.monotonic()
        # Si el cooldown solicitado es más largo, ajustar el timestamp hacia atrás
        # para que la duración real sea la indicada
        if duration > _SIGNAL_COOLDOWN_S:
            self._signal_cooldowns[key] = time.monotonic() - _SIGNAL_COOLDOWN_S + duration

    # ── Callbacks del DataEngine ──────────────────────────────────────────

    async def _on_candle(self, candle: Candle) -> None:
        """
        Llamado por DataEngine cuando llega una vela cerrada.
        Ejecuta todas las estrategias sobre el símbolo de la vela.
        """
        if not candle.is_closed or not self._running:
            return

        symbol = candle.symbol
        candles = self._data_engine.get_candles(symbol)

        if not candles:
            return

        for strategy_id, strategy in self._strategies.items():
            try:
                # Cooldown check: no generar señal si estamos en cooldown
                # (se comprueba para LONG/SHORT antes de llamar a generate_signal)
                ticker = self._data_engine.get_ticker(symbol)
                position = self._risk_manager.get_open_position(symbol, strategy_id)
                signal = await strategy.generate_signal(symbol, candles, ticker, position)
                if signal is not None:
                    # NUEVO: `position` ya está scopeado a esta misma
                    # estrategia (symbol + strategy_id), así que una señal
                    # de cierre generada por `strategy` siempre corresponde
                    # a su propia posición — ya no hace falta comprobar
                    # conflicto con otra estrategia, cada una gestiona la
                    # suya de forma independiente.

                    # Cooldown check
                    if self._is_on_cooldown(signal.strategy_id, symbol, signal.direction.value):
                        continue

                    try:
                        self._signal_queue.put_nowait(signal)
                        self._set_cooldown(signal.strategy_id, symbol, signal.direction.value)
                        logger.info(
                            "strategy_engine.signal_queued",
                            strategy=strategy_id,
                            symbol=symbol,
                            direction=signal.direction.value,
                            entry=str(signal.entry_price),
                            stop=str(signal.stop_loss),
                        )
                    except asyncio.QueueFull:
                        logger.warning(
                            "strategy_engine.signal_queue_full",
                            strategy=strategy_id,
                            symbol=symbol,
                        )
            except Exception as exc:
                logger.error(
                    "strategy_engine.strategy_error",
                    strategy=strategy_id,
                    symbol=symbol,
                    error=str(exc),
                )

    async def _on_ticker(self, ticker: Ticker) -> None:
        """
        Llamado por DataEngine en cada actualización de precio.
        Actualiza precios en el ExecutionEngine y verifica trailing stops.
        """
        if not self._running:
            return

        symbol = ticker.symbol
        self._execution_engine.update_price(symbol, ticker.last_price)

        # Verificar trailing stop para TODAS las posiciones abiertas en
        # este símbolo. NUEVO: ahora puede haber una posición por cada
        # estrategia simultáneamente en la misma moneda, así que hay que
        # comprobar cada una por separado en vez de una sola por símbolo.
        try:
            open_positions = self._risk_manager.get_open_positions_for_symbol(symbol)
            for position in open_positions:
                exit_reason = await self._execution_engine.handle_trailing_stop_check(
                    symbol, position.strategy_id, ticker.last_price
                )
                if not exit_reason:
                    continue
                from models.signal import SignalDirection
                from models.order import PositionSide
                direction = (
                    SignalDirection.CLOSE_LONG
                    if position.side == PositionSide.LONG
                    else SignalDirection.CLOSE_SHORT
                )
                exit_signal = Signal(
                    strategy_id=position.strategy_id,
                    symbol=symbol,
                    direction=direction,
                    entry_price=ticker.last_price,
                    stop_loss=ticker.last_price,
                    confidence=1.0,
                    metadata={"exit_reason": exit_reason},
                )
                try:
                    self._signal_queue.put_nowait(exit_signal)
                except asyncio.QueueFull:
                    pass
        except Exception as exc:
            logger.error(
                "strategy_engine.trailing_stop_error",
                symbol=symbol,
                error=str(exc),
            )

    # ── Procesador de señales ─────────────────────────────────────────────

    async def _signal_processor_loop(self) -> None:
        """
        Consume señales de la queue y las procesa secuencialmente
        para evitar condiciones de carrera.
        """
        while self._running:
            try:
                signal = await asyncio.wait_for(
                    self._signal_queue.get(),
                    timeout=1.0,
                )
                await self._process_signal(signal)
                self._signal_queue.task_done()
            except asyncio.TimeoutError:
                continue
            except Exception as exc:
                logger.error("strategy_engine.signal_processor_error", error=str(exc))

    async def _process_signal(self, signal: Signal) -> None:
        """Procesa una señal: calcula ATR y ejecuta a través del ExecutionEngine."""
        symbol = signal.symbol

        # Filtro de tendencia: solo para estrategias que siguen momentum/
        # tendencia (TREND_MOMENTUM, VOLATILITY_BREAKOUT) — para esas,
        # comprar mientras el símbolo ya está cayendo es ir contra la
        # propia premisa de la estrategia. Las de reversión a la media/
        # arbitraje estadístico (MEAN_REVERSION, SYNTHETIC_ARB,
        # CROSS_CURRENCY_ARB, TRIANGULAR_ARB, CROSS_RATE_PAIRS,
        # SMART_ROUTING, DIP_REBOUND) tienen la premisa contraria a
        # propósito: comprar la caída esperando que revierta. Bloquearlas
        # aquí las anulaba casi por completo.
        if (
            self._settings.block_long_in_downtrend
            and signal.is_entry
            and signal.direction == SignalDirection.LONG
            and signal.strategy_id in _DOWNTREND_BLOCKED_STRATEGIES
            and self._trend_watcher is not None
        ):
            trend = self._trend_watcher.get_direction(symbol)
            if trend == "down":
                logger.info(
                    "strategy_engine.signal_rejected.downtrend",
                    strategy=signal.strategy_id,
                    symbol=symbol,
                )
                self._set_cooldown(
                    signal.strategy_id, symbol, signal.direction.value,
                    _REJECTED_COOLDOWN_S,
                )
                return

        candles = self._data_engine.get_candles(symbol)

        # Calcular ATR actual para position sizing
        atr_value = 0.0
        if len(candles) >= self._settings.atr_period:
            import numpy as np
            highs = np.array([float(c.high) for c in candles], dtype=np.float64)
            lows = np.array([float(c.low) for c in candles], dtype=np.float64)
            closes = np.array([float(c.close) for c in candles], dtype=np.float64)
            atr_vals = atr(highs, lows, closes, self._settings.atr_period)
            if not np.isnan(atr_vals[-1]):
                atr_value = float(atr_vals[-1])

        try:
            order = await self._execution_engine.process_signal(signal, atr_value)
            if order:
                logger.info(
                    "strategy_engine.order_executed",
                    symbol=signal.symbol,
                    direction=signal.direction.value,
                    strategy=signal.strategy_id,
                    order_id=order.order_id,
                    qty=str(order.qty),
                )
            elif signal.is_entry:
                # Señal de entrada rechazada: aplicar cooldown más largo
                self._set_cooldown(
                    signal.strategy_id, symbol, signal.direction.value,
                    _REJECTED_COOLDOWN_S,
                )
        except Exception as exc:
            logger.error(
                "strategy_engine.execution_error",
                signal_id=signal.signal_id,
                symbol=signal.symbol,
                error=str(exc),
            )
