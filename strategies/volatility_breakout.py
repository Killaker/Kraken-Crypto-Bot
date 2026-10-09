"""
strategies/volatility_breakout.py
Estrategia B — Volatility Breakout: Squeeze Momentum + Volume/Delta Divergence.

Lógica de trading:
══════════════════
El mercado alterna entre fases de COMPRESIÓN (baja volatilidad, rango estrecho)
y EXPANSIÓN (breakout explosivo). Esta estrategia detecta exactamente ese momento.

FASE 1 — SQUEEZE DETECTION:
  - Bollinger Bands están DENTRO de Keltner Channels → energía comprimida.
  - Se espera activamente el squeeze.

FASE 2 — BREAKOUT CONFIRMATION (squeeze acaba de liberarse):
  El squeeze OFF (Bollinger sale de Keltner) se combina con:

  LONG:
    - Momentum Squeeze positivo y creciente (presión alcista acumulada).
    - Delta de volumen > umbral (dominancia compradora en la ruptura).
    - ADX > 20 (breakout con tendencia real, no ruido).
    - OBV divergencia no bajista (OBV apoya el movimiento).
    - Precio cruza EMA de tendencia desde abajo.

  SHORT:
    - Momentum Squeeze negativo y decreciente (presión bajista acumulada).
    - Delta de volumen < -umbral (dominancia vendedora).
    - ADX > 20.
    - OBV divergencia no alcista.
    - Precio cruza EMA de tendencia desde arriba.

EXIT:
  - ADX cae por debajo de 20 (agotamiento de tendencia).
  - Momentum cambia de signo (reversion).
  - Trailing Stop ATR alcanzado.

STOP LOSS: ATR × multiplier desde el rango de la vela de breakout.
TAKE PROFIT: Proyección 2× el ancho del squeeze (distancia de Keltner).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

import numpy as np

from core.config import Settings
from core.logger import get_logger
from models.market_data import Candle, Ticker
from models.order import Position
from models.signal import Signal, SignalDirection, SignalStrength
from strategies.base_strategy import BaseStrategy
from utils.indicators import (
    adx,
    atr,
    bollinger_bands,
    delta_price_divergence,
    ema,
    keltner_channels,
    obv_divergence,
    rolling_delta,
    rolling_volume_ratio,
    squeeze_momentum,
)

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SqueezeState:
    """Estado del squeeze en un instante dado."""
    in_squeeze: bool          # Está comprimido ahora
    just_fired: bool          # Acaba de salir del squeeze (transición ON → OFF)
    momentum: float           # Valor del momentum (+ alcista, - bajista)
    momentum_rising: bool     # El momentum está creciendo (en absoluto)
    momentum_prev: float      # Momentum de la barra anterior
    squeeze_duration: int     # Barras que lleva en squeeze (o llevaba)


class VolatilityBreakoutStrategy(BaseStrategy):
    """
    Estrategia B: Volatility Breakout mediante Squeeze Momentum + Delta Divergence.

    Configuración desde Settings:
    - vb_volume_threshold:   Delta mínimo para confirmar breakout (default 0.08).
    - vb_breakout_atr_mult:  Multiplicador ATR para SL en breakout (default 1.0).
    - vb_adx_threshold:      ADX mínimo para confirmar tendencia (default 20).
    - vb_min_squeeze_bars:   Barras mínimas en squeeze para señal válida (default 3).
    - vb_ema_period:         EMA de tendencia para filtrar dirección (default 50).
    - vb_bb_period:          Período Bollinger para squeeze (default 20).
    - vb_kc_atr_mult:        Multiplicador ATR de Keltner (default 1.5).
    """

    strategy_id = "VOLATILITY_BREAKOUT"

    # Umbrales internos
    _ADX_STRONG_TREND   = 25.0   # ADX > 25 = tendencia fuerte (bonus confianza)
    _MOMENTUM_MIN_ABS   = 0.0    # Sin mínimo: pares */BTC tienen momentum ~0.000001
    _OBV_NEUTRAL_BAND   = 0.02   # Divergencia OBV considerada neutral

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

        # Parámetros configurables
        self._volume_threshold  = getattr(settings, "vb_volume_threshold",  0.08)
        self._breakout_atr_mult = getattr(settings, "vb_breakout_atr_mult", 1.0)
        self._adx_threshold     = getattr(settings, "vb_adx_threshold",     20.0)
        self._min_squeeze_bars  = getattr(settings, "vb_min_squeeze_bars",  3)
        self._ema_period        = getattr(settings, "vb_ema_period",        50)
        self._bb_period         = getattr(settings, "vb_bb_period",         20)
        self._kc_atr_mult       = getattr(settings, "vb_kc_atr_mult",       1.5)
        self._atr_period        = settings.atr_period
        self._atr_multiplier    = settings.atr_multiplier

        # Mínimo de velas necesarias
        self.min_candles = max(
            self._ema_period,
            self._bb_period * 2,
            self._atr_period * 2,
        ) + 30

        logger.info(
            "strategy.initialized",
            strategy=self.strategy_id,
            volume_threshold=self._volume_threshold,
            adx_threshold=self._adx_threshold,
            min_squeeze_bars=self._min_squeeze_bars,
            ema_period=self._ema_period,
        )

    # ─────────────────────────────────────────────────────────────────────────
    #  Interfaz pública
    # ─────────────────────────────────────────────────────────────────────────

    async def generate_signal(
        self,
        symbol: str,
        candles: list[Candle],
        ticker: Optional[Ticker] = None,
        position: Optional[Position] = None,
    ) -> Optional[Signal]:
        """
        Genera señal de breakout si el squeeze acaba de liberarse
        con confirmación de volumen, delta y ADX.
        """
        closed = [c for c in candles if c.is_closed]

        if not self.has_enough_data(closed):
            logger.debug(
                "strategy.insufficient_data",
                strategy=self.strategy_id,
                symbol=symbol,
                available=len(closed),
                required=self.min_candles,
            )
            return None

        opens, highs, lows, closes, volumes = self._to_arrays(closed)

        # Calcular todos los indicadores
        ind = self._calculate_indicators(opens, highs, lows, closes, volumes)

        # Detectar estado del squeeze
        squeeze_state = self._detect_squeeze_state(ind)

        # Evaluar señal
        return self._evaluate_signal(symbol, highs, lows, closes, ind, squeeze_state, closed[-1], position)

    # ─────────────────────────────────────────────────────────────────────────
    #  Cálculo de indicadores
    # ─────────────────────────────────────────────────────────────────────────

    def _calculate_indicators(
        self,
        opens: np.ndarray,
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray,
        volumes: np.ndarray,
    ) -> dict:
        """Calcula todos los indicadores necesarios, retorna dict con valores actuales."""

        # ── Squeeze Momentum ──────────────────────────────────────────────────
        momentum_arr, in_squeeze_arr, squeeze_release_arr = squeeze_momentum(
            highs, lows, closes,
            bb_period=self._bb_period,
            bb_std=2.0,
            kc_period=self._bb_period,
            kc_atr_period=self._atr_period,
            kc_atr_mult=self._kc_atr_mult,
        )

        # ── ADX ───────────────────────────────────────────────────────────────
        adx_arr, plus_di_arr, minus_di_arr = adx(highs, lows, closes, self._atr_period)

        # ── ATR ───────────────────────────────────────────────────────────────
        atr_arr = atr(highs, lows, closes, self._atr_period)

        # ── EMA de tendencia ─────────────────────────────────────────────────
        ema_arr = ema(closes, self._ema_period)

        # ── Delta de volumen rolling ──────────────────────────────────────────
        delta_arr = rolling_delta(opens, highs, lows, closes, volumes, window=self._atr_period)

        # ── Divergencia Delta/Precio ──────────────────────────────────────────
        delta_div_arr = delta_price_divergence(closes, delta_arr, window=self._bb_period)

        # ── OBV Divergencia ───────────────────────────────────────────────────
        obv_div_arr = obv_divergence(closes, volumes, window=self._bb_period)

        # ── Ratio de volumen ──────────────────────────────────────────────────
        vol_ratio_arr = rolling_volume_ratio(volumes, window=self._bb_period)

        # ── Bollinger y Keltner para cálculo de TP ───────────────────────────
        bb_upper_arr, _, bb_lower_arr = bollinger_bands(closes, self._bb_period, 2.0)
        kc_upper_arr, _, kc_lower_arr = keltner_channels(
            highs, lows, closes, self._bb_period, self._atr_period, self._kc_atr_mult
        )

        # Valores actuales (última y penúltima barra)
        return {
            # Arrays completos (necesarios para detectar squeeze_duration)
            "in_squeeze_arr":    in_squeeze_arr,
            "momentum_arr":      momentum_arr,
            # Valores escalares última barra
            "momentum":          momentum_arr[-1],
            "momentum_prev":     momentum_arr[-2] if len(momentum_arr) > 1 else np.nan,
            "in_squeeze":        bool(in_squeeze_arr[-1]),
            "in_squeeze_prev":   bool(in_squeeze_arr[-2]) if len(in_squeeze_arr) > 1 else False,
            "squeeze_release":   squeeze_release_arr[-1],
            "adx":               adx_arr[-1],
            "plus_di":           plus_di_arr[-1],
            "minus_di":          minus_di_arr[-1],
            "atr":               atr_arr[-1],
            "ema":               ema_arr[-1],
            "delta":             delta_arr[-1],
            "delta_divergence":  delta_div_arr[-1],
            "obv_divergence":    obv_div_arr[-1],
            "volume_ratio":      vol_ratio_arr[-1],
            "bb_upper":          bb_upper_arr[-1],
            "bb_lower":          bb_lower_arr[-1],
            "kc_upper":          kc_upper_arr[-1],
            "kc_lower":          kc_lower_arr[-1],
        }

    # ─────────────────────────────────────────────────────────────────────────
    #  Detección del estado del squeeze
    # ─────────────────────────────────────────────────────────────────────────

    def _detect_squeeze_state(self, ind: dict) -> SqueezeState:
        """
        Analiza el historial de squeeze y determina si acaba de dispararse.
        just_fired = True cuando la barra anterior estaba en squeeze y la actual no.
        """
        squeeze_arr = ind["in_squeeze_arr"]
        momentum    = ind["momentum"]
        mom_prev    = ind["momentum_prev"]

        # Duración del squeeze (cuántas barras seguidas estuvo comprimido)
        duration = 0
        for sq in reversed(squeeze_arr[:-1]):  # no incluir la barra actual
            if sq:
                duration += 1
            else:
                break

        just_fired = (
            ind["in_squeeze_prev"] and not ind["in_squeeze"]
            and duration >= self._min_squeeze_bars
        )

        momentum_rising = (
            not np.isnan(mom_prev) and not np.isnan(momentum)
            and abs(momentum) > abs(mom_prev)
        )

        return SqueezeState(
            in_squeeze=ind["in_squeeze"],
            just_fired=just_fired,
            momentum=float(momentum) if not np.isnan(momentum) else 0.0,
            momentum_rising=momentum_rising,
            momentum_prev=float(mom_prev) if not np.isnan(mom_prev) else 0.0,
            squeeze_duration=duration,
        )

    # ─────────────────────────────────────────────────────────────────────────
    #  Evaluación de señal
    # ─────────────────────────────────────────────────────────────────────────

    def _evaluate_signal(
        self,
        symbol: str,
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray,
        ind: dict,
        sq: SqueezeState,
        last_candle: Candle,
        position: Optional["Position"] = None,
    ) -> Optional[Signal]:
        """
        Evalúa las condiciones completas de entrada y salida.
        """
        current_price = Decimal(str(closes[-1]))
        atr_value     = ind["atr"]
        ema_value     = ind["ema"]
        adx_value     = ind["adx"]
        delta         = ind["delta"]
        delta_div     = ind["delta_divergence"]
        obv_div       = ind["obv_divergence"]
        volume_ratio  = ind["volume_ratio"]

        # Guard: valores NaN en indicadores críticos
        critical = [atr_value, ema_value, adx_value, delta]
        if any(np.isnan(v) for v in critical):
            logger.debug("strategy.nan_indicators", symbol=symbol, strategy=self.strategy_id)
            return None

        # ── Si está en squeeze: monitoreo, no señal ───────────────────────────
        if sq.in_squeeze:
            logger.debug(
                "strategy.in_squeeze",
                symbol=symbol,
                duration=sq.squeeze_duration,
                momentum=f"{sq.momentum:.4f}",
            )
            return None

        # ── SEÑAL DE LONG (breakout alcista) ──────────────────────────────────
        long_conditions = self._check_long_conditions(
            closes, current_price, ema_value, adx_value, delta, delta_div, obv_div,
            volume_ratio, sq
        )

        if long_conditions["valid"]:
            atr_dec   = Decimal(str(atr_value))
            stop_mult = Decimal(str(self._breakout_atr_mult * self._atr_multiplier))
            stop_loss = current_price - atr_dec * stop_mult

            # Take Profit: proyección del ancho del squeeze
            kc_width  = Decimal(str(ind["kc_upper"] - ind["kc_lower"]))
            take_profit = current_price + kc_width  # Proyección 1× el canal

            strength   = self._classify_strength(adx_value, sq, volume_ratio)
            confidence = self._calculate_confidence(long_conditions["scores"])

            signal = Signal(
                strategy_id=self.strategy_id,
                symbol=symbol,
                direction=SignalDirection.LONG,
                entry_price=current_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                strength=strength,
                confidence=confidence,
                metadata={
                    "trigger": "squeeze_breakout_long",
                    "squeeze_duration": sq.squeeze_duration,
                    "momentum": sq.momentum,
                    "adx": float(adx_value),
                    "delta": float(delta),
                    "delta_divergence": float(delta_div) if not np.isnan(delta_div) else None,
                    "obv_divergence": float(obv_div) if not np.isnan(obv_div) else None,
                    "volume_ratio": float(volume_ratio) if not np.isnan(volume_ratio) else None,
                    "ema": float(ema_value),
                    "atr": float(atr_value),
                    "kc_width": float(kc_width),
                    "scores": long_conditions["scores"],
                },
            )
            logger.info(
                "strategy.signal_generated",
                strategy=self.strategy_id,
                symbol=symbol,
                direction="LONG",
                squeeze_bars=sq.squeeze_duration,
                adx=f"{adx_value:.1f}",
                delta=f"{delta:.3f}",
                momentum=f"{sq.momentum:.4f}",
                entry=str(current_price),
                stop=str(stop_loss),
                tp=str(take_profit),
                confidence=f"{confidence:.3f}",
            )
            return signal

        # SHORT eliminado: Kraken SPOT no permite posiciones cortas.

        # ── SEÑAL DE SALIDA (agotamiento de tendencia) ─────────────────────────
        return self._check_exit_signal(
            symbol, current_price, adx_value, ind["adx"], sq, position
        )

    # ─────────────────────────────────────────────────────────────────────────
    #  Condiciones de entrada
    # ─────────────────────────────────────────────────────────────────────────

    def _check_long_conditions(
        self,
        closes: np.ndarray,
        price: Decimal,
        ema_val: float,
        adx_val: float,
        delta: float,
        delta_div: float,
        obv_div: float,
        vol_ratio: float,
        sq: SqueezeState,
    ) -> dict:
        """
        Verifica condiciones LONG. Retorna dict con 'valid' y 'scores' (ponderaciones).

        Condición PRINCIPAL (obligatoria):
          - Squeeze acaba de dispararse (just_fired) con al menos min_squeeze_bars.
          - Momentum positivo y creciente.

        Condiciones de CONFIRMACIÓN (al menos 3 de 4):
          - ADX > umbral (tendencia real).
          - Delta > volume_threshold (compradores agresivos).
          - Precio por encima de EMA de tendencia.
          - OBV divergencia no bajista.
        """
        scores: dict[str, float] = {}

        # ── CONDICIÓN PRINCIPAL ───────────────────────────────────────────────
        if not sq.just_fired:
            return {"valid": False, "scores": scores}
        if sq.momentum <= self._MOMENTUM_MIN_ABS:
            return {"valid": False, "scores": scores}
        if not sq.momentum_rising:
            return {"valid": False, "scores": scores}

        scores["squeeze_fired"] = 1.0  # peso 1.0

        # ── CONFIRMACIONES ────────────────────────────────────────────────────
        # 1. ADX
        if not np.isnan(adx_val):
            if adx_val >= self._ADX_STRONG_TREND:
                scores["adx"] = 1.0
            elif adx_val >= self._adx_threshold:
                scores["adx"] = 0.6
            else:
                scores["adx"] = 0.0
        else:
            scores["adx"] = 0.0

        # 2. Delta de volumen (compradores dominan la ruptura)
        if not np.isnan(delta):
            if delta >= self._volume_threshold:
                scores["delta"] = 1.0
            elif delta >= self._volume_threshold * 0.5:
                scores["delta"] = 0.5
            else:
                scores["delta"] = 0.0
        else:
            scores["delta"] = 0.0

        # 3. Precio vs EMA (breakout en tendencia alcista)
        price_f = float(price)
        if not np.isnan(ema_val):
            if price_f > ema_val:
                scores["ema_filter"] = 1.0
            elif price_f > ema_val * 0.998:  # 0.2% tolerancia
                scores["ema_filter"] = 0.4
            else:
                scores["ema_filter"] = 0.0
        else:
            scores["ema_filter"] = 0.5  # Neutral si no hay EMA

        # 4. OBV divergencia (no bajista)
        if not np.isnan(obv_div):
            if obv_div > self._OBV_NEUTRAL_BAND:
                scores["obv"] = 1.0
            elif obv_div > -self._OBV_NEUTRAL_BAND:
                scores["obv"] = 0.5  # Neutral
            else:
                scores["obv"] = 0.0  # Bajista: contra-señal
        else:
            scores["obv"] = 0.5

        # ── VALIDACIÓN: al menos 3 de 4 confirmaciones con score > 0.4 ────────
        confirmations = [v for k, v in scores.items() if k != "squeeze_fired"]
        strong_confirmations = sum(1 for v in confirmations if v >= 0.4)
        valid = strong_confirmations >= 2

        return {"valid": valid, "scores": scores}

    def _check_short_conditions(
        self,
        closes: np.ndarray,
        price: Decimal,
        ema_val: float,
        adx_val: float,
        delta: float,
        delta_div: float,
        obv_div: float,
        vol_ratio: float,
        sq: SqueezeState,
    ) -> dict:
        """
        Verifica condiciones SHORT. Espejo de _check_long_conditions.
        """
        scores: dict[str, float] = {}

        # ── CONDICIÓN PRINCIPAL ───────────────────────────────────────────────
        if not sq.just_fired:
            return {"valid": False, "scores": scores}
        if sq.momentum >= -self._MOMENTUM_MIN_ABS:
            return {"valid": False, "scores": scores}
        if not sq.momentum_rising:  # Rising en valor absoluto
            return {"valid": False, "scores": scores}

        scores["squeeze_fired"] = 1.0

        # ── CONFIRMACIONES ────────────────────────────────────────────────────
        # 1. ADX
        if not np.isnan(adx_val):
            if adx_val >= self._ADX_STRONG_TREND:
                scores["adx"] = 1.0
            elif adx_val >= self._adx_threshold:
                scores["adx"] = 0.6
            else:
                scores["adx"] = 0.0
        else:
            scores["adx"] = 0.0

        # 2. Delta de volumen (vendedores dominan)
        if not np.isnan(delta):
            if delta <= -self._volume_threshold:
                scores["delta"] = 1.0
            elif delta <= -self._volume_threshold * 0.5:
                scores["delta"] = 0.5
            else:
                scores["delta"] = 0.0
        else:
            scores["delta"] = 0.0

        # 3. Precio vs EMA (breakout en tendencia bajista)
        price_f = float(price)
        if not np.isnan(ema_val):
            if price_f < ema_val:
                scores["ema_filter"] = 1.0
            elif price_f < ema_val * 1.002:
                scores["ema_filter"] = 0.4
            else:
                scores["ema_filter"] = 0.0
        else:
            scores["ema_filter"] = 0.5

        # 4. OBV divergencia (no alcista)
        if not np.isnan(obv_div):
            if obv_div < -self._OBV_NEUTRAL_BAND:
                scores["obv"] = 1.0
            elif obv_div < self._OBV_NEUTRAL_BAND:
                scores["obv"] = 0.5
            else:
                scores["obv"] = 0.0
        else:
            scores["obv"] = 0.5

        confirmations = [v for k, v in scores.items() if k != "squeeze_fired"]
        strong_confirmations = sum(1 for v in confirmations if v >= 0.4)
        valid = strong_confirmations >= 3

        return {"valid": valid, "scores": scores}

    # ─────────────────────────────────────────────────────────────────────────
    #  Señal de salida por agotamiento
    # ─────────────────────────────────────────────────────────────────────────

    def _check_exit_signal(
        self,
        symbol: str,
        price: Decimal,
        adx_current: float,
        adx_prev: float,
        sq: SqueezeState,
        position: Optional["Position"] = None,
    ) -> Optional[Signal]:
        """Solo emite CLOSE_LONG si hay posición LONG abierta."""
        from models.order import PositionSide
        if position is None:
            return None
        if getattr(position, "side", None) != PositionSide.LONG:
            return None

        if (
            not np.isnan(sq.momentum) and not np.isnan(sq.momentum_prev)
            and sq.momentum_prev > 0
            and sq.momentum < 0
        ):
            logger.info(
                "strategy.exit_signal",
                strategy=self.strategy_id,
                symbol=symbol,
                reason="momentum_sign_change",
                momentum=f"{sq.momentum:.4f}",
            )
            return Signal(
                strategy_id=self.strategy_id,
                symbol=symbol,
                direction=SignalDirection.CLOSE_LONG,
                entry_price=price,
                stop_loss=price,
                confidence=0.75,
                metadata={"exit_reason": "momentum_sign_change"},
            )
        return None

    # ─────────────────────────────────────────────────────────────────────────
    #  Helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _classify_strength(
        self,
        adx_val: float,
        sq: SqueezeState,
        vol_ratio: float,
    ) -> SignalStrength:
        """Clasifica la fuerza de la señal."""
        strong_points = 0
        if adx_val >= self._ADX_STRONG_TREND:
            strong_points += 1
        if sq.squeeze_duration >= self._min_squeeze_bars * 2:
            strong_points += 1
        if not np.isnan(vol_ratio) and vol_ratio >= self._settings.vb_volume_threshold:
            strong_points += 1

        if strong_points >= 3:
            return SignalStrength.STRONG
        if strong_points >= 2:
            return SignalStrength.MODERATE
        return SignalStrength.WEAK

    def _calculate_confidence(self, scores: dict[str, float]) -> float:
        """
        Calcula confianza como media ponderada de los scores.
        squeeze_fired tiene peso 2x por ser condición obligatoria.
        """
        weights = {
            "squeeze_fired": 2.0,
            "adx":           1.0,
            "delta":         1.5,
            "ema_filter":    0.8,
            "obv":           0.7,
        }
        total_weight = 0.0
        total_score  = 0.0
        for key, score in scores.items():
            w = weights.get(key, 1.0)
            total_score  += score * w
            total_weight += w

        if total_weight == 0:
            return 0.0
        return round(min(total_score / total_weight, 1.0), 4)
