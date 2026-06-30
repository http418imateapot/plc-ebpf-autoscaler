import json
import sqlite3
from pathlib import Path

from decoder import DeadLetterQueue, PointRecord, SQLiteProcessor, build_line_protocol, normalize_payload


def test_normalize_payload_uses_payload_and_topic_fallback():
    point = normalize_payload(
        {"machine_sn": "FAB01", "module": "2", "unit": "4", "value": 12.5},
        "FAB01/2/4/pressure",
        "2026-01-01T00:00:00+00:00",
    )
    assert point.machine_sn == "FAB01"
    assert point.module == "2"
    assert point.unit == "4"
    assert point.point_id == "pressure"
    assert point.value == 12.5


def test_sqlite_processor_persists_processed_points(tmp_path: Path):
    db_path = tmp_path / "points.db"
    processor = SQLiteProcessor(str(db_path))
    point = PointRecord(
        machine_sn="FAB01",
        module="1",
        unit="2",
        point_id="temp",
        topic="FAB01/1/2/temp",
        value=42,
        timestamp="2026-01-01T00:00:00+00:00",
        received_at="2026-01-01T00:00:00+00:00",
    )

    processor.process(point, {"value": 42})
    processor.close()

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT machine_sn, module, unit, point_id, point_value FROM processed_points"
    ).fetchone()
    conn.close()
    assert row == ("FAB01", "1", "2", "temp", json.dumps(42))


def test_dead_letter_queue_records_failures(tmp_path: Path):
    db_path = tmp_path / "dlq.db"
    dlq = DeadLetterQueue(str(db_path))

    dlq.record("FAB01/1/#", "bad-payload", "boom", "2026-01-01T00:00:00+00:00")
    dlq.close()

    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT topic, payload, error FROM dead_letters").fetchone()
    conn.close()
    assert row == ("FAB01/1/#", "bad-payload", "boom")


def test_build_line_protocol_formats_tags_and_field():
    point = PointRecord(
        machine_sn="FAB01",
        module="1",
        unit="2",
        point_id="temp",
        topic="FAB01/1/2/temp",
        value=42,
        timestamp="2026-01-01T00:00:00+00:00",
        received_at="2026-01-01T00:00:00+00:00",
    )

    line = build_line_protocol(point)

    assert line.startswith("plc_point,machine_sn=FAB01,module=1,unit=2,point_id=temp value=42i ")
