import unittest
from pathlib import Path


class CliTests(unittest.TestCase):
    def test_parse_source_files_returns_three_sender_sources(self) -> None:
        from sender.__main__ import parse_source_files

        self.assertEqual(
            parse_source_files(["sender_01=one.mat", "sender_02=two.mat", "sender_03=three.mat"]),
            {
                "sender_01": Path("one.mat"),
                "sender_02": Path("two.mat"),
                "sender_03": Path("three.mat"),
            },
        )

    def test_parse_source_files_rejects_duplicate_sender(self) -> None:
        from sender.__main__ import parse_source_files

        with self.assertRaisesRegex(ValueError, "duplicate sender_id"):
            parse_source_files(["sender_01=one.mat", "sender_01=two.mat"])

    def test_parse_source_files_rejects_malformed_entry(self) -> None:
        from sender.__main__ import parse_source_files

        with self.assertRaisesRegex(ValueError, "sender_id=MAT_PATH"):
            parse_source_files(["sender_01"])


if __name__ == "__main__":
    unittest.main()
