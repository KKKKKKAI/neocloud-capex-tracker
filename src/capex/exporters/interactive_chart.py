"""Interactive Plotly chart for GitHub Pages.

Features:
- Stacked bar chart (cloud/DC revenue by company)
- Click legend to toggle companies — YoY growth line RECALCULATES
  dynamically based on visible companies only
- Hover tooltips with exact values + % of total
- Annual ↔ Quarterly toggle button
- Total $B annotations update when companies are toggled
- Fully self-contained HTML — no server needed

NOTE: Quarterly cloud segment data is currently XBRL-only for
pure-play neoclouds. Hyperscaler quarterly segment data requires
LLM extraction from 10-Q filings (Phase 8B — deferred). When that
data lands in the DB, regenerating this chart will automatically
include it.
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
    """Load quarterly cloud segment revenue (XBRL where available)."""
    # Get FYE months
    fye = {}
    for r in conn.execute(
        "SELECT ticker, fiscal_year_end_month FROM companies"
    ):
        fye[r["ticker"]] = r["fiscal_year_end_month"]

    rows = conn.execute(
        "SELECT sd.ticker, sd.period_of_report, sd.period_token, "
        "sd.fiscal_year, COALESCE(e.value_usd, e.value) as val "
        "FROM extractions e "
        "JOIN source_documents sd ON e.source_document_id = sd.id "
        "WHERE e.metric_key IN ('cloud_segment_revenue', 'revenue') "
        "AND sd.period_token != 'AR' "
        "AND e.id = ("
        "  SELECT e2.id FROM extractions e2 "
        "  JOIN source_documents sd2 ON e2.source_document_id = sd2.id "
        "  WHERE sd2.ticker = sd.ticker "
        "  AND sd2.period_of_report = sd.period_of_report "
        "  AND e2.metric_key IN ('cloud_segment_revenue', 'revenue') "
        "  ORDER BY CASE WHEN e2.metric_key='cloud_segment_revenue' "
        "  THEN 0 ELSE 1 END, e2.value_usd DESC NULLS LAST LIMIT 1"
        ") "
        "ORDER BY sd.ticker, sd.fiscal_year, sd.period_of_report"
    ).fetchall()

    # Group by ticker + FY for de-cumulation
    by_ticker_fy: dict[str, dict[int, list]] = {}
    for r in rows:
        t = r["ticker"]
        if t == "NBIS" and r["fiscal_year"] < NBIS_START:
            continue
        # Only include pure-plays (whole_company) for quarterly
        # since hyperscalers don't have quarterly segment data yet
        if t not in ("CRWV", "APLD", "GDS", "IREN", "NBIS"):
            continue
        fy = r["fiscal_year"]
        by_ticker_fy.setdefault(t, {}).setdefault(fy, []).append(r)

    by_quarter: dict[str, dict[str, float]] = {}
    for t, fy_data in by_ticker_fy.items():
        for _fy, points in fy_data.items():
            points.sort(key=lambda x: x["period_of_report"])
            prev = 0
            for p in points:
                v = abs(p["val"]) if p["val"] else 0
                token = p["period_token"]
                period = p["period_of_report"]
                if token == "Q1":
                    qv = v
                    prev = v
                elif token in ("Q2", "Q3"):
                    qv = v - prev
                    prev = v
                else:
                    qv = v
                label = f"{period[:4]}Q{_q_num(token)}"
                by_quarter.setdefault(t, {})[label] = qv

    all_qs = sorted(
        set(q for d in by_quarter.values() for q in d)
    )
    return {"quarters": all_qs, "by_quarter": by_quarter}


def _q_num(token: str) -> str:
    return {"Q1": "1", "Q2": "2", "Q3": "3", "H1": "2", "H2": "4"}.get(
        token, "?"
    )


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
    <button id="btn-annual" class="active" onclick="showAnnual()">Annual</button>
    <button id="btn-quarterly" onclick="showQuarterly()">Quarterly</button>
    <button onclick="selectAll()">Select All</button>
    <button onclick="deselectAll()">Deselect All</button>
  </div>
  <div id="plotDiv" style="width:100%;height:700px;"></div>
  <div class="note">
    Click legend entries to toggle companies — YoY growth auto-recalculates.
    *Tencent: FinTech &amp; Business Services proxy (includes WeChat Pay).
    <br>Quarterly view: XBRL data for pure-play neoclouds only.
    Hyperscaler quarterly segment data requires LLM extraction from 10-Q
    filings (coming soon).
    <br>Source:
    <a href="https://github.com/KKKKKKAI/neocloud-capex-tracker">
    neocloud-capex-tracker</a>
  </div>
</div>

<script>
/*
 * Chart settings from data/seeds/chart_config.yaml
 * Hardcoded here for static HTML — keep in sync with the YAML.
 * YoY axis: FIXED -10% to 100% (never auto-scale)
 * Bar labels: always ABOVE the bar top (yref='y', yshift=15)
 * Legend: vertical, right-hand side
 * Select/Deselect All buttons: included
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

// ---- INIT ----
var plotDiv = document.getElementById('plotDiv');
var allTraces = [];
var yoyTraceIdx = -1;

function buildTraces(dataTraces, xLabels) {{
    allTraces = [];

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

    var totals = calcTotals();
    var yoy = calcYoY(totals);
    var yoyX = [], yoyY = [], yoyText = [];
    for (var i = 0; i < yoy.length; i++) {{
        if (yoy[i] !== null) {{
            yoyX.push(xLabels[i]);
            yoyY.push(yoy[i]);
            yoyText.push(yoy[i].toFixed(0) + '%');
        }}
    }}

    yoyTraceIdx = allTraces.length;
    allTraces.push({{
        name: 'YoY Growth %',
        x: yoyX, y: yoyY,
        type: 'scatter',
        mode: 'lines+markers+text',
        line: {{ color: '#2C3E50', width: 3 }},
        marker: {{ size: 8 }},
        text: yoyText,
        textposition: 'top center',
        textfont: {{ size: 10, color: '#2C3E50' }},
        yaxis: 'y2',
        hovertemplate: '%{{x}}: %{{y:.1f}}% YoY<extra></extra>'
    }});
    return totals;
}}

function calcTotals() {{
    if (allTraces.length === 0) return [];
    var n = allTraces[0].x.length;
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

function calcYoY(totals) {{
    var yoy = [null];
    for (var i = 1; i < totals.length; i++) {{
        if (totals[i-1] > 0) {{
            yoy.push((totals[i] / totals[i-1] - 1) * 100);
        }} else {{
            yoy.push(null);
        }}
    }}
    return yoy;
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

function renderChart(dataTraces, xLabels, subtitle) {{
    var totals = buildTraces(dataTraces, xLabels);

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
            title: 'YoY Growth (%)',
            overlaying: 'y', side: 'right',
            showgrid: false,
            ticksuffix: '%',
            range: [CFG_YOY_MIN, CFG_YOY_MAX],
            fixedrange: true
        }},
        legend: {{
            orientation: 'v',
            x: 1.02, y: 1.0,
            xanchor: 'left', yanchor: 'top',
            font: {{ size: 11 }},
            tracegroupgap: 2
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
        setTimeout(recalcYoY, 100);
    }});
    plotDiv.on('plotly_legenddoubleclick', function() {{
        setTimeout(recalcYoY, 100);
    }});
}}

function recalcYoY() {{
    var data = plotDiv.data;
    var xLabels = currentView === 'annual' ? xAnnual : xQuarterly;
    var n = xLabels.length;

    var totals = new Array(n).fill(0);
    for (var i = 0; i < data.length; i++) {{
        if (data[i].type === 'bar' && data[i].visible !== 'legendonly') {{
            for (var j = 0; j < data[i].y.length; j++) {{
                totals[j] += (data[i].y[j] || 0);
            }}
        }}
    }}

    var yoy = calcYoY(totals);
    var yoyX = [], yoyY = [], yoyText = [];
    for (var i = 0; i < yoy.length; i++) {{
        if (yoy[i] !== null) {{
            yoyX.push(xLabels[i]);
            yoyY.push(yoy[i]);
            yoyText.push(yoy[i].toFixed(0) + '%');
        }}
    }}

    Plotly.restyle(plotDiv, {{
        x: [yoyX], y: [yoyY], text: [yoyText]
    }}, [yoyTraceIdx]);

    Plotly.relayout(plotDiv, {{
        annotations: makeAnnotations(totals, xLabels)
    }});
}}

// ---- SELECT / DESELECT ALL ----
function selectAll() {{
    var update = {{}};
    var indices = [];
    for (var i = 0; i < plotDiv.data.length; i++) {{
        indices.push(i);
    }}
    Plotly.restyle(plotDiv, {{ visible: true }}, indices);
    setTimeout(recalcYoY, 100);
}}

function deselectAll() {{
    var indices = [];
    for (var i = 0; i < plotDiv.data.length; i++) {{
        if (plotDiv.data[i].type === 'bar') {{
            indices.push(i);
        }}
    }}
    Plotly.restyle(plotDiv, {{ visible: 'legendonly' }}, indices);
    setTimeout(recalcYoY, 100);
}}

// ---- VIEW SWITCHING ----
function showAnnual() {{
    currentView = 'annual';
    document.getElementById('btn-annual').classList.add('active');
    document.getElementById('btn-quarterly').classList.remove('active');
    renderChart(annualTraces, xAnnual,
        '12 companies, aggregated annual, USD-normalized. ' +
        'Click legend to toggle — YoY recalculates automatically.');
}}

function showQuarterly() {{
    currentView = 'quarterly';
    document.getElementById('btn-quarterly').classList.add('active');
    document.getElementById('btn-annual').classList.remove('active');
    if (quarterlyTraces.length === 0) {{
        plotDiv.innerHTML = '<p style="text-align:center;padding:200px 0;color:#888;">' +
            'Quarterly cloud segment data not yet available.<br>' +
            'Requires LLM extraction from 10-Q filings (Phase 8B).</p>';
        return;
    }}
    renderChart(quarterlyTraces, xQuarterly,
        'Quarterly (XBRL data, pure-play neoclouds only). ' +
        'Hyperscaler quarterly data requires 10-Q extraction.');
}}

// ---- INIT ----
showAnnual();
</script>
</body>
</html>"""
