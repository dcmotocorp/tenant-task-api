# Tenant Task API

A runnable FastAPI reference project for tenant-scoped jobs. It demonstrates API key to tenant resolution, atomic idempotent creation, guarded state transitions, and a durable event outbox. The project uses SQLite so reviewers can run it without cloud credentials.

## Run locally

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[test]'
export TENANT_API_KEYS='{"alpha":"local-alpha-secret","beta":"local-beta-secret"}'
uvicorn app:app --reload
```

On Windows PowerShell, set the variable with `$env:TENANT_API_KEYS='{"alpha":"local-alpha-secret"}'`. Browse `/docs` for the interactive OpenAPI specification. The example keys are **local placeholders**; provide random keys through your deployment secrets manager.

```bash
curl -X POST http://localhost:8000/jobs \
  -H 'X-API-Key: local-alpha-secret' -H 'Idempotency-Key: demo-001' \
  -H 'Content-Type: application/json' \
  -d '{"name":"daily-report","payload":{"date":"2026-11-23"}}'
curl http://localhost:8000/events -H 'X-API-Key: local-alpha-secret'
pytest -q
```

## Design choices

- Every database read and write includes the server-resolved tenant. A caller cannot choose its own tenant identifier. Cross-tenant reads and updates return 404.
- A `(tenant, idempotency key)` unique constraint, request digest, and `BEGIN IMMEDIATE` transaction prevent duplicate jobs during concurrent retries. Reusing a key with different input returns 409.
- A job starts `pending`, may move to `running`, and then `done` or `failed`. `expected_version` rejects stale updates.
- Each successful write inserts an outbox event in the **same transaction**. `GET /events?after=<id>` supports cursor polling. A real consumer should persist its cursor and handle at-least-once delivery.
- SQLite WAL enables local concurrent readers. This is a reference service, not a benchmark or a claim of production scale; for distributed deployment, use PostgreSQL, managed secret storage, rate limits, and a dedicated outbox worker.

## Docker

```bash
docker build -t tenant-task-api .
docker run --rm -p 8000:8000 -e TENANT_API_KEYS='{"alpha":"replace-with-long-random-key"}' \
  -v tenant-tasks:/data tenant-task-api
```

The persistent volume holds the SQLite database; the image runs as a non-root user. CI runs the HTTP-level tests on pushes and pull requests.
