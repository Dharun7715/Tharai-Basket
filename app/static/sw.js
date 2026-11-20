const CACHE='tharai-basket-shell-v8';
const SHELL=[
 '/static/css/style.css',
 '/static/js/app.js',
 '/static/manifest.json',
 '/static/images/tharai-basket-app-icon.svg',
 '/static/images/tharai-basket-logo.webp'
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
 if(url.origin!==self.location.origin||!url.pathname.startsWith('/static/')) return;
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
