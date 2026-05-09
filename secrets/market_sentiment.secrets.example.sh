#!/usr/bin/env bash

# Copy this file to secrets/market_sentiment.secrets.sh and fill local values.
# Do not commit the filled file.

export ALPHAVANTAGE_API_KEY=""
export FRED_API_KEY=""
export EIA_API_KEY=""
export SEC_USER_AGENT="market-sentiment/0.1 (local research use; contact@example.com)"

export SOCIAL_ENABLED="false"
export X_ENABLED="false"
export X_PROVIDER="twscrape,twikit"
export X_SEARCH_PRODUCT="Latest"
export X_MAX_POSTS="50"
export X_DB_PATH="data/state/twscrape_accounts.db"
export X_COOKIES_PATH="data/state/x_cookies.json"

export OPTIONS_ENABLED="false"
export OPTIONS_PROVIDER="alpha_vantage"
export OPTIONS_MAX_CONTRACTS="80"

# Optional X account bootstrap.
# export X_ACCOUNTS_FILE="secrets/x_accounts.txt"
# export X_ACCOUNTS_LINE_FORMAT="username:password:email:email_password:_:cookies"

# Optional twikit login.
# export X_USERNAME=""
# export X_EMAIL=""
# export X_PASSWORD=""
# export X_EMAIL_PASSWORD=""
