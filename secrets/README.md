# Secrets

Put real local credentials here.

Tracked files in this folder must contain placeholders only. Actual secret files are ignored by `.gitignore`.

Default local secret file:

```bash
source secrets/market_sentiment.secrets.sh
```

Example template:

```bash
cp secrets/market_sentiment.secrets.example.sh secrets/market_sentiment.secrets.sh
chmod 600 secrets/market_sentiment.secrets.sh
```
