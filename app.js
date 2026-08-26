// ============================================================
// KETS APP FRONTEND
//
// The KETS frontend now talks to its own backend.
// The KETS backend is the connector to the ORIGINAL TRADING BOT.
//
// Browser flow:
// ORIGINAL TRADING BOT
//        ↓
// KETS backend
//        ↓
// this app.js
//
// No direct Twelve Data connection is made by the app.
// ============================================================

const API_BASE = window.location.origin;

const API_SIGNALS = `${API_BASE}/api/signals`;
const API_STATUS = `${API_BASE}/api/status`;
const API_MARKET = `${API_BASE}/api/market`;

const WEBSITE_URL =
  window.KETS_WEBSITE_URL ||
  "#";

const REFRESH_MS = 120000;

let nextRefreshAt =
  Date.now() + REFRESH_MS;

let cachedSignals = [];

const $ = id =>
  document.getElementById(id);


function money(value){

  if (
    typeof value === "number" &&
    Number.isFinite(value)
  ){

    return "$" +
      value.toLocaleString(
        undefined,
        {
          maximumFractionDigits: 2
        }
      );
  }

  return value ?? "--";
}


function esc(value){

  return String(
    value ?? ""
  ).replace(
    /[&<>"']/g,
    char => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#039;"
    }[char])
  );
}


function hasAccess(){

  const until =
    Number(
      localStorage.getItem(
        "kets_access_until"
      ) || 0
    );

  return until > Date.now();
}


function updateAccess(){

  if (!$("accessState"))
    return;

  const until =
    Number(
      localStorage.getItem(
        "kets_access_until"
      ) || 0
    );

  const active =
    until > Date.now();

  $("accessState").textContent =
    active ? "ACTIVE" : "LOCKED";

  if (!active){

    $("countdown").textContent =
      "Payment required";

    return;
  }

  const seconds =
    Math.max(
      0,
      Math.floor(
        (until - Date.now()) / 1000
      )
    );

  const h =
    Math.floor(
      seconds / 3600
    );

  const m =
    Math.floor(
      (seconds % 3600) / 60
    );

  const s =
    seconds % 60;

  $("countdown").textContent =
    `Access ends in ` +
    `${String(h).padStart(2,"0")}:` +
    `${String(m).padStart(2,"0")}:` +
    `${String(s).padStart(2,"0")}`;
}


function renderMarket(markets){

  const entries =
    Object.values(
      markets || {}
    );

  if (!$("marketData"))
    return;

  $("marketData").innerHTML =
    entries.length
      ? entries.map(m => `
        <article class="signal">

          <div class="row">
            <b>${esc(m.asset)}</b>
            <span>${esc(m.symbol || "")}</span>
          </div>

          <div class="row">
            <span>Live price</span>
            <b>${money(m.price)}</b>
          </div>

          <div class="row">
            <span>Latest candle</span>
            <span>
              ${esc(m.candle_time || "--")}
            </span>
          </div>

          <div class="row">
            <span>Original bot signal</span>
            <b>
              ${esc(
                m.signal ||
                "NO SIGNAL"
              )}
            </b>
          </div>

          <div class="row">
            <span>Strength</span>
            <b>
              ${esc(
                m.score ?? "--"
              )}${m.score != null ? "%" : ""}
            </b>
          </div>

          <div class="row">
            <span>Source update</span>
            <span>
              ${esc(
                m.updated_at ||
                "--"
              )}
            </span>
          </div>

        </article>
      `).join("")
      : `
        <div class="empty">
          Waiting for original trading bot data…
        </div>
      `;
}


function renderSignal(
  signal,
  locked
){

  const direction =
    String(
      signal.direction ||
      signal.signal ||
      ""
    ).toUpperCase();

  const cls =
    direction.includes("BUY")
      ? "buy"
      : direction.includes("SELL")
        ? "sell"
        : "";

  const body = `

    <div class="row">
      <b class="${cls}">
        ${esc(
          direction ||
          "SIGNAL"
        )}
      </b>

      <span>
        ${esc(
          signal.asset ||
          signal.market ||
          ""
        )}
      </span>
    </div>

    <div class="row">
      <span>Entry</span>
      <b>
        ${money(
          signal.entry ??
          signal.price
        )}
      </b>
    </div>

    <div class="row">
      <span>Take Profit</span>
      <b>
        ${money(
          signal.take_profit ??
          signal.tp
        )}
      </b>
    </div>

    <div class="row">
      <span>Stop Loss</span>
      <b>
        ${money(
          signal.stop_loss ??
          signal.sl
        )}
      </b>
    </div>

    <div class="row">
      <span>Strength</span>
      <b>
        ${esc(
          signal.score ??
          "--"
        )}%
      </b>
    </div>

    <div class="row">
      <span>Time</span>
      <span>
        ${esc(
          signal.timestamp ??
          signal.time ??
          ""
        )}
      </span>
    </div>

    ${
      locked
        ? `
          <div class="lock-note">
            🔒 Unlock with an active
            payment plan.
          </div>
        `
        : ""
    }
  `;

  return `
    <article
      class="signal ${
        locked ? "locked" : ""
      }"
    >
      ${body}
    </article>
  `;
}


function renderSignals(data){

  const signals =
    Array.isArray(data)
      ? data
      : (
          data.signals ||
          []
        );

  cachedSignals =
    signals;

  const active =
    hasAccess();

  const latest =
    signals.slice(0, 10);

  if ($("signals")){

    $("signals").innerHTML =
      latest.length
        ? latest.map(
            signal =>
              renderSignal(
                signal,
                !active
              )
          ).join("")
        : `
          <div class="empty">
            No new signals.
          </div>
        `;
  }

  const history =
    signals.slice(10);

  if ($("history")){

    $("history").innerHTML =
      history.length
        ? history.map(
            signal => {

              const date =
                new Date(
                  signal.timestamp ||
                  signal.time ||
                  0
                );

              return `
                <div class="history-item">

                  <div class="row">
                    <b>
                      ${esc(
                        signal.direction ||
                        signal.signal ||
                        "SIGNAL"
                      )}
                    </b>

                    <span>
                      ${esc(
                        signal.asset ||
                        signal.market ||
                        ""
                      )}
                    </span>
                  </div>

                  <div class="row">

                    <span>
                      ${
                        Number.isNaN(
                          date.getTime()
                        )
                          ? ""
                          : date.toLocaleString()
                      }
                    </span>

                    <span>
                      ${money(
                        signal.entry ??
                        signal.price
                      )}
                    </span>

                  </div>

                </div>
              `;
            }
          ).join("")
        : `
          <div class="empty">
            No previous signals.
          </div>
        `;
  }
}


async function getJSON(url){

  const response =
    await fetch(
      url,
      {
        cache: "no-store"
      }
    );

  if (!response.ok){

    throw new Error(
      `HTTP ${response.status}`
    );
  }

  return response.json();
}


async function loadBackend(){

  try{

    const [
      status,
      signals,
      market
    ] = await Promise.all([
      getJSON(API_STATUS),
      getJSON(API_SIGNALS),
      getJSON(API_MARKET)
    ]);

    renderSignals(
      signals
    );

    renderMarket(
      market.markets
    );

    const connected =
      status.source_connected === true;

    if ($("statusText")){

      $("statusText").textContent =
        connected
          ? "Original trading bot connected"
          : "Waiting for original trading bot";
    }

    if ($("backendTime")){

      $("backendTime").textContent =
        status.time_eat
          ? new Date(
              status.time_eat
            ).toLocaleString()
          : "--";
    }

    nextRefreshAt =
      Date.now() +
      REFRESH_MS;

  }catch(error){

    console.warn(
      "KETS backend:",
      error
    );

    if ($("statusText")){

      $("statusText").textContent =
        "KETS backend unavailable";
    }

    nextRefreshAt =
      Date.now() +
      REFRESH_MS;
  }
}


function countdownTick(){

  const remaining =
    Math.max(
      0,
      nextRefreshAt -
      Date.now()
    );

  const seconds =
    Math.ceil(
      remaining / 1000
    );

  if ($("nextCheck")){

    $("nextCheck").textContent =
      `${Math.floor(seconds / 60)}:` +
      `${String(
        seconds % 60
      ).padStart(2,"0")}`;
  }

  updateAccess();
}


document
  .querySelectorAll(
    "[data-plan]"
  )
  .forEach(button => {

    button.addEventListener(
      "click",
      () => {

        alert(
          `Selected ${button.dataset.plan}. ` +
          `Payment gateway still needs ` +
          `to be connected before real ` +
          `payment is accepted.`
        );
      }
    );
  });


let deferredPrompt;


window.addEventListener(
  "beforeinstallprompt",
  event => {

    event.preventDefault();

    deferredPrompt =
      event;

    if ($("installBtn"))
      $("installBtn").hidden = false;
  }
);


if ($("installBtn")){

  $("installBtn").addEventListener(
    "click",
    async () => {

      if (!deferredPrompt)
        return;

      deferredPrompt.prompt();

      await deferredPrompt.userChoice;

      deferredPrompt =
        null;

      $("installBtn").hidden =
        true;
    }
  );
}


if ($("websiteLink")){

  $("websiteLink").href =
    WEBSITE_URL;
}


loadBackend();


setInterval(
  loadBackend,
  REFRESH_MS
);


setInterval(
  countdownTick,
  1000
);


countdownTick();


if (
  "serviceWorker" in navigator
){

  navigator.serviceWorker
    .register(
      "service-worker.js"
    )
    .catch(
      console.warn
    );
}
