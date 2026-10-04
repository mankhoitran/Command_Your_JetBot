"""Agent orchestration. Reasons over summaries, never frames."""

from __future__ import print_function

import logging
import threading
import time

from .events import EventType
from .task import (
    ACTIVE_PERCEPTION, CANCELLED, COMPLETED, EXECUTING, FAILED, IDLE,
    OBSERVING, PLANNING, REPLANNING, VERIFYING,
)

log = logging.getLogger("jetbot.agent")

INTENT_TOOLS = {
    "stop": [("motion.stop", {})],
    "forward": [("motion.forward", {"duration": 1.0})],
    "left": [("motion.left", {"duration": 0.6})],
    "right": [("motion.right", {"duration": 0.6})],
    "backward": [("motion.backward", {"duration": 0.6})],
    "scan": [("camera.scan_environment", {})],
    "explore": [("motion.explore", {"duration": 3.0})],
    "follow": [("nav.follow", {})],
    "status": [],
}


class Agent(object):
    def __init__(self, cfg, world, bus, llm, tools, tasks, memory, typesafe, health):
        self.cfg = cfg
        self.world = world
        self.bus = bus
        self.llm = llm
        self.tools = tools
        self.tasks = tasks
        self.memory = memory
        self.typesafe = typesafe
        self.health = health
        self.min_reason_s = float(cfg.get("llm", {}).get("min_reason_interval_s", 6.0))
        self._lock = threading.Lock()
        self._pending = []
        self._last_reason = 0.0
        self._busy = False
        self.last_plan = None
        self.last_say = "idle"
        self.decisions = []
        self._stop = threading.Event()
        self._thread = None
        self.hz = float(cfg.get("loops", {}).get("agent_hz", 4))
        bus.subscribe(EventType.NAVIGATION_BLOCKED, self._on_blocked)
        bus.subscribe(EventType.VOICE_TRANSCRIPT, self._on_voice)
        bus.subscribe(EventType.USER_COMMAND, self._on_user)
        bus.subscribe(EventType.EMERGENCY_STOP, self._on_estop)

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="agent")
        self._thread.daemon = True
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def submit(self, text, source="web"):
        text = (text or "").strip()
        if not text:
            return None
        item = {"text": text, "source": source, "ts": time.time()}
        with self._lock:
            self._pending.append(item)
        self.bus.emit(EventType.USER_COMMAND, source, {"text": text})
        return item

    def _on_voice(self, event):
        text = (event.payload or {}).get("text")
        if text:
            self.submit(text, source="whisper")

    def _on_user(self, event):
        pass

    def _on_estop(self, event):
        with self._lock:
            self._pending = []
        try:
            if self.tasks.state not in (IDLE, COMPLETED, FAILED):
                self.tasks.cancel("estop")
        except Exception:
            pass
        self.last_say = "emergency stop"

    def _on_blocked(self, event):
        # Deterministic local recovery is already running. Ask LLM only if a task is active.
        if self.tasks.state in (EXECUTING, OBSERVING):
            try:
                self.tasks.transition(REPLANNING, action="blocked")
            except ValueError:
                pass
            self.submit("Path blocked. Replan using current world state. Do not ram the obstacle.", source="nav")

    def _loop(self):
        period = 1.0 / max(1.0, self.hz)
        while not self._stop.is_set():
            t0 = time.time()
            item = None
            with self._lock:
                if self._pending and not self._busy:
                    item = self._pending.pop(0)
            if item is not None:
                self._handle(item)
            slept = time.time() - t0
            if slept < period:
                time.sleep(period - slept)

    def _handle(self, item):
        self._busy = True
        text = item["text"]
        try:
            if self.tasks.state in (IDLE, COMPLETED, FAILED, CANCELLED):
                self.tasks.start(text[:80], goal=text)
            else:
                try:
                    self.tasks.transition(PLANNING, action="reason")
                except ValueError:
                    pass

            # Cheap local intents (and TypeSafe if configured) avoid LLM round-trips.
            local = self._local_intent(text)
            if local is not None:
                intent, conf = local
                if intent == "status":
                    self.last_say = self.world.summary_for_llm()[:240]
                    self._record("local", intent, self.last_say, [])
                    try:
                        self.tasks.transition(COMPLETED, action="status", progress=1.0)
                    except ValueError:
                        pass
                    return
                if intent == "stop":
                    self.tools.dispatch("motion.stop", {})
                    self.last_say = "Stopping."
                    self._record("local", intent, self.last_say, [{"name": "motion.stop", "args": {}}])
                    try:
                        self.tasks.transition(COMPLETED, action="stop", progress=1.0)
                    except ValueError:
                        pass
                    return
                tools = INTENT_TOOLS.get(intent) or []
                results = [self.tools.dispatch(n, a) for n, a in tools]
                self.last_say = "OK: %s" % intent
                self._record("local", intent, self.last_say, [{"name": n, "args": a} for n, a in tools], results)
                return

            now = time.time()
            if now - self._last_reason < self.min_reason_s:
                time.sleep(max(0.0, self.min_reason_s - (now - self._last_reason)))

            world_s = self.world.summary_for_llm()
            mem_s = self.memory.summary_for_llm(text)
            plan, err = self.llm.plan(text, world_s, mem_s)
            self._last_reason = time.time()
            if plan is None:
                self.bus.emit(EventType.LLM_UNAVAILABLE, "llm", {"error": err})
                self.health.set("llm", False, err or "unavailable")
                self.last_say = "LLM unavailable; holding position. %s" % (err or "")
                self.tools.dispatch("motion.stop", {})
                self._record("llm-fail", None, self.last_say, [])
                try:
                    self.tasks.transition(FAILED, action="llm", failure=err or "llm")
                except ValueError:
                    pass
                return
            self.health.set("llm", True, "ok", extra={"latency_s": self.llm.last_latency_s})
            self.last_plan = plan
            self.last_say = plan.get("say") or ""
            results = []
            for tool in plan.get("tools") or []:
                results.append(self.tools.dispatch(tool.get("name"), tool.get("args") or {}))
            if plan.get("failed"):
                try:
                    self.tasks.transition(FAILED, action="plan", failure=plan.get("reason"))
                except ValueError:
                    pass
            elif plan.get("done") and not (plan.get("tools")):
                try:
                    self.tasks.transition(COMPLETED, action="done", progress=1.0)
                except ValueError:
                    pass
            else:
                try:
                    self.tasks.transition(EXECUTING, action="tools", progress=0.5)
                except ValueError:
                    try:
                        self.tasks.transition(OBSERVING, action="tools")
                    except ValueError:
                        pass
            self._record("llm", None, self.last_say, plan.get("tools") or [], results, plan.get("reason"))
            if plan.get("say"):
                self.memory.add_note(
                    "Decision: %s (%s)" % (plan.get("say"), plan.get("reason") or ""),
                    reason="confirmation", source="agent", confidence=0.5,
                    context=text[:120],
                )
        except Exception:
            log.exception("agent handle failed")
        finally:
            self._busy = False

    def _local_intent(self, text):
        ts = None
        if self.typesafe is not None:
            ts = self.typesafe.classify_intent(text)
        if ts and ts.get("intent") and ts.get("confidence", 0) >= 0.72 and ts.get("intent") != "unknown":
            if ts.get("needs_confirm", 0) >= 0.7:
                return None
            return ts["intent"], ts["confidence"]
        t = text.strip().lower()
        mapping = [
            (("stop", "halt", "freeze", "estop", "e-stop"), "stop"),
            (("look around", "scan", "survey", "inspect room"), "scan"),
            (("explore", "wander", "look around the room"), "explore"),
            (("follow me", "follow", "come here"), "follow"),
            (("status", "where are you", "what's going on", "report"), "status"),
            (("go forward", "move forward", "drive forward", "forward", "ahead"), "forward"),
            (("back", "backward", "reverse"), "backward"),
            (("left", "turn left"), "left"),
            (("right", "turn right"), "right"),
        ]
        for keys, intent in mapping:
            for key in keys:
                if t == key or t.startswith(key + " ") or t.endswith(" " + key):
                    return intent, 0.9
        return None

    def _record(self, kind, intent, say, tools, results=None, reason=""):
        entry = {
            "t": time.time(),
            "kind": kind,
            "intent": intent,
            "say": say,
            "reason": reason,
            "tools": tools,
            "results": results or [],
        }
        self.decisions.append(entry)
        if len(self.decisions) > 40:
            self.decisions = self.decisions[-40:]
        self.tools.last_say = say

    def snapshot(self):
        return {
            "say": self.last_say,
            "busy": self._busy,
            "pending": len(self._pending),
            "last_plan": self.last_plan,
            "decisions": list(self.decisions[-8:]),
            "task": self.tasks.snapshot(),
        }
