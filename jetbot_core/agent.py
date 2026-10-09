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

_LLM_NEEDLES = (
    "replan", "path blocked", "do not ram",
    "tell me", "do you see", "what do you see",
    "look for", "find a", "find the",
    "toward", "towards", "go through",
    "closer to", "move closer",
)

TOOL_FAIL_SAY = {
    "estop": "Emergency stop is active.",
    "motion_locked": "Motion is currently locked.",
    "stale_perception": "Motion is paused because the camera state is stale.",
    "obstacle": "Path is blocked; holding.",
    "recovery_active": "Recovery is in progress; waiting.",
    "unknown_target": "I can't currently identify the requested target.",
    "scan already running": "A scan is already running.",
}


def _needs_llm(text):
    """True when a cheap local/Jev intent would swallow a command that needs a plan."""
    if not text:
        return False
    return any(needle in text for needle in _LLM_NEEDLES)


def _normalize_command(text):
    t = (text or "").strip().lower()
    t = t.replace("'", "").replace("\u2019", "")
    return t


def _phrase_match(text, key):
    """Match a keyword as a command, not as an incidental word in a longer request."""
    t = _normalize_command(text)
    k = _normalize_command(key)
    if not t or not k:
        return False
    if t == k:
        return True
    if t.startswith(k + " "):
        return True
    # Multi-word keys ("go forward", "turn left") may appear at the end or inside.
    if " " in k:
        if t.endswith(" " + k):
            return True
        if (" " + k + " ") in (" " + t + " "):
            return True
        return False
    # Single-token left/right are too common as adjectives ("on the left").
    if k in ("left", "right"):
        return False
    return t.endswith(" " + k)


def say_for_tool_results(results, default_say):
    """Honest status: never claim OK when a tool returned ok:false."""
    for result in results or []:
        if not isinstance(result, dict):
            continue
        if result.get("ok"):
            continue
        err = str(result.get("error") or "failed")
        return (TOOL_FAIL_SAY.get(err) or err)[:240]
    return default_say


class Agent(object):
    def __init__(self, cfg, world, bus, llm, tools, tasks, memory, typesafe, health,
                 safety=None, planner=None, spatial=None):
        self.cfg = cfg
        self.world = world
        self.bus = bus
        self.llm = llm
        self.tools = tools
        self.tasks = tasks
        self.memory = memory
        self.typesafe = typesafe
        self.health = health
        self.safety = safety
        self.planner = planner
        self.spatial = spatial
        self.min_reason_s = float(cfg.get("llm", {}).get("min_reason_interval_s", 6.0))
        self._lock = threading.Lock()
        self._pending = []
        self._last_reason = 0.0
        self._busy = False
        self._abort = False
        self.last_plan = None
        self.last_say = "idle"
        self.decisions = []
        self._stop = threading.Event()
        self._thread = None
        self.hz = float(cfg.get("loops", {}).get("agent_hz", 4))
        bus.subscribe(EventType.NAVIGATION_BLOCKED, self._on_blocked)
        bus.subscribe(EventType.NAVIGATION_RECOVERY_FAILED, self._on_recovery_failed)
        bus.subscribe(EventType.NAVIGATION_RECOVERED, self._on_recovered)
        bus.subscribe(EventType.VOICE_TRANSCRIPT, self._on_voice)
        bus.subscribe(EventType.USER_COMMAND, self._on_user)
        bus.subscribe(EventType.EMERGENCY_STOP, self._on_estop)
        bus.subscribe(EventType.SCAN_COMPLETED, self._on_scan_done)
        bus.subscribe(EventType.OBJECT_DETECTED, self._on_object)

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
            self._abort = True
        try:
            if self.tasks.state not in (IDLE, COMPLETED, FAILED):
                self.tasks.cancel("estop")
        except Exception:
            pass
        self.last_say = "emergency stop"

    def _named_task(self):
        return self.tasks.state not in (IDLE, COMPLETED, FAILED, CANCELLED) and bool(self.tasks.task_id)

    def _on_blocked(self, event):
        # Local recovery is already running. Do not enqueue an LLM replan yet.
        if not self._named_task():
            return
        if self.tasks.state in (EXECUTING, OBSERVING):
            try:
                self.tasks.transition(REPLANNING, action="blocked")
            except ValueError:
                pass

    def _on_recovery_failed(self, event):
        if not self._named_task():
            return
        try:
            if self.tasks.state not in (REPLANNING,):
                self.tasks.transition(REPLANNING, action="recovery_failed")
        except ValueError:
            pass
        self.submit(
            "Path blocked. Recovery failed. Replan using current world state. Do not ram the obstacle.",
            source="nav",
        )

    def _on_recovered(self, event):
        if self.tasks.state == REPLANNING:
            try:
                self.tasks.transition(EXECUTING, action="recovered")
            except ValueError:
                pass

    def _on_scan_done(self, event):
        if self.tasks.state == ACTIVE_PERCEPTION:
            try:
                self.tasks.transition(OBSERVING, action="scan_done", progress=0.3)
            except ValueError:
                pass

    def _on_object(self, event):
        if self.spatial is None:
            return
        ids = (event.payload or {}).get("ids") or []
        objs = self.world.objects_view()
        for oid in ids:
            obj = objs.get(oid)
            if obj:
                self.spatial.remember_landmark(
                    obj.get("id"), obj.get("class"), obj.get("position"),
                    obj.get("confidence", 0.5), source="perception",
                )

    def _aborted(self):
        if self._abort:
            return True
        if self.safety is not None and self.safety.estop:
            return True
        return False

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
        user_source = item.get("source")
        if user_source != "nav":
            self._abort = False
        try:
            if self._aborted():
                self.last_say = "emergency stop"
                return
            if self.tasks.state in (IDLE, COMPLETED, FAILED, CANCELLED):
                self.tasks.start(text[:80], goal=text)
            else:
                try:
                    self.tasks.transition(PLANNING, action="reason")
                except ValueError:
                    pass

            local = self._local_intent(text, source=user_source)
            if local is not None:
                intent, conf = local
                if self._aborted():
                    return
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
                results = []
                for n, a in tools:
                    if self._aborted():
                        break
                    results.append(self.tools.dispatch(n, a))
                self.last_say = say_for_tool_results(results, "OK: %s" % intent)
                self._record("local", intent, self.last_say, [{"name": n, "args": a} for n, a in tools], results)
                try:
                    self.tasks.transition(EXECUTING, action="tools", progress=0.5)
                except ValueError:
                    pass
                return

            now = time.time()
            if now - self._last_reason < self.min_reason_s:
                time.sleep(max(0.0, self.min_reason_s - (now - self._last_reason)))
            if self._aborted():
                return

            world_s = self.world.summary_for_llm()
            if self.spatial is not None:
                world_s = world_s + "\n" + self.spatial.summary()
            mem_s = self.memory.summary_for_llm(text)
            plan, err = self.llm.plan(text, world_s, mem_s)
            self._last_reason = time.time()
            if self._aborted():
                self.last_say = "emergency stop"
                return
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
            plan = self._ensure_usable_plan(plan, text)
            self.last_plan = plan
            self.last_say = plan.get("say") or ""
            results = []
            for tool in plan.get("tools") or []:
                if self._aborted():
                    break
                results.append(self.tools.dispatch(tool.get("name"), tool.get("args") or {}))
            if self._aborted():
                self.last_say = "emergency stop"
                return
            self.last_say = say_for_tool_results(results, plan.get("say") or "")
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

    def _keyword_intent(self, text):
        mapping = [
            (("stop", "halt", "freeze", "estop", "e-stop", "dont move", "do not move"), "stop"),
            (("look around", "scan the room", "check whats around", "inspect room", "survey", "scan"), "scan"),
            (("explore", "wander", "look around the room"), "explore"),
            (("follow me", "come here", "follow"), "follow"),
            (("status", "where are you", "whats going on", "report"), "status"),
            (("go forward", "move forward", "drive forward", "come forward", "move ahead", "go ahead", "forward", "ahead"), "forward"),
            (("go backward", "move backward", "drive backward", "go back", "move back", "back up", "backup", "backward", "reverse"), "backward"),
            (("turn left", "go left", "rotate left", "to the left", "left"), "left"),
            (("turn right", "go right", "rotate right", "to the right", "right"), "right"),
        ]
        for keys, intent in mapping:
            for key in keys:
                if _phrase_match(text, key):
                    return intent, 0.9
        return None

    def _local_intent(self, text, source=""):
        t = (text or "").strip().lower()
        if source in ("nav",) or _needs_llm(t):
            return None
        hit = self._keyword_intent(text)
        if hit is not None:
            return hit
        ts = None
        if self.typesafe is not None:
            ts = self.typesafe.classify_intent(text)
        if ts and ts.get("intent") and ts.get("confidence", 0) >= 0.72 and ts.get("intent") != "unknown":
            if ts.get("needs_confirm", 0) >= 0.7:
                return None
            return ts["intent"], ts["confidence"]
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

    def _ensure_usable_plan(self, plan, text):
        """If Gemma truncated JSON, still emit a concrete tool decision."""
        plan = dict(plan or {})
        tools = plan.get("tools") or []
        say = (plan.get("say") or "").strip()
        bad_parse = plan.get("reason") in ("unstructured", "json parse failed", "empty llm")
        if say.startswith("{") or say == "I could not parse a plan.":
            bad_parse = True
        if tools and not say.startswith("{"):
            return plan
        blocked = False
        try:
            blocked = bool((self.world.obstacles_view().get("obstacles") or {}).get("blocked"))
        except Exception:
            blocked = False
        if blocked or "block" in (text or "").lower():
            plan["tools"] = [{"name": "camera.scan_environment", "args": {}}]
            if bad_parse or not say or say.startswith("{"):
                plan["say"] = "Path blocked; scanning for a way around."
            plan["reason"] = plan.get("reason") or "fallback scan"
            return plan
        if bad_parse and not tools:
            plan["tools"] = [{"name": "camera.scan_environment", "args": {}}]
            if not say or say.startswith("{") or say == "I could not parse a plan.":
                plan["say"] = "I could not finish a plan; scanning the room."
            plan["reason"] = plan.get("reason") or "fallback scan"
        return plan

    def snapshot(self):
        return {
            "say": self.last_say,
            "busy": self._busy,
            "pending": len(self._pending),
            "last_plan": self.last_plan,
            "decisions": list(self.decisions[-8:]),
        }
