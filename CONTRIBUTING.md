# Contributing to AgentDB

## Releasing

Releases are published to PyPI automatically via GitHub Actions when a version tag is pushed. No manual `twine` upload needed.

```bash
# 1. Bump version in pyproject.toml
# 2. Commit, tag, and push
git add pyproject.toml
git commit -m "chore: bump version to X.Y.Z"
git tag vX.Y.Z
git push origin main && git push origin vX.Y.Z
```

The `publish.yml` workflow triggers on `v*` tags, builds the distribution, and uploads to PyPI using a stored `PYPI_API_TOKEN` secret. Create the GitHub release separately with `gh release create vX.Y.Z`.
