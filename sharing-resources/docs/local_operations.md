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
sharing-resources/secrets/equity_research.secrets.sh
```

Source them before running commands that need API keys or social/email credentials:

```bash
source sharing-resources/secrets/equity_research.secrets.sh
```

Do not paste secret values into docs, tests, references, or final answers.

## Common Commands

Validate configuration:

```bash
equity-research --config config/default.toml preflight
```

Run a deep review on one or more tickers:

```bash
equity-research --config config/default.toml review ZM NVDA AMZN
```

Run an ad-hoc review on a single ticker:

```bash
equity-research --config config/default.toml review-ticker IRTC --name "iRobot"
```

Place a valuation model order:

```bash
equity-research --config config/default.toml valuation-order TICKER --order order.json --portrait portrait.json
```

Check open decisions against fresh prices:

```bash
equity-research --config config/default.toml check-decisions
```

Show a saved report by date:

```bash
equity-research --config config/default.toml show-report --date 2026-08-17
```

Clean up old data by retention policy:

```bash
equity-research --config config/default.toml cleanup-data
```

Initialize the database (first run only):

```bash
equity-research --config config/default.toml init-db
```

## Reports

Daily outputs are under:

```text
data/reports/<date>/
```

Common outputs:

- `manual_agent_report.zh.md`
- `review_packets/<ticker>.json`

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
