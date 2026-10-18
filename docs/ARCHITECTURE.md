# Architecture

## Components

```mermaid
flowchart LR
    client["Client<br/>(Postman, browser, curl)"]

    subgraph django["Django process (runserver / gunicorn worker)"]
        direction TB
        views["apps.routing.views<br/>RouteView · RouteMapView · HealthView"]
        ser["serializers.py<br/>input validation + schema"]
        resp["responses.py<br/>payload, rounding, GeoJSON"]
        exc["exception_handler.py<br/>one error envelope"]
        planner["services/planner.py<br/>orchestrator"]
        geocoding["services/geocoding.py<br/>lat,lon · City, ST · ORS fallback<br/>offline US check"]
        orsc["services/ors_client.py<br/>timeouts · retries · call counter"]
        geom["services/route_geometry.py<br/>mile markers, resampling"]
        corr["services/corridor.py<br/>station index + KD-tree"]
        opt["services/optimizer.py<br/>greedy + stop-penalty DP"]
        keys["services/cache_keys.py<br/>namespaced generations"]
    end

    subgraph data["In-memory, loaded once per process"]
        idx[("Station index<br/>6,604 stations<br/>numpy arrays")]
        gaz[("Offline gazetteer<br/>46,949 places<br/>+ KD-tree")]
    end

    cache[("Django cache<br/>route · response · geocode<br/>+ throttle counters")]
    db[("SQLite<br/>FuelStation")]
    ors["OpenRouteService<br/>api.heigit.org<br/>directions · Pelias"]
    tiles["OpenStreetMap tiles<br/>(browser only)"]

    client --> views
    views --> ser & resp & planner
    views -. errors .-> exc
    planner --> geocoding & geom & corr & opt
    geocoding --> gaz
    geocoding --> orsc
    planner --> orsc
    orsc --> ors
    corr --> idx
    idx -. first use .-> db
    views & planner & geocoding --> keys --> cache
    client -. map page .-> tiles
```

### Why each layer exists

| Layer | Responsibility | Django-free? |
|---|---|---|
| `views.py` | HTTP only: validate input → call the planner → build the payload; the response cache; throttling via `throttle_scope` | no |
| `serializers.py` | Parameter rules (ranges, `start_fuel` parsing, tank check) and the OpenAPI response schema | no |
| `responses.py` | Turns a `TripPlan` into JSON or map context. All rounding happens here, and only here | settings only |
| `exception_handler.py` | Maps domain errors, validation, throttling and framework errors to `{"error": {code, message, details}}` | no |
| `services/planner.py` | Orchestration: resolve → same-location check → route (route cache) → geometry → corridor → optimizer (penalised and pure). Collects timings and call counts | uses the cache |
| `services/geocoding.py` | Location resolution, the offline "inside the US" check, and the geocode cache | uses the cache |
| `services/ors_client.py` | The only module that talks HTTP to ORS. Converts `[lon, lat]` order, applies timeouts and retries, and maps errors | settings only |
| `services/route_geometry.py` | Polyline → cumulative miles → uniform 0.5-mile resampling | **yes** (numpy) |
| `services/corridor.py` | Station arrays (lazy singleton) and the KD-tree corridor query | **yes**, except the DB loader |
| `services/optimizer.py` | Greedy and DP; pure functions with their own `UnreachableRouteError` | **yes** |
| `apps/stations/` | `FuelStation` model, admin, CSV cleaning, gazetteer, offline and ORS-fallback geocoding, loading commands | partly |

The pure modules (geometry, corridor, optimizer) are unit-tested without Django. Domain exceptions are converted to HTTP in one place.

## Request sequence

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant V as RouteView
    participant RC as Response cache
    participant P as Planner
    participant G as Geocoding
    participant RTC as Route cache
    participant O as ORS (HeiGIT)
    participant K as Corridor + optimizer

    C->>V: GET /api/v1/route/?start=…&finish=…
    V->>V: throttle (30/min) · validate (serializer)
    V->>RC: get(normalised inputs + params + data version)
    alt response cached
        RC-->>V: payload
        V-->>C: 200 · external_api_calls 0 · cache_hit true
    else miss
        V->>P: plan_trip(params)
        P->>G: resolve start, finish
        Note over G: "lat,lon" / "City, ST" → offline (0 calls)<br/>street address → ORS geocode (1 call, cached 30 d)
        P->>P: same-location check
        P->>RTC: get(endpoints + profile)
        alt route cached
            RTC-->>P: Route (0 calls)
        else
            P->>O: POST /openrouteservice/v2/directions/{profile}/geojson
            Note over O: 30 s read timeout · 1 retry on timeout<br/>≤ 2 retries on 502/503/504
            O-->>P: GeoJSON LineString
            P->>RTC: set (24 h)
        end
        P->>K: geometry → corridor (KD-tree) → optimizer (DP + pure greedy)
        K-->>P: stops, accounting
        P-->>V: TripPlan + timings + calls/attempts
        V->>RC: set payload (24 h)
        V-->>C: 200 JSON (+ map_url)
    end
```

`GET /api/v1/route/map/` follows the same path with its own response-cache entry. It reuses the route cache, so opening a `map_url` after the JSON request makes **0** routing calls.

## Caching layers

| Layer | Key | Value | TTL | Saves |
|---|---|---|---|---|
| **Response** (`views.py`) | normalised `start`/`finish` text + every parameter + endpoint kind + station-data version | rendered payload (without `map_url` and timings) | 24 h | everything: geocoding, routing, corridor, DP, serialisation |
| **Route** (`planner.py`) | profile + endpoints rounded to 1e-5° (~1 m) | `Route` (polyline, distance, duration) | 24 h | the ORS directions call, even when vehicle parameters change |
| **Geocode** (`geocoding.py`) | normalised free text | ORS results | 30 days | the ORS geocode call |

- **Backend:** Django's cache framework, configured with `CACHE_URL`. The default is a file cache in `.cache/`, so it survives `runserver` restarts and is shared by gunicorn workers. Tests use LocMem; production would use Redis. `MAX_ENTRIES` is raised from Django's default of 300 to 10,000.
- **Invalidation:** Django can't delete keys by prefix, so each namespace has a **generation counter** stored in the cache and embedded in every key. `manage.py clear_cache` bumps it, which makes all old entries unreachable for every process at once, while leaving throttle counters alone. Counters are created from the clock in nanoseconds and only ever increase, so an evicted counter can't resurrect old entries. That eviction is a real failure mode of the file cache, which culls a random third of its entries when full; it was found by the benchmark ([ADR-010](DESIGN_DECISIONS.md)).

## In-memory data and process model

- **Station index:** 6,604 stations as numpy arrays (price, lat, lon, 3D unit vectors). It's loaded from the database on the first request that needs it, never per request, and never in `AppConfig.ready()`, which also runs during `migrate` and tests.
- **Offline gazetteer:** 46,949 Census places and their KD-tree (about 0.8 s to load; the station index takes about 0.1 s). It's used for `"City, ST"` lookups and the US check.
- Both are thread-safe lazy singletons. `/api/health/` touches both, so one health call warms a process.
- **gunicorn:** `WARM_ON_STARTUP=1` with `--preload` loads both in the master process before forking, so workers share the memory copy-on-write and no request pays the load. DB connections are closed before the fork.

## Data pipeline

```mermaid
flowchart TD
    csv["data/fuel-prices-for-be-assessment.csv<br/>8,151 rows"] --> clean["clean_rows()<br/>trim · US only · valid price<br/>dedupe by OPIS ID (lowest price)"]
    clean --> stations6626["6,626 unique US stations"]
    census["US Census Gazetteer 2026<br/>places · county subdivisions · counties<br/>(public domain)"] --> build["manage.py build_places<br/>normalise names, aliases, priorities"]
    build --> places["data/us_places.csv (46,949)<br/>data/us_counties.csv (3,091)"]
    stations6626 --> offline["offline match<br/>exact · compact · fuzzy ≥ 0.95, same state"]
    places --> offline
    offline -->|6,454 matched| geocoded
    offline -->|130 unmatched cities| fallback["ORS structured geocode, once<br/>rules 1–6 · cached in data/ors_geocode_cache.json"]
    fallback -->|113 accepted, 150 stations| geocoded["data/stations_geocoded.csv<br/>6,604 stations (99.67%)"]
    fallback -->|17 rejected + reasons| unmatched["data/unmatched_cities.csv"]
    geocoded --> load["manage.py load_stations<br/>idempotent upsert"]
    load --> sqlite[("SQLite FuelStation")]
    sqlite --> index[("in-memory station index")]
```

Details are in [DATA_PIPELINE.md](DATA_PIPELINE.md).

## Errors

| HTTP | `error.code` | Raised by |
|---|---|---|
| 400 | `invalid_input`, `parse_error`, `bad_request` | serializer, JSON parser, Django request limits |
| 413 | `request_too_large` | body over 2.5 MB |
| 405 | `method_not_allowed` | DRF |
| 422 | `location_not_found`, `location_outside_usa`, `same_location`, `route_unreachable` | geocoding, planner, optimizer gap, ORS 2004/2009/2010 |
| 429 | `rate_limited` | our throttle, or ORS 429 |
| 502 | `routing_provider_error` | other ORS errors |
| 503 | `routing_provider_unavailable`, `station_data_unavailable` | connection errors after retries, bad or missing key, no stations loaded |
| 504 | `routing_provider_timeout` | read timeout after the retry |
