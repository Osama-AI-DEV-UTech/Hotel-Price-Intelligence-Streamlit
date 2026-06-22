"""
Price History Store — SQLite.
Every live search is snapshotted (market stats, per-vendor quotes,
cross-vendor comparisons, vendor health, timeline points). Over time this
builds REAL historical price data — the only honest way to analyse "previous
prices", since vendor APIs only quote current/future dates.

stdlib sqlite3 (WAL mode) + asyncio.to_thread — no extra dependencies.
"""
from __future__ import annotations

import asyncio
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import structlog

from app.core.config.settings import get_settings
from app.schemas.models import HotelSearchRequest, PriceComparisonResponse

logger = structlog.get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  search_id TEXT, ts TEXT, destination TEXT, destination_key TEXT,
  hotel_name TEXT, checkin TEXT, checkout TEXT, nights INTEGER,
  currency TEXT, total_hotels INTEGER,
  market_low REAL, market_avg REAL, market_high REAL,
  cheapest_vendor TEXT, cheapest_hotel TEXT
);
CREATE INDEX IF NOT EXISTS ix_snap_dest ON snapshots(destination_key, ts);

CREATE TABLE IF NOT EXISTS quotes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  snapshot_id INTEGER, ts TEXT, destination_key TEXT, checkin TEXT,
  vendor TEXT, hotel_id TEXT, hotel_name TEXT, stars INTEGER, rating REAL,
  price REAL, currency TEXT
);
CREATE INDEX IF NOT EXISTS ix_quotes_dest ON quotes(destination_key, ts);
CREATE INDEX IF NOT EXISTS ix_quotes_hotel ON quotes(hotel_name);

CREATE TABLE IF NOT EXISTS comparisons(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  snapshot_id INTEGER, ts TEXT, destination_key TEXT, hotel_name TEXT,
  vendors_count INTEGER, cheapest_vendor TEXT, cheapest_price REAL,
  most_expensive_vendor TEXT, most_expensive_price REAL, spread_pct REAL
);
CREATE INDEX IF NOT EXISTS ix_comp_dest ON comparisons(destination_key, ts);

CREATE TABLE IF NOT EXISTS vendor_health(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  snapshot_id INTEGER, ts TEXT, vendor TEXT, status TEXT,
  response_ms INTEGER, hotels INTEGER
);
CREATE INDEX IF NOT EXISTS ix_health_vendor ON vendor_health(vendor, ts);

CREATE TABLE IF NOT EXISTS timeline_points(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  snapshot_id INTEGER, ts TEXT, destination_key TEXT, label TEXT,
  offset_days INTEGER, checkin TEXT,
  min REAL, avg REAL, median REAL, max REAL, samples INTEGER
);

CREATE TABLE IF NOT EXISTS watchlist(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT, destination TEXT, hotel_name TEXT,
  checkin TEXT, checkout TEXT, adults INTEGER, rooms INTEGER,
  currency TEXT, target_price REAL, active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS watch_runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  watch_id INTEGER, ts TEXT, best_price REAL, best_vendor TEXT,
  best_hotel TEXT, market_avg REAL, hotels_found INTEGER,
  change_pct REAL, alert INTEGER DEFAULT 0, note TEXT
);
CREATE INDEX IF NOT EXISTS ix_runs_watch ON watch_runs(watch_id, ts);
"""


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def _dest_key(destination: str) -> str:
    return destination.strip().lower()


class HistoryStore:
    """Thread-safe SQLite store. All public async methods run in a thread."""

    def __init__(self, db_path: str) -> None:
        path = Path(db_path)
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[2] / path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        logger.info("history_store_ready", db=str(path))

    # ── low-level helpers ─────────────────────────────────────────────────────

    def _execute(self, sql: str, params: tuple = ()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur.lastrowid or 0

    def _query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    # ── Recording (called after every search — fire and forget) ──────────────

    def record_search(self, req: HotelSearchRequest, resp: PriceComparisonResponse) -> None:
        ts = _now()
        dk = _dest_key(req.destination)
        snap_id = self._execute(
            "INSERT INTO snapshots(search_id, ts, destination, destination_key, hotel_name,"
            " checkin, checkout, nights, currency, total_hotels, market_low, market_avg,"
            " market_high, cheapest_vendor, cheapest_hotel)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (resp.search_id, ts, req.destination, dk, req.hotel_name or "",
             resp.checkin, resp.checkout, resp.nights, resp.currency,
             resp.total_hotels_found, resp.market_lowest_price, resp.market_average_price,
             resp.market_highest_price, resp.cheapest_vendor_overall, resp.cheapest_hotel_overall),
        )
        with self._lock:
            for vr in resp.vendors:
                self._conn.execute(
                    "INSERT INTO vendor_health(snapshot_id, ts, vendor, status, response_ms, hotels)"
                    " VALUES(?,?,?,?,?,?)",
                    (snap_id, ts, vr.vendor, vr.search_status, vr.response_time_ms, vr.hotels_found),
                )
                for h in vr.hotels:
                    if h.lowest_rate:
                        self._conn.execute(
                            "INSERT INTO quotes(snapshot_id, ts, destination_key, checkin, vendor,"
                            " hotel_id, hotel_name, stars, rating, price, currency)"
                            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (snap_id, ts, dk, resp.checkin, vr.vendor, h.vendor_hotel_id,
                             h.name, h.stars, h.guest_rating, h.lowest_rate, resp.currency),
                        )
            for c in resp.comparisons:
                self._conn.execute(
                    "INSERT INTO comparisons(snapshot_id, ts, destination_key, hotel_name,"
                    " vendors_count, cheapest_vendor, cheapest_price, most_expensive_vendor,"
                    " most_expensive_price, spread_pct) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (snap_id, ts, dk, c.canonical_name, len(c.vendor_prices),
                     c.cheapest_vendor, c.cheapest_price, c.most_expensive_vendor,
                     c.most_expensive_price, c.price_difference_pct),
                )
            if resp.price_timeline:
                for p in resp.price_timeline.points:
                    if p.sample_size:
                        self._conn.execute(
                            "INSERT INTO timeline_points(snapshot_id, ts, destination_key, label,"
                            " offset_days, checkin, min, avg, median, max, samples)"
                            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (snap_id, ts, dk, p.label, p.offset_days, p.checkin,
                             p.min_price, p.avg_price, p.median_price, p.max_price, p.sample_size),
                        )
            self._conn.commit()
        logger.info("search_recorded", snapshot_id=snap_id, destination=dk)

    async def record_search_async(self, req: HotelSearchRequest,
                                  resp: PriceComparisonResponse) -> None:
        try:
            await asyncio.to_thread(self.record_search, req, resp)
        except Exception as exc:  # history must never break a search
            logger.error("history_record_failed", error=str(exc))

    # ── History queries ────────────────────────────────────────────────────────

    def destinations(self) -> list[dict[str, Any]]:
        return self._query(
            "SELECT destination_key AS destination, COUNT(*) AS snapshots,"
            " MIN(ts) AS first_seen, MAX(ts) AS last_seen,"
            " ROUND(AVG(market_avg),2) AS avg_market_price"
            " FROM snapshots WHERE total_hotels > 0"
            " GROUP BY destination_key ORDER BY snapshots DESC"
        )

    def trend(self, destination: str, days: int = 90) -> dict[str, Any]:
        dk = _dest_key(destination)
        since = (datetime.utcnow() - timedelta(days=days)).isoformat(timespec="seconds")
        market = self._query(
            "SELECT substr(ts,1,10) AS day, ROUND(AVG(market_avg),2) AS avg_price,"
            " ROUND(MIN(market_low),2) AS min_price, ROUND(MAX(market_high),2) AS max_price,"
            " COUNT(*) AS searches"
            " FROM snapshots WHERE destination_key=? AND ts>=? AND total_hotels>0"
            " GROUP BY day ORDER BY day",
            (dk, since),
        )
        by_vendor = self._query(
            "SELECT substr(ts,1,10) AS day, vendor, ROUND(AVG(price),2) AS avg_price,"
            " COUNT(*) AS quotes"
            " FROM quotes WHERE destination_key=? AND ts>=?"
            " GROUP BY day, vendor ORDER BY day",
            (dk, since),
        )
        return {"destination": dk, "days": days, "market": market, "by_vendor": by_vendor}

    def hotel_history(self, hotel_name: str, destination: str | None = None,
                      days: int = 180) -> list[dict[str, Any]]:
        since = (datetime.utcnow() - timedelta(days=days)).isoformat(timespec="seconds")
        sql = (
            "SELECT substr(ts,1,10) AS day, vendor, hotel_name,"
            " ROUND(AVG(price),2) AS avg_price, ROUND(MIN(price),2) AS min_price,"
            " COUNT(*) AS quotes"
            " FROM quotes WHERE hotel_name LIKE ? AND ts>=?"
        )
        params: list[Any] = [f"%{hotel_name}%", since]
        if destination:
            sql += " AND destination_key=?"
            params.append(_dest_key(destination))
        sql += " GROUP BY day, vendor, hotel_name ORDER BY day"
        return self._query(sql, tuple(params))

    # ── Vendor performance analytics ──────────────────────────────────────────

    def vendor_analytics(self, days: int = 90) -> list[dict[str, Any]]:
        since = (datetime.utcnow() - timedelta(days=days)).isoformat(timespec="seconds")
        health = self._query(
            "SELECT vendor, COUNT(*) AS searches,"
            " SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS successes,"
            " SUM(CASE WHEN status='api_error' THEN 1 ELSE 0 END) AS errors,"
            " ROUND(AVG(CASE WHEN status='success' THEN response_ms END),0) AS avg_response_ms,"
            " ROUND(AVG(CASE WHEN status='success' THEN hotels END),1) AS avg_hotels"
            " FROM vendor_health WHERE ts>=? GROUP BY vendor",
            (since,),
        )
        wins = {r["vendor"]: r for r in self._query(
            "SELECT cheapest_vendor AS vendor, COUNT(*) AS wins,"
            " ROUND(AVG(spread_pct),1) AS avg_spread_pct_when_winning,"
            " ROUND(AVG(most_expensive_price - cheapest_price),2) AS avg_saving_vs_worst"
            " FROM comparisons WHERE ts>=? AND vendors_count>1 GROUP BY cheapest_vendor",
            (since,),
        )}
        total_multi = self._query(
            "SELECT COUNT(*) AS n FROM comparisons WHERE ts>=? AND vendors_count>1", (since,),
        )[0]["n"]
        avg_price = {r["vendor"]: r["avg_quote"] for r in self._query(
            "SELECT vendor, ROUND(AVG(price),2) AS avg_quote FROM quotes WHERE ts>=? GROUP BY vendor",
            (since,),
        )}

        out = []
        for h in health:
            v = h["vendor"]
            w = wins.get(v, {})
            searches = h["searches"] or 0
            participated = searches - self._not_configured_count(v, since)
            out.append({
                "vendor": v,
                "searches": searches,
                "successes": h["successes"],
                "errors": h["errors"],
                "success_rate_pct": round(h["successes"] / participated * 100, 1)
                                    if participated > 0 else 0.0,
                "avg_response_ms": h["avg_response_ms"] or 0,
                "avg_hotels_returned": h["avg_hotels"] or 0,
                "avg_quote_price": avg_price.get(v),
                "wins_cheapest": w.get("wins", 0),
                "win_rate_pct": round(w.get("wins", 0) / total_multi * 100, 1)
                                if total_multi > 0 else 0.0,
                "avg_spread_pct_when_winning": w.get("avg_spread_pct_when_winning", 0.0),
                "avg_saving_vs_worst": w.get("avg_saving_vs_worst", 0.0),
            })
        out.sort(key=lambda r: (-r["wins_cheapest"], -r["success_rate_pct"]))
        return out

    def _not_configured_count(self, vendor: str, since: str) -> int:
        rows = self._query(
            "SELECT COUNT(*) AS n FROM vendor_health"
            " WHERE vendor=? AND ts>=? AND status='not_configured'",
            (vendor, since),
        )
        return rows[0]["n"] if rows else 0

    # ── Watchlist CRUD + runs ──────────────────────────────────────────────────

    def watch_create(self, **kw: Any) -> int:
        return self._execute(
            "INSERT INTO watchlist(created_at, destination, hotel_name, checkin, checkout,"
            " adults, rooms, currency, target_price, active) VALUES(?,?,?,?,?,?,?,?,?,1)",
            (_now(), kw["destination"], kw.get("hotel_name") or "", kw["checkin"], kw["checkout"],
             kw.get("adults", 2), kw.get("rooms", 1), kw.get("currency", "USD"),
             kw.get("target_price") or 0.0),
        )

    def watch_list(self) -> list[dict[str, Any]]:
        watches = self._query("SELECT * FROM watchlist ORDER BY id DESC")
        for w in watches:
            runs = self._query(
                "SELECT * FROM watch_runs WHERE watch_id=? ORDER BY ts DESC LIMIT 1", (w["id"],),
            )
            w["last_run"] = runs[0] if runs else None
        return watches

    def watch_get(self, watch_id: int) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM watchlist WHERE id=?", (watch_id,))
        return rows[0] if rows else None

    def watch_delete(self, watch_id: int) -> None:
        self._execute("DELETE FROM watchlist WHERE id=?", (watch_id,))
        self._execute("DELETE FROM watch_runs WHERE watch_id=?", (watch_id,))

    def watch_deactivate(self, watch_id: int, reason: str = "") -> None:
        self._execute("UPDATE watchlist SET active=0 WHERE id=?", (watch_id,))
        if reason:
            self._execute(
                "INSERT INTO watch_runs(watch_id, ts, best_price, best_vendor, best_hotel,"
                " market_avg, hotels_found, change_pct, alert, note)"
                " VALUES(?,?,0,'','',0,0,0,0,?)",
                (watch_id, _now(), reason),
            )

    def watch_last_run(self, watch_id: int) -> dict[str, Any] | None:
        rows = self._query(
            "SELECT * FROM watch_runs WHERE watch_id=? AND best_price>0"
            " ORDER BY ts DESC LIMIT 1", (watch_id,),
        )
        return rows[0] if rows else None

    def watch_record_run(self, watch_id: int, **kw: Any) -> int:
        return self._execute(
            "INSERT INTO watch_runs(watch_id, ts, best_price, best_vendor, best_hotel,"
            " market_avg, hotels_found, change_pct, alert, note)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (watch_id, _now(), kw.get("best_price", 0.0), kw.get("best_vendor", ""),
             kw.get("best_hotel", ""), kw.get("market_avg", 0.0), kw.get("hotels_found", 0),
             kw.get("change_pct", 0.0), int(kw.get("alert", False)), kw.get("note", "")),
        )

    def watch_runs(self, watch_id: int, limit: int = 200) -> list[dict[str, Any]]:
        return self._query(
            "SELECT * FROM watch_runs WHERE watch_id=? ORDER BY ts ASC LIMIT ?",
            (watch_id, limit),
        )

    def recent_alerts(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._query(
            "SELECT r.*, w.destination, w.hotel_name AS watch_hotel FROM watch_runs r"
            " JOIN watchlist w ON w.id = r.watch_id"
            " WHERE r.alert=1 ORDER BY r.ts DESC LIMIT ?", (limit,),
        )


# ── Singleton ──────────────────────────────────────────────────────────────────
_store: HistoryStore | None = None


def get_history_store() -> HistoryStore | None:
    """Returns the store, or None when HISTORY_ENABLED=false."""
    global _store
    s = get_settings()
    if not s.history.enabled:
        return None
    if _store is None:
        _store = HistoryStore(s.history.db_path)
    return _store
