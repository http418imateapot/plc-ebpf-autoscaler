#!/usr/bin/env python3
import argparse
import json
import logging
import signal
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import paho.mqtt.client as mqtt


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

DEFAULT_TOPIC = "#"


class ValidationError(Exception):
    pass


@dataclass(frozen=True)
class PointRecord:
    machine_sn: str
    module: str
    unit: str
    point_id: str
    topic: str
    value: Any
    timestamp: str
    received_at: str


class DeadLetterQueue:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dead_letters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic TEXT NOT NULL,
                payload TEXT NOT NULL,
                error TEXT NOT NULL,
                received_at TEXT NOT NULL
            )
            """
        )
        self.conn.commit()

    def record(self, topic: str, payload: Any, error: str, received_at: str) -> None:
        payload_text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, sort_keys=True)
        self.conn.execute(
            "INSERT INTO dead_letters (topic, payload, error, received_at) VALUES (?, ?, ?, ?)",
            (topic, payload_text, error, received_at),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


class LineProtocolProcessor:
    def process(self, point: PointRecord, payload: dict[str, Any]) -> None:
        line = build_line_protocol(point)
        logger.info(
            "Point processed",
            extra={
                "extra": {
                    "topic": point.topic,
                    "machine_sn": point.machine_sn,
                    "module": point.module,
                    "unit": point.unit,
                    "point_id": point.point_id,
                    "line_protocol": line,
                }
            },
        )

    def close(self) -> None:
        return


class SQLiteProcessor:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_points (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                machine_sn TEXT NOT NULL,
                module TEXT NOT NULL,
                unit TEXT NOT NULL,
                point_id TEXT NOT NULL,
                topic TEXT NOT NULL,
                point_value TEXT,
                point_ts TEXT NOT NULL,
                received_at TEXT NOT NULL,
                raw_payload TEXT NOT NULL
            )
            """
        )
        self.conn.commit()

    def process(self, point: PointRecord, payload: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT INTO processed_points (
                machine_sn, module, unit, point_id, topic, point_value, point_ts, received_at, raw_payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                point.machine_sn,
                point.module,
                point.unit,
                point.point_id,
                point.topic,
                json.dumps(point.value, ensure_ascii=False),
                point.timestamp,
                point.received_at,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
            ),
        )
        self.conn.commit()
        logger.info(
            "Point persisted",
            extra={
                "extra": {
                    "topic": point.topic,
                    "machine_sn": point.machine_sn,
                    "module": point.module,
                    "unit": point.unit,
                    "point_id": point.point_id,
                    "sqlite_path": str(self.path),
                }
            },
        )

    def close(self) -> None:
        self.conn.close()


class MqttDecoder:
    reconnect_initial = 2
    reconnect_max = 60

    def __init__(self, topic: str, broker: str, port: int, processor: LineProtocolProcessor | SQLiteProcessor, dlq: DeadLetterQueue) -> None:
        self.topic = topic
        self.broker = broker
        self.port = port
        self.processor = processor
        self.dlq = dlq
        self.msg_count = 0
        self.stop_event = threading.Event()
        self.reconnect_delay = self.reconnect_initial
        self.client = mqtt.Client(userdata={"decoder": self})
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

    @staticmethod
    def _on_connect(client, userdata, flags, rc):
        userdata["decoder"].on_connect(client, flags, rc)

    @staticmethod
    def _on_disconnect(client, userdata, rc):
        userdata["decoder"].on_disconnect(client, rc)

    @staticmethod
    def _on_message(client, userdata, msg):
        userdata["decoder"].on_message(msg)

    def on_connect(self, client: mqtt.Client, flags: dict[str, Any], rc: int) -> None:
        if rc == 0:
            self.reconnect_delay = self.reconnect_initial
            client.subscribe(self.topic)
            logger.info(
                "Connected and subscribed",
                extra={"extra": {"topic": self.topic, "broker": self.broker, "port": self.port}},
            )
            return
        logger.error("MQTT connect failed", extra={"extra": {"topic": self.topic, "rc": rc}})

    def on_disconnect(self, client: mqtt.Client, rc: int) -> None:
        if rc == 0 or self.stop_event.is_set():
            logger.info("MQTT disconnected cleanly.", extra={"extra": {"topic": self.topic}})
            return
        delay = self.reconnect_delay
        logger.warning(
            "MQTT disconnected unexpectedly; will reconnect",
            extra={"extra": {"topic": self.topic, "rc": rc, "retry_in_s": delay}},
        )
        while not self.stop_event.wait(delay):
            next_delay = min(delay * 2, self.reconnect_max)
            try:
                client.reconnect()
                logger.info("MQTT reconnected successfully.", extra={"extra": {"topic": self.topic}})
                self.reconnect_delay = self.reconnect_initial
                return
            except Exception as exc:  # pragma: no cover - network-dependent
                logger.warning(
                    "Reconnect attempt failed; retrying",
                    extra={"extra": {"topic": self.topic, "error": str(exc), "next_retry_s": next_delay}},
                )
                delay = next_delay
                self.reconnect_delay = delay

    def on_message(self, msg) -> None:
        received_at = utcnow_iso()
        start = time.monotonic()
        raw_payload = msg.payload.decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw_payload)
            if not isinstance(payload, dict):
                raise ValidationError("payload must be a JSON object")
            point = normalize_payload(payload, msg.topic, received_at)
            self.processor.process(point, payload)
            self.msg_count += 1
            logger.info(
                "Message received",
                extra={
                    "extra": {
                        "topic": msg.topic,
                        "msg_count": self.msg_count,
                        "latency_ms": round((time.monotonic() - start) * 1000, 2),
                        "machine_sn": point.machine_sn,
                        "module": point.module,
                        "unit": point.unit,
                        "point_id": point.point_id,
                    }
                },
            )
        except Exception as exc:
            self.dlq.record(msg.topic, raw_payload, str(exc), received_at)
            logger.error(
                "Error processing message",
                extra={"extra": {"topic": msg.topic, "error": str(exc), "dlq_path": str(self.dlq.path)}},
            )

    def stop(self) -> None:
        self.stop_event.set()
        self.client.disconnect()

    def close(self) -> None:
        self.processor.close()
        self.dlq.close()

    def run(self) -> int:
        def _handle_stop(signum, frame):
            logger.info("Signal received; stopping decoder.", extra={"extra": {"signal": signum, "topic": self.topic}})
            self.stop()

        signal.signal(signal.SIGTERM, _handle_stop)
        signal.signal(signal.SIGINT, _handle_stop)
        logger.info(
            "Decoder starting",
            extra={"extra": {"topic": self.topic, "broker": self.broker, "port": self.port}},
        )
        try:
            self.client.connect(self.broker, self.port, keepalive=60)
        except Exception as exc:
            logger.error(
                "Initial broker connection failed",
                extra={"extra": {"broker": self.broker, "port": self.port, "error": str(exc)}},
            )
            self.close()
            return 1
        self.client.loop_start()
        self.stop_event.wait()
        self.client.loop_stop()
        logger.info("Decoder stopped", extra={"extra": {"topic": self.topic, "total_messages": self.msg_count}})
        self.close()
        return 0


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def first_present(payload: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in payload and payload[key] not in (None, ""):
            return payload[key]
    return None


def normalize_payload(payload: dict[str, Any], topic: str, received_at: str) -> PointRecord:
    topic_parts = topic.split("/")
    machine_sn = first_present(payload, ("machine_sn", "machineSn")) or safe_topic_part(topic_parts, 0)
    module = first_present(payload, ("module", "module_id", "moduleId")) or safe_topic_part(topic_parts, 1)
    unit = first_present(payload, ("unit", "unit_id", "unitId")) or safe_topic_part(topic_parts, 2)
    point_id = first_present(payload, ("point_id", "pointId")) or safe_topic_part(topic_parts, 3)
    if not all([machine_sn, module, unit, point_id]):
        raise ValidationError("machine_sn, module, unit, and point_id are required")
    timestamp = str(first_present(payload, ("timestamp", "ts", "event_ts")) or received_at)
    return PointRecord(
        machine_sn=str(machine_sn),
        module=str(module),
        unit=str(unit),
        point_id=str(point_id),
        topic=topic,
        value=payload.get("value"),
        timestamp=timestamp,
        received_at=received_at,
    )


def safe_topic_part(parts: list[str], index: int) -> str | None:
    if len(parts) <= index:
        return None
    value = parts[index]
    if value in {"#", "+", ""}:
        return None
    return value


def escape_tag(value: str) -> str:
    return value.replace("\\", "\\\\").replace(" ", "\\ ").replace(",", "\\,").replace("=", "\\=")


def format_field_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return f"{value}i"
    if isinstance(value, float):
        return str(value)
    if value is None:
        return '""'
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def build_line_protocol(point: PointRecord) -> str:
    tags = ",".join(
        [
            f"machine_sn={escape_tag(point.machine_sn)}",
            f"module={escape_tag(point.module)}",
            f"unit={escape_tag(point.unit)}",
            f"point_id={escape_tag(point.point_id)}",
        ]
    )
    timestamp_ns = int(datetime.fromisoformat(point.received_at).timestamp() * 1_000_000_000)
    return f"plc_point,{tags} value={format_field_value(point.value)} {timestamp_ns}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="MQTT PLC point data decoder with auto-reconnect, processor plugins, and structured logging."
    )
    parser.add_argument("--topic", type=str, default=DEFAULT_TOPIC, help=f"MQTT subscription topic (default: '{DEFAULT_TOPIC}')")
    parser.add_argument("--broker", type=str, default="localhost", help="MQTT broker hostname or IP.")
    parser.add_argument("--port", type=int, default=1883, help="MQTT broker port.")
    parser.add_argument("--processor", choices=["lineprotocol", "sqlite"], default="lineprotocol", help="Point processor plugin to use.")
    parser.add_argument("--sqlite-path", type=str, default="/var/lib/plc-edgeflow/points.db", help="SQLite destination when using the sqlite processor.")
    parser.add_argument("--dlq-path", type=str, default="/var/lib/plc-edgeflow/decoder-dlq.db", help="SQLite dead-letter queue path.")
    return parser


def build_processor(args: argparse.Namespace) -> LineProtocolProcessor | SQLiteProcessor:
    if args.processor == "sqlite":
        return SQLiteProcessor(args.sqlite_path)
    return LineProtocolProcessor()


def main() -> int:
    args = build_parser().parse_args()
    processor = build_processor(args)
    dlq = DeadLetterQueue(args.dlq_path)
    decoder = MqttDecoder(args.topic, args.broker, args.port, processor, dlq)
    return decoder.run()


if __name__ == "__main__":
    sys.exit(main())
