"""Verify cent-priced catalogs preserve the starting Firebase inventory."""

import copy
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from automation.prepare_live import prepare
from pricing_engine import ROOT


class CentFirebaseTests(unittest.TestCase):
    def test_prepare_preserves_cent_prices_and_stock_without_writing_firebase(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "public").mkdir()
            for name in ("market_config.json", "public/published_price_file.json",
                         "public/decision_audit_log.json"):
                shutil.copy(ROOT / name, root / name)
            catalog = json.loads((root / "public/published_price_file.json").read_text())
            state = {item["offer_id"]: {"price": item["total_price"],
                     "virtual": item["virtual_available"], "physical": item["physical_available"]}
                     for item in catalog}
            state["BASE_SKU_GREEN"]["price"] = 0.99
            state["BASE_SKU_SILVER"]["price"] = 1.99
            state["PRIME_SKU_SILVER"]["price"] = 0.99
            state["last_updated"] = datetime(2026, 10, 29, tzinfo=timezone.utc)
            before = copy.deepcopy(state)
            with patch("automation.prepare_live.public_firebase_state", return_value=state):
                prepare(root)
            result = json.loads((root / "public/published_price_file.json").read_text())
            green = next(item for item in result if item["offer_id"] == "BASE_SKU_GREEN")
            silver = next(item for item in result if item["offer_id"] == "BASE_SKU_SILVER")
            self.assertEqual(green["total_price"], 0.99)
            self.assertEqual(green["virtual_available"], state["BASE_SKU_GREEN"]["virtual"])
            self.assertEqual(silver["total_price"], 1.99)
            self.assertEqual(state, before)


if __name__ == "__main__":
    unittest.main()
