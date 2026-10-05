"""Compose subsystems and own process lifetime."""

from __future__ import print_function

import logging
import os
import signal
import threading
import time

from .agent import Agent
from .config import AttrDict, load_config
from .events import EventBus, EventType
from .hardware.camera import LatestFrameBuffer, try_create_camera
from .hardware.motors import try_create_motors
from .hardware.servo import try_create_servo
from .health import HealthMonitor, host_telemetry
from .llm import LLMClient
from .memory import SemanticMemory
from .navigation import LocalPlanner
from .perception.pipeline import PerceptionPipeline
from .safety import MotionCommand, SafetyController
from .spatial import SpatialMemory
from .task import TaskManager
from .tools import ToolRouter
from .typesafe_client import TypeSafeClient
from .whisper_client import WhisperClient
from .world import WorldState

log = logging.getLogger("jetbot.runtime")


class JetBotRuntime(object):
    def __init__(self, cfg):
        if not isinstance(cfg, dict):
            cfg = dict(cfg)
        self.cfg = cfg
        self.bus = EventBus(history=250)
        stale = float(cfg.get("safety", {}).get("stale_perception_s", 0.9))
        stop_m = float(cfg.get("safety", {}).get("min_obstacle_m", 0.18))
        self.world = WorldState(stale_s=stale, stop_m=stop_m)
        self.health = HealthMonitor(self.world)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        mem_path = cfg.get("memory", {}).get("path", "data/amem.json")
        if not os.path.isabs(mem_path):
            mem_path = os.path.join(root, mem_path)
        self.memory = SemanticMemory(
            mem_path,
            max_notes=int(cfg.get("memory", {}).get("max_notes", 400)),
            retrieve_k=int(cfg.get("memory", {}).get("retrieve_k", 5)),
            bus=self.bus,
        )
        self.spatial = SpatialMemory()
        self.motors, motors_backend = try_create_motors(cfg)
        self.servo, servo_backend = try_create_servo(cfg)
        self.overlay_buffer = LatestFrameBuffer()
        self.camera, self.frame_buffer, camera_backend = try_create_camera(cfg)
        self.world.update_robot(hardware={
            "motors": motors_backend,
            "servo": servo_backend,
            "camera": camera_backend,
        })
        self.safety = SafetyController(cfg, self.motors, self.world, self.bus)
        self.planner = LocalPlanner(cfg, self.world, self.safety, self.bus)
        self.perception = PerceptionPipeline(
            cfg, self.frame_buffer, self.world, self.bus, self.overlay_buffer,
        )
        self.tasks = TaskManager(self.world, self.bus)
        self.llm = LLMClient(cfg)
        self.whisper = WhisperClient(cfg)
        self.typesafe = TypeSafeClient(cfg)
        self.tools = ToolRouter(
            self.world, self.safety, self.planner, self.servo,
            self.memory, self.tasks, self.bus,
        )
        self.agent = Agent(
            cfg, self.world, self.bus, self.llm, self.tools,
            self.tasks, self.memory, self.typesafe, self.health,
            safety=self.safety, planner=self.planner, spatial=self.spatial,
        )
        self._stop = threading.Event()
        self._telem = None
        self.started_at = time.time()
        self._bind_health()

    def _bind_health(self):
        self.health.set("motors", True, self.world.robot["hardware"]["motors"])
        self.health.set("servo", True, self.world.robot["hardware"]["servo"])
        self.health.set("camera", True, self.world.robot["hardware"]["camera"])
        self.health.set("safety", True, "armed")
        self.health.set("perception", True, "starting")
        ok, detail = self.llm.health()
        self.health.set("llm", ok, detail)
        ok, detail = self.whisper.health()
        self.health.set("whisper", ok, detail)
        ts_on, ts_detail = self.typesafe.health()
        self.health.set("typesafe", ts_on, ts_detail)
        self.health.set("memory", True, "notes=%d" % len(self.memory.notes))

    def start(self):
        log.info("Starting JetBot runtime")
        self.safety.start()
        self.perception.start()
        self.planner.start()
        self.agent.start()
        self._telem = threading.Thread(target=self._telemetry_loop, name="telemetry")
        self._telem.daemon = True
        self._telem.start()
        self.health.set("perception", True, "running")
        self.bus.emit(EventType.HEALTH, "runtime", {"status": "started"})

    def stop(self):
        log.info("Stopping JetBot runtime")
        self._stop.set()
        try:
            self.safety.emergency_stop(source="shutdown")
        except Exception:
            pass
        for part in (self.agent, self.planner, self.perception, self.safety, self.camera):
            try:
                part.stop()
            except Exception:
                log.exception("stop failed for %s", part)
        try:
            self.motors.close()
        except Exception:
            pass
        try:
            self.servo.close()
        except Exception:
            pass

    def _telemetry_loop(self):
        hz = float(self.cfg.get("loops", {}).get("telemetry_hz", 2))
        period = 1.0 / max(0.5, hz)
        while not self._stop.is_set():
            host = host_telemetry()
            self.health.set("host", True, "ok", extra=host)
            servo = self.servo.snapshot()
            if servo.get("enabled"):
                self.world.update_robot(camera_pan=servo.get("pan", 0), camera_tilt=servo.get("tilt", 0))
            else:
                self.world.update_robot(camera_pan=0.0, camera_tilt=0.0)
            cam = self.camera.snapshot()
            self.health.set("camera", bool(cam.get("ok")), cam.get("backend", ""), extra={"fps": cam.get("fps")})
            time.sleep(period)

    def status_payload(self):
        snap = self.world.snapshot()
        cam = self.camera.snapshot()
        mot = self.motors.snapshot()
        servo = self.servo.snapshot()
        return {
            "uptime_s": time.time() - self.started_at,
            "robot": snap["robot"],
            "obstacles": snap["obstacles"],
            "objects": snap["objects"],
            "navigation": snap["navigation"],
            "task": self.tasks.snapshot(),
            "perception": snap["perception"],
            "freshness": snap["freshness"],
            "uncertainty": snap["uncertainty"],
            "health": self.health.snapshot(),
            "camera": cam,
            "motors": mot,
            "servo": servo,
            "agent": self.agent.snapshot(),
            "memory": self.memory.snapshot(),
            "estop": self.safety.estop,
            "allow_motion": bool(self.safety.allow_motion),
        }

    def handle_manual(self, action, extra=None):
        extra = extra or {}
        action = str(action or "").lower()
        if action in ("estop", "emergency_stop", "stop_now"):
            self.safety.emergency_stop(source="ui")
            return {"ok": True, "action": "estop"}
        if action in ("clear_estop", "reset_estop"):
            self.safety.clear_estop(source="ui")
            return {"ok": True, "action": "clear_estop"}
        if action in ("unlock", "allow_motion", "motion_on"):
            return self.set_allow_motion(True, source="ui")
        if action in ("lock", "deny_motion", "motion_off"):
            return self.set_allow_motion(False, source="ui")
        if action == "stop":
            self.planner.halt()
            self.safety.request(MotionCommand(0, 0, "ui-stop", 0.1))
            return {"ok": True, "action": "stop"}
        mapping = {
            "forward": "motion.forward",
            "backward": "motion.backward",
            "left": "motion.left",
            "right": "motion.right",
            "explore": "motion.explore",
            "scan": "camera.scan_environment",
            "look_forward": "camera.look_forward",
            "look_left": "camera.look_left",
            "look_right": "camera.look_right",
            "look_up": "camera.look_up",
            "look_down": "camera.look_down",
        }
        if action in mapping:
            return self.tools.dispatch(mapping[action], extra)
        return {"ok": False, "error": "unknown action %s" % action}

    def set_allow_motion(self, allowed, source="ui"):
        allowed = bool(allowed)
        self.cfg.setdefault("robot", {})["allow_motion"] = allowed
        if not allowed:
            try:
                self.planner.halt()
            except Exception:
                pass
        self.safety.set_allow_motion(allowed, source=source)
        return {"ok": True, "allow_motion": allowed, "action": "unlock" if allowed else "lock"}


def create_runtime(config_path=None):
    cfg = load_config(config_path)
    return JetBotRuntime(cfg)


def install_signal_handlers(runtime):
    def _handler(signum, frame):
        runtime.stop()
        os._exit(0)
    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)
