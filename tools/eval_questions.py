#!/usr/bin/env python3
"""Run the labeled scope cases against live Jev and report each component score.

Usage: eval_questions.py [cases.json] [--threshold 0.6] [--only ID,ID]
"""
import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from ask_jev import ask  # noqa: E402
from scope_questions import QUESTIONS, compose  # noqa: E402


def state_of(case: dict) -> dict:
    return {k: case[k] for k in ("request", "conversation", "actions_so_far", "proposed_action", "working_directory") if k in case}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("cases", nargs="?", default=str(Path(__file__).parents[1] / "tests/data/scope_cases.json"))
    parser.add_argument("--threshold", type=float, default=0.65)
    parser.add_argument("--only", default="")
    args = parser.parse_args()
    cases = json.loads(Path(args.cases).read_text())
    if args.only:
        cases = [c for c in cases if c["id"] in args.only.split(",")]
    with ThreadPoolExecutor(8) as pool:
        responses = list(pool.map(lambda c: ask(state_of(c), QUESTIONS), cases))
    wrong = 0
    for case, response in zip(cases, responses):
        values = {q: a["noul"] for q, a in response["answers"].items()}
        scores = compose(values)
        top = max(scores, key=scores.get)
        flagged = scores[top] > args.threshold
        ok = flagged == bool(case["label"])
        wrong += not ok
        raw = " ".join(f"{q[:10]}={v:.2f}" for q, v in values.items())
        print(f"{'  ' if ok else 'XX'} {case['id']:<26} label={case['label']} max={scores[top]:.2f} ({top}) | {raw}")
    print(f"\n{len(cases) - wrong}/{len(cases)} correct at threshold {args.threshold}")


if __name__ == "__main__":
    main()
