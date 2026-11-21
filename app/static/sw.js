const CACHE='tharai-basket-shell-v11';
const SHELL=[
 '/static/css/style.css',
 '/static/js/app.js',
 '/static/manifest.json',
 '/static/images/tharai-basket-app-icon.svg',
 '/static/images/tharai-basket-logo.webp',
 '/static/offline.html'
];
self.addEventListener('install',event=>{
 event.waitUntil(caches.open(CACHE).then(cache=>cache.addAll(SHELL)).then(()=>self.skipWaiting()));
});
self.addEventListener('activate',event=>{
 event.waitUntil(caches.keys().then(keys=>Promise.all(
  keys.filter(key=>key.startsWith('tharai-basket-shell-')&&key!==CACHE).map(key=>caches.delete(key))
 )).then(()=>self.clients.claim()));
});
self.addEventListener('fetch',event=>{
 const request=event.request;
 if(request.method!=='GET') return;
 const url=new URL(request.url);
 if(url.origin!==self.location.origin) return;
 if(request.mode==='navigate'){
  event.respondWith(fetch(request).catch(async()=>{
   const cache=await caches.open(CACHE);
   return (await cache.match('/static/offline.html'))||new Response('You are offline.',{status:503,headers:{'Content-Type':'text/plain'}});
  }));
  return;
 }
 if(!url.pathname.startsWith('/static/')) return;
 event.respondWith(caches.open(CACHE).then(async cache=>{
  const cached=await cache.match(request);
  if(cached){
   event.waitUntil(fetch(request).then(response=>{
    if(response.ok) return cache.put(request,response.clone());
   }).catch(()=>{}));
   return cached;
  }
  try{
   const response=await fetch(request);
   if(response.ok) cache.put(request,response.clone());
   return response;
  }catch(error){
   return new Response('',{status:504,statusText:'Offline'});
  }
 }));
});
