"""Validated seller pricing, private audits, and public offer history.

The dynamic strategy is a heuristic, not an equilibrium solver. Imports have no
network or filesystem side effects. Set actual class rules in market_config.json.
"""

import argparse
import json
import math
import os
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parent
UNIT_WHOLESALE_COST = 0.25
MIN_WHOLE_DOLLAR_PRICE = 1.0
MAX_PRICE_CAP = 5.0
SKU_IDS = ("SKU_GREEN", "SKU_SILVER", "SKU_GOLD")
STRATEGIES = ("dynamic_response", "fixed_price", "fixed_markup")


@contextmanager
def exclusive_project_lock(root):
    """Prevent two pricing processes from rewriting the same files."""
    with (Path(root) / ".market_listener.lock").open("a+b") as stream:
        acquired = False
        try:
            if os.name == "nt":
                import msvcrt
                stream.seek(0, os.SEEK_END)
                if stream.tell() == 0:
                    stream.write(b"0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
            yield
        except BlockingIOError as error:
            raise RuntimeError("Another pricing process is using this project; stop it first") from error
        finally:
            if acquired:
                if os.name == "nt":
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def canonical_seller(value):
    return "".join(char for char in value.casefold() if char.isalnum())


def number(value, name, minimum=0.0):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    if not math.isfinite(value) or value < minimum:
        raise ValueError(f"{name} must be finite and at least {minimum}")
    return float(value)


def count(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer of at least {minimum}")
    return value


def atomic_write_json(path, data):
    """Readers never see a partially written JSON file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def read_json(path, default=None):
    """Corrupt files raise instead of silently erasing history."""
    path = Path(path)
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


@dataclass
class SKUState:
    sku_id: str
    tier: int
    wholesale_cost: float = UNIT_WHOLESALE_COST
    inventory_virtual: int = 0
    inventory_physical: int = 0
    recent_sales_velocity: float = 0.0
    fixed_price_baseline: float = 2.0


@dataclass
class CompetitorOffer:
    seller_id: str
    sku_id: str
    unit_price: float
    virtual_available: int


@dataclass
class BotPopulationBelief:
    bargain_hunter_share: float = 0.25
    convenience_shopper_share: float = 0.25
    collector_share: float = 0.25
    returning_loyal_share: float = 0.25

    def normalized(self):
        weights = {key: number(value, key) for key, value in asdict(self).items()}
        total = sum(weights.values())
        if total <= 0:
            raise ValueError("Buyer belief weights need a positive total")
        return BotPopulationBelief(**{key: value / total for key, value in weights.items()})


@dataclass
class RoundSalesObservation:
    # Standard tier counts exclude Prime sales, which are counted separately.
    sold_green: int
    sold_silver: int
    sold_gold: int
    sold_prime: int
    customers_bought_nothing: int
    total_active_customers: int


@dataclass
class MarketOffer:
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
    manual_overrides: dict
    unit_profit_by_offer: Dict[str, float]
    decision_id: str = field(default_factory=lambda: uuid.uuid4().hex)


class BotArchetypeTracker:
    """EWMA estimates; sales alone do not identify actual buyer personalities."""

    def __init__(self, smoothing_alpha=0.45):
        self.alpha = number(smoothing_alpha, "smoothing_alpha")
        if self.alpha > 1:
            raise ValueError("smoothing_alpha must be between zero and one")
        self.belief = BotPopulationBelief()

    def update_beliefs(self, obs):
        for key, value in asdict(obs).items():
            count(value, key)
        purchases = obs.sold_green + obs.sold_silver + obs.sold_gold + obs.sold_prime
        if purchases + obs.customers_bought_nothing != obs.total_active_customers:
            raise ValueError("Use exclusive purchase counts and include every customer")
        if not obs.total_active_customers:
            return self.belief
        denominator = max(1, purchases)
        signals = (
            0.6 * obs.sold_green / denominator
            + 0.4 * obs.customers_bought_nothing / obs.total_active_customers,
            obs.sold_silver / denominator, obs.sold_gold / denominator,
            obs.sold_prime / denominator,
        )
        total = sum(signals)
        if total:
            values = [(1 - self.alpha) * old + self.alpha * signal / total
                      for old, signal in zip(asdict(self.belief).values(), signals)]
            self.belief = BotPopulationBelief(*values).normalized()
        return self.belief


class DualMarketPricingEngine:
    def __init__(self, min_whole_price=MIN_WHOLE_DOLLAR_PRICE,
                 max_price_cap=MAX_PRICE_CAP, fixed_markup_multiplier=8.0,
                 stockout_squeeze_threshold=3, enable_prime_discount_offers=True):
        low = number(min_whole_price, "min_whole_price", 1)
        high = number(max_price_cap, "max_price_cap", 1)
        if not low.is_integer() or not high.is_integer() or low > high or high > 5:
            raise ValueError("Whole-dollar bounds must satisfy 1 <= minimum <= maximum <= 5")
        self.min_whole_price = low
        self.max_price_cap = high
        self.fixed_markup_multiplier = number(fixed_markup_multiplier, "markup")
        self.stockout_squeeze_threshold = count(stockout_squeeze_threshold, "stock threshold")
        if type(enable_prime_discount_offers) is not bool:
            raise ValueError("enable_prime_discount_offers must be a boolean")
        self.enable_prime_discount_offers = enable_prime_discount_offers

    def clamp_whole_dollar(self, raw_price, low=None, high=None):
        raw_price = number(raw_price, "price")
        lower = math.ceil(max(self.min_whole_price, number(low, "lower bound")
                              if low is not None else self.min_whole_price))
        upper = math.floor(min(self.max_price_cap, number(high, "upper bound")
                               if high is not None else self.max_price_cap))
        if lower > upper:
            raise ValueError("No whole-dollar price satisfies the requested bounds")
        return float(max(lower, min(upper, math.floor(raw_price + 0.5))))

    def _validate_skus(self, skus):
        if set(skus) != set(SKU_IDS):
            raise ValueError(f"Expected exactly these SKUs: {SKU_IDS}")
        for tier, sku_id in enumerate(SKU_IDS, 1):
            sku = skus[sku_id]
            if sku.sku_id != sku_id or type(sku.tier) is not int or sku.tier != tier:
                raise ValueError(f"Invalid SKU/tier mapping for {sku_id}")
            count(sku.inventory_virtual, f"{sku_id} virtual stock")
            count(sku.inventory_physical, f"{sku_id} physical stock")
            number(sku.recent_sales_velocity, f"{sku_id} velocity")
            number(sku.fixed_price_baseline, f"{sku_id} baseline", 1)
            if number(sku.wholesale_cost, f"{sku_id} cost") > self.max_price_cap:
                raise ValueError(f"{sku_id} cost exceeds the price cap")

    def get_effective_competitor_min_price(self, sku_id, competitor_offers):
        viable = [offer.unit_price for offer in competitor_offers
                  if offer.sku_id == sku_id
                  and type(offer.virtual_available) is int
                  and offer.virtual_available > self.stockout_squeeze_threshold
                  and not isinstance(offer.unit_price, bool)
                  and isinstance(offer.unit_price, (int, float))
                  and math.isfinite(offer.unit_price)
                  and UNIT_WHOLESALE_COST <= offer.unit_price <= self.max_price_cap]
        return min(viable) if viable else None

    def compute_tier_prices(self, skus, competitor_offers, bot_belief,
                            rounds_remaining, strategy_mode="dynamic_response"):
        self._validate_skus(skus)
        count(rounds_remaining, "rounds_remaining", 1)
        if strategy_mode not in STRATEGIES:
            raise ValueError(f"Unknown strategy: {strategy_mode}")
        belief = bot_belief.normalized()
        green, silver, gold = (skus[key] for key in SKU_IDS)
        if strategy_mode == "fixed_price":
            raw = [sku.fixed_price_baseline for sku in (green, silver, gold)]
        elif strategy_mode == "fixed_markup":
            raw = [sku.wholesale_cost * (self.fixed_markup_multiplier + 4 * index)
                   for index, sku in enumerate((green, silver, gold))]
        else:
            competitor = {key: self.get_effective_competitor_min_price(key, competitor_offers)
                          for key in SKU_IDS}
            p_green = 1.0 if (belief.bargain_hunter_share >= 0.4 or (
                competitor["SKU_GREEN"] is not None and competitor["SKU_GREEN"] <= 1.5
                and green.recent_sales_velocity < green.inventory_virtual / rounds_remaining)) else 2.0
            p_gold = 4.0 if (belief.collector_share < 0.15
                and competitor["SKU_GOLD"] is not None and competitor["SKU_GOLD"] <= 4
                and gold.recent_sales_velocity < gold.inventory_virtual / rounds_remaining
                and gold.inventory_virtual > 4) else 5.0
            p_silver = 3.0
            if belief.convenience_shopper_share >= 0.35 and silver.inventory_virtual <= 4 and p_gold == 5:
                p_silver = 4.0
            elif competitor["SKU_SILVER"] is not None and competitor["SKU_SILVER"] <= 2 and p_green == 1:
                p_silver = 2.0
            raw = [p_green, p_silver, p_gold]
        return {key: self.clamp_whole_dollar(price, low=skus[key].wholesale_cost)
                for key, price in zip(SKU_IDS, raw)}

    def compute_optimal_ad_bid(self, tier_prices, bot_belief,
                              expected_extra_customers_reached=8.0,
                              estimated_conversion_rate=0.45, target_ad_roi=0.35,
                              max_ad_budget=10.0, *, unit_costs=None,
                              prime_prices=None, available_units=None):
        """Expected-margin bid limit; the historical method name is retained."""
        reach = number(expected_extra_customers_reached, "extra reach")
        conversion = number(estimated_conversion_rate, "conversion")
        if conversion > 1:
            raise ValueError("Conversion rate cannot exceed one")
        roi = number(target_ad_roi, "target ROI")
        budget = number(max_ad_budget, "ad budget")
        belief = bot_belief.normalized()
        costs = unit_costs or dict.fromkeys(SKU_IDS, UNIT_WHOLESALE_COST)
        margins = {key: max(0.0, tier_prices[key] - costs[key]) for key in SKU_IDS}
        prime = prime_prices or tier_prices
        returning_margin = sum(max(0.0, prime[key] - costs[key]) for key in SKU_IDS[1:]) / 2
        expected_margin = (belief.bargain_hunter_share * margins["SKU_GREEN"]
            + belief.convenience_shopper_share * margins["SKU_SILVER"]
            + belief.collector_share * margins["SKU_GOLD"]
            + belief.returning_loyal_share * returning_margin)
        units = reach * conversion
        if available_units is not None:
            units = min(units, count(available_units, "available_units"))
        return math.floor(min(budget, units * expected_margin / (1 + roi)) * 100) / 100

    def build_daily_catalog(self, skus, competitor_offers, bot_belief,
                            rounds_remaining, physical_pouch_count, round_number=1,
                            strategy_mode="dynamic_response", manual_overrides=None):
        count(round_number, "round_number", 1)
        count(physical_pouch_count, "physical_pouch_count")
        now = utc_now()
        tiers = self.compute_tier_prices(skus, competitor_offers, bot_belief,
                                         rounds_remaining, strategy_mode)
        recommended = {f"BASE_{key}": value for key, value in tiers.items()}
        recommended["OFFER_CHARITY_MYSTERY_POUCH"] = tiers["SKU_SILVER"]
        if self.enable_prime_discount_offers:
            recommended.update({f"PRIME_{key}": self.clamp_whole_dollar(
                tiers[key] - 1, low=skus[key].wholesale_cost, high=tiers[key])
                for key in SKU_IDS[1:]})
        submitted = dict(recommended)
        overrides = manual_overrides or {}
        for offer_id, override in overrides.items():
            if offer_id not in submitted:
                raise ValueError(f"Cannot override an unpublished offer: {offer_id}")
            if not isinstance(override, (list, tuple)) or len(override) != 2:
                raise ValueError("Each override requires [price, reason]")
            price, reason = override
            price = number(price, "override price", self.min_whole_price)
            if not price.is_integer() or price > self.max_price_cap or not isinstance(reason, str) or not reason.strip():
                raise ValueError("Overrides need a valid whole-dollar price and a written reason")
            submitted[offer_id] = price
        if "OFFER_CHARITY_MYSTERY_POUCH" not in overrides:
            submitted["OFFER_CHARITY_MYSTERY_POUCH"] = submitted["BASE_SKU_SILVER"]
        for key in SKU_IDS[1:]:
            prime_id = f"PRIME_{key}"
            if prime_id in submitted and prime_id not in overrides:
                submitted[prime_id] = self.clamp_whole_dollar(
                    submitted[f"BASE_{key}"] - 1, low=skus[key].wholesale_cost,
                    high=submitted[f"BASE_{key}"])
            if prime_id in submitted and submitted[prime_id] > submitted[f"BASE_{key}"]:
                raise ValueError("A Prime offer cannot cost more than its standard offer")
        offers, offer_costs = [], {}
        for offer_id, price in submitted.items():
            is_pouch = offer_id == "OFFER_CHARITY_MYSTERY_POUCH"
            is_prime = offer_id.startswith("PRIME_")
            sku_id = "SKU_MYSTERY_POUCH" if is_pouch else offer_id.split("_", 1)[1]
            cost = UNIT_WHOLESALE_COST if is_pouch else skus[sku_id].wholesale_cost
            if price < cost:
                raise ValueError(f"{offer_id} would sell below its assigned unit cost")
            offer_costs[offer_id] = cost
            eligibility = "ALL_CUSTOMERS_ONE_ITEM_LIMIT"
            terms = "ENROLLS_BUYER_IN_SOLDIER_PRIME_NO_EXPIRATION" if self.enable_prime_discount_offers else "NONE"
            if is_pouch:
                eligibility = "PHYSICAL_ONLY_ONE_ITEM_LIMIT_CHARITY_SHOES_FOR_CHILDREN"
                terms = "SOLDIER_PRIME_MEMBERS_GET_TWO_POUCH_PEEK_SELECTION" if self.enable_prime_discount_offers else "NONE"
            elif is_prime:
                eligibility = "RETURNING_CUSTOMER_SOLDIER_PRIME_MEMBER_ONE_ITEM_LIMIT"
                discount = submitted[f"BASE_{sku_id}"] - price
                terms = f"SELLER_FUNDED_{discount:g}_DOLLAR_DISCOUNT_INCLUDED_IN_PRICE_NO_EXPIRATION"
            offers.append(MarketOffer(offer_id, {sku_id: 1}, price,
                0 if is_pouch else skus[sku_id].inventory_virtual,
                physical_pouch_count if is_pouch else skus[sku_id].inventory_physical,
                eligibility, terms, now))
        final_tiers = {key: submitted[f"BASE_{key}"] for key in SKU_IDS}
        prime_tiers = {key: submitted.get(f"PRIME_{key}", final_tiers[key]) for key in SKU_IDS}
        bid = self.compute_optimal_ad_bid(final_tiers, bot_belief,
            unit_costs={key: skus[key].wholesale_cost for key in SKU_IDS},
            prime_prices=prime_tiers, available_units=sum(s.inventory_virtual for s in skus.values()))
        audit = DecisionAuditRecord(now, round_number, rounds_remaining, strategy_mode,
            asdict(bot_belief.normalized()), {key: asdict(sku) for key, sku in skus.items()},
            [asdict(offer) for offer in competitor_offers], recommended, submitted, bid,
            {key: {"algorithm_recommended": recommended[key], "human_override_price": submitted[key],
                   "reason": value[1].strip()} for key, value in overrides.items()},
            {key: round(price - offer_costs[key], 2) for key, price in submitted.items()})
        return offers, audit


def export_price_file_and_audit_log(offers, audit_record,
        price_file_path=ROOT / "public/published_price_file.json",
        audit_log_path=ROOT / "decision_audit_log.json", *, public_history_path=None):
    """Idempotent audit append, public change history, then atomic catalog replace."""
    audit = asdict(audit_record) if isinstance(audit_record, DecisionAuditRecord) else audit_record
    catalog = [asdict(offer) if isinstance(offer, MarketOffer) else offer for offer in offers]
    audit_path = Path(audit_log_path).resolve()
    price_directory = Path(price_file_path).resolve().parent
    if audit_path.is_relative_to((ROOT / "public").resolve()) or (
            price_directory.name == "public" and audit_path.is_relative_to(price_directory)):
        raise ValueError("Private audits must be stored outside public/")
    logs = read_json(audit_path, [])
    if not isinstance(logs, list):
        raise ValueError("Private audit must be a JSON array")
    if not any(row.get("decision_id") == audit.get("decision_id") for row in logs):
        atomic_write_json(audit_path, logs + [audit])
    if public_history_path is not None:
        history = read_json(public_history_path, [])
        if not isinstance(history, list) or any("inputs_snapshot" in row for row in history):
            raise ValueError("Migrate the public audit to sanitized price history first")
        latest = {row["offer_id"]: row for row in history}
        for offer in catalog:
            previous = latest.get(offer["offer_id"])
            fields = ("total_price", "product_quantities", "eligibility", "loyalty_program_terms")
            if previous is None or previous.get("active") is False or any(previous.get(key) != offer[key] for key in fields):
                history.append({"timestamp": offer["timestamp"], "round_number": audit["round_number"],
                    "offer_id": offer["offer_id"], "previous_price": previous.get("total_price") if previous else None,
                    "active": True, **{key: offer[key] for key in fields}})
        active_ids = {offer["offer_id"] for offer in catalog}
        for offer_id, previous in latest.items():
            if offer_id not in active_ids and previous.get("active", True):
                history.append({**previous, "timestamp": audit["timestamp"],
                    "round_number": audit["round_number"], "active": False,
                    "previous_price": previous["total_price"]})
        atomic_write_json(public_history_path, history)
    atomic_write_json(price_file_path, catalog)


def generate_replay_comparison_files(engine, skus, competitor_offers, bot_belief,
        rounds_remaining, physical_pouch_count, round_number=1, *, output_directory=ROOT / "replays"):
    """Write inputs only; the buyer must replay with identical opening state and draws."""
    outputs = {}
    for mode in STRATEGIES:
        offers, _ = engine.build_daily_catalog(skus, competitor_offers, bot_belief,
            rounds_remaining, physical_pouch_count, round_number, mode)
        outputs[mode] = [asdict(offer) for offer in offers]
        atomic_write_json(Path(output_directory) / f"replay_price_file_{mode}.json", outputs[mode])
    return outputs


def load_market_config(path=ROOT / "market_config.json"):
    config = read_json(path)
    if not isinstance(config, dict):
        raise ValueError("A market configuration JSON object is required")
    skus = {key: SKUState(**value) for key, value in config["skus"].items()}
    competitors = [CompetitorOffer(**value) for value in config.get("competitor_offers", [])]
    for offer in competitors:
        if not isinstance(offer.seller_id, str) or not offer.seller_id.strip() or offer.sku_id not in SKU_IDS:
            raise ValueError("Competitor offers need a seller identifier and a recognized SKU")
        number(offer.unit_price, "competitor price", 0.01)
        count(offer.virtual_available, "competitor stock")
    belief = BotPopulationBelief(**config.get("bot_belief", {})).normalized()
    engine = DualMarketPricingEngine(enable_prime_discount_offers=config.get("enable_prime_discount_offers", True))
    engine._validate_skus(skus)
    count(config["round_number"], "round_number", 1)
    count(config["rounds_remaining"], "rounds_remaining", 1)
    count(config["physical_pouch_count"], "physical_pouch_count")
    own_names = config.get("seller_aliases", []) + [config["seller_id"]]
    if not all(isinstance(name, str) and canonical_seller(name) for name in own_names):
        raise ValueError("Seller identifiers must contain letters or digits")
    own_aliases = {canonical_seller(name) for name in own_names}
    competitors = [offer for offer in competitors if canonical_seller(offer.seller_id) not in own_aliases]
    return config, skus, competitors, belief, engine


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "market_config.json")
    parser.add_argument("--replays", action="store_true", help="Write replay inputs without publishing")
    args = parser.parse_args()
    if not args.replays:
        parser.error("For catalog updates use: python3 -m automation.run")
    with exclusive_project_lock(args.config.resolve().parent):
        output_root = args.config.resolve().parent
        config, skus, competitors, belief, engine = load_market_config(args.config)
        checkpoint = read_json(output_root / "data/automation_state.json", {})
        if checkpoint.get("pending_publication") is not None:
            raise RuntimeError("Resume the pending publication before generating replay inputs")
        for key, remaining in checkpoint.get("buyer_report", {}).get("remaining_virtual", {}).items():
            if key in skus:
                skus[key].inventory_virtual = count(remaining, "buyer stock")
        if "competitor_offers" in checkpoint:
            competitors = [CompetitorOffer(**offer) for offer in checkpoint["competitor_offers"]]
        generate_replay_comparison_files(engine, skus, competitors, belief, config["rounds_remaining"],
            config["physical_pouch_count"], config["round_number"], output_directory=output_root / "replays")
        print("Replay inputs written to replays/. No purchases were simulated.")


if __name__ == "__main__":
    main()
