"""illusion, the terminal kiosk.

Runs on the closet laptop next to the barcode scanner and the printer. It is a
pure client: it holds no database, drives no Discord connection, and does not
listen on a port except for the health endpoint the fleet asks about. When claws
is unreachable a command simply is not applied, and says so -- nothing is
buffered for a replay that could double count a scan later.
"""

import asyncio
import collections
import os
import signal
import socket
import time
from importlib.metadata import version

try:
    import readline
except ImportError:
    print("readline not installed, the prompt will have no history or line editing")

from illusion_core import config as illusion_config
from illusion_core import helpers as illusion_helpers
from illusion_core.clients import ClawsClient, LipglossClient, ServiceUnavailable
from illusion_core.commands import DB_Commands, DuplicateScan, Rows
from illusion_core import fleet

illusion_version = version("illusion-kiosk")

boot_time = time.time()

shutdown_event = asyncio.Event()
shutdown_started = False

health_server = None
health_task = None

# Mirrors lipgloss's own limit
MAX_COPIES = 100

# Had to include at least 1 other reference
joanne_hat = r"""
      ▆▅▄▃▃▃▃▃▃▄▅▆      
      ▆▆▆▆▆▆▆▆▆▆▆▆      
     ▕░░░░░░░░░░░░▏     
 ▆▅▄▄▄▆▆▆▆▆▆▆▆▆▆▆▆▄▄▄▅▆ 
 ▆▆▆▆▆▆▆▆▆▆▆▆▆▆▆▆▆▆▆▆▆▆ 
  ▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀ 
""".strip("\n")


def render(result):
    """Terminal rendering: strings pass through, Rows becomes a table."""
    if isinstance(result, Rows):
        return illusion_helpers.make_table(result.data, exclude=result.exclude)

    if isinstance(result, DuplicateScan):
        return f"{render(result.info)}\n{result.message}"

    return result


def queue_text(status):
    """Render lipgloss's queue status as a terminal table."""
    if not isinstance(status, dict):
        return status

    if not status["jobs"]:
        return f"{status['title']}\n{status['description']}"

    table = illusion_helpers.make_table(
        status["jobs"],
        exclude=["HEADER"],
        field_names=illusion_helpers.QUEUE_FIELD_NAMES,
        vertical=False,
    )

    return f"{status['title']}\n{status['description']}\n{table}"


async def command_help():
    command_list = [
        {
            "COMMAND": "about",
            "USAGE": "about",
            "DESCRIPTION": "Info about illusion",
        },
        {
            "COMMAND": "exit",
            "USAGE": "exit",
            "DESCRIPTION": "Exit illusion",
        },
        {
            "COMMAND": "clear",
            "USAGE": "clear",
            "DESCRIPTION": "Clear the terminal",
        },
        {
            "COMMAND": "resolve",
            "USAGE": "resolve <sku>",
            "DESCRIPTION": "Mark an item as not low",
        },
        {
            "COMMAND": "delete",
            "USAGE": "delete <sku>",
            "DESCRIPTION": "Delete an item",
        },
        {
            "COMMAND": "info",
            "USAGE": "info <sku>",
            "DESCRIPTION": "Get info about an item",
        },
        {
            "COMMAND": "search",
            "USAGE": "search <item name>",
            "DESCRIPTION": "Search for items",
        },
        {
            "COMMAND": "get_tags",
            "USAGE": "get_tags",
            "DESCRIPTION": "List all item tags",
        },
        {
            "COMMAND": "add_tag",
            "USAGE": "add_tag <sku> <tag>",
            "DESCRIPTION": "Add a tag to an item",
        },
        {
            "COMMAND": "get_locations",
            "USAGE": "get_locations",
            "DESCRIPTION": "List every location in use",
        },
        {
            "COMMAND": "where",
            "USAGE": "where <location>",
            "DESCRIPTION": "List the items in a location",
        },
        {
            "COMMAND": "set_location",
            "USAGE": "set_location <sku> [location]",
            "DESCRIPTION": "Set where an item lives, no location clears it",
        },
        {
            "COMMAND": "increase",
            "USAGE": "increase <sku> [amount]",
            "DESCRIPTION": "Increase item stock",
        },
        {
            "COMMAND": "decrease",
            "USAGE": "decrease <sku> [amount]",
            "DESCRIPTION": "Decrease item stock",
        },
        {
            "COMMAND": "set",
            "USAGE": "set <sku> <quantity>",
            "DESCRIPTION": "Set item stock",
        },
        {
            "COMMAND": "rescan",
            "USAGE": "rescan",
            "DESCRIPTION": "Count a DigiKey bag that was just refused as already scanned",
        },
    ]

    if PRINTING_ENABLED:
        command_list.extend(
            [
                {
                    "COMMAND": "print_barcode",
                    "USAGE": "print <sku>",
                    "DESCRIPTION": "Print a barcode with the printer",
                },
                {
                    "COMMAND": "printer_info",
                    "USAGE": "printer_info",
                    "DESCRIPTION": "Get info about the printer",
                },
                {
                    "COMMAND": "print_label",
                    "USAGE": 'print_label <line 1> ["line 2"]',
                    "DESCRIPTION": "Print a label with the specified text",
                },
                {
                    "COMMAND": "bulk_print",
                    "USAGE": 'bulk_print [lower sku] [upper sku]',
                    "DESCRIPTION": "Print barcodes for a range of skus",
                },
                {
                    "COMMAND": "print_queue",
                    "USAGE": "print_queue",
                    "DESCRIPTION": "Show the print queue",
                },
                {
                    "COMMAND": "print_resume",
                    "USAGE": "print_resume",
                    "DESCRIPTION": "Resume the print queue after fixing the printer",
                },
                {
                    "COMMAND": "print_cancel",
                    "USAGE": "print_cancel <job id>",
                    "DESCRIPTION": "Cancel a queued print job",
                },
                {
                    "COMMAND": "print_clear",
                    "USAGE": "print_clear",
                    "DESCRIPTION": "Clear every job from the print queue",
                },
            ]
        )

    return f"\n<sku> required argument\n[amount] optional argument\n\n{illusion_helpers.make_table(command_list)}\n"


TERMINAL_REPLY_TO = "kiosk"


def terminal_print(message):
    # The input prompt has no trailing newline, so anything printed from the
    # background lands on top of it, reprint it to keep the input line intact
    print(f"\n{message}\n> ", end="", flush=True)
    mark_activity()

async def terminal_notify(event):
    # The terminal gets the plain text; the title and embed are for discord
    terminal_print(event["message"])


# When the screen last changed, and whether anything is on it that an idle
# clear should wipe. Without the flag an untouched kiosk would clear its
# already empty screen every half hour for no reason.
last_activity = time.monotonic()
screen_dirty = True


def mark_activity():
    global last_activity, screen_dirty

    last_activity = time.monotonic()
    screen_dirty = True


def print_banner():
    print(f"illusion {illusion_version}")
    print("ready")


def clear_terminal():
    global screen_dirty

    # Home the cursor, clear the screen, then the scrollback too, otherwise a
    # week of scans is still one scroll away
    print("\033[H\033[2J\033[3J", end="")
    print_banner()
    screen_dirty = False


async def idle_clear_loop():
    """Clear the terminal once it has sat untouched for IDLE_CLEAR_SECONDS."""
    while not shutdown_event.is_set():
        await asyncio.sleep(IDLE_CHECK_SECONDS)

        if not screen_dirty or time.monotonic() - last_activity < IDLE_CLEAR_SECONDS:
            continue

        clear_terminal()

        # input() is still blocked in its thread, so reissue the prompt along
        # with anything typed but never entered, which readline still holds
        pending = readline.get_line_buffer() if "readline" in globals() else ""
        print(f"> {pending}", end="", flush=True)


async def terminal_loop():
    register_notifier(TERMINAL_REPLY_TO, terminal_notify)

    print_banner()

    # The barcode of a DigiKey bag refused as a repeat by the command just
    # before this one. Only ever the immediately previous command, so a stray
    # `rescan` typed later cannot count some long forgotten bag.
    duplicate_barcode = None

    while not shutdown_event.is_set():
        try:
            text = await asyncio.to_thread(input, "> ")
        except EOFError:
            await graceful_exit("terminal EOF")
            break
        except Exception as e:
            print(f"Terminal input error: {e}")
            await asyncio.sleep(1)
            continue

        text = text.strip()

        if not text:
            continue

        mark_activity()

        parts = text.split(maxsplit=2) # Make sure to update this if commands w/ 3+ fields are added
        command = parts[0].lower()
        response_message = None

        pending_rescan, duplicate_barcode = duplicate_barcode, None

        try:
            if command == "exit" and len(parts) >= 1:
                response_message = "Exiting"
                print(response_message)
                await graceful_exit("terminal exit")
                break

            elif command == "clear" and len(parts) == 1:
                clear_terminal()

            elif command == "help" and len(parts) >= 1:
                response_message = await command_help()

            elif command == "about" and len(parts) >= 1:
                bot_uptime, system_uptime = await command_handler.handler_uptime()
                text = f"""illusion \nversion: {illusion_version}\nkiosk uptime: {bot_uptime}\nsystem uptime: {system_uptime}""".strip("\n")
            
                hat_lines = joanne_hat.splitlines()
                text_lines = text.splitlines()

                hat_width = max(len(line) for line in hat_lines)
                gap = 4

                for i in range(len(hat_lines)):
                    if len(text_lines) > i:
                        if i != 2:
                            print(f"\033[38;2;192;140;149m{hat_lines[i].ljust(hat_width + gap)}\033[0m{text_lines[i]}")
                        else:
                            print(f"\033[38;2;230;222;208m{hat_lines[i].ljust(hat_width + gap)}\033[0m{text_lines[i]}")
                    else:
                        if i != 2:
                            print(f"\033[38;2;192;140;149m{hat_lines[i].ljust(hat_width + gap)}\033[0m")
                        else:
                            print(f"\033[38;2;230;222;208m{hat_lines[i].ljust(hat_width + gap)}\033[0m")

                response_message = await fleet_status()

            elif command == "get_tags" and len(parts) >= 1:
                response_message = render(await command_handler.handler_get_tags())
            elif command == "add_tag" and len(parts) == 3:
                response_message = await command_handler.handler_add_tag(parts[1], parts[2])
            elif command == "get_locations" and len(parts) >= 1:
                response_message = render(await command_handler.handler_get_locations())
            elif command == "where" and len(parts) >= 2:
                # A location is several words more often than not, so take the
                # rest of the line rather than only the next word
                location = " ".join(" ".join(parts[1:]).split())
                response_message = render(await command_handler.handler_search_location(location))
            elif command == "set_location" and len(parts) >= 2:
                location = parts[2] if len(parts) == 3 else None
                response_message = await command_handler.handler_set_location(parts[1], location)
            elif parts[0].startswith("EER-") and len(parts) >= 1: # Basic bar code scanner support
                response_message = await command_handler.handler_decrease(parts[0])
            elif text.startswith("[)>") or (text.isdigit() and len(text) > 8): # Digikey data matrix
                result = await command_handler.handler_digikey_scan(text.strip().replace("|", "\u241d"))

                if isinstance(result, DuplicateScan):
                    duplicate_barcode = result.barcode

                response_message = render(result)
            elif command == "rescan" and len(parts) == 1:
                if pending_rescan is None:
                    response_message = "Nothing to rescan. `rescan` only works right after a DigiKey bag is refused as already scanned."
                else:
                    response_message = render(await command_handler.handler_digikey_scan(pending_rescan, force=True))
            elif command == "resolve" and len(parts) >= 2:
                response_message = await command_handler.handler_resolve(parts[1])
            elif command == "delete" and len(parts) >= 2:
                response_message = await command_handler.handler_delete_item(parts[1])
            elif command == "info" and len(parts) >= 2:
                response_message = render(await command_handler.handler_info(parts[1]))
            elif command == "search" and len(parts) >= 2:
                # join back parts and remove whitespace
                query = " ".join(" ".join(parts[1:]).split())
                response_message = render(await command_handler.handler_search(query))
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
            elif command == "print_barcode" and PRINTING_ENABLED and len(parts) >= 2:
                if len(parts) != 3:
                    style = "slim_barcode"
                    sku = parts[1]
                    line_1 = None
                else:
                    style = "label_2_line"
                    sku = parts[1]
                    line_1 = parts[2]

                response_message = await command_handler.handler_print(style=style, text_line_1=line_1, sku=sku, reply_to=TERMINAL_REPLY_TO)
            elif command == "printer_info" and PRINTING_ENABLED and len(parts) >= 1:
                response_message = await command_handler.handler_printer_info()
            elif command == "print_label" and PRINTING_ENABLED and len(parts) >= 2:
                # Awful, Awful, Awful
                # I hate this code
                # Can't be replaced by shlex without breaking non qouted strings
                if len(parts) == 3:
                    cleaned_text = text.replace("print_label ", "")
                    if '"' in cleaned_text:
                        lines = cleaned_text.split('"')
                        if len(lines) >= 4:
                            line_1 = lines[1]
                            line_2 = lines[3] # Why did i flip this order before????????? -PC
                            style = "label_2_line"
                        else:
                            response_message = "Invalid Qoutes"
                    else:
                        line_1 = f"{parts[1]} {parts[2]}"
                        line_2 = None
                        style = "label_1_line"
                else:
                    line_1 = parts[1]
                    style = "label_1_line"
                    line_2 = None
            
                if response_message == None:
                    response_message = await command_handler.handler_print(style=style, text_line_1=line_1, text_line_2=line_2, reply_to=TERMINAL_REPLY_TO)
            elif command == "bulk_print" and PRINTING_ENABLED and len(parts) == 3:
                response_message = await command_handler.handler_bulk_print_niimbot(parts[1], parts[2], reply_to=TERMINAL_REPLY_TO)
            elif command == "print_queue" and PRINTING_ENABLED and len(parts) >= 1:
                response_message = queue_text(await command_handler.handler_print_queue())
            elif command == "print_resume" and PRINTING_ENABLED and len(parts) >= 1:
                response_message = await command_handler.handler_print_resume()
            elif command == "print_clear" and PRINTING_ENABLED and len(parts) >= 1:
                response_message = await command_handler.handler_print_clear()
            elif command == "print_cancel" and PRINTING_ENABLED and len(parts) >= 2:
                response_message = await command_handler.handler_print_cancel(parts[1])
            elif command == "set" and len(parts) == 3:
                response_message = await command_handler.handler_set_stock(parts[1], parts[2])
            else:
                response_message = f"Invalid Command: {command}\n\nHelp:{await command_help()}"
        except ServiceUnavailable as e:
            # Fail fast: the command is simply not applied, and says so
            response_message = f"Service unavailable, command not applied.\n{e}"
        except Exception as e:
            # One bad command must never take the terminal down with it.
            response_message = f"Command failed: {e}"

        if response_message != None:
            print(response_message)



# reply_to token -> handler. Bounded: a token is registered per print command
# and only jobs that finish cleanly remove theirs, so a printer left broken for
# a week must not grow this without limit.
notifiers = collections.OrderedDict()

MAX_NOTIFIERS = 100


def register_notifier(token, handler):
    notifiers[token] = handler
    notifiers.move_to_end(token)

    while len(notifiers) > MAX_NOTIFIERS:
        notifiers.popitem(last=False)


async def lipgloss_event_loop():
    """Route print updates back to the terminal.

    Reconnects on its own, because lipgloss restarting must not silently end
    print notifications for the rest of the session.

    Reconnect failures are deliberately not reported here. This loop retries
    every five seconds, and terminal_print reissues the prompt after anything it
    writes, so a chatty version makes the terminal unusable for as long as
    lipgloss is down. Nothing is hidden by staying quiet: every print command
    reports an unreachable lipgloss at the moment it is used, which is when it
    actually matters to whoever is standing there.
    """
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
                    terminal_print(f"Unable to deliver print update: {e}")

                if event["event"] == "job.done":
                    notifiers.pop(event["reply_to"], None)
        except Exception:
            if shutdown_event.is_set():
                return

        await asyncio.sleep(5)


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

    for client in (claws, lipgloss):
        if client is None:
            continue

        try:
            await client.aclose()
        except Exception as e:
            print(f"Error closing client: {e}")


def install_signal_handlers():
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(
            sig,
            lambda s=sig: asyncio.create_task(graceful_exit(s.name)),
        )


CONFIG_PATH = os.environ.get("ILLUSION_KIOSK_CONFIG", "./kiosk.yaml")

try:
    config = illusion_config.load(CONFIG_PATH)

    required = ["kiosk.claws.url", "kiosk.claws.token"]

    if illusion_config.get(config, "kiosk.printer.enabled"):
        required += ["kiosk.lipgloss.url", "kiosk.lipgloss.token"]

    illusion_config.require(config, required, source=CONFIG_PATH)

    illusion_config.add_defaults(config, CONFIG_PATH, {
        "kiosk.terminal.idle_clear_minutes": 30,
    })
except illusion_config.ConfigError as e:
    print(e)
    raise SystemExit(1)

PRINTING_ENABLED = bool(illusion_config.get(config, "kiosk.printer.enabled"))

claws = ClawsClient(
    illusion_config.get(config, "kiosk.claws.url"),
    illusion_config.get(config, "kiosk.claws.token"),
)

# Its settings are only required when printing is on, and every command that
# reaches it is gated on PRINTING_ENABLED, so there is nothing to build without
lipgloss = None

if PRINTING_ENABLED:
    lipgloss = LipglossClient(
        illusion_config.get(config, "kiosk.lipgloss.url"),
        illusion_config.get(config, "kiosk.lipgloss.token"),
    )

command_handler = DB_Commands(claws, lipgloss, boot_time)

SERVICE_NAME = "illusion-kiosk"
HOSTNAME = socket.gethostname()

# The kiosk is otherwise a pure client. It listens on exactly one read only
# route so claws can tell whether it is alive -- cheaper than plumbing a reply
# channel back down the event stream just to answer version questions.
HEALTH_HOST = illusion_config.get(config, "kiosk.health.host", "127.0.0.1")
HEALTH_PORT = illusion_config.get(config, "kiosk.health.port", 8082)

# 0 turns the idle clear off
IDLE_CLEAR_SECONDS = illusion_config.get(config, "kiosk.terminal.idle_clear_minutes", 30) * 60
IDLE_CHECK_SECONDS = 30


async def fleet_status():
    """The whole fleet, as a terminal table.

    Falls back to what this machine can see for itself when claws is
    unreachable, rather than printing nothing.
    """
    try:
        status = await claws.status()
    except ServiceUnavailable as e:
        local = fleet.health_payload(SERVICE_NAME, illusion_version, boot_time)

        return (
            f"Could not reach claws, showing only this machine.\n{e}\n\n"
            + illusion_helpers.make_table(
                fleet.fleet_rows({"services": [{"state": "ok", **local}]}),
                field_names=fleet.FLEET_FIELD_NAMES,
                vertical=False,
            )
        )

    table = illusion_helpers.make_table(
        fleet.fleet_rows(status), field_names=fleet.FLEET_FIELD_NAMES, vertical=False
    )

    skew = fleet.version_skew(status)

    return f"{table}\n{skew}" if skew else table


async def run():
    install_signal_handlers()

    if PRINTING_ENABLED:
        asyncio.create_task(lipgloss_event_loop())

    if IDLE_CLEAR_SECONDS:
        asyncio.create_task(idle_clear_loop())

    global health_server, health_task

    watched = []

    if HEALTH_PORT:
        health_server = fleet.make_health_server(
            fleet.make_health_app(SERVICE_NAME, illusion_version, boot_time,
                                  {"host": HOSTNAME}),
            HEALTH_HOST,
            HEALTH_PORT,
        )
        health_task = asyncio.create_task(health_server.serve())
        watched.append(health_task)

    await fleet.announce(claws, SERVICE_NAME, illusion_version, boot_time, HOSTNAME)

    terminal_task = asyncio.create_task(terminal_loop())
    watched.append(terminal_task)

    # Watch both rather than only the terminal. uvicorn treats a failed bind as
    # fatal, and the health endpoint binds a tailnet address that does not exist
    # until tailscaled is up, which a cold boot can lose the race to. Left as a
    # background task, that failure was silent: the kiosk carried on with no
    # health endpoint, so it never exited, so nothing restarted it, and the
    # fleet reported it unknown until someone noticed by hand.
    done, pending = await asyncio.wait(watched, return_when=asyncio.FIRST_COMPLETED)

    for task in pending:
        task.cancel()

    # graceful_exit stops the health server and waits for it
    if shutdown_event.is_set():
        return

    if terminal_task not in done:
        # The health server stopped on its own, which only happens when
        # something is wrong. Re-raise it so the process exits non-zero and the
        # launcher restarts us, which is what actually retries the bind.
        health_task.result()

        raise RuntimeError("health endpoint stopped unexpectedly")


def main():
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
