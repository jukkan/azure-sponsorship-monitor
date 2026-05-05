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


# Maps unit rate ($/unit, rounded to 2 dp) to a human-readable AI meter label.
# Rates confirmed from live billing data on MS-AZR-0036P (April 2026).
_BRSDT_RATE_LABELS: dict[float, str] = {
    0.10: "Sora 2 · video ($/sec)",
    0.17: "GPT-5.2 · cached input",   # legacy rate, pre-March 2026
    0.25: "GPT-5.4 · cached input",
    0.50: "GPT-5.4-pp · cached input",
    1.75: "GPT-5.2 · input",
    2.50: "GPT-5.4 · input",
    5.00: "GPT-5.4-pp · input",
    14.00: "GPT-5.2 · output",
    15.00: "GPT-5.4 · output",
    30.00: "GPT-5.4-pro · input / GPT-5.4-pp · output",
    180.00: "GPT-5.4-pro · output",
}
_BRSDT_OTHER_RATES: frozenset[float] = frozenset({0.04})
_BRSDT_PREFIX = "Daily_BRSDT_"
_BRSDT_AI_CATEGORY = "Azure OpenAI"
_BRSDT_OTHER_CATEGORY = "Sponsored (other)"
_BRSDT_RATE_TOLERANCE = 0.05  # 5 % relative tolerance for rate matching


def _match_brsdt_rate(implied_rate: float) -> tuple[bool, str | None]:
    """
    Match an implied rate against the BRSDT lookup table.

    Returns ``(matched, label)``:
    * ``(True, "<model label>")`` – AI model identified
    * ``(True, None)``           – non-AI sponsored service ($0.04 etc.)
    * ``(False, None)``          – no match found
    """
    # Non-AI flat-fee rates (small absolute tolerance)
    for other_rate in _BRSDT_OTHER_RATES:
        if abs(implied_rate - other_rate) < 0.005:
            return True, None

    # AI model rates (relative tolerance for tiered / FP edge-cases)
    best_label: str | None = None
    best_distance = float("inf")
    for rate, label in _BRSDT_RATE_LABELS.items():
        rel_distance = abs(implied_rate - rate) / rate
        if rel_distance < _BRSDT_RATE_TOLERANCE and rel_distance < best_distance:
            best_distance = rel_distance
            best_label = label

    if best_label is not None:
        return True, best_label
    return False, None


def _is_brsdt_row(rec: dict) -> bool:
    """Check if a record is a Daily_BRSDT billing row that needs decoding.

    A row only needs BRSDT decoding when it has the BRSDT identifier
    **and** the API did not already supply a real meter name/category.
    Many rows share the ``Daily_BRSDT_*`` name but already carry proper
    values like ``meter_name='B DTU'``, ``meter_category='SQL Database'``
    — those must be left untouched.
    """
    has_brsdt_id = (
        rec.get("meter_name", "").startswith(_BRSDT_PREFIX)
        or rec.get("name", "").startswith(_BRSDT_PREFIX)
    )
    if not has_brsdt_id:
        return False
    # If the API already provided a real meter name (not the BRSDT prefix
    # itself), the row does not need decoding.
    real_name = rec.get("meter_name", "")
    if real_name and not real_name.startswith(_BRSDT_PREFIX):
        return False
    return True


def _decode_brsdt(rec: dict, cost: float) -> dict:
    """
    For Daily_BRSDT_* rows, replace meter_name and meter_category with
    human-readable labels derived from the implied unit rate.

    Only rows matching a known AI model rate get ``is_brsdt = True``
    (collapsed into "Azure OpenAI" in the service view).  All other
    BRSDT rows are non-AI services that happen to share the same
    Commerce API meter — they get ``is_brsdt = False`` and category
    "Sponsored (other)".
    """
    if not _is_brsdt_row(rec):
        return rec

    qty = float(rec.get("quantity") or 0)
    if qty == 0:
        # Zero quantity — can't compute rate, treat as non-AI
        rec["meter_category"] = _BRSDT_OTHER_CATEGORY
        return rec

    if cost == 0:
        # RateCard missing or $0 billing period — can't determine model
        rec["meter_category"] = _BRSDT_OTHER_CATEGORY
        rec["meter_name"] = "BRSDT (unrated)"
        return rec

    implied_rate = round(cost / qty, 2)
    rec["brsdt_implied_rate"] = implied_rate
    matched, label = _match_brsdt_rate(implied_rate)

    if matched and label is not None:
        # Confirmed AI model usage
        rec["is_brsdt"] = True
        rec["meter_name"] = label
        rec["meter_category"] = _BRSDT_AI_CATEGORY
    elif matched:
        # Known non-AI rate ($0.04 etc.)
        rec["meter_category"] = _BRSDT_OTHER_CATEGORY
    else:
        # Unknown rate — non-AI service, label with rate for investigation
        log.info(
            "BRSDT row with unmatched rate $%.2f/unit (meter_id=%s)",
            implied_rate,
            rec.get("meter_id", ""),
        )
        rec["meter_name"] = f"BRSDT ${implied_rate:.2f}/unit"
        rec["meter_category"] = _BRSDT_OTHER_CATEGORY

    return rec


def _format_cost(cost: float | None) -> str:
    if not cost:
        return "0.00"
    return f"{cost:.2f}"


app.jinja_env.filters["format_cost"] = _format_cost


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
    average_daily_cost = 0.0
    chart_labels_list: list[str] = []
    chart_daily: dict[str, dict[str, float]] = {}
    svc_chart_daily: dict[str, dict[str, float]] = {}
    cache_efficiency: dict[str, float] = {}
    svc_cost: dict[str, float] = {}
    svc_quantity: dict[str, float] = {}
    svc_unit: dict[str, str] = {}
    brsdt_detail_keys: set[str] = set()
    brsdt_unmatched_rates: set[float] = set()
    has_brsdt = False
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

        selected_period_days = (end_dt - start_dt).days

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
                qty = float(rec["quantity"] or 0)
                meter_id = rec.get("meter_id", "")
                rate_info = rate_map.get(meter_id)
                cost = calculate_cost(qty, rate_info) if rate_info else 0.0
                rec["cost"] = cost
                rec = _decode_brsdt(rec, cost)
                if not rec.get("meter_category"):
                    rec["meter_category"] = "Unknown"

                is_brsdt = rec.get("is_brsdt", False)
                detail_key = rec["meter_name"] or rec["name"] or "Unknown"

                # Full-detail totals (all meters with decoded names)
                total_quantity_by_meter[detail_key] = (
                    total_quantity_by_meter.get(detail_key, 0.0) + qty
                )
                if detail_key not in unit_by_meter:
                    unit_by_meter[detail_key] = rec.get("unit", "")
                total_cost_by_meter[detail_key] = (
                    total_cost_by_meter.get(detail_key, 0.0) + cost
                )
                grand_total_cost += cost

                # Service-level totals (BRSDT collapsed to "Azure OpenAI")
                svc_key = _BRSDT_AI_CATEGORY if is_brsdt else detail_key
                svc_cost[svc_key] = svc_cost.get(svc_key, 0.0) + cost
                if not is_brsdt:
                    svc_quantity[svc_key] = (
                        svc_quantity.get(svc_key, 0.0) + qty
                    )
                    if svc_key not in svc_unit:
                        svc_unit[svc_key] = rec.get("unit", "")

                if is_brsdt:
                    has_brsdt = True
                    brsdt_detail_keys.add(detail_key)
                    if detail_key.startswith("BRSDT $"):
                        brsdt_unmatched_rates.add(
                            rec.get("brsdt_implied_rate", 0.0)
                        )

                # Chart: daily cost per meter (detail level)
                _d = str(rec["usage_start"])[:10]
                if detail_key not in chart_daily:
                    chart_daily[detail_key] = {}
                chart_daily[detail_key][_d] = (
                    chart_daily[detail_key].get(_d, 0.0) + cost
                )
                # Service-level chart
                if svc_key not in svc_chart_daily:
                    svc_chart_daily[svc_key] = {}
                svc_chart_daily[svc_key][_d] = (
                    svc_chart_daily[svc_key].get(_d, 0.0) + cost
                )
            chart_labels_list = sorted({str(r["usage_start"])[:10] for r in records})
            if selected_period_days > 0:
                average_daily_cost = grand_total_cost / selected_period_days

            # Cache efficiency: cached_input_tokens / (cached + regular input)
            _cache_input_suffix = "· cached input"
            _regular_input_suffix = "· input"
            cache_hits: dict[str, float] = {}
            regular_inputs: dict[str, float] = {}
            for key, qty in total_quantity_by_meter.items():
                if key.endswith(_cache_input_suffix):
                    model = key.replace(_cache_input_suffix, "").strip()
                    cache_hits[model] = cache_hits.get(model, 0.0) + qty
                elif key.endswith(_regular_input_suffix):
                    model = key.replace(_regular_input_suffix, "").strip()
                    regular_inputs[model] = regular_inputs.get(model, 0.0) + qty
            for model in cache_hits:
                total_in = cache_hits[model] + regular_inputs.get(model, 0.0)
                if total_in > 0:
                    cache_efficiency[model] = cache_hits[model] / total_in

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

    # Service-level summaries (BRSDT collapsed to "Azure OpenAI")
    svc_sorted_meters = sorted(
        svc_cost.keys(),
        key=lambda m: svc_cost.get(m, 0.0),
        reverse=True,
    )
    svc_sorted_cost_dict = {m: svc_cost.get(m, 0.0) for m in svc_sorted_meters}
    svc_sorted_quantity_dict = {
        m: svc_quantity.get(m, 0.0) for m in svc_sorted_meters
    }
    svc_meter_order = [
        m for m in svc_sorted_meters if svc_cost.get(m, 0.0) > 0
    ]
    svc_chart_series = {
        m: [svc_chart_daily.get(m, {}).get(d, 0.0) for d in chart_labels_list]
        for m in svc_meter_order
    }

    # AI-only summaries (decoded BRSDT meters)
    ai_sorted_meters = [
        m for m in sorted_meters if m in brsdt_detail_keys
    ]
    ai_quantity = {m: total_quantity_by_meter[m] for m in ai_sorted_meters}
    ai_cost = {
        m: total_cost_by_meter.get(m, 0.0) for m in ai_sorted_meters
    }
    ai_unit = {m: unit_by_meter.get(m, "") for m in ai_sorted_meters}
    ai_meter_order = [
        m for m in ai_sorted_meters
        if total_cost_by_meter.get(m, 0.0) > 0
    ]
    ai_chart_series = {
        m: [chart_daily.get(m, {}).get(d, 0.0) for d in chart_labels_list]
        for m in ai_meter_order
    }
    ai_total_cost = sum(ai_cost.values())

    return render_template(
        "index.html",
        records=records,
        total_quantity_by_meter=sorted_quantity,
        total_cost_by_meter=sorted_cost,
        unit_by_meter=unit_by_meter,
        grand_total_cost=grand_total_cost,
        average_daily_cost=average_daily_cost,
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
        cache_efficiency=cache_efficiency,
        has_brsdt=has_brsdt,
        svc_quantity=svc_sorted_quantity_dict,
        svc_cost=svc_sorted_cost_dict,
        svc_unit=svc_unit,
        svc_chart_series=svc_chart_series,
        svc_meter_order=svc_meter_order,
        ai_quantity=ai_quantity,
        ai_cost=ai_cost,
        ai_unit=ai_unit,
        ai_chart_series=ai_chart_series,
        ai_meter_order=ai_meter_order,
        ai_total_cost=ai_total_cost,
        brsdt_rate_labels=_BRSDT_RATE_LABELS,
        brsdt_other_rates=_BRSDT_OTHER_RATES,
        brsdt_unmatched_rates=sorted(brsdt_unmatched_rates),
    )


@app.route("/health")
def health():
    """Simple liveness probe."""
    return {"status": "ok"}, 200


if __name__ == "__main__":
    app.run(debug=False)
