# Contributing to plc-ebpf-autoscaler

Thank you for taking the time to contribute. This project targets a **semiconductor FAB production-line SBC**, so stability and correctness take priority over new features.

---

## Table of Contents

1. [Code of Conduct](#code-of-conduct)
2. [How to Contribute](#how-to-contribute)
3. [Development Setup](#development-setup)
4. [Coding Conventions](#coding-conventions)
5. [Testing](#testing)
6. [Submitting a Pull Request](#submitting-a-pull-request)
7. [Reporting Security Issues](#reporting-security-issues)

---

## Code of Conduct

Be respectful and constructive. Issues and pull requests that are disrespectful or off-topic will be closed.

---

## How to Contribute

| Type | Process |
|------|---------|
| Bug report | Open a GitHub Issue with steps to reproduce and the relevant JSON log output. |
| Feature request | Open a GitHub Issue first to discuss feasibility before writing code. |
| Documentation fix | Open a PR directly — no prior issue needed. |
| Security vulnerability | See [SECURITY.md](SECURITY.md) — **do not open a public issue**. |

---

## Development Setup

### Prerequisites

- Linux (kernel 5.x recommended); eBPF tests require root or `CAP_BPF`/`CAP_PERFMON`.
- Python 3.10 or later.
- [BCC toolchain](https://github.com/iovisor/bcc/blob/master/INSTALL.md) installed via system packages:

  ```bash
  sudo apt-get install bpfcc-tools linux-headers-$(uname -r)
  ```

- A running Mosquitto broker for integration tests:

  ```bash
  sudo apt-get install mosquitto
  sudo systemctl start mosquitto
  ```

### Install in editable mode

```bash
git clone https://github.com/http418imateapot/plc-ebpf-autoscaler.git
cd plc-ebpf-autoscaler

# Create and activate a virtual environment (recommended)
python3 -m venv .venv
source .venv/bin/activate

# Install runtime + dev dependencies
pip install -e ".[dev]"
```

> **BCC note:** `pip install -e ".[ebpf]"` installs the PyPI `bcc` stub.  
> For real eBPF functionality always prefer the system package (`python3-bpfcc`).

---

## Coding Conventions

Follow the rules documented in `.github/copilot-instructions.md`. Key points:

- **Python 3.10+** — use type hints, `match`, `|` union syntax.
- **Shebang** — `#!/usr/bin/env python3` on every script.
- **Logging** — always use the `JsonFormatter` / `logger.*` pattern; never `print()`.
- **MQTT topics** — validate against MQTT 3.1.1; `#` must be the final segment.
- **eBPF** — use `kretprobe` + `PT_REGS_RC(ctx)` for byte counts; never bare kprobe call counts.
- **Process lifecycle** — all spawn/terminate logic goes through `reconcile_decoders()`.
- **SIGTERM** — every script must handle it gracefully.

---

## Testing

Unit tests run without eBPF or a live MQTT broker:

```bash
python3 -m pytest
```

Integration smoke test (requires a running Mosquitto):

```bash
python3 adjust.py --dry_run --interval 5 --machine_sn TEST01
```

Before submitting a PR, ensure:

- All existing tests pass with no warnings.
- New behaviour is covered by at least one test.
- No secrets or credentials appear in committed files.

---

## Submitting a Pull Request

1. Fork the repository and create a branch off `main`:

   ```bash
   git checkout -b fix/your-short-description
   ```

2. Make your changes following the coding conventions above.

3. Reference the relevant SDD task ID (e.g., `TASK-04`) in commit messages where applicable.

4. Push the branch and open a PR against `main`. Fill in the PR template (title, description, testing notes).

5. A maintainer will review within a reasonable timeframe. Please respond promptly to review comments.

---

## Reporting Security Issues

See [SECURITY.md](SECURITY.md).
