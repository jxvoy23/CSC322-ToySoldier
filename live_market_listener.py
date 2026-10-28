"""
Live Firestore Market Ticker & Dynamic Repricing Engine
======================================================
Listens to incoming trade events from Cloud Firestore in real time.
Updates inventory, recalculates whole-dollar best responses, and
synchronizes published_price_file.json immediately.
"""

import json
import time
from google.cloud import firestore
from pricing_engine import DualMarketPricingEngine, SKUState, CompetitorOffer, BotArchetypeTracker, BotPopulationBelief

PROJECT_ID = "toy-soldier-9bf43"
db = firestore.Client.from_service_account_json("serviceAccountKey.json")

# Initial local state
my_skus = {
    "SKU_GREEN": SKUState("SKU_GREEN", tier=1, wholesale_cost=0.25, inventory_virtual=28, inventory_physical=24, recent_sales_velocity=4.0, fixed_price_baseline=2.00),
    "SKU_SILVER": SKUState("SKU_SILVER", tier=2, wholesale_cost=0.25, inventory_virtual=16, inventory_physical=14, recent_sales_velocity=7.0, fixed_price_baseline=3.00),
    "SKU_GOLD": SKUState("SKU_GOLD", tier=3, wholesale_cost=0.25, inventory_virtual=7, inventory_physical=6, recent_sales_velocity=5.0, fixed_price_baseline=5.00),
}

competitor_state = {
    "Team_Alpha": {"SKU_GREEN": 18, "SKU_SILVER": 10, "SKU_GOLD": 4},
    "Team_Beta": {"SKU_GREEN": 2, "SKU_SILVER": 0, "SKU_GOLD": 0},
}

engine = DualMarketPricingEngine(min_whole_price=1.00, max_price_cap=5.00, stockout_squeeze_threshold=3)
bot_belief = BotPopulationBelief()

def on_trade_snapshot(col_snapshot, changes, read_time):
    """Callback triggered whenever a trade is appended to Firestore."""
    for change in changes:
        if change.type.name == 'ADDED':
            trade = change.document.to_dict()
            print(f"\n[LIVE TICKER] Trade executed: {trade.get('seller_id')} sold {trade.get('sku_id')} for ${trade.get('price_paid')}")
            
            seller = trade.get('seller_id')
            sku = trade.get('sku_id')
            rem = trade.get('remaining_virtual', 0)
            
            # 1. Update stock tracking
            if seller == "Vanguard_Garrison" and sku in my_skus:
                my_skus[sku].inventory_virtual = rem
            elif seller in competitor_state and sku in competitor_state[seller]:
                competitor_state[seller][sku] = rem
                if rem <= 3:
                    print(f"🔥 [STOCKOUT SQUEEZE DETECTED] {seller} has only {rem} units left of {sku}! Raising prices...")

            # 2. Re-evaluate Competitor Offers
            comp_offers = []
            for s_name, skus in competitor_state.items():
                for s_id, s_qty in skus.items():
                    # Default competitor price reference
                    p = 2.00 if s_id == "SKU_GREEN" else (3.00 if s_id == "SKU_SILVER" else 5.00)
                    comp_offers.append(CompetitorOffer(s_name, s_id, p, s_qty))

            # 3. Re-calculate Real-Time Offers
            offers, audit = engine.build_daily_catalog(
                skus=my_skus,
                competitor_offers=comp_offers,
                bot_belief=bot_belief,
                rounds_remaining=5,
                physical_pouch_count=12,
                round_number=4
            )

            # 4. Overwrite public/published_price_file.json
            with open("public/published_price_file.json", "w", encoding="utf-8") as f:
                json.dump([o.__dict__ for o in offers], f, indent=2)

            # 5. Push Live State directly to Firestore /vanguard_live/state
            live_doc = {o.offer_id: {"price": o.total_price, "virtual": o.virtual_available, "physical": o.physical_available} for o in offers}
            live_doc["last_updated"] = firestore.SERVER_TIMESTAMP
            db.collection("vanguard_live").document("state").set(live_doc)
            print(f"✔ Live catalog updated & synced to Firestore: {[f'{o.offer_id}: ${o.total_price}' for o in offers]}")

# Listen to the market feed collection
feed_query = db.collection("market_feed")
watcher = feed_query.on_snapshot(on_trade_snapshot)

print("🚀 Vanguard Live Market Listener active. Waiting for trade pings...")
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    watcher.unsubscribe()