"""Small, auditable example of tenant-safe task writes and event delivery."""

import hashlib
import hmac
import json
import os
import sqlite3
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def db_path() -> str:
    return os.environ.get("TASK_DB", "tasks.sqlite3")


@contextmanager
def connection():
    db = sqlite3.connect(db_path(), timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=10000")
    try:
        yield db
    finally:
        db.close()


def init_db():
    with connection() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
              id TEXT PRIMARY KEY, tenant TEXT NOT NULL, name TEXT NOT NULL,
              payload TEXT NOT NULL, status TEXT NOT NULL, version INTEGER NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS jobs_tenant_created ON jobs(tenant, created_at, id);
            CREATE TABLE IF NOT EXISTS requests (
              tenant TEXT NOT NULL, key TEXT NOT NULL, digest TEXT NOT NULL,
              job_id TEXT NOT NULL REFERENCES jobs(id),
              PRIMARY KEY(tenant, key)
            );
            CREATE TABLE IF NOT EXISTS outbox (
              id INTEGER PRIMARY KEY AUTOINCREMENT, tenant TEXT NOT NULL,
              job_id TEXT NOT NULL REFERENCES jobs(id), event TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_tenant_id ON outbox(tenant, id);
        """)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Tenant Task API", version="0.1.0", lifespan=lifespan)


def tenant_from_key(x_api_key: Annotated[str, Header(min_length=1)]):
    """Resolve an authenticated tenant; never trust a caller-supplied tenant ID."""
    try:
        keys = json.loads(os.environ["TENANT_API_KEYS"])
    except (KeyError, ValueError, TypeError):
        raise HTTPException(503, "Server API keys are not configured") from None
    if not isinstance(keys, dict):
        raise HTTPException(503, "Server API keys are not configured")
    for tenant, key in keys.items():
        if isinstance(key, str) and hmac.compare_digest(x_api_key, key):
            return tenant
    raise HTTPException(401, "Invalid API key")


Tenant = Annotated[str, Depends(tenant_from_key)]


class NewJob(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    payload: dict = Field(default_factory=dict)


class Transition(BaseModel):
    status: Literal["running", "done", "failed"]
    expected_version: int = Field(ge=1)


def job_or_404(db, tenant, job_id):
    row = db.execute("SELECT * FROM jobs WHERE tenant=? AND id=?", (tenant, job_id)).fetchone()
    if row is None:
        raise HTTPException(404, "Job not found")
    return dict(row)


def job_view(row):
    return {**row, "payload": json.loads(row["payload"])}


@app.post("/jobs", status_code=201)
def create_job(body: NewJob, tenant: Tenant, idempotency_key: Annotated[str, Header(min_length=1, max_length=128)]):
    canonical = body.model_dump_json()
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        prior = db.execute("SELECT digest, job_id FROM requests WHERE tenant=? AND key=?",
                           (tenant, idempotency_key)).fetchone()
        if prior:
            if prior["digest"] != digest:
                raise HTTPException(409, "Idempotency key reused with a different request")
            return job_view(job_or_404(db, tenant, prior["job_id"]))
        job_id, now = uuid.uuid4().hex, utc_now()
        db.execute("INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (job_id, tenant, body.name, json.dumps(body.payload, sort_keys=True), "pending", 1, now))
        db.execute("INSERT INTO requests VALUES (?, ?, ?, ?)",
                   (tenant, idempotency_key, digest, job_id))
        db.execute("INSERT INTO outbox(tenant, job_id, event, created_at) VALUES (?, ?, ?, ?)",
                   (tenant, job_id, "created", now))
        db.commit()
        return job_view(job_or_404(db, tenant, job_id))


@app.get("/jobs")
def list_jobs(tenant: Tenant, limit: Annotated[int, Query(ge=1, le=100)] = 20):
    with connection() as db:
        rows = db.execute("SELECT * FROM jobs WHERE tenant=? ORDER BY created_at DESC, id DESC LIMIT ?",
                          (tenant, limit)).fetchall()
        return [job_view(dict(row)) for row in rows]


@app.get("/jobs/{job_id}")
def get_job(job_id: str, tenant: Tenant):
    with connection() as db:
        return job_view(job_or_404(db, tenant, job_id))


@app.patch("/jobs/{job_id}")
def transition_job(job_id: str, body: Transition, tenant: Tenant):
    allowed = {"pending": {"running"}, "running": {"done", "failed"}}
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = job_or_404(db, tenant, job_id)
        if row["version"] != body.expected_version:
            raise HTTPException(409, "Version conflict")
        if body.status not in allowed.get(row["status"], set()):
            raise HTTPException(409, "Invalid status transition")
        db.execute("UPDATE jobs SET status=?, version=version+1 WHERE tenant=? AND id=?",
                   (body.status, tenant, job_id))
        db.execute("INSERT INTO outbox(tenant, job_id, event, created_at) VALUES (?, ?, ?, ?)",
                   (tenant, job_id, body.status, utc_now()))
        db.commit()
        return job_view(job_or_404(db, tenant, job_id))


@app.get("/events")
def list_events(tenant: Tenant, after: Annotated[int, Query(ge=0)] = 0,
                limit: Annotated[int, Query(ge=1, le=100)] = 20):
    """Poll the durable outbox by cursor; a consumer can safely resume after its last ID."""
    with connection() as db:
        rows = db.execute("SELECT id, job_id, event, created_at FROM outbox "
                          "WHERE tenant=? AND id>? ORDER BY id LIMIT ?", (tenant, after, limit)).fetchall()
        return [dict(row) for row in rows]
