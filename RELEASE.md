# Releasing `clichefactory`

Releases are published by GitHub Actions (`.github/workflows/release.yml`)
when a version tag is pushed. Nothing is uploaded from a laptop, and no PyPI
token is stored anywhere: PyPI trusts this repo's release workflow directly
(trusted publishing), and the publish step waits for the owner's approval.

Versioning is SemVer (pre-1.0: minor for additive API, patch for bugfix/docs).
The single source of truth for the version is `clichefactory/__about__.py`;
`pyproject.toml` reads it via `[tool.hatch.version]`.

---

## Release checklist

```
- [ ] 1. PR: bump clichefactory/__about__.py + add the CHANGELOG.md entry
- [ ] 2. Merge it (tests must be green)
- [ ] 3. Tag main and push the tag
- [ ] 4. Approve the publish job in the Actions tab
- [ ] 5. Check PyPI and the GitHub release
- [ ] 6. Bump the downstream pins
```

### 1. Version bump and changelog (in a PR)

| Bump  | When                                                  |
| ----- | ----------------------------------------------------- |
| patch | Bugfix / docs only, no public API change              |
| minor | Additive public API (new functions, new params)       |
| major | Breaking change (post-1.0 only; pre-1.0 use minor)    |

```python
# clichefactory/__about__.py
__version__ = "X.Y.Z"
```

Add an entry at the top of `CHANGELOG.md` (Keep a Changelog style). The
release notes on GitHub are this entry, so the heading must be exactly:

```markdown
## [X.Y.Z] — YYYY-MM-DD
```

Check it before pushing (standard library only, no install needed):

```bash
python scripts/release_check.py vX.Y.Z
```

### 2. Merge

`tests.yml` runs on the PR: the full suite on Python 3.12 and 3.13, plus a
plain `pip install clichefactory` (no `[local]`) that must import and start the
CLI.

### 3. Tag and push

```bash
git checkout main && git pull
git tag -a vX.Y.Z -m vX.Y.Z
git push origin vX.Y.Z
```

The release workflow then:

1. checks the tag matches `__about__.py`, the changelog has the entry, and the
   commit is on `main`;
2. runs the tests, builds the sdist and wheel, installs the wheel in a clean
   venv and checks the version it reports;
3. **waits for approval**, then publishes to PyPI;
4. creates the GitHub release with the changelog entry and the built files.

If step 1 or 2 fails, nothing is published. Fix it, delete the tag
(`git push --delete origin vX.Y.Z && git tag -d vX.Y.Z`), and tag again.

### 4. Approve

Actions tab → the `release` run → **Review deployments** → `pypi` → Approve.
GitHub also emails the reviewer.

### 5. Check

- https://pypi.org/project/clichefactory/ shows the new version.
- https://github.com/ClicheFactory/clichefactory-sdk/releases has the release.

### 6. Downstream pins

The SDK is pinned in other repos; bump them in their own PRs:

| Repo | Where | How |
| --- | --- | --- |
| FactoryServer (aio-server) | `pyproject.toml` floor + `uv.lock` | `uv lock --upgrade-package clichefactory`, then `grep -A2 'name = "clichefactory"' uv.lock` to confirm |
| ClicheFactory (Emio) | `requirements.txt` | `clichefactory[local]==X.Y.Z` |
| clichefactory-mcp | `pyproject.toml` floor | only if MCP needs the new version; see its RELEASE.md |
| clichefactory-test-suite | `pyproject.toml` | `clichefactory[local]==X.Y.Z` |

---

## Release candidates (testing on dev first)

A pre-release version (`X.Y.ZrcN`, also `aN` / `bN`) goes through the same
workflow and is published to PyPI as a pre-release. `pip install clichefactory`
ignores pre-releases, so customers don't get it; you have to ask for it by
version.

1. Set `__version__ = "0.9.0rc1"` and add a `## [0.9.0rc1] — date` entry.
2. Tag `v0.9.0rc1` and push. A pre-release may be tagged from a branch, so a
   change can be tried before it is merged.
3. Approve as usual. The GitHub release is marked as a pre-release.
4. Pin `clichefactory[local]==0.9.0rc1` on aio-server's `dev` and in the test
   suite, deploy dev, test.
5. When it's good: `__version__ = "0.9.0"`, a `## [0.9.0]` entry, merge, tag.

---

## One-time setup (repo owner)

Done once; after that a release needs no credentials at all.

1. **PyPI → trusted publisher.** On https://pypi.org/manage/project/clichefactory/settings/publishing/,
   add a GitHub publisher:
   - Owner: `ClicheFactory`
   - Repository: `clichefactory-sdk`
   - Workflow name: `release.yml`
   - Environment name: `pypi`
2. **GitHub → environment.** Repo Settings → Environments → New environment
   `pypi`:
   - Required reviewers: yourself.
   - Deployment branches and tags: "Selected branches and tags", add the tag
     rule `v*`.
3. **After the first release works,** revoke the old PyPI API token
   (https://pypi.org/manage/account/token/) and remove `UV_PUBLISH_TOKEN`
   from `~/.zshrc`.

---

## Troubleshooting

### "File already exists" from PyPI

PyPI never accepts the same version twice, even after a delete. Bump to the
next patch (or the next `rcN`) and release that. Never try to fix a published
version in place.

### The check job fails

The message says which of the three it is: the tag doesn't match
`__about__.py`, the changelog entry is missing (the heading must be
`## [X.Y.Z] — date`), or a final release was tagged off `main`.

### The publish job fails with an OIDC / trusted publishing error

The PyPI publisher settings must match exactly: owner, repo,
`release.yml`, and environment `pypi`. Fix them on PyPI and re-run the
failed job from the Actions tab; the tag doesn't need to change.

### Publishing by hand (emergency only)

If Actions is unavailable: create a PyPI token scoped to `clichefactory` for
this one release, `rm -rf dist && uv build && uv publish --token ...`, then
revoke the token. Push the tag afterwards so the GitHub release exists too
(the workflow will fail at publish because the version exists; create the
GitHub release by hand from the changelog entry).
