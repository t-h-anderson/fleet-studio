"""Tracker outages and recovery with synthetic CLI results; no network or login."""
from copy import deepcopy
import os
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from studio_backlog import clean_config
from studio_tracker_io import TrackerReadError, freshness, read_cli, unavailable_local_proxies
from studio_trackers import github_get, gitlab_get, tracker_for
import test_progress as fixtures


def result(code=0, output='[]', error=''):
    return SimpleNamespace(returncode=code, stdout=output, stderr=error)


class TrackerReadTests(unittest.TestCase):
    args = ['glab', 'api', 'projects/example/issues', '--method', 'GET']

    def test_failures_classified_without_echoing_credentials(self):
        cases = {'HTTP 401': 'authentication', 'HTTP 403': 'access', 'HTTP 404': 'access',
                 'HTTP 429 rate limit': 'rate-limit', 'x509 certificate rejected': 'tls',
                 'context deadline exceeded': 'timeout', 'proxyconnect tcp: connection refused': 'connectivity',
                 'could not resolve host': 'connectivity', 'unrecognised failure': 'unknown'}
        for diagnostic, kind in cases.items():
            with self.subTest(kind=kind), patch('studio_tracker_io.subprocess.run', return_value=result(1, 'token=secret', diagnostic)), patch('studio_tracker_io.socket.create_connection') as probe:
                with self.assertRaises(TrackerReadError) as caught:
                    read_cli(self.args, 'GitLab', 'tracker.example', env={})
                self.assertEqual(caught.exception.kind, kind)
                self.assertNotIn('secret', str(caught.exception))
                probe.assert_not_called()

    def test_spawn_and_response_errors_are_safe(self):
        cases = [(FileNotFoundError('secret'), 'missing-cli'),
                 (subprocess.TimeoutExpired('secret', 35, output='secret'), 'timeout'),
                 (OSError('secret'), 'unknown'), (result(output='secret-not-json'), 'response')]
        for outcome, kind in cases:
            with self.subTest(kind=kind), patch('studio_tracker_io.subprocess.run', side_effect=[outcome]):
                with self.assertRaises(TrackerReadError) as caught:
                    read_cli(self.args, 'GitHub', 'github.com', env={})
                self.assertEqual(caught.exception.kind, kind)
                self.assertNotIn('secret', str(caught.exception))

    def test_numeric_ticket_ids_are_not_http_statuses(self):
        from studio_tracker_io import failure_kind
        self.assertEqual(failure_kind('Get https://gitlab.example/issues/401: dial tcp 127.0.0.1:64356: connection refused'), 'connectivity')
        self.assertEqual(failure_kind('Get https://gitlab.example/pipelines/429: x509: certificate signed by unknown authority'), 'tls')
        for text, kind in [('HTTP/1.1 401 Unauthorized', 'authentication'), ('status code: 403', 'access'), ('HTTP 404', 'access'), ('HTTP 429', 'rate-limit')]:
            self.assertEqual(failure_kind(text), kind)

    def test_default_keeps_dead_proxy_without_probing_or_retry(self):
        environment = {'HTTP_PROXY': 'http://127.0.0.1:65531'}
        with patch('studio_tracker_io.subprocess.run', return_value=result(1, error='proxyconnect connection refused')) as run, patch('studio_tracker_io.socket.create_connection') as probe:
            with self.assertRaises(TrackerReadError):
                read_cli(self.args, 'GitLab', 'tracker.example', env=environment)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.kwargs['env'], environment)
            probe.assert_not_called()

    def test_opted_in_get_retries_once_and_isolates_child_environment(self):
        environment = {'HTTP_PROXY': 'http://127.0.0.1:65531', 'HTTPS_PROXY': 'http://127.0.0.1:65531',
                       'ALL_PROXY': 'https://corporate.example:443', 'NO_PROXY': 'tracker.example', 'TOKEN': 'secret'}
        seen = []
        def run(*args, **kwargs):
            seen.append(deepcopy(kwargs['env']))
            return result(1, error='proxyconnect connection refused') if len(seen) == 1 else result(output='{"ok": true}')
        with patch.dict(os.environ, environment, clear=True), patch('studio_tracker_io.subprocess.run', side_effect=run), patch('studio_tracker_io.socket.create_connection', side_effect=ConnectionRefusedError) as probe, self.assertLogs('studio_tracker_io', 'WARNING') as logs:
            self.assertEqual(read_cli(self.args, 'GitLab', 'tracker.example', recover_local_proxy=True), {'ok': True})
            self.assertEqual(dict(os.environ), environment)
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0], environment)
        self.assertEqual(seen[1], {k: v for k, v in environment.items() if k not in ('HTTP_PROXY', 'HTTPS_PROXY')})
        probe.assert_called_once_with(('127.0.0.1', 65531), timeout=0.2)
        self.assertNotIn('secret', str(logs.output))
        self.assertNotIn('65531', str(logs.output))

    def test_live_remote_credentialed_and_malformed_proxies_are_preserved(self):
        for url in ('http://corporate.example:8080', 'http://user:secret@127.0.0.1:8080',
                    'http://127.0.0.1:bad', 'ftp://127.0.0.1:8080', 'not-a-url'):
            with self.subTest(url=url), patch('studio_tracker_io.socket.create_connection') as probe:
                self.assertEqual(unavailable_local_proxies({'HTTPS_PROXY': url}), [])
                probe.assert_not_called()
        with patch('studio_tracker_io.socket.create_connection') as probe:
            self.assertEqual(unavailable_local_proxies({'HTTPS_PROXY': 'http://localhost:8080'}), [])
            probe.assert_called_once()
        with patch('studio_tracker_io.socket.create_connection', side_effect=ConnectionRefusedError):
            self.assertEqual(unavailable_local_proxies({'all_proxy': 'socks5://[::1]:1080'}), ['all_proxy'])

    def test_recovery_never_applies_to_mutations_or_non_connectivity_errors(self):
        for args, diagnostic in [(self.args[:-1] + ['POST'], 'connection refused'),
                                 (self.args[:-2], 'connection refused'), (self.args, 'HTTP 401')]:
            with self.subTest(args=args, diagnostic=diagnostic), patch('studio_tracker_io.subprocess.run', return_value=result(1, error=diagnostic)) as run, patch('studio_tracker_io.unavailable_local_proxies') as probes:
                with self.assertRaises(TrackerReadError):
                    read_cli(args, 'GitLab', 'tracker.example', env={}, recover_local_proxy=True)
                self.assertEqual(run.call_count, 1)
                probes.assert_not_called()

    def test_second_failure_is_classified_without_further_retry(self):
        with patch('studio_tracker_io.subprocess.run', side_effect=[result(1, error='connection refused'), result(1, error='HTTP 401 secret')]) as run, patch('studio_tracker_io.socket.create_connection', side_effect=ConnectionRefusedError), self.assertLogs('studio_tracker_io', 'WARNING'):
            with self.assertRaises(TrackerReadError) as caught:
                read_cli(self.args, 'GitLab', 'tracker.example', env={'HTTP_PROXY': 'http://127.0.0.1:65531'}, recover_local_proxy=True)
            self.assertEqual(caught.exception.kind, 'authentication')
            self.assertNotIn('secret', str(caught.exception))
            self.assertEqual(run.call_count, 2)

    def test_provider_configuration_and_token_isolation(self):
        environment = {'GITLAB_TOKEN': 'stale-secret', 'GH_TOKEN': 'github-secret', 'HTTPS_PROXY': 'https://proxy.example:443'}
        with patch.dict(os.environ, environment, clear=True), patch('studio_trackers.shutil.which', return_value='fake-cli'), patch('studio_tracker_io.subprocess.run', return_value=result()) as run:
            gitlab_get('https://gitlab.example/team/project', 'issues')
            self.assertNotIn('GITLAB_TOKEN', run.call_args.kwargs['env'])
            self.assertEqual(run.call_args.kwargs['env']['HTTPS_PROXY'], environment['HTTPS_PROXY'])
            github_get('https://github.com/team/project', 'issues')
            self.assertEqual(run.call_args.kwargs['env'], environment)
            self.assertEqual(dict(os.environ), environment)
        for provider in ('gitlab', 'github'):
            config = {'provider': provider, 'projectUrl': 'https://tracker.example/team/project', 'recoverLocalProxy': True, 'scope': 'all'}
            with patch('studio_trackers.' + provider + '_get', return_value=[]) as get:
                tracker_for(config).snapshot(config)
                self.assertTrue(get.call_args.kwargs['recover_local_proxy'])
                tracker_for(config).history(config, 1)
                self.assertTrue(get.call_args.kwargs['recover_local_proxy'])

    def test_valid_json_with_wrong_tracker_shape_is_response_error(self):
        for provider in ('gitlab', 'github'):
            config = {'provider': provider, 'projectUrl': 'https://tracker.example/team/project', 'scope': 'all'}
            with patch('studio_trackers.' + provider + '_get', return_value=None):
                with self.assertRaises(TrackerReadError) as caught:
                    tracker_for(config).snapshot(config)
                self.assertEqual(caught.exception.kind, 'response')

    def test_freshness_and_backwards_compatible_configuration(self):
        self.assertEqual(freshness({'observedAt': None, 'error': 'outage'}, 500), 'unavailable')
        self.assertEqual(freshness({'observedAt': 100}, 500), 'stale')
        self.assertEqual(freshness({'observedAt': 499, 'error': 'outage'}, 500), 'stale')
        self.assertEqual(freshness({'observedAt': 499, 'partial': True}, 500), 'partial')
        self.assertEqual(freshness({'observedAt': 499}, 500), 'current')
        self.assertNotIn('recoverLocalProxy', clean_config({}, set()))
        self.assertTrue(clean_config({'recoverLocalProxy': True}, set())['recoverLocalProxy'])
        for invalid in (1, 'true', None):
            with self.assertRaisesRegex(ValueError, 'proxy recovery'):
                clean_config({'recoverLocalProxy': invalid}, set())


class TrackerProjectionTests(unittest.TestCase):
    setUp, tearDown, app = fixtures.ProgressTests.setUp, fixtures.ProgressTests.tearDown, fixtures.ProgressTests.app

    def test_backlog_retains_success_diagnoses_outage_then_clears_error(self):
        app, fleet = self.app()
        with patch('studio_backlog.threading.Thread'), patch('studio_backlog.time.time', return_value=self.now):
            first = app.backlog.snapshot(fleet['id'])
        self.assertEqual(first['freshness'], 'unavailable')
        item = app.backlog.cache[fleet['id']]
        rows = [{'iid': 1, 'url': self.payload['taskUrl'], 'title': 'Example', 'state': 'opened', 'stage': 'ready', 'labels': [], 'assignees': []}]
        app.launch_enabled = True
        with patch('studio_backlog.tracker_for') as adapter, patch('studio_backlog.time.time', return_value=self.now):
            adapter.return_value.snapshot.return_value = (rows, False)
            app.backlog._refresh(fleet['id'], first['config'], item)
            self.assertEqual(app.backlog.snapshot(fleet['id'])['freshness'], 'current')
            adapter.return_value.snapshot.side_effect = TrackerReadError('connectivity', 'Connection failed.')
            app.backlog._refresh(fleet['id'], first['config'], item)
            failed = app.backlog.snapshot(fleet['id'])
            self.assertEqual((failed['freshness'], failed['errorKind']), ('stale', 'connectivity'))
            self.assertEqual(failed['items'][0]['iid'], 1)
            self.assertEqual(failed['observedAt'], self.now)
            adapter.return_value.snapshot.side_effect = None
            app.backlog._refresh(fleet['id'], first['config'], item)
            recovered = app.backlog.snapshot(fleet['id'])
            self.assertEqual((recovered['freshness'], recovered['error'], recovered['errorKind']), ('current', '', ''))

    def test_ci_never_successful_failure_and_opted_in_refresh(self):
        app, fleet = self.app()
        fleet['backlog']['recoverLocalProxy'] = True
        metrics = app.progress.metrics
        first = metrics.ci_snapshot(fleet)
        item = metrics.ci[fleet['id']]
        with patch('studio_trackers.gitlab_get', side_effect=TrackerReadError('authentication', 'Authentication failed.')) as get:
            metrics._refresh_ci(fleet['id'], item)
            self.assertTrue(get.call_args.kwargs['recover_local_proxy'])
        failed = metrics.ci_snapshot(fleet)
        self.assertEqual((first['freshness'], failed['freshness']), ('unavailable', 'unavailable'))
        self.assertEqual(failed['errorKind'], 'authentication')
        self.assertTrue(failed['stale'])
        with patch('studio_trackers.gitlab_get', return_value=[]):
            metrics._refresh_ci(fleet['id'], item)
        recovered = metrics.ci_snapshot(fleet)
        self.assertEqual((recovered['freshness'], recovered['errorKind'], recovered['error']), ('current', '', ''))

    def test_unknown_exception_never_leaks_output_or_claims_login_failure(self):
        app, fleet = self.app()
        metrics = app.progress.metrics
        metrics.ci_snapshot(fleet)
        with patch('studio_trackers.gitlab_get', side_effect=ValueError('token=secret')):
            metrics._refresh_ci(fleet['id'], metrics.ci[fleet['id']])
        failed = metrics.ci_snapshot(fleet)
        self.assertNotIn('secret', failed['error'])
        self.assertNotIn('login', failed['error'])
        self.assertEqual(failed['errorKind'], 'unknown')
