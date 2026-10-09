"""
strategies/triangular_arb.py
Estrategia J — Triangular Arbitrage.

Generaliza a CROSS_CURRENCY_ARB (que solo compara TARGET contra UNA
referencia fija): aquí se recorren TODOS los demás símbolos trackeados
como posible tercer vértice del triángulo

    BRIDGE → TARGET → REF → BRIDGE

Como Kraken no siempre lista el par TARGET/REF directo, el cruce se
deriva implícitamente de los dos pares reales que sí existen:

    ratio(TARGET, REF) = precio(TARGET/BRIDGE) / precio(REF/BRIDGE)

Ese ratio es exactamente el tipo de cambio TARGET/REF implícito del
triángulo. Se mantiene su media y desviación típica móviles para CADA
posible REF, y se entra en el cruce cuya desviación (z-score) sea mayor
en magnitud y supere tri_min_zscore — es decir, de entre todos los
triángulos posibles con este TARGET, se opera el más desalineado, no
uno fijo.

LONG (TARGET):
  - Existe un REF cuyo ratio TARGET/REF está tri_min_zscore desviaciones
    típicas POR DEBAJO de su media móvil (TARGET infravalorado respecto
    a REF a través del bridge).
  - REF no está en caída libre propia (para no confundir contagio con
    ineficiencia).

EXIT:
  - El ratio vuelve a estar dentro de tri_exit_zscore desv. típicas de
    su media (el triángulo converge).
  - Stop Loss: ATR × atr_multiplier (igual que el resto de estrategias).
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

import numpy as np

from core.config import Settings
from core.logger import get_logger
from models.market_data import Candle, Ticker
from models.order import Position
from models.signal import Signal, SignalDirection, SignalStrength
from strategies.base_strategy import BaseStrategy
from utils.indicators import atr, ema

logger = get_logger(__name__)


class TriangularArbStrategy(BaseStrategy):
    strategy_id = "TRIANGULAR_ARB"

    _REF_TREND_EMA = 20

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lookback = settings.tri_lookback
        self._min_zscore = settings.tri_min_zscore
        self._exit_zscore = settings.tri_exit_zscore
        self._atr_period = settings.atr_period
        self._atr_multiplier = settings.atr_multiplier
        self._all_symbols = list(settings.trading_symbols)
        self._data_engine = None  # inyectado por StrategyEngine
        # NOTA: Position no tiene un campo `metadata` genérico para que la
        # estrategia recuerde qué REF escogió al abrir (no se toca
        # models/order.py para mantener este cambio autocontenido), así
        # que se guarda aquí, en memoria de la propia instancia de
        # estrategia, qué triángulo (ref_symbol) está operando cada
        # símbolo mientras la posición sigue abierta.
        self._open_refs: dict[str, str] = {}
        # NUEVO: cola de aperturas pendientes — la posición REAL solo se
        # registra cuando el ExecutionEngine confirma el fill. Si no se
        # filtrara, la propia señal de apertura (emitida con position=None)
        # volvería a evaluarse en la siguiente vela como si aún no hubiera
        # posición y abriría una segunda entrada encima de la primera.
        self._pending_open: set[str] = set()

        self.min_candles = max(self._lookback, self._atr_period, self._REF_TREND_EMA * 2) + 20

        logger.info(
            "strategy.initialized",
            strategy=self.strategy_id,
            lookback=self._lookback,
            min_zscore=self._min_zscore,
            universe=len(self._all_symbols),
        )

    def set_data_engine(self, data_engine) -> None:
        self._data_engine = data_engine

    def on_position_registered(self, symbol: str) -> None:
        """
        Callback invocado por StrategyEngine cuando el ExecutionEngine
        confirmó el fill de una entrada de esta estrategia. Libera la marca
        "apertura en vuelo" para que las siguientes velas evalúen solo la
        SALIDA (gestión del triángulo), no otra entrada encima.
        """
        self._pending_open.discard(symbol)

    def _infer_reference_for_position(
        self, symbol: str, target_closes: np.ndarray
    ) -> Optional[str]:
        """
        Reconstruye qué REF usar para gestionar una posición cuya apertura
        no se registró en `_open_refs` (p.ej. posición huérfana tras un
        reinicio del bot). Elige el candidato con mayor |z-score| válido,
        el mismo criterio que en la apertura.
        """
        best_ref: Optional[str] = None
        best_abs_z = 0.0
        for ref_symbol in self._all_symbols:
            if ref_symbol == symbol:
                continue
            z = self._ratio_zscore(symbol, ref_symbol, target_closes)
            if z is None:
                continue
            if abs(z) > best_abs_z:
                best_abs_z = abs(z)
                best_ref = ref_symbol
        return best_ref

    # ─────────────────────────────────────────────────────────────────

    async def generate_signal(
        self,
        symbol: str,
        candles: list[Candle],
        ticker: Optional[Ticker] = None,
        position: Optional[Position] = None,
    ) -> Optional[Signal]:
        if self._data_engine is None:
            return None

        closed = [c for c in candles if c.is_closed]
        if not self.has_enough_data(closed):
            return None

        n_target = min(len(closed), self._lookback + self._REF_TREND_EMA + 5)
        target_closes = np.array([float(c.close) for c in closed[-n_target:]], dtype=np.float64)

        _, highs, lows, closes_full, _ = self._to_arrays(closed)
        atr_vals = atr(highs, lows, closes_full, self._atr_period)
        atr_value = float(atr_vals[-1]) if not np.isnan(atr_vals[-1]) else 0.0
        current_price = Decimal(str(closes_full[-1]))

        # ── SALIDA: si ya hay posición, solo hace falta revisar SU propio
        # triángulo (guardado en metadata al abrir), no recorrer todos de
        # nuevo.
        if position and position.is_open:
            # La posición REAL ya existe → la apertura en vuelo (si aún
            # figuraba) quedó confirmada; liberarla para que los estados
            # internos de la estrategia queden coherentes.
            self._pending_open.discard(symbol)
            ref_symbol = self._open_refs.get(symbol)
            if not ref_symbol:
                # CORREGIDO: esta posición NO se abrió en esta sesión del
                # bot (p.ej. el bot se reinició con la posición ya abierta
                # y `self._open_refs` es memoria volátil). Sin saber qué
                # REF usó, era imposible evaluar su convergencia — la
                # posición quedaba sin gestión de salida por estrategia
                # hasta el timeout de 24h. Se reconstruye el REF óptimo
                # recorriendo los candidatos como haría en la apertura; si
                # ninguno da z-score, se sale por seguridad liberando el
                # capital (el stop/TP genéricos siguen siendo la red de
                # protección mientras tanto).
                ref_symbol = self._infer_reference_for_position(symbol, target_closes)
                if not ref_symbol:
                    return Signal(
                        strategy_id=self.strategy_id,
                        symbol=symbol,
                        direction=SignalDirection.CLOSE_LONG,
                        entry_price=current_price,
                        stop_loss=current_price,
                        confidence=0.5,
                        metadata={"exit_reason": "triangular_ref_unknown"},
                    )
                self._open_refs[symbol] = ref_symbol
            z = self._ratio_zscore(symbol, ref_symbol, target_closes)
            if abs(z) < self._exit_zscore:
                self._open_refs.pop(symbol, None)
                return Signal(
                    strategy_id=self.strategy_id,
                    symbol=symbol,
                    direction=SignalDirection.CLOSE_LONG,
                    entry_price=current_price,
                    stop_loss=current_price,
                    confidence=0.8,
                    metadata={"exit_reason": "triangular_converged", "zscore": z, "ref_symbol": ref_symbol},
                )
            return None

        # ── ENTRADA: recorrer todos los posibles REF y quedarse con el
        # cruce más desalineado (mayor |z-score|), no el primero que
        # cumpla el umbral — así el "triángulo" operado es siempre el
        # mejor disponible en ese instante, entre todos los posibles.
        # NUEVO: si ya hay una apertura EN VUELO para este símbolo (señal
        # emitida la vela anterior, aún sin fill confirmado), no volver a
        # emitir otra — evitar doble posición por la misma estrategia.
        if symbol in self._pending_open:
            return None
        best_ref: Optional[str] = None
        best_z = 0.0
        best_edge = 0.0  # unidades de precio TARGET (hacia la media del ratio)
        for ref_symbol in self._all_symbols:
            if ref_symbol == symbol:
                continue
            ref_candles = [c for c in self._data_engine.get_candles(ref_symbol) if c.is_closed]
            if len(ref_candles) < self.min_candles:
                continue
            n = min(len(target_closes), len(ref_candles))
            if n < self._lookback:
                continue
            ref_closes = np.array([float(c.close) for c in ref_candles[-n:]], dtype=np.float64)
            tgt_closes = target_closes[-n:]
            if np.any(ref_closes == 0):
                continue

            z = self._zscore_from_arrays(tgt_closes, ref_closes)
            if z is None:
                continue
            if abs(z) > abs(best_z):
                # Filtro de contagio: evitar "atrapar cuchillos" si REF
                # también está en caída fuerte propia — igual criterio que
                # CROSS_CURRENCY_ARB.
                ref_ema_arr = ema(ref_closes, min(self._REF_TREND_EMA, len(ref_closes) - 1))
                ref_trend_down = (
                    not np.isnan(ref_ema_arr[-1]) and ref_closes[-1] < ref_ema_arr[-1] * 0.98
                )
                if z < 0 and ref_trend_down:
                    continue
                best_z = z
                best_ref = ref_symbol
                ratio = tgt_closes / ref_closes
                mean_ratio = float(np.mean(ratio[-self._lookback:]))
                best_edge = mean_ratio * float(ref_closes[-1]) - float(current_price)

        if best_ref is None or abs(best_z) < self._min_zscore or best_z >= 0 or atr_value <= 0:
            # Solo se opera el lado LONG (TARGET infravalorado); best_z<0
            # requerido. Igual que el resto de estrategias direccionales
            # del bot — el SHORT no está habilitado en modo paper/LONG-only.
            return None

        # ELIMINADO: _is_global_best_target exigía que este símbolo fuera
        # el ÚNICO más infravalorado de los 11 trackeados — en la
        # práctica, como mucho UN símbolo podía abrir TRIANGULAR_ARB a la
        # vez en TODO el bot, aunque varios estuvieran genuinamente
        # sobrevendidos a la par (exactamente lo esperable en una caída
        # de mercado amplia). El problema que esto intentaba resolver
        # (8 LONGs vs ADA en el mismo segundo) era en realidad la misma
        # condición de carrera entre validate_signal y register_position
        # que ya se corrigió con la reserva atómica (_pending_entries en
        # RiskManager) — el duplicado por símbolo ya está cubierto ahí,
        # sin necesidad de este gate global que bloqueaba símbolos
        # distintos entre sí sin motivo.

        min_tp_dist = self._min_tp_distance(current_price)
        reversion_dist = Decimal(str(max(best_edge, 0.0)))
        # El z-score no es un % de precio. Si la reversión esperada a la
        # media del ratio no cubre comisiones+margen, inflar el TP solo
        # produce timeouts con pérdida de fees (8 de 9 en la 1ª sesión).
        if reversion_dist < min_tp_dist:
            logger.info(
                "strategy.signal_skipped.edge_below_cost",
                strategy=self.strategy_id,
                symbol=symbol,
                ref=best_ref,
                zscore=f"{best_z:.3f}",
                edge_pct=f"{float(reversion_dist / current_price) * 100:.3f}" if current_price else "0",
                min_pct=f"{float(min_tp_dist / current_price) * 100:.3f}" if current_price else "0",
            )
            return None

        atr_dec = Decimal(str(atr_value))
        stop_loss = current_price - atr_dec * Decimal(str(self._atr_multiplier))
        take_profit = current_price + reversion_dist

        confidence = min(abs(best_z) / (self._min_zscore * 2.0), 1.0)
        strength = (
            SignalStrength.STRONG if abs(best_z) >= self._min_zscore * 1.5
            else SignalStrength.MODERATE
        )

        self._open_refs[symbol] = best_ref
        self._pending_open.add(symbol)
        signal = Signal(
            strategy_id=self.strategy_id,
            symbol=symbol,
            direction=SignalDirection.LONG,
            entry_price=current_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            strength=strength,
            confidence=round(confidence, 4),
            metadata={"ref_symbol": best_ref, "zscore": best_z},
        )
        logger.info(
            "strategy.signal_generated",
            strategy=self.strategy_id,
            symbol=symbol,
            direction="LONG",
            ref=best_ref,
            zscore=f"{best_z:.3f}",
            entry=str(current_price),
            stop=str(stop_loss),
            tp=str(take_profit),
        )
        return signal

    # ─────────────────────────────────────────────────────────────────

    def _zscore_from_arrays(
        self, target_closes: np.ndarray, ref_closes: np.ndarray
    ) -> Optional[float]:
        ratio = target_closes / ref_closes
        window = ratio[-self._lookback:]
        std = float(np.std(window))
        if std == 0:
            return None
        mean = float(np.mean(window))
        return (float(ratio[-1]) - mean) / std

    def _best_z_for_symbol(self, symbol: str) -> Optional[float]:
        closed = [c for c in self._data_engine.get_candles(symbol) if c.is_closed]
        if len(closed) < self._lookback:
            return None
        target_closes = np.array(
            [float(c.close) for c in closed[-(self._lookback + self._REF_TREND_EMA + 5):]],
            dtype=np.float64,
        )
        best_z: Optional[float] = None
        for ref_symbol in self._all_symbols:
            if ref_symbol == symbol:
                continue
            z = self._ratio_zscore(symbol, ref_symbol, target_closes)
            if z is None:
                continue
            if best_z is None or abs(z) > abs(best_z):
                best_z = z
        return best_z

    def _is_global_best_target(self, symbol: str, z: float) -> bool:
        """True si ningún otro TARGET está más infravalorado (z más negativo)."""
        for other in self._all_symbols:
            if other == symbol:
                continue
            other_z = self._best_z_for_symbol(other)
            if other_z is not None and other_z < 0 and other_z < z:
                return False
        return True

    def _ratio_zscore(
        self, symbol: str, ref_symbol: str, target_closes: np.ndarray
    ) -> Optional[float]:
        ref_candles = [c for c in self._data_engine.get_candles(ref_symbol) if c.is_closed]
        if len(ref_candles) < self._lookback:
            return None
        n = min(len(target_closes), len(ref_candles))
        ref_closes = np.array([float(c.close) for c in ref_candles[-n:]], dtype=np.float64)
        if np.any(ref_closes == 0):
            return None
        return self._zscore_from_arrays(target_closes[-n:], ref_closes)
