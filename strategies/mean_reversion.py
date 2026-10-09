"""
strategies/mean_reversion.py
Estrategia A — Mean Reversion: VWAP Z-Score + Bollinger Bands.

Lógica de trading:
══════════════════
LONG (compra):
  - Z-Score < -threshold (precio sobreventa vs VWAP)
  - Precio toca o cruza la banda inferior de Bollinger (%B < 0.05)
  - Volumen no es excepcionalmente bajo (sin sequía de liquidez)

SHORT (venta):
  - Z-Score > +threshold (precio sobrecompra vs VWAP)
  - Precio toca o cruza la banda superior de Bollinger (%B > 0.95)
  - Volumen no es excepcionalmente bajo

EXIT:
  - Z-Score regresa al rango [-0.5, +0.5] (reversión completada)
  - Stop Loss: ATR × multiplier (se calcula externamente en RiskManager)
  - Take Profit: VWAP (media de retorno)
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

import numpy as np

from core.config import Settings
from core.exceptions import InsufficientDataError
from core.logger import get_logger
from models.market_data import Candle, Ticker
from models.order import Position
from models.signal import Signal, SignalDirection, SignalStrength
from strategies.base_strategy import BaseStrategy
from utils.indicators import (
    atr,
    bb_percent_b,
    bollinger_bands,
    rolling_volume_ratio,
    vwap,
    vwap_zscore,
)

logger = get_logger(__name__)


class MeanReversionStrategy(BaseStrategy):
    """
    Estrategia de Mean Reversion basada en VWAP Z-Score y Bollinger Bands.

    Parámetros configurables via Settings:
    - mr_vwap_window:        Ventana rolling del VWAP.
    - mr_zscore_threshold:   Umbral Z-Score para señal (default 2.0).
    - mr_bb_period:          Período de las Bollinger Bands.
    - mr_bb_std:             Desviaciones estándar de las bandas.
    """

    strategy_id = "MEAN_REVERSION"

    # Umbral de %B para confirmar que el precio está en la banda
    _BB_LOWER_THRESHOLD = 0.15  # %B < 0.15 => cerca de la banda inferior
    _BB_UPPER_THRESHOLD = 0.85  # %B > 0.85 => cerca de la banda superior

    # Z-Score de salida (reversión suficiente)
    _EXIT_ZSCORE = 0.5

    # Volumen mínimo como ratio del promedio (evitar señales en mercado muerto)
    _MIN_VOLUME_RATIO = 0.1

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._vwap_window = settings.mr_vwap_window
        self._zscore_threshold = settings.mr_zscore_threshold
        self._bb_period = settings.mr_bb_period
        self._bb_std = settings.mr_bb_std
        self._atr_period = settings.atr_period
        self._atr_multiplier = settings.atr_multiplier

        # Mínimo de velas = max de todos los períodos + margen
        self.min_candles = max(self._vwap_window, self._bb_period, self._atr_period) + 20

        logger.info(
            "strategy.initialized",
            strategy=self.strategy_id,
            vwap_window=self._vwap_window,
            zscore_threshold=self._zscore_threshold,
            bb_period=self._bb_period,
            bb_std=self._bb_std,
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
        Analiza los últimos candles y retorna una señal de trading o None.
        Utiliza solo velas cerradas (is_closed=True).
        """
        # Filtrar solo velas cerradas
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

        # Extraer arrays OHLCV
        _, highs, lows, closes, volumes = self._to_arrays(closed)

        # Calcular indicadores
        indicators = self._calculate_indicators(highs, lows, closes, volumes)

        # Analizar señal
        return self._evaluate_signal(symbol, closes, indicators, closed[-1], position)

    # ─────────────────────────────────────────────────────────────────────────
    #  Cálculo de indicadores
    # ─────────────────────────────────────────────────────────────────────────

    def _calculate_indicators(
        self,
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray,
        volumes: np.ndarray,
    ) -> dict:
        """
        Calcula todos los indicadores necesarios.
        Retorna dict con los valores actuales (última barra).
        """
        # VWAP rolling
        vwap_vals = vwap(highs, lows, closes, volumes, window=self._vwap_window)

        # Z-Score de desvío precio vs VWAP
        zscore_vals = vwap_zscore(closes, vwap_vals, window=self._vwap_window)

        # Bollinger Bands
        bb_upper, bb_middle, bb_lower = bollinger_bands(
            closes, period=self._bb_period, std_dev=self._bb_std
        )
        percent_b = bb_percent_b(closes, bb_upper, bb_lower)

        # ATR para stop loss
        atr_vals = atr(highs, lows, closes, period=self._atr_period)

        # Ratio de volumen (normalizado vs promedio)
        vol_ratio = rolling_volume_ratio(volumes, window=self._vwap_window)

        # Valores actuales (última barra, índice -1)
        current_idx = -1

        return {
            "vwap": vwap_vals[current_idx],
            "zscore": zscore_vals[current_idx],
            "bb_upper": bb_upper[current_idx],
            "bb_middle": bb_middle[current_idx],
            "bb_lower": bb_lower[current_idx],
            "percent_b": percent_b[current_idx],
            "atr": atr_vals[current_idx],
            "volume_ratio": vol_ratio[current_idx],
            # Prev zscore para detectar cruce
            "prev_zscore": zscore_vals[-2] if len(zscore_vals) > 1 else np.nan,
        }

    # ─────────────────────────────────────────────────────────────────────────
    #  Evaluación de señal
    # ─────────────────────────────────────────────────────────────────────────

    def _evaluate_signal(
        self,
        symbol: str,
        closes: np.ndarray,
        ind: dict,
        last_candle: Candle,
        position: Optional[Position] = None,
    ) -> Optional[Signal]:
        """
        Evalúa las condiciones de entrada/salida y retorna un Signal o None.
        """
        zscore = ind["zscore"]
        percent_b = ind["percent_b"]
        atr_value = ind["atr"]
        vwap_price = ind["vwap"]
        volume_ratio = ind["volume_ratio"]
        prev_zscore = ind["prev_zscore"]

        # Guard: valores no válidos (NaN al inicio de la serie)
        if any(np.isnan(v) for v in [zscore, percent_b, atr_value, vwap_price]):
            logger.debug("strategy.nan_indicators", symbol=symbol)
            return None

        # Guard: volumen mínimo (evitar señales en mercados illíquidos).
        # AJUSTADO: tras intentar reconstruir el volumen en tiempo real
        # desde el polling REST (vol_24h del ticker), la lectura sigue
        # saliendo en 0.00 exacto ~95% de las veces — más vale asumir que
        # es un dato no fiable (limitación de leer por REST en vez de un
        # stream de trades) que una ausencia real de liquidez: ningún par
        # líquido de Kraken tiene volumen genuinamente CERO en el 95% de
        # sus ventanas de 5 min. Ahora solo bloquea una lectura BAJA pero
        # medida (0 < ratio < mínimo) — un 0.00 exacto ya no cuenta.
        if not np.isnan(volume_ratio) and 0 < volume_ratio < self._MIN_VOLUME_RATIO:
            logger.debug("strategy.low_volume", symbol=symbol, ratio=f"{volume_ratio:.2f}")
            return None

        current_price = Decimal(str(closes[-1]))
        atr_dec = Decimal(str(atr_value))
        vwap_dec = Decimal(str(vwap_price))
        atr_stop_dist = atr_dec * Decimal(str(self._atr_multiplier))

        # ── SEÑAL LONG ───────────────────────────────────────────────────────
        if (
            zscore < -self._zscore_threshold
            and percent_b < self._BB_LOWER_THRESHOLD
        ):
            # CORREGIDO: el stop a 2×ATR quedaba dentro del rango normal de
            # oscilación de la banda de Bollinger inferior — precisamente
            # la zona donde la propia señal de entrada espera que el
            # precio esté rebotando antes de revertir. Resultado real en
            # el log: DOT y XRP saltaron el stop por simple ruido dentro
            # de la banda, antes de que la reversión tuviera oportunidad
            # de completarse. Ahora el stop de MEAN_REVERSION usa la banda
            # inferior (menos un pequeño colchón de ATR) cuando queda más
            # lejos que el stop ATR normal, dando margen real para oscilar
            # dentro del rango estadístico esperado. Solo cierra si el
            # precio rompe claramente por debajo de la banda — señal de
            # que la reversión ha fallado de verdad — en vez de por un
            # simple toque dentro del rango normal. Nunca se ELIMINA el
            # stop: quitar la protección de riesgo dejaría la posición
            # expuesta a una caída sin límite si el precio sigue
            # cayendo en vez de revertir.
            bb_lower_dec = Decimal(str(ind["bb_lower"]))
            band_based_stop = bb_lower_dec - atr_dec * Decimal("0.5")
            atr_based_stop = current_price - atr_stop_dist
            stop_loss = min(atr_based_stop, band_based_stop)

            # CORREGIDO: usar el VWAP crudo como TP podía dar una distancia
            # menor que el coste de comisiones+slippage del two-hop (4 legs),
            # sobre todo en mercado lateral con VWAP muy pegado al precio.
            # Se exige que el TP cubra al menos el coste mínimo requerido;
            # si el VWAP no llega, se proyecta esa distancia mínima.
            min_tp_dist = self._min_tp_distance(current_price)
            vwap_dist = vwap_dec - current_price
            # Si el VWAP (el edge real de la señal) no cubre costes, no
            # abrir: inflar el TP por encima del VWAP es pedir un
            # overshoot que la señal no promete.
            if vwap_dist < min_tp_dist:
                return None
            take_profit = current_price + vwap_dist

            # Si ni siquiera hay margen entre precio y VWAP (reversión muy
            # débil) y el TP forzado queda por debajo del stop, descartar.
            if take_profit <= current_price:
                return None

            # Fuerza de la señal
            strength = self._classify_strength(abs(zscore))
            confidence = self._calculate_confidence(zscore, percent_b, volume_ratio, "LONG")

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
                    "zscore": float(zscore),
                    "percent_b": float(percent_b),
                    "atr": float(atr_value),
                    "vwap": float(vwap_price),
                    "volume_ratio": float(volume_ratio) if not np.isnan(volume_ratio) else None,
                    "bb_lower": ind["bb_lower"],
                    "bb_upper": ind["bb_upper"],
                },
            )
            logger.info(
                "strategy.signal_generated",
                strategy=self.strategy_id,
                symbol=symbol,
                direction="LONG",
                zscore=f"{zscore:.3f}",
                percent_b=f"{percent_b:.3f}",
                entry=str(current_price),
                stop=str(stop_loss),
                tp=str(take_profit),
            )
            return signal

        # SHORT eliminado: Kraken SPOT no permite posiciones cortas.

        # ── SEÑAL DE SALIDA ───────────────────────────────────────────────────
        # Detectamos cruce del Z-Score de vuelta a la zona neutra.
        # CORREGIDO: antes no comprobaba si había posición abierta, así que
        # emitía CLOSE_LONG en cada vela mientras el z-score se mantenía en
        # zona neutra, inundando la cola de señales con cierres duplicados
        # (visible en el log como el mismo CLOSE_LONG repetido cada pocos
        # segundos para el mismo símbolo).
        if position is not None and position.is_open and not np.isnan(prev_zscore):
            if (
                prev_zscore < -self._EXIT_ZSCORE
                and zscore >= -self._EXIT_ZSCORE
            ):
                # Precio ha revertido: cerrar LONG
                return Signal(
                    strategy_id=self.strategy_id,
                    symbol=symbol,
                    direction=SignalDirection.CLOSE_LONG,
                    entry_price=current_price,
                    stop_loss=current_price,  # No relevante para cierre
                    confidence=0.8,
                    metadata={"exit_reason": "zscore_reversion", "zscore": float(zscore)},
                )
            # CLOSE_SHORT eliminado: no hay posiciones cortas en SPOT.

        return None

    # ─────────────────────────────────────────────────────────────────────────
    #  Helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _classify_strength(self, abs_zscore: float) -> SignalStrength:
        """Clasifica la fuerza de la señal según el Z-Score."""
        if abs_zscore >= self._zscore_threshold * 1.5:
            return SignalStrength.STRONG
        if abs_zscore >= self._zscore_threshold * 1.1:
            return SignalStrength.MODERATE
        return SignalStrength.WEAK

    def _calculate_confidence(
        self,
        zscore: float,
        percent_b: float,
        volume_ratio: float,
        direction: str,
    ) -> float:
        """
        Calcula una puntuación de confianza [0.0, 1.0] ponderando
        la intensidad de los tres indicadores.
        """
        # Componente Z-Score (0.0 - 0.5): más extremo → más confianza
        z_component = min(abs(zscore) / (self._zscore_threshold * 2.0), 0.5)

        # Componente %B (0.0 - 0.3): más alejado del centro → más confianza
        if direction == "LONG":
            bb_component = (1.0 - percent_b) * 0.3
        else:
            bb_component = percent_b * 0.3

        # Componente Volumen (0.0 - 0.2): más volumen → más confianza
        if np.isnan(volume_ratio):
            vol_component = 0.1
        else:
            vol_component = min((volume_ratio - 1.0) / 4.0, 0.2) if volume_ratio > 1.0 else 0.0

        confidence = z_component + bb_component + vol_component
        return round(min(max(confidence, 0.0), 1.0), 4)
