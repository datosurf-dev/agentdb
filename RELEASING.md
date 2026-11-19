# Releasing

Releases are published to PyPI automatically via GitHub Actions using [trusted publishing](https://docs.pypi.org/trusted-publishers/) (OIDC — no API key needed).

## Steps

1. **Bump the version** in `pyproject.toml`:
   ```toml
   version = "0.4.0"
   ```

2. **Commit and merge to main** via PR:
   ```bash
   git add pyproject.toml
   git commit -m "chore: bump version to 0.4.0"
   ```

3. **Tag the merge commit and push the tag:**
   ```bash
   git tag v0.4.0
   git push origin v0.4.0
   ```

4. The `publish.yml` workflow triggers automatically, builds the package, and publishes to PyPI.

## Version guidelines

AgentDB is **pre-1.0** (`0.y.z`). While the major version is zero the public API
is still under active design, so anything may change between releases. This
follows semver §4: *"Major version zero is for initial development. Anything
MAY change at any time."*

### Pre-1.0 (current): breaking changes bump MINOR

| Change type | Bump | Example |
|---|---|---|
| Bug fixes only | PATCH | `0.4.0` → `0.4.1` |
| New features or APIs (backward compatible) | MINOR | `0.4.0` → `0.5.0` |
| **Breaking changes** (removals, renames, signature changes) | **MINOR** | `0.4.0` → `0.5.0` |
| Dropping Python versions / major storage-format breaks | MINOR + release note | — |

Breaking changes **must** be called out explicitly in the commit message and the
release notes, since the version number alone no longer signals them.

### After 1.0: standard semver

| Change type | Bump |
|---|---|
| Bug fixes only | PATCH (`x.x.1`) |
| New backward-compatible features or APIs | MINOR (`x.1.x`) |
| Breaking changes | MAJOR (`1.x.x`) |

Promoting to `1.0.0` is a deliberate commitment to API stability — do not do it
by accident as part of a routine release.

### Precedent

| Tag | Content |
|---|---|
| `v0.1.0` | Initial extraction |
| `v0.2.0` | Model-agnostic runtime spec |
| `v0.3.0` | Release 0.3.0 |
| `v0.4.0` | **Breaking**: `FIREWORKS_CAPABILITIES` → `SERVERLESS_CAPABILITIES`. Adds `ingest_turns()` / `purge_session()`, `memories.session_id`, cache-reload fix |
