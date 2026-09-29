#!/usr/bin/env python3
"""Send a {"state", "questions"} JSON file to Jev and print each answer compactly.

Usage: ask_jev.py request.json
Uses the key the hook uses.
"""
import importlib.machinery
import json
import os
import sys
import time
import types
import urllib.request
from pathlib import Path


def api_key() -> str:
    loader = importlib.machinery.SourceFileLoader("jsc", str(Path(__file__).resolve().parents[1] / "jev-scope-control"))
    jsc = types.ModuleType("jsc")
    loader.exec_module(jsc)
    return jsc.api_key() or sys.exit("no TypeSafe API key found")


def ask(state, questions) -> dict:
    body = json.dumps({"state": state, "model": "jev-latest", "questions": questions}).encode()
    request = urllib.request.Request(
        "https://api.typesafe.ai/v1/systemone", data=body, method="POST",
        headers={"Authorization": f"Bearer {api_key()}", "Content-Type": "application/json", "User-Agent": "jev-scope-control"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


if __name__ == "__main__":
    request = json.loads(Path(sys.argv[1]).read_text())
    start = time.monotonic()
    response = ask(request["state"], request["questions"])
    print(f"model={response.get('model')} seconds={time.monotonic() - start:.2f} usage={response.get('usage')}")
    for qid, answer in response["answers"].items():
        if "choice" in answer:
            probs = sorted(answer["probabilities"].items(), key=lambda kv: -kv[1])
            print(f"{qid}: {answer['choice']} (conf {answer.get('confidence', 0):.2f}) " + ", ".join(f"{k}={v:.2f}" for k, v in probs))
        elif "noul" in answer:
            print(f"{qid}: noul={answer['noul']:.3f}")
        else:
            print(f"{qid}: {json.dumps(answer)}")
