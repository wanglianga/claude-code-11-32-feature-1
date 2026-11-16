"""Combination reach analytics for promotion-plan what-if trials.

A plan's value is *new* listeners, so an arbitrary program set is evaluated as
a union over persons — never as a sum of per-program weighted counts. Every
figure is computed on demand from per-person-per-program facts (confirmed
minutes + coverage flag); there are no pre-materialised cross tables and no
truncated high-order intersections.

Two exact evaluation paths share the same per-person facts:

* ``scan``   — iterate persons, test set membership, deduplicate weights.
               Direct and explainable down to the individual.
* ``bitmap`` — one compressed bitmap per program, indexed by person (a Python
               arbitrary-precision int; unset regions cost no objects). A
               trial set is a bitmap OR followed by a weighted reduction.
               Still exact: union/marginal are set algebra, not sampling.

Both paths must agree exactly on the same inputs; tests assert that.

Missing-data rule: a person whose *relevant airtime* for a program is not
fully covered by their diary is ``unknown`` for that program. Unknown persons
are never counted as "not reached". They enter the upper bound of union reach
and are reported as their own weighted bucket.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from ..errors import invalid

REACHED = "reached"
NOT_REACHED = "not_reached"
UNKNOWN = "unknown"

STATUSES = (REACHED, NOT_REACHED, UNKNOWN)


def program_status(minutes: float, covered: bool, threshold: float) -> str:
    """One person, one program.

    ``reached``    — confirmed listening minutes meet the threshold.
    ``not_reached``— below threshold AND the program's relevant airtime is
                     fully covered by the diary (so "did not listen" is known).
    ``unknown``    — below threshold AND relevant airtime is only partially
                     covered; the person may or may not have listened.
    """
    if minutes >= threshold:
        return REACHED
    return NOT_REACHED if covered else UNKNOWN


@dataclass
class PlanPerson:
    """One sample person's per-program facts for the current weight version."""

    person_id: str
    weight: float
    minutes: dict[str, float] = field(default_factory=dict)
    covered: dict[str, bool] = field(default_factory=dict)


def _r6(value: float) -> float:
    return round(float(value), 6)


# ---------------------------------------------------------------------------
# Scan path: iterate persons, deduplicate weights. Direct and explainable.
# ---------------------------------------------------------------------------
class ScanEvaluator:
    """Exact set metrics computed by scanning persons one by one."""

    def __init__(self, persons: list[PlanPerson], threshold: float):
        self.persons = persons
        self.threshold = threshold

    def _status(self, person: PlanPerson, program: str) -> str:
        return program_status(
            person.minutes.get(program, 0.0),
            person.covered.get(program, False),
            self.threshold,
        )

    def union_bounds(self, programs: list[str]) -> dict[str, Any]:
        reached_w = unknown_w = not_reached_w = 0.0
        n_reached = n_unknown = n_not = 0
        for person in self.persons:
            saw_unknown = False
            for p in programs:
                st = self._status(person, p)
                if st == REACHED:
                    reached_w += person.weight
                    n_reached += 1
                    break
                if st == UNKNOWN:
                    saw_unknown = True
            else:
                if saw_unknown:
                    unknown_w += person.weight
                    n_unknown += 1
                else:
                    not_reached_w += person.weight
                    n_not += 1
        return _union_payload(reached_w, unknown_w, not_reached_w, n_reached, n_unknown, n_not)

    def union_lower(self, programs: list[str]) -> float:
        total = 0.0
        for person in self.persons:
            for p in programs:
                if self._status(person, p) == REACHED:
                    total += person.weight
                    break
        return total

    def exclusive_listen(self, programs: list[str]) -> dict[str, dict[str, float]]:
        lower = {p: 0.0 for p in programs}
        upper = {p: 0.0 for p in programs}
        for person in self.persons:
            status = {p: self._status(person, p) for p in programs}
            for p in programs:
                if status[p] != REACHED:
                    continue
                others = [q for q in programs if q != p]
                if all(status[q] == NOT_REACHED for q in others):
                    lower[p] += person.weight
                if all(status[q] != REACHED for q in others):
                    upper[p] += person.weight
        return {p: {"lower": _r6(lower[p]), "upper": _r6(upper[p])} for p in programs}

    def crossover(self, programs: list[str], pairwise: bool = True) -> dict[str, Any]:
        lower_w = upper_w = 0.0
        lower_n = upper_n = 0
        pair_lower: dict[tuple[str, str], float] = {}
        pair_upper: dict[tuple[str, str], float] = {}
        pairs = [(programs[i], programs[j]) for i in range(len(programs)) for j in range(i + 1, len(programs))]
        for person in self.persons:
            status = {p: self._status(person, p) for p in programs}
            n_reached = sum(1 for s in status.values() if s == REACHED)
            n_unknown = sum(1 for s in status.values() if s == UNKNOWN)
            if n_reached >= 2:
                lower_w += person.weight
                lower_n += 1
            if n_reached + n_unknown >= 2:
                upper_w += person.weight
                upper_n += 1
            if pairwise:
                for a, b in pairs:
                    if status[a] == REACHED and status[b] == REACHED:
                        pair_lower[(a, b)] = pair_lower.get((a, b), 0.0) + person.weight
                    if status[a] != NOT_REACHED and status[b] != NOT_REACHED:
                        pair_upper[(a, b)] = pair_upper.get((a, b), 0.0) + person.weight
        out: dict[str, Any] = {
            "reached_2plus": {
                "lower": _r6(lower_w),
                "upper": _r6(upper_w),
                "unweighted_lower": lower_n,
                "unweighted_upper": upper_n,
            }
        }
        if pairwise:
            out["pairwise"] = [
                {
                    "programs": [a, b],
                    "lower": _r6(pair_lower.get((a, b), 0.0)),
                    "upper": _r6(pair_upper.get((a, b), 0.0)),
                }
                for a, b in pairs
            ]
        return out

    def intersections(self, sets: list[list[str]]) -> list[dict[str, Any]]:
        out = []
        for wanted in sets:
            lower_w = upper_w = 0.0
            for person in self.persons:
                statuses = [self._status(person, p) for p in wanted]
                if all(s == REACHED for s in statuses):
                    lower_w += person.weight
                if all(s != NOT_REACHED for s in statuses):
                    upper_w += person.weight
            out.append({"programs": list(wanted), "lower": _r6(lower_w), "upper": _r6(upper_w)})
        return out

    def marginal_bounds(self, base: list[str], program: str) -> tuple[float, float]:
        """(definite new, possible new) weighted persons from adding ``program``."""
        lower = upper = 0.0
        for person in self.persons:
            base_reached = False
            base_unknown = False
            for p in base:
                st = self._status(person, p)
                if st == REACHED:
                    base_reached = True
                    break
                if st == UNKNOWN:
                    base_unknown = True
            if base_reached:
                continue
            add_status = self._status(person, program)
            if not base_unknown and add_status == REACHED:
                lower += person.weight
            if add_status != NOT_REACHED:
                upper += person.weight
        return lower, upper

    def marginal(self, base: list[str], program: str) -> dict[str, Any]:
        lower, upper = self.marginal_bounds(base, program)
        return {
            "program": program,
            "lower": _r6(lower),
            "upper": _r6(upper),
            "union_with": self.union_bounds([*base, program]),
        }


def _union_payload(
    reached_w: float,
    unknown_w: float,
    not_reached_w: float,
    n_reached: int,
    n_unknown: int,
    n_not: int,
) -> dict[str, Any]:
    return {
        "lower": _r6(reached_w),
        "upper": _r6(reached_w + unknown_w),
        "confirmed": _r6(reached_w),
        "unknown": _r6(unknown_w),
        "not_reached": _r6(not_reached_w),
        "unweighted": {
            "reached": n_reached,
            "unknown": n_unknown,
            "not_reached": n_not,
        },
    }


# ---------------------------------------------------------------------------
# Bitmap path: one compressed bitmap per program, indexed by person.
# A Python arbitrary-precision int is a compact bit array: unset regions cost
# no objects, OR/AND are word-level, and results stay exact.
# ---------------------------------------------------------------------------
def bits_weight(bits: int, weights: list[float]) -> float:
    """Sum weights over set bit positions (positions follow person order)."""
    total = 0.0
    while bits:
        lsb = bits & -bits
        total += weights[lsb.bit_length() - 1]
        bits ^= lsb
    return total


class BitmapIndex:
    """Per-program reached/unknown bitmaps over a fixed person universe."""

    def __init__(
        self,
        person_ids: list[str],
        weights: list[float],
        reached_bits: dict[str, int],
        unknown_bits: dict[str, int],
    ):
        self.person_ids = person_ids
        self.weights = weights
        self.reached_bits = reached_bits
        self.unknown_bits = unknown_bits
        self.universe = (1 << len(person_ids)) - 1 if person_ids else 0
        self.total_weight = sum(weights)

    @classmethod
    def build(
        cls,
        persons: list[PlanPerson],
        programs: list[str],
        threshold: float,
    ) -> "BitmapIndex":
        person_ids = [p.person_id for p in persons]
        weights = [p.weight for p in persons]
        reached_bits: dict[str, int] = {}
        unknown_bits: dict[str, int] = {}
        for program in programs:
            r_bits = 0
            u_bits = 0
            for i, person in enumerate(persons):
                st = program_status(
                    person.minutes.get(program, 0.0),
                    person.covered.get(program, False),
                    threshold,
                )
                if st == REACHED:
                    r_bits |= 1 << i
                elif st == UNKNOWN:
                    u_bits |= 1 << i
            reached_bits[program] = r_bits
            unknown_bits[program] = u_bits
        return cls(person_ids, weights, reached_bits, unknown_bits)

    def memory_bytes(self) -> int:
        """Approximate heap cost of the stored bitmaps (the managed state)."""
        total = 0
        for bits in list(self.reached_bits.values()) + list(self.unknown_bits.values()):
            total += (bits.bit_length() + 7) // 8
        return total

    def serialise(self) -> dict[str, dict[str, str]]:
        return {
            program: {
                "reached_bits": str(self.reached_bits[program]),
                "unknown_bits": str(self.unknown_bits[program]),
            }
            for program in self.reached_bits
        }


class BitmapEvaluator:
    """Exact set metrics computed with bitmap set algebra."""

    def __init__(self, index: BitmapIndex):
        self.index = index

    def _r(self, program: str) -> int:
        return self.index.reached_bits.get(program, 0)

    def _u(self, program: str) -> int:
        return self.index.unknown_bits.get(program, 0)

    def _or(self, bits: Iterable[int]) -> int:
        out = 0
        for b in bits:
            out |= b
        return out

    def _w(self, bits: int) -> float:
        return bits_weight(bits, self.index.weights)

    def union_bits(self, programs: list[str]) -> tuple[int, int]:
        reached = self._or(self._r(p) for p in programs)
        unknown = self._or(self._u(p) for p in programs) & ~reached
        return reached, unknown

    def union_bounds(self, programs: list[str]) -> dict[str, Any]:
        reached, unknown = self.union_bits(programs)
        not_reached = self.index.universe & ~reached & ~unknown
        return _union_payload(
            self._w(reached),
            self._w(unknown),
            self._w(not_reached),
            reached.bit_count(),
            unknown.bit_count(),
            not_reached.bit_count(),
        )

    def union_lower(self, programs: list[str]) -> float:
        reached = self._or(self._r(p) for p in programs)
        return self._w(reached)

    def exclusive_listen(self, programs: list[str]) -> dict[str, dict[str, float]]:
        out = {}
        for p in programs:
            others = [q for q in programs if q != p]
            others_reached = self._or(self._r(q) for q in others)
            others_any = self._or(self._r(q) | self._u(q) for q in others)
            lower = self._r(p) & ~others_any
            upper = self._r(p) & ~others_reached
            out[p] = {"lower": _r6(self._w(lower)), "upper": _r6(self._w(upper))}
        return out

    def crossover(self, programs: list[str], pairwise: bool = True) -> dict[str, Any]:
        lower_bits = 0
        upper_bits = 0
        pairs = [(programs[i], programs[j]) for i in range(len(programs)) for j in range(i + 1, len(programs))]
        pair_rows = []
        for a, b in pairs:
            both_reached = self._r(a) & self._r(b)
            both_possible = (self._r(a) | self._u(a)) & (self._r(b) | self._u(b))
            lower_bits |= both_reached
            upper_bits |= both_possible
            if pairwise:
                pair_rows.append(
                    {
                        "programs": [a, b],
                        "lower": _r6(self._w(both_reached)),
                        "upper": _r6(self._w(both_possible)),
                    }
                )
        out: dict[str, Any] = {
            "reached_2plus": {
                "lower": _r6(self._w(lower_bits)),
                "upper": _r6(self._w(upper_bits)),
                "unweighted_lower": lower_bits.bit_count(),
                "unweighted_upper": upper_bits.bit_count(),
            }
        }
        if pairwise:
            out["pairwise"] = pair_rows
        return out

    def intersections(self, sets: list[list[str]]) -> list[dict[str, Any]]:
        out = []
        for wanted in sets:
            lower_bits = self.index.universe
            upper_bits = self.index.universe
            for p in wanted:
                lower_bits &= self._r(p)
                upper_bits &= self._r(p) | self._u(p)
            out.append(
                {
                    "programs": list(wanted),
                    "lower": _r6(self._w(lower_bits)),
                    "upper": _r6(self._w(upper_bits)),
                }
            )
        return out

    def marginal_bits(self, base: list[str], program: str) -> tuple[int, int]:
        """(definite new, possible new) bit sets from adding ``program``."""
        base_reached, base_unknown = self.union_bits(base)
        not_reached_base = self.index.universe & ~base_reached & ~base_unknown
        definite = not_reached_base & self._r(program)
        possible = (self.index.universe & ~base_reached) & (self._r(program) | self._u(program))
        return definite, possible

    def marginal_bounds(self, base: list[str], program: str) -> tuple[float, float]:
        definite, possible = self.marginal_bits(base, program)
        return self._w(definite), self._w(possible)

    def marginal(self, base: list[str], program: str) -> dict[str, Any]:
        lower, upper = self.marginal_bounds(base, program)
        return {
            "program": program,
            "lower": _r6(lower),
            "upper": _r6(upper),
            "union_with": self.union_bounds([*base, program]),
        }


# ---------------------------------------------------------------------------
# Greedy plan search: budget, must-include and mutually exclusive slots all
# enter the SAME marginal recomputation, round by round.
# ---------------------------------------------------------------------------
def _conflict_map(exclusive_groups: list[list[str]]) -> dict[str, set[str]]:
    conflicts: dict[str, set[str]] = {}
    for group in exclusive_groups:
        for a in group:
            for b in group:
                if a != b:
                    conflicts.setdefault(a, set()).add(b)
    return conflicts


def validate_optimize_request(
    programs: list[str],
    costs: dict[str, float],
    budget: float,
    must_include: list[str],
    exclusive_groups: list[list[str]],
) -> None:
    """Domain validation for one optimisation request (422 on any breach)."""
    unknown_programs = sorted(
        {p for p in must_include if p not in programs}
        | {p for group in exclusive_groups for p in group if p not in programs}
    )
    if unknown_programs:
        raise invalid(
            "plan_program_unknown",
            "must_include / exclusive_groups reference programs outside the task candidate pool",
            {"programs": unknown_programs, "candidate_pool": programs},
        )
    missing_costs = [p for p in programs if p not in costs]
    if missing_costs:
        raise invalid(
            "plan_cost_missing",
            "every candidate program needs a positive cost for budget-constrained search",
            {"programs": missing_costs},
        )
    nonpositive = sorted(p for p, c in costs.items() if not isinstance(c, (int, float)) or c <= 0)
    if nonpositive:
        raise invalid(
            "plan_cost_invalid",
            "costs must be positive numbers",
            {"programs": nonpositive},
        )
    conflicts = _conflict_map(exclusive_groups)
    must_pairs = [
        (a, b)
        for i, a in enumerate(must_include)
        for b in must_include[i + 1 :]
        if b in conflicts.get(a, set())
    ]
    if must_pairs:
        raise invalid(
            "plan_must_include_conflict",
            "must_include programs sit in the same mutually exclusive group and can never be bought together",
            {"conflicting_pairs": [[a, b] for a, b in must_pairs]},
        )
    must_cost = sum(costs[p] for p in must_include)
    if must_cost > budget + 1e-9:
        raise invalid(
            "plan_budget_infeasible",
            "must_include programs alone exceed the budget",
            {"must_include_cost": round(must_cost, 6), "budget": budget},
        )


def greedy_optimize(
    evaluator: ScanEvaluator | BitmapEvaluator,
    *,
    programs: list[str],
    costs: dict[str, float],
    budget: float,
    must_include: list[str] | None = None,
    exclusive_groups: list[list[str]] | None = None,
    score: str = "marginal_per_cost",
) -> dict[str, Any]:
    """Greedy marginal-reach search under budget / must-include / exclusivity.

    Every round recomputes the marginal confirmed (lower-bound) union reach of
    each remaining candidate against the current selection; candidates blocked
    by an exclusive group or by the remaining budget are recorded with their
    exclusion reason in the same round, so the choice stays auditable.
    """
    must_include = list(dict.fromkeys(must_include or []))
    exclusive_groups = exclusive_groups or []
    if score not in ("marginal_per_cost", "marginal"):
        raise invalid("plan_score_invalid", "score must be marginal_per_cost or marginal")
    validate_optimize_request(programs, costs, budget, must_include, exclusive_groups)

    conflicts = _conflict_map(exclusive_groups)
    selected: list[str] = list(must_include)
    blocked: set[str] = set()
    for p in selected:
        blocked |= conflicts.get(p, set())
    spent = sum(costs[p] for p in selected)

    rounds: list[dict[str, Any]] = []
    stop_reason = "no_candidates_remaining"
    while True:
        remaining = budget - spent
        round_candidates: list[dict[str, Any]] = []
        best: dict[str, Any] | None = None
        for p in sorted(programs):
            if p in selected:
                continue
            cost = float(costs[p])
            if p in blocked:
                clash_with = sorted(conflicts.get(p, set()) & set(selected))
                round_candidates.append(
                    {
                        "program": p,
                        "cost": _r6(cost),
                        "eligible": False,
                        "excluded_reason": "exclusive_conflict",
                        "conflicts_with_selected": clash_with,
                    }
                )
                continue
            if cost > remaining + 1e-9:
                round_candidates.append(
                    {
                        "program": p,
                        "cost": _r6(cost),
                        "eligible": False,
                        "excluded_reason": "cost_exceeds_remaining_budget",
                        "remaining_budget": _r6(remaining),
                    }
                )
                continue
            m_low, m_up = evaluator.marginal_bounds(selected, p)
            score_value = (m_low / cost) if score == "marginal_per_cost" else m_low
            # Raw floats drive ranking and the stop decision; rounded copies
            # are what the round log shows.
            record = {
                "program": p,
                "cost": _r6(cost),
                "eligible": True,
                "excluded_reason": None,
                "marginal_lower": _r6(m_low),
                "marginal_upper": _r6(m_up),
                "score": _r6(score_value),
                "_raw_score": score_value,
                "_raw_marginal_lower": m_low,
                "_raw_cost": cost,
            }
            round_candidates.append(record)
            if best is None or _better(record, best):
                best = record
        if best is None:
            budget_blocked = any(
                c["excluded_reason"] == "cost_exceeds_remaining_budget" for c in round_candidates
            )
            stop_reason = "budget_exhausted" if budget_blocked else "no_candidates_remaining"
            rounds.append(
                {
                    "round": len(rounds) + 1,
                    "candidates": [_public_candidate(c) for c in round_candidates],
                    "selected": None,
                    "reason": (
                        "no remaining candidate fits the budget"
                        if budget_blocked
                        else "no selectable candidate remains"
                    ),
                }
            )
            break
        if best["_raw_marginal_lower"] <= 0:
            stop_reason = "no_positive_marginal"
            rounds.append(
                {
                    "round": len(rounds) + 1,
                    "candidates": [_public_candidate(c) for c in round_candidates],
                    "selected": None,
                    "reason": "no candidate adds confirmed (lower-bound) reach; search stops",
                }
            )
            break
        selected.append(best["program"])
        spent += float(costs[best["program"]])
        blocked |= conflicts.get(best["program"], set())
        rounds.append(
            {
                "round": len(rounds) + 1,
                "candidates": [_public_candidate(c) for c in round_candidates],
                "selected": best["program"],
                "reason": (
                    f"highest {'marginal reach per unit cost' if score == 'marginal_per_cost' else 'marginal reach'} "
                    f"among eligible candidates ({best['score']})"
                ),
                "budget_remaining_after": _r6(budget - spent),
            }
        )

    final = evaluator.union_bounds(selected) if selected else _union_payload(0.0, 0.0, 0.0, 0, 0, 0)
    return {
        "selected": selected,
        "must_include": must_include,
        "added_by_search": [p for p in selected if p not in must_include],
        "budget": _r6(budget),
        "total_cost": _r6(spent),
        "remaining_budget": _r6(budget - spent),
        "union_reach": final,
        "rounds": rounds,
        "stop_reason": stop_reason,
        "score_rule": score,
        "objective": (
            "maximise confirmed (lower-bound) union reach; missing-data uncertainty is reported "
            "as the [lower, upper] interval and never used to rank candidates"
        ),
        "constraints": {
            "budget": _r6(budget),
            "must_include": must_include,
            "exclusive_groups": exclusive_groups,
        },
    }


def _better(candidate: dict[str, Any], incumbent: dict[str, Any]) -> bool:
    """Deterministic ranking: score, then absolute marginal, then cheaper, then key."""
    if candidate["_raw_score"] != incumbent["_raw_score"]:
        return candidate["_raw_score"] > incumbent["_raw_score"]
    if candidate["_raw_marginal_lower"] != incumbent["_raw_marginal_lower"]:
        return candidate["_raw_marginal_lower"] > incumbent["_raw_marginal_lower"]
    if candidate["_raw_cost"] != incumbent["_raw_cost"]:
        return candidate["_raw_cost"] < incumbent["_raw_cost"]
    return candidate["program"] < incumbent["program"]


def _public_candidate(record: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in record.items() if not k.startswith("_")}


# ---------------------------------------------------------------------------
# Evaluation assembly (shared by both engines)
# ---------------------------------------------------------------------------
PAIRWISE_MAX_PROGRAMS = 15


def evaluate_set(
    evaluator: ScanEvaluator | BitmapEvaluator,
    programs: list[str],
    *,
    marginal_candidates: list[str] | None = None,
    intersection_sets: list[list[str]] | None = None,
    include_pairwise: bool = True,
) -> dict[str, Any]:
    """Full set metrics: union, exclusive listen, crossover, marginals."""
    pairwise = include_pairwise and len(programs) <= PAIRWISE_MAX_PROGRAMS
    result: dict[str, Any] = {
        "union_reach": evaluator.union_bounds(programs),
        "exclusive_listen": evaluator.exclusive_listen(programs),
        "crossover": evaluator.crossover(programs, pairwise=pairwise),
    }
    if include_pairwise and not pairwise:
        result["crossover"]["pairwise_omitted"] = (
            f"set has {len(programs)} programs; pairwise detail is emitted for sets of "
            f"up to {PAIRWISE_MAX_PROGRAMS}. Use explicit `intersections` for exact "
            "higher-order queries — they are computed, never truncated."
        )
    if marginal_candidates:
        result["marginals"] = [evaluator.marginal(programs, p) for p in marginal_candidates]
    if intersection_sets:
        result["intersections"] = evaluator.intersections(intersection_sets)
    return result


def person_breakdown(persons: list[PlanPerson], programs: list[str], threshold: float) -> list[dict[str, Any]]:
    """Per-person explanation rows: set status plus per-program facts."""
    rows = []
    for person in persons:
        per_program = {}
        set_status = NOT_REACHED
        saw_unknown = False
        for p in programs:
            st = program_status(
                person.minutes.get(p, 0.0), person.covered.get(p, False), threshold
            )
            per_program[p] = {
                "status": st,
                "confirmed_minutes": _r6(person.minutes.get(p, 0.0)),
                "covered": person.covered.get(p, False),
            }
            if st == REACHED:
                set_status = REACHED
            elif st == UNKNOWN:
                saw_unknown = True
        if set_status != REACHED and saw_unknown:
            set_status = UNKNOWN
        rows.append(
            {
                "person_id": person.person_id,
                "weight": _r6(person.weight),
                "set_status": set_status,
                "programs": per_program,
            }
        )
    return rows
