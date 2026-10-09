"""
engines/trend_watcher.py
Detecta cambios de tendencia por símbolo (cruce de EMA rápida/lenta sobre
las últimas horas de velas) para avisar al usuario en el dashboard, y
también se usa como filtro de entradas LONG para las estrategias que
siguen momentum/tendencia (ver strategy_engine._DOWNTREND_BLOCKED_STRATEGIES).

Reutiliza la infraestructura ya existente: el histórico de velas que
DataEngine ya mantiene (mismo candle_callback que usan las estrategias),
y la misma función `ema` de utils/indicators.py que usan el resto de
estrategias — ningún fetch ni fuente de datos nueva.

AJUSTADO (fiabilidad): con EMA 6/18 y un solo cruce como disparador, el
detector reaccionaba a ruido normal de precio (la EMA rápida cruzando la
lenta por una vela y volviendo enseguida) — y como get_direction() ahora
también se usa como GATE de trading (no solo para el popup informativo),
una lectura ruidosa no solo generaba un aviso de más, sino que podía
bloquear entradas legítimas. Dos filtros adicionales antes de declarar un
cambio de tendencia:
1) separación mínima entre EMAs (trend_watch_min_separation_pct) — un
   cruce "al ras" no cuenta, tiene que notarse de verdad.
2) confirmación en N velas seguidas en la misma dirección
   (trend_watch_confirm_candles) — un pico de una sola vela no vale.
Periodos más anchos (9/26, los clásicos de MACD) en vez de 6/18.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import numpy as np

from core.config import Settings
from core.logger import get_logger
from models.market_data import Candle
from utils.indicators import ema

logger = get_logger(__name__)


class TrendWatcher:
    def __init__(self, settings: Settings) -> None:
        self._fast_period = settings.trend_watch_ema_fast
        self._slow_period = settings.trend_watch_ema_slow
        self._lookback = settings.trend_watch_lookback_candles
        self._min_separation_pct = settings.trend_watch_min_separation_pct
        self._confirm_candles = settings.trend_watch_confirm_candles

        self._last_direction: dict[str, str] = {}       # confirmada (estable)
        self._pending_direction: dict[str, str] = {}     # candidata, aún sin confirmar
        self._pending_count: dict[str, int] = {}
        self._alerts: list[dict] = []
        self._max_alerts = 50
        self._next_id = 1
        self._data_engine = None  # inyectado en set_data_engine

        logger.info(
            "trend_watcher.initialized",
            ema_fast=self._fast_period,
            ema_slow=self._slow_period,
            lookback_candles=self._lookback,
            min_separation_pct=self._min_separation_pct,
            confirm_candles=self._confirm_candles,
        )

    def set_data_engine(self, data_engine) -> None:
        self._data_engine = data_engine

    def _raw_direction(self, symbol: str) -> Optional[str]:
        """
        Dirección de la vela actual, o None si la separación entre EMAs
        es demasiado pequeña para fiarse (zona de cruce/ruido).
        """
        if self._data_engine is None:
            return None
        candles = [c for c in self._data_engine.get_candles(symbol) if c.is_closed]
        n = min(len(candles), self._lookback)
        if n < self._slow_period + 2:
            return None
        closes = np.array([float(c.close) for c in candles[-n:]], dtype=np.float64)
        ema_fast = ema(closes, self._fast_period)
        ema_slow = ema(closes, self._slow_period)
        if np.isnan(ema_fast[-1]) or np.isnan(ema_slow[-1]) or ema_slow[-1] == 0:
            return None
        separation = abs(ema_fast[-1] - ema_slow[-1]) / abs(ema_slow[-1])
        if separation < self._min_separation_pct:
            return None  # demasiado al ras del cruce para fiarse
        return "up" if ema_fast[-1] > ema_slow[-1] else "down"

    def _update_confirmed_direction(self, symbol: str) -> None:
        """
        Actualiza el estado de confirmación para `symbol`: exige
        `confirm_candles` lecturas seguidas en la misma dirección (cada
        una ya filtrada por separación mínima) antes de dar por buena esa
        dirección como la "confirmada" — la que se expone en
        get_direction() y dispara el popup si cambia.
        """
        raw = self._raw_direction(symbol)
        if raw is None:
            # Zona de ruido/cruce: no cuenta para confirmar NI para
            # reiniciar el conteo pendiente — simplemente se ignora esta
            # vela para la máquina de confirmación.
            return

        if self._pending_direction.get(symbol) == raw:
            self._pending_count[symbol] = self._pending_count.get(symbol, 0) + 1
        else:
            self._pending_direction[symbol] = raw
            self._pending_count[symbol] = 1

        if self._pending_count[symbol] < self._confirm_candles:
            return

        previous = self._last_direction.get(symbol)
        self._last_direction[symbol] = raw
        if previous is not None and previous != raw:
            alert = {
                "id": self._next_id,
                "symbol": symbol,
                "direction": raw,  # "up" | "down"
                "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            }
            self._next_id += 1
            self._alerts.append(alert)
            if len(self._alerts) > self._max_alerts:
                self._alerts = self._alerts[-self._max_alerts:]
            logger.info("trend_watcher.trend_change", symbol=symbol, direction=raw)

    async def on_candle(self, candle: Candle) -> None:
        if not candle.is_closed:
            return
        self._update_confirmed_direction(candle.symbol)

    def get_direction(self, symbol: str) -> Optional[str]:
        """
        Tendencia CONFIRMADA del símbolo: "up", "down" o None si aún no
        hay datos suficientes o no se ha confirmado ninguna dirección
        todavía. Se usa tanto para el popup del dashboard como para el
        filtro de entradas LONG de las estrategias de momentum/tendencia.
        """
        cached = self._last_direction.get(symbol)
        if cached is not None:
            return cached
        self._update_confirmed_direction(symbol)
        return self._last_direction.get(symbol)

    def get_alerts_since(self, last_seen_id: int) -> list[dict]:
        """Alertas con id > last_seen_id, para que el dashboard solo reciba las nuevas."""
        return [a for a in self._alerts if a["id"] > last_seen_id]

    def get_recent_alerts(self, limit: int = 20) -> list[dict]:
        return self._alerts[-limit:]
