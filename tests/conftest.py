"""Hypothesis settings: no per-example deadline, so slow CI machines do not flake."""

from hypothesis import settings

settings.register_profile("slo-ledger", deadline=None)
settings.load_profile("slo-ledger")
