# Data And Outputs

## Generated Data

The `data/` folder is generated runtime state. It can be large and should not be treated as source material for the skill unless the task is to inspect a historical run.

Do not commit `data/`. Some raw provider payloads and rerun artifacts can contain request metadata or query-parameter fields from upstream APIs.

Main folders:

- `data/raw/`: raw provider payloads by run date
- `data/reports/`: generated daily reports and review packets
- `data/state/`: SQLite state, cookies, provider DBs, and other local state
- `data/reruns/`: manual rerun artifacts

## Daily Report Folder

Expected shape:

```text
data/reports/YYYY-MM-DD/
  report.md
  report.json
  review_queue.md
  review_packets/
  manual_agent_report.zh.md
```

## How To Use Outputs

For quick inspection, start with `manual_agent_report.zh.md` when it exists. It is designed for human and Agent review in Chinese.

For structured processing, use `report.json` and `review_packets/*.json`.

For audit trail or source debugging, inspect `data/raw/<date>/` and storage records.

## Retention

Retention is configured in `config/watchlist.toml` under `[retention]`.

The cleanup command is:

```bash
market-sentiment --config config/watchlist.toml cleanup-data --date YYYY-MM-DD
```

Cleanup should preserve the reference date's current report and avoid deleting the latest output unexpectedly.
