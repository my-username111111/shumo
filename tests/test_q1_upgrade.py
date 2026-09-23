"""Small exhaustive oracles, transport edge cases, and corruption detection."""
from dataclasses import replace
from itertools import product
from math import inf
import random
from types import SimpleNamespace
import unittest

import numpy as np

from model import Box, DEM, Leg, Node, Scenario, TransportType
from q1 import safe_payload, safe_payload_status, solve_all, solve_zone
from q1_frontier import (Label, ZonePatterns, budget_choice, critical_analysis,
                         global_frontiers, pareto, recover)
from q1_reference import global_reference, global_reserve_thresholds
from transport_check import TransportReplay
from verify import verify_q2


def terrain():
    d = DEM.__new__(DEM)
    d.width = d.nrows = 8
    d.dx = d.dy = 0.001
    d.x0 = d.y0 = 0.0
    d.nodata = -32767.0
    d.values = np.zeros((8, 8), dtype=np.float32)
    return d


def fixture():
    s = Scenario.__new__(Scenario)
    s.dem = terrain()
    s.nodes = {"O01": Node("O01",0.0005,-0.0005,0),
               "S01": Node("S01",0.0035,-0.0025,0), "S02": Node("S02",0.0055,-0.0035,0)}
    s.transport = {
        "A": TransportType("A",5,8,0.08,10,3000,1500,1,0.2,10,2,3,1,3,2,0.8),
        "B": TransportType("B",8,12,0.12,15,4000,1800,1.8,0.2,15,3,4,1,4,3,0.8)}
    boxes = [Box(str(i),z,"test",kg,0.01,False,None,9999,1)
             for i,(z,kg) in enumerate((("S01",3),("S01",7),("S01",3),("S02",3),("S02",7)))]
    s.boxes = {b.id:b for b in boxes}
    s.zone_boxes = {z:[b for b in boxes if b.zone==z] for z in ("S01","S02")}
    s.aircraft = {"U1":"A","U2":"B"}
    s.battery_count = {"A":1,"B":1}
    s.battery_charge = {"A":100,"B":200}
    return s


def exhaustive(s, replay, zone):
    """Actually enumerate every partition and type assignment; no DP/pruning."""
    def rec(remaining):
        if not remaining:
            return [(0,0.0,0.0,1.0)]
        first, rest = remaining[0], remaining[1:]
        result = []
        for bits in product((False,True),repeat=len(rest)):
            chosen = [first]+[b for b,yes in zip(rest,bits) if yes]
            other = tuple(b for b,yes in zip(rest,bits) if not yes)
            for model in s.transport:
                try:
                    r = replay.evaluate(model,[(zone,chosen)],reserve=0)
                except AssertionError:
                    continue
                for n,e,t,rho in rec(other):
                    result.append((n+1,e+r["energy_kwh"],t+r["duration"],min(rho,r["return_soc"])))
        return result
    return rec(tuple(b.id for b in s.zone_boxes[zone]))


def brute_frontier(rows):
    points = sorted(set((round(e,9),round(t,6)) for e,t in rows))
    return [a for a in points if not any(b!=a and b[0]<=a[0] and b[1]<=a[1] for b in points)]


class GeometryTests(unittest.TestCase):
    def test_edge_corner_and_reversal(self):
        d=terrain()
        self.assertEqual(set(d.crossed_pixels((0.001,-0.0002),(0.001,-0.0028))),
                         {(x,y) for x in (0,1) for y in (0,1,2)})
        self.assertEqual(set(d.crossed_pixels((0.001,-0.001),(0.001,-0.001))),
                         {(0,0),(0,1),(1,0),(1,1)})
        a,b=(0.0005,-0.0005),(0.0025,-0.0025)
        self.assertEqual(d.crossed_pixels(a,b),d.crossed_pixels(b,a))
        self.assertIn((1,0),d.crossed_pixels(a,b))
        self.assertIn((0,1),d.crossed_pixels(a,b))

    def test_random_supercover_independent_rectangles(self):
        d=terrain()
        replay=TransportReplay(SimpleNamespace(dem=d))
        rng=random.Random(2026)
        for _ in range(200):
            a=(rng.uniform(.00001,.00799),-rng.uniform(.00001,.00799))
            b=(rng.uniform(.00001,.00799),-rng.uniform(.00001,.00799))
            self.assertEqual(d.crossed_pixels(a,b),replay.cells(a,b))
        for a,b in (((.001,-.0002),(.001,-.0068)),((.001,-.001),(.006,-.006)),((0,0),(0,-.007))):
            self.assertEqual(d.crossed_pixels(a,b),replay.cells(a,b))

    def test_partial_nodata_must_fail(self):
        d=terrain()
        d.values[1,1]=d.nodata
        with self.assertRaises(ValueError):
            d.peak((.0005,-.0005),(.0025,-.0025))
        d.values[1,1]=float("nan")
        with self.assertRaises(ValueError):
            d.peak((.0005,-.0005),(.0025,-.0025))
        with self.assertRaises(ValueError):
            d.crossed_pixels((-.001,0),(.001,-.001))


class PhysicsTests(unittest.TestCase):
    def test_hand_calculated_energy_time(self):
        s=fixture()
        leg=Leg(1000,100,50,30,50)
        expected=1000/1500+(5+8)*9.81*50/(.8*3600000)
        self.assertAlmostEqual(s.flight_energy("A",leg,8),expected,places=12)
        self.assertAlmostEqual(s.flight_time("A",leg),50/3+1000/10+30/2,places=12)
        self.assertLess(s.flight_energy("A",leg,0),s.flight_energy("A",leg,8))

    def test_replay_does_not_call_shared_physics(self):
        s=fixture()
        visits=[("S01",["0"]),("S02",["3"])]
        result=s.route("A",visits)
        def fail(*args):
            raise AssertionError("production method called")
        s.node_leg=s.flight_energy=s.flight_time=s.route=fail
        checked=TransportReplay(s).evaluate("A",visits)
        self.assertAlmostEqual(result["energy_kwh"],checked["energy_kwh"],places=12)
        self.assertAlmostEqual(result["duration"],checked["duration"],places=9)
        self.assertAlmostEqual(checked["legs"][1]["payload_kg"],3)
        self.assertEqual(checked["legs"][-1]["payload_kg"],0)

    def test_invalid_reserve_and_infeasible_empty(self):
        s=fixture()
        for rho in (-.1,1.1,float("nan"),inf):
            with self.assertRaises(ValueError):
                safe_payload(s,"S01","A",rho)
        s.transport["A"]=replace(s.transport["A"],empty_range=100,full_range=50)
        status=safe_payload_status(s,"S01","A")
        self.assertFalse(status["empty_round_trip_feasible"])
        self.assertIsNone(status["max_payload_kg"])

    def test_duplicates_invalid_parameters_and_cargo(self):
        with self.assertRaises(ValueError):
            Scenario._unique_ids([["a"],["a"]],"test")
        s=fixture()
        s.validate_transport_data()
        with self.assertRaises(ValueError):
            solve_zone(s,"S01",box_ids={"missing"})
        with self.assertRaises(AssertionError):
            TransportReplay(s).evaluate("A",[("S01",["0","0"])])
        with self.assertRaises(AssertionError):
            TransportReplay(s).evaluate("A",[("S02",["0"])])
        s.transport["A"]=replace(s.transport["A"],climb_efficiency=0)
        with self.assertRaises(ValueError):
            s.validate_transport_data()


class OptimizationTests(unittest.TestCase):
    def test_both_dps_against_exhaustive_partitions(self):
        s=fixture()
        replay=TransportReplay(s)
        zones={z:ZonePatterns(s,z) for z in s.zone_boxes}
        _,frontiers,_=global_frontiers(zones,extra_sorties=len(s.boxes))
        raw={z:exhaustive(s,replay,z) for z in zones}
        combinations=[(a[0]+b[0],a[1]+b[1],a[2]+b[2],min(a[3],b[3]))
                      for a in raw["S01"] for b in raw["S02"] if min(a[3],b[3])>=.2]
        for n,rows in frontiers.items():
            expected=brute_frontier([(e,t) for count,e,t,rho in combinations if count==n])
            actual=sorted((round(p.energy,9),round(p.seconds,6)) for p in rows)
            self.assertEqual(actual,expected)
            for p in rows:
                replay.check_q1(recover(zones,p))
        expected=brute_frontier([(e,t) for n,e,t,rho in combinations])
        reference,_=global_reference(s,replay)
        self.assertEqual([(round(e,9),round(t,6)) for e,t in reference],expected)

    def test_critical_thresholds_against_bruteforce(self):
        s=fixture()
        replay=TransportReplay(s)
        thresholds,_=global_reserve_thresholds(s,replay)
        a,b=exhaustive(s,replay,"S01"),exhaustive(s,replay,"S02")
        for n,rho in thresholds.items():
            expected=max(min(x[3],y[3]) for x in a for y in b if x[0]+y[0]==n)
            self.assertAlmostEqual(rho,expected,places=12)
        zones={z:ZonePatterns(s,z) for z in s.zone_boxes}
        analysis=critical_analysis(zones,0,1)
        for row in analysis["global_intervals"]:
            for rho in ((row["left_open"]+row["right_closed"])/2,row["right_closed"]):
                feasible=[n for n,r in thresholds.items() if rho<=r+1e-10]
                self.assertEqual(row["sorties"],min(feasible) if feasible else None)
        with self.assertRaises(ValueError):
            critical_analysis(zones,.5,.1)

    def test_global_budget_is_not_per_zone_budget(self):
        # Spending all additional energy in one zone is globally better.
        rows=pareto([Label(10,100,()),Label(11,20,()),Label(12,95,()),Label(13,15,())])
        chosen=budget_choice(rows,11)
        self.assertEqual((chosen.energy,chosen.seconds),(11,20))
        self.assertIsNone(budget_choice(rows,9))
        self.assertEqual(budget_choice(rows,13).seconds,15)

    def test_corrupted_export_is_rejected(self):
        s=fixture()
        q1=solve_all(s,sensitivity=False)
        q1["zone_plans"]["S01"]["batches"][0]["energy"]+=.01
        with self.assertRaises(AssertionError):
            TransportReplay(s).check_q1(q1["zone_plans"])

    def test_fake_aircraft_and_battery_are_rejected(self):
        s=fixture()
        def make(identifier,model,drone,battery,start,visits):
            route=s.route(model,visits)
            row=dict(id=identifier,drone=drone,model=model,battery=battery,start=start,
                     return_time=start+route["duration"],
                     battery_ready=start+route["duration"]+s.charge_time(route["return_soc"],s.battery_charge[model]),
                     energy_kwh=route["energy_kwh"],return_soc=route["return_soc"],
                     visits=route["visits"],load_kg=route["load_kg"],load_m3=route["load_m3"],route=route)
            return row,{bid:dict(time=start+time,sortie=identifier,zone=s.boxes[bid].zone)
                        for bid,time in route["delivery"].items()}
        row,d1=make("T1","A","U1","A01",0,[("S01",["0"]),("S02",["3"])])
        row2,d2=make("T2","B","U2","B01",0,[("S01",["1","2"])])
        row3,d3=make("T3","B","U2","B01",row2["battery_ready"],[("S02",["4"])])
        rows=[row,row2,row3]
        deliveries={**d1,**d2,**d3}
        verify_q2(s,{"sorties":rows,"deliveries":deliveries})
        row["drone"]="U_FAKE"
        with self.assertRaises(AssertionError):
            verify_q2(s,{"sorties":rows,"deliveries":deliveries})
        row["drone"]="U1"
        row["battery"]="A_FAKE"
        with self.assertRaises(AssertionError):
            verify_q2(s,{"sorties":rows,"deliveries":deliveries})


if __name__=="__main__":
    unittest.main()
