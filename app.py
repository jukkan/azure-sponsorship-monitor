"""
Azure Sponsorship Monitor
~~~~~~~~~~~~~~~~~~~~~~~~~
A lightweight Flask web-app that queries the Azure Commerce
UsageAggregates API so that sponsored-subscription usage can be
tracked without the normal Consumption API (which does not cover
sponsorship offers such as ms-azr-0036p).

Environment variables (see .env.example):
  AZURE_SUBSCRIPTION_ID – target subscription
  AZURE_TENANT_ID       – service-principal tenant   \
  AZURE_CLIENT_ID       – service-principal app-id    > optional; falls back to
  AZURE_CLIENT_SECRET   – service-principal secret   /  DefaultAzureCredential
  FLASK_SECRET_KEY      – Flask session secret
"""

import os
from datetime import datetime, timedelta, timezone

from azure.identity import DefaultAzureCredential
from azure.mgmt.commerce import UsageManagementClient
from dotenv import load_dotenv
from flask import Flask, render_template, request

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")

# ---------------------------------------------------------------------------
# Azure helpers
# ---------------------------------------------------------------------------

def _get_client() -> UsageManagementClient:
    """Return an authenticated UsageManagementClient."""
    subscription_id = os.environ["AZURE_SUBSCRIPTION_ID"]
    credential = DefaultAzureCredential()
    return UsageManagementClient(credential, subscription_id)


def fetch_usage(
    start_time: datetime,
    end_time: datetime,
    granularity: str = "Daily",
    show_details: bool = False,
) -> list[dict]:
    """
    Fetch usage aggregates for the configured subscription and return a list
    of simplified record dicts suitable for the template.

    Parameters
    ----------
    start_time:
        Start of the reporting window (UTC-aware datetime).
    end_time:
        End of the reporting window (UTC-aware datetime).
    granularity:
        ``"Daily"`` or ``"Hourly"``.
    show_details:
        When ``True`` the API returns instance-level meter details.
    """
    client = _get_client()
    results = client.usage_aggregates.list(
        reported_start_time=start_time,
        reported_end_time=end_time,
        show_details=show_details,
        aggregation_granularity=granularity,
    )

    records = []
    for item in results:
        props = item.properties if item.properties else {}
        records.append(
            {
                "id": item.id,
                "name": item.name,
                "meter_name": getattr(props, "meter_name", ""),
                "meter_category": getattr(props, "meter_category", ""),
                "meter_sub_category": getattr(props, "meter_sub_category", ""),
                "unit": getattr(props, "unit", ""),
                "quantity": getattr(props, "quantity", 0),
                "usage_start": getattr(props, "usage_start_time", ""),
                "usage_end": getattr(props, "usage_end_time", ""),
                "subscription_id": getattr(props, "subscription_id", ""),
                "info_fields": getattr(props, "info_fields", {}),
            }
        )
    return records


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    """Dashboard: render usage for the selected date range."""
    today = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    default_start = today - timedelta(days=30)
    default_end = today

    start_str = request.args.get("start", default_start.strftime("%Y-%m-%d"))
    end_str = request.args.get("end", default_end.strftime("%Y-%m-%d"))
    granularity = request.args.get("granularity", "Daily")
    show_details = request.args.get("show_details", "false").lower() == "true"

    error = None
    records = []
    total_quantity_by_meter: dict[str, float] = {}

    try:
        start_dt = datetime.strptime(start_str, "%Y-%m-%d").replace(
            tzinfo=timezone.utc
        )
        end_dt = datetime.strptime(end_str, "%Y-%m-%d").replace(
            tzinfo=timezone.utc
        )
        if start_dt >= end_dt:
            raise ValueError("Start date must be before end date.")
        if (end_dt - start_dt).days > 365:
            raise ValueError("Date range must not exceed 365 days.")

        records = fetch_usage(start_dt, end_dt, granularity, show_details)

        for rec in records:
            key = rec["meter_name"] or rec["name"] or "Unknown"
            total_quantity_by_meter[key] = (
                total_quantity_by_meter.get(key, 0.0) + float(rec["quantity"] or 0)
            )

    except ValueError as exc:
        error = str(exc)
    except KeyError:
        error = (
            "AZURE_SUBSCRIPTION_ID is not set. "
            "Copy .env.example to .env and fill in your credentials."
        )
    except Exception as exc:  # noqa: BLE001
        error = f"Azure API error: {exc}"

    return render_template(
        "index.html",
        records=records,
        total_quantity_by_meter=total_quantity_by_meter,
        start=start_str,
        end=end_str,
        granularity=granularity,
        show_details=show_details,
        error=error,
    )


@app.route("/health")
def health():
    """Simple liveness probe."""
    return {"status": "ok"}, 200


if __name__ == "__main__":
    app.run(debug=False)
