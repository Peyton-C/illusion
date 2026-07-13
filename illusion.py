import discord
from discord import app_commands
from discord.ext import commands
from inventory_reader import SpreadsheetManager
import asyncio
import yaml
import tomllib
from pathlib import Path

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
    async def handler_add_item(self, item_name, priority, quantity, vendor_1, link_1, 
                              vendor_2 = None, link_2 = None, vendor_3 = None, link_3 = None, 
                              vendor_4 = None, link_4 = None, vendor_5 = None, link_5 = None,):
        global inventory

        new_item = {
            "NAME": item_name,
            "PRIORITY": priority,
            "ORDER_QUANTITY": quantity,
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
            response_message = f"Removed {item["NAME"]} from inventory, SKU: {sku}"
        else:
            response_message = f"Invalid sku: {sku}"
        
        return response_message
    
    async def handler_info(self, sku):
        global inventory

        sku = await clean_sku(sku)
        if inventory.validate_sku(sku):
            item = inventory.get_item(sku)
            response_message = await make_table(item)
        else:
            response_message = f"Invalid sku: {sku}"
        
        return response_message
    
    async def handler_resolve(self, sku):
        global inventory

        sku = await clean_sku(sku)
        if inventory.validate_sku(sku):
            item = inventory.get_item(sku)

            if item["LOW"] == True:
                inventory.update_item(sku, {"LOW": "FALSE"})
                inventory.save()
                response_message = f"{sku} nolonger marked as low"
            else:
                response_message = f"{sku} not marked as low"
        else:
            response_message = f"Invalid sku: {sku}"
        
        return response_message

    async def handler_low(self, sku):
        global inventory

        sku = await clean_sku(sku)
        if inventory.validate_sku(sku):
            item = inventory.get_item(sku)
            if item["LOW"] == False:
                inventory.update_item(sku, {"LOW": "TRUE"})
                inventory.save()

                thread_with_message = await channel.create_thread(
                    name=f"{item["NAME"]}: {item["SKU"]}",
                    content=f"We are getting low on: {item["NAME"]}\nPriority: {item["PRIORITY"]}\nOrder Quantity: {item["ORDER_QUANTITY"]}\n\nLinks:\n{item["VENDOR_1"]}: {item["LINK_1"]}",
                )

                response_message = f"{sku} marked as low, therad: {thread_with_message.thread.name} created"
            else:
                response_message = f"{sku} already marked as low"
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
        ]

        return await make_table(results, exclude=exclude)

async def clean_sku(sku):
    if len(sku) <= 6:
        sku = "SKU-" + ("0" * (6 - len(sku))) + sku
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

        parts = text.split(maxsplit=1)
        command = parts[0].lower()
        
        if command == "exit":
            inventory.save()
            await bot.close()

        if command == "about":
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
        
        if len(parts) == 2 and len(parts[1]) >= 1:
                if command == "low":
                    response_message = await command_handler.handler_low(parts[1])
                elif command == "resolve":
                    response_message = await command_handler.handler_resolve(parts[1])
                elif command == "delete":
                    response_message = await command_handler.handler_delete_item(parts[1])
                elif command == "info":
                    response_message = await command_handler.handler_info(parts[1])
                elif command == "search":
                    response_message = await command_handler.handler_search(parts[1])
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
    elif isinstance(channel, discord.Thread):
        sku = channel.name.split(": ")[1]
    
    response_message = await command_handler.handler_resolve(sku)
    await interaction.response.send_message(response_message)

@bot.tree.command(name="low", description="Mark stock as being low")
@app_commands.describe(sku="Item Sku")
async def low(interaction: discord.Interaction, sku: str):
    response_message = await command_handler.handler_low(sku)
    await interaction.response.send_message(response_message)

@bot.tree.command(name="info", description="Get info about an item")
@app_commands.describe(sku="Item Sku")
async def info(interaction: discord.Interaction, sku: str):
    response_message = await command_handler.handler_info(sku)

    if response_message.startswith("Invalid sku"):
        await interaction.response.send_message(response_message)
    else:
        await interaction.response.send_message(f"```{response_message}```")

@bot.tree.command(name="delete", description="Delete an item")
@app_commands.describe(sku="Item Sku")
async def delete(interaction: discord.Interaction, sku: str):
    response_message = await command_handler.handler_delete_item(sku)
    await interaction.response.send_message(response_message)

@bot.tree.command(name="add_item", description="Add item to inventory")
@app_commands.describe(item_name="Item Name",
                       priority="Item Priority",
                       quantity="Number of units to order when stock low",
                       vendor_1="Source 1 for Item", link_1="Source 1 Purchase Link",
                       vendor_2="Source 2 for Item", link_2="Source 2 Purchase Link",
                       vendor_3="Source 3 for Item", link_3="Source 3 Purchase Link",
                       vendor_4="Source 4 for Item", link_4="Source 4 Purchase Link",
                       vendor_5="Source 5 for Item", link_5="Source 5 Purchase Link",
                       )

async def add_item(interaction: discord.Interaction, item_name: str, priority: str, quantity: str, 
                   vendor_1: str, link_1: str, vendor_2: str | None = None, link_2: str | None = None, 
                   vendor_3: str | None = None, link_3: str | None = None, vendor_4: str | None = None, 
                   link_4: str | None = None, vendor_5: str | None = None, link_5: str | None = None):

    response_message = await command_handler.handler_add_item(item_name, priority, quantity, vendor_1, link_1, 
                              vendor_2, link_2, vendor_3, link_3, vendor_4, link_4, vendor_5, link_5,)

    await interaction.response.send_message(response_message)

@bot.tree.command(name="search", description="Search inventory by item name")
@app_commands.describe(name="Item name")
async def search(interaction: discord.Interaction, name: str):
    response_message = await command_handler.handler_search(name)

    if response_message.startswith("No items found"):
        await interaction.response.send_message(response_message)
    else:
        await interaction.response.send_message(f"```{response_message}```")

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