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
        
        if command == "low" and len(parts) == 2 and len(parts[1]) >= 6:
            if len(parts[1]) <= 6:
                sku = "SKU-" + ("0" * 6 - len(parts[1])) + parts[1]
            else:
                sku = parts[1]

            if inventory.validate_sku(sku):
                item = inventory.get_item(sku)
                if item["LOW"] == True:
                    inventory.update_item(sku, {"LOW": "TRUE"})
                    
                    thread_with_message = await channel.create_thread(
                        name=f"{item["NAME"]}: {item["SKU"]}",
                        content=f"We are getting low on: {item["NAME"]}\nPriority: {item["PRIORITY"]}\nOrder Quantity: {item["ORDER_QUANTITY"]}\n\nLinks:\n{item["VENDOR_1"]}: {item["LINK_1"]}",
                    )

                    print(f"Created forum post: {thread_with_message.thread.name}")
                else:
                    print(f"{sku} already marked as low")
            else:
                print("Invalid sku")


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
    latency = round(bot.latency * 1000)
    await interaction.response.send_message(f"illusion\nversion: {illusion_version}")

@bot.tree.command(name="resolve", description="Mark low stock warnings as resolved")
async def ping(interaction: discord.Interaction):
    channel = interaction.channel

    print(channel.name)

    if not isinstance(channel, discord.Thread):
        await interaction.response.send_message(
            "This command must be used inside a low-stock thread.",
            ephemeral=True,
        )
        return

    sku = channel.name.split(": ")
    inventory.set_low(sku, False)
    inventory.save()

    await interaction.response.send_message(f"Resolved: {sku[1]}")

@bot.tree.command(name="add_item", description="Add item to inventory")
@app_commands.describe(item_name="Item Name",
                       priority="Item Priority",
                       quantity="Number of units to order when stock low",
                       vendor_1="Source 1 for Item",
                       link_1="Source 1 Purchase Link",
                       vendor_2="Source 2 for Item",
                       link_2="Source 2 Purchase Link",
                       vendor_3="Source 3 for Item",
                       link_3="Source 3 Purchase Link",
                       vendor_4="Source 4 for Item",
                       link_4="Source 4 Purchase Link",
                       vendor_5="Source 5 for Item",
                       link_5="Source 5 Purchase Link",
                       )

async def add_item(interaction: discord.Interaction, item_name: str, priority: str, quantity: str, 
                   vendor_1: str, link_1: str, vendor_2: str | None = None, link_2: str | None = None, 
                   vendor_3: str | None = None, link_3: str | None = None, vendor_4: str | None = None, 
                   link_4: str | None = None, vendor_5: str | None = None, link_5: str | None = None):
    new_item = {
            "NAME": item_name,
            "PRIORITY": priority,
            "ORDER_QUANTITY": quantity,
            "LINK_1": link_1,
            "VENDOR_1": vendor_1
        }
    
    new_sku = inventory.add_item(new_item)

    vendors = [vendor_1, vendor_2, vendor_3, vendor_4, vendor_5]
    links = [link_1, link_2, link_3, link_4, link_5]
    
    for i in range(len(vendors)):
        if vendors[i] != None and links[i] != None:
            inventory.add_vendor(new_sku, vendors[i], links[i])

    response_message = f"Added {item_name} to inventory, SKU: {new_sku}"
    inventory.save()
    print(f"{response_message}\n>")
    await interaction.response.send_message(response_message)

@bot.event
async def setup_hook():
    bot.loop.create_task(terminal_loop())

with open("./config.yaml", "r") as file:
    config = yaml.safe_load(file)

TOKEN = config["illusion"]["discord"]["token"]
GUILD_ID = config["illusion"]["discord"]["server_id"]
FORUM_CHANNEL_ID = config["illusion"]["discord"]["fourm_id"] 

inventory = SpreadsheetManager(config["illusion"]["spreadsheet_location"])
bot.run(TOKEN)