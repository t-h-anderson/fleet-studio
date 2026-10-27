"""Role, policy and automation regressions. All native sends are mocked."""
from copy import deepcopy
from pathlib import Path
import sys
import json
import subprocess
import time
import types
import unittest
from unittest.mock import patch
import uuid
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_studio as fixtures
from fleet_studio import Application
from studio_roles import make_role, parse_roles, policy_proposal, refresh_roles
from studio_control import assignment_text, rename_session
POLICY = """# Roles and Responsibilities

| Role | Responsibilities | Authority and handoff |
|---|---|---|
| Writer | Implement assigned work. | Own assigned files. |
| Integrator | Deliver reviewed work. | Own delivery. |

## Unrelated
| Thing | Value | Comment |
|---|---|---|
| Keep | This | Unchanged |
"""

class DesignTests(unittest.TestCase):
    setUp = fixtures.StudioTests.setUp
    tearDown = fixtures.StudioTests.tearDown

    def app(self):
        app = Application(self.root)
        state = app.store.snapshot()
        fleet = state["fleets"][0]
        for agent, key in zip(fleet["agents"], ("writer", "integrator")):
            agent["roleIds"] = [next(r["id"] for r in fleet["roles"] if r["key"] == key)]
        app.store.save(state)
        return app, app.store.fleet(fleet["id"])

    def busy(self, app, fleet):
        """Give every agent open work: periodic triggers only reach agents with something to do."""
        for agent in fleet["agents"]:
            app.health.current[(fleet["id"], agent["id"])] = {"state": "idle", "since": 0, "signals": [], "openWork": True}

    def rule(self, fleet, kind="timer", mode="automatic"):
        return {"id": str(uuid.uuid4()), "name": "Deliver packet", "kind": kind,
                "sourceRoleId": fleet["agents"][0]["roleIds"][0], "targetRoleId": fleet["agents"][1]["roleIds"][0],
                "eventName": "packet-ready", "condition": "turn-completed", "minutes": 1,
                "message": "Check the assigned packet within authorized scope.", "enabled": True, "mode": mode}

    def save_rules(self, app, fleet, rules):
        state = app.store.snapshot()
        state["fleets"][0]["triggers"] = rules
        app.store.save(state)
        return app.store.fleet(fleet["id"])

    def monitor(self, fleet):
        now = time.time()
        return {"observedAt": now, "sessions": {a["sessionId"]: {
            "status": "idle", "pids": [100], "goalStatus": None, "goalKnown": True, "queueCount": 0,
            "turnId": "turn-1", "turnStatus": "completed", "observedAt": now} for a in fleet["agents"]}}

    def wait_controls(self, app):
        end = time.monotonic() + 3
        while time.monotonic() < end:
            if all(c["status"] != "sending" for c in app.controls.snapshot(full=True)):
                return
            time.sleep(.01)
        self.fail("Control dispatch did not complete")

    def policy_fleet(self):
        return {"roles": [make_role(row, i) for i, row in enumerate(parse_roles(POLICY))],
                "agents": [], "roleLinks": [], "triggers": [],
                "roleSource": {"repoDirectory": str(self.root), "commit": "a" * 40,
                               "path": "doc/ROLES_AND_RESPONSIBILITIES.md", "url": "https://example.invalid/policy"}}

    def test_policy_patch_preserves_other_sections(self):
        fleet = self.policy_fleet()
        with patch("studio_roles.git", return_value=POLICY):
            self.assertEqual(policy_proposal(fleet)["patch"], "")
            fleet["roles"][0]["responsibilities"] = "Implement and document assigned work."
            result = policy_proposal(fleet)
        self.assertEqual(result["baseCommit"], "a" * 40)
        self.assertIn("+| Writer | Implement and document", result["patch"])
        self.assertIn("| Keep | This | Unchanged |", result["document"])
        self.assertEqual(len(parse_roles(POLICY)), 2)

    def test_refresh_accepts_published_draft_and_prunes_removed_role(self):
        fleet = self.policy_fleet()
        writer, integrator = fleet["roles"]
        writer["responsibilities"] = "New agreed responsibility."
        fleet["agents"] = [{"roleIds": [integrator["id"]]}]
        fleet["triggers"] = [{"sourceRoleId": writer["id"], "targetRoleId": integrator["id"], "kind": "event"}]
        fresh = POLICY.replace("Implement assigned work.", "New agreed responsibility.").replace("| Integrator | Deliver reviewed work. | Own delivery. |\n", "")
        with patch("studio_roles.git", return_value=POLICY), patch("studio_roles.read_policy", return_value=(
            {**fleet["roleSource"], "commit": "b" * 40}, parse_roles(fresh), fresh)):
            refresh_roles(fleet)
        self.assertEqual(fleet["roles"][0]["id"], writer["id"])
        self.assertEqual(fleet["roles"][0]["baseline"]["responsibilities"], writer["responsibilities"])
        self.assertEqual(fleet["agents"][0]["roleIds"], [])
        self.assertEqual(fleet["triggers"], [])

    def test_assignment_combines_shared_and_individual_scope(self):
        app, fleet = self.app()
        fleet["agents"][0]["notes"] = "Only packet A; retain current custody."
        message = assignment_text(fleet, fleet["agents"][0])
        self.assertIn("Implement assigned work", message)
        self.assertIn("Only packet A", message)
        self.assertIn("## Reporting", message)
        self.assertNotIn("Acknowledge", message)

    def test_preview_rejects_stale_state_and_slash_commands(self):
        app, fleet = self.app()
        payload = {"fleetId": fleet["id"], "agentIds": [fleet["agents"][0]["id"]], "action": "message", "message": "Review packet A"}
        preview = app.controls.preview(payload)
        app.store.save(app.store.snapshot())
        with self.assertRaisesRegex(ValueError, "changed"):
            app.controls.send(preview["id"])
        with self.assertRaisesRegex(ValueError, "slash"):
            app.controls.preview({**payload, "message": "/exit"})

    def test_dispatch_is_exact_and_idempotent(self):
        app, fleet = self.app()
        preview = app.controls.preview({"fleetId": fleet["id"], "agentIds": [fleet["agents"][0]["id"]],
                                        "action": "message", "message": "Check A's literal value\nand $scope."})
        with patch("studio_control.subprocess.run", return_value=types.SimpleNamespace(returncode=0, stdout="", stderr="")) as run:
            app.controls.send(preview["id"]); self.wait_controls(app); app.controls.send(preview["id"])
        self.assertEqual(run.call_count, 1)
        args = run.call_args.args[0]
        self.assertEqual(args[1:4], ["queue", "--thread", fleet["agents"][0]["sessionId"]])
        self.assertEqual(args[-1], preview["targets"][0]["text"])
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertEqual(app.controls.snapshot()[0]["targets"][0]["status"], "queued")

    def test_timers_reserve_target_and_do_not_repeat(self):
        app, fleet = self.app()
        fleet = self.save_rules(app, fleet, [self.rule(fleet), self.rule(fleet)])
        self.busy(app, fleet)
        monitor = self.monitor(fleet); started = time.time()
        with patch("studio_control.subprocess.run", return_value=types.SimpleNamespace(returncode=0, stdout="", stderr="")) as run:
            app.triggers.tick(app.store.snapshot(), monitor, started)
            app.triggers.tick(app.store.snapshot(), monitor, started + 61)
            self.wait_controls(app)
            app.triggers.tick(app.store.snapshot(), monitor, started + 62)
        self.assertEqual(run.call_count, 1)
        statuses = [a["status"] for a in app.triggers.snapshot()["actions"]]
        self.assertIn("held", statuses); self.assertIn("queued", statuses)

    def test_stop_unknown_queue_and_closed_guards_hold(self):
        for change in ({"goalStatus": "paused"}, {"goalKnown": False}, {"queueCount": 1}, {"status": "closed", "pids": []}):
            app, fleet = self.app(); rule = self.rule(fleet)
            fleet = self.save_rules(app, fleet, [rule]); monitor = self.monitor(fleet)
            monitor["sessions"][fleet["agents"][1]["sessionId"]].update(change)
            with patch("studio_control.subprocess.run") as run:
                app.triggers.fire(fleet, rule, "timer", monitor)
            self.assertEqual(run.call_count, 0)
            self.assertEqual(app.triggers.snapshot()["actions"][0]["status"], "held")
            app.triggers.path.unlink(missing_ok=True)

    def test_disabled_rule_cannot_fire_from_stale_snapshot(self):
        app, fleet = self.app(); rule = self.rule(fleet)
        fleet = self.save_rules(app, fleet, [rule])
        self.save_rules(app, fleet, [{**rule, "enabled": False}])
        app.triggers.fire(fleet, rule, "stale timer", self.monitor(fleet))
        self.assertEqual(app.triggers.snapshot()["actions"], [])

    def test_condition_gap_does_not_replay_historical_completion(self):
        app, fleet = self.app()
        fleet = self.save_rules(app, fleet, [self.rule(fleet, "condition", "review")])
        monitor = self.monitor(fleet); source = monitor["sessions"][fleet["agents"][0]["sessionId"]]
        source.update(turnId=None, turnStatus=None, status="unknown")
        app.triggers.tick(app.store.snapshot(), monitor)
        source.update(turnId="old-turn", turnStatus="completed", status="idle")
        app.triggers.tick(app.store.snapshot(), monitor)
        self.assertEqual(app.triggers.snapshot()["actions"], [])
        source.update(turnId="new-turn", turnStatus="inProgress", status="working")
        app.triggers.tick(app.store.snapshot(), monitor)
        source.update(turnStatus="completed", status="idle")
        app.triggers.tick(app.store.snapshot(), monitor)
        self.assertEqual(len(app.triggers.snapshot()["actions"]), 1)

    def test_event_and_review_dispatch_are_deduplicated(self):
        app, fleet = self.app()
        fleet = self.save_rules(app, fleet, [self.rule(fleet, "event", "review")])
        payload = {"fleetId": fleet["id"], "agentId": fleet["agents"][0]["id"], "eventName": "packet-ready", "eventId": "packet-A-reviewed-head"}
        self.assertFalse(app.triggers.report(payload)["duplicate"])
        self.assertTrue(app.triggers.report(payload)["duplicate"])
        action = app.triggers.snapshot()["actions"][0]
        first = app.triggers.review(action["id"]); second = app.triggers.review(action["id"])
        self.assertEqual(first["id"], second["id"])
        with patch("studio_control.subprocess.run", return_value=types.SimpleNamespace(returncode=0, stdout="", stderr="")) as run:
            app.controls.send(first["id"]); self.wait_controls(app)
            app.triggers.tick(app.store.snapshot(), self.monitor(fleet))
            with self.assertRaises(ValueError): app.triggers.review(action["id"])
        self.assertEqual(run.call_count, 1)
        self.assertEqual(app.triggers.snapshot()["actions"][0]["status"], "queued")


    def test_dismissing_action_invalidates_unsent_preview(self):
        app, fleet = self.app()
        fleet = self.save_rules(app, fleet, [self.rule(fleet, "event", "review")])
        app.triggers.report({"fleetId": fleet["id"], "agentId": fleet["agents"][0]["id"],
                             "eventName": "packet-ready", "eventId": "dismiss-case"})
        action = app.triggers.snapshot()["actions"][0]
        preview = app.triggers.review(action["id"])
        app.triggers.dismiss(action["id"])
        with self.assertRaisesRegex(ValueError, "expired"):
            app.controls.send(preview["id"])


    def test_native_rename_protocol_without_resuming_thread(self):
        stub = self.root / "fake_app_server.py"
        receipt = self.root / "protocol.json"
        stub.write_text(
            "import sys,json\n"
            "messages=[]\n"
            "for line in sys.stdin:\n"
            "    m=json.loads(line); messages.append(m)\n"
            "    if 'id' in m: print(json.dumps({'id':m['id'],'result':{}}),flush=True)\n"
            "open(" + repr(str(receipt)) + ",'w').write(json.dumps(messages))\n",
            encoding="utf-8")
        actual_popen = subprocess.Popen
        with patch("studio_control.subprocess.Popen", side_effect=lambda args, **kwargs: actual_popen(
                [sys.executable, str(stub)], **kwargs)) as launch:
            rename_session("mock-codex", "thread-uuid", "Writer's named session")
        messages = json.loads(receipt.read_text())
        self.assertEqual([m["method"] for m in messages], ["initialize", "initialized", "thread/name/set"])
        self.assertEqual(messages[-1]["params"], {"threadId": "thread-uuid", "name": "Writer's named session"})
        self.assertEqual(launch.call_args.args[0], ["mock-codex", "app-server", "--stdio"])

if __name__ == "__main__":
    unittest.main(verbosity=2)