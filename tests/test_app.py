"""
Unit tests for app.py
~~~~~~~~~~~~~~~~~~~~~
These tests use pytest + Flask's test client and mock all Azure SDK calls so
no real Azure credentials are required to run them.
"""

import importlib
import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers to build a fake UsageAggregationManagementClient
# ---------------------------------------------------------------------------

def _make_record(**kwargs):
    """Return a SimpleNamespace mimicking a UsageAggregation with flat attributes."""
    defaults = dict(
        id="/subscriptions/sub-123/providers/Microsoft.Commerce/UsageAggregates/record-1",
        name="record-1",
        meter_id="meter-abc",
        meter_name="Compute Hours",
        meter_category="Virtual Machines",
        meter_sub_category="D-Series",
        unit="Hours",
        quantity=2.5,
        usage_start_time="2024-01-01T00:00:00Z",
        usage_end_time="2024-01-02T00:00:00Z",
        subscription_id="sub-123",
        info_fields={},
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _make_rate_card_response(meters=None):
    """Return a SimpleNamespace mimicking a ResourceRateCardInfo."""
    if meters is None:
        meters = []
    return SimpleNamespace(meters=meters)


def _make_meter(meter_id="meter-abc", meter_rates=None, included_quantity=0):
    """Return a SimpleNamespace mimicking a MeterInfo."""
    if meter_rates is None:
        meter_rates = {"0": 0.10}
    return SimpleNamespace(
        meter_id=meter_id,
        meter_rates=meter_rates,
        included_quantity=included_quantity,
    )


def _fake_client_returning(records, rate_card_meters=None):
    """Return a mock UsageManagementClient with usage_aggregates and rate_card."""
    client = MagicMock()
    client.usage_aggregates.list.return_value = iter(records)
    if rate_card_meters is not None:
        client.rate_card.get.return_value = _make_rate_card_response(rate_card_meters)
    else:
        # Default: return an empty rate card
        client.rate_card.get.return_value = _make_rate_card_response([])
    return client


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def app_client():
    """Create a Flask test client with AZURE_SUBSCRIPTION_ID set."""
    os.environ["AZURE_SUBSCRIPTION_ID"] = "sub-test-123"
    os.environ["FLASK_SECRET_KEY"] = "test-secret"

    # Import the app module fresh so env vars are picked up
    if "app" in sys.modules:
        del sys.modules["app"]
    import app as app_module
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as client:
        yield client, app_module


@pytest.fixture()
def app_client_no_sub():
    """Flask test client with NO subscription ID set."""
    os.environ.pop("AZURE_SUBSCRIPTION_ID", None)
    if "app" in sys.modules:
        del sys.modules["app"]
    with patch("dotenv.load_dotenv"):
        import app as app_module
    app_module.app.config["TESTING"] = True
    # Ensure it's truly absent even if load_dotenv ran elsewhere
    os.environ.pop("AZURE_SUBSCRIPTION_ID", None)
    with app_module.app.test_client() as client:
        yield client


# ---------------------------------------------------------------------------
# Tests – /health endpoint
# ---------------------------------------------------------------------------

def test_health_endpoint(app_client):
    client, _ = app_client
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ok"


# ---------------------------------------------------------------------------
# Tests – index / dashboard
# ---------------------------------------------------------------------------

def test_index_renders_without_records(app_client):
    """With a valid subscription but no usage records the page should load."""
    client, app_module = app_client
    fake = _fake_client_returning([])
    with patch.object(app_module, "_get_client", return_value=fake):
        resp = client.get("/?refresh=true")
    assert resp.status_code == 200
    assert b"No usage records found" in resp.data


def test_index_renders_records(app_client):
    """Records returned by the API should appear in the table."""
    client, app_module = app_client
    meters = [_make_meter(meter_id="meter-abc", meter_rates={"0": 0.10})]
    fake = _fake_client_returning([_make_record()], rate_card_meters=meters)
    with patch.object(app_module, "_get_client", return_value=fake):
        resp = client.get("/?refresh=true")
    assert resp.status_code == 200
    assert b"Compute Hours" in resp.data
    assert b"Virtual Machines" in resp.data
    assert b"2.500000" in resp.data


def test_index_aggregates_totals(app_client):
    """Quantities for the same meter should be summed in the summary cards."""
    client, app_module = app_client
    records = [
        _make_record(meter_name="Compute Hours", quantity=1.0),
        _make_record(meter_name="Compute Hours", quantity=3.0),
    ]
    fake = _fake_client_returning(records)
    with patch.object(app_module, "_get_client", return_value=fake):
        resp = client.get("/?refresh=true")
    assert resp.status_code == 200
    # 1.0 + 3.0 = 4.0 → displayed as 4.0000
    assert b"4.0000" in resp.data


def test_index_invalid_date_range(app_client):
    """When start >= end the page should show a validation error."""
    client, app_module = app_client
    fake = _fake_client_returning([])
    with patch.object(app_module, "_get_client", return_value=fake):
        resp = client.get("/?start=2024-02-01&end=2024-01-01&refresh=true")
    assert resp.status_code == 200
    assert b"Start date must be before" in resp.data


def test_index_date_range_too_wide(app_client):
    """A range exceeding 365 days should produce a validation error."""
    client, app_module = app_client
    fake = _fake_client_returning([])
    with patch.object(app_module, "_get_client", return_value=fake):
        resp = client.get("/?start=2020-01-01&end=2024-01-01&refresh=true")
    assert resp.status_code == 200
    assert b"365 days" in resp.data


def test_index_end_date_today_rejected(app_client):
    """End date set to today should be rejected (data not yet available)."""
    client, app_module = app_client
    from datetime import datetime, timedelta, timezone
    today = datetime.now(timezone.utc)
    yesterday = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    today_str = today.strftime("%Y-%m-%d")
    fake = _fake_client_returning([])
    with patch.object(app_module, "_get_client", return_value=fake):
        resp = client.get(f"/?start={yesterday}&end={today_str}&refresh=true")
    assert resp.status_code == 200
    assert b"End date must be before today" in resp.data


def test_index_missing_subscription(app_client_no_sub):
    """A missing subscription ID should surface an informative error, not a 500."""
    resp = app_client_no_sub.get("/?refresh=true")
    assert resp.status_code == 200
    assert b"AZURE_SUBSCRIPTION_ID" in resp.data


def test_index_azure_api_error(app_client):
    """Generic Azure SDK errors should be caught and shown to the user."""
    client, app_module = app_client
    with patch.object(
        app_module, "_get_client", side_effect=RuntimeError("connection refused")
    ):
        resp = client.get("/?refresh=true")
    assert resp.status_code == 200
    assert b"Azure API error" in resp.data


# ---------------------------------------------------------------------------
# Tests – fetch_usage helper
# ---------------------------------------------------------------------------

def test_fetch_usage_calls_api_with_correct_args(app_client):
    """fetch_usage should forward all parameters to the SDK list() call."""
    _, app_module = app_client
    fake = _fake_client_returning([])
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 31, tzinfo=timezone.utc)

    with patch.object(app_module, "_get_client", return_value=fake):
        app_module.fetch_usage(start, end, granularity="Hourly", show_details=True)

    fake.usage_aggregates.list.assert_called_once_with(
        reported_start_time=start,
        reported_end_time=end,
        show_details=True,
        aggregation_granularity="Hourly",
    )


def test_fetch_usage_maps_properties(app_client):
    """fetch_usage should map SDK properties to the expected dict keys."""
    _, app_module = app_client
    record = _make_record(meter_name="Storage", quantity=42.0, unit="GB")
    fake = _fake_client_returning([record])
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 31, tzinfo=timezone.utc)

    with patch.object(app_module, "_get_client", return_value=fake):
        results = app_module.fetch_usage(start, end)

    assert len(results) == 1
    assert results[0]["meter_name"] == "Storage"
    assert results[0]["quantity"] == 42.0
    assert results[0]["unit"] == "GB"


# ---------------------------------------------------------------------------
# Tests – calculate_cost helper
# ---------------------------------------------------------------------------

def test_calculate_cost_flat_rate(app_client):
    """Flat rate: quantity × rate."""
    _, app_module = app_client
    rate_info = {"meter_rates": {0.0: 0.10}, "included_quantity": 0}
    assert app_module.calculate_cost(10.0, rate_info) == pytest.approx(1.0)


def test_calculate_cost_with_included_quantity(app_client):
    """Included quantity should be subtracted before billing."""
    _, app_module = app_client
    rate_info = {"meter_rates": {0.0: 0.50}, "included_quantity": 5}
    # 10 - 5 included = 5 billable × 0.50 = 2.50
    assert app_module.calculate_cost(10.0, rate_info) == pytest.approx(2.50)


def test_calculate_cost_tiered_rates(app_client):
    """Tiered rates: first 100 at $0.10, remaining at $0.05."""
    _, app_module = app_client
    rate_info = {"meter_rates": {0.0: 0.10, 100.0: 0.05}, "included_quantity": 0}
    # 150 qty → 100 × 0.10 + 50 × 0.05 = 10.0 + 2.5 = 12.5
    assert app_module.calculate_cost(150.0, rate_info) == pytest.approx(12.5)


def test_calculate_cost_zero_quantity(app_client):
    """Zero quantity should return zero cost."""
    _, app_module = app_client
    rate_info = {"meter_rates": {0.0: 0.10}, "included_quantity": 0}
    assert app_module.calculate_cost(0.0, rate_info) == 0.0


def test_calculate_cost_empty_rate_info(app_client):
    """Missing rate info should return zero."""
    _, app_module = app_client
    assert app_module.calculate_cost(10.0, {}) == 0.0
    assert app_module.calculate_cost(10.0, None) == 0.0


# ---------------------------------------------------------------------------
# Tests – cost display on dashboard
# ---------------------------------------------------------------------------

def test_index_shows_total_cost(app_client):
    """When rate card data is available, grand total cost should appear."""
    client, app_module = app_client
    meters = [_make_meter(meter_id="meter-abc", meter_rates={"0": 0.10})]
    records = [_make_record(meter_id="meter-abc", quantity=100.0)]
    fake = _fake_client_returning(records, rate_card_meters=meters)
    with patch.object(app_module, "_get_client", return_value=fake):
        resp = client.get("/?refresh=true")
    assert resp.status_code == 200
    # 100.0 × $0.10 = $10.00
    assert b"10.00" in resp.data
    assert b"Estimated total cost" in resp.data


def test_index_shows_rate_card_warning(app_client):
    """When rate card fetch fails, a warning should appear."""
    client, app_module = app_client
    fake = _fake_client_returning([_make_record()])
    fake.rate_card.get.side_effect = RuntimeError("rate card unavailable")
    with patch.object(app_module, "_get_client", return_value=fake):
        resp = client.get("/?refresh=true")
    assert resp.status_code == 200
    assert b"RateCard data is unavailable" in resp.data
