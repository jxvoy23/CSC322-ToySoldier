'use strict';

(() => {
  const PRODUCTS = [
    {id: 'BASE_SKU_GREEN', sku: 'SKU_GREEN', name: 'Common Infantry', price: 2, label: 'green'},
    {id: 'BASE_SKU_SILVER', sku: 'SKU_SILVER', name: 'Premium Officer', price: 3, label: 'silver'},
    {id: 'OFFER_CHARITY_MYSTERY_POUCH', sku: 'SKU_MYSTERY_POUCH', name: 'Charity Mystery Pouch', price: 3, label: 'pouch'},
    {id: 'BASE_SKU_GOLD', sku: 'SKU_GOLD', name: 'Gold Commander', price: 5, label: 'gold'},
    {id: 'PRIME_SKU_SILVER', sku: 'SKU_SILVER', name: 'Soldier Prime · Silver Officer', price: 2},
    {id: 'PRIME_SKU_GOLD', sku: 'SKU_GOLD', name: 'Soldier Prime · Gold Commander', price: 4}
  ];
  const knownProducts = new Map(PRODUCTS.map(product => [product.id, product]));
  const $ = id => document.getElementById(id);
  const elements = new Map();
  const el = id => {
    if (!elements.has(id)) elements.set(id, $(id));
    return elements.get(id);
  };
  const text = (id, value) => { if (el(id)) el(id).textContent = value; };
  const money = value => '$' + Number(value).toFixed(2);
  const isPrime = id => typeof id === 'string' && id.startsWith('PRIME_');
  const activeId = id => prime && ['BASE_SKU_SILVER', 'BASE_SKU_GOLD'].includes(id)
    ? id.replace('BASE_', 'PRIME_') : id;
  const buttons = new Map(PRODUCTS.slice(0, 4).map(product =>
    [product.id, document.querySelector('button[data-select="' + product.id + '"]')]));
  let offers = new Map();
  let catalogTime = null;
  let verified = false;
  let prime = false;
  let refreshing = false;
  let tickerUnsubscribe = null;
  let tickerConnecting = false;
  let tickerGeneration = 0;
  let sdkPromise = null;

  function feedback(message, error = false) {
    text('orderSuccess', message);
    if (el('orderSuccess')) el('orderSuccess').classList.toggle('error', error);
  }

  function available(offer) {
    return verified && offer && offer.physical_available > 0;
  }

  function updateSummary() {
    const id = el('itemChoice').value;
    const product = knownProducts.get(id);
    const offer = offers.get(id);
    const eligible = !isPrime(id) || el('returningCustomer').checked;
    text('selectedName', product ? product.name : 'No item selected');
    text('selectedPrice', product ? money(offer ? offer.total_price : product.price) : '—');
    text('selectedStatus', !product ? 'Choose an item to see its price.'
      : !verified ? 'Price and stock are unverified. Refresh the catalog before saving.'
      : !available(offer) ? 'This offer is unavailable for pickup.'
      : !eligible ? 'Confirm Soldier Prime eligibility above.'
      : 'Quantity 1 · Pay in person after seller confirmation.');
    el('submitOrder').disabled = !product || !available(offer) || !eligible;
  }

  function render() {
    for (const product of PRODUCTS.slice(0, 4)) {
      const id = activeId(product.id);
      const offer = offers.get(id);
      const reference = knownProducts.get(id);
      text('price-' + product.id, offer ? money(offer.total_price) : verified ? '—' : money(reference.price));
      text('phys-' + product.id, 'Physical Stock: ' + (offer
        ? offer.physical_available + ' units' + (verified ? '' : ' (last known)')
        : verified ? 'Offer unavailable' : 'Not verified'));
      text('virt-' + product.id, product.sku === 'SKU_MYSTERY_POUCH'
        ? 'Simulated Virtual Stock: 0 (In-Person Only)'
        : 'Simulated Virtual Stock: ' + (offer
          ? offer.virtual_available + ' units' + (verified ? '' : ' (last known)')
          : verified ? 'Offer unavailable' : 'Not verified'));
      const button = buttons.get(product.id);
      if (button) {
        button.disabled = !available(offer);
        button.firstChild.textContent = !verified ? 'Verify availability '
          : button.disabled ? 'Unavailable for pickup ' : 'Choose ' + product.label + ' ';
      }
    }
    for (const metal of ['silver', 'gold']) {
      for (const mode of ['base', 'prime']) {
        const id = mode.toUpperCase() + '_SKU_' + metal.toUpperCase();
        const offer = offers.get(id);
        text('compare-' + metal + '-' + mode,
          offer ? money(offer.total_price) : verified ? '—' : money(knownProducts.get(id).price));
      }
    }
    text('primePriceNote', verified
      ? 'Current published prices. A dash means an offer is not published. Any Prime discount is already included.'
      : 'Reference or last known prices. Refresh to verify current offers.');
    el('standardMode').setAttribute('aria-pressed', String(!prime));
    el('primeMode').setAttribute('aria-pressed', String(prime));
    text('modeNotice', prime
      ? 'Soldier Prime requires one completed prior purchase. Published discounts are included. Limit 1 item per customer.'
      : 'Standard pricing. Limit 1 item per customer.');
    const current = el('itemChoice').value;
    const options = [new Option('Choose one item', '')];
    for (const product of PRODUCTS) {
      const offer = offers.get(product.id);
      const option = new Option(product.name + ' · ' + (offer ? money(offer.total_price)
        : verified ? 'unavailable' : money(product.price) + ' reference'), product.id);
      option.disabled = !available(offer);
      options.push(option);
    }
    el('itemChoice').replaceChildren(...options);
    if (knownProducts.has(current)) el('itemChoice').value = current;
    updateSummary();
  }

  function validateCatalog(data) {
    if (!Array.isArray(data) || !data.length || data.length > 100) throw Error('Invalid catalog');
    const result = new Map();
    const timestamps = new Set();
    for (const offer of data) {
      if (!offer || !knownProducts.has(offer.offer_id)) throw Error('Unknown offer');
      if (result.has(offer.offer_id)) throw Error('Duplicate offer');
      const product = knownProducts.get(offer.offer_id);
      const quantities = offer.product_quantities;
      if (!Number.isInteger(offer.total_price) || offer.total_price < 1 || offer.total_price > 5
          || !Number.isInteger(offer.physical_available) || offer.physical_available < 0
          || !Number.isInteger(offer.virtual_available) || offer.virtual_available < 0
          || !quantities || Object.keys(quantities).length !== 1 || quantities[product.sku] !== 1
          || typeof offer.eligibility !== 'string' || typeof offer.loyalty_program_terms !== 'string'
          || typeof offer.timestamp !== 'string' || !/(Z|[+-]\d{2}:\d{2})$/.test(offer.timestamp)
          || !Number.isFinite(Date.parse(offer.timestamp))) {
        throw Error('Invalid offer fields');
      }
      if (product.sku === 'SKU_MYSTERY_POUCH' && offer.virtual_available !== 0) {
        throw Error('Mystery pouches must be physical only');
      }
      if (isPrime(offer.offer_id) && !offer.eligibility.includes('RETURNING_CUSTOMER')) {
        throw Error('Prime eligibility is missing');
      }
      timestamps.add(offer.timestamp);
      result.set(offer.offer_id, offer);
    }
    if (timestamps.size !== 1) throw Error('Mixed catalog snapshots');
    for (const sku of ['SKU_SILVER', 'SKU_GOLD']) {
      const base = result.get('BASE_' + sku);
      const loyal = result.get('PRIME_' + sku);
      if (base && loyal && (base.virtual_available !== loyal.virtual_available
          || base.physical_available !== loyal.physical_available || loyal.total_price > base.total_price)) {
        throw Error('Inconsistent shared stock or Prime price');
      }
    }
    return {offers: result, timestamp: [...timestamps][0]};
  }

  async function fetchJSON(path) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 6000);
    try {
      const response = await fetch(path, {cache: 'no-store', signal: controller.signal});
      if (!response.ok) throw Error('File unavailable');
      return await response.json();
    } finally {
      clearTimeout(timeout);
    }
  }

  async function refreshHistory() {
    try {
      const history = await fetchJSON('decision_audit_log.json');
      if (!Array.isArray(history)) throw Error('Invalid history');
      const rows = history.slice(-50).reverse().map(entry => {
        if (!entry || typeof entry.offer_id !== 'string'
            || !Number.isFinite(Date.parse(entry.timestamp))
            || !Number.isFinite(entry.total_price) || typeof entry.eligibility !== 'string') {
          throw Error('Invalid history entry');
        }
        const row = document.createElement('tr');
        for (const value of [
          new Date(entry.timestamp).toLocaleString(), entry.offer_id,
          entry.previous_price == null ? 'Initial' : money(entry.previous_price),
          entry.active === false ? 'Withdrawn' : money(entry.total_price), entry.eligibility.replaceAll('_', ' ')
        ]) {
          const cell = document.createElement('td');
          cell.textContent = value;
          row.append(cell);
        }
        return row;
      });
      el('priceHistoryBody').replaceChildren(...rows);
      text('historyStatus', history.length
        ? 'Showing the latest ' + rows.length + ' published price or eligibility changes. Download the full history for earlier entries.'
        : 'No published price changes are recorded.');
    } catch {
      text('historyStatus', 'Price history is temporarily unavailable. The catalog status is shown above.');
    }
  }

  async function refreshCatalog() {
    if (refreshing) return;
    refreshing = true;
    el('retrySync').disabled = true;
    void refreshHistory();
    try {
      const candidate = validateCatalog(await fetchJSON('published_price_file.json'));
      if (catalogTime && Date.parse(candidate.timestamp) < Date.parse(catalogTime)) {
        throw Error('Older catalog received');
      }
      offers = candidate.offers;
      catalogTime = candidate.timestamp;
      verified = true;
      text('lastUpdated', new Date(catalogTime).toLocaleString());
      text('syncStatus', 'Published catalog verified · ' + new Date(catalogTime).toLocaleString());
      el('catalogStatus').classList.add('is-live');
    } catch {
      verified = false;
      text('syncStatus', offers.size
        ? 'Refresh failed · Last known prices shown. Pickup requests are paused.'
        : 'Catalog unavailable · Reference prices shown. Refresh to verify stock.');
      text('lastUpdated', catalogTime ? new Date(catalogTime).toLocaleString() + ' (last known)' : 'Unverified');
      el('catalogStatus').classList.remove('is-live');
    } finally {
      refreshing = false;
      el('retrySync').disabled = false;
      render();
    }
  }

  function loadScript(url) {
    return new Promise((resolve, reject) => {
      const script = document.createElement('script');
      const timer = setTimeout(() => { script.remove(); reject(Error('SDK timeout')); }, 5000);
      script.src = url;
      script.async = true;
      script.onload = () => { clearTimeout(timer); resolve(); };
      script.onerror = () => { clearTimeout(timer); script.remove(); reject(Error('SDK unavailable')); };
      document.head.append(script);
    });
  }

  async function loadFirebase() {
    if (window.firebase && window.firebase.firestore) return;
    if (!sdkPromise) {
      sdkPromise = (async () => {
        if (!window.firebase) await loadScript('https://www.gstatic.com/firebasejs/10.7.1/firebase-app-compat.js');
        if (!window.firebase.firestore) await loadScript('https://www.gstatic.com/firebasejs/10.7.1/firebase-firestore-compat.js');
      })().catch(error => { sdkPromise = null; throw error; });
    }
    await sdkPromise;
  }

  async function initTicker() {
    if (tickerConnecting || tickerUnsubscribe) return;
    if (['localhost', '127.0.0.1', '[::1]'].includes(location.hostname)
        && !new URLSearchParams(location.search).has('live')) {
      text('tickerText', 'Local preview · Published catalog is available below.');
      text('livePingCount', 'Live ticker disabled');
      return;
    }
    tickerConnecting = true;
    const generation = ++tickerGeneration;
    try {
      await loadFirebase();
      if (generation !== tickerGeneration) return;
      const firebase = window.firebase;
      const app = firebase.apps.length ? firebase.app() : firebase.initializeApp({
        apiKey: 'AIzaSyUpWnmxqu2nSwvcTSyTPhZfeWMVFCFB8BA',
        authDomain: 'toy-soldier-9bf43.firebaseapp.com',
        projectId: 'toy-soldier-9bf43'
      });
      tickerUnsubscribe = app.firestore().collection('market_feed')
        .orderBy('timestamp', 'desc').limit(1).onSnapshot(snapshot => {
          if (generation !== tickerGeneration) return;
          text('livePingCount', 'Simulated feed connected');
          const latest = snapshot.docs[0];
          if (!latest) { text('tickerText', 'Simulated market feed · Awaiting official trades.'); return; }
          const trade = latest.data();
          if (trade.market && trade.market !== 'simulated') return;
          if (typeof trade.seller_id !== 'string' || typeof trade.sku_id !== 'string'
              || !Number.isFinite(trade.price_paid) || !Number.isInteger(trade.remaining_virtual)
              || trade.remaining_virtual < 0) return;
          text('tickerText', 'Recent simulated trade · ' + trade.seller_id.slice(0, 200)
            + ' · ' + trade.sku_id.slice(0, 200) + ' · ' + money(trade.price_paid)
            + ' · Virtual stock remaining: ' + trade.remaining_virtual);
        }, () => {
          if (generation !== tickerGeneration) return;
          tickerUnsubscribe = null;
          text('tickerText', 'Simulated activity feed is unavailable. Published prices are shown below.');
          text('livePingCount', 'Ticker offline');
        });
    } catch {
      text('tickerText', 'Simulated activity feed is unavailable. Published prices are shown below.');
      text('livePingCount', 'Ticker offline');
    } finally {
      tickerConnecting = false;
    }
  }

  function setMode(value) {
    prime = value;
    const current = el('itemChoice').value;
    render();
    if (['BASE_SKU_SILVER', 'BASE_SKU_GOLD'].includes(current) || isPrime(current)) {
      el('itemChoice').value = value ? current.replace('BASE_', 'PRIME_') : current.replace('PRIME_', 'BASE_');
      updateSummary();
    }
    feedback('');
  }

  el('standardMode').addEventListener('click', () => setMode(false));
  el('primeMode').addEventListener('click', () => setMode(true));
  el('itemChoice').addEventListener('change', () => { feedback(''); updateSummary(); });
  el('returningCustomer').addEventListener('change', () => { feedback(''); updateSummary(); });
  document.querySelectorAll('[data-select]').forEach(button => button.addEventListener('click', event => {
    event.preventDefault();
    const id = activeId(button.dataset.select);
    if (!available(offers.get(id))) { feedback('Refresh the catalog or choose an available item.', true); return; }
    el('itemChoice').value = id;
    feedback('');
    updateSummary();
    el('reserve').scrollIntoView({behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'});
    el('itemChoice').focus({preventScroll: true});
  }));
  el('retrySync').addEventListener('click', () => { void refreshCatalog(); void initTicker(); });
  el('orderForm').addEventListener('submit', event => {
    event.preventDefault();
    if (!el('orderForm').reportValidity()) return;
    const id = el('itemChoice').value;
    const product = knownProducts.get(id);
    const offer = offers.get(id);
    if (!product || !available(offer)) { feedback('Choose a verified, available offer.', true); return; }
    if (isPrime(id) && !el('returningCustomer').checked) {
      feedback('Confirm your prior purchase to use Soldier Prime.', true);
      el('returningCustomer').focus();
      return;
    }
    const name = el('custName').value.trim();
    const email = el('custEmail').value.trim();
    if (!name || !email) { feedback('Enter your name and contact email.', true); return; }
    const lines = [
      'VANGUARD GARRISON — PICKUP REQUEST',
      'NOT SUBMITTED OR CONFIRMED. Show this request to the campus seller.', '',
      'Buyer: ' + name, 'Email: ' + email, 'Item: ' + product.name, 'Offer ID: ' + id,
      'Quantity: 1', 'Price: ' + money(offer.total_price) + ' (seller confirmation required)',
      'Catalog timestamp: ' + catalogTime, 'Eligibility: ' + offer.eligibility,
      'Prior purchase declared: ' + (el('returningCustomer').checked ? 'Yes' : 'No'),
      'Preferred pickup: ' + el('pickupLocation').value, 'Notes: ' + el('notes').value.trim(), '',
      'Pay exact whole-dollar cash after the seller confirms. Saving this request does not hold inventory.'
    ];
    const url = URL.createObjectURL(new Blob([lines.join('\n')], {type: 'text/plain;charset=utf-8'}));
    const link = document.createElement('a');
    link.href = url;
    link.download = 'vanguard-pickup-request.txt';
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    feedback('Request saved to your device. Show it to the seller to confirm price, stock, and pickup. Nothing was sent or reserved.');
    el('orderSuccess').focus();
  });

  render();
  void refreshCatalog();
  void initTicker();
  setInterval(() => { if (!document.hidden) void refreshCatalog(); }, 30000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) void refreshCatalog(); });
  window.addEventListener('pagehide', () => {
    tickerGeneration++;
    if (tickerUnsubscribe) tickerUnsubscribe();
    tickerUnsubscribe = null;
  });
  window.addEventListener('pageshow', event => { if (event.persisted) { void refreshCatalog(); void initTicker(); } });
})();
