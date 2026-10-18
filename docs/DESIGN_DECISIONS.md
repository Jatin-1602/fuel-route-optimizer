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

**Consequences.** The offline match rate is 97.4% of unique US stations (99.7% with the ORS fallback, ADR-007). Coordinates are city-level, not the exact exit, and the route-corridor width absorbs that error. The remaining unmatched cities are mostly unincorporated hamlets, big-city neighbourhoods and county-as-city postal names; they are listed in `data/unmatched_cities.csv`.

---

## ADR-003 — Very conservative fuzzy matching (cutoff 0.95, same state only)

**Context.** With a difflib cutoff of 0.88, `"Middleton, MI"` matched `"Middletown, MI"` (score 0.947). These are two different villages about 35 miles apart. `"Grovertown, IN"` → `"Groverton"` (a correct match) has exactly the same score, so no cutoff can separate the two cases.

**Decision.** A wrong coordinate is worse than an excluded station, because a fuel stop could be placed off the route. Lookup order:
1. Exact normalized key, also tried with a trailing "township" removed.
2. Key with spaces removed (`"De Forest"` == `"DeForest"`, `"La Place"` == `"Laplace"`), which covers almost all legitimate near-matches.
3. difflib with cutoff **0.95**, within the same state only.

**Consequences.** Two borderline stations (Grovertown and Middleton) are left unmatched instead of risking a wrong location. Both were later recovered correctly by the ORS fallback (ADR-007).

---

## ADR-004 — Duplicate OPIS IDs: keep the lowest price

**Context.** 678 OPIS IDs appear more than once (905 extra rows in the US subset). Names sometimes differ (`"PILOT TRAVEL CENTER #1243"` vs `"PILOT #1243"`), and 487 US IDs have conflicting prices. City and state never differ between duplicates.

**Decision.** Treat each ID as one physical stop and keep the **lowest** posted price, since a cost-minimizing driver pays the lowest available price. On a price tie the first row wins, so the output is deterministic. The number of merged rows is kept in `source_rows`.

**Alternative.** Averaging the duplicate prices would be more conservative, and would be a one-line change in `apps/stations/services/cleaning.py`.

---

## ADR-005 — HeiGIT gateway `api.heigit.org` as the ORS base URL

**Context.** HeiGIT, which operates OpenRouteService, [deprecated `api.openrouteservice.org`](https://ask.openrouteservice.org/t/deprecating-api-openrouteservice-org-in-favour-of-api-heigit-org/7912) on 2026-04-28. The old host has received only 10% of the plan quota since 2026-08-27, and it [shuts down on 2026-11-02 to 06](https://ask.openrouteservice.org/t/reducing-the-quota-of-deprecated-api-api-openrouteservice-org/8013). New paths follow `api.heigit.org/<service>/<version>`.

**Decision.** `ORS_BASE_URL` defaults to `https://api.heigit.org` (no trailing slash), and the client appends the service paths:

| Use | Old | New |
|---|---|---|
| Directions | `/v2/directions/{profile}/geojson` | `/openrouteservice/v2/directions/{profile}/geojson` |
| Geocode (free text) | `/geocode/search` | `/pelias/v1/search` |
| Geocode (structured) | `/geocode/search/structured` | `/pelias/v1/search/structured` |

The key ("Basic Key" from account.heigit.org) is sent in the `Authorization` header. This was verified on 2026-10-18 with one real call to each endpoint: all returned HTTP 200, and without a key they return 401 `"Authorization field missing"`.

**Quotas.** These come from the response headers for this key: directions 2,000/day (`X-Ratelimit-Limit`) and geocoding 3,000/day. The public [restrictions page](https://openrouteservice.org/restrictions/) lists directions at 2,000/day and 40/min, geocoding at 1,000/day and 100/min, and a **6,000 km maximum route distance**. Coast to coast is about 4,500 km, so it fits.

---

## ADR-006 — Offline "is this point in the US?" check for `lat,lon` input

**Context.** Raw coordinates must resolve with 0 external calls, and must be rejected outside the US. Bounding boxes alone are too coarse: Toronto lies inside New York's box. A fixed "within N miles of a US place" rule fails too, because Census places are about 2 miles apart in the Northeast but 40–60 miles apart in rural Nevada.

**Decision.** A point is accepted if it lies inside a padded state bounding box, derived from Gazetteer points with Alaska's Aleutians handled across the antimeridian. It must also lie within max(10 mi, 2.5 × local spacing) of the nearest Census place, where local spacing is that place's distance to its 3rd-nearest neighbour. The lookup is a KD-tree on 3D unit vectors, taking microseconds.

**Evidence.** Accepted: NYC (ratio 1.48), rural NV (0.48), Big Bend (0.36), Anchorage, Honolulu, Key West. Rejected: Toronto (5.0), Montreal (5.4), mid-Lake Michigan (4.7), the Gulf of Mexico (8.1), Monterrey, Mexico City, the open ocean.

**Limitation.** Points within about 10 miles of the land border, such as Windsor ON (5 mi from Detroit) or Tijuana, can pass. Only polygon data or a reverse-geocode call could separate them. `"City, ST"` input naming a Canadian province or "Mexico"/"Canada" is rejected by name with 0 calls.

---

## ADR-007 — ORS fallback for station cities missing from the Census gazetteer

**Context.** 130 (city, state) pairs, 172 stations, were not in the Census data. They are mostly unincorporated hamlets, city neighbourhoods and county-as-city names.

**Decision.** `manage.py geocode_stations --ors-fallback` queries each unique pair **once**, using Pelias structured search, `size=5`, at least 0.7 s apart. It stores the raw answers in the committed `data/ors_geocode_cache.json`. Every run, with or without the flag, applies the cache with 0 calls. Acceptance is recomputed from the cache each time, so changing a rule never costs API calls.

Acceptance rules:
1. The layer is locality-type (`locality` or `localadmin`).
2. The country is USA.
3. The state equals the CSV state.
4. The point lies inside that state's padded bounding box.
5. **Ambiguity:** prefer candidates whose name equals the CSV city; reject if those candidates are more than 25 mi apart.
6. **County collision:** if the CSV "city" is also a county in that state, the result must lie in that county.

Rules 5 and 6 were added after auditing a first run that used only rules 1–4. That run accepted results that were far off:

| Pair | Wrong result | Error |
|---|---|---|
| Antioch, TN | One of **five** Antiochs in Tennessee (the stops are on I-24 in Nashville) | ~110 mi |
| Henrico, VA | A hamlet in Mecklenburg County (the stops are in Henrico County) | ~100 mi |
| Sawyer, MI | K. I. Sawyer AFB in the Upper Peninsula (the right "Sawyer" was result #2) | ~300 mi |

**Result.** 113 of 130 cities accepted (150 stations), giving **6,604 / 6,626 = 99.67%** overall. 17 cities (22 stations) were rejected with reasons:
- 10 ambiguous (including Chesterfield VA: four Chesterfields in Virginia)
- 4 county collisions
- 3 region-only answers (Pelias found no locality)

The reasons are listed in `data/unmatched_cities.csv`.

**Trade-off.** The county rule is conservative. It probably also rejects three correct results: Pendleton KY (in Henry County, on I-71), Edwards IL and Lawrence MS. That costs 3 stations and avoids placing a stop about 100 miles away. Transient ORS errors (19 in the first batch) skip the pair, which is retried on the next run. A 429 stops the batch and keeps the progress made so far.
