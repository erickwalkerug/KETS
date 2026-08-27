const API_BASE = localStorage.getItem("KETS_API_BASE") || "";
const WEBSITE_URL = "https://my-btc-bot-l0xm.onrender.com";
const REFRESH_MS = 10000;

const state = {
  paid: false,
  signals: {},
  history: [],
  status: {},
  plans: {}
};

const $ = id => document.getElementById(id);

function api(path, options={}) {
  const headers = {"Accept":"application/json", ...(options.headers || {})};
  const key = localStorage.getItem("KETS_API_KEY");
  if (key) headers["X-KETS-API-KEY"] = key;
  return fetch(`${API_BASE}${path}`, {...options, headers}).then(async r => {
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
    return data;
  });
}

function money(v) {
  if (v == null) return "--";
  return "$" + Number(v).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
}

function countdown(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const h = String(Math.floor(sec/3600)).padStart(2,"0");
  const m = String(Math.floor((sec%3600)/60)).padStart(2,"0");
  const s = String(sec%60).padStart(2,"0");
  return `${h}:${m}:${s}`;
}

function renderSignals() {
  const grid = $("signalGrid");
  if (!state.paid) {
    grid.innerHTML = `<article class="signal-card"><div class="signal-top"><div class="market">BTC</div><div class="direction wait">LOCKED</div></div><div class="price">••••••</div><div class="strength">Live signal locked for unpaid users</div></article>
    <article class="signal-card"><div class="signal-top"><div class="market">GOLD</div><div class="direction wait">LOCKED</div></div><div class="price">••••••</div><div class="strength">Unlock a plan to view live signals</div></article>`;
    $("lockedCard").style.display = "flex";
    return;
  }
  $("lockedCard").style.display = "none";
  const cards = ["BTC","GOLD"].map(m => {
    const s = state.signals[m];
    if (!s) return `<article class="signal-card"><div class="signal-top"><div class="market">${m}</div><div class="direction wait">WAIT</div></div><div class="price">--</div><div class="strength">No current signal</div></article>`;
    const dir = (s.direction || "WAIT").toLowerCase();
    return `<article class="signal-card ${dir}">
      <div class="signal-top"><div class="market">KETS ${s.market}</div><div class="direction ${dir}">${s.direction}</div></div>
      <div class="price">${money(s.price)}</div>
      <div class="strength"><span>Strength ${s.strength}%</span><div class="bar"><i style="width:${Math.min(100,s.strength)}%"></i></div></div>
      <div class="metrics">
        <div class="metric"><span>Take Profit</span><strong>${money(s.take_profit)}</strong></div>
        <div class="metric"><span>Stop Loss</span><strong>${money(s.stop_loss)}</strong></div>
        <div class="metric"><span>Expected Move</span><strong>${money(s.expected_move)}</strong></div>
        <div class="metric"><span>Duration</span><strong>${s.estimated_duration || "--"}</strong></div>
      </div>
    </article>`;
  }).join("");
  grid.innerHTML = cards;
}

function renderHistory() {
  const el = $("historyList");
  if (!state.history.length) {
    el.innerHTML = `<div class="empty">No signals recorded in the last 7 days.</div>`;
    return;
  }
  el.innerHTML = state.history.slice().reverse().slice(0,80).map(s => {
    const dir = (s.direction || "").toLowerCase();
    const t = s.timestamp ? new Date(s.timestamp).toLocaleString() : "";
    return `<div class="history-row">
      <div><div class="history-market">${s.market || ""}</div><div class="history-meta">${t}</div></div>
      <div class="history-dir ${dir}">${s.direction || "--"} · ${s.strength || 0}%</div>
      <div class="history-price">${money(s.price)}</div>
    </div>`;
  }).join("");
}

function renderStatus() {
  const w = state.status.signal_window || {};
  $("engineStatus").textContent = w.active ? "Engine active" : "Outside signal hours";
  $("signalWindow").textContent = w.active ? countdown(w.seconds_to_stop) : countdown(w.seconds_to_start);
  $("windowLabel").textContent = w.active ? "Time left before signals stop" : "Until signals start at 06:00 EAT";
  $("nextBroadcast").textContent = countdown(state.status.next_broadcast_seconds || 0);
}

async function loadAll() {
  try {
    const [status, history, access] = await Promise.all([
      api("/api/status"),
      api("/api/history"),
      api("/api/access").catch(() => ({paid:false}))
    ]);
    state.status = status;
    state.history = history.history || [];
    state.paid = Boolean(access.paid);
    if (state.paid) {
      const live = await api("/api/signals");
      state.signals = live.signals || {};
    } else state.signals = {};
    renderStatus(); renderSignals(); renderHistory();
  } catch (e) {
    $("engineStatus").textContent = "Backend unavailable";
    console.error(e);
  }
}

async function loadPlans() {
  try {
    const data = await api("/api/plans");
    state.plans = data;
  } catch(e) {}
}

function openModal(html) {
  $("modalPanel").innerHTML = html;
  $("modal").classList.add("open");
}
function closeModal(){ $("modal").classList.remove("open"); }

function showPlans() {
  const p = state.plans.plans || {
    "1_day":{name:"1 Day",ugx:5000,usd:5},
    "1_week":{name:"1 Week",ugx:30000,usd:30},
    "1_month":{name:"1 Month",ugx:100000,usd:100},
    "1_year":{name:"1 Year",ugx:1000000,usd:1000}
  };
  openModal(`<button class="close" onclick="closeModal()">×</button><span class="eyebrow">UNLOCK KETS</span><h2>Choose your plan</h2><p class="country-note">Uganda: MTN/Airtel in UGX. Outside Uganda: bank/card payment in USD. Payment must be verified server-side before live signals unlock.</p>
  <div class="plan-grid">${Object.entries(p).map(([key,x])=>`<div class="plan"><h3>${x.name}</h3><div class="ugx">UGX ${Number(x.ugx).toLocaleString()}</div><p>International: $${x.usd}</p><button class="primary-btn" onclick="showPayment('${key}')">Continue</button></div>`).join("")}</div>`);
}

function showPayment(key) {
  const p = state.plans.plans?.[key];
  openModal(`<button class="close" onclick="closeModal()">×</button><span class="eyebrow">PAYMENT</span><h2>${p?.name || key}</h2>
  <div class="plan"><strong>Uganda</strong><p>MTN: ${state.plans.uganda?.mtn || "+256791058183"}<br>Airtel: ${state.plans.uganda?.airtel || "+256747427556"}<br>Amount: UGX ${Number(p?.ugx || 0).toLocaleString()}</p></div>
  <div class="plan"><strong>Outside Uganda</strong><p>Bank account: ${state.plans.international?.bank_account || "9030028492447"}<br>Amount: $${p?.usd || 0}</p></div>
  <p class="country-note">Do not enter card or mobile-money PINs into the app. Automatic access requires a real payment verification integration on the backend.</p>
  <button class="primary-btn" onclick="closeModal()">Done</button>`);
}

function showProfile() {
  const profile = JSON.parse(localStorage.getItem("KETS_PROFILE") || "{}");
  openModal(`<button class="close" onclick="closeModal()">×</button><span class="eyebrow">ACCOUNT</span><h2>Your profile</h2>
  <div class="profile-form">
    <input id="pname" placeholder="Profile name" value="${profile.name || ""}">
    <input id="puser" placeholder="Username" value="${profile.username || ""}">
    <select id="pcountry"><option value="">Choose country</option>${countryOptions(profile.country)}</select>
    <input id="ppic" placeholder="Profile picture URL (optional)" value="${profile.picture || ""}">
    <button class="primary-btn" onclick="saveProfile()">Save profile</button>
  </div>`);
}
function countryOptions(selected="") {
  const countries = ["Uganda","Kenya","Tanzania","Rwanda","South Africa","United States","United Kingdom","Canada","Australia","India","Nigeria","Ghana","United Arab Emirates","Other"];
  return countries.map(c=>`<option ${c===selected?"selected":""}>${c}</option>`).join("");
}
function saveProfile(){
  const profile={name:$("pname").value,username:$("puser").value,country:$("pcountry").value,picture:$("ppic").value};
  localStorage.setItem("KETS_PROFILE",JSON.stringify(profile));
  closeModal();
}
function showNotifications(){
  openModal(`<button class="close" onclick="closeModal()">×</button><span class="eyebrow">NOTIFICATIONS</span><h2>KETS notifications</h2><p class="country-note">Browser notifications require HTTPS and the user's permission. Push delivery also requires a push service and server-side notification integration.</p><button class="primary-btn" onclick="requestNotifications()">Enable notifications</button>`);
}
async function requestNotifications(){
  if (!("Notification" in window)) return alert("Notifications are not supported by this browser.");
  const permission = await Notification.requestPermission();
  alert(permission === "granted" ? "Notifications enabled on this device." : "Notification permission was not granted.");
}

document.addEventListener("click", e => {
  const view = e.target.closest("[data-view]")?.dataset.view;
  if (view === "plans") showPlans();
  if (view === "profile") showProfile();
  if (view === "signals") window.scrollTo({top:document.querySelector(".section-head")?.offsetTop || 0,behavior:"smooth"});
  if (view === "home") window.scrollTo({top:0,behavior:"smooth"});
});
$("notificationBtn").onclick = showNotifications;
$("profileBtn").onclick = showProfile;
$("modal").addEventListener("click", e => { if(e.target.classList.contains("modal-backdrop")) closeModal(); });

$("androidLink").href = WEBSITE_URL + "#android";
$("iphoneLink").href = WEBSITE_URL + "#iphone";
$("websiteLink").href = WEBSITE_URL;

loadPlans();
loadAll();
setInterval(loadAll, REFRESH_MS);
