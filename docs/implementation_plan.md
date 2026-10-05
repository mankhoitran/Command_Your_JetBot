# Implementation Plan — Pipeline Fixes

**Date:** 2026-10-05  
**Source:** `docs/pipeline_audit.md`  
**Target:** `/home/jetbot/CYJ`  
**Constraint:** Python 3.6, Jetson Nano 4GB, one process, three loops, one motor owner  
**Status:** Implemented 2026-10-05. `python3 tests/test_core.py` — 39 tests OK. Camera remains **fixed** (`servo.enabled: false`).

Do **not** add a second planner, ROS, extra perception threads, or another LLM path. The architecture is correct. Interfaces and dual writers are not.

Work in the order below. A later phase that lands before an earlier one will re-create competing controllers on the same wheels / same `front_m` / same task flag.

---

## Hardware constraint — camera is fixed (now)

The CSI camera is **bolted to the chassis**. There is **no pan/tilt servo** on this robot today (I2C 0x40 / PCA9685 is absent). A servo may be added later; until then the software must not pretend the optical axis can move.

| Now | Later (optional) |
|---|---|
| Camera optical axis = chassis forward, always | PCA9685 pan/tilt, semantic look/scan |
| `camera_pan` / `camera_tilt` stay `0` | Real pose from servo |
| `camera.look_*` is a no-op (honest error / note) | Servo presets |
| `camera.scan_environment` = still FOV survey, or in-place **wheel** turns | Servo sweep + halt wheels |
| Perception always publishes chassis `front_m` | Skip / rotate depth when `|pan|` large |

**Critical:** the current `SimulatedServo` still mutates `world.robot.camera_pan` when tools call `look_named`. If perception later skips obstacle updates when pan ≠ 0, a **fake software pan on a fixed camera would starve safety of depth**. That must not happen.

Keep the `ServoController` interface. Force a **fixed** backend until `servo.enabled: true` and the device is actually on the bus.

---

## Ground rules

1. LLM never PWM. Tools stay semantic. E-stop never waits on network.
2. Perception owns chassis `front_m` / `blocked`. Safety owns stop. Planner owns drive mode. Agent owns intent. TaskManager owns lifecycle.
3. Pad/manual must not look like a cognitive task.
4. Tests: extend `tests/test_core.py` (stdlib `unittest`, no hardware, no network). Run `python3 tests/test_core.py` after each phase.
5. Python 3.6 only. No f-strings, dataclasses, typing generics, new deps.
6. After each phase, update `docs/architecture.md` only if ownership or data flow actually changed.
7. **Fixed camera:** do not write I2C servo, do not change pan/tilt in WorldState, do not skip body-frame depth because a tool “looked left”.

---

## Ownership (do not blur)

| Resource | Owner | Callers may request, not write |
|---|---|---|
| PWM / `set_speeds` | `SafetyController` | Planner via `request()`, e-stop via `motors.stop()` |
| Drive mode + timeout | `LocalPlanner` | Tools via `set_mode` / `halt` |
| Chassis `front_m` / `blocked` | Perception (camera = body frame **now**) | Safety, planner (read) |
| Task SM | `TaskManager` via Agent | Tools report actions only |
| Camera pose | **Fixed at (0,0)** until `servo.enabled` | Tools must not fake pan; no off-axis skip |
| Intent | Agent: keywords → TypeSafe → LLM | TypeSafe never on the hot path |
| Spatial landmarks | `SpatialMemory` (phase 7) or docs-only until then | Do not pretend `nav.go_to` is metric |

---

## Phase 0 — Test harness for the broken contracts

**Why first.** Current tests never assert “a dark blob can produce `front_m ≤ min_obstacle_m`”, never expire explore, never check keyword-before-TypeSafe. Fixing code without these tests will regress.

**Files**

- `tests/test_core.py` (extend; do not split unless a file exceeds ~500 lines)

**Add tests (they should fail until the matching phase lands)**

| Test | Asserts | Unlocks phase |
|---|---|---|
| `test_blocked_blob_reaches_stop_distance` | geometric estimator on a near-field blob → `front_m ≤ blocked_m` and `blocked is True` | 1 |
| `test_open_floor_not_blocked` | bright empty floor → `blocked is False`, `front_m` well above stop | 1 |
| `test_safety_stops_when_world_blocked` | `WorldState` with that `front_m` → `SafetyController.request(forward)` returns `obstacle` | 1 |
| `test_explore_expires` | `set_mode("explore", duration_s=0.05)` then sleep → mode `idle` | 2 |
| `test_follow_expires` | same for `follow` | 2 |
| `test_scan_halts_planner` | tool scan starts → planner mode is stop/idle (or `scan` wheel survey), never corridor-follow | 2 |
| `test_look_on_fixed_camera_does_not_change_pan` | `camera.look_left` leaves `camera_pan=0` when servo disabled | 2 |
| `test_scan_does_not_fake_pan` | scan worker does not write non-zero pan/tilt | 2 |
| `test_fixed_camera_always_updates_obstacles` | perception still calls `update_obstacles` after a look tool | 2 |
| `test_pad_does_not_enter_executing` | `ensure_executing` / pad path leaves state IDLE or only notes action | 3 |
| `test_blocked_does_not_submit_immediately` | first `NAVIGATION_BLOCKED` does not enqueue LLM replan | 3 |
| `test_keywords_before_typesafe` | mock TypeSafe that would hang; `"go forward"` never calls it | 4 |
| `test_estop_skips_pending_tools` | set estop during `_handle`; no `dispatch` after return | 4 |
| `test_safety_reads_obstacles_without_full_snapshot` | new cheap accessor used by `validate` | 5 |
| `test_status_payload_one_world_snapshot` | health path does not call `world.snapshot()` again | 5 |
| `test_frame_buffer_put_copies` | mutate source after `put`; `get` is unchanged | 6 |
| `test_estop_beats_inflight_apply` | stop then concurrent `_loop` tick cannot write non-zero speeds | 6 |

**Done when:** tests exist and fail for the right reason (not import errors).

---

## Phase 1 — Make the obstacle pipeline real (audit A)

**Goal.** A near obstacle can produce `blocked=True` and `front_m ≤ safety.min_obstacle_m`. Safety hard-stops. Planner recovery can fire.

Because the camera is fixed, this phase is the entire obstacle story: every RGB frame **is** chassis-forward. No pan compensation.

**Files**

- `jetbot_core/perception/depth.py`
- `jetbot_core/perception/pipeline.py`
- `jetbot_core/world.py` (single blocked predicate)
- `jetbot_core/safety.py` (read WorldState `blocked` / `front_m`, do not re-invent)
- `jetbot_core/navigation.py` (read the same predicate)
- `config.yaml` / `jetbot_core/config.py` only if a new `perception.min_m` is needed; prefer wiring existing `safety.min_obstacle_m`

**Implementation**

1. `GeometricDepthEstimator`
   - Set `min_m` to `0.0` or `≤ blocked_m` (recommend `0.05`).
   - Map occupancy so `occ ≈ 1` → `front_m ≈ min_m`, `occ ≈ 0` → `max_m`.
   - `blocked = front_m <= blocked_m` must be reachable from a synthetic / real near blob.
   - Keep `blocked_m` injected from `safety.min_obstacle_m` (pipeline already does this). Also pass `min_m`.
2. `WorldState.update_obstacles`
   - Compute and store `blocked` in one place: `front_m is not None and front_m <= stop_m`.
   - Optional: store `stop_m` on WorldState at init from config so planner/safety do not each pick a different constant.
3. `SafetyController.validate`
   - Forward stop: `obs["blocked"]` **or** `front_m <= min_obstacle_m`.
   - Do not require a second independent formula.
4. `LocalPlanner._compute`
   - `close` uses `obs["blocked"]` (and `front_m` only as fallback).
   - Use or delete `blocked_bins_center`; do not leave a dead threshold that disagrees with WorldState.

**Do not**

- Change camera, agent, or tools in this phase.
- Add a neural depth model.
- Gate depth on `camera_pan` (always 0, and must stay 0).

**Verify**

- `GeometricDepthTests.test_open_vs_blocked` plus new stop-distance tests pass.
- Manual: synthetic camera orange block → UI shows BLOCKED and `front` ≤ 0.18 m.

**Done when:** recovery and safety stop are reachable; slow zone still works for `min_obstacle_m < front ≤ slow_obstacle_m`.

---

## Phase 2 — Fixed camera + one driver at a time (audit B, rewritten)

**Goal.** Explore/follow time out. “Look” / “scan” do **not** fake a gimbal. Depth keeps updating. If the user asks to look around, the **chassis** turns (planner), not a missing servo.

**Files**

- `config.yaml`, `jetbot_core/config.py`
- `jetbot_core/hardware/servo.py`
- `jetbot_core/runtime.py` (health: `servo: disabled/fixed`)
- `jetbot_core/navigation.py`
- `jetbot_core/tools.py`
- `web/static/index.html`, `web/static/app.js` (optional: hide or relabel look pads)
- `docs/architecture.md` (camera is fixed; servo optional)

**Config**

```yaml
servo:
  enabled: false          # hardware not fitted
  # address / channels kept for a future board
```

`try_create_servo`: if `enabled` is false, **never** open I2C, return `FixedServo` (or SimulatedServo that ignores `look`). `camera_pan` / `camera_tilt` remain 0. Health detail: `fixed` / `disabled`.

When a future board exists: set `servo.enabled: true`; `try_create_servo` may probe 0x40. Only then may look() move PWM and WorldState pan/tilt.

**Implementation**

1. `FixedServo` / disabled path
   - `look(pan, tilt)` and `look_named(name)`: no-op, return `(0.0, 0.0)`.
   - `snapshot()`: `{"backend": "fixed", "pan": 0, "tilt": 0, "ok": True, "enabled": False}`.
2. WorldState
   - Telemetry loop may still copy servo snapshot; it will be zeros.
   - Tools must **not** `update_robot(camera_pan=45)` on the fixed path.
3. `LocalPlanner._compute`
   - Expire **all** timed modes, including `explore` and `follow`, when `now > _until`.
   - Persistent follow/explore must be re-asserted by the tool/agent with a new duration, not run forever.
4. Camera tools on a fixed camera
   - `camera.look_forward` / `look_up` / `look_down`: `{ok: True, camera: "fixed"}`. No motion.
   - `camera.look_left` / `look_right`: **do not pan**. Either:
     - `{ok: True, camera: "fixed", note: "no gimbal"}` (honest, preferred for pad LOOK buttons), or
     - map to a short `planner.set_mode("left"|"right", duration_s=0.4)` so “look left” is a body yaw. Pick **one** and document it. Recommendation: pad LOOK left/right = body yaw; LOOK up/down = no-op. LLM `camera.look_left` same as pad.
   - `camera.inspect`: no servo aim. If target bbox exists, optional short body yaw toward bbox `cx`; else no-op.
5. `camera.scan_environment` (fixed camera)
   - Halt first (`planner.halt()`).
   - **Do not** run `SCAN_POSES` servo dwells.
   - Two legal implementations (pick A unless motion is locked):
     - **A — body survey (default when `allow_motion`):** planner sequence `stop → left dwell → right dwell → forward`, each segment a timed mode. Camera stays body-front, so depth stays valid. Record objects after each dwell.
     - **B — still survey (when motion locked / estop):** dwell on current FOV only, write a memory note, `SCAN_COMPLETED`.
   - Scan worker never calls `servo.look_named`.
6. Perception
   - **Now:** always `update_obstacles` from the latest frame. No off-axis skip.
   - **Later (comment + config gate only):** if `servo.enabled` and `abs(pan) > limit`, skip chassis front. Do **not** implement the skip while `enabled` is false — dead code that can be wired wrong is how fake pan would break safety.
7. UI
   - Relabel LOOK LEFT/RIGHT as body turn, or keep icons but status line says `camera: fixed`.
   - SCAN button still valid (body survey or still survey).

**Do not**

- Implement camera-to-body rotation.
- Skip depth because simulated pan is non-zero.
- Probe PCA9685 on boot.

**Verify**

- Explore expires.
- `look_left` does not change pan; obstacles keep updating.
- Scan with wheels locked still completes without I2C and without fake pan.
- Scan with motion on uses body turns, not servo.

**Done when:** WorldState pan/tilt stay 0; scan cannot starve depth; explore cannot run forever.

---

## Phase 3 — Arbiter for “blocked” (audit C + I, task writers)

**Goal.** One obstacle → planner recovery only. Agent replans after recovery fails. Pad does not enter `EXECUTING`. Scan does not drive the task SM.

**Files**

- `jetbot_core/task.py`
- `jetbot_core/tools.py`
- `jetbot_core/agent.py`
- `jetbot_core/navigation.py`
- `jetbot_core/events.py` (optional `NAVIGATION_RECOVERY_FAILED`)

**Implementation**

1. `TaskManager.ensure_executing`
   - If `task_id` is None / name is pad-manual: `note_action` only, stay `IDLE`.
   - Promotion `IDLE → PLANNING → EXECUTING` only when Agent `start()`ed a named goal.
2. Pad tools (`_forward`, `_left`, … from UI) call `note_action`, not `ensure_executing`.
   - LLM/agent tools for a live task may still `ensure_executing`.
3. `Agent._on_blocked`
   - If no named task (`IDLE` / pad): return.
   - If named task: `transition(REPLANNING)` **without** `submit()` on the first event.
   - Start a recovery timer (e.g. 2.0 s) or subscribe to a new `NAVIGATION_RECOVERY_FAILED`.
4. `LocalPlanner`
   - After emitting `NAVIGATION_BLOCKED`, recover for `recovery_s` (config, default 2.0).
   - If still `close` after that, emit `NAVIGATION_RECOVERY_FAILED` once; then Agent may `submit(replan)`.
   - If recovered, emit existing `NAVIGATION_RECOVERED`; Agent must not replan.
5. While `mode` is recovery (or a dedicated `recovery` mode):
   - `ToolRouter` motion tools except `motion.stop` return `{"ok": False, "error": "recovery_active"}`.
   - Agent `_handle` skips motion tools if planner is in recovery.
   - Body-survey scan must not interrupt recovery; wait or cancel scan.
6. `ToolRouter._scan_worker`
   - Emit `SCAN_REQUESTED` / `SCAN_COMPLETED` only.
   - Do **not** `tasks.transition(...)`. Agent may move `ACTIVE_PERCEPTION → OBSERVING` on `SCAN_COMPLETED`.

**Verify**

- Pad forward + synthetic block: recovery turns, no LLM queue, task stays IDLE.
- Named “explore the room” + block: recovery first; replan only after timeout.

**Done when:** three reactions no longer run in parallel; task pill matches Agent-owned lifecycle.

---

## Phase 4 — Unblock the slow loop (audit D)

**Goal.** Cheap intents never hit OpenRouter. E-stop aborts in-flight plans. Late LLM plans cannot start a scan or body-survey.

**Files**

- `jetbot_core/agent.py`
- `jetbot_core/tools.py` (estop guard on `_scan` / `_look`)
- `jetbot_core/runtime.py` (pass `safety` into Agent if not already reachable)
- `jetbot_core/llm.py` (`SYSTEM_PROMPT`): tell the model the camera is **fixed**; `camera.look_*` does not aim a gimbal; `scan` is a still or in-place wheel survey. Prefer `motion.left` / `motion.right` to face a new direction.

**Implementation**

1. `_local_intent`
   - Keyword map **first**.
   - TypeSafe only if no keyword match **and** client enabled **and** key present.
   - Keep `_needs_llm` / `source=="nav"` bypass.
2. Abort flag
   - `_on_estop`: clear `_pending`, set `self._abort = True`.
   - `_handle`: after TypeSafe, after LLM, before **each** `dispatch`, if `safety.estop` or `_abort`: stop, no tools.
   - Reset `_abort` at start of a new user command only, not at start of a leftover handle.
3. `ToolRouter.dispatch` / `_scan`
   - If `world.robot.estop`: refuse motion and scan except halt.
4. Do not raise TypeSafe timeout; fail-open to keywords/LLM as today, just not first.

**Verify**

- `"go forward"` with a TypeSafe mock that sleeps 8 s returns immediately via keywords.
- E-stop during `llm.plan` mock: no `camera.scan_environment` after return.

**Done when:** command bar is snappy for stop/move/scan; estop cannot be undone by a late LLM plan.

---

## Phase 5 — Cheap WorldState reads (audit E + J status)

**Goal.** Safety 20 Hz and planner 10 Hz do not deepcopy the world. HTTP status takes one snapshot.

**Files**

- `jetbot_core/world.py`
- `jetbot_core/safety.py`
- `jetbot_core/navigation.py`
- `jetbot_core/health.py`
- `jetbot_core/runtime.py`
- `web/static/app.js` (poll interval)

**Implementation**

1. `WorldState`
   - `obstacles_view()` → shallow copy of `obstacles` + freshness of depth only, under lock, no deepcopy of objects/health/task.
   - Keep `snapshot()` for UI / LLM summary.
2. `SafetyController.validate` and `_loop`: use `obstacles_view()`.
3. `LocalPlanner._compute`: use `obstacles_view()` plus a small `navigation_view()` / robot estop flag. Full snapshot only for `follow` (needs objects); even then copy `objects` dict without deepcopy of robot/health.
4. `HealthMonitor.snapshot(host_metrics=None)`
   - Read `world.health` under lock (the dict already there).
   - Do **not** call `world.snapshot()`.
   - `host_telemetry()` once; runtime status passes it in.
5. `JetBotRuntime.status_payload`
   - One `world.snapshot()`.
   - `health.snapshot()` without a second world snapshot.
   - Drop duplicate `task` inside `agent.snapshot()` **or** stop embedding it (Agent snapshot should not re-copy TaskManager if status already has `task`).
6. UI: `setInterval(tick, 700)` → `1000` or `1500`. MJPEG stays the high-rate path.
7. UI camera pose line: show `fixed` when servo disabled, not fake degrees.

**Verify**

- Unit test: monkeypatch `copy.deepcopy` counter; safety validate does not increment it.
- Console still renders health/objects.

**Done when:** fast loop no longer contends with UI deepcopy; nested snapshot gone.

---

## Phase 6 — Frame stability and e-stop TOCTOU (audit F, G)

**Goal.** Latest-frame slot holds a stable image. E-stop cannot lose a race to the 20 Hz apply.

**Files**

- `jetbot_core/hardware/camera.py`
- `jetbot_core/safety.py`
- `jetbot_core/hardware/motors.py` (optional belt-and-suspenders)

**Implementation**

1. `LatestFrameBuffer.put`
   - Store `frame.copy()` if `frame` is an ndarray (or copy in `_BaseCapture._loop` before `put`, once).
   - Readers may keep `copy=True` for overlay mutation; producer copy is mandatory.
2. `SafetyController._loop`
   - After computing `left, right`, re-acquire `_lock`, re-read `_estop` and `allow_motion`, then write motors.
   - Put `allow_motion` under `_lock` in `set_allow_motion` / `request`.
3. Optional: `MotorController.set_speeds` no-ops if a `safety_gate` callable returns False. Prefer the re-check in Safety first so SimulatedMotors tests stay simple.

**Verify**

- Mutate source array after `put`; buffer frame unchanged.
- Thread test: loop about to `set_speeds(0.2,0.2)` vs `emergency_stop()` → motors end at 0.

**Done when:** no torn frames; estop wins the in-flight tick.

---

## Phase 7 — Honest navigation / spatial (audit H) and leftover J items

**Goal.** Stop advertising metric `nav.go_to`. Either wire `SpatialMemory` as a thin landmark store or document it as unused. Fuse detection with depth only as far as Nano allows.

**Files**

- `jetbot_core/runtime.py`
- `jetbot_core/spatial.py`
- `jetbot_core/tools.py` (`_go_to`)
- `jetbot_core/perception/pipeline.py`
- `jetbot_core/world.py` (`TrackedObject.position`)
- `docs/architecture.md`
- `config.yaml` (`servo.enabled`, `navigation.recovery_s` if added in phase 3)

**Implementation (keep small)**

1. Instantiate `SpatialMemory` on the runtime **or** remove it from the architecture table. Prefer instantiate:
   - On `SCAN_COMPLETED` / new object: `remember_landmark(id, class, None, confidence)`.
   - Agent LLM summary: one line from `spatial.summary()` if landmarks exist.
2. `nav.go_to`
   - If `target` is a live WorldState object: `set_mode("follow", duration_s=..., target=target)`.
   - Else: still/body `camera.scan_environment` and return `{"ok": False, "error": "unknown_target"}` — do not silently explore forever.
3. Optional fusion: for each track bbox, sample the depth bin covering `cx`; set `position` to `(None, None, front_m_bin)` robot-relative z only. Skip if this costs more than one extra slice of the already-computed bins. Valid **now** because camera = body frame.
4. I2C lock: **motors only** while servo is disabled. Do not add a servo+motor bus lock until `servo.enabled` is true. Simulated servo needs no lock.
5. Whisper: leave on threaded HTTP (acceptable). Optionally cap `timeout_s` in config to 45 if UI feels wedged; not a pipeline conflict.
6. Command latch: in `_loop`, expire on `duration_s` **or** `command_timeout_s`, whichever is **smaller** for non-zero cmds (`min` not `max`), so a 0.2 s turn is not held 1.2 s if the planner stalls.
7. Architecture doc: camera path is fixed RGB; “active perception” = body yaw + still survey, not gimbal. Servo section marked **optional / not fitted**.

**Verify**

- `nav.go_to` unknown target does not leave explore latched.
- Architecture doc matches runtime (spatial present or struck; servo disabled).

**Done when:** no dead module in the ownership table; go_to cannot become infinite wander; docs do not describe a gimbal this robot does not have.

---

## Future: fitting a pan/tilt board

Do this **only** when hardware is on I2C 0x40 (or the real address):

1. Set `servo.enabled: true`.
2. Confirm `try_create_servo` returns `pca9685`, not `fixed`.
3. Then, and only then:
   - Restore `look_named` PWM + WorldState pan/tilt.
   - Scan = halt wheels + servo `SCAN_POSES` (old tool sequence).
   - Perception skip/rotate chassis `front_m` when `|pan|` or `|tilt|` exceeds limits.
   - Add motor+servo I2C lock.
4. Add tests that fake `enabled=True` with a mock servo; do not enable on the floor until those pass.

Do not leave `enabled: true` with SimulatedServo pretending to pan — that is the failure mode this plan exists to prevent.

---

## Suggested file touch map

| Phase | Primary files | Tests |
|---|---|---|
| 0 | `tests/test_core.py` | new failing tests |
| 1 | `perception/depth.py`, `perception/pipeline.py`, `world.py`, `safety.py`, `navigation.py` | depth + safety stop |
| 2 | `config.py`, `hardware/servo.py`, `navigation.py`, `tools.py`, `runtime.py`, UI | expire + fixed camera + scan halt |
| 3 | `task.py`, `tools.py`, `agent.py`, `navigation.py`, `events.py` | pad IDLE, delayed replan |
| 4 | `agent.py`, `tools.py`, `llm.py` | keywords first, estop abort, prompt = fixed cam |
| 5 | `world.py`, `safety.py`, `navigation.py`, `health.py`, `runtime.py`, `web/static/app.js` | no deepcopy on fast path |
| 6 | `hardware/camera.py`, `safety.py` | copy-on-put, estop race |
| 7 | `runtime.py`, `spatial.py`, `tools.py`, `docs/architecture.md` | go_to honesty, docs = no gimbal |

---

## Out of scope (do not do in this upgrade)

- TensorRT / MiDaS / jetson-inference install
- ROS2 / Nav2 / metric SLAM / pose integration from wheel odometry (no encoders)
- Buying / wiring / calibrating a pan/tilt (hardware not present)
- Implementing off-axis depth skip “just in case”
- Rewriting the web stack
- Making TypeSafe a second brain
- Storing frames in WorldState or A-MEM
- Raising capture resolution or adding a second detector while geometric depth runs

---

## Acceptance (all phases)

1. Near obstacle: `blocked=True`, forward command rejected, recovery turn if a named nav mode is active.
2. Explore/follow stop when duration elapses.
3. Camera pose in WorldState stays `(pan=0, tilt=0)`. Look tools do not fake a gimbal.
4. Scan: wheels halted or a **body** survey; depth keeps updating from the fixed camera.
5. Pad motion never starts an LLM replan.
6. `"stop"` / `"forward"` / `"scan"` never wait on OpenRouter.
7. E-stop zeros motors and prevents late tool dispatch (including scan / body survey).
8. `/api/status` at ~1 Hz does not starve the 20 Hz safety loop (spot-check: safety thread period stays ~50 ms under UI load).
9. MJPEG frames are not torn (visual + copy test).
10. Boot does not open `/dev/i2c-*` for a servo. Health shows servo `fixed` / `disabled`.
11. `python3 tests/test_core.py` green on the Nano without hardware.

---

## Rollout

Implement on the Nano tree in `/home/jetbot/CYJ`. After each phase:

```
cd /home/jetbot/CYJ
python3 tests/test_core.py
```

Do not enable `JETBOT_ALLOW_MOTION=1` until phase 1 and 6 tests pass. Phase 2–3 should land before any unattended explore on the floor. Leave `servo.enabled: false` in `config.yaml` until a real pan/tilt is fitted.
