# JetBot Cognitive Architecture

Python 3.6 process on Jetson Nano 4GB. The LLM is not the realtime controller.

## Loops

```
FAST  (~20 Hz)   perception freshness + obstacle distance → SafetyController → motors
MEDIUM (~10 Hz)  latest RGB → depth heuristic + detect/track → WorldState → LocalPlanner
SLOW   (event)   user/voice/nav-blocked → Agent → LLM/TypeSafe → semantic tools
```

Emergency stop: `SafetyController.emergency_stop()` → `motors.stop()`. No LLM, no HTTP, no memory.

## Components

| Piece | Module | Responsibility |
|---|---|---|
| Camera | `hardware/camera.py` | Gst/V4L2/synthetic capture thread → `LatestFrameBuffer` |
| Perception | `perception/pipeline.py` | Latest-frame consumer; overlay for UI only |
| World State | `world.py` | Operational truth: pose, obstacles, tracks, freshness |
| Spatial | `spatial.py` | Landmarks / explored poses |
| A-MEM-like | `memory.py` | Semantic notes, links, lexical retrieval |
| Safety | `safety.py` | Clamp, stale-sensor, obstacle, e-stop, watchdog |
| Navigation | `navigation.py` | Corridor follow, recovery turn, follow-bbox |
| Task | `task.py` | IDLE→PLANNING→EXECUTING→… explicit transitions |
| Agent | `agent.py` | Orchestration; summaries only |
| Tools | `tools.py` | Semantic camera/motion/memory; never PWM |
| LLM | `llm.py` | `192.168.20.150:8008/v1` chat completions |
| Whisper | `whisper_client.py` | `192.168.20.150:8003/inference` |
| TypeSafe | `typesafe_client.py` | Optional intent `choice` + confirm `noul` |
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
`safety.min_obstacle_m` zeros forward commands.

## Task lifecycle

`IDLE → PLANNING → EXECUTING → OBSERVING → VERIFYING → COMPLETED`

Recovery: `EXECUTING → REPLANNING → EXECUTING`  
Uncertainty: `EXECUTING → ACTIVE_PERCEPTION → PLANNING`

## LLM contract

The local llama.cpp server advertises **n_ctx = 2048**. Prompts are a world
summary + a few memory lines + the user instruction. The model must return a
JSON tool plan. Local keyword/TypeSafe intents skip the LLM for stop/move/scan.

## Hardware backends

On this Nano at audit time, I2C bus 1 had no MotorHAT (0x60) or PCA9685 (0x40).
`simulate_if_missing: true` keeps motors/servo simulated so the rest of the
stack is testable. When the HAT appears, the same interfaces drive it.
---
