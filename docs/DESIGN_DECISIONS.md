# Design decisions

Short ADR-style entries: context → decision → consequences.

| # | Decision |
|---|---|
| 001 | Django 6.1 despite third-party classifiers stopping at 6.0 |
| 002 | Offline station geocoding with the Census Gazetteer |
| 003 | Very conservative fuzzy matching |
| 004 | Duplicate station IDs: keep the lowest price |
| 005 | HeiGIT gateway as the ORS base URL |
| 006 | Offline "inside the US" check for coordinates |
| 007 | Rule-checked ORS fallback for unmatched cities (rules 1–6) |
| 008 | Stop penalty: optimize "cost effective" with an exact DP |
| 009 | Timeouts and retries; calls vs attempts |
| 010 | Three cache layers with generation-counter invalidation |
| 011 | Django + DRF (+ drf-spectacular) |
| 012 | SQLite |
| 013 | OpenRouteService vs other routing APIs |
| 014 | In-memory KD-tree instead of PostGIS |
| 015 | Corridor width, city-level accuracy, ignoring detours |
| 016 | Start-fuel modes |
| 017 | Throttling |
| 018 | Lazy in-memory data; gunicorn `--preload` + `WARM_ON_STARTUP` |
| 019 | Response design |
| — | [What I would do with more time](#what-i-would-do-with-more-time) |

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

**Context.** The assignment asks for the "optimal location to fuel up along the route — **optimal mostly means cost effective** based on fuel prices". The pure cost optimum (the greedy, see `docs/ALGORITHM.md`) is provably the cheapest, but on NYC → LA it makes **19 stops**, several of them 1–3 gallon top-ups that save a few cents because the next station is marginally cheaper. No driver would do that. "Mostly" leaves room to weigh the cost of stopping itself.

**Decision.** A `stop_penalty` parameter (USD per stop, **default 15**, range 0–200) is added to the objective: minimise *fuel cost + stop_penalty × number of stops*. The penalty is a planning weight; it is **not** added to `total_fuel_cost`.

- `stop_penalty = 0` runs the original greedy unchanged: fast and provably optimal for pure cost.
- `stop_penalty > 0` runs an **exact DP**. It relies on the known structure of optimal plans: at every stop you either fill up or buy just enough to arrive empty at the next stop (or the destination). So fuel on arrival is one of finitely many values: 0, `C − d(j, l)` for an earlier station *j* that filled up, or the start fuel minus the distance driven. Each station is processed with a handful of vectorised numpy operations: the fill-up state collapses to one number per station, and the just-enough transitions use a prefix minimum over states sorted by fuel. That makes it O(n·k) over pre-merged stations within range. The DP returns the optimal *set* of stops, and the greedy restricted to that set computes the purchases, so the accounting code is shared.
- The response exposes the trade-off: `summary.pure_cost_optimum` (cost and stop count at penalty 0) and `summary.extra_cost_vs_pure_optimum`.

**Evidence** (real recorded routes, 500 mi range, 10 mpg, empty start):

| Route | Pure optimum | Penalty $15 | Extra fuel cost | Largest leg | DP time |
|---|---|---|---|---|---|
| NYC → LA (2,811.6 mi) | 19 stops, $852.87 | **7 stops**, $860.57 | **+$7.69** | 482.5 mi | ~33 ms |
| Chicago → Houston (1,081.8 mi) | 8 stops, $317.48 | **3 stops**, $319.53 | **+$2.05** | 475.5 mi | ~12 ms |

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
- With the final configuration (30 s plus one retry), the two benchmarks in `docs/benchmarks/` saw **6 read timeouts at 30 s** among about 40 directions requests, **including one on the short Dallas → Austin route**. So besides slow first computations there are also sporadic stalls. The retry rescued 2 of them. Two NYC → LA requests stalled on both attempts and returned 504. The slowest successful call took 14.8 s, which would have failed under a 10 s timeout.

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

---

## ADR-010 — Three cache layers with generation-counter invalidation

**Context.** Requirements: fast responses (a cached repeat in under 50 ms) and as few routing calls as possible. Most requests repeat a small set of city pairs, and vehicle parameters change more often than endpoints.

**Decision.** Use Django's cache framework (backend chosen by `CACHE_URL`; default is a file cache in `.cache/`) with three namespaces:

1. **Response cache:** normalised inputs, every parameter and the station-data version map to the rendered payload. A hit skips geocoding, routing, corridor search, the DP and serialisation.
2. **Route cache:** profile plus endpoints rounded to ~1 m map to the ORS route, so changing `mpg`, `start_fuel` or `stop_penalty` still costs 0 calls.
3. **Geocode cache:** free text maps to ORS results, for 30 days.

Invalidation uses a **generation counter** per namespace, stored in the cache and embedded in every key. Django can't delete keys by prefix. `manage.py clear_cache` bumps the generations, which works across all running processes and leaves throttle counters alone.

**Bug found by the benchmark.** The file cache culls a random third of its entries once it exceeds `MAX_ENTRIES` (300 by default). A culled counter restarted at 1, and older entries became reachable again, giving stale hits after `clear_cache`. Fixes:
- Counters are created from the clock in nanoseconds and only ever increase.
- `MAX_ENTRIES` is raised to 10,000 (`CACHE_MAX_ENTRIES`).
- A test simulates eviction.

**Consequences.** On gunicorn, a cached NYC → LA response takes 36 ms with geometry and 7 ms without (`docs/benchmarks/`). In production the backend would be Redis (`CACHE_URL=redis://…`), with no code change.

---

## ADR-011 — Django + Django REST Framework (+ drf-spectacular)

**Context.** The assignment requires the latest stable Django.

**Decision.**
- **DRF** for request parsing and validation (serializers), class-based API views, throttling, a pluggable exception handler and renderers: JSON for the API, templates for the map page.
- **drf-spectacular** generates an OpenAPI 3 schema and Swagger UI from the same serializers. It's validated in tests with `--fail-on-warn`.
- No authentication classes (a public, read-only API), so DRF does no session or CSRF handling.

**Alternatives.** Plain Django views with `JsonResponse` would need validation, schema and error handling written by hand. Django Ninja is closer to FastAPI but less established than DRF.

---

## ADR-012 — SQLite

**Context.** Reviewers should get running with `migrate` and `load_stations` and no database server. The data is read-only at request time: 6,604 rows, loaded into memory once per process.

**Decision.** Use SQLite, configurable with `DATABASE_URL`. Requests never query stations; they use the in-memory index.

**Consequences.** Moving to PostgreSQL is a one-line `DATABASE_URL` change. The upsert (`bulk_create(update_conflicts=True)`) works on both.

---

## ADR-013 — OpenRouteService (HeiGIT) as the routing and geocoding provider

**Options considered.**
- **OSRM public demo server:** no SLA and no geocoding, so it isn't meant for applications.
- **Google Maps Platform:** needs a billing account, and its terms restrict caching results.
- **Mapbox, GraphHopper:** viable, with free tiers.
- **ORS:** chosen because it is free with a key, has directions **and** Pelias geocoding behind one key, a truck profile (`driving-hgv`), a 6,000 km maximum route (coast to coast fits), and 2,000 directions requests per day.

**Decision.** Use ORS through the HeiGIT gateway (ADR-005), with OpenStreetMap tiles and Leaflet for the map page. Leaflet is pinned to 1.9.4 from cdnjs with SRI hashes.

**Consequences.** The free tier's latency varies. The first computation of a long route can take 10–20 s, and occasional stalls happen (ADR-009). Our caching makes this a one-time cost per route.

---

## ADR-014 — In-memory KD-tree instead of PostGIS

**Context.** The corridor query has to find the stations within X miles of a polyline that is ~2,800 miles long, quickly.

**Decision.**
- Station coordinates and prices live in numpy arrays.
- Per request, the route is resampled every 0.5 mi (~5,600 points for NYC → LA), and a `scipy.spatial.cKDTree` is built on the points' **3D unit vectors**.
- Stations that survive a cheap bounding-box prefilter are queried in one vectorised call for their nearest route point. That gives each station's **mile marker** and **off-route distance**.
- Unit vectors make chord distance monotonic in great-circle distance, so there's no lat/lon distortion and no antimeridian special case.

**Consequences.** The corridor search takes ~5 ms on NYC → LA, with no GDAL or PostGIS installation for reviewers.

With more time: PostGIS (`ST_DWithin` on a geography LineString plus `ST_LineLocatePoint`) would be the scalable choice for millions of stations or exact station coordinates.

---

## ADR-015 — Corridor width, city-level accuracy, ignoring detours

**Context.** Station coordinates are city centroids (ADR-002), so a station can sit several miles from the highway it actually serves.

**Decision.**
- The default corridor is **10 miles** (`corridor_miles`, 1–50). It's wide enough to absorb the centroid error, and narrow enough to exclude stations on parallel roads most of the time.
- The detour to a station is **not** added to fuel use; `off_route_miles` is reported per stop.
- Stations at the same mile marker (one city) are merged, keeping the cheapest.

**Consequences.** On NYC → LA there are 455 stations in the corridor, and the default plan's stops are within 10 mi of the route.

Improvement: add `2 × off_route_miles` to the consumption at each stop, or geocode exact exits (Future work).

---

## ADR-016 — Start-fuel modes

**Decision.** `start_fuel` can be:
- **`empty`** (default): the first stop must be within 25 mi, and the fuel used to reach it is charged at that stop's price, so `total_fuel_cost` covers every gallon burned. This is the most honest answer to "total money spent on fuel".
- **`full`:** 50 gal, not charged; the cost is money spent at the pump.
- **a number of gallons:** not charged.

**Implementation.** `full` and numeric starts are modelled as a free virtual station before the start, so the same greedy applies. `empty` tries every candidate first stop in the window.

**Consequences.** All three modes are covered by the brute-force optimality test (ADR-008). On the recorded NYC → LA route with the default penalty: `empty` → 7 stops, $860.57; `full` → 6 stops, $706.01; `12.5` → 7 stops, $820.13.

---

## ADR-017 — Throttling

**Context.** One public endpoint can drain the free ORS quota of 2,000 directions requests per day and 40 per minute.

**Decision.**
- DRF `ScopedRateThrottle` with scope `route` on the route and map endpoints only: **30/min per client IP**, configurable with `ROUTE_THROTTLE_RATE`.
- Counters live in the default cache, so they're shared by gunicorn workers.
- Throttling runs before the view, so throttled requests never reach ORS.
- The response is 429 `rate_limited` with `Retry-After`.

**Consequences.** The Postman Runner (16 route requests) fits within one minute. Benchmarks raise the rate, and `scripts/benchmark.py` prints a clear warning if it gets a 429.

---

## ADR-018 — Lazy in-memory data; gunicorn `--preload` + `WARM_ON_STARTUP`

**Context.** The station index takes ~0.1 s to load, and the gazetteer with its KD-tree ~0.8 s (measured medians). Loading them per request would destroy latency. Loading them in `AppConfig.ready()` runs during `migrate`, `makemigrations`, tests and every management command, and querying the database there is discouraged (the tables may not exist yet).

**Decision.**
- Both are **thread-safe lazy singletons** (double-checked locking), loaded on first use.
- `/api/health/` touches both, so a single health call warms a process.
- `load_stations` resets the index in its own process; running servers reload on restart.
- For production, `WARM_ON_STARTUP=1` loads both when `config/wsgi.py` is imported. Combined with `gunicorn --preload`, that happens **once in the master** before forking, and workers share the memory copy-on-write. DB connections are closed before the fork.

**Consequences.** `meta.timings_ms.data_load` is ~0 on every request after warm-up. The gunicorn benchmark ran with this configuration.

---

## ADR-019 — Response design

**Decisions.**
- Money and gallons are rounded to 2 decimals, prices to 3, miles to 1 and coordinates to 5, **only** when building the response. Internal values stay unrounded.
- The route polyline is thinned to vertices at least 0.25 mi apart along the path: the recorded NYC → LA route goes from 21,760 to 7,911 points, and the full response is ~171 KB (benchmark). `include_geometry=false` gives a ~4 KB response.
- `summary.pure_cost_optimum` and `extra_cost_vs_pure_optimum` make the stop-penalty trade-off visible (ADR-008).
- `meta.external_api_calls` counts API operations and `meta.external_api_attempts` counts retries (ADR-009). `meta.timings_ms` breaks down every step.
- Errors use one envelope, `{"error": {"code", "message", "details"}}`, produced by a single DRF exception handler. Domain exceptions carry their own code and HTTP status.
- `map_url` reproduces the request as a Leaflet page and is served from the same caches (0 calls).

---

## What I would do with more time

- **Exact station coordinates:** geocode each exit (`"I-44, EXIT 283"`) against OSM motorway junctions, instead of using city centroids. This removes the corridor slack and makes detour costs computable.
- **PostGIS** for the corridor query, and **live prices** through a scheduled import replacing the static CSV.
- **Detour-aware cost:** add `2 × off_route_miles` of fuel per stop. Also consider time, opening hours and truck-stop amenities in the penalty.
- **Redis** for the cache and throttle counters, and **async views with httpx**, so a slow ORS call doesn't hold a worker thread.
- **Self-hosted ORS or OSRM** for predictable latency and no quota; a circuit breaker around the provider.
- **Docker** image and Compose file, **CI** (pytest, ruff and newman on every push), and API keys or authentication for non-public deployments.
- Route alternatives: compare the cheapest-fuel plan across 2–3 alternative routes. ORS limits alternatives to routes of at most 100 km on the public API.
