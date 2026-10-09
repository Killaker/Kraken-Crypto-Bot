"""
core/config.py
Configuración centralizada usando Pydantic Settings.
Carga automática desde .env con validación de tipos y valores.

Operativa: conversión directa BTC ↔ TARGET en Kraken SPOT.
  Entrada: BTC → TARGET  (1 orden de mercado, par TARGET/BTC)
  Salida:  TARGET → BTC  (1 orden de mercado, par TARGET/BTC)
No hay futuros, leverage ni fiat. BTC actúa simultáneamente como
activo base en reposo y como activo cotización de los pares target.
"""
from __future__ import annotations

import sys
from enum import Enum
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _env_file_candidates() -> list[str]:
    """
    Resuelve dónde buscar el .env, en capas — las que existan se cargan y
    las posteriores pisan los valores de las anteriores; python-dotenv no
    falla si un archivo de la lista no existe, así que es seguro listarlas
    todas sin comprobar antes si están:

    1. El .env empaquetado DENTRO del propio .exe (snapshot congelado en
       el momento de compilar con PyInstaller — ver kraken_bot_directo.spec,
       `datas += [(".env", ".")]`). Así el bot arranca con una
       configuración completa sin depender de ningún archivo externo junto
       al .exe.
    2. Un .env real junto al .exe (o junto a la raíz del proyecto en modo
       desarrollo, `python main.py`). Si el usuario lo coloca ahí, sus
       valores pisan a los del punto 1 sin necesidad de recompilar — útil
       para cambiar credenciales o parámetros sin tocar el código.

    CORREGIDO: antes `env_file=".env"` era una ruta relativa al directorio
    de trabajo actual (CWD) en el momento de lanzar el .exe — pydantic-
    settings/python-dotenv nunca miraban dentro de `sys._MEIPASS` (donde
    PyInstaller extrae los `datas` empaquetados en modo --onefile), así
    que el .env embebido por el .spec nunca se usaba en la práctica: el
    bot SOLO arrancaba si había un .env real al lado del .exe (y además
    solo si se lanzaba desde esa misma carpeta como CWD).
    """
    candidates: list[str] = []
    bundle_dir = getattr(sys, "_MEIPASS", None)
    if bundle_dir:
        # Ejecutándose como .exe compilado (PyInstaller).
        candidates.append(str(Path(bundle_dir) / ".env"))
        exe_dir = Path(sys.executable).resolve().parent
    else:
        # Ejecutándose como script Python normal (modo desarrollo).
        exe_dir = Path(__file__).resolve().parent.parent
    candidates.append(str(exe_dir / ".env"))
    return candidates


class BotMode(str, Enum):
    PAPER = "PAPER"
    LIVE = "LIVE"


class LogFormat(str, Enum):
    JSON = "json"
    CONSOLE = "console"


class ActiveStrategy(str, Enum):
    MEAN_REVERSION = "MEAN_REVERSION"
    VOLATILITY_BREAKOUT = "VOLATILITY_BREAKOUT"
    # DELTA_NEUTRAL_ARB eliminada: dependía de funding rate de futuros,
    # que no existe en Kraken Spot.
    SYNTHETIC_ARB = "SYNTHETIC_ARB"
    SMART_ROUTING = "SMART_ROUTING"
    CROSS_RATE_PAIRS = "CROSS_RATE_PAIRS"
    PORTFOLIO_REBALANCE = "PORTFOLIO_REBALANCE"
    CROSS_CURRENCY_ARB = "CROSS_CURRENCY_ARB"
    # Única estrategia de momentum/tendencia: todas las demás asumen
    # reversión a la media o arbitraje de spread, así que sufren por igual
    # cuando el mercado tiende de forma sostenida en vez de revertir.
    TREND_MOMENTUM = "TREND_MOMENTUM"
    # Estrategia I — Dip Rebound: entra en caídas puntuales dentro de una
    # tendencia bajista confirmada, apostando al rebote técnico de corto
    # plazo (mean-reversion agresiva de baja frecuencia).
    DIP_REBOUND = "DIP_REBOUND"
    # Estrategia J — Triangular Arb: generaliza CROSS_CURRENCY_ARB a TODOS
    # los posibles triángulos BRIDGE→TARGET→REF→BRIDGE, no solo uno fijo.
    TRIANGULAR_ARB = "TRIANGULAR_ARB"


ALL_STRATEGY_IDS: list[str] = [s.value for s in ActiveStrategy]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_env_file_candidates(),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        env_ignore_empty=True,  # evita json.loads("") en vars vacías del .env
    )

    # ── Modo de operación ──────────────────────────────────────────────────────
    bot_mode: BotMode = BotMode.PAPER
    # Ya no hay market_type: siempre SPOT en Kraken.
    use_testnet: bool = False  # Kraken no tiene testnet público estable; ver nota en README.

    # ── Credenciales Kraken ───────────────────────────────────────────────────
    kraken_api_key: str = ""
    kraken_api_secret: str = ""

    # ── Activo base de conversión ────────────────────────────────────────────
    # Activo cripto que el bot mantiene "en reposo" y que actúa como activo
    # cotización de todos los pares target (p.ej. ETH/BTC, SOL/BTC).
    # Operativa directa: 1 orden por dirección, sin salto intermedio.
    convert_base_asset: str = "BTC"

    # ── Activo cotización de los pares target ─────────────────────────────────
    # En operativa directa bridge_asset == convert_base_asset (BTC).
    # Se mantiene el campo para compatibilidad con estrategias que construyen
    # pares como f"{symbol}/{bridge_asset}". No genera ningún salto extra.
    bridge_asset: str = "BTC"

    # ── Pares y timeframe ─────────────────────────────────────────────────────
    # trading_symbols son los activos cripto objetivo (sin el base),
    # p.ej. ["ETH", "SOL"] → pares directos ETH/BTC, SOL/BTC.
    # Si se deja VACÍO y auto_discover_symbols=true, el bot descubre en
    # tiempo de arranque los pares más líquidos TARGET/BTC en Kraken.
    trading_symbols: list[str] = Field(
        default=["SOL", "XRP", "ADA", "DOT", "LTC", "LINK", "ATOM", "BCH", "ETH", "DOGE", "ALGO"]
    )
    timeframe: str = "5m"

    # ── Descubrimiento automático de símbolos ─────────────────────────────────
    auto_discover_symbols: bool = True
    # Límite de pares simultáneos a vigilar (más pares = más carga de WS/CPU).
    max_auto_symbols: Annotated[int, Field(ge=1, le=50)] = 15
    # Si es True, el descubrimiento automático solo considera criptos de la
    # whitelist `major_symbols` (mercados grandes, más líquidos y con spreads
    # más ajustados).
    restrict_to_major_symbols: bool = False
    major_symbols: list[str] = Field(
        default=[
            "SOL", "XRP", "ADA", "DOT", "LTC", "LINK", "ATOM", "BCH", "ETH"
        ]
    )

    # ── Comisiones y rentabilidad neta ────────────────────────────────────────
    # Comisión taker de Kraken por operación (orden de mercado), como fracción.
    # 0.0026 = 0.26% (tier base). Ajusta si tu volumen te da un tier mejor.
    kraken_taker_fee_pct: Annotated[float, Field(ge=0.0, le=0.01)] = 0.0026
    # Margen mínimo de beneficio NETO exigido para cerrar una posición.
    # Conversión directa implica 2 legs (1 entrada + 1 salida) →
    # break-even ≈ round_trip_fee_pct (0.52% con tier base de Kraken).
    # Se mantiene el colchón de 0.6% neto para absorber slippage real.
    min_net_profit_pct: Annotated[float, Field(ge=0.0, le=0.05)] = 0.006

    # Buffer de slippage estimado POR ROUND-TRIP (2 market orders).
    # Reducido de 0.35% (que asumía 4 legs) a 0.20%: dos órdenes de
    # mercado en pares TARGET/BTC tienen menor deslizamiento que cuatro.
    slippage_buffer_pct: Annotated[float, Field(ge=0.0, le=0.02)] = 0.002

    # ── Gestión de riesgo ─────────────────────────────────────────────────────
    risk_per_trade: Annotated[float, Field(gt=0.0, le=0.05)] = 0.01

    # Tope duro de notional por posición como fracción del balance total.
    # Evita que un stop_loss muy cercano al entry (precios de escala
    # minúscula, ATR pequeño) dispare un qty cuyo notional (qty*entry)
    # supere el capital real disponible — en LIVE el exchange lo
    # rechazaría, pero en PAPER se ejecutaba igual, liquidando fees sobre
    # un notional varias veces mayor que el balance.
    max_position_notional_pct: Annotated[float, Field(gt=0.0, le=1.0)] = 0.3
    # Tope de capital desplegado en posiciones ABIERTAS a la vez, como
    # fracción del equity mark-to-market. Sin esto, TRIANGULAR_ARB podía
    # abrir 8-9 LONGs correlacionados en el mismo segundo (visto en log:
    # todos vs ADA) y dejar el wallet en polvo, pagando fees 8 veces por
    # la MISMA apuesta. 0.55 deja colchón para no operar all-in.
    max_deployed_notional_pct: Annotated[float, Field(gt=0.0, le=1.0)] = 0.55
    # Máximo de posiciones abiertas (o en vuelo) POR estrategia. Restaurado
    # a propósito: sin él, un único z-score de un REF (ADA) disparaba un
    # LONG en cada target. 2 permite un par de triángulos distintos, no
    # un spray de todo el universo.
    # AJUSTADO: con 2, TREND_MOMENTUM se topó con el límite en plena
    # noche y quedó bloqueada el resto de la sesión (9 señales generadas,
    # solo 2 convertidas en posición). Subido a 4 para dar más margen sin
    # quitar el límite por completo — sigue habiendo un tope por
    # estrategia, max_open_positions sigue aplicando como tope global.
    max_positions_per_strategy: Annotated[int, Field(ge=1, le=20)] = 4
    # Spread bid/ask máximo para aceptar una entrada a mercado. Pares
    # TARGET/BTC ilíquidos (ALGO, DOGE) pueden tener 0.5-1.5% de spread
    # que se come el edge estadístico antes de empezar.
    max_entry_spread_pct: Annotated[float, Field(ge=0.0, le=0.05)] = 0.004
    # Bloquear LONGs de mean-reversion/arb cuando el TrendWatcher marca
    # el símbolo en tendencia bajista. DIP_REBOUND es la excepción (su
    # premisa ES comprar la caída).
    block_long_in_downtrend: bool = True
    # Notional mínimo como fracción del EQUITY (no del cash residual).
    # Si se mide sobre el cash que queda, las últimas migajas del spray
    # (0.0005 BTC) pasaban el filtro 5% y solo servían para pagar fees.
    min_viable_notional_pct: Annotated[float, Field(gt=0.0, le=0.5)] = 0.08
    # Detector de cambio de tendencia (TrendWatcher). También se usa
    # como filtro de entradas LONG (block_long_in_downtrend): no es
    # puramente informativo — un LONG de mean-reversion/arb sobre un
    # símbolo en tendencia bajista es cazar un cuchillo.
    # AJUSTADO: periodos más anchos (9/26, clásicos de MACD) + dos filtros
    # de fiabilidad nuevos — ver engines/trend_watcher.py. get_direction()
    # ahora también actúa como gate de trading (block_long_in_downtrend),
    # así que una lectura ruidosa ya no es solo un aviso de más, puede
    # bloquear una entrada legítima.
    trend_watch_ema_fast: int = 9
    trend_watch_ema_slow: int = 26
    trend_watch_lookback_candles: int = 60
    trend_watch_min_separation_pct: float = 0.0015  # 0.15% entre EMAs
    trend_watch_confirm_candles: int = 3

    # Escalado del tamaño de posición por confianza de la señal (ver
    # RiskManager.calculate_order_qty). confidence=0.0 → se arriesga
    # confidence_size_min_mult × risk_per_trade; confidence=1.0 → se
    # arriesga confidence_size_max_mult × risk_per_trade; escala lineal
    # entre medias. max no puede superar max_position_notional_pct, que
    # sigue aplicándose después como tope duro.
    confidence_size_min_mult: Annotated[float, Field(ge=0.1, le=1.0)] = 0.5
    confidence_size_max_mult: Annotated[float, Field(ge=1.0, le=3.0)] = 1.5
    # DISYUNTOR DE CATÁSTROFE — no configurable a "sin límite" ni
    # desactivable desde ninguna estrategia. Un stop ATR mal calibrado, un
    # gap de precio o un evento extremo del mercado nunca deben poder
    # dejar una posición sangrando sin fondo. Es independiente de
    # cualquier stop/trailing por estrategia: aunque el ATR de una
    # estrategia calcule un stop más ancho (o alguien lo desactive por
    # error), este límite absoluto sobre el precio de entrada SIEMPRE
    # cierra la posición si se alcanza.
    catastrophic_stop_pct: Annotated[float, Field(gt=0.0, le=0.5)] = 0.08  # -8%
    max_daily_drawdown: Annotated[float, Field(gt=0.0, le=0.20)] = 0.12
    atr_period: Annotated[int, Field(ge=5, le=50)] = 14
    atr_multiplier: Annotated[float, Field(ge=0.5, le=5.0)] = 2.0
    max_open_positions: Annotated[int, Field(ge=1, le=20)] = 14
    # NUEVO: cierre forzado por tiempo. Evita posiciones "zombis" que ni
    # tocan el stop ni convergen (visto en logs: SYNTHETIC_ARB en SOL
    # oscilando sin llegar al umbral de rentabilidad durante >6h, capital
    # bloqueado hasta un cierre manual). 0 desactiva el límite.
    # AJUSTADO: 3h resultó demasiado poco para que una reversión real se
    # complete — la mayoría de cierres automáticos eran por timeout con
    # el precio apenas movido (ni cerca del TP ni del stop), así que el
    # único "efecto" del timeout corto era cristalizar el arrastre de
    # comisiones antes de que la posición tuviera tiempo de revertir.
    # Subido a 24h para dar margen real.
    max_position_hold_seconds: Annotated[int, Field(ge=0)] = 86400  # 24h
    # OPTIMIZACIÓN: prórroga para posiciones que YA van en positivo al
    # vencer el plazo base, para darles margen a alcanzar su TP real en
    # vez de cortarlas a medio camino. No amplía el riesgo: en cuanto deja
    # de estar en positivo se cierra al instante, y el stop/trailing
    # normal sigue protegiendo durante toda la prórroga.
    max_hold_extension_mult: Annotated[float, Field(ge=1.0, le=5.0)] = 2.0
    # leverage eliminado: spot no admite apalancamiento en este bot.

    # ── Estrategias activas ───────────────────────────────────────────────────
    # Default alineado con el .env actual: si algún día falta el .env (o
    # la capa externa no lo sobreescribe, ver _env_file_candidates), el
    # bot arranca con las mismas estrategias que se han estado usando en
    # la práctica, en vez de caer solo a MEAN_REVERSION.
    active_strategies: list[ActiveStrategy] = Field(
        default=[
            ActiveStrategy.MEAN_REVERSION,
            ActiveStrategy.CROSS_CURRENCY_ARB,
            ActiveStrategy.TRIANGULAR_ARB,
        ]
    )

    # Estrategia A — Mean Reversion
    mr_vwap_window: int = 20
    mr_zscore_threshold: float = 1.8
    mr_bb_period: int = 20
    mr_bb_std: float = 2.0

    # Estrategia B — Volatility Breakout
    vb_volume_threshold: float = 0.08
    vb_breakout_atr_mult: float = 1.0
    vb_adx_threshold: float = 20.0
    vb_min_squeeze_bars: int = 3
    vb_ema_period: int = 50
    vb_bb_period: int = 20
    vb_kc_atr_mult: float = 1.5

    # Estrategia C — Synthetic Arbitrage (precio sintético vs par directo TARGET/BTC)
    # AJUSTADO: con 0.004 (0.4%) SYNTHETIC_ARB acumulaba 1 ganadora de 12
    # operaciones (8%) en las últimas 4 sesiones — el umbral era tan bajo
    # que entraba con ruido normal de precio, no con una desviación
    # estadísticamente significativa. Subido a 0.008 para exigir una señal
    # más clara y rara antes de arriesgar capital.
    # AJUSTADO: a 0.018 (1.8%) SYNTHETIC_ARB pasó >30h activa sin generar
    # NI UNA señal — demasiado exigente. Bajado a 0.01 (1%), sigue
    # cubriendo de sobra el coste round-trip pero deja pasar más señales.
    sa_min_deviation_pct: float = 0.01
    sa_lookback: int = 30                   # velas para media/desviación del spread sintético

    # Estrategia D — Smart Order Routing (verificación de spread/profundidad en TARGET/BTC)
    sr_max_spread_pct: float = 0.006        # spread bid/ask máximo tolerado
    sr_min_edge_pct: float = 0.005          # ventaja mínima esperada tras enrutar

    # Estrategia E — Cross-Rate Pairs Trading (z-score del cross-rate entre dos targets)
    crp_lookback: int = 60
    crp_zscore_entry: float = 1.5
    crp_zscore_exit: float = 0.3
    crp_pair_symbol_a: str = ""             # si vacío, se auto-selecciona de trading_symbols
    crp_pair_symbol_b: str = ""

    # Estrategia F — Portfolio Rebalancing (rebalanceo directo BTC↔TARGET por desviación de peso)
    pr_target_weights: dict[str, float] = Field(default_factory=dict)  # symbol -> peso [0,1]
    pr_rebalance_band_pct: float = 0.05     # banda de tolerancia antes de rebalancear
    pr_check_interval_bars: int = 12        # cada cuántas velas se evalúa el rebalanceo

    # Estrategia G — Cross-Currency Arbitrage (triangular BRIDGE vs dos TARGETs)
    # AJUSTADO: mismo motivo que sa_min_deviation_pct — a 1.8% llevaba
    # >30h sin generar ninguna señal. Bajado a 1%.
    cca_min_edge_pct: float = 0.01
    cca_lookback: int = 20

    # Estrategia J — Triangular Arb. Generaliza CROSS_CURRENCY_ARB: en vez
    # de comparar TARGET contra UNA sola referencia fija, recorre TODOS
    # los símbolos trackeados como posibles "tercer leg" del triángulo
    # BRIDGE→TARGET→REF→BRIDGE (vía sus respectivos TARGET/BRIDGE y
    # REF/BRIDGE, ya que Kraken no siempre lista el par TARGET/REF
    # directo) y opera el cruce con mayor desviación estadística (z-score)
    # sobre su media móvil, no solo el primero de la lista.
    # 48 velas de 5m ≈ 4h: con 20 el z-score era ruido de un par de horas
    # y disparaba sprays (8 LONGs vs ADA en el mismo segundo).
    tri_lookback: int = 48
    # AJUSTADO: con 2.0, TRIANGULAR_ARB cerró 8 de 9 operaciones en
    # pérdida (-0.5% a -1.05%, todas por timeout, ninguna por stop) en su
    # primera sesión real — el umbral dejaba entrar con desviaciones que
    # no llegaban a revertir a tiempo. Subido a 2.8 para exigir un
    # desalineamiento mucho más raro/significativo.
    # AJUSTADO: 2.8 dejaba pasar muy pocas señales en la práctica (las
    # únicas aperturas de TRIANGULAR_ARB en 37h llegaron ya con el filtro
    # de tendencia corregido). Bajado a 2.3 para encontrar más
    # oportunidades sin volver al 2.0 original (que ya dio problemas).
    tri_min_zscore: float = 2.3         # desviaciones estándar mínimas del ratio para entrar
    tri_exit_zscore: float = 0.5        # vuelve a esta banda (en desv. std) para cerrar por convergencia

    # Estrategia H — Trend Momentum (única estrategia de tendencia: todas
    # las anteriores asumen reversión a la media o arbitraje de spread, y
    # sufren por igual cuando el mercado tiende de forma sostenida en vez
    # de revertir). Entra a favor de una tendencia recién confirmada por
    # cruce de EMAs + fuerza ADX + volumen OBV; sale cuando el momentum se
    # agota (cruce EMA contrario).
    tm_ema_fast: int = 9
    tm_ema_slow: int = 21
    tm_adx_period: int = 14
    tm_adx_threshold: float = 25.0      # ADX > 25 = tendencia fuerte, filtra mercados laterales
    tm_obv_lookback: int = 20           # ventana para confirmar volumen acompañando el movimiento
    tm_stop_atr_mult: float = 3.5       # stop más ancho que mean-reversion: la tendencia necesita margen para pullbacks
    tm_tp_atr_mult: float = 4.0         # deja correr el beneficio más lejos que una reversión típica

    # Estrategia I — Dip Rebound (caída puntual dentro de tendencia bajista).
    # Entra en LONG cuando el precio cae >= dr_drop_pct desde el máximo de
    # las últimas dr_lookback velas Y la tendencia de fondo es bajista
    # (EMA rápida < EMA lenta), apostando al rebote técnico tras la
    # sobreventa. Cierra al alcanzar dr_target_net_pct de beneficio NETO
    # (ya descontadas ambas comisiones + slippage), o por stop ATR si la
    # caída continúa en vez de rebotar.
    dr_lookback: int = 20               # velas para el máximo de referencia
    dr_drop_pct: float = 0.015          # caída mínima desde el máximo (1.5%)
    dr_target_net_pct: float = 0.02     # objetivo de beneficio NETO (2%)
    dr_ema_fast: int = 9
    dr_ema_slow: int = 21
    dr_stop_atr_mult: float = 3.0       # stop más ancho: la caída ya es la premisa de entrada

    # ── WebSocket / reconexión ────────────────────────────────────────────────
    ws_reconnect_delay: int = 5
    ws_max_reconnects: int = 10
    rest_timeout: int = 10

    # ── Rate limiting ─────────────────────────────────────────────────────────
    # Kraken es más estricto que OKX con el rate limit de órdenes (tier-based).
    rate_limit_orders_per_second: int = 1
    rate_limit_weight_per_minute: int = 60

    # ── Logging ───────────────────────────────────────────────────────────────
    log_level: str = "INFO"
    log_format: LogFormat = LogFormat.JSON

    # ── Dashboard web ─────────────────────────────────────────────────────────
    dashboard_enabled: bool = True
    dashboard_host: str = "127.0.0.1"
    dashboard_port: Annotated[int, Field(ge=1, le=65535)] = 8001

    @field_validator("trading_symbols", "major_symbols", mode="before")
    @classmethod
    def parse_symbols(cls, v: str | list[str]) -> list[str]:
        if isinstance(v, str):
            return [s.strip().upper() for s in v.split(",") if s.strip()]
        return [s.upper() for s in v]

    @field_validator("active_strategies", mode="before")
    @classmethod
    def parse_strategies(cls, v: str | list) -> list:
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v

    # ── Fuentes de configuración personalizadas ───────────────────────────────
    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings, dotenv_settings, **kwargs):
        """Reemplaza las fuentes env con variantes que NO intentan hacer
        json.loads() en los campos de lista CSV, dejando que los
        field_validator(mode='before') se encarguen del parseo.
        Solución compatible con cualquier versión de pydantic-settings.
        """
        _csv_fields = frozenset({"trading_symbols", "major_symbols", "active_strategies"})

        for src in (env_settings, dotenv_settings):
            orig_cls = type(src)

            def _prepare(self, field_name, field, value, value_is_complex,
                         _orig=orig_cls.prepare_field_value, _csv=_csv_fields):
                if field_name in _csv and isinstance(value, str):
                    return value  # string crudo → field_validator lo parsea
                return _orig(self, field_name, field, value, value_is_complex)

            # Creamos subclase dinámica y reasignamos __class__ en el objeto ya
            # inicializado (legal en Python si la nueva clase es subclase).
            src.__class__ = type(f"Csv{orig_cls.__name__}", (orig_cls,), {
                "prepare_field_value": _prepare,
            })

        return (init_settings, env_settings, dotenv_settings, *kwargs.values())

    @property
    def is_live(self) -> bool:
        return self.bot_mode == BotMode.LIVE

    @property
    def round_trip_fee_pct(self) -> float:
        """Comisión combinada de un round-trip directo (2 legs × taker fee).
        Entrada: 1 orden BTC→TARGET; Salida: 1 orden TARGET→BTC."""
        return self.kraken_taker_fee_pct * 2

    @property
    def bridge_pair_str(self) -> str:
        """Mantenido por compatibilidad con código heredado.
        En operativa directa BTC→TARGET no existe salto BASE→BRIDGE separado;
        retorna cadena vacía para que cualquier código que lo llame falle
        de forma explícita en lugar de ejecutar una orden errónea."""
        return ""

    def target_pair(self, symbol: str) -> str:
        """Par ccxt directo TARGET/BTC (p.ej. 'ETH/BTC', 'SOL/BTC').
        Si `symbol` ya lleva '/', se retorna tal cual."""
        if "/" in symbol:
            return symbol
        return f"{symbol}/{self.convert_base_asset}"

    def kraken_pair(self, symbol: str) -> str:
        """Compatibilidad: retorna el par directo TARGET/BTC.
        Equivalente a `target_pair(symbol)`."""
        return self.target_pair(symbol)


# Singleton global
_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
