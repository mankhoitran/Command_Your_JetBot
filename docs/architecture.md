# JetBot Cognitive Architecture

Python 3.6 process on Jetson Nano 4GB. The LLM is not the realtime controller.

## Loops

```
FAST  (~20 Hz)   perception freshness + obstacle distance → SafetyController → motors
MEDIUM (~10 Hz)  latest RGB → depth heuristic + detect/track → WorldState → LocalPlanner
SLOW   (event)   user/voice/recovery-failed → Agent → keywords → TypeSafe → LLM
```

Emergency stop: `SafetyController.emergency_stop()` → `motors.stop()`. No LLM, no HTTP, no memory.

## Components

| Piece | Module | Responsibility |
|---|---|---|
| Camera | `hardware/camera.py` | Gst/V4L2/synthetic capture thread → `LatestFrameBuffer` |
| Perception | `perception/pipeline.py` | Latest-frame consumer; overlay for UI only |
| World State | `world.py` | Operational truth: pose, obstacles, tracks, freshness |
| Spatial | `spatial.py` | Landmarks from detections / scans |
| Servo | `hardware/servo.py` | **Disabled.** Camera is bolted forward (`servo.enabled: false`). `FixedServo` no-op until a PCA9685 is fitted. |
| A-MEM-like | `memory.py` | Semantic notes, links, lexical retrieval |
| Safety | `safety.py` | Clamp, stale-sensor, obstacle, e-stop, watchdog |
| Navigation | `navigation.py` | Corridor follow, recovery turn, follow-bbox |
| Task | `task.py` | IDLE→PLANNING→EXECUTING→… explicit transitions |
| Agent | `agent.py` | Orchestration; summaries only |
| Tools | `tools.py` | Semantic camera/motion/memory; never PWM |
| LLM | `llm.py` | `192.168.20.150:8008/v1` chat completions |
| Whisper | `whisper_client.py` | `192.168.20.150:8003/inference` |
| TypeSafe / Jev | `typesafe_client.py` | OpenRouter Decisions API, model `~typesafe/jev-latest` |
| Web | `web.py` + `web/static` | MJPEG + JSON console |

## Camera path

```
CSI/nvargus (or V4L2, or synthetic)
        │ latest-frame slot (drop stale)
        ├──────────► MJPEG / UI   (copy, jpeg)
        └──────────► Perception worker (resize 160×120)
                         ├ depth heuristic
                         ├ detect + IoU track
                         └ WorldState + events
```

Inference never sits in the capture callback.

The camera optical axis **is** chassis forward. `camera.look_left` / `look_right` yaw the body; `look_up` / `look_down` are no-ops. `camera.scan_environment` is a still FOV survey (wheels locked) or in-place wheel turns. Do not skip chassis depth based on a fake pan.

## World vs memory

Priority: **sensor → World State → spatial map → semantic memory**.

A-MEM notes are written only on meaningful events (new object, scan complete,
nav failure, user note). Raw frames are never stored.

Uncertainty labels: `KNOWN | PROBABLE | UNKNOWN | STALE | CONTRADICTED`.

## Safety boundary

```
Agent / Planner / UI
        │  MotionCommand(left, right, source, duration)
        ▼
SafetyController.validate()
        │  estop / stale / min_distance / max_speed / timeout
        ▼
MotorController.set_speeds()
```

Forward motion with stale depth is rejected. Obstacle closer than
`safety.min_obstacle_m` (0.18 m) zeros forward commands. In-place
left/right are allowed down to `safety.turn_obstacle_m` (0.06 m).
Wheels stay locked until `POST /api/motion` or the console UNLOCK
WHEELS control (`allow_motion`).

## Task lifecycle

`IDLE → PLANNING → EXECUTING → OBSERVING → VERIFYING → COMPLETED`

Recovery: `EXECUTING → REPLANNING → EXECUTING`  
Uncertainty: `EXECUTING → ACTIVE_PERCEPTION → PLANNING`

## LLM contract

The local llama.cpp server advertises **n_ctx = 2048**. Prompts are a world
summary + a few memory lines + the user instruction. The model must return a
JSON tool plan. **Keywords first**, then TypeSafe if unknown, then LLM.
Pad/manual motion does not start a cognitive task. `NAVIGATION_BLOCKED`
runs local recovery; the Agent replans only on `NAVIGATION_RECOVERY_FAILED`.

## Hardware backends

On this Nano at audit time, I2C bus 1 had no MotorHAT (0x60) or PCA9685 (0x40).
`simulate_if_missing: true` keeps motors simulated. The camera servo stays
**fixed/disabled** (`servo.enabled: false`) until a pan/tilt board is fitted;
do not enable simulated pan — that would starve safety of body-frame depth.

Geometric depth maps occupancy onto `[min_m, max_m]` with `min_m ≤ min_obstacle_m`
so a near blob can actually trip `blocked` / the hard stop.
---
