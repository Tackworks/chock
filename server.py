"""
Chock — A self-hosted approval queue for AI agent teams.
Human-in-the-loop approvals with contingent conditions.
Approve, deny, or approve-with-conditions. Agents get structured payloads back.
"""

import sqlite3
import json
import uuid
import os
import hmac
import ipaddress
from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator
from typing import Optional
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
import urllib.request
import threading

DB_PATH = Path(os.environ.get("CHOCK_DB", str(Path(__file__).parent / "data" / "approvals.db")))
STATIC_DIR = Path(__file__).parent / "static"
HOST = os.environ.get("CHOCK_HOST", "127.0.0.1")
PORT = int(os.environ.get("CHOCK_PORT", "8796"))
WEBHOOK_URLS = [u.strip() for u in os.environ.get("CHOCK_WEBHOOKS", "").split(",") if u.strip()]
API_KEY = os.environ.get("CHOCK_API_KEY", "")

# --- SSRF Protection ---

_BLOCKED_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
]

def _is_private_ip(addr) -> bool:
    """Check if an IP address is in a blocked private range."""
    if addr == ipaddress.ip_address("::1"):
        return True
    for network in _BLOCKED_NETWORKS:
        if addr in network:
            return True
    return False


def is_url_safe(url: str) -> bool:
    """Validate that a URL is safe to call (no SSRF to internal networks).

    Blocks:
    - Non-http(s) schemes
    - Private/loopback IPv4 ranges: 10/8, 172.16/12, 192.168/16, 127/8, 169.254/16
    - IPv6 loopback (::1)
    - Hostnames that resolve to private IPs

    Note: This does not fully prevent DNS rebinding (TOCTOU between resolve and
    request). For alpha software this is acceptable. A production deployment should
    use a custom resolver or proxy that pins DNS results.
    """
    import socket

    try:
        parsed = urlparse(url)
    except Exception:
        return False

    if parsed.scheme not in ("http", "https"):
        return False

    hostname = parsed.hostname
    if not hostname:
        return False

    # Check raw IP literals first
    try:
        addr = ipaddress.ip_address(hostname)
        return not _is_private_ip(addr)
    except ValueError:
        pass

    # Resolve hostname and check all resulting IPs
    try:
        results = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        if not results:
            return False
        for family, _, _, _, sockaddr in results:
            addr = ipaddress.ip_address(sockaddr[0])
            if _is_private_ip(addr):
                return False
    except (socket.gaierror, OSError):
        # DNS resolution failed — block the request
        return False

    return True


app = FastAPI(title="Chock", version="1.1.0")


# --- Optional API Key Auth ---

READ_METHODS = {"GET", "HEAD", "OPTIONS"}

class ApiKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not API_KEY:
            return await call_next(request)
        path = request.url.path
        if path == "/" or path.startswith("/static") or path == "/health":
            return await call_next(request)
        if request.method in READ_METHODS:
            return await call_next(request)
        key = request.headers.get("x-api-key") or request.headers.get("authorization", "").removeprefix("Bearer ")
        if not hmac.compare_digest(key, API_KEY):
            return JSONResponse(status_code=401, content={"detail": "Invalid or missing API key"})
        return await call_next(request)

app.add_middleware(ApiKeyMiddleware)


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
            if not is_url_safe(url):
                print(f"[chock] Webhook blocked (SSRF protection): {url}", flush=True)
                continue
            try:
                req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
                urllib.request.urlopen(req, timeout=5)
            except Exception as e:
                print(f"[chock] Webhook failed ({url}): {e}", flush=True)
    threading.Thread(target=_fire, daemon=True).start()


def fire_callback(callback_url: str, request_id: str, result: dict):
    """Fire a callback to the requesting agent when their approval is responded to."""
    payload = json.dumps({
        "request_id": request_id,
        "result": result,
        "timestamp": now_iso()
    }).encode()
    if not is_url_safe(callback_url):
        print(f"[chock] Callback blocked (SSRF protection): {callback_url}", flush=True)
        return
    def _fire():
        try:
            req = urllib.request.Request(callback_url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            print(f"[chock] Callback failed ({callback_url}): {e}", flush=True)
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
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
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
    key: str = Field(max_length=100)
    label: str = Field(max_length=500)
    type: str = "text"  # text, number, boolean, select
    options: list[str] = Field(default=[], max_length=100)  # for select type
    required: bool = False
    description: str = Field(default="", max_length=2000)

class ApprovalCreate(BaseModel):
    title: str = Field(max_length=500)
    plan: str = Field(default="", max_length=10000)
    requester: str = Field(default="", max_length=200)
    context: str = Field(default="", max_length=10000)
    tags: list[str] = Field(default=[], max_length=50)
    priority: str = "normal"
    callback_url: str = Field(default="", max_length=2000)
    conditions_schema: list[ConditionField] = Field(default=[], max_length=50)

    @field_validator("tags")
    @classmethod
    def validate_tag_lengths(cls, v):
        for tag in v:
            if len(tag) > 100:
                raise ValueError("Each tag must be 100 characters or fewer")
        return v

class ApprovalRespond(BaseModel):
    status: str  # approved, denied, conditional
    responder: str = Field(default="", max_length=200)
    comment: str = Field(default="", max_length=10000)
    conditions: dict = Field(default={}, max_length=50)  # filled in for conditional approvals


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
                  tag: Optional[str] = None, limit: int = 200, offset: int = 0):
    """List approval requests with optional filters."""
    limit = max(1, min(limit, 1000))
    offset = max(0, offset)

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
    query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

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
    limit = min(limit, 1000)
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
