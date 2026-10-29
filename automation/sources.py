"""Read the buyer's results and extract comparable public competitor offers."""

import hashlib
import json
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

from pricing_engine import CompetitorOffer, SKU_IDS, canonical_seller, count, number, utc_now

MAX_SOURCE_BYTES = 2 * 1024 * 1024


def parse_time(value):
    if not isinstance(value, str):
        raise ValueError("A timezone-aware ISO timestamp is required")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("A timezone-aware ISO timestamp is required") from error
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("Timestamp must include a timezone")
    return stamp.astimezone(timezone.utc)


def fetch_url(url, timeout):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Source URL must be an HTTP(S) URL without embedded credentials")
    request = urllib.request.Request(url, headers={
        "User-Agent": "VanguardGarrisonMarketResearch/1.0",
        "Accept": "application/json, text/html;q=0.9, */*;q=0.5",
        "Cache-Control": "no-cache",
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        content = response.read(MAX_SOURCE_BYTES + 1)
    if len(content) > MAX_SOURCE_BYTES:
        raise ValueError("Source is too large")
    return content


def read_buyer_source(source, root, timeout):
    if not isinstance(source, dict):
        raise ValueError("buyer_results must be a source object")
    if source.get("type") == "file":
        name = source.get("path")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("buyer_results needs a file path")
        path = (Path(root) / name).resolve()
        if path.is_relative_to((Path(root) / "public").resolve()):
            raise ValueError("Buyer results must be outside public/")
        content = path.read_bytes()
        if len(content) > MAX_SOURCE_BYTES:
            raise ValueError("Buyer results are too large")
    elif source.get("type") == "url":
        if not isinstance(source.get("url"), str):
            raise ValueError("buyer_results needs a URL")
        content = fetch_url(source["url"], timeout)
    else:
        raise ValueError("buyer_results type must be file or url")
    try:
        report = json.loads(content.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Buyer results must be valid UTF-8 JSON") from error
    return report


def validate_buyer_report(report, config, previous, increase_reason=None):
    if not isinstance(report, dict):
        raise ValueError("Buyer results must be a JSON object")
    report_id = report.get("report_id")
    seller = report.get("seller_id")
    if not isinstance(report_id, str) or not report_id.strip() or len(report_id) > 200:
        raise ValueError("Buyer results need a nonempty report_id")
    if not isinstance(seller, str) or canonical_seller(seller) not in {
        canonical_seller(name) for name in config.get("seller_aliases", []) + [config["seller_id"]]
    }:
        raise ValueError("Buyer report seller_id does not match this seller")
    round_number = count(report.get("round"), "buyer report round", 1)
    if round_number > config["round_number"]:
        raise ValueError("Buyer report round is newer than market_config.json")
    reported_at = parse_time(report.get("reported_at"))
    stock = report.get("remaining_virtual")
    if not isinstance(stock, dict) or set(stock) != set(SKU_IDS):
        raise ValueError("Buyer report must include remaining_virtual for all three soldier SKUs")
    stock = {sku: count(stock[sku], f"buyer {sku} stock") for sku in SKU_IDS}
    transactions = report.get("transactions", [])
    if not isinstance(transactions, list):
        raise ValueError("Buyer report transactions must be an array")
    digest = hashlib.sha256(json.dumps(report, sort_keys=True, ensure_ascii=False,
                                       separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
    if previous.get("report_id"):
        old_time = parse_time(previous["reported_at"])
        if round_number < previous["round"] or reported_at < old_time:
            raise ValueError("Buyer report is older than the last accepted report")
        if (report_id == previous["report_id"] or reported_at == old_time) and digest != previous["sha256"]:
            raise ValueError("A changed buyer report needs a new report_id and later reported_at")
    prior_stock = previous.get("remaining_virtual")
    if prior_stock is not None:
        for sku in SKU_IDS:
            old = count(prior_stock[sku], f"previous {sku} stock")
            if stock[sku] > old and not (isinstance(increase_reason, str) and increase_reason.strip()):
                raise ValueError(f"{sku} stock increased; confirm the correction with --allow-increase-reason")
    return {
        "report_id": report_id, "round": round_number,
        "reported_at": reported_at.isoformat().replace("+00:00", "Z"),
        "sha256": digest, "remaining_virtual": stock,
        "transaction_count": len(transactions),
    }


def parse_price_file(document, source):
    rows = document.get("offers") if isinstance(document, dict) else document
    if not isinstance(rows, list):
        raise ValueError("Competitor price file must be an offer array or an object with offers")
    by_id = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("offer_id"), str):
            raise ValueError("Competitor offers must have offer_id values")
        if row["offer_id"] in by_id:
            raise ValueError("Duplicate competitor offer_id")
        by_id[row["offer_id"]] = row
    mapping = source.get("offer_ids")
    if not isinstance(mapping, dict) or not mapping or not set(mapping).issubset(SKU_IDS):
        raise ValueError("price_file source needs offer_ids mapped to our SKUs")
    if any(not isinstance(value, str) or not value for value in mapping.values()) or len(set(mapping.values())) != len(mapping):
        raise ValueError("Each SKU must map to a different competitor offer ID")
    allowed = source.get("unrestricted_eligibility", ["ALL_CUSTOMERS", "ALL_CUSTOMERS_ONE_ITEM_LIMIT"])
    if not isinstance(allowed, list) or not allowed or not all(isinstance(v, str) and v.strip() for v in allowed):
        raise ValueError("unrestricted_eligibility must list public offer eligibility values")
    allowed = {v.strip().casefold() for v in allowed}
    results = []
    for sku, offer_id in mapping.items():
        if not isinstance(offer_id, str) or offer_id not in by_id:
            raise ValueError(f"Mapped offer missing for {sku}")
        row = by_id[offer_id]
        quantities = row.get("product_quantities")
        if (not isinstance(quantities, dict) or len(quantities) != 1
                or type(next(iter(quantities.values()))) is not int
                or next(iter(quantities.values())) != 1):
            raise ValueError(f"{offer_id} must be a single-item offer")
        eligibility = row.get("eligibility")
        if not isinstance(eligibility, str) or eligibility.strip().casefold() not in allowed:
            raise ValueError(f"{offer_id} is not marked as an unrestricted offer")
        effective = row.get("timestamp", row.get("effective_at", row.get("effective_time")))
        if parse_time(effective) > datetime.now(timezone.utc):
            raise ValueError(f"{offer_id} is not yet effective")
        expiry = row.get("expires_at", row.get("expiration_time"))
        if expiry is not None and parse_time(expiry) <= datetime.now(timezone.utc):
            raise ValueError(f"{offer_id} has expired")
        results.append(CompetitorOffer(source["seller_id"], sku,
            number(row.get("total_price"), f"{offer_id} price", 0.01),
            count(row.get("virtual_available"), f"{offer_id} virtual stock")))
    return results


class IdElementParser(HTMLParser):
    """Collect text and attributes for uniquely identified HTML elements."""

    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, wanted):
        super().__init__(convert_charrefs=True)
        self.wanted = wanted
        self.elements = {}
        self.stack = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        capture = None
        element_id = attrs.get("id")
        if element_id in self.wanted:
            if element_id in self.elements:
                raise ValueError(f"HTML element ID is duplicated: {element_id}")
            capture = {"attributes": attrs, "text": []}
            self.elements[element_id] = capture
        if tag not in self.VOID_TAGS:
            self.stack.append((tag, capture))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        for _, capture in self.stack:
            if capture is not None:
                capture["text"].append(data)


def parse_html(document, source):
    items = source.get("items")
    if not isinstance(items, dict) or not items or not set(items).issubset(SKU_IDS):
        raise ValueError("HTML source needs items mapped to our SKUs")
    wanted = set()
    for item in items.values():
        if not isinstance(item, dict):
            raise ValueError("HTML item settings must be objects")
        for key in ("price_element_id", "virtual_stock_element_id"):
            value = item.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"HTML item needs {key}")
            wanted.add(value)
    parser = IdElementParser(wanted)
    parser.feed(document)

    def element_value(item, prefix):
        element_id = item[prefix + "_element_id"]
        if element_id not in parser.elements:
            raise ValueError(f"Missing HTML element: {element_id}")
        element = parser.elements[element_id]
        attr = item.get(prefix + "_attribute")
        if attr is not None:
            if not isinstance(attr, str) or attr not in element["attributes"]:
                raise ValueError(f"Missing attribute on HTML element: {element_id}")
            value = element["attributes"][attr]
            if not isinstance(value, str):
                raise ValueError(f"HTML attribute has no value: {element_id}")
            return value
        return " ".join(element["text"]).strip()

    results = []
    for sku, item in items.items():
        price_text = element_value(item, "price")
        stock_text = element_value(item, "virtual_stock")
        price_match = re.fullmatch(r"\s*\$?\s*(\d+(?:\.\d{1,2})?)\s*", price_text)
        stock_match = re.fullmatch(r"\s*(\d+)\s*(?:units?|in stock|available)?\s*", stock_text, re.I)
        if not price_match or not stock_match:
            raise ValueError(f"Could not read an unambiguous price and virtual stock for {sku}")
        results.append(CompetitorOffer(source["seller_id"], sku,
            number(float(price_match.group(1)), f"{sku} price", 0.01), int(stock_match.group(1))))
    return results


def collect_competitors(sources, own_names, timeout):
    if not isinstance(sources, list) or not sources:
        raise ValueError("Add at least one real competitor in config/automation.json")
    own_aliases = {canonical_seller(name) for name in own_names}
    seen = set()
    results, metadata = [], []
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError("Each competitor source must be an object")
        seller, url = source.get("seller_id"), source.get("url")
        if not isinstance(seller, str) or not canonical_seller(seller) or not isinstance(url, str):
            raise ValueError("Each competitor needs seller_id and url")
        key = canonical_seller(seller)
        if key in own_aliases or key in seen:
            raise ValueError("Competitor seller IDs must be unique and different from our seller")
        seen.add(key)
        content = fetch_url(url, timeout)
        try:
            text = content.decode("utf-8-sig")
            if source.get("format") == "price_file":
                offers = parse_price_file(json.loads(text), source)
            elif source.get("format") == "html":
                offers = parse_html(text, source)
            else:
                raise ValueError("Competitor format must be price_file or html")
        except (UnicodeError, json.JSONDecodeError) as error:
            raise ValueError(f"Could not read competitor source for {seller}") from error
        results.extend(offers)
        parsed = urllib.parse.urlsplit(url)
        metadata.append({"seller_id": seller, "url": urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")),
                         "checked_at": utc_now(), "sha256": hashlib.sha256(content).hexdigest()})
    return results, metadata
