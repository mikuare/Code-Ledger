# Changelog

All notable changes to CodeLedger. Format based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Entries describe what was wrong and how it was found, not only what changed.
Most of these were silent — the tool returned a confident answer that happened
to be wrong — which is the failure mode this project exists to avoid.

## [Unreleased]

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
