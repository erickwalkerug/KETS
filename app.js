const API_BASE = window.location.origin;
const REFRESH_MS = 10000;
let statusTick = null;

const state = { paid:false, token:localStorage.getItem('kets_access_token')||'', signals:{}, history:[], status:{}, plans:{} };
const $ = id => document.getElementById(id);

async function api(path){
  const r = await fetch(`${API_BASE}${path}`, {headers:{Accept:"application/json", ...(state.token?{Authorization:`Bearer ${state.token}`}:{})}});
  const data = await r.json().catch(()=>({}));
  if(!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}

function money(v){
  if(v == null || !Number.isFinite(Number(v))) return "--";
  return "$" + Number(v).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
}
function countdown(sec){
  sec=Math.max(0,Math.floor(Number(sec)||0));
  return `${String(Math.floor(sec/3600)).padStart(2,"0")}:${String(Math.floor((sec%3600)/60)).padStart(2,"0")}:${String(sec%60).padStart(2,"0")}`;
}
function esc(v){
  return String(v ?? "").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"}[c]));
}
function formatMove(v,pct){
  if(v==null) return "--";
  const n=Number(v), sign=n>0?"+":"";
  return `${sign}${money(Math.abs(n))}${pct==null?"":` (${sign}${Number(pct).toFixed(2)}%)`}`;
}
function signalAge(ts){
  const start=Date.parse(ts);
  if(!Number.isFinite(start)) return "--";
  return countdown((Date.now()-start)/1000);
}

function renderSignals(){
  const grid=$("signalGrid");
  if(!state.paid){
    grid.innerHTML=["BTC","GOLD"].map(m=>`
      <article class="signal-card locked">
        <div class="signal-top"><div class="market">${m}</div><div class="direction wait">LOCKED</div></div>
        <div class="price">••••••</div>
        <div class="strength">Live signal hidden until access is unlocked.</div>
      </article>`).join("");
    $("lockedCard").style.display="flex";
    return;
  }
  $("lockedCard").style.display="none";
  const markets=["BTC","GOLD"];
  grid.innerHTML=markets.map(m=>{
    const s=state.signals[m];
    if(!s) return `<article class="signal-card"><div class="signal-top"><div class="market">${m}</div><div class="direction wait">WAIT</div></div><div class="price">--</div><div class="strength">No current signal.</div></article>`;
    const dir=(s.direction||"WAIT").toLowerCase();
    return `<article class="signal-card ${dir}">
      <div class="signal-top"><div class="market">KETS ${esc(s.market||m)}</div><div class="direction ${dir}">${esc(s.direction)}</div></div>
      <div class="price">${money(s.current_price??s.price)}</div>
      <div class="entry-line">Entry ${money(s.entry??s.price)}</div>
      <div class="strength"><span>Strength ${Number(s.score??s.strength??0)}%</span><div class="bar"><i style="width:${Math.min(100,Number(s.score??s.strength??0))}%"></i></div></div>
      <div class="metrics">
        <div class="metric"><span>Market move</span><strong>${formatMove(s.price_move,s.price_move_pct)}</strong></div>
        <div class="metric"><span>Signal duration</span><strong>${signalAge(s.timestamp)}</strong></div>
        <div class="metric"><span>Expected move</span><strong>${formatMove(s.expected_move,s.expected_move_pct)}</strong></div>
        <div class="metric"><span>Estimated duration</span><strong>${esc(s.estimated_duration||"--")}</strong></div>
        <div class="metric"><span>Take profit</span><strong>${money(s.take_profit)}</strong></div>
        <div class="metric"><span>Stop loss</span><strong>${money(s.stop_loss)}</strong></div>
      </div>
    </article>`;
  }).join("");
}

function renderHistory(){
  const el=$("historyList");
  if(!state.history.length){el.innerHTML=`<div class="empty">No signals recorded in the last 7 days.</div>`;return;}
  el.innerHTML=state.history.slice().reverse().slice(0,80).map(s=>{
    const dir=(s.direction||"").toLowerCase();
    const t=s.timestamp?new Date(s.timestamp).toLocaleString():"";
    return `<div class="history-row">
      <div><div class="history-market">${esc(s.market||s.asset||"")}</div><div class="history-meta">${esc(t)}</div></div>
      <div class="history-dir ${dir}">${esc(s.direction||"--")} · ${esc(s.strength||0)}%</div>
      <div class="history-price">${money(s.price)}</div>
    </div>`;
  }).join("");
}

function renderStatus(){
  const w=state.status.signal_window||{};
  const next=Number(state.status.next_broadcast_seconds||0);
  $("engineStatus").textContent=state.status.engine_running?"Engine online":"Engine offline";
  $("signalWindow").textContent=w.active?countdown(w.seconds_to_stop):countdown(w.seconds_to_start);
  $("windowLabel").textContent=w.active?"Time left before signals stop":"Until signals start at 06:00 EAT";
  $("nextBroadcast").textContent=countdown(next);
}

function renderPlans(){
  const grid=$("plansGrid"), plans=state.plans||{};
  grid.innerHTML=Object.entries(plans).map(([id,p])=>`
    <div class="plan">
      <h3>${esc(p.name)}</h3>
      <strong>UGX ${Number(p.ugx).toLocaleString()}</strong>
      <span>Mobile Money · MTN or Airtel</span>
      <button class="primary-btn" onclick="openPayment('${esc(id)}')">Pay with Mobile Money</button>
    </div>`).join("");
}
window.openPayment=(plan)=>{
  const p=state.plans[plan];
  const modal=document.createElement("div");
  modal.className="payment-modal";
  modal.innerHTML=`<div class="payment-box">
    <button class="close-btn" onclick="this.closest('.payment-modal').remove()">×</button>
    <span class="eyebrow">KETS ACCESS</span>
    <h2>${esc(p.name)} · UGX ${Number(p.ugx).toLocaleString()}</h2>
    <p class="payment-note">Enter the mobile-money number that should receive the payment prompt.</p>
    <label>Email<input id="payEmail" type="email" placeholder="you@example.com" autocomplete="email"></label>
    <label>Phone<input id="payPhone" type="tel" placeholder="07XXXXXXXX" autocomplete="tel"></label>
    <label>Network<select id="payNetwork"><option value="MTN">MTN</option><option value="AIRTEL">Airtel</option></select></label>
    <button class="primary-btn full" onclick="startPayment('${esc(plan)}')">Request payment</button>
    <div id="payResult" class="payment-result"></div>
  </div>`;
  document.body.appendChild(modal);
};
window.startPayment=async(plan)=>{
  const result=$("payResult");
  const email=$("payEmail").value.trim(), phone=$("payPhone").value.trim(), network=$("payNetwork").value;
  result.textContent="Starting payment request…";
  try{
    const r=await fetch("/api/payments/create",{method:"POST",headers:{"Content-Type":"application/json","Accept":"application/json"},body:JSON.stringify({plan,email,phone,network})});
    const d=await r.json();
    if(!r.ok) throw new Error(d.error||"Payment could not be started");
    result.innerHTML=`<strong>Payment request sent.</strong><br>${esc(d.message)}<br><small>Reference: ${esc(d.tx_ref)}</small><br><span id="verifyText">Waiting for payment confirmation…</span>`;
    pollPayment(d.tx_ref,d.transaction_id);
  }catch(e){result.textContent=e.message}
};
async function pollPayment(tx_ref,transaction_id){
  const out=$("verifyText"); if(!out)return;
  let attempts=0;
  const timer=setInterval(async()=>{
    attempts++;
    try{
      const r=await fetch("/api/payments/verify",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({tx_ref,transaction_id})});
      const d=await r.json();
      if(d.paid){
        state.token=d.token; localStorage.setItem("kets_access_token",d.token);
        out.textContent="Payment confirmed. Live signals unlocked.";
        clearInterval(timer);
        setTimeout(()=>{document.querySelector(".payment-modal")?.remove();loadAll()},900);
      }else if(attempts>=30){
        clearInterval(timer);
        out.textContent="Still pending. If you completed the payment, press the plan button again to check later.";
      }else{
        out.textContent=`Checking payment… ${attempts}/30`;
      }
    }catch(e){
      if(attempts>=30){clearInterval(timer);out.textContent="Verification could not be completed yet."}
    }
  },5000);
}

function startTicker(){
  if(statusTick) clearInterval(statusTick);
  statusTick=setInterval(()=>{
    const w=state.status.signal_window||{};
    if(w.active && w.seconds_to_stop>0) w.seconds_to_stop--;
    if(!w.active && w.seconds_to_start>0) w.seconds_to_start--;
    if(state.status.next_broadcast_seconds>0) state.status.next_broadcast_seconds--;
    renderStatus(); if(state.paid) renderSignals();
  },1000);
}

async function loadAll(){
  try{
    const [status,history,access,plans]=await Promise.all([
      api("/api/status"),api("/api/history"),api("/api/access"),api("/api/plans")
    ]);
    state.status=status; state.history=history.history||[]; state.paid=Boolean(access.paid); state.plans=plans.plans||{};
    if(state.paid){
      const live=await api("/api/signals");
      state.signals=live.signals||{};
    }else state.signals={};
    renderStatus();renderSignals();renderHistory();renderPlans();startTicker();
  }catch(e){
    $("engineStatus").textContent="Connection error";
    $("signalGrid").innerHTML=`<div class="empty">Unable to connect to KETS backend. ${esc(e.message)}</div>`;
  }
}
$("plansBtn").onclick=()=>document.getElementById("plansSection").scrollIntoView({behavior:"smooth"});
loadAll();
setInterval(loadAll,REFRESH_MS);
