"""Read-only telemetry for the pipeline monitor.

Not on the safety path. Loop meters, motor phases, correlation IDs, and
the three-view payload are derived from real runtime instrumentation.
Never infers successful execution from a submitted request.
"""

from __future__ import print_function

import threading
import time
import uuid

from .events import EventType


_SECRET_BITS = ("api_key", "authorization", "password", "token", "secret", "cookie")

_local = threading.local()


def new_corr_id():
    return uuid.uuid4().hex[:12]


def get_corr_id():
    return getattr(_local, "corr_id", None)


def set_corr_id(corr_id):
    _local.corr_id = corr_id
    return corr_id


def clear_corr_id():
    _local.corr_id = None


def bind_corr_id(corr_id):
    """Set corr_id, returning the previous value for restore."""
    prev = get_corr_id()
    _local.corr_id = corr_id
    return prev


def redact(value, depth=0):
    """Strip credentials and oversized blobs from telemetry payloads."""
    if depth > 4:
        return "<max-depth>"
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            low = str(key).lower()
            if any(bit in low for bit in _SECRET_BITS):
                out[key] = "<redacted>"
            else:
                out[key] = redact(item, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        if len(value) > 40:
            return [redact(v, depth + 1) for v in value[:40]] + ["…[%d]" % len(value)]
        return [redact(v, depth + 1) for v in value]
    if isinstance(value, (bytes, bytearray)):
        return "<bytes %d>" % len(value)
    return value


def _cmd_fields(command):
    if command is None:
        return {"left": None, "right": None, "source": None, "duration_s": None, "corr_id": None}
    if isinstance(command, dict):
        return {
            "left": command.get("left"),
            "right": command.get("right"),
            "source": command.get("source"),
            "duration_s": command.get("duration_s"),
            "corr_id": command.get("corr_id"),
        }
    return {
        "left": getattr(command, "left", None),
        "right": getattr(command, "right", None),
        "source": getattr(command, "source", None),
        "duration_s": getattr(command, "duration_s", None),
        "corr_id": getattr(command, "corr_id", None) or get_corr_id(),
    }


class LoopMeter(object):
    """Cheap period tracker. tick() is a few arithmetic ops under a lock."""

    def __init__(self, name, target_hz, window=40):
        self.name = name
        self.target_hz = float(target_hz) if target_hz else 0.0
        self._window = max(8, int(window))
        self._lock = threading.Lock()
        self._times = []
        self.last_period_ms = 0.0
        self.max_period_ms = 0.0
        self.overruns = 0
        self.ticks = 0
        self.last_ts = 0.0

    def tick(self):
        now = time.time()
        with self._lock:
            if self.last_ts:
                period = now - self.last_ts
                self.last_period_ms = period * 1000.0
                if self.last_period_ms > self.max_period_ms:
                    self.max_period_ms = self.last_period_ms
                if self.target_hz > 0:
                    expected = 1.0 / self.target_hz
                    if period > expected * 1.6:
                        self.overruns += 1
            self.last_ts = now
            self.ticks += 1
            self._times.append(now)
            if len(self._times) > self._window:
                self._times = self._times[-self._window:]

    def snapshot(self):
        now = time.time()
        with self._lock:
            hz = 0.0
            if len(self._times) >= 2:
                span = self._times[-1] - self._times[0]
                if span > 0:
                    hz = (len(self._times) - 1) / span
            age = (now - self.last_ts) if self.last_ts else None
            target = self.target_hz
            last_ms = self.last_period_ms
            max_ms = self.max_period_ms
            overruns = self.overruns
            ticks = self.ticks
            last_ts = self.last_ts
        stale = False
        if ticks and target > 0 and age is not None:
            stale = age > (3.0 / target)
        return {
            "name": self.name,
            "target_hz": round(target, 2),
            "hz": round(hz, 2),
            "last_period_ms": round(last_ms, 2),
            "max_period_ms": round(max_ms, 2),
            "overruns": overruns,
            "ticks": ticks,
            "last_ts": last_ts,
            "age_s": None if age is None else round(age, 3),
            "stale": stale,
        }


class MotorLedger(object):
    """Requested / accepted / applied / rejected motor phases.

    Applied is recorded only when the written speeds or source change, so a
    20 Hz watchdog does not flood the bounded history.
    """

    def __init__(self, limit=80):
        self._lock = threading.Lock()
        self._history = []
        self._limit = int(limit)
        self._seq = 0
        self.last_requested = None
        self.last_accepted = None
        self.last_applied = None
        self.last_rejected = None
        self._applied_key = None

    def record(self, phase, command, reason=None, corr_id=None, history=True):
        fields = _cmd_fields(command)
        if corr_id:
            fields["corr_id"] = corr_id
        elif not fields.get("corr_id"):
            fields["corr_id"] = get_corr_id()
        item = {
            "ts": time.time(),
            "phase": phase,
            "left": fields["left"],
            "right": fields["right"],
            "source": fields["source"],
            "duration_s": fields["duration_s"],
            "reason": reason,
            "corr_id": fields["corr_id"],
        }
        with self._lock:
            self._seq += 1
            item["seq"] = self._seq
            if phase == "requested":
                self.last_requested = item
            elif phase == "accepted":
                self.last_accepted = item
            elif phase == "applied":
                key = (
                    round(float(item["left"] or 0.0), 3),
                    round(float(item["right"] or 0.0), 3),
                    item["source"],
                )
                if key == self._applied_key and history:
                    self.last_applied = item
                    return item
                self._applied_key = key
                self.last_applied = item
            elif phase == "rejected":
                self.last_rejected = item
            if history:
                self._history.append(item)
                if len(self._history) > self._limit:
                    self._history = self._history[-self._limit:]
        return item

    def for_corr(self, corr_id, limit=16):
        if not corr_id:
            return []
        with self._lock:
            items = [row for row in self._history if row.get("corr_id") == corr_id]
        if limit:
            items = items[-int(limit):]
        return list(items)

    def snapshot(self, limit=16):
        with self._lock:
            hist = list(self._history)
            payload = {
                "seq": self._seq,
                "last_requested": self.last_requested,
                "last_accepted": self.last_accepted,
                "last_applied": self.last_applied,
                "last_rejected": self.last_rejected,
            }
        if limit:
            hist = hist[-int(limit):]
        payload["history"] = hist
        payload["fill"] = len(hist)
        payload["limit"] = self._limit
        return payload


class BoundedList(object):
    def __init__(self, limit=60):
        self._lock = threading.Lock()
        self._items = []
        self._limit = int(limit)

    def append(self, item):
        with self._lock:
            self._items.append(item)
            if len(self._items) > self._limit:
                self._items = self._items[-self._limit:]

    def snapshot(self, limit=None):
        with self._lock:
            items = list(self._items)
        if limit:
            items = items[-int(limit):]
        return items

    def __len__(self):
        with self._lock:
            return len(self._items)


_ERROR_EVENTS = (
    EventType.COMMAND_REJECTED,
    EventType.LLM_UNAVAILABLE,
    EventType.WHISPER_UNAVAILABLE,
    EventType.EMERGENCY_STOP,
    EventType.PERCEPTION_STALE,
    EventType.NAVIGATION_RECOVERY_FAILED,
    EventType.TASK_FAILED,
)

_SOURCE_NODE = {
    "depth": "perception",
    "detector": "perception",
    "tracker": "perception",
    "perception": "perception",
    "nav": "planner",
    "safety": "safety",
    "tools": "tools",
    "llm": "llm",
    "runtime": "runtime",
    "camera": "camera",
    "task": "task",
    "whisper": "whisper",
    "web": "input",
    "monitor": "input",
    "ui": "input",
    "http": "input",
    "user": "input",
    "agent": "agent",
    "memory": "memory",
    "scan": "tools",
    "typesafe": "typesafe",
}


PIPELINE_EDGES = (
    ("input", "agent"),
    ("whisper", "agent"),
    ("agent", "typesafe"),
    ("agent", "llm"),
    ("agent", "tools"),
    ("agent", "task"),
    ("llm", "tools"),
    ("tools", "planner"),
    ("tools", "memory"),
    ("tools", "camera"),
    ("camera", "perception"),
    ("perception", "world"),
    ("world", "planner"),
    ("world", "safety"),
    ("world", "agent"),
    ("planner", "safety"),
    ("safety", "motors"),
    ("tools", "servo"),
)


def _node(node_id, label, group, status, detail="", extra=None, last_ts=None):
    item = {
        "id": node_id,
        "label": label,
        "group": group,
        "status": status,
        "detail": (detail or "")[:160],
        "last_ts": last_ts,
    }
    if extra:
        item["extra"] = extra
    return item


def _health_status(health, name, fallback="idle"):
    row = (health or {}).get(name) or {}
    if not row:
        return fallback, ""
    if row.get("online") is False:
        return "error", row.get("detail") or "offline"
    return "ok", row.get("detail") or ""


def _age(ts):
    if not ts:
        return None
    return round(time.time() - float(ts), 3)


class TelemetryHub(object):
    """Process-wide read-only telemetry. Cannot command motors."""

    def __init__(self, error_limit=60):
        self.motor = MotorLedger()
        self.errors = BoundedList(error_limit)
        self._lock = threading.Lock()
        self._last_by_node = {}
        self.started_at = time.time()

    def on_event(self, event):
        try:
            source = getattr(event, "source", None) or "unknown"
            node_id = _SOURCE_NODE.get(source, source)
            rec = {
                "ts": getattr(event, "timestamp", time.time()),
                "type": getattr(event, "type", None),
                "source": source,
                "corr_id": getattr(event, "corr_id", None),
            }
            with self._lock:
                self._last_by_node[node_id] = rec
            if getattr(event, "type", None) in _ERROR_EVENTS:
                payload = getattr(event, "payload", None) or {}
                reason = payload.get("reason") or payload.get("error") or event.type
                self.errors.append({
                    "ts": rec["ts"],
                    "type": event.type,
                    "source": source,
                    "message": str(reason)[:200],
                    "corr_id": rec["corr_id"],
                })
        except Exception:
            return

    def last_event(self, node_id):
        with self._lock:
            return self._last_by_node.get(node_id)

    def snapshot(self, runtime):
        """Assemble the three-view monitor payload. Never deepcopy WorldState."""
        health = {}
        try:
            health = runtime.health.snapshot()
        except Exception:
            health = {}
        host = (health.get("host_metrics") or health.get("host") or {})
        if not isinstance(host, dict):
            host = {}
        loops = self._loops(runtime)
        queues = self._queues(runtime)
        motor = self.motor.snapshot(limit=40)
        nodes, edges = self._pipeline(runtime, health, loops)
        tracer = getattr(runtime, "tracer", None)
        trace = {"seq": 0, "active": None, "runs": [], "last_asr": None, "last_llm": None, "last_tool": None}
        if tracer is not None:
            try:
                trace = tracer.snapshot(limit=24)
            except Exception:
                pass
        task = {}
        try:
            task = runtime.tasks.snapshot()
        except Exception:
            pass
        agent = getattr(runtime, "agent", None)
        planner = getattr(runtime, "planner", None)
        llm = getattr(runtime, "llm", None)
        ts = getattr(runtime, "typesafe", None)
        whisper = getattr(runtime, "whisper", None)
        safety = getattr(runtime, "safety", None)
        started = getattr(runtime, "started_at", self.started_at)
        estop = bool(getattr(safety, "estop", False)) if safety is not None else False
        allow = bool(getattr(safety, "allow_motion", False)) if safety is not None else False
        dashboard = {
            "host": {
                "cpu_pct": host.get("cpu_pct"),
                "load1": host.get("load1"),
                "mem_total_mb": host.get("mem_total_mb"),
                "mem_avail_mb": host.get("mem_avail_mb"),
                "mem_used_mb": None,
                "temp_c": host.get("temp_c"),
                "gpu_pct": host.get("gpu_pct"),
                "ts": host.get("ts"),
            },
            "loops": loops,
            "queues": queues,
            "errors": self.errors.snapshot(limit=20),
            "health": health,
            "motor": motor,
            "estop": estop,
            "allow_motion": allow,
            "task": task,
        }
        mem_total = host.get("mem_total_mb")
        mem_avail = host.get("mem_avail_mb")
        if mem_total is not None and mem_avail is not None:
            try:
                dashboard["host"]["mem_used_mb"] = round(float(mem_total) - float(mem_avail), 1)
            except Exception:
                pass
        models = {
            "llm": {
                "name": getattr(llm, "model", None) if llm is not None else None,
                "url": getattr(llm, "base_url", None) if llm is not None else None,
                "ok": getattr(llm, "ok", None) if llm is not None else None,
                "error": getattr(llm, "last_error", None) if llm is not None else None,
                "latency_s": getattr(llm, "last_latency_s", None) if llm is not None else None,
                "finish_reason": getattr(llm, "last_finish_reason", None) if llm is not None else None,
            },
            "typesafe": {
                "name": getattr(ts, "model", None) if ts is not None else None,
                "enabled": getattr(ts, "enabled", None) if ts is not None else None,
                "ok": getattr(ts, "ok", None) if ts is not None else None,
                "error": getattr(ts, "last_error", None) if ts is not None else None,
            },
            "whisper": {
                "url": getattr(whisper, "base_url", None) if whisper is not None else None,
                "ok": getattr(whisper, "ok", None) if whisper is not None else None,
                "error": getattr(whisper, "last_error", None) if whisper is not None else None,
                "last_text": getattr(whisper, "last_text", None) if whisper is not None else None,
            },
        }
        return {
            "uptime_s": time.time() - started,
            "estop": estop,
            "allow_motion": allow,
            "dashboard": dashboard,
            "pipeline": {"nodes": nodes, "edges": edges},
            "trace": trace,
            "agent": {
                "busy": bool(getattr(agent, "_busy", False)) if agent is not None else False,
                "pending": len(getattr(agent, "_pending", []) or []) if agent is not None else 0,
                "say": getattr(agent, "last_say", "") if agent is not None else "",
                "last_plan": getattr(agent, "last_plan", None) if agent is not None else None,
            },
            "task": task,
            "planner": {
                "mode": getattr(planner, "mode", "idle") if planner is not None else "idle",
                "recovery": bool(planner.in_recovery()) if planner is not None and hasattr(planner, "in_recovery") else False,
            },
            "models": models,
            "health": health,
            "live_updated_ts": getattr(getattr(runtime, "live", None), "updated_ts", 0.0),
        }

    def _loops(self, runtime):
        meters = []
        mapping = (
            ("safety", getattr(runtime, "safety", None)),
            ("planner", getattr(runtime, "planner", None)),
            ("agent", getattr(runtime, "agent", None)),
            ("perception", getattr(runtime, "perception", None)),
            ("camera", getattr(runtime, "camera", None)),
            ("telemetry", runtime),
        )
        for name, obj in mapping:
            meter = getattr(obj, "meter", None) if obj is not None else None
            if meter is not None and hasattr(meter, "snapshot"):
                try:
                    meters.append(meter.snapshot())
                    continue
                except Exception:
                    pass
            meters.append({
                "name": name, "target_hz": 0, "hz": 0, "last_period_ms": 0,
                "max_period_ms": 0, "overruns": 0, "ticks": 0, "last_ts": 0,
                "age_s": None, "stale": False,
            })
        return meters

    def _queues(self, runtime):
        agent = getattr(runtime, "agent", None)
        bus = getattr(runtime, "bus", None)
        tracer = getattr(runtime, "tracer", None)
        tools = getattr(runtime, "tools", None)
        pending = 0
        busy = False
        if agent is not None:
            pending = len(getattr(agent, "_pending", []) or [])
            busy = bool(getattr(agent, "_busy", False))
        hist = 0
        hist_limit = 0
        if bus is not None:
            hist_limit = int(getattr(bus, "_history_limit", 0) or 0)
            try:
                with bus._lock:
                    hist = len(bus._history)
            except Exception:
                hist = 0
        runs = 0
        run_limit = 0
        if tracer is not None:
            run_limit = int(getattr(tracer, "_max_runs", 0) or 0)
            try:
                with tracer._lock:
                    runs = len(tracer._runs)
            except Exception:
                runs = 0
        scanning = bool(getattr(tools, "_scanning", False)) if tools is not None else False
        return {
            "agent_pending": pending,
            "agent_busy": busy,
            "event_history": hist,
            "event_history_limit": hist_limit,
            "trace_runs": runs,
            "trace_run_limit": run_limit,
            "scan_running": scanning,
            "error_buffer": len(self.errors),
        }

    def _pipeline(self, runtime, health, loops):
        loop_by = dict((row.get("name"), row) for row in loops or [])
        safety = getattr(runtime, "safety", None)
        planner = getattr(runtime, "planner", None)
        agent = getattr(runtime, "agent", None)
        tools = getattr(runtime, "tools", None)
        llm = getattr(runtime, "llm", None)
        ts = getattr(runtime, "typesafe", None)
        whisper = getattr(runtime, "whisper", None)
        camera = getattr(runtime, "camera", None)
        motors = getattr(runtime, "motors", None)
        servo = getattr(runtime, "servo", None)
        world = getattr(runtime, "world", None)
        perception = getattr(runtime, "perception", None)
        tasks = getattr(runtime, "tasks", None)
        memory = getattr(runtime, "memory", None)
        tracer = getattr(runtime, "tracer", None)

        cam_snap = {}
        mot_snap = {}
        servo_snap = {}
        obs_view = {}
        perc = {}
        try:
            if camera is not None:
                cam_snap = camera.snapshot() or {}
        except Exception:
            cam_snap = {}
        try:
            if motors is not None:
                mot_snap = motors.snapshot() or {}
        except Exception:
            mot_snap = {}
        try:
            if servo is not None:
                servo_snap = servo.snapshot() or {}
        except Exception:
            servo_snap = {}
        try:
            if world is not None and hasattr(world, "obstacles_view"):
                obs_view = world.obstacles_view() or {}
            if world is not None and hasattr(world, "perception_view"):
                perc = world.perception_view() or {}
        except Exception:
            obs_view = {}
            perc = {}

        estop = bool(getattr(safety, "estop", False)) if safety is not None else False
        allow = bool(getattr(safety, "allow_motion", False)) if safety is not None else False
        last_reject = getattr(safety, "_last_reject", "") if safety is not None else ""
        planner_mode = getattr(planner, "mode", "idle") if planner is not None else "idle"
        recovery = False
        if planner is not None and hasattr(planner, "in_recovery"):
            try:
                recovery = bool(planner.in_recovery())
            except Exception:
                recovery = False
        agent_busy = bool(getattr(agent, "_busy", False)) if agent is not None else False
        pending = len(getattr(agent, "_pending", []) or []) if agent is not None else 0
        task_state = "IDLE"
        if tasks is not None:
            task_state = getattr(tasks, "state", "IDLE") or "IDLE"

        def from_loop(name, ok_status="running"):
            row = loop_by.get(name) or {}
            if row.get("stale"):
                return "stale", "age %.2fs" % (row.get("age_s") or 0)
            if row.get("ticks"):
                return ok_status, "%.1f Hz" % (row.get("hz") or 0)
            return "idle", "no ticks"

        nodes = []

        st, detail = from_loop("camera")
        if cam_snap.get("ok") is False:
            st, detail = "error", cam_snap.get("error") or cam_snap.get("backend") or "camera down"
        elif cam_snap.get("ok"):
            if st != "stale":
                st = "running"
            detail = "%s · %.1f fps" % (cam_snap.get("backend") or "cam", cam_snap.get("fps") or 0)
        nodes.append(_node("camera", "Camera", "sense", st, detail, extra=cam_snap, last_ts=(loop_by.get("camera") or {}).get("last_ts")))

        st, detail = from_loop("perception")
        if perc:
            detail = "depth %.1f · det %.1f" % (perc.get("depth_fps") or 0, perc.get("detect_fps") or 0)
        nodes.append(_node("perception", "Perception", "sense", st, detail, extra={
            "fps": perc.get("fps"), "depth_fps": perc.get("depth_fps"),
            "detect_fps": perc.get("detect_fps"), "backend": perc.get("backend"),
            "latency_ms": perc.get("latency_ms"),
        }, last_ts=perc.get("last_depth_ts") or perc.get("last_frame_ts")))

        obs = obs_view.get("obstacles") or {}
        fresh = (obs_view.get("freshness") or {}).get("obstacles")
        wstatus = "ok"
        wdetail = "front=%s" % (obs.get("front_m") if obs.get("front_m") is not None else "?")
        if fresh == "STALE":
            wstatus, wdetail = "stale", "depth stale"
        elif obs.get("blocked"):
            wstatus, wdetail = "warn", "blocked %.2fm" % (obs.get("front_m") or 0)
        nodes.append(_node("world", "WorldState", "state", wstatus, wdetail, extra={
            "blocked": obs.get("blocked"), "front_m": obs.get("front_m"),
            "freshness": fresh, "direction": obs.get("direction"),
        }, last_ts=obs.get("last_update")))

        st, detail = from_loop("planner")
        if recovery:
            st, detail = "warn", "recovery"
        elif planner_mode and planner_mode not in ("idle", "stop"):
            st, detail = "running", "mode " + str(planner_mode)
        nodes.append(_node("planner", "Planner", "act", st, detail, extra={
            "mode": planner_mode, "recovery": recovery,
        }, last_ts=(loop_by.get("planner") or {}).get("last_ts")))

        st, detail = from_loop("safety")
        if estop:
            st, detail = "error", "ESTOP"
        elif not allow:
            st, detail = "locked", "wheels locked"
        elif last_reject:
            detail = "last reject: %s" % last_reject
        nodes.append(_node("safety", "Safety", "act", st, detail, extra={
            "estop": estop, "allow_motion": allow, "last_reject": last_reject,
        }, last_ts=(loop_by.get("safety") or {}).get("last_ts")))

        applied = self.motor.last_applied
        mstatus = "idle"
        mdetail = mot_snap.get("backend") or "motors"
        if estop:
            mstatus, mdetail = "error", "estop"
        elif not allow:
            mstatus, mdetail = "locked", "locked"
        elif applied and (abs(applied.get("left") or 0) > 1e-4 or abs(applied.get("right") or 0) > 1e-4):
            mstatus = "running"
            mdetail = "L %.2f R %.2f" % (applied.get("left") or 0, applied.get("right") or 0)
        elif self.motor.last_rejected and (
            not applied or (self.motor.last_rejected.get("ts") or 0) > (applied.get("ts") or 0)
        ):
            mstatus = "warn"
            mdetail = "rejected %s" % (self.motor.last_rejected.get("reason") or "")
        nodes.append(_node("motors", "Motors", "act", mstatus, mdetail, extra=mot_snap, last_ts=(applied or {}).get("ts") or mot_snap.get("last_command")))

        enabled = bool(servo_snap.get("enabled"))
        if not enabled:
            sstatus, sdetail = "disabled", servo_snap.get("backend") or "fixed"
        else:
            sstatus, sdetail = "ok", "pan %.0f tilt %.0f" % (servo_snap.get("pan") or 0, servo_snap.get("tilt") or 0)
        nodes.append(_node("servo", "Servo", "act", sstatus, sdetail, extra=servo_snap))

        st, detail = from_loop("agent", "idle")
        if agent_busy:
            st, detail = "running", "busy"
        elif pending:
            st, detail = "waiting", "queued %d" % pending
        nodes.append(_node("agent", "Agent", "think", st, detail, extra={
            "busy": agent_busy, "pending": pending,
            "say": (getattr(agent, "last_say", "") or "")[:120] if agent is not None else "",
        }, last_ts=(loop_by.get("agent") or {}).get("last_ts")))

        hst, hdet = _health_status(health, "llm", "idle")
        if llm is not None and getattr(llm, "last_error", None) and not getattr(llm, "ok", True):
            hst, hdet = "error", str(llm.last_error)[:80]
        elif agent_busy and getattr(llm, "last_latency_s", None):
            hst = "running"
            hdet = "%.2fs" % float(llm.last_latency_s or 0)
        nodes.append(_node("llm", "LLM", "think", hst, hdet, extra={
            "model": getattr(llm, "model", None) if llm is not None else None,
            "ok": getattr(llm, "ok", None) if llm is not None else None,
            "latency_s": getattr(llm, "last_latency_s", None) if llm is not None else None,
        }))

        ts_on = bool(getattr(ts, "enabled", False)) if ts is not None else False
        if not ts_on:
            tstatus, tdetail = "disabled", "off"
        else:
            tstatus, tdetail = _health_status(health, "typesafe", "idle")
        nodes.append(_node("typesafe", "TypeSafe", "think", tstatus, tdetail, extra={
            "enabled": ts_on,
            "ok": getattr(ts, "ok", None) if ts is not None else None,
        }))

        wst, wdet = _health_status(health, "whisper", "idle")
        nodes.append(_node("whisper", "Whisper", "sense", wst, wdet, extra={
            "ok": getattr(whisper, "ok", None) if whisper is not None else None,
            "last_text": (getattr(whisper, "last_text", None) or "")[:80] if whisper is not None else "",
        }))

        scanning = bool(getattr(tools, "_scanning", False)) if tools is not None else False
        last_tool = None
        if tracer is not None:
            last_tool = getattr(tracer, "last_tool", None)
        tstatus = "running" if scanning else "idle"
        tdetail = "scan" if scanning else "idle"
        if last_tool:
            tdetail = str(last_tool.get("name") or "tool")
            if last_tool.get("ok") is False:
                tstatus = "warn"
                tdetail += " rejected"
            elif last_tool.get("ok"):
                tstatus = "ok"
        nodes.append(_node("tools", "Tools", "think", tstatus, tdetail, extra={
            "scanning": scanning,
            "last": last_tool,
        }, last_ts=(last_tool or {}).get("ts")))

        tstatus = "idle"
        tdetail = task_state
        if task_state in ("FAILED", "CANCELLED"):
            tstatus = "error"
        elif task_state in ("EXECUTING", "PLANNING", "REPLANNING", "ACTIVE_PERCEPTION"):
            tstatus = "running"
        nodes.append(_node("task", "Task", "state", tstatus, tdetail, extra={
            "state": task_state,
        }))

        notes = 0
        try:
            if memory is not None:
                notes = len(getattr(memory, "notes", []) or [])
        except Exception:
            notes = 0
        mst, mdet = _health_status(health, "memory", "ok")
        nodes.append(_node("memory", "Memory", "state", mst, mdet or ("notes %d" % notes), extra={"notes": notes}))

        istatus = "ok" if pending or agent_busy else "idle"
        nodes.append(_node("input", "Input", "sense", istatus, "web / monitor / voice"))

        last_map = {}
        with self._lock:
            last_map = dict(self._last_by_node)
        for node in nodes:
            ev = last_map.get(node["id"])
            if ev:
                node["last_event"] = ev
                if not node.get("last_ts"):
                    node["last_ts"] = ev.get("ts")

        edges = []
        for src, dst in PIPELINE_EDGES:
            # Structural links only. Do not mark an edge "live" without a real event.
            edges.append({"from": src, "to": dst})
        return nodes, edges
