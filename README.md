# zubyria-pricing

Pricing engine + quote page for the Zubyria properties (Tseglina, Modryna, Zharyna).

- `pricing_engine.py` — rules: rates, Ukrainian holiday multipliers, min-stay, jacuzzi, fees, Anya's 20% bonus
- `lambda_function.py` — Lambda handler: HTML quote form at `/`, JSON API at `/quote.json`
- Deploys to Lambda `zubyria-pricing` (us-east-1) on push to `main` via GitHub Actions

Pricing rules mirror the Google Sheet "Zubyria Pricing Rules v0.2" (source of truth for edits).
