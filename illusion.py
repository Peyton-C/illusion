import discord
from discord import app_commands
from discord.ext import commands
from inventory_reader import SpreadsheetManager
import asyncio
import yaml
import tomllib
from pathlib import Path
import barcode_generator, illusion_helpers
from PIL import Image
import os, io

pyproject_path = Path(__file__).resolve().parents[0] / "./pyproject.toml"

with pyproject_path.open("rb") as f:
    pyproject = tomllib.load(f)

illusion_version = pyproject["project"]["version"]
intents = discord.Intents.default()

bot = commands.Bot(command_prefix="!", intents=intents)

# Had to include at least 1 other reference
joanne_hat = r"""
      ▆▅▄▃▃▃▃▃▃▄▅▆      
      ▆▆▆▆▆▆▆▆▆▆▆▆      
     ▕░░░░░░░░░░░░▏     
 ▆▅▄▄▄▆▆▆▆▆▆▆▆▆▆▆▆▄▄▄▅▆ 
 ▆▆▆▆▆▆▆▆▆▆▆▆▆▆▆▆▆▆▆▆▆▆ 
  ▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀ 
""".strip("\n")

class DB_Commands:
    async def handler_add_item(self, item_name, priority, order_quantity, tracking_mode="KANBAN", quantity_on_hand=None, 
                               low_threshold=None, unit=None, decrease_amount=None, vendor_1 = None, link_1 = None, 
                               vendor_2 = None, link_2 = None, vendor_3 = None, link_3 = None, 
                               vendor_4 = None, link_4 = None, vendor_5 = None, link_5 = None,): 
        global inventory

        new_item = {
            "NAME": item_name,
            "PRIORITY": priority,
            "ORDER_QUANTITY": order_quantity,
            "TRACKING_MODE": tracking_mode,
            "QUANTITY_ON_HAND": quantity_on_hand,
            "LOW_THRESHOLD": low_threshold,
            "LOW_THREAD_ID": None,
            "UNIT": unit,
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
            "LOW": "FALSE"
        }
        
        new_sku = inventory.add_item(new_item)

        inventory.save()

        response_message = f"Added {item_name} to inventory, SKU: {new_sku}"
        return response_message
    
    async def handler_delete_item(self, sku):
        global inventory
        sku = await clean_sku(sku)
        if inventory.validate_sku(sku):
            item = inventory.get_item(sku)
            inventory.delete_item(sku)
            inventory.save()
            response_message = f"Removed {item['NAME']} from inventory, SKU: {sku}"
        else:
            response_message = f"Invalid sku: {sku}"
        
        return response_message
    
    async def handler_info(self, sku):
        global inventory

        sku = await clean_sku(sku)
        if inventory.validate_sku(sku):
            item = inventory.get_item(sku)
            response_message = await make_table(item, ["PRIORITY", "TRACKING_MODE", "LOW_THRESHOLD", "UNIT", "LOW_THREAD_ID", "DECREASE_AMOUNT", 
                                                       "VENDOR_1", "LINK_1", "VENDOR_2", "LINK_2", "VENDOR_3", "LINK_3", "VENDOR_4", "LINK_4", "VENDOR_5", "LINK_5"])
        else:
            response_message = f"Invalid sku: {sku}"
        
        return response_message
    
    async def handler_resolve(self, sku, archive_thread=False):
        global inventory

        sku = await clean_sku(sku)
        if inventory.validate_sku(sku):
            item = inventory.get_item(sku)

            if item["LOW"] == True:
                inventory.update_item(sku, {"LOW": "FALSE"})
                inventory.save()
                response_message = f"{sku} no longer marked as low"
                if archive_thread:
                    thread_message = await self.archive_low_thread(sku)
                    response_message += f"\n{thread_message}"
            else:
                response_message = f"{sku} not marked as low"
        else:
            response_message = f"Invalid sku: {sku}"
        
        return response_message

    async def handler_low(self, sku):
        global inventory, channel

        sku = await clean_sku(sku)
        if inventory.validate_sku(sku):
            result = inventory.decrease_item(sku)
            item = result["item"]

            if result["low_changed"]:
                thread_with_message = await channel.create_thread(
                    name=f"{item['NAME']}: {item['SKU']}",
                    content=illusion_helpers.make_low_thread_content(item),
                    view=illusion_helpers.make_vendor_buttons(item),
                )
                thread_name = thread_with_message.thread.name

                inventory.update_item(sku, {"LOW_THREAD_ID": thread_with_message.thread.id,},)
                inventory.save()

            if item["TRACKING_MODE"] == "KANBAN":
                if result["low_changed"]:
                    return f"{sku} marked as low, thread: {thread_name} created"

                return f"{sku} already marked as low"
            
            unit = item["UNIT"] or "units"

            response_message = (
                f"{sku} decreased by "
                f"{illusion_helpers.format_quantity(result['decrease_amount'])} {unit}: "
                f"{illusion_helpers.format_quantity(result['old_quantity'])} -> "
                f"{illusion_helpers.format_quantity(result['new_quantity'])}"
            )

            if result["low_changed"]:
                response_message += f"\nLow threshold reached, thread: {thread_name} created"
            elif item["LOW"]:
                response_message += "\nItem is already marked as low."
        else:
            response_message = f"Invalid sku: {sku}"
        
        return response_message
        

    async def handler_search(self, name: str):
        global inventory

        results = inventory.search_items(name, limit=10)

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
            "PRIORITY", 
            "LOW_THREAD_ID",
            "TRACKING_MODE", 
            "LOW_THRESHOLD", 
            "UNIT", 
            "DECREASE_AMOUNT",
            "ORDER_QUANTITY",
        ]

        return await make_table(results, exclude=exclude)
    
    async def handler_decrease(self, sku, amount=None):
        global inventory
        global channel

        sku = await clean_sku(sku)

        if inventory.validate_sku(sku):
            result = inventory.decrease_item(sku, amount)
            item = result["item"]

            thread_name = None

            if result["low_changed"]:
                thread_with_message = await channel.create_thread(
                    name=f"{item['NAME']}: {item['SKU']}",
                    content=illusion_helpers.make_low_thread_content(item),
                    view=illusion_helpers.make_vendor_buttons(item),
                )
                thread_name = thread_with_message.thread.name

                inventory.update_item(sku, {"LOW_THREAD_ID": thread_with_message.thread.id,},)
                inventory.save()

            if item["TRACKING_MODE"] == "KANBAN":
                if result["low_changed"]:
                    return f"{sku} marked as low, thread: {thread_name} created"

                return f"{sku} already marked as low"

            unit = item["UNIT"] or "units"

            response_message = (
                f"{sku} decreased by "
                f"{illusion_helpers.format_quantity(result['decrease_amount'])} {unit}: "
                f"{illusion_helpers.format_quantity(result['old_quantity'])} -> "
                f"{illusion_helpers.format_quantity(result['new_quantity'])}"
            )

            if result["low_changed"]:
                response_message += f"\nLow threshold reached, thread: {thread_name} created"
            elif item["LOW"]:
                response_message += "\nItem is already marked as low."
        else:
            response_message = f"Invalid sku: {sku}"

        return response_message
    
    async def handler_increase(self, sku, amount=1):
        global inventory

        sku = await clean_sku(sku)

        if inventory.validate_sku(sku):
            item = inventory.increase_item(sku, float(amount))
            inventory.save()

            unit = item["UNIT"] or "units"

            response_message = (
                f"{sku} increased by {illusion_helpers.format_quantity(amount)} {unit}. "
                f"New stock: {illusion_helpers.format_quantity(item['QUANTITY_ON_HAND'])} {unit}. "
                f"Low: {item['LOW']}"
            )
        else:
            response_message = f"Invalid sku: {sku}"

        return response_message
    
    async def handler_set_stock(self, sku, quantity):
        global inventory

        sku = await clean_sku(sku)

        if inventory.validate_sku(sku):
            item = inventory.set_stock(sku, float(quantity))
            inventory.save()

            unit = item["UNIT"] or "units"

            response_message = (
                f"{sku} stock set to "
                f"{illusion_helpers.format_quantity(item['QUANTITY_ON_HAND'])} {unit}. "
                f"Low: {item['LOW']}"
            )
        else:
            response_message = f"Invalid sku: {sku}"

        return response_message
    
    async def archive_low_thread(self, sku):
        global inventory
        global bot

        item = inventory.get_item(sku)

        if item is None:
            return "No item found."

        thread_id = item.get("LOW_THREAD_ID")

        if not thread_id:
            return "No low-stock thread was stored for this item."

        try:
            thread = bot.get_channel(int(thread_id))

            if thread is None:
                thread = await bot.fetch_channel(int(thread_id))

        except discord.NotFound:
            inventory.update_item(sku, {"LOW_THREAD_ID": None})
            inventory.save()
            return "Stored thread no longer exists."

        if not isinstance(thread, discord.Thread):
            return "Stored channel is not a thread."

        await thread.edit(
            archived=True,
            reason=f"{sku} resolved",
        )

        inventory.update_item(sku, {"LOW_THREAD_ID": None})
        inventory.save()

        return "Low-stock thread archived."
    
    async def handler_generate_barcode(self, sku):
        return barcode_generator.generate_barcode(sku)
    
    async def handler_niimbot_barcode(self, sku):
        sku = await clean_sku(sku)
        serial_port = config["illusion"]["printer"]["niimbot"]["port"] 
        font_path = config["illusion"]["printer"]["niimbot"]["font_path"]
        font_size = config["illusion"]["printer"]["niimbot"]["font_size"]

        bc_path = barcode_generator.generate_barcode_niimbot(text=sku, font_path=font_path, font_size=font_size)

        result = illusion_helpers.niimbot_print(bc_path, serial_port)
        return result

    async def handler_command_help(self):
        command_list = {
            "about": "Info about illusion",
            "exit": "Exit illusion",
            "low": "Mark an item as low",
            "resolve": "Mark an item as being not low",
            "delete": "Delete an item",
            "info": "Get info about an item",
            "search": "Search for items",
            "increase": "Increase item stock",
            "decrease": "Decrease item stock",
            "set": "Set item stock",
            "print": "Print a barcode with the Niimbot",
        }

        return f"\n{await make_table(command_list)}\n"

async def clean_sku(sku):
    if len(sku) <= 6:
        sku = "EER-" + ("0" * (6 - len(sku))) + sku
    return sku

async def make_table(data, exclude=None):
    missing = "N/A"

    if exclude is None:
        exclude = [""]

    if isinstance(data, dict):
        rows = [data]
    else:
        rows = data

    if not rows:
        return ""

    # Vertical Table
    if len(rows) == 1:
        row = rows[0]

        field_header = "Field"
        value_header = "Value"

        table_data = []

        for field in row:
            if field not in exclude:
                value = row.get(field, missing)
                table_data.append((str(field), str(value)))

        if not table_data:
            return ""

        field_width = max(
            len(field_header),
            *(len(field) for field, _ in table_data),
        )

        value_width = max(
            len(value_header),
            *(len(value) for _, value in table_data),
        )

        header = (
            f"| {field_header.ljust(field_width)} "
            f"| {value_header.ljust(value_width)} |"
        )

        separator = f"| {'-' * field_width} | {'-' * value_width} |"

        body = []
        for field, value in table_data:
            body.append(
                f"| {field.ljust(field_width)} | {value.ljust(value_width)} |"
            )

        return "\n".join([header, separator] + body)

    # Horizontal Table
    columns = []

    for row in rows:
        for key in row:
            if key not in columns and key not in exclude:
                columns.append(key)

    if not columns:
        return ""

    string_rows = []

    for row in rows:
        string_row = {}

        for column in columns:
            string_row[column] = str(row.get(column, missing))

        string_rows.append(string_row)

    column_widths = {}

    for column in columns:
        max_cell_width = max(len(row[column]) for row in string_rows)
        column_widths[column] = max(len(column), max_cell_width)

    header_cells = []

    for column in columns:
        header_cells.append(column.ljust(column_widths[column]))

    header = "| " + " | ".join(header_cells) + " |"

    separator_cells = []

    for column in columns:
        separator_cells.append("-" * column_widths[column])

    separator = "| " + " | ".join(separator_cells) + " |"

    table_rows = []

    for row in string_rows:
        cells = []

        for column in columns:
            cells.append(row[column].ljust(column_widths[column]))

        table_rows.append("| " + " | ".join(cells) + " |")

    return "\n".join([header, separator] + table_rows)

async def terminal_loop():
    await bot.wait_until_ready()

    print("ready")

    while not bot.is_closed():
        text = await asyncio.to_thread(input, "> ")
        text = text.strip()

        if not text:
            continue

        parts = text.split(maxsplit=2) # Make sure to update this is commands w/ 3+ fields are added
        command = parts[0].lower()
        response_message = None

        if command == "exit" and len(parts) >= 1:
            response_message = "Exiting"
            inventory.save()
            await bot.close()

        elif command == "help" and len(parts) >= 1:
            response_message = await command_handler.handler_command_help()

        elif command == "about" and len(parts) >= 1:
            text = f"""illusion \nversion: {illusion_version}""".strip("\n")
            
            hat_lines = joanne_hat.splitlines()
            text_lines = text.splitlines()

            hat_width = max(len(line) for line in hat_lines)
            gap = 4

            for i in range(len(hat_lines)):
                if len(text_lines) > i:
                    print(f"{hat_lines[i].ljust(hat_width + gap)}{text_lines[i]}")
                else:
                    print(f"{hat_lines[i].ljust(hat_width + gap)}")
            
            response_message = ""
            
        elif parts[0].startswith("EER-") and len(parts) >= 1: # Basic bar code scanner support
            response_message = await command_handler.handler_decrease(parts[0])
        elif command == "low" and len(parts) >= 2:
            response_message = await command_handler.handler_low(parts[1])
        elif command == "resolve" and len(parts) >= 2:
            response_message = await command_handler.handler_resolve(parts[1])
        elif command == "delete" and len(parts) >= 2:
            response_message = await command_handler.handler_delete_item(parts[1])
        elif command == "info" and len(parts) >= 2:
            response_message = await command_handler.handler_info(parts[1])
        elif command == "search" and len(parts) >= 2:
            response_message = await command_handler.handler_search(parts[1])
        elif command == "decrease" and len(parts) >= 2:
            if len(parts) == 3:
                response_message = await command_handler.handler_decrease(parts[1], parts[2])
            else:
                response_message = await command_handler.handler_decrease(parts[1])
        elif command == "increase" and len(parts) >= 2:
            if len(parts) == 3:
                response_message = await command_handler.handler_increase(parts[1], parts[2])
            else:
                response_message = await command_handler.handler_increase(parts[1])
        elif command == "print" and config["illusion"]["printer"]["niimbot"]["enabled"] and len(parts) >= 2:
            response_message = await command_handler.handler_niimbot_barcode(parts[1])
        elif command == "set" and len(parts) == 3:
            response_message = await command_handler.handler_set_stock(parts[1], parts[2])
        else:
            response_message = f"Invalid Command: {command}\n\nHelp:{await command_handler.handler_command_help()}"

        if response_message != None:
            print(response_message)


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



@bot.tree.command(name="ping", description="Check bot latency")
async def ping(interaction: discord.Interaction):
    latency = round(bot.latency * 1000)
    await interaction.response.send_message(f"Pong! `{latency}ms`")

@bot.tree.command(name="about", description="About illusion")
async def about(interaction: discord.Interaction):
    await interaction.response.send_message(f"illusion\nversion: {illusion_version}")

@bot.tree.command(name="resolve", description="Mark low stock warnings as resolved")
@app_commands.describe(sku="Item Sku")
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

    cleaned_sku = await clean_sku(sku)
    await command_handler.archive_low_thread(cleaned_sku)

@bot.tree.command(name="low", description="Mark stock as being low")
@app_commands.describe(sku="Item Sku")
async def low(interaction: discord.Interaction, sku: str):
    response_message = await command_handler.handler_low(sku)
    await interaction.response.send_message(response_message)

@bot.tree.command(name="set_stock", description="Set current stock")
@app_commands.describe(sku="Item Sku", value="Stock amount")
async def set_stock(interaction: discord.Interaction, sku: str, value: str):
    response_message = await command_handler.handler_set_stock(sku, value)
    await interaction.response.send_message(response_message)

    cleaned_sku = await clean_sku(sku)
    item = inventory.get_item(cleaned_sku)
    if item["LOW"] == False and item["LOW_THREAD_ID"] != None:
        await command_handler.archive_low_thread(cleaned_sku)

@bot.tree.command(name="decrease", description="Decrease current stock")
@app_commands.describe(sku="Item Sku", amount="Amount to decrease by")
async def decrease(interaction: discord.Interaction, sku: str, amount: str | None = "1"):
    response_message = await command_handler.handler_decrease(sku, amount)
    await interaction.response.send_message(response_message)

@bot.tree.command(name="increase", description="Increase current stock")
@app_commands.describe(sku="Item Sku", amount="Amount to increase by")
async def increase(interaction: discord.Interaction, sku: str, amount: str | None = "1"):
    response_message = await command_handler.handler_increase(sku, amount)
    await interaction.response.send_message(response_message)

    cleaned_sku = await clean_sku(sku)
    item = inventory.get_item(cleaned_sku)
    if item["LOW"] == False and item["LOW_THREAD_ID"] != None:
        await command_handler.archive_low_thread(sku)

@bot.tree.command(name="info", description="Get info about an item")
@app_commands.describe(sku="Item Sku")
async def info(interaction: discord.Interaction, sku: str):
    response_message = await command_handler.handler_info(sku)

    if response_message.startswith("Invalid sku"):
        await interaction.response.send_message(response_message)
    else:
        cleaned_sku = await clean_sku(sku)
        item = inventory.get_item(cleaned_sku)

        await interaction.response.send_message(f"```{response_message}```", view=illusion_helpers.make_vendor_buttons(item),)

@bot.tree.command(name="delete", description="Delete an item")
@app_commands.describe(sku="Item Sku")
async def delete(interaction: discord.Interaction, sku: str):
    response_message = await command_handler.handler_delete_item(sku)
    await interaction.response.send_message(response_message)

@bot.tree.command(name="add_item", description="Add item to inventory w/ per unit tracking")
@app_commands.describe(item_name="Item Name",
                       priority="Item Priority",
                       order_quantity="Number of units to order when stock low", unit="Unit name",
                       quantity="Number of units on hand", low_threshold="Minimum Stock",
                       vendor_1="Source 1 for Item", link_1="Source 1 Purchase Link",
                       vendor_2="Source 2 for Item", link_2="Source 2 Purchase Link",
                       vendor_3="Source 3 for Item", link_3="Source 3 Purchase Link",
                       vendor_4="Source 4 for Item", link_4="Source 4 Purchase Link",
                       vendor_5="Source 5 for Item", link_5="Source 5 Purchase Link",
                       )

async def add_item(interaction: discord.Interaction, item_name: str, priority: str, 
                   quantity: str, order_quantity: str, low_threshold: str, unit: str,
                   vendor_1: str, link_1: str, vendor_2: str | None = None, link_2: str | None = None, 
                   vendor_3: str | None = None, link_3: str | None = None, vendor_4: str | None = None, 
                   link_4: str | None = None, vendor_5: str | None = None, link_5: str | None = None):

    response_message = await command_handler.handler_add_item(item_name, priority, order_quantity, "Quantity", quantity, low_threshold, unit, "1", vendor_1, link_1, 
                                                              vendor_2, link_2, vendor_3, link_3, vendor_4, link_4, vendor_5, link_5,)

    await interaction.response.send_message(response_message)

@bot.tree.command(name="add_kanban", description="Add item to inventory w/ kanban tracking")
@app_commands.describe(item_name="Item Name",
                       priority="Item Priority",
                       order_quantity="Number of units to order when stock low",
                       vendor_1="Source 1 for Item", link_1="Source 1 Purchase Link",
                       vendor_2="Source 2 for Item", link_2="Source 2 Purchase Link",
                       vendor_3="Source 3 for Item", link_3="Source 3 Purchase Link",
                       vendor_4="Source 4 for Item", link_4="Source 4 Purchase Link",
                       vendor_5="Source 5 for Item", link_5="Source 5 Purchase Link",
                       )

async def add_kanban(interaction: discord.Interaction, item_name: str, priority: str, order_quantity: str,
                   vendor_1: str, link_1: str, vendor_2: str | None = None, link_2: str | None = None, 
                   vendor_3: str | None = None, link_3: str | None = None, vendor_4: str | None = None, 
                   link_4: str | None = None, vendor_5: str | None = None, link_5: str | None = None):

    response_message = await command_handler.handler_add_item(item_name, priority, order_quantity, "Kanban", None, None, None, None, vendor_1, link_1, 
                                                              vendor_2, link_2, vendor_3, link_3, vendor_4, link_4, vendor_5, link_5,)

    await interaction.response.send_message(response_message)

@bot.tree.command(name="add_hybrid", description="Add item to inventory w/ hybrid tracking")
@app_commands.describe(item_name="Item Name",
                       priority="Item Priority",
                       order_quantity="Number of units to order when stock low", unit="Unit name",
                       quantity="Number of units on hand", low_threshold="Minimum Stock", decrease_amount="Amount to decrease by",
                       vendor_1="Source 1 for Item", link_1="Source 1 Purchase Link",
                       vendor_2="Source 2 for Item", link_2="Source 2 Purchase Link",
                       vendor_3="Source 3 for Item", link_3="Source 3 Purchase Link",
                       vendor_4="Source 4 for Item", link_4="Source 4 Purchase Link",
                       vendor_5="Source 5 for Item", link_5="Source 5 Purchase Link",
                       )

async def add_hybrid(interaction: discord.Interaction, item_name: str, priority: str, 
                   quantity: str, order_quantity: str, low_threshold: str, unit: str, decrease_amount: str,
                   vendor_1: str, link_1: str, vendor_2: str | None = None, link_2: str | None = None, 
                   vendor_3: str | None = None, link_3: str | None = None, vendor_4: str | None = None, 
                   link_4: str | None = None, vendor_5: str | None = None, link_5: str | None = None):

    response_message = await command_handler.handler_add_item(item_name, priority, order_quantity, "Hybrid", 
                                                              quantity, low_threshold, unit, decrease_amount, vendor_1, link_1, 
                                                              vendor_2, link_2, vendor_3, link_3, vendor_4, link_4, vendor_5, link_5,)

    await interaction.response.send_message(response_message)

# Something about search makes discord hate it, no clue why -PC
@bot.tree.command(name="search", description="Search inventory by item name")
@app_commands.describe(name="Item name")
async def search(interaction: discord.Interaction, name: str):
    await interaction.response.defer()
    response_message = await command_handler.handler_search(name)

    if response_message.startswith("No items found"):
        await interaction.followup.send(response_message)
    else:
        await interaction.followup.send(f"```{response_message}```")

@bot.tree.command(name="generate_barcode", description="Generate a barcode")
@app_commands.describe(sku="Item Sku")
async def generate_barcode(interaction: discord.Interaction, sku: str):
    sku = await clean_sku(sku)
    if inventory.validate_sku(sku):
        file_path = await command_handler.handler_generate_barcode(sku)
        file = discord.File(file_path)

        await interaction.response.send_message(f"Barcode", file=file)
    else:
        await interaction.response.send_message(f"Invalid sku {sku}")

@bot.tree.command(name="print_barcode", description="Print a barcode w/ the Niimbot")
@app_commands.describe(sku="Item Sku")
async def print_barcode(interaction: discord.Interaction, sku: str):
    await interaction.response.defer()
    sku = await clean_sku(sku)
    
    response_message = await command_handler.handler_niimbot_barcode(sku)
    await interaction.followup.send(response_message)

@bot.tree.command(name="print_image", description="Print an image w/ the Niimbot")
@app_commands.describe(image="Image to print", rotate="Degrees to rotate by")
async def print_barcode(interaction: discord.Interaction, image: discord.Attachment, rotate: int = 0):
    if image.content_type is None or not image.content_type.startswith("image/"):
        await interaction.response.send_message("Please upload a valid image.", ephemeral=True)
        return
    
    await interaction.response.defer()

    image_bytes = await image.read()
    with Image.open(io.BytesIO(image_bytes)) as img:
        rotated = img.rotate(rotate, expand=True)
        resized = rotated.resize((92, 320))

        os.makedirs("/tmp/illusion/imgs/", exist_ok=True)

        output_path = os.path.join(
            "/tmp/illusion/imgs/",
            f"resized_{image.filename}",
        )

        resized.save(output_path)

    serial_port = config["illusion"]["printer"]["niimbot"]["port"]
    response_message = illusion_helpers.niimbot_print(output_path, serial_port)
    await interaction.followup.send(response_message)

@bot.event
async def setup_hook():
    bot.loop.create_task(terminal_loop())

with open("./config.yaml", "r") as file:
    config = yaml.safe_load(file)

TOKEN = config["illusion"]["discord"]["token"]
GUILD_ID = config["illusion"]["discord"]["server_id"]
FORUM_CHANNEL_ID = config["illusion"]["discord"]["fourm_id"] 
command_handler = DB_Commands()
inventory = SpreadsheetManager(config["illusion"]["database_location"])
bot.run(TOKEN)