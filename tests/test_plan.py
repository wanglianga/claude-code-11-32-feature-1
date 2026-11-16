"""Tests for promotion-plan combination reach tasks.

Unit tests cover the exact set algebra (scan vs bitmap), missing-data bounds,
marginals, arbitrary-order intersections and the constrained greedy search.
API tests run the acceptance scenarios on the fictional dataset: high overlap,
high marginal, missing-data bounds and mutually exclusive slots.
"""
from __future__ import annotations

import random

import pytest

from app.analytics.reach import (
    BitmapEvaluator,
    BitmapIndex,
    PlanPerson,
    ScanEvaluator,
    evaluate_set,
    greedy_optimize,
    person_breakdown,
    program_status,
)
from app.errors import ServiceError
from demo import fixtures

THRESHOLD = 5.0


def _person(pid, weight, minutes, covered):
    return PlanPerson(pid, weight, dict(minutes), dict(covered))


# ---------------------------------------------------------------------------
# Unit: status classification and missing-data bounds
# ---------------------------------------------------------------------------
def test_program_status_three_way():
    assert program_status(10.0, True, THRESHOLD) == "reached"
    assert program_status(10.0, False, THRESHOLD) == "reached"  # confirmed wins over missing
    assert program_status(0.0, True, THRESHOLD) == "not_reached"
    assert program_status(3.0, True, THRESHOLD) == "not_reached"
    assert program_status(0.0, False, THRESHOLD) == "unknown"
    assert program_status(3.0, False, THRESHOLD) == "unknown"


def _bound_sample():
    """Four persons exercising every set-status bucket."""
    return [
        _person("P1", 100.0, {"a": 30, "b": 10}, {"a": True, "b": True}),   # reached both
        _person("P2", 200.0, {"a": 30}, {"a": True, "b": True}),            # reached a only
        _person("P3", 300.0, {}, {"a": True, "b": False}),                  # unknown b, not_reached a
        _person("P4", 400.0, {}, {"a": True, "b": True}),                   # not_reached both
    ]


def test_union_bounds_unknown_never_not_reached():
    ev = ScanEvaluator(_bound_sample(), THRESHOLD)
    out = ev.union_bounds(["a", "b"])
    assert out["confirmed"] == 300.0
    assert out["lower"] == 300.0
    assert out["upper"] == 600.0          # P3 could be a listener
    assert out["unknown"] == 300.0        # P3 is unknown, never "not reached"
    assert out["not_reached"] == 400.0    # only P4 is certainly not reached
    assert out["unweighted"] == {"reached": 2, "unknown": 1, "not_reached": 1}
    # Partition of the weighted sample.
    assert out["lower"] + out["unknown"] + out["not_reached"] == 1000.0


def test_union_single_program_bounds():
    ev = ScanEvaluator(_bound_sample(), THRESHOLD)
    a = ev.union_bounds(["a"])
    assert (a["lower"], a["upper"], a["not_reached"]) == (300.0, 300.0, 700.0)
    b = ev.union_bounds(["b"])
    assert (b["lower"], b["upper"], b["not_reached"]) == (100.0, 400.0, 600.0)


def test_exclusive_and_crossover_exact():
    ev = ScanEvaluator(_bound_sample(), THRESHOLD)
    excl = ev.exclusive_listen(["a", "b"])
    # P2 certainly listens only to a; P1 listens to both, so a-exclusive is just P2.
    assert excl["a"] == {"lower": 200.0, "upper": 200.0}
    # b-exclusive: nobody (P1 also reached a).
    assert excl["b"] == {"lower": 0.0, "upper": 0.0}
    cross = ev.crossover(["a", "b"])
    assert cross["reached_2plus"]["lower"] == 100.0   # P1
    assert cross["reached_2plus"]["upper"] == 100.0   # P3 has only one candidate program
    assert cross["pairwise"] == [{"programs": ["a", "b"], "lower": 100.0, "upper": 100.0}]


def test_marginal_bounds_definite_vs_possible():
    ev = ScanEvaluator(_bound_sample(), THRESHOLD)
    # Adding b to {a}: P3 is not_reached(a) and unknown(b) -> possible, not definite.
    lower, upper = ev.marginal_bounds(["a"], "b")
    assert lower == 0.0
    assert upper == 300.0
    # Adding a to {b}: P2 is not_reached(b) and reached(a) -> definite new.
    lower, upper = ev.marginal_bounds(["b"], "a")
    assert lower == 200.0
    assert upper == 200.0


def test_intersections_third_order_with_missing():
    persons = [
        _person("Q1", 10.0, {"a": 9, "b": 9, "c": 9}, {"a": True, "b": True, "c": True}),
        _person("Q2", 20.0, {"a": 9, "b": 9}, {"a": True, "b": True, "c": True}),
        _person("Q3", 30.0, {"a": 9}, {"a": True, "b": False, "c": False}),
        _person("Q4", 40.0, {}, {"a": True, "b": True, "c": True}),
    ]
    ev = ScanEvaluator(persons, THRESHOLD)
    (three,) = ev.intersections([["a", "b", "c"]])
    assert three["lower"] == 10.0          # Q1 only
    assert three["upper"] == 40.0          # Q1 + Q3 (Q3 unknown in b and c)
    (two,) = ev.intersections([["b", "c"]])
    assert two["lower"] == 10.0
    assert two["upper"] == 40.0            # Q1 + Q3


# ---------------------------------------------------------------------------
# Unit: scan and bitmap engines must agree exactly
# ---------------------------------------------------------------------------
def _random_persons(n=60, programs=("a", "b", "c", "d"), seed=42):
    rng = random.Random(seed)
    persons = []
    for i in range(n):
        minutes, covered = {}, {}
        for p in programs:
            roll = rng.random()
            if roll < 0.35:
                minutes[p] = rng.choice([6.0, 12.0, 40.0, 90.0])
                covered[p] = True
            elif roll < 0.7:
                minutes[p] = 0.0
                covered[p] = True
            else:
                minutes[p] = 0.0
                covered[p] = False
        persons.append(PlanPerson(f"R{i:03d}", round(rng.uniform(0.5, 3.0), 6), minutes, covered))
    return persons


def test_scan_and_bitmap_engines_agree_exactly():
    programs = ["a", "b", "c", "d"]
    persons = _random_persons()
    scan = ScanEvaluator(persons, THRESHOLD)
    index = BitmapIndex.build(persons, programs, THRESHOLD)
    bitmap = BitmapEvaluator(index)
    for subset in (["a"], ["a", "b"], ["b", "c", "d"], ["a", "b", "c", "d"]):
        s = evaluate_set(
            scan, subset,
            marginal_candidates=[p for p in programs if p not in subset],
            intersection_sets=[programs[:3]],
        )
        b = evaluate_set(
            bitmap, subset,
            marginal_candidates=[p for p in programs if p not in subset],
            intersection_sets=[programs[:3]],
        )
        assert s == b, f"engines disagree on {subset}"


def test_bitmap_index_serialisation_roundtrip():
    programs = ["a", "b", "c"]
    persons = _random_persons(n=25, programs=programs)
    index = BitmapIndex.build(persons, programs, THRESHOLD)
    blob = index.serialise()
    restored = BitmapIndex(
        person_ids=index.person_ids,
        weights=index.weights,
        reached_bits={p: int(v["reached_bits"]) for p, v in blob.items()},
        unknown_bits={p: int(v["unknown_bits"]) for p, v in blob.items()},
    )
    assert BitmapEvaluator(restored).union_bounds(programs) == BitmapEvaluator(index).union_bounds(programs)
    assert index.memory_bytes() > 0


# ---------------------------------------------------------------------------
# Unit: constrained greedy search
# ---------------------------------------------------------------------------
def _greedy_sample():
    # x and y reach the same audience (overlap); z reaches a disjoint one.
    return [
        _person("G1", 100.0, {"x": 30, "y": 30}, {"x": True, "y": True, "z": True}),
        _person("G2", 200.0, {"x": 30, "y": 30}, {"x": True, "y": True, "z": True}),
        _person("G3", 300.0, {"z": 30}, {"x": True, "y": True, "z": True}),
        _person("G4", 400.0, {}, {"x": True, "y": True, "z": True}),
    ]


def test_greedy_budget_exclusive_and_reasons():
    persons = _greedy_sample()
    ev = ScanEvaluator(persons, THRESHOLD)
    out = greedy_optimize(
        ev,
        programs=["x", "y", "z"],
        costs={"x": 50.0, "y": 40.0, "z": 60.0},
        budget=100.0,
        exclusive_groups=[["x", "y"]],
    )
    # y is the cheaper way to buy the shared audience; x is then blocked by
    # the exclusive group, and z joins with its disjoint audience.
    assert out["selected"] == ["y", "z"]
    assert out["total_cost"] == 100.0
    assert out["union_reach"]["lower"] == 600.0
    assert out["stop_reason"] in {"no_candidates_remaining", "budget_exhausted"}
    # The exclusive exclusion is recorded inside the same marginal round.
    round2 = out["rounds"][1]
    x_record = next(c for c in round2["candidates"] if c["program"] == "x")
    assert x_record["eligible"] is False
    assert x_record["excluded_reason"] == "exclusive_conflict"
    assert x_record["conflicts_with_selected"] == ["y"]


def test_greedy_must_include_enters_first_recomputation():
    persons = _greedy_sample()
    ev = ScanEvaluator(persons, THRESHOLD)
    out = greedy_optimize(
        ev,
        programs=["x", "y", "z"],
        costs={"x": 50.0, "y": 40.0, "z": 60.0},
        budget=110.0,
        must_include=["x"],
        exclusive_groups=[["x", "y"]],
    )
    assert out["selected"][0] == "x"
    assert "y" not in out["selected"]
    assert out["selected"] == ["x", "z"]
    assert out["total_cost"] == 110.0


def test_greedy_budget_blocks_unaffordable_candidate():
    persons = _greedy_sample()
    ev = ScanEvaluator(persons, THRESHOLD)
    out = greedy_optimize(
        ev,
        programs=["x", "z"],
        costs={"x": 50.0, "z": 60.0},
        budget=80.0,
    )
    assert out["selected"] == ["x"]
    assert out["stop_reason"] == "budget_exhausted"
    round2 = out["rounds"][1] if len(out["rounds"]) > 1 else out["rounds"][0]
    z_record = next(
        c for r in out["rounds"] for c in r["candidates"]
        if c["program"] == "z" and c["excluded_reason"] == "cost_exceeds_remaining_budget"
    )
    assert z_record["eligible"] is False


def test_greedy_validation_errors():
    ev = ScanEvaluator(_greedy_sample(), THRESHOLD)
    with pytest.raises(ServiceError) as exc:
        greedy_optimize(ev, programs=["x", "z"], costs={"x": 10.0}, budget=100.0)
    assert exc.value.code == "plan_cost_missing"
    with pytest.raises(ServiceError) as exc:
        greedy_optimize(
            ev, programs=["x", "z"], costs={"x": 50.0, "z": 60.0}, budget=50.0,
            must_include=["x", "z"],
        )
    assert exc.value.code == "plan_budget_infeasible"
    with pytest.raises(ServiceError) as exc:
        greedy_optimize(
            ev, programs=["x", "y"], costs={"x": 10.0, "y": 10.0}, budget=100.0,
            must_include=["x", "y"], exclusive_groups=[["x", "y"]],
        )
    assert exc.value.code == "plan_must_include_conflict"


def test_greedy_engines_agree():
    programs = ["a", "b", "c", "d"]
    persons = _random_persons()
    costs = {"a": 10.0, "b": 20.0, "c": 15.0, "d": 25.0}
    scan = ScanEvaluator(persons, THRESHOLD)
    bitmap = BitmapEvaluator(BitmapIndex.build(persons, programs, THRESHOLD))
    s = greedy_optimize(scan, programs=programs, costs=costs, budget=50.0)
    b = greedy_optimize(bitmap, programs=programs, costs=costs, budget=50.0)
    assert s["selected"] == b["selected"]
    assert s["union_reach"] == b["union_reach"]


def test_person_breakdown_explains_individuals():
    rows = person_breakdown(_bound_sample(), ["a", "b"], THRESHOLD)
    by_id = {r["person_id"]: r for r in rows}
    assert by_id["P1"]["set_status"] == "reached"
    assert by_id["P3"]["set_status"] == "unknown"
    assert by_id["P3"]["programs"]["b"]["covered"] is False
    assert by_id["P4"]["set_status"] == "not_reached"


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------
def _plan_body(seeded, config=None, candidates=None):
    body = {
        "note": "pytest-plan",
        "inputs": {
            "recruitment_batch_seq": seeded["recruitment_batch_seq"],
            "target_batch_seq": seeded["target_batch_seq"],
            "program_batch_seq": seeded["plan_program_batch_seq"],
            "diary_seq_cutoff": seeded["latest_cutoff"],
        },
    }
    if config:
        body["config"] = config
    if candidates:
        body["candidate_programs"] = candidates
    return body


def _create_task(client, seeded, config=None):
    resp = client.post("/v1/plan/tasks", json=_plan_body(seeded, config))
    assert resp.status_code == 201, resp.text
    task = resp.json()["task"]
    assert task["status"] == "calibrated", task["calibration"]
    return task


def _evaluate(client, task_id, body):
    resp = client.post(f"/v1/plan/tasks/{task_id}/evaluations", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# API: task creation and acceptance scenarios
# ---------------------------------------------------------------------------
def test_plan_task_creation_locks_inputs(client, seeded):
    task = _create_task(client, seeded)
    assert task["weight_version"] == 1
    assert task["engine"] == "scan"  # 6 candidates <= default bitmap threshold
    assert sorted(task["candidate_programs"]) == sorted(
        [k for k, *_ in fixtures.PLAN_PROGRAM_SPECS]
    )
    summary = task["sample_summary"]
    assert summary["in_sample_persons"] > 0
    # Persons without any diary never enter the weighted sample.
    assert summary["excluded_by_code"]["missing_diary"] > 0
    assert summary["missing_diary_treated_as_zero_listening"] is False
    assert task["calibration"]["status"] == "converged"


def test_unknown_probability_frame_is_refused(client, seeded):
    body = _plan_body(seeded)
    body["inputs"]["recruitment_batch_seq"] = seeded["recruitment_unknown_probability_seq"]
    resp = client.post("/v1/plan/tasks", json=body)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "inclusion_probability_unknown"


def test_high_overlap_union_is_not_a_sum(client, seeded):
    """Acceptance: high overlap. morning_block's audience sits inside
    youth_drive's, so the union dedups to youth_drive alone while the naive
    sum of per-program counts doubles it."""
    task = _create_task(client, seeded)
    tid = task["task_id"]
    youth = _evaluate(client, tid, {"set": ["youth_drive"],
                                    "marginal_candidates": ["morning_block"]})
    both = _evaluate(client, tid, {"set": ["youth_drive", "morning_block"]})
    y = youth["union_reach"]["lower"]
    assert both["union_reach"]["lower"] == y
    assert both["union_reach"]["upper"] == youth["union_reach"]["upper"]
    # The naive per-program sum would claim roughly double the audience.
    morning = _evaluate(client, tid, {"set": ["morning_block"]})
    naive_sum = y + morning["union_reach"]["lower"]
    assert naive_sum > both["union_reach"]["lower"] * 1.5
    # Marginal contribution of morning_block over youth_drive: exactly zero
    # confirmed new listeners; the only residual uncertainty is the weight of
    # persons undecidable for both (missing data), never a guessed gain.
    marginal = youth["marginals"][0]
    assert marginal["program"] == "morning_block"
    assert marginal["lower"] == 0.0
    assert marginal["upper"] == youth["union_reach"]["unknown"]
    # morning_block owns no exclusive audience at all.
    assert both["exclusive_listen"]["morning_block"] == {"lower": 0.0, "upper": 0.0}
    # But the pairwise overlap equals morning_block's whole audience.
    pair = both["crossover"]["pairwise"][0]
    assert pair["lower"] == morning["union_reach"]["lower"]


def test_high_marginal_midnight_news(client, seeded):
    """Acceptance: high marginal. midnight_news reaches an audience disjoint
    from youth_drive's: up to its full audience is new for a youth plan, in
    contrast to the exactly-zero marginal of the overlapping morning_block."""
    task = _create_task(client, seeded)
    tid = task["task_id"]
    midnight = _evaluate(client, tid, {"set": ["midnight_news"]})
    combo = _evaluate(client, tid, {"set": ["youth_drive"], "marginal_candidates": ["midnight_news"]})
    marginal = combo["marginals"][0]
    own_reach = midnight["union_reach"]["lower"]
    assert own_reach > 0
    # Every midnight listener could be new for the youth plan...
    assert marginal["upper"] == own_reach
    # ...and the definite (lower-bound) gain is a large share of that audience.
    assert marginal["lower"] > 0
    assert marginal["lower"] > 0.5 * own_reach
    # Disjoint confirmed audiences: the union equals the sum of the singles.
    youth = _evaluate(client, tid, {"set": ["youth_drive"]})
    both = _evaluate(client, tid, {"set": ["youth_drive", "midnight_news"]})
    assert both["union_reach"]["lower"] == pytest.approx(
        youth["union_reach"]["lower"] + midnight["union_reach"]["lower"], abs=1e-6
    )


def test_missing_data_bounds_and_not_reached_rule(client, seeded):
    """Acceptance: missing-data bounds. Diarists with the day-2 daytime gap
    are undecidable for youth_drive: they lift the upper bound and are never
    placed in not_reached. midnight_news airtime is fully covered for them."""
    task = _create_task(client, seeded)
    tid = task["task_id"]
    youth = _evaluate(client, tid, {"set": ["youth_drive"], "include_persons": True})
    u = youth["union_reach"]
    assert u["unknown"] > 0
    assert u["upper"] == pytest.approx(u["lower"] + u["unknown"], abs=1e-6)
    assert u["lower"] < u["upper"]
    total = youth["sample"]["total_weight"]
    assert u["lower"] + u["unknown"] + u["not_reached"] == pytest.approx(total, abs=1e-3)
    # Unknown persons exist in the per-person breakdown and are never not_reached.
    unknown_persons = [p for p in youth["persons"] if p["set_status"] == "unknown"]
    assert unknown_persons, "expected undecidable persons for the daytime program"
    for p in unknown_persons:
        assert p["programs"]["youth_drive"]["covered"] is False
        assert p["set_status"] != "not_reached"
    # midnight_news: every relevant slot is covered for the whole sample.
    midnight = _evaluate(client, tid, {"set": ["midnight_news"]})
    m = midnight["union_reach"]
    assert m["unknown"] == 0.0
    assert m["lower"] == m["upper"]


def test_engines_agree_on_api(client, seeded):
    """Both computation paths answer the same ad-hoc set identically."""
    task = _create_task(client, seeded)
    tid = task["task_id"]
    body = {
        "set": ["youth_drive", "afternoon_talk", "midnight_news"],
        "marginal_candidates": ["drive_extended"],
        "intersections": [["youth_drive", "afternoon_talk", "midnight_news"]],
    }
    scan = _evaluate(client, tid, {**body, "engine": "scan"})
    bitmap = _evaluate(client, tid, {**body, "engine": "bitmap"})
    assert scan["engine_used"] == "scan"
    assert bitmap["engine_used"] == "bitmap"
    for key in ("union_reach", "exclusive_listen", "crossover", "marginals", "intersections"):
        assert scan[key] == bitmap[key], key


def test_explicit_high_order_intersection_is_exact(client, seeded):
    """No pre-materialised cross tables, no truncated orders: a 3-way
    intersection is computed on demand and matches the per-person breakdown."""
    task = _create_task(client, seeded)
    tid = task["task_id"]
    programs = ["youth_drive", "morning_block", "drive_extended"]
    out = _evaluate(client, tid, {"set": programs, "intersections": [programs],
                                  "include_persons": True})
    (inter,) = out["intersections"]
    assert inter["programs"] == programs
    # Recompute the lower bound by hand from the per-person rows (weights are
    # rounded to 6 decimals in the breakdown, hence the 1e-3 tolerance).
    manual = sum(
        p["weight"]
        for p in out["persons"]
        if all(p["programs"][k]["status"] == "reached" for k in programs)
    )
    assert inter["lower"] == pytest.approx(manual, abs=1e-3)
    # morning_block and drive_extended audiences sit inside youth_drive's, so
    # the 3-way confirmed intersection equals drive_extended's own reach.
    drive = _evaluate(client, tid, {"set": ["drive_extended"]})
    assert inter["lower"] <= drive["union_reach"]["lower"] + 1e-6


def test_evaluate_unknown_program_rejected(client, seeded):
    task = _create_task(client, seeded)
    resp = client.post(f"/v1/plan/tasks/{task['task_id']}/evaluations",
                       json={"set": ["no_such_program"]})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "plan_program_unknown"


def test_evaluations_are_stored_with_weight_version(client, seeded):
    task = _create_task(client, seeded)
    tid = task["task_id"]
    created = _evaluate(client, tid, {"set": ["youth_drive"]})
    listing = client.get(f"/v1/plan/tasks/{tid}/evaluations").json()
    assert any(e["eval_id"] == created["eval_id"] for e in listing)
    detail = client.get(f"/v1/plan/tasks/{tid}/evaluations/{created['eval_id']}").json()
    assert detail["weight_version"] == 1
    assert detail["union_reach"] == created["union_reach"]


# ---------------------------------------------------------------------------
# API: constrained optimisation
# ---------------------------------------------------------------------------
def _optimize(client, tid, **overrides):
    body = {
        "budget": fixtures.PLAN_BUDGET,
        "costs": fixtures.PLAN_COSTS,
        "exclusive_groups": fixtures.PLAN_EXCLUSIVE_GROUPS,
    }
    body.update(overrides)
    created = client.post(f"/v1/plan/tasks/{tid}/optimizations", json=body)
    assert created.status_code == 201, created.text
    job_id = created.json()["job"]["job_id"]
    executed = client.post(f"/v1/plan/tasks/{tid}/optimizations/{job_id}/execute")
    assert executed.status_code == 200, executed.text
    return executed.json()["job"]


def test_optimize_respects_all_constraints_in_one_search(client, seeded):
    """Acceptance: budget, must-include and mutually exclusive slots all enter
    the same marginal recomputation; every round records why."""
    task = _create_task(client, seeded)
    tid = task["task_id"]
    job = _optimize(client, tid)
    result = job["result"]
    assert result["engine_used"] == "scan"
    assert result["total_cost"] <= fixtures.PLAN_BUDGET
    selected = result["selected"]
    # Mutually exclusive slots are never bought together.
    assert not ({"youth_drive", "morning_block"} <= set(selected))
    assert not ({"afternoon_talk", "evening_talk"} <= set(selected))
    # The disjoint midnight audience is valuable enough to be bought.
    assert "midnight_news" in selected
    assert result["union_reach"]["lower"] > 0
    # Every round lists every candidate with its marginal or exclusion reason.
    reasons = {
        c["excluded_reason"]
        for r in result["rounds"] for c in r["candidates"] if not c["eligible"]
    }
    assert "exclusive_conflict" in reasons
    assert result["stop_reason"] in {
        "budget_exhausted", "no_candidates_remaining", "no_positive_marginal",
    }


def test_optimize_must_include_and_exclusive_conflict_visible(client, seeded):
    task = _create_task(client, seeded)
    tid = task["task_id"]
    job = _optimize(client, tid, must_include=["youth_drive"])
    result = job["result"]
    assert result["selected"][0] == "youth_drive"
    assert "morning_block" not in result["selected"]
    conflict_records = [
        c for r in result["rounds"] for c in r["candidates"]
        if c["program"] == "morning_block" and c["excluded_reason"] == "exclusive_conflict"
    ]
    assert conflict_records, "morning_block must be shown as blocked by the exclusive group"


def test_optimize_validation_errors(client, seeded):
    task = _create_task(client, seeded)
    tid = task["task_id"]
    base = {"budget": 100.0, "costs": fixtures.PLAN_COSTS,
            "exclusive_groups": fixtures.PLAN_EXCLUSIVE_GROUPS}
    resp = client.post(f"/v1/plan/tasks/{tid}/optimizations",
                       json={**base, "budget": 80.0,
                             "must_include": ["youth_drive", "midnight_news"]})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "plan_budget_infeasible"
    resp = client.post(f"/v1/plan/tasks/{tid}/optimizations",
                       json={**base, "must_include": ["youth_drive", "morning_block"]})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "plan_must_include_conflict"
    resp = client.post(f"/v1/plan/tasks/{tid}/optimizations",
                       json={"budget": 100.0, "costs": {"youth_drive": 45.0}})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "plan_cost_missing"


# ---------------------------------------------------------------------------
# API: rebind, stale jobs and bitmap index invalidation
# ---------------------------------------------------------------------------
def test_stale_job_cannot_write_back_after_rebind(client, seeded):
    """Acceptance: a survey-period switch or weight update invalidates queued
    work — the old job is refused and writes nothing back."""
    task = _create_task(client, seeded)
    tid = task["task_id"]
    body = {"budget": 100.0, "costs": fixtures.PLAN_COSTS,
            "exclusive_groups": fixtures.PLAN_EXCLUSIVE_GROUPS}
    job = client.post(f"/v1/plan/tasks/{tid}/optimizations", json=body).json()["job"]
    assert job["status"] == "queued"
    assert job["weight_version_snapshot"] == 1

    rebind = client.post(f"/v1/plan/tasks/{tid}/rebind",
                         json={"inputs": _plan_body(seeded)["inputs"]})
    assert rebind.status_code == 200, rebind.text
    payload = rebind.json()
    assert payload["weight_version"] == 2
    assert payload["stale_jobs_rejected"] == 1

    refused = client.post(f"/v1/plan/tasks/{tid}/optimizations/{job['job_id']}/execute")
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "plan_job_stale"
    # Nothing was written back.
    detail = client.get(f"/v1/plan/tasks/{tid}/optimizations/{job['job_id']}").json()["job"]
    assert detail["status"] == "stale_rejected"
    assert detail["result"] is None
    assert detail["stale_reason"]["snapshot_weight_version"] == 1
    assert detail["stale_reason"]["current_weight_version"] == 2


def test_weight_update_changes_results_only_after_rebind(client, seeded):
    """A late diary changes the weight version; evaluations before and after
    the rebind are stored under their own versions."""
    task = _create_task(client, seeded)
    tid = task["task_id"]
    before = _evaluate(client, tid, {"set": ["midnight_news"]})
    assert before["weight_version"] == 1
    # A late resubmission arrives for S402 (revision 2, replacing survey day 3
    # and adding midnight listening): the diary cutoff moves.
    late_diary = {
        "person_id": "S402",
        "household_id": "H04",
        "revision": 2,
        "submitted_at": "2026-09-05T10:00:00+08:00",
        "replacement_scope": {
            "type": "replace",
            "start": "2026-09-03T00:00:00+08:00",
            "end": "2026-09-04T00:00:00+08:00",
        },
        "coverage": [{"start": "2026-09-03T00:00:00+08:00", "end": "2026-09-04T00:00:00+08:00"}],
        "entries": [
            {"start": "2026-09-03T07:00:00+08:00", "end": "2026-09-03T08:30:00+08:00",
             "station": "RADIO-1", "source_serial": "S402-D3-002"},
            {"start": "2026-09-03T23:30:00+08:00", "end": "2026-09-04T00:10:00+08:00",
             "station": "RADIO-2", "source_serial": "S402-S3-002"},
        ],
    }
    resp = client.post("/v1/diary/submissions", json=late_diary)
    assert resp.status_code == 201, resp.text
    new_cutoff = client.get("/v1/diary/cutoff").json()["available_max_submission_seq"]
    inputs = {**_plan_body(seeded)["inputs"], "diary_seq_cutoff": new_cutoff}
    rebind = client.post(f"/v1/plan/tasks/{tid}/rebind", json={"inputs": inputs})
    assert rebind.status_code == 200, rebind.text
    after = _evaluate(client, tid, {"set": ["midnight_news"]})
    assert after["weight_version"] == 2
    # The weight update is real: S402's new midnight listening lifts the reach.
    assert after["union_reach"]["lower"] > before["union_reach"]["lower"]
    # The old evaluation stays stored under version 1.
    stored = client.get(f"/v1/plan/tasks/{tid}/evaluations/{before['eval_id']}").json()
    assert stored["weight_version"] == 1


def test_bitmap_index_built_and_invalidated_on_rebind(client, seeded):
    """Acceptance: the bitmap path pays for index builds, and a weight update
    invalidates them — both facts are inspectable."""
    task = _create_task(client, seeded, config={"engine": "bitmap"})
    tid = task["task_id"]
    assert task["engine"] == "bitmap"
    index = client.get(f"/v1/plan/tasks/{tid}/index").json()
    assert len(index["generations"]) == 1
    assert index["generations"][0]["status"] == "built"
    assert index["generations"][0]["program_count"] == len(fixtures.PLAN_PROGRAM_SPECS)
    assert index["generations"][0]["memory_bytes"] > 0
    assert len(index["current_program_bits"]) == len(fixtures.PLAN_PROGRAM_SPECS)

    # Bitmap engine answers the same ad-hoc set as the scan engine would.
    out = _evaluate(client, tid, {"set": ["youth_drive", "midnight_news"]})
    assert out["engine_used"] == "bitmap"

    rebind = client.post(f"/v1/plan/tasks/{tid}/rebind",
                         json={"inputs": _plan_body(seeded)["inputs"]})
    assert rebind.status_code == 200
    assert rebind.json()["stale_indexes_invalidated"] == 1
    index = client.get(f"/v1/plan/tasks/{tid}/index").json()
    statuses = {g["weight_version"]: g["status"] for g in index["generations"]}
    assert statuses == {1: "stale", 2: "built"}


def test_plan_task_persons_endpoint(client, seeded):
    task = _create_task(client, seeded)
    tid = task["task_id"]
    data = client.get(f"/v1/plan/tasks/{tid}/persons", params={"in_sample": True}).json()
    assert data["weight_version"] == 1
    persons = data["persons"]
    assert persons, "expected in-sample persons"
    assert all(p["final_weight"] is not None for p in persons)
    excluded = client.get(f"/v1/plan/tasks/{tid}/persons",
                          params={"in_sample": False}).json()["persons"]
    codes = {p["exclusion_code"] for p in excluded}
    assert "missing_diary" in codes
