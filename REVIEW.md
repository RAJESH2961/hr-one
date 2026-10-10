# REVIEW.md

Defects found in the starter `app/main.py` (all fixed in the current file).

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `MongoClient(...)` | Not `tz_aware`: PyMongo returns naive datetimes, so later arithmetic mixes naive and aware values or silently treats UTC as local time. | Punch-out against a stored punch-in raises `TypeError` (naive vs aware) → 500. | `tz_aware=True`, plus `as_utc()` guard for any naive value. |
| 2 | module level / startup | No indexes are created: there is no unique key on `employees.emp_code` or `attendance_logs(emp_code,date)`, and every list/analytics query is a COLLSCAN. | `/admin/explain` shows COLLSCAN; races create duplicates. | `ensure_indexes()` in the lifespan hook (idempotent, retried from `/health`). |
| 3 | `health` | Always returns 200, even when MongoDB is down. | Stop mongod → `/health` still says ok. | `ping` the server; 503 on failure. |
| 4 | `compute_late_minutes` | `punch_in.replace(hour=…)` builds shift start in the punch's own time zone (UTC/naive), not IST. | Shift 09:30 IST, punch 09:28 IST (03:58Z) → shift start computed as 09:30Z → 0, and a 10:05 IST punch also gives 0. | Shift start built from the IST attendance date (`shift_bounds`). |
| 5 | `compute_late_minutes` | Grace compares whole **minutes** `> 10`, so 09:40:59 → 10 minutes → treated as on time. R2 requires strictly more than 10:00 (seconds). | 09:40:01 must give 10; starter gives 0. | Compare seconds `> 600`, then floor minutes. |
| 6 | `compute_late_minutes` | No overnight handling: a 00:30 punch for a 22:00 shift is "before" the shift start on the same date. | Overnight shift late punch gives 0 instead of 150. | Late is measured from the start of the shift of the attendance date (R1). |
| 7 | `compute_work_hours` | `round()` on a float is banker's rounding / binary-float rounding, not half-up. | 4h29m42s = 4.495 h → starter returns 4.49 (and then `half_day` true); correct is 4.50. | `Decimal(seconds)/3600` quantized `ROUND_HALF_UP`. |
| 8 | `compute_overtime` | No 30-minute minimum (R3). | 18:35 punch-out → 5 min overtime; should be 0. | Return 0 below 30. |
| 9 | `compute_overtime` | Shift end is put on `date_str` even for overnight shifts (the end is the next day). | 22:00–06:00, out 06:40 next day → ~1480 min overtime. | `shift_bounds` adds a day when `shift_end <= shift_start`. |
| 11 | all helpers / punch-in | Instants not truncated to whole seconds (R1). | `punched_at=…900` ms stored with .900 and changes late calc. | `ms_to_dt` truncates to seconds; that value is stored and returned. |
| 12 | `EmployeeIn` | No validation: any `emp_code`, email, HH:MM, `joined_on`, equal shift start/end accepted. | `{"emp_code":"x","shift_start":"99:99"}` → 201. | Pydantic patterns/lengths from openapi, real-date check, `shift_start != shift_end`. |
| 13 | `create_employee` | Check-then-insert: two concurrent creates both pass `find_one` and both insert. | Fire 10 parallel creates → several 201s. | Unique index + catch `DuplicateKeyError` → 409. |
| 14 | `create_employee` | `datetime.now()` is local naive time (stored as if UTC, wrong by +5:30 on an IST box), and the response returns a datetime string, not epoch ms. | `created_at` is an ISO string / off by hours. | `datetime.now(UTC)`; response converts to epoch ms. |
| 15 | `list_employees` | `skip = page * page_size` skips the first page. | `page=1` returns rows 21–40. | `(page-1) * page_size`. |
| 16 | `list_employees` | `total` counts the whole collection, ignoring the department filter (R10). | `?department=Sales` → total = all employees. | `count_documents(q)`. |
| 17 | `list_employees` | No sort (contract: `emp_code` asc) and no bounds on `page`/`page_size` (`page=0`, `page_size=10000`, negative skip → 500). | `page=0` → negative skip error 500. | `.sort("emp_code")`, `Query(ge=1)`, `le=100`. |
| 18 | `list_employees` | `if department:` treats `""` as no filter. | `?department=` returns everybody. | `is not None`. |
| 19 | `list_employees` / `create_employee` | `created_at` returned as datetime, not epoch ms. | Response shape doesn't match `Employee` schema. | `employee_out()`. |
| 20 | `punch_in` | Unknown employee → `emp` is `None` → `emp["shift_start"]` TypeError → 500 (and the check happens after other work). | `{"emp_code":"EMP9999"}` → 500. | 404 first via `get_employee`. |
| 21 | `punch_in` | `datetime.fromtimestamp(ms/1000)` uses the server's local zone and the date is the server-local date, not IST (R1). | On a UTC server a 01:00 IST punch lands on the previous day. | Aware UTC datetime; date = IST date with overnight rule (`attendance_date`). |
| 22 | `punch_in` | No overnight rule (punch before `shift_end` belongs to the previous day's shift). | 00:30 punch on a 22:00–06:00 shift creates a new day's record. | `attendance_date()`. |
| 23 | `punch_in` | `punched_at` not validated: seconds, floats, strings ("123"), out-of-range accepted (pydantic lax int coerces `"1783…"` and `1.0`). | `punched_at: 1783312500` (seconds) → a 1970 record. | Strict int, `ge=1e11`, `le=4102444800000` → 422. |
| 24 | `punch_in` | `status` is any string (ABSENT/LEAVE/garbage). | `status:"FOO"` → 201. | `Literal["PRESENT","WFH","ON_DUTY"]`. |
| 25 | `punch_in` | Check-then-insert race: simultaneous punch-ins both pass `find_one` and insert duplicates. | 10 parallel identical punch-ins → several 201s. | Unique `(emp_code,date)` index; `DuplicateKeyError` → 409. |
| 26 | `punch_in` | `if body.punched_at` treats 0 as missing (falsy). | Minor; 0 now rejected by validation anyway. | `is not None`. |
| 27 | `punch_in` | Response leaks an `id` (ObjectId string) and returns datetimes instead of epoch ms. | Response has `id`, `punch_in` is an ISO string. | `record_out()`; `_id` never returned. |
| 28 | `list_attendance` | Loads the entire filtered collection into memory and sorts/paginates in Python. | Slow / memory heavy at 100k; no index in explain. | `find().sort().skip().limit()` + `count_documents`, backed by indexes. |
| 29 | `list_attendance` | Sort only by `date` desc; missing tie-break `emp_code` asc → unstable pages. | Same-day rows appear in random order across pages. | Sort `[("date",-1),("emp_code",1)]`. |
| 30 | `list_attendance` | No validation: bad dates, `date_from > date_to`, unknown status, `page<1`, `page_size>100`. | `?date_from=2026-07-09&date_to=2026-07-01` → 200 empty; `page=0` → wrong slice. | Date parsing, 422 on reversed range, `Status` enum, `Query` bounds. |
| 31 | `list_attendance` | Leaks `id`; returns datetimes not epoch ms; legacy docs missing `half_day`/`history` are returned without defaults. | Schema mismatch on sample record 9. | `record_out()` fills defaults. |

## Looked at and decided were not defects
- `IST = timezone(timedelta(hours=5, minutes=30))`: correct, IST has no DST.
- `int(x / 60)` truncation in the starter: for positive values it equals floor; my code uses `//` so negatives also floor.
- Storing dates as `"YYYY-MM-DD"` strings and comparing them lexicographically for ranges: correct because the format is fixed-width.
- Using `emp_code` (not `_id`) as the identifier: matches the contract.
- `load_dotenv()`: it does not override real environment variables, so real env already wins.
