"""End-to-end tests: install the plugin into a real Claude Code, Codex, pi or opencode CLI and run a turn.

Each test installs this repo as a plugin into a throwaway home, then runs one headless turn. The model API
and the TypeSafe API are local mocks (tests/mocks.py), so no network or real credentials are needed. The
scripted model runs an in-scope command, then an out-of-scope one, then replies. The hook must let the
first run, deny the second before it runs, and the model must see the deny reason.

Judgment quality against live Jev is tested separately (tests/test_scope_live.py, tests/e2e_claude_code.py).
Tests skip when a CLI is not found. Point them at a binary with CLAUDE_BIN, CODEX_BIN, PI_BIN or OPENCODE_BIN.
Run with: python3 -m unittest tests.test_e2e -v
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mocks import IN_SCOPE_COMMAND, MARKER, OUT_OF_SCOPE_COMMAND, REPLY, TASK, MockAnthropic, MockJev, MockResponses  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
PLUGIN = "jev-scope-control@jev-scope-control"
MOONSHINER = Path.home() / ".local/share/moonshiner/toolchains"


def find(env, name, *fallbacks):
    for candidate in (os.environ.get(env), shutil.which(name), *map(str, fallbacks)):
        if candidate and Path(candidate).is_file():
            return candidate
    return None


CLAUDE_BIN = find("CLAUDE_BIN", "claude")
CODEX_BIN = find("CODEX_BIN", "codex")
PI_BIN = find("PI_BIN", "pi", MOONSHINER / "pi/node_modules/.bin/pi")
OPENCODE_BIN = find("OPENCODE_BIN", "opencode", MOONSHINER / "opencode/node_modules/opencode-linux-x64/bin/opencode")


def scope_answer(qid, body):
    """Jev stand-in: the task's command serves the request; the other one pushes unasked."""
    proposed = json.dumps(body["state"]["proposed_action"])
    if qid in ("serves_request", "makes_change"):
        return 0.95
    if qid == "unasked_publish" and "PUSHED" in proposed:
        return 0.95
    return 0.05


class _E2EBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.work = root / "work"
        self.home.mkdir()
        self.work.mkdir()
        (self.work / "notes.txt").write_text("teh notes\n")
        self.jev = MockJev()
        self.jev.answer = scope_answer
        self.env = {
            "HOME": str(self.home),
            "PATH": os.environ.get("PATH", ""),
            "SHELL": "/bin/sh",
            "LANG": "C.UTF-8",
            "TYPESAFE_API_KEY": "test-key",
            "TYPESAFE_BASE_URL": self.jev.url,
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        }

    def tearDown(self):
        self.jev.close()
        self.tmp.cleanup()

    def run_cli(self, args, timeout=180):
        proc = subprocess.run(
            args, cwd=self.work, env=self.env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout
        )
        self.assertEqual(proc.returncode, 0, f"{args}\nstdout:\n{proc.stdout[-3000:]}\nstderr:\n{proc.stderr[-3000:]}")
        return proc

    def assert_in_scope_ran_and_out_of_scope_denied(self, harness, reply):
        # What happened on disk: the first command ran, the second never did.
        self.assertTrue((self.work / "FIXED").exists(), "the in-scope command did not run")
        self.assertFalse((self.work / "PUSHED").exists(), "the out-of-scope command ran")

        log_path = self.home / ".jev-scope-control" / "log.jsonl"
        self.assertTrue(log_path.exists(), "the hook never ran (no log file)")
        log = [json.loads(line) for line in log_path.read_text().splitlines()]
        self.assertEqual([e.get("denied") for e in log], [False, True], log)
        self.assertTrue(all(e["harness"] == harness for e in log), log)
        self.assertEqual(log[1]["flagged"], ["unasked_publish"])

        # What Jev saw, built from the CLI's own session: the task, and the first call as done.
        self.assertEqual(self.jev.schema_errors, [])
        first, second = (c["body"]["state"] for c in self.jev.calls)
        self.assertEqual(first["request"], TASK)
        self.assertIn(IN_SCOPE_COMMAND, json.dumps(first["proposed_action"]))
        self.assertEqual(second["request"], TASK)
        self.assertIn(OUT_OF_SCOPE_COMMAND, json.dumps(second["proposed_action"]))
        self.assertEqual(len(second["actions_so_far"]), 1, second["actions_so_far"])
        self.assertIn(IN_SCOPE_COMMAND, json.dumps(second["actions_so_far"][0]))

        # The model got the reason as the denied call's result.
        self.assertTrue(reply.startswith(REPLY), reply)
        self.assertIn(MARKER, reply)
        self.assertIn("stop and ask the user", reply)


@unittest.skipUnless(CLAUDE_BIN, "claude CLI not found (set CLAUDE_BIN)")
class ClaudeCodeE2E(_E2EBase):
    def setUp(self):
        super().setUp()
        self.model = MockAnthropic()
        self.env.update({
            "CLAUDE_CONFIG_DIR": str(self.home / ".claude"),
            "ANTHROPIC_BASE_URL": self.model.url,
            "ANTHROPIC_API_KEY": "dummy",
            "DISABLE_AUTOUPDATER": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        })

    def tearDown(self):
        self.model.close()
        super().tearDown()

    def test_plugin_installs_and_denies_the_out_of_scope_call(self):
        self.run_cli([CLAUDE_BIN, "plugin", "marketplace", "add", str(REPO)])
        self.run_cli([CLAUDE_BIN, "plugin", "install", PLUGIN])
        self.assertIn(PLUGIN, self.run_cli([CLAUDE_BIN, "plugin", "list"]).stdout)

        proc = self.run_cli([
            CLAUDE_BIN, "-p", TASK, "--output-format", "json", "--model", "claude-sonnet-5",
            "--allowedTools", "Bash",
        ])
        self.assert_in_scope_ran_and_out_of_scope_denied("claude", json.loads(proc.stdout).get("result", ""))


@unittest.skipUnless(CODEX_BIN, "codex CLI not found (set CODEX_BIN)")
class CodexE2E(_E2EBase):
    def setUp(self):
        super().setUp()
        self.model = MockResponses()
        self.env.update({
            "CODEX_HOME": str(self.home / ".codex"),
            "CODEX_SQLITE_HOME": str(self.home / ".codex"),
            "CODEX_API_KEY": "dummy",
        })
        (self.home / ".codex").mkdir()

    def tearDown(self):
        self.model.close()
        super().tearDown()

    def test_plugin_installs_and_denies_the_out_of_scope_call(self):
        self.run_cli([CODEX_BIN, "plugin", "marketplace", "add", str(REPO)])
        self.run_cli([CODEX_BIN, "plugin", "add", PLUGIN])
        listing = self.run_cli([CODEX_BIN, "plugin", "list"]).stdout
        self.assertIn(PLUGIN, listing)
        self.assertIn("installed, enabled", listing)

        # --dangerously-bypass-hook-trust stands in for approving the hook once with /hooks.
        proc = self.run_cli([
            CODEX_BIN, "exec", "--skip-git-repo-check", "--dangerously-bypass-hook-trust",
            "--dangerously-bypass-approvals-and-sandbox",
            "-c", f"openai_base_url={json.dumps(self.model.url + '/v1')}",
            "-m", "gpt-5.5-codex", TASK,
        ])
        reply = next((line for line in proc.stdout.splitlines() if line.startswith(REPLY)), proc.stdout)
        self.assert_in_scope_ran_and_out_of_scope_denied("codex", reply)


class CodexKeyFileE2E(CodexE2E):
    """The key only in ~/.config/jev-scope-control/env, not in Codex's environment."""

    def setUp(self):
        super().setUp()
        del self.env["TYPESAFE_API_KEY"]
        key_file = self.home / ".config" / "jev-scope-control" / "env"
        key_file.parent.mkdir(parents=True)
        key_file.write_text("TYPESAFE_API_KEY=file-key\n")

    def test_plugin_installs_and_denies_the_out_of_scope_call(self):
        super().test_plugin_installs_and_denies_the_out_of_scope_call()
        self.assertEqual({c["headers"]["Authorization"] for c in self.jev.calls}, {"Bearer file-key"})


@unittest.skipUnless(PI_BIN, "pi CLI not found (set PI_BIN)")
class PiE2E(_E2EBase):
    def setUp(self):
        super().setUp()
        self.model = MockAnthropic()
        agent_dir = self.home / ".pi" / "agent"
        agent_dir.mkdir(parents=True)
        (agent_dir / "models.json").write_text(json.dumps({"providers": {"mock": {
            "baseUrl": self.model.url, "api": "anthropic-messages", "apiKey": "dummy", "models": [{"id": "claude-mock"}],
        }}}))
        self.env.update({"PI_OFFLINE": "1", "PI_TELEMETRY": "0"})

    def tearDown(self):
        self.model.close()
        super().tearDown()

    def test_plugin_installs_and_denies_the_out_of_scope_call(self):
        self.run_cli([PI_BIN, "install", str(REPO)])
        self.assertIn(str(REPO), self.run_cli([PI_BIN, "list"]).stdout)
        proc = self.run_cli([PI_BIN, "--print", "--no-session", "--model", "mock/claude-mock", TASK])
        self.assert_in_scope_ran_and_out_of_scope_denied("pi", proc.stdout.strip())


@unittest.skipUnless(OPENCODE_BIN, "opencode CLI not found (set OPENCODE_BIN)")
class OpencodeE2E(_E2EBase):
    """Driven through opencode's server, which its TUI runs on."""

    def setUp(self):
        super().setUp()
        self.model = MockAnthropic()
        config = self.home / ".config" / "opencode"
        config.mkdir(parents=True)
        (config / "opencode.json").write_text(json.dumps({
            "plugin": [os.environ.get("OPENCODE_PLUGIN_SPEC", str(REPO))],
            "autoupdate": False,
            "share": "disabled",
            "provider": {"anthropic": {"options": {"baseURL": self.model.url + "/v1", "apiKey": "dummy"}}},
            "permission": {"bash": "allow"},
        }))
        for var, sub in (("XDG_CONFIG_HOME", ".config"), ("XDG_DATA_HOME", ".local/share"),
                         ("XDG_CACHE_HOME", ".cache"), ("XDG_STATE_HOME", ".local/state")):
            self.env[var] = str(self.home / sub)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self.base = f"http://127.0.0.1:{port}"
        self.server = subprocess.Popen(
            [OPENCODE_BIN, "serve", "--port", str(port), "--hostname", "127.0.0.1"], cwd=self.work, env=self.env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def tearDown(self):
        self.server.terminate()
        self.server.wait(timeout=10)
        self.model.close()
        super().tearDown()

    def api(self, method, path, body=None):
        request = urllib.request.Request(
            f"{self.base}{path}?directory={self.work}", method=method, headers={"Content-Type": "application/json"},
            data=json.dumps(body).encode() if body is not None else None,
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            raw = response.read()
            return json.loads(raw) if raw else None

    def test_plugin_loads_and_denies_the_out_of_scope_call(self):
        deadline = time.monotonic() + 30
        while True:
            try:
                session = self.api("POST", "/session", {})
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.2)
        reply = self.api("POST", f"/session/{session['id']}/message", {
            "model": {"providerID": "anthropic", "modelID": "claude-sonnet-4-5"},
            "parts": [{"type": "text", "text": TASK}],
        })
        text = "".join(p.get("text", "") for p in reply["parts"] if p["type"] == "text")
        self.assert_in_scope_ran_and_out_of_scope_denied("opencode", text)


if __name__ == "__main__":
    unittest.main()
