"""
Bloomberg Terminal HTTP API wrapper
Exposes BDP, BDH, BDS, intraday bars, field catalog, and AI chat via REST.
"""

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, Response
from pydantic import BaseModel, field_validator
from typing import Optional, List
import blpapi
import datetime
import os
import json
import csv
import io
import re as _re
import time
import threading
import logging

logger = logging.getLogger("bbg_api")

# Try loading .env file from the same directory as this script
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

# ── Docs toggle (disable in production with ENABLE_DOCS=false) ───────────────
_enable_docs = os.environ.get("ENABLE_DOCS", "true").lower() in ("1", "true", "yes")

app = FastAPI(
    title="Bloomberg Terminal API",
    description="REST wrapper for Bloomberg Terminal (blpapi). BDP, BDH, BDS, intraday bars, field catalog, AI assistant.",
    version="1.1.0",
    docs_url="/docs" if _enable_docs else None,
    redoc_url="/redoc" if _enable_docs else None,
    openapi_url="/openapi.json" if _enable_docs else None,
)

# ── Rate limiter (in-memory, per-IP) ─────────────────────────────────────────
class _RateLimiter:
    def __init__(self):
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def check(self, ip: str, limit: int, window_s: int = 60) -> bool:
        """Return True if under limit, False if rate-limited."""
        now = time.time()
        cutoff = now - window_s
        with self._lock:
            hits = self._hits.get(ip, [])
            hits = [t for t in hits if t > cutoff]
            if len(hits) >= limit:
                self._hits[ip] = hits
                return False
            hits.append(now)
            self._hits[ip] = hits
            return True

_rate = _RateLimiter()
_BBG_RATE_LIMIT = int(os.environ.get("BBG_RATE_LIMIT", "60"))   # per minute
_CHAT_RATE_LIMIT = int(os.environ.get("CHAT_RATE_LIMIT", "20"))  # per minute

_CORS_ORIGINS = os.environ.get("CORS_ORIGINS", "http://127.0.0.1:*,http://localhost:*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$",
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Log every request with client IP, method, path, and response code."""
    start = time.time()
    client_ip = request.client.host if request.client else "unknown"
    response = await call_next(request)
    elapsed = int((time.time() - start) * 1000)
    logger.info(f"{client_ip} {request.method} {request.url.path} -> {response.status_code} ({elapsed}ms)")
    return response

def _get_client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"

def _check_bbg_rate(request: Request):
    ip = _get_client_ip(request)
    if not _rate.check(ip, _BBG_RATE_LIMIT):
        raise HTTPException(429, detail="Rate limit exceeded. Try again in a minute.")

def _check_chat_rate(request: Request):
    ip = _get_client_ip(request)
    if not _rate.check(ip, _CHAT_RATE_LIMIT):
        raise HTTPException(429, detail="Chat rate limit exceeded. Try again in a minute.")

BBG_HOST = os.environ.get("BBG_HOST", "127.0.0.1")
BBG_PORT = int(os.environ.get("BBG_PORT", "8194"))

# ── CSV helper ────────────────────────────────────────────────────────────────

def _rows_to_csv(rows: list, filename: str = "bloomberg_data.csv") -> Response:
    """Convert a list of dicts to a CSV Response for Excel download."""
    if not rows:
        return Response(content="", media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

# ── Session ───────────────────────────────────────────────────────────────────

def _get_session():
    opts = blpapi.SessionOptions()
    opts.setServerHost(BBG_HOST)
    opts.setServerPort(BBG_PORT)
    session = blpapi.Session(opts)
    if not session.start():
        raise RuntimeError("Cannot connect to Bloomberg Terminal on port 8194")
    return session

def _open_service(session, name):
    if not session.openService(name):
        raise RuntimeError(f"Failed to open Bloomberg service: {name}")
    return session.getService(name)

def _send_and_collect(session, request, timeout_ms=15000):
    """Send request and collect all response messages, handling all event types safely."""
    session.sendRequest(request)
    msgs = []
    deadline = datetime.datetime.utcnow().timestamp() + timeout_ms / 1000
    while True:
        remaining = int((deadline - datetime.datetime.utcnow().timestamp()) * 1000)
        if remaining <= 0:
            raise TimeoutError("Bloomberg request timed out")
        ev = session.nextEvent(timeout=min(remaining, 5000))
        etype = ev.eventType()
        if etype in (blpapi.Event.PARTIAL_RESPONSE, blpapi.Event.RESPONSE):
            for msg in ev:
                msgs.append(msg)
            if etype == blpapi.Event.RESPONSE:
                return msgs
        elif etype == blpapi.Event.TIMEOUT:
            raise TimeoutError("Bloomberg request timed out")
        elif etype in (blpapi.Event.SESSION_STATUS, blpapi.Event.SERVICE_STATUS,
                       blpapi.Event.REQUEST_STATUS, blpapi.Event.AUTHORIZATION_STATUS):
            # Non-data events — check for errors then continue
            for msg in ev:
                if msg.hasElement("reason"):
                    reason = msg.getElement("reason")
                    desc = _get_str(reason, "description") or _get_str(reason, "message") or "Unknown error"
                    raise RuntimeError(f"Bloomberg error: {desc}")
        # All other event types: skip and keep waiting


def _element_to_python(el):
    """Convert a blpapi Element to a Python-native value."""
    try:
        dt = el.datatype()
        if el.isNull():
            return None
        if dt == blpapi.DataType.BOOL:
            return el.getValueAsBool()
        if dt in (blpapi.DataType.INT32, blpapi.DataType.INT64):
            return el.getValueAsInteger()
        if dt in (blpapi.DataType.FLOAT32, blpapi.DataType.FLOAT64):
            v = el.getValueAsFloat()
            return None if v != v else v  # NaN → None
        if dt in (blpapi.DataType.DATE, blpapi.DataType.TIME, blpapi.DataType.DATETIME):
            return str(el.getValueAsDatetime())
        if dt == blpapi.DataType.SEQUENCE:
            return [_element_to_python(el.getValueAsElement(i)) for i in range(el.numValues())]
        if dt == blpapi.DataType.CHOICE:
            return _element_to_python(el.getChoice())
        return el.getValueAsString() or None
    except Exception:
        try:
            return el.getValueAsString() or None
        except Exception:
            return None

def _element_to_dict(el):
    """Recursively convert a blpapi sequence element to a dict."""
    d = {}
    for i in range(el.numElements()):
        child = el.getElement(i)
        name = str(child.name())
        dt = child.datatype()
        if dt == blpapi.DataType.SEQUENCE:
            rows = []
            for j in range(child.numValues()):
                rows.append(_element_to_dict(child.getValueAsElement(j)))
            d[name] = rows
        else:
            d[name] = _element_to_python(child)
    return d

def _get_str(el, name):
    try:
        return el.getElementAsString(name) or None
    except Exception:
        return None


# ── Health ────────────────────────────────────────────────────────────────────

def _safe_error(e: Exception) -> str:
    """Truncate error messages to avoid leaking internal details."""
    msg = str(e)
    if len(msg) > 200:
        msg = msg[:200] + "..."
    # Strip file paths
    msg = _re.sub(r'[A-Z]:\\[^\s"]+', '[path]', msg)
    msg = _re.sub(r'/[^\s"]+\.py', '[path]', msg)
    return msg

@app.get("/health", tags=["System"])
def health():
    try:
        session = _get_session()
        session.stop()
        return {"status": "ok", "bloomberg_port": BBG_PORT, "blpapi_version": blpapi.version()}
    except Exception as e:
        raise HTTPException(503, detail="Bloomberg Terminal is not reachable")


# ── BDP ───────────────────────────────────────────────────────────────────────

@app.get("/bdp", tags=["Reference Data"])
def bdp(
    request: Request,
    securities: str = Query(..., description="Comma-separated tickers, e.g. AAPL US Equity,MSFT US Equity"),
    fields: str = Query(..., description="Comma-separated fields, e.g. PX_LAST,NAME,MARKET_CAP"),
    overrides: Optional[str] = Query(None, description="Semicolon-separated key=value overrides, e.g. PRICING_SOURCE=BGN"),
    format: Optional[str] = Query(None, description="Response format: json (default) or csv"),
):
    """Bloomberg Data Point — current/reference field values for one or more securities."""
    _check_bbg_rate(request)
    secs = [s.strip() for s in securities.split(",") if s.strip()]
    flds = [f.strip() for f in fields.split(",") if f.strip()]
    session = _get_session()
    try:
        svc = _open_service(session, "//blp/refdata")
        req = svc.createRequest("ReferenceDataRequest")
        for s in secs:
            req.append("securities", s)
        for f in flds:
            req.append("fields", f)
        if overrides:
            ovr = req.getElement("overrides")
            for pair in overrides.split(";"):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    o = ovr.appendElement()
                    o.setElement("fieldId", k.strip())
                    o.setElement("value", v.strip())
        results = []
        for msg in _send_and_collect(session, req):
            data = msg.getElement("securityData")
            for i in range(data.numValues()):
                item = data.getValueAsElement(i)
                sec = item.getElementAsString("security")
                row = {"security": sec}
                fd = item.getElement("fieldData")
                for f in flds:
                    try:
                        row[f] = _element_to_python(fd.getElement(f))
                    except Exception:
                        row[f] = None
                # Surface field exceptions
                if item.hasElement("fieldExceptions"):
                    exc = item.getElement("fieldExceptions")
                    for j in range(exc.numValues()):
                        ex_el = exc.getValueAsElement(j)
                        bad_field = _get_str(ex_el, "fieldId")
                        if bad_field:
                            row[bad_field] = f"[error: {_get_str(ex_el.getElement('errorInfo'), 'message')}]"
                results.append(row)
        if format == "csv":
            return _rows_to_csv(results, "bdp_data.csv")
        return {"results": results}
    except Exception as e:
        raise HTTPException(500, detail=_safe_error(e))
    finally:
        session.stop()


# ── BDH ───────────────────────────────────────────────────────────────────────

@app.get("/bdh", tags=["Historical Data"])
def bdh(
    request: Request,
    securities: str = Query(..., description="Comma-separated tickers"),
    fields: str = Query("PX_LAST", description="Comma-separated fields"),
    start_date: str = Query(..., description="YYYY-MM-DD or YYYYMMDD"),
    end_date: Optional[str] = Query(None, description="YYYY-MM-DD or YYYYMMDD (default: today)"),
    periodicity: str = Query("DAILY", description="DAILY, WEEKLY, MONTHLY, QUARTERLY, SEMI_ANNUALLY, YEARLY"),
    currency: Optional[str] = Query(None, description="Currency override, e.g. USD"),
    adjust: Optional[str] = Query(None, description="ACTUAL or CALENDAR"),
    format: Optional[str] = Query(None, description="Response format: json (default) or csv"),
):
    """Bloomberg Data History — OHLCV and field history for one or more securities."""
    _check_bbg_rate(request)
    secs = [s.strip() for s in securities.split(",") if s.strip()]
    flds = [f.strip() for f in fields.split(",") if f.strip()]
    end = (end_date or datetime.date.today().strftime("%Y%m%d")).replace("-", "")
    session = _get_session()
    try:
        svc = _open_service(session, "//blp/refdata")
        req = svc.createRequest("HistoricalDataRequest")
        for s in secs:
            req.append("securities", s)
        for f in flds:
            req.append("fields", f)
        req.set("startDate", start_date.replace("-", ""))
        req.set("endDate", end)
        req.set("periodicitySelection", periodicity)
        if currency:
            req.set("currency", currency)
        if adjust:
            req.set("adjustmentType", adjust)
        results = {}
        for msg in _send_and_collect(session, req):
            sd = msg.getElement("securityData")
            sec = sd.getElementAsString("security")
            rows = []
            fd_arr = sd.getElement("fieldData")
            for i in range(fd_arr.numValues()):
                rows.append(_element_to_dict(fd_arr.getValueAsElement(i)))
            results[sec] = rows
        if format == "csv":
            # Flatten multi-security results: add "security" column to each row
            flat = []
            for sec_key, sec_rows in results.items():
                for row in sec_rows:
                    flat.append({"security": sec_key, **row})
            return _rows_to_csv(flat, "bdh_data.csv")
        if len(secs) == 1:
            return {"security": secs[0], "results": list(results.values())[0] if results else []}
        return {"results": results}
    except Exception as e:
        raise HTTPException(500, detail=_safe_error(e))
    finally:
        session.stop()


# ── BDS ───────────────────────────────────────────────────────────────────────

@app.get("/bds", tags=["Bulk Data"])
def bds(
    request: Request,
    security: str = Query(..., description="Single ticker, e.g. SPX Index"),
    field: str = Query(..., description="Bulk field, e.g. INDX_MEMBERS, DVD_HIST_ALL"),
    overrides: Optional[str] = Query(None, description="Semicolon-separated key=value overrides"),
    format: Optional[str] = Query(None, description="Response format: json (default) or csv"),
):
    """Bloomberg Data Set — bulk/array fields like index members, dividend history."""
    _check_bbg_rate(request)
    session = _get_session()
    try:
        svc = _open_service(session, "//blp/refdata")
        req = svc.createRequest("ReferenceDataRequest")
        req.append("securities", security)
        req.append("fields", field)
        if overrides:
            ovr = req.getElement("overrides")
            for pair in overrides.split(";"):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    o = ovr.appendElement()
                    o.setElement("fieldId", k.strip())
                    o.setElement("value", v.strip())
        for msg in _send_and_collect(session, req):
            data = msg.getElement("securityData")
            item = data.getValueAsElement(0)
            fd = item.getElement("fieldData")
            try:
                bulk = fd.getElement(field)
                rows = []
                for i in range(bulk.numValues()):
                    rows.append(_element_to_dict(bulk.getValueAsElement(i)))
                if format == "csv":
                    return _rows_to_csv(rows, f"bds_{field.lower()}.csv")
                return {"security": security, "field": field, "count": len(rows), "results": rows}
            except Exception:
                return {"security": security, "field": field, "count": 0, "results": []}
        return {"security": security, "field": field, "count": 0, "results": []}
    except Exception as e:
        raise HTTPException(500, detail=_safe_error(e))
    finally:
        session.stop()


# ── Intraday Bars ─────────────────────────────────────────────────────────────

@app.get("/intraday/bars", tags=["Intraday"])
def intraday_bars(
    request: Request,
    security: str = Query(..., description="Ticker, e.g. AAPL US Equity"),
    event_type: str = Query("TRADE", description="TRADE, BID, ASK, BID_BEST, ASK_BEST"),
    interval: int = Query(5, description="Bar interval in minutes (1–1440)", ge=1, le=1440),
    start_datetime: str = Query(..., description="ISO datetime: 2026-04-03T09:30:00"),
    end_datetime: Optional[str] = Query(None, description="ISO datetime (default: now UTC)"),
):
    """Intraday OHLCV bars. Requires Bloomberg intraday data subscription."""
    _check_bbg_rate(request)
    end = end_datetime or datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
    session = _get_session()
    try:
        svc = _open_service(session, "//blp/refdata")
        req = svc.createRequest("IntradayBarRequest")
        req.set("security", security)
        req.set("eventType", event_type)
        req.set("interval", interval)
        req.set("startDateTime", start_datetime)
        req.set("endDateTime", end)
        rows = []
        for msg in _send_and_collect(session, req, timeout_ms=20000):
            try:
                bars = msg.getElement("barData").getElement("barTickData")
                for i in range(bars.numValues()):
                    rows.append(_element_to_dict(bars.getValueAsElement(i)))
            except Exception:
                pass
        return {"security": security, "interval_min": interval, "count": len(rows), "results": rows}
    except Exception as e:
        raise HTTPException(500, detail=_safe_error(e))
    finally:
        session.stop()


# ── Intraday Ticks ────────────────────────────────────────────────────────────

@app.get("/intraday/ticks", tags=["Intraday"])
def intraday_ticks(
    request: Request,
    security: str = Query(..., description="Ticker, e.g. AAPL US Equity"),
    event_types: str = Query("TRADE", description="Comma-separated event types: TRADE,BID,ASK"),
    start_datetime: str = Query(..., description="ISO datetime: 2026-04-03T09:30:00"),
    end_datetime: Optional[str] = Query(None, description="ISO datetime (default: now UTC)"),
    max_ticks: int = Query(500, le=5000),
):
    """Raw tick data for a security."""
    _check_bbg_rate(request)
    end = end_datetime or datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
    session = _get_session()
    try:
        svc = _open_service(session, "//blp/refdata")
        req = svc.createRequest("IntradayTickRequest")
        req.set("security", security)
        for et in event_types.split(","):
            req.append("eventTypes", et.strip())
        req.set("startDateTime", start_datetime)
        req.set("endDateTime", end)
        req.set("includeConditionCodes", True)
        rows = []
        for msg in _send_and_collect(session, req, timeout_ms=20000):
            try:
                ticks = msg.getElement("tickData").getElement("tickData")
                for i in range(min(ticks.numValues(), max_ticks)):
                    rows.append(_element_to_dict(ticks.getValueAsElement(i)))
            except Exception:
                pass
        return {"security": security, "count": len(rows), "results": rows}
    except Exception as e:
        raise HTTPException(500, detail=_safe_error(e))
    finally:
        session.stop()


# ── Field Search ──────────────────────────────────────────────────────────────

@app.get("/fields/search", tags=["Field Catalog"])
def field_search(
    request: Request,
    query: str = Query(..., description="Search term, e.g. 'earnings per share'"),
    max_results: int = Query(20, le=100),
):
    """Search Bloomberg field catalog by keyword (like FLDS <GO>)."""
    _check_bbg_rate(request)
    session = _get_session()
    try:
        svc = _open_service(session, "//blp/apiflds")
        req = svc.createRequest("FieldSearchRequest")
        req.set("searchSpec", query)
        req.getElement("include").setElement("fieldType", "All")
        results = []
        for msg in _send_and_collect(session, req, timeout_ms=15000):
            try:
                fld_data = msg.getElement("fieldData")
                for i in range(min(fld_data.numValues(), max_results)):
                    item = fld_data.getValueAsElement(i)
                    row = _element_to_dict(item)
                    results.append(row)
            except Exception:
                pass
        if not results:
            return {"results": [], "note": "//blp/apiflds returned no results. This service may require a Bloomberg Field Catalog (FLDS) subscription. Use the Terminal: FLDS <GO>"}
        return {"results": results}
    except Exception as e:
        raise HTTPException(500, detail=_safe_error(e))
    finally:
        session.stop()


# ── Field Info ────────────────────────────────────────────────────────────────

@app.get("/fields/info", tags=["Field Catalog"])
def field_info(
    request: Request,
    fields: str = Query(..., description="Comma-separated field mnemonics, e.g. PX_LAST,PE_RATIO"),
):
    """Get metadata for specific Bloomberg fields."""
    _check_bbg_rate(request)
    flds = [f.strip() for f in fields.split(",") if f.strip()]
    session = _get_session()
    try:
        svc = _open_service(session, "//blp/apiflds")
        req = svc.createRequest("FieldInfoRequest")
        for f in flds:
            req.append("id", f)  # FieldInfoRequest uses "id" not "fields"
        req.set("returnFieldDocumentation", True)
        results = []
        for msg in _send_and_collect(session, req, timeout_ms=15000):
            try:
                fd = msg.getElement("fieldData")
                for i in range(fd.numValues()):
                    results.append(_element_to_dict(fd.getValueAsElement(i)))
            except Exception:
                pass
        if not results:
            return {"results": [], "note": "//blp/apiflds returned no results. This service may require a Bloomberg Field Catalog subscription. Use the Terminal: FLDS <GO>"}
        return {"results": results}
    except Exception as e:
        raise HTTPException(500, detail=_safe_error(e))
    finally:
        session.stop()


# ── Security Lookup ───────────────────────────────────────────────────────────

@app.get("/security/lookup", tags=["Reference Data"])
def security_lookup(
    request: Request,
    query: str = Query(..., description="Search string, e.g. 'Apple'"),
    max_results: int = Query(10, le=50),
    yellow_key_filter: Optional[str] = Query(None, description="Equity, Bond, Curncy, Index, Comdty, Govt, Mtge, Muni"),
):
    """Search for securities by name or identifier (like SECF <GO>)."""
    _check_bbg_rate(request)
    YK = {'EQUITY':2,'GOVT':6,'BOND':7,'CORP':7,'INDEX':8,'CURNCY':9,'CURR':9,
          'COMDTY':1,'CMDT':1,'MTGE':10,'MUNI':3,'PFD':4}
    session = _get_session()
    try:
        svc = _open_service(session, "//blp/instruments")
        req = svc.createRequest("instrumentListRequest")
        req.set("query", query)
        req.set("maxResults", max_results)
        if yellow_key_filter:
            yk_int = YK.get(yellow_key_filter.upper(), 0)
            req.set("yellowKeyFilter", yk_int)
        results = []
        for msg in _send_and_collect(session, req, timeout_ms=10000):
            try:
                inst = msg.getElement("results")
                for i in range(inst.numValues()):
                    item = inst.getValueAsElement(i)
                    results.append({
                        "security": _get_str(item, "security"),
                        "description": _get_str(item, "description"),
                    })
            except Exception:
                pass
        return {"results": results}
    except Exception as e:
        raise HTTPException(500, detail=_safe_error(e))
    finally:
        session.stop()


# ── BQL helpers ───────────────────────────────────────────────────────────────

def _parse_bql(query: str):
    """
    Parse a BQL expression into (field, overrides_dict, securities_list).
    Handles: get(FIELD(K=V,...)) for(['SEC1','SEC2',...])
    Returns None for any component that can't be parsed.
    """
    field, overrides, securities = None, {}, []
    # Extract field and its overrides from get(FIELD(K=V,...))
    # Use balanced matching to handle nested parens like range(-3Q,0Q)
    m = _re.search(r'get\((\w+)\((.+)\)\s*\)\s*for', query, _re.IGNORECASE)
    if m:
        field = m.group(1)
        for kv in m.group(2).split(','):
            kv = kv.strip()
            if '=' in kv:
                k, v = kv.split('=', 1)
                overrides[k.strip()] = v.strip()
    # Extract securities from for([...])
    sec_m = _re.findall(r"'([^']+)'", query)
    securities = sec_m
    return field, overrides, securities


def _bql_overrides_to_bdp(overrides: dict) -> str:
    """
    Convert BQL fiscal overrides to BDP override string.
    BQL FPT=Q,FPO=0Q  →  BDP PERIODICITY_SELECTION=QUARTERLY
    BQL FPO=-1Q        →  BDP EQY_FUND_PERIOD_OFFSET=-1
    """
    parts = []
    fpt = overrides.get('FPT', 'Q').upper()
    fpo = overrides.get('FPO', '0Q')

    if fpt == 'Q':
        parts.append('PERIODICITY_SELECTION=QUARTERLY')
    elif fpt == 'A':
        parts.append('PERIODICITY_SELECTION=YEARLY')
    elif fpt == 'S':
        parts.append('PERIODICITY_SELECTION=SEMI_ANNUALLY')

    # Parse period offset: 0Q → 0, -1Q → -1, -2Q → -2
    offset_m = _re.match(r'^(-?\d+)[QA]$', fpo)
    if offset_m:
        offset = int(offset_m.group(1))
        if offset != 0:
            parts.append(f'EQY_FUND_PERIOD_OFFSET={offset}')

    if overrides.get('CURRENCY'):
        parts.append(f'EQY_FUND_CRNCY={overrides["CURRENCY"]}')

    return ';'.join(parts)


# ── BQL endpoint ───────────────────────────────────────────────────────────────

@app.get("/bql", tags=["BQL"])
def bql_query(
    request: Request,
    query: str = Query(..., description=(
        "BQL expression. e.g. "
        "get(NUMBER_OF_VEHICLES_SOLD(FPT=Q,FPO=0Q,ACT_EST_MAPPING=PRECISE,FS=MRC)) "
        "for(['TSLA US Equity'])"
    )),
):
    """
    Bloomberg Query Language — runs via //blp/bql if licensed, otherwise falls back
    to BDP with equivalent fiscal period overrides. Use for KPI/operational data.
    """
    _check_bbg_rate(request)
    field, overrides, securities = _parse_bql(query)

    # ── Attempt 1: native //blp/bql service ──────────────────────────────────
    session = _get_session()
    bql_svc = None
    try:
        opened = session.openService("//blp/bql")   # returns True/False
        if opened:
            bql_svc = session.getService("//blp/bql")
    except Exception:
        pass

    if bql_svc is not None:
        try:
            req = bql_svc.createRequest("sendQuery")
            req.set("expression", query)
            results, columns = [], []
            for msg in _send_and_collect(session, req, timeout_ms=30000):
                try:
                    if msg.hasElement("results"):
                        res_el = msg.getElement("results")
                        for ci in range(res_el.numValues()):
                            col = res_el.getValueAsElement(ci)
                            col_name = _get_str(col, "name") or f"col_{ci}"
                            if col_name not in columns:
                                columns.append(col_name)
                            if col.hasElement("values"):
                                vals_el = col.getElement("values")
                                for ri in range(vals_el.numValues()):
                                    val_el = vals_el.getValueAsElement(ri)
                                    while len(results) <= ri:
                                        results.append({})
                                    results[ri][col_name] = _element_to_python(val_el)
                    else:
                        results.append(_element_to_dict(msg))
                except Exception as ex:
                    results.append({"_parse_error": str(ex)})
            session.stop()
            return {"source": "bql", "query": query, "columns": columns,
                    "count": len(results), "results": results}
        except Exception as e:
            session.stop()
            raise HTTPException(500, detail=_safe_error(e))

    # BQL service not available — fall through to BDP
    session.stop()

    # ── Attempt 2: BDP fallback with fiscal overrides ─────────────────────────
    if field and securities:
        bdp_overrides = _bql_overrides_to_bdp(overrides) or None
        try:
            result = bdp(
                request=request,
                securities=",".join(securities),
                fields=field,
                overrides=bdp_overrides,
            )
            # Tag the response so the UI knows it came from BDP fallback
            result["source"] = "bdp_fallback"
            result["note"] = (
                f"//blp/bql service is not licensed on this Terminal. "
                f"Returned via BDP with fiscal overrides: {bdp_overrides or 'none'}. "
                f"Some KPI fields (vehicle deliveries, subscribers, etc.) may return null — "
                f"those require the Bloomberg BQL API license or the Excel Add-in."
            )
            result["bql_query"] = query
            return result
        except Exception:
            # BDP fallback also failed — field is likely BQL-only.
            # Return a structured response instead of an error.
            excel_formula = (
                f'=BQL("{securities[0]}","{field}"'
                + "".join(f',"{k}={v}"' for k, v in overrides.items())
                + ')'
            )
            return {
                "source": "bql_unavailable",
                "query": query,
                "field": field,
                "securities": securities,
                "results": [],
                "note": (
                    f"//blp/bql service is not licensed and the field '{field}' "
                    f"is not available via BDP. This is a BQL-only field."
                ),
                "excel_formula": excel_formula,
                "alternatives": [
                    f"Use the Excel formula: {excel_formula}",
                    "The =BQL() function works in Bloomberg Excel Add-in with an active Terminal session",
                    "Contact Bloomberg to enable BQL API access on your account",
                ],
            }

    # ── Neither worked ────────────────────────────────────────────────────────
    raise HTTPException(503, detail={
        "error": "//blp/bql service is not available on this Bloomberg Terminal installation.",
        "reason": "BQL requires a separate Bloomberg BQL API license beyond standard Terminal access.",
        "alternatives": [
            "Use the Excel formula shown in the Excel tab — =BQL() works if you have Bloomberg Excel Add-in",
            "Contact Bloomberg sales to enable BQL API access on your account",
            "Some fields may be available via BDP with overrides — try the /fields/search endpoint",
        ],
        "excel_formula": (
            f'=BQL("{securities[0] if securities else "<TICKER>"}","{field or "<FIELD>"}","FPT=Q","FPO=0Q","ACT_EST_MAPPING=PRECISE","FS=MRC","CURRENCY=USD","XLFILL=b")'
            if field else None
        ),
    })


# ── Yield Curve ───────────────────────────────────────────────────────────────

CURVE_TICKERS = {
    "USD": ["USGG1M Index","USGG3M Index","USGG6M Index","USGG1YR Index",
            "USGG2YR Index","USGG3YR Index","USGG5YR Index","USGG7YR Index",
            "USGG10YR Index","USGG20YR Index","USGG30YR Index"],
    "EUR": ["GDBR2Y Index","GDBR5Y Index","GDBR10Y Index","GDBR30Y Index"],
    "GBP": ["GUKG2Y Index","GUKG5Y Index","GUKG10Y Index","GUKG30Y Index"],
    "JPY": ["JGBS2 Index","JGBS5 Index","JGBS10 Index","JGBS30 Index"],
}

@app.get("/curve", tags=["Fixed Income"])
def curve(
    request: Request,
    curve_id: str = Query("USD", description="USD, EUR, GBP, JPY"),
    date: Optional[str] = Query(None, description="YYYYMMDD (default: today)"),
):
    """Bloomberg yield curve — sovereign rates by tenor."""
    _check_bbg_rate(request)
    tickers = CURVE_TICKERS.get(curve_id.upper())
    if not tickers:
        raise HTTPException(400, detail=f"Unknown curve_id '{curve_id}'. Available: {list(CURVE_TICKERS)}")
    ovr = f"REFERENCE_DATE={date.replace('-','')}" if date else None
    return bdp(request=request, securities=",".join(tickers), fields="PX_LAST,SECURITY_DES", overrides=ovr)


# ── AI Chat ───────────────────────────────────────────────────────────────────

class ConfigRequest(BaseModel):
    ANTHROPIC_API_KEY: Optional[str] = None
    OPENAI_API_KEY: Optional[str] = None
    GOOGLE_API_KEY: Optional[str] = None

_CFG_DIR = os.environ.get("CONFIG_DIR", os.path.dirname(os.path.abspath(__file__)))

# Provider definitions: env-var name, config-file key, default model, API base URL
AI_PROVIDERS = {
    "anthropic": {
        "env_key": "ANTHROPIC_API_KEY",
        "default_model": "claude-sonnet-4-6",
        "base_url": "https://api.anthropic.com",
    },
    "openai": {
        "env_key": "OPENAI_API_KEY",
        "default_model": "gpt-5.4",
        "base_url": "https://api.openai.com",
    },
    "google": {
        "env_key": "GOOGLE_API_KEY",
        "default_model": "gemini-2.5-pro",
        "base_url": "https://generativelanguage.googleapis.com",
    },
}

def _load_cfg():
    cfg_path = os.path.join(_CFG_DIR, "config.json")
    try:
        with open(cfg_path) as f:
            return json.load(f)
    except Exception:
        return {}

def _save_cfg(data):
    cfg_path = os.path.join(_CFG_DIR, "config.json")
    with open(cfg_path, "w") as f:
        json.dump(data, f)
    # Restrict permissions to owner-only (ignored on Windows)
    try:
        os.chmod(cfg_path, 0o600)
    except OSError:
        pass

def _get_api_key(provider: str) -> str:
    """Get API key for provider: env var → config file."""
    info = AI_PROVIDERS.get(provider, AI_PROVIDERS["anthropic"])
    key = os.environ.get(info["env_key"]) or ""
    if not key:
        key = _load_cfg().get(info["env_key"], "")
    return key

@app.post("/config", tags=["System"])
def set_config(req: ConfigRequest):
    """Save API keys to config.json."""
    try:
        existing = _load_cfg()
        if req.ANTHROPIC_API_KEY is not None:
            existing["ANTHROPIC_API_KEY"] = req.ANTHROPIC_API_KEY
        if req.OPENAI_API_KEY is not None:
            existing["OPENAI_API_KEY"] = req.OPENAI_API_KEY
        if req.GOOGLE_API_KEY is not None:
            existing["GOOGLE_API_KEY"] = req.GOOGLE_API_KEY
        _save_cfg(existing)
        return {"status": "saved"}
    except Exception as e:
        raise HTTPException(500, detail=_safe_error(e))

@app.get("/config", tags=["System"])
def get_config():
    """Check which API keys are configured per provider."""
    cfg = _load_cfg()
    providers = {}
    for name, info in AI_PROVIDERS.items():
        has_env = bool(os.environ.get(info["env_key"]))
        has_file = bool(cfg.get(info["env_key"]))
        providers[name] = {
            "key_set": has_env or has_file,
            "source": "env" if has_env else ("file" if has_file else "none"),
            "default_model": info["default_model"],
        }
    # Legacy fields for backward compat
    anth = providers.get("anthropic", {})
    return {
        "anthropic_key_set": anth.get("key_set", False),
        "source": anth.get("source", "none"),
        "providers": providers,
    }


class ChatMessage(BaseModel):
    role: str
    content: str

    @field_validator("content")
    @classmethod
    def content_max_length(cls, v):
        if len(v) > 10000:
            raise ValueError("Message content exceeds 10,000 character limit")
        return v

    @property
    def safe_role(self):
        """Only allow 'user' and 'assistant' roles to prevent prompt injection."""
        return self.role if self.role in ("user", "assistant") else "user"

MAX_CHAT_MESSAGES = 30  # Server-side cap on conversation length

class ChatRequest(BaseModel):
    messages: List[ChatMessage]
    provider: Optional[str] = None   # anthropic | openai | google
    model: Optional[str] = None      # override default model

    @field_validator("messages")
    @classmethod
    def limit_messages(cls, v):
        if len(v) > MAX_CHAT_MESSAGES:
            v = v[-MAX_CHAT_MESSAGES:]
        return v

BBG_BASE_URL = os.environ.get("BBG_BASE_URL", "")
OPENBB_BASE_URL = os.environ.get("OPENBB_BASE_URL", "")

CHAT_SYSTEM = """You are an expert Bloomberg Terminal and OpenBB API assistant embedded in a web playground. You help users get REAL data from a live Bloomberg Terminal via tool calls.

## ABSOLUTE RULES — these override everything else and your prior training:

1. **NEVER fabricate or guess data values.** Do NOT include any specific number, date, price, EPS, market cap, subscriber count, vehicle delivery figure, store count, or any other factual quantity in your response unless it came from a tool call you made in THIS conversation. Saying "TSLA delivered ~466,000 vehicles in Q3" is FORBIDDEN unless a tool you just called returned that exact number. Users put your output into financial analysis pipelines — fabricated values are dangerous.

2. **NEVER invent Bloomberg field mnemonics.** Bloomberg has tens of thousands of fields and most plausible-sounding names (NUMBER_OF_VEHICLES_SOLD, RETAIL_SUBSCRIBERS, UNIT_SALES_IPHONE, MONTHLY_ACTIVE_USERS, etc.) do NOT exist or return wrong data. Before mentioning ANY field that is not in the verified short list below, you MUST call `fields_search` to confirm it exists. If `fields_search` returns no real match, tell the user the field is not available — do not propose a guess.

3. **Use tools — don't just describe them.** When the user asks for actual data ("what's AAPL trading at?", "TSLA's revenue last quarter", "SPX members"), call the appropriate tool (`bdp`, `bdh`, `bds`, `bql`, `security_lookup`) and return the real result. Don't just hand the user a URL and stop.

4. **If a tool returns null, an empty result, or `[error: ...]`, report that honestly.** Do not substitute a value from your training data. Say: "Bloomberg returned no value for FIELD on SECURITY — this field may not be licensed on this Terminal, may not exist, or may not apply to this security. Try /fields/search to find an alternative."

5. **Always %20-encode spaces in URLs you write out.** "TSLA US Equity" → "TSLA%20US%20Equity".

6. **For ambiguous tickers, call `security_lookup` first.** If the user says "Tesla" or "Apple", confirm the canonical ticker (TSLA US Equity, AAPL US Equity) before querying.

## Tools you can call (call them — don't just describe them):

- `bdp(securities, fields, overrides?)` — current/reference values. Real-time prices, fundamentals, ratios.
- `bdh(securities, fields, start_date, end_date?, periodicity?)` — historical time series. Dates: YYYY-MM-DD.
- `bds(security, field, overrides?)` — bulk/array data (INDX_MEMBERS, DVD_HIST_ALL, BOARD_OF_DIRECTORS, EARN_ANN_DT_AND_EPS, OPT_EXPIRE_DT).
- `bql(query)` — Bloomberg Query Language. Returns 503 if not licensed; tool result will say so.
- `fields_search(query)` — search Bloomberg field catalog. **Use this before mentioning any non-standard field.**
- `fields_info(fields)` — get metadata for known fields.
- `security_lookup(query, yellow_key_filter?)` — find canonical ticker for a name. yellow_key_filter: Equity|Bond|Curncy|Index|Comdty|Govt.
- `curve(curve_id)` — sovereign yield curve. curve_id: USD|EUR|GBP|JPY.

## Verified Bloomberg fields you may use without searching first:
PX_LAST, PX_BID, PX_ASK, PX_OPEN, PX_HIGH, PX_LOW, PX_VOLUME, PX_MID, CHG_PCT_1D,
NAME, SECURITY_DES, MARKET_CAP, PE_RATIO, PX_TO_BOOK_RATIO, CURRENT_EV_TO_EBITDA,
SALES_REV_TURN, EBITDA, NET_INCOME, IS_EPS_DILUTED, RETURN_ON_EQY, NET_MARGIN,
FREE_CASH_FLOW, BS_TOT_ASSET, TOT_EQUITY, CF_CASH_FROM_OPER, CURR_ENTP_VAL,
TOT_RETURN_INDEX_GROSS_DVDS, YLD_YTM_MID, DUR_MID

For any other field — including ALL operational/KPI fields (deliveries, subscribers, units sold, store count, occupancy, etc.) — call `fields_search` FIRST.

## Bloomberg ticker format reference:
- Equities: "AAPL US Equity", "TSLA US Equity", "7203 JP Equity"
- Indices: "SPX Index", "CCMP Index", "INDU Index", "UKX Index", "NKY Index"
- FX: "EURUSD Curncy", "USDJPY Curncy", "GBPUSD Curncy"
- Fixed Income: "USGG10YR Index" (10Y yield), "US912828Z864 Govt"
- Commodities: "GC1 Comdty" (Gold), "CL1 Comdty" (WTI Oil), "CO1 Comdty" (Brent)
- Rates: "SOFRRATE Index", "FEDL01 Index", "USGG3M Index"

## OpenBB API endpoints (no tool wrapper — give the user the URL to run):
- GET {openbb_base}/api/v1/equity/price/historical?symbol=AAPL&start_date=YYYY-MM-DD&provider=yfinance
- GET {openbb_base}/api/v1/equity/price/quote?symbol=AAPL&provider=yfinance
- GET {openbb_base}/api/v1/equity/profile?symbol=AAPL&provider=yfinance
- GET {openbb_base}/api/v1/equity/fundamental/income?symbol=AAPL&period=annual&provider=fmp
- GET {openbb_base}/api/v1/equity/fundamental/balance?symbol=AAPL&period=annual&provider=fmp
- GET {openbb_base}/api/v1/equity/fundamental/cash?symbol=AAPL&period=annual&provider=fmp
- GET {openbb_base}/api/v1/equity/fundamental/ratios?symbol=AAPL&period=annual&provider=fmp
- GET {openbb_base}/api/v1/equity/estimates/price_target?symbol=AAPL&provider=fmp
- GET {openbb_base}/api/v1/fixedincome/government/yield_curve?date=YYYY-MM-DD&provider=fred
- GET {openbb_base}/api/v1/fixedincome/rate/sofr?start_date=YYYY-MM-DD&provider=fred
- GET {openbb_base}/api/v1/currency/price/historical?symbol=EURUSD&start_date=YYYY-MM-DD&provider=fmp
- GET {openbb_base}/api/v1/economy/cpi?countries=united_states&frequency=monthly&provider=fred
- GET {openbb_base}/api/v1/economy/gdp/real?start_date=YYYY-MM-DD&provider=oecd
- GET {openbb_base}/api/v1/derivatives/options/chains?symbol=AAPL&provider=cboe

## Excel formula equivalents (provide AFTER you have real tool results, so the user can re-run in their own spreadsheet):
- BDP: =BDP("AAPL US Equity","PX_LAST")
- BDH (array, Ctrl+Shift+Enter): =BDH("AAPL US Equity","PX_LAST","20250101","20260103","periodicitySelection=DAILY")
- BDS (array): =BDS("SPX Index","INDX_MEMBERS")
- BQL: =BQL("AAPL US Equity","FIELD_NAME","FPT=Q","FPO=0Q") — only after confirming the field via fields_search.

## Workflow for a typical user question:

STEP 1 — If the user gives a company name not a ticker → call `security_lookup` first.
STEP 2 — Identify the right tool (bdp for current, bdh for historical, bds for bulk, bql for fiscal/KPI).
STEP 3 — If the field isn't in the verified list above → call `fields_search` first.
STEP 4 — Call the data tool. Report the EXACT value the tool returned (or honestly report null/error).
STEP 5 — Provide the equivalent Excel formula so the user can re-run it themselves.

Be concise. Today's date is """ + datetime.date.today().isoformat() + """.

Bloomberg API base: {bbg_base}
OpenBB API base: {openbb_base}
"""


CHAT_SYSTEM = (CHAT_SYSTEM
    .replace("{bbg_base}", BBG_BASE_URL)
    .replace("{openbb_base}", OPENBB_BASE_URL)
)


# ── Chat tools (real Bloomberg execution, no fabrication) ────────────────────
# Canonical tool catalogue. Each entry is converted to the per-provider schema
# in the streaming helpers below.
CHAT_TOOL_DEFS = [
    {
        "name": "bdp",
        "description": "Bloomberg Data Point - current/reference values for one or more securities and fields. Use for live quotes, names, fundamentals, ratios.",
        "params": {
            "securities": {"type": "string", "description": "Comma-separated tickers like 'AAPL US Equity,MSFT US Equity'"},
            "fields":     {"type": "string", "description": "Comma-separated field mnemonics like 'PX_LAST,MARKET_CAP'"},
            "overrides":  {"type": "string", "description": "Optional. Semicolon-separated key=value overrides, e.g. 'PRICING_SOURCE=BGN'"},
        },
        "required": ["securities", "fields"],
    },
    {
        "name": "bdh",
        "description": "Bloomberg Data History - historical time series for one or more securities. start_date/end_date in YYYY-MM-DD.",
        "params": {
            "securities":  {"type": "string", "description": "Comma-separated tickers"},
            "fields":      {"type": "string", "description": "Comma-separated field mnemonics, default PX_LAST"},
            "start_date":  {"type": "string", "description": "YYYY-MM-DD"},
            "end_date":    {"type": "string", "description": "YYYY-MM-DD (default: today)"},
            "periodicity": {"type": "string", "description": "DAILY, WEEKLY, MONTHLY, QUARTERLY, SEMI_ANNUALLY, YEARLY (default DAILY)"},
            "currency":    {"type": "string", "description": "Optional currency override e.g. USD"},
        },
        "required": ["securities", "fields", "start_date"],
    },
    {
        "name": "bds",
        "description": "Bloomberg Data Set - bulk/array data for a single security. Examples: INDX_MEMBERS, DVD_HIST_ALL, BOARD_OF_DIRECTORS, EARN_ANN_DT_AND_EPS, OPT_EXPIRE_DT.",
        "params": {
            "security":  {"type": "string", "description": "Single ticker, e.g. 'SPX Index'"},
            "field":     {"type": "string", "description": "Bulk field mnemonic"},
            "overrides": {"type": "string", "description": "Optional semicolon-separated overrides"},
        },
        "required": ["security", "field"],
    },
    {
        "name": "bql",
        "description": "Bloomberg Query Language - operational/KPI/fiscal data not available via BDP. Returns 503 with structured fallback if not licensed on this Terminal.",
        "params": {
            "query": {"type": "string", "description": "BQL expression, e.g. get(NUMBER_OF_VEHICLES_SOLD(FPT=Q,FPO=0Q)) for(['TSLA US Equity'])"},
        },
        "required": ["query"],
    },
    {
        "name": "fields_search",
        "description": "Search the Bloomberg field catalog. Call this BEFORE proposing any non-standard field name to confirm it actually exists.",
        "params": {
            "query": {"type": "string", "description": "Search term, e.g. 'subscribers' or 'iphone units'"},
        },
        "required": ["query"],
    },
    {
        "name": "fields_info",
        "description": "Get description and metadata for one or more known Bloomberg fields.",
        "params": {
            "fields": {"type": "string", "description": "Comma-separated field mnemonics"},
        },
        "required": ["fields"],
    },
    {
        "name": "security_lookup",
        "description": "Look up the canonical Bloomberg ticker for a company name or partial ticker. Use this when the user gives a name without a yellow key.",
        "params": {
            "query":             {"type": "string", "description": "Company name or partial ticker, e.g. 'Tesla'"},
            "yellow_key_filter": {"type": "string", "description": "Optional: Equity, Bond, Curncy, Index, Comdty, Govt"},
        },
        "required": ["query"],
    },
    {
        "name": "curve",
        "description": "Sovereign yield curve snapshot for USD, EUR, GBP, or JPY.",
        "params": {
            "curve_id": {"type": "string", "description": "USD, EUR, GBP, or JPY"},
            "date":     {"type": "string", "description": "Optional YYYYMMDD; default today"},
        },
        "required": ["curve_id"],
    },
]

def _tool_paths():
    return {
        "bdp": "/bdp", "bdh": "/bdh", "bds": "/bds", "bql": "/bql",
        "fields_search": "/fields/search", "fields_info": "/fields/info",
        "security_lookup": "/security/lookup", "curve": "/curve",
    }

def _tools_anthropic():
    out = []
    for t in CHAT_TOOL_DEFS:
        out.append({
            "name": t["name"],
            "description": t["description"],
            "input_schema": {
                "type": "object",
                "properties": t["params"],
                "required": t["required"],
            },
        })
    return out

def _tools_openai():
    out = []
    for t in CHAT_TOOL_DEFS:
        out.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": {
                    "type": "object",
                    "properties": t["params"],
                    "required": t["required"],
                },
            },
        })
    return out

def _tools_google():
    decls = []
    for t in CHAT_TOOL_DEFS:
        decls.append({
            "name": t["name"],
            "description": t["description"],
            "parameters": {
                "type": "object",
                "properties": t["params"],
                "required": t["required"],
            },
        })
    return [{"functionDeclarations": decls}]

CHAT_TOOLS_ANTHROPIC = _tools_anthropic()
CHAT_TOOLS_OPENAI    = _tools_openai()
CHAT_TOOLS_GOOGLE    = _tools_google()

_TOOL_RESULT_MAX_CHARS = 8000  # cap per tool result returned to the model
_MAX_TOOL_TURNS = 6            # safety cap on multi-turn tool loops

async def _exec_tool(name: str, input_data: dict) -> str:
    """Execute a chat tool by calling our own loopback API. Returns a JSON string.

    All real Bloomberg data flows through this; the model is forbidden from
    inventing values, so every quoted number must originate from a tool result.
    """
    import httpx
    paths = _tool_paths()
    path = paths.get(name)
    if not path:
        return json.dumps({"_error": f"unknown tool: {name}"})
    params = {k: v for k, v in (input_data or {}).items() if v not in (None, "")}
    base_port = os.environ.get("API_PORT", "8195")
    base = f"http://127.0.0.1:{base_port}"
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            r = await client.get(f"{base}{path}", params=params)
        try:
            payload = r.json()
        except Exception:
            payload = {"_status": r.status_code, "_raw": r.text[:500]}
        if r.status_code >= 400:
            wrap = {"_status": r.status_code}
            if isinstance(payload, dict):
                wrap.update(payload)
            else:
                wrap["detail"] = payload
            payload = wrap
        out = json.dumps(payload, default=str)
        if len(out) > _TOOL_RESULT_MAX_CHARS:
            out = out[:_TOOL_RESULT_MAX_CHARS] + ' ...[truncated]'
        return out
    except Exception as e:
        return json.dumps({"_error": _safe_error(e)})


@app.post("/chat", tags=["AI Assistant"])
async def chat(request: Request, req: ChatRequest):
    """AI assistant that helps construct Bloomberg and OpenBB API calls.
    Supports Anthropic, OpenAI, and Google Gemini providers."""
    _check_chat_rate(request)
    import httpx

    provider = (req.provider or os.environ.get("AI_PROVIDER", "anthropic")).lower()
    if provider not in AI_PROVIDERS:
        raise HTTPException(400, detail=f"Unknown provider '{provider}'. Supported: {', '.join(AI_PROVIDERS)}")

    api_key = _get_api_key(provider)
    if not api_key:
        env_name = AI_PROVIDERS[provider]["env_key"]
        raise HTTPException(503, detail=f"No {env_name} found. Enter it in the playground settings (⚙).")

    info = AI_PROVIDERS[provider]
    model = req.model or os.environ.get("AI_MODEL") or os.environ.get("ANTHROPIC_MODEL") or info["default_model"]
    messages = [{"role": m.safe_role, "content": m.content} for m in req.messages]

    if provider == "anthropic":
        generator = _stream_anthropic(api_key, model, messages)
    elif provider == "openai":
        generator = _stream_openai(api_key, model, messages)
    elif provider == "google":
        generator = _stream_google(api_key, model, messages)
    else:
        raise HTTPException(400, detail=f"Provider '{provider}' not implemented")

    return StreamingResponse(generator, media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def _stream_anthropic(api_key, model, messages):
    """Multi-turn tool-use loop against Anthropic Messages API.

    Streams text deltas to the client AND emits tool_use / tool_result SSE
    events so the UI can show what was actually called and what was returned.
    The model is FORBIDDEN by the system prompt from quoting values that did
    not come from a tool result in this conversation.
    """
    import httpx, json as _json
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    headers = {
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
        "x-api-key": api_key,
    }

    # Convert the inbound messages (string content) to Anthropic's content-block
    # format so we can append tool_use / tool_result blocks across turns.
    conv = [{"role": m["role"], "content": m["content"]} for m in messages]

    for turn in range(_MAX_TOOL_TURNS):
        body = {
            "model": model,
            "max_tokens": 2048,
            "system": CHAT_SYSTEM,
            "tools": CHAT_TOOLS_ANTHROPIC,
            "messages": conv,
            "stream": True,
        }
        blocks = {}      # index -> {"type": "text"|"tool_use", ...}
        stop_reason = None

        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream("POST", f"{base_url}/v1/messages",
                                     headers=headers, json=body) as resp:
                if resp.status_code != 200:
                    body_bytes = await resp.aread()
                    err = body_bytes.decode(errors="replace")[:500]
                    yield f"data: {_json.dumps({'type':'text','text': f'[Anthropic API error {resp.status_code}]: {err}'})}\n\n"
                    yield "data: [DONE]\n\n"
                    return

                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if not data_str:
                        continue
                    try:
                        ev = _json.loads(data_str)
                    except Exception:
                        continue
                    et = ev.get("type")
                    if et == "content_block_start":
                        idx = ev.get("index")
                        cb = ev.get("content_block", {})
                        if cb.get("type") == "text":
                            blocks[idx] = {"type": "text", "text": ""}
                        elif cb.get("type") == "tool_use":
                            blocks[idx] = {
                                "type": "tool_use",
                                "id":    cb.get("id"),
                                "name":  cb.get("name"),
                                "input_json": "",
                            }
                    elif et == "content_block_delta":
                        idx = ev.get("index")
                        delta = ev.get("delta", {})
                        if delta.get("type") == "text_delta":
                            text = delta.get("text", "")
                            if text and idx in blocks and blocks[idx]["type"] == "text":
                                blocks[idx]["text"] += text
                                yield f"data: {_json.dumps({'type':'text','text': text})}\n\n"
                        elif delta.get("type") == "input_json_delta":
                            if idx in blocks and blocks[idx]["type"] == "tool_use":
                                blocks[idx]["input_json"] += delta.get("partial_json", "")
                    elif et == "message_delta":
                        sr = ev.get("delta", {}).get("stop_reason")
                        if sr:
                            stop_reason = sr

        # End of one streamed turn — handle tool calls if any
        tool_blocks = [(idx, b) for idx, b in sorted(blocks.items())
                       if b.get("type") == "tool_use"]
        if not tool_blocks or stop_reason != "tool_use":
            yield "data: [DONE]\n\n"
            return

        # Append the assistant message in content-block form
        assistant_content = []
        for idx in sorted(blocks.keys()):
            b = blocks[idx]
            if b["type"] == "text" and b["text"]:
                assistant_content.append({"type": "text", "text": b["text"]})
            elif b["type"] == "tool_use":
                try:
                    tool_input = _json.loads(b["input_json"]) if b["input_json"] else {}
                except Exception:
                    tool_input = {}
                b["_parsed_input"] = tool_input
                assistant_content.append({
                    "type":  "tool_use",
                    "id":    b["id"],
                    "name":  b["name"],
                    "input": tool_input,
                })
        conv.append({"role": "assistant", "content": assistant_content})

        # Execute every tool, stream events, then append a single user turn
        # containing all tool_result blocks.
        tool_results = []
        for _idx, b in tool_blocks:
            yield f"data: {_json.dumps({'type':'tool_use','id':b['id'],'name':b['name'],'input':b.get('_parsed_input', {})})}\n\n"
            out_str = await _exec_tool(b["name"], b.get("_parsed_input", {}))
            yield f"data: {_json.dumps({'type':'tool_result','id':b['id'],'name':b['name'],'output':out_str[:2000]})}\n\n"
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": b["id"],
                "content": out_str,
            })
        conv.append({"role": "user", "content": tool_results})

    # Hit the turn cap — let the client know
    yield f"data: {_json.dumps({'type':'text','text': '[Tool-use turn limit reached; final answer truncated.]'})}\n\n"
    yield "data: [DONE]\n\n"


async def _stream_openai(api_key, model, messages):
    """Tool-use loop against OpenAI-compatible chat.completions.

    Uses non-streaming requests so we can cleanly assemble tool_calls between
    turns; final assistant text is delivered in chunks so the UI still feels
    incremental (most providers only batch a few KB of text per turn).
    """
    import httpx, json as _json
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com")
    headers = {
        "content-type": "application/json",
        "authorization": f"Bearer {api_key}",
    }
    conv = [{"role": "system", "content": CHAT_SYSTEM}]
    for m in messages:
        conv.append({"role": m["role"], "content": m["content"]})

    for turn in range(_MAX_TOOL_TURNS):
        body = {
            "model": model,
            "max_tokens": 2048,
            "messages": conv,
            "tools": CHAT_TOOLS_OPENAI,
            "tool_choice": "auto",
        }
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(f"{base_url}/v1/chat/completions",
                                  headers=headers, json=body)
        if r.status_code != 200:
            err = r.text[:500]
            yield f"data: {_json.dumps({'type':'text','text': f'[OpenAI API error {r.status_code}]: {err}'})}\n\n"
            yield "data: [DONE]\n\n"
            return
        try:
            data = r.json()
        except Exception:
            yield f"data: {_json.dumps({'type':'text','text': '[OpenAI: non-JSON response]'})}\n\n"
            yield "data: [DONE]\n\n"
            return

        choice = (data.get("choices") or [{}])[0]
        msg    = choice.get("message", {}) or {}
        content    = msg.get("content") or ""
        tool_calls = msg.get("tool_calls") or []
        finish     = choice.get("finish_reason")

        # Stream any text content in modest chunks
        if content:
            CHUNK = 80
            for k in range(0, len(content), CHUNK):
                yield f"data: {_json.dumps({'type':'text','text': content[k:k+CHUNK]})}\n\n"

        if not tool_calls or finish not in ("tool_calls", "function_call", None):
            yield "data: [DONE]\n\n"
            return

        # Persist the assistant turn including tool_calls
        conv.append({
            "role":      "assistant",
            "content":   content,
            "tool_calls": tool_calls,
        })

        # Execute every tool and append one tool message per call
        for tc in tool_calls:
            fn   = tc.get("function", {}) or {}
            name = fn.get("name", "")
            try:
                args = _json.loads(fn.get("arguments") or "{}")
            except Exception:
                args = {}
            yield f"data: {_json.dumps({'type':'tool_use','id':tc.get('id'),'name':name,'input':args})}\n\n"
            out_str = await _exec_tool(name, args)
            yield f"data: {_json.dumps({'type':'tool_result','id':tc.get('id'),'name':name,'output':out_str[:2000]})}\n\n"
            conv.append({
                "role":         "tool",
                "tool_call_id": tc.get("id"),
                "name":         name,
                "content":      out_str,
            })

    yield f"data: {_json.dumps({'type':'text','text': '[Tool-use turn limit reached; final answer truncated.]'})}\n\n"
    yield "data: [DONE]\n\n"


async def _stream_google(api_key, model, messages):
    """Tool-use loop against Google Gemini generateContent.

    Non-streaming per turn so we can cleanly assemble functionCall / functionResponse
    parts; final text is chunked back to the client.
    """
    import httpx, json as _json
    base_url = os.environ.get("GOOGLE_BASE_URL", "https://generativelanguage.googleapis.com")

    contents = []
    for m in messages:
        role = "user" if m["role"] == "user" else "model"
        contents.append({"role": role, "parts": [{"text": m["content"]}]})

    headers = {
        "content-type": "application/json",
        "x-goog-api-key": api_key,
    }

    for turn in range(_MAX_TOOL_TURNS):
        body = {
            "contents": contents,
            "systemInstruction": {"parts": [{"text": CHAT_SYSTEM}]},
            "tools": CHAT_TOOLS_GOOGLE,
            "generationConfig": {"maxOutputTokens": 2048},
        }
        url = f"{base_url}/v1beta/models/{model}:generateContent"
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(url, headers=headers, json=body)
        if r.status_code != 200:
            err = r.text[:500]
            yield f"data: {_json.dumps({'type':'text','text': f'[Google API error {r.status_code}]: {err}'})}\n\n"
            yield "data: [DONE]\n\n"
            return
        try:
            data = r.json()
        except Exception:
            yield f"data: {_json.dumps({'type':'text','text': '[Google: non-JSON response]'})}\n\n"
            yield "data: [DONE]\n\n"
            return

        cand  = (data.get("candidates") or [{}])[0]
        parts = ((cand.get("content") or {}).get("parts")) or []

        text_parts = []
        function_calls = []
        for p in parts:
            if "text" in p and p["text"]:
                text_parts.append(p["text"])
            elif "functionCall" in p:
                function_calls.append(p["functionCall"])

        # Stream any text
        joined = "".join(text_parts)
        if joined:
            CHUNK = 80
            for k in range(0, len(joined), CHUNK):
                yield f"data: {_json.dumps({'type':'text','text': joined[k:k+CHUNK]})}\n\n"

        if not function_calls:
            yield "data: [DONE]\n\n"
            return

        # Append model turn (with the function calls) to the conversation
        contents.append({"role": "model", "parts": parts})

        # Execute each call and append a single user turn with all function responses
        response_parts = []
        for fc in function_calls:
            name = fc.get("name", "")
            args = fc.get("args", {}) or {}
            yield f"data: {_json.dumps({'type':'tool_use','id':name,'name':name,'input':args})}\n\n"
            out_str = await _exec_tool(name, args)
            yield f"data: {_json.dumps({'type':'tool_result','id':name,'name':name,'output':out_str[:2000]})}\n\n"
            try:
                out_payload = _json.loads(out_str)
            except Exception:
                out_payload = {"raw": out_str}
            response_parts.append({
                "functionResponse": {
                    "name": name,
                    "response": {"content": out_payload},
                }
            })
        contents.append({"role": "user", "parts": response_parts})

    yield f"data: {_json.dumps({'type':'text','text': '[Tool-use turn limit reached; final answer truncated.]'})}\n\n"
    yield "data: [DONE]\n\n"


if __name__ == "__main__":
    import uvicorn
    api_host = os.environ.get("API_HOST", "127.0.0.1")
    api_port = int(os.environ.get("API_PORT", "8195"))
    uvicorn.run("bbg_api:app", host=api_host, port=api_port, reload=False, log_level="info")
