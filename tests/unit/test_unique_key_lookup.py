"""Unique key lookup: exactly one readable live record, or nothing.

A repeat `write()` on an existing key stores a sibling and leaves the original
live, so a key can have several live records and `read()` returns whichever it
encounters first — with no indication a choice was made. A caller that treats a
key as a configured pointer ("the guidance for this repository") cannot rely on
that.

`select_unique()` returns the record only when exactly one live record exists for
the key *and* the caller may read it. Uniqueness and the record are resolved from a
single committed read, so a concurrent write cannot land between the two, and a
withdrawal committed elsewhere is observed on the next lookup.
"""

import pytest

from agentdb import AgentDB

KEY = "memory/guidance/komper/bug-fix"


def _db(tmp_path, *, policy=None):
    if policy is None:
        return AgentDB(memory_dir=str(tmp_path / "db"))
    path = tmp_path / "permissions.yaml"
    path.write_text(policy)
    return AgentDB(memory_dir=str(tmp_path / "db"), permissions_path=str(path))


def _write(db, *, value="Verify the upgrade path.", repo="komper", agent_id="maint"):
    return db.write(
        key=KEY, value=value, origin="human_approved", agent_id=agent_id,
        repository_id=repo, task_type="bug_fix",
    )


# ---------------------------------------------------------------------------
# The three outcomes
# ---------------------------------------------------------------------------

def test_exactly_one_record_is_returned(tmp_path):
    db = _db(tmp_path)
    _write(db)
    record = db.select_unique(KEY)
    assert record is not None
    assert record.value == "Verify the upgrade path."


def test_a_missing_key_returns_nothing(tmp_path):
    db = _db(tmp_path)
    assert db.select_unique(KEY) is None


def test_two_live_siblings_are_ambiguous_and_return_nothing(tmp_path):
    """The case the lookup exists for: a repeat write leaves the original live."""
    db = _db(tmp_path)
    _write(db, value="for komper", repo="komper")
    _write(db, value="for another repo", repo="another")

    # read() still returns one of them, arbitrarily.
    assert db.read(KEY) is not None
    # The unique lookup declines to choose.
    assert db.select_unique(KEY) is None


def test_archiving_a_sibling_restores_uniqueness(tmp_path):
    db = _db(tmp_path)
    _write(db, value="for komper", repo="komper")
    sibling = _write(db, value="for another repo", repo="another")
    assert db.select_unique(KEY) is None

    db._store.archive_record(sibling.id)
    record = db.select_unique(KEY)
    assert record is not None and record.value == "for komper"


def test_a_denied_read_returns_nothing(tmp_path):
    policy = (
        "default_policy: deny\n"
        "agents:\n"
        "  owner:\n"
        "    can_read:\n      - 'memory/**'\n"
        "    can_write:\n      - 'memory/**'\n"
        "  reader:\n"
        "    can_read:\n      - 'memory/elsewhere/**'\n"
    )
    db = _db(tmp_path, policy=policy)
    _write(db, agent_id="owner")

    assert db.select_unique(KEY, agent_id="owner") is not None
    # A caller who may not read it gets nothing — same answer as "absent".
    assert db.select_unique(KEY, agent_id="reader") is None
    assert db.select_unique(KEY, agent_id=None) is None


def test_denial_is_not_reported_as_ambiguity(tmp_path):
    """Two live records the caller cannot read is still simply "nothing"."""
    policy = (
        "default_policy: deny\n"
        "agents:\n"
        "  owner:\n"
        "    can_read:\n      - 'memory/**'\n"
        "    can_write:\n      - 'memory/**'\n"
    )
    db = _db(tmp_path, policy=policy)
    _write(db, value="one", agent_id="owner")
    _write(db, value="two", agent_id="owner")

    assert db.select_unique(KEY, agent_id="owner") is None
    assert db.select_unique(KEY, agent_id="stranger") is None


# ---------------------------------------------------------------------------
# Committed state: two already-open instances
# ---------------------------------------------------------------------------

def test_a_committed_sibling_write_is_observed_by_an_open_instance(tmp_path):
    """An instance opened first must see a sibling committed by another."""
    writer = _db(tmp_path)
    _write(writer)
    reader = AgentDB(memory_dir=str(tmp_path / "db"))     # opened before the sibling
    assert reader.select_unique(KEY) is not None

    # A second instance adds a sibling under the same key.
    other = AgentDB(memory_dir=str(tmp_path / "db"))
    _write(other, value="a sibling", repo="another")

    assert reader.select_unique(KEY) is None, (
        "an already-open instance did not observe the committed sibling"
    )


def test_a_committed_withdrawal_is_observed_by_an_open_instance(tmp_path):
    """And a withdrawal committed elsewhere takes effect on the next lookup."""
    db = _db(tmp_path)
    _write(db)
    reader = AgentDB(memory_dir=str(tmp_path / "db"))
    assert reader.select_unique(KEY) is not None

    db.delete(KEY, agent_id="maint", trust_zone="human_approved")

    assert reader.select_unique(KEY) is None, (
        "an already-open instance kept serving a withdrawn record"
    )


def test_uniqueness_and_the_record_come_from_one_read(tmp_path):
    """A sibling appearing between a count and a fetch cannot be missed.

    The store resolves both from a single SELECT, so there is no window to land
    in. This guards the implementation shape rather than a reachable race: it
    asserts the method does exactly one store call.
    """
    db = _db(tmp_path)
    _write(db)
    calls = []
    original = db._store.select_unique_live_by_key

    def counted(key):
        calls.append(key)
        return original(key)

    db._store.select_unique_live_by_key = counted
    db.select_unique(KEY)
    assert calls == [KEY]


# ---------------------------------------------------------------------------
# Existing read/write behavior is unchanged
# ---------------------------------------------------------------------------

def test_read_still_returns_a_record_when_ambiguous(tmp_path):
    """read() is untouched: it returns one record, as before."""
    db = _db(tmp_path)
    _write(db, value="one")
    _write(db, value="two", repo="another")
    assert db.read(KEY) is not None


def test_write_still_creates_siblings(tmp_path):
    db = _db(tmp_path)
    _write(db, value="one")
    _write(db, value="two", repo="another")
    live = [r for r in db._store.list_all() if r.key == KEY]
    assert len(live) == 2
