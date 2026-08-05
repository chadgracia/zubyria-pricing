"""
Zubyria Properties — Reservation / date-block store.
Backed by DynamoDB table zubyria-reservations (us-east-1).
Table keys: pk (S), sk (S).  All blocks have pk="BLOCK", sk=<timestamp_uuid>.
Occupied nights: [checkin, checkout) — checkout day is free for the next check-in.
"""
import datetime
import uuid
from decimal import Decimal

TABLE_NAME = "zubyria-reservations"
REGION = "us-east-1"


def _to_dynamo(obj):
    """Recursively convert Python floats to Decimal for DynamoDB storage."""
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, bool):
        return obj  # bool before int: bool is subclass of int
    if isinstance(obj, int):
        return obj
    if isinstance(obj, dict):
        return {k: _to_dynamo(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_dynamo(v) for v in obj]
    return obj


def _from_dynamo(obj):
    """Recursively convert DynamoDB Decimal back to float (or int if whole number)."""
    if isinstance(obj, Decimal):
        f = float(obj)
        return int(f) if f == int(f) else f
    if isinstance(obj, dict):
        return {k: _from_dynamo(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_from_dynamo(v) for v in obj]
    return obj


def _overlaps(ci1, co1, ci2, co2):
    """True if half-open intervals [ci1,co1) and [ci2,co2) share at least one night."""
    return ci1 < co2 and ci2 < co1


def _d(s):
    from datetime import date
    return date.fromisoformat(s) if isinstance(s, str) else s


class Store:
    def __init__(self, table):
        self._t = table

    def list_blocks(self):
        items, kw = [], {
            "KeyConditionExpression": "pk = :pk",
            "ExpressionAttributeValues": {":pk": "BLOCK"},
        }
        while True:
            r = self._t.query(**kw)
            items.extend(r.get("Items", []))
            if "LastEvaluatedKey" not in r:
                break
            kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]
        return [_from_dynamo(item) for item in items]

    def availability(self, checkin, checkout, exclude_id=None):
        """Return {house_id: {"available": False, "blocked_by": label}} for every house
        that has an overlapping active block.  Houses with no overlap are omitted
        (caller treats absence as available=True)."""
        result = {}
        for b in self.list_blocks():
            if b.get("status") != "active":
                continue
            if exclude_id and b.get("sk") == exclude_id:
                continue
            bc, bo = _d(b["checkin"]), _d(b["checkout"])
            if not _overlaps(checkin, checkout, bc, bo):
                continue
            for h in b.get("houses", []):
                if h not in result:
                    result[h] = {"available": False, "blocked_by": b.get("label", "")}
        return result

    def add_block(self, houses, checkin, checkout, label, created_by="admin",
                  exclude_id=None, snapshot=None, quote_params=None, btype=None,
                  notes=None, deposit=None, override_subtotal=None, price_note=None,
                  payments=None, expenses=None, edited_from=None):
        """Check availability for the given houses (excluding exclude_id block); if any conflict,
        return {"ok": False, "conflicts": [label, ...]} without writing.
        On success write the item and return {"ok": True, "id": sk}."""
        avail = self.availability(checkin, checkout, exclude_id=exclude_id)
        conflicts = [
            avail[h]["blocked_by"]
            for h in houses
            if h in avail and not avail[h]["available"]
        ]
        if conflicts:
            return {"ok": False, "conflicts": conflicts}
        sk = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%S%f") + "_" + uuid.uuid4().hex[:8]
        item = {
            "pk": "BLOCK", "sk": sk,
            "houses": houses,
            "checkin": checkin.isoformat() if hasattr(checkin, "isoformat") else checkin,
            "checkout": checkout.isoformat() if hasattr(checkout, "isoformat") else checkout,
            "label": label,
            "status": "active",
            "created_at": datetime.datetime.utcnow().isoformat(),
            "created_by": created_by,
        }
        if snapshot:
            item["snapshot"] = snapshot
        if quote_params:
            item["quote_params"] = quote_params
        if btype:
            item["btype"] = btype
        if notes:
            item["notes"] = notes
        if deposit:
            # Legacy scalar, kept only so old items still read; the ledger is truth.
            item["deposit"] = deposit
        if payments is not None:
            item["payments"] = payments
        if expenses is not None:
            item["expenses"] = expenses
        if edited_from:
            # Lineage kept for auditing; never rendered as a raw id.
            item["edited_from"] = edited_from
        if override_subtotal:
            item["override_subtotal"] = override_subtotal
        if price_note:
            item["price_note"] = price_note
        self._t.put_item(Item=_to_dynamo(item))
        return {"ok": True, "id": sk}

    def set_price_override(self, block_id, snapshot, override_subtotal, price_note):
        """Write a manual price override, or clear it when override_subtotal is None.

        Only the price fields move; dates, houses and notes are untouched, so the
        block keeps its identity (and its detail URL) across a re-price.
        """
        names = {"#sn": "snapshot", "#ov": "override_subtotal", "#pn": "price_note"}
        if override_subtotal is None:
            self._t.update_item(
                Key={"pk": "BLOCK", "sk": block_id},
                UpdateExpression="SET #sn = :sn REMOVE #ov, #pn",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues={":sn": _to_dynamo(snapshot)},
            )
        else:
            self._t.update_item(
                Key={"pk": "BLOCK", "sk": block_id},
                UpdateExpression="SET #sn = :sn, #ov = :ov, #pn = :pn",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=_to_dynamo({
                    ":sn": snapshot,
                    ":ov": override_subtotal,
                    ":pn": price_note or "",
                }),
            )

    def set_payments(self, block_id, payments):
        """Replace a block's payment ledger wholesale.

        Callers derive the new list from the current one (append / remove) so a
        migrated legacy deposit is materialised on the first write.
        """
        self._t.update_item(
            Key={"pk": "BLOCK", "sk": block_id},
            UpdateExpression="SET #p = :p",
            ExpressionAttributeNames={"#p": "payments"},
            ExpressionAttributeValues={":p": _to_dynamo(payments)},
        )

    def set_expenses(self, block_id, expenses):
        """Replace a reservation's expense ledger wholesale (mirrors set_payments)."""
        self._t.update_item(
            Key={"pk": "BLOCK", "sk": block_id},
            UpdateExpression="SET #e = :e",
            ExpressionAttributeNames={"#e": "expenses"},
            ExpressionAttributeValues={":e": _to_dynamo(expenses)},
        )

    def supersede_block(self, block_id, new_sk):
        """Retire a record because an edit replaced it.

        Distinct from cancel_block: a superseded record's money lives on the new
        record, so the finances report must not count it twice. A real guest
        cancellation still uses cancel_block and keeps counting.
        """
        self._t.update_item(
            Key={"pk": "BLOCK", "sk": block_id},
            UpdateExpression="SET #s = :s, #b = :b",
            ExpressionAttributeNames={"#s": "status", "#b": "superseded_by"},
            ExpressionAttributeValues={":s": "superseded", ":b": new_sk},
        )

    def cancel_block(self, block_id):
        self._t.update_item(
            Key={"pk": "BLOCK", "sk": block_id},
            UpdateExpression="SET #s = :s",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "cancelled"},
        )


def get_store():
    """Return a Store backed by the real DynamoDB table. boto3 is bundled in Lambda."""
    import boto3
    table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE_NAME)
    return Store(table)
