#!/usr/bin/env python3
"""
Refreshes the dataset embedded in markets.html from Yahoo Finance.

Stdlib-only (urllib/json), so it runs anywhere with no pip install -
a local machine, a cron box, or a GitHub Actions runner.

Usage:
    python3 update_data.py
"""

import datetime
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HTML_PATH = "markets.html"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
CHART_URL = "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}?range=1mo&interval=1d"
OUNCE_TO_GRAM = 31.1034768

# The watchlist. Edit this to add/remove/regroup what the page tracks -
# the next run picks up the change automatically.
INDICES = [
    ("NSE (Nifty 50)", "^NSEI"),
    ("New York (Dow)", "^DJI"),
    ("Nasdaq", "^IXIC"),
    ("Nikkei 225", "^N225"),
    ("Shanghai", "000001.SS"),
    ("London (FTSE)", "^FTSE"),
]

# Gold/silver are quoted as USD/troy-oz futures and converted to INR/gram
# using the USD/INR rate, so they're comparable to the other India series.
GOLD_SYMBOL = "GC=F"
SILVER_SYMBOL = "SI=F"
USDINR_SYMBOL = "INR=X"

STOCK_GROUPS = [
    ("Banking & Finance", ["HDFCBANK", "ICICIBANK", "IDFCFIRSTB", "JIOFIN"], ".NS"),
    ("Infra & Energy", ["LT", "RELIANCE"], ".NS"),
    ("Consumer & Retail", ["TITAN", "DMART"], ".NS"),
    ("Aviation", ["INDIGO"], ".NS"),
    ("US Stocks", ["WMT", "NVDA", "GOOGL", "AAPL", "MSFT"], ""),
]


def yahoo_quote_link(symbol):
    return "https://finance.yahoo.com/quote/" + urllib.parse.quote(symbol, safe="")


def fetch_chart(symbol, retries=3):
    url = CHART_URL.format(symbol=urllib.parse.quote(symbol, safe=""))
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    last_err = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.load(resp)
        except (urllib.error.URLError, urllib.error.HTTPError) as e:
            last_err = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"failed to fetch {symbol}: {last_err}")


def series(payload):
    result = payload["chart"]["result"][0]
    ts = result["timestamp"]
    closes = result["indicators"]["quote"][0]["close"]
    dates, values = [], []
    for t, c in zip(ts, closes):
        if c is None:
            continue
        dt = datetime.datetime.fromtimestamp(t, tz=datetime.timezone.utc)
        dates.append(dt.strftime("%Y-%m-%d"))
        values.append(round(c, 4))
    return dates, values


def align(dates, ref_dates, ref_values):
    """Forward-fill ref_values onto dates (used to line FX rate up with gold/silver dates)."""
    ref_map = dict(zip(ref_dates, ref_values))
    out = []
    last = ref_values[0] if ref_values else None
    for d in dates:
        if d in ref_map:
            last = ref_map[d]
        out.append(last)
    return out


def fetch_series_entry(label, symbol, link=None):
    payload = fetch_chart(symbol)
    dates, values = series(payload)
    return {"label": label, "dates": dates, "values": values, "link": link or yahoo_quote_link(symbol)}


def build_dataset():
    print("Fetching indices...", file=sys.stderr)
    indices = []
    for label, symbol in INDICES:
        indices.append(fetch_series_entry(label, symbol))
        time.sleep(1.5)

    print("Fetching gold/silver/FX...", file=sys.stderr)
    gold_payload = fetch_chart(GOLD_SYMBOL)
    time.sleep(1.5)
    silver_payload = fetch_chart(SILVER_SYMBOL)
    time.sleep(1.5)
    fx_payload = fetch_chart(USDINR_SYMBOL)

    gold_dates, gold_usd_oz = series(gold_payload)
    silver_dates, silver_usd_oz = series(silver_payload)
    fx_dates, fx_values = series(fx_payload)

    gold_fx = align(gold_dates, fx_dates, fx_values)
    silver_fx = align(silver_dates, fx_dates, fx_values)
    gold_inr_gram = [round(v * r / OUNCE_TO_GRAM, 2) for v, r in zip(gold_usd_oz, gold_fx)]
    silver_inr_gram = [round(v * r / OUNCE_TO_GRAM, 2) for v, r in zip(silver_usd_oz, silver_fx)]

    commodities = [
        {"label": "Gold (INR/gram)", "dates": gold_dates, "values": gold_inr_gram, "link": yahoo_quote_link(GOLD_SYMBOL)},
        {"label": "Silver (INR/gram)", "dates": silver_dates, "values": silver_inr_gram, "link": yahoo_quote_link(SILVER_SYMBOL)},
    ]

    print("Fetching stocks by group...", file=sys.stderr)
    stocks = {}
    for group, symbols, suffix in STOCK_GROUPS:
        entries = []
        for sym in symbols:
            full_symbol = sym + suffix
            entries.append(fetch_series_entry(sym, full_symbol))
            time.sleep(1.5)
        stocks[group] = entries

    return {
        "generatedAt": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "indices": indices,
        "commodities": commodities,
        "stocks": stocks,
    }


def update_html(dataset, html_path=HTML_PATH):
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()

    new_json = json.dumps(dataset, separators=(",", ":"))
    pattern = re.compile(
        r'(<script id="app-data" type="application/json">).*?(</script>)', re.S
    )
    new_content, n = pattern.subn(
        lambda m: m.group(1) + new_json + m.group(2), content
    )
    if n != 1:
        raise RuntimeError(f"expected exactly 1 app-data script tag, found {n}")

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(new_content)


def main():
    dataset = build_dataset()
    update_html(dataset)
    print(f"Updated {HTML_PATH} (generatedAt {dataset['generatedAt']})", file=sys.stderr)


if __name__ == "__main__":
    main()
