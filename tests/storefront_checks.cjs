/* Optional browser regression checks.
 * Install Playwright and Chromium, then run: node tests/storefront_checks.cjs
 * All non-local requests are blocked.
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

    await scenario('cent prices render while in-person pickup is hidden', async page => {
      await page.goto(base);
      await verified(page);
      assert.equal(await page.locator('#reserve').isHidden(), true);
      assert.equal(await page.locator('button[data-select="BASE_SKU_SILVER"]').isHidden(), true);
      assert.equal(await page.locator('#price-BASE_SKU_GREEN').textContent(), '$1.49');
      await page.click('#primeMode');
      assert.equal(await page.locator('#price-BASE_SKU_SILVER').textContent(), '$0.99');
      assert.equal(await page.locator('#priceHistoryBody tr').count() > 0, true);
      if (process.env.SCREENSHOT_DIR) {
        fs.mkdirSync(process.env.SCREENSHOT_DIR, {recursive: true});
        await page.screenshot({path: path.join(process.env.SCREENSHOT_DIR, 'storefront-desktop.png'), fullPage: true});
      }
    }, page => {
      const rows = clone(catalog);
      rows.find(row => row.offer_id === 'BASE_SKU_GREEN').total_price = 1.49;
      rows.find(row => row.offer_id === 'BASE_SKU_SILVER').total_price = 1.99;
      rows.find(row => row.offer_id === 'PRIME_SKU_SILVER').total_price = 0.99;
      return page.route('**/published_price_file.json', route => route.fulfill({
        contentType: 'application/json', body: JSON.stringify(rows)}));
    });

    await scenario('invalid catalog cannot enable a pickup request', async page => {
      await page.goto(base);
      await unavailable(page);
      assert.equal(await page.locator('button[data-select="BASE_SKU_GREEN"]').isDisabled(), true);
      assert.equal(await page.locator('#submitOrder').isDisabled(), true);
    }, page => page.route('**/published_price_file.json', route => route.fulfill({
      contentType: 'application/json', body: JSON.stringify([catalog[0], catalog[0]])})));

    await scenario('sub-cent catalog prices are rejected', async page => {
      await page.goto(base);
      await unavailable(page);
    }, page => {
      const rows = clone(catalog);
      rows.find(row => row.offer_id === 'BASE_SKU_GREEN').total_price = 1.234;
      return page.route('**/published_price_file.json', route => route.fulfill({
        contentType: 'application/json', body: JSON.stringify(rows)}));
    });

    await scenario('physical stock remains separate from virtual stock', async page => {
      await page.goto(base);
      await verified(page);
      assert.match(await page.locator('#phys-BASE_SKU_GREEN').textContent(), /0 units/);
      assert.match(await page.locator('#virt-BASE_SKU_GOLD').textContent(), /0 units/);
      assert.equal(await page.locator('#reserve').isHidden(), true);
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

    await scenario('catalog download is available without Firestore', async page => {
      const external = [];
      page.on('request', request => {
        if (new URL(request.url()).origin !== base) external.push(request.url());
      });
      await page.goto(base);
      await verified(page);
      assert.equal(await page.locator('a[href="published_price_file.json"]').count() > 0, true);
      assert.deepEqual(external, []);
    });

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
