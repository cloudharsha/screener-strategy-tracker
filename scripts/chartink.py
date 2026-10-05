"""Chartink screeners: fetch, combine into a strategy buy list, and render a snapshot.

A chartink config.yml looks like:

    name: NKS Best Buy — 5-day swing
    source: chartink
    scans:
      - tag: NKS
        url: https://chartink.com/screener/copy-nks-best-buy-stocks-for-intraday-2
      - tag: pious
        url: https://chartink.com/screener/pious-volume-generator
        intersect: https://chartink.com/screener/large-cap-stocks   # optional filter
    strategy:
      hold_days: 5    # buy at next open = Day 1, sell at close of Day N
      slots: 10       # buy list keeps the top N by Close x Volume

Signals = union of all scans (a scan with `intersect` keeps only stocks that are
also in that screener). A stock found by several scans gets every tag.
"""

from __future__ import annotations

import json
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import unquote, urlsplit

import requests
import yaml
from bs4 import BeautifulSoup

BASE = "https://chartink.com"
MAX_ROWS = 2000  # large-cap-stocks alone returns ~575 rows; the default 160 would truncate it
RETRIES = 3
HOLIDAYS_FILE = Path(__file__).resolve().parent.parent / "nse_holidays.yml"

# Company cell of a rendered chartink snapshot row: [SYMBOL](https://chartink.com/stocks/SYMBOL.html)
SNAPSHOT_STOCK_RE = re.compile(r"\[([^\]]+)\]\(https://chartink\.com/stocks/([^)]+)\.html\)")


class ChartinkError(Exception):
    """A chartink screener could not be fetched or parsed."""


# ---------- fetching ----------

def slug_of(url: str) -> str:
    parts = [p for p in urlsplit(url.strip()).path.split("/") if p]
    if len(parts) != 2 or parts[0] != "screener":
        raise ChartinkError(f"not a chartink screener URL: {url!r}")
    return parts[1]


def extract_clause(html: str) -> str:
    """The scan clause from a chartink screener page (current Vue page or legacy form)."""
    soup = BeautifulSoup(html, "html.parser")
    tag = soup.find("scanner")
    if tag is not None and tag.get(":scan-json"):
        clause = (json.loads(tag[":scan-json"]).get("atlas_query") or "").strip()
        if clause:
            return clause
    tag = soup.find(["input", "textarea"], {"name": "scan_clause"})
    if tag is not None:
        clause = (tag.get("value") or tag.string or "").strip()
        if clause:
            return clause
    raise ChartinkError("scan clause not found on page — private screener or layout changed?")


def _request(session: requests.Session, method: str, url: str, **kwargs) -> requests.Response:
    last: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            response = session.request(method, url, timeout=30, **kwargs)
            if response.status_code == 404:
                raise ChartinkError(f"{url} returned HTTP 404")
            response.raise_for_status()
            return response
        except ChartinkError:
            raise
        except Exception as exc:  # network hiccup, 5xx, 419 token expiry, timeout
            last = exc
            if attempt < RETRIES:
                time.sleep(2 * attempt)
    raise ChartinkError(f"failed to fetch {url}: {last}")


def run_screener(session: requests.Session, url: str) -> list[dict]:
    """Rows (nsecode, name, close, per_chg, volume) for a saved chartink screener."""
    page_url = f"{BASE}/screener/{slug_of(url)}"
    clause = extract_clause(_request(session, "GET", page_url).text)
    token = session.cookies.get("XSRF-TOKEN", domain="chartink.com") or session.cookies.get("XSRF-TOKEN")
    if not token:
        raise ChartinkError("chartink did not set an XSRF-TOKEN cookie")
    response = _request(
        session, "POST", f"{BASE}/screener/process",
        data={"max_rows": MAX_ROWS, "scan_clause": clause},
        headers={"x-xsrf-token": unquote(token), "x-requested-with": "XMLHttpRequest",
                 "referer": page_url, "origin": BASE},
    )
    payload = response.json()
    if "data" not in payload:
        raise ChartinkError(f"unexpected response from {page_url}: {str(payload)[:200]}")
    rows = payload["data"]
    if len(rows) >= MAX_ROWS:
        raise ChartinkError(f"{page_url} hit max_rows={MAX_ROWS}; results would be truncated")
    return rows


# ---------- combining ----------

def combine(scan_results: list[tuple[str, list[dict], set[str] | None]]) -> list[dict]:
    """Union of scans. Each item is (tag, rows, allowed nsecodes or None).
    Returns rows with a `tags` list, sorted by Close x Volume (highest first)."""
    merged: dict[str, dict] = {}
    for tag, rows, allowed in scan_results:
        for row in rows:
            code = row.get("nsecode") or str(row.get("bsecode") or "")
            if not code or (allowed is not None and code not in allowed):
                continue
            entry = merged.setdefault(code, {**row, "nsecode": code, "tags": []})
            if tag not in entry["tags"]:
                entry["tags"].append(tag)
    out = list(merged.values())
    for row in out:
        row["traded_value"] = float(row.get("close") or 0) * float(row.get("volume") or 0)
    out.sort(key=lambda r: -r["traded_value"])
    return out


def fetch_strategy(session: requests.Session, config: dict) -> list[dict]:
    results = []
    cache: dict[str, list[dict]] = {}

    def rows_for(url: str) -> list[dict]:
        key = slug_of(url)
        if key not in cache:
            cache[key] = run_screener(session, url)
        return cache[key]

    for scan in config["scans"]:
        allowed = None
        if scan.get("intersect"):
            allowed = {r.get("nsecode") for r in rows_for(scan["intersect"])}
        results.append((scan["tag"], rows_for(scan["url"]), allowed))
    return combine(results)


# ---------- trading calendar ----------

def load_holidays(path: Path = HOLIDAYS_FILE) -> set[date]:
    if not path.exists():
        return set()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {date.fromisoformat(str(d)) for d in data.get("holidays", [])}


def next_trading_day(day: date, holidays: set[date]) -> date:
    day += timedelta(days=1)
    while day.weekday() >= 5 or day in holidays:
        day += timedelta(days=1)
    return day


def entry_exit(signal_day: date, hold_days: int, holidays: set[date]) -> tuple[date, date]:
    """Entry = next trading day after the signal (Day 1); exit = close of Day `hold_days`."""
    entry = next_trading_day(signal_day, holidays)
    exit_day = entry
    for _ in range(hold_days - 1):
        exit_day = next_trading_day(exit_day, holidays)
    return entry, exit_day


# ---------- rendering ----------

def previous_snapshot(folder: Path, today: str) -> tuple[str, set[str]] | None:
    candidates = sorted(p for p in folder.glob("*.md")
                        if re.match(r"^\d{4}-\d{2}-\d{2}\.md$", p.name) and p.stem < today)
    if not candidates:
        return None
    latest = candidates[-1]
    return latest.stem, {key for _, key in SNAPSHOT_STOCK_RE.findall(latest.read_text(encoding="utf-8"))}


def _link(code: str) -> str:
    return f"[{code}](https://chartink.com/stocks/{code}.html)"


def _money(x: float) -> str:
    return f"{x:,.2f}"


def render(*, name: str, config: dict, date_str: str, fetched: datetime, rows: list[dict],
           previous: tuple[str, set[str]] | None, holidays: set[date]) -> str:
    strategy = config.get("strategy") or {}
    hold, slots = int(strategy.get("hold_days", 5)), int(strategy.get("slots", 10))
    entry, exit_day = entry_exit(date.fromisoformat(date_str), hold, holidays)

    lines = [f"# {name} — {date_str}", ""]
    for scan in config["scans"]:
        extra = f" ∩ {scan['intersect']}" if scan.get("intersect") else ""
        lines.append(f"- Scan `{scan['tag']}`: {scan['url']}{extra}")
    lines += [
        f"- Fetched: {fetched.strftime('%Y-%m-%d %H:%M')} IST",
        f"- Stocks: {len(rows)}",
        f"- Rule: buy at open {entry.isoformat()} (Day 1), sell at close {exit_day.isoformat()} (Day {hold}); "
        f"{slots} slots, highest Close × Volume first",
        "",
    ]

    if previous is None:
        lines += ["_No previous snapshot — baseline._", ""]
    else:
        prev_date, prev_keys = previous
        current = {r["nsecode"] for r in rows}
        added = [r["nsecode"] for r in rows if r["nsecode"] not in prev_keys]
        removed = sorted(prev_keys - current)
        lines += [f"## Added since {prev_date} ({len(added)})", ""]
        lines += [f"- {code}" for code in added] or ["_None._"]
        lines += ["", f"## Removed since {prev_date} ({len(removed)})", ""]
        lines += [f"- {code}" for code in removed] or ["_None._"]
        lines += [""]

    lines += [f"## Buy list (top {slots}, before removing stocks you already hold)", ""]
    if rows:
        lines += ["| # | Stock | Name | Scan | Close | % Chg | Volume | Close × Volume (₹ Cr) | Buy (open) | Sell (close) |",
                  "|---|---|---|---|---|---|---|---|---|---|"]
        for i, r in enumerate(rows, 1):
            buy = f"{entry.isoformat()}" if i <= slots else "— (no slot)"
            sell = f"{exit_day.isoformat()}" if i <= slots else "—"
            name_cell = str(r.get("name", "")).replace("|", "\\|")
            lines.append(
                f"| {i} | {_link(r['nsecode'])} | {name_cell} | {', '.join(r['tags'])} | "
                f"{_money(float(r.get('close') or 0))} | {float(r.get('per_chg') or 0):+.2f}% | "
                f"{int(r.get('volume') or 0):,} | {r['traded_value'] / 1e7:,.1f} | {buy} | {sell} |")
    else:
        lines.append("_No signals today — stay in cash._")
    lines.append("")
    return "\n".join(lines)


def snapshot(session: requests.Session, *, folder: Path, config: dict, today: str,
             now: datetime) -> tuple[str, int]:
    """Markdown for today's snapshot and the number of stocks in it."""
    rows = fetch_strategy(session, config)
    md = render(name=config["name"], config=config, date_str=today, fetched=now, rows=rows,
                previous=previous_snapshot(folder, today), holidays=load_holidays())
    return md, len(rows)
