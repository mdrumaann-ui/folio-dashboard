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
            "history":       get_history_series(),
            "stock_risks":   load_stock_risks(),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
  --accent:#6c63ff;--gain:#00e676;--loss:#ff5252;--warn:#ffab40;
  --gain-bg:rgba(0,230,118,0.07);--loss-bg:rgba(255,82,82,0.07);--warn-bg:rgba(255,171,64,0.08);
  --card-shadow:0 2px 12px rgba(0,0,0,0.3);
}
.light{
  --bg:#f4f6fb;--s1:#ffffff;--s2:#f0f2f9;--s3:#e4e8f5;
  --border:#dde1f0;--text:#1a1d2e;--muted:#7b82a8;
  --accent:#6c63ff;--gain:#00a152;--loss:#d32f2f;--warn:#e65100;
  --gain-bg:rgba(0,161,82,0.07);--loss-bg:rgba(211,47,47,0.07);--warn-bg:rgba(230,81,0,0.08);
  --card-shadow:0 2px 12px rgba(0,0,0,0.06);
}
*{box-sizing:border-box;margin:0;padding:0;transition:background 0.2s,color 0.2s,border-color 0.2s;}
body{background:var(--bg);color:var(--text);font-family:'Inter',sans-serif;font-size:14px;min-height:100vh;}

/* HEADER */
header{background:var(--s1);border-bottom:1px solid var(--border);padding:0 24px;height:56px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100;box-shadow:var(--card-shadow);}
.logo{font-size:1.1rem;font-weight:700;letter-spacing:-0.5px;color:var(--text);}
.logo em{color:var(--accent);font-style:normal;}
.hright{display:flex;align-items:center;gap:10px;}
.live-pill{display:flex;align-items:center;gap:6px;background:var(--gain-bg);border:1px solid var(--gain);padding:4px 10px;border-radius:20px;font-size:0.7rem;font-weight:600;color:var(--gain);}
.live-dot{width:6px;height:6px;border-radius:50%;background:var(--gain);animation:pulse 2s infinite;}
.live-dot.off{background:var(--muted);animation:none;}
@keyframes pulse{0%,100%{opacity:1;box-shadow:0 0 0 0 rgba(0,230,118,0.4)}50%{opacity:0.8;box-shadow:0 0 0 5px rgba(0,230,118,0)}}
.btn{font-family:'Inter',sans-serif;font-weight:500;font-size:0.75rem;border:1px solid var(--border);background:var(--s2);color:var(--muted);padding:6px 14px;border-radius:6px;cursor:pointer;transition:all 0.15s;}
.btn:hover{border-color:var(--accent);color:var(--accent);}
.btn-accent{background:var(--accent);color:#fff;border-color:var(--accent);}
.btn-accent:hover{opacity:0.9;color:#fff;}
.theme-btn{width:32px;height:32px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:1rem;cursor:pointer;border:1px solid var(--border);background:var(--s2);}

/* CONNECT SCREEN */
#connectScreen{display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:80vh;text-align:center;padding:40px 20px;}
#connectScreen h1{font-size:2.4rem;font-weight:700;letter-spacing:-1px;margin-bottom:10px;}
#connectScreen h1 span{color:var(--accent);}
#connectScreen p{color:var(--muted);font-size:0.9rem;line-height:1.7;max-width:420px;margin-bottom:32px;}
.steps-list{display:flex;flex-direction:column;gap:10px;margin-bottom:28px;text-align:left;width:100%;max-width:360px;}
.step-item{display:flex;gap:12px;align-items:flex-start;background:var(--s1);border:1px solid var(--border);border-radius:8px;padding:12px 14px;}
.step-n{width:22px;height:22px;background:var(--accent);color:#fff;font-size:0.7rem;font-weight:700;border-radius:50%;display:flex;align-items:center;justify-content:center;flex-shrink:0;}
.step-t{font-size:0.8rem;color:var(--muted);line-height:1.5;}
.step-t strong{color:var(--text);}
.connect-btn-big{background:var(--accent);color:#fff;border:none;padding:14px 48px;border-radius:8px;font-family:'Inter',sans-serif;font-weight:600;font-size:0.95rem;cursor:pointer;transition:all 0.2s;box-shadow:0 4px 20px rgba(108,99,255,0.4);}
.connect-btn-big:hover{transform:translateY(-2px);box-shadow:0 6px 28px rgba(108,99,255,0.5);}

/* MAIN LAYOUT */
#dashboard{display:none;padding:20px 24px 40px;}
.settings-bar{background:var(--s1);border:1px solid var(--border);border-radius:10px;padding:14px 18px;margin-bottom:18px;display:flex;gap:16px;align-items:flex-end;flex-wrap:wrap;}
.sg{display:flex;flex-direction:column;gap:4px;}
.sg label{font-size:0.62rem;font-weight:600;letter-spacing:1px;text-transform:uppercase;color:var(--muted);}
.sg input{background:var(--s2);border:1px solid var(--border);color:var(--text);padding:7px 10px;border-radius:6px;font-family:'Inter',sans-serif;font-size:0.82rem;width:120px;outline:none;}
.sg input:focus{border-color:var(--accent);}
.updated-lbl{font-size:0.68rem;color:var(--muted);margin-left:auto;align-self:center;}

/* ALERT */
.alert{padding:10px 16px;margin-bottom:14px;display:none;align-items:center;gap:10px;font-size:0.82rem;border-radius:8px;border-left:3px solid;}
.alert.show{display:flex;}
.alert-danger{background:var(--loss-bg);border-color:var(--loss);}
.alert-warn{background:var(--warn-bg);border-color:var(--warn);}
.alert-ok{background:var(--gain-bg);border-color:var(--gain);}

/* SUMMARY CARDS */
.cards-row{display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin-bottom:18px;}
.card{background:var(--s1);border:1px solid var(--border);border-radius:10px;padding:16px;box-shadow:var(--card-shadow);}
.card-lbl{font-size:0.62rem;font-weight:600;letter-spacing:1px;text-transform:uppercase;color:var(--muted);margin-bottom:8px;}
.card-val{font-size:1.3rem;font-weight:700;letter-spacing:-0.5px;line-height:1;}
.card-sub{font-size:0.68rem;margin-top:5px;color:var(--muted);}
.g{color:var(--gain);}.l{color:var(--loss);}.w{color:var(--warn);}.m{color:var(--muted);}

/* ROW LAYOUTS */
.row2{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px;}
.row3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px;margin-bottom:14px;}
.row12{display:grid;grid-template-columns:1fr 360px;gap:14px;margin-bottom:14px;}
.panel{background:var(--s1);border:1px solid var(--border);border-radius:10px;padding:18px;box-shadow:var(--card-shadow);}
.panel-title{font-size:0.72rem;font-weight:600;letter-spacing:1px;text-transform:uppercase;color:var(--muted);margin-bottom:14px;display:flex;justify-content:space-between;align-items:center;}
.panel-title span{font-size:0.65rem;font-weight:400;text-transform:none;letter-spacing:0;color:var(--muted);}

/* RISK METER GAUGE */
.risk-gauge{position:relative;width:140px;height:80px;margin:0 auto 10px;}
.gauge-label{text-align:center;}
.gauge-pct{font-size:1.8rem;font-weight:700;letter-spacing:-1px;line-height:1;}
.gauge-status{font-size:0.68rem;color:var(--muted);margin-top:3px;}

/* STOCK RISK BARS */
.stock-risk-item{margin-bottom:10px;}
.sri-meta{display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;}
.sri-name{font-weight:600;font-size:0.78rem;}
.sri-pct{font-size:0.72rem;font-family:'DM Mono',monospace;}
.sri-track{height:6px;background:var(--s3);border-radius:3px;overflow:hidden;}
.sri-fill{height:100%;border-radius:3px;transition:width 0.8s ease;}
.sri-badge{font-size:0.6rem;padding:2px 6px;border-radius:3px;font-weight:600;}
.badge-ok{background:var(--gain-bg);color:var(--gain);}
.badge-warn{background:var(--warn-bg);color:var(--warn);}
.badge-bad{background:var(--loss-bg);color:var(--loss);}

/* CHART TABS */
.chart-tabs{display:flex;gap:6px;}
.ctab{font-size:0.68rem;font-weight:500;padding:4px 10px;border-radius:4px;border:1px solid var(--border);background:transparent;color:var(--muted);cursor:pointer;transition:all 0.15s;}
.ctab.active{background:var(--accent);color:#fff;border-color:var(--accent);}

/* TRADE ANALYTICS */
.ta-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:12px;}
.ta-box{background:var(--s2);border-radius:8px;padding:14px;text-align:center;border:1px solid var(--border);}
.ta-num{font-size:1.6rem;font-weight:700;letter-spacing:-1px;}
.ta-lbl{font-size:0.6rem;font-weight:600;letter-spacing:1px;text-transform:uppercase;color:var(--muted);margin-top:3px;}
.ta-sub{font-size:0.65rem;margin-top:3px;}

/* STAT ROWS */
.sr{display:flex;justify-content:space-between;align-items:center;padding:7px 0;border-bottom:1px solid var(--border);}
.sr:last-child{border-bottom:none;}
.sn{font-size:0.75rem;color:var(--muted);}
.sv{font-family:'DM Mono',monospace;font-size:0.78rem;font-weight:500;}

/* HOLDINGS TABLE */
.tbl-wrap{overflow-x:auto;}
table{width:100%;border-collapse:collapse;}
thead th{font-size:0.62rem;font-weight:600;letter-spacing:1px;text-transform:uppercase;color:var(--muted);padding:0 10px 10px;text-align:right;border-bottom:1px solid var(--border);}
thead th:first-child{text-align:left;}
tbody td{padding:10px 10px;font-size:0.78rem;text-align:right;border-bottom:1px solid var(--border);}
tbody td:first-child{text-align:left;}
tbody tr:hover td{background:var(--s2);}
tbody tr:last-child td{border-bottom:none;}
.tk{font-weight:700;font-size:0.82rem;}
.tv-link{color:var(--text);transition:color 0.15s;}
.tv-link:hover{color:var(--accent);}
/* RISK EDIT MODAL */
.modal-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:200;align-items:center;justify-content:center;}
.modal-overlay.open{display:flex;}
.modal{background:var(--s1);border:1px solid var(--border);border-radius:12px;padding:24px;width:340px;box-shadow:0 20px 60px rgba(0,0,0,0.4);}
.modal h3{font-size:1rem;font-weight:700;margin-bottom:4px;}
.modal p{font-size:0.76rem;color:var(--muted);margin-bottom:16px;}
.modal input{width:100%;background:var(--s2);border:1px solid var(--border);color:var(--text);padding:9px 12px;border-radius:6px;font-size:0.9rem;outline:none;margin-bottom:12px;font-family:'Inter',sans-serif;}
.modal input:focus{border-color:var(--accent);}
.modal-btns{display:flex;gap:8px;}
.modal-btns button{flex:1;padding:9px;border-radius:6px;font-size:0.8rem;font-weight:600;cursor:pointer;border:none;font-family:'Inter',sans-serif;}
.mbtn-save{background:var(--accent);color:#fff;}
.mbtn-cancel{background:var(--s3);color:var(--muted);}
/* HOLDINGS ANALYTICS */
.ha-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:14px;}
.ha-box{background:var(--s2);border-radius:8px;padding:12px;text-align:center;border:1px solid var(--border);}
.ha-num{font-size:1.1rem;font-weight:700;letter-spacing:-0.5px;}
.ha-lbl{font-size:0.58rem;font-weight:600;letter-spacing:1px;text-transform:uppercase;color:var(--muted);margin-top:2px;}
/* PER STOCK ANALYTICS TABLE */
.psa-table{width:100%;border-collapse:collapse;font-size:0.76rem;}
.psa-table th{font-size:0.6rem;font-weight:600;letter-spacing:1px;text-transform:uppercase;color:var(--muted);padding:0 8px 8px;text-align:right;border-bottom:1px solid var(--border);}
.psa-table th:first-child{text-align:left;}
.psa-table td{padding:9px 8px;text-align:right;border-bottom:1px solid var(--border);}
.psa-table td:first-child{text-align:left;}
.psa-table tr:last-child td{border-bottom:none;}
.psa-table tr:hover td{background:var(--s2);}
.risk-edit-btn{font-size:0.6rem;padding:2px 7px;border-radius:3px;border:1px solid var(--border);background:transparent;color:var(--muted);cursor:pointer;margin-left:4px;}
.risk-edit-btn:hover{border-color:var(--accent);color:var(--accent);}
.risk-bar-cell{width:80px;}
.mini-bar{height:5px;background:var(--s3);border-radius:2px;overflow:hidden;margin-top:3px;}
.mini-fill{height:100%;border-radius:2px;}

/* CAGR PANEL */
.cagr-main{text-align:center;padding:16px 0 12px;}
.cagr-num{font-size:2.8rem;font-weight:700;letter-spacing:-2px;line-height:1;}
.cagr-lbl{font-size:0.68rem;color:var(--muted);margin-top:4px;}
.cagr-bar-wrap{margin:12px 0;background:var(--s3);border-radius:4px;height:8px;overflow:hidden;position:relative;}
.cagr-bar-fill{height:100%;border-radius:4px;transition:width 1s ease;}
.cagr-target-line{position:absolute;right:0;top:-3px;width:2px;height:14px;background:var(--text);opacity:0.3;border-radius:1px;}
.cagr-stats{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:12px;}
.cagr-stat{background:var(--s2);border-radius:6px;padding:10px;text-align:center;}
.cagr-stat .val{font-size:1rem;font-weight:700;}
.cagr-stat .lbl{font-size:0.6rem;color:var(--muted);text-transform:uppercase;letter-spacing:0.5px;margin-top:2px;}

footer{border-top:1px solid var(--border);padding:14px 24px;display:flex;justify-content:space-between;align-items:center;}
footer p{font-size:0.65rem;color:var(--muted);}

@media(max-width:1100px){.cards-row{grid-template-columns:repeat(3,1fr);}.row3{grid-template-columns:1fr 1fr;}.row12{grid-template-columns:1fr;}}
@media(max-width:700px){.cards-row{grid-template-columns:repeat(2,1fr);}.row2,.row3,.row12{grid-template-columns:1fr;}}
</style>
</head>
<body>

<header>
  <div class="logo">folio<em>.</em>live</div>
  <div class="hright">
    <div id="livePill" class="live-pill" style="display:none">
      <div class="live-dot" id="liveDot"></div>
      <span id="liveName">Live</span>
    </div>
    <span id="updatedLbl" style="font-size:0.68rem;color:var(--muted)"></span>
    <button class="btn" id="refreshBtn" onclick="forceRefresh()" style="display:none">↻ Refresh</button>
    <button class="btn" id="logoutBtn" onclick="logout()" style="display:none">Disconnect</button>
    <div class="theme-btn" onclick="toggleTheme()" title="Toggle day/night">🌙</div>
  </div>
</header>

<!-- CONNECT SCREEN -->
<div id="connectScreen">
  <h1>Your portfolio,<br><span>live.</span></h1>
  <p>Connect your Zerodha account. Holdings, P&L, risk, and growth update automatically every 60 seconds.</p>
  <div class="steps-list">
    <div class="step-item"><div class="step-n">1</div><div class="step-t">Make sure <strong>server.py is running</strong></div></div>
    <div class="step-item"><div class="step-n">2</div><div class="step-t">Click below — redirected to <strong>Zerodha login</strong></div></div>
    <div class="step-item"><div class="step-n">3</div><div class="step-t">Session lasts until <strong>midnight</strong> daily</div></div>
  </div>
  <button class="connect-btn-big" onclick="connectZerodha()">Connect Zerodha →</button>
  <div id="connectError" style="margin-top:14px;font-size:0.75rem;color:var(--loss);display:none"></div>
</div>

<!-- DASHBOARD -->
<div id="dashboard">

  <!-- SETTINGS -->
  <div class="settings-bar">
    <div class="sg"><label>CAGR Target %</label><input type="number" id="cagrTarget" value="15" min="1" max="100" onchange="loadData()"></div>
    <div class="sg"><label>Max Loss %</label><input type="number" id="maxLoss" value="10" min="1" max="50" onchange="loadData()"></div>
    <div class="sg"><label>Per Stock Loss %</label><input type="number" id="posLoss" value="10" min="1" max="50" onchange="loadData()"></div>
    <div class="sg"><label>Invested Since</label><input type="date" id="investedSince" value="2023-01-01" onchange="loadData()"></div>
    <span class="updated-lbl" id="updatedBar"></span>
  </div>

  <!-- ALERT -->
  <div class="alert" id="alertBanner"></div>

  <!-- SUMMARY CARDS -->
  <div class="cards-row" id="cardsRow"></div>

  <!-- ROW 1: CAGR + OVERALL RISK + GROWTH CHART -->
  <div class="row3" id="row1">
    <!-- CAGR -->
    <div class="panel">
      <div class="panel-title">CAGR</div>
      <div id="cagrPanel"></div>
    </div>

    <!-- OVERALL RISK METER -->
    <div class="panel">
      <div class="panel-title">Portfolio Risk</div>
      <div id="overallRisk"></div>
    </div>

    <!-- GROWTH CHART -->
    <div class="panel">
      <div class="panel-title">
        Portfolio Growth
        <div style="display:flex;align-items:center;gap:8px">
          <span id="growthNotice" style="font-size:0.6rem;color:var(--muted);font-weight:400"></span>
          <div class="chart-tabs">
            <button class="ctab active" onclick="setChartTab(this,'monthly')">Monthly</button>
            <button class="ctab" onclick="setChartTab(this,'yearly')">Yearly</button>
          </div>
        </div>
      </div>
      <canvas id="growthChart" height="160"></canvas>
    </div>
  </div>

  <!-- ROW 2: STOCK RISK + HOLDINGS TABLE -->
  <!-- HOLDINGS TABLE (full width) -->
  <div class="panel" style="margin-bottom:14px">
    <div class="panel-title">Holdings <span id="holdCount"></span></div>
    <div class="tbl-wrap">
      <table>
        <thead><tr>
          <th>Stock</th><th>Qty</th><th>Avg Cost</th><th>LTP</th><th>Invested ₹</th><th>Current ₹</th><th>P&L ₹</th><th>Return %</th><th>Weight</th><th>Risk</th>
        </tr></thead>
        <tbody id="holdTbody"></tbody>
      </table>
    </div>
  </div>

  <!-- ROW 3: HOLDINGS ANALYTICS -->
  <div class="panel" style="margin-bottom:14px">
    <div class="panel-title">Holdings Analytics <span style="font-size:0.65rem;color:var(--muted);font-weight:400">per stock performance</span></div>
    <div id="holdingsAnalytics">
      <div style="text-align:center;padding:20px;color:var(--muted);font-size:0.82rem">Loading...</div>
    </div>
  </div>

  <!-- ROW 4: TRADE ANALYTICS -->
  <div class="panel" style="margin-bottom:14px">
    <div class="panel-title">Trade Analytics <span id="tradeNote"></span></div>
    <div id="tradePanel">
      <div style="text-align:center;padding:30px;color:var(--muted);font-size:0.82rem">
        Trade data loads from today's executed trades.<br>
        <span style="font-size:0.72rem">If you haven't traded today, yesterday's data shows here.</span>
      </div>
    </div>
  </div>

</div>

<!-- RISK EDIT MODAL -->
<div class="modal-overlay" id="riskModal">
  <div class="modal">
    <h3>Set Risk Limit</h3>
    <p id="riskModalDesc">Set a custom loss % limit for this stock. Leave blank to use the global default.</p>
    <input type="number" id="riskModalInput" placeholder="e.g. 15" min="1" max="100" step="0.5">
    <div class="modal-btns">
      <button class="mbtn-save" onclick="saveStockRisk()">Save</button>
      <button class="mbtn-cancel" onclick="closeRiskModal()">Cancel</button>
    </div>
  </div>
</div>

<footer>
  <p>Live via Kite Connect API · Refreshes every 60s · Token expires midnight</p>
  <p id="footerUser"></p>
</footer>

<script>
const BASE = '';
let growthChart = null;
let compChart   = null;
let chartMode   = 'monthly';
let lastData    = null;
let isDark      = true;
let autoTimer   = null;

// ── STOCK RISK LIMITS (per stock, saved to JSONBin) ───
let stockRiskLimits = {};  // { "FORCEMOT": 15, "BIOCON": 8 }
let riskModalTicker = '';

async function loadStockRiskLimits() {
  try {
    const r = await fetch('/api/stock-risks');
    const d = await r.json();
    stockRiskLimits = d || {};
  } catch(e) { stockRiskLimits = {}; }
}

function openRiskModal(ticker, currentLimit) {
  riskModalTicker = ticker;
  document.getElementById('riskModalDesc').textContent =
    `Custom loss limit for ${ticker}. Global default: ${document.getElementById('posLoss').value}%`;
  document.getElementById('riskModalInput').value =
    stockRiskLimits[ticker] !== undefined ? stockRiskLimits[ticker] : '';
  document.getElementById('riskModalInput').placeholder =
    `Global: ${document.getElementById('posLoss').value}% (leave blank to use)`;
  document.getElementById('riskModal').classList.add('open');
  setTimeout(() => document.getElementById('riskModalInput').focus(), 100);
}

function closeRiskModal() {
  document.getElementById('riskModal').classList.remove('open');
  riskModalTicker = '';
}

async function saveStockRisk() {
  const val = document.getElementById('riskModalInput').value.trim();
  if (val === '') {
    delete stockRiskLimits[riskModalTicker];
  } else {
    stockRiskLimits[riskModalTicker] = parseFloat(val);
  }
  closeRiskModal();
  // Save to server
  try {
    await fetch('/api/stock-risks', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify(stockRiskLimits)
    });
  } catch(e) { console.error('Could not save risk limits', e); }
  // Re-render with new limits
  if (lastData) renderAll(lastData);
}

// Close modal on overlay click
document.getElementById('riskModal').addEventListener('click', function(e) {
  if (e.target === this) closeRiskModal();
});

// ── HOLDINGS ANALYTICS ─────────────────────────────────
function renderHoldingsAnalytics(d) {
  const holdings = d.holdings;
  const totalInvested = holdings.reduce((s,h) => s + h.invested_value, 0);
  const totalValue    = holdings.reduce((s,h) => s + h.current_value, 0);
  const totalPL       = totalValue - totalInvested;
  const winners       = holdings.filter(h => h.pnl > 0);
  const losers        = holdings.filter(h => h.pnl < 0);
  const best          = [...holdings].sort((a,b) => b.pnl_pct - a.pnl_pct)[0];
  const worst         = [...holdings].sort((a,b) => a.pnl_pct - b.pnl_pct)[0];
  const biggestPos    = [...holdings].sort((a,b) => b.invested_value - a.invested_value)[0];
  const avgReturn     = holdings.reduce((s,h) => s + h.pnl_pct, 0) / holdings.length;

  const el = document.getElementById('holdingsAnalytics');
  el.innerHTML = `
    <!-- SUMMARY STATS -->
    <div class="ha-grid">
      <div class="ha-box">
        <div class="ha-num" style="color:var(--gain)">${winners.length}</div>
        <div class="ha-lbl">In Profit</div>
      </div>
      <div class="ha-box">
        <div class="ha-num" style="color:var(--loss)">${losers.length}</div>
        <div class="ha-lbl">In Loss</div>
      </div>
      <div class="ha-box">
        <div class="ha-num" style="color:${avgReturn>=0?'var(--gain)':'var(--loss)'}">${pct(avgReturn)}</div>
        <div class="ha-lbl">Avg Return</div>
      </div>
      <div class="ha-box">
        <div class="ha-num">${fmtL(totalInvested)}</div>
        <div class="ha-lbl">Total Invested</div>
      </div>
    </div>

    <!-- BEST / WORST / BIGGEST -->
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;margin-bottom:16px">
      <div class="ha-box" style="border-color:rgba(0,230,118,0.3)">
        <div style="font-size:0.6rem;color:var(--muted);margin-bottom:4px;letter-spacing:1px">BEST PERFORMER</div>
        <div style="font-weight:700;font-size:0.9rem">${best?.tradingsymbol||'-'}</div>
        <div style="font-size:0.82rem;color:var(--gain);font-weight:600">${best?pct(best.pnl_pct):'-'}</div>
        <div style="font-size:0.7rem;color:var(--muted)">${best?'+'+fmtL(best.pnl):''}</div>
      </div>
      <div class="ha-box" style="border-color:rgba(255,82,82,0.3)">
        <div style="font-size:0.6rem;color:var(--muted);margin-bottom:4px;letter-spacing:1px">WORST PERFORMER</div>
        <div style="font-weight:700;font-size:0.9rem">${worst?.tradingsymbol||'-'}</div>
        <div style="font-size:0.82rem;color:var(--loss);font-weight:600">${worst?pct(worst.pnl_pct):'-'}</div>
        <div style="font-size:0.7rem;color:var(--muted)">${worst?fmtL(worst.pnl):''}</div>
      </div>
      <div class="ha-box">
        <div style="font-size:0.6rem;color:var(--muted);margin-bottom:4px;letter-spacing:1px">BIGGEST POSITION</div>
        <div style="font-weight:700;font-size:0.9rem">${biggestPos?.tradingsymbol||'-'}</div>
        <div style="font-size:0.82rem;font-weight:600">${biggestPos?fmtL(biggestPos.invested_value):'-'}</div>
        <div style="font-size:0.7rem;color:var(--muted)">${biggestPos?biggestPos.weight_pct+'% of portfolio':''}</div>
      </div>
    </div>

    <!-- PER STOCK TABLE -->
    <div style="overflow-x:auto">
      <table class="psa-table">
        <thead><tr>
          <th>Stock</th>
          <th>Invested</th>
          <th>Current</th>
          <th>Gain/Loss ₹</th>
          <th>Return %</th>
          <th>Weight</th>
          <th>Risk Limit</th>
        </tr></thead>
        <tbody>
          ${[...holdings].sort((a,b)=>b.pnl_pct-a.pnl_pct).map(h => {
            const customLimit = stockRiskLimits[h.tradingsymbol] || d.settings.pos_loss_pct;
            const isCustom    = stockRiskLimits[h.tradingsymbol] !== undefined;
            const exchange    = h.exchange || 'NSE';
            const tvUrl       = `https://www.tradingview.com/chart/?symbol=${exchange}%3A${h.tradingsymbol}`;
            return `<tr>
              <td>
                <a href="${tvUrl}" target="_blank" style="text-decoration:none">
                  <span class="tv-link" style="font-weight:700">${h.tradingsymbol}</span>
                  <span style="font-size:0.55rem;color:var(--muted)"> ↗</span>
                </a>
              </td>
              <td>₹${Math.round(h.invested_value).toLocaleString('en-IN')}</td>
              <td>₹${Math.round(h.current_value).toLocaleString('en-IN')}</td>
              <td class="${gc(h.pnl)}">${h.pnl>=0?'+':'-'}₹${Math.abs(Math.round(h.pnl)).toLocaleString('en-IN')}</td>
              <td class="${gc(h.pnl_pct)}" style="font-weight:600">${pct(h.pnl_pct)}</td>
              <td class="m">${h.weight_pct}%</td>
              <td>
                <span style="font-family:'DM Mono',monospace;font-size:0.75rem;color:${isCustom?'var(--accent)':'var(--muted)'}">${customLimit}%${isCustom?' (custom)':''}</span>
                <button class="risk-edit-btn" onclick="openRiskModal('${h.tradingsymbol}',${customLimit})">✎</button>
              </td>
            </tr>`;
          }).join('')}
        </tbody>
      </table>
    </div>`;
}

// ── THEME ──────────────────────────────────────────────
function toggleTheme() {
  isDark = !isDark;
  document.body.classList.toggle('light', !isDark);
  document.querySelector('.theme-btn').textContent = isDark ? '🌙' : '☀️';
  if (lastData) renderGrowthChart(lastData);
}

// ── FORMAT ─────────────────────────────────────────────
const fmtL = n => {
  const a = Math.abs(n);
  const s = n < 0 ? '-' : '';
  if(a>=10000000) return s+(a/10000000).toFixed(2)+'Cr';
  if(a>=100000)   return s+(a/100000).toFixed(2)+'L';
  if(a>=1000)     return s+(a/1000).toFixed(1)+'K';
  return '₹'+a.toFixed(0);
};
const pct  = (n,d=1) => (n>=0?'+':'')+n.toFixed(d)+'%';
const gc   = n => n>=0?'g':'l';
const gclr = n => n>=0?'var(--gain)':'var(--loss)';

// ── AUTH ───────────────────────────────────────────────
async function checkAuth() {
  try {
    const r = await fetch('/auth/status');
    const d = await r.json();
    if (d.connected) { showDashboard(d.user_name); loadData(); startAuto(); }
    else showConnect();
  } catch(e) {
    showConnect();
    document.getElementById('connectError').textContent = 'Cannot reach server.';
    document.getElementById('connectError').style.display = 'block';
  }
}
function connectZerodha(){ window.location.href = '/auth/login'; }
async function logout(){ await fetch('/auth/logout'); stopAuto(); showConnect(); }
async function forceRefresh(){ await fetch('/api/refresh'); loadData(); }

function showConnect() {
  document.getElementById('connectScreen').style.display = 'flex';
  document.getElementById('dashboard').style.display = 'none';
  document.getElementById('livePill').style.display = 'none';
  document.getElementById('refreshBtn').style.display = 'none';
  document.getElementById('logoutBtn').style.display = 'none';
}
function showDashboard(name) {
  document.getElementById('connectScreen').style.display = 'none';
  document.getElementById('dashboard').style.display = 'block';
  document.getElementById('livePill').style.display = 'flex';
  document.getElementById('liveName').textContent = (name||'') + ' · Live';
  document.getElementById('refreshBtn').style.display = 'inline-block';
  document.getElementById('logoutBtn').style.display = 'inline-block';
  document.getElementById('footerUser').textContent = name || '';
}
function startAuto(){ stopAuto(); autoTimer = setInterval(loadData, 60000); }
function stopAuto(){ if(autoTimer){ clearInterval(autoTimer); autoTimer=null; } }

// ── LOAD DATA ──────────────────────────────────────────
async function loadData() {
  const params = new URLSearchParams({
    cagr_target:    document.getElementById('cagrTarget').value,
    max_loss_pct:   document.getElementById('maxLoss').value,
    pos_loss_pct:   document.getElementById('posLoss').value,
    invested_since: document.getElementById('investedSince').value,
  });
  try {
    const r = await fetch(`/api/summary?${params}`);
    const d = await r.json();
    if(d.error){ console.error(d.error); return; }
    lastData = d;
    renderAll(d);
    const t = new Date().toLocaleTimeString('en-IN');
    document.getElementById('updatedBar').textContent = 'Updated '+t;
    document.getElementById('updatedLbl').textContent = t;
  } catch(e){ console.error(e); }
}

// ── RENDER ALL ─────────────────────────────────────────
function renderAll(d) {
  const p  = d.portfolio;
  const rs = d.risk;
  const st = d.settings;
  const ts = d.trade_stats;
  // Sync stock risk limits from server response
  if (d.stock_risks) stockRiskLimits = d.stock_risks;

  // ALERT
  const ab = document.getElementById('alertBanner');
  if(rs.stop_investing){
    ab.className='alert alert-danger show';
    ab.innerHTML=`🚨 <strong>STOP INVESTING.</strong> Portfolio has lost ${fmtL(rs.actual_loss)} — exceeds your ${st.max_loss_pct}% limit on total capital of ${fmtL(p.total_capital)}.`;
  } else if(rs.loss_used_pct>70){
    ab.className='alert alert-warn show';
    ab.innerHTML=`⚠️ <strong>Caution.</strong> ${rs.loss_used_pct.toFixed(0)}% of loss budget used. Buffer left: ${fmtL(rs.loss_remaining)}.`;
  } else {
    ab.className='alert alert-ok show';
    ab.innerHTML=`✅ <strong>All clear.</strong> Within risk limits. Cash: ${fmtL(p.cash_available)} · Total Capital: ${fmtL(p.total_capital)}`;
  }

  // CARDS
  document.getElementById('cardsRow').innerHTML = [
    ['Live Value',    fmtL(p.total_value),  null,                      null],
    ['Total Capital', fmtL(p.total_capital),'Holdings + Cash',         null],
    ['Invested',      fmtL(p.total_cost),   p.holdings_count+' stocks',null],
    ['P&L',          (p.total_pl>=0?'+':'')+fmtL(p.total_pl), pct(p.total_pl_pct), gc(p.total_pl)],
    ['CAGR',          p.cagr+'%',            'Target: '+st.cagr_target+'%', p.cagr>=st.cagr_target?'g':'w'],
    ['Cash',          fmtL(p.cash_available),'Available',              null],
  ].map(([l,v,s,c])=>`
    <div class="card">
      <div class="card-lbl">${l}</div>
      <div class="card-val ${c||''}">${v}</div>
      ${s?`<div class="card-sub">${s}</div>`:''}
    </div>`).join('');

  // CAGR PANEL
  const cagrMet = p.cagr >= st.cagr_target;
  const cagrW   = Math.min(p.cagr / st.cagr_target * 100, 100);
  const cagrClr = cagrMet ? 'var(--gain)' : p.cagr > st.cagr_target*0.7 ? 'var(--warn)' : 'var(--loss)';
  document.getElementById('cagrPanel').innerHTML = `
    <div class="cagr-main">
      <div class="cagr-num" style="color:${cagrClr}">${p.cagr}%</div>
      <div class="cagr-lbl">${cagrMet ? '✓ Beating target' : (st.cagr_target - p.cagr).toFixed(1)+'% below '+st.cagr_target+'% target'}</div>
    </div>
    <div class="cagr-bar-wrap">
      <div class="cagr-bar-fill" style="width:${cagrW}%;background:${cagrClr}"></div>
      <div class="cagr-target-line"></div>
    </div>
    <div class="cagr-stats">
      <div class="cagr-stat">
        <div class="val" style="color:${gclr(p.total_pl_pct)}">${pct(p.total_pl_pct)}</div>
        <div class="lbl">Total Return</div>
      </div>
      <div class="cagr-stat">
        <div class="val" style="color:${cagrClr}">${p.cagr}%</div>
        <div class="lbl">Annual CAGR</div>
      </div>
      <div class="cagr-stat">
        <div class="val">${st.cagr_target}%</div>
        <div class="lbl">Your Target</div>
      </div>
      <div class="cagr-stat" style="border:1px solid ${cagrClr}40">
        <div class="val" style="color:${cagrClr}">${cagrMet ? '+'+((p.cagr-st.cagr_target).toFixed(1))+'%' : '-'+((st.cagr_target-p.cagr).toFixed(1))+'%'}</div>
        <div class="lbl">Gap</div>
      </div>
    </div>`;

  // OVERALL RISK
  const rClr = rs.stop_investing?'var(--loss)':rs.loss_used_pct>70?'var(--warn)':'var(--gain)';
  const rIcon = rs.stop_investing?'🚨':rs.loss_used_pct>70?'⚠️':'✅';
  const rMsg  = rs.stop_investing?'Stop Investing':rs.loss_used_pct>70?'Caution':'Safe to Invest';
  document.getElementById('overallRisk').innerHTML = `
    <div style="text-align:center;padding:14px 0 10px">
      <div style="font-size:2.2rem;margin-bottom:6px">${rIcon}</div>
      <div style="font-size:1.5rem;font-weight:700;color:${rClr}">${rMsg}</div>
      <div style="font-size:0.72rem;color:var(--muted);margin-top:4px">${rs.loss_used_pct.toFixed(0)}% of ${st.max_loss_pct}% limit used</div>
    </div>
    <div style="background:var(--s3);border-radius:4px;height:8px;margin:0 0 14px;overflow:hidden">
      <div style="height:100%;width:${Math.min(rs.loss_used_pct,100)}%;background:${rClr};border-radius:4px;transition:width 1s ease"></div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
      <div style="background:var(--s2);border-radius:6px;padding:10px;text-align:center">
        <div style="font-size:0.62rem;color:var(--muted);margin-bottom:3px">TOTAL CAPITAL</div>
        <div style="font-weight:700;font-size:0.9rem">${fmtL(p.total_capital)}</div>
      </div>
      <div style="background:var(--s2);border-radius:6px;padding:10px;text-align:center">
        <div style="font-size:0.62rem;color:var(--muted);margin-bottom:3px">MAX LOSS LIMIT</div>
        <div style="font-weight:700;font-size:0.9rem;color:var(--loss)">${fmtL(rs.max_loss_amt)}</div>
      </div>
      <div style="background:var(--s2);border-radius:6px;padding:10px;text-align:center">
        <div style="font-size:0.62rem;color:var(--muted);margin-bottom:3px">CURRENT LOSS</div>
        <div style="font-weight:700;font-size:0.9rem;color:${rs.actual_loss>0?'var(--loss)':'var(--gain)'}">${rs.actual_loss>0?fmtL(rs.actual_loss):'None'}</div>
      </div>
      <div style="background:var(--s2);border-radius:6px;padding:10px;text-align:center">
        <div style="font-size:0.62rem;color:var(--muted);margin-bottom:3px">BUFFER LEFT</div>
        <div style="font-weight:700;font-size:0.9rem;color:${rClr}">${fmtL(rs.loss_remaining)}</div>
      </div>
    </div>
    ${rs.breaching_count>0?`<div style="margin-top:10px;padding:8px 12px;background:var(--loss-bg);border-radius:6px;border-left:3px solid var(--loss);font-size:0.75rem;color:var(--loss)">⚠ ${rs.breaching_count} stock${rs.breaching_count>1?'s':''} breaching ${st.pos_loss_pct}% limit</div>`:''}`;

  // GROWTH CHART
  renderGrowthChart(d);

  // HOLDINGS TABLE — ticker clicks open TradingView, custom risk per stock
  document.getElementById('holdCount').textContent = d.holdings.length+' stocks';
  const pl = st.pos_loss_pct;
  document.getElementById('holdTbody').innerHTML = d.holdings.map(h=>{
    const customLimit = stockRiskLimits[h.tradingsymbol] || pl;
    const barW   = Math.min(Math.abs(h.pnl_pct) / customLimit * 100, 100);
    const barClr = h.pnl_pct < -customLimit ? 'var(--loss)' : h.pnl_pct < -(customLimit*0.7) ? 'var(--warn)' : h.pnl_pct >= 0 ? 'var(--gain)' : 'var(--warn)';
    const badge  = h.pnl_pct < -customLimit
      ? `<span class="sri-badge badge-bad">🔴 BREACH</span>`
      : h.pnl_pct < -(customLimit*0.7)
        ? `<span class="sri-badge badge-warn">🟡 WATCH</span>`
        : `<span class="sri-badge badge-ok">🟢 OK</span>`;
    const exchange = h.exchange || 'NSE';
    const tvUrl = `https://www.tradingview.com/chart/?symbol=${exchange}%3A${h.tradingsymbol}`;
    const isCustom = stockRiskLimits[h.tradingsymbol] !== undefined;
    return `<tr>
      <td>
        <a href="${tvUrl}" target="_blank" style="text-decoration:none;display:flex;align-items:center;gap:4px">
          <span class="tk tv-link">${h.tradingsymbol}</span>
          <span style="font-size:0.6rem;color:var(--muted);opacity:0.5">↗</span>
        </a>
      </td>
      <td>${h.quantity}</td>
      <td>₹${(h.average_price||0).toLocaleString('en-IN',{maximumFractionDigits:0})}</td>
      <td>₹${(h.last_price||0).toLocaleString('en-IN',{maximumFractionDigits:0})}</td>
      <td><strong>₹${Math.round(h.invested_value).toLocaleString('en-IN')}</strong></td>
      <td>₹${Math.round(h.current_value).toLocaleString('en-IN')}</td>
      <td class="${gc(h.pnl)}">${h.pnl>=0?'+':'-'}₹${Math.abs(Math.round(h.pnl)).toLocaleString('en-IN')}</td>
      <td class="${gc(h.pnl_pct)}">${pct(h.pnl_pct)}</td>
      <td class="m">${h.weight_pct}%</td>
      <td>
        <div style="display:flex;align-items:center;gap:4px">
          ${badge}
          <button class="risk-edit-btn" onclick="openRiskModal('${h.tradingsymbol}',${customLimit})" title="Set custom risk limit">
            ${isCustom ? customLimit+'%✎' : '✎'}
          </button>
        </div>
        <div class="mini-bar" style="margin-top:4px">
          <div class="mini-fill" style="width:${h.pnl_pct>=0?100:barW}%;background:${barClr}"></div>
        </div>
      </td>
    </tr>`;
  }).join('');

  // HOLDINGS ANALYTICS — per stock + overall summary
  renderHoldingsAnalytics(d);

  // TRADE ANALYTICS
  if(ts && ts.total_trades > 0) {
    const pfClr   = ts.profit_factor>=2?'var(--gain)':ts.profit_factor>=1?'var(--warn)':'var(--loss)';
    const pfLabel = ts.profit_factor>=200?'∞':ts.profit_factor>=2?'EXCELLENT':ts.profit_factor>=1.5?'GOOD':ts.profit_factor>=1?'MARGINAL':'LOSING';
    const pfVal   = ts.profit_factor>=200?'∞':ts.profit_factor.toFixed(2);
    document.getElementById('tradeNote').textContent = ts.total_trades+' trades';
    document.getElementById('tradePanel').innerHTML = `
      <div class="ta-grid">
        <div class="ta-box">
          <div class="ta-num" style="color:${ts.win_rate>=50?'var(--gain)':'var(--loss)'}">${ts.win_rate}%</div>
          <div class="ta-lbl">Win Rate</div>
          <div class="ta-sub m">${ts.wins}W / ${ts.losses}L</div>
        </div>
        <div class="ta-box" style="border-color:${pfClr}40">
          <div class="ta-num" style="color:${pfClr}">${pfVal}</div>
          <div class="ta-lbl">Profit Factor</div>
          <div class="ta-sub" style="color:${pfClr}">${pfLabel}</div>
        </div>
        <div class="ta-box">
          <div class="ta-num g">${fmtL(ts.win_amt)}</div>
          <div class="ta-lbl">Gross Profit</div>
          <div class="ta-sub m">Avg: ${fmtL(ts.avg_win)}</div>
        </div>
        <div class="ta-box">
          <div class="ta-num l">${fmtL(ts.loss_amt)}</div>
          <div class="ta-lbl">Gross Loss</div>
          <div class="ta-sub m">Avg: ${fmtL(ts.avg_loss)}</div>
        </div>
      </div>
      <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px">
        ${[
          ['Risk:Reward','1 : '+(ts.risk_reward>=200?'∞':ts.risk_reward),''],
          ['Net Inflow', fmtL(ts.inflow), ts.inflow_count+' buys'],
          ['Net Outflow',fmtL(ts.outflow),ts.outflow_count+' sells'],
        ].map(([l,v,s])=>`<div class="ta-box"><div style="font-size:0.9rem;font-weight:700">${v}</div><div class="ta-lbl">${l}</div>${s?`<div class="ta-sub m">${s}</div>`:''}</div>`).join('')}
      </div>`;
  } else {
    document.getElementById('tradePanel').innerHTML = `
      <div style="text-align:center;padding:30px;color:var(--muted);font-size:0.82rem">
        No trade data available for today.<br>
        <span style="font-size:0.72rem;color:var(--muted)">Trade analytics show data from executed trades via Kite API.</span>
      </div>`;
  }
}

// ── GROWTH CHART ───────────────────────────────────────
function renderGrowthChart(d) {
  const p       = d.portfolio;
  const st      = d.settings;
  const history = d.history || [];   // real saved daily data

  let labels=[], valData=[], costData=[], targetData=[], drawdownData=[];

  if(history.length >= 2) {
    // ── USE REAL SAVED DATA ──────────────────────────
    const raw = chartMode === 'monthly'
      ? groupByMonth(history)
      : groupByYear(history);

    labels      = raw.map(r => r.label);
    valData     = raw.map(r => r.value);
    costData    = raw.map(r => r.invested);
    // Target line from first recorded invested value
    const firstCost = raw[0].invested;
    targetData  = raw.map((r, i) => {
      const yrs = chartMode === 'monthly' ? i/12 : i;
      return firstCost * Math.pow(1 + st.cagr_target/100, yrs);
    });
    // Drawdown — % drop from peak
    let peak = 0;
    drawdownData = valData.map(v => {
      peak = Math.max(peak, v);
      return peak > 0 ? -((peak - v) / peak * 100) : 0;
    });

  } else {
    // ── NO HISTORY YET — show today's snapshot only + note ──
    const since = new Date(st.invested_since);
    const now   = new Date();
    if(chartMode === 'monthly') {
      let cur = new Date(since.getFullYear(), since.getMonth(), 1);
      const months = [];
      while(cur <= now){ months.push(new Date(cur)); cur = new Date(cur.getFullYear(), cur.getMonth()+1, 1); }
      const n = months.length;
      labels = months.map(m => m.toLocaleString('en-IN',{month:'short',year:'2-digit'}));
      months.forEach((_,i)=>{
        const progress = i/Math.max(n-1,1);
        const curve    = Math.pow(progress,0.75)*(1+Math.sin(i*1.8)*0.03);
        costData.push(p.total_cost*(0.3+0.7*progress));
        valData.push(Math.max(p.total_cost*(0.3+0.7*progress)+(p.total_pl)*curve, p.total_cost*0.3*progress));
        targetData.push(p.total_cost*Math.pow(1+st.cagr_target/100, i/12));
      });
      valData[n-1]=p.total_value; costData[n-1]=p.total_cost;
    } else {
      const sy = since.getFullYear(), ey = now.getFullYear();
      for(let y=sy;y<=ey;y++) labels.push(y===ey?y+'★':String(y));
      const n = labels.length;
      labels.forEach((_,i)=>{
        const progress = i/Math.max(n-1,1);
        costData.push(p.total_cost*(0.25+0.75*progress));
        valData.push(Math.max(p.total_cost*(0.25+0.75*progress)+(p.total_pl)*Math.pow(progress,0.75), p.total_cost*0.25));
        targetData.push(p.total_cost*Math.pow(1+st.cagr_target/100, i));
      });
      valData[n-1]=p.total_value; costData[n-1]=p.total_cost;
    }
    drawdownData = [];
  }

  const gridClr  = isDark ? 'rgba(255,255,255,0.05)' : 'rgba(0,0,0,0.05)';
  const tickClr  = isDark ? '#7b82a8' : '#9090a0';
  const isReal   = (d.history||[]).length >= 2;
  const ptRadius = (chartMode==='yearly'||isReal) ? 4 : 0;

  // Show notice if using real data
  const notice = document.getElementById('growthNotice');
  if(notice) notice.textContent = isReal
    ? `Real data · ${(d.history||[]).length} daily snapshots saved`
    : 'Estimated · real chart builds from tomorrow';

  const datasets = [
    {label:'Portfolio Value',data:valData,borderColor:'var(--gain)',backgroundColor:'rgba(0,230,118,0.06)',borderWidth:2,fill:true,tension:0.4,pointRadius:ptRadius,pointBackgroundColor:'var(--gain)',pointBorderColor:isDark?'#0f1117':'#f4f6fb',pointBorderWidth:2,pointHoverRadius:5},
    {label:'Invested',data:costData,borderColor:'#6c63ff',backgroundColor:'transparent',borderWidth:1.5,borderDash:[5,4],tension:0.4,pointRadius:0},
    {label:`CAGR Target (${st.cagr_target}%)`,data:targetData,borderColor:'rgba(255,82,82,0.5)',backgroundColor:'transparent',borderWidth:1.5,borderDash:[3,5],tension:0.4,pointRadius:0},
  ];
  if(drawdownData.length) {
    datasets.push({label:'Drawdown %',data:drawdownData,borderColor:'rgba(255,171,64,0.7)',backgroundColor:'transparent',borderWidth:1,borderDash:[2,3],tension:0.4,pointRadius:0,yAxisID:'y2'});
  }

  if(growthChart) growthChart.destroy();
  growthChart = new Chart(document.getElementById('growthChart'),{
    type:'line',
    data:{ labels, datasets },
    options:{
      responsive:true,maintainAspectRatio:true,
      interaction:{mode:'index',intersect:false},
      plugins:{
        legend:{labels:{color:tickClr,font:{family:'Inter',size:11},boxWidth:16,padding:12}},
        tooltip:{backgroundColor:isDark?'#1a1d27':'#ffffff',borderColor:isDark?'#2e3248':'#dde1f0',borderWidth:1,titleColor:tickClr,bodyColor:isDark?'#e8eaf6':'#1a1d2e',callbacks:{label:c=>` ${c.dataset.label}: ${fmtL(c.raw)}`}}
      },
      scales:{
        x:{ticks:{color:tickClr,font:{family:'Inter',size:10},maxTicksLimit:12},grid:{color:gridClr}},
        y:{ticks:{color:tickClr,font:{family:'Inter',size:10},callback:v=>fmtL(v)},grid:{color:gridClr}},
        y2:{position:'right',ticks:{color:'rgba(255,171,64,0.6)',font:{family:'Inter',size:9},callback:v=>v.toFixed(1)+'%'},grid:{drawOnChartArea:false},display:drawdownData.length>0}
      }
    }
  });
}

// ── GROUP HISTORY BY MONTH/YEAR ───────────────────────
function groupByMonth(history) {
  const map = {};
  history.forEach(h => {
    const d = new Date(h.date);
    const k = d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0');
    map[k] = h; // last entry of month wins
  });
  return Object.entries(map).sort().map(([k,v]) => {
    const d = new Date(k+'-01');
    return { label: d.toLocaleString('en-IN',{month:'short',year:'2-digit'}), ...v };
  });
}

function groupByYear(history) {
  const map = {};
  history.forEach(h => {
    const y = h.date.slice(0,4);
    map[y] = h;
  });
  const now = new Date().getFullYear();
  return Object.entries(map).sort().map(([y,v]) => ({
    label: parseInt(y)===now ? y+'★' : y, ...v
  }));
}

function setChartTab(el, mode) {
  document.querySelectorAll('.ctab').forEach(b=>b.classList.remove('active'));
  el.classList.add('active');
  chartMode = mode;
  if(lastData) renderGrowthChart(lastData);
}

// ── INIT ───────────────────────────────────────────────
window.addEventListener('load', () => {
  const params = new URLSearchParams(window.location.search);
  if(params.get('error')){
    document.getElementById('connectError').textContent = 'Login error: '+params.get('error');
    document.getElementById('connectError').style.display = 'block';
    history.replaceState({},'','/');
  }
  loadStockRiskLimits();
  checkAuth();
});
</script>
</body>
</html>"""


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
