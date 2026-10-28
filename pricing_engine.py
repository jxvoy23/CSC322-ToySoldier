"""
================================================================================
CODE BRIEF: TOY SOLDIER DUAL-MARKET PRICING ENGINE (pricing_engine.py)
================================================================================
PURPOSE:
This script is our team's automated pricing, loyalty, and advertising decision
engine for both the Online Simulated Market and the In-Person Physical Market.

HOW THE CODE WORKS (5 CORE MODULES):
1. HARD RULE GUARDRAILS (Global Constants & clamp_whole_dollar):
   - Locks in the official $0.25 unit wholesale cost.
   - Enforces the $5.00 maximum price ceiling and a $1.00 minimum floor
     (guaranteeing $0.75 to $4.75 net profit per soldier sold).
   - Restricts every offer to 1 item per customer (quantity = 1).
   - Rounds all prices to clean whole dollars ($1, $2, $3, $4, $5) to eliminate
     coin/change friction in person while keeping base prices identical online.

2. BOT PERSONALITY TRACKER (BotArchetypeTracker):
   - Reads the Buyer Team's daily public summary (units sold by tier and the
     number of bots that bought nothing).
   - Uses an Exponentially Weighted Moving Average (EWMA) to infer the current
     mix of Bargain Hunters, Convenience Shoppers, Collectors, and Returning
     Loyalists as bot personalities shift over time.

3. SCRAPE-SAFE & ANTI-SPOOFING PRICING (DualMarketPricingEngine):
   - Keeps competitive whole-dollar base prices ($2 Green, $3 Silver, $5 Gold)
     live 24/7 so random or high-traffic buyer scrapes always convert.
   - Filters out rival offers with 0 stock (Phantom Stock Spoofing) and ignores
     cheap rivals with <= 3 virtual units left ('Stockout Squeeze'), letting
     low-stock competitors sell out first while we capture residual demand.

4. 'SOLDIER PRIME' LOYALTY & $3.00 CHARITY MYSTERY POUCH (build_daily_catalog):
   - Publishes returning-customer 'Soldier Prime' whole-dollar offers ($2 Silver,
     $4 Gold) funded by seller margin with no expiration, explicitly labeled to
     prevent double-counting in the Buyer Team's utility formula.
   - Pegs the unified Charity Mystery Pouch ('Shoes for Children') to the Tier 2
     Silver price ($3.00) with virtual_available = 0 (preventing simulation
     deduction errors) and physical_available set to our sealed pouch stock,
     locking in a guaranteed $2.75 net profit on every draw.

5. AD AUCTION BIDDING, AUDIT LOGS & REPLAY BENCHMARKING:
   - Computes our maximum profitable bid for Instructor Advertising Auctions
     based on expected customer reach, bot personality margins, and Prime LTV.
   - Exports 'published_price_file.json', appends 'decision_audit_log.json',
     and generates 'dynamic_response', 'fixed_price', and 'fixed_markup' files
     for the Buyer Team's replay comparison tool.
================================================================================
"""

import json
import math
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple


# ============================================================================
# 1. GLOBAL RULE CONSTANTS
# ============================================================================

UNIT_WHOLESALE_COST: float = 0.25
MIN_WHOLE_DOLLAR_PRICE: float = 1.00
MAX_PRICE_CAP: float = 5.00


# ============================================================================
# 2. DATA STRUCTURES & MODELS
# ============================================================================

@dataclass
class SKUState:
    """Internal state for a single toy soldier SKU tier."""
    sku_id: str
    tier: int  # 1 = Common Green, 2 = Premium Silver, 3 = Collector Gold
    wholesale_cost: float = UNIT_WHOLESALE_COST
    inventory_virtual: int = 0
    inventory_physical: int = 0
    recent_sales_velocity: float = 0.0
    fixed_price_baseline: float = 2.00


@dataclass
class CompetitorOffer:
    """Scraped single-unit offer from a competing seller's published price file."""
    seller_id: str
    sku_id: str
    unit_price: float
    virtual_available: int


@dataclass
class BotPopulationBelief:
    """
    Estimated probability distribution of online buyer bot personalities.
    Updated each round from observed tier sales and 'bought_nothing' counts.
    """
    bargain_hunter_share: float = 0.25
    convenience_shopper_share: float = 0.25
    collector_share: float = 0.25
    returning_loyal_share: float = 0.25


@dataclass
class RoundSalesObservation:
    """Public aggregate round results published by the Buyer Team."""
    sold_green: int
    sold_silver: int
    sold_gold: int
    sold_prime: int
    customers_bought_nothing: int
    total_active_customers: int


@dataclass
class MarketOffer:
    """
    Published single-unit offer in the common price-file format.
    Includes loyalty terms and explicit single-unit quantities (quantity = 1).
    """
    offer_id: str
    product_quantities: Dict[str, int]
    total_price: float
    virtual_available: int
    physical_available: int
    eligibility: str
    loyalty_program_terms: str
    timestamp: str


@dataclass
class DecisionAuditRecord:
    """Complete daily audit log saved before seeing results."""
    timestamp: str
    round_number: int
    rounds_remaining: int
    strategy_mode: str
    bot_belief_state: dict
    inputs_snapshot: Dict[str, dict]
    competitor_snapshot: List[dict]
    recommended_prices: Dict[str, float]
    submitted_prices: Dict[str, float]
    recommended_ad_bid: float
    manual_overrides: Dict[str, Dict[str, object]]
    unit_profit_by_offer: Dict[str, float]


# ============================================================================
# 3. BOT PERSONALITY TRACKER (EWMA ARCHETYPE INFERENCE)
# ============================================================================

class BotArchetypeTracker:
    """
    Infers changes in the Buyer Team's bot population personalities over time
    using public daily results (tier sales + number of customers who bought nothing).
    """

    def __init__(self, smoothing_alpha: float = 0.45):
        self.alpha = smoothing_alpha
        self.belief = BotPopulationBelief()

    def update_beliefs(self, obs: RoundSalesObservation) -> BotPopulationBelief:
        """Updates our estimate of the bot personality distribution after a round."""
        total_buyers = max(1, obs.total_active_customers)
        total_purchases = max(
            1, obs.sold_green + obs.sold_silver + obs.sold_gold + obs.sold_prime
        )

        # 1. Bargain Hunter Signal: Tier 1 Green share + 'bought nothing' rate
        no_buy_rate = obs.customers_bought_nothing / total_buyers
        green_share = obs.sold_green / total_purchases
        raw_bargain = 0.60 * green_share + 0.40 * no_buy_rate

        # 2. Convenience Shopper Signal: Standalone Tier 2 Silver purchases
        raw_convenience = obs.sold_silver / total_purchases

        # 3. Collector Signal: Tier 3 Gold purchases
        raw_collector = obs.sold_gold / total_purchases

        # 4. Returning / Prime Loyal Signal: Prime offer redemptions
        raw_returning = obs.sold_prime / total_purchases

        raw_total = raw_bargain + raw_convenience + raw_collector + raw_returning
        if raw_total <= 0:
            return self.belief

        b_new = (1 - self.alpha) * self.belief.bargain_hunter_share + self.alpha * (raw_bargain / raw_total)
        c_new = (1 - self.alpha) * self.belief.convenience_shopper_share + self.alpha * (raw_convenience / raw_total)
        col_new = (1 - self.alpha) * self.belief.collector_share + self.alpha * (raw_collector / raw_total)
        r_new = (1 - self.alpha) * self.belief.returning_loyal_share + self.alpha * (raw_returning / raw_total)

        s = b_new + c_new + col_new + r_new
        self.belief = BotPopulationBelief(
            bargain_hunter_share=round(b_new / s, 3),
            convenience_shopper_share=round(c_new / s, 3),
            collector_share=round(col_new / s, 3),
            returning_loyal_share=round(r_new / s, 3),
        )
        return self.belief


# ============================================================================
# 4. CORE ADAPTIVE WHOLE-DOLLAR PRICING ENGINE
# ============================================================================

class DualMarketPricingEngine:
    """
    Game-theoretic pricing engine enforcing whole-dollar prices ($1 to $5),
    a $0.25 unit cost floor, a $5.00 hard price ceiling, a 1-item-per-buyer rule,
    Soldier Prime loyalty offers, and Advertising Auction bid optimization.
    """

    def __init__(
        self,
        min_whole_price: float = MIN_WHOLE_DOLLAR_PRICE,
        max_price_cap: float = MAX_PRICE_CAP,
        fixed_markup_multiplier: float = 8.0,
        stockout_squeeze_threshold: int = 3,
        enable_prime_discount_offers: bool = True,
    ):
        self.min_whole_price = float(int(min_whole_price))
        self.max_price_cap = float(int(max_price_cap))
        self.fixed_markup_multiplier = fixed_markup_multiplier
        self.stockout_squeeze_threshold = stockout_squeeze_threshold
        self.enable_prime_discount_offers = enable_prime_discount_offers

    def clamp_whole_dollar(
        self, raw_price: float, low: Optional[float] = None, high: Optional[float] = None
    ) -> float:
        """
        Rounds any price to a clean whole dollar ($1.00, $2.00, $3.00, $4.00, $5.00)
        and clamps it strictly within [low, high] and [$1.00, $5.00].
        """
        lower_bound = max(self.min_whole_price, low if low is not None else self.min_whole_price)
        upper_bound = min(self.max_price_cap, high if high is not None else self.max_price_cap)
        rounded = float(math.floor(raw_price + 0.5))
        return float(max(lower_bound, min(upper_bound, rounded)))

    def get_effective_competitor_min_price(
        self, sku_id: str, competitor_offers: List[CompetitorOffer]
    ) -> Optional[float]:
        """
        Filters competitor offers to prevent spoofing and execute the Stockout Squeeze:
        - Ignores offers with virtual_available <= 3 (Stockout Squeeze & Phantom Stock).
        - Ignores invalid prices outside [$0.25, $5.00].
        """
        viable_prices = [
            offer.unit_price
            for offer in competitor_offers
            if offer.sku_id == sku_id
            and offer.virtual_available > self.stockout_squeeze_threshold
            and UNIT_WHOLESALE_COST <= offer.unit_price <= self.max_price_cap
        ]
        return min(viable_prices) if viable_prices else None

    def compute_tier_prices(
        self,
        skus: Dict[str, SKUState],
        competitor_offers: List[CompetitorOffer],
        bot_belief: BotPopulationBelief,
        rounds_remaining: int,
        strategy_mode: str = "dynamic_response",
    ) -> Dict[str, float]:
        """
        Computes whole-dollar unit prices in {$1.00, $2.00, $3.00, $4.00, $5.00}
        using competitor prices, inventory, velocity, and inferred bot personalities.
        """
        s_green = skus["SKU_GREEN"]
        s_silver = skus["SKU_SILVER"]
        s_gold = skus["SKU_GOLD"]

        # MODE A: FIXED PRICE BASELINE (For Buyer Replay Tool)
        if strategy_mode == "fixed_price":
            return {
                "SKU_GREEN": self.clamp_whole_dollar(s_green.fixed_price_baseline, 1.0, 2.0),
                "SKU_SILVER": self.clamp_whole_dollar(s_silver.fixed_price_baseline, 2.0, 4.0),
                "SKU_GOLD": self.clamp_whole_dollar(s_gold.fixed_price_baseline, 4.0, 5.0),
            }

        # MODE B: FIXED MARKUP BASELINE (For Buyer Replay Tool)
        if strategy_mode == "fixed_markup":
            return {
                "SKU_GREEN": self.clamp_whole_dollar(s_green.wholesale_cost * self.fixed_markup_multiplier, 1.0, 2.0),
                "SKU_SILVER": self.clamp_whole_dollar(s_silver.wholesale_cost * (self.fixed_markup_multiplier + 4.0), 2.0, 4.0),
                "SKU_GOLD": self.clamp_whole_dollar(s_gold.wholesale_cost * (self.fixed_markup_multiplier + 8.0), 3.0, 5.0),
            }

        # MODE C: DYNAMIC BOT-AWARE & SCRAPE-SAFE WHOLE-DOLLAR PRICING
        comp_min_green = self.get_effective_competitor_min_price("SKU_GREEN", competitor_offers)
        comp_min_silver = self.get_effective_competitor_min_price("SKU_SILVER", competitor_offers)
        comp_min_gold = self.get_effective_competitor_min_price("SKU_GOLD", competitor_offers)

        # 1. TIER 1 (Common Green): {$1.00, $2.00}
        expected_burn_green = s_green.inventory_virtual / max(1, rounds_remaining)
        if bot_belief.bargain_hunter_share >= 0.40 or (
            comp_min_green is not None
            and comp_min_green <= 1.50
            and s_green.recent_sales_velocity < expected_burn_green
        ):
            p_green = 1.00
        else:
            p_green = 2.00
        p_green = self.clamp_whole_dollar(p_green, low=1.00, high=2.00)

        # 2. TIER 3 (Collector Gold): {$4.00, $5.00}
        expected_burn_gold = s_gold.inventory_virtual / max(1, rounds_remaining)
        if (
            bot_belief.collector_share < 0.15
            and comp_min_gold is not None
            and comp_min_gold <= 4.00
            and s_gold.recent_sales_velocity < expected_burn_gold
            and s_gold.inventory_virtual > 4
        ):
            p_gold = 4.00
        else:
            p_gold = 5.00
        p_gold = self.clamp_whole_dollar(p_gold, low=4.00, high=5.00)

        # 3. TIER 2 (Premium Silver & Mystery Pouch Anchor): {$2.00, $3.00, $4.00}
        target_silver = 3.00
        if (
            bot_belief.convenience_shopper_share >= 0.35
            and s_silver.inventory_virtual <= 4
            and p_gold == 5.00
        ):
            target_silver = 4.00
        elif comp_min_silver is not None and comp_min_silver <= 2.00 and p_green == 1.00:
            target_silver = 2.00

        p_silver = self.clamp_whole_dollar(
            target_silver,
            low=p_green + 1.00,
            high=p_gold - 1.00,
        )

        return {
            "SKU_GREEN": p_green,
            "SKU_SILVER": p_silver,
            "SKU_GOLD": p_gold,
        }

    def compute_optimal_ad_bid(
        self,
        tier_prices: Dict[str, float],
        bot_belief: BotPopulationBelief,
        expected_extra_customers_reached: float = 8.0,
        estimated_conversion_rate: float = 0.45,
        target_ad_roi: float = 0.35,
        max_ad_budget: float = 10.00,
    ) -> float:
        """
        Calculates our optimal bid in Instructor Advertising Auctions based on
        weighted tier margins and Soldier Prime repeat-purchase LTV.
        """
        margin_green = tier_prices["SKU_GREEN"] - UNIT_WHOLESALE_COST
        margin_silver = tier_prices["SKU_SILVER"] - UNIT_WHOLESALE_COST
        margin_gold = tier_prices["SKU_GOLD"] - UNIT_WHOLESALE_COST

        weighted_unit_profit = (
            bot_belief.bargain_hunter_share * margin_green
            + bot_belief.convenience_shopper_share * margin_silver
            + bot_belief.collector_share * margin_gold
            + bot_belief.returning_loyal_share * ((margin_silver + margin_gold) / 2.0)
        )

        prime_ltv_multiplier = 1.25
        expected_incremental_profit = (
            expected_extra_customers_reached
            * estimated_conversion_rate
            * weighted_unit_profit
            * prime_ltv_multiplier
        )

        optimal_bid = expected_incremental_profit * (1.0 - target_ad_roi)
        return round(min(max_ad_budget, max(0.0, optimal_bid)), 2)

    def build_daily_catalog(
        self,
        skus: Dict[str, SKUState],
        competitor_offers: List[CompetitorOffer],
        bot_belief: BotPopulationBelief,
        rounds_remaining: int,
        physical_pouch_count: int,
        round_number: int = 1,
        strategy_mode: str = "dynamic_response",
        manual_overrides: Optional[Dict[str, Tuple[float, str]]] = None,
    ) -> Tuple[List[MarketOffer], DecisionAuditRecord]:
        """
        Generates the always-live whole-dollar catalog, including Soldier Prime
        returning-customer offers and the $3.00 Charity Mystery Pouch.
        """
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        manual_overrides = manual_overrides or {}

        recommended_tiers = self.compute_tier_prices(
            skus=skus,
            competitor_offers=competitor_offers,
            bot_belief=bot_belief,
            rounds_remaining=rounds_remaining,
            strategy_mode=strategy_mode,
        )

        p_green = recommended_tiers["SKU_GREEN"]
        p_silver = recommended_tiers["SKU_SILVER"]
        p_gold = recommended_tiers["SKU_GOLD"]
        p_pouch = p_silver

        p_prime_silver = self.clamp_whole_dollar(p_silver - 1.00, low=2.00, high=4.00)
        p_prime_gold = self.clamp_whole_dollar(p_gold - 1.00, low=3.00, high=4.00)

        recommended_all = {
            "BASE_SKU_GREEN": p_green,
            "BASE_SKU_SILVER": p_silver,
            "BASE_SKU_GOLD": p_gold,
            "PRIME_SKU_SILVER": p_prime_silver,
            "PRIME_SKU_GOLD": p_prime_gold,
            "OFFER_CHARITY_MYSTERY_POUCH": p_pouch,
        }

        submitted_all = dict(recommended_all)
        override_log: Dict[str, Dict[str, object]] = {}

        for offer_id, (override_price, reason) in manual_overrides.items():
            if offer_id in submitted_all:
                safe_override = self.clamp_whole_dollar(override_price, 1.00, 5.00)
                override_log[offer_id] = {
                    "algorithm_recommended": recommended_all[offer_id],
                    "human_override_price": safe_override,
                    "reason": reason,
                }
                submitted_all[offer_id] = safe_override

        total_phys_units = sum(max(0, s.inventory_physical) for s in skus.values())
        safe_phys_pouches = max(0, min(physical_pouch_count, total_phys_units))

        offers = [
            MarketOffer(
                offer_id="BASE_SKU_GREEN",
                product_quantities={"SKU_GREEN": 1},
                total_price=submitted_all["BASE_SKU_GREEN"],
                virtual_available=max(0, skus["SKU_GREEN"].inventory_virtual),
                physical_available=max(0, skus["SKU_GREEN"].inventory_physical),
                eligibility="ALL_CUSTOMERS_ONE_ITEM_LIMIT",
                loyalty_program_terms="ENROLLS_BUYER_IN_SOLDIER_PRIME_NO_EXPIRATION",
                timestamp=now_iso,
            ),
            MarketOffer(
                offer_id="BASE_SKU_SILVER",
                product_quantities={"SKU_SILVER": 1},
                total_price=submitted_all["BASE_SKU_SILVER"],
                virtual_available=max(0, skus["SKU_SILVER"].inventory_virtual),
                physical_available=max(0, skus["SKU_SILVER"].inventory_physical),
                eligibility="ALL_CUSTOMERS_ONE_ITEM_LIMIT",
                loyalty_program_terms="ENROLLS_BUYER_IN_SOLDIER_PRIME_NO_EXPIRATION",
                timestamp=now_iso,
            ),
            MarketOffer(
                offer_id="BASE_SKU_GOLD",
                product_quantities={"SKU_GOLD": 1},
                total_price=submitted_all["BASE_SKU_GOLD"],
                virtual_available=max(0, skus["SKU_GOLD"].inventory_virtual),
                physical_available=max(0, skus["SKU_GOLD"].inventory_physical),
                eligibility="ALL_CUSTOMERS_ONE_ITEM_LIMIT",
                loyalty_program_terms="ENROLLS_BUYER_IN_SOLDIER_PRIME_NO_EXPIRATION",
                timestamp=now_iso,
            ),
            MarketOffer(
                offer_id="OFFER_CHARITY_MYSTERY_POUCH",
                product_quantities={"SKU_SILVER": 1},
                total_price=submitted_all["OFFER_CHARITY_MYSTERY_POUCH"],
                virtual_available=0,
                physical_available=safe_phys_pouches,
                eligibility="PHYSICAL_ONLY_ONE_ITEM_LIMIT_CHARITY_SHOES_FOR_CHILDREN",
                loyalty_program_terms="SOLDIER_PRIME_MEMBERS_GET_TWO_POUCH_PEEK_SELECTION",
                timestamp=now_iso,
            ),
        ]

        if self.enable_prime_discount_offers:
            offers.extend([
                MarketOffer(
                    offer_id="PRIME_SKU_SILVER",
                    product_quantities={"SKU_SILVER": 1},
                    total_price=submitted_all["PRIME_SKU_SILVER"],
                    virtual_available=max(0, skus["SKU_SILVER"].inventory_virtual),
                    physical_available=max(0, skus["SKU_SILVER"].inventory_physical),
                    eligibility="RETURNING_CUSTOMER_SOLDIER_PRIME_MEMBER_ONE_ITEM_LIMIT",
                    loyalty_program_terms="SELLER_FUNDED_1_DOLLAR_DISCOUNT_INCLUDED_IN_PRICE_NO_EXPIRATION",
                    timestamp=now_iso,
                ),
                MarketOffer(
                    offer_id="PRIME_SKU_GOLD",
                    product_quantities={"SKU_GOLD": 1},
                    total_price=submitted_all["PRIME_SKU_GOLD"],
                    virtual_available=max(0, skus["SKU_GOLD"].inventory_virtual),
                    physical_available=max(0, skus["SKU_GOLD"].inventory_physical),
                    eligibility="RETURNING_CUSTOMER_SOLDIER_PRIME_MEMBER_ONE_ITEM_LIMIT",
                    loyalty_program_terms="SELLER_FUNDED_1_DOLLAR_DISCOUNT_INCLUDED_IN_PRICE_NO_EXPIRATION",
                    timestamp=now_iso,
                ),
            ])

        for offer in offers:
            assert sum(offer.product_quantities.values()) == 1, f"{offer.offer_id} violates 1-item rule!"
            assert self.min_whole_price <= offer.total_price <= self.max_price_cap, (
                f"{offer.offer_id} price ${offer.total_price} outside [$1.00, $5.00] cap!"
            )
            assert offer.total_price.is_integer(), f"{offer.offer_id} is not a whole-dollar price!"

        recommended_ad_bid = self.compute_optimal_ad_bid(
            tier_prices=recommended_tiers,
            bot_belief=bot_belief,
        )

        unit_profits = {
            offer_id: round(price - UNIT_WHOLESALE_COST, 2)
            for offer_id, price in submitted_all.items()
        }

        audit_record = DecisionAuditRecord(
            timestamp=now_iso,
            round_number=round_number,
            rounds_remaining=rounds_remaining,
            strategy_mode=strategy_mode,
            bot_belief_state=asdict(bot_belief),
            inputs_snapshot={k: asdict(v) for k, v in skus.items()},
            competitor_snapshot=[asdict(c) for c in competitor_offers],
            recommended_prices=recommended_all,
            submitted_prices=submitted_all,
            recommended_ad_bid=recommended_ad_bid,
            manual_overrides=override_log,
            unit_profit_by_offer=unit_profits,
        )

        return offers, audit_record


# ============================================================================
# 5. FILE EXPORT & REPLAY BENCHMARKING UTILITIES
# ============================================================================

def export_price_file_and_audit_log(
    offers: List[MarketOffer],
    audit_record: DecisionAuditRecord,
    price_file_path: str = "published_price_file.json",
    audit_log_path: str = "decision_audit_log.json",
) -> None:
    """Writes the downloadable price file and appends the decision audit log."""
    with open(price_file_path, "w", encoding="utf-8") as f:
        json.dump([asdict(o) for o in offers], f, indent=2)

    existing_logs = []
    if os.path.exists(audit_log_path):
        try:
            with open(audit_log_path, "r", encoding="utf-8") as f:
                existing_logs = json.load(f)
        except json.JSONDecodeError:
            existing_logs = []

    existing_logs.append(asdict(audit_record))
    with open(audit_log_path, "w", encoding="utf-8") as f:
        json.dump(existing_logs, f, indent=2)


def generate_replay_comparison_files(
    engine: DualMarketPricingEngine,
    skus: Dict[str, SKUState],
    competitor_offers: List[CompetitorOffer],
    bot_belief: BotPopulationBelief,
    rounds_remaining: int,
    physical_pouch_count: int,
    round_number: int = 1,
) -> Dict[str, List[dict]]:
    """
    Generates all three strategy price files ('dynamic_response', 'fixed_price',
    'fixed_markup') from the exact same starting state for the Buyer Replay Tool.
    """
    modes = ["dynamic_response", "fixed_price", "fixed_markup"]
    comparison_outputs: Dict[str, List[dict]] = {}

    for mode in modes:
        offers, _ = engine.build_daily_catalog(
            skus=skus,
            competitor_offers=competitor_offers,
            bot_belief=bot_belief,
            rounds_remaining=rounds_remaining,
            physical_pouch_count=physical_pouch_count,
            round_number=round_number,
            strategy_mode=mode,
        )
        comparison_outputs[mode] = [asdict(o) for o in offers]
        filename = f"replay_price_file_{mode}.json"
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(comparison_outputs[mode], f, indent=2)

    return comparison_outputs


if __name__ == "__main__":
    # 1. Update Bot Personality Beliefs from yesterday's public market report
    bot_tracker = BotArchetypeTracker(smoothing_alpha=0.45)
    yesterday_results = RoundSalesObservation(
        sold_green=4,
        sold_silver=7,
        sold_gold=5,
        sold_prime=6,
        customers_bought_nothing=3,
        total_active_customers=25,
    )
    updated_bot_beliefs = bot_tracker.update_beliefs(yesterday_results)

    # 2. Load current SKU inventory & $0.25 unit cost
    current_skus = {
        "SKU_GREEN": SKUState("SKU_GREEN", tier=1, wholesale_cost=0.25, inventory_virtual=28, inventory_physical=24, recent_sales_velocity=4.0, fixed_price_baseline=2.00),
        "SKU_SILVER": SKUState("SKU_SILVER", tier=2, wholesale_cost=0.25, inventory_virtual=16, inventory_physical=14, recent_sales_velocity=7.0, fixed_price_baseline=3.00),
        "SKU_GOLD": SKUState("SKU_GOLD", tier=3, wholesale_cost=0.25, inventory_virtual=7, inventory_physical=6, recent_sales_velocity=5.0, fixed_price_baseline=5.00),
    }

    # 3. Load scraped competitor offers
    scraped_competitors = [
        CompetitorOffer("Team_Alpha", "SKU_GREEN", unit_price=2.00, virtual_available=18),
        CompetitorOffer("Team_Beta", "SKU_GREEN", unit_price=1.00, virtual_available=2),  # Ignored (Stockout Squeeze <= 3)
        CompetitorOffer("Team_Alpha", "SKU_SILVER", unit_price=3.00, virtual_available=10),
        CompetitorOffer("Team_Alpha", "SKU_GOLD", unit_price=5.00, virtual_available=4),
    ]

    # 4. Run Adaptive Whole-Dollar Engine
    engine = DualMarketPricingEngine(
        min_whole_price=1.00,
        max_price_cap=5.00,
        stockout_squeeze_threshold=3,
        enable_prime_discount_offers=True,
    )

    daily_offers, daily_audit = engine.build_daily_catalog(
        skus=current_skus,
        competitor_offers=scraped_competitors,
        bot_belief=updated_bot_beliefs,
        rounds_remaining=5,
        physical_pouch_count=12,
        round_number=4,
        strategy_mode="dynamic_response",
    )

    # In pricing_engine.py, update the output paths:
    export_price_file_and_audit_log(
        offers=daily_offers,
        audit_record=daily_audit,
        price_file_path="public/published_price_file.json",
        audit_log_path="public/decision_audit_log.json"
    )