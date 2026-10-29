"""Reprice from buyer results and competitor sites, once or every 30 minutes."""

import argparse
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
    export_price_file_and_audit_log, load_market_config, number, read_json,
)

LOGGER = logging.getLogger(__name__)


def load_settings(path):
    settings = read_json(path)
    if not isinstance(settings, dict):
        raise ValueError(f"Automation settings not found or not a JSON object: {path}")
    interval = count(settings.get("interval_seconds"), "interval_seconds", 60)
    timeout = number(settings.get("request_timeout_seconds"), "request_timeout_seconds", 1)
    if timeout > 120:
        raise ValueError("request_timeout_seconds must be at most 120")
    if interval < timeout:
        raise ValueError("The polling interval must be at least the request timeout")
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
        command = ["firebase", "deploy", "--only", "hosting"]
    elif target.startswith("preview:"):
        channel = target.split(":", 1)[1]
        command = ["firebase", "hosting:channel:deploy", channel]
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


def run_cycle(root=ROOT, settings_path=None, target="local", increase_reason=None):
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
        return
    settings = load_settings(settings_path)
    config, skus, _, belief, engine = load_market_config(root / "market_config.json")
    previous = state.get("buyer_report") or {"remaining_virtual": initial_stock(root, config, skus)}
    report = read_buyer_source(settings.get("buyer_results"), root, settings["request_timeout_seconds"])
    buyer = validate_buyer_report(report, config, previous, increase_reason)
    for sku in SKU_IDS:
        skus[sku].inventory_virtual = buyer["remaining_virtual"][sku]
    competitors, source_details = collect_competitors(
        settings.get("competitors"), config.get("seller_aliases", []) + [config["seller_id"]],
        settings["request_timeout_seconds"])
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
    state["pending_publication"] = {
        "target": target, "project_id": config["project_id"],
        "offers": [asdict(offer) for offer in offers], "audit": audit,
    }
    atomic_write_json(state_path, state)
    publish_pending(root, state, state_path, target)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=Path, default=ROOT / "config/automation.json",
                        help="Automation settings JSON (default: config/automation.json)")
    parser.add_argument("--watch", action="store_true", help="Run now and keep checking at the configured interval")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--deploy-preview", metavar="CHANNEL", help="Deploy each update to a Hosting preview channel")
    target.add_argument("--deploy-live", action="store_true", help="Deploy each update to the live Hosting site")
    parser.add_argument("--allow-increase-reason", metavar="REASON",
                        help="For one run, record an instructor-approved inventory correction")
    args = parser.parse_args(argv)
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
            if not args.watch:
                run_cycle(ROOT, args.sources, selected, args.allow_increase_reason)
                return 0
            LOGGER.info("Checking immediately and then at the configured interval. Target: %s", selected)
            while True:
                started = time.monotonic()
                try:
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
