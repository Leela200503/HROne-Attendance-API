import os
import math
from datetime import datetime, timedelta, timezone
from typing import Optional
from decimal import Decimal, ROUND_HALF_UP

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

@app.on_event("startup")
def startup_db_client():
    db.employees.create_index("emp_code", unique=True)
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    db.attendance_logs.create_index("date")

def _ms(dt: datetime) -> Optional[int]:
    if not dt:
        return None
    return int(dt.timestamp() * 1000)

def truncate_to_seconds(dt: datetime) -> datetime:
    return dt.replace(microsecond=0)

def get_attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    punch_in_ist = punch_in.astimezone(IST)
    is_overnight = shift_end <= shift_start
    if is_overnight:
        h, m = map(int, shift_end.split(":"))
        shift_end_time = punch_in_ist.replace(hour=h, minute=m, second=0, microsecond=0)
        if punch_in_ist.time() < shift_end_time.time():
            return (punch_in_ist.date() - timedelta(days=1)).isoformat()
    return punch_in_ist.date().isoformat()

def compute_late_minutes(punch_in: datetime, shift_start: str, att_date: str) -> int:
    h, m = map(int, shift_start.split(":"))
    start = datetime.fromisoformat(att_date).replace(hour=h, minute=m, tzinfo=IST)
    minutes = math.floor((punch_in - start).total_seconds() / 60)
    return minutes if minutes > 10 else 0

def compute_work_hours(punch_in: datetime, punch_out: datetime) -> Optional[float]:
    if not punch_out:
        return None
    hours = (punch_out - punch_in).total_seconds() / 3600
    return float(Decimal(str(hours)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))

def compute_overtime(punch_out: datetime, shift_end: str, shift_start: str, att_date: str) -> int:
    if not punch_out:
        return 0
    h, m = map(int, shift_end.split(":"))
    is_overnight = shift_end <= shift_start
    end_date = datetime.fromisoformat(att_date)
    if is_overnight:
        end_date += timedelta(days=1)
    end = end_date.replace(hour=h, minute=m, tzinfo=IST)
    minutes = math.floor((punch_out - end).total_seconds() / 60)
    return minutes if minutes >= 30 else 0

def compute_half_day(work_hours: Optional[float]) -> bool:
    if work_hours is None:
        return False
    return work_hours < 4.50

class EmployeeIn(BaseModel):
    emp_code: str = Field(..., pattern=r'^EMP\d{4,6}$')
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    shift_end: str = Field("18:30", pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    joined_on: str

    @model_validator(mode='after')
    def validate_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self

class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: str = "PRESENT"
    
    @field_validator("status")
    def validate_status(cls, v):
        if v not in ("PRESENT", "WFH", "ON_DUTY"):
            raise ValueError("status must be PRESENT, WFH, or ON_DUTY")
        return v

class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)

class RegularizeIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

    @field_validator("status")
    def validate_status(cls, v):
        if v and v not in ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"):
            raise ValueError("Invalid status")
        return v

def format_employee_doc(doc):
    doc.pop("_id", None)
    if "created_at" in doc and isinstance(doc["created_at"], datetime):
        doc["created_at"] = _ms(doc["created_at"])
    return doc

def format_attendance_doc(doc):
    doc.pop("_id", None)
    if "punch_in" in doc and isinstance(doc["punch_in"], datetime):
        doc["punch_in"] = _ms(doc["punch_in"])
    if "punch_out" in doc and isinstance(doc["punch_out"], datetime):
        doc["punch_out"] = _ms(doc["punch_out"])
    if "history" in doc:
        for entry in doc["history"]:
            if "at" in entry and isinstance(entry["at"], datetime):
                entry["at"] = _ms(entry["at"])
            if "changes" in entry:
                for k in ["punch_in", "punch_out"]:
                    if k in entry["changes"]:
                        if isinstance(entry["changes"][k].get("from"), datetime):
                            entry["changes"][k]["from"] = _ms(entry["changes"][k]["from"])
                        if isinstance(entry["changes"][k].get("to"), datetime):
                            entry["changes"][k]["to"] = _ms(entry["changes"][k]["to"])
    return doc

def get_month_range(month: str):
    y, m = map(int, month.split("-"))
    start = f"{y:04d}-{m:02d}-01"
    if m == 12:
        end = f"{y+1:04d}-01-01"
    else:
        end = f"{y:04d}-{m+1:02d}-01"
    last_day = (datetime.fromisoformat(end) - timedelta(days=1)).date().isoformat()
    return start, last_day

def build_attendance_list_query(qp):
    q = {}
    if "emp_code" in qp:
        q["emp_code"] = qp["emp_code"]
    if "date_from" in qp or "date_to" in qp:
        if "date_from" in qp and "date_to" in qp and qp["date_from"] > qp["date_to"]:
            raise HTTPException(422, "date_from > date_to")
        q["date"] = {}
        if "date_from" in qp: q["date"]["$gte"] = qp["date_from"]
        if "date_to" in qp: q["date"]["$lte"] = qp["date_to"]
    if "status" in qp:
        q["status"] = qp["status"]
    return q

def build_employee_monthly_pipeline(emp_code: str, month: str):
    start_dt, end_dt = get_month_range(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start_dt, "$lte": end_dt}}},
        {"$group": {
            "_id": "$emp_code",
            "present_days": {
                "$sum": {
                    "$cond": [
                        {"$and": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$in": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [1, 2, 3, 4, 5]]}
                        ]},
                        {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1.0]},
                        0
                    ]
                }
            },
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}
        }}
    ]

def build_department_summary_pipeline(month: str, department: Optional[str] = None):
    start_dt, end_dt = get_month_range(month)
    match_stage = {"joined_on": {"$lte": end_dt}}
    if department:
        match_stage["department"] = department
    return [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {"$eq": ["$emp_code", "$$emp"]},
                            "date": {"$gte": start_dt, "$lte": end_dt}
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "headcount_set": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": [
                                {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$in": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": {"$ifNull": ["$logs.date", "2000-01-01"]}}}}, [1, 2, 3, 4, 5]]}
                            ]},
                            {"$cond": [{"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]}, 0.5, 1.0]},
                            0
                        ]
                    }
                },
                "work_hours": {
                    "$push": {
                        "$cond": [
                            {"$and": [
                                {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$ne": ["$logs.work_hours", None]}
                            ]},
                            "$logs.work_hours",
                            "$$REMOVE"
                        ]
                    }
                },
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}
            }
        },
        {
            "$project": {
                "department": "$_id",
                "headcount": {"$size": "$headcount_set"},
                "present_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
                "avg_work_hours": {"$avg": "$work_hours"},
                "_id": 0
            }
        },
        {"$sort": {"department": 1}}
    ]

def build_late_leaderboard_pipeline(month: str, limit: int = 10, department: Optional[str] = None):
    start_dt, end_dt = get_month_range(month)
    logs_match = {
        "date": {"$gte": start_dt, "$lte": end_dt},
        "late_minutes": {"$gt": 0}
    }
    pipeline = [
        {"$match": logs_match},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1}
        }},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "emp"
        }},
        {"$unwind": "$emp"}
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline.extend([
        {
            "$project": {
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
                "_id": 0
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                }
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}}
    ])
    return pipeline

def build_department_trend_pipeline(department: str, from_date: str, to_date: str):
    return [
        {"$match": {"department": department}},
        {"$addFields": {"temp_date": {"$dateFromString": {"dateString": from_date}}}},
        {"$limit": 1},
        {
            "$densify": {
                "field": "temp_date",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [
                        {"$dateFromString": {"dateString": from_date}},
                        {"$dateFromString": {"dateString": (datetime.fromisoformat(to_date) + timedelta(days=1)).date().isoformat()}}
                    ]
                }
            }
        },
        {"$project": {"date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$temp_date"}}}},
        {
            "$lookup": {
                "from": "employees",
                "let": {"d": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "department": department,
                            "$expr": {"$lte": ["$joined_on", "$$d"]}
                        }
                    },
                    {"$project": {"emp_code": 1}}
                ],
                "as": "emps"
            }
        },
        {"$addFields": {"headcount": {"$size": "$emps"}}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"d": "$date", "emps": "$emps.emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$date", "$$d"]},
                                    {"$in": ["$emp_code", "$$emps"]}
                                ]
                            }
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {
            "$addFields": {
                "is_working_day": {
                    "$in": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [1, 2, 3, 4, 5]]
                },
                "present_count": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0,
                        "in": {
                            "$add": [
                                "$$value",
                                {"$cond": [
                                    {"$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$cond": [{"$eq": [{"$ifNull": ["$$this.half_day", False]}, True]}, 0.5, 1.0]},
                                    0
                                ]}
                            ]
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {"$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]}
                        }
                    }
                }
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None
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
                        "window": {
                            "documents": [-6, "current"]
                        }
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1
            }
        }
    ]

@app.get("/health")
def health():
    try:
        client.admin.command('ping')
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "db not ready")

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = truncate_to_seconds(datetime.now(timezone.utc))
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return format_employee_doc(doc)

@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = 1, page_size: int = 20):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q).sort("emp_code", ASCENDING).skip(skip).limit(page_size))
    items = [format_employee_doc(d) for d in items]
    return {"items": items, "total": total, "page": page, "page_size": page_size}

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    ts = datetime.fromtimestamp(body.punched_at / 1000, tz=timezone.utc) if body.punched_at else datetime.now(timezone.utc)
    ts = truncate_to_seconds(ts)
    
    d = get_attendance_date(ts, emp["shift_start"], emp["shift_end"])
    
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], d),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
        
    return format_attendance_doc(doc)

@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
        
    ts = datetime.fromtimestamp(body.punched_at / 1000, tz=timezone.utc) if body.punched_at else datetime.now(timezone.utc)
    ts = truncate_to_seconds(ts)
    
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, 
        sort=[("punch_in", DESCENDING)]
    )
    if not rec:
        raise HTTPException(404, "no punch-in found")
        
    if rec.get("punch_out"):
        raise HTTPException(409, "record is already punched out")
        
    if (ts - rec["punch_in"]).total_seconds() > 24 * 3600:
        raise HTTPException(422, "punched_at is more than 24 hours after punch_in")
        
    wh = compute_work_hours(rec["punch_in"], ts)
    om = compute_overtime(ts, emp["shift_end"], emp["shift_start"], rec["date"])
    hd = compute_half_day(wh)
    
    update_doc = {
        "$set": {
            "punch_out": ts,
            "work_hours": wh,
            "overtime_minutes": om,
            "half_day": hd
        }
    }
    
    res = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        update_doc,
        return_document=True
    )
    
    if not res:
        raise HTTPException(409, "record is already punched out")
        
    return format_attendance_doc(res)

@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    # build q
    try:
        q = build_attendance_list_query({"emp_code": emp_code, "date_from": date_from, "date_to": date_to, "status": status})
    except Exception as e:
        raise HTTPException(422, str(e))
        
    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    docs = list(db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size))
    
    items = [format_attendance_doc(d) for d in docs]
    return {"items": items, "total": total, "page": page, "page_size": page_size}

@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
        
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(404, "record not found")
        
    changes = {}
    new_status = body.status if body.status else rec.get("status")
    
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT/LEAVE records cannot have punch times")
    
    new_punch_in = None
    if body.punch_in is not None:
        new_punch_in = truncate_to_seconds(datetime.fromtimestamp(body.punch_in / 1000, tz=timezone.utc))
    else:
        new_punch_in = rec.get("punch_in")
        
    new_punch_out = None
    if body.punch_out is not None:
        new_punch_out = truncate_to_seconds(datetime.fromtimestamp(body.punch_out / 1000, tz=timezone.utc))
    else:
        new_punch_out = rec.get("punch_out")

    if new_status in ("PRESENT", "WFH", "ON_DUTY"):
        if not new_punch_in:
            raise HTTPException(422, "presence status requires punch_in")
        d = get_attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
        if d != date:
            raise HTTPException(422, "punch_in date does not match record date")
            
    if new_punch_in and new_punch_out:
        if new_punch_out <= new_punch_in:
            raise HTTPException(422, "punch_out <= punch_in")
        if (new_punch_out - new_punch_in).total_seconds() > 24 * 3600:
            raise HTTPException(422, "punch_out > 24h after punch_in")
            
    if new_status in ("ABSENT", "LEAVE"):
        new_punch_in = None
        new_punch_out = None

    new_vals = {
        "status": new_status,
        "punch_in": new_punch_in,
        "punch_out": new_punch_out,
    }
    
    if new_status in ("ABSENT", "LEAVE"):
        new_vals["work_hours"] = None
        new_vals["late_minutes"] = 0
        new_vals["overtime_minutes"] = 0
        new_vals["half_day"] = False
    else:
        new_vals["late_minutes"] = compute_late_minutes(new_punch_in, emp["shift_start"], date) if new_punch_in else 0
        if new_punch_out:
            wh = compute_work_hours(new_punch_in, new_punch_out)
            new_vals["work_hours"] = wh
            new_vals["overtime_minutes"] = compute_overtime(new_punch_out, emp["shift_end"], emp["shift_start"], date)
            new_vals["half_day"] = compute_half_day(wh)
        else:
            new_vals["work_hours"] = None
            new_vals["overtime_minutes"] = 0
            new_vals["half_day"] = False
            
    for k, v in new_vals.items():
        old_v = rec.get(k)
        if k == "late_minutes" and old_v is None: old_v = 0
        if k == "overtime_minutes" and old_v is None: old_v = 0
        if k == "half_day" and old_v is None: old_v = False
        if v != old_v:
            changes[k] = {"from": old_v, "to": v}

    if not changes:
        raise HTTPException(422, "request changes nothing")
        
    history_entry = {
        "at": truncate_to_seconds(datetime.now(timezone.utc)),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes
    }
    
    update_doc = {
        "$set": new_vals,
        "$push": {"history": history_entry}
    }
    
    q_hist = {"$size": len(rec["history"])} if "history" in rec else {"$exists": False}
    # Wait, size match works. But if absent, could be missing.
    # $or is safer
    or_cond = [{"history": {"$exists": False}}, {"history": {"$size": 0}}] if not rec.get("history") else [{"history": {"$size": len(rec["history"])}}]
    
    res = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "$or": or_cond},
        update_doc,
        return_document=True
    )
    if not res:
        raise HTTPException(409, "concurrent edit detected")
        
    return format_attendance_doc(res)

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
        
    start_dt, end_dt = get_month_range(month)
    pipeline = build_employee_monthly_pipeline(emp_code, month)
    
    res = list(db.attendance_logs.aggregate(pipeline))
    stats = res[0] if res else {
        "present_days": 0.0, "leave_days": 0, "late_count": 0,
        "total_late_minutes": 0, "total_overtime_minutes": 0
    }
    
    calc_start = max(emp["joined_on"], start_dt)
    calc_end = end_dt
    
    working_days = 0
    if calc_start <= calc_end:
        cur = datetime.fromisoformat(calc_start)
        last = datetime.fromisoformat(calc_end)
        while cur <= last:
            if cur.weekday() < 5:
                working_days += 1
            cur += timedelta(days=1)
            
    pct = None
    if working_days > 0:
        pct = float(Decimal(str(stats["present_days"] / working_days * 100)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))
        
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": float(stats["present_days"]),
        "leave_days": stats["leave_days"],
        "late_count": stats["late_count"],
        "total_late_minutes": stats["total_late_minutes"],
        "total_overtime_minutes": stats["total_overtime_minutes"],
        "attendance_pct": pct
    }

@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    pipeline = build_department_summary_pipeline(month, department)
    res = list(db.employees.aggregate(pipeline))
    
    for r in res:
        if r["avg_work_hours"] is not None:
            r["avg_work_hours"] = float(Decimal(str(r["avg_work_hours"])).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))
            
    res = [r for r in res if r["headcount"] > 0]
    return {"month": month, "items": res}

@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = 10, department: Optional[str] = None):
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}

@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str, to_date: str):
    if from_date > to_date:
        raise HTTPException(422, "from_date > to_date")
        
    d1 = datetime.fromisoformat(from_date)
    d2 = datetime.fromisoformat(to_date)
    if (d2 - d1).days > 92:
        raise HTTPException(422, "range exceeds 92 days")
        
    emp_count = db.employees.count_documents({"department": department})
    if emp_count == 0:
        raise HTTPException(404, "unknown department")
        
    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    res = list(db.employees.aggregate(pipeline))
    
    for r in res:
        if r["attendance_rate"] is not None:
            r["attendance_rate"] = float(Decimal(str(r["attendance_rate"])).quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP))
        if r["moving_avg_7d"] is not None:
            r["moving_avg_7d"] = float(Decimal(str(r["moving_avg_7d"])).quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP))
            
    return {"department": department, "items": res}

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: str, request: Request):
    valid_endpoints = ["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]
    if endpoint not in valid_endpoints:
        raise HTTPException(422, "invalid endpoint")
        
    qp = dict(request.query_params)
    
    if endpoint == "attendance_list":
        try:
            q = build_attendance_list_query(qp)
        except Exception as e:
            raise HTTPException(422, str(e))
        page = int(qp.get("page", 1))
        page_size = int(qp.get("page_size", 20))
        skip = (page - 1) * page_size
        
        cmd = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": page_size
        }
        explain_res = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_res}
        
    elif endpoint == "employee_monthly":
        if "emp_code" not in qp or "month" not in qp:
            raise HTTPException(422, "missing required parameters")
        pipeline = build_employee_monthly_pipeline(qp["emp_code"], qp["month"])
        cmd = {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}
        explain_res = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_res}
        
    elif endpoint == "department_summary":
        if "month" not in qp:
            raise HTTPException(422, "missing required parameters")
        pipeline = build_department_summary_pipeline(qp["month"], qp.get("department"))
        cmd = {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}
        explain_res = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_res}
        
    elif endpoint == "late_leaderboard":
        if "month" not in qp:
            raise HTTPException(422, "missing required parameters")
        limit = int(qp.get("limit", 10))
        pipeline = build_late_leaderboard_pipeline(qp["month"], limit, qp.get("department"))
        cmd = {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}
        explain_res = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_res}
        
    elif endpoint == "department_trend":
        if "department" not in qp or "from" not in qp or "to" not in qp:
            raise HTTPException(422, "missing required parameters")
        pipeline = build_department_trend_pipeline(qp["department"], qp["from"], qp["to"])
        cmd = {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}
        explain_res = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_res}
