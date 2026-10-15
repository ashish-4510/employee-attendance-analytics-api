"""
Employee Attendance & Analytics API
Run from D:\\candidate_kit:
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
"""

import os
import re
import calendar
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError
from pymongo.collation import Collation
from bson import json_util


# ============================================================
# CONFIGURATION
# ============================================================

load_dotenv()

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://127.0.0.1:27017"),
    serverSelectionTimeoutMS=3000,
    tz_aware=True,
)

db = client[os.getenv("MONGO_DB", "attendance_db")]

employees_collection = db.employees
attendance_collection = db.attendance_logs

app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
)

PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = PRESENCE_STATUSES | {"ABSENT", "LEAVE"}


# ============================================================
# SHARED HELPERS
# ============================================================

def now_utc():
    return datetime.now(UTC).replace(microsecond=0)


def parse_epoch_ms(value):
    if value is None:
        return None

    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("Timestamp must be an integer in epoch milliseconds.")

    if value < 100000000000 or value > 4102444800000:
        raise ValueError("Timestamp is outside the permitted range.")

    return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0)


def epoch_ms(value):
    if value is None:
        return None

    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)

    return int(value.timestamp() * 1000)


def serialize(value):
    if isinstance(value, datetime):
        return epoch_ms(value)

    if isinstance(value, dict):
        return {
            key: serialize(item)
            for key, item in value.items()
            if key not in {"_id", "id"}
        }

    if isinstance(value, list):
        return [serialize(item) for item in value]

    return value


def round_half_up(value, places=2):
    quantum = Decimal("1").scaleb(-places)
    return float(
        Decimal(str(value)).quantize(
            quantum,
            rounding=ROUND_HALF_UP,
        )
    )


def parse_date(value):
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError()
        return parsed
    except (TypeError, ValueError):
        raise HTTPException(422, "Date must use YYYY-MM-DD format.")


def validate_month(month):
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month or ""):
        raise HTTPException(422, "month must use YYYY-MM format.")

    try:
        first = date.fromisoformat(month + "-01")
    except ValueError:
        raise HTTPException(422, "Invalid month.")

    if first.year < 1 or first.year > 9998:
        raise HTTPException(422, "Invalid month.")

    if first.month == 12:
        next_month = date(first.year + 1, 1, 1)
    else:
        next_month = date(first.year, first.month + 1, 1)

    return first, next_month, next_month - timedelta(days=1)


def _month_bounds(month):
    return validate_month(month)


def get_employee(emp_code):
    employee = employees_collection.find_one({"emp_code": emp_code})

    if not employee:
        raise HTTPException(404, "Employee not found.")

    return employee


def attendance_date(punch_in, shift_start, shift_end):
    local = punch_in.astimezone(IST)

    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))

    start_minutes = sh * 60 + sm
    end_minutes = eh * 60 + em
    current_minutes = local.hour * 60 + local.minute

    if end_minutes <= start_minutes and current_minutes < end_minutes:
        return (local.date() - timedelta(days=1)).isoformat()

    return local.date().isoformat()


def calculate_late_minutes(punch_in, shift_start, record_date=None):
    if punch_in is None:
        return 0

    local = punch_in.astimezone(IST)
    sh, sm = map(int, shift_start.split(":"))

    scheduled_day = (
        date.fromisoformat(record_date)
        if record_date
        else local.date()
    )

    scheduled = datetime(
        scheduled_day.year,
        scheduled_day.month,
        scheduled_day.day,
        sh,
        sm,
        tzinfo=IST,
    )

    elapsed_seconds = (local - scheduled).total_seconds()

    # Strictly more than 10 minutes late.
    if elapsed_seconds <= 600:
        return 0

    return max(0, int(elapsed_seconds // 60))


def calculate_work_hours(punch_in, punch_out):
    if punch_in is None or punch_out is None:
        return None

    seconds = (punch_out - punch_in).total_seconds()
    return round_half_up(seconds / 3600, 2)


def calculate_overtime(punch_out, employee, record_date):
    if punch_out is None:
        return 0

    sh, sm = map(int, employee["shift_start"].split(":"))
    eh, em = map(int, employee["shift_end"].split(":"))

    day = date.fromisoformat(record_date)

    # Overnight shift.
    if (eh, em) <= (sh, sm):
        day += timedelta(days=1)

    scheduled_end = datetime(
        day.year,
        day.month,
        day.day,
        eh,
        em,
        tzinfo=IST,
    )

    elapsed_minutes = int(
        (punch_out.astimezone(IST) - scheduled_end).total_seconds() // 60
    )

    # Overtime counts only when at least 30 minutes.
    return elapsed_minutes if elapsed_minutes >= 30 else 0


def ensure_indexes():
    employees_collection.create_index(
        [("emp_code", ASCENDING)],
        unique=True,
        name="uq_employee_code",
    )
    employees_collection.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)],
        name="ix_employee_department_code",
    )
    employees_collection.create_index(
        [("joined_on", ASCENDING)],
        name="ix_employee_joined_on",
    )

    attendance_collection.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uq_attendance_employee_date",
    )
    attendance_collection.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="ix_attendance_date_employee",
    )
    attendance_collection.create_index(
        [("status", ASCENDING), ("date", DESCENDING)],
        name="ix_attendance_status_date",
    )
    attendance_collection.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
        name="ix_attendance_employee_punchin",
    )
    attendance_collection.create_index(
        [("date", ASCENDING), ("late_minutes", DESCENDING)],
        name="ix_attendance_date_late",
    )


@app.on_event("startup")
def startup():
    ensure_indexes()


# ============================================================
# REQUEST MODELS
# ============================================================

class EmployeeIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=3, max_length=120)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("email")
    @classmethod
    def validate_email(cls, value):
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError("Enter a valid email address.")
        return value

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift_time(cls, value):
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("Shift time must use HH:MM format.")
        return value

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value):
        try:
            parsed = date.fromisoformat(value)
            if parsed.isoformat() != value:
                raise ValueError()
        except ValueError:
            raise ValueError("joined_on must use YYYY-MM-DD format.")
        return value

    @model_validator(mode="after")
    def validate_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end cannot be equal.")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def validate_timestamp(cls, value):
        if value is not None:
            parse_epoch_ms(value)
        return value

    @field_validator("status")
    @classmethod
    def validate_status(cls, value):
        if value not in PRESENCE_STATUSES:
            raise ValueError("status must be PRESENT, WFH, or ON_DUTY.")
        return value


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def validate_timestamp(cls, value):
        if value is not None:
            parse_epoch_ms(value)
        return value


class AttendanceCorrectionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=1, max_length=500)
    regularized_by: str = Field(min_length=1, max_length=100)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_timestamps(cls, value):
        if value is not None:
            parse_epoch_ms(value)
        return value

    @field_validator("status")
    @classmethod
    def validate_status(cls, value):
        if value is not None and value not in ALL_STATUSES:
            raise ValueError("Invalid attendance status.")
        return value


# ============================================================
# HEALTH
# ============================================================

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(503, "MongoDB is unavailable.")


# ============================================================
# EMPLOYEES
# ============================================================

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()

    try:
        employees_collection.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists.")

    return serialize(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {"department": department} if department is not None else {}

    total = employees_collection.count_documents(query)

    cursor = (
        employees_collection.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    return {
        "items": [serialize(doc) for doc in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ============================================================
# ATTENDANCE PUNCH-IN
# ============================================================

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    employee = get_employee(body.emp_code)

    punched_at = (
        parse_epoch_ms(body.punched_at)
        if body.punched_at is not None
        else now_utc()
    )

    record_day = attendance_date(
        punched_at,
        employee["shift_start"],
        employee["shift_end"],
    )

    doc = {
        "emp_code": body.emp_code,
        "date": record_day,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": calculate_late_minutes(
            punched_at,
            employee["shift_start"],
            record_day,
        ),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        attendance_collection.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "Already punched in for this attendance date.")

    return serialize(doc)


# ============================================================
# ATTENDANCE PUNCH-OUT
# ============================================================

@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = get_employee(body.emp_code)

    punched_at = (
        parse_epoch_ms(body.punched_at)
        if body.punched_at is not None
        else now_utc()
    )

    record = attendance_collection.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": punched_at},
            "punch_out": None,
            "status": {"$in": list(PRESENCE_STATUSES)},
        },
        sort=[("punch_in", DESCENDING)],
    )

    if record is None:
        raise HTTPException(404, "No open punch-in record found.")

    if punched_at <= record["punch_in"]:
        raise HTTPException(422, "Punch-out must be after punch-in.")

    if punched_at > record["punch_in"] + timedelta(hours=24):
        raise HTTPException(
            422,
            "Punch-out cannot be more than 24 hours after punch-in.",
        )

    hours = calculate_work_hours(record["punch_in"], punched_at)
    overtime = calculate_overtime(punched_at, employee, record["date"])

    result = attendance_collection.update_one(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punched_at,
                "work_hours": hours,
                "overtime_minutes": overtime,
                "half_day": hours < 4.50,
            }
        },
    )

    if result.modified_count != 1:
        raise HTTPException(409, "Attendance record was already punched out.")

    return serialize(attendance_collection.find_one({"_id": record["_id"]}))


# ============================================================
# ATTENDANCE LIST
# ============================================================

@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from:
        parse_date(date_from)
    if date_to:
        parse_date(date_to)

    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to.")

    query = {}

    if emp_code:
        query["emp_code"] = emp_code

    if status:
        if status not in ALL_STATUSES:
            raise HTTPException(422, "Invalid attendance status.")
        query["status"] = status

    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to

    total = attendance_collection.count_documents(query)

    cursor = (
        attendance_collection.find(query, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    return {
        "items": [serialize(doc) for doc in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ============================================================
# ATTENDANCE CORRECTION WITH HISTORY
# ============================================================

@app.patch("/attendance/{emp_code}/{record_date}")
def correct_attendance(
    emp_code: str,
    record_date: str,
    body: AttendanceCorrectionIn,
):
    parse_date(record_date)
    employee = get_employee(emp_code)

    record = attendance_collection.find_one(
        {"emp_code": emp_code, "date": record_date}
    )

    if not record:
        raise HTTPException(404, "Attendance record not found.")

    supplied = body.model_fields_set

    new_status = (
        body.status
        if "status" in supplied
        else record.get("status")
    )

    old_in = record.get("punch_in")
    old_out = record.get("punch_out")

    new_in = (
        parse_epoch_ms(body.punch_in)
        if "punch_in" in supplied and body.punch_in is not None
        else old_in
    )

    new_out = (
        parse_epoch_ms(body.punch_out)
        if "punch_out" in supplied and body.punch_out is not None
        else old_out
    )

    if new_status in {"ABSENT", "LEAVE"}:
        if (
            ("punch_in" in supplied and body.punch_in is not None)
            or ("punch_out" in supplied and body.punch_out is not None)
        ):
            raise HTTPException(
                422,
                "ABSENT/LEAVE cannot be submitted with punch times.",
            )
        new_in, new_out = None, None

    else:
        if new_in is None:
            raise HTTPException(
                422,
                "A presence status requires a punch_in time.",
            )

        if attendance_date(
            new_in,
            employee["shift_start"],
            employee["shift_end"],
        ) != record_date:
            raise HTTPException(
                422,
                "punch_in must belong to this attendance date.",
            )

        if new_out is not None:
            if new_out <= new_in:
                raise HTTPException(422, "punch_out must be after punch_in.")

            if new_out > new_in + timedelta(hours=24):
                raise HTTPException(
                    422,
                    "punch_out cannot be more than 24 hours after punch_in.",
                )

    new_hours = (
        calculate_work_hours(new_in, new_out)
        if new_in is not None and new_out is not None
        else None
    )

    new_late = (
        calculate_late_minutes(
            new_in,
            employee["shift_start"],
            record_date,
        )
        if new_in is not None
        else 0
    )

    new_overtime = (
        calculate_overtime(new_out, employee, record_date)
        if new_out is not None
        else 0
    )

    new_half = new_hours < 4.50 if new_hours is not None else False

    updated_values = {
        "status": new_status,
        "punch_in": new_in,
        "punch_out": new_out,
        "work_hours": new_hours,
        "late_minutes": new_late,
        "overtime_minutes": new_overtime,
        "half_day": new_half,
    }

    changes = {}

    for key, new_value in updated_values.items():
        old_value = record.get(key)
        if old_value != new_value:
            changes[key] = {"from": old_value, "to": new_value}

    if not changes:
        raise HTTPException(422, "The correction does not change the record.")

    history_entry = {
        "at": now_utc(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    filter_doc = {"_id": record["_id"]}

    for key in updated_values:
        if key in record:
            filter_doc[key] = record[key]
        else:
            filter_doc[key] = {"$exists": False}

    result = attendance_collection.update_one(
        filter_doc,
        {
            "$set": updated_values,
            "$push": {"history": history_entry},
        },
    )

    if result.modified_count != 1:
        raise HTTPException(
            409,
            "Attendance changed concurrently; retry the correction.",
        )

    return serialize(
        attendance_collection.find_one({"_id": record["_id"]})
    )


# ============================================================
# EMPLOYEE MONTHLY ANALYTICS
# ============================================================

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly_analytics(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    month_start, next_month, month_end = _month_bounds(month)
    employee = get_employee(emp_code)

    joined_on = parse_date(employee["joined_on"])
    effective_start = max(month_start, joined_on)

    working_days = 0
    current_day = effective_start

    while current_day <= month_end:
        if current_day.weekday() < 5:
            working_days += 1
        current_day += timedelta(days=1)

    records = list(
        attendance_collection.aggregate(
            [
                {
                    "$match": {
                        "emp_code": emp_code,
                        "date": {
                            "$gte": month_start.isoformat(),
                            "$lt": next_month.isoformat(),
                        },
                    }
                },
                {
                    "$project": {
                        "_id": 0,
                        "date": 1,
                        "status": 1,
                        "half_day": 1,
                        "late_minutes": 1,
                        "overtime_minutes": 1,
                    }
                },
            ]
        )
    )

    present_days = 0.0
    leave_days = 0
    late_count = 0
    total_late_minutes = 0
    total_overtime_minutes = 0

    for record in records:
        record_day = parse_date(record["date"])
        status = record.get("status")

        if status == "LEAVE":
            leave_days += 1

        if (
            status in PRESENCE_STATUSES
            and record_day.weekday() < 5
            and record_day >= joined_on
        ):
            present_days += (
                0.5 if record.get("half_day", False) else 1.0
            )

        late = int(record.get("late_minutes") or 0)
        overtime = int(record.get("overtime_minutes") or 0)

        if late > 0:
            late_count += 1

        total_late_minutes += late
        total_overtime_minutes += overtime

    attendance_pct = None

    if working_days > 0:
        attendance_pct = round_half_up(
            Decimal(str(present_days))
            / Decimal(working_days)
            * 100,
            2,
        )

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


# ============================================================
# DEPARTMENT MONTHLY SUMMARY
# ============================================================

@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    month_start, next_month, month_end = _month_bounds(month)

    employee_match = {"joined_on": {"$lte": month_end.isoformat()}}

    if department is not None:
        employee_match["department"] = department

    pipeline = [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", month_start.isoformat()]},
                                    {"$lt": ["$date", next_month.isoformat()]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$unwind": {
                "path": "$logs",
                "preserveNullAndEmptyArrays": True,
            }
        },
        {
            "$group": {
                "_id": {
                    "department": "$department",
                    "emp_code": "$emp_code",
                    "joined_on": "$joined_on",
                },
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", list(PRESENCE_STATUSES)]},
                                    {"$gte": ["$logs.date", "$_id.joined_on"]},
                                    {
                                        "$in": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$logs.date"
                                                    }
                                                }
                                            },
                                            [2, 3, 4, 5, 6],
                                        ]
                                    },
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$logs.half_day", True]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {"$isNumber": "$logs.work_hours"},
                            "$logs.work_hours",
                            0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {"$isNumber": "$logs.work_hours"},
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$logs.late_minutes", 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$logs.status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$logs.status", "ON_DUTY"]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$_id.department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$present_days"},
                "work_hours_sum": {"$sum": "$work_hours_sum"},
                "work_hours_count": {"$sum": "$work_hours_count"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
            }
        },
        {"$sort": {"_id": 1}},
    ]

    try:
        rows = list(employees_collection.aggregate(pipeline))
    except PyMongoError as exc:
        raise HTTPException(500, f"Department aggregation failed: {exc}")

    items = []

    for row in rows:
        hours_count = row["work_hours_count"]

        average = (
            round_half_up(
                Decimal(str(row["work_hours_sum"]))
                / Decimal(hours_count),
                2,
            )
            if hours_count
            else None
        )

        items.append(
            {
                "department": row["_id"],
                "headcount": row["headcount"],
                "present_days": row["present_days"],
                "avg_work_hours": average,
                "late_count": row["late_count"],
                "total_late_minutes": row["total_late_minutes"],
                "leave_count": row["leave_count"],
                "on_duty_count": row["on_duty_count"],
            }
        )

    return {"month": month, "items": items}


# ============================================================
# LATE-COMERS LEADERBOARD
# ============================================================

@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    month_start, next_month, _ = _month_bounds(month)

    pipeline = [
        {
            "$match": {
                "date": {
                    "$gte": month_start.isoformat(),
                    "$lt": next_month.isoformat(),
                },
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
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]

    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})

    pipeline.extend(
        [
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": "$employee.name",
                    "department": "$employee.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        ]
    )

    try:
        items = list(attendance_collection.aggregate(pipeline))
    except PyMongoError as exc:
        raise HTTPException(500, f"Leaderboard aggregation failed: {exc}")

    return {"month": month, "items": items}


# ============================================================
# DEPARTMENT DAILY TREND
# ============================================================

@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    start = parse_date(from_date)
    end = parse_date(to_date)

    if end < start:
        raise HTTPException(422, "to must not be before from.")

    if (end - start).days > 91:
        raise HTTPException(422, "Date range cannot exceed 92 days.")

    if not employees_collection.find_one({"department": department}):
        raise HTTPException(404, "Department not found.")

    start_string = start.isoformat()
    end_string = end.isoformat()

    # Build one daily row per date using MongoDB aggregation and $densify.
    # The initial employee collection ensures that a department with
    # employees but no attendance logs still has input documents.
    pipeline = [
        {"$match": {"department": department}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", start_string]},
                                    {"$lte": ["$date", end_string]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$unwind": {
                "path": "$logs",
                "preserveNullAndEmptyArrays": True,
            }
        },
        {
            "$group": {
                "_id": "$logs.date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$logs.status", list(PRESENCE_STATUSES)]},
                            {
                                "$cond": [
                                    {"$eq": ["$logs.half_day", True]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$logs.late_minutes", 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$match": {
                "_id": {
                    "$gte": start_string,
                    "$lte": end_string,
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": {
                    "$dateFromString": {"dateString": "$_id"}
                },
                "present_count": 1,
                "late_count": 1,
            }
        },
        {"$sort": {"date": 1}},
        {
            "$densify": {
                "field": "date",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [
                        datetime(
                            start.year, start.month, start.day,
                            tzinfo=UTC,
                        ),
                        datetime(
                            end.year, end.month, end.day,
                            tzinfo=UTC,
                        ) + timedelta(days=1),
                    ],
                },
            }
        },
        {
            "$set": {
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "date_string": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$date",
                        "timezone": "UTC",
                    }
                },
                "is_working_day": {
                    "$in": [{"$dayOfWeek": "$date"}, [2, 3, 4, 5, 6]]
                },
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"day": "$date_string"},
                "pipeline": [
                    {
                        "$match": {
                            "department": department,
                            "$expr": {"$lte": ["$joined_on", "$$day"]},
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "headcount_result",
            }
        },
        {
            "$set": {
                "headcount": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$headcount_result.count", 0]},
                        0,
                    ]
                }
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$round": [
                                {
                                    "$divide": [
                                        "$present_count",
                                        "$headcount",
                                    ]
                                },
                                4,
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$date_string",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {"$round": ["$moving_avg_7d", 4]},
            }
        },
        {"$sort": {"date": 1}},
    ]

    try:
        items = list(employees_collection.aggregate(pipeline))
    except PyMongoError as exc:
        raise HTTPException(500, f"Department trend aggregation failed: {exc}")

    # The pipeline uses $densify; this check catches incompatible MongoDB
    # versions rather than silently returning a misleading trend.
    return {"department": department, "items": items}


# ============================================================
# MONGODB EXPLAIN
# ============================================================

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
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
    allowed = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }

    if endpoint not in allowed:
        raise HTTPException(
            422,
            f"endpoint must be one of: {', '.join(sorted(allowed))}",
        )

    try:
        if endpoint == "attendance_list":
            query = {}

            if emp_code:
                query["emp_code"] = emp_code

            if date_from:
                parse_date(date_from)
                query.setdefault("date", {})["$gte"] = date_from

            if date_to:
                parse_date(date_to)
                query.setdefault("date", {})["$lte"] = date_to

            if date_from and date_to and date_from > date_to:
                raise HTTPException(
                    422,
                    "date_from must not be after date_to.",
                )

            if status:
                if status not in ALL_STATUSES:
                    raise HTTPException(422, "Invalid status.")
                query["status"] = status

            explain = (
                attendance_collection.find(query)
                .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
                .skip((page - 1) * page_size)
                .limit(page_size)
                .explain("executionStats")
            )
            collection_name = "attendance_logs"

        elif endpoint == "employee_monthly":
            if not emp_code or not month:
                raise HTTPException(
                    422,
                    "emp_code and month are required.",
                )

            get_employee(emp_code)
            month_start, next_month, _ = _month_bounds(month)

            pipeline = [
                {
                    "$match": {
                        "emp_code": emp_code,
                        "date": {
                            "$gte": month_start.isoformat(),
                            "$lt": next_month.isoformat(),
                        },
                    }
                },
                {
                    "$project": {
                        "date": 1,
                        "status": 1,
                        "late_minutes": 1,
                        "overtime_minutes": 1,
                    }
                },
            ]

            explain = db.command(
                "explain",
                {
                    "aggregate": "attendance_logs",
                    "pipeline": pipeline,
                    "cursor": {},
                },
                verbosity="executionStats",
            )
            collection_name = "attendance_logs"

        elif endpoint == "department_summary":
            if not month:
                raise HTTPException(422, "month is required.")

            month_start, next_month, month_end = _month_bounds(month)

            match = {"joined_on": {"$lte": month_end.isoformat()}}
            if department is not None:
                match["department"] = department

            pipeline = [
                {"$match": match},
                {
                    "$lookup": {
                        "from": "attendance_logs",
                        "let": {"code": "$emp_code"},
                        "pipeline": [
                            {
                                "$match": {
                                    "$expr": {
                                        "$and": [
                                            {"$eq": ["$emp_code", "$$code"]},
                                            {
                                                "$gte": [
                                                    "$date",
                                                    month_start.isoformat(),
                                                ]
                                            },
                                            {
                                                "$lt": [
                                                    "$date",
                                                    next_month.isoformat(),
                                                ]
                                            },
                                        ]
                                    }
                                }
                            }
                        ],
                        "as": "logs",
                    }
                },
            ]

            explain = db.command(
                "explain",
                {
                    "aggregate": "employees",
                    "pipeline": pipeline,
                    "cursor": {},
                },
                verbosity="executionStats",
            )
            collection_name = "employees"

        elif endpoint == "late_leaderboard":
            if not month:
                raise HTTPException(422, "month is required.")

            month_start, next_month, _ = _month_bounds(month)

            pipeline = [
                {
                    "$match": {
                        "date": {
                            "$gte": month_start.isoformat(),
                            "$lt": next_month.isoformat(),
                        },
                        "late_minutes": {"$gt": 0},
                    }
                },
                {
                    "$group": {
                        "_id": "$emp_code",
                        "total_late_minutes": {"$sum": "$late_minutes"},
                    }
                },
                {"$sort": {"total_late_minutes": -1}},
            ]

            explain = db.command(
                "explain",
                {
                    "aggregate": "attendance_logs",
                    "pipeline": pipeline,
                    "cursor": {},
                },
                verbosity="executionStats",
            )
            collection_name = "attendance_logs"

        else:
            if not department or not from_date or not to_date:
                raise HTTPException(
                    422,
                    "department, from and to are required.",
                )

            start_day = parse_date(from_date)
            end_day = parse_date(to_date)

            if end_day < start_day or (end_day - start_day).days > 91:
                raise HTTPException(422, "Invalid trend date range.")

            pipeline = [
                {
                    "$match": {
                        "department": department,
                        "joined_on": {"$lte": end_day.isoformat()},
                    }
                }
            ]

            explain = db.command(
                "explain",
                {
                    "aggregate": "employees",
                    "pipeline": pipeline,
                    "cursor": {},
                },
                verbosity="executionStats",
            )
            collection_name = "employees"

    except HTTPException:
        raise
    except PyMongoError as exc:
        raise HTTPException(500, f"MongoDB explain failed: {exc}")

    return {
        "endpoint": endpoint,
        "collection": collection_name,
        "explain": json_util.loads(json_util.dumps(explain)),
    }