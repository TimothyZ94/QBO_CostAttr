# QBO Customer Exporter

A small modular Python utility that:

1. Pops up and asks for **Intuit Client ID** every run.
2. Pops up and asks for **Intuit Client Secret** every run.
3. Does **not** save either credential to disk.
4. Opens the Intuit OAuth authorization page in your browser.
5. Gets the QBO `realmId` / Company ID from OAuth.
6. Fetches all active and inactive QBO Customer entities.
7. Pages through customer results.
8. Exports customer names and IDs to `output/qbo_customers.csv`.

## Project structure

- `main.py` — application runner
- `config.py` — non-secret settings
- `credentials_ui.py` — popup dialogs
- `oauth_flow.py` — Intuit OAuth 2.0
- `qbo_client.py` — QBO REST/query API
- `exporter.py` — CSV output
- `requirements.txt` — Python dependencies
- `run_qbo_customer_export.bat` — Windows launcher

## Before first run

### 1. Create/configure your Intuit app

In the Intuit Developer Portal, create/open your QuickBooks Online app.

The app needs the QuickBooks Accounting scope.

### 2. Configure Redirect URI

The redirect URI in `config.py` must exactly match a redirect URI configured for the same environment in your Intuit app.

Default:

```python
ENVIRONMENT = "sandbox"
REDIRECT_URI = "http://localhost:8000/callback"
```

This is suitable for sandbox/local development.

### Production

For real QBO company data, change:

```python
ENVIRONMENT = "production"
REDIRECT_URI = "https://YOUR-REGISTERED-HTTPS-URL/callback"
```

The production redirect URI must be registered in the Intuit Developer Portal.

For a non-local HTTPS redirect, this program uses a manual fallback:
after Intuit redirects your browser, copy the **full redirected URL** from the browser address bar and paste it into the popup. The script reads the `code`, `realmId`, and `state` from that URL.

## Run on Windows

Double-click:

`run_qbo_customer_export.bat`

Or run manually:

```bash
pip install -r requirements.txt
python main.py
```

## Output

The CSV contains:

- `CustomerName`
- `CustomerId`
- `FullyQualifiedName`
- `CompanyName`
- `Active`
- `IsSubCustomer`
- `ParentCustomerId`

The requested QBO customer ID is the `CustomerId` column.

## Security design

The Client ID and Client Secret are held only in process memory and are not written by this program to any config, JSON, CSV, token cache, or `.env` file.

This version also does not persist OAuth access/refresh tokens. Therefore, each run performs a fresh QuickBooks authorization flow.
