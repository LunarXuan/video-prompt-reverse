#!/usr/bin/env python3
"""Compile a validated generation spec into model-targeted deliverables."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

from validate_spec import validate


TARGETS = ("generic", "veo", "sora", "kling", "wan", "seedance")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compact(parts: list[object], separator: str = "；") -> str:
    return separator.join(str(part).strip() for part in parts if part is not None and str(part).strip())


def aspect_ratio(source: dict) -> str:
    width, height = source.get("width"), source.get("height")
    if not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0:
        return "保持源视频宽高比"
    from math import gcd
    divisor = gcd(width, height)
    return f"{width // divisor}:{height // divisor}"


def negative_text(negative: dict) -> str:
    labels = {
        "identity": "身份一致性",
        "anatomy": "肢体与面部",
        "wardrobe_props": "服装与道具",
        "environment": "环境连续性",
        "motion_physics": "动作与物理",
        "camera_editing": "镜头与剪辑",
        "rendering": "渲染质量",
    }
    blocks = []
    for key, label in labels.items():
        values = negative.get(key, [])
        if values:
            blocks.append(f"{label}：{compact(values, '、')}")
    return "；".join(blocks)


def shot_sentence(shot: dict) -> str:
    time_range = f"{float(shot['start']):.2f}-{float(shot['end']):.2f}秒"
    visual = compact([
        shot.get("shot_size"), shot.get("camera_angle"), shot.get("focal_length"),
        shot.get("composition"), shot.get("depth_of_field"),
    ], "，")
    progression = compact([
        f"起始：{shot.get('opening_frame', '')}",
        f"主体：{shot.get('subject_action', '')}",
        f"轨迹：{shot.get('motion_trajectory', '')}",
        f"表情：{shot.get('expression_change', '')}",
        f"摄影机：{compact([shot.get('camera_motion'), shot.get('camera_speed')], '，')}",
        f"光线变化：{shot.get('lighting_change', '')}",
        f"结束：{shot.get('ending_frame', '')}",
        f"声音：{compact([shot.get('speech_function'), shot.get('sound_and_music')], '，')}",
        f"衔接：{shot.get('transition_out', '')}",
    ])
    return f"[{shot.get('id')}｜{time_range}] {visual}。{progression}。"


def mechanics_clauses(data: dict) -> tuple[list[str], list[str]]:
    """Prompt clauses for confirmed body mechanisms, paired in both languages.

    Unconfirmed hypotheses stay out of the prompts: an observation is not an
    action until a human accepts the mechanism behind it.
    """
    mechanics = data.get("action_mechanics")
    if not isinstance(mechanics, dict) or mechanics.get("status") != "analyst_confirmed":
        return [], []
    chinese, english = [], []
    for item in mechanics.get("hypotheses", []):
        if not isinstance(item, dict) or item.get("verdict") != "supported":
            continue
        if item.get("prompt_hint_zh"):
            chinese.append(str(item["prompt_hint_zh"]).strip())
        if item.get("prompt_hint_en"):
            english.append(str(item["prompt_hint_en"]).strip())
    return chinese, english


def action_mechanics_markdown(data: dict) -> str:
    mechanics = data.get("action_mechanics")
    if not isinstance(mechanics, dict):
        return "# Action mechanics\n\nNot provided. Run scripts/analyze_motion.py before describing motion.\n"
    verdicts = {"supported": "支持", "refuted": "证伪", "inconclusive": "证据不足"}
    lines = ["# Action mechanics", "",
             f"status: `{mechanics.get('status')}`；primary: `{mechanics.get('primary_id')}`", "",
             "| 假设 | 结论 | 置信度 | 发力链 | 支持证据 | 反对证据 |",
             "| --- | --- | --- | --- | --- | --- |"]
    for item in mechanics.get("hypotheses", []):
        chain = " → ".join(
            f"{link.get('region')}({link.get('role')}/{link.get('joint_action')})"
            for link in item.get("chain", []) if isinstance(link, dict))
        lines.append("| {} | {} | {:.2f} | {} | {} | {} |".format(
            item.get("id", ""), verdicts.get(item.get("verdict"), item.get("verdict")),
            float(item.get("confidence", 0)), chain or "-",
            "；".join(item.get("support", [])) or "-", "；".join(item.get("against", [])) or "-"))
    lines += ["", "只有 `status: analyst_confirmed` 且 `verdict: supported` 的假设才会写入提示词。", ""]
    return "\n".join(lines)


def compile_chinese_positive(data: dict) -> str:
    source = data["source"]
    global_data = data["global"]
    lighting = global_data.get("lighting", {})
    duration = float(source["duration_seconds"])
    header = (
        f"生成一段约{duration:.2f}秒、{aspect_ratio(source)}画幅的视频。"
        "只复用参考视频的镜头逻辑、空间关系、视觉方法与节奏，不复刻具体人物、品牌、台词或故事。"
    )
    anchors = compact([
        f"主体原型：{global_data.get('subject_archetype', '')}",
        f"身份锚点：{compact(global_data.get('identity_anchors', []), '、')}",
        f"服装材质锚点：{compact(global_data.get('wardrobe_material_anchors', []), '、')}",
        f"环境：{global_data.get('environment', '')}",
        f"前中后景：{global_data.get('foreground_midground_background', '')}",
        f"时间与天气：{global_data.get('time_and_weather', '')}",
        f"光线：{compact([lighting.get('type'), lighting.get('direction'), lighting.get('intensity'), lighting.get('color_temperature'), lighting.get('contrast')], '，')}",
        f"色彩与材质：{global_data.get('palette_and_materials', '')}",
        f"视觉风格：{global_data.get('visual_style', '')}",
    ])
    shots = "\n".join(shot_sentence(shot) for shot in data["shots"])
    mechanics_zh, _ = mechanics_clauses(data)
    mechanics = f"动作机理：{compact(mechanics_zh, '；')}。" if mechanics_zh else ""
    rhythm = compact([
        f"叙事逻辑：{global_data.get('narrative_logic', '')}",
        f"节奏逻辑：{global_data.get('pacing_logic', '')}",
        f"全程保持：{compact(global_data.get('continuity_invariants', []), '、')}",
    ])
    blocks = [header, f"{anchors}。", shots]
    if mechanics:
        blocks.append(mechanics)
    blocks.append(f"{rhythm}。")
    return "\n".join(blocks).strip() + "\n"


def language_prompt(sections: list[tuple[str, str]]) -> str:
    return "\n\n".join(f"{heading}\n\n{text.strip()}" for heading, text in sections) + "\n"


def native_prompt(data: dict, target: str, english: str, chinese: str | None = None) -> tuple[str, str | None]:
    supplied = data.get("target_prompts", {}).get(target, {})
    if isinstance(supplied, str) and supplied.strip():
        return supplied.strip() + "\n", None
    if isinstance(supplied, dict) and isinstance(supplied.get("native_prompt"), str) and supplied["native_prompt"].strip():
        return supplied["native_prompt"].strip() + "\n", None
    prefixes = {
        "generic": "通用视频生成提示词：",
        "veo": "Veo cinematic video prompt；保留自然语言时间推进、摄影机运动与原生音频意图。\n",
        "sora": "Sora chronological cinematic prompt；保留动作、镜头、光线、材质和声音的时间关系。\n",
        "kling": "Kling 多镜头生成提示词；各镜头明确时长、景别、视角、动作和运镜。\n",
        "wan": "Wan 动态优先提示词；突出动作顺序、运动方向、表情变化和摄影机运动。\n",
        "seedance": "Seedance 中文视频提示词（中文优先，按 主体/动作 → 场景/光线 → 运镜 → 风格/画质 组织）：\n",
    }
    warning = None
    if target == "seedance" and chinese:
        return prefixes["seedance"] + chinese, warning
    return prefixes[target] + english, warning


def timeline_markdown(data: dict) -> str:
    headers = ["镜号", "时间/时长", "景别", "机位/角度", "焦段/景深", "构图", "主体动作/表情", "运镜/速度", "声音", "转场", "置信度"]
    rows = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for shot in data["shots"]:
        duration = float(shot["end"]) - float(shot["start"])
        cells = [
            shot["id"], f"{shot['start']:.2f}-{shot['end']:.2f}s / {duration:.2f}s",
            shot["shot_size"], shot["camera_angle"], compact([shot.get("focal_length"), shot.get("depth_of_field")], "/"),
            shot["composition"], compact([shot["subject_action"], shot["expression_change"]], "；"),
            compact([shot["camera_motion"], shot["camera_speed"]], "；"), shot["sound_and_music"],
            shot["transition_out"], f"{float(shot['confidence']):.2f}",
        ]
        safe = [str(cell).replace("|", "\\|").replace("\n", " ") for cell in cells]
        rows.append("| " + " | ".join(safe) + " |")
    return "\n".join(rows) + "\n"


def compile_visual_evidence(data: dict, output_dir: Path) -> list[dict]:
    evidence_dir = output_dir / "evidence_frames"
    evidence_dir.mkdir(exist_ok=True)
    records = []
    gallery = ["# Visual evidence frames", ""]
    allowed_suffixes = {".jpg", ".jpeg", ".png", ".webp"}
    for item in data["visual_evidence"]:
        source = Path(item["path"]).expanduser().resolve()
        if not source.is_file():
            raise OSError(f"visual evidence image not found: {source}")
        suffix = source.suffix.lower()
        if suffix not in allowed_suffixes:
            raise ValueError(f"unsupported visual evidence image type: {source}")
        filename = f"{item['id']}_{float(item['timestamp']):010.3f}s{suffix}"
        target = (evidence_dir / filename).resolve()
        if source != target:
            shutil.copy2(source, target)
        digest = sha256_file(target)
        record = {
            "id": item["id"],
            "shot_id": item["shot_id"],
            "timestamp": float(item["timestamp"]),
            "role": item["role"],
            "caption_zh": item["caption_zh"],
            "caption_en": item["caption_en"],
            "path": str(target),
            "sha256": digest,
        }
        records.append(record)
        title = f"{item['id']} | {item['shot_id']} | {float(item['timestamp']):.3f}s | {item['role']}"
        alt = f"{item['caption_zh']} / {item['caption_en']}"
        gallery.extend([f"## {title}", "", f"![{alt}](evidence_frames/{filename})", ""])
    (output_dir / "evidence_gallery.md").write_text("\n".join(gallery).rstrip() + "\n", encoding="utf-8")
    (output_dir / "visual_evidence.json").write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path)
    parser.add_argument("--target", choices=TARGETS, default="generic")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    data = json.loads(args.spec.read_text(encoding="utf-8-sig"))
    errors = validate(data)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    output_dir = args.output_dir.resolve()
    if output_dir.exists() and any(output_dir.iterdir()) and not args.overwrite:
        parser.error(f"output directory is not empty: {output_dir}; use --overwrite deliberately")
    output_dir.mkdir(parents=True, exist_ok=True)
    visual_evidence = compile_visual_evidence(data, output_dir)

    profile_path = Path(__file__).resolve().parent.parent / "references" / "model_profiles.json"
    profiles = json.loads(profile_path.read_text(encoding="utf-8"))
    profile = profiles[args.target]
    chinese_positive = compile_chinese_positive(data)
    chinese_negative = negative_text(data["negative_constraints"]).strip() + "\n"
    _, mechanics_en = mechanics_clauses(data)
    english_positive = data["english_prompt"]["positive"].strip()
    if mechanics_en:
        english_positive += "\n\nBody mechanics: " + "; ".join(mechanics_en) + "."
    english_positive += "\n"
    english_negative = data["english_prompt"]["negative"].strip() + "\n"
    chinese_bundle = language_prompt([
        ("中文正面提示词", chinese_positive),
        ("中文负面提示词", chinese_negative),
    ])
    english_bundle = language_prompt([
        ("English Positive Prompt", english_positive),
        ("English Negative Prompt", english_negative),
    ])
    native, native_warning = native_prompt(data, args.target, english_positive, chinese_positive)

    (output_dir / "prompt_zh.txt").write_text(chinese_bundle, encoding="utf-8")
    (output_dir / "prompt_en.txt").write_text(english_bundle, encoding="utf-8")
    (output_dir / "prompt_native.txt").write_text(native, encoding="utf-8")
    (output_dir / "negative_prompt.txt").write_text(chinese_negative, encoding="utf-8")
    (output_dir / "timeline.md").write_text(timeline_markdown(data), encoding="utf-8")
    (output_dir / "action_mechanics.md").write_text(action_mechanics_markdown(data), encoding="utf-8")
    if isinstance(data.get("motion_analysis"), dict):
        (output_dir / "motion_analysis.json").write_text(
            json.dumps(data["motion_analysis"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    shutil.copy2(args.spec, output_dir / "generation_spec.json")

    plan = {
        "target": args.target,
        "profile_version": profiles["profile_version"],
        "profile": profile,
        "duration_seconds": data["source"]["duration_seconds"],
        "aspect_ratio": aspect_ratio(data["source"]),
        "native_prompt_warning": native_warning,
        "execution_parameters": data.get("execution_parameters", {}).get(args.target, {}),
    }
    (output_dir / "generation_plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    manifest = {
        "manifest_version": "1.1",
        "source_sha256": data["source"]["sha256"],
        "spec_sha256": hashlib.sha256(args.spec.read_bytes()).hexdigest(),
        "motion_analysis_sha256": (sha256_text(json.dumps(data["motion_analysis"], ensure_ascii=False, indent=2) + "\n")
                                   if isinstance(data.get("motion_analysis"), dict) else None),
        "action_mechanics_sha256": (sha256_text(action_mechanics_markdown(data))
                                    if isinstance(data.get("action_mechanics"), dict) else None),
        "action_mechanics_status": (data.get("action_mechanics", {}) or {}).get("status"),
        "prompt_zh_bundle_sha256": sha256_text(chinese_bundle),
        "prompt_en_bundle_sha256": sha256_text(english_bundle),
        "prompt_zh_positive_sha256": sha256_text(chinese_positive),
        "prompt_zh_negative_sha256": sha256_text(chinese_negative),
        "prompt_en_positive_sha256": sha256_text(english_positive),
        "prompt_en_negative_sha256": sha256_text(english_negative),
        "prompt_zh_sha256": sha256_text(chinese_positive),
        "prompt_native_sha256": sha256_text(native),
        "visual_evidence": [
            {"id": item["id"], "shot_id": item["shot_id"], "timestamp": item["timestamp"], "sha256": item["sha256"]}
            for item in visual_evidence
        ],
        "target": args.target,
        "profile_version": profiles["profile_version"],
        "uncertainties": data["uncertainties"],
    }
    (output_dir / "reproducibility.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(output_dir)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
