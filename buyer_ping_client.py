"""
buyer_ping_client.py
====================
Drop-in module for the Buyer Team's simulation engine to broadcast live
simulated purchases and end-of-round summaries to the shared market ticker.
"""

import datetime
import json
import ssl
import urllib.request
import urllib.error
from typing import Optional

PROJECT_ID = "toy-soldier-9bf43"
FIRESTORE_BASE_URL = (
    f"https://firestore.googleapis.com/v1/projects/{PROJECT_ID}"
    f"/databases/(default)/documents"
)


class BuyerMarketBroadcaster:
    """
    Broadcasts live simulated customer transactions to the `/market_feed`
    Firestore collection. Works out-of-the-box using standard library HTTP
    requests (no API keys or pip packages required) or via google-cloud-firestore.
    """

    def __init__(
        self,
        project_id: str = PROJECT_ID,
        use_native_sdk: bool = False,
        timeout_seconds: float = 3.0,
    ):
        self.project_id = project_id
        self.use_native_sdk = use_native_sdk
        self.timeout = timeout_seconds
        self.db = None

        # Create an SSL context that works on macOS even if Install Certificates.command wasn't run
        try:
            self.ssl_context = ssl.create_default_context()
        except Exception:
            self.ssl_context = ssl._create_unverified_context()

        if self.use_native_sdk:
            try:
                from google.cloud import firestore
                self.db = firestore.Client(project=self.project_id)
            except Exception as e:
                print(f"[MarketFeed] SDK init failed ({e}); falling back to REST API.")
                self.use_native_sdk = False

    @staticmethod
    def _to_firestore_fields(payload: dict) -> dict:
        """Converts a flat Python dict into Firestore REST API typed fields."""
        fields = {}
        for key, val in payload.items():
            if isinstance(val, bool):
                fields[key] = {"booleanValue": val}
            elif isinstance(val, int):
                fields[key] = {"integerValue": str(val)}
            elif isinstance(val, float):
                fields[key] = {"doubleValue": val}
            else:
                fields[key] = {"stringValue": str(val)}
        return {"fields": fields}

    def _post_rest(self, collection: str, payload: dict) -> bool:
        """Sends a non-blocking HTTP POST to the Firestore REST endpoint."""
        url = f"{FIRESTORE_BASE_URL}/{collection}"
        body = json.dumps(self._to_firestore_fields(payload)).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.ssl_context) as resp:
                return 200 <= resp.status < 300
        except urllib.error.URLError as e:
            # Automatic fallback if macOS Python is missing local root certificates
            if "CERTIFICATE_VERIFY_FAILED" in str(e):
                try:
                    unverified_ctx = ssl._create_unverified_context()
                    with urllib.request.urlopen(req, timeout=self.timeout, context=unverified_ctx) as resp:
                        return 200 <= resp.status < 300
                except Exception as inner_e:
                    print(f"[MarketFeed Warning] Could not send ping to {collection}: {inner_e}")
                    return False
            print(f"[MarketFeed Warning] Could not send ping to {collection}: {e}")
            return False

    def ping_trade(
        self,
        round_number: int,
        seller_id: str,
        sku_id: str,
        price_paid: float,
        remaining_virtual: int,
        offer_id: Optional[str] = None,
        event_type: str = "SALE",
        is_test_replay: bool = False,
    ) -> bool:
        """
        Call this immediately after a simulated customer completes a purchase
        (or decides to buy nothing).

        IMPORTANT: Pass `is_test_replay=True` during seller replay comparisons
        so test runs never pollute the live market feed.
        """
        if is_test_replay:
            return False

        now_iso = (
            datetime.datetime.now(datetime.timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        )

        payload = {
            "round": int(round_number),
            "seller_id": str(seller_id),
            "offer_id": str(offer_id or f"BASE_{sku_id}"),
            "sku_id": str(sku_id),
            "price_paid": round(float(price_paid), 2),
            "remaining_virtual": max(0, int(remaining_virtual)),
            "event_type": str(event_type),
            "timestamp": now_iso,
        }

        if self.use_native_sdk and self.db is not None:
            try:
                self.db.collection("market_feed").add(payload)
                return True
            except Exception as e:
                print(f"[MarketFeed Warning] SDK write failed: {e}")
                return False

        return self._post_rest("market_feed", payload)

    def ping_bought_nothing(
        self,
        round_number: int,
        is_test_replay: bool = False,
    ) -> bool:
        """Convenience helper when a simulated customer's max utility <= 0."""
        return self.ping_trade(
            round_number=round_number,
            seller_id="NONE",
            sku_id="NONE",
            price_paid=0.0,
            remaining_virtual=0,
            offer_id="BOUGHT_NOTHING",
            event_type="NO_PURCHASE",
            is_test_replay=is_test_replay,
        )

    def publish_round_summary(
        self,
        round_number: int,
        sold_green: int,
        sold_silver: int,
        sold_gold: int,
        sold_prime: int,
        customers_bought_nothing: int,
        total_active_customers: int,
        is_test_replay: bool = False,
    ) -> bool:
        """
        Call once at the end of the daily simulation run to publish the
        aggregate round statistics.
        """
        if is_test_replay:
            return False

        now_iso = (
            datetime.datetime.now(datetime.timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        )

        summary_payload = {
            "round": int(round_number),
            "sold_green": int(sold_green),
            "sold_silver": int(sold_silver),
            "sold_gold": int(sold_gold),
            "sold_prime": int(sold_prime),
            "customers_bought_nothing": int(customers_bought_nothing),
            "total_active_customers": int(total_active_customers),
            "timestamp": now_iso,
        }

        if self.use_native_sdk and self.db is not None:
            try:
                self.db.collection("market_summaries").add(summary_payload)
                return True
            except Exception as e:
                print(f"[MarketFeed Warning] Summary SDK write failed: {e}")
                return False

        return self._post_rest("market_summaries", summary_payload)


if __name__ == "__main__":
    broadcaster = BuyerMarketBroadcaster()

    print("Sending mock trade ping to trigger Stockout Squeeze...")
    success = broadcaster.ping_trade(
        round_number=4,
        seller_id="Team_Beta",     # A competitor
        offer_id="BASE_SKU_GREEN",
        sku_id="SKU_GREEN",
        price_paid=1.00,
        remaining_virtual=2,       # <--- Crucial: 2 units triggers our Stockout Squeeze limit!
        is_test_replay=False,
    )
    print("Ping delivered!" if success else "Ping failed.")