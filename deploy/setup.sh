#!/usr/bin/env bash
# One-time Google Cloud setup for US Outbound (SPEC 3, 6, 13, 14 phase 0). Safe to re-run:
# every step checks what exists first. Each command is printed before it runs; set
# DRY_RUN=1 to print them without running anything.
#
#   PROJECT=columbus-510209 REGION=europe-west2 deploy/setup.sh
#
# Needs: gcloud and bq, logged in as someone who can manage IAM on the project.
# Creates: the service account us-outbound@, its roles (BigQuery data editor on the
# us_outbound dataset only, BigQuery job user on the project, secret accessor on the seven
# secrets only), the seven secrets with no value, and the Artifact Registry repository.
# Run `us-outbound bq apply --live` before this, so the dataset exists for its grant.
#
# Secret values are never in this repository. Harry adds each one by hand:
#   printf '%s' "$VALUE" | gcloud secrets versions add us-outbound-hubspot-token --data-file=- --project "$PROJECT"
set -euo pipefail

: "${PROJECT:?set PROJECT, the Google Cloud project id}"
: "${REGION:?set REGION, e.g. europe-west2}"
DATASET="us_outbound"
REPO="${REPO:-us-outbound}"
SA_NAME="us-outbound"
SA="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
# The seven secrets of SPEC 13 (names as in us_outbound/context.py SECRET_NAMES, plus the
# Google service account). The jobs run as $SA on Cloud Run and do not read the Google one.
SECRETS=(
  us-outbound-apollo-api-key
  us-outbound-clay-api-key
  us-outbound-instantly-api-key
  us-outbound-hubspot-token
  us-outbound-slack-bot-token
  us-outbound-google-service-account
  us-outbound-claude-api-key
)

run() {
  printf '+ %s\n' "$*" >&2
  if [[ "${DRY_RUN:-0}" != "1" ]]; then "$@"; fi
}
exists() { "$@" >/dev/null 2>&1; }

echo "== APIs"
run gcloud services enable run.googleapis.com cloudscheduler.googleapis.com secretmanager.googleapis.com \
  artifactregistry.googleapis.com bigquery.googleapis.com sheets.googleapis.com iam.googleapis.com \
  --project "$PROJECT"

echo "== Service account $SA"
if exists gcloud iam service-accounts describe "$SA" --project "$PROJECT"; then
  echo "exists"
else
  run gcloud iam service-accounts create "$SA_NAME" --project "$PROJECT" \
    --display-name "US Outbound jobs" --description "Runs the US Outbound Cloud Run Jobs (SPEC 3)"
fi

echo "== BigQuery: job user on the project, data editor on $DATASET only"
run gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$SA" \
  --role roles/bigquery.jobUser --condition None --quiet >/dev/null
if exists bq --project_id "$PROJECT" show --dataset "$PROJECT:$DATASET"; then
  # Dataset-level IAM through DCL; granting a role already held changes nothing.
  run bq --project_id "$PROJECT" query --nouse_legacy_sql \
    "GRANT \`roles/bigquery.dataEditor\` ON SCHEMA \`$PROJECT.$DATASET\` TO \"serviceAccount:$SA\""
else
  echo "dataset $PROJECT:$DATASET does not exist yet: run 'us-outbound bq apply --live', then re-run this script"
fi

echo "== Secrets (created empty; values are added by hand)"
for name in "${SECRETS[@]}"; do
  if exists gcloud secrets describe "$name" --project "$PROJECT"; then
    echo "secret $name exists"
  else
    run gcloud secrets create "$name" --project "$PROJECT" \
      --replication-policy user-managed --locations "$REGION" --labels system=us-outbound
  fi
  run gcloud secrets add-iam-policy-binding "$name" --project "$PROJECT" \
    --member "serviceAccount:$SA" --role roles/secretmanager.secretAccessor --quiet >/dev/null
done

echo "== Artifact Registry repository $REPO"
if exists gcloud artifacts repositories describe "$REPO" --location "$REGION" --project "$PROJECT"; then
  echo "exists"
else
  run gcloud artifacts repositories create "$REPO" --repository-format docker --location "$REGION" \
    --project "$PROJECT" --description "US Outbound job images"
fi

cat <<EOF

Done. Next (docs/phase0-runbook.md):
  1. Add each secret's value:  printf '%s' "\$VALUE" | gcloud secrets versions add NAME --data-file=- --project $PROJECT
  2. Share the settings sheet with $SA as Editor.
  3. deploy/deploy.sh builds the image and creates the Cloud Run Jobs and their schedules.
     It grants the scheduler run.invoker on each job (job-level, not project-wide).
EOF
