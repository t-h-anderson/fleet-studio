"""Dispatch: finished work moves on, idle agents get the next ticket, blockers reach their owner."""
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_design as fixtures


def ticket(iid, labels=('AI Ready',), assignees=()):
    return {'iid': iid, 'title': 'Ticket ' + str(iid), 'state': 'opened', 'labels': list(labels), 'stage': 'ready',
            'url': 'https://gitlab.example/team/project/-/issues/' + str(iid), 'assignees': list(assignees)}


class DispatchTests(unittest.TestCase):
    setUp = fixtures.DesignTests.setUp
    tearDown = fixtures.DesignTests.tearDown
    app = fixtures.DesignTests.app
    monitor = fixtures.DesignTests.monitor

    def writers(self, app, count=2):
        """Make every agent a writer so they compete for the same tickets."""
        state = app.store.snapshot()
        fleet = state['fleets'][0]
        writer = next(r for r in fleet['roles'] if r['key'] == 'writer')
        for agent in fleet['agents'][:count]:
            agent['roleIds'] = [writer['id']]
        app.store.save(state)
        fleet = app.store.fleet(fleet['id'])
        for agent in fleet['agents']:
            app.health.current[(fleet['id'], agent['id'])] = {'state': 'idle', 'since': time.time() - 600, 'signals': [], 'openWork': False}
        return fleet

    def backlog(self, items):
        return {'items': items, 'observedAt': time.time(), 'error': ''}

    def sent(self, app, send):
        return [app.controls.previews[c.args[0]]['targets'][0] for c in send.call_args_list]

    def test_idle_agents_each_get_a_different_oldest_ticket_once(self):
        app, fleet = self.app()
        fleet = self.writers(app)
        items = [ticket(7), ticket(3), ticket(5, assignees=['someone'])]
        with patch.object(app.backlog, 'snapshot', return_value=self.backlog(items)), \
                patch.object(app.controls, 'send', side_effect=[{'id': 'r1'}, {'id': 'r2'}]) as send:
            app.dispatch.tick(app.store.snapshot(), self.monitor(fleet))
            app.dispatch.tick(app.store.snapshot(), self.monitor(fleet))
        targets = self.sent(app, send)
        self.assertEqual(len(targets), 2)
        self.assertIn('/issues/3', targets[0]['text'])
        self.assertIn('/issues/7', targets[1]['text'])
        self.assertNotEqual(targets[0]['agentId'], targets[1]['agentId'])

    def test_nothing_is_sent_without_an_eligible_ticket(self):
        app, fleet = self.app()
        fleet = self.writers(app)
        with patch.object(app.backlog, 'snapshot', return_value=self.backlog([ticket(1, assignees=['x'])])), \
                patch.object(app.controls, 'send') as send:
            app.dispatch.tick(app.store.snapshot(), self.monitor(fleet))
        send.assert_not_called()

    def test_offered_ticket_is_open_work_and_the_inferred_ticket(self):
        app, fleet = self.app()
        fleet = self.writers(app, 1)
        with patch.object(app.backlog, 'snapshot', return_value=self.backlog([ticket(4)])), \
                patch.object(app.controls, 'send', return_value={'id': 'r1'}):
            app.dispatch.tick(app.store.snapshot(), self.monitor(fleet))
        agent = fleet['agents'][0]
        from contextlib import closing
        with closing(app.progress.connect()) as db:
            self.assertTrue(app.health.open_work(db, fleet['id'], agent)[0])
        self.assertTrue(app.automation.ticket(fleet, agent).endswith('/issues/4'))

    def test_done_routes_to_the_next_role(self):
        app, fleet = self.app()
        writer, integrator = fleet['agents']
        with patch.object(app.controls, 'send', return_value={'id': 'r1'}) as send:
            result = app.reports.report({'kind': 'done', 'fleetId': fleet['id'], 'agentId': writer['id'], 'note': 'Merged-ready',
                                         'ticket': 'https://gitlab.example/team/project/-/issues/9', 'to': 'Integrator'})
        [target] = self.sent(app, send)
        self.assertEqual(target['agentId'], integrator['id'])
        self.assertIn('passed to ' + integrator['name'], result['next'])

    def test_done_with_several_next_roles_asks_which(self):
        app, fleet = self.app()
        writer = fleet['agents'][0]
        with patch.object(app.controls, 'send') as send:
            result = app.reports.report({'kind': 'done', 'fleetId': fleet['id'], 'agentId': writer['id'], 'note': 'Finished'})
        send.assert_not_called()
        self.assertIn('Who picks this up next?', result['next'])

    def test_blocked_reaches_its_owner_who_wakes_the_agent(self):
        app, fleet = self.app()
        writer, integrator = fleet['agents']
        with patch.object(app.controls, 'send', return_value={'id': 'r1'}) as send:
            result = app.reports.report({'kind': 'blocked', 'fleetId': fleet['id'], 'agentId': writer['id'],
                                         'note': 'Need the merge window', 'to': integrator['name']})
            self.assertIn(integrator['name'] + ' has been told', result['next'])
            app.reports.report({'kind': 'done', 'fleetId': fleet['id'], 'agentId': integrator['id'], 'note': 'Window open'})
            app.reports.report({'kind': 'done', 'fleetId': fleet['id'], 'agentId': integrator['id'], 'note': 'Again'})
        texts = [t['text'] for t in self.sent(app, send)]
        self.assertIn('is blocked and needs you', texts[0])
        self.assertEqual(sum('has reported back' in t for t in texts), 1)

    def test_blocked_without_owner_escalates(self):
        app, fleet = self.app()
        app.reports.report({'kind': 'blocked', 'fleetId': fleet['id'], 'agentId': fleet['agents'][0]['id'], 'note': 'No credentials'})
        self.assertEqual(len(app.automation.snapshot(fleet['id'])['escalations']), 1)


if __name__ == '__main__':
    unittest.main()
