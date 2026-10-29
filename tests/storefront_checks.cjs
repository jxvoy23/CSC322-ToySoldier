/* Optional browser regression checks.
 * Install Playwright and Chromium, then run: node tests/storefront_checks.cjs
 * All non-local requests are blocked. No production Firestore access occurs.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const {chromium} = require('playwright');

const publicRoot = path.resolve(__dirname, '../public');
const catalog = JSON.parse(fs.readFileSync(path.join(publicRoot, 'published_price_file.json'), 'utf8'));
const clone = value => JSON.parse(JSON.stringify(value));

const server = http.createServer((request, response) => {
  const pathname = decodeURIComponent(new URL(request.url, 'http://localhost').pathname);
  const file = path.resolve(publicRoot, '.' + (pathname === '/' ? '/index.html' : pathname));
  if (!file.startsWith(publicRoot + path.sep)) { response.writeHead(403).end(); return; }
  fs.readFile(file, (error, content) => {
    if (error) { response.writeHead(404).end(); return; }
    const types = {'.html': 'text/html', '.js': 'text/javascript', '.json': 'application/json'};
    response.writeHead(200, {'Content-Type': types[path.extname(file)] || 'text/plain', 'Cache-Control': 'no-store'});
    response.end(content);
  });
});

(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = 'http://127.0.0.1:' + server.address().port;
  let browser;
  let passed = 0;
  const downloadDirectory = fs.mkdtempSync(path.join(os.tmpdir(), 'vanguard-checks-'));
  try {
    const launchOptions = {headless: true};
    if (process.env.CHROMIUM_EXECUTABLE_PATH) launchOptions.executablePath = process.env.CHROMIUM_EXECUTABLE_PATH;
    if (process.env.CHROMIUM_ARGS) launchOptions.args = JSON.parse(process.env.CHROMIUM_ARGS);
    browser = await chromium.launch(launchOptions);
    async function scenario(name, action, beforeLoad) {
      const context = await browser.newContext({acceptDownloads: true});
      const errors = [];
      await context.route('**/*', route => {
        if (new URL(route.request().url()).origin === base) return route.continue();
        return route.abort();
      });
      const page = await context.newPage();
      page.on('pageerror', error => errors.push(error.message));
      try {
        if (beforeLoad) await beforeLoad(page);
        await action(page);
        assert.deepEqual(errors, [], 'No uncaught browser errors');
        console.log('PASS ' + name);
        passed++;
      } finally {
        await context.close();
      }
    }
    const verified = page => page.waitForFunction(() =>
      document.getElementById('syncStatus').textContent.startsWith('Published catalog verified'));
    const unavailable = page => page.waitForFunction(() =>
      document.getElementById('syncStatus').textContent.includes('Catalog unavailable'));

    await scenario('catalog, Prime selection, and complete pickup download', async page => {
      await page.goto(base);
      await verified(page);
      await page.click('#primeMode'); // No selected item: regression for the old null.startsWith crash.
      await page.click('button[data-select="BASE_SKU_SILVER"]');
      assert.equal(await page.locator('#itemChoice').inputValue(), 'PRIME_SKU_SILVER');
      assert.equal(await page.locator('#submitOrder').isDisabled(), true);
      await page.check('#returningCustomer');
      await page.fill('#custName', 'Test Buyer');
      await page.fill('#custEmail', 'test@example.com');
      await page.selectOption('#pickupLocation', {label: 'Campus Library'});
      const downloadPromise = page.waitForEvent('download');
      await page.click('#submitOrder');
      const download = await downloadPromise;
      const output = path.join(downloadDirectory, download.suggestedFilename());
      await download.saveAs(output);
      const content = fs.readFileSync(output, 'utf8');
      assert.match(content, /Offer ID: PRIME_SKU_SILVER/);
      assert.match(content, /Price: \$2\.00/);
      assert.match(content, /NOT SUBMITTED OR CONFIRMED/);
      assert.equal(await page.locator('#custEmail').inputValue(), 'test@example.com');
      assert.equal(await page.locator('#priceHistoryBody tr').count() > 0, true);
      if (process.env.SCREENSHOT_DIR) {
        fs.mkdirSync(process.env.SCREENSHOT_DIR, {recursive: true});
        await page.screenshot({path: path.join(process.env.SCREENSHOT_DIR, 'storefront-desktop.png'), fullPage: true});
      }
    });

    await scenario('invalid catalog cannot enable a pickup request', async page => {
      await page.goto(base);
      await unavailable(page);
      assert.equal(await page.locator('button[data-select="BASE_SKU_GREEN"]').isDisabled(), true);
      assert.equal(await page.locator('#submitOrder').isDisabled(), true);
    }, page => page.route('**/published_price_file.json', route => route.fulfill({
      contentType: 'application/json', body: JSON.stringify([catalog[0], catalog[0]])})));

    await scenario('physical stock controls pickup independently of virtual stock', async page => {
      await page.goto(base);
      await verified(page);
      assert.equal(await page.locator('button[data-select="BASE_SKU_GREEN"]').isDisabled(), true);
      assert.equal(await page.locator('button[data-select="BASE_SKU_GOLD"]').isDisabled(), false);
    }, page => {
      const rows = clone(catalog);
      rows.find(row => row.offer_id === 'BASE_SKU_GREEN').physical_available = 0;
      for (const row of rows.filter(row => row.offer_id.endsWith('SKU_GOLD'))) row.virtual_available = 0;
      return page.route('**/published_price_file.json', route => route.fulfill({
        contentType: 'application/json', body: JSON.stringify(rows)}));
    });

    await scenario('failed refresh retains marked prices and a retry recovers', async page => {
      await page.goto(base);
      await verified(page);
      await page.route('**/published_price_file.json', route => route.abort());
      await page.click('#retrySync');
      await page.waitForFunction(() => document.getElementById('syncStatus').textContent.includes('Refresh failed'));
      assert.equal(await page.locator('button[data-select="BASE_SKU_GREEN"]').isDisabled(), true);
      assert.match(await page.locator('#phys-BASE_SKU_GREEN').textContent(), /last known/);
      await page.unroute('**/published_price_file.json');
      await page.click('#retrySync');
      await verified(page);
      assert.equal(await page.locator('button[data-select="BASE_SKU_GREEN"]').isDisabled(), false);
    });

    await scenario('a Firebase SDK failure does not block the catalog', async page => {
      await page.goto(base + '/?live');
      await verified(page);
      await page.waitForFunction(() => document.getElementById('livePingCount').textContent === 'Ticker offline');
      assert.equal(await page.locator('button[data-select="BASE_SKU_GREEN"]').isDisabled(), false);
    });

    await scenario('trade text is escaped and refreshes do not multiply subscriptions', async page => {
      await page.goto(base + '/?live');
      await verified(page);
      await page.waitForFunction(() => document.getElementById('tickerText').textContent.includes('<img'));
      assert.equal(await page.locator('img[src="x"]').count(), 0);
      for (let index = 0; index < 3; index++) {
        await page.click('#retrySync');
        await page.waitForFunction(() => !document.getElementById('retrySync').disabled);
      }
      assert.equal(await page.evaluate(() => window.subscriptionCount), 1);
    }, page => page.addInitScript(() => {
      window.subscriptionCount = 0;
      const database = {collection: () => ({orderBy: () => ({limit: () => ({
        onSnapshot: callback => {
          window.subscriptionCount++;
          queueMicrotask(() => callback({docs: [{data: () => ({
            seller_id: '<img src="x" onerror="throw 1">', sku_id: 'SKU_SILVER',
            price_paid: 3, remaining_virtual: 15, market: 'simulated'
          })}]}));
          return () => {};
        }
      })})})};
      window.firebase = {apps: [{}], firestore: () => database,
        app: () => ({firestore: () => database})};
    }));

    await scenario('missing Prime offers are unavailable without affecting Standard', async page => {
      await page.goto(base);
      await verified(page);
      await page.click('#primeMode');
      assert.equal(await page.locator('button[data-select="BASE_SKU_SILVER"]').isDisabled(), true);
      await page.click('#standardMode');
      assert.equal(await page.locator('button[data-select="BASE_SKU_SILVER"]').isDisabled(), false);
    }, page => page.route('**/published_price_file.json', route => route.fulfill({
      contentType: 'application/json', body: JSON.stringify(catalog.filter(row => !row.offer_id.startsWith('PRIME_')))})));

    await scenario('mobile storefront fits the viewport', async page => {
      await page.setViewportSize({width: 375, height: 812});
      await page.goto(base);
      await verified(page);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      await page.click('#primeMode');
      await page.click('#standardMode');
      if (process.env.SCREENSHOT_DIR) {
        await page.screenshot({path: path.join(process.env.SCREENSHOT_DIR, 'storefront-mobile.png'), fullPage: true});
      }
    });
    console.log(passed + ' browser scenarios passed.');
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
    fs.rmSync(downloadDirectory, {recursive: true, force: true});
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
