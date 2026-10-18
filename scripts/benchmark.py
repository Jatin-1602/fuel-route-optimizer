"""Benchmark the running API: cold vs warm-ORS vs cached, full vs lean responses.

Phases, per route and response variant:

* **cold**     — ``manage.py clear_cache`` before every request: geocode + 1 ORS directions
                 call + corridor + optimizer + response (the honest first-request latency).
* **warm-ORS** — only the response cache is cleared: the route comes from the route cache
                 (0 ORS calls), everything else is recomputed (our own processing time).
* **cached**   — identical repeat request served from the response cache.

Variants: ``full`` (with GeoJSON geometry) and ``lean`` (``include_geometry=false``).

Usage (server started with a high throttle, e.g. ``ROUTE_THROTTLE_RATE=1000/min``):

    python scripts/benchmark.py --base-url http://localhost:8000 --label runserver

``clear_cache`` runs in a subprocess, so the benchmark must use the same cache backend
as the server (the default file cache in ``.cache/`` is shared automatically).
Cold runs spend real ORS quota: routes x variants x --cold-runs directions calls.
"""

import argparse
import collections
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import requests

REPO = Path(__file__).resolve().parent.parent
ROUTES: dict[str, tuple[str, str]] = {
    "NYC -> LA": ("New York, NY", "Los Angeles, CA"),
    "Chicago -> Houston": ("Chicago, IL", "Houston, TX"),
    "Dallas -> Austin": ("Dallas, TX", "Austin, TX"),
}
VARIANTS: dict[str, dict[str, str]] = {"full": {}, "lean": {"include_geometry": "false"}}


@dataclass
class Sample:
    wall_ms: float
    server_ms: float | None
    status: int
    calls: int
    attempts: int
    size: int


@dataclass
class Result:
    route: str
    variant: str
    phase: str
    samples: list[Sample] = field(default_factory=list)

    @property
    def ok(self) -> list[Sample]:
        return [s for s in self.samples if s.status == 200]


class RateLimited(Exception):
    pass


def clear_cache(only: str | None = None) -> None:
    cmd = [sys.executable, str(REPO / "manage.py"), "clear_cache"]
    if only:
        cmd += ["--only", only]
    subprocess.run(cmd, cwd=REPO, check=True, capture_output=True)


def request(session: requests.Session, base_url: str, params: dict[str, str]) -> Sample:
    started = time.perf_counter()
    response = session.get(f"{base_url}/api/v1/route/", params=params, timeout=60)
    wall = 1000 * (time.perf_counter() - started)
    if response.status_code == 429:
        raise RateLimited(response.headers.get("Retry-After", "?"))
    meta = response.json().get("meta", {}) if response.status_code == 200 else {}
    return Sample(
        wall_ms=wall,
        server_ms=meta.get("timings_ms", {}).get("request_total"),
        status=response.status_code,
        calls=meta.get("external_api_calls", 0),
        attempts=meta.get("external_api_attempts", 0),
        size=len(response.content),
    )


def prime_route(session: requests.Session, base_url: str, params: dict[str, str]) -> None:
    """Make sure the route is in the route cache (cold attempts may have failed upstream)."""
    for _ in range(3):
        if request(session, base_url, params).status == 200:
            return
        time.sleep(2)


def pct(values: list[float], q: float) -> float:
    return float(np.percentile(values, q)) if values else float("nan")


def warn_rate_limited(retry_after: str) -> None:
    bar = "!" * 78
    print(
        f"\n{bar}\n  WARNING: HTTP 429 rate_limited from the API (Retry-After: {retry_after}s).\n"
        "  The server's throttle is too low for a benchmark. Restart it with e.g.\n"
        "      ROUTE_THROTTLE_RATE=1000/min python manage.py runserver\n"
        "  (or set ROUTE_THROTTLE_RATE in .env) and run the benchmark again.\n"
        f"  Results below are incomplete.\n{bar}\n",
        file=sys.stderr,
    )


def run(args: argparse.Namespace) -> list[Result]:
    session = requests.Session()
    for _ in range(10):  # warm every worker's in-memory data
        session.get(f"{args.base_url}/api/health/", timeout=30).raise_for_status()

    results: list[Result] = []
    for route_name in args.routes:
        start, finish = ROUTES[route_name]
        for variant, extra in VARIANTS.items():
            params = {"start": start, "finish": finish, **extra}
            phases = [
                ("cold", args.cold_runs),
                ("warm-ORS", args.warm_runs),
                ("cached", args.cached_runs),
            ]
            for phase, n in phases:
                result = Result(route_name, variant, phase)
                results.append(result)
                for i in range(n):
                    if phase == "cold":
                        clear_cache()
                        if i:
                            time.sleep(args.cold_pause)  # stay well under ORS's 40/min
                    elif phase == "warm-ORS":
                        if i == 0:
                            prime_route(session, args.base_url, params)
                        clear_cache("response")
                    elif i == 0:
                        request(session, args.base_url, params)  # prime the response cache
                    result.samples.append(request(session, args.base_url, params))
                print(
                    f"  {route_name:<19} {variant:<5} {phase:<9} {len(result.ok)}/{n} ok",
                    file=sys.stderr,
                )
    return results


COLUMNS = (
    "route",
    "variant",
    "phase",
    "n",
    "p50 ms",
    "p95 ms",
    "server p50",
    "ORS calls",
    "attempts",
    "KB",
)


def report(results: list[Result], label: str) -> None:
    """Print a Markdown table (paste-able into the README)."""
    rows = []
    for r in results:
        ok = r.ok
        walls = [s.wall_ms for s in ok]
        servers = [s.server_ms for s in ok if s.server_ms is not None]
        failed = collections.Counter(s.status for s in r.samples if s.status != 200)
        errors = ", ".join(f"{n}x{code}" for code, n in sorted(failed.items()))
        rows.append(
            [
                r.route,
                r.variant,
                r.phase + (f" (failed: {errors})" if errors else ""),
                str(len(ok)),
                f"{pct(walls, 50):.1f}",
                f"{pct(walls, 95):.1f}",
                f"{statistics.median(servers):.1f}" if servers else "-",
                str(sum(s.calls for s in ok)),
                str(sum(s.attempts for s in ok)),
                f"{statistics.mean(s.size for s in ok) / 1024:.0f}" if ok else "-",
            ]
        )
    widths = [max(len(c), *(len(row[i]) for row in rows)) for i, c in enumerate(COLUMNS)]

    def line(cells: list[str] | tuple[str, ...]) -> str:
        return (
            "| "
            + " | ".join(
                c.ljust(w) if i < 3 else c.rjust(w)
                for i, (c, w) in enumerate(zip(cells, widths, strict=True))
            )
            + " |"
        )

    print(f"\n### Benchmark: {label}\n")
    print(line(COLUMNS))
    print("|" + "|".join("-" * (w + 2) for w in widths) + "|")
    for row in rows:
        print(line(row))
    print(
        "\np50/p95: client wall time incl. HTTP and JSON transfer; server p50: "
        "meta.timings_ms.request_total; ORS calls/attempts: summed over the phase. "
        "Failures are upstream (ORS) errors returned as 502/503/504."
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--label", default="server")
    parser.add_argument("--routes", nargs="+", choices=list(ROUTES), default=list(ROUTES))
    parser.add_argument("--cold-runs", type=int, default=3, help="Real ORS calls per route+variant")
    parser.add_argument("--warm-runs", type=int, default=10)
    parser.add_argument("--cached-runs", type=int, default=20)
    parser.add_argument("--cold-pause", type=float, default=1.5, help="Seconds between cold calls")
    args = parser.parse_args()

    calls = len(args.routes) * len(VARIANTS) * args.cold_runs
    print(
        f"Benchmarking {args.base_url} ({args.label}); cold phase makes ~{calls} ORS calls.",
        file=sys.stderr,
    )
    try:
        results = run(args)
    except RateLimited as exc:
        warn_rate_limited(str(exc))
        sys.exit(2)
    except requests.ConnectionError:
        sys.exit(f"Cannot reach {args.base_url}. Is the server running?")
    report(results, args.label)


if __name__ == "__main__":
    main()
