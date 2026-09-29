#!/usr/bin/env python3
"""Re-score the states saved by replay.py with the current questions, to see what a wording change does.

    python3 tools/rejudge.py OUT.jsonl REPLAY.jsonl [REPLAY.jsonl ...]

Prints each call denied now or before, and the share denied at several thresholds.
"""
import argparse
import json
from concurrent.futures import ThreadPoolExecutor

from scope_questions import jsc


def top(scores: dict) -> float:
    return max(scores.values(), default=0.0)


def rescore(row: dict) -> dict:
    try:
        values = jsc.noul_values(jsc.ask_jev(row["state"], jsc.QUESTIONS, jsc.api_key(), timeout=30))
    except Exception as error:
        return {**row, "error": str(error)}
    return {**row, "before": row.get("scores", {}), "values": values,
            "scores": {k: round(v, 2) for k, v in jsc.compose(values).items()}}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("out")
    parser.add_argument("replays", nargs="+")
    args = parser.parse_args()
    rows = [json.loads(line) for path in args.replays for line in open(path) if line.strip()]
    rows = [r for r in rows if "state" in r]
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(rescore, rows))
    t = jsc.DEFAULT_THRESHOLD
    with open(args.out, "w") as f:
        for r in results:
            f.write(json.dumps(r) + "\n")
    for r in results:
        if "error" in r:
            print("ERROR", r["session"], r["error"])
            continue
        now, before = top(r["scores"]), top(r["before"])
        if now > t or before > t:
            worst = max(r["scores"], key=r["scores"].get)
            print(f"{'DENY' if now > t else 'ok  '} {before:.2f} -> {now:.2f} {worst:20s} {r['session']} {r['call'][:70]}")
    scored = [r for r in results if "error" not in r]
    for threshold in (0.6, 0.65, 0.7, 0.75, 0.8):
        n = sum(top(r["scores"]) > threshold for r in scored)
        print(f"threshold {threshold:.2f}: {n}/{len(scored)} denied ({100 * n / max(len(scored), 1):.1f}%)")


if __name__ == "__main__":
    main()
