# Language support design

CodeLedger's value depends on one thing: when it says *nothing depends on this
symbol*, that has to be true. Today that claim is trustworthy for Python, decent
for JavaScript and TypeScript, and thin everywhere else. This document describes
how the analysis layer becomes uniform across modern languages without giving up
the two properties the project is built on — it stays local, and it never
asserts what it cannot back up.

## The problem with the current design

Analysis is hard-coded in `parser.py`. `parse_file` branches on "is this
Python?" and falls through to a list of regexes; `dependencies` does the same.
That produces three different qualities of answer that the rest of the system
cannot distinguish:

| Language | Symbols | Edges | Trustworthy? |
|---|---|---|---|
| Python | AST | calls, uses, imports | yes |
| JS/TS/JSX/Vue/Svelte | line regex | imports, in-file calls via brace ranges | mostly |
| Go, Rust, Java, C#, C++, Ruby, PHP, Kotlin, Swift | line regex | imports only | no |

The third row is the problem. `impact` cannot tell "no dependents" from "no
coverage", so it either over-trusts an empty index or falls back to scanning
every file — which on `pip` produced 35% false positives, matching the word
`Command` inside docstrings and comments.

## Design

### 1. Providers

Analysis moves behind a small protocol. A provider turns source text into
symbols and edges, and declares how much it can be trusted.

```python
class LanguageProvider(Protocol):
    name: str
    coverage: Coverage                     # FULL | SHALLOW | NONE
    def symbols(self, path, text) -> list[SymbolData]: ...
    def edges(self, path, text, symbols) -> list[Edge]: ...
```

Three implementations, resolved per file in this order:

1. **`TreeSitterProvider`** — `coverage=FULL`. Real parse trees for every
   installed grammar.
2. **`PythonAstProvider`** — `coverage=FULL`. Kept because it needs no
   dependency and is already exact; it is the zero-install guarantee that at
   least one language is fully covered.
3. **`RegexProvider`** — `coverage=SHALLOW`. Today's line patterns, unchanged.
   The floor, not the ceiling.

### 2. Generic tree walking, not 100 hand-written query files

Writing tree-sitter queries per language does not scale to "every modern
language" and rots as grammars change. Grammar authors follow strong naming
conventions, so a generic walker gets most languages for free:

| Concept | Node types matched |
|---|---|
| definition | type ends in `_definition`, `_declaration`, `_item`, or `_specifier` **and** has a `name` field |
| call | type in `call_expression`, `call`, `method_invocation`, `invocation_expression`, `function_call` |
| import | type contains `import`, `use_declaration`, `package_clause`, `require` |

A per-language override table handles the deviations (Go's `type_spec`, Rust's
`use_declaration` path shape, C#'s namespaces). The override table is expected
to grow; the generic path is expected to carry the long tail. A language with no
grammar and no override still works — it falls to `RegexProvider`.

Because the walker has real scopes, two things improve for free:

- **Accurate ranges.** No more brace counting, so a call is attributed to the
  symbol that truly encloses it, even with braces inside strings.
- **Qualified names.** `qualified_name` becomes `Class.method` rather than a
  copy of `name`. `lookup` stops conflating every `render` in the repository,
  which is the largest remaining source of `impact` false positives.

### 3. Coverage is data, not a comment

Each indexed file records the provider and coverage tier that produced it. That
single fact removes the guessing:

- `impact` trusts the index for `FULL` files and automatically scans for
  `SHALLOW` ones, instead of inferring coverage from an empty result.
- `status` and `doctor` report coverage per language, so a Go developer is told
  their impact analysis is shallow rather than discovering it by being wrong.
- `scope` can weight its boundary by coverage.

This is the same principle already applied to `risk: UNKNOWN` and
`boundary_evidence`, extended to language analysis.

### 4. Optional dependency, never a hard requirement

`pip install codeledger` stays dependency-free and behaves exactly as it does
today. Grammars are an extra:

```bash
pip install "codeledger[languages]"     # tree-sitter + tree-sitter-language-pack, ~3 MB
```

`TreeSitterProvider` is selected only if the import succeeds and a grammar for
the language loads. Any failure degrades to the current behaviour and is
reported by `doctor` — it never raises. Nothing about the local-first, no-network
guarantee changes: grammars are compiled parsers shipped in the wheel, and no
source ever leaves the machine.

### 5. Migration

`files.analysis_version` already exists and is already part of the staleness
check. Providers stamp it (`ast:1`, `tree-sitter:1`, `regex:1`). When a file's
recorded version differs from what the current provider would produce, the file
is reparsed on the next `refresh --changed`. Installing the extra therefore
upgrades an existing index incrementally, with no re-init and no migration
command.

## Sequencing

1. Extract the provider protocol; move existing Python-AST and regex code behind
   it with no behaviour change. Tests stay green.
2. Record provider and coverage per file; expose in `status`/`doctor`; switch
   `impact`'s fallback from "empty result" to "coverage is not FULL".
3. Add `TreeSitterProvider` with the generic walker plus overrides for Go, Rust,
   Java, C#, Kotlin, Swift, Ruby, PHP.
4. Qualified names and scope-accurate call attribution.
5. Benchmark against a large repository per language; publish the coverage table
   in the README so the claim is checkable rather than marketing.

Step 1 and 2 are worth doing even if tree-sitter is never adopted: they make the
existing dishonesty visible and fixable.
