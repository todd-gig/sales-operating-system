# Cloud SQL Migration Guide

Migrates the Sales Operating System from ephemeral SQLite on Cloud Run to the
durable `sie-postgres-production` Cloud SQL (Postgres 15) instance in GCP
project `carmen-beach-properties`, region `us-central1`.

---

## Prerequisites

```bash
gcloud auth login
gcloud config set project carmen-beach-properties
```

Confirm the Cloud SQL instance is running:

```bash
gcloud sql instances describe sie-postgres-production --format="value(state)"
# Expected: RUNNABLE
```

---

## Step 1 — Create the application database and user

Connect via Cloud SQL Auth Proxy or Cloud Shell:

```bash
# Option A: Cloud Shell (no proxy needed)
gcloud sql connect sie-postgres-production --user=postgres --database=postgres

# Option B: local proxy (install cloud-sql-proxy first)
cloud-sql-proxy carmen-beach-properties:us-central1:sie-postgres-production &
psql "host=127.0.0.1 port=5432 user=postgres dbname=postgres"
```

Inside the psql session:

```sql
-- Create the database
CREATE DATABASE sales_os;

-- Create a least-privilege app user
CREATE USER sales_os_app WITH PASSWORD 'REPLACE_WITH_STRONG_PASSWORD';

-- Grant access
GRANT CONNECT ON DATABASE sales_os TO sales_os_app;
\c sales_os
GRANT USAGE  ON SCHEMA public TO sales_os_app;
GRANT CREATE ON SCHEMA public TO sales_os_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO sales_os_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO sales_os_app;

-- Confirm
\l sales_os
```

---

## Step 2 — Store DATABASE_URL in Secret Manager

The DATABASE_URL uses the **Unix-socket** format so Cloud Run connects through
the Cloud SQL Auth Proxy sidecar (automatically injected when you add
`--add-cloudsql-instances`).

```
postgresql+asyncpg://sales_os_app:PASSWORD@/sales_os?host=/cloudsql/carmen-beach-properties:us-central1:sie-postgres-production
```

Create the secret:

```bash
echo -n "postgresql+asyncpg://sales_os_app:REPLACE_WITH_STRONG_PASSWORD@/sales_os?host=/cloudsql/carmen-beach-properties:us-central1:sie-postgres-production" \
  | gcloud secrets create sales-os-database-url \
      --data-file=- \
      --replication-policy=user-managed \
      --locations=us-central1

# Verify
gcloud secrets versions access latest --secret=sales-os-database-url
```

Grant the Cloud Run service account access to the secret:

```bash
# Find the Cloud Run service account (defaults to the Compute default SA)
PROJECT_NUMBER=$(gcloud projects describe carmen-beach-properties --format="value(projectNumber)")
SA="$PROJECT_NUMBER-compute@developer.gserviceaccount.com"

gcloud secrets add-iam-policy-binding sales-os-database-url \
  --member="serviceAccount:$SA" \
  --role="roles/secretmanager.secretAccessor"
```

---

## Step 3 — Install Python dependencies

Add the Postgres async driver and Alembic to `requirements.txt`:

```
asyncpg>=0.29.0
aiosqlite>=0.20.0
alembic>=1.13.0
sqlalchemy[asyncio]>=2.0.0
```

Rebuild and push the Docker image (Cloud Build handles this), or locally:

```bash
pip install asyncpg aiosqlite alembic "sqlalchemy[asyncio]"
```

---

## Step 4 — Run the initial Alembic migration

### Option A: from your local machine via the Cloud SQL Auth Proxy

```bash
# Start proxy in background
cloud-sql-proxy carmen-beach-properties:us-central1:sie-postgres-production \
  --port=5432 &

# Set DATABASE_URL pointing to local proxy
export DATABASE_URL="postgresql+asyncpg://sales_os_app:REPLACE_WITH_STRONG_PASSWORD@localhost:5432/sales_os"

# Run migration
cd /path/to/sales-operating-system
alembic upgrade head
```

### Option B: as a Cloud Build step (one-shot migration job)

Add this step to `cloudbuild.yaml` **before** the Cloud Run deploy step:

```yaml
  # ── Run Alembic migrations ─────────────────────────────────────────────────
  - name: 'gcr.io/$PROJECT_ID/sales-operating-system:$COMMIT_SHA'
    entrypoint: python
    args: ['-m', 'alembic', 'upgrade', 'head']
    env:
      - 'DATABASE_URL=$$DATABASE_URL'
    secretEnv: ['DATABASE_URL']
```

And declare the secret at the bottom of `cloudbuild.yaml`:

```yaml
availableSecrets:
  secretManager:
    - versionName: projects/$PROJECT_ID/secrets/sales-os-database-url/versions/latest
      env: DATABASE_URL
```

---

## Step 5 — Update cloudbuild.yaml Cloud Run deploy step

Replace the existing `--set-env-vars` and add Cloud SQL + secret wiring:

```yaml
  - name: 'gcr.io/google.com/cloudsdktool/cloud-sdk'
    entrypoint: gcloud
    args:
      - run
      - deploy
      - sales-operating-system
      - --image=gcr.io/$PROJECT_ID/sales-operating-system:$COMMIT_SHA
      - --region=us-central1
      - --platform=managed
      - --port=8003
      - --min-instances=0
      - --max-instances=5
      - --memory=512Mi
      - --cpu=1
      - --timeout=60
      - --no-allow-unauthenticated
      # Cloud SQL Auth Proxy sidecar — injects the Unix socket
      - --add-cloudsql-instances=carmen-beach-properties:us-central1:sie-postgres-production
      # Inject DATABASE_URL from Secret Manager
      - --set-secrets=DATABASE_URL=sales-os-database-url:latest
      # Other env vars (DATABASE_PATH is no longer needed)
      - --set-env-vars=CORS_ORIGINS=https://gigaton-ui.web.app
      # Optional secrets
      # - --set-secrets=ANTHROPIC_API_KEY=anthropic-api-key:latest
```

---

## Step 6 — Grant Cloud Run the Cloud SQL Client role

```bash
PROJECT_NUMBER=$(gcloud projects describe carmen-beach-properties --format="value(projectNumber)")
SA="$PROJECT_NUMBER-compute@developer.gserviceaccount.com"

gcloud projects add-iam-policy-binding carmen-beach-properties \
  --member="serviceAccount:$SA" \
  --role="roles/cloudsql.client"
```

---

## Step 7 — Update app/main.py startup

The lifespan in `app/main.py` currently calls `init_global_db(db_path)` (SQLite
path-based).  After the migration that function still works for local dev, but
Cloud Run will use `app/database.py` (the new async SQLAlchemy engine).

**Minimum change required in `app/main.py`:**

```python
from app.database import DATABASE_BACKEND

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    if DATABASE_BACKEND == "sqlite":
        # Legacy local-dev path
        db_path = os.environ.get("DATABASE_PATH", "sales_os.db")
        db = init_global_db(db_path)
        runtime = AgentRuntime(db)
        runtime.seed_builtin_templates()
        print(f"[SalesOS] SQLite initialised at '{db_path}'")
    else:
        # Postgres: tables already exist (Alembic ran in Cloud Build).
        # Seed step is handled as a one-off Cloud Build job or migration.
        db = init_global_db(":memory:")   # no-op sentinel; routes use async session
        runtime = AgentRuntime(db)
        print("[SalesOS] Postgres backend ready (Cloud SQL)")
    yield
    db.close()
```

A full async refactor of the route handlers to use `AsyncSession` from
`app.database.get_async_session` is recommended as a follow-up, tracked in
`BETA_2_GAP_LIST` as Mn-06.

---

## Connection string reference

| Context | DATABASE_URL format |
|---|---|
| Cloud Run (production) | `postgresql+asyncpg://user:pass@/dbname?host=/cloudsql/PROJECT:REGION:INSTANCE` |
| Local dev (proxy on 5432) | `postgresql+asyncpg://user:pass@localhost:5432/dbname` |
| Local dev (SQLite fallback) | *(unset DATABASE_URL — auto-falls back)* |
| Alembic offline SQL dump | set `sqlalchemy.url` in `alembic.ini` or pass `--url` flag |

The exact production URL for this project:

```
postgresql+asyncpg://sales_os_app:PASSWORD@/sales_os?host=/cloudsql/carmen-beach-properties:us-central1:sie-postgres-production
```

---

## Verifying the migration

```bash
# Check alembic_version table
psql "$DATABASE_URL" -c "SELECT version_num FROM alembic_version;"
# Expected: 001

# Check tables exist
psql "$DATABASE_URL" -c "\dt"
```

---

## Rolling back

```bash
alembic downgrade base
# Drops ALL tables; only use in a non-production environment.
```

---

## Model changes noted (action required before seeding)

The existing `database.py` uses TEXT primary keys containing Excel IDs like
`MC-001`.  The Alembic migration generates UUIDs for all PKs.  Two changes are
required when re-seeding from `scripts/seed_from_xlsx.py`:

1. **Primary keys** — the seeder currently passes the Excel ID string (e.g.
   `MC-001`) as the `id` field.  With UUID PKs the seeder must either:
   - generate a UUID per row and store the original Excel ID in
     `source_reference` (already a column in `product_catalog`), OR
   - add an `excel_id TEXT UNIQUE` column (add in a `002_add_excel_id.py`
     migration) and keep the business key separate from the surrogate PK.

2. **JSON columns** — `tool_policy_json`, `output_schema_json`, `config_json`,
   `payload_json`, `result_json`, `input_json`, `output_json`, `metadata_json`
   are stored as TEXT strings in the SQLite version.  The Postgres schema
   defines them as JSONB.  The seeder and any route that writes these columns
   must pass a dict (or `json.loads(value)`) rather than a JSON string.

3. **Boolean columns** — `is_active` and `required` are INTEGER (0/1) in
   SQLite.  Postgres uses `BOOLEAN` (`true`/`false`).  Any code that writes
   `1` or `0` must be updated to `True` / `False`.

4. **Timestamps** — all `*_at` fields are TEXT ISO-8601 strings in the current
   codebase.  Postgres stores them as `TIMESTAMP WITH TIME ZONE`.  The
   SQLAlchemy layer handles Python `datetime` objects automatically; string
   values passed directly via raw SQL must be cast or converted.
