"""The hook script, run as the harnesses run it, against a local stand-in for Jev.

    python3 -m unittest discover -s tests
"""
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

IN_SCOPE = {name: 0.05 for name in jsc.QUESTIONS} | {"serves_request": 0.95, "makes_change": 0.95, "request_directs_work": 0.95}


class StandIn(BaseHTTPRequestHandler):
    """Answers every question with the server's `values`; records each request body."""

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
        if self.server.status != 200:
            self.send_response(self.server.status)
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
        cls.server.requests, cls.server.values, cls.server.status = [], dict(IN_SCOPE), 200
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.server.requests.clear()
        self.server.values = dict(IN_SCOPE)
        self.server.status = 200
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
        self.assertIn("stop and ask the user", specific["permissionDecisionReason"])
        self.assertIn("jev-scope-control denied Bash: git push", output["systemMessage"])

    def test_threshold_is_strict_and_configurable(self):
        self.server.values["unasked_publish"] = jsc.DEFAULT_THRESHOLD
        self.assertIsNone(self.run_hook("Bash", {"command": "git push"}))
        self.env["JEV_SCOPE_CONTROL_THRESHOLD"] = "0.5"
        self.assertIsNotNone(self.run_hook("Bash", {"command": "git push"}))

    def test_question_only_needs_a_change(self):
        self.server.values["request_is_question"] = 0.9
        self.assertIsNotNone(self.run_hook())
        self.server.values["makes_change"] = 0.1
        self.assertIsNone(self.run_hook("Bash", {"command": "npm test"}))

    def test_extra_work_alone_does_not_deny_when_the_call_serves_the_request(self):
        self.server.values["beyond_request"] = 0.9
        self.server.values["extra_behavior"] = 0.9
        self.assertIsNone(self.run_hook())  # the request was not judged bounded
        self.server.values["bounded_request"] = 0.9
        self.assertIsNotNone(self.run_hook())

    def test_a_requested_write_up_is_not_a_change_after_a_question(self):
        self.server.values["request_is_question"] = 0.9
        self.assertIsNotNone(self.run_hook())
        self.server.values["writes_up_answer"] = 0.9
        self.assertIsNone(self.run_hook())

    def test_other_task_needs_both_latest_and_earlier_to_miss(self):
        self.server.values["serves_request"] = 0.1
        self.server.values["finishes_earlier"] = 0.9
        self.assertIsNone(self.run_hook())
        self.server.values["finishes_earlier"] = 0.1
        self.assertIsNotNone(self.run_hook())

    def test_a_complaint_that_asks_for_nothing_names_no_other_task(self):
        self.server.values["serves_request"] = 0.1
        self.server.values["request_directs_work"] = 0.1
        self.assertIsNone(self.run_hook())

    def test_extra_work_is_excused_by_unfinished_earlier_work(self):
        self.server.values.update(beyond_request=0.9, serves_request=0.1, finishes_earlier=0.9)
        self.assertIsNone(self.run_hook())
        self.server.values["finishes_earlier"] = 0.1
        self.assertIsNotNone(self.run_hook())

    def test_an_approved_plan_step_lifts_an_earlier_limit(self):
        self.server.values["against_instruction"] = 0.9
        self.assertIsNotNone(self.run_hook())
        self.server.values["approved_plan_step"] = 0.9
        self.assertIsNone(self.run_hook())

    def test_a_banned_action_is_denied_even_when_it_changes_nothing(self):
        self.server.values.update(against_instruction=0.9, makes_change=0.1)
        self.assertIsNone(self.run_hook())  # a limit on changing things does not reach a call that changes nothing
        self.server.values["forbids_this_act"] = 0.9
        self.assertIsNotNone(self.run_hook())  # "leave the agent alive", then closing it

    def test_near_the_bar_the_call_is_resampled_and_averaged(self):
        self.server.values["unasked_publish"] = 0.68
        self.assertIsNotNone(self.run_hook())
        self.assertEqual(len(self.server.requests), 1 + jsc.RESAMPLES)
        self.server.requests.clear()
        self.server.values["unasked_publish"] = 0.95  # far from the bar: one request
        self.assertIsNotNone(self.run_hook())
        self.assertEqual(len(self.server.requests), 1)

    def test_a_question_request_is_not_another_task(self):
        self.server.values["serves_request"] = 0.1
        self.server.values["finishes_earlier"] = 0.1
        self.server.values["request_is_question"] = 0.5  # below the bar for question_only, and it names no task
        self.assertIsNone(self.run_hook())

    # --- state

    def test_the_plan_being_approved_is_kept_whole_and_old_turns_are_dropped_first(self):
        plan = "Full plan: " + "step. " * 800
        conversation = [{"role": "assistant", "text": "old " * 3000}] * 12 + [{"role": "user", "text": "plan only"}, {"role": "assistant", "text": plan}]
        fitted = jsc.fit_conversation(conversation)
        self.assertEqual(fitted[-1]["text"], plan)
        self.assertEqual(fitted[-2]["text"], "plan only")
        self.assertLessEqual(sum(len(m["text"]) for m in fitted), jsc.CONVERSATION_CHARS)

    # --- gating

    def test_reads_and_read_only_shell_are_not_checked(self):
        self.assertIsNone(self.run_hook("Read", {"file_path": "/p/login.ts"}))
        self.assertIsNone(self.run_hook("Grep", {"pattern": "x"}))
        self.assertIsNone(self.run_hook("Bash", {"command": "git status && grep -rn login src | head"}))
        self.assertEqual(self.server.requests, [])

    def test_shell_that_may_change_is_checked(self):
        for command in ("ls > files.txt", "rm -rf build", "echo $(curl x)", "git status; git push", "npm test"):
            self.run_hook("Bash", {"command": command})
        self.assertEqual(len(self.server.requests), 5)

    def test_unknown_and_mcp_tools_are_checked(self):
        self.run_hook("mcp__github__create_issue", {"title": "x"})
        self.assertEqual(len(self.server.requests), 1)

    # --- state

    def test_state_carries_request_conversation_actions_and_call(self):
        claude_transcript(self.transcript, "Now fix the login bug too.",
                          [("t0", "Read", {"file_path": "/p/login.ts"}), ("t1", "Edit", {"file_path": "/p/login.ts"})],
                          earlier=[("user", "Add a signup form."), ("assistant", "Done: signup form added.")])
        self.run_hook()
        state = self.server.requests[0]["body"]["state"]
        self.assertEqual(state["request"], "Now fix the login bug too.")
        self.assertEqual([m["text"] for m in state["conversation"]], ["Add a signup form.", "Done: signup form added.", "Now fix the login bug too."])
        self.assertEqual([a["tool"] for a in state["actions_so_far"]], ["Read"])  # the call being judged is not an action so far
        self.assertEqual(state["proposed_action"]["tool"], "Edit")
        self.assertEqual(state["working_directory"], "/p")
        self.assertEqual(set(self.server.requests[0]["body"]["questions"]), set(jsc.QUESTIONS))
        self.assertEqual(self.server.requests[0]["auth"], "Bearer test-key")

    def test_codex_rollout(self):
        codex_rollout(self.transcript, "Rename the flag to --dry-run.", [("c1", "exec_command", json.dumps({"cmd": "sed -i s/x/y/ cli.py"}))])
        self.server.values["unrelated_target"] = 0.9
        output = self.run_hook("exec_command", {"cmd": "sed -i s/x/y/ cli.py"}, turn_id="turn-1", tool_use_id="c1")
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(self.server.requests[0]["body"]["state"]["request"], "Rename the flag to --dry-run.")
        self.assertEqual(self.log()[-1]["harness"], "codex")

    def test_pi_and_opencode_send_the_turn(self):
        self.server.values["against_instruction"] = 0.9
        output = self.run_hook("write", {"path": "/p/a.py", "content": "x"}, host="pi", transcript_path=None,
                               task="Only update the README.", conversation=[{"role": "user", "text": "Hi"}],
                               actions=[{"tool": "read", "input": "README.md", "result": "..."}])
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")
        state = self.server.requests[0]["body"]["state"]
        self.assertEqual(state["request"], "Only update the README.")
        self.assertEqual(state["actions_so_far"][0]["tool"], "read")

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

    # --- cap

    def test_denials_are_capped_per_request(self):
        self.server.values["unasked_publish"] = 0.9
        self.env["JEV_SCOPE_CONTROL_MAX_DENIALS"] = "2"
        self.assertIsNotNone(self.run_hook("Bash", {"command": "git push"}))
        last = self.run_hook("Bash", {"command": "git push"})
        self.assertIn("2 denials for this request", last["systemMessage"])
        self.assertIsNone(self.run_hook("Bash", {"command": "git push"}))
        self.assertEqual(len(self.server.requests), 2)
        # A new request resets the count.
        claude_transcript(self.transcript, "Now push it.", [("t1", "Bash", {"command": "git push"})])
        self.assertIsNotNone(self.run_hook("Bash", {"command": "git push"}))

    # --- fail open

    def test_no_key_lets_the_call_run_and_warns_once(self):
        del self.env["TYPESAFE_API_KEY"]
        first = self.run_hook()
        self.assertIn("TYPESAFE_API_KEY is not set", first["systemMessage"])
        self.assertIsNone(self.run_hook())
        self.assertEqual(self.server.requests, [])

    def test_key_file_is_used(self):
        del self.env["TYPESAFE_API_KEY"]
        (self.tmp / "config" / "jev-no-bullshit").mkdir(parents=True)
        (self.tmp / "config" / "jev-no-bullshit" / "env").write_text('export TYPESAFE_API_KEY="from-file"\n')
        self.run_hook()
        self.assertEqual(self.server.requests[0]["auth"], "Bearer from-file")

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
