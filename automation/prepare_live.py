"""Prepare the optimized catalog from the existing Firebase inventory.

This updates local files only. It does not write to Firestore or deploy Hosting.
"""

import hashlib
import json
import logging
import sys
from datetime import datetime, timezone

from automation.firebase_inventory import inventory_from_state, public_firebase_state
from pricing_engine import ROOT, exclusive_project_lock, export_price_file_and_audit_log, load_market_config, number, read_json


def prepare(root=ROOT):
    config, _, _, _, _ = load_market_config(root / "market_config.json")
    state = public_firebase_state(config["project_id"])
    inventory_from_state(state)
    catalog = read_json(root / "public/published_price_file.json")
    if not isinstance(catalog, list) or not catalog:
        raise ValueError("The local public catalog is missing or empty")
    stamp = state.get("last_updated")
    if not isinstance(stamp, datetime) or stamp.tzinfo is None:
        raise ValueError("Firebase inventory needs a timezone-aware last_updated timestamp")
    stamp = stamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    for offer in catalog:
        if not isinstance(offer, dict) or offer.get("offer_id") not in state:
            raise ValueError("A catalog offer is missing from the Firebase inventory")
        live = state[offer["offer_id"]]
        if not isinstance(live, dict):
            raise ValueError("A Firebase offer has an invalid format")
        price = number(live.get("price"), "Firebase published price", 1)
        if price > 5 or not price.is_integer():
            raise ValueError("Firebase price is outside the whole-dollar $1–$5 range")
        offer.update(total_price=price, virtual_available=live["virtual"],
                     physical_available=live["physical"], timestamp=stamp,
                     simulated_stock_status="not_synchronized")
    basis = hashlib.sha256(json.dumps(catalog, sort_keys=True).encode("utf-8")).hexdigest()
    audit = {"decision_id": "firebase-baseline-" + basis, "timestamp": stamp,
             "round_number": config["round_number"], "source": "Firebase existing inventory and published prices"}
    export_price_file_and_audit_log(catalog, audit, root / "public/published_price_file.json",
        root / "data/decision_audit_log.json", public_history_path=root / "public/decision_audit_log.json")
    print("Prepared the optimized catalog from Firebase. Firebase inventory timestamp:", stamp)
    print("Review the inventory and catalog. No Firebase data was changed and no site was deployed.")


def main():
    try:
        with exclusive_project_lock(ROOT):
            prepare(ROOT)
        return 0
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        logging.error("Live catalog preparation failed: %s", error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
