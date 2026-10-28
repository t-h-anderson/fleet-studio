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
            # Finishing unrelated tickets does not wake the blocked agent; reporting the blocker sorted does, once.
            app.reports.report({'kind': 'done', 'fleetId': fleet['id'], 'agentId': integrator['id'], 'note': 'Other work',
                                'ticket': 'https://gitlab.example/team/project/-/issues/77'})
            self.assertFalse(any('has reported back' in t['text'] for t in self.sent(app, send)))
            app.reports.report({'kind': 'unblocked', 'fleetId': fleet['id'], 'agentId': integrator['id'], 'note': 'Window open'})
            app.reports.report({'kind': 'unblocked', 'fleetId': fleet['id'], 'agentId': integrator['id'], 'note': 'Again'})
        texts = [t['text'] for t in self.sent(app, send)]
        self.assertIn('is blocked and needs you', texts[0])
        self.assertEqual(sum('has reported back' in t for t in texts), 1)

    def test_blocked_without_owner_escalates_and_acknowledging_wakes_the_agent(self):
        from studio_automation import Automation
        app, fleet = self.app()
        writer = fleet['agents'][0]
        result = app.reports.report({'kind': 'blocked', 'fleetId': fleet['id'], 'agentId': writer['id'], 'note': 'No credentials'})
        self.assertIn('once they have looked at it', result['next'])
        [escalation] = app.automation.snapshot(fleet['id'])['escalations']
        # Saved straight away, not on the next tick that happens to change something.
        self.assertEqual(len(Automation(app).data['escalations']), 1)
        with patch.object(app.controls, 'send', return_value={'id': 'r1'}) as send:
            app.automation.acknowledge(fleet['id'], escalation['id'])
            app.automation.acknowledge(fleet['id'], escalation['id'])
        [target] = self.sent(app, send)
        self.assertEqual(target['agentId'], writer['id'])
        self.assertIn('The operator has looked at what blocked you', target['text'])

    def test_a_stale_claim_never_locks_an_agent_out(self):
        app, fleet = self.app()
        fleet = self.writers(app, 1)
        writer = fleet['agents'][0]
        # Closed in the tracker without a report: the claim is released and the next ticket offered.
        app.dispatch.claim(fleet['id'], ticket(4)['url'], writer['id'])
        closed = {**ticket(4), 'state': 'closed'}
        with patch.object(app.backlog, 'snapshot', return_value=self.backlog([closed, ticket(5)])), \
                patch.object(app.controls, 'send', return_value={'id': 'r1'}) as send:
            app.dispatch.tick(app.store.snapshot(), self.monitor(fleet))
        self.assertIn('/issues/5', self.sent(app, send)[0]['text'])
        # A claim older than the agent's last done report was settled by it.
        app.dispatch.release(fleet['id'], ticket(5)['url'])
        app.dispatch.claim(fleet['id'], ticket(6)['url'], writer['id'])
        app.reports.report({'kind': 'done', 'fleetId': fleet['id'], 'agentId': writer['id'], 'note': 'Finished',
                            'ticket': 'https://gitlab.example/team/project/-/issues/99'})
        self.assertTrue(app.dispatch.ready(fleet, writer, self.monitor(fleet), app.dispatch.claims(fleet['id'])))

    def test_a_failed_offer_releases_the_ticket_and_is_not_retried_every_tick(self):
        app, fleet = self.app()
        fleet = self.writers(app, 1)
        with patch.object(app.backlog, 'snapshot', return_value=self.backlog([ticket(4)])), \
                patch.object(app.controls, 'send', side_effect=RuntimeError('backend down')) as send:
            app.dispatch.tick(app.store.snapshot(), self.monitor(fleet))
            app.dispatch.tick(app.store.snapshot(), self.monitor(fleet))
        self.assertEqual(send.call_count, 1)
        self.assertEqual(app.dispatch.claims(fleet['id'])['https://gitlab.example/team/project/-/issues/4']['status'], 'done')

    def test_an_undeliverable_blocker_notice_is_escalated(self):
        app, fleet = self.app()
        writer, integrator = fleet['agents']
        with patch.object(app.controls, 'send', side_effect=RuntimeError('session gone')):
            app.reports.report({'kind': 'blocked', 'fleetId': fleet['id'], 'agentId': writer['id'], 'note': 'Need access', 'to': integrator['name']})
        [escalation] = app.automation.snapshot(fleet['id'])['escalations']
        self.assertIn('blocked-notify failed', escalation['message'])

    def test_a_direct_handoff_claims_the_ticket_for_its_recipient(self):
        app, fleet = self.app()
        writer, integrator = fleet['agents']
        url = 'https://gitlab.example/team/project/-/issues/9'
        with patch.object(app.controls, 'send', return_value={'id': 'r1'}):
            app.reports.report({'kind': 'handoff', 'fleetId': fleet['id'], 'agentId': writer['id'], 'note': 'Yours', 'to': 'Integrator', 'ticket': url})
        claim = app.dispatch.claims(fleet['id'])[url]
        self.assertEqual((claim['agent'], claim['status']), (integrator['id'], 'offered'))
        # A retried report reuses the delivery and must not leave the ticket unclaimed.
        payload = {'kind': 'handoff', 'fleetId': fleet['id'], 'agentId': writer['id'], 'note': 'Again', 'to': 'Integrator', 'ticket': url, 'retryId': 'x'}
        with patch.object(app.controls, 'send', return_value={'id': 'r2'}) as send:
            app.reports.report(payload)
            app.reports.report(payload)
        self.assertEqual(send.call_count, 1)
        claim = app.dispatch.claims(fleet['id'])[url]
        self.assertEqual((claim['agent'], claim['status']), (integrator['id'], 'offered'))

    def test_a_done_report_closes_every_claim_the_agent_held(self):
        app, fleet = self.app()
        writer = fleet['agents'][0]
        for iid in (1, 2):
            app.dispatch.claim(fleet['id'], ticket(iid)['url'], writer['id'])
        app.reports.report({'kind': 'done', 'fleetId': fleet['id'], 'agentId': writer['id'], 'note': 'Finished', 'ticket': ticket(2)['url']})
        self.assertEqual({row['status'] for row in app.dispatch.claims(fleet['id']).values()}, {'done'})

    def test_stop_on_an_agent_with_no_running_turn_still_mutes_it(self):
        from studio_agents import NothingRunning
        from studio_automation import clean_automations
        app, fleet = self.app()
        writer = fleet['agents'][0]
        [rule] = clean_automations([{'id': str(uuid.uuid4()), 'name': 'Stop loops', 'when': {'signal': 'looping'}, 'action': 'stop'}], set())
        fire = {'action': 'stop', 'status': 'pending', 'detail': '', 'fallback': False}
        health = {'state': 'looping', 'since': 0, 'signals': []}
        with patch.object(app.automation, 'stop_turn', side_effect=NothingRunning('nothing')):
            app.automation.execute(fleet, rule, writer, health, fire, self.monitor(fleet))
        self.assertEqual(fire['status'], 'sent')
        self.assertIn('No turn was running', fire['detail'])
        self.assertTrue(app.automation.paused_reason(fleet['id'], writer))


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

    def test_working_limit_holds_within_one_tick(self):
        app, fleet = self.app()
        state = app.store.snapshot()
        state['fleets'][0]['throttles'] = {'maxWorkingAgents': 1}
        app.store.save(state)
        fleet = app.store.fleet(fleet['id'])
        monitor = self.monitor(fleet)
        writer, integrator = fleet['agents']
        # Both are idle in the tick's snapshot; letting work through to one counts it as working.
        self.assertEqual(app.automation.hold(fleet['id'], [writer], monitor), '')
        self.assertIn('Throttled', app.automation.hold(fleet['id'], [integrator], monitor))

    def test_cooldown_runs_from_delivery_not_creation(self):
        app, fleet = self.app()
        writer = fleet['agents'][0]
        [cleaned] = __import__('studio_automation').clean_automations([{'id': str(uuid.uuid4()), 'name': 'Reprompt', 'when': {'signal': 'stalled'},
                                                                        'action': 'reprompt', 'cooldownMinutes': 15}], set())
        now = time.time()
        app.automation.data['fires'].append({'id': 'f', 'fleetId': fleet['id'], 'ruleId': cleaned['id'], 'ruleName': 'Reprompt',
            'agentId': writer['id'], 'agentName': writer['name'], 'action': 'reprompt', 'fallback': False, 'at': now - 3600,
            'sentAt': now - 60, 'episode': 0, 'healthState': 'stalled', 'evidence': '', 'status': 'sent', 'detail': '', 'outcome': None})
        health = {'state': 'stalled', 'since': 0, 'signals': [{'state': 'stalled', 'kind': 'idle-with-work', 'message': 'x'}]}
        with patch.object(app.automation, 'execute') as execute:
            self.assertFalse(app.automation.consider(fleet, cleaned, writer, health, self.monitor(fleet), now))
            execute.assert_not_called()

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


class Round4Tests(unittest.TestCase):
    """Studio acts by default and does not add waste of its own."""
    setUp = fixtures.DesignTests.setUp
    tearDown = fixtures.DesignTests.tearDown
    app = fixtures.DesignTests.app
    monitor = fixtures.DesignTests.monitor

    def rule(self, **changes):
        from studio_automation import clean_automations
        [rule] = clean_automations([{'id': str(uuid.uuid4()), 'name': 'Rule', 'when': {'signal': 'any-problem'}, 'action': 'reprompt', **changes}], set())
        return rule

    def health(self, state='stalled', kind='idle-with-work'):
        return {'state': state, 'since': 0, 'signals': [{'state': state, 'kind': kind, 'message': 'x'}]}

    def test_an_unconfigured_fleet_recovers_by_default_but_an_emptied_one_stays_empty(self):
        app, fleet = self.app()
        rules = app.store.fleet(fleet['id'])['automations']
        self.assertEqual(len(rules), 4)
        self.assertTrue(all(r['enabled'] for r in rules))
        state = app.store.snapshot()
        state['fleets'][0]['automations'] = []
        app.store.save(state)
        self.assertEqual(app.store.fleet(fleet['id'])['automations'], [])

    def test_unrecognised_commands_are_not_work(self):
        from studio_telemetry import call_metadata
        cls = lambda command: call_metadata('Bash', {'command': command})['cls']
        self.assertEqual([cls('git fetch origin'), cls('jq .x f.json'), cls('some-tool --x')], ['other'] * 3)
        self.assertEqual([cls('python -m pytest'), cls('npm test'), cls('matlab -batch "runtests"'), cls('./build.sh')], ['work'] * 4)

    def test_an_agent_whose_turn_just_ended_gets_the_next_ticket_now(self):
        app, fleet = self.app()
        writer = fleet['agents'][0]
        app.health.current[(fleet['id'], writer['id'])] = {'state': 'productive', 'since': time.time(), 'signals': [], 'openWork': False}
        monitor = self.monitor(fleet)
        self.assertTrue(app.dispatch.ready(fleet, writer, monitor, {}))
        monitor['sessions'][writer['sessionId']]['status'] = 'working'
        self.assertFalse(app.dispatch.ready(fleet, writer, monitor, {}))

    def test_periodic_checks_skip_blocked_agents_and_timers_reach_only_busy_ones(self):
        app, fleet = self.app()
        writer, integrator = fleet['agents']
        app.health.current[(fleet['id'], writer['id'])] = {'state': 'waiting', 'since': 0, 'signals': [], 'openWork': True}
        self.assertFalse(app.triggers.has_open_work(fleet, writer))
        app.health.current[(fleet['id'], writer['id'])] = {'state': 'idle', 'since': 0, 'signals': [], 'openWork': True}
        self.assertTrue(app.triggers.has_open_work(fleet, writer))

    def test_one_recovery_message_at_a_time_across_rules(self):
        app, fleet = self.app()
        writer = fleet['agents'][0]
        first, second = self.rule(name='First'), self.rule(name='Second')
        now = time.time()
        with patch.object(app.controls, 'send', return_value={'id': 'r1'}):
            self.assertTrue(app.automation.consider(fleet, first, writer, self.health(), self.monitor(fleet), now))
            self.assertFalse(app.automation.consider(fleet, second, writer, self.health(), self.monitor(fleet), now + 60))

    def test_reprompts_that_are_not_helping_escalate_instead(self):
        app, fleet = self.app()
        writer = fleet['agents'][0]
        with patch.object(app.controls, 'send') as send:
            app.automation.consider(fleet, self.rule(), writer, self.health('looping', 'nudge-loop'), self.monitor(fleet), time.time())
        send.assert_not_called()
        self.assertEqual(app.automation.data['fires'][-1]['action'], 'escalate')

    def test_a_looping_turn_is_stopped_before_the_reprompt(self):
        app, fleet = self.app()
        writer = fleet['agents'][0]
        calls = []
        with patch.object(app.automation, 'stop_turn', side_effect=lambda a: calls.append('stop') or 'Stopped.'), \
                patch.object(app.automation, 'send', side_effect=lambda *a: calls.append('send')):
            app.automation.consider(fleet, self.rule(), writer, self.health('looping', 'failing-repeat'), self.monitor(fleet), time.time())
        self.assertEqual(calls, ['stop', 'send'])

    def test_a_delivery_held_by_an_open_claude_client_escalates_at_once(self):
        from studio_triggers import CLIENT_OPEN
        app, fleet = self.app()
        writer = fleet['agents'][0]
        now = time.time()
        fire = {'id': 'f', 'fleetId': fleet['id'], 'ruleId': 'r', 'ruleName': 'Restart', 'agentId': writer['id'], 'agentName': writer['name'],
                'action': 'reprompt', 'at': now, 'status': 'held', 'evidence': 'Stalled', 'detail': writer['name'] + CLIENT_OPEN + '; Studio cannot start a turn.'}
        self.assertTrue(app.automation.escalate_long_hold(fleet, fire, now, 20))
        self.assertIn('Reply in the session', app.automation.data['escalations'][-1]['message'])

    def test_the_brief_says_how_to_choose_the_next_role(self):
        from workflow_spec import brief_document, export_spec
        app, fleet = self.app()
        text = brief_document(fleet, export_spec(fleet), fleet['agents'][0], 'a' * 32, Path('/studio'))
        self.assertIn('report done --to <role>', text)

    def test_the_operator_is_reminded_once_about_a_blocked_agent(self):
        app, fleet = self.app()
        writer = fleet['agents'][0]
        app.reports.report({'kind': 'blocked', 'fleetId': fleet['id'], 'agentId': writer['id'], 'note': 'No credentials'})
        record = app.automation.data['escalations'][-1]
        state = app.store.snapshot()
        self.assertFalse(app.automation.remind(state, record['at'] + 600))
        self.assertTrue(app.automation.remind(state, record['at'] + 2000))
        self.assertFalse(app.automation.remind(state, record['at'] + 4000))
        self.assertIn('Still blocked', record['message'])
        with patch.object(app.controls, 'send', return_value={'id': 'r1'}) as send:
            app.automation.acknowledge(fleet['id'], record['id'])
        text = app.controls.previews[send.call_args.args[0]]['targets'][0]['text']
        self.assertIn('You reported: No credentials', text)


class Round4ReviewTests(unittest.TestCase):
    """Regressions found reviewing round 4."""
    setUp = fixtures.DesignTests.setUp
    tearDown = fixtures.DesignTests.tearDown
    app = fixtures.DesignTests.app
    monitor = fixtures.DesignTests.monitor

    def test_real_work_behind_paths_prefixes_and_wrappers_still_counts(self):
        from studio_telemetry import call_metadata
        for command in ('.venv/bin/python -m pytest -q', 'FOO=1 pytest', 'PYTHONPATH=. python -m pytest', 'timeout 600 pytest',
                        'sudo make', 'coverage run -m pytest', 'pre-commit run -a', 'uv run pytest', 'docker exec app pytest',
                        'Set-Content -Path a.txt -Value x', r'.\.venv\Scripts\python.exe -m pytest'):
            self.assertEqual(call_metadata('Bash', {'command': command})['cls'], 'work', command)

    def test_an_unrecognised_command_never_makes_a_busy_agent_look_stuck(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_health import NOW, call, turn
        from studio_health import DEFAULTS, assess
        calls = []
        for i in range(4):
            calls.append(call('Bash', {'command': 'internal-runner --check'}, NOW - 600 + i * 100))
            calls.append(call('Bash', {'command': 'cat out.log'}, NOW - 590 + i * 100))
        self.assertEqual(assess({'status': 'working'}, [turn(calls, NOW - 10, status='open')], NOW, DEFAULTS)['state'], 'productive')

    def test_reminders_only_for_fresh_blockers_still_in_force(self):
        app, fleet = self.app()
        writer = fleet['agents'][0]
        app.reports.report({'kind': 'blocked', 'fleetId': fleet['id'], 'agentId': writer['id'], 'note': 'No credentials'})
        record = app.automation.data['escalations'][-1]
        state = app.store.snapshot()
        # Far past the window (an old escalation after an upgrade): no burst of reminders.
        self.assertFalse(app.automation.remind(state, record['at'] + 10 * 3600))
        # The agent reported since, so it is no longer blocked.
        app.reports.report({'kind': 'unblocked', 'fleetId': fleet['id'], 'agentId': writer['id'], 'note': 'Found them'})
        self.assertFalse(app.automation.remind(state, record['at'] + 2000))

    def test_blocks_reported_before_targeted_wakes_still_wake_on_done(self):
        import json
        from contextlib import closing
        app, fleet = self.app()
        writer, integrator = fleet['agents']
        note = {'kind': 'blocked', 'note': 'Need the window', 'at': time.time() - 60, 'waitingOn': integrator['id'], 'ticket': ''}
        with closing(app.progress.connect()) as db, db:
            db.execute('INSERT OR REPLACE INTO agent_notes VALUES(?,?,?)', (fleet['id'], writer['id'], json.dumps(note)))
        with patch.object(app.controls, 'send', return_value={'id': 'r1'}) as send:
            app.dispatch.wake_waiting(fleet, integrator, 'done', 'https://gitlab.example/team/project/-/issues/5')
        self.assertEqual(send.call_count, 1)

    def test_claude_entry_point_keeps_the_persons_own_notes(self):
        app, fleet = self.app()
        state = app.store.snapshot()
        state['fleets'][0]['agents'][0]['backend'] = 'claude-code'
        app.store.save(state)
        preview = app.deployments.preview({'fleetId': fleet['id'], 'revision': app.store.snapshot()['revision'], 'kind': 'deploy'})
        app.deployments.apply(preview['id'])
        directory = Path(app.store.fleet(fleet['id'])['agents'][0]['directory'])
        (directory / 'CLAUDE.local.md').write_text('# My notes\nUse the staging DB.\n', encoding='utf-8')
        [change] = app.entrypoints.preview({'fleetId': fleet['id'], 'revision': app.store.snapshot()['revision'],
                                            'agentIds': [fleet['agents'][0]['id']]})['changes']
        self.assertTrue(change['after'].startswith('# My notes\nUse the staging DB.\n'))
        self.assertIn('<!-- fleet-studio:start -->', change['after'])

    def test_existing_fleets_get_the_defaults_once(self):
        import json
        from fleet_studio import Store
        app, fleet = self.app()
        path = app.store.path
        saved = json.loads(path.read_text())
        saved.pop('recoveryDefaults', None)
        saved['fleets'][0]['automations'] = []
        path.write_text(json.dumps(saved))
        self.assertEqual(len(Store(app.root).snapshot()['fleets'][0]['automations']), 4)
        upgraded = json.loads(path.read_text())
        upgraded['fleets'][0]['automations'] = []
        path.write_text(json.dumps(upgraded))
        self.assertEqual(Store(app.root).snapshot()['fleets'][0]['automations'], [])
