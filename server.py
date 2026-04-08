"""
Chock — A self-hosted approval queue for AI agent teams.
Human-in-the-loop approvals with contingent conditions.
Approve, deny, or approve-with-conditions. Agents get structured payloads back.
"""

import sqlite3
import json
import uuid
import os
from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional
import urllib.request
import threading

DB_PATH = Path(os.environ.get("CHOCK_DB", str(Path(__file__).parent / "data" / "approvals.db")))
STATIC_DIR = Path(__file__).parent / "static"
HOST = os.environ.get("CHOCK_HOST", "127.0.0.1")
PORT = int(os.environ.get("CHOCK_PORT", "8796"))
WEBHOOK_URLS = [u.strip() for u in os.environ.get("CHOCK_WEBHOOKS", "").split(",") if u.strip()]

app = FastAPI(title="Chock", version="1.0.0")


# --- Webhooks ---

def fire_webhooks(event: str, request_id: str, details: dict):
    """Fire webhooks for approval events. Fire-and-forget in background thread."""
    if not WEBHOOK_URLS:
        return
    payload = json.dumps({
        "event": event,
        "request_id": request_id,
        "details": details,
        "timestamp": now_iso()
    }).encode()
    def _fire():
        for url in WEBHOOK_URLS:
            try:
                req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
                urllib.request.urlopen(req, timeout=5)
            except Exception:
                pass
    threading.Thread(target=_fire, daemon=True).start()


def fire_callback(callback_url: str, request_id: str, result: dict):
    """Fire a callback to the requesting agent when their approval is responded to."""
    payload = json.dumps({
        "request_id": request_id,
        "result": result,
        "timestamp": now_iso()
    }).encode()
    def _fire():
        try:
            req = urllib.request.Request(callback_url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=10)
        except Exception:
            pass
    threading.Thread(target=_fire, daemon=True).start()


# --- Database ---

def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS requests (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                plan TEXT NOT NULL DEFAULT '',
                requester TEXT DEFAULT '',
                context TEXT DEFAULT '',
                tags TEXT DEFAULT '[]',
                priority TEXT DEFAULT 'normal',
                status TEXT NOT NULL DEFAULT 'pending',
                callback_url TEXT DEFAULT '',
                conditions_schema TEXT DEFAULT '[]',
                response TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT,
                action TEXT NOT NULL,
                actor TEXT DEFAULT '',
                details TEXT DEFAULT '',
                timestamp TEXT NOT NULL
            )
        """)


@contextmanager
def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# --- Models ---

VALID_PRIORITIES = ["low", "normal", "high", "critical"]
VALID_STATUSES = ["pending", "approved", "denied", "conditional"]

class ConditionField(BaseModel):
    key: str
    label: str
    type: str = "text"  # text, number, boolean, select
    options: list[str] = []  # for select type
    required: bool = False
    description: str = ""

class ApprovalCreate(BaseModel):
    title: str
    plan: str = ""
    requester: str = ""
    context: str = ""
    tags: list[str] = []
    priority: str = "normal"
    callback_url: str = ""
    conditions_schema: list[ConditionField] = []

class ApprovalRespond(BaseModel):
    status: str  # approved, denied, conditional
    responder: str = ""
    comment: str = ""
    conditions: dict = {}  # filled in for conditional approvals


# --- Helpers ---

def parse_request(row) -> dict:
    """Convert a DB row to a request dict with parsed JSON fields."""
    req = dict(row)
    req["tags"] = json.loads(req["tags"])
    for field in ("conditions_schema", "response"):
        if req.get(field):
            try:
                req[field] = json.loads(req[field])
            except (json.JSONDecodeError, TypeError):
                pass
        elif field == "conditions_schema":
            req[field] = []
        elif field == "response":
            req[field] = None
    return req


# --- API Routes ---

@app.post("/api/requests", status_code=201)
def create_request(req: ApprovalCreate):
    """Submit a new approval request. Returns the request ID for polling."""
    if req.priority not in VALID_PRIORITIES:
        raise HTTPException(400, f"Invalid priority. Use one of: {VALID_PRIORITIES}")

    request_id = f"chk-{uuid.uuid4().hex[:8]}"
    ts = now_iso()

    with get_db() as db:
        db.execute(
            """INSERT INTO requests (id, title, plan, requester, context, tags, priority,
               status, callback_url, conditions_schema, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)""",
            (request_id, req.title, req.plan, req.requester, req.context,
             json.dumps(req.tags), req.priority, req.callback_url,
             json.dumps([c.model_dump() for c in req.conditions_schema]),
             ts, ts)
        )
        db.execute(
            "INSERT INTO activity (request_id, action, actor, details, timestamp) VALUES (?, ?, ?, ?, ?)",
            (request_id, "created", req.requester, req.title[:100], ts)
        )

    fire_webhooks("request_created", request_id, {"title": req.title, "requester": req.requester, "priority": req.priority})

    return {"id": request_id, "status": "pending"}


@app.get("/api/requests")
def list_requests(status: Optional[str] = None, requester: Optional[str] = None,
                  q: Optional[str] = None, priority: Optional[str] = None,
                  tag: Optional[str] = None):
    """List approval requests with optional filters."""
    query = "SELECT * FROM requests WHERE 1=1"
    params = []
    if status:
        if status not in VALID_STATUSES:
            raise HTTPException(400, f"Invalid status. Use one of: {VALID_STATUSES}")
        query += " AND status = ?"
        params.append(status)
    if requester:
        query += " AND requester = ?"
        params.append(requester)
    if priority:
        if priority not in VALID_PRIORITIES:
            raise HTTPException(400, f"Invalid priority. Use one of: {VALID_PRIORITIES}")
        query += " AND priority = ?"
        params.append(priority)
    if q:
        query += " AND (title LIKE ? OR plan LIKE ? OR context LIKE ?)"
        params.extend([f"%{q}%", f"%{q}%", f"%{q}%"])
    if tag:
        query += " AND tags LIKE ?"
        params.append(f'%"{tag}"%')
    query += " ORDER BY created_at DESC"

    with get_db() as db:
        rows = db.execute(query, params).fetchall()
    return [parse_request(row) for row in rows]


@app.get("/api/requests/{request_id}")
def get_request(request_id: str):
    """Get a single approval request."""
    with get_db() as db:
        row = db.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Request not found")
    return parse_request(row)


@app.get("/api/pending")
def list_pending():
    """List all pending approval requests, oldest first. This is the approval queue."""
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM requests WHERE status = 'pending' ORDER BY "
            "CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 WHEN 'low' THEN 3 END, "
            "created_at ASC"
        ).fetchall()
    return [parse_request(row) for row in rows]


@app.post("/api/requests/{request_id}/respond")
def respond_to_request(request_id: str, resp: ApprovalRespond):
    """Respond to an approval request. Status: approved, denied, or conditional.
    For conditional, provide structured conditions matching the conditions_schema."""
    if resp.status not in ["approved", "denied", "conditional"]:
        raise HTTPException(400, "Status must be 'approved', 'denied', or 'conditional'")

    with get_db() as db:
        existing = db.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
        if not existing:
            raise HTTPException(404, "Request not found")

        if existing["status"] != "pending":
            raise HTTPException(400, f"Already responded: {existing['status']}")

        # Validate conditions for conditional approvals
        if resp.status == "conditional":
            schema_raw = existing["conditions_schema"]
            if schema_raw:
                schema = json.loads(schema_raw)
                required_keys = [f["key"] for f in schema if f.get("required")]
                missing = [k for k in required_keys if k not in resp.conditions]
                if missing:
                    raise HTTPException(400, f"Missing required conditions: {missing}")

        ts = now_iso()
        response_data = {
            "status": resp.status,
            "responder": resp.responder,
            "comment": resp.comment,
            "conditions": resp.conditions,
            "responded_at": ts,
        }

        db.execute(
            "UPDATE requests SET status = ?, response = ?, updated_at = ? WHERE id = ?",
            (resp.status, json.dumps(response_data), ts, request_id)
        )
        detail = resp.status + (f": {resp.comment}" if resp.comment else "")
        db.execute(
            "INSERT INTO activity (request_id, action, actor, details, timestamp) VALUES (?, ?, ?, ?, ?)",
            (request_id, f"responded_{resp.status}", resp.responder, detail, ts)
        )

    fire_webhooks(f"request_{resp.status}", request_id, {
        "title": existing["title"],
        "responder": resp.responder,
        "conditions": resp.conditions if resp.status == "conditional" else {},
    })

    # Fire callback to the requesting agent if they provided a callback URL
    if existing["callback_url"]:
        fire_callback(existing["callback_url"], request_id, response_data)

    return {"status": resp.status, "request_id": request_id, "response": response_data}


@app.delete("/api/requests/{request_id}")
def cancel_request(request_id: str, requester: Optional[str] = None):
    """Cancel a pending approval request. Only pending requests can be cancelled."""
    with get_db() as db:
        existing = db.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
        if not existing:
            raise HTTPException(404, "Request not found")
        if existing["status"] != "pending":
            raise HTTPException(400, f"Cannot cancel — already {existing['status']}")

        db.execute("DELETE FROM requests WHERE id = ?", (request_id,))
        db.execute(
            "INSERT INTO activity (request_id, action, actor, details, timestamp) VALUES (?, ?, ?, ?, ?)",
            (request_id, "cancelled", requester or existing["requester"] or "", existing["title"], now_iso())
        )
    return {"status": "cancelled"}


@app.get("/api/activity")
def get_activity(limit: int = 50):
    """Get recent activity log."""
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM activity ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/health")
def health():
    return {"status": "ok", "service": "chock", "timestamp": now_iso()}


# --- Static files & SPA fallback ---

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


# --- Startup ---

@app.on_event("startup")
def startup():
    init_db()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT)
