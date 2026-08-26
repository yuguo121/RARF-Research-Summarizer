from __future__ import annotations

import socket
import sys
from pathlib import Path

from rarf_summarizer.app import serve


def _free_port(preferred: int) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if "--help" in argv or "-h" in argv:
        print("Usage: rarf-desk [--port N] [--no-browser] [--root PATH]")
        return 0
    port = 8765
    open_browser = True
    project_root: Path | None = None
    args: list[str] = []
    it = iter(argv)
    for arg in it:
        if arg == "--no-browser":
            open_browser = False
        elif arg == "--port":
            port = int(next(it))
        elif arg == "--root":
            project_root = Path(next(it)).expanduser().resolve()
        else:
            args.append(arg)
    if args:
        port = int(args[0])
    if project_root is None:
        # When launched from an installed console script, cwd may not be the project.
        candidate = Path.cwd()
        project_root = candidate if (candidate / "config" / "settings.yaml").is_file() else None
    port = _free_port(port)
    try:
        serve(port=port, open_browser=open_browser, project_root=project_root)
    except OSError as exc:
        print(f"could not start RARF desk on port {port}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
