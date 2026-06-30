# Copilot Instructions — plc-ebpf-autoscaler

> **SDD: plc-ebpf-autoscaler Stability Hardening**
> Version: 1.0 | Target: Semiconductor FAB, SBC deployment

---

## Project Context

This project runs on a **single-board computer (SBC)** inside a semiconductor FAB production line.
It uses **eBPF** to monitor PLC serial-port (tty) traffic and dynamically scales MQTT decoder
processes in response to data flow changes. **Stability and correctness are the top priorities.**
Cost is not a constraint.

### Architecture

```
[PLC Hardware]
     │ Serial/COM port (ttyACM0, etc.)
     ▼
[SBC — Linux Kernel (eBPF kprobe/kretprobe on tty_read)]
     │ bytes-per-interval measurement
     ▼
[adjust.py — Monitor & Reconciler]   ← runs as plcmon via systemd
     │ subprocess.Popen / SIGTERM
     ▼
[decoder.py × N — MQTT Consumers]   ← each subscribes to one MQTT topic tier
     │ paho-mqtt
     ▼
[Mosquitto MQTT Broker (localhost:1883)]
     │ topics: {machine_sn}/{module}/{unit}/#
     ▼
[Downstream: data transform / upload / storage]
```

### Key Files

| File | Role |
|---|---|
| `adjust.py` | eBPF monitor + decoder process lifecycle manager |
| `decoder.py` | MQTT subscriber + PLC point data processor |
| `requirements.txt` | Pinned Python dependencies |
| `systemd/plc-adjust.service` | systemd unit for adjust.py (runs as `plcmon`) |
| `systemd/plc-decoder@.service` | systemd template unit for individual decoders |

---

## Coding Conventions

### Language & Runtime
- Python 3.10+ only.
- Use `#!/usr/bin/env python3` shebang on all scripts.
- All scripts must handle `SIGTERM` gracefully (clean up subprocesses / MQTT connections).

### Logging
- **Always use structured JSON logging** via the `JsonFormatter` defined in `adjust.py`.
- Every log line must include `ts`, `level`, and `event` keys.
- Add domain fields via the `extra={"extra": {...}}` pattern, for example:
  ```python
  logger.info("Decoder spawned", extra={"extra": {"topic": topic, "pid": proc.pid}})
  ```
- Never use `print()` for runtime output; use `logger.*`.
- Logs must be ingestible by `journald`, Loki, or any JSON log aggregator.

### MQTT Topics
- All subscription topics must be **valid MQTT 3.1.1 filter strings**.
- The multi-level wildcard `#` must appear **only as the final path segment**.
- Valid pattern: `^[^#]*(#|[^#]+/\#)$`
- Topic tier logic (controlled by `--min_delta` bytes threshold):
  - Low traffic  → `{machine_sn}/#`
  - Mid traffic  → `{machine_sn}/{module}/#`  (one per module)
  - High traffic → `{machine_sn}/{module}/{unit}/#`  (one per module+unit)
- `machine_sn` must never be hardcoded; always read from `args.machine_sn`.

### eBPF / BCC
- Use **kretprobe** (`attach_kretprobe`) to measure actual bytes returned by `tty_read`
  via `PT_REGS_RC(ctx)`, not call-count kprobe.
- Use a paired entry kprobe (`attach_kprobe`) + `BPF_HASH(entry_file, u64, u64)` to
  carry the `struct file *` into the return probe for device-name filtering.
- The `bytes` BPF hash accumulates bytes per PID; delta across intervals drives scaling.
- Always check `BPF.get_kprobe_functions(b"tty_read")` before attaching; exit cleanly if absent.
- Check `/sys/fs/bpf` exists before loading BPF programs.

### Process Lifecycle
- `active_decoders: dict[str, subprocess.Popen]` is the single source of truth.
- `reconcile_decoders(desired_topics)` is the only place that spawns or terminates decoders.
- Never spawn a decoder if one is already running for the same topic.
- Always check `proc.poll()` to detect crashes and restart them in the next reconcile call.
- On shutdown, call `terminate_decoder()` for every topic; use `proc.wait(timeout=5)` then `proc.kill()`.

### MQTT Reconnect (decoder.py)
- Always implement `on_disconnect` with **exponential back-off** retry (initial 2 s, max 60 s).
- Re-subscribe in `on_connect` using the topic stored in `userdata`.
- Use `loop_start()` + a `threading.Event` keep-alive rather than `loop_forever()`
  so the main thread can react to signals.

### No Placeholder Code
- Do **not** add `time.sleep()` stubs inside `on_message` or any processing path.
  Insert a `# TODO: replace with real processing logic` comment if needed.

---

## SDD Task Reference

The following tasks define the full stability hardening scope.
Reference a task ID in commit messages and PR descriptions.

### P0 — Correctness

| ID | Title | Status |
|---|---|---|
| TASK-01 | Fix MQTT topic wildcard format | ✅ Done |
| TASK-02 | Replace tty_read call-count with bytes-read (kretprobe) | ✅ Done |
| TASK-03 | Parameterise machine SN (`--machine_sn`) | ✅ Done |

### P1 — Process Lifecycle Management

| ID | Title | Status |
|---|---|---|
| TASK-04 | Track and reap spawned decoder processes (scale-in) | ✅ Done |
| TASK-05 | Prevent duplicate decoder spawning for same topic | ✅ Done |
| TASK-06 | Add MQTT reconnect logic to decoder.py | ✅ Done |

### P2 — Observability & Deployment

| ID | Title | Status |
|---|---|---|
| TASK-07 | Structured JSON logging in adjust.py and decoder.py | ✅ Done |
| TASK-08 | systemd unit files | ✅ Done |
| TASK-09 | Remove simulation delay from decoder.py | ✅ Done |
| TASK-10 | Pin all dependency versions | ✅ Done |

---

## Deployment Notes

### systemd Setup (Recommended)

```bash
# Create dedicated low-privilege user
sudo useradd --system --no-create-home plcmon

# Grant required capabilities (no full sudo needed)
# See systemd/plc-adjust.service → AmbientCapabilities

# Install units
sudo cp systemd/plc-adjust.service /etc/systemd/system/
sudo cp systemd/plc-decoder@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now plc-adjust
```

### Environment Variables (override defaults in unit files)

| Variable | Default | Description |
|---|---|---|
| `PLC_SERIAL` | `all` | Serial port device name to filter |
| `PLC_MACHINE_SN` | `1` | Machine serial number |
| `PLC_INTERVAL` | `60` | Measurement interval (seconds) |
| `PLC_MIN_DELTA` | `8192` | Bytes threshold to trigger scaling |

---

## Testing

Run unit tests (no eBPF kernel required):

```bash
pytest
```

For integration testing with a live broker:

```bash
mosquitto &
python3 adjust.py --dry_run --interval 5 --machine_sn TEST01
```
