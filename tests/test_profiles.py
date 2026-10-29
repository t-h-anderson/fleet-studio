"""Profiles: save a fleet's whole setup to a file, and retool a fleet from one in a single reviewed deploy."""
import json
from pathlib import Path
import sys
import time
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_design as fixtures
from studio_profiles import SCHEMA, previous_slug
from workflow_spec import digest, export_spec

SAMPLE = json.loads((Path(__file__).resolve().parents[1] / 'workflows' / 'sample.workflow.json').read_text(encoding='utf-8'))


class ProfileTests(unittest.TestCase):
    setUp = fixtures.DesignTests.setUp
    tearDown = fixtures.DesignTests.tearDown
    app = fixtures.DesignTests.app

    def revision(self, app):
        return app.store.snapshot()['revision']

    def save(self, app, fleet, name, **extra):
        return app.profiles.save({'fleetId': fleet['id'], 'revision': self.revision(app), 'name': name, **extra})

    def switch(self, app, fleet, slug):
        preview = app.deployments.preview({'fleetId': fleet['id'], 'revision': self.revision(app), 'kind': 'profile', 'profile': slug})
        result = app.deployments.apply(preview['id'])
        return preview, result, app.store.fleet(fleet['id'])

    def edit(self, app, change):
        state = app.store.snapshot()
        change(state['fleets'][0])
        app.store.save(state)
        return app.store.fleet(state['fleets'][0]['id'])

    def sample_profile(self, app):
        """A profile with its own roles, written as a person would copy one into the folder."""
        profile = {'schema': SCHEMA, 'name': 'Sample delivery', 'spec': SAMPLE,
                   'assignments': {'Writer A': ['integrator'], 'Writer B': ['writer']},
                   'delivery': {'submit-reviewed-packet': 'automatic'},
                   'automations': [{'id': str(uuid.uuid4()), 'name': 'Nudge writers', 'enabled': True, 'roleId': 'writer',
                                    'when': {'signal': 'stalled', 'forMinutes': 0}, 'action': 'reprompt'}],
                   'throttles': {'roleTokensPerHour': {'writer': 50000}}}
        app.profiles.write('sample-delivery', profile)
        return profile

    def test_saving_lists_and_deletes_profiles_without_machine_details(self):
        app, fleet = self.app()
        saved = self.save(app, fleet, 'Day shift')
        self.assertEqual(saved['slug'], 'day-shift')
        text = (app.root / 'profiles' / 'day-shift.profile.json').read_text(encoding='utf-8')
        for agent in fleet['agents']:
            self.assertNotIn(agent['sessionId'], text)
            self.assertNotIn(agent['directory'], text)
        for role in fleet['roles']:
            self.assertNotIn(role['id'], text)
        self.assertEqual([p['name'] for p in app.profiles.list(fleet)['profiles']], ['Day shift'])
        with self.assertRaisesRegex(ValueError, 'exists'):
            self.save(app, fleet, 'Day  Shift!')
        app.profiles.delete({'slug': 'day-shift'})
        self.assertEqual(app.profiles.list(fleet)['profiles'], [])

    def test_bad_names_and_paths_are_refused(self):
        app, fleet = self.app()
        for slug in ('../fleet-studio', 'Day', '', None, 'a/b'):
            with self.assertRaises(ValueError):
                app.profiles.read(slug)
        with self.assertRaisesRegex(ValueError, 'letters or numbers'):
            self.save(app, fleet, '***')
        with self.assertRaisesRegex(ValueError, 'kept for'):
            self.save(app, fleet, 'Before switch ab')

    def test_switching_to_a_profile_with_other_roles_retools_and_deploys(self):
        app, fleet = self.app()
        self.sample_profile(app)
        # A plain import is refused while agents hold roles the new workflow drops; a profile carries the new assignments.
        with self.assertRaisesRegex(ValueError, 'Reassign'):
            app.deployments.preview({'fleetId': fleet['id'], 'revision': self.revision(app), 'kind': 'import',
                                     'spec': {**SAMPLE, 'roles': SAMPLE['roles'][:2], 'handoffs': SAMPLE['handoffs'][:1],
                                              'triggers': SAMPLE['triggers'][:1], 'states': [], 'transitions': []}})
        preview, result, after = self.switch(app, fleet, 'sample-delivery')
        self.assertEqual(preview['profile']['name'], 'Sample delivery')
        roles = {r['specId']: r for r in after['roles']}
        agents = {a['name']: a for a in after['agents']}
        self.assertEqual(agents['Writer A']['roleIds'], [roles['integrator']['id']])
        self.assertEqual(agents['Writer B']['roleIds'], [roles['writer']['id']])
        link = next(l for l in after['roleLinks'] if l['specId'] == 'submit-reviewed-packet')
        self.assertEqual(link['delivery'], 'automatic')
        trigger = next(t for t in after['triggers'] if t.get('workflowLinkId') == link['id'])
        self.assertTrue(trigger['enabled'])
        self.assertEqual([r['roleId'] for r in after['automations']], [roles['writer']['id']])
        self.assertEqual(after['throttles']['roleTokensPerHour'], {roles['writer']['id']: 50000})
        # The switch is a release: the brief names the new role.
        brief = Path(result['release']['directory'], 'agents', agents['Writer A']['id'] + '.md').read_text(encoding='utf-8')
        self.assertIn('Integrator', brief)
        self.assertEqual(app.deployments.data['active'][fleet['id']], result['release']['id'])

    def test_what_cannot_be_matched_is_listed(self):
        app, fleet = self.app()
        profile = self.sample_profile(app)
        profile['assignments'] = {'Writer A': ['writer'], 'Ghost': ['integrator']}
        profile['automations'].append({'id': str(uuid.uuid4()), 'name': 'Watch ghost', 'enabled': True, 'roleId': '',
                                       'agentName': 'Ghost', 'when': {'signal': 'stalled', 'forMinutes': 0}, 'action': 'reprompt'})
        profile['automations'].append({'id': str(uuid.uuid4()), 'name': 'Old role rule', 'enabled': True, 'roleId': 'retired-role',
                                       'when': {'signal': 'stalled', 'forMinutes': 0}, 'action': 'reprompt'})
        app.profiles.write('sample-delivery', profile)
        preview = app.deployments.preview({'fleetId': fleet['id'], 'revision': self.revision(app), 'kind': 'profile', 'profile': 'sample-delivery'})
        remap = ' | '.join(preview['profile']['remap'])
        self.assertIn('Writer B has no role', remap)
        self.assertIn('Ghost is in the profile but not in this fleet', remap)
        self.assertIn('"Watch ghost" dropped', remap)
        self.assertIn('"Old role rule" dropped', remap)

    def test_the_previous_setup_is_saved_and_switching_back_restores_it(self):
        app, fleet = self.app()
        fleet = self.edit(app, lambda f: f['roleLinks'][3].update(delivery='review'))
        before = digest(export_spec(fleet))
        names = lambda f: {a['name']: [next(r['name'] for r in f['roles'] if r['id'] == rid) for rid in a['roleIds']] for a in f['agents']}
        assignments = names(fleet)
        self.sample_profile(app)
        self.switch(app, fleet, 'sample-delivery')
        listed = app.profiles.list(fleet)['profiles']
        self.assertEqual(listed[0]['slug'], previous_slug(fleet['id']))
        self.assertTrue(listed[0]['automatic'])
        self.assertIn('Before switching to Sample delivery', listed[0]['name'])
        self.assertTrue(next(p for p in listed if p['slug'] == 'sample-delivery')['active'])
        _, _, back = self.switch(app, fleet, previous_slug(fleet['id']))
        self.assertEqual(digest(export_spec(back)), before)
        self.assertEqual(names(back), assignments)
        self.assertEqual(back['roleLinks'][3]['delivery'], 'review')

    def test_a_profile_or_draft_changed_after_review_is_refused(self):
        app, fleet = self.app()
        profile = self.sample_profile(app)
        preview = app.deployments.preview({'fleetId': fleet['id'], 'revision': self.revision(app), 'kind': 'profile', 'profile': 'sample-delivery'})
        app.profiles.write('sample-delivery', {**profile, 'description': 'Edited', 'savedAt': time.time()})
        with self.assertRaisesRegex(ValueError, 'changed since the review'):
            app.deployments.apply(preview['id'])
        preview = app.deployments.preview({'fleetId': fleet['id'], 'revision': self.revision(app), 'kind': 'profile', 'profile': 'sample-delivery'})
        self.edit(app, lambda f: f.update(description='Changed'))
        with self.assertRaisesRegex(ValueError, 'changed'):
            app.deployments.apply(preview['id'])
        self.assertNotIn(fleet['id'], app.deployments.data['active'])


if __name__ == '__main__':
    unittest.main()
