self.addEventListener('install',e=>self.skipWaiting());
self.addEventListener('activate',e=>e.waitUntil(self.clients.claim()));
self.addEventListener('push',e=>{let data={title:'Gantu dashboard',body:'A new location report is available.'};try{Object.assign(data,e.data.json())}catch(_){}e.waitUntil(self.registration.showNotification(data.title,{body:data.body,tag:'gantu-location',icon:'/icon.svg',data:{url:'/'}}))});
self.addEventListener('notificationclick',e=>{e.notification.close();e.waitUntil(self.clients.matchAll({type:'window',includeUncontrolled:true}).then(async wins=>{for(const w of wins){if('focus' in w){await w.focus();return}}return self.clients.openWindow('/')}))});
