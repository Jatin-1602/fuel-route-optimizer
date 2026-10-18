"""Census Gazetteer parsing, condensation and offline lookup."""

from pathlib import Path

import pytest
from django.conf import settings

from apps.stations.services.gazetteer import (
    GazetteerRecord,
    Place,
    PlaceIndex,
    PlaceKind,
    build_places,
    read_gazetteer,
    read_places_csv,
    write_places_csv,
)

PLACE_HEADER = (
    "USPS|GEOID|GEOIDFQ|ANSICODE|NAME|LSAD|FUNCSTAT|ALAND|AWATER|ALAND_SQMI|AWATER_SQMI"
    "|INTPTLAT|INTPTLONG"
)


def _record(
    state: str, name: str, kind: PlaceKind, area: float = 1.0, lat: float = 1.0
) -> GazetteerRecord:
    return GazetteerRecord(state=state, name=name, lat=lat, lon=-lat, land_area=area, kind=kind)


def test_read_gazetteer_pipe_delimited_skips_non_states(tmp_path: Path) -> None:
    path = tmp_path / "2026_Gaz_place_national.txt"
    path.write_text(
        PLACE_HEADER
        + "\nTX|1|x|y|Dallas city|25|A|100|0|1|0|32.79|-96.76"
        + "\nTX|2|x|y|Cloverleaf CDP|57|S|50|0|1|0|29.78|-95.17"
        + "\nPR|3|x|y|San Juan zona urbana|62|S|10|0|1|0|18.4|-66.0\n",
        encoding="utf-8",
    )
    records = list(read_gazetteer(path))

    assert [(r.state, r.name, r.kind) for r in records] == [
        ("TX", "Dallas city", PlaceKind.INCORPORATED),
        ("TX", "Cloverleaf CDP", PlaceKind.CDP),
    ]
    assert records[0].lat == pytest.approx(32.79)
    assert records[0].lon == pytest.approx(-96.76)


def test_read_gazetteer_tab_delimited_with_trailing_header_whitespace(tmp_path: Path) -> None:
    path = tmp_path / "2024_Gaz_cousubs_national.txt"
    header = "USPS\tGEOID\tNAME\tALAND\tINTPTLAT\tINTPTLONG                    "
    path.write_text(
        header
        + "\nMA\t1\tBridgewater Town city\t5\t41.98\t-70.97"
        + "\nGA\t2\tMacon East CCD\t9\t32.8\t-83.5\n",
        encoding="utf-8",
    )
    records = list(read_gazetteer(path))

    assert [(r.name, r.kind) for r in records] == [
        ("Bridgewater Town city", PlaceKind.COUSUB_CIVIL),
        ("Macon East CCD", PlaceKind.COUSUB_STATISTICAL),
    ]
    assert records[0].lon == pytest.approx(-70.97)


def test_build_places_prefers_incorporated_over_cdp_and_cousub() -> None:
    places = build_places(
        [
            _record("OH", "Springfield township", PlaceKind.COUSUB_CIVIL, area=999, lat=3),
            _record("OH", "Springfield CDP", PlaceKind.CDP, area=500, lat=2),
            _record("OH", "Springfield city", PlaceKind.INCORPORATED, area=10, lat=1),
        ]
    )
    assert len(places) == 1
    assert places[0].lat == 1
    assert places[0].kind is PlaceKind.INCORPORATED


def test_build_places_prefers_primary_name_over_alias() -> None:
    places = build_places(
        [
            _record("CA", "San Buenaventura (Ventura) city", PlaceKind.INCORPORATED, area=1, lat=1),
            _record("CA", "Ventura city", PlaceKind.INCORPORATED, area=0.1, lat=2),
        ]
    )
    ventura = next(p for p in places if p.normalized_name == "ventura")
    assert ventura.lat == 2


def test_build_places_tie_breaks_on_land_area() -> None:
    places = build_places(
        [
            _record("WI", "Lincoln town", PlaceKind.COUSUB_CIVIL, area=5, lat=1),
            _record("WI", "Lincoln town", PlaceKind.COUSUB_CIVIL, area=50, lat=2),
        ]
    )
    assert [p.lat for p in places] == [2]


def test_places_csv_roundtrip(tmp_path: Path) -> None:
    places = [Place("TX", "dallas", "Dallas", 32.793333, -96.766513, PlaceKind.INCORPORATED)]
    path = tmp_path / "us_places.csv"
    assert write_places_csv(places, path) == 1
    assert read_places_csv(path) == places


@pytest.fixture
def index() -> PlaceIndex:
    return PlaceIndex(
        [
            Place("TX", "dallas", "Dallas", 32.79, -96.77, PlaceKind.INCORPORATED),
            Place("WI", "deforest", "DeForest", 43.24, -89.34, PlaceKind.INCORPORATED),
            Place("MI", "middletown", "Middletown", 42.99, -84.14, PlaceKind.CDP),
            Place("OK", "big cabin", "Big Cabin", 36.54, -95.23, PlaceKind.INCORPORATED),
            Place("NY", "albuquerqueville", "Albuquerqueville", 1.0, 1.0, PlaceKind.CDP),
        ]
    )


def test_lookup_exact_is_case_and_punctuation_insensitive(index: PlaceIndex) -> None:
    match = index.lookup("  DALLAS ", "tx")
    assert match is not None
    assert (match.method, match.place.display_name) == ("exact", "Dallas")


def test_lookup_compact_ignores_spaces(index: PlaceIndex) -> None:
    match = index.lookup("De Forest", "WI")
    assert match is not None
    assert match.method == "compact"


def test_lookup_fuzzy_accepts_single_typo_in_long_name(index: PlaceIndex) -> None:
    match = index.lookup("Albuquerquevile", "NY")
    assert match is not None
    assert match.method == "fuzzy"
    assert match.score >= index.fuzzy_cutoff


def test_lookup_fuzzy_rejects_similar_but_different_town(index: PlaceIndex) -> None:
    # Middleton, MI and Middletown, MI are different villages ~35 miles apart.
    assert index.lookup("Middleton", "MI") is None


def test_lookup_strips_trailing_township_from_input() -> None:
    index = PlaceIndex([Place("NJ", "monroe", "Monroe", 40.3, -74.4, PlaceKind.COUSUB_CIVIL)])
    match = index.lookup("Monroe Township", "NJ")
    assert match is not None
    assert match.method == "exact"


def test_lookup_never_crosses_states(index: PlaceIndex) -> None:
    assert index.lookup("Dallas", "OK") is None
    assert index.lookup("Big Cabin", "TX") is None


def test_lookup_without_fuzzy(index: PlaceIndex) -> None:
    assert index.lookup("Albuquerquevile", "NY", fuzzy=False) is None


def test_committed_us_places_csv_resolves_known_cities() -> None:
    index = PlaceIndex.from_csv(settings.US_PLACES_CSV)
    assert len(index) > 40_000
    for city, state in [
        ("New York", "NY"),
        ("Los Angeles", "CA"),
        ("Nashville", "TN"),
        ("Boise", "ID"),
    ]:
        assert index.lookup(city, state) is not None, (city, state)
