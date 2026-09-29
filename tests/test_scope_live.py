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
from scope_questions import jsc  # noqa: E402

FIELDS = ("request", "conversation", "actions_so_far", "proposed_action", "working_directory")


def cases() -> list[dict]:
    return [c for path in sorted(glob.glob(str(ROOT / "tests/data/scope_cases*.json"))) for c in json.loads(Path(path).read_text())]


@unittest.skipUnless(jsc.api_key(), "no TYPESAFE_API_KEY")
class ScopeCasesLive(unittest.TestCase):
    def test_every_case(self):
        runs = int(os.environ.get("JEV_SCOPE_RUNS", "1"))
        todo = cases() * runs

        def judge(case):
            state = {k: case[k] for k in FIELDS if k in case}
            for attempt in range(3):  # retry transport errors, not judgments
                try:
                    return jsc.judge(state, jsc.api_key(), jsc.DEFAULT_THRESHOLD)[2]
                except (OSError, ValueError) as error:
                    last = error
            raise last

        with ThreadPoolExecutor(8) as pool:
            results = list(pool.map(judge, todo))
        wrong = []
        for case, scores in zip(todo, results):
            top = max(scores, key=scores.get)
            if (scores[top] > jsc.DEFAULT_THRESHOLD) != bool(case["label"]):
                wrong.append(f"{case['id']} (label {case['label']}): {top}={scores[top]:.2f}")
        self.assertEqual(wrong, [], f"{len(wrong)} of {len(todo)} judged wrong")


if __name__ == "__main__":
    unittest.main()
