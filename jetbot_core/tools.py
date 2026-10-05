"""Semantic tools the Agent may call. All motion goes through Safety."""

from __future__ import print_function

import logging
import threading
import time

from .events import EventType
from .safety import MotionCommand
from .task import ACTIVE_PERCEPTION, COMPLETED, EXECUTING, FAILED, OBSERVING

log = logging.getLogger("jetbot.tools")

SCAN_POSES = [
    ("forward", 0.35),
    ("left", 0.45),
    ("left_up", 0.35),
    ("forward", 0.25),
    ("right", 0.45),
    ("right_up", 0.35),
    ("forward", 0.4),
]


class ToolRouter(object):
    def __init__(self, world, safety, planner, servo, memory, tasks, bus):
        self.world = world
        self.safety = safety
        self.planner = planner
        self.servo = servo
        self.memory = memory
        self.tasks = tasks
        self.bus = bus
        self._scan_lock = threading.Lock()
        self._scanning = False
        self.last_say = ""

    def dispatch(self, name, args=None):
        args = args or {}
        name = str(name).strip()
        handler = {
            "motion.forward": self._forward,
            "motion.backward": self._backward,
            "motion.left": self._left,
            "motion.right": self._right,
            "motion.stop": self._stop,
            "motion.explore": self._explore,
            "camera.look_forward": lambda a: self._look("forward"),
            "camera.look_left": lambda a: self._look("left"),
            "camera.look_right": lambda a: self._look("right"),
            "camera.look_up": lambda a: self._look("up"),
            "camera.look_down": lambda a: self._look("down"),
            "camera.scan_environment": self._scan,
            "camera.inspect": self._inspect,
            "nav.go_to": self._go_to,
            "nav.follow": self._follow,
            "memory.remember": self._remember,
            "task.complete": self._complete,
            "task.fail": self._fail,
        }.get(name)
        if handler is None:
            return {"ok": False, "error": "unknown tool %s" % name}
        try:
            result = handler(args)
            if result is None:
                result = {"ok": True, "tool": name}
            elif isinstance(result, dict) and "ok" not in result:
                result["ok"] = True
            result["tool"] = name
            return result
        except Exception as exc:
            log.exception("tool %s failed", name)
            return {"ok": False, "tool": name, "error": str(exc)}

    def _duration(self, args, default=1.0):
        try:
            d = float(args.get("duration", default))
        except Exception:
            d = default
        if d < 0.15:
            d = 0.15
        if d > 4.0:
            d = 4.0
        return d

    def _forward(self, args):
        d = self._duration(args, 1.0)
        self.planner.set_mode("forward", duration_s=d)
        self.tasks.ensure_executing(action="forward", progress=0.4)
        return {"ok": True, "duration": d}

    def _backward(self, args):
        d = self._duration(args, 0.6)
        self.planner.set_mode("backup", duration_s=d)
        self.tasks.ensure_executing(action="backup")
        return {"ok": True, "duration": d}

    def _left(self, args):
        d = self._duration(args, 0.5)
        self.planner.set_mode("left", duration_s=d)
        self.tasks.ensure_executing(action="left")
        return {"ok": True, "duration": d}

    def _right(self, args):
        d = self._duration(args, 0.5)
        self.planner.set_mode("right", duration_s=d)
        self.tasks.ensure_executing(action="right")
        return {"ok": True, "duration": d}

    def _stop(self, args):
        self.planner.halt()
        self.safety.request(MotionCommand(0, 0, "tool-stop", 0.1))
        return {"ok": True}

    def _explore(self, args):
        d = self._duration(args, 3.0)
        self.planner.set_mode("explore", duration_s=d)
        self.tasks.ensure_executing(action="explore")
        return {"ok": True, "duration": d}

    def _look(self, pose):
        pan, tilt = self.servo.look_named(pose)
        self.world.update_robot(camera_pan=pan, camera_tilt=tilt)
        try:
            self.tasks.transition(ACTIVE_PERCEPTION, action="look_" + pose)
        except ValueError:
            pass
        return {"ok": True, "pan": pan, "tilt": tilt}

    def _scan(self, args):
        if not self._scan_lock.acquire(False):
            return {"ok": False, "error": "scan already running"}
        try:
            if self._scanning:
                return {"ok": False, "error": "scan already running"}
            self._scanning = True
        finally:
            self._scan_lock.release()
        thread = threading.Thread(target=self._scan_worker, name="scan")
        thread.daemon = True
        thread.start()
        return {"ok": True, "started": True}

    def _scan_worker(self):
        self.bus.emit(EventType.SCAN_REQUESTED, "camera", {})
        try:
            self.tasks.transition(ACTIVE_PERCEPTION, action="scan")
        except ValueError:
            pass
        seen = []
        try:
            for pose, dwell in SCAN_POSES:
                if self.world.snapshot()["robot"]["estop"]:
                    break
                pan, tilt = self.servo.look_named(pose)
                self.world.update_robot(camera_pan=pan, camera_tilt=tilt)
                time.sleep(dwell)
                snap = self.world.snapshot()
                for obj in snap.get("objects", {}).values():
                    seen.append("%s:%s" % (obj.get("id"), obj.get("class")))
            self.servo.look_named("forward")
            self.world.update_robot(camera_pan=0.0, camera_tilt=0.0)
            content = "Room scan: %s" % (", ".join(seen[:12]) if seen else "no distinct objects")
            self.memory.add_note(
                content, reason="scan_complete", tags=["scan"],
                context="initial or active perception scan", source="scan", confidence=0.55,
            )
            self.bus.emit(EventType.SCAN_COMPLETED, "camera", {"objects": seen[:12]})
            try:
                self.tasks.transition(OBSERVING, action="scan_done", progress=0.3)
            except ValueError:
                pass
        finally:
            self._scanning = False

    def _inspect(self, args):
        target = args.get("target") or args.get("object")
        # Look slightly toward the object's last bbox center if known.
        snap = self.world.snapshot()
        obj = (snap.get("objects") or {}).get(target) if target else None
        if obj:
            x0, y0, x1, y1 = obj["bbox"]
            cx = (x0 + x1) * 0.5
            cy = (y0 + y1) * 0.5
            pan = (0.5 - cx) * 70.0
            tilt = (0.5 - cy) * 30.0
            self.servo.look(pan, tilt)
            self.world.update_robot(camera_pan=pan, camera_tilt=tilt)
        else:
            self._look("forward")
        try:
            self.tasks.transition(ACTIVE_PERCEPTION, action="inspect")
        except ValueError:
            pass
        return {"ok": True, "target": target}

    def _go_to(self, args):
        target = args.get("target") or args.get("name")
        self.planner.set_mode("explore", duration_s=2.5, target=target)
        self.world.set_navigation(current_target=target, mode="explore")
        self.tasks.ensure_executing(action="go_to")
        return {"ok": True, "target": target, "note": "local explore toward target; no metric map"}

    def _follow(self, args):
        target = args.get("target")
        self.planner.set_mode("follow", duration_s=4.0, target=target)
        self.world.set_navigation(current_target=target, mode="follow")
        self.tasks.ensure_executing(action="follow")
        return {"ok": True, "target": target}

    def _remember(self, args):
        content = args.get("content") or args.get("text") or ""
        reason = args.get("reason") or "user_note"
        nid = self.memory.add_note(content, reason=reason, source="agent", confidence=0.7)
        return {"ok": True, "id": nid}

    def _complete(self, args):
        try:
            self.tasks.transition(COMPLETED, action="done", progress=1.0)
        except ValueError:
            pass
        self.planner.halt()
        return {"ok": True}

    def _fail(self, args):
        reason = args.get("reason") or "failed"
        try:
            self.tasks.transition(FAILED, action="fail", failure=reason)
        except ValueError:
            pass
        self.planner.halt()
        return {"ok": True, "reason": reason}
