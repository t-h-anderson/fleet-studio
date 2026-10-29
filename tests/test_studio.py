"""Backend regression tests. Uses temporary data and a mocked process launcher."""
from copy import deepcopy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fleet_studio import Application, ConflictError, Server, Store, ValidationError, validate_fleets, atomic_json
import studio_runtime


class StudioTests(unittest.TestCase):
    def test_atomic_save_recovers_transient_windows_sharing_failure(self):
        target = self.root / "shared.json"
        atomic_json(target, {"before": True})
        import os
        replace = os.replace
        failure = PermissionError("Windows sharing conflict")
        failure.winerror = 32
        with patch("fleet_studio.os.replace", side_effect=[failure, None]) as mocked:
            def retry(source, destination):
                if mocked.call_count == 1:
                    raise failure
                replace(source, destination)
            mocked.side_effect = retry
            atomic_json(target, {"after": True})
        self.assertEqual(json.loads(target.read_text()), {"after": True})
        with patch("fleet_studio.os.replace", side_effect=failure):
            with self.assertRaises(PermissionError):
                atomic_json(target, {"lost": True})
        self.assertEqual(json.loads(target.read_text()), {"after": True})
        self.assertEqual(list(self.root.glob("shared.json.*.tmp")), [])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix=".test-unit-", dir=Path(__file__).resolve().parent)
        self.root = Path(self.temporary.name).resolve()
        assert self.root.parent == Path(__file__).resolve().parent
        assert self.root.name.startswith(".test-unit-")
        self.legacy = {
            "version": 1, "windowName": "test-fleet", "codexPath": sys.executable,
            "agents": [
                {"name": "Writer A", "directory": str(self.root), "sessionId": str(uuid.uuid4()), "enabled": True},
                {"name": "Writer B", "directory": str(self.root), "sessionId": str(uuid.uuid4()), "enabled": True},
            ],
        }
        (self.root / "fleet.json").write_text(json.dumps(self.legacy))
        self.store = Store(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def test_save_persists_design_and_updates_existing_launcher(self):
        state = self.store.snapshot()
        fleet = state["fleets"][0]
        fleet["agents"][0]["name"] = "Reviewer's desk"
        fleet["agents"][0]["position"] = {"x": 333, "y": 444}
        saved = self.store.save(state)
        self.assertEqual(saved["revision"], 2)
        self.assertEqual(Store(self.root).snapshot(), saved)
        compatible = json.loads((self.root / "fleet.json").read_text())
        self.assertEqual(compatible["agents"][0]["name"], "Reviewer's desk")
        self.assertEqual(compatible["agents"][0]["sessionId"], self.legacy["agents"][0]["sessionId"])
        self.assertEqual(compatible["codexPath"], sys.executable)

    def test_stale_save_does_not_overwrite_newer_work(self):
        old = self.store.snapshot()
        newer = deepcopy(old)
        newer["fleets"][0]["description"] = "Newer work"
        self.store.save(newer)
        with self.assertRaises(ConflictError):
            self.store.save(old)
        self.assertEqual(self.store.snapshot()["fleets"][0]["description"], "Newer work")

    def test_duplicate_session_is_rejected_without_writing(self):
        state = self.store.snapshot()
        state["fleets"][0]["agents"][1]["sessionId"] = state["fleets"][0]["agents"][0]["sessionId"]
        before = (self.root / "fleet-studio.json").read_bytes()
        with self.assertRaises(ValidationError):
            self.store.save(state)
        self.assertEqual((self.root / "fleet-studio.json").read_bytes(), before)

    def test_draft_role_can_be_saved_but_cannot_be_launched(self):
        state = self.store.snapshot()
        state["fleets"][0]["agents"][0].update(sessionId="", directory="")
        saved = self.store.save(state)
        fleet = saved["fleets"][0]
        with self.assertRaises(ValidationError):
            self.store.launch_config(fleet, [fleet["agents"][0]["id"]])
        self.assertEqual(len(json.loads((self.root / "fleet.json").read_text())["agents"]), 1)

    def test_retired_agent_links_are_dropped_on_save(self):
        # The agent layout's planning links never routed work; old saves still load, and the links go on the next save.
        state = self.store.snapshot()
        fleet = state["fleets"][0]
        first, second = [agent["id"] for agent in fleet["agents"]]
        fleet["links"] = [{"id": str(uuid.uuid4()), "source": first, "target": first, "label": "self"},
                          {"id": str(uuid.uuid4()), "source": first, "target": second, "label": "Review"}]
        saved = self.store.save(state)
        self.assertNotIn("links", saved["fleets"][0])
        self.assertNotIn("links", Store(self.root).snapshot()["fleets"][0])


    def test_relative_folders_are_resolved_before_launch_snapshot(self):
        state = self.store.snapshot()
        state["fleets"][0]["agents"][0]["directory"] = "."
        saved = self.store.save(state)
        self.assertEqual(saved["fleets"][0]["agents"][0]["directory"], str(Path.cwd().resolve()))

    def test_invalid_terminal_name_and_nonfinite_positions(self):
        for change in ("window", "position", "directory", "name"):
            state = self.store.snapshot()
            f = state["fleets"][0]
            if change == "window":
                f["windowName"] = "window;command"
            elif change == "position":
                f["agents"][0]["position"]["x"] = float("nan")
            elif change == "directory":
                f["agents"][0]["directory"] = str(self.root / "absent")
            else:
                f["agents"][0]["name"] = "Agent; command"
            with self.subTest(change=change), self.assertRaises(ValidationError):
                validate_fleets(state["fleets"])

    def test_launch_uses_selected_snapshot_and_existing_powershell_entry(self):
        app = Application(self.root)
        fleet = app.store.snapshot()["fleets"][0]
        config = app.store.launch_config(fleet, [fleet["agents"][0]["id"]])
        job_id = uuid.uuid4().hex
        app.jobs[job_id] = {"id": job_id, "status": "running"}
        app.windows = True
        with patch("fleet_studio.subprocess.run", return_value=types.SimpleNamespace(
            returncode=0, stdout="Opened 1 tab(s) in window 'test-fleet'.", stderr=""
        )) as execute, patch.dict("os.environ", {"WINDIR": "C:/Windows"}):
            app._run_launch(job_id, fleet, config)
        command = execute.call_args.args[0]
        self.assertIn(str(Path(__file__).resolve().parents[1] / "Start-CodexFleet.ps1"), command)
        snapshot_path = Path(command[command.index("-ConfigPath") + 1])
        self.assertEqual(json.loads(snapshot_path.read_text())["agents"], self.legacy["agents"][:1])
        self.assertEqual(app.jobs[job_id]["status"], "completed")
        self.assertNotIn("shell", execute.call_args.kwargs)

    def test_posix_launch_opens_each_backend_in_tmux(self):
        app = Application(self.root)
        app.windows = False
        state = app.store.snapshot()
        fleet = state["fleets"][0]
        fleet["agents"][1]["backend"] = "claude-code"
        app.store.save(state)
        fleet = app.store.snapshot()["fleets"][0]
        config = app.store.launch_config(fleet, [a["id"] for a in fleet["agents"][:2]])
        job_id = uuid.uuid4().hex
        app.jobs[job_id] = {"id": job_id, "status": "running"}
        with patch("studio_agents.AgentBackend.ready_to_launch", return_value=True), patch("fleet_studio.tmux_launch", return_value=True) as launch:
            app._run_launch(job_id, fleet, config)
        commands = [call.args[2] for call in launch.call_args_list]
        self.assertEqual(commands[0][1:3], ["resume", fleet["agents"][0]["sessionId"]])
        self.assertEqual(commands[1][1:], ["--resume", fleet["agents"][1]["sessionId"]])
        self.assertEqual(app.jobs[job_id]["status"], "completed")

    def test_runtime_does_not_treat_stale_turn_as_live_process(self):
        sid = self.legacy["agents"][0]["sessionId"]
        with closing(sqlite3.connect(self.root / "state_5.sqlite")) as db:
            db.execute("CREATE TABLE threads(id,name,cwd,updated_at,source,archived)")
            db.execute("INSERT INTO threads VALUES(?,?,?,?,?,?)", (sid, "Writer", str(self.root), time.time(), "cli", 0))
            db.commit()
        with closing(sqlite3.connect(self.root / "thread_history_1.sqlite")) as db:
            db.execute("CREATE TABLE thread_turns(thread_id,rollout_ordinal,status,started_at,completed_at,turn_id)")
            db.execute("INSERT INTO thread_turns VALUES(?,?,?,?,?,?)", (sid, 1, "inProgress", 123, None, "turn-one"))
            db.commit()
        with patch("studio_runtime.writer_owners", return_value=[]):
            self.assertEqual(studio_runtime.observe(self.root, [sid])["sessions"][sid]["status"], "closed")
        with patch("studio_runtime.writer_owners", return_value=[456]):
            self.assertEqual(studio_runtime.observe(self.root, [sid])["sessions"][sid]["status"], "working")
        db = studio_runtime.connect(self.root, "state_5.sqlite")
        try:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("CREATE TABLE should_not_exist(value)")
        finally:
            db.close()

    def test_http_local_origin_token_and_revision_guards(self):
        app = Application(self.root, launch_enabled=False)
        server = Server(("127.0.0.1", 0), app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:" + str(server.server_port)
        def post(path, payload, headers=None):
            request = Request(base + path, data=json.dumps(payload).encode(),
                              headers={"Content-Type": "application/json", **(headers or {})})
            with urlopen(request) as response:
                return json.load(response)
        try:
            with urlopen(base + "/api/bootstrap") as response:
                bootstrap = json.load(response)
            with self.assertRaises(HTTPError) as denied:
                post("/api/state", bootstrap["state"])
            self.assertEqual(denied.exception.code, 403)
            with self.assertRaises(HTTPError) as denied:
                post("/api/state", bootstrap["state"], {"X-Fleet-Token": app.token, "Origin": "https://unrelated.example"})
            self.assertEqual(denied.exception.code, 403)
            saved = post("/api/state", bootstrap["state"], {"X-Fleet-Token": app.token})
            self.assertEqual(saved["state"]["revision"], 2)
            with self.assertRaises(HTTPError) as stale:
                post("/api/state", bootstrap["state"], {"X-Fleet-Token": app.token})
            self.assertEqual(stale.exception.code, 409)
            with self.assertRaises(HTTPError) as denied:
                urlopen(Request(base + "/api/bootstrap", headers={"Host": "unrelated.example"}))
            self.assertEqual(denied.exception.code, 403)
            with self.assertRaises(HTTPError) as missing:
                urlopen(base + "/fleet.json")
            self.assertEqual(missing.exception.code, 404)
            with self.assertRaises(HTTPError):
                post("/api/launch", {}, {"X-Fleet-Token": app.token})
            self.assertFalse(app.jobs)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
