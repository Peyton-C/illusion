from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any


class SpreadsheetManager:
    def __init__(self, file_path: str, sheet_name: str = "Inventory", sku_prefix: str = "SKU"):
        self.file_path = Path(file_path)
        self.sheet_name = sheet_name
        self.sku_prefix = sku_prefix

        self.sku_header = "SKU"
        self.sku_padding = 6

        self.default_headers = ["SKU", "NAME", "PRIORITY", "ORDER_QUANTITY", "LOW", 
                                "LINK_1", "VENDOR_1", "LINK_2", "VENDOR_2", "LINK_3", "VENDOR_3",
                                "LINK_4", "VENDOR_4", "LINK_5","VENDOR_5",
        ]

        self.item_fields = {"SKU", "NAME", "PRIORITY", "ORDER_QUANTITY", "LOW",}

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

    def _create_tables(self) -> None:
        with self.lock:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS items (
                    sku TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    priority TEXT,
                    order_quantity TEXT,
                    low INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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
            self.connection.commit()

    def _get_headers(self) -> list[str]:
        return list(self.default_headers)

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
            "PRIORITY": row["priority"],
            "ORDER_QUANTITY": row["order_quantity"],
            "LOW": self._bool_to_python(row["low"]),
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
        
    def _escape_like(self, value: str) -> str:
        return (
            value.replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )

    def read_all(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.connection.execute(
                """
                SELECT sku, name, priority, order_quantity, low
                FROM items
                ORDER BY sku
                """
            ).fetchall()

            return [self._row_to_dict(row) for row in rows]

    def validate_sku(self, sku: str) -> bool:
        return self._find_row_by_sku(sku) is not None

    def get_item(self, sku: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.connection.execute(
                """
                SELECT sku, name, priority, order_quantity, low
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
            priority = item_data.get("PRIORITY")
            order_quantity = item_data.get("ORDER_QUANTITY")
            low = self._normalize_bool(item_data.get("LOW"))

            if not name:
                raise ValueError("NAME is required.")

            self.connection.execute(
                """
                INSERT INTO items (
                    sku,
                    name,
                    priority,
                    order_quantity,
                    low
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    new_sku,
                    name,
                    priority,
                    order_quantity,
                    low,
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

                if header in {"NAME", "PRIORITY", "ORDER_QUANTITY", "LOW"}:
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
                    "PRIORITY": "priority",
                    "ORDER_QUANTITY": "order_quantity",
                    "LOW": "low",
                }

                assignments = []
                values = []

                for header, value in item_updates.items():
                    column = column_map[header]
                    assignments.append(f"{column} = ?")

                    if header == "LOW":
                        values.append(self._normalize_bool(value))
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
        
    def search_items(self, name_query: str, limit: int = 10) -> list[dict[str, Any]]:
        name_query = name_query.strip()

        if not name_query:
            return []

        escaped_query = self._escape_like(name_query)

        contains_pattern = f"%{escaped_query}%"
        prefix_pattern = f"{escaped_query}%"

        with self.lock:
            rows = self.connection.execute(
                """
                SELECT sku, name, priority, order_quantity, low
                FROM items
                WHERE LOWER(name) LIKE LOWER(?) ESCAPE '\\'
                ORDER BY
                    CASE
                        WHEN LOWER(name) = LOWER(?) THEN 0
                        WHEN LOWER(name) LIKE LOWER(?) ESCAPE '\\' THEN 1
                        ELSE 2
                    END,
                    name COLLATE NOCASE
                LIMIT ?
                """,
                (
                    contains_pattern,
                    name_query,
                    prefix_pattern,
                    limit,
                ),
            ).fetchall()

            return [self._row_to_dict(row) for row in rows]

    def save(self) -> None:
        with self.lock:
            self.connection.commit()

    def close(self) -> None:
        with self.lock:
            self.connection.close()