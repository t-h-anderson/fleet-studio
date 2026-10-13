"""Portable workflows, release isolation, and explicit adoption contracts."""
from copy import deepcopy
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch
import test_studio as fixtures
from fleet_studio import Application, validate_fleets
from workflow_spec import apply_spec, export_spec, digest, render, validate, project_ticket, SCHEMA
from studio_workflow import populate_triggers


class SpecificationTests(unittest.TestCase):
    setUp = fixtures.StudioTests.setUp
    tearDown = fixtures.StudioTests.tearDown

    def app(self):
        app = Application(self.root, launch_enabled=False)
        state = app.store.snapshot()
        f = state["fleets"][0]
        for a in f["agents"]:
            a["roleIds"] = [f["roles"][1]["id"]]
        populate_triggers(f)
        state["fleets"][0] = apply_spec(f, export_spec(f))
        app.store.save(state)
        return app

    def deploy(self, app):
        state = app.store.snapshot()
        p = app.deployments.preview({"fleetId": state["fleets"][0]["id"], "revision": state["revision"], "kind": "deploy"})
        return app.deployments.apply(p["id"])

    def test_strict_validation_and_references(self):
        spec = export_spec(self.store.snapshot()["fleets"][0])
        for mutate in [lambda s:s.update(enabled=True), lambda s:s["roles"][0].update(id="../escape"),
                       lambda s:s["roles"].append(deepcopy(s["roles"][0])), lambda s:s["roles"][0].update(sources=["missing"]),
                       lambda s:s["handoffs"][0].update(target="missing"), lambda s:s.update(schemaVersion=True)]:
            broken = deepcopy(spec); mutate(broken)
            with self.assertRaises(ValueError): validate(broken)
        self.assertEqual(validate(spec), spec)

    def test_reporting_setup_preserves_routing_custody_and_explicit_automation_pause(self):
        app=self.app();self.deploy(app)
        before=app.store.snapshot();f=before['fleets'][0]
        app.deployments.pause({'fleetId':f['id'],'paused':True})
        p=app.deployments.preview({'fleetId':f['id'],'revision':before['revision'],'kind':'reporting'})
        self.assertEqual(p['changes']['policies']['added'],['progress-reporting'])
        for section in ('roles','handoffs','states','triggers'):
            self.assertFalse(p['changes'][section]['changed'])
        with patch.object(app.controls,'send') as send:
            result=app.deployments.apply(p['id']);send.assert_not_called()
        after=app.store.fleet(f['id'])
        self.assertEqual(after['agents'],f['agents']);self.assertEqual(after['triggers'],f['triggers'])
        self.assertTrue(app.deployments.data['paused'][f['id']])
        doc=Path(result['release']['directory'],'agents',f['agents'][0]['id']+'.md').read_text(encoding='utf-8')
        self.assertIn('## Progress reports',doc);self.assertIn('handoff-received',doc)
        with self.assertRaisesRegex(ValueError,'already deployed'):
            app.deployments.preview({'fleetId':f['id'],'revision':app.store.snapshot()['revision'],'kind':'reporting'})

    def test_roundtrip_rename_preserves_bindings_positions_and_baselines(self):
        app = self.app();f = app.store.snapshot()["fleets"][0];spec = export_spec(f)
        spec["roles"][1]["name"] = "Renamed role"
        next_fleet = apply_spec(f, spec)
        self.assertEqual(next_fleet["agents"], f["agents"])
        self.assertEqual([r["position"] for r in next_fleet["roles"]], [r["position"] for r in f["roles"]])
        self.assertEqual([r["id"] for r in next_fleet["roles"]], [r["id"] for r in f["roles"]])
        self.assertEqual(export_spec(validate_fleets([next_fleet])[0]), spec)
        encoded = json.dumps(spec)
        for a in f["agents"]:
            self.assertNotIn(a["sessionId"], encoded);self.assertNotIn(a["directory"], encoded)
        self.assertNotIn("baseline", encoded);self.assertNotIn('"enabled"', encoded)
        self.assertTrue(all(not t["enabled"] for t in next_fleet["triggers"]))

    def test_bound_role_removal_and_stale_preview_are_rejected(self):
        app = self.app();state = app.store.snapshot();f = state["fleets"][0];spec = export_spec(f)
        removed = spec["roles"][1]["id"]
        spec["roles"] = [r for r in spec["roles"] if r["id"] != removed]
        spec["handoffs"] = [];spec["triggers"] = []
        with self.assertRaisesRegex(ValueError,"assigned"):
            apply_spec(f,spec)
        p = app.deployments.preview({"fleetId":f["id"],"revision":state["revision"],"kind":"import","spec":export_spec(f)})
        app.store.save(state)
        with self.assertRaisesRegex(ValueError,"changed"):
            app.deployments.apply(p["id"])

    def test_draft_edits_do_not_change_active_definitions_or_targets(self):
        app = self.app();result = self.deploy(app);fid = result["state"]["fleets"][0]["id"]
        original = app.runtime_fleet(app.store.fleet(fid))
        state = app.store.snapshot();f = state["fleets"][0]
        f["roles"][1]["responsibilities"] = "New draft responsibilities"
        f["agents"][0]["roleIds"] = [f["roles"][0]["id"]]
        f["triggers"][0]["message"] = "New draft instruction"
        app.store.save(state)
        self.assertEqual(app.runtime_fleet(app.store.fleet(fid)),original)
        self.assertTrue(app.deployments.overview(app.store.fleet(fid))["draftChanged"])
        self.assertNotIn("New draft responsibilities", (Path(result["release"]["directory"])/"WORKFLOW.md").read_text(encoding="utf-8"))

    def test_session_rebinding_and_pause_stop_automatic_delivery(self):
        app=self.app();state=app.store.snapshot();state["fleets"][0]["triggers"][0]["enabled"]=True;app.store.save(state)
        result=self.deploy(app);fid=result["state"]["fleets"][0]["id"]
        self.assertTrue(app.runtime_fleet(app.store.fleet(fid))["triggers"][0]["enabled"])
        state=app.store.snapshot();state["fleets"][0]["agents"][0]["sessionId"]="";app.store.save(state)
        self.assertFalse(app.runtime_fleet(app.store.fleet(fid))["agents"][0]["enabled"])
        app.deployments.pause({"fleetId":fid,"paused":True})
        self.assertFalse(any(t["enabled"] for t in app.runtime_fleet(app.store.fleet(fid))["triggers"]))
        with self.assertRaisesRegex(ValueError,"bindings changed"):
            app.deployments.notification({"fleetId":fid,"agentIds":[state["fleets"][0]["agents"][0]["id"]]})

    def test_release_rebaseline_and_pending_action_invalidated(self):
        app=self.app();state=app.store.snapshot();f=state["fleets"][0]
        f["triggers"][0].update(enabled=True,kind="timer",mode="review",minutes=1)
        # A timer linked to a handoff keeps the same source role by design.
        app.store.save(state);first=self.deploy(app)
        app.triggers.tick(app.store.snapshot(),{},now=100)
        app.triggers.tick(app.store.snapshot(),{},now=161)
        action=app.triggers.snapshot()["actions"][0]
        second=self.deploy(app)
        app.triggers.tick(app.store.snapshot(),{},now=200)
        tid=app.store.snapshot()["fleets"][0]["triggers"][0]["id"]
        self.assertEqual(app.triggers.data["states"][tid]["nextDue"],260)
        with self.assertRaisesRegex(ValueError,"changed"):
            app.triggers.review(action["id"])
        self.assertNotEqual(first["release"]["id"],second["release"]["id"])

    def test_import_keeps_active_release_and_disables_new_draft(self):
        app=self.app();state=app.store.snapshot();f=state["fleets"][0];f["triggers"][0]["enabled"]=True;app.store.save(state)
        self.deploy(app);state=app.store.snapshot();f=state["fleets"][0];before=app.runtime_fleet(f)
        spec=export_spec(f);spec["roles"][0]["name"]="New name"
        p=app.deployments.preview({"fleetId":f["id"],"revision":state["revision"],"kind":"import","spec":spec})
        result=app.deployments.apply(p["id"])
        draft=result["state"]["fleets"][0]
        self.assertFalse(any(t["enabled"] for t in draft["triggers"]))
        self.assertEqual(app.runtime_fleet(draft),before)

    def test_adoption_is_explicit_idempotent_and_versioned(self):
        app=self.app();result=self.deploy(app);f=result["state"]["fleets"][0];rid=result["release"]["id"]
        self.assertEqual(app.controls.snapshot(),[])
        preview=app.deployments.notification({"fleetId":f["id"]})
        self.assertEqual(preview["deploymentId"],rid)
        self.assertIn("workflow_client.py",preview["targets"][0]["text"])
        for target in preview["targets"]:
            self.assertIn("--agent "+target["agentId"],target["text"])
            self.assertIn(target["agentId"]+".md",target["text"])
            self.assertNotIn("<your-agent-id>",target["text"])
        self.assertEqual(preview,app.controls.existing_preview(preview["id"]))
        self.assertEqual(app.deployments.overview(f)["acks"],[])
        args={"fleetId":f["id"],"agentId":f["agents"][0]["id"],"releaseId":rid}
        app.deployments.acknowledge(args);app.deployments.acknowledge(args)
        self.assertEqual(len(app.deployments.overview(f)["acks"]),1)
        self.deploy(app)
        with self.assertRaisesRegex(ValueError,"no longer active"):
            app.deployments.acknowledge(args)

    def test_blocking_gap_and_invalid_contract_prevent_deploy(self):
        app=self.app();state=app.store.snapshot();f=state["fleets"][0]
        f["workflow"]["gaps"]=[dict(id="missing",summary="Unresolved acceptance",roles=[],blocking=True,resolution="open",sources=[])]
        app.store.save(state)
        with self.assertRaisesRegex(ValueError,"Unresolved acceptance"):self.deploy(app)

    def test_sample_mapping_and_generated_document_drift(self):
        root=Path(__file__).resolve().parents[1]/"workflows"
        spec=validate(json.loads((root/"sample.workflow.json").read_text(encoding="utf-8")))
        self.assertEqual((root/"SAMPLE_WORKFLOW.md").read_text(encoding="utf-8"),render(spec))
        self.assertEqual(json.loads((root/"workflow.schema.json").read_text(encoding="utf-8")),SCHEMA)
        self.assertTrue(any(h['source']=='writer' and h['target']=='integrator' for h in spec['handoffs']))
        self.assertTrue(any(p['id']=='progress-reporting' for p in spec['policies']))
        self.assertEqual(project_ticket({'state':'closed','labels':['AI Claimed']},spec)['id'],'closed')
        renamed=deepcopy(spec);renamed['roles'][0]['name']='Renamed owner'
        self.assertNotEqual(digest(spec),digest(renamed))

    def test_contract_staleness_survives_export_import(self):
        from studio_workflow import contract_problem
        app=self.app();f=app.store.snapshot()["fleets"][0]
        t=next(t for t in f["triggers"] if t.get("workflowLinkId"))
        h=next(h for h in f["roleLinks"] if h["id"]==t["workflowLinkId"])
        h["condition"]="Changed acceptance guard"
        spec=export_spec(f)
        imported=apply_spec(f,spec)
        self.assertTrue(contract_problem(imported, imported["triggers"][0]))
        self.assertEqual(export_spec(imported),spec)

    def test_overlapping_notification_previews_reserve_each_recipient_once(self):
        app=self.app();result=self.deploy(app);f=result["state"]["fleets"][0]
        first=app.deployments.notification({"fleetId":f["id"]})
        second=app.deployments.notification({"fleetId":f["id"],"agentIds":[f["agents"][0]["id"]]})
        app.launch_enabled=True
        with patch("studio_control.threading.Thread"):
            app.controls.send(first["id"])
            with self.assertRaisesRegex(ValueError,"already notified"):
                app.controls.send(second["id"])
        self.assertEqual(len(app.controls.snapshot()),1)

    def test_reviewed_entrypoints_and_offline_instructions(self):
        from workflow_client import instructions
        app=self.app();result=self.deploy(app);f=result["state"]["fleets"][0]
        aid=f["agents"][0]["id"];entry=self.root/"AGENTS.override.md"
        original=b"# Existing local instructions\r\n"
        entry.write_bytes(original)
        p=app.entrypoints.preview({"fleetId":f["id"],"revision":result["state"]["revision"]})
        self.assertEqual(entry.read_bytes(),original)
        entry.write_text("Changed outside Studio",encoding="utf-8")
        app.launch_enabled=True
        with self.assertRaisesRegex(ValueError,"changed since preview"):
            app.entrypoints.apply(p["id"])
        entry.write_bytes(original)
        installed=app.entrypoints.apply(p["id"])
        self.assertEqual(installed["installed"],[str(entry)])
        self.assertIn("instructions --fleet",entry.read_text(encoding="utf-8"))
        self.assertIn("Writer A",instructions(f["id"],aid,self.root,self.root))
        self.assertIn("fleet_event.py", instructions(f["id"],aid,self.root,self.root))
        self.assertIn("stable event ID", instructions(f["id"],aid,self.root,self.root))
        with self.assertRaisesRegex(ValueError,"no longer matches"):
            instructions(f["id"],aid,self.root,self.root.parent)
        state=app.store.snapshot();state["fleets"][0]["roles"][1]["responsibilities"]="Redeployed role";app.store.save(state);self.deploy(app)
        self.assertIn("Redeployed role",instructions(f["id"],aid,self.root,self.root))
        self.assertEqual(entry.read_text(encoding="utf-8"),p["changes"][0]["after"])

    def test_shared_folder_migration_tracks_each_agent_and_changed_entrypoint(self):
        from workflow_client import instructions
        app=self.app();result=self.deploy(app);f=result["state"]["fleets"][0]
        before=app.deployments.overview(f)
        self.assertFalse(before["migrationComplete"])
        self.assertTrue(all(a["entryPoint"]=="not-installed" for a in before["adoption"]))
        with self.assertRaisesRegex(ValueError,"all agents sharing"):
            app.entrypoints.preview({"fleetId":f["id"],"revision":result["state"]["revision"],"agentIds":[f["agents"][0]["id"]]})
        preview=app.entrypoints.preview({"fleetId":f["id"],"revision":result["state"]["revision"]})
        self.assertEqual(len(preview["changes"]),1)
        self.assertEqual(preview["changes"][0]["agentIds"],[a["id"] for a in f["agents"]])
        for a in f["agents"]:
            self.assertIn(a["name"],preview["changes"][0]["after"])
            self.assertIn("--agent "+a["id"],preview["changes"][0]["after"])
        self.assertIn("--directory (Get-Location).Path",preview["changes"][0]["after"])
        self.assertIn("different or nested checkout",preview["changes"][0]["after"])
        app.launch_enabled=True
        installed=app.entrypoints.apply(preview["id"])
        f=installed["state"]["fleets"][0]
        self.assertTrue(all(a["entryPoint"]=="installed" for a in app.deployments.overview(f)["adoption"]))
        child=self.root/"child";child.mkdir()
        with self.assertRaisesRegex(ValueError,"no longer matches"):
            instructions(f["id"],f["agents"][0]["id"],self.root,child)
        for a in f["agents"]:
            self.assertIn("# Assignment: "+a["name"],instructions(f["id"],a["id"],self.root,self.root))
            app.deployments.acknowledge({"fleetId":f["id"],"agentId":a["id"],"releaseId":result["release"]["id"]})
        self.assertTrue(app.deployments.overview(f)["migrationComplete"])
        entry=self.root/"AGENTS.override.md"
        entry.write_text("Changed outside Studio",encoding="utf-8")
        changed=app.deployments.overview(f)
        self.assertFalse(changed["migrationComplete"])
        self.assertTrue(all(a["entryPoint"]=="changed" for a in changed["adoption"]))
        entry.unlink()
        self.assertTrue(all(a["entryPoint"]=="missing" for a in app.deployments.overview(f)["adoption"]))

    def test_entrypoints_reject_rebound_session_and_adoption_requires_current_bindings(self):
        app=self.app();result=self.deploy(app);state=app.store.snapshot();f=state["fleets"][0]
        first=f["agents"][0]
        app.deployments.acknowledge({"fleetId":f["id"],"agentId":first["id"],"releaseId":result["release"]["id"]})
        first["sessionId"]=""
        state=app.store.save(state);f=state["fleets"][0]
        with self.assertRaisesRegex(ValueError,"bindings changed"):
            app.entrypoints.preview({"fleetId":f["id"],"revision":state["revision"]})
        row=app.deployments.overview(f)["adoption"][0]
        self.assertFalse(row["bindingMatches"])
        self.assertFalse(row["acknowledged"])
