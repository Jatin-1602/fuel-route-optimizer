"""Cleaning and de-duplication of the raw fuel-prices CSV."""

from decimal import Decimal
from pathlib import Path

import pytest
from django.conf import settings

from apps.stations.services.cleaning import clean_fuel_csv, clean_rows, clean_text, read_raw_csv


def _row(
    opis_id: str = "1", price: str = "3.50", state: str = "TX", **overrides: str
) -> dict[str, str]:
    row = {
        "OPIS Truckstop ID": opis_id,
        "Truckstop Name": "PILOT #1",
        "Address": "I-35, EXIT 1",
        "City": "Dallas",
        "State": state,
        "Rack ID": "100",
        "Retail Price": price,
    }
    row.update(overrides)
    return row


def test_clean_text_trims_and_collapses_whitespace() -> None:
    assert clean_text("  I-35,  EXIT   271 ") == "I-35, EXIT 271"
    assert clean_text(None) == ""


def test_whitespace_is_normalized_in_every_field() -> None:
    stations, _ = clean_rows([_row(Address=" I-35,  EXIT 271 ", City=" Dallas  ", State=" tx ")])
    assert stations[0].address == "I-35, EXIT 271"
    assert stations[0].city == "Dallas"
    assert stations[0].state == "TX"


def test_canadian_and_non_state_rows_are_excluded() -> None:
    rows = [
        _row("1"),
        _row("2", state="ON"),
        _row("3", state="BC"),
        _row("4", state="PR"),
        _row("5", state=""),
    ]
    stations, report = clean_rows(rows)

    assert [s.opis_id for s in stations] == [1]
    assert report.non_us_dropped == {"ON": 1, "BC": 1, "PR": 1, "<blank>": 1}


def test_duplicate_ids_keep_lowest_price_and_count_rows() -> None:
    rows = [
        _row("20", "3.899", **{"Truckstop Name": "PILOT TRAVEL CENTER #1243"}),
        _row("20", "3.799", **{"Truckstop Name": "PILOT #1243"}),
        _row("20", "3.999"),
    ]
    stations, report = clean_rows(rows)

    assert len(stations) == 1
    assert stations[0].price == Decimal("3.799")
    assert stations[0].name == "PILOT #1243"
    assert stations[0].source_rows == 3
    assert report.duplicate_rows_merged == 2
    assert report.ids_with_conflicting_prices == 1


def test_duplicate_price_tie_keeps_first_row() -> None:
    rows = [
        _row("7", "3.00", **{"Truckstop Name": "FIRST"}),
        _row("7", "3.00", **{"Truckstop Name": "SECOND"}),
    ]
    stations, report = clean_rows(rows)
    assert stations[0].name == "FIRST"
    assert report.ids_with_conflicting_prices == 0


@pytest.mark.parametrize("price", ["", "abc", "0", "-1.5", "NaN"])
def test_invalid_prices_are_dropped(price: str) -> None:
    stations, report = clean_rows([_row(price=price)])
    assert stations == []
    assert report.invalid_price_dropped == 1


def test_non_numeric_id_is_dropped() -> None:
    stations, report = clean_rows([_row(opis_id="X12")])
    assert stations == []
    assert report.malformed_dropped == 1


def test_output_sorted_by_id_and_rack_optional() -> None:
    stations, _ = clean_rows([_row("9"), _row("2", **{"Rack ID": ""})])
    assert [s.opis_id for s in stations] == [2, 9]
    assert stations[0].rack_id is None


def test_missing_columns_raise(tmp_path: Path) -> None:
    path = tmp_path / "bad.csv"
    path.write_text("OPIS Truckstop ID,City\n1,Dallas\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing columns"):
        read_raw_csv(path)


def test_real_csv_statistics() -> None:
    """Regression guard on the provided file (numbers documented in DATA_PIPELINE.md)."""
    stations, report = clean_fuel_csv(settings.FUEL_PRICES_CSV)

    assert report.rows_in == 8151
    assert sum(report.non_us_dropped.values()) == 620
    assert set(report.non_us_dropped) == {"AB", "BC", "MB", "NB", "NS", "ON", "QC", "SK", "YT"}
    assert report.stations_out == len(stations) == 6626
    assert len({s.opis_id for s in stations}) == len(stations)
    assert min(s.price for s in stations) >= Decimal("2.687")
