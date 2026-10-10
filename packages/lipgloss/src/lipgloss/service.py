"""lipgloss: label rendering and printing, over HTTP.

Runs on whichever machine the Niimbot is plugged into. It holds no database and
never talks to claws about inventory: callers that want an item name on a label
resolve it themselves and pass literal text, which keeps this service's only
dependency the printer in front of it.
"""

import asyncio
import json
import time
from importlib.metadata import version
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, UploadFile, Form
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from illusion_core import config as illusion_config
from illusion_core.uptime import service_uptime_ms, system_uptime_ms
from lipgloss import printer
from illusion_core.events import EventBus
from lipgloss.label_maker import (
    LabelMaker,
    LABEL_STYLES,
    PREVIEW_MAX_SCALE,
    dithered_png,
    flattened_for_print,
    missing_values,
    preview_png,
)
from lipgloss.print_queue import MAX_COPIES, PrintQueue
from lipgloss.printer import PrinterUnavailable

# From package metadata, never hardcoded: the fleet status flags a version
# mismatch across services, and a constant left behind by a release bump would
# report a mismatch that does not exist.
VERSION = version("lipgloss")

LABEL_WIDTH = 320
LABEL_HEIGHT = 96
BARCODE_WIDTH = 350
BARCODE_HEIGHT = 280


class PrintRequest(BaseModel):
    style: str
    sku: str | None = None
    line_1: str | None = None
    line_2: str | None = None
    copies: int = Field(default=1, ge=1, le=MAX_COPIES)
    source: str = "unknown"
    reply_to: str | None = None


class BarcodeRangeRequest(BaseModel):
    lower: int
    upper: int

    # Optional, and defaulting to what this endpoint used to hardcode, so
    # callers written before it existed keep working unchanged.
    #
    # Only styles that actually render the SKU are accepted -- see
    # print_barcodes.
    style: str = "slim_barcode"

    # Text for the run. line_1/line_2 are the same on every label; the _by_sku
    # maps override them for individual SKUs and are what make a range of real
    # items each carry its own name.
    #
    # The maps are filled by the caller, never by this service. lipgloss does
    # not know what a SKU means -- see the module docstring -- so anything that
    # wants item names resolves them against claws first and sends the result.
    # A SKU absent from a map falls back to the flat value.
    line_1: str | None = None
    line_2: str | None = None
    line_1_by_sku: dict[str, str] | None = None
    line_2_by_sku: dict[str, str] | None = None

    source: str = "unknown"
    reply_to: str | None = None


class RenderRequest(BaseModel):
    style: str = "classic_barcode"
    sku: str | None = None
    line_1: str | None = None
    line_2: str | None = None
    width: int = BARCODE_WIDTH
    height: int = BARCODE_HEIGHT
    rotate: int = 0


class PreviewRequest(BaseModel):
    """The print fields, minus everything about actually printing."""

    style: str
    sku: str | None = None
    line_1: str | None = None
    line_2: str | None = None
    scale: int = Field(default=3, ge=1, le=PREVIEW_MAX_SCALE)


def create_app(config_path="./lipgloss.yaml"):
    config = illusion_config.load(
        config_path,
        required=[
            "lipgloss.token",
            "lipgloss.printer.port",
            "lipgloss.printer.font_path",
        ],
    )

    token = illusion_config.get(config, "lipgloss.token")
    model = illusion_config.get(config, "lipgloss.printer.model", "d110")
    output_dir = Path(
        illusion_config.get(config, "lipgloss.labels.output_dir", "/tmp/illusion-labels")
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    events = EventBus()
    labelmaker = LabelMaker(illusion_config.get(config, "lipgloss.printer.font_path"))
    printqueue = PrintQueue(
        illusion_config.get(config, "lipgloss.printer.port"), model, events=events
    )

    started_at = time.time()

    async def require_token(authorization: str = Header(default="")):
        if authorization != f"Bearer {token}":
            raise HTTPException(status_code=401, detail="bad or missing bearer token")

    app = FastAPI(title="lipgloss", version=VERSION)
    app.state.queue = printqueue
    app.state.events = events

    @app.on_event("startup")
    async def _start():
        printqueue.start()

    @app.on_event("shutdown")
    async def _stop():
        await printqueue.stop()

    def _label_path(prefix):
        # Every job needs its own file, otherwise a label queued later would
        # overwrite one still waiting to be printed
        return str(output_dir / f"{prefix}_{time.time_ns()}")

    def _render_printable(style, sku, line_1, line_2, prefix, rotate=90):
        """The one place a printable label is rendered.

        /print and /preview both come through here, so a preview cannot quietly
        drift from what the printer is handed: same style, same geometry, and
        the only difference is the quarter turn the printer needs and a person
        reading it does not.
        """
        if style not in LABEL_STYLES:
            raise HTTPException(status_code=400, detail=f"unknown style: {style}")

        missing = missing_values(
            style, {"sku": sku, "input_text_1": line_1, "input_text_2": line_2}
        )

        if missing:
            raise HTTPException(
                status_code=400, detail=f"{style} needs {', '.join(missing)}"
            )

        return labelmaker.render_label(
            style_name=style,
            input_text_1=line_1,
            input_text_2=line_2,
            sku=sku,
            width=LABEL_WIDTH,
            height=LABEL_HEIGHT,
            rotate=rotate,
            output=_label_path(prefix),
        )

    # Health is deliberately unauthenticated so claws can report liveness even
    # if the shared token is rotated on one side only
    @app.get("/health")
    async def health():
        return {
            "service": "lipgloss",
            "version": VERSION,
            "uptime_ms": service_uptime_ms(started_at),
            "system_uptime_ms": system_uptime_ms(),
            "printer_port": illusion_config.get(config, "lipgloss.printer.port"),
            "model": model,
        }

    @app.get("/printer", dependencies=[Depends(require_token)])
    async def printer_info():
        return {"message": await printqueue.printer_info()}

    @app.get("/queue", dependencies=[Depends(require_token)])
    async def queue():
        return printqueue.status()

    @app.post("/print", dependencies=[Depends(require_token)])
    async def print_label(request: PrintRequest):
        output = _render_printable(
            request.style, request.sku, request.line_1, request.line_2, "label"
        )

        if request.sku and request.line_1:
            description = f"{request.line_1} ({request.sku})"
        elif request.sku:
            description = request.sku
        elif request.line_1:
            description = request.line_1
        else:
            description = request.style

        job, message = printqueue.add(
            output,
            description[:60],
            copies=request.copies,
            reply_to=request.reply_to,
            source=request.source,
        )

        # A job accepted onto a paused queue is not printing, and a caller
        # showing the label back to whoever asked for it should not say it is
        return {
            "job_id": job.job_id if job else None,
            "message": message,
            "paused": printqueue.paused,
        }

    @app.post("/print/barcodes", dependencies=[Depends(require_token)])
    async def print_barcodes(request: BarcodeRangeRequest):
        if request.upper < request.lower:
            return {"job_id": None, "message": f"{request.lower} is higher than {request.upper}"}

        if request.style not in LABEL_STYLES:
            return {"job_id": None, "message": f"Unknown style: {request.style}"}

        # Which cells the style has decides what this can fill in. A style with
        # no SKU cell would print the same label for every number in the range,
        # which is never what someone asking for a range of SKUs wanted.
        fields = {cell["value"] for cell in LABEL_STYLES[request.style]["cells"]}

        if "sku" not in fields:
            return {
                "job_id": None,
                "message": (
                    f"{request.style} does not put the SKU on the label, so "
                    "every label in the range would come out identical."
                ),
            }

        def _sku(number):
            return f"EER-{number:06d}"

        def _text(sku, by_sku, flat):
            """This label's text: its own if the caller gave it one, else the
            run's."""
            return (by_sku or {}).get(sku) or flat

        # Checked across the whole range rather than once, because the _by_sku
        # maps mean different labels can be missing different things. The SKU
        # is stood in for since it is supplied per label below.
        for number in range(request.lower, request.upper + 1):
            sku = _sku(number)

            missing = missing_values(
                request.style,
                {
                    "sku": "per-label",
                    "input_text_1": _text(sku, request.line_1_by_sku, request.line_1),
                    "input_text_2": _text(sku, request.line_2_by_sku, request.line_2),
                },
            )

            if missing:
                return {
                    "job_id": None,
                    "message": f"{request.style} needs {', '.join(missing)} for {sku}",
                }

        total_prints = request.upper - request.lower + 1

        # Warn about a roll that cant fit the job before printing any of it,
        # the queue itself will stop if we run out part way through anyway
        try:
            media_info = await printqueue.printer_media()
        except PrinterUnavailable as e:
            return {"job_id": None, "message": f"Unable to print, {e}"}

        remaining_media = printer.media_remaining(media_info)

        if total_prints > int(media_info["total_len"]):
            return {
                "job_id": None,
                "message": (
                    "This exceeds the max amount of prints possible on a single roll.\n"
                    "Please split this into smaller jobs. \n"
                    f"{total_prints} requested, {media_info['total_len']} possible"
                ),
            }

        if total_prints > remaining_media:
            return {
                "job_id": None,
                "message": (
                    "This exceeds the amounts of prints left on the current roll.\n"
                    "Please split this into smaller jobs. \n"
                    f"{total_prints} requested, {remaining_media} available"
                ),
            }

        def _range_label(number):
            """One label of the run.

            Routed through _render_printable rather than rendering here, so a
            range gets the same geometry and the same style checks a single
            print does.
            """
            sku = _sku(number)

            return _render_printable(
                request.style,
                sku,
                _text(sku, request.line_1_by_sku, request.line_1),
                _text(sku, request.line_2_by_sku, request.line_2),
                f"barcode_{sku}",
            )

        pages = [
            _range_label(number)
            for number in range(request.lower, request.upper + 1)
        ]

        description = f"barcodes EER-{request.lower:06d} to EER-{request.upper:06d}"

        job, message = printqueue.add(
            pages, description, reply_to=request.reply_to, source=request.source
        )

        return {"job_id": job.job_id if job else None, "message": message}

    @app.post("/print/image", dependencies=[Depends(require_token)])
    async def print_image(
        file: UploadFile,
        description: str = Form(...),
        copies: int = Form(default=1),
        source: str = Form(default="unknown"),
        reply_to: str | None = Form(default=None),
    ):
        path = Path(_label_path("image") + ".png")

        # Composited onto white rather than written through untouched. The head
        # has no alpha channel and niimprint does not look for one, so a logo
        # on a transparent background would otherwise burn the whole label.
        # /preview/image flattens through the same helper, so what was looked at
        # is what comes out.
        try:
            flattened_for_print(await file.read()).save(path, format="PNG")
        except OSError as e:
            raise HTTPException(status_code=422, detail=f"unreadable image: {e}")

        job, message = printqueue.add(
            str(path), description[:60], copies=copies, reply_to=reply_to, source=source
        )

        return {"job_id": job.job_id if job else None, "message": message}

    @app.post("/preview", dependencies=[Depends(require_token)])
    async def preview(request: PreviewRequest):
        """The label /print would make, blown up for a screen, printing nothing.

        Unrotated and scaled up, because this one is for a person to look at,
        and thrown away as soon as it has been encoded: a preview that is never
        queued has no reason to sit in the label directory.
        """
        output = _render_printable(
            request.style,
            request.sku,
            request.line_1,
            request.line_2,
            "preview",
            rotate=0,
        )

        try:
            return Response(content=preview_png(output, request.scale), media_type="image/png")
        finally:
            Path(output).unlink(missing_ok=True)

    @app.post("/preview/image", dependencies=[Depends(require_token)])
    async def preview_image(
        file: UploadFile,
        scale: int = Form(default=3),
        rotate: int = Form(default=0),
    ):
        """An uploaded image as the printer would lay it down, printing nothing.

        The counterpart to /preview, for the one kind of label this service does
        not render: /print/image hands the bytes to the print head untouched, so
        without this there is no way to find out what the dithering does to a
        picture except to spend a label on it.

        rotate turns the answer for reading, not for printing. An image arrives
        here the way the head wants it -- 96 across and 320 long -- and a label
        is read the other way round, so a caller showing one beside a rendered
        preview asks for 270 and gets the same shape /preview returns.

        Nothing is written to disk. Unlike /print/image there is no job at the
        end of this, so there is nothing for a file to outlive.
        """
        try:
            content = dithered_png(await file.read(), scale, rotate)
        except OSError as e:
            # UnidentifiedImageError is an OSError, and so is a truncated file.
            # Both are the caller's problem rather than this service's.
            raise HTTPException(status_code=422, detail=f"unreadable image: {e}")

        return Response(content=content, media_type="image/png")

    @app.post("/render", dependencies=[Depends(require_token)])
    async def render(request: RenderRequest):
        """Render a label and hand back the PNG, without printing it."""
        if request.style not in LABEL_STYLES:
            raise HTTPException(status_code=400, detail=f"unknown style: {request.style}")

        output = labelmaker.render_label(
            style_name=request.style,
            input_text_1=request.line_1,
            input_text_2=request.line_2,
            sku=request.sku,
            width=request.width,
            height=request.height,
            rotate=request.rotate,
            output=_label_path("render"),
        )

        return StreamingResponse(open(output, "rb"), media_type="image/png")

    @app.post("/queue/resume", dependencies=[Depends(require_token)])
    async def resume():
        return {"message": await printqueue.resume()}

    @app.post("/queue/clear", dependencies=[Depends(require_token)])
    async def clear():
        return {"message": printqueue.clear()}

    @app.delete("/queue/{job_id}", dependencies=[Depends(require_token)])
    async def cancel(job_id: int):
        cancelled, message = printqueue.cancel(job_id)

        return {"cancelled": cancelled, "message": message}

    @app.get("/events", dependencies=[Depends(require_token)])
    async def event_stream():
        subscription = events.subscribe()

        async def stream():
            try:
                while not subscription.dropped.is_set():
                    try:
                        event = await asyncio.wait_for(subscription.queue.get(), timeout=20)
                    except TimeoutError:
                        # Keeps the connection from being reaped by anything in
                        # between while the printer is idle
                        yield ": keepalive\n\n"
                        continue

                    yield f"data: {json.dumps(event)}\n\n"
            finally:
                events.unsubscribe(subscription)

        return StreamingResponse(stream(), media_type="text/event-stream")

    return app


def main():
    import os

    import uvicorn

    config_path = os.environ.get("LIPGLOSS_CONFIG", "./lipgloss.yaml")

    try:
        config = illusion_config.load(config_path)
    except illusion_config.ConfigError as e:
        print(e)
        raise SystemExit(1)

    uvicorn.run(
        create_app(config_path),
        host=illusion_config.get(config, "lipgloss.bind.host", "127.0.0.1"),
        port=illusion_config.get(config, "lipgloss.bind.port", 8081),
        log_level="info",
    )


if __name__ == "__main__":
    main()
