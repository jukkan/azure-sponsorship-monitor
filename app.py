"""
Azure Sponsorship Monitor
~~~~~~~~~~~~~~~~~~~~~~~~~
A lightweight Flask web-app that queries the Azure Commerce
UsageAggregates API so that sponsored-subscription usage can be
tracked without the normal Consumption API (which does not cover
sponsorship offers such as ms-azr-0036p).

The app also fetches the RateCard for the configured offer so that
estimated costs can be calculated per usage line.

Environment variables (see .env.example):
  AZURE_SUBSCRIPTION_ID – target subscription
  AZURE_TENANT_ID       – service-principal tenant   \
  AZURE_CLIENT_ID       – service-principal app-id    > optional; falls back to
  AZURE_CLIENT_SECRET   – service-principal secret   /  DefaultAzureCredential
  FLASK_SECRET_KEY      – Flask session secret
  AZURE_OFFER_ID        – e.g. MS-AZR-0036P (default)
  AZURE_CURRENCY        – e.g. USD (default)
  AZURE_LOCALE          – e.g. en-US (default)
  AZURE_REGION_INFO     – e.g. US (default)
"""

import logging
import os
from datetime import datetime, timedelta, timezone

from azure.identity import DefaultAzureCredential
from azure.mgmt.commerce import UsageManagementClient
from dotenv import load_dotenv
from flask import Flask, render_template, request

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Azure helpers
# ---------------------------------------------------------------------------

def _get_client() -> UsageManagementClient:
    """Return an authenticated UsageManagementClient."""
    subscription_id = os.environ["AZURE_SUBSCRIPTION_ID"]
    credential = DefaultAzureCredential()
    return UsageManagementClient(credential, subscription_id)


def fetch_rate_card() -> dict[str, dict]:
    """
    Fetch the RateCard for the configured offer and return a dict keyed by
    meter_id.  Each value contains ``meter_rates`` (a dict of tiered rates)
    and ``included_quantity``.

    Returns an empty dict if the RateCard call fails (e.g. the offer doesn't
    support it) so the dashboard can still render quantity-only data.
    """
    offer_id = os.environ.get("AZURE_OFFER_ID", "MS-AZR-0036P")
    currency = os.environ.get("AZURE_CURRENCY", "USD")
    locale = os.environ.get("AZURE_LOCALE", "en-US")
    region = os.environ.get("AZURE_REGION_INFO", "US")

    rate_filter = (
        f"OfferDurableId eq '{offer_id}' and Currency eq '{currency}' "
        f"and Locale eq '{locale}' and RegionInfo eq '{region}'"
    )
    try:
        client = _get_client()
        card = client.rate_card.get(filter=rate_filter)
    except Exception:  # noqa: BLE001
        log.warning("RateCard fetch failed – costs will not be available", exc_info=True)
        return {}

    rates: dict[str, dict] = {}
    for meter in card.meters or []:
        mid = getattr(meter, "meter_id", None)
        if not mid:
            continue
        # meter_rates is a dict like {"0": 0.025} or {"0": 0.10, "100": 0.08}
        raw_rates = getattr(meter, "meter_rates", {}) or {}
        rates[mid] = {
            "meter_rates": {float(k): v for k, v in raw_rates.items()},
            "included_quantity": getattr(meter, "included_quantity", 0) or 0,
        }
    return rates


def calculate_cost(quantity: float, rate_info: dict) -> float:
    """
    Given a usage quantity and a rate_info dict (from fetch_rate_card),
    calculate the estimated cost applying tiered rates.
    """
    if not rate_info or not rate_info.get("meter_rates"):
        return 0.0

    included = rate_info.get("included_quantity", 0) or 0
    billable = max(0.0, quantity - included)
    if billable == 0:
        return 0.0

    tiers = sorted(rate_info["meter_rates"].items())  # [(0, rate), (100, rate), …]
    cost = 0.0
    remaining = billable

    for i, (threshold, rate) in enumerate(tiers):
        if remaining <= 0:
            break
        # How much quantity falls in this tier?
        if i + 1 < len(tiers):
            next_threshold = tiers[i + 1][0]
            tier_qty = min(remaining, next_threshold - threshold)
        else:
            tier_qty = remaining
        cost += tier_qty * rate
        remaining -= tier_qty

    return cost


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
        records.append(
            {
                "id": item.id,
                "name": item.name,
                "meter_id": getattr(item, "meter_id", "") or "",
                "meter_name": getattr(item, "meter_name", "") or "",
                "meter_category": getattr(item, "meter_category", "") or "",
                "meter_sub_category": getattr(item, "meter_sub_category", "") or "",
                "unit": getattr(item, "unit", "") or "",
                "quantity": getattr(item, "quantity", 0),
                "usage_start": getattr(item, "usage_start_time", ""),
                "usage_end": getattr(item, "usage_end_time", ""),
                "subscription_id": getattr(item, "subscription_id", ""),
                "info_fields": getattr(item, "info_fields", {}),
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
    default_end = today - timedelta(days=1)

    start_str = request.args.get("start", default_start.strftime("%Y-%m-%d"))
    end_str = request.args.get("end", default_end.strftime("%Y-%m-%d"))
    granularity = request.args.get("granularity", "Daily")
    show_details = request.args.get("show_details", "false").lower() == "true"

    error = None
    rate_card_warning = None
    records = []
    total_quantity_by_meter: dict[str, float] = {}
    total_cost_by_meter: dict[str, float] = {}
    unit_by_meter: dict[str, str] = {}
    grand_total_cost = 0.0
    chart_labels_list: list[str] = []
    chart_daily: dict[str, dict[str, float]] = {}
    currency = os.environ.get("AZURE_CURRENCY", "USD")
    fetched = request.args.get("refresh") == "true"

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
        if end_dt >= today:
            raise ValueError("End date must be before today (usage data is delayed 24–48 h).")

        if fetched:
            records = fetch_usage(start_dt, end_dt, granularity, show_details)

            # Fetch rates and calculate costs
            rate_map = fetch_rate_card()
            if not rate_map and records:
                rate_card_warning = (
                    "RateCard data is unavailable \u2013 cost estimates are not shown. "
                    "Check AZURE_OFFER_ID and credentials."
                )

            for rec in records:
                key = rec["meter_name"] or rec["name"] or "Unknown"
                qty = float(rec["quantity"] or 0)
                total_quantity_by_meter[key] = (
                    total_quantity_by_meter.get(key, 0.0) + qty
                )
                if key not in unit_by_meter:
                    unit_by_meter[key] = rec.get("unit", "")
                meter_id = rec.get("meter_id", "")
                rate_info = rate_map.get(meter_id)
                cost = calculate_cost(qty, rate_info) if rate_info else 0.0
                rec["cost"] = cost
                total_cost_by_meter[key] = total_cost_by_meter.get(key, 0.0) + cost
                grand_total_cost += cost
                # chart: accumulate daily cost per meter
                _d = str(rec["usage_start"])[:10]
                if key not in chart_daily:
                    chart_daily[key] = {}
                chart_daily[key][_d] = chart_daily[key].get(_d, 0.0) + cost
            chart_labels_list = sorted({str(r["usage_start"])[:10] for r in records})

    except ValueError as exc:
        error = str(exc)
    except KeyError:
        error = (
            "AZURE_SUBSCRIPTION_ID is not set. "
            "Copy .env.example to .env and fill in your credentials."
        )
    except Exception as exc:  # noqa: BLE001
        error = f"Azure API error: {exc}"

    # Sort meter summaries by cost descending
    sorted_meters = sorted(
        total_quantity_by_meter.keys(),
        key=lambda m: total_cost_by_meter.get(m, 0.0),
        reverse=True,
    )
    sorted_quantity = {m: total_quantity_by_meter[m] for m in sorted_meters}
    sorted_cost = {m: total_cost_by_meter.get(m, 0.0) for m in sorted_meters}

    chart_meter_order = [m for m in sorted_meters if total_cost_by_meter.get(m, 0.0) > 0]
    chart_series_data = {
        m: [chart_daily.get(m, {}).get(d, 0.0) for d in chart_labels_list]
        for m in chart_meter_order
    }

    return render_template(
        "index.html",
        records=records,
        total_quantity_by_meter=sorted_quantity,
        total_cost_by_meter=sorted_cost,
        unit_by_meter=unit_by_meter,
        grand_total_cost=grand_total_cost,
        currency=currency,
        rate_card_warning=rate_card_warning,
        fetched=fetched,
        start=start_str,
        end=end_str,
        granularity=granularity,
        show_details=show_details,
        error=error,
        chart_labels=chart_labels_list,
        chart_series_data=chart_series_data,
        chart_meter_order=chart_meter_order,
    )


@app.route("/health")
def health():
    """Simple liveness probe."""
    return {"status": "ok"}, 200


if __name__ == "__main__":
    app.run(debug=False)
