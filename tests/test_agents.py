"""Agent backends, remote nodes, tracker adapters and role runtime constraints. Native CLIs are mocked."""
import json
import os
from pathlib import Path
import threading
import time
import types
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import uuid

import test_design as fixtures
from fleet_node import Node, NodeServer
from fleet_studio import ValidationError
import studio_agents
from studio_agents import ClaudeCodeBackend, ClaudeTranscript, Runtimes, clean_hosts
import studio_trackers
from studio_trackers import GitHubTracker, fetch_github_issues, issue_prefix, provider_of
from workflow_spec import export_spec, runtime_problems, validate


def write_transcript(root, session_id, rows, cwd="/work/project"):
    folder = root / "projects" / "-work-project"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (session_id + ".jsonl")
    with path.open("a", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps({"cwd": cwd, "sessionId": session_id, "timestamp": "2026-10-13T12:00:00Z", **row}) + "\n")
    return path


def assistant(text, stop="end_turn", usage=None, blocks=None):
    return {"type": "assistant", "uuid": uuid.uuid4().hex, "message": {
        "role": "assistant", "model": "claude-test", "stop_reason": stop,
        "usage": usage or {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 1000},
        "content": blocks or [{"type": "thinking", "thinking": "private"}, {"type": "text", "text": text}]}}


class ClaudeBackendTests(unittest.TestCase):
    setUp = fixtures.DesignTests.setUp
    tearDown = fixtures.DesignTests.tearDown

    def test_transcript_reads_public_text_titles_and_tokens_incrementally(self):
        sid = str(uuid.uuid4())
        path = write_transcript(self.root, sid, [
            {"type": "ai-title", "aiTitle": "Generated title"},
            {"type": "user", "uuid": "u1", "message": {"role": "user", "content": "Implement the packet"}},
            assistant("Working on it", stop="tool_use", blocks=[{"type": "text", "text": "Working on it"}, {"type": "tool_use", "name": "Bash", "input": {"command": "secret"}}]),
            {"type": "user", "uuid": "u2", "message": {"role": "user", "content": [{"type": "tool_result", "content": "tool output"}]}},
            {"type": "assistant", "isSidechain": True, "message": {"content": [{"type": "text", "text": "subagent"}]}},
        ])
        reader = ClaudeTranscript(path)
        state = reader.refresh()
        self.assertEqual(state["title"], "Generated title")
        self.assertEqual([m["text"] for m in state["messages"]], ["Implement the packet", "Working on it"])
        self.assertEqual(state["tokensUsed"], 1015)
        self.assertEqual(state["stopReason"], "tool_use")
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"type": "custom-title", "customTitle": "Writer"}) + "\n")
            stream.write(json.dumps(assistant("Done")) + "\n")
            stream.write('{"type": "assistant", "partial')
        state = reader.refresh()
        self.assertEqual(state["title"], "Writer")
        self.assertEqual(state["messages"][-1]["kind"], "summary")
        self.assertEqual(state["tokensUsed"], 2030)
        self.assertNotIn("private", json.dumps(state))
        self.assertNotIn("tool output", json.dumps(state))

    def test_observation_reports_working_idle_and_closed(self):
        sid = str(uuid.uuid4())
        backend = ClaudeCodeBackend(root=self.root)
        write_transcript(self.root, sid, [assistant("Running tests", stop="tool_use")])
        with patch("studio_agents.claude_processes", return_value=[42]):
            item = backend.observe([sid])["sessions"][sid]
        self.assertEqual((item["status"], item["turnStatus"]), ("working", "inProgress"))
        write_transcript(self.root, sid, [assistant("Finished")])
        with patch("studio_agents.claude_processes", return_value=[42]):
            self.assertEqual(backend.observe([sid])["sessions"][sid]["status"], "idle")
        with patch("studio_agents.claude_processes", return_value=[]):
            item = backend.observe([sid])["sessions"][sid]
        self.assertEqual(item["status"], "closed")
        self.assertEqual(item["usage"]["tokensUsed"], 2030)
        missing = str(uuid.uuid4())
        self.assertEqual(backend.observe([missing])["sessions"][missing]["status"], "unknown")

    def test_send_refuses_open_interactive_session_and_resumes_headless_otherwise(self):
        sid = str(uuid.uuid4())
        backend = ClaudeCodeBackend(executable="claude", root=self.root, runtime=self.root / ".runtime")
        path = write_transcript(self.root, sid, [assistant("Done")], cwd=str(self.root))
        os.utime(path, (time.time()-120, time.time()-120))
        with patch("studio_agents.claude_processes", return_value=[7]), patch("studio_agents.subprocess.Popen") as popen:
            with self.assertRaises(RuntimeError):
                backend.send(sid, "Review the packet", str(self.root))
            popen.assert_not_called()
        with patch("studio_agents.claude_processes", return_value=[]), patch("studio_agents.subprocess.Popen") as popen:
            status, _ = backend.send(sid, "Review the packet", str(self.root))
        self.assertEqual(status, "started")
        self.assertEqual(popen.call_args.args[0], ["claude", "-p", "--resume", sid, "--output-format", "json", "--", "Review the packet"])
        self.assertEqual(popen.call_args.kwargs["cwd"], str(self.root))
        with self.assertRaises(studio_agents.BackendUnsupported):
            backend.rename(sid, "Writer")


class BindingTests(unittest.TestCase):
    setUp = fixtures.DesignTests.setUp
    tearDown = fixtures.DesignTests.tearDown
    app = fixtures.DesignTests.app
    monitor = fixtures.DesignTests.monitor
    wait_controls = fixtures.DesignTests.wait_controls

    def test_hosts_require_tls_or_loopback_and_named_token_variable(self):
        self.assertEqual(clean_hosts([{"id": "gpu-box", "url": "https://gpu.example:8790/", "tokenEnv": "GPU_TOKEN"}])[0]["url"],
                         "https://gpu.example:8790")
        clean_hosts([{"id": "tunnel", "url": "http://127.0.0.1:8790", "tokenEnv": "TUNNEL_TOKEN"}])
        for bad in ({"id": "local", "url": "https://a.example", "tokenEnv": "T"},
                    {"id": "plain", "url": "http://a.example", "tokenEnv": "T"},
                    {"id": "creds", "url": "https://user:pw@a.example", "tokenEnv": "T"},
                    {"id": "token", "url": "https://a.example", "tokenEnv": "not a name"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                clean_hosts([bad])

    def test_agent_bindings_validate_hosts_and_skip_remote_folder_checks(self):
        app, f = self.app()
        state = app.store.snapshot()
        state["fleets"][0]["agents"][0].update(hostId="gpu-box", directory="/srv/remote/checkout")
        with self.assertRaises(ValidationError):
            app.store.save(state)
        state["hosts"] = [{"id": "gpu-box", "url": "https://gpu.example", "tokenEnv": "GPU_TOKEN"}]
        state["fleets"][0]["agents"][1]["backend"] = "claude-code"
        saved = app.store.save(state)
        agents = saved["fleets"][0]["agents"]
        self.assertEqual((agents[0]["hostId"], agents[0]["directory"]), ("gpu-box", "/srv/remote/checkout"))
        self.assertEqual(agents[1]["backend"], "claude-code")
        legacy = json.loads((self.root / "fleet.json").read_text())
        self.assertEqual(legacy["agents"], [], "Only local Codex agents belong to the PowerShell launcher")
        # Older clients omit hosts; saving must not drop configured nodes.
        state = app.store.snapshot()
        state.pop("hosts")
        self.assertEqual(app.store.save(state)["hosts"][0]["id"], "gpu-box")

    def test_controls_route_by_backend_binding(self):
        app, f = self.app()
        state = app.store.snapshot()
        state["fleets"][0]["agents"][1]["backend"] = "claude-code"
        app.store.save(state)
        f = app.store.fleet(f["id"])
        preview = app.controls.preview({"fleetId": f["id"], "action": "message", "agentIds": [a["id"] for a in f["agents"]], "message": "Check the handoff"})
        with patch("studio_agents.subprocess.run", return_value=types.SimpleNamespace(returncode=0, stdout="", stderr="")) as run, \
                patch.object(app.runtimes.local["claude-code"], "send", return_value=("started", "Process started")) as send:
            app.controls.send(preview["id"])
            self.wait_controls(app)
        self.assertEqual(run.call_args.args[0][1:3], ["queue", "--thread"])
        self.assertEqual(send.call_args.args[0], f["agents"][1]["sessionId"])
        receipt = app.controls.snapshot()[-1]
        self.assertEqual([t["status"] for t in receipt["targets"]], ["queued", "started"])
        with self.assertRaises(ValueError):
            app.controls.preview({"fleetId": f["id"], "action": "rename", "agentIds": [f["agents"][1]["id"]]})

    def test_runtimes_merge_backends_and_mark_unreachable_hosts_unknown(self):
        runtimes = Runtimes(self.root, codex_root=self.root / "no-codex")
        runtimes.local["claude-code"].root = self.root
        runtimes.configure([{"id": "gpu-box", "url": "https://gpu.example", "tokenEnv": "FLEET_TEST_MISSING_TOKEN"}])
        local, remote = str(uuid.uuid4()), str(uuid.uuid4())
        write_transcript(self.root, local, [assistant("Done")])
        with patch("studio_agents.claude_processes", return_value=[]):
            merged = runtimes.observe([{"sessionId": local, "backend": "claude-code"},
                                       {"sessionId": remote, "backend": "codex", "hostId": "gpu-box"}])
        self.assertEqual(merged["sessions"]["local:claude-code:" + local]["status"], "closed")
        self.assertEqual(merged["sessions"]["gpu-box:codex:" + remote]["status"], "unknown")
        self.assertEqual(merged["sessions"]["gpu-box:codex:" + remote]["hostId"], "gpu-box")
        self.assertTrue(any("gpu-box" in e for e in merged["errors"]))
        self.assertEqual([r["id"] for r in merged["catalogue"]], [local])

    def test_role_runtime_constraint_blocks_mismatched_deployment(self):
        app, f = self.app()
        state = app.store.snapshot()
        role_id = state["fleets"][0]["agents"][0]["roleIds"][0]
        role = next(r for r in state["fleets"][0]["roles"] if r["id"] == role_id)
        role["runtime"] = {"backends": ["claude-code"], "location": "any"}
        app.store.save(state)
        f = app.store.fleet(f["id"])
        self.assertEqual(len(runtime_problems(f)), 1)
        exported = export_spec(f)
        self.assertEqual(next(r for r in exported["roles"] if r["name"] == role["name"])["runtime"]["backends"], ["claude-code"])
        with self.assertRaises(ValueError) as refused:
            app.deployments.preview({"fleetId": f["id"], "revision": app.store.snapshot()["revision"], "kind": "deploy"})
        self.assertIn("requires claude-code", str(refused.exception))
        state = app.store.snapshot()
        state["fleets"][0]["agents"][0]["backend"] = "claude-code"
        app.store.save(state)
        self.assertEqual(runtime_problems(app.store.fleet(f["id"])), [])
        bad = {**exported, "roles": [{**exported["roles"][0], "runtime": {"backends": ["other"], "location": "any"}}] + exported["roles"][1:]}
        with self.assertRaises(ValueError):
            validate(bad)

    def test_backend_rebinding_disables_deployed_recipient(self):
        app, f = self.app()
        preview = app.deployments.preview({"fleetId": f["id"], "revision": app.store.snapshot()["revision"], "kind": "deploy"})
        app.deployments.apply(preview["id"])
        self.assertTrue(all(a["enabled"] for a in app.runtime_fleet(app.store.fleet(f["id"]))["agents"]))
        state = app.store.snapshot()
        state["fleets"][0]["agents"][1]["backend"] = "claude-code"
        app.store.save(state)
        runtime = app.runtime_fleet(app.store.fleet(f["id"]))
        self.assertEqual([a["enabled"] for a in runtime["agents"]], [True, False])


class FakeBackend(studio_agents.AgentBackend):
    name, label = "codex", "Fake"
    capabilities = {"queue": True, "rename": False, "goals": False, "activity": True, "tokens": False}

    def __init__(self):
        self.sent = []

    def catalogue(self):
        return [{"id": "s", "name": "Remote", "directory": "/srv"}]

    def observe(self, ids):
        return {"observedAt": 1, "sessions": {i: {"sessionId": i, "status": "idle", "pids": [9]} for i in ids}, "catalogue": [], "errors": []}

    def send(self, sid, text, directory=""):
        self.sent.append((sid, text))
        return "queued", "Fake queue accepted"


class NodeTests(unittest.TestCase):
    setUp = fixtures.DesignTests.setUp
    tearDown = fixtures.DesignTests.tearDown

    def test_studio_reaches_node_with_token_and_node_rejects_commands(self):
        node = Node(self.root)
        fake = node.backends["codex"] = FakeBackend()
        token = "t" * 32
        server = NodeServer(("127.0.0.1", 0), node, token)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            url = "http://127.0.0.1:" + str(server.server_port)
            runtimes = Runtimes(self.root, codex_root=self.root / "no-codex")
            runtimes.configure([{"id": "box", "url": url, "tokenEnv": "FLEET_TEST_NODE_TOKEN"}])
            sid = str(uuid.uuid4())
            with patch.dict(os.environ, {"FLEET_TEST_NODE_TOKEN": token}):
                merged = runtimes.observe([{"sessionId": sid, "hostId": "box"}])
                self.assertEqual(merged["sessions"]["box:codex:" + sid]["status"], "idle")
                backend = runtimes.backend("codex", "box")
                self.assertEqual(backend.send(sid, "Plain instruction")[0], "queued")
                with self.assertRaises(RuntimeError):
                    backend.send(sid, "/compact")
                runtimes.refresh_remote_catalogue()
                self.assertEqual(runtimes.remote_catalogue["box"][0]["hostId"], "box")
            self.assertEqual(fake.sent, [(sid, "Plain instruction")])
            request = Request(url + "/node/observe", data=b'{"backend":"codex","sessionIds":[]}',
                              headers={"Authorization": "Bearer wrong"}, method="POST")
            with self.assertRaises(HTTPError) as denied:
                urlopen(request)
            self.assertEqual(denied.exception.code, 401)
        finally:
            server.shutdown()
            server.server_close()


class TrackerTests(unittest.TestCase):
    def test_provider_detection_and_issue_prefixes(self):
        self.assertEqual(provider_of({"projectUrl": "https://github.com/o/r"}), "github")
        self.assertEqual(provider_of({"projectUrl": "https://gitlab.example/g/p"}), "gitlab")
        self.assertEqual(provider_of({"projectUrl": "https://git.corp/o/r", "provider": "github"}), "github")
        self.assertEqual(issue_prefix({"projectUrl": "https://github.com/o/r"}), "https://github.com/o/r/issues/")
        self.assertEqual(issue_prefix({"projectUrl": "https://gitlab.example/g/p"}), "https://gitlab.example/g/p/-/issues/")

    def test_github_issues_are_normalised_and_pull_requests_excluded(self):
        rows = [{"number": 1, "title": "Ready", "state": "open", "labels": [{"name": "AI Ready"}], "updated_at": "2026-10-13T00:00:00Z", "assignees": [{"login": "octo"}]},
                {"number": 2, "title": "A PR", "state": "open", "labels": [], "updated_at": "2026-10-13T00:00:00Z", "pull_request": {}},
                {"number": 3, "title": "Done", "state": "closed", "labels": [], "updated_at": "2026-10-13T00:00:00Z", "assignees": None}]
        issues, partial = fetch_github_issues({"projectUrl": "https://github.com/o/r", "scope": "all"}, lambda url, suffix: rows if "page=1&" in suffix else [])
        self.assertFalse(partial)
        self.assertEqual([(i["iid"], i["state"], i["stage"]) for i in issues], [(1, "opened", "ready"), (3, "closed", "closed")])
        self.assertEqual(issues[0]["url"], "https://github.com/o/r/issues/1")
        self.assertEqual(issues[0]["assignees"], ["octo"])

    def test_github_label_history_and_actions_runs_use_shared_shapes(self):
        events = [{"event": "labeled", "created_at": "t1", "label": {"name": "AI Claimed"}},
                  {"event": "assigned", "created_at": "t2"},
                  {"event": "unlabeled", "created_at": "t3", "label": {"name": "AI Ready"}}]
        runs = {"workflow_runs": [{"id": 5, "status": "completed", "conclusion": "failure", "head_branch": "main", "head_sha": "abc",
                                   "html_url": "https://github.com/o/r/actions/runs/5", "created_at": "2026-10-13T10:00:00Z",
                                   "run_started_at": "2026-10-13T10:00:30Z", "updated_at": "2026-10-13T10:05:30Z", "name": "CI"},
                                  {"id": 6, "status": "in_progress", "conclusion": None, "head_branch": "dev", "head_sha": "def",
                                   "created_at": "2026-10-13T11:00:00Z", "run_started_at": "2026-10-13T11:00:00Z", "updated_at": "2026-10-13T11:01:00Z"}]}
        with patch("studio_trackers.github_get", side_effect=[events, runs]):
            history = GitHubTracker().history({"projectUrl": "https://github.com/o/r"}, 1)
            pipelines, summary, notice = GitHubTracker().pipelines("https://github.com/o/r")
        self.assertEqual([(e["action"], e["label"]["name"]) for e in history], [("remove", "AI Ready"), ("add", "AI Claimed")])
        self.assertEqual((pipelines[0]["status"], pipelines[0]["duration"], pipelines[0]["queued_duration"]), ("failed", 300, 30))
        self.assertEqual((pipelines[1]["status"], pipelines[1]["duration"]), ("running", None))
        self.assertIsNone(summary)
        self.assertIn("test totals", notice)

    def test_gh_cli_failure_does_not_echo_output(self):
        with patch("studio_tracker_io.subprocess.run", return_value=types.SimpleNamespace(returncode=1, stdout="token=secret", stderr="secret")):
            with self.assertRaises(ValueError) as failure:
                studio_trackers.github_get("https://github.com/o/r", "issues")
        self.assertNotIn("secret", str(failure.exception))


if __name__ == "__main__":
    unittest.main()
