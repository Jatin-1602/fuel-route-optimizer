"""Offline geocoding of cleaned stations to city-level coordinates."""

import csv
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from apps.stations.services.cleaning import CleanStation
from apps.stations.services.gazetteer import PlaceIndex, PlaceKind

if TYPE_CHECKING:
    from apps.stations.services.ors_fallback import FallbackDecision

GEOCODED_CSV_FIELDS: tuple[str, ...] = (
    "opis_id",
    "name",
    "address",
    "city",
    "state",
    "rack_id",
    "price",
    "lat",
    "lon",
    "geocode_source",
    "geocode_match",
    "source_rows",
)

UNMATCHED_CSV_FIELDS: tuple[str, ...] = (
    "city",
    "state",
    "station_count",
    "closest_gazetteer_name",
    "similarity",
    "ors_result",
    "ors_reason",
)

_SOURCE_BY_KIND: dict[PlaceKind, str] = {
    PlaceKind.INCORPORATED: "census_place",
    PlaceKind.CDP: "census_place",
    PlaceKind.COUSUB_CIVIL: "census_cousub",
    PlaceKind.COUSUB_STATISTICAL: "census_cousub",
}


@dataclass(frozen=True, slots=True)
class GeocodedStation:
    """A cleaned station plus city-level coordinates."""

    opis_id: int
    name: str
    address: str
    city: str
    state: str
    rack_id: int | None
    price: Decimal
    lat: float
    lon: float
    geocode_source: str  # "census_place" | "census_cousub" | "ors"
    geocode_match: str  # "exact" | "fuzzy"
    source_rows: int = 1


@dataclass(frozen=True, slots=True)
class UnmatchedCity:
    """A (city, state) pair we could not resolve, with a diagnostic nearest name."""

    city: str
    state: str
    station_count: int
    closest_gazetteer_name: str
    similarity: float
    ors_result: str = ""  # label returned by the ORS fallback, if it was queried
    ors_reason: str = ""  # why that result was rejected


@dataclass(slots=True)
class GeocodingReport:
    """Match statistics for :func:`geocode_stations`."""

    stations_in: int = 0
    matched: int = 0
    matched_by: Counter[str] = field(default_factory=Counter)
    unmatched_stations: int = 0
    unmatched_cities: list[UnmatchedCity] = field(default_factory=list)

    @property
    def match_rate(self) -> float:
        """Fraction of stations that received coordinates (0..1)."""
        return self.matched / self.stations_in if self.stations_in else 0.0


def find_unmatched_pairs(stations: list[CleanStation], index: PlaceIndex) -> list[tuple[str, str]]:
    """Unique ``(city, state)`` pairs the offline index cannot resolve, most stations first."""
    counts: Counter[tuple[str, str]] = Counter(
        (s.city, s.state) for s in stations if index.lookup(s.city, s.state) is None
    )
    return [pair for pair, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def _with_coordinates(
    station: CleanStation, lat: float, lon: float, source: str, match: str
) -> GeocodedStation:
    return GeocodedStation(
        **{k: v for k, v in asdict(station).items() if k != "source_rows"},
        lat=lat,
        lon=lon,
        geocode_source=source,
        geocode_match=match,
        source_rows=station.source_rows,
    )


def geocode_stations(
    stations: list[CleanStation],
    index: PlaceIndex,
    fallback: "Mapping[tuple[str, str], FallbackDecision] | None" = None,
) -> tuple[list[GeocodedStation], GeocodingReport]:
    """Attach coordinates to each station; unresolved stations are reported.

    The offline gazetteer is tried first. ``fallback`` optionally maps
    ``(city, state)`` to an ORS fallback decision: accepted ones supply coordinates
    (``geocode_source="ors"``), rejected ones are reported with their reason.
    """
    fallback = fallback or {}
    report = GeocodingReport(stations_in=len(stations))
    geocoded: list[GeocodedStation] = []
    unmatched: dict[tuple[str, str], int] = defaultdict(int)

    for station in stations:
        match = index.lookup(station.city, station.state)
        if match is not None:
            source = _SOURCE_BY_KIND[match.place.kind]
            report.matched_by[f"{source}:{match.method}"] += 1
            geocoded.append(
                _with_coordinates(station, match.place.lat, match.place.lon, source, match.method)
            )
            continue
        decision = fallback.get((station.city, station.state))
        if decision is not None and decision.accepted and decision.result is not None:
            method = "exact" if decision.result.match_type == "exact" else "fuzzy"
            report.matched_by[f"ors:{method}"] += 1
            geocoded.append(
                _with_coordinates(station, decision.result.lat, decision.result.lon, "ors", method)
            )
            continue
        unmatched[(station.city, station.state)] += 1

    report.matched = len(geocoded)
    report.unmatched_stations = sum(unmatched.values())
    for (city, state), count in sorted(unmatched.items(), key=lambda kv: (-kv[1], kv[0])):
        closest = index.closest_name(city, state)
        decision = fallback.get((city, state))
        report.unmatched_cities.append(
            UnmatchedCity(
                city=city,
                state=state,
                station_count=count,
                closest_gazetteer_name=closest[0] if closest else "",
                similarity=round(closest[1], 3) if closest else 0.0,
                ors_result=decision.result.label if decision and decision.result else "",
                ors_reason=decision.reason if decision else "",
            )
        )
    return geocoded, report


def write_geocoded_csv(stations: list[GeocodedStation], path: Path) -> None:
    """Write geocoded stations (the committed ``data/stations_geocoded.csv``)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=GEOCODED_CSV_FIELDS)
        writer.writeheader()
        for s in stations:
            row = asdict(s)
            row["rack_id"] = "" if s.rack_id is None else s.rack_id
            row["lat"] = f"{s.lat:.6f}"
            row["lon"] = f"{s.lon:.6f}"
            writer.writerow(row)


def read_geocoded_csv(path: Path) -> list[GeocodedStation]:
    """Read ``data/stations_geocoded.csv`` back into dataclasses."""
    with path.open(encoding="utf-8", newline="") as f:
        return [
            GeocodedStation(
                opis_id=int(row["opis_id"]),
                name=row["name"],
                address=row["address"],
                city=row["city"],
                state=row["state"],
                rack_id=int(row["rack_id"]) if row["rack_id"] else None,
                price=Decimal(row["price"]),
                lat=float(row["lat"]),
                lon=float(row["lon"]),
                geocode_source=row["geocode_source"],
                geocode_match=row["geocode_match"],
                source_rows=int(row["source_rows"]),
            )
            for row in csv.DictReader(f)
        ]


def write_unmatched_csv(cities: list[UnmatchedCity], path: Path) -> None:
    """Write the unmatched (city, state) diagnostics to ``data/unmatched_cities.csv``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=UNMATCHED_CSV_FIELDS)
        writer.writeheader()
        for c in cities:
            writer.writerow(asdict(c))
