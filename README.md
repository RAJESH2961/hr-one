# HROne – Employee Attendance & Analytics API

FastAPI + MongoDB (6.0+). All code is in `app/main.py`.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # MONGO_URI / MONGO_DB
python sample_seed.py       # optional: nine sample documents
uvicorn app.main:app --port 8000
```

Indexes are created automatically at startup. Every endpoint in `openapi.yaml` is implemented, including
`/admin/explain/{endpoint}`. See `REVIEW.md` for the starter defects and `DECISIONS.md` for design choices.

Nothing is missing.
