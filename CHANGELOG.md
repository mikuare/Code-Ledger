# Changelog

All notable changes to CodeLedger. Format based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Entries describe what was wrong and how it was found, not only what changed.
Most of these were silent — the tool returned a confident answer that happened
to be wrong — which is the failure mode this project exists to avoid.

## [Unreleased]

### Added

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
