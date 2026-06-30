# eBPF 嵌入式 PLC 點位採集器流量管控

[![CI](https://github.com/http418imateapot/plc-ebpf-autoscaler/actions/workflows/ci.yml/badge.svg)](https://github.com/http418imateapot/plc-ebpf-autoscaler/actions/workflows/ci.yml)
[![SQL Lint](https://github.com/http418imateapot/plc-ebpf-autoscaler/actions/workflows/sql-lint.yml/badge.svg)](https://github.com/http418imateapot/plc-ebpf-autoscaler/actions/workflows/sql-lint.yml)
[![GitHub release](https://img.shields.io/github/v/release/http418imateapot/plc-ebpf-autoscaler)](https://github.com/http418imateapot/plc-ebpf-autoscaler/releases)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## 簡介

### 問題背景

本專案針對半導體高頻量測製程中，以單板電腦開發的 PLC 採集轉拋模組，在訊號採集後產生的資料擁塞問題進行改善。利用 eBPF 在 Linux 核心層即時監控數據流量 (PLC 點位採集資料來自串列埠，並轉發緩存至 MQTT topic)，與採用 Sidecar 架構整合既有系統。當監控到流量超標時，即動態調整點位資料消化程式的實例數量、MQTT Topic 訂閱策略，以實現資源的自動調配。

### 實作方案

* **即時流量監控**：使用 eBPF kretprobe 監控 `tty_read` 實際回傳的 **位元組數**，精確反映真實資料量。
* **動態資源調控**：依位元組流量門檻，調控點位消化程式的實例數量及 MQTT Topic 訂閱策略（支援擴容與縮容）。
* **橫向擴展**：不修改現有點位資料消化程式，以 Sidecar 架構實現動態擴展。
* **穩定部署**：提供 systemd 服務單元，支援開機自啟、崩潰自復原、最小權限執行。

### 專案架構

```
plc-ebpf-autoscaler/
├── README.md                      # 本文件
├── CHANGELOG.md                   # 版本異動紀錄
├── CONTRIBUTING.md                # 貢獻指南
├── SECURITY.md                    # 安全漏洞回報政策
├── LICENSE                        # MIT License
├── pyproject.toml                 # 套件定義（PEP 517/518）
├── requirements.txt               # 鎖定版本依賴（pip install -r 用）
├── decoder.py                     # PLC 採集點位資料消化、解碼程式
├── adjust.py                      # PLC 點位採集資訊量監測、點位消化調控程式
├── tests/
│   ├── test_adjust.py
│   └── test_decoder.py
└── systemd/
    ├── plc-adjust.service         # systemd unit (adjust.py)
    └── plc-decoder@.service       # systemd template unit (decoder.py)
```

---

## 系統需求與安裝

### 系統需求

* **作業系統**：Linux (Kernel 4.18+，建議 5.8+ 以支援 `CAP_PERFMON`)
* **MQTT Broker**：Mosquitto 或其他相容的 MQTT broker
* **開發工具**
    * Python 3.10+、pip
    * BCC 0.29.1 (用於 eBPF 程式開發)

### 方法 A：pip 安裝（推薦）

> **注意：** BCC 為系統套件，需透過 apt 安裝，不可用 pip 取代。

#### 1. 安裝系統依賴

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-pip mosquitto \
    bpfcc-tools linux-headers-$(uname -r)
sudo systemctl enable --now mosquitto
```

#### 2. 安裝本套件

```bash
# 從 GitHub 安裝（正式部署）
pip install git+https://github.com/http418imateapot/plc-ebpf-autoscaler.git

# 或從本機原始碼安裝（開發模式）
pip install -e ".[dev]"
```

安裝後即可使用 `plc-adjust` 與 `plc-decoder` 命令：

```bash
plc-adjust --help
plc-decoder --help
```

#### 3. 建立資料目錄

```bash
sudo mkdir -p /var/lib/plc-edgeflow
```

---

### 方法 B：手動安裝（無 pip 環境）

#### 安裝並啟用 MQTT broker (Mosquitto)

```bash
sudo apt-get install mosquitto
sudo systemctl enable mosquitto
sudo systemctl start mosquitto
```

#### 更新並安裝 Python 3 與 pip

```bash
sudo apt-get update
sudo apt-get install python3 python3-pip
```

#### 安裝 BCC 工具及相關 header

```bash
sudo apt-get install bpfcc-tools linux-headers-$(uname -r)
```

#### 安裝 Python 相依套件

```bash
pip3 install -r requirements.txt
mkdir -p /var/lib/plc-edgeflow
```

---

## 專案情境說明

### 點位採集頻率與資料量

本專案預設情境：每一機台有 16 個模組 × 8 個單元 × 4 個點位（共 512 點），每點 16 bytes，總計 **8192 bytes/s** 的採集數據。點位採集透過單板電腦的 COM / Serial Port 通訊。

### MQTT Topic 結構

```
{machine_sn}/{module_id}/{unit_id}/{point_id}
```

`adjust.py` 依流量分三個層級訂閱（均符合 MQTT 3.1.1 規範）：

| 流量層級 | 訂閱 Topic 範例 |
|---|---|
| 低（< min_delta） | `{sn}/#` |
| 中（< 2×min_delta） | `{sn}/1/#`, `{sn}/2/#`, … |
| 高（≥ 2×min_delta） | `{sn}/1/1/#`, `{sn}/1/2/#`, … |

---

## 使用說明

### 方法一：直接執行（開發 / 測試用）

#### 啟動監測與調控程式

```bash
sudo python3 adjust.py
```

完整參數說明：

```
usage: adjust.py [-h] [--serial SERIAL] [--interval INTERVAL]
                 [--min_delta MIN_DELTA] [--max_module MAX_MODULE]
                 [--max_unit MAX_UNIT] [--machine_sn MACHINE_SN] [--dry_run]

Integrated eBPF monitor and adjuster: measure tty_read bytes and
dynamically spawn/terminate decoder processes based on PLC point flow.

options:
  -h, --help                show this help message and exit
  --serial SERIAL           Serial port name to filter (e.g., 'ttyACM0').
                            Default 'all' means no filtering.
  --interval INTERVAL       Measurement interval in seconds (default: 60).
  --min_delta MIN_DELTA     Minimum bytes per interval to trigger scaling
                            (default: 8192 — 512 points × 16 bytes).
  --max_module MAX_MODULE   Maximum module ID (default: 16).
  --max_unit MAX_UNIT       Maximum unit ID (default: 8).
  --machine_sn MACHINE_SN  Machine serial number for MQTT topic prefix
                            (default: '1').
  --dry_run                 Print intended actions without spawning processes.
```

#### Dry-run 測試（不影響生產線）

```bash
python3 adjust.py --dry_run --interval 5 --machine_sn TEST01
```

### 方法二：systemd 部署（生產環境推薦）

#### 方式 A — 透過 pip 安裝後部署

```bash
# 建立低權限服務帳號
sudo useradd --system --no-create-home plcmon

# 安裝套件（讓 plc-adjust / plc-decoder console scripts 進入 PATH）
sudo pip install git+https://github.com/http418imateapot/plc-ebpf-autoscaler.git

# 更新 ExecStart 為 console script 路徑（查詢安裝位置）
which plc-adjust   # 通常為 /usr/local/bin/plc-adjust

# 安裝 systemd 服務（ExecStart 已預設指向 /usr/bin/python3 /opt/plc-edgeflow/adjust.py，
# 若使用 pip 安裝請將其改為 /usr/local/bin/plc-adjust）
sudo cp systemd/plc-adjust.service /etc/systemd/system/
sudo cp systemd/plc-decoder@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now plc-adjust
```

#### 方式 B — 直接複製腳本部署

```bash
# 建立低權限服務帳號
sudo useradd --system --no-create-home plcmon

# 部署程式
sudo mkdir -p /opt/plc-edgeflow
sudo cp adjust.py decoder.py /opt/plc-edgeflow/
sudo chown -R plcmon:plcmon /opt/plc-edgeflow

# 安裝 systemd 服務
sudo cp systemd/plc-adjust.service /etc/systemd/system/
sudo cp systemd/plc-decoder@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now plc-adjust
```

站點環境變數可透過 `/etc/plc-edgeflow/adjust.env` 覆蓋預設值：

```bash
sudo mkdir -p /etc/plc-edgeflow
sudo tee /etc/plc-edgeflow/adjust.env <<EOF
PLC_SERIAL=ttyACM0
PLC_MACHINE_SN=FAB01-TOOL42
PLC_INTERVAL=60
PLC_MIN_DELTA=8192
PLC_METRICS_HOST=127.0.0.1
PLC_METRICS_PORT=9108
PLC_DECODER_PROCESSOR=lineprotocol
PLC_DECODER_SQLITE_PATH=/var/lib/plc-edgeflow/points.db
PLC_DECODER_DLQ_PATH=/var/lib/plc-edgeflow/decoder-dlq.db
EOF
```

---

## 日誌格式

所有程式輸出為 **JSON 結構化日誌**，可直接串接 `journald`、Loki、或任何 JSON 日誌聚合器：

```json
{"ts": "2025-01-01T12:00:00", "level": "INFO", "event": "Interval measurement", "delta_bytes": 9500, "interval_s": 60, "active_decoders": 3}
{"ts": "2025-01-01T12:00:00", "level": "INFO", "event": "Decoder spawned", "topic": "FAB01/2/3/#", "pid": 12345}
{"ts": "2025-01-01T12:01:00", "level": "INFO", "event": "Decoder terminated", "topic": "FAB01/2/3/#", "pid": 12345}
```

查詢 systemd journal：

```bash
journalctl -u plc-adjust -f -o json
```

---

## 開發說明

本專案使用 `.github/copilot-instructions.md` 作為 **GitHub Copilot SDD（軟體設計文件）**，記錄完整的編碼規範、架構決策與改版任務。Copilot Coding Agent 及 Copilot CLI 可直接讀取該文件並依規範產生符合專案標準的程式碼。

相關開源文件：

| 文件 | 說明 |
|------|------|
| [CHANGELOG.md](CHANGELOG.md) | 版本異動紀錄（Keep a Changelog 格式） |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 貢獻指南、開發環境設定 |
| [SECURITY.md](SECURITY.md) | 安全漏洞回報政策 |
| [LICENSE](LICENSE) | MIT License |

```bash
# 安裝開發依賴
pip install -e ".[dev]"

# 執行測試
python3 -m pytest
```



## 多機台設定與熱重載

`adjust.py` 現在支援可選的 YAML 設定檔，讓單一服務管理多台機台與多個串列埠：

```yaml
machines:
  - machine_sn: FAB01
    serial_port: ttyACM0
    min_delta: 8192
    max_module: 16
    max_unit: 8
  - machine_sn: FAB02
    serial_port: ttyUSB0
    min_delta: 4096
```

啟動時加入 `--config /etc/plc-edgeflow/config.yaml`。更新設定檔後送出 `SIGHUP` 即可熱重載，不必重啟整個監測服務。

## Decoder 處理管線

`decoder.py` 支援兩種處理模式：

- `--processor lineprotocol`：將正規化後的 PLC 點位資料轉為 InfluxDB line protocol，透過 JSON 結構化日誌輸出。
- `--processor sqlite`：將正規化後的 PLC 點位資料寫入 SQLite（WAL 模式）。

所有處理失敗的訊息都會寫入本地 SQLite dead-letter queue，避免 broker 短暫故障或資料格式異常時直接遺失。

## 健康檢查與監控

`adjust.py` 會提供兩個 HTTP 端點：

- `GET /healthz`：回傳當前 machine context 狀態與最近一次 reload 結果
- `GET /metrics`：Prometheus 格式指標，包含 `plc_bytes_delta`、`plc_active_decoders_count`、`plc_decoder_crashes_total`、`plc_ebpf_attach_errors_total`

範例：

```bash
curl http://127.0.0.1:9108/healthz
curl http://127.0.0.1:9108/metrics
```

## 開發測試

安裝完相依套件後可直接執行：

```bash
python3 -m pytest
```
