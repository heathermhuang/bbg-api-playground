# Bloomberg Terminal API Playground

Your Bloomberg Terminal as a REST API. Query BDP, BDH, BDS, BQL, intraday data, and more from any browser -- with an AI assistant that writes the queries for you.

Built on [blpapi](https://www.bloomberg.com/professional/support/api-library/) + [OpenBB](https://openbb.co). AI assistant supports [Claude](https://anthropic.com), [OpenAI](https://openai.com), and [Gemini](https://ai.google.dev) -- bring your own key.

---

### The Playground

Point, click, query. Every Bloomberg endpoint in one interface with syntax-highlighted JSON, sortable tables, and auto-generated charts. The sidebar organizes 30+ example requests by category so you never start from scratch.

![Bloomberg API Playground](docs/images/hero-playground.png)

---

### Instant Data Tables

Responses automatically render as sortable, formatted tables. Green/red color coding on percentage changes. Click any column header to sort. Supports multi-security, multi-field queries out of the box.

![Sortable Table View](docs/images/table-view.png)

---

### AI That Speaks Bloomberg

Ask in plain English, get the exact API call + Excel formula. The Claude-powered assistant knows every Bloomberg field, BQL syntax, and the difference between what works via API vs. what needs the Excel Add-in. Hit the Run button to execute directly.

![AI Chat Assistant](docs/images/ai-chat.png)

---

### Excel Formula Builder

108 Bloomberg fields across 9 categories. Pick a security, pick a field, get the exact =BDP(), =BDH(), =BDS(), or =BQL() formula ready to paste. The quick reference shows which fields work via API and which are Excel-only.

![Formula Builder](docs/images/formula-builder.png)

---

### Excel Bridge

Get data from the API straight into Excel. Auto-generated Power Query M code, VBA macros with MSXML2.XMLHTTP60, tab-separated copy for quick paste, and CSV download. Data refreshes with one click in Excel.

![Excel Bridge](docs/images/excel-bridge.png)

---

### Mobile Ready

Full playground on your phone. Sidebar becomes a slide-out drawer, tabs scroll horizontally, chat goes full-screen. Same functionality, smaller screen.

<p align="center">
  <img src="docs/images/mobile-responsive.png" alt="Mobile Responsive" width="280">
</p>

---

## Features

- **REST API wrapper** -- BDP, BDH, BDS, BQL, intraday bars/ticks, field search, security lookup, yield curves
- **Interactive playground** -- categorized examples, parameter editor, one-click execution
- **AI assistant** -- chatbot that builds Bloomberg API calls and Excel formulas from natural language (Claude, GPT, Gemini -- bring your own key)
- **Multiple views** -- JSON, sortable table, auto-detected charts (time series, bar, pie)
- **Formula Builder** -- generates =BDP(), =BDH(), =BDS(), =BQL() with 108-field quick reference
- **Excel Bridge** -- Power Query, VBA macro, TSV copy, CSV download
- **OpenBB integration** -- equities, fixed income, FX, economy, derivatives via OpenBB Platform
- **Mobile responsive** -- sidebar drawer, scrollable tabs, full-screen chat
- **Configurable endpoints** -- set API URLs per-browser via settings modal
- **CSV export** -- append `?format=csv` to any BDP/BDH/BDS endpoint

## Prerequisites

- **Bloomberg Terminal** running on the same machine (or network-accessible via `BBG_HOST`)
- **Python 3.9+**
- **blpapi** Python package (requires the Bloomberg C++ SDK)

## Quick Start

```bash
# Clone the repo
git clone https://github.com/heathermhuang/bbg-api-playground.git
cd bbg-api-playground

# Copy and edit environment config
cp .env.example .env
# Edit .env with your Bloomberg host/port, optional Anthropic key, etc.

# Install dependencies
pip install -r requirements.txt

# Start the all-in-one playground (API + static files + proxy)
python proxy-playground.py
```

Open **http://127.0.0.1:8081** in your browser.

## Architecture

```
Browser ──> proxy-playground.py (:8081)
               ├─ /bdp, /bdh, /bds, /bql, ...  ──> bbg_api.py (:8195) ──> Bloomberg Terminal
               ├─ /api/...                      ──> OpenBB API (:6900)
               └─ /*.html, /*.js, /*.css        ──> static files
```

### Running Components Separately

```bash
# Bloomberg API server only
uvicorn bbg_api:app --host 127.0.0.1 --port 8195

# OpenBB API (if installed)
openbb-api --host 127.0.0.1 --port 6900

# Proxy + static server
python proxy-playground.py
```

## Configuration

All configuration is via environment variables (or a `.env` file):

| Variable | Default | Description |
|----------|---------|-------------|
| `BBG_HOST` | `127.0.0.1` | Bloomberg Terminal host |
| `BBG_PORT` | `8194` | Bloomberg Terminal port |
| `API_HOST` | `127.0.0.1` | API server listen address |
| `API_PORT` | `8195` | API server listen port |
| `PROXY_HOST` | `127.0.0.1` | Proxy server listen address |
| `PROXY_PORT` | `8081` | Proxy server listen port |
| `OPENBB_HOST` | `127.0.0.1` | OpenBB API host |
| `OPENBB_PORT` | `6900` | OpenBB API port |
| `SERVE_DIR` | `./` | Static file directory |
| `ALLOWED_IPS` | `*` | Comma-separated IP whitelist, or `*` for all |
| `AI_PROVIDER` | `anthropic` | Default AI provider (`anthropic`, `openai`, `google`) |
| `AI_MODEL` | | Override default model for the provider |
| `ANTHROPIC_API_KEY` | | API key for Claude |
| `OPENAI_API_KEY` | | API key for OpenAI / compatible APIs |
| `GOOGLE_API_KEY` | | API key for Google Gemini |

The OpenAI provider works with any OpenAI-compatible API (Groq, Together, Mistral, etc.) -- set `OPENAI_BASE_URL` to point to your provider.

### Browser-Side Settings

Click the gear icon in the playground header to configure:
- **Bloomberg API URL** -- where BDP/BDH/BDS/BQL requests go
- **OpenBB API URL** -- where OpenBB requests go
- **Display name** -- shown in the status bar
- **AI Provider** -- choose Anthropic (Claude), OpenAI (GPT), or Google (Gemini)
- **Model** -- override the default model for your provider
- **API Key** -- saved to the server per-provider for chatbot use

Settings persist in `localStorage` per browser.

## File Structure

```
playground.html          Main playground UI (all-in-one SPA)
formula-builder.html     Standalone Excel formula builder
fields.js                Shared Bloomberg field definitions (108 fields, 9 categories)
bbg_api.py               FastAPI server wrapping blpapi
proxy-playground.py      Reverse proxy + static file server
proxy.py                 Minimal Bloomberg-only proxy
proxy-bbg.py             Bloomberg-only proxy variant
serve.py                 Simple static file server
requirements.txt         Python dependencies
.env.example             Environment variable template
.gitignore               Git ignore rules
```

## API Endpoints

### Bloomberg Terminal

| Method | Path | Description |
|--------|------|-------------|
| GET | `/bdp?securities=...&fields=...` | Reference data (current values) |
| GET | `/bdh?securities=...&fields=...&start_date=...` | Historical time series |
| GET | `/bds?security=...&field=...` | Bulk/array data |
| GET | `/bql?query=...` | Bloomberg Query Language |
| GET | `/intraday/bars?security=...&interval=...&start_datetime=...` | Intraday bars |
| GET | `/intraday/ticks?security=...&start_datetime=...` | Intraday ticks |
| GET | `/fields/search?query=...` | Field name search |
| GET | `/fields/info?fields=...` | Field metadata |
| GET | `/security/lookup?query=...` | Security search |
| GET | `/curve?curve_id=USD` | Yield curve |
| GET | `/health` | Health check |

Append `?format=csv` to `/bdp`, `/bdh`, or `/bds` for CSV download.

### OpenBB (proxied via `/api/`)

All OpenBB Platform API v1 endpoints are available under `/api/v1/...`.

## Security Notes

- **Never expose this directly to the internet** without IP whitelisting (`ALLOWED_IPS`) or a reverse proxy with authentication
- The API key is stored server-side in `config.json` (git-ignored)
- The proxy trusts `CF-Connecting-IP` header for Cloudflare deployments; strip this in other setups
- CORS is set to `*` for local development; restrict in production

## License

MIT License. See [LICENSE](LICENSE).
