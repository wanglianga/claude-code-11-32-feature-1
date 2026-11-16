"""Promotion-plan combination reach tasks.

A plan task pins one survey period and one weight version (its own bounded
raking over the plan sample), then answers arbitrary program-set questions —
union reach, exclusive listen, crossover, missing-data bounds, marginals and
budget-constrained greedy search — on top of that frozen base.

Unlike a run, the plan sample does **not** drop persons whose diary is only
partially filled. A person enters the weighted sample when at least one
candidate program's relevant airtime is fully covered (or confirmed listened);
per program, undecidable persons are ``unknown`` — they feed the upper bound
and are never counted as not-reached.

Concurrency rule: ``weight_version`` increments on every rebind (survey-period
switch or weight update). Optimisation jobs snapshot the version at creation;
at execution time the version is re-checked under a row lock and a stale job
is refused — it never writes results back against a newer base.
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .analysis import analyze_person, build_frame, exposure_intervals, survey_days
from .analytics.raking import RakingConfig, rake
from .analytics.reach import (
    BitmapEvaluator,
    BitmapIndex,
    PlanPerson,
    ScanEvaluator,
    evaluate_set,
    greedy_optimize,
    person_breakdown,
    program_status,
)
from .config import Settings
from .db import Database
from .errors import conflict, invalid, not_found
from .ingest import load_programs, load_recruitment, load_submissions, load_targets
from .jsonutil import canonical_hash, jb, loads
from .timeutil import clip, iso, length_minutes, local_instant, merge, subtract

DEFAULT_DIARY_WINDOW = {"start_local": "00:00", "end_local": "24:00"}


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------
def serialize_task(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["task_id"] = str(out["task_id"])
    for key in ("inputs", "candidate_programs", "survey_period", "config", "calibration", "sample_summary"):
        if key in out:
            out[key] = loads(out[key])
    for key in ("created_at", "updated_at"):
        if out.get(key) is not None:
            out[key] = iso(out[key])
    return out


def serialize_job(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["job_id"] = str(out["job_id"])
    out["task_id"] = str(out["task_id"])
    for key in ("request", "result", "stale_reason"):
        if key in out:
            out[key] = loads(out[key])
    for key in ("created_at", "finished_at"):
        if out.get(key) is not None:
            out[key] = iso(out[key])
    return out


def fetch_task(cur, task_id) -> dict[str, Any]:
    cur.execute("SELECT * FROM plan_tasks WHERE task_id = %s", (str(task_id),))
    row = cur.fetchone()
    if row is None:
        raise not_found("plan_task_not_found", "plan task does not exist")
    return serialize_task(row)


def _fetch_task_for_update(cur, task_id) -> dict[str, Any]:
    cur.execute("SELECT * FROM plan_tasks WHERE task_id = %s FOR UPDATE", (str(task_id),))
    row = cur.fetchone()
    if row is None:
        raise not_found("plan_task_not_found", "plan task does not exist")
    return serialize_task(row)


def _audit(cur, actor: str, action: str, entity_id: str, details: dict[str, Any]) -> None:
    cur.execute(
        "INSERT INTO audit_events (actor, action, entity, entity_id, details) VALUES (%s,%s,'plan_task',%s,%s)",
        (actor, action, entity_id, jb(details)),
    )


# ---------------------------------------------------------------------------
# Task construction (create + rebind share this pipeline)
# ---------------------------------------------------------------------------
def _tz(config: dict[str, Any], programs: dict[str, Any]) -> ZoneInfo:
    tz_name = programs["timezone"]
    if config.get("timezone") and config["timezone"] != tz_name:
        raise invalid(
            "timezone_mismatch",
            "plan task timezone must match the program batch timezone",
            {"task_timezone": config["timezone"], "program_timezone": tz_name},
        )
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as exc:
        raise invalid("timezone_unknown", "unknown timezone", {"timezone": tz_name}) from exc


def _validate_inputs(cur, inputs: dict[str, Any], config: dict[str, Any]):
    """Load the pinned batches and apply the same input gates a run applies."""
    recruitment = load_recruitment(cur, inputs["recruitment_batch_seq"])
    targets = load_targets(cur, inputs["target_batch_seq"])
    programs = load_programs(cur, inputs["program_batch_seq"])
    tz = _tz(config, programs)

    period_start = recruitment["survey_period_start"]
    period_end = recruitment["survey_period_end"]
    expected = survey_days(period_start, period_end)
    outside = sorted(
        {a["air_date"] for a in programs["airings"]}
        - {a["air_date"] for a in programs["airings"] if period_start <= a["air_date"] <= period_end}
    )
    if outside:
        raise invalid(
            "program_outside_survey_period",
            "program airings fall outside the recruitment survey period",
            {"air_dates": [d.isoformat() for d in outside]},
        )
    unknown_probability = [
        r["person_id"] for r in recruitment["rows"] if r["inclusion_probability"] is None
    ]
    if unknown_probability:
        raise invalid(
            "inclusion_probability_unknown",
            "unknown inclusion probability blocks weighted plan estimates; "
            "supply a frame batch with known sampling probabilities",
            {"persons": unknown_probability, "batch_seq": inputs["recruitment_batch_seq"]},
        )
    seq_cutoff = int(inputs["diary_seq_cutoff"])
    cur.execute("SELECT COALESCE(MAX(submission_seq), 0) AS m FROM diary_submissions")
    max_seq = cur.fetchone()["m"]
    if seq_cutoff > max_seq:
        raise invalid(
            "diary_cutoff_beyond_available",
            "diary_seq_cutoff exceeds available submissions",
            {"cutoff": seq_cutoff, "max_available": max_seq},
        )
    margins = loads(targets["dimensions"])
    for dim in config["raking"]["dimensions"]:
        if dim not in margins:
            raise invalid("target_dimension_missing", f"target batch has no margin for {dim}")
    return recruitment, targets, programs, tz, expected, margins


def _program_facts(
    analysis: dict[str, Any],
    program_map: dict[str, dict[str, Any]],
    survey_bounds: tuple[Any, Any],
    threshold: float,
) -> dict[str, dict[str, Any]]:
    """Confirmed minutes and coverage flag for one person over every candidate.

    Relevant airtime is clipped to the survey period; a program is ``covered``
    only when every relevant airing minute sits inside the declared diary
    coverage and outside any unresolved-conflict (unknown) range.
    """
    resolved = analysis["resolved"]
    station_intervals = resolved["station_intervals"]
    unknown_ranges = resolved["unknown_ranges"]
    confirmed_nonlisten = resolved["confirmed_nonlisten_ranges"]
    coverage = analysis["reconstruction"]["coverage"]
    facts: dict[str, dict[str, Any]] = {}
    for key, program in program_map.items():
        confirmed: list[tuple[Any, Any]] = []
        missing: list[tuple[Any, Any]] = []
        for airing in program["airings"]:
            slot = clip(airing["start_utc"], airing["end_utc"], survey_bounds[0], survey_bounds[1])
            if slot is None:
                continue
            confirmed.extend(
                exposure_intervals(
                    station_intervals, unknown_ranges, confirmed_nonlisten, airing["station"], slot
                )
            )
            missing.extend(subtract([slot], coverage))
            missing.extend(
                piece
                for piece in (clip(u[0], u[1], slot[0], slot[1]) for u in unknown_ranges)
                if piece is not None
            )
        minutes = length_minutes(merge(confirmed))
        covered = not merge(missing)
        facts[key] = {
            "minutes": round(minutes, 3),
            "covered": covered,
            "status": program_status(minutes, covered, threshold),
        }
    return facts


def _build_state(
    cur,
    config: dict[str, Any],
    inputs: dict[str, Any],
    candidate_programs: list[str],
) -> dict[str, Any]:
    """Build the plan sample, per-program facts and the calibrated weights."""
    recruitment, targets, programs, tz, expected, margins = _validate_inputs(cur, inputs, config)
    threshold = float(config["reach_minutes"])
    submissions = load_submissions(cur, int(inputs["diary_seq_cutoff"]))

    program_map: dict[str, dict[str, Any]] = {}
    for a in programs["airings"]:
        program_map.setdefault(a["program_key"], {"title": a["title"], "airings": []})["airings"].append(a)
    unknown_keys = sorted(set(candidate_programs) - set(program_map))
    if unknown_keys:
        raise invalid(
            "plan_program_unknown",
            "candidate programs are not present in the pinned program batch",
            {"programs": unknown_keys, "batch_seq": inputs["program_batch_seq"]},
        )
    candidate_map = {k: program_map[k] for k in candidate_programs}

    period_start = recruitment["survey_period_start"]
    period_end = recruitment["survey_period_end"]
    survey_bounds = (
        local_instant(period_start, 0, tz),
        local_instant(period_end + timedelta(days=1), 0, tz),
    )

    analysis_config = {
        **config,
        "diary_window": DEFAULT_DIARY_WINDOW,
        "completeness": {"type": "full_coverage", "max_uncovered_minutes": 0.0, "min_days": 0},
    }
    frame = build_frame(recruitment, submissions, analysis_config)
    from .runs import _load_resolutions

    resolutions = _load_resolutions(cur, [p.person_id for p in frame["persons"]])

    persons_state: list[dict[str, Any]] = []
    excluded: dict[str, int] = {}
    for fp in frame["persons"]:
        entry = {
            "person_id": fp.person_id,
            "household_id": fp.household_id,
            "strata_effective": fp.strata_effective,
            "age_band": fp.age_band,
            "region": fp.region,
            "sex": fp.sex,
            "base_weight": fp.base_weight,
            "in_sample": False,
            "exclusion_code": fp.exclusion_code,
            "exclusion_detail": fp.exclusion_detail,
            "facts": {},
        }
        if fp.exclusion_code is None:
            analysis = analyze_person(
                fp, submissions.get(fp.person_id, []), expected, analysis_config, resolutions, tz
            )
            facts = _program_facts(analysis, candidate_map, survey_bounds, threshold)
            entry["facts"] = facts
            # Inclusion rule: at least one candidate program is decidable for
            # this person. Fully undecidable persons carry no weight and appear
            # in no bound; they are listed for transparency only.
            if any(f["status"] != "unknown" for f in facts.values()):
                entry["in_sample"] = True
            else:
                entry["exclusion_code"] = "no_decidable_program"
                entry["exclusion_detail"] = {
                    "reason": (
                        "every candidate program's relevant airtime is only partially covered; "
                        "the person is never counted as not-reached and carries no weight"
                    )
                }
        if entry["exclusion_code"]:
            excluded[entry["exclusion_code"]] = excluded.get(entry["exclusion_code"], 0) + 1
        persons_state.append(entry)

    in_sample = [p for p in persons_state if p["in_sample"]]
    rc = RakingConfig(
        tolerance=float(config["raking"]["tolerance"]),
        max_iterations=int(config["raking"]["max_iterations"]),
        lower_factor=float(config["raking"]["lower_factor"]),
        upper_factor=float(config["raking"]["upper_factor"]),
        dimensions=tuple(config["raking"]["dimensions"]),
    )
    calibration: dict[str, Any]
    weights: dict[str, float] = {}
    if not in_sample:
        calibration = {
            "status": "sample_empty",
            "success": False,
            "reason": "no person has a decidable candidate program; weights are not computed",
        }
    else:
        result = rake(
            [
                {
                    "age_band": p["age_band"],
                    "region": p["region"],
                    "sex": p["sex"],
                    "strata_effective": p["strata_effective"],
                }
                for p in in_sample
            ],
            [float(p["base_weight"]) for p in in_sample],
            margins,
            rc,
        )
        calibration = result.summary()
        calibration.update(
            {
                "tolerance": rc.tolerance,
                "max_iterations": rc.max_iterations,
                "lower_factor": rc.lower_factor,
                "upper_factor": rc.upper_factor,
                "bound_scale": "per-person, relative to that person's design weight (1/p)",
                "publishable": bool(result.success),
            }
        )
        if result.success:
            for p, w in zip(in_sample, result.weights):
                weights[p["person_id"]] = w

    sample_summary = {
        "frame_persons": len(persons_state),
        "in_sample_persons": len(in_sample),
        "excluded_by_code": excluded,
        "candidate_programs": candidate_programs,
        "survey_period": {
            "start": period_start.isoformat(),
            "end": period_end.isoformat(),
            "days": len(expected),
        },
        "inclusion_rule": (
            "a person enters the weighted plan sample when at least one candidate program is "
            "decidable (relevant airtime fully covered or confirmed listened); per program, "
            "partially covered persons are unknown and never counted as not-reached"
        ),
        "missing_diary_treated_as_zero_listening": False,
    }
    return {
        "persons": persons_state,
        "weights": weights,
        "calibration": calibration,
        "sample_summary": sample_summary,
        "survey_period": sample_summary["survey_period"],
        "threshold": threshold,
    }


def _resolve_engine(config: dict[str, Any], candidate_count: int) -> str:
    engine = config.get("engine", "auto")
    if engine in ("scan", "bitmap"):
        return engine
    threshold = int(config.get("bitmap_program_threshold", 12))
    return "bitmap" if candidate_count > threshold else "scan"


def _persist_state(cur, task_id: str, weight_version: int, state: dict[str, Any]) -> None:
    for p in state["persons"]:
        cur.execute(
            """
            INSERT INTO plan_task_persons
                (task_id, weight_version, person_id, household_id, strata_effective,
                 age_band, region, sex, base_weight, final_weight, in_sample,
                 exclusion_code, exclusion_detail)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                task_id,
                weight_version,
                p["person_id"],
                p["household_id"],
                p["strata_effective"],
                p["age_band"],
                p["region"],
                p["sex"],
                p["base_weight"],
                state["weights"].get(p["person_id"]),
                p["in_sample"],
                p["exclusion_code"],
                jb(p["exclusion_detail"] or {}),
            ),
        )
        for key, fact in p["facts"].items():
            cur.execute(
                """
                INSERT INTO plan_task_person_program
                    (task_id, weight_version, person_id, program_key,
                     confirmed_minutes, covered, status)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    task_id,
                    weight_version,
                    p["person_id"],
                    key,
                    fact["minutes"],
                    fact["covered"],
                    fact["status"],
                ),
            )


def _persist_index(cur, task_id: str, weight_version: int, index: BitmapIndex) -> None:
    cur.execute(
        """
        INSERT INTO plan_task_indexes
            (task_id, weight_version, person_ids, person_count, program_count, memory_bytes, status)
        VALUES (%s,%s,%s,%s,%s,%s,'built')
        """,
        (
            task_id,
            weight_version,
            jb(index.person_ids),
            len(index.person_ids),
            len(index.reached_bits),
            index.memory_bytes(),
        ),
    )
    for program in sorted(index.reached_bits):
        cur.execute(
            """
            INSERT INTO plan_task_program_bits
                (task_id, weight_version, program_key, reached_bits, unknown_bits,
                 reached_persons, unknown_persons)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                task_id,
                weight_version,
                program,
                str(index.reached_bits[program]),
                str(index.unknown_bits[program]),
                index.reached_bits[program].bit_count(),
                index.unknown_bits[program].bit_count(),
            ),
        )


def _stale_indexes(cur, task_id: str) -> int:
    cur.execute(
        "UPDATE plan_task_indexes SET status='stale' WHERE task_id=%s AND status='built'",
        (task_id,),
    )
    return cur.rowcount


def _load_plan_persons(cur, task_id: str, weight_version: int) -> list[PlanPerson]:
    cur.execute(
        """
        SELECT person_id, final_weight FROM plan_task_persons
         WHERE task_id=%s AND weight_version=%s AND in_sample
         ORDER BY person_id
        """,
        (task_id, weight_version),
    )
    rows = cur.fetchall()
    persons = [
        PlanPerson(person_id=r["person_id"], weight=float(r["final_weight"]))
        for r in rows
    ]
    cur.execute(
        """
        SELECT person_id, program_key, confirmed_minutes, covered
          FROM plan_task_person_program
         WHERE task_id=%s AND weight_version=%s
        """,
        (task_id, weight_version),
    )
    by_person = {p.person_id: p for p in persons}
    for r in cur.fetchall():
        person = by_person.get(r["person_id"])
        if person is None:
            continue
        person.minutes[r["program_key"]] = float(r["confirmed_minutes"])
        person.covered[r["program_key"]] = bool(r["covered"])
    return persons


def _load_or_build_index(
    cur,
    task: dict[str, Any],
    persons: list[PlanPerson],
    threshold: float,
) -> BitmapIndex:
    """Load the persisted bitmap index for the task's current weight version.

    Missing or partial indexes are (re)built on demand and persisted, so a
    bitmap evaluation never silently falls back to the scan path.
    """
    task_id = task["task_id"]
    version = task["weight_version"]
    candidates = task["candidate_programs"]
    cur.execute(
        "SELECT person_ids, person_count FROM plan_task_indexes WHERE task_id=%s AND weight_version=%s AND status='built'",
        (task_id, version),
    )
    head = cur.fetchone()
    person_ids = [p.person_id for p in persons]
    if head is not None and loads(head["person_ids"]) == person_ids:
        cur.execute(
            "SELECT program_key, reached_bits, unknown_bits FROM plan_task_program_bits WHERE task_id=%s AND weight_version=%s",
            (task_id, version),
        )
        bits = {r["program_key"]: r for r in cur.fetchall()}
        if all(p in bits for p in candidates):
            return BitmapIndex(
                person_ids=person_ids,
                weights=[p.weight for p in persons],
                reached_bits={p: int(bits[p]["reached_bits"]) for p in candidates},
                unknown_bits={p: int(bits[p]["unknown_bits"]) for p in candidates},
            )
    index = BitmapIndex.build(persons, candidates, threshold)
    cur.execute(
        "DELETE FROM plan_task_program_bits WHERE task_id=%s AND weight_version=%s",
        (task_id, version),
    )
    cur.execute(
        "DELETE FROM plan_task_indexes WHERE task_id=%s AND weight_version=%s",
        (task_id, version),
    )
    _persist_index(cur, task_id, version, index)
    return index


# ---------------------------------------------------------------------------
# Create / rebind
# ---------------------------------------------------------------------------
def _task_config(body_config: Any) -> dict[str, Any]:
    from .models import PlanConfigIn

    return PlanConfigIn.model_validate(body_config or {}).model_dump(mode="json")


def _input_hash(inputs, candidate_programs, config, recruitment, targets, programs, cur, seq_cutoff):
    from .runs import _diary_hash_rows

    margins = loads(targets["dimensions"])
    return canonical_hash(
        {
            "inputs": inputs,
            "candidate_programs": candidate_programs,
            "config": config,
            "recruitment": [
                {
                    "person_id": r["person_id"],
                    "household_id": r["household_id"],
                    "strata": r["strata"],
                    "age_band": r["age_band"],
                    "region": r["region"],
                    "sex": r["sex"],
                    "p": None if r["inclusion_probability"] is None else float(r["inclusion_probability"]),
                }
                for r in recruitment["rows"]
            ],
            "targets": {
                "seq": inputs["target_batch_seq"],
                "margins": {dim: {cat: float(v) for cat, v in cats.items()} for dim, cats in margins.items()},
            },
            "programs": [
                {
                    "key": a["program_key"],
                    "station": a["station"],
                    "air_date": a["air_date"].isoformat(),
                    "start": a["start_local"],
                    "end": a["end_local"],
                }
                for a in programs["airings"]
            ],
            "diary_rows": _diary_hash_rows(cur, seq_cutoff),
        }
    )


def create_task(db: Database, settings: Settings, body: dict[str, Any], actor: str) -> dict[str, Any]:
    inputs = body["inputs"]
    config = _task_config(body.get("config"))
    with db.scoped() as conn:
        cur = conn.cursor()
        recruitment, targets, programs, tz, expected, _margins = _validate_inputs(cur, inputs, config)
        all_keys = sorted({a["program_key"] for a in programs["airings"]})
        candidate_programs = body.get("candidate_programs")
        if candidate_programs is None:
            candidate_programs = all_keys
        candidate_programs = sorted(dict.fromkeys(candidate_programs))
        if not candidate_programs:
            raise invalid("plan_candidates_empty", "candidate program pool must not be empty")

        state = _build_state(cur, {**config, "timezone": programs["timezone"]}, inputs, candidate_programs)
        config = {**config, "timezone": programs["timezone"]}
        engine = _resolve_engine(config, len(candidate_programs))
        input_hash = _input_hash(
            inputs, candidate_programs, config, recruitment, targets, programs, cur, int(inputs["diary_seq_cutoff"])
        )
        task_id = str(uuid.uuid4())
        status = "calibrated" if state["calibration"].get("success") else "rejected"
        cur.execute(
            """
            INSERT INTO plan_tasks
                (task_id, status, engine, weight_version, inputs, candidate_programs,
                 survey_period, config, input_hash, calibration, sample_summary, note, created_by)
            VALUES (%s,%s,%s,1,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                task_id,
                status,
                engine,
                jb(inputs),
                jb(candidate_programs),
                jb(state["survey_period"]),
                jb(config),
                input_hash,
                jb(state["calibration"]),
                jb(state["sample_summary"]),
                body.get("note"),
                actor,
            ),
        )
        _persist_state(cur, task_id, 1, state)
        index_built = False
        if status == "calibrated" and engine == "bitmap":
            persons = _load_plan_persons(cur, task_id, 1)
            index = BitmapIndex.build(persons, candidate_programs, state["threshold"])
            _persist_index(cur, task_id, 1, index)
            index_built = True
        _audit(
            cur,
            actor,
            "plan_task.create",
            task_id,
            {
                "inputs": inputs,
                "candidate_programs": candidate_programs,
                "engine": engine,
                "input_hash": input_hash,
                "status": status,
            },
        )
        conn.commit()
        task = fetch_task(cur, task_id)
    return {
        "task": task,
        "engine_resolved": engine,
        "bitmap_index_built": index_built,
        "limitations": {
            "union_is_never_a_sum_of_program_counts": True,
            "partially_covered_persons_are_unknown_not_not_reached": True,
            "high_order_intersections": "computed exactly on demand; never pre-materialised, never truncated",
        },
    }


def rebind_task(db: Database, task_id: str, body: dict[str, Any], actor: str) -> dict[str, Any]:
    """Switch survey period and/or refresh weights: rebuild under a new version.

    The previous weight version's rows stay in the tables for history; bitmap
    indexes of the old version are marked stale, and queued jobs snapshotting
    the old version are marked stale_rejected so they can never write back.
    """
    with db.scoped() as conn:
        cur = conn.cursor()
        current = _fetch_task_for_update(cur, task_id)
        inputs = body["inputs"]
        config = _task_config(body.get("config")) if body.get("config") is not None else current["config"]
        recruitment, targets, programs, tz, expected, _margins = _validate_inputs(cur, inputs, config)
        all_keys = sorted({a["program_key"] for a in programs["airings"]})
        candidate_programs = body.get("candidate_programs")
        if candidate_programs is None:
            candidate_programs = current["candidate_programs"]
        candidate_programs = sorted(dict.fromkeys(candidate_programs))
        if not candidate_programs:
            raise invalid("plan_candidates_empty", "candidate program pool must not be empty")

        state = _build_state(cur, {**config, "timezone": programs["timezone"]}, inputs, candidate_programs)
        config = {**config, "timezone": programs["timezone"]}
        engine = _resolve_engine(config, len(candidate_programs))
        input_hash = _input_hash(
            inputs, candidate_programs, config, recruitment, targets, programs, cur, int(inputs["diary_seq_cutoff"])
        )
        new_version = int(current["weight_version"]) + 1
        status = "calibrated" if state["calibration"].get("success") else "rejected"
        stale_indexes = _stale_indexes(cur, task_id)
        cur.execute(
            """
            UPDATE plan_tasks
               SET status=%s, engine=%s, weight_version=%s, inputs=%s, candidate_programs=%s,
                   survey_period=%s, config=%s, input_hash=%s, calibration=%s, sample_summary=%s,
                   note=COALESCE(%s, note), updated_at=now()
             WHERE task_id=%s
            """,
            (
                status,
                engine,
                new_version,
                jb(inputs),
                jb(candidate_programs),
                jb(state["survey_period"]),
                jb(config),
                input_hash,
                jb(state["calibration"]),
                jb(state["sample_summary"]),
                body.get("note"),
                task_id,
            ),
        )
        _persist_state(cur, task_id, new_version, state)
        index_built = False
        if status == "calibrated" and engine == "bitmap":
            persons = _load_plan_persons(cur, task_id, new_version)
            index = BitmapIndex.build(persons, candidate_programs, state["threshold"])
            _persist_index(cur, task_id, new_version, index)
            index_built = True
        cur.execute(
            """
            UPDATE plan_jobs
               SET status='stale_rejected', finished_at=now(),
                   stale_reason = jsonb_build_object(
                       'code', 'plan_weight_version_changed',
                       'message', 'survey period switched or weights updated; the job''s snapshot is stale',
                       'snapshot_weight_version', weight_version_snapshot,
                       'current_weight_version', %s
                   )
             WHERE task_id=%s AND status='queued' AND weight_version_snapshot <> %s
            """,
            (new_version, task_id, new_version),
        )
        stale_jobs = cur.rowcount
        _audit(
            cur,
            actor,
            "plan_task.rebind",
            task_id,
            {
                "weight_version": new_version,
                "inputs": inputs,
                "input_hash": input_hash,
                "status": status,
                "stale_indexes_invalidated": stale_indexes,
                "stale_jobs_rejected": stale_jobs,
            },
        )
        conn.commit()
        task = fetch_task(cur, task_id)
    return {
        "task": task,
        "weight_version": new_version,
        "engine_resolved": engine,
        "bitmap_index_built": index_built,
        "stale_indexes_invalidated": stale_indexes,
        "stale_jobs_rejected": stale_jobs,
    }


# ---------------------------------------------------------------------------
# Evaluation (synchronous what-if over arbitrary sets)
# ---------------------------------------------------------------------------
def _validate_set_request(task: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    candidates = set(task["candidate_programs"])
    requested = body["set"]
    if len(requested) != len(set(requested)):
        raise invalid("plan_set_duplicate", "the evaluation set contains duplicate programs")
    unknown = sorted(set(requested) - candidates)
    if unknown:
        raise invalid(
            "plan_program_unknown",
            "evaluation set references programs outside the task candidate pool",
            {"programs": unknown, "candidate_pool": sorted(candidates)},
        )
    marginals = list(dict.fromkeys(body.get("marginal_candidates") or []))
    unknown_marginals = sorted(set(marginals) - candidates)
    if unknown_marginals:
        raise invalid(
            "plan_program_unknown",
            "marginal candidates are outside the task candidate pool",
            {"programs": unknown_marginals},
        )
    intersections = body.get("intersections") or []
    for group in intersections:
        if len(group) < 2:
            raise invalid("plan_intersection_invalid", "an intersection query needs at least two programs")
        unknown_group = sorted(set(group) - candidates)
        if unknown_group:
            raise invalid(
                "plan_program_unknown",
                "intersection references programs outside the task candidate pool",
                {"programs": unknown_group},
            )
    return {
        "set": list(requested),
        "marginal_candidates": [p for p in marginals if p not in requested],
        "intersections": [list(dict.fromkeys(g)) for g in intersections],
        "engine": body.get("engine"),
        "include_persons": bool(body.get("include_persons")),
    }


def evaluate_task(db: Database, task_id: str, body: dict[str, Any], actor: str) -> dict[str, Any]:
    with db.scoped() as conn:
        cur = conn.cursor()
        task = _fetch_task_for_update(cur, task_id)
        if task["status"] != "calibrated":
            raise conflict(
                "plan_task_not_calibrated",
                "plan task calibration did not succeed; evaluation is unavailable",
                {"calibration": task["calibration"]},
            )
        req = _validate_set_request(task, body)
        engine = req["engine"] or task["engine"]
        if engine not in ("scan", "bitmap"):
            raise invalid("plan_engine_invalid", "engine must be scan or bitmap")
        threshold = float(task["config"]["reach_minutes"])
        persons = _load_plan_persons(cur, task_id, task["weight_version"])
        if engine == "bitmap":
            index = _load_or_build_index(cur, task, persons, threshold)
            evaluator: Any = BitmapEvaluator(index)
        else:
            evaluator = ScanEvaluator(persons, threshold)

        metrics = evaluate_set(
            evaluator,
            req["set"],
            marginal_candidates=req["marginal_candidates"],
            intersection_sets=req["intersections"],
        )
        result: dict[str, Any] = {
            "set": req["set"],
            "reach_threshold_minutes": threshold,
            "sample": {
                "persons": len(persons),
                "total_weight": round(sum(p.weight for p in persons), 6),
            },
            **metrics,
            "method": {
                "union": "weighted dedup over persons; a plan's value is new listeners, never a sum of per-program counts",
                "missing_data": (
                    "persons whose relevant airtime is not fully covered are unknown: they count toward "
                    "the upper bound and are never placed in not_reached"
                ),
                "engine_scan": "per-person scan of the set with weight dedup; explainable to the individual",
                "engine_bitmap": "per-program compressed bitmaps indexed by person; set union is a bitmap OR, still exact",
                "high_order_intersections": "computed exactly on demand from per-person facts; never pre-materialised, never truncated",
            },
        }
        if req["include_persons"]:
            result["persons"] = person_breakdown(persons, req["set"], threshold)

        eval_id = str(uuid.uuid4())
        cur.execute(
            """
            INSERT INTO plan_evaluations (eval_id, task_id, weight_version, engine, request, result, created_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                eval_id,
                task_id,
                task["weight_version"],
                engine,
                jb(body),
                jb(result),
                actor,
            ),
        )
        _audit(
            cur,
            actor,
            "plan_task.evaluate",
            task_id,
            {"eval_id": eval_id, "weight_version": task["weight_version"], "engine": engine, "set": req["set"]},
        )
        conn.commit()
    return {
        "eval_id": eval_id,
        "task_id": task_id,
        "weight_version": task["weight_version"],
        "engine_used": engine,
        **result,
    }


# ---------------------------------------------------------------------------
# Optimisation jobs (two-phase: queue under a version snapshot, execute later)
# ---------------------------------------------------------------------------
def create_optimization(db: Database, task_id: str, body: dict[str, Any], actor: str) -> dict[str, Any]:
    with db.scoped() as conn:
        cur = conn.cursor()
        task = fetch_task(cur, task_id)
        if task["status"] != "calibrated":
            raise conflict(
                "plan_task_not_calibrated",
                "plan task calibration did not succeed; optimisation is unavailable",
                {"calibration": task["calibration"]},
            )
        candidates = list(task["candidate_programs"])
        # Fail fast on domain errors instead of queueing a job that cannot run.
        from .analytics.reach import validate_optimize_request

        validate_optimize_request(
            candidates,
            {k: float(v) for k, v in body["costs"].items()},
            float(body["budget"]),
            list(body.get("must_include") or []),
            [list(g) for g in (body.get("exclusive_groups") or [])],
        )
        job_id = str(uuid.uuid4())
        cur.execute(
            """
            INSERT INTO plan_jobs
                (job_id, task_id, kind, status, weight_version_snapshot, input_hash_snapshot,
                 request, created_by)
            VALUES (%s,%s,'optimize','queued',%s,%s,%s,%s)
            """,
            (
                job_id,
                task_id,
                task["weight_version"],
                task["input_hash"],
                jb(body),
                actor,
            ),
        )
        _audit(
            cur,
            actor,
            "plan_task.job_create",
            task_id,
            {"job_id": job_id, "weight_version_snapshot": task["weight_version"]},
        )
        conn.commit()
        cur.execute("SELECT * FROM plan_jobs WHERE job_id=%s", (job_id,))
        job = serialize_job(cur.fetchone())
    return {"job": job}


def execute_optimization(db: Database, task_id: str, job_id: str, actor: str) -> dict[str, Any]:
    with db.scoped() as conn:
        cur = conn.cursor()
        task = _fetch_task_for_update(cur, task_id)
        cur.execute(
            "SELECT * FROM plan_jobs WHERE job_id=%s AND task_id=%s",
            (job_id, task_id),
        )
        row = cur.fetchone()
        if row is None:
            raise not_found("plan_job_not_found", "optimisation job does not exist for this task")
        job = serialize_job(row)
        if job["status"] == "done":
            return {"job": job, "already_executed": True}
        if job["status"] == "stale_rejected":
            raise conflict(
                "plan_job_stale",
                "the job's weight-version snapshot is stale; it may not write back",
                job.get("stale_reason"),
            )
        if task["status"] != "calibrated":
            raise conflict(
                "plan_task_not_calibrated",
                "plan task calibration did not succeed; optimisation is unavailable",
                {"calibration": task["calibration"]},
            )
        if (
            int(job["weight_version_snapshot"]) != int(task["weight_version"])
            or job["input_hash_snapshot"] != task["input_hash"]
        ):
            stale_reason = {
                "code": "plan_weight_version_changed",
                "message": (
                    "survey period switched or weights updated since the job was queued; "
                    "results computed from the old base are refused and nothing is written back"
                ),
                "snapshot_weight_version": job["weight_version_snapshot"],
                "current_weight_version": task["weight_version"],
            }
            cur.execute(
                "UPDATE plan_jobs SET status='stale_rejected', stale_reason=%s, finished_at=now() WHERE job_id=%s",
                (jb(stale_reason), job_id),
            )
            _audit(cur, actor, "plan_task.job_stale_rejected", task_id, {"job_id": job_id, **stale_reason})
            conn.commit()
            raise conflict("plan_job_stale", stale_reason["message"], stale_reason)

        request = job["request"]
        threshold = float(task["config"]["reach_minutes"])
        persons = _load_plan_persons(cur, task_id, task["weight_version"])
        engine = task["engine"]
        if engine == "bitmap":
            index = _load_or_build_index(cur, task, persons, threshold)
            evaluator = BitmapEvaluator(index)
        else:
            evaluator = ScanEvaluator(persons, threshold)
        result = greedy_optimize(
            evaluator,
            programs=list(task["candidate_programs"]),
            costs={k: float(v) for k, v in request["costs"].items()},
            budget=float(request["budget"]),
            must_include=list(request.get("must_include") or []),
            exclusive_groups=[list(g) for g in (request.get("exclusive_groups") or [])],
            score=request.get("score") or "marginal_per_cost",
        )
        result.update(
            {
                "engine_used": engine,
                "weight_version": task["weight_version"],
                "reach_threshold_minutes": threshold,
                "sample": {
                    "persons": len(persons),
                    "total_weight": round(sum(p.weight for p in persons), 6),
                },
            }
        )
        cur.execute(
            "UPDATE plan_jobs SET status='done', result=%s, finished_at=now() WHERE job_id=%s",
            (jb(result), job_id),
        )
        _audit(
            cur,
            actor,
            "plan_task.job_execute",
            task_id,
            {"job_id": job_id, "weight_version": task["weight_version"], "stop_reason": result["stop_reason"]},
        )
        conn.commit()
        cur.execute("SELECT * FROM plan_jobs WHERE job_id=%s", (job_id,))
        return {"job": serialize_job(cur.fetchone()), "already_executed": False}
