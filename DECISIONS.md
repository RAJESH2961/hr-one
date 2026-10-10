# DECISIONS.md

1. **Indexes.** `employees.emp_code` unique (identity, 409 on duplicate create). `employees(department, joined_on)` for department filters, headcount and trend; `employees.joined_on` for the all-department summary. `attendance_logs(emp_code, date)` unique: one record per day, punch-in race safety, monthly summary and lookups. `(date -1, emp_code 1)` matches the list sort and the leaderboard range; `(status, date, emp_code)` for status filters; `(emp_code, punch_in -1)` finds the latest open record at punch-out. I rejected an index on `late_minutes`: the date range is already selective enough and it would only add write cost.

2. **Punch-in race.** Both requests compute the same `(emp_code, date)` and call `insert_one`. The unique index lets exactly one write succeed; the other gets `DuplicateKeyError`, which I turn into 409. There is no read before the insert.

3. **Ties.** Ranks come from `$rank` (1, 2, 2, 4) and I filter `rank <= limit`, so everyone tied at the cutoff is returned, ordered by `emp_code`. Cutting by row count would drop people arbitrarily.

4. **Headcount.** The summary starts from `employees` filtered by `joined_on <= month end`, then `$lookup`s their logs. Each employee is one row, even with an empty log array.

5. **At 100x.** Precompute daily per-department rollups on write instead of aggregating raw logs per request.
