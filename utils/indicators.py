"""
utils/indicators.py
Indicadores técnicos optimizados con numpy/numba.
Todas las funciones trabajan sobre arrays de numpy para máxima eficiencia.
"""
from __future__ import annotations

import numpy as np
from numpy.typing import NDArray


# ─────────────────────────────────────────────────────────────────────────────
#  ATR — Average True Range
# ─────────────────────────────────────────────────────────────────────────────

def true_range(
    high: NDArray[np.float64],
    low: NDArray[np.float64],
    close: NDArray[np.float64],
) -> NDArray[np.float64]:
    """
    True Range para cada barra.
    TR = max(H-L, |H-C_prev|, |L-C_prev|)
    """
    n = len(high)
    tr = np.empty(n, dtype=np.float64)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        hl = high[i] - low[i]
        hc = abs(high[i] - close[i - 1])
        lc = abs(low[i] - close[i - 1])
        tr[i] = max(hl, hc, lc)
    return tr


def atr(
    high: NDArray[np.float64],
    low: NDArray[np.float64],
    close: NDArray[np.float64],
    period: int = 14,
) -> NDArray[np.float64]:
    """
    ATR usando EMA de Wilder (RMA).
    Retorna array del mismo tamaño. Los primeros `period` valores son NaN.
    """
    tr = true_range(high, low, close)
    n = len(tr)
    result = np.full(n, np.nan, dtype=np.float64)
    if n < period:
        return result

    # Primera barra: simple mean
    result[period - 1] = np.mean(tr[:period])
    alpha = 1.0 / period
    for i in range(period, n):
        result[i] = result[i - 1] * (1.0 - alpha) + tr[i] * alpha
    return result


# ─────────────────────────────────────────────────────────────────────────────
#  VWAP — Volume Weighted Average Price
# ─────────────────────────────────────────────────────────────────────────────

def vwap(
    high: NDArray[np.float64],
    low: NDArray[np.float64],
    close: NDArray[np.float64],
    volume: NDArray[np.float64],
    window: int | None = None,
) -> NDArray[np.float64]:
    """
    VWAP rolling. Si window=None calcula VWAP de sesión (acumulado).
    Retorna array del mismo tamaño.
    """
    typical = (high + low + close) / 3.0
    tpv = typical * volume  # Precio × Volumen

    if window is None:
        cum_tpv = np.cumsum(tpv)
        cum_vol = np.cumsum(volume)
    else:
        # Rolling sum con convolución O(n)
        kernel = np.ones(window, dtype=np.float64)
        cum_tpv = np.convolve(tpv, kernel, mode="full")[: len(tpv)]
        cum_vol = np.convolve(volume, kernel, mode="full")[: len(volume)]
        # Primeros (window-1) valores son parciales; los forzamos a NaN
        cum_tpv[:window - 1] = np.nan
        cum_vol[:window - 1] = np.nan

    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(cum_vol > 0, cum_tpv / cum_vol, np.nan)


def vwap_zscore(
    close: NDArray[np.float64],
    vwap_values: NDArray[np.float64],
    window: int = 20,
) -> NDArray[np.float64]:
    """
    Z-Score rodante del desvío precio vs VWAP.
    z = (close - vwap) / rolling_std(close - vwap, window)
    """
    diff = close - vwap_values
    n = len(diff)
    result = np.full(n, np.nan, dtype=np.float64)

    for i in range(window - 1, n):
        segment = diff[i - window + 1 : i + 1]
        std = np.std(segment, ddof=1)
        if std > 0:
            result[i] = diff[i] / std
    return result


# ─────────────────────────────────────────────────────────────────────────────
#  Bollinger Bands
# ─────────────────────────────────────────────────────────────────────────────

def bollinger_bands(
    close: NDArray[np.float64],
    period: int = 20,
    std_dev: float = 2.0,
) -> tuple[NDArray[np.float64], NDArray[np.float64], NDArray[np.float64]]:
    """
    Bollinger Bands.
    Retorna (upper, middle, lower). Primeros (period-1) valores son NaN.
    """
    n = len(close)
    upper = np.full(n, np.nan)
    middle = np.full(n, np.nan)
    lower = np.full(n, np.nan)

    for i in range(period - 1, n):
        segment = close[i - period + 1 : i + 1]
        mean = np.mean(segment)
        std = np.std(segment, ddof=1)
        middle[i] = mean
        upper[i] = mean + std_dev * std
        lower[i] = mean - std_dev * std

    return upper, middle, lower


def bb_percent_b(
    close: NDArray[np.float64],
    upper: NDArray[np.float64],
    lower: NDArray[np.float64],
) -> NDArray[np.float64]:
    """
    %B de Bollinger. Indica dónde está el precio dentro de las bandas.
    %B = (close - lower) / (upper - lower)
    """
    band_width = upper - lower
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(band_width > 0, (close - lower) / band_width, np.nan)


# ─────────────────────────────────────────────────────────────────────────────
#  Volume Delta
# ─────────────────────────────────────────────────────────────────────────────

def rolling_volume_ratio(
    volume: NDArray[np.float64],
    window: int = 20,
) -> NDArray[np.float64]:
    """
    Ratio del volumen actual sobre el volumen promedio de la ventana.
    ratio = volume[i] / mean(volume[i-window:i])
    """
    n = len(volume)
    result = np.full(n, np.nan, dtype=np.float64)
    for i in range(window, n):
        avg = np.mean(volume[i - window : i])
        if avg > 0:
            result[i] = volume[i] / avg
    return result


def cumulative_delta(buy_vol: NDArray[np.float64], sell_vol: NDArray[np.float64]) -> NDArray[np.float64]:
    """
    Delta acumulado: diferencia entre volumen comprador y vendedor.
    Útil para detectar divergencia precio/delta.
    """
    return np.cumsum(buy_vol - sell_vol)


# ─────────────────────────────────────────────────────────────────────────────
#  EMA — Exponential Moving Average
# ─────────────────────────────────────────────────────────────────────────────

def ema(
    data: NDArray[np.float64],
    period: int,
) -> NDArray[np.float64]:
    """
    EMA estándar con inicialización por SMA.
    Retorna array del mismo tamaño. Los primeros (period-1) valores son NaN.
    """
    n = len(data)
    result = np.full(n, np.nan, dtype=np.float64)
    if n < period:
        return result

    # Seed: SMA de las primeras `period` barras
    result[period - 1] = np.mean(data[:period])
    k = 2.0 / (period + 1.0)
    for i in range(period, n):
        result[i] = data[i] * k + result[i - 1] * (1.0 - k)
    return result


# ─────────────────────────────────────────────────────────────────────────────
#  ADX — Average Directional Index (método Wilder)
# ─────────────────────────────────────────────────────────────────────────────

def adx(
    high: NDArray[np.float64],
    low: NDArray[np.float64],
    close: NDArray[np.float64],
    period: int = 14,
) -> tuple[NDArray[np.float64], NDArray[np.float64], NDArray[np.float64]]:
    """
    ADX de Wilder.

    Returns:
        (adx_line, plus_di, minus_di) — todos del mismo tamaño, NaN al inicio.

    Interpretación:
        - ADX > 25: tendencia fuerte.
        - +DI > -DI: tendencia alcista.
        - -DI > +DI: tendencia bajista.
    """
    n = len(high)
    plus_dm  = np.zeros(n, dtype=np.float64)
    minus_dm = np.zeros(n, dtype=np.float64)
    tr_arr   = true_range(high, low, close)

    for i in range(1, n):
        up_move   = high[i] - high[i - 1]
        down_move = low[i - 1] - low[i]
        plus_dm[i]  = up_move   if (up_move > down_move and up_move > 0)   else 0.0
        minus_dm[i] = down_move if (down_move > up_move and down_move > 0) else 0.0

    # Wilder smoothing (RMA) de TR, +DM, -DM
    def _wilder_smooth(arr: NDArray[np.float64], p: int) -> NDArray[np.float64]:
        out = np.full(len(arr), np.nan)
        if len(arr) < p:
            return out
        out[p - 1] = np.sum(arr[:p])
        alpha = 1.0 / p
        for i in range(p, len(arr)):
            out[i] = out[i - 1] * (1.0 - alpha) + arr[i]
        return out

    smooth_tr  = _wilder_smooth(tr_arr, period)
    smooth_pdm = _wilder_smooth(plus_dm, period)
    smooth_mdm = _wilder_smooth(minus_dm, period)

    with np.errstate(invalid="ignore", divide="ignore"):
        plus_di_arr  = np.where(smooth_tr > 0, 100.0 * smooth_pdm / smooth_tr, np.nan)
        minus_di_arr = np.where(smooth_tr > 0, 100.0 * smooth_mdm / smooth_tr, np.nan)
        dx = np.where(
            (plus_di_arr + minus_di_arr) > 0,
            100.0 * np.abs(plus_di_arr - minus_di_arr) / (plus_di_arr + minus_di_arr),
            np.nan,
        )

    # ADX = Wilder smooth del DX
    adx_arr = _wilder_smooth(np.where(np.isnan(dx), 0.0, dx), period)
    # Re-propagar NaN donde no había datos suficientes
    adx_arr[:period * 2 - 1] = np.nan

    return adx_arr, plus_di_arr, minus_di_arr


# ─────────────────────────────────────────────────────────────────────────────
#  Keltner Channels
# ─────────────────────────────────────────────────────────────────────────────

def keltner_channels(
    high: NDArray[np.float64],
    low: NDArray[np.float64],
    close: NDArray[np.float64],
    ema_period: int = 20,
    atr_period: int = 10,
    atr_multiplier: float = 1.5,
) -> tuple[NDArray[np.float64], NDArray[np.float64], NDArray[np.float64]]:
    """
    Keltner Channels basados en EMA ± ATR × multiplier.

    Returns:
        (upper, middle, lower)

    Uso principal: Squeeze cuando Bollinger está DENTRO de Keltner.
    """
    middle = ema(close, ema_period)
    atr_vals = atr(high, low, close, atr_period)
    dist = atr_vals * atr_multiplier

    upper = middle + dist
    lower = middle - dist
    return upper, middle, lower


# ─────────────────────────────────────────────────────────────────────────────
#  Squeeze Momentum (Lazybear / modificado)
# ─────────────────────────────────────────────────────────────────────────────

def squeeze_momentum(
    high: NDArray[np.float64],
    low: NDArray[np.float64],
    close: NDArray[np.float64],
    bb_period: int = 20,
    bb_std: float = 2.0,
    kc_period: int = 20,
    kc_atr_period: int = 10,
    kc_atr_mult: float = 1.5,
) -> tuple[NDArray[np.float64], NDArray[np.bool_], NDArray[np.float64]]:
    """
    Squeeze Momentum Indicator.

    Returns:
        momentum  : Histograma de momentum (positivo/negativo).
        in_squeeze: Bool array — True cuando Bollinger está DENTRO de Keltner.
        squeeze_release: Diferencia entre bandas (cuánto se expande).

    Squeeze ON  => mercado en compresión (rango estrecho, energía acumulada).
    Squeeze OFF => expansión inminente → breakout signal.
    """
    n = len(close)
    volumes = np.ones(n)  # Placeholder; Keltner no usa volumen

    # Bollinger Bands
    bb_upper, bb_mid, bb_lower = bollinger_bands(close, bb_period, bb_std)

    # Keltner Channels
    kc_upper, kc_mid, kc_lower = keltner_channels(
        high, low, close, kc_period, kc_atr_period, kc_atr_mult
    )

    # Squeeze: Bollinger DENTRO de Keltner
    with np.errstate(invalid="ignore"):
        in_squeeze = (bb_upper <= kc_upper) & (bb_lower >= kc_lower)

    # Momentum: linreg(close - midpoint(highest_high, lowest_low), period)
    highest_high = np.full(n, np.nan)
    lowest_low   = np.full(n, np.nan)
    for i in range(bb_period - 1, n):
        highest_high[i] = np.max(high[i - bb_period + 1 : i + 1])
        lowest_low[i]   = np.min(low[i - bb_period + 1 : i + 1])

    delta_hl = (highest_high + lowest_low) / 2.0
    with np.errstate(invalid="ignore"):
        src = close - (delta_hl + bb_mid) / 2.0

    # Regresión lineal rolling para suavizar el momentum
    momentum = np.full(n, np.nan, dtype=np.float64)
    x = np.arange(bb_period, dtype=np.float64)
    x -= x.mean()
    for i in range(bb_period - 1, n):
        y = src[i - bb_period + 1 : i + 1]
        if not np.any(np.isnan(y)):
            # Pendiente de la regresión lineal
            momentum[i] = float(np.polyfit(x, y, 1)[0])

    # Squeeze release: expansión de Keltner relativa al precio
    kc_width = kc_upper - kc_lower
    with np.errstate(invalid="ignore", divide="ignore"):
        squeeze_release = np.where(kc_mid > 0, kc_width / kc_mid, np.nan)

    return momentum, in_squeeze, squeeze_release


# ─────────────────────────────────────────────────────────────────────────────
#  OBV — On-Balance Volume (rolling normalizado)
# ─────────────────────────────────────────────────────────────────────────────

def obv(
    close: NDArray[np.float64],
    volume: NDArray[np.float64],
) -> NDArray[np.float64]:
    """
    On-Balance Volume acumulado.
    OBV[i] = OBV[i-1] + vol  si close[i] > close[i-1]
           = OBV[i-1] - vol  si close[i] < close[i-1]
           = OBV[i-1]        si close[i] == close[i-1]
    """
    n = len(close)
    result = np.zeros(n, dtype=np.float64)
    for i in range(1, n):
        if close[i] > close[i - 1]:
            result[i] = result[i - 1] + volume[i]
        elif close[i] < close[i - 1]:
            result[i] = result[i - 1] - volume[i]
        else:
            result[i] = result[i - 1]
    return result


def obv_divergence(
    close: NDArray[np.float64],
    volume: NDArray[np.float64],
    window: int = 20,
) -> NDArray[np.float64]:
    """
    Divergencia OBV vs precio (normalizada).

    Positivo => OBV sube mientras precio baja (divergencia alcista).
    Negativo => OBV baja mientras precio sube (divergencia bajista).

    Cálculo: slope(OBV, window) - slope(close, window) normalizado.
    """
    obv_vals = obv(close, volume)
    n = len(close)
    result = np.full(n, np.nan, dtype=np.float64)
    x = np.arange(window, dtype=np.float64)

    for i in range(window - 1, n):
        price_seg = close[i - window + 1 : i + 1]
        obv_seg   = obv_vals[i - window + 1 : i + 1]
        if np.any(np.isnan(price_seg)):
            continue

        # Pendientes normalizadas
        price_slope = np.polyfit(x, price_seg, 1)[0] / (price_seg.mean() + 1e-10)
        obv_slope   = np.polyfit(x, obv_seg, 1)[0]   / (abs(obv_seg.mean()) + 1e-10)
        result[i]   = obv_slope - price_slope

    return result


# ─────────────────────────────────────────────────────────────────────────────
#  Volume Delta Divergence (a partir de candles OHLCV)
# ─────────────────────────────────────────────────────────────────────────────

def estimate_buy_sell_volume(
    open_: NDArray[np.float64],
    high: NDArray[np.float64],
    low: NDArray[np.float64],
    close: NDArray[np.float64],
    volume: NDArray[np.float64],
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """
    Estima volumen comprador y vendedor a partir de OHLCV (sin tick data).

    Método: fracción de rango alcista vs total (approx. de Kaplan/Wick).
    buy_frac  = (close - low) / (high - low + ε)
    sell_frac = 1 - buy_frac
    """
    spread = high - low
    with np.errstate(invalid="ignore", divide="ignore"):
        buy_frac  = np.where(spread > 0, (close - low) / spread, 0.5)
    sell_frac = 1.0 - buy_frac
    return volume * buy_frac, volume * sell_frac


def rolling_delta(
    open_: NDArray[np.float64],
    high: NDArray[np.float64],
    low: NDArray[np.float64],
    close: NDArray[np.float64],
    volume: NDArray[np.float64],
    window: int = 14,
) -> NDArray[np.float64]:
    """
    Delta de volumen rolling (compras - ventas) normalizado por volumen total.
    Rango [-1, +1]. Positivo => dominancia compradora.
    """
    buy_vol, sell_vol = estimate_buy_sell_volume(open_, high, low, close, volume)
    raw_delta = buy_vol - sell_vol

    n = len(raw_delta)
    result = np.full(n, np.nan, dtype=np.float64)
    kernel = np.ones(window, dtype=np.float64)

    cum_delta = np.convolve(raw_delta, kernel, mode="full")[:n]
    cum_vol   = np.convolve(volume,    kernel, mode="full")[:n]

    with np.errstate(invalid="ignore", divide="ignore"):
        result = np.where(cum_vol > 0, cum_delta / cum_vol, np.nan)

    result[:window - 1] = np.nan
    return result


def delta_price_divergence(
    close: NDArray[np.float64],
    delta: NDArray[np.float64],
    window: int = 20,
) -> NDArray[np.float64]:
    """
    Detecta divergencia entre delta de volumen y movimiento de precio.

    Retorna:
        > 0 => divergencia alcista (precio baja, delta sube)
        < 0 => divergencia bajista (precio sube, delta baja)
        ≈ 0 => sin divergencia
    """
    n = len(close)
    result = np.full(n, np.nan, dtype=np.float64)
    x = np.arange(window, dtype=np.float64)

    for i in range(window - 1, n):
        p_seg = close[i - window + 1 : i + 1]
        d_seg = delta[i - window + 1 : i + 1]
        if np.any(np.isnan(d_seg)):
            continue
        p_slope = np.polyfit(x, p_seg, 1)[0] / (p_seg.mean() + 1e-10)
        d_slope = np.polyfit(x, d_seg, 1)[0]
        result[i] = d_slope - p_slope  # positivo = divergencia alcista

    return result
