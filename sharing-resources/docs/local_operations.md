# Local Operations

## Setup

Install the local package from the project root:

```bash
python3 -m pip install -e .
```

Install optional X providers only when needed:

```bash
python3 -m pip install -e ".[social]"
```

## Secrets

Real credentials live in:

```bash
sharing-resources/secrets/market_sentiment.secrets.sh
```

Source them before running commands that need API keys or social/email credentials:

```bash
source sharing-resources/secrets/market_sentiment.secrets.sh
```

Do not paste secret values into docs, tests, references, or final answers.

## Daily Run

```bash
market-sentiment --config config/watchlist.toml preflight
market-sentiment --config config/watchlist.toml run-daily
```

For a specific date:

```bash
market-sentiment --config config/watchlist.toml run-daily --date 2026-03-26
```

## Reports

Daily outputs are under:

```text
data/reports/<date>/
```

Common outputs:

- `report.md`
- `report.json`
- `review_queue.md`
- `review_packets/*.json`
- `manual_agent_report.zh.md`

## Tests

Run all tests:

```bash
python3 -m pytest sharing-resources/tests
```

Run focused tests while editing:

```bash
python3 -m pytest sharing-resources/tests/test_config.py
python3 -m pytest sharing-resources/tests/test_pipeline.py
python3 -m pytest sharing-resources/tests/test_scoring.py
```
