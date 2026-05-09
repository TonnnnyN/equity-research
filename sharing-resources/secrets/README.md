# Secrets

Put real local credentials here.

Tracked files in this folder must contain placeholders only. Actual secret files are ignored by `.gitignore`.

Default local secret file:

```bash
source sharing-resources/secrets/market_sentiment.secrets.sh
```

Example template:

```bash
cp sharing-resources/secrets/market_sentiment.secrets.example.sh sharing-resources/secrets/market_sentiment.secrets.sh
chmod 600 sharing-resources/secrets/market_sentiment.secrets.sh
```
