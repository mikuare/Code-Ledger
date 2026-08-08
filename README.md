# CodeLedger

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
cd "/mnt/c/Users/edujk/Desktop/HD anti gravity/clever-ticket-buddy-94-96"
codeledger init --quick --verbose
codeledger refresh --changed --verbose
```

`--quick` discovers source files, records size/mtime/hash metadata, and skips semantic parsing. The following `refresh --changed` parses only files that still need analysis. Normal refreshes use `os.scandir()` with directory pruning, avoid symlinks, skip non-source files and files larger than the configured limit, and reuse size/mtime metadata before hashing. Configure `source_extensions`, `max_file_size`, `ignores`, and `follow_symlinks` in `.ai/codeledger/config.json`.

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
pip install "codeledger[languages]"
```

This adds `tree-sitter` and a bundled grammar pack (~3 MB, 370+ grammars, prebuilt wheels — no compiler, no network at runtime). Every language then gets the same treatment: real symbol ranges, qualified names, and call graphs.

| Tier | Analysis | Languages |
|---|---|---|
| `full` | parse tree — symbols, scopes, call graph | Python, JS, TS, JSX/TSX, Go, Rust, Java, C#, Kotlin, Swift, Ruby, PHP, C, C++, Scala, Elixir, Lua, Dart, Haskell, and any other grammar in the pack |
| `full` (no install) | Python AST | Python |
| `shallow` | line patterns, imports only | everything else when grammars are absent |

Analysis is optional on purpose. `pip install codeledger` stays dependency-free and keeps working; it simply reports reduced coverage instead of guessing. Every file records the provider and coverage tier that produced it, so the system can tell *"nothing depends on this"* apart from *"this language is not really analysed"* — and `impact` verifies against the working tree whenever coverage is `shallow`, rather than trusting a partial index.

```bash
codeledger status      # includes analysis.shallow_languages and an install hint
```

Coverage is checked in, not asserted: `test_every_supported_language_yields_symbols_and_a_call_graph` builds a real file in each of nine languages and fails if symbols or the call graph are missing. A grammar that yields nothing degrades to `shallow` rather than reporting empty results as full coverage.

Installing or removing grammars upgrades an existing index in place — `files.analysis_version` records the provider, so the next `refresh --changed` reparses only what a different analyser would now read. No re-init, no migration command.

The design and its trade-offs are in [docs/LANGUAGE_SUPPORT.md](docs/LANGUAGE_SUPPORT.md).

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
