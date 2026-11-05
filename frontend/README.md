# Local API usage dashboard

This Streamlit frontend is separate from the Lambda source. Its read-only S3 access
policy and connection outputs are defined in Serverless/CloudFormation.
It lets you choose a customer, select inclusive start/end dates, generate a usage
report, inspect request counts and per-key details, and download CSV or JSON.

## Run locally

From the repository root, use the existing Python 3.15 virtual environment:

```powershell
.\.venv\Scripts\python.exe -m pip install -r frontend/requirements.txt
cd frontend
..\.venv\Scripts\python.exe -m streamlit run app.py
```

Open **http://localhost:8501**. Starting from `frontend/` applies its `.streamlit/config.toml`
theme and binds the app to this machine. Press Ctrl+C in the terminal to stop it.

## Structure

| File | Responsibility |
| --- | --- |
| `app.py` | Customer/date form, metrics, chart, preview, and downloads |
| `report_service.py` | Sample loading, customer matching, range validation, and download formatting |
| `s3_source.py` | Manifest/hash-verified S3 reads and cross-month customer calculations |
| `connection.py` | Environment/Secrets settings and AWS client construction |
| `requirements.txt` | Pinned Streamlit version plus shared SDK requirements |
| `.streamlit/config.toml` | Local server and visual configuration |
| `tests/test_frontend.py` | Report calculations and actual Streamlit widget interactions |

## Data flow and boundaries

The sidebar offers **Local sample** and **S3 demo history**. Local customer choices
and date bounds come from `samples/api-gateway-usage.json`.
For a submitted request, the service invokes the existing local Lambda runner with
SDK-stubbed API Gateway responses and temporary local S3 output. It then selects
the requested customer and dates from the generated monthly report and recalculates
the totals. Temporary files are removed after the report is read; downloads are
delivered through the browser. Requests from multiple keys belonging to the same
customer are added together, with individual key rows retained for traceability.

The sample covers **October 1–31, 2026**. Both endpoints are included, including
single-day selections. The UI prevents selecting dates outside the sample and
rejects reversed ranges. It displays API request counts, not counts of distinct
API endpoints. It does not apply prices or billing rules.

Local mode makes no AWS calls. S3 mode reads the uploaded synthetic history as
described below. User login and RAG integration have not been implemented.
Date-range filtering for local mode happens in the frontend service. The Lambda
also supports explicit manual date ranges for separate live checks, while its
schedule remains monthly. Live UI access and customer authorization must be designed
before a shared deployment; a dropdown alone does not restrict data access.
The current Serverless package excludes the frontend, and no deployment is performed.

## Test

From the repository root:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s frontend/tests -v
```

GitHub Actions installs the frontend through `requirements-dev.txt` and runs these
tests separately from the backend suite. S3 unit/UI tests use in-memory objects;
they do not need AWS credentials.

## Connect to the existing S3 infrastructure

`infrastructure/frontend-data-access.yml` is imported by `serverless.yml`. The stack
outputs `FrontendHistoryBucket`, `FrontendHistoryPrefix`, `FrontendHistoryManifestKey`,
and `FrontendHistoryReadPolicyArn`. The managed policy grants only `s3:GetObject`
inside the synthetic dataset prefix. It is not automatically attached to a user or
role; attach it to the dedicated identity used by the hosted frontend. No access keys
or public bucket access are created by the template.

The reader gets customers and calendar bounds from the dataset manifest, then reads
only the selected dates. It verifies SHA-256 hashes, day/customer completeness,
synthetic labels, and counts before calculating totals. Missing data fails visibly;
explicit zero-usage records remain valid. Cross-month ranges are supported. JSON
downloads include source keys/hashes for traceability. Queries do not invoke the
reporting Lambda, generate new traffic, or write to S3.

For local use, copy `.streamlit/secrets.example.toml` to `.streamlit/secrets.toml`
inside this frontend folder and fill the bucket/prefix from the stack outputs:

```toml
[usage]
source = "s3"
region = "us-east-1"
bucket = "YOUR_FRONTEND_HISTORY_BUCKET_OUTPUT"
prefix = "usage/demo/dataset=historical-2026-08_2026-10-v1"
```

Use the existing local AWS profile/environment for credentials. Alternatively, set
`USAGE_DATA_SOURCE=s3`, `USAGE_S3_BUCKET`, `USAGE_S3_PREFIX`, and `AWS_DEFAULT_REGION`
in the process environment. These override the non-secret `[usage]` values. The UI
defaults to local mode if no source is configured; selecting S3 never silently
falls back to local sample data on a connection error.

For the hosted Streamlit app, put the same `[usage]` settings in **Manage app →
Settings → Secrets**, plus credentials for a dedicated read-only identity:

```toml
[aws]
access_key_id = "ENTER_IN_STREAMLIT_SECRETS_ONLY"
secret_access_key = "ENTER_IN_STREAMLIT_SECRETS_ONLY"
# Add session_token here only when using temporary credentials.
```

The hosted app cannot reuse a local AWS sign-in. Configure its identity through your
AWS access-management process and attach `FrontendHistoryReadPolicyArn`; do not use
an administrator identity for hosting. Never commit keys or paste them into chat.
Actual `.streamlit/secrets.toml` files are excluded by `.gitignore`.

The S3 source deliberately accepts only explicitly synthetic demo datasets. The
customer dropdown is a filter, not authorization for private customer data. Add
customer authentication and authorization before introducing real customer records.
