# CodeLedger

[![CI](https://github.com/mikuare/Code-Ledger/actions/workflows/ci.yml/badge.svg)](https://github.com/mikuare/Code-Ledger/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)

**Give your coding agent a memory of your codebase — what exists, what changed, who changed it, and what breaks if you touch it.**

Coding agents start every session blind. They re-read your repository, rebuild something that already exists, or change a file the task never mentioned. CodeLedger keeps a local index so the agent can ask instead of guess, and so you can see afterwards exactly what it touched.

```bash
codeledger impact authenticateUser   # who breaks if I change this?
codeledger why formatName            # who changed this, and for what request?
codeledger scope "fix login" --files src/billing/charge.py   # is this even in scope?
```

It runs entirely on your machine, stores everything in SQLite, and speaks [MCP](#mcp) so Claude Code, Codex, Cursor, Gemini, and Aider can query it mid-conversation. No source code ever leaves the machine.

Its one design rule: **never assert what it cannot back up.** Unknown authorship stays `unknown`, an unanalysable language reports `shallow` coverage, and an ambiguous task returns `UNKNOWN` rather than a confident wrong answer.

CodeLedger is a local-first, SQLite-backed project memory and change-intelligence CLI for coding agents and human developers. It indexes file hashes and symbols incrementally, preserving deleted symbols as historical evidence instead of inventing authorship or intent.

For the complete installation and daily workflow, see [docs/SETUP_AND_WORKFLOW.md](docs/SETUP_AND_WORKFLOW.md).

## Quick start

```bash
python -m codeledger.cli init
python -m codeledger.cli status
python -m codeledger.cli context "authentication timeout" --json
python -m codeledger.cli lookup authenticateUser
python -m codeledger.cli impact authenticateUser
python -m codeledger.cli refresh --changed
```

Useful safety/intelligence commands:

```bash
codeledger prompt "Add admin user activity tracking, preserve permissions, and add tests"
codeledger plan "Add admin user activity tracking, preserve permissions, and add tests"
codeledger handshake "Add admin user activity tracking, preserve permissions, and add tests" --ai-plan "Update the admin user view, preserve permissions, and add tests"
codeledger tests --files src/admin/users.tsx --symbols UserList
codeledger features --infer
codeledger git-import
codeledger regressions
```

For large repositories, especially projects under `/mnt/c` in WSL, use the fast metadata pass first:

```bash
cd "/path/to/your-project"
codeledger init --quick --verbose
codeledger refresh --changed --verbose
```

`--quick` discovers source files, records size/mtime/hash metadata, and skips semantic parsing. The following `refresh --changed` parses only files that still need analysis. Normal refreshes use `os.scandir()` with directory pruning, avoid symlinks, skip non-source files and files larger than the configured limit, and reuse size/mtime metadata before hashing. Configure `source_extensions`, `max_file_size`, `ignores`, and `follow_symlinks` in `.ai/codeledger/config.json`.

### What an incremental refresh costs

`refresh` reports its own price, so a slow project can be diagnosed rather than guessed at:

```text
CODELEDGER REFRESH

  Discovery      0.331s
  Hashing        0.000s
  Parsing        0.000s
  Database       0.001s
  Total          0.334s

  Files checked   800  (parallel stat)
  Files changed   0
  Directories     19 visited, 2 pruned
  Traversal       full
```

Proving that nothing changed means asking every source file for its size and mtime — a directory's timestamp does not move when a file inside it is edited, so there is no cheaper way to be sure, and CodeLedger will not report "no changes" on a guess. What it avoids is the expensive part: nothing is read, hashed, or parsed unless its metadata moved, and ignored directories are pruned before they are entered.

That one `stat` per file is the whole cost, and its price varies enormously — roughly 2µs on a local Linux volume against 1ms across `/mnt/c`, where every call is a round-trip to the Windows filesystem driver. Those round-trips are issued in parallel when, and only when, a timed sample shows the volume is slow enough to be worth it; on a fast volume the thread pool would cost far more than the work.

Measured on 800 source files plus 4,000 ignored files, cold:

| | `/mnt/c` (WSL) | native Linux volume |
|---|---|---|
| no-op refresh | 0.68s | 0.017s |
| one-file refresh | 0.49s | 0.026s |
| 5,000 files, no-op | 3.8s | 0.15s |

**If your project can live on the Linux filesystem rather than under `/mnt/c`, put it there.** It is roughly 25× faster here, and that is a property of the WSL filesystem boundary, not of CodeLedger.

## Agent workflow

Start a session before an agent edits the project, then refresh afterward. Refresh only reparses changed files and automatically records the agent, session, changed files, and changed symbols.

```bash
SESSION=$(codeledger session start --agent codex --request "Fix login timeout" --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["session_id"])')
codeledger context "login timeout"
# let the agent or developer make changes
codeledger refresh --changed --agent codex --session "$SESSION" --request "Fix login timeout"
codeledger session end --session-id "$SESSION"
codeledger changes
```

Known agents include `codex`, `claude-code`, `gemini`, `aider`, `cursor`, and `human`. Unknown names are retained as generic providers.

## Automatic agent integration

For the most automatic workflow, wrap the local agent command:

```bash
codeledger run --agent codex --request "Fix login timeout" -- codex
codeledger run --agent claude-code --request "Add search pagination" -- claude
```

The wrapper prints context before the agent starts, creates a session, runs the command in the project directory, refreshes changed files afterward, records a change, and returns the agent's exit code.

If an agent cannot be wrapped, use the polling watcher in another terminal:

```bash
codeledger watch --agent codex --interval 2
```

The watcher records external edits as they happen and attributes them to the selected agent/session. If no agent evidence is available, use `unknown`; CodeLedger never fabricates authorship.

Each poll walks the project tree, so idle polls back off geometrically from `--interval` toward `--max-interval` (default 30s) and reset to `--interval` as soon as a change is recorded. An active session stays responsive while an idle watcher stops re-walking a large tree every two seconds. Pass `--max-interval 0` to poll at a fixed rate.

The installed command is `codeledger` after `pip install -e .`. State lives in `.ai/codeledger/codeledger.db`; generated Markdown exports are derived views, never the source of truth. Secrets and common generated/dependency directories are ignored by default. Git is optional and used only when available.

## Design

The core is deterministic: filesystem inventory, SHA-256 hashes, Python AST parsing, conservative multi-language extraction, SQLite indexes, and optional Git evidence. The adapter boundary is intentionally small so Codex, Claude Code, MCP, CI, and other integrations can record sessions and changes without coupling the storage layer to a provider.

Current source and filesystem state outrank indexed memory. An unchanged file is not reparsed during `refresh --changed`; removed symbols are marked `deleted` and remain queryable. Unknown attribution is represented as `unknown`/`NOT RECORDED`.

File identity is the SHA-256 of the raw bytes, never of a lossily decoded string, so an edit confined to bytes that are not valid UTF-8 still registers as a change.

`impact` answers from the dependency index — call, use, and import edges recorded at parse time — rather than reading the working tree, and reports `"source": "index"`. When the index is known to be incomplete, `--scan` adds a full filesystem pass:

```bash
codeledger impact authenticateUser          # indexed edges, bounded work
codeledger impact authenticateUser --scan   # also reads every source file
```

See [Language support](#language-support) for what each language's analysis is actually based on.

Because that coverage is uneven, **absence of evidence is never reported as absence of impact**. If the index finds no dependents at all, `impact` reads the working tree before answering and reports `"source": "index + fallback scan"`. A query matching no indexed symbol returns `risk: UNKNOWN` with the reason, rather than a falsely reassuring `LOW`. Pass `fallback=False` through the API to keep a query strictly indexed.

## Language support

Install the grammars for full parse-tree analysis across languages:

```bash
pip install "code-ledger[languages]"
```

This adds `tree-sitter` and a bundled grammar pack (~3 MB, 370+ grammars, prebuilt wheels — no compiler, no network at runtime). Every language then gets the same treatment: real symbol ranges, qualified names, and call graphs.

| Tier | Analysis | Languages |
|---|---|---|
| `full` | parse tree — symbols, scopes, call graph | Python, JS, TS, JSX/TSX, Go, Rust, Java, C#, Kotlin, Swift, Ruby, PHP, C, C++, Scala, Elixir, Lua, Dart, Haskell, and any other grammar in the pack |
| `full` (no install) | Python AST | Python |
| `shallow` | line patterns, imports only | everything else when grammars are absent |

Analysis is optional on purpose. `pip install code-ledger` stays dependency-free and keeps working; it simply reports reduced coverage instead of guessing. Every file records the provider and coverage tier that produced it, so the system can tell *"nothing depends on this"* apart from *"this language is not really analysed"* — and `impact` verifies against the working tree whenever coverage is `shallow`, rather than trusting a partial index.

```bash
codeledger status      # includes analysis.shallow_languages and an install hint
```

Coverage is checked in, not asserted: `test_every_supported_language_yields_symbols_and_a_call_graph` builds a real file in each of nine languages and fails if symbols or the call graph are missing. A grammar that yields nothing degrades to `shallow` rather than reporting empty results as full coverage.

Installing or removing grammars upgrades an existing index in place — `files.analysis_version` records the provider, so the next `refresh --changed` reparses only what a different analyser would now read. No re-init, no migration command.

The design and its trade-offs are in [docs/LANGUAGE_SUPPORT.md](docs/LANGUAGE_SUPPORT.md).

## More than one agent on the same project

Claude Code and Codex can share one ledger. Set each up once, then let both query it:

```bash
codeledger setup-agent codex
codeledger setup-agent claude-code
```

The database is WAL-mode SQLite, so several agents and a watcher can read and write concurrently. At the start of a turn an agent asks what happened while it was not looking:

```bash
codeledger since --agent claude-code    # since claude-code last recorded anything
codeledger since 42                     # since change #42
codeledger since session-f8e17355e4     # since a session started
```

```text
2 change(s) by codex: 1 file(s), 2 symbol(s). 2 were made by another agent.
   #8 by codex  ['src/auth/session.py']  symbols=['login', 'logout']  effect=symbols-changed
   #7 by codex  ['src/auth/session.py']  symbols=['login']  effect=symbols-changed
```

Agents reach the same thing through MCP as `codeledger_get_changes_since`.

When two agents are live, CodeLedger warns before they collide. Editing the same *symbol* is a stronger signal than merely touching the same file, and it is graded accordingly:

```text
POTENTIAL CONFLICT: claude-code also changed the same symbol(s): login.
Re-read those before editing so the two agents do not undo each other.
```

### Attribution is graded, not asserted

The filesystem records that a file changed. It does not record which process changed it, and no amount of watching recovers that. So every change stores how well its authorship is actually known:

| Confidence | When | Recorded as |
|---|---|---|
| `HIGH` | The agent called `refresh` itself — it is reporting its own work | that agent |
| `MEDIUM` | A change entered by hand via `record` | the name given |
| `LOW` | The watcher observed an edit | `unknown`, with the live agents named as context |
| `UNKNOWN` | A refresh was recorded with no agent name | `unknown` |

The watcher never credits the name it was launched with, even when that agent is the only one running. `watch --agent codex` says who started the watcher, not who wrote the file — a developer, an editor or a formatter produces an identical filesystem event. If you want per-symbol authorship, have each agent call `refresh` itself; that is what the protocol tells them to do.

For the sharpest record with two agents: run the watcher for continuous safety, and have each agent refresh on its own behalf when it finishes a task.

### When a session dies without saying goodbye

A watcher is an ordinary foreground process. Closing WSL, closing the IDE, a crash or `kill -9` all end it without any chance to clean up — no signal handler can cover the last two. So liveness is decided from evidence rather than from the row still saying `active`: each session records a PID, a host and a heartbeat, and any command that reports who is working reconciles them first.

```bash
codeledger session list
```

```text
ACTIVE:
   claude-code session-8ec7d4321e (pid=unrecorded, last activity 2026-08-09T06:15:13+00:00)
CRASHED:
   codex session-1a380d901c (pid=999123) — process 999123 is no longer running on this host
```

A dead PID is conclusive. A *live* PID is not, because PIDs are recycled — so a session whose heartbeat has gone quiet past `session_stale_seconds` is retired regardless. An agent may legitimately think for several minutes, so idleness is not death: `IDLE` still counts as live, only `STALE` stops counting. Nothing is ever deleted; a retired session keeps its history and gains a reason. `codeledger doctor` reports any that are left over, and `codeledger session reconcile` retires them on demand.

## Did the change actually do anything?

The most expensive failure in agent-assisted work is the silent loop: you prompt, the agent edits, nothing changes, you prompt again. The agent has no memory of the last attempt, so it re-reads the repository and often tries the same thing — spending tokens to rediscover what already failed.

Every refresh now reports what an edit actually achieved:

| `effect` | Meaning |
|---|---|
| `symbols-changed` | real code changed |
| `text-only` | files changed but no symbol did — formatting, comments, or an edit that missed |
| `none` | nothing changed at all |

A file rewritten with identical content never counts. `codeledger run` says so directly rather than burying it:

```text
[CODELEDGER] NO EFFECT: this attempt changed 1 file(s) but no symbol.
[CODELEDGER] Run `codeledger progress 'Fix the total calculation rounding'` before retrying.
```

Before retrying a task that did not work, ask what previous attempts did — one cheap query instead of re-reading the codebase:

```bash
codeledger progress "Fix the total calculation rounding"
```

```text
status:   REPEATING
guidance: 3 attempts have edited calculate_total and verification still fails.
          Editing the same symbol again is unlikely to help. Re-read the failure
          output, widen the search with `codeledger impact <symbol>`, or ask the
          user whether the request describes the real problem.
```

The four verdicts are `NO_EFFECT` (attempts changed no symbol — the edits are not reaching the code that runs), `REPEATING` (same symbols edited repeatedly, verification still failing), `UNVERIFIED` (real changes, no evidence recorded), and `VERIFIED` (verification passed after the last attempt — stop editing). Agents reach it through MCP as `codeledger_get_progress`, and the protocol written into `CLAUDE.md`/`AGENTS.md`/`CODEX.md` tells them to call it before a retry.

Note what this does *not* do: it never claims the user's prompt was wrong. It reports what the attempts changed and whether verification passed, and where the evidence points at the request itself, it says to ask you.

## Attribution

Every file and symbol records the agent and session that last changed it, and `why` links a symbol to the request behind it:

```bash
codeledger why formatName
```

```text
answer:      Last recorded request touching this symbol: Uppercase the formatted name
attribution: formatName  src/admin/users.tsx  last_modified_by=claude-code  session=sess-42
```

Credit is assigned only to symbols whose content actually changed. A symbol that merely shifted lines because of an edit elsewhere in the same file keeps its previous author and `updated_at` — a refresh never reassigns authorship for work nobody did. Symbols changed outside a recorded session are attributed to `unknown`, never guessed.

## Issues, decisions, and verification

```bash
codeledger issue AUTH-42 "Refresh token expiry edge case" --severity HIGH
codeledger decision ADR-1 "Keep authentication centralized" --rationale "Avoid duplicate services"
codeledger verify symbol authenticateUser TEST PASSED --evidence "python -m unittest tests/test_auth.py"
codeledger issues
codeledger decisions
```

These records are local SQLite data and are surfaced automatically by `context`.

## MCP

CodeLedger includes a local stdio MCP server. Configure an MCP-capable agent to launch:

```bash
codeledger mcp --root /path/to/project
```

Available tools include context retrieval, symbol lookup, impact analysis, history, issues, decisions, and incremental refresh. The server never sends source code over the network.

For Codex, initialize the integration once from the project directory:

```bash
codeledger setup-codex
```

Then start a new Codex session and leave the watcher running in a second terminal:

```bash
codeledger watch --agent codex
```

Codex can query CodeLedger during the same conversation through MCP, while the watcher records edits continuously. This removes the need to exit Codex or manually run `status`/`changes` after every task. Restarting Codex after MCP setup is required because an already-running client does not gain new tools dynamically.

## Scope guard

Every task-aware refresh now produces a conservative scope result:

```text
SAFE     changed files fit the known task boundary
WARNING  unrelated files or symbols changed; review the diff
UNKNOWN  CodeLedger lacks enough context to define a safe boundary
```

The wrapper and watcher display warnings automatically. You can also check a proposed diff directly:

```bash
codeledger scope "Update authentication" --files src/auth/service.py src/auth/session.py --symbols authenticateUser refreshSession
```

Scope warnings are advisory, not destructive blocking. A new file is allowed only in a directory that already contains a task-relevant file — a sibling directory such as `src/billing/` is not covered by a match in `src/auth/` — while ambiguous tasks remain `UNKNOWN` instead of being falsely marked safe.

The boundary is drawn from indexed symbols matching the request and from any paths written into the request itself. When neither exists, request keywords are matched against file paths so that a task whose wording happens not to match a symbol name still gets a judgement rather than `UNKNOWN`. Every result reports `boundary_evidence` naming which of these was used, and keyword matches are labelled weak evidence.

## Safety loop

Use pre-change planning and evidence-backed verification:

```bash
codeledger plan "Add admin user activity cards"
codeledger verify-run project project TYPECHECK -- npm run typecheck
codeledger regressions
```

The plan reports existing implementations, affected files, risk, known issues, decisions, and suggested tests. `verify-run` executes a local command without a shell, stores its output and pass/fail result, and `regressions` identifies subjects that previously passed and later failed. These results are also available to Codex through MCP.

## Prompt understanding

CodeLedger also creates a deterministic task brief before the agent edits code:

```bash
codeledger prompt "Add a secure admin view showing newly registered users, preserve existing permissions, and add tests"
```

The brief extracts intent, likely project areas, paths, constraints, acceptance criteria, risk, and clarifying questions. It does not invent requirements or call an external AI service. The structured brief is included automatically in `context`, `plan`, and MCP responses so agents receive a clearer, project-aware task.
