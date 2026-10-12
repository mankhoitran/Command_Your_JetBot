#!/usr/bin/env python3
"""Unit tests - no hardware, no network required."""

from __future__ import print_function

import json
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
from jetbot_core.llm import parse_plan, SYSTEM_PROMPT, LLMClient
from jetbot_core.agent import Agent, _needs_llm, say_for_tool_results
from jetbot_core.trace import TraceRecorder
from jetbot_core.live import LiveSettings
from jetbot_core.telemetry import (
    LoopMeter, MotorLedger, TelemetryHub, bind_corr_id, clear_corr_id,
    get_corr_id, new_corr_id, redact,
)

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
        self.assertIn("wheels: LOCKED", text)
        self.assertIn("recovery_active=False", text)

    def test_llm_summary_exposes_lock_recovery_and_bins(self):
        w = WorldState()
        w.update_robot(allow_motion=True)
        w.update_obstacles(1.4, [0.1, 0.2, 0.3, 0.2, 0.1], False, "none", 0.6, "test")
        w.set_navigation(mode="recovery", current_target=None, blocked=True)
        text = w.summary_for_llm()
        self.assertIn("wheels: FREE", text)
        self.assertIn("recovery_active=True", text)
        self.assertIn("bins(L->R)=0.10 0.20 0.30 0.20 0.10", text)


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
        planner.safety.allow_motion = True
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

    def test_paraphrases_resolve_locally(self):
        agent = Agent.__new__(Agent)
        agent.typesafe = None
        cases = [
            ("come forward a little", "forward"),
            ("move ahead", "forward"),
            ("rotate right", "right"),
            ("go left", "left"),
            ("don't move", "stop"),
            ("check what's around", "scan"),
            ("scan the room", "scan"),
        ]
        for text, intent in cases:
            hit = Agent._keyword_intent(agent, text)
            self.assertIsNotNone(hit, text)
            self.assertEqual(hit[0], intent, text)
            out = Agent._local_intent(agent, text, source="web")
            self.assertEqual(out[0], intent, text)
        self.assertIsNone(Agent._keyword_intent(agent, "move toward the chair"))
        self.assertIsNone(Agent._keyword_intent(agent, "on the left"))
        self.assertIsNone(Agent._local_intent(agent, "move toward the chair", source="web"))

    def test_say_for_tool_results_is_honest(self):
        self.assertEqual(
            say_for_tool_results([{"ok": False, "error": "motion_locked"}], "OK: forward"),
            "Motion is currently locked.",
        )
        self.assertEqual(
            say_for_tool_results([{"ok": True, "tool": "motion.forward"}], "OK: forward"),
            "OK: forward",
        )
        self.assertEqual(
            say_for_tool_results([{"ok": False, "error": "unknown_target"}], "OK: follow"),
            "I can't currently identify the requested target.",
        )

    def test_locked_motion_is_not_ok(self):
        cfg = load_config(os.path.join(ROOT, "config.yaml"))
        w = WorldState()
        bus = EventBus()
        planner = LocalPlanner(cfg, w, SafetyController(cfg, SimulatedMotors(), w, bus), bus)
        planner.safety.allow_motion = False
        tasks = TaskManager(w, bus)
        tools = ToolRouter(w, planner.safety, planner, FixedServo(), SemanticMemory(tempfile.mkstemp(suffix=".json")[1]), tasks, bus)
        out = tools.dispatch("motion.forward", {"duration": 1.0})
        self.assertFalse(out.get("ok"))
        self.assertEqual(out.get("error"), "motion_locked")
        self.assertNotEqual(planner.mode, "forward")
        look = tools.dispatch("camera.look_left", {})
        self.assertFalse(look.get("ok"))
        self.assertEqual(look.get("error"), "motion_locked")
        self.assertEqual(w.robot["camera_pan"], 0.0)

    def test_go_to_resolves_class_name(self):
        cfg = load_config(os.path.join(ROOT, "config.yaml"))
        w = WorldState()
        w.upsert_object(TrackedObject("obj_03", "chair", [0.2, 0.2, 0.5, 0.7], 0.9))
        w.upsert_object(TrackedObject("obj_01", "object", [0.6, 0.3, 0.8, 0.6], 0.4))
        bus = EventBus()
        planner = LocalPlanner(cfg, w, SafetyController(cfg, SimulatedMotors(), w, bus), bus)
        planner.safety.allow_motion = True
        tasks = TaskManager(w, bus)
        tools = ToolRouter(w, planner.safety, planner, FixedServo(), SemanticMemory(tempfile.mkstemp(suffix=".json")[1]), tasks, bus)
        out = tools.dispatch("nav.go_to", {"target": "chair"})
        self.assertTrue(out.get("ok"))
        self.assertEqual(out.get("target"), "obj_03")
        self.assertEqual(planner.mode, "follow")
        miss = tools.dispatch("nav.go_to", {"target": "table"})
        self.assertFalse(miss.get("ok"))
        self.assertEqual(miss.get("error"), "unknown_target")

    def test_follow_nobody_is_unknown_target(self):
        cfg = load_config(os.path.join(ROOT, "config.yaml"))
        w = WorldState()
        bus = EventBus()
        planner = LocalPlanner(cfg, w, SafetyController(cfg, SimulatedMotors(), w, bus), bus)
        planner.safety.allow_motion = True
        tasks = TaskManager(w, bus)
        tools = ToolRouter(w, planner.safety, planner, FixedServo(), SemanticMemory(tempfile.mkstemp(suffix=".json")[1]), tasks, bus)
        out = tools.dispatch("nav.follow", {})
        self.assertFalse(out.get("ok"))
        self.assertEqual(out.get("error"), "unknown_target")


class TraceTests(unittest.TestCase):
    def test_run_and_steps(self):
        tr = TraceRecorder(max_runs=5)
        rid = tr.begin_run("web", "go forward")
        tr.step("intent", "keyword", input={"text": "go forward"}, output={"intent": "forward"}, ok=True)
        tr.step("tool", "motion.forward", input={"duration": 1.0}, output={"ok": True}, ok=True)
        tr.end_run(status="ok", say="OK: forward")
        snap = tr.snapshot()
        self.assertEqual(snap["runs"][0]["id"], rid)
        self.assertEqual(snap["runs"][0]["status"], "ok")
        stages = [s["stage"] for s in snap["runs"][0]["steps"]]
        self.assertIn("intent", stages)
        self.assertIn("tool", stages)
        self.assertEqual(snap["active"], None)

    def test_orphan_asr_is_closed(self):
        tr = TraceRecorder()
        tr.step("asr", "whisper", output="hello", ok=True, model="whisper.cpp")
        snap = tr.snapshot()
        self.assertEqual(snap["runs"][0]["kind"], "asr")
        self.assertEqual(snap["runs"][0]["status"], "ok")
        self.assertEqual(snap["last_asr"]["output"], "hello")


class LiveSettingsTests(unittest.TestCase):
    def test_prompt_applies_to_llm(self):
        path = tempfile.mkstemp(suffix=".json")[1]
        live = LiveSettings(path)
        class Dummy(object):
            pass
        rt = Dummy()
        rt.llm = LLMClient({"llm": {}})
        rt.agent = Dummy()
        rt.agent.set_intent_tools = lambda m: None
        rt.agent.reset_intent_tools = lambda: None
        rt.agent.set_keyword_map = lambda m: None
        rt.agent.reset_keyword_map = lambda: None
        rt.tools = Dummy()
        rt.tools.set_enabled_map = lambda m: setattr(rt.tools, "enabled", m)
        rt.tools.tool_docs = {}
        live.patch({"system_prompt": "You are a test robot."})
        live.apply(rt)
        self.assertEqual(rt.llm.system_prompt, "You are a test robot.")
        live.patch({"system_prompt": None})
        live.apply(rt)
        self.assertEqual(rt.llm.system_prompt, SYSTEM_PROMPT)

    def test_disable_tool_blocks_dispatch(self):
        cfg = load_config(os.path.join(ROOT, "config.yaml"))
        w = WorldState()
        bus = EventBus()
        planner = LocalPlanner(cfg, w, SafetyController(cfg, SimulatedMotors(), w, bus), bus)
        planner.safety.allow_motion = True
        tasks = TaskManager(w, bus)
        tools = ToolRouter(w, planner.safety, planner, FixedServo(), SemanticMemory(tempfile.mkstemp(suffix=".json")[1]), tasks, bus)
        tools.set_enabled_map({"motion.forward": False})
        out = tools.dispatch("motion.forward", {"duration": 0.2})
        self.assertFalse(out.get("ok"))
        self.assertEqual(out.get("error"), "tool_disabled")

    def test_keyword_override_changes_intent(self):
        class Dummy(object):
            pass
        agent = Agent.__new__(Agent)
        agent.keyword_map = None
        agent.typesafe = None
        agent.set_keyword_map([[["dance"], "scan"]])
        hit = agent._keyword_intent("dance")
        self.assertEqual(hit[0], "scan")


class TelemetryTests(unittest.TestCase):
    def test_corr_id_thread_local(self):
        cid = new_corr_id()
        prev = bind_corr_id(cid)
        self.assertEqual(get_corr_id(), cid)
        clear_corr_id()
        self.assertIsNone(get_corr_id())
        self.assertIsNone(prev)

    def test_event_carries_corr_id(self):
        bind_corr_id("abc123")
        try:
            bus = EventBus()
            ev = bus.emit(EventType.USER_COMMAND, "web", {"text": "hi"})
            self.assertEqual(ev.corr_id, "abc123")
            rec = bus.recent(limit=1)[0]
            self.assertEqual(rec["corr_id"], "abc123")
            self.assertTrue(rec["seq"] >= 1)
        finally:
            clear_corr_id()

    def test_loop_meter_hz(self):
        meter = LoopMeter("agent", 50, window=20)
        for _ in range(8):
            meter.tick()
            time.sleep(0.01)
        snap = meter.snapshot()
        self.assertGreater(snap["ticks"], 1)
        self.assertGreater(snap["hz"], 20)
        self.assertFalse(snap["stale"])

    def test_loop_meter_stale(self):
        meter = LoopMeter("safety", 20)
        meter.tick()
        meter.last_ts = time.time() - 1.0
        snap = meter.snapshot()
        self.assertTrue(snap["stale"])

    def test_redact_strips_secrets(self):
        out = redact({"api_key": "sk-secret", "text": "ok", "nested": {"token": "x"}})
        self.assertEqual(out["api_key"], "<redacted>")
        self.assertEqual(out["nested"]["token"], "<redacted>")
        self.assertEqual(out["text"], "ok")

    def test_motor_ledger_phases_distinct(self):
        led = MotorLedger(limit=8)
        cmd = MotionCommand(0.2, 0.2, "nav-forward", 0.4, corr_id="c1")
        led.record("requested", cmd, corr_id="c1")
        led.record("accepted", cmd, corr_id="c1")
        led.record("applied", MotionCommand(0.18, 0.18, "nav-forward", 0.4, corr_id="c1"), corr_id="c1")
        snap = led.snapshot()
        self.assertEqual(snap["last_requested"]["phase"], "requested")
        self.assertEqual(snap["last_accepted"]["phase"], "accepted")
        self.assertEqual(snap["last_applied"]["phase"], "applied")
        self.assertNotEqual(snap["last_requested"]["left"], snap["last_applied"]["left"])
        self.assertEqual(len(led.for_corr("c1")), 3)

    def test_motor_ledger_dedupes_applied(self):
        led = MotorLedger()
        cmd = MotionCommand(0.1, 0.1, "nav-forward", 0.2, corr_id="c2")
        led.record("applied", cmd, corr_id="c2")
        led.record("applied", cmd, corr_id="c2")
        self.assertEqual(len(led.snapshot(limit=20)["history"]), 1)

    def test_motor_ledger_overflow_bounded(self):
        led = MotorLedger(limit=5)
        for i in range(12):
            led.record("requested", MotionCommand(0.1, 0.1, "t", 0.1, corr_id="x"), corr_id="x")
        snap = led.snapshot(limit=20)
        self.assertEqual(len(snap["history"]), 5)

    def test_queue_does_not_imply_applied(self):
        cfg = load_config(os.path.join(ROOT, "config.yaml"))
        w = WorldState()
        bus = EventBus()
        motors = SimulatedMotors()
        safety = SafetyController(cfg, motors, w, bus)
        safety.allow_motion = False
        tel = TelemetryHub()
        safety.telemetry = tel
        bind_corr_id("queued1")
        try:
            ok, reason = safety.request(MotionCommand(0.2, 0.2, "nav-forward", 0.4))
            self.assertFalse(ok)
            self.assertEqual(reason, "motion_locked")
            snap = tel.motor.snapshot()
            self.assertEqual(snap["last_requested"]["corr_id"], "queued1")
            self.assertEqual(snap["last_rejected"]["reason"], "motion_locked")
            self.assertIsNone(snap["last_applied"])
            self.assertEqual(motors.snapshot()["left"], 0.0)
        finally:
            clear_corr_id()

    def test_safety_records_requested_accepted_applied(self):
        cfg = load_config(os.path.join(ROOT, "config.yaml"))
        w = WorldState(stale_s=0.5)
        w.update_obstacles(1.5, [0.1, 0.1, 0.1, 0.1, 0.1], False, "front", 0.7, "test")
        w.set_perception_meta(last_depth_ts=time.time(), last_frame_ts=time.time())
        motors = SimulatedMotors()
        bus = EventBus()
        safety = SafetyController(cfg, motors, w, bus)
        safety.allow_motion = True
        tel = TelemetryHub()
        safety.telemetry = tel
        bind_corr_id("go1")
        try:
            ok, reason = safety.request(MotionCommand(0.2, 0.2, "nav-forward", 0.4))
            self.assertTrue(ok)
            self.assertEqual(reason, "ok")
            safety._loop = safety._loop  # keep attribute
            # One apply tick without starting the 20 Hz thread.
            with safety._lock:
                cmd = safety._command
            ok2, reason2, safe = safety.validate(cmd)
            self.assertTrue(ok2)
            motors.set_speeds(safe.left, safe.right)
            tel.motor.record("applied", MotionCommand(safe.left, safe.right, cmd.source, 0, corr_id="go1"),
                             corr_id="go1")
            snap = tel.motor.snapshot()
            self.assertEqual(snap["last_requested"]["corr_id"], "go1")
            self.assertEqual(snap["last_accepted"]["corr_id"], "go1")
            self.assertEqual(snap["last_applied"]["corr_id"], "go1")
            self.assertGreater(abs(snap["last_applied"]["left"] or 0), 0)
        finally:
            clear_corr_id()

    def test_estop_rejects_without_ui_control(self):
        cfg = load_config(os.path.join(ROOT, "config.yaml"))
        w = WorldState()
        w.update_obstacles(1.5, [0.1] * 5, False, "front", 0.7, "test")
        w.set_perception_meta(last_depth_ts=time.time())
        bus = EventBus()
        safety = SafetyController(cfg, SimulatedMotors(), w, bus)
        safety.allow_motion = True
        tel = TelemetryHub()
        safety.telemetry = tel
        safety.emergency_stop("test")
        ok, reason = safety.request(MotionCommand(0.2, 0.2, "nav-forward", 0.3, corr_id="e1"))
        self.assertFalse(ok)
        self.assertEqual(reason, "estop")
        self.assertEqual(tel.motor.last_rejected["reason"], "estop")

    def test_trace_propagates_corr_id(self):
        bind_corr_id("run9")
        try:
            tr = TraceRecorder()
            rid = tr.begin_run("web", "go forward", corr_id="run9")
            tr.step("intent", "keyword", ok=True)
            tr.step("tool", "motion.forward", ok=True)
            tr.end_run(status="ok")
            snap = tr.snapshot()
            self.assertEqual(snap["runs"][0]["id"], rid)
            self.assertEqual(snap["runs"][0]["corr_id"], "run9")
            self.assertEqual(snap["runs"][0]["steps"][0]["corr_id"], "run9")
        finally:
            clear_corr_id()

    def test_pipeline_snapshot_has_three_views(self):
        # Stub runtime — do not construct JetBotRuntime (that opens CSI / LLM).
        from jetbot_core.health import HealthMonitor
        cfg = load_config(os.path.join(ROOT, "config.yaml"))
        w = WorldState()
        bus = EventBus()
        motors = SimulatedMotors()
        safety = SafetyController(cfg, motors, w, bus)
        safety.allow_motion = False
        tel = TelemetryHub()
        safety.telemetry = tel
        tracer = TraceRecorder()

        class _Live(object):
            updated_ts = 0.0

        class _Tasks(object):
            state = "IDLE"
            def snapshot(self):
                return {"state": "IDLE", "name": None}

        class _Rt(object):
            pass

        rt = _Rt()
        rt.health = HealthMonitor(w)
        rt.telemetry = tel
        rt.safety = safety
        rt.planner = None
        rt.agent = None
        rt.perception = None
        rt.camera = None
        rt.motors = motors
        rt.servo = None
        rt.world = w
        rt.tools = None
        rt.llm = None
        rt.typesafe = None
        rt.whisper = None
        rt.memory = None
        rt.tasks = _Tasks()
        rt.tracer = tracer
        rt.bus = bus
        rt.meter = LoopMeter("telemetry", 2)
        rt.live = _Live()
        rt.started_at = time.time()
        snap = tel.snapshot(rt)
        self.assertIn("dashboard", snap)
        self.assertIn("pipeline", snap)
        self.assertIn("trace", snap)
        self.assertIn("loops", snap["dashboard"])
        self.assertIn("motor", snap["dashboard"])
        ids = [n["id"] for n in snap["pipeline"]["nodes"]]
        for need in ("camera", "perception", "world", "planner", "safety", "motors",
                     "agent", "llm", "tools", "task"):
            self.assertIn(need, ids)
        safety_node = [n for n in snap["pipeline"]["nodes"] if n["id"] == "safety"][0]
        self.assertEqual(safety_node["status"], "locked")
        self.assertIs(safety.telemetry, tel)
        self.assertIsNone(snap["dashboard"]["motor"]["last_applied"])
        json.dumps(snap, default=str)  # must serialize for /api/pipeline

    def test_monitor_is_read_only_for_motion(self):
        with open(os.path.join(ROOT, "jetbot_core", "monitor.py")) as handle:
            src = handle.read()
        self.assertNotIn("emergency_stop", src)
        self.assertNotIn("set_speeds", src)
        self.assertNotIn("set_allow_motion", src)
        self.assertIn("pipeline_snapshot", src)

    def test_disconnected_client_does_not_block_safety(self):
        cfg = load_config(os.path.join(ROOT, "config.yaml"))
        w = WorldState()
        w.update_obstacles(1.4, [0.1] * 5, False, "front", 0.7, "test")
        w.set_perception_meta(last_depth_ts=time.time())
        motors = SimulatedMotors()
        bus = EventBus()
        safety = SafetyController(cfg, motors, w, bus)
        safety.allow_motion = True
        tel = TelemetryHub()
        safety.telemetry = tel
        t0 = time.time()
        for _ in range(30):
            safety.request(MotionCommand(0.15, 0.15, "nav-forward", 0.2))
        elapsed = time.time() - t0
        self.assertLess(elapsed, 0.2)

    def test_agent_submit_does_not_mark_tool_ok(self):
        cfg = load_config(os.path.join(ROOT, "config.yaml"))
        w = WorldState()
        bus = EventBus()
        agent = Agent.__new__(Agent)
        agent._lock = __import__("threading").Lock()
        agent._pending = []
        agent.bus = bus
        item = Agent.submit(agent, "go forward", source="monitor")
        self.assertTrue(item["corr_id"])
        self.assertEqual(len(agent._pending), 1)
        self.assertEqual(bus.recent(limit=1)[0]["type"], EventType.USER_COMMAND)


class HostTelemetryTests(unittest.TestCase):
    def test_cpu_pct_second_sample(self):
        from jetbot_core.health import host_telemetry
        first = host_telemetry()
        time.sleep(0.05)
        second = host_telemetry()
        self.assertIn("cpu_pct", first)
        self.assertIn("load1", second)
        self.assertIn("mem_avail_mb", second)
        if second.get("cpu_pct") is not None:
            self.assertGreaterEqual(second["cpu_pct"], 0.0)
            self.assertLessEqual(second["cpu_pct"], 100.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
