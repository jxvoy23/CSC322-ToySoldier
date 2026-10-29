"""Offline integration tests for buyer results, competitor scraping, and publishing."""

import json
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import automation.run as runner
from automation.run import run_cycle
from automation.sources import parse_html, parse_price_file
from pricing_engine import ROOT, exclusive_project_lock


OFFERS = [
    {"offer_id": "OTHER_GREEN", "product_quantities": {"OTHER_GREEN_SKU": 1},
     "total_price": 1, "virtual_available": 9, "eligibility": "ALL_CUSTOMERS", "timestamp": "2025-01-01T00:00:00Z"},
    {"offer_id": "OTHER_SILVER", "product_quantities": {"OTHER_SILVER_SKU": 1},
     "total_price": 2, "virtual_available": 9, "eligibility": "ALL_CUSTOMERS", "timestamp": "2025-01-01T00:00:00Z"},
]


class Handler(BaseHTTPRequestHandler):
    routes = {}

    def do_GET(self):
        body = self.routes.get(self.path)
        if body is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class AutomationTests(unittest.TestCase):
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
        (self.root / "data").mkdir()
        (self.root / "config").mkdir()
        (self.root / "public").mkdir()
        shutil.copy(ROOT / "market_config.json", self.root / "market_config.json")
        (self.root / "public/published_price_file.json").write_text("[]\n")
        (self.root / "public/decision_audit_log.json").write_text("[]\n")
        self.report = {
            "report_id": "round4-report1", "seller_id": "Vanguard_Garrison", "round": 4,
            "reported_at": "2026-10-29T08:00:00Z",
            "remaining_virtual": {"SKU_GREEN": 27, "SKU_SILVER": 15, "SKU_GOLD": 7},
            "transactions": [{"offer_id": "BASE_SKU_GREEN", "quantity": 1}],
        }
        self.write_report()
        Handler.routes = {"/prices.json": json.dumps(OFFERS).encode()}
        self.settings = {
            "interval_seconds": 1800, "request_timeout_seconds": 5,
            "buyer_results": {"type": "file", "path": "data/buyer_results.json"},
            "competitors": [{"seller_id": "Competitor A",
                "url": f"http://127.0.0.1:{self.server.server_port}/prices.json",
                "format": "price_file", "offer_ids": {
                    "SKU_GREEN": "OTHER_GREEN", "SKU_SILVER": "OTHER_SILVER"}}],
        }
        self.write_settings()

    def tearDown(self):
        self.temp.cleanup()

    def write_report(self):
        (self.root / "data/buyer_results.json").write_text(json.dumps(self.report))

    def write_settings(self):
        (self.root / "config/automation.json").write_text(json.dumps(self.settings))

    def cycle(self, **kwargs):
        with exclusive_project_lock(self.root):
            run_cycle(self.root, **kwargs)

    def read_json(self, name):
        return json.loads((self.root / name).read_text())

    def test_end_to_end_uses_buyer_stock_and_competitor_prices(self):
        self.cycle()
        offers = {row["offer_id"]: row for row in self.read_json("public/published_price_file.json")}
        self.assertEqual((offers["BASE_SKU_GREEN"]["total_price"], offers["BASE_SKU_SILVER"]["total_price"]), (1, 2))
        self.assertEqual(offers["BASE_SKU_GREEN"]["virtual_available"], 27)
        self.assertEqual(offers["BASE_SKU_SILVER"]["virtual_available"], 15)
        self.assertEqual(offers["PRIME_SKU_SILVER"]["virtual_available"], 15)
        self.assertEqual(offers["BASE_SKU_GREEN"]["physical_available"], 24)
        self.assertEqual(offers["OFFER_CHARITY_MYSTERY_POUCH"]["virtual_available"], 0)
        audit = self.read_json("data/decision_audit_log.json")[-1]
        self.assertEqual(audit["buyer_report"]["transaction_count"], 1)
        self.assertEqual(len(audit["competitor_snapshot"]), 2)
        self.assertIn("checked_at", audit["competitor_sources"][0])
        self.assertNotIn("transactions", json.dumps(self.read_json("public/decision_audit_log.json")))
        self.cycle()
        self.assertEqual(self.read_json("data/automation_state.json")["buyer_report"]["remaining_virtual"]["SKU_GREEN"], 27)

    def test_bad_or_missing_sources_leave_catalog_unchanged(self):
        before = (self.root / "public/published_price_file.json").read_bytes()
        Handler.routes["/prices.json"] = b"bad json"
        with self.assertRaises(ValueError):
            self.cycle()
        self.assertEqual((self.root / "public/published_price_file.json").read_bytes(), before)
        self.assertFalse((self.root / "data/automation_state.json").exists())
        self.settings["competitors"] = []
        self.write_settings()
        with self.assertRaisesRegex(ValueError, "at least one"):
            self.cycle()
        self.assertEqual((self.root / "public/published_price_file.json").read_bytes(), before)

    def test_old_or_rewritten_buyer_results_are_rejected(self):
        self.cycle()
        self.report["remaining_virtual"]["SKU_GREEN"] = 26
        self.write_report()
        with self.assertRaisesRegex(ValueError, "new report_id"):
            self.cycle()
        self.report["report_id"] = "older"
        self.report["reported_at"] = "2026-10-29T07:00:00Z"
        self.write_report()
        with self.assertRaisesRegex(ValueError, "older"):
            self.cycle()

    def test_later_report_changes_virtual_only_and_increases_need_reason(self):
        self.cycle()
        self.report.update(report_id="round4-report2", reported_at="2026-10-29T09:00:00Z")
        self.report["remaining_virtual"]["SKU_GREEN"] = 26
        self.write_report()
        self.cycle()
        self.report.update(report_id="round4-correction", reported_at="2026-10-29T10:00:00Z")
        self.report["remaining_virtual"]["SKU_GREEN"] = 27
        self.write_report()
        with self.assertRaisesRegex(ValueError, "stock increased"):
            self.cycle()
        self.cycle(increase_reason="Instructor approved correction")
        audit = self.read_json("data/decision_audit_log.json")[-1]
        self.assertEqual(audit["inventory_increase_reason"], "Instructor approved correction")
        green = self.read_json("public/published_price_file.json")[0]
        self.assertEqual((green["virtual_available"], green["physical_available"]), (27, 24))

    def test_wrong_seller_or_missing_stock_stops_publication(self):
        self.report["seller_id"] = "Other team"
        self.write_report()
        with self.assertRaisesRegex(ValueError, "seller_id"):
            self.cycle()
        self.report["seller_id"] = "Vanguard_Garrison"
        del self.report["remaining_virtual"]["SKU_GOLD"]
        self.write_report()
        with self.assertRaisesRegex(ValueError, "all three"):
            self.cycle()

    def test_failed_deploy_retries_same_publication_without_duplicate_audit(self):
        with patch("automation.run.deploy", side_effect=RuntimeError("test deploy failed")):
            with self.assertRaisesRegex(RuntimeError, "failed"):
                self.cycle(target="preview:optimized-code")
        state = self.read_json("data/automation_state.json")
        decision_id = state["pending_publication"]["audit"]["decision_id"]
        with self.assertRaisesRegex(ValueError, "same target"):
            self.cycle(target="live")
        with patch("automation.run.deploy") as deploy, patch("automation.run.collect_competitors", side_effect=AssertionError):
            self.cycle(target="preview:optimized-code")
        deploy.assert_called_once()
        self.assertIsNone(self.read_json("data/automation_state.json")["pending_publication"])
        audit = self.read_json("data/decision_audit_log.json")
        self.assertEqual(sum(row["decision_id"] == decision_id for row in audit), 1)

    def test_legacy_checkpoint_stock_and_pending_are_preserved(self):
        legacy = {"round_number": 4, "virtual_inventory": {"SKU_GREEN": 26}, "pending_publication": None}
        (self.root / ".market_state.json").write_text(json.dumps(legacy))
        with self.assertRaisesRegex(ValueError, "stock increased"):
            self.cycle()
        legacy["pending_publication"] = {"old": "update"}
        (self.root / ".market_state.json").write_text(json.dumps(legacy))
        with self.assertRaisesRegex(ValueError, "pending publication"):
            self.cycle()

    def test_buyer_url_is_supported_without_putting_results_in_public(self):
        Handler.routes["/results.json"] = json.dumps(self.report).encode()
        self.settings["buyer_results"] = {"type": "url", "url": f"http://127.0.0.1:{self.server.server_port}/results.json"}
        self.write_settings()
        self.cycle()
        self.assertEqual(self.read_json("data/automation_state.json")["buyer_report"]["report_id"], "round4-report1")
        self.assertFalse((self.root / "public/buyer_results.json").exists())

    def test_watcher_runs_immediately_and_schedules_after_30_minutes(self):
        with patch.object(runner, "ROOT", self.root), patch.object(runner, "run_cycle") as cycle, \
                patch.object(runner.time, "monotonic", side_effect=[1000, 1005]), \
                patch.object(runner.time, "sleep", side_effect=KeyboardInterrupt) as sleep:
            status = runner.main(["--sources", str(self.root / "config/automation.json"),
                                  "--watch", "--deploy-preview", "optimized-code"])
        self.assertEqual(status, 0)
        cycle.assert_called_once_with(self.root, self.root / "config/automation.json", "preview:optimized-code")
        sleep.assert_called_once_with(1795)


class ExtractionTests(unittest.TestCase):
    def test_html_uses_selected_price_and_virtual_stock(self):
        source = {"seller_id": "Seller", "items": {"SKU_GREEN": {
            "price_element_id": "price-green", "virtual_stock_element_id": "stock-green"}}}
        result = parse_html('<div id="price-green"><span>$1.50</span></div><b id="stock-green">9 in stock</b>', source)
        self.assertEqual((result[0].unit_price, result[0].virtual_available), (1.5, 9))
        with self.assertRaisesRegex(ValueError, "Missing HTML element"):
            parse_html('<span id="price-green">$1</span>', source)

    def test_html_attribute_and_duplicate_ids(self):
        source = {"seller_id": "Seller", "items": {"SKU_GREEN": {
            "price_element_id": "p", "price_attribute": "content", "virtual_stock_element_id": "s"}}}
        self.assertEqual(parse_html('<meta id="p" content="2.50"><span id="s">8</span>', source)[0].unit_price, 2.5)
        with self.assertRaisesRegex(ValueError, "duplicated"):
            parse_html('<b id="p">1</b><b id="p">2</b><b id="s">8</b>', source)

    def test_restricted_and_bundle_offers_cannot_be_mistaken_for_base(self):
        source = {"seller_id": "Seller", "offer_ids": {"SKU_GREEN": "OTHER_GREEN"}}
        restricted = [dict(OFFERS[0], eligibility="RETURNING_CUSTOMERS_ONLY")]
        with self.assertRaisesRegex(ValueError, "unrestricted"):
            parse_price_file(restricted, source)
        bundle = [dict(OFFERS[0], product_quantities={"OTHER_GREEN_SKU": 2})]
        with self.assertRaisesRegex(ValueError, "single-item"):
            parse_price_file(bundle, source)

    def test_future_and_expired_competitor_prices_are_rejected(self):
        source = {"seller_id": "Seller", "offer_ids": {"SKU_GREEN": "OTHER_GREEN"}}
        with self.assertRaisesRegex(ValueError, "not yet effective"):
            parse_price_file([dict(OFFERS[0], timestamp="2099-01-01T00:00:00Z")], source)
        with self.assertRaisesRegex(ValueError, "expired"):
            parse_price_file([dict(OFFERS[0], expires_at="2025-01-02T00:00:00Z")], source)


if __name__ == "__main__":
    unittest.main()
