# JetBot Cognitive Robotics — Claude Opus Implementation Prompt

## ROLE

You are the principal software architect and senior robotics engineer responsible for transforming this existing JetBot project into a robust, modular, event-driven cognitive robotics system.

Do NOT treat this as a simple "LLM controls motors" project.

The target architecture is a hierarchical robotics system with:

- real-time reactive safety
- continuous visual perception
- world-state estimation
- spatial memory
- semantic long-term memory
- task lifecycle management
- navigation
- active perception
- LLM-based high-level reasoning
- deterministic execution
- event-driven communication
- high-quality web UI
- strict resource awareness for Jetson Nano 4GB

Your job is to inspect the existing codebase, understand it deeply, research the reference implementations below, and then implement the architecture incrementally without destroying working functionality.

---

# 0. VERY IMPORTANT: DO NOT START CODING IMMEDIATELY

Before modifying code:

1. Inspect the entire repository.
2. Identify the current architecture.
3. Identify entry points.
4. Identify existing Agent / Brain / LLM code.
5. Identify motor control.
6. Identify camera handling.
7. Identify servo handling.
8. Identify Web UI.
9. Identify Whisper / ASR integration.
10. Identify current remote LLM configuration.
11. Identify configuration/env files.
12. Identify dependencies and runtime constraints.
13. Identify existing tests.
14. Identify existing hardware abstractions.
15. Identify anything that is already implemented correctly and should NOT be rewritten.

Create an internal architecture map first.

Then compare the current implementation against the target architecture below.

DO NOT blindly rewrite the project.

Prefer targeted refactoring and incremental integration.

---

# 1. REFERENCE REPOSITORIES — READ THEM BEFORE IMPLEMENTING

Clone these repositories into a clearly isolated reference directory such as:

    .references/

or:

    third_party/references/

Do NOT copy their entire implementations into production code.

Read the relevant source code, architecture, examples, APIs, and design patterns.

## 1.1 NVIDIA Jetson vision

Repository:

    https://github.com/dusty-nv/jetson-inference

Study especially:

- TensorRT inference patterns
- detectNet
- camera input
- GStreamer integration
- image handling
- Jetson GPU execution
- realtime inference
- depthNet if useful
- WebRTC / streaming examples
- Python/C++ boundaries
- memory/resource management

The goal is to learn how to build efficient Jetson-native perception.

Do NOT automatically install every dependency.

First determine what is compatible with this Jetson Nano 4GB and the existing JetPack/L4T environment.

---

## 1.2 JetBot ROS reference

Repository:

    https://github.com/dusty-nv/jetbot_ros

Study:

- JetBot camera abstraction
- motor abstraction
- ROS node separation
- sensor → processing → control architecture
- navigation model separation
- simulation structure
- hardware abstraction

Use it as an architecture reference.

Do NOT migrate the entire project to ROS2 unless there is a strong technical reason.

The current system can remain a lightweight native Python architecture.

---

## 1.3 Navigation2

Repository:

    https://github.com/ros-navigation/navigation2

Study the architecture, especially:

- planner/controller separation
- lifecycle/state management
- collision monitoring
- velocity control
- navigation actions
- behavior trees
- recovery behaviors
- action/service interfaces

Do NOT install Nav2 just for the sake of using Nav2.

Extract useful architectural concepts.

The Jetson Nano has limited resources.

If a lightweight native implementation is more appropriate, explain why before introducing the abstraction.

---

## 1.4 A-MEM

Primary implementation:

    https://github.com/WujiangXu/A-mem-sys

Also inspect:

    https://github.com/agiresearch/A-mem

Study:

- memory note construction
- memory linking
- semantic retrieval
- contextual descriptions
- memory evolution
- temporal information
- agent-driven memory operations

IMPORTANT:

A-MEM is NOT the realtime World State.

Do not use A-MEM as the source of truth for:

- current obstacle position
- current robot pose
- current camera orientation
- realtime collision state
- current free-space
- motor commands

Instead, use A-MEM as semantic/long-term memory.

---

## 1.5 TypeSafe

Repository / Claude skill:

    https://github.com/typesafe-ai/skills

Read the TypeSafe skill and understand how it can be used from an agentic workflow.

TypeSafe is a decision/validation tool available to the Agent/LLM.

It is NOT another brain.

Possible uses:

- intent classification
- action validation
- confidence estimation
- ambiguity detection
- structured decisions
- confirmation decisions

Do not force TypeSafe into every decision.

Use it where typed probabilistic decisions provide value.

---

# 2. TARGET SYSTEM

The target system should conceptually look like:

                    HUMAN / EXTERNAL INPUT
                             │
             ┌───────────────┼────────────────┐
             │               │                │
           Voice           Web             Button/API
             │
          Whisper
             │
             └───────────────┬────────────────┘
                             ▼
                    ┌─────────────────┐
                    │      AGENT      │
                    │                 │
                    │ Task Manager    │
                    │ LLM Reasoner    │
                    │ Decision Layer  │
                    │ Tool Router     │
                    └────────┬────────┘
                             │
             ┌───────────────┼───────────────────┐
             │               │                   │
             ▼               ▼                   ▼
       World Model        Memory              Tools
             │               │                   │
             │            A-MEM             Camera
             │                                Navigation
             │                                Motion
             │                                TypeSafe
             │
             ▲
             │
      ┌──────┴─────────┐
      │   PERCEPTION   │
      │                │
      │ RGB Camera     │
      │ Depth          │
      │ Detection      │
      │ Tracking       │
      │ Free Space     │
      └──────┬─────────┘
             │
             ▼
        Event System
             │
             ▼
      Safety Controller
             │
             ▼
       Motors / Servo

This is a conceptual architecture.

Adapt it to the actual repository after auditing the codebase.

---

# 3. CORE ARCHITECTURAL PRINCIPLE

The most important rule:

THE LLM IS NOT THE REALTIME ROBOT CONTROLLER.

The LLM performs:

- task understanding
- high-level planning
- reasoning
- replanning
- semantic interpretation
- deciding when more information is needed

The LLM must NOT directly:

- PWM motors
- continuously steer
- bypass safety
- issue arbitrary low-level motor commands
- make realtime collision decisions

The execution path must be:

    LLM
      ↓
    Intent / Plan
      ↓
    Navigation / Action Layer
      ↓
    Safety Validation
      ↓
    Hardware

Emergency stop must bypass the LLM entirely.

---

# 4. THREE TEMPORAL LOOPS

The architecture must use three different timescales.

## FAST LOOP — realtime safety

Approximate responsibility:

    Camera / Depth
          ↓
    obstacle / collision estimation
          ↓
    safety controller
          ↓
    motor stop / speed limiting

This loop must be deterministic.

Do NOT call the LLM here.

Do NOT perform expensive semantic memory operations here.

The robot must be able to stop even when:

- LLM server is unavailable
- network is unavailable
- Agent crashes
- A-MEM fails
- Web UI crashes
- Whisper fails

---

## MEDIUM LOOP — perception and navigation

Conceptually:

    Camera
      ↓
    Perception
      ↓
    World State
      ↓
    Navigation
      ↓
    Local action
      ↓
    Observe again

This loop handles:

- obstacle updates
- object tracking
- free-space
- local navigation
- target tracking
- camera orientation
- active perception

---

## SLOW / EVENT-DRIVEN LOOP — cognition

Conceptually:

    Task
      ↓
    Agent
      ↓
    LLM
      ↓
    Plan
      ↓
    Tools
      ↓
    Observe
      ↓
    Replan when necessary

The LLM should NOT be invoked for every frame.

Only invoke it when reasoning is actually necessary.

---

# 5. WORLD MODEL

Implement a dedicated World Model / World State subsystem.

Do not call everything "memory".

Separate:

## Current World State

Contains things such as:

- robot state
- robot pose estimate
- velocity
- camera orientation
- visible objects
- tracked object identities
- obstacles
- free-space
- target
- current task
- current action
- confidence
- timestamps
- last-seen information
- active uncertainty

Example:

    robot:
      pose
      velocity
      battery
      camera_pan
      camera_tilt

    objects:
      chair_01:
        class
        position
        confidence
        last_seen
        track_id

    obstacles:
      ...

    navigation:
      current_target
      current_path
      blocked
      confidence

World State must be operational and fast.

---

# 6. SPATIAL MEMORY

Maintain longer-lived knowledge separately.

Examples:

- room structure
- known doors
- tables
- chairs
- charging station
- known areas
- previously observed locations
- relationships between objects
- areas already explored

Spatial memory should not store every frame.

It should represent useful environmental knowledge.

---

# 7. A-MEM INTEGRATION

Use A-MEM for semantic/long-term memory.

A-MEM should be responsible for knowledge such as:

    "The charging station was previously observed near the wall."

    "The table was observed from the entrance."

    "The left side of the room contains a chair."

    "The robot previously explored this area."

Do NOT put raw perception streams into A-MEM.

Do NOT create a memory for every frame.

Do NOT allow stale semantic memory to override current perception.

Priority must be:

    REALTIME SENSOR OBSERVATION
        >
    CURRENT WORLD STATE
        >
    SPATIAL MAP
        >
    LONG-TERM SEMANTIC MEMORY

For example, if A-MEM says an area is empty but current depth detects an obstacle, current perception wins.

---

# 8. MEMORY UPDATE POLICY

Do not blindly write every observation.

Create a memory only when something meaningful happened.

Potential triggers:

- new object discovered
- object identity confirmed
- object moved significantly
- new room/area discovered
- important spatial relationship discovered
- target found
- target lost
- navigation failure
- successful navigation
- repeated observation confirms previous knowledge
- important environmental change

Each memory should have appropriate metadata such as:

- timestamp
- confidence
- source
- location
- related objects
- observation context
- last verified time

Use confidence and freshness.

Do not turn uncertain perception into permanent fact immediately.

---

# 9. UNCERTAINTY

This is a first-class concept.

The system should distinguish:

    KNOWN
    PROBABLE
    UNKNOWN
    STALE
    CONTRADICTED

Example:

    door_A:
        position = known
        confidence = 0.94

    behind_table_A:
        state = UNKNOWN

The Agent should be able to reason:

    "I do not know."

rather than hallucinating an answer.

Unknown state can trigger active perception.

---

# 10. ACTIVE PERCEPTION

The camera is mounted on a servo.

Therefore camera movement is itself an action.

Expose semantic actions such as:

    camera.look_forward()
    camera.look_left()
    camera.look_right()
    camera.look_up()
    camera.look_down()
    camera.scan_environment()
    camera.inspect(target)

Do NOT make the LLM control servo degrees directly.

The camera subsystem should hide low-level servo control.

Example:

    Agent:
      "I am uncertain about the left side."

    ↓

    camera.look_left()

    ↓

    perception

    ↓

    World State update

    ↓

    Agent continues reasoning

This is active perception.

---

# 11. INITIAL ROOM SCAN

At task/bootstrap time, support:

    START
      ↓
    Camera scan
      ↓
    Perception
      ↓
    World State
      ↓
    Initial spatial knowledge
      ↓
    Agent
      ↓
    movement

Do NOT attempt to build a perfect map before moving.

The purpose of the initial scan is to obtain enough information to begin safely and intelligently.

During movement, continue perception.

---

# 12. CAMERA STREAM ARCHITECTURE

This is extremely important.

The camera stream must NOT be blocked by inference.

Avoid:

    capture
      ↓
    depth
      ↓
    detection
      ↓
    LLM
      ↓
    capture again

Instead use a producer/consumer or latest-frame architecture.

Conceptually:

                  Camera
                    │
              latest-frame buffer
               ┌────┴────┐
               │         │
               ▼         ▼
            Web UI    Perception
                        │
             ┌──────────┼─────────┐
             ▼          ▼         ▼
           Depth     Detection  Tracking

The web stream should remain smooth even when inference is slow.

Use:

- bounded queues
- latest-frame semantics where appropriate
- asynchronous workers
- frame dropping when necessary
- backpressure
- timestamps
- inference scheduling
- separate streaming and inference paths

Do not accumulate unlimited frames.

The newest frame is usually more valuable than stale frames for navigation.

---

# 13. PERCEPTION PIPELINE

The current camera should support:

    RGB
     ↓
    Depth Estimation
     ↓
    Free-Space / Obstacle Estimation

and independently:

    RGB
     ↓
    Object Detection
     ↓
    Tracking

Then combine them:

    Detection
       +
    Depth
       +
    Tracking
       ↓
    Spatial Perception
       ↓
    World State

Do not assume object detection itself equals obstacle detection.

An object detector tells you what is there.

Navigation needs:

- distance
- geometry
- free-space
- traversability
- relation to robot trajectory

---

# 14. DEPTH MODEL

Use a lightweight depth estimation model appropriate for Jetson Nano 4GB.

First inspect:

- existing CUDA version
- JetPack/L4T
- TensorRT availability
- GPU memory
- current Python environment
- camera resolution
- current FPS

Do not install a large model blindly.

Benchmark at realistic resolutions.

The objective is not maximum depth quality.

The objective is:

    sufficiently useful depth
    +
    stable realtime behavior
    +
    low memory usage

If jetson-inference provides a suitable depth primitive or integration path, evaluate it first.

If another lightweight model is technically better for this hardware, explain why before introducing it.

---

# 15. NAVIGATION

Navigation should be separated from LLM reasoning.

The LLM should request semantic goals:

    go_to("table_A")
    inspect("door_A")
    explore("unknown_area")
    follow("person_01")

The navigation layer converts those goals into executable local actions.

Conceptually:

    semantic goal
        ↓
    navigation planner
        ↓
    local path / direction
        ↓
    safety controller
        ↓
    motors

Navigation must consume:

- World State
- depth
- free-space
- robot state
- target
- uncertainty

Do not make the LLM calculate low-level geometry every time.

---

# 16. EVENT SYSTEM

Implement an event-driven communication layer.

Define typed events such as:

    ObjectDetected
    ObjectUpdated
    ObjectLost

    ObstacleDetected
    ObstacleCleared

    WorldChanged
    ScanRequested
    ScanCompleted

    TargetFound
    TargetLost

    LowConfidence
    UncertaintyDetected

    TaskStarted
    TaskPaused
    TaskCompleted
    TaskFailed
    TaskCancelled

    NavigationBlocked
    NavigationRecovered

    EmergencyStop

Events should be typed and structured.

Do not use arbitrary strings everywhere.

An event should contain enough metadata for debugging.

Example:

    {
      type: "ObstacleDetected",
      timestamp: ...,
      source: "depth",
      confidence: 0.93,
      distance: 0.38,
      direction: "front"
    }

Safety-critical events must not depend on the Agent.

---

# 17. TASK LIFECYCLE

Implement a proper task state machine.

At minimum:

    IDLE
      ↓
    PLANNING
      ↓
    EXECUTING
      ↓
    OBSERVING
      ↓
    VERIFYING
      ↓
    COMPLETED

With recovery:

    EXECUTING
        ↓
    obstacle / failure
        ↓
    REPLANNING
        ↓
    EXECUTING

And:

    EXECUTING
        ↓
    uncertainty
        ↓
    OBSERVING / ACTIVE_PERCEPTION
        ↓
    PLANNING

The Task Manager owns the lifecycle.

The LLM does not own the lifecycle.

---

# 18. AGENT

Agent is the orchestration/control-plane.

It should coordinate:

- Task Manager
- LLM
- World Model
- Memory
- Navigation
- Camera
- Motion
- TypeSafe
- Event system

The Agent should reason over summarized state, not raw frame streams.

For example, give the LLM:

    Current task:
      go to charging station

    World:
      charging station known
      location confidence 0.88

    Navigation:
      path blocked

    Perception:
      obstacle at 0.42m
      left side partially unexplored

    Memory:
      charging station was previously seen near east wall

The LLM should NOT receive 30 FPS raw images by default.

---

# 19. TYPE SAFE

Integrate TypeSafe only where useful.

Possible use cases:

    classify_intent()
    classify_robot_state()
    validate_action()
    estimate_decision_confidence()
    determine_if_confirmation_is_needed()

Use typed outputs.

Do not add TypeSafe calls everywhere.

Avoid latency and unnecessary external calls.

---

# 20. WHISPER

Whisper is an external ASR service.

Keep the architecture:

    microphone
       ↓
    Whisper
       ↓
    text
       ↓
    Agent

Do not make Whisper part of the reasoning architecture.

Do not make Agent responsible for speech recognition.

---

# 21. SAFETY CONTROLLER

This is a hard boundary.

Before hardware:

    Agent / Planner
          ↓
    Safety Validation
          ↓
    Hardware

Safety checks should include whatever is appropriate to the actual hardware:

- velocity limits
- acceleration limits
- command duration
- obstacle distance
- sensor freshness
- command validity
- hardware state
- watchdog timeout

Emergency stop:

    EMERGENCY STOP
          ↓
    motor.stop()

No LLM.

No network.

No memory.

No Web UI dependency.

---

# 22. HARDWARE ABSTRACTION

Keep hardware behind interfaces.

At minimum conceptually:

    MotorController
    ServoController
    CameraController

The Agent should not know:

- GPIO details
- PWM details
- motor driver details
- camera driver internals

Example:

    motion.forward(speed=0.15)

rather than:

    GPIO.write(...)
    PWM(...)
    ...

---

# 23. RESOURCE MANAGEMENT

This is a Jetson Nano 4GB.

Treat compute as a scarce resource.

Measure:

- RAM
- GPU memory
- CPU
- FPS
- inference latency
- queue depth
- camera latency
- event latency
- LLM latency

Avoid running:

- multiple heavy models simultaneously
- unnecessary full-resolution inference
- blocking synchronous pipelines
- unlimited queues
- duplicate frame copies

Prefer:

- low-resolution perception
- GPU-aware scheduling
- frame skipping
- latest-frame processing
- asynchronous execution
- model reuse
- batching only when actually beneficial

---

# 24. WEB UI

The current UI should be redesigned, not merely patched.

The UI should look like a real robotics control console.

It should expose at least:

## Live Camera

- smooth realtime stream
- camera orientation
- FPS
- latency

## Robot Status

- connection
- motor state
- battery if available
- CPU
- RAM
- GPU
- temperature if available

## Perception

- detected objects
- confidence
- depth
- obstacle status
- tracking IDs

Overlay useful information on the camera view when practical.

## World State

Display:

- current robot state
- current target
- known objects
- confidence
- last seen
- navigation state

## Task

Display:

- current task
- state
- current action
- progress
- failure reason
- replanning status

## Memory

Provide a debug/inspection view:

- recent memories
- relevant memories
- linked memories
- timestamps
- confidence
- source

Do not expose sensitive internal LLM chain-of-thought.

Show concise reasoning summaries / decisions / tool calls where appropriate.

## Events

Live event timeline:

    14:21:03 ObjectDetected
    14:21:04 WorldChanged
    14:21:05 NavigationBlocked
    14:21:05 EmergencyStop
    14:21:06 Replanning

## Manual Controls

Provide safe manual controls:

- forward
- backward
- left
- right
- stop
- camera left/right/up/down
- scan

The STOP button must be visually prominent.

The UI must never bypass backend safety validation.

---

# 25. UI DESIGN PRINCIPLES

Do not make the UI look like a generic CRUD dashboard.

Aim for a polished robotics control station:

- dark professional interface
- clear hierarchy
- large camera viewport
- compact telemetry
- status indicators
- clean cards
- live event stream
- responsive layout
- good spacing
- subtle animation only where useful
- clear error states
- clear connected/disconnected states

Avoid excessive gradients, huge text, unnecessary decoration, or visual noise.

Prioritize information density and usability.

If the existing frontend framework is already good, keep it.

Do not rewrite the frontend stack unnecessarily.

---

# 26. OBSERVABILITY

Build observability into the system.

Every important subsystem should expose:

- health
- latency
- last update
- error state
- queue size
- FPS where applicable

Examples:

    Camera:       ONLINE
    Depth:        8.4 FPS
    Detection:    5.1 FPS
    World State:  20 Hz
    LLM:          ONLINE
    Whisper:       ONLINE
    Memory:       ONLINE
    Motors:        READY

This should be visible in the UI.

---

# 27. FAILURE HANDLING

Assume components will fail.

Examples:

LLM unavailable:

    continue safe local operation
    → report unavailable
    → do not crash robot

Whisper unavailable:

    web/button control still works

A-MEM unavailable:

    current World State still works

Camera inference unavailable:

    raw stream may continue
    safety behavior must fail safely

Web UI unavailable:

    robot runtime must continue safely

Network unavailable:

    emergency stop must still work locally

Any stale perception data must be marked stale.

Never silently treat stale sensor information as current.

---

# 28. CONFIGURATION

Do not hardcode:

- IP addresses
- model names
- thresholds
- ports
- camera settings
- FPS
- safety distances
- servo limits
- motor limits

Use the existing configuration approach if one exists.

Preserve the current remote services.

The current architecture already uses remote services for:

LLM:

    http://192.168.20.150:8008/v1

Whisper:

    http://192.168.20.150:8003

Do not change these assumptions unless the existing code/configuration shows otherwise.

---

# 29. TESTING STRATEGY

Do not wait until the entire architecture is finished.

Implement and test incrementally.

At minimum:

## Unit tests

- World State updates
- confidence handling
- stale data
- event creation
- event dispatch
- task transitions
- safety validation
- navigation decisions
- memory update policy

## Integration tests

- camera → perception
- perception → World State
- World State → Agent
- Agent → planner
- planner → safety
- safety → motor

## Failure tests

- LLM timeout
- Whisper timeout
- memory failure
- camera failure
- stale depth
- lost target
- blocked navigation
- emergency stop

Use mocks for hardware where appropriate.

Do not require physical hardware for every test.

---

# 30. DEVELOPMENT ORDER

Do NOT attempt to implement everything at once.

Use this approximate sequence:

PHASE 1
    Audit existing repository
    Document current architecture
    Identify reusable code

PHASE 2
    Establish core interfaces
    Hardware abstraction
    Event model
    World State model
    Task state machine

PHASE 3
    Camera pipeline
    latest-frame buffer
    streaming/perception separation

PHASE 4
    Depth
    obstacle estimation
    free-space estimation

PHASE 5
    object detection
    tracking
    spatial perception

PHASE 6
    World State integration
    confidence
    freshness
    uncertainty

PHASE 7
    Navigation layer
    local action planning
    safety validation

PHASE 8
    Agent integration
    LLM planning
    tool routing
    TypeSafe

PHASE 9
    A-MEM / semantic memory
    meaningful memory writes
    retrieval
    memory evolution

PHASE 10
    initial environment scan
    active perception

PHASE 11
    Web UI redesign
    telemetry
    events
    world state
    perception overlays
    task state
    memory inspection

PHASE 12
    failure handling
    benchmarks
    profiling
    integration testing

Do not move to the next phase if the previous phase is structurally broken.

---

# 31. ARCHITECTURAL QUALITY RULES

Prefer:

- interfaces
- dependency injection where useful
- typed models
- dataclasses / Pydantic where appropriate
- async where it actually helps
- clear module boundaries
- deterministic components
- explicit state transitions
- structured events
- testability

Avoid:

- global mutable state
- circular imports
- giant Agent classes
- giant app.py files
- hidden background threads
- arbitrary callbacks everywhere
- direct motor access from LLM
- raw frame storage
- memory writes on every frame
- blocking inference in HTTP handlers
- hardcoded configuration
- unnecessary frameworks

---

# 32. DO NOT OVERENGINEER

The target architecture is sophisticated, but the hardware is small.

Do NOT introduce:

- Kafka
- Kubernetes
- microservice explosion
- unnecessary databases
- huge vector databases
- heavyweight distributed systems
- ROS2 solely for architectural fashion

unless there is a concrete technical reason.

A clean modular Python process with async workers, typed events, queues, and clear interfaces may be better.

Keep the architecture conceptually scalable without making the Nano unnecessarily complex.

---

# 33. ASK QUESTIONS WHEN SOMETHING IS WRONG

This is mandatory.

If you encounter:

- conflicting architecture
- missing information
- unclear hardware capability
- unclear camera interface
- incompatible dependency
- incompatible JetPack version
- unclear motor API
- ambiguous existing behavior
- dangerous assumption
- architectural contradiction
- uncertainty about whether to rewrite something
- a choice that materially changes the system

DO NOT silently guess.

STOP and ask me a concise, technically specific question.

Good question:

    "The current camera driver uses OpenCV/V4L2 at 30 FPS, but the proposed TensorRT pipeline requires GStreamer/NvArgus for zero-copy. Should the production path remain V4L2 or can I migrate the camera backend?"

Bad question:

    "What should I do?"

When asking a question:

1. Explain the detected issue.
2. Explain the two or three viable options.
3. State your technical recommendation if appropriate.
4. Ask for the decision.

Do not ask questions for trivial implementation details that you can determine yourself.

---

# 34. BEFORE EVERY MAJOR ARCHITECTURAL CHANGE

Explain internally:

    Current behavior
        ↓
    Problem
        ↓
    Proposed design
        ↓
    Why it is better
        ↓
    Compatibility impact
        ↓
    Implementation

Preserve working functionality unless there is a clear reason to change it.

---

# 35. DOCUMENTATION

Update or create architecture documentation.

At minimum:

    docs/architecture.md

Include:

- system architecture
- component responsibilities
- event flow
- World State schema
- task lifecycle
- perception pipeline
- memory architecture
- safety boundary
- navigation architecture
- runtime loops
- camera streaming architecture

Also document:

    docs/development.md

with:

- setup
- dependencies
- hardware requirements
- services
- configuration
- testing
- profiling
- debugging

---

# 36. FINAL ARCHITECTURAL TARGET

The final system should conceptually behave like:

    OBSERVE
       ↓
    PERCEIVE
       ↓
    UPDATE WORLD MODEL
       ↓
    CHECK SAFETY
       ↓
    DO WE NEED TO REASON?
       │
       ├── NO
       │    ↓
       │  CONTINUE
       │
       └── YES
            ↓
          AGENT
            ↓
           LLM
            ↓
      TypeSafe if useful
            ↓
          PLAN
            ↓
       NAVIGATION
            ↓
      SAFETY VALIDATION
            ↓
          EXECUTE
            ↓
         OBSERVE AGAIN

With memory:

    meaningful observation
            ↓
       World State
            ↓
      Memory Policy
            ↓
       A-MEM
            ↓
    semantic retrieval
            ↓
          Agent

With active perception:

    uncertainty
        ↓
    camera action
        ↓
    new observation
        ↓
    World State update
        ↓
    continue planning

With emergency safety:

    obstacle / unsafe state
            ↓
       LOCAL STOP
            ↓
      notify Agent
            ↓
       REPLANNING

---

# 37. SUCCESS CRITERIA

Do not consider the work complete merely because the code runs.

The resulting system should demonstrate:

1. Camera stream remains responsive while inference runs.
2. Perception is decoupled from the web stream.
3. World State updates independently of LLM.
4. Safety works without LLM/network.
5. Agent can reason over structured world state.
6. Navigation does not require LLM-generated low-level motor commands.
7. Camera servo can perform semantic active perception.
8. Initial room scan is supported.
9. Meaningful world changes generate events.
10. Task lifecycle is explicit and recoverable.
11. A-MEM stores semantic long-term knowledge rather than raw realtime state.
12. Uncertainty is represented explicitly.
13. TypeSafe is used selectively for structured decisions.
14. Whisper remains an external ASR service.
15. UI exposes the actual robot state rather than fake/static telemetry.
16. Failure of one subsystem does not unnecessarily crash the entire robot.
17. The code remains understandable and modular.
18. The system remains realistic for Jetson Nano 4GB.

---

# 38. MOST IMPORTANT INSTRUCTION

Think before coding.

You are not being asked to mechanically implement the architecture described above.

You are being asked to:

    inspect
    understand
    challenge
    improve
    design
    implement
    benchmark
    test

If you find a better architecture than the one described here, propose it.

If something in this specification is technically wrong for the actual hardware or repository, DO NOT silently implement it.

Tell me.

The goal is not to make the code match this prompt.

The goal is to build the best practical cognitive robotics architecture that this JetBot hardware and existing codebase can realistically support.

Start by auditing the repository and producing an architecture gap analysis.

Do not make major code changes until the gap analysis is complete and the important ambiguities have been resolved.

