"""
folio · Real-Time Portfolio Dashboard
======================================
Single file. Run once. Everything included.

SETUP (5 minutes):
  1. pip install kiteconnect flask flask-cors
  2. Get API key from developers.kite.trade (free Personal plan)
  3. Set your keys below OR use environment variables
  4. python server.py
  5. Open http://localhost:5000
  6. Click "Connect Zerodha" — log in once per day
"""

import os, json, math, time, threading
from datetime import datetime, date
from flask import Flask, redirect, request, jsonify, Response
from flask_cors import CORS

# ─────────────────────────────────────────────────────────
# CONFIG — put your keys here OR set as environment vars
# ─────────────────────────────────────────────────────────
API_KEY    = os.getenv("KITE_API_KEY",    "YOUR_API_KEY_HERE")
API_SECRET = os.getenv("KITE_API_SECRET", "YOUR_API_SECRET_HERE")

# Your portfolio settings (can also be changed in the UI)
CAGR_TARGET    = float(os.getenv("CAGR_TARGET",    "15"))   # % annual return goal
MAX_LOSS_PCT   = float(os.getenv("MAX_LOSS_PCT",    "10"))   # % max portfolio loss
POS_LOSS_PCT   = float(os.getenv("POS_LOSS_PCT",    "10"))   # % max per-position loss
INVESTED_SINCE = os.getenv("INVESTED_SINCE", "2023-01-01")  # YYYY-MM-DD

# ─────────────────────────────────────────────────────────

try:
    from kiteconnect import KiteConnect
    kite = KiteConnect(api_key=API_KEY)
    KITE_AVAILABLE = True
except ImportError:
    KITE_AVAILABLE = False
    print("⚠  kiteconnect not installed. Run: pip install kiteconnect flask flask-cors")

app = Flask(__name__)
CORS(app)

# In-memory session store
session = {
    "access_token": None,
    "connected_at": None,
    "user_name":    None,
    "cache":        {},
    "cache_ts":     {},
}

CACHE_TTL = 60  # seconds — refresh data every 60s

# ─────────────────────────────────────────────────────────
# AUTH
# ─────────────────────────────────────────────────────────

@app.route("/auth/login")
def auth_login():
    if not KITE_AVAILABLE:
        return "kiteconnect not installed. Run: pip install kiteconnect", 500
    return redirect(kite.login_url())


@app.route("/auth/callback")
def auth_callback():
    request_token = request.args.get("request_token")
    status        = request.args.get("status", "")
    if status != "success" or not request_token:
        return redirect("/?error=login_failed")
    try:
        data = kite.generate_session(request_token, api_secret=API_SECRET)
        session["access_token"] = data["access_token"]
        session["connected_at"] = datetime.now().isoformat()
        kite.set_access_token(data["access_token"])
        # Fetch profile
        profile = kite.profile()
        session["user_name"] = profile.get("user_name", "")
        session["cache"]     = {}  # clear cache on new login
        return redirect("/?connected=1")
    except Exception as e:
        return redirect(f"/?error={str(e)}")


@app.route("/auth/status")
def auth_status():
    return jsonify({
        "connected":    bool(session["access_token"]),
        "connected_at": session["connected_at"],
        "user_name":    session["user_name"],
    })


@app.route("/auth/logout")
def auth_logout():
    session["access_token"] = None
    session["connected_at"] = None
    session["user_name"]    = None
    session["cache"]        = {}
    return jsonify({"ok": True})


def require_auth():
    """Set token on kite object. Returns error response or None."""
    if not KITE_AVAILABLE:
        return jsonify({"error": "kiteconnect not installed"}), 503
    token = session.get("access_token")
    if not token:
        return jsonify({"error": "Not connected", "login_url": "/auth/login"}), 401
    kite.set_access_token(token)
    return None


def cached(key, fn, ttl=CACHE_TTL):
    """Simple in-memory cache."""
    now = time.time()
    if key in session["cache"] and (now - session["cache_ts"].get(key, 0)) < ttl:
        return session["cache"][key]
    result = fn()
    session["cache"][key]    = result
    session["cache_ts"][key] = now
    return result


# ─────────────────────────────────────────────────────────
# PORTFOLIO HISTORY — auto-saves daily snapshots
# ─────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────
# FREE CLOUD STORAGE via JSONBin.io
# ─────────────────────────────────────────────────────────
# 1. Go to jsonbin.io → sign up free
# 2. Create a bin with content: {}
# 3. Copy the Bin ID and your API key
# 4. Set as environment variables in Render:
#    JSONBIN_BIN_ID   = your bin id  (e.g. 64a1b2c3d4e5f6...)
#    JSONBIN_API_KEY  = your api key (e.g. $2b$10$...)
# ─────────────────────────────────────────────────────────

import urllib.request, urllib.error

JSONBIN_BIN_ID  = os.getenv("JSONBIN_BIN_ID",  "")
JSONBIN_API_KEY = os.getenv("JSONBIN_API_KEY",  "")
JSONBIN_BASE    = f"https://api.jsonbin.io/v3/b/{JSONBIN_BIN_ID}"

def load_history():
    """Load portfolio history from JSONBin cloud storage."""
    if not JSONBIN_BIN_ID or not JSONBIN_API_KEY:
        # Fallback to local file if keys not set
        try:
            if os.path.exists("portfolio_history.json"):
                with open("portfolio_history.json") as f:
                    return json.load(f)
        except: pass
        return {}
    try:
        req = urllib.request.Request(
            JSONBIN_BASE + "/latest",
            headers={"X-Master-Key": JSONBIN_API_KEY, "X-Bin-Meta": "false"}
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"JSONBin load error: {e}")
        return {}

def save_snapshot(total_value, total_cost, cash):
    """Save today's snapshot to JSONBin. Once per day."""
    try:
        today   = date.today().isoformat()
        history = load_history()
        if today in history:
            return  # already saved today
        history[today] = {
            "date":          today,
            "value":         round(total_value, 2),
            "invested":      round(total_cost, 2),
            "cash":          round(cash, 2),
            "total_capital": round(total_value + cash, 2),
            "pl":            round(total_value - total_cost, 2),
            "pl_pct":        round((total_value - total_cost) / total_cost * 100 if total_cost > 0 else 0, 2),
        }
        if JSONBIN_BIN_ID and JSONBIN_API_KEY:
            data = json.dumps(history).encode()
            req  = urllib.request.Request(
                JSONBIN_BASE,
                data=data,
                method="PUT",
                headers={"Content-Type": "application/json", "X-Master-Key": JSONBIN_API_KEY}
            )
            urllib.request.urlopen(req, timeout=5)
        else:
            with open("portfolio_history.json", "w") as f:
                json.dump(history, f, indent=2)
        print(f"Snapshot saved for {today}")
    except Exception as e:
        print(f"Could not save snapshot: {e}")

def get_history_series():
    """Return sorted list of daily snapshots."""
    history = load_history()
    return sorted(history.values(), key=lambda x: x["date"])

# ─────────────────────────────────────────────────────────
# DATA HELPERS
# ─────────────────────────────────────────────────────────

def enrich_holdings(raw):
    """Add computed fields to each holding."""
    enriched = []
    for h in raw:
        qty   = h.get("quantity", 0)
        avg   = h.get("average_price", 0)
        ltp   = h.get("last_price", avg)
        val   = qty * ltp
        cost  = qty * avg
        pl    = val - cost
        pl_pct = (pl / cost * 100) if cost > 0 else 0
        enriched.append({
            **h,
            "current_value":  round(val, 2),
            "invested_value": round(cost, 2),
            "pnl":            round(pl, 2),
            "pnl_pct":        round(pl_pct, 2),
            "weight_pct":     0,  # filled after total is known
        })
    total_val = sum(h["current_value"] for h in enriched)
    for h in enriched:
        h["weight_pct"] = round((h["current_value"] / total_val * 100) if total_val > 0 else 0, 2)
    return enriched, total_val


def calc_cagr(cost, value, since_str):
    try:
        since = datetime.strptime(since_str, "%Y-%m-%d")
        years = (datetime.now() - since).days / 365.25
        if years < 0.01 or cost <= 0:
            return 0
        return round((math.pow(value / cost, 1 / years) - 1) * 100, 2)
    except:
        return 0


def calc_trade_stats(trades):
    """Win/loss/profit factor from trade history."""
    wins = losses = win_amt = loss_amt = 0
    inflow = outflow = 0
    inflow_count = outflow_count = 0

    # Group buys and sells to estimate P&L per symbol
    buy_map = {}   # symbol -> list of (qty, price)

    for t in trades:
        sym   = t.get("tradingsymbol", "")
        qty   = t.get("quantity", 0)
        price = t.get("average_price", t.get("price", 0))
        ttype = t.get("transaction_type", "").upper()
        amt   = qty * price

        if ttype == "BUY":
            inflow += amt
            inflow_count += 1
            if sym not in buy_map:
                buy_map[sym] = []
            buy_map[sym].append({"qty": qty, "price": price})

        elif ttype == "SELL":
            outflow += amt
            outflow_count += 1
            # FIFO P&L estimate
            if sym in buy_map and buy_map[sym]:
                buy = buy_map[sym][0]
                pl  = (price - buy["price"]) * min(qty, buy["qty"])
                if pl > 0:
                    wins += 1; win_amt += pl
                else:
                    losses += 1; loss_amt += abs(pl)
                # consume the buy
                if qty >= buy["qty"]:
                    buy_map[sym].pop(0)
                else:
                    buy_map[sym][0]["qty"] -= qty

    total_trades  = wins + losses
    win_rate      = (wins / total_trades * 100) if total_trades > 0 else 0
    avg_win       = (win_amt / wins)    if wins   > 0 else 0
    avg_loss      = (loss_amt / losses) if losses > 0 else 0
    profit_factor = (win_amt / loss_amt) if loss_amt > 0 else (float("inf") if win_amt > 0 else 0)
    rr            = (avg_win / avg_loss) if avg_loss > 0 else float("inf")

    return {
        "wins":          wins,
        "losses":        losses,
        "total_trades":  total_trades,
        "win_rate":      round(win_rate, 1),
        "win_amt":       round(win_amt, 2),
        "loss_amt":      round(loss_amt, 2),
        "avg_win":       round(avg_win, 2),
        "avg_loss":      round(avg_loss, 2),
        "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else 999,
        "risk_reward":   round(rr, 2) if rr != float("inf") else 999,
        "inflow":        round(inflow, 2),
        "outflow":       round(outflow, 2),
        "inflow_count":  inflow_count,
        "outflow_count": outflow_count,
        "net_deployed":  round(inflow - outflow, 2),
    }


# ─────────────────────────────────────────────────────────
# API ROUTES
# ─────────────────────────────────────────────────────────

@app.route("/api/summary")
def api_summary():
    err = require_auth()
    if err: return err
    try:
        settings = {
            "cagr_target":    float(request.args.get("cagr_target",    CAGR_TARGET)),
            "max_loss_pct":   float(request.args.get("max_loss_pct",   MAX_LOSS_PCT)),
            "pos_loss_pct":   float(request.args.get("pos_loss_pct",   POS_LOSS_PCT)),
            "invested_since": request.args.get("invested_since",       INVESTED_SINCE),
        }

        raw_holdings = cached("holdings", kite.holdings)
        holdings, total_val = enrich_holdings(raw_holdings)

        total_cost = sum(h["invested_value"] for h in holdings)
        total_pl   = total_val - total_cost
        total_pl_pct = (total_pl / total_cost * 100) if total_cost > 0 else 0
        cagr       = calc_cagr(total_cost, total_val, settings["invested_since"])

        # Load per-stock custom risk limits
        stock_risks = load_stock_risks()
        def get_stock_limit(ticker):
            return stock_risks.get(ticker, settings["pos_loss_pct"])
        breaching = [h for h in holdings if h["pnl_pct"] < -get_stock_limit(h["tradingsymbol"])]
        sorted_h  = sorted(holdings, key=lambda h: h["pnl_pct"], reverse=True)

        # Funds / cash — fetch BEFORE risk calculation
        try:
            margins   = cached("margins", kite.margins)
            equity    = margins.get("equity", {})
            cash_avail = equity.get("available", {}).get("cash", 0)
        except:
            cash_avail = 0

        # Risk based on TOTAL capital (holdings + cash)
        total_capital = total_val + cash_avail
        max_loss_amt  = total_capital * (settings["max_loss_pct"] / 100)
        actual_loss   = abs(total_pl) if total_pl < 0 else 0
        loss_used_pct = (actual_loss / max_loss_amt * 100) if max_loss_amt > 0 else 0
        stop_investing = actual_loss >= max_loss_amt

        # Auto-save daily snapshot
        save_snapshot(total_val, total_cost, cash_avail)

        # Trade stats
        try:
            trades = cached("trades", kite.trades, ttl=300)
            trade_stats = calc_trade_stats(trades)
        except:
            trades      = []
            trade_stats = None

        # Safe load history and stock risks — don't let these crash the whole response
        try:
            history_data = get_history_series()
        except Exception:
            history_data = []
        try:
            stock_risks_data = load_stock_risks()
        except Exception:
            stock_risks_data = {}

        return jsonify({
            "timestamp":     datetime.now().isoformat(),
            "user_name":     session["user_name"],
            "settings":      settings,
            "portfolio": {
                "total_value":    round(total_val, 2),
                "total_cost":     round(total_cost, 2),
                "total_pl":       round(total_pl, 2),
                "total_pl_pct":   round(total_pl_pct, 2),
                "cagr":           cagr,
                "cagr_met":       cagr >= settings["cagr_target"],
                "holdings_count": len(holdings),
                "cash_available": round(cash_avail, 2),
                "total_capital":  round(total_capital, 2),
            },
            "risk": {
                "max_loss_amt":   round(max_loss_amt, 2),
                "actual_loss":    round(actual_loss, 2),
                "loss_used_pct":  round(loss_used_pct, 2),
                "loss_remaining": round(max(max_loss_amt - actual_loss, 0), 2),
                "stop_investing": stop_investing,
                "breaching_count":len(breaching),
                "breaching":      [{"ticker": h["tradingsymbol"], "pnl_pct": h["pnl_pct"]} for h in breaching],
            },
            "holdings":      sorted(holdings, key=lambda h: h["current_value"], reverse=True),
            "top_gainers":   [{"ticker": h["tradingsymbol"], "pnl_pct": h["pnl_pct"], "pnl": h["pnl"]} for h in sorted_h[:5]],
            "top_losers":    [{"ticker": h["tradingsymbol"], "pnl_pct": h["pnl_pct"], "pnl": h["pnl"]} for h in sorted_h[-5:][::-1]],
            "trade_stats":   trade_stats,
            "history":       history_data,
            "stock_risks":   stock_risks_data,
        })
    except Exception as e:
        import traceback
        print("ERROR in /api/summary:", traceback.format_exc())
        return jsonify({"error": str(e), "detail": traceback.format_exc()}), 500


@app.route("/api/holdings")
def api_holdings():
    err = require_auth()
    if err: return err
    try:
        raw = cached("holdings", kite.holdings)
        holdings, _ = enrich_holdings(raw)
        return jsonify(holdings)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/positions")
def api_positions():
    err = require_auth()
    if err: return err
    try:
        return jsonify(cached("positions", kite.positions, ttl=30))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/funds")
def api_funds():
    err = require_auth()
    if err: return err
    try:
        margins = cached("margins", kite.margins)
        eq = margins.get("equity", {})
        return jsonify({
            "available_cash": eq.get("available", {}).get("cash", 0),
            "used_margin":    eq.get("utilised", {}).get("debits", 0),
            "net_balance":    eq.get("net", 0),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/refresh")
def api_refresh():
    """Force-clear cache and re-fetch."""
    err = require_auth()
    if err: return err
    session["cache"]    = {}
    session["cache_ts"] = {}
    return jsonify({"ok": True, "message": "Cache cleared"})


@app.route("/api/nifty-pe")
def api_nifty_pe():
    """Fetch Nifty 50 PE ratio — tries multiple free sources."""
    sources = [
        # Source 1: NSE India market data API
        ("https://www.nseindia.com/api/allIndices", "NSE"),
        # Source 2: Yahoo Finance
        ("https://query2.finance.yahoo.com/v10/finance/quoteSummary/%5ENSEI?modules=summaryDetail", "Yahoo"),
    ]
    
    # Try NSE India
    try:
        session_req = urllib.request.Request(
            "https://www.nseindia.com",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
        )
        import http.cookiejar
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        opener.open(session_req, timeout=5)  # get cookies
        
        api_req = urllib.request.Request(
            "https://www.nseindia.com/api/allIndices",
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.nseindia.com/",
                "X-Requested-With": "XMLHttpRequest",
            }
        )
        with opener.open(api_req, timeout=8) as r:
            data = json.loads(r.read().decode())
        for item in data.get("data", []):
            if item.get("index") == "NIFTY 50":
                pe = item.get("pe")
                pb = item.get("pb")
                div = item.get("divYield")
                if pe:
                    return jsonify({
                        "pe": round(float(pe), 2),
                        "pb": round(float(pb), 2) if pb else None,
                        "div_yield": round(float(div), 2) if div else None,
                        "source": "NSE India"
                    })
    except Exception as e:
        print(f"NSE PE fetch failed: {e}")

    # Try Yahoo Finance
    try:
        req = urllib.request.Request(
            "https://query2.finance.yahoo.com/v10/finance/quoteSummary/%5ENSEI?modules=summaryDetail",
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=6) as r:
            data = json.loads(r.read().decode())
        pe = data["quoteSummary"]["result"][0]["summaryDetail"].get("trailingPE", {}).get("raw")
        if pe:
            return jsonify({"pe": round(pe, 2), "source": "Yahoo Finance"})
    except Exception as e:
        print(f"Yahoo PE fetch failed: {e}")

    return jsonify({"pe": None, "error": "Could not fetch PE ratio"}), 200


@app.route("/api/ticker")
def api_ticker():
    """Fetch live index prices from Yahoo Finance — free, no API key needed."""
    symbols = {
        "NIFTY50":   "^NSEI",
        "MIDCAP100": "NIFTY_MIDCAP_100.NS",  # Yahoo Finance symbol for Nifty Midcap 100
        "SMALLCAP":  "^CNXSC",        # NSE Smallcap 100
    }
    result = {}
    for name, sym in symbols.items():
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1m&range=1d"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=6) as r:
                data = json.loads(r.read().decode())
            meta  = data["chart"]["result"][0]["meta"]
            price = meta.get("regularMarketPrice", 0)
            prev  = meta.get("chartPreviousClose", price)
            chg   = price - prev
            chgPct= (chg / prev * 100) if prev else 0
            entry = {
                "price":     round(price, 2),
                "change":    round(chg, 2),
                "changePct": round(chgPct, 2),
            }
            # PE ratio for Nifty 50 — fetch from NSE India public API
            if name == "NIFTY50":
                try:
                    # NSE India public endpoint (no auth, free)
                    nse_url = "https://www.nseindia.com/api/equity-meta-info?symbol=NIFTY%2050"
                    nse_req = urllib.request.Request(nse_url, headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                        "Accept": "application/json",
                        "Referer": "https://www.nseindia.com/"
                    })
                    with urllib.request.urlopen(nse_req, timeout=6) as pr:
                        nse_data = json.loads(pr.read().decode())
                    pe = nse_data.get("pdSymbolPe") or nse_data.get("pe")
                    if pe: entry["pe"] = round(float(pe), 1)
                except:
                    # Fallback 1: Yahoo Finance v10
                    try:
                        yf_url = "https://query2.finance.yahoo.com/v10/finance/quoteSummary/%5ENSEI?modules=summaryDetail"
                        yf_req = urllib.request.Request(yf_url, headers={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64)","Accept":"application/json","Accept-Language":"en-US,en;q=0.9"})
                        with urllib.request.urlopen(yf_req, timeout=6) as pr2:
                            yf_data = json.loads(pr2.read().decode())
                        pe2 = yf_data["quoteSummary"]["result"][0]["summaryDetail"].get("trailingPE",{}).get("raw")
                        if pe2: entry["pe"] = round(pe2, 1)
                    except:
                        # Fallback 2: hardcoded note so UI shows something
                        entry["pe"] = None
                        entry["pe_note"] = "Visit nseindia.com"
            result[name] = entry
        except Exception as e:
            result[name] = {"price": 0, "change": 0, "changePct": 0, "error": str(e)}
    return jsonify(result)


@app.route("/api/debug")
def api_debug():
    """Shows what's working and what's not — visit this URL to diagnose issues."""
    import traceback
    result = {
        "kite_available":   KITE_AVAILABLE,
        "connected":        bool(session.get("access_token")),
        "user":             session.get("user_name"),
        "jsonbin_set":      bool(JSONBIN_BIN_ID and JSONBIN_API_KEY),
        "cache_keys":       list(session.get("cache", {}).keys()),
    }
    if session.get("access_token") and KITE_AVAILABLE:
        kite.set_access_token(session["access_token"])
        try:
            h = kite.holdings()
            result["holdings_count"] = len(h)
            result["holdings_ok"] = True
        except Exception as e:
            result["holdings_ok"] = False
            result["holdings_error"] = str(e)
        try:
            m = kite.margins()
            result["margins_ok"] = True
        except Exception as e:
            result["margins_ok"] = False
            result["margins_error"] = str(e)
        try:
            history = get_history_series()
            result["history_count"] = len(history)
            result["history_ok"] = True
        except Exception as e:
            result["history_ok"] = False
            result["history_error"] = str(e)
    return jsonify(result)


@app.route("/api/history")
def api_history():
    """Return all saved daily portfolio snapshots."""
    return jsonify(get_history_series())


# ── STOCK RISK LIMITS — stored alongside history in JSONBin ──

STOCK_RISK_KEY = "__stock_risks__"

def load_stock_risks():
    history = load_history()
    return history.get(STOCK_RISK_KEY, {})

def save_stock_risks(risks):
    try:
        history = load_history()
        history[STOCK_RISK_KEY] = risks
        if JSONBIN_BIN_ID and JSONBIN_API_KEY:
            data = json.dumps(history).encode()
            req  = urllib.request.Request(
                JSONBIN_BASE,
                data=data,
                method="PUT",
                headers={"Content-Type": "application/json", "X-Master-Key": JSONBIN_API_KEY}
            )
            urllib.request.urlopen(req, timeout=5)
        else:
            with open("portfolio_history.json", "w") as f:
                json.dump(history, f, indent=2)
    except Exception as e:
        print(f"Could not save stock risks: {e}")


@app.route("/api/stock-risks", methods=["GET"])
def get_stock_risks():
    return jsonify(load_stock_risks())


@app.route("/api/stock-risks", methods=["POST"])
def set_stock_risks():
    try:
        risks = request.get_json()
        save_stock_risks(risks)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────────────────────
# SSE — Server-Sent Events for live push updates
# ─────────────────────────────────────────────────────────

@app.route("/api/stream")
def api_stream():
    """
    Streams portfolio updates to the frontend every 60 seconds.
    Frontend uses EventSource to receive live updates without polling.
    """
    err = require_auth()
    if err: return err

    def generate():
        while True:
            try:
                kite.set_access_token(session["access_token"])
                raw = kite.holdings()
                holdings, total_val = enrich_holdings(raw)
                total_cost = sum(h["invested_value"] for h in holdings)
                total_pl   = total_val - total_cost
                data = {
                    "total_value":  round(total_val, 2),
                    "total_pl":     round(total_pl, 2),
                    "total_pl_pct": round((total_pl/total_cost*100) if total_cost>0 else 0, 2),
                    "timestamp":    datetime.now().isoformat(),
                }
                yield f"data: {json.dumps(data)}\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
            time.sleep(60)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ─────────────────────────────────────────────────────────
# FRONTEND — served directly from this file
# ─────────────────────────────────────────────────────────




@app.route("/api/journal", methods=["GET"])
def get_journal():
    """Get journal entries from JSONBin or local file."""
    try:
        history = load_history()
        return jsonify(history.get("__journal__", {}))
    except Exception as e:
        return jsonify({}), 200

@app.route("/api/journal", methods=["POST"])
def save_journal():
    """Save journal entries."""
    try:
        journal_data = request.get_json()
        history = load_history()
        history["__journal__"] = journal_data
        if JSONBIN_BIN_ID and JSONBIN_API_KEY:
            data = json.dumps(history).encode()
            req  = urllib.request.Request(
                JSONBIN_BASE, data=data, method="PUT",
                headers={"Content-Type": "application/json", "X-Master-Key": JSONBIN_API_KEY}
            )
            urllib.request.urlopen(req, timeout=5)
        else:
            with open("portfolio_history.json", "w") as f:
                json.dump(history, f, indent=2)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/news-proxy")
def news_proxy():
    """Proxy Google News RSS to avoid CORS issues."""
    try:
        url = request.args.get("url", "")
        if not url or "news.google.com" not in url:
            return jsonify([]), 200
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            content = r.read().decode("utf-8", errors="replace")
        # Simple RSS parse
        import re
        items = []
        entries = re.findall(r'<item>(.*?)</item>', content, re.DOTALL)
        for e in entries[:8]:
            title = re.search(r'<title><!\[CDATA\[(.*?)\]\]></title>', e) or re.search(r'<title>(.*?)</title>', e)
            link  = re.search(r'<link>(.*?)</link>', e)
            date  = re.search(r'<pubDate>(.*?)</pubDate>', e)
            source= re.search(r'<source[^>]*>(.*?)</source>', e)
            if title:
                items.append({
                    "title":   title.group(1).strip(),
                    "link":    link.group(1).strip() if link else "#",
                    "pubDate": date.group(1)[:16] if date else "",
                    "source":  source.group(1).strip() if source else "Google News",
                })
        return jsonify(items)
    except Exception as e:
        return jsonify([]), 200


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>folio · Live</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script>
<style>
:root{
  --bg:#0f1117;--s1:#1a1d27;--s2:#22263a;--s3:#2a2f45;
  --border:#2e3248;--text:#e8eaf6;--muted:#7b82a8;
  --accent:#6c63ff;--gain:#00e676;--loss:#ff5252;--warn:#ffab40;--orange:#ff7043;
  --gain-bg:rgba(0,230,118,0.07);--loss-bg:rgba(255,82,82,0.07);--warn-bg:rgba(255,171,64,0.08);
  --shadow:0 2px 12px rgba(0,0,0,0.3);
}
.light{
  --bg:#f4f6fb;--s1:#fff;--s2:#f0f2f9;--s3:#e4e8f5;
  --border:#dde1f0;--text:#1a1d2e;--muted:#7b82a8;
  --accent:#6c63ff;--gain:#00a152;--loss:#d32f2f;--warn:#e65100;--orange:#bf360c;
  --gain-bg:rgba(0,161,82,0.07);--loss-bg:rgba(211,47,47,0.07);--warn-bg:rgba(230,81,0,0.08);
  --shadow:0 2px 12px rgba(0,0,0,0.06);
}
*{box-sizing:border-box;margin:0;padding:0;}
body{background:var(--bg);color:var(--text);font-family:'Inter',sans-serif;font-size:13px;min-height:100vh;overflow-x:hidden;}

/* TICKER STRIP */
.ticker-strip{background:var(--s1);border-bottom:1px solid var(--border);padding:0 8px;height:36px;display:flex;align-items:center;gap:0;overflow:hidden;position:sticky;top:0;z-index:101;}
.ticker-item{display:inline-flex;align-items:center;gap:9px;padding:0 24px;cursor:pointer;text-decoration:none;color:var(--text);transition:opacity 0.15s;white-space:nowrap;height:100%;}
.ticker-item:hover{opacity:0.75;background:var(--s2);}
.ticker-name{font-size:0.75rem;font-weight:600;color:var(--muted);letter-spacing:0.3px;}
.ticker-price{font-family:'DM Mono',monospace;font-size:0.85rem;font-weight:700;}
.ticker-chg{font-family:'DM Mono',monospace;font-size:0.78rem;font-weight:500;}
.ticker-sep{color:var(--border);font-size:0.8rem;}

/* HEADER */
header{background:var(--s1);border-bottom:1px solid var(--border);padding:0 16px;height:44px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:36px;z-index:100;box-shadow:var(--shadow);}
.logo{font-size:1rem;font-weight:700;letter-spacing:-0.5px;}
.logo em{color:var(--accent);font-style:normal;}
.hright{display:flex;align-items:center;gap:8px;}
.live-pill{display:flex;align-items:center;gap:5px;background:var(--gain-bg);border:1px solid var(--gain);padding:3px 9px;border-radius:20px;font-size:0.65rem;font-weight:600;color:var(--gain);}
.live-dot{width:5px;height:5px;border-radius:50%;background:var(--gain);animation:pulse 2s infinite;}
.live-dot.off{background:var(--muted);animation:none;}
@keyframes pulse{0%,100%{opacity:1;box-shadow:0 0 0 0 rgba(0,230,118,0.4)}50%{opacity:0.8;box-shadow:0 0 0 4px rgba(0,230,118,0)}}
.btn{font-size:0.7rem;font-weight:500;border:1px solid var(--border);background:var(--s2);color:var(--muted);padding:5px 11px;border-radius:5px;cursor:pointer;transition:all 0.15s;}
.btn:hover{border-color:var(--accent);color:var(--accent);}
.theme-btn{width:28px;height:28px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:0.85rem;cursor:pointer;border:1px solid var(--border);background:var(--s2);}

/* NAV TABS */
.nav-tabs{background:var(--s1);border-bottom:1px solid var(--border);padding:0 16px;display:flex;gap:2px;overflow-x:auto;position:sticky;top:80px;z-index:99;}
.nav-tab{font-size:0.72rem;font-weight:500;padding:10px 14px;border:none;background:none;color:var(--muted);cursor:pointer;border-bottom:2px solid transparent;white-space:nowrap;transition:all 0.15s;}
.nav-tab:hover{color:var(--text);}
.nav-tab.active{color:var(--accent);border-bottom-color:var(--accent);}

/* CONNECT SCREEN */
#connectScreen{display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:70vh;text-align:center;padding:40px 20px;}
#connectScreen h1{font-size:2rem;font-weight:700;letter-spacing:-1px;margin-bottom:10px;}
#connectScreen h1 span{color:var(--accent);}
#connectScreen p{color:var(--muted);font-size:0.85rem;line-height:1.7;max-width:400px;margin-bottom:28px;}
.steps-list{display:flex;flex-direction:column;gap:8px;margin-bottom:24px;text-align:left;width:100%;max-width:340px;}
.step-item{display:flex;gap:10px;align-items:flex-start;background:var(--s1);border:1px solid var(--border);border-radius:7px;padding:10px 13px;}
.step-n{width:20px;height:20px;background:var(--accent);color:#fff;font-size:0.65rem;font-weight:700;border-radius:50%;display:flex;align-items:center;justify-content:center;flex-shrink:0;}
.step-t{font-size:0.76rem;color:var(--muted);line-height:1.5;}
.step-t strong{color:var(--text);}
.connect-btn-big{background:var(--accent);color:#fff;border:none;padding:12px 40px;border-radius:7px;font-weight:600;font-size:0.9rem;cursor:pointer;transition:all 0.2s;box-shadow:0 4px 16px rgba(108,99,255,0.35);}
.connect-btn-big:hover{transform:translateY(-1px);box-shadow:0 6px 24px rgba(108,99,255,0.5);}

/* MAIN */
#dashboard{display:none;}
.page{display:none;padding:14px 16px 24px;}
.page.active{display:block;}
#page-journal.active{padding-bottom:0;}

/* SETTINGS BAR */
.settings-bar{background:var(--s1);border:1px solid var(--border);border-radius:8px;padding:11px 14px;margin-bottom:14px;display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap;}
.sg{display:flex;flex-direction:column;gap:3px;}
.sg label{font-size:0.58rem;font-weight:600;letter-spacing:1px;text-transform:uppercase;color:var(--muted);}
.sg input{background:var(--s2);border:1px solid var(--border);color:var(--text);padding:6px 9px;border-radius:5px;font-size:0.78rem;width:110px;outline:none;font-family:'Inter',sans-serif;}
.sg input:focus{border-color:var(--accent);}
.updated-lbl{font-size:0.64rem;color:var(--muted);margin-left:auto;align-self:center;}

/* ALERT */
.alert{padding:9px 14px;margin-bottom:12px;display:none;align-items:center;gap:9px;font-size:0.78rem;border-radius:7px;border-left:3px solid;}
.alert.show{display:flex;}
.alert-danger{background:var(--loss-bg);border-color:var(--loss);}
.alert-warn{background:var(--warn-bg);border-color:var(--warn);}
.alert-ok{background:var(--gain-bg);border-color:var(--gain);}

/* CARDS */
.cards-row{display:grid;grid-template-columns:repeat(6,1fr);gap:10px;margin-bottom:14px;}
.card{background:var(--s1);border:1px solid var(--border);border-radius:8px;padding:13px 12px;box-shadow:var(--shadow);}
.card-lbl{font-size:0.58rem;font-weight:600;letter-spacing:1px;text-transform:uppercase;color:var(--muted);margin-bottom:6px;}
.card-val{font-size:1.15rem;font-weight:700;letter-spacing:-0.5px;line-height:1;}
.card-sub{font-size:0.63rem;margin-top:4px;color:var(--muted);}
.g{color:var(--gain);}.l{color:var(--loss);}.w{color:var(--warn);}.m{color:var(--muted);}

/* PANELS */
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px;}
.grid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-bottom:12px;}
.panel{background:var(--s1);border:1px solid var(--border);border-radius:8px;padding:16px;box-shadow:var(--shadow);margin-bottom:12px;}
.panel-title{font-size:0.68rem;font-weight:600;letter-spacing:1px;text-transform:uppercase;color:var(--muted);margin-bottom:12px;display:flex;justify-content:space-between;align-items:center;}
.panel-title span{font-size:0.62rem;font-weight:400;text-transform:none;letter-spacing:0;color:var(--muted);}

/* TABLE */
.tbl-wrap{overflow-x:auto;}
table{width:100%;border-collapse:collapse;}
thead th{font-size:0.58rem;font-weight:600;letter-spacing:1px;text-transform:uppercase;color:var(--muted);padding:0 8px 8px;text-align:right;border-bottom:1px solid var(--border);}
thead th:first-child{text-align:left;}
tbody td{padding:8px 8px;font-size:0.74rem;text-align:right;border-bottom:1px solid var(--border);}
tbody td:first-child{text-align:left;}
tbody tr:hover td{background:var(--s2);}
tbody tr:last-child td{border-bottom:none;}
.tk{font-weight:700;font-size:0.78rem;}
.tv-link{color:var(--text);transition:color 0.15s;text-decoration:none;}
.tv-link:hover{color:var(--accent);}
.mini-bar{height:4px;background:var(--s3);border-radius:2px;overflow:hidden;margin-top:3px;}
.mini-fill{height:100%;border-radius:2px;}
.sri-badge{font-size:0.58rem;padding:2px 5px;border-radius:3px;font-weight:600;}
.badge-ok{background:var(--gain-bg);color:var(--gain);}
.badge-warn{background:var(--warn-bg);color:var(--warn);}
.badge-orange{background:rgba(255,112,67,0.1);color:var(--orange);}
.badge-bad{background:var(--loss-bg);color:var(--loss);}

/* CAGR */
.cagr-num{font-size:2.4rem;font-weight:700;letter-spacing:-2px;line-height:1;}
.cagr-bar-wrap{margin:10px 0;background:var(--s3);border-radius:3px;height:6px;overflow:hidden;}
.cagr-bar-fill{height:100%;border-radius:3px;transition:width 1s ease;}
.cagr-stats{display:grid;grid-template-columns:1fr 1fr;gap:7px;margin-top:10px;}
.cagr-stat{background:var(--s2);border-radius:6px;padding:9px;text-align:center;}
.cagr-stat .val{font-size:0.9rem;font-weight:700;}
.cagr-stat .lbl{font-size:0.57rem;color:var(--muted);text-transform:uppercase;letter-spacing:0.5px;margin-top:2px;}

/* CHART TABS */
.chart-tabs{display:flex;gap:5px;}
.ctab{font-size:0.65rem;font-weight:500;padding:3px 9px;border-radius:4px;border:1px solid var(--border);background:transparent;color:var(--muted);cursor:pointer;transition:all 0.15s;}
.ctab.active{background:var(--accent);color:#fff;border-color:var(--accent);}

/* ANALYTICS */
.ha-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin-bottom:12px;}
.ha-box{background:var(--s2);border-radius:7px;padding:11px;text-align:center;border:1px solid var(--border);}
.ha-num{font-size:1rem;font-weight:700;letter-spacing:-0.5px;}
.ha-lbl{font-size:0.57rem;font-weight:600;letter-spacing:1px;text-transform:uppercase;color:var(--muted);margin-top:2px;}
.big-metric{background:var(--s2);border-radius:10px;padding:18px;border:1px solid var(--border);}
.big-metric-num{font-size:2.5rem;font-weight:700;letter-spacing:-2px;line-height:1;}
.big-metric-lbl{font-size:0.6rem;font-weight:600;letter-spacing:1.5px;text-transform:uppercase;color:var(--muted);margin-top:4px;}

/* RISK METER */
.risk-big-num{font-size:1.4rem;font-weight:700;letter-spacing:-1px;}
.risk-status-lbl{font-size:0.68rem;color:var(--muted);margin-top:3px;}

/* MODAL */
.modal-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:200;align-items:center;justify-content:center;}
.modal-overlay.open{display:flex;}
.modal{background:var(--s1);border:1px solid var(--border);border-radius:10px;padding:22px;width:320px;box-shadow:0 20px 60px rgba(0,0,0,0.4);}
.modal h3{font-size:0.95rem;font-weight:700;margin-bottom:4px;}
.modal p{font-size:0.72rem;color:var(--muted);margin-bottom:14px;}
.modal input{width:100%;background:var(--s2);border:1px solid var(--border);color:var(--text);padding:8px 11px;border-radius:5px;font-size:0.85rem;outline:none;margin-bottom:11px;font-family:'Inter',sans-serif;}
.modal input:focus{border-color:var(--accent);}
.modal-btns{display:flex;gap:7px;}
.modal-btns button{flex:1;padding:8px;border-radius:5px;font-size:0.76rem;font-weight:600;cursor:pointer;border:none;font-family:'Inter',sans-serif;}
.mbtn-save{background:var(--accent);color:#fff;}
.mbtn-cancel{background:var(--s3);color:var(--muted);}
.risk-edit-btn{font-size:0.57rem;padding:2px 6px;border-radius:3px;border:1px solid var(--border);background:transparent;color:var(--muted);cursor:pointer;margin-left:3px;}
.risk-edit-btn:hover{border-color:var(--accent);color:var(--accent);}

/* CALENDAR */
.cal-grid{display:grid;grid-template-columns:repeat(7,1fr);gap:3px;margin-bottom:14px;}
.cal-header{display:grid;grid-template-columns:repeat(7,1fr);gap:3px;margin-bottom:6px;}
.cal-dow{font-size:0.6rem;font-weight:600;text-align:center;color:var(--muted);padding:4px 0;letter-spacing:0.5px;}
.cal-day{aspect-ratio:1;border-radius:5px;display:flex;flex-direction:column;align-items:center;justify-content:center;cursor:pointer;transition:all 0.15s;border:1px solid transparent;position:relative;min-height:32px;}
.cal-day:hover{border-color:var(--accent);z-index:1;}
.cal-day.active{border-color:var(--accent);box-shadow:0 0 0 2px rgba(108,99,255,0.3);}
.cal-day .d-num{font-size:0.68rem;font-weight:600;line-height:1;}
.cal-day .d-pl{font-size:0.52rem;font-family:'DM Mono',monospace;line-height:1;margin-top:1px;}
.cal-day.gain{background:rgba(0,230,118,0.15);}
.cal-day.loss{background:rgba(255,82,82,0.15);}
.cal-day.neutral{background:var(--s3);}
.cal-day.empty{background:transparent;border-color:transparent;cursor:default;}
.cal-insights{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:4px;}
.cal-ins-box{background:var(--s2);border-radius:6px;padding:10px;border:1px solid var(--border);}
.cal-ins-lbl{font-size:0.58rem;color:var(--muted);text-transform:uppercase;letter-spacing:0.5px;margin-bottom:4px;}
.cal-ins-val{font-size:0.82rem;font-weight:700;}
.cal-ins-sub{font-size:0.65rem;color:var(--muted);margin-top:2px;}

/* SECTOR CHART */
.sector-legend{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px;}
.sec-item{display:flex;align-items:center;gap:5px;font-size:0.68rem;cursor:pointer;padding:3px 7px;border-radius:4px;border:1px solid var(--border);transition:all 0.15s;}
.sec-item:hover{border-color:var(--accent);}
.sec-item.dimmed{opacity:0.3;}
.sec-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0;}
.sector-details{background:var(--s2);border-radius:7px;padding:12px;margin-top:10px;border:1px solid var(--border);display:none;}
.sector-details.show{display:block;}

/* JOURNAL */
.journal-layout{display:grid;grid-template-columns:190px 1fr;gap:14px;}
.journal-sidebar{background:var(--s2);border-radius:8px;padding:12px;border:1px solid var(--border);height:fit-content;align-self:start;position:sticky;top:138px;}
.journal-mini-cal .mc-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:6px;}
.journal-mini-cal .mc-title{font-size:0.68rem;font-weight:600;}
.journal-mini-cal .mc-nav{background:none;border:none;color:var(--muted);cursor:pointer;font-size:0.85rem;padding:1px 4px;}
.journal-mini-cal .mc-grid{display:grid;grid-template-columns:repeat(7,1fr);gap:2px;}
.journal-mini-cal .mc-dow{font-size:0.52rem;text-align:center;color:var(--muted);padding:2px 0;}
.mc-day{font-size:0.6rem;text-align:center;padding:3px 2px;border-radius:3px;cursor:pointer;border:1px solid transparent;}
.mc-day:hover{border-color:var(--accent);}
.mc-day.has-entry{font-weight:700;color:var(--accent);}
.mc-day.today{background:var(--accent);color:#fff;border-radius:50%;}
.mc-day.selected{border-color:var(--accent);}
.journal-main{display:flex;flex-direction:column;gap:8px;min-height:0;overflow:hidden;}
.journal-date{font-size:1rem;font-weight:700;letter-spacing:-0.5px;}
.journal-search{background:var(--s2);border:1px solid var(--border);color:var(--text);padding:6px 11px;border-radius:6px;font-size:0.75rem;outline:none;width:100%;font-family:'Inter',sans-serif;}
.journal-search:focus{border-color:var(--accent);}
/* TOOLBAR */
.journal-toolbar{display:flex;gap:4px;flex-wrap:wrap;padding:6px 8px;background:var(--s3);border-radius:6px;border:1px solid var(--border);}
.jtool{background:none;border:1px solid var(--border);color:var(--muted);padding:3px 9px;border-radius:4px;cursor:pointer;font-size:0.7rem;font-family:'Inter',sans-serif;font-weight:500;transition:all 0.15s;white-space:nowrap;}
.jtool:hover{border-color:var(--accent);color:var(--accent);}
.jtool.active{background:var(--accent);color:#fff;border-color:var(--accent);}
.jtool-sep{width:1px;background:var(--border);margin:2px 3px;}
.journal-textarea{background:var(--s2);border:1px solid var(--border);color:var(--text);padding:14px 16px;border-radius:8px;font-size:0.84rem;outline:none;width:100%;resize:vertical;font-family:'Inter',sans-serif;line-height:1.7;min-height:400px;}
.journal-textarea:focus{border-color:var(--accent);}
.journal-save-status{font-size:0.62rem;color:var(--muted);}
.search-results{background:var(--s2);border:1px solid var(--border);border-radius:7px;padding:8px;display:none;max-height:160px;overflow-y:auto;}
.search-result-item{padding:6px 0;border-bottom:1px solid var(--border);cursor:pointer;}
.search-result-item:last-child{border-bottom:none;}
.search-result-item:hover .sri-date{color:var(--accent);}
.sri-date{font-size:0.68rem;font-weight:600;margin-bottom:2px;}
.sri-preview{font-size:0.65rem;color:var(--muted);}

/* NEWS */
.news-item{padding:11px 0;border-bottom:1px solid var(--border);display:flex;gap:10px;}
.news-item:last-child{border-bottom:none;}
.news-ticker{font-size:0.62rem;font-weight:700;color:var(--accent);background:var(--gain-bg);padding:2px 7px;border-radius:3px;flex-shrink:0;align-self:flex-start;margin-top:1px;}
.news-content{}
.news-title{font-size:0.78rem;font-weight:500;line-height:1.4;margin-bottom:3px;}
.news-meta{font-size:0.62rem;color:var(--muted);}
.news-title a{color:var(--text);text-decoration:none;}
.news-title a:hover{color:var(--accent);}

/* BLUR MODE */
.blur-mode .blur-val{filter:blur(5px);user-select:none;transition:filter 0.2s;}
.blur-mode .blur-val.peek{filter:blur(0);}

/* NEWS TAGS */
.news-tag{display:inline-block;font-size:0.58rem;font-weight:700;padding:1px 6px;border-radius:3px;margin-right:3px;letter-spacing:0.3px;}
.tag-up{background:rgba(0,230,118,0.15);color:var(--gain);}
.tag-dn{background:rgba(255,82,82,0.15);color:var(--loss);}
.tag-neutral{background:var(--s3);color:var(--muted);}
.tag-announce{background:rgba(108,99,255,0.15);color:var(--accent);}
.tag-quarterly{background:rgba(255,171,64,0.15);color:var(--warn);}
.tag-dividend{background:rgba(0,230,118,0.15);color:var(--gain);}
.tag-split{background:rgba(108,99,255,0.15);color:var(--accent);}
.tag-bonus{background:rgba(255,171,64,0.15);color:var(--warn);}
.news-filter-bar{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px;align-items:center;}
.nfbtn{font-size:0.65rem;padding:3px 9px;border-radius:4px;border:1px solid var(--border);background:transparent;color:var(--muted);cursor:pointer;transition:all 0.15s;font-family:'Inter',sans-serif;}
.nfbtn.active{background:var(--accent);color:#fff;border-color:var(--accent);}
.nf-ticker-wrap{display:flex;flex-wrap:wrap;gap:4px;margin-bottom:8px;}
.nf-ticker-btn{font-size:0.6rem;padding:2px 7px;border-radius:3px;border:1px solid var(--border);background:transparent;color:var(--muted);cursor:pointer;transition:all 0.15s;}
.nf-ticker-btn.active{background:var(--s3);color:var(--text);border-color:var(--accent);}

/* EARNINGS/EVENTS */
.event-row{display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--border);font-size:0.74rem;}
.event-row:last-child{border-bottom:none;}
.event-date{font-family:'DM Mono',monospace;font-size:0.65rem;color:var(--muted);min-width:70px;}
.event-ticker{font-weight:700;min-width:80px;color:var(--accent);}
.event-type{font-size:0.6rem;padding:2px 6px;border-radius:3px;font-weight:600;}
.ev-earnings{background:rgba(255,171,64,0.15);color:var(--warn);}
.ev-dividend{background:rgba(0,230,118,0.15);color:var(--gain);}
.ev-split{background:rgba(108,99,255,0.15);color:var(--accent);}
.ev-bonus{background:rgba(255,112,67,0.15);color:var(--orange);}

/* SELL BUTTON */
.sell-btn{font-size:0.58rem;padding:2px 7px;border-radius:3px;border:1px solid rgba(255,82,82,0.5);background:rgba(255,82,82,0.08);color:var(--loss);cursor:pointer;font-family:'Inter',sans-serif;font-weight:600;text-decoration:none;transition:all 0.15s;}
.sell-btn:hover{background:rgba(255,82,82,0.2);}

/* RESPONSIVE for 1080p laptop */
@media(max-width:1366px){
  .cards-row{grid-template-columns:repeat(3,1fr);}
  .grid3{grid-template-columns:1fr 1fr;}
  .journal-layout{grid-template-columns:160px 1fr;}
}
@media(max-width:900px){
  .cards-row{grid-template-columns:repeat(2,1fr);}
  .grid2,.grid3{grid-template-columns:1fr;}
  .journal-layout{grid-template-columns:1fr;}
  .journal-sidebar{display:none;}
}
</style>
</head>
<body>

<!-- TICKER STRIP — PE Ratio first, then indices -->
<div class="ticker-strip" id="tickerStrip">
  <!-- NIFTY 50 -->
  <a class="ticker-item" href="https://www.tradingview.com/chart/?symbol=NSE%3ANIFTY" target="_blank" style="border-right:1px solid var(--border)">
    <span class="ticker-name">NIFTY 50</span>
    <span class="ticker-price" id="tn50">—</span>
    <span class="ticker-chg" id="tc50"></span>
  </a>
  <!-- MIDCAP 100 -->
  <a class="ticker-item" href="https://www.tradingview.com/chart/?symbol=NSE%3AMIDCPNIFTY" target="_blank" style="border-right:1px solid var(--border)">
    <span class="ticker-name">NIFTY MIDCAP 100</span>
    <span class="ticker-price" id="tnMid">—</span>
    <span class="ticker-chg" id="tcMid"></span>
  </a>
  <!-- SMALLCAP 100 -->
  <a class="ticker-item" href="https://www.tradingview.com/chart/?symbol=NSE%3ANIFTYSMLCAP100" target="_blank">
    <span class="ticker-name">NIFTY SMALLCAP 100</span>
    <span class="ticker-price" id="tnSml">—</span>
    <span class="ticker-chg" id="tcSml"></span>
  </a>
  <span style="margin-left:auto;font-size:0.55rem;color:var(--muted);padding-right:12px;flex-shrink:0" id="tickerUpdated"></span>
</div>

<header>
  <div class="logo">folio<em>.</em>live</div>
  <div class="hright">
    <div id="livePill" class="live-pill" style="display:none">
      <div class="live-dot" id="liveDot"></div>
      <span id="liveName">Live</span>
    </div>
    <span id="updatedLbl" style="font-size:0.64rem;color:var(--muted)"></span>
    <a href="https://kite.zerodha.com" target="_blank" class="btn" id="kiteBtn" style="display:none;text-decoration:none" title="Open Kite">⚡ Kite</a>
    <button class="btn" id="blurBtn" onclick="toggleBlur()" style="display:none" title="Blur/unblur ₹ values">👁 Hide ₹</button>
    <button class="btn" id="refreshBtn" onclick="forceRefresh()" style="display:none">↻ Refresh</button>
    <button class="btn" id="logoutBtn" onclick="logout()" style="display:none">Disconnect</button>
    <div class="theme-btn" onclick="toggleTheme()" title="Toggle day/night">🌙</div>
  </div>
</header>

<!-- NAV TABS -->
<div class="nav-tabs" id="navTabs" style="display:none">
  <button class="nav-tab active" onclick="showPage('overview')">📊 Overview</button>
  <button class="nav-tab" onclick="showPage('analytics')">📈 Portfolio Analytics</button>
  <button class="nav-tab" onclick="showPage('calendar')">📅 Calendar</button>
  <button class="nav-tab" onclick="showPage('sectors')">🥧 Sectors</button>
  <button class="nav-tab" onclick="showPage('journal')">📓 Journal</button>
  <button class="nav-tab" onclick="showPage('news')">📰 News</button>
</div>

<!-- CONNECT SCREEN -->
<div id="connectScreen">
  <h1>Your portfolio,<br><span>live.</span></h1>
  <p>Connect once. See everything — holdings, P&L, risk, growth, sectors, news.</p>
  <div class="steps-list">
    <div class="step-item"><div class="step-n">1</div><div class="step-t">Make sure <strong>server.py is running</strong></div></div>
    <div class="step-item"><div class="step-n">2</div><div class="step-t">Click below → <strong>Zerodha login</strong></div></div>
    <div class="step-item"><div class="step-n">3</div><div class="step-t">Session lasts until <strong>midnight</strong></div></div>
  </div>
  <button class="connect-btn-big" onclick="connectZerodha()">Connect Zerodha →</button>
  <div id="connectError" style="margin-top:12px;font-size:0.72rem;color:var(--loss);display:none"></div>
</div>

<!-- DASHBOARD -->
<div id="dashboard">

<!-- ══ PAGE: OVERVIEW ══════════════════════════════════ -->
<div class="page active" id="page-overview">
  <div class="settings-bar">
    <div class="sg">
      <label>Profit Target %</label>
      <input type="number" id="cagrTarget" value="100" min="1" max="10000" onchange="saveSettings();loadData()">
      <small style="font-size:0.58rem;color:var(--muted)">e.g. 100 = double your money</small>
    </div>
    <div class="sg"><label>Max Loss %</label><input type="number" id="maxLoss" value="10" min="1" max="50" onchange="saveSettings();loadData()"></div>
    <div class="sg"><label>Per Stock Loss %</label><input type="number" id="posLoss" value="10" min="1" max="50" onchange="saveSettings();loadData()"></div>
    <div class="sg" style="background:var(--s2);border:1px solid var(--border);border-radius:6px;padding:6px 10px;min-width:130px">
      <div style="font-size:0.58rem;font-weight:600;letter-spacing:1px;text-transform:uppercase;color:var(--muted);margin-bottom:3px">NIFTY P/E</div>
      <div id="peDisplay" style="font-family:'DM Mono',monospace;font-size:0.82rem;font-weight:600;color:var(--warn)">—</div>
      <div id="peNote" style="font-size:0.58rem;color:var(--muted);margin-top:1px">loading...</div>
    </div>
    <span class="updated-lbl" id="updatedBar"></span>
  </div>
  <div class="alert" id="alertBanner"></div>
  <div class="cards-row" id="cardsRow"></div>

  <div class="grid3">
    <div class="panel" style="margin-bottom:0">
      <div class="panel-title">Portfolio Target Progress</div>
      <div id="cagrPanel"></div>
    </div>
    <div class="panel" style="margin-bottom:0">
      <div class="panel-title">Portfolio Risk <span id="riskLimitLbl"></span></div>
      <div id="overallRisk"></div>
    </div>
    <div class="panel" style="margin-bottom:0">
      <div class="panel-title">
        Portfolio Growth Tracker
        <div style="display:flex;align-items:center;gap:6px">
          <span id="growthNotice" style="font-size:0.58rem;color:var(--muted);font-weight:400"></span>
          <div class="chart-tabs">
            <button class="ctab active" onclick="setChartTab(this,'monthly')">M</button>
            <button class="ctab" onclick="setChartTab(this,'yearly')">Y</button>
          </div>
        </div>
      </div>
      <canvas id="growthChart" height="150"></canvas>
    </div>
  </div>

  <div class="panel">
    <div class="panel-title">Holdings <span id="holdCount"></span></div>
    <div class="tbl-wrap">
      <table>
        <thead><tr>
          <th>Stock</th><th>Qty</th><th>Avg</th><th>LTP</th><th>Invested ₹</th><th>Current ₹</th><th>P&L ₹</th><th>Return</th><th>Weight</th><th>% Risk</th><th>Risk/Sell</th>
        </tr></thead>
        <tbody id="holdTbody"></tbody>
      </table>
    </div>
  </div>
</div>

<!-- ══ PAGE: ANALYTICS ════════════════════════════════ -->
<div class="page" id="page-analytics">
  <div class="panel">
    <div class="panel-title">Portfolio Analytics <span>holdings performance</span></div>
    <div id="holdingsAnalytics">
      <div style="text-align:center;padding:20px;color:var(--muted);font-size:0.78rem">Loading...</div>
    </div>
  </div>
</div>

<!-- ══ PAGE: CALENDAR ═════════════════════════════════ -->
<div class="page" id="page-calendar">
  <div class="panel">
    <div class="panel-title">
      P&L Heatmap Calendar
      <div style="display:flex;align-items:center;gap:6px">
        <button onclick="calNavYear(-1)" style="background:none;border:1px solid var(--border);color:var(--muted);padding:3px 8px;border-radius:4px;cursor:pointer;font-size:0.8rem">‹</button>
        <span id="calYearLabel" style="font-size:0.72rem;font-weight:600;min-width:40px;text-align:center"></span>
        <button onclick="calNavYear(1)" style="background:none;border:1px solid var(--border);color:var(--muted);padding:3px 8px;border-radius:4px;cursor:pointer;font-size:0.8rem">›</button>
        <button onclick="calCurrentYear=new Date().getFullYear();renderCalendar()" style="background:var(--accent);color:#fff;border:none;padding:3px 10px;border-radius:4px;cursor:pointer;font-size:0.65rem;font-weight:600">Today</button>
      </div>
    </div>
    <!-- FULL YEAR HEATMAP — all 12 months side by side like the reference -->
    <div style="overflow-x:auto;padding-bottom:8px">
      <div id="yearHeatmap" style="display:flex;gap:10px;min-width:900px"></div>
    </div>
    <!-- COLOUR LEGEND -->
    <div style="display:flex;align-items:center;gap:6px;margin:10px 0 14px">
      <span style="font-size:0.6rem;color:var(--muted)">Less</span>
      <div style="width:10px;height:10px;border-radius:2px;background:rgba(255,82,82,0.8)"></div>
      <div style="width:10px;height:10px;border-radius:2px;background:rgba(255,82,82,0.4)"></div>
      <div style="width:10px;height:10px;border-radius:2px;background:var(--s3)"></div>
      <div style="width:10px;height:10px;border-radius:2px;background:rgba(0,230,118,0.35)"></div>
      <div style="width:10px;height:10px;border-radius:2px;background:rgba(0,230,118,0.75)"></div>
      <span style="font-size:0.6rem;color:var(--muted)">More</span>
      <span style="font-size:0.6rem;color:var(--muted);margin-left:8px">🟥 Loss &nbsp; 🟩 Profit &nbsp; ⬜ No data</span>
    </div>
    <!-- SELECTED DAY DETAIL -->
    <div id="calDayDetail" style="background:var(--s2);border-radius:6px;padding:9px 13px;margin-bottom:12px;display:none;border:1px solid rgba(108,99,255,0.3)">
      <span id="calDayDetailText" style="font-size:0.76rem"></span>
    </div>
    <!-- INSIGHTS -->
    <div class="cal-insights" id="calInsights"></div>
  </div>
</div>

<!-- ══ PAGE: SECTORS ══════════════════════════════════ -->
<div class="page" id="page-sectors">
  <div class="panel">
    <div class="panel-title">
      Sector Allocation
      <div style="display:flex;align-items:center;gap:10px">
        <label style="display:flex;align-items:center;gap:5px;font-size:0.68rem;color:var(--muted);cursor:pointer;font-weight:400;text-transform:none;letter-spacing:0">
          <input type="radio" name="sectorView" value="value" checked onchange="renderSectors(lastData)" style="accent-color:var(--accent)"> Current Value
        </label>
        <label style="display:flex;align-items:center;gap:5px;font-size:0.68rem;color:var(--muted);cursor:pointer;font-weight:400;text-transform:none;letter-spacing:0">
          <input type="radio" name="sectorView" value="invested" onchange="renderSectors(lastData)" style="accent-color:var(--accent)"> Invested
        </label>
        <label style="display:flex;align-items:center;gap:5px;font-size:0.68rem;color:var(--muted);cursor:pointer;font-weight:400;text-transform:none;letter-spacing:0">
          <input type="radio" name="sectorView" value="pl" onchange="renderSectors(lastData)" style="accent-color:var(--accent)"> P&L
        </label>
      </div>
    </div>
    <div style="display:grid;grid-template-columns:280px 1fr;gap:20px;align-items:start">
      <!-- PIE CHART -->
      <div>
        <canvas id="sectorChart" width="280" height="280"></canvas>
        <div id="sectorLegend" style="margin-top:10px;display:flex;flex-direction:column;gap:4px"></div>
      </div>
      <!-- SECTOR LIST + DETAIL -->
      <div>
        <div id="sectorRows" style="margin-bottom:12px"></div>
        <div id="sectorDetails" style="display:none;background:var(--s2);border-radius:8px;padding:14px;border:1px solid var(--border);margin-bottom:12px"></div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px" id="sectorSummary"></div>
      </div>
    </div>
  </div>
</div>

<!-- ══ PAGE: JOURNAL ══════════════════════════════════ -->
<div class="page" id="page-journal">
  <div class="panel">
    <div class="panel-title" style="margin-bottom:10px">
      Investment Journal
      <div class="journal-save-status" id="journalSaveStatus"></div>
    </div>
    <div class="journal-layout">

      <!-- SIDEBAR: mini calendar -->
      <div class="journal-sidebar">
        <div class="journal-mini-cal" id="journalMiniCal"></div>
      </div>

      <!-- MAIN: editor -->
      <div class="journal-main">
        <!-- Date + search row -->
        <div style="display:flex;align-items:center;gap:10px">
          <div class="journal-date" id="journalDate"></div>
          <input class="journal-search" id="journalSearch" placeholder="🔍 Search entries..." oninput="searchJournal(this.value)" style="max-width:260px">
        </div>
        <div class="search-results" id="searchResults"></div>

        <!-- FORMATTING TOOLBAR -->
        <div class="journal-toolbar">
          <button class="jtool" onclick="jInsert('# ','',true)" title="Heading 1">H1</button>
          <button class="jtool" onclick="jInsert('## ','',true)" title="Heading 2">H2</button>
          <button class="jtool" onclick="jInsert('### ','',true)" title="Heading 3">H3</button>
          <div class="jtool-sep"></div>
          <button class="jtool" onclick="jWrap('**','**')" title="Bold"><strong>B</strong></button>
          <button class="jtool" onclick="jWrap('_','_')" title="Italic"><em>I</em></button>
          <button class="jtool" onclick="jWrap('~~','~~')" title="Strikethrough"><s>S</s></button>
          <div class="jtool-sep"></div>
          <button class="jtool" onclick="jInsert('• ','',true)" title="Bullet point">• Bullet</button>
          <button class="jtool" onclick="jInsert('- [ ] ','',true)" title="Checkbox">☐ Todo</button>
          <button class="jtool" onclick="jNumberedList()" title="Numbered list">1. List</button>
          <div class="jtool-sep"></div>
          <button class="jtool" onclick="jInsert('---
','',true)" title="Divider">— Rule</button>
          <button class="jtool" onclick="jInsert('> ','',true)" title="Quote">❝ Quote</button>
          <button class="jtool" onclick="jInsert('📈 ','',true)" title="Trade note">📈 Trade</button>
          <button class="jtool" onclick="jInsert('⚠️ ','',true)" title="Risk note">⚠️ Risk</button>
          <button class="jtool" onclick="jInsert('💡 ','',true)" title="Idea">💡 Idea</button>
          <div class="jtool-sep"></div>
          <button class="jtool" onclick="jClear()" title="Clear entry" style="color:var(--loss)">✕ Clear</button>
        </div>

        <!-- TEXTAREA — fills remaining height -->
        <textarea class="journal-textarea" id="journalEntry"
          placeholder="Write your thoughts, trade notes, market observations...&#10;&#10;Use the toolbar above to add headers, bullet points, and more."
          oninput="autoSaveJournal()"></textarea>
      </div>
    </div>
  </div>
</div>

<!-- ══ PAGE: NEWS ═════════════════════════════════════ -->
<div class="page" id="page-news">
  <!-- SUB-TABS -->
  <div style="display:flex;gap:4px;margin-bottom:12px;border-bottom:1px solid var(--border);padding-bottom:0">
    <button class="nav-tab active" id="nstab-news" onclick="switchNewsTab('news',this)" style="font-size:0.7rem;padding:7px 14px">📰 Stock News</button>
    <button class="nav-tab" id="nstab-twitter" onclick="switchNewsTab('twitter',this)" style="font-size:0.7rem;padding:7px 14px">🐦 @Prakashplutus</button>
    <button class="nav-tab" id="nstab-events" onclick="switchNewsTab('events',this)" style="font-size:0.7rem;padding:7px 14px">📆 Upcoming Events</button>
  </div>

  <!-- STOCK NEWS SUB-PAGE -->
  <div id="nspage-news">
    <div class="panel">
      <div class="panel-title">Stock News <span id="newsNote">for your holdings</span></div>
      <!-- TAG FILTERS -->
      <div class="news-filter-bar">
        <span style="font-size:0.6rem;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:0.5px">Filter:</span>
        <button class="nfbtn active" onclick="setNewsTagFilter('all',this)">All</button>
        <button class="nfbtn" onclick="setNewsTagFilter('target_up',this)">🎯 Target ↑</button>
        <button class="nfbtn" onclick="setNewsTagFilter('target_dn',this)">📉 Target ↓</button>
        <button class="nfbtn" onclick="setNewsTagFilter('neutral',this)">Neutral</button>
        <button class="nfbtn" onclick="setNewsTagFilter('announcement',this)">📢 Announce</button>
        <button class="nfbtn" onclick="setNewsTagFilter('quarterly',this)">📊 Quarterly</button>
        <button class="nfbtn" onclick="setNewsTagFilter('dividend',this)">💰 Dividend</button>
        <button class="nfbtn" onclick="setNewsTagFilter('split',this)">✂️ Split</button>
        <button class="nfbtn" onclick="setNewsTagFilter('bonus',this)">🎁 Bonus</button>
      </div>
      <!-- TICKER FILTER -->
      <div class="nf-ticker-wrap" id="newsTickerFilter"></div>
      <div id="newsContainer">
        <div style="text-align:center;padding:30px;color:var(--muted);font-size:0.78rem">Loading news...</div>
      </div>
    </div>
  </div>

  <!-- TWITTER SUB-PAGE -->
  <div id="nspage-twitter" style="display:none">
    <div class="panel">
      <div class="panel-title">@Prakashplutus — Twitter / X Feed</div>
      <div style="text-align:center;padding:20px 0 10px">
        <a href="https://twitter.com/Prakashplutus" target="_blank"
           style="display:inline-flex;align-items:center;gap:8px;background:var(--s2);border:1px solid var(--border);color:var(--text);padding:9px 18px;border-radius:7px;text-decoration:none;font-size:0.8rem;font-weight:600;transition:all 0.15s"
           onmouseover="this.style.borderColor='var(--accent)'" onmouseout="this.style.borderColor='var(--border)'">
          <span style="font-size:1.1rem">𝕏</span> Open @Prakashplutus on Twitter/X
        </a>
      </div>
      <div style="margin-top:12px">
        <div style="font-size:0.65rem;color:var(--muted);margin-bottom:10px;text-align:center">Embedded timeline · Live tweets from @Prakashplutus</div>
        <div style="border:1px solid var(--border);border-radius:8px;overflow:hidden;min-height:400px;background:var(--s2)">
          <a class="twitter-timeline"
             id="twtTimelineEmbed"
             data-height="600"
             data-chrome="noheader nofooter transparent"
             href="https://twitter.com/Prakashplutus?ref_src=twsrc%5Etfw">Tweets by Prakashplutus</a>
          <script async src="https://platform.twitter.com/widgets.js" charset="utf-8"></script>
        </div>
      </div>
      <div style="margin-top:12px;padding:10px;background:var(--s2);border-radius:6px;border:1px solid var(--border)">
        <div style="font-size:0.65rem;color:var(--muted);margin-bottom:6px;font-weight:600">Quick Links</div>
        <div style="display:flex;flex-wrap:wrap;gap:8px">
          <a href="https://twitter.com/Prakashplutus" target="_blank" style="font-size:0.7rem;color:var(--accent);text-decoration:none">→ Profile</a>
          <a href="https://twitter.com/search?q=from%3APrakashplutus+stock&f=live" target="_blank" style="font-size:0.7rem;color:var(--accent);text-decoration:none">→ Stock tweets</a>
          <a href="https://twitter.com/search?q=from%3APrakashplutus+buy&f=live" target="_blank" style="font-size:0.7rem;color:var(--accent);text-decoration:none">→ Buy calls</a>
          <a href="https://twitter.com/search?q=from%3APrakashplutus+target&f=live" target="_blank" style="font-size:0.7rem;color:var(--accent);text-decoration:none">→ Targets</a>
        </div>
      </div>
    </div>
  </div>

  <!-- UPCOMING EVENTS SUB-PAGE -->
  <div id="nspage-events" style="display:none">
    <div class="panel">
      <div class="panel-title">Upcoming Events <span id="eventsNote">for your portfolio</span>
        <button onclick="loadUpcomingEvents()" style="font-size:0.65rem;padding:3px 9px;border-radius:4px;border:1px solid var(--border);background:transparent;color:var(--accent);cursor:pointer">↻ Refresh</button>
      </div>
      <div id="eventsContainer">
        <div style="text-align:center;padding:30px;color:var(--muted);font-size:0.78rem">Click Upcoming Events tab to load...</div>
      </div>
    </div>
  </div>
</div>

</div><!-- end dashboard -->

<!-- RISK EDIT MODAL -->
<div class="modal-overlay" id="riskModal">
  <div class="modal">
    <h3>Set Risk Limit</h3>
    <p id="riskModalDesc">Custom loss % limit. Leave blank for global default.</p>
    <input type="number" id="riskModalInput" placeholder="e.g. 15" min="1" max="100" step="0.5">
    <div class="modal-btns">
      <button class="mbtn-save" onclick="saveStockRisk()">Save</button>
      <button class="mbtn-cancel" onclick="closeRiskModal()">Cancel</button>
    </div>
  </div>
</div>

<footer style="border-top:1px solid var(--border);padding:12px 16px;display:flex;justify-content:space-between;align-items:center;">
  <p style="font-size:0.62rem;color:var(--muted)">folio.live · Kite Connect API · Refreshes every 60s · Token expires midnight</p>
  <p style="font-size:0.62rem;color:var(--muted)" id="footerUser"></p>
</footer>

<script>
// ── STATE ──────────────────────────────────────────────
let growthChart=null, sectorChart=null;
let chartMode='monthly', isDark=true, autoTimer=null;
let lastData=null, stockRiskLimits={}, riskModalTicker='';
let journalData={}, journalCurrentDate='', journalSaveTimer=null;
let calCurrentYear=new Date().getFullYear(), calCurrentMonth=new Date().getMonth();
let activeSectors=new Set();
let blurActive=false, newsTagFilter='all', newsTickerFilter='all';
// NSE Sector Map — comprehensive list of NSE-listed stocks by sector
// Add your specific holdings here if missing
const SECTOR_MAP = {
  // Common Kite API ticker name variants
  'NATCOPHARM':'Pharma & Healthcare','SUNPHARMA':'Pharma & Healthcare',
  'ONGC':'Oil & Gas','MRPL':'Oil & Gas','CPCB':'Oil & Gas',
  'TATAMOTORS':'Automobiles','TATAMTRDVR':'Automobiles',
  'HDFCBANK':'Financial Services','ICICIBANK':'Financial Services',
  'WIPRO':'Information Technology','HCLTECH':'Information Technology',
  'KIRLOSENG':'Capital Goods','KIRLOSBROS':'Capital Goods','KIRLOSKAR':'Capital Goods',
  'GVT&D':'Capital Goods','GVTD':'Capital Goods',
  'MBAPL':'Capital Goods',
  'ANURAS':'Chemicals',  // Anupam Rasayan India — Specialty Chemicals
  'VENUSREM':'Pharma & Healthcare',
  // Financial Services
  'HDFCBANK':'Financial Services','SBIN':'Financial Services','ICICIBANK':'Financial Services',
  'KOTAKBANK':'Financial Services','AXISBANK':'Financial Services','INDUSINDBK':'Financial Services',
  'BAJFINANCE':'Financial Services','BAJAJFINSV':'Financial Services','SBICARD':'Financial Services',
  'CHOLAFIN':'Financial Services','MUTHOOTFIN':'Financial Services','MANAPPURAM':'Financial Services',
  'LICHSGFIN':'Financial Services','RECLTD':'Financial Services','PFC':'Financial Services',
  // Energy & Oil
  'RELIANCE':'Oil & Gas','ONGC':'Oil & Gas','IOC':'Oil & Gas','BPCL':'Oil & Gas','CPCL':'Oil & Gas',
  'MRPL':'Oil & Gas','GAIL':'Oil & Gas','OIL':'Oil & Gas','PETRONET':'Oil & Gas','IGL':'Oil & Gas','MGL':'Oil & Gas',
  'HINDPETRO':'Oil & Gas','CASTROLIND':'Oil & Gas','GSPL':'Oil & Gas',
  // Information Technology
  'INFY':'Information Technology','TCS':'Information Technology','WIPRO':'Information Technology',
  'TECHM':'Information Technology','HCLTECH':'Information Technology','LTI':'Information Technology',
  'MPHASIS':'Information Technology','COFORGE':'Information Technology','PERSISTENT':'Information Technology',
  'LTIMINDTREE':'Information Technology','OFSS':'Information Technology',
  // Pharma & Healthcare
  'SUNPHARMA':'Pharma & Healthcare','CIPLA':'Pharma & Healthcare','NATCO':'Pharma & Healthcare','NATCOPHARM':'Pharma & Healthcare','NATCOPHARMA':'Pharma & Healthcare',
  'DRREDDY':'Pharma & Healthcare','BIOCON':'Pharma & Healthcare','LUPIN':'Pharma & Healthcare',
  'AUROPHARMA':'Pharma & Healthcare','DIVISLAB':'Pharma & Healthcare','TORNTPHARM':'Pharma & Healthcare',
  'ABBOTINDIA':'Pharma & Healthcare','IPCALAB':'Pharma & Healthcare','ALKEM':'Pharma & Healthcare',
  'VENUSREM':'Pharma & Healthcare','SYNGENE':'Pharma & Healthcare','METROPOLIS':'Pharma & Healthcare',
  // Automobiles
  'TATAMOTOR':'Automobiles','MARUTI':'Automobiles','BAJAJ-AUTO':'Automobiles',
  'HEROMOTOCO':'Automobiles','EICHERMOT':'Automobiles','M&M':'Automobiles',
  'FORCEMOT':'Automobiles','ASHOKLEY':'Automobiles','ESCORTS':'Automobiles',
  'BALKRISIND':'Automobiles','MRF':'Automobiles','APOLLOTYRE':'Automobiles',
  // Capital Goods & Engineering
  'GVT&D':'Capital Goods','L&T':'Capital Goods','BEL':'Capital Goods','HAL':'Capital Goods',
  'BHEL':'Capital Goods','SIEMENS':'Capital Goods','ABB':'Capital Goods',
  'THERMAX':'Capital Goods','CUMMINSIND':'Capital Goods','KAJARIACER':'Capital Goods',
  'GRINDWELL':'Capital Goods','APLAPOLLO':'Capital Goods',
  // FMCG
  'HINDUNILVR':'FMCG','ITC':'FMCG','NESTLEIND':'FMCG','BRITANNIA':'FMCG',
  'DABUR':'FMCG','MARICO':'FMCG','GODREJCP':'FMCG','COLPAL':'FMCG',
  'EMAMILTD':'FMCG','TATACONSUM':'FMCG','VBL':'FMCG','UBL':'FMCG',
  // Metals & Mining
  'TATASTEEL':'Metals','JSWSTEEL':'Metals','HINDALCO':'Metals','VEDL':'Metals',
  'NATIONALUM':'Metals','SAIL':'Metals','NMDC':'Metals','COALINDIA':'Metals',
  'JINDALSTEL':'Metals','APLAPOLLO':'Metals',
  // Real Estate
  'DLF':'Real Estate','GODREJPROP':'Real Estate','OBEROIRLTY':'Real Estate',
  'PRESTIGE':'Real Estate','PHOENIXLTD':'Real Estate','BRIGADE':'Real Estate',
  // Cement
  'ULTRACEMCO':'Cement','SHREECEM':'Cement','AMBUJACEM':'Cement',
  'ACC':'Cement','DALMIACELE':'Cement','RAMCOCEM':'Cement',
  // Telecom
  'BHARTIARTL':'Telecom','IDEA':'Telecom','TATACOMM':'Telecom',
  // Consumer Durables
  'HAVELLS':'Consumer Durables','VOLTAS':'Consumer Durables','WHIRLPOOL':'Consumer Durables',
  'BLUESTARCO':'Consumer Durables','CROMPTON':'Consumer Durables','AMBER':'Consumer Durables',
  // Chemicals — Specialty & Agrochemicals
  'ANURAS':'Chemicals','ANUPAMR':'Chemicals','ANUPAM':'Chemicals',
  'PIDILITIND':'Chemicals','ASIANPAINT':'Chemicals','BERGEPAINT':'Chemicals',
  'AARTIIND':'Chemicals','DEEPAKNTR':'Chemicals','VINATIORGA':'Chemicals',
  'CLEAN':'Chemicals','ROSSARI':'Chemicals','TATACHEM':'Chemicals',
  'NOCIL':'Chemicals','ALKYLAMINE':'Chemicals','FINEORG':'Chemicals',
  // Insurance
  'HDFCLIFE':'Insurance','SBILIFE':'Insurance','ICICIGI':'Insurance',
  'ICICIPRULI':'Insurance','LICI':'Insurance','GICRE':'Insurance',
};

// Smart sector guesser for unknown tickers
function getSector(ticker){
  if(SECTOR_MAP[ticker]) return SECTOR_MAP[ticker];
  const t = ticker.toUpperCase().replace(/[&-]/g,'');
  // Pattern match to proper sector names — never return ticker name
  if(/BANK|FIN|CREDIT|LOAN|LEASING|INVEST|ASSET|WEALTH|CAPITAL/.test(t)) return 'Financial Services';
  if(/PHARMA|DRUG|LAB|BIOTECH|MEDIC|HEALTH|HOSPITAL|DIAGNOS|SURG|NATCO/.test(t)) return 'Pharma & Healthcare';
  if(/TECH|INFOSY|SOFT|DIGIT|DATA|CYBER|IT|SYST|COMPUT/.test(t)) return 'Information Technology';
  if(/AUTO|MOTOR|VEHICL|TRACTOR|TYRE|WHEEL|GEAR|PISTON|BRAKE/.test(t)) return 'Automobiles';
  if(/STEEL|METAL|IRON|COPPER|ZINC|ALUM|ALLOY|CAST|MINING|MINERAL/.test(t)) return 'Metals & Mining';
  if(/CEMENT|CONCRET|CONSTRUCT|BUILD|INFRA/.test(t)) return 'Infrastructure';
  if(/POWER|ENERGY|SOLAR|WIND|ELECTR|TRANSMIS|GENERAT/.test(t)) return 'Energy & Power';
  if(/GAS|OIL|PETRO|REFIN|FUEL|LUBRIC|HPCL|BPCL|IOCL/.test(t)) return 'Oil & Gas';
  if(/REAL|PROP|REALTY|ESTATE|HOUS|LAND|DEVEL/.test(t)) return 'Real Estate';
  if(/RETAIL|SHOP|MARKET|STORE|ECOMM|TRADE/.test(t)) return 'Retail & Consumer';
  if(/FOOD|BEVERAGE|DRINK|AGRO|SUGAR|RICE|DAIRY/.test(t)) return 'FMCG & Food';
  if(/CHEM|FERTIL|PESTICI|PAINT|COAT|PLASTIC/.test(t)) return 'Chemicals';
  if(/TELECOM|MOBILE|NETWORK|BROADBAND|FIBRE/.test(t)) return 'Telecom';
  if(/INSUR|LIFE|GENERAL/.test(t)) return 'Insurance';
  if(/MEDIA|NEWS|PUBLISH|FILM|ENTERTAIN/.test(t)) return 'Media & Entertainment';
  if(/HOTEL|RESORT|TRAVEL|TOUR|HOSPITALITY/.test(t)) return 'Hospitality';
  if(/TEXTILE|FABRIC|GARMENT|COTTON|YARN/.test(t)) return 'Textiles';
  return 'Diversified'; // fallback is "Diversified" not the ticker name
}

// ── FORMAT ─────────────────────────────────────────────
const fmtL=n=>{const a=Math.abs(n),s=n<0?'-':'';if(a>=10000000)return s+(a/10000000).toFixed(2)+'Cr';if(a>=100000)return s+(a/100000).toFixed(2)+'L';if(a>=1000)return s+(a/1000).toFixed(1)+'K';return (n<0?'-':'')+'₹'+a.toFixed(0);};
const pct=(n,d=1)=>(n>=0?'+':'')+n.toFixed(d)+'%';
const gc=n=>n>=0?'g':'l';
const gclr=n=>n>=0?'var(--gain)':'var(--loss)';

// ── THEME ──────────────────────────────────────────────
function toggleTheme(){
  isDark=!isDark;
  document.body.classList.toggle('light',!isDark);
  document.querySelector('.theme-btn').textContent=isDark?'🌙':'☀️';
  try{localStorage.setItem('folio_theme',isDark?'dark':'light');}catch(e){}
  if(lastData)renderGrowthChart(lastData);
}

// ── BLUR TOGGLE ────────────────────────────────────────
function toggleBlur(){
  blurActive=!blurActive;
  document.body.classList.toggle('blur-mode',blurActive);
  const btn=document.getElementById('blurBtn');
  if(btn){btn.textContent=blurActive?'👁 Show ₹':'👁 Hide ₹';btn.style.borderColor=blurActive?'var(--warn)':'';btn.style.color=blurActive?'var(--warn)':'';}
  // Wire up click-to-peek on all blur-val elements when blur is active
  document.querySelectorAll('.blur-val').forEach(el=>{
    if(blurActive){
      el.style.cursor='pointer';
      el.title='Click to peek';
      el._blurClick = function(e){
        e.stopPropagation();
        this.classList.toggle('peek');
        // Auto-rehide after 2s
        if(this.classList.contains('peek')){
          clearTimeout(this._peekTimer);
          this._peekTimer=setTimeout(()=>this.classList.remove('peek'),2000);
        }
      };
      el.addEventListener('click',el._blurClick);
    } else {
      el.style.cursor='';
      el.title='';
      el.classList.remove('peek');
      if(el._blurClick){el.removeEventListener('click',el._blurClick);delete el._blurClick;}
    }
  });
}
}

// ── NAV ────────────────────────────────────────────────
function showPage(name){
  document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.nav-tab').forEach(t=>t.classList.remove('active'));
  document.getElementById('page-'+name).classList.add('active');
  event.target.classList.add('active');
  if(name==='sectors'&&lastData) renderSectors(lastData);
  if(name==='calendar') renderCalendar();
  if(name==='journal') initJournal();
  if(name==='news'&&lastData) loadNews(lastData);
  if(name==='analytics'&&lastData) renderHoldingsAnalytics(lastData);
}

// ── AUTH ───────────────────────────────────────────────
async function checkAuth(){
  try{
    const r=await fetch('/auth/status');const d=await r.json();
    if(d.connected){showDashboard(d.user_name);loadData();startAuto();}
    else showConnect();
  }catch(e){showConnect();document.getElementById('connectError').textContent='Cannot reach server.';document.getElementById('connectError').style.display='block';}
}
function connectZerodha(){window.location.href='/auth/login';}
async function logout(){await fetch('/auth/logout');stopAuto();showConnect();}
async function forceRefresh(){await fetch('/api/refresh');loadData();}
function showConnect(){
  document.getElementById('connectScreen').style.display='flex';
  document.getElementById('dashboard').style.display='none';
  document.getElementById('navTabs').style.display='none';
  document.getElementById('livePill').style.display='none';
  document.getElementById('refreshBtn').style.display='none';
  document.getElementById('logoutBtn').style.display='none';
}
function showDashboard(name){
  document.getElementById('connectScreen').style.display='none';
  document.getElementById('dashboard').style.display='block';
  document.getElementById('navTabs').style.display='flex';
  document.getElementById('livePill').style.display='flex';
  document.getElementById('liveName').textContent=(name||'')+' · Live';
  document.getElementById('refreshBtn').style.display='inline-block';
  document.getElementById('logoutBtn').style.display='inline-block';
  document.getElementById('blurBtn').style.display='inline-block';
  document.getElementById('kiteBtn').style.display='inline-block';
  document.getElementById('footerUser').textContent=name||'';
}
let tickerTimer=null;
async function fetchNiftyPE(){
  try{
    const r = await fetch('/api/nifty-pe');
    const d = await r.json();
    const peDisp = document.getElementById('peDisplay');
    const peNote = document.getElementById('peNote');
    if(d.pe && peDisp){
      peDisp.textContent = d.pe.toFixed(1)+'x';
      const pe = d.pe;
      const note = pe > 25 ? '⚠️ Expensive' : pe > 20 ? '~ Fair value' : '✅ Cheap';
      if(peNote) peNote.textContent = note + (d.pb ? ` · PB ${d.pb}x` : '');
    } else if(peDisp){
      peDisp.textContent = '—';
      if(peNote) peNote.textContent = 'unavailable';
    }
  }catch(e){
    const peNote = document.getElementById('peNote');
    if(peNote) peNote.textContent = 'unavailable';
  }
}

function startAuto(){
  stopAuto();
  autoTimer=setInterval(loadData,60000);
  updateIndexTickers();  // fetch immediately
  tickerTimer=setInterval(updateIndexTickers,300000); // refresh every 5 min
}
function stopAutoTicker(){if(tickerTimer){clearInterval(tickerTimer);tickerTimer=null;}}
function stopAuto(){if(autoTimer){clearInterval(autoTimer);autoTimer=null;}}

// ── LOAD DATA ──────────────────────────────────────────
async function loadData(){
  const params=new URLSearchParams({cagr_target:document.getElementById('cagrTarget').value,max_loss_pct:document.getElementById('maxLoss').value,pos_loss_pct:document.getElementById('posLoss').value,invested_since:'2020-01-01'});
  try{
    const r=await fetch(`/api/summary?${params}`);const d=await r.json();
    if(d.error){
      console.error('API error:', d.error, d.detail||'');
      // Still show dashboard with error message
      document.getElementById('alertBanner').className='alert alert-danger show';
      document.getElementById('alertBanner').innerHTML=`⚠️ <strong>Data error:</strong> ${d.error}`;
      return;
    }
    lastData=d;
    if(d.stock_risks)stockRiskLimits=d.stock_risks;
    renderOverview(d);
    const t=new Date().toLocaleTimeString('en-IN');
    document.getElementById('updatedBar').textContent='Updated '+t;
    document.getElementById('updatedLbl').textContent=t;
  }catch(e){console.error(e);}
}

// ── INDEX TICKERS (estimated from holdings data) ───────
async function updateIndexTickers(){
  try{
    const r = await fetch('/api/ticker');
    const d = await r.json();
    function setTicker(priceId, chgId, data){
      const pe = document.getElementById('pe50');
      const priceEl = document.getElementById(priceId);
      const chgEl   = document.getElementById(chgId);
      if(!data||!data.price) return;
      if(priceEl) priceEl.textContent = data.price.toLocaleString('en-IN',{maximumFractionDigits:2});
      if(chgEl){
        const up = data.change >= 0;
        chgEl.textContent = (up?'▲ +':'▼ ')+Math.abs(data.changePct).toFixed(2)+'%';
        chgEl.style.color = up ? 'var(--gain)' : 'var(--loss)';
      }
    }
    setTicker('tn50',  'tc50',  d.NIFTY50);
    setTicker('tnMid', 'tcMid', d.MIDCAP100);
    setTicker('tnSml', 'tcSml', d.SMALLCAP);
    // PE ratio — only in settings bar now
    fetchNiftyPE();
    const upEl = document.getElementById('tickerUpdated');
    if(upEl) upEl.textContent = 'Updated '+new Date().toLocaleTimeString('en-IN',{hour:'2-digit',minute:'2-digit'});
    // Also update PE in settings bar
    const peDisp = document.getElementById('peDisplay');
    const peNote = document.getElementById('peNote');
    if(peDisp && d.NIFTY50?.pe){
      peDisp.textContent = d.NIFTY50.pe.toFixed(1)+'x';
      const pe = d.NIFTY50.pe;
      let note = pe > 25 ? '⚠ Expensive' : pe > 20 ? 'Fair value' : '✓ Cheap';
      if(peNote) peNote.textContent = note;
    }
  }catch(e){ console.log('Ticker fetch failed:', e); }
}

// ── RENDER OVERVIEW ────────────────────────────────────
function renderOverview(d){
  const p=d.portfolio,rs=d.risk,st=d.settings;

  // ALERT
  const ab=document.getElementById('alertBanner');
  if(rs.stop_investing){ab.className='alert alert-danger show';ab.innerHTML=`🚨 <strong>STOP INVESTING.</strong> Loss of ${fmtL(rs.actual_loss)} exceeds ${st.max_loss_pct}% limit on ₹${fmtL(p.total_capital)} total capital.`;}
  else if(rs.loss_used_pct>70){ab.className='alert alert-warn show';ab.innerHTML=`⚠️ <strong>Caution.</strong> ${rs.loss_used_pct.toFixed(0)}% of loss budget used. Safety cushion: ${fmtL(rs.loss_remaining)}.`;}
  else{ab.className='alert alert-ok show';ab.innerHTML=`✅ <strong>All clear.</strong> Within risk limits. Total capital: <span class="blur-val">${fmtL(p.total_capital)}</span> · Cash: <span class="blur-val">${fmtL(p.cash_available)}</span>`;}

  // Daily P&L estimate (today's change = current value minus yesterday's snapshot)
  const hist=d.history||[];
  let dailyPL=0,dailyPct=0;
  if(hist.length>=1){
    const yest=hist[hist.length-1];
    dailyPL=p.total_value-(yest.value||p.total_value);
    dailyPct=yest.value>0?(dailyPL/yest.value*100):0;
  }

  // CARDS — blur-val on ₹ amounts; keep % visible; exclude Avg/LTP (those are in table)
  document.getElementById('cardsRow').innerHTML=[
    ['Live Value',`<span class="blur-val">${fmtL(p.total_value)}</span>`,null,null],
    ['Total Capital',`<span class="blur-val">${fmtL(p.total_capital)}</span>`,'Holdings + Cash',null],
    ['Invested',`<span class="blur-val">${fmtL(p.total_cost)}</span>`,p.holdings_count+' stocks',null],
    ['P&L',`<span class="blur-val">${(p.total_pl>=0?'+':'')+fmtL(p.total_pl)}</span>`,pct(p.total_pl_pct),gc(p.total_pl)],
    ['Today\'s P&L',`<span class="blur-val">${(dailyPL>=0?'+':'')+fmtL(dailyPL)}</span>`,pct(dailyPct,2),gc(dailyPL)],
    ['Cash',`<span class="blur-val">${fmtL(p.cash_available)}</span>`,'Available',null],
  ].map(([l,v,s,c])=>`<div class="card"><div class="card-lbl">${l}</div><div class="card-val ${c||''}">${v}</div>${s?`<div class="card-sub">${s}</div>`:''}</div>`).join('');

  // PROFIT TARGET PANEL — projected value = cost × (1 + target/100)
  const profitMet = p.total_pl_pct >= st.cagr_target;
  const progW     = Math.min(p.total_pl_pct / Math.max(st.cagr_target,1) * 100, 100);
  const progClr   = profitMet ? 'var(--gain)' : p.total_pl_pct > st.cagr_target*0.7 ? 'var(--warn)' : p.total_pl_pct > 0 ? 'var(--warn)' : 'var(--loss)';
  const remaining = (st.cagr_target - p.total_pl_pct).toFixed(1);
  const projectedVal = p.total_cost * (1 + st.cagr_target/100);
  const neededGain = projectedVal - p.total_value;
  document.getElementById('cagrPanel').innerHTML=`
    <div style="text-align:center;padding:10px 0 6px">
      <div class="cagr-num" style="color:${progClr}">${p.total_pl_pct.toFixed(1)}%</div>
      <div style="font-size:0.68rem;color:var(--muted);margin-top:4px">${profitMet ? '🎯 Profit target reached!' : 'Need '+remaining+'% more to hit '+st.cagr_target+'% target'}</div>
    </div>
    <div class="cagr-bar-wrap"><div class="cagr-bar-fill" style="width:${progW}%;background:${progClr}"></div></div>
    <div style="margin:8px 0 4px;padding:8px 10px;background:var(--s2);border-radius:6px;border:2px solid ${progClr};display:flex;justify-content:space-between;align-items:center">
      <div>
        <div style="font-size:0.57rem;color:var(--muted);text-transform:uppercase;letter-spacing:0.8px;margin-bottom:2px">🎯 Target Portfolio Value</div>
        <div style="font-size:1rem;font-weight:800;color:${progClr}" class="blur-val">${fmtL(projectedVal)}</div>
      </div>
      <div style="text-align:right">
        <div style="font-size:0.57rem;color:var(--muted);text-transform:uppercase;letter-spacing:0.8px;margin-bottom:2px">${profitMet?'Exceeded by':'Still need'}</div>
        <div style="font-size:0.88rem;font-weight:700;color:${profitMet?'var(--gain)':'var(--loss)'}" class="blur-val">${profitMet?'+'+fmtL(p.total_value-projectedVal):fmtL(neededGain)}</div>
      </div>
    </div>
    <div class="cagr-stats">
      <div class="cagr-stat"><div class="val" style="color:${gclr(p.total_pl_pct)}">${pct(p.total_pl_pct)}</div><div class="lbl">P&L %</div></div>
      <div class="cagr-stat"><div class="val">${st.cagr_target}%</div><div class="lbl">Target</div></div>
      <div class="cagr-stat"><div class="val blur-val" style="color:${gclr(p.total_pl)}">${p.total_pl>=0?'+':''}${fmtL(p.total_pl)}</div><div class="lbl">P&L ₹</div></div>
      <div class="cagr-stat" style="border:1px solid ${progClr}40"><div class="val" style="color:${progClr}">${profitMet?'✓ Met':remaining+'% left'}</div><div class="lbl">Gap</div></div>
    </div>`;

  // PORTFOLIO RISK
  document.getElementById('riskLimitLbl').textContent='Limit: '+st.max_loss_pct+'%';
  const rClr=rs.stop_investing?'var(--loss)':rs.loss_used_pct>80?'var(--loss)':rs.loss_used_pct>60?'var(--orange)':rs.loss_used_pct>40?'var(--warn)':'var(--gain)';
  const rIcon=rs.stop_investing?'🚨':rs.loss_used_pct>60?'⚠️':'✅';
  const rMsg=rs.stop_investing?'Stop Investing':rs.loss_used_pct>80?'High Risk':rs.loss_used_pct>60?'Caution':rs.loss_used_pct>40?'Watch':'Safe';
  document.getElementById('overallRisk').innerHTML=`
    <div style="text-align:center;padding:10px 0 8px">
      <div style="font-size:1.8rem;margin-bottom:4px">${rIcon}</div>
      <div class="risk-big-num" style="color:${rClr}">${rMsg}</div>
      <div class="risk-status-lbl">${rs.loss_used_pct.toFixed(0)}% of ${st.max_loss_pct}% limit used</div>
    </div>
    <div style="background:var(--s3);border-radius:3px;height:6px;margin:8px 0 12px;overflow:hidden">
      <div style="height:100%;width:${Math.min(rs.loss_used_pct,100)}%;background:${rClr};border-radius:3px;transition:width 1s ease"></div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:7px">
      <div style="background:var(--s2);border-radius:5px;padding:8px;text-align:center">
        <div style="font-size:0.6rem;color:var(--muted);margin-bottom:2px">TOTAL CAPITAL</div>
        <div style="font-weight:700;font-size:0.82rem" class="blur-val">${fmtL(p.total_capital)}</div>
      </div>
      <div style="background:var(--s2);border-radius:5px;padding:8px;text-align:center">
        <div style="font-size:0.6rem;color:var(--muted);margin-bottom:2px">MAX LOSS LIMIT</div>
        <div style="font-weight:700;font-size:0.82rem;color:var(--loss)" class="blur-val">${fmtL(rs.max_loss_amt)}</div>
      </div>
      <div style="background:var(--s2);border-radius:5px;padding:8px;text-align:center">
        <div style="font-size:0.6rem;color:var(--muted);margin-bottom:2px">CURRENT LOSS</div>
        <div style="font-weight:700;font-size:0.82rem;color:${rs.actual_loss>0?'var(--loss)':'var(--gain)'}" class="blur-val">${rs.actual_loss>0?fmtL(rs.actual_loss):'None'}</div>
      </div>
      <div style="background:var(--s2);border-radius:5px;padding:8px;text-align:center;border:1px solid ${rClr}40">
        <div style="font-size:0.6rem;color:var(--muted);margin-bottom:2px">LOSS LIMIT LEFT</div>
        <div style="font-weight:700;font-size:0.82rem;color:${rClr}" class="blur-val">${fmtL(rs.loss_remaining)}</div>
      </div>
    </div>
    ${rs.breaching_count>0?`<div style="margin-top:8px;padding:7px 10px;background:var(--loss-bg);border-radius:5px;border-left:3px solid var(--loss);font-size:0.7rem;color:var(--loss)">⚠ ${rs.breaching_count} stock${rs.breaching_count>1?'s':''} breaching limit</div>`:''}`;

  // GROWTH CHART
  renderGrowthChart(d);

  // HOLDINGS TABLE
  document.getElementById('holdCount').textContent=d.holdings.length+' stocks';
  const pl=st.pos_loss_pct;
  document.getElementById('holdTbody').innerHTML=d.holdings.map(h=>{
    const customLimit=stockRiskLimits[h.tradingsymbol]||pl;
    const lp=Math.abs(h.pnl_pct);
    const ratio=lp/customLimit;
    const barClr=h.pnl_pct>=0?'var(--gain)':ratio>=1?'var(--loss)':ratio>=0.8?'var(--orange)':ratio>=0.5?'var(--warn)':'var(--gain)';
    const badge=h.pnl_pct>=0?`<span class="sri-badge badge-ok">OK</span>`:ratio>=1?`<span class="sri-badge badge-bad">🔴 BREACH</span>`:ratio>=0.8?`<span class="sri-badge badge-orange">🟠 SOON</span>`:ratio>=0.5?`<span class="sri-badge badge-warn">🟡 WATCH</span>`:`<span class="sri-badge badge-ok">🟢 OK</span>`;
    const exchange=h.exchange||'NSE';
    const tvUrl=`https://www.tradingview.com/chart/?symbol=${exchange}%3A${h.tradingsymbol}`;
    const kiteUrl=`https://kite.zerodha.com/positions`;
    const isCustom=stockRiskLimits[h.tradingsymbol]!==undefined;
    // % Risk = the limit set (custom or global), coloured by how close to breach
    const riskColor = ratio>=1?'var(--loss)':ratio>=0.8?'var(--orange)':ratio>=0.5?'var(--warn)':'var(--muted)';
    // Sell button: only show when loss has breached the limit
    const sellBtn = ratio>=1 ? `<a href="${kiteUrl}" target="_blank" class="sell-btn" title="Loss limit breached — sell in Kite">Sell</a>` : '';
    return `<tr>
      <td><a href="${tvUrl}" target="_blank" style="text-decoration:none;display:flex;align-items:center;gap:3px"><span class="tk tv-link">${h.tradingsymbol}</span><span style="font-size:0.55rem;color:var(--muted)">↗</span></a></td>
      <td>${h.quantity}</td>
      <td>₹${(h.average_price||0).toLocaleString('en-IN',{maximumFractionDigits:0})}</td>
      <td>₹${(h.last_price||0).toLocaleString('en-IN',{maximumFractionDigits:0})}</td>
      <td class="blur-val"><strong>₹${Math.round(h.invested_value).toLocaleString('en-IN')}</strong></td>
      <td class="blur-val">₹${Math.round(h.current_value).toLocaleString('en-IN')}</td>
      <td class="${gc(h.pnl)} blur-val">${h.pnl>=0?'+':'-'}₹${Math.abs(Math.round(h.pnl)).toLocaleString('en-IN')}</td>
      <td class="${gc(h.pnl_pct)}">${pct(h.pnl_pct)}</td>
      <td class="m">${h.weight_pct}%</td>
      <td style="color:${riskColor};font-size:0.72rem">${customLimit}%</td>
      <td>
        <div style="display:flex;align-items:center;gap:3px;flex-wrap:wrap">${badge}<button class="risk-edit-btn" onclick="openRiskModal('${h.tradingsymbol}',${customLimit})">${isCustom?customLimit+'%':''} ✎</button>${sellBtn}</div>
        <div class="mini-bar"><div class="mini-fill" style="width:${h.pnl_pct>=0?100:Math.min(ratio*100,100)}%;background:${barClr}"></div></div>
      </td>
    </tr>`;
  }).join('');
}

// ── GROWTH CHART ───────────────────────────────────────
function renderGrowthChart(d){
  const p=d.portfolio,st=d.settings,history=d.history||[];
  let labels=[],valData=[],costData=[],targetData=[],drawdownData=[];

  // Profit target = total capital × (1 + target%) — the number to beat
  // Uses total capital (invested + cash) so withdrawals are accounted for
  const baseCap      = p.total_capital;   // invested + cash
  // Target = original cost + target% gain (e.g. 100% target = double invested amount)
  const profitTarget = p.total_cost * (1 + st.cagr_target/100);

  if(history.length>=2){
    const raw=chartMode==='monthly'?groupByMonth(history):groupByYear(history);
    labels=raw.map(r=>r.label);
    valData=raw.map(r=>r.value);
    // Target = flat line showing what you need to reach
    targetData=raw.map(()=>profitTarget);
    let peak=0;drawdownData=valData.map(v=>{peak=Math.max(peak,v);return peak>0?-((peak-v)/peak*100):0;});
  }else{
    // No history — generate monthly timeline from 2023 to now as estimated
    const since=new Date('2023-01-01'),now=new Date();
    if(chartMode==='monthly'){
      let cur=new Date(since.getFullYear(),since.getMonth(),1);const months=[];
      while(cur<=now){months.push(new Date(cur));cur=new Date(cur.getFullYear(),cur.getMonth()+1,1);}
      const n=months.length;
      labels=months.map(m=>m.toLocaleString('en-IN',{month:'short',year:'2-digit'}));
      months.forEach((_,i)=>{
        const pr=i/Math.max(n-1,1),cv=Math.pow(pr,0.75)*(1+Math.sin(i*1.8)*0.03);
        const tc=baseCap*(0.3+0.7*pr);
        valData.push(Math.max(tc+p.total_pl*cv,tc*0.85));
        targetData.push(profitTarget);
      });
      valData[n-1]=p.total_value;
    }else{
      const sy=since.getFullYear(),ey=now.getFullYear();
      for(let y=sy;y<=ey;y++)labels.push(y===ey?y+'★':String(y));
      const n=labels.length;
      labels.forEach((_,i)=>{
        const pr=i/Math.max(n-1,1),tc=baseCap*(0.25+0.75*pr);
        valData.push(Math.max(tc+p.total_pl*Math.pow(pr,0.75),tc*0.8));
        targetData.push(profitTarget);
      });
      valData[n-1]=p.total_value;
    }
    drawdownData=[];
  }

  const gc2=isDark?'rgba(255,255,255,0.04)':'rgba(0,0,0,0.04)';
  const tc2=isDark?'#7b82a8':'#9090a0';
  const isReal=(d.history||[]).length>=2;
  const ptR=(chartMode==='yearly'||isReal)?3:0;
  const notice=document.getElementById('growthNotice');
  if(notice)notice.textContent=isReal?`${(d.history||[]).length} real days`:'Estimated';

  // Color portfolio line: green when above CAGR target, amber when below
  // Green line when portfolio value has crossed the profit target
  const aboveTarget = p.total_value >= profitTarget;
  const lineColor   = aboveTarget ? 'var(--gain)' : 'var(--warn)';
  const fillColor   = aboveTarget ? 'rgba(0,230,118,0.06)' : 'rgba(255,171,64,0.06)';

  const datasets=[
    {label:'Portfolio Value',data:valData,borderColor:lineColor,backgroundColor:fillColor,borderWidth:2.5,fill:true,tension:0.4,pointRadius:ptR,pointBackgroundColor:lineColor,pointBorderColor:isDark?'#0f1117':'#f4f6fb',pointBorderWidth:2,pointHoverRadius:5},
    {label:`🎯 Target (+${st.cagr_target}%)`,data:targetData,borderColor:'rgba(255,82,82,0.85)',backgroundColor:'rgba(255,82,82,0.05)',borderWidth:2.5,borderDash:[6,3],tension:0.4,pointRadius:0,fill:false},
  ];
  if(drawdownData.length)datasets.push({label:'Drawdown %',data:drawdownData,borderColor:'rgba(255,171,64,0.6)',backgroundColor:'transparent',borderWidth:1,borderDash:[2,3],tension:0.4,pointRadius:0,yAxisID:'y2'});

  if(growthChart)growthChart.destroy();
  growthChart=new Chart(document.getElementById('growthChart'),{
    type:'line',data:{labels,datasets},
    options:{responsive:true,maintainAspectRatio:true,interaction:{mode:'index',intersect:false},
      plugins:{legend:{labels:{color:tc2,font:{family:'Inter',size:10},boxWidth:14,padding:10}},tooltip:{backgroundColor:isDark?'#1a1d27':'#fff',borderColor:isDark?'#2e3248':'#dde1f0',borderWidth:1,titleColor:tc2,bodyColor:isDark?'#e8eaf6':'#1a1d2e',callbacks:{label:c=>` ${c.dataset.label}: ${fmtL(c.raw)}`}}},
      scales:{x:{ticks:{color:tc2,font:{family:'Inter',size:9},maxTicksLimit:10},grid:{color:gc2}},y:{ticks:{color:tc2,font:{family:'Inter',size:9},callback:v=>fmtL(v)},grid:{color:gc2}},y2:{position:'right',ticks:{color:'rgba(255,171,64,0.5)',font:{family:'Inter',size:9},callback:v=>v.toFixed(1)+'%'},grid:{drawOnChartArea:false},display:drawdownData.length>0}}}
  });
}

function groupByMonth(h){const m={};h.forEach(x=>{const d=new Date(x.date),k=d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0');m[k]=x;});return Object.entries(m).sort().map(([k,v])=>{const d=new Date(k+'-01');return{label:d.toLocaleString('en-IN',{month:'short',year:'2-digit'}),...v};});}
function groupByYear(h){const m={};h.forEach(x=>{const y=x.date.slice(0,4);m[y]=x;});const ny=new Date().getFullYear();return Object.entries(m).sort().map(([y,v])=>({label:parseInt(y)===ny?y+'★':y,...v}));}
function setChartTab(el,mode){document.querySelectorAll('.ctab').forEach(b=>b.classList.remove('active'));el.classList.add('active');chartMode=mode;if(lastData)renderGrowthChart(lastData);}

// ── PORTFOLIO ANALYTICS ────────────────────────────────
function renderHoldingsAnalytics(d){
  const holdings=d.holdings;
  const totalInvested=holdings.reduce((s,h)=>s+h.invested_value,0);
  const winners=holdings.filter(h=>h.pnl>0),losers=holdings.filter(h=>h.pnl<0),neutral=holdings.filter(h=>h.pnl===0);
  const best=[...holdings].sort((a,b)=>b.pnl_pct-a.pnl_pct)[0];
  const worst=[...holdings].sort((a,b)=>a.pnl_pct-b.pnl_pct)[0];
  const biggestPos=[...holdings].sort((a,b)=>b.invested_value-a.invested_value)[0];
  const avgReturn=holdings.reduce((s,h)=>s+h.pnl_pct,0)/holdings.length;
  const total=holdings.length;
  const winRate=total>0?winners.length/total*100:0;
  const lossRate=total>0?losers.length/total*100:0;
  const winLossRatio=losers.length>0?(winners.length/losers.length).toFixed(2):'∞';
  const grossProfit=winners.reduce((s,h)=>s+h.pnl,0);
  const grossLoss=Math.abs(losers.reduce((s,h)=>s+h.pnl,0));
  const profitFactor=grossLoss>0?grossProfit/grossLoss:grossProfit>0?999:0;
  const pfVal=profitFactor>=999?'∞':profitFactor.toFixed(2);
  const pfColor=profitFactor>=2?'var(--gain)':profitFactor>=1?'var(--warn)':'var(--loss)';
  const pfLabel=profitFactor>=2?'EXCELLENT':profitFactor>=1.5?'GOOD':profitFactor>=1?'MARGINAL':'LOSING';
  // Avg win/loss amounts and %
  const avgWinAmt=winners.length>0?grossProfit/winners.length:0;
  const avgLossAmt=losers.length>0?grossLoss/losers.length:0;
  const avgWinPct=winners.length>0?winners.reduce((s,h)=>s+h.pnl_pct,0)/winners.length:0;
  const avgLossPct=losers.length>0?Math.abs(losers.reduce((s,h)=>s+h.pnl_pct,0)/losers.length):0;
  const rr=avgLossAmt>0?(avgWinAmt/avgLossAmt).toFixed(2):'∞';

  document.getElementById('holdingsAnalytics').innerHTML=`
    <div class="grid2" style="margin-bottom:12px">
      <!-- WIN RATE -->
      <div class="big-metric">
        <div style="font-size:0.6rem;font-weight:600;letter-spacing:1.5px;text-transform:uppercase;color:var(--muted);margin-bottom:8px">Win Rate</div>
        <div class="big-metric-num" style="color:${winRate>=50?'var(--gain)':'var(--loss)'}">${winRate.toFixed(0)}%</div>
        <div class="big-metric-lbl">${winners.length} wins · ${losers.length} losses · ${total} stocks</div>
        <div style="display:flex;height:5px;border-radius:3px;overflow:hidden;margin:10px 0 8px">
          <div style="width:${winRate}%;background:var(--gain);transition:width 0.8s ease"></div>
          <div style="width:${lossRate}%;background:var(--loss);transition:width 0.8s ease"></div>
          ${neutral.length?`<div style="flex:1;background:var(--s3)"></div>`:''}
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:7px;margin-bottom:8px">
          <div style="background:var(--gain-bg);border-radius:5px;padding:8px;text-align:center">
            <div style="font-size:0.78rem;font-weight:700;color:var(--gain)" class="blur-val">+${fmtL(grossProfit)}</div>
            <div style="font-size:0.57rem;color:var(--muted);text-transform:uppercase;letter-spacing:0.5px;margin-top:2px">Gross Profit</div>
          </div>
          <div style="background:var(--loss-bg);border-radius:5px;padding:8px;text-align:center">
            <div style="font-size:0.78rem;font-weight:700;color:var(--loss)" class="blur-val">-${fmtL(grossLoss)}</div>
            <div style="font-size:0.57rem;color:var(--muted);text-transform:uppercase;letter-spacing:0.5px;margin-top:2px">Gross Loss</div>
          </div>
        </div>
        <div style="font-size:0.68rem;color:var(--muted)">Win/Loss Ratio: <strong style="color:var(--text)">${winLossRatio}</strong></div>
      </div>

      <!-- PROFIT FACTOR -->
      <div class="big-metric" style="border-color:${pfColor}30">
        <div style="font-size:0.6rem;font-weight:600;letter-spacing:1.5px;text-transform:uppercase;color:var(--muted);margin-bottom:8px">Profit Factor</div>
        <div class="big-metric-num" style="color:${pfColor}">${pfVal}</div>
        <div class="big-metric-lbl" style="color:${pfColor}">${pfLabel}</div>
        <div style="font-size:0.65rem;color:var(--muted);margin-top:4px">Gross Profit ÷ Gross Loss</div>
        <div style="margin:10px 0 8px;background:var(--s3);border-radius:3px;height:5px;overflow:hidden">
          <div style="height:100%;width:${Math.min(profitFactor>=999?100:profitFactor/3*100,100)}%;background:${pfColor};border-radius:3px;transition:width 0.8s ease"></div>
        </div>
        <div style="font-size:0.65rem;color:var(--muted);line-height:1.6">
          <span style="color:var(--gain)">›2.0 Excellent</span> &nbsp;·&nbsp;
          <span style="color:var(--warn)">1.0–2.0 Marginal</span> &nbsp;·&nbsp;
          <span style="color:var(--loss)">‹1.0 Losing</span>
        </div>
      </div>
    </div>

    <!-- AVG WIN / AVG LOSS / RR -->
    <div class="panel" style="background:var(--s2);margin-bottom:12px">
      <div class="panel-title">Avg Win / Avg Loss <span>Reward-Risk Ratio</span></div>
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px">
        <div style="text-align:center;padding:12px;background:var(--gain-bg);border-radius:7px;border:1px solid rgba(0,230,118,0.2)">
          <div style="font-size:0.58rem;color:var(--muted);letter-spacing:1px;text-transform:uppercase;margin-bottom:5px">Avg Win</div>
          <div style="font-size:1.1rem;font-weight:700;color:var(--gain)" class="blur-val">+${fmtL(avgWinAmt)}</div>
          <div style="font-size:0.68rem;color:var(--gain);margin-top:2px">${pct(avgWinPct)}</div>
        </div>
        <div style="text-align:center;padding:12px;background:var(--loss-bg);border-radius:7px;border:1px solid rgba(255,82,82,0.2)">
          <div style="font-size:0.58rem;color:var(--muted);letter-spacing:1px;text-transform:uppercase;margin-bottom:5px">Avg Loss</div>
          <div style="font-size:1.1rem;font-weight:700;color:var(--loss)" class="blur-val">-${fmtL(avgLossAmt)}</div>
          <div style="font-size:0.68rem;color:var(--loss);margin-top:2px">-${avgLossPct.toFixed(1)}%</div>
        </div>
        <div style="text-align:center;padding:12px;background:var(--s3);border-radius:7px;border:1px solid var(--border)">
          <div style="font-size:0.58rem;color:var(--muted);letter-spacing:1px;text-transform:uppercase;margin-bottom:5px">Reward:Risk</div>
          <div style="font-size:1.1rem;font-weight:700;color:${parseFloat(rr)>=1?'var(--gain)':'var(--warn)'}">1 : ${rr}</div>
          <div style="font-size:0.62rem;color:var(--muted);margin-top:2px">Higher = better reward</div>
        </div>
      </div>
      <div style="margin-top:8px;font-size:0.65rem;color:var(--muted);text-align:center;padding:4px;background:var(--s3);border-radius:4px">
        Higher RR suggests better reward for the risk taken. Even a 40% win rate is profitable if RR &gt; 1.5
      </div>
    </div>

    <!-- SUMMARY GRID -->
    <div class="ha-grid">
      <div class="ha-box"><div class="ha-num">${total}</div><div class="ha-lbl">Stocks</div></div>
      <div class="ha-box"><div class="ha-num" style="color:${avgReturn>=0?'var(--gain)':'var(--loss)'}">${pct(avgReturn)}</div><div class="ha-lbl">Avg Return</div></div>
      <div class="ha-box"><div class="ha-num blur-val">${fmtL(totalInvested)}</div><div class="ha-lbl">Invested</div></div>
      <div class="ha-box" style="border-color:rgba(0,230,118,0.3)">
        <div class="ha-num" style="color:var(--gain)">${best?.tradingsymbol||'-'}</div>
        <div class="ha-lbl">Best Stock</div>
        <div style="font-size:0.62rem;color:var(--gain);margin-top:2px">${best?pct(best.pnl_pct):''}</div>
      </div>
      <div class="ha-box" style="border-color:rgba(255,82,82,0.3)">
        <div class="ha-num" style="color:var(--loss)">${worst?.tradingsymbol||'-'}</div>
        <div class="ha-lbl">Worst Stock</div>
        <div style="font-size:0.62rem;color:var(--loss);margin-top:2px">${worst?pct(worst.pnl_pct):''}</div>
      </div>
    </div>

    <!-- BIGGEST POSITION -->
    <div style="background:var(--s2);border-radius:7px;padding:11px 14px;display:flex;justify-content:space-between;align-items:center;border:1px solid var(--border)">
      <div>
        <div style="font-size:0.58rem;color:var(--muted);text-transform:uppercase;letter-spacing:1px;margin-bottom:2px">Biggest Position</div>
        <div style="font-weight:700">${biggestPos?.tradingsymbol||'-'} · <span class="blur-val">${fmtL(biggestPos?.invested_value||0)}</span> · ${biggestPos?.weight_pct||0}% of portfolio</div>
      </div>
      <div style="text-align:right">
        <div style="font-size:0.58rem;color:var(--muted);text-transform:uppercase;letter-spacing:1px;margin-bottom:2px">Return</div>
        <div style="font-weight:700;color:${gclr(biggestPos?.pnl_pct||0)}">${pct(biggestPos?.pnl_pct||0)}</div>
      </div>
    </div>`;
}

// ── CALENDAR — full year heatmap like GitHub ───────────
function buildDayPLMap(history){
  const plMap={};
  // history[i].pl is TOTAL unrealised P&L on that day
  // daily change = today's portfolio value minus yesterday's portfolio value
  history.forEach((h,i)=>{
    const dayPL = i>0
      ? (h.value||0) - (history[i-1].value||0)  // value change = actual daily gain/loss
      : 0;
    plMap[h.date] = {pl: Math.round(dayPL), value: h.value, totalPL: h.pl};
  });
  // Also mark today from last data
  if(lastData){
    const today = new Date().toISOString().split('T')[0];
    if(!plMap[today] && history.length>0){
      const last = history[history.length-1];
      const dayPL = (lastData.portfolio.total_value||0) - (last.value||0);
      plMap[today] = {pl: Math.round(dayPL), value: lastData.portfolio.total_value, totalPL: lastData.portfolio.total_pl};
    }
  }
  return plMap;
}

function calColor(pl, maxAbsPL){
  if(pl===undefined||pl===null) return isDark?'#1e2130':'#e8ecf4'; // no data
  if(pl===0) return isDark?'#2a2f45':'#d0d5e8'; // neutral
  const intensity = Math.min(Math.abs(pl)/Math.max(maxAbsPL,1), 1);
  if(pl>0){
    const a = 0.2 + intensity*0.75;
    return `rgba(0,230,118,${a.toFixed(2)})`;
  } else {
    const a = 0.2 + intensity*0.75;
    return `rgba(255,82,82,${a.toFixed(2)})`;
  }
}

function renderCalendar(){
  const history = (lastData?.history)||[];
  const plMap   = buildDayPLMap(history);
  const year    = calCurrentYear;
  const today   = new Date();
  document.getElementById('calYearLabel').textContent = year;

  // If no history yet, show today's P&L from live data
  if(lastData && history.length === 0){
    const todayStr = today.toISOString().split('T')[0];
    const p = lastData.portfolio;
    // Use total P&L as today's indicator
    if(p.total_pl !== 0) plMap[todayStr] = {pl: p.total_pl, value: p.total_value};
  }

  // Calculate max abs P&L for intensity scaling
  const pls = Object.values(plMap).map(v=>Math.abs(v.pl)).filter(v=>v>0);
  const maxPL = pls.length ? Math.max(...pls) : 1;

  const MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  const DOWS   = ['M','T','W','T','F','S','S'];

  let html = '';
  MONTHS.forEach((mName, mIdx) => {
    const firstDay    = new Date(year, mIdx, 1);
    const daysInMonth = new Date(year, mIdx+1, 0).getDate();
    // Start from Monday (1) not Sunday
    let startOffset = (firstDay.getDay()+6)%7; // convert to Mon=0

    html += `<div style="flex-shrink:0">
      <div style="font-size:0.62rem;font-weight:600;color:var(--muted);margin-bottom:4px;letter-spacing:0.5px">${mName}</div>
      <div style="display:flex;gap:1px;margin-bottom:3px">
        ${DOWS.map(d=>`<div style="width:11px;font-size:0.42rem;color:var(--muted);text-align:center">${d}</div>`).join('')}
      </div>
      <div style="display:grid;grid-template-columns:repeat(7,11px);gap:2px">`;

    // Empty cells before 1st
    for(let i=0;i<startOffset;i++) html+=`<div style="width:11px;height:11px;border-radius:2px"></div>`;

    for(let d=1;d<=daysInMonth;d++){
      const dateStr = `${year}-${String(mIdx+1).padStart(2,'0')}-${String(d).padStart(2,'0')}`;
      const data    = plMap[dateStr];
      const bg      = calColor(data?.pl, maxPL);
      const isToday = today.toISOString().split('T')[0]===dateStr;
      const title   = data ? `${dateStr}: ${data.pl>=0?'+':''}${fmtL(data.pl)}` : dateStr;
      html+=`<div
        style="width:11px;height:11px;border-radius:2px;background:${bg};cursor:pointer;${isToday?'outline:1.5px solid var(--accent);outline-offset:1px':''}"
        onclick="showCalDay('${dateStr}',${data?data.pl:0})"
        title="${title}"></div>`;
    }
    html += `</div></div>`;
  });

  document.getElementById('yearHeatmap').innerHTML = html;

  // Insights
  const dayPLs = history.map((h,i)=>({date:h.date,pl:i>0?h.pl-history[i-1].pl:0})).filter(x=>x.pl!==0);
  const sortedPLs = [...dayPLs].sort((a,b)=>b.pl-a.pl);
  const bestDay   = sortedPLs[0];
  const worstDay  = [...dayPLs].sort((a,b)=>a.pl-b.pl)[0];
  const monthlyMap={};
  history.forEach(h=>{const m=h.date.slice(0,7);if(!monthlyMap[m])monthlyMap[m]={start:h.pl,end:h.pl};monthlyMap[m].end=h.pl;});
  const monthlyPLs=Object.entries(monthlyMap).map(([m,v])=>({month:m,pl:v.end-v.start}));
  const bestMonth  = [...monthlyPLs].sort((a,b)=>b.pl-a.pl)[0];
  const worstMonth = [...monthlyPLs].sort((a,b)=>a.pl-b.pl)[0];

  document.getElementById('calInsights').innerHTML=`
    <div class="cal-ins-box"><div class="cal-ins-lbl">Best Day</div><div class="cal-ins-val g blur-val">${bestDay?'+'+fmtL(bestDay.pl):'—'}</div><div class="cal-ins-sub">${bestDay?.date||'No data yet'}</div></div>
    <div class="cal-ins-box"><div class="cal-ins-lbl">Worst Day</div><div class="cal-ins-val l blur-val">${worstDay?fmtL(worstDay.pl):'—'}</div><div class="cal-ins-sub">${worstDay?.date||'No data yet'}</div></div>
    <div class="cal-ins-box"><div class="cal-ins-lbl">Best Month</div><div class="cal-ins-val g blur-val">${bestMonth?'+'+fmtL(bestMonth.pl):'—'}</div><div class="cal-ins-sub">${bestMonth?.month||'No data yet'}</div></div>
    <div class="cal-ins-box"><div class="cal-ins-lbl">Worst Month</div><div class="cal-ins-val l blur-val">${worstMonth?fmtL(worstMonth.pl):'—'}</div><div class="cal-ins-sub">${worstMonth?.month||'No data yet'}</div></div>`;
}

function showCalDay(date, pl){
  const detail = document.getElementById('calDayDetail');
  if(!pl){detail.style.display='none';return;}
  detail.style.display='block';
  document.getElementById('calDayDetailText').innerHTML=
    `<strong>${new Date(date).toLocaleDateString('en-IN',{day:'2-digit',month:'long',year:'numeric'})}</strong>
     — P&L: <span style="color:${pl>=0?'var(--gain)':'var(--loss)'}"><strong>${pl>=0?'+':''}${fmtL(pl)}</strong></span>`;
}

function calNavYear(dir){
  calCurrentYear += dir;
  renderCalendar();
}

// ── SECTORS — horizontal stacked bar like reference ────
const COLORS=['#5b6cf9','#00bcd4','#ab47bc','#5c6bc0','#26c6da','#00897b','#f06292','#26a69a','#7e57c2','#42a5f5'];

function renderSectors(d){
  if(!d) return;
  const holdings = d.holdings;
  const sectorMap = {};

  holdings.forEach(h=>{
    const sector = getSector(h.tradingsymbol);
    if(!sectorMap[sector]) sectorMap[sector]={value:0,invested:0,pl:0,stocks:[]};
    sectorMap[sector].value    += h.current_value;
    sectorMap[sector].invested += h.invested_value;
    sectorMap[sector].pl       += h.pnl;
    sectorMap[sector].stocks.push(h);
  });

  const viewEl = document.querySelector('input[name="sectorView"]:checked');
  const view   = viewEl ? viewEl.value : 'value';
  const sectors = Object.entries(sectorMap).sort((a,b)=>b[1].pl - a[1].pl);
  const totalVal     = holdings.reduce((s,h)=>s+h.current_value, 0);
  const totalInvested= holdings.reduce((s,h)=>s+h.invested_value, 0);
  const maxBar = view==='pl'
    ? Math.max(...sectors.map(([,v])=>Math.abs(v.pl)), 1)
    : view==='invested' ? totalInvested : totalVal;

  // PIE CHART
  if(sectorChart) sectorChart.destroy();
  sectorChart = new Chart(document.getElementById('sectorChart'),{
    type: 'doughnut',
    data:{
      labels: sectors.map(([n])=>n),
      datasets:[{
        data: sectors.map(([,v])=>v.value),
        backgroundColor: COLORS.slice(0, sectors.length),
        borderColor: isDark?'#0f1117':'#f4f6fb',
        borderWidth: 2, hoverOffset: 10,
      }]
    },
    options:{
      responsive:true, maintainAspectRatio:true, cutout:'55%',
      plugins:{
        legend:{display:false},
        tooltip:{
          backgroundColor:isDark?'#1a1d27':'#fff',
          borderColor:isDark?'#2e3248':'#dde1f0',borderWidth:1,
          titleColor:isDark?'#7b82a8':'#9090a0',
          bodyColor:isDark?'#e8eaf6':'#1a1d2e',
          callbacks:{
            title:ctx=>ctx[0].label,
            label:ctx=>{
              const s=sectors[ctx.dataIndex][1];
              const p=(s.value/totalVal*100).toFixed(1);
              return [` Value: ${fmtL(s.value)} (${p}%)`,` P&L: ${s.pl>=0?'+':''}${fmtL(s.pl)} · ${s.stocks.length} stocks`];
            }
          }
        }
      },
      onClick:(_,els)=>{
        if(els.length) showSectorDetail(sectors[els[0].index][0],sectors[els[0].index][1],COLORS[els[0].index%COLORS.length],totalVal);
      }
    }
  });

  // LEGEND
  const legEl = document.getElementById('sectorLegend');
  if(legEl) legEl.innerHTML = sectors.map(([name,v],i)=>`
    <div style="display:flex;align-items:center;gap:6px;cursor:pointer;padding:3px 4px;border-radius:4px"
         onmouseover="this.style.background='var(--s3)'" onmouseout="this.style.background=''"
         onclick="showSectorDetail('${name}',null,'${COLORS[i%COLORS.length]}',${totalVal})">
      <div style="width:8px;height:8px;border-radius:2px;background:${COLORS[i%COLORS.length]};flex-shrink:0"></div>
      <span style="font-size:0.68rem;flex:1;font-weight:500">${name}</span>
      <span style="font-size:0.65rem;font-family:'DM Mono',monospace;color:${v.pl>=0?'var(--gain)':'var(--loss)'}" class="blur-val">
        ${v.pl>=0?'+':''}${fmtL(v.pl)}
      </span>
      <span style="font-size:0.6rem;color:var(--muted);width:28px;text-align:right">${(v.value/totalVal*100).toFixed(0)}%</span>
    </div>`).join('');

  // SECTOR ROWS
  document.getElementById('sectorRows').innerHTML = sectors.map(([name,v],i)=>{
    const dispVal = view==='pl'?v.pl:view==='invested'?v.invested:v.value;
    const barW    = Math.min(Math.abs(dispVal)/maxBar*100, 100);
    const barClr  = view==='pl'?(v.pl>=0?'var(--gain)':'var(--loss)'):COLORS[i%COLORS.length];
    const uid     = 'sec_'+i;
    const sortedStocks = [...v.stocks].sort((a,b)=>b.pnl-a.pnl);
    const stocksHtml = sortedStocks.map(h=>`
      <div style="display:flex;justify-content:space-between;align-items:center;padding:6px 10px 6px 18px;border-bottom:1px solid var(--border)">
        <a href="https://www.tradingview.com/chart/?symbol=${h.exchange||'NSE'}%3A${h.tradingsymbol}"
           target="_blank" style="font-size:0.74rem;font-weight:600;color:var(--text);text-decoration:none">${h.tradingsymbol} ↗</a>
        <div style="display:flex;align-items:center;gap:10px">
          <span style="font-size:0.68rem;color:var(--muted)" class="blur-val">${fmtL(h.current_value)}</span>
          <span style="font-size:0.74rem;font-weight:600;color:${h.pnl>=0?'var(--gain)':'var(--loss)'};min-width:60px;text-align:right" class="blur-val">${h.pnl>=0?'+':''}${fmtL(h.pnl)}</span>
          <span style="font-size:0.7rem;color:${h.pnl_pct>=0?'var(--gain)':'var(--loss)'};min-width:44px;text-align:right">${pct(h.pnl_pct)}</span>
        </div>
      </div>`).join('');

    return `<div style="border-bottom:1px solid var(--border)">
      <div style="display:flex;align-items:center;gap:10px;padding:9px 0 5px;cursor:pointer"
           onclick="const el=document.getElementById('${uid}');const ar=document.getElementById('${uid}a');el.style.display=el.style.display==='none'?'block':'none';ar.textContent=el.style.display==='none'?'▸':'▾'">
        <div style="width:10px;height:10px;border-radius:2px;background:${COLORS[i%COLORS.length]};flex-shrink:0"></div>
        <div style="flex:1;min-width:0">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:5px">
            <div style="display:flex;align-items:center;gap:6px">
              <span style="font-weight:600;font-size:0.78rem">${name}</span>
              <span style="font-size:0.62rem;color:var(--muted)">${v.stocks.length} stock${v.stocks.length>1?'s':''}</span>
            </div>
            <div style="display:flex;align-items:center;gap:8px">
              <span style="font-family:'DM Mono',monospace;font-size:0.72rem;font-weight:600;color:${v.pl>=0?'var(--gain)':'var(--loss)'}" class="blur-val">${v.pl>=0?'+':''}${fmtL(v.pl)}</span>
              <span style="font-size:0.65rem;color:var(--muted)">${(v.value/totalVal*100).toFixed(1)}%</span>
              <span id="${uid}a" style="font-size:0.7rem;color:var(--muted)">▸</span>
            </div>
          </div>
          <div style="height:4px;background:var(--s3);border-radius:2px;overflow:hidden">
            <div style="height:100%;width:${barW}%;background:${barClr};border-radius:2px;transition:width 0.6s ease"></div>
          </div>
        </div>
      </div>
      <div id="${uid}" style="display:none;background:var(--s2);border-radius:6px;margin-bottom:8px;overflow:hidden;border:1px solid var(--border)">
        <div style="display:flex;justify-content:space-between;padding:5px 10px;background:var(--s3)">
          <span style="font-size:0.6rem;font-weight:600;color:var(--muted);text-transform:uppercase">Stock</span>
          <div style="display:flex;gap:10px">
            <span style="font-size:0.6rem;color:var(--muted);text-transform:uppercase">Value</span>
            <span style="font-size:0.6rem;color:var(--muted);text-transform:uppercase;min-width:60px">P&L ₹</span>
            <span style="font-size:0.6rem;color:var(--muted);text-transform:uppercase;min-width:44px;text-align:right">Return</span>
          </div>
        </div>
        ${stocksHtml}
      </div>
    </div>`;
  }).join('');

  // SUMMARY
  const gainSectors = sectors.filter(([,v])=>v.pl>0);
  const lossSectors = sectors.filter(([,v])=>v.pl<0);
  const bestSec  = gainSectors[0];
  const worstSec = lossSectors[lossSectors.length-1];
  document.getElementById('sectorSummary').innerHTML=`
    <div class="cal-ins-box" style="border-color:rgba(0,230,118,0.3)">
      <div class="cal-ins-lbl">Top Profit Sector</div>
      <div class="cal-ins-val g">${bestSec?bestSec[0]:'—'}</div>
      <div class="cal-ins-sub g blur-val">${bestSec?'+'+fmtL(bestSec[1].pl)+' · '+bestSec[1].stocks.length+' stocks':''}</div>
    </div>
    <div class="cal-ins-box" style="border-color:rgba(255,82,82,0.3)">
      <div class="cal-ins-lbl">Top Loss Sector</div>
      <div class="cal-ins-val l">${worstSec?worstSec[0]:'—'}</div>
      <div class="cal-ins-sub l blur-val">${worstSec?fmtL(worstSec[1].pl)+' · '+worstSec[1].stocks.length+' stocks':''}</div>
    </div>`;

  window._sectorData  = Object.fromEntries(sectors);
  window._sectorTotal = totalVal;
}

function showSectorDetail(name, data, color, totalVal){
  const s = data || window._sectorData?.[name];
  if(!s) return;
  const pct_ = (s.value/totalVal*100).toFixed(1);
  const gainers = [...s.stocks].filter(h=>h.pnl>0).sort((a,b)=>b.pnl-a.pnl);
  const losers  = [...s.stocks].filter(h=>h.pnl<=0).sort((a,b)=>a.pnl-b.pnl);
  const row = h=>`
    <div style="display:flex;justify-content:space-between;align-items:center;padding:8px 12px;margin-bottom:3px;
                border-radius:5px;background:${h.pnl>=0?'var(--gain-bg)':'var(--loss-bg)'};
                border-left:3px solid ${h.pnl>=0?'var(--gain)':'var(--loss)'}">
      <a href="https://www.tradingview.com/chart/?symbol=${h.exchange||'NSE'}%3A${h.tradingsymbol}"
         target="_blank" style="font-weight:700;font-size:0.8rem;color:var(--text);text-decoration:none">
        ${h.tradingsymbol} ↗
        <span style="font-size:0.65rem;font-weight:400;color:var(--muted);margin-left:6px" class="blur-val">${fmtL(h.current_value)}</span>
      </a>
      <div>
        <span style="font-size:0.8rem;font-weight:700;color:${h.pnl>=0?'var(--gain)':'var(--loss)'}" class="blur-val">${h.pnl>=0?'+':''}${fmtL(h.pnl)}</span>
        <span style="font-size:0.72rem;margin-left:8px;color:${h.pnl_pct>=0?'var(--gain)':'var(--loss)'}">${pct(h.pnl_pct)}</span>
      </div>
    </div>`;

  document.getElementById('sectorDetails').style.display='block';
  document.getElementById('sectorDetails').innerHTML=`
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
      <div style="display:flex;align-items:center;gap:8px">
        <div style="width:10px;height:10px;border-radius:2px;background:${color}"></div>
        <span style="font-weight:700;font-size:0.95rem">${name}</span>
        <span style="font-size:0.7rem;color:var(--muted)">${s.stocks.length} stocks · ${pct_}%</span>
      </div>
      <button onclick="document.getElementById('sectorDetails').style.display='none'"
              style="background:none;border:none;color:var(--muted);cursor:pointer;font-size:1rem;padding:0">✕</button>
    </div>
    <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:16px">
      ${[['Weight',pct_+'%','',false],['Value',fmtL(s.value),'',true],['Invested',fmtL(s.invested),'',true],
         ['P&L',(s.pl>=0?'+':'')+fmtL(s.pl),s.pl>=0?'color:var(--gain)':'color:var(--loss)',true]].map(([l,v,st,blr])=>
        `<div style="background:var(--s3);border-radius:6px;padding:9px;text-align:center">
           <div style="font-size:0.55rem;color:var(--muted);text-transform:uppercase;margin-bottom:3px">${l}</div>
           <div style="font-weight:700;${st}" class="${blr?'blur-val':''}">${v}</div>
         </div>`).join('')}
    </div>
    ${gainers.length?`<div style="font-size:0.65rem;font-weight:700;color:var(--gain);text-transform:uppercase;letter-spacing:1px;margin-bottom:7px">▲ Making money (${gainers.length})</div>${gainers.map(row).join('')}`:''}
    ${losers.length?`<div style="font-size:0.65rem;font-weight:700;color:var(--loss);text-transform:uppercase;letter-spacing:1px;margin:${gainers.length?'14px':0} 0 7px">▼ Losing money (${losers.length})</div>${losers.map(row).join('')}`:''}`;
}

// ── JOURNAL ────────────────────────────────────────────
function initJournal(){
  const today=new Date();
  journalCurrentDate=today.toISOString().split('T')[0];
  document.getElementById('journalDate').textContent=today.toLocaleDateString('en-IN',{day:'2-digit',month:'long',year:'numeric'});
  loadJournalEntry(journalCurrentDate);
  buildMiniCal(today.getFullYear(),today.getMonth());
}

function loadJournalEntry(date){
  journalCurrentDate=date;
  document.getElementById('journalDate').textContent=new Date(date).toLocaleDateString('en-IN',{day:'2-digit',month:'long',year:'numeric'});
  const entry=journalData[date]||'';
  document.getElementById('journalEntry').value=entry;
  document.getElementById('journalSaveStatus').textContent=entry?'Saved':'No entry for this date';
}

// ── JOURNAL TOOLBAR ────────────────────────────────────
function jInsert(prefix, suffix, newline){
  const ta = document.getElementById('journalEntry');
  const start = ta.selectionStart, end = ta.selectionEnd;
  const before = ta.value.substring(0, start);
  const sel    = ta.value.substring(start, end);
  const after  = ta.value.substring(end);
  const nl     = newline && before.length > 0 && !before.endsWith('\n') ? '\n' : '';
  const insert = nl + prefix + sel + suffix;
  ta.value = before + insert + after;
  const pos = before.length + insert.length;
  ta.setSelectionRange(pos, pos);
  ta.focus();
  autoSaveJournal();
}

function jWrap(prefix, suffix){
  const ta = document.getElementById('journalEntry');
  const start = ta.selectionStart, end = ta.selectionEnd;
  const sel = ta.value.substring(start, end);
  if(!sel){ jInsert(prefix, suffix, false); return; }
  ta.value = ta.value.substring(0,start) + prefix + sel + suffix + ta.value.substring(end);
  ta.setSelectionRange(start + prefix.length, end + prefix.length);
  ta.focus();
  autoSaveJournal();
}

function jClear(){
  if(!confirm('Clear today\'s journal entry?')) return;
  const ta = document.getElementById('journalEntry');
  ta.value = '';
  autoSaveJournal();
}

function jNumberedList(){
  const ta = document.getElementById('journalEntry');
  const before = ta.value.substring(0, ta.selectionStart);
  // Find the last numbered item in text before cursor
  const lines = before.split('\n');
  let nextNum = 1;
  for(let i = lines.length-1; i >= 0; i--){
    const m = lines[i].match(/^(\d+)\./);
    if(m){ nextNum = parseInt(m[1]) + 1; break; }
  }
  jInsert(nextNum + '. ', '', true);
}

function autoSaveJournal(){
  clearTimeout(journalSaveTimer);
  journalSaveTimer=setTimeout(()=>{
    const text=document.getElementById('journalEntry').value;
    if(text.trim())journalData[journalCurrentDate]=text;
    else delete journalData[journalCurrentDate];
    document.getElementById('journalSaveStatus').textContent='Auto-saved ✓';
    saveJournalToServer();
  },1000);
}

async function saveJournalToServer(){
  // Always save to localStorage as primary (instant, reliable)
  try{ localStorage.setItem('folio_journal_v1', JSON.stringify(journalData)); }catch(e){}
  // Also save to server as backup
  try{
    const r = await fetch('/api/journal',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(journalData)});
    const d = await r.json();
    if(d.ok) document.getElementById('journalSaveStatus').textContent = '✓ Saved ' + new Date().toLocaleTimeString('en-IN',{hour:'2-digit',minute:'2-digit'});
  }catch(e){
    document.getElementById('journalSaveStatus').textContent = '✓ Saved locally';
  }
}

async function loadJournalFromServer(){
  // Load from localStorage first (instant)
  try{
    const local = localStorage.getItem('folio_journal_v1');
    if(local){ journalData = JSON.parse(local); }
  }catch(e){}
  // Then try to sync from server (may have newer entries from other devices)
  try{
    const r = await fetch('/api/journal');
    if(r.ok){
      const serverData = await r.json();
      if(serverData && Object.keys(serverData).length > 0){
        // Merge: server wins for dates not in local
        journalData = {...journalData, ...serverData};
        localStorage.setItem('folio_journal_v1', JSON.stringify(journalData));
      }
    }
  }catch(e){}
}

function searchJournal(query){
  const resultsEl=document.getElementById('searchResults');
  if(!query.trim()){resultsEl.style.display='none';return;}
  const results=Object.entries(journalData).filter(([d,t])=>t.toLowerCase().includes(query.toLowerCase())||d.includes(query)).sort((a,b)=>b[0].localeCompare(a[0]));
  if(!results.length){resultsEl.style.display='none';return;}
  resultsEl.style.display='block';
  resultsEl.innerHTML=results.map(([d,t])=>`
    <div class="search-result-item" onclick="loadJournalEntry('${d}');document.getElementById('searchResults').style.display='none'">
      <div class="sri-date">${new Date(d).toLocaleDateString('en-IN',{day:'2-digit',month:'short',year:'numeric'})}</div>
      <div class="sri-preview">${t.substring(0,80)}${t.length>80?'...':''}</div>
    </div>`).join('');
}

function buildMiniCal(year,month){
  const today=new Date();
  const firstDay=new Date(year,month,1).getDay();
  const days=new Date(year,month+1,0).getDate();
  const monthName=new Date(year,month,1).toLocaleString('en-IN',{month:'short'});
  let html=`
  <div class="mc-header">
    <button class="mc-nav" onclick="miniCalNav(-12)" title="Prev year" style="font-size:0.6rem;padding:1px 4px">«</button>
    <button class="mc-nav" onclick="miniCalNav(-1)" title="Prev month">‹</button>
    <div class="mc-title" style="font-size:0.68rem">${monthName} ${year}</div>
    <button class="mc-nav" onclick="miniCalNav(1)" title="Next month">›</button>
    <button class="mc-nav" onclick="miniCalNav(12)" title="Next year" style="font-size:0.6rem;padding:1px 4px">»</button>
  </div>
  <div style="text-align:center;margin-bottom:6px">
    <button onclick="jumpToToday()" style="background:var(--accent);color:#fff;border:none;padding:2px 10px;border-radius:3px;font-size:0.6rem;font-weight:600;cursor:pointer;width:100%">↩ Today</button>
  </div>
  <div class="mc-grid">
    ${['S','M','T','W','T','F','S'].map(d=>`<div class="mc-dow">${d}</div>`).join('')}`;
  for(let i=0;i<firstDay;i++)html+=`<div></div>`;
  for(let d=1;d<=days;d++){
    const dateStr=`${year}-${String(month+1).padStart(2,'0')}-${String(d).padStart(2,'0')}`;
    const isToday=today.getFullYear()===year&&today.getMonth()===month&&today.getDate()===d;
    const hasEntry=!!journalData[dateStr];
    const isSel=dateStr===journalCurrentDate;
    html+=`<div class="mc-day${hasEntry?' has-entry':''}${isToday?' today':''}${isSel?' selected':''}"
      onclick="loadJournalEntry('${dateStr}')"
      title="${dateStr}${hasEntry?' · has entry':''}">${d}</div>`;
  }
  html+='</div>';
  document.getElementById('journalMiniCal').innerHTML=html;
  document.getElementById('journalMiniCal').dataset.year=year;
  document.getElementById('journalMiniCal').dataset.month=month;
}

function miniCalNav(dir){
  const el=document.getElementById('journalMiniCal');
  let y=parseInt(el.dataset.year),m=parseInt(el.dataset.month)+dir;
  while(m>11){m-=12;y++;}
  while(m<0){m+=12;y--;}
  buildMiniCal(y,m);
}

function jumpToToday(){
  const today=new Date();
  buildMiniCal(today.getFullYear(),today.getMonth());
  loadJournalEntry(today.toISOString().split('T')[0]);
}

// ── NEWS ───────────────────────────────────────────────
let allNewsItems = [];

function switchNewsTab(tab, el){
  ['news','twitter','events'].forEach(t=>{
    document.getElementById('nspage-'+t).style.display=t===tab?'block':'none';
    document.getElementById('nstab-'+t).classList.toggle('active',t===tab);
  });
  if(tab==='events') loadUpcomingEvents();
  if(tab==='twitter'){
    // Reload twitter widget in case theme changed
    if(window.twttr && window.twttr.widgets) window.twttr.widgets.load();
  }
}

function setNewsTagFilter(tag, el){
  newsTagFilter=tag;
  document.querySelectorAll('.nfbtn').forEach(b=>b.classList.remove('active'));
  el.classList.add('active');
  renderNewsItems();
}

function setNewsTickerFilter(ticker, el){
  newsTickerFilter = newsTickerFilter===ticker ? 'all' : ticker;
  document.querySelectorAll('.nf-ticker-btn').forEach(b=>b.classList.remove('active'));
  if(newsTickerFilter!=='all') el.classList.add('active');
  renderNewsItems();
}

function tagNews(title){
  const t=title.toLowerCase();
  const tags=[];
  if(/target.*(raise|increas|upgrad|hike|up|revise up)/i.test(title)||/price target.*increas/i.test(title)) tags.push({cls:'tag-up',label:'Target ↑',key:'target_up'});
  else if(/target.*(cut|lower|downgrad|reduc|slash|down)/i.test(title)||/price target.*cut/i.test(title)) tags.push({cls:'tag-dn',label:'Target ↓',key:'target_dn'});
  if(/q[1-4]\s*(result|earning|profit|revenue|quarter)/i.test(t)||/quarterly result/i.test(t)||/quarter earn/i.test(t)) tags.push({cls:'tag-quarterly',label:'Quarterly',key:'quarterly'});
  if(/dividend/i.test(t)) tags.push({cls:'tag-dividend',label:'Dividend',key:'dividend'});
  if(/stock.?split|split.?stock/i.test(t)) tags.push({cls:'tag-split',label:'Split',key:'split'});
  if(/bonus.?share|bonus.?issue/i.test(t)) tags.push({cls:'tag-bonus',label:'Bonus',key:'bonus'});
  if(/board meeting|agm|egm|announce|approve|appoint|acqui|merger|buyback|open offer/i.test(t)) tags.push({cls:'tag-announce',label:'Announce',key:'announcement'});
  if(!tags.length) tags.push({cls:'tag-neutral',label:'Neutral',key:'neutral'});
  return tags;
}

function renderNewsItems(){
  const container=document.getElementById('newsContainer');
  if(!allNewsItems.length){container.innerHTML=`<div style="text-align:center;padding:30px;color:var(--muted)">No news loaded.</div>`;return;}
  let filtered=allNewsItems;
  if(newsTickerFilter!=='all') filtered=filtered.filter(n=>n.ticker===newsTickerFilter);
  if(newsTagFilter!=='all') filtered=filtered.filter(n=>n.tags.some(t=>t.key===newsTagFilter));
  if(!filtered.length){container.innerHTML=`<div style="text-align:center;padding:30px;color:var(--muted);font-size:0.78rem">No news matching this filter.</div>`;return;}
  container.innerHTML=filtered.slice(0,30).map(n=>`
    <div class="news-item">
      <div style="display:flex;flex-direction:column;gap:3px;flex-shrink:0">
        <div class="news-ticker">${n.ticker}</div>
        <div>${n.tags.map(t=>`<span class="news-tag ${t.cls}">${t.label}</span>`).join('')}</div>
      </div>
      <div class="news-content">
        <div class="news-title"><a href="${n.link}" target="_blank">${n.title}</a></div>
        <div class="news-meta">${n.pubDate||''} · ${n.source||'Google News'}</div>
      </div>
    </div>`).join('');
}

async function loadNews(d){
  const holdings=d?.holdings||[];
  document.getElementById('newsNote').textContent=`for ${holdings.length} holdings`;
  // Build ticker filter buttons
  const tickerWrap=document.getElementById('newsTickerFilter');
  if(tickerWrap) tickerWrap.innerHTML=`<span style="font-size:0.6rem;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:0.5px;align-self:center">Stock:</span>`
    +holdings.map(h=>`<button class="nf-ticker-btn" onclick="setNewsTickerFilter('${h.tradingsymbol}',this)">${h.tradingsymbol}</button>`).join('');

  const container=document.getElementById('newsContainer');
  container.innerHTML=`<div style="text-align:center;padding:20px;color:var(--muted);font-size:0.75rem">Fetching news...</div>`;
  allNewsItems=[];

  try{
    for(const h of holdings.slice(0,8)){
      const query=encodeURIComponent(h.tradingsymbol+' NSE stock');
      const rssUrl=`https://news.google.com/rss/search?q=${query}&hl=en-IN&gl=IN&ceid=IN:en`;
      try{
        const r=await fetch(`/api/news-proxy?url=${encodeURIComponent(rssUrl)}`);
        if(r.ok){
          const items=await r.json();
          items.forEach(i=>{
            const tags=tagNews(i.title||'');
            allNewsItems.push({...i,ticker:h.tradingsymbol,tags});
          });
        }
      }catch(e){}
    }
    if(allNewsItems.length){
      // Sort by date desc
      allNewsItems.sort((a,b)=>new Date(b.pubDate||0)-new Date(a.pubDate||0));
      renderNewsItems();
    }else{
      container.innerHTML=`<div style="text-align:center;padding:30px;color:var(--muted);font-size:0.78rem">
        News requires the /api/news-proxy endpoint.<br>
        <span style="font-size:0.68rem">Holdings: ${holdings.map(h=>`<a href="https://economictimes.indiatimes.com/markets/stocks/news" target="_blank" style="color:var(--accent)">${h.tradingsymbol}</a>`).join(', ')}</span><br><br>
        <a href="https://economictimes.indiatimes.com/markets/stocks/news" target="_blank" style="color:var(--accent)">→ Economic Times</a>
        &nbsp;·&nbsp;
        <a href="https://www.moneycontrol.com/news/business/stocks/" target="_blank" style="color:var(--accent)">→ MoneyControl</a>
      </div>`;
    }
  }catch(e){container.innerHTML=`<div style="text-align:center;padding:20px;color:var(--muted)">Could not load news.</div>`;}
}

// ── UPCOMING EVENTS (Earnings / Dividends / Splits) ────
async function loadUpcomingEvents(){
  const holdings=(lastData?.holdings)||[];
  if(!holdings.length){document.getElementById('eventsContainer').innerHTML=`<div style="text-align:center;padding:20px;color:var(--muted)">No holdings found.</div>`;return;}
  document.getElementById('eventsNote').textContent=`for ${holdings.length} stocks`;
  const container=document.getElementById('eventsContainer');
  container.innerHTML=`<div style="text-align:center;padding:20px;color:var(--muted);font-size:0.75rem">Loading events data...</div>`;

  // Fetch events from Google News RSS — look for dividend/result/split/bonus keywords
  const events=[];
  const today=new Date();
  const tickers=holdings.map(h=>h.tradingsymbol);

  try{
    for(const ticker of tickers.slice(0,10)){
      // Search for upcoming events
      for(const keyword of ['result date','dividend','board meeting','record date','bonus','split']){
        const query=encodeURIComponent(`${ticker} ${keyword} 2025 2026`);
        const rssUrl=`https://news.google.com/rss/search?q=${query}&hl=en-IN&gl=IN&ceid=IN:en`;
        try{
          const r=await fetch(`/api/news-proxy?url=${encodeURIComponent(rssUrl)}`);
          if(r.ok){
            const items=await r.json();
            items.slice(0,2).forEach(item=>{
              // Classify event type
              const t=item.title.toLowerCase();
              let evType='', evCls='', evLabel='';
              if(/q[1-4].*(result|earning)|quarterly result|result date/i.test(item.title)){evType='earnings';evCls='ev-earnings';evLabel='📊 Earnings';}
              else if(/dividend|interim div|final div/i.test(item.title)){evType='dividend';evCls='ev-dividend';evLabel='💰 Dividend';}
              else if(/stock.?split|split.?stock/i.test(item.title)){evType='split';evCls='ev-split';evLabel='✂️ Split';}
              else if(/bonus.?share/i.test(item.title)){evType='bonus';evCls='ev-bonus';evLabel='🎁 Bonus';}
              else if(/board meeting|agm|record date/i.test(item.title)){evType='meeting';evCls='ev-earnings';evLabel='📋 Meeting';}
              else return;
              events.push({ticker,title:item.title,link:item.link,pubDate:item.pubDate,evType,evCls,evLabel});
            });
          }
        }catch(e){}
      }
    }
  }catch(e){}

  if(!events.length){
    container.innerHTML=`
      <div style="text-align:center;padding:20px;color:var(--muted);font-size:0.78rem;margin-bottom:16px">
        Live event data unavailable. Check these sources:
      </div>
      <div style="display:flex;flex-wrap:wrap;gap:8px;justify-content:center;margin-bottom:20px">
        ${tickers.slice(0,15).map(t=>`
          <a href="https://www.nseindia.com/get-quotes/equity?symbol=${t}" target="_blank"
             style="font-size:0.68rem;color:var(--accent);background:var(--s2);padding:3px 8px;border-radius:4px;border:1px solid var(--border);text-decoration:none">${t}</a>`).join('')}
      </div>
      <div style="display:flex;flex-wrap:wrap;gap:8px;justify-content:center">
        <a href="https://www.nseindia.com/companies-listing/corporate-filings-event-calendar" target="_blank" style="font-size:0.72rem;color:var(--accent);text-decoration:none">→ NSE Event Calendar</a>
        <a href="https://www.bseindia.com/corporates/ann.html" target="_blank" style="font-size:0.72rem;color:var(--accent);text-decoration:none">→ BSE Announcements</a>
        <a href="https://trendlyne.com/earnings/upcoming/" target="_blank" style="font-size:0.72rem;color:var(--accent);text-decoration:none">→ Trendlyne Earnings</a>
        <a href="https://www.screener.in/screens/upcoming-results/" target="_blank" style="font-size:0.72rem;color:var(--accent);text-decoration:none">→ Screener Upcoming</a>
      </div>`;
    return;
  }

  // Group by event type
  const grouped={earnings:[],dividend:[],split:[],bonus:[],meeting:[]};
  events.forEach(e=>{if(grouped[e.evType]) grouped[e.evType].push(e);});

  const sections=[
    {key:'earnings',label:'📊 Earnings / Results'},
    {key:'dividend',label:'💰 Dividends'},
    {key:'split',label:'✂️ Stock Splits'},
    {key:'bonus',label:'🎁 Bonus Shares'},
    {key:'meeting',label:'📋 Board Meetings'},
  ];

  container.innerHTML=sections.filter(s=>grouped[s.key].length>0).map(s=>`
    <div style="margin-bottom:16px">
      <div style="font-size:0.68rem;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:var(--muted);margin-bottom:8px;padding-bottom:6px;border-bottom:1px solid var(--border)">${s.label}</div>
      ${grouped[s.key].map(ev=>`
        <div class="event-row">
          <div class="event-ticker">${ev.ticker}</div>
          <span class="event-type ${ev.evCls}">${ev.evLabel}</span>
          <div style="flex:1;min-width:0">
            <a href="${ev.link}" target="_blank" style="font-size:0.72rem;color:var(--text);text-decoration:none;display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${ev.title}</a>
            <div class="event-date">${ev.pubDate||''}</div>
          </div>
        </div>`).join('')}
    </div>`).join('') +
    `<div style="margin-top:12px;padding:8px;background:var(--s2);border-radius:6px;border:1px solid var(--border);font-size:0.65rem;color:var(--muted);display:flex;flex-wrap:wrap;gap:10px">
      <span>For official dates:</span>
      <a href="https://www.nseindia.com/companies-listing/corporate-filings-event-calendar" target="_blank" style="color:var(--accent);text-decoration:none">NSE Calendar</a>
      <a href="https://trendlyne.com/earnings/upcoming/" target="_blank" style="color:var(--accent);text-decoration:none">Trendlyne</a>
      <a href="https://www.screener.in/screens/upcoming-results/" target="_blank" style="color:var(--accent);text-decoration:none">Screener</a>
    </div>`;
}

// ── RISK MODAL ─────────────────────────────────────────
async function loadStockRiskLimits(){try{const r=await fetch('/api/stock-risks');const d=await r.json();stockRiskLimits=d||{};}catch(e){stockRiskLimits={};}}
function openRiskModal(ticker,cur){riskModalTicker=ticker;document.getElementById('riskModalDesc').textContent=`Custom loss limit for ${ticker}. Global: ${document.getElementById('posLoss').value}%`;document.getElementById('riskModalInput').value=stockRiskLimits[ticker]!==undefined?stockRiskLimits[ticker]:'';document.getElementById('riskModal').classList.add('open');setTimeout(()=>document.getElementById('riskModalInput').focus(),100);}
function closeRiskModal(){document.getElementById('riskModal').classList.remove('open');riskModalTicker='';}
async function saveStockRisk(){const v=document.getElementById('riskModalInput').value.trim();if(v==='')delete stockRiskLimits[riskModalTicker];else stockRiskLimits[riskModalTicker]=parseFloat(v);closeRiskModal();try{await fetch('/api/stock-risks',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(stockRiskLimits)});}catch(e){}if(lastData)renderOverview(lastData);}
document.getElementById('riskModal').addEventListener('click',function(e){if(e.target===this)closeRiskModal();});

// ── INIT ───────────────────────────────────────────────
// ── SETTINGS PERSISTENCE ──────────────────────────────
const SETTINGS_KEY = 'folio_settings_v1';

function saveSettings(){
  const s={
    cagrTarget:    document.getElementById('cagrTarget').value,
    maxLoss:       document.getElementById('maxLoss').value,
    posLoss:       document.getElementById('posLoss').value,
    investedSince: '2020-01-01',
  };
  try{ localStorage.setItem(SETTINGS_KEY, JSON.stringify(s)); }catch(e){}
}

function loadSettings(){
  try{
    const raw = localStorage.getItem(SETTINGS_KEY);
    if(!raw) return;
    const s = JSON.parse(raw);
    if(s.cagrTarget)    document.getElementById('cagrTarget').value    = s.cagrTarget;
    if(s.maxLoss)       document.getElementById('maxLoss').value       = s.maxLoss;
    if(s.posLoss)       document.getElementById('posLoss').value       = s.posLoss;

  }catch(e){}
}

window.addEventListener('load',()=>{
  // Restore theme first — before anything renders
  try{
    const savedTheme = localStorage.getItem('folio_theme');
    if(savedTheme === 'light'){
      isDark = false;
      document.body.classList.add('light');
      document.querySelector('.theme-btn').textContent = '☀️';
    }
  }catch(e){}

  const params=new URLSearchParams(window.location.search);
  if(params.get('error')){document.getElementById('connectError').textContent='Login error: '+params.get('error');document.getElementById('connectError').style.display='block';history.replaceState({},'','/');}
  loadSettings();
  loadStockRiskLimits();
  loadJournalFromServer();
  fetchNiftyPE();
  checkAuth();
});
</script>
</html>
"""



@app.route("/")
def index():
    return DASHBOARD_HTML


# ─────────────────────────────────────────────────────────
# RUN
# ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n" + "="*55)
    print("  folio · Real-Time Portfolio Dashboard")
    print("="*55)
    if API_KEY == "YOUR_API_KEY_HERE":
        print("\n  ⚠  Add your Kite API keys:")
        print("     Edit server.py lines 20-21  OR")
        print("     export KITE_API_KEY=xxx")
        print("     export KITE_API_SECRET=xxx")
    print("\n  → Open http://localhost:5000")
    print("  → Click 'Connect Zerodha'")
    print("  → Dashboard goes live instantly")
    print("\n  Token expires at midnight (Zerodha rule)")
    print("  Re-run server.py each day or keep it running")
    print("="*55 + "\n")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
