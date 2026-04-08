# Cloud Run + Firestore Deploy

This app now supports Cloud Run for compute and Firestore for persistent history.

## 1. Set project and region

```powershell
$env:PROJECT_ID = "YOUR_PROJECT_ID"
$env:REGION = "us-east1"
$env:SERVICE = "irving-mvp"
$env:SERVICE_ACCOUNT = "irving-mvp-runtime"

gcloud config set project $env:PROJECT_ID
```

## 2. Enable required APIs

```powershell
gcloud services enable `
  run.googleapis.com `
  cloudbuild.googleapis.com `
  artifactregistry.googleapis.com `
  firestore.googleapis.com
```

## 3. Create Firestore database

Use Native mode. Pick a region close to your users.

```powershell
gcloud firestore databases create `
  --location=$env:REGION `
  --type=firestore-native
```

If a Firestore database already exists in the project, skip this step.

## 4. Create runtime service account

```powershell
gcloud iam service-accounts create $env:SERVICE_ACCOUNT `
  --display-name="Irving MVP Cloud Run Runtime"
```

Grant it Firestore access:

```powershell
gcloud projects add-iam-policy-binding $env:PROJECT_ID `
  --member="serviceAccount:$($env:SERVICE_ACCOUNT)@$($env:PROJECT_ID).iam.gserviceaccount.com" `
  --role="roles/datastore.user"
```

`roles/datastore.user` is the Firestore role that gives the service read/write access to documents.

## 5. Prepare runtime env vars

Copy `cloudrun.env.yaml.example` to a real file and fill in your values:

```powershell
Copy-Item cloudrun.env.yaml.example cloudrun.env.yaml
```

Minimum fields to fill:

- `NOTION_TOKEN`
- `NOTION_REVIEW_QUEUE_DB_ID`
- `NOTION_CONTEXT_SNAPSHOTS_DB_ID`
- `ANTHROPIC_API_KEY`
- `OPENAI_API_KEY`
- `GEMINI_API_KEY`

Optional:

- `IRVING_API_KEY`
- `GOOGLE_SERVICE_ACCOUNT_JSON`
- `DRIVE_OUTPUT_FOLDER_ID`
- `FIRESTORE_PROJECT_ID`
- `FIRESTORE_HISTORY_COLLECTION`

Notes:

- On Cloud Run, Firestore uses Application Default Credentials from the service account, so no Firestore key file is needed.
- Leave `FIRESTORE_PROJECT_ID` blank if the Firestore database is in the same Google Cloud project as the Cloud Run service.

## 6. Deploy to Cloud Run

From the repo root:

```powershell
gcloud run deploy $env:SERVICE `
  --source . `
  --region $env:REGION `
  --allow-unauthenticated `
  --service-account "$($env:SERVICE_ACCOUNT)@$($env:PROJECT_ID).iam.gserviceaccount.com" `
  --env-vars-file cloudrun.env.yaml
```

Cloud Run source deploy uses the repo `Dockerfile` if present.

## 7. Verify

After deploy:

```powershell
$env:SERVICE_URL = gcloud run services describe $env:SERVICE --region $env:REGION --format="value(status.url)"
Invoke-WebRequest "$env:SERVICE_URL/health" -UseBasicParsing
Invoke-WebRequest "$env:SERVICE_URL/history/state?user_id=test-user" -UseBasicParsing
```

Expected:

- `/health` returns JSON with `"status":"ok"`
- `/health` shows `"history":"configured"`
- `/history/state` returns a JSON payload rather than `404`

## 8. Point the frontend at Cloud Run

If you keep serving `index.html` from GitHub Pages, open the app with:

```text
https://irvinginsights.github.io/Irving-Agents/?server=https://YOUR_CLOUD_RUN_URL
```

The app stores that backend URL in local storage.

If you later serve the frontend from the same Cloud Run service or another non-GitHub host, the app defaults to same-origin automatically.

## 9. Recommended next hardening

- Move API keys into Secret Manager instead of plaintext env vars.
- Add a custom domain in front of Cloud Run.
- If you want warm-ish behavior, use Cloud Scheduler or an external pinger like UptimeRobot against `/health`.
