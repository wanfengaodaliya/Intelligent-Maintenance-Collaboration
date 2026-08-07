import unittest
from pathlib import Path


class CliTests(unittest.TestCase):
    def test_parse_bearing_files_returns_three_sources(self) -> None:
        from sender.__main__ import parse_bearing_files

        self.assertEqual(
            parse_bearing_files(
                [
                    "bearing_01=one.mat",
                    "bearing_02=two.mat",
                    "bearing_03=three.mat",
                ]
            ),
            {
                "bearing_01": Path("one.mat"),
                "bearing_02": Path("two.mat"),
                "bearing_03": Path("three.mat"),
            },
        )

    def test_parse_bearing_files_rejects_duplicate_id(self) -> None:
        from sender.__main__ import parse_bearing_files

        with self.assertRaisesRegex(ValueError, "duplicate bearing_id"):
            parse_bearing_files(
                [
                    "bearing_01=one.mat",
                    "bearing_01=two.mat",
                    "bearing_03=three.mat",
                ]
            )

    def test_parse_bearing_files_rejects_malformed_entry(self) -> None:
        from sender.__main__ import parse_bearing_files

        with self.assertRaisesRegex(ValueError, "bearing_id=MAT_PATH"):
            parse_bearing_files(["bearing_01", "bearing_02=two.mat", "bearing_03=three.mat"])


if __name__ == "__main__":
    unittest.main()
