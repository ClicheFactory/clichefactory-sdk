# Releasing `clichefactory`

Checklist for cutting a new release of the SDK to PyPI and GitHub.
Versioning is SemVer (pre-1.0: minor for additive API, patch for bugfix/docs).

The single source of truth for the version is `clichefactory/__about__.py`.
`pyproject.toml` reads it dynamically via `[tool.hatch.version]`.

---

## Prerequisites (one-time setup)

- `uv` installed (`brew install uv`)
- PyPI account with API token scoped to the `clichefactory` project:
  https://pypi.org/manage/account/token/
- Token saved as `UV_PUBLISH_TOKEN` in your shell environment, e.g. in
  `~/.zshrc`:
  ```bash
  export UV_PUBLISH_TOKEN=pypi-AgEI...
  ```

---

## Release checklist

Copy this block into your scratchpad and tick off as you go:

```
- [ ] 1. Pick the new version (X.Y.Z)
- [ ] 2. Bump clichefactory/__about__.py
- [ ] 3. Update CHANGELOG.md
- [ ] 4. Decide on README.md / other unstaged changes
- [ ] 5. uv build (after wiping dist/)
- [ ] 6. Smoke-test the wheel locally
- [ ] 7. uv publish
- [ ] 8. Commit, tag, push (main + tag)
- [ ] 9. Optional: GitHub release in the UI
```

---

### 1. Pick the new version

| Bump  | When                                                  |
| ----- | ----------------------------------------------------- |
| patch | Bugfix / docs only, no public API change              |
| minor | Additive public API (new functions, new params)       |
| major | Breaking change (post-1.0 only; pre-1.0 use minor)    |

### 2. Bump `clichefactory/__about__.py`

```python
__version__ = "X.Y.Z"
```

This is the only place to change it.

### 3. Update `CHANGELOG.md`

Add a new entry at the top following the existing format
(Keep a Changelog style):

```markdown
## [X.Y.Z] — YYYY-MM-DD

### Added / Changed / Fixed / Removed

- Concise bullet describing the change.
```

### 4. Decide on `README.md` / other unstaged changes

Run `git status` in `clichefactory-sdk/`. If unrelated edits are
sitting there, either:

- include them in this release if they're complete, or
- `git checkout -- <file>` to revert them.

Do not ship a half-finished README.

### 5. `uv build` (after wiping `dist/`)

```bash
cd ~/ClicheFactory/clichefactory-sdk
rm -rf dist/
uv build
ls dist/
```

Expect:
- `clichefactory-X.Y.Z-py3-none-any.whl`
- `clichefactory-X.Y.Z.tar.gz`

Wiping `dist/` first prevents `uv publish` from re-uploading old
versions and getting a confusing 400 from PyPI.

### 6. Smoke-test the wheel locally

```bash
uv venv /tmp/cliche-test
source /tmp/cliche-test/bin/activate
uv pip install dist/clichefactory-X.Y.Z-py3-none-any.whl
python -c "import clichefactory; print(clichefactory.__version__)"
deactivate
rm -rf /tmp/cliche-test
```

If the version doesn't match, stop and figure out why before publishing.

### 7. `uv publish`

```bash
uv publish
```

Reads `UV_PUBLISH_TOKEN` from the environment. Or pass `--token` inline:

```bash
uv publish --token pypi-AgEI...
```

Confirm on https://pypi.org/project/clichefactory/ that the new version
shows up.

### 8. Commit, tag, push

```bash
cd ~/ClicheFactory/clichefactory-sdk

git add clichefactory/__about__.py CHANGELOG.md \
        clichefactory/_engine/...   # whichever files changed
# (also git add README.md if you're keeping those changes)

git commit -m "<type>: <summary>

Bumps version to X.Y.Z.

<bullet list of changes>"

git tag -a vX.Y.Z -m "vX.Y.Z"
git push origin main
git push origin vX.Y.Z
```

### 9. Optional: GitHub release in the UI

Only useful if you want pretty release notes / downloadable artifacts.
PyPI is the source of truth for `pip install`.

1. https://github.com/nouveau-rubynho/clichefactory-sdk/releases
2. **Draft a new release**
3. Choose the existing tag `vX.Y.Z`, target `main`
4. Title: `vX.Y.Z`
5. Description: paste the relevant CHANGELOG entry
6. Optionally drag in `dist/clichefactory-X.Y.Z-py3-none-any.whl` and
   `clichefactory-X.Y.Z.tar.gz`
7. Keep "Set as the latest release" checked, **Publish release**

---

## Troubleshooting

### `uv publish` fails with 400 / "File already exists"

PyPI does not allow re-uploading a version. You either:
- forgot to bump `__about__.py`, or
- left old artifacts in `dist/` from a previous release.

Fix: bump to the next patch (e.g. `X.Y.(Z+1)`), wipe `dist/`, rebuild,
republish. **Never** try to "fix" a published version in place — cut a
new patch instead.

### Downstream consumers still pull the old version after bumping

If a downstream service pins the SDK and isn't picking up the new
version, run `uv lock --upgrade-package clichefactory` in that
consumer's repo. Without `--upgrade-package`, `uv lock` will keep the
existing pinned version even if the floor moved. After running it,
`grep` the lock file to confirm the new version is actually pinned.

### Token leaked into shell history

Revoke it on PyPI immediately and generate a new one. Prefer
`UV_PUBLISH_TOKEN` in `~/.zshrc` over inline `--token` for this reason.
