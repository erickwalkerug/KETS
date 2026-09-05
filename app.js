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
function signalTimestamp(s){return s?.timestamp_utc||s?.timestamp||s?.source_timestamp||"";}
function parseKetsDate(raw){
  if(raw==null||raw==="") return NaN;
  if(typeof raw==="number"){
    const n=Number(raw);
    if(!Number.isFinite(n)) return NaN;
    return n<1e12?n*1000:n;
  }
  let text=String(raw).trim();
  if(!text) return NaN;
  // Explicit EAT/UTC labels from older KETS signals.
  if(/\\bEAT\\b/i.test(text)) text=text.replace(/\\s*EAT\\s*$/i,"+03:00").replace(" ","T");
  else if(/\\bUTC\\b/i.test(text)) text=text.replace(/\\s*UTC\\s*$/i,"Z").replace(" ","T");
  // ISO timestamps without an offset are UTC when they come from timestamp_utc.
  if(/^[0-9]{4}-[0-9]{2}-[0-9]{2}T?[0-9]{2}:[0-9]{2}/.test(text) && !/[zZ]|[+-][0-9]{2}:?[0-9]{2}$/.test(text)){
    text=text.replace(" ","T")+"Z";
  }
  const n=Date.parse(text);
  return Number.isFinite(n)?n:NaN;
}
function parseSignalTime(s){
  const raw=signalTimestamp(s);
  let n=parseKetsDate(raw);
  if(!Number.isFinite(n) && s?.received_at) n=parseKetsDate(s.received_at);
  return n;
}
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
function authMsg(t,bad=true, target="loginMsg"){
 const el=$(target);
 if(!el)return;
 el.textContent=t;
 el.className="form-message "+(bad?"error":"ok");
}
async function finishLogin(d){state.token=d.token;sessionStorage.setItem("kets_user_token",d.token);state.user=d.user;showApp();await loadAll();}
async function login(){
 try{const d=await api("/api/auth/login",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({email:$("loginEmail").value.trim(),password:$("loginPassword").value})});await finishLogin(d);}
 catch(e){
  const msg=String(e.message||"Sign-in failed.");
  const friendly=msg.toLowerCase().includes("account not found")
    ? "No KETS account was found for this email. Create your account first, then sign in."
    : `Sign-in failed: ${msg}`;
  authMsg(friendly,true,"loginMsg");
  if(msg.toLowerCase().includes("active payment plan")) loadAuthPlans();
}
}
async function register(){
 try{
  await api("/api/auth/register",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:$("regName").value.trim(),email:$("regEmail").value.trim(),password:$("regPassword").value,country_code:$("regCountry").value,country_name:$("regCountry").value==="UG"?"Uganda":"Other"})});
  $("loginEmail").value=$("regEmail").value.trim();
  showAuth("login");
  authMsg("Account created successfully. Choose a payment plan, complete payment, then sign in.",false,"loginMsg");
 }catch(e){authMsg(`Account creation failed: ${e.message}`,true,"registerMsg");}
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
function goldConfidenceLabel(score, explicit=""){
 const e=String(explicit||"").toUpperCase();
 if(e.includes("VERY STRONG")) return "VERY STRONG";
 if(e.includes("STRONG")) return "STRONG";
 if(e.includes("MODERATE")) return "MODERATE";
 const n=Number(score||0);
 if(n>=90)return "VERY STRONG";
 if(n>=75)return "STRONG";
 if(n>=60)return "MODERATE";
 return "WEAK";
}
function goldConfidenceDescription(label, score){
 const descriptions={
  "VERY STRONG":"Multiple confirmation factors align. This is a very strong qualifying setup.",
  "STRONG":"The main confirmation factors align and the setup has strong directional probability.",
  "MODERATE":"The setup has supporting confirmation, but additional caution is recommended.",
  "WEAK":"Confirmation is limited. Treat this setup with extra caution."
 };
 return descriptions[label]||`Signal confidence is ${Number(score||0)}%.`;
}
function goldUsd(n){return Number.isFinite(Number(n))?`$${Number(n).toLocaleString("en-US",{minimumFractionDigits:2,maximumFractionDigits:2})}`:"--";}
function goldPrice(n){return Number.isFinite(Number(n))?Number(n).toLocaleString("en-US",{minimumFractionDigits:2,maximumFractionDigits:2}):"--";}
function goldNum(n, decimals=0){return Number.isFinite(Number(n))?Number(n).toLocaleString("en-US",{minimumFractionDigits:decimals,maximumFractionDigits:decimals}):"--";}
function renderRichDashboard(s, asset){
 const isGold=asset==='GOLD';
 const direction=String(s?.direction||s?.signal||"WAIT").toUpperCase();
 const cls=direction==="SELL"?"sell":"buy";
 const score=Number(s?.score??s?.strength??s?.confidence??0);
 const confidence=goldConfidenceLabel(score,s?.confidence_label||s?.confidenceLabel||s?.confidence_level||s?.confidenceLevel);
 const desc=goldConfidenceDescription(confidence,score);
 const eqScoreRaw=Number(s?.entry_quality_score??s?.entryQualityScore??s?.entry_quality?.score);
 const eqScore=Number.isFinite(eqScoreRaw)?Math.max(0,Math.min(100,Math.round(eqScoreRaw))):null;
 const eqStatus=String((s?.entry_quality_status??s?.entryQualityStatus??s?.entry_quality?.status)||"").trim();
 const eqReversal=Boolean(s?.entry_quality_reversal??s?.entryQualityReversal??s?.entry_quality?.clear_reversal);
 const eqReasons=Array.isArray(s?.entry_quality_reasons)?s.entry_quality_reasons:(Array.isArray(s?.entry_quality?.reasons)?s.entry_quality.reasons:[]);
 const eqExtended=/EXTENDED/i.test(eqStatus)||eqReasons.some(x=>/excessively extended/i.test(String(x)));
 const gateAvailable=eqScore!==null;
 const gatePass=gateAvailable && score>=90 && eqScore>=80 && !eqReversal && !eqExtended;
 const eqClass=eqReversal?"reject":eqExtended||(eqScore!==null&&eqScore<65)?"caution":"pass";
 const eqLabel=eqStatus||"ENTRY QUALITY DATA PENDING";
 const symbol=isGold?"XAUUSD":"BTC/USD";
 const displayName=isGold?"GOLD":"BITCOIN";
 const entry=Number(s?.entry??s?.entry_price??s?.price??s?.market_price??s?.current_price);
 const targetRaw=Number(s?.take_profit??s?.target??s?.target_price);
 const stopRaw=Number(s?.stop_loss??s?.stopLoss??s?.sl);
 const target=Number.isFinite(targetRaw)?targetRaw:(Number.isFinite(entry)&&Number.isFinite(Number(s?.price_move)) ? entry+(direction==='SELL'?-1:1)*Math.abs(Number(s.price_move)) : NaN);
 const stop=Number.isFinite(stopRaw)?stopRaw:(Number.isFinite(entry)&&Number.isFinite(Number(s?.risk_move)) ? entry+(direction==='SELL'?1:-1)*Math.abs(Number(s.risk_move)) : NaN);
 const defaultMove=Number.isFinite(entry)&&Number.isFinite(target)?Math.abs(target-entry):NaN;
 const move=Number.isFinite(Number(s?.price_move))?Math.abs(Number(s.price_move)):defaultMove;
 const riskMove=Number.isFinite(Number(s?.risk_move))?Math.abs(Number(s.risk_move)):(Number.isFinite(entry)&&Number.isFinite(stop)?Math.abs(entry-stop):NaN);
 const contractSize=Number(s?.contract_size)||(isGold?100:1);
 const lotUnit=isGold?"oz / 1.00 lot":"BTC / 1.00 lot";
 const profitPerLotUsd=Number(s?.profit_per_lot_usd)||Number(s?.reward_per_lot_usd_profit)|| (Number.isFinite(move)?move*contractSize:NaN);
 const rewardPerLot=Number(s?.reward_per_lot_display)||Number(s?.reward_per_lot_usd_risk)|| (Number.isFinite(move)?move:NaN);
 const riskPerLot=Number(s?.risk_per_lot_display)||Number(s?.risk_per_lot_usd_risk)|| (Number.isFinite(riskMove)?riskMove:NaN);
 const rr=Number(s?.risk_reward)|| (Number.isFinite(rewardPerLot)&&Number.isFinite(riskPerLot)&&riskPerLot>0?rewardPerLot/riskPerLot:NaN);
 const rate=Number(s?.usd_ugx_rate)||3800;
 const movePct=Number.isFinite(Number(s?.price_move_pct))?Number(s.price_move_pct):(Number.isFinite(entry)&&Number.isFinite(move)&&entry?move/entry*100:0);
 const signalMs=parseSignalTime(s||{});
 const signalDisplay=Number.isFinite(signalMs)?new Date(signalMs).toLocaleString():String(signalTimestamp(s||{})||"--");
 const priceText=n=>Number.isFinite(Number(n))?Number(n).toLocaleString("en-US",{minimumFractionDigits:isGold?2:2,maximumFractionDigits:isGold?2:2}):"--";
 const usdText=n=>Number.isFinite(Number(n))?`$${Number(n).toLocaleString("en-US",{minimumFractionDigits:2,maximumFractionDigits:2})}`:"--";
 const pct=Number.isFinite(movePct)?movePct.toFixed(2):"--";
 const stopMove=Number.isFinite(riskMove)?riskMove:0;
 const r1=Number(s?.stop_management?.["1R"] ?? (Number.isFinite(entry)?entry+(direction==="SELL"?stopMove:-stopMove):NaN));
 const r15=Number(s?.stop_management?.["1.5R"] ?? (Number.isFinite(entry)?entry+(direction==="SELL"?stopMove*1.5:-stopMove*1.5):NaN));
 const r2=Number(s?.stop_management?.["2R"] ?? (Number.isFinite(entry)?entry+(direction==="SELL"?stopMove*2:-stopMove*2):NaN));
 const fixed=Number(s?.stop_management?.fixed ?? stop);
 const lots=[0.01,0.02,0.03,0.04,0.05,0.06,0.07,0.09,0.10,0.20,0.30,0.40,0.50,0.60,0.70,0.90,1.00];
 const profitTable=(rows)=>`<div class="gold-table-scroll"><div class="gold-profit-table gold-profit-table-wide" style="grid-template-columns:120px repeat(${rows.length},minmax(70px,1fr));">
   <div class="gold-row-label">LOT SIZE</div>${rows.map(l=>`<div class="gold-cell lot">${l.toFixed(2)}</div>`).join("")}
   <div class="gold-row-label">PROFIT<br>(USD)</div>${rows.map(l=>`<div class="gold-cell profit">${usdText(Number.isFinite(profitPerLotUsd)?profitPerLotUsd*l:NaN)}</div>`).join("")}
   <div class="gold-row-label">PROFIT<br>(UGX)</div>${rows.map(l=>`<div class="gold-cell ugx">${Number.isFinite(profitPerLotUsd)?goldNum(profitPerLotUsd*l*rate,0):"--"}</div>`).join("")}
 </div></div>`;
 return `<article class="gold-dashboard ${cls} ${isGold?'gold-market-dashboard':'btc-market-dashboard'}">
  <div class="gold-confidence-top"><div class="gold-confidence-title">KETS CONFIDENCE</div><div class="gold-confidence-value">${esc(confidence)}${score?` · ${score}%`:""}</div><div class="gold-confidence-description">${esc(desc)}</div></div>
  <div class="entry-quality-panel ${eqClass}">
   <div class="entry-quality-head">
    <div><span class="entry-quality-kicker">ENTRY QUALITY</span><strong>${eqScore!==null?`${eqScore}/100`:"--"}</strong></div>
    <div class="entry-quality-status">${esc(eqLabel)}</div>
   </div>
   <div class="entry-quality-gate">
    <span>90+ ENTRY GATE</span>
    <b>${score<90?"NOT REQUIRED":!gateAvailable?"SOURCE DATA PENDING":gatePass?"PASS — QUALITY CONFIRMED":"WAIT — ENTRY QUALITY FILTER"}</b>
   </div>
   <div class="entry-quality-reasons">
    ${eqReasons.length?eqReasons.map(reason=>`<span class="eq-check ${/CLEAR REVERSAL|excessively extended|not aligned|incomplete|conflict|weak/i.test(String(reason))?"warn":"ok"}">${/CLEAR REVERSAL/i.test(String(reason))?"⛔":"•"} ${esc(reason)}</span>`).join(""):`<span class="eq-check pending">• Entry-quality details will appear when supplied by the trading engine.</span>`}
   </div>
  </div>
  <div class="gold-header">
   <div class="gold-symbol"><strong>${displayName}</strong> <span>(${symbol})</span><div class="gold-direction ${cls}">${direction==="SELL"?"SELL ↘":"BUY ↗"}</div></div>
   <div class="gold-metric"><span>ENTRY PRICE</span><strong>${priceText(entry)}</strong></div>
   <div class="gold-metric"><span>TARGET PRICE</span><strong>${priceText(target)}</strong></div>
   <div class="gold-metric"><span>STOP LOSS</span><strong class="red">${priceText(stop)}</strong></div>
   <div class="gold-metric"><span>PRICE MOVE</span><strong class="green">${priceText(move)}<small>${usdText(profitPerLotUsd)} / 1 LOT</small></strong></div>
  </div>
  <div class="gold-potential"><span>POTENTIAL PROFIT (PER LOT)</span><strong>${usdText(profitPerLotUsd)}</strong><small>(For 1.00 Lot)</small></div>
  <div class="gold-live-grid">
   <div><span>SIGNAL TIME</span><b>${esc(signalDisplay)}</b></div>
   <div><span>MARKET MOVE</span><b>${priceText(move)} (${pct}%)</b></div>
   <div><span>EXPECTED MOVE</span><b>${priceText(move)} (${pct}%)</b></div>
   <div><span>ESTIMATED DURATION</span><b>${esc(s?.estimated_duration||s?.duration_text||"--")}</b></div>
  </div>
  <div class="gold-sl-panel"><div class="gold-panel-title">🛡 STOP LOSS MANAGEMENT</div><div class="gold-sl-grid">
   <div class="gold-sl-box active"><b>🟢 FIXED SL</b><strong>${priceText(fixed)}</strong><small>${Number.isFinite(riskPerLot)?`(-${usdText(riskPerLot)})`:"--"}</small></div>
   <div class="gold-sl-box"><b>○ 1R</b><strong>${priceText(r1)}</strong><small>${Number.isFinite(riskPerLot)?`(-${usdText(riskPerLot)})`:"--"}</small></div>
   <div class="gold-sl-box"><b>○ 1.5R</b><strong>${priceText(r15)}</strong><small>${Number.isFinite(riskPerLot)?`(-${usdText(riskPerLot*1.5)})`:"--"}</small></div>
   <div class="gold-sl-box"><b>○ 2R</b><strong>${priceText(r2)}</strong><small>${Number.isFinite(riskPerLot)?`(-${usdText(riskPerLot*2)})`:"--"}</small></div>
  </div><div class="gold-trailing"><b>TRAILING SL</b><span>Smart SL<br>(Dynamic)</span></div><div class="gold-summary"><div><span>RISK (PER LOT)</span><b class="red">${usdText(riskPerLot)}</b></div><div><span>REWARD (PER LOT)</span><b class="green">${usdText(rewardPerLot)}</b></div><div><span>RISK:REWARD</span><b class="green">1:${goldNum(rr,2)}</b></div></div></div>
  <div class="gold-profit-title">POTENTIAL PROFIT IN <b>USD</b> (BASED ON MARKET PRICE MOVEMENT)</div>
  ${profitTable(lots.slice(0,9))}${profitTable(lots.slice(9))}
  <div class="gold-rate">USD/UGX RATE: ${goldNum(rate,0)} · ${contractSize} ${lotUnit} · PROFIT = MOVE × LOT × CONTRACT SIZE</div>
 </article>`;
}
function renderWaitingDashboard(asset){
 const isGold=asset==='GOLD';
 return `<article class="gold-dashboard wait ${isGold?'gold-market-dashboard':'btc-market-dashboard'}">
   <div class="gold-confidence-top"><div class="gold-confidence-title">KETS CONFIDENCE</div><div class="gold-confidence-value">WAITING</div><div class="gold-confidence-description">NO QUALIFYING SETUP — KETS is monitoring ${isGold?'Gold':'Bitcoin'} and will update automatically when a qualifying setup is available.</div></div>
   <div class="gold-header waiting-header"><div class="gold-symbol"><strong>${isGold?'GOLD':'BITCOIN'}</strong> <span>(${isGold?'XAUUSD':'BTC/USD'})</span><div class="gold-direction wait">WAIT</div></div><div class="gold-metric"><span>ENTRY PRICE</span><strong>--</strong></div><div class="gold-metric"><span>TARGET PRICE</span><strong>--</strong></div><div class="gold-metric"><span>STOP LOSS</span><strong class="red">--</strong></div><div class="gold-metric"><span>PRICE MOVE</span><strong class="green">--</strong></div></div>
   <div class="entry-quality-panel pending">
   <div class="entry-quality-head"><div><span class="entry-quality-kicker">ENTRY QUALITY</span><strong>--</strong></div><div class="entry-quality-status">WAITING FOR SIGNAL</div></div>
   <div class="entry-quality-gate"><span>90+ ENTRY GATE</span><b>NOT ACTIVE</b></div>
   <div class="entry-quality-reasons"><span class="eq-check pending">• KETS will evaluate entry quality when a qualifying setup appears.</span></div>
  </div>
  <div class="gold-live-grid"><div><span>STATUS</span><b>Monitoring</b></div><div><span>SIGNAL TIME</span><b>--</b></div><div><span>EXPECTED MOVE</span><b>--</b></div><div><span>ESTIMATED DURATION</span><b>--</b></div></div>
   <div class="gold-sl-panel"><div class="gold-panel-title">🛡 STOP LOSS MANAGEMENT</div><div class="gold-sl-grid"><div class="gold-sl-box active"><b>🟢 FIXED SL</b><strong>--</strong><small>Waiting for setup</small></div><div class="gold-sl-box"><b>○ 1R</b><strong>--</strong><small>Waiting</small></div><div class="gold-sl-box"><b>○ 1.5R</b><strong>--</strong><small>Waiting</small></div><div class="gold-sl-box"><b>○ 2R</b><strong>--</strong><small>Waiting</small></div></div><div class="gold-trailing"><b>TRAILING SL</b><span>Smart SL<br>(Dynamic)</span></div><div class="gold-summary"><div><span>RISK (PER LOT)</span><b class="red">--</b></div><div><span>REWARD (PER LOT)</span><b class="green">--</b></div><div><span>RISK:REWARD</span><b class="green">--</b></div></div></div>
   <div class="gold-profit-title">POTENTIAL PROFIT IN <b>USD</b> (BASED ON MARKET PRICE MOVEMENT)</div><div class="waiting-profit">Dashboard ready. A qualifying signal will populate entry, target, stop loss and profit projections automatically.</div>
 </article>`;
}
function renderSignals(){
 const grid=$("signalGrid");
 const gold=state.signals?.GOLD||state.signals?.XAUUSD||state.signals?.XAU;
 const btc=state.signals?.BTC||state.signals?.BTCUSD;
 // Only the market scheduled for the current day is displayed. The backend
 // already enforces the same schedule: Monday-Friday = GOLD, Saturday-Sunday = BTC.
 const activeMarkets=Array.isArray(state.status?.markets)?state.status.markets.map(x=>String(x).toUpperCase()):[];
 const serverMs=Date.parse(state.status?.server_time||state.status?.time_eat||"");
 const day=Number.isFinite(serverMs)?new Date(serverMs).getUTCDay():new Date().getDay();
 const isWeekend=day===0||day===6;
 const showGold=activeMarkets.length?activeMarkets.some(x=>x==="GOLD"||x==="XAUUSD"||x==="XAU"):!isWeekend;
 const showBtc=activeMarkets.length?activeMarkets.some(x=>x==="BTC"||x==="BTCUSD"||x==="BITCOIN"):isWeekend;
 let html="";
 if(showGold) html += gold ? renderRichDashboard(gold,'GOLD') : renderWaitingDashboard('GOLD');
 if(showBtc) html += btc ? renderRichDashboard(btc,'BTC') : renderWaitingDashboard('BTC');
 grid.innerHTML=html || `<div class="empty">No market dashboard is scheduled right now.</div>`;
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
      <div class="history-meta">${esc(Number.isFinite(parseSignalTime(s))?new Date(parseSignalTime(s)).toLocaleString():(s.timestamp||""))}</div>
    </div>
    <div class="history-dir ${cls}">${esc(direction)}${noSetup?"":" · "+esc(strength)+"%"}</div>
    <div class="history-entry-quality">${noSetup?"--":(s.entry_quality_score!=null?`${esc(s.entry_quality_score)}/100`:"--")}<small>${noSetup?"":esc(s.entry_quality_status||"")}</small></div>
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
 const w=state.status.signal_window||{};$("engineStatus").textContent=state.status.engine_running?"Engine online · Live monitoring":"Engine offline";
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
 }catch(e){state.token="";sessionStorage.removeItem("kets_user_token");showAuth("login");if($("loginMsg"))authMsg(e.message,true,"loginMsg");}
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
