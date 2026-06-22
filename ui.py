"""
UbidStay Price Intelligence — Streamlit Dashboard
Run: streamlit run ui.py

Runs fully in-process — no separate FastAPI server, no HTTP calls from the
UI. Every value rendered comes straight from the backend functions in
backend.py (which call the same orchestrator/services that used to sit
behind uvicorn), which themselves only ever return live vendor data.
Vendor names, colors, prices, charts, seasonal windows — all derived from
the function-call result at runtime.

Modes: Search · History · Watchlist · Vendor Analytics
Search tabs: Vendor Results · Price Comparison · Seasonal Analysis ·
             AI Intelligence · Market Overview
"""
from __future__ import annotations

import json
import os
import re
import statistics
import sys
from datetime import date, timedelta
from pathlib import Path

import plotly.graph_objects as go
import streamlit as st

# ── Page config ────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="UbidStay · Price Intelligence",
    page_icon="🏨",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Make the bundled backend importable + push Streamlit Secrets into the
#    process environment BEFORE the backend (and the settings it loads) is
#    imported. This is what lets API keys be configured via the Streamlit
#    Cloud "Secrets" panel instead of a .env file. ─────────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _hydrate_env_from_secrets() -> None:
    try:
        raw = dict(st.secrets)
    except Exception:
        return

    def _flatten(d: dict) -> None:
        for k, v in d.items():
            if hasattr(v, "items"):          # a [section] table in secrets.toml
                _flatten(dict(v))
            else:
                os.environ.setdefault(str(k), str(v))

    _flatten(raw)


_hydrate_env_from_secrets()

import backend  # noqa: E402  — local module; must be imported after env hydration

# ── CSS (visual theme only — no data) ─────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=Plus+Jakarta+Sans:wght@700;800&display=swap');
html, body, .stApp { background:#060d1f !important; font-family:'Inter',sans-serif !important; }
#MainMenu,footer,header{visibility:hidden}
.stDeployButton{display:none}
section[data-testid="stSidebar"]{background:#080f22 !important;border-right:1px solid #0f2044 !important;}
section[data-testid="stSidebar"] *{color:#cbd5e1 !important;}
/* Lock the sidebar permanently open — remove both toggle controls */
[data-testid="stSidebarCollapseButton"]{display:none !important;}
[data-testid="stExpandSidebarButton"]{display:none !important;}
section[data-testid="stSidebar"] .stSelectbox label,
section[data-testid="stSidebar"] .stTextInput label,
section[data-testid="stSidebar"] .stNumberInput label,
section[data-testid="stSidebar"] .stDateInput label,
section[data-testid="stSidebar"] .stSlider label,
section[data-testid="stSidebar"] .stCheckbox label{
  color:#64748b !important;font-size:0.7rem !important;
  text-transform:uppercase;letter-spacing:1px;font-weight:600;
}
section[data-testid="stSidebar"] input,
section[data-testid="stSidebar"] select{background:#0d1a35 !important;border:1px solid #1e3a5f !important;color:#e2e8f0 !important;border-radius:8px !important;}
.stButton>button{background:linear-gradient(135deg,#1e40af,#3b82f6) !important;color:#fff !important;border:none !important;border-radius:10px !important;font-weight:700 !important;letter-spacing:.5px !important;box-shadow:0 4px 20px rgba(59,130,246,.35) !important;transition:all .2s !important;}
.stButton>button:hover{transform:translateY(-2px) !important;box-shadow:0 8px 25px rgba(59,130,246,.5) !important;}
.stTabs [data-baseweb="tab-list"]{background:#0d1a35 !important;border:1px solid #1e3a5f !important;border-radius:12px !important;padding:4px !important;gap:4px !important;}
.stTabs [data-baseweb="tab"]{color:#64748b !important;border-radius:8px !important;font-weight:600 !important;font-size:.84rem !important;padding:9px 18px !important;}
.stTabs [aria-selected="true"]{background:linear-gradient(135deg,#1e40af,#3b82f6) !important;color:#fff !important;}
[data-testid="stExpander"]{background:#0a1228 !important;border:1px solid #1e3a5f !important;border-radius:12px !important;}
::-webkit-scrollbar{width:5px;height:5px}
::-webkit-scrollbar-track{background:#060d1f}
::-webkit-scrollbar-thumb{background:#1e3a5f;border-radius:3px}
div[data-testid="stVerticalBlock"]{gap:.5rem}
</style>
""", unsafe_allow_html=True)

# Visual palette — colors/icons assigned to vendors dynamically by order of
# appearance in API responses (no hardcoded vendor names).
_PALETTE = ["#3b82f6", "#8b5cf6", "#f59e0b", "#10b981", "#ef4444", "#06b6d4", "#ec4899", "#84cc16"]
_VENDOR_DOTS = ["🔵", "🟣", "🟡", "🟢", "🔴", "🟠", "🟤", "⚪"]

ACTION_CFG = {
    "BOOK_NOW": {"color": "#10b981", "bg": "#022c22", "border": "#065f46", "icon": "✅", "label": "BOOK NOW"},
    "WAIT":     {"color": "#f59e0b", "bg": "#1c1200", "border": "#78350f", "icon": "⏳", "label": "WAIT"},
    "MONITOR":  {"color": "#3b82f6", "bg": "#071235", "border": "#1e40af", "icon": "👁️", "label": "MONITOR"},
}
STATUS_CFG = {
    "success":        {"color": "#10b981", "label": "Live Data"},
    "not_configured": {"color": "#64748b", "label": "Not Configured"},
    "api_error":      {"color": "#ef4444", "label": "API Error"},
    "no_results":     {"color": "#f59e0b", "label": "No Results"},
}
TREND_CFG = {
    "rising":  {"color": "#ef4444", "icon": "📈", "label": "Rising"},
    "falling": {"color": "#10b981", "icon": "📉", "label": "Falling"},
    "stable":  {"color": "#3b82f6", "icon": "➡️", "label": "Stable"},
}
_CHART = dict(template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)",
              plot_bgcolor="rgba(13,26,53,.6)", font=dict(family="Inter", color="#94a3b8"))


# ── helpers ────────────────────────────────────────────────────────────────────
def vendor_color(vendor: str, order: dict) -> str:
    return _PALETTE[order.setdefault(vendor, len(order)) % len(_PALETTE)]


def vendor_dot(vendor: str, order: dict) -> str:
    return _VENDOR_DOTS[order.setdefault(vendor, len(order)) % len(_VENDOR_DOTS)]


def _stars(n) -> str:
    return "⭐" * min(int(n), 7) if n else ""


def _pill(text, color, size=".68rem"):
    return (f'<span style="background:{color}18;border:1px solid {color}55;color:{color};'
            f'font-size:{size};font-weight:700;padding:3px 10px;border-radius:999px;'
            f'letter-spacing:.5px;white-space:nowrap">{text}</span>')


def _flatten(html: str) -> str:
    """
    Collapse newlines + indentation into a single line.
    CRITICAL: Streamlit's markdown treats any line indented by 4+ spaces as a
    CODE BLOCK — multi-line HTML templates would leak raw HTML into the UI.
    All custom HTML must pass through this before st.markdown().
    """
    return re.sub(r"\n\s*", " ", html).strip()


def _html(html: str) -> None:
    st.markdown(_flatten(html), unsafe_allow_html=True)


def _card(html, border="#1e3a5f", bg="#0d1a35", pad="1.2rem", radius="14px"):
    _html(f'<div style="background:{bg};border:1px solid {border};border-radius:{radius};'
          f'padding:{pad};margin-bottom:.75rem">{html}</div>')


def _section(icon, title, sub=""):
    sub_html = (f'<p style="color:#475569;font-size:.78rem;margin:2px 0 0 36px">{sub}</p>'
                if sub else "")
    _html(f"""
    <div style="margin:1.4rem 0 .8rem 0">
      <div style="display:flex;align-items:center;gap:10px">
        <span style="font-size:1.1rem">{icon}</span>
        <span style="font-family:'Plus Jakarta Sans',sans-serif;font-size:1.05rem;font-weight:800;color:#e2e8f0;letter-spacing:-.3px">{title}</span>
      </div>{sub_html}
    </div>""")


def _metric_tile(label: str, value: str, chip: str = "", chip_color: str = "#10b981"):
    chip_html = (f'<div style="margin-top:6px">{_pill(chip, chip_color, ".62rem")}</div>'
                 if chip else "")
    _html(f"""
    <div style="padding:.4rem 0">
      <div style="color:#64748b;font-size:.78rem;font-weight:600">{label}</div>
      <div style="color:#f1f5f9;font-family:'Plus Jakarta Sans',sans-serif;font-size:2.1rem;font-weight:800;line-height:1.15">{value}</div>
      {chip_html}
    </div>""")


# ── Access control — authorized users only ─────────────────────────────────────
def _require_login() -> None:
    if st.session_state.get("authenticated"):
        return

    app_password = os.environ.get("APP_PASSWORD", "")

    st.markdown(
        '<div style="max-width:380px;margin:9vh auto 0 auto;text-align:center">'
        '<div style="font-family:\'Plus Jakarta Sans\',sans-serif;font-size:1.6rem;'
        'font-weight:800;color:#3b82f6">🏨 UbidStay</div>'
        '<div style="color:#64748b;font-size:.72rem;font-weight:700;letter-spacing:2px;'
        'margin-top:.2rem">PRICE INTELLIGENCE PLATFORM</div>'
        '<div style="color:#475569;font-size:.8rem;margin-top:1.2rem">Authorized access only'
        '</div></div>',
        unsafe_allow_html=True,
    )

    if not app_password:
        # Fail CLOSED: with no password configured, nobody gets in until the
        # admin sets one — this must never silently open the app up.
        st.error("⚠️ This app is not yet configured. Set `APP_PASSWORD` in "
                 "**Settings → Secrets** (Streamlit Cloud) and reload.")
        st.stop()

    _, mid, _ = st.columns([1, 1.1, 1])
    with mid:
        pwd = st.text_input("Password", type="password", key="_login_pwd",
                            label_visibility="collapsed", placeholder="Enter password",
                            on_change=lambda: st.session_state.update(_login_submit=True))
        login_clicked = st.button("Unlock", width="stretch", key="_login_btn")
        if login_clicked or st.session_state.pop("_login_submit", False):
            if pwd == app_password:
                st.session_state["authenticated"] = True
                st.rerun()
            else:
                st.error("Incorrect password")
    st.stop()


_require_login()

# ═══════════════════════════════ SEARCH TABS ═══════════════════════════════════

def tab_vendor_results(result: dict, order: dict, cur: str) -> None:
    _section("🏨", "Results by Vendor", "every hotel each supplier returned — with all rate options")
    for v in result.get("vendors", []):
        dot = vendor_dot(v["vendor"], order)
        scfg = STATUS_CFG.get(v["search_status"], STATUS_CFG["api_error"])
        title = f'{dot} {v["vendor_display_name"]} — {v["hotels_found"]} hotels · {scfg["label"]}'
        with st.expander(title, expanded=(v["search_status"] == "success")):
            st.markdown(f'{_pill(scfg["label"].upper(), scfg["color"])} '
                        f'<span style="color:#475569;font-size:.75rem;margin-left:8px">'
                        f'{v["response_time_ms"]} ms</span>', unsafe_allow_html=True)
            if v.get("error"):
                _card(f'<span style="color:#fca5a5;font-size:.8rem">{v["error"]}</span>',
                      border="#7f1d1d", bg="#2d0a0a", pad=".7rem 1rem")
            if v["search_status"] == "not_configured":
                st.info("Add this vendor's API credentials in Secrets to enable live data.")
                continue
            if not v.get("hotels"):
                if v["search_status"] == "no_results":
                    st.info("Live call succeeded but returned no hotels for these parameters.")
                continue

            vcolor = vendor_color(v["vendor"], order)
            for h in v["hotels"][:15]:
                rating_html = (f'<span style="color:#3b82f6;font-weight:700;font-size:.8rem;'
                               f'margin-left:6px">{h["guest_rating"]:.1f}/10</span>'
                               if h.get("guest_rating") else "")
                amen = "".join(
                    f'<span style="background:#0a1228;border:1px solid #1e3a5f;color:#64748b;'
                    f'font-size:.62rem;padding:2px 8px;border-radius:6px;margin-right:5px">{a}</span>'
                    for a in (h.get("amenities") or [])[:5] if a
                )
                addr = h.get("address") or h.get("city") or ""
                low = h.get("lowest_rate") or 0
                high = h.get("highest_rate") or 0
                up_to = (f'<div style="color:#475569;font-size:.68rem">up to {high:,.0f}</div>'
                         if high > low else "")

                rate_boxes = ""
                for r in (h.get("rates") or [])[:3]:
                    ref = (_pill("✓ Refundable", "#10b981", ".6rem") if r.get("is_refundable")
                           else _pill("⚠ Non-refundable", "#f59e0b", ".6rem"))
                    rate_boxes += f"""
                    <div style="flex:1;min-width:170px;background:#0a1228;border:1px solid #16263f;border-radius:10px;padding:.7rem .8rem">
                      <div style="color:#94a3b8;font-size:.7rem;font-weight:600">{r.get("room_type", "Room")}</div>
                      <div style="color:#475569;font-size:.62rem;margin-bottom:4px">🍽 {r.get("meal_plan", "Room Only")}</div>
                      <div style="color:#f1f5f9;font-family:'Plus Jakarta Sans',sans-serif;font-size:1.15rem;font-weight:800">{r["price_per_night"]:,.0f}</div>
                      <div style="color:#475569;font-size:.6rem">/night · {r["total_price"]:,.0f} total</div>
                      <div style="margin-top:5px">{ref}</div>
                    </div>"""

                _card(f"""
                <div style="display:flex;justify-content:space-between;gap:14px;flex-wrap:wrap">
                  <div style="flex:1;min-width:250px">
                    <span style="color:#f1f5f9;font-weight:700;font-size:.95rem">{h["name"]}</span>
                    <span style="font-size:.72rem;margin-left:6px">{_stars(h.get("stars"))}</span>{rating_html}
                    <div style="color:#64748b;font-size:.72rem;margin:3px 0 7px 0">📍 {addr}</div>
                    <div>{amen}</div>
                  </div>
                  <div style="text-align:right;min-width:110px">
                    <div style="color:#475569;font-size:.62rem;font-weight:700;letter-spacing:1px">FROM</div>
                    <div style="color:{vcolor};font-family:'Plus Jakarta Sans',sans-serif;font-size:1.7rem;font-weight:800">{low:,.0f}</div>
                    <div style="color:#475569;font-size:.65rem">/night</div>
                    {up_to}
                  </div>
                </div>
                <div style="display:flex;gap:10px;margin-top:.8rem;flex-wrap:wrap">{rate_boxes}</div>
                """, border="#16263f", bg="#0c1730")


def tab_price_comparison(result: dict, order: dict, cur: str) -> None:
    comps = result.get("comparisons", [])
    multi = [c for c in comps if len(c.get("vendor_prices", [])) > 1]
    _section("⚖️", "Cross-Vendor Price Comparison",
             f"{len(multi)} properties matched across multiple vendors · sorted by biggest spread")
    if not comps:
        st.info("No comparison data — no live prices were returned.")
        return
    if not multi:
        st.info("No property appeared in more than one vendor's live results for this search — "
                "showing single-vendor properties below.")
    for c in (multi or comps)[:14]:
        rows = "".join(
            f'<div style="display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid #0f2044">'
            f'<span style="color:{vendor_color(v["vendor"], order)};font-weight:600;font-size:.8rem">'
            f'{vendor_dot(v["vendor"], order)} {v["vendor"]}</span>'
            f'<span style="color:#e2e8f0;font-size:.82rem;font-weight:600">{v["price_per_night"]:,.0f} {v.get("currency", cur)}'
            f'{" 🏆" if v["vendor"] == c.get("cheapest_vendor") else ""}</span></div>'
            for v in sorted(c.get("vendor_prices", []), key=lambda x: x["price_per_night"])
        )
        if len(c.get("vendor_prices", [])) > 1 and c.get("price_difference", 0) > 0:
            spread = _pill(f'SAVE {c["price_difference"]:,.0f} {cur} '
                           f'({c.get("price_difference_pct", 0):.0f}%)', "#10b981")
        else:
            spread = ""
        _card(f"""
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:.4rem">
            <div>
              <span style="color:#f1f5f9;font-weight:700;font-size:.92rem">{c["canonical_name"]}</span>
              <span style="margin-left:8px;font-size:.72rem">{_stars(c.get("stars"))}</span>
              <span style="color:#64748b;font-size:.7rem;margin-left:8px">{c.get("canonical_address", "")}</span>
            </div>{spread}
          </div>{rows}
        """)


def tab_seasonal(result: dict, cur: str) -> None:
    tl = result.get("price_timeline")
    _section("📊", "Seasonal Price Intelligence",
             f'{result["destination"]} — live forward scan: real vendor quotes at shifted check-in dates')
    if not tl or not tl.get("points"):
        st.info("Enable **Live future-price scan** in the sidebar and search again to see "
                "the forward price curve.")
        return
    pts = [p for p in tl["points"] if p.get("sample_size")]
    if not pts:
        st.info("The live forward scan returned no data for this search.")
        return

    scan_avg = statistics.fmean([p["avg_price"] for p in pts])
    cheapest_label = min(pts, key=lambda p: p["avg_price"])["label"]
    expensive_label = max(pts, key=lambda p: p["avg_price"])["label"]
    colors, your_label = [], None
    for p in pts:
        if p["label"] == cheapest_label:
            colors.append("#10b981")
        elif p["label"] == expensive_label:
            colors.append("#ef4444")
        else:
            colors.append("#f59e0b")
        if p["offset_days"] == 0:
            your_label = p["label"]

    cleft, cright = st.columns([2.2, 1])
    with cleft:
        fig = go.Figure(go.Bar(
            x=[p["label"] for p in pts], y=[p["avg_price"] for p in pts],
            marker_color=colors, text=[f'{p["avg_price"]:,.0f}' for p in pts],
            textposition="outside",
            customdata=[[p["checkin"], p["min_price"], p["max_price"], p["sample_size"]] for p in pts],
            hovertemplate="<b>%{x}</b><br>check-in %{customdata[0]}<br>avg %{y:,.0f}"
                          "<br>min %{customdata[1]:,.0f} · max %{customdata[2]:,.0f}"
                          "<br>%{customdata[3]} live prices<extra></extra>",
        ))
        if your_label:
            fig.add_annotation(x=your_label, y=max(p["avg_price"] for p in pts) * 1.12,
                               text="▼ Your trip", showarrow=False,
                               font=dict(color="#cbd5e1", size=12))
        fig.update_layout(height=420, margin=dict(l=10, r=10, t=40, b=10),
                          yaxis_title=f"Avg live price / night ({cur})", showlegend=False, **_CHART)
        st.plotly_chart(fig, width='stretch')
        _html('<div style="display:flex;gap:18px;margin-top:-6px">'
              + _pill("🟥 Most expensive window", "#ef4444")
              + _pill("🟨 Mid", "#f59e0b") + _pill("🟩 Cheapest window", "#10b981")
              + '</div>')
    with cright:
        your = next((p for p in pts if p["offset_days"] == 0), pts[0])
        your_idx = round(your["avg_price"] / scan_avg * 100) if scan_avg else 100
        season_word = ("Cheapest scanned window" if your["label"] == cheapest_label else
                       "Most expensive scanned window" if your["label"] == expensive_label else
                       "Mid-range window")
        scolor = ("#10b981" if your["label"] == cheapest_label else
                  "#ef4444" if your["label"] == expensive_label else "#f59e0b")
        _card(f"""
          <div style="color:{scolor};font-size:.68rem;font-weight:700;letter-spacing:1px">💚 YOUR TRAVEL WINDOW</div>
          <div style="color:#f1f5f9;font-family:'Plus Jakarta Sans',sans-serif;font-size:1.3rem;font-weight:800;margin:.2rem 0">{season_word}</div>
          <div style="color:{scolor};font-size:.78rem">Price index {your_idx} ({your_idx - 100:+d}% vs scan avg)</div>
        """, border=scolor + "55", bg="#07140f" if scolor == "#10b981" else "#0d1a35")
        cheap = min(pts, key=lambda p: p["avg_price"])
        _card(f"""
          <div style="color:#10b981;font-size:.68rem;font-weight:700;letter-spacing:1px">💚 CHEAPEST WINDOW</div>
          <div style="color:#f1f5f9;font-weight:800;font-size:1.05rem;margin:.2rem 0">{cheap["label"]} · {cheap["avg_price"]:,.0f} {cur}</div>
          <div style="color:#475569;font-size:.72rem">check-in {cheap["checkin"]} — best time to buy inventory</div>
        """, border="#065f46", bg="#07140f")
        exp = max(pts, key=lambda p: p["avg_price"])
        _card(f"""
          <div style="color:#ef4444;font-size:.68rem;font-weight:700;letter-spacing:1px">🔥 MOST EXPENSIVE WINDOW</div>
          <div style="color:#f1f5f9;font-weight:800;font-size:1.05rem;margin:.2rem 0">{exp["label"]} · {exp["avg_price"]:,.0f} {cur}</div>
          <div style="color:#475569;font-size:.72rem">check-in {exp["checkin"]} — premium pricing</div>
        """, border="#7f1d1d", bg="#190b0b")
        tcfg = TREND_CFG.get(tl.get("trend", "stable"), TREND_CFG["stable"])
        _card(f"""
          <div style="color:{tcfg["color"]};font-size:.68rem;font-weight:700;letter-spacing:1px">{tcfg["icon"]} FORWARD TREND</div>
          <div style="color:{tcfg["color"]};font-family:'Plus Jakarta Sans',sans-serif;font-size:1.5rem;font-weight:800;margin:.2rem 0">{tl.get("trend_pct", 0):+.1f}%</div>
          <div style="color:#475569;font-size:.72rem">{tcfg["label"]} · confidence {tl.get("confidence", 0):.0%} · vendors: {", ".join(tl.get("vendors_used", []))}</div>
        """, border=tcfg["color"] + "55")

    _section("🗓️", "Window-by-Window Breakdown", "")
    cols = st.columns(min(len(pts), 8))
    for i, p in enumerate(pts[:8]):
        idx = round(p["avg_price"] / scan_avg * 100) if scan_avg else 100
        is_you = p["offset_days"] == 0
        is_cheap = p["label"] == cheapest_label
        bcolor = "#065f46" if is_cheap else ("#1e40af" if is_you else "#1e3a5f")
        tag = ('<div style="margin-top:4px">' + _pill("▲ yours", "#3b82f6", ".58rem") + "</div>") if is_you else ""
        with cols[i % len(cols)]:
            _card(f"""
              <div style="text-align:center">
                <div style="color:#64748b;font-size:.64rem;font-weight:700">{p["label"]}</div>
                <div style="color:{"#10b981" if is_cheap else "#f1f5f9"};font-family:'Plus Jakarta Sans',sans-serif;font-size:1.15rem;font-weight:800">{p["avg_price"]:,.0f}</div>
                <div style="color:#475569;font-size:.6rem">{idx} idx · {p["sample_size"]} quotes</div>
                <div style="color:#475569;font-size:.58rem">{p["checkin"]}</div>{tag}
              </div>""", pad=".6rem .4rem", border=bcolor)
    if tl.get("best_booking_advice"):
        _card(f'<span style="color:#cbd5e1;font-size:.85rem">💡 {tl["best_booking_advice"]}</span>',
              border="#1e40af")


def tab_ai(result: dict, dname, cur: str) -> None:
    reco = result.get("ai_recommendation")
    _section("🤖", "AI Recommendation & Analysis", "full reasoning behind the booking recommendation")
    if not reco:
        st.info("No AI recommendation — no live prices were returned by any vendor.")
        return
    cfg = ACTION_CFG.get(reco["action"], ACTION_CFG["MONITOR"])
    left, right = st.columns([1.9, 1])
    with left:
        analysis_html = (reco.get("full_analysis", "") or "").replace("\n", "<br>")
        _card(f'<div style="color:#a8b8d0;font-size:.88rem;line-height:1.7">{analysis_html}</div>',
              pad="1.4rem")
    with right:
        conf = float(reco.get("confidence", 0))
        _card(f"""
          <div style="text-align:center;padding:.4rem 0">
            <div style="font-size:2rem">{cfg["icon"]}</div>
            <div style="color:{cfg["color"]};font-family:'Plus Jakarta Sans',sans-serif;font-size:1.5rem;font-weight:800;letter-spacing:1px">{cfg["label"]}</div>
            <div style="color:#475569;font-size:.7rem;margin:.4rem 0">Confidence: {conf:.0%}</div>
            <div style="background:#0a1228;border-radius:99px;height:8px;overflow:hidden">
              <div style="background:{cfg["color"]};width:{conf * 100:.0f}%;height:8px"></div>
            </div>
          </div>""", border=cfg["border"], bg=cfg["bg"])
        _card(f"""
          <div style="color:#3b82f6;font-size:.68rem;font-weight:700;letter-spacing:1px">🔔 WHEN TO BOOK</div>
          <div style="color:#f1f5f9;font-weight:700;margin-top:.3rem">{reco.get("best_time_to_book", "—")}</div>
        """)
        avoid = reco.get("avoid_periods") or []
        if avoid:
            _card(f"""
              <div style="color:#ef4444;font-size:.68rem;font-weight:700;letter-spacing:1px">🚫 AVOID THESE WINDOWS</div>
              <div style="color:#f1f5f9;font-weight:700;margin-top:.3rem">{" · ".join(avoid)}</div>
              <div style="color:#64748b;font-size:.7rem;margin-top:2px">higher live prices in the forward scan</div>
            """, border="#7f1d1d", bg="#190b0b")
        tips = reco.get("tips") or []
        if tips:
            _html('<div style="color:#f59e0b;font-size:.68rem;font-weight:700;letter-spacing:1px;margin:.4rem 0 .4rem 4px">💡 EXPERT TIPS</div>')
            for t in tips:
                _card(f'<span style="color:#94a3b8;font-size:.8rem">▸ {t}</span>',
                      pad=".6rem .9rem", border="#16263f", bg="#0a1228")
        _card(f"""
          <div style="display:flex;justify-content:space-between">
            <span style="color:#64748b;font-size:.75rem">Best price</span>
            <span style="color:#10b981;font-weight:800">{reco.get("best_price", 0):,.0f} {cur} · {dname(reco.get("best_vendor", ""))}</span>
          </div>
          <div style="display:flex;justify-content:space-between;margin-top:4px">
            <span style="color:#64748b;font-size:.75rem">Saving vs market avg</span>
            <span style="color:#e2e8f0;font-weight:700">{reco.get("potential_saving", 0):,.0f} {cur}</span>
          </div>""")


def tab_market(result: dict, order: dict, cur: str) -> None:
    _section("📊", "Market Overview", "aggregated pricing intelligence across all vendors")
    live = [v for v in result.get("vendors", []) if v.get("hotels")]
    if not live:
        st.info("No live vendor data to aggregate for this search.")
        return

    names = [v["vendor_display_name"] for v in live]
    counts = [v["hotels_found"] for v in live]
    colors = [vendor_color(v["vendor"], order) for v in live]

    c1, c2, c3 = st.columns(3)
    with c1:
        fig = go.Figure(go.Pie(labels=names, values=counts, hole=.58,
                               marker=dict(colors=colors, line=dict(color="#060d1f", width=2)),
                               textinfo="percent", textfont=dict(size=11)))
        total = sum(counts)
        fig.add_annotation(text=f"<b>{total}</b><br>Hotels", showarrow=False,
                           font=dict(size=15, color="#f1f5f9"))
        fig.update_layout(title="Hotels by Vendor", height=330,
                          margin=dict(l=10, r=10, t=45, b=10),
                          legend=dict(orientation="h", y=-0.1), **_CHART)
        st.plotly_chart(fig, width='stretch')
    with c2:
        fig = go.Figure()
        for v in live:
            lows = [h["lowest_rate"] for h in v["hotels"] if h.get("lowest_rate")]
            highs = [h.get("highest_rate") or h["lowest_rate"] for h in v["hotels"] if h.get("lowest_rate")]
            if not lows:
                continue
            lo, hi, avg = min(lows), max(highs), statistics.fmean(lows)
            col = vendor_color(v["vendor"], order)
            fig.add_trace(go.Bar(x=[v["vendor_display_name"]], y=[hi - lo], base=[lo],
                                 marker_color=col, opacity=.75, width=.55,
                                 text=[f"{avg:,.0f} avg"], textposition="outside",
                                 hovertemplate=f"min {lo:,.0f} · max {hi:,.0f}<extra></extra>",
                                 showlegend=False))
        fig.update_layout(title="Price Range per Vendor (min–max)", height=330,
                          margin=dict(l=10, r=10, t=45, b=10),
                          yaxis_title=f"{cur}/night", **_CHART)
        st.plotly_chart(fig, width='stretch')
    with c3:
        star_counts: dict[int, int] = {}
        for v in live:
            for h in v["hotels"]:
                if h.get("stars"):
                    star_counts[int(h["stars"])] = star_counts.get(int(h["stars"]), 0) + 1
        if star_counts:
            ks = sorted(star_counts)
            fig = go.Figure(go.Bar(x=["⭐" * k for k in ks], y=[star_counts[k] for k in ks],
                                   marker_color="#f59e0b", text=[star_counts[k] for k in ks],
                                   textposition="outside"))
            fig.update_layout(title="Star Rating Distribution", height=330,
                              margin=dict(l=10, r=10, t=45, b=10), **_CHART)
            st.plotly_chart(fig, width='stretch')
        else:
            st.info("No star data returned by vendors.")

    _section("🏆", "Vendor Performance Summary", "")
    for v in result.get("vendors", []):
        scfg = STATUS_CFG.get(v["search_status"], STATUS_CFG["api_error"])
        dot = vendor_dot(v["vendor"], order)
        lows = [h["lowest_rate"] for h in v.get("hotels", []) if h.get("lowest_rate")]
        ratings = [h["guest_rating"] for h in v.get("hotels", []) if h.get("guest_rating")]
        cheapest = f"{min(lows):,.0f}" if lows else "—"
        avgp = f"{statistics.fmean(lows):,.0f}" if lows else "—"
        avgr = f"{statistics.fmean(ratings):.1f}" if ratings else "—"
        _card(f"""
          <div style="display:flex;align-items:center;gap:18px;flex-wrap:wrap">
            <div style="min-width:200px">
              <span style="color:#f1f5f9;font-weight:700">{dot} {v["vendor_display_name"]}</span>
              <span style="margin-left:8px">{_pill(scfg["label"], scfg["color"], ".6rem")}</span>
            </div>
            <div style="flex:1;display:flex;gap:26px;flex-wrap:wrap">
              <div><div style="color:#64748b;font-size:.66rem">Hotels</div><div style="color:#f1f5f9;font-size:1.25rem;font-weight:800">{v["hotels_found"]}</div></div>
              <div><div style="color:#64748b;font-size:.66rem">Cheapest</div><div style="color:#f1f5f9;font-size:1.25rem;font-weight:800">{cheapest}</div></div>
              <div><div style="color:#64748b;font-size:.66rem">Avg Price</div><div style="color:#f1f5f9;font-size:1.25rem;font-weight:800">{avgp}</div></div>
              <div><div style="color:#64748b;font-size:.66rem">Avg Rating</div><div style="color:#f1f5f9;font-size:1.25rem;font-weight:800">{avgr}</div></div>
              <div><div style="color:#64748b;font-size:.66rem">Response</div><div style="color:#f1f5f9;font-size:1.25rem;font-weight:800">{v["response_time_ms"]:,} ms</div></div>
            </div>
          </div>""", pad=".9rem 1.2rem")


# ═══════════════════════ ADVANCED ANALYSIS VIEWS ═══════════════════════════════

def render_history() -> None:
    _section("📜", "Price History",
             "real recorded data from your past live searches — grows every time you search")
    try:
        dests = backend.history_destinations()
    except Exception as exc:
        st.error(f"History error: {exc}")
        return
    if not dests:
        st.info("No history yet. Run a few searches — every live result is recorded "
                "automatically and will appear here.")
        return
    c1, c2 = st.columns([3, 1])
    with c1:
        labels = [f'{d["destination"]} ({d["snapshots"]} snapshots, last {str(d["last_seen"])[:10]})'
                  for d in dests]
        idx = st.selectbox("Destination", range(len(dests)), format_func=lambda i: labels[i])
    with c2:
        days = st.number_input("Days back", 7, 730, 90)
    dest = dests[idx]["destination"]
    try:
        trend = backend.history_trend(dest, int(days))
    except Exception as exc:
        st.error(f"Trend error: {exc}")
        return
    market = trend.get("market", [])
    if market:
        fig = go.Figure()
        x = [m["day"] for m in market]
        fig.add_trace(go.Scatter(x=x, y=[m["max_price"] for m in market], mode="lines",
                                 line=dict(width=0), showlegend=False, hoverinfo="skip"))
        fig.add_trace(go.Scatter(x=x, y=[m["min_price"] for m in market], name="Min–Max",
                                 mode="lines", fill="tonexty",
                                 fillcolor="rgba(59,130,246,.12)", line=dict(width=0)))
        fig.add_trace(go.Scatter(x=x, y=[m["avg_price"] for m in market], name="Market average",
                                 mode="lines+markers", line=dict(color="#3b82f6", width=3)))
        fig.update_layout(height=360, margin=dict(l=10, r=10, t=30, b=10),
                          yaxis_title="Price / night", legend=dict(orientation="h", y=1.12),
                          **_CHART)
        st.plotly_chart(fig, width='stretch')
    by_vendor = trend.get("by_vendor", [])
    if by_vendor:
        _section("🏷️", "Per-Vendor Average Price Over Time", "")
        vorder: dict = {}
        fig2 = go.Figure()
        for v in sorted({r["vendor"] for r in by_vendor}):
            rows = [r for r in by_vendor if r["vendor"] == v]
            fig2.add_trace(go.Scatter(x=[r["day"] for r in rows], y=[r["avg_price"] for r in rows],
                                      name=v, mode="lines+markers",
                                      line=dict(color=vendor_color(v, vorder), width=2)))
        fig2.update_layout(height=340, margin=dict(l=10, r=10, t=30, b=10),
                           yaxis_title="Avg price / night",
                           legend=dict(orientation="h", y=1.15), **_CHART)
        st.plotly_chart(fig2, width='stretch')
    _section("🏨", "Specific Hotel History", "price per vendor over time for one property")
    hname = st.text_input("Hotel name", placeholder="e.g. Grand Hotel & Spa", key="hist_hotel")
    if hname.strip():
        try:
            rows = backend.history_hotel(hname.strip(), dest, int(days))
        except Exception as exc:
            st.error(f"Hotel history error: {exc}")
            return
        if not rows:
            st.info("No recorded quotes for this hotel yet — search it a few times first.")
        else:
            vorder2: dict = {}
            fig3 = go.Figure()
            for v in sorted({r["vendor"] for r in rows}):
                vrows = [r for r in rows if r["vendor"] == v]
                fig3.add_trace(go.Scatter(x=[r["day"] for r in vrows],
                                          y=[r["avg_price"] for r in vrows],
                                          name=v, mode="lines+markers",
                                          line=dict(color=vendor_color(v, vorder2), width=2)))
            fig3.update_layout(height=320, margin=dict(l=10, r=10, t=30, b=10),
                               yaxis_title="Avg price / night",
                               legend=dict(orientation="h", y=1.15), **_CHART)
            st.plotly_chart(fig3, width='stretch')
            st.dataframe(rows, width='stretch', height=260)


def render_watchlist() -> None:
    _section("👁️", "Watchlist & Auto-Monitoring",
             "system re-scans these live on a schedule and alerts on price drops")
    with st.expander("➕ Add a new watch", expanded=False):
        c1, c2 = st.columns(2)
        with c1:
            w_dest = st.text_input("Destination", key="w_dest")
            w_hotel = st.text_input("Hotel name (optional)", key="w_hotel")
            w_currency = st.text_input("Currency", value="USD", max_chars=3, key="w_cur")
        with c2:
            w_in = st.date_input("Check-in", value=date.today() + timedelta(days=30), key="w_in")
            w_out = st.date_input("Check-out", value=date.today() + timedelta(days=33), key="w_out")
            w_target = st.number_input("Target price /night (0 = off)", 0.0, 100000.0, 0.0,
                                       key="w_target")
        if st.button("Add to watchlist", key="w_add"):
            if not w_dest.strip():
                st.warning("Destination required")
            else:
                try:
                    payload = {"destination": w_dest.strip(), "checkin": w_in.isoformat(),
                               "checkout": w_out.isoformat(),
                               "currency": (w_currency or "USD").upper(),
                               "target_price": float(w_target)}
                    if w_hotel.strip():
                        payload["hotel_name"] = w_hotel.strip()
                    r = backend.watchlist_create(payload)
                    st.success(f"Watch #{r['id']} created")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Create failed: {exc}")
    try:
        watches = backend.watchlist_list()
        alerts = backend.watchlist_alerts_recent()
    except Exception as exc:
        st.error(f"Watchlist error: {exc}")
        return
    if alerts:
        _section("🚨", "Recent Alerts", "")
        for a in alerts[:8]:
            tgt = f' · {a.get("watch_hotel")}' if a.get("watch_hotel") else ""
            _card(f'<span style="color:#fca5a5;font-weight:600">{a["destination"]}{tgt}</span> '
                  f'<span style="color:#94a3b8;font-size:.8rem">— {a.get("note", "")} '
                  f'({str(a["ts"])[:16]})</span>', border="#7f1d1d", pad=".7rem 1rem")
    if not watches:
        st.info("No watches yet — add one above. The background monitor scans every active "
                "watch automatically; you can also run scans manually.")
        return
    if st.button("▶️ Run ALL active watches now (live scan)", key="run_all"):
        with st.spinner("Running live scans for all watches…"):
            try:
                results = backend.watchlist_run_all()
                st.success(f"{len(results)} watches scanned")
                st.rerun()
            except Exception as exc:
                st.error(f"Run-all failed: {exc}")
    for w in watches:
        lr = w.get("last_run") or {}
        status_pill = _pill("ACTIVE", "#10b981") if w.get("active") else _pill("INACTIVE", "#64748b")
        hotel_part = f' · 🎯 {w["hotel_name"]}' if w.get("hotel_name") else ""
        target_part = (f' · target {w["target_price"]:.0f}'
                       if (w.get("target_price") or 0) > 0 else "")
        last_part = (f'{lr["best_price"]:.0f} {w.get("currency", "USD")}/night via '
                     f'{lr.get("best_vendor", "?")} ({lr.get("change_pct", 0):+.1f}% vs prev · '
                     f'{str(lr.get("ts", ""))[:16]})'
                     if lr.get("best_price") else "no successful run yet")
        _card(f"""
          <div style="display:flex;justify-content:space-between;align-items:center">
            <div>
              <span style="color:#f1f5f9;font-weight:700">#{w["id"]} {w["destination"]}{hotel_part}</span>
              <span style="color:#64748b;font-size:.75rem;margin-left:8px">{w["checkin"]} → {w["checkout"]}{target_part}</span>
            </div>{status_pill}
          </div>
          <div style="color:#94a3b8;font-size:.82rem;margin-top:.35rem">Last: {last_part}</div>
        """)
        b1, b2, b3, _sp = st.columns([1, 1, 1, 4])
        with b1:
            if st.button("Run now", key=f"run_{w['id']}"):
                with st.spinner("Live scan…"):
                    try:
                        r = backend.watchlist_run_one(w['id'])
                        if r.get("best_price"):
                            st.success(f'{r["best_price"]:.0f} via {r.get("best_vendor", "?")}'
                                       + (" 🚨 ALERT" if r.get("alert") else ""))
                        else:
                            st.warning(r.get("note") or "No live prices on this run")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Run failed: {exc}")
        with b2:
            show = st.button("History", key=f"hist_{w['id']}")
        with b3:
            if st.button("Delete", key=f"del_{w['id']}"):
                try:
                    backend.watchlist_delete(w['id'])
                    st.rerun()
                except Exception as exc:
                    st.error(f"Delete failed: {exc}")
        if show:
            try:
                runs = [r for r in backend.watchlist_history(w['id'])
                        if r.get("best_price")]
            except Exception as exc:
                runs = []
                st.error(f"History failed: {exc}")
            if runs:
                figw = go.Figure()
                figw.add_trace(go.Scatter(x=[r["ts"] for r in runs],
                                          y=[r["best_price"] for r in runs],
                                          mode="lines+markers", name="Best price",
                                          line=dict(color="#3b82f6", width=3)))
                ar = [r for r in runs if r.get("alert")]
                if ar:
                    figw.add_trace(go.Scatter(x=[r["ts"] for r in ar],
                                              y=[r["best_price"] for r in ar],
                                              mode="markers", name="Alert",
                                              marker=dict(color="#ef4444", size=12, symbol="star")))
                figw.update_layout(height=300, margin=dict(l=10, r=10, t=30, b=10),
                                   yaxis_title="Best price / night",
                                   legend=dict(orientation="h", y=1.15), **_CHART)
                st.plotly_chart(figw, width='stretch')
            else:
                st.info("No successful runs recorded yet for this watch.")


def render_analytics() -> None:
    _section("📊", "Vendor Performance Analytics",
             "computed from YOUR recorded live searches — more searches = sharper numbers")
    days = st.number_input("Days back", 7, 730, 90, key="an_days")
    try:
        stats = backend.analytics_vendors(int(days))
    except Exception as exc:
        st.error(f"Analytics error: {exc}")
        return
    if not stats:
        st.info("No recorded data yet — run a few searches first.")
        return
    vorder: dict = {}
    vendors = [s["vendor"] for s in stats]
    colors = [vendor_color(v, vorder) for v in vendors]
    c1, c2 = st.columns(2)
    with c1:
        fig = go.Figure(go.Bar(x=vendors, y=[s["win_rate_pct"] for s in stats],
                               marker_color=colors,
                               text=[f'{s["win_rate_pct"]:.0f}%' for s in stats],
                               textposition="outside"))
        fig.update_layout(title="Cheapest-vendor win rate (multi-vendor matches)",
                          height=330, margin=dict(l=10, r=10, t=50, b=10), **_CHART)
        st.plotly_chart(fig, width='stretch')
    with c2:
        fig = go.Figure(go.Bar(x=vendors, y=[s["success_rate_pct"] for s in stats],
                               marker_color=colors,
                               text=[f'{s["success_rate_pct"]:.0f}%' for s in stats],
                               textposition="outside"))
        fig.update_layout(title="Live-call success rate", height=330,
                          margin=dict(l=10, r=10, t=50, b=10), **_CHART)
        st.plotly_chart(fig, width='stretch')
    c3, c4 = st.columns(2)
    with c3:
        fig = go.Figure(go.Bar(x=vendors, y=[s["avg_response_ms"] or 0 for s in stats],
                               marker_color=colors))
        fig.update_layout(title="Avg response time (ms)", height=300,
                          margin=dict(l=10, r=10, t=50, b=10), **_CHART)
        st.plotly_chart(fig, width='stretch')
    with c4:
        fig = go.Figure(go.Bar(x=vendors, y=[s["avg_saving_vs_worst"] or 0 for s in stats],
                               marker_color=colors))
        fig.update_layout(title="Avg saving vs most expensive vendor (same hotel)",
                          height=300, margin=dict(l=10, r=10, t=50, b=10), **_CHART)
        st.plotly_chart(fig, width='stretch')
    st.dataframe(stats, width='stretch')


# ═══════════════════════════════ SIDEBAR ═══════════════════════════════════════

with st.sidebar:
    _html("""
    <div style="padding:.5rem 0 1rem 0">
      <span style="font-family:'Plus Jakarta Sans',sans-serif;font-size:1.25rem;font-weight:800;color:#3b82f6">🏨 UbidStay</span><br>
      <span style="color:#64748b;font-size:.66rem;font-weight:700;letter-spacing:2.5px">PRICE INTELLIGENCE PLATFORM</span>
    </div>""")

    mode = st.radio("Mode",
                    ["🔍 Search", "📜 History", "👁️ Watchlist", "📊 Vendor Analytics"],
                    label_visibility="collapsed")

    go_search = False
    if mode == "🔍 Search":
        st.markdown("---")
        _html('<div style="color:#64748b;font-size:.68rem;font-weight:700;letter-spacing:1px">📍 DESTINATION</div>')
        destination = st.text_input("Destination", placeholder="e.g. Dubai, Karachi, Pakistan",
                                    label_visibility="collapsed")
        hotel_name = st.text_input("Hotel Name (optional)", placeholder="🎯 Specific hotel (optional)",
                                   help="Restrict the whole analysis to one property",
                                   label_visibility="collapsed")
        _html('<div style="color:#64748b;font-size:.68rem;font-weight:700;letter-spacing:1px;margin-top:.4rem">📅 DATES</div>')
        c1, c2 = st.columns(2)
        with c1:
            checkin = st.date_input("Check-in", value=date.today() + timedelta(days=30),
                                    label_visibility="collapsed")
        with c2:
            checkout = st.date_input("Check-out", value=date.today() + timedelta(days=35),
                                     label_visibility="collapsed")
        _html('<div style="color:#64748b;font-size:.68rem;font-weight:700;letter-spacing:1px;margin-top:.4rem">👥 GUESTS</div>')
        c1, c2, c3 = st.columns(3)
        with c1:
            adults = st.number_input("Adults", 1, 20, 2)
        with c2:
            children = st.number_input("Kids", 0, 10, 0)
        with c3:
            rooms = st.number_input("Rooms", 1, 10, 1)
        _html('<div style="color:#64748b;font-size:.68rem;font-weight:700;letter-spacing:1px;margin-top:.4rem">⚙️ FILTERS</div>')
        budget = st.slider("Max budget/night (0 = none)", 0, 2000, 0, 10,
                           help="0 means no budget cap")
        stars = st.slider("Min stars", 0, 5, 0)
        rating = st.slider("Min guest rating", 0.0, 10.0, 0.0, 0.5)
        radius = st.slider("Search radius (km)", 1, 100, 10)
        c1, c2 = st.columns(2)
        with c1:
            currency = st.text_input("Currency", value="USD", max_chars=3)
        with c2:
            accommodation_type = st.selectbox("Type", ["hotel", "apartment", "resort",
                                                       "vacation_rental"])
        include_timeline = st.checkbox("Live future-price scan (prediction)", value=True,
                                       help="Re-queries vendors at +7/+14/+30/+60/+90 days — slower but predictive")
        st.markdown("---")
        go_search = st.button("🔍  RUN PRICE INTELLIGENCE", width='stretch')

# ═══════════════════════════════ HEADER ════════════════════════════════════════

_html("""
<div style="margin-bottom:.15rem">
  <span style="font-family:'Plus Jakarta Sans',sans-serif;font-size:2rem;font-weight:800;color:#f1f5f9">Hotel Price </span>
  <span style="font-family:'Plus Jakarta Sans',sans-serif;font-size:2rem;font-weight:800;color:#38bdf8">Intelligence</span>
</div>
<div style="color:#475569;font-size:.85rem;margin-bottom:.8rem">Multi-vendor comparison · Live forward-price analysis · AI-powered booking recommendations</div>""")

vendor_meta: list = []
try:
    vendor_meta = backend.vendors()
    chips = []
    for v in vendor_meta:
        color = "#10b981" if v["configured"] else "#64748b"
        label = "LIVE" if v["configured"] else "NO KEY"
        pill = _pill(f"{v['display_name']} · {label}", color)
        chips.append(f'<span style="margin-right:8px">{pill}</span>')
    st.markdown(f'<div style="margin:.2rem 0 1rem 0">{"".join(chips)}</div>',
                unsafe_allow_html=True)
except Exception as exc:
    st.error(f"⚠️ Backend error: `{exc}`")

# ── Non-search modes (advanced analysis) ───────────────────────────────────────
if mode == "📜 History":
    render_history()
    st.stop()
elif mode == "👁️ Watchlist":
    render_watchlist()
    st.stop()
elif mode == "📊 Vendor Analytics":
    render_analytics()
    st.stop()

# ── Run search ─────────────────────────────────────────────────────────────────
if go_search:
    if not destination.strip():
        st.warning("Enter a destination first.")
        st.stop()
    payload = {
        "destination": destination.strip(),
        "checkin": checkin.isoformat(),
        "checkout": checkout.isoformat(),
        "rooms": int(rooms),
        "adults": int(adults),
        "children": int(children),
        "budget": float(budget),
        "stars": int(stars),
        "rating": float(rating),
        "radius": int(radius),
        "accommodation_type": accommodation_type,
        "currency": currency.strip().upper() or "USD",
        "include_timeline": bool(include_timeline),
    }
    if hotel_name.strip():
        payload["hotel_name"] = hotel_name.strip()
    with st.spinner("Querying live vendor APIs… (future-price scan can take a minute)"):
        try:
            st.session_state["result"] = backend.search(payload)
        except Exception as exc:
            st.session_state.pop("result", None)
            st.error(f"Search failed: {exc}")

result = st.session_state.get("result")
if not result:
    st.info("Set your search parameters in the sidebar and run a search. "
            "Results come exclusively from the live vendor APIs configured on the server.")
    st.stop()

cur = result.get("currency", "USD")
order: dict = {}
display_names = {v["vendor"]: v["vendor_display_name"] for v in result.get("vendors", [])}
for v in result.get("vendors", []):          # stable colors by response order
    vendor_color(v["vendor"], order)


def dname(vendor: str) -> str:
    return display_names.get(vendor, vendor)


summary = result.get("summary", {})
cheapest = summary.get("cheapest_option", {}) or {}
market_avg = result.get("market_average_price", 0) or 0
best_price = cheapest.get("price_per_night", 0) or 0
save_vs_avg = max(market_avg - best_price, 0)
reco = result.get("ai_recommendation")
tl = result.get("price_timeline")

# ── HERO recommendation card ───────────────────────────────────────────────────
if reco:
    cfg = ACTION_CFG.get(reco["action"], ACTION_CFG["MONITOR"])
    trend_chip = ""
    if tl and tl.get("points"):
        tcfg = TREND_CFG.get(tl.get("trend", "stable"), TREND_CFG["stable"])
        trend_chip = _pill(f'{tcfg["icon"]} {tcfg["label"]} forward prices', tcfg["color"], ".72rem")
    analysis_html = (reco.get("full_analysis", "") or "").replace("\n", "<br>")
    target_line = f' · 🎯 {result["hotel_name"]}' if result.get("hotel_name") else ""
    _card(f"""
      <div style="display:flex;justify-content:space-between;gap:24px;flex-wrap:wrap">
        <div style="flex:1;min-width:300px">
          <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
            <span style="background:{cfg["bg"]};border:1px solid {cfg["border"]};color:{cfg["color"]};font-weight:800;font-size:.8rem;padding:6px 16px;border-radius:9px;letter-spacing:1.5px">{cfg["icon"]} {cfg["label"]}</span>
            {trend_chip}
            <span style="color:#64748b;font-size:.75rem">{reco.get("confidence", 0):.0%} confidence · engine: {reco.get("engine", "?")}</span>
          </div>
          <div style="font-family:'Plus Jakarta Sans',sans-serif;color:#f1f5f9;font-size:1.45rem;font-weight:800;margin:.7rem 0 .25rem 0;letter-spacing:-.4px">{reco.get("headline", "")}</div>
          <div style="color:#64748b;font-size:.78rem;margin-bottom:.7rem">📍 {result["destination"]}{target_line} · 🗓 {result["checkin"]} → {result["checkout"]} · 🌙 {result["nights"]} nights</div>
          <div style="border-left:3px solid {cfg["color"]};padding-left:14px;color:#a8b8d0;font-size:.84rem;line-height:1.65">{analysis_html}</div>
        </div>
        <div style="text-align:right;min-width:170px">
          <div style="color:#64748b;font-size:.66rem;font-weight:700;letter-spacing:1.5px">BEST PRICE / NIGHT</div>
          <div style="color:#f1f5f9;font-family:'Plus Jakarta Sans',sans-serif;font-size:3rem;font-weight:800;line-height:1.1">{best_price:,.0f}<span style="font-size:1.1rem;color:#64748b"> {cur}</span></div>
          <div style="color:{cfg["color"]};font-size:.8rem;font-weight:600">via {dname(cheapest.get("vendor", ""))}</div>
          <div style="color:#10b981;font-size:.75rem;margin-top:4px">Save {save_vs_avg:,.0f} vs avg</div>
          <div style="color:#475569;font-size:.7rem">Market avg: {market_avg:,.0f}</div>
        </div>
      </div>
    """, border=cfg["color"] + "66",
         bg="linear-gradient(135deg,#07140f 0%,#0a1830 60%,#0d1a35 100%)"
            if reco["action"] == "BOOK_NOW" else "#0c1730",
         pad="1.5rem", radius="16px")

# ── Metric tiles row ───────────────────────────────────────────────────────────
live_count = summary.get("vendors_live", 0)
multi_count = summary.get("hotels_across_multiple_vendors", 0)
price_index_val, price_index_chip = "—", ""
if tl and tl.get("points"):
    valid = [p for p in tl["points"] if p.get("sample_size")]
    if valid and tl.get("current_window_avg"):
        scan_avg = statistics.fmean([p["avg_price"] for p in valid])
        if scan_avg > 0:
            pi = round(tl["current_window_avg"] / scan_avg * 100)
            price_index_val = str(pi)
            price_index_chip = f"{pi - 100:+d}% vs scan avg"

m = st.columns(6)
with m[0]:
    _metric_tile("Hotels Found", f'{result.get("total_hotels_found", 0)}')
with m[1]:
    _metric_tile("Vendors", str(live_count),
                 f'{live_count}/{summary.get("vendors_total", 0)} live', "#3b82f6")
with m[2]:
    _metric_tile("Cross-Matched", str(multi_count), "↑ properties" if multi_count else "",
                 "#10b981")
with m[3]:
    _metric_tile("Market Low", f"{result.get('market_lowest_price', 0):,.0f}",
                 f'↑ via {dname(cheapest.get("vendor", ""))}' if cheapest.get("vendor") else "",
                 "#10b981")
with m[4]:
    _metric_tile("Market Avg", f"{market_avg:,.0f}")
with m[5]:
    _metric_tile("Price Index", price_index_val, price_index_chip,
                 "#ef4444" if price_index_chip.startswith("+") else "#10b981")

# ── Tabs ───────────────────────────────────────────────────────────────────────
t1, t2, t3, t4, t5 = st.tabs(["🏨 Vendor Results", "💰 Price Comparison",
                              "📊 Seasonal Analysis", "🧠 AI Intelligence",
                              "📈 Market Overview"])
with t1:
    tab_vendor_results(result, order, cur)
with t2:
    tab_price_comparison(result, order, cur)
with t3:
    tab_seasonal(result, cur)
with t4:
    tab_ai(result, dname, cur)
with t5:
    tab_market(result, order, cur)

# ── Footer + diagnostics ───────────────────────────────────────────────────────
_html(f"""
<div style="display:flex;justify-content:space-between;border-top:1px solid #0f2044;margin-top:1.2rem;padding-top:.8rem;color:#475569;font-size:.72rem">
  <span>🔎 Search ID: {result["search_id"]}</span>
  <span>⚡ Completed in <b style="color:#3b82f6">{result["total_search_time_ms"]:,} ms</b></span>
  <span>🏨 {result.get("total_hotels_found", 0)} hotels · {live_count} vendors · {result["nights"]} nights</span>
</div>""")

with st.expander("🛰️ Agent orchestration trace"):
    for step in result.get("agent_trace", []):
        icon = {"completed": "✅", "failed": "❌", "skipped": "⏭️"}.get(step["status"], "•")
        st.markdown(
            f'{icon} **{step["agent"]}** — {step["status"]} · {step["duration_ms"]} ms  \n'
            f'<span style="color:#64748b;font-size:.8rem">{step.get("detail", "")}</span>',
            unsafe_allow_html=True,
        )

st.download_button(
    "⬇️ Download full response (JSON)",
    data=json.dumps(result, indent=2, default=str),
    file_name=f'ubidstay_{result["search_id"]}.json',
    mime="application/json",
)
