import json
import unittest

from downkeep.report import as_json, as_text, build, clean, human
from downkeep.scan import FileRecord, Snapshot, scan
from tests.helpers import FolderTest

DAY_NS = 86_400 * 1_000_000_000
NOW = 2_000 * DAY_NS


def record(name, size, days=10.0):
    return FileRecord(name, name, size, NOW - int(days * DAY_NS), 1, 1, 1)


def report(*files, skipped=None, **options):
    return build(Snapshot("/downloads", NOW, files, skipped or {}), **options)


class ReportTest(unittest.TestCase):
    def test_totals_and_types(self):
        result = report(record("a.zip", 300), record("b.ZIP", 200), record("c.pdf", 50), record("plain", 1))
        self.assertEqual((result["files"], result["bytes"]), (4, 551))
        self.assertEqual(
            result["types"],
            [
                {"type": ".zip", "files": 2, "bytes": 500},
                {"type": ".pdf", "files": 1, "bytes": 50},
                {"type": "no extension", "files": 1, "bytes": 1},
            ],
        )

    def test_age_edges(self):
        ages = [0, 6.9, 7, 29.9, 30, 89.9, 90, 364.9, 365, 5000, -3]
        result = report(*[record(f"{index}.txt", 1, days) for index, days in enumerate(ages)])
        self.assertEqual([row["files"] for row in result["ages"]], [3, 2, 2, 2, 2])

    def test_biggest_files(self):
        result = report(record("small", 10), record("b", 45), record("a", 45), top=2)
        self.assertEqual([file["name"] for file in result["biggest"]], ["a", "b"])
        self.assertEqual(result["biggest_share"], 90)

    def test_top_zero(self):
        result = report(record("a", 5), top=0)
        self.assertEqual((result["biggest"], result["biggest_share"]), ([], 0))
        self.assertNotIn("Biggest", as_text(result))

    def test_future_dates_are_zero_days_old(self):
        self.assertEqual(report(record("a", 1, days=-9))["biggest"][0]["days_old"], 0)

    def test_empty_text(self):
        text = as_text(report())
        self.assertIn("0 files, 0 B. Nothing was changed.", text)
        self.assertNotIn("By type", text)

    def test_text_shows_everything_left_out(self):
        text = as_text(report(record("a.txt", 5), skipped={"folders": 18, "links": 2}))
        self.assertIn("1 file, 5 B", text)
        self.assertIn("18  folders", text)
        self.assertIn("2  links", text)

    def test_empty_age_groups_are_hidden_in_text(self):
        text = as_text(report(record("a.txt", 5, days=1)))
        self.assertIn("under a week", text)
        self.assertNotIn("over a year", text)

    def test_extra_types_are_grouped(self):
        text = as_text(report(*[record(f"f.x{index}", 10) for index in range(11)]), types=8)
        self.assertIn("3 other types", text)

    def test_names_cannot_control_the_terminal(self):
        text = as_text(report(record("evil\x1b[2J\r\n\u202egpj.exe", 5)))
        self.assertNotRegex(text, "[\x1b\r\u202e]")
        self.assertEqual(clean("caf\u00e9 \u0928\u092e\u0938\u094d\u0924\u0947.txt"), "caf\u00e9 \u0928\u092e\u0938\u094d\u0924\u0947.txt")

    def test_json_round_trips_odd_names(self):
        result = report(record("bad\udcff.txt", 5))
        self.assertEqual(json.loads(as_json(result)), result)

    def test_human_sizes(self):
        cases = {0: "0 B", 999: "999 B", 1000: "1.0 KB", 999_949: "999.9 KB", 999_950: "1.0 MB", 3_500_000_000: "3.5 GB"}
        for size, expected in cases.items():
            with self.subTest(size=size):
                self.assertEqual(human(size), expected)


class ReportFromDiskTest(FolderTest):
    def test_real_folder(self):
        self.add("a.zip", b"x" * 2000, days=200)
        self.add("b.txt", b"x" * 500, days=2)
        result = build(scan(self.folder))
        self.assertEqual((result["files"], result["bytes"]), (2, 2500))
        self.assertEqual(result["biggest"][0]["days_old"], 200)


if __name__ == "__main__":
    unittest.main()
