"""Live-editable agent settings (prompt, tools, keywords). Persisted locally."""

from __future__ import print_function

import copy
import json
import logging
import os
import threading
import time

log = logging.getLogger("jetbot.live")

DEFAULT_TOOL_DOCS = {
    "motion.forward": "Drive forward. args: {duration: 0.2-3s}",
    "motion.backward": "Back up. args: {duration: 0.2-3s}",
    "motion.left": "Turn left in place. args: {duration: 0.2-3s}",
    "motion.right": "Turn right in place. args: {duration: 0.2-3s}",
    "motion.stop": "Halt immediately.",
    "motion.explore": "Wander while avoiding obstacles. args: {duration}",
    "camera.look_forward": "No-op on a bolted camera.",
    "camera.look_left": "Yaw the body left (fixed camera).",
    "camera.look_right": "Yaw the body right (fixed camera).",
    "camera.look_up": "No-op on a bolted camera.",
    "camera.look_down": "No-op on a bolted camera.",
    "camera.scan_environment": "Still FOV survey or in-place wheel survey.",
    "camera.inspect": "Look at an object. args: {target: object_id}",
    "nav.go_to": "Navigate toward an object. args: {target: object_id|class}",
    "nav.follow": "Follow a visible object. args: {target: object_id|class}",
    "memory.remember": "Store a note. args: {content, reason}",
    "task.complete": "Mark the current task done. args: {reason}",
    "task.fail": "Mark the current task failed. args: {reason}",
}


def default_keyword_map():
    # Keep in sync with Agent._keyword_intent. Editable via monitor.
    return [
        [["stop", "halt", "freeze", "estop", "e-stop", "dont move", "do not move"], "stop"],
        [["look around", "scan the room", "check whats around", "inspect room", "survey", "scan"], "scan"],
        [["explore", "wander", "look around the room"], "explore"],
        [["follow me", "come here", "follow"], "follow"],
        [["status", "where are you", "whats going on", "report"], "status"],
        [["go forward", "move forward", "drive forward", "come forward", "move ahead", "go ahead", "forward", "ahead"], "forward"],
        [["go backward", "move backward", "drive backward", "go back", "move back", "back up", "backup", "backward", "reverse"], "backward"],
        [["turn left", "go left", "rotate left", "to the left", "left"], "left"],
        [["turn right", "go right", "rotate right", "to the right", "right"], "right"],
    ]


def default_intent_tools():
    from .agent import INTENT_TOOLS
    out = {}
    for intent, tools in INTENT_TOOLS.items():
        out[intent] = [{"name": n, "args": dict(a)} for n, a in tools]
    return out


class LiveSettings(object):
    """Overlay applied on top of code defaults. Survives restart via JSON."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.RLock()
        self.updated_ts = 0.0
        self.data = {
            "system_prompt": None,
            "allowed_tools": None,
            "tool_docs": {},
            "tool_enabled": {},
            "intent_tools": None,
            "keyword_map": None,
            "llm": {},
        }
        self.load()

    def load(self):
        if not self.path or not os.path.isfile(self.path):
            return
        try:
            with open(self.path, "r") as handle:
                overlay = json.load(handle) or {}
            if isinstance(overlay, dict):
                with self._lock:
                    self.data.update(overlay)
                    self.updated_ts = os.path.getmtime(self.path)
            log.info("Loaded live settings from %s", self.path)
        except Exception as exc:
            log.warning("live settings load failed: %s", exc)

    def save(self):
        if not self.path:
            return False
        parent = os.path.dirname(os.path.abspath(self.path))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent)
        tmp = self.path + ".tmp"
        with self._lock:
            payload = copy.deepcopy(self.data)
        with open(tmp, "w") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
        os.replace(tmp, self.path)
        self.updated_ts = time.time()
        return True

    def snapshot(self):
        with self._lock:
            return copy.deepcopy(self.data)

    def patch(self, overlay):
        if not isinstance(overlay, dict):
            raise ValueError("overlay must be an object")
        with self._lock:
            for key in ("system_prompt", "allowed_tools", "tool_docs",
                        "tool_enabled", "intent_tools", "keyword_map", "llm"):
                if key not in overlay:
                    continue
                value = overlay[key]
                if key == "llm" and isinstance(value, dict):
                    self.data.setdefault("llm", {}).update(value)
                elif key == "tool_docs" and isinstance(value, dict):
                    self.data.setdefault("tool_docs", {}).update(value)
                elif key == "tool_enabled" and isinstance(value, dict):
                    self.data.setdefault("tool_enabled", {}).update(value)
                else:
                    self.data[key] = value
            self.updated_ts = time.time()
        return self.snapshot()

    def apply(self, runtime):
        """Push overlay onto the running Agent / LLM / ToolRouter. Instant."""
        data = self.snapshot()
        llm = getattr(runtime, "llm", None)
        agent = getattr(runtime, "agent", None)
        tools = getattr(runtime, "tools", None)

        prompt = data.get("system_prompt")
        if llm is not None:
            from .llm import SYSTEM_PROMPT
            llm.system_prompt = prompt if prompt else SYSTEM_PROMPT
            allowed = data.get("allowed_tools")
            if allowed:
                llm.allowed_tools = list(allowed)
            else:
                from .llm import ALLOWED_TOOLS
                llm.allowed_tools = list(ALLOWED_TOOLS)
            llm_over = data.get("llm") or {}
            for attr in ("model", "temperature", "max_tokens", "timeout", "enable_thinking", "base_url"):
                if attr in llm_over and llm_over[attr] not in (None, ""):
                    if attr == "timeout":
                        llm.timeout = float(llm_over[attr])
                    elif attr == "max_tokens":
                        llm.max_tokens = int(llm_over[attr])
                    elif attr == "temperature":
                        llm.temperature = float(llm_over[attr])
                    elif attr == "enable_thinking":
                        llm.enable_thinking = bool(llm_over[attr])
                    elif attr == "base_url":
                        llm.base_url = str(llm_over[attr]).rstrip("/")
                    elif attr == "model":
                        llm.model = str(llm_over[attr])

        if tools is not None:
            enabled = data.get("tool_enabled") or {}
            tools.set_enabled_map(enabled)
            docs = data.get("tool_docs") or {}
            if docs:
                tools.tool_docs.update(docs)

        if agent is not None:
            intent = data.get("intent_tools")
            if intent:
                agent.set_intent_tools(intent)
            else:
                agent.reset_intent_tools()
            kmap = data.get("keyword_map")
            if kmap:
                agent.set_keyword_map(kmap)
            else:
                agent.reset_keyword_map()
        return True
