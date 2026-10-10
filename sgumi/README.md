# SGUMI - Super Graphic Ultra Modern Interface
The Super Graphic Ultra Modern Interface, a desktop frontend for [lipgloss](../packages/lipgloss).

## Building
Dependencies per platform are in [docs/Building.md](docs/Building.md). Clone with submodules first, then from the **repository root**:

```sh
cmake -S sgumi -B sgumi/build
cmake --build sgumi/build -j
```

The binary lands at `sgumi/build/sgumi` on Linux and Windows, and at`sgumi/build/sgumi.app` on macOS.

If `third_party/sdl` is empty, configure stops and tells you to run:

```sh
git submodule update --init --recursive
```

## Configuration
Unlike the Python services, SGUMI doesn't read a `.yaml` for it's config, and because it's a proper desktop app that gets launched from a menu, it stores it's config in the proper place for the OS it's running on:

| Platform | Path |
|---|---|
| macOS | `~/Library/Application Support/Eastern Edge/sgumi_config.json` |
| Windows | `%APPDATA%\Eastern Edge\sgumi_config.json` |
| else | `$XDG_CONFIG_HOME/eastern-edge/sgumi_config.json`, which is `~/.config/…` unless the session moved it |

`SGUMI_CONFIG_PATH` overrides all three, which is how you run two instances against two different lipgloss hosts.

Five settings, all editable under **SGUMI → Settings**:

```json
{
    "lipgloss_url": "http://127.0.0.1:8081",
    "lipgloss_token": "",
    "claws_url": "http://127.0.0.1:8080",
    "claws_token": "",
    "poll_seconds": 5
}
```

The tokens are separate secrets: `lipgloss_token` must match `lipgloss.yaml` on the printer host, `claws_token` must match `claws.yaml` on the inventory host.

`poll_seconds` is how often the queue is re-read, clamped to 1–60. It's a setting because where SGUMI runs decides what's reasonable: on the kiosk lipgloss is the same machine and a poll costs nothing, so 1 is fine there, while the default is plenty over the tailnet. It only governs how soon a job *someone else* queued turns up: printer faults and finished jobs arrive on `/events` as they happen, whatever this is set to.

Without claws every style still prints, you just type the text yourself.

The settings window opens automatically on first launch, when no lipgloss token is set.

## Reading the status line
`/health` is unauthenticated and `/queue` is not, which is deliberate over in lipgloss and is what lets the indicator tell three failures apart:

| What you see      | What it means |
|------------------ |---|
| 🔴 Unreachable    | Nothing answered. Wrong URL, wrong port, service down, or the tailnet is not up |
| 🟠 Token rejected | The service is running and healthy; the token does not match `lipgloss.yaml` |
| 🟠 Queue paused   | Connected and fine. The printer needs attention — the reason is under the queue heading |
| 🟢 Connected      | Working |

Without that split, a token typo and a dead service look identical, which on a kiosk means someone power-cycles a laptop that was never the problem.

The indicator only describes lipgloss. claws is on the About page instead, since it not being reachable doesn't stop anything printing

## Keyboard
| Key | Does                          |
|-----|-------------------------------|
| F10 | Toggles the live theme editor |

Configure with `-DSGUMI_THEME_EDITOR=OFF` to leave it out of a build entirely.

## Origin
This is based off components of the [Software_2027](https://github.com/EasternEdgeRobotics/Software_2027) ROV frontend, at commit `ff6a7f6`. The window, the frame loop, the config-path resolution, the theme format, and the packaging rules were taken from it, while everything related to the ROV was dropped.

That is a **fork point, not a subscription**. Nothing here tracks that repo automatically, and the two are expected to diverge. To see what has changed in the bones since, run this in a Software_2027 checkout:

```sh
git diff ff6a7f6..HEAD -- apps/frontend/src/main.cpp \
                          apps/frontend/CMakeLists.txt \
                          libs/eer_gfx/src/Theme.cpp
```

The four vendored checkouts under `third_party/` are pinned to the same commits Software_2027 uses, so a bug reproduced there reproduces here.

## What's next
In rough order:

1. **Range styles in the bot.** The wire now carries `style` and per-SKU text, so the bot could offer what SGUMI does.

[`clients.py`](../packages/illusion-core/src/illusion_core/clients.py) is the reference for every endpoint.
