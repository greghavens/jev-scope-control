"""The script's questions and composition, for the tools. The script is the only copy."""
import importlib.machinery
import importlib.util
from pathlib import Path

_loader = importlib.machinery.SourceFileLoader("jsc", str(Path(__file__).resolve().parent.parent / "jev-scope-control"))
_spec = importlib.util.spec_from_loader("jsc", _loader)
jsc = importlib.util.module_from_spec(_spec)
_loader.exec_module(jsc)

QUESTIONS = jsc.QUESTIONS
compose = jsc.compose


def case_state(case: dict) -> dict:
    """A labeled case's state as the hook builds it: jev-no-bullshit's context, plus the case's call.

    Cases record the conversation before the request; jev-no-bullshit's conversation includes it."""
    conversation = [*case.get("conversation", []), {"role": "user", "text": case["request"]}]
    actions = [jsc.make_action(a["tool"], a["input"], a.get("result"), False) for a in case.get("actions_so_far", [])]
    call = case["proposed_action"]
    state, _ = jsc.scope_state(case["request"], actions, [], conversation, call["tool"], call["input"])
    state["proposed_action"] = call
    return state
