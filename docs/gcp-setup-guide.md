# GCP / BigQuery Setup Guide — AU Migration & Jobs Radar

> One-time setup for the cloud data warehouse layer of the project.
> Estimated time: **20–30 minutes**.
> Estimated cost: **$0** within the BigQuery sandbox / always-free tier
> (1 TB query + 10 GB storage / month).

---

## Prerequisites

- A Google account
- A web browser
- Ability to install the `gcloud` CLI (optional but recommended)

---

## Step 1 — Create a GCP project

1. Open the [Cloud Console → Project picker](https://console.cloud.google.com/projectselector2/home/dashboard).
2. Click **New Project**.
3. Fill in:
   - **Project name**: `au-jobs-radar`
   - **Project ID**: `au-jobs-radar` (will get a numeric suffix if taken — copy
     the final ID, you'll need it for `.env`)
   - **Organisation / Location**: leave default (No organisation is fine for a
     personal portfolio project)
4. Click **Create**. Wait ~30 seconds for the project to provision.
5. Make sure the project is selected in the top-bar dropdown before continuing.

> **Why**: BigQuery datasets live inside a GCP project. dbt uses
> `project.dataset.table` as its fully-qualified identifier.

---

## Step 2 — Enable required APIs

In the Cloud Console search bar, search for and **Enable** each of:

1. **BigQuery API** — query engine
2. **BigQuery Storage API** — fast reads (used by dbt + pandas)
3. **Cloud Storage API** — for raw JSONL landing bucket

Or via CLI:

```bash
gcloud config set project au-jobs-radar
gcloud services enable bigquery.googleapis.com \
                       bigquerystorage.googleapis.com \
                       storage.googleapis.com
```

---

## Step 3 — Create the GCS bucket (raw landing zone)

1. Go to [Cloud Storage → Buckets](https://console.cloud.google.com/storage/browser).
2. Click **Create**.
3. Fill in:
   - **Name**: `au-jobs-radar-raw` (must be globally unique — append your
     initials if taken, e.g. `au-jobs-radar-raw-sw`)
   - **Location type**: Region → `australia-southeast1` (Sydney)
   - **Storage class**: Standard
   - **Public access prevention**: Enforced (default — keep it on)
   - **Access control**: Uniform
4. Click **Create**.

> **Why**: The plan stores raw Adzuna JSON Lines as date-partitioned files
> here. BigQuery will then load them into the `raw.adzuna_jobs` table.

Update `.env`:
```
GCS_BUCKET_RAW=au-jobs-radar-raw   # or your chosen name
```

---

## Step 4 — Create the three BigQuery datasets

In the Cloud Console:

1. Open [BigQuery Studio](https://console.cloud.google.com/bigquery).
2. In the **Explorer** panel, find your project `au-jobs-radar`. Click the
   three-dot menu → **Create dataset**.
3. Repeat the form **three times** — once for each layer:

| Dataset ID | Location           | Default table expiration |
|------------|--------------------|--------------------------|
| `raw`      | australia-southeast1 | (leave empty / never)  |
| `staging`  | australia-southeast1 | (leave empty / never)  |
| `marts`    | australia-southeast1 | (leave empty / never)  |

> **Why three datasets**: matches the `staging → intermediate → marts` dbt
> pattern. dbt-bigquery will use `dataset_raw / dataset_staging / dataset_marts`
> from your profile.

CLI alternative:

```bash
for ds in raw staging marts; do
  bq --location=australia-southeast1 mk --dataset au-jobs-radar:$ds
done
```

---

## Step 5 — Create a service account for the pipeline

1. [IAM & Admin → Service Accounts](https://console.cloud.google.com/iam-admin/serviceaccounts).
2. Click **Create service account**.
3. Fill in:
   - **Name**: `au-jobs-radar-pipeline`
   - **Description**: "Reads/writes BigQuery + GCS for the AU Jobs Radar pipeline"
4. Click **Create and continue**.
5. **Grant access** — add these roles **one by one**:
   - `BigQuery Data Editor`     (read/write tables)
   - `BigQuery Job User`        (run queries / load jobs)
   - `Storage Object Admin`     (read/write GCS objects in our bucket)
6. Click **Done**.

### Generate the JSON key

1. In the service-accounts list, click the email of the one you just created.
2. **Keys** tab → **Add Key** → **Create new key** → **JSON** → **Create**.
3. A `*.json` file downloads. **Move it into the project root** and rename to
   `gcp-credentials.json` (this filename is already in `.gitignore`).
4. Update `.env`:

```
GOOGLE_APPLICATION_CREDENTIALS=./gcp-credentials.json
GCP_PROJECT_ID=au-jobs-radar     # use the actual ID from Step 1
GCP_LOCATION=australia-southeast1
```

> **Security**: This file is the equivalent of a password for your GCP
> resources. **Never commit it.** If it leaks, revoke it immediately from the
> Keys tab and generate a new one.

---

## Step 6 — Verify access

From the project root with `.env` filled in:

```bash
# Sanity check via the bq CLI (after `gcloud auth activate-service-account`):
bq --project_id=au-jobs-radar ls
# Expected output: raw / staging / marts datasets listed.

# Or via Python (after `uv sync`):
uv run python -c "from google.cloud import bigquery; \
  c = bigquery.Client(); \
  print('Datasets:', [d.dataset_id for d in c.list_datasets()])"
```

If both commands list `raw`, `staging`, `marts` — Step 6 passes. If you see
`PERMISSION_DENIED`, re-check the roles in Step 5.

---

## Step 7 — `dbt debug` smoke test (after Day 1 dbt skeleton lands)

```bash
cd dbt
uv run dbt debug --profiles-dir .
```

Expected output ends with:

```
All checks passed!
```

If `dbt debug` fails on auth, double-check:
- `GOOGLE_APPLICATION_CREDENTIALS` in `.env` is an **absolute path**, or run
  dbt from the project root so the relative path resolves correctly.
- The service account has the three roles from Step 5.

---

## Cost expectations

For a 14-day MVP at this scale:

| Service        | Usage                                 | Free tier | Expected cost |
|----------------|---------------------------------------|-----------|---------------|
| BigQuery query | <1 GB/day scanned in dev              | 1 TB/month | $0           |
| BigQuery storage | <100 MB total                       | 10 GB/month | $0          |
| Cloud Storage  | <50 MB JSONL files                    | 5 GB/month | $0           |
| Egress         | Power BI reads small fact tables only | 1 GB/month | $0           |

**Total: $0**. Set up a budget alert at $5/month for safety:
[Billing → Budgets & Alerts](https://console.cloud.google.com/billing/budgets).

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `403 Access Denied: BigQuery BigQuery: Permission denied while getting Drive credentials` | Service account missing `roles/bigquery.dataEditor` |
| `404 Not found: Dataset au-jobs-radar:raw was not found in location US` | You created the dataset in `US` instead of `australia-southeast1`. Recreate it. |
| `oauth2 errors / refresh token expired` | Regenerate the JSON key (Step 5) |
| `dbt debug` says profile not found | Make sure `profiles.yml` is in `~/.dbt/` or pass `--profiles-dir` pointing at the repo's `dbt/` directory |

---

When all 7 steps pass, mark Day-0 GCP setup as complete in `开发日志.txt` and
ping me to proceed with Day-1 dbt configuration.
