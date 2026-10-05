# JetBot Pipeline Audit

**Date:** 2026-10-05  
**Scope:** `/home/jetbot/CYJ` end-to-end, not isolated files  
**Constraint:** diagnosis only; no code changes in this pass

The intended stack is one process, three loops, one motor owner. The code mostly follows that shape. The failures are interface mismatches and dual writers, not missing modules.

```
Camera thread ──► LatestFrameBuffer
                      ├─► MJPEG (overlay, else raw)
                      └─► Perception worker
                             ├ depth heuristic  ─┐
                             ├ detect + IoU     ─┼─► WorldState
                             └ overlay          ─┘     │
                                                       ├─► Safety 20Hz ──► motors
                                                       ├─► Planner 10Hz ──► Safety.request
                                                       └─► Agent 4Hz ──► Tools ──► Planner / servo
HTTP / Whisper / TypeSafe / LLM ──► Agent queue
```

---

## 1. Critical conflicts / bottlenecks

### A. Obstacle pipeline is dead (safety never hard-stops)

Geometric depth **cannot** report `front_m ≤ 0.18 m`, which is the stop threshold everywhere else.

| Layer | Threshold | Actual `front_m` |
|---|---|---|
| Depth | `min_m=0.22` floor | always `∈ [0.22, 2.2]` |
| Depth `blocked` | `front_m ≤ 0.18` | **never true** |
| Planner recovery | `blocked` or `front < 0.18` | **never true** |
| Safety stop | `front ≤ min_obstacle_m (0.18)` | **never true** |
| Safety slow | `front ≤ 0.36` | only this still works |

**Files / functions**

- `jetbot_core/perception/depth.py` — `GeometricDepthEstimator.__init__` / `estimate`
- `jetbot_core/perception/pipeline.py` — `PerceptionPipeline.__init__` (passes `blocked_m=min_obstacle_m`, never `min_m`)
- `jetbot_core/safety.py` — `SafetyController.validate`
- `jetbot_core/navigation.py` — `LocalPlanner._compute` (`close`)

**Why they conflict**

Three loops share one number (`obstacles.front_m` / `blocked`) but the producer floors it above the consumers’ stop line. Recovery, `OBSTACLE_DETECTED`, and e-stop-on-contact never fire. The robot can only slow, not stop.

**Owner**

Perception owns the metric. Safety owns the stop. They must share one reachable scale.

**Minimal fix**

Set `min_m=0` (or `≤ blocked_m`) in `GeometricDepthEstimator`, or map occupancy so `occ=1 → 0.05 m`. Pass `min_m` from `safety.min_obstacle_m`. Keep a single blocked predicate in WorldState; planner/safety should read that, not re-derive it.

---

### B. Explore / follow never yield; scan drives while wheels move

```python
# jetbot_core/navigation.py
if mode != "idle" and now > until and mode not in ("explore", "follow"):
    with self._lock:
        self.mode = "idle"
```

`motion.explore`, `nav.go_to` (aliased to explore), and `nav.follow` run until something else calls `halt()`. Tool durations (2.5–4 s) are stored and ignored.

`camera.scan_environment` does **not** halt the planner. Scan worker pans the camera while the corridor follower still treats the image as **robot-front**.

**Files / functions**

- `LocalPlanner._compute` / `set_mode`
- `ToolRouter._explore`, `_go_to`, `_follow`, `_scan` / `_scan_worker`

**Why they conflict**

Nav, active perception, and safety all interpret “front” as chassis-forward. After a look-left, depth/occupancy is camera-frame. Planner may recover-spin (if A were fixed) or slow; scan still slews the servo. Two actuators, one world axis.

**Owner**

`LocalPlanner` owns drive mode and its timeout. `ToolRouter` may request scan only after `planner.halt()` (or a `scan` mode that zeros wheels). Depth must be camera-relative and **not** written as chassis `front_m` unless pan/tilt ≈ 0.

**Minimal fix**

1. Expire `explore`/`follow` on `_until` (same as `forward`).
2. `_scan` / `_look` → `planner.halt()` first.
3. Skip `world.update_obstacles` when `|pan|` or `|tilt|` exceeds a few degrees, or rotate bins into body frame.

---

### C. Three uncoordinated reactions to “blocked”

If A is fixed, one obstacle would simultaneously:

1. Perception emit `OBSTACLE_DETECTED`
2. Planner emit `NAVIGATION_BLOCKED` and **recovery-turn**
3. Agent `_on_blocked` queue an LLM replan (`source="nav"`) while recovery is already turning
4. LLM later dispatch `camera.scan` / `motion.*` on top of recovery

Pad “forward” also calls `tasks.ensure_executing`, so a **manual** bump can start a cognitive replan.

**Files / functions**

- `PerceptionPipeline._loop` (obstacle events)
- `LocalPlanner._compute` (recovery)
- `Agent._on_blocked` / `_handle`
- `TaskManager.ensure_executing`

**Why they conflict**

Recovery is the medium-loop response. LLM replan is the slow loop. Both run. No arbiter. Task state is used as the trigger even for pad motion.

**Owner**

Planner owns local recovery. Agent should replan only for **named tasks**, after recovery fails (timeout / still blocked), not on the first `NAVIGATION_BLOCKED`. Pad/manual must not enter `EXECUTING`.

**Minimal fix**

- `ensure_executing` only if a real task exists; pad uses `note_action`.
- `_on_blocked`: do not `submit()` immediately; wait N recovery seconds or a `recovery_failed` event.
- While `mode == "recovery"`, ignore new motion tools except `stop` / estop.

---

### D. Agent slow-loop blocks itself (TypeSafe then LLM)

Every command-bar / whisper utterance hits `_local_intent` **before** keywords:

```python
# jetbot_core/agent.py
if self.typesafe is not None:
    ts = self.typesafe.classify_intent(text)
```

That is a synchronous OpenRouter POST, timeout **8 s**. Then `llm.plan` can block **30 s**. `_busy` holds the queue the whole time. E-stop clears `_pending` but **does not abort** `_handle`; after the HTTP call returns, tools still dispatch (motion is rejected, **scan/servo are not**).

**Files / functions**

- `Agent._local_intent`, `_handle`, `_on_estop`
- `TypeSafeClient.classify_intent`
- `LLMClient.plan`
- `ToolRouter._scan_worker`

**Why they conflict**

TypeSafe was specified as optional, fail-open, not on the hot path. It currently precedes the cheap matcher. The agent thread is a single blocking pipeline, not an event loop.

**Owner**

Agent owns intent. Keywords first; TypeSafe only on unknown text. LLM never on the servo/motor path. E-stop must set an abort flag that `_handle` checks before `dispatch`.

**Minimal fix**

Keyword map first. TypeSafe only if no match. Check `safety.estop` after every blocking call; skip tools if set. Do not start scans after estop.

---

### E. WorldState lock + deepcopy is the shared-memory bottleneck

`WorldState.snapshot()` holds `RLock` and `deepcopy`s the whole tree. Callers:

- Safety `validate` on **every `request` and every 20 Hz tick**
- Planner 10 Hz
- HTTP `/api/status` every **700 ms**, which then calls `health.snapshot()` → **another full snapshot**
- Agent, scan worker, `/api/world`

Perception writes (`update_obstacles`, `upsert_object`) stall behind UI snapshots. Safety’s 20 Hz loop can miss its period on a Nano.

**Files / functions**

- `WorldState.snapshot`
- `SafetyController.validate` / `_loop`
- `JetBotRuntime.status_payload`
- `HealthMonitor.snapshot` (nested snapshot)
- `web/static/app.js` `setInterval(tick, 700)`

**Owner**

WorldState owns truth. Readers should get a cheap consistent view; HTTP should not force the fast loop to copy health/objects/task.

**Minimal fix**

- Safety/planner: read `obstacles` + `freshness` under lock without deepcopy.
- `status_payload`: one snapshot; `health.snapshot()` must not snapshot the world again.
- UI: 2–3 Hz status is enough; MJPEG is already the high-rate path.

---

### F. Capture buffer stores OpenCV’s live Mat (frame tear / stale overlay)

```python
# hardware/camera.py _loop
frame = self._grab()          # cap.read() reuses internal buffer
self.buffer.put(frame, t0)    # stores reference, no copy
```

Perception/MJPEG `get(copy=True)` copy **later**. Next `read()` can overwrite the ndarray still sitting in the slot → torn frames, or “latest” that mutates under the copy.

**Files**

- `hardware/camera.py` — `_BaseCapture._loop`, `LatestFrameBuffer.put`

**Owner**

Camera producer. Latest-frame semantics require the slot to hold a **stable** image.

**Minimal fix**

`put()` copies, or `_grab()` copies before `put`. One copy at the producer is enough.

---

### G. E-stop vs 20 Hz apply (TOCTOU)

```python
# jetbot_core/safety.py
with self._lock:
    estop = self._estop
    cmd = self._command
if estop or not self.allow_motion:
    ...
else:
    ...
    self.motors.set_speeds(left, right)
```

`emergency_stop()` calls `motors.stop()`, then an in-flight loop iteration that already sampled `estop=False` writes speeds again. `allow_motion` is not under `_lock`.

**Owner**

SafetyController exclusively. `set_speeds` must refuse if estop/lock is set (check inside the motor lock, or re-read flags immediately before write).

**Minimal fix**

Re-check `_estop` / `allow_motion` under lock just before `set_speeds`. Motor controller can also no-op if a process-wide estop flag is set.

---

### H. Spatial layer is documented but not in the process

`spatial.py` is never constructed. `nav.go_to` sets explore + a name the corridor follower never uses. Pose stays `(0,0,0)`. Architecture priority `sensor → World → spatial map → semantic memory` is World + JSON notes only.

**Files**

- `spatial.py` (dead)
- `runtime.py` `__init__`
- `ToolRouter._go_to`
- `WorldState.robot.pose`

**Owner**

Either instantiate `SpatialMemory` and have perception/tools write landmarks, or stop advertising `nav.go_to` as navigation. Until pose exists, `nav.go_to` should be `follow` if the target is a live track, else `scan` + fail.

---

### I. Task lifecycle has too many writers

Legal SM is real; almost every transition is wrapped in `except ValueError: pass`.

Writers: `Agent._handle`, `Agent._on_blocked`, `Agent._on_estop`, `ToolRouter` (pad + LLM tools), `ToolRouter._scan_worker`.

Result: pad-forward leaves `EXECUTING` forever; scan and agent fight `ACTIVE_PERCEPTION` / `OBSERVING` / `EXECUTING`; UI task pill lies.

**Owner**

`TaskManager` only. Tools report actions; Agent/TaskManager transition. Scan worker should emit `SCAN_COMPLETED` and not call `transition`.

---

### J. Secondary (keep, but don’t ignore)

| Issue | Where | Note |
|---|---|---|
| I2C unsynchronized | `motors.set_speeds` 20 Hz vs `servo.look` on scan thread | Same `/dev/i2c-1`, no bus lock |
| Whisper 90 s on HTTP thread | `web.py` `_whisper` | Threaded server survives; one worker stuck |
| `blocked_bins_center` unused | `LocalPlanner` | Dead config vs `obs.blocked` |
| Detection not fused with depth | `pipeline.py` | `TrackedObject.position` always `None`; follow is bbox-only |
| Command latch lifetime | `safety._loop` `max(duration_s, command_timeout_s)` | 0.2 s cmd held 1.2 s; masked because planner ticks at 10 Hz |
| Dual health copies | `status_payload` vs `agent.snapshot()["task"]` | Duplicate task blob |

---

## 2. What is actually sound

- LLM never PWM; tools are semantic; e-stop HTTP path does not wait on LLM.
- Capture thread does not run inference (unlike stock `wander.py`).
- Planner → `Safety.request` → motors is the only drive path (plus e-stop `motors.stop()`).
- Memory writes only on meaningful reasons; no frames in World/A-MEM.
- MJPEG prefers overlay, falls back to raw — stream ≠ inference, aside from F.

---

## 3. Who should own what

| Resource | Owner | Must not |
|---|---|---|
| PWM / `set_speeds` | `SafetyController` | Agent, tools, HTTP, planner |
| Drive mode + timeout | `LocalPlanner` | Scan worker, LLM |
| Chassis `front_m` / `blocked` | Perception, body frame only | Servo-off-axis frames |
| Task SM | `TaskManager` via Agent | Pad tools, scan thread |
| Servo pose | Camera tools, after wheels halted | Parallel explore |
| Intent | Agent: keywords → optional TypeSafe → LLM | TypeSafe on every string |
| Spatial landmarks | `SpatialMemory` (or delete from docs) | Pretend `nav.go_to` is metric |

---

## 4. Minimal fix order (no redesign)

1. **Depth scale** so `blocked` / `min_obstacle_m` are reachable — this is the safety bug.
2. **Expire explore/follow**; **halt before scan**; don’t publish body-front depth while panned.
3. **Stop pad from entering EXECUTING**; debounce Agent replan vs recovery.
4. **Keywords before TypeSafe**; abort tools if estop.
5. **Cheap obstacle read for safety 20 Hz**; one snapshot per HTTP status.
6. **Copy on camera `put`**.
7. **Re-check estop immediately before `set_speeds`**.

Do not add a second planner, ROS, or another perception thread until 1–3 are fixed: those are competing controllers on the same wheels, same “front”, and same task flag.
