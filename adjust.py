#!/usr/bin/env python3
import argparse
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import yaml

try:
    from bcc import BPF
except ImportError:  # pragma: no cover - exercised in production
    BPF = None


class JsonFormatter(logging.Formatter):
    def format(self, record):
        log_obj = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "event": record.getMessage(),
        }
        if hasattr(record, "extra"):
            log_obj.update(record.extra)
        return json.dumps(log_obj, ensure_ascii=False)


handler = logging.StreamHandler()
handler.setFormatter(JsonFormatter())
logging.basicConfig(level=logging.INFO, handlers=[handler])
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MachineConfig:
    machine_sn: str
    serial_port: str = "all"
    min_delta: int = 8192
    max_module: int = 16
    max_unit: int = 8

    @classmethod
    def from_dict(cls, raw: dict[str, Any], defaults: "MachineConfig") -> "MachineConfig":
        machine_sn = str(raw.get("machine_sn") or raw.get("machineSn") or "").strip()
        if not machine_sn:
            raise ValueError("machine_sn is required for every configured machine")
        serial_port = str(raw.get("serial_port") or raw.get("serial") or defaults.serial_port).strip() or "all"
        return cls(
            machine_sn=machine_sn,
            serial_port=serial_port,
            min_delta=int(raw.get("min_delta", defaults.min_delta)),
            max_module=int(raw.get("max_module", defaults.max_module)),
            max_unit=int(raw.get("max_unit", defaults.max_unit)),
        )

    @property
    def key(self) -> tuple[str, str]:
        return self.machine_sn, self.serial_port.lower()


class MetricsState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.decoder_crashes_total = 0
        self.ebpf_attach_errors_total = 0
        self.contexts: dict[str, dict[str, Any]] = {}
        self.last_reload_status = "initial"

    def set_reload_status(self, status: str) -> None:
        with self._lock:
            self.last_reload_status = status

    def record_attach_error(self, machine_sn: str, serial_port: str, error: str) -> None:
        key = f"{machine_sn}:{serial_port}"
        with self._lock:
            self.ebpf_attach_errors_total += 1
            self.contexts[key] = {
                "machine_sn": machine_sn,
                "serial_port": serial_port,
                "last_delta": 0,
                "active_decoders": 0,
                "healthy": False,
                "last_error": error,
            }

    def record_context(self, machine_sn: str, serial_port: str, delta: int, active_decoders: int, healthy: bool, last_error: str | None = None) -> None:
        key = f"{machine_sn}:{serial_port}"
        with self._lock:
            self.contexts[key] = {
                "machine_sn": machine_sn,
                "serial_port": serial_port,
                "last_delta": delta,
                "active_decoders": active_decoders,
                "healthy": healthy,
                "last_error": last_error,
            }

    def increment_decoder_crashes(self) -> None:
        with self._lock:
            self.decoder_crashes_total += 1

    def remove_context(self, machine_sn: str, serial_port: str) -> None:
        key = f"{machine_sn}:{serial_port}"
        with self._lock:
            self.contexts.pop(key, None)

    def health_snapshot(self) -> dict[str, Any]:
        with self._lock:
            contexts = list(self.contexts.values())
            unhealthy = [ctx for ctx in contexts if not ctx.get("healthy", False)]
            return {
                "status": "ok" if not unhealthy else "degraded",
                "reload_status": self.last_reload_status,
                "contexts": contexts,
                "decoder_crashes_total": self.decoder_crashes_total,
                "ebpf_attach_errors_total": self.ebpf_attach_errors_total,
            }

    def render_prometheus(self) -> str:
        with self._lock:
            lines = [
                "# HELP plc_decoder_crashes_total Total crashed decoders detected by adjust.py",
                "# TYPE plc_decoder_crashes_total counter",
                f"plc_decoder_crashes_total {self.decoder_crashes_total}",
                "# HELP plc_ebpf_attach_errors_total Total eBPF attach failures",
                "# TYPE plc_ebpf_attach_errors_total counter",
                f"plc_ebpf_attach_errors_total {self.ebpf_attach_errors_total}",
                "# HELP plc_bytes_delta Latest bytes delta observed for a machine context",
                "# TYPE plc_bytes_delta gauge",
                "# HELP plc_active_decoders_count Number of active decoders for a machine context",
                "# TYPE plc_active_decoders_count gauge",
                "# HELP plc_context_health Machine context health (1 healthy, 0 unhealthy)",
                "# TYPE plc_context_health gauge",
            ]
            for ctx in self.contexts.values():
                labels = f'machine_sn="{ctx["machine_sn"]}",serial_port="{ctx["serial_port"]}"'
                lines.append(f"plc_bytes_delta{{{labels}}} {ctx['last_delta']}")
                lines.append(f"plc_active_decoders_count{{{labels}}} {ctx['active_decoders']}")
                lines.append(f"plc_context_health{{{labels}}} {1 if ctx['healthy'] else 0}")
            return "\n".join(lines) + "\n"


class WatchdogNotifier:
    def __init__(self) -> None:
        self.notify_socket = os.getenv("NOTIFY_SOCKET")
        watchdog_usec = os.getenv("WATCHDOG_USEC")
        self.watchdog_interval = int(watchdog_usec) / 2_000_000 if watchdog_usec else None
        self._last_ping = 0.0

    def _send(self, message: str) -> None:
        if not self.notify_socket:
            return
        address = self.notify_socket
        if address.startswith("@"):
            address = "\0" + address[1:]
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendall(message.encode("utf-8"))

    def ready(self) -> None:
        self._send("READY=1")

    def status(self, message: str) -> None:
        self._send(f"STATUS={message}")

    def ping_if_due(self) -> None:
        if not self.watchdog_interval:
            return
        now = time.monotonic()
        if now - self._last_ping >= self.watchdog_interval:
            self._send("WATCHDOG=1")
            self._last_ping = now


class MonitoringHandler(BaseHTTPRequestHandler):
    metrics_state: MetricsState | None = None

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            payload = json.dumps(self.metrics_state.health_snapshot(), ensure_ascii=False).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path == "/metrics":
            payload = self.metrics_state.render_prometheus().encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args: Any) -> None:
        return


class MonitoringServer:
    def __init__(self, host: str, port: int, metrics_state: MetricsState) -> None:
        MonitoringHandler.metrics_state = metrics_state
        self.server = ThreadingHTTPServer((host, port), MonitoringHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.host = host
        self.port = port

    def start(self) -> None:
        self.thread.start()
        logger.info(
            "Monitoring endpoints started",
            extra={"extra": {"host": self.host, "port": self.port, "healthz": "/healthz", "metrics": "/metrics"}},
        )

    def stop(self) -> None:
        if self.thread.is_alive():
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)
            return
        self.server.server_close()


class MachineRuntime:
    def __init__(self, config: MachineConfig, args: argparse.Namespace, metrics: MetricsState) -> None:
        self.config = config
        self.args = args
        self.metrics = metrics
        self.active_decoders: dict[str, subprocess.Popen] = {}
        self.decoder_failures: dict[str, dict[str, float]] = {}
        self.decoder_started_at: dict[str, float] = {}
        self.lock = threading.Lock()
        self.bpf = None
        self.bytes_table = None
        self.last_error: str | None = None
        self.script_dir = Path(__file__).resolve().parent

    @property
    def healthy(self) -> bool:
        return self.bpf is not None and self.bytes_table is not None and self.last_error is None

    def _generate_filter_code(self) -> str:
        serial = self.config.serial_port
        if serial.lower() == "all":
            logger.info("No serial port filtering; measuring all tty_read bytes.", extra={"extra": {"machine_sn": self.config.machine_sn}})
            return ""
        logger.info(
            "Serial port filter active",
            extra={"extra": {"machine_sn": self.config.machine_sn, "serial": serial}},
        )
        return "".join(f"    if (d_name[{index}] != '{char}') return 0;\n" for index, char in enumerate(serial))

    def _build_bpf_text(self) -> str:
        filter_code = self._generate_filter_code()
        return f"""
#include <uapi/linux/ptrace.h>
#include <linux/fs.h>
#include <linux/dcache.h>

BPF_HASH(bytes, u32);
BPF_HASH(entry_file, u64, u64);

int trace_tty_read_entry(struct pt_regs *ctx) {{
    struct file *file = (struct file *)PT_REGS_PARM1(ctx);
    if (!file)
        return 0;
    struct dentry *dentry = file->f_path.dentry;
    if (!dentry)
        return 0;
    char d_name[32] = {{}};
    bpf_probe_read_str(d_name, sizeof(d_name), dentry->d_name.name);
    {filter_code}
    u64 pid_tgid = bpf_get_current_pid_tgid();
    u64 fp = (u64)file;
    entry_file.update(&pid_tgid, &fp);
    return 0;
}}

int trace_tty_read_exit(struct pt_regs *ctx) {{
    u64 pid_tgid = bpf_get_current_pid_tgid();
    u64 *fp = entry_file.lookup(&pid_tgid);
    if (!fp)
        return 0;
    entry_file.delete(&pid_tgid);
    ssize_t n = (ssize_t)PT_REGS_RC(ctx);
    if (n <= 0)
        return 0;
    u32 pid = (u32)(pid_tgid >> 32);
    u64 *val = bytes.lookup(&pid);
    if (val) {{
        (*val) += (u64)n;
    }} else {{
        u64 init_val = (u64)n;
        bytes.update(&pid, &init_val);
    }}
    return 0;
}}
"""

    def start(self) -> None:
        if BPF is None:
            raise RuntimeError("bcc is not installed; unable to start eBPF monitoring")
        self.bpf = BPF(text=self._build_bpf_text())
        self.bpf.attach_kprobe(event="tty_read", fn_name="trace_tty_read_entry")
        self.bpf.attach_kretprobe(event="tty_read", fn_name="trace_tty_read_exit")
        self.bytes_table = self.bpf.get_table("bytes")
        logger.info(
            "Attached kprobe+kretprobe to tty_read",
            extra={"extra": {"machine_sn": self.config.machine_sn, "serial": self.config.serial_port}},
        )
        self.metrics.record_context(self.config.machine_sn, self.config.serial_port, 0, 0, True)

    def _decoder_command(self, topic: str) -> list[str]:
        command = [
            sys.executable,
            str(self.script_dir / "decoder.py"),
            "--topic",
            topic,
            "--broker",
            self.args.decoder_broker,
            "--port",
            str(self.args.decoder_port),
            "--processor",
            self.args.decoder_processor,
            "--dlq-path",
            self.args.decoder_dlq_path,
        ]
        if self.args.decoder_processor == "sqlite":
            command.extend(["--sqlite-path", self.args.decoder_sqlite_path])
        return command

    def spawn_decoder(self, topic: str) -> None:
        proc = subprocess.Popen(self._decoder_command(topic))
        self.active_decoders[topic] = proc
        self.decoder_started_at[topic] = time.monotonic()
        logger.info(
            "Decoder spawned",
            extra={"extra": {"machine_sn": self.config.machine_sn, "topic": topic, "pid": proc.pid}},
        )

    def terminate_decoder(self, topic: str) -> None:
        proc = self.active_decoders.pop(topic, None)
        self.decoder_started_at.pop(topic, None)
        if proc is None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        logger.info(
            "Decoder terminated",
            extra={"extra": {"machine_sn": self.config.machine_sn, "topic": topic, "pid": proc.pid}},
        )

    def _mark_crash(self, topic: str, proc: subprocess.Popen) -> None:
        now = time.monotonic()
        state = self.decoder_failures.setdefault(topic, {"count": 0, "last_crash": 0.0})
        if now - state["last_crash"] > self.args.decoder_restart_backoff_window:
            state["count"] = 0
        state["count"] += 1
        state["last_crash"] = now
        self.metrics.increment_decoder_crashes()
        logger.warning(
            "Decoder crashed; will evaluate restart backoff",
            extra={
                "extra": {
                    "machine_sn": self.config.machine_sn,
                    "topic": topic,
                    "pid": proc.pid,
                    "returncode": proc.returncode,
                    "crash_count": state["count"],
                }
            },
        )

    def _restart_allowed(self, topic: str) -> bool:
        state = self.decoder_failures.get(topic)
        if not state:
            return True
        now = time.monotonic()
        if now - state["last_crash"] > self.args.decoder_restart_backoff_window:
            self.decoder_failures.pop(topic, None)
            return True
        if state["count"] < self.args.decoder_crash_threshold:
            return True
        logger.warning(
            "Decoder restart deferred by backoff policy",
            extra={
                "extra": {
                    "machine_sn": self.config.machine_sn,
                    "topic": topic,
                    "crash_count": state["count"],
                    "backoff_window_s": self.args.decoder_restart_backoff_window,
                }
            },
        )
        return False

    def reconcile_decoders(self, desired_topics: set[str], delta: int) -> None:
        with self.lock:
            for topic, proc in list(self.active_decoders.items()):
                if proc.poll() is not None:
                    self.active_decoders.pop(topic, None)
                    self.decoder_started_at.pop(topic, None)
                    self._mark_crash(topic, proc)
                elif time.monotonic() - self.decoder_started_at.get(topic, 0.0) > self.args.interval:
                    self.decoder_failures.pop(topic, None)

            for topic in set(self.active_decoders) - desired_topics:
                self.terminate_decoder(topic)

            for topic in sorted(desired_topics - set(self.active_decoders)):
                if self._restart_allowed(topic):
                    self.spawn_decoder(topic)

            self.metrics.record_context(
                self.config.machine_sn,
                self.config.serial_port,
                delta=delta,
                active_decoders=len(self.active_decoders),
                healthy=self.healthy,
                last_error=self.last_error,
            )

    def measure_and_reconcile(self) -> None:
        if self.bytes_table is None:
            raise RuntimeError("bytes table is not initialised")
        current_total = sum(value.value for _, value in self.bytes_table.items())
        self.bytes_table.clear()
        desired = determine_topics(current_total, self.config)
        logger.info(
            "Interval measurement",
            extra={
                "extra": {
                    "machine_sn": self.config.machine_sn,
                    "serial": self.config.serial_port,
                    "delta_bytes": current_total,
                    "interval_s": self.args.interval,
                    "active_decoders": len(self.active_decoders),
                }
            },
        )
        self.metrics.record_context(
            self.config.machine_sn,
            self.config.serial_port,
            delta=current_total,
            active_decoders=len(self.active_decoders),
            healthy=self.healthy,
            last_error=self.last_error,
        )
        if self.args.dry_run:
            logger.info(
                "[Dry Run] Desired decoder topics",
                extra={"extra": {"machine_sn": self.config.machine_sn, "desired_topics": sorted(desired), "delta_bytes": current_total}},
            )
            return
        reconcile_event = "High flow detected; reconciling decoders" if current_total >= self.config.min_delta else "Data flow within normal range; scaling to single wildcard decoder."
        logger.info(
            reconcile_event,
            extra={"extra": {"machine_sn": self.config.machine_sn, "desired_topics": sorted(desired), "delta_bytes": current_total}},
        )
        self.reconcile_decoders(desired, current_total)

    def close(self) -> None:
        with self.lock:
            for topic in list(self.active_decoders):
                self.terminate_decoder(topic)
        if self.bpf is not None:
            try:
                self.bpf.cleanup()
            except Exception:
                pass
        self.metrics.remove_context(self.config.machine_sn, self.config.serial_port)


class Adjuster:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.metrics = MetricsState()
        self.stop_event = threading.Event()
        self.reload_event = threading.Event()
        self.watchdog = WatchdogNotifier()
        self.server = MonitoringServer(args.metrics_host, args.metrics_port, self.metrics)
        self.contexts: dict[tuple[str, str], MachineRuntime] = {}
        self.kprobe_checked = False

    def ensure_environment(self) -> None:
        if not os.path.exists("/sys/fs/bpf"):
            logger.error("bpffs is not mounted. Run: sudo mount -t bpf bpffs /sys/fs/bpf")
            sys.exit(1)
        if BPF is None:
            logger.error("bcc is not installed; unable to load eBPF monitoring.")
            sys.exit(1)
        kprobe_fns = BPF.get_kprobe_functions(b"tty_read")
        if not kprobe_fns:
            logger.error("tty_read not found in kprobe functions.")
            sys.exit(1)
        self.kprobe_checked = True

    def install_signal_handlers(self) -> None:
        def _handle_stop(signum, frame):
            logger.info("Signal received; stopping adjuster.", extra={"extra": {"signal": signum}})
            self.stop_event.set()

        def _handle_reload(signum, frame):
            logger.info("SIGHUP received; scheduling configuration reload.")
            self.reload_event.set()

        signal.signal(signal.SIGTERM, _handle_stop)
        signal.signal(signal.SIGINT, _handle_stop)
        signal.signal(signal.SIGHUP, _handle_reload)

    def load_machine_configs(self) -> list[MachineConfig]:
        defaults = MachineConfig(
            machine_sn=self.args.machine_sn,
            serial_port=self.args.serial,
            min_delta=self.args.min_delta,
            max_module=self.args.max_module,
            max_unit=self.args.max_unit,
        )
        if not self.args.config:
            return [defaults]
        config_path = Path(self.args.config)
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        machines = raw.get("machines", [])
        if not isinstance(machines, list) or not machines:
            raise ValueError("config file must contain a non-empty 'machines' list")
        return [MachineConfig.from_dict(machine, defaults) for machine in machines]

    def reconcile_contexts(self, configs: list[MachineConfig]) -> None:
        desired_keys = {config.key for config in configs}
        for key in list(self.contexts):
            if key not in desired_keys:
                logger.info("Removing machine context", extra={"extra": {"machine_sn": key[0], "serial": key[1]}})
                self.contexts.pop(key).close()
        for config in configs:
            key = config.key
            existing = self.contexts.get(key)
            if existing and existing.config == config:
                continue
            if existing:
                logger.info("Reloading machine context", extra={"extra": {"machine_sn": config.machine_sn, "serial": config.serial_port}})
                existing.close()
            runtime = MachineRuntime(config, self.args, self.metrics)
            try:
                runtime.start()
            except Exception as exc:
                runtime.last_error = str(exc)
                self.metrics.record_attach_error(config.machine_sn, config.serial_port, str(exc))
                logger.error(
                    "Failed to start machine context",
                    extra={"extra": {"machine_sn": config.machine_sn, "serial": config.serial_port, "error": str(exc)}},
                )
                continue
            self.contexts[key] = runtime

    def reload_configs(self, status: str) -> None:
        try:
            configs = self.load_machine_configs()
            self.reconcile_contexts(configs)
            self.metrics.set_reload_status(status)
            logger.info("Configuration loaded", extra={"extra": {"machine_count": len(self.contexts), "source": self.args.config or "cli"}})
        except Exception as exc:
            self.metrics.set_reload_status("failed")
            logger.error("Configuration reload failed", extra={"extra": {"error": str(exc)}})

    def run(self) -> int:
        self.ensure_environment()
        self.install_signal_handlers()
        self.server.start()
        self.reload_configs("loaded")
        self.watchdog.ready()
        self.watchdog.status("Monitoring tty_read flow")
        logger.info(
            "Monitor loop started",
            extra={"extra": {"interval_s": self.args.interval, "config": self.args.config, "machine_count": len(self.contexts)}},
        )
        while not self.stop_event.wait(self.args.interval):
            if self.reload_event.is_set():
                self.reload_event.clear()
                self.reload_configs("reloaded")
            for runtime in list(self.contexts.values()):
                try:
                    runtime.measure_and_reconcile()
                except Exception as exc:
                    runtime.last_error = str(exc)
                    self.metrics.record_context(
                        runtime.config.machine_sn,
                        runtime.config.serial_port,
                        delta=0,
                        active_decoders=len(runtime.active_decoders),
                        healthy=False,
                        last_error=str(exc),
                    )
                    logger.error(
                        "Machine context measurement failed",
                        extra={"extra": {"machine_sn": runtime.config.machine_sn, "serial": runtime.config.serial_port, "error": str(exc)}},
                    )
            self.watchdog.ping_if_due()
        self.shutdown()
        return 0

    def shutdown(self) -> None:
        logger.info("Shutting down; terminating all decoder processes.")
        for runtime in list(self.contexts.values()):
            runtime.close()
        self.server.stop()


def determine_topics(delta: int, config: MachineConfig) -> set[str]:
    if delta < config.min_delta:
        return {f"{config.machine_sn}/#"}
    if delta < 2 * config.min_delta:
        return {f"{config.machine_sn}/{module}/#" for module in range(1, config.max_module + 1)}
    return {
        f"{config.machine_sn}/{module}/{unit}/#"
        for module in range(1, config.max_module + 1)
        for unit in range(1, config.max_unit + 1)
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Integrated eBPF monitor and adjuster: measure tty_read bytes and dynamically "
            "spawn/terminate decoder processes based on PLC point flow."
        )
    )
    parser.add_argument("--serial", type=str, default="all", help="Specify the serial port name/path to filter.")
    parser.add_argument("--interval", type=int, default=60, help="Measurement interval in seconds.")
    parser.add_argument("--min_delta", type=int, default=8192, help="Minimum bytes read per interval to trigger scaling.")
    parser.add_argument("--max_module", type=int, default=16, help="Maximum module ID.")
    parser.add_argument("--max_unit", type=int, default=8, help="Maximum unit ID.")
    parser.add_argument("--machine_sn", type=str, default="1", help="Machine serial number used as the first MQTT topic level.")
    parser.add_argument("--dry_run", action="store_true", help="Enable dry run mode.")
    parser.add_argument("--config", type=str, help="Optional YAML file containing a machines list for multi-machine monitoring.")
    parser.add_argument("--metrics-host", type=str, default="127.0.0.1", help="Host interface for /healthz and /metrics.")
    parser.add_argument("--metrics-port", type=int, default=9108, help="Port for /healthz and /metrics.")
    parser.add_argument("--decoder-broker", type=str, default="localhost", help="Broker passed to spawned decoders.")
    parser.add_argument("--decoder-port", type=int, default=1883, help="Broker port passed to spawned decoders.")
    parser.add_argument("--decoder-processor", choices=["lineprotocol", "sqlite"], default="lineprotocol", help="Processor used by spawned decoders.")
    parser.add_argument("--decoder-sqlite-path", type=str, default="/var/lib/plc-edgeflow/points.db", help="SQLite destination for decoder processor mode sqlite.")
    parser.add_argument("--decoder-dlq-path", type=str, default="/var/lib/plc-edgeflow/decoder-dlq.db", help="SQLite dead-letter queue path for spawned decoders.")
    parser.add_argument("--decoder-crash-threshold", type=int, default=3, help="Crash count threshold before restart backoff is enforced.")
    parser.add_argument("--decoder-restart-backoff-window", type=int, default=60, help="Backoff window in seconds for repeated decoder crashes.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return Adjuster(args).run()


if __name__ == "__main__":
    sys.exit(main())
