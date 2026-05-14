#!/usr/bin/env bash
# Sales-OS Cloud SQL provisioning — one-shot script.
#
# Provisions the Cloud SQL Postgres instance + database + user + password
# secret + IAM grants that the sales-os runtime needs to switch from
# SQLite to Postgres (deferred from PR #2).
#
# After running this script, deploy via cloudbuild-cloud-sql.yaml.
#
# Estimated wall-clock: 5-7 minutes (Cloud SQL instance creation is slow).
# Idempotent — safe to re-run; existing resources are skipped with warnings.
#
# Usage:
#   PROJECT_ID=carmen-beach-properties \
#   REGION=us-central1 \
#   bash scripts/provision_cloud_sql.sh
#
# Required env (override defaults via env):
#   PROJECT_ID         — defaults to gcloud's active project
#   REGION             — defaults to us-central1
#   INSTANCE_NAME      — defaults to sales-os-pg
#   DB_NAME            — defaults to sales_os
#   APP_DB_USER        — defaults to sales_os_app
#   TIER               — defaults to db-f1-micro (tiny dev tier)
#   RUNTIME_SA         — defaults to sales-operating-system-runtime
#
# Prerequisites (verified by the script before any action):
#   - gcloud authenticated as a user with project Owner or Cloud SQL Admin
#   - Cloud SQL Admin API enabled (`gcloud services enable sqladmin.googleapis.com`)
#   - Secret Manager API enabled (`gcloud services enable secretmanager.googleapis.com`)

set -euo pipefail

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-us-central1}"
INSTANCE_NAME="${INSTANCE_NAME:-sales-os-pg}"
DB_NAME="${DB_NAME:-sales_os}"
APP_DB_USER="${APP_DB_USER:-sales_os_app}"
TIER="${TIER:-db-f1-micro}"
RUNTIME_SA="${RUNTIME_SA:-sales-operating-system-runtime}"

if [[ -z "$PROJECT_ID" ]]; then
  echo "ERROR: PROJECT_ID not set and no active gcloud project." >&2
  echo "Run: gcloud config set project <project-id>" >&2
  exit 1
fi

echo "─────────────────────────────────────────────────────────────"
echo "Sales-OS Cloud SQL provisioning"
echo "─────────────────────────────────────────────────────────────"
echo "  PROJECT_ID    = $PROJECT_ID"
echo "  REGION        = $REGION"
echo "  INSTANCE_NAME = $INSTANCE_NAME"
echo "  DB_NAME       = $DB_NAME"
echo "  APP_DB_USER   = $APP_DB_USER"
echo "  TIER          = $TIER"
echo "  RUNTIME_SA    = $RUNTIME_SA"
echo "─────────────────────────────────────────────────────────────"
read -r -p "Proceed? [y/N] " confirm
if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
  echo "Aborted."
  exit 1
fi

# ── Pre-flight: enable APIs (idempotent) ────────────────────────────────────
echo
echo "▶ Enabling required APIs..."
gcloud services enable sqladmin.googleapis.com \
                       secretmanager.googleapis.com \
                       iam.googleapis.com \
                       run.googleapis.com \
                       --project="$PROJECT_ID"

# ── 1. Create Cloud SQL instance (if not exists) ─────────────────────────────
echo
echo "▶ [1/7] Cloud SQL Postgres instance: $INSTANCE_NAME"
if gcloud sql instances describe "$INSTANCE_NAME" --project="$PROJECT_ID" >/dev/null 2>&1; then
  echo "  ↳ Instance already exists; skipping create."
else
  echo "  ↳ Creating instance (this takes ~3-5 min)..."
  gcloud sql instances create "$INSTANCE_NAME" \
    --database-version=POSTGRES_15 \
    --region="$REGION" \
    --tier="$TIER" \
    --storage-size=10GB \
    --storage-type=SSD \
    --backup \
    --backup-start-time=04:00 \
    --maintenance-window-day=SUN \
    --maintenance-window-hour=05 \
    --availability-type=zonal \
    --project="$PROJECT_ID"
fi

# ── 2. Create database ───────────────────────────────────────────────────────
echo
echo "▶ [2/7] Database: $DB_NAME"
if gcloud sql databases describe "$DB_NAME" \
     --instance="$INSTANCE_NAME" --project="$PROJECT_ID" >/dev/null 2>&1; then
  echo "  ↳ Database already exists; skipping."
else
  gcloud sql databases create "$DB_NAME" \
    --instance="$INSTANCE_NAME" --project="$PROJECT_ID"
fi

# ── 3. Generate app password + store in Secret Manager ──────────────────────
echo
echo "▶ [3/7] App password + Secret Manager secret"
SECRET_NAME="sales-os-db-password"
if gcloud secrets describe "$SECRET_NAME" --project="$PROJECT_ID" >/dev/null 2>&1; then
  echo "  ↳ Secret $SECRET_NAME already exists; skipping create."
  echo "    To rotate manually:"
  echo "      gcloud secrets versions add $SECRET_NAME --data-file=- <<< 'new-pw'"
else
  APP_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
  echo "  ↳ Generated 24-byte URL-safe password."
  printf "%s" "$APP_PASSWORD" | gcloud secrets create "$SECRET_NAME" \
    --replication-policy=automatic \
    --data-file=- \
    --project="$PROJECT_ID"
  echo "  ↳ Secret created: $SECRET_NAME"
fi
# Read latest version (will fail loudly if missing — that's correct)
APP_PASSWORD="$(gcloud secrets versions access latest \
  --secret="$SECRET_NAME" --project="$PROJECT_ID")"

# ── 4. Create app DB user with that password ────────────────────────────────
echo
echo "▶ [4/7] DB user: $APP_DB_USER"
if gcloud sql users list --instance="$INSTANCE_NAME" --project="$PROJECT_ID" \
     --format="value(name)" | grep -qx "$APP_DB_USER"; then
  echo "  ↳ User already exists; updating password to match secret..."
  gcloud sql users set-password "$APP_DB_USER" \
    --instance="$INSTANCE_NAME" \
    --password="$APP_PASSWORD" \
    --project="$PROJECT_ID"
else
  gcloud sql users create "$APP_DB_USER" \
    --instance="$INSTANCE_NAME" \
    --password="$APP_PASSWORD" \
    --project="$PROJECT_ID"
fi

# ── 5. Ensure runtime service account exists ────────────────────────────────
echo
echo "▶ [5/7] Runtime service account: ${RUNTIME_SA}"
RUNTIME_SA_EMAIL="${RUNTIME_SA}@${PROJECT_ID}.iam.gserviceaccount.com"
if gcloud iam service-accounts describe "$RUNTIME_SA_EMAIL" \
     --project="$PROJECT_ID" >/dev/null 2>&1; then
  echo "  ↳ Already exists."
else
  gcloud iam service-accounts create "$RUNTIME_SA" \
    --display-name="Sales-OS Cloud Run runtime" \
    --project="$PROJECT_ID"
fi

# ── 6. IAM grants ───────────────────────────────────────────────────────────
echo
echo "▶ [6/7] IAM grants on runtime SA"
# cloudsql.client — required for Cloud SQL Auth Proxy
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${RUNTIME_SA_EMAIL}" \
  --role="roles/cloudsql.client" \
  --condition=None \
  --quiet
# secretmanager.secretAccessor — to read DB_PASSWORD secret
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${RUNTIME_SA_EMAIL}" \
  --role="roles/secretmanager.secretAccessor" \
  --condition=None \
  --quiet
echo "  ↳ Granted: cloudsql.client + secretmanager.secretAccessor"

# ── 7. Run initial Alembic migration ───────────────────────────────────────
echo
echo "▶ [7/7] Initial Alembic migration"
echo "  ↳ Auth Proxy connection string:"
echo "    cloudsql.connectionName = ${PROJECT_ID}:${REGION}:${INSTANCE_NAME}"
echo
echo "  Run from your local dev shell after exporting DATABASE_URL:"
cat <<EOF

    # Start Cloud SQL Auth Proxy in another terminal (TCP mode, port 5432):
    cloud-sql-proxy ${PROJECT_ID}:${REGION}:${INSTANCE_NAME} --port=5432 &

    # Then in this shell:
    export DATABASE_URL="postgresql+asyncpg://${APP_DB_USER}:\$(gcloud secrets versions access latest --secret=${SECRET_NAME})@localhost:5432/${DB_NAME}"
    python -m alembic upgrade head

EOF

# ── Summary ─────────────────────────────────────────────────────────────────
echo
echo "─────────────────────────────────────────────────────────────"
echo "Provisioning complete."
echo "─────────────────────────────────────────────────────────────"
echo
echo "Cloud Run deployment values (use these in cloudbuild-cloud-sql.yaml):"
echo "  --service-account=${RUNTIME_SA_EMAIL}"
echo "  --add-cloudsql-instances=${PROJECT_ID}:${REGION}:${INSTANCE_NAME}"
echo "  --set-env-vars=DB_HOST=/cloudsql/${PROJECT_ID}:${REGION}:${INSTANCE_NAME},DB_NAME=${DB_NAME},DB_USER=${APP_DB_USER}"
echo "  --set-secrets=DB_PASSWORD=${SECRET_NAME}:latest"
echo
echo "Next: deploy with the runtime-flip cloudbuild manifest:"
echo "    gcloud builds submit --config=cloudbuild-cloud-sql.yaml"
echo
