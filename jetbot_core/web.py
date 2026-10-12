"""Stdlib HTTP console: JSON APIs + MJPEG. Does not bypass safety."""

from __future__ import print_function

import cgi
import json
import logging
import mimetypes
import os
import threading
import time

try:
    from urllib.parse import urlparse, parse_qs
except ImportError:
    from urlparse import urlparse, parse_qs

from io import BytesIO

try:
    from http.server import BaseHTTPRequestHandler, HTTPServer
except ImportError:
    from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer

try:
    from socketserver import ThreadingMixIn
except ImportError:
    from SocketServer import ThreadingMixIn

import cv2
import numpy as np

from .events import EventType

log = logging.getLogger("jetbot.web")

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "static")


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _json_bytes(payload):
    return json.dumps(payload, default=_json_default).encode("utf-8")


def _json_default(obj):
    if isinstance(obj, bytes):
        return obj.decode("utf-8", "replace")
    return str(obj)


class JetBotHandler(BaseHTTPRequestHandler):
    runtime = None
    jpeg_quality = 60
    mjpeg_fps = 12

    def log_message(self, fmt, *args):
        log.debug("%s " + fmt, self.address_string(), *args)

    def _send(self, code, body, content_type="application/json; charset=utf-8", extra=None):
        if not isinstance(body, bytes):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
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
        if path == "/" or path == "/index.html":
            return self._static("/index.html")
        if path.startswith("/static/"):
            return self._static(path[len("/static"):] if path.startswith("/static") else path)
        if path in ("/api/status", "/api/state"):
            return self._send(200, _json_bytes(self.runtime.status_payload()))
        if path == "/api/events":
            qs = parse_qs(parsed.query or "")
            limit = int((qs.get("limit") or [80])[0])
            after = float((qs.get("after") or [0])[0])
            return self._send(200, _json_bytes({"events": self.runtime.bus.recent(limit=limit, after_ts=after)}))
        if path == "/api/world":
            return self._send(200, _json_bytes(self.runtime.world.snapshot()))
        if path == "/api/memory":
            return self._send(200, _json_bytes(self.runtime.memory.snapshot()))
        if path == "/api/health":
            return self._send(200, _json_bytes(self.runtime.health.snapshot()))
        if path == "/api/task":
            return self._send(200, _json_bytes(self.runtime.tasks.snapshot()))
        if path in ("/video", "/mjpeg", "/camera.mjpg"):
            return self._mjpeg()
        if path.startswith("/"):
            # try static file by basename
            candidate = os.path.join(STATIC_DIR, path.lstrip("/"))
            if os.path.isfile(candidate):
                return self._file(candidate)
        self._send(404, _json_bytes({"error": "not found", "path": path}))

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if path == "/api/estop":
            self.runtime.safety.emergency_stop(source="http")
            return self._send(200, _json_bytes({"ok": True, "estop": True}))
        if path == "/api/estop/clear":
            self.runtime.safety.clear_estop(source="http")
            return self._send(200, _json_bytes({"ok": True, "estop": False}))
        if path == "/api/command":
            from .telemetry import bind_corr_id, clear_corr_id, new_corr_id
            data = _parse_body(raw, ctype)
            text = data.get("text") or data.get("command") or ""
            corr_id = new_corr_id()
            bind_corr_id(corr_id)
            try:
                self.runtime.agent.submit(text, source="web")
            finally:
                clear_corr_id()
            return self._send(200, _json_bytes({
                "ok": True, "accepted": text[:200], "corr_id": corr_id,
            }))
        if path == "/api/manual":
            data = _parse_body(raw, ctype)
            result = self.runtime.handle_manual(data.get("action"), data)
            return self._send(200 if result.get("ok") else 400, _json_bytes(result))
        if path == "/api/motion":
            data = _parse_body(raw, ctype)
            allowed = data.get("allow")
            if allowed is None:
                allowed = data.get("allow_motion")
            if allowed is None:
                action = str(data.get("action") or "").lower()
                if action in ("unlock", "on", "true", "1"):
                    allowed = True
                elif action in ("lock", "off", "false", "0"):
                    allowed = False
            if allowed is None:
                return self._send(400, _json_bytes({"ok": False, "error": "allow required"}))
            result = self.runtime.set_allow_motion(bool(allowed), source="http")
            return self._send(200, _json_bytes(result))
        if path == "/api/whisper":
            return self._whisper(raw, ctype)
        if path == "/api/scan":
            result = self.runtime.tools.dispatch("camera.scan_environment", {})
            return self._send(200, _json_bytes(result))
        self._send(404, _json_bytes({"error": "not found"}))

    def _whisper(self, raw, ctype):
        if ctype in ("application/json", "application/x-www-form-urlencoded", "text/plain"):
            return self._send(400, _json_bytes({"ok": False, "error": "no audio"}))
        audio = None
        filename = "speech.wav"
        if ctype.startswith("multipart/"):
            environ = {
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": self.headers.get("Content-Type"),
                "CONTENT_LENGTH": str(len(raw)),
            }
            fs = cgi.FieldStorage(fp=BytesIO(raw), headers=self.headers, environ=environ)
            field = fs["file"] if "file" in fs else None
            if field is None:
                for key in fs.keys():
                    field = fs[key]
                    break
            if field is not None and getattr(field, "file", None) is not None:
                audio = field.file.read()
                filename = getattr(field, "filename", None) or filename
        else:
            audio = raw
        if not audio:
            return self._send(400, _json_bytes({"ok": False, "error": "no audio"}))
        from .telemetry import bind_corr_id, clear_corr_id, new_corr_id
        corr_id = new_corr_id()
        bind_corr_id(corr_id)
        try:
            text, err = self.runtime.whisper.transcribe_bytes(audio, filename=filename)
            if err:
                self.runtime.bus.emit(EventType.WHISPER_UNAVAILABLE, "whisper", {"error": err},
                                      corr_id=corr_id)
                return self._send(502, _json_bytes({"ok": False, "error": err, "corr_id": corr_id}))
            if text:
                self.runtime.agent.submit(text, source="whisper")
            return self._send(200, _json_bytes({"ok": True, "text": text, "corr_id": corr_id}))
        finally:
            clear_corr_id()

    def _mjpeg(self):
        self.send_response(200)
        self.send_header("Age", "0")
        self.send_header("Cache-Control", "no-cache, private")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.end_headers()
        period = 1.0 / max(1, int(self.mjpeg_fps))
        last_seq = 0
        quality = int(self.jpeg_quality)
        while True:
            t0 = time.time()
            frame, ts, seq = self.runtime.overlay_buffer.get(copy=True, min_seq=last_seq, wait_s=0.3)
            if frame is None:
                frame, ts, seq = self.runtime.frame_buffer.get(copy=True, wait_s=0.05)
            if frame is None:
                frame = np.zeros((240, 320, 3), dtype=np.uint8)
            last_seq = seq
            ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
            if not ok:
                time.sleep(period)
                continue
            payload = encoded.tobytes()
            try:
                self.wfile.write(b"--frame\r\n")
                self.wfile.write(b"Content-Type: image/jpeg\r\n")
                self.wfile.write(("Content-Length: %d\r\n\r\n" % len(payload)).encode("ascii"))
                self.wfile.write(payload)
                self.wfile.write(b"\r\n")
            except Exception:
                break
            slept = time.time() - t0
            if slept < period:
                time.sleep(period - slept)

    def _static(self, rel):
        rel = rel.lstrip("/")
        if not rel:
            rel = "index.html"
        path = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not path.startswith(os.path.abspath(STATIC_DIR)):
            return self._send(403, b"forbidden", "text/plain")
        if not os.path.isfile(path):
            return self._send(404, b"missing", "text/plain")
        return self._file(path)

    def _file(self, path):
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        with open(path, "rb") as handle:
            body = handle.read()
        self._send(200, body, ctype)


def _parse_body(raw, ctype):
    if not raw:
        return {}
    if ctype == "application/json":
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    try:
        text = raw.decode("utf-8")
        qs = parse_qs(text)
        return dict((k, v[0] if v else "") for k, v in qs.items())
    except Exception:
        return {}


def _local_ips():
    ips = set(["127.0.0.1"])
    try:
        import socket as _sock
        s = _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ips.add(s.getsockname()[0])
        finally:
            s.close()
        for info in _sock.getaddrinfo(_sock.gethostname(), None, _sock.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    return sorted(ips)


def ensure_self_signed_cert(cert_path, key_path):
    """Create a self-signed cert for HTTPS (mic needs a secure context)."""
    import subprocess
    if os.path.isfile(cert_path) and os.path.isfile(key_path):
        return cert_path, key_path
    try:
        os.makedirs(os.path.dirname(os.path.abspath(cert_path)), exist_ok=True)
    except TypeError:
        # Python 3.6: exist_ok not supported by os.makedirs? It is (3.2+). Keep guard.
        parent = os.path.dirname(os.path.abspath(cert_path))
        if not os.path.isdir(parent):
            os.makedirs(parent)
    san = ",".join(["DNS:jetbot", "DNS:jetbot.local", "DNS:localhost"] +
                   ["IP:%s" % ip for ip in _local_ips()])
    cmd = [
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", key_path, "-out", cert_path, "-days", "825",
        "-subj", "/CN=jetbot-console",
        "-addext", "subjectAltName=%s" % san,
    ]
    try:
        subprocess.check_call(cmd, stdout=open(os.devnull, "w"), stderr=subprocess.STDOUT)
    except Exception:
        # OpenSSL 1.1.1 supports -addext (1.1.1+); fall back without SAN.
        cmd = [c for c in cmd if not c.startswith("subjectAltName")]
        idx = cmd.index("-addext")
        del cmd[idx:idx + 2]
        subprocess.check_call(cmd, stdout=open(os.devnull, "w"), stderr=subprocess.STDOUT)
    log.info("Generated self-signed HTTPS cert %s", cert_path)
    return cert_path, key_path


def serve(runtime, host="0.0.0.0", port=8080):
    JetBotHandler.runtime = runtime
    JetBotHandler.jpeg_quality = int(runtime.cfg.get("web", {}).get("jpeg_quality", 60))
    JetBotHandler.mjpeg_fps = int(runtime.cfg.get("web", {}).get("mjpeg_fps", 12))
    server = ThreadedHTTPServer((host, int(port)), JetBotHandler)
    web_cfg = runtime.cfg.get("web", {})
    if web_cfg.get("ssl_enabled", False):
        import ssl
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cert = web_cfg.get("ssl_cert", "data/ssl/cert.pem")
        key = web_cfg.get("ssl_key", "data/ssl/key.pem")
        if not os.path.isabs(cert):
            cert = os.path.join(root, cert)
        if not os.path.isabs(key):
            key = os.path.join(root, key)
        ensure_self_signed_cert(cert, key)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        log.info("Web console on https://%s:%s (self-signed cert)", host, port)
    else:
        log.info("Web console on http://%s:%s", host, port)
    return server
