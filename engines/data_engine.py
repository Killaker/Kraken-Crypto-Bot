"""
engines/data_engine.py
Motor de datos: WebSocket + REST para Kraken SPOT.
Operativa directa BTC↔TARGET: resuelve pares TARGET/BTC (o su alias XBT)
sin necesidad de un par puente separado.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from collections import defaultdict, deque
from decimal import Decimal
from typing import Any, Callable, Coroutine, Optional

import ccxt.async_support as ccxt

from core.config import Settings
from core.logger import get_logger
from models.market_data import Candle, Ticker

logger = get_logger(__name__)

# Máximo de velas históricas a mantener por símbolo
MAX_CANDLES = 500

# Alias Kraken: BTC se representa como XBT en muchos pares
_KRAKEN_ALIASES: dict[str, list[str]] = {
    "BTC": ["BTC", "XBT"],
    "XBT": ["XBT", "BTC"],
}



_TF_MAP: dict[str, timedelta] = {
    "1m": timedelta(minutes=1),   "3m": timedelta(minutes=3),
    "5m": timedelta(minutes=5),   "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30), "1h": timedelta(hours=1),
    "2h": timedelta(hours=2),     "4h": timedelta(hours=4),
    "6h": timedelta(hours=6),     "8h": timedelta(hours=8),
    "12h": timedelta(hours=12),   "1d": timedelta(days=1),
    "3d": timedelta(days=3),      "1w": timedelta(weeks=1),
}

def _kraken_variants(base: str, quote: str) -> list[str]:
    """Genera variantes de par con alias XBT/BTC."""
    bases = _KRAKEN_ALIASES.get(base.upper(), [base.upper()])
    quotes = _KRAKEN_ALIASES.get(quote.upper(), [quote.upper()])
    return [f"{b}/{q}" for b in bases for q in quotes]


def _resolve_pair(
    symbol: str,
    bridge: str,
    markets: dict,
) -> tuple[str, bool] | None:
    """
    Busca el par ccxt para TARGET/BRIDGE en el catálogo de markets.

    Estrategia:
    1. Busca por nombre de par (con variantes XBT/BTC).
    2. Si no encuentra por nombre, itera todos los markets y compara
       base/quote canónicos que ya expone ccxt, evitando depender de
       la nomenclatura exacta de Kraken.

    Retorna (pair_str, inverted):
      inverted=False  → el par cotiza como TARGET/BRIDGE  (precio directo)
      inverted=True   → el par cotiza como BRIDGE/TARGET  (precio = 1/raw)
    """
    sym_up = symbol.upper()
    bridge_up = bridge.upper()
    bridge_aliases = set(_KRAKEN_ALIASES.get(bridge_up, [bridge_up]))

    # 1. Búsqueda por nombre — directo: TARGET/BRIDGE (y alias XBT)
    for variant in _kraken_variants(sym_up, bridge_up):
        if variant in markets:
            return (variant, False)

    # 2. Búsqueda por nombre — invertido: BRIDGE/TARGET (y alias XBT)
    for variant in _kraken_variants(bridge_up, sym_up):
        if variant in markets:
            return (variant, True)

    # 3. Búsqueda semántica: recorrer markets comparando base/quote canónicos
    #    (ccxt normaliza XBT→BTC en los campos base/quote de muchos exchanges)
    sym_aliases = set(_KRAKEN_ALIASES.get(sym_up, [sym_up])) | {sym_up}
    for pair_str, mkt in markets.items():
        mkt_base = (mkt.get("base") or "").upper()
        mkt_quote = (mkt.get("quote") or "").upper()
        if mkt_base in sym_aliases and mkt_quote in bridge_aliases:
            return (pair_str, False)
        if mkt_base in bridge_aliases and mkt_quote in sym_aliases:
            return (pair_str, True)

    return None


class DataEngine:
    """
    Gestiona la conexión a Kraken y provee datos de mercado
    (velas históricas + ticker en tiempo real) al resto del sistema.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._symbols: list[str] = list(settings.trading_symbols)

        self._exchange: Optional[ccxt.kraken] = None
        self._candles: dict[str, deque[Candle]] = defaultdict(
            lambda: deque(maxlen=MAX_CANDLES)
        )
        self._tickers: dict[str, Ticker] = {}

        # Callbacks registrados externamente
        self._candle_callbacks: list[Callable[[Candle], Coroutine]] = []
        self._ticker_callbacks: list[Callable[[Ticker], Coroutine]] = []

        # Último open_time ya notificado por símbolo (evita disparar el
        # callback de vela cerrada dos veces para la misma vela cuando
        # tanto el refresco REST como _poll_tickers podrían cerrarla).
        self._last_notified_open_time: dict[str, datetime] = {}
        # NUEVO: volumen de la vela EN FORMACIÓN se calcula como el
        # volumen_24h del ticker (que SÍ llega en cada poll) menos su
        # valor en el instante en que esa vela se abrió — el polling REST
        # no da trade-by-trade, pero volume_24h es una cuenta corriente
        # que el propio Kraken mantiene, así que la diferencia entre dos
        # lecturas sí es volumen real negociado en ese tramo. Antes toda
        # vela abierta en tiempo real (no traída por el refresco
        # histórico) nacía con volume=Decimal("0") y lo arrastraba
        # SIN CAMBIOS durante toda su vida, incluso al cerrarse — el 94%
        # de los chequeos de "low_volume" en el log mostraban ratio=0.00
        # exacto, bloqueando MEAN_REVERSION casi siempre, no por falta de
        # liquidez real sino porque el volumen nunca se medía.
        self._candle_open_volume_24h: dict[str, Decimal] = {}

        # En operativa directa BTC↔TARGET no existe par puente separado;
        # se mantienen estos atributos como compatibilidad con código que
        # pueda consultarlos, pero siempre devuelven None/False.
        self._bridge_pair: Optional[str] = None
        self._bridge_pair_inverted: bool = False

        self._running = False
        self._ws_task: Optional[asyncio.Task] = None

        # BTC/EUR en vivo (Kraken). Sirve para congelar el precio de
        # entrada de cada posición en EUR en el instante de apertura.
        self._btc_eur_pair: Optional[str] = None
        self._btc_eur_rate: Optional[Decimal] = None

    # ── Inicialización de exchange ─────────────────────────────────────

    def _build_exchange(self) -> ccxt.kraken:
        params: dict[str, Any] = {
            "enableRateLimit": True,
            "timeout": self._settings.rest_timeout * 1000,
        }
        if self._settings.kraken_api_key:
            params["apiKey"] = self._settings.kraken_api_key
            params["secret"] = self._settings.kraken_api_secret
        return ccxt.kraken(params)

    # ── Resolución de pares ────────────────────────────────────────────

    async def _resolve_target_pairs(self, markets: dict) -> dict[str, tuple[str, bool]]:
        """
        Para cada símbolo objetivo, resuelve el par ccxt correcto en Kraken
        (TARGET/BTC o su alias XBT). Usa búsqueda semántica por base/quote.
        """
        bridge = self._settings.bridge_asset
        resolved: dict[str, tuple[str, bool]] = {}

        for symbol in self._symbols:
            result = _resolve_pair(symbol, bridge, markets)
            if result is not None:
                pair_str, inverted = result
                resolved[symbol] = (pair_str, inverted)
                logger.info(
                    "data_engine.resolve_target_pair.found",
                    symbol=symbol,
                    pair=pair_str,
                    inverted=inverted,
                )
            else:
                logger.warning(
                    "data_engine.resolve_target_pair.not_found",
                    symbol=symbol,
                    bridge=bridge,
                )

        return resolved

    def _resolve_btc_eur_pair(self, markets: dict) -> Optional[str]:
        """Par ccxt para BTC/EUR en Kraken (BTC o alias XBT)."""
        for variant in _kraken_variants("BTC", "EUR"):
            if variant in markets:
                return variant
        eur_aliases = {"EUR"}
        btc_aliases = set(_KRAKEN_ALIASES.get("BTC", ["BTC"])) | {"BTC", "XBT"}
        for pair_str, mkt in markets.items():
            if (mkt.get("base") or "").upper() in btc_aliases and (
                mkt.get("quote") or ""
            ).upper() in eur_aliases:
                return pair_str
        return None

    # ── Ciclo de vida ─────────────────────────────────────────────────

    async def start(self) -> None:
        self._exchange = self._build_exchange()
        markets = await self._exchange.load_markets()

        # No hay par puente que resolver en operativa directa BTC↔TARGET.
        self._target_pairs = await self._resolve_target_pairs(markets)
        self._btc_eur_pair = self._resolve_btc_eur_pair(markets)
        if self._btc_eur_pair:
            logger.info("data_engine.btc_eur_pair.found", pair=self._btc_eur_pair)
        else:
            logger.warning("data_engine.btc_eur_pair.not_found")

        # Cargar velas históricas REST para todos los símbolos resueltos
        await self._fetch_historical_candles()

        self._running = True
        self._ws_task = asyncio.create_task(
            self._ws_loop(), name="data_engine_ws"
        )
        logger.info("data_engine.started", symbols=self._symbols)

    async def stop(self) -> None:
        self._running = False
        if self._ws_task:
            self._ws_task.cancel()
        if self._exchange:
            await self._exchange.close()
        logger.info("data_engine.stopped")

    # ── Carga histórica ───────────────────────────────────────────────

    async def _fetch_historical_candles(self) -> None:
        tf = self._settings.timeframe
        for symbol, (pair, inverted) in self._target_pairs.items():
            try:
                ohlcv = await self._exchange.fetch_ohlcv(pair, tf, limit=MAX_CANDLES)
                tf_delta = _TF_MAP.get(tf, timedelta(minutes=5))
                now = datetime.now(tz=timezone.utc)
                n_rows = len(ohlcv)
                for idx, row in enumerate(ohlcv):
                    open_time  = datetime.fromtimestamp(row[0] / 1000, tz=timezone.utc)
                    close_time = open_time + tf_delta - timedelta(milliseconds=1)
                    o  = Decimal(str(row[1] or 0))
                    h  = Decimal(str(row[2] or 0))
                    lo = Decimal(str(row[3] or 0))
                    c  = Decimal(str(row[4] or 0))
                    v  = Decimal(str(row[5] or 0))
                    if inverted and o > 0:
                        o, h, lo, c = (Decimal("1")/o, Decimal("1")/lo,
                                       Decimal("1")/h,  Decimal("1")/c)
                    # CORREGIDO: se marcaba is_closed=True SIEMPRE, incluida
                    # la última fila devuelta por el exchange — que en
                    # Kraken suele ser la vela EN CURSO, aún sin cerrar de
                    # verdad (su close_time todavía no ha llegado). Este
                    # refresco se repite cada ~5 minutos
                    # (_ws_stream.candle_refresh_error/candle_refresh_counter)
                    # y, al pisar la última vela con is_closed=True de forma
                    # incondicional, dejaba a _poll_tickers permanentemente
                    # "atascado": su máquina de estados solo reabre una vela
                    # nueva al detectar la transición no-cerrada→cerrada, y
                    # esa transición nunca ocurre si la vela YA llega
                    # marcada como cerrada desde aquí. Resultado observado:
                    # cero velas cerradas, cero señales, durante más de una
                    # hora seguida. Ahora solo la última fila se evalúa
                    # contra el reloj real; si su close_time aún no ha
                    # pasado, se guarda como en formación (is_closed=False)
                    # para que _poll_tickers pueda seguir actualizándola y,
                    # cuando corresponda, cerrarla y notificar UNA vez.
                    is_last_row = idx == n_rows - 1
                    is_closed = not (is_last_row and now < close_time)
                    candle = Candle(
                        symbol=symbol,
                        timeframe=tf,
                        open_time=open_time,
                        close_time=close_time,
                        open=o, high=h, low=lo, close=c,
                        volume=v,
                        quote_volume=Decimal("0"),
                        trades=0,
                        is_closed=is_closed,
                    )
                    self._candles[symbol].append(candle)
                    if is_last_row and not is_closed:
                        # El refresco histórico REEMPLAZA el deque entero;
                        # cualquier línea base de volumen en vivo que
                        # tuviéramos para la vela anterior ya no
                        # corresponde a esta — se limpia para que el
                        # próximo tick la reestablezca desde cero (ver
                        # rama `open_baseline is None` en _poll_tickers).
                        self._candle_open_volume_24h.pop(symbol, None)

                    # Red de seguridad: si incluso la vela más reciente ya
                    # llega cerrada desde el exchange (no todos devuelven
                    # la vela en curso), _poll_tickers nunca vería la
                    # transición no-cerrada→cerrada para disparar el
                    # callback — así que se dispara aquí directamente,
                    # evitando duplicados con `_last_notified_open_time`.
                    if is_last_row and is_closed:
                        last_notified = self._last_notified_open_time.get(symbol)
                        if last_notified is None or open_time > last_notified:
                            self._last_notified_open_time[symbol] = open_time
                            for cb in self._candle_callbacks:
                                asyncio.create_task(cb(candle))
                logger.info(

                    "data_engine.historical_loaded",
                    symbol=symbol,
                    pair=pair,
                    count=len(ohlcv),
                )
            except Exception as exc:
                logger.error(
                    "data_engine.historical_error",
                    symbol=symbol,
                    pair=pair,
                    error=str(exc),
                )

    # ── WebSocket loop ────────────────────────────────────────────────

    async def _ws_loop(self) -> None:
        reconnects = 0
        while self._running and reconnects <= self._settings.ws_max_reconnects:
            try:
                await self._ws_stream()
                reconnects = 0
            except asyncio.CancelledError:
                break
            except Exception as exc:
                reconnects += 1
                logger.warning(
                    "data_engine.ws_reconnecting",
                    attempt=reconnects,
                    error=str(exc),
                )
                await asyncio.sleep(self._settings.ws_reconnect_delay)

    async def _ws_stream(self) -> None:
        """
        Polling REST de tickers + refresco periódico de velas históricas.
        """
        candle_refresh_counter = 0
        # CORREGIDO: en sesiones anteriores, un fallo puntual de Kraken
        # (rate-limit o caída momentánea) se registraba y se reintentaba
        # a los 2s sin más — en un log se vieron 53 fallos seguidos en 9
        # minutos, machacando un endpoint ya con problemas, con TODAS las
        # posiciones sin precio actualizado (stops/TP ciegos) durante todo
        # ese corte. Ahora cada fallo consecutivo aumenta la espera
        # (2s→4s→8s...hasta 30s), y se resetea en cuanto una petición
        # vuelve a tener éxito.
        consecutive_failures = 0
        while self._running:
            ok = await self._poll_tickers()
            consecutive_failures = 0 if ok else consecutive_failures + 1
            candle_refresh_counter += 1
            # Refrescar velas cada 60 ciclos (~5 min) para mantener el histórico actualizado
            if candle_refresh_counter >= 150:
                candle_refresh_counter = 0
                try:
                    await self._fetch_historical_candles()
                except Exception as exc:
                    logger.warning("data_engine.candle_refresh_error", error=str(exc))
            delay = min(2 * (2 ** consecutive_failures), 30) if consecutive_failures else 2
            await asyncio.sleep(delay)

    async def _poll_tickers(self) -> bool:
        all_pairs = [pair for pair, _ in self._target_pairs.values()]
        if self._btc_eur_pair:
            all_pairs.append(self._btc_eur_pair)
        try:
            all_raw = await self._exchange.fetch_tickers(all_pairs)
            ok = True
        except Exception as exc:
            logger.warning("data_engine.fetch_tickers_error", error=str(exc))
            all_raw = {}
            ok = False

        if self._btc_eur_pair:
            raw_btc_eur = all_raw.get(self._btc_eur_pair)
            if raw_btc_eur is not None:
                last_btc_eur = raw_btc_eur.get("last") or raw_btc_eur.get("close") or 0
                try:
                    rate = Decimal(str(last_btc_eur))
                    if rate > 0:
                        self._btc_eur_rate = rate
                except Exception:
                    pass

        for symbol, (pair, inverted) in self._target_pairs.items():
            try:
                raw = all_raw.get(pair)
                if raw is None:
                    continue

                raw_last = raw["last"] or raw.get("close") or 0
                raw_bid  = raw["bid"]  or 0
                raw_ask  = raw["ask"]  or 0

                last = Decimal(str(raw_last))
                bid  = Decimal(str(raw_bid))
                ask  = Decimal(str(raw_ask))

                # inverted=True → el par en Kraken es BRIDGE/TARGET (p.ej. XBT/SOL)
                # El precio que devuelve ccxt es cuántos TARGET compra 1 BRIDGE.
                # Necesitamos cuántos BRIDGE cuesta 1 TARGET → invertir.
                if inverted and last > 0:
                    last = Decimal("1") / last
                    bid, ask = (
                        (Decimal("1") / ask) if ask else Decimal(0),
                        (Decimal("1") / bid) if bid else Decimal(0),
                    )

                logger.debug(
                    "data_engine.ticker_raw",
                    symbol=symbol,
                    pair=pair,
                    inverted=inverted,
                    raw_last=str(raw_last),
                    final_last=str(last),
                )

                ts_raw = raw.get("timestamp")
                ts_dt = (
                    datetime.fromtimestamp(ts_raw / 1000, tz=timezone.utc)
                    if ts_raw
                    else datetime.now(tz=timezone.utc)
                )
                vol_24h = Decimal(str(
                    raw.get("baseVolume") or raw.get("quoteVolume") or 0
                ))
                pct = float(raw.get("percentage") or 0)
                # Si el par está invertido el % de cambio también se invierte de signo
                if inverted:
                    pct = -pct

                ticker = Ticker(
                    symbol=symbol,
                    timestamp=ts_dt,
                    last_price=last,
                    bid=bid,
                    ask=ask,
                    volume_24h=vol_24h,
                    price_change_pct_24h=pct,
                    funding_rate=None,
                )
                self._tickers[symbol] = ticker

                for cb in self._ticker_callbacks:
                    asyncio.create_task(cb(ticker))

                # Actualizar última vela.
                #
                # CORREGIDO (1ª vuelta): antes se marcaba is_closed=True en
                # CADA poll (cada 5s) aunque la vela siguiera en formación,
                # disparando generate_signal() sobre datos incompletos.
                #
                # CORREGIDO (2ª vuelta): el fix anterior, al cruzar el
                # límite de la ventana, marcaba is_closed=True pero
                # SEGUÍA REESCRIBIENDO la misma vela (mismo open_time/
                # close_time) en cada poll siguiente — como "now" ya había
                # superado ese close_time, is_closed quedaba en True PARA
                # SIEMPRE y el callback de vela (generate_signal en las 7
                # estrategias) se disparaba en CADA poll de 5s durante
                # todo el resto de la ventana de 5 minutos, hasta el
                # siguiente refresco REST (~cada 5 min). Esto es lo que
                # explica el patrón del log: posiciones abiertas y
                # cerradas con ~75s de diferencia (12 polls × 5s = 60s,
                # el intervalo de PORTFOLIO_REBALANCE) con pérdida neta
                # igual a la comisión de ida y vuelta — el bot compraba y
                # vendía casi al instante sin movimiento real de precio.
                # Ahora, al cruzar el límite, la vela se cierra
                # DEFINITIVAMENTE (dispara el callback una única vez) y
                # se abre una vela NUEVA para el siguiente período, que
                # empieza en formación (is_closed=False) — el callback ya
                # no puede volver a dispararse sobre la misma vela.
                sym_candles = self._candles.get(symbol)
                if sym_candles:
                    prev = sym_candles[-1]
                    now = datetime.now(tz=timezone.utc)

                    open_baseline = self._candle_open_volume_24h.get(symbol)
                    if open_baseline is None:
                        # Primera vez que vemos esta vela en formación: no
                        # hay base todavía, se fija ahora (volumen de este
                        # primer tick se reporta como 0, se corrige en el
                        # siguiente poll — más fiable que arriesgar un
                        # delta negativo por falta de baseline).
                        open_baseline = vol_24h
                        self._candle_open_volume_24h[symbol] = open_baseline
                    live_volume = vol_24h - open_baseline
                    if live_volume < 0:
                        # vol_24h se resetea a medianoche (ventana móvil de
                        # Kraken) — un delta negativo solo puede ser eso,
                        # nunca volumen real. Se toma como 0 en vez de
                        # arrastrar un número negativo sin sentido.
                        live_volume = Decimal("0")

                    if now < prev.close_time:
                        # Sigue en formación: solo actualizar OHLC en vivo.
                        updated = Candle(
                            symbol=symbol,
                            timeframe=prev.timeframe,
                            open_time=prev.open_time,
                            close_time=prev.close_time,
                            open=prev.open,
                            high=max(prev.high, last),
                            low=min(prev.low, last),
                            close=last,
                            volume=live_volume,
                            quote_volume=prev.quote_volume,
                            trades=prev.trades,
                            is_closed=False,
                        )
                        sym_candles[-1] = updated
                    elif not prev.is_closed:
                        # La ventana ha terminado de verdad: cerrar esta
                        # vela una única vez y disparar las estrategias.
                        closed_candle = Candle(
                            symbol=symbol,
                            timeframe=prev.timeframe,
                            open_time=prev.open_time,
                            close_time=prev.close_time,
                            open=prev.open,
                            high=max(prev.high, last),
                            low=min(prev.low, last),
                            close=last,
                            volume=live_volume,
                            quote_volume=prev.quote_volume,
                            trades=prev.trades,
                            is_closed=True,
                        )
                        sym_candles[-1] = closed_candle
                        self._last_notified_open_time[symbol] = closed_candle.open_time
                        for cb in self._candle_callbacks:
                            asyncio.create_task(cb(closed_candle))

                        # Abrir la vela del siguiente período, en formación,
                        # para que los ticks posteriores no reabran ni
                        # vuelvan a disparar la que acaba de cerrarse.
                        period = prev.close_time - prev.open_time
                        new_open_time = prev.close_time
                        new_close_time = new_open_time + period
                        # Nueva línea base para medir el volumen de ESTA
                        # vela que empieza ahora — ver comentario junto a
                        # self._candle_open_volume_24h en __init__.
                        self._candle_open_volume_24h[symbol] = vol_24h
                        sym_candles.append(Candle(
                            symbol=symbol,
                            timeframe=prev.timeframe,
                            open_time=new_open_time,
                            close_time=new_close_time,
                            open=last,
                            high=last,
                            low=last,
                            close=last,
                            volume=Decimal("0"),
                            quote_volume=Decimal("0"),
                            trades=0,
                            is_closed=False,
                        ))
                    # else: prev ya está cerrada y "now" ya pasó su
                    # close_time — no hacer nada hasta que el próximo
                    # refresco REST (_fetch_historical_candles) traiga la
                    # vela real siguiente; evita reabrir/recerrar en bucle.

            except Exception as exc:
                logger.warning(
                    "data_engine.ticker_poll_error",
                    symbol=symbol,
                    pair=pair,
                    error=str(exc),
                )
        return ok

    # ── API pública ───────────────────────────────────────────────────

    def get_candles(self, symbol: str) -> list[Candle]:
        return list(self._candles[symbol])

    def get_ticker(self, symbol: str) -> Optional[Ticker]:
        return self._tickers.get(symbol)

    def get_btc_eur_rate(self) -> Optional[Decimal]:
        """BTC/EUR en vivo, o None si aún no hay tick."""
        return self._btc_eur_rate

    def get_bridge_pair(self) -> Optional[str]:
        """Mantenido por compatibilidad. En operativa directa siempre retorna None."""
        return self._bridge_pair

    def is_bridge_pair_inverted(self) -> bool:
        """Mantenido por compatibilidad. En operativa directa siempre retorna False."""
        return self._bridge_pair_inverted

    def get_target_pair(self, symbol: str) -> Optional[tuple[str, bool]]:
        return getattr(self, "_target_pairs", {}).get(symbol)

    def on_candle(self, callback: Callable[[Candle], Coroutine]) -> None:
        self._candle_callbacks.append(callback)

    def on_ticker(self, callback: Callable[[Ticker], Coroutine]) -> None:
        self._ticker_callbacks.append(callback)
