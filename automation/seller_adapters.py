"""Site-specific adapters for the three public class seller feeds.

Only explicitly mapped, unrestricted single-soldier offers are comparable.
Unknown stock is preserved as unknown, not converted into a stock count.
"""

from datetime import datetime, timezone
from html.parser import HTMLParser

from pricing_engine import CompetitorOffer, SKU_IDS, count, number


def _mapping(source):
    mapping = source.get("offer_ids")
    if not isinstance(mapping, dict) or not mapping or not set(mapping).issubset(SKU_IDS):
        raise ValueError("Seller feed needs offer_ids mapped to our SKUs")
    if (any(not isinstance(v, str) or not v for v in mapping.values())
            or len(set(mapping.values())) != len(mapping)):
        raise ValueError("Each SKU needs a different seller offer ID")
    return mapping


def _index(rows, key):
    if not isinstance(rows, list):
        raise ValueError("Seller feed is missing its list of offers or products")
    indexed = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get(key), str) or not row[key]:
            raise ValueError(f"Seller entries need a {key}")
        if row[key] in indexed:
            raise ValueError(f"Seller feed has a duplicate {key}")
        indexed[row[key]] = row
    return indexed


def _mapped(indexed, offer_id):
    if offer_id not in indexed:
        raise ValueError(f"Mapped seller offer is missing: {offer_id}")
    return indexed[offer_id]


def _check_period(row, start, end):
    # Reuse the timezone-aware validation used by the existing importer.
    from automation.sources import parse_time
    now = datetime.now(timezone.utc)
    if parse_time(row.get(start)) > now:
        raise ValueError("Seller offer is not yet effective")
    if row.get(end) is not None and parse_time(row[end]) <= now:
        raise ValueError("Seller offer has expired")


def _offer(source, sku, price, stock, status=None):
    if stock is None:
        status = status or "unknown"
    else:
        stock = count(stock, "seller simulated stock")
        status = "known"
    return CompetitorOffer(source["seller_id"], sku,
                           number(price, "seller price", 0.01), stock, status)


def parse_pocket_soldier(document, source):
    if not isinstance(document, dict) or document.get("seller_id") != "pocket-platoon":
        raise ValueError("Unexpected Pocket Soldier feed identity")
    rows = _index(document.get("offers"), "offer_id")
    results = []
    for sku, offer_id in _mapping(source).items():
        row = _mapped(rows, offer_id)
        quantities = row.get("quantities")
        if (quantities != {"standard-soldier": 1}
                or type(quantities.get("standard-soldier")) is not int
                or row.get("product_id") != "standard-soldier"):
            raise ValueError(f"{offer_id} is not one standard soldier")
        if (row.get("requires_seller_code") not in (None, False)
                or row.get("parent_offer_id") is not None
                or not isinstance(row.get("eligibility_terms"), str)
                or not row["eligibility_terms"].strip().casefold().startswith("open to all customers")):
            raise ValueError(f"{offer_id} is not an unrestricted base offer")
        if row.get("currency") != "USD":
            raise ValueError(f"{offer_id} currency must be USD")
        _check_period(row, "effective_from", "expires_at")
        results.append(_offer(source, sku, row.get("total_price"),
                              row.get("simulated_available_quantity")))
    return results


class _VisibleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ignored = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.ignored += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self.ignored:
            self.ignored -= 1

    def handle_data(self, data):
        if not self.ignored:
            self.parts.append(data)


def parse_troopers(document, availability_html, source):
    if (not isinstance(document, dict) or not isinstance(document.get("price_snapshot_id"), str)
            or not document["price_snapshot_id"]):
        raise ValueError("Troopers catalog needs a price snapshot ID")
    rows = _index(document.get("products"), "id")
    parser = _VisibleText()
    parser.feed(availability_html)
    visible = " ".join(" ".join(parser.parts).split()).casefold()
    # This is a seller claim, not a verified count from the buyer ledger.
    status = ("seller_claimed_unlimited" if
              "unlimited supply for all available products" in visible else "unknown")
    results = []
    for sku, product_id in _mapping(source).items():
        row = _mapped(rows, product_id)
        components = row.get("components")
        if (type(row.get("quantity")) is not int or row["quantity"] != 1
                or not isinstance(components, dict) or len(components) != 1
                or type(next(iter(components.values()))) is not int
                or next(iter(components.values())) != 1):
            raise ValueError(f"{product_id} is not a definite single-soldier offer")
        results.append(_offer(source, sku, row.get("price"), None, status))
    return results


def parse_toy_soldier_stand(document, source):
    if (not isinstance(document, dict)
            or document.get("format") != "toy-soldier-price-file/1"
            or document.get("seller") != "Toy Soldier Stand"
            or document.get("currency") != "USD"):
        raise ValueError("Unexpected Toy Soldier Stand feed identity, format, or currency")
    offers = _index(document.get("offers"), "offer_id")
    products = _index(document.get("products"), "product_id")
    results = []
    for sku, offer_id in _mapping(source).items():
        row = _mapped(offers, offer_id)
        items = row.get("items")
        if (not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict)
                or type(items[0].get("quantity")) is not int or items[0]["quantity"] != 1
                or not isinstance(items[0].get("product_id"), str)):
            raise ValueError(f"{offer_id} is not a single-soldier offer")
        if (row.get("eligibility") != "all" or not isinstance(row.get("markets"), list)
                or "simulated" not in row["markets"]):
            raise ValueError(f"{offer_id} is not an unrestricted simulated offer")
        _check_period(row, "effective_from", "effective_until")
        product = _mapped(products, items[0]["product_id"])
        results.append(_offer(source, sku, row.get("total_price"), product.get("simulated_available")))
    return results
