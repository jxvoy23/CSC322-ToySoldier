# Vanguard Garrison | CSC322 Toy Soldier Market

Vanguard Garrison is a seller team in the CSC322 computational game theory market. Our project combines a public toy soldier storefront, a downloadable price file for the buyer team, and a Python pricing tool that responds to buyer results and competing sellers.

- [Live storefront](https://toy-soldier-9bf43.web.app/)
- [Published price file](https://toy-soldier-9bf43.web.app/published_price_file.json)
- [Buyer catalog endpoint](https://toy-soldier-9bf43.web.app/api/catalog)
- [Published price history](https://toy-soldier-9bf43.web.app/decision_audit_log.json)

## Project status

The storefront on `main` reads a published static JSON catalog. `/api/catalog` serves the same catalog through a public, read-only Firebase Hosting rewrite. It does not accept purchases: the buyer team must record purchases in its own ledger.

Three competitor feeds are configured. The Python tools can scan them, recommend prices, import compatible buyer results, and optionally deploy a new catalog. Firebase Hosting only serves the files; it does not start or schedule the Python process. The older buyer ping client and listener are not required.

**Inventory status:** Prices-only mode uses the last recorded Firebase stock without deducting buyer sales. Offers from that mode are marked `simulated_stock_status: "not_synchronized"`. Check the live catalog's status and reconcile it against an authoritative buyer ledger before relying on its simulated counts. A recent price timestamp does not verify the stock.

The Firebase-backed watcher uses current Firestore inventory rather than resetting it from `market_config.json`. Before importing buyer results, reconcile which transactions have already been included and confirm the round, remaining rounds, and simulated Prime eligibility with the instructor and buyer team.

## Choose an automation mode

Run commands from the project root. Setup and full examples are below.

| Mode | What it does | Buyer results needed? | Firebase effect |
| --- | --- | --- | --- |
| Local preview | Serves the files currently in `public/` on your computer. | No | None. |
| `--scan-only` | Checks competitor feeds without repricing or deploying. | No | None. |
| `--prices-only` | Generates prices using the last recorded Firebase stock; marks simulated stock as unsynchronized. | No | Reads Firestore; does not deduct sales. |
| Full watcher | Imports new buyer transactions, updates inventory, and generates a catalog. | Yes; reconcile and initialize the baseline first. | Writes buyer transactions and stock to Firestore. |

Add `--watch` to keep a process running. The default competitor interval is 30 minutes; the full watcher also checks for changed buyer results every 60 seconds. Catalog files remain local unless a deployment option is supplied. **A full watcher can update Firestore even when the Hosting target is local or a preview.**

## How the market works

1. **We publish offers.** The storefront and downloadable JSON show product identifiers, prices, eligibility, timestamps, and separate physical and virtual availability.
2. **The buyer team takes a snapshot.** At the cutoff agreed with the instructor, buyers save the published offers and use that snapshot for the simulated round. Later price changes do not change accepted purchases.
3. **We reconcile results.** Confirm the authoritative virtual-sales ledger with the buyer team. Physical sales and cash are tracked separately.
4. **We check the market.** The automation checks configured competitors and, when configured for a full cycle, reads the buyer's report to generate a catalog and decision record.
5. **We publish updates.** When explicitly configured to deploy, the process sends a successful catalog update to a Firebase Hosting preview or live site.

A price check does not necessarily change a price. The dynamic strategy is a heuristic using inventory, costs, buyer preference estimates, and available competitor offers. Sales-velocity values remain in the configuration but are not currently used to set prices or automatically derived from the buyer ledger. It is not a claim that we have found a market equilibrium.

## Storefront and buyer handoff

The buyer team can download [our live offers](https://toy-soldier-9bf43.web.app/published_price_file.json). The preview channel is separate and may expire.

Each offer has a stable `offer_id`, `product_quantities`, `total_price`, `eligibility`, `loyalty_program_terms`, `timestamp`, `virtual_available`, and `physical_available`. `simulated_stock_status` indicates whether the counts were generated from a buyer report or are not synchronized; it cannot confirm that every sale has been delivered to the watcher. The buyer team should save the full file at the announced cutoff and use the agreed common format and market rules.

- Standard and Prime Silver offers share the same `SKU_SILVER` stock. Standard and Prime Gold offers share the same `SKU_GOLD` stock.
- Prime prices already include the published discount. Simulated Prime eligibility requires buyer-team support and instructor approval.
- `OFFER_CHARITY_MYSTERY_POUCH` uses `SKU_MYSTERY_POUCH` and is physical only, with zero virtual availability.
- In-person pickup is currently unavailable. The pickup form and selection controls are hidden; no alternative physical checkout is implied. Physical inventory remains recorded separately.

Prices are in whole cents. The default minimum is the assigned $0.25 unit cost and the existing maximum is $5. For the dynamic strategy, when a comparable competitor's reported virtual stock meets the configured availability rules, the engine targets one cent below that competitor, subject to the price and product-cost floors. If our heuristic already recommends less, it does not raise the price to match. Unknown or sold-out competitor stock does not qualify. Prime discounts remain seller-funded and are clamped to the same cost floors. Prices can reach cost, leaving zero unit margin.

## Project files

| Path | Purpose |
| --- | --- |
| [`public/index.html`](public/index.html), [`public/app.js`](public/app.js) | Storefront and catalog display; pickup controls are currently hidden. |
| [`public/published_price_file.json`](public/published_price_file.json) | Downloadable offers used by the storefront and buyer team. |
| [`public/decision_audit_log.json`](public/decision_audit_log.json) | Public price and eligibility history prepared for publication. |
| [`market_config.json`](market_config.json) | Round, inventory, strategy, preference estimates, and manual overrides. |
| [`config/automation.json`](config/automation.json) | The three seller feeds, JSON input, and 1,800-second interval. |
| [`automation/sources.py`](automation/sources.py) | Reads buyer reports and extracts configured competitor offers. |
| [`automation/seller_adapters.py`](automation/seller_adapters.py) | Validates and translates the three sellers' different feed formats. |
| [`automation/firebase_inventory.py`](automation/firebase_inventory.py) | Validates buyer sales and updates Firebase stock atomically and idempotently. |
| [`automation/prepare_live.py`](automation/prepare_live.py) | Rebuilds the local catalog from publicly readable Firestore prices and inventory; see the cautions below. |
| [`automation/run.py`](automation/run.py) | Runs the pricing cycle and optionally deploys through the Firebase CLI. |
| [`pricing_engine.py`](pricing_engine.py) | Pricing recommendations, offer generation, decision records, and replay inputs. |
| [`examples/`](examples/) | Illustrative source settings and a proposed buyer report. Example URLs, transactions, and quantities are not official. |
| `data/` | Local buyer files, automation state, and detailed decision audit; created as needed and ignored by Git. |
| [`firebase.json`](firebase.json), [`.firebaserc`](.firebaserc) | Firebase Hosting configuration and project selection. |
| [`tests/`](tests/) | Offline pricing, automation, and optional browser checks. |

## Requirements and local preview

The Python tools require Python 3.10 or newer. Competitor scans and local catalog preparation use the standard library. **Both Firebase-backed pricing modes** need `firebase-admin` from `requirements.txt` and a service account or Google application-default credentials with Firestore access. Hosting deployments use Node.js and `npx -y firebase-tools@latest`; its login is separate from the Python credentials.

Clone the repository and enter the project folder:

```bash
git clone https://github.com/jxvoy23/CSC322-ToySoldier.git
cd CSC322-ToySoldier
```

The examples below use `python3` on macOS or Linux. On Windows, use `py -3` in its place; for example, `py -3 -m http.server 8000 --directory public`.

From the project root, run:

```bash
python3 -m http.server 8000 --directory public
```

Open [http://localhost:8000](http://localhost:8000). Press `Ctrl+C` to stop the local server. This shows the catalog currently in `public/`; it does not fetch buyer results, run the pricing process, or deploy to Firebase.

## Buyer JSON and Firebase inventory

The importer supports buyer ledger records with fields such as `_id`, `sellerName`, `offerId`, `unitsBought`, `isoTimestamp`, and `status`. A site's browser state or test export is not automatically the shared official ledger. A previously inspected Brigade Exchange export contained sample trades and buyer profiles; confirm the current export with the buyer team instead of assuming it is suitable. Request an authoritative, seller-filtered JSON endpoint or file and confirm transaction IDs remain stable across exports. A `localhost` address refers to the visitor's own machine, not a public endpoint for this watcher.

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

A previously inspected buyer CSV export omitted purchased quantity and a reliably unique event ID. If the team supplies a compatible CSV, use `csv_firestore`; [`examples/buyer_seller_csv_export.js`](examples/buyer_seller_csv_export.js) illustrates the required fields. Do not infer units from the total or import an export that lacks them.

Before processing the first CSV or JSON report, reconcile Firebase's current stock with the buyer ledger and initialize the baseline **once**:

- If there are no existing sales in the first report and future exports contain only new sales, run `python3 -m automation.run --initialize-buyer-baseline empty`.
- If the current Firebase inventory already includes all sales in an existing, compatible report, configure that report and run `python3 -m automation.run --initialize-buyer-baseline existing`. Those rows are marked as already counted, without changing stock.

Never select either option until the team knows which transactions Firebase already includes. Initialization writes a baseline and event markers to Firestore but preserves its current inventory counts. The full watcher stops if buyer results are missing; it cannot detect sales that have not been delivered. Keep the original buyer reports privately for reconciliation.

The buyer team must confirm when it will release authoritative results, and the instructor must confirm the official cutoff. The watcher checks sellers at least every 30 minutes and looks for changed buyer results every 60 seconds; changed results trigger another seller scan and pricing cycle.

## Configure competitor sources

The default file already lists the supplied competitors. The mapping below compares similar product tiers, not necessarily identical products or approved simulated customer preferences. Confirm these mappings with the buyer team's model before relying on the recommendations.

| Seller | Feed | Mapped comparison | Availability handling |
| --- | --- | --- | --- |
| Pocket Soldier | [`prices.json`](https://neon-palmier-f0739a.netlify.app/prices.json) | `PP-SINGLE-001` → Green | A null simulated count is unknown and is not used to set a competitor minimum. Bundles and code-only promotions are excluded. |
| Frieds Fort Troopers | [`api/catalog`](https://troopers.fjb.chatgpt.site/api/catalog) and [`prices`](https://troopers.fjb.chatgpt.site/prices) | `basic-1` → Green; `personalized-1` (Graphic) → Silver; `limited-1` (Gold) → Gold | The price page currently claims unlimited supply. The script checks that visible claim on each run and labels it `seller_claimed_unlimited`, rather than inventing a stock count. The engine may use those prices; the buyer ledger remains authoritative. If the claim disappears, availability is unknown and those prices are excluded. |
| Toy Soldier Stand | [`prices.json`](https://toy-soliders.jarbencer.com/prices.json) | `GREEN-1` → Green | The feed's simulated product count is used; zero or three or fewer units are excluded by the stockout filter. Physical stock is not substituted. |

Mappings are in `config/automation.json`. The Troopers catalog does not itself provide an effective timestamp or numeric stock. Its current snapshot ID and the availability-page hash are retained in the private audit. All configured feeds must be readable and valid for a pricing cycle; an unexpected change stops the cycle instead of guessing.

To keep a separate editable local configuration without overwriting an existing one, use the following on macOS or Linux:

```bash
mkdir -p data
test -e data/sources.json || cp config/automation.json data/sources.json
```

In Windows PowerShell:

```powershell
New-Item -ItemType Directory -Force data | Out-Null
if (-not (Test-Path data/sources.json)) { Copy-Item config/automation.json data/sources.json }
```

Open `data/sources.json` in VS Code to adjust mappings or the buyer report's local path or direct URL. **The runner uses `config/automation.json` by default**; add `--sources data/sources.json` to each command that should use the copy, including baseline initialization. For example:

```bash
python3 -m automation.run --sources data/sources.json --scan-only
```

[`examples/automation.example.json`](examples/automation.example.json) also demonstrates the generic source formats; its URLs and IDs are placeholders.

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

To run the Python watcher, install its dependency:

```bash
python3 -m pip install -r requirements.txt
```

For the Python watcher, set `GOOGLE_APPLICATION_CREDENTIALS` to your service-account file, place `serviceAccountKey.json` in the project root, or configure application-default credentials. Keep keys out of Git and never place them in `public/`. The service account needs access to this Firebase project. Running Firebase CLI login does not by itself provide these Python credentials.

For example, set the path for the current terminal session on macOS or Linux:

```bash
export GOOGLE_APPLICATION_CREDENTIALS="/path/to/serviceAccountKey.json"
```

In Windows PowerShell:

```powershell
$env:GOOGLE_APPLICATION_CREDENTIALS = "C:\path\to\serviceAccountKey.json"
```

Replace the example path with your own private key file. These environment variables apply to the current terminal session.

If deploying with a watcher, authenticate the Firebase CLI separately:

```bash
npx -y firebase-tools@latest login
```

Without an official buyer feed, you may start a **prices-only** watcher:

```bash
python3 -m automation.run --prices-only --watch --deploy-live
```

It scans competitors and can publish changed prices every 30 minutes, using the last recorded Firebase inventory. It never reads buyer sales or automatically deducts stock. The site and price file mark simulated stock as not synchronized with the buyer team. Those counts can be stale, so reconcile them manually before relying on them for sales; a new price timestamp does not verify the inventory. This mode does not initialize the buyer baseline. Remove `--deploy-live` to prepare updates locally without publishing them.

For full pricing and inventory updates, first obtain an official buyer report, reconcile Firebase stock, and initialize the buyer baseline. Prepare one update locally with `python3 -m automation.run`. To run immediately and then approximately every 30 minutes, deploying successful updates to the **live** Hosting site:

```bash
python3 -m automation.run --watch --deploy-live
```

Or deploy successful updates to a preview channel:

```bash
python3 -m automation.run --watch --deploy-preview optimized-code
```

The preview channel name is just a Hosting channel, not a Git branch. **The full watcher still imports sales into the configured Firestore database when using a preview or no deployment flag.** For an isolated test, use a separate Firebase project and credentials, set its project ID in `market_config.json`, and provide a matching test inventory and buyer report.

Keep the terminal process and computer running, awake, and online. Press `Ctrl+C` to stop it. The watcher checks for changed buyer results every minute and scans seller feeds at least every 30 minutes; the buyer page itself cannot push its exports to this computer. Each successful deployment publishes the entire `public/` folder. Run only one pricing process per checkout; a project lock prevents simultaneous writers. The static price file is the public price authority; legacy Firestore price fields are not automatically repriced by this watcher.

Missing or invalid buyer results stop a full watcher cycle. An unavailable seller feed also skips the cycle. A failed deployment leaves a pending update in `data/automation_state.json`; retry with the **same deployment target**. If Firebase stock changed before a pending update is retried, the stale pending catalog is discarded and the next cycle rebuilds it. Firestore stock can change before a Hosting deployment finishes, so a failed deployment can leave the static storefront showing the previous stock until a successful retry. Monitor the logs and reconcile against the buyer ledger.

### Prepare or deploy a catalog manually

`automation.prepare_live` is intended for preparing a local catalog from the prices and inventory in the publicly readable Firestore state. It writes local catalog and audit files, labels simulated stock unsynchronized, and does not itself change Firestore or deploy:

```bash
python3 -m automation.prepare_live
```

**Do not run this as a routine step before every deployment.** The pricing watcher updates the static catalog, but does not update the old Firestore price fields. Preparing from Firestore can therefore replace newer local prices with older ones. Check and reconcile both prices and inventory before using it.

To deploy a catalog you have already reviewed in `public/`, use:

```bash
npx -y firebase-tools@latest deploy --only hosting --project toy-soldier-9bf43
```

This replaces the live Hosting files with the entire local `public/` folder, which may differ from the current live site. Confirm the storefront and published price file afterwards. A one-time deployment does not start the watcher.

## Records and comparisons

The local detailed audit at `data/decision_audit_log.json` records the inputs available for each decision, including buyer stock, competitor observations, recommendations, submitted prices, and override reasons. Record a manual override's reason before seeing that round's results. The public history records offer price or eligibility changes; its timestamp is when the catalog was prepared, not an independent server-confirmed deployment time.

The historical root `decision_audit_log.json` and `pricing_engine.py` are tracked in this public repository. Their contents are visible on GitHub even though they are outside the Firebase `public/` folder. `data/` is ignored by Git and is not automatically backed up.

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
