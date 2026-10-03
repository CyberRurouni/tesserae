"""
Orchestrator city — the loop that runs the whole country.

One problem solved: the keyword lifecycle. Generate (Actor 1) -> search
each keyword SEQUENTIALLY (captcha safety) -> time the exhausted array
(Actor 2) -> review coverage at the threshold (checkpoint c) -> time
each new exclusion prompt in parallel (Actor 4) -> repeat or harvest.

Citizens:
  - collection.py — Redis db3 persistence (keywords/prompts with TTLs,
    harvest flag); the crash-proof mirror of the runtime collection
  - actors.py     — the four AI calls, each with one narrow contract
  - main.py       — the loop itself
"""

from . import actors, collection
from .main import run_orchestrator

__all__ = ["actors", "collection", "run_orchestrator"]
