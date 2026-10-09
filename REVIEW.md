# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Uses naive local times for `start` calculation instead of UTC, and doesn't handle overnight shifts (R1). | Passing a punch-in before midnight for an overnight shift gives incorrect late minutes. BSON datetimes convert incorrectly. | Switched to UTC calculations; shift `start` calendar day is resolved correctly to handle the overnight edge case. |
| 2 | `compute_late_minutes` | Uses `int()` which truncates towards zero rather than mathematically flooring as per R2. | A late arrival of exactly 10 min 59 sec gives 10 (not late), which is technically correct here but risks subtle bugs. | Used `math.floor` explicitly on total seconds / 60. |
| 3 | `compute_work_hours` | Uses Python's `round` (banker's rounding) instead of standard half-up rounding (R4). | Duration of e.g., 2.505 hours would round to 2.50 instead of 2.51. | Used `Decimal(str(val)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)`. |
| 4 | `compute_overtime` | Assumes the `shift_end` is on the same calendar day as `date_str`, breaking overnight shifts (R3). | Punching out of a 22:00-06:00 shift incorrectly computes a negative or huge overtime. | For overnight shifts, added 1 day to the calendar date before combining with `shift_end`. |
| 5 | `create_employee` | Generates a naive local `created_at` timestamp rather than UTC. Misses BSON format conversion logic for the response. | `created_at` appears in local time in DB, and returns a datetime string rather than integer epoch ms. | Used `datetime.now(timezone.utc)` and mapped all response datetimes to integer epoch ms. |
| 6 | `create_employee` | Lacks validation ensuring `shift_start` differs from `shift_end`. | Client creates an employee with identical shift start and end. | Added Pydantic validator on `EmployeeIn` and explicitly checked in endpoint. |
| 7 | `list_employees` | `total` is hardcoded to count all documents regardless of `department` filter. | `GET /employees?department=XYZ` returns `total` equal to entire company headcount. | Passed the same query `q` to `count_documents(q)`. |
| 8 | `list_employees` | `skip` is calculated as `page * page_size`, which skips the first page. | `page=1` returns items 21-40. | Changed to `(page - 1) * page_size`. |
| 9 | `list_employees` | Does not sort by `emp_code` ascending as required. | Results are returned in MongoDB natural insertion order. | Chained `.sort("emp_code", pymongo.ASCENDING)` to the query. |
| 10| `punch_in` | Uses local naive `datetime` instead of UTC. Blindly uses `ts.date()` for attendance date, ignoring overnight shift R1. | Punch-ins before midnight for overnight shifts fall on the wrong attendance day. | Truncated to seconds and correctly computed the attendance date using R1 logic. |
| 11| `punch_in` | Does not check if the employee exists before dereferencing `emp["shift_start"]`. | Requesting a punch-in for a non-existent `emp_code` throws `TypeError`. | Added explicit null check and raise `HTTPException(404)`. |
| 12| `list_attendance`| Pulls entire collection into memory before slicing, and incorrectly sorts only by date in Python. | Application OOMs on 100k+ records. Missing secondary sort by `emp_code`. | Used MongoDB's native `.sort([("date", -1), ("emp_code", 1)])`, `.skip()`, and `.limit()`. |
| 13| `list_attendance`| Exposes MongoDB's internal `_id` as `id` instead of hiding it as per conventions. | Responses contain `"id": "5f3a..."`. | Used `d.pop("_id", None)` and removed `"id"` injection logic across endpoints. |

Also note anything you looked at and decided was **not** a defect, and why:
- I didn't consider the lack of `$setWindowFields` usage in Python code a defect since analytics are meant to be built with MongoDB pipelines anyway.
- The starter code's use of synchronous `pymongo` vs `motor` is not a defect. Either approach is perfectly valid as long as it scales properly.
