"""Live load on the Workflow board: per hand-off deliveries, role load and hotspots."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_design as fixtures
from studio_rules import flow


class LiveFlowTests(unittest.TestCase):
    setUp = fixtures.DesignTests.setUp
    tearDown = fixtures.DesignTests.tearDown
    app = fixtures.DesignTests.app

    def handoff(self):
        """The fixture's Writer -> Integrator hand-off, switched on so it has a trigger."""
        app, fleet = self.app()
        writer, integrator = fleet['agents']
        link = next(l for l in fleet['roleLinks'] if l['source'] == writer['roleIds'][0] and l['target'] == integrator['roleIds'][0])
        state = app.store.snapshot()
        next(l for l in state['fleets'][0]['roleLinks'] if l['id'] == link['id'])['delivery'] = 'automatic'
        app.store.save(state)
        fleet = app.store.fleet(fleet['id'])
        trigger = next(t for t in fleet['triggers'] if t['workflowLinkId'] == link['id'])
        return app, fleet, link, trigger, writer, integrator

    def action(self, app, fleet, trigger, writer, integrator, status, at, **extra):
        row = {'id': 'a-' + status + str(at), 'fleetId': fleet['id'], 'triggerId': trigger['id'], 'name': trigger['name'],
               'status': status, 'createdAt': at, 'sourceAgentId': writer['id'], 'targetAgentId': integrator['id'],
               'targetName': integrator['name'], **extra}
        app.triggers.data['actions'].append(row)
        return row

    def test_deliveries_land_on_their_handoff(self):
        app, fleet, link, trigger, writer, integrator = self.handoff()
        now = app.dispatch.clock()
        self.action(app, fleet, trigger, writer, integrator, 'pending', now - 60)
        self.action(app, fleet, trigger, writer, integrator, 'held', now - 120)
        self.action(app, fleet, trigger, writer, integrator, 'needs-attention', now - 30)
        self.action(app, fleet, trigger, writer, integrator, 'queued', now - 3600)
        self.action(app, fleet, trigger, writer, integrator, 'reconciled', now - 1800)
        self.action(app, fleet, trigger, writer, integrator, 'queued', now - 90000)
        self.action(app, {'id': 'elsewhere'}, trigger, writer, integrator, 'pending', now - 10)
        row = flow(app, fleet)['links'][link['id']]
        self.assertEqual((row['pending'], row['held'], row['attention'], row['recent']), (1, 1, 1, 2))
        self.assertEqual(row['oldestAt'], now - 120)
        self.assertEqual([i['status'] for i in row['items']], ['held', 'pending', 'needs-attention'])
        self.assertEqual((row['items'][0]['from'], row['items'][0]['to']), (writer['name'], integrator['name']))

    def test_recovery_handoffs_use_the_one_link_joining_the_roles(self):
        app, fleet, link, trigger, writer, integrator = self.handoff()
        fire = {'id': 'h', 'fleetId': fleet['id'], 'ruleId': 'agent-handoff', 'ruleName': 'Hand-off', 'agentId': writer['id'],
                'agentName': writer['name'], 'recipientId': integrator['id'], 'recipientName': integrator['name'],
                'action': 'handoff', 'at': app.dispatch.clock() - 10, 'status': 'held', 'detail': '', 'evidence': '', 'outcome': None}
        app.automation.data['fires'].append(fire)
        self.assertEqual(flow(app, fleet)['links'][link['id']]['held'], 1)
        # With two hand-offs between the same roles, neither can claim it; the role-pair edge still counts it.
        state = app.store.snapshot()
        state['fleets'][0]['roleLinks'].append({**link, 'id': '00000000-0000-4000-8000-000000000001', 'label': 'Second path'})
        app.store.save(state)
        data = flow(app, app.store.fleet(fleet['id']))
        self.assertEqual(data['links'][link['id']]['held'], 0)
        self.assertEqual(data['edges'][writer['roleIds'][0] + '>' + integrator['roleIds'][0]]['pending'], 1)

    def test_role_load_adds_up_tokens_waste_and_throttles(self):
        app, fleet, link, trigger, writer, integrator = self.handoff()
        app.health.current[(fleet['id'], writer['id'])] = {'state': 'admin-heavy', 'tokens24h': 1000, 'wasteTokens24h': 400}
        app.automation.data['throttled'][fleet['id'] + ':' + writer['id']] = app.dispatch.clock() - 60
        role = flow(app, fleet)['roles'][writer['roleIds'][0]]
        self.assertEqual((role['tokens24h'], role['wasteTokens24h'], role['throttled'], role['stuck']), (1000, 400, 1, 1))
        self.assertEqual(role['agents'], [{'id': writer['id'], 'name': writer['name'], 'state': 'admin-heavy'}])

    def test_hotspots_are_the_largest_queue_and_a_slow_handoff(self):
        app, fleet, link, trigger, writer, integrator = self.handoff()
        now = app.dispatch.clock()
        self.assertEqual(flow(app, fleet)['hot'], {'role': None, 'link': None, 'slowMinutes': 15})
        self.action(app, fleet, trigger, writer, integrator, 'pending', now - 60)
        self.assertIsNone(flow(app, fleet)['hot']['link'])
        self.action(app, fleet, trigger, writer, integrator, 'held', now - 20 * 60)
        self.assertEqual(flow(app, fleet)['hot']['link'], link['id'])
        app.dispatch.tickets_by_role = lambda fleet, settings: [('t1', [integrator['roleIds'][0]]), ('t2', [integrator['roleIds'][0]]),
                                                                ('t3', [writer['roleIds'][0]])]
        self.assertEqual(flow(app, fleet)['hot']['role'], integrator['roleIds'][0])


if __name__ == '__main__':
    unittest.main()
