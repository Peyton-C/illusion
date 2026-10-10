"""The closets's locations.

A location is one value on an item rather than a list of tags, so this is the
list of the ones that exist, the nicknames people say out loud instead of them,
and the sub-groups inside a shelf. Predefining them is what stops "Shelf 4B",
"shelf 4b" and "4B" becoming three different shelves in `get_locations`, and it
is what lets the kiosk and the bot offer a shelf before anything is on it.

Sub-groups live in the location string itself, after a colon, so an item still
carries exactly one location and every existing query keeps working:

    Shelf 4B                     the shelf, nothing more specific said
    Shelf 4B: Soldering Equipment   the bin on it

Asking for the shelf includes its sub-groups; asking for the sub-group does not
include the rest of the shelf.
"""

from __future__ import annotations

import re
from typing import NamedTuple

SEPARATOR = ": "

_COMPACT_RE = re.compile(r"[^a-z0-9]+")


class Location(NamedTuple):
    name: str
    aliases: tuple[str, ...] = ()
    groups: tuple[str, ...] = ()


# Order matters: this is the order things are offered and listed in.
CATALOGUE = (
    Location(
        "Shelf 4A",
        ("4A",),
        ("Old Monitors", "Admin/Marketing"),
    ),
    Location(
        "Shelf 4B",
        ("4B",),
        ("Controller, Mice, and Keyboard Bin", "Low-level Components", "Soldering Equipment"),
    ),
    Location(
        "Shelf 4C",
        ("4C",),
        ("Thrusters and Vokey Stuff", "O Rings", "Power Conversion", "High-level Components"),
    ),
    Location(
        "Shelf 4D",
        ("4D",),
        ("Acrylic / Polycarbonate Scrap", "PVC Bucket", "Wires", "Power Cords",
         "Ethernet, Misc Cables", "Rope"),
    ),
    # Shelves with <2 sub-groups should always just use their one group's name as part of their title
    Location(
        "Shelf 4E (Underneath)",
        ("4E", "Underneath Shelf 4", "Under Shelf 4"),
    ),
    Location(
        "Shelf 5A (Archive)",
        ("5A", "Archive"),
    ),
    Location(
        "Shelf 5B (Projects)",
        ("5B",),
    ),
    Location(
        "Shelf 5C (Projects)",
        ("5C",),
    ),
    Location(
        "Shelf 5D",
        ("5D",),
        ("Metal", "HDPE Scrap", "Enclosures"),
    ),
    Location(
        "Shelf 5E (Underneath)",
        ("5E", "Underneath Shelf 5", "Under Shelf 5"),
    ),
    Location("Tool Chest 2", ("Mastercraft 2", "Tool Cabinet 2")),
    Location("Tool Chest 3", ("Mastercraft 3", "Tool Cabinet 3")),
    Location(
        "Hazardous Materials Cabinet 1",
        ("Hazmat Cabinet 1", "Hazardous Material Cabinet"),
    ),
    Location("Wall Storage", ("Screw Wall", "Pink Floyd")),
    Location("Murphy Desk", ("Printer Desk",)),
)


def full_name(top: str, group: str) -> str:
    return f"{top}{SEPARATOR}{group}"


def clean(value) -> str:
    """Collapse the whitespace and the spacing around the separator.

    "shelf 4b:soldering equipment" and "Shelf 4B : Soldering  Equipment" are
    both someone naming the same bin at a terminal.
    """
    text = " ".join(str(value or "").split())

    return re.sub(r"\s*:\s*", SEPARATOR, text).strip()


def _compact(value) -> str:
    """Casefolded with everything that is not a letter or digit taken out.

    The forgiving half of the lookup: it is what makes "shelf4b", "Shelf 4B"
    and "shelf 4b soldering equipment" land on the right shelf without any of
    them having to be spelled out as an alias.
    """
    return _COMPACT_RE.sub("", str(value or "").casefold())


# Every full location, in catalogue order: the top level ones and their
# sub-groups, which is what gets offered for completion
KNOWN_LOCATIONS: tuple[str, ...] = tuple(
    name
    for location in CATALOGUE
    for name in (location.name, *(full_name(location.name, group) for group in location.groups))
)

# canonical -> the names that lead to it, canonical first, for display
NAMES_FOR: dict[str, tuple[str, ...]] = {}

_LOOKUP: dict[str, str] = {}
_COMPACT_LOOKUP: dict[str, str] = {}


def _register(text: str, canonical: str) -> None:
    # setdefault, so the earlier and more canonical spelling always wins a tie
    _LOOKUP.setdefault(clean(text).casefold(), canonical)
    _COMPACT_LOOKUP.setdefault(_compact(text), canonical)


def _build() -> None:
    # A sub-group name on its own only means something when exactly one shelf
    # has it. Anything on two shelves cannot stand alone, and typing it leaves
    # the location as free text rather than guessing a shelf on someone's
    # behalf.
    group_owners: dict[str, list[str]] = {}

    for location in CATALOGUE:
        for group in location.groups:
            group_owners.setdefault(group.casefold(), []).append(
                full_name(location.name, group)
            )

    for location in CATALOGUE:
        names = [location.name, *location.aliases]

        for name in names:
            _register(name, location.name)

            for group in location.groups:
                _register(full_name(name, group), full_name(location.name, group))

        NAMES_FOR[location.name] = tuple(names)

        for group in location.groups:
            canonical = full_name(location.name, group)
            aliases = [f"{alias}{SEPARATOR}{group}" for alias in location.aliases]

            if len(group_owners[group.casefold()]) == 1:
                _register(group, canonical)
                aliases.insert(0, group)

            NAMES_FOR[canonical] = (canonical, *aliases)


_build()


def resolve(value) -> str | None:
    """The canonical location this names, or None if it names none of them."""
    text = clean(value)

    if not text:
        return None

    canonical = _LOOKUP.get(text.casefold())

    if canonical is not None:
        return canonical

    return _COMPACT_LOOKUP.get(_compact(text))


def search(query: str = "") -> list[tuple[str, str | None]]:
    """Known locations matching the query, best first.

    Each result is the canonical name and the name that actually matched, which
    is None when that was the canonical one. Somebody typing "mastercraft"
    needs to see that it is going to become "Tool Chest 3" before they pick it.
    """
    wanted = clean(query).casefold()

    if not wanted:
        return [(name, None) for name in KNOWN_LOCATIONS]

    compact_wanted = _compact(query)
    scored = []

    def rank_of(name: str) -> int | None:
        folded = name.casefold()

        if folded == wanted:
            return 0

        if folded.startswith(wanted):
            return 1

        if wanted in folded:
            return 2

        if compact_wanted and compact_wanted in _compact(name):
            return 3

        return None

    for position, canonical in enumerate(KNOWN_LOCATIONS):
        best = rank_of(canonical)
        matched = None

        for alias in NAMES_FOR[canonical][1:]:
            rank = rank_of(alias)

            if rank is None:
                continue

            if best is None or rank < best:
                best = rank

            # Reported only when the alias is the reason this is in the list at
            # all. Saying '4A -> Shelf 4A' to someone who typed "4" tells them
            # nothing they cannot already see.
            if matched is None and rank_of(canonical) is None:
                matched = alias

        if best is not None:
            scored.append((best, position, canonical, matched))

    scored.sort(key=lambda entry: entry[:2])

    return [(canonical, matched) for _, _, canonical, matched in scored]


def is_known(value) -> bool:
    return resolve(value) is not None
