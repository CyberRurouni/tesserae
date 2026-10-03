"""
Storage city — where verdicts become durable facts.

Solves one problem: taking the checkpoint's verdicts and turning them into
Redis marks + on-disk stores, identically no matter which interface drives
the pipeline (smoke runner today, orchestrator tomorrow). Without it, every
caller re-implements the marking logic and they WILL drift apart.
"""

from .results import record_results
from .profile import load_profile

__all__ = ["record_results", "load_profile"]
