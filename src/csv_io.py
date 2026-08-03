"""Table read/write helpers and input-mapping role resolution.

Physical table names are never hardcoded; roles are resolved from the tables mapped
in Keboola Input/Output Mapping by case-insensitive substring match on table name.
"""

import csv

from keboola.component.exceptions import UserException

from constants import REQUIRED_COLUMNS, TABLE_ROLE_PATTERNS


def resolve_table_by_role(tables: list, role: str):
    """Resolve exactly one table definition matching `role`.

    Raises UserException if the role is missing or matched by more than one table.
    """
    pattern = TABLE_ROLE_PATTERNS[role].lower()
    matches = [t for t in tables if pattern in t.name.lower()]

    if len(matches) == 0:
        raise UserException(
            f"No mapped table found for role '{role}' (expected a table name containing '{TABLE_ROLE_PATTERNS[role]}')."
        )
    if len(matches) > 1:
        matched_names = [t.name for t in matches]
        raise UserException(f"Ambiguous mapped tables for role '{role}': {matched_names}")

    return matches[0]


def validate_required_columns(fieldnames: list, role: str) -> None:
    """Safety check that a resolved table actually contains the columns this component needs."""
    required = REQUIRED_COLUMNS[role]
    missing = [col for col in required if col not in fieldnames]
    if missing:
        raise UserException(f"Table for role '{role}' is missing required column(s): {missing}")


def read_table(table_definition) -> tuple[list[dict], list[str]]:
    """Read a Keboola input table. Returns (rows, fieldnames)."""
    with open(table_definition.full_path, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])
    return rows, fieldnames


def write_table(table_definition, rows: list[dict], fieldnames: list[str]) -> None:
    """Write a Keboola output table (full replace). Passes through unknown columns unchanged."""
    with open(table_definition.full_path, mode="w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore", restval="")
        writer.writeheader()
        writer.writerows(rows)
