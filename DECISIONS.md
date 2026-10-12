# Design Decisions

1. **Indexes.** A unique index on `employees.emp_code` prevents duplicate employees. A unique compound index on `attendance_logs.emp_code` and `date` prevents duplicate daily attendance records. Other indexes support department, date, employee, and status queries. I would reject overlapping indexes without query-plan evidence because they increase storage and write costs.

2. **Punch-in race.** Two requests may both initially find no record. The unique compound index allows only one insert for the same employee and date. The other insert raises a duplicate-key error and returns a conflict response.

3. **Ties.** The leaderboard uses competition ranking. If employees tie at the cutoff rank, all employees with that rank are returned, even if the result exceeds `limit`.

4. **Headcount.** The department summary starts from eligible employees and looks up their attendance logs. Employees without logs remain included in headcount.

5. **Scaling 100x.** I would benchmark realistic workloads, inspect query plans, and tune indexes based on measured performance. I would also review connection-pool settings and add monitoring before introducing more infrastructure.
