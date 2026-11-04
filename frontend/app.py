"""Streamlit customer usage explorer.

Input: customer dropdown and inclusive date selections within the local fixture.
Output: a report preview, usage chart, and CSV/JSON downloads on Generate report.
Launch from the repository root with: python -m streamlit run frontend/app.py
"""

# Bootstrap project imports regardless of the Streamlit working directory.
from pathlib import Path
import sys
# Render the frontend using the dependency pinned in frontend/requirements.txt.
import streamlit as st

# Keep all shared paths and styling before the rendering functions.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
PAGE_STYLE = """
<style>
.stApp { background: #f7f9fb; }
.block-container { max-width: 1160px; padding-top: 2.5rem; padding-bottom: 3rem; }
h1 { letter-spacing: -0.04em; }
div[data-testid="stMetric"] { background: white; border: 1px solid #e1e7ec;
    border-radius: 14px; padding: 20px; }
div[data-testid="stForm"] { background: white; border-radius: 14px; }
</style>
"""

# Import the report boundary after making the repository packages available.
sys.path.insert(0, str(PROJECT_ROOT))
from frontend.report_service import available_source, downloads, generate_customer_report


def render_report(report):
    """Input: generated customer report. Render its totals, key rows, and downloads.

    Returns None. The displayed dates always come from the generated report, even
    if the user is preparing a different selection in the form.
    """
    st.subheader(report["customer_name"])
    st.caption(f"{report['customer_id']}  ·  {report['start_date']} to {report['end_date']}  ·  UTC dates")
    requests, keys, days = st.columns(3)
    requests.metric("API requests", f"{report['total_requests']:,}")
    keys.metric("API keys used", report["api_key_count"])
    days.metric("Days selected", report["days_selected"])
    st.write("")
    # Retain per-key detail so several keys belonging to one customer remain traceable.
    st.subheader("Usage breakdown")
    rows = [{"API key": row["api_key_name"] or row["api_key_id"],
             "API key ID": row["api_key_id"], "Usage plan": row["usage_plan_name"],
             "Requests": row["requests_used"]} for row in report["usage"]]
    if rows:
        st.dataframe(rows, hide_index=True, width="stretch")
        # Group daily records across the customer's keys for the chart, without creating daily files.
        daily_totals = {}
        for row in report["usage"]:
            for entry in row["daily_usage"]:
                daily_totals[entry["date"]] = daily_totals.get(entry["date"], 0) + entry["requests_used"]
        st.subheader("Requests over time")
        st.bar_chart([{"Date": day, "Requests": total} for day, total in sorted(daily_totals.items())],
                     x="Date", y="Requests", color="#137c70", height=230)
    else:
        st.info("No usage entries were returned for this customer in the selected period.")
    csv_bytes, json_bytes = downloads(report)
    filename = f"api-usage_{report['customer_id']}_{report['start_date']}_{report['end_date']}"
    st.subheader("Download report")
    csv_column, json_column, spacer = st.columns([1, 1, 2])
    csv_column.download_button("Download CSV", data=csv_bytes, file_name=filename + ".csv",
                               mime="text/csv", width="stretch", key="download_csv")
    json_column.download_button("Download JSON", data=json_bytes, file_name=filename + ".json",
                                mime="application/json", width="stretch", key="download_json")
    st.caption("Usage counts only. Pricing and billing calculations have not been applied.")


def main():
    """Render the selection form and generate a report only when explicitly submitted.

    Inputs: Streamlit widget/session state and the local sample file.
    Outputs: rendered controls and report, with validation errors shown in the page.
    No AWS credentials, deployment, S3 writes, or RAG service is used by this UI.
    """
    st.set_page_config(page_title="API Usage | Customer Reports", page_icon="📊", layout="wide")
    st.markdown(PAGE_STYLE, unsafe_allow_html=True)
    st.caption("API METERING  /  CUSTOMER REPORTS")
    st.title("Your API usage, clearly counted.")
    st.write("Choose a customer and reporting period to explore their API requests.")
    try:
        source = available_source()
    except (OSError, ValueError, KeyError) as error:
        st.error(f"Unable to load local usage data: {error}")
        return
    st.info(f"Local sample data · Available {source['start_date']:%d %b %Y} – "
            f"{source['end_date']:%d %b %Y}. Reports use inclusive UTC dates.")
    with st.form("report_filters"):
        customer_column, start_column, end_column = st.columns([2, 1, 1])
        customer = customer_column.selectbox(
            "Customer", options=list(source["customers"]), key="customer_id",
            format_func=lambda identifier: f"{source['customers'][identifier]} · {identifier}")
        start = start_column.date_input("Start date", value=source["start_date"],
                                        min_value=source["start_date"], max_value=source["end_date"],
                                        format="DD/MM/YYYY", key="start_date")
        end = end_column.date_input("End date", value=source["end_date"],
                                    min_value=source["start_date"], max_value=source["end_date"],
                                    format="DD/MM/YYYY", key="end_date")
        submitted = st.form_submit_button("Generate report", type="primary")
    if submitted:
        # Clear previous results before validation so an invalid request cannot show stale downloads.
        st.session_state.pop("usage_report", None)
        try:
            with st.spinner("Generating your usage report…"):
                st.session_state["usage_report"] = generate_customer_report(customer, start, end)
        except (ValueError, OSError, KeyError) as error:
            st.error(str(error))
    if "usage_report" in st.session_state:
        render_report(st.session_state["usage_report"])
    elif not submitted:
        st.caption("Your report will appear here. Select a customer and click Generate report to begin.")


# Streamlit executes this entrypoint; importing the module alone does not render a page.
if __name__ == "__main__":
    main()
