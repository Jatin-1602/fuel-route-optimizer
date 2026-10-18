# Setup

## Requirements

- **Python 3.12 or newer** (Django 6.1 requires ≥ 3.12). Developed and tested on 3.12.12.
- Git, and about 300 MB of disk space for the virtualenv.
- A free OpenRouteService **Basic Key** (routing only; see [Getting an API key](#getting-an-openrouteservice-api-key)).

## Linux (primary: Ubuntu / Linux Mint)

```bash
# 1. Python 3.12 (skip if `python3.12 --version` already works)
sudo apt update && sudo apt install python3.12 python3.12-venv

# 2. Clone and create a virtualenv
git clone <repo-url> fuel-route-optimizer && cd fuel-route-optimizer
python3.12 -m venv .venv
source .venv/bin/activate           # prompt shows (.venv)
python -m pip install --upgrade pip
pip install -r requirements-dev.txt # runtime + test/lint tools (requirements.txt = runtime only)

# 3. Configuration
cp .env.example .env
#   edit .env: set ORS_API_KEY=<your key>; leave the rest at their defaults

# 4. Database and station data
python manage.py migrate
python manage.py load_stations      # 6,604 stations from data/stations_geocoded.csv

# 5. Run
python manage.py runserver
curl -s localhost:8000/api/health/  # {"status":"ok", "stations_loaded":6604, ...}
```

Then open <http://localhost:8000/api/docs/> (Swagger UI).

### Using pyenv (optional)

The repo contains a `.python-version` file (`3.12.12`). With [pyenv](https://github.com/pyenv/pyenv):

```bash
pyenv install 3.12.12        # once
cd fuel-route-optimizer      # pyenv picks 3.12.12 automatically
python -m venv .venv && source .venv/bin/activate
```

## macOS

```bash
brew install python@3.12
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
```

The remaining steps match Linux.

## Windows (PowerShell)

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1          # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
pip install -r requirements-dev.txt
copy .env.example .env
python manage.py migrate
python manage.py load_stations
python manage.py runserver
```

gunicorn doesn't run on Windows. Use `runserver`, or WSL for the benchmark.

## Getting an OpenRouteService API key

1. Sign up at <https://account.heigit.org> (HeiGIT runs OpenRouteService).
2. Open **Manage → Key** at <https://account.heigit.org/manage/key> and copy the **Basic Key**.
3. Put it in `.env` as `ORS_API_KEY=...`. Never commit `.env`; it is gitignored.

The free tier allows 2,000 directions requests per day (40/min) and a route of at most 6,000 km. The API uses the HeiGIT gateway `https://api.heigit.org`; the old `api.openrouteservice.org` host is deprecated ([ADR-005](DESIGN_DECISIONS.md)).

## Environment variables (`.env`)

| Variable | Default | Purpose |
|---|---|---|
| `ORS_API_KEY` | *(empty)* | OpenRouteService key. Without it, routing returns 503 `routing_provider_unavailable` |
| `ORS_BASE_URL` | `https://api.heigit.org` | Gateway base URL (no trailing slash) |
| `ORS_CONNECT_TIMEOUT_SECONDS` | `5` | Connect timeout for all ORS requests |
| `ORS_DIRECTIONS_READ_TIMEOUT_SECONDS` | `30` | Read timeout for directions (the first computation of a long route can take 10–20 s) |
| `ORS_GEOCODE_READ_TIMEOUT_SECONDS` | `10` | Read timeout for geocoding |
| `ORS_READ_TIMEOUT_RETRIES` | `1` | Retries after a read timeout |
| `ORS_MAX_RETRIES` | `2` | Retries after connection errors or 502/503/504 |
| `ROUTE_THROTTLE_RATE` | `30/min` | Per-client limit on route and map requests (raise for benchmarks) |
| `DEFAULT_STOP_PENALTY` | `15` | USD per stop used when choosing stops ([ADR-008](DESIGN_DECISIONS.md)) |
| `RESPONSE_GEOMETRY_SPACING_MILES` | `0.25` | Minimum spacing of returned route vertices |
| `DEBUG` | `False` (example sets `True`) | Django debug mode |
| `SECRET_KEY` | dev-only key when `DEBUG=True` | **Required** when `DEBUG=False` |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1` | Comma-separated host names |
| `DATABASE_URL` | `sqlite:///db.sqlite3` | Any django-environ DB URL |
| `CACHE_URL` | file cache in `.cache/` | e.g. `locmemcache://` or `redis://localhost:6379/1` |
| `CACHE_MAX_ENTRIES` | `10000` | File-cache size before Django culls entries |
| `ROUTE_CACHE_TTL_SECONDS` | `86400` | Route and response cache lifetime (24 h) |
| `GEOCODE_CACHE_TTL_SECONDS` | `2592000` | Geocode cache lifetime (30 days) |
| `GAZETTEER_YEAR` | `2026` | Census Gazetteer vintage for `build_places` |
| `LOG_LEVEL` | `INFO` | Log level for `apps.*` loggers |
| `WARM_ON_STARTUP` | *(unset)* | `1` loads stations and gazetteer at WSGI import (use with `gunicorn --preload`) |

## Database and data

| Command | When |
|---|---|
| `python manage.py migrate` | Once, and after pulling new migrations |
| `python manage.py load_stations` | Loads the committed `data/stations_geocoded.csv`. Idempotent (an upsert by OPIS ID that also removes stations no longer in the file). Restart running servers afterwards; each process keeps its own in-memory index |
| `python manage.py createsuperuser` | Optional, for the admin at `/admin/` (stations searchable and filterable by state, sorted by price) |

### Regenerating the geocoded data (optional)

Reviewers don't need this, because the outputs are committed.

```bash
python manage.py build_places                 # download the Census Gazetteer → data/us_places.csv, data/us_counties.csv
python manage.py geocode_stations             # clean + geocode → data/stations_geocoded.csv (uses the committed ORS cache, 0 calls)
python manage.py geocode_stations --ors-fallback   # also query ORS for unmatched cities missing from the cache (needs a key)
python manage.py load_stations
```

See [DATA_PIPELINE.md](DATA_PIPELINE.md).

## Running

| Task | Command |
|---|---|
| Dev server | `python manage.py runserver` |
| Production-style server | `WARM_ON_STARTUP=1 gunicorn config.wsgi --preload --workers 2 --threads 4 -b 127.0.0.1:8000` |
| Tests | `pytest -q` (316 tests, no network) |
| Tests with coverage | `pytest --cov=apps --cov-report=term-missing` |
| Lint / format | `ruff check .` / `ruff format .` |
| Clear caches | `python manage.py clear_cache` (`--only route\|response\|geocode`, `--all` also resets throttle counters) |
| Benchmark | `python scripts/benchmark.py --base-url http://localhost:8000` (run the server with `ROUTE_THROTTLE_RATE=1000/min`) |
| Postman / newman | see [POSTMAN_GUIDE.md](POSTMAN_GUIDE.md) |

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `ERROR: Package requires a different Python` or Django won't install | Python older than 3.12. Check `python --version` inside the venv and recreate it with `python3.12 -m venv .venv` |
| `ModuleNotFoundError: django` | The venv isn't active: run `source .venv/bin/activate` (Windows: `.venv\Scripts\Activate.ps1`) |
| 503 `routing_provider_unavailable`, *"ORS_API_KEY is missing"* | Set `ORS_API_KEY` in `.env` and restart the server |
| 503, *"rejected the API key"* | Wrong key or a copy/paste error (no quotes or spaces). Copy the **Basic Key** again |
| 503 `station_data_unavailable` | Run `python manage.py load_stations`, then restart the server |
| 504 `routing_provider_timeout` | ORS took more than 30 s twice in a row (it happens with first-time long routes). Retry; repeats are cached |
| 429 `rate_limited` | More than 30 route requests per minute. Wait, or raise `ROUTE_THROTTLE_RATE` |
| `Error: That port is already in use` | `python manage.py runserver 8001`, or stop the other process (`lsof -i :8000`) |
| `SSLError` / proxy errors when calling ORS | Corporate proxy or old CA bundle. Set `HTTPS_PROXY`/`REQUESTS_CA_BUNDLE`, or run `pip install --upgrade certifi` |
| Results look stale after changing data | `python manage.py clear_cache` and restart the server |
