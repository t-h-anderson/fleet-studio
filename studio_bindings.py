"""Stable runtime identities; omitted fields retain legacy local Codex semantics."""


def runtime_binding(agent):
    return {key: agent[key] for key, default in (('backend', 'codex'), ('hostId', 'local'))
            if agent.get(key, default) != default}


def session_key(agent):
    sid = agent.get('sessionId', agent.get('id', ''))
    host, backend = agent.get('hostId', 'local'), agent.get('backend', 'codex')
    return sid if (host, backend) == ('local', 'codex') else ':'.join((host, backend, sid))


def same_binding(left, right, fields=('sessionId', 'directory')):
    return bool(left is not None and right is not None and runtime_binding(left) == runtime_binding(right)
                and all(left.get(key) == right.get(key) for key in fields))


def host_config(agent, hosts):
    host = agent.get('hostId', 'local')
    return next(({k: h[k] for k in ('id', 'url', 'tokenEnv')} for h in hosts if h['id'] == host), None)
