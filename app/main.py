"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (real environment variables take precedence over .env)
Database: MongoDB Atlas only (no local MongoDB or Docker fallback)
"""
import calendar
import math
import os
import re
from contextlib import asynccontextmanager
from datetime import date as dt_date
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

# --------------------------------------------------------------------------- #
# MongoDB Atlas Connection & Lifespan Index Setup
# --------------------------------------------------------------------------- #
MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

if not MONGO_URI:
    # Explicitly enforce MongoDB Atlas only; never silently fall back to localhost
    raise RuntimeError("MONGO_URI environment variable is required (MongoDB Atlas only).")

# tz_aware=True ensures BSON dates are loaded as timezone-aware UTC datetimes
client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


def init_indexes():
    """Create all required indexes idempotently at application startup."""
    # Employee indexes
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index("department")

    # Attendance indexes
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("date", 1), ("status", 1)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: create indexes idempotently
    try:
        init_indexes()
    except Exception as e:
        # If DB is not reachable at startup, let it log; /health will return 503
        pass
    yield
    client.close()


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Time & Business Logic Helpers (R1 - R5)
# --------------------------------------------------------------------------- #
def truncate_to_seconds(dt: datetime) -> datetime:
    """R1: Instants are truncated to whole seconds before calculation and storage."""
    return dt.replace(microsecond=0)


def epoch_ms_to_utc_dt(ms: int) -> datetime:
    """Convert integer epoch milliseconds to UTC datetime truncated to seconds."""
    truncated_sec = ms // 1000
    return datetime.fromtimestamp(truncated_sec, tz=timezone.utc)


def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert UTC datetime to integer epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def determine_attendance_date(punch_in_dt: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: A record's date is the IST calendar date of its punch_in.
    Overnight shifts (shift_end <= shift_start): a punch-in whose IST time is
    earlier than shift_end belongs to the shift that started the previous day.
    """
    dt_ist = punch_in_dt.astimezone(IST)
    is_overnight = shift_end <= shift_start
    if is_overnight:
        end_h, end_m = map(int, shift_end.split(":"))
        if dt_ist.time() < time(end_h, end_m):
            return (dt_ist.date() - timedelta(days=1)).isoformat()
    return dt_ist.date().isoformat()


def compute_late_minutes(punch_in_dt: datetime, shift_start: str, attendance_date: str) -> int:
    """
    R2: Grace period 10 minutes. Late only if punch-in is strictly more than 10:00
    after shift_start; then late_minutes = whole minutes (floor) since shift_start.
    """
    punch_in_ist = punch_in_dt.astimezone(IST)
    sh_h, sh_m = map(int, shift_start.split(":"))
    shift_date = dt_date.fromisoformat(attendance_date)
    shift_start_dt = datetime.combine(shift_date, time(sh_h, sh_m), tzinfo=IST)

    diff_sec = (punch_in_ist - shift_start_dt).total_seconds()
    if diff_sec <= 600:  # <= 10 minutes: within grace period
        return 0
    return max(0, int(math.floor(diff_sec / 60.0)))


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    diff_sec = int((punch_out_dt - punch_in_dt).total_seconds())
    hours_dec = (Decimal(diff_sec) / Decimal(3600)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return float(hours_dec)


def compute_overtime_minutes(
    punch_out_dt: datetime, shift_start: str, shift_end: str, attendance_date: str
) -> int:
    """
    R3: Whole minutes (floor) from shift_end to punch_out, counted only if that is
    at least 30; otherwise 0. Accounts for overnight shifts.
    """
    punch_out_ist = punch_out_dt.astimezone(IST)
    end_h, end_m = map(int, shift_end.split(":"))
    shift_date = dt_date.fromisoformat(attendance_date)
    if shift_end <= shift_start:  # overnight shift ends next calendar day
        shift_date += timedelta(days=1)
    shift_end_dt = datetime.combine(shift_date, time(end_h, end_m), tzinfo=IST)

    diff_sec = (punch_out_ist - shift_end_dt).total_seconds()
    if diff_sec < 1800:  # < 30 minutes
        return 0
    return max(0, int(math.floor(diff_sec / 60.0)))


def compute_half_day(work_hours: Optional[float]) -> bool:
    """
    R5: After punch-out, half_day is true when rounded work_hours < 4.50.
    """
    if work_hours is None:
        return False
    return work_hours < 4.50


def round_half_up(val: Optional[float], decimals: int = 2) -> Optional[float]:
    """R8: Round reported numbers to specified decimals half-up."""
    if val is None:
        return None
    d = Decimal(str(val))
    q = Decimal("10") ** -decimals
    return float(d.quantize(q, rounding=ROUND_HALF_UP))


def get_month_date_range(month_str: str) -> tuple[dt_date, dt_date, int]:
    """Parse YYYY-MM into start_date, end_date, and days_in_month."""
    year, month = map(int, month_str.split("-"))
    _, last_day = calendar.monthrange(year, month)
    start_date = dt_date(year, month, 1)
    end_date = dt_date(year, month, last_day)
    return start_date, end_date, last_day


def count_working_days(start_date: dt_date, end_date: dt_date, joined_on_str: str) -> int:
    """
    R7: Monday - Friday, no holiday calendar.
    For an employee who joined mid-month, working days are counted from joined_on.
    """
    join_date = dt_date.fromisoformat(joined_on_str)
    if join_date > end_date:
        return 0
    effective_start = max(start_date, join_date)
    working_days = 0
    curr = effective_start
    while curr <= end_date:
        if curr.weekday() < 5:  # Monday to Friday
            working_days += 1
        curr += timedelta(days=1)
    return working_days


# --------------------------------------------------------------------------- #
# Pydantic Schemas
# --------------------------------------------------------------------------- #
class StatusEnum(str, Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class PresenceStatusEnum(str, Enum):
    PRESENT = "PRESENT"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class EmployeeCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def validate_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class EmployeeResponse(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str
    shift_end: str
    joined_on: str
    created_at: int


class EmployeePageResponse(BaseModel):
    items: List[EmployeeResponse]
    total: int
    page: int
    page_size: int


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: PresenceStatusEnum = PresenceStatusEnum.PRESENT


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)
    status: Optional[StatusEnum] = None
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class AttendanceRecordResponse(BaseModel):
    emp_code: str
    date: str
    status: StatusEnum
    punch_in: Optional[int]
    punch_out: Optional[int]
    work_hours: Optional[float]
    late_minutes: int
    overtime_minutes: int
    half_day: bool
    history: List[Dict[str, Any]] = []


class AttendancePageResponse(BaseModel):
    items: List[AttendanceRecordResponse]
    total: int
    page: int
    page_size: int


class EmployeeMonthlyResponse(BaseModel):
    emp_code: str
    month: str
    working_days: int
    present_days: float
    leave_days: int
    late_count: int
    total_late_minutes: int
    total_overtime_minutes: int
    attendance_pct: Optional[float]


class DepartmentSummaryItem(BaseModel):
    department: str
    headcount: int
    present_days: float
    avg_work_hours: Optional[float]
    late_count: int
    total_late_minutes: int
    leave_count: int
    on_duty_count: int


class DepartmentSummaryResponse(BaseModel):
    month: str
    items: List[DepartmentSummaryItem]


class LeaderboardItem(BaseModel):
    rank: int
    emp_code: str
    name: str
    department: str
    total_late_minutes: int
    late_count: int


class LeaderboardResponse(BaseModel):
    month: str
    items: List[LeaderboardItem]


class TrendItem(BaseModel):
    date: str
    is_working_day: bool
    headcount: int
    present_count: float
    late_count: int
    attendance_rate: Optional[float]
    moving_avg_7d: Optional[float]


class TrendResponse(BaseModel):
    department: str
    items: List[TrendItem]


class ExplainEndpointEnum(str, Enum):
    attendance_list = "attendance_list"
    employee_monthly = "employee_monthly"
    department_summary = "department_summary"
    late_leaderboard = "late_leaderboard"
    department_trend = "department_trend"


class ExplainResponse(BaseModel):
    endpoint: str
    collection: str
    explain: Dict[str, Any]


def sanitize_for_json(obj: Any) -> Any:
    """Recursively converts MongoDB/BSON types into JSON-serializable primitives."""
    if isinstance(obj, dict):
        return {str(k): sanitize_for_json(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [sanitize_for_json(v) for v in obj]
    elif isinstance(obj, (datetime, dt_date)):
        return obj.isoformat()
    elif isinstance(obj, Decimal):
        return float(obj)
    return obj


def format_attendance_doc(doc: dict) -> dict:
    """Format MongoDB attendance document into contract AttendanceRecord schema."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_epoch_ms(doc.get("punch_in")),
        "punch_out": dt_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [
            {
                "at": dt_to_epoch_ms(h.get("at")),
                "by": h.get("by"),
                "reason": h.get("reason"),
                "changes": {
                    k: {
                        "from": dt_to_epoch_ms(v.get("from")) if k in ("punch_in", "punch_out") else v.get("from"),
                        "to": dt_to_epoch_ms(v.get("to")) if k in ("punch_in", "punch_out") else v.get("to"),
                    }
                    for k, v in h.get("changes", {}).items()
                },
            }
            for h in doc.get("history", [])
        ],
    }


def format_employee_doc(doc: dict) -> dict:
    """Format MongoDB employee document into contract Employee schema."""
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_epoch_ms(doc.get("created_at")),
    }


# --------------------------------------------------------------------------- #
# Endpoints (Phase A Review & Fixes)
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    """Returns 200 only if MongoDB answers a ping, otherwise 503."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Database unavailable",
        )


@app.post("/employees", status_code=201, response_model=EmployeeResponse)
def create_employee(body: EmployeeCreate):
    """
    Creates an employee. Enforces unique emp_code via database unique index
    (concurrent duplicate creates yield exactly one 201 and the rest 409).
    """
    now_utc = truncate_to_seconds(datetime.now(timezone.utc))
    doc = body.model_dump()
    doc["created_at"] = now_utc

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="emp_code already exists",
        )

    return format_employee_doc(doc)


@app.get("/employees", response_model=EmployeePageResponse)
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """
    List employees sorted by emp_code ascending.
    total reflects the filtered set, not the whole collection.
    """
    q: Dict[str, Any] = {}
    if department:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [format_employee_doc(d) for d in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post(
    "/attendance/punch-in",
    status_code=201,
    response_model=AttendanceRecordResponse,
)
def punch_in(body: PunchInRequest):
    """
    Creates the day's record (R1, R2). Status PRESENT, WFH, or ON_DUTY.
    Race-safe: concurrent punch-ins for the same (emp_code, date) yield
    exactly one 201 and the rest 409.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(
            status_code=404,
            detail="Employee not found",
        )

    # Determine punch-in instant
    if body.punched_at is not None:
        punch_in_dt = epoch_ms_to_utc_dt(body.punched_at)
    else:
        punch_in_dt = truncate_to_seconds(datetime.now(timezone.utc))

    # Derive attendance date (R1, handles overnight shifts)
    attendance_date = determine_attendance_date(
        punch_in_dt, emp["shift_start"], emp["shift_end"]
    )

    # Derive late minutes (R2)
    late_mins = compute_late_minutes(
        punch_in_dt, emp["shift_start"], attendance_date
    )

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status.value,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="already punched in for this date",
        )

    return format_attendance_doc(doc)


def build_attendance_list_query(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[StatusEnum] = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[Dict[str, Any], int, int]:
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=422,
            detail="date_from must be <= date_to",
        )

    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        date_q: Dict[str, Any] = {}
        if date_from:
            date_q["$gte"] = date_from
        if date_to:
            date_q["$lte"] = date_to
        q["date"] = date_q
    if status:
        q["status"] = status.value

    skip = (page - 1) * page_size
    return q, skip, page_size


@app.get("/attendance", response_model=AttendancePageResponse)
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[StatusEnum] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """
    List attendance records sorted by date descending, then emp_code ascending.
    Paginates in MongoDB; fast at ~100k records.
    """
    q, skip, limit_count = build_attendance_list_query(
        emp_code, date_from, date_to, status, page, page_size
    )

    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(limit_count)
    )

    items = [format_attendance_doc(d) for d in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Endpoints (Phase B, Group B1: Punch-Out & Regularization)
# --------------------------------------------------------------------------- #
@app.post(
    "/attendance/punch-out",
    status_code=200,
    response_model=AttendanceRecordResponse,
)
def punch_out(body: PunchOutRequest):
    """
    Closes the employee's most recent record whose punch_in is at or before punched_at
    (works across midnight for overnight shifts). Computes work_hours (R4),
    overtime_minutes (R3) and half_day (R5).
    Errors: 404 unknown employee, or no punch-in found; 409 already punched out;
    422 if punched_at <= punch_in or more than 24 hours after it.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_out_dt = epoch_ms_to_utc_dt(body.punched_at)
    else:
        punch_out_dt = truncate_to_seconds(datetime.now(timezone.utc))

    # Find employee's most recent record whose punch_in is at or before punched_at
    candidate = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_dt}},
        sort=[("punch_in", -1)],
    )
    if not candidate:
        raise HTTPException(status_code=404, detail="No matching punch-in found")

    punch_in_dt = candidate["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    # 422 validations
    if punch_out_dt <= punch_in_dt:
        raise HTTPException(
            status_code=422,
            detail="punched_at must be strictly greater than punch_in",
        )

    diff_sec = (punch_out_dt - punch_in_dt).total_seconds()
    if diff_sec > 86400:
        raise HTTPException(
            status_code=422,
            detail="punched_at cannot be more than 24 hours after punch_in",
        )

    # 409 check: record is already punched out
    if candidate.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Record is already punched out")

    # Compute derived fields (R3, R4, R5)
    work_hours = compute_work_hours(punch_in_dt, punch_out_dt)
    overtime_minutes = compute_overtime_minutes(
        punch_out_dt, emp["shift_start"], emp["shift_end"], candidate["date"]
    )
    half_day = compute_half_day(work_hours)

    # Atomic close of record (find_one_and_update guarantees mutual exclusion)
    updated_doc = db.attendance_logs.find_one_and_update(
        {"_id": candidate["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated_doc:
        raise HTTPException(status_code=409, detail="Record was already punched out concurrently")

    return format_attendance_doc(updated_doc)


@app.patch(
    "/attendance/{emp_code}/{date}",
    status_code=200,
    response_model=AttendanceRecordResponse,
)
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeRequest,
):
    """
    Regularize (manually correct) one day's record.
    Recompute derived fields and append exactly one entry to history recording only changed fields.
    """
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise HTTPException(status_code=422, detail="Invalid date format, expected YYYY-MM-DD")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    existing = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not existing:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    curr_status = existing["status"]
    curr_punch_in = existing.get("punch_in")
    if curr_punch_in is not None and curr_punch_in.tzinfo is None:
        curr_punch_in = curr_punch_in.replace(tzinfo=timezone.utc)

    curr_punch_out = existing.get("punch_out")
    if curr_punch_out is not None and curr_punch_out.tzinfo is None:
        curr_punch_out = curr_punch_out.replace(tzinfo=timezone.utc)

    curr_work_hours = existing.get("work_hours")
    curr_late_minutes = existing.get("late_minutes", 0)
    curr_overtime_minutes = existing.get("overtime_minutes", 0)
    curr_half_day = existing.get("half_day", False)
    curr_history = existing.get("history", [])

    # Determine target status
    new_status = body.status.value if body.status is not None else curr_status

    # Rule: setting status ABSENT or LEAVE clears both punch times, so supplying punch_in or punch_out with it is 422
    if body.status in (StatusEnum.ABSENT, StatusEnum.LEAVE):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail="Cannot supply punch_in or punch_out when setting status to ABSENT or LEAVE",
            )
        new_punch_in = None
        new_punch_out = None
    elif new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail="Cannot supply punch times for ABSENT or LEAVE status",
            )
        new_punch_in = None
        new_punch_out = None
    else:
        # Presence status (PRESENT, WFH, ON_DUTY)
        if body.punch_in is not None:
            new_punch_in = epoch_ms_to_utc_dt(body.punch_in)
        else:
            new_punch_in = curr_punch_in

        if body.punch_out is not None:
            new_punch_out = epoch_ms_to_utc_dt(body.punch_out)
        else:
            new_punch_out = curr_punch_out

    # Rule: a presence status requires a punch_in (supplied or already stored)
    if new_status in ("PRESENT", "WFH", "ON_DUTY") and new_punch_in is None:
        raise HTTPException(
            status_code=422,
            detail="A presence status requires a punch_in",
        )

    # Rule: punch_in must stay on the record's attendance date (R1)
    if new_punch_in is not None:
        derived_date = determine_attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
        if derived_date != date:
            raise HTTPException(
                status_code=422,
                detail=f"punch_in belongs to attendance date {derived_date}, not {date}",
            )

    # Rule: punch_out must be after punch_in and within 24 hours
    if new_punch_out is not None:
        if new_punch_in is None:
            raise HTTPException(
                status_code=422,
                detail="punch_out cannot exist without punch_in",
            )
        if new_punch_out <= new_punch_in:
            raise HTTPException(
                status_code=422,
                detail="punch_out must be strictly after punch_in",
            )
        if (new_punch_out - new_punch_in).total_seconds() > 86400:
            raise HTTPException(
                status_code=422,
                detail="punch_out cannot be more than 24 hours after punch_in",
            )

    # Recompute derived fields (R2 - R5)
    if new_status in ("ABSENT", "LEAVE"):
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        new_late_minutes = compute_late_minutes(new_punch_in, emp["shift_start"], date)
        if new_punch_out is not None:
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime_minutes = compute_overtime_minutes(
                new_punch_out, emp["shift_start"], emp["shift_end"], date
            )
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    # Compute actual field changes
    changes: Dict[str, Dict[str, Any]] = {}
    if new_status != curr_status:
        changes["status"] = {"from": curr_status, "to": new_status}

    if new_punch_in != curr_punch_in:
        changes["punch_in"] = {"from": curr_punch_in, "to": new_punch_in}

    if new_punch_out != curr_punch_out:
        changes["punch_out"] = {"from": curr_punch_out, "to": new_punch_out}

    if new_work_hours != curr_work_hours:
        changes["work_hours"] = {"from": curr_work_hours, "to": new_work_hours}

    if new_late_minutes != curr_late_minutes:
        changes["late_minutes"] = {"from": curr_late_minutes, "to": new_late_minutes}

    if new_overtime_minutes != curr_overtime_minutes:
        changes["overtime_minutes"] = {"from": curr_overtime_minutes, "to": new_overtime_minutes}

    if new_half_day != curr_half_day:
        changes["half_day"] = {"from": curr_half_day, "to": new_half_day}

    # Rule: a request that changes nothing is 422
    if not changes:
        raise HTTPException(
            status_code=422,
            detail="Request does not change any fields",
        )

    now_utc = truncate_to_seconds(datetime.now(timezone.utc))
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Atomic update with optimistic lock on history length to prevent history loss under concurrency
    updated_doc = db.attendance_logs.find_one_and_update(
        {
            "_id": existing["_id"],
            "history": {"$size": len(curr_history)},
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {
                "history": history_entry,
            },
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated_doc:
        raise HTTPException(
            status_code=409,
            detail="Concurrent modification conflict; please retry",
        )

    return format_attendance_doc(updated_doc)


# --------------------------------------------------------------------------- #
# Endpoints (Phase B, Group B2: Monthly & Department Analytics)
# --------------------------------------------------------------------------- #
def build_employee_monthly_pipeline(emp_code: str, month: str, last_day: int) -> List[Dict[str, Any]]:
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": f"{month}-01", "$lte": f"{month}-{last_day:02d}"},
            }
        },
        {
            "$project": {
                "status": 1,
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "half_day": {"$ifNull": ["$half_day", False]},
                "is_weekday": {
                    "$lte": [
                        {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                        5,
                    ]
                },
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    "$is_weekday",
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]


@app.get(
    "/analytics/employees/{emp_code}/monthly",
    status_code=200,
    response_model=EmployeeMonthlyResponse,
)
def employee_monthly_analytics(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """
    Monthly attendance summary for one employee backed by a MongoDB aggregation.
    working_days per R7. present_days per R5-R7 (half day = 0.5, Mon-Fri only).
    leave_days = LEAVE records. late_count = late_minutes > 0.
    attendance_pct = present_days / working_days * 100 (R8 rounding), or null when working_days is 0.
    404 for an unknown employee.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    start_date, end_date, last_day = get_month_date_range(month)
    working_days = count_working_days(start_date, end_date, emp["joined_on"])

    pipeline = build_employee_monthly_pipeline(emp_code, month, last_day)
    agg_res = list(db.attendance_logs.aggregate(pipeline))
    if agg_res:
        row = agg_res[0]
        present_days = round_half_up(float(row.get("present_days", 0.0)), 2)
        leave_days = int(row.get("leave_days", 0))
        late_count = int(row.get("late_count", 0))
        total_late_minutes = int(row.get("total_late_minutes", 0))
        total_overtime_minutes = int(row.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days > 0:
        attendance_pct = round_half_up((present_days / working_days) * 100.0, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


def build_department_summary_pipeline(
    start_date_str: str, end_date_str: str, department: Optional[str] = None
) -> List[Dict[str, Any]]:
    match_emp: Dict[str, Any] = {"joined_on": {"$lte": end_date_str}}
    if department:
        match_emp["department"] = department

    return [
        {"$match": match_emp},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "emp_codes": {"$push": "$emp_code"},
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"codes": "$emp_codes"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$in": ["$emp_code", "$$codes"]},
                                    {"$gte": ["$date", start_date_str]},
                                    {"$lte": ["$date", end_date_str]},
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "status": 1,
                            "work_hours": 1,
                            "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                            "half_day": {"$ifNull": ["$half_day", False]},
                            "is_weekday": {
                                "$lte": [
                                    {
                                        "$isoDayOfWeek": {
                                            "$dateFromString": {
                                                "dateString": "$date"
                                            }
                                        }
                                    },
                                    5,
                                ]
                            },
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_days": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                "$is_weekday",
                                                {
                                                    "$in": [
                                                        "$status",
                                                        ["PRESENT", "WFH", "ON_DUTY"],
                                                    ]
                                                },
                                            ]
                                        },
                                        {"$cond": ["$half_day", 0.5, 1.0]},
                                        0.0,
                                    ]
                                }
                            },
                            "total_work_hours": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {
                                                    "$in": [
                                                        "$status",
                                                        ["PRESENT", "WFH", "ON_DUTY"],
                                                    ]
                                                },
                                                {"$ne": ["$work_hours", None]},
                                            ]
                                        },
                                        "$work_hours",
                                        0.0,
                                    ]
                                }
                            },
                            "work_hours_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {
                                                    "$in": [
                                                        "$status",
                                                        ["PRESENT", "WFH", "ON_DUTY"],
                                                    ]
                                                },
                                                {"$ne": ["$work_hours", None]},
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
                                }
                            },
                            "total_late_minutes": {"$sum": "$late_minutes"},
                            "leave_count": {
                                "$sum": {
                                    "$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]
                                }
                            },
                            "on_duty_count": {
                                "$sum": {
                                    "$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]
                                }
                            },
                        }
                    },
                ],
                "as": "log_stats",
            }
        },
        {"$unwind": {"path": "$log_stats", "preserveNullAndEmptyArrays": True}},
        {
            "$project": {
                "department": "$_id",
                "headcount": "$headcount",
                "present_days": {"$ifNull": ["$log_stats.present_days", 0.0]},
                "total_work_hours": "$log_stats.total_work_hours",
                "work_hours_count": "$log_stats.work_hours_count",
                "late_count": {"$ifNull": ["$log_stats.late_count", 0]},
                "total_late_minutes": {"$ifNull": ["$log_stats.total_late_minutes", 0]},
                "leave_count": {"$ifNull": ["$log_stats.leave_count", 0]},
                "on_duty_count": {"$ifNull": ["$log_stats.on_duty_count", 0]},
            }
        },
        {"$sort": {"department": 1}},
    ]


@app.get(
    "/analytics/departments/summary",
    status_code=200,
    response_model=DepartmentSummaryResponse,
)
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """
    Department summary for a month via MongoDB aggregation across both collections.
    Headcount per R9 (people with zero logs still count, people who join later do not).
    avg_work_hours is record-level mean over present-status records with work_hours; null if none.
    A department whose headcount is 0 for the month is omitted.
    """
    start_date, end_date, last_day = get_month_date_range(month)
    end_date_str = end_date.isoformat()
    start_date_str = start_date.isoformat()

    pipeline = build_department_summary_pipeline(start_date_str, end_date_str, department)
    cursor = db.employees.aggregate(pipeline)
    items = []
    for doc in cursor:
        wh_count = doc.get("work_hours_count") or 0
        total_wh = doc.get("total_work_hours") or 0.0
        avg_wh = round_half_up(total_wh / wh_count, 2) if wh_count > 0 else None

        items.append(
            {
                "department": doc["department"],
                "headcount": int(doc["headcount"]),
                "present_days": round_half_up(float(doc["present_days"]), 2),
                "avg_work_hours": avg_wh,
                "late_count": int(doc["late_count"]),
                "total_late_minutes": int(doc["total_late_minutes"]),
                "leave_count": int(doc["leave_count"]),
                "on_duty_count": int(doc["on_duty_count"]),
            }
        )

    return {"month": month, "items": items}


# --------------------------------------------------------------------------- #
# Endpoints (Phase B, Group B3: Late Leaderboard & Department Trend)
# --------------------------------------------------------------------------- #
def build_late_leaderboard_pipeline(
    start_str: str, end_str: str, limit: int, department: Optional[str] = None
) -> List[Dict[str, Any]]:
    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": start_str, "$lte": end_str},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {
                        "$rank": {}
                    }
                },
            }
        },
        {
            "$match": {
                "rank": {"$lte": limit}
            }
        },
        {
            "$sort": {
                "total_late_minutes": -1,
                "_id": 1,
            }
        },
        {
            "$project": {
                "_id": 0,
                "rank": "$rank",
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": "$total_late_minutes",
                "late_count": "$late_count",
            }
        },
        {
            "$sort": {
                "total_late_minutes": -1,
                "emp_code": 1,
            }
        },
    ])
    return pipeline


@app.get(
    "/analytics/leaderboard/late",
    status_code=200,
    response_model=LeaderboardResponse,
)
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    """
    Ranks employees by total late_minutes in the month (only employees with total > 0).
    Standard competition ranking: ties share rank, next rank is skipped (1, 2, 2, 4).
    limit is applied after ranking (returns every row whose rank <= limit).
    Order: total_late_minutes desc, emp_code asc.
    """
    start_date, end_date, last_day = get_month_date_range(month)
    start_str = start_date.isoformat()
    end_str = end_date.isoformat()

    pipeline = build_late_leaderboard_pipeline(start_str, end_str, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


def build_department_trend_pipeline(
    department: str, from_date: dt_date, to_date: dt_date
) -> List[Dict[str, Any]]:
    start_dt = datetime.combine(from_date, time.min, tzinfo=timezone.utc)
    end_dt = datetime.combine(to_date, time.min, tzinfo=timezone.utc)

    dates_boundary = [start_dt]
    if start_dt != end_dt:
        dates_boundary.append(end_dt)

    return [
        {"$match": {"department": department}},
        {
            "$group": {
                "_id": "$department",
                "dept_employees": {
                    "$push": {"emp_code": "$emp_code", "joined_on": "$joined_on"}
                },
                "emp_codes": {"$push": "$emp_code"},
            }
        },
        {"$project": {"dept_employees": 1, "emp_codes": 1, "dates_boundary": dates_boundary}},
        {"$unwind": "$dates_boundary"},
        {
            "$densify": {
                "field": "dates_boundary",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": "full",
                },
            }
        },
        {
            "$fill": {
                "sortBy": {"dates_boundary": 1},
                "output": {
                    "dept_employees": {"method": "locf"},
                    "emp_codes": {"method": "locf"},
                },
            }
        },
        {
            "$group": {
                "_id": "$dates_boundary",
                "dept_employees": {"$first": "$dept_employees"},
                "emp_codes": {"$first": "$emp_codes"},
            }
        },
        {"$sort": {"_id": 1}},
        {
            "$project": {
                "date_dt": "$_id",
                "date_str": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$_id",
                        "timezone": "+00:00",
                    }
                },
                "is_working_day": {"$lte": [{"$isoDayOfWeek": "$_id"}, 5]},
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$dept_employees",
                            "as": "e",
                            "cond": {
                                "$lte": [
                                    "$$e.joined_on",
                                    {
                                        "$dateToString": {
                                            "format": "%Y-%m-%d",
                                            "date": "$_id",
                                            "timezone": "+00:00",
                                        }
                                    },
                                ]
                            },
                        }
                    }
                },
                "emp_codes": 1,
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"codes": "$emp_codes", "curr_date": "$date_str"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$in": ["$emp_code", "$$codes"]},
                                    {"$eq": ["$date", "$$curr_date"]},
                                ]
                            }
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$in": [
                                                "$status",
                                                ["PRESENT", "WFH", "ON_DUTY"],
                                            ]
                                        },
                                        {"$cond": ["$half_day", 0.5, 1.0]},
                                        0.0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$gt": ["$late_minutes", 0]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "day_stats",
            }
        },
        {"$unwind": {"path": "$day_stats", "preserveNullAndEmptyArrays": True}},
        {
            "$project": {
                "date": "$date_str",
                "is_working_day": "$is_working_day",
                "headcount": "$headcount",
                "present_count": {"$ifNull": ["$day_stats.present_count", 0.0]},
                "late_count": {"$ifNull": ["$day_stats.late_count", 0]},
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$divide": [
                                {"$ifNull": ["$day_stats.present_count", 0.0]},
                                "$headcount",
                            ]
                        },
                        None,
                    ]
                },
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"]
                        },
                    }
                },
            }
        },
        {"$sort": {"date": 1}},
    ]


@app.get(
    "/analytics/departments/{department}/trend",
    status_code=200,
    response_model=TrendResponse,
)
def department_trend(
    department: str,
    from_date_str: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date_str: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    """
    Daily attendance trend for a department with gap filling ($densify) and 7-day moving average.
    Returns exactly one row per calendar day from 'from' to 'to' inclusive.
    attendance_rate = present_count / headcount (4 decimals half-up) on working days with headcount > 0; else null.
    moving_avg_7d = mean of non-null attendance_rate among current row and up to 6 preceding rows inside the range.
    Errors: 404 unknown department; 422 if to < from or range exceeds 92 days.
    """
    try:
        from_date = dt_date.fromisoformat(from_date_str)
        to_date = dt_date.fromisoformat(to_date_str)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date format or range")

    if to_date < from_date:
        raise HTTPException(status_code=422, detail="'to' date must be >= 'from' date")

    range_days = (to_date - from_date).days + 1
    if range_days > 92:
        raise HTTPException(status_code=422, detail="Date range exceeds 92 days")

    # 404 check: department must exist in employees collection
    dept_emp_count = db.employees.count_documents({"department": department})
    if dept_emp_count == 0:
        raise HTTPException(status_code=404, detail="Department not found")

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    cursor = db.employees.aggregate(pipeline)
    items = []
    for doc in cursor:
        items.append(
            {
                "date": doc["date"],
                "is_working_day": bool(doc["is_working_day"]),
                "headcount": int(doc["headcount"]),
                "present_count": round_half_up(float(doc["present_count"]), 2),
                "late_count": int(doc["late_count"]),
                "attendance_rate": round_half_up(doc.get("attendance_rate"), 4),
                "moving_avg_7d": round_half_up(doc.get("moving_avg_7d"), 4),
            }
        )

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Endpoints (Phase B, Group B4: Admin Explain)
# --------------------------------------------------------------------------- #
@app.get(
    "/admin/explain/{endpoint}",
    status_code=200,
    response_model=ExplainResponse,
)
def explain_endpoint(
    endpoint: ExplainEndpointEnum,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[StatusEnum] = None,
    from_date_str: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date_str: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """
    MongoDB explain for an endpoint's main query or aggregation pipeline.
    Runs explain with executionStats verbosity for the named endpoint,
    built exactly as that endpoint builds it (same filters, sort, pagination).
    Accepts the same query parameters as the endpoint it explains.
    Missing required parameters -> 422.
    """
    if endpoint == ExplainEndpointEnum.attendance_list:
        collection_name = "attendance_logs"
        q, skip, limit_count = build_attendance_list_query(
            emp_code=emp_code,
            date_from=date_from,
            date_to=date_to,
            status=status,
            page=page,
            page_size=page_size,
        )
        cmd = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": limit_count,
        }

    elif endpoint == ExplainEndpointEnum.employee_monthly:
        collection_name = "attendance_logs"
        if not emp_code:
            raise HTTPException(
                status_code=422,
                detail="emp_code is required for employee_monthly explain",
            )
        if not month:
            raise HTTPException(
                status_code=422,
                detail="month is required for employee_monthly explain",
            )
        try:
            start_date, end_date, last_day = get_month_date_range(month)
        except Exception:
            raise HTTPException(
                status_code=422,
                detail="Invalid month format",
            )
        pipeline = build_employee_monthly_pipeline(emp_code, month, last_day)
        cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }

    elif endpoint == ExplainEndpointEnum.department_summary:
        collection_name = "employees"
        if not month:
            raise HTTPException(
                status_code=422,
                detail="month is required for department_summary explain",
            )
        try:
            start_date, end_date, last_day = get_month_date_range(month)
        except Exception:
            raise HTTPException(
                status_code=422,
                detail="Invalid month format",
            )
        pipeline = build_department_summary_pipeline(
            start_date.isoformat(), end_date.isoformat(), department
        )
        cmd = {
            "aggregate": "employees",
            "pipeline": pipeline,
            "cursor": {},
        }

    elif endpoint == ExplainEndpointEnum.late_leaderboard:
        collection_name = "attendance_logs"
        if not month:
            raise HTTPException(
                status_code=422,
                detail="month is required for late_leaderboard explain",
            )
        try:
            start_date, end_date, last_day = get_month_date_range(month)
        except Exception:
            raise HTTPException(
                status_code=422,
                detail="Invalid month format",
            )
        pipeline = build_late_leaderboard_pipeline(
            start_date.isoformat(), end_date.isoformat(), limit, department
        )
        cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }

    elif endpoint == ExplainEndpointEnum.department_trend:
        collection_name = "employees"
        if not department:
            raise HTTPException(
                status_code=422,
                detail="department is required for department_trend explain",
            )
        if not from_date_str or not to_date_str:
            raise HTTPException(
                status_code=422,
                detail="'from' and 'to' dates are required for department_trend explain",
            )
        try:
            from_date = dt_date.fromisoformat(from_date_str)
            to_date = dt_date.fromisoformat(to_date_str)
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail="Invalid date format or range",
            )
        if to_date < from_date:
            raise HTTPException(
                status_code=422,
                detail="'to' date must be >= 'from' date",
            )
        if (to_date - from_date).days + 1 > 92:
            raise HTTPException(
                status_code=422,
                detail="Date range exceeds 92 days",
            )
        pipeline = build_department_trend_pipeline(department, from_date, to_date)
        cmd = {
            "aggregate": "employees",
            "pipeline": pipeline,
            "cursor": {},
        }

    # Execute explain with executionStats verbosity against MongoDB
    try:
        raw_explain = db.command({"explain": cmd, "verbosity": "executionStats"})
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Explain execution failed: {str(e)}",
        )

    return {
        "endpoint": endpoint.value,
        "collection": collection_name,
        "explain": sanitize_for_json(raw_explain),
    }
