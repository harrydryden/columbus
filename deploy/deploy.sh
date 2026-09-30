#!/usr/bin/env bash
# Build the image and create or update one Cloud Run Job per entry in deploy/jobs.yaml,
# plus a Cloud Scheduler job for each one with a schedule (SPEC 3, 9, 13).
#
#   PROJECT=columbus-510209 REGION=europe-west2 SETTINGS_SHEET_ID=... deploy/deploy.sh
#
# Cloud Run Jobs are not HTTP services: there is no public endpoint (SPEC 2). Cloud
# Scheduler starts each job through the Cloud Run Admin API as the us-outbound service
# account, which holds run.invoker on that job only. Disabled (later-phase) jobs get
# nothing, and an existing scheduler for one is paused.
#
# Each command is printed before it runs; DRY_RUN=1 prints without running. Harry reviews
# before each deploy (SPEC 13). Needs gcloud, docker (or BUILDER=cloudbuild), and the repo's
# Python environment (pip install -e .) to read jobs.yaml.
set -euo pipefail

: "${PROJECT:?set PROJECT, the Google Cloud project id}"
: "${REGION:?set REGION, e.g. europe-west2}"
: "${SETTINGS_SHEET_ID:?set SETTINGS_SHEET_ID, the US Outbound – Settings spreadsheet id}"
BQ_LOCATION="${BQ_LOCATION:-EU}"
REPO="${REPO:-us-outbound}"
SA="us-outbound@${PROJECT}.iam.gserviceaccount.com"
TAG="${TAG:-$(git rev-parse --short HEAD)}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/us-outbound:${TAG}"
PY="${PY:-.venv/bin/python}"
TZ_NAME="Europe/London"  # SPEC 9: all times are UK time

cd "$(dirname "$0")/.."

run() {
  printf '+ %s\n' "$*" >&2
  if [[ "${DRY_RUN:-0}" != "1" ]]; then "$@"; fi
}
exists() { "$@" >/dev/null 2>&1; }

if [[ -n "$(git status --porcelain)" ]]; then
  echo "the working tree has uncommitted changes; commit them so the image tag names what is deployed" >&2
  exit 1
fi

echo "== Build $IMAGE"
if [[ "${BUILDER:-docker}" == "cloudbuild" ]]; then
  # Cloud Build has a free daily allowance; check with Harry before relying on it (SPEC 1.1).
  run gcloud builds submit --tag "$IMAGE" --project "$PROJECT" .
else
  run gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet
  run docker build --platform linux/amd64 -t "$IMAGE" .
  run docker push "$IMAGE"
fi

ENV_VARS="US_OUTBOUND_PROJECT=${PROJECT},US_OUTBOUND_BQ_LOCATION=${BQ_LOCATION},US_OUTBOUND_SETTINGS_SHEET_ID=${SETTINGS_SHEET_ID}"

PLAN="$("$PY" -m us_outbound deploy plan)"
# The plan is read on fd 3 so no gcloud command in the loop can swallow it from stdin.
while IFS=$'\t' read -r name schedule disabled args timeout memory <&3; do
  job="us-outbound-${name//_/-}"
  sched="${job}-schedule"
  echo "== $name ($job)"
  if [[ "$disabled" == "true" ]]; then
    echo "disabled (later phase): nothing deployed"
    if exists gcloud scheduler jobs describe "$sched" --location "$REGION" --project "$PROJECT"; then
      run gcloud scheduler jobs pause "$sched" --location "$REGION" --project "$PROJECT"
    fi
    continue
  fi
  run gcloud run jobs deploy "$job" --project "$PROJECT" --region "$REGION" --image "$IMAGE" \
    --service-account "$SA" --args="$args" --set-env-vars "$ENV_VARS" \
    --tasks 1 --max-retries 0 --task-timeout "$timeout" --memory "$memory" --cpu 1 \
    --labels system=us-outbound,job="${name//_/-}"
  # PHASE0-CONFIRM: roles/run.invoker carries run.jobs.run (Cloud Run's scheduling guide uses it).
  run gcloud run jobs add-iam-policy-binding "$job" --project "$PROJECT" --region "$REGION" \
    --member "serviceAccount:$SA" --role roles/run.invoker --quiet >/dev/null
  if [[ "$schedule" == "-" ]]; then
    echo "no schedule: run it with 'gcloud run jobs execute $job --region $REGION'"
    continue
  fi
  uri="https://run.googleapis.com/v2/projects/${PROJECT}/locations/${REGION}/jobs/${job}:run"
  if exists gcloud scheduler jobs describe "$sched" --location "$REGION" --project "$PROJECT"; then
    run gcloud scheduler jobs update http "$sched" --location "$REGION" --project "$PROJECT" \
      --schedule "$schedule" --time-zone "$TZ_NAME" --uri "$uri" --http-method POST \
      --oauth-service-account-email "$SA"
    run gcloud scheduler jobs resume "$sched" --location "$REGION" --project "$PROJECT" >/dev/null || true
  else
    run gcloud scheduler jobs create http "$sched" --location "$REGION" --project "$PROJECT" \
      --schedule "$schedule" --time-zone "$TZ_NAME" --uri "$uri" --http-method POST \
      --oauth-service-account-email "$SA" --attempt-deadline 60s \
      --description "US Outbound $name (SPEC 9)"
  fi
done 3<<<"$PLAN"

echo "Deployed $IMAGE. Check with: gcloud run jobs list --region $REGION --project $PROJECT; then 'us-outbound status'."
