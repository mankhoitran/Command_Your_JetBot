"""Optional TypeSafe System One client. Never on the safety path.

API: POST https://api.typesafe.ai/v1/systemone
Disabled unless typesafe.enabled and an API key exist.
"""

from __future__ import print_function

import logging
import os

try:
    import requests
except ImportError:
    requests = None

log = logging.getLogger("jetbot.typesafe")


class TypeSafeClient(object):
    def __init__(self, cfg):
        ts = cfg.get("typesafe", {})
        self.enabled = bool(ts.get("enabled"))
        self.api_url = ts.get("api_url", "https://api.typesafe.ai/v1/systemone")
        self.model = ts.get("model", "jev-latest")
        self.timeout = float(ts.get("timeout_s", 8))
        self.api_key = ts.get("api_key") or os.environ.get("TYPESAFE_API_KEY") or ""
        if self.api_key:
            self.enabled = True
        self.last_error = None
        self.ok = False

    def classify_intent(self, text):
        """Typed intent. Fail-open to None so the Agent can fall back to LLM."""
        if not self.enabled or not self.api_key:
            return None
        if requests is None:
            self.last_error = "requests missing"
            return None
        body = {
            "state": {"utterance": text},
            "model": self.model,
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
                },
            },
        }
        headers = {
            "Authorization": "Bearer " + self.api_key,
            "Content-Type": "application/json",
        }
        try:
            resp = requests.post(self.api_url, json=body, headers=headers, timeout=self.timeout)
            if resp.status_code >= 400:
                self.ok = False
                self.last_error = "http %s" % resp.status_code
                log.warning("TypeSafe error: %s %s", resp.status_code, resp.text[:200])
                return None
            data = resp.json()
            answers = data.get("answers") or {}
            intent = answers.get("intent") or {}
            noul = answers.get("needs_confirm") or {}
            self.ok = True
            self.last_error = None
            return {
                "intent": intent.get("choice"),
                "confidence": float(intent.get("confidence") or 0.0),
                "probabilities": intent.get("probabilities") or {},
                "needs_confirm": float(noul.get("noul") or 0.0),
            }
        except Exception as exc:
            self.ok = False
            self.last_error = str(exc)
            log.warning("TypeSafe unavailable: %s", exc)
            return None
