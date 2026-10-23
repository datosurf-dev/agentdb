# Contributing to AgentDB

## Development setup

```bash
git clone https://github.com/datosurf-dev/agentdb.git
cd agentdb
pip install -e ".[dev]"
```

## Running tests

```bash
pytest
```

Slow tests (require a model download) are marked and skipped by default:

```bash
pytest -m "not slow"   # fast only
pytest                  # all tests
```

## Commit style

Use [conventional commits](https://www.conventionalcommits.org/):

- `feat:` — new backward-compatible feature
- `fix:` — bug fix
- `chore:` — maintenance (deps, version bumps, CI)
- `docs:` — documentation only

## Pull requests

- One thematic change per PR
- Include tests for new behavior
- Keep PRs small and mergeable on their own

## Releasing

See [RELEASING.md](RELEASING.md).
