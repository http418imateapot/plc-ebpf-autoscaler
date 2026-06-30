#!/usr/bin/env python3
import argparse
import json
import logging
import signal
import sys
import threading
import time

import paho.mqtt.client as mqtt

# ---------------------------------------------------------------------------
# JSON structured logging (mirrors adjust.py)
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

# Default subscription topic: wildcard for all machines.
DEFAULT_TOPIC = "#"

# ---------------------------------------------------------------------------
# Reconnect state
# ---------------------------------------------------------------------------
_reconnect_delay = 2      # seconds; doubles on each failure, capped at 60
_reconnect_max   = 60


def on_connect(client, userdata, flags, rc):
    """Subscribe to the configured topic on successful connection."""
    global _reconnect_delay
    if rc == 0:
        _reconnect_delay = 2  # reset back-off on successful connect
        topic = userdata.get("topic", DEFAULT_TOPIC)
        client.subscribe(topic)
        logger.info(
            "Connected and subscribed",
            extra={"extra": {"topic": topic}},
        )
    else:
        logger.error(
            "MQTT connect failed",
            extra={"extra": {"rc": rc}},
        )


def on_disconnect(client, userdata, rc):
    """
    Handle broker disconnection with exponential back-off reconnect.
    rc == 0 is a clean disconnect (e.g., SIGTERM); rc != 0 is unexpected.
    """
    global _reconnect_delay
    if rc == 0:
        logger.info("MQTT disconnected cleanly.")
        return
    logger.warning(
        "MQTT disconnected unexpectedly; will reconnect",
        extra={"extra": {"rc": rc, "retry_in_s": _reconnect_delay}},
    )
    while True:
        time.sleep(_reconnect_delay)
        _reconnect_delay = min(_reconnect_delay * 2, _reconnect_max)
        try:
            client.reconnect()
            logger.info("MQTT reconnected successfully.")
            return
        except Exception as exc:
            logger.warning(
                "Reconnect attempt failed; retrying",
                extra={"extra": {"error": str(exc), "next_retry_s": _reconnect_delay}},
            )


def on_message(client, userdata, msg):
    """
    Decode and process an incoming MQTT PLC point message.
    Logs per-message receive time and cumulative message count.
    """
    t_recv = time.monotonic()
    userdata["msg_count"] = userdata.get("msg_count", 0) + 1
    try:
        payload_str = msg.payload.decode()
        data = json.loads(payload_str)
        latency_ms = round((time.monotonic() - t_recv) * 1000, 2)
        logger.info(
            "Message received",
            extra={
                "extra": {
                    "topic": msg.topic,
                    "msg_count": userdata["msg_count"],
                    "latency_ms": latency_ms,
                    "data": data,
                }
            },
        )
        # TODO: replace with real processing logic (transform, persist, forward)
    except Exception as exc:
        logger.error(
            "Error processing message",
            extra={"extra": {"topic": msg.topic, "error": str(exc)}},
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="MQTT PLC point data decoder with auto-reconnect and structured logging."
    )
    parser.add_argument(
        "--topic",
        type=str,
        default=DEFAULT_TOPIC,
        help=f"MQTT subscription topic (default: '{DEFAULT_TOPIC}')",
    )
    parser.add_argument(
        "--broker",
        type=str,
        default="localhost",
        help="MQTT broker hostname or IP (default: 'localhost').",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=1883,
        help="MQTT broker port (default: 1883).",
    )
    args = parser.parse_args()

    userdata = {"topic": args.topic, "msg_count": 0}
    client = mqtt.Client(userdata=userdata)
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message

    # Use a threading.Event so SIGTERM can cleanly stop the loop.
    stop_event = threading.Event()

    def _handle_stop(signum, frame):
        logger.info("Signal received; stopping decoder.")
        stop_event.set()
        client.disconnect()

    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    logger.info(
        "Decoder starting",
        extra={"extra": {"topic": args.topic, "broker": args.broker, "port": args.port}},
    )

    try:
        client.connect(args.broker, args.port, keepalive=60)
    except Exception as exc:
        logger.error(
            "Initial broker connection failed",
            extra={"extra": {"broker": args.broker, "port": args.port, "error": str(exc)}},
        )
        sys.exit(1)

    client.loop_start()
    stop_event.wait()
    client.loop_stop()
    logger.info(
        "Decoder stopped",
        extra={"extra": {"total_messages": userdata.get("msg_count", 0)}},
    )


if __name__ == "__main__":
    main()

