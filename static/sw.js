const CACHE = 'invoice-classifier-v1';
const STATIC = [
  '/',
  '/dashboard',
  '/static/manifest.json',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  'https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=DM+Sans:ital,wght@0,300;0,400;0,500;1,300&display=swap',
];

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll(STATIC)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);

  // Always go network-first for API calls and auth
  if (
    url.pathname.startsWith('/classify') ||
    url.pathname.startsWith('/auth') ||
    url.pathname.startsWith('/drive') ||
    url.pathname.startsWith('/webhook') ||
    url.pathname.startsWith('/invoices') ||
    url.pathname.startsWith('/export')
  ) {
    e.respondWith(
      fetch(e.request).catch(() =>
        new Response(JSON.stringify({ error: 'You are offline' }), {
          headers: { 'Content-Type': 'application/json' },
          status: 503,
        })
      )
    );
    return;
  }

  // Cache-first for static assets
  e.respondWith(
    caches.match(e.request).then(cached => {
      if (cached) return cached;
      return fetch(e.request).then(res => {
        if (res && res.status === 200 && res.type !== 'opaque') {
          const clone = res.clone();
          caches.open(CACHE).then(c => c.put(e.request, clone));
        }
        return res;
      });
    })
  );
});

// Background sync for offline uploads (queued when offline, sent when back online)
self.addEventListener('sync', e => {
  if (e.tag === 'sync-invoices') {
    e.waitUntil(syncQueuedInvoices());
  }
});

async function syncQueuedInvoices() {
  // Notify all open clients to retry any queued uploads
  const clients = await self.clients.matchAll({ type: 'window' });
  clients.forEach(c => c.postMessage({ type: 'SYNC_READY' }));
}
