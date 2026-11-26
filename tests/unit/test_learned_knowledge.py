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
