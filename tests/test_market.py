"""Offline regression checks. No Firestore credentials or network are used."""

import copy
import io
import json
import math
import ssl
import tempfile
import unittest
import urllib.error
from dataclasses import asdict
from pathlib import Path
from unittest.mock import Mock, patch
from contextlib import redirect_stdout

import pricing_engine
from buyer_ping_client import BuyerMarketBroadcaster
from live_market_listener import MarketState
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


class ListenerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.config_path = self.root / "market_config.json"
        self.config_path.write_bytes((ROOT / "market_config.json").read_bytes())
        self.state = MarketState(self.config_path)

    def tearDown(self):
        self.directory.cleanup()

    def test_virtual_trade_never_changes_physical_stock(self):
        physical = {key: value.inventory_physical for key, value in self.state.skus.items()}
        self.assertTrue(self.state.apply_trade(trade()))
        self.assertEqual(self.state.skus["SKU_SILVER"].inventory_virtual, 15)
        self.assertEqual({key: value.inventory_physical for key, value in self.state.skus.items()}, physical)

    def test_equal_timestamp_trades_are_not_lost(self):
        self.assertTrue(self.state.apply_trade(trade(15)))
        self.assertTrue(self.state.apply_trade(trade(14)))
        self.assertFalse(self.state.apply_trade(trade(14)))
        self.assertEqual(self.state.skus["SKU_SILVER"].inventory_virtual, 14)

    def test_old_events_wrong_round_and_restock_attempts_are_ignored(self):
        self.state.apply_trade(trade(14, "2026-10-29T10:00:01Z"))
        self.assertFalse(self.state.apply_trade(trade(13)))
        self.assertFalse(self.state.apply_trade(trade(12, round=3)))
        self.assertFalse(self.state.apply_trade(trade(16, "2026-10-29T10:00:02Z")))
        self.assertEqual(self.state.skus["SKU_SILVER"].inventory_virtual, 14)

    def test_restart_retains_stock_and_event_cursor(self):
        self.state.apply_trade(trade())
        self.state.save()
        restarted = MarketState(self.config_path)
        self.assertEqual(restarted.skus["SKU_SILVER"].inventory_virtual, 15)
        self.assertFalse(restarted.apply_trade(trade()))

    def test_aliases_share_one_stock_pool(self):
        self.state.apply_trade(trade(15, seller_id="Group 5"))
        self.assertFalse(self.state.apply_trade(trade(16, seller_id="Vanguard_Garrison")))
        self.assertFalse(self.state.competitors)

    def test_competitor_prime_sale_does_not_replace_standard_price(self):
        self.state.apply_trade(trade(10, seller_id="OtherTeam"))
        self.state.apply_trade(trade(9, "2026-10-29T10:00:01Z",
            seller_id="OtherTeam", offer_id="PRIME_SKU_SILVER", price_paid=2))
        offer = next(iter(self.state.competitors.values()))
        self.assertEqual(offer["unit_price"], 3)
        self.assertEqual(offer["virtual_available"], 9)

    def test_malformed_trade_cannot_mutate_state(self):
        for changes in ({"remaining_virtual": -1}, {"round": True},
                        {"timestamp": "2026-10-29T10:00:00"}, {"price_paid": math.inf},
                        {"market": "physical"}, {"is_test_replay": True}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.state.apply_trade(trade(**changes))
        self.assertEqual(self.state.skus["SKU_SILVER"].inventory_virtual, 16)

    def test_failed_publication_resumes_after_restart_without_duplicate_audit(self):
        self.state.apply_trade(trade())
        self.state.prepare_publication()
        decision_id = self.state.pending_publication["audit"]["decision_id"]
        database = Mock()
        database.collection.return_value.document.return_value.set.side_effect = RuntimeError("Offline")
        with self.assertRaises(RuntimeError):
            self.state.publish_pending(database)
        restarted = MarketState(self.config_path)
        self.assertEqual(restarted.pending_publication["audit"]["decision_id"], decision_id)
        restarted.publish_pending(Mock())
        audit = json.loads((self.root / "decision_audit_log.json").read_text())
        self.assertEqual(len(audit), 1)
        self.assertIsNone(restarted.pending_publication)
        history = json.loads((self.root / "public/decision_audit_log.json").read_text())
        self.assertTrue(history)
        self.assertFalse(any("inputs_snapshot" in row or "bot_belief_state" in row for row in history))

    def test_deployment_failure_keeps_the_pending_update(self):
        self.state.prepare_publication()
        deployer = Mock(side_effect=RuntimeError("CLI failure"))
        with self.assertRaises(RuntimeError):
            self.state.publish_pending(Mock(), deployer)
        self.assertIsNotNone(MarketState(self.config_path).pending_publication)
        self.state.publish_pending(Mock(), Mock())
        self.assertEqual(len(json.loads((self.root / "decision_audit_log.json").read_text())), 1)

    def test_corrupt_audit_is_preserved_and_blocks_publication(self):
        damaged = self.root / "decision_audit_log.json"
        damaged.write_text("not JSON")
        self.state.prepare_publication()
        with self.assertRaises(json.JSONDecodeError):
            self.state.publish_pending(Mock())
        self.assertEqual(damaged.read_text(), "not JSON")

    def test_second_writer_is_rejected(self):
        with exclusive_project_lock(self.root):
            with self.assertRaises((RuntimeError, OSError)):
                with exclusive_project_lock(self.root):
                    self.fail("Two writers acquired the same project lock")

    def test_cli_uses_the_selected_project_checkpoint_and_competitor_state(self):
        self.state.apply_trade(trade(27, sku_id="SKU_GREEN", offer_id="BASE_SKU_GREEN"))
        self.state.apply_trade(trade(100, seller_id="OtherTeam", sku_id="SKU_GREEN",
            offer_id="BASE_SKU_GREEN", price_paid=1))
        self.state.save()
        with patch("sys.argv", ["pricing_engine.py", "--config", str(self.config_path)]), redirect_stdout(io.StringIO()):
            pricing_engine.main()
        catalog = json.loads((self.root / "public/published_price_file.json").read_text())
        green = next(offer for offer in catalog if offer["offer_id"] == "BASE_SKU_GREEN")
        self.assertEqual(green["virtual_available"], 27)
        self.assertEqual(green["total_price"], 1)

    def test_cli_cannot_overwrite_an_unfinished_publication(self):
        self.state.prepare_publication()
        with patch("sys.argv", ["pricing_engine.py", "--config", str(self.config_path)]):
            with self.assertRaises(RuntimeError):
                pricing_engine.main()

    def test_detailed_audit_cannot_be_exported_under_public(self):
        self.state.prepare_publication()
        pending = self.state.pending_publication
        with self.assertRaises(ValueError):
            export_price_file_and_audit_log(pending["offers"], pending["audit"],
                self.root / "public/catalog.json", self.root / "public/private_audit.json")


class Response(io.BytesIO):
    status = 200

    def __init__(self, body=b"{}"):
        super().__init__(body)


class BuyerClientTests(unittest.TestCase):
    def setUp(self):
        self.client = BuyerMarketBroadcaster()

    def test_replay_sends_nothing(self):
        with patch("urllib.request.urlopen") as network:
            self.assertFalse(self.client.ping_trade(4, "Vanguard_Garrison", "SKU_SILVER",
                3, 15, is_test_replay=True))
            network.assert_not_called()

    def test_validation_sends_nothing_for_bad_stock(self):
        with patch("urllib.request.urlopen") as network, self.assertRaises(ValueError):
            self.client.ping_trade(4, "Vanguard_Garrison", "SKU_SILVER", 3, -1)
        network.assert_not_called()

    def test_tls_certificate_verification_is_enabled(self):
        self.assertEqual(self.client.ssl_ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(self.client.ssl_ctx.check_hostname)

    def test_retries_use_the_same_document_id(self):
        with patch("urllib.request.urlopen", side_effect=[Response(), Response()]) as network:
            for _ in range(2):
                self.assertTrue(self.client.ping_trade(4, "Vanguard_Garrison", "SKU_SILVER",
                    3, 15, transaction_id="accepted-trade-1"))
            self.assertEqual(network.call_args_list[0].args[0].full_url,
                             network.call_args_list[1].args[0].full_url)

    def test_conflict_is_success_only_for_identical_trade_values(self):
        with patch("urllib.request.urlopen", return_value=Response()) as network:
            self.client.ping_trade(4, "Vanguard_Garrison", "SKU_SILVER", 3, 15, transaction_id="one")
            request = network.call_args.args[0]
            body = request.data
        conflict = urllib.error.HTTPError(request.full_url, 409, "Already exists", None, None)
        with patch("urllib.request.urlopen", side_effect=[conflict, Response(body)]):
            self.assertTrue(self.client.ping_trade(4, "Vanguard_Garrison", "SKU_SILVER", 3, 15, transaction_id="one"))
        with patch("urllib.request.urlopen", side_effect=[conflict, Response(body)]):
            self.assertFalse(self.client.ping_trade(4, "Vanguard_Garrison", "SKU_SILVER", 4, 15, transaction_id="one"))

    def test_network_failure_does_not_undo_or_raise_for_a_committed_purchase(self):
        with patch("urllib.request.urlopen", side_effect=OSError("Offline")):
            self.assertFalse(self.client.ping_trade(4, "Vanguard_Garrison", "SKU_SILVER", 3, 15))


if __name__ == "__main__":
    unittest.main()
