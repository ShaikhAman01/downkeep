import json
import unittest

from downkeep.evidence import Duplicates, Extracted
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


class ProofTest(unittest.TestCase):
    files = [record("a.zip", 100), record("b.zip", 50), record("c.txt", 5)]

    def test_sites(self):
        result = report(*self.files, hosts={"a.zip": "x.example", "b.zip": "x.example"})
        self.assertEqual(
            result["sites"],
            {"known": [{"site": "x.example", "files": 2, "bytes": 150}], "unknown": {"files": 1, "bytes": 5}},
        )
        text = as_text(result)
        self.assertIn("Probably from", text)
        self.assertRegex(text, r"unknown +1 ")

    def test_many_sites_are_grouped(self):
        files = [record(f"{index}", 1) for index in range(12)]
        text = as_text(report(*files, hosts={str(index): f"s{index}.example" for index in range(12)}), rows=8)
        self.assertRegex(text, r"4 other sites +4 ")
        self.assertNotIn("unknown", text)

    def test_copies(self):
        result = report(*self.files, duplicates=[Duplicates((record("one", 40), record("two", 40), record("ev\x1bil", 40)))])
        self.assertEqual(result["duplicates"][0]["wasted"], 80)
        text = as_text(result)
        self.assertIn("Exact copies: 2 extra, 80 B (contents compared)", text)
        self.assertIn("one  =  two  =  ev\N{REPLACEMENT CHARACTER}il", text)

    def test_extracted_zips(self):
        result = report(
            *self.files,
            extracted=[Extracted(self.files[0], "/downloads", 1), Extracted(self.files[1], "/downloads/b", 3)],
        )
        text = as_text(result)
        self.assertIn("Zips already extracted: 2, 150 B (every file checked)", text)
        self.assertIn("a.zip  (1 file in this folder)", text)
        self.assertIn("b.zip  (3 files in b)", text)

    def test_long_lists_are_cut_in_text_only(self):
        groups = [Duplicates((record(f"{index}a", 4), record(f"{index}b", 4))) for index in range(10)]
        result = report(*self.files, duplicates=groups)
        self.assertEqual(len(result["duplicates"]), 10)
        self.assertIn("and 2 more", as_text(result, rows=8))
        self.assertNotIn("more (see", as_text(result, rows=10))

    def test_quick_report_has_no_proof(self):
        result = report(*self.files)
        self.assertFalse({"sites", "duplicates", "extracted"} & result.keys())
        self.assertNotIn("Probably", as_text(result))


class ReportFromDiskTest(FolderTest):
    def test_real_folder(self):
        self.add("a.zip", b"x" * 2000, days=200)
        self.add("b.txt", b"x" * 500, days=2)
        result = build(scan(self.folder))
        self.assertEqual((result["files"], result["bytes"]), (2, 2500))
        self.assertEqual(result["biggest"][0]["days_old"], 200)


if __name__ == "__main__":
    unittest.main()
