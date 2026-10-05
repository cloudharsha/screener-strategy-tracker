# screener-strategy-tracker

Tracks which companies appear on custom [screener.in](https://www.screener.in) screens and
[Chartink](https://chartink.com) strategy scans, day by day.

A GitHub Action runs every weekday after market close, scrapes each screen you've
registered, and commits a dated snapshot. Git history then becomes the record of
which companies entered and exited each screen, and at what price.

## Layout

```
screeners/
  <slug>/
    config.yml       # the screen's name and URL
    2026-09-15.md    # one snapshot per run day
```

Each snapshot holds the full result table (every column the screen renders) plus
an **Added / Removed** diff against the previous snapshot, keyed on the company's
screener.in symbol so renames don't show up as false churn.

## Adding a screener

Create a folder under `screeners/` and drop in a `config.yml`:

```yaml
name: Promoter & Institutions Buying, Retail Exiting
url: https://www.screener.in/screens/3922203/promoter-institutions-buying-retail-exiting/
```

The script auto-discovers every `screeners/*/config.yml` — nothing else to register.

> **Use the plain screen URL.** A `?limit=` parameter makes screener.in redirect
> anonymous visitors to `/register/`. The script strips query params anyway, so
> pasting the URL straight from your browser is fine either way.

## Chartink strategies

A Chartink config combines one or more public Chartink screeners into a strategy
buy list:

```yaml
name: NKS + Pious Large-Cap — 5-day swing
source: chartink
scans:
  - tag: NKS
    url: https://chartink.com/screener/copy-nks-best-buy-stocks-for-intraday-2
  - tag: pious
    url: https://chartink.com/screener/pious-volume-generator
    intersect: https://chartink.com/screener/large-cap-stocks   # optional: keep only stocks also in this screener
strategy:
  hold_days: 5   # buy at next day's open (Day 1), sell at the close of Day 5
  slots: 10      # the buy list marks the top 10 by Close × Volume
```

The signals are the union of every scan. A stock found by more than one scan
lists all of its tags. Each snapshot shows the ranked buy list with Close,
% change, Volume, Close × Volume, and the buy and sell dates. Those dates skip
weekends and the NSE holidays in [`nse_holidays.yml`](nse_holidays.yml); add the
next year's holidays each December. The Added / Removed diff is keyed on the NSE
symbol.

Tracked strategies (rules and backtests are in the `strategies/` folders of
chartink-cli):

| Folder | Strategy | Role |
|---|---|---|
| `screeners/nks-best-buy` | NKS Best Buy, 5-day swing | Main |
| `screeners/nks-pious-largecap` | NKS + (Pious ∩ Large Cap), 5-day swing | Backup |

## Running locally

```bash
pip install -r requirements-dev.txt
pytest -q                                            # offline tests
python scripts/fetch_screeners.py --dry-run          # print, don't write
python scripts/fetch_screeners.py                    # write today's snapshots
python scripts/fetch_screeners.py --only <slug>      # just one screener
```

Snapshots are dated in IST (`Asia/Kolkata`) and re-running on the same day
overwrites that day's file.

## Schedule

`.github/workflows/screener-snapshot.yml` runs at 16:47 UTC (22:17 IST) Monday
through Friday, and can be triggered by hand via **Run workflow**. The tests run
first, and the snapshot is skipped if they fail. If one screen fails, the others
are still committed and the run is marked red.

`.github/workflows/tests.yml` runs the tests on every push to `main` and on pull requests.

> GitHub disables scheduled workflows on repos with no activity for 60 days. The
> daily snapshot commits count as activity, so this only matters if the schedule
> is already broken.
