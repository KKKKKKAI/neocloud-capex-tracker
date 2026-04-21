"""Chart generation from the extractions DB.

IMPORTANT — YoY GROWTH RECALCULATION:
    YoY growth rates are ALWAYS recomputed from the raw DB data at
    chart generation time. They are never cached or stored. This is
    O(1) per data point and ensures the growth line is always correct
    when the underlying revenue series changes.

    DO NOT filter out YoY values based on magnitude. If the growth
    rate is 50%+ because new companies entered the dataset, that's
    a VALID data point that should be shown on the chart. The footnote
    explains when companies enter.

Usage:
    from capex.exporters.charts import (
        generate_cloud_revenue_chart,
        generate_metric_chart,
        generate_all_metric_charts,
    )
    generate_cloud_revenue_chart()         # cloud_revenue_annual.png
    generate_metric_chart("revenue")       # revenue_annual.png
    generate_all_metric_charts()           # all 4 PNGs
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
DB_PATH = REPO_ROOT / "data" / "db" / "capex.db"
CHARTS_DIR = REPO_ROOT / "charts"

# Company display config
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
LABELS = {"0700": "Tencent*"}  # override display names
NBIS_START = 2024  # filter pre-restructuring

# Per-metric config for the static PNGs — mirrors the interactive
# chart's METRIC_CONFIGS so the dashboard thumbnails match the
# interactive pages. Exclude sets too: if a metric has no coverage
# for a ticker, keep it out of the stacked bar entirely.
METRIC_PNG_CONFIGS: dict[str, dict[str, Any]] = {
    "cloud_segment_revenue": {
        "output_name": "cloud_revenue_annual.png",
        "title": "Cloud / Datacenter Revenue — AI Infrastructure Ecosystem",
        "yaxis": "Cloud/DC Revenue ($B USD)",
        "footnote": (
            "Sources: SEC 10-K/20-F segment tables, HKEX AR "
            "(pdfplumber). BIDU: direct (FY18-22) + derived (FY23-25).\n"
            "*Tencent: FinTech & Business Services proxy. "
            "BABA: Cloud Computing/Intelligence. CNY at period-end FX."
        ),
        "exclude_tickers": {"META"},
    },
    "revenue": {
        "output_name": "revenue_annual.png",
        "title": "Total Revenue — AI Infrastructure Ecosystem",
        "yaxis": "Total Revenue ($B USD)",
        "footnote": (
            "Sources: SEC 10-K/20-F income statements, HKEX AR. "
            "All values USD-normalized at period-end FX."
        ),
        "exclude_tickers": set(),
    },
    "capital_expenditures": {
        "output_name": "capex_annual.png",
        "title": "Capital Expenditures — AI Infrastructure Ecosystem",
        "yaxis": "CapEx ($B USD)",
        "footnote": (
            "Sources: SEC 10-K/20-F cash-flow statements, HKEX AR. "
            "Tencent excluded — only fragmentary HKEX annual coverage."
        ),
        "exclude_tickers": {"0700"},
    },
    "operating_cash_flow": {
        "output_name": "operating_cash_flow_annual.png",
        "title": "Operating Cash Flow — AI Infrastructure Ecosystem",
        "yaxis": "Operating Cash Flow ($B USD)",
        "footnote": (
            "Sources: SEC 10-K/20-F cash-flow statements, HKEX AR. "
            "Tencent excluded — only fragmentary HKEX annual coverage."
        ),
        "exclude_tickers": {"0700"},
    },
}


def generate_metric_chart(
    metric_key: str,
    output: str | Path | None = None,
    db_path: str | Path | None = None,
) -> Path:
    """Generate an annual stacked-bar + YoY PNG for the given metric.

    YoY growth is ALWAYS recalculated from the DB. No filtering, no
    caching. If you add or modify data, just call this function again.
    """
    cfg = METRIC_PNG_CONFIGS.get(metric_key)
    if cfg is None:
        raise ValueError(f"unknown metric_key: {metric_key!r}")

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.ticker as mticker
    import numpy as np

    output = Path(output or CHARTS_DIR / cfg["output_name"])
    output.parent.mkdir(parents=True, exist_ok=True)
    db_path = db_path or DB_PATH
    exclude = cfg["exclude_tickers"]

    # --- STEP 1: Load data from DB ---
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    # ORDER BY filing_date ASC — later rows overwrite the dict so
    # restated values (from a newer 10-K) win over original as-reported.
    # But 0/NULL restated rows (usually LLM mis-reads of an empty cell)
    # are demoted so the authentic original keeps its slot.
    rows = conn.execute(
        "SELECT sd.ticker, sd.fiscal_year, "
        "COALESCE(e.value_usd, e.value) as val "
        "FROM extractions e "
        "JOIN source_documents sd ON e.source_document_id = sd.id "
        "WHERE e.metric_key = ? "
        "AND sd.period_token = 'AR' "
        "AND e.period_type = 'FY' "
        "ORDER BY "
        "  CASE WHEN COALESCE(e.value_usd, e.value) IS NULL "
        "         OR COALESCE(e.value_usd, e.value) = 0 THEN 1 ELSE 0 END ASC, "
        "  sd.filing_date ASC, e.extracted_at ASC",
        (metric_key,),
    ).fetchall()
    conn.close()

    by_year: dict[int, dict[str, float]] = {}
    for r in rows:
        fy, t, v = r["fiscal_year"], r["ticker"], r["val"]
        if t in exclude:
            continue
        if not v or v <= 0:
            continue
        if t == "NBIS" and fy < NBIS_START:
            continue
        by_year.setdefault(fy, {})[t] = v

    years = sorted(y for y in by_year if y >= 2015)
    if not years:
        raise RuntimeError(
            f"no data found for {metric_key!r} — DB may be empty"
        )
    totals = [sum(by_year[y].values()) for y in years]

    # --- STEP 2: Compute YoY growth — ALWAYS from scratch ---
    yoy: list[float | None] = [None]
    for i in range(1, len(totals)):
        if totals[i - 1] > 0:
            yoy.append((totals[i] / totals[i - 1] - 1) * 100)
        else:
            yoy.append(None)

    # --- STEP 3: Plot ---
    fig, ax1 = plt.subplots(figsize=(18, 9))
    ax2 = ax1.twinx()
    x = np.arange(len(years))
    bottom = np.zeros(len(years))

    n_companies = 0
    for co in STACK_ORDER:
        vals = np.array(
            [by_year[y].get(co, 0) / 1000 for y in years]
        )
        if vals.sum() > 0:
            n_companies += 1
            label = LABELS.get(co, co)
            ax1.bar(
                x, vals, 0.7, bottom=bottom, label=label,
                color=COLORS.get(co, "#888"),
                edgecolor="white", linewidth=0.5,
            )
            bottom += vals

    # Total labels
    for i, t in enumerate(totals):
        ax1.text(
            i, t / 1000 + 5, f"${t / 1000:.0f}B",
            ha="center", va="bottom", fontsize=9, fontweight="bold",
        )

    # YoY line — show ALL valid points, no magnitude filter
    yoy_x = [i for i, v in enumerate(yoy) if v is not None]
    yoy_y = [yoy[i] for i in yoy_x]
    if yoy_x:
        ax2.plot(
            yoy_x, yoy_y, "o-", lw=2.5, ms=7,
            color="#2C3E50", label="YoY Growth %", zorder=10,
        )
        for xi, yi in zip(yoy_x, yoy_y, strict=True):
            ax2.annotate(
                f"{yi:.0f}%", (xi, yi),
                textcoords="offset points", xytext=(0, 10),
                ha="center", fontsize=8, fontweight="bold",
                color="#2C3E50",
            )

    # Formatting
    ax1.set_xlabel("Fiscal Year", fontsize=13, fontweight="bold")
    ax1.set_ylabel(cfg["yaxis"], fontsize=14, fontweight="bold")
    ax2.set_ylabel(
        "YoY Growth (%)", fontsize=13, fontweight="bold",
        color="#2C3E50",
    )
    ax1.set_xticks(x)
    ax1.set_xticklabels(
        [f"FY{y}" for y in years], rotation=45, ha="right", fontsize=10
    )
    ax1.grid(axis="y", alpha=0.3)
    ax1.set_axisbelow(True)
    h1, l1 = ax1.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax1.legend(
        h1 + h2, l1 + l2, loc="upper left",
        fontsize=9, ncol=3, framealpha=0.95,
    )
    if yoy_y:
        ax2.set_ylim(0, max(yoy_y) * 1.5)
    ax2.yaxis.set_major_formatter(mticker.PercentFormatter())

    plt.title(
        f"{cfg['title']}\n"
        f"{n_companies} companies, aggregated annual, USD-normalized "
        f"(FY{years[0]}–FY{years[-1]})",
        fontsize=15, fontweight="bold", pad=15,
    )
    fig.text(
        0.5, -0.02, cfg["footnote"],
        ha="center", fontsize=7.5, style="italic", color="#666",
    )

    plt.tight_layout()
    plt.savefig(
        str(output), dpi=150, bbox_inches="tight",
        facecolor="white", edgecolor="none",
    )
    plt.close(fig)
    return output


def generate_cloud_revenue_chart(
    output: str | Path | None = None,
    db_path: str | Path | None = None,
) -> Path:
    """Backwards-compatible wrapper — generates the cloud/DC revenue PNG."""
    return generate_metric_chart(
        "cloud_segment_revenue", output=output, db_path=db_path,
    )


def generate_all_metric_charts(
    db_path: str | Path | None = None,
    out_dir: str | Path | None = None,
) -> list[Path]:
    """Emit a PNG for every metric in METRIC_PNG_CONFIGS."""
    out_dir = Path(out_dir or CHARTS_DIR)
    paths: list[Path] = []
    for metric_key, cfg in METRIC_PNG_CONFIGS.items():
        paths.append(generate_metric_chart(
            metric_key,
            output=out_dir / cfg["output_name"],
            db_path=db_path,
        ))
    return paths
