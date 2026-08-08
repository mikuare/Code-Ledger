"""Small repeatable local benchmark: python3 benchmarks/benchmark_index.py"""
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codeledger.core import Ledger

with TemporaryDirectory() as directory:
    root = Path(directory)
    for index in range(250):
        path = root / "src" / f"module_{index}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"def function_{index}():\n    return {index}\n", encoding="utf-8")
    (root / "node_modules" / "generated").mkdir(parents=True)
    for index in range(250):
        (root / "node_modules" / "generated" / f"ignored_{index}.py").write_text("def ignored():\n pass\n")
    ledger = Ledger(root)
    started = perf_counter(); first = ledger.init(); first_time = perf_counter() - started
    started = perf_counter(); second = ledger.refresh(changed_only=True); second_time = perf_counter() - started
    (root / "src" / "module_1.py").write_text("def function_1():\n    return 999\n", encoding="utf-8")
    started = perf_counter(); third = ledger.refresh(changed_only=True); third_time = perf_counter() - started
    print({"initial_seconds": round(first_time, 4), "no_change_seconds": round(second_time, 4), "one_file_seconds": round(third_time, 4), "initial_files": first["metrics"]["files_discovered"], "no_change_files": second["files"], "one_file_files": third["files"]})
