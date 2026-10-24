const CACHE="kets-display-v11-buttons-transparent-20261024";
const APP_SHELL=["/","/index.html","/styles.css?v=20261024buttonsfix","/app.js?v=20261024buttonsfix","/manifest.json","/eric-investments-background.png","/icon-192.png","/icon-512.png"];
self.addEventListener("install",event=>{
  event.waitUntil((async()=>{
    const cache=await caches.open(CACHE);
    await Promise.all(APP_SHELL.map(async url=>{try{const response=await fetch(url,{cache:"reload"});if(response.ok)await cache.put(url,response);}catch(_){}}));
    await self.skipWaiting();
  })());
});
self.addEventListener("activate",event=>{
  event.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(k=>k!==CACHE).map(k=>caches.delete(k)))).then(()=>self.clients.claim()));
});
self.addEventListener("fetch",event=>{
  if(event.request.method!=="GET"||!event.request.url.startsWith(self.location.origin))return;
  event.respondWith((async()=>{
    try{
      const response=await fetch(event.request,{cache:"no-store"});
      if(response.ok){const cache=await caches.open(CACHE);cache.put(event.request,response.clone()).catch(()=>{});}
      return response;
    }catch(_){return (await caches.match(event.request))||(await caches.match("/"))||Response.error();}
  })());
});
