import contextlib
import http.client
import io
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from importlib.util import find_spec
from pathlib import Path
from unittest import mock

from downkeep import server
from downkeep.model import ActionError, Busy, Identity, Move, Result, Stay
from downkeep.scan import ScanError, scan
from downkeep.server import MAX_BODY, MAX_LIST, PLAN_SECONDS, STALE, Actions, App, make_server
from tests.helpers import FolderTest

REAL = all(find_spec(f"downkeep.{name}") for name in ("store", "engine", "trash", "clear", "sorting", "auto", "schedule"))


class FakeActions:
    """Stands in for every module that changes something, and remembers what it was asked."""

    def __init__(self):
        self.features = {"tidy": True, "trash": True, "restore": True}
        self.scheduled = (True, "")
        self.calls = []
        self.error = None
        self.result = Result(7, ["b.txt"], [])
        self.left = Result()
        self.runs = []
        self.moves = None
        self.stays = [Stay("odd.xyz", "No rule matches")]
        self.saved = [{"id": "images", "folder": "Images", "on": True, "auto": True, "days": 1}]
        self.on = False
        self.hold = None
        self.suggested = [{"folder": "Bills", "why": "3 files have invoice in the name", "rule": {"words": ["invoice"]}, "files": 3}]
        self.settings = {}

    def _do(self, *call):
        self.calls.append(call)
        if self.hold:
            self.hold(call)
        if self.error:
            raise self.error

    def able(self):
        return dict(self.features)

    def schedule(self):
        return self.scheduled

    def recover(self):
        self._do("recover")
        return self.left

    def refresh_schedule(self, folder):
        self._do("refresh", folder)

    def trash(self, folder, names):
        self._do("trash", folder, list(names))
        return self.result

    def plan(self, folder):
        self._do("plan", folder)
        if self.moves is not None:
            return self.moves, self.stays, self.suggested
        return [Move(file, "Documents", "a document") for file in scan(folder).files], self.stays, self.suggested

    def name(self, rule):
        self._do("name", rule)
        return "Invoices"

    def setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self._do("set_setting", key, value)
        self.settings[key] = value

    def tidy(self, folder, moves):
        self._do("tidy", folder, [(move.file.name, move.folder) for move in moves])
        return self.result

    def history(self, limit=30):
        self._do("history")
        return self.runs

    def undo(self, batch):
        self._do("undo", batch)
        return self.result

    def rules(self):
        self._do("rules")
        return {"rules": self.saved, "kinds": [{"id": "images", "name": "Pictures"}], "defaults": []}

    def save_rules(self, rules):
        self._do("save", rules)
        self.saved = rules

    def auto(self, folder):
        self._do("auto", folder)
        return {"available": True, "why": "", "on": self.on, "next": None, "waiting": 0, "folder": folder if self.on else None, "note": ""}

    def set_auto(self, folder, on):
        self._do("set_auto", folder, on)
        self.on = on

    def auto_run(self):
        self._do("auto_run")
        return self.result

    def did(self, name):
        return [call for call in self.calls if call[0] == name]


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


class ActionsTest(FolderTest):
    def setUp(self):
        super().setUp()
        for name in ["a.txt", "b.txt"]:
            self.add(name, b"same")
        self.add("c.pdf", b"other")
        self.clock = Clock()
        self.actions = FakeActions()
        for name, value in {"find_hosts": mock.Mock(return_value={}), "BEAT_SECONDS": 0.02}.items():
            patch = mock.patch(f"downkeep.server.{name}", value)
            patch.start()
            self.addCleanup(patch.stop)
        self.app = App(str(self.folder), mock.Mock(), self.clock, home=self.folder.parent, actions=self.actions)
        self.server = make_server(self.app)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]
        self.key = json.loads(self.send("/api/session", {"code": self.app.code}, key="")[1])["key"]

    def send(self, path, body=None, key=None, raw=None, **headers):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(connection.close)
        content = raw if raw is not None else json.dumps(body or {}).encode()
        key = self.key if key is None else key
        connection.request("POST", path, content, {"Content-Type": "application/json", "X-Downkeep-Key": key, **headers})
        answer = connection.getresponse()
        return answer.status, answer.read()

    def ask(self, path, body=None):
        status, content = self.send(path, body)
        return status, json.loads(content)

    def good(self, path, body=None):
        status, value = self.ask(path, body)
        self.assertEqual(status, 200, value)
        return value

    def refused(self, path, body=None, status=409):
        found, value = self.ask(path, body)
        self.assertEqual(found, status, value)
        return value["error"]

    def plan(self):
        return self.good("/api/tidy/plan")


ROUTES = [
    "/api/rules/name", "/api/tidy/later", "/api/system", "/api/trash", "/api/tidy/plan", "/api/tidy/apply", "/api/history", "/api/undo",
    "/api/rules", "/api/rules/save", "/api/auto", "/api/auto/set",
]


class LockTest(ActionsTest):
    def test_every_route_needs_the_key(self):
        for path in ROUTES:
            for key in ["", "x", self.app.code, self.key[:-1]]:
                with self.subTest(path=path, key=key):
                    self.assertEqual(self.send(path, {}, key=key)[0], 403)
        self.assertEqual(self.actions.calls, [])

    def test_every_route_refuses_other_pages(self):
        for path in ROUTES:
            for headers in [{"Host": "evil.example"}, {"Origin": "https://evil.example"}, {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"}]:
                with self.subTest(path=path, headers=headers):
                    self.assertEqual(self.send(path, {}, **headers)[0], 403)
        self.assertEqual(self.actions.calls, [])

    def test_every_route_takes_only_json_objects(self):
        for path in ROUTES:
            with self.subTest(path=path):
                self.assertEqual(self.send(path, raw=b"[]")[0], 400)
                self.assertEqual(self.send(path, **{"Content-Type": "text/plain"})[0], 400)
        self.assertEqual(self.actions.calls, [])

    def test_no_route_answers_a_plain_visit(self):
        for path in ROUTES:
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            self.addCleanup(connection.close)
            connection.request("GET", path, headers={"X-Downkeep-Key": self.key})
            self.assertEqual(connection.getresponse().status, 404)

    def test_only_lists_may_be_long(self):
        self.assertLessEqual(MAX_LIST, 1_000_000)
        for path in ROUTES:
            limit = MAX_LIST if path in ("/api/trash", "/api/tidy/apply", "/api/rules/save", "/api/rules/name") else MAX_BODY
            with self.subTest(path=path):
                self.assertEqual(self.send(path, raw=b"", **{"Content-Length": str(limit + 1)})[0], 413)
        names = ["n" * 200 for _ in range(400)]
        self.assertEqual(self.send("/api/trash", {"names": names})[0], 409)
        self.assertEqual(self.send("/api/undo", {"batch": 1, "pad": "n" * MAX_BODY})[0], 413)


class TrashTest(ActionsTest):
    def test_moves_what_the_report_proved(self):
        self.good("/api/report")
        self.actions.result = Result(3, ["b.txt"], [Stay("x", "It changed.")])
        value = self.good("/api/trash", {"names": ["b.txt"]})
        self.assertEqual(value, {"batch": 3, "done": ["b.txt"], "skipped": [{"name": "x", "why": "It changed."}]})
        self.assertEqual(self.actions.did("trash"), [("trash", str(self.folder), ["b.txt"])])

    def test_nothing_is_known_before_a_report(self):
        self.refused("/api/trash", {"names": ["b.txt"]})
        self.assertEqual(self.actions.calls, [])

    def test_only_proven_names(self):
        self.good("/api/report")
        for names in [
            ["a.txt"], ["c.pdf"], ["b.txt", "c.pdf"], ["missing"], ["../b.txt"], [str(self.folder / "b.txt")], ["b.txt", "b.txt"],
            [], None, "b.txt", {"b.txt": 1}, [1], [None], [["b.txt"]], ["b.txt\0"], ["B.TXT"],
        ]:
            with self.subTest(names=names):
                self.refused("/api/trash", {"names": names})
        self.assertEqual(self.actions.did("trash"), [])

    def test_odd_names_survive_the_trip(self):
        for name in ["‮fdp.exe", "a\nb.txt", "\udcff.txt"]:
            with self.subTest(name=name):
                self.app.proven = {name}
                self.actions.result = Result(1, [name], [])
                self.assertEqual(self.good("/api/trash", {"names": [name]})["done"], [name])
                self.assertEqual(self.actions.calls[-1][2], [name])

    def test_moved_names_are_forgotten(self):
        self.good("/api/report")
        self.good("/api/trash", {"names": ["b.txt"]})
        self.refused("/api/trash", {"names": ["b.txt"]})
        self.assertEqual(self.ask("/api/show", {"name": "b.txt"})[0], 404)
        self.assertEqual(len(self.actions.did("trash")), 1)

    def test_another_folder_forgets_the_names(self):
        self.good("/api/report")
        (self.folder.parent / "Music").mkdir()
        self.good("/api/choose", {"path": ["Music"]})
        self.refused("/api/trash", {"names": ["b.txt"]})
        self.assertEqual(self.actions.did("trash"), [])

    def test_a_folder_changed_during_a_scan_proves_nothing(self):
        (self.folder.parent / "Music").mkdir()
        real = server.find_duplicates
        with mock.patch("downkeep.server.find_duplicates", lambda files: (self.app.choose(["Music"]), real(files))[1]):
            self.good("/api/report")
        self.refused("/api/trash", {"names": ["b.txt"]})

    def test_not_available(self):
        self.good("/api/report")
        self.actions.features["trash"] = False
        self.assertEqual(self.refused("/api/trash", {"names": ["b.txt"]}), server.NO_TRASH)
        self.assertEqual(self.actions.calls, [])


class TidyTest(ActionsTest):
    def test_plan(self):
        (self.folder / "Documents").mkdir()
        value = self.plan()
        self.assertEqual(value["moves"][0], {"name": "a.txt", "folder": "Documents", "why": "a document", "bytes": 4})
        self.assertEqual([move["name"] for move in value["moves"]], ["a.txt", "b.txt", "c.pdf"])
        self.assertEqual(value["stays"], [{"name": "odd.xyz", "why": "No rule matches"}])
        self.assertEqual(value["folders"], [{"name": "Documents", "exists": True}])
        self.assertGreaterEqual(len(value["plan"]), 16)
        self.assertEqual(self.actions.did("tidy"), [])

    def test_plan_says_which_folders_are_new_or_links(self):
        self.assertEqual(self.plan()["folders"], [{"name": "Documents", "exists": False}])
        (self.folder.parent / "elsewhere").mkdir()
        self.symlink("Documents", self.folder.parent / "elsewhere")
        self.assertEqual(self.plan()["folders"], [{"name": "Documents", "exists": False}])

    def test_applies_only_the_ticked_names_in_plan_order(self):
        made = self.plan()
        value = self.good("/api/tidy/apply", {"plan": made["plan"], "names": ["c.pdf", "a.txt"]})
        self.assertEqual(value["batch"], 7)
        self.assertEqual(self.actions.did("tidy"), [("tidy", str(self.folder), [("a.txt", "Documents"), ("c.pdf", "Documents")])])

    def test_only_names_in_the_plan(self):
        made = self.plan()["plan"]
        for names in [["odd.xyz"], ["missing"], ["a.txt", "../a.txt"], ["a.txt", "a.txt"], [], None, "a.txt", [5]]:
            with self.subTest(names=names):
                self.refused("/api/tidy/apply", {"plan": made, "names": names})
        self.assertEqual(self.actions.did("tidy"), [])
        self.good("/api/tidy/apply", {"plan": made, "names": ["a.txt"]})

    def stale(self, plan):
        self.assertEqual(self.refused("/api/tidy/apply", {"plan": plan, "names": ["a.txt"]}), STALE)
        self.assertEqual(self.actions.did("tidy"), [])

    def test_unknown_plan(self):
        self.stale("x")
        made = self.plan()["plan"]
        for plan in ["", None, 5, [], made[:-1], made + "x"]:
            with self.subTest(plan=plan):
                self.stale(plan)

    def test_another_plan_made_since(self):
        first = self.plan()["plan"]
        self.assertNotEqual(self.plan()["plan"], first)
        self.stale(first)

    def test_too_old(self):
        made = self.plan()["plan"]
        self.clock.now += PLAN_SECONDS + 1
        self.stale(made)

    def test_a_plan_lasts_half_an_hour(self):
        self.assertEqual(PLAN_SECONDS, 30 * 60)

    def test_almost_too_old(self):
        made = self.plan()["plan"]
        self.clock.now += PLAN_SECONDS - 1
        self.good("/api/tidy/apply", {"plan": made, "names": ["a.txt"]})

    def test_another_folder_chosen(self):
        made = self.plan()["plan"]
        (self.folder.parent / "Music").mkdir()
        self.good("/api/choose", {"path": ["Music"]})
        self.stale(made)
        self.good("/api/choose", {"path": ["Downloads"]})
        self.stale(made)

    def test_folder_replaced_by_another(self):
        made = self.plan()["plan"]
        self.folder.rename(self.folder.parent / "old")
        self.folder.mkdir()
        self.stale(made)

    def test_folder_gone(self):
        made = self.plan()["plan"]
        self.folder.rename(self.folder.parent / "old")
        self.stale(made)

    def test_folder_chosen_during_the_plan(self):
        (self.folder.parent / "Music").mkdir()
        self.actions.hold = lambda call: call[0] == "plan" and self.app.choose(["Music"])
        self.stale(self.plan()["plan"])

    def test_a_plan_works_once(self):
        made = self.plan()["plan"]
        self.good("/api/tidy/apply", {"plan": made, "names": ["a.txt"]})
        self.assertEqual(self.refused("/api/tidy/apply", {"plan": made, "names": ["c.pdf"]}), STALE)
        self.assertEqual(len(self.actions.did("tidy")), 1)

    def test_a_failed_tidy_uses_up_the_plan(self):
        made = self.plan()["plan"]
        self.actions.error = ActionError("Nope")
        self.refused("/api/tidy/apply", {"plan": made, "names": ["a.txt"]})
        self.actions.error = None
        self.assertEqual(self.refused("/api/tidy/apply", {"plan": made, "names": ["a.txt"]}), STALE)

    def test_any_change_makes_the_plan_stale(self):
        self.good("/api/report")
        for path, body in [("/api/trash", {"names": ["b.txt"]}), ("/api/undo", {"batch": 3}), ("/api/rules/save", {"rules": []})]:
            with self.subTest(path=path):
                made = self.plan()["plan"]
                self.good(path, body)
                self.stale(made)

    def test_looking_does_not_make_the_plan_stale(self):
        made = self.plan()["plan"]
        for path in ["/api/system", "/api/history", "/api/rules", "/api/auto", "/api/report"]:
            self.good(path)
        self.good("/api/tidy/apply", {"plan": made, "names": ["a.txt"]})

    def test_not_available(self):
        made = self.plan()["plan"]
        self.actions.features["tidy"] = False
        for path, body in [("/api/tidy/plan", {}), ("/api/tidy/apply", {"plan": made, "names": ["a.txt"]}), ("/api/undo", {"batch": 1}), ("/api/rules", {}), ("/api/rules/save", {"rules": []})]:
            with self.subTest(path=path):
                self.assertEqual(self.refused(path, body), server.NO_TIDY)
        self.assertEqual(self.good("/api/history"), {"batches": []})
        self.assertEqual([call[0] for call in self.actions.calls], ["plan"])

    def test_unreadable_folder(self):
        self.actions.error = ScanError("Can't read it\n")
        self.assertEqual(self.refused("/api/tidy/plan"), "Can't read it\N{REPLACEMENT CHARACTER}")


class SuggestTest(ActionsTest):
    def test_plan_brings_suggestions(self):
        self.assertEqual(self.plan()["suggested"], self.actions.suggested)

    def test_at_most_three(self):
        self.actions.suggested = [{"folder": f"F{number}", "why": "", "rule": {}, "files": 1} for number in range(5)]
        self.assertEqual([item["folder"] for item in self.plan()["suggested"]], ["F0", "F1", "F2"])

    def test_not_now_hides_them_until_they_change(self):
        self.plan()
        self.assertEqual(self.good("/api/tidy/later"), {})
        self.assertEqual(self.actions.settings[server.HIDDEN], {str(self.folder): ["Bills"]})
        self.assertEqual(self.plan()["suggested"], [])
        self.actions.suggested = [*self.actions.suggested, {"folder": "Lectures", "why": "", "rule": {}, "files": 2}]
        self.assertEqual(len(self.plan()["suggested"]), 2)

    def test_not_now_is_per_folder(self):
        self.plan()
        self.good("/api/tidy/later")
        (self.folder.parent / "Music").mkdir()
        self.good("/api/choose", {"path": ["Music"]})
        self.assertEqual(len(self.plan()["suggested"]), 1)

    def test_not_now_needs_a_plan_for_this_folder(self):
        self.assertEqual(self.refused("/api/tidy/later"), STALE)
        self.plan()
        (self.folder.parent / "Music").mkdir()
        self.good("/api/choose", {"path": ["Music"]})
        self.assertEqual(self.refused("/api/tidy/later"), STALE)
        self.assertEqual(self.actions.did("set_setting"), [])

    def test_not_now_keeps_a_short_list(self):
        self.actions.settings[server.HIDDEN] = {f"/f{number}": [] for number in range(80)}
        self.plan()
        self.good("/api/tidy/later")
        hidden = self.actions.settings[server.HIDDEN]
        self.assertEqual(len(hidden), server.HIDDEN_FOLDERS)
        self.assertEqual(list(hidden)[-1], str(self.folder))

    def test_odd_settings_are_ignored(self):
        self.actions.settings[server.HIDDEN] = ["x"]
        self.assertEqual(len(self.plan()["suggested"]), 1)
        self.good("/api/tidy/later")

    def test_not_now_does_not_make_the_plan_stale(self):
        made = self.plan()["plan"]
        self.good("/api/tidy/later")
        self.good("/api/tidy/apply", {"plan": made, "names": ["a.txt"]})

    def test_name(self):
        self.assertEqual(self.good("/api/rules/name", {"rule": {"words": ["invoice"]}}), {"folder": "Invoices"})
        for rule in [None, [], "x", 5]:
            with self.subTest(rule=rule):
                self.refused("/api/rules/name", {"rule": rule})
        self.assertEqual(len(self.actions.did("name")), 1)
        self.actions.features["tidy"] = False
        self.refused("/api/rules/name", {"rule": {}})

    def test_name_takes_a_long_rule(self):
        rule = {"words": ["w" * 200 for _ in range(100)]}
        self.assertGreater(len(json.dumps({"rule": rule})), MAX_BODY)
        self.good("/api/rules/name", {"rule": rule})

    def test_name_changes_nothing(self):
        made = self.plan()["plan"]
        self.good("/api/rules/name", {"rule": {"words": ["x"]}})
        self.assertEqual(self.actions.did("save") + self.actions.did("set_setting"), [])
        self.good("/api/tidy/apply", {"plan": made, "names": ["a.txt"]})

    def test_not_available(self):
        self.actions.features["tidy"] = False
        self.refused("/api/tidy/later")

    def test_reasons_say_what_the_file_is(self):
        cases = {
            ("a.pdf", "Documents go to Documents"): "A PDF document",
            ("b.XLSX", "Spreadsheets go to Documents"): "A spreadsheet",
            ("cat.png", "Pictures go to Images"): "A picture",
            ("x.zz", ".zz files go to Odd"): "A .zz file",
            ("bill.pdf", "The name contains invoice"): "Has invoice in the name",
            ("r.zip", "Came from github.com"): "Came from github.com",
            ("x.pdf.exe", "Named like a .pdf file but really .exe. Installers go to Installers"):
                "Named like a .pdf file but really .exe. A Windows installer",
            ("odd", "Something new"): "Something new",
        }
        for (name, why), expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(server.reason(name, why), expected)
        self.actions.moves = [Move(scan(self.folder).files[2], "Documents", "Documents go to Documents")]
        self.assertEqual(self.plan()["moves"][0]["why"], "A PDF document")


class OtherRoutesTest(ActionsTest):
    def test_undo(self):
        self.assertEqual(self.good("/api/undo", {"batch": 7})["done"], ["b.txt"])
        self.assertEqual(self.actions.did("undo"), [("undo", 7)])
        for batch in [None, "7", 7.0, True, 0, -1, [7], {}]:
            with self.subTest(batch=batch):
                self.refused("/api/undo", {"batch": batch})
        self.assertEqual(len(self.actions.did("undo")), 1)

    def test_history(self):
        self.actions.runs = [{"batch": 1, "kind": "tidy"}]
        self.assertEqual(self.good("/api/history"), {"batches": [{"batch": 1, "kind": "tidy"}]})

    def test_rules(self):
        self.assertEqual(self.good("/api/rules")["rules"][0]["id"], "images")
        saved = self.good("/api/rules/save", {"rules": [{"id": "x"}]})
        self.assertEqual(saved["rules"], [{"id": "x"}])
        self.assertEqual(self.actions.did("save"), [("save", [{"id": "x"}])])

    def test_bad_rules_show_the_reason(self):
        self.actions.error = ActionError("A folder needs a name")
        self.assertEqual(self.refused("/api/rules/save", {"rules": []}), "A folder needs a name")

    def test_system(self):
        self.actions.features["restore"] = False
        self.actions.scheduled = (False, "No timer here")
        self.assertEqual(self.good("/api/system"), {
            "tidy": True, "trash": True, "restore": False,
            "auto": {"available": False, "why": "No timer here"}, "recovered": None,
        })

    def test_automatic(self):
        self.assertFalse(self.good("/api/auto")["on"])
        self.assertTrue(self.good("/api/auto/set", {"on": True})["on"])
        self.assertFalse(self.good("/api/auto/set", {"on": False})["on"])
        self.assertEqual([call[1:] for call in self.actions.did("set_auto")], [(str(self.folder), True), (str(self.folder), False)])

    def test_automatic_needs_a_plain_yes_or_no(self):
        for on in [None, 1, 0, "true", "", []]:
            with self.subTest(on=on):
                self.refused("/api/auto/set", {"on": on})
        self.assertEqual(self.actions.did("set_auto"), [])

    def test_automatic_not_available_can_still_be_turned_off(self):
        self.actions.scheduled = (False, "No timer here")
        self.assertEqual(self.refused("/api/auto/set", {"on": True}), "No timer here")
        self.assertEqual(self.actions.did("set_auto"), [])
        self.good("/api/auto/set", {"on": False})

    def test_automatic_on_an_unsupported_system(self):
        self.actions.features["tidy"] = False
        value = self.good("/api/auto")
        self.assertEqual((value["available"], value["on"], value["why"]), (False, False, server.NO_AUTO))
        self.assertEqual(self.refused("/api/auto/set", {"on": True}), server.NO_AUTO)
        self.assertEqual(self.actions.calls, [])

    def test_errors_are_plain(self):
        calls = [
            ("/api/history", {}), ("/api/undo", {"batch": 1}), ("/api/rules", {}), ("/api/rules/save", {"rules": []}),
            ("/api/auto", {}), ("/api/auto/set", {"on": False}), ("/api/tidy/plan", {}),
        ]
        for path, body in calls:
            for error, status in [(ActionError("It is open."), 409), (ActionError(""), 409), (Busy(""), 409), (OSError("secret"), 500), (KeyError("secret"), 500)]:
                with self.subTest(path=path, error=error):
                    self.actions.error = error
                    found, value = self.ask(path, body)
                    self.assertEqual(found, status)
                    self.assertTrue(value["error"])
                    self.assertNotIn("secret", json.dumps(value))
                    self.assertEqual(value.get("busy", False), isinstance(error, Busy))

    def test_the_server_keeps_working_after_a_failure(self):
        self.actions.error = KeyError("x")
        self.refused("/api/undo", {"batch": 1}, 500)
        self.actions.error = None
        self.good("/api/undo", {"batch": 1})
        self.assertEqual((self.app.working, self.app.acting), (0, 0))


class OneAtATimeTest(ActionsTest):
    def start(self, path, body):
        thread = threading.Thread(target=lambda: self.send(path, body), daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        return thread

    def waiting(self):
        started, carry_on = threading.Event(), threading.Event()
        self.addCleanup(carry_on.set)

        def hold(call):
            if call[0] in ("undo", "trash", "tidy", "set_auto"):
                started.set()
                carry_on.wait(5)

        self.actions.hold = hold
        return started, carry_on

    def test_never_stops_while_files_are_moved(self):
        started, carry_on = self.waiting()
        thread = self.start("/api/undo", {"batch": 1})
        self.assertTrue(started.wait(5))
        self.clock.now += 1_000_000
        self.assertEqual(self.app.pages, 0)
        self.assertFalse(self.app.finished())
        carry_on.set()
        thread.join(5)
        self.assertFalse(self.app.finished())
        self.clock.now += server.CLOSE_SECONDS + 1
        self.assertTrue(self.app.finished())

    def test_finishes_when_the_page_goes_away(self):
        self.good("/api/report")
        started, carry_on = self.waiting()
        finished = threading.Event()
        real = self.actions.trash
        self.actions.trash = lambda *args: (real(*args), finished.set())[0]
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request("POST", "/api/trash", json.dumps({"names": ["b.txt"]}), {"Content-Type": "application/json", "X-Downkeep-Key": self.key})
        self.assertTrue(started.wait(5))
        connection.close()
        carry_on.set()
        self.assertTrue(finished.wait(5))
        self.app.settle()
        self.good("/api/history")

    def test_settle_waits_for_the_move(self):
        started, carry_on = self.waiting()
        self.start("/api/undo", {"batch": 1})
        self.assertTrue(started.wait(5))
        settled = threading.Event()
        threading.Thread(target=lambda: (self.app.settle(), settled.set()), daemon=True).start()
        self.assertFalse(settled.wait(0.2))
        carry_on.set()
        self.assertTrue(settled.wait(5))

    def test_a_scan_waits_for_the_move(self):
        for path, body in [("/api/report", {}), ("/api/tidy/plan", {})]:
            with self.subTest(path=path):
                started, carry_on = self.waiting()
                self.start("/api/undo", {"batch": 1})
                self.assertTrue(started.wait(5))
                with mock.patch("downkeep.server.scan", side_effect=scan) as scanned:
                    thread = self.start(path, body)
                    time.sleep(0.15)
                    self.assertFalse(scanned.called)
                    self.assertEqual(self.actions.did("plan"), [])
                    carry_on.set()
                    thread.join(5)
                self.actions.calls.clear()

    def test_the_move_waits_for_a_scan(self):
        self.good("/api/report")
        scanning, carry_on = threading.Event(), threading.Event()
        self.addCleanup(carry_on.set)
        real = server.find_duplicates

        def slow(files):
            scanning.set()
            carry_on.wait(5)
            return real(files)

        with mock.patch("downkeep.server.find_duplicates", slow):
            self.start("/api/report", {})
            self.assertTrue(scanning.wait(5))
            thread = self.start("/api/trash", {"names": ["b.txt"]})
            time.sleep(0.15)
            self.assertEqual(self.actions.did("trash"), [])
            carry_on.set()
            thread.join(5)
        self.assertEqual(len(self.actions.did("trash")), 1)

    def test_two_moves_never_overlap(self):
        started, carry_on = self.waiting()
        self.start("/api/undo", {"batch": 1})
        self.assertTrue(started.wait(5))
        second = self.start("/api/auto/set", {"on": True})
        time.sleep(0.15)
        self.assertEqual(self.actions.did("set_auto"), [])
        carry_on.set()
        second.join(5)
        self.assertEqual(len(self.actions.did("set_auto")), 1)


class RecoverTest(ActionsTest):
    def test_what_was_found_is_available_to_the_page(self):
        self.actions.left = Result(None, ["a.txt"], [Stay("b.txt", "Look at it")])
        self.app.recover()
        self.assertEqual(self.good("/api/system")["recovered"], {"batch": None, "done": ["a.txt"], "skipped": [{"name": "b.txt", "why": "Look at it"}]})
        self.assertEqual(self.actions.calls, [("recover",), ("refresh", str(self.folder))])

    def test_the_window_opens_whatever_happens(self):
        for error in [Busy("x"), ActionError("x"), OSError("x"), KeyError("x")]:
            with self.subTest(error=error):
                self.actions.error = error
                self.app.recover()
                self.assertIsNone(self.app.recovered)
        self.actions.error = None
        self.assertIsNone(self.good("/api/system")["recovered"])

    def test_nothing_to_recover_on_an_unsupported_system(self):
        self.actions.features["tidy"] = False
        self.app.recover()
        self.assertEqual(self.actions.calls, [])


def module(name, **values):
    made = types.ModuleType(f"downkeep.{name}")
    made.__dict__.update(values)
    return made


class FakeStore:
    opened = closed = 0

    def __init__(self, folder=None):
        self.settings = {}
        FakeStore.opened += 1

    def __enter__(self):
        return self

    def __exit__(self, *_):
        FakeStore.closed += 1

    def get(self, key, default=None):
        return self.settings.get(key, default)

    def set(self, key, value):
        self.settings[key] = value

    def batch(self, batch):
        return None if batch == 404 else types.SimpleNamespace(kind="auto" if batch == 9 else "tidy")

    def history(self, limit):
        return [limit]


class DefaultActionsTest(unittest.TestCase):
    """The real class with every module it uses replaced."""

    def setUp(self):
        FakeStore.opened = FakeStore.closed = 0
        self.log = []
        note = lambda name, value=None: lambda *args: (self.log.append((name, *args[1:])), value)[1]
        self.modules = {
            "store": module("store", Store=FakeStore),
            "engine": module("engine", SUPPORTED=True, recover=note("recover", Result()), apply_moves=note("apply", Result()), undo=note("undo", Result(1, ["x"]))),
            "trash": module("trash", SUPPORTED=True, CAN_RESTORE=True),
            "clear": module("clear", apply_clear=note("clear", Result())),
            "auto": module("auto", status=note("status", {"on": True}), turn_on=note("on"), turn_off=note("off"), run=note("run", Result()), note_undo=note("note")),
            "schedule": module("schedule", available=lambda: (True, ""), installed_command=lambda: ["/gone/downkeep"], command=lambda: ["/here/downkeep"], install=lambda command: self.log.append(("install", command))),
        }
        import downkeep

        for name, value in self.modules.items():
            for patch in [mock.patch.dict(sys.modules, {f"downkeep.{name}": value}), mock.patch.object(downkeep, name, value, create=True)]:
                patch.start()
                self.addCleanup(patch.stop)
        self.actions = Actions()

    def test_every_call_reaches_its_module_and_closes_the_store(self):
        self.actions.recover()
        self.actions.trash("/d", ["a"])
        self.actions.tidy("/d", [1])
        self.actions.undo(4)
        self.actions.auto("/d")
        self.actions.set_auto("/d", True)
        self.actions.set_auto("/d", False)
        self.actions.auto_run()
        self.assertEqual(self.actions.history(5), [5])
        self.assertEqual(self.log, [
            ("recover",), ("recover",), ("clear", "/d", ["a"]), ("apply", "/d", [1]), ("undo", 4), ("status", "/d"), ("on", "/d"), ("off",), ("run",),
        ])
        self.assertEqual(FakeStore.opened, FakeStore.closed)

    def test_only_an_undone_automatic_run_is_noted(self):
        self.actions.undo(9)
        self.assertEqual(self.log, [("undo", 9), ("note", 9)])
        self.log.clear()
        self.modules["engine"].undo = lambda store, batch: Result(1, [], [Stay("x", "y")])
        self.actions.undo(9)
        self.assertEqual(self.log, [])

    def test_an_unknown_run_is_refused_plainly(self):
        with self.assertRaisesRegex(ActionError, "isn't in the history"):
            self.actions.undo(404)
        self.assertEqual(self.log, [])

    def test_a_note_that_fails_does_not_hide_the_undo(self):
        self.modules["auto"].note_undo = mock.Mock(side_effect=ActionError("x"))
        self.assertEqual(self.actions.undo(9).done, ["x"])

    def test_what_is_possible_here(self):
        self.assertEqual(self.actions.able(), {"tidy": True, "trash": True, "restore": True})
        self.modules["trash"].SUPPORTED = False
        self.assertEqual(self.actions.able()["trash"], False)
        self.modules["engine"].SUPPORTED = False
        self.assertEqual(self.actions.able(), {"tidy": False, "trash": False, "restore": False})
        self.assertEqual(self.actions.schedule(), (False, server.NO_AUTO))

    def test_missing_modules_mean_not_available(self):
        with mock.patch.dict(sys.modules, {"downkeep.engine": None}), mock.patch.object(sys.modules["downkeep"], "engine"):
            del sys.modules["downkeep"].engine
            self.assertEqual(Actions().able(), {"tidy": False, "trash": False, "restore": False})

    def test_schedule_follows_a_moved_app(self):
        self.actions.refresh_schedule("/d")
        self.assertIn(("install", ["/here/downkeep"]), self.log)

    def test_schedule_is_left_alone_otherwise(self):
        for installed in [None, [sys.executable, "-m", "downkeep", "auto-run"]]:
            with self.subTest(installed=installed):
                self.modules["schedule"].installed_command = lambda: installed
                self.actions.refresh_schedule("/d")
        self.modules["schedule"].installed_command = lambda: ["/gone/downkeep"]
        self.modules["auto"].status = lambda store, folder: {"on": False}
        self.actions.refresh_schedule("/d")
        self.assertNotIn("install", [entry[0] for entry in self.log])


class SmallPartsTest(unittest.TestCase):
    def test_names(self):
        self.assertEqual(server._names(["a"], {"a", "b"}), ["a"])
        for value in [[], ["a", "a"], ["c"], [1], "a", None, ("a",)]:
            with self.subTest(value=value), self.assertRaises(ActionError):
                server._names(value, {"a", "b"})

    def test_identity_is_kept_in_the_plan(self):
        with tempfile.TemporaryDirectory() as holder:
            folder = Path(holder).resolve() / "d"
            folder.mkdir()
            (folder / "a.txt").write_bytes(b"x")
            os.utime(folder / "a.txt", (1, 1))
            app = App(str(folder), mock.Mock(), actions=FakeActions())
            made = app.plan()
            move = app.planned.moves["a.txt"]
            info = os.stat(folder / "a.txt")
            self.assertEqual(Identity.of(move.file), Identity(info.st_dev, info.st_ino, 1, info.st_mtime_ns))
            self.assertEqual(made["plan"], app.planned.id)


@unittest.skipUnless(REAL, "the modules that move files are not here")
class RealTest(ActionsTest):
    """Everything together, over temporary folders. Only the schedule is replaced."""

    def setUp(self):
        super().setUp()
        base = self.folder.parent
        os.environ.pop("DOWNKEEP_LINK_FILE", None)
        patch = mock.patch.dict(os.environ, {
            "DOWNKEEP_DATA": str(base / "records"), "XDG_DATA_HOME": str(base / "data"),
            "XDG_STATE_HOME": str(base / "state"), "XDG_CONFIG_HOME": str(base / "config"),
        })
        patch.start()
        self.addCleanup(patch.stop)
        self.installed = None
        fakes = {
            "downkeep.schedule.available": lambda *args: (True, ""),
            "downkeep.schedule.install": lambda command, *args: setattr(self, "installed", command),
            "downkeep.schedule.remove": lambda *args: setattr(self, "installed", None),
            "downkeep.schedule.installed_command": lambda *args: self.installed,
            "downkeep.schedule.command": lambda: ["/here/downkeep", "--auto-run"],
            "downkeep.engine.site_of": lambda path: None,
            "downkeep.engine.save_site": mock.Mock(side_effect=AssertionError),
        }
        for name, value in fakes.items():
            patch = mock.patch(name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.app.actions = Actions()
        able = self.app.actions.able()
        if not (able["tidy"] and able["trash"]):
            self.skipTest("files can't be moved on this system yet")

    def names(self):
        return sorted(item.name for item in self.folder.iterdir())

    def test_trash_settles_an_earlier_crash_first(self):
        from downkeep.store import Store

        self.good("/api/report")
        file = next(file for file in scan(self.folder).files if file.name == "c.pdf")
        with Store() as store:
            batch = store.begin("tidy", str(self.folder))
            store.intent(batch, "move", file.path, str(self.folder / "Documents" / "c.pdf"), Identity.of(file))
        self.good("/api/trash", {"names": ["b.txt"]})
        with Store() as store:
            self.assertEqual(store.open_intents(), [])

    def test_undo_of_an_unknown_run(self):
        self.assertIn("isn't in the history", self.refused("/api/undo", {"batch": 999}))

    def test_trash_and_back(self):
        if not self.app.actions.able()["restore"]:
            self.skipTest("the Recycle Bin can't be checked here")
        self.assertEqual(self.good("/api/report")["duplicates"][0]["names"], ["a.txt", "b.txt"])
        result = self.good("/api/trash", {"names": ["b.txt"]})
        self.assertEqual((result["done"], result["skipped"]), (["b.txt"], []))
        self.assertEqual(self.names(), ["a.txt", "c.pdf"])
        run = self.good("/api/history")["batches"][0]
        self.assertEqual((run["kind"], run["count"], run["names"], run["can_undo"]), ("trash", 1, ["b.txt"], True))
        self.assertEqual(self.good("/api/undo", {"batch": result["batch"]})["done"], ["b.txt"])
        self.assertEqual(self.names(), ["a.txt", "b.txt", "c.pdf"])
        self.assertEqual((self.folder / "b.txt").read_bytes(), b"same")

    def test_a_copy_that_changed_after_the_report_stays(self):
        self.good("/api/report")
        self.add("b.txt", b"mine")
        result = self.good("/api/trash", {"names": ["b.txt"]})
        self.assertEqual((result["done"], len(result["skipped"])), ([], 1))
        self.assertEqual((self.folder / "b.txt").read_bytes(), b"mine")

    def test_tidy_and_back(self):
        made = self.plan()
        self.assertEqual({move["folder"] for move in made["moves"]}, {"Documents"})
        result = self.good("/api/tidy/apply", {"plan": made["plan"], "names": ["a.txt", "c.pdf"]})
        self.assertEqual(sorted(result["done"]), ["a.txt", "c.pdf"])
        self.assertEqual(self.names(), ["Documents", "b.txt"])
        self.assertEqual(self.good("/api/report")["files"], 1)
        back = self.good("/api/undo", {"batch": result["batch"]})
        self.assertEqual(sorted(back["done"]), ["a.txt", "c.pdf"])
        self.assertEqual(self.names(), ["a.txt", "b.txt", "c.pdf"])
        self.assertEqual([run["kind"] for run in self.good("/api/history")["batches"]], ["undo", "tidy"])
        self.refused("/api/undo", {"batch": result["batch"]})
        self.refused("/api/undo", {"batch": 999})

    def test_a_file_that_changed_after_the_plan_stays(self):
        made = self.plan()
        self.add("a.txt", b"longer than before", days=5)
        result = self.good("/api/tidy/apply", {"plan": made["plan"], "names": ["a.txt", "c.pdf"]})
        self.assertEqual((result["done"], [stay["name"] for stay in result["skipped"]]), (["c.pdf"], ["a.txt"]))
        self.assertTrue((self.folder / "a.txt").exists())

    def test_nothing_is_overwritten(self):
        made = self.plan()
        (self.folder / "Documents").mkdir()
        (self.folder / "Documents" / "c.pdf").write_bytes(b"older")
        result = self.good("/api/tidy/apply", {"plan": made["plan"], "names": ["c.pdf"]})
        self.assertEqual(result["done"], [])
        self.assertEqual((self.folder / "Documents" / "c.pdf").read_bytes(), b"older")
        self.assertEqual((self.folder / "c.pdf").read_bytes(), b"other")

    def test_rules(self):
        found = self.good("/api/rules")
        self.assertEqual(found["rules"], found["defaults"])
        self.assertTrue(all(kind["name"] for kind in found["kinds"]))
        own = {"id": "", "folder": "Letters", "words": ["c"], "sites": [], "kinds": [], "extensions": [], "on": True, "auto": False, "days": 7}
        saved = self.good("/api/rules/save", {"rules": [own, *found["rules"]]})["rules"]
        self.assertEqual((saved[0]["folder"], saved[0]["id"]), ("Letters", "own-1"))
        folders = {move["name"]: move["folder"] for move in self.plan()["moves"]}
        self.assertEqual(folders["c.pdf"], "Letters")
        self.assertIn("name", self.refused("/api/rules/save", {"rules": [{**own, "folder": ""}]}))
        for rules in [None, "x", [1], [{"folder": "../up"}], [{**own, "evil": 1}]]:
            with self.subTest(rules=rules):
                self.refused("/api/rules/save", {"rules": rules})
        self.assertEqual(self.good("/api/rules")["rules"], saved)

    def test_suggestions_and_names(self):
        from downkeep import sorting

        if not hasattr(sorting, "suggest_name"):
            self.skipTest("suggested names are not here yet")
        for number in range(4):
            self.add(f"invoice {number}.pdf", b"bill %d" % number)
        made = self.plan()
        self.assertTrue(any("invoice" in item["why"].lower() for item in made["suggested"]), made["suggested"])
        for item in made["suggested"]:
            self.assertLessEqual({"folder", "why", "rule", "files"}, set(item))
        rule = {"id": "", "folder": "", "words": ["invoice"], "sites": [], "kinds": [], "extensions": [], "on": True, "auto": False, "days": 7}
        self.assertTrue(self.good("/api/rules/name", {"rule": rule})["folder"])
        self.assertEqual(self.names(), ["a.txt", "b.txt", "c.pdf", *[f"invoice {number}.pdf" for number in range(4)]])
        for bad in [{**rule, "words": [], "kinds": []}, {**rule, "evil": 1}, {**rule, "kinds": ["nope"]}]:
            with self.subTest(bad=bad):
                self.refused("/api/rules/name", {"rule": bad})
        if made["suggested"]:
            self.good("/api/tidy/later")
            self.assertEqual(self.plan()["suggested"], [])

    def test_automatic(self):
        self.assertFalse(self.good("/api/auto")["on"])
        status = self.good("/api/auto/set", {"on": True})
        self.assertEqual((status["on"], status["folder"]), (True, str(self.folder)))
        self.assertEqual(self.installed, ["/here/downkeep", "--auto-run"])
        self.app.actions.auto_run()
        self.assertEqual(self.names(), ["a.txt", "b.txt", "c.pdf"])
        self.assertFalse(self.good("/api/auto/set", {"on": False})["on"])
        self.assertIsNone(self.installed)

    def test_busy(self):
        from downkeep.store import Store

        made = self.plan()["plan"]
        with Store() as store, store.lock():
            status, value = self.ask("/api/tidy/apply", {"plan": made, "names": ["a.txt"]})
        self.assertEqual((status, value["busy"]), (409, True))
        self.assertEqual(self.names(), ["a.txt", "b.txt", "c.pdf"])

    def test_an_interrupted_run_is_shown(self):
        from downkeep.store import Store

        file = next(file for file in scan(self.folder).files if file.name == "c.pdf")
        with Store() as store:
            batch = store.begin("tidy", str(self.folder))
            store.intent(batch, "move", file.path, str(self.folder / "Documents" / "c.pdf"), Identity.of(file))
        self.app.recover()
        found = self.good("/api/system")["recovered"]
        self.assertEqual((found["done"], [stay["name"] for stay in found["skipped"]]), ([], ["c.pdf"]))
        self.app.recover()
        self.assertEqual(self.good("/api/system")["recovered"]["skipped"], [])

    def test_commands(self):
        from downkeep.cli import main

        def run(*args):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), mock.patch("downkeep.cli.downloads_folder", return_value=self.folder):
                code = main(list(args))
            return code, out.getvalue()

        self.assertIn("3 files would move into 1 folder", run("tidy")[1])
        self.assertEqual(self.names(), ["a.txt", "b.txt", "c.pdf"])
        self.assertIn("Moved 3 files", run("tidy", "--apply")[1])
        self.assertEqual(self.names(), ["Documents"])
        self.assertIn("Tidied, 3 files", run("history")[1])
        self.assertIn("Put back 3 files", run("undo")[1])
        self.assertEqual(self.names(), ["a.txt", "b.txt", "c.pdf"])
        self.assertIn("nothing to undo", run("undo")[1])
        self.assertIn("is on for", run("auto", "on")[1])
        self.assertEqual(run("auto-run"), (0, ""))
        self.assertIn("is off", run("auto", "off")[1])


if __name__ == "__main__":
    unittest.main()
