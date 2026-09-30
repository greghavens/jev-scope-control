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
        answers = {q: {"noul": self.server.values.get(q, 0.05)} for q in body["questions"] if q != "kind"}
        if "kind" in body["questions"]:  # the gate: unsure unless a test says otherwise, so the full check runs
            answers["kind"] = {"type": "choice", "probabilities": self.server.values.get("kind", {"asked": 0.5, "extra": 0.5})}
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

    def forget(self):
        """Drop the short cache of verdicts, as if the repeat came more than CACHE_SECONDS later."""
        for path in (self.tmp / ".jev-scope-control" / "cache").glob("*"):
            path.unlink()

    def log(self):
        path = self.tmp / ".jev-scope-control" / "log.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    # --- decisions

    def test_in_scope_call_runs(self):
        self.assertIsNone(self.run_hook())
        self.assertEqual(len(self.server.requests), 2)  # the gate is unsure, so the full check runs
        self.assertFalse(self.log()[-1]["denied"])

    def test_the_gate_alone_allows_a_call_it_calls_asked(self):
        self.server.values["kind"] = {"asked": 0.9, "extra": 0.1}
        self.server.values["unasked_publish"] = 0.9  # never asked
        self.assertIsNone(self.run_hook())
        self.assertEqual(len(self.server.requests), 1)
        body = self.server.requests[0]["body"]
        self.assertEqual(set(body["questions"]), {"kind"})
        self.assertEqual(body["state"]["task"], "Login fails for uppercase emails. Fix it.")
        self.assertEqual(body["state"]["proposed_action"]["tool"], "Edit")
        self.assertEqual(self.log()[-1]["stage"], "gate")

    def test_a_gate_risk_over_the_bar_gets_the_full_check(self):
        self.server.values["kind"] = {"asked": 0.6, "publish": 0.4}
        self.server.values["unasked_publish"] = 0.9
        self.assertIsNotNone(self.run_hook("Bash", {"command": "git push"}))
        self.assertEqual([set(r["body"]["questions"]) for r in self.server.requests], [{"kind"}, set(jsc.QUESTIONS)])
        self.assertEqual(self.log()[-1]["stage"], "full")

    def test_the_gate_sees_limits_the_user_set_long_ago(self):
        earlier = [("user", "Never edit anything under migrations/."), ("assistant", "Understood.")]
        for n in range(10):
            earlier += [("user", f"Now do step {n}."), ("assistant", f"Did step {n}.")]
        claude_transcript(self.transcript, "Fix the login bug.", [("t1", "Edit", {"file_path": "/p/login.ts"})], earlier=earlier)
        self.run_hook()
        state = self.server.requests[0]["body"]["state"]
        self.assertEqual(state["older_user_limits"], ["Never edit anything under migrations/."])
        self.assertEqual(len(state["earlier_user_messages"]), jsc.GATE_USER_MESSAGES)
        self.assertLess(len(json.dumps(state)), 6_000)

    def test_the_same_call_again_is_not_asked_again(self):
        self.assertIsNone(self.run_hook())
        self.assertEqual(len(self.server.requests), 2)
        self.server.values["unasked_publish"] = 0.9
        self.assertIsNone(self.run_hook())
        self.assertEqual(len(self.server.requests), 2)
        self.assertTrue(self.log()[-1]["cached"])
        self.forget()
        self.assertIsNotNone(self.run_hook())

    def test_out_of_scope_call_is_denied_with_reasons(self):
        self.server.values["unasked_publish"] = 0.9
        output = self.run_hook("Bash", {"command": "git push"})
        specific = output["hookSpecificOutput"]
        self.assertEqual(specific["hookEventName"], "PreToolUse")
        self.assertEqual(specific["permissionDecision"], "deny")
        self.assertTrue(specific["permissionDecisionReason"].startswith("[jev-scope-control] Denied"))
        self.assertIn(jsc.FEEDBACK["unasked_publish"], specific["permissionDecisionReason"])
        self.assertIn("tell the user plainly what you were trying to do", specific["permissionDecisionReason"])
        self.assertIn("Do not retry it or work around it", specific["permissionDecisionReason"])
        self.assertIn("jev-scope-control denied Bash: git push", output["systemMessage"])

    def test_threshold_is_strict_and_configurable(self):
        self.server.values["unasked_publish"] = jsc.DEFAULT_THRESHOLD
        self.assertIsNone(self.run_hook("Bash", {"command": "git push"}))
        self.forget()
        self.env["JEV_SCOPE_CONTROL_THRESHOLD"] = "0.5"
        self.assertIsNotNone(self.run_hook("Bash", {"command": "git push"}))

    def test_extra_behavior_denies_only_when_beyond_request_agrees(self):
        self.server.values["extra_behavior"] = 0.9
        self.assertIsNone(self.run_hook())
        self.forget()
        self.server.values["beyond_request"] = 0.9  # excused on its own, since the call serves the request
        self.assertIsNotNone(self.run_hook())

    def test_extra_work_is_excused_by_unfinished_earlier_work(self):
        self.server.values.update(beyond_request=0.9, serves_request=0.1, finishes_earlier=0.9)
        self.assertIsNone(self.run_hook())
        self.forget()
        self.server.values["finishes_earlier"] = 0.1
        self.assertIsNotNone(self.run_hook())

    def test_jev_is_asked_once_and_its_answer_is_used(self):
        self.server.values["unasked_publish"] = 0.68  # near the bar: still one full check
        self.assertIsNotNone(self.run_hook())
        self.assertEqual([set(r["body"]["questions"]) for r in self.server.requests], [{"kind"}, set(jsc.QUESTIONS)])

    # --- state

    # --- gating

    def test_reads_and_read_only_shell_are_not_checked(self):
        self.assertIsNone(self.run_hook("Read", {"file_path": "/p/login.ts"}))
        self.assertIsNone(self.run_hook("Grep", {"pattern": "x"}))
        self.assertIsNone(self.run_hook("Bash", {"command": "git status && grep -rn login src | head"}))
        self.assertIsNone(self.run_hook("Bash", {"command": "git status\nfor f in *.py; do wc -l $f; done\ncat <<'EOF'\nnotes\nEOF"}))
        self.assertEqual(self.server.requests, [])

    def test_a_write_on_a_later_line_is_checked(self):
        self.run_hook("Bash", {"command": "git status\ngit push"})
        self.assertTrue(self.server.requests)

    def test_shell_that_may_change_is_checked(self):
        for command in ("ls > files.txt", "rm -rf build", "echo $(curl x)", "git status; git push", "npm test"):
            self.run_hook("Bash", {"command": command})
        asked = [set(r["body"]["questions"]) for r in self.server.requests]
        # Plain writes skip the read/write question; every command then gets the gate and, unsure, the full check.
        self.assertEqual(asked.count({"writes"}), 2)
        self.assertEqual(asked.count({"kind"}), 5)
        self.assertEqual(asked.count(set(jsc.QUESTIONS)), 5)

    def test_a_plain_write_skips_the_read_write_question(self):
        for command in ("rm -rf build", "git commit -am x", "sed -i s/a/b/ f", "echo hi > out.txt"):
            self.run_hook("Bash", {"command": command})
        self.assertNotIn({"writes"}, [set(r["body"]["questions"]) for r in self.server.requests])

    def test_a_command_jev_calls_a_read_gets_no_scope_questions(self):
        self.server.values["writes"] = 0.1
        self.server.values["unasked_publish"] = 0.9
        self.assertIsNone(self.run_hook("Bash", {"command": "python3 -c 'print(open(\"x\").read())'"}))
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(self.server.requests[0]["body"]["state"],
                         {"tool_call": {"tool": "Bash", "input": {"command": "python3 -c 'print(open(\"x\").read())'"}}})
        self.assertEqual(set(self.server.requests[0]["body"]["questions"]), {"writes"})
        self.forget()
        self.server.values["writes"] = 0.9
        self.assertIsNotNone(self.run_hook("Bash", {"command": "python3 -c 'print(open(\"x\").read())'"}))
        self.assertEqual(len(self.server.requests), 4)

    def test_the_request_and_the_reply_before_it_reach_jev(self):
        # The current turn has only a tool call, no text yet. The reply before the request and the request
        # itself were dropped, so "continue" reached Jev with nothing to continue.
        claude_transcript(self.transcript, "continue", [("t1", "Bash", {"command": "rm build.log"})], earlier=[
            ("user", "Remove the unverified_same_state question."),
            ("assistant", "Removed the unverified_same_state question; next I delete the build log."),
        ])
        self.run_hook("Bash", {"command": "rm build.log"})
        sent = [m["text"] for m in self.server.requests[-1]["body"]["state"]["conversation"]]
        self.assertIn("Removed the unverified_same_state question; next I delete the build log.", sent)
        self.assertEqual(sent[-1], "continue")

    def test_another_plugins_note_is_not_taken_as_the_request(self):
        note = "The jev-no-bullshit plugin sent a message:\n[jev-no-bullshit] Double-check these before you finish:\n- Unverified claim: \"It passes.\" Check this."
        claude_transcript(self.transcript, "Fix the login bug.", [("t1", "Edit", {"file_path": "/p/login.ts", "old_string": "a", "new_string": "b"})],
                          earlier=[("user", "Rename the helper."), ("assistant", "Renamed it. It passes.")])
        lines = self.transcript.read_text().splitlines()
        lines.append(json.dumps({"type": "user", "uuid": "n1", "timestamp": "2026-09-28T10:00:30Z", "message": {"role": "user", "content": note}}))
        self.transcript.write_text("\n".join(lines) + "\n")
        self.run_hook()
        state = self.server.requests[-1]["body"]["state"]
        self.assertEqual(state["task"], "Fix the login bug.")
        notes = [m for m in state["conversation"] if "Double-check" in m["text"] or "double-check" in m["text"]]
        self.assertTrue(notes and all(m["role"] == "hook" for m in notes), state["conversation"])

    def test_a_subagent_launch_is_checked_with_its_assignment(self):
        prompt = "Fix the off-by-one in src/pagination.py and run the tests."
        claude_transcript(self.transcript, "Fix the pagination bug.", [("t1", "Agent", {"description": "Fix pagination", "prompt": prompt})])
        self.run_hook("Agent", {"description": "Fix pagination", "prompt": prompt, "subagent_type": "general-purpose"})
        state = self.server.requests[-1]["body"]["state"]
        self.assertEqual(state["proposed_action"]["tool"], "Agent")
        self.assertEqual(state["proposed_action"]["input"]["prompt"], prompt)
        self.assertIn("subagents", state["proposed_action"]["note"])
        self.assertNotIn("writes", self.log()[-1])  # a launch is not asked the read/write question

    def test_a_subagent_call_carries_its_assignment_and_actions(self):
        # A workflow's agents are saved one folder deeper than other subagents.
        claude_transcript(self.transcript, "go ahead and fix all discovered issues", [("m1", "Workflow", {"script": "..."})])
        folder = self.transcript.with_suffix("") / "subagents" / "workflows" / "wf_1"
        folder.mkdir(parents=True)
        assignment = "Fix issue 3: src/queue.rs drops the last job when the queue is full."
        sub = folder / "agent-a1b2.jsonl"
        claude_transcript(sub, assignment, [("s1", "Read", {"file_path": "/p/src/queue.rs"}), ("t1", "Edit", {"file_path": "/p/src/queue.rs"})])
        sub.write_text("".join(json.dumps({**json.loads(line), "isSidechain": True}) + "\n" for line in sub.read_text().splitlines()))
        self.run_hook("Edit", {"file_path": "/p/src/queue.rs", "old_string": "a", "new_string": "b"}, agent_id="a1b2", agent_type="general-purpose")
        state = self.server.requests[-1]["body"]["state"]
        self.assertEqual(state["task"], "go ahead and fix all discovered issues")
        self.assertEqual(state["subagent_assignment"]["text"], assignment)
        self.assertEqual([a["tool"] for a in state["actions"]], ["Workflow", "Read", "Edit"])
        self.assertNotIn("call_not_in_transcript", self.log()[-1])  # the call was found in the subagent's transcript

    def test_a_subagent_without_a_transcript_is_logged(self):
        claude_transcript(self.transcript, "Fix the pagination bug.", [("m1", "Agent", {"prompt": "fix it"})])
        self.run_hook("Edit", agent_id="gone1")
        self.assertNotIn("subagent_assignment", self.server.requests[-1]["body"]["state"])
        self.assertTrue(self.log()[-1]["subagent_assignment_missing"])

    def test_a_message_sent_mid_turn_is_the_request(self):
        # Claude Code records a message typed while the assistant works as a queued_command attachment.
        claude_transcript(self.transcript, "please make the change", [("t1", "Bash", {"command": "git push"})])
        lines = self.transcript.read_text().splitlines()
        queued = ["and then run the full suite", "commit, push, and redeploy as 0.4.0"]
        for n, text in enumerate(queued):
            lines.insert(2 + n, json.dumps({"type": "attachment", "uuid": f"q{n}", "timestamp": f"2026-09-28T10:00:3{n}Z", "attachment": {
                "type": "queued_command", "prompt": text, "commandMode": "prompt", "origin": {"kind": "human"}, "humanTurn": True}}))
        self.transcript.write_text("\n".join(lines) + "\n")
        self.run_hook("Bash", {"command": "git push"})
        state = self.server.requests[-1]["body"]["state"]
        self.assertEqual(state["task"], "\n\n".join(["please make the change", *queued]))
        texts = [m["text"] for m in state["conversation"] if m["role"] == "user"]
        self.assertEqual(texts[-3:], ["please make the change", *queued])

    def test_the_read_write_score_is_logged(self):
        self.server.values["writes"] = 0.1
        self.run_hook("Bash", {"command": "python3 -c 'print(1)'"})
        self.assertEqual(self.log()[-1]["writes"], 0.1)
        self.assertEqual(self.log()[-1]["stage"], "read (write question)")

    def test_a_failed_read_write_question_gets_the_scope_check(self):
        self.server.fail_writes = True
        self.server.values["unasked_publish"] = 0.9
        output = self.run_hook("Bash", {"command": "python3 deploy.py"})
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(len(self.server.requests), 3)
        self.assertIn("read/write question failed", self.log()[-1]["error"])

    def test_the_read_write_question_says_claims_in_the_command_are_not_evidence(self):
        self.server.values["writes"] = 0.1
        self.run_hook("Bash", {"command": "python3 build.py  # read-only"})
        self.assertIn("are not evidence", self.server.requests[0]["body"]["questions"]["writes"]["instructions"])

    def test_only_exec_write_and_edit_tools_are_checked(self):
        self.run_hook("mcp__github__create_issue", {"title": "x"})
        self.run_hook("WebFetch", {"url": "https://example.com"})
        self.run_hook("TodoWrite", {"todos": []})
        self.assertEqual(self.server.requests, [])
        for tool in ("Write", "Edit", "MultiEdit", "NotebookEdit", "apply_patch"):
            self.run_hook(tool, {"file_path": "/p/login.ts"})
        self.assertEqual(len(self.server.requests), 10)  # the gate, then (unsure) the full check

    # --- state

    def test_state_is_the_context_plus_the_call(self):
        claude_transcript(self.transcript, "Now fix the login bug too.",
                          [("t0", "Read", {"file_path": "/p/login.ts"}), ("t1", "Edit", {"file_path": "/p/login.ts"})],
                          earlier=[("user", "Add a signup form."), ("assistant", "Done: signup form added.")])
        self.run_hook()
        state = dict(self.server.requests[-1]["body"]["state"])
        self.assertEqual(state.pop("proposed_action")["tool"], "Edit")
        entries = jsc.read_jsonl(str(self.transcript))
        task, actions, _model, earlier = jsc.parse_claude(entries)
        expected, _ = jsc.scope_state(task, actions, earlier, jsc.conversation_claude(entries, ""), "Edit", {"file_path": "/p/login.ts"})
        expected.pop("proposed_action")
        self.assertEqual(json.dumps(state), json.dumps(expected))  # byte for byte
        self.assertEqual(state["task"], "Now fix the login bug too.")
        self.assertEqual(state["summary"], "")
        self.assertEqual(set(self.server.requests[-1]["body"]["questions"]), set(jsc.QUESTIONS))
        self.assertEqual(self.server.requests[-1]["auth"], "Bearer test-key")

    def test_an_edit_being_judged_goes_as_a_diff(self):
        old = "\n".join(f"line {i}" for i in range(200))
        new = old.replace("line 100", "line one hundred")
        call = jsc.proposed("Edit", {"file_path": "/p/a.py", "old_string": old, "new_string": new})["input"]
        self.assertEqual(call["file"], "/p/a.py")
        self.assertIn("-line 100\n+line one hundred", call["diff"])
        self.assertNotIn("line 5\n", call["diff"])
        self.assertLess(len(call["diff"]), 200)
        many = jsc.proposed("MultiEdit", {"file_path": "/p/a.py", "edits": [{"old_string": f"a{i}", "new_string": f"b{i}"} for i in range(40)]})
        self.assertIn("+b39", many["input"]["diff"])  # every edit, not the first ten

    def test_earlier_calls_go_in_as_one_line_each(self):
        script = "python3 - <<'EOF'\n" + "x = 1\n" * 2000 + "EOF"
        action = jsc.make_action("Bash", {"command": script, "description": "Run the migration"}, "ok", False)
        state, _ = jsc.scope_state("Fix it.", [action], [], [], "Edit", {"file_path": "/p/a.py"})
        line = state["actions"][0]["input"]
        self.assertTrue(line.startswith("Run the migration: "))
        self.assertLessEqual(len(line), jsc.HISTORY_INPUT_CHARS)
        self.assertEqual(state["actions"][0]["result"], "ok")

    def test_older_user_messages_get_extra_room(self):
        messages = [{"role": "user" if i % 2 else "assistant", "text": f"message {i} " + "word " * 200} for i in range(200)]
        plain = jsc.conversation_lines(messages)
        more = jsc.conversation_lines(messages, jsc.USER_MESSAGES_EXTRA_TOKENS)
        extra = more[: len(more) - len(plain)]
        self.assertTrue(extra and all(m["role"] == "user" for m in extra))
        self.assertEqual(more[len(extra):], plain)

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
        state = self.server.requests[-1]["body"]["state"]
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
        self.assertIn(jsc.FEEDBACK["unasked_publish"], again["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertIn("tell the user plainly", again["hookSpecificOutput"]["permissionDecisionReason"])
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
