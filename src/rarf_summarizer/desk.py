from __future__ import annotations

import sys
from pathlib import Path

from rarf_summarizer.app import serve


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    port = 8765
    open_browser = True
    args: list[str] = []
    it = iter(argv)
    for arg in it:
        if arg == "--no-browser":
            open_browser = False
        elif arg == "--port":
            port = int(next(it))
        else:
            args.append(arg)
    if args:
        port = int(args[0])
    serve(port=port, open_browser=open_browser, project_root=Path.cwd())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
