"""
Employee Attendance & Analytics API — HROne assignment
Run from the repository root:
    uvicorn app.main:app --port 8000

Configuration:
    MONGO_URI (defaults to local MongoDB)
    MONGO_DB  (defaults to attendance_db)
"""
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal, Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import (
    BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
)
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from bson import json_util

load_dotenv()

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = PRESENCE_STATUSES | {"ABSENT", "LEAVE"}
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# ---------------------------------------------------------------------------
# Startup indexes
# ---------------------------------------------------------------------------
@app.on_event("startup")
def create_indexes():
    db.employees.create_index(
        [("emp_code", ASCENDING)], unique=True, name="uq_employee_code"
    )
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)],
        name="idx_employee_department_joined",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True, name="uq_attendance_employee_date",
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="idx_attendance_date_employee",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)],
        name="idx_attendance_employee_date_status",
    )
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("status", ASCENDING)],
        name="idx_attendance_date_status",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def utc_now_naive() -> datetime:
    """MongoDB BSON datetimes are stored as UTC; PyMongo returns naive UTC by default."""
    return datetime.now(UTC).replace(microsecond=0).replace(tzinfo=None)


def datetime_from_epoch_ms(value: int) -> datetime:
    """Validate elsewhere, then truncate an epoch-ms instant to whole seconds."""
    return datetime.fromtimestamp(value / 1000, tz=UTC).replace(
        microsecond=0, tzinfo=None
    )


def epoch_ms_from_datetime(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    else:
        value = value.astimezone(UTC)
    value = value.replace(microsecond=0)
    return int(value.timestamp() * 1000)


def to_ist(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(IST)


def attendance_date_for_punch(punch_in: datetime, employee: dict) -> str:
    local = to_ist(punch_in)
    shift_start = employee["shift_start"]
    shift_end = employee["shift_end"]
    result = local.date()
    if shift_end <= shift_start and local.strftime("%H:%M") < shift_end:
        result -= timedelta(days=1)
    return result.isoformat()


def shift_start_datetime(attendance_date: str, shift_start: str) -> datetime:
    return datetime.fromisoformat(f"{attendance_date}T{shift_start}").replace(
        tzinfo=IST
    )


def shift_end_datetime(
    attendance_date: str, shift_start: str, shift_end: str
) -> datetime:
    end_day = date.fromisoformat(attendance_date)
    if shift_end <= shift_start:
        end_day += timedelta(days=1)
    return datetime.fromisoformat(f"{end_day.isoformat()}T{shift_end}").replace(
        tzinfo=IST
    )


def compute_late_minutes(
    punch_in: Optional[datetime], shift_start: str, attendance_date: str
) -> int:
    if punch_in is None:
        return 0
    elapsed = (to_ist(punch_in) - shift_start_datetime(
        attendance_date, shift_start
    )).total_seconds()
    if elapsed <= 600:
        return 0
    return max(0, int(elapsed // 60))


def compute_work_hours(punch_in: Optional[datetime], punch_out: Optional[datetime]):
    if punch_in is None or punch_out is None:
        return None
    seconds = int((punch_out - punch_in).total_seconds())
    hours = Decimal(seconds) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(
    punch_out: Optional[datetime],
    shift_start: str,
    shift_end: str,
    attendance_date: str,
) -> int:
    if punch_out is None:
        return 0
    end = shift_end_datetime(attendance_date, shift_start, shift_end)
    elapsed = (to_ist(punch_out) - end).total_seconds()
    minutes = int(elapsed // 60)
    return minutes if minutes >= 30 else 0


def derived_values(
    punch_in: Optional[datetime],
    punch_out: Optional[datetime],
    employee: dict,
    attendance_date: str,
) -> dict:
    work_hours = compute_work_hours(punch_in, punch_out)
    return {
        "late_minutes": compute_late_minutes(
            punch_in, employee["shift_start"], attendance_date
        ),
        "work_hours": work_hours,
        "overtime_minutes": compute_overtime(
            punch_out, employee["shift_start"], employee["shift_end"],
            attendance_date,
        ),
        "half_day": work_hours is not None and work_hours < 4.50,
    }


def round_half_up(value: float, places: int = 2) -> float:
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def api_document(doc: Optional[dict]) -> Optional[dict]:
    """Convert internal MongoDB documents to contract response representations."""
    if doc is None:
        return None
    result = dict(doc)
    result.pop("_id", None)
    result.pop("id", None)
    if "created_at" in result:
        result["created_at"] = epoch_ms_from_datetime(result["created_at"])
    for field in ("punch_in", "punch_out"):
        if field in result:
            result[field] = epoch_ms_from_datetime(result[field])
    if "history" in result:
        history = []
        for entry in result.get("history") or []:
            e = dict(entry)
            e["at"] = epoch_ms_from_datetime(e.get("at"))
            changes = {}
            for key, change in (e.get("changes") or {}).items():
                c = dict(change)
                if key in {"punch_in", "punch_out"}:
                    c["from"] = epoch_ms_from_datetime(c.get("from"))
                    c["to"] = epoch_ms_from_datetime(c.get("to"))
                changes[key] = c
            e["changes"] = changes
            history.append(e)
        result["history"] = history
    return result


def validate_epoch_ms(value: Any, field_name: str = "punched_at") -> Optional[int]:
    if value is None:
        return None
    if type(value) is not int or not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
        raise HTTPException(
            status_code=422,
            detail=f"{field_name} must be an integer epoch-millisecond timestamp",
        )
    return value


def validate_iso_date(value: str, field_name: str = "date") -> str:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail=f"{field_name} must be YYYY-MM-DD")
    if parsed.isoformat() != value:
        raise HTTPException(status_code=422, detail=f"{field_name} must be YYYY-MM-DD")
    return value


def validate_month(month: str) -> tuple[date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month or ""):
        raise HTTPException(status_code=422, detail="month must be YYYY-MM")
    start = date.fromisoformat(month + "-01")
    if start.month == 12:
        next_month = date(start.year + 1, 1, 1)
    else:
        next_month = date(start.year, start.month + 1, 1)
    return start, next_month


def month_working_days(joined_on: str, month_start: date, next_month: date) -> int:
    start = max(date.fromisoformat(joined_on), month_start)
    end = next_month - timedelta(days=1)
    if start > end:
        return 0
    count = 0
    current = start
    while current <= end:
        if current.weekday() < 5:
            count += 1
        current += timedelta(days=1)
    return count


def validate_page(page: int, page_size: int):
    if page < 1:
        raise HTTPException(status_code=422, detail="page must be at least 1")
    if not 1 <= page_size <= 100:
        raise HTTPException(status_code=422, detail="page_size must be between 1 and 100")


def status_presence_expression():
    return {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}


def date_match_for_month(month: str) -> dict:
    start, next_month = validate_month(month)
    return {"date": {"$gte": start.isoformat(), "$lt": next_month.isoformat()}}


def json_safe(value):
    return json_util.loads(json_util.dumps(value))


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(
        max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
    )
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: date

    @field_validator("name", "department")
    @classmethod
    def non_blank(cls, value):
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @model_validator(mode="after")
    def different_shift_times(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def valid_timestamp(cls, value):
        if value is not None and not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
            raise ValueError("punched_at must be epoch milliseconds")
        return value


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[StrictInt] = None

    @field_validator("punched_at")
    @classmethod
    def valid_timestamp(cls, value):
        if value is not None and not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
            raise ValueError("punched_at must be epoch milliseconds")
        return value


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_timestamp(cls, value):
        if value is not None and not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
            raise ValueError("timestamps must be epoch milliseconds")
        return value


# ---------------------------------------------------------------------------
# System and employee endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump(mode="json")
    doc["created_at"] = utc_now_naive()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return api_document(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    query = {}
    if department is not None:
        query["department"] = department
    total = db.employees.count_documents(query)
    docs = list(
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [api_document(d) for d in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ---------------------------------------------------------------------------
# Attendance endpoints
# ---------------------------------------------------------------------------
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="unknown employee")

    ts = datetime_from_epoch_ms(body.punched_at) if body.punched_at is not None else utc_now_naive()
    attendance_date = attendance_date_for_punch(ts, employee)
    derived = derived_values(ts, None, employee, attendance_date)
    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": derived["late_minutes"],
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409, detail="already punched in for this attendance date"
        )
    return api_document(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="unknown employee")

    ts = datetime_from_epoch_ms(body.punched_at) if body.punched_at is not None else utc_now_naive()
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        raise HTTPException(status_code=404, detail="no punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="record already punched out")

    elapsed = (ts - record["punch_in"]).total_seconds()
    if elapsed <= 0 or elapsed > 24 * 3600:
        raise HTTPException(
            status_code=422,
            detail="punched_at must be after punch_in and within 24 hours",
        )

    values = derived_values(record["punch_in"], ts, employee, record["date"])
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "punch_out": None,
            "punch_in": {"$lte": ts},
        },
        {
            "$set": {
                "punch_out": ts,
                "work_hours": values["work_hours"],
                "overtime_minutes": values["overtime_minutes"],
                "half_day": values["half_day"],
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        latest = db.attendance_logs.find_one({"_id": record["_id"]})
        if latest and latest.get("punch_out") is not None:
            raise HTTPException(status_code=409, detail="record already punched out")
        raise HTTPException(status_code=409, detail="record changed concurrently")
    return api_document(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if date_from:
        validate_iso_date(date_from, "date_from")
    if date_to:
        validate_iso_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must not exceed date_to")

    query = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status is not None:
        query["status"] = status

    total = db.attendance_logs.count_documents(query)
    docs = list(
        db.attendance_logs.find(query, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    for doc in docs:
        doc["punch_in"] = epoch_ms_from_datetime(doc.get("punch_in"))
        doc["punch_out"] = epoch_ms_from_datetime(doc.get("punch_out"))
        doc["history"] = api_document({"history": doc.get("history", [])})["history"]
        doc.setdefault("half_day", False)
        doc.setdefault("late_minutes", 0)
        doc.setdefault("overtime_minutes", 0)
    return {"items": docs, "total": total, "page": page, "page_size": page_size}


# ---------------------------------------------------------------------------
# Manual regularization
# ---------------------------------------------------------------------------

# Manual regularization
# ---------------------------------------------------------------------------
@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    validate_iso_date(date)
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="unknown employee")

    record = db.attendance_logs.find_one(
        {"emp_code": emp_code, "date": date}
    )
    if record is None:
        raise HTTPException(status_code=404, detail="attendance record not found")


    requested = body.model_dump(exclude_unset=True)
    reason = requested["reason"]
    regularized_by = requested["regularized_by"]
    old_history = record.get("history", [])
    old_values = {
        "status": record.get("status"),
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False),
    }

    new_status = requested.get("status", record.get("status"))
    new_in = (
        datetime_from_epoch_ms(requested["punch_in"])
        if "punch_in" in requested and requested["punch_in"] is not None
        else (None if "punch_in" in requested else record.get("punch_in"))
    )
    new_out = (
        datetime_from_epoch_ms(requested["punch_out"])
        if "punch_out" in requested and requested["punch_out"] is not None
        else (None if "punch_out" in requested else record.get("punch_out"))
    )

    if new_status in {"ABSENT", "LEAVE"}:
        if "punch_in" in requested or "punch_out" in requested:
            raise HTTPException(
                status_code=422,
                detail="ABSENT and LEAVE cannot be combined with punch times",
            )
        new_in, new_out = None, None
    elif new_in is None:
        raise HTTPException(status_code=422, detail="presence status requires punch_in")

    if new_in is not None:
        expected_date = attendance_date_for_punch(new_in, employee)
        if expected_date != date:
            raise HTTPException(
                status_code=422, detail="punch_in must belong to the record attendance date"
            )
    if new_out is not None:
        if new_in is None:
            raise HTTPException(status_code=422, detail="punch_out requires punch_in")
        elapsed = (new_out - new_in).total_seconds()
        if elapsed <= 0 or elapsed > 24 * 3600:
            raise HTTPException(
                status_code=422, detail="punch_out must be after punch_in and within 24 hours"
            )

    new_derived = derived_values(new_in, new_out, employee, date)
    new_values = {
        "status": new_status,
        "punch_in": new_in,
        "punch_out": new_out,
        **new_derived,
    }
    changes = {}
    for key, new_value in new_values.items():
        old_value = old_values.get(key)
        if old_value != new_value:
            if key in {"punch_in", "punch_out"}:
                old_value = epoch_ms_from_datetime(old_value)
                new_value_for_history = epoch_ms_from_datetime(new_value)
            else:
                new_value_for_history = new_value
            changes[key] = {"from": old_value, "to": new_value_for_history}

    if not changes:
        raise HTTPException(status_code=422, detail="request does not change the record")

    history_entry = {
        "at": utc_now_naive(),
        "by": regularized_by,
        "reason": reason,
        "changes": {
            key: {
                "from": (
                    datetime_from_epoch_ms(value["from"])
                    if key in {"punch_in", "punch_out"} and value["from"] is not None
                    else value["from"]
                ),
                "to": (
                    datetime_from_epoch_ms(value["to"])
                    if key in {"punch_in", "punch_out"} and value["to"] is not None
                    else value["to"]
                ),
            }
            for key, value in changes.items()
        },
    }

    filter_query = {"_id": record["_id"]}
    if "history" in record:
        filter_query["history"] = old_history
    else:
        filter_query["history"] = {"$exists": False}

    updated = db.attendance_logs.find_one_and_update(
        filter_query,
        {
            "$set": {
                "status": new_status,
                "punch_in": new_in,
                "punch_out": new_out,
                **new_derived,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="record changed concurrently")
    return api_document(updated)


# ---------------------------------------------------------------------------
# Analytics: all log analytics are executed by MongoDB aggregation pipelines.
# ---------------------------------------------------------------------------
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    month_start, next_month = validate_month(month)
    employee = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if employee is None:
        raise HTTPException(status_code=404, detail="unknown employee")

    pipeline = [
        {"$match": {"emp_code": emp_code, **date_match_for_month(month)}},
        {"$addFields": {
            "weekday": {
                "$dayOfWeek": {
                    "$dateFromString": {"dateString": "$date"}
                }
            },
            "presence": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
            "effective_half_day": {"$ifNull": ["$half_day", False]},
            "effective_late": {"$ifNull": ["$late_minutes", 0]},
            "effective_overtime": {"$ifNull": ["$overtime_minutes", 0]},
        }},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {
                "$cond": [
                    {"$and": [
                        "$presence",
                        {"$gte": ["$weekday", 2]},
                        {"$lte": ["$weekday", 6]},
                    ]},
                    {"$cond": ["$effective_half_day", 0.5, 1.0]},
                    0,
                ]
            }},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$effective_late", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$effective_late"},
            "total_overtime_minutes": {"$sum": "$effective_overtime"},
        }},
    ]
    result = list(db.attendance_logs.aggregate(pipeline))
    stats = result[0] if result else {}
    # Count working days in MongoDB too, including employees with no attendance logs.
    working_pipeline = [
        {"$match": {"emp_code": emp_code}},
        {"$set": {
            "joined_date": {"$dateFromString": {"dateString": "$joined_on"}},
            "month_start": {"$literal": datetime.combine(month_start, time.min)},
            "next_month": {"$literal": datetime.combine(next_month, time.min)},
        }},
        {"$set": {"count_start": {"$max": ["$joined_date", "$month_start"]}}},
        {"$set": {"day_offsets": {"$range": [
            0,
            {"$max": [0, {"$dateDiff": {
                "startDate": "$count_start", "endDate": "$next_month", "unit": "day"
            }}]},
        ]}}},
        {"$set": {"working_days": {"$size": {"$filter": {
            "input": "$day_offsets",
            "as": "offset",
            "cond": {"$and": [
                {"$ne": [{"$dayOfWeek": {"$dateAdd": {
                    "startDate": "$count_start", "unit": "day", "amount": "$$offset"
                }}}, 1]},
                {"$ne": [{"$dayOfWeek": {"$dateAdd": {
                    "startDate": "$count_start", "unit": "day", "amount": "$$offset"
                }}}, 7]},
            ]},
        }}}}},
        {"$project": {"_id": 0, "working_days": 1}},
    ]
    working_result = list(db.employees.aggregate(working_pipeline))
    working_days = working_result[0]["working_days"] if working_result else 0
    present_days = round_half_up(stats.get("present_days", 0), 2)
    pct = (
        round_half_up(present_days / working_days * 100, 4)
        if working_days else None
    )
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": stats.get("leave_days", 0),
        "late_count": stats.get("late_count", 0),
        "total_late_minutes": stats.get("total_late_minutes", 0),
        "total_overtime_minutes": stats.get("total_overtime_minutes", 0),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str,
    department: Optional[str] = None,
):
    month_start, next_month = validate_month(month)
    last_day = (next_month - timedelta(days=1)).isoformat()
    log_match = date_match_for_month(month)

    employee_match = {"joined_on": {"$lte": last_day}}
    if department is not None:
        employee_match["department"] = department

    pipeline = [
        {"$match": employee_match},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"code": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$code"]},
                    {"$gte": ["$date", month_start.isoformat()]},
                    {"$lt": ["$date", next_month.isoformat()]},
                ]}}},
                {"$addFields": {
                    "weekday": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                    "presence": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    "effective_half_day": {"$ifNull": ["$half_day", False]},
                    "effective_late": {"$ifNull": ["$late_minutes", 0]},
                    "effective_overtime": {"$ifNull": ["$overtime_minutes", 0]},
                }},
            ],
            "as": "logs",
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "headcount_codes": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$cond": [
                {"$and": [
                    {"$eq": ["$logs.presence", True]},
                    {"$gte": ["$logs.weekday", 2]},
                    {"$lte": ["$logs.weekday", 6]},
                ]},
                {"$cond": ["$logs.effective_half_day", 0.5, 1.0]},
                0,
            ]}},
            "avg_work_hours": {"$avg": {"$cond": [
                {"$and": [
                    {"$eq": ["$logs.presence", True]},
                    {"$ne": ["$logs.work_hours", None]},
                ]},
                "$logs.work_hours",
                None,
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$logs.effective_late", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$logs.effective_late"},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": {"$size": "$headcount_codes"},
            "present_days": 1,
            "avg_work_hours": 1,
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
        }},
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"department": 1}},
    ]
    items = list(db.employees.aggregate(pipeline, allowDiskUse=True))
    for item in items:
        item["present_days"] = round_half_up(item.get("present_days", 0), 2)
        if item.get("avg_work_hours") is not None:
            item["avg_work_hours"] = round_half_up(item["avg_work_hours"], 2)
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    validate_month(month)
    pipeline = [
        {"$match": {**date_match_for_month(month), "late_minutes": {"$gt": 0}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1},
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "employee",
        }},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend([
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "employee.emp_code": 1}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$employee.emp_code",
            "name": "$employee.name",
            "department": "$employee.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
    ])
    items = list(db.attendance_logs.aggregate(pipeline, allowDiskUse=True))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(alias="from"),
    to_date: str = Query(alias="to"),
):
    validate_iso_date(from_date, "from")
    validate_iso_date(to_date, "to")
    start_day = date.fromisoformat(from_date)
    end_day = date.fromisoformat(to_date)
    if end_day < start_day:
        raise HTTPException(status_code=422, detail="to must be on or after from")
    if (end_day - start_day).days + 1 > 92:
        raise HTTPException(status_code=422, detail="range must not exceed 92 days")

    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(status_code=404, detail="unknown department")

    start_dt = datetime.combine(start_day, time.min, tzinfo=UTC)
    end_exclusive = datetime.combine(end_day + timedelta(days=1), time.min, tzinfo=UTC)

    # $documents + $densify create the complete date series inside MongoDB.
    pipeline = [
        {"$documents": [{"day": start_dt}]},
        {"$densify": {
            "field": "day",
            "range": {
                "step": 1,
                "unit": "day",
                "bounds": [start_dt, end_exclusive],
            },
        }},
        {"$match": {"day": {"$lt": end_exclusive}}},
        {"$set": {"date": {"$dateToString": {
            "date": "$day", "format": "%Y-%m-%d", "timezone": "UTC"
        }}}},
        {"$lookup": {
            "from": "employees",
            "let": {"d": "$date"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$department", department]},
                    {"$lte": ["$joined_on", "$$d"]},
                ]}}},
                {"$count": "count"},
            ],
            "as": "hc",
        }},
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.count", 0]}, 0]}}},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"d": "$date"},
            "pipeline": [
                {"$match": {"$expr": {"$eq": ["$date", "$$d"]}}},
                {"$lookup": {
                    "from": "employees",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "e",
                }},
                {"$unwind": "$e"},
                {"$match": {"$expr": {"$eq": ["$e.department", department]}}},
                {"$group": {
                    "_id": None,
                    "present_count": {"$sum": {"$cond": [
                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                        0,
                    ]}},
                    "late_count": {"$sum": {"$cond": [
                        {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0
                    ]}},
                }},
            ],
            "as": "daily",
        }},
        {"$set": {
            "present_count": {"$ifNull": [{"$arrayElemAt": ["$daily.present_count", 0]}, 0]},
            "late_count": {"$ifNull": [{"$arrayElemAt": ["$daily.late_count", 0]}, 0]},
            "is_working_day": {"$and": [
                {"$gte": [{"$dayOfWeek": "$day"}, 2]},
                {"$lte": [{"$dayOfWeek": "$day"}, 6]},
            ]},
        }},
        {"$set": {
            "attendance_rate": {"$cond": [
                {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                {"$divide": ["$present_count", "$headcount"]},
                None,
            ]}
        }},
        {"$setWindowFields": {
            "sortBy": {"day": 1},
            "output": {
                "moving_avg_7d": {
                    "$avg": "$attendance_rate",
                    "window": {"documents": [-6, 0]},
                }
            },
        }},
        {"$project": {
            "_id": 0,
            "date": 1,
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": 1,
            "moving_avg_7d": 1,
        }},
        {"$sort": {"date": 1}},
    ]
    items = list(db.aggregate(pipeline, allowDiskUse=True))
    for item in items:
        if item["attendance_rate"] is not None:
            item["attendance_rate"] = round_half_up(item["attendance_rate"], 4)
        if item["moving_avg_7d"] is not None:
            item["moving_avg_7d"] = round_half_up(item["moving_avg_7d"], 4)
    return {"department": department, "items": items}


# ---------------------------------------------------------------------------
# Explain endpoint
# ---------------------------------------------------------------------------
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    from_date: Optional[str] = Query(default=None, alias="from"),
    to_date: Optional[str] = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    validate_page(page, page_size)
    if endpoint in ("attendance", "attendance_list"):
        if date_from:
            validate_iso_date(date_from, "date_from")
        if date_to:
            validate_iso_date(date_to, "date_to")
        query = {}
        if emp_code is not None:
            query["emp_code"] = emp_code
        if date_from or date_to:
            query["date"] = {}
            if date_from:
                query["date"]["$gte"] = date_from
            if date_to:
                query["date"]["$lte"] = date_to
        if status:
            query["status"] = status
        cmd = {
            "find": "attendance_logs",
            "filter": query,
            "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
        explanation = db.command(
            "explain", cmd, verbosity="executionStats"
        )
        collection = "attendance_logs"

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        validate_month(month)
        pipeline = [
            {"$match": {"emp_code": emp_code, **date_match_for_month(month)}},
            {"$group": {"_id": "$emp_code", "count": {"$sum": 1}}},
        ]
        explanation = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        collection = "attendance_logs"

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        start, next_month = validate_month(month)
        last_day = (next_month - timedelta(days=1)).isoformat()
        query = {"joined_on": {"$lte": last_day}}
        if department is not None:
            query["department"] = department
        explanation = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": [
                {"$match": query},
                {"$lookup": {
                    "from": "attendance_logs",
                    "let": {"code": "$emp_code"},
                    "pipeline": [{"$match": {"$expr": {"$and": [
                        {"$eq": ["$emp_code", "$$code"]},
                        {"$gte": ["$date", start.isoformat()]},
                        {"$lt": ["$date", next_month.isoformat()]},
                    ]}}}],
                    "as": "logs",
                }},
            ], "cursor": {}},
            verbosity="executionStats",
        )
        collection = "employees"

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        validate_month(month)
        pipeline = [
            {"$match": {**date_match_for_month(month), "late_minutes": {"$gt": 0}}},
            {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}},
            {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        ]
        if department is not None:
            pipeline.append({"$match": {"employee.department": department}})
        pipeline.append({"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }})
        pipeline.append({"$match": {"rank": {"$lte": limit}}})
        explanation = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        collection = "attendance_logs"

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=422, detail="department, from and to are required")
        validate_iso_date(from_date, "from")
        validate_iso_date(to_date, "to")
        start_day, end_day = date.fromisoformat(from_date), date.fromisoformat(to_date)
        if end_day < start_day or (end_day - start_day).days + 1 > 92:
            raise HTTPException(status_code=422, detail="invalid trend date range")
        start_dt = datetime.combine(start_day, time.min, tzinfo=UTC)
        end_exclusive = datetime.combine(end_day + timedelta(days=1), time.min, tzinfo=UTC)
        pipeline = [
            {"$documents": [{"day": start_dt}]},
            {"$densify": {"field": "day", "range": {
                "step": 1, "unit": "day", "bounds": [start_dt, end_exclusive]
            }}},
            {"$match": {"day": {"$lt": end_exclusive}}},
        ]
        explanation = db.command(
            "explain",
            {"aggregate": 1, "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        collection = "database"

    else:
        raise HTTPException(status_code=422, detail="unknown endpoint")

    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": json_safe(explanation),
    }
