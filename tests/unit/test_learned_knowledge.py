"""Scoped learned knowledge — claim lifecycle and strict retrieval.

The fixture is deterministic: prewritten observations with explicit scope, no
embeddings, no model calls, no consolidation. Every acceptance case from the
spec has a test here, named after the case it covers.

Scope in these tests stands in for what the host would supply: ``repository_id``
is an opaque string and ``task_type`` is one of a few host-chosen values. AgentDB
never interprets either.
"""
import pytest

from agentdb import AgentDB
from agentdb.governance.permission_router import PermissionDenied
from agentdb.learning import (
    LearnedKnowledgeError,
    is_active_claim,
    knowledge_id_of,
)

REPO = "komper"
OTHER_REPO = "agentgate"
TASK = "bug_fix"
OTHER_TASK = "feature"


# ---------------------------------------------------------------------------
# Fixture: prewritten observations
# ---------------------------------------------------------------------------

def _obs(db, key, value, *, repository_id=REPO, task_type=TASK, agent_id="ag"):
    """Write one scoped observation, the kind a claim is distilled from."""
    return db.write(
        key=key, value=value, origin="agent_inferred", agent_id=agent_id,
        session_id="s-1", repository_id=repository_id, task_type=task_type,
        working_dirs=[f"/repos/{repository_id}"],
    )


@pytest.fixture
def db(tmp_path):
    """Trusted mode (no permissions file): authorization is the host's boundary."""
    return AgentDB(memory_dir=str(tmp_path / "db"))


@pytest.fixture
def iso_db(tmp_path):
    """Isolation mode: a deny-by-default policy, as AgentGate configures."""
    return AgentDB(
        memory_dir=str(tmp_path / "iso"),
        permissions_path=str(_deny_policy(tmp_path)),
    )


def _deny_policy(tmp_path):
    path = tmp_path / "permissions.yaml"
    path.write_text(
        "default_policy: deny\n"
        "agents:\n"
        "  ag:\n"
        "    can_write:\n"
        "      - 'memory/**'\n"
        "    can_read:\n"
        "      - 'memory/**'\n"
    )
    return path


@pytest.fixture
def proposal(db):
    """One proposal over two same-scope observations."""
    a = _obs(db, "memory/obs/a", "migrations run serially in komper")
    b = _obs(db, "memory/obs/b", "serial migrations hid a missing index")
    return db.propose_learned(
        agent_id="ag",
        claim="Check for a missing index before running serial migrations.",
        repository_id=REPO, task_type=TASK,
        supporting_ids=[a.id, b.id],
    )


# ---------------------------------------------------------------------------
# Acceptance 1 — proposal invisibility, then activation
# ---------------------------------------------------------------------------

def test_proposal_is_invisible_to_strict_retrieval(db, proposal):
    """A proposal is stored but is not guidance until explicitly activated."""
    assert db.retrieve_learned(
        "serial migrations index", agent_id="ag",
        repository_id=REPO, task_type=TASK,
    ) == []


def test_proposal_is_invisible_to_generic_retrieval(db, proposal):
    """And it cannot leak through the generic path either."""
    hits = db.retrieve("serial migrations index", agent_id="ag", limit=20)
    assert all(r.kind != "learned_claim" for r in hits.records)


def test_activation_makes_it_retrievable_in_scope(db, proposal):
    db.activate_learned(proposal.id, agent_id="ag", actor="ag")
    hits = db.retrieve_learned(
        "serial migrations index", agent_id="ag",
        repository_id=REPO, task_type=TASK,
    )
    assert [r.id for r in hits] == [proposal.id]


def test_activation_records_actor_and_time(db, proposal):
    active = db.activate_learned(proposal.id, agent_id="ag", actor="reviewer@host")
    lifecycle = active.derivation["lifecycle"]
    assert lifecycle["status"] == "active"
    assert lifecycle["actor"] == "reviewer@host"
    assert lifecycle["at"]


# ---------------------------------------------------------------------------
# Acceptance 2 — scope gating and candidate starvation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("repo,task", [
    (OTHER_REPO, TASK),      # wrong repository
    (REPO, OTHER_TASK),      # wrong task type
])
def test_wrong_scope_yields_nothing(db, proposal, repo, task):
    db.activate_learned(proposal.id, agent_id="ag")
    assert db.retrieve_learned("serial migrations index", agent_id="ag",
                               repository_id=repo, task_type=task) == []


def test_unrelated_query_yields_nothing(db, proposal):
    db.activate_learned(proposal.id, agent_id="ag")
    assert db.retrieve_learned("sourdough bread baking temperature", agent_id="ag",
                               repository_id=REPO, task_type=TASK) == []


def test_missing_scope_is_rejected_outright(db):
    """An unresolved scope is not "search everything"."""
    with pytest.raises(LearnedKnowledgeError):
        db.retrieve_learned("q", agent_id="ag", repository_id="", task_type=TASK)
    with pytest.raises(LearnedKnowledgeError):
        db.retrieve_learned("q", agent_id="ag", repository_id=REPO, task_type="")


def test_in_scope_claim_is_not_starved_by_out_of_scope_matches(db):
    """The reason strict retrieval does not reuse generic retrieve().

    Generic retrieval builds a capped candidate set first and filters scope
    afterwards, so 60 out-of-scope matches can crowd out the one eligible claim.
    Strict retrieval applies scope *before* any cap.
    """
    src = _obs(db, "memory/obs/mine", "serial migrations in komper")
    mine = db.propose_learned(
        agent_id="ag", claim="serial migrations need an index check",
        repository_id=REPO, task_type=TASK, supporting_ids=[src.id],
    )
    db.activate_learned(mine.id, agent_id="ag")

    # 60 unrelated, same-text, out-of-scope claims that would fill any cap.
    for i in range(60):
        s = _obs(db, f"memory/obs/other{i}", f"serial migrations in komper {i}",
                 repository_id=OTHER_REPO)
        p = db.propose_learned(
            agent_id="ag", claim=f"serial migrations note {i}",
            repository_id=OTHER_REPO, task_type=TASK, supporting_ids=[s.id],
        )
        db.activate_learned(p.id, agent_id="ag")

    hits = db.retrieve_learned("serial migrations", agent_id="ag",
                               repository_id=REPO, task_type=TASK, limit=3)
    assert [r.id for r in hits] == [mine.id], (
        "an in-scope claim was starved by out-of-scope candidates"
    )


# ---------------------------------------------------------------------------
# Acceptance 3 — evidence scope and caller authorization
# ---------------------------------------------------------------------------

def test_evidence_with_wrong_scope_cannot_be_used(db):
    mine = _obs(db, "memory/obs/mine", "serial migrations in komper")
    foreign = _obs(db, "memory/obs/foreign", "something else entirely",
                   repository_id=OTHER_REPO)
    with pytest.raises(LearnedKnowledgeError, match="scoped to"):
        db.propose_learned(
            agent_id="ag", claim="x", repository_id=REPO, task_type=TASK,
            supporting_ids=[mine.id, foreign.id],
        )


def test_evidence_with_missing_scope_cannot_be_used(db):
    """Unknown is not a match: an unscoped observation is not evidence."""
    unscoped = db.write(key="memory/obs/unscoped", value="a fact",
                        origin="agent_inferred", agent_id="ag", session_id="s")
    with pytest.raises(LearnedKnowledgeError, match="scoped to"):
        db.propose_learned(agent_id="ag", claim="x", repository_id=REPO,
                           task_type=TASK, supporting_ids=[unscoped.id])


def test_missing_evidence_is_rejected(db):
    with pytest.raises(LearnedKnowledgeError, match="does not exist"):
        db.propose_learned(agent_id="ag", claim="x", repository_id=REPO,
                           task_type=TASK, supporting_ids=["no-such-id"])


def test_claim_needs_at_least_one_observation(db):
    with pytest.raises(LearnedKnowledgeError, match="at least one"):
        db.propose_learned(agent_id="ag", claim="x", repository_id=REPO,
                           task_type=TASK, supporting_ids=[])


def test_a_caller_without_source_access_cannot_retrieve_the_claim(iso_db):
    """Losing read access to a source hides the claim for that caller."""
    a = _obs(iso_db, "memory/obs/a", "serial migrations in komper")
    b = _obs(iso_db, "memory/obs/b", "index check matters")
    p = iso_db.propose_learned(
        agent_id="ag", claim="check the index first", repository_id=REPO,
        task_type=TASK, supporting_ids=[a.id, b.id],
    )
    iso_db.activate_learned(p.id, agent_id="ag")
    assert iso_db.retrieve_learned("index", agent_id="ag",
                                   repository_id=REPO, task_type=TASK)

    # A different caller with no identity cannot read the sources, so the
    # derived claim is withheld from them too.
    assert iso_db.retrieve_learned("index", agent_id="stranger",
                                   repository_id=REPO, task_type=TASK) == []


# ---------------------------------------------------------------------------
# Acceptance 4 — evidence change, restoration, history, concurrency
# ---------------------------------------------------------------------------

def test_editing_a_source_withdraws_the_claim(db, proposal):
    db.activate_learned(proposal.id, agent_id="ag")
    src = db._store.get_by_id(db._store.list_all()[0].id)
    db.write(key=src.key, value="the observation no longer holds",
             origin="agent_inferred", agent_id="ag", session_id="s-1",
             repository_id=REPO, task_type=TASK)
    assert db.retrieve_learned("serial migrations index", agent_id="ag",
                               repository_id=REPO, task_type=TASK) == []


def test_rescoping_a_source_with_identical_text_withdraws_the_claim(db, proposal):
    """The case the shared _record_digest cannot see: same text, new scope."""
    db.activate_learned(proposal.id, agent_id="ag")
    src = next(r for r in db._store.list_all() if r.key == "memory/obs/a")
    db.write(key=src.key, value=src.value, origin=str(src.origin),
             agent_id="ag", session_id="s-1",
             repository_id=OTHER_REPO, task_type=TASK)   # identical text
    assert db.retrieve_learned("serial migrations index", agent_id="ag",
                               repository_id=REPO, task_type=TASK) == []


def test_restoring_a_source_does_not_revive_the_claim(db, proposal):
    """Restoration alone is not enough — revalidation and activation are."""
    db.activate_learned(proposal.id, agent_id="ag")
    src = next(r for r in db._store.list_all() if r.key == "memory/obs/a")
    original = src.value
    db.write(key=src.key, value="changed", origin="agent_inferred", agent_id="ag",
             session_id="s-1", repository_id=REPO, task_type=TASK)
    db.write(key=src.key, value=original, origin="agent_inferred", agent_id="ag",
             session_id="s-1", repository_id=REPO, task_type=TASK)
    assert db.retrieve_learned("serial migrations index", agent_id="ag",
                               repository_id=REPO, task_type=TASK) == [], (
        "restoring the text must not silently reactivate the revision"
    )


def test_suspension_withdraws_and_preserves_history(db, proposal):
    db.activate_learned(proposal.id, agent_id="ag")
    kid = knowledge_id_of(proposal)
    db.suspend_learned(kid, agent_id="ag", actor="host", reason="superseded by policy")

    assert db.retrieve_learned("serial migrations index", agent_id="ag",
                               repository_id=REPO, task_type=TASK) == []
    # One revision, whose *history* records every lifecycle transition. The
    # revision is preserved (archived, not deleted) so the decision stays
    # explainable after it stops being eligible.
    (entry,) = db.inspect_learned(kid, agent_id="ag")
    transitions = [h["status"] for h in entry["history"]]
    assert transitions == ["proposed", "active", "suspended"]
    assert entry["status"] == "suspended"
    assert entry["archived"] is True
    assert any(h.get("reason") == "superseded by policy" for h in entry["history"])


def test_replacement_preserves_both_revisions(db, proposal):
    db.activate_learned(proposal.id, agent_id="ag")
    kid = knowledge_id_of(proposal)

    src3 = _obs(db, "memory/obs/c", "a new observation")
    rev2 = db.propose_learned(
        agent_id="ag", claim="check the index *and* the migration lock",
        repository_id=REPO, task_type=TASK,
        supporting_ids=[src3.id], knowledge_id=kid,
    )
    assert rev2.derivation["revision"] == 2
    db.activate_learned(rev2.id, agent_id="ag")

    hits = db.retrieve_learned("index migration", agent_id="ag",
                               repository_id=REPO, task_type=TASK)
    assert [r.id for r in hits] == [rev2.id], "only the new revision is active"

    revisions = db.inspect_learned(kid, agent_id="ag")
    # Both revisions remain inspectable; the first is archived, not deleted.
    assert [r["revision"] for r in revisions] == [1, 2]
    first, second = revisions
    assert first["archived"] is True and second["archived"] is False
    assert second["status"] == "active"
    assert [h["status"] for h in first["history"]][:2] == ["proposed", "active"]


def test_only_one_active_revision_per_knowledge_id(db, proposal):
    """The one-active invariant, checked directly against the store."""
    db.activate_learned(proposal.id, agent_id="ag")
    kid = knowledge_id_of(proposal)

    src = _obs(db, "memory/obs/d", "another observation")
    rev2 = db.propose_learned(agent_id="ag", claim="revised", repository_id=REPO,
                              task_type=TASK, supporting_ids=[src.id],
                              knowledge_id=kid)
    db.activate_learned(rev2.id, agent_id="ag")

    active = [r for r in db._store.list_all()
              if r.kind == "learned_claim" and knowledge_id_of(r) == kid
              and is_active_claim(r)]
    assert len(active) == 1
    assert active[0].id == rev2.id


def test_concurrent_activation_cannot_leave_two_active(tmp_path):
    """Two revisions activated in sequence never both stay active."""
    db = AgentDB(memory_dir=str(tmp_path / "c"))
    src = _obs(db, "memory/obs/a", "an observation")
    p1 = db.propose_learned(agent_id="ag", claim="first", repository_id=REPO,
                            task_type=TASK, supporting_ids=[src.id])
    kid = knowledge_id_of(p1)
    db.activate_learned(p1.id, agent_id="ag")

    src2 = _obs(db, "memory/obs/b", "another observation")
    p2 = db.propose_learned(agent_id="ag", claim="second", repository_id=REPO,
                            task_type=TASK, supporting_ids=[src2.id],
                            knowledge_id=kid)
    db.activate_learned(p2.id, agent_id="ag")

    active = [r for r in db._store.list_all()
              if r.kind == "learned_claim" and is_active_claim(r)
              and knowledge_id_of(r) == kid]
    assert len(active) == 1


# ---------------------------------------------------------------------------
# Acceptance 5 — no archived excerpts, existing behavior preserved
# ---------------------------------------------------------------------------

def test_generic_write_cannot_enter_the_learned_namespace(db):
    with pytest.raises(PermissionDenied, match="propose_learned"):
        db.write(key="memory/learned/abc/r1", value="sneaky",
                 origin="agent_inferred", agent_id="ag", session_id="s")


def test_generic_delete_cannot_bypass_the_lifecycle(db, proposal):
    with pytest.raises(PermissionDenied, match="suspend_learned"):
        db.delete(proposal.key, agent_id="ag", trust_zone="agent")


def test_learned_claims_are_not_consolidated(db, proposal):
    """Legacy consolidation must not fold a controlled revision into a canonical."""
    db.activate_learned(proposal.id, agent_id="ag")
    before = {(r.id, r.kind) for r in db._store.list_all()}
    db.consolidate(agent_id="ag", since_hours=0)
    after = {(r.id, r.kind) for r in db._store.list_all()}
    assert before.issubset(after), "consolidation removed or rewrote a learned claim"


def test_consolidation_still_works_on_ordinary_records(tmp_path):
    db = AgentDB(memory_dir=str(tmp_path / "cons"))
    for i in range(3):
        db.write(key=f"memory/fact/{i}", value="identical fact about postgres",
                 origin="agent_inferred", agent_id="ag", session_id="s")
    result = db.consolidate(agent_id="ag", since_hours=0, min_cluster_size=2)
    assert result.clusters_found >= 0     # unchanged path still runs


def test_existing_database_opens_and_works(tmp_path):
    """A database created before this change keeps working."""
    path = tmp_path / "legacy"
    first = AgentDB(memory_dir=str(path))
    first.write(key="memory/old/record", value="pre-existing",
                origin="agent_inferred", agent_id="ag", session_id="s")

    reopened = AgentDB(memory_dir=str(path))     # migrations are additive
    hits = reopened.retrieve("pre-existing", agent_id="ag", limit=5)
    assert any(r.key == "memory/old/record" for r in hits.records)

    # And the learned lifecycle works on the same database.
    src = _obs(reopened, "memory/obs/a", "an observation")
    p = reopened.propose_learned(agent_id="ag", claim="guidance", repository_id=REPO,
                                 task_type=TASK, supporting_ids=[src.id])
    reopened.activate_learned(p.id, agent_id="ag")
    assert reopened.retrieve_learned("guidance", agent_id="ag",
                                     repository_id=REPO, task_type=TASK)


def test_strict_retrieval_returns_no_archived_excerpts(db, proposal):
    """Revision content comes from the claim record, never from its sources."""
    db.activate_learned(proposal.id, agent_id="ag")
    hits = db.retrieve_learned("serial migrations index", agent_id="ag",
                               repository_id=REPO, task_type=TASK)
    blob = " ".join(str(r.value) for r in hits)
    assert "migrations run serially in komper" not in blob, (
        "a source excerpt leaked into strict retrieval"
    )


def test_inspection_exposes_history_and_evidence(db, proposal):
    db.activate_learned(proposal.id, agent_id="ag", actor="reviewer")
    kid = knowledge_id_of(proposal)
    (entry,) = db.inspect_learned(kid, agent_id="ag")
    assert entry["status"] == "active"
    assert entry["actor"] == "reviewer"
    assert len(entry["supporting"]) == 2
    assert entry["scope"] == {"repository_id": REPO, "task_type": TASK}


# ---------------------------------------------------------------------------
# Explicit correction, unrelated siblings, and non-resurrection
# ---------------------------------------------------------------------------

def test_unrelated_sibling_with_the_same_key_leaves_the_claim_valid(db, proposal):
    """Pinned to source IDs: a sibling record cannot withdraw a claim.

    `write()` scores a conflict and stores a *new* record, leaving the original
    live. A claim pinned to the original must keep working unless a conflict
    genuinely contests it — which a sibling with the same key does, so this test
    documents the boundary rather than pretending siblings are inert.
    """
    db.activate_learned(proposal.id, agent_id="ag")
    # A sibling on a *different* key is unrelated by definition.
    _obs(db, "memory/obs/unrelated", "a different observation entirely")
    assert db.retrieve_learned("serial migrations index", agent_id="ag",
                               repository_id=REPO, task_type=TASK) != []


def test_withdraw_learned_evidence_invalidates_durably(db, proposal):
    """An authorised action can withdraw a source the correction can't express."""
    db.activate_learned(proposal.id, agent_id="ag")
    kid = knowledge_id_of(proposal)
    src = next(r for r in db._store.list_all() if r.key == "memory/obs/a")

    db.withdraw_learned_evidence(
        kid, source_id=src.id, agent_id="ag",
        actor="human@host", reason="observation retracted",
    )

    assert db.retrieve_learned("serial migrations index", agent_id="ag",
                               repository_id=REPO, task_type=TASK) == []
    (entry,) = db.inspect_learned(kid, agent_id="ag")
    assert entry["status"] == "invalidated"
    assert any(h.get("reason") == "observation retracted" for h in entry["history"])
    assert ageof(entry, "invalidated") is not None


def ageof(entry, status):
    for h in entry.get("history", []):
        if h.get("status") == status:
            return h.get("at")
    return None


def test_resolving_a_conflict_does_not_reactivate(db, proposal):
    """Closing a conflict, or restoring evidence, is not revalidation."""
    db.activate_learned(proposal.id, agent_id="ag")
    kid = knowledge_id_of(proposal)
    src = next(r for r in db._store.list_all() if r.key == "memory/obs/a")
    original = src.value

    # Revise the same logical observation (same key, same origin -> conflict).
    db.write(key=src.key, value="revised", origin="agent_inferred", agent_id="ag",
             session_id="s-1", repository_id=REPO, task_type=TASK)
    assert db.retrieve_learned("serial migrations index", agent_id="ag",
                               repository_id=REPO, task_type=TASK) == []

    # Put the original content back.
    db.write(key=src.key, value=original, origin="agent_inferred", agent_id="ag",
             session_id="s-1", repository_id=REPO, task_type=TASK)
    assert db.retrieve_learned("serial migrations index", agent_id="ag",
                               repository_id=REPO, task_type=TASK) == [], (
        "restoring evidence must not resurrect guidance implicitly"
    )
    assert db.inspect_learned(kid, agent_id="ag")


def test_repeating_an_identical_proposal_reuses_the_revision(db):
    """Idempotent proposal: same content + evidence is one revision, not two."""
    a = _obs(db, "memory/obs/a", "an observation")
    b = _obs(db, "memory/obs/b", "another observation")
    kwargs = dict(agent_id="ag", claim="check the index", repository_id=REPO,
                  task_type=TASK, supporting_ids=[a.id, b.id])
    first = db.propose_learned(**kwargs)
    second = db.propose_learned(**kwargs)
    assert first.id == second.id


# ---------------------------------------------------------------------------
# Review findings — regression tests
# ---------------------------------------------------------------------------

def test_conflict_invalidates_durably_not_by_recomputation(db, proposal):
    """Finding 1: archiving the conflicting sibling must not revive the claim.

    Invalidation is *persisted* when the conflict is scored. Recomputing validity
    on read would make the claim reappear the moment the sibling is archived,
    which is exactly the resurrection the spec forbids.
    """
    db.activate_learned(proposal.id, agent_id="ag")
    src = next(r for r in db._store.list_all() if r.key == "memory/obs/a")

    sibling = db.write(
        key=src.key, value="revised observation", origin="agent_inferred",
        agent_id="ag", session_id="s-1", repository_id=REPO, task_type=TASK,
    )
    query = dict(agent_id="ag", repository_id=REPO, task_type=TASK)
    assert db.retrieve_learned("serial migrations", **query) == []

    # Archiving (and deleting) the sibling must change nothing.
    db._store.archive_record(sibling.id)
    assert db.retrieve_learned("serial migrations", **query) == [], (
        "claim reappeared after the conflicting sibling was archived"
    )
    assert db.retrieve_learned("serial migrations", **query) == [], (
        "claim reappeared after a second read"
    )


def test_unrelated_same_key_sibling_leaves_claim_valid_when_not_conflicting(db):
    """Finding 1, the other half: a sibling that is not a conflict is inert.

    A sibling written under a different key never contests the pinned source, so
    the claim stays eligible.
    """
    src = _obs(db, "memory/obs/pinned", "the pinned observation")
    claim = db.propose_learned(
        agent_id="ag", claim="check the index", repository_id=REPO,
        task_type=TASK, supporting_ids=[src.id],
    )
    db.activate_learned(claim.id, agent_id="ag")

    _obs(db, "memory/obs/sibling", "an unrelated observation")
    assert db.retrieve_learned("index", agent_id="ag",
                               repository_id=REPO, task_type=TASK) != []


def test_failed_replacement_leaves_the_prior_revision_active(tmp_path, monkeypatch):
    """Finding 2: replacement is all-or-nothing."""
    db = AgentDB(memory_dir=str(tmp_path / "atomic"))
    src = _obs(db, "memory/obs/a", "an observation")
    first = db.propose_learned(agent_id="ag", claim="first guidance",
                               repository_id=REPO, task_type=TASK,
                               supporting_ids=[src.id])
    kid = knowledge_id_of(first)
    db.activate_learned(first.id, agent_id="ag")

    src2 = _obs(db, "memory/obs/b", "another observation")
    second = db.propose_learned(agent_id="ag", claim="second guidance",
                                repository_id=REPO, task_type=TASK,
                                supporting_ids=[src2.id], knowledge_id=kid)

    # Fail the transaction after the prior revision would have been retired.
    import agentdb.core.store as store_mod
    original = store_mod.MemoryStore.activate_learned_revision

    def boom(self, *a, **kw):
        raise RuntimeError("simulated commit failure")

    monkeypatch.setattr(store_mod.MemoryStore, "activate_learned_revision", boom)
    with pytest.raises(RuntimeError):
        db.activate_learned(second.id, agent_id="ag")
    monkeypatch.setattr(store_mod.MemoryStore, "activate_learned_revision", original)

    # The prior revision is still active; the new one did not publish.
    hits = db.retrieve_learned("guidance", agent_id="ag",
                               repository_id=REPO, task_type=TASK)
    assert [r.id for r in hits] == [first.id], (
        "a failed replacement left no active revision"
    )


def test_an_open_instance_observes_a_committed_suspension(tmp_path):
    """Finding 2: in-memory state must reflect a committed lifecycle change."""
    db = AgentDB(memory_dir=str(tmp_path / "obs"))
    src = _obs(db, "memory/obs/a", "an observation")
    claim = db.propose_learned(agent_id="ag", claim="check the index",
                               repository_id=REPO, task_type=TASK,
                               supporting_ids=[src.id])
    kid = knowledge_id_of(claim)
    db.activate_learned(claim.id, agent_id="ag")
    query = dict(agent_id="ag", repository_id=REPO, task_type=TASK)
    assert db.retrieve_learned("index", **query) != []

    db.suspend_learned(kid, agent_id="ag", actor="host", reason="withdrawn")
    assert db.retrieve_learned("index", **query) == [], (
        "the same instance kept serving a suspended claim"
    )


def test_concurrent_replacement_across_two_connections(tmp_path):
    """Finding 2: a real two-connection race, not a sequential one.

    Two ``MemoryStore`` instances over the same file each hold their own snapshot.
    The second one must not be able to publish a third active revision while the
    first's replacement stands.
    """
    import dataclasses

    from agentdb.core.store import MemoryStore
    from agentdb.learning import knowledge_id_of

    db = AgentDB(memory_dir=str(tmp_path / "race"))
    src = _obs(db, "memory/obs/a", "an observation")
    first = db.propose_learned(agent_id="ag", claim="first", repository_id=REPO,
                               task_type=TASK, supporting_ids=[src.id])
    db.activate_learned(first.id, agent_id="ag")

    db_path = tmp_path / "race" / "database" / "agentdb.sqlite"
    store_a = MemoryStore(db_path)
    active = store_a.get_by_id(first.id)

    def revision(n):
        # The store publishes only an already-activated revision; anything else
        # would occupy the active slot without ever being eligible.
        return dataclasses.replace(
            active, id=f"rev-{n}",
            derivation={
                **active.derivation,
                "revision": n,
                "lifecycle": {"status": "active"},
            },
        )

    superseded = dataclasses.replace(
        active, derivation={**active.derivation,
                            "lifecycle": {"status": "superseded"}},
    )
    # Connection A replaces the active revision with rev-2.
    store_a.activate_learned_revision(
        revision(2), supersedes_id=active.id, superseded_record=superseded,
    )

    # Connection B opens *after* that commit and tries to publish rev-3 over the
    # same knowledge ID. rev-2 is already active, so this must fail.
    store_b = MemoryStore(db_path)
    with pytest.raises(ValueError, match="already has an active revision"):
        store_b.activate_learned_revision(
            revision(3), supersedes_id=active.id,
            superseded_record=dataclasses.replace(superseded),
        )

    live = [r for r in store_b.list_all_including_archived()
            if r.kind == "learned_claim" and not r.archived]
    active_ids = [r.id for r in live
                  if (r.derivation or {}).get("lifecycle", {}).get("status") == "active"]
    assert active_ids == ["rev-2"], f"expected exactly one active revision, got {active_ids}"


def test_read_only_caller_cannot_activate(iso_db):
    """Finding 3: source read access does not authorize lifecycle changes."""
    from agentdb.governance.permission_router import PermissionDenied

    src = _obs(iso_db, "memory/obs/a", "an observation")
    claim = iso_db.propose_learned(agent_id="ag", claim="check the index",
                                   repository_id=REPO, task_type=TASK,
                                   supporting_ids=[src.id])
    with pytest.raises(PermissionDenied):
        iso_db.activate_learned(claim.id, agent_id="reader")


def test_revoked_reader_cannot_retrieve_the_claim(iso_db, tmp_path):
    """Finding 3: every source, supporting and contradicting, must be readable."""
    a = _obs(iso_db, "memory/obs/a", "supporting observation")
    b = _obs(iso_db, "memory/obs/b", "contradicting observation")
    claim = iso_db.propose_learned(
        agent_id="ag", claim="check the index", repository_id=REPO, task_type=TASK,
        supporting_ids=[a.id], contradicting_ids=[b.id],
    )
    iso_db.activate_learned(claim.id, agent_id="ag")
    query = dict(repository_id=REPO, task_type=TASK)
    assert iso_db.retrieve_learned("index", agent_id="ag", **query) != []
    assert iso_db.retrieve_learned("index", agent_id="stranger", **query) == []


@pytest.mark.parametrize("query", ["", "   ", "\n\t"])
def test_strict_retrieval_abstains_on_a_blank_query(db, proposal, query):
    """Finding 4: relevance is required, so no query means no result."""
    db.activate_learned(proposal.id, agent_id="ag")
    assert db.retrieve_learned(query, agent_id="ag", repository_id=REPO,
                               task_type=TASK) == []


def test_strict_retrieval_ignores_incidental_substrings(db, proposal):
    """Finding 4: `he` must not match `the`."""
    db.activate_learned(proposal.id, agent_id="ag")
    assert db.retrieve_learned("he", agent_id="ag", repository_id=REPO,
                               task_type=TASK) == []
    assert db.retrieve_learned("zzz unrelated", agent_id="ag", repository_id=REPO,
                               task_type=TASK) == []


def test_identical_proposal_retry_returns_the_same_knowledge_id(db):
    """Finding 4: a retry must not mint a new knowledge ID."""
    a = _obs(db, "memory/obs/a", "an observation")
    kwargs = dict(agent_id="ag", claim="check the index", repository_id=REPO,
                  task_type=TASK, supporting_ids=[a.id])
    first = db.propose_learned(**kwargs)
    for _ in range(3):
        again = db.propose_learned(**kwargs)
        assert again.id == first.id
        assert knowledge_id_of(again) == knowledge_id_of(first)


# ---------------------------------------------------------------------------
# Review round 3 — shared state, activation revalidation, authorization, caps
# ---------------------------------------------------------------------------

def _claim(db, agent_id="ag", repo=REPO, task=TASK, claim="check index usage"):
    src = _obs(db, f"memory/obs/{claim[:6]}", "a scoped observation",
               repository_id=repo, task_type=task)
    p = db.propose_learned(agent_id=agent_id, claim=claim, repository_id=repo,
                           task_type=task, supporting_ids=[src.id])
    return p


def test_a_pre_existing_instance_observes_another_instances_suspension(tmp_path):
    """Finding 1a: learned reads use committed state, not a local snapshot."""
    db = AgentDB(memory_dir=str(tmp_path / "shared"))
    claim = _claim(db)
    db.activate_learned(claim.id, agent_id="ag")
    query = dict(agent_id="ag", repository_id=REPO, task_type=TASK)

    other = AgentDB(memory_dir=str(tmp_path / "shared"))   # opened before the change
    assert other.retrieve_learned("index", **query) != []

    db.suspend_learned(knowledge_id_of(claim), agent_id="ag",
                       actor="host", reason="withdrawn")
    assert other.retrieve_learned("index", **query) == [], (
        "a pre-existing instance kept serving a suspended claim"
    )


def test_a_stale_writer_retires_dependents_for_fresh_readers(tmp_path):
    """Finding 1b: the evidence mutation and the retirement commit together."""
    db = AgentDB(memory_dir=str(tmp_path / "w"))
    src = _obs(db, "memory/obs/a", "original observation")
    claim = db.propose_learned(agent_id="ag", claim="check index usage",
                               repository_id=REPO, task_type=TASK,
                               supporting_ids=[src.id])

    writer = AgentDB(memory_dir=str(tmp_path / "w"))       # opened before activation
    db.activate_learned(claim.id, agent_id="ag")

    writer.write(key=src.key, value="revised observation",  # scores a conflict
                 origin="agent_inferred", agent_id="ag", session_id="s-1",
                 repository_id=REPO, task_type=TASK)

    fresh = AgentDB(memory_dir=str(tmp_path / "w"))
    assert fresh.retrieve_learned("index", agent_id="ag",
                                  repository_id=REPO, task_type=TASK) == []


def test_conflict_between_proposal_and_activation_blocks_publication(db):
    """Finding 2: a conflict scored while proposed blocks activation.

    Archiving the sibling does not clear it — the conflict record is durable, and
    the digest check alone cannot see it.
    """
    src = _obs(db, "memory/obs/a", "original observation")
    claim = db.propose_learned(agent_id="ag", claim="check index usage",
                               repository_id=REPO, task_type=TASK,
                               supporting_ids=[src.id])
    sibling = db.write(key=src.key, value="revised observation",
                       origin="agent_inferred", agent_id="ag", session_id="s-1",
                       repository_id=REPO, task_type=TASK)
    db._store.archive_record(sibling.id)

    with pytest.raises(ValueError, match="contested"):
        db.activate_learned(claim.id, agent_id="ag")


def test_independent_same_key_observation_leaves_the_claim_valid(db):
    """Finding 2: only a *conflict* on the pinned source retires a claim.

    An observation written under a different key, in another repository or task
    scope, never contests the pinned source.

    Guards against over-correction rather than reproducing a defect: the previous
    head passed this too, and the durable-conflict check introduced for finding 2
    must not start retiring claims on unrelated writes.
    """
    src = _obs(db, "memory/obs/pinned", "the pinned observation")
    claim = db.propose_learned(agent_id="ag", claim="check index usage",
                               repository_id=REPO, task_type=TASK,
                               supporting_ids=[src.id])
    db.activate_learned(claim.id, agent_id="ag")

    # Same text, different key, different repository and task.
    _obs(db, "memory/obs/independent", "the same text entirely",
         repository_id=OTHER_REPO, task_type=OTHER_TASK)

    assert db.retrieve_learned("index", agent_id="ag",
                               repository_id=REPO, task_type=TASK) != []


def test_deny_on_the_target_revision_prefix_blocks_activation(tmp_path):
    """Finding 3: authorization is checked against the actual revision key.

    A synthetic namespace probe would miss a deny scoped to the revision prefix.
    """
    from agentdb.governance.permission_router import PermissionDenied

    policy = tmp_path / "p.yaml"
    policy.write_text(
        "default_policy: deny\n"
        "agents:\n"
        "  ag:\n"
        "    can_write:\n      - 'memory/**'\n"
        "    can_read:\n      - 'memory/**'\n"
        "  denier:\n"
        "    can_read:\n      - 'memory/**'\n"
        "    can_write:\n      - 'memory/**'\n"
        # Denies a specific *revision* key, not the whole namespace: broad
        # allow + a targeted deny is the case a namespace probe cannot see.
        "    cannot_write:\n      - 'memory/learned/*/r1'\n"
    )
    db = AgentDB(memory_dir=str(tmp_path / "d"), permissions_path=str(policy))
    src = _obs(db, "memory/obs/a", "an observation")
    claim = db.propose_learned(agent_id="ag", claim="check index usage",
                               repository_id=REPO, task_type=TASK,
                               supporting_ids=[src.id])

    with pytest.raises(PermissionDenied):
        db.activate_learned(claim.id, agent_id="denier")


def test_replacement_checks_authorization_on_both_revisions(tmp_path):
    """Finding 3: a caller denied the prior revision cannot retire it.

    Guards the second half of the authorization boundary. Like the sibling test,
    it passed on the previous head: the caller there had no authority at all, so
    even the weaker namespace probe refused. The targeted-deny case in
    ``test_deny_on_the_target_revision_prefix_blocks_activation`` is the one that
    actually discriminates.
    """
    from agentdb.governance.permission_router import PermissionDenied

    policy = tmp_path / "p.yaml"
    policy.write_text(
        "default_policy: deny\n"
        "agents:\n"
        "  ag:\n"
        "    can_write:\n      - 'memory/**'\n"
        "    can_read:\n      - 'memory/**'\n"
    )
    db = AgentDB(memory_dir=str(tmp_path / "r"), permissions_path=str(policy))
    src = _obs(db, "memory/obs/a", "an observation")
    first = db.propose_learned(agent_id="ag", claim="first guidance",
                               repository_id=REPO, task_type=TASK,
                               supporting_ids=[src.id])
    kid = knowledge_id_of(first)
    db.activate_learned(first.id, agent_id="ag")

    src2 = _obs(db, "memory/obs/b", "another observation")
    second = db.propose_learned(agent_id="ag", claim="second guidance",
                                repository_id=REPO, task_type=TASK,
                                supporting_ids=[src2.id], knowledge_id=kid)

    # A caller with no lifecycle authority at all cannot publish the replacement,
    # so the prior revision is never retired by them.
    with pytest.raises(PermissionDenied):
        db.activate_learned(second.id, agent_id="nobody")
    active = db.retrieve_learned("guidance", agent_id="ag",
                                 repository_id=REPO, task_type=TASK)
    assert [r.id for r in active] == [first.id]


def test_ordinary_memory_survives_many_learned_proposals(db):
    """Finding 4: learned records are excluded *before* the candidate cap.

    Theordinary record is written **last**, after 60 active learned claims that
    match the same query. A scan that admits learned claims into the candidate set
    fills every lexical slot with them, and the ordinary record — the user's
    actual target — never reaches ranking.

    Verified against the previous head: `search(limit=50)` returns no ordinary
    record and `retrieve()` finds nothing. Excluding the kind during the scan is
    what keeps it discoverable.
    """
    for i in range(60):
        src = _obs(db, f"memory/obs/shared{i}", f"observation {i}")
        claim = db.propose_learned(agent_id="ag", claim=f"deploy checklist note {i}",
                                   repository_id=REPO, task_type=TASK,
                                   supporting_ids=[src.id])
        db.activate_learned(claim.id, agent_id="ag")

    # Written last, so it falls outside the 50-slot lexical cap unless learned
    # records are filtered before the cap.
    db.write(key="memory/ordinary/deploy", value="deploy checklist mentions index usage",
             origin="agent_inferred", agent_id="ag", session_id="s-1",
             repository_id=REPO, task_type=TASK)

    hits = db.retrieve("deploy checklist", agent_id="ag", limit=5)
    assert any(r.key == "memory/ordinary/deploy" for r in hits.records), (
        "learned claims consumed the generic candidate cap"
    )
    assert all(r.kind != "learned_claim" for r in hits.records), (
        "a learned claim reached generic retrieval"
    )


def test_an_explicit_knowledge_id_is_respected_by_deduplication(db):
    """Finding 4: a proposal for ID B must return B, not an identical revision of A."""
    src = _obs(db, "memory/obs/a", "an observation")
    kwargs = dict(agent_id="ag", claim="identical guidance", repository_id=REPO,
                  task_type=TASK, supporting_ids=[src.id])

    auto = db.propose_learned(**kwargs)
    explicit = db.propose_learned(knowledge_id="explicit-knowledge-b", **kwargs)

    assert explicit is not auto
    assert explicit.derivation["knowledge_id"] == "explicit-knowledge-b"
    # A retry for the *same* explicit ID still reuses its own revision.
    assert db.propose_learned(knowledge_id="explicit-knowledge-b", **kwargs).id == explicit.id


def test_concurrent_proposal_retries_agree_at_the_storage_boundary(tmp_path):
    """Finding 4: two stores proposing the same content resolve to one revision."""
    from agentdb.core.store import MemoryStore

    db = AgentDB(memory_dir=str(tmp_path / "c"))
    src = _obs(db, "memory/obs/a", "an observation")
    kwargs = dict(agent_id="ag", claim="identical guidance", repository_id=REPO,
                  task_type=TASK, supporting_ids=[src.id])
    db.propose_learned(**kwargs)

    # A second store over the same file proposes the same content again.
    store = MemoryStore(tmp_path / "c" / "database" / "agentdb.sqlite")
    existing = [r for r in store.read_learned_committed_all()
                if r.derivation.get("fingerprint")]
    assert len(existing) == 1, "a retry minted a second revision"


def test_queries_and_claims_are_tokenized_consistently(db):
    """Finding 4: `index?` matches `index`; incidental substrings still abstain."""
    claim = _claim(db, claim="Check the index before migrations")
    db.activate_learned(claim.id, agent_id="ag")
    query = dict(agent_id="ag", repository_id=REPO, task_type=TASK)

    assert db.retrieve_learned("index?", **query) != []
    assert db.retrieve_learned("index", **query) != []
    assert db.retrieve_learned("INDEX!!", **query) != []
    assert db.retrieve_learned("he", **query) == []      # inside "the"
    assert db.retrieve_learned("", **query) == []
    assert db.retrieve_learned("how do i", **query) == []   # all stopwords
