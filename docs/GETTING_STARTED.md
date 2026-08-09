# Getting started with CodeLedger

A practical guide: first setup, the daily loop, and what to do when WSL, your
IDE, or an agent closes — expectedly or not.

---

## 1. What this is, and what it is not

CodeLedger is **memory for your AI coding agents**. It is not a build tool, a
test runner, or a debugger, and it will not find bugs for you.

What it does is remove the two most expensive failures in agent-assisted work:

- **Re-reading the repository every turn.** The agent asks the ledger instead.
  On an 800-file project a context query answers in well under a second, against
  a full scan of everything.
- **Silently repeating a fix that already failed.** Every edit is recorded with
  whether it changed real code, only text, or nothing at all — so an agent can
  be told "three attempts changed no symbol; your edits are not reaching the
  code that runs" instead of trying the same thing again.

Your debugging still comes from tests, the debugger, and reading errors.

---

## 2. First-time setup

Do this once per project.

```bash
cd /path/to/your-project

python3 -m venv .venv
.venv/bin/pip install "code-ledger[languages] @ git+https://github.com/mikuare/Code-Ledger.git"

.venv/bin/codeledger init
.venv/bin/codeledger setup-agent claude-code     # and/or: codex, gemini, aider, cursor
```

Two notes that matter:

**Keep `[languages]`.** Without the tree-sitter grammars, only Python gets exact
comment-versus-code classification. Everything else falls back to line patterns,
where adding a comment reads as a code change. CodeLedger marks those results
`LOW confidence` rather than lying about them, but the signal is much weaker.

**Install from GitHub, not from a local folder.** A `pip install -e /some/path`
means every project runs whatever is in that working tree at the time, including
half-finished edits.

If `pip install` fails with an "externally managed environment" error, that is
PEP 668 protecting your system Python. The virtual environment above is the fix.

---

## 3. What `init` puts in your project

| Path | What it is | Commit it? |
|---|---|---|
| `.ai/codeledger/` | The SQLite database and config | No — **add it to `.gitignore` yourself** |
| `CLAUDE.md`, `AGENTS.md`, `CODEX.md` | The agent protocol | Your call; useful for teammates |

`init` does not write to your `.gitignore`. It creates exactly six files and
modifies nothing else, so the database will be committed unless you add:

```gitignore
.ai/codeledger/
```

**Your source code is never modified.** `git diff` immediately after `init` is
empty.

Secrets are not indexed. `.env`, `.env.*`, `id_rsa` and `id_ed25519` are excluded
before anything reads them, and no file contents are stored — only hashes,
symbol names, and signatures.

The database is disposable. If anything ever looks wrong, delete
`.ai/codeledger/` and run `init` again. You lose recorded history, not code.

---

## 4. The daily loop

After `setup-agent`, the agent does this itself over MCP. You do not type these.

1. **Before editing** — the agent asks for a plan and context: relevant symbols,
   recent history, known issues, blast radius.
2. **After editing** — the agent calls `refresh`, which records what changed and
   whether it had any effect.
3. **When something did not work** — the agent asks for progress, and gets back
   one of `NO_PRIOR_ATTEMPTS`, `PROGRESSING`, `NO_EFFECT`, `REPEATING`,
   `UNVERIFIED`, or `VERIFIED`, with guidance.
4. **When tests run** — evidence is recorded. An agent *saying* "done" never
   marks anything verified; only a real command's exit code does.
5. **Starting a session** — the agent asks whether this task has been worked on
   before, and gets back the previous goal, what failed, and the next action —
   without the old conversation or re-reading the repository.
6. **Ending a session** — the agent records a checkpoint so the next one can
   continue. CodeLedger cannot see the conversation, so the agent supplies the
   meaning; if a session ends without one, a thin `LOW`-confidence record of
   what was observed is written instead.

Three things the agent is told before it edits, which are worth recognising when
you see them:

- **shared dependency / blast radius** — the symbol it is about to change is
  used elsewhere, and here are the files and areas a change reaches.
- **scope ambiguity** — the request touches shared code without saying how
  widely it applies, so the agent should ask you rather than pick for you.
- **duplicate implementation** — its plan creates something the project already
  has, with the existing pieces named. A recommendation, not a refusal.

The commands you may want by hand:

```bash
codeledger status                      # index counts, live agents, stale sessions
codeledger doctor                      # full health check and what to run next
codeledger refresh --changed           # catch the index up after outside edits
codeledger progress "fix login timeout"  # what did previous attempts achieve?
codeledger resume "fix login timeout"    # what a new session would be told
codeledger plan "remove the theme color" # blast radius and scope before editing
codeledger verify-run project project TEST -- npm test    # record real evidence
```

---

## 5. Running two agents on the same project

Claude Code and Codex can share one ledger. The database is WAL-mode SQLite, so
several agents and a watcher read and write concurrently without corruption.

**To get cross-agent conflict warnings, each agent needs a live session** — and
this is the part that is easy to miss:

> The MCP interface has **no session tool**. An agent that only talks to
> CodeLedger over MCP never registers a session, so `active_agents` stays empty
> and conflict detection never fires.

So when you start each agent, register it:

```bash
codeledger session start --agent claude-code --request "what you are working on"
codeledger session start --agent codex --request "what they are working on"
```

Then overlapping work is caught, and graded by what is actually at stake:

```text
POTENTIAL CONFLICT: codex also changed the same symbol(s): login.
Re-read those before editing so the two agents do not undo each other.
```

A shared **symbol** is `HIGH`. A shared **file** only is `MEDIUM` — touching the
same file can be coincidence; rewriting the same function is one agent about to
undo the other.

At the start of a turn, an agent asks what it missed:

```bash
codeledger since --agent claude-code    # since claude-code last recorded anything
```

### Who gets credit for an edit

| Confidence | When | Recorded as |
|---|---|---|
| `HIGH` | The agent called `refresh` itself | that agent |
| `MEDIUM` | Entered by hand with `codeledger record` | the name given |
| `LOW` | The watcher observed a filesystem change | `unknown` |
| `UNKNOWN` | A refresh with no agent name | `unknown` |

The watcher **never** credits the name it was started with. `watch --agent codex`
says who launched the watcher, not who wrote the file — a developer, an editor,
or a formatter produces an identical filesystem event. If you want per-symbol
authorship, have each agent refresh on its own behalf.

---

## 6. Closing things: what happens and what to do

This is the section to bookmark.

### The short version

**Your data is always safe.** The ledger is a file inside your project. Closing
anything — a terminal, an IDE, WSL, the whole machine — loses nothing. There is
no daemon and no server to restart.

**On next open, run one command:**

```bash
codeledger refresh --changed
```

That catches the index up on anything edited while CodeLedger was not looking.
Everything else repairs itself automatically.

---

### Scenario A — You press Ctrl+C on the watcher

The session closes cleanly.

```text
ENDED:
   codex session-97673860e6
```

Nothing to do. Start the watcher again whenever you like.

---

### Scenario B — You close the terminal, or the IDE

The watcher dies with its terminal. There is no auto-restart; it is an ordinary
foreground process.

`SIGTERM` and `SIGHUP` are handled, so in most cases the session still closes
cleanly as `ENDED`. If it was killed outright, the next scenario applies.

On next open:

```bash
codeledger refresh --changed
```

---

### Scenario C — You close WSL, reboot, or lose power

Nothing can run at that moment — no signal handler survives a `SIGKILL` or a
shutdown. CodeLedger does not pretend otherwise; it repairs afterwards instead.

**On next open, dead sessions are detected automatically.** Any command that
reports who is working reconciles first, so you do not have to remember a
cleanup step:

```bash
codeledger session list
```

```text
CRASHED:
   codex session-1a380d901c (pid=999123) — process 999123 is no longer running on this host
```

How it decides, using two independent signals:

- **A dead PID is conclusive** → `CRASHED` immediately.
- **A live PID proves little**, because PIDs are recycled after a reboot — so a
  session whose heartbeat has gone quiet is retired anyway.

Then catch the index up on edits made while nothing was watching:

```bash
codeledger refresh --changed
```

**A caveat worth knowing.** Sessions created by `codeledger session start` record
no PID — that command exits immediately, so its PID would mean nothing. Those are
judged on time alone:

| Time since last activity | State | Counts as live? |
|---|---|---|
| under 15 minutes | `ACTIVE` | yes |
| 15 minutes to 1 hour | `IDLE` | yes — an agent may legitimately think for minutes |
| over 1 hour | `STALE` | no |

So if you close WSL and reopen it **five minutes later**, a manually started
session still shows `ACTIVE`. That is honest rather than wrong: five minutes of
silence is not proof that anything died. If it bothers you, close it yourself:

```bash
codeledger session end --session-id session-1a380d901c
```

Tune the thresholds in `.ai/codeledger/config.json` via `session_idle_seconds`
(default 900) and `session_stale_seconds` (default 3600).

---

### Scenario D — Codex or Claude Code crashes mid-task

Their partial edits are still on disk, but may never have been recorded.

```bash
codeledger refresh --changed --agent claude-code --request "what it was doing"
codeledger progress "what it was doing"
```

The second command tells you whether the interrupted attempt actually achieved
anything before it died, so the next attempt does not start from scratch.

---

### Scenario E — Someone edited files outside CodeLedger

A teammate, a formatter, a `git checkout`, a merge.

```bash
codeledger refresh --changed
```

Those edits are recorded as `unknown` at `LOW` confidence, which is correct —
the filesystem cannot say who made them.

You do not strictly have to remember this: context queries stat the files behind
their own answer and re-analyse anything that drifted, so CodeLedger will not
report a symbol that the source no longer contains.

---

### Your next-open checklist

```bash
cd /path/to/your-project

codeledger doctor              # is anything wrong, and what should I run?
codeledger refresh --changed   # catch up on edits made while away

# only if you want conflict detection between two agents this session:
codeledger session start --agent claude-code --request "today's task"
```

---

## 7. Session states

| State | Meaning | Counts as a live agent |
|---|---|---|
| `ACTIVE` | Recent activity | yes |
| `IDLE` | Quiet, but within the stale limit | yes |
| `STALE` | No activity past the limit | no |
| `CRASHED` | Its process is gone | no |
| `ENDED` | Closed cleanly | no |
| `UNKNOWN` | No usable timestamp; liveness undeterminable | no |

Sessions are **never deleted**. A retired session keeps its full history and
gains a reason explaining how it ended.

---

## 8. When something looks wrong

```bash
codeledger doctor
```

It checks the database, WAL mode, foreign keys, schema, migrations, sessions,
the file and symbol indexes, git, analysis coverage, the agent protocol files,
config, and whether CodeLedger is accidentally indexing its own storage — then
prints the commands to run.

```text
CODELEDGER DOCTOR

Database            OK
Wal                 OK
Sessions            0 live, 1 stale/crashed
File Index          OK
Analysis Coverage   shallow: go — pip install 'code-ledger[languages]'

Recommended:
   codeledger session reconcile
```

Last resort, and it is a safe one: delete `.ai/codeledger/` and run
`codeledger init` again.

---

## 9. Performance, and where to put your project

Measured on 800 source files plus 4,000 ignored files, cold:

| | `/mnt/c` (Windows drive via WSL) | Native Linux volume |
|---|---|---|
| no-op refresh | 0.66s | 0.029s |
| one-file refresh | 0.47s | 0.042s |
| 5,000 files, no-op | 3.8s | 0.25s |

**If a project can live under `~/` instead of `/mnt/c`, put it there.** It is
roughly 25× faster, and that is a property of the WSL filesystem boundary, not of
CodeLedger — every file check is a round-trip to the Windows filesystem driver.

### About the watcher

`codeledger watch` costs a full scan per poll, and everything it records is
`unknown` at `LOW` confidence. **Letting each agent call `refresh` itself is
both cheaper and more trustworthy** — that is the main path.

The watcher is the safety net for what nobody reports: an agent that crashed
before refreshing, a formatter, a `git checkout`, your own edits in another
editor.

**It waits before claiming anything.** Recording an edit is destructive to
authorship — once a file matches the index, the author's own refresh finds
nothing left to report, so the work would be credited to `unknown` with no
request attached, which also strips the request text that `progress` needs to
detect a repeated attempt. So the watcher leaves fresh edits alone for
`--claim-window` seconds (default 90) and records only what nobody claimed:

```text
1 recent edit(s) pending — leaving them for their author to claim.

Recorded UNCLAIMED change #1: login.py
  Nobody reported these within 90s, so they are attributed to 'unknown'.
  If an agent made them, have it call refresh itself next time.
```

That message is a useful signal in itself: if you keep seeing "UNCLAIMED" for
work an agent did, that agent is not calling `refresh` and you are losing
attribution and repeat detection.

On `/mnt/c`, slow the polling so scans do not overlap:

```bash
codeledger watch --agent codex --interval 15 --max-interval 60
```

Set `--claim-window 0` only if you want the old behaviour of recording
immediately, and accept that agents lose credit for their own work.

---

## 10. Cheat sheet

Each project gets **its own** virtual environment. Do not activate a shared venv
living inside the CodeLedger source folder — every project would then run
whichever version that one environment happens to hold, and you could not
upgrade projects independently.

```text
FIRST TIME (once per project)
─────────────────────────────
cd /path/to/project
python3 -m venv .venv
.venv/bin/pip install "code-ledger[languages] @ git+https://github.com/mikuare/Code-Ledger.git"
.venv/bin/codeledger init
.venv/bin/codeledger setup-agent claude-code      # and/or codex
.venv/bin/codeledger doctor


EVERY DAY
─────────
cd /path/to/project
.venv/bin/codeledger refresh --changed     # catch up on edits made while away

Then open Claude / Codex and work normally.
The agents query and refresh themselves over MCP.


TWO AGENTS AT ONCE
──────────────────
.venv/bin/codeledger session start --agent claude-code --request "today's task"
.venv/bin/codeledger session start --agent codex --request "their task"

Required for conflict warnings — MCP has no session tool, so without this
`active_agents` stays empty and overlaps are never reported.


AFTER YOU EDIT FILES YOURSELF
─────────────────────────────
.venv/bin/codeledger refresh --changed


AFTER A CRASH, OR CLOSING WSL
─────────────────────────────
.venv/bin/codeledger doctor
.venv/bin/codeledger refresh --changed


AFTER UPGRADING CODELEDGER
──────────────────────────
.venv/bin/pip install --upgrade "code-ledger[languages] @ git+https://github.com/mikuare/Code-Ledger.git"
.venv/bin/codeledger --version      # confirm the number actually moved
.venv/bin/codeledger init           # refresh the agent protocol files

The database migrates itself on the first command, additively and in one
transaction. History is preserved; no re-index is required.
`init` overwrites CLAUDE.md / AGENTS.md / CODEX.md — back them up if edited.
Skipping `init` leaves your agents on the old protocol, so new abilities
(resume, checkpoints, blast-radius warnings) never reach them.


CONTINUING WORK ACROSS SESSIONS
───────────────────────────────
.venv/bin/codeledger resume "the task you are picking up"

Agents do this themselves. Run it by hand to see what they will be told.


REMOVING CODELEDGER FROM A PROJECT
──────────────────────────────────
<your-agent> mcp remove codeledger   # claude / codex / gemini / cursor …
rm -rf .ai/codeledger
rm -f CLAUDE.md AGENTS.md CODEX.md   # only if they hold nothing of your own
.venv/bin/pip uninstall code-ledger

Your source code is never touched, so there is nothing else to undo.


WATCHER — optional safety net, not the main path
────────────────────────────────────────────────
Normally: don't run it. Agents refreshing themselves is cheaper and gives
HIGH-confidence attribution.

Run it when something edits files without reporting — a crash-prone agent,
a formatter, a teammate:

.venv/bin/codeledger watch --agent codex --interval 15 --max-interval 60

It holds fresh edits for 90s (--claim-window) so their author can claim them,
then records only what nobody did, as `unknown` / LOW.
```

If you prefer not to type `.venv/bin/` every time, activate it for the shell
session instead — but activate the **project's** environment, not CodeLedger's:

```bash
cd /path/to/project
source .venv/bin/activate
codeledger doctor
```

---

## 11. Known limits

- **No session tool over MCP.** Agents must be registered with
  `codeledger session start` for conflict detection to work (see section 5).
- **Renames read as a delete plus an add.** The old path is retired and the new
  one indexed; the link between them is not recorded.
- **Comment detection needs grammars.** Without `[languages]`, non-Python files
  report a comment edit as a code change, flagged `LOW confidence`.
- **The watcher is a foreground process.** It dies with its terminal, and cannot
  attribute what it sees. It is a safety net, not the main path.
- **The watcher records deletions immediately**, without waiting out the claim
  window — a deleted file has no mtime to age. So if an agent deletes a file and
  the watcher polls before the agent reports, that deletion is credited to
  `unknown`. Edits, which are the common case, are held for their author.
- **`--json` must come after the subcommand.** `codeledger status --json` works;
  `codeledger --json status` is silently ignored.
- **Not yet proven in long-running real-world use.** The test suite is thorough
  (139 tests, and successive audits found and fixed several correctness bugs), but
  tested is not the same as lived-in. Start on a project you can experiment with.

---

## 12. Removing CodeLedger from a project

CodeLedger is meant to be easy to walk away from. It never modifies your source
code, so detaching is deleting what `init` created and unregistering the MCP
server. There is no uninstall command to run and nothing to migrate back.

**What `init` created — six files, and nothing else:**

```text
.ai/codeledger/codeledger.db          the history
.ai/codeledger/config.json            ignores, extensions, thresholds
.ai/codeledger/agent-integration.md   setup notes
CLAUDE.md   AGENTS.md   CODEX.md      the agent protocol
```

Your files and your `.gitignore` are left exactly as they were.

**To detach:**

```bash
# 1. Stop the watcher if you have one running (Ctrl+C in its terminal).

# 2. Unregister the MCP server from each agent you set it up with.
claude mcp remove codeledger      # or: codex / gemini / cursor mcp remove codeledger

# 3. Delete what init created.
rm -rf .ai/codeledger
rm -f CLAUDE.md AGENTS.md CODEX.md

# 4. Remove the package.
pip uninstall code-ledger
```

**Before step 3, check those three protocol files.** If you added your own notes
to `CLAUDE.md`, delete only the CodeLedger protocol section and keep the rest.
(Worth knowing generally: `init` rewrites all three in full every time it runs,
so anything you add to them is already at risk — keep your own instructions in a
separate file.)

If you added `.ai/codeledger/` to `.gitignore`, that line can go too.

### Keeping the history without keeping the tool

The database is a plain SQLite file. If you may come back, move
`.ai/codeledger/codeledger.db` somewhere safe instead of deleting it — dropping
it back in later restores everything, and the schema migrates itself forward on
the first command whatever version it was written by. To read it without
CodeLedger installed:

```bash
sqlite3 codeledger.db "SELECT timestamp, agent, user_request, summary FROM changes ORDER BY id DESC LIMIT 20;"
```

### Pausing instead of removing

To stop CodeLedger participating without losing anything, just unregister the
MCP server (step 2) and leave the rest in place. Nothing runs on its own — there
is no daemon and no background process except the watcher, if you started one.
