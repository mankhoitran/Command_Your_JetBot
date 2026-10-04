"""Remote OpenAI-compatible LLM. Slow loop only. Summaries, never frames."""

from __future__ import print_function

import json
import logging

try:
    import requests
except ImportError:
    requests = None

log = logging.getLogger("jetbot.llm")

SYSTEM_PROMPT = """You are the high-level reasoner for a Jetson Nano JetBot.
You are NOT the realtime controller. Never invent PWM, GPIO, or raw motor values.
You receive a structured world summary. Reply with ONE JSON object only:
{
  "say": "short status the human can read",
  "reason": "one sentence why",
  "need_llm": false,
  "tools": [{"name": "TOOL", "args": {}}],
  "done": false,
  "failed": false
}
Allowed tools:
- motion.forward / motion.backward / motion.left / motion.right / motion.stop
  args: {"duration": seconds 0.2-3}
- motion.explore args: {"duration": seconds}
- camera.look_forward / look_left / look_right / look_up / look_down
- camera.scan_environment
- camera.inspect args: {"target": "object_id"}
- nav.go_to args: {"target": "name"}
- nav.follow args: {"target": "object_id"}
- memory.remember args: {"content": "...", "reason": "user_note|confirmation|new_area"}
- task.complete / task.fail args: {"reason": "..."}
If blocked, prefer camera scan or turn, not forcing forward.
If you do not know, say so and use camera.scan_environment.
Keep tools <= 3 per reply. Keep say <= 160 chars.
"""


class LLMClient(object):
    def __init__(self, cfg):
        llm = cfg.get("llm", {})
        self.base_url = llm.get("base_url", "http://192.168.20.150:8008/v1").rstrip("/")
        self.model = llm.get("model", "gemma-4-E4B-it-Q4_K_M")
        self.timeout = float(llm.get("timeout_s", 25))
        self.max_tokens = int(llm.get("max_tokens", 280))
        self.temperature = float(llm.get("temperature", 0.2))
        self.ok = False
        self.last_error = None
        self.last_latency_s = 0.0

    def health(self):
        if requests is None:
            return False, "requests missing"
        try:
            url = self.base_url + "/models"
            resp = requests.get(url, timeout=3)
            self.ok = resp.status_code < 400
            if self.ok:
                self.last_error = None
                try:
                    data = resp.json()
                    models = data.get("data") or data.get("models") or []
                    if models and not self.model:
                        self.model = models[0].get("id") or models[0].get("name")
                except Exception:
                    pass
                return True, "ok"
            self.last_error = "http %s" % resp.status_code
            return False, self.last_error
        except Exception as exc:
            self.ok = False
            self.last_error = str(exc)
            return False, self.last_error

    def plan(self, user_text, world_summary, memory_summary, extra=""):
        if requests is None:
            return None, "requests missing"
        import time as _t
        payload = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": (
                    "Instruction:\n%s\n\nWorld:\n%s\n\n%s\n%s"
                    % (user_text, world_summary, memory_summary, extra or "")
                )},
            ],
        }
        url = self.base_url + "/chat/completions"
        t0 = _t.time()
        try:
            resp = requests.post(url, json=payload, timeout=self.timeout)
            self.last_latency_s = _t.time() - t0
            if resp.status_code >= 400:
                self.ok = False
                self.last_error = "http %s %s" % (resp.status_code, resp.text[:200])
                return None, self.last_error
            data = resp.json()
            text = data["choices"][0]["message"]["content"]
            self.ok = True
            self.last_error = None
            return parse_plan(text), None
        except Exception as exc:
            self.last_latency_s = _t.time() - t0
            self.ok = False
            self.last_error = str(exc)
            return None, self.last_error


def parse_plan(text):
    if not text:
        return _empty_plan("empty llm")
    blob = text.strip()
    if "```" in blob:
        parts = blob.split("```")
        for part in parts:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("{"):
                blob = part
                break
    start = blob.find("{")
    end = blob.rfind("}")
    if start < 0 or end <= start:
        return {
            "say": blob[:160],
            "reason": "unstructured",
            "need_llm": False,
            "tools": [],
            "done": False,
            "failed": False,
            "raw": text[:400],
        }
    try:
        data = json.loads(blob[start:end + 1])
    except Exception:
        return _empty_plan("json parse failed", raw=text)
    tools = data.get("tools") or []
    clean = []
    if isinstance(tools, list):
        for tool in tools[:4]:
            if not isinstance(tool, dict):
                continue
            name = tool.get("name") or tool.get("tool")
            if not name:
                continue
            args = tool.get("args") or tool.get("arguments") or {}
            if not isinstance(args, dict):
                args = {}
            clean.append({"name": str(name), "args": args})
    return {
        "say": str(data.get("say") or "")[:240],
        "reason": str(data.get("reason") or "")[:240],
        "need_llm": bool(data.get("need_llm")),
        "tools": clean,
        "done": bool(data.get("done")),
        "failed": bool(data.get("failed")),
        "raw": text[:400],
    }


def _empty_plan(reason, raw=""):
    return {
        "say": "I could not parse a plan.",
        "reason": reason,
        "need_llm": False,
        "tools": [],
        "done": False,
        "failed": False,
        "raw": raw[:400],
    }
