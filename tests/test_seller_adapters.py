"""Verify live seller feed contracts and the complete local pricing path."""

import copy
import json
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import automation.run as runner
from automation.run import run_cycle, scan_competitors
from automation.seller_adapters import (
    parse_pocket_soldier, parse_toy_soldier_stand, parse_troopers,
)
from pricing_engine import DualMarketPricingEngine, ROOT, exclusive_project_lock


POCKET_SOURCE = {"seller_id": "Pocket Soldier", "offer_ids": {"SKU_GREEN": "PP-SINGLE-001"}}
TROOPERS_SOURCE = {"seller_id": "Frieds Fort Troopers", "offer_ids": {
    "SKU_GREEN": "basic-1", "SKU_SILVER": "personalized-1", "SKU_GOLD": "limited-1"}}
STAND_SOURCE = {"seller_id": "Toy Soldier Stand", "offer_ids": {"SKU_GREEN": "GREEN-1"}}

POCKET = {"seller_id": "pocket-platoon", "offers": [{
    "offer_id": "PP-SINGLE-001", "product_id": "standard-soldier",
    "quantities": {"standard-soldier": 1}, "total_price": 0.5, "currency": "USD",
    "eligibility_terms": "Open to all customers. No scheduled expiry.",
    "effective_from": "2025-01-01T00:00:00Z", "expires_at": None,
    "simulated_available_quantity": None}]}
TROOPERS = {"price_snapshot_id": "price_snapshot-001", "products": [
    {"id": "basic-1", "quantity": 1, "price": 1.5, "components": {"plain": 1}},
    {"id": "personalized-1", "quantity": 1, "price": 2, "components": {"graphic": 1}},
    {"id": "limited-1", "quantity": 1, "price": 5, "components": {"limited": 1}},
    {"id": "plain-3", "quantity": 3, "price": 3.5, "components": {"plain": 3}}]}
STAND = {"format": "toy-soldier-price-file/1", "seller": "Toy Soldier Stand", "currency": "USD",
         "products": [{"product_id": "green", "simulated_available": 0, "physical_available": 20}],
         "offers": [{"offer_id": "GREEN-1", "items": [{"product_id": "green", "quantity": 1}],
                     "total_price": 0.75, "eligibility": "all", "markets": ["physical", "simulated"],
                     "effective_from": "2025-01-01T00:00:00Z", "effective_until": None}]}
UNLIMITED = "<html><p>Unlimited supply for all available products.</p></html>"


class SellerAdapterTests(unittest.TestCase):
    def test_stock_distinguishes_unknown_zero_and_seller_claim(self):
        pocket = parse_pocket_soldier(POCKET, POCKET_SOURCE)[0]
        stand = parse_toy_soldier_stand(STAND, STAND_SOURCE)[0]
        troopers = parse_troopers(TROOPERS, UNLIMITED, TROOPERS_SOURCE)[0]
        self.assertEqual((pocket.virtual_available, pocket.availability_status), (None, "unknown"))
        self.assertEqual((stand.virtual_available, stand.availability_status), (0, "known"))
        self.assertEqual((troopers.virtual_available, troopers.availability_status),
                         (None, "seller_claimed_unlimited"))
        engine = DualMarketPricingEngine()
        self.assertEqual(engine.get_effective_competitor_min_price("SKU_GREEN", [pocket, stand, troopers]), 1.5)
        self.assertIsNone(engine.get_effective_competitor_min_price("SKU_GREEN", [pocket, stand]))

    def test_troopers_requires_visible_current_unlimited_claim(self):
        html = "<script>Unlimited supply for all available products.</script><p>Contact us.</p>"
        offers = parse_troopers(TROOPERS, html, TROOPERS_SOURCE)
        self.assertTrue(all(offer.availability_status == "unknown" for offer in offers))
        self.assertIsNone(DualMarketPricingEngine().get_effective_competitor_min_price("SKU_GREEN", offers))

    def test_restricted_bundled_future_and_expired_offers_fail(self):
        doc = copy.deepcopy(POCKET)
        doc["offers"][0]["requires_seller_code"] = True
        with self.assertRaisesRegex(ValueError, "unrestricted"):
            parse_pocket_soldier(doc, POCKET_SOURCE)
        doc = copy.deepcopy(POCKET)
        doc["offers"][0]["quantities"]["standard-soldier"] = 2
        with self.assertRaisesRegex(ValueError, "one standard"):
            parse_pocket_soldier(doc, POCKET_SOURCE)
        doc = copy.deepcopy(POCKET)
        doc["offers"][0]["effective_from"] = "2099-01-01T00:00:00Z"
        with self.assertRaisesRegex(ValueError, "not yet effective"):
            parse_pocket_soldier(doc, POCKET_SOURCE)
        doc = copy.deepcopy(STAND)
        doc["offers"][0]["effective_until"] = "2025-01-02T00:00:00Z"
        with self.assertRaisesRegex(ValueError, "expired"):
            parse_toy_soldier_stand(doc, STAND_SOURCE)
        doc = copy.deepcopy(TROOPERS)
        doc["products"][0]["components"]["plain"] = 2
        with self.assertRaisesRegex(ValueError, "single-soldier"):
            parse_troopers(doc, UNLIMITED, TROOPERS_SOURCE)

    def test_missing_or_invalid_data_does_not_become_a_price(self):
        doc = copy.deepcopy(STAND)
        doc["products"][0]["simulated_available"] = -1
        with self.assertRaisesRegex(ValueError, "stock"):
            parse_toy_soldier_stand(doc, STAND_SOURCE)
        doc = copy.deepcopy(TROOPERS)
        doc["products"][0]["price"] = True
        with self.assertRaisesRegex(ValueError, "price"):
            parse_troopers(doc, UNLIMITED, TROOPERS_SOURCE)
        doc["products"] = doc["products"][1:]
        with self.assertRaisesRegex(ValueError, "missing"):
            parse_troopers(doc, UNLIMITED, TROOPERS_SOURCE)


class Handler(BaseHTTPRequestHandler):
    routes = {}

    def do_GET(self):
        if self.path not in self.routes:
            self.send_error(404)
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(self.routes[self.path])

    def log_message(self, *args):
        pass


class SellerPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for folder in ("data", "config", "public"):
            (self.root / folder).mkdir()
        shutil.copy(ROOT / "market_config.json", self.root / "market_config.json")
        (self.root / "public/published_price_file.json").write_text("[]")
        (self.root / "public/decision_audit_log.json").write_text("[]")
        Handler.routes = {"/pocket": json.dumps(POCKET).encode(),
                          "/troopers": json.dumps(TROOPERS).encode(),
                          "/troopers-prices": UNLIMITED.encode(),
                          "/stand": json.dumps(STAND).encode()}
        host = f"http://127.0.0.1:{self.server.server_port}"
        settings = {"interval_seconds": 1800, "request_timeout_seconds": 5,
                    "buyer_results": {"type": "file", "path": "data/buyer_results.json"},
                    "competitors": [
                        {**POCKET_SOURCE, "format": "pocket_soldier", "url": host + "/pocket"},
                        {**TROOPERS_SOURCE, "format": "troopers_catalog", "url": host + "/troopers",
                         "availability_url": host + "/troopers-prices"},
                        {**STAND_SOURCE, "format": "toy_soldier_stand", "url": host + "/stand"}]}
        (self.root / "config/automation.json").write_text(json.dumps(settings))

    def tearDown(self):
        self.temp.cleanup()

    def test_scan_needs_no_buyer_and_never_changes_public_catalog(self):
        with exclusive_project_lock(self.root):
            scan_competitors(self.root)
        snapshot = json.loads((self.root / "data/competitor_snapshot.json").read_text())
        self.assertEqual(len(snapshot["competitor_sources"]), 3)
        self.assertEqual(len(snapshot["competitor_offers"]), 5)
        self.assertEqual((self.root / "public/published_price_file.json").read_text(), "[]")
        self.assertFalse((self.root / "data/automation_state.json").exists())

    def test_scan_watcher_runs_immediately_and_waits_for_the_next_interval(self):
        with patch.object(runner, "ROOT", self.root), patch.object(runner, "scan_competitors") as scan, \
                patch.object(runner.time, "monotonic", side_effect=[1000, 1005]), \
                patch.object(runner.time, "sleep", side_effect=KeyboardInterrupt) as sleep:
            status = runner.main(["--sources", str(self.root / "config/automation.json"),
                                  "--scan-only", "--watch"])
        self.assertEqual(status, 0)
        scan.assert_called_once_with(self.root, self.root / "config/automation.json")
        sleep.assert_called_once_with(1795)

    def test_buyer_report_and_real_feed_shapes_drive_a_local_cycle(self):
        report = {"report_id": "round4-test", "seller_id": "Vanguard_Garrison", "round": 4,
                  "reported_at": "2026-10-29T08:00:00Z", "transactions": [],
                  "remaining_virtual": {"SKU_GREEN": 27, "SKU_SILVER": 15, "SKU_GOLD": 7}}
        (self.root / "data/buyer_results.json").write_text(json.dumps(report))
        with exclusive_project_lock(self.root):
            run_cycle(self.root)
        catalog = json.loads((self.root / "public/published_price_file.json").read_text())
        audit = json.loads((self.root / "data/decision_audit_log.json").read_text())[-1]
        green = next(row for row in catalog if row["offer_id"] == "BASE_SKU_GREEN")
        self.assertEqual((green["total_price"], green["virtual_available"]), (1, 27))
        self.assertEqual(len(audit["competitor_snapshot"]), 5)
        self.assertIn("availability_sha256", audit["competitor_sources"][1])


if __name__ == "__main__":
    unittest.main()
