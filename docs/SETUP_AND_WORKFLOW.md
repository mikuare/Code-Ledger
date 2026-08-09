# CodeLedger Setup and Workflow Guide

CodeLedger is a local project-memory and change-intelligence layer for AI-assisted development. It indexes a project once, tracks future changes incrementally, gives agents focused context, warns about unrelated edits, and records verification evidence.

## 1. Install CodeLedger

Use WSL for projects developed through WSL or stored on the Windows filesystem.

```bash
cd "~/code-ledger"
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
codeledger --version
```

For full analysis of languages other than Python, install the grammar extra
(~3 MB of prebuilt parsers, no compiler required):

```bash
python -m pip install -e ".[languages]"
```

Without it, only Python is fully analysed and every other language is indexed
with line patterns. CodeLedger reports which, rather than guessing:

```bash
codeledger doctor      # database, schema, migrations, sessions, index, coverage, protocols
```

Installing the extra later upgrades an existing project index automatically on
the next `refresh --changed`. There is no re-init and no migration step.

The virtual environment must be activated again whenever a new terminal is opened:

```bash
source "~/code-ledger/.venv/bin/activate"
```

Do not use `--break-system-packages` on Debian/Ubuntu. The virtual environment keeps the system Python untouched.

## 2. Initialize a project

Replace the example path with your project path. Quote paths containing spaces.

```bash
source "~/code-ledger/.venv/bin/activate"
cd "/path/to/your-project"
codeledger init
```

Initialization creates local project memory under:

```text
.ai/codeledger/
```

It also creates or updates concise agent guidance files:

```text
AGENTS.md
CLAUDE.md
CODEX.md
```

For a large WSL/Windows project, use the fast two-stage flow:

```bash
codeledger init --quick --verbose
codeledger refresh --changed --verbose
```

`--quick` records file metadata first. The following refresh performs the semantic indexing only where needed.

You only need to initialize a project once. Do not run `init` before every task.

## 3. Recommended Codex workflow

### One-time Codex setup

Run this once from the project directory:

```bash
codeledger setup-codex
```

Restart Codex after setup so it loads the new MCP server.

### Start automatic tracking

Open a second WSL terminal and leave this running:

```bash
source "~/code-ledger/.venv/bin/activate"
cd "/path/to/your-project"
codeledger watch --agent codex
```

In another terminal, start Codex normally:

```bash
source "~/code-ledger/.venv/bin/activate"
cd "/path/to/your-project"
codex
```

Now enter tasks directly in Codex. CodeLedger can provide task context through MCP, while the watcher records changed files and symbols continuously.

The normal flow is:

```text
User prompt
    ↓
CodeLedger prompt analysis
    ↓
Project context, history, risk, and scope
    ↓
Codex edits targeted files
    ↓
Watcher detects changes
    ↓
Scope guard checks unrelated edits
    ↓
Verification results are recorded
    ↓
Next prompt sees updated project memory
```

## 4. Manual or wrapper workflow

If MCP is unavailable, use the wrapper for a fully tracked task:

```bash
codeledger run \
  --agent codex \
  --request "Add filtering by department to the admin user list" \
  -- codex
```

The wrapper starts a session, prints context, passes the request to the agent, refreshes changed files afterward, runs the scope check, and records the change.

The wrapper remains optional. The watcher plus MCP workflow is better for long-running interactive sessions.

## 5. Prompt understanding and planning

Analyze a user request before editing:

```bash
codeledger prompt \
  "Add a secure admin login migration, preserve existing permissions, and add tests"
```

Generate the complete pre-change report:

```bash
codeledger plan \
  "Add a secure admin login migration, preserve existing permissions, and add tests"
```

The plan includes:

- Intent
- Likely project areas
- Existing files and symbols
- Constraints
- Acceptance criteria
- Risk
- Known issues
- Architecture decisions
- Suggested tests
- Whether a broad scan is necessary

### Task understanding handshake

Before editing, the AI can submit its proposed implementation for comparison with the original request:

```bash
codeledger handshake \
  "Add admin activity tracking, preserve permissions, and add tests" \
  --ai-plan "Modify the admin users view and auth tracking, preserve permissions, add tests, and avoid payment code"
```

The result is `ALIGNED`, `WARNING`, or `AWAITING_AI_PLAN`. A warning identifies omitted preservation constraints or project areas so the AI can revise its plan before changing code.

## 6. Scope guard

The scope guard is automatically included in watcher and wrapper refreshes. It reports:

```text
SAFE     changed files fit the known task boundary
WARNING  unrelated files or symbols changed
UNKNOWN  insufficient context exists to define a safe boundary
```

You can check a change boundary manually:

```bash
codeledger scope \
  "Update authentication" \
  --files src/auth/service.py src/auth/session.py \
  --symbols authenticateUser refreshSession
```

Warnings are advisory. CodeLedger does not silently discard edits or block legitimate new files.

## 7. Verification and regression detection

Record a verification manually:

```bash
codeledger verify symbol authenticateUser TEST PASSED \
  --evidence "python3 -m unittest tests/test_auth.py"
```

Run and record a command directly. The command must appear after `--`:

```bash
codeledger verify-run project project TYPECHECK \
  -- npm run typecheck
```

Other examples:

```bash
codeledger verify-run project project TEST -- npm test
codeledger verify-run project project BUILD -- npm run build
codeledger verify-run project project LINT -- npm run lint
```

Find regressions:

```bash
codeledger regressions
```

CodeLedger reports when a subject was previously verified as working and later failed.

## 8. Useful commands

```bash
codeledger status
codeledger context "authentication timeout"
codeledger lookup authenticateUser
codeledger impact authenticateUser
codeledger history authenticateUser
codeledger changes
codeledger issues
codeledger decisions
codeledger prompt "Add admin user activity tracking, preserve permissions, and add tests"
codeledger plan "Add admin user activity tracking, preserve permissions, and add tests"
codeledger handshake "Add admin user activity tracking, preserve permissions, and add tests" --ai-plan "Update the admin user view, preserve permissions, and add tests"
codeledger tests --files src/admin/users.tsx --symbols UserList
codeledger features --infer
codeledger git-import
codeledger regressions
codeledger refresh --changed
codeledger export
```

`status` and `changes` are inspection commands. They are not required after every task when the watcher is running.

`git-import` imports available Git commits and changed-file metadata into the local database. It does not invent prior agent or session information.

## 9. Claude Code and other agents

The CodeLedger core and watcher are provider-neutral:

```bash
codeledger watch --agent claude-code
codeledger watch --agent gemini
codeledger watch --agent aider
codeledger watch --agent cursor
```

One-command MCP setup is available for supported local agent CLIs:

```bash
codeledger setup-agent codex
codeledger setup-agent claude-code
codeledger setup-agent gemini
codeledger setup-agent aider
codeledger setup-agent cursor
```

The command reports the exact setup command and whether the local executable accepted it. Agent configuration syntax can vary by installed version, so always review the result and restart that agent after setup.

Inspect an MCP setup template without changing agent configuration:

```bash
codeledger agent-config claude-code
```

Infer high-level functionality groups from source paths and names:

```bash
codeledger features --infer
```

Inferred functionality is marked `UNKNOWN` until tests or manual verification provide evidence.

Codex has the built-in `setup-codex` helper. Other agents can use the same local MCP server through their own MCP configuration, or use `CLAUDE.md`/`AGENTS.md` instructions and the watcher.

## 10. Configuration

Project configuration lives at:

```text
.ai/codeledger/config.json
```

Important settings include:

```json
{
  "max_file_size": 2000000,
  "source_extensions": [".py", ".ts", ".tsx"],
  "follow_symlinks": false,
  "ignores": ["node_modules", "dist", "custom-generated-folder"]
}
```

Additional ignore patterns can be placed in:

```text
codeledger.ignore
```

CodeLedger automatically ignores its own `.ai/codeledger/` data, dependencies, build output, common caches, secrets, and large/non-source files.

## 11. Troubleshooting

### `codeledger: command not found`

Activate the virtual environment:

```bash
source "~/code-ledger/.venv/bin/activate"
```

### Initialization appears slow on WSL

Use the two-stage flow:

```bash
codeledger init --quick --verbose
codeledger refresh --changed --verbose
```

The optimized walker prunes ignored directories before entering them and avoids reparsing unchanged files.

### Codex does not see CodeLedger tools

Run:

```bash
codeledger setup-codex
```

Then restart Codex. An already-running Codex process does not gain newly configured MCP tools dynamically.

### Scope says `UNKNOWN`

This is intentional. CodeLedger will not claim that a change is safe when it lacks enough evidence. Every scope result reports `boundary_evidence` naming what the judgement was based on; an empty list means no evidence was found. Add a more specific task, name the files in the request, run `codeledger plan`, or inspect the diff manually.

### `impact` says the coverage is shallow

The language is being indexed with line patterns rather than a parse tree, so
the dependency graph is incomplete and `impact` verified against the working
tree instead of trusting it. Install the grammars:

```bash
python -m pip install -e ".[languages]"
codeledger refresh --changed
```

### The watcher keeps recording the same files

Check whether those files are generated or temporary. Build output, scratch
directories, and dev-server artifacts should be excluded, or every rewrite is
recorded as a change:

```bash
echo "__scratch__/" >> codeledger.ignore
echo "dist/" >> codeledger.ignore
codeledger refresh --changed
```

A file being rewritten with identical content is *not* recorded — only a real
content change is. Repeated records mean the content is genuinely changing.

### Tests fail after a change

Record the failure and inspect regressions:

```bash
codeledger verify-run project project TEST -- your-test-command
codeledger regressions
```

## 12. Data and privacy

CodeLedger stores project memory locally in SQLite. It does not upload source code by default. Secrets such as `.env` files and private keys are excluded by default. Generated Markdown exports are derived views; the SQLite database is canonical.
