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

        max_loss_amt  = total_cost * (settings["max_loss_pct"] / 100)
        actual_loss   = abs(total_pl) if total_pl < 0 else 0
        loss_used_pct = (actual_loss / max_loss_amt * 100) if max_loss_amt > 0 else 0
        stop_investing = actual_loss >= max_loss_amt

        breaching = [h for h in holdings if h["pnl_pct"] < -settings["pos_loss_pct"]]
        sorted_h  = sorted(holdings, key=lambda h: h["pnl_pct"], reverse=True)

        # Funds / cash
        try:
            margins   = cached("margins", kite.margins)
            equity    = margins.get("equity", {})
            cash_avail = equity.get("available", {}).get("cash", 0)
        except:
            cash_avail = 0

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
<title>folio · Live Dashboard</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700;800&family=DM+Mono:wght@400;500&family=Instrument+Sans:wght@300;400;500&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script>
<style>
:root{--bg:#0d0f10;--s1:#141618;--s2:#1c1f21;--s3:#242729;--border:#2a2e30;--text:#e8eaeb;--muted:#6b7275;--accent:#f0b429;--gain:#3ecf8e;--loss:#f87171;--warn:#fb923c;--gain-bg:rgba(62,207,142,0.08);--loss-bg:rgba(248,113,113,0.08);--warn-bg:rgba(251,146,60,0.1);}
*{box-sizing:border-box;margin:0;padding:0;}
body{background:var(--bg);color:var(--text);font-family:'Instrument Sans',sans-serif;font-weight:300;min-height:100vh;}
body::before{content:'';position:fixed;inset:0;background:radial-gradient(ellipse 80% 50% at 50% -20%,rgba(240,180,41,0.04),transparent);pointer-events:none;}
.wrap{max-width:1320px;margin:0 auto;padding:0 24px;}

/* HEADER */
header{display:flex;align-items:center;justify-content:space-between;padding:20px 0 18px;border-bottom:1px solid var(--border);margin-bottom:20px;}
.logo{font-family:'Syne',sans-serif;font-size:1.3rem;font-weight:800;letter-spacing:-1px;}
.logo em{color:var(--accent);font-style:normal;}
.header-right{display:flex;align-items:center;gap:12px;}
.live-dot{width:8px;height:8px;border-radius:50%;background:var(--gain);animation:pulse 2s infinite;}
.live-dot.off{background:var(--muted);animation:none;}
@keyframes pulse{0%,100%{opacity:1;box-shadow:0 0 0 0 rgba(62,207,142,0.4)}50%{opacity:0.8;box-shadow:0 0 0 6px rgba(62,207,142,0)}}
.status-txt{font-family:'DM Mono',monospace;font-size:0.68rem;color:var(--muted);}
.btn{font-family:'Syne',sans-serif;font-weight:700;font-size:0.75rem;border:none;padding:8px 18px;cursor:pointer;transition:all 0.2s;letter-spacing:0.5px;}
.btn-primary{background:var(--accent);color:#0d0f10;}
.btn-primary:hover{background:#f5c842;}
.btn-ghost{background:none;border:1px solid var(--border);color:var(--muted);}
.btn-ghost:hover{border-color:var(--text);color:var(--text);}

/* CONNECT SCREEN */
#connectScreen{display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:70vh;text-align:center;}
#connectScreen h1{font-family:'Syne',sans-serif;font-size:2.8rem;font-weight:800;letter-spacing:-2px;margin-bottom:12px;}
#connectScreen h1 span{color:var(--accent);}
#connectScreen p{color:var(--muted);font-size:0.9rem;line-height:1.7;max-width:440px;margin-bottom:32px;}
.connect-steps{display:flex;flex-direction:column;gap:10px;margin-bottom:32px;text-align:left;max-width:380px;width:100%;}
.step{display:flex;gap:12px;align-items:flex-start;background:var(--s1);border:1px solid var(--border);padding:12px 16px;}
.step-n{width:22px;height:22px;background:var(--accent);color:#0d0f10;font-family:'DM Mono',monospace;font-size:0.7rem;font-weight:500;display:flex;align-items:center;justify-content:center;flex-shrink:0;margin-top:1px;}
.step-t{font-size:0.8rem;color:var(--muted);line-height:1.5;}
.step-t strong{color:var(--text);}
.connect-big-btn{background:var(--accent);color:#0d0f10;border:none;padding:14px 48px;font-family:'Syne',sans-serif;font-weight:800;font-size:1rem;cursor:pointer;letter-spacing:0.5px;transition:all 0.2s;}
.connect-big-btn:hover{background:#f5c842;transform:translateY(-2px);}

/* SETTINGS BAR */
#settingsBar{background:var(--s1);border:1px solid var(--border);padding:14px 20px;margin-bottom:20px;display:flex;gap:20px;align-items:flex-end;flex-wrap:wrap;}
.sg{display:flex;flex-direction:column;gap:4px;}
.sg label{font-family:'DM Mono',monospace;font-size:0.58rem;letter-spacing:1.5px;text-transform:uppercase;color:var(--muted);}
.sg input{background:var(--s2);border:1px solid var(--border);color:var(--text);padding:6px 10px;font-family:'DM Mono',monospace;font-size:0.8rem;width:110px;outline:none;}
.sg input:focus{border-color:var(--accent);}

/* ALERT */
.alert{padding:12px 18px;margin-bottom:16px;display:flex;align-items:center;gap:10px;font-size:0.82rem;border-left:3px solid;display:none;}
.alert.show{display:flex;}
.alert-danger{background:var(--loss-bg);border-color:var(--loss);}
.alert-warn{background:var(--warn-bg);border-color:var(--warn);}
.alert-ok{background:var(--gain-bg);border-color:var(--gain);}

/* CARDS */
.cards{display:grid;grid-template-columns:repeat(5,1fr);gap:1px;background:var(--border);border:1px solid var(--border);margin-bottom:16px;}
.card{background:var(--s1);padding:18px 16px;}
.card-lbl{font-family:'DM Mono',monospace;font-size:0.58rem;letter-spacing:1.5px;text-transform:uppercase;color:var(--muted);margin-bottom:7px;}
.card-val{font-family:'Syne',sans-serif;font-size:1.4rem;font-weight:700;letter-spacing:-1px;line-height:1;}
.card-sub{font-family:'DM Mono',monospace;font-size:0.65rem;margin-top:5px;}
.g{color:var(--gain);}.l{color:var(--loss);}.w{color:var(--warn);}.m{color:var(--muted);}

/* GRID */
.g3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px;margin-bottom:14px;}
.g13{display:grid;grid-template-columns:1fr 320px;gap:14px;margin-bottom:14px;}
.panel{background:var(--s1);border:1px solid var(--border);padding:20px;}
.ph{font-family:'Syne',sans-serif;font-weight:700;font-size:0.75rem;letter-spacing:1px;text-transform:uppercase;color:var(--muted);margin-bottom:14px;display:flex;justify-content:space-between;align-items:center;}
.ph span{font-family:'DM Mono',monospace;font-size:0.65rem;font-weight:400;text-transform:none;letter-spacing:0;}

/* PROGRESS */
.prog{margin-bottom:12px;}
.prog-meta{display:flex;justify-content:space-between;margin-bottom:4px;font-size:0.75rem;}
.prog-track{height:5px;background:var(--s3);border-radius:3px;overflow:hidden;position:relative;}
.prog-fill{height:100%;border-radius:3px;transition:width 0.8s ease;}

/* RISK STATUS */
.risk-big{text-align:center;padding:12px;border:1px solid var(--border);margin-bottom:12px;}
.risk-big .num{font-family:'Syne',sans-serif;font-size:2rem;font-weight:800;letter-spacing:-1px;line-height:1;}
.risk-big .lbl{font-family:'DM Mono',monospace;font-size:0.58rem;color:var(--muted);letter-spacing:1px;margin-top:3px;}

/* STAT ROWS */
.sr{display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid var(--border);}
.sr:last-child{border-bottom:none;}
.sn{font-size:0.76rem;color:var(--muted);}
.sv{font-family:'DM Mono',monospace;font-size:0.8rem;font-weight:500;}

/* TABLE */
table{width:100%;border-collapse:collapse;}
thead th{font-family:'DM Mono',monospace;font-size:0.58rem;letter-spacing:1px;text-transform:uppercase;color:var(--muted);padding:0 8px 8px;text-align:right;border-bottom:1px solid var(--border);}
thead th:first-child{text-align:left;}
tbody td{padding:9px 8px;font-size:0.76rem;text-align:right;border-bottom:1px solid rgba(42,46,48,0.5);}
tbody td:first-child{text-align:left;}
tbody tr:hover td{background:var(--s2);}
.tk{font-family:'Syne',sans-serif;font-weight:700;font-size:0.8rem;}
.ri{display:inline-block;font-family:'DM Mono',monospace;font-size:0.6rem;padding:2px 6px;}
.ri-ok{background:var(--gain-bg);color:var(--gain);}
.ri-warn{background:var(--warn-bg);color:var(--warn);}
.ri-bad{background:var(--loss-bg);color:var(--loss);}

/* WIN/LOSS */
.wl2{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:12px;}
.wl-box{text-align:center;padding:10px;background:var(--s2);border:1px solid var(--border);}
.wl-num{font-family:'Syne',sans-serif;font-size:1.6rem;font-weight:800;letter-spacing:-1px;}
.wl-lbl{font-family:'DM Mono',monospace;font-size:0.55rem;color:var(--muted);letter-spacing:1px;margin-top:2px;}
.wl-sub{font-family:'DM Mono',monospace;font-size:0.58rem;margin-top:3px;}

/* FUND FLOW BAR */
.flow-bar{display:flex;height:28px;border-radius:2px;overflow:hidden;margin:10px 0;}
.fi{background:var(--gain);display:flex;align-items:center;justify-content:center;font-family:'DM Mono',monospace;font-size:0.62rem;color:#0d0f10;font-weight:500;}
.fo{background:var(--loss);display:flex;align-items:center;justify-content:center;font-family:'DM Mono',monospace;font-size:0.62rem;color:#0d0f10;font-weight:500;}

/* REFRESH SPINNER */
.spin{display:inline-block;width:10px;height:10px;border:1.5px solid var(--muted);border-top-color:var(--accent);border-radius:50%;animation:spin 0.8s linear infinite;margin-left:6px;vertical-align:middle;}
@keyframes spin{to{transform:rotate(360deg)}}

footer{border-top:1px solid var(--border);padding:16px 0;display:flex;justify-content:space-between;align-items:center;margin-top:8px;}
footer p{font-family:'DM Mono',monospace;font-size:0.65rem;color:var(--muted);}

.fade-in{animation:fi 0.4s ease forwards;}
@keyframes fi{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:translateY(0)}}
@media(max-width:900px){.cards{grid-template-columns:repeat(3,1fr);}.g3,.g13{grid-template-columns:1fr;}}
</style>
</head>
<body>
<div class="wrap">
<header>
  <div class="logo">folio<em>.</em>live</div>
  <div class="header-right">
    <div class="live-dot off" id="liveDot"></div>
    <span class="status-txt" id="statusTxt">Not connected</span>
    <button class="btn btn-ghost" id="refreshBtn" onclick="forceRefresh()" style="display:none">↻ Refresh</button>
    <button class="btn btn-ghost" id="logoutBtn" onclick="logout()" style="display:none">Disconnect</button>
  </div>
</header>

<!-- CONNECT SCREEN -->
<div id="connectScreen">
  <h1>Your portfolio,<br><span>live.</span></h1>
  <p>Connect your Zerodha account once. Your holdings, P&L, risk metrics, and fund flow update automatically — no CSV uploads.</p>
  <div class="connect-steps">
    <div class="step"><div class="step-n">1</div><div class="step-t">Make sure <strong>server.py is running</strong> on your machine (python server.py)</div></div>
    <div class="step"><div class="step-n">2</div><div class="step-t">Click below — you'll be redirected to <strong>Zerodha's login page</strong></div></div>
    <div class="step"><div class="step-n">3</div><div class="step-t">Log in once. Your session lasts until <strong>midnight</strong> (Zerodha's rule)</div></div>
  </div>
  <button class="connect-big-btn" onclick="connectZerodha()">Connect Zerodha →</button>
  <div id="connectError" style="margin-top:16px;font-family:'DM Mono',monospace;font-size:0.72rem;color:var(--loss);display:none"></div>
</div>

<!-- DASHBOARD -->
<div id="dashboard" style="display:none">

  <!-- SETTINGS -->
  <div id="settingsBar">
    <div class="sg"><label>CAGR Target %</label><input type="number" id="cagrTarget" value="15" min="1" max="100" onchange="loadData()"></div>
    <div class="sg"><label>Max Portfolio Loss %</label><input type="number" id="maxLoss" value="10" min="1" max="50" onchange="loadData()"></div>
    <div class="sg"><label>Max Position Loss %</label><input type="number" id="posLoss" value="10" min="1" max="50" onchange="loadData()"></div>
    <div class="sg"><label>Invested Since</label><input type="date" id="investedSince" value="2023-01-01" onchange="loadData()"></div>
    <div style="font-family:'DM Mono',monospace;font-size:0.65rem;color:var(--muted);margin-left:auto;align-self:center" id="lastUpdated"></div>
  </div>

  <!-- ALERT -->
  <div class="alert" id="alertBanner"></div>

  <!-- CARDS -->
  <div class="cards" id="cards"></div>

  <!-- ROW 1 -->
  <div class="g3">
    <!-- CAGR -->
    <div class="panel">
      <div class="ph">CAGR Progress <span id="cagrPeriodLbl"></span></div>
      <div class="risk-big" id="cagrBig"></div>
      <div class="prog" id="cagrProg"></div>
      <div id="cagrStats"></div>
    </div>
    <!-- RISK -->
    <div class="panel">
      <div class="ph">Risk Meter</div>
      <div class="risk-big" id="riskBig"></div>
      <div class="prog" id="riskProg"></div>
      <div id="riskStats"></div>
    </div>
    <!-- FUND FLOW -->
    <div class="panel">
      <div class="ph">Fund Flow <span id="flowNote"></span></div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:10px">
        <div style="background:var(--gain-bg);border:1px solid rgba(62,207,142,0.2);padding:10px">
          <div style="font-family:'DM Mono',monospace;font-size:0.55rem;letter-spacing:1.5px;text-transform:uppercase;color:var(--gain);margin-bottom:3px">↓ Inflow</div>
          <div style="font-family:'Syne',sans-serif;font-size:1.1rem;font-weight:700;color:var(--gain)" id="flowIn">—</div>
          <div style="font-family:'DM Mono',monospace;font-size:0.62rem;color:var(--muted);margin-top:2px" id="flowInC">—</div>
        </div>
        <div style="background:var(--loss-bg);border:1px solid rgba(248,113,113,0.2);padding:10px">
          <div style="font-family:'DM Mono',monospace;font-size:0.55rem;letter-spacing:1.5px;text-transform:uppercase;color:var(--loss);margin-bottom:3px">↑ Outflow</div>
          <div style="font-family:'Syne',sans-serif;font-size:1.1rem;font-weight:700;color:var(--loss)" id="flowOut">—</div>
          <div style="font-family:'DM Mono',monospace;font-size:0.62rem;color:var(--muted);margin-top:2px" id="flowOutC">—</div>
        </div>
      </div>
      <div class="flow-bar" id="flowBar"></div>
      <div id="flowStats"></div>
    </div>
  </div>

  <!-- ROW 2: TABLE + WIN/LOSS -->
  <div class="g13">
    <div class="panel">
      <div class="ph">Holdings <span id="holdCount"></span></div>
      <div style="overflow-x:auto"><table>
        <thead><tr>
          <th>Stock</th><th>Qty</th><th>Avg</th><th>LTP</th><th>Value</th><th>P&L ₹</th><th>P&L %</th><th>Wt</th><th>Risk</th>
        </tr></thead>
        <tbody id="holdTbody"></tbody>
      </table></div>
    </div>
    <div class="panel">
      <div class="ph">Trade Analytics</div>
      <div id="wlPanel"><div style="color:var(--muted);font-size:0.78rem;text-align:center;padding:20px">No trade data available</div></div>
    </div>
  </div>

  <!-- COMPOSITION CHART -->
  <div class="panel" style="margin-bottom:16px">
    <div class="ph">Portfolio Composition</div>
    <canvas id="compChart" height="70"></canvas>
  </div>

</div>

<footer>
  <p>Live data via Kite Connect API · Refreshes every 60s · <span id="footerUser"></span></p>
  <p>All data processed locally · Token expires at midnight</p>
</footer>
</div>

<script>
const BASE = '';  // same origin
let compChart = null;
let autoRefreshTimer = null;

// ── FORMAT ──────────────────────────────────────────────
const fmtL = n => {
  const a = Math.abs(n);
  if(a>=10000000) return (n<0?'-':'')+(Math.abs(n)/10000000).toFixed(2)+'Cr';
  if(a>=100000)   return (n<0?'-':'')+(Math.abs(n)/100000).toFixed(2)+'L';
  if(a>=1000)     return (n<0?'-':'')+(Math.abs(n)/1000).toFixed(1)+'K';
  return '₹'+Math.abs(n).toFixed(0);
};
const pct = (n,d=1) => (n>=0?'+':'')+n.toFixed(d)+'%';
const gc  = n => n>=0?'g':'l';

// ── AUTH ────────────────────────────────────────────────
async function checkAuth() {
  try {
    const r = await fetch(BASE + '/auth/status');
    const d = await r.json();
    if (d.connected) {
      showDashboard(d.user_name);
      loadData();
      startAutoRefresh();
    } else {
      showConnect();
    }
  } catch(e) {
    showConnect();
    document.getElementById('connectError').textContent = 'Cannot reach server. Is server.py running?';
    document.getElementById('connectError').style.display = 'block';
  }
}

function connectZerodha() {
  window.location.href = BASE + '/auth/login';
}

async function logout() {
  await fetch(BASE + '/auth/logout');
  stopAutoRefresh();
  showConnect();
}

function showConnect() {
  document.getElementById('connectScreen').style.display = 'flex';
  document.getElementById('dashboard').style.display     = 'none';
  document.getElementById('liveDot').classList.add('off');
  document.getElementById('statusTxt').textContent = 'Not connected';
  document.getElementById('refreshBtn').style.display = 'none';
  document.getElementById('logoutBtn').style.display  = 'none';
}

function showDashboard(userName) {
  document.getElementById('connectScreen').style.display = 'none';
  document.getElementById('dashboard').style.display     = 'block';
  document.getElementById('liveDot').classList.remove('off');
  document.getElementById('statusTxt').textContent = userName ? `${userName} · Live` : 'Live';
  document.getElementById('refreshBtn').style.display = 'inline-block';
  document.getElementById('logoutBtn').style.display  = 'inline-block';
  document.getElementById('footerUser').textContent   = userName || '';
}

// ── DATA LOADING ────────────────────────────────────────
async function loadData() {
  const params = new URLSearchParams({
    cagr_target:    document.getElementById('cagrTarget').value,
    max_loss_pct:   document.getElementById('maxLoss').value,
    pos_loss_pct:   document.getElementById('posLoss').value,
    invested_since: document.getElementById('investedSince').value,
  });

  document.getElementById('statusTxt').innerHTML =
    (document.getElementById('statusTxt').textContent.split('·')[0] || 'Live') +
    '· <span class="spin"></span>';

  try {
    const r = await fetch(`${BASE}/api/summary?${params}`);
    const d = await r.json();
    if (d.error) { alert('Error: ' + d.error); return; }
    renderAll(d);
    document.getElementById('lastUpdated').textContent =
      'Updated ' + new Date().toLocaleTimeString('en-IN');
    document.getElementById('statusTxt').textContent =
      (d.user_name || '') + ' · Live';
  } catch(e) {
    document.getElementById('statusTxt').textContent = 'Connection error';
  }
}

function startAutoRefresh() {
  stopAutoRefresh();
  autoRefreshTimer = setInterval(loadData, 60000); // every 60s
}

function stopAutoRefresh() {
  if (autoRefreshTimer) { clearInterval(autoRefreshTimer); autoRefreshTimer = null; }
}

async function forceRefresh() {
  await fetch(BASE + '/api/refresh');
  loadData();
}

// ── RENDER ──────────────────────────────────────────────
function renderAll(d) {
  const p  = d.portfolio;
  const rs = d.risk;
  const st = d.settings;
  const ts = d.trade_stats;

  // ── ALERT BANNER
  const banner = document.getElementById('alertBanner');
  if (rs.stop_investing) {
    banner.className = 'alert alert-danger show';
    banner.innerHTML = `🚨 <strong>STOP INVESTING.</strong> Portfolio loss of ${fmtL(rs.actual_loss)} exceeds your ${st.max_loss_pct}% limit (${fmtL(rs.max_loss_amt)}). Wait for conditions to improve.`;
  } else if (rs.loss_used_pct > 70) {
    banner.className = 'alert alert-warn show';
    banner.innerHTML = `⚠️ <strong>Caution.</strong> ${rs.loss_used_pct.toFixed(0)}% of loss budget used. Only ${fmtL(rs.loss_remaining)} remaining before investing pause.`;
  } else {
    banner.className = 'alert alert-ok show';
    banner.innerHTML = `✅ <strong>All clear.</strong> Portfolio within risk limits. Cash available: ${fmtL(p.cash_available)}.`;
  }

  // ── CARDS
  document.getElementById('cards').innerHTML = [
    ['Live Value',     fmtL(p.total_value),   null,                  null],
    ['Invested',       fmtL(p.total_cost),    p.holdings_count+' stocks', null],
    ['Unrealised P&L', (p.total_pl>=0?'+':'-')+fmtL(Math.abs(p.total_pl)), pct(p.total_pl_pct), gc(p.total_pl)],
    ['CAGR',           p.cagr+'%',            'Target: '+st.cagr_target+'%', p.cagr>=st.cagr_target?'g':'w'],
    ['Risk Used',      rs.loss_used_pct.toFixed(0)+'%', 'of '+st.max_loss_pct+'% limit', rs.loss_used_pct>80?'l':rs.loss_used_pct>50?'w':'g'],
  ].map(([l,v,s,c])=>`<div class="card"><div class="card-lbl">${l}</div><div class="card-val ${c||''}">${v}</div>${s?`<div class="card-sub ${c||'m'}">${s}</div>`:''}</div>`).join('');

  // ── CAGR PANEL — clean card style
  const cagrMet = p.cagr >= st.cagr_target;
  const cagrPct = Math.min(p.cagr / st.cagr_target * 100, 100);
  const cagrColor = cagrMet ? 'var(--gain)' : 'var(--warn)';
  document.getElementById('cagrBig').innerHTML = '';
  document.getElementById('cagrProg').innerHTML = '';
  document.getElementById('cagrStats').innerHTML = `
    <div style="text-align:center;padding:20px 0 16px">
      <div style="font-size:0.72rem;color:var(--muted);margin-bottom:6px;letter-spacing:1px">YOUR CAGR</div>
      <div style="font-size:3rem;font-weight:800;color:${cagrColor};letter-spacing:-2px;line-height:1">${p.cagr}%</div>
      <div style="font-size:0.75rem;color:var(--muted);margin-top:6px">Target: <strong style="color:var(--text)">${st.cagr_target}%</strong></div>
    </div>
    <div style="background:var(--s3);border-radius:4px;height:8px;margin:0 0 16px;overflow:hidden">
      <div style="height:100%;width:${cagrPct}%;background:${cagrColor};border-radius:4px;transition:width 1s ease"></div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
      <div style="background:var(--s2);padding:12px;border-radius:4px;text-align:center">
        <div style="font-size:0.62rem;color:var(--muted);margin-bottom:4px">RETURN</div>
        <div style="font-size:1.1rem;font-weight:700;color:${gc(p.total_pl_pct)==='g'?'var(--gain)':'var(--loss)'}">${pct(p.total_pl_pct)}</div>
      </div>
      <div style="background:var(--s2);padding:12px;border-radius:4px;text-align:center;border:1px solid ${cagrColor}40">
        <div style="font-size:0.62rem;color:var(--muted);margin-bottom:4px">STATUS</div>
        <div style="font-size:0.85rem;font-weight:700;color:${cagrColor}">${cagrMet ? '✓ On Track' : (st.cagr_target - p.cagr).toFixed(1)+'% gap'}</div>
      </div>
    </div>`;

  // ── RISK PANEL — clean card style
  const rCls = rs.stop_investing?'l':rs.loss_used_pct>70?'w':'g';
  const rColor = rs.stop_investing?'var(--loss)':rs.loss_used_pct>70?'var(--warn)':'var(--gain)';
  const rIcon = rs.stop_investing ? '🚨' : rs.loss_used_pct>70 ? '⚠️' : '✅';
  const rMsg = rs.stop_investing ? 'Stop Investing' : rs.loss_used_pct>70 ? 'Caution' : 'Safe';
  document.getElementById('riskBig').innerHTML = '';
  document.getElementById('riskProg').innerHTML = '';
  document.getElementById('riskStats').innerHTML = `
    <div style="text-align:center;padding:20px 0 16px">
      <div style="font-size:2rem;margin-bottom:6px">${rIcon}</div>
      <div style="font-size:1.6rem;font-weight:800;color:${rColor};letter-spacing:-1px;line-height:1">${rMsg}</div>
      <div style="font-size:0.72rem;color:var(--muted);margin-top:6px">${rs.loss_used_pct.toFixed(0)}% of ${st.max_loss_pct}% limit used</div>
    </div>
    <div style="background:var(--s3);border-radius:4px;height:8px;margin:0 0 16px;overflow:hidden">
      <div style="height:100%;width:${Math.min(rs.loss_used_pct,100)}%;background:${rColor};border-radius:4px;transition:width 1s ease"></div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
      <div style="background:var(--s2);padding:12px;border-radius:4px;text-align:center">
        <div style="font-size:0.62rem;color:var(--muted);margin-bottom:4px">MAX LOSS</div>
        <div style="font-size:1rem;font-weight:700;color:var(--loss)">${fmtL(rs.max_loss_amt)}</div>
      </div>
      <div style="background:var(--s2);padding:12px;border-radius:4px;text-align:center">
        <div style="font-size:0.62rem;color:var(--muted);margin-bottom:4px">BUFFER LEFT</div>
        <div style="font-size:1rem;font-weight:700;color:${rColor}">${fmtL(rs.loss_remaining)}</div>
      </div>
    </div>
    ${rs.breaching_count>0 ? `<div style="margin-top:10px;padding:8px 12px;background:var(--loss-bg);border-left:3px solid var(--loss);font-size:0.75rem;color:var(--loss)">${rs.breaching_count} position${rs.breaching_count>1?'s':''} breaching ${st.pos_loss_pct}% loss limit</div>` : ''}`;

  // ── FUND FLOW
  if (ts) {
    const inPct = (ts.inflow+ts.outflow)>0 ? ts.inflow/(ts.inflow+ts.outflow)*100 : 70;
    document.getElementById('flowIn').textContent  = fmtL(ts.inflow);
    document.getElementById('flowInC').textContent = ts.inflow_count + ' buy trades';
    document.getElementById('flowOut').textContent = fmtL(ts.outflow);
    document.getElementById('flowOutC').textContent= ts.outflow_count + ' sell trades';
    document.getElementById('flowBar').innerHTML   = `<div class="fi" style="width:${inPct}%">${inPct>25?'In':''}</div><div class="fo" style="width:${100-inPct}%">${(100-inPct)>15?'Out':''}</div>`;
    document.getElementById('flowStats').innerHTML = [
      ['Net Deployed', `<span class="${gc(ts.net_deployed)}">${fmtL(ts.net_deployed)}</span>`],
      ['Portfolio Value', fmtL(p.total_value)],
      ['Overall P&L', `<span class="${gc(p.total_pl)}">${p.total_pl>=0?'+':'-'}${fmtL(Math.abs(p.total_pl))}</span>`],
    ].map(([n,v])=>`<div class="sr"><span class="sn">${n}</span><span class="sv">${v}</span></div>`).join('');
    document.getElementById('flowNote').textContent = 'from trade history';
  } else {
    document.getElementById('flowIn').textContent  = fmtL(p.total_cost);
    document.getElementById('flowInC').textContent = 'estimated from holdings';
    document.getElementById('flowOut').textContent = '—';
    document.getElementById('flowOutC').textContent= 'no trade data';
    document.getElementById('flowNote').textContent = 'estimated';
  }

  // ── HOLDINGS TABLE
  document.getElementById('holdCount').textContent = d.holdings.length + ' stocks';
  const posLimit = st.pos_loss_pct;
  document.getElementById('holdTbody').innerHTML = d.holdings.map(h => {
    const cls = h.pnl_pct < -posLimit ? 'ri-bad' : h.pnl_pct < -(posLimit*0.7) ? 'ri-warn' : 'ri-ok';
    const lbl = h.pnl_pct < -posLimit ? '🔴 BREACH' : h.pnl_pct < -(posLimit*0.7) ? '🟡 WATCH' : '🟢 OK';
    return `<tr>
      <td><span class="tk">${h.tradingsymbol}</span></td>
      <td>${h.quantity}</td>
      <td>₹${h.average_price?.toLocaleString('en-IN',{maximumFractionDigits:0})}</td>
      <td>₹${h.last_price?.toLocaleString('en-IN',{maximumFractionDigits:0})}</td>
      <td>₹${Math.round(h.current_value).toLocaleString('en-IN')}</td>
      <td class="${gc(h.pnl)}">${h.pnl>=0?'+':'-'}₹${Math.abs(Math.round(h.pnl)).toLocaleString('en-IN')}</td>
      <td class="${gc(h.pnl_pct)}">${pct(h.pnl_pct)}</td>
      <td class="m">${h.weight_pct}%</td>
      <td><span class="ri ${cls}">${lbl}</span></td>
    </tr>`;
  }).join('');

  // ── WIN/LOSS + PROFIT FACTOR
  if (ts) {
    const pfColor = ts.profit_factor>=2?'var(--gain)':ts.profit_factor>=1?'var(--warn)':'var(--loss)';
    const pfLabel = ts.profit_factor>=200?'∞':ts.profit_factor>=2?'EXCELLENT':ts.profit_factor>=1.5?'GOOD':ts.profit_factor>=1?'MARGINAL':'LOSING';
    const pfVal   = ts.profit_factor>=200?'∞':ts.profit_factor.toFixed(2);
    document.getElementById('wlPanel').innerHTML = `
      <div class="wl2">
        <div class="wl-box">
          <div class="wl-num" style="color:${ts.win_rate>=50?'var(--gain)':'var(--loss)'}">${ts.win_rate}%</div>
          <div class="wl-lbl">WIN RATE</div>
          <div class="wl-sub m">${ts.wins}W / ${ts.losses}L</div>
        </div>
        <div class="wl-box" style="border-color:${pfColor}">
          <div class="wl-num" style="color:${pfColor}">${pfVal}</div>
          <div class="wl-lbl">PROFIT FACTOR</div>
          <div class="wl-sub" style="color:${pfColor}">${pfLabel}</div>
        </div>
      </div>
      ${[
        ['Gross Profit', `<span class="g">+${fmtL(ts.win_amt)}</span>`],
        ['Gross Loss',   `<span class="l">−${fmtL(ts.loss_amt)}</span>`],
        ['Avg Win',      `<span class="g">+${fmtL(ts.avg_win)}</span>`],
        ['Avg Loss',     `<span class="l">−${fmtL(ts.avg_loss)}</span>`],
        ['Risk:Reward',  `1 : ${ts.risk_reward>=200?'∞':ts.risk_reward}`],
        ['Total Realised',`<span class="${gc(ts.win_amt-ts.loss_amt)}">${fmtL(ts.win_amt-ts.loss_amt)}</span>`],
      ].map(([n,v])=>`<div class="sr"><span class="sn">${n}</span><span class="sv">${v}</span></div>`).join('')}`;
  }

  // ── COMPOSITION CHART
  const sorted = [...d.holdings].sort((a,b)=>b.current_value-a.current_value);
  const colors = ['#f0b429','#3ecf8e','#60a5fa','#f87171','#a78bfa','#fb923c','#34d399','#f472b6','#94a3b8','#fbbf24'];
  if (compChart) compChart.destroy();
  compChart = new Chart(document.getElementById('compChart'), {
    type: 'bar',
    data: {
      labels: sorted.map(h=>h.tradingsymbol),
      datasets: [
        { label:'Value', data:sorted.map(h=>h.current_value), backgroundColor:sorted.map((_,i)=>colors[i%colors.length]), borderRadius:2 },
        { label:'Cost',  data:sorted.map(h=>h.invested_value), backgroundColor:'rgba(255,255,255,0.05)', borderRadius:2 },
      ]
    },
    options:{
      responsive:true,
      plugins:{legend:{labels:{color:'#6b7275',font:{family:'DM Mono',size:11}}},tooltip:{backgroundColor:'#1c1f21',titleColor:'#6b7275',bodyColor:'#e8eaeb',callbacks:{label:c=>` ${c.dataset.label}: ${fmtL(c.raw)}`}}},
      scales:{x:{ticks:{color:'#6b7275',font:{family:'DM Mono',size:11}},grid:{color:'rgba(255,255,255,0.03)'}},y:{ticks:{color:'#6b7275',font:{family:'DM Mono',size:11},callback:v=>fmtL(v)},grid:{color:'rgba(255,255,255,0.03)'}}}
    }
  });
}

// Check on load + handle redirect params
window.addEventListener('load', () => {
  const params = new URLSearchParams(window.location.search);
  if (params.get('error')) {
    document.getElementById('connectError').textContent = 'Login failed: ' + params.get('error');
    document.getElementById('connectError').style.display = 'block';
    history.replaceState({}, '', '/');
  }
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
