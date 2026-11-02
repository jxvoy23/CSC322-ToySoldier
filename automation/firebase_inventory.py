"""Import buyer CSV or JSON sales once and keep inventory in Firestore.

The original Brigade Exchange CSV omits quantities and globally unique event IDs.
It must be extended with Units_Bought, Event_ID and ISO_Timestamp before it can
authorize inventory changes. Never infer quantity from the total price.
"""

import copy
import csv
import hashlib
import io
import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from automation.sources import MAX_SOURCE_BYTES, fetch_url, parse_time
from pricing_engine import SKU_IDS, canonical_seller, count, number, utc_now

OFFER_SKUS = {
    "BASE_SKU_GREEN": "SKU_GREEN", "BASE_SKU_SILVER": "SKU_SILVER",
    "PRIME_SKU_SILVER": "SKU_SILVER", "BASE_SKU_GOLD": "SKU_GOLD",
    "PRIME_SKU_GOLD": "SKU_GOLD",
}
MAX_MATCHING_EVENTS = 200


def parse_csv(content, seller_aliases):
    """Return validated seller events without buyer names or customer profiles."""
    if len(content) > MAX_SOURCE_BYTES:
        raise ValueError("Buyer CSV is too large")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeError as error:
        raise ValueError("Buyer CSV must be UTF-8") from error
    reader = csv.DictReader(io.StringIO(text, newline=""))
    required = {"Event_ID", "ISO_Timestamp", "Selected_Seller", "Offer_ID", "Units_Bought", "Status"}
    if not reader.fieldnames or len(reader.fieldnames) != len(set(reader.fieldnames)):
        raise ValueError("Buyer CSV must have unique column names")
    missing = required - set(reader.fieldnames)
    if missing:
        raise ValueError("Buyer CSV is missing required columns: " + ", ".join(sorted(missing))
                         + ". Ask the buyer team to include event ID, ISO timestamp and units bought.")
    aliases = {canonical_seller(name) for name in seller_aliases}
    events = {}
    for line, row in enumerate(reader, start=2):
        if None in row:
            raise ValueError(f"Buyer CSV line {line} has too many fields")
        seller = (row.get("Selected_Seller") or "").strip()
        if canonical_seller(seller) not in aliases:
            continue
        status = (row.get("Status") or "").strip().upper()
        if status != "PURCHASED":
            raise ValueError(f"Buyer CSV line {line} has an unsupported seller transaction status")
        event_id = (row.get("Event_ID") or "").strip()
        offer_id = (row.get("Offer_ID") or "").strip()
        raw_quantity = (row.get("Units_Bought") or "").strip()
        if not event_id or len(event_id) > 200:
            raise ValueError(f"Buyer CSV line {line} needs a stable unique Event_ID")
        if offer_id not in OFFER_SKUS:
            raise ValueError(f"Buyer CSV line {line} has an unknown or physical-only offer ID")
        if not raw_quantity.isascii() or not raw_quantity.isdigit() or int(raw_quantity) < 1:
            raise ValueError(f"Buyer CSV line {line} needs a positive whole Units_Bought")
        timestamp = parse_time((row.get("ISO_Timestamp") or "").strip())
        if timestamp > datetime.now(timezone.utc):
            raise ValueError(f"Buyer CSV line {line} has a future transaction timestamp")
        event = {"event_id": event_id, "offer_id": offer_id, "sku_id": OFFER_SKUS[offer_id],
                 "quantity": int(raw_quantity), "timestamp": timestamp.isoformat().replace("+00:00", "Z")}
        prior = events.get(event_id)
        if prior is not None and prior != event:
            raise ValueError(f"Buyer CSV line {line} reuses an Event_ID with different sale details")
        events[event_id] = event
    if len(events) > MAX_MATCHING_EVENTS:
        raise ValueError(f"Buyer CSV exceeds {MAX_MATCHING_EVENTS} seller sales; split the export")
    return list(events.values())


def parse_buyer_json(content, seller_aliases):
    """Read a seller-scoped API ledger or the buyer page's JSON ledger shape.

    Only sale fields are retained. Ask the buyer team for a filtered official
    ledger; their full export also contains private customer information.
    """
    if len(content) > MAX_SOURCE_BYTES:
        raise ValueError("Buyer JSON is too large")
    try:
        document = json.loads(content.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Buyer results must be valid UTF-8 JSON") from error
    if isinstance(document, list):
        rows = document
    elif isinstance(document, dict):
        collections = document.get("collections")
        if isinstance(collections, dict) and "ledgers" in collections:
            rows = collections["ledgers"]
        else:
            rows = document.get("transactions", document.get("ledger"))
    else:
        rows = None
    if not isinstance(rows, list):
        raise ValueError("Buyer JSON must contain transactions, ledger, or collections.ledgers")

    header = ["Event_ID", "ISO_Timestamp", "Selected_Seller", "Offer_ID", "Units_Bought", "Status"]
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=header)
    writer.writeheader()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Each buyer JSON ledger entry must be an object")
        event_id = row.get("event_id", row.get("_id"))
        if isinstance(event_id, dict):
            event_id = event_id.get("$oid")
        writer.writerow({"Event_ID": event_id, "ISO_Timestamp": row.get("isoTimestamp", row.get("timestamp")),
                         "Selected_Seller": row.get("seller_id", row.get("sellerName")),
                         "Offer_ID": row.get("offer_id", row.get("offerId")),
                         "Units_Bought": row.get("quantity", row.get("unitsBought")),
                         "Status": row.get("status")})
    try:
        return parse_csv(out.getvalue().encode("utf-8"), seller_aliases)
    except ValueError as error:
        raise ValueError(f"Invalid buyer JSON ledger: {error}") from error


def load_buyer_input(source, root, timeout):
    kind = source.get("type")
    field = "json" if kind == "json_firestore" else "csv" if kind == "csv_firestore" else None
    if field is None:
        raise ValueError("Buyer source must be csv_firestore or json_firestore")
    location = source.get(field)
    if not isinstance(location, dict):
        raise ValueError(f"{kind} needs a {field} source object")
    if location.get("type") == "file":
        name = location.get("path")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Buyer results need a file path")
        path = (Path(root) / name).resolve()
        if path.is_relative_to((Path(root) / "public").resolve()):
            raise ValueError("Buyer results must be outside public/")
        if not path.exists():
            return None
        content = path.read_bytes()
    elif location.get("type") == "url":
        url = location.get("url")
        if not isinstance(url, str):
            raise ValueError("Buyer results need a direct download URL")
        auth_env = location.get("bearer_token_env")
        if auth_env is None:
            content = fetch_url(url, timeout)
        else:
            if not isinstance(auth_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", auth_env):
                raise ValueError("bearer_token_env must name an environment variable")
            token = os.environ.get(auth_env)
            if not token or "\n" in token or "\r" in token:
                raise ValueError(f"Buyer API token environment variable {auth_env} is missing or invalid")
            parsed = urllib.parse.urlsplit(url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("Authenticated buyer API must be an HTTPS URL without embedded credentials")

            class SameOriginRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, headers, newurl):
                    old, new = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(newurl)
                    if (old.scheme, old.netloc) != (new.scheme, new.netloc):
                        raise ValueError("Buyer API redirected to a different origin")
                    return super().redirect_request(req, fp, code, msg, headers, newurl)

            request = urllib.request.Request(url, headers={"Authorization": "Bearer " + token,
                "Accept": "application/json, text/csv;q=0.9", "Cache-Control": "no-cache"})
            with urllib.request.build_opener(SameOriginRedirect()).open(request, timeout=timeout) as response:
                content = response.read(MAX_SOURCE_BYTES + 1)
    else:
        raise ValueError("Buyer results source type must be file or url")
    if len(content) > MAX_SOURCE_BYTES:
        raise ValueError("Buyer results are too large")
    return content


def load_csv(source, root, timeout):
    """Compatibility alias for existing CSV configurations."""
    return load_buyer_input({**source, "type": "csv_firestore"}, root, timeout)


def parse_buyer_input(source, content, seller_aliases):
    if source.get("type") == "csv_firestore":
        return parse_csv(content, seller_aliases)
    if source.get("type") == "json_firestore":
        return parse_buyer_json(content, seller_aliases)
    raise ValueError("Unknown buyer results format")


def firebase_client(project_id, root):
    """Use a local service account or Google application-default credentials."""
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore
    except ImportError as error:
        raise RuntimeError("Install Firebase support: python3 -m pip install -r requirements.txt") from error
    app_name = "vanguard-" + project_id
    try:
        app = firebase_admin.get_app(app_name)
    except ValueError:
        configured = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        local_key = Path(root) / "serviceAccountKey.json"
        key_path = Path(configured).expanduser() if configured else local_key if local_key.is_file() else None
        credential = credentials.Certificate(str(key_path)) if key_path else credentials.ApplicationDefault()
        cred_project = getattr(credential, "project_id", None)
        if cred_project and cred_project != project_id:
            raise ValueError("Firebase credentials belong to a different project")
        app = firebase_admin.initialize_app(credential, {"projectId": project_id}, name=app_name)
    return firestore.client(app=app), firestore


def public_firebase_state(project_id, timeout=15):
    """Read the project's publicly readable inventory without a service key."""
    if not isinstance(project_id, str) or not re.fullmatch(r"[a-z][a-z0-9-]{4,29}", project_id):
        raise ValueError("Invalid Firebase project ID")
    name = f"projects/{project_id}/databases/(default)/documents/vanguard_live/state"
    try:
        payload = json.loads(fetch_url("https://firestore.googleapis.com/v1/" + name, timeout).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Firebase returned invalid inventory data") from error
    if not isinstance(payload, dict) or payload.get("name") != name:
        raise ValueError("Firebase returned an unexpected document")

    def decode(field):
        if not isinstance(field, dict):
            raise ValueError("Invalid Firebase field")
        if "mapValue" in field:
            values = field["mapValue"].get("fields", {})
            if not isinstance(values, dict):
                raise ValueError("Invalid Firebase map")
            return {key: decode(value) for key, value in values.items()}
        if "integerValue" in field:
            try:
                return int(field["integerValue"])
            except (TypeError, ValueError) as error:
                raise ValueError("Invalid Firebase integer") from error
        if "doubleValue" in field:
            return number(field["doubleValue"], "Firebase value")
        if "timestampValue" in field:
            return parse_time(field["timestampValue"])
        if "stringValue" in field:
            return field["stringValue"]
        if "booleanValue" in field:
            return field["booleanValue"]
        if "nullValue" in field:
            return None
        raise ValueError("Unsupported Firebase value type")

    fields = payload.get("fields")
    if not isinstance(fields, dict):
        raise ValueError("Firebase inventory document has no fields")
    return {key: decode(value) for key, value in fields.items()}


def inventory_from_state(state):
    if not isinstance(state, dict):
        raise ValueError("Firebase inventory document does not exist")
    virtual, physical = {}, {}
    for sku in SKU_IDS:
        row = state.get(f"BASE_{sku}")
        if not isinstance(row, dict):
            raise ValueError(f"Firebase state is missing BASE_{sku}")
        virtual[sku] = count(row.get("virtual"), f"Firebase {sku} virtual stock")
        physical[sku] = count(row.get("physical"), f"Firebase {sku} physical stock")
        number(row.get("price"), f"Firebase {sku} price", 0.01)
        prime = state.get(f"PRIME_{sku}")
        if prime is not None and (not isinstance(prime, dict)
                                  or prime.get("virtual") != virtual[sku]
                                  or prime.get("physical") != physical[sku]):
            raise ValueError(f"Firebase standard and Prime {sku} stock do not agree")
    pouch = state.get("OFFER_CHARITY_MYSTERY_POUCH")
    if not isinstance(pouch, dict) or pouch.get("virtual") != 0:
        raise ValueError("Firebase mystery pouch must be physical-only")
    pouch_physical = count(pouch.get("physical"), "Firebase physical pouch stock")
    return virtual, physical, pouch_physical


def _event_digest(event):
    return hashlib.sha256(json.dumps(event, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def apply_csv_transaction(client, firestore_module, events, initialize=None):
    """Atomically apply new events, or establish an explicitly chosen baseline.

    initialize='empty' requires no matching events; 'existing' records events as
    already included in Firebase stock. Neither option changes stock.
    """
    if initialize not in (None, "empty", "existing"):
        raise ValueError("Invalid buyer-results baseline mode")
    state_ref = client.collection("vanguard_live").document("state")
    status_ref = client.collection("vanguard_automation").document("csv_baseline")
    event_refs = [client.collection("vanguard_buyer_events").document(
        hashlib.sha256(("brigade:" + event["event_id"]).encode("utf-8")).hexdigest()) for event in events]
    transaction = client.transaction()

    @firestore_module.transactional
    def perform(tx):
        state_snap = state_ref.get(transaction=tx)
        status_snap = status_ref.get(transaction=tx)
        # Firestore requires all reads before writes in a transaction.
        event_snaps = [ref.get(transaction=tx) for ref in event_refs]
        state = state_snap.to_dict() if state_snap.exists else None
        virtual, _, _ = inventory_from_state(state)
        if initialize and status_snap.exists:
            raise ValueError("Firebase buyer-results baseline is already initialized")
        if not initialize and not status_snap.exists:
            raise ValueError("Initialize the Firebase buyer-results baseline before processing sales")
        if initialize == "empty" and events:
            raise ValueError("Cannot use an empty baseline when buyer results already contain seller sales")
        new_events = []
        for event, ref, snap in zip(events, event_refs, event_snaps):
            digest = _event_digest(event)
            if snap.exists:
                if (snap.to_dict() or {}).get("digest") != digest:
                    raise ValueError("A previously processed Event_ID has changed; reconcile it manually")
                continue
            new_events.append((event, ref, digest))
        new_virtual = dict(virtual)
        if not initialize:
            for event, _, _ in new_events:
                sku = event["sku_id"]
                new_virtual[sku] -= event["quantity"]
                if new_virtual[sku] < 0:
                    raise ValueError(f"Buyer sales would make {sku} inventory negative")
        now = utc_now()
        if initialize:
            tx.create(status_ref, {"initialized_at": now, "baseline_mode": initialize})
        for event, ref, digest in new_events:
            tx.create(ref, {"digest": digest, "sku_id": event["sku_id"],
                            "offer_id": event["offer_id"], "quantity": event["quantity"],
                            "event_time": event["timestamp"], "recorded_at": now,
                            "included_in_baseline": bool(initialize)})
        if not initialize and new_events:
            updates = {"last_updated": datetime.now(timezone.utc)}
            for sku in SKU_IDS:
                if new_virtual[sku] != virtual[sku]:
                    updates[f"BASE_{sku}.virtual"] = new_virtual[sku]
                    if f"PRIME_{sku}" in state:
                        updates[f"PRIME_{sku}.virtual"] = new_virtual[sku]
            tx.update(state_ref, updates)
            state = copy.deepcopy(state)
            for sku in SKU_IDS:
                state[f"BASE_{sku}"]["virtual"] = new_virtual[sku]
                if f"PRIME_{sku}" in state:
                    state[f"PRIME_{sku}"]["virtual"] = new_virtual[sku]
        return state, len(new_events) if not initialize else 0

    return perform(transaction)
