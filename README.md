# UbidStay — Hotel Price Intelligence Platform

A multi-vendor hotel price intelligence system built for Travelomatix /
Ubidtours. It searches multiple hotel suppliers live, in parallel, and
turns the results into a single, decision-ready view: who is cheapest
right now, where prices are headed, and when to buy.

There is no demo, mock, or synthetic data anywhere in the system — every
number shown comes from a live vendor API call made at search time.

---

## Core Functionality

### 1. Multi-Vendor Live Search
Queries the following suppliers simultaneously for the same destination
and dates:
- SerpAPI (Google Hotels)
- Booking.com
- Expedia / Hotels.com
- HotelBeds
- Amadeus
- Travelomatix

Each vendor's full hotel list and rates are shown independently. A vendor
without valid credentials is clearly reported as **not configured** rather
than silently skipped or faked.

### 2. Cross-Vendor Price Comparison
The same physical property is matched across vendors by name/address
similarity, surfacing:
- Cheapest vendor for that hotel
- Most expensive vendor for that hotel
- Price spread (absolute and percentage)

### 3. Live Price Timeline (Forward Prediction)
Re-queries the fastest vendors at future check-in windows
(+7 / +14 / +30 / +60 / +90 days) to build a real, live forward price
curve — not a static seasonal model. From this the system derives:
- Overall trend (rising / falling / stable)
- The cheapest scanned window and potential saving vs. the requested dates

### 4. AI Booking Recommendation
Produces a clear **BOOK_NOW / WAIT / MONITOR** decision with supporting
analysis, confidence score, best vendor, and timing guidance.
- Uses GPT-4o for the narrative when an OpenAI key is configured
- Falls back to a deterministic, rule-based engine (using the same live
  numbers) when it isn't — the recommendation logic never invents data

### 5. Price History
Every search is recorded to a local store. From this, the platform shows:
- Market price trend per destination over time
- Per-vendor average price trend
- Price history for a specific named hotel

### 6. Watchlist & Monitoring
Save a destination (or a specific hotel) with target dates and an optional
target price. The system can re-scan any saved watch on demand and will
flag:
- A price drop beyond a configured threshold
- The target price being reached

### 7. Vendor Performance Analytics
Computed entirely from accumulated search history:
- Win rate — how often each vendor is the cheapest for a matched property
- Success rate — live calls that actually returned data
- Average response time and average hotels returned
- Average saving versus the most expensive vendor for the same hotel

### 8. Access Control
The dashboard is gated behind a password. Without a configured password,
the application does not load — there is no default "open" state.

### 9. Umrah / Hajj & General Travel Package Support
Search supports a generic accommodation type filter (hotel, apartment,
resort, vacation rental), budget cap, minimum star rating, minimum guest
rating, and search radius — applicable to both leisure and religious
travel itineraries.

---

## Search Modes

| Mode | Purpose |
|---|---|
| **Search** | Run a live multi-vendor price intelligence search |
| **History** | Review recorded price trends by destination or hotel |
| **Watchlist** | Manage saved watches and run on-demand scans |
| **Vendor Analytics** | Compare supplier performance over time |

## Search Result Views

| Tab | Content |
|---|---|
| **Vendor Results** | Each supplier's raw hotel list and rates |
| **Price Comparison** | Same-hotel, cross-vendor price matchup |
| **Seasonal Analysis** | Live forward price curve and booking windows |
| **AI Intelligence** | The booking recommendation and full reasoning |
| **Market Overview** | Aggregated pricing statistics across all vendors |

---

## Tech Stack

- **Backend logic:** Python, FastAPI-style service layer, Pydantic
- **Dashboard:** Streamlit
- **Vendor integrations:** HTTPX-based adapters per supplier
  (OAuth2 / API key / RapidAPI as required by each vendor)
- **AI recommendation:** OpenAI (GPT-4o), with a deterministic fallback
- **Storage:** SQLite — price history, watchlist, and run records
- **Charts:** Plotly

---

## Reliability Contract

Every vendor call follows the same strict rule, with no exceptions:

| Outcome | Behaviour |
|---|---|
| Credentials missing | Reported as `not_configured` — vendor excluded |
| Live call succeeds | Real data returned |
| Live call returns nothing | Reported as `no_results` |
| Live call fails | Reported as `api_error` with the real error message |

There is no fallback to demo or placeholder data at any stage.
