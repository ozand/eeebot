from __future__ import annotations

import argparse
from wsgiref.simple_server import make_server

from .app import create_app
from .collector import collect_once, run_poll_loop
from .config import load_config
from .storage import init_db


def main() -> None:
    parser = argparse.ArgumentParser(prog="nanobot-ops-dashboard")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db")
    sub.add_parser("collect-once")

    poll = sub.add_parser("poll")
    poll.add_argument("--iterations", type=int, default=None)

    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8787)

    args = parser.parse_args()
    cfg = load_config()

    if args.command == "init-db":
        init_db(cfg.db_path)
        print(cfg.db_path)
        return

    if args.command == "collect-once":
        init_db(cfg.db_path)
        result = collect_once(cfg)
        print(result)
        return

    if args.command == "poll":
        init_db(cfg.db_path)
        run_poll_loop(cfg, iterations=args.iterations)
        return

    if args.command == "serve":
        init_db(cfg.db_path)
        app = create_app(cfg)
        with make_server(args.host, args.port, app) as httpd:
            print(f"Serving on http://{args.host}:{args.port}")
            httpd.serve_forever()
