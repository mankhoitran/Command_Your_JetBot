# Reference notes (patterns only — not vendored)

The construct prompt asks to clone dusty-nv/jetson-inference, dusty-nv/jetbot_ros,
ros-navigation/navigation2, A-MEM, and TypeSafe skills. Those trees are not
copied into production. jetson-inference and Nav2 are large, need packages
this image does not have, and would fight the Nano's 4GB RAM.

## jetson-inference (dusty-nv)

- `gstCamera` / `videoSource` produce frames independently of `detectNet`.
- TensorRT engines are loaded once and reused.
- Overlays are a display path, not a control path.
- `depthNet` exists but is not installed on this Nano.

## jetbot_ros (dusty-nv)

- Camera, motors, and collision each own a node.
- Hardware details stay behind publishers/subscribers.
- We map nodes → threads + interfaces.

## Navigation2

- Planner vs controller vs collision monitor.
- Lifecycle: unconfigured → inactive → active.
- Recovery behaviors (spin, backup) when blocked.
- Costmaps are too heavy; we use a 1D freespace histogram.

## A-MEM

Note fields: id, content, keywords, tags, context, timestamp, links,
evolution history. Insert may rewrite related notes. Never store raw frames.

## TypeSafe

`POST /v1/systemone` with `state`, `model`, `questions`.
Primitives: noul (P(yes)), choice (option + distribution), score (weighted level).
Use for intent only. Code owns the workflow.
---
