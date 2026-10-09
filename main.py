"""
main.py
Entry point del bot de trading crypto.
Gestiona el ciclo de vida completo con señales OS y health check.
"""
from __future__ import annotations

import asyncio
import signal
import sys
from datetime import datetime, timezone
from typing import Optional

from core.config import get_settings
from core.exceptions import KillSwitchTriggered
from core.logger import configure_logging, get_logger
from engines.data_engine import DataEngine
from engines.execution_engine import ExecutionEngine
from engines.risk_manager import RiskManager
from engines.strategy_engine import StrategyEngine
from engines.trend_watcher import TrendWatcher

# Logger se configura después de leer settings
logger: Optional[object] = None


class TradingBot:
    """
    Clase principal del bot. Ensambla y coordina todos los engines.

    Ciclo de vida:
    ┌─────────┐    ┌─────────────┐    ┌────────────────┐    ┌───────────────┐
    │DataEngine│───▶│StrategyEngine│───▶│  RiskManager   │───▶│ExecutionEngine│
    │(WS/REST) │    │  (Signals)   │    │(Validate/Size) │    │(Orders/Paper) │
    └─────────┘    └─────────────┘    └────────────────┘    └───────────────┘
    """

    HEALTH_CHECK_INTERVAL = 300  # segundos entre health checks (5 min)

    def __init__(self) -> None:
        self._settings = get_settings()
        self._data_engine: Optional[DataEngine] = None
        self._risk_manager: Optional[RiskManager] = None
        self._execution_engine: Optional[ExecutionEngine] = None
        self._strategy_engine: Optional[StrategyEngine] = None
        self._trend_watcher: Optional[TrendWatcher] = None
        self._running = False
        self._shutdown_event = asyncio.Event()
        self._dashboard: Optional[object] = None
        self._dashboard_task: Optional[asyncio.Task] = None

    async def initialize(self) -> None:
        """Inicializa todos los engines en orden de dependencia."""
        logger.info(
            "bot.initializing",
            mode=self._settings.bot_mode.value,
            market="SPOT",
            symbols=self._settings.trading_symbols,
            strategies=[s.value for s in self._settings.active_strategies],
        )

        # 1. DataEngine
        self._data_engine = DataEngine(self._settings)

        # 2. RiskManager (no requiere inicialización async todavía)
        self._risk_manager = RiskManager(self._settings)

        # 3. ExecutionEngine (se conecta al exchange e inicializa RiskManager)
        self._execution_engine = ExecutionEngine(self._settings, self._risk_manager)
        self._execution_engine.set_data_engine(self._data_engine)
        balance = await self._execution_engine.initialize()

        # Si PAPER mode, inicializar RiskManager con filtros mock
        if not self._settings.is_live:
            mock_filters = self._execution_engine._mock_symbol_filters()
            await self._risk_manager.initialize(balance, mock_filters)

        # 4. StrategyEngine (depende de los anteriores)
        self._strategy_engine = StrategyEngine(
            settings=self._settings,
            data_engine=self._data_engine,
            risk_manager=self._risk_manager,
            execution_engine=self._execution_engine,
        )

        # 5. TrendWatcher — solo informativo (popup del dashboard), no
        # participa en el trading ni depende de las estrategias activas.
        self._trend_watcher = TrendWatcher(self._settings)
        self._trend_watcher.set_data_engine(self._data_engine)
        self._strategy_engine.set_trend_watcher(self._trend_watcher)

        logger.info("bot.initialized", initial_balance=str(balance))

    async def start(self) -> None:
        """Inicia todos los engines."""
        # Arrancar DataEngine (carga histórica + WS)
        await self._data_engine.start()

        # Arrancar StrategyEngine (suscripciones + signal loop)
        await self._strategy_engine.start()

        # Suscribir el TrendWatcher a las velas cerradas (mismo evento que
        # ya consumen las estrategias, sin tocar su flujo).
        self._data_engine.on_candle(self._trend_watcher.on_candle)

        self._running = True
        logger.info("bot.started")

        # Arrancar el dashboard web (solo lectura de estado, no bloqueante)
        if self._settings.dashboard_enabled:
            try:
                from dashboard.server import DashboardServer
            except ModuleNotFoundError as _e:
                logger.error(
                    "bot.dashboard_import_failed",
                    error=str(_e),
                    hint="Instala dependencias: pip install fastapi uvicorn websockets",
                )
                self._settings.__dict__["dashboard_enabled"] = False
                return

            self._dashboard = DashboardServer(
                self,
                host=self._settings.dashboard_host,
                port=self._settings.dashboard_port,
            )
            self._dashboard_task = asyncio.create_task(
                self._dashboard.start(), name="dashboard_server"
            )
            logger.info(
                "bot.dashboard_started",
                url=f"http://{self._settings.dashboard_host}:{self._settings.dashboard_port}",
            )

    async def run(self) -> None:
        """Loop principal. Bloquea hasta recibir señal de shutdown."""
        await self.initialize()
        await self.start()

        # Inicia el health check en paralelo
        health_task = asyncio.create_task(
            self._health_check_loop(),
            name="health_check",
        )

        try:
            # Esperar la señal de parada
            await self._shutdown_event.wait()
        finally:
            health_task.cancel()
            await self.shutdown()

    async def shutdown(self) -> None:
        """Apagado ordenado: cierra posiciones, desconecta WS, limpia recursos."""
        if not self._running:
            return
        self._running = False

        logger.warning("bot.shutting_down")

        # 0. Detener el dashboard web
        if self._dashboard:
            await self._dashboard.stop()
        if self._dashboard_task and not self._dashboard_task.done():
            self._dashboard_task.cancel()
            try:
                await self._dashboard_task
            except (asyncio.CancelledError, Exception):
                pass

        # 1. Parar StrategyEngine (deja de generar señales)
        if self._strategy_engine:
            await self._strategy_engine.stop()

        # 2. Parar DataEngine (cierra WebSocket)
        if self._data_engine:
            await self._data_engine.stop()

        # 3. Cerrar ExecutionEngine (libera conexión exchange)
        if self._execution_engine:
            # En LIVE: reportar posiciones abiertas al cierre
            if self._risk_manager:
                open_positions = self._risk_manager.get_all_positions()
                if open_positions:
                    logger.warning(
                        "bot.open_positions_on_shutdown",
                        count=len(open_positions),
                        symbols=[p.symbol for p in open_positions],
                    )
            await self._execution_engine.close()

        logger.info("bot.shutdown_complete")

    def request_shutdown(self) -> None:
        """Solicita el shutdown de forma thread-safe (usado por manejadores de señal)."""
        logger.warning("bot.shutdown_requested")
        self._shutdown_event.set()

    # ── Health Check ──────────────────────────────────────────────────────

    async def _health_check_loop(self) -> None:
        """
        Loop de monitoreo que se ejecuta cada N segundos.
        Registra el estado del bot y verifica el drawdown.
        """
        while self._running:
            await asyncio.sleep(self.HEALTH_CHECK_INTERVAL)
            try:
                await self._run_health_check()
            except KillSwitchTriggered as exc:
                logger.critical("bot.kill_switch_health_check", reason=str(exc))
                self.request_shutdown()
            except Exception as exc:
                logger.error("bot.health_check_error", error=str(exc))

    async def _run_health_check(self) -> None:
        """Recopila y loguea métricas de salud del sistema."""
        if not self._risk_manager:
            return

        open_positions = self._risk_manager.get_all_positions()
        daily_dd = self._risk_manager.daily_drawdown

        logger.info(
            "bot.health_check",
            timestamp=datetime.now(tz=timezone.utc).isoformat(),
            open_positions=len(open_positions),
            daily_drawdown=f"{daily_dd:.2%}",
            kill_switch_active=self._risk_manager.is_kill_switch_active,
            positions=[
                {
                    "symbol": p.symbol,
                    "side": p.side.value,
                    "qty": str(p.qty),
                    "entry": str(p.entry_price),
                    "pnl_pct": f"{p.pnl_pct:.3%}",
                }
                for p in open_positions
            ],
        )


# ─────────────────────────────────────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────────────────────────────────────

def _setup_signal_handlers(bot: TradingBot, loop: asyncio.AbstractEventLoop) -> None:
    """Registra manejadores de señal OS para apagado ordenado."""
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(
                sig,
                lambda: loop.call_soon_threadsafe(bot.request_shutdown),
            )
        except NotImplementedError:
            # Windows no soporta add_signal_handler para todos los signals
            signal.signal(sig, lambda s, f: bot.request_shutdown())


async def _main() -> None:
    settings = get_settings()

    # Configurar logging primero
    configure_logging(
        level=settings.log_level,
        use_json=(settings.log_format.value == "json"),
    )

    global logger
    from core.logger import get_logger as _get_logger
    logger_instance = _get_logger("main")

    # Reemplazar el logger global
    import core.logger as _core_logger

    logger_instance.info(
        "bot.starting",
        mode=settings.bot_mode.value,
        market="SPOT",
        testnet=settings.use_testnet,
        symbols=settings.trading_symbols,
    )

    bot = TradingBot()
    loop = asyncio.get_event_loop()
    _setup_signal_handlers(bot, loop)

    try:
        await bot.run()
    except KillSwitchTriggered as exc:
        logger_instance.critical("main.kill_switch", reason=str(exc))
        sys.exit(1)
    except KeyboardInterrupt:
        logger_instance.info("main.keyboard_interrupt")
    except Exception as exc:
        logger_instance.critical("main.unhandled_exception", error=str(exc), exc_info=True)
        sys.exit(1)
    finally:
        logger_instance.info("main.exit")


def main() -> None:
    """Punto de entrada principal."""
    # Configurar logging básico antes de cargar settings
    configure_logging(level="INFO", use_json=False)
    global logger
    from core.logger import get_logger as _get_logger
    logger = _get_logger("main")

    # Windows + Python ≥3.8: asyncio usa ProactorEventLoop por defecto,
    # que causa "Event loop is closed" al cerrar conexiones SSL/aiohttp.
    # SelectorEventLoop evita ese error en Windows.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    asyncio.run(_main())


if __name__ == "__main__":
    main()
