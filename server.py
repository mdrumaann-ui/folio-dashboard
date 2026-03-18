import os, json, math, time, threading, urllib.request, urllib.error, re
from datetime import datetime, date
from flask import Flask, redirect, request, jsonify, Response
from flask_cors import CORS

# KiteConnect
try:
    from kiteconnect import KiteConnect
    KITE_AVAILABLE = True
except ImportError:
    KITE_AVAILABLE = False
    print("kiteconnect not installed. Run: pip install kiteconnect flask flask-cors")

app = Flask(__name__)
CORS(app)

# CONFIG - Edit these or use environment variables
API_KEY = os.getenv("KITE_API_KEY", "YOUR_API_KEY_HERE")
API_SECRET = os.getenv("KITE_API_SECRET", "YOUR_API_SECRET_HERE")
TITLE = "folio.live"
CAGR_TARGET = float(os.getenv("CAGR_TARGET", 15))
MAX_LOSS_PCT = float(os.getenv("MAX_LOSS_PCT", 10))
POS_LOSS_PCT = float(os.getenv("POS_LOSS_PCT", 10))
INVESTED_SINCE = os.getenv("INVESTED_SINCE", "2023-01-01")

print("="*55)
print("folio.live - Real-Time Portfolio Dashboard (BLUR TOGGLE)")
print("="*55)

# Session
session = {"access_token": None, "connected_at": None, "username": None, "cache": {}, "cache_ts": {}, "CACHETTL": 60}
kite = KiteConnect(api_key=API_KEY) if KITE_AVAILABLE else None

# JSONBin
JSONBIN_BIN_ID = os.getenv("JSONBIN_BIN_ID")
JSONBIN_API_KEY = os.getenv("JSONBIN_API_KEY")
JSONBIN_BASE = f"https://api.jsonbin.io/v3/b/{JSONBIN_BIN_ID}" if JSONBIN_BIN_ID else None

def load_history():
    if not JSONBIN_BIN_ID or not JSONBIN_API_KEY:
        try:
            if os.path.exists("portfolio_history.json"):
                with open("portfolio_history.json") as f:
                    return json.load(f)
        except: pass
        return {}
    try:
        req = urllib.request.Request(f"{JSONBIN_BASE}/latest", headers={"X-Master-Key": JSONBIN_API_KEY, "X-Bin-Meta": "false"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode())
    except: return {}

def save_history(history):
    if JSONBIN_BIN_ID and JSONBIN_API_KEY:
        data = json.dumps(history).encode()
        req = urllib.request.Request(JSONBIN_BASE, data=data, method="PUT", headers={"Content-Type": "application/json", "X-Master-Key": JSONBIN_API_KEY})
        urllib.request.urlopen(req, timeout=5)
    else:
        with open("portfolio_history.json", "w") as f:
            json.dump(history, f, indent=2)

# Auth routes (unchanged)
@app.route('/auth/login')
def auth_login():
    if not KITE_AVAILABLE: return "kiteconnect not installed", 500
    return redirect(kite.login_url())

@app.route('/auth/callback')
def auth_callback():
    request_token = request.args.get("request_token")
    if request.args.get("status") != "success" or not request_token: return redirect("?error=login_failed")
    try:
        data = kite.generate_session(request_token, api_secret=API_SECRET)
        session["access_token"] = data["access_token"]
        session["connected_at"] = datetime.now().isoformat()
        kite.set_access_token(data["access_token"])
        profile = kite.profile()
        session["username"] = profile.get("user_name")
        session["cache"] = {}
        return redirect("?connected=1")
    except Exception as e: return redirect(f"?error={str(e)}")

@app.route('/auth/status')
def auth_status(): return jsonify({"connected": bool(session.get("access_token")), "connected_at": session["connected_at"], "username": session["username"]})

@app.route('/auth/logout')
def auth_logout():
    session["access_token"] = session["connected_at"] = session["username"] = None
    session["cache"] = {}
    return jsonify({"ok": True})

def require_auth():
    if not KITE_AVAILABLE: return jsonify({"error": "kiteconnect not installed"}), 503
    token = session.get("access_token")
    if not token: return jsonify({"error": "Not connected", "login_url": "/auth/login"}), 401
    kite.set_access_token(token)
    return None

def cached(key, fn, ttl=60):
    now = time.time()
    if key in session["cache"] and now - session["cache_ts"].get(key, 0) < ttl:
        return session["cache"][key]
    result = fn()
    session["cache"][key] = result
    session["cache_ts"][key] = now
    return result

# BLUR STATE ENDPOINT - NEW!
@app.route('/api/blur-state', methods=['GET', 'POST'])
def blur_state():
    err = require_auth()
    if err: return err
    try:
        if request.method == 'POST':
            state = request.get_json()
            history = load_history()
            history['BLUR_STATE'] = state
            save_history(history)
            return jsonify({"ok": True})
        history = load_history()
        return jsonify(history.get('BLUR_STATE', {
            'cards': {'liveValue': True, 'totalCapital': True, 'cashAvailable': True},
            'holdings': {'invested': True, 'current': False, 'pnl': True}
        }))
    except: return jsonify({"cards": {"liveValue": True, "totalCapital": True, "cashAvailable": True}, "holdings": {"invested": True, "current": False, "pnl": True}}), 200

def enrich_holdings(raw):
    enriched = []
    for h in raw:
        qty, avg, ltp = h.get("quantity", 0), h.get("average_price", 0), h.get("last_price", 0)
        val, cost = qty * ltp, qty * avg
        pl, pl_pct = val - cost, pl / cost * 100 if cost else 0
        enriched.append({**h, "current_value": round(val, 2), "invested_value": round(cost, 2), "pnl": round(pl, 2), "pnl_pct": round(pl_pct, 2), "weight_pct": 0})
    total_val = sum(h["current_value"] for h in enriched)
    for h in enriched: h["weight_pct"] = round(h["current_value"] / total_val * 100 if total_val else 0, 2)
    return enriched, total_val

def calc_cagr(cost, value, since_str):
    try:
        years = (datetime.now() - datetime.strptime(since_str, "%Y-%m-%d")).days / 365.25
        return round(((value / cost) ** (1 / years) - 1) * 100, 2) if years > 0.01 and cost else 0
    except: return 0

def load_stock_risks(): return load_history().get("STOCKRISKKEY", {})

@app.route('/api/summary')
def api_summary():
    err = require_auth()
    if err: return err
    try:
        settings = {"cagr_target": float(request.args.get("cagr_target", CAGR_TARGET)), "max_loss_pct": float(request.args.get("max_loss_pct", MAX_LOSS_PCT)), "pos_loss_pct": float(request.args.get("pos_loss_pct", POS_LOSS_PCT)), "invested_since": request.args.get("invested_since", INVESTEDSINCE)}
        raw_holdings = cached("holdings", kite.holdings)
        holdings, total_val = enrich_holdings(raw_holdings)
        total_cost = sum(h["invested_value"] for h in holdings)
        total_pl, total_pl_pct = total_val - total_cost, total_pl / total_cost * 100 if total_cost else 0
        cagr = calc_cagr(total_cost, total_val, settings["invested_since"])
        
        stock_risks = load_stock_risks()
        breaching = [h for h in holdings if h["pnl_pct"] < -stock_risks.get(h["tradingsymbol"], settings["pos_loss_pct"])]
        sorted_holdings = sorted(holdings, key=lambda h: h["pnl_pct"], reverse=True)
        
        try: cash_avail = cached("margins", kite.margins)["equity"]["available"]["cash"]
        except: cash_avail = 0
        
        total_capital = total_val + cash_avail
        max_loss_amt = total_capital * settings["max_loss_pct"] / 100
        actual_loss = abs(total_pl) if total_pl < 0 else 0
        loss_used_pct = actual_loss / max_loss_amt * 100 if max_loss_amt else 0
        
        save_history({**load_history(), date.today().isoformat(): {"value": total_val, "invested": total_cost, "cash": cash_avail}})
        
        return jsonify({
            "timestamp": datetime.now().isoformat(), "username": session["username"], "settings": settings,
            "portfolio": {"total_value": round(total_val, 2), "total_cost": round(total_cost, 2), "total_pl": round(total_pl, 2), "total_pl_pct": round(total_pl_pct, 2), "cagr": cagr, "holdings_count": len(holdings), "cash_available": round(cash_avail, 2), "total_capital": round(total_capital, 2)},
            "risk": {"max_loss_amt": round(max_loss_amt, 2), "actual_loss": round(actual_loss, 2), "loss_used_pct": round(loss_used_pct, 2), "breaching_count": len(breaching)},
            "holdings": sorted_holdings
        })
    except Exception as e:
        import traceback
        print("ERROR:", traceback.format_exc())
        return jsonify({"error": str(e)}), 500

@app.route('/api/holdings')
def api_holdings():
    err = require_auth()
    if err: return err
    try: return jsonify(enrich_holdings(cached("holdings", kite.holdings))[0])
    except: return jsonify({"error": "Holdings fetch failed"}), 500

# Serve dashboard with BLUR FEATURES
DASHBOARD_HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>folio.live</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script>
<style>
:root{--bg:#0f1117;--s1:#1a1d27;--s2:#22263a;--s3:#2a2f45;--border:#2e3248;--text:#e8eaf6;--muted:#7b82a8;--accent:#6c63ff;--gain:#00e676;--loss:#ff5252;--warn:#ffab40;--orange:#ff7043;--gain-bg:rgba(0,230,118,0.07);--loss-bg:rgba(255,82,82,0.07);--warn-bg:rgba(255,171,64,0.08);--shadow:0 2px 12px rgba(0,0,0,0.3)}
.light{--bg:#f4f6fb;--s1:#fff;--s2:#f0f2f9;--s3:#e4e8f5;--border:#dde1f0;--text:#1a1d2e;--gain:#00a152;--loss:#d32f2f;--warn:#e65100;--orange:#bf360c;--gain-bg:rgba(0,161,82,0.07);--loss-bg:rgba(211,47,47,0.07);--shadow:0 2px 12px rgba(0,0,0,0.06)}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}body{background:var(--bg);color:var(--text);font-family:Inter,sans-serif;font-size:13px;min-height:100vh;overflow-x:hidden}
.blur{filter:blur(4px)!important;transition:filter 0.3s ease}.blur:hover{filter:blur(2px)!important}.toggle-eye{font-size:1rem;color:var(--muted);cursor:pointer;padding:2px;transition:color 0.2s;position:absolute;right:8px;top:32px}.toggle-eye:hover{color:var(--accent)!important}
.card{position:relative}.cards-row{display:grid;grid-template-columns:repeat(6,1fr);gap:10px;margin-bottom:14px}.card{background:var(--s1);border:1px solid var(--border);border-radius:8px;padding:13px 12px;box-shadow:var(--shadow)}.card-lbl{font-size:0.58rem;font-weight:600;letter-spacing:1px;text-transform:uppercase;color:var(--muted);margin-bottom:6px}.card-val{font-size:1.15rem;font-weight:700;letter-spacing:-0.5px;line-height:1}.card-sub{font-size:0.63rem;margin-top:4px;color:var(--muted)}
.nav-tabs{background:var(--s1);border-bottom:1px solid var(--border);padding:0 16px;display:flex;gap:2px;overflow-x:auto;position:sticky;top:80px;z-index:99}.nav-tab{font-size:0.72rem;font-weight:500;padding:10px 14px;border:none;background:none;color:var(--muted);cursor:pointer;border-bottom:2px solid transparent;white-space:nowrap;transition:all 0.15s}.nav-tab.active{color:var(--accent);border-bottom-color:var(--accent)}
#dashboard{display:none}.page{display:none;padding:14px 16px 24px}.page.active{display:block}
.tbl-wrap{overflow-x:auto}table{width:100%;border-collapse:collapse}thead th{font-size:0.58rem;font-weight:600;letter-spacing:1px;text-transform:uppercase;color:var(--muted);padding:0 8px 8px;text-align:right;border-bottom:1px solid var(--border)}thead th:first-child{text-align:left}tbody td{padding:8px;font-size:0.74rem;text-align:right;border-bottom:1px solid var(--border)}tbody td:first-child{text-align:left}.tk{font-weight:700;font-size:0.78rem}
button{padding:8px 16px;border:none;background:var(--accent);color:#fff;border-radius:6px;font-weight:500;cursor:pointer;font-family:Inter,sans-serif;font-size:0.85rem;transition:all 0.2s}button:hover{opacity:0.9;transform:translateY(-1px)}
#connectScreen{display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:70vh;text-align:center;padding:40px 20px}#connectScreen h1{font-size:2rem;font-weight:700;letter-spacing:-1px;margin-bottom:10px}#connectScreen h1 span{color:var(--accent)}
</style>
</head>
<body>
<div class="ticker-strip" id="tickerStrip"><!-- Ticker content --></div>
<header><div class="logo">folio<em>.live</em></div><div class="hright"><div class="live-pill" id="livePill" style="display:none"><div class="live-dot" id="liveDot"></div><span id="liveName">Live</span></div><span id="updatedLbl" style="font-size:0.64rem;color:var(--muted)"></span><button class="btn" id="refreshBtn" onclick="forceRefresh()" style="display:none">Refresh</button><button class="btn" id="logoutBtn" onclick="logout()" style="display:none">Disconnect</button><div class="theme-btn" onclick="toggleTheme()" title="Toggle day/night">🌙</div></div></header>
<div class="nav-tabs" id="navTabs" style="display:none"><button class="nav-tab active" onclick="showPage('overview')">Overview</button><button class="nav-tab" onclick="showPage('holdings')">Holdings</button></div>
<div id="connectScreen"><h1>Your portfolio,<br><span>live.</span></h1><p>Connect once. See everything - holdings, P&L, risk, growth.</p><div class="steps-list"><div class="step-item"><div class="step-n">1</div><div class="step-t">Make sure <strong>server.py is running</strong></div></div><div class="step-item"><div class="step-n">2</div><div class="step-t">Click below <strong>Zerodha login</strong></div></div><div class="step-item"><div class="step-n">3</div><div class="step-t">Session lasts until <strong>midnight</strong></div></div></div><button class="connect-btn-big" onclick="connectZerodha()">Connect Zerodha</button></div>
<div id="dashboard">
<div class="page active" id="page-overview">
<div class="cards-row" id="cardsRow">
<div class="card" style="position:relative"><div class="card-lbl">Live Value</div><div class="card-val blur" id="liveValue">₹ --.--</div><span class="toggle-eye" id="liveValueToggle" onclick="toggleBlur('liveValue')">👁️</span></div>
<div class="card" style="position:relative"><div class="card-lbl">Total Capital</div><div class="card-val blur" id="totalCapital">₹ --.--</div><span class="toggle-eye" id="totalCapitalToggle" onclick="toggleBlur('totalCapital')">👁️</span></div>
<div class="card"><div class="card-lbl">Holdings + Cash</div><div class="card-val" id="totalCapitalDisplay">₹ --.--</div></div>
<div class="card"><div class="card-lbl">Invested</div><div class="card-val" id="totalCost">₹ --.--</div><div class="card-sub" id="holdingsCount"></div></div>
<div class="card"><div class="card-lbl">P&L</div><div class="card-val" id="totalPL">₹ --.--</div><div class="card-sub" id="totalPLPct">--</div></div>
<div class="card" style="position:relative"><div class="card-lbl">Cash</div><div class="card-val blur" id="cashAvailable">₹ --.--</div><span class="toggle-eye" id="cashAvailableToggle" onclick="toggleBlur('cashAvailable')">👁️</span></div>
</div>
<div class="tbl-wrap"><table><thead><tr><th>Stock</th><th>Qty</th><th>Avg</th><th>LTP</th><th style="position:relative">Invested<span class="toggle-eye" onclick="toggleBlur('invested','holdings')">👁️</span></th><th style="position:relative">Current<span class="toggle-eye" onclick="toggleBlur('current','holdings')">🙈</span></th><th style="position:relative">P&L<span class="toggle-eye" onclick="toggleBlur('pnl','holdings')">👁️</span></th><th>Return</th><th>Weight</th><th>Risk</th></tr></thead><tbody id="holdTbody"></tbody></table></div>
</div>
</div>
<footer style="border-top:1px solid var(--border);padding:12px 16px;display:flex;justify-content:space-between;align-items:center"><p style="font-size:0.62rem;color:var(--muted)">folio.live | Kite Connect API | Refreshes every 60s | Token expires midnight</p><p style="font-size:0.62rem;color:var(--muted)" id="footerUser"></p></footer>
<script>
let lastData=null,blurState={cards:{liveValue:true,totalCapital:true,cashAvailable:true},holdings:{invested:true,current:false,pnl:true}};
async function loadData(){try{const r=await fetch('/api/summary'),d=await r.json();lastData=d;renderOverview(d);document.getElementById('footerUser').textContent=d.username||'';document.getElementById('livePill').style.display='flex';}catch(e){console.error(e)}}
async function loadBlurState(){try{blurState=await(await fetch('/api/blur-state')).json()}catch(e){}applyBlur();localStorage.setItem('folio-blur',JSON.stringify(blurState))}
function saveBlurState(){fetch('/api/blur-state',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(blurState)})}
function applyBlur(){['liveValue','totalCapital','cashAvailable'].forEach(id=>{const el=document.getElementById(id);el&&el.classList.toggle('blur',blurState.cards[id])});[5,6,7].forEach((col,i)=>{const key=['invested','current','pnl'][i];const cells=document.querySelectorAll(`#holdTbody td:nth-child(${col})`);cells.forEach(c=>c.classList.toggle('blur',blurState.holdings[key]))})}
function toggleBlur(key,type='cards'){if(type==='cards'){blurState.cards[key]=!blurState.cards[key];document.getElementById(key).classList.toggle('blur');document.getElementById(key+'Toggle').innerHTML=blurState.cards[key]?'👁️':'🙈'}else{blurState.holdings[key]=!blurState.holdings[key];const col={invested:5,current:6,pnl:7}[key];document.querySelectorAll(`#holdTbody td:nth-child(${col})`).forEach(c=>c.classList.toggle('blur'))}saveBlurState();localStorage.setItem('folio-blur',JSON.stringify(blurState))}
function renderOverview(d){const p=d.portfolio;document.getElementById('liveValue').textContent='₹'+p.total_value.toLocaleString('en-IN');document.getElementById('totalCapital').textContent='₹'+p.total_capital.toLocaleString('en-IN');document.getElementById('totalCapitalDisplay').textContent='₹'+p.total_capital.toLocaleString('en-IN');document.getElementById('totalCost').textContent='₹'+p.total_cost.toLocaleString('en-IN');document.getElementById('totalPL').textContent='₹'+p.total_pl.toLocaleString('en-IN');document.getElementById('totalPLPct').textContent=p.total_pl_pct.toFixed(1)+'%';document.getElementById('holdingsCount').textContent=p.holdings_count+' stocks';document.getElementById('cashAvailable').textContent='₹'+p.cash_available.toLocaleString('en-IN');setTimeout(applyBlur,100)}
function renderHoldings(){const tbody=document.getElementById('holdTbody');tbody.innerHTML=lastData.holdings.map(h=>`<tr><td><a href="https://www.tradingview.com/chart?symbol=NSE:${h.tradingsymbol}" target="_blank" style="color:var(--text);text-decoration:none">${h.tradingsymbol}</a></td><td>${h.quantity}</td><td>${h.average_price.toLocaleString('en-IN')}</td><td>${h.last_price.toLocaleString('en-IN')}</td><td>₹${h.invested_value.toLocaleString('en-IN')}</td><td>₹${h.current_value.toLocaleString('en-IN')}</td><td style="color:${h.pnl>=0?'var(--gain)':'var(--loss)'}">${h.pnl>=0?'+' :''}₹${Math.abs(h.pnl).toLocaleString('en-IN')}</td><td>${h.pnl_pct.toFixed(1)}%</td><td>${h.weight_pct.toFixed(1)}%</td><td>${h.pnl_pct<-10?'⚠️':''}</td></tr>`).join('');setTimeout(applyBlur,50)}
function connectZerodha(){window.location.href='/auth/login'}
function forceRefresh(){sessionStorage.clear();loadData()}
function logout(){fetch('/auth/logout').then(()=>location.reload())}
function showPage(page){document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));document.getElementById('page-'+page).classList.add('active')}
document.addEventListener('DOMContentLoaded',()=>{const saved=localStorage.getItem('folio-blur');if(saved)blurState=JSON.parse(saved);loadBlurState();loadData();setInterval(loadData,60000)});

let isDark=true;function toggleTheme(){isDark=!isDark;document.body.classList.toggle('light',isDark);document.querySelector('.theme-btn').textContent=isDark?'🌙':'☀️';localStorage.setItem('folio-theme',isDark?'dark':'light')}
</script>
</body></html>'''

@app.route('/')
def index(): return Response(DASHBOARD_HTML, mimetype='text/html')

if API_KEY == "YOUR_API_KEY_HERE": print("Add your Kite API keys\nEdit server.py lines 20-21 OR\nexport KITE_API_KEY=xxx\nexport KITE_API_SECRET=xxx")
print("Open http://localhost:5000\nClick Connect Zerodha\nDashboard goes live instantly!")
print("="*55)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
