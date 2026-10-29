# Vanguard Garrison | CSC322 Toy Soldier Market

Vanguard Garrison is a seller team in the CSC322 computational game theory market. Our project combines a public toy soldier storefront, a downloadable price file for the buyer team, and a Python pricing tool that responds to buyer results and competing sellers.

- [Storefront preview](https://toy-soldier-9bf43--optimized-code-ri6rr5p1.web.app/)
- [Download the preview price file](https://toy-soldier-9bf43--optimized-code-ri6rr5p1.web.app/published_price_file.json)
- [Download the preview price history](https://toy-soldier-9bf43--optimized-code-ri6rr5p1.web.app/decision_audit_log.json)

The links above are for a Firebase Hosting **preview**, which can expire. The current live site is a separate deployment. Confirm the active link with the team before sharing it as the official storefront.

## Project status

The `optimized-code` branch contains the JSON-based workflow. The buyer team can read our published price file without installing our Python code. We will obtain buyer results separately and collect competitor prices ourselves; the previous Firestore trade ticker, buyer ping client, and listener have been removed from this version.

The 30-minute update process is implemented but needs an official buyer results source, real competitor URLs, and a running Python process before it can operate. A Firebase Hosting deployment serves the static site; it does not start the Python process.

The current catalog and `market_config.json` contain carried-forward values, not verified stock counts. Before official trading, reconcile virtual inventory with the buyer team's ledger, count physical inventory, confirm the round and remaining rounds, and obtain instructor and buyer-team approval for simulated Prime offers.

## How the market works

1. **We publish offers.** The storefront and downloadable JSON show product identifiers, prices, eligibility, timestamps, and separate physical and virtual availability.
2. **The buyer team takes a snapshot.** At the instructor's daily cutoff, buyers save the published offers and use that snapshot for the simulated round. Later price changes apply to a future simulated round and do not change accepted purchases.
3. **We reconcile results.** The buyer team's ledger is authoritative for remaining virtual stock. Physical sales and cash are tracked separately.
4. **We check the market.** Our local automation reads the buyer's report, checks configured competitor sources, and uses those inputs to generate a catalog and decision record.
5. **We publish updates.** When explicitly configured to deploy, the process sends a successful catalog update to a Firebase Hosting preview or live site.

A price check does not necessarily change a price. The dynamic strategy is a heuristic using inventory, costs, sales estimates, buyer preference estimates, and available competitor offers. It is not a claim that we have found a market equilibrium.

## Storefront and buyer handoff

The buyer team can download [our preview offers](https://toy-soldier-9bf43--optimized-code-ri6rr5p1.web.app/published_price_file.json) while integration is being tested. Use the official live URL only after it is deployed and approved.

Each offer has a stable `offer_id`, `product_quantities`, `total_price`, `eligibility`, `loyalty_program_terms`, `timestamp`, `virtual_available`, and `physical_available`. The buyer team should save the full file at the announced cutoff and use the agreed common format and market rules.

- Standard and Prime Silver offers share the same `SKU_SILVER` stock. Standard and Prime Gold offers share the same `SKU_GOLD` stock.
- Prime prices already include the published discount. Simulated Prime eligibility requires buyer-team support and instructor approval.
- `OFFER_CHARITY_MYSTERY_POUCH` uses `SKU_MYSTERY_POUCH` and is physical only, with zero virtual availability.
- A pickup request is downloaded to the customer's device; it is not sent to the seller and does not reserve stock. Confirm the price, pickup, and payment with the customer.

## Project files

| Path | Purpose |
| --- | --- |
| [`public/index.html`](public/index.html), [`public/app.js`](public/app.js) | Storefront, catalog display, and pickup request. |
| [`public/published_price_file.json`](public/published_price_file.json) | Downloadable offers used by the storefront and buyer team. |
| [`public/decision_audit_log.json`](public/decision_audit_log.json) | Public price and eligibility history prepared for publication. |
| [`market_config.json`](market_config.json) | Round, inventory, strategy, preference estimates, and manual overrides. |
| [`config/automation.json`](config/automation.json) | Default source settings and 1,800-second interval; competitor list is initially empty. |
| [`automation/sources.py`](automation/sources.py) | Reads buyer reports and extracts configured competitor offers. |
| [`automation/run.py`](automation/run.py) | Runs the pricing cycle and optionally deploys through the Firebase CLI. |
| [`pricing_engine.py`](pricing_engine.py) | Pricing recommendations, offer generation, decision records, and replay inputs. |
| [`examples/`](examples/) | Illustrative source settings and a proposed buyer report. Example URLs, transactions, and quantities are not official. |
| `data/` | Local buyer files, automation state, and detailed decision audit; created as needed and ignored by Git. |
| [`firebase.json`](firebase.json), [`.firebaserc`](.firebaserc) | Firebase Hosting configuration and project selection. |
| [`tests/`](tests/) | Offline pricing, automation, and optional browser checks. |

## Requirements and local preview

The Python tools require Python 3.10 or newer and use the standard library. No Python packages are needed. Node.js is only needed for the JavaScript check or optional browser tests. The Firebase CLI is needed for deployments.

From the project root, run:

```bash
python3 -m http.server 8000 --directory public
```

Open [http://localhost:8000](http://localhost:8000). Press `Ctrl+C` to stop the local server. This shows the catalog currently in `public/`; it does not fetch buyer results, run the pricing process, or deploy to Firebase.

## Buyer results

[`examples/buyer_results.example.json`](examples/buyer_results.example.json) is a proposed format for one seller, subject to agreement with the buyer team. If the team publishes another format, update the importer in `automation/sources.py` before running official automation.

| Field | Meaning |
| --- | --- |
| `report_id` | Unique identifier for the report version. |
| `seller_id` | `Vanguard_Garrison` or an alias in `market_config.json`. |
| `round` | Positive integer no greater than the configured round. |
| `reported_at` | Timezone-aware ISO timestamp, such as `2026-10-29T12:00:00Z`. |
| `remaining_virtual` | Nonnegative integer counts for all three soldier SKUs: `SKU_GREEN`, `SKU_SILVER`, and `SKU_GOLD`. |
| `transactions` | Optional array of purchases; may be empty for an opening stock report. |

The importer uses `remaining_virtual` directly; it does not subtract the listed transactions again or verify their totals against stock. It records the report ID, timestamp, remaining counts, hash, and transaction count in the local decision audit. **It does not archive the complete buyer report fetched from a URL.** Save the buyer's official transaction records separately for reconciliation and the assignment. Keep customer identities and payment details out of public files.

The default settings read `data/buyer_results.json`. Alternatively, configure a direct URL to the buyer's results JSON. Replace the local file when a new report is released, or use an endpoint that the buyer team updates. Older reports are rejected; a changed report needs a new ID and a later timestamp.

Stock increases require a recorded reason. After confirming an instructor-approved correction, stop the watcher and run a single update:

```bash
python3 -m automation.run --sources data/sources.json --allow-increase-reason "Instructor-approved inventory correction"
```

The command above prepares local files. Add the appropriate deployment option if the correction should also be published. Restart the watcher without `--allow-increase-reason` afterwards. If an older `.market_state.json` has a pending publication, review and resolve it before migrating.

## Configure competitor sources

Create a local configuration without overwriting an existing one:

```bash
mkdir -p data
test -e data/sources.json || cp config/automation.json data/sources.json
```

Open `data/sources.json` in VS Code. Add at least one actual competitor and configure the buyer report's local path or direct URL. Use [`examples/automation.example.json`](examples/automation.example.json) as a guide; all URLs and offer IDs there are placeholders.

- `interval_seconds` defaults to `1800`, or 30 minutes. `request_timeout_seconds` limits each request.
- For `price_file`, map our SKU to the competitor's corresponding unrestricted, single-item `offer_id`. The parser expects an offer array or an object with an `offers` array, a price, virtual stock, eligibility, and a timezone-aware effective timestamp. It rejects future offers and expired offers when a supported expiration timestamp is provided.
- For `html`, map a SKU to the IDs of elements containing its price and **virtual** stock. Optional attributes can be selected with `price_attribute` or `virtual_stock_attribute`.
- HTML generated only by JavaScript is not available to this parser. Use the seller's public JSON when possible, or write a site-specific adapter. Confirm product comparisons, eligibility, and expiration before relying on them.

Every configured source must be readable and valid for a new cycle to be prepared. The dynamic strategy considers a competitor's price only when that offer's reported virtual stock exceeds three units.

## Run pricing and deploy

After configuring actual sources and verifying the buyer report, prepare one update locally:

```bash
python3 -m automation.run --sources data/sources.json
```

This updates the local catalog, public history file, and detailed audit. It does not deploy. Review the local result before sharing it.

To run immediately and then approximately every 30 minutes, deploying successful updates to the `optimized-code` **preview** channel:

```bash
firebase login
python3 -m automation.run --sources data/sources.json --watch --deploy-preview optimized-code
```

Keep the terminal process and computer running, awake, and connected to the internet. Press `Ctrl+C` to stop it. Running with `--watch` and no deployment option updates only local files. A manual preview deployment of the current `public/` folder can be made with:

```bash
firebase hosting:channel:deploy optimized-code --project toy-soldier-9bf43
```

After preview testing and the required approvals, this command enables repeated deployments to the **live** Hosting site:

```bash
python3 -m automation.run --sources data/sources.json --watch --deploy-live
```

Each successful live cycle deploys the current `public/` folder. Run only one pricing process per checkout; a project lock prevents simultaneous writers.

If required buyer data or a competitor source is unavailable or invalid, that cycle is skipped and the previous deployed catalog remains available. A failed deployment leaves a pending update in `data/automation_state.json`. Retry using the **same deployment target** to publish the saved update; later cycles can then process newer data. Monitor terminal messages for errors.

## Records and comparisons

The local detailed audit at `data/decision_audit_log.json` records the inputs available for each decision, including buyer stock, competitor observations, recommendations, submitted prices, and override reasons. Record a manual override's reason before seeing that round's results. The public history records offer price or eligibility changes; its timestamp is when the catalog was prepared, not an independent server-confirmed deployment time.

The historical root `decision_audit_log.json` and `pricing_engine.py` are tracked in this repository. If the repository is public, their contents are public on GitHub even though they are outside the Firebase `public/` folder. `data/` is ignored by Git and is not automatically backed up.

Generate candidate price files for comparing `dynamic_response`, `fixed_price`, and `fixed_markup`:

```bash
python3 pricing_engine.py --replays
```

Run this after stopping the watcher. The files under `replays/` are inputs, not completed simulations. The buyer team must replay them from the same opening state, customer draws, and competitor offers; a replay must not change the official ledger.

Maintain the required simulated transaction records, physical sales, expenses, and inventory records separately. Announce physical selling locations through the class market channel and follow the instructor's reporting rules.

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
