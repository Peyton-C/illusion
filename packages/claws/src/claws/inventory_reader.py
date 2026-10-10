from __future__ import annotations

import re
import sqlite3
import threading
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz, process

from claws import locations

# Search scoring. Every word of the query gets a 0-100 score against each column
# below, and the row keeps the best column per word. A row only survives if all
# of its words land somewhere, so "pla black" needs both, in any order.
_SEARCH_WORD_RE = re.compile(r"[a-z0-9]+")

# (column, weight, fuzzy). Fuzzy matching is only worth its cost on the columns
# people actually type at, notes and part numbers are there for exact recall.
_SEARCH_FIELDS = (
    ("name", 1.0, True),
    ("tags", 0.9, True),
    ("location", 0.9, True),
    ("sku", 0.85, False),
    ("digikey_part_number", 0.85, False),
    ("notes", 0.5, False),
)

_SEARCH_FUZZY_FLOOR = 72  # below this a misspelling is just a different word
_SEARCH_WORD_FLOOR = 40   # every query word has to clear this somewhere


def _search_words(value: Any) -> list[str]:
    return _SEARCH_WORD_RE.findall(str(value or "").casefold())


def _score_word(word: str, words: list[str], compact: str, fuzzy: bool) -> float:
    """Best score for one query word in one column, 0 if it is not in there."""
    best = 0.0

    for candidate in words:
        if candidate == word:
            return 100.0

        if candidate.startswith(word):
            best = max(best, 94.0)
        elif word in candidate:
            best = max(best, 86.0)

    if best:
        return best

    # "m3x12" should still find "M3 x 12mm", so retry against the column with
    # its spaces taken out before paying for fuzzy matching
    if word in compact:
        return 80.0

    # Never fuzzy match a word with a digit in it. "12mm" and "10mm" are one
    # edit apart and are different screws, guessing there hands someone the
    # wrong part. Sizes and part numbers have to be typed right.
    if not fuzzy or any(character.isdigit() for character in word):
        return 0.0

    match = process.extractOne(
        word, words, scorer=fuzz.ratio, score_cutoff=_SEARCH_FUZZY_FLOOR
    )

    if match is None:
        return 0.0

    # Scaled so a typo can never outrank a word that really is in the column
    return match[1] * 0.78


def _like_prefix(location: str) -> str:
    """A LIKE pattern for everything filed under this location.

    Escaped, because a location is free text and a stray % in one would
    otherwise turn a lookup of one shelf into a lookup of every shelf.
    """
    escaped = (
        location.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )

    return f"{escaped}{locations.SEPARATOR}%"


class SpreadsheetManager:
    def __init__(self, file_path: str, sheet_name: str = "Inventory", sku_prefix: str = "EER"):
        self.file_path = Path(file_path)
        self.sheet_name = sheet_name
        self.sku_prefix = sku_prefix

        self.sku_header = "SKU"
        self.sku_padding = 6

        self.default_headers = ["SKU", "NAME", "LOCATION", "ORDER_QUANTITY", "LOW", "LOW_THREAD_ID",
                                "TRACKING_MODE", "QUANTITY_ON_HAND", "LOW_THRESHOLD", "DECREASE_AMOUNT",
                                "LINK_1", "VENDOR_1", "LINK_2", "VENDOR_2", "LINK_3", "VENDOR_3",
                                "LINK_4", "VENDOR_4", "LINK_5", "VENDOR_5", "DIGIKEY_PART_NUMBER",
                                "TAGS", "NOTES",
        ]

        self.item_fields = {"SKU", "NAME", "ORDER_QUANTITY", "LOW",}

        self.lock = threading.RLock()

        self.file_path.parent.mkdir(parents=True, exist_ok=True)

        self.connection = sqlite3.connect(
            self.file_path,
            check_same_thread=False,
        )
        self.connection.row_factory = sqlite3.Row

        self._configure_database()
        self._create_tables()

    def _configure_database(self) -> None:
        with self.lock:
            self.connection.execute("PRAGMA foreign_keys = ON")
            self.connection.execute("PRAGMA journal_mode = WAL")
            self.connection.execute("PRAGMA synchronous = NORMAL")
            self.connection.execute("PRAGMA busy_timeout = 5000")
            
    def _migrate_items_table(self) -> None:
        existing_columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(items)").fetchall()
        }

        if "low_thread_id" not in existing_columns:
            self.connection.execute(
                "ALTER TABLE items ADD COLUMN low_thread_id INTEGER"
            )
        
        # 1.0.0 migration
        if "digikey_part_number" not in existing_columns:
            self.connection.execute(
                "ALTER TABLE items ADD COLUMN digikey_part_number TEXT"
            )
            self.connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_items_dkpn
                    ON items (digikey_part_number)
                    WHERE digikey_part_number IS NOT NULL
                """
            )

        # 1.3.0 migration
        if "tags" not in existing_columns:
            self.connection.execute(
                "ALTER TABLE items ADD COLUMN tags TEXT"
            )
        
        if "notes" not in existing_columns:
            self.connection.execute(
                "ALTER TABLE items ADD COLUMN notes TEXT"
            )

        # 1.3.X fix (1.4.0)
        self._remove_literal_none_tags()

        # 1.6.0 migration
        if "priority" in existing_columns:
            self.connection.execute(
                "ALTER TABLE items DROP COLUMN priority"
            )

        if "unit" in existing_columns:
            self.connection.execute(
                "ALTER TABLE items DROP COLUMN unit"
            )

        if "location" not in existing_columns:
            self.connection.execute(
                "ALTER TABLE items ADD COLUMN location TEXT"
            )

        # After the migration rather than in _create_tables, because on an older
        # database the column does not exist until the line above has run
        self.connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_items_location_nocase
                ON items (location COLLATE NOCASE)
            """
        )

        self._canonicalize_locations()

    def _canonicalize_locations(self) -> None:
        """Rewrite stored locations the catalogue now spells differently.

        Not tied to a version, because the catalogue is the thing that changes:
        the day "Screw Wall" becomes an alias of "Wall Storage", every item
        already on that wall is stored under a name that a lookup of the wall
        no longer matches. Renaming a location is meant to be a one line edit
        to the catalogue, and this is what makes it one.
        """
        rows = self.connection.execute(
            """
            SELECT sku, location
            FROM items
            WHERE location IS NOT NULL
            AND TRIM(location) != ''
            """
        ).fetchall()

        for row in rows:
            canonical = locations.resolve(row["location"])

            if canonical is None or canonical == row["location"]:
                continue

            self.connection.execute(
                """
                UPDATE items
                SET location = ?
                WHERE sku = ?
                """,
                (canonical, row["sku"]),
            )

    def _remove_literal_none_tags(self) -> None:
        rows = self.connection.execute(
            """
            SELECT sku, tags
            FROM items
            WHERE tags LIKE '%None%'
            """
        ).fetchall()

        for row in rows:
            tags = self._split_tags(row["tags"])
            cleaned = [tag for tag in tags if tag != "None"]

            if len(cleaned) == len(tags):
                continue

            self.connection.execute(
                """
                UPDATE items
                SET tags = ?
                WHERE sku = ?
                """,
                (self._join_tags(cleaned) or None, row["sku"]),
            )

            
    def _create_tables(self) -> None:
        with self.lock:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS items (
                    sku TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    order_quantity TEXT,
                    low_thread_id INTEGER,
                    low INTEGER NOT NULL DEFAULT 0,

                    tracking_mode TEXT NOT NULL DEFAULT 'KANBAN',
                    quantity_on_hand REAL,
                    low_threshold REAL,
                    decrease_amount REAL NOT NULL DEFAULT 1.0,

                    location TEXT,
                    tags TEXT,
                    notes TEXT,

                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

                    CHECK (
                        tracking_mode IN (
                            'KANBAN',
                            'QUANTITY',
                            'HYBRID'
                        )
                    )
                );

                CREATE TABLE IF NOT EXISTS vendors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sku TEXT NOT NULL,
                    vendor_number INTEGER NOT NULL,
                    vendor_name TEXT,
                    link TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

                    FOREIGN KEY (sku)
                        REFERENCES items (sku)
                        ON DELETE CASCADE,

                    UNIQUE (sku, vendor_number),

                    CHECK (
                        vendor_number >= 1
                        AND vendor_number <= 5
                    )
                );

                CREATE INDEX IF NOT EXISTS idx_vendors_sku
                    ON vendors (sku);

                -- Every DigiKey bag whose stock has been counted, so the same
                -- bag scanned twice is caught instead of counted twice. Not
                -- unique on barcode: two bags off one order line can carry
                -- identical labels, and counting the second is a deliberate
                -- override that still gets its own row. Cascades so a deleted
                -- item forgets its bags, and a reused sku inherits none.
                CREATE TABLE IF NOT EXISTS digikey_scans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    barcode TEXT NOT NULL,
                    sku TEXT NOT NULL,
                    digikey_part_number TEXT,
                    quantity REAL,
                    scanned_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

                    FOREIGN KEY (sku)
                        REFERENCES items (sku)
                        ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_digikey_scans_barcode
                    ON digikey_scans (barcode);

                CREATE INDEX IF NOT EXISTS idx_items_name_nocase
                    ON items (name COLLATE NOCASE);

                CREATE TRIGGER IF NOT EXISTS trg_items_updated_at
                AFTER UPDATE ON items
                FOR EACH ROW
                BEGIN
                    UPDATE items
                    SET updated_at = CURRENT_TIMESTAMP
                    WHERE sku = OLD.sku;
                END;
                """
            )
            self._migrate_items_table()
            self.connection.commit()

    def _get_headers(self) -> list[str]:
        return list(self.default_headers)
    
    def _normalize_tracking_mode(self, value: Any) -> str:
        if value is None:
            return "KANBAN"

        tracking_mode = str(value).strip().upper()

        if tracking_mode not in {"KANBAN", "QUANTITY", "HYBRID"}:
            raise ValueError(
                "TRACKING_MODE must be one of: KANBAN, QUANTITY, HYBRID."
            )

        return tracking_mode


    def _normalize_float(
        self,
        value: Any,
        default: float | None = None,
    ) -> float | None:
        if value is None or value == "":
            return default

        return float(value)

    def _normalize_bool(self, value: Any) -> int:
        if isinstance(value, bool):
            return int(value)

        if value is None:
            return 0

        if isinstance(value, int):
            return int(value != 0)

        value_string = str(value).strip().lower()

        if value_string in {"true", "yes", "y", "1"}:
            return 1

        return 0

    def _bool_to_python(self, value: Any) -> bool:
        return bool(value)

    def _generate_sku(self) -> str:
        with self.lock:
            rows = self.connection.execute(
                """
                SELECT sku
                FROM items
                WHERE sku LIKE ?
                """,
                (f"{self.sku_prefix}-%",),
            ).fetchall()

            highest_number = 0

            for row in rows:
                sku = str(row["sku"])

                if not sku.startswith(f"{self.sku_prefix}-"):
                    continue

                number_part = sku.replace(f"{self.sku_prefix}-", "", 1)

                if number_part.isdigit():
                    highest_number = max(highest_number, int(number_part))

            next_number = highest_number + 1
            return f"{self.sku_prefix}-{next_number:0{self.sku_padding}d}"

    def _row_to_dict(self, row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        item = {
            "SKU": row["sku"],
            "NAME": row["name"],
            "LOCATION": row["location"],
            "ORDER_QUANTITY": row["order_quantity"],
            "LOW": self._bool_to_python(row["low"]),
            "TRACKING_MODE": row["tracking_mode"],
            "QUANTITY_ON_HAND": row["quantity_on_hand"],
            "LOW_THRESHOLD": row["low_threshold"],
            "DECREASE_AMOUNT": row["decrease_amount"],
            "LOW_THREAD_ID": row["low_thread_id"],
            "DIGIKEY_PART_NUMBER": row["digikey_part_number"],
            "TAGS": row["tags"],
            "NOTES": row["notes"],
        }

        for vendor_number in range(1, 6):
            item[f"LINK_{vendor_number}"] = None
            item[f"VENDOR_{vendor_number}"] = None

        vendor_rows = self.connection.execute(
            """
            SELECT vendor_number, vendor_name, link
            FROM vendors
            WHERE sku = ?
            ORDER BY vendor_number
            """,
            (row["sku"],),
        ).fetchall()

        for vendor_row in vendor_rows:
            vendor_number = vendor_row["vendor_number"]
            item[f"VENDOR_{vendor_number}"] = vendor_row["vendor_name"]
            item[f"LINK_{vendor_number}"] = vendor_row["link"]

        return item

    def _find_row_by_sku(self, sku: str) -> str | None:
        with self.lock:
            row = self.connection.execute(
                """
                SELECT sku
                FROM items
                WHERE sku = ?
                """,
                (sku,),
            ).fetchone()

            if row is None:
                return None

            return str(row["sku"])
        
    def _normalize_location(self, value: Any) -> str | None:
        """Blank clears the location, and anything the catalogue knows is folded
        onto the canonical spelling of it.

        Locations are typed by hand at the kiosk, so "shelf 5a", "5A" and
        "Shelf 5A (Archive)" are one shelf and must not become three rows in
        get_locations. A location the catalogue has never heard of is still
        allowed -- the shop rearranges itself faster than the catalogue does --
        and then falls back to reusing a spelling already in use, so an ad-hoc
        location does not fork on capitalisation either.
        """
        if value is None:
            return None

        location = locations.clean(value)

        if not location:
            return None

        canonical = locations.resolve(location)

        if canonical is not None:
            return canonical

        with self.lock:
            row = self.connection.execute(
                """
                SELECT location
                FROM items
                WHERE location = ? COLLATE NOCASE
                LIMIT 1
                """,
                (location,),
            ).fetchone()

        if row is None:
            return location

        return str(row["location"])

    def _location_counts(self) -> dict[str, int]:
        """How many items are in each location, keyed by canonical name.

        Resolved rather than taken from the column, so a row written before the
        catalogue existed still counts towards the shelf it names.
        """
        with self.lock:
            rows = self.connection.execute(
                """
                SELECT location, COUNT(*) AS count
                FROM items
                WHERE location IS NOT NULL
                AND TRIM(location) != ''
                GROUP BY location COLLATE NOCASE
                """
            ).fetchall()

        counts: dict[str, int] = {}

        for row in rows:
            name = locations.resolve(row["location"]) or locations.clean(row["location"])
            counts[name] = counts.get(name, 0) + row["count"]

        return counts

    def get_locations(self) -> list[dict[str, Any]]:
        """Every location, grouped under its top level name.

        Known locations are listed even when nothing is in them yet, because
        the point of predefining a shelf is that someone can put the first
        thing on it. Grouped rather than flat because a row per sub-group is
        more rows than a Discord embed can hold, and because what someone
        wants from this is the shape of the shop rather than a list.
        """
        counts = self._location_counts()

        ordered = list(locations.KNOWN_LOCATIONS)
        ordered += sorted(
            (name for name in counts if name not in locations.NAMES_FOR),
            key=str.casefold,
        )

        totals: dict[str, int] = {}
        groups: dict[str, list[str]] = {}

        for name in ordered:
            top, _, group = name.partition(locations.SEPARATOR)
            count = counts.get(name, 0)

            totals[top] = totals.get(top, 0) + count
            groups.setdefault(top, [])

            if group:
                groups[top].append(f"{group} ({count})")

        return [
            {
                "LOCATION": top,
                "COUNT": totals[top],
                "SUB_GROUPS": ", ".join(groups[top]) or None,
            }
            for top in groups
        ]

    def suggest_locations(self, query: str = "", limit: int = 25) -> list[dict[str, Any]]:
        """Flat completions for the bot and the kiosk, aliases included.

        The counterpart to suggest_items: get_locations is for reading, this is
        for picking one. Known locations come first even when empty, since
        offering only the shelves already in use is how a new shelf never gets
        used.
        """
        counts = self._location_counts()
        results = []

        for canonical, matched in locations.search(query):
            results.append(
                {
                    "LOCATION": canonical,
                    "ALIAS": matched,
                    "COUNT": counts.get(canonical, 0),
                }
            )

        wanted = locations.clean(query).casefold()

        for name in sorted(counts, key=str.casefold):
            if name in locations.NAMES_FOR:
                continue

            if wanted and wanted not in name.casefold():
                continue

            results.append({"LOCATION": name, "ALIAS": None, "COUNT": counts[name]})

        return results[:limit]

    def get_items_by_location(self, location_query: str) -> list[dict[str, Any]]:
        """Everything in a location, including everything in its sub-groups.

        Asking for Shelf 4B means the whole shelf: the things filed into a bin
        on it and the things only ever recorded as being on it somewhere.
        """
        query = locations.clean(location_query)

        if not query:
            return []

        target = locations.resolve(query) or query

        with self.lock:
            rows = self.connection.execute(
                """
                SELECT sku, name, location, order_quantity, low, tracking_mode, quantity_on_hand, low_threshold, decrease_amount, low_thread_id, digikey_part_number, tags, notes
                FROM items
                WHERE location = ? COLLATE NOCASE
                OR location LIKE ? ESCAPE '\\'
                ORDER BY location COLLATE NOCASE, name COLLATE NOCASE
                """,
                (target, _like_prefix(target)),
            ).fetchall()

            return [self._row_to_dict(row) for row in rows]

    def set_location(self, sku: str, location: Any) -> bool:
        """False when there is no such item. An empty location clears it."""
        with self.lock:
            if not self.validate_sku(sku):
                return False

            self.connection.execute(
                """
                UPDATE items
                SET location = ?
                WHERE sku = ?
                """,
                (
                    self._normalize_location(location),
                    sku,
                ),
            )

            self.connection.commit()
            return True

    def _split_tags(self, value: Any) -> list[str]:
        if value is None:
            return []

        return [tag.strip() for tag in str(value).split(",") if tag.strip()]

    def _join_tags(self, tags: list[str]) -> str:
        return ", ".join(tags)

    def get_tags(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.connection.execute(
                """
                SELECT tags
                FROM items
                WHERE tags IS NOT NULL
                AND TRIM(tags) != ''
                """
            ).fetchall()

        counts: dict[str, int] = {}
        display_names: dict[str, str] = {}

        for row in rows:
            for tag in self._split_tags(row["tags"]):
                key = tag.casefold()

                counts[key] = counts.get(key, 0) + 1
                display_names.setdefault(key, tag)

        return [
            {
                "TAG": display_names[key],
                "COUNT": counts[key],
            }
            for key in sorted(display_names)
        ]

    def get_items_by_tag(self, tag_query: str) -> list[dict[str, Any]]:
        tag_query = tag_query.strip().casefold()

        if not tag_query:
            return []

        with self.lock:
            rows = self.connection.execute(
                """
                SELECT
                    sku,
                    name,
                    location,
                    order_quantity,
                    low,
                    tracking_mode,
                    quantity_on_hand,
                    low_threshold,
                    decrease_amount,
                    low_thread_id,
                    digikey_part_number,
                    tags,
                    notes
                FROM items
                WHERE tags IS NOT NULL
                ORDER BY name COLLATE NOCASE
                """
            ).fetchall()

        results = []

        for row in rows:
            item_tags = [
                tag.casefold()
                for tag in self._split_tags(row["tags"])
            ]

            if tag_query in item_tags:
                results.append(self._row_to_dict(row))

        return results

    def get_item_tags(self, sku: str) -> list[str] | None:
        with self.lock:
            row = self.connection.execute(
                """
                SELECT tags
                FROM items
                WHERE sku = ?
                """,
                (sku,),
            ).fetchone()

            if row is None:
                return None

            return self._split_tags(row["tags"])

    def read_all(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.connection.execute(
                """
                SELECT sku, name, location, order_quantity, low, tracking_mode, quantity_on_hand, low_threshold, decrease_amount, low_thread_id, digikey_part_number, tags, notes
                FROM items
                ORDER BY sku
                """
            ).fetchall()

            return [self._row_to_dict(row) for row in rows]

    def count_items(self) -> int:
        """How many items exist, without reading them.

        For /health, which is unauthenticated: counting by reading every row and
        taking its length is unbounded work for anyone who can reach the port.
        """
        with self.lock:
            return self.connection.execute("SELECT COUNT(*) FROM items").fetchone()[0]

    def validate_sku(self, sku: str) -> bool:
        return self._find_row_by_sku(sku) is not None

    def get_item(self, sku: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.connection.execute(
                """
                SELECT sku, name, location, order_quantity, low, tracking_mode, quantity_on_hand, low_threshold, decrease_amount, low_thread_id, digikey_part_number, tags, notes
                FROM items
                WHERE sku = ?
                """,
                (sku,),
            ).fetchone()

            if row is None:
                return None

            return self._row_to_dict(row)

    def add_item(self, item_data: dict[str, Any]) -> str:
        with self.lock:
            new_sku = self._generate_sku()

            name = item_data.get("NAME")
            order_quantity = item_data.get("ORDER_QUANTITY")
            low = self._normalize_bool(item_data.get("LOW"))

            tracking_mode = self._normalize_tracking_mode(
                item_data.get("TRACKING_MODE")
            )
            quantity_on_hand = self._normalize_float(
                item_data.get("QUANTITY_ON_HAND")
            )
            low_threshold = self._normalize_float(item_data.get("LOW_THRESHOLD"))
            decrease_amount = self._normalize_float(
                item_data.get("DECREASE_AMOUNT"),
                1.0,
            )

            digikey_part_number = item_data.get("DIGIKEY_PART_NUMBER")
            location = self._normalize_location(item_data.get("LOCATION"))
            tags = item_data.get("TAGS")
            notes = item_data.get("NOTES")

            if not name:
                raise ValueError("NAME is required.")

            self.connection.execute(
                """
                INSERT INTO items (
                    sku,
                    name,
                    order_quantity,
                    low,
                    tracking_mode,
                    quantity_on_hand,
                    low_threshold,
                    decrease_amount,
                    digikey_part_number,
                    location,
                    tags,
                    notes
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_sku,
                    name,
                    order_quantity,
                    low,
                    tracking_mode,
                    quantity_on_hand,
                    low_threshold,
                    decrease_amount,
                    digikey_part_number,
                    location,
                    tags,
                    notes
                ),
            )

            for vendor_number in range(1, 6):
                vendor_name = item_data.get(f"VENDOR_{vendor_number}")
                link = item_data.get(f"LINK_{vendor_number}")

                if vendor_name or link:
                    self.connection.execute(
                        """
                        INSERT INTO vendors (
                            sku,
                            vendor_number,
                            vendor_name,
                            link
                        )
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            new_sku,
                            vendor_number,
                            vendor_name,
                            link,
                        ),
                    )

            self.connection.commit()
            return new_sku

    def update_item(self, sku: str, updates: dict[str, Any]) -> bool:
        with self.lock:
            if not self.validate_sku(sku):
                return False

            item_updates: dict[str, Any] = {}
            vendor_updates: dict[int, dict[str, Any]] = {}

            for header, value in updates.items():
                if header == self.sku_header:
                    continue

                if header not in self.default_headers:
                    raise ValueError(f"Header '{header}' does not exist.")

                if header in {"NAME", "ORDER_QUANTITY", "LOW", "TRACKING_MODE", "QUANTITY_ON_HAND", "LOW_THRESHOLD", "DECREASE_AMOUNT", "LOW_THREAD_ID", "DIGIKEY_PART_NUMBER", "LOCATION", "TAGS", "NOTES"}:
                    item_updates[header] = value
                    continue

                if header.startswith("VENDOR_") or header.startswith("LINK_"):
                    field_name, vendor_number_string = header.rsplit("_", 1)
                    vendor_number = int(vendor_number_string)

                    if vendor_number not in vendor_updates:
                        vendor_updates[vendor_number] = {}

                    vendor_updates[vendor_number][field_name] = value

            if item_updates:
                column_map = {
                    "NAME": "name",
                    "ORDER_QUANTITY": "order_quantity",
                    "LOW": "low",
                    "TRACKING_MODE": "tracking_mode",
                    "QUANTITY_ON_HAND": "quantity_on_hand",
                    "LOW_THRESHOLD": "low_threshold",
                    "DECREASE_AMOUNT": "decrease_amount",
                    "LOW_THREAD_ID": "low_thread_id",
                    "DIGIKEY_PART_NUMBER": "digikey_part_number",
                    "LOCATION": "location",
                    "TAGS": "tags",
                    "NOTES": "notes",
                }


                assignments = []
                values = []

                for header, value in item_updates.items():
                    column = column_map[header]
                    assignments.append(f"{column} = ?")

                    if header == "LOW":
                        values.append(self._normalize_bool(value))
                    elif header == "TRACKING_MODE":
                        values.append(self._normalize_tracking_mode(value))
                    elif header == "LOCATION":
                        values.append(self._normalize_location(value))
                    elif header in {
                        "QUANTITY_ON_HAND",
                        "LOW_THRESHOLD",
                        "DECREASE_AMOUNT",
                    }:
                        values.append(self._normalize_float(value))
                    else:
                        values.append(value)

                values.append(sku)

                self.connection.execute(
                    f"""
                    UPDATE items
                    SET {", ".join(assignments)}
                    WHERE sku = ?
                    """,
                    values,
                )

            for vendor_number, vendor_data in vendor_updates.items():
                existing = self.connection.execute(
                    """
                    SELECT id
                    FROM vendors
                    WHERE sku = ?
                    AND vendor_number = ?
                    """,
                    (sku, vendor_number),
                ).fetchone()

                current_vendor_name = None
                current_link = None

                if existing is not None:
                    current = self.connection.execute(
                        """
                        SELECT vendor_name, link
                        FROM vendors
                        WHERE sku = ?
                        AND vendor_number = ?
                        """,
                        (sku, vendor_number),
                    ).fetchone()

                    current_vendor_name = current["vendor_name"]
                    current_link = current["link"]

                vendor_name = vendor_data.get("VENDOR", current_vendor_name)
                link = vendor_data.get("LINK", current_link)

                if existing is None:
                    self.connection.execute(
                        """
                        INSERT INTO vendors (
                            sku,
                            vendor_number,
                            vendor_name,
                            link
                        )
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            sku,
                            vendor_number,
                            vendor_name,
                            link,
                        ),
                    )
                else:
                    self.connection.execute(
                        """
                        UPDATE vendors
                        SET vendor_name = ?,
                            link = ?
                        WHERE sku = ?
                        AND vendor_number = ?
                        """,
                        (
                            vendor_name,
                            link,
                            sku,
                            vendor_number,
                        ),
                    )

            self.connection.commit()
            return True

    def preview_rename(self, find: str, replace: str, case_sensitive: bool = False) -> list[dict[str, Any]]:
        """Every item whose name would change, without writing anything.

        Matching is substring rather than whole-word, and case-insensitive
        unless asked otherwise -- the point is catching a typo made
        consistently (ex: through Discord's edit-last-command), and someone
        fixing "capasiter" should not have to also chase "Capasiter" and
        "CAPASITER" down one at a time.
        """
        find = str(find or "")

        if not find:
            return []

        flags = 0 if case_sensitive else re.IGNORECASE
        pattern = re.compile(re.escape(find), flags)

        with self.lock:
            rows = self.connection.execute("SELECT sku, name FROM items").fetchall()

        changes = []

        for row in rows:
            name = row["name"] or ""
            new_name = pattern.sub(replace, name)

            if new_name != name:
                changes.append({"SKU": row["sku"], "OLD_NAME": name, "NEW_NAME": new_name})

        return changes

    def apply_rename(self, changes: list[dict[str, Any]]) -> None:
        """Write exactly the changes given, in one transaction.

        Takes the changes rather than a find/replace pair so the caller
        decides how fresh they need to be. The service re-runs preview_rename
        right before this, so a rename made in the gap between someone seeing
        the preview and pressing confirm is reflected rather than clobbered.
        """
        with self.lock:
            for change in changes:
                self.connection.execute(
                    """
                    UPDATE items
                    SET name = ?
                    WHERE sku = ?
                    """,
                    (change["NEW_NAME"], change["SKU"]),
                )

            self.connection.commit()

    def delete_item(self, sku: str) -> bool:
        with self.lock:
            cursor = self.connection.execute(
                """
                DELETE FROM items
                WHERE sku = ?
                """,
                (sku,),
            )

            self.connection.commit()
            return cursor.rowcount > 0
        
    def decrease_item(self, sku: str, amount: float | None = None,) -> dict[str, Any] | None:
        with self.lock:
            row = self.connection.execute(
                """
                SELECT
                    sku,
                    name,
                    location,
                    order_quantity,
                    low,
                    tracking_mode,
                    quantity_on_hand,
                    low_threshold,
                    decrease_amount,
                    low_thread_id,
                    digikey_part_number,
                    tags,
                    notes
                FROM items
                WHERE sku = ?
                """,
                (sku,),
            ).fetchone()

            if row is None:
                return None

            item = self._row_to_dict(row)
            tracking_mode = item["TRACKING_MODE"]
            was_low = item["LOW"]

            if tracking_mode == "KANBAN":
                if not was_low:
                    self.connection.execute(
                        """
                        UPDATE items
                        SET low = 1
                        WHERE sku = ?
                        """,
                        (sku,),
                    )
                    self.connection.commit()

                return {
                    "item": self.get_item(sku),
                    "tracking_mode": tracking_mode,
                    "quantity_changed": False,
                    "old_quantity": None,
                    "new_quantity": None,
                    "decrease_amount": None,
                    "low_changed": not was_low,
                }

            old_quantity = self._normalize_float(
                item["QUANTITY_ON_HAND"],
                0.0,
            )
            decrease_amount = self._normalize_float(
                amount,
                item["DECREASE_AMOUNT"] or 1.0,
            )

            if decrease_amount is None or decrease_amount <= 0:
                raise ValueError("Decrease amount must be greater than 0.")

            new_quantity = max(0.0, old_quantity - decrease_amount)

            low_threshold = self._normalize_float(item["LOW_THRESHOLD"])

            if low_threshold is None:
                should_be_low = new_quantity <= 0
            else:
                should_be_low = new_quantity <= low_threshold

            new_low = was_low or should_be_low

            self.connection.execute(
                """
                UPDATE items
                SET quantity_on_hand = ?,
                    low = ?
                WHERE sku = ?
                """,
                (
                    new_quantity,
                    self._normalize_bool(new_low),
                    sku,
                ),
            )

            self.connection.commit()

            return {
                "item": self.get_item(sku),
                "tracking_mode": tracking_mode,
                "quantity_changed": True,
                "old_quantity": old_quantity,
                "new_quantity": new_quantity,
                "decrease_amount": decrease_amount,
                "low_changed": should_be_low and not was_low,
            }


    def set_stock(self, sku: str, quantity: float) -> dict[str, Any] | None:
        with self.lock:
            item = self.get_item(sku)

            if item is None:
                return None

            quantity = float(quantity)
            low_threshold = self._normalize_float(item["LOW_THRESHOLD"])

            if low_threshold is None:
                low = quantity <= 0
            else:
                low = quantity <= low_threshold

            self.connection.execute(
                """
                UPDATE items
                SET quantity_on_hand = ?,
                    low = ?
                WHERE sku = ?
                """,
                (
                    quantity,
                    self._normalize_bool(low),
                    sku,
                ),
            )

            self.connection.commit()

            return self.get_item(sku)
    
    def increase_item(self, sku: str, amount: float) -> dict[str, Any] | None:
        with self.lock:
            item = self.get_item(sku)

            if item is None:
                return None

            amount = float(amount)

            if amount <= 0:
                raise ValueError("Increase amount must be greater than 0.")

            current_quantity = self._normalize_float(
                item["QUANTITY_ON_HAND"],
                0.0,
            )

            new_quantity = current_quantity + amount

            return self.set_stock(sku, new_quantity)

    def add_vendor(self, sku: str, vendor_name: str, link: str,) -> bool:
        with self.lock:
            if not self.validate_sku(sku):
                return False

            used_vendor_numbers = {
                row["vendor_number"]
                for row in self.connection.execute(
                    """
                    SELECT vendor_number
                    FROM vendors
                    WHERE sku = ?
                    """,
                    (sku,),
                ).fetchall()
            }

            for vendor_number in range(1, 6):
                if vendor_number not in used_vendor_numbers:
                    self.connection.execute(
                        """
                        INSERT INTO vendors (
                            sku,
                            vendor_number,
                            vendor_name,
                            link
                        )
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            sku,
                            vendor_number,
                            vendor_name,
                            link,
                        ),
                    )
                    self.connection.commit()
                    return True

            return False

    def add_tag(self, sku: str, tag: str) -> bool:
        with self.lock:
            if not self.validate_sku(sku):
                return False

            tag = str(tag).strip()

            if not tag:
                return False

            if "," in tag:
                raise ValueError("Tags cannot contain commas.")

            row = self.connection.execute(
                """
                SELECT tags
                FROM items
                WHERE sku = ?
                """,
                (sku,),
            ).fetchone()

            if row is None:
                return False

            tags = self._split_tags(row["tags"])
            existing_keys = {existing_tag.casefold() for existing_tag in tags}

            if tag.casefold() in existing_keys:
                return True

            tags.append(tag)

            self.connection.execute(
                """
                UPDATE items
                SET tags = ?
                WHERE sku = ?
                """,
                (
                    self._join_tags(tags),
                    sku,
                ),
            )

            self.connection.commit()
            return True

    def preview_tag_rename(self, find_tag: str, replace_tag: str, case_sensitive: bool = False) -> list[dict[str, Any]]:
        """Every item whose tag list would change if find_tag were renamed to replace_tag.

        A tag is matched whole, never as a substring of a longer one --
        renaming "BlueRobotics" must not also catch a hypothetical
        "BlueRoboticsSpares". An item already carrying both is included too,
        with the duplicate folded away in NEW_TAGS: that fold is the merge,
        for two spellings of the same vendor that both ended up on an item.
        """
        find_tag = str(find_tag or "").strip()
        replace_tag = str(replace_tag or "").strip()

        if not find_tag or not replace_tag:
            return []

        fold = (lambda tag: tag) if case_sensitive else str.casefold
        target = fold(find_tag)

        with self.lock:
            rows = self.connection.execute(
                """
                SELECT sku, name, tags
                FROM items
                WHERE tags IS NOT NULL
                """
            ).fetchall()

        changes = []

        for row in rows:
            tags = self._split_tags(row["tags"])

            if target not in {fold(tag) for tag in tags}:
                continue

            new_tags = []
            seen = set()

            for tag in tags:
                new_tag = replace_tag if fold(tag) == target else tag
                new_key = fold(new_tag)

                if new_key in seen:
                    continue

                seen.add(new_key)
                new_tags.append(new_tag)

            changes.append(
                {
                    "SKU": row["sku"],
                    "NAME": row["name"],
                    "OLD_TAGS": self._join_tags(tags),
                    "NEW_TAGS": self._join_tags(new_tags),
                }
            )

        return changes

    def apply_tag_rename(self, changes: list[dict[str, Any]]) -> None:
        """Write exactly the changes given, in one transaction.

        Takes the changes rather than a find/replace pair for the same reason
        apply_rename does: the service re-runs preview_tag_rename right
        before this, so a tag added or removed in the gap between the preview
        and the confirm is reflected rather than clobbered.
        """
        with self.lock:
            for change in changes:
                self.connection.execute(
                    """
                    UPDATE items
                    SET tags = ?
                    WHERE sku = ?
                    """,
                    (change["NEW_TAGS"] or None, change["SKU"]),
                )

            self.connection.commit()

    def search_items(self, name_query: str, limit: int = 10) -> list[dict[str, Any]]:
        name_query = name_query.strip()

        if not name_query:
            return []

        query_words = _search_words(name_query)

        if not query_words:
            return []

        query_folded = name_query.casefold()
        query_compact = "".join(query_words)

        # The whole table, scored in here. At a couple thousand items that is
        # cheaper than it sounds, and it is the only way to tolerate typos
        # without an index sqlite cannot give us (no spellfix1 in our build)
        with self.lock:
            rows = self.connection.execute(
                """
                SELECT sku, name, location, order_quantity, low, tracking_mode, quantity_on_hand, low_threshold, decrease_amount, low_thread_id, digikey_part_number, tags, notes
                FROM items
                """
            ).fetchall()

        scored = []

        for row in rows:
            columns = []

            for column, weight, fuzzy in _SEARCH_FIELDS:
                words = _search_words(row[column])
                columns.append((words, "".join(words), weight, fuzzy))

            word_scores = []

            for word in query_words:
                best = 0.0

                for words, compact, weight, fuzzy in columns:
                    best = max(best, weight * _score_word(word, words, compact, fuzzy))

                    if best == 100.0:  # an exact name hit, nothing can beat it
                        break

                if best < _SEARCH_WORD_FLOOR:
                    word_scores = None
                    break

                word_scores.append(best)

            if word_scores is None:  # a word landed nowhere, so the row is out
                continue

            score = sum(word_scores) / len(word_scores)

            # Put the obvious answers on top: an exact name beats a name that
            # starts with the query, which beats one that only contains it
            name_folded = str(row["name"] or "").casefold()

            if name_folded == query_folded:
                score += 1000.0
            elif name_folded.startswith(query_folded):
                score += 500.0
            elif query_compact in "".join(_search_words(row["name"])):
                score += 250.0

            if query_folded in {tag.casefold() for tag in self._split_tags(row["tags"])}:
                score += 200.0

            # Typing a shelf name is asking what is on that shelf, so the items
            # actually on it come before the ones that only mention it
            if query_folded == str(row["location"] or "").casefold():
                score += 200.0

            scored.append((-score, name_folded, row))

        scored.sort(key=lambda entry: entry[:2])

        return [self._row_to_dict(row) for _, _, row in scored[:limit]]
        
    def suggest_items(self, query: str, limit: int = 25) -> list[dict[str, str]]:
        """sku and name only, for the bot's autocomplete.

        Same ranking as search_items, just a much smaller payload, since this
        runs on every keystroke and only ever fills a dropdown. An empty query
        is the moment the field is focused, so list the top of the inventory
        rather than nothing.
        """
        query = query.strip()

        if query:
            return [
                {"SKU": item["SKU"], "NAME": item["NAME"]}
                for item in self.search_items(query, limit=limit)
            ]

        with self.lock:
            rows = self.connection.execute(
                """
                SELECT sku, name
                FROM items
                ORDER BY name COLLATE NOCASE
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        return [{"SKU": row["sku"], "NAME": row["name"]} for row in rows]

    def get_item_by_dkpn(self, dkpn: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM items WHERE digikey_part_number = ?",
                (dkpn.strip(),),
            ).fetchone()
            return self._row_to_dict(row) if row else None

    def _normalize_barcode(self, barcode: str) -> str:
        # The same label reaches claws with its separators either as raw
        # control characters or as their visible stand-ins, depending on the
        # scanner and what the kiosk did to it. Stored one way so both match.
        return barcode.strip().replace("\x1d", "␝").replace("\x1e", "␞")

    def get_digikey_scan(self, barcode: str) -> dict[str, Any] | None:
        """The most recent time this bag was counted, or None if it never was."""
        with self.lock:
            row = self.connection.execute(
                """
                SELECT sku, digikey_part_number, quantity, scanned_at,
                       COUNT(*) OVER () AS times_scanned
                FROM digikey_scans
                WHERE barcode = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (self._normalize_barcode(barcode),),
            ).fetchone()

        if row is None:
            return None

        return {
            "SKU": row["sku"],
            "DIGIKEY_PART_NUMBER": row["digikey_part_number"],
            "QUANTITY": row["quantity"],
            "SCANNED_AT": row["scanned_at"],
            "TIMES_SCANNED": row["times_scanned"],
        }

    def record_digikey_scan(self, barcode: str, sku: str, dkpn: str | None, quantity: float | None) -> bool:
        with self.lock:
            if not self.validate_sku(sku):
                return False

            self.connection.execute(
                """
                INSERT INTO digikey_scans (
                    barcode,
                    sku,
                    digikey_part_number,
                    quantity
                )
                VALUES (?, ?, ?, ?)
                """,
                (self._normalize_barcode(barcode), sku, dkpn, quantity),
            )
            self.connection.commit()

            return True

    def save(self) -> None:
        with self.lock:
            self.connection.commit()

    def close(self) -> None:
        with self.lock:
            self.connection.close()