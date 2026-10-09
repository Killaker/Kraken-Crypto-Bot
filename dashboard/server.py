"""
dashboard/server.py
Servidor de UI web para monitorear el bot en tiempo real.

Expone:
- GET  /                → sirve el frontend estático (HTML/JS/CSS)
- WS   /ws               → push de snapshots de estado cada ~1s
- GET  /api/state        → snapshot puntual (fallback sin WS)
- GET  /api/logs         → últimas N líneas de log en memoria

Diseño: el dashboard NUNCA escribe en el bot (solo lectura), salvo el
endpoint explícito /api/kill-switch/reset, que es una acción administrativa
consciente. No coloca ni cancela órdenes.
"""
from __future__ import annotations

import asyncio
import json
from collections import deque
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from core.logger import get_logger, register_dashboard_sink

logger = get_logger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

# Buffer circular de logs en memoria para mostrarlos en la UI
LOG_BUFFER: deque[dict] = deque(maxlen=500)


def push_log(level: str, event: str, **kwargs: Any) -> None:
    """Agrega una entrada al buffer de logs que consume la UI."""
    LOG_BUFFER.append(
        {
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "level": level,
            "event": event,
            **{k: _jsonable(v) for k, v in kwargs.items()},
        }
    )


def _jsonable(value: Any) -> Any:
    """Convierte Decimal/enum/otros a tipos serializables por json."""
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "value") and isinstance(getattr(value, "value"), (str, int, float)):
        return value.value
    return value


class DashboardServer:
    """
    Encapsula la app FastAPI y mantiene una referencia débil al TradingBot
    para poder leer su estado sin acoplar el bot a la UI.
    """

    def __init__(self, bot: Any, host: str = "127.0.0.1", port: int = 8001) -> None:
        """
        `bot` es la instancia de TradingBot (main.TradingBot). Se tipa como Any
        para evitar un import circular entre main.py y dashboard/server.py.
        """
        self._bot = bot
        self._host = host
        self._port = port
        self._app = FastAPI(title="Trading Bot Dashboard")
        self._active_ws: set[WebSocket] = set()
        self._broadcast_task: Optional[asyncio.Task] = None
        self._server: Optional[Any] = None
        self._setup_routes()
        register_dashboard_sink(push_log)

    # ── Construcción del snapshot de estado ────────────────────────────────

    def build_snapshot(self) -> dict:
        """Arma el estado actual del bot leyendo sus engines internos."""
        bot = self._bot
        settings = bot._settings
        risk_manager = bot._risk_manager
        data_engine = bot._data_engine
        strategy_engine = bot._strategy_engine

        positions: list[dict] = []
        daily_dd = 0.0
        kill_switch = False
        entries_paused = False
        balance = "0"

        if risk_manager is not None:
            daily_dd = risk_manager.daily_drawdown
            kill_switch = risk_manager.is_kill_switch_active
            entries_paused = risk_manager.new_entries_paused
            balance = str(risk_manager._account_balance)
            # NUEVO: coste real de cerrar la posición AHORA (round-trip de
            # 2 comisiones taker + slippage), igual que el resto del bot
            # usa para calcular TPs. pnl_pct es el movimiento de precio
            # BRUTO — una posición podía verse "en verde" en el dashboard
            # con, por ejemplo, +0.3%, cuando en realidad cerrarla en ese
            # momento daría pérdida neta (0.3% - ~0.73% de coste = -0.43%).
            # Esto explica buena parte de "veo verde durante la sesión
            # pero al cerrar tengo pérdidas": el marcador no reflejaba el
            # coste de la propia operación de cierre.
            total_cost_pct = (
                risk_manager._settings.round_trip_fee_pct
                + risk_manager._settings.slippage_buffer_pct
            ) * 100
            btc_eur = data_engine.get_btc_eur_rate() if data_engine is not None else None
            for p in risk_manager.get_all_positions():
                # Backfill: si la posición se abrió antes de tener BTC/EUR
                # (o este código), se congela con la primera tasa disponible
                # y a partir de ahí ya no se toca. Nunca se recalcula en
                # ticks posteriores — eso era lo que hacía "bailar" la
                # columna Entrada del dashboard.
                if (
                    (p.entry_eur is None or p.entry_eur <= 0)
                    and btc_eur
                    and btc_eur > 0
                    and p.entry_price > 0
                ):
                    p.entry_eur = p.entry_price * btc_eur
                    p.entry_btc_eur_rate = btc_eur
                pnl_net_pct = p.pnl_pct * 100 - total_cost_pct

                # NUEVO: rentabilidad calculada en EUR, no en BTC. El par
                # se cotiza y liquida en BTC, así que pnl_pct (arriba) es
                # el que de verdad dispara stop/TP/trailing — pero si el
                # BTC se mueve mucho en EUR entre la apertura y ahora, el
                # resultado en BTC puede no coincidir con lo que el
                # usuario percibe en EUR (p.ej. la moneda sube en EUR
                # aunque haya caído frente al BTC, o al revés). pnl_eur_pct
                # usa entry_eur (congelado con la tasa de apertura) contra
                # el valor actual con la tasa EUR en vivo, así que refleja
                # el cambio de valor real en EUR, incluyendo el propio
                # movimiento del BTC/EUR.
                pnl_eur_pct = None
                if p.entry_eur and p.entry_eur > 0 and btc_eur and btc_eur > 0:
                    current_eur = p.current_price * btc_eur
                    pnl_eur_pct = float((current_eur - p.entry_eur) / p.entry_eur) * 100

                positions.append(
                    {
                        "symbol": p.symbol,
                        "side": p.side.value,
                        "qty": str(p.qty),
                        "entry_price": str(p.entry_price),
                        "entry_eur": str(p.entry_eur) if p.entry_eur is not None else None,
                        "entry_btc_eur_rate": (
                            str(p.entry_btc_eur_rate)
                            if p.entry_btc_eur_rate is not None
                            else None
                        ),
                        "current_price": str(p.current_price),
                        "pnl_pct": round(p.pnl_pct * 100, 3),
                        "pnl_net_pct": round(pnl_net_pct, 3),
                        "pnl_eur_pct": round(pnl_eur_pct, 3) if pnl_eur_pct is not None else None,
                        "pnl_eur_net_pct": (
                            round(pnl_eur_pct - total_cost_pct, 3)
                            if pnl_eur_pct is not None
                            else None
                        ),
                        "stop_loss": str(p.stop_loss) if p.stop_loss is not None else None,
                        "trailing_stop_price": (
                            str(p.trailing_stop_price)
                            if p.trailing_stop_price is not None
                            else None
                        ),
                        "strategy_id": p.strategy_id,
                    }
                )

        tickers: dict[str, dict] = {}
        if data_engine is not None:
            for symbol in settings.trading_symbols:
                t = data_engine.get_ticker(symbol)
                if t:
                    tickers[symbol] = {
                        "last_price": str(t.last_price),
                        "bid": str(t.bid),
                        "ask": str(t.ask),
                        "price_change_pct_24h": t.price_change_pct_24h,
                        "funding_rate": str(t.funding_rate) if t.funding_rate else None,
                    }

        strategies = list(strategy_engine._strategies.keys()) if strategy_engine else []
        available_strategies = [
            "MEAN_REVERSION",
            "VOLATILITY_BREAKOUT",
            "SYNTHETIC_ARB",
            "SMART_ROUTING",
            "CROSS_RATE_PAIRS",
            "PORTFOLIO_REBALANCE",
            "CROSS_CURRENCY_ARB",
            "TREND_MOMENTUM",
            "DIP_REBOUND",
            "TRIANGULAR_ARB",
        ]

        return {
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "bot": {
                "running": bot._running,
                "mode": settings.bot_mode.value,
                "exchange": "kraken",
                "convert_base_asset": settings.convert_base_asset,
                "testnet": settings.use_testnet,
                "symbols": settings.trading_symbols,
                "timeframe": settings.timeframe,
                "active_strategies": strategies,
                "available_strategies": available_strategies,
                "disabled_symbols": risk_manager.disabled_symbols if risk_manager else [],
            },
            "risk": {
                "balance": balance,
                "daily_drawdown": round(daily_dd * 100, 3),
                "max_daily_drawdown": round(settings.max_daily_drawdown * 100, 3),
                "kill_switch_active": kill_switch,
                "max_open_positions": settings.max_open_positions,
                "risk_per_trade": round(settings.risk_per_trade * 100, 3),
                "new_entries_paused": entries_paused,
            },
            "positions": positions,
            "tickers": tickers,
            "trend_alerts": (
                bot._trend_watcher.get_recent_alerts(20) if bot._trend_watcher else []
            ),
        }

    # ── Rutas HTTP / WS ──────────────────────────────────────────────────

    def _setup_routes(self) -> None:
        app = self._app

        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

        @app.get("/")
        async def index() -> FileResponse:
            return FileResponse(str(STATIC_DIR / "index.html"))

        @app.get("/api/state")
        async def api_state() -> JSONResponse:
            return JSONResponse(self.build_snapshot())

        @app.get("/api/logs")
        async def api_logs(limit: int = 200) -> JSONResponse:
            items = list(LOG_BUFFER)[-limit:]
            return JSONResponse(items)

        @app.get("/api/strategies")
        async def api_strategies() -> JSONResponse:
            se = self._bot._strategy_engine
            available = [
                "MEAN_REVERSION",
                "VOLATILITY_BREAKOUT",
                "SYNTHETIC_ARB",
                "SMART_ROUTING",
                "CROSS_RATE_PAIRS",
                "PORTFOLIO_REBALANCE",
                "CROSS_CURRENCY_ARB",
                "TREND_MOMENTUM",
                "DIP_REBOUND",
                "TRIANGULAR_ARB",
            ]
            active = se.get_active_strategy_ids() if se else []
            return JSONResponse({"available": available, "active": active})

        @app.post("/api/strategies/{strategy_id}/enable")
        async def enable_strategy(strategy_id: str) -> JSONResponse:
            se = self._bot._strategy_engine
            if se is None:
                return JSONResponse({"success": False, "error": "engine not ready"}, status_code=503)
            ok = se.set_strategy_enabled(strategy_id, True)
            await self._broadcast_once()
            return JSONResponse({"success": ok, "active": se.get_active_strategy_ids()})

        @app.post("/api/strategies/{strategy_id}/disable")
        async def disable_strategy(strategy_id: str) -> JSONResponse:
            se = self._bot._strategy_engine
            if se is None:
                return JSONResponse({"success": False, "error": "engine not ready"}, status_code=503)
            ok = se.set_strategy_enabled(strategy_id, False)
            await self._broadcast_once()
            return JSONResponse({"success": ok, "active": se.get_active_strategy_ids()})

        @app.post("/api/kill-switch/reset")
        async def reset_kill_switch() -> JSONResponse:
            """
            Acción administrativa consciente: desactiva el kill-switch
            manualmente. Es la única excepción a la regla de "el dashboard
            nunca escribe en el bot" documentada en el docstring del módulo
            — necesaria porque, tras el fix del bug que bloqueaba también
            los cierres de posición, el kill-switch ya no atrapa posiciones,
            pero sigue impidiendo ABRIR nuevas hasta que se resetee o pase
            el rollover diario. Antes de este endpoint la única forma de
            recuperarse era reiniciar el proceso del bot entero.
            """
            rm = self._bot._risk_manager
            if rm is None:
                return JSONResponse({"success": False, "error": "risk manager not ready"}, status_code=503)
            if not rm.is_kill_switch_active:
                return JSONResponse({"success": True, "already_inactive": True})
            await rm.reset_kill_switch()
            logger.warning("dashboard.kill_switch_reset_requested")
            await self._broadcast_once()
            return JSONResponse({"success": True, "kill_switch_active": rm.is_kill_switch_active})

        @app.post("/api/entries/pause")
        async def pause_new_entries() -> JSONResponse:
            """
            Segunda excepción consciente a "el dashboard nunca escribe en
            el bot": pausa manualmente la apertura de posiciones NUEVAS,
            sin tocar las ya abiertas (que se siguen gestionando con
            normalidad: TP, stop, trailing, cierres de convergencia). Útil
            para dejar que las posiciones actuales se resuelvan solas
            mientras se revisa o ajusta la configuración del bot.
            """
            rm = self._bot._risk_manager
            if rm is None:
                return JSONResponse({"success": False, "error": "risk manager not ready"}, status_code=503)
            rm.pause_new_entries()
            logger.warning("dashboard.new_entries_paused")
            await self._broadcast_once()
            return JSONResponse({"success": True, "new_entries_paused": rm.new_entries_paused})

        @app.post("/api/entries/resume")
        async def resume_new_entries() -> JSONResponse:
            rm = self._bot._risk_manager
            if rm is None:
                return JSONResponse({"success": False, "error": "risk manager not ready"}, status_code=503)
            rm.resume_new_entries()
            logger.warning("dashboard.new_entries_resumed")
            await self._broadcast_once()
            return JSONResponse({"success": True, "new_entries_paused": rm.new_entries_paused})

        @app.post("/api/symbols/{symbol}/disable")
        async def disable_symbol(symbol: str) -> JSONResponse:
            rm = self._bot._risk_manager
            if rm is None:
                return JSONResponse({"success": False, "error": "risk manager not ready"}, status_code=503)
            rm.disable_symbol(symbol)
            logger.warning("dashboard.symbol_disabled", symbol=symbol.upper())
            await self._broadcast_once()
            return JSONResponse({"success": True, "disabled_symbols": rm.disabled_symbols})

        @app.post("/api/symbols/{symbol}/enable")
        async def enable_symbol(symbol: str) -> JSONResponse:
            rm = self._bot._risk_manager
            if rm is None:
                return JSONResponse({"success": False, "error": "risk manager not ready"}, status_code=503)
            rm.enable_symbol(symbol)
            logger.warning("dashboard.symbol_enabled", symbol=symbol.upper())
            await self._broadcast_once()
            return JSONResponse({"success": True, "disabled_symbols": rm.disabled_symbols})

        @app.post("/api/positions/{symbol}/close")
        async def close_position_manual(symbol: str, strategy: str | None = None) -> JSONResponse:
            ee = self._bot._execution_engine
            if ee is None:
                return JSONResponse({"success": False, "error": "execution engine not ready"}, status_code=503)
            try:
                order = await ee.close_position_manual(symbol.upper(), strategy_id=strategy)
            except Exception as exc:
                logger.error("dashboard.manual_close_error", symbol=symbol.upper(), error=str(exc))
                return JSONResponse({"success": False, "error": str(exc)}, status_code=500)
            if order is None:
                return JSONResponse(
                    {"success": False, "error": "no open position, cierre ambiguo (indica ?strategy=) o cierre falló"},
                    status_code=404,
                )
            logger.warning("dashboard.manual_close", symbol=symbol.upper(), strategy=strategy)
            await self._broadcast_once()
            return JSONResponse({"success": True, "symbol": symbol.upper()})

        @app.websocket("/ws")
        async def ws_endpoint(websocket: WebSocket) -> None:
            await websocket.accept()
            self._active_ws.add(websocket)
            logger.info("dashboard.ws_connected", clients=len(self._active_ws))
            try:
                while True:
                    # Mantenemos la conexión viva; no esperamos mensajes del cliente
                    await websocket.receive_text()
            except WebSocketDisconnect:
                pass
            finally:
                self._active_ws.discard(websocket)
                logger.info("dashboard.ws_disconnected", clients=len(self._active_ws))

    # ── Broadcast periódico ─────────────────────────────────────────────

    async def _broadcast_loop(self, interval: float = 1.0) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                await self._broadcast_once()
            except Exception as exc:
                # CORREGIDO: un fallo puntual al construir/serializar el
                # snapshot (p.ej. estado transitorio justo tras un cierre
                # manual) mataba esta tarea de forma silenciosa (una
                # excepción no capturada dentro de un `while True` sin
                # try/except detiene el loop para siempre). El dashboard
                # dejaba de recibir actualizaciones para TODOS los
                # clientes conectados: la tabla de posiciones quedaba
                # congelada con la posición ya cerrada todavía visible, y
                # cualquier otro botón (kill-switch, pausar entradas,
                # otras estrategias) parecía "no hacer nada" porque su
                # resultado nunca llegaba a reflejarse en la UI. Ahora un
                # fallo puntual se loguea y el broadcast continúa en el
                # siguiente ciclo.
                logger.error("dashboard.broadcast_error", error=str(exc))

    async def _broadcast_once(self) -> None:
        if not self._active_ws:
            return
        snapshot = self.build_snapshot()
        snapshot["logs"] = list(LOG_BUFFER)[-20:]
        payload = json.dumps(snapshot, default=str)
        dead: list[WebSocket] = []
        # CORREGIDO: `await ws.send_text(...)` cede el control del event
        # loop; si mientras tanto un cliente nuevo se conecta o se
        # desconecta, muta `self._active_ws` (un set) en plena iteración
        # — "Set changed size during iteration", visto en el log, que
        # mataba el broadcast para todo el mundo. Se itera sobre una copia
        # (list(...)) para que una conexión/desconexión concurrente no
        # rompa este bucle.
        for ws in list(self._active_ws):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._active_ws.discard(ws)

    # ── Ciclo de vida ─────────────────────────────────────────────────

    async def start(self) -> None:
        import uvicorn

        config = uvicorn.Config(
            self._app,
            host=self._host,
            port=self._port,
            log_level="warning",
            loop="asyncio",
        )
        self._server = uvicorn.Server(config)
        self._broadcast_task = asyncio.create_task(self._broadcast_loop(), name="dashboard_broadcast")
        logger.info("dashboard.starting", host=self._host, port=self._port)
        await self._server.serve()

    async def stop(self) -> None:
        if self._broadcast_task:
            self._broadcast_task.cancel()
        if self._server:
            self._server.should_exit = True
