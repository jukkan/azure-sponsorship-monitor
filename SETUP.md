# End-to-End Setup on Windows

Complete walkthrough for running Azure Sponsorship Monitor locally.

---

## Prerequisites

| Tool | Minimum version | Install |
|------|-----------------|---------|
| Python | 3.11+ | [python.org/downloads](https://www.python.org/downloads/) — check **"Add python.exe to PATH"** during install |
| Git | any | [git-scm.com](https://git-scm.com/download/win) |
| Azure CLI *(optional)* | 2.50+ | [aka.ms/installazurecli](https://aka.ms/installazurecli) — needed only if you skip steps 2-3 and use `az login` instead |

Verify both are available:

```powershell
python --version
git --version
```

---

## Step 1 — Clone the repo and create a virtual environment

```powershell
git clone https://github.com/jukkan/azure-sponsorship-monitor.git
cd azure-sponsorship-monitor

python -m venv .venv
.\.venv\Scripts\Activate.ps1        # prompt changes to (.venv)
```

> **CMD users:** run `.venv\Scripts\activate.bat` instead.

Install the Python dependencies:

```powershell
pip install -r requirements.txt
```

This installs Flask 3.1, azure-mgmt-commerce 6.0, azure-identity 1.21, python-dotenv 1.1, and six (undeclared dependency of azure-mgmt-commerce).

---

## Step 2 — Register a service principal in Azure AD

The app needs read access to the Commerce/UsageAggregates endpoint. The cleanest way is a service principal with Reader access on the sponsorship subscription.

### 2a. Create the App Registration

1. Open the [Azure Portal](https://portal.azure.com) → **Microsoft Entra ID** → **App registrations** → **New registration**.
2. Name it something like `sponsorship-monitor`.
3. Leave **Supported account types** as "Single tenant".
4. Click **Register**.
5. On the overview page, note:
   - **Application (client) ID** → this is `AZURE_CLIENT_ID`
   - **Directory (tenant) ID** → this is `AZURE_TENANT_ID`

### 2b. Create a client secret

1. In the app registration, go to **Certificates & secrets** → **Client secrets** → **New client secret**.
2. Give it a description (e.g. `local-dev`) and pick an expiry.
3. Click **Add**.
4. **Copy the secret Value immediately** — it won't be shown again. This is `AZURE_CLIENT_SECRET`.

### 2c. Grant the service principal Reader on your subscription

1. Go to **Subscriptions** → select your sponsorship subscription.
2. Note the **Subscription ID** → this is `AZURE_SUBSCRIPTION_ID`.
3. Go to **Access control (IAM)** → **Add** → **Add role assignment**.
4. Select **Reader** role → click **Next**.
5. Click **+ Select members** → search for `sponsorship-monitor` → select it → **Review + assign**.

> **Why Reader?** The UsageAggregates API requires at least Reader on the subscription scope. No write permissions are needed.

---

## Step 3 — Configure environment variables

Copy the example file and fill in your values:

```powershell
Copy-Item .env.example .env
```

Open `.env` in your editor and replace the placeholders:

```ini
# Required — your sponsorship subscription
AZURE_SUBSCRIPTION_ID=<paste Subscription ID from step 2c>

# Service principal credentials from step 2a/2b
AZURE_TENANT_ID=<paste Directory (tenant) ID>
AZURE_CLIENT_ID=<paste Application (client) ID>
AZURE_CLIENT_SECRET=<paste client secret Value>
```

> **Alternative: skip the service principal entirely.**
> If you have the [Azure CLI](https://aka.ms/installazurecli) installed, run `az login` in your terminal and only set `AZURE_SUBSCRIPTION_ID` in `.env`. The app's `DefaultAzureCredential` will fall back to your CLI session automatically. This is the quickest path for local dev but won't work in headless/server environments.

---

## Step 4 — Run the app

```powershell
flask run
```

Open **http://localhost:5000/** in your browser. You should see the dashboard with date-range filters. Select a date range that overlaps with your sponsored usage and click **Refresh**.

> **Tip — accessing from an external browser:**
> By default Flask binds to `127.0.0.1`, which only accepts connections from the local machine. To allow access from other devices on the same network, start with:
> ```powershell
> flask run --host 0.0.0.0
> ```

> **Troubleshooting — "AZURE_SUBSCRIPTION_ID is not set":**
> Make sure `.env` exists in the repo root and the virtual environment is activated.
>
> **Troubleshooting — "Azure API error: … AuthenticationError":**
> Double-check all four credential values in `.env`. Ensure the service principal has Reader access on the correct subscription.
>
> **Troubleshooting — No records returned:**
> The UsageAggregates API can have a 24-48 hour delay. Try a wider date range ending yesterday.

---

## Step 5 — Run the tests

The test suite mocks all Azure SDK calls, so **no credentials are needed**:

```powershell
pip install pytest              # if not already installed
pytest tests/ -v
```

All 18 tests should pass. They cover:
- `/health` endpoint
- Dashboard rendering with and without records
- Per-meter total aggregation
- Date validation (start ≥ end, range > 365 days, end ≥ today)
- Missing subscription ID handling
- Azure API error handling
- Correct parameter forwarding to the SDK
- Property mapping from SDK response
- Cost calculation: flat rate, included quantity, tiered rates, zero quantity, empty rate info
- Grand total cost display
- RateCard unavailable warning

---

## Quick-reference: all environment variables

| Variable | Required | Default | Purpose |
|----------|----------|---------|---|
| `AZURE_SUBSCRIPTION_ID` | **Yes** | — | Target sponsorship subscription |
| `AZURE_TENANT_ID` | No\* | — | Entra ID tenant for the service principal |
| `AZURE_CLIENT_ID` | No\* | — | Service principal application ID |
| `AZURE_CLIENT_SECRET` | No\* | — | Service principal secret |
| `AZURE_OFFER_ID` | No | `MS-AZR-0036P` | Offer durable ID for the RateCard API |
| `AZURE_CURRENCY` | No | `USD` | Currency code for cost estimates |
| `AZURE_LOCALE` | No | `en-US` | Locale for the RateCard API |
| `AZURE_REGION_INFO` | No | `US` | Region for the RateCard API |
| `FLASK_SECRET_KEY` | No | `dev-secret-change-me` | Flask session secret |

\* Not required if using `az login` (Azure CLI fallback via `DefaultAzureCredential`).

---

## Security notes

- `.env` is in `.gitignore` — credentials are never committed.
- The app validates date inputs server-side (must parse as `YYYY-MM-DD`, start < end, max 365-day window).
- No write operations are performed against Azure — only read-only API calls.
