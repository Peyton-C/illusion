"""claws: the inventory service.

Owns the database and every rule about stock. It knows nothing about Discord or
about printers: when an item crosses its low threshold, claws records the fact
and announces it, and whoever cares about forum threads reacts to that. That is
the coupling this service exists to break -- the low-stock thread used to be
created inline by whichever process happened to change the stock, which stops
working the moment the database lives on a different machine from the bot.
"""

import asyncio
import json
import time
from importlib.metadata import version

import httpx

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from illusion_core import config as illusion_config
from illusion_core.events import EventBus
from illusion_core.fleet import (
    PROBE_TIMEOUT_S,
    cached_entry,
    health_payload,
)
from illusion_core.uptime import service_uptime_ms, system_uptime_ms
from claws import locations
from claws.digikey_client import DigiKeyClient
from claws.inventory_reader import SpreadsheetManager

# From package metadata, never hardcoded: the fleet status flags a version
# mismatch across services, and a constant left behind by a release bump would
# report a mismatch that does not exist.
VERSION = version("claws")


class NewItem(BaseModel):
    item: dict


class Updates(BaseModel):
    updates: dict


class Amount(BaseModel):
    amount: float | None = None


class Quantity(BaseModel):
    quantity: float


class TagRequest(BaseModel):
    tag: str


class LocationRequest(BaseModel):
    location: str | None = None


class VendorRequest(BaseModel):
    vendor_name: str
    link: str


class RenameRequest(BaseModel):
    find: str
    replace: str = ""
    case_sensitive: bool = False


class LowThread(BaseModel):
    thread_id: int | None = None


class ScanRequest(BaseModel):
    barcode: str
    # Look the bag up even if it has been counted before
    force: bool = False


class ScanRecord(BaseModel):
    barcode: str
    sku: str
    digikey_part_number: str | None = None
    quantity: float | None = None


class Registration(BaseModel):
    service: str
    version: str
    uptime_ms: int | None = None
    system_uptime_ms: int | None = None
    host: str | None = None


# Fields that count discrete things, so they cannot be fractional on an item
# tracked per item. LOW_THRESHOLD and ORDER_QUANTITY are deliberately not here:
# a threshold of 2.5 is only an odd way of writing 3 and does no harm, and
# ORDER_QUANTITY describes a purchase rather than stock on hand.
WHOLE_NUMBER_FIELDS = ("QUANTITY_ON_HAND", "DECREASE_AMOUNT")

FRACTION_HINT = "HYBRID tracking is the one that takes fractions."


def _is_fractional(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        # Not a number at all, which the database layer rejects with a better
        # message than anything this could produce
        return False

    return number != int(number)


def amount_problem(item, amount):
    """Whether this amount makes sense for how the item is tracked.

    KANBAN items have no quantity at all -- a decrease just marks them low and
    the amount is discarded -- so there is nothing to refuse. QUANTITY items are
    counted in whole units, so a fraction is a mistake worth catching rather
    than silently rounding away. HYBRID items are measured rather than counted,
    so fractions are the entire point of the mode.

    Returns the reason as a string, or None if the amount is fine.
    """
    if amount is None or item["TRACKING_MODE"] != "QUANTITY":
        return None

    if _is_fractional(amount):
        return (
            f"{item['SKU']} is tracked per item, so the amount has to be a whole "
            f"number. {FRACTION_HINT}"
        )

    return None


def no_count_problem(item, action):
    """KANBAN items have no quantity at all, so counting operations are meaningless.

    They are only low or not low: a decrease marks them low and that is the
    whole model. Anything that sets or adds to a count would give them a
    phantom quantity and, worse, let a LOW_THRESHOLD they should not have start
    deciding their low state.
    """
    if item["TRACKING_MODE"] != "KANBAN":
        return None

    return (
        f"{item['SKU']} is KANBAN tracked, so it has no stock count to {action}. "
        f"Mark it low with `decrease`, or clear that with `resolve`."
    )


def fields_problem(tracking_mode, values, sku=None):
    """The same rules, for values being written rather than an amount applied.

    Without this the checks are trivially bypassed: a fractional
    DECREASE_AMOUNT stored on the item is what a bare `decrease` uses, and
    writing QUANTITY_ON_HAND directly sidesteps set_stock entirely.
    """
    subject = sku or "This item"
    present = [field for field in WHOLE_NUMBER_FIELDS if values.get(field) is not None]

    if tracking_mode == "KANBAN":
        if not present:
            return None

        return (
            f"{subject} is KANBAN tracked, so it has no stock count. "
            f"Leave {' and '.join(present)} unset, or track it as QUANTITY or HYBRID."
        )

    if tracking_mode != "QUANTITY":
        return None

    bad = [
        f"{field} ({values[field]})"
        for field in present
        if _is_fractional(values[field])
    ]

    if not bad:
        return None

    requirement = "must be a whole number" if len(bad) == 1 else "must be whole numbers"

    return f"{subject} is tracked per item, so {' and '.join(bad)} {requirement}. {FRACTION_HINT}"


def rename_problem(changes):
    """Whether this batch of renames is safe to write, as a reason or None.

    The one thing a substring replace can do that nothing else here can undo
    cleanly is blank out a name entirely (replacing "Capasiter" with "" on an
    item named exactly that). Caught here rather than left to the NAME NOT
    NULL constraint, which would fail the whole batch with a database error
    instead of saying which items caused it.
    """
    blank = [change for change in changes if not change["NEW_NAME"].strip()]

    if not blank:
        return None

    skus = ", ".join(change["SKU"] for change in blank[:5])
    more = "" if len(blank) <= 5 else f", and {len(blank) - 5} more"

    return f"That would blank the name of {skus}{more}. Refusing."


def create_app(config_path="./claws.yaml"):
    config = illusion_config.load(
        config_path,
        required=["claws.token", "claws.database_location"],
    )

    token = illusion_config.get(config, "claws.token")
    inventory = SpreadsheetManager(illusion_config.get(config, "claws.database_location"))
    events = EventBus()
    started_at = time.time()

    # Which services make up the fleet, from static config. The set is fixed, so
    # this is not service discovery -- and a static list is what lets `about`
    # survive a claws restart. An in memory registry alone would forget every
    # service that had already announced itself, and a kiosk idle since before
    # the restart would never re-announce.
    fleet = illusion_config.get(config, "claws.fleet", {}) or {}

    # service -> {payload, at}. Last successful contact, used to report an
    # assumed uptime when a service cannot be reached right now.
    last_seen = {}

    digikey = None

    if illusion_config.get(config, "claws.digikey.enabled"):
        digikey = DigiKeyClient(config_path, root="claws")

    async def require_token(authorization: str = Header(default="")):
        if authorization != f"Bearer {token}":
            raise HTTPException(status_code=401, detail="bad or missing bearer token")

    app = FastAPI(title="claws", version=VERSION)
    auth = [Depends(require_token)]

    def _item_or_404(sku):
        item = inventory.get_item(sku)

        if item is None:
            raise HTTPException(status_code=404, detail=f"no item with sku {sku}")

        return item

    def _announce(item, was_low):
        """Publish a low-stock transition, if one just happened.

        Keyed on the transition itself, never on whether a thread id happens to
        be set. Archiving is asynchronous now: the bot reacts to item.resolved,
        archives the thread and only then clears LOW_THREAD_ID. Any stock change
        landing in that gap would re-fire a thread-based check every time, so
        this compares before and after directly and fires exactly once.

        Derived here rather than trusted from the caller, because every path
        that changes stock must produce the same events and only this service
        sees both sides of the change.
        """
        if item["LOW"] and not was_low:
            events.publish({"event": "item.low", "sku": item["SKU"], "item": item})
            return "low"

        if was_low and not item["LOW"]:
            events.publish({"event": "item.resolved", "sku": item["SKU"], "item": item})
            return "resolved"

        return None

    # Unauthenticated so the fleet can report liveness even if a token is
    # rotated on one side only
    @app.get("/health")
    async def health():
        return {
            "service": "claws",
            "version": VERSION,
            "uptime_ms": service_uptime_ms(started_at),
            "system_uptime_ms": system_uptime_ms(),
            "items": inventory.count_items(),
        }

    async def _probe(name, url):
        try:
            async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_S) as client:
                response = await client.get(f"{url.rstrip('/')}/health")
                response.raise_for_status()
                payload = response.json()
        except Exception:
            return _from_cache(name)

        last_seen[name] = {"payload": payload, "at": time.time()}

        return {"service": name, "state": "ok", **payload}

    def _from_cache(name):
        cached = last_seen.get(name)
        age_ms = 0 if cached is None else round((time.time() - cached["at"]) * 1000)

        return cached_entry(name, cached, age_ms)

    @app.post("/register", dependencies=auth)
    async def register(registration: Registration):
        """Announce on startup. Warms the cache so a restart shows up promptly.

        Not the source of truth for who exists -- that is the static fleet
        config -- just for what they last told us about themselves.
        """
        last_seen[registration.service] = {
            "payload": registration.model_dump(exclude_none=True),
            "at": time.time(),
        }

        return {"registered": registration.service}

    @app.get("/status", dependencies=auth)
    async def status():
        """The whole fleet in one call.

        Probes run concurrently, so the budget is the total rather than per
        service and one dead host cannot stretch `about` past a few seconds.
        """
        results = await asyncio.gather(
            *(_probe(name, url) for name, url in fleet.items()),
            return_exceptions=True,
        )

        services = [
            {"service": "claws", "state": "ok", **health_payload("claws", VERSION, started_at)}
        ]

        for name, result in zip(fleet, results):
            if isinstance(result, BaseException):
                services.append(_from_cache(name))
            else:
                services.append(result)

        return {"services": services}

    @app.get("/items", dependencies=auth)
    async def list_items():
        return inventory.read_all()

    @app.get("/low-threads", dependencies=auth)
    async def low_threads():
        """Every item whose low-stock thread state is worth looking at.

        The source of truth for which threads should exist. Returns the items
        that are low, or that still carry a thread id, each tagged with what
        needs doing:

          create   low, but no thread was ever opened
          archive  no longer low, but its thread is still open
          ok       low with a thread, which is the steady state

        The predicate lives here rather than in the bot because it is a
        statement about inventory, and because the bot is not the only thing
        that might ever want to ask.
        """
        rows = []

        for item in inventory.read_all():
            low = bool(item["LOW"])
            thread_id = item["LOW_THREAD_ID"]

            if not low and not thread_id:
                continue

            if low and not thread_id:
                action = "create"
            elif not low and thread_id:
                action = "archive"
            else:
                action = "ok"

            rows.append({"action": action, "item": item})

        return rows

    @app.get("/items/by-dkpn/{dkpn}", dependencies=auth)
    async def item_by_dkpn(dkpn: str):
        return inventory.get_item_by_dkpn(dkpn)

    @app.get("/items/{sku}", dependencies=auth)
    async def get_item(sku: str):
        return _item_or_404(sku)

    @app.post("/items", dependencies=auth)
    async def add_item(request: NewItem):
        problem = fields_problem(
            request.item.get("TRACKING_MODE") or "KANBAN", request.item
        )

        if problem:
            return {"rejected": problem}

        sku = inventory.add_item(request.item)
        inventory.save()

        return {"sku": sku}

    @app.patch("/items/{sku}", dependencies=auth)
    async def update_item(sku: str, request: Updates):
        old = _item_or_404(sku)

        # The mode may be changing in this same request, so check against what
        # the item is about to become rather than what it is now
        problem = fields_problem(
            request.updates.get("TRACKING_MODE") or old["TRACKING_MODE"],
            request.updates,
            sku,
        )

        if problem:
            return {"rejected": problem}

        was_low = bool(old["LOW"])

        inventory.update_item(sku, request.updates)
        inventory.save()

        item = inventory.get_item(sku)

        return {"item": item, "transition": _announce(item, was_low)}

    @app.delete("/items/{sku}", dependencies=auth)
    async def delete_item(sku: str):
        item = _item_or_404(sku)
        inventory.delete_item(sku)
        inventory.save()

        return item

    @app.post("/items/{sku}/decrease", dependencies=auth)
    async def decrease(sku: str, request: Amount):
        problem = amount_problem(_item_or_404(sku), request.amount)

        if problem:
            return {"rejected": problem}

        result = inventory.decrease_item(sku, request.amount)
        inventory.save()

        # decrease_item already reports the crossing, so this does not re-derive it
        if result["low_changed"]:
            events.publish({"event": "item.low", "sku": sku, "item": result["item"]})

        return {**result, "transition": "low" if result["low_changed"] else None}

    @app.post("/items/{sku}/increase", dependencies=auth)
    async def increase(sku: str, request: Amount):
        old = _item_or_404(sku)
        problem = no_count_problem(old, "increase") or amount_problem(old, request.amount)

        if problem:
            return {"rejected": problem}

        was_low = bool(old["LOW"])

        item = inventory.increase_item(sku, float(request.amount or 1))
        inventory.save()

        return {"item": item, "transition": _announce(item, was_low)}

    @app.put("/items/{sku}/stock", dependencies=auth)
    async def set_stock(sku: str, request: Quantity):
        old = _item_or_404(sku)
        problem = no_count_problem(old, "set") or fields_problem(
            old["TRACKING_MODE"], {"QUANTITY_ON_HAND": request.quantity}, sku
        )

        if problem:
            return {"rejected": problem}

        was_low = bool(old["LOW"])

        item = inventory.set_stock(sku, request.quantity)
        inventory.save()

        return {"item": item, "transition": _announce(item, was_low)}

    @app.post("/items/{sku}/resolve", dependencies=auth)
    async def resolve(sku: str):
        item = _item_or_404(sku)

        if not item["LOW"]:
            return {"changed": False, "item": item}

        inventory.update_item(sku, {"LOW": "FALSE"})
        inventory.save()

        item = inventory.get_item(sku)
        events.publish({"event": "item.resolved", "sku": sku, "item": item})

        return {"changed": True, "item": item}

    # The bot owns thread ids; it writes them back through this one endpoint
    # rather than through the general update route, so the ownership is obvious
    @app.put("/items/{sku}/low-thread", dependencies=auth)
    async def set_low_thread(sku: str, request: LowThread):
        _item_or_404(sku)
        inventory.update_item(sku, {"LOW_THREAD_ID": request.thread_id})
        inventory.save()

        return inventory.get_item(sku)

    @app.get("/items/{sku}/tags", dependencies=auth)
    async def item_tags(sku: str):
        tags = inventory.get_item_tags(sku)

        if tags is None:
            raise HTTPException(status_code=404, detail=f"no item with sku {sku}")

        return tags

    @app.post("/items/{sku}/tags", dependencies=auth)
    async def add_tag(sku: str, request: TagRequest):
        _item_or_404(sku)

        return {"added": inventory.add_tag(sku, request.tag)}

    @app.get("/items/{sku}/location", dependencies=auth)
    async def item_location(sku: str):
        return {"location": _item_or_404(sku)["LOCATION"]}

    # A location is one value rather than a list, so it is set rather than added,
    # and an empty one clears it
    @app.put("/items/{sku}/location", dependencies=auth)
    async def set_location(sku: str, request: LocationRequest):
        _item_or_404(sku)

        inventory.set_location(sku, request.location)
        inventory.save()

        item = inventory.get_item(sku)

        # Whether it landed on a shelf that exists. A location off the
        # catalogue is allowed, but it is worth telling whoever typed it, since
        # at that point it is as likely to be a typo as a new shelf.
        return {"item": item, "known": locations.is_known(item["LOCATION"])}

    @app.post("/items/{sku}/vendors", dependencies=auth)
    async def add_vendor(sku: str, request: VendorRequest):
        _item_or_404(sku)

        return {"added": inventory.add_vendor(sku, request.vendor_name, request.link)}

    @app.get("/search", dependencies=auth)
    async def search(name: str, limit: int = 10):
        return inventory.search_items(name, limit=limit)

    @app.post("/items/rename/preview", dependencies=auth)
    async def rename_preview(request: RenameRequest):
        if not request.find.strip():
            return {"rejected": "Give some text to find in item names."}

        changes = inventory.preview_rename(request.find, request.replace, request.case_sensitive)
        problem = rename_problem(changes)

        if problem:
            return {"rejected": problem}

        return {"changes": changes}

    @app.post("/items/rename/apply", dependencies=auth)
    async def rename_apply(request: RenameRequest):
        """Re-matches rather than trusting a list of skus from a prior preview,
        so a rename confirmed minutes later reflects the catalogue as it is
        now, not as it was when the preview was shown."""
        if not request.find.strip():
            return {"rejected": "Give some text to find in item names."}

        changes = inventory.preview_rename(request.find, request.replace, request.case_sensitive)
        problem = rename_problem(changes)

        if problem:
            return {"rejected": problem}

        if changes:
            inventory.apply_rename(changes)
            inventory.save()

        return {"changes": changes}

    @app.get("/suggest", dependencies=auth)
    async def suggest(query: str = "", limit: int = 25):
        return inventory.suggest_items(query, limit=limit)

    @app.get("/tags", dependencies=auth)
    async def tags():
        return inventory.get_tags()

    @app.get("/tags/{tag}/items", dependencies=auth)
    async def items_by_tag(tag: str):
        return inventory.get_items_by_tag(tag)

    @app.post("/tags/rename/preview", dependencies=auth)
    async def tag_rename_preview(request: RenameRequest):
        if not request.find.strip():
            return {"rejected": "Give a tag to find."}

        if not request.replace.strip():
            return {"rejected": "Give a tag to rename it to."}

        changes = inventory.preview_tag_rename(request.find, request.replace, request.case_sensitive)

        return {"changes": changes}

    @app.post("/tags/rename/apply", dependencies=auth)
    async def tag_rename_apply(request: RenameRequest):
        """Re-matches rather than trusting a list of skus from a prior preview,
        for the same reason /items/rename/apply does: a tag added or removed
        in the gap between the preview and the confirm is reflected rather
        than clobbered."""
        if not request.find.strip():
            return {"rejected": "Give a tag to find."}

        if not request.replace.strip():
            return {"rejected": "Give a tag to rename it to."}

        changes = inventory.preview_tag_rename(request.find, request.replace, request.case_sensitive)

        if changes:
            inventory.apply_tag_rename(changes)
            inventory.save()

        return {"changes": changes}

    @app.get("/locations", dependencies=auth)
    async def all_locations():
        return inventory.get_locations()

    # Before the {location:path} route below, which would otherwise swallow it
    @app.get("/locations/suggest", dependencies=auth)
    async def suggest_locations(query: str = "", limit: int = 25):
        return inventory.suggest_locations(query, limit=limit)

    # :path because a location is free text off a shelf label, and "Acrylic /
    # Polycarbonate Scrap" is one location rather than two path segments
    @app.get("/locations/{location:path}/items", dependencies=auth)
    async def items_by_location(location: str):
        return inventory.get_items_by_location(location)

    @app.post("/digikey/scan", dependencies=auth)
    async def digikey_scan(request: ScanRequest):
        if digikey is None:
            raise HTTPException(status_code=503, detail="DigiKey support is not enabled")

        # Checked before DigiKey is asked, so a repeat scan costs no API call
        # and is still caught while DigiKey is down
        if not request.force:
            previous = inventory.get_digikey_scan(request.barcode)

            if previous is not None:
                return {"duplicate": previous}

        try:
            data = await asyncio.to_thread(digikey.lookup_barcode, request.barcode)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"DigiKey lookup failed: {e}")

        return data

    # Separate from /digikey/scan because the stock change happens in between,
    # through the ordinary item routes. A bag is only recorded once its stock
    # has actually landed, so a scan that failed can simply be scanned again.
    @app.post("/digikey/scans", dependencies=auth)
    async def record_digikey_scan(request: ScanRecord):
        _item_or_404(request.sku)

        inventory.record_digikey_scan(
            request.barcode, request.sku, request.digikey_part_number, request.quantity
        )

        return {"recorded": True}

    @app.get("/digikey/part/{part_number:path}", dependencies=auth)
    async def digikey_part(part_number: str):
        if digikey is None:
            raise HTTPException(status_code=503, detail="DigiKey support is not enabled")

        try:
            return await asyncio.to_thread(digikey.lookup_part_number, part_number)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"DigiKey lookup failed: {e}")

    @app.get("/events", dependencies=auth)
    async def event_stream():
        subscription = events.subscribe()

        async def stream():
            try:
                while not subscription.dropped.is_set():
                    try:
                        event = await asyncio.wait_for(subscription.queue.get(), timeout=20)
                    except TimeoutError:
                        yield ": keepalive\n\n"
                        continue

                    yield f"data: {json.dumps(event, default=str)}\n\n"
            finally:
                events.unsubscribe(subscription)

        return StreamingResponse(stream(), media_type="text/event-stream")

    return app


def main():
    import os

    import uvicorn

    config_path = os.environ.get("CLAWS_CONFIG", "./claws.yaml")

    try:
        config = illusion_config.load(config_path)
    except illusion_config.ConfigError as e:
        print(e)
        raise SystemExit(1)

    uvicorn.run(
        create_app(config_path),
        host=illusion_config.get(config, "claws.bind.host", "127.0.0.1"),
        port=illusion_config.get(config, "claws.bind.port", 8080),
        log_level="info",
    )


if __name__ == "__main__":
    main()
