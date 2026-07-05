let SIGNALS = null;

function classToken(classification) {
  return (classification || "").toLowerCase();
}

function badgeToken(label) {
  return (label || "").toLowerCase().replace(/\s+/g, "-");
}

function fmtTime(iso) {
  if (!iso) return "no data yet";
  const d = new Date(iso);
  return d.toLocaleString([], { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
}

async function loadSignals() {
  try {
    const res = await fetch("data/signals.json", { cache: "no-store" });
    SIGNALS = await res.json();
  } catch (e) {
    SIGNALS = { generated_at: null, sectors: [] };
  }
  render();
}

function render() {
  document.getElementById("updated-at").textContent = fmtTime(SIGNALS.generated_at);

  if (!SIGNALS.sectors || SIGNALS.sectors.length === 0) {
    document.getElementById("home-view").style.display = "none";
    document.getElementById("empty-state").style.display = "block";
    return;
  }

  const grid = document.getElementById("sector-grid");
  grid.innerHTML = "";
  SIGNALS.sectors.forEach((sector, idx) => {
    const card = document.createElement("div");
    card.className = `sector-card ${classToken(sector.classification)}`;
    card.onclick = () => showSector(idx);
    card.innerHTML = `
      <div class="sector-name">${sector.name.replace("NIFTY ", "")}</div>
      <div class="sector-score">${sector.bullish_score ?? "—"}</div>
      <div class="sector-meta">RSI ${sector.avg_rsi ?? "—"} · MACD+ ${sector.macd_bullish_pct ?? "—"}%</div>
    `;
    grid.appendChild(card);
  });
}

function showSector(idx) {
  const sector = SIGNALS.sectors[idx];
  document.getElementById("home-view").style.display = "none";
  document.getElementById("sector-view").style.display = "block";
  document.getElementById("sector-view-title").textContent =
    `${sector.name} — ${sector.classification} (${sector.bullish_score})`;

  const list = document.getElementById("stock-list");
  list.innerHTML = "";

  if (!sector.stocks || sector.stocks.length === 0) {
    list.innerHTML = `<div class="empty">No stock-level data for this sector.</div>`;
    return;
  }

  sector.stocks.forEach((s) => {
    const row = document.createElement("div");
    row.className = "stock-row";
    const changeColor = s.change_pct >= 0 ? "var(--bull)" : "var(--bear)";
    row.innerHTML = `
      <div class="stock-left">
        <div class="stock-symbol">${s.symbol}</div>
        <div class="stock-sub">RSI ${s.rsi ?? "—"} · MACD ${s.macd_hist ?? "—"} · Vol ${s.volume_ratio ?? "—"}x · Del% ${s.delivery_pct ?? "—"}</div>
      </div>
      <div class="stock-right">
        <div class="stock-price">₹${s.ltp} <span style="color:${changeColor}">${s.change_pct >= 0 ? "+" : ""}${s.change_pct}%</span></div>
        <span class="badge ${badgeToken(s.swing_signal)}">${s.swing_signal}</span>
      </div>
    `;
    list.appendChild(row);
  });
}

function showHome() {
  document.getElementById("sector-view").style.display = "none";
  document.getElementById("home-view").style.display = "block";
}

loadSignals();

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("service-worker.js").catch(() => {});
  });
}
