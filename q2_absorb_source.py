"""Empty one Q2 sortie into two routes, allowing new zone visits and visit reordering."""

from __future__ import annotations

import argparse
import json
from itertools import permutations, product
from pathlib import Path

from model import BASE, Scenario
from q2 import Dispatch, Job
from q2_exact_schedule import solve_case
from q2_merge_search import dummy_plan, jobs_from_plan
from verify import verify_q2


ROOT = Path(__file__).resolve().parent


def repartition(job: Job, added: tuple[str, ...], s: Scenario) -> list[Job]:
    grouped = {zone: list(ids) for zone, ids in job.visits}
    for bid in added:
        grouped.setdefault(s.boxes[bid].zone, []).append(bid)
    return [Job(job.id, tuple((zone, tuple(sorted(grouped[zone]))) for zone in order))
            for order in permutations(sorted(grouped))]


def feasible_options(dispatcher: Dispatch, job: Job) -> list[dict]:
    result = []
    for model in dispatcher.s.transport:
        route = dispatcher.evaluate(job, model)
        if route is None:
            continue
        if all(relative <= (dispatcher.s.boxes[bid].hard_deadline
                            if dispatcher.s.boxes[bid].hard_deadline is not None
                            else dispatcher.s.boxes[bid].desired) + 1e-7
               for bid, relative in route["delivery"].items()):
            result.append(route)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=BASE)
    parser.add_argument("--input", type=Path,
                        default=ROOT / "results_q2_three_to_two_fast" / "weighted_refined" / "plan.json")
    parser.add_argument("--source-sortie", type=int, default=4)
    parser.add_argument("--recipient-sorties", type=str, default="15,17")
    parser.add_argument("--output", type=Path, default=ROOT / "results_q2_absorb_18")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--seconds-per-candidate", type=float, default=4)
    parser.add_argument("--makespan-cap", type=float, default=10000)
    parser.add_argument("--objective", choices=("energy", "makespan", "weighted"),
                        default="energy")
    args = parser.parse_args()
    s = Scenario(args.data_dir)
    source = json.loads(args.input.read_text(encoding="utf-8"))
    verify_q2(s, source)
    dispatcher = Dispatch(s)
    jobs = jobs_from_plan(source)
    source_index = args.source_sortie - 1
    recipients = tuple(int(x) - 1 for x in args.recipient_sorties.split(","))
    if (source_index < 0 or source_index >= len(jobs) or len(recipients) != 2
            or len({source_index, *recipients}) != 3
            or any(i < 0 or i >= len(jobs) for i in recipients)):
        parser.error("Choose one source and two distinct valid recipients")
    boxes = jobs[source_index].box_ids
    candidates = {}
    for assignment in product((0, 1), repeat=len(boxes)):
        added = [tuple(b for b, side in zip(boxes, assignment) if side == k)
                 for k in (0, 1)]
        left = [(job, feasible_options(dispatcher, job))
                for job in repartition(jobs[recipients[0]], added[0], s)]
        right = [(job, feasible_options(dispatcher, job))
                 for job in repartition(jobs[recipients[1]], added[1], s)]
        for left_job, left_options in left:
            if not left_options:
                continue
            for right_job, right_options in right:
                if not right_options:
                    continue
                signature = (left_job.visits, right_job.visits)
                candidates[signature] = dict(left=left_job, right=right_job,
                                             energy=min(x["energy_kwh"] for x in left_options)
                                             + min(x["energy_kwh"] for x in right_options))
    ranked = sorted(candidates.values(), key=lambda x: x["energy"])
    args.output.mkdir(parents=True, exist_ok=True)
    results = []
    best = None
    for rank, candidate in enumerate(ranked[:args.limit], 1):
        updated = [job for k, job in enumerate(jobs) if k != source_index]
        for old_index, new_job in zip(recipients, (candidate["left"], candidate["right"])):
            updated[old_index - (old_index > source_index)] = new_job
        updated = [Job(f"J{k:03d}", job.visits) for k, job in enumerate(updated, 1)]
        trial = dummy_plan(s, dispatcher, updated, source["objective"]["makespan"])
        case = {"energy": "alns", "makespan": "makespan",
                "weighted": "main"}[args.objective]
        plan, report = solve_case(s, trial, case, 1,
                                  args.seconds_per_candidate, 8, 2026 + rank,
                                  makespan_cap=args.makespan_cap)
        result = dict(rank=rank, added_routes=[candidate["left"].visits,
                                                   candidate["right"].visits],
                      estimate_energy_kwh=candidate["energy"],
                      status=report["status"],
                      objective=plan["objective"] if plan else None)
        results.append(result)
        print(json.dumps({k: v for k, v in result.items() if k != "added_routes"},
                         ensure_ascii=False), flush=True)
        keys = {"energy": ("energy_kwh", "makespan"),
                "makespan": ("makespan", "energy_kwh"),
                "weighted": ("weighted_delivery_seconds", "energy_kwh")}
        measure = lambda p: tuple(p["objective"][key] for key in keys[args.objective])
        if plan is not None and (best is None or measure(plan) < measure(best)):
            best = plan
    report = dict(source=str(args.input), source_objective=source["objective"],
                  source_sortie=source_index + 1,
                  recipients=[i + 1 for i in recipients], objective=args.objective,
                  physical_candidates=len(candidates), tested=len(results),
                  cases=results, best_objective=best["objective"] if best else None)
    (args.output / "search.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if best is not None:
        verify_q2(s, best)
        (args.output / "best_plan.json").write_text(
            json.dumps(best, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
