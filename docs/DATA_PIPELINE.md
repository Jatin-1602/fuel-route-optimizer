# Data pipeline

The pipeline turns `data/fuel-prices-for-be-assessment.csv` (the provided file) into geocoded stations in the database. All outputs are committed, so reviewers only run `migrate` and `load_stations`.

```
fuel-prices CSV ──clean──► 6,626 US stations ──offline match──► 6,454 ──ORS fallback (cached)──► 6,604 (99.67%)
                                                    ▲
                    Census Gazetteer 2026 ──build_places──► us_places.csv (46,949) + us_counties.csv (3,091)
```

## 1. What is in the CSV

From `python scripts/profile_csv.py`:

| Statistic | Value |
|---|---|
| Columns | `OPIS Truckstop ID, Truckstop Name, Address, City, State, Rack ID, Retail Price` |
| Data rows | 8,151 |
| Unique OPIS IDs | 6,738 (**678 IDs appear more than once**, up to 6 rows) |
| … duplicates with different prices / names / city | 597 / 227 / 0 |
| Unique (city, state) pairs | 3,893 (trimmed, case-insensitive) |
| State codes | 57: 48 US states plus **9 Canadian provinces/territories** |
| Canadian rows | 620 (ON 217, AB 180, BC 121, MB 42, SK 36, YT 8, QC 6, NS 6, NB 4) |
| US states with no stations | **AK, HI, DC** |
| Prices | $2.687 – $6.399 (mean $3.499, median $3.432); 0 invalid |
| Whitespace issues | 1,356 fields with leading/trailing spaces, 1,393 with double spaces (e.g. `"I-35,  EXIT 271"`) |
| Coordinates | **none**. Addresses are highway exits (`"I-44, EXIT 283 & US-69"`) that can't be geocoded as street addresses |

## 2. Cleaning (`apps/stations/services/cleaning.py`)

1. Trim and collapse whitespace in every field.
2. Keep the **50 states + DC** only. That drops the 620 Canadian rows.
3. Drop rows with a non-numeric ID or a missing or non-positive price (none in this file).
4. **De-duplicate by OPIS ID, keeping the lowest price.** This merged 905 rows; 487 US IDs had conflicting prices.
   - *Why the lowest:* it's the same physical stop, and a cost-minimising driver pays the lowest posted price. On a tie the first row wins, so output is deterministic. The number of merged rows is kept in `source_rows`.
   - *Alternative:* averaging the duplicates is more conservative and would be a one-line change.

Result: **6,626 unique US stations.** Prices keep all 8 decimals from the CSV (`DecimalField(12, 8)`).

## 3. Offline gazetteer (`manage.py build_places`)

**Source:** US Census Bureau **Gazetteer files, 2026 vintage** (public domain): *places* (incorporated places and census-designated places), *county subdivisions* (townships, New England towns, CCDs) and *counties*. Each row has an internal-point latitude and longitude.

> The 2026 files are **pipe-delimited**; earlier vintages are tab-delimited. The parser detects the delimiter from the header line.

### Name normalisation (`normalize.py`)

| Rule | Example |
|---|---|
| Lowercase, strip accents and punctuation, collapse spaces | `"Española"` → `espanola`, `"O'Fallon"` → `ofallon` |
| Synonyms | saint → st, sainte → ste, fort → ft, mount → mt, mountain → mtn, twp → township |
| `Mc` prefix joined | `"MC DERMITT"` → `mcdermitt` |
| `-boro` → `-borough` | `"Middlesboro"` → `middlesborough` |
| Leading N/S/E/W expanded | `"S Coffeyville"` → `south coffeyville` |
| Census descriptors stripped **case-sensitively** (they're lowercase in Census data) | `"Kansas City city"` → `kansas city`, while `"Carson City"` keeps its "City" |
| Descriptors handled | city, town, village, borough, CDP, CCD, UT, township, charter township, municipality, plantation, comunidad, zona urbana, urban county, (balance), … government |

**Aliases** rank below primary names:
- `"San Buenaventura (Ventura) city"` → also `ventura`
- consolidated city-counties such as `"Nashville-Davidson metropolitan government (balance)"`, `"Macon-Bibb County"` and `"Lexington-Fayette urban county"` → also `nashville`, `macon`, `lexington`
- `"Boise City city"` → `boise`; `"Bridgewater Town city"` → `bridgewater`

**Priority** when names collide within a state: incorporated place > CDP > civil county subdivision > statistical subdivision (CCD), then primary name > alias, then larger land area.

**Output:** `data/us_places.csv` with **46,949** entries (19,898 incorporated, 12,470 CDP, 11,423 civil county subdivisions, 3,158 statistical), and `data/us_counties.csv` with **3,091** counties, parishes, boroughs and census areas. The same file powers request-time `"City, ST"` lookups.

## 4. Offline matching (`manage.py geocode_stations`)

Lookup order, always **within the CSV's state**:
1. The exact normalised name, also tried with a trailing "township" removed (`"Monroe Township"`).
2. The name with spaces removed (`"De Forest"` = `"DeForest"`, `"La Place"` = `"Laplace"`).
3. difflib fuzzy match with cutoff **0.95**.

The fuzzy cutoff is deliberately strict. At 0.88, `"Middleton, MI"` matched `"Middletown, MI"`, a different village about 35 miles away, and the correct `"Grovertown → Groverton"` match had *the same score*. A wrong coordinate is worse than a missing station ([ADR-003](DESIGN_DECISIONS.md)).

**Result:** 6,454 / 6,626 = **97.40%** offline (6,441 exact, 13 ignoring spaces, 0 fuzzy). The other 172 stations are in 130 cities the Census doesn't list: unincorporated hamlets (Ruther Glen VA, Breezewood PA, Jean NV), neighbourhoods of big cities (Antioch TN, Curtis Bay MD), and county names used as city names (Henrico VA, Chesterfield VA).

**Sanity checks:**
- every coordinate lies inside the contiguous US;
- the only stations more than 450 miles from their state's median are in the El Paso area, which is legitimate;
- re-running the pipeline produces byte-identical files.

## 5. ORS fallback for the remaining cities (`--ors-fallback`)

Each unique unmatched (city, state) pair is queried **once**: ORS Pelias structured search, up to 5 results, at least 0.7 s apart. The raw answers are stored in the committed **`data/ors_geocode_cache.json`** (130 entries). Every later run, with or without the flag, applies the cache with **0 calls**, and acceptance is re-evaluated from it each time.

A result is accepted only if all of these hold:

| # | Rule | Why |
|---|---|---|
| 1 | layer is `locality` or `localadmin` | Rejects answers that fell back to a whole state or county |
| 2 | country is USA | |
| 3 | state equals the CSV state | |
| 4 | inside that state's bounding box (from Gazetteer points, padded 0.25°, antimeridian-aware for Alaska) | |
| 5 | **Ambiguity:** prefer results whose name equals the CSV city; reject if they're more than 25 mi apart | Pelias's top answer for "Antioch, TN" is one of **five** Antiochs in Tennessee, ~110 mi from the I-24 truck stops; for "Sawyer, MI" the right Sawyer was result #2 |
| 6 | **County collision:** if the CSV city is also a county name in that state, the result must lie in that county | "Henrico, VA" resolved to a hamlet ~100 mi from Henrico County |

Rules 5 and 6 were added after auditing a run that used only rules 1–4, which accepted those wrong places ([ADR-007](DESIGN_DECISIONS.md)).

**Result:** 113 of 130 cities accepted (150 stations, `geocode_source = "ors"`), giving **6,604 / 6,626 = 99.67%**.

**Rejected (17 cities, 22 stations)**, with reasons kept in `data/unmatched_cities.csv`:

| Reason | Cities |
|---|---|
| Ambiguous (rule 5), 10 | Antioch TN, Cedar Grove TN, Chesterfield VA, Denmark TN, Irvington AL, Ivydale WV, Knoxville AL, Mount Nebo WV, Rosedale VA, Saint Johns FL |
| County collision (rule 6), 4 | Henrico VA, Pendleton KY, Edwards IL, Lawrence MS |
| Only a state-level answer (rule 1), 3 | Bois D Arc MO, Joelton TN, Mc Alpin FL |

The county rule is conservative: Pendleton KY, Edwards IL and Lawrence MS were probably correct. We lose 3 stations to avoid placing a stop about 100 miles away.

## 6. Loading (`manage.py load_stations`)

This is an idempotent upsert keyed on `opis_id` (`bulk_create(update_conflicts=True)`), and it deletes stations that are no longer in the file, all in **one transaction**. Running it twice gives created 0 and updated *n* on the second run. Running servers keep their in-memory index until they restart.

## 7. Accuracy limits

- Coordinates are the **city's internal point**, not the exit. A large city's centroid can be several miles from its truck stops. The 10-mile corridor absorbs this, and `off_route_miles` is reported per stop.
- Stations sharing a city share one coordinate, so they're merged at the same mile marker, keeping the cheapest.
- Prices are a static snapshot.

## 8. Refreshing prices

1. Replace `data/fuel-prices-for-be-assessment.csv` with a new export (same columns).
2. Run `python manage.py geocode_stations`. New cities are matched offline; to try ORS for new unmatched cities, add `--ors-fallback` (uses the API key and only queries uncached pairs).
3. Run `python manage.py load_stations` and restart the server. The `data_version` in responses changes, so the response cache naturally misses. `python manage.py clear_cache` also works.

With live prices, the CSV step would become a scheduled import job; see Future work in [DESIGN_DECISIONS.md](DESIGN_DECISIONS.md).
