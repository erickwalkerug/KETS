// ============================================================
// KETS APP FRONTEND — RESILIENT DASHBOARD
// Dashboard is visible 24/7.
// Signal generation window: 06:00–18:00 East Africa Time (UTC+3).
// The ORIGINAL TRADING BOT remains the source of signal data.
// ============================================================

const API_BASE = window.location.origin;
const API_SIGNALS = `${API_BASE}/api/signals`;
const API_STATUS  = `${API_BASE}/api/status`;
const API_MARKET  = `${API_BASE}/api/market`;

const REFRESH_MS = 120000;
let nextRefreshAt = Date.now() + REFRESH_MS;
let cachedSignals = [];

const $ = id => document.getElementById(id);

function money(value) {
  if (typeof value === "number" && Number.isFinite(value)) {
    return "$" + value.toLocaleString(undefined, { maximumFractionDigits: 2 });
  }
  return value ?? "--";
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, char => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#039;"
  }[char]));
}

// Uganda / East Africa Time. The browser can be in another timezone,
// so the signal window is calculated explicitly as UTC+3.
function eatNow() {
  const now = new Date();
  const utcMs = now.getTime() + now.getTimezoneOffset() * 60000;
  return new Date(utcMs + 3 * 60 * 60000);
}

function signalHoursOpen() {
  const d = eatNow();
  const minutes = d.getHours() * 60 + d.getMinutes();
  return minutes >= 360 && minutes < 1080; // 06:00 inclusive to 18:00 exclusive
}

function updateSignalWindow() {
  const open = signalHoursOpen();

  if ($("nextCheck")) {
    $("nextCheck").textContent = open ? "2:00" : "06:00 EAT";
  }

  const liveLabel = document.querySelector(".section-title span");
  if (liveLabel && $("signals")) {
    // Only change the Latest Signals section label.
    const headings = document.querySelectorAll(".section-title");
    for (const h of headings) {
      const title = h.querySelector("h2");
      const span = h.querySelector("span");
      if (title && span && title.textContent.trim() === "Latest Signals") {
        span.textContent = open ? "Live" : "Paused";
      }
    }
  }

  if ($("statusText")) {
    const current = $("statusText").dataset.backendState || "";
    if (!open) {
      $("statusText").textContent = current === "connected"
        ? "Connected — scanning paused until 06:00 EAT"
        : "Dashboard online — signals start 06:00 EAT";
    } else if (current === "connected") {
      $("statusText").textContent = "Original trading bot connected";
    } else if (current === "error") {
      $("statusText").textContent = "KETS backend unavailable";
    } else {
      $("statusText").textContent = "Connecting...";
    }
  }
}

function hasAccess() {
  const until = Number(localStorage.getItem("kets_access_until") || 0);
  return until > Date.now();
}

function updateAccess() {
  if (!$("accessState")) return;

  const until = Number(localStorage.getItem("kets_access_until") || 0);
  const active = until > Date.now();

  $("accessState").textContent = active ? "ACTIVE" : "LOCKED";

  if (!active) {
    $("countdown").textContent = "Payment required";
    return;
  }

  const seconds = Math.max(0, Math.floor((until - Date.now()) / 1000));
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;

  $("countdown").textContent =
    `Access ends in ${String(h).padStart(2,"0")}:` +
    `${String(m).padStart(2,"0")}:${String(s).padStart(2,"0")}`;
}

function renderMarket(markets) {
  const entries = Object.values(markets || {});
  if (!$("marketData")) return;

  $("marketData").innerHTML = entries.length
    ? entries.map(m => `
      <article class="signal">
        <div class="row"><b>${esc(m.asset)}</b><span>${esc(m.symbol || "")}</span></div>
        <div class="row"><span>Live price</span><b>${money(m.price)}</b></div>
        <div class="row"><span>Latest candle</span><span>${esc(m.candle_time || "--")}</span></div>
        <div class="row"><span>Original bot signal</span><b>${esc(m.signal || "NO SIGNAL")}</b></div>
        <div class="row"><span>Strength</span><b>${esc(m.score ?? "--")}${m.score != null ? "%" : ""}</b></div>
        <div class="row"><span>Source update</span><span>${esc(m.updated_at || "--")}</span></div>
      </article>
    `).join("")
    : `<div class="empty">Waiting for original trading bot data…</div>`;
}

function renderSignal(signal, locked) {
  const direction = String(signal.direction || signal.signal || "").toUpperCase();
  const cls = direction.includes("BUY") ? "buy" : direction.includes("SELL") ? "sell" : "";

  return `
    <article class="signal ${locked ? "locked" : ""}">
      <div class="row"><b class="${cls}">${esc(direction || "SIGNAL")}</b><span>${esc(signal.asset || signal.market || "")}</span></div>
      <div class="row"><span>Entry</span><b>${money(signal.entry ?? signal.price)}</b></div>
      <div class="row"><span>Take Profit</span><b>${money(signal.take_profit ?? signal.tp)}</b></div>
      <div class="row"><span>Stop Loss</span><b>${money(signal.stop_loss ?? signal.sl)}</b></div>
      <div class="row"><span>Strength</span><b>${esc(signal.score ?? "--")}%</b></div>
      <div class="row"><span>Time</span><span>${esc(signal.timestamp ?? signal.time ?? "")}</span></div>
      ${locked ? `<div class="lock-note">🔒 Unlock with an active payment plan.</div>` : ""}
    </article>
  `;
}

function renderSignals(data) {
  const signals = Array.isArray(data) ? data : (data?.signals || []);
  cachedSignals = signals;

  const latest = signals.slice(0, 10);
  const active = hasAccess();

  if ($("signals")) {
    $("signals").innerHTML = latest.length
      ? latest.map(s => renderSignal(s, !active)).join("")
      : `<div class="empty">${
          signalHoursOpen()
            ? "No new signals."
            : "Signal scanning paused. Resumes at 06:00 EAT."
        }</div>`;
  }

  const history = signals.slice(10);

  if ($("history")) {
    $("history").innerHTML = history.length
      ? history.map(signal => {
          const date = new Date(signal.timestamp || signal.time || 0);
          return `
            <div class="history-item">
              <div class="row">
                <b>${esc(signal.direction || signal.signal || "SIGNAL")}</b>
                <span>${esc(signal.asset || signal.market || "")}</span>
              </div>
              <div class="row">
                <span>${Number.isNaN(date.getTime()) ? "" : date.toLocaleString()}</span>
                <span>${money(signal.entry ?? signal.price)}</span>
              </div>
            </div>`;
        }).join("")
      : `<div class="empty">No previous signals.</div>`;
  }
}

async function getJSON(url) {
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

// IMPORTANT: Each API request is independent.
// A missing /api/market endpoint must NOT prevent the dashboard from rendering.
async function loadBackend() {
  let connected = false;

  // Status
  try {
    const status = await getJSON(API_STATUS);
    connected = status.source_connected === true;

    if ($("backendTime")) {
      $("backendTime").textContent = status.time_eat
        ? new Date(status.time_eat).toLocaleString()
        : "--";
    }
  } catch (error) {
    console.warn("KETS status:", error);
  }

  // Signals
  try {
    const signals = await getJSON(API_SIGNALS);
    renderSignals(signals);
  } catch (error) {
    console.warn("KETS signals:", error);
    if ($("signals") && signalHoursOpen()) {
      $("signals").innerHTML = `<div class="empty">Waiting for signal data from the original bot…</div>`;
    }
  }

  // Market
  try {
    const market = await getJSON(API_MARKET);
    renderMarket(market?.markets || {});
  } catch (error) {
    console.warn("KETS market:", error);
    if ($("marketData")) {
      $("marketData").innerHTML =
        `<div class="empty">Waiting for original trading bot data…</div>`;
    }
  }

  if ($("statusText")) {
    $("statusText").dataset.backendState = connected ? "connected" : "error";
  }

  nextRefreshAt = Date.now() + REFRESH_MS;
  updateSignalWindow();
}

function countdownTick() {
  const remaining = Math.max(0, nextRefreshAt - Date.now());
  const seconds = Math.ceil(remaining / 1000);

  if ($("nextCheck") && signalHoursOpen()) {
    $("nextCheck").textContent =
      `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2,"0")}`;
  }

  updateAccess();
  updateSignalWindow();
}

document.querySelectorAll("[data-plan]").forEach(button => {
  button.addEventListener("click", () => {
    alert(
      `Selected ${button.dataset.plan}. ` +
      `Payment gateway still needs to be connected before real payment is accepted.`
    );
  });
});

let deferredPrompt;

window.addEventListener("beforeinstallprompt", event => {
  event.preventDefault();
  deferredPrompt = event;
  if ($("installBtn")) $("installBtn").hidden = false;
});

if ($("installBtn")) {
  $("installBtn").addEventListener("click", async () => {
    if (!deferredPrompt) return;
    deferredPrompt.prompt();
    await deferredPrompt.userChoice;
    deferredPrompt = null;
    $("installBtn").hidden = true;
  });
}

if ($("websiteLink")) {
  $("websiteLink").href = window.KETS_WEBSITE_URL || "#";
}

// Render dashboard state immediately — never wait for the backend.
updateSignalWindow();
updateAccess();
loadBackend();

setInterval(loadBackend, REFRESH_MS);
setInterval(countdownTick, 1000);

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("service-worker.js?v=20260827").catch(console.warn);
}
