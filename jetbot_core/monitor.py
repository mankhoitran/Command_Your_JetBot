"""Pipeline monitor HTTP server. Separate port, same process as the console."""

from __future__ import print_function

import logging
import mimetypes
import os
import threading

try:
    from urllib.parse import urlparse, parse_qs
except ImportError:
    from urlparse import urlparse, parse_qs

try:
    from http.server import BaseHTTPRequestHandler, HTTPServer
except ImportError:
    from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer

try:
    from socketserver import ThreadingMixIn
except ImportError:
    from SocketServer import ThreadingMixIn

from .llm import SYSTEM_PROMPT, ALLOWED_TOOLS
from .live import DEFAULT_TOOL_DOCS, default_intent_tools, default_keyword_map
from .web import ThreadedHTTPServer, _json_bytes, _parse_body, ensure_self_signed_cert

log = logging.getLogger("jetbot.monitor")

MONITOR_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "monitor"
)


class MonitorHandler(BaseHTTPRequestHandler):
    runtime = None

    def log_message(self, fmt, *args):
        log.debug("%s " + fmt, self.address_string(), *args)

    def _send(self, code, body, content_type="application/json; charset=utf-8"):
        if not isinstance(body, bytes):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        rt = self.runtime
        if path in ("/", "/index.html"):
            return self._static("index.html")
        if path == "/api/pipeline":
            return self._send(200, _json_bytes(rt.pipeline_snapshot()))
        if path == "/api/trace":
            qs = parse_qs(parsed.query or "")
            limit = int((qs.get("limit") or [40])[0])
            after = int((qs.get("after") or [0])[0])
            return self._send(200, _json_bytes(rt.tracer.snapshot(limit=limit, after_seq=after)))
        if path == "/api/events":
            qs = parse_qs(parsed.query or "")
            limit = int((qs.get("limit") or [80])[0])
            after = float((qs.get("after") or [0])[0])
            return self._send(200, _json_bytes({"events": rt.bus.recent(limit=limit, after_ts=after)}))
        if path == "/api/settings":
            return self._send(200, _json_bytes(self._settings_payload()))
        if path == "/api/status":
            return self._send(200, _json_bytes(rt.status_payload()))
        if path.startswith("/"):
            candidate = os.path.join(MONITOR_DIR, path.lstrip("/"))
            if os.path.isfile(candidate):
                return self._file(candidate)
        self._send(404, _json_bytes({"error": "not found", "path": path}))

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        data = _parse_body(raw, ctype)
        rt = self.runtime
        if path == "/api/settings":
            try:
                overlay = rt.live.patch(data)
                rt.live.apply(rt)
                rt.live.save()
            except Exception as exc:
                log.exception("live settings patch failed")
                return self._send(400, _json_bytes({"ok": False, "error": str(exc)}))
            return self._send(200, _json_bytes({
                "ok": True,
                "updated_ts": rt.live.updated_ts,
                "settings": self._settings_payload(),
            }))
        if path == "/api/settings/reset":
            rt.live.data = {
                "system_prompt": None,
                "allowed_tools": None,
                "tool_docs": {},
                "tool_enabled": {},
                "intent_tools": None,
                "keyword_map": None,
                "llm": {},
            }
            rt.live.apply(rt)
            rt.live.save()
            return self._send(200, _json_bytes({"ok": True, "settings": self._settings_payload()}))
        if path == "/api/command":
            text = data.get("text") or data.get("command") or ""
            rt.agent.submit(text, source="monitor")
            return self._send(200, _json_bytes({"ok": True, "accepted": text[:200]}))
        self._send(404, _json_bytes({"error": "not found"}))

    def _settings_payload(self):
        rt = self.runtime
        overlay = rt.live.snapshot()
        prompt = overlay.get("system_prompt") or SYSTEM_PROMPT
        allowed = overlay.get("allowed_tools") or list(ALLOWED_TOOLS)
        docs = dict(DEFAULT_TOOL_DOCS)
        docs.update(overlay.get("tool_docs") or {})
        enabled = overlay.get("tool_enabled") or {}
        tools = []
        for name in allowed:
            tools.append({
                "name": name,
                "enabled": True if name not in enabled else bool(enabled[name]),
                "doc": docs.get(name, ""),
            })
        # Include disabled-but-known tools so the editor can re-enable them.
        for name, on in enabled.items():
            if name not in allowed:
                tools.append({"name": name, "enabled": bool(on), "doc": docs.get(name, "")})
        llm = rt.llm
        return {
            "system_prompt": prompt,
            "system_prompt_overridden": bool(overlay.get("system_prompt")),
            "allowed_tools": allowed,
            "tools": tools,
            "intent_tools": overlay.get("intent_tools") or default_intent_tools(),
            "keyword_map": overlay.get("keyword_map") or default_keyword_map(),
            "llm": {
                "model": llm.model,
                "base_url": llm.base_url,
                "temperature": llm.temperature,
                "max_tokens": llm.max_tokens,
                "timeout_s": llm.timeout,
                "enable_thinking": llm.enable_thinking,
            },
            "updated_ts": rt.live.updated_ts,
        }

    def _static(self, rel):
        path = os.path.normpath(os.path.join(MONITOR_DIR, rel.lstrip("/")))
        if not path.startswith(os.path.abspath(MONITOR_DIR)):
            return self._send(403, b"forbidden", "text/plain")
        if not os.path.isfile(path):
            return self._send(404, b"missing", "text/plain")
        return self._file(path)

    def _file(self, path):
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        with open(path, "rb") as handle:
            body = handle.read()
        self._send(200, body, ctype)


def serve_monitor(runtime, host="0.0.0.0", port=8090, ssl_enabled=False,
                  ssl_cert=None, ssl_key=None):
    MonitorHandler.runtime = runtime
    server = ThreadedHTTPServer((host, int(port)), MonitorHandler)
    if ssl_enabled:
        import ssl
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cert = ssl_cert or "data/ssl/cert.pem"
        key = ssl_key or "data/ssl/key.pem"
        if not os.path.isabs(cert):
            cert = os.path.join(root, cert)
        if not os.path.isabs(key):
            key = os.path.join(root, key)
        ensure_self_signed_cert(cert, key)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        log.info("Pipeline monitor on https://%s:%s", host, port)
    else:
        log.info("Pipeline monitor on http://%s:%s", host, port)
    return server


def start_monitor_thread(server):
    thread = threading.Thread(target=server.serve_forever, name="monitor-http")
    thread.daemon = True
    thread.start()
    return thread
