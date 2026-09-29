#!/usr/bin/env python3
"""Replay real Claude Code tool calls through jev-scope-control and report which it would deny.

Each sampled call is judged on the transcript as it stood just before the call, the way the hook
sees it. Nothing is denied or changed; this only measures.

Usage: replay.py [--sessions 40] [--per-session 3] [--out results.jsonl] [--include-automated]
"""
import argparse
import glob
import importlib.machinery
import importlib.util
import json
import os
import random
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader("jsc", str(ROOT / "jev-scope-control"))
spec = importlib.util.spec_from_loader("jsc", loader)
jsc = importlib.util.module_from_spec(spec)
loader.exec_module(jsc)


def call_points(entries: list[dict]) -> list[tuple[int, dict]]:
    """(entry index, tool_use block) for each main-thread call the hook would check."""
    points = []
    for n, e in enumerate(entries):
        if e.get("type") != "assistant" or e.get("isSidechain"):
            continue
        for b in (e.get("message") or {}).get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_use" and jsc.needs_check(b.get("name"), b.get("input")):
                points.append((n, b))
    return points


def codex_call_points(entries: list[dict]) -> list[tuple[int, dict]]:
    """(entry index, call as {id, name, input}) for each Codex call the hook would check."""
    points = []
    for n, e in enumerate(entries):
        p = e.get("payload") or {}
        if e.get("type") != "response_item" or p.get("type") not in ("function_call", "custom_tool_call"):
            continue
        tool_input = p.get("arguments") if p["type"] == "function_call" else p.get("input")
        if isinstance(tool_input, str) and p["type"] == "function_call":
            try:
                tool_input = json.loads(tool_input)
            except ValueError:
                pass
        if p.get("name") == "apply_patch" and isinstance(tool_input, str):
            tool_input = {"command": tool_input}
        if jsc.needs_check(p.get("name"), tool_input):
            points.append((n, {"id": p.get("call_id"), "name": p.get("name"), "input": tool_input}))
    return points


def codex_files() -> list[str]:
    files = []
    for path in glob.glob(os.path.expanduser("~/.codex/sessions/*/*/*/rollout-*.jsonl")):
        try:
            with open(path) as f:
                meta = json.loads(f.readline()).get("payload") or {}
        except (OSError, ValueError):
            continue
        # Interactive sessions only: codex_exec runs are automated pipelines.
        if meta.get("originator") == "codex-tui" and meta.get("source") in ("cli", "vscode") and os.path.getsize(path) > 50_000:
            files.append(path)
    return files


def judge(point: dict) -> dict:
    entries, n, block = point["entries"], point["index"], point["block"]
    parse = jsc.parse_codex if point.get("codex") else jsc.parse_claude
    request, conversation, actions = parse(entries[: n + 1], block["id"])
    base = {"session": point["session"], "tool": block["name"], "call": jsc.short_call(block["name"], block["input"])}
    if not request:
        return {**base, "skipped": "no request"}
    state = jsc.build_state(request, conversation, actions, block["name"], block["input"], point["cwd"])
    try:
        _, values, scores = jsc.judge(state, jsc.api_key(), jsc.DEFAULT_THRESHOLD)
    except Exception as error:
        return {**base, "error": str(error)}
    flagged = sorted((k for k, v in scores.items() if v > jsc.DEFAULT_THRESHOLD), key=lambda k: -scores[k])
    return {**base, "denied": bool(flagged), "flagged": flagged, "scores": {k: round(v, 2) for k, v in scores.items()},
            "request": jsc.one_line(request, 300), "state_chars": len(json.dumps(state)), "state": state}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sessions", type=int, default=40)
    parser.add_argument("--per-session", type=int, default=3)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--out", default="")
    parser.add_argument("--skip", type=int, default=0, help="skip the newest N sessions (to hold out the tuning set)")
    parser.add_argument("--codex", action="store_true", help="replay Codex rollouts instead of Claude Code transcripts")
    parser.add_argument("--exclude", default="", help="a previous --out file whose calls to leave out")
    parser.add_argument("--include-automated", action="store_true", help="include jev-graph-builder pipeline workspaces")
    args = parser.parse_args()
    rng = random.Random(args.seed)
    files = codex_files() if args.codex else glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl"))
    excluded = set()
    if args.exclude:
        excluded = {(r["session"], r["call"]) for r in map(json.loads, open(args.exclude))}
    if not args.include_automated:
        files = [f for f in files if "jgb-workspaces" not in f]
    files.sort(key=os.path.getmtime, reverse=True)
    points = []
    seen = 0
    for path in files:
        if len({p["session"] for p in points}) >= args.sessions:
            break
        entries = jsc.read_jsonl(path)
        candidates = codex_call_points(entries) if args.codex else call_points(entries)
        session = Path(path).stem[-8:] if args.codex else Path(path).stem[:8]
        candidates = [(n, b) for n, b in candidates if (session, jsc.short_call(b["name"], b["input"])) not in excluded]
        if not candidates:
            continue
        seen += 1
        if seen <= args.skip:
            continue
        cwd = next((e.get("cwd") or (e.get("payload") or {}).get("cwd") for e in entries if e.get("cwd") or (e.get("payload") or {}).get("cwd")), None)
        for n, block in rng.sample(candidates, min(args.per_session, len(candidates))):
            points.append({"session": session, "entries": entries, "index": n, "block": block, "cwd": cwd, "codex": args.codex})
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(judge, points))
    judged = [r for r in results if "denied" in r]
    denied = [r for r in judged if r["denied"]]
    for r in denied:
        print(f"DENY {r['session']} {','.join(r['flagged'])} {max(r['scores'].values()):.2f}\n     call:    {r['call']}\n     request: {r['request'][:220]}")
    errors = [r for r in results if "error" in r]
    for r in errors[:5]:
        print("ERROR", r["session"], r["error"][:200])
    print(f"\n{len(denied)}/{len(judged)} calls denied ({len(errors)} errors, {len(results) - len(judged) - len(errors)} skipped) "
          f"from {len({r['session'] for r in results})} sessions; max state {max((r['state_chars'] for r in judged), default=0)} chars")
    if args.out:
        with open(args.out, "w") as f:
            for r in results:
                f.write(json.dumps(r) + "\n")


if __name__ == "__main__":
    sys.exit(main())
