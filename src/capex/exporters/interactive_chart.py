"""Interactive Plotly chart for GitHub Pages.

Features:
- Stacked bar chart (cloud/DC revenue by company)
- Click legend to toggle companies — YoY growth line RECALCULATES
  dynamically based on visible companies only
- Hover tooltips with exact values + % of total
- Annual ↔ Quarterly toggle button
- Total $B annotations update when companies are toggled
- Fully self-contained HTML — no server needed

NOTE: Quarterly data uses cloud_segment_revenue where available,
falling back to total group revenue for companies without quarterly
segment breakdowns. Annual data uses cloud_segment_revenue only.
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
    "AMZN", "MSFT", "GOOGL", "ORCL", "0700", "CRWV",
    "GDS", "BABA", "BIDU", "APLD", "IREN", "NBIS",
]
COLORS = {
    "AMZN": "#FF9900", "MSFT": "#00A4EF", "GOOGL": "#4285F4",
    "ORCL": "#F80000", "0700": "#00B140", "CRWV": "#7D3C98",
    "GDS": "#1A5276", "BABA": "#FF6A00", "BIDU": "#2932E1",
    "APLD": "#2E86C1", "IREN": "#27AE60", "NBIS": "#F39C12",
}
LABELS = {"0700": "Tencent*"}
NBIS_START = 2024
FLOW_METRICS = {"cloud_segment_revenue", "revenue"}


def generate_interactive(
    output: str | Path | None = None,
    db_path: str | Path | None = None,
) -> Path:
    """Generate interactive Plotly HTML chart with dynamic YoY."""
    output = Path(output or DOCS_DIR / "index.html")
    output.parent.mkdir(parents=True, exist_ok=True)
    db_path = db_path or DB_PATH

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    annual = _load_annual(conn)
    quarterly = _load_quarterly(conn)
    conn.close()

    html = _build_html(annual, quarterly)
    output.write_text(html, encoding="utf-8")
    return output


def _load_annual(conn) -> dict[str, Any]:
    """Load annual cloud segment revenue data."""
    rows = conn.execute(
        "SELECT sd.ticker, sd.fiscal_year, "
        "COALESCE(e.value_usd, e.value) as val "
        "FROM extractions e "
        "JOIN source_documents sd ON e.source_document_id = sd.id "
        "WHERE e.metric_key = 'cloud_segment_revenue' "
        "AND sd.period_token = 'AR'"
    ).fetchall()

    by_year: dict[int, dict[str, float]] = {}
    for r in rows:
        fy, t, v = r["fiscal_year"], r["ticker"], r["val"]
        if not v or v <= 0 or (t == "NBIS" and fy < NBIS_START):
            continue
        by_year.setdefault(fy, {})[t] = v

    years = sorted(y for y in by_year if y >= 2015)
    return {"years": years, "by_year": by_year}


def _load_quarterly(conn) -> dict[str, Any]:
    """Load quarterly cloud segment revenue strictly from period_type rows.

    Reads only `cloud_segment_revenue` with period_type in
    ('Q1','Q2','Q3','Q4'). Values are already standalone (either stored
    directly or derived by the reconcile engine). If a company-quarter
    has no cloud_segment_revenue row, the chart simply has no bar for
    that cell — we never substitute total revenue, which would
    mislabel non-cloud data as cloud.
    """
    rows = conn.execute(
        """
        SELECT sd.ticker, sd.period_of_report, sd.fiscal_year,
               e.period_type,
               COALESCE(e.value_usd, e.value) as val
        FROM extractions e
        JOIN source_documents sd ON e.source_document_id = sd.id
        WHERE e.metric_key = 'cloud_segment_revenue'
          AND e.period_type IN ('Q1','Q2','Q3','Q4')
          AND e.value_usd IS NOT NULL
          AND e.id = (
            SELECT e2.id FROM extractions e2
            JOIN source_documents sd2 ON e2.source_document_id = sd2.id
            WHERE sd2.ticker = sd.ticker
              AND sd2.fiscal_year = sd.fiscal_year
              AND e2.metric_key = 'cloud_segment_revenue'
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
        """
    ).fetchall()

    by_quarter: dict[str, dict[str, float]] = {}
    for r in rows:
        t = r["ticker"]
        if t == "NBIS" and r["fiscal_year"] < NBIS_START:
            continue
        v = abs(r["val"]) if r["val"] else 0
        if v <= 0:
            continue
        # Label each bar by the CALENDAR quarter of the value's period.
        # For period_type='Q4' (derived, anchored to the annual filing),
        # we use the annual period_of_report's calendar quarter. For
        # standalone Q1/Q2/Q3 (anchored to the 10-Q), the 10-Q's own
        # period_of_report is correct.
        period = r["period_of_report"]
        if r["period_type"] == "Q4":
            label = _q4_label_for(period, r["fiscal_year"])
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


def _build_html(annual: dict, quarterly: dict) -> str:
    """Build the complete HTML with Plotly + custom JS."""
    years = annual["years"]
    by_year = annual["by_year"]
    x_annual = [f"FY{y}" for y in years]
    quarters = quarterly["quarters"]
    by_quarter = quarterly["by_quarter"]

    # Build trace data for JS
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

    return _HTML_TEMPLATE.format(
        annual_traces_json=json.dumps(annual_traces),
        quarterly_traces_json=json.dumps(quarterly_traces),
        x_annual_json=json.dumps(x_annual),
        x_quarterly_json=json.dumps(quarters),
        has_quarterly="true" if quarterly_traces else "false",
    )


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Cloud/Datacenter Revenue — AI Infrastructure Ecosystem</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         margin: 0; padding: 20px; background: #fafafa; }}
  #chart {{ width: 100%; max-width: 1400px; margin: 0 auto; }}
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
        ? '12 companies, aggregated annual, USD-normalized. ' +
          'Dashed = aggregate YoY %.'
        : 'Quarterly cloud/DC revenue. Dashed = aggregate YoY %; ' +
          'dotted = aggregate QoQ % (click legend to show).';

    var layout = {{
        barmode: 'stack',
        title: {{
            text: 'Cloud/Datacenter Revenue — AI Infrastructure Ecosystem<br>' +
                  '<sub>' + subtitle + '</sub>',
            font: {{ size: 18 }}, x: 0.5
        }},
        xaxis: {{ title: 'Period', tickfont: {{ size: 11 }} }},
        yaxis: {{ title: 'Cloud/DC Revenue ($B USD)', gridcolor: '#E5E5E5' }},
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
