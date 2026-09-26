"""Search for a lower Q4 resource deficit while keeping one Q3 schedule feasible.

The input Q3 is never changed in place.  The CP model keeps its box batches and
visit orders, and searches aircraft types, start times, physical resources and
relay schedules.  Every saved candidate is independently recertified for Q3
and all legal Q4 partitions before it is compared with the input.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from pathlib import Path

from model import Scenario
from q3_joint import Geometry, site_pool, solve
from q3_upgrade import dump_json
from search_q34_compromise import read, save_candidate
from optimize_q3_imported import partition_atoms
from deliver_q34_compromise import choose
from optimize_q3_time_guarded import matching


ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT = ROOT / "results_q3_imported/selection/primary/q3_plan.json"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--protected", type=Path,
                        default=DEFAULT_INPUT.parent / "protected_partitions.json")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--two-shortage", type=int, default=7)
    parser.add_argument("--three-shortage", type=int, default=11)
    parser.add_argument("--makespan-extra", type=float, default=5.0)
    parser.add_argument("--energy-extra", type=float, default=0.15)
    parser.add_argument("--delivery-extra", type=float, default=3000.0)
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--seed", type=int, default=2601)
    parser.add_argument("--fixed-models", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    scenario = Scenario()
    baseline = read(args.input)
    protected = read(args.protected)
    old = [protected[str(k)] for k in (2, 3)]
    atoms = partition_atoms(old)
    limits = []
    for partition, shortage in zip(old, (args.two_shortage, args.three_shortage)):
        limits.append(dict(
            blocks=[[i for i, atom in enumerate(atoms)
                     if set(atom) <= set(group["zones"])]
                    for group in partition["groups"]],
            shortage_total=shortage,
            resource_total=partition["resource_total"],
        ))
    caps = dict(
        soft=0,
        makespan=baseline["objective"]["makespan"] + args.makespan_extra,
        weighted=baseline["objective"]["weighted_delivery_seconds"] + args.delivery_extra,
        energy=(baseline["objective"]["energy_kwh"] + args.energy_extra) * 1e6,
        sorties=len(baseline["relay_sorties"]),
    )
    dump_json(args.output / "configuration.json", dict(
        input=str(args.input), protected=str(args.protected), output=str(args.output),
        target_shortage={"2": args.two_shortage, "3": args.three_shortage},
        atoms=atoms, limits=limits, caps=caps, seconds=args.seconds,
        seed=args.seed, fixed_models=args.fixed_models,
    ))
    geometry = Geometry(scenario, site_pool(scenario, baseline),
                        extra=baseline["extra_loss_db"], interval=12.0)
    source = deepcopy(dict(sorties=baseline["transport_sorties"],
                           relay_sorties=baseline["relay_sorties"]))
    candidate, report = solve(
        scenario, source, geometry, seconds=args.seconds, seed=args.seed,
        slots=2, variable_models=not args.fixed_models, policy="makespan",
        q4_groups=atoms, resource_buffer=5.0, objective_caps=caps,
        partition_resource_limits=limits,
    )
    dump_json(args.output / "solver_report.json", report)
    if candidate is None:
        dump_json(args.output / "result.json", dict(status="NO_CERTIFIED_PLAN",
                                                     stages=report["stages"]))
        return
    candidate, q4, _ = save_candidate(scenario, candidate, args.output)
    selected = {}
    for k in (2, 3):
        partition = matching(q4, protected[str(k)])
        selected[str(k)] = dict(
            id=partition["id"], shortage=partition["shortage_total"],
            resources=partition["resource_total"], cv=partition["workload_cv"],
        ) if partition else None
    selected["best_balanced"] = {
        str(k): dict(id=(best := choose(q4, k))["id"],
                     shortage=best["shortage_total"],
                     resources=best["resource_total"], cv=best["workload_cv"])
        for k in (2, 3)
    }
    dump_json(args.output / "result.json", dict(
        status="CERTIFIED_CANDIDATE", baseline=baseline["objective"],
        candidate=candidate["objective"], selected_partitions=selected,
    ))


if __name__ == "__main__":
    main()
