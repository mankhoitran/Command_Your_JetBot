# JetBot Cognitive Runtime

Event-driven control stack for this Jetson Nano 4GB. The LLM plans; it does not PWM the motors.

See:

- `docs/gap_analysis.md` — audit of the stock kit vs this architecture
- `docs/architecture.md` — loops, safety boundary, world vs memory
- `docs/development.md` — how to run, test, and configure
- `jetbot_construct.md` — original design prompt

```
python3 run.py
```

Then open `http://<nano-ip>:8080`. Red **STOP** is a local emergency stop.
