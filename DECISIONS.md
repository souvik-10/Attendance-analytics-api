# Design Decisions

## 1. Indexes
The unique `employees.emp_code` index enforces employee-code uniqueness. The `employees.department` index supports department filtering. The unique `attendance_logs(emp_code, date)` index prevents duplicate daily attendance records and supports employee/date lookups. The `attendance_logs(date, emp_code)` index supports attendance sorting, and `attendance_logs(date, status)` supports date and status filtering. I would avoid adding indexes to rarely queried derived fields because extra indexes increase write and storage costs.

I considered indexing derived work-hours fields but rejected it because the current queries do not filter on them.

## 2. Concurrent Punch-In Requests
Two requests may attempt the same insert simultaneously. The unique `(emp_code, date)` index prevents both records from being stored. One request succeeds; the duplicate-key error from the other should be translated to HTTP 409.

## 3. Leaderboard Ties
Competition ranking produces ranks such as 1, 2, 2, 4. Filtering by rank preserves all employees tied at the cutoff, even if the result contains more rows than the requested limit.

## 4. Employees Without Attendance Logs
The department summary starts from eligible employees and preserves employees with no matching attendance records. They still contribute to headcount.

## 5. Scaling by 100x
I would introduce scheduled or event-driven daily and monthly summary collections to reduce repeated aggregation over raw attendance records, while retaining the original logs for auditing.
