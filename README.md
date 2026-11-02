# Vanguard Garrison | CSC322 Toy Soldier Market

Vanguard Garrison is a seller team in the CSC322 computational game theory market. Our project combines a public toy soldier storefront, a downloadable price file for the buyer team, and a Python pricing tool that responds to buyer results and competing sellers.

- [Storefront preview](https://toy-soldier-9bf43--optimized-code-ri6rr5p1.web.app/)
- [Download the preview price file](https://toy-soldier-9bf43--optimized-code-ri6rr5p1.web.app/published_price_file.json)
- [Download the preview price history](https://toy-soldier-9bf43--optimized-code-ri6rr5p1.web.app/decision_audit_log.json)

The links above are for a Firebase Hosting **preview**, which can expire. The current live site is a separate deployment. Confirm the active link with the team before sharing it as the official storefront.

## Project status

The `optimized-code` branch publishes a JSON price file for buyers and reads our inventory from Firebase when preparing the live catalog or running the buyer-results watcher. Competitor prices are collected from the other sellers; the previous buyer ping client and listener are not required.

After deploying this version, buyers can also read the same price file at `/api/catalog`. It is a public, read-only Hosting rewrite, not a purchase endpoint. The existing `/published_price_file.json` URL remains available. Recording purchases still requires the buyer team's official ledger; do not point buyers to a nonexistent `POST /api/purchases` on our site.

The three competitor feeds are configured. A scan-only process can check them every 30 minutes without changing prices. `--prices-only` can reprice using the last Firebase counts and marks simulated stock as not synchronized; it never deducts buyer sales. The full pricing watcher imports new sales from an official buyer ledger. Firebase Hosting serves static files; it does not start either Python process.

The older live storefront reads Firestore directly. The optimized storefront reads its published static JSON catalog. Deploying this branch replaces the live Hosting `public/` folder. `python3 -m automation.prepare_live` first reads the current public Firestore state into that catalog without changing the database.

The existing Firebase virtual inventory is the starting stock. The watcher reads the current Firebase counts rather than resetting them from `market_config.json`. Before importing buyer results, reconcile which transactions have already been included and confirm the round, remaining rounds, and simulated Prime eligibility with the instructor and buyer team.

## How the market works

1. **We publish offers.** The storefront and downloadable JSON show product identifiers, prices, eligibility, timestamps, and separate physical and virtual availability.
2. **The buyer team takes a snapshot.** At the instructor's daily cutoff, buyers save the published offers and use that snapshot for the simulated round. Later price changes apply to a future simulated round and do not change accepted purchases.
3. **We reconcile results.** The buyer team's ledger is authoritative for remaining virtual stock. Physical sales and cash are tracked separately.
4. **We check the market.** Our local automation reads the buyer's report, checks configured competitor sources, and uses those inputs to generate a catalog and decision record.
5. **We publish updates.** When explicitly configured to deploy, the process sends a successful catalog update to a Firebase Hosting preview or live site.

A price check does not necessarily change a price. The dynamic strategy is a heuristic using inventory, costs, buyer preference estimates, and available competitor offers. Sales-velocity values remain in the configuration but are not currently used to set prices or automatically derived from the buyer ledger. It is not a claim that we have found a market equilibrium.

## Storefront and buyer handoff

The buyer team can download [our live offers](https://toy-soldier-9bf43.web.app/published_price_file.json). The preview channel is separate and may expire.

Each offer has a stable `offer_id`, `product_quantities`, `total_price`, `eligibility`, `loyalty_program_terms`, `timestamp`, `virtual_available`, and `physical_available`. The buyer team should save the full file at the announced cutoff and use the agreed common format and market rules.

- Standard and Prime Silver offers share the same `SKU_SILVER` stock. Standard and Prime Gold offers share the same `SKU_GOLD` stock.
- Prime prices already include the published discount. Simulated Prime eligibility requires buyer-team support and instructor approval.
- `OFFER_CHARITY_MYSTERY_POUCH` uses `SKU_MYSTERY_POUCH` and is physical only, with zero virtual availability.
- In-person pickup is currently unavailable. The pickup form and selection controls are hidden; no alternative physical checkout is implied. Physical inventory remains recorded separately.

Prices are in whole cents. The default minimum is the assigned $0.25 unit cost and the existing maximum is $5. For the dynamic strategy, when a comparable competitor's reported virtual stock meets the configured availability rules, the engine targets one cent below that competitor, subject to the price and product-cost floors. If our heuristic already recommends less, it does not raise the price to match. Unknown or sold-out competitor stock does not qualify. Prime discounts remain seller-funded and are clamped to the same cost floors. Prices can reach cost, leaving zero unit margin.

## Project files

| Path | Purpose |
| --- | --- |
| [`public/index.html`](public/index.html), [`public/app.js`](public/app.js) | Storefront, catalog display, and pickup request. |
| [`public/published_price_file.json`](public/published_price_file.json) | Downloadable offers used by the storefront and buyer team. |
| [`public/decision_audit_log.json`](public/decision_audit_log.json) | Public price and eligibility history prepared for publication. |
| [`market_config.json`](market_config.json) | Round, inventory, strategy, preference estimates, and manual overrides. |
| [`config/automation.json`](config/automation.json) | The three seller feeds, JSON input, and 1,800-second interval. |
| [`automation/sources.py`](automation/sources.py) | Reads buyer reports and extracts configured competitor offers. |
| [`automation/seller_adapters.py`](automation/seller_adapters.py) | Validates and translates the three sellers' different feed formats. |
| [`automation/firebase_inventory.py`](automation/firebase_inventory.py) | Validates buyer sales and updates Firebase stock atomically and idempotently. |
| [`automation/prepare_live.py`](automation/prepare_live.py) | Copies the existing publicly readable Firebase inventory into the local Hosting catalog. |
| [`automation/run.py`](automation/run.py) | Runs the pricing cycle and optionally deploys through the Firebase CLI. |
| [`pricing_engine.py`](pricing_engine.py) | Pricing recommendations, offer generation, decision records, and replay inputs. |
| [`examples/`](examples/) | Illustrative source settings and a proposed buyer report. Example URLs, transactions, and quantities are not official. |
| `data/` | Local buyer files, automation state, and detailed decision audit; created as needed and ignored by Git. |
| [`firebase.json`](firebase.json), [`.firebaserc`](.firebaserc) | Firebase Hosting configuration and project selection. |
| [`tests/`](tests/) | Offline pricing, automation, and optional browser checks. |

## Requirements and local preview

The Python tools require Python 3.10 or newer. Competitor scans and local catalog preparation use the standard library. The Firebase buyer-results watcher also needs `firebase-admin` from `requirements.txt` and a service account or Google application-default credentials with Firestore access. Hosting deployments use Node.js and `npx -y firebase-tools@latest`; its login is separate from the Python credentials.

From the project root, run:

```bash
python3 -m http.server 8000 --directory public
```

Open [http://localhost:8000](http://localhost:8000). Press `Ctrl+C` to stop the local server. This shows the catalog currently in `public/`; it does not fetch buyer results, run the pricing process, or deploy to Firebase.

## Buyer JSON and Firebase inventory

The Brigade Exchange page has an **Export DB JSON** action. Its browser ledger objects contain `_id`, `sellerName`, `offerId`, `unitsBought`, `isoTimestamp`, and `status`, which this watcher can read. A site's own browser state is not automatically the shared official ledger: the current page creates sample trades on load, and the full JSON export also includes private buyer profiles. Ask the buyer team to provide an authoritative, seller-filtered JSON endpoint or file and confirm IDs remain stable across exports. The `localhost:5000` bridge in their page addresses the browser visitor's own machine; it is not a public endpoint for our server.

The default input is `data/buyer_results.json`. When the buyer sends an official seller-filtered file, replace that file with the new version. If they publish a direct endpoint, change the `json` source to `{"type":"url","url":"https://..."}`. Do not use their Google Sites page URL as the JSON endpoint. When the watcher is running it checks the selected file or URL for changes every 60 seconds.

To use an approved seller-specific JSON endpoint instead of the default local file, update `buyer_results` in `config/automation.json` (or a separate `--sources` settings file):

```json
{
  "type": "json_firestore",
  "json": {
    "type": "url",
    "url": "https://buyer-team.example/api/sellers/vanguard/transactions",
    "bearer_token_env": "BRIGADE_API_TOKEN"
  }
}
```

The URL above is a placeholder, not an existing buyer endpoint. Omit `bearer_token_env` for a buyer-approved public feed. For an authenticated feed, put the token in the named environment variable, not in Git or the URL; authenticated requests require HTTPS and refuse cross-origin redirects. The importer accepts a seller-specific `transactions` array, a `ledger` array, or the buyer page's `collections.ledgers` shape, retaining only the necessary sale fields. Do not request the buyer team's private customer profiles.

The importer processes `PURCHASED` entries for our seller only. It stores processed event hashes in Firestore and atomically subtracts each new sale once; re-reading the same event will not subtract it again. Standard and Prime offers share inventory. It does not deduct simulated purchases from physical stock, allow negative inventory, or publish the buyer report and profiles. Seller names and offer IDs must match our configured aliases and published catalog.

The buyer's current CSV export omits purchased quantity and a reliably unique event ID. If the team later supplies a compatible CSV, use `csv_firestore`; [`examples/buyer_seller_csv_export.js`](examples/buyer_seller_csv_export.js) illustrates the additional fields. Do not infer units from the total or use the unmodified CSV to reduce stock.

Before processing the first CSV or JSON report, reconcile Firebase's current stock with the buyer ledger and initialize the baseline **once**:

- If there are no existing sales in the first report and future exports contain only new sales, run `python3 -m automation.run --initialize-buyer-baseline empty`.
- If the current Firebase inventory already includes all sales in an existing, compatible report, configure that report and run `python3 -m automation.run --initialize-buyer-baseline existing`. Those rows are marked as already counted, without changing stock.

Never select either option until the team knows which transactions Firebase already includes. Existing Firestore counts are preserved at initialization. The full watcher stops if buyer results are missing; it cannot detect sales that have not been delivered. Keep the original buyer reports privately for reconciliation.

The buyer page describes 15 overlapping two-hour windows from 5:00 AM to 5:00 AM using the viewer's local clock. However, its current page-load code generates all 15 sample trades immediately and its default seller URLs are placeholders. Those windows are not a verified publication schedule or instructor cutoff. The watcher checks sellers at least every 30 minutes and looks for changed buyer results every 60 seconds; changed results trigger another seller scan and pricing cycle. Ask the buyer team when it will release an authoritative ledger and have the instructor confirm the official cutoff.

## Configure competitor sources

The default file already lists the supplied competitors. The mapping below compares similar product tiers, not necessarily identical products or approved simulated customer preferences. Confirm these mappings with the buyer team's model before relying on the recommendations.

| Seller | Feed | Mapped comparison | Availability handling |
| --- | --- | --- | --- |
| Pocket Soldier | [`prices.json`](https://neon-palmier-f0739a.netlify.app/prices.json) | `PP-SINGLE-001` → Green | A null simulated count is unknown and is not used to set a competitor minimum. Bundles and code-only promotions are excluded. |
| Frieds Fort Troopers | [`api/catalog`](https://troopers.fjb.chatgpt.site/api/catalog) and [`prices`](https://troopers.fjb.chatgpt.site/prices) | `basic-1` → Green; `personalized-1` (Graphic) → Silver; `limited-1` (Gold) → Gold | The price page currently claims unlimited supply. The script checks that visible claim on each run and labels it `seller_claimed_unlimited`, rather than inventing a stock count. The engine may use those prices; the buyer ledger remains authoritative. If the claim disappears, availability is unknown and those prices are excluded. |
| Toy Soldier Stand | [`prices.json`](https://toy-soliders.jarbencer.com/prices.json) | `GREEN-1` → Green | The feed's simulated product count is used; zero or three or fewer units are excluded by the stockout filter. Physical stock is not substituted. |

Mappings are in `config/automation.json`. The Troopers catalog does not itself provide an effective timestamp or numeric stock. Its current snapshot ID and the availability-page hash are retained in the private audit. All configured feeds must be readable and valid for a pricing cycle; an unexpected change stops the cycle instead of guessing.

To keep a separate editable local configuration without overwriting an existing one:

```bash
mkdir -p data
test -e data/sources.json || cp config/automation.json data/sources.json
```

Open `data/sources.json` in VS Code to adjust mappings or the buyer report's local path or direct URL. [`examples/automation.example.json`](examples/automation.example.json) also demonstrates the generic source formats; its URLs and IDs are placeholders.

An older `data/sources.json` may still point to the previous JSON buyer report or have an empty seller list. Update that file explicitly or use the default `config/automation.json`; the copy command above deliberately does not overwrite it.

- `interval_seconds` defaults to `1800`, or 30 minutes; `buyer_poll_seconds` defaults to `60`. `request_timeout_seconds` limits each request.
- For `price_file`, map our SKU to the competitor's corresponding unrestricted, single-item `offer_id`. The parser expects an offer array or an object with an `offers` array, a price, virtual stock, eligibility, and a timezone-aware effective timestamp. It rejects future offers and expired offers when a supported expiration timestamp is provided.
- For `html`, map a SKU to the IDs of elements containing its price and **virtual** stock. Optional attributes can be selected with `price_attribute` or `virtual_stock_attribute`.
- The three named feeds use their site-specific adapters; do not change their `format` without updating the adapter. HTML generated only by JavaScript is not available to the generic HTML parser.

The dynamic strategy considers a competitor's price when reported virtual stock exceeds three units, or when the Troopers page explicitly claims unlimited availability. Unknown and sold-out offers remain in the private observations but cannot trigger an automatic response. The observed claim is not a confirmation from the buyer ledger.

## Check competitors without changing prices

Run one check, or keep checking approximately every 30 minutes:

```bash
python3 -m automation.run --scan-only
python3 -m automation.run --scan-only --watch
```

These commands need no buyer file and do not generate prices or deploy. They print the mapped prices and stock status and save the latest observation to the Git-ignored `data/competitor_snapshot.json`. Press `Ctrl+C` to stop the watcher. The machine must stay awake, online, and running the process; this is not an unattended cloud service. A failed check leaves the previous snapshot in place and reports an error.

## Run pricing and deploy

To prepare the optimized site from the inventory and prices currently stored in the publicly readable Firebase state, then deploy the prepared folder to the **live** Firebase Hosting site:

```bash
python3 -m automation.prepare_live
npx -y firebase-tools@latest login
npx -y firebase-tools@latest deploy --only hosting --project toy-soldier-9bf43
```

Stop if preparation reports an error. The first command does not change Firestore or deploy. It preserves the Firebase state's last-updated timestamp and prepares the local catalog and public price history. The final command replaces the live Hosting files with this branch's `public/` folder. Check the live storefront and downloadable `published_price_file.json` afterwards. This one-time deploy does not start the pricing watcher.

To run the Python watcher, install its dependency:

```bash
python3 -m pip install -r requirements.txt
```

For the Python watcher, set `GOOGLE_APPLICATION_CREDENTIALS` to your service-account file, place `serviceAccountKey.json` in the project root, or configure application-default credentials. Keep keys out of Git and never place them in `public/`. The service account needs access to this Firebase project. Running Firebase CLI login does not by itself provide these Python credentials.

Without an official buyer feed, you may start a **prices-only** watcher:

```bash
python3 -m automation.run --prices-only --watch --deploy-live
```

It scans competitors and can publish changed prices every 30 minutes, using the last recorded Firebase inventory. It never reads buyer sales or automatically deducts stock. The site and price file mark simulated stock as not synchronized with the buyer team. Those counts can be stale, so reconcile them manually before relying on them for sales; a new price timestamp does not verify the inventory. This mode does not initialize the buyer baseline.

For full pricing and inventory updates, first obtain an official buyer report, reconcile Firebase stock, and initialize the buyer baseline. Prepare one update locally with `python3 -m automation.run`. To run immediately and then approximately every 30 minutes, deploying successful updates to the **live** Hosting site:

```bash
python3 -m automation.run --watch --deploy-live
```

Or deploy successful updates to a preview channel:

```bash
python3 -m automation.run --watch --deploy-preview optimized-code
```

Keep the terminal process and computer running, awake, and online. Press `Ctrl+C` to stop it. The watcher checks for changed buyer results every minute and scans seller feeds at least every 30 minutes; the buyer page itself cannot push its exports to this computer. Each successful deployment publishes the entire `public/` folder. Run only one pricing process per checkout; a project lock prevents simultaneous writers. The static price file is the public price authority; legacy Firestore price fields are not automatically repriced by this watcher.

Missing or invalid buyer results stop a full watcher cycle. An unavailable seller feed also skips the cycle. A failed deployment leaves a pending update in `data/automation_state.json`; retry with the **same deployment target**. If Firebase stock changed before a pending update is retried, the stale pending catalog is discarded and the next cycle rebuilds it. Firestore stock can change before a Hosting deployment finishes, so a failed deployment can leave the static storefront showing the previous stock until a successful retry. Monitor the logs and reconcile against the buyer ledger.

## Records and comparisons

The local detailed audit at `data/decision_audit_log.json` records the inputs available for each decision, including buyer stock, competitor observations, recommendations, submitted prices, and override reasons. Record a manual override's reason before seeing that round's results. The public history records offer price or eligibility changes; its timestamp is when the catalog was prepared, not an independent server-confirmed deployment time.

The historical root `decision_audit_log.json` and `pricing_engine.py` are tracked in this repository. If the repository is public, their contents are public on GitHub even though they are outside the Firebase `public/` folder. `data/` is ignored by Git and is not automatically backed up.

Generate candidate price files for comparing `dynamic_response`, `fixed_price`, and `fixed_markup`:

```bash
python3 pricing_engine.py --replays
```

Run this after stopping the watcher. The files under `replays/` are inputs, not completed simulations. The buyer team must replay them from the same opening state, customer draws, and competitor offers; a replay must not change the official ledger.

Maintain the required simulated transaction records, physical sales, expenses, and inventory records separately. If physical selling becomes possible, announce locations through the class market channel and follow the instructor's reporting rules.

## Checks

```bash
python3 -m unittest discover -s tests -v
npm run check
```

Optional browser checks:

```bash
npm install --no-save --package-lock=false playwright
npx playwright install chromium
node tests/storefront_checks.cjs
```

These checks do not confirm real buyer data, competitor mappings, stock counts, eligibility approval, or Firebase account access.

## Firebase data and credentials

The current storefront does not read the old Firestore trade feed. Existing Firestore rules and data are not changed by deploying Hosting. Once the old live site and any other consumers no longer need the feed, review the public create permissions for `market_feed` and `market_summaries`. Keep service-account keys and other credentials out of Git and `public/`.
