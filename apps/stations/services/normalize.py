"""Place-name normalization shared by the Census gazetteer, the station CSV and user input.

Two entry points:

* :func:`normalize_place_name` — canonical matching key for any free-form name
  (``"St. Louis"``, ``"SAINT LOUIS"`` and ``"st louis"`` all become ``"st louis"``).
* :func:`census_name_variants` — turns a raw Census ``NAME`` such as
  ``"San Buenaventura (Ventura) city"`` into one or more ``(key, display_name)`` pairs.
"""

import re
import unicodedata

# Word-level synonyms, mapped to a single canonical (short) form.
_TOKEN_SYNONYMS: dict[str, str] = {
    "saint": "st",
    "sainte": "ste",
    "fort": "ft",
    "mount": "mt",
    "mountain": "mtn",
    "twp": "township",
}
_DIRECTIONS: dict[str, str] = {"n": "north", "s": "south", "e": "east", "w": "west"}

_DROP_CHARS = re.compile("[.'\u2019`]")  # "St." -> "St", "O'Fallon" -> "OFallon"
_SPLIT_CHARS = re.compile(r"[^a-z0-9 ]+")  # hyphens, slashes, commas, parentheses, "&" ...
_SPACES = re.compile(r"\s+")

# Census legal/statistical area descriptors appended to NAME. They are lowercase (or
# all-caps abbreviations) in the Census files, so stripping them *case-sensitively*
# keeps genuine name parts such as "Carson City" or "Kansas City" intact.
_CENSUS_SUFFIXES: tuple[str, ...] = (
    "city and borough",
    "charter township",
    "urban county",
    "zona urbana",
    "comunidad",
    "municipality",
    "corporation",
    "plantation",
    "township",
    "borough",
    "village",
    "city",
    "town",
    "CDP",
    "CCD",
    "UT",
)
_CENSUS_SUFFIX_RE = re.compile(r"\s+(?:" + "|".join(map(re.escape, _CENSUS_SUFFIXES)) + r")$")
_GOVERNMENT_RE = re.compile(
    r"\s+(?:unified|consolidated|metropolitan|metro|urban county)?\s*government$"
)
_PARENTHETICAL_RE = re.compile(r"^(?P<main>.*?)\s*\((?P<alias>[^)]*)\)\s*(?P<rest>.*)$")
# Statistical areas that are not a named place at all.
_NOT_A_PLACE_RE = re.compile(r"(?:subdivisions? not defined|census subarea)$")


def normalize_place_name(name: str) -> str:
    """Return the canonical matching key for a place name.

    Lowercases, strips accents and punctuation, collapses whitespace, and unifies
    common abbreviations (saint/st, fort/ft, mount/mt, "mc donald"/"mcdonald", a
    leading N/S/E/W).
    """
    text = unicodedata.normalize("NFKD", name)
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    text = _DROP_CHARS.sub("", text)
    text = _SPLIT_CHARS.sub(" ", text)
    tokens = [_TOKEN_SYNONYMS.get(t, t) for t in _SPACES.split(text.strip()) if t]
    # Leading directional abbreviation: "S Coffeyville" -> "south coffeyville"
    if len(tokens) > 1 and tokens[0] in _DIRECTIONS:
        tokens[0] = _DIRECTIONS[tokens[0]]

    # "Middlesboro" -> "middlesborough" (Census uses the long spelling)
    tokens = [t[: -len("boro")] + "borough" if t.endswith("boro") else t for t in tokens]

    merged: list[str] = []
    i = 0
    while i < len(tokens):
        # "mc dermitt" -> "mcdermitt" (the CSV sometimes splits Mc/Mac prefixes)
        if tokens[i] == "mc" and i + 1 < len(tokens):
            merged.append("mc" + tokens[i + 1])
            i += 2
            continue
        merged.append(tokens[i])
        i += 1
    return " ".join(merged)


def compact_key(key: str) -> str:
    """Key with spaces removed, so ``"de forest"`` and ``"deforest"`` compare equal."""
    return key.replace(" ", "")


def _strip_census_suffixes(name: str) -> str:
    """Remove trailing Census area descriptors (repeatedly, e.g. ``"X city (balance)"``)."""
    previous = None
    while previous != name:
        previous = name
        name = name.replace("(balance)", "").strip()
        name = _GOVERNMENT_RE.sub("", name).strip()
        name = _CENSUS_SUFFIX_RE.sub("", name).strip()
    return name


def census_name_variants(raw_name: str) -> list[tuple[str, str, bool]]:
    """Expand a raw Census ``NAME`` into ``(key, display_name, is_alias)`` triples.

    * ``"Abbeville city"`` -> ``[("abbeville", "Abbeville", False)]``
    * ``"San Buenaventura (Ventura) city"`` -> primary ``san buenaventura`` + alias ``ventura``
    * ``"Nashville-Davidson metropolitan government (balance)"`` -> primary
      ``nashville davidson`` + alias ``nashville`` (consolidated city-counties such as
      ``"Macon-Bibb County"`` or ``"Lexington-Fayette urban county"`` likewise)
    * ``"Boise City city"`` -> ``boise city`` + alias ``boise``
    * ``"Bridgewater Town city"`` -> ``bridgewater town`` + alias ``bridgewater``

    Returns an empty list for statistical areas that are not named places
    (``"District 5"``, ``"County subdivisions not defined"``).
    """
    if _NOT_A_PLACE_RE.search(raw_name):
        return []
    is_consolidated = (
        "government" in raw_name
        or "(balance)" in raw_name
        or raw_name.endswith((" urban county", " County"))
    )
    # The official name itself ends in "City"/"Town" (followed by the lowercase LSAD).
    official_suffix = re.search(r" (City|Town) (?:city|town|CDP|township)$", raw_name)
    name = _strip_census_suffixes(raw_name)

    displays: list[tuple[str, bool]] = []
    match = _PARENTHETICAL_RE.match(name)
    if match:
        main = _strip_census_suffixes(f"{match['main']} {match['rest']}".strip())
        displays.append((main, False))
        displays.append((_strip_census_suffixes(match["alias"]), True))
    else:
        displays.append((name, False))

    if is_consolidated:
        head = re.split(r"[-/]", displays[0][0], maxsplit=1)[0].strip()
        displays.append((head, True))
    if official_suffix:
        head = displays[0][0].removesuffix(f" {official_suffix[1]}").strip()
        if len(head) > 3:  # skip degenerate aliases such as "Old" from "Old Town"
            displays.append((head, True))

    variants: list[tuple[str, str, bool]] = []
    seen: set[str] = set()
    for display, is_alias in displays:
        key = normalize_place_name(display)
        if not key or key in seen or any(ch.isdigit() for ch in key):
            continue
        seen.add(key)
        variants.append((key, display, is_alias))
    return variants
