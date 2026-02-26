# Inventory Locking Simulator

This project demonstrates pessimistic and optimistic locking strategies for an inventory system using FastAPI and PostgreSQL.

Key files:
- `docker-compose.yml` - orchestrates the `app` and `db` services.
- `Dockerfile` - builds the FastAPI app image.
- `seeds/init.sql` - creates tables and initial products.
- `app/main.py` - FastAPI application implementing both locking strategies.
- `concurrent-test.sh` - script to simulate concurrent requests.
- `monitor-locks.sh` - script to watch PostgreSQL locks.
- `.env.example` - environment variables.

Quick start

1. Copy `.env.example` to `.env` and adjust if needed.
2. Build and start services:

```bash
docker-compose up --build
```

3. Run a concurrency test (once app is healthy):

```bash
./concurrent-test.sh pessimistic
./concurrent-test.sh optimistic
```

4. Check stats:

```bash
curl http://localhost:8080/api/orders/stats
```

Notes

- The pessimistic endpoint uses `SELECT ... FOR UPDATE` and a configurable `lock_timeout`.
- The optimistic endpoint uses a `version` column and retries with exponential backoff.
- All database queries and HTTP requests are logged with structured JSON; DB ops include timing metrics to help analyze latency.
