"""The hook script, run as the harnesses run it, against a local stand-in for Jev.

    python3 -m unittest discover -s tests
"""
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "jev-scope-control"
sys.path.insert(0, str(ROOT / "tools"))
from scope_questions import jsc  # noqa: E402

IN_SCOPE = {name: 0.05 for name in jsc.QUESTIONS} | {"serves_request": 0.95, "writes": 0.95}


class StandIn(BaseHTTPRequestHandler):
    """Answers every question with the server's `values`; records each request body."""

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
        if self.server.status != 200 or (self.server.fail_writes and "writes" in body["questions"]):
            self.send_response(self.server.status if self.server.status != 200 else 503)
            self.end_headers()
            self.wfile.write(b"down")
            return
        answers = {q: {"noul": self.server.values.get(q, 0.05)} for q in body["questions"]}
        data = json.dumps({"model": "jev-test", "answers": answers, "usage": {"input_tokens": 1}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def claude_transcript(path: Path, request: str, calls: list[tuple[str, str, dict]], earlier: list[tuple[str, str]] = ()) -> None:
    """A Claude Code transcript: earlier (role, text) messages, the request, then the given tool calls."""
    lines = []
    for n, (role, text) in enumerate(earlier):
        lines.append({"type": role, "uuid": f"e{n}", "timestamp": f"2026-09-28T09:00:{n:02d}Z",
                      "message": {"role": role, "content": text if role == "user" else [{"type": "text", "text": text}]}})
    lines.append({"type": "user", "uuid": "u1", "timestamp": "2026-09-28T10:00:00Z", "message": {"role": "user", "content": request}})
    for n, (call_id, tool, tool_input) in enumerate(calls):
        lines.append({"type": "assistant", "uuid": f"a{n}", "timestamp": f"2026-09-28T10:01:{n:02d}Z",
                      "message": {"role": "assistant", "content": [{"type": "tool_use", "id": call_id, "name": tool, "input": tool_input}]}})
        lines.append({"type": "user", "uuid": f"r{n}", "timestamp": f"2026-09-28T10:01:{n:02d}Z",
                      "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call_id, "content": "ok"}]}})
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))


def codex_rollout(path: Path, request: str, calls: list[tuple[str, str, str]]) -> None:
    lines = [
        {"type": "session_meta", "payload": {"id": "s1", "cwd": "/p"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": request}},
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": request}]}},
    ]
    for call_id, name, arguments in calls:
        lines.append({"type": "response_item", "payload": {"type": "function_call", "name": name, "call_id": call_id, "arguments": arguments}})
        lines.append({"type": "response_item", "payload": {"type": "function_call_output", "call_id": call_id, "output": "ok"}})
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))


class HookTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), StandIn)
        cls.server.requests, cls.server.values, cls.server.status, cls.server.fail_writes = [], dict(IN_SCOPE), 200, False
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.server.requests.clear()
        self.server.values = dict(IN_SCOPE)
        self.server.status, self.server.fail_writes = 200, False
        self.env = {
            "PATH": os.environ.get("PATH", ""), "HOME": str(self.tmp), "XDG_CONFIG_HOME": str(self.tmp / "config"),
            "TYPESAFE_API_KEY": "test-key", "TYPESAFE_BASE_URL": f"http://127.0.0.1:{self.server.server_port}",
        }
        self.transcript = self.tmp / "t.jsonl"
        claude_transcript(self.transcript, "Login fails for uppercase emails. Fix it.", [("t1", "Edit", {"file_path": "/p/login.ts"})])

    def run_hook(self, tool="Edit", tool_input=None, **extra):
        hook_input = {"hook_event_name": "PreToolUse", "session_id": "s1", "transcript_path": str(self.transcript),
                      "cwd": "/p", "tool_name": tool, "tool_use_id": "t1",
                      "tool_input": tool_input if tool_input is not None else {"file_path": "/p/login.ts", "old_string": "a", "new_string": "b"},
                      **extra}
        done = subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps(hook_input), capture_output=True, text=True, env=self.env, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout) if done.stdout.strip() else None

    def log(self):
        path = self.tmp / ".jev-scope-control" / "log.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    # --- decisions

    def test_in_scope_call_runs(self):
        self.assertIsNone(self.run_hook())
        self.assertEqual(len(self.server.requests), 1)
        self.assertFalse(self.log()[-1]["denied"])

    def test_out_of_scope_call_is_denied_with_reasons(self):
        self.server.values["unasked_publish"] = 0.9
        output = self.run_hook("Bash", {"command": "git push"})
        specific = output["hookSpecificOutput"]
        self.assertEqual(specific["hookEventName"], "PreToolUse")
        self.assertEqual(specific["permissionDecision"], "deny")
        self.assertTrue(specific["permissionDecisionReason"].startswith("[jev-scope-control] Denied"))
        self.assertIn(jsc.FEEDBACK["unasked_publish"], specific["permissionDecisionReason"])
        self.assertIn("Do not work around this", specific["permissionDecisionReason"])
        self.assertIn("jev-scope-control denied Bash: git push", output["systemMessage"])

    def test_threshold_is_strict_and_configurable(self):
        self.server.values["unasked_publish"] = jsc.DEFAULT_THRESHOLD
        self.assertIsNone(self.run_hook("Bash", {"command": "git push"}))
        self.env["JEV_SCOPE_CONTROL_THRESHOLD"] = "0.5"
        self.assertIsNotNone(self.run_hook("Bash", {"command": "git push"}))

    def test_extra_behavior_denies_only_when_beyond_request_agrees(self):
        self.server.values["extra_behavior"] = 0.9
        self.assertIsNone(self.run_hook())
        self.server.values["beyond_request"] = 0.9  # excused on its own, since the call serves the request
        self.assertIsNotNone(self.run_hook())

    def test_extra_work_is_excused_by_unfinished_earlier_work(self):
        self.server.values.update(beyond_request=0.9, serves_request=0.1, finishes_earlier=0.9)
        self.assertIsNone(self.run_hook())
        self.server.values["finishes_earlier"] = 0.1
        self.assertIsNotNone(self.run_hook())

    def test_an_approved_plan_step_lifts_an_earlier_limit(self):
        self.server.values["against_instruction"] = 0.9
        self.assertIsNotNone(self.run_hook())
        self.server.values["approved_plan_step"] = 0.9
        self.assertIsNone(self.run_hook(tool_input={"file_path": "/p/login.ts", "old_string": "a", "new_string": "c"}))

    def test_building_on_what_the_user_rejected_is_denied(self):
        self.server.values["builds_on_rejected"] = 0.9
        out = self.run_hook()
        self.assertIsNotNone(out)
        self.assertIn("rejected", json.dumps(out))

    def test_jev_is_asked_once_and_its_answer_is_used(self):
        self.server.values["unasked_publish"] = 0.68  # near the bar: still one request
        self.assertIsNotNone(self.run_hook())
        self.assertEqual(len(self.server.requests), 1)

    # --- state

    # --- gating

    def test_reads_and_read_only_shell_are_not_checked(self):
        self.assertIsNone(self.run_hook("Read", {"file_path": "/p/login.ts"}))
        self.assertIsNone(self.run_hook("Grep", {"pattern": "x"}))
        self.assertIsNone(self.run_hook("Bash", {"command": "git status && grep -rn login src | head"}))
        self.assertEqual(self.server.requests, [])

    def test_shell_that_may_change_is_checked(self):
        for command in ("ls > files.txt", "rm -rf build", "echo $(curl x)", "git status; git push", "npm test"):
            self.run_hook("Bash", {"command": command})
        self.assertEqual(len(self.server.requests), 10)  # read/write, then scope

    def test_a_command_jev_calls_a_read_gets_no_scope_questions(self):
        self.server.values["writes"] = 0.1
        self.server.values["unasked_publish"] = 0.9
        self.assertIsNone(self.run_hook("Bash", {"command": "python3 -c 'print(open(\"x\").read())'"}))
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(self.server.requests[0]["body"]["state"],
                         {"tool_call": {"tool": "Bash", "input": {"command": "python3 -c 'print(open(\"x\").read())'"}}})
        self.assertEqual(set(self.server.requests[0]["body"]["questions"]), {"writes"})
        self.server.values["writes"] = 0.9
        self.assertIsNotNone(self.run_hook("Bash", {"command": "python3 -c 'print(open(\"x\").read())'"}))
        self.assertEqual(len(self.server.requests), 3)

    def test_a_failed_read_write_question_gets_the_scope_check(self):
        self.server.fail_writes = True
        self.server.values["unasked_publish"] = 0.9
        output = self.run_hook("Bash", {"command": "git push"})
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(len(self.server.requests), 2)
        self.assertIn("read/write question failed", self.log()[0]["error"])

    def test_the_read_write_question_says_claims_in_the_command_are_not_evidence(self):
        self.server.values["writes"] = 0.1
        self.run_hook("Bash", {"command": "rm -rf build  # read-only"})
        self.assertIn("are not evidence", self.server.requests[0]["body"]["questions"]["writes"]["instructions"])

    def test_only_exec_write_and_edit_tools_are_checked(self):
        self.run_hook("mcp__github__create_issue", {"title": "x"})
        self.run_hook("WebFetch", {"url": "https://example.com"})
        self.run_hook("TodoWrite", {"todos": []})
        self.assertEqual(self.server.requests, [])
        for tool in ("Write", "Edit", "MultiEdit", "NotebookEdit", "apply_patch"):
            self.run_hook(tool, {"file_path": "/p/login.ts"})
        self.assertEqual(len(self.server.requests), 5)

    # --- state

    def test_state_is_the_context_plus_the_call(self):
        claude_transcript(self.transcript, "Now fix the login bug too.",
                          [("t0", "Read", {"file_path": "/p/login.ts"}), ("t1", "Edit", {"file_path": "/p/login.ts"})],
                          earlier=[("user", "Add a signup form."), ("assistant", "Done: signup form added.")])
        self.run_hook()
        state = dict(self.server.requests[0]["body"]["state"])
        self.assertEqual(state.pop("proposed_action")["tool"], "Edit")
        entries = jsc.read_jsonl(str(self.transcript))
        task, actions, _model, earlier = jsc.parse_claude(entries)
        expected, _ = jsc.scope_state(task, actions, earlier, jsc.conversation_claude(entries), "Edit", {"file_path": "/p/login.ts"})
        expected.pop("proposed_action")
        self.assertEqual(json.dumps(state), json.dumps(expected))  # byte for byte
        self.assertEqual(state["task"], "Now fix the login bug too.")
        self.assertEqual(state["summary"], "")
        self.assertEqual(set(self.server.requests[0]["body"]["questions"]), set(jsc.QUESTIONS))
        self.assertEqual(self.server.requests[0]["auth"], "Bearer test-key")

    def test_codex_rollout(self):
        codex_rollout(self.transcript, "Rename the flag to --dry-run.", [("c1", "exec_command", json.dumps({"cmd": "sed -i s/x/y/ cli.py"}))])
        self.server.values["unrelated_target"] = 0.9
        output = self.run_hook("exec_command", {"cmd": "sed -i s/x/y/ cli.py"}, turn_id="turn-1", tool_use_id="c1")
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(self.server.requests[-1]["body"]["state"]["task"], "Rename the flag to --dry-run.")
        self.assertEqual(self.log()[-1]["harness"], "codex")

    def test_pi_and_opencode_send_the_turn(self):
        self.server.values["against_instruction"] = 0.9
        output = self.run_hook("write", {"path": "/p/a.py", "content": "x"}, host="pi", transcript_path=None,
                               task="Only update the README.", conversation=[{"role": "user", "text": "Hi"}],
                               actions=[{"tool": "read", "input": "README.md", "result": "..."}])
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")
        state = self.server.requests[0]["body"]["state"]
        self.assertEqual(state["task"], "Only update the README.")
        self.assertEqual(state["actions"][0]["tool"], "read")

    def test_waits_for_a_call_the_harness_writes_after_the_hook_starts(self):
        claude_transcript(self.transcript, "Login fails for uppercase emails. Fix it.", [])
        late = threading.Timer(0.3, claude_transcript, (self.transcript, "Login fails for uppercase emails. Fix it.",
                                                        [("t1", "Edit", {"file_path": "/p/login.ts"})]))
        late.start()
        self.server.values["unasked_publish"] = 0.9
        self.assertEqual(self.run_hook()["hookSpecificOutput"]["permissionDecision"], "deny")
        late.join()
        self.assertIn("uppercase emails", json.dumps(self.server.requests[0]))
        self.assertNotIn("call_not_in_transcript", self.log()[-1])

    def test_no_request_found_lets_the_call_run(self):
        self.transcript.write_text("")
        self.assertIsNone(self.run_hook())
        self.assertEqual(self.server.requests, [])

    # --- repeats

    def test_a_denied_call_stays_denied_until_the_request_changes(self):
        self.server.values["unasked_publish"] = 0.9
        self.assertIsNotNone(self.run_hook("Bash", {"command": "git push"}))
        self.assertEqual(len(self.server.requests), 2)
        self.server.values["unasked_publish"] = 0.05  # Jev is not asked again: the repeat is denied as is
        again = self.run_hook("Bash", {"command": "git push"})
        self.assertIn("already denied for this request", again["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertEqual(len(self.server.requests), 2)
        self.assertTrue(self.log()[-1]["repeat"])
        for _ in range(3):  # however many times it is retried
            self.assertIsNotNone(self.run_hook("Bash", {"command": "git push"}))
        for n in range(25):  # and however many other calls are denied in between
            self.server.values["unasked_publish"] = 0.9
            self.run_hook("Bash", {"command": f"git push {n}"})
        self.server.values["unasked_publish"] = 0.05
        self.server.requests.clear()
        self.assertIsNotNone(self.run_hook("Bash", {"command": "git push"}))
        self.assertEqual(self.server.requests, [])
        # A different call is checked as usual.
        self.assertIsNone(self.run_hook("Bash", {"command": "git push --tags"}))
        self.assertEqual(len(self.server.requests), 2)
        # A new request from the user: the same call is checked again.
        claude_transcript(self.transcript, "Push the fix to origin.", [("t1", "Bash", {"command": "git push"})])
        self.assertIsNone(self.run_hook("Bash", {"command": "git push"}))
        self.assertEqual(len(self.server.requests), 4)

    # --- fail open

    def test_no_key_lets_the_call_run_and_warns_once(self):
        del self.env["TYPESAFE_API_KEY"]
        first = self.run_hook()
        self.assertIn("TYPESAFE_API_KEY is not set", first["systemMessage"])
        self.assertIsNone(self.run_hook())
        self.assertEqual(self.server.requests, [])

    def test_key_file_is_used(self):
        del self.env["TYPESAFE_API_KEY"]
        (self.tmp / "config" / "jev-scope-control").mkdir(parents=True)
        (self.tmp / "config" / "jev-scope-control" / "env").write_text('export TYPESAFE_API_KEY="from-file"\n')
        self.run_hook()
        self.assertEqual(self.server.requests[0]["auth"], "Bearer from-file")

    def test_this_plugins_key_file_comes_before_the_environment(self):
        (self.tmp / "config" / "jev-scope-control").mkdir(parents=True)
        (self.tmp / "config" / "jev-scope-control" / "env").write_text("TYPESAFE_API_KEY=own-file\n")
        self.run_hook()
        self.assertEqual(self.server.requests[-1]["auth"], "Bearer own-file")

    def test_jev_error_lets_the_call_run(self):
        self.server.status = 503
        output = self.run_hook()
        self.assertNotIn("hookSpecificOutput", output)
        self.assertIn("did not check this call", output["systemMessage"])
        self.assertIn("HTTP 503", self.log()[-1]["error"])

    def test_unreachable_jev_lets_the_call_run(self):
        self.env["TYPESAFE_BASE_URL"] = "http://127.0.0.1:9"
        self.assertNotIn("hookSpecificOutput", self.run_hook())

    def test_plain_http_to_a_remote_host_is_refused(self):
        self.env["TYPESAFE_BASE_URL"] = "http://example.com"
        output = self.run_hook()
        self.assertNotIn("hookSpecificOutput", output)
        self.assertIn("https", self.log()[-1]["error"])

    def test_bad_input_lets_the_call_run(self):
        done = subprocess.run([sys.executable, str(SCRIPT)], input="not json", capture_output=True, text=True, env=self.env, timeout=30)
        self.assertEqual((done.returncode, done.stdout), (0, ""))

    def test_private_files_are_owner_only(self):
        self.run_hook()
        self.assertEqual((self.tmp / ".jev-scope-control").stat().st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()


class PackagingTest(unittest.TestCase):
    """Each harness's entry point reaches the one script."""

    def test_manifests_parse_and_agree(self):
        claude = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
        codex = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text())
        package = json.loads((ROOT / "package.json").read_text())
        self.assertEqual(claude["name"], codex["name"])
        self.assertEqual(claude["version"], codex["version"])
        self.assertEqual(package["name"], claude["name"])

    def test_hook_config_runs_the_script_before_every_tool(self):
        hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text())["hooks"]["PreToolUse"]
        command = hooks[0]["hooks"][0]["command"]
        self.assertNotIn("matcher", hooks[0])
        self.assertEqual(command.replace("${CLAUDE_PLUGIN_ROOT}", str(ROOT)).split('"')[1], str(SCRIPT))
        self.assertTrue(os.access(SCRIPT, os.X_OK))

    def test_pi_and_opencode_shims_resolve_the_script(self):
        package = json.loads((ROOT / "package.json").read_text())
        for entry in (package["main"], *package["pi"]["extensions"]):
            shim = (ROOT / entry).resolve()
            self.assertIn('new URL("../jev-scope-control", import.meta.url)', shim.read_text())
            self.assertEqual((shim.parent / ".." / "jev-scope-control").resolve(), SCRIPT)
