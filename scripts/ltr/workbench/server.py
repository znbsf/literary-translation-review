"""Loopback HTTP server: strict Host/Origin checks, static assets and the JSON API."""
from __future__ import annotations

import json
from http import HTTPStatus
from typing import Any, Mapping, Sequence
import argparse
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .common import WorkbenchError, STATIC_ROOT, MAX_BODY_BYTES, static_file
from .common import _contained, _LOOPBACK_HOSTS
from .config import WorkbenchConfig
from .store import WorkbenchStore


class WorkbenchHandler(BaseHTTPRequestHandler):
    """Strict loopback handler: no directory browsing, no CORS, no leaks."""

    server_version = "corpus-workbench"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # Injected by :class:`WorkbenchServer`.
    store: WorkbenchStore
    allowed_hosts: frozenset[str]
    allowed_origins: frozenset[str]

    # -- logging -------------------------------------------------------
    def log_message(self, format: str, *args: Any) -> None:
        # Never log query strings, bodies, source text or configured paths.
        return

    def log_error(self, format: str, *args: Any) -> None:
        return

    # -- helpers -------------------------------------------------------
    def _respond(
        self,
        status: int,
        body: bytes,
        content_type: str,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD" and body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):  # pragma: no cover
                return

    def _send_json(self, status: int, payload: Mapping[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=False).encode("utf-8")
        self._respond(status, body, "application/json; charset=utf-8")

    def _send_error(self, status: int, code: str) -> None:
        self._send_json(status, {"error": code, "status": int(status)})

    def _host_ok(self) -> bool:
        header = self.headers.get("Host")
        if header is None:
            return False
        host = header.strip().lower()
        return host in self.allowed_hosts

    def _origin_ok(self) -> bool:
        header = self.headers.get("Origin")
        if header is None:
            return True
        value = header.strip().lower()
        if value == "null":
            return False
        return value in self.allowed_origins and value == "http://" + self.headers.get("Host", "").strip().lower()

    def _request_acceptable(self) -> bool:
        if not self._host_ok():
            self._send_error(HTTPStatus.FORBIDDEN, "host_not_allowed")
            return False
        if not self._origin_ok():
            self._send_error(HTTPStatus.FORBIDDEN, "origin_not_allowed")
            return False
        # Requests explicitly flagged as cross-site by a modern browser.
        if (self.headers.get("Sec-Fetch-Site") or "").strip().lower() == "cross-site":
            self._send_error(HTTPStatus.FORBIDDEN, "cross_site_blocked")
            return False
        return True

    def _same_origin(self) -> bool:
        """Strict check used by the mutating review endpoint."""
        origin = self.headers.get("Origin")
        if origin is None:
            return False
        if not self._origin_ok():
            return False
        referer = self.headers.get("Referer")
        if referer is not None:
            try:
                parts = urlsplit(referer.strip())
            except ValueError:
                return False
            if (parts.hostname or "").lower() not in _LOOPBACK_HOSTS:
                return False
            try:
                port = parts.port
            except ValueError:
                return False
            if port != int(self.server.server_address[1]):
                return False
        return True

    @staticmethod
    def _json_content_type(value: str | None) -> bool:
        if not value:
            return False
        mime = value.split(";", 1)[0].strip().lower()
        return mime == "application/json"

    def _read_body(self) -> Mapping[str, Any]:
        length_header = self.headers.get("Content-Length")
        if length_header is None:
            raise WorkbenchError(HTTPStatus.LENGTH_REQUIRED, "length_required")
        try:
            length = int(length_header.strip())
        except (TypeError, ValueError):
            raise WorkbenchError(HTTPStatus.BAD_REQUEST, "invalid_length") from None
        if length < 0:
            raise WorkbenchError(HTTPStatus.BAD_REQUEST, "invalid_length")
        if length > MAX_BODY_BYTES:
            raise WorkbenchError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "body_too_large")
        if length == 0:
            raise WorkbenchError(HTTPStatus.BAD_REQUEST, "body_required")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise WorkbenchError(HTTPStatus.BAD_REQUEST, "invalid_json") from None
        if not isinstance(payload, Mapping):
            raise WorkbenchError(HTTPStatus.BAD_REQUEST, "invalid_json")
        return payload

    # -- verbs ---------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - http.server API
        if not self._request_acceptable():
            return
        try:
            self._handle_get()
        except WorkbenchError as exc:
            self._send_error(exc.status, exc.code)
        except Exception:
            self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error")

    def do_HEAD(self) -> None:  # noqa: N802 - http.server API
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        if not self._request_acceptable():
            return
        try:
            self._handle_post()
        except WorkbenchError as exc:
            self._send_error(exc.status, exc.code)
        except Exception:
            self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error")

    def do_PUT(self) -> None:  # noqa: N802 - http.server API
        if not self._request_acceptable():
            return
        self._send_error(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed")

    do_PATCH = do_PUT
    do_DELETE = do_PUT

    def do_OPTIONS(self) -> None:  # noqa: N802 - http.server API
        # No CORS preflight support: this server is same-origin only.
        if not self._host_ok():
            self._send_error(HTTPStatus.FORBIDDEN, "host_not_allowed")
            return
        self._send_error(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed")

    # -- routing -------------------------------------------------------
    def _handle_get(self) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path
        asset = static_file(path)
        if asset is not None:
            self._serve_static(asset)
            return
        if path.startswith("/api/"):
            pairs = parse_qs(parsed.query, keep_blank_values=True)
            params = _first_values(pairs)
            self._route_api_get(path, params)
            return
        self._send_error(HTTPStatus.NOT_FOUND, "not_found")

    def _serve_static(self, filename: str) -> None:
        target = (STATIC_ROOT / filename).resolve(strict=False)
        if not _contained(target, STATIC_ROOT.resolve(strict=False)):
            self._send_error(HTTPStatus.NOT_FOUND, "not_found")
            return
        if not target.is_file():
            self._send_error(HTTPStatus.NOT_FOUND, "static_missing")
            return
        content_type = {
            ".html": "text/html; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
        }.get(target.suffix, "application/octet-stream")
        try:
            data = target.read_bytes()
        except OSError:
            self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "static_unreadable")
            return
        self._respond(
            HTTPStatus.OK,
            data,
            content_type,
            {
                "Content-Security-Policy": (
                    "default-src 'self'; img-src 'self' data:; "
                    "script-src 'self'; style-src 'self'; connect-src 'self'; "
                    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
                    "form-action 'none'"
                )
            },
        )

    def _route_api_get(self, path: str, params: Mapping[str, Any]) -> None:
        store = self.store
        if path == "/api/overview":
            self._send_json(HTTPStatus.OK, store.overview())
        elif path == "/api/editions":
            self._send_json(HTTPStatus.OK, store.editions(params.get("corpus")))
        elif path == "/api/dialogues":
            self._send_json(HTTPStatus.OK, store.dialogues(params))
        elif path == "/api/evidence":
            self._send_json(HTTPStatus.OK, store.evidence(params))
        elif path == "/api/glossary":
            self._send_json(HTTPStatus.OK, store.glossary(params))
        elif path == "/api/dialogue":
            self._send_json(HTTPStatus.OK, store.dialogue(params))
        elif path == "/api/ocr-reading-order-candidates":
            self._send_json(HTTPStatus.OK, store.ocr_reading_order_candidates(params))
        elif path == "/api/ocr-reading-order-candidate":
            self._send_json(HTTPStatus.OK, store.ocr_reading_order_candidate(params))
        elif path == "/api/ocr-reading-order-image":
            data, content_type = store.ocr_reading_order_image(params)
            self._respond(HTTPStatus.OK, data, content_type)
        elif path == "/api/ocr-reading-units":
            self._send_json(HTTPStatus.OK, store.ocr_reading_units(params))
        elif path == "/api/ocr-regions":
            self._send_json(HTTPStatus.OK, store.ocr_regions(params))
        elif path == "/api/ocr-region":
            self._send_json(HTTPStatus.OK, store.ocr_region(params))
        elif path == "/api/ocr-vision-candidates":
            self._send_json(HTTPStatus.OK, store.ocr_vision_candidates(params))
        elif path == "/api/ocr-vision-candidate":
            self._send_json(HTTPStatus.OK, store.ocr_vision_candidate(params))
        elif path == "/api/ocr-vision-baseline":
            self._send_json(HTTPStatus.OK, store.ocr_vision_baseline(params))
        elif path == "/api/ocr-vision-image":
            data, content_type = store.ocr_vision_image(params)
            self._respond(HTTPStatus.OK, data, content_type)
        elif path == "/api/scan":
            data, content_type = store.scan_image(params)
            self._respond(HTTPStatus.OK, data, content_type)
        elif path == "/api/reviews":
            self._send_json(HTTPStatus.OK, store.review_export(params))
        else:
            self._send_error(HTTPStatus.NOT_FOUND, "not_found")

    def _handle_post(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path != "/api/reviews":
            self._send_error(HTTPStatus.NOT_FOUND, "not_found")
            return
        if not self._same_origin():
            self._send_error(HTTPStatus.FORBIDDEN, "origin_not_allowed")
            return
        if not self._json_content_type(self.headers.get("Content-Type")):
            self._send_error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "json_required")
            return
        body = self._read_body()
        result = self.store.save_review(body)
        self._send_json(HTTPStatus.OK, result)



def _first_values(pairs: Mapping[str, list[str]]) -> dict[str, str]:
    """Collapse repeated parameters to a single value, dropping repeats."""
    return {key: values[0] for key, values in pairs.items() if values}


class WorkbenchServer(ThreadingHTTPServer):
    """Threaded loopback-only server bound to ``127.0.0.1``."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        port: int,
        store: WorkbenchStore,
        host: str = "127.0.0.1",
    ) -> None:
        if host not in {"127.0.0.1", "localhost"}:
            raise WorkbenchError(HTTPStatus.BAD_REQUEST, "invalid_host")
        self.store = store
        handler = type("BoundWorkbenchHandler", (WorkbenchHandler,), {"store": store})
        super().__init__((host, port), handler)
        bound_port = int(self.server_address[1])
        handler.allowed_hosts = frozenset(
            {
                f"127.0.0.1:{bound_port}",
                f"localhost:{bound_port}",
                f"[::1]:{bound_port}",
            }
        )
        handler.allowed_origins = frozenset(
            {
                f"http://127.0.0.1:{bound_port}",
                f"http://localhost:{bound_port}",
                f"http://[::1]:{bound_port}",
            }
        )

    def server_close(self) -> None:
        try:
            self.store.close()
        finally:
            super().server_close()


def build_server(config: WorkbenchConfig, port: int = 0) -> WorkbenchServer:
    """Create (but do not start) a loopback server for ``config``."""
    if not isinstance(port, int) or isinstance(port, bool):
        raise WorkbenchError(HTTPStatus.BAD_REQUEST, "invalid_port")
    if port < 0 or port > 65535:
        raise WorkbenchError(HTTPStatus.BAD_REQUEST, "invalid_port")
    store = WorkbenchStore(config)
    return WorkbenchServer(port, store)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ltr.workbench",
        description="Local read-only corpus workbench (loopback only).",
    )
    parser.add_argument("--config", required=True, help="external JSON config file")
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="loopback port (0 selects a free port)",
    )
    args = parser.parse_args(argv)

    try:
        config = WorkbenchConfig.load(args.config)
        server = build_server(config, args.port)
    except WorkbenchError as exc:
        print(f"corpus-workbench: {exc.code}", file=sys.stderr)
        return 2
    except OSError:
        print("corpus-workbench: bind_failed", file=sys.stderr)
        return 2

    host, port = server.server_address[0], server.server_address[1]
    print(f"corpus-workbench: listening on http://127.0.0.1:{port}/", flush=True)
    del host
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


