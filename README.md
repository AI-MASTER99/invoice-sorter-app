# Invoice Sorter

AI-powered customs invoice processing for EU→UK imports.
Multi-tenant SaaS built on FastAPI + Claude + Supabase.

## Live

- App: https://app.invoice-sorter.com
- Landing: https://www.invoice-sorter.com

## Tech stack

- **Backend**: FastAPI, Python 3.12
- **AI**: Anthropic Claude (Sonnet 4.6) for extraction + sub-code matching
- **Database + Storage**: Supabase (PostgreSQL + object storage)
- **Hosting**: Render (free tier, EU region)
- **Frontend**: Vanilla HTML/CSS/JS — no framework

## Local development

```bash
cd invoiceflow
pip install -r requirements.txt

# Create .env with:
#   ANTHROPIC_API_KEY=...
#   SUPABASE_URL=...
#   SUPABASE_KEY=...
#   SECRET_KEY=...
#   APP_PASSWORD=...

uvicorn main:app --reload --port 8000
```

Open http://localhost:8000/login.

## Environment variables

| Var | Purpose |
|-----|---------|
| `ANTHROPIC_API_KEY` | Claude API key |
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_KEY` | Supabase `service_role` key |
| `SECRET_KEY` | Random string for session cookies |
| `APP_PASSWORD` | Default admin password on first run |
| `AI_MODEL` | Claude model (default: `claude-sonnet-4-6`) |
