# Changelog

All notable changes to CodeLedger. Format based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Entries describe what was wrong and how it was found, not only what changed.
Most of these were silent — the tool returned a confident answer that happened
to be wrong — which is the failure mode this project exists to avoid.

## [Unreleased]

A reliability pass over the intelligence that already existed. Every item below
was reproduced from a real session before it was fixed, and each has a
regression test that fails without the fix. No new subsystem, no schema change,
no migration: the goal was to make the existing answers trustworthy rather than
to add more of them.

### Fixed

- **Language keywords and comment prose were indexed as project symbols.** The
  shallow provider's method pattern matched any `keyword (...) {` statement, so
  `if`, `for`, `while`, `switch` and `catch` became symbols; and because it
  scanned raw lines, `// will type the value` produced a type named `the` and
  `class of service` produced a class named `of`. Comment spans are now blanked
  before matching (line numbers preserved), type declarations must sit at the
  head of a line behind known modifiers, and a control-flow head in the method
  slot is rejected structurally — it cannot be an identifier there, so nothing
  real is lost. A method genuinely named `move` or `submit` is untouched.

- **SQL indexed its locals and columns, and missed its functions and tables.**
  The generic tree walker read `tree-sitter-sql` through JavaScript conventions:
  a PL/pgSQL `DECLARE` local is a `function_declaration` in that grammar and a
  column is a `column_definition`, both of which looked like definitions — while
  the real definitions (`create_function`, `create_table`, `create_view`, …)
  matched no convention at all. So `v_email`, `column_name` and `id` were
  indexed and `check_user` and `users` were absent. SQL now has an explicit
  definition list with the conventions switched off, covering functions, tables,
  views, materialized views, triggers, types, indexes, policies, schemas and
  sequences. `CREATE PROCEDURE` and `CREATE DOMAIN` are not in the grammar and
  are reported as unparsed rather than guessed at.

- **Struct fields, class properties and method receivers were project symbols.**
  Suffix matching pulled in Go's `addr` and its `s` receiver and TypeScript's
  `items`. These are storage slots, and are now definitions only when they bind
  a function — which keeps `handleClick = () => {}` and drops the rest.

- **A file the grammar could not parse claimed `FULL` coverage.** With no
  symbols from the grammar and none from the line patterns, a parse error now
  records `SHALLOW`. `impact` treats `FULL` as licence to believe an empty
  dependent list, so claiming it there turned "not parsed" into "nothing
  depends on this".

- **Scope warnings fired on ordinary work.** `scope_check` compared every
  changed symbol against the symbols whose *names* matched the request, so any
  symbol not named after the request was "unexpected" — which is most of them.
  A change to `App.jsx` for "add period navigation" was reported as an
  unexpected file with unexpected symbols `App` and `move`. Scope is now a
  question about files: the boundary includes the files that depend on the
  relevant symbols (from the dependency graph, not word matching), the paths the
  request names, and optionally the files the plan declared; and a symbol is
  unexpected only when the file it lives in is. The boundary also no longer
  inherits the 20-item presentation cap, which silently clipped it.

- **The handshake approved plans that contradicted the request.** It required
  more than one unmentioned area to warn and never compared the plan's paths to
  anything, so a plan to rewrite an unrelated file — or one that violated a
  scope the user had stated explicitly — returned `ALIGNED`. It now warns when
  the plan names a path outside the user's stated scope or outside the indexed
  relevant scope, and returns `INSUFFICIENT_EVIDENCE` instead of `ALIGNED` when
  nothing connects the request to this project. A plan that names no paths is
  not warned about for its silence.

- **Deleted symbols were returned with live line numbers and signatures.** A
  symbol marked `deleted` kept the position it had when it was removed, so
  nothing downstream could tell a symbol that exists from one that used to.
  Deleted symbols now present `line_start`, `line_end` and `signature` as null,
  with the recorded values moved under `historical`; `impact` reports them
  separately from live matches and no longer counts their files as defining
  files.

- **Recreating a symbol created a duplicate row.** Prior symbols were matched
  with `status='active'`, so a symbol that came back missed the revive path and
  was inserted again — leaving two rows for one name, one permanently deleted
  and still carrying stale metadata, both returned by every lookup. The existing
  row is now revived, keeping its id, its creation time and its change history.

- **Cold start returned the whole repository.** `resume` capped its own lists at
  20 but embedded change records whose per-change file lists were unbounded, so
  one broad refactor returned 301 paths to say "there is no checkpoint". Change
  records now bound their file and symbol lists and report `files_total` /
  `files_truncated` alongside, and cold start leads with a short orienting
  summary. Measured on a 301-file project: 6,801 → 2,229 characters, and no
  longer grows with the repository.

- **The request was lost when the ledger already knew it.** A bare
  `codeledger refresh --changed` recorded `NOT RECORDED` even while a single
  live session in the same database held the request text. The session and its
  request are now inherited when exactly one live session exists. Authorship is
  deliberately *not* inherited: a live session proves work is underway, not who
  ran the command, so the agent stays `unknown` at `UNKNOWN` confidence and the
  reason says both things plainly. The watcher inherits nothing, as before.

- **The MCP server discarded the client name it had just established.**
  `codeledger_refresh` and `codeledger_record_change` passed the literal
  `"unknown"` when the argument was omitted, although the server had identified
  the client during `initialize` and started the session under that name. They
  now fall back to it; an explicit argument still wins.

### Notes

- Existing databases upgrade in place. The `files.analysis_version` stamp moves
  to `:2`, so `refresh --changed` reparses each file once and retires symbols
  like `if` and `v_email` — they are marked deleted, never destroyed, and all
  change history is preserved. There is no migration to run.
- `codeledger scope` gains optional `--plan-files` and `--plan-symbols`.
- `since` and change records gain `files_total`, `files_truncated`,
  `symbols_total` and `symbols_truncated`; the handshake gains
  `scope_violations` and `plan_paths`; `impact` gains `historical_symbols`.

## [0.4.0]

Adds a context-continuity layer: an agent's conversation is temporary, and this
turns the engineering knowledge inside it into something the next session can
read. The MCP server now starts and ends a session of its own, which is a
behaviour change for anyone running more than one agent — see below.

### Added

- **Session checkpoints and task-aware resume.** A long conversation eventually
  hits the model's context limit, and what gets compressed away first is the
  expensive part: which approaches were already tried and abandoned. The next
  session then re-reads the repository to rediscover the state, and repeats the
  failed attempt, because nothing recorded that it failed.

  `codeledger_record_checkpoint` stores a compact summary of where a task has
  got to — goal, current state, what was accomplished, what failed, what is
  unresolved, and the single next action. `codeledger_get_resume` retrieves it
  in a new session. Checkpoints reference existing changes, decisions, issues
  and verifications by id rather than copying them, so a checkpoint cannot
  quietly disagree with the ledger it came from.

  CodeLedger never sees the conversation, so it does not write the semantic half
  itself: `codeledger_get_session_state` assembles what was observed and the
  agent supplies the meaning. A summary invented from file lists would read
  exactly as confident as one an agent actually wrote.

- **Resume selects by task, not by recency.** Loading the most recent checkpoint
  is wrong whenever the user has moved on: a checkpoint about dashboard CSS
  aimed at a payments task points the agent at the wrong subsystem with full
  confidence. Selection scores the new task against each checkpoint's goal using
  the same overlap rule that repeat detection already uses, and when nothing
  matches it returns `NO_RELEVANT_CHECKPOINT` with the open goals listed, rather
  than promoting an unrelated one to fill the space.

- **Checkpoints are re-checked against the source before they are believed.**
  A checkpoint is an AI summary, the lowest rank in this project's existing
  ordering — source code, then filesystem and Git, then tests, then structured
  memory, then summaries. Every file and symbol it names is validated at resume
  time: a symbol deleted since the checkpoint was written is dropped from the
  body and reported under `stale_items` with the reason. It is never deleted,
  because what changed underneath the work is itself worth knowing.

- **Agent, provider and model recorded separately per session.** `sessions`
  gains `provider`, `model` and `model_version`. An agent name says which
  program is running, not which model it is driving today, so the model is
  recorded only when the runtime actually reports it and is `UNKNOWN` otherwise.
  Nothing is inferred from the agent name. The vendor table is data, not
  behaviour: CodeLedger never branches on which provider an agent belongs to,
  and an unrecognised agent is `generic` rather than guessed at.

- **Context-window awareness, where a runtime offers it.** `context_window` and
  `context_used` are accepted wherever they are available and drive a
  recommendation past a configurable threshold (`checkpoint_threshold_pct`,
  default 80). Most runtimes expose neither, so `UNKNOWN` is the normal answer
  and the feature works identically without them. CodeLedger recommends; it
  never interrupts an agent mid-task.

- **`plan` now reports what a change would reach, not only where it is defined.**
  The dependency graph could already answer "who breaks if this changes?" —
  `impact` has walked it since the beginning — but the pre-change path never
  asked. `plan` built its file list from `lookup`, which returns the file a
  symbol is *defined* in, so a plan for a provider used by four pages reported
  one file while the ledger held six. Nothing was wrong with the data; it simply
  never reached the agent through the tool the protocol tells it to call.

  `plan` gains `shared_dependencies`, `blast_radius` and `coverage_caveat`, from
  indexed queries only (`impact(fallback=False)`). The scanning fallback stays
  out of this path deliberately: it reads the whole working tree, which is the
  right trade when an agent asks about one symbol and the wrong one on every
  planning call. A test fails the build if planning ever scans.

- **Scope ambiguity is reported instead of guessed at.** "Remove the theme
  colour" does not say which of Landing, Payment, Queue and Dashboard it means,
  and an agent that picks one silently is confidently wrong half the time.

  Five signals decide this, because no single one is trustworthy: how many areas
  depend on the symbol, whether it is shared by design (its location, its name,
  and how widely it is actually used — conventions can lie, the measurement
  cannot), the size of the radius, whether the request already names a scope,
  and the intent. Adding to a shared module changes nothing for its existing
  dependents, so `add a helper to drawerState` asks nothing. A request that
  names its own scope is never questioned, however wide it reaches. A plain
  helper used by two areas is not ambiguous — warning there is how a guard
  teaches agents to ignore it.

- **A plan that duplicates an existing implementation is warned about.** Asked
  to make a button open the same kind of panel, an agent proposing "a new
  CheckoutFlyout with its own slide animation and its own open/close state"
  previously handshook as `ALIGNED`. Nothing was looking for the most expensive
  mistake available.

  `handshake` compares the symbols a plan says it will create against what the
  project already has, and reads the dependency edges *forwards* one hop to name
  the whole existing flow rather than just its entry point — the shared drawer
  and shared state underneath are what reuse actually means. It recommends and
  never rejects: a new implementation is sometimes correct, and when it is, the
  agent should say why rather than proceed silently.

  Areas are computed per file under container directories like `pages/` and
  `components/`, so four sibling pages count as four areas rather than one.

### Fixed

- **A database from an older CodeLedger could not be opened at all.** Upgrading
  a real project failed on `sqlite3.OperationalError: no such column: coverage`,
  raised from `connect` before a single migration had run — so the tool could
  not open an intact database to migrate it, and every command was dead.

  The schema was applied in one script, tables and indexes together, before the
  migrations. `CREATE TABLE IF NOT EXISTS` is a no-op on a table that already
  exists, whatever shape it is in — but `CREATE INDEX IF NOT EXISTS` is not: the
  index genuinely does not exist, so SQLite tried to build
  `idx_files_coverage ON files(coverage)` against a table whose `coverage`
  column was added by a migration that had not run yet. Two indexes were
  affected; `idx_dependencies_source_file` would have failed immediately after.

  Tables, migrations and indexes are now three ordered steps, so every column an
  index names is guaranteed to exist by the time the index is built. Migrations
  additionally run in one transaction: a failure rolls the whole upgrade back
  rather than leaving a half-migrated database that looks upgraded.

  The existing migration test did not catch this because its fixture was built
  from the *current* `SCHEMA` constant, which already contains every column —
  making it a fresh database with some columns removed rather than an old one.
  The regression test now uses the verbatim schema of a database found in the
  field, and asserts the general invariant that an upgraded database ends up
  shaped exactly like a newly created one.

### Changed

- **The MCP server now owns a session.** Previously it started none: an
  MCP-connected agent had no session to heartbeat, nothing to attach a
  checkpoint to, and no entry in `codeledger_get_active_agents` for another
  agent's conflict check to find. It now starts a session on `initialize`,
  records activity on every tool call, and ends it when the client disconnects.

  Two consequences worth knowing. A live MCP session appears in `active_agents`,
  so conflict checks and the watcher's attribution message will mention it —
  authorship is unaffected, and an observed edit is still recorded as `unknown`
  at LOW confidence, because the filesystem still cannot show which process
  wrote a file. And a session that goes quiet past the stale limit is revived by
  the next tool call rather than staying dead for the life of the process.

- **A session that ends without a checkpoint gets a mechanical one.** When the
  MCP client disconnects or `codeledger run` exits, the agent is already gone
  and cannot be asked what the work meant. The fallback records only what was
  observed — files, symbols, the request the changes were made for — at LOW
  confidence, and says so in place of a next action. It never replaces a
  checkpoint an agent wrote.

- **`codeledger run` hands the agent its previous work before it starts**, using
  the same relevance test, so an unrelated earlier task is not loaded.

- **The MCP handshake now carries usage instructions.** An MCP server cannot put
  anything into a model's turn — tools are pull-only — so a server that merely
  offers `codeledger_get_resume` is relying entirely on the agent having read
  the protocol file. The `initialize` result now also returns the standard
  `instructions` field telling the client to resume before reading source and to
  checkpoint before finishing. Clients may ignore it, which is why the protocol
  file still says the same thing. `serverInfo.version` also reports the real
  version, having been hardcoded to `0.1.0` since the server was written.

### Upgrading

Run `codeledger init` in existing projects after upgrading. The schema migrates
by itself on the first command — additively, destroying nothing — but the agent
protocol files (`CLAUDE.md`, `AGENTS.md`, `CODEX.md`) are written at `init` time,
so without re-running it agents will not know to resume or checkpoint at all.
Re-running `init` preserves all history and overwrites only those three files.

## [0.3.0]

The watcher's default behaviour changes: it now waits before recording an edit,
so anyone relying on it indexing immediately should read the entry below and set
`--claim-window 0` if they really want the old behaviour.

### Fixed

- **The watcher stole the change it was meant to back up.** Indexing an edit is
  destructive to authorship: once the file matches the index, the agent's own
  refresh finds nothing left to record. So a watcher that polled first took the
  change, credited it to `unknown`, and — recording no request, because it
  cannot know one — left `progress` reporting `NO_PRIOR_ATTEMPTS` for work that
  had just happened. Running the watcher therefore *disabled repeat detection*,
  the thing this project exists to provide, while appearing to be a harmless
  safety net.

  The watcher now holds back edits made within `--claim-window` seconds
  (default 90) and records only what nobody claimed, saying so plainly:

  ```text
  1 recent edit(s) pending — leaving them for their author to claim.
  Recorded UNCLAIMED change #1: login.py
    Nobody reported these within 90s, so they are attributed to 'unknown'.
  ```

  An agent reporting its own work never waits — only the watcher does. With the
  watcher running, a Claude edit now keeps `HIGH` confidence, keeps the symbol
  credited to `claude-code`, and keeps `progress` returning `PROGRESSING`
  instead of `NO_PRIOR_ATTEMPTS`. Deletions are still recorded immediately, as
  a removed file has no mtime left to age.

## [0.2.0]

Released with the version number bumped for a reason worth recording: it had
stayed at `0.1.0` through every change below, so `pip install --upgrade` saw the
requirement already satisfied and silently did nothing. An upgrade appeared to
succeed, reported the same version, and left the old code running. Any release
that people install from a git URL has to move its version or it cannot be
upgraded at all.

### Changed

- **Incremental refresh made ~3x cheaper on WSL `/mnt/c`.** Profiling an 800-file
  project put 94–99% of a no-op refresh in discovery, and 70% of discovery in a
  single call: `DirEntry.stat`. Nothing was being hashed or parsed — the whole
  cost was proving that 800 files had *not* changed. A stat costs about 2µs on a
  local ext4 volume and about 1ms across `/mnt/c`, where each one is a round-trip
  to the Windows filesystem driver.

  Discovery now separates the walk from the stat: the walk decides which files
  matter using only what `readdir` already returned, so nothing is stat-ed until
  the surviving set is known and a pruned directory costs nothing. Those stats
  are then issued in parallel — `os.stat` releases the GIL, and the round-trips
  overlap almost perfectly, turning 1.84s of waiting into 0.17s on a sample of
  800 files.

  Threading is chosen by measurement, not by guessing the filesystem: a sample is
  stat-ed serially and timed, and the pool is used only if this volume is
  genuinely slow. That matters because on ext4 the pool is a large *pessimisation*
  — 1.4ms of work becomes 25ms of scheduling — so a naive "always parallel" would
  have made native Linux projects 18x slower. The hot path also no longer builds
  a `Path` per entry to take it apart again.

  Measured on identical cold fixtures, 800 source files plus 4,000 ignored, on
  `/mnt/c`: no-op refresh 2.03s → 0.68s, one-file refresh 1.86s → 0.49s. On ext4
  the same project answers a no-op in 17ms, and 5,000 files in 146ms.

  A full traversal still happens, and still stats every source file. That is
  deliberate: nothing cheaper can prove a file's contents did not change —
  directory mtime does not move when a file is edited — so skipping it would mean
  reporting "no changes" without knowing. `refresh` now reports what it cost:
  files checked, files changed, files analysed, directories pruned, whether stats
  ran serially or in parallel, and whether the traversal was full or targeted.

### Fixed

- **Phantom sessions made every later edit `unknown`.** A session was only ever
  closed by the Ctrl+C handler in `watch`. Closing WSL, closing the IDE, a
  crash, `kill -9` or power loss left `status='active'` in the database forever.
  That dead session then counted as a competing agent, so the watcher recorded
  every subsequent edit as `unknown`, conflict warnings fired against an agent
  that died days ago, and `status` reported it as working. One hard shutdown
  degraded attribution permanently. Sessions now record a PID, host and
  heartbeat, and `reconcile_sessions()` reclassifies them as `crashed` (the
  process is gone), `stale` (no activity past the limit), `idle` (quiet but
  within it) or `ended`. Both signals are required: a dead PID is conclusive,
  but a live PID proves little because PIDs are recycled, so a stale heartbeat
  retires the session anyway. Reconciliation runs before anything reports who is
  active. Nothing is deleted — rows keep their history and gain a reason.
- **A failed refresh held the write lock for the life of the process.** `refresh`
  writes many rows before committing, so an exception part-way through left an
  open write transaction. In `watch` or the MCP server — both long-lived — a
  single failure blocked every other agent's write until it timed out. It now
  rolls back and re-raises. The regression test for this went from 34s of lock
  timeouts to passing instantly.
- **The index and its change record committed separately.** A crash between them
  left files marked current with no change row to explain them, so `since` and
  `progress` silently under-reported. They are now one transaction.
- **Comments and formatting counted as code changes.** A symbol's hash was its
  raw source lines, so adding a comment, a docstring or a blank line reported
  `effect=symbols-changed` — the signal an agent relies on to tell a real fix
  from one that missed — and reassigned authorship of code the agent had not
  touched. Python symbols now hash a docstring-stripped AST dump, and
  tree-sitter symbols hash their parse tree with comment nodes removed, so
  comparison is code to code. Reformatting is no longer a logic change.
- **`context` answered from memory the filesystem had already contradicted.** A
  file edited outside CodeLedger left the index reporting symbols that no longer
  existed. `context` now stats the files behind its answer and re-analyses only
  those that drifted, which keeps the query cheap while making it truthful.
- **`doctor` was an undocumented alias for `status`.** It was registered, printed
  index counts, and was referenced in the docs as a diagnostic. It now checks the
  database, WAL, foreign keys, schema, migrations, sessions, file and symbol
  indexes, git, analysis coverage, agent protocols, config and storage, and
  prints the commands to run.

- **The watcher credited the name it was launched with.** With one agent live,
  an observed edit was recorded as that agent's work. But `watch --agent codex`
  says who started the watcher, not who wrote the file — a developer, an editor
  or a formatter produces an identical filesystem event. Observed edits are now
  always `unknown` at `LOW` confidence, with the live agents named as context
  rather than credited.
- **Concurrent refreshes each claimed the same edit.** Reads inside a refresh
  used a snapshot taken when its transaction began, so an agent could not see an
  edit another agent had already recorded and indexed it again. Four agents
  refreshing together produced four `HIGH`-confidence change records for one
  edit — fabricated authorship for three of them, and inflated attempt counts
  that `progress` would later read as a repeat. Refresh now takes the write lock
  before reading anything it decides on (`BEGIN IMMEDIATE`), so each sees
  committed state. Readers are unaffected: WAL keeps `context`, `since` and
  `impact` fast while writers take turns. Verified with eight concurrent
  writers; no duplicates, no losses, no lock timeouts, and no measured cost.
- **Docstrings escaped the tree-sitter code hash.** A docstring is a bare string
  statement, not a comment node, so with grammars installed a documentation edit
  read as a code change — meaning the same file classified differently depending
  on whether an optional package happened to be present. Bodies now drop their
  leading string literal. The rule is scoped to block nodes, because the first
  named child of an argument list is often a string too, and dropping that would
  make `f("a")` and `f("b")` hash identically.

### Added

- **Attribution confidence.** Every change records how well its authorship is
  actually known, not just a name: `HIGH` (the agent called `refresh` itself),
  `MEDIUM` (entered by hand), `LOW` (the watcher observed it, so authorship is
  `unknown`), `UNKNOWN` (no agent named). Stored on `changes` and returned by
  `refresh`.
- **Effect confidence.** Telling a comment from code needs a parse tree. The
  no-dependency line-pattern provider cannot, so for those files `effect` is
  reported at `LOW` confidence naming the shallow files and the install hint,
  instead of asserting a code change it cannot distinguish from a reindent.
- **Cross-agent conflict detection.** `conflicts()` (MCP:
  `codeledger_check_conflicts`) reports when another *live* agent recently
  changed the same code, and grades it: a shared symbol is `HIGH`, a shared file
  only `MEDIUM`. Surfaced automatically by `refresh`, `since`, `watch` and `run`.
  A reconciled-away session cannot raise a conflict.
- **Session management.** `codeledger session list | reconcile | status`, grouped
  by real status rather than a flat dump of rows all reading `active`. `status`
  reports `active_agents` and `stale_sessions`. Configurable via
  `session_idle_seconds` (900) and `session_stale_seconds` (3600) — an agent may
  legitimately think for minutes, so idle is not death.
- **Centralized, idempotent session cleanup.** One context manager closes the
  session on normal exit, `SIGINT`, `SIGTERM` and `SIGHUP`, then re-raises so the
  process still dies of the signal it was sent. It cannot cover `SIGKILL` or a
  WSL shutdown and does not pretend to — that is what reconciliation is for.
  Closing twice never rewrites how a session really finished.
- **Token-efficiency metrics on `context`**: files relevant, files in repository,
  files avoided, symbols and changes returned, whether a full scan was required.
- **Multi-agent handoff, and attribution that refuses to guess.**
  `codeledger since [<change id>|<session>|<timestamp>]` (MCP:
  `codeledger_get_changes_since`) answers "what changed while I was not
  looking"; with `--agent <name>` and no marker it means *since that agent last
  recorded anything*, which is what an agent needs at the start of a turn to
  avoid overwriting work it cannot see. The database is WAL-mode SQLite, so
  several agents and a watcher can read and write concurrently.

  The subtler half is who gets credited. An agent calling `refresh` on its own
  behalf is reporting its own work and is authoritative. The `watch` process is
  not — it observes the filesystem, which cannot show which agent wrote a file.
  Previously it credited every observed edit to whichever `--agent` name it was
  started with, so a ledger watched as `codex` silently attributed Claude
  Code's edits to codex, and `why <symbol>` then reported that invented
  authorship as fact. Observed refreshes now record authorship as `unknown`
  while more than one agent holds an active session, and say why. With a single
  agent working the inference is fair, so it still attributes normally.
- **Effect tracking and loop detection.** Every refresh reports whether an edit
  changed real code (`symbols-changed`), only text (`text-only`), or nothing
  (`none`), stored on `changes.effect`. `codeledger progress "<request>"` (MCP:
  `codeledger_get_progress`) reports whether prior attempts at a request
  achieved anything: `NO_EFFECT`, `REPEATING`, `UNVERIFIED`, or `VERIFIED`, with
  guidance for each. `codeledger run` prints a `NO EFFECT` warning immediately,
  and the agent protocol instructs agents to check progress before retrying.
  This targets the common failure where an agent edits, nothing changes, and it
  retries the same thing — re-reading the repository each time. It reports what
  attempts changed and whether verification passed; it never claims the user's
  prompt was wrong.
- **Multi-language analysis via tree-sitter** (`providers.py`). Analysis sits
  behind a provider protocol declaring a coverage tier: tree-sitter (`full`),
  Python AST (`full`, no dependency), line patterns (`shallow`). Verified end to
  end on Go, Rust, Java, C#, Ruby, PHP, Kotlin, Swift and C++ — all yield
  symbols and a working call graph. Optional: `pip install "code-ledger[languages]"`.
  See [docs/LANGUAGE_SUPPORT.md](docs/LANGUAGE_SUPPORT.md).
- **Coverage as recorded data.** Every file stores the provider and tier that
  produced it, so the system distinguishes *"nothing depends on this"* from
  *"this language is not really analysed"*. `status` and `doctor` report
  `shallow_languages` with an install hint.
- **Per-symbol attribution.** `files`/`symbols` record the agent and session
  that last changed them. `why <symbol>` reports who changed it, in what
  session, and the originating request.
- **`impact --scan`** for an exhaustive filesystem pass when the index is known
  to be incomplete, and automatic fallback when coverage is shallow.
- **`watch --max-interval`** — idle polls back off geometrically and reset the
  moment a change lands.
- MIT licence, CI across Python 3.10–3.13 in both installation configurations,
  and a benchmark that measures a real multi-language project.

### Fixed

- **File hashing went through a lossy decode.** Identity was the hash of text
  decoded with `errors="replace"`, so two files differing only in undecodable
  bytes shared a hash and `refresh --changed` silently skipped a real edit.
  Now hashes raw bytes.
- **Deleted files were re-reported on every refresh.** The deletion sweep never
  checked whether a file was already marked deleted, so any file that had ever
  been removed produced a change record on every `watch` poll — forever. Found
  in real use: 215 records for one file, one every 2.1 seconds. It also defeated
  the idle backoff, since the watcher always believed something had just
  changed.
- **Deleting a file left its symbols `active`**, so `impact` could name symbols
  from a file that no longer exists as live dependents.
- **Requests were matched as a single substring.** `context` passed the whole
  request to `lookup`, so `"Fix the login timeout"` found nothing even with a
  `login` symbol indexed. `plan` returned no files and `scope` answered
  `UNKNOWN` for every sentence — meaning the entire pre-change layer was blind
  to realistic input. No test caught it because every test queried a bare symbol
  name, the one shape that worked.
- **ES module imports were invisible.** The pattern could not match
  `import { x } from '...'`, so the dependency graph for any JavaScript or
  TypeScript project was near-empty. Changing a hook a component imports and
  calls reported `referencing_files: []` and `risk: LOW`.
- **Module-level import edges were discarded.** They use a `__module__`
  sentinel that never resolved to a symbol, leaving the graph import-blind.
  `dependencies` gained `source_file_id`.
- **`impact` read every file in the repository on every call**, while a
  dependency table sat unused. Now index-driven: 78× faster on `/mnt/c` and
  *more* precise — the filesystem scan produced 35% false positives on `pip`,
  matching words inside docstrings.
- **`changes.risk` was never written**, though `context` selected it. Derived
  from the recorded request; `UNKNOWN` when there is no request.
- **Every symbol in a touched file was marked changed.** Editing one function in
  a twenty-function file recorded all twenty, inflating change records and scope
  checks. Only symbols whose content hash moved are now reported, and untouched
  symbols keep their previous author and timestamp.
- **The scope guard was close to vacuous.** It matched on path *prefixes*, so a
  single relevant file under `src/` marked the entire subtree `SAFE`. Now
  same-directory. It also returned `UNKNOWN` whenever the request wording did
  not match an indexed symbol name; the boundary now also uses paths named in
  the request, and request keywords as weak evidence, reporting
  `boundary_evidence` either way.
- **`lookup` treated `%` and `_` as wildcards** and returned unbounded results.
- **A tree-sitter grammar returning nothing claimed `full` coverage** — exactly
  the dishonesty the coverage tier exists to prevent. Now degrades to `shallow`,
  with a two-symbol threshold so a legitimately empty file does not trigger it.
- **`import_specifier` was registered as a definition**, inventing a symbol for
  every imported name.
- **`plan` never suggested tests** that the dedicated `suggest_tests` function
  would have found.
- **`agents.py` was dead code**, so the documented behaviour "unknown names are
  retained as generic providers" was not actually implemented. Now wired into
  `start_session`.
- Numbered SQL placeholders (`?1`) that raise on Python 3.14.
- Personal filesystem paths and a private project name published in the README
  and setup guide.

### Changed

- Distribution renamed to **`code-ledger`**. `codeledger` on PyPI is taken by an
  unrelated tool in the same space, so the previous install instructions pointed
  users at someone else's package. The import name and `codeledger` command are
  unchanged.
- WAL journal mode and a busy timeout, since `watch` and the MCP server hold the
  database open simultaneously.
- Dropped the unused `search_index`, `symbol_versions` and `feature_symbols`
  tables. Removing the FTS5 virtual table also drops a dependency on an optional
  SQLite compile flag.
- `cli.py` argument parsing rebuilt into `build_parser()`; every subcommand is
  now parse-tested.

### Known limitations

- Only nine languages are individually verified. The other 360+ grammars are
  expected to work through the generic walker but are untested; they degrade to
  `shallow` rather than answering wrongly.
- The regex provider's block detection is brace counting, not parsing. Braces
  inside strings or comments can skew a symbol's end line and misattribute a
  call to a neighbour. Only applies without the grammar extra.
- Not yet published to PyPI, so `pip install code-ledger` does not work yet.
  Install from the repository.

## [0.1.0]

Initial implementation: SQLite-backed file and symbol index, incremental
refresh, sessions and change records, issues, decisions, features,
verification and regression tracking, scope guard, prompt/plan/handshake
briefs, Git import, and a local stdio MCP server.
