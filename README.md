# CSC322 Toy Soldier Market

Vanguard Garrison · Operation Warm Sole

The storefront publishes a downloadable price file for the buyer team. Our own Python process reads the buyer team's reported results, checks configured competitor sources, and prepares a revised catalog at a 30-minute interval. Firebase Hosting serves the storefront and price file.

## Project layout

| Path | Purpose |
| --- | --- |
| `public/index.html`, `public/app.js` | Storefront and pickup request. The page reads our published JSON and has no Firestore trade ticker. |
| `public/published_price_file.json` | Downloadable catalog of offers, prices, eligibility and separate virtual and physical availability. |
| `public/decision_audit_log.json` | Public history of published price and offer changes. |
| `market_config.json` | Seller ID, round, physical inventory, starting virtual inventory, strategy and any manual price overrides. |
| `config/automation.json` | Default source settings. It contains no real competitor sources and therefore will not run until configured. |
| `automation/sources.py` | Reads buyer results and extracts comparable offers from competitor JSON or HTML. |
| `automation/run.py` | Runs the pricing cycle, tracks the last report, and optionally deploys to Firebase Hosting. |
| `pricing_engine.py` | Builds offers, pricing recommendations, decision records and replay inputs. |
| `examples/` | Illustrative buyer report and source settings. All names, URLs and numbers are examples, not official data. |
| `data/` | Local buyer data, automation state and detailed audit. Created when needed and ignored by Git. |
| `firebase.json`, `.firebaserc` | Firebase Hosting configuration and default project. |
| `tests/` | Offline integration and browser checks. |

The old buyer ping client, Firestore listener and storefront ticker have been removed. Firebase Hosting is still used to serve the site. The buyer does not need to install our Python code.

## Before using official results

1. Confirm the current round, rounds remaining, physical inventory and pouch count in `market_config.json`. Its carried-forward virtual counts are Green 28, Silver 16 and Gold 7; these are not verified against the buyer ledger. Confirm the opening quantities with the buyer team.
2. Confirm with the instructor and buyer team before enabling Prime in the simulated market. `enable_prime_discount_offers` remains at its previous setting; change it only after agreeing on the rule.
3. Ask the buyer team where its official results JSON will be published and what fields it will contain. `examples/buyer_results.example.json` is a **proposal**, not an agreed buyer format. If the buyer team chooses another format, adapt `automation/sources.py` to its actual output before enabling the automation.
4. Obtain the real competitor website or price-file URLs. Identify which unrestricted, single-item offer corresponds to each comparable soldier and which availability number is **virtual** stock.

At the daily cutoff, the buyer team saves the offer snapshot it will use for that simulated round. A later site update applies to the next simulated round; it does not change accepted purchases. Physical customers see the current published prices. Follow the instructor's announcement for the cutoff and results release time.

## Buyer results JSON

The current importer accepts a single seller report like the example in `examples/buyer_results.example.json`:

- `report_id`: a unique ID for this version of the report.
- `seller_id`: `Vanguard_Garrison` or one of its configured aliases.
- `round`: the completed or current round, as an integer.
- `reported_at`: an ISO timestamp with a timezone, for example `2026-10-29T12:00:00Z`.
- `remaining_virtual`: the buyer ledger's remaining quantities for `SKU_GREEN`, `SKU_SILVER` and `SKU_GOLD`.
- `transactions`: an array describing purchases. It can be empty or omitted when the buyer provides an opening inventory snapshot.

The transaction list is kept for reconciliation; the automation uses the reported **remaining** inventory directly and does not deduct the transactions again. It counts transactions for the audit but does not independently verify them against the remaining stock. Do not include customer identities in a public results file.

Save the official file locally as `data/buyer_results.json`, or set the buyer source to a stable URL that returns the JSON directly. Replace the file when a newer report is released. A replacement or correction needs a new `report_id` and a later `reported_at`. Older reports and silent stock increases are rejected.

If the instructor authorizes a stock increase or correction, stop the watcher and run one update with a reason. This records the reason in the detailed audit:

```bash
python3 -m automation.run --sources data/sources.json --allow-increase-reason "Instructor-approved inventory correction"
```

Add the same deployment option used for normal updates if the correction should be deployed. Restart the watcher without the correction option afterwards. Check the buyer ledger and any old `.market_state.json` before approving an increase. An unfinished old listener publication blocks migration until it is reviewed.

## Configure competitor sources

Create your local settings file:

```bash
mkdir -p data
cp config/automation.json data/sources.json
```

Open `data/sources.json` in VS Code. It starts with an empty `competitors` list. Use `examples/automation.example.json` as a guide and replace every example with a real source and the matching identifiers. Keep secret URLs and credentials out of Git; `data/` is ignored. The normal source settings are:

- `interval_seconds`: `1800` for 30 minutes.
- `request_timeout_seconds`: how long each fetch may take.
- `buyer_results`: either `{"type": "file", "path": "data/buyer_results.json"}` or `{"type": "url", "url": "https://..."}`.
- `competitors`: one entry per seller to check. Every configured source must succeed for the cycle to publish.

A `price_file` source downloads a seller's JSON, either an array of offers or an object containing an `offers` array. Set `offer_ids` to map each of **our** SKUs to the seller's corresponding offer ID. Mapped offers must contain a single product with quantity 1, `total_price`, integer `virtual_available`, `eligibility`, and a timezone-aware `timestamp`, `effective_at` or `effective_time`. If `expires_at` or `expiration_time` is supplied, it must also be an ISO timestamp and must not be expired. Future offers are rejected. Use `unrestricted_eligibility` to list the exact eligibility strings that mean the offer is available to everyone. Do not map a loyalty or bundle offer as a standard single-item offer.

An `html` source downloads the page's initial HTML and uses the configured element IDs for the price and virtual stock. For an attribute such as `<meta id="green-price" content="2.00">`, add `"price_attribute": "content"`. `virtual_stock_attribute` works the same way. The selected elements must contain an unambiguous dollar amount and a nonnegative stock count. HTML generated only by JavaScript cannot be read by this parser; use the site's public JSON if available, or add a site-specific adapter. Check expiry and eligibility on the seller's site when mapping HTML elements.

The current dynamic strategy considers comparable competitor prices only when their published virtual stock exceeds three units. It also uses our remaining inventory, costs, estimated buyer preferences and sales velocity. A check does not guarantee that a price will change. Do not treat estimated preferences as measured facts.

## Prepare and run an update

Python 3.10 or newer is required. The Python code uses the standard library; there are no pip dependencies. Run from the project root.

After configuring at least one real competitor and obtaining a buyer report, prepare a local update:

```bash
python3 -m automation.run --sources data/sources.json
```

This writes the local catalog, public history and `data/` audit. It does not deploy. Open the local storefront:

```bash
python3 -m http.server 8000 --directory public
```

Visit http://localhost:8000. The storefront refreshes its published file while open; that refresh is separate from the 30-minute pricing cycle.

To check and deploy automatically to the `optimized-code` Firebase **preview** channel, install and sign in to the Firebase CLI on the computer first, then run:

```bash
firebase login
python3 -m automation.run --sources data/sources.json --watch --deploy-preview optimized-code
```

The process runs immediately and then every 30 minutes. Keep the terminal process and computer running, awake and connected to the internet. Press `Ctrl+C` to stop it. Without `--deploy-preview` or `--deploy-live`, the watcher only updates local files.

After the preview is tested and the project is ready for the live site, the corresponding live command is:

```bash
python3 -m automation.run --sources data/sources.json --watch --deploy-live
```

This deploys to the live Hosting site on every successful cycle. Do not run a preview watcher and a live watcher from the same checkout at the same time. A project lock prevents concurrent pricing processes.

A missing or invalid buyer report, an inaccessible competitor, or an unrecognized offer stops that cycle before a new catalog is prepared. The previous deployed version stays available. A failed deployment keeps a pending update in `data/automation_state.json`; retry with the **same target** and the same prepared catalog will be published without duplicating its audit entry. Subsequent checks can then use newer source data.

## Pricing records and comparison

Detailed decision records are written to `data/decision_audit_log.json`. They include the buyer report identifier and stock, competitor prices and stock, source check times, recommendations, submitted prices and override reasons. The existing root `decision_audit_log.json` is historical; on the first successful new publication it is copied into the local detailed audit if the latter does not exist. It remains visible in GitHub if the repository is public. Ignoring `data/` does not remove information already committed to Git history. The pricing code itself is also visible in a public repository.

For replay inputs after stopping the watcher:

```bash
python3 pricing_engine.py --replays
```

The resulting files under `replays/` are candidate price files, not completed simulations. The buyer team must replay each strategy from the same starting state, customer draws and competitor offers. Do not change an official ledger during a replay.

## Verification and operations

```bash
python3 -m unittest discover -s tests -v
npm run check
```

Optional browser checks need Playwright and Chromium:

```bash
npm install --no-save --package-lock=false playwright
npx playwright install chromium
node tests/storefront_checks.cjs
```

The project does not process payments, reserve items, or maintain physical sales automatically. A pickup request downloads to the customer's device and must be shown to the seller. Record physical sales and expenses separately and keep physical availability current.

This change does not alter existing deployed Firestore rules or data. Once the old live site no longer needs the trade feed, review its public `market_feed` and `market_summaries` create permissions and any other Firestore users before closing access. Keep service-account keys private.
