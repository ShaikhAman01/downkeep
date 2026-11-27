import copy
import json
import os
import random
import sys
import types
import unittest
from unittest import mock

from downkeep import engine, sorting
from downkeep.model import ActionError, Move, Stay
from downkeep.scan import FileRecord, Snapshot, scan
from downkeep.sorting import DEFAULT_RULES, EXTENSIONS, KINDS, check_rules, kind_of, match, plan, same, saved_rules
from downkeep.store import Store
from tests.helpers import FolderTest


def record(name, links=1):
    return FileRecord(name, os.path.join("/d", name), 1, 1, 1, abs(hash(name)), links)


def snapshot(*names):
    return Snapshot("/d", 1, tuple(name if isinstance(name, FileRecord) else record(name) for name in names), {})


def look(**folders):
    """A made-up disk: folder names to the names inside. None is a file or a link."""

    def inside(path):
        if path == "/d":
            return {name: names is not None for name, names in folders.items()}
        return dict.fromkeys(folders[os.path.basename(path)], False)

    return inside


def rule(folder="Mine", **more):
    return {"folder": folder, **more}


def rules(*own):
    return check_rules([*own, *DEFAULT_RULES])


def planned(names, *own, disk=None, hosts=None):
    moves, stays = plan(snapshot(*names), rules(*own), hosts, look(**(disk or {})))
    return {move.file.name: move.folder for move in moves}, {stay.name: stay.why for stay in stays}


class KindTest(unittest.TestCase):
    def test_kinds(self):
        for name, kind in {
            "a.PDF": "documents", "a.csv": "spreadsheets", "a.pptx": "presentations", "a.JPeG": "images",
            "a.mkv": "videos", "a.flac": "music", "a.tar.gz": "archives", "a.TAR.ZST": "archives", "a.tgz": "archives",
            "a.part1.rar": "archives", "a.r00": "archives", "a.z01": "archives", "a.7z.001": "archives",
            "a.exe": "installers", "a.AppImage": "installers", "a.iso": "installers", "a.flatpakref": "installers",
            "a.epub": "books", "a.ttf": "fonts", "a.torrent": "torrents", "a.py": "code", "invoice.pdf.exe": "installers",
        }.items():
            self.assertEqual(kind_of(name), kind, name)

    def test_unclear_names_have_no_kind(self):
        for name in ("README", "a", "a.", ".pdf", "pdf", "a.html", "a.ts", "a.key", "a.pem", "a.bin", "a.zzz", "a.pdf ",
                     "a.2024", "a.v1.003", "a.r0", "", "a.pdf\n"):
            self.assertIsNone(kind_of(name), name)

    def test_each_extension_has_one_kind_and_every_kind_a_default(self):
        everything = [extension for extensions in EXTENSIONS.values() for extension in extensions]
        self.assertEqual(len(everything), len(set(everything)))
        self.assertEqual(set(EXTENSIONS), set(KINDS))
        self.assertEqual({kind for item in DEFAULT_RULES for kind in item["kinds"]}, set(KINDS))
        self.assertTrue(all(item == item.lower() and "." not in item for item in everything))


class MatchTest(unittest.TestCase):
    def found(self, name, *own, host=None):
        hit = match(record(name), rules(*own), host)
        return hit and (hit[0]["folder"], hit[1])

    def test_reasons_are_plain(self):
        self.assertEqual(self.found("a.png"), ("Images", "Pictures go to Images"))
        self.assertEqual(self.found("my invoice.pdf", rule("Bills", words=["invoice"])),
                         ("Bills", "The name contains invoice"))
        self.assertEqual(self.found("a.pdf", rule("College", sites=["example.edu"]), host="college.example.edu"),
                         ("College", "Came from college.example.edu"))
        self.assertEqual(self.found("a.step", rule("3D", extensions=["step"])), ("3D", ".step files go to 3D"))
        self.assertIsNone(self.found("a.zzz"))

    def test_own_rules_come_first_and_in_order(self):
        one, two = rule("One", words=["cat"]), rule("Two", extensions=["png"])
        self.assertEqual(self.found("cat.png", one, two)[0], "One")
        self.assertEqual(self.found("cat.png", two, one)[0], "Two")
        listed_last = check_rules([*DEFAULT_RULES, two])
        self.assertEqual(match(record("a.png"), listed_last)[0]["folder"], "Two")

    def test_defaults_can_be_renamed_and_turned_off(self):
        changed = [item | {"folder": "Photos"} if item["id"] == "images" else item for item in DEFAULT_RULES]
        self.assertEqual(match(record("a.png"), check_rules(changed))[1], "Pictures go to Photos")
        off = [item | {"on": False} if item["id"] == "images" else item for item in DEFAULT_RULES]
        self.assertIsNone(match(record("a.png"), check_rules(off)))

    def test_words(self):
        for word, name, expected in [
            ("invoice", "INVOICE.pdf", True), ("Invoice", "march_invoice-2.pdf", True), ("invoice", "invoice2024.pdf", True),
            ("invoice", "2024invoice.pdf", True), ("invoice", "MarchInvoice.pdf", True), ("invoice", "invoices.pdf", False),
            ("art", "party.pdf", False), ("art", "party art.pdf", True), ("cs101", "cs101 notes.pdf", True),
            ("cs101", "cs1012.pdf", False), ("bank statement", "Bank_Statement-May.pdf", True),
            ("bank-statement", "bank statement.pdf", True), ("pdf", "a.pdf", False), ("youtube", "YouTube clip.mp4", True),
            ("été", "été 2024.jpg", True), ("ÉTÉ", "été.jpg", True), ("straße", "STRASSE.pdf", True),
            ("发票", "电子发票2024.pdf", True), ("बिल", "बिजली बिल.pdf", True), ("файл", "Файл.pdf", True),
            ("файл", "файлы.pdf", False), ("c++", "learn c++.pdf", True), ("a.b", "x a b.pdf", True), ("(1)", "x (1).pdf", True),
            (".*", "anything.pdf", None),
        ]:
            with self.subTest(word=word, name=name):
                if expected is None:
                    self.assertNotEqual(self.found(name, rule(words=[word]))[0], "Mine")
                    continue
                self.assertEqual(self.found(name, rule(words=[word]))[0] == "Mine", expected)

    def test_sites_match_whole_parts_only(self):
        own = rule(sites=["example.com"])
        for host, expected in [("example.com", True), ("a.b.example.com", True), ("badexample.com", False),
                               ("example.com.evil.net", False), ("com", False), (None, False)]:
            self.assertEqual(self.found("a.zzz", own, host=host) is not None, expected, host)

    def test_disguised_programs_go_by_their_real_ending(self):
        own = rule("Bills", words=["invoice"], sites=["example.com"])
        for name in ("invoice.pdf.exe", "invoice.PDF .EXE", "invoice.jpg.msi"):
            folder, why = self.found(name, own, host="example.com")
            self.assertEqual(folder, "Installers")
            self.assertTrue(why.startswith(f"Named like a .{name.lower().split('.')[1].strip()} file but really ."), why)
            self.assertTrue(why.endswith("Installers go to Installers"), why)
        self.assertEqual(self.found("setup.v2.exe", own)[1], "Installers go to Installers")
        self.assertIsNone(self.found("invoice.pdf.scr", own))
        self.assertIn("really .exe", self.found("a.pdf.exe", rule("Programs", extensions=["exe"]))[1])

    def test_compound_extensions(self):
        self.assertEqual(self.found("a.TAR.GZ", rule(extensions=["tar.gz"]))[0], "Mine")
        self.assertEqual(self.found("a.tar.gz", rule(extensions=["gz"]))[0], "Mine")
        self.assertEqual(self.found("a.gz", rule(extensions=["tar.gz"]))[0], "Archives")
        self.assertEqual(self.found("targz", rule(extensions=["gz"])), None)


class PlanTest(unittest.TestCase):
    def test_typical_folder(self):
        moves, stays = planned(["b.pdf", "a.png", "x.zzz"])
        self.assertEqual(moves, {"a.png": "Images", "b.pdf": "Documents"})
        self.assertEqual(stays, {"x.zzz": "No rule matches"})

    def test_same_answer_in_any_order(self):
        names = ["b.pdf", "a.png", "A.png", "a.PNG", "x.zzz", "c.mp3"]
        expected = plan(snapshot(*names), rules(), None, look(Music=["C.MP3"]))
        for seed in range(20):
            random.Random(seed).shuffle(names)
            self.assertEqual(plan(snapshot(*names), rules(), None, look(Music=["C.MP3"])), expected)
        self.assertEqual([move.file.name for move in expected[0]], ["A.png", "b.pdf"])

    def test_never_to_a_name_that_is_taken(self):
        for taken in ("a.png", "A.PNG"):
            moves, stays = planned(["a.png"], disk={"Images": [taken]})
            self.assertEqual((moves, stays), ({}, {"a.png": "Images already has a file with this name"}))
        moves, stays = planned(["é.png"], disk={"Images": ["é.png"]})
        self.assertEqual(moves, {})

    def test_two_files_that_would_clash(self):
        moves, stays = planned(["a.png", "A.PNG", "b.png"])
        self.assertEqual(moves, {"A.PNG": "Images", "b.png": "Images"})
        self.assertEqual(stays, {"a.png": "Another file with the same name is also going to Images"})
        one, two = rule("Same", extensions=["png"]), rule("same", extensions=["gif"])
        self.assertEqual(planned(["a.png"], one, two)[0], {"a.png": "Same"})
        moves, stays = planned(["é.png", "é.png"])
        self.assertEqual((len(moves), len(stays)), (1, 1))

    def test_folder_that_is_really_a_file_or_link(self):
        moves, stays = planned(["a.png"], disk={"Images": None})
        self.assertEqual((moves, stays), ({}, {"a.png": "Something else here is already named Images"}))
        self.assertEqual(planned(["a.png"], disk={"images": None})[0], {})

    def test_existing_spelling_is_used(self):
        moves, _ = plan(snapshot("a.png"), rules(), None, look(images=[]))
        self.assertEqual(moves, [Move(record("a.png"), "images", "Pictures go to images")])
        self.assertEqual(planned(["a.png"], disk={"images": [], "Images": []})[0], {"a.png": "Images"})
        self.assertEqual(planned(["a.png"], disk={"images": ["a.png"]})[0], {})

    def test_hard_links_stay(self):
        moves, stays = planned([record("a.png", links=2)])
        self.assertEqual((moves, stays), ({}, {"a.png": "The same file also exists under another name"}))

    def test_folders_that_cannot_be_read(self):
        def nothing(path):
            raise PermissionError

        self.assertEqual(plan(snapshot("a.png"), rules(), None, nothing), ([], [Stay("a.png", "Couldn't check the folder")]))

        def only_top(path):
            if path == "/d":
                return {"Images": True}
            raise PermissionError

        self.assertEqual(plan(snapshot("a.png"), rules(), None, only_top)[1][0].why, "Couldn't check what is in Images")

    def test_hosts_are_used(self):
        moves, _ = planned(["a.zzz", "b.zzz"], rule(sites=["x.org"]), hosts={os.path.join("/d", "a.zzz"): "x.org"})
        self.assertEqual(moves, {"a.zzz": "Mine"})

    def test_plan_changes_nothing_it_was_given(self):
        given = rules(rule(words=["a"]))
        before = copy.deepcopy(given)
        plan(snapshot("a.png"), given, None, look(images=[]))
        self.assertEqual(given, before)

    def test_odd_names(self):
        self.assertEqual(planned(["a\udce9.png", "a\nb.png"])[0], {"a\udce9.png": "Images", "a\nb.png": "Images"})


class RealFolderTest(FolderTest):
    def test_reads_the_disk(self):
        self.add("a.png")
        self.add("b.png")
        self.add("c.mp3")
        (self.folder / "Images").mkdir()
        (self.folder / "Images" / "B.PNG").write_bytes(b"x")
        (self.folder / "elsewhere").mkdir()
        self.symlink("Music", self.folder / "elsewhere")
        moves, stays = plan(scan(self.folder), rules())
        self.assertEqual([(move.file.name, move.folder) for move in moves], [("a.png", "Images")])
        self.assertEqual({stay.name: stay.why for stay in stays}, {
            "b.png": "Images already has a file with this name", "c.mp3": "Something else here is already named Music"})

    @unittest.skipUnless(engine.SUPPORTED, "moving is not available here")
    def test_everything_planned_really_moves(self):
        names = ["a.png", "invoice.pdf.exe", "x.tar.gz", "song.mp3", "notes.zzz", "bill.pdf"]
        for name in names:
            self.add(name, name.encode())
        moves, _ = plan(scan(self.folder), rules(rule("Bills ₹", words=["bill"])))
        with Store(self.folder.parent / "data") as store, mock.patch("downkeep.engine.site_of", return_value=None):
            result = engine.apply_moves(store, str(self.folder), moves)
        self.assertEqual((sorted(result.done), result.skipped), (sorted(set(names) - {"notes.zzz"}), []))
        self.assertTrue((self.folder / "Installers" / "invoice.pdf.exe").is_file())
        self.assertTrue((self.folder / "Bills ₹" / "bill.pdf").is_file())

    @unittest.skipUnless(engine.SUPPORTED, "moving is not available here")
    def test_every_accepted_folder_name_suits_the_engine(self):
        for name in ("Bills", "Été", "A b c", "-x", "a.b", "発票", "Com", "conx", "$x", "#1", "x" * 60):
            engine._check_folder_name(check_rules([rule(name, words=["a"])])[0]["folder"])


class CheckRulesTest(unittest.TestCase):
    def refused(self, value, part=""):
        with self.assertRaises(ActionError) as caught:
            check_rules(value)
        self.assertIn(part, str(caught.exception))
        self.assertTrue(str(caught.exception).isprintable())

    def test_defaults_pass_unchanged(self):
        self.assertEqual(check_rules(DEFAULT_RULES), DEFAULT_RULES)
        self.assertEqual(check_rules(json.loads(json.dumps(DEFAULT_RULES))), DEFAULT_RULES)

    def test_checking_twice_changes_nothing(self):
        own = [rule("été", words=[" Straße ", "a_b"], sites=["https://www.Bücher.example/x?y"], extensions=["*.TAR.GZ"],
                    kinds=["books"], days=0), rule("ÉTÉ", words=["x"])]
        once = check_rules(own)
        self.assertEqual(check_rules(json.loads(json.dumps(once))), once)
        self.assertEqual(once[0]["sites"], ["xn--bcher-kva.example"])
        self.assertEqual(once[0]["folder"], "\u00e9té")
        self.assertEqual(once[0]["extensions"], ["tar.gz"])
        self.assertEqual(once[0]["words"], ["Straße", "a_b"])

    def test_result_is_a_copy(self):
        given = copy.deepcopy(DEFAULT_RULES)
        check_rules(given)[0]["kinds"].append("x")
        self.assertEqual(given, DEFAULT_RULES)
        self.assertEqual(check_rules([])[0]["kinds"], DEFAULT_RULES[0]["kinds"])
        check_rules([])[0]["kinds"].append("x")
        self.assertNotIn("x", DEFAULT_RULES[0]["kinds"])

    def test_missing_defaults_come_back_turned_off(self):
        found = check_rules([rule(words=["a"])])
        self.assertEqual([item["id"] for item in found[1:]], [item["id"] for item in DEFAULT_RULES])
        self.assertFalse(any(item["on"] for item in found[1:]))

    def test_ids(self):
        found = check_rules([rule("A", id="own-1", words=["a"]), rule("B", words=["b"]), rule("C", id="", words=["c"])])
        self.assertEqual([item["id"] for item in found[:3]], ["own-1", "own-2", "own-3"])
        self.refused([rule("A", id="x", words=["a"]), rule("B", id="x", words=["b"])], "same id")
        for bad in ("UP", "a b", "a/b", "-a", "a" * 41, "é", "a\n", 5, None, []):
            self.refused([rule(id=bad, words=["a"])], "id")

    def test_wrong_shapes(self):
        for value in (None, {}, "x", 5, [None], [[]], ["x"], [{}], [{5: 1}], [rule(words=["a"], extra=1)],
                      [rule(words="a")], [rule(words=[5])], [rule(words=[None])], [rule(kinds=["nope"])],
                      [rule(kinds=[["images"]])], [rule(words={"a": 1})], [rule(extensions=None)]):
            self.refused(value)
        for key in ("on", "auto"):
            for bad in (1, 0, None, "true", []):
                self.refused([rule(words=["a"], **{key: bad})], "on or off")

    def test_sizes(self):
        many = [rule(f"F{n}", words=["a"]) for n in range(sorting.MAX_RULES - len(DEFAULT_RULES))]
        self.assertEqual(len(check_rules(check_rules(many))), sorting.MAX_RULES)
        self.refused([*many, rule("X", words=["a"])], "at most 100")
        self.refused([rule(words=["a"])] * 1000, "at most 100")
        words = [f"w{n}" for n in range(51)]
        self.assertEqual(len(check_rules([rule(words=words[:50])])[0]["words"]), 50)
        self.refused([rule(words=words)], "at most 50")
        self.refused([rule(words=["a" * 61])])
        self.refused([rule(words=["a" * 10**6])], "too long")
        self.refused([rule("a" * 10**6, words=["a"])], "60")
        self.refused([rule(extensions=["a" * 21])])

    def test_days(self):
        for good in (0, 365):
            self.assertEqual(check_rules([rule(words=["a"], days=good)])[0]["days"], good)
        for bad in (-1, 366, 1.0, "7", None, True, float("nan"), 10**30):
            self.refused([rule(words=["a"], days=bad)], "days")

    def test_folder_names_legal_everywhere(self):
        bad = [
            "", " ", ".", "..", ".hidden", "~", "~$x", " lead", "trail ", "trail.", "a/b", "a\\b", "a:b", "a*b", "a?b", 'a"b',
            "a<b", "a>b", "a|b", "a\0b", "a\nb", "a\tb", "a\x7fb", "a\x85b", "a‮b", "a​b", "﻿a", "a\udc80", "a" * 61,
            "CON", "con", "Con.txt", "PRN", "AUX", "NUL", "nul.tar.gz", "COM1", "com0", "LPT1", "lpt9.x", "COM¹", "LPT³",
            "CON .txt", "CONIN$", "CONOUT$", "CLOCK$", "../x", "/etc", "C:", " x", 5, None, ["a"], b"a",
        ]
        for name in bad:
            with self.subTest(name=name):
                self.refused([rule(name, words=["a"])])
        for name in ("Bills", "Été", "発票", "a b", "a.b", "Com", "COM10", "conx", "LPT", "null", "-a", "a" * 60, "R&D (old)", "100%"):
            with self.subTest(name=name):
                self.assertEqual(check_rules([rule(name, words=["a"])])[0]["folder"], name)

    def test_names_that_differ_by_case_or_accents_share_a_folder(self):
        found = check_rules([rule("PICS", words=["a"]), rule("pics", words=["b"]), rule("é", words=["c"]),
                             rule("é", words=["d"]), rule("IMAGES", words=["e"]), *DEFAULT_RULES])
        self.assertEqual([item["folder"] for item in found[:5]], ["PICS", "PICS", "é", "é", "IMAGES"])
        self.assertEqual({item["folder"] for item in found if item["id"] == "images"}, {"IMAGES"})
        self.assertEqual(same("STRASSE"), same("straße"))

    def test_a_rule_must_match_something(self):
        self.refused([rule()], "doesn't say which files")

    def test_extensions_words_sites(self):
        for bad in ("", ".", "a/b", "a b", "..", "é", "a..b", "-", "*", "a*", "\n"):
            self.refused([rule(extensions=[bad])], "ending")
        for bad in ("", " ", "_", "-.", "a\nb", "a\0", "‮a", "\udc80"):
            self.refused([rule(words=[bad])], "word")
        for bad in ("", "com", "http://", "ftp://example.com", "a b.com", ".", "javascript://x.y z", "https://[::1]/", ".com"):
            self.refused([rule(sites=[bad])], "example.com")
        good = check_rules([rule(sites=["Example.COM", "www.example.com", "https://a.example.com:80/p?token=secret", "example.com."])])
        self.assertEqual(good[0]["sites"], ["example.com", "a.example.com"])
        self.assertNotIn("secret", json.dumps(good))

    def test_hostile_noise_is_only_ever_refused(self):
        source = random.Random(7)
        pieces = [None, True, 0, -1, 1e9, "", "a", "..", "CON", "\0", "\udc80", [], {}, ["a"], [["a"]], {"a": 1}, "x" * 999]
        keys = [*DEFAULT_RULES[0], "zzz"]
        for _ in range(3000):
            value = [{key: source.choice(pieces) for key in source.sample(keys, source.randint(0, 9))} for _ in range(2)]
            try:
                found = check_rules(value)
            except ActionError:
                continue
            self.assertEqual(check_rules(found), found)


class SavedRulesTest(FolderTest):
    def test_saved_rules(self):
        with Store(self.folder.parent / "data") as store:
            self.assertEqual(saved_rules(store), DEFAULT_RULES)
            mine = rules(rule("Mine", words=["a"]))
            store.set(sorting.RULES_KEY, mine)
            self.assertEqual(saved_rules(store), mine)
            store.set(sorting.RULES_KEY, "broken")
            with self.assertRaises(ActionError):
                saved_rules(store)


class SuggestNameTest(unittest.TestCase):
    def test_names(self):
        for given, expected in [
            ({"sites": ["college.example.edu"]}, "College"), ({"sites": ["github.com"]}, "GitHub"), ({"sites": ["notgithub.com"]}, "Notgithub"), ({"sites": ["gist.githubusercontent.com"]}, "GitHub"),
            ({"sites": ["drive.google.com"]}, "Google Drive"), ({"sites": ["docs.google.com"]}, "Google"),
            ({"sites": ["web.whatsapp.com"]}, "WhatsApp"), ({"sites": ["lms.college.example.edu"]}, "College"),
            ({"sites": ["moodle.uni.ac.in"]}, "Uni"), ({"sites": ["my.portal.bank.com"]}, "Bank"),
            ({"sites": ["example.com"]}, "New folder"), ({"sites": ["www.example.org"]}, "New folder"),
            ({"sites": ["files.site.test"]}, "New folder"), ({"sites": ["a.localhost"]}, "New folder"),
            ({"sites": ["www.bbc.co.uk"]}, "Bbc"), ({"sites": ["mail.ox.ac.uk"]}, "Ox"), ({"sites": ["bbc.co.uk"]}, "Bbc"), ({"sites": ["dl.shop.com"]}, "Shop"),
            ({"sites": ["xn--bcher-kva.de"]}, "Bücher"), ({"sites": ["10.0.0.1"]}, "New folder"),
            ({"sites": ["10.0.0.1"], "words": ["invoice"]}, "Invoices"), ({"words": ["receipt"]}, "Receipts"),
            ({"words": ["lecture"]}, "Lectures"), ({"words": ["lecture notes"]}, "Lectures"), ({"words": ["tax"]}, "Tax"),
            ({"words": ["physics"]}, "Physics"), ({"words": ["Invoice"]}, "Invoices"), ({"words": ["screenshot"]}, "Screenshots"),
            ({"words": ["data"]}, "Data"), ({"words": ["news"]}, "News"), ({"words": ["homework"]}, "Homework"),
            ({"words": ["media"]}, "Media"), ({"words": ["structures"]}, "Structures"), ({"words": ["drawing"]}, "Drawing"),
            ({"words": ["lemon"]}, "Lemon"), ({"words": ["cs101"]}, "Cs101"),
            ({"words": ["café"]}, "Café"), ({"words": ["发票"]}, "发票"), ({"words": ["wash"]}, "Wash"),
            ({"kinds": ["images"]}, "Pictures"), ({"extensions": ["pdf"]}, "PDF files"),
            ({"extensions": ["tar.gz"]}, "TAR.GZ files"), ({"words": ["con"]}, "New folder"), ({"words": ["CON"]}, "New folder"),
            ({"words": ["a/b"]}, "New folder"), ({"words": ["x" * 70]}, "New folder"), ({}, "New folder"),
            ({"sites": [None], "words": [5]}, "New folder"),
            ({"sites": ["chatgpt.com"]}, "ChatGPT"), ({"sites": ["mail-attachment.googleusercontent.com"]}, "Gmail"),
            ({"sites": ["app-companion-430619.appspot.com"]}, "New folder"), ({"sites": ["shop-2024.example.com"]}, "Shop-2024"),
        ]:
            with self.subTest(given=given):
                self.assertEqual(sorting.suggest_name(given), expected)

    def test_always_accepted(self):
        source = random.Random(3)
        pieces = ["a", ".", "con", "CON", "x" * 70, "é", "‮", " ", "~", "1.2.3.4", "a.b", "co.uk", "www.", "-"]
        for _ in range(2000):
            words = ["".join(source.choices(pieces, k=source.randint(1, 4)))]
            name = sorting.suggest_name({"words": words, "sites": words, "extensions": words})
            self.assertEqual(check_rules([rule(name, words=["a"])])[0]["folder"], name)


class SuggestTest(unittest.TestCase):
    def suggested(self, names, own=(), hosts=None, disk=None, own_names=()):
        return sorting.suggest(snapshot(*names), rules(*own), hosts, look(**(disk or {})), own_names)

    def test_sites_and_words(self):
        names = [f"Invoice {n}.pdf" for n in range(4)] + [f"notes {n}.pdf" for n in range(3)] + ["x.png", "y.png"]
        hosts = {os.path.join("/d", name): "www.college.example.edu" for name in names[4:7]}
        found = self.suggested(names, hosts=hosts)
        self.assertEqual([(item["folder"], item["why"], item["files"]) for item in found], [
            ("Invoices", "4 files have invoice in the name", 4),
            ("College", "3 files came from college.example.edu", 3)])
        for item in found:
            self.assertEqual(check_rules([item["rule"]])[0], item["rule"])
            self.assertEqual((item["rule"]["on"], item["rule"]["auto"], item["rule"]["days"]), (True, True, 1))
            self.assertNotIn(item["rule"]["id"], [default["id"] for default in DEFAULT_RULES])
        self.assertEqual(found[0]["rule"]["words"], ["invoice"])
        self.assertEqual(found[1]["rule"]["sites"], ["college.example.edu"])
        self.assertEqual(len({item["rule"]["id"] for item in found}), 2)

    def test_rules_really_cover_what_they_say(self):
        names = ["MarchInvoice.pdf", "invoice_2.pdf", "INVOICE-x.png", "invoices.pdf", "invoice.pdf.exe"]
        (item,) = self.suggested(names)
        moves, _ = plan(snapshot(*names), check_rules([item["rule"], *DEFAULT_RULES]), None, look())
        self.assertEqual(sum(move.folder == item["folder"] for move in moves), item["files"])
        self.assertEqual(item["files"], 3)

    def test_at_least_three(self):
        self.assertEqual(self.suggested(["bill a.pdf", "bill b.pdf", "x.pdf"]), [])
        self.assertEqual(self.suggested(["bill a.pdf", "bill b.pdf", "bill.pdf.exe"]), [])
        self.assertEqual(len(self.suggested(["bill a.pdf", "bill b.pdf", "bill c.pdf"])), 1)

    def test_generic_and_private_looking_words_are_not_suggested(self):
        for word in ("copy", "final", "file", "download", "img", "untitled", "scan", "march", "pdf", "zip", "deadbeef",
                     "xkcdqrst", "o" * 31, "ab", "v2", "2024", "IMG", "Copy (1)"):
            with self.subTest(word=word):
                names = [f"{word} {n}.zzz" for n in range(5)]
                self.assertEqual(self.suggested(names), [])
        self.assertEqual(self.suggested([f"Screenshot {n}.png" for n in range(3)])[0]["folder"], "Screenshots")
        self.assertEqual(self.suggested([f"{n}1234567890123 receipt.pdf" for n in range(3)])[0]["rule"]["words"], ["receipt"])

    def test_numbers_dates_and_versions_are_ignored(self):
        names = ["2024-05-01 v1.2.pdf", "2024-05-02 v1.2.pdf", "2024-05-03 v1.2.pdf", "IMG_20240501_1234.jpg"] * 2
        self.assertEqual(self.suggested(list(dict.fromkeys(names))), [])

    def test_two_suggestions_never_share_a_folder(self):
        found = self.suggested([f"lecture {n}.pdf" for n in "abc"] + [f"lectures {n}.pdf" for n in "abcd"])
        self.assertEqual([item["folder"] for item in found], ["Lectures"])

    def test_joined_words_are_found(self):
        found = self.suggested([f"BankStatement{n}.pdf" for n in range(3)])
        self.assertIn(found[0]["rule"]["words"], (["bank"], ["statement"]))

    def test_unicode_words(self):
        found = self.suggested(["Счёт 1.pdf", "счёт 2.pdf", "СЧЁТ-3.pdf"])
        self.assertEqual((found[0]["rule"]["words"], found[0]["folder"]), (["счёт"], "Счёт"))

    def test_at_most_three_and_best_first(self):
        names = [f"{word} {n}.zzz" for word, count in (("lemon", 6), ("mango", 5), ("olive", 4), ("peach", 3))
                 for n in range(count)]
        self.assertEqual([item["folder"] for item in self.suggested(names)], ["Lemon", "Mango", "Olive"])

    def test_names_already_in_use_are_skipped(self):
        names = [f"invoice {n}.pdf" for n in range(3)]
        self.assertEqual(self.suggested(names, own=[rule("INVOICES", words=["bill"])]), [])
        self.assertEqual(self.suggested(names, disk={"invoices": []}), [])
        self.assertEqual(self.suggested(names, disk={"Invoices": None}), [])

    def test_files_already_sorted_by_an_own_rule_are_skipped(self):
        names = [f"invoice {n}.pdf" for n in range(3)]
        self.assertEqual(self.suggested(names, own=[rule("Bills", extensions=["pdf"])]), [])
        self.assertEqual(len(self.suggested(names, own=[rule("Bills", extensions=["pdf"], on=False)])), 1)

    def test_the_same_files_are_suggested_once(self):
        names = [f"tax return {n}.pdf" for n in range(4)]
        found = self.suggested(names)
        self.assertEqual(len(found), 1)
        self.assertIn(found[0]["rule"]["words"], (["return"], ["tax"]))

    def test_ip_sites_are_skipped(self):
        names = [f"a{n}.zzz" for n in range(4)]
        self.assertEqual(self.suggested(names, hosts={os.path.join("/d", name): "192.168.1.5" for name in names}), [])

    def test_deterministic_and_pure(self):
        names = [f"{word} {n}.pdf" for word in ("lemon", "mango") for n in range(3)]
        given = rules()
        before = copy.deepcopy(given)
        first = self.suggested(names)
        for seed in range(10):
            random.Random(seed).shuffle(names)
            self.assertEqual(sorting.suggest(snapshot(*names), given, None, look()), first)
        self.assertEqual(given, before)

    def test_unreadable_folder_still_suggests(self):
        def nothing(path):
            raise PermissionError

        self.assertEqual(len(sorting.suggest(snapshot(*[f"bill {n}.pdf" for n in range(3)]), rules(), None, nothing)), 1)


class TrustedSuggestionTest(unittest.TestCase):
    def suggested(self, names, own=(), hosts=None, own_names=(), defaults=DEFAULT_RULES):
        return sorting.suggest(snapshot(*names), check_rules([*own, *defaults]), hosts, look(), own_names)

    def folders(self, *args, **kwargs):
        return [item["folder"] for item in self.suggested(*args, **kwargs)]

    def test_web_addresses_and_emails_are_never_words(self):
        for names in ([f"{n}(chosic.com).zzz" for n in "abc"], [f"key {n} someone@gmail.com.zzz" for n in "abc"],
                      [f"page {n} https www.example.org.zzz" for n in "abc"], [f"x{n}_mailto_http.zzz" for n in "abc"],
                      [f"{n} www.shop.co.uk.zzz" for n in "abc"], [f"{n} socket.io.zzz" for n in "abc"]):
            with self.subTest(names=names):
                self.assertEqual(self.suggested(names), [])
        for word in sorted(sorting.WEB_WORDS):
            self.assertEqual(sorting._words(f"{word} notes.pdf"), {"notes"})
        self.assertEqual(sorting._words("Invoice_acme.co.in_comics.pdf"), {"invoice", "comics"})
        self.assertEqual(self.folders([f"comics {n}.zzz" for n in "abc"]), ["Comics"])

    def test_editing_and_export_suffixes_are_never_words(self):
        for word in ("preview", "removebg", "copy", "final", "edited", "compressed", "resized", "scan", "scanned",
                     "export", "exported", "download", "untitled", "new", "draft", "cropped", "thumbnail", "original"):
            with self.subTest(word=word):
                self.assertEqual(self.suggested([f"{word} {n}.zzz" for n in range(4)]), [])
        self.assertEqual(self.suggested([f"IMG_2025010{n}-removebg-preview.zzz" for n in range(4)]), [])

    def test_own_name_is_never_a_word(self):
        names = ["10th_Marksheet_Doe_Jane.pdf", "12th_Marksheet_Doe_Jane.pdf", "Id_Card_Doe_Jane.jpg",
                 "Certificate_Doe_Jane.pdf", "Passport-JaneDoe.png", "jdoe-notes.txt"]
        self.assertTrue({"Doe", "Jane"} & set(self.folders(names)))
        self.assertEqual(self.folders(names, own_names=["Jane Doe", "jdoe"]), [])
        self.assertEqual(sorting._name_words(["Jane Doe", "", None, "JaneDoe"]), {"jane", "doe", "janedoe"})

    def test_own_name_comes_from_the_account(self):
        names = [f"Report_Doe_Jane_{n}.zzz" for n in range(3)] + [f"jdoe stuff {n}.zzz" for n in range(4)]
        entry = types.SimpleNamespace(pw_name="jdoe", pw_gecos="Jane Doe,,,")
        fake_pwd = types.SimpleNamespace(getpwuid=lambda uid: entry)
        with mock.patch.object(sorting.os, "name", "posix"), mock.patch.object(sorting.os, "getuid", lambda: 7, create=True), \
                mock.patch.dict(sys.modules, {"pwd": fake_pwd}), mock.patch.object(sorting.getpass, "getuser", lambda: "jdoe"):
            self.assertEqual(sorted(sorting._account_names()), ["Jane Doe", "jdoe", "jdoe"])
            self.assertEqual(self.folders(names, own_names=None), ["Stuff", "Reports"])
        with mock.patch.object(sorting.os, "name", "nt"), mock.patch.dict(sorting.os.environ, {"USERNAME": "jdoe"}), \
                mock.patch.object(sorting, "_windows_display_name", lambda: "Jane Doe"), \
                mock.patch.object(sorting.getpass, "getuser", lambda: "jdoe"):
            self.assertEqual(self.folders(names, own_names=None), ["Stuff", "Reports"])

    def test_no_account_name_fails_quietly(self):
        def broken(*args):
            raise KeyError("no such user")

        def no_user():
            raise OSError("no user name")

        with mock.patch.object(sorting.os, "name", "posix"), mock.patch.object(sorting.os, "getuid", lambda: 7, create=True), \
                mock.patch.dict(sys.modules, {"pwd": types.SimpleNamespace(getpwuid=broken)}), \
                mock.patch.object(sorting.getpass, "getuser", no_user):
            self.assertEqual(sorting._account_names(), [])
            self.assertEqual(self.folders([f"Report {n}.pdf" for n in range(3)], own_names=None), ["Reports"])
        with mock.patch.object(sorting.os, "name", "nt"), mock.patch.dict(sorting.os.environ, clear=True), \
                mock.patch.object(sorting.getpass, "getuser", no_user):
            self.assertIsInstance(sorting._account_names(), list)

    def test_words_that_only_split_a_built_in_folder_are_skipped(self):
        music = ["lofi-chill-rain.mp3", "lofi-chill-beat.mp3", "lofi-chill-night.mp3", "lofi-cozy-cafe.mp3",
                 "lofi-cozy-snow.mp3", "lofi-cozy-tea.mp3", "lofi-chill-license.txt"]
        self.assertEqual(self.suggested(music), [])
        self.assertEqual(self.suggested([f"home-{n}-dark.png" for n in range(5)]), [])
        self.assertEqual(self.suggested([f"wedding {n}.jpg" for n in range(5)]), [])
        self.assertEqual(self.folders([f"Screenshot {n}.png" for n in range(3)]), ["Screenshots"])
        music_off = check_rules([rule["id"] == "music" and {**rule, "on": False} or rule for rule in DEFAULT_RULES])
        self.assertEqual(self.folders(music, defaults=music_off), ["Lofi"])

    def test_topics_across_kinds_stay(self):
        self.assertEqual(self.folders(["wedding 1.jpg", "wedding 2.jpg", "wedding guests.xlsx", "wedding.mp4"]), ["Wedding"])
        self.assertEqual(self.folders(["CS50 Lecture 1.pdf", "cs50 lecture 2.pptx", "Lecture 3 notes.docx"]), ["Lectures"])
        self.assertEqual(self.folders([f"physics {n}.pdf" for n in range(3)]), ["Physics"])

    def test_sites_are_kept_even_for_one_kind(self):
        names = [f"photo {n}.jpg" for n in range(4)]
        hosts = {os.path.join("/d", name): "web.whatsapp.com" for name in names}
        self.assertEqual(self.folders(names, hosts=hosts), ["WhatsApp"])
        hosts = {os.path.join("/d", name): "app-companion-430619.appspot.com" for name in names}
        self.assertEqual(self.folders(names, hosts=hosts), [])

    def test_stronger_bar(self):
        self.assertEqual(self.suggested([f"tax {n}.zzz" for n in range(5)]), [])
        self.assertEqual(self.folders([f"taxes {n}.zzz" for n in range(3)]), ["Taxes"])
        self.assertEqual(self.suggested([f"taxes {n}.zzz" for n in range(2)]), [])
        names = [f"garden rose {n}.zzz" for n in range(4)] + ["rose.zzz", "garden 1.zzz", "garden 2.zzz"]
        self.assertEqual(self.folders(names), ["Garden"])

    def test_regression_names_like_a_real_downloads_folder(self):
        names = [f"Song-Title-{n}(chosic.com).mp3" for n in "abc"] + [
            "Some-Recovery-Key_2026-04-28_someone@gmail.com.pdf",
            *[f"IMG_2025010{n}_1234-removebg-preview.png" for n in range(4)],
            "10th_Marksheet_Doe_Jane.pdf", "12th_Marksheet_Doe_Jane.pdf", "Id_Card_Doe_Jane.jpg", "Certificate_Doe_Jane.pdf",
            *[f"lofi-chill-{n}.mp3" for n in range(3)], *[f"lofi-cozy-{n}.mp3" for n in range(3)],
            "Lecture 1 slides.pptx", "Lecture 2.pdf", "lecture-3-recording.mp4",
            "Invoice March.pdf", "invoice-april.png", "Invoice_May.pdf", "invoice june.pdf",
            *[f"chat photo {n}.jpg" for n in range(3)],
        ]
        hosts = {os.path.join("/d", f"chat photo {n}.jpg"): "web.whatsapp.com" for n in range(3)}
        found = self.suggested(names, hosts=hosts, own_names=["Jane Doe"])
        self.assertEqual([item["folder"] for item in found], ["Invoices", "Lectures", "WhatsApp"])
        self.assertEqual(found[0]["rule"]["words"], ["invoice"])
        self.assertEqual(found[2]["rule"]["sites"], ["web.whatsapp.com"])
        with mock.patch.object(sorting, "MOST_SUGGESTED", 50):
            every = {item["folder"].casefold() for item in self.suggested(names, hosts=hosts, own_names=["Jane Doe"])}
        self.assertFalse(every & {"com", "chosic", "gmail", "someone", "doe", "jane", "preview", "removebg", "chill",
                                  "cozy", "lofi", "title", "song"}, every)
