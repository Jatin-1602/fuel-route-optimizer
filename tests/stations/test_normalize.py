"""Place-name normalization rules."""

import pytest

from apps.stations.services.normalize import (
    census_name_variants,
    compact_key,
    normalize_place_name,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Dallas", "dallas"),
        ("  NEW   YORK ", "new york"),
        ("St. Louis", "st louis"),
        ("Saint Louis", "st louis"),
        ("Fort Worth", "ft worth"),
        ("Ft. Worth", "ft worth"),
        ("Mount Vernon", "mt vernon"),
        ("O'Fallon", "ofallon"),
        ("MC DERMITT", "mcdermitt"),
        ("McDermitt", "mcdermitt"),
        ("Winston-Salem", "winston salem"),
        ("Española", "espanola"),
        ("Middlesboro", "middlesborough"),
        ("Kansas City", "kansas city"),  # "City" here is part of the name, never stripped
    ],
)
def test_normalize_place_name(raw: str, expected: str) -> None:
    assert normalize_place_name(raw) == expected


def test_compact_key_ignores_spaces() -> None:
    assert compact_key(normalize_place_name("De Forest")) == compact_key(
        normalize_place_name("DeForest")
    )


@pytest.mark.parametrize(
    ("raw", "keys"),
    [
        ("Abbeville city", ["abbeville"]),
        ("Abanda CDP", ["abanda"]),
        ("Kansas City city", ["kansas city", "kansas"]),
        ("Carson City", ["carson city"]),  # no lowercase LSAD suffix -> untouched
        ("Redford charter township", ["redford"]),
        ("Autaugaville CCD", ["autaugaville"]),
        ("Indianapolis city (balance)", ["indianapolis"]),
        ("San Buenaventura (Ventura) city", ["san buenaventura", "ventura"]),
        (
            "Nashville-Davidson metropolitan government (balance)",
            ["nashville davidson", "nashville"],
        ),
        (
            "Louisville/Jefferson County metro government (balance)",
            ["louisville jefferson county", "louisville"],
        ),
        ("Macon-Bibb County", ["macon bibb county", "macon"]),
        ("Lexington-Fayette urban county", ["lexington fayette", "lexington"]),
        ("Boise City city", ["boise city", "boise"]),
        ("Bridgewater Town city", ["bridgewater town", "bridgewater"]),
        ("Old Town city", ["old town"]),  # degenerate alias "old" suppressed
        ("Winston-Salem city", ["winston salem"]),  # ordinary hyphen: no alias
        ("District 5", []),
        ("County subdivisions not defined", []),
    ],
)
def test_census_name_variants(raw: str, keys: list[str]) -> None:
    assert [key for key, _, _ in census_name_variants(raw)] == keys


def test_census_variants_flag_aliases_and_keep_display_names() -> None:
    variants = census_name_variants("San Buenaventura (Ventura) city")
    assert variants == [
        ("san buenaventura", "San Buenaventura", False),
        ("ventura", "Ventura", True),
    ]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("S Coffeyville", "south coffeyville"),
        ("N. Little Rock", "north little rock"),
        ("Monroe Twp", "monroe township"),
        ("E", "e"),  # a lone letter is not expanded
    ],
)
def test_abbreviation_expansion(raw: str, expected: str) -> None:
    assert normalize_place_name(raw) == expected
