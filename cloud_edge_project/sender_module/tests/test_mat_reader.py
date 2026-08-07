import unittest
from pathlib import Path

import numpy as np


class MatWindowTests(unittest.TestCase):
    def test_windows_align_signals_and_use_latest_temperature(self) -> None:
        from sender.mat_reader import MatRecord, SignalSeries

        record = MatRecord(
            source_path=Path("sample.mat"),
            series={
                "vibration": SignalSeries(np.array([0.00, 0.02, 0.04, 0.06]), np.array([1, 2, 3, 4]), 60),
                "phase_current_1_A": SignalSeries(np.array([0.00, 0.02, 0.04, 0.06]), np.array([5, 6, 7, 8]), 60),
                "phase_current_2_A": SignalSeries(np.array([0.00, 0.02, 0.04, 0.06]), np.array([9, 10, 11, 12]), 60),
                "shaft_speed_rpm": SignalSeries(np.array([0.00, 0.03, 0.06]), np.array([900, 901, 902]), 40),
                "load_torque_nm": SignalSeries(np.array([0.00, 0.03, 0.06]), np.array([1.0, 1.1, 1.2]), 40),
                "bearing_radial_load_n": SignalSeries(np.array([0.00, 0.03, 0.06]), np.array([1000, 1001, 1002]), 40),
            },
            temperature_times=np.array([-0.01, 1.0]),
            temperature_values=np.array([46.0, 47.0]),
        )

        windows = list(record.windows(duration_ms=50, count=2))

        self.assertEqual(windows[0].data["vibration"]["values"], [1.0, 2.0, 3.0])
        self.assertEqual(windows[0].data["shaft_speed_rpm"]["values"], [900.0, 901.0])
        self.assertEqual(windows[0].data["bearing_module_temperature_c"], 46.0)
        self.assertEqual(windows[1].data["vibration"]["values"], [4.0])
        self.assertEqual(windows[1].data["vibration"]["sample_count"], 1)

    def test_windows_keep_short_final_signal_instead_of_padding(self) -> None:
        from sender.mat_reader import MatRecord, SignalSeries

        times = np.array([0.00, 0.025, 0.05])
        record = MatRecord(
            source_path=Path("short.mat"),
            series={
                name: SignalSeries(times, np.array([1.0, 2.0, 3.0]), rate)
                for name, rate in {
                    "vibration": 40,
                    "phase_current_1_A": 40,
                    "phase_current_2_A": 40,
                    "shaft_speed_rpm": 40,
                    "load_torque_nm": 40,
                    "bearing_radial_load_n": 40,
                }.items()
            },
            temperature_times=np.array([0.0]),
            temperature_values=np.array([46.0]),
        )

        second = list(record.windows(duration_ms=50, count=2))[1]

        self.assertEqual(second.data["vibration"]["sample_count"], 1)
        self.assertEqual(second.data["vibration"]["values"], [3.0])


class RealMatIntegrationTests(unittest.TestCase):
    def test_local_ka01_record_yields_eighty_windows(self) -> None:
        from sender.mat_reader import load_mat_record

        mat_path = (
            Path(__file__).resolve().parents[2]
            / "KA01"
            / "N09_M07_F10_KA01_1.mat"
        )
        if not mat_path.exists():
            self.skipTest("local KA01 file is not available")

        windows = list(load_mat_record(mat_path).windows(duration_ms=50, count=80))

        self.assertEqual(len(windows), 80)
        self.assertEqual(windows[0].data["vibration"]["sample_count"], 3200)
        self.assertEqual(windows[0].data["shaft_speed_rpm"]["sample_count"], 200)
        self.assertGreater(windows[-1].data["vibration"]["sample_count"], 0)
        self.assertLessEqual(windows[-1].data["vibration"]["sample_count"], 3200)


if __name__ == "__main__":
    unittest.main()
