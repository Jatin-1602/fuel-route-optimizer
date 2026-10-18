# Postman guide

The collection [`postman/FuelRouteOptimizer.postman_collection.json`](../postman/FuelRouteOptimizer.postman_collection.json) uses Collection format v2.1. It contains **17 requests with 79 test assertions**, ordered so the Collection Runner works top to bottom. It **passes on repeated runs**: the first run makes about 7 routing calls, and later runs are served from the cache.

## 1. Import

1. Start the API (`python manage.py runserver`, or gunicorn; see the README).
2. In Postman, choose **Import**, then drag in both files from `postman/`:
   - `FuelRouteOptimizer.postman_collection.json`
   - `local.postman_environment.json` (defines `base_url = http://localhost:8000`)
3. Select the **"Fuel Route Optimizer - local"** environment (top right). If your server uses a different host or port, edit `base_url` there.

## 2. What each request demonstrates

| # | Request | Shows | Key assertions |
|---|---|---|---|
| 01 | Health (warm-up) | Data loaded into memory; key configured (as a boolean only) | stations > 6,000, gazetteer > 40,000 |
| 02 | NYC → LA | Coast-to-coast plan, offline geocoding | `external_api_calls <= 1`, cost > 0, every leg ≤ range, trade-off reported; saves `map_url` |
| 03 | NYC → LA again | Response cache | `external_api_calls == 0`, `cache_hit == true` |
| 04 | Map page | HTML/Leaflet map for request 02 (0 calls) | 200, `text/html`, contains the trip data |
| 05 | Chicago → Houston (POST) | JSON body instead of query parameters | `external_api_calls <= 1` |
| 06 | Dallas → Austin | Trip shorter than one tank | exactly 1 stop, within 25 mi of the start |
| 07 | `start_fuel=full` | Starts with a full tank that isn't charged | 50 gal, initial cost 0 |
| 08 | `start_fuel=12.5` | Partial tank | initial fuel 12.5 gal |
| 09 | `profile=driving-hgv` | Truck routing | `profile == driving-hgv` |
| 10 | `lat,lon` inputs | Coordinates checked offline against US bounds | `resolved_by == coordinates` |
| 11 | Street address | Free-text address geocoded by ORS, then cached | `external_api_calls <= 3`, `resolved_by == ors` |
| 12 | `stop_penalty=0` | Pure-cost optimum (route reused, 0 calls) | `extra_cost_vs_pure_optimum == 0` |
| 13 | `include_geometry=false` | Lean response | `route == null`, < 20 KB |
| 14 | Error: missing parameter | Validation envelope | 400 `invalid_input`, details mention `finish` |
| 15 | Error: outside the USA | Toronto, ON (no routing call) | 422 `location_outside_usa` |
| 16 | Error: invalid `start_fuel` | `half` | 400 `invalid_input`, details mention `start_fuel` |
| 17 | Error: same location | Dallas, TX vs Dallas, Texas | 422 `same_location` |

Every request also runs two collection-level checks: the response time is under 15 s, and the response is not **429**. A 429 fails with the message *"wait 60 s or raise ROUTE_THROTTLE_RATE"*.

## 3. Run the collection

**Postman app:** right-click the collection, choose **Run collection**, keep the order, then **Run**.

**CLI** (same result, useful for CI):

```bash
npx newman@6.2.3 run postman/FuelRouteOptimizer.postman_collection.json \
  -e postman/local.postman_environment.json
```

### Rate limit

Route endpoints allow **30 requests/minute per client** by default, to protect the free ORS quota. One full run sends **16 route requests**, so either wait a minute between runs or raise the limit in `.env`:

```
ROUTE_THROTTLE_RATE=60/min
```

then restart the server.

## 4. Demo prep (before recording)

Run these in order, about 2 minutes before you start recording:

```bash
cd ~/Desktop/Interview/fuel-route-optimizer && source .venv/bin/activate

# 1. Clear cached routes, responses and geocodes, so request 02 shows a real routing call
python manage.py clear_cache

# 2. Start the server (keep this terminal open)
python manage.py runserver

# 3. In another terminal: warm-up. This loads stations + gazetteer (~1 s, once), so the demo never shows a cold start
curl -s localhost:8000/api/health/

# 4. Recommended: let OpenRouteService compute NYC -> LA once (its first computation of a long
#    route can take > 10 s), then clear OUR caches again. Request 02 still makes a real routing
#    call on camera (external_api_calls: 1), and ORS answers it in about 2 s.
curl -s -o /dev/null "localhost:8000/api/v1/route/?start=New%20York,%20NY&finish=Los%20Angeles,%20CA"
python manage.py clear_cache
```

Then in Postman, run the collection once (Runner: 17/17 requests, 79/79 assertions) and walk through requests 02 → 03 → 04 on camera:

- **02:** point at `summary.total_fuel_cost`, `number_of_stops`, `pure_cost_optimum`, `estimated_savings`, `meta.external_api_calls: 1` and `meta.timings_ms`.
- **03:** the same request, now with `external_api_calls: 0`, `cache_hit: true`, and a few milliseconds.
- **04:** copy `map_url` from 02 into a browser to show the map.
- Finish with one error case (15, Toronto) and the green Runner summary.

**If request 02 returns 504** (`routing_provider_timeout`), OpenRouteService was slow computing a long route for the first time. Send it again; it usually returns in about 2 s. This is a good moment to show the error envelope and `external_api_attempts`.

### Checklist

- [ ] `.env` has `ORS_API_KEY`; `curl localhost:8000/api/health/` shows `"ors_api_key_configured": true`
- [ ] `python manage.py clear_cache` run, server restarted, health warm-up done
- [ ] Postman environment "Fuel Route Optimizer - local" selected
- [ ] Not more than one Runner run in the last minute (rate limit)
