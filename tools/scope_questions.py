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


def case_context(case: dict) -> tuple[str, list[dict], list[dict], list[dict], str]:
    """(task, actions, earlier actions, conversation, assignment) for a labeled case, as the hook reads them.

    A case rebuilt from a real transcript carries the hook's own reading of it as `hook_context`. The others
    record the conversation before the request; the hook's conversation includes it."""
    if "hook_context" in case:
        c = case["hook_context"]
        return c["task"], c["actions"], c["earlier"], c["conversation"], c["assignment"]
    conversation = [*case.get("conversation", []), {"role": "user", "text": case["request"]}]
    actions = [jsc.make_action(a["tool"], a["input"], a.get("result"), False) for a in case.get("actions_so_far", [])]
    return case["request"], actions, [], conversation, case.get("assignment", "")


def gate_case_state(case: dict) -> dict:
    """A labeled case's gate state as the hook builds it."""
    task, actions, earlier, conversation, assignment = case_context(case)
    call = case["proposed_action"]
    return jsc.gate_state(task, conversation, call["tool"], call["input"], assignment, actions, earlier)


def case_state(case: dict) -> dict:
    """A labeled case's full-check state as the hook builds it: the context, plus the case's call."""
    task, actions, earlier, conversation, assignment = case_context(case)
    call = case["proposed_action"]
    state, _ = jsc.scope_state(task, actions, earlier, conversation, call["tool"], call["input"], assignment)
    # A case rebuilt from a transcript keeps the call as the hook sent it; a launch keeps the hook's note on what it starts.
    if "hook_context" not in case and call["tool"].lower() not in jsc.AGENT_TOOLS:
        state["proposed_action"] = call
    return state
