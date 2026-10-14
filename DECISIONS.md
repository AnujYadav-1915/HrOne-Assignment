# DECISIONS.md

1. **Indexes.**
   I indexed `employees.emp_code` (unique) for fast point lookups, and `(department, joined_on)` for department filtering and headcount queries. On `attendance_logs`, I added a unique index on `(emp_code, date)` to prevent duplicate punch-ins, `(date, emp_code)` for sorted pagination, `(emp_code, punch_in)` to quickly find the open record during punch-out, and `(date, late_minutes)` for the late leaderboard. I considered and rejected indexing `history` because it is an audit trail array and not queried directly.

2. **Punch-in race.**
   When two identical requests arrive at the same time, both compute the attendance date and attempt to insert into `attendance_logs`. Because of the unique compound index on `(emp_code, date)`, MongoDB atomically allows only the first insert to succeed (returning 201). The second insert fails with a `DuplicateKeyError`, which my code catches and turns into a clean 409 Conflict without race conditions.

3. **Ties.**
   I used `$setWindowFields` with `$rank` so tied employees share the same rank and subsequent ranks are skipped (1, 2, 2, 4). The `limit` filter is checked after ranking (`rank <= limit`), and tied rows are ordered by `emp_code` ascending. If two employees tie at the cutoff rank, both are included in the response so no one is cut off arbitrarily.

4. **Headcount.**
   The department summary aggregation starts from the `employees` collection instead of attendance logs. It matches employees where `joined_on <= month_end`, groups them by department to calculate the headcount, and then uses a `$lookup` to join their attendance logs. This ensures employees who haven't logged any attendance are still counted in headcount.

5. **One thing you would change.**
   For 100x the data, running aggregations across millions of raw logs on every request would become slow. I would shard `attendance_logs` on `emp_code` and pre-calculate daily and monthly stats asynchronously into a separate summary collection whenever attendance is recorded or regularized.
