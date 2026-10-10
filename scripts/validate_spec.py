#!/usr/bin/env python3
"""Validate the structural and temporal invariants of a generation spec."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


SHOT_TEXT_FIELDS = [
    "shot_size", "camera_angle", "composition", "opening_frame", "subject_action",
    "motion_trajectory", "expression_change", "camera_motion", "camera_speed",
    "ending_frame", "transition_in", "transition_out", "sound_and_music",
]
NEGATIVE_GROUPS = [
    "identity", "anatomy", "wardrobe_props", "environment",
    "motion_physics", "camera_editing", "rendering",
]


def validate(data: object) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["root must be an object"]
    for key in ("schema_version", "source", "intent", "global", "shots", "visual_evidence", "negative_constraints", "english_prompt", "uncertainties"):
        if key not in data:
            errors.append(f"missing root field: {key}")

    source = data.get("source", {})
    if not isinstance(source, dict):
        errors.append("source must be an object")
        source = {}
    duration = source.get("duration_seconds")
    if not isinstance(duration, (int, float)) or duration <= 0:
        errors.append("source.duration_seconds must be positive")
        duration = None
    digest = source.get("sha256")
    if not isinstance(digest, str) or len(digest) != 64:
        errors.append("source.sha256 must be a 64-character digest")

    shots = data.get("shots")
    if not isinstance(shots, list) or not shots:
        errors.append("shots must be a non-empty array")
        shots = []
    seen_ids: set[str] = set()
    previous_end = 0.0
    for index, shot in enumerate(shots):
        prefix = f"shots[{index}]"
        if not isinstance(shot, dict):
            errors.append(f"{prefix} must be an object")
            continue
        shot_id = shot.get("id")
        if not isinstance(shot_id, str) or not shot_id:
            errors.append(f"{prefix}.id must be a non-empty string")
        elif shot_id in seen_ids:
            errors.append(f"{prefix}.id is duplicated: {shot_id}")
        else:
            seen_ids.add(shot_id)
        start, end = shot.get("start"), shot.get("end")
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            errors.append(f"{prefix}.start and .end must be numbers")
            continue
        if start < 0 or end <= start:
            errors.append(f"{prefix} must satisfy 0 <= start < end")
        if start + 0.001 < previous_end:
            errors.append(f"{prefix} overlaps the previous shot")
        if duration is not None and end > duration + 0.05:
            errors.append(f"{prefix}.end exceeds source duration")
        previous_end = max(previous_end, float(end))
        for field in SHOT_TEXT_FIELDS:
            if field not in shot or not isinstance(shot.get(field), str):
                errors.append(f"{prefix}.{field} must be a string")
        confidence = shot.get("confidence")
        if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            errors.append(f"{prefix}.confidence must be within [0, 1]")
        evidence = shot.get("evidence_timestamps")
        if not isinstance(evidence, list) or not all(isinstance(item, (int, float)) for item in evidence):
            errors.append(f"{prefix}.evidence_timestamps must be a numeric array")

    visual_evidence = data.get("visual_evidence")
    if not isinstance(visual_evidence, list) or not visual_evidence:
        errors.append("visual_evidence must be a non-empty array")
        visual_evidence = []
    seen_evidence_ids: set[str] = set()
    previous_timestamp = -1.0
    for index, item in enumerate(visual_evidence):
        prefix = f"visual_evidence[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix} must be an object")
            continue
        evidence_id = item.get("id")
        if not isinstance(evidence_id, str) or not evidence_id.strip():
            errors.append(f"{prefix}.id must be a non-empty string")
        elif evidence_id in seen_evidence_ids:
            errors.append(f"{prefix}.id is duplicated: {evidence_id}")
        else:
            seen_evidence_ids.add(evidence_id)
        shot_id = item.get("shot_id")
        if not isinstance(shot_id, str) or shot_id not in seen_ids:
            errors.append(f"{prefix}.shot_id must reference an existing shot")
        timestamp = item.get("timestamp")
        if not isinstance(timestamp, (int, float)):
            errors.append(f"{prefix}.timestamp must be a number")
        else:
            if timestamp < 0 or (duration is not None and timestamp > duration + 0.05):
                errors.append(f"{prefix}.timestamp must be within source duration")
            if timestamp + 0.001 < previous_timestamp:
                errors.append(f"{prefix}.timestamp is out of chronological order")
            previous_timestamp = max(previous_timestamp, float(timestamp))
        for field in ("path", "role", "caption_zh", "caption_en"):
            value = item.get(field)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{prefix}.{field} must be a non-empty string")

    negatives = data.get("negative_constraints", {})
    if not isinstance(negatives, dict):
        errors.append("negative_constraints must be an object")
    else:
        for group in NEGATIVE_GROUPS:
            if not isinstance(negatives.get(group), list):
                errors.append(f"negative_constraints.{group} must be an array")
    english_prompt = data.get("english_prompt", {})
    if not isinstance(english_prompt, dict):
        errors.append("english_prompt must be an object")
    else:
        for field in ("positive", "negative"):
            value = english_prompt.get(field)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"english_prompt.{field} must be a non-empty string")
    # Optional motion layers. Absent means "motion analysis was not run"; present
    # means the structure must hold, because prompts are generated from it.
    motion = data.get("motion_analysis")
    if motion is not None:
        if not isinstance(motion, dict):
            errors.append("motion_analysis must be an object")
        else:
            for field in ("version", "sampling", "global", "rois", "limitations"):
                if field not in motion:
                    errors.append(f"missing motion_analysis field: {field}")
            global_motion = motion.get("global")
            if isinstance(global_motion, dict):
                ratio = global_motion.get("reciprocity")
                if ratio is not None and (not isinstance(ratio, (int, float)) or not 0 <= ratio <= 1):
                    errors.append("motion_analysis.global.reciprocity must be within [0, 1]")
            rois = motion.get("rois")
            if isinstance(rois, list):
                seen_roi_ids: set[str] = set()
                for index, roi in enumerate(rois):
                    prefix = f"motion_analysis.rois[{index}]"
                    if not isinstance(roi, dict):
                        errors.append(f"{prefix} must be an object")
                        continue
                    roi_id = roi.get("id")
                    if not isinstance(roi_id, str) or not roi_id:
                        errors.append(f"{prefix}.id must be a non-empty string")
                    elif roi_id in seen_roi_ids:
                        errors.append(f"{prefix}.id is duplicated: {roi_id}")
                    else:
                        seen_roi_ids.add(roi_id)
            elif rois is not None:
                errors.append("motion_analysis.rois must be an array")
            limitations = motion.get("limitations")
            if limitations is not None and not isinstance(limitations, list):
                errors.append("motion_analysis.limitations must be an array")

    mechanics = data.get("action_mechanics")
    if mechanics is not None:
        if not isinstance(mechanics, dict):
            errors.append("action_mechanics must be an object")
        else:
            seen_ids: set[str] = set()
            status = mechanics.get("status")
            if status not in ("auto_hypothesis", "analyst_confirmed"):
                errors.append("action_mechanics.status must be auto_hypothesis or analyst_confirmed")
            hypotheses = mechanics.get("hypotheses")
            if not isinstance(hypotheses, list):
                errors.append("action_mechanics.hypotheses must be an array")
            else:
                for index, item in enumerate(hypotheses):
                    prefix = f"action_mechanics.hypotheses[{index}]"
                    if not isinstance(item, dict):
                        errors.append(f"{prefix} must be an object")
                        continue
                    identifier = item.get("id")
                    if not isinstance(identifier, str) or not identifier:
                        errors.append(f"{prefix}.id must be a non-empty string")
                    elif identifier in seen_ids:
                        errors.append(f"{prefix}.id is duplicated: {identifier}")
                    else:
                        seen_ids.add(identifier)
                    if item.get("verdict") not in ("supported", "refuted", "inconclusive"):
                        errors.append(f"{prefix}.verdict must be supported, refuted or inconclusive")
                    confidence = item.get("confidence")
                    if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
                        errors.append(f"{prefix}.confidence must be within [0, 1]")
                    chain = item.get("chain")
                    if not isinstance(chain, list):
                        errors.append(f"{prefix}.chain must be an array")
                    else:
                        for link_index, link in enumerate(chain):
                            if not isinstance(link, dict) or not link.get("region"):
                                errors.append(f"{prefix}.chain[{link_index}] must name a region")
                    if not isinstance(item.get("support"), list) or not isinstance(item.get("against"), list):
                        errors.append(f"{prefix}.support and .against must be arrays")
                    # Anything written into a prompt needs a paired clause in both languages.
                    if item.get("verdict") == "supported":
                        if not item.get("prompt_hint_zh") or not item.get("prompt_hint_en"):
                            errors.append(f"{prefix} supported hypothesis needs prompt_hint_zh and prompt_hint_en")
            primary = mechanics.get("primary_id")
            if primary is not None and primary not in seen_ids:
                errors.append("action_mechanics.primary_id must reference an existing hypothesis")

    if not isinstance(data.get("uncertainties"), list):
        errors.append("uncertainties must be an array")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path)
    args = parser.parse_args()
    data = json.loads(args.spec.read_text(encoding="utf-8-sig"))
    errors = validate(data)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"VALID: {args.spec}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
