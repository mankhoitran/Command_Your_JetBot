"""External whisper.cpp ASR. Not part of reasoning."""

from __future__ import print_function

import logging
import os

try:
    import requests
except ImportError:
    requests = None

log = logging.getLogger("jetbot.whisper")


class WhisperClient(object):
    def __init__(self, cfg):
        w = cfg.get("whisper", {})
        self.base_url = w.get("base_url", "http://192.168.20.150:8003").rstrip("/")
        self.timeout = float(w.get("timeout_s", 45))
        self.ok = False
        self.last_error = None

    def health(self):
        if requests is None:
            return False, "requests missing"
        try:
            resp = requests.get(self.base_url + "/", timeout=3)
            self.ok = resp.status_code < 500
            self.last_error = None if self.ok else ("http %s" % resp.status_code)
            return self.ok, self.last_error or "ok"
        except Exception as exc:
            self.ok = False
            self.last_error = str(exc)
            return False, self.last_error

    def transcribe_bytes(self, audio_bytes, filename="speech.wav"):
        if requests is None:
            return None, "requests missing"
        url = self.base_url + "/inference"
        files = {"file": (filename, audio_bytes, "application/octet-stream")}
        data = {"temperature": "0.0", "response_format": "json"}
        try:
            resp = requests.post(url, files=files, data=data, timeout=self.timeout)
            if resp.status_code >= 400:
                self.ok = False
                self.last_error = "http %s %s" % (resp.status_code, resp.text[:160])
                return None, self.last_error
            self.ok = True
            self.last_error = None
            try:
                payload = resp.json()
            except Exception:
                return resp.text.strip(), None
            text = payload.get("text") or payload.get("transcription") or payload.get("data") or ""
            if isinstance(text, dict):
                text = text.get("text") or ""
            return (text or "").strip(), None
        except Exception as exc:
            self.ok = False
            self.last_error = str(exc)
            return None, self.last_error

    def transcribe_file(self, path):
        if not os.path.isfile(path):
            return None, "missing file"
        with open(path, "rb") as handle:
            return self.transcribe_bytes(handle.read(), filename=os.path.basename(path))
