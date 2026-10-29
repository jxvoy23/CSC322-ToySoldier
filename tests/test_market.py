"""Offline regression checks. No Firestore credentials or network are used."""

import copy
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from pricing_engine import (
    ROOT, BotArchetypeTracker, BotPopulationBelief, DualMarketPricingEngine,
    RoundSalesObservation, atomic_write_json, exclusive_project_lock,
    export_price_file_and_audit_log, generate_replay_comparison_files, load_market_config,
)


def trade(remaining=15, timestamp="2026-10-29T10:00:00Z", **overrides):
    return {
        "round": 4, "seller_id": "Vanguard_Garrison", "sku_id": "SKU_SILVER",
        "offer_id": "BASE_SKU_SILVER", "price_paid": 3.0,
        "remaining_virtual": remaining, "timestamp": timestamp, **overrides,
    }


class PricingTests(unittest.TestCase):
    def setUp(self):
        self.config, self.skus, self.competitors, self.belief, self.engine = load_market_config()

    def build(self, **overrides):
        args = dict(skus=self.skus, competitor_offers=self.competitors,
                    bot_belief=self.belief, rounds_remaining=5,
                    physical_pouch_count=12, round_number=4)
        args.update(overrides)
        return self.engine.build_daily_catalog(**args)

    def test_default_prices_and_shared_stock(self):
        offers, _ = self.build()
        actual = {offer.offer_id: offer for offer in offers}
        self.assertEqual({key: row.total_price for key, row in actual.items()}, {
            "BASE_SKU_GREEN": 2, "BASE_SKU_SILVER": 3, "BASE_SKU_GOLD": 5,
            "OFFER_CHARITY_MYSTERY_POUCH": 3, "PRIME_SKU_SILVER": 2, "PRIME_SKU_GOLD": 4})
        self.assertEqual(actual["BASE_SKU_SILVER"].virtual_available,
                         actual["PRIME_SKU_SILVER"].virtual_available)
        self.assertEqual(actual["OFFER_CHARITY_MYSTERY_POUCH"].virtual_available, 0)
        self.assertEqual(actual["OFFER_CHARITY_MYSTERY_POUCH"].product_quantities, {"SKU_MYSTERY_POUCH": 1})

    def test_prime_terms_match_actual_discount_after_base_override(self):
        offers, audit = self.build(manual_overrides={"BASE_SKU_SILVER": [2, "Approved price test"]})
        actual = {offer.offer_id: offer for offer in offers}
        self.assertEqual(actual["PRIME_SKU_SILVER"].total_price, 1)
        self.assertIn("1_DOLLAR", actual["PRIME_SKU_SILVER"].loyalty_program_terms)
        self.assertEqual(audit.submitted_prices["OFFER_CHARITY_MYSTERY_POUCH"], 2)
        self.assertEqual(audit.recommended_prices["BASE_SKU_SILVER"], 3)

    def test_disabled_prime_has_no_phantom_audit_offers(self):
        self.engine.enable_prime_discount_offers = False
        offers, audit = self.build()
        self.assertEqual(len(offers), 4)
        self.assertFalse(any(key.startswith("PRIME_") for key in audit.submitted_prices))
        self.assertEqual(offers[0].loyalty_program_terms, "NONE")

    def test_invalid_inputs_fail_explicitly(self):
        cases = [
            {"strategy_mode": "typo"}, {"rounds_remaining": 0},
            {"physical_pouch_count": -1},
            {"manual_overrides": {"UNKNOWN": [2, "reason"]}},
            {"manual_overrides": {"BASE_SKU_GREEN": [2, " "]}},
            {"manual_overrides": {"BASE_SKU_GREEN": [float("nan"), "reason"]}},
        ]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                self.build(**case)
        self.skus["SKU_GREEN"].inventory_virtual = True
        with self.assertRaises(ValueError):
            self.build()
        with self.assertRaises(ValueError):
            DualMarketPricingEngine(min_whole_price=2.5)
        with self.assertRaises(ValueError):
            self.engine.clamp_whole_dollar(2, low=4, high=3)

    def test_strategy_grid_obeys_price_and_inventory_invariants(self):
        for mode in ("dynamic_response", "fixed_price", "fixed_markup"):
            for inventory in (0, 1, 30):
                for sku in self.skus.values():
                    sku.inventory_virtual = inventory
                offers, audit = self.build(strategy_mode=mode)
                for offer in offers:
                    self.assertEqual(sum(offer.product_quantities.values()), 1)
                    self.assertTrue(1 <= offer.total_price <= 5)
                    self.assertEqual(offer.total_price, int(offer.total_price))
                    self.assertGreaterEqual(offer.virtual_available, 0)
                if inventory == 0:
                    self.assertEqual(audit.recommended_ad_bid, 0)

    def test_ad_bid_uses_discounted_returning_customer_margin(self):
        prices = {"SKU_GREEN": 2, "SKU_SILVER": 3, "SKU_GOLD": 5}
        prime = {"SKU_GREEN": 2, "SKU_SILVER": 2, "SKU_GOLD": 4}
        belief = BotPopulationBelief(0, 0, 0, 1)
        bid = self.engine.compute_optimal_ad_bid(prices, belief, prime_prices=prime,
            expected_extra_customers_reached=1, estimated_conversion_rate=1,
            target_ad_roi=0, max_ad_budget=10)
        self.assertEqual(bid, 2.75)

    def test_belief_normalization_and_observation_validation(self):
        belief = BotArchetypeTracker().update_beliefs(RoundSalesObservation(4, 7, 5, 6, 3, 25))
        self.assertAlmostEqual(sum(asdict(belief).values()), 1)
        with self.assertRaises(ValueError):
            BotArchetypeTracker().update_beliefs(RoundSalesObservation(4, 7, 5, 6, 3, 20))
        with self.assertRaises(ValueError):
            BotPopulationBelief(0, 0, 0, 0).normalized()

    def test_replay_files_do_not_mutate_input_state(self):
        before = copy.deepcopy(self.skus)
        with tempfile.TemporaryDirectory() as folder:
            result = generate_replay_comparison_files(self.engine, self.skus, self.competitors,
                self.belief, 5, 12, output_directory=folder)
            self.assertEqual(len(list(Path(folder).glob("*.json"))), 3)
            self.assertEqual(set(result), {"fixed_price", "fixed_markup", "dynamic_response"})
        self.assertEqual(self.skus, before)



if __name__ == "__main__":
    unittest.main()
