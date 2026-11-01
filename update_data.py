#!/usr/bin/env python3
"""
Refreshes the dataset embedded in markets.html from Yahoo Finance.

Stdlib-only (urllib/json), so it runs anywhere with no pip install -
a local machine, a cron box, or a GitHub Actions runner.

A series that can't be fetched keeps its previous data and is listed under
"pending" in the dataset, for a later --retry-pending run to pick up.

Usage:
    python3 update_data.py                  # refresh everything
    python3 update_data.py --retry-pending  # refresh only what's pending
    python3 update_data.py --push           # also commit markets.html and push
"""

import argparse
import datetime
import functools
import json
import re
import subprocess
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
REQUEST_GAP_SECONDS = 2
RATE_LIMIT_WAIT_SECONDS = 120
APP_DATA_RE = re.compile(
    r'(<script id="app-data" type="application/json">)(.*?)(</script>)', re.S
)

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
    rate_limit_retried = False
    attempt = 0
    while attempt < retries:
        time.sleep(REQUEST_GAP_SECONDS)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429:
                if rate_limit_retried:
                    break
                rate_limit_retried = True
                print(f"Rate limited on {symbol}, retrying in {RATE_LIMIT_WAIT_SECONDS}s...", file=sys.stderr)
                time.sleep(RATE_LIMIT_WAIT_SECONDS)
                continue
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
        attempt += 1
        time.sleep(2 * attempt)
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


@functools.lru_cache(maxsize=1)
def fetch_usdinr():
    return series(fetch_chart(USDINR_SYMBOL))


def fetch_commodity_entry(label, symbol):
    dates, usd_oz = series(fetch_chart(symbol))
    fx_dates, fx_values = fetch_usdinr()
    fx = align(dates, fx_dates, fx_values)
    inr_gram = [round(v * r / OUNCE_TO_GRAM, 2) for v, r in zip(usd_oz, fx)]
    return {"label": label, "dates": dates, "values": inr_gram, "link": yahoo_quote_link(symbol)}


def fetch_currency_entry(label):
    dates, values = fetch_usdinr()
    return {"label": label, "dates": dates, "values": values, "link": yahoo_quote_link(USDINR_SYMBOL)}


def build_dataset(previous, only=None):
    """Fetch the watchlist, falling back to `previous` for failed series.

    If `only` is given, just those labels (and any new to the watchlist) are
    fetched; everything else is carried over from `previous`.
    """
    previous_entries = {
        e["label"]: e
        for entries in [previous["indices"], previous["commodities"], previous.get("currency", []), *previous["stocks"].values()]
        for e in entries
    }
    failed = []
    refreshed = []

    def collect(pairs):
        entries = []
        for label, fetch in pairs:
            if only is not None and label not in only and label in previous_entries:
                entries.append(previous_entries[label])
                continue
            try:
                entries.append(fetch())
                refreshed.append(label)
            except RuntimeError as e:
                print(f"Keeping old data for {label}: {e}", file=sys.stderr)
                failed.append(label)
                if label in previous_entries:
                    entries.append(previous_entries[label])
        return entries

    print("Fetching indices...", file=sys.stderr)
    indices = collect(
        (label, functools.partial(fetch_series_entry, label, symbol)) for label, symbol in INDICES
    )

    print("Fetching gold/silver/FX...", file=sys.stderr)
    commodities = collect(
        (label, functools.partial(fetch_commodity_entry, label, symbol))
        for label, symbol in [("Gold (INR/gram)", GOLD_SYMBOL), ("Silver (INR/gram)", SILVER_SYMBOL)]
    )

    currency = collect([("USD/INR", functools.partial(fetch_currency_entry, "USD/INR"))])

    print("Fetching stocks by group...", file=sys.stderr)
    stocks = {}
    for group, symbols, suffix in STOCK_GROUPS:
        stocks[group] = collect(
            (sym, functools.partial(fetch_series_entry, sym, sym + suffix)) for sym in symbols
        )

    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "generatedAt": now if refreshed else previous["generatedAt"],
        "pending": failed,
        "indices": indices,
        "commodities": commodities,
        "currency": currency,
        "stocks": stocks,
    }


def load_dataset(html_path=HTML_PATH):
    with open(html_path, "r", encoding="utf-8") as f:
        match = APP_DATA_RE.search(f.read())
    if not match:
        raise RuntimeError(f"no app-data script tag found in {html_path}")
    return json.loads(match.group(2))


def update_html(dataset, html_path=HTML_PATH):
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()

    new_json = json.dumps(dataset, separators=(",", ":"))
    new_content, n = APP_DATA_RE.subn(
        lambda m: m.group(1) + new_json + m.group(3), content
    )
    if n != 1:
        raise RuntimeError(f"expected exactly 1 app-data script tag, found {n}")

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(new_content)


def publish(html_path=HTML_PATH):
    """Commit html_path if it changed, then push. Also sends any commit an earlier failed push left behind."""
    if subprocess.run(["git", "diff", "--quiet", "HEAD", "--", html_path]).returncode != 0:
        subprocess.run(["git", "commit", "-m", "Update market data", "--", html_path], check=True)
    subprocess.run(["git", "push", "origin", "HEAD"], check=True, timeout=120)


def main():
    parser = argparse.ArgumentParser(description="Refresh the dataset embedded in markets.html.")
    parser.add_argument("--retry-pending", action="store_true", help="only fetch series a previous run couldn't")
    parser.add_argument("--push", action="store_true", help="commit markets.html and push it to the remote")
    args = parser.parse_args()

    previous = load_dataset()
    only = None
    if args.retry_pending:
        only = set(previous.get("pending", []))
        if not only:
            print("Nothing pending.", file=sys.stderr)
            if args.push:
                publish()
            return

    dataset = build_dataset(previous, only)
    update_html(dataset)
    print(f"Updated {HTML_PATH} (generatedAt {dataset['generatedAt']})", file=sys.stderr)
    if args.push:
        publish()

    if dataset["pending"]:
        print(f"Still pending: {', '.join(dataset['pending'])}", file=sys.stderr)
        if args.retry_pending:
            sys.exit(1)


if __name__ == "__main__":
    main()
