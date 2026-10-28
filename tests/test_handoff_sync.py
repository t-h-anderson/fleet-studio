"""A hand-off is one object: its routing trigger follows it on every save."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_design as fixtures
from fleet_studio import Store
from studio_workflow import contract_problem, sync_handoff_triggers
from workflow_spec import digest, export_spec


class HandoffSyncTests(unittest.TestCase):
    setUp = fixtures.DesignTests.setUp
    tearDown = fixtures.DesignTests.tearDown
    app = fixtures.DesignTests.app

    def link(self, fleet):
        """The first hand-off between two ordinary roles."""
        roles = {r['id']: r for r in fleet['roles']}
        from studio_workflow import internal
        return next(l for l in fleet['roleLinks'] if not internal(roles[l['source']]) and not internal(roles[l['target']]))

    def save(self, app, change):
        state = app.store.snapshot()
        change(state['fleets'][0])
        app.store.save(state)
        return app.store.fleet(state['fleets'][0]['id'])

    def trigger_for(self, fleet, link):
        return next((t for t in fleet['triggers'] if t.get('workflowLinkId') == link['id']), None)

    def test_switching_a_handoff_on_creates_its_trigger_and_off_disables_it(self):
        app, fleet = self.app()
        link = self.link(fleet)
        fleet = self.save(app, lambda f: next(l for l in f['roleLinks'] if l['id'] == link['id']).update(delivery='automatic'))
        trigger = self.trigger_for(fleet, link)
        self.assertEqual((trigger['enabled'], trigger['mode'], trigger['eventName']), (True, 'automatic', 'handoff-' + link['id'][:8]))
        fleet = self.save(app, lambda f: next(l for l in f['roleLinks'] if l['id'] == link['id']).update(delivery='review'))
        self.assertEqual(self.trigger_for(fleet, link)['mode'], 'review')
        before = digest(export_spec(fleet))
        fleet = self.save(app, lambda f: next(l for l in f['roleLinks'] if l['id'] == link['id']).update(delivery='off'))
        trigger = self.trigger_for(fleet, link)
        self.assertFalse(trigger['enabled'])
        # Off is a deploy-time change seen through the enabled flag, not a change to the portable spec.
        self.assertEqual(digest(export_spec(fleet)), before)

    def test_editing_a_handoff_refreshes_its_trigger_and_keeps_custom_instructions(self):
        app, fleet = self.app()
        link = self.link(fleet)
        fleet = self.save(app, lambda f: next(l for l in f['roleLinks'] if l['id'] == link['id']).update(delivery='automatic'))
        generated = self.trigger_for(fleet, link)
        fleet = self.save(app, lambda f: next(l for l in f['roleLinks'] if l['id'] == link['id']).update(label='Ship reviewed packet'))
        trigger = self.trigger_for(fleet, link)
        self.assertEqual((trigger['id'], trigger['eventName']), (generated['id'], generated['eventName']))
        self.assertEqual(trigger['name'], 'Ship reviewed packet')
        self.assertIn('Ship reviewed packet', trigger['message'])
        self.assertFalse(contract_problem(fleet, trigger))

        def custom(f):
            next(t for t in f['triggers'] if t.get('workflowLinkId') == link['id'])['message'] = 'Our own wording'
        fleet = self.save(app, custom)
        fleet = self.save(app, lambda f: next(l for l in f['roleLinks'] if l['id'] == link['id']).update(condition='Tests pass'))
        self.assertEqual(self.trigger_for(fleet, link)['message'], 'Our own wording')
        self.assertFalse(contract_problem(fleet, self.trigger_for(fleet, link)))

    def test_a_changed_handoff_no_longer_blocks_deploy(self):
        app, fleet = self.app()
        link = self.link(fleet)
        self.save(app, lambda f: next(l for l in f['roleLinks'] if l['id'] == link['id']).update(delivery='automatic'))
        self.save(app, lambda f: next(l for l in f['roleLinks'] if l['id'] == link['id']).update(artifact='Reviewed head'))
        preview = app.deployments.preview({'fleetId': fleet['id'], 'revision': app.store.snapshot()['revision'], 'kind': 'deploy'})
        self.assertTrue(app.deployments.apply(preview['id'])['release'])

    def test_removing_a_handoff_removes_its_trigger(self):
        app, fleet = self.app()
        link = self.link(fleet)
        self.save(app, lambda f: next(l for l in f['roleLinks'] if l['id'] == link['id']).update(delivery='automatic'))
        fleet = self.save(app, lambda f: f.update(roleLinks=[l for l in f['roleLinks'] if l['id'] != link['id']]))
        self.assertIsNone(self.trigger_for(fleet, link))

    def test_handoffs_to_reviewer_subagents_stay_off(self):
        app, fleet = self.app()
        from studio_workflow import internal
        roles = {r['id']: r for r in fleet['roles']}
        inner = next(l for l in fleet['roleLinks'] if internal(roles[l['source']]) or internal(roles[l['target']]))
        fleet = self.save(app, lambda f: next(l for l in f['roleLinks'] if l['id'] == inner['id']).update(delivery='automatic'))
        self.assertEqual(next(l for l in fleet['roleLinks'] if l['id'] == inner['id'])['delivery'], 'off')
        self.assertIsNone(self.trigger_for(fleet, inner))

    def test_sync_is_idempotent_and_leaves_ordinary_triggers_alone(self):
        app, fleet = self.app()
        link = self.link(fleet)
        fleet = self.save(app, lambda f: next(l for l in f['roleLinks'] if l['id'] == link['id']).update(delivery='review'))
        timer = {'id': str(uuid.uuid4()), 'name': 'Hourly check', 'kind': 'timer', 'sourceRoleId': '', 'targetRoleId': link['target'],
                 'eventName': 'packet-ready', 'condition': 'turn-completed', 'minutes': 60, 'message': 'Check open work.',
                 'enabled': True, 'mode': 'automatic', 'recipientMode': 'single'}
        triggers = fleet['triggers'] + [timer]
        once = sync_handoff_triggers(deepcopy(fleet['roles']), deepcopy(fleet['roleLinks']), deepcopy(triggers))
        twice = sync_handoff_triggers(deepcopy(fleet['roles']), deepcopy(fleet['roleLinks']), deepcopy(once))
        self.assertEqual(once, twice)
        self.assertIn(timer, once)

    def test_saved_fleets_migrate_without_changing_their_spec(self):
        app, fleet = self.app()
        before = digest(export_spec(fleet))
        saved = json.loads(app.store.path.read_text())
        for link in saved['fleets'][0]['roleLinks']:
            link.pop('delivery', None)
        app.store.path.write_text(json.dumps(saved))
        migrated = Store(app.root).snapshot()['fleets'][0]
        self.assertTrue(all(link['delivery'] in ('automatic', 'review', 'off') for link in migrated['roleLinks']))
        self.assertEqual(digest(export_spec(migrated)), before)
        self.assertEqual(migrated['triggers'], fleet['triggers'])


if __name__ == '__main__':
    unittest.main()
