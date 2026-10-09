"""
engines/execution_engine.py
Motor de ejecución para Kraken SPOT — conversión directa BTC ↔ TARGET.

Operativa:
  Entrada: comprar TARGET/BTC (1 orden de mercado)
  Salida:  vender TARGET/BTC (1 orden de mercado)

Sin salto intermedio (two-hop eliminado). BTC es simultáneamente el
activo base en reposo y el activo cotización de todos los pares target.

- SHORT/CLOSE_SHORT filtrados (SPOT no permite cortos).
- Validación de rentabilidad neta directa ANTES de ejecutar.
- TIF/timeout por orden: si no se rellena en ORDER_TIMEOUT_S, aborta.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import ccxt.async_support as ccxt

from core.config import Settings
from core.logger import get_logger
from engines.risk_manager import RiskManager, SymbolFilters
from models.order import Order, OrderSide, OrderStatus, OrderType, Position, PositionSide
from models.signal import Signal, SignalDirection

logger = get_logger(__name__)

# Segundos máximos esperando que la orden se rellene antes de abortar
ORDER_TIMEOUT_S: int = 10
# Intervalo de polling para comprobar estado de la orden
ORDER_POLL_INTERVAL_S: float = 0.5

# Señales que implican SHORT en SPOT (no ejecutables)
_SHORT_DIRECTIONS = {SignalDirection.SHORT, SignalDirection.CLOSE_SHORT}


class DirectOrderAborted(Exception):
    """Se lanza cuando la orden directa BTC↔TARGET se aborta."""


class ExecutionEngine:
    """
    Ejecuta señales mediante conversión directa BTC ↔ TARGET en Kraken SPOT.

    Entrada: comprar TARGET/BTC  (1 orden de mercado)
    Salida:  vender TARGET/BTC  (1 orden de mercado)
    """

    # Motivos de cierre que representan un precio ya alcanzado (stop de
    # riesgo real o take-profit ya vetado por _is_direct_profitable) y
    # que por tanto deben ejecutarse SIEMPRE, sin la compuerta de
    # rentabilidad que sí aplica a los cierres por convergencia
    # estadística de las estrategias (zscore_reversion, overweight, etc.).
    # "dip_rebound_target": reservado por si DIP_REBOUND emite en el futuro
    # su propio exit_reason explícito; hoy su TP ya se cierra vía
    # "take_profit_hit" (chequeo genérico en RiskManager.update_trailing_stops).
    _PRICE_EXIT_REASONS = frozenset({
        "trailing_stop_hit", "take_profit_hit", "manual_close",
        "max_hold_timeout", "dip_rebound_target", "catastrophic_stop_hit",
    })

    def __init__(self, settings: Settings, risk_manager: RiskManager) -> None:
        self._settings = settings
        self._risk_manager = risk_manager
        self._exchange: Optional[ccxt.kraken] = None
        # Balance en BTC — activo base de reposo y cotización de todos los pares.
        self._paper_balance: Decimal = Decimal("0.03")  # ~2700€ en BTC
        self._live_prices: dict[str, Decimal] = {}
        self._paper_positions: dict[str, Decimal] = {}
        self._data_engine = None

    # ── Inicialización ────────────────────────────────────────────────

    async def initialize(self) -> Decimal:
        if self._settings.is_live:
            self._exchange = self._build_exchange()
            await self._exchange.load_markets()
            balance_data = await self._exchange.fetch_balance()
            base = self._settings.convert_base_asset
            bal = Decimal(str(balance_data["total"].get(base, 0)))
            logger.info("execution_engine.initialized_live", balance=str(bal), asset=base)
            return bal
        else:
            logger.info("execution_engine.initialized_paper", balance=str(self._paper_balance), unit="BTC")
            return self._paper_balance

    def _build_exchange(self) -> ccxt.kraken:
        return ccxt.kraken({
            "apiKey": self._settings.kraken_api_key,
            "secret": self._settings.kraken_api_secret,
            "enableRateLimit": True,
            "timeout": self._settings.rest_timeout * 1000,
        })

    def _mock_symbol_filters(self) -> dict[str, SymbolFilters]:
        filters = {}
        for symbol in self._settings.trading_symbols:
            filters[symbol] = SymbolFilters(
                min_qty=Decimal("0.0001"),
                max_qty=Decimal("1000000"),
                step_size=Decimal("0.0001"),
                min_price=Decimal("0.000001"),
                max_price=Decimal("9999999"),
                tick_size=Decimal("0.000001"),
                min_notional=Decimal("0"),
            )
        return filters

    # ── Precio en tiempo real ─────────────────────────────────────────

    def update_price(self, symbol: str, price: Decimal) -> None:
        self._live_prices[symbol] = price

    def set_data_engine(self, data_engine) -> None:
        """Inyecta DataEngine para leer BTC/EUR al abrir y congelar entry_eur."""
        self._data_engine = data_engine

    def _snapshot_entry_eur(self, entry_price: Decimal) -> tuple[Optional[Decimal], Optional[Decimal]]:
        """
        Congela el precio de la cripto en EUR en el instante de apertura.
        Retorna (entry_eur, btc_eur_rate). Si aún no hay tasa, (None, None)
        — el dashboard lo rellenará con el primer BTC/EUR disponible y
        lo dejará fijo a partir de ahí.
        """
        rate = None
        if self._data_engine is not None:
            rate = self._data_engine.get_btc_eur_rate()
        if rate is None or rate <= 0 or entry_price <= 0:
            return None, None
        return entry_price * rate, rate

    # ── Proceso de señal ──────────────────────────────────────────────

    async def process_signal(self, signal: Signal, atr_value: float) -> Optional[Order]:
        # Filtrar SHORT en SPOT — no ejecutar ni loguear como error
        if signal.direction in _SHORT_DIRECTIONS:
            logger.debug(
                "execution_engine.short_skipped_spot",
                direction=signal.direction.value,
                symbol=signal.symbol,
            )
            return None

        validated = await self._risk_manager.validate_signal(signal)
        if not validated:
            return None

        if signal.direction in (SignalDirection.CLOSE_LONG,):
            return await self._close_position(signal)

        if signal.direction == SignalDirection.LONG:
            return await self._open_long(signal, atr_value)

        return None

    # ── Apertura de posición (LONG directo BTC→TARGET) ────────────────

    async def _open_long(self, signal: Signal, atr_value: float) -> Optional[Order]:
        symbol = signal.symbol
        ticker = None
        if self._data_engine is not None:
            ticker = self._data_engine.get_ticker(symbol)

        # Entrada a mercado: el fill real de un BUY es el ASK, no el last.
        # Usar last inflaba el paper PnL (compras "mejor" que el libro).
        if ticker is not None and ticker.ask and ticker.ask > 0:
            entry_price = ticker.ask
        else:
            entry_price = self._live_prices.get(symbol, signal.entry_price)

        if ticker is not None and ticker.bid > 0 and ticker.ask > 0:
            spread_pct = (ticker.ask - ticker.bid) / ticker.bid
            max_spread = Decimal(str(self._settings.max_entry_spread_pct))
            if spread_pct > max_spread:
                logger.info(
                    "execution_engine.signal_rejected.wide_spread",
                    symbol=symbol,
                    spread_pct=f"{float(spread_pct) * 100:.3f}",
                    max_pct=f"{float(max_spread) * 100:.3f}",
                )
                await self._risk_manager.release_pending_entry(symbol, signal.strategy_id)
                return None

        # Re-anclar SL/TP a la distancia original respecto al fill (ask),
        # no al close de la vela: si el ask está por encima, el TP se
        # acorta y el stop se aleja — peor R:R — si no se desplazan.
        if signal.entry_price and signal.entry_price > 0 and entry_price != signal.entry_price:
            delta = entry_price - signal.entry_price
            if signal.stop_loss and signal.stop_loss > 0:
                signal.stop_loss = signal.stop_loss + delta
            if signal.take_profit and signal.take_profit > 0:
                signal.take_profit = signal.take_profit + delta
            signal.entry_price = entry_price

        # CORREGIDO: se recalculaba el stop con el ATR genérico del
        # StrategyEngine, descartando el stop (y por tanto el take_profit
        # coherente con él) que cada estrategia ya había calculado con su
        # propio atr_multiplier/lógica. Ahora se respeta el stop de la
        # señal si la estrategia ya lo fijó; solo se recalcula con ATR
        # genérico como fallback si la señal no trae uno válido.
        if signal.stop_loss and signal.stop_loss > 0 and signal.stop_loss != entry_price:
            stop = signal.stop_loss
        else:
            stop = self._risk_manager.calculate_atr_stop(
                signal.direction, entry_price, atr_value
            )

        # Con conversión directa el coste total (2 legs) baja a ~0.72%-0.82%,
        # por lo que el stop mínimo también se reduce proporcionalmente.
        # Se mantiene la lógica de ensanche para que la distancia de riesgo
        # sea al menos la mitad de la distancia mínima de beneficio exigida.
        total_cost_pct = (
            self._settings.round_trip_fee_pct
            + self._settings.min_net_profit_pct
            + self._settings.slippage_buffer_pct
        )
        min_risk_pct = Decimal(str(total_cost_pct)) * Decimal("1.4") * Decimal("0.5")
        current_risk_pct = abs(entry_price - stop) / entry_price if entry_price > 0 else Decimal("0")
        if current_risk_pct < min_risk_pct:
            if signal.direction == SignalDirection.LONG:
                stop = entry_price * (Decimal("1") - min_risk_pct)
            else:
                stop = entry_price * (Decimal("1") + min_risk_pct)

        signal.stop_loss = stop

        qty = await self._risk_manager.calculate_order_qty(signal, atr_value)
        if qty is None:
            # Aborta tras reservar el hueco en validate_signal — liberarlo,
            # si no este símbolo+estrategia quedaría bloqueado para
            # siempre creyendo que tiene una entrada en curso.
            await self._risk_manager.release_pending_entry(symbol, signal.strategy_id)
            return None

        # ── Validación de rentabilidad neta (conversión directa) ───────
        # Se aplica siempre (PAPER y LIVE) para que el paper trading refleje
        # fielmente qué señales se ejecutarían en LIVE.
        tp = getattr(signal, "take_profit", None)
        if not self._is_direct_profitable(entry_price, stop, tp, symbol):
            await self._risk_manager.release_pending_entry(symbol, signal.strategy_id)
            return None

        if self._settings.is_live:
            return await self._execute_direct_entry(symbol, qty, entry_price, stop, signal)
        else:
            return self._paper_open(symbol, qty, entry_price, stop, signal)

    def _is_direct_profitable(self, entry_price: Decimal, stop_loss: Decimal,
                              take_profit: Optional[Decimal] = None,
                              symbol: str = "") -> bool:
        """
        Verifica rentabilidad del round-trip directo BTC↔TARGET
        (2 legs × taker_fee + slippage).

        Exige que el TP, si existe, cubra el coste total por sí mismo; si no
        hay TP, exige un R:R mínimo razonable respecto al SL.
        """
        if entry_price <= 0:
            return False

        total_cost = (
            Decimal(str(self._settings.round_trip_fee_pct))
            + Decimal(str(self._settings.min_net_profit_pct))
            + Decimal(str(self._settings.slippage_buffer_pct))
        )

        # CORREGIDO: exigir reward >= total_cost deja margen CERO tras
        # cubrir costes — cualquier slippage adicional o timing de cierre
        # imperfecto convierte una señal "rentable" en pérdida neta real.
        # Se exige un colchón de seguridad del 40% sobre el coste total
        # para que el TP, una vez alcanzado, deje beneficio neto real y no
        # solo cubra gastos justos.
        required_reward = total_cost * Decimal("1.4")

        # CORREGIDO: cuando el TP de una estrategia cae exactamente sobre
        # el "suelo" mínimo (caso más común: reversion_dist/vwap_dist no
        # supera min_tp_dist), reward y required_reward son matemáticamente
        # idénticos, pero cada uno se calcula por un camino distinto
        # (float→str→Decimal en la estrategia vs. Decimal directo aquí),
        # dejando un ruido de redondeo binario del orden de 1e-15. Eso
        # bastaba para que reward quedara una fracción infinitesimal por
        # debajo de required_reward y la señal se rechazara — confirmado
        # en el log: 450 señales dimensionadas, solo 2 ejecutadas en 2.5h.
        # Se añade una tolerancia (muchísimo menor que cualquier variación
        # de mercado real) que absorbe ese ruido sin relajar el filtro de
        # rentabilidad real.
        _tolerance = Decimal("0.00000001")

        if take_profit and take_profit > 0 and take_profit != entry_price:
            reward = abs(take_profit - entry_price) / entry_price
            if Decimal(str(reward)) < required_reward - _tolerance:
                logger.info(
                    "execution_engine.signal_rejected.tp_below_cost",
                    symbol=symbol, entry=str(entry_price), tp=str(take_profit),
                    reward_pct=f"{float(reward)*100:.3f}",
                    required_pct=f"{float(required_reward)*100:.3f}",
                )
                return False
            return True

        # Sin take_profit explícito: exigir que el riesgo (distancia al SL)
        # sea al menos 2x el coste total CON colchón, para que un TP
        # implícito (trailing) tenga margen real de cubrir comisiones antes
        # de tocar el stop.
        if stop_loss > 0:
            risk = abs(entry_price - stop_loss) / entry_price
            if Decimal(str(risk)) > required_reward * 2:
                return True

        logger.info(
            "execution_engine.signal_rejected.risk_below_cost",
            symbol=symbol, entry=str(entry_price), stop=str(stop_loss),
            tp=str(take_profit), required_pct=f"{float(required_reward)*100:.3f}",
        )
        return False

    async def _execute_direct_entry(
        self,
        symbol: str,
        qty: Decimal,
        entry_price: Decimal,
        stop: Decimal,
        signal: Signal,
    ) -> Optional[Order]:
        """
        Ejecuta entrada directa: comprar TARGET/BTC con una sola orden de mercado.
        Si la orden no se rellena en ORDER_TIMEOUT_S, aborta sin abrir posición.
        """
        target_pair = self._settings.target_pair(symbol)

        try:
            order_resp = await self._exchange.create_order(
                symbol=target_pair,
                type="market",
                side="buy",
                amount=float(qty),
            )
            order_id = order_resp["id"]
            logger.info("execution_engine.entry_order_placed", pair=target_pair, id=order_id)
        except Exception as exc:
            logger.error("execution_engine.entry_order_failed", pair=target_pair, error=str(exc))
            await self._risk_manager.release_pending_entry(symbol, signal.strategy_id)
            return None

        filled = await self._wait_for_fill(order_id, target_pair, ORDER_TIMEOUT_S)
        if not filled:
            logger.warning(
                "execution_engine.entry_order_timeout",
                pair=target_pair,
                order_id=order_id,
                timeout_s=ORDER_TIMEOUT_S,
            )
            await self._risk_manager.release_pending_entry(symbol, signal.strategy_id)
            return None

        filled_qty = await self._get_filled_qty(order_id, target_pair)

        order = Order(
            symbol=symbol,
            side=OrderSide.BUY,
            qty=filled_qty,
            price=entry_price,
            order_type=OrderType.MARKET,
            status=OrderStatus.FILLED,
            strategy_id=signal.strategy_id,
        )
        trailing_delta = abs(entry_price - stop)
        total_cost_pct = (
            self._settings.round_trip_fee_pct
            + self._settings.min_net_profit_pct
            + self._settings.slippage_buffer_pct
        )
        trailing_activation = entry_price * (Decimal("1") + Decimal(str(total_cost_pct)) * Decimal("1.1"))
        entry_eur, entry_btc_eur_rate = self._snapshot_entry_eur(entry_price)
        position = Position(
            symbol=symbol,
            side=PositionSide.LONG,
            qty=filled_qty,
            entry_price=entry_price,
            stop_loss=stop,
            take_profit=getattr(signal, "take_profit", None),
            trailing_stop_delta=trailing_delta,
            trailing_activation_price=trailing_activation,
            strategy_id=signal.strategy_id,
            entry_eur=entry_eur,
            entry_btc_eur_rate=entry_btc_eur_rate,
        )
        await self._risk_manager.register_position(position)
        return order

    async def _wait_for_fill(self, order_id: str, pair: str, timeout_s: int) -> bool:
        """Polling hasta que la orden quede filled o expire el timeout."""
        elapsed = 0.0
        while elapsed < timeout_s:
            try:
                order = await self._exchange.fetch_order(order_id, pair)
                if order["status"] == "closed":
                    return True
                if order["status"] == "canceled":
                    return False
            except Exception as exc:
                logger.warning(
                    "execution_engine.fill_poll_error",
                    order_id=order_id,
                    error=str(exc),
                )
            await asyncio.sleep(ORDER_POLL_INTERVAL_S)
            elapsed += ORDER_POLL_INTERVAL_S
        return False

    async def _get_filled_qty(self, order_id: str, pair: str) -> Decimal:
        try:
            order = await self._exchange.fetch_order(order_id, pair)
            return Decimal(str(order.get("filled") or order.get("amount") or 0))
        except Exception:
            return Decimal("0")

    # ── Cierre de posición (venta directa TARGET→BTC) ─────────────────

    async def _close_position(self, signal: Signal) -> Optional[Order]:
        symbol = signal.symbol
        position = self._risk_manager.get_open_position(symbol, signal.strategy_id)

        if position is None:
            logger.debug("execution_engine.close_skipped_no_position", symbol=symbol)
            return None

        if position.side != PositionSide.LONG:
            logger.warning(
                "execution_engine.close_unexpected_side",
                symbol=symbol,
                side=position.side.value,
            )
            return None

        qty = position.qty
        # Venta a mercado: el fill real es el BID.
        if self._data_engine is not None:
            ticker = self._data_engine.get_ticker(symbol)
            if ticker is not None and ticker.bid and ticker.bid > 0:
                exit_price = ticker.bid
            elif position.current_price and position.current_price > 0:
                exit_price = position.current_price
            else:
                exit_price = self._live_prices.get(symbol, signal.entry_price)
        else:
            exit_price = (
                position.current_price
                if position.current_price and position.current_price > 0
                else self._live_prices.get(symbol, signal.entry_price)
            )

        # Los cierres por convergencia estadística solo se ejecutan si el
        # movimiento bruto ya cubre el coste total del round-trip directo
        # (2 legs); los cierres por precio real (stop/trailing/take_profit)
        # se ejecutan siempre.
        exit_reason = (signal.metadata or {}).get("exit_reason")
        if exit_reason is not None and exit_reason not in self._PRICE_EXIT_REASONS:
            total_cost_pct = (
                self._settings.round_trip_fee_pct
                + self._settings.min_net_profit_pct
                + self._settings.slippage_buffer_pct
            )
            if position.entry_price > 0:
                gross_move = (exit_price - position.entry_price) / position.entry_price
            else:
                gross_move = Decimal("0")
            if gross_move < Decimal(str(total_cost_pct)):
                logger.info(
                    "execution_engine.close_skipped_unprofitable_convergence",
                    symbol=symbol,
                    exit_reason=exit_reason,
                    gross_move_pct=f"{float(gross_move) * 100:.3f}",
                    required_pct=f"{total_cost_pct * 100:.3f}",
                )
                return None

        if self._settings.is_live:
            target_pair = self._settings.target_pair(symbol)
            # Orden directa: vender TARGET/BTC — 1 sola leg
            try:
                exit_order = await self._exchange.create_order(
                    symbol=target_pair,
                    type="market",
                    side="sell",
                    amount=float(qty),
                )
                exit_filled = await self._wait_for_fill(
                    exit_order["id"], target_pair, ORDER_TIMEOUT_S
                )
                if not exit_filled:
                    logger.warning(
                        "execution_engine.close_order_timeout",
                        symbol=symbol,
                        order_id=exit_order["id"],
                    )
                    return None
            except Exception as exc:
                logger.error("execution_engine.close_order_failed", symbol=symbol, error=str(exc))
                return None

        gross_pnl = (exit_price - position.entry_price) * qty

        # Comisiones: 1 leg entrada + 1 leg salida = 2 legs totales.
        entry_notional = position.entry_price * qty
        entry_fee = entry_notional * Decimal(str(self._settings.kraken_taker_fee_pct))
        exit_notional = exit_price * qty
        exit_fee = exit_notional * Decimal(str(self._settings.kraken_taker_fee_pct))
        pnl = gross_pnl - entry_fee - exit_fee

        if not self._settings.is_live:
            self._paper_balance += exit_notional - exit_fee
            self._paper_positions.pop(symbol, None)

        # CORREGIDO: en PAPER se pasa el balance real ya actualizado
        # (new_balance) para que risk_manager quede sincronizado como
        # espejo exacto de _paper_balance — ver sync_balance/close_position.
        await self._risk_manager.close_position(
            symbol, position.strategy_id, pnl,
            new_balance=self._paper_balance if not self._settings.is_live else None,
        )

        order = Order(
            symbol=symbol,
            side=OrderSide.SELL,
            qty=qty,
            price=exit_price,
            order_type=OrderType.MARKET,
            status=OrderStatus.FILLED,
            strategy_id=signal.strategy_id,
        )
        # CORREGIDO: el log solo mostraba symbol/qty/pnl, obligando a cruzar
        # manualmente con el paper_open correspondiente para saber a qué
        # precio se abrió, cuánto duró la posición, o por qué se cerró.
        # Se añade todo el contexto necesario para diagnosticar sin tener
        # que reconstruir la operación línea a línea.
        hold_seconds = (
            (datetime.now(tz=timezone.utc) - position.opened_at).total_seconds()
            if position.opened_at else None
        )
        pnl_pct = float(pnl / entry_notional * 100) if entry_notional > 0 else 0.0
        logger.info(
            "execution_engine.position_closed",
            symbol=symbol,
            qty=str(qty),
            entry_price=str(position.entry_price),
            exit_price=str(exit_price),
            pnl=str(pnl),
            pnl_pct=f"{pnl_pct:.3f}",
            strategy=position.strategy_id,
            exit_reason=(signal.metadata or {}).get("exit_reason", "unknown"),
            hold_seconds=f"{hold_seconds:.1f}" if hold_seconds is not None else None,
        )
        return order

    # ── Paper trading ─────────────────────────────────────────────────

    def _paper_open(
        self,
        symbol: str,
        qty: Decimal,
        entry_price: Decimal,
        stop: Decimal,
        signal: Signal,
    ) -> Optional[Order]:
        cost = qty * entry_price
        fee = cost * Decimal(str(self._settings.kraken_taker_fee_pct))  # 1 leg entrada

        # CORREGIDO: no había ningún chequeo de que cost+fee no superase el
        # balance paper disponible — el log mostraba balance_after negativo
        # (-0.16, -0.33, -3.2 BTC) tras aperturas sucesivas, simulando
        # comprar con dinero que no existía. En LIVE el exchange rechazaría
        # la orden por fondos insuficientes; en PAPER debe hacer lo mismo.
        if cost + fee > self._paper_balance:
            logger.warning(
                "execution_engine.paper_open_insufficient_balance",
                symbol=symbol,
                required=str(cost + fee),
                available=str(self._paper_balance),
            )
            asyncio.create_task(
                self._risk_manager.release_pending_entry(symbol, signal.strategy_id)
            )
            return None

        self._paper_balance -= cost + fee
        self._paper_positions[symbol] = qty
        asyncio.create_task(self._risk_manager.sync_balance(self._paper_balance))

        # Usar la distancia real entry↔stop (ATR de la estrategia) como delta
        # del trailing; activar solo cuando el precio ya cubre el coste total.
        trailing_delta = abs(entry_price - stop)
        total_cost_pct = (
            self._settings.round_trip_fee_pct
            + self._settings.min_net_profit_pct
            + self._settings.slippage_buffer_pct
        )
        trailing_activation = entry_price * (Decimal("1") + Decimal(str(total_cost_pct)) * Decimal("1.1"))
        entry_eur, entry_btc_eur_rate = self._snapshot_entry_eur(entry_price)
        position = Position(
            symbol=symbol,
            side=PositionSide.LONG,
            qty=qty,
            entry_price=entry_price,
            stop_loss=stop,
            take_profit=getattr(signal, "take_profit", None),
            trailing_stop_delta=trailing_delta,
            trailing_activation_price=trailing_activation,
            strategy_id=signal.strategy_id,
            entry_eur=entry_eur,
            entry_btc_eur_rate=entry_btc_eur_rate,
        )
        asyncio.create_task(self._risk_manager.register_position(position))

        logger.info(
            "execution_engine.paper_open",
            symbol=symbol,
            qty=str(qty),
            entry=str(entry_price),
            stop=str(stop),
            take_profit=str(getattr(signal, "take_profit", None)),
            fee=str(fee),
            balance_after=str(self._paper_balance),
            strategy=signal.strategy_id,
            entry_eur=str(entry_eur) if entry_eur is not None else None,
        )
        return Order(
            symbol=symbol,
            side=OrderSide.BUY,
            qty=qty,
            price=entry_price,
            order_type=OrderType.MARKET,
            status=OrderStatus.FILLED,
            strategy_id=signal.strategy_id,
        )

    # ── Cierre manual desde el dashboard ─────────────────────────────

    async def close_position_manual(self, symbol: str, strategy_id: Optional[str] = None) -> Optional[Order]:
        """
        Cierra una posición abierta de forma inmediata desde el dashboard.
        Usa exit_reason='manual_close' para saltarse la compuerta de
        rentabilidad estadística y ejecutarse siempre, igual que
        trailing_stop_hit o take_profit_hit.

        NUEVO: ahora puede haber más de una posición abierta en el mismo
        símbolo (una por estrategia), así que `symbol` solo ya no basta
        para identificar cuál cerrar. Si se indica `strategy_id`, cierra
        esa posición exacta. Si no se indica y solo hay una posición
        abierta en ese símbolo, se cierra esa (compatibilidad). Si hay
        varias y no se especifica cuál, se rechaza explícitamente en vez
        de adivinar.
        """
        candidates = self._risk_manager.get_open_positions_for_symbol(symbol)
        if strategy_id:
            position = next((p for p in candidates if p.strategy_id == strategy_id), None)
        elif len(candidates) == 1:
            position = candidates[0]
        elif len(candidates) > 1:
            logger.warning(
                "execution_engine.manual_close_ambiguous",
                symbol=symbol,
                strategies=[p.strategy_id for p in candidates],
            )
            return None
        else:
            position = None

        if position is None:
            logger.warning("execution_engine.manual_close_no_position", symbol=symbol, strategy=strategy_id)
            return None

        from models.signal import Signal, SignalDirection
        # CORREGIDO: se tomaba el precio de `self._live_prices` (un dict
        # separado, interno de ExecutionEngine) y, si por cualquier motivo
        # no tenía entrada para el símbolo, caía silenciosamente en
        # `position.entry_price` — es decir, podía ejecutar el cierre al
        # mismo precio de apertura (PnL ≈ 0) aunque el precio real llevara
        # rato movido, sin relación con lo que el usuario veía marcado en
        # el dashboard. `position.current_price` es el valor que el
        # dashboard realmente muestra (se actualiza en cada tick desde
        # RiskManager.update_trailing_stops), así que es la fuente
        # correcta y más fiable para "el precio marcado actual": se usa
        # como prioridad, y `_live_prices`/entry_price solo como reserva
        # si por algún motivo aún no se ha recibido ningún tick.
        current_price = (
            position.current_price
            if position.current_price and position.current_price > 0
            else self._live_prices.get(symbol, position.entry_price)
        )
        signal = Signal(
            symbol=symbol,
            direction=SignalDirection.CLOSE_LONG,
            entry_price=current_price,
            stop_loss=current_price,  # No relevante para cierre
            strategy_id=position.strategy_id or "manual",
            metadata={"exit_reason": "manual_close"},
        )
        logger.info("execution_engine.manual_close_requested", symbol=symbol)
        return await self._close_position(signal)

    # ── Trailing stop ─────────────────────────────────────────────────

    async def handle_trailing_stop_check(
        self, symbol: str, strategy_id: str, price: Decimal
    ) -> Optional[str]:
        return await self._risk_manager.update_trailing_stops(symbol, strategy_id, price)

    # ── Cierre ────────────────────────────────────────────────────────

    async def close(self) -> None:
        if self._exchange:
            await self._exchange.close()
        logger.info("execution_engine.closed")
