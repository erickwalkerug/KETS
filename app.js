const API_BASE=window.location.origin, REFRESH_MS=10000;
const freshHash=new URLSearchParams(location.hash.replace(/^#/,"?"));
const hashToken=freshHash.get("login")||"";
sessionStorage.removeItem("kets_user_token");
if(hashToken)sessionStorage.setItem("kets_user_token",hashToken);
const state={token:hashToken,user:null,access:null,signals:{},history:[],payments:[],community:{},status:{},plans:{}};
const $=id=>document.getElementById(id);
const COUNTRIES=[
["UG","Uganda","+256"],["KE","Kenya","+254"],["TZ","Tanzania","+255"],["RW","Rwanda","+250"],["BI","Burundi","+257"],["SS","South Sudan","+211"],["NG","Nigeria","+234"],["GH","Ghana","+233"],["ZA","South Africa","+27"],["ZM","Zambia","+260"],["ZW","Zimbabwe","+263"],["ET","Ethiopia","+251"],["US","United States","+1"],["GB","United Kingdom","+44"],["CA","Canada","+1"],["AU","Australia","+61"],["IN","India","+91"],["AE","United Arab Emirates","+971"],["DE","Germany","+49"],["FR","France","+33"],["IT","Italy","+39"],["NL","Netherlands","+31"],["BR","Brazil","+55"],["CN","China","+86"],["JP","Japan","+81"]
];
function headers(extra={}){return {Accept:"application/json",...(state.token?{Authorization:`Bearer ${state.token}`}:{}) ,...extra};}
async function api(path,opts={}){
 const r=await fetch(API_BASE+path,{...opts,headers:headers(opts.headers||{})});
 const d=await r.json().catch(()=>({}));
 if(!r.ok) throw Error(d.error||`HTTP ${r.status}`);
 return d;
}
function money(v,currency="UGX"){return v==null||!Number.isFinite(Number(v))?"--":currency+" "+Number(v).toLocaleString();}
function isUganda(){return String(state.user?.country_code||"").toUpperCase()==="UG";}
function planAmount(p){return isUganda()?p?.ugx:p?.usd;}
function planCurrency(){return isUganda()?"UGX":"USD";}
function countdown(sec){sec=Math.max(0,Math.floor(Number(sec)||0));return `${String(Math.floor(sec/3600)).padStart(2,"0")}:${String(Math.floor(sec%3600/60)).padStart(2,"0")}:${String(sec%60).padStart(2,"0")}`;}
function esc(v){return String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"}[c]));}
function signalAge(ts){const s=Date.parse(ts);return Number.isFinite(s)?countdown((Date.now()-s)/1000):"--";}
function countryOptions(selected=""){return COUNTRIES.map(([c,n,d])=>`<option value="${c}" ${c===selected?"selected":""}>${n} · ${c} · ${d}</option>`).join("");}
function showAuth(tab="login"){
 $("authScreen").classList.remove("hidden");$("app").classList.add("hidden");
 document.querySelectorAll(".tab").forEach(b=>b.classList.toggle("active",b.dataset.tab===tab));
 $("loginForm").classList.toggle("hidden",tab!=="login");$("registerForm").classList.toggle("hidden",tab!=="register");
}
function showApp(){ $("authScreen").classList.add("hidden");$("app").classList.remove("hidden");}
function authMsg(t,bad=true){$("authMsg").textContent=t;$("authMsg").className="form-message "+(bad?"error":"ok");}
async function finishLogin(d){state.token=d.token;sessionStorage.setItem("kets_user_token",d.token);state.user=d.user;showApp();await loadAll();}
async function login(){
 try{const d=await api("/api/auth/login",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({email:$("loginEmail").value.trim(),password:$("loginPassword").value})});await finishLogin(d);}
 catch(e){authMsg(e.message);}
}
async function register(){
 const code=$("regCountry").value,n=COUNTRIES.find(x=>x[0]===code);
 try{const d=await api("/api/auth/register",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:$("regName").value.trim(),email:$("regEmail").value.trim(),password:$("regPassword").value,country_code:code,country_name:n?n[1]:""})});await finishLogin(d);}
 catch(e){authMsg(e.message);}
}
async function google(){
 try{const d=await api("/api/auth/google");location.href=d.redirect_url;}catch(e){authMsg(e.message);}
}
function renderProfile(){
 const u=state.user;if(!u)return;
 $("profileName").textContent=u.name||"KETS User";$("profileEmail").textContent=u.email;
 $("profileCountry").textContent=u.country_name?`${u.country_name} · ${u.country_code}`:"Country not set";
 $("miniName").textContent=(u.name||"Profile").split(" ")[0];
 $("miniAvatar").src=u.profile_picture||"/static/kets-icon.svg";$("profileAvatar").src=u.profile_picture||"/static/kets-icon.svg";
 $("userCountry").textContent=u.country_name?`${u.country_name} · ${u.country_code}`:"Country not set";
}
function renderAccess(){
 const a=state.access, c=$("accessCard");
 if(a?.paid&&!a.plan){c.innerHTML=`<strong>Developer override</strong><span>Server access override is active.</span>`;return;}
 if(!a?.paid){c.innerHTML=`<strong>No active plan</strong><span>Choose a plan below to unlock live signals.</span>`;return;}
 const p=state.plans[a.plan];$("accessPlan").textContent=p?p.name.toUpperCase():a.plan.toUpperCase();
 $("accessExpiry").textContent=a.expires?`Expires ${new Date(a.expires).toLocaleString()}`:"Active";
 c.innerHTML=`<strong>${esc(p?.name||a.plan)} · ${money(planAmount(p),planCurrency())}</strong><span>Active until ${new Date(a.expires).toLocaleString()}</span>`;
}
function renderSignals(){
 const grid=$("signalGrid");
 if(!state.access?.paid){grid.innerHTML=["BTC","GOLD"].map(m=>`<article class="signal-card locked"><div class="signal-top"><div class="market">${m}</div><div class="direction wait">LOCKED</div></div><div class="price">••••••</div><div class="strength">Live signal hidden until your paid access is active.</div></article>`).join("");$("lockedCard").style.display="flex";return;}
 $("lockedCard").style.display="none";
 grid.innerHTML=["BTC","GOLD"].map(m=>{const s=state.signals[m];if(!s)return `<article class="signal-card"><div class="signal-top"><div class="market">${m}</div><div class="direction wait">WAIT</div></div><div class="price">--</div><div class="strength">No current signal.</div></article>`;const dir=(s.direction||"WAIT").toLowerCase();return `<article class="signal-card ${dir}"><div class="signal-top"><div class="market">KETS ${esc(s.market||m)}</div><div class="direction ${dir}">${esc(s.direction)}</div></div><div class="price">${money(s.current_price??s.price)}</div><div class="entry-line">Entry ${money(s.entry??s.price)}</div><div class="strength"><span>Strength ${Number(s.score??s.strength??0)}%</span><div class="bar"><i style="width:${Math.min(100,Number(s.score??s.strength??0))}%"></i></div></div><div class="metrics"><div class="metric"><span>Market move</span><strong>${money(s.price_move)}</strong></div><div class="metric"><span>Signal duration</span><strong>${signalAge(s.timestamp)}</strong></div><div class="metric"><span>Expected move</span><strong>${money(s.expected_move)}</strong></div><div class="metric"><span>Estimated duration</span><strong>${esc(s.estimated_duration||"--")}</strong></div><div class="metric"><span>Take profit</span><strong>${money(s.take_profit)}</strong></div><div class="metric"><span>Stop loss</span><strong>${money(s.stop_loss)}</strong></div></div></article>`;}).join("");
}
function renderHistory(){
 const el=$("historyList");if(!state.history.length){el.innerHTML=`<div class="empty">No signals recorded in the last 7 days.</div>`;return;}
 el.innerHTML=state.history.slice().reverse().slice(0,80).map(s=>`<div class="history-row"><div><div class="history-market">${esc(s.market||s.asset||"")}</div><div class="history-meta">${esc(s.timestamp?new Date(s.timestamp).toLocaleString():"")}</div></div><div class="history-dir ${(s.direction||"").toLowerCase()}">${esc(s.direction||"--")} · ${esc(s.strength||0)}%</div><div class="history-price">${money(s.price)}</div></div>`).join("");
}
function renderPayments(){
 const el=$("paymentHistory");if(!state.payments.length){el.innerHTML=`<div class="empty">No payments yet.</div>`;return;}
 el.innerHTML=state.payments.map(p=>`<div class="history-row"><div><div class="history-market">${esc(state.plans[p.plan]?.name||p.plan)}</div><div class="history-meta">${esc(new Date(p.created_at).toLocaleString())} · ${esc(p.network||"Pesapal")}</div></div><div class="history-dir ${p.status==="COMPLETED"?"buy":"wait"}">${esc(p.status)}</div><div class="history-price">${money(p.amount)}</div></div>`).join("");
}
function renderCommunity(){
 const el=$("communityGrid"), order=["30_min","1_hour","4_hour","1_day","1_week","1_month","1_year"];
 el.innerHTML=order.map(k=>`<div class="community-card"><span>${esc(state.plans[k]?.name||k)}</span><strong>${Number(state.community[k]||0).toLocaleString()}</strong><small>active paid users</small></div>`).join("");
}
function renderStatus(){
 const w=state.status.signal_window||{};$("engineStatus").textContent=state.status.engine_running?"Engine online":"Engine offline";
 $("signalWindow").textContent=w.active?countdown(w.seconds_to_stop):countdown(w.seconds_to_start);$("windowLabel").textContent=w.active?"Time left before signals stop":"Until signals start at 06:00 EAT";$("nextBroadcast").textContent=countdown(state.status.next_broadcast_seconds);
}
function renderPlans(){
 const abroad=!isUganda();
 const entries=Object.entries(state.plans).filter(([id,p])=>!abroad || p.usd!=null);
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
 const u=state.user,m=$("profileModal");m.classList.remove("hidden");m.innerHTML=`<div class="modal-box"><button class="close-btn" onclick="this.classList.add('x');document.getElementById('profileModal').classList.add('hidden')">×</button><span class="eyebrow">ACCOUNT</span><h2>Your profile</h2><div class="profile-editor"><img id="editAvatar" class="avatar large" src="${esc(u.profile_picture||"/static/kets-icon.svg")}"><label class="upload-btn">Change picture<input id="avatarFile" type="file" accept="image/png,image/jpeg,image/webp"></label></div><label>Full name<input id="editName" value="${esc(u.name||"")}"></label><label>Country<select id="editCountry">${countryOptions(u.country_code)}</select></label><div id="profileMsg" class="form-message"></div><button class="primary-btn full" id="saveProfile">Save profile</button><button class="danger-btn full" id="logoutBtn">Sign out</button></div>`;
 $("avatarFile").onchange=e=>{const f=e.target.files[0];if(!f)return;if(f.size>800000){$("profileMsg").textContent="Image must be under 800 KB.";return}const rd=new FileReader();rd.onload=()=>{$("editAvatar").src=rd.result;$("editAvatar").dataset.value=rd.result};rd.readAsDataURL(f);};
 $("saveProfile").onclick=async()=>{try{const c=$("editCountry").value,n=COUNTRIES.find(x=>x[0]===c);const d=await api("/api/auth/profile",{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:$("editName").value.trim(),country_code:c,country_name:n?n[1]:"",profile_picture:$("editAvatar").dataset.value||u.profile_picture||""})});state.user=d.user;m.classList.add("hidden");renderProfile();}catch(e){$("profileMsg").textContent=e.message;}};
 $("logoutBtn").onclick=()=>{state.token="";sessionStorage.removeItem("kets_user_token");location.reload();};
}
async function loadAll(){
 try{
  const me=await api("/api/auth/me");state.user=me.user;state.access=me.access;
  const [status,history,access,plans,community,payments]=await Promise.all([api("/api/status"),api("/api/history"),api("/api/access"),api("/api/plans"),api("/api/community"),api("/api/payments/history")]);
  state.status=status;state.history=history.history||[];state.access=access;state.plans=plans.plans||{};state.community=community.counts||{};state.payments=payments.payments||[];
  if(state.access.paid)try{state.signals=(await api("/api/signals")).signals||{};}catch{state.signals={};}else state.signals={};
  showApp();renderProfile();renderAccess();renderStatus();renderSignals();renderHistory();renderPayments();renderCommunity();renderPlans();
 }catch(e){state.token="";sessionStorage.removeItem("kets_user_token");showAuth("login");if($("authMsg"))authMsg(e.message);}
}
document.querySelectorAll(".tab").forEach(b=>b.onclick=()=>showAuth(b.dataset.tab));
$("regCountry").innerHTML+=countryOptions();
$("loginBtn").onclick=login;$("registerBtn").onclick=register;$("googleBtn").onclick=google;$("profileBtn").onclick=openProfile;$("editProfileBtn").onclick=openProfile;$("plansBtn").onclick=()=>document.getElementById("plansSection").scrollIntoView({behavior:"smooth"});
const q=new URLSearchParams(location.search);if(q.get("google")==="success"||q.get("payment")==="success")history.replaceState({},document.title,"/");if(location.hash)history.replaceState({},document.title,"/");
if(state.token)loadAll();else showAuth("login");
setInterval(()=>{if(state.token&&!$("app").classList.contains("hidden"))loadAll()},REFRESH_MS);

if("serviceWorker" in navigator){navigator.serviceWorker.register("/service-worker.js").catch(()=>{});}
