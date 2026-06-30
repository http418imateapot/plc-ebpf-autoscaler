#!/usr/bin/env python3
import json
import logging
import os
import argparse
import signal
import subprocess
import sys
import time
from bcc import BPF

# ---------------------------------------------------------------------------
# JSON structured logging
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# CLI arguments
# ---------------------------------------------------------------------------
parser = argparse.ArgumentParser(
    description=(
        "Integrated eBPF monitor and adjuster: measure tty_read bytes and "
        "dynamically spawn/terminate decoder processes based on PLC point flow."
    )
)
parser.add_argument(
    "--serial",
    type=str,
    default="all",
    help="Specify the serial port name/path to filter (e.g., 'ttyACM0'). Default 'all' means no filtering.",
)
parser.add_argument(
    "--interval",
    type=int,
    default=60,
    help="Measurement interval in seconds (default: 60 seconds).",
)
parser.add_argument(
    "--min_delta",
    type=int,
    default=8192,
    help=(
        "Minimum bytes read per interval to trigger spawning a new decoder process "
        "(default: 8192 bytes — matches 512 points × 16 bytes)."
    ),
)
parser.add_argument(
    "--max_module",
    type=int,
    default=16,
    help="Maximum module ID (default: 16).",
)
parser.add_argument(
    "--max_unit",
    type=int,
    default=8,
    help="Maximum unit ID (default: 8).",
)
parser.add_argument(
    "--machine_sn",
    type=str,
    default="1",
    help="Machine serial number used as the first MQTT topic level (default: '1').",
)
parser.add_argument(
    "--dry_run",
    action="store_true",
    help="Enable dry run mode: do not actually spawn decoder processes, only display test info.",
)
args = parser.parse_args()


def generate_filter_code(serial):
    """
    Generate eBPF C code to filter events by serial port device name.
    Each character is compared individually; mismatches cause an early return.
    """
    code = ""
    for i, char in enumerate(serial):
        code += f"    if (d_name[{i}] != '{char}') return 0;\n"
    return code


# Determine if filtering is required.
if args.serial.lower() != "all":
    filter_code = generate_filter_code(args.serial)
    logger.info("Serial port filter active", extra={"extra": {"serial": args.serial}})
else:
    filter_code = ""
    logger.info("No serial port filtering; measuring all tty_read bytes.")

# ---------------------------------------------------------------------------
# eBPF program — kretprobe accumulates actual bytes returned by tty_read
# ---------------------------------------------------------------------------
bpf_text = f"""
#include <uapi/linux/ptrace.h>
#include <linux/fs.h>
#include <linux/dcache.h>

BPF_HASH(bytes, u32);

int trace_tty_read_ret(struct pt_regs *ctx) {{
    // Retrieve the file pointer stored by the entry probe via a scratch map,
    // or re-read from the first argument via PT_REGS_PARM1 at entry.
    // For a kretprobe we use a per-CPU entry map to carry the file pointer.
    return 0;
}}

// Entry probe: stash the file pointer so the return probe can filter by name.
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
    if (!fp) {{
        return 0;
    }}
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

# ---------------------------------------------------------------------------
# Startup checks
# ---------------------------------------------------------------------------
if not os.path.exists("/sys/fs/bpf"):
    logger.error(
        "bpffs is not mounted. Run: sudo mount -t bpf bpffs /sys/fs/bpf"
    )
    sys.exit(1)

# Load the BPF program.
b = BPF(text=bpf_text)

# Attach entry kprobe to capture the file pointer, and kretprobe to read bytes.
kprobe_fns = BPF.get_kprobe_functions(b"tty_read")
if not kprobe_fns:
    logger.error("tty_read not found in kprobe functions.")
    sys.exit(1)

b.attach_kprobe(event="tty_read", fn_name="trace_tty_read_entry")
b.attach_kretprobe(event="tty_read", fn_name="trace_tty_read_exit")
logger.info("Attached kprobe+kretprobe to tty_read; starting byte-level monitoring.")

bytes_table = b.get_table("bytes")

# ---------------------------------------------------------------------------
# Topic determination (TASK-01 + TASK-03)
# ---------------------------------------------------------------------------
def determine_topics(delta):
    """
    Return the desired set of MQTT subscription topics given the bytes-per-interval delta.

    Tiers:
      - delta < min_delta           → one wildcard covering everything: "{sn}/#"
      - min_delta ≤ delta < 2×      → per-module wildcards: "{sn}/{mod}/#"
      - delta ≥ 2×min_delta         → per-module+unit topics: "{sn}/{mod}/{unit}/#"

    All returned topics are valid MQTT 3.1.1 filter strings
    (multi-level wildcard '#' appears only at the final level).
    """
    base = args.min_delta
    sn = args.machine_sn
    if delta < base:
        return {f"{sn}/#"}
    if delta < 2 * base:
        return {f"{sn}/{mod}/#" for mod in range(1, args.max_module + 1)}
    topics = set()
    for mod in range(1, args.max_module + 1):
        for unit in range(1, args.max_unit + 1):
            topics.add(f"{sn}/{mod}/{unit}/#")
    return topics


# ---------------------------------------------------------------------------
# Decoder process management (TASK-04 + TASK-05)
# ---------------------------------------------------------------------------
# Maps topic → active Popen; only one decoder per topic at a time.
active_decoders: dict = {}


def spawn_decoder(topic: str) -> None:
    """Start a decoder subprocess for *topic* and register it."""
    proc = subprocess.Popen(
        [sys.executable, os.path.join(os.path.dirname(__file__), "decoder.py"), "--topic", topic]
    )
    active_decoders[topic] = proc
    logger.info(
        "Decoder spawned",
        extra={"extra": {"topic": topic, "pid": proc.pid}},
    )


def terminate_decoder(topic: str) -> None:
    """Gracefully terminate the decoder for *topic*, then forcefully if needed."""
    proc = active_decoders.pop(topic, None)
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
        extra={"extra": {"topic": topic, "pid": proc.pid}},
    )


def reconcile_decoders(desired_topics: set) -> None:
    """
    Bring the set of running decoders in line with *desired_topics*.
    - Restart crashed decoders that are still desired.
    - Spawn missing decoders for desired topics.
    - Terminate decoders for topics no longer desired.
    """
    # Reap crashed processes still in active_decoders
    for topic, proc in list(active_decoders.items()):
        if proc.poll() is not None:
            logger.warning(
                "Decoder crashed; will restart",
                extra={"extra": {"topic": topic, "pid": proc.pid, "returncode": proc.returncode}},
            )
            del active_decoders[topic]

    # Terminate decoders no longer needed
    for topic in set(active_decoders) - desired_topics:
        terminate_decoder(topic)

    # Spawn decoders for topics not yet running
    for topic in desired_topics - set(active_decoders):
        spawn_decoder(topic)


def shutdown(signum=None, frame=None) -> None:
    """Terminate all decoders cleanly on SIGTERM / SIGINT."""
    logger.info("Shutting down; terminating all decoder processes.")
    for topic in list(active_decoders):
        terminate_decoder(topic)
    sys.exit(0)


signal.signal(signal.SIGTERM, shutdown)

# ---------------------------------------------------------------------------
# Measurement and reconciliation loop
# ---------------------------------------------------------------------------
previous_total = 0
logger.info(
    "Monitor loop started",
    extra={"extra": {"interval_s": args.interval, "min_delta_bytes": args.min_delta, "machine_sn": args.machine_sn}},
)

while True:
    try:
        time.sleep(args.interval)
    except KeyboardInterrupt:
        shutdown()

    current_total = sum(v.value for k, v in bytes_table.items())
    delta = current_total - previous_total
    previous_total = current_total

    logger.info(
        "Interval measurement",
        extra={"extra": {"delta_bytes": delta, "interval_s": args.interval, "active_decoders": len(active_decoders)}},
    )

    desired = determine_topics(delta) if delta >= args.min_delta else {f"{args.machine_sn}/#"}

    if args.dry_run:
        logger.info(
            "[Dry Run] Desired decoder topics",
            extra={"extra": {"desired_topics": sorted(desired), "delta_bytes": delta}},
        )
    else:
        if delta >= args.min_delta:
            logger.info(
                "High flow detected; reconciling decoders",
                extra={"extra": {"delta_bytes": delta, "desired_topics": sorted(desired)}},
            )
        else:
            logger.info(
                "Data flow within normal range; scaling to single wildcard decoder.",
                extra={"extra": {"delta_bytes": delta}},
            )
        reconcile_decoders(desired)

