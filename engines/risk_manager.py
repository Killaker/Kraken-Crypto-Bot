"""
engines/risk_manager.py
Gestión de riesgo: position sizing, kill-switch, trailing stops.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from core.config import Settings
from core.exceptions import KillSwitchTriggered, RiskLimitExceeded
from core.logger import get_logger
from models.order import Position, PositionSide
from models.signal import Signal, SignalDirection
from utils.filters import apply_lot_size_filter, apply_notional_filter, calculate_position_size

logger = get_logger(__name__)


class SymbolFilters:
    """Filtros de precisión para un símbolo específico."""
    __slots__ = (
        "min_qty", "max_qty", "step_size",
        "min_price", "max_price", "tick_size",
        "min_notional",
    )

    def __init__(
        self,
        min_qty: Decimal,
        max_qty: Decimal,
        step_size: Decimal,
        min_price: Decimal,
        max_price: Decimal,
        tick_size: Decimal,
        min_notional: Decimal,
    ) -> None:
        self.min_qty = min_qty
        self.max_qty = max_qty
        self.step_size = step_size
        self.min_price = min_price
        self.max_price = max_price
        self.tick_size = tick_size
        self.min_notional = min_notional


class DailyPnLTracker:
    """
    Rastrea el P&L realizado del día para el kill-switch.
    Se resetea automáticamente a medianoche UTC.
    """

    def __init__(self) -> None:
        self._date: date = datetime.now(tz=timezone.utc).date()
        self._realized_pnl: Decimal = Decimal("0")  # solo informativo/logging
        self._peak_equity: Decimal = Decimal("0")
        self._last_equity: Decimal = Decimal("0")
        self._lock = asyncio.Lock()

    async def initialize(self, equity: Decimal) -> None:
        async with self._lock:
            self._peak_equity = equity
            self._last_equity = equity

    async def update(self, pnl_delta: Decimal, equity: Decimal) -> None:
        """
        `equity` DEBE ser el valor total de la cuenta en este instante:
        efectivo libre + valor a mercado de TODAS las posiciones abiertas
        restantes (ver RiskManager._mark_to_market_equity). NO el efectivo
        libre a secas.

        CORREGIDO (segunda capa del mismo problema): la vez anterior se
        igualaron pico y actual a "starting_balance + pnl realizado" para
        que dejaran de compararse magnitudes distintas (efectivo libre vs
        curva de PnL). Pero esa curva seguía sin ver el valor de las
        posiciones que YA estaban abiertas en el momento de fijar
        `starting_balance` (al iniciar el bot o en el rollover diario a
        medianoche) — ese capital, bloqueado en posiciones en ese
        instante, desaparecía del cálculo el resto del día aunque fuera
        dinero real. Con hasta 11 posiciones abiertas a la vez en este
        log, el importe excluido era una fracción grande de la cuenta, e
        infló el "drawdown" a más del 9% con pérdidas reales de apenas
        1-2% por operación — disparando el kill-switch sin motivo real.
        Ahora se usa el equity TOTAL mark-to-market directamente, que ya
        incluye tanto el efectivo libre como el valor de cada posición
        abierta a su precio actual: abrir, cerrar, o que llegue la
        medianoche nunca "pierde de vista" capital real.
        """
        async with self._lock:
            self._check_date_rollover(equity)
            self._realized_pnl += pnl_delta
            self._last_equity = equity
            if equity > self._peak_equity:
                self._peak_equity = equity

    def _check_date_rollover(self, equity: Decimal) -> None:
        today = datetime.now(tz=timezone.utc).date()
        if today != self._date:
            logger.info(
                "daily_pnl_tracker.day_rollover",
                old_date=str(self._date),
                new_date=str(today),
                daily_pnl=str(self._realized_pnl),
            )
            self._date = today
            self._realized_pnl = Decimal("0")
            self._peak_equity = equity
            self._last_equity = equity

    @property
    def daily_drawdown(self) -> float:
        """Drawdown desde el pico de equity (mark-to-market) del día."""
        if self._peak_equity == 0:
            return 0.0
        dd = self._peak_equity - self._last_equity
        return float(max(dd, Decimal("0")) / self._peak_equity)


class RiskManager:
    """
    Gestor de riesgo del bot.

    Responsabilidades:
    - Calcular el tamaño de posición basado en riesgo fijo (1-2% / trade).
    - Calcular stop loss basado en ATR.
    - Verificar límites de posiciones abiertas.
    - Monitorear drawdown diario y activar kill-switch.
    - Actualizar trailing stops en tiempo real.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._daily_tracker = DailyPnLTracker()
        self._symbol_filters: dict[str, SymbolFilters] = {}
        self._kill_switch_active = False
        self._open_positions: dict[str, Position] = {}  # key: f"{symbol}|{strategy_id}"
        self._pending_entries: set[str] = set()  # reservas de hueco mientras se abre (ver validate_signal)
        self._account_balance: Decimal = Decimal("0")
        self._lock = asyncio.Lock()
        # Pausa manual de nuevas entradas (botón del dashboard). A
        # diferencia del kill-switch, no bloquea nada por riesgo — es una
        # decisión consciente del usuario de no abrir MÁS posiciones
        # mientras revisa/ajusta el bot, sin tocar las ya abiertas (que
        # siguen gestionadas con normalidad: TP, stop, trailing, cierres
        # de convergencia).
        self._new_entries_paused = False
        # NUEVO: igual que pause_new_entries pero por moneda individual —
        # botón del dashboard para que el usuario decida, símbolo a
        # símbolo, en cuáles no quiere que se abran posiciones nuevas
        # (p.ej. tras ver una mala racha o una notificación de cambio de
        # tendencia bajista). No afecta a las posiciones ya abiertas en
        # ese símbolo, que se siguen gestionando con normalidad.
        self._disabled_symbols: set[str] = set()

    @property
    def new_entries_paused(self) -> bool:
        return self._new_entries_paused

    def pause_new_entries(self) -> None:
        self._new_entries_paused = True

    def resume_new_entries(self) -> None:
        self._new_entries_paused = False

    @property
    def disabled_symbols(self) -> list[str]:
        return sorted(self._disabled_symbols)

    def disable_symbol(self, symbol: str) -> None:
        self._disabled_symbols.add(symbol.upper())

    def enable_symbol(self, symbol: str) -> None:
        self._disabled_symbols.discard(symbol.upper())

    # ── Inicialización ────────────────────────────────────────────────────

    def _position_mark(self, p: Position) -> Decimal:
        px = p.current_price if p.current_price > 0 else p.entry_price
        return p.qty * px

    def _mark_to_market_equity(self, free_cash: Decimal) -> Decimal:
        """
        Equity TOTAL de la cuenta: efectivo libre + valor a precio actual
        de cada posición abierta. Es la magnitud correcta para medir
        drawdown — ver el comentario largo en DailyPnLTracker.update.
        """
        open_value = sum(
            (self._position_mark(p) for p in self._open_positions.values()),
            Decimal("0"),
        )
        return free_cash + open_value

    async def initialize(self, balance: Decimal, symbol_filters: dict[str, SymbolFilters]) -> None:
        """
        Debe llamarse al inicio con el balance actual y los filtros del exchange.
        """
        self._account_balance = balance
        self._symbol_filters = symbol_filters
        await self._daily_tracker.initialize(self._mark_to_market_equity(balance))
        logger.info(
            "risk_manager.initialized",
            balance=str(balance),
            symbols=list(symbol_filters.keys()),
        )

    async def update_balance(self, new_balance: Decimal, pnl_delta: Decimal = Decimal("0")) -> None:
        async with self._lock:
            self._account_balance = new_balance
            equity = self._mark_to_market_equity(new_balance)
        await self._daily_tracker.update(pnl_delta, equity)
        await self._check_kill_switch()

    async def reserve_capital(self, amount: Decimal) -> None:
        """
        [Sustituido por sync_balance] Se mantiene solo por compatibilidad;
        ver sync_balance para la sincronización real usada en apertura y
        cierre de posiciones.
        """
        async with self._lock:
            self._account_balance = max(self._account_balance - amount, Decimal("0"))

    async def sync_balance(self, balance: Decimal) -> None:
        """
        Sincroniza _account_balance (usado para dimensionar/cappear señales)
        con el balance real gestionado por execution_engine
        (_paper_balance en PAPER, o el balance reportado por el exchange
        en LIVE), tanto al ABRIR como al CERRAR una posición.

        CORREGIDO: _account_balance solo se actualizaba al CERRAR
        posiciones (sumando el PnL realizado vía update_balance), pero
        nunca se descontaba al ABRIR — quedaba desincronizado del wallet
        real. Con varias posiciones abiertas a la vez, risk_manager
        seguía calculando el tamaño/cap de notional de cada señal nueva
        como si casi todo el capital original siguiera libre, cuando en
        realidad estaba inmovilizado en las posiciones ya abiertas.
        Resultado visible en el log: el sizing pasaba, pero
        execution_engine rechazaba la apertura real por
        "insufficient_balance" — el bot dejaba de ejecutar conversiones
        nuevas en cuanto había 2-3 posiciones abiertas.

        Un primer intento de arreglo (reserve_capital, restando
        cost+fee en la apertura y sumando solo el pnl en el cierre) creaba
        una DOBLE resta: el cierre nunca compensaba de vuelta el capital
        reservado en la apertura, dejando el balance de risk_manager cada
        vez más bajo que el real. La solución correcta es no hacer
        aritmética de deltas en dos sitios distintos: risk_manager debe
        simplemente REFLEJAR el balance real que ya mantiene
        execution_engine, que es la única fuente de verdad del efectivo
        disponible. No toca el tracker de PnL diario (eso lo hace
        update_balance/close_position con el pnl_delta explícito) ni
        dispara el kill-switch por sí sola.
        """
        async with self._lock:
            self._account_balance = balance

    # ── Kill-switch ───────────────────────────────────────────────────────

    async def _check_kill_switch(self) -> None:
        """
        Activa el kill-switch si el drawdown diario supera el límite.

        NOTA: esta comprobación se dispara desde `update_balance`, que a su
        vez es invocado por `close_position`. Si el cierre que provoca el
        drawdown excesivo es precisamente el que activa el kill-switch, la
        posición YA se ha cerrado y el balance YA se ha actualizado antes de
        que se lance esta excepción — el kill-switch bloquea únicamente
        APERTURAS futuras (vía `validate_signal`), nunca deshace el cierre
        que lo disparó. El log "execution_error" que aparece justo después
        de un cierre es ese aviso, no un fallo del cierre en sí.
        """
        drawdown = self._daily_tracker.daily_drawdown
        limit = self._settings.max_daily_drawdown

        if drawdown >= limit and not self._kill_switch_active:
            self._kill_switch_active = True
            logger.critical(
                "risk_manager.kill_switch_triggered",
                drawdown=f"{drawdown:.2%}",
                limit=f"{limit:.2%}",
            )
            raise KillSwitchTriggered(drawdown, limit)

    @property
    def is_kill_switch_active(self) -> bool:
        return self._kill_switch_active

    async def reset_kill_switch(self) -> None:
        """
        Desactiva el kill-switch manualmente y reinicia el tracker de PnL
        diario tomando el balance actual como nuevo punto de partida/pico.

        Es una acción administrativa consciente: el kill-switch se activó
        porque el drawdown diario superó el límite configurado, así que
        resetearlo sin más contexto podría dejar que el bot siga operando
        en las mismas condiciones que causaron la activación. Se expone
        solo a través de una acción explícita del usuario (botón en el
        dashboard / endpoint dedicado), nunca automáticamente.
        """
        async with self._lock:
            self._kill_switch_active = False
            balance = self._account_balance
            equity = self._mark_to_market_equity(balance)
        await self._daily_tracker.initialize(equity)
        logger.warning(
            "risk_manager.kill_switch_reset",
            balance=str(balance),
        )

    # ── Validación de señal ────────────────────────────────────────────────

    # ── Claves de posición ───────────────────────────────────────────────
    # NUEVO: antes `_open_positions` usaba el símbolo como única clave,
    # así que el sistema físicamente no podía tener dos posiciones
    # abiertas en la misma moneda bajo estrategias distintas (la segunda
    # pisaba a la primera, o se rechazaba por "position_exists"). Ahora la
    # clave es compuesta (símbolo + estrategia): cada estrategia gestiona
    # su propia posición en una moneda de forma independiente del resto.
    @staticmethod
    def _pos_key(symbol: str, strategy_id: str) -> str:
        return f"{symbol}|{strategy_id}"

    async def validate_signal(self, signal: Signal) -> bool:
        """
        Valida si una señal puede convertirse en orden.
        Verifica:
        1. Kill-switch no activo (solo bloquea ENTRADAS).
        2. Máx. posiciones abiertas no superado.
        3. Símbolo no tiene ya posición abierta en la misma dirección.

        CORREGIDO: el kill-switch bloqueaba TODAS las señales, incluidas
        las de salida (CLOSE_LONG/CLOSE_SHORT). Eso significaba que, una
        vez activado, ninguna posición abierta podía cerrarse — ni por
        trailing stop ni por señal de estrategia — quedando atrapadas y
        expuestas al mercado indefinidamente, justo lo opuesto de lo que
        un kill-switch de protección de capital debe hacer. Ahora las
        señales de salida se evalúan ANTES del chequeo de kill-switch y
        siempre se permiten: el kill-switch solo impide abrir posiciones
        nuevas, nunca cerrar las existentes.
        """
        if not signal.is_entry:
            return True  # Las salidas siempre pasan, incluso con kill-switch activo

        if self._new_entries_paused:
            logger.info("risk_manager.signal_rejected.entries_paused", signal_id=signal.signal_id)
            return False

        if signal.symbol.upper() in self._disabled_symbols:
            logger.info(
                "risk_manager.signal_rejected.symbol_disabled",
                signal_id=signal.signal_id,
                symbol=signal.symbol,
            )
            return False

        if self._kill_switch_active:
            logger.warning("risk_manager.signal_rejected.kill_switch", signal_id=signal.signal_id)
            return False

        async with self._lock:
            open_count = len(self._open_positions)
            strat_id = signal.strategy_id
            strat_count = sum(
                1 for k in self._open_positions if k.endswith(f"|{strat_id}")
            ) + sum(
                1 for k in self._pending_entries if k.endswith(f"|{strat_id}")
            )
            deployed = sum(
                (self._position_mark(p) for p in self._open_positions.values()),
                Decimal("0"),
            )
            equity = self._mark_to_market_equity(self._account_balance)

        if open_count >= self._settings.max_open_positions:
            logger.warning(
                "risk_manager.signal_rejected.max_positions",
                open=open_count,
                max=self._settings.max_open_positions,
            )
            return False

        max_per_strat = self._settings.max_positions_per_strategy
        if strat_count >= max_per_strat:
            logger.info(
                "risk_manager.signal_rejected.max_per_strategy",
                strategy=strat_id,
                open=strat_count,
                max=max_per_strat,
            )
            return False

        max_deployed = equity * Decimal(str(self._settings.max_deployed_notional_pct))
        if equity > 0 and deployed >= max_deployed:
            logger.info(
                "risk_manager.signal_rejected.max_deployed",
                deployed=str(deployed),
                max_deployed=str(max_deployed),
                equity=str(equity),
            )
            return False

        # No duplicar posición en el mismo símbolo PARA LA MISMA
        # estrategia. Dos estrategias distintas SÍ pueden tener cada una
        # su propia posición abierta en la misma moneda simultáneamente.
        #
        # CORREGIDO: esta comprobación (`in self._open_positions`) se
        # hacía SIN el lock, y entre que pasaba y que la posición se
        # registraba de verdad en `register_position` (bastante más
        # adelante: calculate_order_qty, el chequeo de rentabilidad, y en
        # LIVE la propia orden al exchange — varios `await` de por medio)
        # pasaba tiempo de sobra para que UNA SEGUNDA señal para el mismo
        # símbolo+estrategia pasara también esta comprobación, porque la
        # primera aún no se había registrado. Las dos acababan abriendo, y
        # `register_position` (un simple `dict[key] = position`) pisaba
        # silenciosamente la primera con la segunda — el entry_price de la
        # posición "saltaba" sin que el usuario cerrara ni abriera nada,
        # porque en realidad pasó a ser otra posición distinta por debajo.
        # Ahora el hueco se reserva de forma atómica, bajo el lock, en el
        # mismo momento en que se comprueba que está libre — ninguna
        # segunda señal puede colarse en el intervalo.
        async with self._lock:
            key = self._pos_key(signal.symbol, signal.strategy_id)
            if key in self._open_positions or key in self._pending_entries:
                logger.warning(
                    "risk_manager.signal_rejected.position_exists",
                    symbol=signal.symbol,
                    strategy=signal.strategy_id,
                )
                return False
            self._pending_entries.add(key)

        return True

    async def release_pending_entry(self, symbol: str, strategy_id: str) -> None:
        """
        Libera la reserva hecha en validate_signal cuando la apertura se
        aborta después (qty inválida, no rentable, orden rechazada...) sin
        llegar a registrar la posición — si no se libera, ese símbolo+
        estrategia quedaría bloqueado para siempre creyendo que ya tiene
        una entrada en curso.
        """
        async with self._lock:
            self._pending_entries.discard(self._pos_key(symbol, strategy_id))

    # ── Cálculo de posición ───────────────────────────────────────────────



    async def calculate_order_qty(
        self,
        signal: Signal,
        atr_value: float,
    ) -> Optional[Decimal]:
        """
        Calcula la cantidad de la orden aplicando:
        1. Position sizing por riesgo fijo.
        2. Stop loss basado en ATR (si el signal usa ATR).
        3. Filtros LOT_SIZE y MIN_NOTIONAL del exchange.

        Retorna None si la orden no es válida.
        """
        if self._account_balance == 0:
            logger.error("risk_manager.zero_balance")
            return None

        symbol = signal.symbol
        filters = self._symbol_filters.get(symbol)
        if not filters:
            logger.error("risk_manager.no_filters", symbol=symbol)
            return None

        entry = signal.entry_price
        stop = signal.stop_loss

        # NUEVO: tamaño de posición escalado por la confianza de la señal
        # (signal.confidence, 0.0-1.0 — cada estrategia ya la calcula a
        # partir de la fuerza de su propia señal, p.ej. el z-score, pero
        # hasta ahora nunca se usaba: toda señal arriesgaba exactamente
        # risk_per_trade, tanto una desviación al límite del umbral como
        # una muy marcada). Ahora el riesgo arriesgado se escala
        # linealmente entre confidence_size_min_mult (confianza 0.0) y
        # confidence_size_max_mult (confianza 1.0) — más balance en las
        # señales con mayor probabilidad de acierto, menos en las débiles,
        # para maximizar ganancias y minimizar pérdidas sin tocar el
        # límite de riesgo por operación en sí (risk_per_trade sigue
        # siendo el 100% = confidence 1.0, no un suelo nuevo).
        confidence = max(0.0, min(1.0, float(signal.confidence)))
        size_mult = (
            self._settings.confidence_size_min_mult
            + confidence * (self._settings.confidence_size_max_mult - self._settings.confidence_size_min_mult)
        )
        scaled_risk_fraction = self._settings.risk_per_trade * size_mult

        # Calcular qty bruta (leverage parameter removed for spot)
        qty = calculate_position_size(
            account_balance=self._account_balance,
            risk_fraction=scaled_risk_fraction,
            entry_price=entry,
            stop_loss=stop,
        )

        if qty <= 0:
            return None

        # CORREGIDO: cuando el stop está muy cerca del entry (precio de
        # escala minúscula, ATR pequeño), qty = risk_amount/price_risk
        # puede explotar a un notional (qty*entry) muy superior al balance
        # total disponible — en LIVE el exchange lo rechazaría, pero en
        # PAPER se ejecutaba igualmente, gastando fees sobre un notional
        # ficticio varias veces mayor que el capital real (visto en log:
        # notional de 2.8 BTC con balance de 0.03 BTC). Se limita el
        # notional máximo a una fracción del balance disponible.
        max_notional = self._account_balance * Decimal(str(self._settings.max_position_notional_pct))
        notional = qty * entry
        if notional > max_notional and entry > 0:
            original_notional = notional
            qty = max_notional / entry
            notional = max_notional
            logger.warning(
                "risk_manager.qty_capped_by_notional",
                symbol=symbol,
                original_notional=str(original_notional),
                capped_notional=str(max_notional),
            )

        # No desplegar más del max_deployed_notional_pct del equity.
        equity = self._mark_to_market_equity(self._account_balance)
        deployed = sum(
            (self._position_mark(p) for p in self._open_positions.values()),
            Decimal("0"),
        )
        headroom = equity * Decimal(str(self._settings.max_deployed_notional_pct)) - deployed
        if headroom <= 0:
            logger.info(
                "risk_manager.signal_rejected.max_deployed",
                symbol=symbol,
                deployed=str(deployed),
                equity=str(equity),
            )
            return None
        if notional > headroom and entry > 0:
            qty = headroom / entry
            notional = headroom

        # Notional mínimo vs EQUITY, no vs cash residual — las migajas
        # (0.0005 BTC) solo pagan fees y diluyen el expectancy.
        min_viable_notional = equity * Decimal(str(self._settings.min_viable_notional_pct))
        if notional < min_viable_notional:
            logger.info(
                "risk_manager.signal_rejected.notional_too_small",
                symbol=symbol,
                notional=str(notional),
                min_viable=str(min_viable_notional),
            )
            return None

        if qty <= 0:
            return None

        # Aplicar LOT_SIZE filter
        adjusted_qty = apply_lot_size_filter(
            qty=qty,
            min_qty=filters.min_qty,
            max_qty=filters.max_qty,
            step_size=filters.step_size,
        )
        if adjusted_qty is None:
            return None

        # Verificar notional mínimo
        if not apply_notional_filter(adjusted_qty, entry, filters.min_notional):
            return None

        # Verificar que el riesgo real no supera el límite (doble check).
        # Usa scaled_risk_fraction (no el risk_per_trade base) — si no, una
        # señal de alta confianza, dimensionada a propósito por encima del
        # riesgo base, se rechazaría aquí mismo por "exceder" un límite
        # que ya no es el que se le aplicó.
        actual_risk = abs(entry - stop) * adjusted_qty
        max_risk = self._account_balance * Decimal(str(scaled_risk_fraction))
        if actual_risk > max_risk * Decimal("1.05"):  # 5% tolerancia por redondeo
            logger.warning(
                "risk_manager.risk_exceeded",
                actual=str(actual_risk),
                max=str(max_risk),
            )
            raise RiskLimitExceeded(
                f"Riesgo real {actual_risk} > máximo {max_risk}"
            )

        logger.info(
            "risk_manager.position_sized",
            symbol=symbol,
            qty=str(adjusted_qty),
            entry=str(entry),
            stop=str(stop),
            take_profit=str(getattr(signal, "take_profit", None)),
            risk_usdt=str(actual_risk),
            strategy=signal.strategy_id,
        )
        return adjusted_qty

    # ── Cálculo de Stop Loss ATR ───────────────────────────────────────────

    def calculate_atr_stop(
        self,
        direction: SignalDirection,
        entry_price: Decimal,
        atr_value: float,
    ) -> Decimal:
        """
        Calcula el stop loss basado en ATR.
        LONG: stop = entry - (ATR * multiplier)
        SHORT: stop = entry + (ATR * multiplier)
        """
        atr_distance = Decimal(str(atr_value)) * Decimal(str(self._settings.atr_multiplier))

        if direction == SignalDirection.LONG:
            return entry_price - atr_distance
        elif direction == SignalDirection.SHORT:
            return entry_price + atr_distance
        return entry_price

    def calculate_trailing_delta(self, atr_value: float) -> Decimal:
        """Calcula el delta del trailing stop en unidades de precio."""
        return Decimal(str(atr_value)) * Decimal(str(self._settings.atr_multiplier))

    # ── Gestión de posiciones ─────────────────────────────────────────────

    async def register_position(self, position: Position) -> None:
        async with self._lock:
            key = self._pos_key(position.symbol, position.strategy_id)
            self._open_positions[key] = position
            self._pending_entries.discard(key)
        logger.info(
            "risk_manager.position_registered",
            symbol=position.symbol,
            strategy=position.strategy_id,
        )

    async def close_position(
        self,
        symbol: str,
        strategy_id: str,
        realized_pnl: Decimal,
        new_balance: Optional[Decimal] = None,
    ) -> None:
        """
        `new_balance`: cuando execution_engine ya conoce el balance real
        tras el cierre (p.ej. `_paper_balance` tras sumar exit_notional -
        exit_fee), se pasa aquí directamente para mantener
        `_account_balance` sincronizado como espejo exacto del wallet real
        — ver sync_balance. Si no se pasa (compatibilidad / LIVE), se usa
        la fórmula anterior (balance actual + pnl realizado).
        """
        async with self._lock:
            self._open_positions.pop(self._pos_key(symbol, strategy_id), None)
        balance = new_balance if new_balance is not None else self._account_balance + realized_pnl
        await self.update_balance(balance, realized_pnl)
        logger.info(
            "risk_manager.position_closed",
            symbol=symbol,
            strategy=strategy_id,
            pnl=str(realized_pnl),
            balance_after=str(self._account_balance),
            daily_drawdown=f"{self._daily_tracker.daily_drawdown:.4%}",
        )

    async def update_trailing_stops(
        self, symbol: str, strategy_id: str, current_price: Decimal
    ) -> Optional[str]:
        """
        Actualiza el trailing stop de la posición abierta para `symbol`.
        Retorna el exit_reason ("trailing_stop_hit" / "take_profit_hit" /
        "max_hold_timeout") si la posición debe cerrarse, o None si sigue
        abierta.

        CORREGIDO: `Position.current_price` nunca se actualizaba en ningún
        punto del sistema, quedando fijo en 0 desde la apertura. Eso hacía
        que `pnl_pct` calculase siempre (0 - entry)/entry = -100%, tanto
        en el dashboard como en el health check — no eran pérdidas reales,
        era un campo de precio nunca refrescado. Como este método ya se
        invoca en cada tick de precio (desde StrategyEngine._on_ticker),
        es el punto natural para mantener current_price y unrealized_pnl
        al día para TODAS las posiciones abiertas, no solo la del trailing
        stop que se está evaluando.
        """
        async with self._lock:
            position = self._open_positions.get(self._pos_key(symbol, strategy_id))
            if not position:
                return None

            position.current_price = current_price
            position.unrealized_pnl = (current_price - position.entry_price) * position.qty
            position.updated_at = datetime.now(tz=timezone.utc)

            # DISYUNTOR DE CATÁSTROFE — se comprueba primero y SIEMPRE,
            # independientemente de lo que diga el stop/trailing de la
            # estrategia. No es negociable ni desactivable: si el precio
            # cae catastrophic_stop_pct por debajo de la entrada, se
            # cierra. Ver comentario en core/config.py.
            catastrophic_price = position.entry_price * (
                Decimal("1") - Decimal(str(self._settings.catastrophic_stop_pct))
                if position.side == PositionSide.LONG
                else Decimal("1") + Decimal(str(self._settings.catastrophic_stop_pct))
            )
            catastrophic_hit = (
                current_price <= catastrophic_price
                if position.side == PositionSide.LONG
                else current_price >= catastrophic_price
            )
            if catastrophic_hit:
                logger.critical(
                    "risk_manager.catastrophic_stop_hit",
                    symbol=symbol,
                    price=str(current_price),
                    entry=str(position.entry_price),
                    limit_pct=f"{self._settings.catastrophic_stop_pct * 100:.1f}",
                )
                return "catastrophic_stop_hit"

            moved = position.update_trailing_stop(current_price)
            if moved:
                logger.debug(
                    "risk_manager.trailing_stop_updated",
                    symbol=symbol,
                    new_stop=str(position.trailing_stop_price),
                )

            if position.is_stop_hit(current_price):
                logger.info(
                    "risk_manager.trailing_stop_hit",
                    symbol=symbol,
                    price=str(current_price),
                    stop=str(position.trailing_stop_price),
                )
                return "trailing_stop_hit"

            # CORREGIDO: position.take_profit se calculaba con cuidado en
            # cada estrategia (con margen de seguridad sobre comisiones)
            # pero NUNCA se comprobaba contra el precio — la única forma
            # de cerrar una posición era el stop (SL/trailing) o la señal
            # de convergencia estadística de la propia estrategia, que no
            # garantiza haber cubierto ya el coste del round-trip. Ahora
            # se comprueba también el TP real aquí, en el mismo tick que
            # ya revisa el stop, para que las posiciones puedan cerrarse
            # con el beneficio neto que la estrategia diseñó.
            if position.take_profit is not None and position.take_profit > 0:
                tp_hit = (
                    current_price >= position.take_profit
                    if position.side == PositionSide.LONG
                    else current_price <= position.take_profit
                )
                if tp_hit:
                    logger.info(
                        "risk_manager.take_profit_hit",
                        symbol=symbol,
                        price=str(current_price),
                        tp=str(position.take_profit),
                    )
                    return "take_profit_hit"

            # NUEVO: cierre por tiempo máximo. Antes una posición que ni
            # tocaba el stop ni convergía (p.ej. SYNTHETIC_ARB en SOL,
            # oscilando entre -0.4% y +0.1% durante más de 6 horas sin
            # llegar nunca al 1.47% requerido para la convergencia
            # estadística) se quedaba abierta indefinidamente, con capital
            # bloqueado, hasta que alguien la cerraba a mano. Pasado
            # max_position_hold_seconds se fuerza el cierre (bypassa la
            # compuerta de rentabilidad, igual que el stop o el TP) para
            # liberar capital y cortar la exposición muerta.
            #
            # OPTIMIZACIÓN: con el timeout fijo, ganadoras genuinas
            # también se cortaban a las 3h aunque siguieran subiendo hacia
            # su TP real (ej. DOT +1.81% forzada por timeout en vez de
            # dejarla llegar a su objetivo). Ahora, si al vencer el plazo
            # la posición YA va en positivo, se le da una prórroga (hasta
            # max_hold_extension_mult × el plazo base) para intentar
            # alcanzar el TP real — pero en CADA tick de la prórroga se
            # revisa: en cuanto el PnL deja de ser positivo se cierra al
            # instante (no se espera al final de la prórroga), y el
            # stop/trailing normal sigue activo en paralelo todo el
            # tiempo. Una posición plana o en pérdida se sigue cerrando
            # exactamente igual que antes, en el plazo base — la prórroga
            # nunca amplía la exposición a pérdida, solo el margen para
            # capturar más ganancia ya en curso.
            if self._settings.max_position_hold_seconds > 0 and position.opened_at:
                held = (datetime.now(tz=timezone.utc) - position.opened_at).total_seconds()
                base_limit = self._settings.max_position_hold_seconds
                if held >= base_limit:
                    pnl_pct = (
                        (current_price - position.entry_price) / position.entry_price
                        if position.side == PositionSide.LONG
                        else (position.entry_price - current_price) / position.entry_price
                    )
                    extended_limit = base_limit * self._settings.max_hold_extension_mult
                    still_winning = pnl_pct > 0 and held < extended_limit
                    if not still_winning:
                        logger.info(
                            "risk_manager.max_hold_timeout",
                            symbol=symbol,
                            held_seconds=f"{held:.0f}",
                            limit_seconds=base_limit,
                            pnl_pct=f"{float(pnl_pct) * 100:.3f}",
                        )
                        return "max_hold_timeout"

        return None

    def get_open_position(self, symbol: str, strategy_id: str) -> Optional[Position]:
        return self._open_positions.get(self._pos_key(symbol, strategy_id))

    def get_open_positions_for_symbol(self, symbol: str) -> list[Position]:
        """Todas las posiciones abiertas en `symbol`, de cualquier estrategia."""
        return [p for p in self._open_positions.values() if p.symbol == symbol]

    def get_all_positions(self) -> list[Position]:
        return list(self._open_positions.values())

    @property
    def daily_drawdown(self) -> float:
        return self._daily_tracker.daily_drawdown