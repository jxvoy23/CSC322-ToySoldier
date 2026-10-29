"""Watch official simulated trades and publish coalesced catalog updates.

Run one listener per checkout. --deploy explicitly enables serial Hosting deploys.
Imports do not initialize credentials, connect to Firestore, or publish anything.
"""

import argparse
import logging
import queue
import subprocess
import time
from dataclasses import asdict
from pathlib import Path

from buyer_ping_client import parse_timestamp, validate_trade
from pricing_engine import (
    ROOT, SKU_IDS, CompetitorOffer, atomic_write_json, count,
    export_price_file_and_audit_log, load_market_config, read_json, exclusive_project_lock,
    canonical_seller,
)

LOGGER = logging.getLogger(__name__)


class MarketState:
    """A local stock mirror. The buyer's virtual ledger remains authoritative."""

    def __init__(self, config_path=ROOT / "market_config.json"):
        self.config_path = Path(config_path).resolve()
        self.root = self.config_path.parent
        self.checkpoint_path = self.root / ".market_state.json"
        self.config, self.skus, competitors, self.belief, self.engine = load_market_config(self.config_path)
        names = self.config.get("seller_aliases", []) + [self.config["seller_id"]]
        self.aliases = {canonical_seller(name) for name in names}
        self.competitors = {
            f"{canonical_seller(offer.seller_id)}:{offer.sku_id}": asdict(offer)
            for offer in competitors if canonical_seller(offer.seller_id) not in self.aliases
        }
        self.last_events = {}
        self.pending_publication = None
        saved = read_json(self.checkpoint_path, {})
        if not isinstance(saved, dict):
            raise ValueError("The listener checkpoint must be a JSON object")
        if saved.get("round_number", 0) > self.config["round_number"]:
            raise ValueError("The configured round is older than the saved listener state")
        for sku, remaining in saved.get("virtual_inventory", {}).items():
            if sku in self.skus:
                # A notification or a restart cannot replenish simulated stock.
                self.skus[sku].inventory_virtual = min(
                    self.skus[sku].inventory_virtual, count(remaining, "checkpoint stock"))
        for key, offer in saved.get("competitors", {}).items():
            if key not in self.competitors:
                self.competitors[key] = offer
            else:
                self.competitors[key]["virtual_available"] = min(
                    self.competitors[key]["virtual_available"], offer["virtual_available"])
        self.last_events = saved.get("last_events", {})
        self.pending_publication = saved.get("pending_publication")
        if self.pending_publication and saved.get("round_number") != self.config["round_number"]:
            raise ValueError("Finish the pending publication before changing the configured round")

    def save(self):
        atomic_write_json(self.checkpoint_path, {
            "version": 1, "round_number": self.config["round_number"],
            "virtual_inventory": {key: sku.inventory_virtual for key, sku in self.skus.items()},
            "competitors": self.competitors, "last_events": self.last_events,
            "pending_publication": self.pending_publication,
        })

    def apply_trade(self, trade):
        validate_trade(trade)
        if trade["round"] != self.config["round_number"]:
            return False
        seller = canonical_seller(trade["seller_id"])
        is_ours = seller in self.aliases
        sku = trade["sku_id"]
        key = f"{'self' if is_ours else seller}:{sku}"
        occurred = parse_timestamp(trade["timestamp"])
        remaining = trade["remaining_virtual"]
        previous = self.last_events.get(key)
        if previous:
            previous_time = parse_timestamp(previous["timestamp"])
            if occurred < previous_time or (occurred == previous_time and remaining >= previous["remaining"]):
                return False
        competitor_key = f"{seller}:{sku}"
        old = self.competitors.get(competitor_key)
        current = self.skus[sku].inventory_virtual if is_ours else (
            old["virtual_available"] if old is not None else None)
        if current is not None and remaining > current:
            LOGGER.warning("Ignored a sale notification that would increase virtual stock")
            return False
        if is_ours:
            self.skus[sku].inventory_virtual = remaining
        else:
            # A loyalty sale price is not a competitor's standard published price.
            price = old.get("unit_price") if old else None
            if trade["offer_id"] == f"BASE_{sku}":
                price = trade["price_paid"]
            self.competitors[competitor_key] = {
                "seller_id": trade["seller_id"], "sku_id": sku,
                "unit_price": price, "virtual_available": remaining,
            }
        self.last_events[key] = {"timestamp": occurred.isoformat(), "remaining": remaining}
        return True

    def prepare_publication(self):
        competitors = [CompetitorOffer(**value) for value in self.competitors.values()
                       if value.get("unit_price") is not None and value["sku_id"] in SKU_IDS]
        offers, audit = self.engine.build_daily_catalog(
            self.skus, competitors, self.belief, self.config["rounds_remaining"],
            self.config["physical_pouch_count"], self.config["round_number"],
            self.config.get("strategy_mode", "dynamic_response"),
            self.config.get("manual_overrides", {}))
        self.pending_publication = {
            "offers": [asdict(offer) for offer in offers], "audit": asdict(audit)}
        # Save the publication alongside the event cursor BEFORE external writes.
        # A failed write or process restart can replay this same publication safely.
        self.save()

    def publish_pending(self, database, deployer=None):
        if self.pending_publication is None:
            return
        pending = self.pending_publication
        export_price_file_and_audit_log(
            pending["offers"], pending["audit"],
            self.root / "public/published_price_file.json", self.root / "decision_audit_log.json",
            public_history_path=self.root / "public/decision_audit_log.json")
        database.collection("vanguard_live").document("state").set({
            "offers": pending["offers"], "catalog_timestamp": pending["audit"]["timestamp"],
            "decision_id": pending["audit"]["decision_id"],
        })
        if deployer is not None:
            deployer()
        self.pending_publication = None
        try:
            self.save()
        except Exception:
            self.pending_publication = pending
            raise
        LOGGER.info("Published %s offers%s", len(pending["offers"]),
                    " and deployed Hosting" if deployer else " locally; Hosting deployment disabled")


def deploy_hosting(root, project_id):
    """Synchronous and checked: deployments cannot overlap within the listener."""
    result = subprocess.run(
        ["firebase", "deploy", "--only", "hosting", "--project", project_id, "--non-interactive"],
        cwd=root, capture_output=True, text=True, timeout=180, check=False)
    if result.returncode:
        # CLI output is useful to the operator; credentials are never supplied as arguments.
        raise RuntimeError(f"Hosting deployment failed:\n{result.stderr[-2000:]}\n{result.stdout[-2000:]}")


def run_listener(state, database, field_filter, *, deploy=False, debounce_seconds=2.0):
    events = queue.Queue()

    def on_snapshot(_documents, changes, _read_time):
        # Firestore invokes this callback on a background thread. It does no I/O.
        for change in changes:
            if change.type.name == "ADDED":
                events.put(change.document.to_dict())

    watcher = database.collection("market_feed").where(
        filter=field_filter("round", "==", state.config["round_number"])).on_snapshot(on_snapshot)
    deployer = (lambda: deploy_hosting(state.root, state.config["project_id"])) if deploy else None
    retry_delay = 1.0
    try:
        if state.pending_publication is None:
            state.prepare_publication()
        while True:
            if state.pending_publication is not None:
                try:
                    state.publish_pending(database, deployer)
                    retry_delay = 1.0
                except Exception:
                    LOGGER.exception("Publication failed; the saved pending update will be retried")
                    time.sleep(retry_delay)
                    retry_delay = min(30.0, retry_delay * 2)
                    continue
            try:
                batch = [events.get(timeout=1.0)]
            except queue.Empty:
                continue
            deadline = time.monotonic() + debounce_seconds
            # A fixed deadline prevents a busy feed from postponing publication forever.
            while time.monotonic() < deadline:
                try:
                    batch.append(events.get(timeout=max(0.001, deadline - time.monotonic())))
                except queue.Empty:
                    break
            changed = False
            for trade in batch:
                try:
                    changed = state.apply_trade(trade) or changed
                except (ValueError, TypeError, KeyError):
                    LOGGER.warning("Ignored a malformed market event", exc_info=False)
            if changed:
                state.prepare_publication()
    finally:
        watcher.unsubscribe()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "market_config.json")
    parser.add_argument("--credentials", type=Path, help="Service-account JSON; otherwise use Application Default Credentials")
    parser.add_argument("--deploy", action="store_true", help="Enable actual Firebase Hosting deployment")
    parser.add_argument("--debounce-seconds", type=float, default=2.0)
    args = parser.parse_args()
    if not 0 <= args.debounce_seconds <= 30:
        parser.error("--debounce-seconds must be between 0 and 30")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        from google.cloud import firestore
        from google.cloud.firestore_v1.base_query import FieldFilter
    except ImportError as error:
        raise SystemExit("Install the listener dependency: python -m pip install -r requirements.txt") from error
    with exclusive_project_lock(args.config.resolve().parent):
        state = MarketState(args.config)
        credentials = args.credentials
        if credentials is None and (state.root / "serviceAccountKey.json").exists():
            credentials = state.root / "serviceAccountKey.json"
        database = (firestore.Client.from_service_account_json(str(credentials), project=state.config["project_id"])
                    if credentials else firestore.Client(project=state.config["project_id"]))
        try:
            LOGGER.info("Listening to simulated round %s. Hosting deployment: %s",
                        state.config["round_number"], "enabled" if args.deploy else "disabled")
            run_listener(state, database, FieldFilter, deploy=args.deploy,
                         debounce_seconds=args.debounce_seconds)
        except KeyboardInterrupt:
            LOGGER.info("Listener stopped; the checkpoint is retained")
        finally:
            database.close()


if __name__ == "__main__":
    main()
