import json
import ssl
import urllib.request
import urllib.error
import datetime
from typing import Optional

class BuyerMarketBroadcaster:
    def __init__(self, project_id: str = "toy-soldier-9bf43", timeout_seconds: float = 3.0):
        self.url = f"https://firestore.googleapis.com/v1/projects/{project_id}/databases/(default)/documents/market_feed"
        self.timeout = timeout_seconds
        # Enforce strict SSL verification; no unverified fallback
        self.ssl_ctx = ssl.create_default_context()

    def _format_for_firestore(self, payload: dict) -> dict:
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

    def ping_trade(
        self,
        round_number: int,
        seller_id: str,
        sku_id: str,
        price_paid: float,
        remaining_virtual: int,
        offer_id: Optional[str] = None,
        is_test_replay: bool = False,
    ) -> bool:
        
        if is_test_replay:
            return False

        payload = {
            "round": int(round_number),
            "seller_id": str(seller_id),
            "offer_id": str(offer_id or f"BASE_{sku_id}"),
            "sku_id": str(sku_id),
            "price_paid": round(float(price_paid), 2),
            "remaining_virtual": max(0, int(remaining_virtual)),
            "timestamp": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        }

        body = json.dumps(self._format_for_firestore(payload)).encode("utf-8")
        req = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        
        # Broad exception wrapper prevents timeouts from escaping to the buyer's loop
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.ssl_ctx) as resp:
                return 200 <= resp.status < 300
        except Exception as e:
            print(f"[MarketFeed Warning] Ping failed or timed out: {e}")
            return False

if __name__ == "__main__":
    print("BuyerMarketBroadcaster initialized.")
    print("Automated test pings from the helper are disabled to prevent live-market pollution.")