"""Standard-library notifications for already committed simulated purchases.

A notification is not a purchase or a stock ledger. Reuse transaction_id and the
accepted values when retrying. Certificate verification is always enabled.
"""

import hashlib
import json
import logging
import math
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

LOGGER = logging.getLogger(__name__)
SKU_IDS = ("SKU_GREEN", "SKU_SILVER", "SKU_GOLD")


def parse_timestamp(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("A timezone-aware timestamp is required")
    return value.astimezone(timezone.utc)


def validate_trade(payload):
    """Validate without converting malformed values or clamping negative stock."""
    if not isinstance(payload, dict):
        raise ValueError("A trade must be a JSON object")
    for key in ("round", "remaining_virtual"):
        value = payload.get(key)
        minimum = 1 if key == "round" else 0
        if type(value) is not int or value < minimum:
            raise ValueError(f"{key} must be an integer of at least {minimum}")
    for key in ("seller_id", "offer_id", "sku_id"):
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > 200:
            raise ValueError(f"{key} must be a nonempty identifier of at most 200 characters")
        if any(ord(char) < 32 for char in value):
            raise ValueError(f"{key} contains control characters")
    if payload["seller_id"].strip().upper() == "NONE" or payload["sku_id"] not in SKU_IDS:
        raise ValueError("Unknown seller or simulated SKU")
    price = payload.get("price_paid")
    if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
        raise ValueError("price_paid must be a positive finite number")
    if payload.get("market", "simulated") != "simulated" or payload.get("is_test_replay", False) is not False:
        raise ValueError("Only official simulated purchases belong in market_feed")
    if type(payload.get("quantity", 1)) is not int or payload.get("quantity", 1) != 1:
        raise ValueError("This feed supports single-unit offers only")
    parse_timestamp(payload.get("timestamp"))
    return payload


class BuyerMarketBroadcaster:
    def __init__(self, project_id="toy-soldier-9bf43", timeout_seconds=3.0, token_provider=None):
        if not isinstance(project_id, str) or not re.fullmatch(r"[a-z][a-z0-9-]{4,62}", project_id):
            raise ValueError("Invalid Firebase project ID")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise ValueError("timeout_seconds must be a positive number")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        self.url = (f"https://firestore.googleapis.com/v1/projects/{project_id}"
                    "/databases/(default)/documents/market_feed")
        self.timeout = timeout_seconds
        self.token_provider = token_provider
        try:
            import certifi
        except ImportError:
            self.ssl_ctx = ssl.create_default_context()
        else:
            self.ssl_ctx = ssl.create_default_context(cafile=certifi.where())

    @staticmethod
    def _format_for_firestore(payload):
        fields = {}
        for key, value in payload.items():
            if isinstance(value, bool):
                fields[key] = {"booleanValue": value}
            elif isinstance(value, int):
                fields[key] = {"integerValue": str(value)}
            elif isinstance(value, float):
                fields[key] = {"doubleValue": value}
            elif isinstance(value, str):
                fields[key] = {"stringValue": value}
            else:
                raise ValueError(f"Unsupported Firestore value for {key}")
        return {"fields": fields}

    def ping_trade(self, round_number, seller_id, sku_id, price_paid,
                   remaining_virtual, offer_id=None, is_test_replay=False, *,
                   transaction_id=None, occurred_at=None):
        if type(is_test_replay) is not bool:
            raise ValueError("is_test_replay must be a boolean")
        if is_test_replay:
            return False
        payload = {
            "round": round_number, "seller_id": seller_id,
            "offer_id": offer_id or f"BASE_{sku_id}", "sku_id": sku_id,
            "price_paid": price_paid, "remaining_virtual": remaining_virtual,
            "market": "simulated", "quantity": 1,
            "timestamp": parse_timestamp(occurred_at or datetime.now(timezone.utc))
                .isoformat(timespec="microseconds").replace("+00:00", "Z"),
        }
        validate_trade(payload)
        payload["price_paid"] = round(float(price_paid), 2)
        if payload["price_paid"] <= 0:
            raise ValueError("price_paid must be at least one cent")
        identity = {key: value for key, value in payload.items() if key != "timestamp"}
        if transaction_id is not None:
            if not isinstance(transaction_id, str) or not transaction_id.strip() or len(transaction_id) > 200:
                raise ValueError("transaction_id must be a nonempty string of at most 200 characters")
            identity = {"seller_id": seller_id, "round": round_number, "transaction_id": transaction_id}
        event_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()
        payload["event_id"] = event_id
        body = json.dumps(self._format_for_firestore(payload), allow_nan=False).encode("utf-8")
        try:
            headers = {"Content-Type": "application/json"}
            if self.token_provider is not None:
                try:
                    token = self.token_provider()
                except Exception:
                    LOGGER.warning("Market authorization token unavailable; retain the trade for retry")
                    return False
                if not isinstance(token, str) or not token.strip():
                    raise ValueError("token_provider must return a current authorization token")
                headers["Authorization"] = f"Bearer {token}"
            request = urllib.request.Request(
                self.url + "?" + urllib.parse.urlencode({"documentId": event_id}),
                data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=self.timeout, context=self.ssl_ctx) as response:
                    return 200 <= response.status < 300
            except urllib.error.HTTPError as error:
                if error.code != 409:
                    LOGGER.warning("Market notification rejected (HTTP %s)", error.code)
                    return False
                # A retry may already exist. Verify the business values rather
                # than accepting an ID reused for a different purchase.
                existing = urllib.request.Request(self.url + "/" + event_id, headers=headers)
                with urllib.request.urlopen(existing, timeout=self.timeout, context=self.ssl_ctx) as response:
                    document = json.load(response)
                if not isinstance(document, dict) or not isinstance(document.get("fields"), dict):
                    return False
                fields = document["fields"]
                expected = self._format_for_firestore(payload)["fields"]
                return all(fields.get(key) == value for key, value in expected.items() if key != "timestamp")
        except (OSError, ValueError, urllib.error.URLError):
            LOGGER.warning("Market notification failed; retain the committed trade for retry")
            return False


if __name__ == "__main__":
    print("Import BuyerMarketBroadcaster in the buyer program. No live test notification was sent.")
