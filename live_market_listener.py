import json
import time
import subprocess
from datetime import datetime
from google.cloud import firestore
from pricing_engine import DualMarketPricingEngine, SKUState, CompetitorOffer, BotArchetypeTracker, BotPopulationBelief

PROJECT_ID = "toy-soldier-9bf43"
db = firestore.Client.from_service_account_json("serviceAccountKey.json")

# State management
my_skus = { ... } # Keep your existing SKU definitions
competitor_state = { ... } # Keep your existing competitor states
engine = DualMarketPricingEngine(min_whole_price=1.00, max_price_cap=5.00, stockout_squeeze_threshold=3)
bot_belief = BotPopulationBelief()

# Prevents old or duplicate events from overwriting current inventory
processed_trades = {}

def on_trade_snapshot(col_snapshot, changes, read_time):
    deploy_required = False

    for change in changes:
        if change.type.name == 'ADDED':
            trade = change.document.to_dict()
            seller = trade.get('seller_id')
            sku = trade.get('sku_id')
            trade_time_str = trade.get('timestamp')
            
            # 1. Deduplication and Chronology Check
            trade_key = f"{seller}_{sku}"
            try:
                trade_time = datetime.fromisoformat(trade_time_str.replace("Z", "+00:00"))
                if trade_key in processed_trades and trade_time <= processed_trades[trade_key]:
                    print(f"Skipping stale/duplicate trade for {trade_key}")
                    continue
                processed_trades[trade_key] = trade_time
            except Exception:
                pass # Fallback if timestamp is malformed

            print(f"\n[LIVE TICKER] Trade executed: {seller} sold {sku} for ${trade.get('price_paid')}")
            rem = trade.get('remaining_virtual', 0)
            
            my_aliases = ["Vanguard_Garrison", "Group 5", "Group_5", "Group5", "Team_5"]
            
            # 2. Update stock tracking
            if seller in my_aliases and sku in my_skus:
                my_skus[sku].inventory_virtual = rem
                deploy_required = True
            elif seller in competitor_state and sku in competitor_state[seller]:
                competitor_state[seller][sku] = rem
                if rem <= 3:
                    print(f"🔥 [STOCKOUT SQUEEZE DETECTED] {seller} has only {rem} units left of {sku}! Raising prices...")
                    deploy_required = True

    # 3. Only recalculate and deploy if relevant state changed
    if deploy_required:
        comp_offers = []
        for s_name, skus in competitor_state.items():
            for s_id, s_qty in skus.items():
                p = 2.00 if s_id == "SKU_GREEN" else (3.00 if s_id == "SKU_SILVER" else 5.00)
                comp_offers.append(CompetitorOffer(s_name, s_id, p, s_qty))

        offers, audit = engine.build_daily_catalog(
            skus=my_skus,
            competitor_offers=comp_offers,
            bot_belief=bot_belief,
            rounds_remaining=5,
            physical_pouch_count=12,
            round_number=4
        )

        # Overwrite local JSON
        with open("public/published_price_file.json", "w", encoding="utf-8") as f:
            json.dump([o.__dict__ for o in offers], f, indent=2)

        # Push state to Firestore
        live_doc = {o.offer_id: {"price": o.total_price, "virtual": o.virtual_available, "physical": o.physical_available} for o in offers}
        live_doc["last_updated"] = firestore.SERVER_TIMESTAMP
        db.collection("vanguard_live").document("state").set(live_doc)
        
        # 4. Auto-Deploy to keep the public download link in sync
        print("Triggering Firebase Deploy to sync published_price_file.json...")
        subprocess.Popen(["firebase", "deploy", "--only", "hosting"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("✔ Live catalog updated & auto-deploy initiated.")

feed_query = db.collection("market_feed")
watcher = feed_query.on_snapshot(on_trade_snapshot)

print("🚀 Vanguard Live Market Listener active. Waiting for trade pings...")
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    watcher.unsubscribe()