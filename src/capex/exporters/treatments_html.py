"""Self-contained HTML viewer for per-company special treatments.

Renders every human-authored rule for every tracked company on a
single page — coverage.yaml structured rules + human_notes.yaml PEL
notes — so a reviewer can audit the full rule book in one place.

Data comes from `capex.audit.treatments_query.query_treatments`; the
page reuses the chart/calendar nav bar via `_build_nav_html`. A small
inline JS block provides client-side search + ticker filter so no
server is required.
"""
from __future__ import annotations

import html
from datetime import datetime, timezone
from pathlib import Path

from ..audit.treatments_query import (
    CompanyTreatmentView,
    DatasetRule,
    HumanNoteView,
    query_treatments,
)
from .interactive_chart import COLORS, _build_nav_html

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCS_DIR = REPO_ROOT / "docs"

STATE_COLORS = {
    "active":     ("#2F855A", "#D1FAE5"),   # green
    "superseded": ("#B7791F", "#FEF3C7"),   # yellow
    "revoked":    ("#6B7280", "#E5E7EB"),   # grey
}

METRIC_DISPLAY = {
    "revenue": "Revenue",
    "capital_expenditures": "CapEx",
    "operating_cash_flow": "Operating Cash Flow",
    "depreciation_amortization": "D&A",
    "property_plant_equipment_net": "PP&E",
    "cloud_segment_revenue": "Cloud / DC Revenue",
}

FYE_MONTH_NAMES = {
    1: "January", 2: "February", 3: "March", 4: "April",
    5: "May", 6: "June", 7: "July", 8: "August",
    9: "September", 10: "October", 11: "November", 12: "December",
}


def generate_treatments_html(
    output: str | Path | None = None,
    db_path: str | Path | None = None,
) -> Path:
    """Write the treatments audit page to `output` and return the path."""
    output = Path(output or DOCS_DIR / "treatments.html")
    output.parent.mkdir(parents=True, exist_ok=True)
    views = query_treatments(db_path=db_path)
    html_str = _build_html(views)
    output.write_text(html_str, encoding="utf-8")
    return output


# ---- Rendering helpers -------------------------------------------

def _ticker_chip(ticker: str) -> str:
    colour = COLORS.get(ticker, "#888")
    return (
        f'<span class="chip" style="background:{colour};">'
        f"{html.escape(ticker)}</span>"
    )


def _state_badge(state: str) -> str:
    fg, bg = STATE_COLORS.get(state, ("#444", "#eee"))
    return (
        f'<span class="badge" style="color:{fg};background:{bg};">'
        f"{html.escape(state)}</span>"
    )


def _fmt_metric(mk: str) -> str:
    return html.escape(METRIC_DISPLAY.get(mk, mk))


def _render_company_notes(notes: str) -> str:
    """Render the free-form company_notes prose as a bullet-per-paragraph list."""
    if not notes.strip():
        return '<p class="muted">— no company-level notes —</p>'
    # Split on blank lines; each paragraph → one bullet.
    paragraphs = [p.strip() for p in notes.split("\n\n") if p.strip()]
    items = "".join(
        f"<li>{html.escape(p).replace(chr(10), '<br>')}</li>"
        for p in paragraphs
    )
    return f'<ul class="notes-list">{items}</ul>'


def _render_dataset_rule(r: DatasetRule) -> str:
    metric_label = ", ".join(_fmt_metric(m) for m in r.metric_keys) or r.dataset
    if r.excluded:
        reason = html.escape(r.exclusion_reason or "no reason given")
        return (
            f'<div class="rule excluded">'
            f'<div class="rule-hdr"><strong>{metric_label}</strong>'
            f' <span class="badge" style="color:#991B1B;background:#FEE2E2">'
            f'excluded</span></div>'
            f'<p class="muted">{reason}</p></div>'
        )
    rows = [
        ("Treatment", html.escape(r.treatment)),
    ]
    if r.segment_names:
        rows.append((
            "Segment names",
            ", ".join(f'&quot;{html.escape(s)}&quot;' for s in r.segment_names),
        ))
    if r.segment_start:
        rows.append(("Segment start", html.escape(r.segment_start)))
    if r.extraction_method:
        rows.append(("Extraction", html.escape(r.extraction_method)))
    if r.adjustment:
        adj = r.adjustment
        if adj.get("method"):
            rows.append(("Adjustment method", html.escape(str(adj["method"]))))
        if adj.get("formula"):
            rows.append(("Formula", html.escape(str(adj["formula"]))))
        if adj.get("rationale"):
            rows.append(("Rationale",
                         html.escape(str(adj["rationale"])).replace("\n", "<br>")))
        if adj.get("caveats"):
            caveats = "".join(
                f"<li>{html.escape(str(c))}</li>" for c in adj["caveats"]
            )
            rows.append(("Caveats", f"<ul>{caveats}</ul>"))
    if r.notes:
        rows.append((
            "Notes",
            html.escape(r.notes).replace("\n", "<br>"),
        ))
    body = "".join(
        f'<dt>{k}</dt><dd>{v}</dd>' for k, v in rows
    )
    return (
        f'<div class="rule">'
        f'<div class="rule-hdr"><strong>{metric_label}</strong></div>'
        f'<dl class="rule-body">{body}</dl>'
        f'</div>'
    )


def _render_human_note(n: HumanNoteView) -> str:
    scope = n.scope or {}
    scope_parts = []
    if scope.get("ticker"):
        scope_parts.append(scope["ticker"])
    if scope.get("metric_keys"):
        scope_parts.append("/".join(scope["metric_keys"]))
    if scope.get("period_range"):
        scope_parts.append(scope["period_range"])
    scope_line = html.escape(" · ".join(scope_parts) or "all")
    kw_html = ""
    if n.keywords_to_match:
        kws = ", ".join(
            f'&quot;{html.escape(k)}&quot;' for k in n.keywords_to_match
        )
        kw_html = f'<dt>Keywords</dt><dd>{kws}</dd>'
    cautions_html = ""
    if n.cautions:
        items = "".join(f"<li>{html.escape(c)}</li>" for c in n.cautions)
        cautions_html = f'<dt>Cautions</dt><dd><ul>{items}</ul></dd>'
    cells_html = ""
    if n.source_cell_keys:
        sample = ", ".join(
            html.escape(c) for c in n.source_cell_keys[:3]
        )
        extra = (
            f" (+{len(n.source_cell_keys) - 3} more)"
            if len(n.source_cell_keys) > 3 else ""
        )
        cells_html = f'<dt>Linked cells</dt><dd>{sample}{extra}</dd>'
    reviewer_html = ""
    if n.reviewer_input:
        reviewer_html = (
            '<details class="verbatim"><summary>reviewer input</summary>'
            f'<blockquote>{html.escape(n.reviewer_input)}</blockquote>'
            '</details>'
        )
    rationale_html = ""
    if n.rationale:
        rationale_html = (
            f'<dt>Rationale</dt>'
            f'<dd>{html.escape(n.rationale).replace(chr(10), "<br>")}</dd>'
        )
    return (
        f'<div class="hnote">'
        f'<div class="rule-hdr">'
        f'<strong>{html.escape(n.id)}</strong>'
        f' {_state_badge(n.state)}'
        f' <span class="muted">scope: {scope_line}</span>'
        f'</div>'
        f'<p class="guidance">{html.escape(n.guidance)}</p>'
        f'<dl class="rule-body">'
        f'{kw_html}{cautions_html}'
        f'<dt>Added</dt><dd>{html.escape(n.added_at or "—")}'
        f' <span class="muted">(audit: '
        f'{html.escape(n.source_audit_run_id or "—")})</span></dd>'
        f'{cells_html}{rationale_html}'
        f'</dl>'
        f'{reviewer_html}'
        f'</div>'
    )


def _render_company_card(v: CompanyTreatmentView) -> str:
    fye = FYE_MONTH_NAMES.get(v.fiscal_year_end_month, str(v.fiscal_year_end_month))
    header = (
        f'<header class="card-hdr">'
        f'{_ticker_chip(v.ticker)}'
        f'<h2>{html.escape(v.full_name)}</h2>'
        f'<span class="muted">FYE: {fye}</span>'
        f'</header>'
        f'<p class="meta">'
        f'Category: <code>{html.escape(v.category)}</code>'
        f' · Currency: {html.escape(v.reporting_currency)}'
        f' · Coverage from {html.escape(v.coverage_start or "—")}'
        f' · Approach: <code>{html.escape(v.extraction_approach or "—")}</code>'
        f'</p>'
    )
    # Quarterly convention summary
    qconv = v.quarterly_convention or {}
    qc_html = ""
    if qconv.get("default"):
        qc_html = (
            f'<p class="meta">Quarterly convention: '
            f'<code>{html.escape(str(qconv["default"]))}</code></p>'
        )
    notes_html = _render_company_notes(v.company_notes)
    rules_html = (
        "".join(_render_dataset_rule(r) for r in v.dataset_rules)
        or '<p class="muted">— no dataset rules configured —</p>'
    )
    if v.human_notes:
        hn_html = "".join(_render_human_note(n) for n in v.human_notes)
    else:
        hn_html = '<p class="muted">— no human notes yet —</p>'
    filter_meta = " ".join(
        [v.ticker, v.full_name, v.category, v.reporting_currency] +
        [r.dataset for r in v.dataset_rules] +
        [mk for r in v.dataset_rules for mk in r.metric_keys]
    )
    metrics = sorted({
        mk for r in v.dataset_rules for mk in r.metric_keys
    })
    return (
        f'<article class="company-card" '
        f'data-ticker="{html.escape(v.ticker)}" '
        f'data-metrics="{html.escape(",".join(metrics))}" '
        f'data-filter="{html.escape(filter_meta.lower())}">'
        f'{header}'
        f'{qc_html}'
        f'<section><h3>Company notes</h3>{notes_html}</section>'
        f'<section><h3>Dataset rules</h3>{rules_html}</section>'
        f'<section><h3>Human notes '
        f'<span class="muted">({len(v.human_notes)})</span></h3>{hn_html}</section>'
        f'</article>'
    )


def _build_html(views: list[CompanyTreatmentView]) -> str:
    nav_html = _build_nav_html("treatments")
    total_rules = sum(len(v.dataset_rules) for v in views)
    total_human = sum(len(v.human_notes) for v in views)
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Ticker dropdown
    ticker_options = "".join(
        f'<option value="{html.escape(v.ticker)}">{html.escape(v.ticker)}</option>'
        for v in views
    )
    # Metric dropdown from union of metrics present
    metrics_present = sorted({
        mk for v in views for r in v.dataset_rules for mk in r.metric_keys
    })
    metric_options = "".join(
        f'<option value="{html.escape(mk)}">{_fmt_metric(mk)}</option>'
        for mk in metrics_present
    )

    cards = "\n".join(_render_company_card(v) for v in views)

    return _HTML_TEMPLATE.format(
        nav_html=nav_html,
        total_companies=len(views),
        total_rules=total_rules,
        total_human=total_human,
        generated=html.escape(generated),
        ticker_options=ticker_options,
        metric_options=metric_options,
        cards=cards,
    )


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Special treatments — AI Infrastructure Ecosystem</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         margin: 0; padding: 20px; background: #fafafa; color: #222; }}
  .container {{ max-width: 1100px; margin: 0 auto; }}
  h1 {{ text-align: center; margin: 0 0 6px; font-size: 22px; }}
  .subtitle {{ text-align: center; color: #666; margin: 0 0 20px; font-size: 13px; }}
  .nav {{ text-align: center; margin: 0 auto 18px; max-width: 1400px; }}
  .nav-pill {{ display: inline-block; padding: 6px 14px; margin: 0 4px;
    border: 1px solid #888; border-radius: 18px; color: #555; font-size: 13px;
    text-decoration: none; background: #fff; }}
  .nav-pill:hover {{ border-color: #2E75B6; color: #2E75B6; }}
  .nav-pill.active {{ background: #2E75B6; color: #fff; border-color: #2E75B6;
    font-weight: bold; }}
  .filter-bar {{ display: flex; gap: 10px; align-items: center;
    background: #fff; border: 1px solid #d1d9e0; border-radius: 8px;
    padding: 10px 14px; margin-bottom: 18px; flex-wrap: wrap; }}
  .filter-bar input, .filter-bar select {{
    padding: 6px 10px; border: 1px solid #d1d9e0; border-radius: 6px;
    font-size: 13px; }}
  .filter-bar input {{ flex: 1; min-width: 180px; }}
  .filter-bar .stats {{ color: #666; font-size: 12px; margin-left: auto; }}
  .company-card {{ background: #fff; border: 1px solid #e5e7eb;
    border-radius: 10px; padding: 18px 22px; margin-bottom: 18px;
    box-shadow: 0 1px 2px rgba(0,0,0,0.04); }}
  .card-hdr {{ display: flex; align-items: center; gap: 10px;
    margin-bottom: 6px; }}
  .card-hdr h2 {{ font-size: 16px; margin: 0; flex: 1; }}
  .chip {{ display: inline-block; min-width: 48px; padding: 2px 8px;
    border-radius: 10px; color: #fff; font-size: 11px; font-weight: bold;
    text-align: center; }}
  .meta {{ color: #555; font-size: 12px; margin: 0 0 10px; }}
  code {{ background: #eee; padding: 1px 5px; border-radius: 3px;
    font-size: 12px; }}
  .muted {{ color: #888; font-style: italic; }}
  h3 {{ font-size: 13px; text-transform: uppercase; letter-spacing: 0.06em;
    color: #666; border-bottom: 1px solid #eee; padding-bottom: 4px;
    margin: 16px 0 8px; }}
  .notes-list {{ margin: 0; padding-left: 20px; }}
  .notes-list li {{ margin-bottom: 8px; font-size: 13px; line-height: 1.5; }}
  .rule, .hnote {{ background: #fafafa; border: 1px solid #e5e7eb;
    border-radius: 6px; padding: 10px 14px; margin: 6px 0; }}
  .rule.excluded {{ background: #FEF2F2; border-color: #FEE2E2; }}
  .rule-hdr {{ display: flex; align-items: center; gap: 8px;
    margin-bottom: 6px; font-size: 13px; }}
  .rule-body {{ margin: 0; font-size: 12px; display: grid;
    grid-template-columns: 150px 1fr; gap: 4px 10px; }}
  .rule-body dt {{ color: #666; font-weight: 600; }}
  .rule-body dd {{ margin: 0; }}
  .rule-body ul {{ margin: 0; padding-left: 18px; }}
  .hnote .guidance {{ font-size: 13px; margin: 6px 0 8px; line-height: 1.5; }}
  .badge {{ padding: 2px 8px; border-radius: 10px; font-size: 10px;
    font-weight: 600; text-transform: lowercase; }}
  .verbatim {{ margin-top: 6px; font-size: 12px; }}
  .verbatim summary {{ cursor: pointer; color: #2E75B6; }}
  .verbatim blockquote {{ margin: 6px 0 0; padding: 8px 12px;
    background: #f3f4f6; border-left: 3px solid #2E75B6; font-style: italic;
    color: #333; font-size: 12px; white-space: pre-wrap; }}
  .sync-caption {{ text-align: center; color: #aaa; font-size: 11px;
    margin-top: 30px; }}
</style>
</head>
<body>
{nav_html}
<div class="container">
  <h1>Special treatments — per-company audit view</h1>
  <p class="subtitle">Every human-authored rule governing extraction for
    the 13 tracked companies</p>

  <div class="filter-bar">
    <input id="search" type="text" placeholder="Search ticker, name, metric, segment…">
    <select id="ticker-filter">
      <option value="">All tickers</option>
      {ticker_options}
    </select>
    <select id="metric-filter">
      <option value="">All metrics</option>
      {metric_options}
    </select>
    <span class="stats">{total_companies} companies · {total_rules} rules ·
      {total_human} human notes</span>
  </div>

  {cards}

  <p class="sync-caption">Generated {generated} from
    <code>coverage.yaml</code> + <code>human_notes.yaml</code> +
    <code>audit_review_feedback</code> table.</p>
</div>

<script>
(function() {{
  const search = document.getElementById('search');
  const tickerFilter = document.getElementById('ticker-filter');
  const metricFilter = document.getElementById('metric-filter');
  const cards = document.querySelectorAll('article.company-card');

  function applyFilters() {{
    const q = search.value.trim().toLowerCase();
    const t = tickerFilter.value;
    const m = metricFilter.value;
    cards.forEach(card => {{
      const haystack = card.getAttribute('data-filter') || '';
      const cardTicker = card.getAttribute('data-ticker') || '';
      const cardMetrics = (card.getAttribute('data-metrics') || '').split(',');
      let show = true;
      if (q && !haystack.includes(q)) show = false;
      if (t && cardTicker !== t) show = false;
      if (m && !cardMetrics.includes(m)) show = false;
      card.style.display = show ? '' : 'none';
    }});
  }}

  search.addEventListener('input', applyFilters);
  tickerFilter.addEventListener('change', applyFilters);
  metricFilter.addEventListener('change', applyFilters);
}})();
</script>
</body>
</html>
"""
