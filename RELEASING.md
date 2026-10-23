# Releasing

Releases are published to PyPI automatically via GitHub Actions using [trusted publishing](https://docs.pypi.org/trusted-publishers/) (OIDC — no API key needed).

## Steps

1. **Bump the version** in `pyproject.toml`:
   ```toml
   version = "0.3.0"
   ```

2. **Commit and merge to main** via PR:
   ```bash
   git add pyproject.toml
   git commit -m "chore: bump version to 0.3.0"
   ```

3. **Tag the merge commit and push the tag:**
   ```bash
   git tag v0.3.0
   git push origin v0.3.0
   ```

4. The `publish.yml` workflow triggers automatically, builds the package, and publishes to PyPI.

## Version guidelines (semver)

| Change type | Bump |
|---|---|
| Bug fixes only | PATCH (`x.x.1`) |
| New backward-compatible features or APIs | MINOR (`x.1.x`) |
| Breaking changes | MAJOR (`1.x.x`) |
