"""
Zubyria Properties — Reservation / date-block store.
Backed by DynamoDB table zubyria-reservations (us-east-1).
Table keys: pk (S), sk (S).  All blocks have pk="BLOCK", sk=<timestamp_uuid>.
Occupied nights: [checkin, checkout) — checkout day is free for the next check-in.
"""
import datetime
import uuid

TABLE_NAME = "zubyria-reservations"
REGION = "us-east-1"


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
        return items

    def availability(self, checkin, checkout):
        """Return {house_id: {"available": False, "blocked_by": label}} for every house
        that has an overlapping active block.  Houses with no overlap are omitted
        (caller treats absence as available=True)."""
        result = {}
        for b in self.list_blocks():
            if b.get("status") != "active":
                continue
            bc, bo = _d(b["checkin"]), _d(b["checkout"])
            if not _overlaps(checkin, checkout, bc, bo):
                continue
            for h in b.get("houses", []):
                if h not in result:
                    result[h] = {"available": False, "blocked_by": b.get("label", "")}
        return result

    def add_block(self, houses, checkin, checkout, label, created_by="admin"):
        """Check availability for the given houses; if any conflict, return
        {"ok": False, "conflicts": [label, ...]} without writing.
        On success write the item and return {"ok": True, "id": sk}."""
        avail = self.availability(checkin, checkout)
        conflicts = [
            avail[h]["blocked_by"]
            for h in houses
            if h in avail and not avail[h]["available"]
        ]
        if conflicts:
            return {"ok": False, "conflicts": conflicts}
        sk = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%S%f") + "_" + uuid.uuid4().hex[:8]
        self._t.put_item(Item={
            "pk": "BLOCK", "sk": sk,
            "houses": houses,
            "checkin": checkin.isoformat() if hasattr(checkin, "isoformat") else checkin,
            "checkout": checkout.isoformat() if hasattr(checkout, "isoformat") else checkout,
            "label": label,
            "status": "active",
            "created_at": datetime.datetime.utcnow().isoformat(),
            "created_by": created_by,
        })
        return {"ok": True, "id": sk}

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
