"""Skill-removal compatibility fence (Workstream A / gate A).

Skill management is gone from AgentDB. These tests prove:
- The dead ``skill_candidate_threshold`` kwarg is accepted-but-ignored with a
  DeprecationWarning (no hard break for existing callers).
- No skill-management surface is importable from the package or its public
  exports.
"""
from __future__ import annotations

import warnings

import pytest

import agentdb
from agentdb import AgentDB


def _db(tmp_path):
    return AgentDB(path=tmp_path / "agentdb.sqlite", memory_dir=tmp_path)


def test_obsolete_skill_candidate_threshold_is_ignored_with_warning(tmp_path):
    db = _db(tmp_path)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        # Must NOT raise, even though the parameter was removed from the
        # underlying LifecycleManager signature.
        db.consolidate("a1", skill_candidate_threshold=3)
    assert any(
        issubclass(w.category, DeprecationWarning)
        and "skill_candidate_threshold" in str(w.message)
        for w in caught
    )


def test_no_skill_management_surface_exported():
    """The package no longer exports skill registry/API objects."""
    for forbidden in ("Skill", "SkillStore", "SkillsAPI", "skills_api"):
        assert not hasattr(agentdb, forbidden), f"{forbidden} still exported"


def test_agentdb_instance_has_no_skills_attribute(tmp_path):
    db = _db(tmp_path)
    for forbidden in ("skills", "skills_api", "skill_store"):
        assert not hasattr(db, forbidden), f"db.{forbidden} still present"


def test_core_package_imports_without_skill_modules():
    """Importing agentdb must not require any skill module to exist."""
    import importlib

    for mod in ("agentdb", "agentdb.interface.client", "agentdb.gateway"):
        importlib.import_module(mod)
    # No skill package should be importable from the installed distribution.
    with pytest.raises(ImportError):
        importlib.import_module("agentdb.skills")
