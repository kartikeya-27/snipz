# Releasing Snipz

Snipz uses **tag-triggered automated releases** via GitHub Actions and PyPI's [Trusted Publisher](https://docs.pypi.org/trusted-publishers/) flow. Push a `v*.*.*` tag, and CI handles the rest: version-drift check, full test run, wheel + sdist build, PyPI publish, GitHub Release creation with artifacts attached.

The first release (`v0.1.0`) was done manually with a local `uv publish`. Every release from `v0.2.0` onward goes through this CI.

---

## One-time setup (already done for v0.1.0+)

You need this configured **once** in the PyPI dashboard for the CI publish to authenticate.

1. Sign in to [pypi.org](https://pypi.org).
2. Go to **Account settings → Publishing → Add a new pending publisher** (or, if the project already exists, **Project → Manage → Publishing**).
3. Fill in:
   - **PyPI Project Name:** `snipz`
   - **Owner:** `kartikeya-27`
   - **Repository name:** `snipz`
   - **Workflow filename:** `release.yml`
   - **Environment name:** `pypi`
4. Save.

That registers GitHub Actions OIDC as a trusted publisher for the `snipz` project. No token is created, transmitted, or stored anywhere. Each release uses a short-lived OIDC token minted at workflow-run time and discarded immediately after.

---

## Release procedure

### 1. Bump the version (in a regular PR)

In a feature branch named `release-vX.Y.Z`:

- `pyproject.toml` → bump `version = "X.Y.Z"`
- `src/snipz/__init__.py` → bump `__version__ = "X.Y.Z"`
- `README.md` → update the status line if the minor version family changed
- Run all three gates locally:
  ```bash
  uv run pytest
  uv run ruff check src/ tests/ benchmarks/
  uv run mypy src/snipz benchmarks/
  ```
- Smoke-test the wheel installs into a fresh venv and reports the right version.
- Open PR, merge to main.

### 2. Tag the merge commit and push

```bash
git checkout main
git pull
git tag -a vX.Y.Z -m "Release vX.Y.Z — one-line summary"
git push origin vX.Y.Z
```

### 3. The CI handles the rest

GitHub Actions runs [`.github/workflows/release.yml`](.github/workflows/release.yml):

| Step | What it does | Failure mode |
|---|---|---|
| Verify version | tag == `pyproject.toml` == `__version__`. Catches the v0.1.0-style version drift bug before publish. | Workflow exits non-zero; nothing reaches PyPI. |
| Run test suite | `uv run pytest` — 140+ tests on SQLite. Postgres tests stay opt-in. | Workflow exits non-zero; nothing reaches PyPI. |
| Lint + typecheck | `ruff check` and `mypy --strict`. | Workflow exits non-zero; nothing reaches PyPI. |
| Build artifacts | `uv build` → wheel + sdist. | Workflow exits non-zero; nothing reaches PyPI. |
| Publish to PyPI | `uv publish` with OIDC token. | Conventional failure modes: name already exists (you re-tagged the same version), permission denied (Trusted Publisher misconfigured), network blip. |
| Create GitHub Release | `gh release create` with artifacts attached. | Idempotent-ish; if the release already exists, the step fails but PyPI has already been published. Run `gh release create` manually for that case. |

Total CI time: ~3–5 min.

---

## Versioning

Snipz follows **SemVer**:

- `0.x.y` — pre-1.0. Breaking changes allowed in minor versions, but documented in release notes.
- `1.x.y` — first stable. Breaking changes require a major bump.
- `x.y.z` — patch releases for bug fixes, no API changes.

Pin recommendations for downstream:

- `snipz>=0.1,<0.2` — current 0.1.x stream; patches allowed, minor breaks forbidden.
- `snipz~=1.0` — once 1.0 is out; equivalent to `>=1.0,<2.0`.

---

## When a release goes wrong

| Symptom | Fix |
|---|---|
| Workflow fails on "Verify tag version" | The tag and pyproject.toml disagree. Delete the tag (`git tag -d vX.Y.Z; git push origin :refs/tags/vX.Y.Z`), bump pyproject in a follow-up PR, re-tag. |
| Workflow fails on tests/ruff/mypy | The pre-flight local run missed something. Fix on main in a follow-up PR, then re-tag (probably with a `+1` patch bump rather than reusing the version). |
| `uv publish` fails with "File already exists" | The version slot is taken — you can't republish the same version with different content. Bump to the next patch and re-tag. |
| Trusted Publisher permission denied | Re-check the PyPI dashboard config (owner, repo, workflow filename, environment all match exactly). |
| `gh release create` fails | The tag is on PyPI but the GitHub Release didn't land. Run `gh release create vX.Y.Z dist/*.whl dist/*.tar.gz --title vX.Y.Z` manually from a checkout. |

---

## Yanking a release

PyPI versions are **immutable** — you cannot republish the same version with different content. If a release is bad, do both:

1. **Yank on PyPI:** [pypi.org/project/snipz/](https://pypi.org/project/snipz/) → Releases → click the bad version → Yank. Yanked releases don't appear in solver candidates by default but stay installable via pin.
2. **Publish a fix as the next patch:** `vX.Y.Z+1` with the fix. Document in the release notes that `vX.Y.Z` is yanked and why.

A yanked version is **not deleted** — anyone who pinned `==X.Y.Z` still installs it. The yank just stops new installs from picking it up by default.
