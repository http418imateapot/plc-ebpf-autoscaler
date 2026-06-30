# eBPF 嵌入式 PLC 點位採集器流量管控

---

## 簡介

### 問題背景

本專案針對半導體高頻量測製程中，以單板電腦開發的 PLC 採集轉拋模組，在訊號採集後產生的資料擁塞問題進行改善。利用 eBPF 在 Linux 核心層即時監控數據流量 (PLC 點位採集資料來自串列埠，並轉發緩存至 MQTT topic)，與採用 Sidecar 架構整合既有系統。當監控到流量超標時，即動態調整點位資料消化程式的實例數量、MQTT Topic 訂閱策略，以實現資源的自動調配。

### 實作方案

* 即時流量監控：使用 eBPF 監控 PLC 點位採集資料流量。
* 動態資源調控：根據監控結果，調控點位消化程式的實例數量及 MQTT Topic 訂閱策略。
* 橫向擴展：不修改現有點位資料消化程式程式，以 Sidecar 架構實現動態擴展。

### 專案架構

```
plc-ebpf-autoscaler/
├── README.md                      # 本文件
├── requirements.txt               # 範例程式所需的 Python 套件
├── decoder.py                     # PLC 採集點位資料消化、解碼程式
├── adjust.py                      # PLC 點位採集資訊量監測、點位消化調控程式
└── systemd/
    ├── plc-adjust.service         # systemd unit (adjust.py)
    └── plc-decoder@.service       # systemd template unit (decoder.py)
```

## 系統需求與安裝

### 系統需求

* 作業系統： Linux (Kernel 4.18+，支援 eBPF)
* MQTT Broker： Mosquitto 或其他相容的 MQTT broker (本案例為安裝於單板電腦)
* 開發工具
    * Python 3.x, paho-mqtt
    * BCC 或 libbpf (用於 eBPF 程式開發)

### 安裝步驟

#### (非必要) 安裝並啟用 MQTT broker (Mosquitto)

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
sudo su
pip3 install -r requirements.txt
```

## 專案情境說明

### 點位採集頻率與資料量

本專案預設的情境為，每一機台有 16 個模組 × 8 個單元 × 4 個點位 -- 共 512 點，每點 16 bytes，總計 8192 bytes/s 的採集數據。點位採集方式，則有一個 PLC 點位採集硬體模組，透過單板電腦的 COM / Serial Port 通訊。

###  PLC 點位採集程式

點位採集模組與程式在此無法提供，但是您可以模擬採集模組的動作，先由 COM / Serial Port 讀取模擬點位資料，再將點位資料發布至 MQTT Topic "``{機台 SN}/{模組 IDˋ}/{單元 ID}/{點位 ID}``"、緩存於 MQTT Broker，然後等待點位資料消化、轉拋程式處理。


## 使用說明

### 啟動監測與調控程式

```bash
sudo su
python3 adjust.py
```

調控程式會根據機台點位採集流量 (COM / Serial Port)，調用啟動解碼程式、去訂閱適當的 MQTT Topic 路徑 (依照資料量判斷，訂閱單一或是個別 PLC 模組、單元的 Topic)、處理 PLC 點位資料。


### 監測調控程式 Usage

```bash!
python3 adjust.py -h
usage: adjust.py [-h] [--serial SERIAL] [--interval INTERVAL] [--min_delta MIN_DELTA] [--max_module MAX_MODULE] [--max_unit MAX_UNIT] [--dry_run]

Integrated eBPF monitor and adjuster: measure tty_read events and dynamically spawn decoder processes based on PLC point flow.

options:
  -h, --help            show this help message and exit
  --serial SERIAL       Specify the serial port name/path to filter (e.g., 'ttyACM0'). Default 'all' means no filtering.
  --interval INTERVAL   Measurement interval in seconds (default: 60 seconds).
  --min_delta MIN_DELTA
                        Minimum delta in tty_read calls to trigger spawning a new decoder process (default: 10).
  --max_module MAX_MODULE
                        Maximum module ID (default: 16).
  --max_unit MAX_UNIT   Maximum unit ID (default: 8).
  --dry_run             Enable dry run mode: do not actually spawn decoder processes, only display test info.

```
