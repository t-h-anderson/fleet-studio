"""Delivery, restart, recurrence and routing regressions; native sends are mocked."""
from copy import deepcopy
import json
import time
import types
import unittest
from unittest.mock import patch
import uuid
import test_design as fixtures
from studio_triggers import Triggers
from workflow_spec import apply_spec, export_spec
from studio_workflow import signature


class TriggerTests(unittest.TestCase):
    setUp = fixtures.DesignTests.setUp
    tearDown = fixtures.DesignTests.tearDown
    app = fixtures.DesignTests.app
    rule = fixtures.DesignTests.rule
    save_rules = fixtures.DesignTests.save_rules
    monitor = fixtures.DesignTests.monitor
    wait_controls = fixtures.DesignTests.wait_controls
    busy = fixtures.DesignTests.busy

    def test_deployed_handoff_sends_without_adoption_acknowledgement(self):
        # Adoption acknowledgements no longer gate automation; agents read their brief when it changes.
        app, f = self.app()
        f = self.save_rules(app, f, [self.rule(f, 'event')])
        preview = app.deployments.preview({'fleetId': f['id'], 'revision': app.store.snapshot()['revision'], 'kind': 'deploy'})
        app.deployments.apply(preview['id'])
        monitor = self.monitor(f)
        app.monitor.update(monitor)
        with patch.object(app.controls, 'send') as send:
            app.triggers.report(self.payload(f))
            send.assert_called_once()

    def payload(self, fleet, event_id="ticket-one-head"):
        return {"fleetId": fleet["id"], "agentId": fleet["agents"][0]["id"],
                "eventName": "packet-ready", "eventId": event_id,
                "ticket": "https://gitlab.example/project/issues/1#note_2",
                "artifact": "reviewed commit abc123; evidence in ticket comment",
                "context": "Inspect this exact packet; retain the existing owner."}

    def refreshed(self, monitor):
        monitor["observedAt"] = time.time()
        for item in monitor["sessions"].values():
            item["observedAt"] = monitor["observedAt"]

    def test_directed_handoffs_reject_ambiguity_and_forward_context(self):
        app, f = self.app()
        state = app.store.snapshot()
        extra = deepcopy(f["agents"][1])
        extra.update(id=str(uuid.uuid4()), sessionId=str(uuid.uuid4()), name="Other recipient")
        state["fleets"][0]["agents"].append(extra)
        app.store.save(state)
        f = self.save_rules(app, f, [self.rule(f, "event", "review")])
        p = self.payload(f)
        with self.assertRaisesRegex(ValueError, "named recipient"):
            app.triggers.report(p)
        with self.assertRaisesRegex(ValueError, "target role"):
            app.triggers.report({**p, "targetAgentId": f["agents"][0]["id"]})
        self.assertEqual(app.triggers.data["events"], [])
        p["targetAgentId"] = extra["id"]
        action = app.triggers.report(p)["actions"][0]
        preview = app.triggers.review(action["id"])
        self.assertEqual([t["agentId"] for t in preview["targets"]], [extra["id"]])
        for key in ("ticket", "artifact", "context"):
            self.assertIn(p[key], preview["targets"][0]["text"])
        with self.assertRaisesRegex(ValueError, "different"):
            app.triggers.report({**p, "context": "different work"})

    def test_two_held_packets_deliver_independently_after_restart(self):
        app, f = self.app()
        f = self.save_rules(app, f, [self.rule(f, "event")])
        monitor = self.monitor(f)
        monitor["sessions"][f["agents"][1]["sessionId"]]["queueCount"] = 1
        with patch.object(app, "status", return_value=monitor):
            app.triggers.report(self.payload(f, "one"))
            app.triggers.report(self.payload(f, "two"))
        self.assertEqual([a["status"] for a in app.triggers.data["actions"]], ["held", "held"])
        app.triggers = Triggers(app)
        monitor["sessions"][f["agents"][1]["sessionId"]]["queueCount"] = 0
        with patch("studio_control.subprocess.run", return_value=types.SimpleNamespace(returncode=0, stdout="", stderr="")) as run:
            app.triggers.tick(app.store.snapshot(), monitor)
            self.wait_controls(app)
            self.assertEqual(run.call_count, 1)
            self.refreshed(monitor)
            app.triggers.tick(app.store.snapshot(), monitor)
            self.wait_controls(app)
            self.refreshed(monitor)
            app.triggers.tick(app.store.snapshot(), monitor)
            self.assertEqual(run.call_count, 2)
        self.assertEqual([a["status"] for a in app.triggers.data["actions"]], ["queued", "queued"])

    def test_recurring_idle_waits_repeats_and_keeps_cooldown_on_restart(self):
        app, f = self.app()
        rule = self.rule(f, "idle")
        f = self.save_rules(app, f, [rule])
        monitor = self.monitor(f)
        self.busy(app, app.store.fleet(f["id"]))
        target = monitor["sessions"][f["agents"][1]["sessionId"]]
        start = time.time()
        with patch("studio_control.subprocess.run", return_value=types.SimpleNamespace(returncode=0, stdout="", stderr="")) as run:
            target["status"] = "working"
            app.triggers.tick(app.store.snapshot(), monitor, start)
            target["status"] = "idle"
            app.triggers.tick(app.store.snapshot(), monitor, start + 5)
            self.assertEqual(run.call_count, 0)
            app.triggers.tick(app.store.snapshot(), monitor, start + 21)
            self.wait_controls(app)
            self.assertEqual(run.call_count, 1)
            app.triggers = Triggers(app)
            self.refreshed(monitor)
            app.triggers.tick(app.store.snapshot(), monitor, start + 30)
            app.triggers.tick(app.store.snapshot(), monitor, start + 79)
            self.assertEqual(run.call_count, 1)
            app.triggers.tick(app.store.snapshot(), monitor, start + 82)
            self.wait_controls(app)
            self.assertEqual(run.call_count, 2)
            self.refreshed(monitor)
            target.update(goalStatus="paused")
            app.triggers.tick(app.store.snapshot(), monitor, start + 200)
            self.assertEqual(run.call_count, 2)

    def test_idle_checks_each_target_without_waiting_for_busy_peers(self):
        app, f = self.app()
        rule = self.rule(f, "idle", "review")
        state = app.store.snapshot()
        state["fleets"][0]["agents"][0]["roleIds"] = [rule["targetRoleId"]]
        app.store.save(state)
        f = self.save_rules(app, f, [rule])
        monitor = self.monitor(f)
        self.busy(app, app.store.fleet(f["id"]))
        monitor["sessions"][f["agents"][0]["sessionId"]]["status"] = "working"
        start = time.time()
        app.triggers.tick(app.store.snapshot(), monitor, start)
        app.triggers.tick(app.store.snapshot(), monitor, start + 16)
        actions = app.triggers.data["actions"]
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["targetAgentId"], f["agents"][1]["id"])

    def test_dedupe_and_unresolved_actions_outlive_display_history(self):
        app, f = self.app()
        f = self.save_rules(app, f, [self.rule(f, "event", "review")])
        with patch.object(app.triggers, "persist"):
            for i in range(505):
                app.triggers.report(self.payload(f, str(i)))
        app.triggers.persist()
        app.triggers = Triggers(app)
        app.triggers.tick(app.store.snapshot(), self.monitor(f))
        self.assertEqual(len(app.triggers.data["events"]), 500)
        self.assertEqual(len(app.triggers.data["actions"]), 505)
        self.assertTrue(app.triggers.report(self.payload(f, "0"))["duplicate"])
        self.assertEqual(len(app.triggers.snapshot()["actions"]), 505)

    def test_event_identity_includes_event_name_and_survives_failed_persist(self):
        app, f = self.app()
        one = self.rule(f, "event", "review")
        two = {**one, "id": str(uuid.uuid4()), "eventName": "accepted"}
        f = self.save_rules(app, f, [one, two])
        p = self.payload(f)
        with patch.object(app.triggers, "persist", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                app.triggers.report(p)
        self.assertEqual(app.triggers.data["actions"], [])
        self.assertFalse(app.triggers.report(p)["duplicate"])
        self.assertFalse(app.triggers.report({**p, "eventName": "accepted"})["duplicate"])
        self.assertEqual(len(app.triggers.data["actions"]), 2)

    def test_uncertain_dispatch_is_never_retried_or_woken_by_idle(self):
        app, f = self.app()
        event = self.rule(f, "event")
        idle = self.rule(f, "idle")
        f = self.save_rules(app, f, [event, idle])
        monitor = self.monitor(f)
        with patch.object(app, "status", return_value=monitor), patch("studio_control.subprocess.run", side_effect=TimeoutError("uncertain")) as run:
            app.triggers.report(self.payload(f))
            self.wait_controls(app)
            app.triggers = Triggers(app)
            self.refreshed(monitor)
            start = time.time()
            app.triggers.tick(app.store.snapshot(), monitor, start)
            app.triggers.tick(app.store.snapshot(), monitor, start + 600)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(app.triggers.data["actions"][0]["status"], "needs-attention")

    def test_held_delivery_does_not_follow_a_rebound_recipient(self):
        app, f = self.app()
        f = self.save_rules(app, f, [self.rule(f, "event")])
        app.triggers.report(self.payload(f))  # No live observations, held.
        state = app.store.snapshot()
        state["fleets"][0]["agents"][1]["sessionId"] = str(uuid.uuid4())
        app.store.save(state)
        with patch("studio_control.subprocess.run") as run:
            app.triggers.tick(app.store.snapshot(), self.monitor(state["fleets"][0]))
            run.assert_not_called()
        self.assertEqual(app.triggers.data["actions"][0]["status"], "needs-attention")
        with self.assertRaisesRegex(ValueError, "rebound"):
            app.triggers.review(app.triggers.data["actions"][0]["id"])

    def test_crash_after_native_send_cannot_lose_idle_cooldown(self):
        app, f = self.app()
        rule = {**self.rule(f, "idle"), "minutes": 10}
        f = self.save_rules(app, f, [rule])
        monitor = self.monitor(f)
        self.busy(app, app.store.fleet(f["id"]))
        started = time.time()
        original_send = app.controls.send
        def crash(preview_id):
            original_send(preview_id)
            raise SystemExit("Simulated abrupt process exit after queue acceptance")
        with patch("studio_control.subprocess.run", return_value=types.SimpleNamespace(returncode=0, stdout="", stderr="")) as run:
            app.triggers.tick(app.store.snapshot(), monitor, started)
            with patch.object(app.controls, "send", side_effect=crash):
                with self.assertRaises(SystemExit):
                    app.triggers.tick(app.store.snapshot(), monitor, started + 16)
            self.wait_controls(app)
            app.triggers = Triggers(app)
            self.refreshed(monitor)
            app.triggers.tick(app.store.snapshot(), monitor, started + 20)
            app.triggers.tick(app.store.snapshot(), monitor, started + 36)
            self.wait_controls(app)
            self.assertEqual(run.call_count, 1)

    def test_formal_handoffs_require_ticket_artifact_and_context(self):
        app, f = self.app()
        rule = self.rule(f, "event", "review")
        link = {"id": str(uuid.uuid4()), "source": rule["sourceRoleId"], "target": rule["targetRoleId"],
                "label": "Send packet", "trigger": "Evidence ready", "artifact": "Reviewed head", "condition": "Scope recorded",
                "delivery": "review"}
        state = app.store.snapshot()
        state["fleets"][0]["roleLinks"].append(link)
        app.store.save(state)
        rule.update(workflowLinkId=link["id"], workflowSignature=signature(link))
        f = self.save_rules(app, f, [rule])
        p = self.payload(f)
        for key in ("ticket", "artifact", "context"):
            with self.assertRaisesRegex(ValueError, "Formal handoffs require"):
                app.triggers.report({**p, key: ""})
        self.assertEqual(app.triggers.data["events"], [])
        self.assertFalse(app.triggers.report(p)["duplicate"])

    def test_final_availability_check_defers_before_native_effect_and_retries(self):
        app, f = self.app()
        f = self.save_rules(app, f, [self.rule(f, "event")])
        state = app.store.snapshot()
        preview = app.deployments.preview({"fleetId": f["id"], "revision": state["revision"], "kind": "deploy"})
        app.deployments.apply(preview["id"])
        for agent in f['agents']:
            app.deployments.acknowledge({'fleetId':f['id'],'agentId':agent['id'],'releaseId':app.deployments.data['active'][f['id']]})
        monitor = self.monitor(f)
        app.monitor.update(monitor)
        original_dispatch = app.controls._dispatch
        delayed = []
        # Capture a reserved dispatch, then change the observed queue before
        # the actual worker gets to submit it to Codex.
        with patch.object(app.controls, "_dispatch", side_effect=lambda p: delayed.append(p)):
            app.triggers.report(self.payload(f))
            deadline = time.monotonic() + 3
            while not delayed and time.monotonic() < deadline:
                time.sleep(.01)
        self.assertEqual(len(delayed), 1)
        app.monitor["sessions"][f["agents"][1]["sessionId"]]["queueCount"] = 1
        with patch("studio_control.subprocess.run", return_value=types.SimpleNamespace(returncode=0, stdout="", stderr="")) as run:
            original_dispatch(delayed[0])
            run.assert_not_called()
            self.assertEqual(app.controls.snapshot()[0]["targets"][0]["status"], "held")
            app.triggers.tick(app.store.snapshot(), monitor)
            self.assertEqual(app.triggers.data["actions"][0]["status"], "held")
            app.monitor["sessions"][f["agents"][1]["sessionId"]]["queueCount"] = 0
            self.refreshed(monitor)
            app.triggers.tick(app.store.snapshot(), monitor)
            self.wait_controls(app)
            self.assertEqual(run.call_count, 1)

    def test_pause_after_reservation_holds_until_same_release_resumes(self):
        app, f = self.app()
        f = self.save_rules(app, f, [self.rule(f, "event")])
        state = app.store.snapshot()
        preview = app.deployments.preview({"fleetId": f["id"], "revision": state["revision"], "kind": "deploy"})
        app.deployments.apply(preview["id"])
        for agent in f['agents']:
            app.deployments.acknowledge({'fleetId':f['id'],'agentId':agent['id'],'releaseId':app.deployments.data['active'][f['id']]})
        monitor = self.monitor(f)
        app.monitor.update(monitor)
        delayed = []
        with patch.object(app.controls, "_dispatch", side_effect=lambda p: delayed.append(p)):
            app.triggers.report(self.payload(f))
            deadline = time.monotonic() + 3
            while not delayed and time.monotonic() < deadline:
                time.sleep(.01)
        self.assertEqual(len(delayed), 1)
        app.deployments.pause({"fleetId": f["id"], "paused": True})
        with patch("studio_control.subprocess.run", return_value=types.SimpleNamespace(returncode=0, stdout="", stderr="")) as run:
            app.controls._dispatch(delayed[0])
            run.assert_not_called()
            app.triggers.tick(app.store.snapshot(), monitor)
            self.assertEqual(app.triggers.data["actions"][0]["status"], "held")
            app.deployments.pause({"fleetId": f["id"], "paused": False})
            self.refreshed(monitor)
            app.triggers.tick(app.store.snapshot(), monitor)
            self.wait_controls(app)
            self.assertEqual(run.call_count, 1)

    def test_new_options_roundtrip_without_changing_legacy_specs(self):
        app, f = self.app()
        f = self.save_rules(app, f, [self.rule(f, "idle"), {**self.rule(f, "event"), "recipientMode": "single"}])
        spec = export_spec(f)
        self.assertEqual(export_spec(apply_spec(f, spec)), spec)
        legacy = json.loads((app.root.parent.parent / "workflows/sample.workflow.json").read_text(encoding="utf-8"))
        self.assertTrue(legacy["triggers"])
