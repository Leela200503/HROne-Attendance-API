# DECISIONS.md

1. **Indexes.**
   - `employees`: Unique index on `emp_code`. Guarantees unique IDs and speeds up joins.
   - `attendance_logs`: Unique compound index on `(emp_code, date)`. Ensures at most one record per employee per day and speeds up `$lookup` aggregations by `emp_code`.
   - `attendance_logs`: Index on `date`. Speeds up the `list_attendance` filtering, the `leaderboard`, and `department_trend` queries which filter by a month/date range.
   - *Considered and rejected*: Indexing `late_minutes` directly. While it might speed up counting late days, `date` + `emp_code` is typically selective enough and we must compute over a date range anyway.

2. **Punch-in race.** 
   Two identical punch-in requests arrive at the same instant. Both calculate the same `emp_code` and `date`. Both attempt an `insert_one()`. Thanks to the unique index on `(emp_code, date)`, MongoDB atomically succeeds for one and throws a `pymongo.errors.DuplicateKeyError` for the other. The code catches this specific exception and correctly returns a 409 Conflict.

3. **Ties.** 
   When two employees tie on late minutes at the cutoff limit (e.g. they both share rank 10), the leaderboard returns both of them (yielding 11 results). The MongoDB `$setWindowFields` pipeline uses `rank` to group their scores, assigning both rank 10, which satisfies the `<= limit` logic. Standard competition ranking assigns them the same rank and skips the subsequent rank.

4. **Headcount.** 
   The department summary pipeline performs a `$lookup` from the `employees` collection (as the base driver) rather than starting from `attendance_logs`. By matching `department` and filtering `joined_on <= end_of_month`, all eligible employees enter the pipeline. If they have zero logs, the joined array is simply empty, but the employee still contributes to the headcount count.

5. **One thing you would change.** 
   If this had to serve 100x the data, running complex `$lookup` and `$setWindowFields` pipelines on raw logs per API call would become a bottleneck. I'd migrate the analytics endpoints to read from an asynchronously updated data warehouse, or use materialized views / `$merge` pipelines on a nightly cron job to pre-calculate daily and monthly aggregates.
