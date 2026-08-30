const API_BASE=window.location.origin, REFRESH_MS=10000, TIMER_MS=1000;
const freshHash=new URLSearchParams(location.hash.replace(/^#/,"?"));
const hashToken=freshHash.get("login")||"";
sessionStorage.removeItem("kets_user_token");
if(hashToken)sessionStorage.setItem("kets_user_token",hashToken);
const state={token:hashToken,user:null,access:null,signals:{},history:[],payments:[],status:{},plans:{},clockOffsetMs:0,signalWindowDeadlineMs:0,nextBroadcastDeadlineMs:0,nextRefreshDeadlineMs:0,loading:false};
const $=id=>document.getElementById(id);
function headers(extra={}){return {Accept:"application/json",...(state.token?{Authorization:`Bearer ${state.token}`}:{}) ,...extra};}
async function api(path,opts={}){
 const controller=new AbortController();
 const timeout=setTimeout(()=>controller.abort(),20000);
 try{
  const r=await fetch(API_BASE+path,{...opts,signal:controller.signal,headers:headers(opts.headers||{})});
  const d=await r.json().catch(()=>({}));
  if(!r.ok) throw Error(d.error||`HTTP ${r.status}`);
  return d;
 }catch(e){
  if(e.name==="AbortError") throw Error("Server is taking too long to respond. Please try again.");
  throw e;
 }finally{clearTimeout(timeout);}
}
function money(v,currency="UGX"){return v==null||!Number.isFinite(Number(v))?"--":currency+" "+Number(v).toLocaleString();}
function signalMoney(v){return money(v,"USD");}
function isUganda(){return String(state.user?.country_code||"UG").toUpperCase()==="UG";}
function planAmount(p){return isUganda()?p?.ugx:p?.usd;}
function planCurrency(){return isUganda()?"UGX":"USD";}
function authIsUganda(){return ($("regCountry")?.value||"UG")==="UG";}
function countdown(sec){sec=Math.max(0,Math.floor(Number(sec)||0));return `${String(Math.floor(sec/3600)).padStart(2,"0")}:${String(Math.floor(sec%3600/60)).padStart(2,"0")}:${String(sec%60).padStart(2,"0")}`;}
function esc(v){return String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"}[c]));}
function serverNowMs(){return Date.now()+state.clockOffsetMs;}
function signalAge(ts){const s=Date.parse(ts);return Number.isFinite(s)?countdown((serverNowMs()-s)/1000):"--";}
function setTimerDeadlines(status){
 const serverMs=Date.parse(status?.server_time||status?.time_eat||"");
 if(Number.isFinite(serverMs)) state.clockOffsetMs=serverMs-Date.now();
 const w=status?.signal_window||{};
 state.signalWindowDeadlineMs=serverNowMs()+Number(w.active?w.seconds_to_stop:w.seconds_to_start||0)*1000;
 state.nextBroadcastDeadlineMs=serverNowMs()+Math.max(0,Number(status?.next_broadcast_seconds||0))*1000;
}
function tickTimers(){
 if(!state.token||$("app")?.classList.contains("hidden")) return;
 const now=serverNowMs();
 const w=state.status.signal_window||{};
 const signalSeconds=Math.max(0,Math.ceil((state.signalWindowDeadlineMs-now)/1000));
 const nextSeconds=Math.max(0,Math.ceil((state.nextRefreshDeadlineMs-now)/1000));
 if($("signalWindow")) $("signalWindow").textContent=countdown(signalSeconds);
 if($("windowLabel")) $("windowLabel").textContent=w.active?"Time left before signals stop":"Until signals start at 06:00 EAT";
 if($("nextBroadcast")) $("nextBroadcast").textContent=countdown(nextSeconds);
 const expiryEl=$("paymentExpiryTimer");
 if(expiryEl && state.access?.expires){
   const exp=Date.parse(state.access.expires);
   expiryEl.textContent=Number.isFinite(exp)?countdown((exp-now)/1000):"--:--:--";
 }
 document.querySelectorAll(".signal-card[data-signal-timestamp]").forEach(card=>{
   const parsed=Date.parse(card.dataset.signalTimestamp||"");
   const age=card.querySelector(".metric:nth-child(2) strong");
   if(age&&Number.isFinite(parsed)) age.textContent=countdown((now-parsed)/1000);
 });
}
function updateCountryCurrency(){
 const sel=$("regCountry"), note=$("currencyNote");
 if(!sel||!note)return;
 const ug=sel.value==="UG";
 note.innerHTML=`Currency: <strong>${ug?"UGX":"USD"}</strong> · ${ug?"Uganda plans":"International plans"}`;
}
async function loadAuthPlans(){
 try{
  const d=await api("/api/plans");
  const plans=d.plans||{};
  const render=(id)=>{
    const el=$(id); if(!el)return;
    el.innerHTML=Object.entries(plans).filter(([_,p])=>authIsUganda()||p.usd!=null).map(([key,p])=>{
      const cur=authIsUganda()?"UGX":"USD", amount=authIsUganda()?p.ugx:p.usd;
      return `<div class="plan"><span class="plan-tag">${p.seconds<=3600?"SHORT ACCESS":"SUBSCRIPTION"}</span><h3>${esc(p.name)}</h3><strong>${money(amount,cur)}</strong><span>${cur}</span><button class="primary-btn full" onclick="openAuthPayment('${esc(key)}')">Pay & activate</button></div>`;
    }).join("");
  };
  render("loginPlansGrid"); render("registerPlansGrid");
 }catch(e){
  ["loginPlansGrid","registerPlansGrid"].forEach(id=>{if($(id))$(id).innerHTML=`<div class="empty">Payment plans unavailable right now.</div>`});
 }
}
window.openAuthPayment=async plan=>{
 const email=($("loginEmail")?.value||$("regEmail")?.value||"").trim().toLowerCase();
 const p=state.plans?.[plan];
 let planData=p;
 if(!planData){try{const d=await api("/api/plans");planData=d.plans?.[plan];}catch{}}
 if(!planData){authMsg("Payment plan unavailable.");return;}
 const abroad=!authIsUganda(),cur=abroad?"USD":"UGX",amt=abroad?planData.usd:planData.ugx;
 const m=document.createElement("div");m.className="modal";
 m.innerHTML=`<div class="modal-box"><button class="close-btn" onclick="this.closest('.modal').remove()">×</button><span class="eyebrow">ACTIVATE SIGN-IN</span><h2>${esc(planData.name)} · ${money(amt,cur)}</h2><p class="muted">Create your account first. Payment is required before normal users can sign in.</p><label>Account email<input id="authPayEmail" type="email" value="${esc(email)}" placeholder="you@example.com"></label>${abroad?`<label>Phone (optional)<input id="authPayPhone" type="tel" placeholder="International phone number"></label><input id="authPayNetwork" type="hidden" value="INTERNATIONAL">`:`<label>Mobile-money phone<input id="authPayPhone" type="tel" placeholder="07XXXXXXXX"></label><label>Network<select id="authPayNetwork"><option value="MTN">MTN</option><option value="AIRTEL">Airtel</option></select></label>`}<button class="primary-btn full" onclick="startAuthPayment('${esc(plan)}')">Continue to Pesapal</button><div id="authPayResult" class="payment-result"></div></div>`;
 document.body.appendChild(m);
};
window.startAuthPayment=async plan=>{
 const r=$("authPayResult");r.textContent="Creating secure Pesapal payment…";
 try{
  const d=await api("/api/payments/create-public",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({plan,email:$("authPayEmail").value.trim(),phone:$("authPayPhone").value.trim(),network:$("authPayNetwork").value})});
  location.href=d.redirect_url;
 }catch(e){r.textContent=e.message;}
};

function showAuth(tab="login"){
 $("authScreen").classList.remove("hidden");$("app").classList.add("hidden");
 document.querySelectorAll(".tab").forEach(b=>b.classList.toggle("active",b.dataset.tab===tab));
 $("loginForm").classList.toggle("hidden",tab!=="login");$("registerForm").classList.toggle("hidden",tab!=="register"); loadAuthPlans();
}
function showApp(){ $("authScreen").classList.add("hidden");$("app").classList.remove("hidden");}
function authMsg(t,bad=true){$("authMsg").textContent=t;$("authMsg").className="form-message "+(bad?"error":"ok");}
async function finishLogin(d){state.token=d.token;sessionStorage.setItem("kets_user_token",d.token);state.user=d.user;showApp();await loadAll();}
async function login(){
 try{const d=await api("/api/auth/login",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({email:$("loginEmail").value.trim(),password:$("loginPassword").value})});await finishLogin(d);}
 catch(e){authMsg(e.message); if(e.message.includes("active payment plan")) renderAuthPlans();}
}
async function register(){
 try{
  await api("/api/auth/register",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:$("regName").value.trim(),email:$("regEmail").value.trim(),password:$("regPassword").value,country_code:$("regCountry").value,country_name:$("regCountry").value==="UG"?"Uganda":"Other"})});
  $("loginEmail").value=$("regEmail").value.trim();
  showAuth("login");
  authMsg("Account created for free. Choose a payment plan, complete payment, then sign in.",false);
 }catch(e){authMsg(e.message);}
}
function renderProfile(){
 const u=state.user;if(!u)return;
 $("profileName").textContent=u.name||"KETS User";$("profileEmail").textContent=u.email;
 $("miniName").textContent=(u.name||"Profile").split(" ")[0];
}
function renderAccess(){
 const a=state.access, c=$("accessCard");
 if(a?.paid&&a.plan){
   const p=state.plans[a.plan];
   $("accessPlan").textContent=p?p.name.toUpperCase():a.plan.toUpperCase();
   $("accessExpiry").textContent=a.expires?`Expires ${new Date(a.expires).toLocaleString()}`:"Active";
   const expiryText=a.expires?new Date(a.expires).toLocaleString():"No expiry";
   c.innerHTML=`<strong>${esc(p?.name||a.plan)} · ${money(planAmount(p),planCurrency())}</strong><span>Live signals are available. Plan active until ${expiryText}</span><div class="expiry-timer">Payment expiry: <b id="paymentExpiryTimer">--:--:--</b></div>`;
   return;
 }
 $("accessPlan").textContent="LOCKED";
 $("accessExpiry").textContent="Payment required";
 c.innerHTML=`<strong>Paid plan required</strong><span>Normal users need an active plan to sign in and access live signals.</span>`;
}
function renderSignals(){
 const grid=$("signalGrid");
 const lockedCard=$("lockedCard");
 if(lockedCard) lockedCard.style.display="none";
 grid.innerHTML=["BTC","GOLD"].map(m=>{
   const s=state.signals?.[m];
   if(!s)return `<article class="signal-card"><div class="signal-top"><div class="market">${m}</div><div class="direction wait">WAIT</div></div><div class="price">--</div><div class="strength">No current signal.</div></article>`;
   const status=String(s.status||s.result||s.signal_status||"").toUpperCase();
   const noSetup=status.includes("NO QUALIFYING")||status.includes("NO_QUALIFYING")||
                  status==="NO_SETUP"||status==="NO SIGNAL"||status==="NO_SIGNAL"||
                  s.qualifying===false;
   if(noSetup){
     return `<article class="signal-card wait" data-signal-timestamp="${esc(s.timestamp||"")}">
       <div class="signal-top"><div class="market">KETS ${esc(s.market||m)}</div><div class="direction wait">NO SETUP</div></div>
       <div class="price">${signalMoney(s.current_price??s.price??s.market_price)}</div>
       <div class="strength">No qualifying setup.</div>
       <div class="metrics">
         <div class="metric"><span>Scan time</span><strong>${esc(s.timestamp?new Date(s.timestamp).toLocaleString():"--")}</strong></div>
         <div class="metric"><span>Scan age</span><strong>${signalAge(s.timestamp)}</strong></div>
         <div class="metric"><span>Market price</span><strong>${signalMoney(s.current_price??s.price??s.market_price)}</strong></div>
       </div>
     </article>`;
   }
   const dir=(s.direction||"WAIT").toLowerCase();
   const label="LIVE";
   return `<article class="signal-card ${dir}" data-signal-timestamp="${esc(s.timestamp||"")}">
     <div class="signal-top"><div class="market">KETS ${esc(s.market||m)}</div><div class="direction ${dir}">${label}</div></div>
     <div class="price">${signalMoney(s.current_price??s.price)}</div>
     <div class="entry-line">Entry ${signalMoney(s.entry??s.price)}</div>
     <div class="strength"><span>LIVE SIGNAL · Strength ${Number(s.score??s.strength??0)}%</span><div class="bar"><i style="width:${Math.min(100,Number(s.score??s.strength??0))}%"></i></div></div>
     <div class="metrics">
       <div class="metric"><span>Signal time</span><strong>${esc(s.timestamp?new Date(s.timestamp).toLocaleString():"--")}</strong></div>
       <div class="metric"><span>Signal age</span><strong>${signalAge(s.timestamp)}</strong></div>
       <div class="metric"><span>Market move</span><strong>${signalMoney(s.price_move)}</strong></div>
       <div class="metric"><span>Expected move</span><strong>${signalMoney(s.expected_move)}</strong></div>
       <div class="metric"><span>Estimated duration</span><strong>${esc(s.estimated_duration||"--")}</strong></div>
       <div class="metric"><span>Take profit</span><strong>${signalMoney(s.take_profit)}</strong></div>
       <div class="metric"><span>Stop loss</span><strong>${signalMoney(s.stop_loss)}</strong></div>
     </div>
   </article>`;
 }).join("");
}
function renderHistory(){
 const el=$("historyList");
 if(!state.history.length){
  el.innerHTML=`<div class="empty">No engine scans recorded in the last 7 days.</div>`;
  return;
 }
 el.innerHTML=state.history.slice().reverse().slice(0,80).map(s=>{
  const status=String(s.status||s.result||s.signal_status||"").toUpperCase();
  const noSetup=status.includes("NO QUALIFYING")||status.includes("NO_QUALIFYING")||
                 status==="NO_SETUP"||status==="NO SIGNAL"||status==="NO_SIGNAL"||
                 s.qualifying===false;
  const direction=noSetup?"NO QUALIFYING SETUP":String(s.direction||"--").toUpperCase();
  const cls=noSetup?"wait":direction.toLowerCase();
  const strength=s.score??s.strength??0;
  const price=s.price??s.current_price??s.market_price;
  return `<div class="history-row">
    <div>
      <div class="history-market">${esc(s.market||s.asset||"")}</div>
      <div class="history-meta">${esc(s.timestamp?new Date(s.timestamp).toLocaleString():"")}</div>
    </div>
    <div class="history-dir ${cls}">${esc(direction)}${noSetup?"":" · "+esc(strength)+"%"}</div>
    <div class="history-price">${signalMoney(price)}</div>
  </div>`;
 }).join("");
}
function renderPayments(){
 const el=$("paymentHistory");if(!state.payments.length){el.innerHTML=`<div class="empty">No payments yet.</div>`;return;}
 el.innerHTML=state.payments.map(p=>`<div class="history-row"><div><div class="history-market">${esc(state.plans[p.plan]?.name||p.plan)}</div><div class="history-meta">${esc(new Date(p.created_at).toLocaleString())} · ${esc(p.network||"Pesapal")}</div></div><div class="history-dir ${p.status==="COMPLETED"?"buy":"wait"}">${esc(p.status)}</div><div class="history-price">${money(p.amount,p.currency||planCurrency())}</div></div>`).join("");
}
function renderCommunity(){}
function renderStatus(){
 const w=state.status.signal_window||{};$("engineStatus").textContent=state.status.engine_running?"Engine online":"Engine offline";
 tickTimers();
}
function renderPlans(){
 const abroad=!isUganda();
 const entries=Object.entries(state.plans).filter(([id,p])=>!abroad || p.usd!=null);if($("plansCurrencyLabel"))$("plansCurrencyLabel").textContent=planCurrency();
 $("plansGrid").innerHTML=entries.map(([id,p])=>{const cur=planCurrency(),amt=planAmount(p);return `<div class="plan"><span class="plan-tag">${p.seconds<=3600?"SHORT ACCESS":"SUBSCRIPTION"}</span><h3>${esc(p.name)}</h3><strong>${money(amt,cur)}</strong><span>Pesapal · ${cur}${isUganda()?" · MTN/Airtel where available":" · International payment methods"}</span><button class="primary-btn" onclick="openPayment('${esc(id)}')">Choose plan</button></div>`}).join("");
}
window.openPayment=plan=>{
 const p=state.plans[plan], abroad=!isUganda(), cur=planCurrency(), amt=planAmount(p),m=document.createElement("div");m.className="modal";m.innerHTML=`<div class="modal-box"><button class="close-btn" onclick="this.closest('.modal').remove()">×</button><span class="eyebrow">SECURE PAYMENT</span><h2>${esc(p.name)} · ${money(amt,cur)}</h2><p class="muted">Payment email is fixed to your signed-in account.</p><label>Email<input value="${esc(state.user.email)}" disabled></label>${abroad?`<label>Phone (optional)<input id="payPhone" type="tel" placeholder="International phone number"></label><input id="payNetwork" type="hidden" value="INTERNATIONAL">`:`<label>Mobile-money phone<input id="payPhone" type="tel" placeholder="07XXXXXXXX"></label><label>Network<select id="payNetwork"><option value="MTN">MTN</option><option value="AIRTEL">Airtel</option></select></label>`}<button class="primary-btn full" onclick="startPayment('${esc(plan)}')">Continue to Pesapal</button><div id="payResult" class="payment-result"></div></div>`;document.body.appendChild(m);
};
window.startPayment=async plan=>{
 const r=document.getElementById("payResult");r.textContent="Creating secure Pesapal payment…";
 try{const d=await api("/api/payments/create",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({plan,email:state.user.email,phone:document.getElementById("payPhone").value,network:document.getElementById("payNetwork").value})});r.innerHTML=`Payment created. Opening Pesapal… <small>${esc(d.tx_ref)}</small>`;location.href=d.redirect_url;}catch(e){r.textContent=e.message;}
};
function openProfile(){
 const u=state.user,m=$("profileModal");m.classList.remove("hidden");m.innerHTML=`<div class="modal-box"><button class="close-btn" onclick="this.classList.add('x');document.getElementById('profileModal').classList.add('hidden')">×</button><span class="eyebrow">ACCOUNT</span><h2>Your profile</h2><label>Full name<input id="editName" value="${esc(u.name||"")}"></label><div id="profileMsg" class="form-message"></div><button class="primary-btn full" id="saveProfile">Save profile</button><button class="danger-btn full" id="logoutBtn">Sign out</button></div>`;
 
 $("saveProfile").onclick=async()=>{try{const d=await api("/api/auth/profile",{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:$("editName").value.trim()})});state.user=d.user;m.classList.add("hidden");renderProfile();}catch(e){$("profileMsg").textContent=e.message;}};
 $("logoutBtn").onclick=()=>{state.token="";sessionStorage.removeItem("kets_user_token");location.reload();};
}
async function loadAll(){
 if(state.loading)return;
 state.loading=true;
 try{
  const me=await api("/api/auth/me");state.user=me.user;state.access=me.access;
  const [status,history,access,plans,payments]=await Promise.all([api("/api/status"),api("/api/history"),api("/api/access"),api("/api/plans"),api("/api/payments/history")]);
  state.status=status;setTimerDeadlines(status);state.history=history.history||[];state.access=access;state.plans=plans.plans||{};state.payments=payments.payments||[];
  try{const signalFeed=await api("/api/signals");state.signals=signalFeed.signals||{};state.signalMode="live";state.signalDelayMinutes=0;}catch{state.signals={};state.signalMode="live";state.signalDelayMinutes=0;}
  showApp();renderProfile();renderAccess();renderStatus();renderSignals();renderHistory();renderPayments();renderPlans();
  refreshDisplayedSignals();
 }catch(e){state.token="";sessionStorage.removeItem("kets_user_token");showAuth("login");if($("authMsg"))authMsg(e.message);}
 finally{state.loading=false;}
}
document.querySelectorAll(".tab").forEach(b=>b.onclick=()=>showAuth(b.dataset.tab));
$("regCountry")?.addEventListener("change",()=>{updateCountryCurrency();loadAuthPlans();});updateCountryCurrency();
if($("loginBtn")) $("loginBtn").onclick=login;
if($("registerBtn")) $("registerBtn").onclick=register;
if($("profileBtn")) $("profileBtn").onclick=openProfile;
if($("editProfileBtn")) $("editProfileBtn").onclick=openProfile;
const q=new URLSearchParams(location.search);if(q.get("payment")==="success")history.replaceState({},document.title,"/");if(location.hash)history.replaceState({},document.title,"/");
state.nextRefreshDeadlineMs=Date.now()+REFRESH_MS;
if(state.token)loadAll();else showAuth("login");
async function refreshDisplayedSignals(){
 if(!state.token||$("app")?.classList.contains("hidden")||state.loading)return;
 try{
  // Signal display is refreshed independently so a payment/profile/API hiccup
  // cannot prevent a newly available signal from reaching the page.
  const [signalFeed, historyFeed, statusFeed, accessFeed]=await Promise.all([
   api("/api/signals"),
   api("/api/history"),
   api("/api/status"),
   api("/api/access")
  ]);
  state.signals=signalFeed.signals||{};
  state.signalMode="live";
  state.signalDelayMinutes=0;
  state.history=historyFeed.history||[];
  state.status=statusFeed;
  state.access=accessFeed;
  setTimerDeadlines(statusFeed);
  renderStatus();
  renderAccess();
  renderSignals();
  renderHistory();

  state.nextRefreshDeadlineMs=serverNowMs()+REFRESH_MS;
  const stamp=$("signalsLastUpdated");
  if(stamp){
   stamp.textContent=`Updated ${new Date().toLocaleTimeString()} · LIVE`;
  }
 }catch(e){
  // Keep the last successfully displayed signal on screen during a
  // temporary network/render failure instead of blanking the dashboard.
  const stamp=$("signalsLastUpdated");
  if(stamp && !stamp.textContent) stamp.textContent="Waiting for signal feed…";
 }
}
setInterval(refreshDisplayedSignals,REFRESH_MS);
setInterval(tickTimers,TIMER_MS);

if("serviceWorker" in navigator){navigator.serviceWorker.register("/service-worker.js").catch(()=>{});}


// KETS PWA installation
let ketsDeferredInstallPrompt=null;
const installPromptEl=()=>document.getElementById("installPrompt");
const isKetsStandalone=()=>window.matchMedia("(display-mode: standalone)").matches||window.navigator.standalone===true;
function isIosKets(){return /iphone|ipad|ipod/i.test(navigator.userAgent)&&!isKetsStandalone();}
function showKetsInstallPrompt(){
  const el=installPromptEl();
  if(!el||isKetsStandalone()) return;
  el.classList.remove("hidden");
}
function hideKetsInstallPrompt(){
  const el=installPromptEl(); if(el) el.classList.add("hidden");
}
window.addEventListener("beforeinstallprompt",e=>{
  e.preventDefault();
  ketsDeferredInstallPrompt=e;
  showKetsInstallPrompt();
});
window.addEventListener("appinstalled",()=>{
  ketsDeferredInstallPrompt=null;
  hideKetsInstallPrompt();
});
document.addEventListener("DOMContentLoaded",()=>{
  const btn=document.getElementById("installBtn"), close=document.getElementById("installClose");
  if(close) close.onclick=hideKetsInstallPrompt;
  if(btn) btn.onclick=async()=>{
    if(ketsDeferredInstallPrompt){
      ketsDeferredInstallPrompt.prompt();
      const result=await ketsDeferredInstallPrompt.userChoice.catch(()=>null);
      if(result?.outcome==="accepted") hideKetsInstallPrompt();
      ketsDeferredInstallPrompt=null;
    }else if(isIosKets()){
      alert("To install KETS on iPhone/iPad: tap Share in Safari, then choose “Add to Home Screen”.");
    }else{
      alert("If your browser supports installation, open the browser menu and choose “Install app” or “Add to Home screen”.");
    }
  };
  if(isIosKets()) setTimeout(showKetsInstallPrompt,1200);
});
