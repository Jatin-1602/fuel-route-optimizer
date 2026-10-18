# Fuel-stop optimization

Code: [`apps/routing/services/optimizer.py`](../apps/routing/services/optimizer.py) (pure Python and numpy, no Django). Tests: [`tests/routing/test_optimizer.py`](../tests/routing/test_optimizer.py).

## 1. Problem

| Symbol | Meaning |
|---|---|
| `D` | route length in miles (from the routed polyline) |
| `m_i`, `p_i` | mile marker and price of station *i* (stations within the corridor, projected onto the route) |
| `R` | maximum range on a full tank (default 500 mi) |
| `mpg` | fuel efficiency (default 10) |
| `C = R / mpg` | tank capacity (default 50 gal) |

Assumptions:
- Fuel use is linear in distance.
- Any amount can be bought at a stop.
- The route is fixed (we optimize *where to buy*, not *which road to take*).
- The trip ends with an (almost) empty tank.

Two objectives are supported:
1. **Pure cost** (`stop_penalty = 0`): minimise money spent on fuel.
2. **Cost-effective** (default `stop_penalty = 15`): minimise *fuel cost + stop_penalty × number of stops*. The assignment says "optimal mostly means cost effective", and a plan that stops for one gallon to save three cents isn't cost-effective for a driver ([ADR-008](DESIGN_DECISIONS.md)).

Stations at the same mile marker are merged first, keeping the cheapest and then the one closest to the road. Several stations in one city share that city's centroid.

## 2. Pure cost: the greedy

This is the classic single-route *gas station problem* (Khuller, Malekian & Mestre, *To fill or not to fill*, 2007). At the current station *i* with *f* gallons in the tank:

```
loop:
    in_range = stations ahead with m_j - m_i <= R
    if some station in in_range is cheaper than i:            # rule 1
        j = the NEAREST cheaper one
        buy max(0, (m_j - m_i)/mpg - f)     # just enough to reach j
        go to j
    elif D - m_i <= R:                                        # rule 2
        buy max(0, (D - m_i)/mpg - f)       # just enough to finish
        done
    elif in_range is not empty:                               # rule 3
        fill the tank (buy C - f)
        go to the CHEAPEST station in in_range
              (tie-break: smaller off-route distance, then nearer)
    else:                                                     # rule 4
        raise UnreachableRouteError(gap start, gap end)       # → HTTP 422
```

### Why it is optimal (exchange argument)

- **Rule 1.** Every gallon bought at *i* is burned before reaching *j*, because we arrive at *j* empty. If an optimal plan bought more at *i* than needed to reach *j*, the extra gallons would be burned after *j*. Buying them at *j* instead is cheaper and still feasible, since *j* is reachable and has capacity left. So "just enough to reach the nearest cheaper station" is never worse.
- **Rule 3.** If *i* is the cheapest station within one tank's reach, every gallon burned in the next *R* miles is cheapest when bought at *i*. Filling up therefore can't hurt. The next stop should be the cheapest station in range: any plan that buys fuel at a dearer station inside the range could shift that purchase to the cheapest one without losing feasibility.
- **Rule 2** is rule 1 with the destination treated as a free "station".
- **Rule 4:** if no fuel source lies within *R* miles, no plan exists.

## 3. Start-fuel modes

| `start_fuel` | Model | What `total_fuel_cost` means |
|---|---|---|
| `empty` (default) | The vehicle has just enough fuel to reach its first stop, which must be within `origin_radius_miles` (25). **Every** candidate first stop in that window is tried; the fuel burned to reach it is charged at that stop's price; the cheapest total wins. | Cost of **every gallon burned** on the trip |
| `full` | Starts with `C` gallons, which aren't charged | Money spent at the pump |
| a number, e.g. `12.5` | Starts with that many gallons (0 < x ≤ C), not charged | Money spent at the pump |

**Implementation trick for `full`/numeric start fuel.** Add a free *virtual station* `(C − f0) × mpg` miles **before** the start, with a full tank. Driving from it to mile 0 leaves exactly `f0` gallons, so the same greedy runs unchanged with no special start-node logic. Its "purchases" are free and aren't reported.

## 4. Worked example

Range 100 mi, 10 mpg (10-gallon tank), a 250-mile route, `start_fuel=empty`, origin radius 25 mi, `stop_penalty=0`.

| Station | A | B | C | D | E |
|---|---|---|---|---|---|
| Mile | 0 | 60 | 90 | 170 | 200 |
| $/gal | 3.00 | 3.50 | **2.80** | 3.20 | 3.60 |

| Step | At | Fuel on arrival | In range | Rule | Buy | Cost |
|---|---|---|---|---|---|---|
| 1 | A (mile 0) | 0 | B, C | 1: C is the nearest cheaper station (B costs more and is skipped) | 9 gal (to reach C) | $27.00 |
| 2 | C (mile 90) | 0 | D (E at 200 is beyond 190) | 3: nothing cheaper, finish 160 mi away → fill up, go to the cheapest in range (D) | 10 gal | $28.00 |
| 3 | D (mile 170) | 2 gal | E, finish (80 mi away) | 2: nothing cheaper ahead, finish within range → just enough | 6 gal | $19.20 |

The total is **$74.20** for 25 gallons. The baseline, at the average price of $3.22/gal, is $80.50, so the plan saves $6.30. This example is a unit test, and the brute-force DP confirms $74.20.

## 5. Cost-effective: stop-penalty DP

With a per-stop cost the greedy is no longer optimal. It stops at every slightly cheaper station. The DP relies on the known structure of optimal plans:

> In an optimal plan, every stop either **fills the tank** or buys **just enough to arrive empty** at the next stop (or at the destination).

That holds for the optimal purchases of *any* fixed set of stops, because of the greedy's exchange argument above. So the fuel on arrival at station *l* is always one of finitely many values:

- `0`, when the previous stop bought just enough to reach *l*;
- `C − d(j, l)/mpg`, when an earlier station *j* within range filled up;
- the start fuel minus the distance driven (`full`/numeric modes), or `0` at a first stop within the origin radius (`empty` mode, which also charges that fuel at the stop's price).

Each station is processed in mile order with a handful of vectorised numpy operations. For the states `(a, c)` at station *j* (fuel on arrival, cost so far), let `key = c − a·p_j`, the cost of the state if its fuel had been bought at *j*:

- **Fill up at *j*:** `F_j = min(key) + C·p_j + penalty`. That's one number per station, and it seeds the state `(C − d(j,l), F_j)` at every station *l* ahead within range.
- **Just enough to reach *l*:** `min over states with a ≤ d(j,l) of key + d(j,l)·p_j + penalty`. That's a prefix minimum over the states sorted by `a`, computed for every target *l* at once with one `searchsorted`.
- **Finish:** `min key + need·p_j + penalty` for states that can't already reach the destination.

Back-pointers rebuild the optimal **set of stops**. Then the greedy restricted to that set computes the purchases. The greedy is optimal for any fixed set, so the objective can't get worse, and the accounting code is shared.

### Results on the recorded real routes (500 mi, 10 mpg, empty start)

| Route | Pure optimum (penalty 0) | Penalty $15 (default) | Extra fuel cost | Largest leg | Optimizer time |
|---|---|---|---|---|---|
| NYC → LA, 2,811.6 mi, 455 corridor stations | 19 stops, $852.87 | **7 stops**, $860.57 | **+$7.69** | 482.5 mi | ~33 ms (DP) vs ~6 ms (greedy) |
| Chicago → Houston, 1,081.8 mi, 179 stations | 8 stops, $317.48 | **3 stops**, $319.53 | **+$2.05** | 475.5 mi | ~12 ms vs ~3 ms |

The penalised NYC → LA plan:

| # | Mile | Station | $/gal | Buy |
|---|---|---|---|---|
| 1 | 10.5 | Bolla Market, Elizabeth NJ | 3.099 | 39.05 gal |
| 2 | 401.0 | Sheetz #639, Youngstown OH | 3.059 | 46.00 gal |
| 3 | 861.0 | Caseys #3686, Utica IL | 2.969 | 48.25 gal |
| 4 | 1343.5 | Akal Travel Center, Waco NE | **2.799** | 50.00 gal |
| 5 | 1748.0 | 7-Eleven 42279, Keenesburg CO | 3.149 | 40.45 gal |
| 6 | 2127.0 | Maverik #693, Green River UT | 3.282 | 37.90 gal |
| 7 | 2523.0 | Maverik #674, North Las Vegas NV | 3.282 | 18.46 gal |

## 6. Complexity

With *n* stations after merging and *k* stations within one tank's range:

- **Greedy:** O(n·k) as a simple forward scan. A monotonic-stack "next cheaper station" precomputation would give O(n), but with n of a few hundred the scan is clearer and takes about 6 ms on NYC → LA. `empty` mode repeats it for each candidate first stop in the origin window (usually a handful).
- **DP:** O(n·k) array work (O(n·k log k) with the per-station sort), done as about 15 numpy calls per station.
- **Corridor search:** O(m log m) to build a KD-tree on the resampled route's *m* points (about 5,600 for NYC → LA), plus O(s log m) to query the *s* stations that survive the bounding-box prefilter. That's about 5 ms.

## 7. How optimality is tested

`test_optimizer_matches_brute_force_on_random_instances` is parametrised over **all three start-fuel modes**, with 300 seeded random instances each (900 in total):

- **Instances:** 0–13 stations, deliberately tied prices, duplicate mile markers, gaps longer than the range, ranges of 40–120 mi, and stop penalties drawn from {0, 0.5, 2, 5, 15, 40}.
- **Brute force:** an independent dynamic program over fuel levels in 0.1-gallon steps. At every station it considers skipping (free) and **every** purchase amount (plus the penalty), with no structural shortcuts and no merging.
- **Asserted:**
  - The optimizer's objective equals the brute force exactly on feasible instances, and both report infeasible ones.
  - Gallons paid = gallons burned in `empty` mode; initial fuel + purchases = burned + left over otherwise.
  - The tank is never over-filled, and every stop buys something.
  - A penalty never adds stops and never makes fuel cheaper than the pure optimum.
  - The generator produces enough of both outcomes to be meaningful.

The discretisation is exact here: distances are integer miles at 10 mpg, so optimal purchases are multiples of 0.1 gallon.

End-to-end tests also run both optimizers on the recorded NYC → LA and Chicago → Houston geometry against all 6,604 real stations.

## 8. Alternatives considered

| Approach | Why not (or not by default) |
|---|---|
| **Fill up at every stop** (stop when nearly empty, fill the tank) | Ignores prices. It may fill up just before a much cheaper station and pays for a full tank it doesn't need at the end. The greedy is never worse, and the brute-force test proves it optimal. "Fill up every time" is effectively one point in the space the DP searches. |
| **Always stop at the cheapest station in range** | Not optimal without the "just enough to reach a cheaper station" rule: it over-buys at mid-priced stations. |
| **Linear program** | Exact for pure cost but needs an LP solver dependency. With a per-stop penalty it becomes a MILP. The DP is exact and dependency-free. |
| **Generic DP over discretised fuel** | It's what the brute-force test does: correct, but O(n·C/Δ) states and too slow at 0.1 gal × 50 gal × hundreds of stations per request. |
| **Minimum purchase size instead of a stop penalty** | A hard threshold creates cliff effects and is harder to reason about. A cost per stop expresses the real trade-off. |
