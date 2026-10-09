"""
dashboard/config_manager.py
Lectura y escritura segura del archivo .env desde la UI web.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import ValidationError

from core.config import ActiveStrategy, BotMode, LogFormat, Settings

ENV_PATH = Path(".env")

SECRET_FIELDS = {"kraken_api_key", "kraken_api_secret"}

FIELD_TO_ENV_KEY = {
    "bot_mode": "BOT_MODE",
    "use_testnet": "USE_TESTNET",
    "kraken_api_key": "KRAKEN_API_KEY",
    "kraken_api_secret": "KRAKEN_API_SECRET",
    "convert_base_asset": "CONVERT_BASE_ASSET",
    "trading_symbols": "TRADING_SYMBOLS",
    "timeframe": "TIMEFRAME",
    "risk_per_trade": "RISK_PER_TRADE",
    "max_daily_drawdown": "MAX_DAILY_DRAWDOWN",
    "atr_period": "ATR_PERIOD",
    "atr_multiplier": "ATR_MULTIPLIER",
    "max_open_positions": "MAX_OPEN_POSITIONS",
    "max_position_hold_seconds": "MAX_POSITION_HOLD_SECONDS",
    "max_hold_extension_mult": "MAX_HOLD_EXTENSION_MULT",
    "max_position_notional_pct": "MAX_POSITION_NOTIONAL_PCT",
    "catastrophic_stop_pct": "CATASTROPHIC_STOP_PCT",
    "confidence_size_min_mult": "CONFIDENCE_SIZE_MIN_MULT",
    "confidence_size_max_mult": "CONFIDENCE_SIZE_MAX_MULT",
    "min_net_profit_pct": "MIN_NET_PROFIT_PCT",
    "slippage_buffer_pct": "SLIPPAGE_BUFFER_PCT",
    "kraken_taker_fee_pct": "KRAKEN_TAKER_FEE_PCT",
    "active_strategies": "ACTIVE_STRATEGIES",
    "entry_mode": "ENTRY_MODE",
    "mr_vwap_window": "MR_VWAP_WINDOW",
    "mr_zscore_threshold": "MR_ZSCORE_THRESHOLD",
    "mr_bb_period": "MR_BB_PERIOD",
    "mr_bb_std": "MR_BB_STD",
    "vb_volume_threshold": "VB_VOLUME_THRESHOLD",
    "vb_breakout_atr_mult": "VB_BREAKOUT_ATR_MULT",
    "vb_adx_threshold": "VB_ADX_THRESHOLD",
    "vb_min_squeeze_bars": "VB_MIN_SQUEEZE_BARS",
    "vb_ema_period": "VB_EMA_PERIOD",
    "vb_bb_period": "VB_BB_PERIOD",
    "vb_kc_atr_mult": "VB_KC_ATR_MULT",
    "ws_reconnect_delay": "WS_RECONNECT_DELAY",
    "ws_max_reconnects": "WS_MAX_RECONNECTS",
    "rest_timeout": "REST_TIMEOUT",
    "rate_limit_orders_per_second": "RATE_LIMIT_ORDERS_PER_SECOND",
    "rate_limit_weight_per_minute": "RATE_LIMIT_WEIGHT_PER_MINUTE",
    "log_level": "LOG_LEVEL",
    "log_format": "LOG_FORMAT",
    "dashboard_enabled": "DASHBOARD_ENABLED",
    "dashboard_host": "DASHBOARD_HOST",
    "dashboard_port": "DASHBOARD_PORT",
    "tm_ema_fast": "TM_EMA_FAST",
    "tm_ema_slow": "TM_EMA_SLOW",
    "tm_adx_period": "TM_ADX_PERIOD",
    "tm_adx_threshold": "TM_ADX_THRESHOLD",
    "tm_obv_lookback": "TM_OBV_LOOKBACK",
    "tm_stop_atr_mult": "TM_STOP_ATR_MULT",
    "tm_tp_atr_mult": "TM_TP_ATR_MULT",
    "dr_lookback": "DR_LOOKBACK",
    "dr_drop_pct": "DR_DROP_PCT",
    "dr_target_net_pct": "DR_TARGET_NET_PCT",
    "dr_ema_fast": "DR_EMA_FAST",
    "dr_ema_slow": "DR_EMA_SLOW",
    "dr_stop_atr_mult": "DR_STOP_ATR_MULT",
    "tri_lookback": "TRI_LOOKBACK",
    "tri_min_zscore": "TRI_MIN_ZSCORE",
    "tri_exit_zscore": "TRI_EXIT_ZSCORE",
}

FIELD_GROUPS: list[dict[str, Any]] = [
    {"id": "operation", "label": "Modo de operación", "fields": ["bot_mode", "use_testnet"]},
    {"id": "credentials", "label": "Credenciales Kraken", "fields": ["kraken_api_key", "kraken_api_secret"]},
    {"id": "market", "label": "Activo base y pares de conversión",
     "fields": ["convert_base_asset", "trading_symbols", "timeframe"]},
    {"id": "risk", "label": "Gestión de riesgo",
     "fields": ["risk_per_trade", "max_daily_drawdown", "atr_period", "atr_multiplier", "max_open_positions",
                "max_position_hold_seconds", "max_hold_extension_mult",
                "max_position_notional_pct", "catastrophic_stop_pct",
                "confidence_size_min_mult", "confidence_size_max_mult",
                "min_net_profit_pct", "slippage_buffer_pct", "kraken_taker_fee_pct"]},
    {"id": "strategies", "label": "Estrategias activas", "fields": ["active_strategies", "entry_mode"]},
    {"id": "mean_reversion", "label": "Estrategia A — Mean Reversion",
     "fields": ["mr_vwap_window", "mr_zscore_threshold", "mr_bb_period", "mr_bb_std"]},
    {"id": "volatility_breakout", "label": "Estrategia B — Volatility Breakout",
     "fields": ["vb_volume_threshold", "vb_breakout_atr_mult", "vb_adx_threshold",
                "vb_min_squeeze_bars", "vb_ema_period", "vb_bb_period", "vb_kc_atr_mult"]},
    {"id": "trend_momentum", "label": "Estrategia H — Trend Momentum",
     "fields": ["tm_ema_fast", "tm_ema_slow", "tm_adx_period", "tm_adx_threshold",
                "tm_obv_lookback", "tm_stop_atr_mult", "tm_tp_atr_mult"]},
    {"id": "dip_rebound", "label": "Estrategia I — Dip Rebound",
     "fields": ["dr_lookback", "dr_drop_pct", "dr_target_net_pct",
                "dr_ema_fast", "dr_ema_slow", "dr_stop_atr_mult"]},
    {"id": "triangular_arb", "label": "Estrategia J — Triangular Arb",
     "fields": ["tri_lookback", "tri_min_zscore", "tri_exit_zscore"]},
    {"id": "websocket", "label": "WebSocket / Reconexión",
     "fields": ["ws_reconnect_delay", "ws_max_reconnects", "rest_timeout"]},
    {"id": "rate_limit", "label": "Rate limiting",
     "fields": ["rate_limit_orders_per_second", "rate_limit_weight_per_minute"]},
    {"id": "logging", "label": "Logging", "fields": ["log_level", "log_format"]},
    {"id": "dashboard", "label": "Dashboard web", "fields": ["dashboard_enabled", "dashboard_host", "dashboard_port"]},
]

FIELD_META: dict[str, dict[str, Any]] = {
    "bot_mode": {"type": "select", "options": [m.value for m in BotMode]},
    "use_testnet": {"type": "bool"},
    "kraken_api_key": {"type": "secret"},
    "kraken_api_secret": {"type": "secret"},
    "convert_base_asset": {"type": "text", "placeholder": "DOGE"},
    "trading_symbols": {"type": "text", "placeholder": "BTC,ETH"},
    "timeframe": {"type": "select", "options": ["1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"]},
    "risk_per_trade": {"type": "number", "step": 0.001, "min": 0.001, "max": 0.05, "suffix": "fracción (0.01 = 1%)"},
    "max_daily_drawdown": {"type": "number", "step": 0.01, "min": 0.01, "max": 0.20, "suffix": "fracción (0.05 = 5%)"},
    "atr_period": {"type": "number", "step": 1, "min": 5, "max": 50},
    "atr_multiplier": {"type": "number", "step": 0.1, "min": 0.5, "max": 5.0},
    "max_open_positions": {"type": "number", "step": 1, "min": 1, "max": 10},
    "max_position_hold_seconds": {"type": "number", "step": 300, "min": 0, "suffix": "segundos (0 = sin límite)"},
    "max_hold_extension_mult": {"type": "number", "step": 0.5, "min": 1.0, "max": 5.0, "suffix": "× plazo base, solo si va en positivo"},
    "max_position_notional_pct": {"type": "number", "step": 0.01, "min": 0.01, "max": 1.0, "suffix": "fracción (0.3 = 30% del balance)"},
    "catastrophic_stop_pct": {"type": "number", "step": 0.01, "min": 0.01, "max": 0.5, "suffix": "fracción (0.08 = -8%), disyuntor no desactivable"},
    "confidence_size_min_mult": {"type": "number", "step": 0.1, "min": 0.1, "max": 1.0, "suffix": "× risk_per_trade con confianza mínima"},
    "confidence_size_max_mult": {"type": "number", "step": 0.1, "min": 1.0, "max": 3.0, "suffix": "× risk_per_trade con confianza máxima"},
    "min_net_profit_pct": {"type": "number", "step": 0.001, "min": 0.0, "max": 0.05, "suffix": "fracción (0.006 = 0.6%)"},
    "slippage_buffer_pct": {"type": "number", "step": 0.0005, "min": 0.0, "max": 0.02, "suffix": "fracción (0.0035 = 0.35%)"},
    "kraken_taker_fee_pct": {"type": "number", "step": 0.0001, "min": 0.0, "max": 0.01, "suffix": "fracción por leg (0.0026 = 0.26%)"},
    "entry_mode": {"type": "select", "options": ["CONTRARIAN", "MOMENTUM"]},
    "active_strategies": {"type": "multiselect", "options": [s.value for s in ActiveStrategy]},
    "mr_vwap_window": {"type": "number", "step": 1, "min": 2},
    "mr_zscore_threshold": {"type": "number", "step": 0.1},
    "mr_bb_period": {"type": "number", "step": 1, "min": 2},
    "mr_bb_std": {"type": "number", "step": 0.1},
    "vb_volume_threshold": {"type": "number", "step": 0.01},
    "vb_breakout_atr_mult": {"type": "number", "step": 0.1},
    "vb_adx_threshold": {"type": "number", "step": 0.5},
    "vb_min_squeeze_bars": {"type": "number", "step": 1, "min": 1},
    "vb_ema_period": {"type": "number", "step": 1, "min": 2},
    "vb_bb_period": {"type": "number", "step": 1, "min": 2},
    "vb_kc_atr_mult": {"type": "number", "step": 0.1},
    "tm_ema_fast": {"type": "number", "step": 1, "min": 2},
    "tm_ema_slow": {"type": "number", "step": 1, "min": 3},
    "tm_adx_period": {"type": "number", "step": 1, "min": 5, "max": 50},
    "tm_adx_threshold": {"type": "number", "step": 0.5, "min": 10, "max": 60, "suffix": "ADX > umbral = tendencia fuerte"},
    "tm_obv_lookback": {"type": "number", "step": 1, "min": 2},
    "tm_stop_atr_mult": {"type": "number", "step": 0.1, "min": 0.5, "max": 6.0},
    "tm_tp_atr_mult": {"type": "number", "step": 0.1, "min": 0.5, "max": 10.0},
    "dr_lookback": {"type": "number", "step": 1, "min": 5, "max": 200},
    "dr_drop_pct": {"type": "number", "step": 0.001, "min": 0.001, "max": 0.2, "suffix": "fracción (0.015 = 1.5%)"},
    "dr_target_net_pct": {"type": "number", "step": 0.001, "min": 0.001, "max": 0.2, "suffix": "fracción (0.02 = 2%), neto de comisiones"},
    "dr_ema_fast": {"type": "number", "step": 1, "min": 2},
    "dr_ema_slow": {"type": "number", "step": 1, "min": 3},
    "dr_stop_atr_mult": {"type": "number", "step": 0.1, "min": 0.5, "max": 6.0},
    "tri_lookback": {"type": "number", "step": 1, "min": 5, "max": 200},
    "tri_min_zscore": {"type": "number", "step": 0.1, "min": 0.5, "max": 5.0, "suffix": "desv. típicas mínimas para entrar"},
    "tri_exit_zscore": {"type": "number", "step": 0.1, "min": 0.1, "max": 3.0, "suffix": "desv. típicas para cerrar por convergencia"},
    "ws_reconnect_delay": {"type": "number", "step": 1, "min": 1},
    "ws_max_reconnects": {"type": "number", "step": 1, "min": 1},
    "rest_timeout": {"type": "number", "step": 1, "min": 1},
    "rate_limit_orders_per_second": {"type": "number", "step": 1, "min": 1},
    "rate_limit_weight_per_minute": {"type": "number", "step": 10, "min": 10},
    "log_level": {"type": "select", "options": ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]},
    "log_format": {"type": "select", "options": [f.value for f in LogFormat]},
    "dashboard_enabled": {"type": "bool"},
    "dashboard_host": {"type": "text"},
    "dashboard_port": {"type": "number", "step": 1, "min": 1, "max": 65535},
}


def _mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return "•" * len(value)
    return "•" * (len(value) - 4) + value[-4:]


def _serialize_value(field_name: str, value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        items = [v.value if hasattr(v, "value") else str(v) for v in value]
        return ",".join(items)
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)


def get_current_config() -> dict[str, Any]:
    settings = Settings()
    groups_out = []

    for group in FIELD_GROUPS:
        fields_out = []
        for field_name in group["fields"]:
            raw_value = getattr(settings, field_name)
            meta = FIELD_META.get(field_name, {"type": "text"})

            if field_name in SECRET_FIELDS:
                display_value = _mask_secret(raw_value) if raw_value else ""
                has_value = bool(raw_value)
            elif isinstance(raw_value, list):
                display_value = [v.value if hasattr(v, "value") else v for v in raw_value]
                has_value = True
            elif hasattr(raw_value, "value"):
                display_value = raw_value.value
                has_value = True
            else:
                display_value = raw_value
                has_value = True

            fields_out.append({
                "name": field_name,
                "env_key": FIELD_TO_ENV_KEY[field_name],
                "value": display_value,
                "has_value": has_value,
                "is_secret": field_name in SECRET_FIELDS,
                **meta,
            })

        groups_out.append({"id": group["id"], "label": group["label"], "fields": fields_out})

    return {"groups": groups_out, "is_live": settings.is_live}


def _read_env_lines() -> list[str]:
    if ENV_PATH.exists():
        return ENV_PATH.read_text(encoding="utf-8").splitlines()
    return []


def _write_env_lines(lines: list[str]) -> None:
    content = "\n".join(lines)
    if not content.endswith("\n"):
        content += "\n"
    ENV_PATH.write_text(content, encoding="utf-8")


def update_config(changes: dict[str, Any]) -> dict[str, Any]:
    current = Settings()
    merged: dict[str, Any] = {}

    for field_name in FIELD_TO_ENV_KEY:
        merged[field_name] = getattr(current, field_name)

    for field_name, new_value in changes.items():
        if field_name not in FIELD_TO_ENV_KEY:
            continue
        if field_name in SECRET_FIELDS:
            if not new_value or set(new_value) == {"•"}:
                continue
        merged[field_name] = new_value

    try:
        validated = Settings(**merged)
    except ValidationError as exc:
        errors: dict[str, str] = {}
        for err in exc.errors():
            loc = ".".join(str(x) for x in err["loc"]) if err["loc"] else "general"
            errors[loc] = err["msg"]
        return {"success": False, "errors": errors}

    lines = _read_env_lines()
    keys_written: set[str] = set()
    new_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            new_lines.append(line)
            continue

        key = stripped.split("=", 1)[0].strip()
        matched_field = next((f for f, k in FIELD_TO_ENV_KEY.items() if k == key), None)
        if matched_field is None:
            new_lines.append(line)
            continue

        new_value = _serialize_value(matched_field, getattr(validated, matched_field))
        new_lines.append(f"{key}={new_value}")
        keys_written.add(key)

    missing = [(field, env_key) for field, env_key in FIELD_TO_ENV_KEY.items() if env_key not in keys_written]
    if missing:
        new_lines.append("")
        new_lines.append("# ─── Añadido desde el dashboard ───────────────────────────────────────────")
        for field, env_key in missing:
            new_lines.append(f"{env_key}={_serialize_value(field, getattr(validated, field))}")

    _write_env_lines(new_lines)
    return {"success": True, "errors": {}}
