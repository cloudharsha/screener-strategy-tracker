"""Offline tests for scripts/chartink.py and the chartink branch of fetch_screeners.py."""

import json
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import chartink  # noqa: E402
import fetch_screeners  # noqa: E402

HOLIDAYS = chartink.load_holidays()
NKS = "https://chartink.com/screener/copy-nks-best-buy-stocks-for-intraday-2"
PIOUS = "https://chartink.com/screener/pious-volume-generator"
LARGE = "https://chartink.com/screener/large-cap-stocks"


def row(code, close, volume, per_chg=1.0):
    return {"nsecode": code, "name": f"{code} Ltd", "close": close, "volume": volume, "per_chg": per_chg}


# ---------- parsing ----------

def test_slug_of():
    assert chartink.slug_of(NKS) == "copy-nks-best-buy-stocks-for-intraday-2"
    assert chartink.slug_of(NKS + "/") == "copy-nks-best-buy-stocks-for-intraday-2"
    with pytest.raises(chartink.ChartinkError):
        chartink.slug_of("https://www.screener.in/screens/1/x/")


def test_extract_clause_vue_page():
    scan_json = json.dumps({"atlas_query": "( {cash} ( latest close > 10 ) )"}).replace('"', "&quot;")
    html = f'<html><scanner :scan-json="{scan_json}"></scanner></html>'
    assert chartink.extract_clause(html) == "( {cash} ( latest close > 10 ) )"


def test_extract_clause_legacy_form():
    html = '<form><textarea name="scan_clause">( {cash} ( latest volume > 1000 ) )</textarea></form>'
    assert chartink.extract_clause(html) == "( {cash} ( latest volume > 1000 ) )"


def test_extract_clause_missing():
    with pytest.raises(chartink.ChartinkError):
        chartink.extract_clause("<html><h1>Private Scan</h1></html>")


# ---------- combining ----------

def test_combine_union_tags_and_ranking():
    nks = [row("AAA", 100, 1_000), row("BBB", 10, 1_000_000)]
    pious = [row("BBB", 10, 1_000_000), row("CCC", 50, 5_000), row("SMALL", 5, 100)]
    out = chartink.combine([("NKS", nks, None), ("pious", pious, {"BBB", "CCC"})])
    assert [r["nsecode"] for r in out] == ["BBB", "CCC", "AAA"]  # SMALL filtered out by intersect
    assert out[0]["tags"] == ["NKS", "pious"]
    assert out[0]["traded_value"] == 10_000_000


def test_fetch_strategy_intersects_and_caches(monkeypatch):
    calls = []
    data = {
        "copy-nks-best-buy-stocks-for-intraday-2": [row("ETERNAL", 320.8, 30_000_000)],
        "pious-volume-generator": [row("TCS", 3000, 1_000_000), row("TINY", 20, 50_000)],
        "large-cap-stocks": [row("TCS", 3000, 1), row("ETERNAL", 320.8, 1)],
    }

    def fake_run(session, url):
        calls.append(chartink.slug_of(url))
        return data[chartink.slug_of(url)]

    monkeypatch.setattr(chartink, "run_screener", fake_run)
    config = {"scans": [{"tag": "NKS", "url": NKS}, {"tag": "pious", "url": PIOUS, "intersect": LARGE},
                        {"tag": "NKS2", "url": NKS}]}
    out = chartink.fetch_strategy(None, config)
    assert [r["nsecode"] for r in out] == ["ETERNAL", "TCS"]
    assert out[0]["tags"] == ["NKS", "NKS2"]
    assert calls.count("copy-nks-best-buy-stocks-for-intraday-2") == 1  # cached


# ---------- network layer (mocked) ----------

class FakeResponse:
    def __init__(self, text="", payload=None, status=200):
        self.text, self._payload, self.status_code = text, payload, status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, payload):
        import requests
        self.cookies = requests.cookies.RequestsCookieJar()
        self.cookies.set("XSRF-TOKEN", "abc%3D", domain="chartink.com")
        self.payload, self.posts = payload, []

    def request(self, method, url, **kwargs):
        if method == "GET":
            return FakeResponse(text='<textarea name="scan_clause">( {cash} ( latest close > 1 ) )</textarea>')
        self.posts.append(kwargs)
        return FakeResponse(payload=self.payload)


def test_run_screener_posts_clause_and_token():
    session = FakeSession({"data": [row("AAA", 1, 1)]})
    rows = chartink.run_screener(session, NKS)
    assert rows[0]["nsecode"] == "AAA"
    sent = session.posts[0]
    assert sent["data"]["scan_clause"] == "( {cash} ( latest close > 1 ) )"
    assert sent["headers"]["x-xsrf-token"] == "abc="  # URL-decoded


def test_run_screener_rejects_truncated_results():
    session = FakeSession({"data": [row(f"S{i}", 1, 1) for i in range(chartink.MAX_ROWS)]})
    with pytest.raises(chartink.ChartinkError, match="max_rows"):
        chartink.run_screener(session, LARGE)


def test_run_screener_rejects_unexpected_payload():
    with pytest.raises(chartink.ChartinkError, match="unexpected response"):
        chartink.run_screener(FakeSession({"error": "token mismatch"}), NKS)


# ---------- trading calendar ----------

def test_holidays_file_loads():
    assert date(2026, 10, 20) in HOLIDAYS  # Dussehra
    assert date(2026, 10, 2) in HOLIDAYS


@pytest.mark.parametrize("signal, entry, exit_day", [
    (date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 12)),   # Mon signal, weekend inside
    (date(2026, 10, 16), date(2026, 10, 19), date(2026, 10, 26)),  # Fri signal, Dussehra (Tue 20) skipped
    (date(2026, 10, 1), date(2026, 10, 5), date(2026, 10, 9)),    # Gandhi Jayanti (Fri 2) skipped for entry
])
def test_entry_exit(signal, entry, exit_day):
    assert chartink.entry_exit(signal, 5, HOLIDAYS) == (entry, exit_day)


# ---------- rendering ----------

CONFIG = {"name": "Test strategy", "scans": [{"tag": "NKS", "url": NKS}],
          "strategy": {"hold_days": 5, "slots": 2}}
NOW = datetime(2026, 10, 5, 22, 17)


def test_render_buy_list_respects_slots():
    rows = chartink.combine([("NKS", [row("A", 100, 3_000), row("B", 100, 2_000), row("C", 100, 1_000)], None)])
    md = chartink.render(name="Test strategy", config=CONFIG, date_str="2026-10-05", fetched=NOW,
                         rows=rows, previous=None, holidays=HOLIDAYS)
    assert "buy at open 2026-10-06 (Day 1), sell at close 2026-10-12 (Day 5)" in md
    assert "| 1 | [A](https://chartink.com/stocks/A.html) |" in md
    assert "| 3 | [C](https://chartink.com/stocks/C.html) | C Ltd | NKS | 100.00 | +1.00% | 1,000 | 0.0 | — (no slot) | — |" in md
    assert "baseline" in md


def test_render_no_signals():
    md = chartink.render(name="Test strategy", config=CONFIG, date_str="2026-10-05", fetched=NOW,
                         rows=[], previous=("2026-10-02", {"OLD"}), holidays=HOLIDAYS)
    assert "_No signals today — stay in cash._" in md
    assert "## Removed since 2026-10-02 (1)" in md


def test_snapshot_diff_round_trip(tmp_path):
    first = chartink.combine([("NKS", [row("A", 1, 1), row("B", 1, 1)], None)])
    (tmp_path / "2026-10-02.md").write_text(
        chartink.render(name="T", config=CONFIG, date_str="2026-10-02", fetched=NOW, rows=first,
                        previous=None, holidays=HOLIDAYS), encoding="utf-8")
    prev = chartink.previous_snapshot(tmp_path, "2026-10-05")
    assert prev == ("2026-10-02", {"A", "B"})

    second = chartink.combine([("NKS", [row("B", 1, 1), row("C", 1, 1)], None)])
    md = chartink.render(name="T", config=CONFIG, date_str="2026-10-05", fetched=NOW, rows=second,
                         previous=prev, holidays=HOLIDAYS)
    assert "## Added since 2026-10-02 (1)\n\n- C" in md
    assert "## Removed since 2026-10-02 (1)\n\n- A" in md


def test_previous_snapshot_ignores_today_and_other_files(tmp_path):
    (tmp_path / "config.yml").write_text("x", encoding="utf-8")
    (tmp_path / "2026-10-05.md").write_text("[X](https://chartink.com/stocks/X.html)", encoding="utf-8")
    assert chartink.previous_snapshot(tmp_path, "2026-10-05") is None


# ---------- repo configs ----------

CONFIGS = sorted(fetch_screeners.SCREENERS_DIR.glob("*/config.yml"))


@pytest.mark.parametrize("path", CONFIGS, ids=[p.parent.name for p in CONFIGS])
def test_repo_configs_are_valid(path):
    data = fetch_screeners.read_config(path)
    if data.get("source") == "chartink":
        cfg = fetch_screeners.check_chartink_config(path, data)
        for scan in cfg["scans"]:
            chartink.slug_of(scan["url"])
            if scan.get("intersect"):
                chartink.slug_of(scan["intersect"])
    else:
        fetch_screeners.load_config(path)


def test_strategy_configs_present():
    names = {p.parent.name for p in CONFIGS}
    assert {"nks-best-buy", "nks-pious-largecap"} <= names


def test_chartink_config_validation(tmp_path):
    path = fetch_screeners.REPO_ROOT / "screeners" / "_tmp" / "config.yml"
    with pytest.raises(fetch_screeners.ScreenerError):
        fetch_screeners.check_chartink_config(path, {"name": "x", "source": "chartink", "scans": []})
    with pytest.raises(fetch_screeners.ScreenerError):
        fetch_screeners.check_chartink_config(path, {"name": "x", "scans": [{"tag": "A"}]})
