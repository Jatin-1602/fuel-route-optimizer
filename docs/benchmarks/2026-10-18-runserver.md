<!-- Output of scripts/benchmark.py, 2026-10-18, Linux Mint 22.1, Python 3.12.12. Cold runs hit the live OpenRouteService API. -->

### Benchmark: runserver (Django dev server)

| route              | variant | phase           |  n | p50 ms | p95 ms | server p50 | ORS calls | attempts |  KB |
|--------------------|---------|-----------------|----|--------|--------|------------|-----------|----------|-----|
| NYC -> LA          | full    | cold (3 errors) |  0 |    nan |    nan |          - |         0 |        0 |   - |
| NYC -> LA          | full    | warm-ORS        | 10 |  153.8 | 7140.1 |      123.6 |         1 |        2 | 171 |
| NYC -> LA          | full    | cached          | 20 |   35.2 |   48.2 |        8.9 |         0 |        0 | 171 |
| NYC -> LA          | lean    | cold            |  3 | 1459.1 | 1523.2 |     1438.7 |         3 |        3 |   4 |
| NYC -> LA          | lean    | warm-ORS        | 10 |   94.6 |  143.3 |       84.7 |         0 |        0 |   4 |
| NYC -> LA          | lean    | cached          | 20 |   49.8 |   50.7 |        2.1 |         0 |        0 |   4 |
| Chicago -> Houston | full    | cold            |  3 |  912.4 |  959.2 |      896.8 |         3 |        3 |  63 |
| Chicago -> Houston | full    | warm-ORS        | 10 |   67.2 |   72.5 |       51.2 |         0 |        0 |  63 |
| Chicago -> Houston | full    | cached          | 20 |   62.1 |   77.0 |        5.2 |         0 |        0 |  63 |
| Chicago -> Houston | lean    | cold            |  3 |  863.3 |  876.8 |      854.4 |         3 |        3 |   3 |
| Chicago -> Houston | lean    | warm-ORS        | 10 |   44.4 |   47.7 |       36.3 |         0 |        0 |   3 |
| Chicago -> Houston | lean    | cached          | 20 |   49.9 |   55.7 |        2.1 |         0 |        0 |   3 |
| Dallas -> Austin   | full    | cold            |  3 |  415.8 |  425.7 |      407.0 |         3 |        3 |  16 |
| Dallas -> Austin   | full    | warm-ORS        | 10 |   27.2 |   29.7 |       17.5 |         0 |        0 |  16 |
| Dallas -> Austin   | full    | cached          | 20 |   52.8 |   57.3 |        2.9 |         0 |        0 |  16 |
| Dallas -> Austin   | lean    | cold            |  3 |  401.9 |  457.1 |      393.1 |         3 |        3 |   3 |
| Dallas -> Austin   | lean    | warm-ORS        | 10 |   21.9 |   31.8 |       14.6 |         0 |        0 |   3 |
| Dallas -> Austin   | lean    | cached          | 20 |   49.9 |   53.0 |        2.0 |         0 |        0 |   2 |

p50/p95: client wall time incl. HTTP and JSON transfer; server p50: meta.timings_ms.request_total; ORS calls/attempts: summed over the phase.

Note: this run used an earlier version of the script. Every NYC → LA "full" cold request failed upstream (ORS read timeouts, returned as 504). As a result, the first NYC → LA "full" warm-ORS request had to fetch the route itself, which accounts for its 1 call and the 7.1 s p95. The script now primes the route before the warm-ORS phase.
Cached requests show a constant ~40–45 ms on the dev server even for 3 KB bodies. That is the dev server's small TCP writes interacting with delayed ACKs on kept-alive connections; gunicorn does not show it (see 2026-10-18-gunicorn.md).
