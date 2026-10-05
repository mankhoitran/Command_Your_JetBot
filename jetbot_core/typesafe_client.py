"""Jev (TypeSafe System One) via OpenRouter. Never on the safety path.

OpenRouter Decisions API: POST https://openrouter.ai/api/alpha/decisions
Model: ~typesafe/jev-latest (alias) or typesafe/jev-1.13
Disabled unless an API key exists (OPENROUTER_API_KEY / TYPESAFE_API_KEY).
"""

from __future__ import print_function

import json
import logging
import os

try:
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen
except ImportError:
    Request = None
    urlopen = None
    HTTPError = URLError = Exception

log = logging.getLogger("jetbot.typesafe")

DEFAULT_API_URL = "https://openrouter.ai/api/alpha/decisions"
DEFAULT_MODEL = "~typesafe/jev-latest"


def _http_json(method, url, headers, body=None, timeout=8):
    if Request is None or urlopen is None:
        raise RuntimeError("urllib missing")
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = Request(url, data=data, headers=headers)
    req.get_method = lambda: method
    try:
        resp = urlopen(req, timeout=timeout)
        raw = resp.read()
        code = getattr(resp, "code", 200)
    except HTTPError as exc:
        raw = exc.read() if hasattr(exc, "read") else b""
        code = getattr(exc, "code", 0)
        text = raw.decode("utf-8", "replace") if raw else str(exc)
        raise RuntimeError("http %s: %s" % (code, text[:300]))
    except URLError as exc:
        raise RuntimeError(str(exc.reason if hasattr(exc, "reason") else exc))
    text = raw.decode("utf-8", "replace") if raw else ""
    if code >= 400:
        raise RuntimeError("http %s: %s" % (code, text[:300]))
    if not text:
        return {}
    return json.loads(text)


class TypeSafeClient(object):
    def __init__(self, cfg):
        ts = cfg.get("typesafe", {})
        self.api_url = ts.get("api_url") or DEFAULT_API_URL
        self.model = ts.get("model") or DEFAULT_MODEL
        self.timeout = float(ts.get("timeout_s", 8))
        self.api_key = (
            ts.get("api_key")
            or os.environ.get("OPENROUTER_API_KEY")
            or os.environ.get("TYPESAFE_API_KEY")
            or ""
        )
        enabled = bool(ts.get("enabled"))
        if self.api_key:
            enabled = True
        self.enabled = enabled
        self.last_error = None
        self.ok = False
        self.last_model = None

    def _headers(self):
        return {
            "Authorization": "Bearer " + self.api_key,
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/mankhoitran/Command_Your_JetBot",
            "X-OpenRouter-Title": "Command Your JetBot",
        }

    def health(self):
        if not self.enabled or not self.api_key:
            return False, "disabled"
        try:
            data = _http_json(
                "GET",
                "https://openrouter.ai/api/v1/key",
                self._headers(),
                timeout=min(self.timeout, 6),
            )
            info = data.get("data") or data
            self.ok = True
            self.last_error = None
            label = str(info.get("label") or info.get("name") or "ok")
            if label.startswith("sk-"):
                label = "key-ok"
            return True, "openrouter %s" % label
        except Exception as exc:
            self.ok = False
            self.last_error = str(exc)
            return False, self.last_error

    def classify_intent(self, text):
        """Typed intent. Fail-open to None so the Agent can fall back to LLM."""
        if not self.enabled or not self.api_key:
            return None
        body = {
            "model": self.model,
            "state": {"utterance": text},
            "questions": {
                "intent": {
                    "type": "choice",
                    "instructions": "What should the JetBot do?",
                    "criteria": {
                        "stop": "Halt immediately",
                        "forward": "Drive forward a bit",
                        "left": "Turn left",
                        "right": "Turn right",
                        "backward": "Back up",
                        "scan": "Look around / inspect the room",
                        "explore": "Wander while avoiding obstacles",
                        "follow": "Follow a person or object",
                        "status": "Report current state",
                        "unknown": "Not a robot command or too ambiguous",
                    },
                },
                "needs_confirm": {
                    "type": "noul",
                    "instructions": "Is this command ambiguous or unsafe enough to ask the human first?",
                    "criteria": {
                        "true": "Ambiguous, unsafe, or should be confirmed before moving.",
                        "false": "Clear, safe command the robot can act on.",
                    },
                },
            },
        }
        try:
            data = _http_json("POST", self.api_url, self._headers(), body, timeout=self.timeout)
            answers = data.get("answers") or {}
            intent = answers.get("intent") or {}
            noul = answers.get("needs_confirm") or {}
            self.ok = True
            self.last_error = None
            self.last_model = data.get("model")
            return {
                "intent": intent.get("choice"),
                "confidence": float(intent.get("confidence") or 0.0),
                "probabilities": intent.get("probabilities") or {},
                "needs_confirm": float(noul.get("noul") or 0.0),
                "model": data.get("model") or self.model,
            }
        except Exception as exc:
            self.ok = False
            self.last_error = str(exc)
            log.warning("Jev/OpenRouter unavailable: %s", exc)
            return None
