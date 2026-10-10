"""The shared command layer.

Both frontends run the same commands against the same services, so this is
where they live. It holds no rendering: handlers that produce tabular output
return a Rows carrying the data and which fields to leave out, and the caller
turns that into a terminal table or a Discord embed. That is the whole reason
this module can sit in illusion-core -- the moment it formatted an embed it
would drag discord back into a package that claws and lipgloss depend on.
"""

import functools
from dataclasses import dataclass, field
from datetime import datetime, timezone

from illusion_core import helpers as illusion_helpers
from illusion_core.clients import ServiceUnavailable
from illusion_core.uptime import format_duration, service_uptime_ms, system_uptime_ms


@dataclass
class Rows:
    """Data for the caller to render, with the fields it should leave out."""

    data: object
    exclude: list = field(default_factory=list)


@dataclass
class DuplicateScan:
    """A DigiKey bag that has already been counted, so stock was left alone.

    Carries the item's info for the caller to show in place of a stock change,
    and the barcode so it can offer to count the bag anyway -- two bags off one
    order line can carry identical labels.
    """

    barcode: str
    message: str
    info: object


def reports_service_errors(handler):
    """Turn an unreachable service into a message instead of a traceback.

    Fail fast by design: nothing is buffered locally for a retry later, the
    caller is simply told plainly which service could not be reached. A scan
    made during an outage is not recorded, and says so.
    """
    @functools.wraps(handler)
    async def wrapper(*args, **kwargs):
        try:
            return await handler(*args, **kwargs)
        except ServiceUnavailable as e:
            return f"Unable to reach {e.service or 'a service'}.\n{e}"

    return wrapper



class DB_Commands:
    def __init__(self, claws, lipgloss, started_at):
        self.claws = claws
        self.lipgloss = lipgloss
        self.started_at = started_at

    @reports_service_errors
    async def handler_add_item(self, item_name, order_quantity, tracking_mode="KANBAN", quantity_on_hand=None,
                               low_threshold=None, decrease_amount=None, vendor_1 = None, link_1 = None, 
                               vendor_2 = None, link_2 = None, vendor_3 = None, link_3 = None, 
                               vendor_4 = None, link_4 = None, vendor_5 = None, link_5 = None,
                               digikey_part_number = None, tags = None, notes = None, location = None,):
        # Digikey part numbers are unique, so we need to make sure that there isnt an existing item with the sane dkpn
        if digikey_part_number != None:
            digikey_test = await self.claws.item_by_dkpn(digikey_part_number)
            if digikey_test != None:
                return f"DKPN {digikey_part_number} is already in use by {digikey_test['SKU']}"

        new_item = {
            "NAME": item_name,
            "ORDER_QUANTITY": order_quantity,
            "TRACKING_MODE": tracking_mode,
            "QUANTITY_ON_HAND": quantity_on_hand,
            "LOW_THRESHOLD": low_threshold,
            "LOW_THREAD_ID": None,
            "DECREASE_AMOUNT": decrease_amount,
            "LINK_1": link_1,
            "VENDOR_1": vendor_1,
            "LINK_2": link_2,
            "VENDOR_2": vendor_2,
            "LINK_3": link_3,
            "VENDOR_3": vendor_3,
            "LINK_4": link_4,
            "VENDOR_4": vendor_4,
            "LINK_5": link_5,
            "VENDOR_5": vendor_5,
            "LOW": "FALSE",
            "DIGIKEY_PART_NUMBER": digikey_part_number,
            "LOCATION": location,
            "TAGS": tags,
            "NOTES": notes,
        }

        created = await self.claws.add_item(new_item)

        if created.get("rejected"):
            return created["rejected"]

        new_sku = created["sku"]

        if digikey_part_number != None:
            digikey_link = f"https://www.digikey.ca/en/products/result?keywords={digikey_part_number}"
            await self.claws.add_vendor(new_sku, "Digikey", digikey_link)

        response_message = f"Added {item_name} to inventory, SKU: {new_sku}"
        return response_message

    @reports_service_errors
    async def handler_delete_item(self, sku):
        sku = illusion_helpers.clean_sku(sku)
        item = await self.claws.delete_item(sku)

        if item is None:
            return f"Invalid sku: {sku}"

        return f"Removed {item['NAME']} from inventory, SKU: {sku}"

    @reports_service_errors
    async def handler_info(self, sku, hide_ext=True):
        sku = illusion_helpers.clean_sku(sku)
        item = await self.claws.get_item(sku)

        if item is not None:
            if hide_ext:
                exclude = ["TRACKING_MODE", "LOW_THRESHOLD", "LOW_THREAD_ID", "DECREASE_AMOUNT",
                            "VENDOR_1", "LINK_1", "VENDOR_2", "LINK_2", "VENDOR_3", "LINK_3", "VENDOR_4", "LINK_4", "VENDOR_5", "LINK_5"]

                if item["TRACKING_MODE"] == "KANBAN":
                    exclude.append("QUANTITY_ON_HAND")
            else:
                exclude = []

            return Rows(item, exclude)

        return f"Invalid sku: {sku}"

    @reports_service_errors
    async def handler_resolve(self, sku, archive_thread=False):
        sku = illusion_helpers.clean_sku(sku)
        result = await self.claws.resolve(sku)

        if result is None:
            return f"Invalid sku: {sku}"

        if not result["changed"]:
            return f"{sku} not marked as low"

        # The thread is archived by whoever is listening for item.resolved, so
        # archive_thread no longer gates anything here
        return f"{sku} no longer marked as low"

    @reports_service_errors
    async def handler_search(self, name: str):
        results = await self.claws.search(name, limit=50)

        if not results:
            return f"No items found matching: {name}"

        exclude = [
            "LINK_1",
            "VENDOR_1",
            "LINK_2",
            "VENDOR_2",
            "LINK_3",
            "VENDOR_3",
            "LINK_4",
            "VENDOR_4",
            "LINK_5",
            "VENDOR_5",
            "LOW_THREAD_ID",
            "TRACKING_MODE",
            "LOW_THRESHOLD",
            "DECREASE_AMOUNT",
            "ORDER_QUANTITY",
            "LOW",
            "NOTES",
            "TAGS",
        ]
        return Rows(results, exclude)

    @reports_service_errors
    async def handler_decrease(self, sku, amount=None):
        sku = illusion_helpers.clean_sku(sku)

        if amount != None and float(amount) <= 0:
            return f"Quantity must be greater than 0"

        result = await self.claws.decrease(sku, float(amount) if amount != None else None)

        if result is None:
            return f"Invalid sku: {sku}"

        # claws decides whether an amount suits how the item is tracked, since
        # that is a rule about stock and it is the service that knows the mode
        if result.get("rejected"):
            return result["rejected"]

        item = result["item"]
        went_low = result["transition"] == "low"

        if item["TRACKING_MODE"] == "KANBAN":
            if went_low:
                return f"{sku} marked as low, a low-stock thread is on its way"

            return f"{sku} already marked as low"

        response_message = (
            f"{sku} decreased by "
            f"{illusion_helpers.format_quantity(result['decrease_amount'])} units: "
            f"{illusion_helpers.format_quantity(result['old_quantity'])} -> "
            f"{illusion_helpers.format_quantity(result['new_quantity'])}"
        )

        # The thread is created by whoever is listening for item.low, which may
        # not be this process, so its name is not available to report here
        if went_low:
            response_message += "\nLow threshold reached, a low-stock thread is on its way"
        elif item["LOW"]:
            response_message += "\nItem is already marked as low."

        return response_message

    @reports_service_errors
    async def handler_increase(self, sku, amount=1):
        sku = illusion_helpers.clean_sku(sku)

        result = await self.claws.increase(sku, float(amount))

        if result is None:
            return f"Invalid sku: {sku}"

        if result.get("rejected"):
            return result["rejected"]

        return self._increase_message(sku, amount, result)

    def _increase_message(self, sku, amount, result):
        item = result["item"]
        response_message = (
            f"{sku} increased by {illusion_helpers.format_quantity(amount)} units. "
            f"New stock: {illusion_helpers.format_quantity(item['QUANTITY_ON_HAND'])} units. "
            f"Low: {item['LOW']}"
        )

        if result["transition"] == "low":
            response_message += "\nLow threshold reached, a low-stock thread is on its way"
        elif result["transition"] == "resolved":
            response_message += "\nNo longer low, the low-stock thread is being archived"

        return response_message

    @reports_service_errors
    async def handler_set_stock(self, sku, quantity):
        sku = illusion_helpers.clean_sku(sku)

        result = await self.claws.set_stock(sku, float(quantity))

        if result is None:
            return f"Invalid sku: {sku}"

        if result.get("rejected"):
            return result["rejected"]

        item = result["item"]
        response_message = (
            f"{sku} stock set to "
            f"{illusion_helpers.format_quantity(item['QUANTITY_ON_HAND'])} units. "
            f"Low: {item['LOW']}"
        )

        if result["transition"] == "low":
            response_message += "\nLow threshold reached, a low-stock thread is on its way"
        elif result["transition"] == "resolved":
            response_message += "\nNo longer low, the low-stock thread is being archived"

        return response_message

    async def handler_generate_barcode(self, sku):
        return await self.lipgloss.render(style="classic_barcode", sku=sku, width=350, height=280, rotate=0)

    async def handler_preview_label(self, style, sku = None, text_line_1 = None, text_line_2 = None, scale = 3):
        """PNG bytes of exactly what handler_print would put on the roll.

        Undecorated like handler_generate_barcode: the caller is holding an
        image, not a message, so it has to deal with the outage itself rather
        than be handed a string where bytes were expected.
        """
        if sku != None:
            sku = illusion_helpers.clean_sku(sku)

        return await self.lipgloss.preview(
            style=style, sku=sku, line_1=text_line_1, line_2=text_line_2, scale=scale,
        )

    async def handler_print_job(self, style, sku = None, text_line_1 = None, text_line_2 = None, quantity = 1, reply_to = None, source = "terminal"):
        """The whole print result, for callers that need the job id to offer a cancel."""
        if sku != None:
            sku = illusion_helpers.clean_sku(sku)

        return await self.lipgloss.print_label(
            style=style, sku=sku, line_1=text_line_1, line_2=text_line_2,
            copies=quantity, source=source, reply_to=reply_to,
        )

    @reports_service_errors
    async def handler_print(self, style, sku = None, text_line_1 = None, text_line_2 = None, quantity = 1, reply_to = None, source = "terminal"):
        result = await self.handler_print_job(
            style=style, sku=sku, text_line_1=text_line_1, text_line_2=text_line_2,
            quantity=quantity, reply_to=reply_to, source=source,
        )

        return result["message"]

    @reports_service_errors
    async def handler_print_image(self, image_bytes, description, quantity = 1, reply_to = None, source = "terminal"):
        result = await self.lipgloss.print_image(
            image_bytes, description[:60], copies=quantity, source=source, reply_to=reply_to,
        )

        return result["message"]

    @reports_service_errors
    async def handler_bulk_print_niimbot(self, sku_lower, sku_upper, reply_to = None, source = "terminal"):
        try:
            lower = int(sku_lower)
            upper = int(sku_upper)
        except ValueError:
            return "Bulk print needs two sku numbers, ex: bulk_print 1 20"

        # The roll length check lives in lipgloss now, since only it can see the printer
        result = await self.lipgloss.print_barcodes(lower, upper, source=source, reply_to=reply_to)

        return result["message"]

    @reports_service_errors
    async def handler_printer_info(self):
        return await self.lipgloss.printer_info()

    @reports_service_errors
    async def handler_print_queue(self):
        return await self.lipgloss.queue()

    @reports_service_errors
    async def handler_print_resume(self):
        return await self.lipgloss.resume()

    @reports_service_errors
    async def handler_print_clear(self):
        return await self.lipgloss.clear()

    async def handler_print_cancel_job(self, job_id):
        """The whole cancel result, for callers that need to know whether it
        actually caught the job."""
        return await self.lipgloss.cancel(job_id)

    @reports_service_errors
    async def handler_print_cancel(self, job_id):
        try:
            job_id = int(job_id)
        except ValueError:
            return f"Invalid job id: {job_id}"

        return (await self.handler_print_cancel_job(job_id))["message"]

    @reports_service_errors
    async def handler_update_item(self, sku, updates: dict[str, object]):
        sku = illusion_helpers.clean_sku(sku)

        cleaned = {}

        for key, value in updates.items():
            if value != None:
                cleaned[key] = value

        updates = cleaned

        if not updates:
            return "No updates provided."

        result = await self.claws.update_item(sku, updates)

        if result is None:
            return f"Invalid sku: {sku}"

        if result.get("rejected"):
            return result["rejected"]

        # Automatically adds a digikey link if a digikey part number was added.
        # Only after the update lands, so an invalid sku does not leave a vendor
        # row behind on an item that was never touched.
        if updates.get("DIGIKEY_PART_NUMBER") is not None:
            digikey_link = f"https://www.digikey.ca/en/products/result?keywords={updates['DIGIKEY_PART_NUMBER']}"
            await self.claws.add_vendor(sku, "Digikey", digikey_link)

        changed_fields = ", ".join(updates.keys())

        return f"Updated {sku}: {changed_fields}"

    @reports_service_errors
    async def handler_digikey_scan(self, barcode_text: str, force=False):
        """Count a DigiKey bag into stock, once.

        force counts it even if this exact label has been scanned before.
        """
        try:
            data = await self.claws.digikey_scan(barcode_text, force=force)
        except ServiceUnavailable as e:
            return f"DigiKey lookup failed: {e}"

        if data.get("duplicate"):
            return await self._duplicate_scan(barcode_text, data["duplicate"])

        dkpn = data.get("DigiKeyPartNumber")
        quantity = data.get("Quantity") or 0
        description = data.get("ProductDescription")

        if not dkpn:
            return "Barcode didn't contain a DigiKey part number"

        existing = await self.claws.item_by_dkpn(dkpn)

        if existing is not None:
            sku = existing["SKU"]
            if existing["TRACKING_MODE"] == "KANBAN":
                return f"{sku} matched {dkpn}, but item is KANBAN tracked"
            if quantity <= 0:
                return f"{sku} matched {dkpn}, but barcode had no quantity"

            result = await self.claws.increase(sku, float(quantity))

            if result is None:
                return f"Invalid sku: {sku}"

            if result.get("rejected"):
                return result["rejected"]

            message = self._increase_message(sku, quantity, result)

            return message + await self._record_scan(barcode_text, sku, dkpn, quantity)

        # New part: create a QUANTITY-tracked item pre-filled from DigiKey
        new_item = {
            "NAME": description or dkpn,
            "ORDER_QUANTITY": None,
            "TRACKING_MODE": "QUANTITY",
            "QUANTITY_ON_HAND": quantity,
            "DECREASE_AMOUNT": 1,
            "DIGIKEY_PART_NUMBER": dkpn,
            "VENDOR_1": "DigiKey",
            "LINK_1": f"https://www.digikey.ca/en/products/result?keywords={dkpn}",
            "LOW": "FALSE",
            "LOCATION": None,
            "TAGS": "per_item_tracking, digikey_scan, digikey",
            "NOTES": None,
        }

        created = await self.claws.add_item(new_item)

        if created.get("rejected"):
            return created["rejected"]

        message = f"New item {created['sku']} created from {dkpn} with {quantity} on hand"

        return message + await self._record_scan(barcode_text, created["sku"], dkpn, quantity)

    async def _record_scan(self, barcode_text, sku, dkpn, quantity):
        """Remember the bag, after its stock has landed. Returns a warning to
        append if that failed, since the stock change itself did go through."""
        try:
            await self.claws.record_digikey_scan(barcode_text, sku, dkpn, quantity)
        except ServiceUnavailable as e:
            return f"\nStock updated, but the bag could not be recorded, so scanning it again will not be caught.\n{e}"

        return ""

    async def _duplicate_scan(self, barcode_text, previous):
        sku = previous["SKU"]

        try:
            scanned_at = (
                datetime.fromisoformat(previous["SCANNED_AT"])
                .replace(tzinfo=timezone.utc)
                .astimezone()
                .strftime("%Y-%m-%d %H:%M")
            )
        except (TypeError, ValueError):
            scanned_at = previous["SCANNED_AT"]

        times = previous["TIMES_SCANNED"]
        counted = "once" if times == 1 else f"{times} times"
        quantity = illusion_helpers.format_quantity(previous["QUANTITY"] or 0)

        message = (
            f"This bag has already been counted {counted}, most recently {scanned_at} "
            f"into {sku} ({quantity} units). Stock was not changed.\n"
            f"If this really is a separate bag with an identical label, type `rescan` to count it."
        )

        return DuplicateScan(barcode_text, message, await self.handler_info(sku))

    async def handler_rename_preview_job(self, find, replace="", case_sensitive=False):
        """The whole preview result, for a caller that shows the list before
        anything is written. Undecorated like handler_print_job: a Discord
        preview needs to tell "nothing to reach" apart from "nothing matched"
        rather than have both collapse into the same string."""
        find = (find or "").strip()

        if not find:
            return {"rejected": "Give some text to find in item names."}

        return await self.claws.rename_preview(find, replace or "", case_sensitive)

    async def handler_rename_apply_job(self, find, replace="", case_sensitive=False):
        """The whole apply result, run fresh rather than off a stored preview."""
        find = (find or "").strip()

        if not find:
            return {"rejected": "Give some text to find in item names."}

        return await self.claws.rename_apply(find, replace or "", case_sensitive)

    async def handler_tag_rename_preview_job(self, find, replace, case_sensitive=False):
        """The whole preview result for merging one tag spelling into another.

        Undecorated for the same reason handler_rename_preview_job is: the
        Discord side needs "can't reach claws" told apart from "no items have
        that tag", and both a tag to find and one to rename it to are
        required -- unlike the item rename, there is no sense in which
        merging a tag into nothing is the operation being asked for.
        """
        find = (find or "").strip()
        replace = (replace or "").strip()

        if not find:
            return {"rejected": "Give a tag to find."}

        if not replace:
            return {"rejected": "Give a tag to rename it to."}

        return await self.claws.tag_rename_preview(find, replace, case_sensitive)

    async def handler_tag_rename_apply_job(self, find, replace, case_sensitive=False):
        """The whole apply result, run fresh rather than off a stored preview."""
        find = (find or "").strip()
        replace = (replace or "").strip()

        if not find:
            return {"rejected": "Give a tag to find."}

        if not replace:
            return {"rejected": "Give a tag to rename it to."}

        return await self.claws.tag_rename_apply(find, replace, case_sensitive)

    @reports_service_errors
    async def handler_get_tags(self):
        tags = await self.claws.tags()

        if not tags:
            return "No tags found."

        return Rows(tags)

    @reports_service_errors
    async def handler_search_tag(self, tag):
        results = await self.claws.items_by_tag(tag)

        if not results:
            return f"No items found with tag: {tag}"

        exclude = [
            "LINK_1",
            "VENDOR_1",
            "LINK_2",
            "VENDOR_2",
            "LINK_3",
            "VENDOR_3",
            "LINK_4",
            "VENDOR_4",
            "LINK_5",
            "VENDOR_5",
            "LOW_THREAD_ID",
            "TRACKING_MODE",
            "LOW_THRESHOLD",
            "DECREASE_AMOUNT",
            "ORDER_QUANTITY",
            "LOW",
            "NOTES",
        ]

        return Rows(results, exclude)

    @reports_service_errors
    async def handler_add_tag(self, sku: str, tags: str):
        """Adds every tag in a comma-separated list, one at a time.

        Comma-separated rather than one call per tag because that is how
        tags are typed everywhere else in the bot (add_item's tags field,
        bulk_rename_tag's autocomplete): the split is what used to be the
        "no commas" rule on a single tag, just read the other way around.
        """
        sku = illusion_helpers.clean_sku(sku)

        requested = [tag.strip() for tag in tags.split(",") if tag.strip()]

        if not requested:
            return "Tag cannot be empty."

        existing_tags = await self.claws.item_tags(sku)

        if existing_tags is None:
            return f"Invalid sku: {sku}"

        existing_keys = {existing_tag.casefold() for existing_tag in existing_tags}

        added = []
        already_had = []
        seen = set()

        for tag in requested:
            key = tag.casefold()

            if key in seen:
                continue

            seen.add(key)

            if key in existing_keys:
                already_had.append(tag)
                continue

            await self.claws.add_tag(sku, tag)
            existing_keys.add(key)
            added.append(tag)

        parts = []

        if added:
            noun = "tag" if len(added) == 1 else "tags"
            parts.append(f"Added {noun} {', '.join(f'`{tag}`' for tag in added)} to {sku}")

        if already_had:
            parts.append(f"{sku} already had {', '.join(f'`{tag}`' for tag in already_had)}")

        return "\n".join(parts)

    @reports_service_errors
    async def handler_get_locations(self):
        locations = await self.claws.locations()

        if not locations:
            return "No locations found."

        return Rows(locations)

    @reports_service_errors
    async def handler_search_location(self, location):
        results = await self.claws.items_by_location(location)

        if not results:
            return f"No items found in: {location}"

        exclude = [
            "LINK_1",
            "VENDOR_1",
            "LINK_2",
            "VENDOR_2",
            "LINK_3",
            "VENDOR_3",
            "LINK_4",
            "VENDOR_4",
            "LINK_5",
            "VENDOR_5",
            "LOW_THREAD_ID",
            "TRACKING_MODE",
            "LOW_THRESHOLD",
            "DECREASE_AMOUNT",
            "ORDER_QUANTITY",
            "LOW",
            "NOTES",
        ]

        return Rows(results, exclude)

    @reports_service_errors
    async def handler_set_location(self, sku: str, location: str | None = None):
        """An empty location clears it, which is how an item comes off a shelf."""
        sku = illusion_helpers.clean_sku(sku)
        location = (location or "").strip()

        result = await self.claws.set_location(sku, location or None)

        if result is None:
            return f"Invalid sku: {sku}"

        item = result["item"]

        if not item["LOCATION"]:
            return f"Cleared the location of {sku}"

        # The stored spelling, not the typed one: claws folds what was typed
        # onto the catalogue, so this is where someone finds out that "5a" went
        # in as "Shelf 5A (Archive)"
        response_message = f"{sku} is in {item['LOCATION']}"

        # Allowed, but worth saying out loud, because at that point it is as
        # likely to be a typo as a shelf nobody has told the catalogue about
        if not result["known"]:
            response_message += "\nThat is not one of the known locations."

        return response_message

    async def handler_uptime(self):
        """Both as human readable durations, computed on this machine's clock."""
        return (
            format_duration(service_uptime_ms(self.started_at)),
            format_duration(system_uptime_ms()),
        )
