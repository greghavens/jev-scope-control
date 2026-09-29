#!/usr/bin/env python3
"""Send a {"state", "questions"} JSON file to Jev and print each answer compactly.

Usage: ask_jev.py request.json
Reads TYPESAFE_API_KEY from the environment, else from ../jev-no-bullshit/.env.
"""
import json
import os
import sys
import time
import urllib.request
from pathlib import Path


def api_key() -> str:
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if key:
        return key
    env = Path(__file__).resolve().parents[2] / "jev-no-bullshit" / ".env"
    for line in env.read_text().splitlines():
        if line.startswith("TYPESAFE_API_KEY="):
            return line.split("=", 1)[1].strip().strip("\"'")
    sys.exit("TYPESAFE_API_KEY not found")


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
