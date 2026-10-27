from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import os
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("FREEBITE_DB", BASE_DIR / "freebite.db"))
FRONTEND_DIR = BASE_DIR.parent / "frontend"

def now_utc():
    return datetime.now(timezone.utc)

def iso(dt):
    return dt.isoformat()

def connect():
    con = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    return con

def init_db():
    with connect() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS food_drops (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            food_name TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'Meals',
            portions_total INTEGER NOT NULL CHECK(portions_total > 0),
            portions_left INTEGER NOT NULL CHECK(portions_left >= 0),
            location TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            organizer TEXT NOT NULL DEFAULT 'Campus Organizer',
            event_name TEXT NOT NULL DEFAULT 'Campus event',
            description TEXT NOT NULL DEFAULT '',
            image_url TEXT NOT NULL DEFAULT '',
            allergens TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active'
        );
        CREATE TABLE IF NOT EXISTS claims (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            food_id INTEGER NOT NULL REFERENCES food_drops(id),
            student_name TEXT NOT NULL,
            claim_code TEXT NOT NULL UNIQUE,
            claimed_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'claimed'
        );
        CREATE INDEX IF NOT EXISTS idx_food_status ON food_drops(status, created_at);
        CREATE INDEX IF NOT EXISTS idx_claim_food ON claims(food_id);
        """)
        count = con.execute("SELECT COUNT(*) FROM food_drops").fetchone()[0]
        if count == 0:
            now = now_utc()
            samples = [
                ("Veg Biryani", "Meals", 15, "Seminar Hall B", now+timedelta(minutes=38), "Tech Club", "AI Workshop", "Freshly packed portions from the workshop lunch.", "", "May contain nuts"),
                ("Sandwiches", "Snacks", 8, "Main Canteen Counter", now+timedelta(minutes=24), "Design Society", "Design Meetup", "Cheese and vegetable sandwiches.", "", "Contains dairy, gluten"),
                ("Fruit Cups", "Healthy", 12, "Library Courtyard", now+timedelta(minutes=55), "Eco Club", "Green Campus Drive", "Fresh seasonal fruit cups.", "", ""),
            ]
            for s in samples:
                con.execute("""INSERT INTO food_drops
                (food_name,category,portions_total,portions_left,location,expires_at,organizer,event_name,description,image_url,allergens,created_at,status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'active')""",
                (s[0],s[1],s[2],s[2],s[3],iso(s[4]),s[5],s[6],s[7],s[8],s[9],iso(now)))
        con.commit()

def serialize_food(row, con):
    d = dict(row)
    d["claims_count"] = con.execute("SELECT COUNT(*) FROM claims WHERE food_id=? AND status='claimed'", (d["id"],)).fetchone()[0]
    d["is_expired"] = datetime.fromisoformat(d["expires_at"]) <= now_utc()
    if d["is_expired"] and d["status"] == "active":
        d["status"] = "expired"
    return d

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

app = FastAPI(title="FreeBite API", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

class FoodCreate(BaseModel):
    food_name: str = Field(min_length=2, max_length=100)
    category: str = Field(default="Meals", max_length=30)
    portions: int = Field(gt=0, le=10000)
    location: str = Field(min_length=2, max_length=160)
    expires_at: datetime
    organizer: str = Field(default="Campus Organizer", min_length=2, max_length=100)
    event_name: str = Field(default="Campus event", max_length=120)
    description: str = Field(default="", max_length=500)
    image_url: str = Field(default="", max_length=500)
    allergens: str = Field(default="", max_length=200)

class ClaimCreate(BaseModel):
    student_name: str = Field(min_length=2, max_length=100)

@app.get("/api/health")
def health():
    return {"status": "ok", "service": "FreeBite API"}

@app.get("/api/foods")
def list_foods(status: str = "active", q: str = "", category: str = "All", sort: str = "newest"):
    with connect() as con:
        rows = con.execute("SELECT * FROM food_drops ORDER BY created_at DESC").fetchall()
        items = []
        for row in rows:
            item = serialize_food(row, con)
            if status == "active" and (item["status"] != "active" or item["portions_left"] <= 0 or item["is_expired"]):
                continue
            if status == "all" and item["status"] not in ("active", "expired", "claimed"):
                continue
            needle = q.strip().lower()
            if needle and needle not in (item["food_name"] + " " + item["location"] + " " + item["organizer"] + " " + item["event_name"]).lower():
                continue
            if category.lower() not in ("all", "") and item["category"].lower() != category.lower():
                continue
            items.append(item)
        if sort == "ending":
            items.sort(key=lambda x: x["expires_at"])
        elif sort == "newest":
            items.sort(key=lambda x: x["created_at"], reverse=True)
        return {"items": items}

@app.post("/api/foods", status_code=201)
def create_food(food: FoodCreate):
    expires = food.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires <= now_utc():
        raise HTTPException(400, "Collection deadline must be in the future.")
    created = iso(now_utc())
    with connect() as con:
        cur = con.execute("""INSERT INTO food_drops
        (food_name,category,portions_total,portions_left,location,expires_at,organizer,event_name,description,image_url,allergens,created_at,status)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'active')""",
        (food.food_name.strip(), food.category, food.portions, food.portions, food.location.strip(),
         iso(expires), food.organizer.strip(), food.event_name.strip(), food.description.strip(),
         food.image_url.strip(), food.allergens.strip(), created))
        con.commit()
        row = con.execute("SELECT * FROM food_drops WHERE id=?", (cur.lastrowid,)).fetchone()
        return serialize_food(row, con)

@app.post("/api/foods/{food_id}/claim", status_code=201)
def claim_food(food_id: int, payload: ClaimCreate):
    with connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM food_drops WHERE id=?", (food_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Food listing not found.")
        expires = datetime.fromisoformat(row["expires_at"])
        if expires <= now_utc() or row["status"] != "active":
            raise HTTPException(409, "This food drop has expired.")
        if row["portions_left"] < 1:
            raise HTTPException(409, "Sorry, all portions have been claimed.")
        # Demo identity: one claim per student per listing.
        existing = con.execute("SELECT id FROM claims WHERE food_id=? AND lower(student_name)=lower(?) AND status='claimed'",
                               (food_id, payload.student_name.strip())).fetchone()
        if existing:
            raise HTTPException(409, "You already claimed a portion from this food drop.")
        import secrets, string
        code = "FB-" + "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))
        con.execute("UPDATE food_drops SET portions_left=portions_left-1 WHERE id=? AND portions_left>0", (food_id,))
        con.execute("INSERT INTO claims(food_id,student_name,claim_code,claimed_at,status) VALUES (?,?,?,?, 'claimed')",
                    (food_id, payload.student_name.strip(), code, iso(now_utc())))
        con.commit()
        updated = con.execute("SELECT * FROM food_drops WHERE id=?", (food_id,)).fetchone()
        claim = con.execute("SELECT * FROM claims WHERE claim_code=?", (code,)).fetchone()
        return {"food": serialize_food(updated, con), "claim": dict(claim)}

@app.get("/api/claims")
def list_claims(student_name: str):
    if not student_name.strip():
        raise HTTPException(400, "student_name is required.")
    with connect() as con:
        rows = con.execute("""SELECT c.*, f.food_name, f.location, f.expires_at, f.category
            FROM claims c JOIN food_drops f ON f.id=c.food_id
            WHERE lower(c.student_name)=lower(?) ORDER BY c.claimed_at DESC""", (student_name.strip(),)).fetchall()
        return {"items": [dict(r) for r in rows]}

@app.get("/api/impact")
def impact():
    with connect() as con:
        claimed = con.execute("SELECT COUNT(*) FROM claims WHERE status='claimed'").fetchone()[0]
        active = con.execute("SELECT COUNT(*) FROM food_drops WHERE status='active' AND portions_left>0 AND expires_at>?", (iso(now_utc()),)).fetchone()[0]
        donated = con.execute("SELECT COALESCE(SUM(portions_total),0) FROM food_drops").fetchone()[0]
        # Transparent estimate: 0.35 kg per portion; adjust for your campus methodology.
        saved_kg = round(claimed * 0.35, 1)
        return {"plates_claimed": claimed, "estimated_kg_avoided": saved_kg, "active_drops": active, "portions_posted": donated, "assumption_kg_per_portion": 0.35}

@app.get("/api/organizer/summary")
def organizer_summary():
    with connect() as con:
        drops = con.execute("SELECT * FROM food_drops ORDER BY created_at DESC").fetchall()
        total = len(drops)
        portions = con.execute("SELECT COALESCE(SUM(portions_total),0) FROM food_drops").fetchone()[0]
        claimed = con.execute("SELECT COUNT(*) FROM claims WHERE status='claimed'").fetchone()[0]
        active = con.execute("SELECT COUNT(*) FROM food_drops WHERE status='active' AND portions_left>0 AND expires_at>?", (iso(now_utc()),)).fetchone()[0]
        return {"total_drops": total, "portions_posted": portions, "portions_claimed": claimed, "active_drops": active}

if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
