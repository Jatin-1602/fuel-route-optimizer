# Design decisions

Short ADR-style entries: context → decision → consequences. (Expanded in the documentation phase.)

---

## ADR-001 — Django 6.1 with third-party classifiers that stop at 6.0

**Context.** The assignment requires the latest stable Django (6.1.2 at build time). drf-spectacular 0.30.0, django-environ 0.14.0 and pytest-django 4.14.0 declare Django classifiers only up to 6.0, though none of them pins an upper bound.

**Decision.** Use Django 6.1.2 and verify compatibility instead of assuming it. Swagger UI and the schema are covered by tests, `manage.py spectacular --validate --fail-on-warn` is clean, settings load via django-environ, and the whole suite runs under pytest-django.

**Consequences.** No fallback was needed. If a later release of these libraries breaks, the fallback is Django 6.0.x.

---

## ADR-002 — Offline station geocoding with the US Census Gazetteer (2026 vintage)

**Context.** The CSV has no coordinates, and its addresses are highway exits (`"I-44, EXIT 283 & US-69"`). Geocoding about 6,600 stations through a free API at request time is impossible within the latency and call budget. Doing it even once in a batch is slow and rate-limited.

**Decision.** Geocode each station to its **city centroid** offline, using the public-domain Census Gazetteer *places* and *county subdivisions* files. Both are condensed into `data/us_places.csv`. The same file also resolves `"City, ST"` user input at request time, with zero network calls.

**Notes found at build time:**
- The **2026 Gazetteer files are pipe-delimited (`|`)**, not tab-delimited as earlier vintages are. The parser detects the delimiter from the header line, so either works.
- Census `NAME` values carry lowercase legal/statistical descriptors (`"Kansas City city"`, `"Redford charter township"`, `"Abanda CDP"`). These are stripped **case-sensitively**, so a title-case "City" that is part of a name (`"Carson City"`) is preserved.
- Some places need aliases to match the names people use:
  - Consolidated city-counties: `"Nashville-Davidson metropolitan government (balance)"`, `"Macon-Bibb County"`, `"Lexington-Fayette urban county"`
  - Official names ending in City or Town: `"Boise City city"`, `"Bridgewater Town city"`
  - Parenthetical names: `"San Buenaventura (Ventura) city"`

  Aliases rank below primary names.
- When several records share a (state, name), the best source wins: incorporated place, then CDP, then civil county subdivision, then statistical county subdivision (CCD), with larger land area as the tie-break.

**Consequences.** The match rate is 97.4% of unique US stations. Coordinates are city-level, not the exact exit, and the route-corridor width absorbs that error. The remaining unmatched cities are mostly unincorporated hamlets, big-city neighbourhoods and county-as-city postal names; they are listed in `data/unmatched_cities.csv`.

---

## ADR-003 — Very conservative fuzzy matching (cutoff 0.95, same state only)

**Context.** With a difflib cutoff of 0.88, `"Middleton, MI"` matched `"Middletown, MI"` (score 0.947). These are two different villages about 35 miles apart. `"Grovertown, IN"` → `"Groverton"` (a correct match) has exactly the same score, so no cutoff can separate the two cases.

**Decision.** A wrong coordinate is worse than an excluded station, because a fuel stop could be placed off the route. Lookup order:
1. Exact normalized key, also tried with a trailing "township" removed.
2. Key with spaces removed (`"De Forest"` == `"DeForest"`, `"La Place"` == `"Laplace"`), which covers almost all legitimate near-matches.
3. difflib with cutoff **0.95**, within the same state only.

**Consequences.** Two borderline stations (Grovertown and Middleton) are left unmatched instead of risking a wrong location. They can be recovered with the optional ORS fallback.

---

## ADR-004 — Duplicate OPIS IDs: keep the lowest price

**Context.** 678 OPIS IDs appear more than once (905 extra rows in the US subset). Names sometimes differ (`"PILOT TRAVEL CENTER #1243"` vs `"PILOT #1243"`), and 487 US IDs have conflicting prices. City and state never differ between duplicates.

**Decision.** Treat each ID as one physical stop and keep the **lowest** posted price, since a cost-minimizing driver pays the lowest available price. On a price tie the first row wins, so the output is deterministic. The number of merged rows is kept in `source_rows`.

**Alternative.** Averaging the duplicate prices would be more conservative, and would be a one-line change in `apps/stations/services/cleaning.py`.
