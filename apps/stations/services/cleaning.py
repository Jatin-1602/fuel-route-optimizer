"""Cleaning and de-duplication of the raw OPIS fuel-prices CSV (pure Python, no Django)."""

import csv
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from apps.stations.us_states import US_STATE_CODES

RAW_COLUMNS: tuple[str, ...] = (
    "OPIS Truckstop ID",
    "Truckstop Name",
    "Address",
    "City",
    "State",
    "Rack ID",
    "Retail Price",
)


@dataclass(frozen=True, slots=True)
class CleanStation:
    """A single US truck stop after cleaning and de-duplication."""

    opis_id: int
    name: str
    address: str
    city: str
    state: str
    rack_id: int | None
    price: Decimal
    source_rows: int = 1  # how many raw CSV rows were merged into this station


@dataclass(slots=True)
class CleaningReport:
    """Counts describing what :func:`clean_rows` did."""

    rows_in: int = 0
    malformed_dropped: int = 0
    invalid_price_dropped: int = 0
    non_us_dropped: Counter[str] = field(default_factory=Counter)
    duplicate_rows_merged: int = 0
    ids_with_conflicting_prices: int = 0
    stations_out: int = 0


def clean_text(value: str | None) -> str:
    """Trim and collapse internal whitespace (``"I-35,  EXIT 271 "`` -> ``"I-35, EXIT 271"``)."""
    return " ".join((value or "").split())


def _parse_price(value: str) -> Decimal | None:
    try:
        price = Decimal(clean_text(value))
    except InvalidOperation:
        return None
    return price if price.is_finite() and price > 0 else None


def clean_rows(rows: list[dict[str, str]]) -> tuple[list[CleanStation], CleaningReport]:
    """Clean raw CSV rows and merge duplicate OPIS IDs.

    Rules: whitespace is normalized; rows outside the 50 states + DC (e.g. Canadian
    provinces) are dropped; rows with a missing/non-numeric ID or a non-positive price
    are dropped; duplicate ``OPIS Truckstop ID`` rows are merged keeping the **lowest
    price** (same physical stop, and a cost-minimizing driver pays the lowest posted
    price). On a price tie the first row wins, so output is deterministic.
    """
    report = CleaningReport(rows_in=len(rows))
    by_id: dict[int, CleanStation] = {}
    prices_by_id: dict[int, set[Decimal]] = {}

    for row in rows:
        state = clean_text(row.get("State")).upper()
        if state not in US_STATE_CODES:
            report.non_us_dropped[state or "<blank>"] += 1
            continue
        try:
            opis_id = int(clean_text(row.get("OPIS Truckstop ID")))
        except ValueError:
            report.malformed_dropped += 1
            continue
        price = _parse_price(row.get("Retail Price", ""))
        if price is None:
            report.invalid_price_dropped += 1
            continue
        rack = clean_text(row.get("Rack ID"))
        candidate = CleanStation(
            opis_id=opis_id,
            name=clean_text(row.get("Truckstop Name")),
            address=clean_text(row.get("Address")),
            city=clean_text(row.get("City")),
            state=state,
            rack_id=int(rack) if rack.isdigit() else None,
            price=price,
        )
        prices_by_id.setdefault(opis_id, set()).add(price)

        existing = by_id.get(opis_id)
        if existing is None:
            by_id[opis_id] = candidate
            continue
        report.duplicate_rows_merged += 1
        keep = candidate if candidate.price < existing.price else existing
        by_id[opis_id] = CleanStation(
            opis_id=keep.opis_id,
            name=keep.name,
            address=keep.address,
            city=keep.city,
            state=keep.state,
            rack_id=keep.rack_id,
            price=keep.price,
            source_rows=existing.source_rows + 1,
        )

    report.ids_with_conflicting_prices = sum(1 for p in prices_by_id.values() if len(p) > 1)
    stations = sorted(by_id.values(), key=lambda s: s.opis_id)
    report.stations_out = len(stations)
    return stations, report


def read_raw_csv(path: Path) -> list[dict[str, str]]:
    """Read the raw OPIS CSV, validating that the expected columns are present."""
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        missing = set(RAW_COLUMNS) - {clean_text(c) for c in reader.fieldnames or []}
        if missing:
            raise ValueError(f"{path} is missing columns: {sorted(missing)}")
        return [{clean_text(k): v for k, v in row.items()} for row in reader]


def clean_fuel_csv(path: Path) -> tuple[list[CleanStation], CleaningReport]:
    """Read and clean the raw CSV at ``path``."""
    return clean_rows(read_raw_csv(path))
