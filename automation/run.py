"""Reprice from buyer results and competitor sites, once or every 30 minutes."""

import argparse
import hashlib
import logging
import re
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

from automation.sources import collect_competitors, read_buyer_source, validate_buyer_report
from pricing_engine import (
    ROOT, SKU_IDS, atomic_write_json, count, exclusive_project_lock,
    export_price_file_and_audit_log, load_market_config, number, read_json, utc_now,
)

LOGGER = logging.getLogger(__name__)


def load_settings(path):
    settings = read_json(path)
    if not isinstance(settings, dict):
        raise ValueError(f"Automation settings not found or not a JSON object: {path}")
    interval = count(settings.get("interval_seconds"), "interval_seconds", 60)
    timeout = number(settings.get("request_timeout_seconds"), "request_timeout_seconds", 1)
    buyer_poll = count(settings.get("buyer_poll_seconds", 60), "buyer_poll_seconds", 15)
    if timeout > 120:
        raise ValueError("request_timeout_seconds must be at most 120")
    if interval < timeout:
        raise ValueError("The polling interval must be at least the request timeout")
    if buyer_poll > interval:
        raise ValueError("buyer_poll_seconds must not exceed interval_seconds")
    return settings


def initial_stock(root, config, skus):
    stock = {sku: skus[sku].inventory_virtual for sku in SKU_IDS}
    old = read_json(root / ".market_state.json", {})
    if not isinstance(old, dict):
        raise ValueError("Old listener checkpoint is invalid; review it before migrating")
    if old.get("pending_publication") is not None:
        raise ValueError("The old listener has a pending publication; resolve it before migrating")
    if old.get("round_number", 0) > config["round_number"]:
        raise ValueError("The old listener checkpoint is newer than the configured round")
    old_stock = old.get("virtual_inventory", {})
    if not isinstance(old_stock, dict):
        raise ValueError("Old listener inventory is invalid")
    for sku in SKU_IDS:
        if sku in old_stock:
            stock[sku] = min(stock[sku], count(old_stock[sku], f"old checkpoint {sku} stock"))
    return stock


def deploy(root, project_id, target):
    if target == "local":
        return
    if target == "live":
        command = ["npx", "-y", "firebase-tools@latest", "deploy", "--only", "hosting"]
    elif target.startswith("preview:"):
        channel = target.split(":", 1)[1]
        command = ["npx", "-y", "firebase-tools@latest", "hosting:channel:deploy", channel]
    else:
        raise ValueError("Invalid deployment target")
    command.extend(["--project", project_id, "--non-interactive"])
    try:
        result = subprocess.run(command, cwd=root, text=True, capture_output=True,
                                timeout=180, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError("Firebase deployment could not complete; the update is saved for retry") from error
    if result.returncode:
        raise RuntimeError("Firebase deployment failed; the update is saved for retry. "
                           + (result.stderr or result.stdout)[-1200:])
    LOGGER.info("Firebase Hosting deployment succeeded: %s", target)


def publish_pending(root, state, state_path, target):
    pending = state["pending_publication"]
    if pending["target"] != target:
        raise ValueError(f"A publication is pending for {pending['target']}; retry with the same target")
    if pending.get("firebase_inventory") is not None:
        from automation.firebase_inventory import firebase_client, inventory_from_state
        client, _ = firebase_client(pending["project_id"], root)
        snap = client.collection("vanguard_live").document("state").get()
        virtual, physical, pouch = inventory_from_state(snap.to_dict() if snap.exists else None)
        current = {"remaining_virtual": virtual, "physical_inventory": physical, "physical_pouch_count": pouch}
        if current != pending["firebase_inventory"]:
            state["pending_publication"] = None
            atomic_write_json(state_path, state)
            raise RuntimeError("Firebase inventory changed before publishing; stale catalog discarded, retry the cycle")
    private_audit = root / "data/decision_audit_log.json"
    legacy_audit = root / "decision_audit_log.json"
    if not private_audit.exists() and legacy_audit.exists():
        previous = read_json(legacy_audit)
        if not isinstance(previous, list):
            raise ValueError("Existing detailed audit is not a JSON array")
        atomic_write_json(private_audit, previous)
    export_price_file_and_audit_log(
        pending["offers"], pending["audit"], root / "public/published_price_file.json",
        private_audit, public_history_path=root / "public/decision_audit_log.json")
    deploy(root, pending["project_id"], target)
    state["pending_publication"] = None
    atomic_write_json(state_path, state)
    LOGGER.info("Published catalog prepared for %s; buyer report %s", target, state["buyer_report"]["report_id"])


def run_cycle(root=ROOT, settings_path=None, target="local", increase_reason=None, prices_only=False):
    """Run while holding the project lock; resume an unfinished publication first."""
    root = Path(root).resolve()
    settings_path = Path(settings_path or root / "config/automation.json").resolve()
    state_path = root / "data/automation_state.json"
    state = read_json(state_path, {})
    if not isinstance(state, dict):
        raise ValueError("Automation state is not a JSON object")
    if state.get("pending_publication") is not None:
        LOGGER.warning("Retrying the previously prepared publication")
        publish_pending(root, state, state_path, target)
        return "retried"
    settings = load_settings(settings_path)
    config, skus, _, belief, engine = load_market_config(root / "market_config.json")
    competitors, source_details = collect_competitors(
        settings.get("competitors"), config.get("seller_aliases", []) + [config["seller_id"]],
        settings["request_timeout_seconds"])
    buyer_source = settings.get("buyer_results")
    if prices_only:
        from automation.firebase_inventory import firebase_client, inventory_from_state
        client, _ = firebase_client(config["project_id"], root)
        snapshot = client.collection("vanguard_live").document("state").get()
        firebase_state = snapshot.to_dict() if snapshot.exists else None
        virtual, physical, pouch_count = inventory_from_state(firebase_state)
        for sku in SKU_IDS:
            skus[sku].inventory_virtual = virtual[sku]
            skus[sku].inventory_physical = physical[sku]
        config["physical_pouch_count"] = pouch_count
        buyer = {"report_id": "firebase:prices-only", "reported_at": utc_now(),
                 "remaining_virtual": virtual, "physical_inventory": physical,
                 "inventory_source": "Firestore vanguard_live/state", "prices_only": True}
        LOGGER.warning("Prices-only mode: Firebase stock is NOT synchronized with buyer sales")
    elif isinstance(buyer_source, dict) and buyer_source.get("type") in ("csv_firestore", "json_firestore"):
        if increase_reason:
            raise ValueError("Correct Firestore inventory separately before running the buyer-results watcher")
        from automation.firebase_inventory import (
            apply_csv_transaction, firebase_client, inventory_from_state, load_buyer_input, parse_buyer_input,
        )
        content = load_buyer_input(buyer_source, root, settings["request_timeout_seconds"])
        if content is None:
            raise ValueError("Buyer results have not arrived; full watcher cannot verify inventory")
        events = parse_buyer_input(buyer_source, content, config.get("seller_aliases", []) + [config["seller_id"]])
        client, firestore = firebase_client(config["project_id"], root)
        firebase_state, imported = apply_csv_transaction(client, firestore, events)
        virtual, physical, pouch_count = inventory_from_state(firebase_state)
        for sku in SKU_IDS:
            skus[sku].inventory_virtual = virtual[sku]
            skus[sku].inventory_physical = physical[sku]
        config["physical_pouch_count"] = pouch_count
        label = "json" if buyer_source["type"] == "json_firestore" else "csv"
        buyer = {"report_id": label + ":" + hashlib.sha256(content).hexdigest()[:20] if content is not None else label + ":not-arrived",
                 "reported_at": utc_now(), "remaining_virtual": virtual, "physical_inventory": physical,
                 "new_transactions": imported, "results_missing": content is None,
                 "inventory_source": "Firestore vanguard_live/state"}
    else:
        previous = state.get("buyer_report") or {"remaining_virtual": initial_stock(root, config, skus)}
        report = read_buyer_source(buyer_source, root, settings["request_timeout_seconds"])
        buyer = validate_buyer_report(report, config, previous, increase_reason)
        for sku in SKU_IDS:
            skus[sku].inventory_virtual = buyer["remaining_virtual"][sku]
    offers, audit = engine.build_daily_catalog(
        skus, competitors, belief, config["rounds_remaining"], config["physical_pouch_count"],
        config["round_number"], config.get("strategy_mode", "dynamic_response"),
        config.get("manual_overrides", {}))
    audit = asdict(audit)
    audit["buyer_report"] = buyer
    audit["competitor_sources"] = source_details
    if increase_reason:
        audit["inventory_increase_reason"] = increase_reason.strip()
    state["buyer_report"] = buyer
    state["competitor_offers"] = [asdict(offer) for offer in competitors]
    published_offers = [asdict(offer) for offer in offers]
    for offer in published_offers:
        offer["simulated_stock_status"] = "not_synchronized" if prices_only else "buyer_report"
    state["pending_publication"] = {
        "target": target, "project_id": config["project_id"],
        "offers": published_offers, "audit": audit,
    }
    if buyer.get("inventory_source") == "Firestore vanguard_live/state":
        state["pending_publication"]["firebase_inventory"] = {
            "remaining_virtual": buyer["remaining_virtual"], "physical_inventory": buyer["physical_inventory"],
            "physical_pouch_count": config["physical_pouch_count"],
        }
    atomic_write_json(state_path, state)
    publish_pending(root, state, state_path, target)
    return "completed"


def scan_competitors(root=ROOT, settings_path=None):
    """Save a private observation without buyer data, repricing, or deployment."""
    root = Path(root).resolve()
    settings = load_settings(Path(settings_path or root / "config/automation.json").resolve())
    config, _, _, _, _ = load_market_config(root / "market_config.json")
    offers, sources = collect_competitors(
        settings.get("competitors"), config.get("seller_aliases", []) + [config["seller_id"]],
        settings["request_timeout_seconds"])
    snapshot = {"checked_at": utc_now(), "competitor_sources": sources,
                "competitor_offers": [asdict(offer) for offer in offers]}
    path = root / "data/competitor_snapshot.json"
    atomic_write_json(path, snapshot)
    for offer in offers:
        availability = (f"{offer.virtual_available} reported" if offer.availability_status == "known"
                        else offer.availability_status.replace("_", " "))
        LOGGER.info("%s: %s $%.2f (simulated stock: %s)",
                    offer.seller_id, offer.sku_id, offer.unit_price, availability)
    LOGGER.info("Private competitor snapshot saved to %s", path)


def initialize_csv_baseline(root, settings_path, mode):
    """Declare whether current Firebase stock already includes the report's sales."""
    from automation.firebase_inventory import apply_csv_transaction, firebase_client, load_buyer_input, parse_buyer_input
    root = Path(root).resolve()
    settings = load_settings(settings_path)
    config, _, _, _, _ = load_market_config(root / "market_config.json")
    source = settings.get("buyer_results")
    if not isinstance(source, dict) or source.get("type") not in ("csv_firestore", "json_firestore"):
        raise ValueError("Configure a CSV or JSON Firestore buyer source before initializing")
    content = load_buyer_input(source, root, settings["request_timeout_seconds"])
    if mode == "existing" and content is None:
        raise ValueError("Existing buyer results are required to mark their transactions as already counted")
    events = parse_buyer_input(source, content, config.get("seller_aliases", []) + [config["seller_id"]]) if content is not None else []
    client, firestore = firebase_client(config["project_id"], root)
    apply_csv_transaction(client, firestore, events, initialize=mode)
    LOGGER.info("Firebase buyer baseline initialized (%s); current Firebase inventory was not changed", mode)


def csv_signature(root, settings):
    from automation.firebase_inventory import load_buyer_input
    content = load_buyer_input(settings["buyer_results"], root, settings["request_timeout_seconds"])
    return hashlib.sha256(content).hexdigest() if content is not None else "missing"


def watch_csv(root, settings_path, target):
    """Poll buyer results each minute; scan sellers every 30 minutes or on change."""
    last_signature = None
    next_market_check = 0
    while True:
        started = time.monotonic()
        poll = 60
        try:
            settings = load_settings(settings_path)
            poll = settings.get("buyer_poll_seconds", 60)
            signature = csv_signature(root, settings)
            if signature != last_signature or started >= next_market_check:
                result = run_cycle(root, settings_path, target)
                if result == "completed":
                    last_signature = signature
                    next_market_check = started + settings["interval_seconds"]
                else:
                    # A pending deploy was retried; check again for newer buyer data.
                    next_market_check = started
        except Exception as error:
            LOGGER.error("Update skipped: %s", error)
            next_market_check = started + poll
        time.sleep(max(0, poll - (time.monotonic() - started)))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=Path, default=ROOT / "config/automation.json",
                        help="Automation settings JSON (default: config/automation.json)")
    parser.add_argument("--watch", action="store_true", help="Run now and keep checking at the configured interval")
    parser.add_argument("--scan-only", action="store_true",
                        help="Only save competitor observations; do not read buyer results or publish")
    parser.add_argument("--prices-only", action="store_true",
                        help="Reprice using last recorded Firebase stock; buyer stock is not synchronized")
    parser.add_argument("--initialize-csv-baseline", "--initialize-buyer-baseline", choices=("empty", "existing"),
                        help="One-time Firebase inventory baseline; existing marks current buyer sales as already counted")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--deploy-preview", metavar="CHANNEL", help="Deploy each update to a Hosting preview channel")
    target.add_argument("--deploy-live", action="store_true", help="Deploy each update to the live Hosting site")
    parser.add_argument("--allow-increase-reason", metavar="REASON",
                        help="For one run, record an instructor-approved inventory correction")
    args = parser.parse_args(argv)
    if args.initialize_csv_baseline and (args.watch or args.scan_only or args.prices_only or args.deploy_preview or args.deploy_live or args.allow_increase_reason):
        parser.error("Initialize the buyer-results baseline as a separate one-time command")
    if args.prices_only and (args.scan_only or args.allow_increase_reason):
        parser.error("--prices-only cannot be combined with --scan-only or an inventory correction")
    if args.scan_only and (args.deploy_preview or args.deploy_live or args.allow_increase_reason):
        parser.error("--scan-only cannot be combined with a deploy option or inventory correction")
    if args.allow_increase_reason and args.watch:
        parser.error("Use --allow-increase-reason for a single run, then restart --watch without it")
    if args.allow_increase_reason is not None and not args.allow_increase_reason.strip():
        parser.error("The inventory correction reason cannot be blank")
    if args.deploy_preview and not re.fullmatch(r"[a-zA-Z0-9-]{1,40}", args.deploy_preview):
        parser.error("Preview channel must contain only letters, numbers, or hyphens")
    selected = "live" if args.deploy_live else f"preview:{args.deploy_preview}" if args.deploy_preview else "local"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        with exclusive_project_lock(ROOT):
            if args.initialize_csv_baseline:
                initialize_csv_baseline(ROOT, args.sources, args.initialize_csv_baseline)
                return 0
            if not args.watch:
                if args.scan_only:
                    scan_competitors(ROOT, args.sources)
                elif args.prices_only:
                    run_cycle(ROOT, args.sources, selected, prices_only=True)
                else:
                    run_cycle(ROOT, args.sources, selected, args.allow_increase_reason)
                return 0
            LOGGER.info("Checking immediately and then at the configured interval. Target: %s",
                        "scan-only" if args.scan_only else selected)
            if not args.scan_only and not args.prices_only and load_settings(args.sources).get("buyer_results", {}).get("type") in ("csv_firestore", "json_firestore"):
                LOGGER.info("Checking for buyer results changes every configured buyer_poll_seconds")
                watch_csv(ROOT, args.sources, selected)
                return 0
            while True:
                started = time.monotonic()
                try:
                    if args.scan_only:
                        scan_competitors(ROOT, args.sources)
                    elif args.prices_only:
                        run_cycle(ROOT, args.sources, selected, prices_only=True)
                    else:
                        run_cycle(ROOT, args.sources, selected)
                except Exception as error:
                    LOGGER.error("Update skipped: %s", error)
                try:
                    interval = load_settings(args.sources)["interval_seconds"]
                except (OSError, ValueError):
                    interval = 1800
                time.sleep(max(0, interval - (time.monotonic() - started)))
    except KeyboardInterrupt:
        LOGGER.info("Automation stopped")
        return 0
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        LOGGER.error("Update skipped: %s", error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
