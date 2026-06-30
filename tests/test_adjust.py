from pathlib import Path
from types import SimpleNamespace

from adjust import Adjuster, MachineConfig, MetricsState, determine_topics


def test_determine_topics_returns_expected_tiers():
    config = MachineConfig(machine_sn="FAB01", serial_port="ttyACM0", min_delta=10, max_module=2, max_unit=2)

    assert determine_topics(5, config) == {"FAB01/#"}
    assert determine_topics(10, config) == {"FAB01/1/#", "FAB01/2/#"}
    assert determine_topics(25, config) == {
        "FAB01/1/1/#",
        "FAB01/1/2/#",
        "FAB01/2/1/#",
        "FAB01/2/2/#",
    }


def test_metrics_state_renders_health_and_prometheus():
    metrics = MetricsState()
    metrics.record_context("FAB01", "ttyACM0", delta=123, active_decoders=2, healthy=True)

    health = metrics.health_snapshot()
    payload = metrics.render_prometheus()

    assert health["status"] == "ok"
    assert payload.count("plc_bytes_delta") == 2
    assert 'machine_sn="FAB01",serial_port="ttyACM0"' in payload


def test_adjuster_load_machine_configs_from_yaml(tmp_path: Path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "machines:\n"
        "  - machine_sn: FAB01\n"
        "    serial_port: ttyACM0\n"
        "    min_delta: 2048\n"
        "  - machine_sn: FAB02\n"
        "    serial: ttyUSB0\n",
        encoding="utf-8",
    )
    args = SimpleNamespace(
        serial="all",
        interval=60,
        min_delta=8192,
        max_module=16,
        max_unit=8,
        machine_sn="1",
        dry_run=True,
        config=str(config_path),
        metrics_host="127.0.0.1",
        metrics_port=0,
        decoder_broker="localhost",
        decoder_port=1883,
        decoder_processor="lineprotocol",
        decoder_sqlite_path=str(tmp_path / "points.db"),
        decoder_dlq_path=str(tmp_path / "dlq.db"),
        decoder_crash_threshold=3,
        decoder_restart_backoff_window=60,
    )

    adjuster = Adjuster(args)
    configs = adjuster.load_machine_configs()
    adjuster.server.stop()

    assert configs == [
        MachineConfig(machine_sn="FAB01", serial_port="ttyACM0", min_delta=2048, max_module=16, max_unit=8),
        MachineConfig(machine_sn="FAB02", serial_port="ttyUSB0", min_delta=8192, max_module=16, max_unit=8),
    ]
