// no caching on purpose: data must always be fresh; this only makes the page installable
self.addEventListener('fetch', () => {});
