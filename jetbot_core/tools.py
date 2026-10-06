"""Semantic tools the Agent may call. All motion goes through Safety."""

from __future__ import print_function

import logging
import threading
import time

from .events import EventType
from .safety import MotionCommand
from .task import COMPLETED, FAILED

log = logging.getLogger("jetbot.tools")

# Body-yaw survey for a fixed chassis camera. Each tuple is (planner_mode, dwell_s).
SCAN_BODY_STEPS = [
    ("stop", 0.25),
    ("left", 0.45),
    ("stop", 0.35),
    ("right", 0.90),
    ("stop", 0.35),
    ("left", 0.45),
    ("stop", 0.30),
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
        self.servo_enabled = bool(getattr(servo, "enabled", False))

    def dispatch(self, name, args=None):
        args = args or {}
        name = str(name).strip()
        if self.world.obstacles_view()["robot"]["estop"] and name not in ("motion.stop", "task.fail", "task.complete"):
            return {"ok": False, "tool": name, "error": "estop"}
        if name.startswith("motion.") and name != "motion.stop" and self.planner.in_recovery():
            return {"ok": False, "tool": name, "error": "recovery_active"}
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

    def _named_task(self):
        snap = self.tasks.snapshot()
        return bool(snap.get("id")) and snap.get("state") not in ("IDLE", None)

    def _note(self, action, progress=None):
        if self._named_task():
            self.tasks.ensure_executing(action=action, progress=progress)
        else:
            self.tasks.note_action(action, progress=progress)

    def _forward(self, args):
        d = self._duration(args, 1.0)
        self.planner.set_mode("forward", duration_s=d)
        self._note("forward", progress=0.4)
        return {"ok": True, "duration": d}

    def _backward(self, args):
        d = self._duration(args, 0.6)
        self.planner.set_mode("backup", duration_s=d)
        self._note("backup")
        return {"ok": True, "duration": d}

    def _left(self, args):
        d = self._duration(args, 0.5)
        self.planner.set_mode("left", duration_s=d)
        self._note("left")
        return {"ok": True, "duration": d}

    def _right(self, args):
        d = self._duration(args, 0.5)
        self.planner.set_mode("right", duration_s=d)
        self._note("right")
        return {"ok": True, "duration": d}

    def _stop(self, args):
        self.planner.halt()
        self.safety.request(MotionCommand(0, 0, "tool-stop", 0.1))
        self.tasks.note_action("stop")
        return {"ok": True}

    def _explore(self, args):
        d = self._duration(args, 3.0)
        self.planner.set_mode("explore", duration_s=d)
        self._note("explore")
        return {"ok": True, "duration": d}

    def _look(self, pose):
        self.planner.halt()
        if self.servo_enabled:
            pan, tilt = self.servo.look_named(pose)
            self.world.update_robot(camera_pan=pan, camera_tilt=tilt)
            return {"ok": True, "pan": pan, "tilt": tilt, "camera": "gimbal"}
        # Fixed camera: left/right become a short body yaw. Up/down/forward are no-ops.
        if pose in ("left", "right"):
            self.planner.set_mode(pose, duration_s=0.4)
            self._note("look_" + pose)
            return {"ok": True, "camera": "fixed", "body_yaw": pose, "pan": 0.0, "tilt": 0.0}
        self.world.update_robot(camera_pan=0.0, camera_tilt=0.0)
        return {"ok": True, "camera": "fixed", "note": "no gimbal", "pan": 0.0, "tilt": 0.0}

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
        return {"ok": True, "started": True, "camera": "fixed" if not self.servo_enabled else "gimbal"}

    def _scan_worker(self):
        self.bus.emit(EventType.SCAN_REQUESTED, "camera", {"fixed": not self.servo_enabled})
        self.planner.halt()
        seen = []
        try:
            if self.world.snapshot()["robot"]["estop"]:
                return
            allow = True
            try:
                allow = bool(self.safety.allow_motion)
            except Exception:
                allow = True
            if self.servo_enabled:
                self._scan_gimbal(seen)
            elif allow:
                self._scan_body(seen)
            else:
                time.sleep(0.4)
                self._collect_seen(seen)
            self.world.update_robot(camera_pan=0.0, camera_tilt=0.0)
            content = "Room scan: %s" % (", ".join(seen[:12]) if seen else "no distinct objects")
            self.memory.add_note(
                content, reason="scan_complete", tags=["scan"],
                context="fixed camera survey" if not self.servo_enabled else "gimbal scan",
                source="scan", confidence=0.55,
            )
            self.bus.emit(EventType.SCAN_COMPLETED, "camera", {"objects": seen[:12], "fixed": not self.servo_enabled})
        finally:
            self.planner.halt()
            self._scanning = False

    def _scan_gimbal(self, seen):
        from .hardware.servo import LOOK_PRESETS
        poses = ("forward", "left", "left_up", "forward", "right", "right_up", "forward")
        dwells = (0.35, 0.45, 0.35, 0.25, 0.45, 0.35, 0.4)
        for pose, dwell in zip(poses, dwells):
            if self.world.snapshot()["robot"]["estop"]:
                break
            if pose in LOOK_PRESETS:
                pan, tilt = self.servo.look_named(pose)
                self.world.update_robot(camera_pan=pan, camera_tilt=tilt)
            time.sleep(dwell)
            self._collect_seen(seen)
        self.servo.look_named("forward")

    def _scan_body(self, seen):
        for mode, dwell in SCAN_BODY_STEPS:
            if self.world.snapshot()["robot"]["estop"]:
                break
            if self.planner.in_recovery() and mode in ("left", "right"):
                self.planner.halt()
                time.sleep(dwell)
                self._collect_seen(seen)
                continue
            self.planner.set_mode(mode, duration_s=dwell)
            time.sleep(dwell)
            self._collect_seen(seen)
        self.planner.halt()

    def _collect_seen(self, seen):
        snap = self.world.snapshot()
        for obj in snap.get("objects", {}).values():
            bit = "%s:%s" % (obj.get("id"), obj.get("class"))
            if bit not in seen:
                seen.append(bit)

    def _inspect(self, args):
        target = args.get("target") or args.get("object")
        self.planner.halt()
        snap = self.world.snapshot()
        obj = (snap.get("objects") or {}).get(target) if target else None
        if self.servo_enabled and obj:
            x0, y0, x1, y1 = obj["bbox"]
            cx = (x0 + x1) * 0.5
            cy = (y0 + y1) * 0.5
            pan = (0.5 - cx) * 70.0
            tilt = (0.5 - cy) * 30.0
            self.servo.look(pan, tilt)
            self.world.update_robot(camera_pan=pan, camera_tilt=tilt)
            return {"ok": True, "target": target, "camera": "gimbal"}
        if obj:
            x0, y0, x1, y1 = obj["bbox"]
            cx = (x0 + x1) * 0.5
            if cx < 0.4:
                self.planner.set_mode("left", duration_s=0.35)
            elif cx > 0.6:
                self.planner.set_mode("right", duration_s=0.35)
            self._note("inspect")
            return {"ok": True, "target": target, "camera": "fixed", "body_yaw": True}
        return {"ok": True, "target": target, "camera": "fixed", "note": "no gimbal"}

    def _go_to(self, args):
        target = args.get("target") or args.get("name")
        objs = self.world.objects_view()
        if target and target in objs:
            self.planner.set_mode("follow", duration_s=4.0, target=target)
            self.world.set_navigation(current_target=target, mode="follow")
            self._note("follow")
            return {"ok": True, "target": target, "mode": "follow"}
        self.planner.halt()
        self._scan({})
        return {"ok": False, "target": target, "error": "unknown_target"}

    def _follow(self, args):
        target = args.get("target")
        self.planner.set_mode("follow", duration_s=4.0, target=target)
        self.world.set_navigation(current_target=target, mode="follow")
        self._note("follow")
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
