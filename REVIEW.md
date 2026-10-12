# Starter Code Review

The following defects were identified in the starter implementation and addressed in the submitted API.

| # | Where (function / area) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Late-arrival rules may not correctly enforce the 10-minute grace period. | Test arrivals exactly 10 minutes late and more than 10 minutes late. | Applied the grace-period rule and calculated whole late minutes. |
| 2 | `compute_work_hours` | Python's default rounding may not follow the required half-up rounding rule. | Test values at rounding boundaries. | Used decimal half-up rounding to two decimal places. |
| 3 | `compute_overtime` | Overnight shifts and the minimum overtime threshold need explicit handling. | Test overnight shifts and overtime durations below and at 30 minutes. | Handled overnight shift ends and applied the 30-minute minimum. |
| 4 | Employee creation | Duplicate employee codes must be rejected consistently. | Submit the same employee code twice. | Added a unique index and duplicate-key handling. |
| 5 | `list_employees` | Pagination can skip the wrong number of records, and the total may not reflect all filters. | Test page 1, page 2, and department filtering. | Applied database-side filtering, counting, sorting, and pagination. |
| 6 | Attendance punch-in | Local-time conversion and duplicate attendance creation can produce incorrect records. | Test IST date boundaries, overnight shifts, and duplicate punch-ins. | Applied IST-based attendance-date logic and database uniqueness enforcement. |
| 7 | Attendance listing | Fetching records and sorting or slicing them in Python is inefficient. | Request a large attendance dataset and inspect query behavior. | Moved filtering, sorting, and pagination into MongoDB. |
| 8 | Attendance punch-out | Punch-out needs validation for missing records and invalid timestamps. | Test an unknown employee, a missing punch-in, or a punch-out before punch-in. | Added validation and conditional updates. |
| 9 | Attendance regularization | Changes require validation and an audit trail. | Test invalid durations, no-op updates, and valid corrections. | Added validation, before/after history, reason tracking, and concurrency checks. |
| 10 | Monthly employee analytics | Attendance metrics must distinguish working days, leave, and attendance statuses. | Test employees with no logs, leave records, and weekend attendance. | Used MongoDB aggregation and explicit working-day and status rules. |
| 11 | Department summary | Employees without attendance logs must still count toward headcount. | Add an employee who has no attendance records. | Counted eligible employees independently of attendance-log presence. |
| 12 | Late leaderboard | Ties need competition ranking, including ties at the requested cutoff. | Test equal late-minute totals and a tie at the limit. | Used database ranking and included all records tied at the cutoff rank. |
| 13 | Department trend | Missing dates and weekends must not disappear from the daily series. | Test a date range with no attendance records on some days. | Generated a continuous date series and calculated the seven-day moving average. |
| 14 | Explain endpoint | Query performance needs to be inspectable. | Call the explain endpoint and inspect the winning query plan. | Added execution-statistics explain output for supported analytics and attendance queries. |

## Items reviewed that are not necessarily defects

- A weekend attendance log is not automatically invalid; weekend records can still contribute to late-arrival and overtime metrics.
- An employee with no attendance logs should still be included in headcount when eligible for the reporting period.
- Competition ranking intentionally skips rank numbers after ties, such as `1, 2, 2, 4`.
- A syntax check or successful sample request does not prove correctness for every hidden evaluation case.
