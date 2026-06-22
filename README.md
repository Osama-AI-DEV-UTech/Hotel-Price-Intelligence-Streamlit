# UbidStay Price Intelligence — Streamlit Deployment

A self-contained, in-process build of the UbidStay Hotel Price Intelligence
platform, ready for deployment on **Streamlit Community Cloud**. The
dashboard no longer talks to a separate FastAPI server over HTTP — every
data operation runs as a direct, in-process Python function call against
the same business logic that previously sat behind `uvicorn`.

---

## 1. What changed

| Area | Before | After |
|---|---|---|
| Data access | `ui.py` called a FastAPI server over HTTP (`requests.get/post/delete`) | `ui.py` calls plain Python functions in `backend.py`, which invoke the same endpoint logic directly, in-process |
| Deployment | Required two running processes (FastAPI + Streamlit) | Single process — Streamlit only |
| Credentials | `.env` file | Streamlit **Secrets** (mapped to environment variables at startup) |
| Access control | None | Password gate (fail-closed — the app will not load without a configured password) |
| Debug output | A "Raw API response (JSON)" panel exposed the full raw response | Removed; the existing "Download full response (JSON)" export button is retained |

No other part of the dashboard — layout, styling, charts, tabs, copy — has
been modified. The visual product is identical to the original.

---

## 2. Project structure

```
.
├── ui.py                        # Streamlit dashboard (entry point)
├── backend.py                   # In-process bridge: calls the FastAPI endpoint
│                                 # functions directly — no HTTP, no localhost
├── app/                         # Original FastAPI service (unchanged)
│   ├── agents/                  # Orchestration, matching, timeline, AI recommendation
│   ├── api/v1/endpoints/        # Endpoint logic — now called directly by backend.py
│   ├── core/config/             # Settings (env-var driven)
│   ├── providers/                # Vendor adapters (SerpAPI, Booking.com, Expedia,
│   │                              # HotelBeds, Amadeus, Travelomatix)
│   ├── schemas/                 # Pydantic request/response models
│   └── services/                # SQLite history store, watchlist monitor, geocoding
├── requirements.txt
├── .streamlit/
│   └── secrets.toml.example     # Template — copy and fill in real values
└── .gitignore
```

---

## 3. Deploying to Streamlit Community Cloud

1. **Push this folder to a GitHub repository** (the full tree above,
   including `app/`).
2. Go to [share.streamlit.io](https://share.streamlit.io) → **New app**.
3. Select the repository and branch, and set:
   - **Main file path:** `ui.py`
4. Before (or right after) deploying, open **App settings → Secrets** and
   paste in your configuration — see [Section 4](#4-configuration--secrets)
   below. At minimum, `APP_PASSWORD` must be set, or the app will refuse to
   start.
5. Save. The app will restart automatically and prompt for the password on
   first load.

---

## 4. Configuration / Secrets

All configuration is supplied via Streamlit Secrets (or, for local
development, a `.streamlit/secrets.toml` file). Copy
`.streamlit/secrets.toml.example` as a starting point.

| Key | Required | Purpose |
|---|---|---|
| `APP_PASSWORD` | **Yes** | Gates access to the dashboard. The app will not load without this set. |
| `OPENAI_API_KEY` | No | Enables GPT-4o-generated booking recommendations. Without it, a deterministic, rule-based recommendation engine is used instead. |
| `SERPAPI_API_KEY` | No | Google Hotels via SerpAPI |
| `BOOKINGCOM_RAPIDAPI_KEY` | No | Booking.com via RapidAPI |
| `EXPEDIA_RAPIDAPI_KEY` | No | Expedia / Hotels.com via RapidAPI |
| `RAPIDAPI_KEY` | No | Shared fallback key for the two RapidAPI vendors above |
| `HOTELBEDS_API_KEY` / `HOTELBEDS_API_SECRET` | No | HotelBeds |
| `AMADEUS_CLIENT_ID` / `AMADEUS_CLIENT_SECRET` | No | Amadeus |
| `TRAVELOMATIX_USER_ID` / `TRAVELOMATIX_USER_PASSWORD` / `TRAVELOMATIX_ACCESS` | No | Travelomatix |
| `HISTORY_ENABLED` | No (default `true`) | Enables the SQLite price-history store |
| `TIMELINE_ENABLED` | No (default `true`) | Enables the live future-date price scan |

Any vendor left unconfigured is reported as `not_configured` in the
dashboard and skipped — there is no demo or mock data fallback anywhere in
this system, consistent with the original design.

---

## 5. Local development

```bash
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
# edit .streamlit/secrets.toml with real values

pip install -r requirements.txt
streamlit run ui.py
```

---

## 6. Known limitation — Watchlist auto-monitoring

The original FastAPI service ran a background `asyncio` task that
re-scanned active watches automatically on a fixed interval
(`MONITOR_INTERVAL_MINUTES`). Streamlit's execution model does not support
a persistent background process of this kind, so the **automatic**
schedule does not run in this build.

Manual scanning is fully supported and unaffected:
- **Run now** — scans a single watch on demand
- **Run ALL active watches now** — scans every active watch on demand

All resulting runs are still recorded to history and reflected in Vendor
Analytics exactly as before. If a truly scheduled scan is required, it
should be run as an external trigger (e.g. a scheduled GitHub Action or
cron job) against a small script that imports and calls
`backend.watchlist_run_all()`.

---

## 7. Security notes

- The password gate fails **closed**: if `APP_PASSWORD` is not set in
  Secrets, the app displays a configuration notice and refuses to render
  any content.
- Secrets are read once at process start and copied into the process
  environment; they are never displayed in the UI or written to disk.
- `.streamlit/secrets.toml` is excluded via `.gitignore` and must never be
  committed to version control.
