# UbidStay — Hotel Price Intelligence Platform

A multi-vendor hotel price intelligence system built for Travelomatix /
Ubidtours. It queries multiple hotel suppliers live and in parallel for
the same destination and dates, then turns the raw results into a single
decision-ready view: who is cheapest right now, where prices are headed,
and when to buy.

There is no demo, mock, or synthetic data anywhere in the system. Every
number shown is the direct result of a live vendor API call made at
search time.

---

## 1. How It Works

### 1.1 Search Orchestration
A single search request is fanned out concurrently to every configured
vendor provider (SerpAPI, Booking.com, Expedia, HotelBeds, Amadeus,
Travelomatix). Each provider is a self-contained adapter responsible for
its own authentication (OAuth2 token caching, HMAC signing, or API key,
depending on the vendor), request shaping, and response parsing into a
common internal hotel/rate schema.

Each provider call resolves to exactly one of four outcomes, with no
exceptions:

| Outcome | Meaning |
|---|---|
| `not_configured` | No credentials supplied — vendor excluded from results |
| `success` | Live data returned |
| `no_results` | Live call succeeded but returned nothing for these dates |
| `api_error` | Live call failed — the real error is surfaced, not hidden |

All providers run concurrently (`asyncio.gather`), so one slow or failing
vendor never blocks or delays the others.

### 1.2 Cross-Vendor Hotel Matching
Once every vendor has responded, a matching engine identifies when the
*same physical hotel* appears across multiple suppliers under slightly
different names. Matching works in two passes:

1. **Name similarity** — hotel names are normalized (lowercased, common
   words like "hotel", "resort", "suites", "the", "&" stripped) and
   compared using a word-overlap (Jaccard-style) score. A score above a
   fixed threshold counts as a match.
2. **Geographic fallback** — if name similarity is borderline, latitude/
   longitude (when provided by the vendor) is checked with a Haversine
   distance calculation; two listings within ~300 metres and a weaker name
   match are still merged.

Matched hotels are grouped into a single comparison record showing every
vendor's price for that property side by side, with the cheapest and most
expensive vendor and the absolute/percentage spread between them.

### 1.3 Live Forward Price Scan ("Timeline")
Rather than applying a static seasonal pricing model, the system re-issues
live searches at shifted check-in dates — `+7, +14, +30, +60, +90` days
from the requested date, keeping stay length and party size constant. Only
the fastest, timeline-capable providers are used for this pass, run under
a concurrency limit.

For each date window, every vendor price returned is pooled and reduced to
`min / avg / median / max / sample_size`. From the resulting curve the
system derives:
- **Trend direction** — rising / falling / stable, based on the percentage
  difference between the requested window and the average of all future
  windows
- **Cheapest and most expensive scanned window**
- **Confidence score** — built from how many vendors and how many total
  price samples contributed to the curve (more data → higher confidence)
- **Booking advice** — a plain-language statement generated directly from
  the numbers above (e.g. recommending a date shift if a cheaper window
  exists)

### 1.4 AI Booking Recommendation
A decision (`BOOK_NOW`, `WAIT`, or `MONITOR`) is produced from the live
search results and the price timeline. Two engines are available:

- **LLM mode** (when an OpenAI key is configured): the live numbers —
  market low/avg/high, vendor spread, timeline trend — are passed to
  GPT-4o, which is constrained to reason only from the supplied figures
  and produce a structured decision, confidence score, and narrative.
- **Deterministic fallback** (always available, no external dependency):
  a fixed rule set scores the same inputs — price position versus market
  average, vendor spread size, and timeline trend — into the same
  `BOOK_NOW / WAIT / MONITOR` decision with a generated explanation.

Both paths consume the exact same live data; the deterministic engine
exists so a recommendation is never blocked by a missing or failed LLM
call.

### 1.5 Price History
Every completed search is persisted to a local SQLite store across four
tables: search snapshots, per-vendor health (status/latency/hotel count),
individual hotel quotes, and matched-hotel comparisons. This recorded
history is what powers the History and Vendor Analytics views — neither
screen calls a vendor; both are pure aggregations over past searches.

### 1.6 Watchlist & Monitoring
A watch stores a destination (optionally a specific hotel), target dates,
and an optional target price. Running a watch re-executes a live search
exactly as the main Search mode does, records the result to history, and
compares the new best price against the previous run to flag either a
significant price drop or the target price being met.

### 1.7 Vendor Performance Analytics
Computed entirely from the recorded history tables (no live calls):
- **Win rate** — of all multi-vendor hotel matches in the period, the
  percentage where this vendor had the lowest price
- **Success rate** — of all search attempts where this vendor was actually
  configured, the percentage that returned live data successfully
- **Average response time** — mean latency of successful calls only
- **Average saving vs. worst** — mean absolute price difference between
  this vendor (as winner) and the most expensive vendor, for the same
  matched hotel

---

## 2. Charts & What They Represent

### Search → Market Overview tab
| Chart | Type | Represents |
|---|---|---|
| **Hotels by Vendor** | Donut | Share of total hotels returned by each vendor for this specific search — shows which suppliers actually had inventory |
| **Price Range per Vendor** | Floating bar (min→max) | Each vendor's lowest-to-highest nightly rate across its own hotel list, with the average labelled — shows how wide or narrow each vendor's pricing is |
| **Star Rating Distribution** | Bar | Count of returned hotels per star rating, pooled across all vendors — shows the quality mix of available inventory |

### Search → Seasonal Analysis tab
| Chart | Type | Represents |
|---|---|---|
| **Forward Price Curve** | Color-coded bar | Live average price at each scanned date window (requested dates, +7, +14, +30, +60, +90 days). Green = cheapest scanned window, red = most expensive, amber = mid-range. The "▼ Your trip" marker shows where the requested dates sit on the curve |

### History mode
| Chart | Type | Represents |
|---|---|---|
| **Market Price Trend** | Line + shaded band | Day-by-day average market price for a destination, aggregated from every past recorded search. The shaded band is the min–max range recorded that day; the solid line is the average |
| **Per-Vendor Average Price Over Time** | Multi-line | Day-by-day average quoted price, one line per vendor, for the selected destination — shows how each supplier's pricing has actually moved over time |
| **Specific Hotel History** | Multi-line | Same per-vendor average price view, filtered to one named property — tracks a single hotel's price by vendor over time |

### Watchlist mode
| Chart | Type | Represents |
|---|---|---|
| **Watch Run History** | Line + markers | Best price recorded on every scheduled/manual run of a saved watch. Star markers highlight runs where a price-drop or target-price alert fired |

### Vendor Analytics mode
| Chart | Type | Represents |
|---|---|---|
| **Cheapest-vendor win rate** | Bar | % of multi-vendor hotel matches where each vendor had the lowest price |
| **Live-call success rate** | Bar | % of configured search attempts that returned live data successfully (errors excluded) |
| **Average response time** | Bar | Mean API latency in milliseconds for each vendor's successful calls |
| **Average saving vs. most expensive vendor** | Bar | Mean price gap (in currency) between each vendor, when it won, and the most expensive vendor for that same hotel |

---

## 3. Functional Summary

| Mode | Purpose |
|---|---|
| **Search** | Run a live multi-vendor price intelligence search |
| **History** | Review recorded price trends by destination or hotel |
| **Watchlist** | Manage saved watches and run on-demand scans |
| **Vendor Analytics** | Compare supplier performance over time |

| Search Result Tab | Content |
|---|---|
| **Vendor Results** | Each supplier's raw hotel list and rates |
| **Price Comparison** | Same-hotel, cross-vendor price matchup |
| **Seasonal Analysis** | Live forward price curve and booking windows |
| **AI Intelligence** | The booking recommendation and full reasoning |
| **Market Overview** | Aggregated pricing statistics across all vendors |

Search also supports an accommodation-type filter (hotel, apartment,
resort, vacation rental), budget cap, minimum star rating, minimum guest
rating, and search radius — usable for both leisure and religious
(Umrah/Hajj) travel itineraries.

Access to the dashboard is gated behind a password; without one
configured, the application will not load.

---

## 4. Tech Stack

- **Backend logic:** Python, FastAPI-style service layer, Pydantic models
- **Dashboard:** Streamlit, Plotly
- **Vendor integrations:** HTTPX-based adapters per supplier (OAuth2 / API
  key / RapidAPI as required by each vendor)
- **AI recommendation:** OpenAI (GPT-4o), with a deterministic rule-based
  fallback
- **Storage:** SQLite — price history, watchlist, and run records
