"""Entry point for the PS5 Command Hub.

Binds the hub to this PC's LAN address so the console can reach it, with the
werkzeug server in threaded mode: a package transfer holds its worker thread for the
whole install, so a single-threaded server would freeze the UI mid-download.

Usage::

    python run.py                 # bind per configs.json
    python run.py --port 9000     # override the port for this run
    python run.py --host 127.0.0.1
    python run.py --mock          # force etahen.mode=mock for this run
    python run.py --no-browser    # do not print the QR-ish banner

Nothing here contacts the internet.
"""

from __future__ import annotations

import argparse
import sys

from server import config, create_app


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PS5 Command Hub")
    parser.add_argument("--host", default=None, help="Override the bind address for this run")
    parser.add_argument("--port", type=int, default=None, help="Override the port for this run")
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Force the built-in mock etaHEN daemons for this run (no console needed)",
    )
    parser.add_argument("--debug", action="store_true", help="Enable Flask debug reloader")
    return parser.parse_args(argv)


def banner(host: str, port: int) -> str:
    mode = str(config.get("etahen.mode", "mock"))
    console = config.get("etahen.console_ip", "")
    lines = [
        "",
        "  PS5 COMMAND HUB  v" + str(config.get("version", "1.0.0")),
        "  " + "-" * 52,
        f"  Open on the console:  http://{host}:{port}",
        f"  etaHEN mode:          {mode}" + (f"  (console {console})" if mode == "live" else "  (no console needed)"),
        f"  Packages:             {', '.join(str(p) for p in config.pkg_directories()) or '(none found - check library.pkg_directories)'}",
        f"  Subnet allowlist:     {', '.join(config.get('server.subnet_allowlist', []) or []) or '(none)'}",
        "  " + "-" * 52,
        "  Ctrl+C to stop.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    config.load()
    if args.mock:
        config.save({"etahen": {"mode": "mock"}})

    # Resolve the binding *before* building the app. The URL the console is told to
    # fetch packages from comes from config.base_url(), so an unrecorded --port
    # override would send the console to the wrong port and every install would 404.
    host = args.host or config.host_ip()
    port = int(args.port or config.get("server.port", 8080) or 8080)
    config.set_runtime_binding(host=host, port=port)

    app = create_app()
    print(banner(config.host_ip(), port))

    try:
        # threaded=True is load-bearing: see the module docstring.
        app.run(host=host, port=port, threaded=True, debug=args.debug, use_reloader=args.debug)
    except OSError as exc:
        print(f"\n[run] Could not bind {host}:{port} -- {exc}")
        print("[run] Another process may already be using the port, or the address is not on this machine.")
        print("[run] Fix server.host_ip / server.port in configs.json, or pass --host/--port.")
        return 1
    except KeyboardInterrupt:
        print("\n[run] Stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
