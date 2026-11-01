"""Small app for the AWS container hosting lab.

All the code lives in this one file on purpose: metadata lookup, notes store,
JSON logging, SIGTERM logging and the crash switch. The browser UI is index.html.
"""

import json
import os
import signal
import socket
import sys
import time
import urllib.request
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Path as PathParam, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field


# ---------- logging ----------

def log(**fields):
    """Write one JSON line to stdout (CloudWatch Logs picks it up via awslogs)."""
    fields = {"ts": datetime.now(timezone.utc).isoformat(), **fields}
    print(json.dumps(fields, default=str), file=sys.stdout, flush=True)


# ---------- ECS task metadata ----------

# Bake the version into the image (docker build --build-arg APP_VERSION=...) or set it in
# the task definition, so you can tell old and new tasks apart during a rolling deploy.
APP_VERSION = os.environ.get("APP_VERSION", "dev")
HOSTNAME = socket.gethostname()
STARTED = time.time()

_metadata_cache = None


def _fetch_metadata():
    """Return the task metadata v4 document, or None when not on ECS."""
    base = os.environ.get("ECS_CONTAINER_METADATA_URI_V4")
    if not base:
        return None
    with urllib.request.urlopen(f"{base}/task", timeout=2) as resp:
        return json.load(resp)


def task_info():
    """Task ID, AZ and launch type. Cached after the first successful lookup."""
    global _metadata_cache
    if _metadata_cache is not None:
        return _metadata_cache
    try:
        doc = _fetch_metadata()
    except Exception as exc:  # endpoint unreachable: report it, don't crash, retry next time
        return {"task_id": "unknown", "availability_zone": "unknown",
                "launch_type": "unknown", "metadata_error": str(exc)}
    if doc is None:
        return {"task_id": "local", "availability_zone": "local", "launch_type": "local"}
    _metadata_cache = {
        "task_id": doc.get("TaskARN", "unknown").rsplit("/", 1)[-1],
        "availability_zone": doc.get("AvailabilityZone", "unknown"),
        "launch_type": doc.get("LaunchType", "unknown"),
        "cluster": doc.get("Cluster", "unknown").rsplit("/", 1)[-1],
    }
    return _metadata_cache


# ---------- notes store ----------

class MemoryStore:
    backend = "memory"

    def __init__(self):
        self._notes = []

    def add(self, note):
        self._notes.append(note)

    def list(self):
        return list(self._notes)

    def delete(self, note_id):
        before = len(self._notes)
        self._notes = [n for n in self._notes if n["id"] != note_id]
        return len(self._notes) < before


class S3Store:
    """One JSON object per note under notes/. Keys sort by creation time."""

    backend = "s3"
    prefix = "notes/"

    def __init__(self, bucket):
        import boto3  # imported here so local runs without AWS don't touch it
        self.bucket = bucket
        self.s3 = boto3.client("s3")

    def add(self, note):
        key = f"{self.prefix}{note['created_ms']:013d}-{note['id']}.json"
        self.s3.put_object(Bucket=self.bucket, Key=key,
                           Body=json.dumps(note).encode(), ContentType="application/json")

    def list(self, limit=50):
        resp = self.s3.list_objects_v2(Bucket=self.bucket, Prefix=self.prefix, MaxKeys=1000)
        keys = sorted(obj["Key"] for obj in resp.get("Contents", []))[-limit:]
        return [json.loads(self.s3.get_object(Bucket=self.bucket, Key=k)["Body"].read())
                for k in keys]

    def delete(self, note_id):
        """Needs s3:DeleteObject on the task role, on top of Get/Put/List."""
        suffix = f"-{note_id}.json"
        for page in self.s3.get_paginator("list_objects_v2").paginate(
                Bucket=self.bucket, Prefix=self.prefix):
            for obj in page.get("Contents", []):
                if obj["Key"].endswith(suffix):
                    self.s3.delete_object(Bucket=self.bucket, Key=obj["Key"])
                    return True
        return False


def make_store():
    bucket = os.environ.get("NOTES_BUCKET")
    return S3Store(bucket) if bucket else MemoryStore()


def new_note(text, source="api"):
    now = time.time()
    return {
        "id": uuid.uuid4().hex[:12],
        "text": text,
        "created_ms": int(now * 1000),
        "created_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "task_id": task_info()["task_id"],
        "source": source,
    }


store = make_store()


# ---------- app ----------

def _install_sigterm_logger():
    """Log SIGTERM, then hand over to uvicorn's own handler so it shuts down cleanly."""
    previous = signal.getsignal(signal.SIGTERM)

    def handler(signum, frame):
        log(event="sigterm", **task_info())
        if callable(previous):
            previous(signum, frame)
        else:
            sys.exit(0)

    try:
        signal.signal(signal.SIGTERM, handler)
    except ValueError:
        pass  # not in the main thread (e.g. under the test client)


@asynccontextmanager
async def lifespan(_app):
    if os.environ.get("CRASH_ON_START", "").lower() == "true":
        log(event="crash_on_start", msg="CRASH_ON_START=true, exiting")
        sys.exit(1)
    _install_sigterm_logger()
    log(event="startup", notes_backend=store.backend, version=APP_VERSION, **task_info())
    yield
    log(event="shutdown", **task_info())


app = FastAPI(title="ecs-lab", lifespan=lifespan)


@app.middleware("http")
async def request_log(request: Request, call_next):
    start = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        log(event="request", method=request.method, path=request.url.path, status=status,
            duration_ms=round((time.perf_counter() - start) * 1000, 1),
            client=request.headers.get("x-forwarded-for",
                                       request.client.host if request.client else None),
            task_id=task_info()["task_id"])


PAGE = (Path(__file__).parent / "index.html").read_text(encoding="utf-8")


@app.get("/")
def whoami(request: Request):
    # Browsers get the notes page; curl, tests and load balancers keep getting JSON.
    if "text/html" in request.headers.get("accept", ""):
        return HTMLResponse(PAGE)
    return {**task_info(), "notes_backend": store.backend, "version": APP_VERSION,
            "hostname": HOSTNAME, "uptime_s": round(time.time() - STARTED, 1)}


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


class NoteIn(BaseModel):
    text: str = Field(min_length=1, max_length=280)


@app.post("/notes", status_code=201)
def add_note(body: NoteIn):
    note = new_note(body.text)
    try:
        store.add(note)
    except Exception as exc:
        log(event="notes_error", op="add", error=str(exc))
        raise HTTPException(status_code=502, detail=f"notes store error: {exc}")
    return note


@app.get("/notes")
def list_notes():
    try:
        # task_id says which task answered: in memory mode each task has its own list.
        return {"backend": store.backend, "task_id": task_info()["task_id"], "notes": store.list()}
    except Exception as exc:
        log(event="notes_error", op="list", error=str(exc))
        raise HTTPException(status_code=502, detail=f"notes store error: {exc}")


@app.delete("/notes/{note_id}", status_code=204)
def delete_note(note_id: str = PathParam(pattern=r"^[0-9a-f]{12}$")):
    try:
        deleted = store.delete(note_id)
    except Exception as exc:
        log(event="notes_error", op="delete", error=str(exc))
        raise HTTPException(status_code=502, detail=f"notes store error: {exc}")
    if not deleted:
        raise HTTPException(status_code=404, detail="note not found")
