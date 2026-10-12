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

Then open **https** (not http) at `https://<nano-ip>:8080`. Find the IP with `hostname -I`. The cert is self-signed — accept/continue in the browser. Plain `http://` on 8080 is reset because SSL is enabled for the microphone. Pipeline monitor: `https://<nano-ip>:8090`. Red **STOP** is a local emergency stop.

The CSI camera is bolted forward (`servo.enabled: false` in `config.yaml`). LOOK left/right yaw the body; there is no pan/tilt gimbal until a PCA9685 is fitted. Wheels stay locked until **UNLOCK WHEELS**. See `docs/pipeline_audit.md` and `docs/implementation_plan.md`.
