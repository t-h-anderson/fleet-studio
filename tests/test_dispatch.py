"""Dispatch: finished work moves on, idle agents get the next ticket, blockers reach their owner."""
from pathlib import Path
import sys
import time
import uuid
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


class RulesTests(unittest.TestCase):
    setUp = fixtures.DesignTests.setUp
    tearDown = fixtures.DesignTests.tearDown
    app = fixtures.DesignTests.app
    monitor = fixtures.DesignTests.monitor
    rule = fixtures.DesignTests.rule
    save_rules = fixtures.DesignTests.save_rules

    def test_rules_listing_and_timeline_cover_every_source(self):
        from studio_rules import list_rules, timeline
        app, fleet = self.app()
        fleet = self.save_rules(app, fleet, [self.rule(fleet, 'event'), self.rule(fleet, 'idle')])
        listing = list_rules(app, fleet)
        self.assertEqual([r['kind'] for r in listing['routing']], ['event', 'idle'])
        self.assertTrue(listing['routing'][1]['periodic'])
        self.assertIn('only for agents with open work', listing['routing'][1]['when'])
        app.automation.data['fires'].append({'id': 'f', 'fleetId': fleet['id'], 'ruleId': 'dispatch', 'ruleName': 'Next ticket',
            'agentId': 'a', 'agentName': 'A', 'action': 'dispatch', 'at': time.time(), 'status': 'sent', 'detail': '',
            'evidence': 'Ticket 3', 'outcome': None})
        app.triggers.report({'fleetId': fleet['id'], 'agentId': fleet['agents'][0]['id'], 'eventName': 'packet-ready', 'eventId': 'e1',
                             'ticket': 'https://gitlab.example/p/-/issues/1', 'artifact': 'a', 'context': 'c'})
        sources = {row['source'] for row in timeline(app, fleet['id'])}
        self.assertEqual(sources, {'routing', 'dispatch'})

    def deploy(self, app, fleet):
        preview = app.deployments.preview({'fleetId': fleet['id'], 'revision': app.store.snapshot()['revision'], 'kind': 'deploy'})
        app.deployments.apply(preview['id'])
        return app.store.fleet(fleet['id'])

    def test_draft_status_reports_undeployed_changes_and_stale_briefs(self):
        app, fleet = self.app()
        self.assertEqual(app.deployments.draft_status(fleet)['active'], False)
        fleet = self.deploy(app, fleet)
        status = app.deployments.draft_status(fleet)
        self.assertEqual((status['draftChanged'], status['staleAgents']), (False, []))
        state = app.store.snapshot()
        state['fleets'][0]['agents'][0]['notes'] = 'Focus on the parser'
        app.store.save(state)
        status = app.deployments.draft_status(app.store.fleet(fleet['id']))
        self.assertTrue(status['draftChanged'])
        self.assertEqual(status['staleAgents'], [fleet['agents'][0]['id']])

    def test_flow_counts_waiting_hand_offs_between_roles(self):
        from studio_rules import flow
        app, fleet = self.app()
        source, target = fleet['agents'][0], fleet['agents'][1]
        app.health.current[(fleet['id'], source['id'])] = {'state': 'looping', 'since': 0, 'signals': []}
        app.automation.data['fires'].append({'id': 'h', 'fleetId': fleet['id'], 'ruleId': 'agent-handoff', 'ruleName': 'Hand-off',
            'agentId': source['id'], 'agentName': source['name'], 'recipientId': target['id'], 'action': 'handoff',
            'at': 100.0, 'status': 'held', 'detail': '', 'evidence': '', 'outcome': None})
        data = flow(app, fleet)
        self.assertEqual(data['roles'][source['roleIds'][0]]['stuck'], 1)
        edge = data['edges'][source['roleIds'][0] + '>' + target['roleIds'][0]]
        self.assertEqual((edge['pending'], edge['oldestAt']), (1, 100.0))

    def test_recovery_rule_can_watch_one_agent(self):
        from studio_automation import clean_automations
        app, fleet = self.app()
        roles = {r['id'] for r in fleet['roles']}
        agents = {a['id'] for a in fleet['agents']}
        row = {'id': str(uuid.uuid4()), 'name': 'Watch writer', 'when': {'signal': 'stalled'}, 'action': 'reprompt',
               'agentId': fleet['agents'][0]['id']}
        self.assertEqual(clean_automations([row], roles, agents)[0]['agentId'], fleet['agents'][0]['id'])
        with self.assertRaises(ValueError):
            clean_automations([{**row, 'agentId': 'elsewhere'}], roles, agents)

    def test_idle_trigger_never_wakes_an_agent_without_work(self):
        app, fleet = self.app()
        fleet = self.save_rules(app, fleet, [self.rule(fleet, 'idle')])
        monitor = self.monitor(fleet)
        with patch.object(app.controls, 'send') as send:
            now = time.time()
            for step in range(4):
                app.triggers.tick(app.store.snapshot(), monitor, now + step * 120)
        send.assert_not_called()

    def test_recovery_sends_are_not_throttled_but_new_work_is(self):
        app, fleet = self.app()
        state = app.store.snapshot()
        state['fleets'][0]['throttles'] = {'maxWorkingAgents': 1}
        app.store.save(state)
        fleet = app.store.fleet(fleet['id'])
        monitor = self.monitor(fleet)
        monitor['sessions'][fleet['agents'][1]['sessionId']]['status'] = 'working'
        writer = fleet['agents'][0]
        self.assertEqual(app.automation.hold(fleet['id'], [writer], monitor, throttle=False), '')
        self.assertIn('Throttled', app.automation.hold(fleet['id'], [writer], monitor, throttle=True))

    def test_token_budget_escalates_once_an_hour(self):
        app, fleet = self.app()
        state = app.store.snapshot()
        state['fleets'][0]['throttles'] = {'fleetTokensPerHour': 1000}
        app.store.save(state)
        fleet = app.store.fleet(fleet['id'])
        app.health.current[(fleet['id'], fleet['agents'][0]['id'])] = {'state': 'burning', 'since': 0, 'signals': [], 'tokensLastHour': 5000}
        now = time.time()
        app.automation.alert_caps(fleet, now)
        app.automation.alert_caps(fleet, now + 60)
        self.assertEqual(len(app.automation.snapshot(fleet['id'])['escalations']), 1)
