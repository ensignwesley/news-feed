from __future__ import annotations

import json
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .core import parse_date

ROUTES = {
    "/command-news/feed.json": "feed.json",
    "/command-news/status.json": "status.json",
}


def handler_factory(output_dir: Path, max_age_seconds: int):
    class Handler(BaseHTTPRequestHandler):
        server_version = "command-news/1.0"

        def send_json(self, code: int, payload: bytes, *, head: bool = False) -> None:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            if not head:
                self.wfile.write(payload)

        def serve_route(self, *, head: bool = False) -> None:
            path = self.path.split("?", 1)[0]
            if path in ROUTES:
                try:
                    payload = (output_dir / ROUTES[path]).read_bytes()
                    json.loads(payload)
                except (FileNotFoundError, json.JSONDecodeError):
                    payload = b'{"error":"published data unavailable"}\n'
                    self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, payload, head=head)
                    return
                self.send_json(HTTPStatus.OK, payload, head=head)
                return
            if path == "/command-news/health":
                code, document = health(output_dir, max_age_seconds)
                payload = (json.dumps(document, separators=(",", ":")) + "\n").encode()
                self.send_json(code, payload, head=head)
                return
            payload = b'{"error":"not found"}\n'
            self.send_json(HTTPStatus.NOT_FOUND, payload, head=head)

        def do_GET(self) -> None:
            self.serve_route()

        def do_HEAD(self) -> None:
            self.serve_route(head=True)

        def reject_mutation(self) -> None:
            payload = b'{"error":"method not allowed"}\n'
            self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
            self.send_header("Allow", "GET, HEAD")
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_POST = reject_mutation
        do_PUT = reject_mutation
        do_PATCH = reject_mutation
        do_DELETE = reject_mutation

        def log_message(self, format: str, *args: object) -> None:
            print(f"{self.address_string()} - {format % args}")

    return Handler


def health(output_dir: Path, max_age_seconds: int) -> tuple[int, dict]:
    try:
        status = json.loads((output_dir / "status.json").read_text(encoding="utf-8"))
        json.loads((output_dir / "feed.json").read_text(encoding="utf-8"))
        generated = parse_date(status.get("generatedAt"))
        if generated is None:
            raise ValueError("missing generatedAt")
        age = max(0, int((datetime.now(timezone.utc) - generated).total_seconds()))
        failures = [name for name, state in status.get("sources", {}).items() if state.get("error")]
        healthy = age <= max_age_seconds and not failures
        return (HTTPStatus.OK if healthy else HTTPStatus.SERVICE_UNAVAILABLE), {
            "healthy": healthy,
            "generatedAt": status["generatedAt"],
            "ageSeconds": age,
            "failedSources": failures,
        }
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return HTTPStatus.SERVICE_UNAVAILABLE, {"healthy": False, "error": str(exc)}


def serve(output_dir: Path, host: str, port: int, max_age_seconds: int) -> None:
    server = ThreadingHTTPServer((host, port), handler_factory(output_dir, max_age_seconds))
    print(f"serving command-news on http://{host}:{port}")
    server.serve_forever()
