# screener-strategy-tracker

Tracks which companies appear on custom [screener.in](https://www.screener.in) screens, day by day.

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

## Running locally

```bash
pip install -r requirements.txt
python scripts/fetch_screeners.py --dry-run          # print, don't write
python scripts/fetch_screeners.py                    # write today's snapshots
python scripts/fetch_screeners.py --only <slug>      # just one screener
```

Snapshots are dated in IST (`Asia/Kolkata`) and re-running on the same day
overwrites that day's file.

## Schedule

`.github/workflows/screener-snapshot.yml` runs at 16:30 UTC (22:00 IST) Monday
through Friday, and can be triggered by hand via **Run workflow**. If one screen
fails the others are still committed, and the run is marked red.

> GitHub disables scheduled workflows on repos with no activity for 60 days. The
> daily snapshot commits count as activity, so this only matters if the schedule
> is already broken.
