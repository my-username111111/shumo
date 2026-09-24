"""Check one further merge for each distinct saved 21-sortie route partition."""

from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter
from pathlib import Path
from shutil import copyfile


ROOT = Path(__file__).resolve().parent


def signature(plan: dict) -> tuple:
    return tuple(sorted(tuple((zone, tuple(boxes)) for zone, boxes in row["visits"])
                        for row in plan["sorties"]))


def main() -> None:
    packaged = ROOT / "results_q2_extended" / "search_20_inputs"
    packaged.mkdir(parents=True, exist_ok=True)
    if not list(packaged.glob("branch_*.json")):
        unique = {}
        for path in sorted((ROOT / "results_q2_beam_21").glob(
                "*/candidate_plans/*.json")):
            plan = json.loads(path.read_text(encoding="utf-8"))
            unique.setdefault(signature(plan), path)
        for index, path in enumerate(unique.values(), 1):
            copyfile(path, packaged / f"branch_{index:02d}.json")
    sources = {}
    for path in sorted(packaged.glob("branch_*.json")):
        plan = json.loads(path.read_text(encoding="utf-8"))
        sources.setdefault(signature(plan), path)
    if not sources:
        raise FileNotFoundError("No packaged 21-sortie branch plans to audit")
    output = ROOT / "results_q2_extended" / "search_20_branches"
    output.mkdir(parents=True, exist_ok=True)
    rows = []
    statuses = Counter()
    for index, path in enumerate(sources.values(), 1):
        target = output / f"branch_{index:02d}"
        command = [sys.executable, str(ROOT / "q2_merge_search.py"),
                   "--input", str(path), "--output", str(target),
                   "--limit", "1000", "--seconds-per-candidate", "3",
                   "--makespan-cap", "10000"]
        if not (target / "merge_search.json").exists():
            subprocess.run(command, check=True, cwd=ROOT, stdout=subprocess.DEVNULL)
        result = json.loads((target / "merge_search.json").read_text(encoding="utf-8"))
        statuses.update(case["status"] for case in result["cases"])
        row = dict(branch=index, source=str(path.relative_to(ROOT)),
                   candidates=result["physical_timing_candidates"],
                   tested=result["tested"],
                   feasible=sum(case["objective"] is not None for case in result["cases"]),
                   best_objective=result["best_objective"])
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    summary = dict(scope="one pairwise merge per distinct saved 21-sortie partition",
                   route_partitions_tested=len(rows),
                   candidate_schedules_tested=sum(row["tested"] for row in rows),
                   feasible_20_sortie_schedules=sum(row["feasible"] for row in rows),
                   solver_status_counts=dict(statuses),
                   branches=rows)
    (ROOT / "results_q2_extended" / "search_20_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
