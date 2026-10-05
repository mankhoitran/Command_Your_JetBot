#!/usr/bin/env python3
"""Unit tests - no hardware, no network required."""

from __future__ import print_function

import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from jetbot_core.config import load_config
from jetbot_core.events import Event, EventBus, EventType
from jetbot_core.llm import parse_plan
from jetbot_core.agent import Agent, _needs_llm
from jetbot_core.memory import SemanticMemory
from jetbot_core.navigation import LocalPlanner
from jetbot_core.safety import MotionCommand, SafetyController
from jetbot_core.task import COMPLETED, EXECUTING, FAILED, IDLE, PLANNING, REPLANNING, TaskManager
from jetbot_core.world import STALE, TrackedObject, WorldState
from jetbot_core.perception.track import IoUTracker
from jetbot_core.perception.detect import Detection
from jetbot_core.hardware.motors import JetbotLibController, SimulatedMotors
from jetbot_core.hardware.camera import LatestFrameBuffer
from jetbot_core.hardware.servo import FixedServo, try_create_servo
from jetbot_core.tools import ToolRouter
import numpy as np


class DummyBus(EventBus):
    pass


class WorldTests(unittest.TestCase):
    def test_upsert_and_stale(self):
        w = WorldState(stale_s=0.05)
        obj = TrackedObject("obj_01", "chair", [0.1, 0.1, 0.4, 0.5], 0.9)
        self.assertTrue(w.upsert_object(obj))
        self.assertFalse(w.upsert_object(obj))
        snap = w.snapshot()
        self.assertEqual(snap["objects"]["obj_01"]["class"], "chair")
        time.sleep(0.06)
        self.assertFalse(w.is_perception_fresh())
        w.perception["last_depth_ts"] = time.time()
        self.assertTrue(w.is_perception_fresh())

    def test_llm_summary_has_no_image(self):
        w = WorldState()
        text = w.summary_for_llm()
        self.assertIn("task:", text)
        self.assertNotIn("ndarray", text)


class EventTests(unittest.TestCase):
    def test_dispatch_and_history(self):
        bus = EventBus(history=5)
        seen = []
        bus.subscribe(EventType.OBSTACLE_DETECTED, lambda e: seen.append(e.type))
        bus.emit(EventType.OBSTACLE_DETECTED, "depth", {"distance": 0.3}, 0.9)
        self.assertEqual(seen, [EventType.OBSTACLE_DETECTED])
        rec = bus.recent(limit=2)
        self.assertEqual(rec[0]["source"], "depth")
        for i in range(8):
            bus.emit(EventType.WORLD_CHANGED, "t", {"i": i})
        self.assertEqual(len(bus.recent(limit=50)), 5)


class TaskTests(unittest.TestCase):
    def test_transitions(self):
        w = WorldState()
        bus = EventBus()
        tm = TaskManager(w, bus)
        tm.start("go to table")
        self.assertEqual(tm.state, PLANNING)
        tm.transition(EXECUTING, action="forward")
        tm.transition(REPLANNING, action="blocked")
        tm.transition(EXECUTING, action="left")
        tm.transition(COMPLETED, progress=1.0)
        self.assertEqual(tm.state, COMPLETED)
        with self.assertRaises(ValueError):
            tm.transition(EXECUTING)

    def test_pad_from_idle(self):
        w = WorldState()
        bus = EventBus()
        tm = TaskManager(w, bus)
        self.assertEqual(tm.state, IDLE)
        tm.ensure_executing(action="left")
        self.assertEqual(tm.state, IDLE)
        self.assertEqual(tm.action, "left")


class SafetyTests(unittest.TestCase):
    def _ctrl(self):
        cfg = load_config(os.path.join(ROOT, "config.yaml"))
        w = WorldState(stale_s=0.5)
        w.update_obstacles(1.5, [0.1, 0.1, 0.1, 0.1, 0.1], False, "front", 0.7, "test")
        w.set_perception_meta(last_depth_ts=time.time(), last_frame_ts=time.time())
        motors = SimulatedMotors()
        bus = EventBus()
        ctrl = SafetyController(cfg, motors, w, bus)
        ctrl.allow_motion = True
        return ctrl, w, motors, bus

    def test_obstacle_blocks_forward(self):
        ctrl, w, motors, bus = self._ctrl()
        w.update_obstacles(0.12, [0.9, 0.9, 0.9, 0.9, 0.9], True, "front", 0.9, "test")
        w.set_perception_meta(last_depth_ts=time.time())
        ok, reason = ctrl.request(MotionCommand(0.2, 0.2, "unit", 1.0))
        self.assertFalse(ok)
        self.assertEqual(reason, "obstacle")

    def test_inplace_turn_allowed_when_forward_would_stop(self):
        ctrl, w, motors, bus = self._ctrl()
        # Between turn_obstacle_m (0.06) and min_obstacle_m (0.18).
        w.update_obstacles(0.12, [0.7, 0.7, 0.8, 0.7, 0.7], True, "front", 0.8, "test")
        w.set_perception_meta(last_depth_ts=time.time())
        ok, reason, safe = ctrl.validate(MotionCommand(-0.22, 0.22, "nav-left", 0.4))
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")
        self.assertLess(safe.left, 0.0)
        self.assertGreater(safe.right, 0.0)
        ok, reason = ctrl.request(MotionCommand(0.2, 0.2, "nav-forward", 0.4))
        self.assertFalse(ok)
        self.assertEqual(reason, "obstacle")

    def test_inplace_turn_stops_when_nearly_touching(self):
        ctrl, w, motors, bus = self._ctrl()
        w.update_obstacles(0.04, [0.95, 0.95, 0.95, 0.95, 0.95], True, "front", 0.9, "test")
        w.set_perception_meta(last_depth_ts=time.time())
        ok, reason, safe = ctrl.validate(MotionCommand(0.22, -0.22, "nav-right", 0.4))
        self.assertFalse(ok)
        self.assertEqual(reason, "obstacle")

    def test_estop_bypasses_everything(self):
        ctrl, w, motors, bus = self._ctrl()
        motors.set_speeds(0.2, 0.2)
        ctrl.emergency_stop("test")
        self.assertTrue(ctrl.estop)
        self.assertEqual(motors.snapshot()["left"], 0.0)
        ok, reason = ctrl.request(MotionCommand(0.2, 0.2, "unit", 1.0))
        self.assertFalse(ok)
        self.assertEqual(reason, "estop")

    def test_stale_perception_blocks_forward(self):
        ctrl, w, motors, bus = self._ctrl()
        w.perception["last_depth_ts"] = time.time() - 5
        ok, reason = ctrl.request(MotionCommand(0.2, 0.2, "unit", 1.0))
        self.assertFalse(ok)
        self.assertEqual(reason, "stale_perception")

    def test_speed_clamp(self):
        ctrl, w, motors, bus = self._ctrl()
        ok, reason, safe = ctrl.validate(MotionCommand(1.0, 1.0, "unit", 1.0))
        self.assertTrue(ok)
        self.assertLessEqual(safe.left, ctrl.max_speed + 1e-6)

    def test_planner_keeps_safety_controller(self):
        ctrl, w, motors, bus = self._ctrl()
        planner = LocalPlanner(load_config(os.path.join(ROOT, "config.yaml")), w, ctrl, bus)
        self.assertTrue(hasattr(planner.safety, "request"))
        self.assertIs(planner.safety, ctrl)

    def test_motion_lock_accepts_without_spinning(self):
        ctrl, w, motors, bus = self._ctrl()
        ctrl.allow_motion = False
        motors.set_speeds(0.0, 0.0)
        ok, reason = ctrl.request(MotionCommand(-0.22, 0.22, "ui-left", 0.4))
        self.assertFalse(ok)
        self.assertEqual(reason, "motion_locked")
        snap = motors.snapshot()
        self.assertEqual(snap["left"], 0.0)
        self.assertEqual(snap["right"], 0.0)

    def test_set_allow_motion_unlocks_turns(self):
        ctrl, w, motors, bus = self._ctrl()
        ctrl.set_allow_motion(False, source="test")
        ok, reason = ctrl.request(MotionCommand(0.22, -0.22, "ui-right", 0.4))
        self.assertFalse(ok)
        self.assertEqual(reason, "motion_locked")
        ctrl.set_allow_motion(True, source="test")
        ok, reason = ctrl.request(MotionCommand(0.22, -0.22, "ui-right", 0.4))
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")


class MemoryTests(unittest.TestCase):
    def test_policy_and_search(self):
        path = tempfile.mkstemp(suffix=".json")[1]
        try:
            mem = SemanticMemory(path, max_notes=20)
            self.assertIsNone(mem.add_note("x", reason="frame"))  # not meaningful
            nid = mem.add_note("The charging station was seen near the east wall.", reason="new_area")
            self.assertTrue(nid)
            # duplicate shortly after is suppressed
            self.assertIsNone(mem.add_note("The charging station was seen near the east wall.", reason="new_area"))
            hits = mem.search("charging station wall")
            self.assertTrue(hits)
            self.assertIn("charging", hits[0]["content"].lower())
        finally:
            try:
                os.remove(path)
            except Exception:
                pass


class TrackerTests(unittest.TestCase):
    def test_stable_id(self):
        tr = IoUTracker()
        d1 = [Detection("object", 0.8, [0.1, 0.1, 0.3, 0.4])]
        a = tr.update(d1)
        d2 = [Detection("object", 0.8, [0.12, 0.11, 0.32, 0.42])]
        b = tr.update(d2)
        self.assertEqual(a[0].id, b[0].id)


class FrameBufferTests(unittest.TestCase):
    def test_latest_frame_drops_old(self):
        buf = LatestFrameBuffer()
        buf.put(np.zeros((2, 2, 3), dtype=np.uint8))
        buf.put(np.ones((2, 2, 3), dtype=np.uint8))
        frame, ts, seq = buf.get()
        self.assertEqual(seq, 2)
        self.assertEqual(int(frame[0, 0, 0]), 1)


class PlanParseTests(unittest.TestCase):
    def test_json_fence(self):
        text = '```json\n{"say": "scan first", "tools": [{"name": "camera.scan_environment", "args": {}}], "done": false}\n```'
        plan = parse_plan(text)
        self.assertEqual(plan["say"], "scan first")
        self.assertEqual(plan["tools"][0]["name"], "camera.scan_environment")

    def test_truncated_json_recovers_tool(self):
        text = (
            '```json\n{\n  "say": "The path is blocked directly ahead. Scanning the environment.",\n'
            '  "reason": "The front path is blocked.",\n  "need_llm": false,\n'
            '  "tools": [\n    {\n      "name": "camera.'
        )
        plan = parse_plan(text)
        self.assertEqual(plan["say"], "The path is blocked directly ahead. Scanning the environment.")
        self.assertEqual(plan["tools"][0]["name"], "camera.scan_environment")

    def test_empty_content(self):
        plan = parse_plan("")
        self.assertEqual(plan["reason"], "empty llm")
        self.assertEqual(plan["tools"], [])


class AgentPlanFallbackTests(unittest.TestCase):
    def test_needs_llm_for_replan(self):
        self.assertTrue(_needs_llm("path blocked. replan using current world state."))
        self.assertTrue(_needs_llm("look around and tell me if you see a chair"))
        self.assertFalse(_needs_llm("look around"))
        self.assertFalse(_needs_llm("go forward"))

    def test_truncated_plan_gets_scan_tool(self):
        class Dummy(object):
            pass
        agent = Agent.__new__(Agent)
        agent.world = Dummy()
        agent.world.snapshot = lambda: {"obstacles": {"blocked": True}}
        plan = {
            "say": "I could not parse a plan.",
            "reason": "json parse failed",
            "tools": [],
        }
        out = agent._ensure_usable_plan(plan, "Path blocked. Replan.")
        self.assertEqual(out["tools"][0]["name"], "camera.scan_environment")
        self.assertIn("scan", out["say"].lower())


class ConfigTests(unittest.TestCase):
    def test_loads_yaml(self):
        cfg = load_config(os.path.join(ROOT, "config.yaml"))
        self.assertEqual(cfg["llm"]["base_url"], "http://192.168.20.150:8008/v1")
        self.assertEqual(cfg["whisper"]["base_url"], "http://192.168.20.150:8003")


class FakeJetbotRobot(object):
    def __init__(self):
        self.left = 0.0
        self.right = 0.0
        self.calls = []

    def set_motors(self, left_speed, right_speed):
        self.left = left_speed
        self.right = right_speed
        self.calls.append(("set_motors", left_speed, right_speed))

    def stop(self):
        self.left = 0.0
        self.right = 0.0
        self.calls.append(("stop",))


class JetbotLibTests(unittest.TestCase):
    def test_set_motors_via_kit_api(self):
        robot = FakeJetbotRobot()
        ctrl = JetbotLibController(robot)
        ctrl.set_speeds(-0.2, 0.2)
        self.assertEqual(robot.calls[-1][0], "set_motors")
        self.assertAlmostEqual(robot.left, -0.2)
        self.assertAlmostEqual(robot.right, 0.2)
        snap = ctrl.snapshot()
        self.assertEqual(snap["backend"], "jetbot")
        ctrl.stop()
        self.assertEqual(robot.left, 0.0)
        self.assertEqual(robot.right, 0.0)


class GeometricDepthTests(unittest.TestCase):
    def test_open_vs_blocked(self):
        from jetbot_core.perception.depth import GeometricDepthEstimator
        est = GeometricDepthEstimator()
        open_img = np.full((120, 160, 3), 180, dtype=np.uint8)
        blocked = np.full((120, 160, 3), 180, dtype=np.uint8)
        blocked[70:115, 60:100] = (20, 20, 200)
        a = est.estimate(open_img)
        b = est.estimate(blocked)
        self.assertGreater(b["bins"][2], a["bins"][2])

    def test_blocked_blob_reaches_stop_distance(self):
        from jetbot_core.perception.depth import GeometricDepthEstimator
        est = GeometricDepthEstimator(min_m=0.05, blocked_m=0.18)
        blocked = np.full((120, 160, 3), 180, dtype=np.uint8)
        blocked[70:115, 20:140] = (10, 10, 180)
        out = est.estimate(blocked)
        self.assertTrue(out["blocked"])
        self.assertLessEqual(out["front_m"], 0.18)

    def test_open_floor_not_blocked(self):
        from jetbot_core.perception.depth import GeometricDepthEstimator
        est = GeometricDepthEstimator(min_m=0.05, blocked_m=0.18)
        open_img = np.full((120, 160, 3), 180, dtype=np.uint8)
        out = est.estimate(open_img)
        self.assertFalse(out["blocked"])
        self.assertGreater(out["front_m"], 0.36)


class PipelineFixTests(unittest.TestCase):
    def test_safety_stops_when_world_blocked(self):
        cfg = load_config(os.path.join(ROOT, "config.yaml"))
        w = WorldState(stale_s=0.5, stop_m=0.18)
        w.update_obstacles(0.12, [0.9, 0.9, 0.9, 0.9, 0.9], False, "front", 0.9, "test")
        w.set_perception_meta(last_depth_ts=time.time(), last_frame_ts=time.time())
        self.assertTrue(w.obstacles["blocked"])
        motors = SimulatedMotors()
        ctrl = SafetyController(cfg, motors, w, EventBus())
        ctrl.allow_motion = True
        ok, reason = ctrl.request(MotionCommand(0.2, 0.2, "unit", 1.0))
        self.assertFalse(ok)
        self.assertEqual(reason, "obstacle")

    def test_explore_expires(self):
        cfg = load_config(os.path.join(ROOT, "config.yaml"))
        w = WorldState()
        w.update_obstacles(1.5, [0.1, 0.1, 0.1, 0.1, 0.1], False, "front", 0.7, "test")
        w.set_perception_meta(last_depth_ts=time.time())
        planner = LocalPlanner(cfg, w, SafetyController(cfg, SimulatedMotors(), w, EventBus()), EventBus())
        planner.safety.allow_motion = True
        planner.set_mode("explore", duration_s=0.05)
        time.sleep(0.08)
        planner._compute()
        self.assertEqual(planner.mode, "idle")

    def test_follow_expires(self):
        cfg = load_config(os.path.join(ROOT, "config.yaml"))
        w = WorldState()
        w.update_obstacles(1.5, [0.1, 0.1, 0.1, 0.1, 0.1], False, "front", 0.7, "test")
        w.set_perception_meta(last_depth_ts=time.time())
        planner = LocalPlanner(cfg, w, SafetyController(cfg, SimulatedMotors(), w, EventBus()), EventBus())
        planner.set_mode("follow", duration_s=0.05)
        time.sleep(0.08)
        planner._compute()
        self.assertEqual(planner.mode, "idle")

    def test_scan_halts_planner(self):
        cfg = load_config(os.path.join(ROOT, "config.yaml"))
        w = WorldState()
        w.update_obstacles(1.5, [0.1] * 5, False, "front", 0.7, "test")
        bus = EventBus()
        planner = LocalPlanner(cfg, w, SafetyController(cfg, SimulatedMotors(), w, bus), bus)
        planner.set_mode("explore", duration_s=4.0)
        tasks = TaskManager(w, bus)
        tools = ToolRouter(w, planner.safety, planner, FixedServo(), SemanticMemory(tempfile.mkstemp(suffix=".json")[1]), tasks, bus)
        tools._scan({})
        time.sleep(0.05)
        self.assertIn(planner.mode, ("stop", "idle", "scan", "left", "right"))
        self.assertNotEqual(planner.mode, "explore")

    def test_look_on_fixed_camera_does_not_change_pan(self):
        cfg = load_config(os.path.join(ROOT, "config.yaml"))
        w = WorldState()
        bus = EventBus()
        planner = LocalPlanner(cfg, w, SafetyController(cfg, SimulatedMotors(), w, bus), bus)
        tasks = TaskManager(w, bus)
        tools = ToolRouter(w, planner.safety, planner, FixedServo(), SemanticMemory(tempfile.mkstemp(suffix=".json")[1]), tasks, bus)
        out = tools.dispatch("camera.look_left", {})
        self.assertTrue(out.get("ok"))
        self.assertEqual(out.get("pan"), 0.0)
        self.assertEqual(w.robot["camera_pan"], 0.0)

    def test_scan_does_not_fake_pan(self):
        servo = FixedServo()
        pan, tilt = servo.look_named("left")
        self.assertEqual(pan, 0.0)
        self.assertEqual(tilt, 0.0)
        self.assertEqual(servo.snapshot()["backend"], "fixed")

    def test_try_create_servo_disabled(self):
        cfg = load_config(os.path.join(ROOT, "config.yaml"))
        self.assertFalse(cfg["servo"]["enabled"])
        ctrl, backend = try_create_servo(cfg)
        self.assertEqual(backend, "fixed")
        self.assertFalse(ctrl.enabled)

    def test_keywords_before_typesafe(self):
        class Hang(object):
            def classify_intent(self, text):
                raise AssertionError("TypeSafe should not run for keyword intents")

        class Dummy(object):
            pass

        agent = Agent.__new__(Agent)
        agent.typesafe = Hang()
        hit = Agent._keyword_intent(agent, "go forward")
        self.assertEqual(hit[0], "forward")
        out = Agent._local_intent(agent, "go forward", source="web")
        self.assertEqual(out[0], "forward")

    def test_blocked_does_not_submit_immediately(self):
        class Dummy(object):
            pass

        submitted = []
        agent = Agent.__new__(Agent)
        agent.tasks = Dummy()
        agent.tasks.state = IDLE
        agent.tasks.task_id = None
        agent.submit = lambda *a, **k: submitted.append(a)
        Agent._on_blocked(agent, Dummy())
        self.assertEqual(submitted, [])

    def test_safety_reads_obstacles_without_full_snapshot(self):
        cfg = load_config(os.path.join(ROOT, "config.yaml"))
        w = WorldState(stale_s=0.5, stop_m=0.18)
        w.update_obstacles(1.5, [0.1] * 5, False, "front", 0.7, "test")
        w.set_perception_meta(last_depth_ts=time.time())
        ctrl = SafetyController(cfg, SimulatedMotors(), w, EventBus())
        ctrl.allow_motion = True
        orig = w.snapshot

        def boom():
            raise AssertionError("validate must not deepcopy the world")

        w.snapshot = boom
        try:
            ok, reason, safe = ctrl.validate(MotionCommand(0.2, 0.2, "unit", 1.0))
            self.assertTrue(ok)
        finally:
            w.snapshot = orig

    def test_frame_buffer_put_copies(self):
        buf = LatestFrameBuffer()
        src = np.zeros((2, 2, 3), dtype=np.uint8)
        buf.put(src)
        src[0, 0] = 9
        frame, ts, seq = buf.get(copy=False)
        self.assertEqual(int(frame[0, 0, 0]), 0)

    def test_estop_beats_inflight_apply(self):
        cfg = load_config(os.path.join(ROOT, "config.yaml"))
        w = WorldState(stale_s=0.5)
        w.update_obstacles(1.5, [0.1] * 5, False, "front", 0.7, "test")
        w.set_perception_meta(last_depth_ts=time.time())
        motors = SimulatedMotors()
        ctrl = SafetyController(cfg, motors, w, EventBus())
        ctrl.allow_motion = True
        ctrl.request(MotionCommand(0.2, 0.2, "unit", 2.0))
        ctrl.emergency_stop("test")
        # Re-check path used by _loop before set_speeds.
        with ctrl._lock:
            estop = ctrl._estop
            allow = ctrl._allow_motion
        self.assertTrue(estop)
        if estop or not allow:
            motors.set_speeds(0.0, 0.0)
        self.assertEqual(motors.snapshot()["left"], 0.0)
        self.assertEqual(motors.snapshot()["right"], 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
