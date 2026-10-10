"""Config loading shared by every illusion service.

Each service declares the fields it actually needs. Missing ones are reported
together, by name, at startup -- rather than one per run, or as a confusing
failure at first use (an empty Discord token surfaces as a login error, an
empty font path as a PIL traceback halfway through a print job).
"""

from pathlib import Path

import yaml


class ConfigError(Exception):
    """The config file is missing, unreadable, or missing required values."""


_MISSING = object()


def get(config, dotted, default=None):
    """Read a dotted path like 'illusion.discord.token', or default."""
    node = config

    for key in dotted.split("."):
        if not isinstance(node, dict) or key not in node:
            return default

        node = node[key]

    return node


def _is_empty(config, dotted):
    value = get(config, dotted, _MISSING)

    return value is _MISSING or value is None or value == ""


def require(config, dotted_paths, source="config"):
    """Raise naming every required field that is absent or blank."""
    missing = [dotted for dotted in dotted_paths if _is_empty(config, dotted)]

    if missing:
        raise ConfigError(
            f"{source} is missing required values:\n"
            + "\n".join(f"  {dotted}" for dotted in missing)
        )


def add_defaults(config, path, defaults):
    """Write any option in defaults the config file lacks back to it.

    defaults maps dotted paths to values. Lets an option added in a later
    version show up in configs that predate it, so it can be found and tuned
    without digging through the example file. Rewriting the file drops any
    comments in it. The in-memory config is updated either way, and a file
    that cannot be written only warns: a new option is never worth refusing
    to boot over.
    """
    added = {dotted: value for dotted, value in defaults.items()
             if get(config, dotted, _MISSING) is _MISSING}

    if not added:
        return

    for dotted, value in added.items():
        _set(config, dotted, value)

    # Written beside it and swapped in, so dying mid write cannot leave a
    # truncated config that stops the next boot
    path = Path(path)
    temp = path.with_name(f".{path.name}.tmp")

    try:
        with temp.open("w") as file:
            yaml.safe_dump(config, file, sort_keys=False)

        # It holds tokens, keep whatever permissions it was locked down to
        temp.chmod(path.stat().st_mode)
        temp.replace(path)
    except OSError as e:
        temp.unlink(missing_ok=True)
        print(f"Could not add new options to {path}, using defaults: {e}")
        return

    for dotted, value in added.items():
        print(f"Added {dotted}: {value} to {path}")


def _set(config, dotted, value):
    *parents, leaf = dotted.split(".")
    node = config

    for key in parents:
        if not isinstance(node.get(key), dict):
            node[key] = {}

        node = node[key]

    node[leaf] = value


def load(path, required=()):
    path = Path(path)

    if not path.is_file():
        raise ConfigError(
            f"{path} not found. Copy config.example.yaml to {path.name} and fill it in."
        )

    try:
        with path.open("r") as file:
            config = yaml.safe_load(file) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{path} is not valid YAML: {e}") from e

    require(config, required, source=str(path))

    return config
