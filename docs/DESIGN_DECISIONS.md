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

**Timeouts and retries:** see ADR-009.

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

---

## ADR-008 — Stop penalty: optimize "cost effective", not just "cheapest"

**Context.** The assignment asks for the "optimal location to fuel up along the route — **optimal mostly means cost effective** based on fuel prices". The pure cost optimum (the greedy, ADR in `docs/ALGORITHM.md`) is provably the cheapest, but on NYC → LA it makes **19 stops**, several of them 1–3 gallon top-ups that save a few cents because the next station is marginally cheaper. No driver would do that. "Mostly" leaves room to weigh the cost of stopping itself.

**Decision.** A `stop_penalty` parameter (USD per stop, **default 15**, range 0–200) is added to the objective: minimise *fuel cost + stop_penalty × number of stops*. The penalty is a planning weight; it is **not** added to `total_fuel_cost`.

- `stop_penalty = 0` runs the original greedy unchanged: fast and provably optimal for pure cost.
- `stop_penalty > 0` runs an **exact DP**. It relies on the known structure of optimal plans: at every stop you either fill up or buy just enough to arrive empty at the next stop (or the destination). So fuel on arrival is one of finitely many values: 0, `C − d(j, l)` for an earlier station *j* that filled up, or the start fuel minus the distance driven. Each station is processed with a handful of vectorised numpy operations: the fill-up state collapses to one number per station, and the just-enough transitions use a prefix minimum over states sorted by fuel. That makes it O(n·k) over pre-merged stations within range. The DP returns the optimal *set* of stops, and the greedy restricted to that set computes the purchases, so the accounting code is shared.
- The response exposes the trade-off: `summary.pure_cost_optimum` (cost and stop count at penalty 0) and `summary.extra_cost_vs_pure_optimum`.

**Evidence** (real recorded routes, 500 mi range, 10 mpg, empty start):

| Route | Pure optimum | Penalty $15 | Extra fuel cost | Largest leg | DP time |
|---|---|---|---|---|---|
| NYC → LA (2,811.6 mi) | 19 stops, $852.87 | **7 stops**, $860.57 | **+$7.69** | 482.5 mi | ~35 ms |
| Chicago → Houston (1,081.8 mi) | 8 stops, $317.48 | **3 stops**, $319.53 | **+$2.05** | 475.5 mi | ~13 ms |

**Verification.** The brute-force test minimises *fuel + penalty × stops* over 0.1-gallon fuel levels, with no structural shortcuts. It covers all three `start_fuel` modes (300 random instances each, penalties 0–40) and matches the optimizer's objective exactly. It also asserts that a penalty never increases the number of stops and never lowers fuel cost below the pure optimum.

**Why $15.** Roughly the value of 10–15 minutes of a driver's time. At this default, NYC → LA pays $7.69 more to make 12 fewer stops, so each skipped stop costs about $0.64. Set `stop_penalty=0` for the strict cheapest plan.

---

## ADR-009 — Timeouts and retries for OpenRouteService

**Context.** Testing on 2026-10-18 against the live API:
- A cached long route returns in about 1–2 s (NYC → LA: 1.5–2.1 s).
- ORS's **first computation** of a long route regularly took **more than 10 s**:
  - In the manual run, one NYC → LA request hung past a 20 s timeout.
  - In the runserver benchmark, NYC → LA cold failed 3 of 3 times with a 10 s timeout plus one retry.
  - In the gunicorn benchmark, NYC → LA and Chicago → Houston cold each failed 2 of 3.
- The identical request seconds later returned in about 1–2 s, which is consistent with ORS caching the route once it finishes computing it.
- Separately, the HeiGIT gateway briefly returned bursts of HTTP 502.

**Decision.**
- Connect timeout **5 s** for everything.
- Read timeout **30 s for directions** and **10 s for geocoding** (geocoding was always fast). Settings: `ORS_DIRECTIONS_READ_TIMEOUT_SECONDS`, `ORS_GEOCODE_READ_TIMEOUT_SECONDS`.
- **Exactly one retry on a read timeout** (`ORS_READ_TIMEOUT_RETRIES=1`), for both directions and geocoding. Both are idempotent reads, so repeating them is safe.
- A separate budget of **up to 2 retries** for connection errors and 502/503/504 (`ORS_MAX_RETRIES`), with a short linear backoff. 429 and 4xx are never retried.
- `meta.external_api_calls` counts API operations (what "one call" refers to). `meta.external_api_attempts` also counts retries, so they stay visible without making a retried request look like two trips.

**Consequences.**
- The worst case for a stalled directions call is about 2 × 30 s before the 504 `routing_provider_timeout`.
- In practice, a reviewer's first long route takes **10–20 s** while ORS computes it, and every repeat is served from our caches in milliseconds (0 calls).
- The Postman demo prep pre-fetches NYC → LA and then runs `clear_cache`, so the live call shown on camera is fast but still real.
