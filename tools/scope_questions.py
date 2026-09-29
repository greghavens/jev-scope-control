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
