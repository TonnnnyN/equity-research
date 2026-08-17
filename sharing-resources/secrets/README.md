# Secrets

Put real local credentials here.

Tracked files in this folder must contain placeholders only. Actual secret files are ignored by `.gitignore`.

Default local secret file:

```bash
source sharing-resources/secrets/equity_research.secrets.sh
```

Example template:

```bash
cp sharing-resources/secrets/equity_research.secrets.example.sh sharing-resources/secrets/equity_research.secrets.sh
chmod 600 sharing-resources/secrets/equity_research.secrets.sh
```
