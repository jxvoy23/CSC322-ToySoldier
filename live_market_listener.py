"""
Live Firestore Market Ticker & Dynamic Repricing Engine
======================================================
Listens to incoming trade events from Cloud Firestore in real time.
Updates inventory, recalculates whole-dollar best responses (including
Stockout Squeezes), and synchronizes the live UI and published JSON.
"""

import json
import time
import subprocess
from datetime import datetime
from google.cloud import firestore
from pricing_engine import (
    DualMarketPricingEngine,
    SKUState,
    CompetitorOffer,
    BotPopulationBelief,
)

PROJECT_ID = "toy-soldier-9bf43"
db = firestore.Client.from_service_account_json("serviceAccountKey.json")

# 1. Full SKU dictionary (must be a dict of SKUState objects, not { ... })
my_skus = {
    "SKU_GREEN": SKUState(
        sku_id="SKU_GREEN",
        tier=1,
        wholesale_cost=0.25,
        inventory_virtual=28,
        inventory_physical=24,
        recent_sales_velocity=4.0,
        fixed_price_baseline=2.00,
    ),
    "SKU_SILVER": SKUState(
        sku_id="SKU_SILVER",
        tier=2,
        wholesale_cost=0.25,
        inventory_virtual=16,
        inventory_physical=14,
        recent_sales_velocity=7.0,
        fixed_price_baseline=3.00,
    ),
    "SKU_GOLD": SKUState(
        sku_id="SKU_GOLD",
        tier=3,
        wholesale_cost=0.25,
        inventory_virtual=7,
        inventory_physical=6,
        recent_sales_velocity=5.0,
        fixed_price_baseline=5.00,
    ),
}

# 2. Competitor state tracking both scraped price and virtual stock
competitor_state = {
    "Team_Alpha": {
        "SKU_GREEN": {"price": 2.00, "stock": 18},
        "SKU_SILVER": {"price": 3.00, "stock": 10},
        "SKU_GOLD": {"price": 5.00, "stock": 4},
    },
    "Team_Beta": {
        "SKU_GREEN": {"price": 1.00, "stock": 2},
        "SKU_SILVER": {"price": 3.00, "stock": 0},
        "SKU_GOLD": {"price": 5.00, "stock": 0},
    },
    "Group_1": {
        "SKU_GREEN": {"price": 2.00, "stock": 20},
        "SKU_SILVER": {"price": 3.00, "stock": 15},
        "SKU_GOLD": {"price": 5.00, "stock": 5},
    },
    "Group_2": {
        "SKU_GREEN": {"price": 2.00, "stock": 18},
        "SKU_SILVER": {"price": 3.00, "stock": 10},
        "SKU_GOLD": {"price": 5.00, "stock": 4},
    },
    "Group_3": {
        "SKU_GREEN": {"price": 2.00, "stock": 18},
        "SKU_SILVER": {"price": 3.00, "stock": 10},
        "SKU_GOLD": {"price": 5.00, "stock": 4},
    },
}

MY_ALIASES = ["Vanguard_Garrison", "Group 5", "Group_5", "Group5", "Team_5"]

engine = DualMarketPricingEngine(
    min_whole_price=1.00,
    max_price_cap=5.00,
    stockout_squeeze_threshold=3,
)
bot_belief = BotPopulationBelief()

# Global tracker for chronology and deduplication
processed_trades = {}


def on_trade_snapshot(col_snapshot, changes, read_time):
    deploy_required = False

    for change in changes:
        if change.type.name == "ADDED":
            trade = change.document.to_dict()
            seller = trade.get("seller_id")
            sku = trade.get("sku_id")
            price_paid = float(trade.get("price_paid", 2.00))
            rem = int(trade.get("remaining_virtual", 0))
            trade_time_str = trade.get("timestamp")

            if not seller or not sku or seller == "NONE":
                continue

            # 1. Deduplication and Chronology Check
            trade_key = f"{seller}_{sku}"
            if trade_time_str:
                try:
                    trade_time = datetime.fromisoformat(
                        trade_time_str.replace("Z", "+00:00")
                    )
                    if (
                        trade_key in processed_trades
                        and trade_time <= processed_trades[trade_key]
                    ):
                        continue
                    processed_trades[trade_key] = trade_time
                except Exception:
                    pass

            print(
                f"\n[LIVE TICKER] Trade executed: {seller} sold {sku} for ${price_paid:.2f} (Remaining: {rem})"
            )

            # 2. Update our stock OR update competitor's scraped price & stock
            if seller in MY_ALIASES and sku in my_skus:
                my_skus[sku].inventory_virtual = rem
                deploy_required = True
            else:
                if seller not in competitor_state:
                    competitor_state[seller] = {}
                competitor_state[seller][sku] = {"price": price_paid, "stock": rem}
                deploy_required = True
                if rem <= 3:
                    print(
                        f"🔥 [STOCKOUT SQUEEZE DETECTED] {seller} has only {rem} units left of {sku}! Raising prices..."
                    )

    # 3. Recalculate and auto-deploy
    if deploy_required:
        comp_offers = []
        for s_name, skus_dict in competitor_state.items():
            for s_id, info in skus_dict.items():
                if isinstance(info, dict):
                    p = float(info.get("price", 2.00))
                    s_qty = int(info.get("stock", 0))
                else:
                    p = (
                        2.00
                        if s_id == "SKU_GREEN"
                        else (3.00 if s_id == "SKU_SILVER" else 5.00)
                    )
                    s_qty = int(info)
                comp_offers.append(CompetitorOffer(s_name, s_id, p, s_qty))

        offers, audit = engine.build_daily_catalog(
            skus=my_skus,
            competitor_offers=comp_offers,
            bot_belief=bot_belief,
            rounds_remaining=5,
            physical_pouch_count=12,
            round_number=4,
            strategy_mode="dynamic_response",
        )

        # Overwrite local JSON for static scrapers
        with open("public/published_price_file.json", "w", encoding="utf-8") as f:
            json.dump([o.__dict__ for o in offers], f, indent=2)

        # Push live state to Firestore for real-time website updates
        live_doc = {
            o.offer_id: {
                "price": o.total_price,
                "virtual": o.virtual_available,
                "physical": o.physical_available,
            }
            for o in offers
        }
        live_doc["last_updated"] = firestore.SERVER_TIMESTAMP
        db.collection("vanguard_live").document("state").set(live_doc)

        print(
            f"✔ Live catalog updated: {[f'{o.offer_id}: ${o.total_price:.2f} (virt: {o.virtual_available})' for o in offers]}"
        )

        # Auto-Deploy hosting in background
        subprocess.Popen(
            ["firebase", "deploy", "--only", "hosting"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


feed_query = db.collection("market_feed")
watcher = feed_query.on_snapshot(on_trade_snapshot)

print("🚀 Vanguard Live Market Listener active. Waiting for trade pings...")
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    watcher.unsubscribe()