# Railway setup

The US Outbound engine runs on Railway, on Spill's existing paid plan. One Railway
project holds everything:

- a **PostgreSQL** database, which is the system of record;
- one always-on **worker** service, built from this repository's `Dockerfile`. The worker
  runs `us-outbound scheduler`, which starts every job on its SPEC 9 schedule in UK time
  (`us-outbound schedule` lists the jobs);
- **service variables** that hold the settings sheet id and the keys, all sealed.

Google is used only for a service account in the Google Cloud project Columbus
(`columbus-510209`). That service account reads and writes the settings sheet through the
Sheets API. It needs no billing.

This replaces Cloud Run Jobs, Cloud Scheduler, Secret Manager, Artifact Registry and
BigQuery (Harry's decision of 30 Sep 2026; see "Where this differs from SPEC" below).
Never paste a key into a chat, an issue or a commit. Keys go only into sealed Railway
variables.

## a. Project and database

1. Before creating anything, set the workspace's preferred region to **EU West
   (Amsterdam)** in Spill's workspace settings (railway.com/workspace). Its identifier is
   `europe-west4-drams3a`. New services then start in the EU.
2. **New Project → Empty project**. Name it **Columbus**, in Spill's workspace (open
   question A6b).
3. On the canvas choose **+ New → Database → PostgreSQL**. Keep the name **Postgres**,
   because the variable in step c refers to it by name. Open its **Settings** and check
   that the region, and its volume's region, are EU West (Amsterdam). Railway cannot move
   a volume between regions, and a Postgres service cannot drop its volume. So if either is
   wrong, delete the service and its volume, fix the preferred region (step 1) and add the
   database again.
4. Leave the database private. Do not add Public Access (a TCP proxy): the worker reaches
   the database over Railway's private network, and SPEC 2 allows no public endpoint.
5. Backups: in the Postgres service, open the **Backups** tab and turn on **Daily** (kept 6
   days) and **Weekly** (kept 27 days), if Spill's plan offers them (open question A6a).

## b. The worker service

1. **+ New → GitHub Repo → `harrydryden/columbus`**. Give the Railway GitHub app access to
   the repository if it asks. Name the service **us-outbound**.
2. In the service's **Settings**, check each of these:

| Setting | Value |
| :- | :- |
| Source | `harrydryden/columbus`, branch `main`. Every push to `main` redeploys, so Harry reviews before merging (SPEC 13) |
| Builder | Dockerfile. The build log says "Using detected Dockerfile!" |
| Custom start command | Empty. The Dockerfile's `ENTRYPOINT ["us-outbound"]` and `CMD ["scheduler"]` start the scheduler. A start command set here would replace the ENTRYPOINT |
| Restart policy | On Failure, max 10 restarts (Railway's default) |
| Healthcheck path | Empty. The worker serves no HTTP |
| Cron schedule | Empty. The worker runs all the time and keeps its own schedule |
| Serverless (app sleeping) | Off. Otherwise Railway sleeps a quiet worker and jobs are missed |
| Region and replicas | EU West (Amsterdam), **1 replica**. Two replicas would start every job twice |
| Networking | No public domain (do not click Generate Domain) and no TCP proxy |
| PR environments (project **Settings → Environments**) | **Off.** Otherwise every pull request gets its own copy of the worker, which runs the schedule next to production |

There is no `railway.json` or `railway.toml`. Railway's config-as-code files are
deprecated: new services cannot opt into them, and existing ones stop being read on
1 Dec 2026. The Dockerfile holds the start command, and the table above lists every
setting that must be checked by hand.

## c. Variables

In the **us-outbound** service, open **Variables**. Add each variable with **New Variable**:
sealed variables cannot be edited in the Raw Editor. Then open the variable's ⋮ menu and
choose **Seal**. Once sealed, a value can never be shown again, in the dashboard or the
API. It can only be replaced.

| Variable | Value | Sealed |
| :- | :- | :- |
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}`, typed exactly so. This is a reference variable, which Railway fills with the database's private URL | yes |
| `US_OUTBOUND_SETTINGS_SHEET_ID` | The id in the settings sheet's URL (`docs.google.com/spreadsheets/d/<id>/edit`) | yes |
| `US_OUTBOUND_GOOGLE_SERVICE_ACCOUNT_JSON` | The whole JSON key file from step d, pasted as it is (several lines are fine) | yes |
| `US_OUTBOUND_APOLLO_API_KEY` | Apollo master API key | yes |
| `US_OUTBOUND_CLAY_API_KEY` | Clay API key | yes |
| `US_OUTBOUND_INSTANTLY_API_KEY` | Instantly API key | yes |
| `US_OUTBOUND_HUBSPOT_TOKEN` | HubSpot service key with the SPEC 13 scopes (phase0-runbook.md §5) | yes |
| `US_OUTBOUND_SLACK_BOT_TOKEN` | Slack bot token (`xoxb-…`) from `deploy/slack-app-manifest.yaml`. Optional for now (Harry, 30 Sep): without it, dry-run jobs write their Slack messages to the log. Live runs refuse to start without it | yes |
| `US_OUTBOUND_CLAUDE_API_KEY` | The new Claude key, with a $10 monthly limit set in the Anthropic console (SPEC 1.1) | yes |
| `US_OUTBOUND_WATCHDOG_URL` | Optional: the Healthchecks.io ping URL of the outside watchdog (step h). Without it, `us-outbound golive` warns | yes |
| `RAILWAY_DEPLOYMENT_DRAINING_SECONDS` | `30`. On a redeploy the scheduler gets 30 s between SIGTERM and SIGKILL. It gives running jobs 25 s, then stops them | no (a setting) |
| `US_OUTBOUND_MAX_PARALLEL` | Optional: how many jobs may run at once (default `2`) | no (a setting) |

A key you do not have yet can be added later. Only the jobs that use it fail, and each
failure names the missing variable. `DATABASE_URL` holds a reference, not the password; if
Railway will not seal a reference variable, leave it unsealed. Press **Deploy** to apply the
staged changes.

Two rules for the sealed variables:

- Sealed values are not passed to `railway run`, `railway shell` or `railway variables`.
  Run commands **inside the worker** with `railway ssh` (step e), where every variable is
  set and the database's private address resolves.
- They are not copied into PR environments or duplicated environments. Add them again
  there if you ever create one.

## d. Google: the Sheets service account

In the Google Cloud console, select the project **Columbus (`columbus-510209`)**.

1. **APIs & Services → Library → Google Sheets API → Enable.** The jobs need nothing else.
   The console may ask to turn on the IAM API while you create the service account. That
   is free. The project needs no billing account: the Sheets API is free.
2. **IAM & Admin → Service Accounts → Create service account.** Name it `us-outbound-sheets`
   (email `us-outbound-sheets@columbus-510209.iam.gserviceaccount.com`). Grant it **no
   roles**: it reaches only the sheets that are shared with it.
3. Open the service account, then **Keys → Add key → Create new key → JSON**. The key file
   downloads. Keep it outside the repository.
   - **If key creation is refused** ("key creation is not allowed", or the button is
     greyed out), the spill.chat organization enforces the policy "Disable service account
     key creation" (`iam.disableServiceAccountKeyCreation`, or its managed form
     `iam.managed.disableServiceAccountKeyCreation`). Organizations created since May 2024
     have it on by default. An organization admin who holds Organization Policy
     Administrator (`roles/orgpolicy.policyAdmin`, granted at the organization) opens
     **IAM & Admin → Organization Policies** with the project `columbus-510209` selected,
     opens that policy, chooses **Manage policy → Override parent's policy**, sets
     enforcement **Off** for this project only, and saves. Create the key, then set the
     project back to **Inherit parent's policy**: the policy only stops new keys being
     made, so the existing key keeps working. If the policy cannot be changed, stop and
     tell me (open question A6c).
4. Paste the whole file into the sealed variable `US_OUTBOUND_GOOGLE_SERVICE_ACCOUNT_JSON`
   (step c). Then delete the downloaded file. If the key is ever lost, create a new one and
   delete the old key in the console.
5. Open **US Outbound – Settings**, then **Share**. Add the service account's email as
   **Editor** and untick "Notify people". If Google refuses because the address is outside
   spill.chat, a Google Workspace admin must allow sharing with it.

## e. Create the tables

The worker's **pre-deploy command** creates them. In the worker's **Settings → Deploy**, set
**Pre-deploy Command** to `us-outbound db apply --live`, with a **Pre-deploy Timeout** of
300 seconds. Railway runs it in the built image, with the service's variables and the
private network, before every deploy. If it fails, that deploy stops and the running one
stays up. Running it again changes nothing: tables and indexes are `IF NOT EXISTS`, and
views are `OR REPLACE`. The deploy log shows `Applied 149 statements to schema us_outbound`.

To run it by hand instead, use the Railway CLI (`npm i -g @railway/cli`, then `railway login`
and `railway link`): `railway ssh -- us-outbound db apply` prints the SQL, and adding
`--live` runs it. The dry-run needs no database, so `.venv/bin/us-outbound db apply` also
works on a laptop.

Without the CLI, any one-off command (`us-outbound settings load --live`, say) can run the
same way: make it the pre-deploy command, start a **new deployment** of the latest commit, read
the deploy log, then set the pre-deploy command back to `us-outbound db apply --live`. Two
things to know: a **Redeploy** reuses the earlier deployment's settings, so it runs the old
command; and the command is not run through a shell, so `a && b` runs only `a`. Use one
command per deployment.

## f. First dry-run checks

Every command runs dry unless it says `--live`, and `live_sending` stays `no` in phase 0.

```
railway ssh -- us-outbound schedule                     # the jobs, their UK times and next runs
railway ssh -- us-outbound dry-run settings_sync        # reads the sheet into the database; errors go to #us-outbound-dev
railway ssh -- us-outbound status                       # settings synced, heartbeats, mailboxes, campaigns
railway ssh -- us-outbound dry-run heartbeat_check
railway ssh -- us-outbound dry-run mailbox_health       # needs the Instantly key
railway ssh -- us-outbound dry-run suppression_load     # needs the HubSpot token
```

Then open the service's **Deployments → View logs**. You should see:

- `scheduler_start` listing every enabled job: the jobs `us-outbound schedule` shows with a next run
  (settings_sync twice, at 02:00 and at 11:30 on weekdays), not only the first four of phase 0;
- `scheduler_job_start` and `scheduler_job_end` with `"exit_code": 0` for
  `heartbeat_check` at five past each hour.

After a day, `us-outbound status` should show every phase-0 job as ok. The remaining
phase-0 steps are in [phase0-runbook.md](phase0-runbook.md).

## g. Costs

- **Railway:** Spill's existing plan. The worker idles at about 0.1 GB of memory and next
  to no CPU; Postgres needs about 0.2 GB and a small volume. At Railway's usage prices
  ($10 per GB of memory a month, $20 per vCPU a month, $0.15 per GB of volume a month),
  that is roughly $3–5 a month. It comes out of the usage the plan already includes, unless
  Spill's workspace already uses more than that (check **Usage** in the workspace
  settings). Backups are billed like volume storage.
- **Google Cloud:** nothing. The Sheets API is free and the project needs no billing
  account.
- **Claude:** the key's own $10 monthly cap (SPEC 1.1), as before.

## h. The outside watchdog

Every Slack alert comes from inside the worker, so if the worker, the database or the Slack token
dies, nothing is said at all. So `heartbeat_check`, at five past every hour, ends by pinging a
Healthchecks.io check, which emails Harry when the ping is late or says something is wrong (Harry,
7 Oct 2026; `us_outbound/ops/watchdog.py`).

1. At healthchecks.io, sign in (the free plan is enough) and choose **Add Check**. Name it
   **US Outbound worker**. Under **Schedule**, keep **Simple** and set **Period** to **1 hour** and
   **Grace Time** to **1 hour**. Under **Integrations**, keep **Email** on, sent to Harry
   (harry@spill.chat).
2. Copy the check's **Ping URL** (`https://hc-ping.com/…`). In the **us-outbound** service's
   **Variables**, add `US_OUTBOUND_WATCHDOG_URL` with it and **Seal** it: anyone who has the URL can
   report the worker alive. Press **Deploy**. Within the hour the check shows its first ping, and
   `us-outbound golive` stops warning "no outside watchdog".
3. Point Railway's deploy failures at Slack: **Project Settings → Webhooks → New Webhook**, with a
   Slack incoming-webhook URL for `#us-outbound` (Slack: **Apps → Incoming Webhooks**), for deployment
   status changes. Railway formats the message for Slack itself. A build that fails, or a worker that
   crashes and stops restarting, then reaches the channel even though no job runs.

What Healthchecks' emails mean:

- **Down, after a /fail ping:** `heartbeat_check` ran and found something to look at: a job that
  missed its heartbeat, a daily job whose latest run failed, or Slack refusing the bot token (or
  out of reach). `#us-outbound` says which, unless Slack is the problem: then replace
  `US_OUTBOUND_SLACK_BOT_TOKEN` and redeploy. `railway ssh -- us-outbound status` lists the jobs.
- **Down, because the ping is late:** no `heartbeat_check` for two hours, so the worker, the
  scheduler or the database is down. Open the service's **Deployments** and **View logs**.
- **Up:** the next ping found nothing wrong.

The ping goes in dry-run too, so the check works before `live_sending` = yes. An unset or bad URL is
logged and skipped, and a ping that fails never fails the job.

## How the scheduler behaves

`us_outbound/ops/scheduler.py` has the full rules. In short:

- It checks the schedule at each minute boundary, in Europe/London time.
- Each due job runs as its own process, `python -m us_outbound run <job> [--live]`, so a
  failing job cannot stop the scheduler. The job's output goes to the same log.
- A job is never run twice at once. A job that is due while still running is skipped and
  logged. `heartbeat.run_job` also refuses a second run through the database, which covers
  the few seconds of a redeploy when the old and new worker overlap.
- At most `US_OUTBOUND_MAX_PARALLEL` jobs run at once (default 2). Other due jobs wait, in
  schedule order.
- A run that passes its timeout (`ops/schedule.py`) is stopped.
- Missed minutes are not run afterwards: after a restart or redeploy, a job waits for its
  next time. `heartbeat_check` alerts in Slack on any job that misses its heartbeat, and
  `us-outbound run <job> --live` runs it by hand.
- The UK clock changes:
  - On the spring change (29 Mar 2026), 01:00–01:59 does not exist, so `suppression_load`
    (01:30) runs once at 02:00.
  - On the autumn change (25 Oct 2026), 01:00–01:59 happens twice, and a job fixed in that
    hour runs only the first time.
  - Jobs with `*` in the minute or hour, such as the polls and the hourly checks, keep
    their real-time rhythm.
- A `--live` job is still dry until `live_sending = yes` in the synced settings (SPEC 0.3): a sheet
  edit counts from the next settings_sync (02:00, and 11:30 on weekdays) or `us-outbound sync`.
- Nothing inside the worker can report that the worker itself is down. Railway restarts it
  on failure, up to 10 times. The outside watchdog (step h) emails Harry when `heartbeat_check`
  stops pinging, and Railway's deploy webhook posts failed deployments to Slack.

## Where this differs from SPEC (Harry, 30 Sep 2026)

| SPEC | Says | Now |
| :- | :- | :- |
| 1.7 Secrets | Keys in Google Secret Manager | Sealed Railway service variables; `context.Secrets` reads them from the environment |
| 1.2, 3, 6 | BigQuery `us_outbound` is the system of record | Railway PostgreSQL, schema `us_outbound`, in the EU West (Amsterdam) region |
| 3 | Cloud Run Jobs and Cloud Scheduler in Spill's Google Cloud project | One always-on Railway worker, whose scheduler starts each job as a process |
| 13 Operations | CLI "locally or through gcloud run jobs execute" | Locally, or inside the worker with `railway ssh -- us-outbound <command>` |
| 13 Secrets | "Google service account" | `US_OUTBOUND_GOOGLE_SERVICE_ACCOUNT_JSON`: the key of the Sheets-only service account in `columbus-510209` |
