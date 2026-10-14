"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (read from environment; local .env loaded as fallback)
"""
import calendar
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, Field, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

# IST timezone (UTC+05:30)
IST = timezone(timedelta(hours=5, minutes=30))

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)
db = client[os.getenv("MONGO_DB", "attendance_db")]

app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
)


# --------------------------------------------------------------------------- #
# Database Startup
# --------------------------------------------------------------------------- #
@app.on_event("startup")
def startup():
    try:
        db.employees.create_index([("emp_code", 1)], unique=True)
        db.employees.create_index([("department", 1), ("joined_on", 1)])
        db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
        db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
        db.attendance_logs.create_index([("emp_code", 1), ("date", -1)])
        db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
        db.attendance_logs.create_index([("date", 1), ("late_minutes", -1)])
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Helpers & Business Rules (R1 - R8)
# --------------------------------------------------------------------------- #
def round_half_up(val: Optional[float], decimals: int = 2) -> Optional[float]:
    if val is None:
        return None
    d = Decimal(str(val))
    target = Decimal("10") ** -decimals
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


def truncate_ms(epoch_ms: int) -> int:
    return (epoch_ms // 1000) * 1000


def ms_to_utc_dt(epoch_ms: int) -> datetime:
    trunc_sec = epoch_ms // 1000
    return datetime.fromtimestamp(trunc_sec, tz=timezone.utc)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def compute_attendance_date(punch_in_ist: datetime, shift_start: str, shift_end: str) -> str:
    sh_h, sh_m = map(int, shift_start.split(":"))
    eh_h, eh_m = map(int, shift_end.split(":"))
    is_overnight = (eh_h, eh_m) <= (sh_h, sh_m)

    punch_time = (punch_in_ist.hour, punch_in_ist.minute, punch_in_ist.second)
    shift_end_time = (eh_h, eh_m, 0)

    # R1: overnight shift punch-in before shift_end belongs to previous day
    if is_overnight and punch_time < shift_end_time:
        att_date = punch_in_ist.date() - timedelta(days=1)
    else:
        att_date = punch_in_ist.date()
    return att_date.isoformat()


def compute_late_minutes(punch_in_ist: datetime, att_date_str: str, shift_start: str) -> int:
    sh_h, sh_m = map(int, shift_start.split(":"))
    att_d = date.fromisoformat(att_date_str)
    start_dt = datetime(att_d.year, att_d.month, att_d.day, sh_h, sh_m, 0, tzinfo=IST)
    diff_sec = int((punch_in_ist - start_dt).total_seconds())

    # R2: grace period 10 minutes (600 seconds)
    if diff_sec > 600:
        return diff_sec // 60
    return 0


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    # R4: seconds / 3600, rounded half-up to 2 decimals
    diff_sec = int((punch_out_dt - punch_in_dt).total_seconds())
    val = Decimal(diff_sec) / Decimal(3600)
    return float(val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_half_day(work_hours: Optional[float]) -> bool:
    # R5: half day when work_hours < 4.50
    return work_hours is not None and work_hours < 4.50


def compute_overtime(punch_out_ist: datetime, att_date_str: str, shift_start: str, shift_end: str) -> int:
    sh_h, sh_m = map(int, shift_start.split(":"))
    eh_h, eh_m = map(int, shift_end.split(":"))
    att_d = date.fromisoformat(att_date_str)
    is_overnight = (eh_h, eh_m) <= (sh_h, sh_m)

    end_d = att_d + timedelta(days=1) if is_overnight else att_d
    end_dt = datetime(end_d.year, end_d.month, end_d.day, eh_h, eh_m, 0, tzinfo=IST)
    diff_sec = int((punch_out_ist - end_dt).total_seconds())

    # R3: minimum 30 minutes overtime
    if diff_sec >= 30 * 60:
        return diff_sec // 60
    return 0


def format_employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc.get("created_at")),
    }


def format_attendance_out(doc: dict) -> dict:
    history_out = []
    for h in doc.get("history", []):
        entry = {
            "at": dt_to_ms(h.get("at")),
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": {},
        }
        for k, v in h.get("changes", {}).items():
            from_val = v.get("from")
            to_val = v.get("to")
            if k in ("punch_in", "punch_out"):
                if isinstance(from_val, datetime):
                    from_val = dt_to_ms(from_val)
                if isinstance(to_val, datetime):
                    to_val = dt_to_ms(to_val)
            entry["changes"][k] = {"from": from_val, "to": to_val}
        history_out.append(entry)

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history_out,
    }


# --------------------------------------------------------------------------- #
# Request Schemas
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def validate_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000, strict=True)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000, strict=True)


class RegularizeIn(BaseModel):
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000, strict=True)
    punch_out: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000, strict=True)


# --------------------------------------------------------------------------- #
# System Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "Database unavailable")


# --------------------------------------------------------------------------- #
# Employee Endpoints
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return format_employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    items = list(
        db.employees.find(q)
        .sort("emp_code", 1)
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items": [format_employee_out(d) for d in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Attendance Endpoints
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is not None:
        p_ms = truncate_ms(body.punched_at)
        p_dt = ms_to_utc_dt(p_ms)
    else:
        now_dt = datetime.now(timezone.utc)
        p_ms = truncate_ms(int(now_dt.timestamp() * 1000))
        p_dt = ms_to_utc_dt(p_ms)

    p_ist = p_dt.astimezone(IST)
    att_date = compute_attendance_date(p_ist, emp["shift_start"], emp["shift_end"])
    late_mins = compute_late_minutes(p_ist, att_date, emp["shift_start"])

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": p_dt,
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
        raise HTTPException(409, "already punched in for this date")

    return format_attendance_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is not None:
        p_ms = truncate_ms(body.punched_at)
        p_dt = ms_to_utc_dt(p_ms)
    else:
        now_dt = datetime.now(timezone.utc)
        p_ms = truncate_ms(int(now_dt.timestamp() * 1000))
        p_dt = ms_to_utc_dt(p_ms)

    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": p_dt, "$ne": None}},
        sort=[("punch_in", -1)],
    )
    if not record:
        raise HTTPException(404, "No punch-in record found")

    if record.get("punch_out") is not None:
        raise HTTPException(409, "Record already punched out")

    in_ms = dt_to_ms(record["punch_in"])
    if p_ms <= in_ms:
        raise HTTPException(422, "punched_at must be strictly after punch_in")
    if p_ms - in_ms > 24 * 3600 * 1000:
        raise HTTPException(422, "punch_out cannot be more than 24 hours after punch_in")

    w_hours = compute_work_hours(record["punch_in"], p_dt)
    h_day = compute_half_day(w_hours)
    ot_mins = compute_overtime(p_dt.astimezone(IST), record["date"], emp["shift_start"], emp["shift_end"])

    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": p_dt,
                "work_hours": w_hours,
                "overtime_minutes": ot_mins,
                "half_day": h_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(409, "Record already punched out")

    return format_attendance_out(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from cannot be greater than date_to")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    items = list(
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items": [format_attendance_out(d) for d in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeIn = ...,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(404, "Attendance record not found")

    if body.status in ("ABSENT", "LEAVE") and (body.punch_in is not None or body.punch_out is not None):
        raise HTTPException(422, "Cannot supply punch times when status is ABSENT or LEAVE")

    target_status = body.status if body.status is not None else rec.get("status")

    if target_status in ("ABSENT", "LEAVE"):
        target_in_dt = None
        target_out_dt = None
        target_w_hours = None
        target_late_mins = 0
        target_ot_mins = 0
        target_half_day = False
    else:
        if body.punch_in is not None:
            target_in_ms = truncate_ms(body.punch_in)
            target_in_dt = ms_to_utc_dt(target_in_ms)
        else:
            target_in_dt = rec.get("punch_in")

        if target_in_dt is None:
            raise HTTPException(422, "Presence status requires a punch_in")

        p_in_ist = target_in_dt.astimezone(IST)
        computed_date = compute_attendance_date(p_in_ist, emp["shift_start"], emp["shift_end"])
        if computed_date != date:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")

        if body.punch_out is not None:
            target_out_ms = truncate_ms(body.punch_out)
            target_out_dt = ms_to_utc_dt(target_out_ms)
        else:
            target_out_dt = rec.get("punch_out")

        if target_out_dt is not None:
            in_ms = dt_to_ms(target_in_dt)
            out_ms = dt_to_ms(target_out_dt)
            if out_ms <= in_ms:
                raise HTTPException(422, "punch_out must be strictly after punch_in")
            if out_ms - in_ms > 24 * 3600 * 1000:
                raise HTTPException(422, "punch_out cannot be more than 24 hours after punch_in")

            target_w_hours = compute_work_hours(target_in_dt, target_out_dt)
            target_half_day = compute_half_day(target_w_hours)
            target_ot_mins = compute_overtime(
                target_out_dt.astimezone(IST),
                date,
                emp["shift_start"],
                emp["shift_end"],
            )
        else:
            target_w_hours = None
            target_half_day = False
            target_ot_mins = 0

        target_late_mins = compute_late_minutes(p_in_ist, date, emp["shift_start"])

    # Track actual changes
    changes_mongo = {}
    old_status = rec.get("status")
    if target_status != old_status:
        changes_mongo["status"] = {"from": old_status, "to": target_status}

    old_in = rec.get("punch_in")
    if dt_to_ms(old_in) != dt_to_ms(target_in_dt):
        changes_mongo["punch_in"] = {"from": old_in, "to": target_in_dt}

    old_out = rec.get("punch_out")
    if dt_to_ms(old_out) != dt_to_ms(target_out_dt):
        changes_mongo["punch_out"] = {"from": old_out, "to": target_out_dt}

    old_wh = rec.get("work_hours")
    if old_wh != target_w_hours:
        changes_mongo["work_hours"] = {"from": old_wh, "to": target_w_hours}

    old_late = rec.get("late_minutes", 0)
    if old_late != target_late_mins:
        changes_mongo["late_minutes"] = {"from": old_late, "to": target_late_mins}

    old_ot = rec.get("overtime_minutes", 0)
    if old_ot != target_ot_mins:
        changes_mongo["overtime_minutes"] = {"from": old_ot, "to": target_ot_mins}

    old_hd = rec.get("half_day", False)
    if old_hd != target_half_day:
        changes_mongo["half_day"] = {"from": old_hd, "to": target_half_day}

    if not changes_mongo:
        raise HTTPException(422, "Request changes nothing")

    now_dt = datetime.now(timezone.utc)
    history_entry = {
        "at": now_dt,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes_mongo,
    }

    current_history_len = len(rec.get("history", []))
    update_filter = {
        "_id": rec["_id"],
        f"history.{current_history_len}": {"$exists": False},
    }

    updated = db.attendance_logs.find_one_and_update(
        update_filter,
        {
            "$set": {
                "status": target_status,
                "punch_in": target_in_dt,
                "punch_out": target_out_dt,
                "work_hours": target_w_hours,
                "late_minutes": target_late_mins,
                "overtime_minutes": target_ot_mins,
                "half_day": target_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(409, "Concurrent modification detected")

    return format_attendance_out(updated)


# --------------------------------------------------------------------------- #
# Analytics Pipelines
# --------------------------------------------------------------------------- #
def get_month_days_range(month: str):
    parts = month.split("-")
    year, m = int(parts[0]), int(parts[1])
    _, last_day = calendar.monthrange(year, m)
    return date(year, m, 1), date(year, m, last_day)


def build_employee_monthly_pipeline(emp_code: str, month: str) -> list:
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": f"{month}-01", "$lte": f"{month}-31"},
            }
        },
        {
            "$project": {
                "emp_code": 1,
                "date": 1,
                "status": 1,
                "half_day": {"$ifNull": ["$half_day", False]},
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "day_of_week": {
                    "$dayOfWeek": {
                        "date": {"$dateFromString": {"dateString": "$date"}}
                    }
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
                                    {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
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


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    m_start, m_end = get_month_days_range(month)
    joined = date.fromisoformat(emp["joined_on"])
    calc_start = max(m_start, joined)

    # R7: working days are Mon-Fri from joined_on
    working_days = 0
    if calc_start <= m_end:
        cur = calc_start
        while cur <= m_end:
            if cur.weekday() < 5:
                working_days += 1
            cur += timedelta(days=1)

    pipeline = build_employee_monthly_pipeline(emp_code, month)
    agg_res = list(db.attendance_logs.aggregate(pipeline))

    if agg_res:
        row = agg_res[0]
        present_days = float(row.get("present_days", 0.0))
        leave_days = int(row.get("leave_days", 0))
        late_count = int(row.get("late_count", 0))
        total_late = int(row.get("total_late_minutes", 0))
        total_ot = int(row.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days > 0:
        pct = round_half_up((present_days / working_days) * 100, 2)
    else:
        pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": pct,
    }


def build_department_summary_pipeline(month: str, department: Optional[str] = None) -> list:
    m_start, m_end = get_month_days_range(month)
    m_end_str = m_end.isoformat()

    # R9: headcount includes anyone who joined on or before month end
    emp_match = {"joined_on": {"$lte": m_end_str}}
    if department:
        emp_match["department"] = department

    return [
        {"$match": emp_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"ecode": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$ecode"]},
                                    {"$gte": ["$date", f"{month}-01"]},
                                    {"$lte": ["$date", f"{month}-31"]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "all_logs": {"$push": "$logs"},
            }
        },
        {
            "$project": {
                "department": "$_id",
                "_id": 0,
                "headcount": 1,
                "logs": {
                    "$reduce": {
                        "input": "$all_logs",
                        "initialValue": [],
                        "in": {"$concatArrays": ["$$value", "$$this"]},
                    }
                },
            }
        },
        {
            "$project": {
                "department": 1,
                "headcount": 1,
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {
                                                "$in": [
                                                    {
                                                        "$dayOfWeek": {
                                                            "date": {
                                                                "$dateFromString": {
                                                                    "dateString": "$$l.date"
                                                                }
                                                            }
                                                        }
                                                    },
                                                    [2, 3, 4, 5, 6],
                                                ]
                                            },
                                            {
                                                "$in": [
                                                    "$$l.status",
                                                    ["PRESENT", "WFH", "ON_DUTY"],
                                                ]
                                            },
                                        ]
                                    },
                                    {
                                        "$cond": [
                                            {"$eq": ["$$l.half_day", True]},
                                            0.5,
                                            1.0,
                                        ]
                                    },
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "work_hours_list": {
                    "$filter": {
                        "input": "$logs",
                        "as": "l",
                        "cond": {
                            "$and": [
                                {
                                    "$in": [
                                        "$$l.status",
                                        ["PRESENT", "WFH", "ON_DUTY"],
                                    ]
                                },
                                {"$ne": ["$$l.work_hours", None]},
                            ]
                        },
                    }
                },
                "late_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]},
                                    1,
                                    0,
                                ]
                            },
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$ifNull": ["$$l.late_minutes", 0]},
                        }
                    }
                },
                "leave_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$eq": ["$$l.status", "LEAVE"]}, 1, 0]},
                        }
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$eq": ["$$l.status", "ON_DUTY"]}, 1, 0]},
                        }
                    }
                },
            }
        },
        {
            "$project": {
                "department": 1,
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": [{"$size": "$work_hours_list"}, 0]},
                        {"$avg": "$work_hours_list.work_hours"},
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    pipeline = build_department_summary_pipeline(month, department)
    results = list(db.employees.aggregate(pipeline))
    items = []
    for r in results:
        items.append(
            {
                "department": r["department"],
                "headcount": r["headcount"],
                "present_days": round_half_up(r["present_days"], 2),
                "avg_work_hours": round_half_up(r["avg_work_hours"], 2),
                "late_count": r["late_count"],
                "total_late_minutes": r["total_late_minutes"],
                "leave_count": r["leave_count"],
                "on_duty_count": r["on_duty_count"],
            }
        )
    return {"month": month, "items": items}


def build_late_leaderboard_pipeline(month: str, limit: int, department: Optional[str] = None) -> list:
    pipeline = [
        {
            "$match": {
                "date": {"$gte": f"{month}-01", "$lte": f"{month}-31"},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend(
        [
            {
                "$group": {
                    "_id": "$emp_code",
                    "emp_code": {"$first": "$emp_code"},
                    "name": {"$first": "$emp.name"},
                    "department": {"$first": "$emp.department"},
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1},
                }
            },
            {"$match": {"total_late_minutes": {"$gt": 0}}},
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": 1,
                    "name": 1,
                    "department": 1,
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ]
    )
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


def build_department_trend_pipeline(department: str, from_date: str, to_date: str) -> list:
    return [
        {
            "$match": {
                "date": {"$gte": from_date, "$lte": to_date},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
            }
        },
        {
            "$unionWith": {
                "coll": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$limit": 1},
                    {
                        "$project": {
                            "_id": 0,
                            "date": from_date,
                            "present_count": {"$literal": 0.0},
                            "late_count": {"$literal": 0},
                        }
                    },
                    {
                        "$unionWith": {
                            "coll": "employees",
                            "pipeline": [
                                {"$match": {"department": department}},
                                {"$limit": 1},
                                {
                                    "$project": {
                                        "_id": 0,
                                        "date": to_date,
                                        "present_count": {"$literal": 0.0},
                                        "late_count": {"$literal": 0},
                                    }
                                },
                            ],
                        }
                    },
                ],
            }
        },
        {
            "$group": {
                "_id": "$_id",
                "present_count": {"$sum": "$present_count"},
                "late_count": {"$sum": "$late_count"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "date_val": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "date_val",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": "full",
                },
            }
        },
        {
            "$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_val"}},
                "date_val": 1,
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "day_of_week": {"$dayOfWeek": {"date": "$date_val"}},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"cur_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$cur_date"]},
                                ]
                            }
                        }
                    },
                    {"$count": "cnt"},
                ],
                "as": "hc",
            }
        },
        {
            "$project": {
                "date": 1,
                "date_val": 1,
                "is_working_day": {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                "headcount": {"$ifNull": [{"$first": "$hc.cnt"}, 0]},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$project": {
                "date": 1,
                "date_val": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                },
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date_val": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"],
                        },
                    }
                },
            }
        },
        {"$sort": {"date": 1}},
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    d_from = date.fromisoformat(from_date)
    d_to = date.fromisoformat(to_date)
    if d_to < d_from:
        raise HTTPException(422, "'to' date cannot be earlier than 'from' date")
    if (d_to - d_from).days > 92:
        raise HTTPException(422, "Range cannot exceed 92 days")

    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(404, "Unknown department")

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    raw_items = list(db.attendance_logs.aggregate(pipeline))

    items = []
    for it in raw_items:
        items.append(
            {
                "date": it["date"],
                "is_working_day": it["is_working_day"],
                "headcount": it["headcount"],
                "present_count": it["present_count"],
                "late_count": it["late_count"],
                "attendance_rate": round_half_up(it["attendance_rate"], 4),
                "moving_avg_7d": round_half_up(it["moving_avg_7d"], 4),
            }
        )

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(..., pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    try:
        if endpoint == "attendance_list":
            if date_from and date_to and date_from > date_to:
                raise HTTPException(422, "date_from cannot be greater than date_to")
            q = {}
            if emp_code:
                q["emp_code"] = emp_code
            if date_from or date_to:
                q["date"] = {}
                if date_from:
                    q["date"]["$gte"] = date_from
                if date_to:
                    q["date"]["$lte"] = date_to
            if status:
                q["status"] = status
            skip = (page - 1) * page_size
            explain_doc = (
                db.attendance_logs.find(q)
                .sort([("date", -1), ("emp_code", 1)])
                .skip(skip)
                .limit(page_size)
                .explain("executionStats")
            )
            return {
                "endpoint": endpoint,
                "collection": "attendance_logs",
                "explain": json_util.loads(json_util.dumps(explain_doc)),
            }

        elif endpoint == "employee_monthly":
            if not emp_code or not month:
                raise HTTPException(422, "Missing emp_code or month")
            pipeline = build_employee_monthly_pipeline(emp_code, month)
            explain_doc = db.command(
                "explain",
                {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
                verbosity="executionStats",
            )
            return {
                "endpoint": endpoint,
                "collection": "attendance_logs",
                "explain": json_util.loads(json_util.dumps(explain_doc)),
            }

        elif endpoint == "department_summary":
            if not month:
                raise HTTPException(422, "Missing month")
            pipeline = build_department_summary_pipeline(month, department)
            explain_doc = db.command(
                "explain",
                {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
                verbosity="executionStats",
            )
            return {
                "endpoint": endpoint,
                "collection": "employees",
                "explain": json_util.loads(json_util.dumps(explain_doc)),
            }

        elif endpoint == "late_leaderboard":
            if not month:
                raise HTTPException(422, "Missing month")
            pipeline = build_late_leaderboard_pipeline(month, limit, department)
            explain_doc = db.command(
                "explain",
                {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
                verbosity="executionStats",
            )
            return {
                "endpoint": endpoint,
                "collection": "attendance_logs",
                "explain": json_util.loads(json_util.dumps(explain_doc)),
            }

        elif endpoint == "department_trend":
            if not department or not from_date or not to_date:
                raise HTTPException(422, "Missing department, from, or to")
            d_from = date.fromisoformat(from_date)
            d_to = date.fromisoformat(to_date)
            if d_to < d_from or (d_to - d_from).days > 92:
                raise HTTPException(422, "Invalid from/to range")
            pipeline = build_department_trend_pipeline(department, from_date, to_date)
            explain_doc = db.command(
                "explain",
                {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
                verbosity="executionStats",
            )
            return {
                "endpoint": endpoint,
                "collection": "attendance_logs",
                "explain": json_util.loads(json_util.dumps(explain_doc)),
            }

    except PyMongoError as e:
        raise HTTPException(500, str(e))
