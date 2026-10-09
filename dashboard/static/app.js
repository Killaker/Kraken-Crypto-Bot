(() => {
  "use strict";

  const el = (id) => document.getElementById(id);

  const connDot = el("conn-dot");
  const statusText = el("status-text");
  const clockEl = el("clock");

  const chipMode = el("chip-mode");
  const chipNet = el("chip-net");
  const chipTf = el("chip-tf");

  const balanceValue = el("balance-value");

  const ddValue = el("dd-value");
  const ddLimit = el("dd-limit");
  const ddFill = el("dd-fill");
  const killBadge = el("kill-badge");
  const killResetBtn = el("kill-reset-btn");
  killResetBtn.addEventListener("click", async () => {
    killResetBtn.disabled = true;
    try {
      const res = await fetch("/api/kill-switch/reset", { method: "POST" });
      if (!res.ok) throw new Error(await res.text());
    } catch (e) { console.error("kill-switch reset error:", e); }
    killResetBtn.disabled = false;
  });

  const entriesBadge = el("entries-badge");
  const entriesToggleBtn = el("entries-toggle-btn");
  let _entriesPaused = false;
  entriesToggleBtn.addEventListener("click", async () => {
    entriesToggleBtn.disabled = true;
    try {
      const action = _entriesPaused ? "resume" : "pause";
      const res = await fetch(`/api/entries/${action}`, { method: "POST" });
      if (!res.ok) throw new Error(await res.text());
    } catch (e) { console.error("entries toggle error:", e); }
    entriesToggleBtn.disabled = false;
  });

  const cfgRisk = el("cfg-risk");
  const cfgMaxPos = el("cfg-maxpos");
  const cfgStrats = el("cfg-strats");

  const tickersBody = el("tickers-body");
  const positionsBody = el("positions-body");
  const positionsCount = el("positions-count");
  const logsList = el("logs-list");

  const seenLogTimestamps = new Set();
  
  // --- NUEVO: Conversión visual a Fiat ---
  // dogeToEurRate and btcToEurRate declared in fetchEurRates block

  // Tasas de conversión a EUR
  let dogeToEurRate = 0.08;   // DOGE/EUR (balance base)
  let btcToEurRate  = 90000;  // BTC/EUR  (bridge, para precios TARGET/BTC→EUR)

  const chipBtcEur = el("chip-btc-eur");
  let _prevBtcEurRate = null;
  let _btcEurReady = false;
  // Precio de entrada en EUR congelado por posición (clave symbol|strategy).
  // El backend ya lo manda en `entry_eur`; esto es red de seguridad para
  // no volver a convertir con la tasa BTC/EUR en vivo si por algún motivo
  // el campo aún no llega.
  const _frozenEntryEur = new Map();

  function renderBtcEurChip(price) {
    if (!chipBtcEur || !price) return;
    chipBtcEur.classList.remove("flash-up", "flash-down");
    if (_prevBtcEurRate !== null) {
      chipBtcEur.classList.add(price >= _prevBtcEurRate ? "flash-up" : "flash-down");
    }
    chipBtcEur.textContent = `BTC/EUR ${price.toLocaleString("es-ES", { maximumFractionDigits: 0 })} €`;
    _prevBtcEurRate = price;
  }

  async function fetchEurRates() {
    try {
      const [r1, r2] = await Promise.all([
        fetch("https://api.kraken.com/0/public/Ticker?pair=XDGEUR"),
        fetch("https://api.kraken.com/0/public/Ticker?pair=XBTEUR"),
      ]);
      const [d1, d2] = await Promise.all([r1.json(), r2.json()]);
      if (d1.result) {
        const k = Object.keys(d1.result)[0];
        dogeToEurRate = parseFloat(d1.result[k].c[0]);
      }
      if (d2.result) {
        const k = Object.keys(d2.result)[0];
        btcToEurRate = parseFloat(d2.result[k].c[0]);
        _btcEurReady = Number.isFinite(btcToEurRate) && btcToEurRate > 0;
        renderBtcEurChip(btcToEurRate);
      }
    } catch (err) { console.error("EUR rates error:", err); }
  }

  fetchEurRates();
  setInterval(fetchEurRates, 5000);
  // ---------------------------------------

  // Precisión adaptativa según magnitud:
  //   >= 1000   → 2 dec  (balance DOGE: 15 234.56)
  //   1–999     → 4 dec  (precio BTC: 234.5612)
  //   0.01–1    → 6 dec  (precio ETH/BTC: 0.034521)
  //   < 0.01    → 8 dec  (precio SOL/BTC: 0.00234156)
  // Si se pasa `decimals` explícito distinto de 2 se respeta (p.ej. qty con 5 dec).
  function fmtNum(v, decimals = null) {
    const n = parseFloat(v);
    if (Number.isNaN(n) || v === null || v === undefined) return "—";
    let d;
    if (decimals !== null) {
      d = decimals;
    } else {
      const abs = Math.abs(n);
      if (abs === 0)        d = 2;
      else if (abs >= 1000) d = 2;
      else if (abs >= 1)    d = 4;
      else if (abs >= 0.01) d = 6;
      else                  d = 8;
    }
    return n.toLocaleString("en-US", {
      minimumFractionDigits: d,
      maximumFractionDigits: d,
    });
  }

  function fmtPct(v, decimals = 2) {
    const n = parseFloat(v);
    if (Number.isNaN(n)) return "—";
    const sign = n > 0 ? "+" : "";
    return `${sign}${n.toFixed(decimals)}%`;
  }

  function updateClock() {
    const now = new Date();
    clockEl.textContent = now.toLocaleTimeString("es-ES", { hour12: false });
  }
  setInterval(updateClock, 1000);
  updateClock();

  // ── Strategy toggle ─────────────────────────────────────────────────────
  let _stratState = {};

  async function toggleStrategy(id, enable) {
    const action = enable ? "enable" : "disable";
    try {
      const res = await fetch(`/api/strategies/${id}/${action}`, { method: "POST" });
      if (!res.ok) throw new Error(await res.text());
    } catch (e) { console.error("strategy toggle error:", e); }
  }

  function renderStrategyToggles(active, available) {
    if (!available || !available.length) return;
    const container = document.getElementById("strategy-toggles");
    if (!container) return;
    const newState = {};
    available.forEach(id => newState[id] = active.includes(id));
    if (JSON.stringify(newState) === JSON.stringify(_stratState)) return;
    _stratState = newState;
    const labels = {
      MEAN_REVERSION: "Mean Rev.",
      VOLATILITY_BREAKOUT: "Vol. Break.",
      SYNTHETIC_ARB: "Synth. Arb.",
      SMART_ROUTING: "Smart Route",
      CROSS_RATE_PAIRS: "Cross-Rate Pairs",
      PORTFOLIO_REBALANCE: "Rebalance",
      CROSS_CURRENCY_ARB: "Cross-Curr. Arb.",
	  TREND_MOMENTUM: "Trend Momentum",
      DIP_REBOUND: "Dip Rebound",
      TRIANGULAR_ARB: "Triangular Arb",
    };
    container.innerHTML = available.map(id => {
      const on = active.includes(id);
      return `<button class="strat-btn ${on?"on":"off"}" data-id="${id}" data-on="${on}">
        <span class="strat-dot"></span>${labels[id] || id}
      </button>`;
    }).join("");
    container.querySelectorAll(".strat-btn").forEach(btn => {
      btn.addEventListener("click", async () => {
        const id = btn.dataset.id;
        const on = btn.dataset.on === "true";
        btn.disabled = true;
        await toggleStrategy(id, !on);
        btn.disabled = false;
        // Force refresh from server
        _stratState = {};
      });
    });
  }

  // ── Activar/desactivar entradas nuevas por moneda ───────────────────
  let _symState = {};

  async function toggleSymbol(symbol, enable) {
    const action = enable ? "enable" : "disable";
    try {
      const res = await fetch(`/api/symbols/${symbol}/${action}`, { method: "POST" });
      if (!res.ok) throw new Error(await res.text());
    } catch (e) { console.error("symbol toggle error:", e); }
  }

  function renderSymbolToggles(allSymbols, disabledSymbols) {
    if (!allSymbols || !allSymbols.length) return;
    const container = document.getElementById("symbol-toggles");
    if (!container) return;
    const disabledSet = new Set(disabledSymbols || []);
    const newState = {};
    allSymbols.forEach(s => newState[s] = !disabledSet.has(s));
    if (JSON.stringify(newState) === JSON.stringify(_symState)) return;
    _symState = newState;
    container.innerHTML = allSymbols.map(symbol => {
      const on = !disabledSet.has(symbol);
      return `<button class="strat-btn ${on?"on":"off"}" data-symbol="${symbol}" data-on="${on}"
        title="${on ? "Permitiendo nuevas entradas" : "Bloqueado: no se abrirán posiciones nuevas (las ya abiertas siguen igual)"}">
        <span class="strat-dot"></span>${symbol}
      </button>`;
    }).join("");
    container.querySelectorAll(".strat-btn").forEach(btn => {
      btn.addEventListener("click", async () => {
        const symbol = btn.dataset.symbol;
        const on = btn.dataset.on === "true";
        btn.disabled = true;
        await toggleSymbol(symbol, !on);
        btn.disabled = false;
        _symState = {};
      });
    });
  }

  // ── Popups de cambio de tendencia ───────────────────────────────────
  let _lastTrendAlertId = 0;
  let _trendAlertsInit = false;

  function showTrendToast(alert) {
    const container = document.getElementById("trend-toasts");
    if (!container) return;
    const up = alert.direction === "up";
    const toast = document.createElement("div");
    toast.className = `trend-toast ${up ? "trend-toast--up" : "trend-toast--down"}`;
    toast.innerHTML = `
      <span class="trend-toast__icon">${up ? "▲" : "▼"}</span>
      <span class="trend-toast__text"><strong>${alert.symbol}</strong> cambia a tendencia ${up ? "alcista" : "bajista"}</span>
      <button class="trend-toast__close" aria-label="Cerrar">×</button>
    `;
    toast.querySelector(".trend-toast__close").addEventListener("click", () => toast.remove());
    container.appendChild(toast);
    setTimeout(() => toast.remove(), 15000);
  }

  function handleTrendAlerts(alerts) {
    if (!alerts || !alerts.length) return;
    if (!_trendAlertsInit) {
      // Primera carga tras (re)conectar: no mostrar popups de alertas ya
      // pasadas, solo recordar el id más alto visto hasta ahora.
      _trendAlertsInit = true;
      _lastTrendAlertId = Math.max(...alerts.map(a => a.id), 0);
      return;
    }
    const fresh = alerts.filter(a => a.id > _lastTrendAlertId);
    if (!fresh.length) return;
    _lastTrendAlertId = Math.max(...fresh.map(a => a.id), _lastTrendAlertId);
    fresh.forEach(showTrendToast);
  }

  function renderBot(bot) {
    chipMode.textContent = bot.mode;
    chipMode.className = "meta-chip " + (bot.mode === "LIVE" ? "live" : "paper");

    chipNet.textContent = bot.testnet ? "TESTNET" : "MAINNET";
    chipNet.className = "meta-chip " + (bot.testnet ? "testnet" : "");

    chipTf.textContent = bot.timeframe || "—";

    connDot.classList.toggle("live", !!bot.running);
    const syms = Array.isArray(bot.symbols) ? bot.symbols.join(", ") : "—";
    statusText.textContent = bot.running ? `En ejecución · ${syms}` : "Bot inicializando…";

    cfgStrats.textContent = (bot.active_strategies || []).join(", ") || "—";
    renderStrategyToggles(bot.active_strategies || [], bot.available_strategies || []);
    renderSymbolToggles(bot.symbols || [], bot.disabled_symbols || []);
  }

  function renderRisk(risk) {
    const balBtc = parseFloat(risk.balance);
    const balEur = balBtc * btcToEurRate;
    balanceValue.textContent = fmtNum(balBtc, 8) + " BTC ≈ " + fmtNum(balEur, 2) + " €";

    ddValue.textContent = `${risk.daily_drawdown.toFixed(2)}%`;
    ddLimit.textContent = `${risk.max_daily_drawdown.toFixed(2)}%`;

    const ratio = risk.max_daily_drawdown > 0
      ? Math.min(100, (risk.daily_drawdown / risk.max_daily_drawdown) * 100)
      : 0;
    ddFill.style.width = `${ratio}%`;
    ddFill.classList.remove("warn", "danger");
    if (ratio >= 90) ddFill.classList.add("danger");
    else if (ratio >= 60) ddFill.classList.add("warn");

    killBadge.textContent = risk.kill_switch_active ? "KILL-SWITCH ACTIVO" : "NORMAL";
    killBadge.classList.toggle("active", !!risk.kill_switch_active);
    killResetBtn.style.display = risk.kill_switch_active ? "inline-block" : "none";

    _entriesPaused = !!risk.new_entries_paused;
    entriesBadge.textContent = _entriesPaused ? "ENTRADAS PAUSADAS" : "ENTRADAS ACTIVAS";
    entriesBadge.classList.toggle("paused", _entriesPaused);
    entriesToggleBtn.textContent = _entriesPaused ? "Reanudar nuevas entradas" : "Pausar nuevas entradas";
    entriesToggleBtn.classList.toggle("is-paused", _entriesPaused);

    cfgRisk.textContent = `${risk.risk_per_trade.toFixed(2)}% / trade`;
    cfgMaxPos.textContent = risk.max_open_positions;
  }

  function renderTickers(tickers) {
    const symbols = Object.keys(tickers);
    if (!symbols.length) {
      tickersBody.innerHTML = `<tr class="empty-row"><td colspan="5">Esperando datos…</td></tr>`;
      return;
    }
    tickersBody.innerHTML = symbols
      .map((sym) => {
        const t = tickers[sym];
        const pct = t.price_change_pct_24h;
        const pctClass = pct >= 0 ? "pct-pos" : "pct-neg";
        
        // Multiplicamos por la tasa fiat
        const lastEur = t.last_price * btcToEurRate;
        const bidEur = t.bid * btcToEurRate;
        const askEur = t.ask * btcToEurRate;

        return `
          <tr>
            <td>${sym}</td>
            <td>${fmtNum(lastEur, 2)} €</td>
            <td class="${pctClass}">${fmtPct(pct)}</td>
            <td>${fmtNum(bidEur, 2)} €</td>
            <td>${fmtNum(askEur, 2)} €</td>
          </tr>`;
      })
      .join("");
  }

  function posKey(p) {
    return `${p.symbol}|${p.strategy_id || ""}`;
  }

  function frozenEntryEur(p) {
    const key = posKey(p);
    const fromBot = parseFloat(p.entry_eur);
    if (Number.isFinite(fromBot) && fromBot > 0) {
      _frozenEntryEur.set(key, fromBot);
      return fromBot;
    }
    if (_frozenEntryEur.has(key)) return _frozenEntryEur.get(key);
    const entryBtc = parseFloat(p.entry_price);
    if (_btcEurReady && Number.isFinite(entryBtc) && entryBtc > 0) {
      const v = entryBtc * btcToEurRate;
      _frozenEntryEur.set(key, v);
      return v;
    }
    return Number.isFinite(entryBtc) ? entryBtc * btcToEurRate : NaN;
  }

  function renderPositions(positions) {
    positionsCount.textContent = positions.length;
    if (!positions.length) {
      positionsBody.innerHTML = `<tr class="empty-row"><td colspan="9">Sin posiciones abiertas</td></tr>`;
      _frozenEntryEur.clear();
      return;
    }
    const openKeys = new Set(positions.map(posKey));
    for (const k of [..._frozenEntryEur.keys()]) {
      if (!openKeys.has(k)) _frozenEntryEur.delete(k);
    }
    positionsBody.innerHTML = positions
      .map((p) => {
        const sideClass = p.side === "LONG" ? "side-long" : "side-short";
        // NUEVO: la columna principal y el marcador usan la rentabilidad
        // en EUR (pnl_eur_net_pct), que es la que de verdad importa al
        // usuario — incluye el movimiento de la propia moneda Y el del
        // BTC/EUR entre la apertura y ahora. El valor en BTC (pnl_pct)
        // sigue siendo el que de verdad dispara stop/TP/trailing (el par
        // se liquida en BTC, no en EUR), así que se mantiene siempre
        // visible en el tooltip para no esconder esa diferencia.
        const hasEur = p.pnl_eur_net_pct !== null && p.pnl_eur_net_pct !== undefined;
        const pnlMainPct = hasEur ? p.pnl_eur_net_pct : (p.pnl_net_pct);
        const pnlClass = pnlMainPct >= 0 ? "pnl-pos" : "pnl-neg";
        const pnlDotClass = pnlMainPct >= 0 ? "pnl-dot pnl-dot--up" : "pnl-dot pnl-dot--down";
        const pnlDotTitle = (pnlMainPct >= 0 ? "Ganancia NETA en EUR" : "Pérdida NETA en EUR")
          + ` si se cierra ahora (${fmtPct(pnlMainPct, 3)})`
          + ` — en BTC (lo que dispara el stop): ${fmtPct(p.pnl_net_pct, 3)} neto, ${fmtPct(p.pnl_pct, 3)} bruto`
          + (hasEur ? `, bruto EUR ${fmtPct(p.pnl_eur_pct, 3)}` : "");

        // Entrada: EUR congelado al abrir + precio nativo TARGET/BTC
        // (también fijo). Actual: EUR a tasa BTC/EUR en vivo.
        const entryEur = frozenEntryEur(p);
        const currentEur = p.current_price * btcToEurRate;
        const stopEur = p.trailing_stop_price ? (p.trailing_stop_price * btcToEurRate) : null;

        return `
          <tr>
            <td><span class="${pnlDotClass}" title="${pnlDotTitle}"></span>${p.symbol}</td>
            <td class="${sideClass}">${p.side}</td>
            <td>${fmtNum(p.qty, 5)}</td>
            <td title="Precio de apertura, congelado">
              <span class="px-eur">${fmtNum(entryEur, 2)} €</span>
              <span class="px-btc">${fmtNum(p.entry_price)} BTC</span>
            </td>
            <td>
              <span class="px-eur">${fmtNum(currentEur, 2)} €</span>
              <span class="px-btc">${fmtNum(p.current_price)} BTC</span>
            </td>
            <td class="${pnlClass}" title="${pnlDotTitle}">${fmtPct(pnlMainPct, 3)}${hasEur ? "" : " (BTC)"}</td>
            <td>${stopEur ? fmtNum(stopEur, 2) + " €" : "—"}</td>
            <td>${p.strategy_id || "—"}</td>
            <td><button class="close-btn" data-symbol="${p.symbol}" data-strategy="${p.strategy_id || ""}">Cerrar</button></td>
          </tr>`;
      })
      .join("");
  }

  // ── Cierre manual de posición ───────────────────────────────────────
  positionsBody.addEventListener("click", async (e) => {
    const btn = e.target.closest(".close-btn");
    if (!btn || btn.disabled) return;
    const symbol = btn.dataset.symbol;
    const strategy = btn.dataset.strategy;
    if (!symbol) return;
    btn.disabled = true;
    btn.textContent = "Cerrando…";
    try {
      const url = `/api/positions/${symbol}/close` + (strategy ? `?strategy=${encodeURIComponent(strategy)}` : "");
      const res = await fetch(url, { method: "POST" });
      if (!res.ok) {
        console.error("manual close error:", await res.text());
        btn.textContent = "Error";
        setTimeout(() => { btn.textContent = "Cerrar"; btn.disabled = false; }, 2000);
      }
    } catch (e) {
      console.error("manual close fetch error:", e);
      btn.textContent = "Error";
      setTimeout(() => { btn.textContent = "Cerrar"; btn.disabled = false; }, 2000);
    }
  });

  function renderLogs(logs) {
    if (!logs || !logs.length) return;
    const fragments = [];
    for (const log of logs) {
      const key = log.timestamp + log.event;
      if (seenLogTimestamps.has(key)) continue;
      seenLogTimestamps.add(key);

      const time = new Date(log.timestamp).toLocaleTimeString("es-ES", { hour12: false });
      const level = (log.level || "info").toLowerCase();
      fragments.push(
        `<div class="log-line level-${level}"><time>${time}</time><span class="evt">${log.event}</span></div>`
      );
    }
    if (fragments.length) {
      logsList.insertAdjacentHTML("afterbegin", fragments.join(""));
      // Limitar tamaño del DOM de logs
      while (logsList.children.length > 200) {
        logsList.removeChild(logsList.lastChild);
      }
    }
  }

  function renderSnapshot(data) {
    renderBot(data.bot);
    renderRisk(data.risk);
    renderTickers(data.tickers || {});
    renderPositions(data.positions || []);
    if (data.logs) renderLogs(data.logs);
    handleTrendAlerts(data.trend_alerts || []);
  }

  // ── Conexión WebSocket con fallback a polling ──────────────────────

  let ws = null;
  let reconnectDelay = 1000;

  function connectWs() {
    const protocol = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${protocol}://${location.host}/ws`);

    ws.onopen = () => {
      statusText.textContent = "Conectado · streaming en vivo";
      reconnectDelay = 1000;
    };

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        renderSnapshot(data);
      } catch (err) {
        console.error("Error parseando snapshot", err);
      }
    };

    ws.onclose = () => {
      statusText.textContent = "Desconectado · reintentando…";
      connDot.classList.remove("live");
      setTimeout(connectWs, reconnectDelay);
      reconnectDelay = Math.min(reconnectDelay * 1.5, 10000);
    };

    ws.onerror = () => {
      ws.close();
    };
  }

  async function pollFallback() {
    try {
      const res = await fetch("/api/state");
      const data = await res.json();
      renderSnapshot(data);
    } catch (err) {
      statusText.textContent = "Sin conexión con el bot";
    }
  }

  // Carga inicial vía REST mientras el WS conecta
  pollFallback();
  connectWs();
})();
