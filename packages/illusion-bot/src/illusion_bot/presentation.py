"""Discord rendering for illusion.

Everything here needs the discord library, which is why it lives in the
frontend rather than illusion-core: claws and lipgloss must never depend on
discord, and lipgloss now returns plain data for callers to render.
"""

import io

import discord

from illusion_core.helpers import FIELD_NAMES, format_quantity, get_vendor_links, make_table

# Discord allows 25 fields per embed, and a queue that long is unreadable anyway
MAX_EMBED_JOBS = 20

# Discord maxes out at 25 embeds, anything higher gets rejected
MAX_EMBED_FIELDS = 25

# Rows per page for a paginated list embed, small enough to page through comfortably
PAGE_SIZE = 10

QUEUE_FIELD_NAMES = {
    "JOB_ID": "Job",
    "DESCRIPTION": "Label",
    "LABELS": "Labels",
    "STATE": "State",
    "SOURCE": "Source",
}


EMBED_COLOUR = discord.Colour.from_rgb(r=192, g=140, b=149) #C08C95
ALERT_COLOUR = discord.Colour.from_rgb(r=176, g=74, b=74) #B04A4A

# The preview travels as an attachment and the embed points at it by name, so
# both sides have to agree on what that name is
LABEL_IMAGE_FILENAME = "label.png"

# Styles are named for the user in the /print choices, but the command rewrites
# some of them once it knows how many lines it got, so the resolved name is what
# has to be spelled out on the preview
LABEL_STYLE_NAMES = {
    "slim_barcode": "Barcode",
    "classic_barcode": "Barcode",
    "label_barcode": "Label w/ Barcode",
    "label_1_line": "Label",
    "label_2_line": "Label, 2 lines",
    "label_1_line_qr": "Label w/ QR Code",
    "label_2_line_qr": "Label w/ QR Code, 2 lines",
    "cable_label": "Cable Label",
    "cable_label_sku": "Cable Label w/ SKU",
    "cable_label_qr": "Cable Label w/ QR Code",
}


def make_low_thread_content(item):
    stock_lines = []

    if item["TRACKING_MODE"] != "KANBAN":
        stock_lines.extend(
            [
                f"Current Stock: {format_quantity(item['QUANTITY_ON_HAND'])}",
                f"Low Threshold: {format_quantity(item['LOW_THRESHOLD'])}",
            ]
        )

    # Only when it is set: whoever restocks this has to go and find the thing,
    # and an empty line saying so helps nobody
    location_lines = [f"Location: {item['LOCATION']}"] if item.get("LOCATION") else []

    return "\n".join(
        [
            f"We are getting low on: {item['NAME']}",
            f"SKU: {item['SKU']}",
            *location_lines,
            f"Tracking Mode: {item['TRACKING_MODE']}",
            f"Order Quantity: {item['ORDER_QUANTITY']}",
            *stock_lines,
        ]
    )


def make_vendor_buttons(item):
    vendor_links = get_vendor_links(item)

    if not vendor_links:
        return None

    view = discord.ui.View()

    for vendor in vendor_links:
        # Discord doesnt allow embeded links without http:// or https://, even though thats a pretty normal thing now, but discord sucks. -PC
        if vendor["url"].startswith("http"):
            url = vendor["url"]
        else:
            url = "http://" + vendor["url"]
        view.add_item(
            discord.ui.Button(
                label=vendor["label"],
                url=url,
                style=discord.ButtonStyle.link,
            )
        )

    return view


def total_pages(rows, page_size=PAGE_SIZE):
    """How many pages `rows` spans in a list embed, at `page_size` rows per page."""
    if not rows or isinstance(rows, dict):
        return 1

    return -(-len(rows) // page_size)


def make_embed(data, exclude=None, field_names=None, title=None, description=None, colour=None, row_name=None, vertical=None, page=0):
    # vertical=None lays a single row out one field per column and anything longer
    # as a field per row, pass True or False to force one or the other
    # row_name names each of those fields after that column, instead of "Result 1"
    # page picks which PAGE_SIZE-sized slice of a multi-row list embed to render
    missing = "N/A"
    inline = False

    if title == None:
        title = "Results:"

    if colour == None:
        colour = EMBED_COLOUR

    if exclude is None:
        exclude = [""]

    if field_names is None:
        field_names = FIELD_NAMES

    if isinstance(data, dict):
        rows = [data]
    else:
        rows = data

    if not rows:
        return discord.Embed(
            title=title or "No Results",
            description=description or "No data found.",
            color=colour,
        )

    def friendly_name(field):
        return field_names.get(field, field)

    embed = discord.Embed(
        title=title,
        description=description,
        color=colour,
    )

    # Vertical Embed
    # Used when there is only one row.
    if vertical == True or (vertical == None and len(rows) == 1):
        row = rows[0]

        added_fields = 0

        for field in row:
            if field in exclude or field == row_name:
                continue

            if added_fields == MAX_EMBED_FIELDS:
                break

            value = row.get(field, missing)

            if value is None or value == "":
                value = missing

            embed.add_field(
                name=friendly_name(field),
                value=str(value),
                inline=inline,
            )

            added_fields += 1

        if added_fields == 0:
            embed.description = embed.description or "No displayable fields."

        return embed

    # Horizontal/List Embed
    # Used when there are multiple rows.
    columns = []

    for row in rows:
        for key in row:
            if key not in columns and key not in exclude and key != row_name:
                columns.append(key)

    if not columns:
        embed.description = embed.description or "No displayable fields."
        return embed

    pages = total_pages(rows)
    start = page * PAGE_SIZE
    window = rows[start:start + PAGE_SIZE]

    for index, row in enumerate(window, start=start + 1):
        lines = []

        for column in columns:
            value = row.get(column, missing)

            if value is None or value == "":
                value = missing

            lines.append(f"**{friendly_name(column)}:** {value}")

        # Best attempt at seperators
        lines.append("‎")

        embed.add_field(
            name=row.get(row_name) if row_name else f"Result {index}",
            value="\n".join(lines),
            inline=False,
        )

    if pages > 1:
        embed.set_footer(text=f"Page {page + 1} of {pages}")

    return embed


def label_file(image_bytes):
    """The preview as an attachment.

    A fresh one per message: discord.File wraps a buffer that is read to the end
    when it is sent, so the same object cannot be reused for a second message.
    """
    return discord.File(io.BytesIO(image_bytes), filename=LABEL_IMAGE_FILENAME)


def label_embed(title, description, style, sku=None, line_1=None, line_2=None,
                copies=1, urgent=False, has_image=True):
    """A label, shown with the values it was built from.

    Empty fields are left out rather than shown as N/A: which fields a style
    even uses varies, and a preview padded with blanks reads as though
    something went missing.
    """
    embed = discord.Embed(
        title=title,
        description=description,
        color=ALERT_COLOUR if urgent else EMBED_COLOUR,
    )

    if has_image:
        embed.set_image(url=f"attachment://{LABEL_IMAGE_FILENAME}")

    embed.add_field(name="Style", value=LABEL_STYLE_NAMES.get(style, style), inline=True)

    if copies > 1:
        embed.add_field(name="Copies", value=str(copies), inline=True)

    for name, value in (("SKU", sku), ("Line 1", line_1), ("Line 2", line_2)):
        if value:
            embed.add_field(name=name, value=str(value), inline=True)

    return embed


# Kept well under Discord's 1024-char field limit and its 25-field cap, same
# spirit as MAX_EMBED_JOBS: a rename big enough to blow past this is exactly
# the kind that most needs a careful look before confirming, not a wall of text
MAX_RENAME_LINES = 15
RENAME_LINE_LIMIT = 100


def _rename_line(change):
    line = f"`{change['SKU']}` {change['OLD_NAME']} -> {change['NEW_NAME']}"

    if len(line) > RENAME_LINE_LIMIT:
        line = f"{line[:RENAME_LINE_LIMIT - 1]}…"

    return line


def _tag_rename_line(change):
    line = f"`{change['SKU']}` {change['OLD_TAGS']} -> {change['NEW_TAGS']}"

    if len(line) > RENAME_LINE_LIMIT:
        line = f"{line[:RENAME_LINE_LIMIT - 1]}…"

    return line


def _change_list_embed(title, description, changes, line, urgent, verb):
    """The shared shape behind rename_embed and tag_rename_embed: a title and
    description, plus as many affected items as comfortably fit, one per
    line via whichever formatter the caller passes.

    verb switches the field heading between the preview ("3 items would
    change") and the outcome ("3 items were renamed"), so it reads right on
    both sides of the confirm button.
    """
    embed = discord.Embed(
        title=title,
        description=description,
        color=ALERT_COLOUR if urgent else EMBED_COLOUR,
    )

    if changes:
        shown = changes[:MAX_RENAME_LINES]
        lines = [line(change) for change in shown]

        hidden = len(changes) - len(shown)

        if hidden > 0:
            lines.append(f"...and {hidden} more")

        embed.add_field(
            name=f"{len(changes)} item{'s' if len(changes) != 1 else ''} {verb}",
            value="\n".join(lines),
            inline=False,
        )

    return embed


def rename_embed(title, description, changes=None, urgent=False, verb="would change"):
    """A find/replace preview or result for item names."""
    return _change_list_embed(title, description, changes, _rename_line, urgent, verb)


def tag_rename_embed(title, description, changes=None, urgent=False, verb="would change"):
    """A find/replace preview or result for merging one tag into another."""
    return _change_list_embed(title, description, changes, _tag_rename_line, urgent, verb)


def notice_embed(title, description, urgent=False):
    """A plain title and description embed, for print updates that arent a list of jobs."""
    return discord.Embed(
        title=title,
        description=description,
        color=ALERT_COLOUR if urgent else EMBED_COLOUR,
    )


def queue_text(status):
    """Render lipgloss's queue status as a terminal table."""
    if not status["jobs"]:
        return f"{status['title']}\n{status['description']}"

    table = make_table(
        status["jobs"], exclude=["HEADER"], field_names=QUEUE_FIELD_NAMES, vertical=False
    )

    return f"{status['title']}\n{status['description']}\n{table}"


def queue_embed(status):
    """Render lipgloss's queue status as a Discord embed."""
    rows = list(status["jobs"])
    description = status["description"]
    paused = status["paused"]

    hidden = len(rows) - MAX_EMBED_JOBS

    if hidden > 0:
        rows = rows[:MAX_EMBED_JOBS]
        description = f"{description}\nOnly the first {MAX_EMBED_JOBS} are listed, {hidden} more behind them."

    if not rows:
        return notice_embed(status["title"], description, urgent=paused)

    # Job id and state are already in each field name
    return make_embed(
        rows,
        exclude=["JOB_ID", "STATE"],
        field_names=QUEUE_FIELD_NAMES,
        title=status["title"],
        description=description,
        colour=ALERT_COLOUR if paused else EMBED_COLOUR,
        row_name="HEADER",
        vertical=False,
    )
