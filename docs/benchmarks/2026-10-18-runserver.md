<!-- Output of scripts/benchmark.py on 2026-10-18 (Linux Mint 22.1, Python 3.12.12). Final configuration: directions read timeout 30 s, geocode 10 s, one retry on read timeout. Cold runs hit the live OpenRouteService API. -->

### Benchmark: runserver (Django dev server)

| route              | variant | phase                |  n | p50 ms |  p95 ms | server p50 | ORS calls | attempts |  KB |
|--------------------|---------|----------------------|----|--------|---------|------------|-----------|----------|-----|
| NYC -> LA          | full    | cold                 |  3 | 1541.5 |  2019.7 |     1511.7 |         3 |        3 | 171 |
| NYC -> LA          | full    | warm-ORS             | 10 |  149.1 |   168.4 |      118.0 |         0 |        0 | 171 |
| NYC -> LA          | full    | cached               | 20 |   34.1 |    63.9 |        9.1 |         0 |        0 | 171 |
| NYC -> LA          | lean    | cold (failed: 2x504) |  1 | 2474.8 |  2474.8 |     2463.6 |         1 |        1 |   4 |
| NYC -> LA          | lean    | warm-ORS             | 10 |   92.8 |   149.8 |       82.8 |         0 |        0 |   4 |
| NYC -> LA          | lean    | cached               | 20 |   49.6 |    51.4 |        2.2 |         0 |        0 |   4 |
| Chicago -> Houston | full    | cold                 |  3 | 1009.9 |  1052.1 |      993.1 |         3 |        3 |  63 |
| Chicago -> Houston | full    | warm-ORS             | 10 |   65.8 |    90.0 |       50.0 |         0 |        0 |  63 |
| Chicago -> Houston | full    | cached               | 20 |   60.2 |    69.6 |        4.7 |         0 |        0 |  63 |
| Chicago -> Houston | lean    | cold                 |  3 |  941.6 |   971.7 |      934.0 |         3 |        3 |   3 |
| Chicago -> Houston | lean    | warm-ORS             | 10 |   45.1 |    92.6 |       36.9 |         0 |        0 |   3 |
| Chicago -> Houston | lean    | cached               | 20 |   49.9 |    51.9 |        2.2 |         0 |        0 |   3 |
| Dallas -> Austin   | full    | cold                 |  3 |  471.7 |   472.7 |      463.4 |         3 |        3 |  16 |
| Dallas -> Austin   | full    | warm-ORS             | 10 |   27.2 |    32.1 |       17.7 |         0 |        0 |  16 |
| Dallas -> Austin   | full    | cached               | 20 |   52.5 |    53.7 |        2.8 |         0 |        0 |  16 |
| Dallas -> Austin   | lean    | cold                 |  3 |  699.0 | 27861.4 |      692.2 |         3 |        4 |   3 |
| Dallas -> Austin   | lean    | warm-ORS             | 10 |   21.9 |    52.1 |       14.2 |         0 |        0 |   3 |
| Dallas -> Austin   | lean    | cached               | 20 |   49.4 |    50.8 |        2.1 |         0 |        0 |   2 |

p50/p95: client wall time incl. HTTP and JSON transfer; server p50: meta.timings_ms.request_total; ORS calls/attempts: summed over the phase. Failures are upstream (ORS) errors returned as 502/503/504 (0 = client timeout).

Server log for this run: 16 successful directions calls (slowest 2.3 s) and 5 read timeouts at 30 s. In two NYC → LA lean cold requests both attempts stalled, giving 504 `routing_provider_timeout`. One Dallas → Austin stall was rescued by the retry (4 attempts for 3 calls).

Cached requests take about 50 ms here even for 2–4 KB bodies, while the server itself takes about 2 ms (`server p50`). This is the dev server's small TCP writes interacting with delayed ACKs on kept-alive connections; gunicorn serves the same requests in 7–12 ms (see 2026-10-18-gunicorn.md).
