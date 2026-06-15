import smtplib, time, hashlib, os, json, requests
import pandas as pd
import yfinance as yf
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone
from bs4 import BeautifulSoup

# ── Config ────────────────────────────────────────────
GMAIL_ADDRESS  = os.environ["GMAIL_ADDRESS"]
GMAIL_APP_PWD  = os.environ["GMAIL_APP_PWD"]
ALERT_EMAIL_TO = GMAIL_ADDRESS
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

PORTFOLIO_FILE = "portfolio.json"
KNOWN_IDS_FILE = "known_ids.txt"

POLITICIAN_WIN_RATES = {
    "Ro Khanna":        100.0,
    "Michael McCaul":   100.0,
    "Nancy Pelosi":     100.0,
    "Pat Fallon":        50.0,
    "Tommy Tuberville":  66.7,
    "Josh Gottheimer":   50.0,
    "Dan Crenshaw":       0.0,
}
DEFAULT_WIN_RATE     = 50.0
CLUSTER_WINDOW_DAYS  = 30
CLUSTER_BONUS        = 25
HIGH_SCORE_THRESHOLD = 70

TRADE_SIZE_MAP = {
    "1K–15K":     8_000,
    "15K–50K":   32_500,
    "50K–100K":  75_000,
    "100K–250K": 175_000,
    "250K–500K": 375_000,
    "500K–1M":   750_000,
    "1M+":     1_500_000,
}


# ── Portfolio ─────────────────────────────────────────
def load_portfolio():
    if os.path.exists(PORTFOLIO_FILE):
        with open(PORTFOLIO_FILE) as f:
            return json.load(f)
    return {
        "positions": {},
        "closed_trades": [],
        "total_invested": 0,
        "last_updated": "",
    }

def save_portfolio(p):
    p["last_updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    with open(PORTFOLIO_FILE, "w") as f:
        json.dump(p, f, indent=2)

def get_price(ticker):
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period="1d")
        if not hist.empty:
            return round(float(hist["Close"].iloc[-1]), 2)
    except:
        pass
    return None

def paper_buy(portfolio, trade):
    ticker     = trade.get("ticker", "")
    politician = trade.get("politician", "")
    size_label = trade.get("range", "")
    amount     = TRADE_SIZE_MAP.get(size_label, 32_500)
    price      = get_price(ticker)

    if not price or price <= 0:
        print(f"  ⚠️  Could not get price for {ticker} — skipping paper trade")
        return portfolio, None

    try:
        shares = round(float(amount) / float(price), 4)
    except Exception as e:
        print(f"  ⚠️  Could not calculate shares for {ticker}: {e}")
        return portfolio, None

    key = f"{ticker}_{politician}_{datetime.now().strftime('%Y%m%d')}"

    position = {
        "key":        key,
        "ticker":     ticker,
        "politician": politician,
        "party":      trade.get("party", ""),
        "action":     "Purchase",
        "shares":     shares,
        "buy_price":  price,
        "amount":     amount,
        "buy_date":   datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "range":      size_label,
        "status":     "open",
    }

    portfolio["positions"][key] = position
    portfolio["total_invested"] += amount
    print(f"  📈 Paper BUY  {shares} shares of {ticker} @ ${price} (${amount:,})")
    return portfolio, position

def paper_sell(portfolio, trade):
    ticker     = trade.get("ticker", "")
    politician = trade.get("politician", "")
    price      = get_price(ticker)

    if not price:
        print(f"  ⚠️  Could not get price for {ticker} — skipping paper sell")
        return portfolio, None

    matched = []
    for key, pos in portfolio["positions"].items():
        if pos["ticker"] == ticker and pos["politician"] == politician and pos["status"] == "open":
            matched.append((key, pos))

    if not matched:
        print(f"  ℹ️  No open position found for {ticker} / {politician} — skipping")
        return portfolio, None

    closed = []
    for key, pos in matched:
        gain     = (price - pos["buy_price"]) / pos["buy_price"] * 100
        profit   = (price - pos["buy_price"]) * pos["shares"]
        closed_pos = {**pos,
            "sell_price": price,
            "sell_date":  datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "gain_pct":   round(gain, 2),
            "profit":     round(profit, 2),
            "status":     "closed",
        }
        portfolio["closed_trades"].append(closed_pos)
        del portfolio["positions"][key]
        closed.append(closed_pos)
        print(f"  📉 Paper SELL {pos['shares']} shares of {ticker} @ ${price} ({gain:+.2f}%)")

    return portfolio, closed


# ── Scraper ───────────────────────────────────────────
def scrape_latest_trades(max_pages=3):
    session = requests.Session()
    session.headers.update(HEADERS)
    records = []

    for page in range(1, max_pages + 1):
        wait = 5 if page == 1 else 3
        time.sleep(wait)
        try:
            r = session.get(f"https://www.capitoltrades.com/trades?page={page}", timeout=15)
            r.raise_for_status()
        except Exception as e:
            print(f"  ⚠️  Page {page} failed: {e}")
            break

        soup = BeautifulSoup(r.text, "lxml")
        for row in soup.select("tbody tr"):
            try:
                politician = row.select_one(".politician-name a")
                politician = politician.get_text(strip=True) if politician else ""

                party_el = row.select_one(".party")
                party = party_el.get_text(strip=True) if party_el else ""

                ticker_el = row.select_one(".issuer-ticker")
                ticker = ticker_el.get_text(strip=True).replace(":US", "").strip() if ticker_el else ""

                asset_el = row.select_one(".issuer-name a")
                asset = asset_el.get_text(strip=True) if asset_el else ""

                tx_el = row.select_one(".tx-type")
                transaction = tx_el.get_text(strip=True) if tx_el else ""
                if "buy" in transaction.lower():
                    transaction = "Purchase"
                elif "sell" in transaction.lower():
                    transaction = "Sale"

                size_el = row.select_one(".trade-size .text-txt-dimmer")
                size = size_el.get_text(strip=True) if size_el else ""

                date_cells = row.select("td .text-center")
                trade_date = ""
                if len(date_cells) >= 2:
                    day_el  = date_cells[1].select_one(".text-size-3")
                    year_el = date_cells[1].select_one(".text-size-2")
                    if day_el and year_el:
                        trade_date = f"{day_el.get_text(strip=True)} {year_el.get_text(strip=True)}"

                if politician and ticker and ticker != "N/A":
                    records.append({
                        "politician":  politician,
                        "party":       party,
                        "date":        trade_date,
                        "ticker":      ticker,
                        "asset_name":  asset,
                        "transaction": transaction,
                        "range":       size,
                    })
            except:
                continue
        time.sleep(3)

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)
    df["date"] = pd.to_datetime(df["date"], format="%d %b %Y", errors="coerce")
    return df.dropna(subset=["date"])


# ── Scorer ────────────────────────────────────────────
def score_trades(df, all_trades):
    if df.empty:
        return df
    purchases = df[df["transaction"] == "Purchase"].copy()
    if purchases.empty:
        return purchases

    purchases["win_rate_score"] = purchases["politician"].map(
        POLITICIAN_WIN_RATES).fillna(DEFAULT_WIN_RATE)

    all_purchases = all_trades[all_trades["transaction"] == "Purchase"].copy() \
        if not all_trades.empty else purchases

    cluster_counts = {}
    for idx, row in purchases.iterrows():
        ticker, date = row["ticker"], row["date"]
        if pd.isna(date):
            cluster_counts[idx] = 1
            continue
        cutoff = date - pd.Timedelta(days=CLUSTER_WINDOW_DAYS)
        count  = len(all_purchases[
            (all_purchases["ticker"] == ticker) &
            (all_purchases["date"]   >= cutoff) &
            (all_purchases["date"]   <= date)
        ])
        cluster_counts[idx] = max(count, 1)

    purchases["cluster_count"] = pd.Series(cluster_counts)
    purchases["cluster_bonus"]  = (purchases["cluster_count"] >= 2).astype(int) * CLUSTER_BONUS
    purchases["score"]          = purchases["win_rate_score"] + purchases["cluster_bonus"]
    purchases["recommended"]    = purchases["score"] >= HIGH_SCORE_THRESHOLD
    return purchases.sort_values("score", ascending=False).reset_index(drop=True)


# ── Portfolio summary ─────────────────────────────────
def portfolio_summary(portfolio):
    positions = portfolio.get("positions", {})
    closed    = portfolio.get("closed_trades", [])

    open_value = 0
    open_cost  = 0
    for pos in positions.values():
        price = get_price(pos["ticker"]) or pos["buy_price"]
        open_value += price * pos["shares"]
        open_cost  += pos["buy_price"] * pos["shares"]

    total_profit = sum(t.get("profit", 0) for t in closed)
    open_gain    = open_value - open_cost
    win_trades   = [t for t in closed if t.get("gain_pct", 0) > 0]
    win_rate     = len(win_trades) / len(closed) * 100 if closed else 0
    best         = max(closed, key=lambda x: x.get("gain_pct", 0), default=None)
    worst        = min(closed, key=lambda x: x.get("gain_pct", 0), default=None)

    return {
        "open_positions": len(positions),
        "closed_trades":  len(closed),
        "open_cost":      round(open_cost, 2),
        "open_value":     round(open_value, 2),
        "open_gain":      round(open_gain, 2),
        "open_gain_pct":  round(open_gain / open_cost * 100, 2) if open_cost else 0,
        "total_profit":   round(total_profit, 2),
        "win_rate":       round(win_rate, 1),
        "best_trade":     best,
        "worst_trade":    worst,
        "last_updated":   portfolio.get("last_updated", ""),
    }


# ── Email ─────────────────────────────────────────────
def send_email(subject, body):
    msg = MIMEMultipart()
    msg["From"]    = GMAIL_ADDRESS
    msg["To"]      = ALERT_EMAIL_TO
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "html"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PWD)
            server.send_message(msg)
        print(f"  📧 Email sent → {ALERT_EMAIL_TO}")
    except Exception as e:
        print(f"  ❌ Email failed: {e}")

def portfolio_email_snippet(summary):
    op  = summary["open_gain_pct"]
    tp  = summary["total_profit"]
    wr  = summary["win_rate"]
    col = "#2e7d32" if op >= 0 else "#c62828"
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "your-username")
    return f"""
    <div style="margin-top:24px;padding:16px;background:#f9f9f9;border-radius:8px;border-left:4px solid {col}">
        <h3 style="margin:0 0 10px;font-size:14px;color:#333">📊 Paper Portfolio Snapshot</h3>
        <table style="font-size:13px;color:#444;border-collapse:collapse;width:100%">
            <tr><td style="padding:3px 12px 3px 0">Open positions</td>
                <td><strong>{summary['open_positions']}</strong></td></tr>
            <tr><td style="padding:3px 12px 3px 0">Open P&L</td>
                <td><strong style="color:{col}">{op:+.2f}%  (${summary['open_gain']:+,.0f})</strong></td></tr>
            <tr><td style="padding:3px 12px 3px 0">Closed trades</td>
                <td><strong>{summary['closed_trades']}</strong></td></tr>
            <tr><td style="padding:3px 12px 3px 0">Total realised profit</td>
                <td><strong style="color:{'#2e7d32' if tp>=0 else '#c62828'}">${tp:+,.0f}</strong></td></tr>
            <tr><td style="padding:3px 12px 3px 0">Win rate</td>
                <td><strong>{wr:.1f}%</strong></td></tr>
        </table>
        <p style="margin:10px 0 0;font-size:12px;color:#888">
            Full dashboard → <a href="https://{owner}.github.io/congress-tracker/">view portfolio</a>
        </p>
    </div>"""

def format_email(new_trades, all_trades, portfolio_snap):
    new_df = pd.DataFrame(new_trades)
    new_df["date"] = pd.to_datetime(new_df["date"], errors="coerce")
    scored = score_trades(new_df, all_trades)

    recommended = scored[scored["recommended"] == True] if not scored.empty else pd.DataFrame()
    others      = scored[scored["recommended"] == False] if not scored.empty else pd.DataFrame()
    sales       = new_df[new_df["transaction"] == "Sale"] if not new_df.empty else pd.DataFrame()

    def make_rows(subset, highlight=False):
        rows = ""
        for _, t in subset.iterrows():
            bg    = "#f0fff4" if highlight else "white"
            badge = "⭐ COPY THIS" if highlight else ""
            color = "#1565C0" if "dem" in str(t.get("party", "")).lower() else "#B71C1C"
            try:
                score_val = t.get("score", None)
                score = int(score_val) if score_val is not None and str(score_val) != "nan" else "-"
            except Exception:
                score = "-"
            rows += f"""<tr style="background:{bg}">
                <td style="padding:8px;border-bottom:1px solid #eee;">
                    <strong style="color:{color}">{t.get('politician', '')}</strong>
                    <br><span style="font-size:11px;color:#888">{t.get('party', '')}</span>
                </td>
                <td style="padding:8px;border-bottom:1px solid #eee;">
                    <strong>{t.get('ticker', '')}</strong>
                    <br><span style="font-size:11px;color:#888">{str(t.get('asset_name', ''))[:30]}</span>
                </td>
                <td style="padding:8px;border-bottom:1px solid #eee;">{t.get('transaction', '')}</td>
                <td style="padding:8px;border-bottom:1px solid #eee;">{t.get('range', '')}</td>
                <td style="padding:8px;border-bottom:1px solid #eee;">{str(t.get('date', ''))[:10]}</td>
                <td style="padding:8px;border-bottom:1px solid #eee;font-weight:bold;color:#2e7d32">{badge}</td>
            </tr>"""
        return rows

    rec_section = ""
    if not recommended.empty:
        rec_section = f"""
        <h3 style="color:#2e7d32">⭐ Recommended to copy ({len(recommended)})</h3>
        <table style="width:100%;border-collapse:collapse;font-size:13px">
            <thead><tr style="background:#e8f5e9">
                <th style="padding:8px;text-align:left">Politician</th>
                <th style="padding:8px;text-align:left">Ticker</th>
                <th style="padding:8px;text-align:left">Type</th>
                <th style="padding:8px;text-align:left">Size</th>
                <th style="padding:8px;text-align:left">Date</th>
                <th style="padding:8px;text-align:left"></th>
            </tr></thead>
            <tbody>{make_rows(recommended, highlight=True)}</tbody>
        </table>"""

    other_section = ""
    if not others.empty or not sales.empty:
        combined_others = pd.concat([others, sales], ignore_index=True) if not sales.empty else others
        other_section = f"""
        <h3 style="color:#555;margin-top:20px">Other new trades ({len(combined_others)})</h3>
        <table style="width:100%;border-collapse:collapse;font-size:13px">
            <thead><tr style="background:#f5f5f5">
                <th style="padding:8px;text-align:left">Politician</th>
                <th style="padding:8px;text-align:left">Ticker</th>
                <th style="padding:8px;text-align:left">Type</th>
                <th style="padding:8px;text-align:left">Size</th>
                <th style="padding:8px;text-align:left">Date</th>
                <th style="padding:8px;text-align:left"></th>
            </tr></thead>
            <tbody>{make_rows(combined_others)}</tbody>
        </table>"""

    return f"""<div style="font-family:Arial,sans-serif;max-width:800px;margin:auto">
        <h2 style="background:#1e1e2e;color:white;padding:20px;border-radius:8px">
            🏛️ Congressional Trade Alert</h2>
        <p style="color:#444">{len(new_trades)} new trade(s) — {datetime.now().strftime("%B %d, %Y at %I:%M %p")}</p>
        {rec_section}
        {other_section}
        {portfolio_email_snippet(portfolio_snap)}
        <p style="color:#888;font-size:12px;margin-top:20px">
            Sent by your Congressional Trade Tracker · Data via Capitol Trades</p>
    </div>"""


# ── Trade ID ──────────────────────────────────────────
def get_trade_id(row):
    try:
        date = pd.to_datetime(str(row.get("date", ""))).strftime("%Y-%m-%d")
    except:
        date = "unknown"
    try:
        politician  = str(row.get("politician",  "")).strip().lower()
        ticker      = str(row.get("ticker",      "")).strip().upper()
        transaction = str(row.get("transaction", "")).strip().lower()
        key = f"{politician}|{date}|{ticker}|{transaction}"
        return hashlib.md5(key.encode()).hexdigest()
    except:
        return hashlib.md5(str(row).encode()).hexdigest()


# ── Main ──────────────────────────────────────────────
def main():
    print(f"🏛️  Congressional Trade Tracker — {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    known_ids = set()
    if os.path.exists(KNOWN_IDS_FILE):
        with open(KNOWN_IDS_FILE) as f:
            known_ids = set(line.strip() for line in f if line.strip())
    print(f"✅ Loaded {len(known_ids)} known trade IDs")

    portfolio = load_portfolio()
    print(f"✅ Portfolio: {len(portfolio['positions'])} open, {len(portfolio['closed_trades'])} closed")

    print("🌐 Scraping Capitol Trades …")
    df = scrape_latest_trades(max_pages=3)
    if df.empty:
        print("❌ No trades fetched")
        return
    print(f"✅ Fetched {len(df)} trades")

    new_trades = []
    new_ids    = set()
    for _, row in df.iterrows():
        tid = get_trade_id(row)
        if tid not in known_ids:
            new_trades.append(row.to_dict())
            new_ids.add(tid)

    print(f"🔍 {len(new_trades)} new trade(s)")

    new_positions = []
    if new_trades:
        for trade in new_trades:
            try:
                tx = str(trade.get("transaction", "")).lower()
                if "purchase" in tx or "buy" in tx:
                    portfolio, pos = paper_buy(portfolio, trade)
                    if pos:
                        new_positions.append(("buy", pos))
                elif "sale" in tx or "sell" in tx:
                    portfolio, closed = paper_sell(portfolio, trade)
                    if closed:
                        for c in (closed if isinstance(closed, list) else [closed]):
                            new_positions.append(("sell", c))
            except Exception as e:
                print(f"  ⚠️  Skipping trade due to error: {e}")
                continue

        save_portfolio(portfolio)

        summary = portfolio_summary(portfolio)
        send_email(
            subject=f"🏛️ {len(new_trades)} New Congressional Trade(s) Filed",
            body=format_email(new_trades, df, summary)
        )

        all_ids = known_ids | new_ids
        with open(KNOWN_IDS_FILE, "w") as f:
            f.write("\n".join(sorted(all_ids)) + "\n")
        print(f"💾 Saved {len(all_ids)} known IDs")
    else:
        save_portfolio(portfolio)
        print("✅ No new trades — portfolio prices refreshed")

    generate_webpage(portfolio)
    print("✅ Webpage updated → index.html")


# ── Webpage ───────────────────────────────────────────
def generate_webpage(portfolio):
    positions = portfolio.get("positions", {})
    closed    = portfolio.get("closed_trades", [])
    updated   = portfolio.get("last_updated", "")

    open_rows = ""
    total_open_cost  = 0
    total_open_value = 0
    for pos in sorted(positions.values(), key=lambda x: x["buy_date"], reverse=True):
        price    = get_price(pos["ticker"]) or pos["buy_price"]
        value    = price * pos["shares"]
        cost     = pos["buy_price"] * pos["shares"]
        gain_pct = (price - pos["buy_price"]) / pos["buy_price"] * 100
        gain_val = value - cost
        total_open_cost  += cost
        total_open_value += value
        color     = "#2e7d32" if gain_pct >= 0 else "#c62828"
        party_col = "#1565C0" if "dem" in pos.get("party", "").lower() else "#B71C1C"
        open_rows += f"""<tr>
            <td>{pos['buy_date']}</td>
            <td><strong style="color:{party_col}">{pos['politician']}</strong></td>
            <td><strong>{pos['ticker']}</strong></td>
            <td>{pos.get('range', '')}</td>
            <td>${pos['buy_price']:,.2f}</td>
            <td>${price:,.2f}</td>
            <td style="color:{color};font-weight:bold">{gain_pct:+.2f}%</td>
            <td style="color:{color};font-weight:bold">${gain_val:+,.0f}</td>
        </tr>"""

    open_gain     = total_open_value - total_open_cost
    open_gain_pct = open_gain / total_open_cost * 100 if total_open_cost else 0

    closed_rows  = ""
    total_profit = 0
    wins         = 0
    for t in sorted(closed, key=lambda x: x.get("sell_date", ""), reverse=True):
        total_profit += t.get("profit", 0)
        if t.get("gain_pct", 0) > 0:
            wins += 1
        color     = "#2e7d32" if t.get("gain_pct", 0) >= 0 else "#c62828"
        party_col = "#1565C0" if "dem" in t.get("party", "").lower() else "#B71C1C"
        closed_rows += f"""<tr>
            <td>{t.get('buy_date', '')}</td>
            <td>{t.get('sell_date', '')}</td>
            <td><strong style="color:{party_col}">{t.get('politician', '')}</strong></td>
            <td><strong>{t.get('ticker', '')}</strong></td>
            <td>{t.get('range', '')}</td>
            <td>${t.get('buy_price', 0):,.2f}</td>
            <td>${t.get('sell_price', 0):,.2f}</td>
            <td style="color:{color};font-weight:bold">{t.get('gain_pct', 0):+.2f}%</td>
            <td style="color:{color};font-weight:bold">${t.get('profit', 0):+,.0f}</td>
        </tr>"""

    win_rate = wins / len(closed) * 100 if closed else 0

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Congressional Trade Tracker — Portfolio</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #0f0f13; color: #e0e0e0; padding: 24px; min-height: 100vh; }}
  h1 {{ font-size: 24px; font-weight: 600; margin-bottom: 4px; color: #fff; }}
  .subtitle {{ color: #888; font-size: 13px; margin-bottom: 28px; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
            gap: 14px; margin-bottom: 32px; }}
  .card {{ background: #1a1a24; border: 1px solid #2a2a38; border-radius: 12px;
           padding: 18px; }}
  .card-label {{ font-size: 12px; color: #888; margin-bottom: 6px; }}
  .card-value {{ font-size: 22px; font-weight: 600; color: #fff; }}
  .card-value.green {{ color: #4caf50; }}
  .card-value.red   {{ color: #f44336; }}
  .section {{ margin-bottom: 36px; }}
  .section h2 {{ font-size: 16px; font-weight: 600; color: #fff;
                 margin-bottom: 14px; padding-bottom: 8px;
                 border-bottom: 1px solid #2a2a38; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ text-align: left; padding: 10px 12px; color: #888;
        font-weight: 500; border-bottom: 1px solid #2a2a38; }}
  td {{ padding: 10px 12px; border-bottom: 1px solid #1e1e2a; }}
  tr:hover td {{ background: #1e1e2a; }}
  .green {{ color: #4caf50; }}
  .red   {{ color: #f44336; }}
  .updated {{ color: #555; font-size: 12px; margin-top: 32px; text-align: center; }}
</style>
</head>
<body>
<h1>🏛️ Congressional Trade Tracker</h1>
<p class="subtitle">Paper portfolio — auto-updated every hour · Last updated: {updated}</p>

<div class="cards">
  <div class="card">
    <div class="card-label">Open positions</div>
    <div class="card-value">{len(positions)}</div>
  </div>
  <div class="card">
    <div class="card-label">Open P&L</div>
    <div class="card-value {'green' if open_gain_pct >= 0 else 'red'}">{open_gain_pct:+.2f}%</div>
  </div>
  <div class="card">
    <div class="card-label">Open value</div>
    <div class="card-value">${total_open_value:,.0f}</div>
  </div>
  <div class="card">
    <div class="card-label">Closed trades</div>
    <div class="card-value">{len(closed)}</div>
  </div>
  <div class="card">
    <div class="card-label">Total profit</div>
    <div class="card-value {'green' if total_profit >= 0 else 'red'}">${total_profit:+,.0f}</div>
  </div>
  <div class="card">
    <div class="card-label">Win rate</div>
    <div class="card-value">{win_rate:.1f}%</div>
  </div>
</div>

<div class="section">
  <h2>📈 Open positions ({len(positions)})</h2>
  {'<p style="color:#555;font-size:13px">No open positions yet.</p>' if not positions else f'''
  <table>
    <thead><tr>
      <th>Bought</th><th>Politician</th><th>Ticker</th><th>Size</th>
      <th>Buy price</th><th>Current</th><th>Gain %</th><th>Gain $</th>
    </tr></thead>
    <tbody>{open_rows}</tbody>
  </table>'''}
</div>

<div class="section">
  <h2>📉 Closed trades ({len(closed)})</h2>
  {'<p style="color:#555;font-size:13px">No closed trades yet.</p>' if not closed else f'''
  <table>
    <thead><tr>
      <th>Bought</th><th>Sold</th><th>Politician</th><th>Ticker</th><th>Size</th>
      <th>Buy price</th><th>Sell price</th><th>Gain %</th><th>Profit $</th>
    </tr></thead>
    <tbody>{closed_rows}</tbody>
  </table>'''}
</div>

<p class="updated">Data via Capitol Trades · Prices via Yahoo Finance ·
   <a href="https://github.com" style="color:#555">GitHub</a></p>
</body>
</html>"""

    with open("index.html", "w") as f:
        f.write(html)


if __name__ == "__main__":
    main()
