"""Secret-safe tracker read diagnostics and opt-in local proxy recovery."""
import ipaddress
import json
import logging
import os
import re
import socket
import subprocess
from urllib.parse import urlsplit


class TrackerReadError(ValueError):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


def failure_kind(output):
    """Inspect CLI diagnostics internally; never return their text to a caller."""
    text = output.lower()
    # URLs and ports often contain these numbers. Only explicit HTTP status
    # syntax establishes an API status; a numeric issue ID is not a response.
    statuses = set(re.findall(r'\b(?:http(?:/\d(?:\.\d)?)?\s+|status(?:\s+code)?\s*[:=]?\s*)([45]\d\d)\b', text))
    if '429' in statuses or 'rate limit' in text:
        return 'rate-limit'
    if '401' in statuses or any(s in text for s in ('invalid token', 'authentication failed', 'not logged', 'auth login', 'bad credentials')):
        return 'authentication'
    if statuses & {'403', '404'} or 'forbidden' in text:
        return 'access'
    if any(s in text for s in ('certificate', 'x509', 'tls handshake')):
        return 'tls'
    if any(s in text for s in ('timed out', 'timeout', 'deadline exceeded')):
        return 'timeout'
    if any(s in text for s in ('proxyconnect', 'proxy connection', 'connection refused', 'connectex', 'no such host',
                               'could not resolve', 'network is unreachable', 'connection reset', 'dial tcp')):
        return 'connectivity'
    return 'unknown'


def read_error(kind, provider, host):
    cli = 'gh' if provider == 'GitHub' else 'glab'
    actions = {
        'missing-cli': 'The ' + cli + ' executable is unavailable. Check its installation and the Studio process PATH.',
        'authentication': 'Authentication failed. Check the ' + cli + ' login for this host.',
        'access': 'The project is unavailable or access was denied. Check its URL and account permissions.',
        'connectivity': 'The connection failed. Check network access and inherited proxy settings; this does not establish a login failure.',
        'tls': 'TLS verification failed. Check the configured certificate trust and proxy; keep verification enabled.',
        'timeout': 'The read timed out. Check connectivity and retry the read.',
        'rate-limit': 'The API rate limit was reached. Wait before refreshing again.',
        'response': 'The CLI returned an unexpected response. Check the CLI version and tracker availability.',
        'unknown': 'The CLI read failed. Check tracker connectivity, project access and CLI configuration.',
    }
    return TrackerReadError(kind, provider + ' (' + host + '): ' + actions[kind])


def unavailable_local_proxies(environment):
    """Return only unreachable, unauthenticated literal-loopback proxy settings."""
    removed = []
    probes = {}
    for key, value in environment.items():
        if key.lower() not in ('http_proxy', 'https_proxy', 'all_proxy'):
            continue
        try:
            proxy = urlsplit(value)
            host = proxy.hostname or ''
            local = host.lower() == 'localhost'
            if not local:
                try:
                    local = ipaddress.ip_address(host).is_loopback
                except ValueError:
                    continue
            if not local or proxy.username is not None or proxy.password is not None:
                continue
            defaults = {'http': 80, 'https': 443, 'socks5': 1080, 'socks5h': 1080}
            if proxy.scheme not in defaults:
                continue
            endpoint = (host, proxy.port or defaults[proxy.scheme])
            if endpoint not in probes:
                try:
                    with socket.create_connection(endpoint, timeout=0.2):
                        probes[endpoint] = True
                except OSError:
                    probes[endpoint] = False
            if not probes[endpoint]:
                removed.append(key)
        except (ValueError, TypeError):
            continue
    return removed


def read_cli(arguments, provider, host, env=None, recover_local_proxy=False):
    environment = dict(os.environ if env is None else env)

    def execute():
        try:
            return subprocess.run(arguments, capture_output=True, text=True, encoding='utf-8', errors='replace',
                                  timeout=35, env=environment, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except FileNotFoundError:
            raise read_error('missing-cli', provider, host) from None
        except subprocess.TimeoutExpired:
            raise read_error('timeout', provider, host) from None
        except OSError:
            raise read_error('unknown', provider, host) from None

    run = execute()
    kind = failure_kind((run.stderr or '') + '\n' + (run.stdout or '')) if run.returncode else None
    # This adapter executes read-only API GETs. Never generalise recovery to mutations.
    if run.returncode and kind == 'connectivity' and recover_local_proxy and '--method' in arguments:
        index = arguments.index('--method')
        if arguments[index + 1:index + 2] == ['GET']:
            removed = unavailable_local_proxies(environment)
            if removed:
                for key in removed:
                    environment.pop(key)
                logging.getLogger(__name__).warning('Retrying tracker GET with unavailable local proxy settings omitted for this child process (explicitly enabled).')
                run = execute()
    if run.returncode:
        raise read_error(failure_kind((run.stderr or '') + '\n' + (run.stdout or '')), provider, host)
    try:
        return json.loads(run.stdout)
    except (ValueError, TypeError):
        raise read_error('response', provider, host) from None


def freshness(snapshot, now):
    observed = snapshot.get('observedAt')
    if observed is None:
        return 'unavailable'
    if snapshot.get('error') or now - observed > 180:
        return 'stale'
    return 'partial' if snapshot.get('partial') else 'current'
