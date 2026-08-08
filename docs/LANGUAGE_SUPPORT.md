# Language support design

**Status: implemented.** This documents what the analysis layer does and why it
is built this way.

CodeLedger's value depends on one thing: when it says *nothing depends on this
symbol*, that has to be true. Language coverage is what makes that claim
honest, so coverage is modelled explicitly rather than assumed.

## The problem this replaced

Analysis used to be hard-coded in `parser.py`: `parse_file` branched on "is this
Python?" and fell through to a list of regexes, and `dependencies` did the same.
That produced three different qualities of answer that nothing downstream could
distinguish:

| Language | Symbols | Edges | Trustworthy? |
|---|---|---|---|
| Python | AST | calls, uses, imports | yes |
| JS/TS/JSX/Vue/Svelte | line regex | imports, in-file calls via brace ranges | mostly |
| Go, Rust, Java, C#, C++, Ruby, PHP, Kotlin, Swift | line regex | imports only | no |

The third row was the problem. `impact` could not tell "no dependents" from "no
coverage", so it either over-trusted an empty index or fell back to scanning
every file — which on `pip` produced 35% false positives, matching the word
`Command` inside docstrings and comments.

## Design

### 1. Providers

Analysis sits behind a small protocol in `providers.py`. A provider turns source
text into symbols and edges, and declares how far it can be trusted.

```python
class LanguageProvider(Protocol):
    name: str
    coverage: str                          # FULL | SHALLOW | NONE
    def symbols(self, path, text) -> list[SymbolData]: ...
    def edges(self, path, text, symbols) -> list[Edge]: ...
```

Three implementations, resolved per file in this order:

1. **`TreeSitterProvider`** — `FULL`. Real parse trees for every installed grammar.
2. **`PythonAstProvider`** — `FULL`. Kept because it needs no dependency and is
   already exact; it guarantees at least one fully covered language with a
   zero-dependency install.
3. **`RegexProvider`** — `SHALLOW`. The original line patterns. The floor, not
   the ceiling.

`analyze()` never raises. A grammar that chokes on a file degrades to the regex
provider and reports the weaker tier.

### 2. Generic tree walking, not per-language query files

Hand-writing tree-sitter queries per language does not scale to "every modern
language" and rots as grammars change. Grammar authors follow strong naming
conventions, so a generic walker carries most languages:

| Concept | Matched by |
|---|---|
| definition | type in `DEFINITION_TYPES`, or ending in `_definition`, `_declaration`, `_item`, `_spec` |
| definition (JS/TS) | `variable_declarator` whose value is an arrow/function expression |
| call | type in `CALL_TYPES` (`call_expression`, `method_invocation`, …) |
| bare call | an identifier matching a symbol defined in the same file (Ruby, Elixir) |
| import | type containing `import`, `use_declaration`, `package_clause`, `require` |

Two exclusions matter, both found by testing rather than reasoning:

- `import_specifier` ends in a definition-like suffix, so suffix matching alone
  registered **every imported name as a definition**. `NON_DEFINITION` blocks it.
- Names are not always on a `name` field. Kotlin uses a `simple_identifier`
  child; C and C++ nest the name inside a `declarator` chain
  (`function_definition → function_declarator → identifier`). `_name_of` walks
  both.

Because the walker has real scopes, ranges are accurate without brace counting,
and `qualified_name` can carry `Class.method` rather than duplicating `name`.

### 3. Coverage is data, not a comment

Each indexed file records the provider and coverage tier that produced it
(`files.analysis_provider`, `files.coverage`). That single fact removes the
guessing:

- `impact` trusts the index for `FULL` files and verifies against the working
  tree for `SHALLOW` ones, instead of inferring coverage from an empty result.
- `status` and `doctor` report `shallow_languages` with an install hint, so a Go
  developer is told their impact analysis is shallow rather than discovering it
  by being wrong.

There is one safety net: a `FULL` provider that extracts **no** symbols while the
line patterns find two or more is downgraded to `SHALLOW`. A grammar returning
nothing must not be reported as full coverage. The two-symbol threshold exists
because a file legitimately holding no symbols is normal, and the line patterns
will hallucinate a single match out of a docstring given the chance.

### 4. Optional dependency, never a hard requirement

`pip install code-ledger` stays dependency-free and behaves as it always did.
Grammars are an extra:

```bash
pip install "code-ledger[languages]"     # tree-sitter + grammar pack, ~3 MB
```

`TreeSitterProvider` is selected only if the import succeeds and a grammar
loads. Any failure degrades and is reported by `doctor`. Nothing about the
local-first guarantee changes: grammars are compiled parsers shipped in the
wheel, and no source leaves the machine.

### 5. Migration

Providers stamp `files.analysis_version` (`ast:1`, `tree-sitter:1`, `regex:1`),
which is part of the staleness check. When a file's recorded version differs
from what the current provider would produce, it is reparsed on the next
`refresh --changed`. Installing or removing the extra therefore upgrades an
existing index in place — no re-init, no migration command.

## Verified

`test_every_supported_language_yields_symbols_and_a_call_graph` builds a real
file in nine languages and asserts each yields symbols, `FULL` coverage, and at
least one internal call edge:

| Go | Rust | Java | C# | Ruby | PHP | Kotlin | Swift | C++ |
|---|---|---|---|---|---|---|---|---|
| ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |

Measured on `pip` (495 files, 185k lines): 487 files fully analysed in 3.5s,
faster than the 4.9s regex-and-AST path it replaced.

The other 360+ grammars in the pack are expected to work through the generic
walker but are **not** individually verified — each could have its own
`variable_declarator` surprise. The safety net degrades those to `shallow`
rather than reporting wrong answers. Adding a language to the test is a
three-line change to `POLYGLOT` in `tests/test_codeledger.py`.
