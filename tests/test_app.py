import json

import pytest
from fastapi.testclient import TestClient

from app import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_DB", str(tmp_path / "tasks.db"))
    monkeypatch.setenv("TENANT_API_KEYS", json.dumps({"alpha": "alpha-secret", "beta": "beta-secret"}))
    with TestClient(app) as client:
        yield client


def headers(key="alpha-secret", request="request-1"):
    return {"X-API-Key": key, "Idempotency-Key": request}


def test_tenant_isolation_and_authentication(client):
    created = client.post("/jobs", headers=headers(), json={"name": "report", "payload": {"rows": 5}})
    assert created.status_code == 201
    job_id = created.json()["id"]
    assert client.get(f"/jobs/{job_id}", headers=headers("beta-secret")).status_code == 404
    assert client.patch(f"/jobs/{job_id}", headers=headers("beta-secret"),
                        json={"status": "running", "expected_version": 1}).status_code == 404
    assert client.get("/jobs", headers=headers("beta-secret")).json() == []
    assert client.get("/events", headers=headers("beta-secret")).json() == []
    assert client.get("/jobs", headers=headers("wrong-key")).status_code == 401
    # The same idempotency key is independent across tenants.
    assert client.post("/jobs", headers=headers("beta-secret"),
                       json={"name": "other"}).json()["id"] != job_id


def test_idempotency_is_atomic_and_detects_conflicts(client):
    first = client.post("/jobs", headers=headers(), json={"name": "report", "payload": {"a": 1}})
    replay = client.post("/jobs", headers=headers(), json={"name": "report", "payload": {"a": 1}})
    conflict = client.post("/jobs", headers=headers(), json={"name": "different"})
    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json()
    assert conflict.status_code == 409
    assert len(client.get("/jobs", headers=headers()).json()) == 1
    assert [event["event"] for event in client.get("/events", headers=headers()).json()] == ["created"]


def test_versioned_transitions_and_outbox_cursor(client):
    job = client.post("/jobs", headers=headers(), json={"name": "report"}).json()
    url = f"/jobs/{job['id']}"
    invalid = client.patch(url, headers=headers(), json={"status": "done", "expected_version": 1})
    assert invalid.status_code == 409
    running = client.patch(url, headers=headers(), json={"status": "running", "expected_version": 1})
    assert running.json()["version"] == 2
    stale = client.patch(url, headers=headers(), json={"status": "done", "expected_version": 1})
    assert stale.status_code == 409
    done = client.patch(url, headers=headers(), json={"status": "done", "expected_version": 2})
    assert done.json()["status"] == "done"
    assert client.patch(url, headers=headers(), json={"status": "failed", "expected_version": 3}).status_code == 409
    events = client.get("/events", headers=headers()).json()
    assert [event["event"] for event in events] == ["created", "running", "done"]
    assert client.get(f"/events?after={events[0]['id']}&limit=1", headers=headers()).json() == [events[1]]


def test_input_bounds(client):
    assert client.post("/jobs", headers=headers(), json={"name": ""}).status_code == 422
    assert client.post("/jobs", headers={"X-API-Key": "alpha-secret"},
                       json={"name": "missing-key"}).status_code == 422
    assert client.get("/events?limit=1000", headers=headers()).status_code == 422
