"""Local benchmark: python3 benchmarks/benchmark_index.py [path]

With no argument this generates a synthetic multi-language project. Pass a real
repository path to measure that instead — synthetic files are uniform and easy
to index, so they flatter the numbers. The figures that matter are the ones
after the first run: `refresh --changed` on an unchanged tree is what `watch`
pays on every poll, and `impact` is what an agent pays on every question.
"""
import shutil
import sys
import tempfile
from pathlib import Path
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codeledger.core import Ledger
from codeledger.providers import capabilities

TEMPLATES = {
    "py":   "import os\n\ndef helper_{i}():\n    return {i}\n\nclass Service_{i}:\n    def run(self):\n        return helper_{i}()\n",
    "tsx":  "import {{ useAuth }} from '../hooks/useAuth';\n\nexport const format_{i} = (v) => v + {i};\n\nexport const View_{i} = () => {{\n  const u = useAuth();\n  return format_{i}(u);\n}};\n",
    "go":   "package main\n\nfunc helper{i}() int {{ return {i} }}\n\nfunc Run{i}() int {{ return helper{i}() }}\n",
    "rs":   "fn helper_{i}() -> i32 {{ {i} }}\n\npub fn run_{i}() -> i32 {{ helper_{i}() }}\n",
}

def build(root: Path, per_language: int = 80) -> None:
    for suffix, template in TEMPLATES.items():
        directory = root / "src" / suffix
        directory.mkdir(parents=True, exist_ok=True)
        for i in range(per_language):
            (directory / f"module_{i}.{suffix}").write_text(template.format(i=i), encoding="utf-8")
    noise = root / "node_modules" / "generated"        # must be pruned, not indexed
    noise.mkdir(parents=True, exist_ok=True)
    for i in range(300):
        (noise / f"ignored_{i}.js").write_text("export const ignored = 1;\n", encoding="utf-8")

def timed(label, function):
    started = perf_counter(); result = function(); elapsed = perf_counter() - started
    print(f"  {label:<26} {elapsed:8.3f}s", end="")
    return result, elapsed

def measure(root: Path) -> None:
    ledger = Ledger(root)
    first, _ = timed("init (cold)", lambda: ledger.init())
    print(f"   {first['metrics']['files_discovered']} files, {first['symbols_changed']} symbols")
    _, noop = timed("refresh --changed (no-op)", lambda: ledger.refresh(changed_only=True))
    print("   <- cost of one `watch` poll")

    target = next(iter(sorted(root.rglob("*.py"))), None) or next(iter(sorted(root.rglob("*"))))
    if target.is_file():
        target.write_text(target.read_text(encoding="utf-8", errors="ignore") + "\n# touched\n", encoding="utf-8")
    changed, _ = timed("refresh --changed (1 file)", lambda: ledger.refresh(changed_only=True))
    print(f"   {len(changed['files'])} file(s), symbols: {changed['symbols'][:3]}")

    # An ignored directory must cost nothing beyond the one stat that prunes it.
    # If these files ever appear in `files_discovered`, traversal is descending
    # into node_modules and the whole index is paying for it.
    metrics = ledger._last_discovery_metrics.as_dict()
    ignored_on_disk = sum(1 for _ in (root / "node_modules").rglob("*.js")) if (root / "node_modules").exists() else 0
    print(f"  {'ignored dirs pruned':<26} {metrics['directories_skipped']:8d}   "
          f"{ignored_on_disk} file(s) under node_modules never opened")

    name = next((row["name"] for row in ledger.db.execute("SELECT name FROM symbols WHERE status='active' LIMIT 1")), "helper")
    indexed, index_time = timed(f"impact {name!r} (index)", lambda: ledger.impact(name, fallback=False))
    print(f"   {len(indexed['referencing_files'])} referencing files")
    scanned, scan_time = timed(f"impact {name!r} (--scan)", lambda: ledger.impact(name, scan=True))
    print(f"   {len(scanned['referencing_files'])} referencing files")
    if index_time > 0:
        print(f"  {'index speedup':<26} {scan_time / index_time:8.0f}x")
    print(f"  {'watch poll / hour idle':<26} {noop * 1800:8.1f}s of CPU at a 2s interval")

if __name__ == "__main__":
    print(f"tree-sitter grammars installed: {capabilities()['tree_sitter_installed']}\n")
    if len(sys.argv) > 1:
        given = Path(sys.argv[1]).resolve()
        print(f"measuring real project: {given}")
        # Never write a ledger into someone's repository just to benchmark it.
        with tempfile.TemporaryDirectory() as directory:
            copy = Path(directory) / given.name
            shutil.copytree(given, copy, ignore=shutil.ignore_patterns(".git", ".ai", "node_modules", ".venv"))
            measure(copy)
    else:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            print(f"measuring synthetic project ({', '.join(TEMPLATES)})")
            build(root)
            measure(root)
