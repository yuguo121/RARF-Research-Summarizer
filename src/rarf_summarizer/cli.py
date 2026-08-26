from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rarf_summarizer.pipeline import Pipeline


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rarf", description="Summarize papers into RARF dimensions.")
    sub = parser.add_subparsers(dest="command", required=True)

    extract = sub.add_parser("extract", help="Extract text from PDFs without calling the LLM")
    extract.add_argument("folder", nargs="?", default=None)
    extract.add_argument("--force", action="store_true")

    summarize = sub.add_parser("summarize", help="Run theory/method/reconcile LLM sessions")
    summarize.add_argument("folder", nargs="?", default=None)
    summarize.add_argument("--force", action="store_true")
    summarize.add_argument("--limit", type=int, default=None)
    summarize.add_argument("--name-contains", default=None)

    export = sub.add_parser("export", help="Write output/RARF_Overview.xlsx")
    export.add_argument("--workbook", default=None)

    sync = sub.add_parser("sync-back", help="Import human edits from the workbook")
    sync.add_argument("--workbook", default=None)

    run = sub.add_parser("run", help="Extract, summarize, and export")
    run.add_argument("folder", nargs="?", default=None)
    run.add_argument("--force", action="store_true")
    run.add_argument("--limit", type=int, default=None)
    run.add_argument("--name-contains", default=None)

    ui = sub.add_parser("ui", help="Open the local RARF desk in a browser")
    ui.add_argument("--port", type=int, default=8765)
    ui.add_argument("--no-browser", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "ui":
        from rarf_summarizer.app import serve

        serve(port=args.port, open_browser=not args.no_browser)
        return 0
    pipeline = Pipeline()
    if args.command == "extract":
        ids = pipeline.extract_folder(Path(args.folder) if args.folder else None, force=args.force)
        print(f"extracted {len(ids)} paper(s)")
        return 0
    if args.command == "summarize":
        ids = pipeline.summarize_folder(
            Path(args.folder) if args.folder else None,
            force=args.force,
            limit=args.limit,
            name_contains=args.name_contains,
        )
        print(f"summarized {len(ids)} paper(s)")
        return 0
    if args.command == "export":
        path = pipeline.export(Path(args.workbook) if args.workbook else None)
        print(path)
        return 0
    if args.command == "sync-back":
        updates = pipeline.sync_back(Path(args.workbook) if args.workbook else None)
        print(f"stored {updates} human override(s)")
        return 0
    if args.command == "run":
        path = pipeline.run(
            Path(args.folder) if args.folder else None,
            force=args.force,
            limit=args.limit,
            name_contains=args.name_contains,
        )
        print(path)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
