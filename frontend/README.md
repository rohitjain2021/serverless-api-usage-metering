# Local API usage dashboard

This Streamlit frontend is separate from the Lambda source and Serverless deployment.
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
| `requirements.txt` | Pinned Streamlit version plus shared SDK requirements |
| `.streamlit/config.toml` | Local server and visual configuration |
| `tests/test_frontend.py` | Report calculations and actual Streamlit widget interactions |

## Data flow and boundaries

The customer choices and date bounds come from `samples/api-gateway-usage.json`.
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

This UI makes no AWS calls and provides no authentication or live S3/RAG integration.
Date-range filtering for this sample UI happens in the frontend service. The Lambda
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
tests separately from the backend suite.
