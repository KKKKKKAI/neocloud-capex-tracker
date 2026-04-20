"""Interactive Plotly chart for GitHub Pages.

Generates one self-contained HTML per metric (cloud_segment_revenue,
revenue, capital_expenditures, operating_cash_flow). Each page has:

- Stacked bar chart (company breakdown)
- Aggregate YoY %  (dashed line, visible by default)
- Aggregate QoQ %  (dotted line, legendonly by default, quarterly view only)
- Annual ↔ Quarterly toggle
- Cross-navigation bar linking the four metric pages
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
DB_PATH = REPO_ROOT / "data" / "db" / "capex.db"
DOCS_DIR = REPO_ROOT / "docs"

STACK_ORDER = [
    "AMZN", "MSFT", "GOOGL", "META", "ORCL", "0700", "CRWV",
    "GDS", "BABA", "BIDU", "APLD", "IREN", "NBIS",
]
COLORS = {
    "AMZN": "#FF9900", "MSFT": "#00A4EF", "GOOGL": "#4285F4",
    "META": "#1877F2", "ORCL": "#F80000", "0700": "#00B140",
    "CRWV": "#7D3C98", "GDS": "#1A5276", "BABA": "#FF6A00",
    "BIDU": "#2932E1", "APLD": "#2E86C1", "IREN": "#27AE60",
    "NBIS": "#F39C12",
}
LABELS = {"0700": "Tencent*"}
NBIS_START = 2024

# Configuration for every chart page we emit.
METRIC_CONFIGS: dict[str, dict[str, Any]] = {
    "cloud_segment_revenue": {
        "page_name": "index.html",
        "nav_label": "Cloud / DC Revenue",
        "chart_title": "Cloud / Datacenter Revenue — AI Infrastructure Ecosystem",
        "yaxis_title": "Cloud / DC Revenue ($B USD)",
        "metric_suffix": "$B",
        "exclude_tickers": {"META"},  # infrastructure buyer, no cloud segment
    },
    "revenue": {
        "page_name": "revenue.html",
        "nav_label": "Total Revenue",
        "chart_title": "Total Revenue — AI Infrastructure Ecosystem",
        "yaxis_title": "Total Revenue ($B USD)",
        "metric_suffix": "$B",
        "exclude_tickers": set(),
    },
    "capital_expenditures": {
        "page_name": "capex.html",
        "nav_label": "CapEx",
        "chart_title": "Capital Expenditures — AI Infrastructure Ecosystem",
        "yaxis_title": "CapEx ($B USD)",
        "metric_suffix": "$B",
        "exclude_tickers": {"0700"},  # only HKEX annual, fragmentary
    },
    "operating_cash_flow": {
        "page_name": "operating_cash_flow.html",
        "nav_label": "Operating Cash Flow",
        "chart_title": "Operating Cash Flow — AI Infrastructure Ecosystem",
        "yaxis_title": "Operating Cash Flow ($B USD)",
        "metric_suffix": "$B",
        "exclude_tickers": {"0700"},
    },
}


def generate_interactive(
    output: str | Path | None = None,
    db_path: str | Path | None = None,
    metric_key: str = "cloud_segment_revenue",
) -> Path:
    """Generate one interactive HTML chart for the given metric."""
    cfg = METRIC_CONFIGS.get(metric_key)
    if cfg is None:
        raise ValueError(f"unknown metric_key: {metric_key!r}")

    output = Path(output or DOCS_DIR / cfg["page_name"])
    output.parent.mkdir(parents=True, exist_ok=True)
    db_path = db_path or DB_PATH

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    annual = _load_annual(conn, metric_key, cfg["exclude_tickers"])
    quarterly = _load_quarterly(conn, metric_key, cfg["exclude_tickers"])
    conn.close()

    html = _build_html(annual, quarterly, metric_key, cfg)
    output.write_text(html, encoding="utf-8")
    return output


def generate_all_interactive(
    db_path: str | Path | None = None,
    out_dir: str | Path | None = None,
) -> list[Path]:
    """Emit all 4 metric chart pages. Returns the list of written paths."""
    out_dir = Path(out_dir or DOCS_DIR)
    paths: list[Path] = []
    for metric_key, cfg in METRIC_CONFIGS.items():
        paths.append(generate_interactive(
            output=out_dir / cfg["page_name"],
            db_path=db_path,
            metric_key=metric_key,
        ))
    return paths


def _load_annual(
    conn, metric_key: str, exclude_tickers: set[str] | None = None,
) -> dict[str, Any]:
    """Load annual values for the given metric."""
    exclude = exclude_tickers or set()
    rows = conn.execute(
        "SELECT sd.ticker, sd.fiscal_year, "
        "COALESCE(e.value_usd, e.value) as val "
        "FROM extractions e "
        "JOIN source_documents sd ON e.source_document_id = sd.id "
        "WHERE e.metric_key = ? "
        "AND sd.period_token = 'AR' "
        "AND e.period_type = 'FY'",
        (metric_key,),
    ).fetchall()

    by_year: dict[int, dict[str, float]] = {}
    for r in rows:
        fy, t, v = r["fiscal_year"], r["ticker"], r["val"]
        if t in exclude:
            continue
        if not v or v <= 0 or (t == "NBIS" and fy < NBIS_START):
            continue
        by_year.setdefault(fy, {})[t] = v

    years = sorted(y for y in by_year if y >= 2015)
    return {"years": years, "by_year": by_year}


def _calendar_qtr_from_fy(fye_month: int, fy: int, ptype: str) -> str | None:
    """Calendar-quarter label for a fiscal (fy, period_type).

    fye_month: company's fiscal year-end month (e.g. 12 for Dec-FYE).
    fy: fiscal year label per the filer (e.g. MSFT FY2025 ends 2025-06-30).
    ptype: 'Q1','Q2','Q3','Q4' — fiscal quarter position.
    """
    pos = {"Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4}.get(ptype)
    if pos is None:
        return None
    # FY Q4 ends on FYE month of fy. FY Q1 ends 9 months before FY Q4.
    # Derive the *end* month of each fiscal quarter:
    end_month_q4 = fye_month
    end_month_q4_year = fy
    # Compute end-month for the target quarter (subtract 3 months per step back)
    offset_months = 3 * (4 - pos)  # Q4→0, Q3→3, Q2→6, Q1→9
    end_month = end_month_q4 - offset_months
    end_year = end_month_q4_year
    while end_month <= 0:
        end_month += 12
        end_year -= 1
    # Calendar quarter of that end-month
    cal_q = (end_month - 1) // 3 + 1
    return f"{end_year}Q{cal_q}"


def _load_quarterly(
    conn, metric_key: str, exclude_tickers: set[str] | None = None,
) -> dict[str, Any]:
    """Load quarterly values strictly from period_type rows."""
    exclude = exclude_tickers or set()
    fye = {
        r["ticker"]: r["fiscal_year_end_month"]
        for r in conn.execute(
            "SELECT ticker, fiscal_year_end_month FROM companies"
        )
    }
    rows = conn.execute(
        """
        SELECT sd.ticker, sd.period_of_report, sd.fiscal_year,
               e.period_type,
               COALESCE(e.value_usd, e.value) as val
        FROM extractions e
        JOIN source_documents sd ON e.source_document_id = sd.id
        WHERE e.metric_key = ?
          AND e.period_type IN ('Q1','Q2','Q3','Q4')
          AND e.value_usd IS NOT NULL
          AND e.id = (
            SELECT e2.id FROM extractions e2
            JOIN source_documents sd2 ON e2.source_document_id = sd2.id
            WHERE sd2.ticker = sd.ticker
              AND sd2.fiscal_year = sd.fiscal_year
              AND e2.metric_key = ?
              AND e2.period_type = e.period_type
              AND e2.value_usd IS NOT NULL
            ORDER BY
              CASE WHEN e2.extraction_type = 'direct' THEN 0
                   WHEN e2.extraction_type = 'inferred' THEN 1
                   ELSE 2 END,
              e2.extracted_at DESC
            LIMIT 1
          )
        ORDER BY sd.ticker, sd.fiscal_year
        """,
        (metric_key, metric_key),
    ).fetchall()

    by_quarter: dict[str, dict[str, float]] = {}
    for r in rows:
        t = r["ticker"]
        if t in exclude:
            continue
        if t == "NBIS" and r["fiscal_year"] < NBIS_START:
            continue
        v = abs(r["val"]) if r["val"] else 0
        if v <= 0:
            continue
        # Label by calendar quarter. For *derived* Q1/Q2/Q3/Q4 rows
        # (anchored to the 10-K period_of_report), compute the label
        # from fiscal_year + period_type + company FYE month. For
        # standalone rows anchored to the matching 10-Q, the
        # period_of_report IS the quarter end, so _qlabel() works.
        period = r["period_of_report"]
        ptype = r["period_type"]
        period_month = int(period[5:7])
        fye_month = fye.get(t, 12)
        # If the row's period_of_report falls on the fiscal year-end
        # and the period_type is a quarter (derived case), derive label
        # from (fy, ptype, fye_month) instead.
        if ptype in ("Q1", "Q2", "Q3", "Q4") and period_month == fye_month:
            derived_label = _calendar_qtr_from_fy(fye_month, r["fiscal_year"], ptype)
            label = derived_label or _qlabel(period)
        else:
            label = _qlabel(period)
        by_quarter.setdefault(t, {})[label] = v

    all_qs = sorted(
        {q for d in by_quarter.values() for q in d
         if int(q[:4]) >= 2019},
        key=_qsort_key,
    )
    return {"quarters": all_qs, "by_quarter": by_quarter}


def _q4_label_for(period: str, fiscal_year: int) -> str:
    """Calendar-quarter label for a derived Q4 value.

    period is the anchor source_doc's period_of_report (usually the
    annual filing's fiscal year-end). Q4 of a fiscal year is the
    calendar quarter of that FY end date.
    """
    return _qlabel(period)


def _calendar_quarter(period: str) -> tuple[int, int]:
    """'2025-09-30' -> (2025, 3). Calendar quarter from period_of_report."""
    y, m = int(period[:4]), int(period[5:7])
    return (y, (m - 1) // 3 + 1)


def _qlabel(period: str) -> str:
    y, q = _calendar_quarter(period)
    return f"{y}Q{q}"


def _qsort_key(label: str) -> tuple[int, int]:
    return (int(label[:4]), int(label[5:]))


def _build_html(
    annual: dict, quarterly: dict, metric_key: str, cfg: dict[str, Any],
) -> str:
    """Build the complete HTML for the given metric."""
    years = annual["years"]
    by_year = annual["by_year"]
    x_annual = [f"FY{y}" for y in years]
    quarters = quarterly["quarters"]
    by_quarter = quarterly["by_quarter"]

    annual_traces = []
    for co in STACK_ORDER:
        vals = [by_year[y].get(co, 0) / 1000 for y in years]
        if sum(vals) == 0:
            continue
        label = LABELS.get(co, co)
        annual_traces.append({
            "name": label,
            "x": x_annual,
            "y": vals,
            "type": "bar",
            "marker": {"color": COLORS.get(co, "#888")},
        })

    quarterly_traces = []
    for co in STACK_ORDER:
        qdata = by_quarter.get(co, {})
        vals = [qdata.get(q, 0) / 1000 for q in quarters]
        if sum(vals) == 0:
            continue
        label = LABELS.get(co, co)
        quarterly_traces.append({
            "name": label,
            "x": quarters,
            "y": vals,
            "type": "bar",
            "marker": {"color": COLORS.get(co, "#888")},
        })

    nav_html = _build_nav_html(metric_key)

    return _HTML_TEMPLATE.format(
        annual_traces_json=json.dumps(annual_traces),
        quarterly_traces_json=json.dumps(quarterly_traces),
        x_annual_json=json.dumps(x_annual),
        x_quarterly_json=json.dumps(quarters),
        has_quarterly="true" if quarterly_traces else "false",
        page_title=cfg["chart_title"],
        chart_title=cfg["chart_title"],
        yaxis_title=cfg["yaxis_title"],
        nav_html=nav_html,
    )


# Non-metric pages that appear in the nav bar after the metric pills.
NAV_EXTRAS: list[dict[str, str]] = [
    {"key": "calendar", "page_name": "calendar.html", "nav_label": "Calendar"},
    {"key": "treatments", "page_name": "treatments.html",
     "nav_label": "Treatments"},
]


def _build_nav_html(current_key: str) -> str:
    """Build the cross-navigation bar linking the metric pages + extras.

    `current_key` is either a metric_key from METRIC_CONFIGS or the key of
    a NAV_EXTRAS entry (e.g. "calendar"). Unknown keys simply render no
    pill as active.
    """
    pills = []
    for mk, c in METRIC_CONFIGS.items():
        cls = "nav-pill active" if mk == current_key else "nav-pill"
        pills.append(
            f'<a class="{cls}" href="{c["page_name"]}">{c["nav_label"]}</a>'
        )
    for extra in NAV_EXTRAS:
        cls = "nav-pill active" if extra["key"] == current_key else "nav-pill"
        pills.append(
            f'<a class="{cls}" href="{extra["page_name"]}">{extra["nav_label"]}</a>'
        )
    return '<div class="nav">' + "".join(pills) + "</div>"


def build_nav_html(current_key: str) -> str:
    """Public re-export of the nav helper for use by other exporters."""
    return _build_nav_html(current_key)


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{page_title}</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         margin: 0; padding: 20px; background: #fafafa; }}
  #chart {{ width: 100%; max-width: 1400px; margin: 0 auto; }}
  .nav {{ text-align: center; margin: 0 auto 14px; max-width: 1400px; }}
  .nav-pill {{ display: inline-block; padding: 6px 14px; margin: 0 4px;
    border: 1px solid #888; border-radius: 18px; color: #555; font-size: 13px;
    text-decoration: none; background: #fff; }}
  .nav-pill:hover {{ border-color: #2E75B6; color: #2E75B6; }}
  .nav-pill.active {{ background: #2E75B6; color: #fff; border-color: #2E75B6;
    font-weight: bold; }}
  .controls {{ text-align: center; margin: 10px 0; }}
  .controls .group {{ display: inline-block; margin: 0 12px; }}
  .controls button {{ padding: 8px 20px; margin: 0 5px; border: 2px solid #2E75B6;
    background: white; color: #2E75B6; cursor: pointer; border-radius: 4px;
    font-size: 14px; font-weight: bold; }}
  .controls button.active {{ background: #2E75B6; color: white; }}
  .controls button:hover {{ background: #2E75B6; color: white; }}
  .note {{ text-align: center; font-size: 11px; color: #888; margin-top: 5px; }}
</style>
</head>
<body>
{nav_html}
<div id="chart">
  <div class="controls">
    <span class="group">
      <button id="btn-annual" class="active" onclick="setView('annual')">Annual</button>
      <button id="btn-quarterly" onclick="setView('quarterly')">Quarterly</button>
    </span>
    <span class="group">
      <button onclick="selectAll()">Select All</button>
      <button onclick="deselectAll()">Deselect All</button>
    </span>
  </div>
  <div id="plotDiv" style="width:100%;height:700px;"></div>
  <div class="note">
    Click legend entries to toggle any series — companies, the aggregate
    <b>YoY %</b> line, or the aggregate <b>QoQ %</b> line.
    Aggregate lines recompute from the currently-visible companies.
    QoQ is only shown in Quarterly view (in Annual view it would equal YoY).
    *Tencent: FinTech &amp; Business Services proxy (includes WeChat Pay).
    <br>Quarterly view: SEC 10-Q filers and BABA 6-K; other 20-F / HKEX filers annual only.
    <br>Source:
    <a href="https://github.com/KKKKKKAI/neocloud-capex-tracker">
    neocloud-capex-tracker</a>
  </div>
</div>

<script>
/*
 * Single chart layout: stacked bars + up to two aggregate growth
 * overlay lines on yaxis2 (YoY visible by default, QoQ hidden-in-legend
 * by default; both toggleable via legend click). QoQ is omitted in
 * Annual view where it would equal YoY.
 *
 * Growth math mirrors src/capex/exporters/_growth.py — keep in lock-step.
 */
var CFG_YOY_MIN = -10;
var CFG_YOY_MAX = 100;
var CFG_LABEL_YSHIFT = 15;

// ---- DATA ----
var annualTraces = {annual_traces_json};
var quarterlyTraces = {quarterly_traces_json};
var xAnnual = {x_annual_json};
var xQuarterly = {x_quarterly_json};
var hasQuarterly = {has_quarterly};
var currentView = 'annual';

// ---- RUNTIME STATE ----
var plotDiv = document.getElementById('plotDiv');
var allTraces = [];
var yoyOverlayIdx = -1;
var qoqOverlayIdx = -1;

// ---- GROWTH HELPERS (mirror of _growth.py) ----
function priorIndexFor(i, xLabels, mode, period) {{
    if (mode === 'qoq') return i - 1;
    if (period === 'annual') return i - 1;
    // quarterly yoy: same-quarter one year prior
    var cur = xLabels[i];
    if (!cur || cur.length < 6 || cur[4] !== 'Q') return -1;
    var year = parseInt(cur.slice(0, 4), 10);
    if (isNaN(year)) return -1;
    var target = (year - 1) + cur.slice(4);
    return xLabels.indexOf(target);
}}

function computeGrowth(series, xLabels, mode, period) {{
    var out = [];
    for (var i = 0; i < xLabels.length; i++) {{
        var pi = priorIndexFor(i, xLabels, mode, period);
        if (pi < 0 || pi >= series.length) {{ out.push(null); continue; }}
        var cur = series[i], prev = series[pi];
        if (cur === null || prev === null || !prev || !cur) {{
            out.push(null);
        }} else {{
            out.push((cur / prev - 1) * 100);
        }}
    }}
    return out;
}}

function calcTotals(xLabels) {{
    // Sum all visible BAR traces.
    var n = xLabels.length;
    var totals = new Array(n).fill(0);
    for (var i = 0; i < allTraces.length; i++) {{
        var t = allTraces[i];
        if (t.type !== 'bar' || t.visible === 'legendonly') continue;
        for (var j = 0; j < t.y.length; j++) {{
            totals[j] += (t.y[j] || 0);
        }}
    }}
    return totals;
}}

function makeAnnotations(totals, xLabels) {{
    var anns = [];
    for (var i = 0; i < totals.length; i++) {{
        if (totals[i] > 0) {{
            anns.push({{
                x: xLabels[i],
                y: totals[i],
                text: '<b>$' + Math.round(totals[i]) + 'B</b>',
                showarrow: false,
                yshift: CFG_LABEL_YSHIFT,
                font: {{ size: 11 }}
            }});
        }}
    }}
    return anns;
}}

function growthSeriesForPlot(totals, xLabels, mode, period) {{
    // Pack (x,y,text) triples for non-null growth values.
    var vals = computeGrowth(totals, xLabels, mode, period);
    var oX = [], oY = [], oText = [];
    for (var i = 0; i < vals.length; i++) {{
        if (vals[i] !== null) {{
            oX.push(xLabels[i]);
            oY.push(vals[i]);
            oText.push(vals[i].toFixed(0) + '%');
        }}
    }}
    return {{ x: oX, y: oY, text: oText }};
}}

// ---- TRACE BUILDERS ----
function buildBarsAndOverlays(dataTraces, xLabels, period) {{
    allTraces = [];
    yoyOverlayIdx = -1;
    qoqOverlayIdx = -1;

    dataTraces.forEach(function(t) {{
        allTraces.push({{
            name: t.name,
            x: t.x,
            y: t.y,
            type: 'bar',
            marker: t.marker,
            hovertemplate: '<b>' + t.name + '</b><br>$%{{y:.1f}}B<extra></extra>'
        }});
    }});

    var totals = calcTotals(xLabels);

    // Aggregate YoY line — visible by default.
    var yoy = growthSeriesForPlot(totals, xLabels, 'yoy', period);
    yoyOverlayIdx = allTraces.length;
    allTraces.push({{
        name: 'Aggregate YoY %',
        x: yoy.x, y: yoy.y,
        type: 'scatter',
        mode: 'lines+markers+text',
        line: {{ color: '#2C3E50', width: 3, dash: 'dash' }},
        marker: {{ size: 8 }},
        text: yoy.text,
        textposition: 'top center',
        textfont: {{ size: 10, color: '#2C3E50' }},
        yaxis: 'y2',
        hovertemplate: '%{{x}}: %{{y:.1f}}% YoY<extra></extra>'
    }});

    // Aggregate QoQ line — Quarterly view only, legendonly by default.
    if (period === 'quarterly') {{
        var qoq = growthSeriesForPlot(totals, xLabels, 'qoq', period);
        qoqOverlayIdx = allTraces.length;
        allTraces.push({{
            name: 'Aggregate QoQ %',
            x: qoq.x, y: qoq.y,
            type: 'scatter',
            mode: 'lines+markers+text',
            visible: 'legendonly',
            line: {{ color: '#C65A1A', width: 2, dash: 'dot' }},
            marker: {{ size: 7, color: '#C65A1A' }},
            text: qoq.text,
            textposition: 'bottom center',
            textfont: {{ size: 10, color: '#C65A1A' }},
            yaxis: 'y2',
            hovertemplate: '%{{x}}: %{{y:.1f}}% QoQ<extra></extra>'
        }});
    }}

    return totals;
}}

// ---- MAIN RENDER ----
function renderChart() {{
    var xLabels = (currentView === 'annual') ? xAnnual : xQuarterly;
    var period = currentView;
    var dataTraces = (currentView === 'annual') ? annualTraces : quarterlyTraces;

    if (currentView === 'quarterly' && (!dataTraces || dataTraces.length === 0)) {{
        plotDiv.innerHTML = '<p style="text-align:center;padding:200px 0;color:#888;">' +
            'Quarterly revenue data not yet available.</p>';
        return;
    }}

    var totals = buildBarsAndOverlays(dataTraces, xLabels, period);

    var subtitle = (currentView === 'annual')
        ? 'Aggregated annual, USD-normalized. ' +
          'Dashed = aggregate YoY %.'
        : 'Quarterly. Dashed = aggregate YoY %; ' +
          'dotted = aggregate QoQ % (click legend to show).';

    var layout = {{
        barmode: 'stack',
        title: {{
            text: '{chart_title}<br>' +
                  '<sub>' + subtitle + '</sub>',
            font: {{ size: 18 }}, x: 0.5
        }},
        xaxis: {{ title: 'Period', tickfont: {{ size: 11 }} }},
        yaxis: {{ title: '{yaxis_title}', gridcolor: '#E5E5E5' }},
        yaxis2: {{
            title: 'Aggregate Growth (%)',
            overlaying: 'y', side: 'right',
            showgrid: false,
            ticksuffix: '%',
            range: [CFG_YOY_MIN, CFG_YOY_MAX],
            fixedrange: true
        }},
        legend: {{
            orientation: 'v', x: 1.02, y: 1.0,
            xanchor: 'left', yanchor: 'top',
            font: {{ size: 11 }}, tracegroupgap: 2
        }},
        hovermode: 'x unified',
        plot_bgcolor: 'white',
        annotations: makeAnnotations(totals, xLabels),
        margin: {{ t: 80, b: 60, r: 150 }}
    }};

    Plotly.newPlot(plotDiv, allTraces, layout, {{
        displayModeBar: true,
        modeBarButtonsToRemove: ['lasso2d', 'select2d']
    }});

    plotDiv.on('plotly_legendclick', function() {{
        setTimeout(recalcOverlays, 100);
    }});
    plotDiv.on('plotly_legenddoubleclick', function() {{
        setTimeout(recalcOverlays, 100);
    }});
}}

function recalcOverlays() {{
    var xLabels = (currentView === 'annual') ? xAnnual : xQuarterly;
    var data = plotDiv.data;
    var n = xLabels.length;

    var totals = new Array(n).fill(0);
    for (var i = 0; i < data.length; i++) {{
        if (data[i].type === 'bar' && data[i].visible !== 'legendonly') {{
            for (var j = 0; j < data[i].y.length; j++) {{
                totals[j] += (data[i].y[j] || 0);
            }}
        }}
    }}

    if (yoyOverlayIdx >= 0) {{
        var yoy = growthSeriesForPlot(totals, xLabels, 'yoy', currentView);
        Plotly.restyle(plotDiv, {{
            x: [yoy.x], y: [yoy.y], text: [yoy.text]
        }}, [yoyOverlayIdx]);
    }}
    if (qoqOverlayIdx >= 0 && currentView === 'quarterly') {{
        var qoq = growthSeriesForPlot(totals, xLabels, 'qoq', currentView);
        Plotly.restyle(plotDiv, {{
            x: [qoq.x], y: [qoq.y], text: [qoq.text]
        }}, [qoqOverlayIdx]);
    }}

    Plotly.relayout(plotDiv, {{
        annotations: makeAnnotations(totals, xLabels)
    }});
}}

// ---- SELECT / DESELECT ALL ----
function selectAll() {{
    var indices = [];
    for (var i = 0; i < plotDiv.data.length; i++) indices.push(i);
    Plotly.restyle(plotDiv, {{ visible: true }}, indices);
    setTimeout(recalcOverlays, 100);
}}

function deselectAll() {{
    // Hide all BAR traces; leave the aggregate overlay lines intact so
    // the user keeps context after a deselect-all click.
    var indices = [];
    for (var i = 0; i < plotDiv.data.length; i++) {{
        if (plotDiv.data[i].type === 'bar') indices.push(i);
    }}
    Plotly.restyle(plotDiv, {{ visible: 'legendonly' }}, indices);
    setTimeout(recalcOverlays, 100);
}}

// ---- VIEW TOGGLE ----
function setView(view) {{
    currentView = view;
    document.getElementById('btn-annual').classList.toggle('active', view === 'annual');
    document.getElementById('btn-quarterly').classList.toggle('active', view === 'quarterly');
    renderChart();
}}

// ---- INIT ----
renderChart();
</script>
</body>
</html>"""
