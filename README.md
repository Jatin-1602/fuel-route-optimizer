# Fuel Route Optimizer

A Django REST API that plans a driving route between two US locations and picks the **most cost-effective fuel stops** along it. It assumes a vehicle with a 500-mile range at 10 mpg, uses the provided truck-stop price list, and returns the route as a map. **Most requests cost exactly one routing API call** (OpenRouteService), and repeats cost none.

```
GET /api/v1/route/?start=New York, NY&finish=Los Angeles, CA
→ 7 fuel stops, $860.57 total fuel cost (+$7.69 vs the 19-stop pure-cost optimum), GeoJSON route, map_url
```

## Features → requirements

| Requirement | How |
|---|---|
| Start/finish inside the USA | `"City, ST"`, `"lat,lon"` or a street address; non-US input → 422 `location_outside_usa` |
| Map of the route | GeoJSON `FeatureCollection` in the JSON response, plus `map_url` → interactive Leaflet/OpenStreetMap page |
| Cost-effective fuel stops | Provably optimal greedy (pure cost) and an exact DP with a per-stop penalty (default $15), both checked against a brute-force solver |
| 500-mile range, multiple stops | Tank = range / mpg (50 gal); every leg ≤ range; configurable `max_range_miles` and `mpg` |
| Total fuel cost at 10 mpg | `summary.total_fuel_cost`, a per-stop breakdown, and savings vs the corridor's average price |
| Provided fuel price file | Cleaned, de-duplicated, US-only, geocoded offline (99.7% of stations) |
| Free map/routing API | OpenRouteService (HeiGIT) for routing; OpenStreetMap tiles for the map |
| Fast | Station index and gazetteer in memory, KD-tree corridor search, layered caching; see [Performance](#performance) |
| Few routing calls | 1 call per new trip, 0 when cached, ≤ 3 with free-text street addresses (`meta.external_api_calls`) |

## Tech stack

Python 3.12 · **Django 6.1.2** · Django REST Framework 3.18.3 · drf-spectacular 0.30.0 (OpenAPI / Swagger UI) · NumPy 2.5.4 + SciPy 1.18.1 (KD-tree) · requests 2.34.2 · SQLite · gunicorn 26.2.0 · pytest + ruff

## Quickstart

```bash
git clone <repo-url> fuel-route-optimizer && cd fuel-route-optimizer
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env          # then set ORS_API_KEY=... (free key: https://account.heigit.org/manage/key)
python manage.py migrate && python manage.py load_stations
python manage.py runserver    # http://localhost:8000/api/docs/
```

The geocoded station data is committed, so no geocoding step or API key is needed to load it. The key is only used for routing.

## Example

```bash
curl "http://localhost:8000/api/v1/route/?start=Chicago,%20IL&finish=Houston,%20TX&include_geometry=false"
```

The response, trimmed:

```json
{
  "summary": {
    "total_distance_miles": 1081.8, "total_fuel_cost": 319.53, "number_of_stops": 3,
    "pure_cost_optimum": {"total_fuel_cost": 317.48, "number_of_stops": 8},
    "extra_cost_vs_pure_optimum": 2.05, "estimated_savings": 37.74, "largest_leg_miles": 475.5
  },
  "fuel_stops": [{"order": 1, "name": "QUIKTRIP #7205", "city": "Lansing", "state": "IL",
                  "price_per_gallon": 3.079, "mile_marker": 18.0, "gallons_purchased": 29.85, "cost": 91.91}, "..."],
  "route": null,
  "map_url": "http://localhost:8000/api/v1/route/map/?start=Chicago%2C+IL&...",
  "meta": {"external_api_calls": 1, "cache_hit": false, "timings_ms": {"routing": 890.6, "corridor": 3.1, "optimize": 12.5}}
}
```

## How many routing API calls?

| Request | Calls |
|---|---|
| `"City, ST"` or `"lat,lon"` for both ends (resolved offline) | **1** (directions) |
| Same request again (response cache) | **0** |
| Same trip with different vehicle parameters (route cache) | **0** |
| Opening `map_url` after the JSON request | **0** |
| Free-text street address at one or both ends | **2–3** (geocode per address, cached 30 days, + directions) |

`meta.external_api_attempts` also counts retries. A stalled request is retried once after the 10 s read timeout, and 502/503/504 or connection errors are retried up to twice.

## Performance

Measured with `scripts/benchmark.py` against **gunicorn 26.2.0** (2 workers × 4 threads, `--preload`, `WARM_ON_STARTUP=1`). Values are client wall time p50 in ms:

| Route | Response | Cold (after `clear_cache`) | Warm-ORS (route cached, all else recomputed) | Cached |
|---|---|---|---|---|
| NYC → LA (2,812 mi) | full (171 KB) | 15,667* | 154 | **38** |
| NYC → LA | lean (4 KB) | 1,794 | 95 | **9** |
| Chicago → Houston (1,082 mi) | full (63 KB) | 964* | 68 | **19** |
| Chicago → Houston | lean | 899 | 45 | **8** |
| Dallas → Austin (195 mi) | full (16 KB) | 509 | 29 | **15** |
| Dallas → Austin | lean | 496 | 24 | **9** |

\* *Upstream variance.* On 2026-10-18, OpenRouteService often took more than 10 s to compute a long route the first time, and the identical request returned in about 1–2 s moments later. Several cold "full" requests therefore ended in 504 after the timeout plus one retry. The cold numbers are ORS latency. Our own processing is the warm-ORS column (corridor search ~5 ms and optimizer ~35 ms on NYC → LA).

Full results, including runserver and p95: [docs/benchmarks/](docs/benchmarks/). To reproduce:

```bash
WARM_ON_STARTUP=1 ROUTE_THROTTLE_RATE=1000/min gunicorn config.wsgi --preload --workers 2 --threads 4 -b 127.0.0.1:8000
python scripts/benchmark.py --base-url http://localhost:8000 --label gunicorn
```

## Postman

Import [`postman/FuelRouteOptimizer.postman_collection.json`](postman/) and the `local` environment, then run the collection top to bottom. It has 17 requests and 79 assertions, and passes on repeated runs. See [docs/POSTMAN_GUIDE.md](docs/POSTMAN_GUIDE.md), including **demo prep**.

## Tests

```bash
pytest -q          # 300+ tests, no network: ORS is mocked with recorded real responses
ruff check .
```

The optimizer is verified against a brute-force dynamic program on 900 random instances, covering all three start-fuel modes, with and without the stop penalty.

## Documentation

- [docs/DESIGN_DECISIONS.md](docs/DESIGN_DECISIONS.md): ADRs (Django version, offline geocoding, ORS base URL, US checks, stop penalty, …)
- [docs/POSTMAN_GUIDE.md](docs/POSTMAN_GUIDE.md): importing and running the collection; demo prep
- [docs/benchmarks/](docs/benchmarks/): raw benchmark output
