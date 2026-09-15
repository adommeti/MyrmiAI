"""Render a ``DigestRun`` to Markdown (archive) and HTML (email).

Two renderers, one ordering rule: within a category the reader gets the
synthesis first (throughline, themes, standouts), then the full item list by
descending signal. The point of the digest is that you can stop reading at any
level and still have got the important part.
"""

from __future__ import annotations

from datetime import timedelta

from jinja2 import Environment

from .models import CategoryBrief, ContentItem, DigestRun, Source

_env = Environment(autoescape=True, trim_blocks=True, lstrip_blocks=True)

SIGNAL_LABEL = {5: "essential", 4: "worth it", 3: "solid", 2: "light", 1: "skip"}


def _fmt_duration(seconds: int | None) -> str:
    if not seconds:
        return ""
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h{minutes % 60:02d}m"


def _week_label(run: DigestRun) -> str:
    last_day = run.period_end - timedelta(days=1)
    if run.period_start.month == last_day.month:
        return f"{run.period_start:%b %-d}-{last_day:%-d, %Y}"
    return f"{run.period_start:%b %-d} - {last_day:%b %-d, %Y}"


# --- Markdown -------------------------------------------------------------


def render_markdown(
    run: DigestRun,
    items: dict[str, ContentItem],
    sources: dict[str, Source],
) -> str:
    out: list[str] = []
    out.append(f"# Weekly digest - {_week_label(run)}")
    out.append("")
    counts = ", ".join(f"{b.category} ({b.item_count})" for b in run.briefs)
    out.append(
        f"*{run.total_items} items across {len(run.briefs)} categories. "
        f"Generated {run.generated_at:%Y-%m-%d %H:%M} UTC.*"
    )
    out.append("")
    if counts:
        out.append(f"**In this issue:** {counts}")
        out.append("")

    if run.stats.get("degraded_count"):
        out.append(
            f"> {run.stats['degraded_count']} item(s) had no transcript or article "
            "text and were summarised from titles and descriptions only. "
            "These are marked *metadata only*."
        )
        out.append("")

    for brief in run.briefs:
        out.extend(_markdown_category(brief, items, sources))

    if not run.briefs:
        out.append("Nothing new this week.")
        out.append("")

    out.append("---")
    out.append("")
    out.append(
        "<sub>Add links for next week's digest to `inbox/links.md`. "
        "Adjust categories in `config/taxonomy.yaml`.</sub>"
    )
    return "\n".join(out) + "\n"


def _markdown_category(
    brief: CategoryBrief,
    items: dict[str, ContentItem],
    sources: dict[str, Source],
) -> list[str]:
    out = [f"## {brief.category}", ""]
    if brief.throughline:
        out += [brief.throughline, ""]

    if brief.themes:
        out.append("**Themes**")
        out += [f"- {theme}" for theme in brief.themes]
        out.append("")

    if brief.standouts:
        out.append("**Start here**")
        for standout in brief.standouts:
            reason = f" - {standout.reason}" if standout.reason else ""
            out.append(
                f"- [{standout.title}]({standout.url}) "
                f"*({standout.source_name})*{reason}"
            )
        out.append("")

    skippable = set(brief.skippable)
    detailed = [s for s in brief.summaries if s.item_id not in skippable]
    if detailed:
        out.append("<details><summary>All items</summary>")
        out.append("")
        for summary in detailed:
            item = items.get(summary.item_id)
            if item is None:
                continue
            source = sources.get(item.source_id)
            meta = [source.name if source else item.author]
            if item.duration_seconds:
                meta.append(_fmt_duration(item.duration_seconds))
            meta.append(f"signal {summary.signal}/5")
            if summary.degraded:
                meta.append("metadata only")

            out.append(f"### [{summary.headline}]({item.url})")
            out.append(f"*{' · '.join(meta)}*")
            out.append("")
            out += [f"- {bullet}" for bullet in summary.bullets]
            if summary.why_it_matters:
                out += ["", f"**Why it matters:** {summary.why_it_matters}"]
            out.append("")
        out.append("</details>")
        out.append("")

    if brief.skippable:
        titles = []
        for item_id in brief.skippable:
            item = items.get(item_id)
            if item:
                titles.append(f"[{item.title}]({item.url})")
        if titles:
            out.append(f"**Skipped ({len(titles)}):** " + " · ".join(titles))
            out.append("")

    return out


# --- HTML -----------------------------------------------------------------

HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Weekly digest - {{ week }}</title>
</head>
<body style="margin:0;padding:0;background:#f4f4f2;">
<div style="max-width:680px;margin:0 auto;padding:32px 20px;
            font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
            color:#1b1b1a;line-height:1.55;">

  <h1 style="margin:0 0 4px;font-size:24px;letter-spacing:-0.02em;">Weekly digest</h1>
  <p style="margin:0 0 24px;color:#6b6b68;font-size:14px;">
    {{ week }} &middot; {{ total }} items &middot; {{ categories|length }} categories
  </p>

  {% if degraded %}
  <p style="margin:0 0 24px;padding:10px 14px;background:#fdf6e3;border-left:3px solid #d9b45b;
            font-size:13px;color:#6b5d33;">
    {{ degraded }} item(s) had no transcript available and were summarised from
    descriptions only.
  </p>
  {% endif %}

  {% for brief in categories %}
  <section style="margin:0 0 40px;">
    <h2 style="margin:0 0 10px;font-size:13px;text-transform:uppercase;letter-spacing:0.08em;
               color:#8a5a2b;border-bottom:1px solid #e2e2de;padding-bottom:6px;">
      {{ brief.category }}
      <span style="float:right;color:#a3a39e;font-weight:400;">{{ brief.item_count }}</span>
    </h2>

    {% if brief.throughline %}
    <p style="margin:0 0 16px;font-size:15px;">{{ brief.throughline }}</p>
    {% endif %}

    {% if brief.themes %}
    <ul style="margin:0 0 18px;padding-left:18px;font-size:14px;color:#3d3d3a;">
      {% for theme in brief.themes %}<li style="margin-bottom:5px;">{{ theme }}</li>{% endfor %}
    </ul>
    {% endif %}

    {% if brief.standouts %}
    <p style="margin:0 0 8px;font-size:12px;text-transform:uppercase;letter-spacing:0.06em;
              color:#8a8a85;">Start here</p>
    {% for s in brief.standouts %}
    <div style="margin:0 0 12px;padding-left:12px;border-left:2px solid #d8d8d3;">
      <a href="{{ s.url }}" style="color:#1b1b1a;font-weight:600;font-size:15px;
         text-decoration:none;">{{ s.title }}</a>
      <div style="font-size:13px;color:#6b6b68;margin-top:2px;">
        {{ s.source_name }}{% if s.reason %} &mdash; {{ s.reason }}{% endif %}
      </div>
    </div>
    {% endfor %}
    {% endif %}

    {% for entry in brief.details %}
    <div style="margin:18px 0 0;padding-top:14px;border-top:1px solid #ececE8;">
      <a href="{{ entry.url }}" style="color:#1b1b1a;font-weight:600;font-size:15px;
         text-decoration:none;">{{ entry.headline }}</a>
      <div style="font-size:12px;color:#8a8a85;margin:3px 0 8px;">{{ entry.meta }}</div>
      <ul style="margin:0;padding-left:18px;font-size:14px;color:#3d3d3a;">
        {% for bullet in entry.bullets %}<li style="margin-bottom:4px;">{{ bullet }}</li>{% endfor %}
      </ul>
      {% if entry.why %}
      <p style="margin:8px 0 0;font-size:13px;color:#5c5c58;">
        <strong style="color:#3d3d3a;">Why it matters:</strong> {{ entry.why }}
      </p>
      {% endif %}
    </div>
    {% endfor %}

    {% if brief.skipped %}
    <p style="margin:16px 0 0;font-size:12px;color:#a3a39e;">
      Skipped: {{ brief.skipped|join(' &middot; ')|safe }}
    </p>
    {% endif %}
  </section>
  {% endfor %}

  {% if not categories %}
  <p style="font-size:15px;color:#6b6b68;">Nothing new this week.</p>
  {% endif %}

  <p style="margin:32px 0 0;padding-top:16px;border-top:1px solid #e2e2de;
            font-size:12px;color:#a3a39e;">
    Add links for next week to <code>inbox/links.md</code>.
  </p>
</div>
</body>
</html>
"""


def render_html(
    run: DigestRun,
    items: dict[str, ContentItem],
    sources: dict[str, Source],
) -> str:
    categories = []
    for brief in run.briefs:
        skippable = set(brief.skippable)
        details = []
        for summary in brief.summaries:
            if summary.item_id in skippable:
                continue
            item = items.get(summary.item_id)
            if item is None:
                continue
            source = sources.get(item.source_id)
            meta = [source.name if source else item.author]
            if item.duration_seconds:
                meta.append(_fmt_duration(item.duration_seconds))
            meta.append(f"signal {summary.signal}/5 ({SIGNAL_LABEL[summary.signal]})")
            if summary.degraded:
                meta.append("metadata only")
            details.append(
                {
                    "headline": summary.headline,
                    "url": item.url,
                    "meta": " · ".join(meta),
                    "bullets": summary.bullets,
                    "why": summary.why_it_matters,
                }
            )
        skipped = [
            items[item_id].title for item_id in brief.skippable if item_id in items
        ]
        categories.append(
            {
                "category": brief.category,
                "item_count": brief.item_count,
                "throughline": brief.throughline,
                "themes": brief.themes,
                "standouts": brief.standouts,
                "details": details,
                "skipped": skipped,
            }
        )

    return _env.from_string(HTML_TEMPLATE).render(
        week=_week_label(run),
        total=run.total_items,
        categories=categories,
        degraded=run.stats.get("degraded_count", 0),
    )
