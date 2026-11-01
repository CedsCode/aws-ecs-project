import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("ECS_CONTAINER_METADATA_URI_V4", raising=False)
    monkeypatch.delenv("CRASH_ON_START", raising=False)
    monkeypatch.setattr(main, "store", main.MemoryStore())
    monkeypatch.setattr(main, "_metadata_cache", None)
    with TestClient(main.app) as c:
        yield c


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_root_is_local_off_ecs(client):
    body = client.get("/").json()
    assert body["task_id"] == "local"
    assert body["launch_type"] == "local"
    assert body["notes_backend"] == "memory"
    assert body["version"] == main.APP_VERSION
    assert body["hostname"]
    assert body["uptime_s"] >= 0


def test_root_serves_page_to_browsers(client):
    r = client.get("/", headers={"Accept": "text/html,application/xhtml+xml"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "<title>ecs-lab</title>" in r.text


def test_root_reads_task_metadata(client, monkeypatch):
    monkeypatch.setattr(main, "_fetch_metadata", lambda: {
        "TaskARN": "arn:aws:ecs:ap-southeast-1:123456789012:task/lab/abc123",
        "AvailabilityZone": "ap-southeast-1a",
        "LaunchType": "FARGATE",
        "Cluster": "arn:aws:ecs:ap-southeast-1:123456789012:cluster/lab",
    })
    body = client.get("/").json()
    assert body["task_id"] == "abc123"
    assert body["availability_zone"] == "ap-southeast-1a"
    assert body["launch_type"] == "FARGATE"


def test_notes_round_trip_in_memory(client):
    assert client.post("/notes", json={"text": "hello"}).status_code == 201
    body = client.get("/notes").json()
    assert body["backend"] == "memory"
    assert body["task_id"] == "local"
    assert [n["text"] for n in body["notes"]] == ["hello"]


def test_delete_note(client):
    note_id = client.post("/notes", json={"text": "bye"}).json()["id"]
    assert client.delete(f"/notes/{note_id}").status_code == 204
    assert client.get("/notes").json()["notes"] == []
    assert client.delete(f"/notes/{note_id}").status_code == 404


def test_delete_rejects_bad_id(client):
    assert client.delete("/notes/not-an-id").status_code == 422


def test_empty_note_rejected(client):
    assert client.post("/notes", json={"text": ""}).status_code == 422


def test_crash_on_start_exits_nonzero():
    """Start the real server the way the container does; it must exit, not serve."""
    env = {**os.environ, "CRASH_ON_START": "true"}
    env.pop("NOTES_BUCKET", None)
    proc = subprocess.run(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", "8765"],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode != 0
    assert "crash_on_start" in proc.stdout
