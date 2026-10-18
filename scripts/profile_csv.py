"""Print data-quality statistics for the raw fuel-prices CSV.

Usage:
    python scripts/profile_csv.py [path/to/fuel-prices.csv]

Standard library only, so it runs without Django or a virtualenv.
"""

import csv
import sys
from collections import Counter, defaultdict
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "fuel-prices-for-be-assessment.csv"

CANADIAN_PROVINCES = {"AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT"}


def main(path: Path) -> None:
    """Profile the CSV at ``path`` and print a human-readable report."""
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        columns = reader.fieldnames or []
        rows = list(reader)

    ids = Counter(r["OPIS Truckstop ID"] for r in rows)
    dup_ids = {i: n for i, n in ids.items() if n > 1}
    by_id: dict[str, list[dict[str, str]]] = defaultdict(list)
    for r in rows:
        by_id[r["OPIS Truckstop ID"]].append(r)
    dup_diff_price = sum(1 for i in dup_ids if len({r["Retail Price"] for r in by_id[i]}) > 1)
    dup_diff_name = sum(1 for i in dup_ids if len({r["Truckstop Name"] for r in by_id[i]}) > 1)
    dup_diff_city = sum(
        1 for i in dup_ids if len({(r["City"].strip(), r["State"].strip()) for r in by_id[i]}) > 1
    )

    states = Counter(r["State"].strip() for r in rows)
    canadian = {s: n for s, n in states.items() if s in CANADIAN_PROVINCES}
    prices = []
    bad_prices = 0
    for r in rows:
        try:
            p = float(r["Retail Price"])
        except ValueError:
            bad_prices += 1
            continue
        if p <= 0:
            bad_prices += 1
        else:
            prices.append(p)

    untrimmed = sum(1 for r in rows for v in r.values() if v != v.strip())
    double_spaces = sum(1 for r in rows for v in r.values() if "  " in v)
    city_states = {(r["City"].strip().lower(), r["State"].strip()) for r in rows}

    print(f"File: {path}")
    print(f"Columns ({len(columns)}): {columns}")
    print(f"Data rows:                         {len(rows):>6}")
    print(f"Unique OPIS Truckstop IDs:         {len(ids):>6}")
    print(f"IDs appearing more than once:      {len(dup_ids):>6}  (max {max(ids.values())} rows)")
    print(f"  ...with differing prices:        {dup_diff_price:>6}")
    print(f"  ...with differing names:         {dup_diff_name:>6}")
    print(f"  ...with differing city/state:    {dup_diff_city:>6}")
    print(f"Unique (City, State) pairs:        {len(city_states):>6}")
    print(f"Distinct state codes:              {len(states):>6}")
    canadian_total = sum(canadian.values())
    print(
        f"Canadian rows:                     {canadian_total:>6}  {dict(sorted(canadian.items()))}"
    )
    print(f"Invalid / non-positive prices:     {bad_prices:>6}")
    print(f"Price range:                       ${min(prices):.3f} - ${max(prices):.3f}")
    mean, median = sum(prices) / len(prices), sorted(prices)[len(prices) // 2]
    print(f"Mean / median price:               ${mean:.3f} / ${median:.3f}")
    print(f"Fields with leading/trailing ws:   {untrimmed:>6}")
    print(f"Fields with double spaces:         {double_spaces:>6}")
    print(
        "Top 10 states by rows:            ",
        ", ".join(f"{s}={n}" for s, n in states.most_common(10)),
    )


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PATH)
