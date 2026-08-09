# Releasing CodeLedger

People install this from a git URL, so **a release that does not move the
version reaches nobody who already has it installed**. `pip install --upgrade`
compares versions, finds the requirement satisfied, and does nothing: the
command succeeds, prints the old version, and leaves the old code running.

That has already happened twice in this project. Both times the code was on
GitHub and every existing install kept running the version before the fix, with
nothing to indicate anything was wrong.

## The checklist

1. **Move the changelog entry** out of `## [Unreleased]` into `## [X.Y.Z]`.
   Say what was wrong and how it was found, not just what changed.

2. **Bump the version in both places.** They must match:
   - `pyproject.toml` → `version = "X.Y.Z"`
   - `codeledger/__init__.py` → `__version__ = "X.Y.Z"`

3. **Run the suite both ways.** CI does this too, but find out before pushing:

   ```bash
   python -m unittest discover -s tests -q          # no grammars
   python -m unittest discover -s tests -q          # again inside a venv with [languages]
   ```

4. **Commit, tag, push both.** The tag is what lets people pin a version:

   ```bash
   git commit -am "release: X.Y.Z"
   git tag -a vX.Y.Z -m "X.Y.Z"
   git push origin main
   git push origin vX.Y.Z
   ```

5. **Verify an upgrade actually lands**, from a project already on the previous
   version — not from a fresh install, which would pass either way:

   ```bash
   .venv/bin/pip install --upgrade "code-ledger[languages] @ git+https://github.com/mikuare/Code-Ledger.git"
   .venv/bin/codeledger --version      # must show the new number
   .venv/bin/codeledger doctor         # Migrations: OK
   ```

## What is checked for you

`tests/test_codeledger.py::ReleaseTests` fails the build if:

- `pyproject.toml` and `codeledger/__init__.py` disagree about the version, or
- `CHANGELOG.md` has no `## [<version>]` section for the declared version.

Both run in CI on every push and pull request. They catch a half-finished
release; they cannot tell you that a change *deserved* a release. That judgement
stays with you — but if the code changed and you are pushing it for others to
install, it needs a version.

## Choosing the number

This project is pre-1.0 and follows Semantic Versioning loosely:

- **Patch** (`0.3.0` → `0.3.1`) — a fix that changes no observable behaviour
  beyond the bug itself.
- **Minor** (`0.3.0` → `0.4.0`) — new commands or MCP tools, a schema migration,
  or a change in default behaviour. `0.3.0` was minor because the watcher began
  waiting before recording an edit.
- **Major** — reserved for 1.0, once the tool has lived on real projects.

## Database migrations

Schema changes go in `MIGRATIONS` in `codeledger/db.py` as additive
`ALTER TABLE` statements. They apply automatically on the first command after an
upgrade. **Never destroy history** — recorded changes, verifications, issues and
decisions are the product. If a migration cannot be additive, say so in the
changelog and explain what the user should do.

## After releasing

Tell people to re-run `codeledger init` in existing projects. The agent protocol
files (`CLAUDE.md`, `AGENTS.md`, `CODEX.md`) are written at `init` time, so an
upgrade alone leaves them on the old protocol. Re-running `init` preserves all
history but overwrites those three files.
