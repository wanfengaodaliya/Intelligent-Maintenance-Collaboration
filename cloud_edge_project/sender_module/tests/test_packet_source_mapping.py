from pathlib import Path
from types import SimpleNamespace

from sender.config import SenderConfig, SenderNodeConfig
from sender.controller import run_sender_task
from sender.mat_reader import SignalWindow

from sender.source_mapping import (
    PacketSourceMappingStore,
    extract_paderborn_bearing_code,
)


def test_packet_source_mapping_preserves_exact_mat_window(tmp_path: Path):
    store = PacketSourceMappingStore(tmp_path / "packet_sources.db")
    store.save(
        packet_id="sd_01_tk_0001_bearing_01_pkt_001",
        task_id="sd_01_tk_0001",
        bearing_id="bearing_01",
        source_path=tmp_path / "N09_M07_F10_KA01_1.mat",
        start_index=0,
        end_index=3200,
        window_index=0,
    )

    assert store.get("sd_01_tk_0001_bearing_01_pkt_001") == {
        "packet_id": "sd_01_tk_0001_bearing_01_pkt_001",
        "task_id": "sd_01_tk_0001",
        "bearing_id": "bearing_01",
        "dataset_name": "paderborn",
        "dataset_version": "paderborn_v1",
        "source_file": "N09_M07_F10_KA01_1.mat",
        "source_bearing_code": "KA01",
        "start_index": 0,
        "end_index": 3200,
        "window_index": 0,
    }


def test_paderborn_code_comes_from_filename_structure():
    assert extract_paderborn_bearing_code("N09_M07_F10_KI04_1.mat") == "KI04"
    assert extract_paderborn_bearing_code("N09_M07_F10_K001_1.mat") == "K001"


def test_sender_task_saves_source_proof_when_it_creates_packet(
    tmp_path: Path, monkeypatch
):
    signals = {
        name: {"sample_rate_hz": rate, "sample_count": 1, "values": [1.0]}
        for name, rate in {
            "vibration": 64000,
            "phase_current_1_A": 64000,
            "phase_current_2_A": 64000,
            "shaft_speed_rpm": 4000,
            "load_torque_nm": 4000,
            "bearing_radial_load_n": 4000,
        }.items()
    }
    signals["bearing_module_temperature_c"] = 25.0
    source_path = tmp_path / "N09_M07_F10_KA01_1.mat"
    record = SimpleNamespace(
        source_path=source_path,
        windows=lambda **kwargs: iter(
            [
                SignalWindow(
                    sequence_number=1,
                    start_seconds=0.0,
                    end_seconds=0.05,
                    start_index=0,
                    end_index=3200,
                    window_index=0,
                    data=signals,
                )
            ]
        ),
    )
    monkeypatch.setattr("sender.controller.load_mat_record", lambda path: record)

    class Publisher:
        reconnect_count = 0
        publish_retry_total = 0

        def __init__(self):
            self.status_counts = {}

        def start(self):
            pass

        def publish(self, packet, payload, topic):
            self.status_counts = {"confirmed": 1}

        def wait_until_settled(self, timeout):
            pass

        def stop(self):
            pass

    class Sink:
        def write_task(self, summary):
            pass

    node = SenderNodeConfig(
        "sender_01", "bearing_01", "http://scheduler", "mqtt", 1883
    )
    config = SenderConfig(
        "machine_01", (node,), 1.0, 0, 30, 1, False, 10, 20, 0, 10,
        50, 50, 1, tmp_path / "logs", tmp_path / "state",
    )
    store = PacketSourceMappingStore(tmp_path / "packet_sources.db")

    run_sender_task(
        config,
        node,
        source_path,
        realtime=False,
        scheduler=SimpleNamespace(
            assign=lambda request: SimpleNamespace(
                target_topic="edge/packets", schedule_retry_count=0
            )
        ),
        publisher=Publisher(),
        log_sink=Sink(),
        task_ids=SimpleNamespace(next_task_id=lambda: "sd_01_tk_0001"),
        source_mapping_store=store,
    )

    mapping = store.get("sd_01_tk_0001_bearing_01_pkt_001")
    assert mapping["source_file"] == "N09_M07_F10_KA01_1.mat"
    assert (mapping["start_index"], mapping["end_index"]) == (0, 3200)
