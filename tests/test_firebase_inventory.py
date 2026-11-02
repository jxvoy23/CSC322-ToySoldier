"""Buyer CSV validation and atomic, idempotent Firebase stock updates."""

import copy
import csv
import io
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import automation.run as runner
from automation.firebase_inventory import (
    apply_csv_transaction, inventory_from_state, load_csv, load_buyer_input,
    parse_buyer_json, parse_csv,
)
from automation.prepare_live import prepare
from automation.run import run_cycle, watch_csv
from pricing_engine import ROOT

HEADER = ["Event_ID", "ISO_Timestamp", "Selected_Seller", "Offer_ID", "Units_Bought", "Status"]


def make_csv(rows, headers=HEADER):
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=headers)
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue().encode()


def row(event_id="evt-1", offer="BASE_SKU_SILVER", quantity="2", seller="Vanguard Garrison"):
    return {"Event_ID": event_id, "ISO_Timestamp": "2026-10-29T12:00:00Z",
            "Selected_Seller": seller, "Offer_ID": offer, "Units_Bought": quantity, "Status": "PURCHASED"}


def current_state():
    return {
        "BASE_SKU_GREEN": {"virtual": 27, "physical": 24, "price": 2.0},
        "BASE_SKU_SILVER": {"virtual": 15, "physical": 14, "price": 3.0},
        "PRIME_SKU_SILVER": {"virtual": 15, "physical": 14, "price": 2.0},
        "BASE_SKU_GOLD": {"virtual": 7, "physical": 6, "price": 5.0},
        "PRIME_SKU_GOLD": {"virtual": 7, "physical": 6, "price": 4.0},
        "OFFER_CHARITY_MYSTERY_POUCH": {"virtual": 0, "physical": 12, "price": 3.0},
        "last_updated": datetime(2026, 10, 29, 4, 25, tzinfo=timezone.utc),
    }


class Snapshot:
    def __init__(self, value):
        self.value = copy.deepcopy(value)
        self.exists = value is not None

    def to_dict(self):
        return copy.deepcopy(self.value)


class Ref:
    def __init__(self, client, name):
        self.client, self.name = client, name

    def get(self, transaction=None):
        return Snapshot(self.client.docs.get(self.name))


class Collection:
    def __init__(self, client, name):
        self.client, self.name = client, name

    def document(self, name):
        return Ref(self.client, self.name + "/" + name)


class Transaction:
    def __init__(self, client):
        self.client, self.writes = client, []

    def create(self, ref, data):
        self.writes.append(("create", ref.name, copy.deepcopy(data)))

    def update(self, ref, data):
        self.writes.append(("update", ref.name, copy.deepcopy(data)))

    def commit(self):
        docs = copy.deepcopy(self.client.docs)
        for operation, name, data in self.writes:
            if operation == "create":
                if name in docs:
                    raise ValueError("duplicate document")
                docs[name] = data
            else:
                for key, value in data.items():
                    target = docs[name]
                    pieces = key.split(".")
                    for piece in pieces[:-1]:
                        target = target[piece]
                    target[pieces[-1]] = value
        self.client.docs = docs


class FakeClient:
    def __init__(self):
        self.docs = {"vanguard_live/state": current_state()}

    def collection(self, name):
        return Collection(self, name)

    def transaction(self):
        return Transaction(self)


class FakeFirestore:
    @staticmethod
    def transactional(fn):
        def execute(transaction):
            result = fn(transaction)
            transaction.commit()
            return result
        return execute


class BuyerCSVTests(unittest.TestCase):
    def test_default_source_is_the_buyer_json_file(self):
        import json
        settings = json.loads((ROOT / "config/automation.json").read_text())
        self.assertEqual(settings["buyer_results"], {"type": "json_firestore",
            "json": {"type": "file", "path": "data/buyer_results.json"}})

    def test_existing_buyer_export_is_rejected_when_quantity_and_id_are_absent(self):
        original = b'Tx_ID,Trigger,Timestamp,Trading_Window_2Hr,Buyer_ID,Persona,Selected_Seller,Offer_ID,Price_Paid,Remaining_Stock,Status\n'
        with self.assertRaisesRegex(ValueError, "Event_ID.*ISO_Timestamp.*Units_Bought"):
            parse_csv(original, ["Vanguard_Garrison"])

    def test_valid_csv_selects_our_sales_and_does_not_export_profiles(self):
        events = parse_csv(make_csv([row(), row("other", seller="Other Seller")]), ["Vanguard_Garrison"])
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0]["sku_id"], events[0]["quantity"]), ("SKU_SILVER", 2))
        self.assertNotIn("buyer", str(events).lower())

    def test_bad_quantity_ids_and_physical_only_offer_fail(self):
        for changes in [{"Units_Bought": ""}, {"Units_Bought": "1.5"},
                        {"Event_ID": ""}, {"Offer_ID": "OFFER_CHARITY_MYSTERY_POUCH"}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                parse_csv(make_csv([{**row(), **changes}]), ["Vanguard_Garrison"])
        with self.assertRaisesRegex(ValueError, "reuses an Event_ID"):
            parse_csv(make_csv([row(), row(quantity="1")]), ["Vanguard_Garrison"])

    def test_file_in_public_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "outside public"):
                load_csv({"csv": {"type": "file", "path": "public/results.csv"}}, Path(tmp), 5)

    def test_buyer_page_json_uses_iso_timestamp_quantity_and_no_profiles(self):
        import json
        document = {"collections": {"buyers": [{"valuation": "private"}], "ledgers": [{
            "_id": "ledger-1", "timestamp": "05:00 AM (Day 1 Run)",
            "isoTimestamp": "2026-10-29T12:00:00Z", "sellerName": "Vanguard Garrison",
            "offerId": "BASE_SKU_SILVER", "unitsBought": 2, "status": "PURCHASED",
            "buyerValuation": 50}]}}
        events = parse_buyer_json(json.dumps(document).encode(), ["Vanguard_Garrison"])
        self.assertEqual(events[0]["event_id"], "ledger-1")
        self.assertEqual((events[0]["quantity"], events[0]["timestamp"]), (2, "2026-10-29T12:00:00Z"))
        self.assertNotIn("Valuation", str(events))

    def test_filtered_json_validates_missing_quantity_and_unknown_format(self):
        import json
        event = {"event_id": "official-1", "timestamp": "2026-10-29T12:00:00Z",
                 "seller_id": "Vanguard_Garrison", "offer_id": "BASE_SKU_GREEN",
                 "quantity": 1, "status": "PURCHASED"}
        self.assertEqual(len(parse_buyer_json(json.dumps({"transactions": [event]}).encode(), ["Vanguard_Garrison"])), 1)
        del event["quantity"]
        with self.assertRaisesRegex(ValueError, "Units_Bought"):
            parse_buyer_json(json.dumps({"transactions": [event]}).encode(), ["Vanguard_Garrison"])
        with self.assertRaisesRegex(ValueError, "must contain"):
            parse_buyer_json(b'{"buyers": []}', ["Vanguard_Garrison"])

    def test_authenticated_buyer_feed_requires_https_and_environment_token(self):
        source = {"type": "json_firestore", "json": {"type": "url", "url": "http://example.com/results",
                                                         "bearer_token_env": "BUYER_FEED_TEST_TOKEN"}}
        with patch.dict("os.environ", {"BUYER_FEED_TEST_TOKEN": "test-token"}):
            with self.assertRaisesRegex(ValueError, "HTTPS"):
                load_buyer_input(source, ROOT, 5)
        source["json"]["url"] = "https://example.com/results"
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(ValueError, "missing"):
                load_buyer_input(source, ROOT, 5)

    def test_bearer_token_is_sent_only_to_original_https_origin(self):
        import urllib.request
        from unittest.mock import MagicMock
        source = {"type": "json_firestore", "json": {"type": "url", "url": "https://example.com/results",
                                                         "bearer_token_env": "BUYER_FEED_TEST_TOKEN"}}
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"transactions": []}'
        opener = MagicMock()
        opener.open.return_value = response
        with patch.dict("os.environ", {"BUYER_FEED_TEST_TOKEN": "test-token"}), \
                patch("automation.firebase_inventory.urllib.request.build_opener", return_value=opener) as build:
            result = load_buyer_input(source, ROOT, 5)
        self.assertEqual(result, b'{"transactions": []}')
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer test-token")
        handler = build.call_args.args[0]
        with self.assertRaisesRegex(ValueError, "different origin"):
            handler.redirect_request(urllib.request.Request("https://example.com/results"),
                                     None, 302, "Found", {}, "https://other.example/results")


class FirestoreInventoryTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()

    def events(self, *rows):
        return parse_csv(make_csv(list(rows)), ["Vanguard_Garrison"])

    def test_baseline_required_and_new_sales_decrement_once(self):
        events = self.events(row(), row("evt-2", "PRIME_SKU_SILVER", "1"))
        with self.assertRaisesRegex(ValueError, "baseline"):
            apply_csv_transaction(self.client, FakeFirestore, events)
        apply_csv_transaction(self.client, FakeFirestore, [], initialize="empty")
        state, imported = apply_csv_transaction(self.client, FakeFirestore, events)
        self.assertEqual(imported, 2)
        self.assertEqual(state["BASE_SKU_SILVER"]["virtual"], 12)
        self.assertEqual(state["PRIME_SKU_SILVER"]["virtual"], 12)
        self.assertEqual(state["BASE_SKU_SILVER"]["physical"], 14)
        self.assertEqual(state["BASE_SKU_GREEN"]["virtual"], 27)
        again, imported = apply_csv_transaction(self.client, FakeFirestore, events)
        self.assertEqual((again["BASE_SKU_SILVER"]["virtual"], imported), (12, 0))

    def test_existing_baseline_never_deducts_old_sales(self):
        events = self.events(row())
        state, imported = apply_csv_transaction(self.client, FakeFirestore, events, initialize="existing")
        self.assertEqual((state["BASE_SKU_SILVER"]["virtual"], imported), (15, 0))
        state, imported = apply_csv_transaction(self.client, FakeFirestore, events)
        self.assertEqual((state["BASE_SKU_SILVER"]["virtual"], imported), (15, 0))

    def test_duplicate_conflict_and_negative_stock_never_partially_commit(self):
        apply_csv_transaction(self.client, FakeFirestore, [], initialize="empty")
        events = self.events(row())
        apply_csv_transaction(self.client, FakeFirestore, events)
        before = copy.deepcopy(self.client.docs)
        with self.assertRaisesRegex(ValueError, "changed"):
            apply_csv_transaction(self.client, FakeFirestore, self.events(row(quantity="1")))
        self.assertEqual(self.client.docs, before)
        with self.assertRaisesRegex(ValueError, "negative"):
            apply_csv_transaction(self.client, FakeFirestore, self.events(row("new", quantity="99")))
        self.assertEqual(self.client.docs, before)

    def test_inconsistent_prime_stock_is_rejected(self):
        state = current_state()
        state["PRIME_SKU_SILVER"]["virtual"] = 99
        with self.assertRaisesRegex(ValueError, "do not agree"):
            inventory_from_state(state)

    def test_prepare_uses_firebase_values_without_writing_remote_state(self):
        import shutil
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "public").mkdir()
            for name in ("market_config.json", "public/published_price_file.json", "public/decision_audit_log.json"):
                shutil.copy(ROOT / name, root / name)
            before = copy.deepcopy(self.client.docs)
            with patch("automation.prepare_live.public_firebase_state", return_value=current_state()):
                prepare(root)
            import json
            catalog = json.loads((root / "public/published_price_file.json").read_text())
            green = next(x for x in catalog if x["offer_id"] == "BASE_SKU_GREEN")
            self.assertEqual((green["virtual_available"], green["physical_available"]), (27, 24))
            self.assertEqual(green["timestamp"], "2026-10-29T04:25:00Z")
            self.assertEqual(self.client.docs, before)

    def test_runner_reads_csv_uses_firebase_stock_and_does_not_deduct_twice(self):
        import json
        import shutil
        apply_csv_transaction(self.client, FakeFirestore, [], initialize="empty")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for folder in ("config", "data", "public"):
                (root / folder).mkdir()
            shutil.copy(ROOT / "market_config.json", root / "market_config.json")
            (root / "public/published_price_file.json").write_text("[]")
            (root / "public/decision_audit_log.json").write_text("[]")
            settings = {"interval_seconds": 1800, "request_timeout_seconds": 5,
                        "buyer_results": {"type": "csv_firestore",
                                          "csv": {"type": "file", "path": "data/buyer_results.csv"}},
                        "competitors": []}
            (root / "config/automation.json").write_text(json.dumps(settings))
            (root / "data/buyer_results.csv").write_bytes(make_csv([row()]))
            with patch("automation.firebase_inventory.firebase_client", return_value=(self.client, FakeFirestore)), \
                    patch("automation.run.collect_competitors", return_value=([], [])):
                run_cycle(root)
                run_cycle(root)
            catalog = json.loads((root / "public/published_price_file.json").read_text())
            silver = next(x for x in catalog if x["offer_id"] == "BASE_SKU_SILVER")
            self.assertEqual((silver["virtual_available"], silver["physical_available"]), (13, 14))
            self.assertEqual(self.client.docs["vanguard_live/state"]["BASE_SKU_SILVER"]["virtual"], 13)

    def test_runner_imports_filtered_json_results_once(self):
        import json
        import shutil
        apply_csv_transaction(self.client, FakeFirestore, [], initialize="empty")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for folder in ("config", "data", "public"):
                (root / folder).mkdir()
            shutil.copy(ROOT / "market_config.json", root / "market_config.json")
            (root / "public/published_price_file.json").write_text("[]")
            (root / "public/decision_audit_log.json").write_text("[]")
            settings = {"interval_seconds": 1800, "request_timeout_seconds": 5,
                        "buyer_results": {"type": "json_firestore",
                                          "json": {"type": "file", "path": "data/buyer_results.json"}},
                        "competitors": []}
            (root / "config/automation.json").write_text(json.dumps(settings))
            ledger = {"transactions": [{"event_id": "event-1", "timestamp": "2026-10-29T12:00:00Z",
                      "seller_id": "Vanguard_Garrison", "offer_id": "BASE_SKU_GREEN",
                      "quantity": 2, "status": "PURCHASED"}]}
            (root / "data/buyer_results.json").write_text(json.dumps(ledger))
            with patch("automation.firebase_inventory.firebase_client", return_value=(self.client, FakeFirestore)), \
                    patch("automation.run.collect_competitors", return_value=([], [])):
                run_cycle(root)
                run_cycle(root)
            catalog = json.loads((root / "public/published_price_file.json").read_text())
            green = next(x for x in catalog if x["offer_id"] == "BASE_SKU_GREEN")
            self.assertEqual((green["virtual_available"], green["physical_available"]), (25, 24))

    def test_retry_discards_catalog_if_firebase_stock_changed(self):
        import json
        import shutil
        apply_csv_transaction(self.client, FakeFirestore, [], initialize="empty")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for folder in ("config", "data", "public"):
                (root / folder).mkdir()
            shutil.copy(ROOT / "market_config.json", root / "market_config.json")
            (root / "public/published_price_file.json").write_text("[]")
            (root / "public/decision_audit_log.json").write_text("[]")
            settings = {"interval_seconds": 1800, "request_timeout_seconds": 5,
                        "buyer_results": {"type": "csv_firestore",
                                          "csv": {"type": "file", "path": "data/buyer_results.csv"}},
                        "competitors": []}
            (root / "config/automation.json").write_text(json.dumps(settings))
            (root / "data/buyer_results.csv").write_bytes(make_csv([]))
            with patch("automation.firebase_inventory.firebase_client", return_value=(self.client, FakeFirestore)), \
                    patch("automation.run.collect_competitors", return_value=([], [])), \
                    patch("automation.run.deploy", side_effect=RuntimeError("offline")) as deploy:
                with self.assertRaisesRegex(RuntimeError, "offline"):
                    run_cycle(root, target="preview:test")
                self.client.docs["vanguard_live/state"]["BASE_SKU_GREEN"]["virtual"] = 26
                with self.assertRaisesRegex(RuntimeError, "stale catalog discarded"):
                    run_cycle(root, target="preview:test")
                self.assertEqual(deploy.call_count, 1)
            state = json.loads((root / "data/automation_state.json").read_text())
            self.assertIsNone(state["pending_publication"])

    def test_prices_only_uses_firebase_and_labels_stock_without_reducing_it(self):
        import json
        import shutil
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for folder in ("config", "data", "public"):
                (root / folder).mkdir()
            shutil.copy(ROOT / "market_config.json", root / "market_config.json")
            (root / "public/published_price_file.json").write_text("[]")
            (root / "public/decision_audit_log.json").write_text("[]")
            settings = {"interval_seconds": 1800, "request_timeout_seconds": 5,
                        "buyer_results": {"type": "json_firestore",
                                          "json": {"type": "file", "path": "data/buyer_results.json"}},
                        "competitors": []}
            (root / "config/automation.json").write_text(json.dumps(settings))
            before = copy.deepcopy(self.client.docs)
            with patch("automation.firebase_inventory.firebase_client", return_value=(self.client, FakeFirestore)), \
                    patch("automation.run.collect_competitors", return_value=([], [])):
                run_cycle(root, prices_only=True)
            catalog = json.loads((root / "public/published_price_file.json").read_text())
            green = next(x for x in catalog if x["offer_id"] == "BASE_SKU_GREEN")
            self.assertEqual((green["virtual_available"], green["physical_available"]), (27, 24))
            self.assertTrue(all(x["simulated_stock_status"] == "not_synchronized" for x in catalog))
            self.assertEqual(self.client.docs, before)

    def test_full_watcher_stops_when_buyer_file_is_missing(self):
        import json
        import shutil
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for folder in ("config", "data", "public"):
                (root / folder).mkdir()
            shutil.copy(ROOT / "market_config.json", root / "market_config.json")
            (root / "public/published_price_file.json").write_text("[]")
            settings = {"interval_seconds": 1800, "request_timeout_seconds": 5,
                        "buyer_results": {"type": "json_firestore",
                                          "json": {"type": "file", "path": "data/buyer_results.json"}},
                        "competitors": []}
            (root / "config/automation.json").write_text(json.dumps(settings))
            with patch("automation.run.collect_competitors", return_value=([], [])), \
                    patch("automation.firebase_inventory.firebase_client") as client:
                with self.assertRaisesRegex(ValueError, "Buyer results have not arrived"):
                    run_cycle(root)
                client.assert_not_called()
            self.assertEqual((root / "public/published_price_file.json").read_text(), "[]")

    def test_prices_only_watcher_runs_immediately_then_waits_30_minutes(self):
        import json
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "settings.json"
            path.write_text(json.dumps({"interval_seconds": 1800, "request_timeout_seconds": 5,
                "buyer_results": {"type": "json_firestore", "json": {"type": "file", "path": "buyer.json"}}}))
            with patch.object(runner, "ROOT", root), patch.object(runner, "run_cycle") as cycle, \
                    patch.object(runner.time, "monotonic", side_effect=[1000, 1005]), \
                    patch.object(runner.time, "sleep", side_effect=KeyboardInterrupt) as sleep:
                status = runner.main(["--sources", str(path), "--prices-only", "--watch", "--deploy-live"])
            self.assertEqual(status, 0)
            cycle.assert_called_once_with(root, path, "live", prices_only=True)
            sleep.assert_called_once_with(1795)

    def test_watcher_reprices_on_csv_change_before_30_minute_seller_scan(self):
        import json
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            settings = {"interval_seconds": 1800, "buyer_poll_seconds": 60, "request_timeout_seconds": 5,
                        "buyer_results": {"type": "csv_firestore", "csv": {"type": "file", "path": "sales.csv"}}}
            path = root / "settings.json"
            path.write_text(json.dumps(settings))
            with patch("automation.run.csv_signature", side_effect=["first", "second"]), \
                    patch("automation.run.run_cycle", return_value="completed") as cycle, \
                    patch("automation.run.time.monotonic", side_effect=[1000, 1005, 1060, 1063]), \
                    patch("automation.run.time.sleep", side_effect=[None, KeyboardInterrupt]) as sleep:
                with self.assertRaises(KeyboardInterrupt):
                    watch_csv(root, path, "local")
            self.assertEqual(cycle.call_count, 2)
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [55, 57])


if __name__ == "__main__":
    unittest.main()
