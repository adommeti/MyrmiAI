"""Command line entry point.

    digest sources          # refresh the subscription registry
    digest classify         # assign categories to new sources
    digest preview          # what would this week's digest cover? (no model calls)
    digest run              # the weekly job: collect, summarise, render, deliver
    digest add <url> [note] # queue a link for next week's digest
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import INBOX_DIR, load_settings, resolve_period
from .pipeline import Pipeline
from .store import Store


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _pipeline(args: argparse.Namespace) -> Pipeline:
    settings = load_settings()
    if getattr(args, "no_email", False):
        settings.email = False
    return Pipeline(
        settings,
        store=Store(),
        takeout_csv=Path(args.takeout) if getattr(args, "takeout", None) else None,
    )


def cmd_sources(args: argparse.Namespace) -> int:
    pipeline = _pipeline(args)
    sources, added = pipeline.sync_sources()
    print(f"{len(sources)} sources in the registry ({len(added)} new).")
    uncategorised = [s for s in sources if not s.category]
    if uncategorised:
        print(f"{len(uncategorised)} awaiting classification. Run: digest classify")
    return 0


def cmd_classify(args: argparse.Namespace) -> int:
    pipeline = _pipeline(args)
    sources = list(pipeline.store.load_sources().values())
    if not sources:
        print("No sources yet. Run `digest sources` first.", file=sys.stderr)
        return 1
    changed = pipeline.classify(sources, recheck=args.recheck)
    print(f"Classified {len(changed)} source(s).")

    counts: dict[str, int] = {}
    for source in sources:
        counts[source.category or "(unclassified)"] = counts.get(source.category or "(unclassified)", 0) + 1
    for category, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {count:4d}  {category}")

    suggestions = {s.suggested_category for s in sources if s.suggested_category}
    if suggestions:
        print("\nCategories the classifier wanted but could not use:")
        for suggestion in sorted(suggestions):
            print(f"  - {suggestion}")
        print("Add any that recur to config/taxonomy.yaml.")
    return 0


def cmd_preview(args: argparse.Namespace) -> int:
    """Show what the run would cover without spending a token on summaries."""
    pipeline = _pipeline(args)
    run = pipeline.run(
        days=args.days, align=not args.no_align, dry_run=True,
        include_seen=args.include_seen, skip_discovery=args.skip_discovery,
    )
    stats = run.stats
    print(f"Window: {run.period_start:%Y-%m-%d} -> {run.period_end:%Y-%m-%d}")
    print(f"Sources: {stats['sources']}   Items: {stats['items']}   "
          f"Without transcript: {stats['no_transcript']}")
    if args.json:
        print(json.dumps(stats, indent=2))
    else:
        for name, count in list(stats["by_source"].items())[:40]:
            print(f"  {count:3d}  {name}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    pipeline = _pipeline(args)
    run = pipeline.run(
        days=args.days, align=not args.no_align,
        include_seen=args.include_seen, skip_discovery=args.skip_discovery,
    )
    print(f"\nDigest for {run.slug}: {run.total_items} items, {len(run.briefs)} categories.")
    for brief in run.briefs:
        print(f"  {brief.item_count:3d}  {brief.category}")
    if run.stats.get("failed"):
        print(f"  ({run.stats['failed']} item(s) could not be summarised)")
    print(f"Written to digests/{run.slug}.md")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    inbox = INBOX_DIR / "links.md"
    inbox.parent.mkdir(parents=True, exist_ok=True)
    if not inbox.exists():
        inbox.write_text("# Saved links\n\n", encoding="utf-8")
    note = " ".join(args.note).strip()
    line = f"- {args.url}" + (f" -- {note}" if note else "")
    with inbox.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    print(f"Queued for the next digest: {args.url}")
    return 0


def cmd_window(args: argparse.Namespace) -> int:
    settings = load_settings()
    start, end = resolve_period(
        settings, now=datetime.now(timezone.utc), days=args.days, align=not args.no_align
    )
    print(f"{start.isoformat()} -> {end.isoformat()}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="digest", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_window_flags(p: argparse.ArgumentParser) -> None:
        p.add_argument("--days", type=int, help="Window length (default: config)")
        p.add_argument("--no-align", action="store_true",
                       help="End the window now instead of at the last period boundary")
        p.add_argument("--include-seen", action="store_true",
                       help="Re-include items already reported in a previous digest")
        p.add_argument("--skip-discovery", action="store_true",
                       help="Use the stored registry; do not call the subscriptions API")

    p_sources = sub.add_parser("sources", help="Refresh the source registry")
    p_sources.add_argument("--takeout", help="Path to a Google Takeout subscriptions.csv")
    p_sources.set_defaults(func=cmd_sources)

    p_classify = sub.add_parser("classify", help="Assign categories to sources")
    p_classify.add_argument("--recheck", action="store_true",
                            help="Reclassify every source, including ones already done")
    p_classify.set_defaults(func=cmd_classify)

    p_preview = sub.add_parser("preview", help="Show coverage without calling the model")
    add_window_flags(p_preview)
    p_preview.add_argument("--takeout")
    p_preview.add_argument("--json", action="store_true")
    p_preview.set_defaults(func=cmd_preview)

    p_run = sub.add_parser("run", help="Generate and deliver the digest")
    add_window_flags(p_run)
    p_run.add_argument("--takeout")
    p_run.add_argument("--no-email", action="store_true")
    p_run.set_defaults(func=cmd_run)

    p_add = sub.add_parser("add", help="Queue a link for the next digest")
    p_add.add_argument("url")
    p_add.add_argument("note", nargs="*", help="Why you saved it")
    p_add.set_defaults(func=cmd_add)

    p_window = sub.add_parser("window", help="Print the window this run would cover")
    p_window.add_argument("--days", type=int)
    p_window.add_argument("--no-align", action="store_true")
    p_window.set_defaults(func=cmd_window)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _configure_logging(args.verbose)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
