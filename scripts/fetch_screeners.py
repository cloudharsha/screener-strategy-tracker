#!/usr/bin/env python3
"""Snapshot screener.in screens into dated markdown files.

Each folder under screeners/ holds a config.yml (name + url). For every one of
them this script scrapes the screen and writes screeners/<slug>/<YYYY-MM-DD>.md
containing the full result table plus a diff against the previous snapshot.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import requests
import yaml
from bs4 import BeautifulSoup

REPO_ROOT = Path(__file__).resolve().parent.parent
SCREENERS_DIR = REPO_ROOT / "screeners"
BASE = "https://www.screener.in"
IST = ZoneInfo("Asia/Kolkata")

# screener.in redirects the default python/curl user agent to /register/.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

MAX_PAGES = 40
REQUEST_DELAY = 1.5
RETRIES = 3

DATE_FILE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")
# Matches the company cell of a rendered snapshot row: [Name](https://.../company/KEY/)
SNAPSHOT_COMPANY_RE = re.compile(r"\[([^\]]+)\]\(\S*?/company/([^/)]+)/?\)")


class ScreenerError(Exception):
    """A screener could not be fetched or parsed."""


def squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def normalize_url(url: str) -> str:
    """Drop query params: `?limit=` makes screener.in redirect to /register/."""
    parts = urlsplit(url.strip())
    if not parts.scheme or not parts.netloc:
        raise ScreenerError(f"not an absolute URL: {url!r}")
    path = parts.path if parts.path.endswith("/") else parts.path + "/"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def fetch_page(session: requests.Session, url: str, page: int) -> BeautifulSoup:
    target = url if page == 1 else f"{url}?page={page}"
    last_error: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            response = session.get(target, timeout=30, allow_redirects=True)
            if re.search(r"/(register|login)/", urlsplit(response.url).path):
                raise ScreenerError(
                    f"{target} redirected to {response.url} — the screen is not public"
                )
            if 400 <= response.status_code < 500:
                raise ScreenerError(f"{target} returned HTTP {response.status_code}")
            response.raise_for_status()
            return BeautifulSoup(response.text, "html.parser")
        except ScreenerError:
            raise
        except Exception as exc:  # network hiccup, 5xx, timeout
            last_error = exc
            if attempt < RETRIES:
                time.sleep(2 * attempt)
    raise ScreenerError(f"failed to fetch {target}: {last_error}")


def parse_table(soup: BeautifulSoup) -> tuple[list[str], list[dict[str, str]]]:
    """Return (column names, rows). Rows keep a `_key` and `_name` for diffing."""
    table = soup.select_one("table.data-table")
    if table is None:
        raise ScreenerError("no table.data-table found — page layout changed?")

    header_row = table.select_one("thead tr") or table.find("tr")
    columns = [squash(th.get_text()) for th in header_row.find_all("th")]
    if not columns:
        raise ScreenerError("could not read column headers")

    rows: list[dict[str, str]] = []
    for tr in table.find_all("tr"):
        link = tr.select_one('a[href^="/company/"]')
        if link is None:
            continue  # repeated sticky header row, or the trailing "Median" row
        cells = [squash(td.get_text()) for td in tr.find_all("td")]
        if not cells:
            continue
        row = dict(zip(columns, cells))
        # /company/NINSYS/consolidated/ -> NINSYS (BSE-only names give a numeric code)
        row["_key"] = link["href"].strip("/").split("/")[1]
        row["_name"] = squash(link.get_text())
        row["_url"] = urljoin(BASE, f"/company/{row['_key']}/")
        rows.append(row)
    return columns, rows


def total_results(soup: BeautifulSoup) -> int | None:
    match = re.search(r"([\d,]+)\s+results?\b", soup.get_text(), re.IGNORECASE)
    return int(match.group(1).replace(",", "")) if match else None


def scrape(session: requests.Session, url: str) -> tuple[str, list[str], list[dict[str, str]]]:
    columns: list[str] = []
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    title = ""
    expected: int | None = None

    for page in range(1, MAX_PAGES + 1):
        if page > 1:
            time.sleep(REQUEST_DELAY)
        soup = fetch_page(session, url, page)
        if page == 1:
            heading = soup.find("h1")
            title = squash(heading.get_text()) if heading else ""
            expected = total_results(soup)
        page_columns, page_rows = parse_table(soup)
        if not page_rows:
            break
        if not columns:
            columns = page_columns
        fresh = [row for row in page_rows if row["_key"] not in seen]
        if not fresh:
            break  # screener.in echoes the last page for out-of-range page numbers
        seen.update(row["_key"] for row in fresh)
        rows.extend(fresh)
        if expected is not None and len(rows) >= expected:
            break
    else:
        raise ScreenerError(f"stopped after {MAX_PAGES} pages — pagination looks broken")

    if not rows:
        raise ScreenerError("screen returned no companies")
    if expected is not None and len(rows) != expected:
        print(
            f"    warning: page says {expected} results but scraped {len(rows)}",
            file=sys.stderr,
        )
    return title, columns, rows


def previous_snapshot(folder: Path, today: str) -> tuple[str, dict[str, str]] | None:
    """Most recent dated snapshot before `today`, as (date, {key: name})."""
    candidates = sorted(
        path
        for path in folder.glob("*.md")
        if DATE_FILE_RE.match(path.name) and path.stem < today
    )
    if not candidates:
        return None
    latest = candidates[-1]
    names = {
        key: name for name, key in SNAPSHOT_COMPANY_RE.findall(latest.read_text(encoding="utf-8"))
    }
    return latest.stem, names


def cell(value: str) -> str:
    return value.replace("|", "\\|") or "—"


def render(
    *,
    title: str,
    url: str,
    date: str,
    fetched: datetime,
    columns: list[str],
    rows: list[dict[str, str]],
    previous: tuple[str, dict[str, str]] | None,
) -> str:
    lines = [
        f"# {title} — {date}",
        "",
        f"- Source: {url}",
        f"- Fetched: {fetched.strftime('%Y-%m-%d %H:%M')} IST",
        f"- Companies: {len(rows)}",
        "",
    ]

    if previous is None:
        lines += ["_No previous snapshot — baseline._", ""]
    else:
        prev_date, prev_names = previous
        current = {row["_key"]: row["_name"] for row in rows}
        added = [(key, name) for key, name in current.items() if key not in prev_names]
        removed = [(key, name) for key, name in prev_names.items() if key not in current]

        lines += [f"## Added since {prev_date} ({len(added)})", ""]
        lines += [f"- {name} ({key})" for key, name in added] or ["_None._"]
        lines += ["", f"## Removed since {prev_date} ({len(removed)})", ""]
        lines += [f"- {name} ({key})" for key, name in removed] or ["_None._"]
        lines += [""]

    lines += ["## Holdings", ""]
    lines.append("| " + " | ".join(cell(column) for column in columns) + " |")
    lines.append("|" + "|".join("---" for _ in columns) + "|")
    for row in rows:
        cells = []
        for index, column in enumerate(columns):
            value = row.get(column, "")
            # Link the company column (index 1 on every screen) back to screener.in.
            if index == 1 and squash(value) == row["_name"]:
                cells.append(f"[{cell(value)}]({row['_url']})")
            else:
                cells.append(cell(value))
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    return "\n".join(lines)


def load_config(path: Path) -> tuple[str, str]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    name, url = data.get("name"), data.get("url")
    if not name or not url:
        raise ScreenerError(f"{path.relative_to(REPO_ROOT)} needs both `name` and `url`")
    return str(name), normalize_url(str(url))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="print the markdown instead of writing files"
    )
    parser.add_argument(
        "--only", metavar="SLUG", help="process a single screener folder by name"
    )
    args = parser.parse_args()

    configs = sorted(SCREENERS_DIR.glob("*/config.yml"))
    if args.only:
        configs = [path for path in configs if path.parent.name == args.only]
    if not configs:
        print("No screeners found under screeners/*/config.yml", file=sys.stderr)
        return 1

    now = datetime.now(IST)
    today = now.strftime("%Y-%m-%d")
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})

    failures: list[str] = []
    for config_path in configs:
        folder = config_path.parent
        slug = folder.name
        print(f"==> {slug}")
        try:
            name, url = load_config(config_path)
            title, columns, rows = scrape(session, url)
            markdown = render(
                title=title or name,
                url=url,
                date=today,
                fetched=now,
                columns=columns,
                rows=rows,
                previous=previous_snapshot(folder, today),
            )
            if args.dry_run:
                print(markdown)
            else:
                out = folder / f"{today}.md"
                out.write_text(markdown, encoding="utf-8")
                print(f"    wrote {out.relative_to(REPO_ROOT)} ({len(rows)} companies)")
        except Exception as exc:
            print(f"    FAILED: {exc}", file=sys.stderr)
            failures.append(slug)

    if failures:
        print(f"\n{len(failures)} screener(s) failed: {', '.join(failures)}", file=sys.stderr)
        return 1
    print(f"\nDone: {len(configs)} screener(s) for {today}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
