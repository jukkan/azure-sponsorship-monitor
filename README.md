# Azure Sponsorship Monitor

Track the consumption of sponsored Azure credits **and estimate costs** using the Azure Commerce UsageAggregates and RateCard APIs.

## Problem

Azure Sponsorship offers (e.g. `MS-AZR-0036P`) can't be monitored via the normal Azure Consumption API. This app uses `UsageManagementClient` from `azure-mgmt-commerce`, which queries the `/providers/Microsoft.Commerce/UsageAggregates` ARM endpoint. It also fetches the **RateCard** for the configured offer so that estimated costs can be calculated per usage line, including support for tiered pricing.

<img width="1364" height="1177" alt="image" src="https://github.com/user-attachments/assets/7c4d7d65-7964-42e2-a42d-f756ffaeb865" />

## Features

- Date-range, granularity (Daily / Hourly), and instance-details selectors
- **Services / AI Detail toggle** — Services view shows per-service cost cards; AI Detail view breaks down Azure OpenAI model usage (GPT-5.x, Sora, etc.)
- **BRSDT rate decoding** — the Commerce API collapses all sponsored AI usage into a single `Daily_BRSDT_*` meter; the app decodes the implied unit rate to identify the specific model and token type (input / output / cached)
- Per-meter summary cards sorted by estimated cost, with a show-more toggle
- Grand total and average daily cost cards
- Daily cost chart (stacked bar or pie) with configurable top-N meters
- **Hierarchical cascading filters** — category → sub-category → meter name; selecting a parent scopes child dropdowns to matching values; filters also update the chart in real time
- Configurable table columns with CSV and JSON export (em-dash safe)
- Cache-efficiency metric for AI models with cached-input token rates
- Rate-mapping reference table showing known BRSDT rate → model mappings
- Loading spinner while API calls are in progress
- Input validation (start < end, max 365 days, end < today)
- Graceful fallback when RateCard data is unavailable
- `/health` liveness probe endpoint

## Project structure

| File | Purpose |
|------|---|
| `app.py` | Flask web app – authenticates via `DefaultAzureCredential`, fetches usage aggregates and rate card, calculates tiered costs, renders dashboard |
| `templates/index.html` | Dashboard UI with summary cards, sortable/filterable usage table, and loading overlay |
| `requirements.txt` | Dependencies: Flask 3.1.0, azure-mgmt-commerce 6.0.0, azure-identity 1.21.0, python-dotenv 1.1.0, six ≥ 1.16.0 |
| `.env.example` | Documents all environment variables (subscription, credentials, RateCard settings) |
| `tests/test_app.py` | 31 pytest unit tests — all pass without real Azure credentials via mocking |
| `SETUP.md` | End-to-end setup guide for Windows |
| `.gitignore` | Excludes `.env`, `venv/`, `__pycache__/`, `data/`, build artifacts |

## Quick start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure credentials
cp .env.example .env
# → edit .env with your subscription ID and service-principal credentials

# 3. Start the app
flask run
```

Open http://localhost:5000/, select a date range, and click **Refresh**.

For the full walkthrough (virtual environment, service principal setup, troubleshooting) see [SETUP.md](SETUP.md).

## Environment variables

| Variable | Required | Default | Purpose |
|----------|----------|---------|---|
| `AZURE_SUBSCRIPTION_ID` | **Yes** | — | Target sponsorship subscription |
| `AZURE_TENANT_ID` | No\* | — | Entra ID tenant for the service principal |
| `AZURE_CLIENT_ID` | No\* | — | Service principal application ID |
| `AZURE_CLIENT_SECRET` | No\* | — | Service principal secret |
| `AZURE_OFFER_ID` | No | `MS-AZR-0036P` | Offer durable ID for the RateCard API |
| `AZURE_CURRENCY` | No | `USD` | Currency code for RateCard |
| `AZURE_LOCALE` | No | `en-US` | Locale for RateCard |
| `AZURE_REGION_INFO` | No | `US` | Region for RateCard |
| `FLASK_SECRET_KEY` | No | `dev-secret-change-me` | Flask session secret |

\* Not required if using `az login` (Azure CLI fallback via `DefaultAzureCredential`).

## Tests

```bash
pip install pytest
pytest tests/ -v
```

31 tests cover: health endpoint, record rendering, aggregation, date validation, missing subscription handling, API error handling, parameter forwarding, property mapping, cost calculation (flat rate, included quantity, tiered, zero, empty), total cost display, RateCard warning, BRSDT decoding (meter name/name field detection, AI rate matching, tolerance matching, unmatched/zero-cost rates, non-AI passthrough, real-name preservation), service-view BRSDT collapse, and rate reference table.

## Security

- Credentials are read from environment variables / `.env` (never hardcoded)
- `.env` is in `.gitignore`
- Input validation prevents malformed or excessively wide date ranges
- No write operations are performed against Azure — only read-only API calls

## Feature requests & ideas

Have an idea for something that would make this tool more useful? Open an [issue](../../issues) on GitHub — all suggestions welcome.
