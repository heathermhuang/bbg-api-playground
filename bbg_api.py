"""
Bloomberg Terminal HTTP API wrapper
Exposes BDP, BDH, BDS, intraday bars, field catalog, and AI chat via REST.
"""

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, Response
from pydantic import BaseModel
from typing import Optional, List
import blpapi
import datetime
import os
import json
import csv
import io
import re as _re

# Try loading .env file from the same directory as this script
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

app = FastAPI(
    title="Bloomberg Terminal API",
    description="REST wrapper for Bloomberg Terminal (blpapi). BDP, BDH, BDS, intraday bars, field catalog, AI assistant.",
    version="1.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

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

@app.get("/health", tags=["System"])
def health():
    try:
        session = _get_session()
        session.stop()
        return {"status": "ok", "bloomberg_port": BBG_PORT, "blpapi_version": blpapi.version()}
    except Exception as e:
        raise HTTPException(503, detail=str(e))


# ── BDP ───────────────────────────────────────────────────────────────────────

@app.get("/bdp", tags=["Reference Data"])
def bdp(
    securities: str = Query(..., description="Comma-separated tickers, e.g. AAPL US Equity,MSFT US Equity"),
    fields: str = Query(..., description="Comma-separated fields, e.g. PX_LAST,NAME,MARKET_CAP"),
    overrides: Optional[str] = Query(None, description="Semicolon-separated key=value overrides, e.g. PRICING_SOURCE=BGN"),
    format: Optional[str] = Query(None, description="Response format: json (default) or csv"),
):
    """Bloomberg Data Point — current/reference field values for one or more securities."""
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
        raise HTTPException(500, detail=str(e))
    finally:
        session.stop()


# ── BDH ───────────────────────────────────────────────────────────────────────

@app.get("/bdh", tags=["Historical Data"])
def bdh(
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
        raise HTTPException(500, detail=str(e))
    finally:
        session.stop()


# ── BDS ───────────────────────────────────────────────────────────────────────

@app.get("/bds", tags=["Bulk Data"])
def bds(
    security: str = Query(..., description="Single ticker, e.g. SPX Index"),
    field: str = Query(..., description="Bulk field, e.g. INDX_MEMBERS, DVD_HIST_ALL"),
    overrides: Optional[str] = Query(None, description="Semicolon-separated key=value overrides"),
    format: Optional[str] = Query(None, description="Response format: json (default) or csv"),
):
    """Bloomberg Data Set — bulk/array fields like index members, dividend history."""
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
        raise HTTPException(500, detail=str(e))
    finally:
        session.stop()


# ── Intraday Bars ─────────────────────────────────────────────────────────────

@app.get("/intraday/bars", tags=["Intraday"])
def intraday_bars(
    security: str = Query(..., description="Ticker, e.g. AAPL US Equity"),
    event_type: str = Query("TRADE", description="TRADE, BID, ASK, BID_BEST, ASK_BEST"),
    interval: int = Query(5, description="Bar interval in minutes (1–1440)", ge=1, le=1440),
    start_datetime: str = Query(..., description="ISO datetime: 2026-04-03T09:30:00"),
    end_datetime: Optional[str] = Query(None, description="ISO datetime (default: now UTC)"),
):
    """Intraday OHLCV bars. Requires Bloomberg intraday data subscription."""
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
        raise HTTPException(500, detail=str(e))
    finally:
        session.stop()


# ── Intraday Ticks ────────────────────────────────────────────────────────────

@app.get("/intraday/ticks", tags=["Intraday"])
def intraday_ticks(
    security: str = Query(..., description="Ticker, e.g. AAPL US Equity"),
    event_types: str = Query("TRADE", description="Comma-separated event types: TRADE,BID,ASK"),
    start_datetime: str = Query(..., description="ISO datetime: 2026-04-03T09:30:00"),
    end_datetime: Optional[str] = Query(None, description="ISO datetime (default: now UTC)"),
    max_ticks: int = Query(500, le=5000),
):
    """Raw tick data for a security."""
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
        raise HTTPException(500, detail=str(e))
    finally:
        session.stop()


# ── Field Search ──────────────────────────────────────────────────────────────

@app.get("/fields/search", tags=["Field Catalog"])
def field_search(
    query: str = Query(..., description="Search term, e.g. 'earnings per share'"),
    max_results: int = Query(20, le=100),
):
    """Search Bloomberg field catalog by keyword (like FLDS <GO>)."""
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
        raise HTTPException(500, detail=str(e))
    finally:
        session.stop()


# ── Field Info ────────────────────────────────────────────────────────────────

@app.get("/fields/info", tags=["Field Catalog"])
def field_info(
    fields: str = Query(..., description="Comma-separated field mnemonics, e.g. PX_LAST,PE_RATIO"),
):
    """Get metadata for specific Bloomberg fields."""
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
        raise HTTPException(500, detail=str(e))
    finally:
        session.stop()


# ── Security Lookup ───────────────────────────────────────────────────────────

@app.get("/security/lookup", tags=["Reference Data"])
def security_lookup(
    query: str = Query(..., description="Search string, e.g. 'Apple'"),
    max_results: int = Query(10, le=50),
    yellow_key_filter: Optional[str] = Query(None, description="Equity, Bond, Curncy, Index, Comdty, Govt, Mtge, Muni"),
):
    """Search for securities by name or identifier (like SECF <GO>)."""
    # yellowKeyFilter integer enum: NONE=0 CMDT=1 EQUITY=2 MUNI=3 PFD=4 GOVT=6 CORP=7 INDEX=8 CURR=9 MTGE=10
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
        raise HTTPException(500, detail=str(e))
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
    m = _re.search(r'get\((\w+)\(([^)]*)\)\)', query, _re.IGNORECASE)
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
            raise HTTPException(500, detail=str(e))

    # BQL service not available — fall through to BDP
    session.stop()

    # ── Attempt 2: BDP fallback with fiscal overrides ─────────────────────────
    if field and securities:
        bdp_overrides = _bql_overrides_to_bdp(overrides) or None
        try:
            result = bdp(
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
        except HTTPException as e:
            raise e
        except Exception as e:
            raise HTTPException(500, detail=f"BDP fallback failed: {e}")

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
    curve_id: str = Query("USD", description="USD, EUR, GBP, JPY"),
    date: Optional[str] = Query(None, description="YYYYMMDD (default: today)"),
):
    """Bloomberg yield curve — sovereign rates by tenor."""
    tickers = CURVE_TICKERS.get(curve_id.upper())
    if not tickers:
        raise HTTPException(400, detail=f"Unknown curve_id '{curve_id}'. Available: {list(CURVE_TICKERS)}")
    ovr = f"REFERENCE_DATE={date.replace('-','')}" if date else None
    return bdp(securities=",".join(tickers), fields="PX_LAST,SECURITY_DES", overrides=ovr)


# ── AI Chat ───────────────────────────────────────────────────────────────────

class ConfigRequest(BaseModel):
    ANTHROPIC_API_KEY: str

_CFG_DIR = os.environ.get("CONFIG_DIR", os.path.dirname(os.path.abspath(__file__)))

@app.post("/config", tags=["System"])
def set_config(req: ConfigRequest):
    """Save API keys to config.json."""
    cfg_path = os.path.join(_CFG_DIR, "config.json")
    try:
        existing = {}
        try:
            with open(cfg_path) as f:
                existing = json.load(f)
        except Exception:
            pass
        existing["ANTHROPIC_API_KEY"] = req.ANTHROPIC_API_KEY
        with open(cfg_path, "w") as f:
            json.dump(existing, f)
        return {"status": "saved"}
    except Exception as e:
        raise HTTPException(500, detail=str(e))

@app.get("/config", tags=["System"])
def get_config():
    """Check which API keys are configured."""
    cfg_path = os.path.join(_CFG_DIR, "config.json")
    has_env = bool(os.environ.get("ANTHROPIC_API_KEY"))
    has_file = False
    try:
        with open(cfg_path) as f:
            has_file = bool(json.load(f).get("ANTHROPIC_API_KEY"))
    except Exception:
        pass
    return {"anthropic_key_set": has_env or has_file, "source": "env" if has_env else ("file" if has_file else "none")}


class ChatMessage(BaseModel):
    role: str
    content: str

    @property
    def safe_role(self):
        """Only allow 'user' and 'assistant' roles to prevent prompt injection."""
        return self.role if self.role in ("user", "assistant") else "user"

class ChatRequest(BaseModel):
    messages: List[ChatMessage]

BBG_BASE_URL = os.environ.get("BBG_BASE_URL", "")
OPENBB_BASE_URL = os.environ.get("OPENBB_BASE_URL", "")

CHAT_SYSTEM = """You are an expert Bloomberg Terminal and OpenBB API assistant embedded in a web playground.

## DECISION TREE — read this first before every response:

STEP 1 — Is the user asking for operational/KPI company data?
  Signals: deliveries, units sold/produced, subscribers, MAU/DAU, ARPU, same-store sales,
  store count, GMV, load factor, RevPAR, ADR, occupancy rate, backlog, churn, renewal rate,
  iPhone/Mac/iPad unit sales, vehicle deliveries, passenger miles, available seat miles.

  YES → Do NOT attempt BDP/BDH. Go directly to BQL. Say:
        "This data is not available via the Bloomberg API. Use Bloomberg Excel BQL:"
        Then give the exact =BQL() formula. Do not ask clarifying questions.
        Known BQL fields for common cases:
          vehicle deliveries / units delivered → NUMBER_OF_VEHICLES_SOLD
          subscriber count / paid subscribers → RETAIL_SUBSCRIBERS or PAID_SUBSCRIBERS
          streaming subscribers (NFLX, DIS) → STREAMING_SUBSCRIBERS
          monthly active users / MAU → MONTHLY_ACTIVE_USERS
          daily active users / DAU → DAILY_ACTIVE_USERS
          ARPU → AVERAGE_REVENUE_PER_USER
          iPhone units → UNIT_SALES_IPHONE
          Mac units → UNIT_SALES_MAC
          iPad units → UNIT_SALES_IPAD
          same-store / comp sales → SAME_STORE_SALES_GROWTH
          store count / locations → STORE_COUNT
          GMV → ECOMMERCE_GMV
          RevPAR → REVPAR
          hotel occupancy → HOTEL_OCCUPANCY_RATE
          ADR → ADR_HOTEL
          load factor → PASSENGER_LOAD_FACTOR
          backlog units → BACKLOG_UNITS
          churn / renewal → CHURN_RATE / RENEWAL_RATE
        IMPORTANT: The /bql endpoint requires a separate Bloomberg BQL API license.
        If the user's Terminal does not have it, the endpoint will automatically fall back to BDP
        with fiscal overrides (which may return null for true KPI fields).
        Always tell the user: "The /bql endpoint will try the BQL service first; if not licensed,
        it falls back to BDP. For guaranteed results, use the =BQL() Excel formula."

        Respond with BOTH:
        A) The /bql API URL (same base as other Bloomberg endpoints):
           {bbg_base}/bql?query=get(FIELD(FPT%3DQ%2CFPO%3D0Q%2CACT_EST_MAPPING%3DPRECISE%2CFS%3DMRC))%20for(%5B'TICKER%20US%20Equity'%5D)
           Note: the query parameter must be URL-encoded. Encode ( as %28, ) as %29, = as %3D, , as %2C, spaces as %20, [ as %5B, ] as %5D, ' as %27
        B) The equivalent =BQL() Excel formula (guaranteed to work with Bloomberg Excel Add-in)

        BQL query syntax for the API (unencoded form, then encode it):
          get(FIELD(FPT=Q,FPO=0Q,ACT_EST_MAPPING=PRECISE,FS=MRC)) for(['TICKER US Equity'])
        Multi-quarter history:
          get(FIELD(FPT=Q,FPO=range(-3Q,0Q),ACT_EST_MAPPING=PRECISE,FS=MRC)) for(['TICKER US Equity'])
        Multi-ticker:
          get(FIELD(FPT=Q,FPO=0Q,ACT_EST_MAPPING=PRECISE,FS=MRC)) for(['TSLA US Equity','NIO US Equity'])

  NO → Continue to Step 2.

STEP 2 — Do I have all required parameters?
  Check the endpoint's required params (listed below). If missing any → ASK. Do not generate URL.
  Exception: if parameter has a sensible default (periodicity=DAILY, end_date=today, provider=yfinance), apply it silently.

STEP 3 — Generate the response:
  1. Brief explanation (2-3 lines)
  2. URL — fully formed, spaces in ticker names MUST be %20-encoded (e.g. TSLA%20US%20Equity)
  3. curl example (use --data-urlencode so spaces are fine there)
  4. Excel formula block labeled ```Excel

## HARD RULES — never violate these:
- NEVER invent Bloomberg field names. Only use fields listed below or fields the user explicitly provides. If uncertain, use /fields/search and say so.
- ALWAYS %20-encode spaces in URL query parameter values. "TSLA US Equity" in a URL = "TSLA%20US%20Equity". Without encoding the URL breaks at the space.
- NEVER ask unnecessary questions when you already have enough information to generate a correct URL.

---

Your job: help the user construct exact API calls for either:

## Bloomberg Terminal API (base: {bbg_base})
- GET /bdp?securities=<tickers>&fields=<fields>[&overrides=<key=value;...>]
  BDP = current/reference data. e.g. ?securities=AAPL US Equity&fields=PX_LAST,PE_RATIO,MARKET_CAP
- GET /bdh?securities=<tickers>&fields=<fields>&start_date=<YYYY-MM-DD>[&end_date=...&periodicity=DAILY|WEEKLY|MONTHLY|QUARTERLY|YEARLY]
  BDH = historical time series
- GET /bds?security=<ticker>&field=<bulk_field>
  BDS = bulk data arrays. Common fields: INDX_MEMBERS, DVD_HIST_ALL, EARN_ANN_DT_AND_EPS, OPT_EXPIRE_DT, BOARD_OF_DIRECTORS
- GET /intraday/bars?security=<ticker>&event_type=TRADE&interval=<mins>&start_datetime=<ISO>&end_datetime=<ISO>
- GET /intraday/ticks?security=<ticker>&event_types=TRADE,BID,ASK&start_datetime=<ISO>&end_datetime=<ISO>
- GET /fields/search?query=<keyword>
- GET /fields/info?fields=<field1,field2>
- GET /security/lookup?query=<name>[&yellow_key_filter=Equity|Bond|Curncy|Index|Comdty|Govt]
- GET /curve?curve_id=USD|EUR|GBP|JPY
- GET /bql?query=<url-encoded-bql-expression>
  BQL = Bloomberg Query Language. For operational/KPI data not in BDP/BDH.
  Example (decoded): get(NUMBER_OF_VEHICLES_SOLD(FPT=Q,FPO=0Q,ACT_EST_MAPPING=PRECISE,FS=MRC)) for(['TSLA US Equity'])
  Example (URL):     /bql?query=get(NUMBER_OF_VEHICLES_SOLD(FPT%3DQ%2CFPO%3D0Q%2CACT_EST_MAPPING%3DPRECISE%2CFS%3DMRC))%20for(%5B'TSLA%20US%20Equity'%5D)

## OpenBB API (base: {openbb_base})
- GET /api/v1/equity/price/historical?symbol=AAPL&start_date=YYYY-MM-DD&provider=yfinance
- GET /api/v1/equity/price/quote?symbol=AAPL&provider=yfinance
- GET /api/v1/equity/profile?symbol=AAPL&provider=yfinance
- GET /api/v1/equity/fundamental/income?symbol=AAPL&period=annual&provider=fmp
- GET /api/v1/equity/fundamental/balance?symbol=AAPL&period=annual&provider=fmp
- GET /api/v1/equity/fundamental/cash?symbol=AAPL&period=annual&provider=fmp
- GET /api/v1/equity/fundamental/ratios?symbol=AAPL&period=annual&provider=fmp
- GET /api/v1/equity/fundamental/metrics?symbol=AAPL&period=annual&provider=fmp
- GET /api/v1/equity/estimates/price_target?symbol=AAPL&provider=fmp
- GET /api/v1/equity/ownership/insider_trading?symbol=AAPL&provider=fmp
- GET /api/v1/fixedincome/government/yield_curve?date=YYYY-MM-DD&provider=fred
- GET /api/v1/fixedincome/rate/sofr?start_date=YYYY-MM-DD&provider=fred
- GET /api/v1/currency/price/historical?symbol=EURUSD&start_date=YYYY-MM-DD&provider=fmp
- GET /api/v1/economy/cpi?countries=united_states&frequency=monthly&provider=fred
- GET /api/v1/economy/gdp/real?start_date=YYYY-MM-DD&provider=oecd
- GET /api/v1/derivatives/options/chains?symbol=AAPL&provider=cboe

## Bloomberg ticker format reference:
- Equities: "AAPL US Equity", "TSLA US Equity", "7203 JP Equity"
- Indices: "SPX Index", "CCMP Index", "INDU Index", "UKX Index", "NKY Index"
- FX: "EURUSD Curncy", "USDJPY Curncy", "GBPUSD Curncy"
- Fixed Income: "USGG10YR Index" (10Y yield), "US912828Z864 Govt" (specific bond)
- Commodities: "GC1 Comdty" (Gold), "CL1 Comdty" (WTI Oil), "CO1 Comdty" (Brent)
- Rates: "SOFRRATE Index", "FEDL01 Index", "USGG3M Index"

## Common Bloomberg fields:
PX_LAST, PX_BID, PX_ASK, PX_OPEN, PX_HIGH, PX_LOW, PX_VOLUME, CHG_PCT_1D,
NAME, SECURITY_DES, MARKET_CAP, PE_RATIO, BEST_EPS, SALES_REV_TURN, RETURN_ON_EQY,
EBITDA, NET_MARGIN, CURR_ENTP_VAL, YLD_YTM_MID, DUR_MID, TOT_RETURN_INDEX_GROSS_DVDS,
BEST_TARGET_PRICE, BEST_BUY_CNT, BEST_HOLD_CNT, BEST_SELL_CNT

## CRITICAL RULES — follow these before generating any URL:

1. NEVER generate a URL with missing required parameters. Required params per endpoint:
   - /bdp  → securities (required), fields (required)
   - /bdh  → securities (required), fields (required), start_date (required, format YYYY-MM-DD)
   - /bds  → security (required), field (required, e.g. INDX_MEMBERS)
   - /intraday/bars  → security (required), start_datetime (required, ISO format), interval (required, minutes)
   - /intraday/ticks → security (required), start_datetime (required, ISO format)
   - /api/v1/equity/price/historical → symbol (required), provider (required), start_date (required)
   - /api/v1/fixedincome/rate/sofr → start_date (required), provider (required)
   - /api/v1/currency/price/historical → symbol (required), provider (required), start_date (required)

2. If the user has not provided all required parameters, ASK for them in the conversation BEFORE generating the URL. For example:
   - User: "show me TSLA historical data" → Ask: "What date range would you like? (e.g. last 1 year, last 6 months, or a specific start date)"
   - User: "BDS for Apple" → Ask: "Which bulk field do you need? e.g. DVD_HIST_ALL (dividends), INDX_MEMBERS (index members), EARN_ANN_DT_AND_EPS (earnings), BOARD_OF_DIRECTORS"
   - User: "intraday for AAPL" → Ask: "What date and time range? And what interval in minutes?"

3. Only generate the URL once you have ALL required parameters from the user. The URL you produce will be directly executed — it must work.

4. Use sensible defaults when clearly implied: periodicity=DAILY for BDH unless specified, provider=yfinance for OpenBB equity unless specified, end_date=today if not given.

## BQL — Bloomberg Query Language (Excel Add-in only, NOT available via API)
BQL is a separate Bloomberg Excel function for operational/alternative data and complex fiscal queries.
It is NOT accessible via the blpapi HTTP API. Always recommend BQL when the user asks for data that
BDP/BDH cannot return.

BQL syntax (legacy, most common):
  =BQL("security", "FIELD_NAME", "OVERRIDE_KEY=VALUE", ...)

BQL syntax (BQL 2.0, more powerful):
  =BQL("univ('TSLA US Equity')", "number_of_vehicles_sold(per=q,fill=prev)")

### BQL use cases and examples:

**Operational / KPI data (NOT in BDP/BDH):**
  =BQL("TSLA US Equity", "NUMBER_OF_VEHICLES_SOLD", "FPT=Q", "FPO=0Q", "ACT_EST_MAPPING=PRECISE", "FS=MRC", "CURRENCY=USD", "XLFILL=b")
    → TSLA vehicle deliveries, most recent quarter

  =BQL("TSLA US Equity", "NUMBER_OF_VEHICLES_SOLD", "FPT=Q", "FPO=-3Q", "FPO=0Q")
    → Last 4 quarters of deliveries (use array, Ctrl+Shift+Enter)

  =BQL("NFLX US Equity", "RETAIL_SUBSCRIBERS", "FPT=Q", "FPO=0Q")
    → Netflix subscriber count

  =BQL("AMZN US Equity", "ECOMMERCE_GMV", "FPT=Q", "FPO=0Q")
    → Amazon GMV

  =BQL("AAPL US Equity", "UNIT_SALES_IPHONE", "FPT=Q", "FPO=0Q")
    → iPhone units sold

**Financials with fiscal period control:**
  =BQL("AAPL US Equity", "sales()", "per=q", "fill=prev", "frq=q")
    → Quarterly revenue (BQL 2.0 style)

  =BQL("MSFT US Equity", "net_income()", "per=q", "frq=q")
    → Quarterly net income

**Estimates vs actuals:**
  =BQL("TSLA US Equity", "is_eps_diluted()", "per=q", "act_est_mapping=precise")
    → EPS actuals with precise actuals/estimates mapping

### BQL key override parameters:
- FPT=Q|A|S — Fiscal period type: Quarterly / Annual / Semi-annual
- FPO=0Q|-1Q|-2Q|-3Q — Fiscal period offset: current / 1 ago / 2 ago / 3 ago
- FPO=0A|-1A — Annual offset
- ACT_EST_MAPPING=PRECISE|INCLUSIVE — How to map actuals vs estimates
- FS=MRC|MRQ — Fiscal statement: most recent cumulative / most recent quarter
- CURRENCY=USD|GBP|EUR — Currency override
- XLFILL=b|f — Excel fill: blank / forward-fill
- per=q|a — Period (BQL 2.0)
- frq=q|a|s — Frequency (BQL 2.0)
- fill=prev|none — Fill missing values (BQL 2.0)

### Common BQL-only fields (not in BDP/BDH):
NUMBER_OF_VEHICLES_SOLD, RETAIL_SUBSCRIBERS, STREAMING_SUBSCRIBERS, PAID_SUBSCRIBERS,
UNIT_SALES_IPHONE, UNIT_SALES_MAC, UNIT_SALES_IPAD, SAME_STORE_SALES_GROWTH,
STORE_COUNT, ACTIVE_USERS, MONTHLY_ACTIVE_USERS, AVERAGE_REVENUE_PER_USER,
ECOMMERCE_GMV, BACKLOG_UNITS, PROD_PRICE_AVG_US, RENEWAL_RATE, CHURN_RATE,
PASSENGER_LOAD_FACTOR, REVENUE_PASSENGER_MILES, AVAILABLE_SEAT_MILES,
HOTEL_OCCUPANCY_RATE, REVPAR, ADR_HOTEL

## CRITICAL RULE: BQL vs BDP/BDH decision:
- If the user asks for operational/KPI data (deliveries, subscribers, units, store count, occupancy, etc.) → use BQL, NOT BDP/BDH
- If BDP returns [error] for a field → try BQL instead, it may be licensed differently
- BQL is Excel-only — always note this clearly and provide the exact =BQL() formula

## API vs Excel field availability:
Some Bloomberg fields are NOT available via the blpapi (HTTP API) due to licensing restrictions, but DO work in the Bloomberg Excel Add-in if the user has the appropriate subscription:

EXCEL-ONLY fields (return errors via API, work in Excel via BDP/BDH):
- BEst consensus fields: BEST_EPS, BEST_SALES, BEST_EBITDA, BEST_TARGET_PRICE, BEST_BUY_CNT, BEST_HOLD_CNT, BEST_SELL_CNT, BEST_PE_RATIO, BEST_ROE, EARN_SURP_PCT_LAST_QTR (require Bloomberg Estimates license)
- BVAL pricing: BVAL_PRICE_MIDPOINT, BVAL_PRICE_BID, BVAL_PRICE_ASK, BVAL_SCORE (require BVAL subscription)
- Spread analytics: Z_SPRD_MID, OAS_SPREAD_MID (require Bloomberg fixed income analytics)
- ESG: ESG_DISCLOSURE_SCORE, ENVIRONMENTAL_SCORE, SOCIAL_SCORE, GOVERNANCE_SCORE, CARBON_EMISSIONS_SCOPE1, CARBON_EMISSIONS_SCOPE2 (require Bloomberg ESG license)
- Implied volatility: 30DAY_IMPVOL_100_PCT, 3MTH_IMPVOL_100_PCT (require options analytics subscription)
- Intraday VWAP: VWAP (require intraday data license)

EXCEL-ONLY via BQL (not accessible at all via API):
- All operational/KPI fields listed in the BQL section above

When the user asks for fields that are Excel-only, you MUST:
1. Warn them: "⚠ This field is not available via the API — it requires a [license name] subscription."
2. Still provide the Excel formula equivalent (BDP, BDH, or BQL as appropriate)
3. Suggest an API-accessible alternative if one exists

## Bloomberg Excel Formula equivalents:
When generating Bloomberg API calls, ALWAYS include the equivalent Bloomberg Excel formula.

- BDP (current/reference data):
  Single field:  =BDP("AAPL US Equity","PX_LAST")
  Multi-field:   =BDP("AAPL US Equity","PE_RATIO")  (one formula per field)
  With override: =BDP("AAPL US Equity","PX_LAST","PRICING_SOURCE","BGN")

- BDH (historical data) — array formula, press Ctrl+Shift+Enter:
  =BDH("AAPL US Equity","PX_LAST","20250101","20260103","periodicitySelection=DAILY")
  Multi-field:   =BDH("AAPL US Equity","PX_LAST,PX_VOLUME","20250101","20260103")

- BDS (bulk/array data) — array formula:
  =BDS("SPX Index","INDX_MEMBERS")
  =BDS("AAPL US Equity","DVD_HIST_ALL")

- Yield curve (one formula per tenor):
  =BDP("USGG2YR Index","PX_LAST")   ' 2Y
  =BDP("USGG10YR Index","PX_LAST")  ' 10Y

- For OpenBB endpoints with no direct Bloomberg equivalent, note: "No direct Bloomberg Excel formula — use Bloomberg Terminal: <suggested function key> <GO>"

Excel formula tips:
- BDH and BDS are array formulas — select a range first, then Ctrl+Shift+Enter
- Dates in BDH: "YYYYMMDD" format or cell reference
- For live auto-refresh: Tools > Real-Time Data > Configure

When you have all parameters, respond with:
1. A brief explanation
2. The exact complete URL
3. A curl example
4. The equivalent Bloomberg Excel formula(s) — formatted in a code block labeled "Excel"

Be concise. Today's date is """ + datetime.date.today().isoformat() + "."

CHAT_SYSTEM = (CHAT_SYSTEM
    .replace("{bbg_base}", BBG_BASE_URL)
    .replace("{openbb_base}", OPENBB_BASE_URL)
)

@app.post("/chat", tags=["AI Assistant"])
async def chat(req: ChatRequest):
    """AI assistant that helps construct Bloomberg and OpenBB API calls."""
    import httpx

    # Key priority: env var → config file
    api_key = os.environ.get("ANTHROPIC_API_KEY") or ""
    if not api_key:
        cfg_path = os.path.join(_CFG_DIR, "config.json")
        try:
            with open(cfg_path) as f:
                api_key = json.load(f).get("ANTHROPIC_API_KEY", "")
        except Exception:
            pass
    if not api_key:
        raise HTTPException(503, detail="No ANTHROPIC_API_KEY found. Enter it in the playground settings (⚙).")

    base_url = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    headers = {
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
        "x-api-key": api_key,
    }

    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    body = {
        "model": model,
        "max_tokens": 1024,
        "system": CHAT_SYSTEM,
        "messages": [{"role": m.safe_role, "content": m.content} for m in req.messages],
        "stream": True,
    }

    async def generate():
        async with httpx.AsyncClient(timeout=60) as client:
            async with client.stream("POST", f"{base_url}/v1/messages",
                                     headers=headers, json=body) as resp:
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        ev = json.loads(data)
                        if ev.get("type") == "content_block_delta":
                            text = ev["delta"].get("text", "")
                            if text:
                                yield f"data: {json.dumps({'text': text})}\n\n"
                    except Exception:
                        pass
        yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


if __name__ == "__main__":
    import uvicorn
    api_host = os.environ.get("API_HOST", "127.0.0.1")
    api_port = int(os.environ.get("API_PORT", "8195"))
    uvicorn.run("bbg_api:app", host=api_host, port=api_port, reload=False, log_level="info")
