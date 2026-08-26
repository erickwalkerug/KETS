const API_BASE = "https://my-btc-bot-l0xm.onrender.com";
const API_SIGNALS = `${API_BASE}/api/signals`;
const API_STATUS = `${API_BASE}/api/status`;
const WEBSITE_URL = "https://example.com"; // Replace with your real KETS website.

const REFRESH_MS = 120000; // Official KETS interval: 2 minutes. Do not change to 10 seconds.
let nextRefreshAt = Date.now();
let cachedSignals = [];

const $ = id => document.getElementById(id);

function money(v){ return typeof v === "number" ? "$"+v.toLocaleString(undefined,{maximumFractionDigits:2}) : (v ?? "--"); }
function esc(s){ return String(s ?? "").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }

function hasAccess(){
  const until = Number(localStorage.getItem("kets_access_until") || 0);
  return until > Date.now();
}

function updateAccess(){
  const until = Number(localStorage.getItem("kets_access_until") || 0);
  const active = until > Date.now();
  $("accessState").textContent = active ? "ACTIVE" : "LOCKED";
  $("accessState").style.color = active ? "#35df91" : "#ffb84d";
  if(!active){ $("countdown").textContent = "Payment required"; return; }
  const seconds = Math.max(0, Math.floor((until-Date.now())/1000));
  const h = Math.floor(seconds/3600), m = Math.floor((seconds%3600)/60), s = seconds%60;
  $("countdown").textContent = `Access ends in ${String(h).padStart(2,"0")}:${String(m).padStart(2,"0")}:${String(s).padStart(2,"0")}`;
}

function renderSignal(s, locked){
  const direction = String(s.direction || s.signal || "").toUpperCase();
  const cls = direction.includes("BUY") ? "buy" : direction.includes("SELL") ? "sell" : "";
  const body = `
    <div class="row"><b class="${cls}">${esc(direction || "SIGNAL")}</b><span>${esc(s.asset || s.market || "")}</span></div>
    <div class="row"><span>Price</span><b>${money(s.entry ?? s.price)}</b></div>
    <div class="row"><span>Take Profit</span><b>${money(s.take_profit ?? s.tp)}</b></div>
    <div class="row"><span>Stop Loss</span><b>${money(s.stop_loss ?? s.sl)}</b></div>
    <div class="row"><span>Strength</span><b>${esc(s.score ?? "--")}%</b></div>
    <div class="row"><span>Time</span><span>${esc(s.timestamp ?? s.time ?? "")}</span></div>
    ${locked ? '<div class="lock-note">🔒 Unlock with an active payment plan.</div>' : ''}
  `;
  return `<article class="signal ${locked ? "locked":""}">${body}</article>`;
}

function render(data){
  const signals = Array.isArray(data) ? data : (data.signals || []);
  cachedSignals = signals;
  const active = hasAccess();
  const latest = signals.slice(0,10);
  $("signals").innerHTML = latest.length ? latest.map(s=>renderSignal(s,!active)).join("") : '<div class="empty">No new signals.</div>';

  const history = signals.slice(10);
  $("history").innerHTML = history.length ? history.map(s=>{
    const d = new Date(s.timestamp || s.time || 0);
    return `<div class="history-item"><div class="row"><b>${esc(s.direction || s.signal || "SIGNAL")}</b><span>${esc(s.asset || s.market || "")}</span></div><div class="row"><span>${isNaN(d)?"":d.toLocaleString()}</span><span>${money(s.entry ?? s.price)}</span></div></div>`;
  }).join("") : '<div class="empty">No previous signals.</div>';
}

async function loadSignals(){
  try{
    const r = await fetch(API_SIGNALS,{cache:"no-store"});
    if(!r.ok) throw new Error("Signals HTTP "+r.status);
    const data = await r.json();
    render(data);
    $("statusText").textContent = "Connected";
  }catch(e){
    $("statusText").textContent = "Backend unavailable";
    console.warn(e);
  }
  nextRefreshAt = Date.now()+REFRESH_MS;
}

async function loadStatus(){
  try{
    const r = await fetch(API_STATUS,{cache:"no-store"});
    if(r.ok){
      const data = await r.json();
      if(data.website_url) $("websiteLink").href = data.website_url;
    }
  }catch(e){}
}

function countdownTick(){
  const remaining = Math.max(0,nextRefreshAt-Date.now());
  const s = Math.ceil(remaining/1000);
  $("nextCheck").textContent = `${Math.floor(s/60)}:${String(s%60).padStart(2,"0")}`;
  updateAccess();
}

document.querySelectorAll("[data-plan]").forEach(btn=>{
  btn.addEventListener("click",()=>{
    // Payment gateway integration is intentionally left for the provider/account details.
    alert(`Selected ${btn.dataset.plan}. Connect your payment gateway before accepting real payments.`);
  });
});

let deferredPrompt;
window.addEventListener("beforeinstallprompt",e=>{
  e.preventDefault(); deferredPrompt=e; $("installBtn").hidden=false;
});
$("installBtn").addEventListener("click",async()=>{
  if(!deferredPrompt) return;
  deferredPrompt.prompt();
  await deferredPrompt.userChoice;
  deferredPrompt=null;
  $("installBtn").hidden=true;
});

$("websiteLink").href = WEBSITE_URL;
loadSignals();
loadStatus();
setInterval(loadSignals, REFRESH_MS);
setInterval(countdownTick,1000);
countdownTick();

if("serviceWorker" in navigator) navigator.serviceWorker.register("service-worker.js").catch(console.warn);
