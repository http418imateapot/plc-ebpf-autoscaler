# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 1.x     | ✅ Yes     |
| < 1.0   | ❌ No      |

## Scope

This policy covers the **plc-ebpf-autoscaler** codebase:

- `adjust.py` — eBPF monitor and decoder process lifecycle manager
- `decoder.py` — MQTT subscriber and PLC point data processor
- Supporting files (`systemd/`, `pyproject.toml`, etc.)

**Out of scope:** vulnerabilities in third-party dependencies (BCC, paho-mqtt, Mosquitto, the Linux kernel). Please report those to the respective upstream projects.

## Reporting a Vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**

Send a report by one of these methods:

1. **GitHub private vulnerability reporting (preferred)**  
   Use the [Security Advisories](https://github.com/http418imateapot/plc-ebpf-autoscaler/security/advisories/new) page to submit a draft advisory. Only repository maintainers can see it.

2. **Email**  
   If GitHub private reporting is unavailable, email the maintainer directly. You can find the contact address in the git commit history or in the `[authors]` section of `pyproject.toml`.

Please include:

- A description of the vulnerability and its potential impact.
- The affected version(s) and component(s).
- Steps to reproduce or a proof-of-concept.
- Any suggested mitigations or patches (optional).

## Response Timeline

| Phase | Target |
|-------|--------|
| Initial acknowledgement | Within 3 business days |
| Triage and severity assessment | Within 7 days |
| Patch or mitigation available | Within 30 days for critical/high; best-effort for lower |
| Public disclosure | Coordinated with reporter after fix is available |

## Security Considerations for Operators

This tool requires elevated Linux capabilities (`CAP_BPF`, `CAP_PERFMON`, optionally `CAP_SYS_ADMIN`) to attach eBPF programs. Operators should:

- Run the service under the dedicated low-privilege `plcmon` user as shown in the systemd unit files.
- Ensure `/sys/fs/bpf` is mounted and accessible only to required users.
- Keep the Linux kernel and BCC toolchain up to date.
- Restrict network access to the `/healthz` and `/metrics` HTTP endpoints (default: `127.0.0.1:9108`) using firewall rules.
- Rotate MQTT broker credentials and restrict broker ACLs to only the topics this service needs.
