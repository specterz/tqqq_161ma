/*
 * app.js — boots Pyodide, loads the REAL Python engine (docs/py/*.py) and data
 * (docs/data/*.csv) into its virtual filesystem, then calls web_api.run_json so
 * the browser produces the exact same results as the CLI. No JS re-port of the
 * math -> no drift.
 */

const $ = (id) => document.getElementById(id);
let pyodide = null;
let equityChart = null;
let signalChart = null;

// --- Boot Pyodide + mount engine and data ----------------------------------
async function boot() {
  setStatus("Loading Python runtime… (first load downloads ~10 MB, then caches)");
  pyodide = await loadPyodide();

  setStatus("Loading pandas…");
  await pyodide.loadPackage(["pandas", "numpy"]);

  setStatus("Mounting engine + data…");
  const manifest = await (await fetch("manifest.json")).json();

  // Create dirs in Pyodide's in-memory FS.
  pyodide.FS.mkdirTree("/app/py");
  pyodide.FS.mkdirTree("/app/data");

  // Fetch each Python module and data CSV, write into the FS.
  await Promise.all([
    ...manifest.py.map(async (f) => {
      const txt = await (await fetch(`py/${f}`)).text();
      pyodide.FS.writeFile(`/app/py/${f}`, txt);
    }),
    ...manifest.data.map(async (f) => {
      const txt = await (await fetch(`data/${f}`)).text();
      pyodide.FS.writeFile(`/app/data/${f}`, txt);
    }),
  ]);

  // Make the engine importable, and point its DATA_DIR at our mounted data.
  // data.py resolves DATA_DIR relative to its own file (../data), which for
  // /app/py/data.py is /app/data — exactly where we wrote the CSVs.
  await pyodide.runPythonAsync(`
import sys
sys.path.insert(0, "/app/py")
import web_api
`);

  setStatus("");
  $("runBtn").disabled = false;
  $("runBtn").textContent = "Run backtest";
  run(); // first render with defaults
}

// --- Read the form into a config dict (mirrors CLI args) --------------------
function readConfig() {
  const mode = $("mode").value;
  const ma = ($("ma").value.match(/\d+/g) || ["161"]).map(Number);
  const leverage = Array.from($("leverage").selectedOptions).map((o) => Number(o.value));
  const overheatingOn = $("overheatingEnabled").checked;
  return {
    mode,
    ma,
    leverage: leverage.length ? leverage : [3],
    overheating: overheatingOn ? Number($("overheatBand").value) : null,
    start: $("start").value || null,
    end: $("end").value || null,
    initial: Number($("initial").value) || 10000,
    contribution: Number($("contribution").value) || 100,
    every: Math.max(1, parseInt($("every").value, 10) || 21),
    initialLumpSum: Number($("initialLumpSum").value) || 0,
    commission: Number($("commission").value) || 0,
    financingSpread: Number($("financingSpread").value) / 100.0,
    expenseRatio: Number($("expenseRatio").value) / 100.0,
    wantMonthly: $("wantMonthly").checked,
  };
}

// --- Run the backtest through Python ----------------------------------------
async function run() {
  if (!pyodide) return;
  const cfg = readConfig();
  setStatus("Computing…");
  const t0 = performance.now();
  let res;
  try {
    // Pass config as JSON to Python; get JSON back.
    pyodide.globals.set("cfg_json", JSON.stringify(cfg));
    const out = await pyodide.runPythonAsync(`web_api.run_json(cfg_json)`);
    res = JSON.parse(out);
  } catch (e) {
    setStatus("Error: " + e.message, true);
    return;
  }
  if (res.error) { setStatus(res.error, true); return; }

  renderTable(res);
  renderEquity(res);
  renderSignal(res);
  renderMonthly(res);
  const ms = (performance.now() - t0).toFixed(0);
  setStatus(`${res.meta.start} → ${res.meta.end} · ${res.meta.tradingDays.toLocaleString()} days · ${ms} ms`);
}

// --- Renderers --------------------------------------------------------------
const pct = (v, signed = true) =>
  v == null ? "n/a" : `${signed && v >= 0 ? "+" : ""}${(v * 100).toFixed(1)}%`;
const money = (v) => "$" + Math.round(v).toLocaleString();

function renderTable(res) {
  $("annHead").textContent = res.meta.annualizedKind; // CAGR or IRR
  const tb = $("resultsTable").querySelector("tbody");
  tb.innerHTML = "";
  const strategyLabels = new Set();
  for (const r of res.results) {
    const isStrat = /MA/.test(r.label) && !/Hold|DCA/.test(r.label);
    const tr = document.createElement("tr");
    if (isStrat) tr.className = "strategy";
    const cells = [
      r.label,
      money(r.invested),
      money(r.finalValue),
      pct(r.totalReturn),
      pct(r.annualized),
      r.sharpe == null ? "n/a" : r.sharpe.toFixed(2),
      pct(r.maxDrawdown, false),
      r.entries.toLocaleString(),
      r.exits.toLocaleString(),
      r.contribs.toLocaleString(),
      money(r.commissionPaid),
    ];
    cells.forEach((c, i) => {
      const td = document.createElement("td");
      td.textContent = c;
      if (i === 3 || i === 4) td.className = (r.annualized ?? 0) >= 0 ? "pos" : "neg";
      tr.appendChild(td);
    });
    tb.appendChild(tr);
  }
  $("metaLine").textContent =
    `${res.meta.leverages.join(", ")} · MA ${res.meta.maWindows.join("/")} · ` +
    (res.meta.overheating == null ? "overheating off" : `overheating +${res.meta.overheating}%`);
  $("resultsCard").classList.remove("hidden");
}

const palette = ["#4c8dff", "#f0883e", "#3fb950", "#a371f7", "#f85149", "#56d4dd"];

function renderEquity(res) {
  const ctx = $("equityChart");
  const datasets = res.results.map((r, i) => ({
    label: r.label,
    data: r.equity.values,
    borderColor: palette[i % palette.length],
    borderWidth: /Hold|DCA/.test(r.label) ? 1 : 2,
    borderDash: /Hold|DCA/.test(r.label) ? [5, 4] : [],
    pointRadius: 0,
    tension: 0.1,
  }));
  const labels = res.results[0].equity.dates;
  if (equityChart) equityChart.destroy();
  equityChart = new Chart(ctx, {
    type: "line",
    data: { labels, datasets },
    options: baseChartOpts(true),
  });
  $("equityCard").classList.remove("hidden");
}

function renderSignal(res) {
  const ctx = $("signalChart");
  const sp = res.signalPanel;
  const datasets = [
    { label: "QQQ", data: sp.qqq.values, borderColor: "#4c8dff", borderWidth: 1.5, pointRadius: 0, tension: 0.1 },
    ...sp.mas.map((m, i) => ({
      label: `${m.window}-day MA`,
      data: m.line.values,
      borderColor: palette[(i + 1) % palette.length],
      borderWidth: 1.5, pointRadius: 0, tension: 0.1,
    })),
  ];
  if (signalChart) signalChart.destroy();
  signalChart = new Chart(ctx, {
    type: "line",
    data: { labels: sp.qqq.dates, datasets },
    options: baseChartOpts(true),
  });
  $("signalCard").classList.remove("hidden");
}

function baseChartOpts(log) {
  return {
    responsive: true,
    maintainAspectRatio: false,
    interaction: { mode: "index", intersect: false },
    scales: {
      x: { ticks: { color: "#8b98a9", maxTicksLimit: 6 }, grid: { color: "#1b2636" } },
      y: {
        type: log ? "logarithmic" : "linear",
        ticks: { color: "#8b98a9" }, grid: { color: "#1b2636" },
      },
    },
    plugins: {
      legend: { labels: { color: "#e6edf3", boxWidth: 12, font: { size: 11 } } },
      tooltip: { callbacks: { label: (c) => `${c.dataset.label}: $${Math.round(c.parsed.y).toLocaleString()}` } },
    },
  };
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
function renderMonthly(res) {
  const card = $("monthlyCard");
  if (!res.monthly || !res.monthly.rows || !res.monthly.rows.length) {
    card.classList.add("hidden");
    return;
  }
  const m = res.monthly;
  $("monthlyLabel").textContent = m.label || "";
  const cell = (v) => (v == null ? "" : `${v >= 0 ? "+" : ""}${v.toFixed(1)}%`);
  const cls = (v) => (v == null ? "" : v >= 0 ? "pos" : "neg");
  let html = "<thead><tr><th>Year</th>" +
    MONTHS.map((mo) => `<th>${mo}</th>`).join("") + "<th>YTD</th></tr></thead><tbody>";
  for (const row of m.rows) {
    html += `<tr><td>${row.year}</td>` +
      row.months.map((v) => `<td class="${cls(v)}">${cell(v)}</td>`).join("") +
      `<td class="${cls(row.ytd)}">${cell(row.ytd)}</td></tr>`;
  }
  html += `<tr><td class="foot">Sum</td>` +
    m.sum.map((v) => `<td class="foot ${cls(v)}">${cell(v)}</td>`).join("") +
    `<td class="foot ${cls(m.sumYtd)}">${cell(m.sumYtd)}</td></tr>`;
  html += `<tr><td class="foot">Avg</td>` +
    m.avg.map((v) => `<td class="foot ${cls(v)}">${cell(v)}</td>`).join("") +
    `<td class="foot ${cls(m.avgYtd)}">${cell(m.avgYtd)}</td></tr>`;
  html += "</tbody>";
  $("monthlyTable").innerHTML = html;
  card.classList.remove("hidden");
}

// --- UI wiring --------------------------------------------------------------
function setStatus(msg, isError = false) {
  const el = $("status");
  el.textContent = msg;
  el.classList.toggle("error", isError);
}

function syncModeVisibility() {
  const dca = $("mode").value === "contributions";
  document.querySelectorAll(".mode-dca").forEach((e) => e.classList.toggle("hidden", !dca));
  document.querySelectorAll(".mode-lump").forEach((e) => e.classList.toggle("hidden", dca));
}

function wire() {
  $("mode").addEventListener("change", () => { syncModeVisibility(); run(); });
  $("overheatingEnabled").addEventListener("change", () => {
    $("overheatBandWrap").classList.toggle("hidden", !$("overheatingEnabled").checked);
    run();
  });
  $("runBtn").addEventListener("click", run);
  syncModeVisibility();
}

wire();
boot().catch((e) => setStatus("Failed to start: " + e.message, true));
