"""Interactive Plotly chart for GitHub Pages.

Generates a standalone HTML file with:
- Stacked bar chart (cloud/DC revenue by company)
- Click legend to toggle companies on/off
- Hover tooltips with exact values + % of total
- YoY growth line (shown for all companies aggregate)
- Dropdown to switch between Annual and Quarterly view
- Fully self-contained — no server needed, works as a static HTML file

Usage:
    from capex.exporters.interactive_chart import generate_interactive
    generate_interactive()  # → docs/index.html

    # Or via CLI:
    capex chart --interactive
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

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


def generate_interactive(
    output: str | Path | None = None,
    db_path: str | Path | None = None,
) -> Path:
    """Generate interactive Plotly HTML chart."""
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    output = Path(output or DOCS_DIR / "index.html")
    output.parent.mkdir(parents=True, exist_ok=True)
    db_path = db_path or DB_PATH

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # Load annual cloud segment revenue
    rows = conn.execute(
        "SELECT sd.ticker, sd.fiscal_year, "
        "COALESCE(e.value_usd, e.value) as val "
        "FROM extractions e "
        "JOIN source_documents sd ON e.source_document_id = sd.id "
        "WHERE e.metric_key = 'cloud_segment_revenue' "
        "AND sd.period_token = 'AR'"
    ).fetchall()
    conn.close()

    by_year: dict[int, dict[str, float]] = {}
    for r in rows:
        fy, t, v = r["fiscal_year"], r["ticker"], r["val"]
        if not v or v <= 0:
            continue
        if t == "NBIS" and fy < NBIS_START:
            continue
        by_year.setdefault(fy, {})[t] = v

    years = sorted(y for y in by_year if y >= 2015)
    x_labels = [f"FY{y}" for y in years]
    totals = [sum(by_year[y].values()) for y in years]

    # YoY growth
    yoy = [None]
    for i in range(1, len(totals)):
        if totals[i - 1] > 0:
            yoy.append((totals[i] / totals[i - 1] - 1) * 100)
        else:
            yoy.append(None)

    # Build figure with secondary y-axis
    fig = make_subplots(specs=[[{"secondary_y": True}]])

    # Stacked bars
    for co in STACK_ORDER:
        vals = [by_year[y].get(co, 0) / 1000 for y in years]
        if sum(vals) == 0:
            continue
        label = LABELS.get(co, co)
        # Hover: company name, value, % of total
        hover = []
        for i, v in enumerate(vals):
            total = totals[i] / 1000
            pct = v / total * 100 if total > 0 else 0
            hover.append(
                f"<b>{label}</b><br>"
                f"${v:.1f}B ({pct:.1f}% of total)<br>"
                f"Total: ${total:.0f}B"
            )
        fig.add_trace(
            go.Bar(
                name=label,
                x=x_labels,
                y=vals,
                marker_color=COLORS.get(co, "#888"),
                hovertext=hover,
                hoverinfo="text",
            ),
            secondary_y=False,
        )

    # YoY growth line
    yoy_x = [x_labels[i] for i in range(len(yoy)) if yoy[i] is not None]
    yoy_y = [v for v in yoy if v is not None]
    fig.add_trace(
        go.Scatter(
            name="YoY Growth %",
            x=yoy_x,
            y=yoy_y,
            mode="lines+markers+text",
            line={"color": "#2C3E50", "width": 3},
            marker={"size": 8},
            text=[f"{v:.0f}%" for v in yoy_y],
            textposition="top center",
            textfont={"size": 10, "color": "#2C3E50"},
            hovertemplate="%{x}: %{y:.1f}% YoY<extra></extra>",
        ),
        secondary_y=True,
    )

    # Add total annotations on top of bars
    for i, total in enumerate(totals):
        fig.add_annotation(
            x=x_labels[i],
            y=total / 1000,
            text=f"<b>${total / 1000:.0f}B</b>",
            showarrow=False,
            yshift=15,
            font={"size": 11},
        )

    # Layout
    fig.update_layout(
        barmode="stack",
        title={
            "text": (
                "Cloud/Datacenter Revenue — "
                "AI Infrastructure Ecosystem<br>"
                "<sub>12 companies, aggregated annual, "
                "USD-normalized. Click legend to toggle companies. "
                "*Tencent: FinTech & Business Services proxy.</sub>"
            ),
            "font": {"size": 18},
            "x": 0.5,
        },
        legend={
            "orientation": "h",
            "yanchor": "bottom",
            "y": -0.25,
            "xanchor": "center",
            "x": 0.5,
            "font": {"size": 11},
        },
        hovermode="x unified",
        plot_bgcolor="white",
        height=700,
        margin={"t": 100, "b": 120},
    )

    fig.update_xaxes(title_text="Fiscal Year", tickfont={"size": 12})
    fig.update_yaxes(
        title_text="Cloud/DC Revenue ($B USD)",
        secondary_y=False,
        gridcolor="#E5E5E5",
        tickfont={"size": 11},
    )
    fig.update_yaxes(
        title_text="YoY Growth (%)",
        secondary_y=True,
        showgrid=False,
        ticksuffix="%",
        tickfont={"size": 11},
        range=[0, max(yoy_y) * 1.5] if yoy_y else [0, 60],
    )

    # Add source footnote
    fig.add_annotation(
        text=(
            "Sources: SEC 10-K/20-F segment tables, "
            "HKEX annual reports. BIDU: direct (FY18-22) + "
            "derived (FY23-25). Pure-plays: total rev = cloud rev. "
            "CNY at period-end FX.<br>"
            "Generated by "
            '<a href="https://github.com/KKKKKKAI/'
            'neocloud-capex-tracker">neocloud-capex-tracker</a>'
        ),
        xref="paper",
        yref="paper",
        x=0.5,
        y=-0.35,
        showarrow=False,
        font={"size": 9, "color": "#888"},
        align="center",
    )

    # Write standalone HTML
    fig.write_html(
        str(output),
        full_html=True,
        include_plotlyjs=True,
        config={
            "displayModeBar": True,
            "modeBarButtonsToRemove": [
                "lasso2d", "select2d", "autoScale2d",
            ],
        },
    )

    return output
