# Configuration And Secrets

## Runtime Config

The CLI reads local runtime configuration from:

```text
config/default.toml
```

This is the live runtime config used by the CLI. Update it when configuration changes need to persist across runs.

## Main Environment Variables

Data and config:

- `MARKET_SENTIMENT_CONFIG`
- `MARKET_SENTIMENT_DATA_DIR`
- `MARKET_SENTIMENT_DB_PATH`
- `SEC_USER_AGENT`

Market data:

- `ALPHAVANTAGE_API_KEY`
- `FRED_API_KEY`
- `EIA_API_KEY`

Email:

- `MARKET_SENTIMENT_SMTP_HOST`
- `MARKET_SENTIMENT_SMTP_PORT`
- `MARKET_SENTIMENT_SMTP_USERNAME`
- `MARKET_SENTIMENT_SMTP_PASSWORD`
- `MARKET_SENTIMENT_EMAIL_FROM`
- `MARKET_SENTIMENT_REPORT_EMAIL_TO`
- `MARKET_SENTIMENT_SMTP_USE_SSL`
- `MARKET_SENTIMENT_SMTP_USE_STARTTLS`
- `MARKET_SENTIMENT_SMTP_TIMEOUT_SECONDS`
- `MARKET_SENTIMENT_EMAIL_SUBJECT_PREFIX`

Social and forum:

- `SOCIAL_ENABLED`
- `REDDIT_ENABLED`
- `REDDIT_SUBREDDITS`
- `REDDIT_CLIENT_ID`
- `REDDIT_CLIENT_SECRET`
- `FORUM_ENABLED`
- `FORUM_BASE_URLS`
- `FORUM_API_KEY`
- `FORUM_API_USERNAME`

X providers:

- `X_ENABLED`
- `X_PROVIDER`
- `X_SEARCH_PRODUCT`
- `X_MAX_POSTS`
- `X_DB_PATH`
- `X_ACCOUNTS_FILE`
- `X_ACCOUNTS_LINE_FORMAT`
- `X_USERNAME`
- `X_EMAIL`
- `X_PASSWORD`
- `X_EMAIL_PASSWORD`
- `X_COOKIES_PATH`
- `X_PROXY_URL`

Options:

- `OPTIONS_ENABLED`
- `OPTIONS_PROVIDER`
- `OPTIONS_REQUIRE_GREEKS`
- `OPTIONS_MAX_CONTRACTS`

## Local Secret File

Actual local keys and credentials belong in:

```text
sharing-resources/secrets/equity_research.secrets.sh
```

Load it with:

```bash
source sharing-resources/secrets/equity_research.secrets.sh
```

Keep placeholder examples in:

```text
sharing-resources/secrets/equity_research.secrets.example.sh
```

## Git Hygiene

`.gitignore` should ignore:

- `sharing-resources/secrets/*`
- `secrets/*`
- `.env`
- `.env.*`
- `data/`
- `.pytest_cache/`
- `__pycache__/`
- `*.sqlite3`
- `*.db`
- `.DS_Store`

Exceptions are allowed only for non-secret documentation and examples:

- `sharing-resources/secrets/README.md`
- `sharing-resources/secrets/*.example.sh`
- `secrets/README.md`
- `secrets/*.example.sh`
- `.env.example`

## Handling Rule

When reporting on secrets, mention only:

- file path
- variable name
- purpose
- whether the file is ignored

Never print the actual value.
