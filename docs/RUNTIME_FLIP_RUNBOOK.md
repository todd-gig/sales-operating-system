# Sales-OS Runtime Flip Runbook — SQLite → Cloud SQL Postgres

> Status: ready to execute. Estimated wall-clock: **15-20 minutes** (5 min provisioning + 5 min migration + 5 min deploy + verify).
>
> Reverses cleanly: keep the original `cloudbuild.yaml` untouched; rollback = `gcloud builds submit --config=cloudbuild.yaml`.

## What this flips

Live service today: `sales-operating-system` on Cloud Run, SQLite at `/data/sales_os.db` (ephemeral filesystem; data lost on every Cloud Run revision restart — that's the gap this closes).

After the flip: same service, same image build pipeline, but `DATABASE_URL` resolves to Cloud SQL Postgres via the Cloud SQL Auth Proxy mounted at `/cloudsql/<instance>`. Connection pooled (5+2), TLS-encrypted, persistent.

## Prereqs (one-time per project)

1. **Project Owner or Cloud SQL Admin** on the target GCP project (`carmen-beach-properties` for prod).
2. **gcloud authenticated**: `gcloud auth login && gcloud config set project carmen-beach-properties`
3. **Billing** active on the project (Cloud SQL is a billed resource — `db-f1-micro` is ~$10/month).
4. **Cloud SQL Admin API** and **Secret Manager API** enabled (the provisioning script enables these idempotently).

## Step 1 — Provision (5-7 min)

```bash
cd /Users/admin/Documents/GitHub/sales-operating-system

# Inspect the script first if you want
less scripts/provision_cloud_sql.sh

# Run with the project of your choice
PROJECT_ID=carmen-beach-properties \
REGION=us-central1 \
bash scripts/provision_cloud_sql.sh
```

Creates idempotently:
- Cloud SQL instance `sales-os-pg` (POSTGRES_15, db-f1-micro, 10GB SSD, daily backup at 04:00, weekly maintenance window Sun 05:00)
- Database `sales_os`
- App DB user `sales_os_app` with a freshly-generated 24-byte URL-safe password
- Secret Manager secret `sales-os-db-password` holding that password
- Runtime SA `sales-operating-system-runtime@<project>.iam.gserviceaccount.com`
- IAM grants: `roles/cloudsql.client` + `roles/secretmanager.secretAccessor` on the runtime SA

Re-running is safe; existing resources are skipped with warnings.

## Step 2 — Migrate schema (3-5 min)

Open a second terminal for the Cloud SQL Auth Proxy:

```bash
# Install once: `gcloud components install cloud-sql-proxy` or download from
# https://cloud.google.com/sql/docs/postgres/sql-proxy
cloud-sql-proxy carmen-beach-properties:us-central1:sales-os-pg --port=5432
```

Back in the original terminal:

```bash
# Read the password from Secret Manager
APP_PW="$(gcloud secrets versions access latest --secret=sales-os-db-password)"

# Compose DATABASE_URL for local Alembic invocation
export DATABASE_URL="postgresql+asyncpg://sales_os_app:${APP_PW}@localhost:5432/sales_os"

# Apply migrations (creates all tables from alembic/versions/001_initial_schema.py)
python -m alembic upgrade head
```

Verify:

```bash
# Should report 'head'
python -m alembic current

# Spot-check a table exists
psql -h localhost -U sales_os_app -d sales_os -c "\dt" <<< "$APP_PW"
```

Stop the Auth Proxy (Ctrl-C in the second terminal) once verified.

## Step 3 — Seed catalog data (optional, 2-3 min)

The catalog seeder runs against the Postgres database the same way it ran against SQLite. From the same terminal as Step 2 (with the Auth Proxy still running):

```bash
# Make sure the xlsx is on Desktop or set a custom path
python scripts/seed_from_pg.py \
  --xlsx ~/Desktop/Sales_Operating_System.xlsx
```

Idempotent — the seeder upserts by `excel_id`.

## Step 4 — Flip the runtime (3-5 min deploy)

```bash
# Same image build pipeline as cloudbuild.yaml; just a different deploy step
# that wires DB_HOST + secrets and adds --add-cloudsql-instances.
gcloud builds submit --config=cloudbuild-cloud-sql.yaml
```

What happens:
1. Docker builds the image (same Dockerfile, same code)
2. Pushes to GCR
3. `gcloud run deploy` with the new config — Cloud Run takes ~30-60s to roll out the revision
4. New revision starts; reads `DB_HOST=/cloudsql/<instance>`, `DB_PASSWORD` from Secret Manager, builds the `DATABASE_URL` via `app/database.py` resolution

## Step 5 — Verify (2 min)

```bash
URL="$(gcloud run services describe sales-operating-system --region=us-central1 --format='value(status.url)')"

# Health check
curl -sS "$URL/health" -H "Authorization: Bearer $(gcloud auth print-identity-token)"
# Expect: {"status": "ok", "database": "postgres", ...}

# Check the catalog count (post-seed, should be 214)
curl -sS "$URL/api/v1/catalog/count" -H "Authorization: Bearer $(gcloud auth print-identity-token)"
```

In the Cloud Run logs (Cloud Console → Cloud Run → sales-operating-system → Logs), confirm:
- No `sqlite3` warnings
- Boot log shows `DATABASE_BACKEND: postgres`
- No `OperationalError: connection refused` or `IAM permission` errors

## Step 6 — Decommission the old SQLite path (later, optional)

Once verified for ≥ 24h:
- Remove the `--add-volume-mount=/data` references from `cloudbuild.yaml` (the SQLite path will no longer be reachable; cleaner is better)
- Archive the GCS bucket / NFS mount if one was set up for SQLite persistence

This is best-effort cleanup; the runtime is already on Postgres regardless.

## Rollback

```bash
gcloud builds submit --config=cloudbuild.yaml
```

The original cloudbuild is untouched. One command, ~3 min, reverts the live revision to SQLite-on-ephemeral.

## Failure modes + fixes

| symptom | likely cause | fix |
|---|---|---|
| `403 Not authorized to access resource. Possibly missing permission cloudsql.instances.get` | Runtime SA missing `roles/cloudsql.client` | Re-run `scripts/provision_cloud_sql.sh` — idempotent re-grant |
| `OperationalError: password authentication failed for user "sales_os_app"` | DB password drift between Cloud SQL user and Secret Manager | `gcloud sql users set-password sales_os_app --instance=sales-os-pg --password="$(gcloud secrets versions access latest --secret=sales-os-db-password)"` |
| `connection to server at "/cloudsql/..." failed` | `--add-cloudsql-instances` flag missing from deploy | re-deploy with `cloudbuild-cloud-sql.yaml` (the flag is hardcoded there) |
| `Alembic: table already exists` | Migration ran more than once against same DB | Skip; `alembic current` will report `head`. Idempotent in normal use. |
| `psycopg2 not installed` | Wrong driver — we're async, use `postgresql+asyncpg://...` | The `DATABASE_URL_SCHEME` env var must be `postgresql+asyncpg`; verify in cloudbuild |

## Anchors

- Spec: `CLOUD_SQL_MIGRATION.md` (309-line architecture doc, shipped in PR #2)
- Scaffolding: `alembic/versions/001_initial_schema.py`, `app/database.py` (shipped in PR #2)
- Provisioning script: `scripts/provision_cloud_sql.sh` (this PR)
- Runtime cloudbuild: `cloudbuild-cloud-sql.yaml` (this PR)
- Drift rule precedent: MAJ-014 (`cloudsql_password_drift_between_instance_and_secret`) — provisioning script defends against this by reading from Secret Manager whenever it sets the password
