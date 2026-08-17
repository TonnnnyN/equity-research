# Runtime And Shared Scripts

This reference covers shared project commands. Skill-specific scripts are documented inside each Skill:

- `skills/equity-research/references/script_reference.md`

## CLI Entry Point

The Python package exposes:

```bash
equity-research
```

Core commands:

```bash
equity-research --config config/default.toml init-db
equity-research --config config/default.toml preflight
equity-research --config config/default.toml review ZM NVDA AMZN
equity-research --config config/default.toml show-report --date YYYY-MM-DD
equity-research --config config/default.toml cleanup-data --date YYYY-MM-DD
```

Use `preflight` before diagnosing provider failures. It distinguishes missing API keys, X account/cookie setup, options configuration, and optional provider coverage.

## `sharing-resources/scripts/redact_sensitive_json.py`

Purpose: remove sensitive values from generated JSON files before archiving, sharing, or debugging.

Command:

```bash
python3 sharing-resources/scripts/redact_sensitive_json.py data
```

Dry run:

```bash
python3 sharing-resources/scripts/redact_sensitive_json.py --dry-run data
```

Redacted keys include `api_key`, `apikey`, `token`, `secret`, `client_secret`, `password`, `email_password`, and `authorization`. URL query strings with those keys are also rewritten.
