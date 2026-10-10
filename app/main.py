"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real environment variables win)

Contract: openapi.yaml (HTTP + rules R1-R10). Storage: DATA_MODEL.md.
"""
import calendar
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv(override=False)  # real environment variables win over .env

log = logging.getLogger("attendance")

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,  # BSON dates come back as aware UTC datetimes, never naive
    serverSelectionTimeoutMS=3000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
NON_PRESENCE = ("ABSENT", "LEAVE")
GRACE_SECONDS = 10 * 60
OVERTIME_MIN_MINUTES = 30
HALF_DAY_HOURS = Decimal("4.50")
MAX_SHIFT = timedelta(hours=24)
MAX_TREND_DAYS = 92

EPOCH_MS_MIN = 100_000_000_000
EPOCH_MS_MAX = 4_102_444_800_000

DATE_RE = r"^\d{4}-\d{2}-\d{2}$"
MONTH_RE = r"^\d{4}-(0[1-9]|1[0-2])$"
HHMM_RE = r"^([01]\d|2[0-3]):[0-5]\d$"

Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
# Strict: floats, strings and bools are rejected; seconds-looking values fail the lower bound.
EpochMs = Annotated[int, Field(strict=True, ge=EPOCH_MS_MIN, le=EPOCH_MS_MAX)]


# --------------------------------------------------------------------------- #
# Indexes
# --------------------------------------------------------------------------- #
def ensure_indexes() -> None:
    """Idempotent: create_index is a no-op when the same index already exists."""
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uniq_emp_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="dept_joined")
    db.employees.create_index([("joined_on", ASCENDING)], name="joined_on")
    al = db.attendance_logs
    al.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uniq_emp_date")
    al.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_emp")
    al.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="status_date_emp")
    al.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="emp_punch_in")


_indexes_ready = False


def _try_ensure_indexes() -> None:
    global _indexes_ready
    if _indexes_ready:
        return
    try:
        ensure_indexes()
        _indexes_ready = True
    except PyMongoError as exc:  # MongoDB not reachable yet; /health retries
        log.warning("index creation deferred: %s", exc)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _try_ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers: time
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> aware UTC datetime truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def as_utc(dt: datetime) -> datetime:
    """Treat naive datetimes as UTC (that is what BSON stores)."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    return int(as_utc(dt).timestamp() * 1000)


def parse_hhmm(value: str) -> tuple[int, int]:
    h, m = value.split(":")
    return int(h), int(m)


def is_overnight(emp: dict) -> bool:
    return emp["shift_end"] <= emp["shift_start"]


def at_ist(day: date, hhmm: str) -> datetime:
    h, m = parse_hhmm(hhmm)
    return datetime(day.year, day.month, day.day, h, m, tzinfo=IST)


def attendance_date(punch_in: datetime, emp: dict) -> date:
    """R1: IST calendar date of the punch-in; for an overnight shift a punch-in before shift_end
    belongs to the shift that started the previous day."""
    local = as_utc(punch_in).astimezone(IST)
    d = local.date()
    if is_overnight(emp) and local.strftime("%H:%M:%S") < emp["shift_end"] + ":00":
        d -= timedelta(days=1)
    return d


def shift_bounds(day: date, emp: dict) -> tuple[datetime, datetime]:
    start = at_ist(day, emp["shift_start"])
    end_day = day + timedelta(days=1) if is_overnight(emp) else day
    return start, at_ist(end_day, emp["shift_end"])


def compute_late_minutes(punch_in: datetime, emp: dict, day: date) -> int:
    """R2: late only if strictly more than 10:00 after shift start; then floor minutes since shift start."""
    start, _ = shift_bounds(day, emp)
    secs = int((as_utc(punch_in) - start).total_seconds())
    return secs // 60 if secs > GRACE_SECONDS else 0


def round_half_up(value, places: int) -> float:
    q = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, 2 decimals half-up (exact decimal arithmetic)."""
    secs = int((as_utc(punch_out) - as_utc(punch_in)).total_seconds())
    return float((Decimal(secs) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, emp: dict, day: date) -> int:
    """R3: floor minutes after shift end, only if >= 30. Overnight shifts end on the next day."""
    _, end = shift_bounds(day, emp)
    secs = int((as_utc(punch_out) - end).total_seconds())
    minutes = secs // 60 if secs > 0 else 0
    return minutes if minutes >= OVERTIME_MIN_MINUTES else 0


def derived_fields(status: str, punch_in, punch_out, emp: dict, day: date) -> dict:
    if status in NON_PRESENCE or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(punch_in, emp, day)
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    wh = compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": wh,
        "late_minutes": late,
        "overtime_minutes": compute_overtime(punch_out, emp, day),
        "half_day": Decimal(str(wh)) < HALF_DAY_HOURS,  # R5 uses the rounded value
    }


def parse_date(value: str, field: str) -> date:
    try:
        if not re.match(DATE_RE, value):
            raise ValueError
        return date.fromisoformat(value)
    except ValueError:
        raise unprocessable(field, "must be a valid date YYYY-MM-DD")


def month_range(month: str) -> tuple[date, date]:
    y, m = map(int, month.split("-"))
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def count_weekdays(start: date, end: date) -> int:
    if end < start:
        return 0
    days = (end - start).days + 1
    full, rem = divmod(days, 7)
    n = full * 5
    for i in range(rem):
        if (start + timedelta(days=full * 7 + i)).weekday() < 5:
            n += 1
    return n


def unprocessable(field: str, msg: str, loc: str = "query") -> HTTPException:
    return HTTPException(422, [{"type": "value_error", "loc": [loc, field], "msg": msg}])


# --------------------------------------------------------------------------- #
# Helpers: serialization
# --------------------------------------------------------------------------- #
TIME_FIELDS = ("punch_in", "punch_out")


def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc.get("name"),
        "email": doc.get("email"),
        "department": doc.get("department"),
        "shift_start": doc.get("shift_start"),
        "shift_end": doc.get("shift_end"),
        "joined_on": doc.get("joined_on"),
        "created_at": dt_to_ms(doc.get("created_at")),
    }


def _change_value(key: str, v):
    return dt_to_ms(v) if key in TIME_FIELDS and isinstance(v, datetime) else v


def history_out(entry: dict) -> dict:
    changes = {
        k: {"from": _change_value(k, c.get("from")), "to": _change_value(k, c.get("to"))}
        for k, c in (entry.get("changes") or {}).items()
    }
    return {"at": dt_to_ms(entry.get("at")), "by": entry.get("by"), "reason": entry.get("reason"), "changes": changes}


def record_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [history_out(h) for h in (doc.get("history") or [])],
    }


def get_employee(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if not emp:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=HHMM_RE)
    shift_end: str = Field("18:30", pattern=HHMM_RE)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _valid_date(cls, v: str) -> str:
        if not re.match(DATE_RE, v):
            raise ValueError("must be YYYY-MM-DD")
        date.fromisoformat(v)  # raises ValueError on e.g. 2026-02-30
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    # Not Optional: an explicit null is a 422; omission means "unchanged". Derived fields sent by a client are ignored.
    status: Status = None
    punch_in: EpochMs = None
    punch_out: EpochMs = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# System / employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    _try_ensure_indexes()
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    now = datetime.now(UTC)
    doc["created_at"] = now.replace(microsecond=now.microsecond // 1000 * 1000)  # BSON keeps ms
    try:
        db.employees.insert_one(doc)  # unique index decides; no check-then-insert
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {"department": department} if department is not None else {}
    total = db.employees.count_documents(q)
    cur = db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in cur], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_date(ts, emp)
    doc = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp, day),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date): exactly one concurrent insert wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if not rec:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    pin = as_utc(rec["punch_in"])
    if ts <= pin:
        raise unprocessable("punched_at", "must be after punch_in", "body")
    if ts - pin > MAX_SHIFT:
        raise unprocessable("punched_at", "must be within 24 hours of punch_in", "body")
    fields = derived_fields(rec["status"], pin, ts, emp, date.fromisoformat(rec["date"]))
    fields["punch_out"] = ts
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},  # conditional: only one concurrent punch-out matches
        {"$set": fields},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


def attendance_query(emp_code, date_from, date_to, status) -> dict:
    q: dict[str, Any] = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    df = parse_date(date_from, "date_from") if date_from is not None else None
    dt = parse_date(date_to, "date_to") if date_to is not None else None
    if df and dt and df > dt:
        raise unprocessable("date_from", "date_from must be <= date_to")
    if df or dt:
        q["date"] = {}
        if df:
            q["date"]["$gte"] = df.isoformat()
        if dt:
            q["date"]["$lte"] = dt.isoformat()
    if status is not None:
        q["status"] = status
    return q


ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = attendance_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    cur = db.attendance_logs.find(q, {"_id": 0}).sort(ATTENDANCE_SORT).skip((page - 1) * page_size).limit(page_size)
    return {"items": [record_out(d) for d in cur], "total": total, "page": page, "page_size": page_size}


def _norm(doc: dict) -> dict:
    """Current state of the editable + derived fields, with the documented defaults for missing values."""
    return {
        "status": doc.get("status"),
        "punch_in": as_utc(doc["punch_in"]) if doc.get("punch_in") else None,
        "punch_out": as_utc(doc["punch_out"]) if doc.get("punch_out") else None,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize(body: RegularizeIn, emp_code: str, date: str = Path(...)):
    day = parse_date(date, "date")
    emp = get_employee(emp_code)
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": day.isoformat()})
    if not rec:
        raise HTTPException(404, "no attendance record for that date")

    sent = body.model_fields_set
    old = _norm(rec)
    status = body.status if "status" in sent else old["status"]
    new_in = ms_to_dt(body.punch_in) if "punch_in" in sent else old["punch_in"]
    new_out = ms_to_dt(body.punch_out) if "punch_out" in sent else old["punch_out"]

    if status in NON_PRESENCE:
        if "punch_in" in sent or "punch_out" in sent:
            raise unprocessable("status", f"{status} cannot have punch times", "body")
        new_in = new_out = None
    else:
        if new_in is None:
            raise unprocessable("punch_in", "a presence status requires punch_in", "body")
        if "punch_in" in sent and attendance_date(new_in, emp) != day:
            raise unprocessable("punch_in", "punch_in must stay on the record's attendance date", "body")
        if new_out is not None:
            if new_out <= new_in:
                raise unprocessable("punch_out", "punch_out must be after punch_in", "body")
            if new_out - new_in > MAX_SHIFT:
                raise unprocessable("punch_out", "punch_out must be within 24 hours of punch_in", "body")

    new = {"status": status, "punch_in": new_in, "punch_out": new_out}
    new.update(derived_fields(status, new_in, new_out, emp, day))
    changes = {k: {"from": old[k], "to": new[k]} for k in new if old[k] != new[k]}
    if not changes:
        raise unprocessable("body", "request changes nothing", "body")

    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    n_hist = len(rec.get("history") or [])
    # Optimistic concurrency: the update only applies if nobody corrected the record since we read it.
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": rec["_id"],
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, n_hist]},
            "status": rec.get("status"),
            "punch_in": rec.get("punch_in"),
            "punch_out": rec.get("punch_out"),
        },
        {"$set": new, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(409, "record was modified concurrently, retry")
    return record_out(updated)


# --------------------------------------------------------------------------- #
# Analytics: shared aggregation fragments
# --------------------------------------------------------------------------- #
def _weekday_expr(date_field: str) -> dict:
    """True when a "YYYY-MM-DD" string field is Mon-Fri ($dayOfWeek: 1 = Sunday, 7 = Saturday)."""
    dow = {"$dayOfWeek": {"$dateFromString": {"dateString": date_field, "format": "%Y-%m-%d"}}}
    return {"$and": [{"$gte": [dow, 2]}, {"$lte": [dow, 6]}]}


def _present_value(prefix: str) -> dict:
    """R5/R6: presence status counts 1, half day 0.5, otherwise 0."""
    return {
        "$cond": [
            {"$in": [f"{prefix}status", list(PRESENCE)]},
            {"$cond": [{"$eq": [{"$ifNull": [f"{prefix}half_day", False]}, True]}, 0.5, 1]},
            0,
        ]
    }


def _late(prefix: str) -> dict:
    return {"$ifNull": [f"{prefix}late_minutes", 0]}


def _round_half_up_expr(expr, places: int) -> dict:
    """Half-up rounding in the database ($round is half-to-even); decimal avoids binary float drift."""
    scale = 10**places
    return {
        "$cond": [
            {"$eq": [{"$ifNull": [expr, None]}, None]},
            None,
            {"$toDouble": {"$divide": [
                {"$floor": {"$add": [{"$multiply": [{"$toDecimal": expr}, scale]}, {"$toDecimal": "0.5"}]}},
                scale,
            ]}},
        ]
    }


# --------------------------------------------------------------------------- #
# Analytics: pipelines (shared by the endpoints and /admin/explain)
# --------------------------------------------------------------------------- #
def employee_monthly_pipeline(emp_code: str, month: str) -> list:
    start, end = month_range(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [_weekday_expr("$date"), _present_value("$"), 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [_late("$"), 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": _late("$")},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def department_summary_pipeline(month: str, department: Optional[str]) -> list:
    start, end = month_range(month)
    match: dict[str, Any] = {"joined_on": {"$lte": end.isoformat()}}  # R9: zero-log employees still match
    if department is not None:
        match["department"] = department
    is_presence = {"$in": ["$$l.status", list(PRESENCE)]}
    has_hours = {"$and": [is_presence, {"$isNumber": "$$l.work_hours"}]}

    def sum_over_logs(expr) -> dict:
        return {"$sum": {"$map": {"input": "$logs", "as": "l", "in": expr}}}

    return [
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
                {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "late_minutes": 1, "work_hours": 1}},
            ],
            "as": "logs",
        }},
        # Per employee first (keeps every employee as exactly one row, so headcount is a plain count).
        {"$project": {
            "_id": 0,
            "department": 1,
            "present": sum_over_logs({"$cond": [_weekday_expr("$$l.date"), _present_value("$$l."), 0]}),
            "wh_sum": sum_over_logs({"$cond": [has_hours, "$$l.work_hours", 0]}),
            "wh_n": sum_over_logs({"$cond": [has_hours, 1, 0]}),
            "late_count": sum_over_logs({"$cond": [{"$gt": [_late("$$l."), 0]}, 1, 0]}),
            "late_total": sum_over_logs(_late("$$l.")),
            "leave": sum_over_logs({"$cond": [{"$eq": ["$$l.status", "LEAVE"]}, 1, 0]}),
            "on_duty": sum_over_logs({"$cond": [{"$eq": ["$$l.status", "ON_DUTY"]}, 1, 0]}),
        }},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$present"},
            "wh_sum": {"$sum": "$wh_sum"},
            "wh_n": {"$sum": "$wh_n"},
            "late_count": {"$sum": "$late_count"},
            "total_late_minutes": {"$sum": "$late_total"},
            "leave_count": {"$sum": "$leave"},
            "on_duty_count": {"$sum": "$on_duty"},
        }},
        {"$sort": {"_id": 1}},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": 1,
            "present_days": _round_half_up_expr("$present_days", 2),
            # Mean over records (sum / count), not a mean of per-employee means.
            "avg_work_hours": _round_half_up_expr(
                {"$cond": [{"$gt": ["$wh_n", 0]}, {"$divide": ["$wh_sum", "$wh_n"]}, None]}, 2),
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
        }},
    ]


def late_leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    start, end = month_range(month)
    pipeline: list = [
        {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs whose employee does not exist are dropped
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})  # rank inside the department only
    pipeline += [
        # Standard competition ranking (1, 2, 2, 4); limit applies to the rank, not the row count.
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$emp.name",
            "department": "$emp.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
    ]
    return pipeline


def department_trend_pipeline(department: str, d_from: date, d_to: date) -> list:
    f, t = d_from.isoformat(), d_to.isoformat()
    as_date = lambda s: {"$dateFromString": {"dateString": s, "format": "%Y-%m-%d", "timezone": "UTC"}}  # noqa: E731
    lo = datetime(d_from.year, d_from.month, d_from.day, tzinfo=UTC)
    hi = datetime(d_to.year, d_to.month, d_to.day, tzinfo=UTC) + timedelta(days=1)  # densify upper bound is exclusive
    rate = {
        "$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": ["$present_count", "$headcount"]},
            None,
        ]
    }
    return [
        {"$match": {"department": department}},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": f, "$lte": t}}},
                {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "late_minutes": 1}},
            ],
            "as": "logs",
        }},
        # Employees without logs survive as a zero row on `from`, so densify always has a seed document.
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": {"$ifNull": ["$logs.date", f]},
            "present_count": {"$sum": _present_value("$logs.")},
            "late_count": {"$sum": {"$cond": [{"$gt": [_late("$logs."), 0]}, 1, 0]}},
        }},
        {"$set": {"day": as_date("$_id")}},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [lo, hi]}}},
        {"$set": {
            "date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d", "timezone": "UTC"}},
            "is_working_day": {"$in": [{"$dayOfWeek": "$day"}, [2, 3, 4, 5, 6]]},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
        }},
        # R9 headcount per day; indexed on (department, joined_on).
        {"$lookup": {
            "from": "employees",
            "let": {"d": "$date"},
            "pipeline": [
                {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$d"]}}},
                {"$count": "n"},
            ],
            "as": "hc",
        }},
        {"$set": {"headcount": {"$ifNull": [{"$first": "$hc.n"}, 0]}}},
        {"$set": {"rate_raw": rate}},
        # Window = current row + up to 6 preceding rows inside the range; $avg ignores nulls (null if all null).
        {"$setWindowFields": {
            "sortBy": {"day": 1},
            "output": {"ma_raw": {"$avg": "$rate_raw", "window": {"documents": [-6, 0]}}},
        }},
        {"$sort": {"day": 1}},
        {"$project": {
            "_id": 0,
            "date": 1,
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": _round_half_up_expr("$rate_raw", 4),
            "moving_avg_7d": _round_half_up_expr("$ma_raw", 4),
        }},
    ]


# --------------------------------------------------------------------------- #
# Analytics: endpoints
# --------------------------------------------------------------------------- #
MonthQ = Query(..., pattern=MONTH_RE)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = MonthQ):
    emp = get_employee(emp_code)
    start, end = month_range(month)
    joined = date.fromisoformat(emp["joined_on"])
    working_days = count_weekdays(max(start, joined), end)  # R7
    rows = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, month)))
    r = rows[0] if rows else {}
    present = r.get("present_days", 0)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present, 2),
        "leave_days": r.get("leave_days", 0),
        "late_count": r.get("late_count", 0),
        "total_late_minutes": r.get("total_late_minutes", 0),
        "total_overtime_minutes": r.get("total_overtime_minutes", 0),
        "attendance_pct": round_half_up(Decimal(str(present)) / working_days * 100, 2) if working_days else None,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = MonthQ, department: Optional[str] = None):
    items = list(db.employees.aggregate(department_summary_pipeline(month, department)))
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = MonthQ, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    items = list(db.attendance_logs.aggregate(late_leaderboard_pipeline(month, limit, department)))
    return {"month": month, "items": items}


def trend_range(d_from: str, d_to: str) -> tuple[date, date]:
    f, t = parse_date(d_from, "from"), parse_date(d_to, "to")
    if t < f:
        raise unprocessable("to", "to must be >= from")
    if (t - f).days + 1 > MAX_TREND_DAYS:
        raise unprocessable("to", f"range must not exceed {MAX_TREND_DAYS} days")
    return f, t


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    d_from: str = Query(..., alias="from"),
    d_to: str = Query(..., alias="to"),
):
    f, t = trend_range(d_from, d_to)
    if not db.employees.find_one({"department": department}, {"_id": 1}):
        raise HTTPException(404, "unknown department")
    items = list(db.employees.aggregate(department_trend_pipeline(department, f, t)))
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin: explain
# --------------------------------------------------------------------------- #
ExplainEndpoint = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]


def _require(**params) -> None:
    missing = [k for k, v in params.items() if v is None]
    if missing:
        raise HTTPException(422, [
            {"type": "missing", "loc": ["query", k], "msg": "Field required"} for k in missing
        ])


@app.get("/admin/explain/{endpoint}")
def explain(
    endpoint: ExplainEndpoint,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_RE),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    d_from: Optional[str] = Query(None, alias="from"),
    d_to: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        coll = "attendance_logs"
        cmd = {
            "find": coll,
            "filter": attendance_query(emp_code, date_from, date_to, status),
            "projection": {"_id": 0},
            "sort": dict(ATTENDANCE_SORT),
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
    else:
        if endpoint == "employee_monthly":
            _require(emp_code=emp_code, month=month)
            coll, pipeline = "attendance_logs", employee_monthly_pipeline(emp_code, month)
        elif endpoint == "department_summary":
            _require(month=month)
            coll, pipeline = "employees", department_summary_pipeline(month, department)
        elif endpoint == "late_leaderboard":
            _require(month=month)
            coll, pipeline = "attendance_logs", late_leaderboard_pipeline(month, limit, department)
        else:
            _require(department=department, **{"from": d_from, "to": d_to})
            f, t = trend_range(d_from, d_to)
            coll, pipeline = "employees", department_trend_pipeline(department, f, t)
        cmd = {"aggregate": coll, "pipeline": pipeline, "cursor": {}}
    result = db.command("explain", cmd, verbosity="executionStats")
    return {"endpoint": endpoint, "collection": coll, "explain": json.loads(json_util.dumps(result))}
