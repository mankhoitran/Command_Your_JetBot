"""Remote OpenAI-compatible LLM. Slow loop only. Summaries, never frames."""

from __future__ import print_function

import json
import logging
import re

try:
    import requests
except ImportError:
    requests = None

log = logging.getLogger("jetbot.llm")

SYSTEM_PROMPT = """You are the decision-making agent for a JetBot.
Your job is to understand the user's intent, inspect the current world state, and choose the safest feasible action through the available tools.

You are NOT the realtime controller.
Never invent PWM, GPIO, raw motor values, or low-level motor commands.
Physical safety is enforced by the Safety layer.

## 1. Decision Responsibility
You decide WHAT the robot should do.
The Safety layer decides WHETHER the requested physical action is currently safe to execute.
Do not treat Safety rejection as a reason to reject the user's intent.
For ordinary navigation commands, prefer a feasible action over refusing the request.
If the exact requested action cannot currently be executed, choose a safe alternative when one exists:
- camera.scan_environment
- turn left/right
- stop
- retry after recovery
- navigate toward a known object
- report that the robot is temporarily unable to move
Only use task.fail when the task is genuinely impossible, unsupported, or cannot proceed with the available information/tools.
Do NOT use task.fail merely because:
- the situation is uncertain
- perception is temporarily stale
- a direct action is temporarily blocked
- the robot needs to scan first
- a safer alternative exists
- the user used natural language rather than an exact tool name

## 2. Robot Motion State
Always consider the current robot state before planning motion.
The world state may contain:
- wheels: LOCKED or FREE
- estop
- recovery_active
- obstacle/front distance
- blocked
- perception freshness
- navigation target
- visible objects
If wheels are LOCKED:
- Do not plan a motion command that assumes the wheels can move.
- Do not interpret this as user intent being invalid.
- Report that motion is currently locked if relevant.
- Prefer non-motion actions such as scanning or explaining the state.
If recovery_active is true:
- Do not issue another normal motion command that conflicts with recovery.
- Allow the planner/recovery process to finish.
- If necessary, choose a non-conflicting action such as stop or scan.
The Safety layer may still reject a physically unsafe command. That is expected behavior and is NOT an Agent-level refusal.

## 3. Uncertainty
Do not require certainty before taking ordinary low-risk actions.
If you do not know enough to safely perform a requested navigation action:
1. Prefer gathering information with camera.scan_environment.
2. Use the resulting world state to continue the task.
3. Only fail the task if the required information still cannot be obtained or the target is genuinely unavailable.
Uncertainty should normally lead to information gathering, not rejection.

## 4. Object References
Users may refer to objects by natural class names: chair, table, person, door, charging station.
The world state may represent these objects using IDs such as obj_01, obj_02, obj_03.
When the user refers to an object by class/name:
1. Find the highest-confidence matching visible object.
2. Use its actual object ID when calling navigation tools.
3. Do not assume the literal word "chair" is itself a valid object ID.
4. If no suitable object is visible, scan the environment when appropriate.
5. Only report unknown_target when the target genuinely cannot be grounded.
Do not fail a task simply because the user's wording does not match the internal object ID.

## 5. Navigation
The camera is fixed to the chassis. There is no pan/tilt gimbal.
camera.look_left / look_right yaw the body; look_up / look_down / look_forward do nothing.
camera.scan_environment is a still FOV survey or in-place wheel turns, not a servo sweep.
To face a new direction, use motion.left / motion.right.
If blocked:
- Do not force forward motion.
- Prefer turning, scanning, or recovery.
- If the planner is already performing recovery, do not fight the recovery with another motion command.
If the user asks to move toward an open area:
- use available obstacle/free-space bins and front distance
- prefer the safest feasible direction
- if the environment is insufficiently observed, scan first

## 6. Natural Language
Understand common paraphrases of basic commands.
move forward / go forward / move ahead / come forward a little -> motion.forward
turn right / go right / rotate right -> motion.right
turn left / go left / rotate left -> motion.left
stop / halt / don't move -> motion.stop
look around / scan the room / check what's around -> camera.scan_environment
Do not refuse a command because the wording was not an exact tool name.

## 7. Tool Failures
Tool execution results are authoritative.
ok: true means the requested action was accepted.
ok: false means the requested action was NOT accepted.
Never report an action as successfully executed when a tool returned ok: false.
For a failed tool call: inspect the returned reason, explain that reason briefly, choose a safe alternative when appropriate, and do not invent a different failure reason.
If motion is rejected because wheels are locked: "Motion is currently locked."
If motion is rejected because perception is stale: "Motion is paused because the camera state is stale."
If the navigation target cannot be found: "I can't currently identify the requested target."
Do not convert every failure into a generic "command rejected" response.

## 8. Safety
Safety rules always have priority over navigation intent.
Never force motion through an obstacle, bypass estop, bypass motion locks, invent sensor values, invent object locations, or override Safety decisions.
However, Safety rejection does not mean the user's command was unreasonable.
Treat the pipeline as: USER INTENT -> AGENT DECISION -> PLANNER -> SAFETY VALIDATION -> EXECUTION
not: USER INTENT -> AGENT DECIDES REJECT

## 9. Output
Return exactly one JSON object. No markdown fences. No extra text.
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
- nav.go_to args: {"target": "object_id"}  (resolve class names to ids; follow if visible, else scan)
- nav.follow args: {"target": "object_id"}
- memory.remember args: {"content": "...", "reason": "user_note|confirmation|new_area"}
- task.complete / task.fail args: {"reason": "..."}
Use only the available tools.
Prefer the minimum number of tool calls required. Keep tools <= 3. Keep say <= 160 chars.
When the user's intent is clear, do not ask for confirmation.
If a safe alternative can satisfy the intent, prefer the alternative over failing the task.
If the task cannot currently proceed, report the concrete reason rather than pretending the command succeeded.
"""

ALLOWED_TOOLS = (
    "motion.forward", "motion.backward", "motion.left", "motion.right",
    "motion.stop", "motion.explore",
    "camera.look_forward", "camera.look_left", "camera.look_right",
    "camera.look_up", "camera.look_down", "camera.scan_environment",
    "camera.inspect", "nav.go_to", "nav.follow",
    "memory.remember", "task.complete", "task.fail",
)


class LLMClient(object):
    def __init__(self, cfg):
        llm = cfg.get("llm", {})
        self.base_url = llm.get("base_url", "http://192.168.20.150:8008/v1").rstrip("/")
        self.model = llm.get("model", "gemma-4-E4B-it-Q4_K_M")
        self.timeout = float(llm.get("timeout_s", 25))
        self.max_tokens = int(llm.get("max_tokens", 400))
        self.temperature = float(llm.get("temperature", 0.2))
        self.enable_thinking = bool(llm.get("enable_thinking", False))
        self.ok = False
        self.last_error = None
        self.last_latency_s = 0.0
        self.last_raw = None
        self.last_finish_reason = None

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
            "chat_template_kwargs": {"enable_thinking": self.enable_thinking},
            "response_format": {"type": "json_object"},
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
                self.last_raw = resp.text[:400]
                return None, self.last_error
            data = resp.json()
            choice = (data.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            self.last_finish_reason = choice.get("finish_reason")
            text = msg.get("content")
            if not text:
                # Gemma-IT can dump the whole budget into reasoning_content.
                text = msg.get("reasoning_content") or ""
            self.last_raw = (text or "")[:800]
            self.ok = True
            self.last_error = None
            plan = parse_plan(text)
            if self.last_finish_reason == "length" and not plan.get("tools"):
                log.warning("LLM truncated (finish=length); repaired=%s", bool(plan.get("tools")))
            return plan, None
        except Exception as exc:
            self.last_latency_s = _t.time() - t0
            self.ok = False
            self.last_error = str(exc)
            return None, self.last_error


def parse_plan(text):
    if not text:
        return _empty_plan("empty llm")
    blob = _strip_fences(text)
    start = blob.find("{")
    if start < 0:
        recovered = _recover_plan(text, reason="unstructured")
        if recovered.get("tools") or recovered.get("say"):
            return recovered
        return {
            "say": blob[:160],
            "reason": "unstructured",
            "need_llm": False,
            "tools": [],
            "done": False,
            "failed": False,
            "raw": text[:400],
        }
    candidate = blob[start:]
    data = _loads_maybe_repaired(candidate)
    if data is None:
        recovered = _recover_plan(text, reason="json parse failed")
        if recovered.get("tools") or (recovered.get("say") and recovered["say"] != "I could not parse a plan."):
            return recovered
        return _empty_plan("json parse failed", raw=text)
    tools = data.get("tools") or []
    clean = _clean_tools(tools)
    if not clean:
        # Truncation often leaves a partial tool name; recover from raw text.
        extra = _extract_tools(text)
        for tool in extra:
            if tool not in clean:
                clean.append(tool)
    say = str(data.get("say") or "")[:240]
    if say.startswith("{") or not say:
        extracted = _extract_say(text)
        if extracted:
            say = extracted[:240]
    return {
        "say": say,
        "reason": str(data.get("reason") or "")[:240],
        "need_llm": bool(data.get("need_llm")),
        "tools": clean,
        "done": bool(data.get("done")),
        "failed": bool(data.get("failed")),
        "raw": text[:400],
    }


def _strip_fences(blob):
    blob = blob.strip()
    # Drop Gemma channel / thought prefixes if thinking leaked into content.
    for marker in ("<|channel|>", "<|channel>", "</think>", "<think>"):
        idx = blob.rfind(marker)
        if idx >= 0:
            blob = blob[idx + len(marker):].lstrip()
    if "```" not in blob:
        return blob
    parts = blob.split("```")
    for part in parts:
        part = part.strip()
        if part.startswith("json"):
            part = part[4:].strip()
        if part.startswith("{"):
            return part
    return blob


def _loads_maybe_repaired(candidate):
    for blob in (candidate, _close_json(candidate)):
        end = blob.rfind("}")
        if end <= 0:
            continue
        try:
            data = json.loads(blob[:end + 1])
        except Exception:
            continue
        if isinstance(data, dict):
            return data
    return None


def _close_json(text):
    """Best-effort close of truncated JSON objects/arrays/strings."""
    out = []
    in_str = False
    escape = False
    stack = []
    for ch in text:
        out.append(ch)
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch in ("}", "]") and stack and stack[-1] == ch:
            stack.pop()
    if in_str:
        out.append('"')
    while stack:
        out.append(stack.pop())
    return "".join(out)


def _clean_tools(tools):
    clean = []
    if not isinstance(tools, list):
        return clean
    for tool in tools[:4]:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name") or tool.get("tool")
        if not name:
            continue
        name = str(name).strip()
        if name not in ALLOWED_TOOLS:
            # Truncated names like "camera." still hint at a scan.
            if name.startswith("camera"):
                name = "camera.scan_environment"
            elif name.startswith("motion"):
                continue
            else:
                continue
        args = tool.get("args") or tool.get("arguments") or {}
        if not isinstance(args, dict):
            args = {}
        clean.append({"name": name, "args": args})
    return clean


def _extract_say(text):
    match = re.search(r'"say"\s*:\s*"((?:\\.|[^"\\])*)"', text)
    if not match:
        return ""
    try:
        return json.loads('"%s"' % match.group(1))
    except Exception:
        return match.group(1)


def _extract_tools(text):
    names = re.findall(r'"name"\s*:\s*"([^"]+)"', text)
    clean = []
    seen = set()
    for name in names:
        name = name.strip()
        if name not in ALLOWED_TOOLS:
            if name.startswith("camera"):
                name = "camera.scan_environment"
            else:
                continue
        if name in seen:
            continue
        seen.add(name)
        clean.append({"name": name, "args": {}})
    return clean


def _recover_plan(text, reason):
    say = _extract_say(text) or "I could not parse a plan."
    tools = _extract_tools(text)
    return {
        "say": say[:240],
        "reason": reason,
        "need_llm": False,
        "tools": tools,
        "done": False,
        "failed": False,
        "raw": (text or "")[:400],
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
