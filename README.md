# CSC322 Toy Soldier Market

**Vanguard Garrison · Operation Warm Sole**

Seller storefront, pricing recommendations, and simulated-market notifications for the CSC322 Toy Soldier project.

[Storefront](https://toy-soldier-9bf43.web.app/) · [Published offers](https://toy-soldier-9bf43.web.app/published_price_file.json)

This revision was prepared from repository commit `0c63f015db457873c81ea6ad16ff15230c84c6b2`. It has not been pushed or deployed.

## Replace the project files

Copy every supplied file into the matching relative path in your checkout. Keep the `public/` and `tests/` directories. In particular, copy the new `public/app.js` along with the HTML.

The configuration preserves the repository's Green/Silver/Gold prices ($2/$3/$5), virtual counts (28/16/7), physical counts (24/14/6), and 12 physical pouches. These are carried-forward repository values, **not a verification of the buyer ledger or physical stock**. Round 4 and five remaining rounds are also carried forward. Check them before an official run. Invented competitor teams have been removed; enter actual published competitor offers.

The pouch is now explicitly `SKU_MYSTERY_POUCH`, with zero virtual availability. Tell the buyer team to skip physical-only offers before validating simulated product choices. All six offer IDs remain unchanged.

## Quick start

Use Python 3.10 or newer. The pricing engine and buyer client require only the standard library. Run commands from the project root:

```bash
python3 pricing_engine.py
python3 -m http.server 8000 --directory public
```

Open [http://localhost:8000](http://localhost:8000). The storefront reads the same JSON that buyers download. It refreshes every 30 seconds while visible. The optional Firestore ticker is disabled on localhost; add `?live` to test it deliberately.

Generating prices writes local files. It does not deploy Hosting. Pickup requests download to the customer's device; they are not transmitted, confirmed, paid, or reserved.

On Windows, use your installed `python` command in place of `python3`.

## Project files

| File | Role |
| --- | --- |
| `market_config.json` | Project ID, active round, stock, buyer estimates, real competitor offers, strategy, and overrides. |
| `pricing_engine.py` | Validated whole-dollar pricing, discounted loyalty offers, audit/history export, replay inputs, atomic JSON writes, and a single-writer lock. |
| `buyer_ping_client.py` | Verified-TLS REST notifications for purchases already committed by the buyer program. |
| `live_market_listener.py` | Current-round feed watcher, persistent stock mirror, batched repricing, recoverable publishing, and optional serial deploys. |
| `public/index.html` | Existing storefront design, pickup form, and public price-history table. |
| `public/app.js` | Validates catalogs, handles requests, polls public files, and optionally loads the simulated-trade ticker. |
| `public/published_price_file.json` | Official downloadable offer snapshot; Standard and Prime reference shared SKU stock. |
| `public/decision_audit_log.json` | Sanitized public price and eligibility history, including withdrawals. |
| `decision_audit_log.json` | Detailed decision records, including the original historical records previously stored in both audit files. |
| `firebase.json`, `.firebaserc` | Hosting directory, cache headers, and Firebase project selection. |
| `requirements.txt` | Tested Firestore Python SDK version; needed only for the listener. |
| `package.json`, `package-lock.json` | Optional JavaScript syntax check. The unused npm Firebase dependency has been removed. |
| `tests/` | Offline Python regressions and optional browser scenarios. |

The runtime creates `.market_state.json` and `.market_listener.lock`. They are ignored by Git. Do not reset the checkpoint during normal operation.

Detailed audits are excluded from the **hosted folder**, but a root-level file tracked in a public GitHub repository is still public on GitHub. Keeping records outside `public/` is not a GitHub access control.

## Configure pricing

Edit `market_config.json` instead of editing duplicate inventories inside Python files.

- `inventory_virtual` mirrors the buyer team's authoritative ledger.
- `inventory_physical` counts individually available, unpackaged soldiers. `physical_pouch_count` counts a separate sealed pool; do not count the same physical figure in both.
- `recent_sales_velocity` and `bot_belief` are estimates. Replace the carried-forward examples with supported observations.
- `competitor_offers` accepts objects with `seller_id`, `sku_id`, `unit_price`, and `virtual_available`. Use public standard single-unit prices. The listener does not treat a competitor's Prime sale price as its standard price.
- `strategy_mode` is `dynamic_response`, `fixed_price`, or `fixed_markup`.
- `manual_overrides` maps an offer ID to `[whole_dollar_price, "reason written before results"]`. Unknown IDs, missing reasons, below-cost prices, and invalid numbers are rejected.
- Set `enable_prime_discount_offers` to false if simulated loyalty has not been approved. The instructor and buyer team must approve support before using it in scored rounds.

Prices remain whole dollars from $1 to $5. Prime prices follow their base price, stay above assigned cost, and publish the actual discount amount. The pouch follows Silver unless explicitly overridden. The ad calculation is an expected-margin bid limit using assumed reach and conversion; it is not an auction winner or guaranteed return.

The EWMA tracker expects mutually exclusive Standard Green, Standard Silver, Standard Gold, and Prime sale counts. Do not count Prime sales again in the Standard totals.

## Buyer integration

Call only after the buyer ledger commits the purchase and its budget and inventory changes:

```python
from buyer_ping_client import BuyerMarketBroadcaster

broadcaster = BuyerMarketBroadcaster(
    timeout_seconds=3.0,
    # token_provider=get_current_buyer_token,  # If your Firebase authorization requires it.
)

sent = broadcaster.ping_trade(
    round_number=4,
    seller_id="Vanguard_Garrison",
    sku_id="SKU_SILVER",
    offer_id="BASE_SKU_SILVER",
    price_paid=3.0,
    remaining_virtual=15,
    transaction_id="round-4-committed-purchase-0001",
    occurred_at="2026-10-29T10:00:00Z",
    is_test_replay=True,  # Skips all network calls. Use False for an official purchase.
)
```

This sample is intentionally a replay notification. Use actual accepted values and `is_test_replay=False` in production. A `False` result means skipped or not confirmed; retain a failed official notification for retry without repeating the purchase.

Reuse the transaction ID and accepted values on retry. Repeated notifications use a deterministic Firestore document ID. If that ID already exists, the client verifies the business fields before reporting success. Invalid trade inputs raise `ValueError` before a request is sent. Omitting the transaction ID remains compatible with the old call signature, but a stable ledger ID is preferred.

The legacy core fields remain `round`, `seller_id`, `sku_id`, `offer_id`, `price_paid`, `remaining_virtual`, and `timestamp`. New writes also include `market: "simulated"`, `quantity: 1`, and `event_id`. Update any restrictive field allowlist in your existing Firestore rules to match. Rules/IAM must authorize the buyer; do not open unrestricted writes just to make a notification succeed.

A token provider can return a refreshed Firebase ID token or authorized Google OAuth access token. See [Firestore REST authentication](https://firebase.google.com/docs/firestore/use-rest-api). TLS verification is always enabled; certificate errors no longer trigger an insecure retry.

## Run the listener

Install its dependency:

```bash
python3 -m pip install -r requirements.txt
python3 live_market_listener.py --credentials serviceAccountKey.json
```

This command connects to Firestore and updates its catalog mirror and local JSON files. Hosting deployment is disabled unless you pass `--deploy`. The service-account file must be authorized for the configured project and must stay out of Git and `public/`. Application Default Credentials are also supported.

The listener filters by the configured round, processes notifications on one worker, and normally groups arrivals for two seconds. Same-timestamp sales can still lower stock. Old events, duplicates, and sale notifications that would replenish stock cannot restore inventory. Physical inventory never changes in response to simulated notifications.

Before any publish, the event cursor, stock, offers, and decision ID are saved together. Failed writes/deploys retry the same pending update, including after restart. Decision IDs prevent duplicate audit entries. Corrupt JSON blocks publication instead of silently erasing history. A project lock prevents the pricing CLI and listener from writing concurrently.

To enable actual Hosting deployment after installing and authenticating the Firebase CLI:

```bash
npm install -g firebase-tools
firebase login
python3 live_market_listener.py --credentials serviceAccountKey.json --deploy
```

Deploys run sequentially with an explicit project ID, checked exit codes, and logged failures. A fixed debounce deadline prevents a busy feed from postponing updates indefinitely. Without `--deploy`, remote static catalogs do not update until you deploy them manually:

```bash
firebase deploy --only hosting --project toy-soldier-9bf43
```

The storefront uses the hosted JSON for prices, not the possibly newer Firestore mirror. The Firestore document is now an object containing `offers`, `catalog_timestamp`, and `decision_id`; update any separate consumers of the old map if needed.

Stop the listener before changing configuration or running the pricing CLI. At the next round, update the active round and remaining rounds. Stock is carried forward. For an instructor-approved restock/correction, reconcile both the config and checkpoint to the buyer ledger while stopped; a sale event alone cannot authorize a stock increase.

The buyer's saved cutoff snapshot still governs that simulated round. Later public updates apply to later simulated rounds and never change an accepted order. Public history timestamps identify when the catalog was prepared, not an independent server-confirmed deployment time.

## Checks and replay inputs

```bash
python3 -m unittest discover -s tests -v
npm run check
python3 pricing_engine.py --replays
```

Replay catalogs are written under `replays/` without changing the official published catalog. They are input files, not completed simulations. The buyer must recompute purchases from the same opening state, customer draws, seed, and fixed competitor offers. No replay should alter the official ledger.

Optional browser checks:

```bash
npm install --no-save --package-lock=false playwright
npx playwright install chromium
node tests/storefront_checks.cjs
```

These scenarios block all non-local network requests and use mocked trade data. They cover normal loading, invalid catalogs, independent physical/virtual availability, refresh recovery, Firebase failure, safe trade text, subscription reuse, missing Prime offers, mobile overflow, and pickup downloads.

The original visual design is retained. Unsupported hardcoded scarcity and fundraising progress claims were removed. Actual stock reconciliation, instructor approvals, production Security Rules/IAM, Firebase CLI credentials, and a real deployment require the live class environment; offline checks do not validate those.

## Operational boundaries

This is seller-side code. It does not implement the buyer's customer-choice program, a payment processor, a reservation service, or a physical-sales ledger. Record real physical sales, expenses, and cash separately, then update physical availability promptly. Confirm the full price with the customer before accepting an order.
