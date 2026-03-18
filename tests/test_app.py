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

def _make_props(**kwargs):
    """Return a SimpleNamespace acting as AggregationProperties."""
    defaults = dict(
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


def _make_record(**kwargs):
    ns = SimpleNamespace(
        id="/subscriptions/sub-123/providers/Microsoft.Commerce/UsageAggregates/record-1",
        name="record-1",
        properties=_make_props(**kwargs),
    )
    return ns


def _fake_client_returning(records):
    """Return a mock UsageManagementClient whose .usage_aggregates.list yields *records*."""
    client = MagicMock()
    client.usage_aggregates.list.return_value = iter(records)
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
    import app as app_module
    app_module.app.config["TESTING"] = True
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
        resp = client.get("/")
    assert resp.status_code == 200
    assert b"No usage records found" in resp.data


def test_index_renders_records(app_client):
    """Records returned by the API should appear in the table."""
    client, app_module = app_client
    fake = _fake_client_returning([_make_record()])
    with patch.object(app_module, "_get_client", return_value=fake):
        resp = client.get("/")
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
        resp = client.get("/")
    assert resp.status_code == 200
    # 1.0 + 3.0 = 4.0 → displayed as 4.0000
    assert b"4.0000" in resp.data


def test_index_invalid_date_range(app_client):
    """When start >= end the page should show a validation error."""
    client, app_module = app_client
    fake = _fake_client_returning([])
    with patch.object(app_module, "_get_client", return_value=fake):
        resp = client.get("/?start=2024-02-01&end=2024-01-01")
    assert resp.status_code == 200
    assert b"Start date must be before" in resp.data


def test_index_date_range_too_wide(app_client):
    """A range exceeding 365 days should produce a validation error."""
    client, app_module = app_client
    fake = _fake_client_returning([])
    with patch.object(app_module, "_get_client", return_value=fake):
        resp = client.get("/?start=2020-01-01&end=2024-01-01")
    assert resp.status_code == 200
    assert b"365 days" in resp.data


def test_index_missing_subscription(app_client_no_sub):
    """A missing subscription ID should surface an informative error, not a 500."""
    resp = app_client_no_sub.get("/")
    assert resp.status_code == 200
    assert b"AZURE_SUBSCRIPTION_ID" in resp.data


def test_index_azure_api_error(app_client):
    """Generic Azure SDK errors should be caught and shown to the user."""
    client, app_module = app_client
    with patch.object(
        app_module, "_get_client", side_effect=RuntimeError("connection refused")
    ):
        resp = client.get("/")
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
