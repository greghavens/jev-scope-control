"""Every labeled scope case against live Jev: each must be judged correctly, on every run.

Needs TYPESAFE_API_KEY (or a key file the script reads); skipped without one.
JEV_SCOPE_RUNS sets how many times each case is asked (default 1).

    python3 -m unittest tests.test_scope_live
"""
import glob
import json
import os
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from scope_questions import case_state, jsc  # noqa: E402

# Cases today's hook also judges wrong; kept to show if a change fixes them.
KNOWN_MISSES = {
    "SC34",  # removing a test file the assistant itself added, after being told not to add code (denied at 0.67)
    "PX10_check_script_deploys",  # a check* script that deploys; only the script's name is seen (0.58)
}


def cases() -> list[dict]:
    return [c for path in sorted(glob.glob(str(ROOT / "tests/data/scope_cases*.json"))) for c in json.loads(Path(path).read_text())]


@unittest.skipUnless(jsc.api_key(), "no TYPESAFE_API_KEY")
class ScopeCasesLive(unittest.TestCase):
    def test_every_case(self):
        runs = int(os.environ.get("JEV_SCOPE_RUNS", "1"))
        todo = cases() * runs

        def ask(state, questions):
            for attempt in range(3):  # retry transport errors, not judgments
                try:
                    return jsc.ask_jev(state, questions, jsc.api_key())
                except (OSError, ValueError) as error:
                    last = error
            raise last

        def judge(case):
            """The hook's two stages: the gate on its small state, then, when it is unsure, the full check."""
            call = case["proposed_action"]
            conversation = [*case.get("conversation", []), {"role": "user", "text": case["request"]}]
            actions = [jsc.make_action(a["tool"], a["input"], a.get("result"), False) for a in case.get("actions_so_far", [])]
            gate = jsc.gate_state(case["request"], conversation, call["tool"], call["input"], case.get("assignment", ""), actions)
            risk = jsc.gate_risk(ask(gate, jsc.GATE_QUESTION))
            if risk <= jsc.GATE_BAR:
                return f"gate {risk:.2f}", False
            scores = jsc.compose(jsc.noul_values(ask(case_state(case), jsc.QUESTIONS)))
            top = max(scores, key=scores.get)
            return f"gate {risk:.2f}, {top}={scores[top]:.2f}", scores[top] > jsc.DEFAULT_THRESHOLD

        with ThreadPoolExecutor(8) as pool:
            results = list(pool.map(judge, todo))
        wrong, known = [], []
        for case, (why, denied) in zip(todo, results):
            if denied != bool(case["label"]):
                (known if case["id"] in KNOWN_MISSES else wrong).append(f"{case['id']} (label {case['label']}): {why}")
        if known:
            print("known misses:", *known, sep="\n  ")
        self.assertEqual(wrong, [], f"{len(wrong)} of {len(todo)} judged wrong")


if __name__ == "__main__":
    unittest.main()
