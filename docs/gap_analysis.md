# JetBot Cognitive Robotics — Architecture Gap Analysis

**Date:** 2026-10-03  
**Machine:** NVIDIA Jetson Nano 4GB (`nano-4gb-jp45`)  
**L4T / JetPack:** R32.5.0 / JetPack 4.5 (Ubuntu 18.04, Python 3.6.9)  
**Workspace inspected:** `/home/jetbot` with production target `/home/jetbot/CYJ`

This document is the Phase 1 audit required by `jetbot_construct.md`.
It records what exists, what is missing, and the implementation decisions
that follow. Code in this folder is written against these facts, not against
an assumed modern Python/ROS stack.

---

## 1. What actually exists

### 1.1 CYJ folder (before this work)

Only `jetbot_construct.md`. There is no prior Agent, Brain, Web UI, Whisper
client, or configuration in `CYJ`.

### 1.2 NVIDIA JetBot package (`/home/jetbot/jetbot`, v0.4.3)

A Jupyter-centric educational kit, not a cognitive runtime.

| Area | Location | Notes |
|---|---|---|
| Entry points | `notebooks/*`, `jetbot/apps/wander.py` | Interactive notebooks + a blocking wander demo |
| Motors | `jetbot/robot.py`, `jetbot/motor.py` | Adafruit MotorHAT I2C PWM, Waveshare INA/INB extras |
| Camera | `jetbot/camera/opencv_gst_camera.py` | nvargus GStreamer, latest-frame via traitlets `value` |
| ZMQ camera | `jetbot/camera/zmq_camera.py` | CONFLATE=1 latest-frame subscriber (good pattern) |
| Object detection | `jetbot/object_detection.py` + SSD TensorRT plugin | Requires a prebuilt `.engine` file; none present |
| Collision avoid | `notebooks/collision_avoidance`, `apps/wander.py` | AlexNet/ResNet binary blocked/free; needs PyTorch |
| Road following | `notebooks/road_following` | Regression CNN; needs PyTorch |
| Object following | `notebooks/object_following` | SSD-MobileNet + centroid steering |
| Web UI | Jupyter widgets only | No robotics console |
| Servo / pan-tilt | **not implemented** | Spec assumes a camera servo; stock JetBot has none |
| Agent / LLM | **not implemented** | |
| Whisper | **not implemented** | |
| Tests | **none** | |
| Config / env | traitlets defaults, `JETBOT_DEFAULT_CAMERA` | No YAML, no remote LLM config |

Stock camera already uses a capture thread that overwrites `self.value`
(latest-frame). Stock wander **blocks the capture callback with inference**,
which is exactly the anti-pattern the spec forbids.

### 1.3 jetcard

OLED stats (`eth0/wlan0/mem/disk`). Useful as a telemetry idea, not a UI.

### 1.4 Runtime environment

| Item | Reality |
|---|---|
| Python | **3.6.9 only** (no 3.7+, no venv, no conda) |
| pip | **not installed** |
| numpy | 1.13.3 |
| OpenCV | 4.1.1 (with GStreamer) |
| TensorRT | 7.1.3 (CUDA 10.2) |
| PyTorch | **not installed** |
| jetson-inference | **not installed** |
| Flask / FastAPI / aiohttp / pydantic | **not installed** |
| Adafruit_MotorHAT | **not installed** in system Python |
| pytest | **not installed** (use stdlib `unittest`) |
| RAM | 3.9G total, ~1.3G available at audit time |
| Disk | 31G free |
| Camera device | `/dev/video0` present, nvargus device-tree present |
| Motor HAT | I2C bus 1 **empty** (no 0x60) |
| PCA9685 servo | I2C **not present** (no 0x40) |
| Network | `wlan0` 192.168.16.58; Tailscale 100.90.21.76 |

### 1.5 Remote services (verified reachable)

| Service | URL | Observed |
|---|---|---|
| LLM | `http://192.168.20.150:8008/v1` | OpenAI-compatible. Model `gemma-4-E4B-it-Q4_K_M`. **n_ctx = 2048**. |
| Whisper | `http://192.168.20.150:8003` | whisper.cpp HTTP server, `/inference` multipart |

These endpoints match the spec. They must be preserved.

---

## 2. Target vs current — gap table

| Spec subsystem | Current | Gap | Decision |
|---|---|---|---|
| Hardware abstraction | Concrete MotorHAT + Gst camera | No interfaces, no servo, HAT missing | New interfaces in `jetbot_core.hardware`; simulate if bus empty |
| Event system | traitlets observers | Untyped, Jupyter-bound | Typed events + in-process bus |
| World State | none | — | Fast in-memory world model, **no images** |
| Spatial memory | none | — | Lightweight object/area store |
| A-MEM | none | ChromaDB/sentence-transformers are too heavy for Nano + Py3.6 | A-MEM *inspired* JSON notes + lexical retrieval |
| Task lifecycle | none | wander is a while-loop | Explicit state machine |
| Agent / LLM | none | — | Slow loop, JSON tool plans, short summaries only |
| TypeSafe | none | Cloud API, no key on device | Optional client; disabled unless `TYPESAFE_API_KEY` |
| Whisper | none | — | External client to :8003, not part of reasoning |
| Safety controller | none (wander drives motors from softmax) | Hard boundary missing | Fast loop, estop bypasses Agent |
| Camera latest-frame | yes (partial) | Inference in capture callback | Dedicated buffer; stream ≠ perception |
| Depth | none, no depthNet, no torch | Cannot install MiDaS/depthNet now | Geometric floor/obstacle heuristic + pluggable backend |
| Detection / tracking | SSD engine missing, no torch | Cannot run stock SSD | OpenCV contour/Haar backend + optional TensorRT adapter |
| Navigation | centroid / binary turn | No planner, no recovery | Local freespace follower + recovery |
| Web UI | Jupyter | — | Stdlib HTTP + MJPEG + dark console |
| Tests | none | — | stdlib unittest with hardware mocks |
| Docs | NVIDIA kit docs | — | `docs/architecture.md`, `docs/development.md` |

---

## 3. Hard constraints (do not violate)

1. **Python 3.6.** No dataclasses, no `list[str]`, no FastAPI, no Pydantic v2, no `HTTPServer` 3.7 helpers. Use stdlib + numpy + OpenCV + `requests` + PyYAML.
2. **Do not `pip install` heavy stacks.** pip is missing; compiling torch/chromadb on this image is not realistic mid-runtime.
3. **Do not call the LLM in the fast loop.** Context window is 2048 tokens — summaries only, never frames.
4. **Do not put perception images into A-MEM or World State.**
5. **Do not drive PWM from the LLM.** Semantic tools only.
6. **Do not require ROS2 / Nav2 / Kafka.** Nano 4GB cannot host that and a camera pipeline.
7. **Do not rewrite `/home/jetbot/jetbot`.** Wrap it. Preserve working kit notebooks.
8. **Emergency stop must work with LLM/Whisper/UI/memory down.**

---

## 4. Reference repositories — what was extracted (not copied)

Cloning dusty-nv/jetson-inference, Navigation2, and jetbot_ros onto this SD card
would consume disk and RAM without making the Nano able to *run* those stacks
(jetson-inference Python module is not installed; Nav2 needs ROS2 Foxy+ and
far more RAM). Architectural patterns taken:

**jetson-inference:** Gst camera → TensorRT → overlay; drop stale frames;
GPU-aware memory. We keep Gst latest-frame and a pluggable detector slot.

**jetbot_ros:** separate camera / motor / perception / motion nodes.
We use threads + interfaces instead of ROS topics.

**Navigation2:** planner ≠ controller; collision monitor is independent;
lifecycle + recovery. Mapped to `LocalPlanner` + `SafetyController` + task
states `REPLANNING` / recovery spin.

**A-MEM (WujiangXu/A-mem-sys, agiresearch/A-mem):** notes with content,
keywords, tags, context, links, evolution on insert. Retrieval is semantic.
We keep the note schema and evolution policy; storage is JSON + lexical
overlap because ChromaDB cannot run here.

**TypeSafe:** `POST https://api.typesafe.ai/v1/systemone` with `state` +
typed `questions` (`choice` / `noul` / `score`). Used only for intent
classification when an API key exists. Not a second brain.

---

## 5. Decisions locked for implementation

| Decision | Choice | Why |
|---|---|---|
| Language / process | One Python 3.6 process, threads | Fits Nano; no extra services |
| HTTP | stdlib `ThreadingMixIn` + `HTTPServer` | No Flask |
| Motors | Real MotorHAT if import+I2C succeed, else simulated | Bus 1 is empty today |
| Servo | PCA9685 if present, else simulated pan/tilt state | Required for active perception API |
| Camera | nvargus Gst → V4L2 → synthetic | Must not crash if CSI is busy |
| Depth | Floor-row heuristic, optional future TensorRT | No engine, no torch |
| Detection | OpenCV contours (+ Haar if cascades exist) | Honest fallback |
| LLM | `/v1/chat/completions`, model from `/v1/models` | Already serving Gemma E4B |
| Memory | `data/amem.json` A-MEM-like | No vector DB |
| TypeSafe | Off by default | No key; fail-open |
| UI | Static SPA + MJPEG + JSON APIs | Dark robotics console |
| Tests | `unittest` + mocks | No pytest, no hardware required |

---

## 6. Ambiguities and how they were resolved

**Motor HAT missing.** Could be unpowered, different bus, or a Waveshare
variant not enumerated. Production path: probe 0x60 on bus 1; if absent,
simulate and surface `motors: SIMULATED` in the UI. Do not hang on I2C.

**Camera servo.** Spec requires semantic look/scan. Stock JetBot camera is
usually fixed. We expose the API and keep an internal pan/tilt pose so
active perception and the UI work even when no PCA9685 is wired.

**Depth model.** Installing MiDaS/depthNet would require a JP4.5 TensorRT
engine and extra RAM. A geometric obstacle estimator is the only path that
is realtime, deterministic, and safe-fail on this image. The detector
interface stays so a TRT engine can be dropped in later.

**TypeSafe cloud vs local.** No local Jev runtime. Optional remote calls
only, never on the safety path.

These are engineering resolutions, not silent guesses that change hardware
behavior. When a MotorHAT appears on I2C, the same interfaces drive it.
---
