from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .core import publish, refresh
from .server import serve


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="news-feed")
    sub = p.add_subparsers(dest="command", required=True)
    update = sub.add_parser("refresh", help="fetch feeds and atomically publish JSON")
    update.add_argument("--config", type=Path, default=Path("config/feeds.json"))
    update.add_argument("--output-dir", type=Path, default=Path("data"))
    update.add_argument(
        "--publish-dir",
        action="append",
        type=Path,
        default=[],
        help="atomically publish public JSON to this directory (repeatable)",
    )
    update.add_argument("--now", help="inject ISO 8601 clock value for deterministic runs")
    update.add_argument("--timeout", type=float, default=12.0)
    update.add_argument("--retries", type=int, default=2)
    server = sub.add_parser("serve", help="serve published JSON read-only")
    server.add_argument("--output-dir", type=Path, default=Path("data"))
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=3011)
    server.add_argument("--max-age-seconds", type=int, default=5 * 3600)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "refresh":
        feeds = json.loads(args.config.read_text(encoding="utf-8"))
        if not isinstance(feeds, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in feeds.items()):
            raise ValueError("feed config must be a JSON object of source-name to URL")
        now = datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now else datetime.now(timezone.utc)
        output, status = refresh(feeds=feeds, now=now, output_dir=args.output_dir, timeout=args.timeout, retries=args.retries)
        # Source failures are represented in completed fallback output. Publish
        # that degraded state before returning the source-failure exit status.
        publish(output, status, args.publish_dir)
        failures = [name for name, state in status["sources"].items() if state["error"]]
        print(json.dumps({"generatedAt": status["generatedAt"], "sources": len(feeds), "failures": failures}))
        return 1 if failures else 0
    serve(args.output_dir, args.host, args.port, args.max_age_seconds)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"news-feed: {exc}", file=sys.stderr)
        raise SystemExit(2)
