"""HTTP clients for the illusion services.

Timeouts are short and deliberate. The kiosk reaches claws across the closet
wifi, and a request that either succeeds or fails cleanly in a few seconds is a
far better dependency than one that hangs. Only idempotent reads are retried:
a decrease that timed out may well have applied, so retrying it silently would
risk double counting.
"""

import json
from urllib.parse import quote

import httpx

CONNECT_TIMEOUT = 5
READ_TIMEOUT = 15
READ_RETRIES = 2


class ServiceUnavailable(Exception):
    """The service could not be reached, or answered with an error.

    status_code is the HTTP status when the service did answer, and None when
    it could not be reached at all. Callers distinguish "no such item" from
    "the inventory service is down" on that, never on the message text.
    """

    def __init__(self, message, status_code=None, service=None):
        super().__init__(message)
        self.status_code = status_code
        self.service = service


def error_detail(response):
    """The service's own explanation, rather than the JSON envelope around it.

    FastAPI wraps a rejection in {"detail": ...}, and these messages are read by
    people in Discord: a preview refused for a missing line should say so, not
    quote JSON at them.
    """
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]

    if isinstance(body, dict) and "detail" in body:
        return str(body["detail"])[:200]

    return response.text[:200]


class BaseClient:
    def __init__(self, url, token, name):
        self._url = url.rstrip("/")
        self._name = name
        self._client = httpx.AsyncClient(
            base_url=self._url,
            headers={"Authorization": f"Bearer {token}"} if token else {},
            timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT),
        )

    async def aclose(self):
        await self._client.aclose()

    async def _request(self, method, path, retry=False, **kwargs):
        attempts = READ_RETRIES if retry else 1
        last_error = None

        for _ in range(attempts):
            try:
                response = await self._client.request(method, path, **kwargs)
            except httpx.HTTPError as e:
                last_error = e
                continue

            if response.status_code >= 400:
                raise ServiceUnavailable(
                    f"{self._name} returned {response.status_code}: {error_detail(response)}",
                    status_code=response.status_code,
                    service=self._name,
                )

            return response

        raise ServiceUnavailable(
            f"{self._name} unreachable at {self._url} ({last_error})", service=self._name
        )

    async def get(self, path, **kwargs):
        return (await self._request("GET", path, retry=True, **kwargs)).json()

    async def or_none(self, method, path, **kwargs):
        """None for a 404, but still raises if the service itself is unreachable."""
        try:
            return await self._json(method, path, **kwargs)
        except ServiceUnavailable as e:
            if e.status_code == 404:
                return None

            raise

    async def post(self, path, **kwargs):
        return (await self._request("POST", path, **kwargs)).json()

    async def delete(self, path, **kwargs):
        return (await self._request("DELETE", path, **kwargs)).json()

    async def _json(self, method, path, **kwargs):
        return (await self._request(method, path, **kwargs)).json()

    async def health(self):
        return await self.get("/health")

    async def events(self):
        """Yield events off the service's SSE stream until the connection drops.

        The first item is always a stream.connected sentinel. The server
        subscribes before it sends any headers, so by the time this yields,
        anything published from that moment on is queued for us. Consumers use
        it to catch up on missed state without racing the subscription: catching
        up first and subscribing second would drop everything published in
        between, every single reconnect.
        """
        async with self._client.stream("GET", "/events", timeout=None) as response:
            if response.status_code >= 400:
                raise ServiceUnavailable(
                    f"{self._name} refused the event stream ({response.status_code})",
                    status_code=response.status_code,
                    service=self._name,
                )

            yield {"event": "stream.connected"}

            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    yield json.loads(line[6:])


class LipglossClient(BaseClient):
    def __init__(self, url, token):
        super().__init__(url, token, "lipgloss")

    async def print_label(self, style, sku=None, line_1=None, line_2=None,
                          copies=1, source="unknown", reply_to=None):
        return await self.post("/print", json={
            "style": style, "sku": sku, "line_1": line_1, "line_2": line_2,
            "copies": copies, "source": source, "reply_to": reply_to,
        })

    async def print_barcodes(self, lower, upper, style="slim_barcode",
                             line_1=None, line_2=None,
                             line_1_by_sku=None, line_2_by_sku=None,
                             source="unknown", reply_to=None):
        # Everything after upper is keyword-defaulted so existing positional
        # callers are unaffected, and the defaults are what the endpoint did
        # before it took any of them. Only styles that render the SKU are
        # accepted, and line_1/line_2 are required exactly when the style has
        # a cell for them -- see print_barcodes in lipgloss's service.py.
        # The _by_sku maps give individual labels their own text, for a range
        # of items that already exist. Resolve them against claws first:
        # lipgloss deliberately knows nothing about inventory.
        return await self.post("/print/barcodes", json={
            "lower": lower, "upper": upper, "style": style,
            "line_1": line_1, "line_2": line_2,
            "line_1_by_sku": line_1_by_sku, "line_2_by_sku": line_2_by_sku,
            "source": source, "reply_to": reply_to,
        })

    async def print_image(self, image_bytes, description, copies=1,
                          source="unknown", reply_to=None):
        return await self.post(
            "/print/image",
            files={"file": ("label.png", image_bytes, "image/png")},
            data={
                "description": description, "copies": str(copies),
                "source": source, **({"reply_to": reply_to} if reply_to else {}),
            },
        )

    async def preview_image(self, image_bytes, scale=3, rotate=0):
        """PNG bytes of an image as the print head would dither it.

        The counterpart to print_image, and the only way to see what one bit per
        pixel does to a picture without spending a label finding out.

        rotate turns the finished dither for reading. An image is sent the way
        the head wants it, long side down the roll; 270 gives it back the shape
        a label is read in, which is what preview() returns for every other
        style.
        """
        response = await self._request(
            "POST",
            "/preview/image",
            files={"file": ("label.png", image_bytes, "image/png")},
            data={"scale": str(scale), "rotate": str(rotate)},
        )

        return response.content

    async def preview(self, style, sku=None, line_1=None, line_2=None, scale=3):
        """PNG bytes of the label print_label would produce, same geometry and all.

        Separate from render() on purpose: render takes whatever size it is
        given, while this one is answered at the printer's own label size, which
        is the only thing worth showing someone before they commit a roll to it.
        """
        response = await self._request("POST", "/preview", json={
            "style": style, "sku": sku, "line_1": line_1, "line_2": line_2,
            "scale": scale,
        })

        return response.content

    async def render(self, style="classic_barcode", sku=None, line_1=None,
                     line_2=None, width=350, height=280, rotate=0):
        """Returns PNG bytes."""
        response = await self._request("POST", "/render", json={
            "style": style, "sku": sku, "line_1": line_1, "line_2": line_2,
            "width": width, "height": height, "rotate": rotate,
        })

        return response.content

    async def queue(self):
        return await self.get("/queue")

    async def printer_info(self):
        return (await self.get("/printer"))["message"]

    async def resume(self):
        return (await self.post("/queue/resume"))["message"]

    async def clear(self):
        return (await self.post("/queue/clear"))["message"]

    async def cancel(self, job_id):
        """{"cancelled": bool, "message": str}, since a job already printed is
        not the same answer as one pulled out of the queue."""
        return await self.delete(f"/queue/{job_id}")


class ClawsClient(BaseClient):
    def __init__(self, url, token):
        super().__init__(url, token, "claws")

    async def get_item(self, sku):
        """None when there is no such item.

        Every mutating call below is also 404-tolerant, which folds away the old
        validate_sku round trip: checking existence and then acting used to be
        two database hits, and over the closet link two round trips per command
        is worth avoiding.
        """
        return await self.or_none("GET", f"/items/{sku}")

    async def read_all(self):
        return await self.get("/items")

    async def add_item(self, item):
        """The whole response, since claws may reject the item rather than
        create one, and the caller needs to see which happened."""
        return await self.post("/items", json={"item": item})

    async def update_item(self, sku, updates):
        return await self.or_none("PATCH", f"/items/{sku}", json={"updates": updates})

    async def delete_item(self, sku):
        return await self.or_none("DELETE", f"/items/{sku}")

    async def decrease(self, sku, amount=None):
        return await self.or_none("POST", f"/items/{sku}/decrease", json={"amount": amount})

    async def increase(self, sku, amount=1):
        return await self.or_none("POST", f"/items/{sku}/increase", json={"amount": amount})

    async def set_stock(self, sku, quantity):
        return await self.or_none("PUT", f"/items/{sku}/stock", json={"quantity": quantity})

    async def resolve(self, sku):
        return await self.or_none("POST", f"/items/{sku}/resolve")

    async def set_low_thread(self, sku, thread_id):
        return await self._json("PUT", f"/items/{sku}/low-thread", json={"thread_id": thread_id})

    async def add_tag(self, sku, tag):
        return await self.or_none("POST", f"/items/{sku}/tags", json={"tag": tag})

    async def set_location(self, sku, location):
        """The updated item and whether its location is a known one, or None
        when there is no such sku."""
        return await self.or_none(
            "PUT", f"/items/{sku}/location", json={"location": location}
        )

    async def add_vendor(self, sku, vendor_name, link):
        return (await self.post(
            f"/items/{sku}/vendors", json={"vendor_name": vendor_name, "link": link}
        ))["added"]

    async def search(self, name, limit=10):
        return await self.get("/search", params={"name": name, "limit": limit})

    async def rename_preview(self, find, replace="", case_sensitive=False):
        """{"rejected": str} or {"changes": [{SKU, OLD_NAME, NEW_NAME}, ...]}."""
        return await self.post("/items/rename/preview", json={
            "find": find, "replace": replace, "case_sensitive": case_sensitive,
        })

    async def rename_apply(self, find, replace="", case_sensitive=False):
        """Same shape as rename_preview, but written to the database.

        Re-matches find/replace itself rather than being handed a list of
        skus, so a rename confirmed after the catalogue moved on renames
        what actually matches now.
        """
        return await self.post("/items/rename/apply", json={
            "find": find, "replace": replace, "case_sensitive": case_sensitive,
        })

    async def suggest(self, query, limit=25):
        return await self.get("/suggest", params={"query": query, "limit": limit})

    async def tags(self):
        return await self.get("/tags")

    async def items_by_tag(self, tag):
        return await self.get(f"/tags/{tag}/items")

    async def tag_rename_preview(self, find, replace, case_sensitive=False):
        """{"rejected": str} or {"changes": [{SKU, NAME, OLD_TAGS, NEW_TAGS}, ...]}."""
        return await self.post("/tags/rename/preview", json={
            "find": find, "replace": replace, "case_sensitive": case_sensitive,
        })

    async def tag_rename_apply(self, find, replace, case_sensitive=False):
        """Same shape as tag_rename_preview, but written to the database.

        Re-matches find/replace itself rather than being handed a list of
        skus, so a tag added or removed after the preview was shown is
        reflected rather than clobbered.
        """
        return await self.post("/tags/rename/apply", json={
            "find": find, "replace": replace, "case_sensitive": case_sensitive,
        })

    async def locations(self):
        return await self.get("/locations")

    async def suggest_locations(self, query="", limit=25):
        return await self.get(
            "/locations/suggest", params={"query": query, "limit": limit}
        )

    async def items_by_location(self, location):
        # Locations are free text off a shelf label, so "Bin Wall / bin array"
        # is a perfectly ordinary one and its slash cannot be left to split the
        # path into segments that match nothing
        return await self.get(f"/locations/{quote(str(location), safe='')}/items")

    async def status(self):
        """The whole fleet: claws plus every service it probes."""
        return await self.get("/status")

    async def register(self, payload):
        return await self.post("/register", json=payload)

    async def low_threads(self):
        """Items whose low-stock thread state needs attention, tagged with the action."""
        return await self.get("/low-threads")

    async def item_tags(self, sku):
        """None when there is no such item, a list otherwise."""
        return await self.or_none("GET", f"/items/{sku}/tags")

    async def item_by_dkpn(self, dkpn):
        return await self.get(f"/items/by-dkpn/{dkpn}")

    async def digikey_part(self, part_number):
        return await self.get(f"/digikey/part/{part_number}")

    async def digikey_scan(self, barcode, force=False):
        """DigiKey's data for the barcode, or {"duplicate": ...} describing the
        last time this bag was counted, unless force is set."""
        return await self.post("/digikey/scan", json={"barcode": barcode, "force": force})

    async def record_digikey_scan(self, barcode, sku, dkpn, quantity):
        return await self.post("/digikey/scans", json={
            "barcode": barcode,
            "sku": sku,
            "digikey_part_number": dkpn,
            "quantity": quantity,
        })
