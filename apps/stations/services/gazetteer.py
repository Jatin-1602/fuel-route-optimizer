"""US Census Gazetteer download, parsing, and the offline place index.

The Census Bureau publishes, per year, national files of *places* (incorporated cities,
towns, villages and census-designated places) and *county subdivisions* (townships,
New England towns, CCDs) with an internal-point latitude/longitude. They are public
domain. We condense them into ``data/us_places.csv`` — one row per (state, name) —
which powers both station geocoding and request-time "City, ST" lookup with zero
network calls.
"""

import csv
import difflib
import io
import logging
import zipfile
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path

import requests

from apps.stations.services.normalize import (
    census_name_variants,
    compact_key,
    normalize_place_name,
)
from apps.stations.us_states import US_STATE_CODES

logger = logging.getLogger(__name__)

GAZETTEER_URL = (
    "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/"
    "{year}_Gazetteer/{year}_Gaz_{kind}_national.zip"
)
GAZETTEER_KINDS: tuple[str, ...] = ("place", "cousubs")
COUNTY_KIND = "counties"

# difflib ratio required for a fuzzy match (same state only). Deliberately very high:
# e.g. "Middleton, MI" vs "Middletown, MI" score 0.947 but are ~35 miles apart, and a
# wrong coordinate is worse than an excluded station.
FUZZY_CUTOFF = 0.95
# Minimum similarity for the "closest name" hint in the unmatched-cities report.
DIAGNOSTIC_FLOOR = 0.6

PLACES_CSV_FIELDS = ("state", "normalized_name", "display_name", "lat", "lon", "kind")
COUNTIES_CSV_FIELDS = ("state", "normalized_name", "display_name")
# County-equivalents that people use as a "city" name. Virginia's independent cities
# ("Richmond city") and Connecticut planning regions are deliberately excluded.
_COUNTY_SUFFIXES: tuple[str, ...] = (" County", " Parish", " Borough", " Census Area")


class PlaceKind(IntEnum):
    """Source quality ranking; lower wins when two records share a (state, name) key."""

    INCORPORATED = 0  # city / town / village / borough
    CDP = 1  # census-designated place (unincorporated community)
    COUSUB_CIVIL = 2  # township / New England town
    COUSUB_STATISTICAL = 3  # CCD, unorganized territory: large areas, coarse centroid


@dataclass(frozen=True, slots=True)
class GazetteerRecord:
    """One raw row from a Census Gazetteer file."""

    state: str
    name: str
    lat: float
    lon: float
    land_area: float
    kind: PlaceKind


@dataclass(frozen=True, slots=True)
class Place:
    """One entry of the condensed offline gazetteer."""

    state: str
    normalized_name: str
    display_name: str
    lat: float
    lon: float
    kind: PlaceKind


@dataclass(frozen=True, slots=True)
class PlaceMatch:
    """Result of a :meth:`PlaceIndex.lookup`."""

    place: Place
    method: str  # "exact" | "compact" | "fuzzy"
    score: float


# --- Download & parse -----------------------------------------------------------------


def download_gazetteer(
    year: int, dest_dir: Path, kinds: Iterable[str] = GAZETTEER_KINDS, timeout: float = 60.0
) -> list[Path]:
    """Download and unzip national Gazetteer files (places + county subdivisions by default).

    Returns the paths of the extracted ``.txt`` files. Files already present are reused.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for kind in kinds:
        target = dest_dir / f"{year}_Gaz_{kind}_national.txt"
        if not target.exists():
            url = GAZETTEER_URL.format(year=year, kind=kind)
            logger.info("Downloading %s", url)
            response = requests.get(url, timeout=timeout)
            response.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                member = next(n for n in archive.namelist() if n.endswith(".txt"))
                target.write_bytes(archive.read(member))
        paths.append(target)
    return paths


def _classify(path_kind: str, raw_name: str) -> PlaceKind:
    if path_kind == "place":
        return PlaceKind.CDP if raw_name.endswith(" CDP") else PlaceKind.INCORPORATED
    if raw_name.endswith((" CCD", " UT")) or "precinct" in raw_name or "district" in raw_name:
        return PlaceKind.COUSUB_STATISTICAL
    return PlaceKind.COUSUB_CIVIL


def _read_rows(path: Path) -> Iterator[dict[str, str]]:
    """Rows of a Gazetteer file; delimiter detected (tab before 2026, pipe in 2026)."""
    with path.open(encoding="utf-8", newline="") as f:
        header = f.readline()
        delimiter = "|" if "|" in header else "\t"
        fields = [h.strip() for h in header.rstrip("\r\n").split(delimiter)]
        yield from csv.DictReader(f, fieldnames=fields, delimiter=delimiter)


def read_gazetteer(path: Path) -> Iterator[GazetteerRecord]:
    """Yield records from a Gazetteer ``.txt`` file (50 states + DC only).

    The delimiter is detected from the header: older vintages are tab-separated, the
    2026 vintage is pipe-separated. Header names are stripped because older files have
    trailing whitespace on the last column.
    """
    path_kind = "place" if "_place_" in path.name else "cousubs"
    for row in _read_rows(path):
        state = row["USPS"].strip()
        if state not in US_STATE_CODES:
            continue
        name = row["NAME"].strip()
        yield GazetteerRecord(
            state=state,
            name=name,
            lat=float(row["INTPTLAT"]),
            lon=float(row["INTPTLONG"]),
            land_area=float(row["ALAND"] or 0),
            kind=_classify(path_kind, name),
        )


def read_counties(path: Path) -> list[tuple[str, str, str]]:
    """``(state, normalized_name, display_name)`` for every county-equivalent in a state.

    ``"Henrico County"`` -> ``("VA", "henrico", "Henrico County")``.
    """
    counties: set[tuple[str, str, str]] = set()
    for row in _read_rows(path):
        state, name = row["USPS"].strip(), row["NAME"].strip()
        if state not in US_STATE_CODES:
            continue
        for suffix in _COUNTY_SUFFIXES:
            if name.endswith(suffix):
                counties.add((state, normalize_place_name(name.removesuffix(suffix)), name))
                break
    return sorted(counties)


def write_counties_csv(counties: Iterable[tuple[str, str, str]], path: Path) -> int:
    """Write ``data/us_counties.csv``; returns the number of rows."""
    rows = list(counties)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(COUNTIES_CSV_FIELDS)
        writer.writerows(rows)
    return len(rows)


def read_county_keys(path: Path) -> frozenset[tuple[str, str]]:
    """``{(state, normalized_county_name)}`` from ``data/us_counties.csv``."""
    with path.open(encoding="utf-8", newline="") as f:
        return frozenset((r["state"], r["normalized_name"]) for r in csv.DictReader(f))


def build_places(records: Iterable[GazetteerRecord]) -> list[Place]:
    """Condense raw records into one :class:`Place` per (state, normalized name).

    When several records share a key, prefer (in order): better :class:`PlaceKind`,
    a primary name over an alias, then the larger land area (a proxy for the "main"
    town of that name).
    """
    best: dict[tuple[str, str], tuple[tuple[int, bool, float], Place]] = {}
    for record in records:
        for key, display, is_alias in census_name_variants(record.name):
            rank = (int(record.kind), is_alias, -record.land_area)
            current = best.get((record.state, key))
            if current is None or rank < current[0]:
                best[(record.state, key)] = (
                    rank,
                    Place(record.state, key, display, record.lat, record.lon, record.kind),
                )
    return sorted((p for _, p in best.values()), key=lambda p: (p.state, p.normalized_name))


def write_places_csv(places: Iterable[Place], path: Path) -> int:
    """Write places to ``path``; returns the number of rows written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(PLACES_CSV_FIELDS)
        for p in places:
            writer.writerow(
                [
                    p.state,
                    p.normalized_name,
                    p.display_name,
                    f"{p.lat:.6f}",
                    f"{p.lon:.6f}",
                    int(p.kind),
                ]
            )
            count += 1
    return count


def read_places_csv(path: Path) -> list[Place]:
    """Load the condensed gazetteer written by :func:`write_places_csv`."""
    with path.open(encoding="utf-8", newline="") as f:
        return [
            Place(
                state=row["state"],
                normalized_name=row["normalized_name"],
                display_name=row["display_name"],
                lat=float(row["lat"]),
                lon=float(row["lon"]),
                kind=PlaceKind(int(row["kind"])),
            )
            for row in csv.DictReader(f)
        ]


# --- Lookup ---------------------------------------------------------------------------


class PlaceIndex:
    """In-memory (state, name) -> coordinates lookup with a conservative fuzzy fallback."""

    def __init__(self, places: Iterable[Place], fuzzy_cutoff: float = FUZZY_CUTOFF) -> None:
        self._exact: dict[tuple[str, str], Place] = {}
        self._compact: dict[tuple[str, str], Place] = {}
        self._names_by_state: dict[str, list[str]] = defaultdict(list)
        for place in places:
            self._exact[(place.state, place.normalized_name)] = place
            self._compact.setdefault((place.state, compact_key(place.normalized_name)), place)
            self._names_by_state[place.state].append(place.normalized_name)
        self.fuzzy_cutoff = fuzzy_cutoff

    def __len__(self) -> int:
        return len(self._exact)

    @classmethod
    def from_csv(cls, path: Path, fuzzy_cutoff: float = FUZZY_CUTOFF) -> "PlaceIndex":
        """Build an index from ``data/us_places.csv``."""
        return cls(read_places_csv(path), fuzzy_cutoff=fuzzy_cutoff)

    def lookup(self, city: str, state: str, *, fuzzy: bool = True) -> PlaceMatch | None:
        """Resolve ``city`` within ``state`` (2-letter code).

        Tries, in order: exact key (also with a trailing "township" removed, as in
        "Monroe Township"), key with spaces removed ("De Forest" == "DeForest"), then a
        difflib fuzzy match. Matching never crosses state lines.
        """
        state = state.strip().upper()
        key = normalize_place_name(city)
        for candidate in (key, key.removesuffix(" township")):
            place = self._exact.get((state, candidate))
            if place is not None:
                return PlaceMatch(place, "exact", 1.0)
        place = self._compact.get((state, compact_key(key)))
        if place is not None:
            return PlaceMatch(place, "compact", 1.0)
        if not fuzzy or not key:
            return None
        candidates = difflib.get_close_matches(
            key, self._names_by_state.get(state, []), n=1, cutoff=self.fuzzy_cutoff
        )
        if not candidates:
            return None
        score = difflib.SequenceMatcher(None, key, candidates[0]).ratio()
        return PlaceMatch(self._exact[(state, candidates[0])], "fuzzy", score)

    def closest_name(
        self, city: str, state: str, floor: float = DIAGNOSTIC_FLOOR
    ) -> tuple[str, float] | None:
        """Best candidate name in ``state`` scoring at least ``floor`` (diagnostics only).

        A non-zero floor lets difflib's cheap pre-filters skip most candidates
        (``cutoff=0`` would run a full comparison against every name in the state).
        """
        key = normalize_place_name(city)
        candidates = difflib.get_close_matches(
            key, self._names_by_state.get(state.strip().upper(), []), n=1, cutoff=floor
        )
        if not candidates:
            return None
        return candidates[0], difflib.SequenceMatcher(None, key, candidates[0]).ratio()


# --- State bounding boxes -------------------------------------------------------------

# Padding added around the hull of a state's Gazetteer points. County subdivisions tile
# each state, so their internal points span nearly its full extent; 0.25 deg (~17 mi)
# covers the remaining margin to the real border.
STATE_BOUNDS_PADDING_DEG = 0.25


def wrap_longitude(state: str, lon: float) -> float:
    """Alaska's Aleutians cross the antimeridian: express them as lon < -180 for AK."""
    return lon - 360.0 if state == "AK" and lon > 0 else lon


@dataclass(frozen=True, slots=True)
class StateBounds:
    """Approximate (padded) bounding box of a state."""

    state: str
    min_lat: float
    max_lat: float
    min_lon: float
    max_lon: float

    def contains(self, lat: float, lon: float) -> bool:
        """Whether the point lies inside this box (antimeridian-aware for Alaska)."""
        lon = wrap_longitude(self.state, lon)
        return self.min_lat <= lat <= self.max_lat and self.min_lon <= lon <= self.max_lon


def compute_state_bounds(
    places: Iterable[Place], padding_deg: float = STATE_BOUNDS_PADDING_DEG
) -> dict[str, StateBounds]:
    """Derive a padded bounding box per state from gazetteer points."""
    lats: dict[str, list[float]] = defaultdict(list)
    lons: dict[str, list[float]] = defaultdict(list)
    for p in places:
        lats[p.state].append(p.lat)
        lons[p.state].append(wrap_longitude(p.state, p.lon))
    return {
        state: StateBounds(
            state=state,
            min_lat=min(lats[state]) - padding_deg,
            max_lat=max(lats[state]) + padding_deg,
            min_lon=min(lons[state]) - padding_deg,
            max_lon=max(lons[state]) + padding_deg,
        )
        for state in lats
    }
