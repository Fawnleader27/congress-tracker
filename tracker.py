import smtplib, time, hashlib, os, requests, pandas as pd
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from bs4 import BeautifulSoup

# ── Config ────────────────────────────────────────────
GMAIL_ADDRESS  = os.environ["GMAIL_ADDRESS"]
GMAIL_APP_PWD  = os.environ["GMAIL_APP_PWD"]
ALERT_EMAIL_TO = GMAIL_ADDRESS
HEADERS        = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# ── Politician win rates from backtest ────────────────
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

# ── Scraper ───────────────────────────────────────────
def scrape_latest_trades(max_pages=3):
    session = requests.Session()
    session.headers.update(HEADERS)
    records = []

    for page in range(1, max_pages + 1):
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
                ticker = ticker_el.get_text(strip=True).replace(":US","").strip() if ticker_el else ""

                asset_el = row.select_one(".issuer-name a")
                asset = asset_el.get_text(strip=True) if asset_el else ""

                tx_el = row.select_one(".tx-type")
                transaction = tx_el.get_text(strip=True) if tx_el else ""
                if "buy" in transaction.lower():    transaction = "Purchase"
                elif "sell" in transaction.lower(): transaction = "Sale"

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
        time.sleep(1.5)

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
        POLITICIAN_WIN_RATES
    ).fillna(DEFAULT_WIN_RATE)

    all_purchases = all_trades[all_trades["transaction"] == "Purchase"].copy() if not all_trades.empty else purchases

    cluster_counts = {}
    for idx, row in purchases.iterrows():
        ticker = row["ticker"]
        date   = row["date"]
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


def format_email(new_trades, all_trades):
    new_df  = pd.DataFrame(new_trades)
    new_df["date"] = pd.to_datetime(new_df["date"], errors="coerce")
    scored  = score_trades(new_df, all_trades)

    if scored.empty:
        # No purchases — just show all new trades simply
        rows = ""
        for t in new_trades:
            color = "#1565C0" if "dem" in str(t.get("party","")).lower() else "#B71C1C"
            rows += f"""<tr>
                <td style="padding:10px;border-bottom:1px solid #eee;">
                    <strong style="color:{color}">{t.get('politician','')}</strong></td>
                <td style="padding:10px;border-bottom:1px solid #eee;">{t.get('ticker','')}</td>
                <td style="padding:10px;border-bottom:1px solid #eee;">{t.get('transaction','')}</td>
                <td style="padding:10px;border-bottom:1px solid #eee;">{t.get('range','')}</td>
                <td style="padding:10px;border-bottom:1px solid #eee;">{str(t.get('date',''))[:10]}</td>
            </tr>"""
        return f"""<div style="font-family:Arial,sans-serif;max-width:800px;margin:auto">
            <h2 style="background:#1e1e2e;color:white;padding:20px;border-radius:8px">🏛️ Congressional Trade Alert</h2>
            <p>{len(new_trades)} new trade(s) — {datetime.now().strftime("%B %d, %Y at %I:%M %p")}</p>
            <table style="width:100%;border-collapse:collapse;font-size:13px">
                <thead><tr style="background:#f5f5f5">
                    <th style="padding:10px;text-align:left">Politician</th>
                    <th style="padding:10px;text-align:left">Ticker</th>
                    <th style="padding:10px;text-align:left">Type</th>
                    <th style="padding:10px;text-align:left">Size</th>
                    <th style="padding:10px;text-align:left">Date</th>
                </tr></thead>
                <tbody>{rows}</tbody>
            </table>
        </div>"""

    recommended = scored[scored["recommended"] == True]
    others      = scored[scored["recommended"] == False]

    def make_rows(subset, highlight=False):
        rows = ""
        for _, t in subset.iterrows():
            bg    = "#f0fff4" if highlight else "white"
            badge = "⭐ COPY THIS" if highlight else ""
            color = "#1565C0" if "dem" in str(t.get("party","")).lower() else "#B71C1C"
            rows += f"""<tr style="background:{bg}">
                <td style="padding:10px;border-bottom:1px solid #eee;">
                    <strong style="color:{color}">{t.get('politician','')}</strong>
                    <span style="color:#888;font-size:11px"> ({t.get('party','')})</span>
                    <br><span style="font-size:11px;color:#888">Win rate: {t.get('win_rate_score', DEFAULT_WIN_RATE):.0f}%</span>
                </td>
                <td style="padding:10px;border-bottom:1px solid #eee;">
                    <strong style="font-size:15px">{t.get('ticker','')}</strong>
                    <br><span style="font-size:11px;color:#888">{str(t.get('asset_name',''))[:35]}</span>
                </td>
                <td style="padding:10px;border-bottom:1px solid #eee;">{t.get('transaction','')}</td>
                <td style="padding:10px;border-bottom:1px solid #eee;">{t.get('range','')}</td>
                <td style="padding:10px;border-bottom:1px solid #eee;">{str(t.get('date',''))[:10]}</td>
                <td style="padding:10px;border-bottom:1px solid #eee;">
                    <strong style="font-size:18px;color:{'#2e7d32' if highlight else '#666'}">{int(t.get('score',0))}</strong>
                    <br><span style="font-size:11px;color:#888">{int(t.get('cluster_count',1))} politician(s)</span>
                </td>
                <td style="padding:10px;border-bottom:1px solid #eee;color:#2e7d32;font-weight:bold">{badge}</td>
            </tr>"""
        return rows

    rec_section = ""
    if not recommended.empty:
        rec_section = f"""
        <h3 style="color:#2e7d32">⭐ Recommended to Copy ({len(recommended)})</h3>
        <table style="width:100%;border-collapse:collapse;font-size:13px">
            <thead><tr style="background:#e8f5e9">
                <th style="padding:10px;text-align:left">Politician</th>
                <th style="padding:10px;text-align:left">Ticker</th>
                <th style="padding:10px;text-align:left">Type</th>
                <th style="padding:10px;text-align:left">Size</th>
                <th style="padding:10px;text-align:left">Date</th>
                <th style="padding:10px;text-align:left">Score</th>
                <th style="padding:10px;text-align:left"></th>
            </tr></thead>
            <tbody>{make_rows(recommended, highlight=True)}</tbody>
        </table>"""

    other_section = ""
    if not others.empty:
        other_section = f"""
        <h3 style="color:#555;margin-top:24px">Other New Trades ({len(others)})</h3>
        <table style="width:100%;border-collapse:collapse;font-size:13px">
            <thead><tr style="background:#f5f5f5">
                <th style="padding:10px;text-align:left">Politician</th>
                <th style="padding:10px;text-align:left">Ticker</th>
                <th style="padding:10px;text-align:left">Type</th>
                <th style="padding:10px;text-align:left">Size</th>
                <th style="padding:10px;text-align:left">Date</th>
                <th style="padding:10px;text-align:left">Score</th>
                <th style="padding:10px;text-align:left"></th>
            </tr></thead>
            <tbody>{make_rows(others, highlight=False)}</tbody>
        </table>"""

    return f"""<div style="font-family:Arial,sans-serif;max-width:800px;margin:auto">
        <h2 style="background:#1e1e2e;color:white;padding:20px;border-radius:8px">🏛️ Congressional Trade Alert</h2>
        <p style="color:#444">{len(new_trades)} new trade(s) — {datetime.now().strftime("%B %d, %Y at %I:%M %p")}
        <br><span style="font-size:12px;color:#888">Score = win rate (0–100) + cluster bonus (+25 if 2+ politicians bought same stock)</span></p>
        {rec_section}
        {other_section}
        <p style="color:#888;font-size:12px;margin-top:24px">Sent by your Congressional Trade Tracker · Data via Capitol Trades</p>
    </div>"""


# ── Main ──────────────────────────────────────────────
def get_trade_id(row):
    key = f"{row.get('politician','')}{row.get('date','')}{row.get('ticker','')}{row.get('transaction','')}"
    return hashlib.md5(key.encode()).hexdigest()

def main():
    print(f"🏛️  Congressional Trade Tracker — {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    # Load known trade IDs from file (persists between runs on GitHub)
    known_ids_file = "known_ids.txt"
    if os.path.exists(known_ids_file):
        with open(known_ids_file) as f:
            known_ids = set(f.read().splitlines())
        print(f"✅ Loaded {len(known_ids)} known trade IDs")
    else:
        known_ids = set()
        print("⚠️  No known IDs file — first run")

    # Scrape latest trades
    print("🌐 Scraping Capitol Trades …")
    df = scrape_latest_trades(max_pages=3)

    if df.empty:
        print("❌ No trades fetched — exiting")
        return

    print(f"✅ Fetched {len(df)} trades")

    # Find new ones
    new_trades = []
    new_ids    = set()
    for _, row in df.iterrows():
        tid = get_trade_id(row)
        if tid not in known_ids:
            new_trades.append(row.to_dict())
            new_ids.add(tid)

    print(f"🔍 {len(new_trades)} new trade(s) found")

    if new_trades:
        send_email(
            subject=f"🏛️ {len(new_trades)} New Congressional Trade(s) Filed",
            body=format_email(new_trades, df)
        )
        # Save updated known IDs
        all_ids = known_ids | new_ids
        with open(known_ids_file, "w") as f:
            f.write("\n".join(all_ids))
        print(f"💾 Saved {len(all_ids)} known IDs")
    else:
        print("✅ No new trades — no email sent")

if __name__ == "__main__":
    main()
