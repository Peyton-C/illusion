"""illusion, the Discord bot.

Runs on the NAS VM beside claws, on the good side of the closet wifi. It owns
everything Discord: slash commands, embeds, and the low-stock forum threads.

The thread lifecycle is driven by claws' events rather than by whichever command
changed the stock, because the kiosk has no Discord connection at all. Events
are the fast path; the reconcile pass is what makes it correct when one is
missed.
"""

import asyncio
import collections
import dataclasses
import io
import os
import signal
import socket
import time
from importlib.metadata import version

import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image

from illusion_core import config as illusion_config
from illusion_core import helpers as illusion_helpers
from illusion_core.clients import ClawsClient, LipglossClient, ServiceUnavailable
from illusion_core.commands import DB_Commands, Rows
from illusion_core import fleet
from illusion_bot import presentation

illusion_version = version("illusion-bot")

boot_time = time.time()

shutdown_event = asyncio.Event()
shutdown_started = False

health_server = None
health_task = None

# bot.wait_until_ready() can return before on_ready has finished, and on_ready
# awaits a channel fetch partway through. Anything needing the forum channel
# waits on this instead.
channel_ready = asyncio.Event()

channel = None

# Mirrors lipgloss's own limit. Slash command ranges are evaluated when the
# decorators run at import time, so this has to be defined before them.
MAX_COPIES = 100

intents = discord.Intents.default()

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    activity=discord.Game(name=f"illusion {illusion_version}"),
    status=discord.Status.online,
)


def render(result):
    """Discord rendering: strings pass through, Rows becomes an embed."""
    if isinstance(result, Rows):
        return presentation.make_embed(result.data, exclude=result.exclude)

    return result


# How long the pager buttons stay live. Nothing is at stake in paging through a
# list, so this is only about not leaving dead buttons cluttering old messages.
RESULTS_PAGER_TIMEOUT = 300


class ResultsPager(discord.ui.View):
    """Prev/Next paging for a list embed with more rows than fit on one page."""

    def __init__(self, requester, rows, exclude):
        super().__init__(timeout=RESULTS_PAGER_TIMEOUT)

        self.requester = requester
        self.rows = rows
        self.exclude = exclude
        self.page = 0
        self.message = None

        self._sync_buttons()

    def embed(self):
        return presentation.make_embed(self.rows, exclude=self.exclude, page=self.page)

    def _sync_buttons(self):
        self.previous_page.disabled = self.page == 0
        self.next_page.disabled = self.page >= presentation.total_pages(self.rows) - 1

    async def interaction_check(self, interaction: discord.Interaction):
        # A results page sitting in a busy channel is not somebody else's to page through
        if interaction.user.id == self.requester.id:
            return True

        await interaction.response.send_message(
            "That result list is someone else's, run the command yourself to page through your own.",
            ephemeral=True,
        )

        return False

    async def on_timeout(self):
        if self.message is None:
            return

        for child in self.children:
            child.disabled = True

        try:
            await self.message.edit(view=self)
        except discord.HTTPException:
            pass

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary)
    async def previous_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.embed(), view=self)


async def send_result(interaction, result, view=None):
    """Reply with whatever the handler produced, embed or plain text.

    Every handler can hand back a string instead of Rows: no search results, an
    invalid sku, or a service it could not reach. Sending that as embed= is what
    breaks, so the choice is made here once rather than at each call site.

    A Rows result with more rows than fit on one page gets a ResultsPager
    instead of the caller's view, unless the caller already supplied one.
    """
    pager = None

    if view is None and isinstance(result, Rows) and presentation.total_pages(result.data) > 1:
        pager = ResultsPager(interaction.user, result.data, result.exclude)
        view = pager
        result = pager.embed()
    else:
        result = render(result)

    if isinstance(result, discord.Embed):
        kwargs = {"embed": result}
    else:
        kwargs = {"content": result}

    if view is not None:
        kwargs["view"] = view

    # Deferring already used up the initial response, so which of the two to
    # call depends on whether the command deferred
    if interaction.response.is_done():
        message = await interaction.followup.send(**kwargs)
    else:
        await interaction.response.send_message(**kwargs)
        message = await interaction.original_response() if pager is not None else None

    if pager is not None:
        pager.message = message


async def create_low_thread(sku, item=None):
    """Open a forum thread for an item that just went low.

    Driven by claws' item.low event rather than called inline by whichever
    command changed the stock: once the kiosk is its own process it has no
    Discord connection at all, so it cannot be the one to do this.
    """
    global channel

    # Belt and braces: the callers wait on channel_ready, but a thread
    # cannot be opened without somewhere to open it
    if channel is None:
        return None

    if item is None:
        item = await claws.get_item(sku)

    if item is None:
        return None

    thread_with_message = await channel.create_thread(
        name=f"{item['NAME']}: {item['SKU']}",
        content=presentation.make_low_thread_content(item),
        view=presentation.make_vendor_buttons(item),
    )

    await claws.set_low_thread(sku, thread_with_message.thread.id)

    return thread_with_message.thread.name

async def archive_low_thread(sku, item=None):
    global bot

    # channel is only set once Discord is connected, so it doubles as the
    # check for whether there is any point looking a thread up
    if channel is None:
        return False, "Not connected to Discord."

    if item is None:
        item = await claws.get_item(sku)

    if item is None:
        return False, "No item found."

    thread_id = item.get("LOW_THREAD_ID")

    if not thread_id:
        return False, "No low-stock thread was stored for this item."

    try:
        thread = bot.get_channel(int(thread_id))

        if thread is None:
            thread = await bot.fetch_channel(int(thread_id))

    except discord.NotFound:
        # The pointer is cleared, so the drift is gone either way
        await claws.set_low_thread(sku, None)
        return True, "Stored thread no longer exists, cleared the reference."

    if not isinstance(thread, discord.Thread):
        return False, "Stored channel is not a thread."

    await thread.edit(
        archived=True,
        reason=f"{sku} resolved",
    )

    await claws.set_low_thread(sku, None)

    return True, "Low-stock thread archived."


@bot.event
async def on_ready():

    global channel
    print(f"Logged in as {bot.user}")

    guild = discord.Object(id=GUILD_ID)

    channel = bot.get_channel(FORUM_CHANNEL_ID)

    if channel is None:
        channel = await bot.fetch_channel(FORUM_CHANNEL_ID)

    if not isinstance(channel, discord.ForumChannel):
        print("That channel is not a forum channel")
        await bot.close()
        return

    bot.tree.copy_global_to(guild=guild)
    await bot.tree.sync(guild=guild)

    channel_ready.set()


# Discord takes at most 25 choices per autocomplete response and wants them
# inside about 3 seconds, so these stay one claws call with no extra work. A
# failure has to come back as an empty list because theres nowhere to show an error
AUTOCOMPLETE_LIMIT = 25
CHOICE_LABEL_LIMIT = 100


async def sku_autocomplete(interaction: discord.Interaction, current: str):
    """Suggest items by name, sku, tag or part number, filling in the sku."""
    try:
        items = await claws.suggest(current, limit=AUTOCOMPLETE_LIMIT)
    except ServiceUnavailable:
        return []

    choices = []

    for item in items:
        label = f"{item['SKU']} - {item['NAME']}"

        if len(label) > CHOICE_LABEL_LIMIT:
            label = f"{label[:CHOICE_LABEL_LIMIT - 1]}…"

        choices.append(app_commands.Choice(name=label, value=item["SKU"]))

    return choices


async def tag_autocomplete(interaction: discord.Interaction, current: str):
    """Suggest tags that already exist, so we stop growing near duplicates.

    No "(count)" suffix on the label: Discord fills the field with whatever
    the picked choice's name says, not its value, so a count left in the
    label is what ends up typed into the field.
    """
    try:
        tags = await claws.tags()
    except ServiceUnavailable:
        return []

    wanted = current.strip().casefold()
    choices = []

    for tag in tags:
        name = tag["TAG"]

        if wanted and wanted not in name.casefold():
            continue

        choices.append(app_commands.Choice(name=name, value=name))

        if len(choices) == AUTOCOMPLETE_LIMIT:
            break

    return choices


async def tags_autocomplete(interaction: discord.Interaction, current: str):
    """Suggest a next tag for a comma-separated tags field.

    Picking a choice replaces the whole field in Discord, not just what was
    being typed, so unlike tag_autocomplete this rebuilds the value as
    everything already typed plus the matched tag -- otherwise choosing a tag
    partway through a list would wipe out the ones typed before it. Only the
    text after the last comma is treated as the search term, and a tag
    already in that prefix is not suggested again.
    """
    prefix, _, partial = current.rpartition(",")
    prefix = prefix.strip()
    partial = partial.strip()

    try:
        tags = await claws.tags()
    except ServiceUnavailable:
        return []

    chosen = {tag.strip().casefold() for tag in prefix.split(",") if tag.strip()}
    wanted = partial.casefold()
    choices = []

    for tag in tags:
        name = tag["TAG"]

        if name.casefold() in chosen:
            continue

        if wanted and wanted not in name.casefold():
            continue

        value = f"{prefix}, {name}" if prefix else name

        # A choice's value has the same 100 character cap Discord puts on the
        # label, and unlike a label this cannot just be truncated with an
        # ellipsis without corrupting a tag further down the list
        if len(value) > CHOICE_LABEL_LIMIT:
            continue

        # No "(count)" suffix here unlike tag_autocomplete: Discord fills the
        # field with the displayed name on pick, not the value, so a count
        # left in the label ends up typed into the field. Fine to leave once
        # this is the only tag going in, but a second tag added after without
        # first deleting it bakes the count into the tags this item gets.
        choices.append(app_commands.Choice(name=value, value=value))

        if len(choices) == AUTOCOMPLETE_LIMIT:
            break

    return choices


async def location_autocomplete(interaction: discord.Interaction, current: str):
    """Suggest locations, so a shelf only ever ends up with one name.

    Matching happens in claws, against the nicknames as well as the names, so
    "mastercraft" finds Tool Chest 3 and "4b" finds Shelf 4B. The alias is
    shown alongside what it resolves to rather than instead of it: picking a
    nickname should not be a surprise about what gets stored.
    """
    try:
        locations = await claws.suggest_locations(current, limit=AUTOCOMPLETE_LIMIT)
    except ServiceUnavailable:
        return []

    choices = []

    for location in locations:
        name = location["LOCATION"]
        label = f"{name} ({location['COUNT']})"

        if location["ALIAS"]:
            label = f"{location['ALIAS']} -> {label}"

        if len(label) > CHOICE_LABEL_LIMIT:
            label = f"{label[:CHOICE_LABEL_LIMIT - 1]}…"

        choices.append(app_commands.Choice(name=label, value=name))

    return choices


@bot.tree.command(name="ping", description="Check bot latency")
async def ping(interaction: discord.Interaction):
    latency = round(bot.latency * 1000)
    await interaction.response.send_message(f"Pong! `{latency}ms`")

@bot.tree.command(name="about", description="About illusion")
async def about(interaction: discord.Interaction):
    # Probing every service takes a few seconds of budget, which would outrun
    # Discord's three second interaction window
    await interaction.response.defer()

    try:
        status = await claws.status()
    except ServiceUnavailable as e:
        local = fleet.health_payload(SERVICE_NAME, illusion_version, boot_time)
        embed = presentation.make_embed(
            fleet.fleet_rows({"services": [{"state": "ok", **local}]}),
            field_names=fleet.FLEET_FIELD_NAMES,
            title="illusion",
            description=f"Could not reach claws, showing only this machine.\n{e}",
            colour=presentation.ALERT_COLOUR,
            vertical=False,
        )
        await interaction.followup.send(embed=embed)
        return

    skew = fleet.version_skew(status)

    embed = presentation.make_embed(
        fleet.fleet_rows(status),
        field_names=fleet.FLEET_FIELD_NAMES,
        title="illusion",
        description=skew or f"version {illusion_version}",
        colour=presentation.ALERT_COLOUR if skew else presentation.EMBED_COLOUR,
        vertical=False,
    )

    await interaction.followup.send(embed=embed)

@bot.tree.command(name="resolve", description="Mark low stock warnings as resolved")
@app_commands.describe(sku="Item Sku")
@app_commands.autocomplete(sku=sku_autocomplete)
async def resolve(interaction: discord.Interaction, sku: str | None = None):
    channel = interaction.channel

    if not isinstance(channel, discord.Thread) and sku == None:
        await interaction.response.send_message(
            "This command requires a sku if you aren't inside a low-stock thread.",
            ephemeral=True,
        )
        return
    elif isinstance(channel, discord.Thread) and sku == None:
        sku = channel.name.split(": ")[1]
    
    response_message = await command_handler.handler_resolve(sku, False)
    await interaction.response.send_message(response_message)

    cleaned_sku = illusion_helpers.clean_sku(sku)
    await archive_low_thread(cleaned_sku)

@bot.tree.command(name="set_stock", description="Set current stock")
@app_commands.describe(sku="Item Sku", value="Stock amount")
@app_commands.autocomplete(sku=sku_autocomplete)
async def set_stock(interaction: discord.Interaction, sku: str, value: str):
    response_message = await command_handler.handler_set_stock(sku, value)
    await interaction.response.send_message(response_message)

@bot.tree.command(name="decrease", description="Decrease current stock")
@app_commands.describe(sku="Item Sku", amount="Amount to decrease by")
@app_commands.autocomplete(sku=sku_autocomplete)
async def decrease(interaction: discord.Interaction, sku: str, amount: str | None = "1"):
    response_message = await command_handler.handler_decrease(sku, amount)
    await interaction.response.send_message(response_message)

@bot.tree.command(name="increase", description="Increase current stock")
@app_commands.describe(sku="Item Sku", amount="Amount to increase by")
@app_commands.autocomplete(sku=sku_autocomplete)
async def increase(interaction: discord.Interaction, sku: str, amount: str | None = "1"):
    response_message = await command_handler.handler_increase(sku, amount)
    await interaction.response.send_message(response_message)

@bot.tree.command(name="info", description="Get info about an item")
@app_commands.describe(sku="Item Sku", hide_ext="Show or hide extra values")
@app_commands.autocomplete(sku=sku_autocomplete)
async def info(interaction: discord.Interaction, sku: str, hide_ext: bool = True):
    await interaction.response.defer()
    cleaned_sku = illusion_helpers.clean_sku(sku)
    item = await claws.get_item(cleaned_sku)

    if item is None:
        await interaction.followup.send("Invalid sku")
        return

    await send_result(
        interaction,
        await command_handler.handler_info(sku, hide_ext),
        view=presentation.make_vendor_buttons(item),
    )

@bot.tree.command(name="delete", description="Delete an item")
@app_commands.describe(sku="Item Sku")
@app_commands.autocomplete(sku=sku_autocomplete)
async def delete(interaction: discord.Interaction, sku: str):
    response_message = await command_handler.handler_delete_item(sku)
    await interaction.response.send_message(response_message)

@bot.tree.command(name="add_item", description="Add item to inventory w/ per unit tracking")
@app_commands.describe(item_name="Item Name",
                       order_quantity="Number of units to order when stock low",
                       quantity="Number of units on hand", low_threshold="Minimum Stock", digikey_part_number="Digikey Part Number",
                       vendor_1="Source 1 for Item", link_1="Source 1 Purchase Link",
                       vendor_2="Source 2 for Item", link_2="Source 2 Purchase Link",
                       vendor_3="Source 3 for Item", link_3="Source 3 Purchase Link",
                       vendor_4="Source 4 for Item", link_4="Source 4 Purchase Link",
                       vendor_5="Source 5 for Item", link_5="Source 5 Purchase Link",
                       location="Where the item lives, ex: Shelf 5A",
                       tags="Comma-separated tags", notes="Notes about this item",
                       )
@app_commands.autocomplete(location=location_autocomplete, tags=tags_autocomplete)

async def add_item(interaction: discord.Interaction, item_name: str,
                   quantity: float, order_quantity: float, low_threshold: float,
                   location: str | None = None,
                   digikey_part_number: str | None = None, tags: str | None = None, notes: str | None = None,
                   vendor_1: str | None = None, link_1: str | None = None, vendor_2: str | None = None, link_2: str | None = None,
                   vendor_3: str | None = None, link_3: str | None = None, vendor_4: str | None = None,
                   link_4: str | None = None, vendor_5: str | None = None, link_5: str | None = None):

    if tags == None:
        tags = "per_item_tracking"
    else:
        tags = f"per_item_tracking, {tags}"

    response_message = await command_handler.handler_add_item(item_name, order_quantity, "QUANTITY", quantity, low_threshold, "1", vendor_1, link_1,
                                                              vendor_2, link_2, vendor_3, link_3, vendor_4, link_4, vendor_5, link_5, digikey_part_number, tags, notes, location,)

    await interaction.response.send_message(response_message)

@bot.tree.command(name="add_item_kanban", description="Add item to inventory w/ kanban tracking")
@app_commands.describe(item_name="Item Name",
                       order_quantity="Number of units to order when stock low", digikey_part_number="Digikey Part Number",
                       vendor_1="Source 1 for Item", link_1="Source 1 Purchase Link",
                       vendor_2="Source 2 for Item", link_2="Source 2 Purchase Link",
                       vendor_3="Source 3 for Item", link_3="Source 3 Purchase Link",
                       vendor_4="Source 4 for Item", link_4="Source 4 Purchase Link",
                       vendor_5="Source 5 for Item", link_5="Source 5 Purchase Link",
                       location="Where the item lives, ex: Shelf 5A",
                       tags="Comma-separated tags", notes="Notes about this item",
                       )
@app_commands.autocomplete(location=location_autocomplete, tags=tags_autocomplete)

async def add_item_kanban(interaction: discord.Interaction, item_name: str, order_quantity: float,
                     location: str | None = None,
                     digikey_part_number: str | None = None, tags: str | None = None, notes: str | None = None,
                   vendor_1: str | None = None, link_1: str | None = None, vendor_2: str | None = None, link_2: str | None = None,
                   vendor_3: str | None = None, link_3: str | None = None, vendor_4: str | None = None,
                   link_4: str | None = None, vendor_5: str | None = None, link_5: str | None = None):

    if tags == None:
        tags = "kanban_tracking"
    else:
        tags = f"kanban_tracking, {tags}"

    response_message = await command_handler.handler_add_item(item_name, order_quantity, "KANBAN", None, None, None, vendor_1, link_1,
                                                              vendor_2, link_2, vendor_3, link_3, vendor_4, link_4, vendor_5, link_5, digikey_part_number, tags, notes, location,)

    await interaction.response.send_message(response_message)

@bot.tree.command(name="add_item_hybrid", description="Add item to inventory w/ hybrid tracking")
@app_commands.describe(item_name="Item Name",
                       order_quantity="Number of units to order when stock low", digikey_part_number="Digikey Part Number",
                       quantity="Number of units on hand", low_threshold="Minimum Stock", decrease_amount="Amount to decrease by",
                       vendor_1="Source 1 for Item", link_1="Source 1 Purchase Link",
                       vendor_2="Source 2 for Item", link_2="Source 2 Purchase Link",
                       vendor_3="Source 3 for Item", link_3="Source 3 Purchase Link",
                       vendor_4="Source 4 for Item", link_4="Source 4 Purchase Link",
                       vendor_5="Source 5 for Item", link_5="Source 5 Purchase Link",
                       location="Where the item lives, ex: Shelf 5A",
                       tags="Comma-separated tags", notes="Notes about this item",
                       )
@app_commands.autocomplete(location=location_autocomplete, tags=tags_autocomplete)

async def add_item_hybrid(interaction: discord.Interaction, item_name: str,
                   quantity: float, order_quantity: float, low_threshold: float, decrease_amount: float,
                   location: str | None = None,
                   digikey_part_number: str | None = None, tags: str | None = None, notes: str | None = None,
                   vendor_1: str | None = None, link_1: str | None = None, vendor_2: str | None = None, link_2: str | None = None, 
                   vendor_3: str | None = None, link_3: str | None = None, vendor_4: str | None = None, 
                   link_4: str | None = None, vendor_5: str | None = None, link_5: str | None = None):

    if tags == None:
        tags = "hybrid_tracking"
    else:
        tags = f"hybrid_tracking, {tags}"

    response_message = await command_handler.handler_add_item(item_name, order_quantity, "HYBRID",
                                                              quantity, low_threshold, decrease_amount, vendor_1, link_1,
                                                              vendor_2, link_2, vendor_3, link_3, vendor_4, link_4, vendor_5, link_5, digikey_part_number, tags, notes, location,)

    await interaction.response.send_message(response_message)

@bot.tree.command(name="add_item_with_dkpn", description="Add item to inventory w/ per unit tracking, getting info using a Digikey part number")
@app_commands.describe(item_name="Item Name",
                       order_quantity="Number of units to order when stock low",
                       digikey_part_number="Digikey Part Number",
                       quantity="Number of units on hand", low_threshold="Minimum Stock",
                       location="Where the item lives, ex: Shelf 5A",
                       tags="Comma-separated tags", notes="Notes about this item",
                       )
@app_commands.autocomplete(location=location_autocomplete, tags=tags_autocomplete)

async def add_item_with_dkpn(interaction: discord.Interaction, digikey_part_number: str,
                   quantity: float, order_quantity: float, low_threshold: float, item_name: str | None = None,
                   location: str | None = None, tags: str | None = None, notes: str | None = None):

    await interaction.response.defer()

    if item_name == None:
        try:
            dkpn_info = await claws.digikey_part(digikey_part_number)
        except ServiceUnavailable as e:
            await interaction.followup.send(f"Could not look up {digikey_part_number}.\n{e}")
            return

        item_name = f"{dkpn_info["Product"]["Manufacturer"]["Name"]} {dkpn_info["Product"]["Description"]["ProductDescription"]}"

    if tags == None:
        tags = "per_item_tracking, digikey_dkpn"
    else:
        tags = f"per_item_tracking, digikey_dkpn, {tags}"

    response_message = await command_handler.handler_add_item(item_name, order_quantity, "HYBRID",
                                                              quantity, low_threshold, 1, None, None,
                                                              None, None, None, None, None, None, None, None, digikey_part_number, tags, notes, location,)

    await interaction.followup.send(response_message)

# Something about search makes discord hate it, no clue why -PC
@bot.tree.command(name="search", description="Search inventory by item name")
@app_commands.describe(name="Item name")
async def search(interaction: discord.Interaction, name: str):
    await interaction.response.defer()

    await send_result(interaction, await command_handler.handler_search(name))

# How long the rename buttons stay live. Same span as a print preview: long
# enough to read the whole list, short enough that a stale preview cannot be
# committed against a catalogue that has moved on.
RENAME_BUTTON_TIMEOUT = 300


@dataclasses.dataclass
class RenameJob:
    """A find/replace the command has worked out is worth previewing.

    Held onto so the Rename button re-runs exactly this find/replace rather
    than trusting the list of skus a preview showed minutes earlier -- an item
    added, edited, or deleted in the meantime is picked up correctly because
    the match is redone at confirm time, not replayed from what was on screen.
    """

    find: str
    replace: str = ""
    case_sensitive: bool = False


class ConfirmRename(discord.ui.View):
    """The preview step: the proposed renames are on screen, nothing is written yet."""

    def __init__(self, requester, job, changes):
        super().__init__(timeout=RENAME_BUTTON_TIMEOUT)

        self.requester = requester
        self.job = job
        self.changes = changes
        self.message = None

    async def interaction_check(self, interaction: discord.Interaction):
        # A preview sitting in a busy channel is not somebody else's to commit
        # a batch of renames to
        if interaction.user.id == self.requester.id:
            return True

        await interaction.response.send_message(
            "That preview is someone else's, run /bulk_rename to get your own.", ephemeral=True
        )

        return False

    async def on_timeout(self):
        if self.message is None:
            return

        embed = presentation.rename_embed(
            title="Rename Preview",
            description="This preview expired, nothing was renamed.",
            changes=self.changes,
        )

        try:
            await self.message.edit(embed=embed, view=None)
        except discord.HTTPException:
            pass

    @discord.ui.button(label="Rename", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()

        await interaction.response.defer()

        try:
            result = await command_handler.handler_rename_apply_job(
                self.job.find, self.job.replace, self.job.case_sensitive
            )
        except ServiceUnavailable as e:
            await interaction.edit_original_response(
                embed=presentation.rename_embed(
                    title="Rename Failed",
                    description=f"Unable to reach claws.\n{e}",
                    urgent=True,
                ),
                view=None,
            )
            return

        if result.get("rejected"):
            embed = presentation.rename_embed(
                title="Not Renamed", description=result["rejected"], urgent=True,
            )
        else:
            changes = result["changes"]

            if not changes:
                embed = presentation.rename_embed(
                    title="Not Renamed",
                    description="Nothing still matched by the time this was confirmed.",
                )
            else:
                embed = presentation.rename_embed(
                    title="Renamed",
                    description=f"Renamed {len(changes)} item{'s' if len(changes) != 1 else ''}.",
                    changes=changes,
                    verb="were renamed",
                )

        await interaction.edit_original_response(embed=embed, view=None)

    @discord.ui.button(label="Discard", style=discord.ButtonStyle.secondary)
    async def discard(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()

        embed = presentation.rename_embed(
            title="Rename Preview",
            description="Discarded, nothing was renamed.",
            changes=self.changes,
        )

        await interaction.response.edit_message(embed=embed, view=None)


@bot.tree.command(name="bulk_rename", description="Find and replace text within item names")
@app_commands.describe(
    find="Text to find in item names",
    replace="Text to replace it with, leave empty to remove it",
    case_sensitive="Only match text with the exact same capitalization",
)
async def bulk_rename(interaction: discord.Interaction, find: str, replace: str = "",
                      case_sensitive: bool = False):
    await interaction.response.defer()

    job = RenameJob(find=find, replace=replace, case_sensitive=case_sensitive)

    try:
        result = await command_handler.handler_rename_preview_job(
            job.find, job.replace, job.case_sensitive
        )
    except ServiceUnavailable as e:
        await interaction.followup.send(f"Unable to reach claws.\n{e}")
        return

    if result.get("rejected"):
        await interaction.followup.send(result["rejected"])
        return

    changes = result["changes"]

    if not changes:
        await interaction.followup.send(f'No item names contain "{job.find}".')
        return

    view = ConfirmRename(interaction.user, job, changes)

    view.message = await interaction.followup.send(
        embed=presentation.rename_embed(
            title="Rename Preview",
            description="Nothing has been renamed yet.",
            changes=changes,
        ),
        view=view,
    )

@dataclasses.dataclass
class TagRenameJob:
    """A tag find/replace the command has worked out is worth previewing.

    Held onto so the Rename button re-runs exactly this find/replace rather
    than trusting the list of skus a preview showed minutes earlier, for the
    same reason RenameJob is.
    """

    find: str
    replace: str
    case_sensitive: bool = False


class ConfirmTagRename(discord.ui.View):
    """The preview step: the items whose tags would change are on screen,
    nothing is written yet."""

    def __init__(self, requester, job, changes):
        super().__init__(timeout=RENAME_BUTTON_TIMEOUT)

        self.requester = requester
        self.job = job
        self.changes = changes
        self.message = None

    async def interaction_check(self, interaction: discord.Interaction):
        # A preview sitting in a busy channel is not somebody else's to commit
        # a batch of tag renames to
        if interaction.user.id == self.requester.id:
            return True

        await interaction.response.send_message(
            "That preview is someone else's, run /rename_tag to get your own.", ephemeral=True
        )

        return False

    async def on_timeout(self):
        if self.message is None:
            return

        embed = presentation.tag_rename_embed(
            title="Tag Rename Preview",
            description="This preview expired, nothing was renamed.",
            changes=self.changes,
        )

        try:
            await self.message.edit(embed=embed, view=None)
        except discord.HTTPException:
            pass

    @discord.ui.button(label="Rename", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()

        await interaction.response.defer()

        try:
            result = await command_handler.handler_tag_rename_apply_job(
                self.job.find, self.job.replace, self.job.case_sensitive
            )
        except ServiceUnavailable as e:
            await interaction.edit_original_response(
                embed=presentation.tag_rename_embed(
                    title="Rename Failed",
                    description=f"Unable to reach claws.\n{e}",
                    urgent=True,
                ),
                view=None,
            )
            return

        if result.get("rejected"):
            embed = presentation.tag_rename_embed(
                title="Not Renamed", description=result["rejected"], urgent=True,
            )
        else:
            changes = result["changes"]

            if not changes:
                embed = presentation.tag_rename_embed(
                    title="Not Renamed",
                    description="Nothing still matched by the time this was confirmed.",
                )
            else:
                embed = presentation.tag_rename_embed(
                    title="Renamed",
                    description=(
                        f'Merged "{self.job.find}" into "{self.job.replace}" on '
                        f"{len(changes)} item{'s' if len(changes) != 1 else ''}."
                    ),
                    changes=changes,
                    verb="were updated",
                )

        await interaction.edit_original_response(embed=embed, view=None)

    @discord.ui.button(label="Discard", style=discord.ButtonStyle.secondary)
    async def discard(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()

        embed = presentation.tag_rename_embed(
            title="Tag Rename Preview",
            description="Discarded, nothing was renamed.",
            changes=self.changes,
        )

        await interaction.response.edit_message(embed=embed, view=None)


@bot.tree.command(name="rename_tag", description="Rename a tag across every item that has it, merging it into an existing one if it matches")
@app_commands.describe(
    find_tag="Existing tag to rename",
    replace_tag="Tag to rename it to",
    case_sensitive="Only match a tag with the exact same capitalization",
)
@app_commands.autocomplete(find_tag=tag_autocomplete, replace_tag=tag_autocomplete)
async def rename_tag(interaction: discord.Interaction, find_tag: str, replace_tag: str,
                     case_sensitive: bool = False):
    await interaction.response.defer()

    job = TagRenameJob(find=find_tag, replace=replace_tag, case_sensitive=case_sensitive)

    try:
        result = await command_handler.handler_tag_rename_preview_job(
            job.find, job.replace, job.case_sensitive
        )
    except ServiceUnavailable as e:
        await interaction.followup.send(f"Unable to reach claws.\n{e}")
        return

    if result.get("rejected"):
        await interaction.followup.send(result["rejected"])
        return

    changes = result["changes"]

    if not changes:
        await interaction.followup.send(f'No items are tagged "{job.find}".')
        return

    view = ConfirmTagRename(interaction.user, job, changes)

    view.message = await interaction.followup.send(
        embed=presentation.tag_rename_embed(
            title="Tag Rename Preview",
            description=f'Nothing has changed yet. Renaming "{job.find}" to "{job.replace}".',
            changes=changes,
        ),
        view=view,
    )

@bot.tree.command(name="search_tag", description="Search inventory by tag")
@app_commands.describe(tag="Tag to search for")
@app_commands.autocomplete(tag=tag_autocomplete)
async def search_tag(interaction: discord.Interaction, tag: str):
    await interaction.response.defer()

    await send_result(interaction, await command_handler.handler_search_tag(tag))


@bot.tree.command(name="get_tags", description="List all item tags")
async def get_tags(interaction: discord.Interaction):
    await interaction.response.defer()

    await send_result(interaction, await command_handler.handler_get_tags())

@bot.tree.command(name="add_tag", description="Add one or more tags to an item")
@app_commands.describe(sku="Item SKU", tags="Tag to add, or several separated by commas")
@app_commands.autocomplete(sku=sku_autocomplete, tags=tags_autocomplete)
async def add_tag(interaction: discord.Interaction, sku: str, tags: str):
    response_message = await command_handler.handler_add_tag(sku, tags)
    await interaction.response.send_message(response_message)

@bot.tree.command(name="get_locations", description="List every location in use")
async def get_locations(interaction: discord.Interaction):
    await interaction.response.defer()

    await send_result(interaction, await command_handler.handler_get_locations())

@bot.tree.command(name="search_location", description="List the items in a location")
@app_commands.describe(location="Location to search for")
@app_commands.autocomplete(location=location_autocomplete)
async def search_location(interaction: discord.Interaction, location: str):
    await interaction.response.defer()

    await send_result(interaction, await command_handler.handler_search_location(location))

@bot.tree.command(name="set_location", description="Set where an item lives, or clear it")
@app_commands.describe(sku="Item SKU", location="Where the item lives, leave empty to clear it")
@app_commands.autocomplete(sku=sku_autocomplete, location=location_autocomplete)
async def set_location(interaction: discord.Interaction, sku: str, location: str | None = None):
    response_message = await command_handler.handler_set_location(sku, location)
    await interaction.response.send_message(response_message)

@bot.tree.command(name="generate_barcode", description="Generate a barcode")
@app_commands.describe(sku="Item Sku")
@app_commands.autocomplete(sku=sku_autocomplete)
async def generate_barcode(interaction: discord.Interaction, sku: str):
    sku = illusion_helpers.clean_sku(sku)
    
    try:
        barcode_bytes = await command_handler.handler_generate_barcode(sku)
    except ServiceUnavailable as e:
        await interaction.response.send_message(f"Unable to reach the print server.\n{e}")
        return

    file = discord.File(io.BytesIO(barcode_bytes), filename=f"{sku}.png")

    await interaction.response.send_message(f"Barcode", file=file)

def make_notifier(interaction: discord.Interaction):
    """Print jobs finish long after the slash command is answered, so updates go to the channel.

    Returns a reply_to token rather than a callback: lipgloss is a separate
    process now and cannot hold a reference to a coroutine in this one. It
    echoes the token on every event about the job, and the dispatcher below
    turns it back into a channel message.
    """
    channel = interaction.channel
    user = interaction.user
    token = f"bot:{interaction.id}"

    async def notify(event):
        if channel is None:
            return

        embed = presentation.notice_embed(
            event["title"], event["message"], urgent=event["urgent"]
        )

        # The mention has to sit outside the embed to actually ping
        await channel.send(user.mention if event["urgent"] else None, embed=embed)

    register_notifier(token, notify)

    return token


# reply_to token -> coroutine handling that submitter's events. Bounded, because
# a token is registered per print command and only the ones whose jobs finish
# cleanly get removed; a printer left broken for a week must not grow this
# without limit.
notifiers = collections.OrderedDict()

MAX_NOTIFIERS = 100


def register_notifier(token, handler):
    notifiers[token] = handler
    notifiers.move_to_end(token)

    while len(notifiers) > MAX_NOTIFIERS:
        notifiers.popitem(last=False)


# Events are the fast path, reconciliation is the correctness guarantee. Nothing
# is replayed on reconnect, and a subscriber that falls far enough behind is
# evicted outright, so the bot never assumes the stream told it everything.
RECONCILE_INTERVAL = 900


async def claws_event_loop():
    """Own the low-stock thread lifecycle, driven by claws.

    This is the coupling the split exists to break. Creating the thread used to
    happen inline inside handler_decrease, which only worked because the bot and
    the kiosk shared a process. Now claws reports the transition and whoever is
    connected to Discord reacts to it, so a scan at the kiosk still opens a
    thread even though the kiosk cannot talk to Discord at all.
    """
    await channel_ready.wait()

    while not shutdown_event.is_set():
        try:
            async for event in claws.events():
                # Reconcile once subscribed, never before: anything that happens
                # while we catch up is queued rather than lost
                if event["event"] == "stream.connected":
                    await reconcile_low_threads(quiet=True)
                    continue

                try:
                    if event["event"] == "item.low":
                        await create_low_thread(event["sku"], event["item"])
                    elif event["event"] == "item.resolved":
                        await archive_low_thread(event["sku"], event["item"])
                except Exception as e:
                    print(f"Unable to handle {event['event']} for {event['sku']}: {e}")
        except Exception as e:
            if shutdown_event.is_set():
                return

            print(f"claws event stream dropped ({e}), reconnecting")

        await asyncio.sleep(5)


async def reconcile_loop():
    """Periodic safety net against a silently missed event."""
    await channel_ready.wait()

    while not shutdown_event.is_set():
        await asyncio.sleep(RECONCILE_INTERVAL)

        if shutdown_event.is_set():
            return

        await reconcile_low_threads(quiet=True)


async def reconcile_low_threads(quiet=False):
    """Bring Discord back in line with claws, which is the source of truth.

    claws decides what needs doing; this only carries it out. Catches
    transitions that happened while the bot was down, and any event lost because
    the stream dropped or this subscriber was evicted for falling behind.
    """
    try:
        rows = await claws.low_threads()
    except ServiceUnavailable as e:
        print(f"Could not reconcile low-stock threads: {e}")
        return

    fixed = 0

    for row in rows:
        item = row["item"]
        sku = item["SKU"]

        try:
            if row["action"] == "create":
                thread_name = await create_low_thread(sku, item)

                if thread_name:
                    print(f"Reconciled {sku}: was low with no thread, opened {thread_name}")
                    fixed += 1
                else:
                    print(f"Could not reconcile {sku}: thread was not created")
            elif row["action"] == "archive":
                archived, detail = await archive_low_thread(sku, item)

                if archived:
                    print(f"Reconciled {sku}: no longer low. {detail}")
                    fixed += 1
                else:
                    print(f"Could not reconcile {sku}: {detail}")
        except Exception as e:
            print(f"Could not reconcile {sku}: {e}")

    if not quiet or fixed:
        print(f"Low-stock threads reconciled: {len(rows)} tracked, {fixed} corrected")


async def lipgloss_event_loop():
    """Route print events back to whoever asked for the job.

    Reconnects on its own: lipgloss restarting, or the link dropping, must not
    silently end print notifications for the rest of the session.
    """
    await bot.wait_until_ready()

    while not shutdown_event.is_set():
        try:
            async for event in lipgloss.events():
                if event["event"] == "stream.connected":
                    continue

                handler = notifiers.get(event.get("reply_to"))

                if handler is None:
                    continue

                try:
                    await handler(event)
                except Exception as e:
                    print(f"Unable to deliver print update: {e}")

                if event["event"] == "job.done":
                    notifiers.pop(event["reply_to"], None)
        except Exception as e:
            if shutdown_event.is_set():
                return

            print(f"lipgloss event stream dropped ({e}), reconnecting")

        await asyncio.sleep(5)

# How long the buttons under a print stay live. Long enough to walk over and
# look at the printer, short enough that a message left in the scrollback cannot
# fire a print into an empty room an hour later.
PRINT_BUTTON_TIMEOUT = 300

# Enough to read a 320x96 label in a Discord message without it filling the
# channel
PREVIEW_SCALE = 3


@dataclasses.dataclass
class LabelPrint:
    """A print the command has finished working out.

    Held onto so a button pressed minutes later still runs exactly the job that
    was previewed, rather than reassembling it from the message it is attached
    to.
    """

    style: str
    sku: str | None = None
    line_1: str | None = None
    line_2: str | None = None
    quantity: int = 1

    def embed(self, title, description, urgent=False, has_image=True):
        return presentation.label_embed(
            title=title,
            description=description,
            style=self.style,
            sku=self.sku,
            line_1=self.line_1,
            line_2=self.line_2,
            copies=self.quantity,
            urgent=urgent,
            has_image=has_image,
        )


def label_message(embed, view=None, preview_bytes=None):
    """Send kwargs for a message about a label, leaving out what it has not got.

    discord.py wants absent rather than None for both of these, and the preview
    is optional in two different ways: the image may have failed to render, and
    a finished print has nothing left to cancel.
    """
    kwargs = {"embed": embed}

    if preview_bytes is not None:
        kwargs["file"] = presentation.label_file(preview_bytes)

    if view is not None:
        kwargs["view"] = view

    return kwargs


async def run_print(interaction, job, preview_bytes):
    """Queue the job, and describe it with a way out while there still is one.

    Returns the embed and the view to put under it. The cancel button is only
    offered when lipgloss actually took the job: there is nothing to cancel when
    it refused it.
    """
    try:
        result = await command_handler.handler_print_job(
            style=job.style,
            sku=job.sku,
            text_line_1=job.line_1,
            text_line_2=job.line_2,
            quantity=job.quantity,
            reply_to=make_notifier(interaction),
            source=f"discord/{interaction.user.display_name}",
        )
    except ServiceUnavailable as e:
        return job.embed(
            title="Print Failed",
            description=f"Unable to reach the print server.\n{e}",
            urgent=True,
            has_image=preview_bytes is not None,
        ), None

    job_id = result.get("job_id")
    paused = result.get("paused")

    if job_id is None:
        title = "Not Printed"
    elif paused:
        title = "Print Queue Paused"
    else:
        title = "Printing"

    embed = job.embed(
        title=title,
        description=result["message"],
        urgent=job_id is None or bool(paused),
        has_image=preview_bytes is not None,
    )

    if job_id is None:
        return embed, None

    return embed, CancelPrint(job, job_id, has_image=preview_bytes is not None)


class ConfirmPrint(discord.ui.View):
    """The preview step: the label is on screen and nothing has printed yet."""

    def __init__(self, requester, job, preview_bytes):
        super().__init__(timeout=PRINT_BUTTON_TIMEOUT)

        self.requester = requester
        self.job = job
        self.preview_bytes = preview_bytes
        self.message = None

    async def interaction_check(self, interaction: discord.Interaction):
        # A preview sitting in a busy channel is not somebody else's to commit
        # to a roll of labels
        if interaction.user.id == self.requester.id:
            return True

        await interaction.response.send_message(
            "That preview is someone else's, run /print to get your own.", ephemeral=True
        )

        return False

    async def on_timeout(self):
        # Buttons that quietly stop working are worse than no buttons, so say
        # what happened to them
        if self.message is None:
            return

        embed = self.job.embed(
            title="Label Preview",
            description="This preview expired, nothing was printed.",
        )

        try:
            await self.message.edit(embed=embed, view=None)
        except discord.HTTPException:
            # Deleted, or the channel went away. An expired preview is not worth
            # taking a background task down over
            pass

    @discord.ui.button(label="Print", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()

        # Printing is a round trip to another machine, so get off Discord's
        # three second clock before making it
        await interaction.response.defer()

        embed, view = await run_print(interaction, self.job, self.preview_bytes)

        # view is passed even when it is None, which is what takes the preview's
        # own buttons off the message: leaving it out would keep them there,
        # doing nothing
        message = await interaction.edit_original_response(embed=embed, view=view)

        if view is not None:
            view.message = message

    @discord.ui.button(label="Discard", style=discord.ButtonStyle.secondary,)
    async def discard(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()

        embed = self.job.embed(
            title="Label Preview", description="Discarded, nothing was printed."
        )

        await interaction.response.edit_message(embed=embed, view=None)


class CancelPrint(discord.ui.View):
    """A one press /print_cancel for the job this message is about.

    Deliberately not locked to whoever printed it: the same job is already
    cancellable by anyone through /print_cancel, and whoever is standing at the
    printer watching it chew through the wrong label is usually not the person
    who sent it.
    """

    def __init__(self, job, job_id, has_image=True):
        super().__init__(timeout=PRINT_BUTTON_TIMEOUT)

        self.job = job
        self.job_id = job_id
        self.has_image = has_image
        self.message = None

    async def on_timeout(self):
        if self.message is None:
            return

        try:
            await self.message.edit(view=None)
        except discord.HTTPException:
            pass

    @discord.ui.button(label="Cancel Job", style=discord.ButtonStyle.danger, emoji="✖️")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        try:
            result = await command_handler.handler_print_cancel_job(self.job_id)
        except ServiceUnavailable as e:
            # The button stays live: the printer is the thing that is broken,
            # and the job may well still be sat in the queue
            await interaction.followup.send(
                f"Unable to reach the print server.\n{e}", ephemeral=True
            )
            return

        self.stop()

        embed = self.job.embed(
            title="Print Cancelled" if result["cancelled"] else "Nothing to Cancel",
            description=result["message"],
            urgent=not result["cancelled"],
            has_image=self.has_image,
        )

        await interaction.edit_original_response(embed=embed, view=None)


@bot.tree.command(name="print", description="Print a label")
@app_commands.describe(style="Label style", text_line_1="Text Line 1", text_line_2="Text Line 2", sku="Item Sku", get_text_from_sku="Get the item name from the provided sku", quantity="Number of copies to print", preview="Look at the label and confirm before anything prints",)
@app_commands.choices(
    style=[
        app_commands.Choice(name="Barcode (Requires sku)", value="slim_barcode"),
        app_commands.Choice(name="Label w/ Barcode (Requires sku and text_line_1)", value="label_barcode"),
        app_commands.Choice(name="Label w/ QR Code (Requires sku and text_line_1, optionally text_line_2)", value="label_qr"),
        app_commands.Choice(name="Label (Requires text_line_1, optionally text_line_2)", value="label"),
        app_commands.Choice(name="Cable Label (Requires text_line_1, optionally text_line_2)", value="cable_label"),
        app_commands.Choice(name="Cable Label w/ SKU (Requires text_line_1 and sku, optionally text_line_2)", value="cable_label_sku"),
        app_commands.Choice(name="Cable Label w/ QR Code (Requires sku and text_line_1)", value="cable_label_qr"),
    ]
)
@app_commands.autocomplete(sku=sku_autocomplete)
async def print_niimbot(interaction: discord.Interaction, style: app_commands.Choice[str], sku: str | None = None,
                        text_line_1: str | None = None, text_line_2: str | None = None, get_text_from_sku: bool = False,
                        quantity: app_commands.Range[int, 1, MAX_COPIES] = 1, preview: bool = False,):
    if not PRINTING_ENABLED:
        await interaction.response.send_message(f"Printer not enabled")
        return

    style_name = style.value

    if sku == None and get_text_from_sku == True:
        await interaction.response.send_message(f"SKU required to get item text from sku")
        return

    if get_text_from_sku == True:
        sku = illusion_helpers.clean_sku(sku)
        item = await claws.get_item(sku)

        if item is None:
            await interaction.response.send_message(f"Invalid sku: {sku}")
            return

        text_line_1 = item["NAME"]

    # Make sure we have all required values for each style
    if text_line_1 == None and (style_name == "label" or style_name == "label_barcode" or style_name == "cable_label" or style_name == "cable_label_qr" or style_name == "label_qr" or style_name == "cable_label_sku"):
        await interaction.response.send_message(f"Style: {style_name} requires text_line_1")
        return
    if text_line_2 == None and (style_name == "cable_label" or style_name == "cable_label_sku"):
        text_line_2 = text_line_1
    if sku == None and (style_name == "slim_barcode" or style_name == "label_barcode" or style_name == "cable_label_qr" or style_name == "label_qr" or style_name == "cable_label_sku"):
        await interaction.response.send_message(f"Style: {style_name} requires sku")
        return

    await interaction.response.defer()

    if style_name == "label":
        if text_line_2 == None:
            style_name = "label_1_line"
        else:
            style_name = "label_2_line"

    if style_name == "label_qr":
        if text_line_2 == None:
            style_name = "label_1_line_qr"
        else:
            style_name = "label_2_line_qr"

    job = LabelPrint(style=style_name, sku=sku, line_1=text_line_1, line_2=text_line_2, quantity=quantity)

    try:
        preview_bytes = await command_handler.handler_preview_label(
            style=job.style, sku=job.sku, text_line_1=job.line_1, text_line_2=job.line_2,
            scale=PREVIEW_SCALE,
        )
    except ServiceUnavailable as e:
        if preview:
            # Looking at it first was the whole point of the command, so there
            # is nothing useful left to do
            await interaction.followup.send(f"Unable to reach the print server.\n{e}")
            return

        # Otherwise the picture was only ever a courtesy, and the print itself
        # reports its own failure well enough
        preview_bytes = None

    if preview:
        view = ConfirmPrint(interaction.user, job, preview_bytes)

        view.message = await interaction.followup.send(
            **label_message(
                job.embed(
                    title="Label Preview",
                    description="Nothing has printed yet.",
                ),
                view,
                preview_bytes,
            )
        )

        return

    embed, view = await run_print(interaction, job, preview_bytes)

    message = await interaction.followup.send(**label_message(embed, view, preview_bytes))

    if view is not None:
        view.message = message

@bot.tree.command(name="print_image", description="Print an image")
@app_commands.describe(image="Image to print", rotate="Degrees to rotate by", quantity="Number of copies to print")
async def print_image(interaction: discord.Interaction, image: discord.Attachment, rotate: int = 0,
                      quantity: app_commands.Range[int, 1, MAX_COPIES] = 1,):
    if not PRINTING_ENABLED:
        await interaction.response.send_message(f"Printer not enabled")
        return
    
    if image.content_type is None or not image.content_type.startswith("image/"):
        await interaction.response.send_message("Please upload a valid image.", ephemeral=True)
        return
    
    await interaction.response.defer()

    image_bytes = await image.read()
    with Image.open(io.BytesIO(image_bytes)) as img:
        rotated = img.rotate(rotate, expand=True)
        resized = rotated.resize((96, 320))

        # lipgloss is a separate process and may be on another machine, so the
        # rendered image travels with the request instead of by path
        buffer = io.BytesIO()
        resized.save(buffer, format="PNG")
        resized_bytes = buffer.getvalue()

    response_message = await command_handler.handler_print_image(resized_bytes, image.filename, quantity=quantity,
                                                                reply_to=make_notifier(interaction), source=f"discord/{interaction.user.display_name}",)
    await interaction.followup.send(response_message)

@bot.tree.command(name="print_queue", description="Show what the printer is working through")
async def print_queue_status(interaction: discord.Interaction):
    if not PRINTING_ENABLED:
        await interaction.response.send_message(f"Printer not enabled")
        return

    await send_result(
        interaction,
        presentation.queue_embed(await command_handler.handler_print_queue()),
    )

@bot.tree.command(name="print_resume", description="Resume the print queue after fixing the printer")
async def print_resume(interaction: discord.Interaction):
    if not PRINTING_ENABLED:
        await interaction.response.send_message(f"Printer not enabled")
        return

    await interaction.response.defer()

    response_message = await command_handler.handler_print_resume()
    await interaction.followup.send(response_message)

@bot.tree.command(name="print_cancel", description="Cancel a queued print job")
@app_commands.describe(job_id="Job id from /print_queue")
async def print_cancel(interaction: discord.Interaction, job_id: int):
    if not PRINTING_ENABLED:
        await interaction.response.send_message(f"Printer not enabled")
        return

    response_message = await command_handler.handler_print_cancel(job_id)
    await interaction.response.send_message(response_message)

@bot.tree.command(name="print_clear", description="Clear every job from the print queue")
async def print_clear(interaction: discord.Interaction):
    if not PRINTING_ENABLED:
        await interaction.response.send_message(f"Printer not enabled")
        return

    response_message = await command_handler.handler_print_clear()
    await interaction.response.send_message(response_message)

@bot.tree.command(name="printer_info", description="Get info about the printer")
async def printer_info(interaction: discord.Interaction):
    if not PRINTING_ENABLED:
        await interaction.response.send_message(f"Printer not enabled")
        return
    
    await interaction.response.defer()

    response_message = await command_handler.handler_printer_info()
    await interaction.followup.send(response_message)

@bot.tree.command(name="update_item", description="Update an existing item")
@app_commands.describe(sku="Item SKU", item_name="Item Name",
                       order_quantity="Number of units to order when stock low",
                       quantity="Number of units on hand", low_threshold="Minimum Stock", decrease_amount="Amount to decrease by",
                       digikey_part_number="Digikey Part Number",
                       vendor_1="Source 1 for Item", link_1="Source 1 Purchase Link",
                       vendor_2="Source 2 for Item", link_2="Source 2 Purchase Link",
                       vendor_3="Source 3 for Item", link_3="Source 3 Purchase Link",
                       vendor_4="Source 4 for Item", link_4="Source 4 Purchase Link",
                       vendor_5="Source 5 for Item", link_5="Source 5 Purchase Link",
                       location="Where the item lives, ex: Shelf 5A",
                       tags="Comma-separated tags", notes="Notes about this item",
                       )
@app_commands.autocomplete(sku=sku_autocomplete, location=location_autocomplete, tags=tags_autocomplete)
async def update_item(interaction: discord.Interaction, sku: str,
                      item_name: str | None = None, location: str | None = None,
                      quantity: str | None = None, order_quantity: str | None = None,
                      low_threshold: str | None = None, decrease_amount: str | None = None, 
                      digikey_part_number: str | None = None, tags: str | None = None, notes: str | None = None,
                      vendor_1: str | None = None, link_1: str | None = None, vendor_2: str | None = None, link_2: str | None = None, 
                      vendor_3: str | None = None, link_3: str | None = None, vendor_4: str | None = None, 
                      link_4: str | None = None, vendor_5: str | None = None, link_5: str | None = None):

    updates = {
            "NAME": item_name,
            "ORDER_QUANTITY": order_quantity,
            "TRACKING_MODE": None,
            "QUANTITY_ON_HAND": quantity,
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
            "LOW": None,
            "DIGIKEY_PART_NUMBER": digikey_part_number,
            "LOCATION": location,
            "NOTES": notes,
            "TAGS": tags,
        }
            
    response_message = await command_handler.handler_update_item(sku, updates)
    await interaction.response.send_message(response_message)



async def graceful_exit(reason: str = "unknown"):
    global shutdown_started

    if shutdown_started:
        return

    shutdown_started = True
    print(f"Graceful exit requested: {reason}")

    shutdown_event.set()

    # Ask uvicorn to stop and give it a moment. Letting the loop tear it down
    # instead leaves its lifespan task to die unhandled, which prints a
    # CancelledError traceback on the way out.
    if health_server is not None:
        health_server.should_exit = True

        try:
            await asyncio.wait_for(health_task, timeout=3)
        except (TimeoutError, asyncio.CancelledError, Exception):
            pass

    try:
        await lipgloss.aclose()
    except Exception as e:
        print(f"Error closing lipgloss client: {e}")

    try:
        await claws.aclose()
    except Exception as e:
        print(f"Error closing claws client: {e}")

    try:
        await bot.close()
    except Exception as e:
        print(f"Error closing bot: {e}")

def install_signal_handlers():
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(
            sig,
            lambda s=sig: asyncio.create_task(graceful_exit(s.name)),
        )

CONFIG_PATH = os.environ.get("ILLUSION_BOT_CONFIG", "./bot.yaml")

try:
    config = illusion_config.load(CONFIG_PATH)

    required = [
        "bot.claws.url",
        "bot.claws.token",
        "bot.discord.token",
        "bot.discord.server_id",
        "bot.discord.fourm_id",
    ]

    if illusion_config.get(config, "bot.printer.enabled"):
        required += ["bot.lipgloss.url", "bot.lipgloss.token"]

    illusion_config.require(config, required, source=CONFIG_PATH)
except illusion_config.ConfigError as e:
    print(e)
    raise SystemExit(1)

PRINTING_ENABLED = bool(illusion_config.get(config, "bot.printer.enabled"))

TOKEN = illusion_config.get(config, "bot.discord.token")
GUILD_ID = illusion_config.get(config, "bot.discord.server_id")
FORUM_CHANNEL_ID = illusion_config.get(config, "bot.discord.fourm_id")

claws = ClawsClient(
    illusion_config.get(config, "bot.claws.url"),
    illusion_config.get(config, "bot.claws.token"),
)

lipgloss = LipglossClient(
    illusion_config.get(config, "bot.lipgloss.url"),
    illusion_config.get(config, "bot.lipgloss.token"),
)

command_handler = DB_Commands(claws, lipgloss, boot_time)

SERVICE_NAME = "illusion-bot"
HOSTNAME = socket.gethostname()

# Co-located with claws on the VM, so this binds loopback and never touches the
# network
HEALTH_HOST = illusion_config.get(config, "bot.health.host", "127.0.0.1")
HEALTH_PORT = illusion_config.get(config, "bot.health.port", 8090)


@bot.event
async def setup_hook():
    install_signal_handlers()

    global health_server, health_task

    if HEALTH_PORT:
        health_server = fleet.make_health_server(
            fleet.make_health_app(SERVICE_NAME, illusion_version, boot_time,
                                  {"host": HOSTNAME}),
            HEALTH_HOST,
            HEALTH_PORT,
        )
        health_task = bot.loop.create_task(health_server.serve())

    bot.loop.create_task(fleet.announce(claws, SERVICE_NAME, illusion_version, boot_time, HOSTNAME))
    bot.loop.create_task(claws_event_loop())
    bot.loop.create_task(reconcile_loop())

    if PRINTING_ENABLED:
        bot.loop.create_task(lipgloss_event_loop())


def main():
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
