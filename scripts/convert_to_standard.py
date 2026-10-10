#!/usr/bin/env python3
"""Convert a reverse-engineered analysis SPEC into a standard Seedance 2.0 storyboard prompt.

The video-prompt-reverse pipeline produces a structured SPEC (subject archetype, scene,
shot list, motion analysis, action mechanics, negative constraints). Those fields are
descriptive prose, not the "standard" prompt shape that the Seedance 2.0 skill expects.

This script bridges the two:

* applies the eight-module formula  ([风格]+[主体描述]+[场景环境]+[动作行为]+[镜头语言]+[光影氛围]+[风格参考]+[约束词])
* emits a [时间轴] where every segment carries 景别 + 运镜/转场 + 画面元素运动 + 声音
* rewrites the result through the Seedance sensitive-word compliance table so the output
  is safer to paste into the platform

Usage:
    python3 scripts/convert_to_standard.py SPEC.json --output-dir OUT [--overwrite]
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Sensitive-word compliance table (from the Seedance 2.0 skill, section 十二).
# Maps high-risk tokens to compliant replacements. Longer tokens are matched first.
# Only intended for positive, compliant creative contexts.
# ---------------------------------------------------------------------------
COMPLIANCE_REPLACEMENTS: dict[str, str] = {
    # 通用高风险词
    "现实生活中": "生活化场景",
    "现实中": "写实语境",
    "实拍": "实景拍摄风格",
    "真人": "真人质感",
    "真实": "写实风格",
    "公众人物": "同类型气质",
    "艺人": "同类型气质",
    "网红": "同款风格",
    "明星": "同款风格",
    "奢侈品": "简约设计款",
    "商标": "同风格设计",
    "logo": "同风格设计",
    "Logo": "同风格设计",
    "LOGO": "同风格设计",
    "品牌名": "同风格设计",
    "驾崩": "剧情落幕",
    "猝死": "生命终点",
    "去世": "离世",
    "逝世": "离世",
    "死亡": "剧情落幕",
    "生孩子": "新生命降临",
    "分娩": "待产",
    "孕妇": "准妈妈",
    "怀孕": "孕期",
    "防疫": "健康防护",
    "病毒": "健康防护",
    "新冠": "公共卫生事件",
    "疫情": "公共卫生事件",
    "美元": "财富",
    "人民币": "资金",
    "现金": "货币",
    "钞票": "货币",
    # 低俗风险类（仅正向艺术/时尚/影视可用）
    "走光": "氛围感穿搭",
    "凸点": "贴身造型",
    "真空": "修身穿搭",
    "性感暴露": "氛围感穿搭",
    "全裸": "素体建模",
    "裸体": "人体艺术造型",
    "撩拨": "神态演绎",
    "诱惑": "情绪感",
    "挑逗": "氛围感",
    "性暗示": "肢体表现力",
    "大尺度": "亲密戏份",
    "激情戏": "情感对手戏",
    "床戏": "剧情互动片段",
    "露肤过多": "露肤设计",
    "裸露": "肌肤线条",
    # 暴力风险类（仅正向影视/科普/竞技可用）
    "校园霸凌": "反霸凌普法内容",
    "家暴": "反家暴公益片段",
    "跳楼": "安全警示教育",
    "溺水": "应急避险科普",
    "坠楼": "安全警示教育",
    "车祸": "交通安全警示",
    "管制器械": "竞技装备",
    "匕首": "道具武器",
    "刀具": "道具武器",
    "枪支": "影视道具",
    "武器": "影视道具",
    "血肉模糊": "剧情化受伤片段",
    "伤口": "剧情化受伤片段",
    "受伤": "负伤",
    "流血": "负伤",
    "互殴": "冲突剧情",
    "斗殴": "搏击竞技",
    "打架": "动作戏",
    # 不良引导与违规风险类
    "封建迷信": "传统民俗展示",
    "跳大神": "非遗文化内容",
    "风水": "传统民俗展示",
    "算命": "传统民俗展示",
    "传销": "非法金融警示内容",
    "套路贷": "金融反诈科普",
    "高利贷": "金融反诈科普",
    "网贷": "金融反诈科普",
    "刷单": "金融反诈科普",
    "酗酒": "健康生活科普",
    "醉酒": "健康生活科普",
    "电子烟": "无烟草画面",
    "抽烟": "无烟草画面",
    "吸烟": "健康生活科普",
    "超速": "违规驾驶警示教育",
    "飙车": "交通安全警示",
    "醉驾": "违规驾驶警示教育",
    "酒驾": "交通安全警示",
    "不良行为": "青少年成长剧情",
    "烟酒": "青春校园日常",
    "早恋": "青春校园日常",
}

# Build a regex that matches the longest tokens first.
_COMPLIANCE_PATTERN = re.compile(
    "|".join(re.escape(k) for k in sorted(COMPLIANCE_REPLACEMENTS, key=len, reverse=True))
)


def compliance_replace(text: str) -> tuple[str, list[dict]]:
    """Return (cleaned_text, replaced_token_records)."""
    hits: list[dict] = []

    def _sub(match: "re.Match[str]") -> str:
        token = match.group(0)
        hits.append({"token": token, "replacement": COMPLIANCE_REPLACEMENTS[token]})
        return COMPLIANCE_REPLACEMENTS[token]

    cleaned = _COMPLIANCE_PATTERN.sub(_sub, text)
    return cleaned, hits


# Engineering metrics (px/frame, Hz, optical-flow, grayscale deltas, ROI percentages…)
# belong in the internal SPEC / motion_analysis blocks for human review only. They
# must never leak into the model-facing prompt. This guard detects leftovers.
_ENG_UNIT_RE = re.compile(
    r"px/?帧|帧/秒|/\s*255|灰度|光流|画高|净位移|累计路径|相邻帧|非刚性|"
    r"\b\d+(\.\d+)?\s*px\b|Hz|毫秒|fps|像素差|帧差",
    re.I,
)


def warn_engineering_units(text: str, label: str) -> None:
    """Print a warning if the model-facing text still carries engineering metrics."""
    hits = sorted(set(_ENG_UNIT_RE.findall(text)))
    if hits:
        print(f"WARNING: {label} 仍含工程单位/指标: {hits}")


# ---------------------------------------------------------------------------
# Field mapping helpers
# ---------------------------------------------------------------------------

def aspect_ratio(width: int, height: int) -> str:
    """Classify the frame ratio into a Seedance-friendly label."""
    if width <= 0 or height <= 0:
        return "9:16"
    ratio = height / width
    if ratio >= 1.6:
        return "9:16 竖屏"
    if ratio <= 1 / 1.6:
        return "16:9 横屏"
    if abs(width / height - 2.35) < 0.15:
        return "2.35:1 电影宽屏"
    divisor = math.gcd(width, height)
    return f"{width // divisor}:{height // divisor}"


def shot_size_to_jingbie(shot_size: str) -> str:
    """Map a free-form shot-size string to the standard 景别 vocabulary."""
    text = shot_size or ""
    # Order matters: longer / more specific terms must be tested before shorter
    # ones. "全身" (full body) maps to the standard 全景 label.
    for term, label in (
        ("特写", "特写"),
        ("中近景", "中近景"),
        ("近景", "近景"),
        ("全身", "全景"),
        ("全景", "全景"),
        ("中景", "中景"),
        ("远景", "远景"),
        ("主观视角", "主观视角"),
    ):
        if term in text:
            return label
    return "中景"


_NEGATION_STRIP = re.compile(
    r"无[推拉摇移环绕升降]{1,8}|无持续俯仰平移|无俯仰平移|无平移|不[推拉摇移环绕升降]|不环绕|不升降"
)


def camera_motion_to_yunjing(camera_motion: str) -> str:
    """Normalise the camera-motion description into a Seedance 运镜 label.

    Negated terms such as '无推拉' must NOT be read as positive motion.
    """
    text = _NEGATION_STRIP.sub("", camera_motion or "")
    # "位移/漂移" describe physical drift, not a camera-move label; strip them
    # so their "移" character is not misread as a positive move.
    text = text.replace("位移", "").replace("漂移", "")
    labels = []
    if "推" in text:
        labels.append("推镜头")
    if "拉" in text:
        labels.append("拉镜头")
    if "摇" in text:
        labels.append("摇镜头")
    if "移" in text:
        labels.append("移镜头")
    if "环绕" in text:
        labels.append("环绕")
    if "升降" in text or "俯冲" in text:
        labels.append("升降镜头")
    if "手持" in text or "晃" in text or "微晃" in text:
        labels.append("手持晃动")
    if not labels:
        return "固定机位（保持主体居中）"
    return "、".join(labels)


def _parse_time_segments(shot: dict) -> list[dict]:
    """Split a shot's natural-language action text into [start, end, text] segments.

    The reverse-engineered subject_action usually carries markers like
    '0-2.3秒：...；2.3-5.5秒：...'. We reuse those boundaries so the standard
    timeline matches the analyst's observed beats. Falls back to one segment.
    """
    action = shot.get("subject_action", "") or ""
    pattern = re.compile(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*秒\s*[:：]")
    matches = list(pattern.finditer(action))
    if not matches:
        return [{"start": shot.get("start", 0.0), "end": shot.get("end", 0.0), "text": action}]

    shot_start = float(shot.get("start", 0.0))
    shot_end = float(shot.get("end", 0.0))
    segments: list[dict] = []
    for idx, m in enumerate(matches):
        start = float(m.group(1))
        end = float(m.group(2))
        body_start = m.end()
        body_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(action)
        text = action[body_start:body_end].strip(" ；;，,")
        # Clamp to the shot window just in case the markers run wide.
        start = max(shot_start, min(start, shot_end))
        end = max(start, min(end, shot_end))
        segments.append({"start": start, "end": end, "text": text})
    return segments


def _fmt_ts(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return f"{value:.1f}"


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------

def build_style_line(global_data: dict, source: dict) -> str:
    visual_style = global_data.get("visual_style", "") or ""
    # Pull a compact style phrase from the front of visual_style if present.
    style_phrase = "写实风格"
    if "美颜" in visual_style or "AI人像" in visual_style:
        style_phrase = "AI美颜人像风格"
    elif "动画" in visual_style:
        style_phrase = "动画风格"
    elif "电影" in visual_style:
        style_phrase = "电影级写实风格"
    duration = float(source.get("duration_seconds", 0.0))
    ar = aspect_ratio(int(source.get("width", 0)), int(source.get("height", 0)))
    # 注意：本片为重度美颜/AI人像滤镜风格，"写实/真实" 会与"美颜柔肤质感"
    # 约束相互打架，故风格氛围用中性、不与美颜冲突的措辞。
    if "宣言" in (global_data.get("narrative_logic", "") or ""):
        atmosphere = "自信宣言氛围"
    elif "美颜" in visual_style or "AI人像" in visual_style:
        atmosphere = "柔和自然光感氛围"
    else:
        atmosphere = "自然光感氛围"
    return f"【风格】{style_phrase}，{_fmt_ts(round(duration, 1))}秒，{ar}，{atmosphere}"


def build_subject_block(global_data: dict) -> str:
    lines = ["【主体描述】"]
    lines.append(f"- 身份：{global_data.get('subject_archetype', '').strip()}")
    anchors = global_data.get("identity_anchors") or []
    if anchors:
        lines.append("- 外貌锚点：" + "；".join(anchors))
    wardrobe = global_data.get("wardrobe_material_anchors") or []
    if wardrobe:
        lines.append("- 服饰材质：" + "；".join(wardrobe))
    # Expression: derive from the first shot if possible (kept short here).
    return "\n".join(lines)


def build_scene_block(global_data: dict) -> str:
    lines = ["【场景环境】"]
    lines.append(f"- 定位：{global_data.get('environment', '').strip()}")
    lines.append(f"- 时间/天气：{global_data.get('time_and_weather', '').strip()}")
    lines.append(f"- 空间层次：{global_data.get('foreground_midground_background', '').strip()}")
    return "\n".join(lines)


def build_timeline(spec: dict, global_narrative: str = "") -> str:
    lines = ["【时间轴】"]
    for shot in spec.get("shots", []):
        jingbie = shot_size_to_jingbie(shot.get("shot_size", ""))
        yunjing = camera_motion_to_yunjing(shot.get("camera_motion", ""))
        cam_text = " ".join([
            shot.get("camera_motion", ""),
            shot.get("transition_in", ""),
            shot.get("transition_out", ""),
            global_narrative,
        ])
        single_take = any(k in cam_text for k in ("无剪辑", "单镜头", "无转场"))
        transition = "无剪辑单镜头" if single_take else "硬切"
        sound = shot.get("sound_and_music", "").strip()
        # Derive environment/effect motion from the shot itself instead of
        # hardcoding scene-specific wording (e.g. a previous video's wall).
        lighting_change = shot.get("lighting_change", "").strip() or "无可见变化"
        env_motion = f"环境光{lighting_change}，背景元素保持稳定无闪烁"
        if "美颜" in (shot.get("effects", "") or ""):
            effect_motion = "美颜滤镜带来面部与发丝的柔和微调"
        else:
            effect_motion = "画面细节随动作自然变化"
        segments = _parse_time_segments(shot)
        for idx, seg in enumerate(segments):
            start, end, text = seg["start"], seg["end"], seg["text"]
            # Weave subject action with environment/background/effect motion cues.
            # NOTE: motion_trajectory (engineering metrics) lives only in the
            # internal SPEC/motion_analysis blocks and must NEVER reach the model.
            seg_text = (
                f"{_fmt_ts(start)}-{_fmt_ts(end)}秒：{jingbie}，{yunjing}（{transition}），"
                f"{text}；{env_motion}，{effect_motion}"
            )
            seg_text += f"；{sound}" if sound else ""
            lines.append(seg_text)
        # Opening / ending frames as boundary notes.
        opening = shot.get("opening_frame", "").strip()
        ending = shot.get("ending_frame", "").strip()
        if opening:
            lines.append(f"（开场：{opening}）")
        if ending:
            lines.append(f"（结尾：{ending}）")
    return "\n".join(lines)


def build_lens_block(shot: dict) -> str:
    lines = ["【镜头语言】"]
    lines.append(f"- 景别：{shot.get('shot_size', '').strip()}")
    lines.append(f"- 运镜：{shot.get('camera_motion', '').strip()}")
    lines.append(f"- 角度：{shot.get('camera_angle', '').strip()}")
    lines.append(f"- 速度：{shot.get('camera_speed', '').strip()}")
    return "\n".join(lines)


def build_light_block(global_data: dict) -> str:
    lighting = global_data.get("lighting", {}) or {}
    lines = ["【光影氛围】"]
    direction = lighting.get("direction", "").strip()
    intensity = lighting.get("intensity", "").strip()
    temperature = lighting.get("color_temperature", "").strip()
    contrast = lighting.get("contrast", "").strip()
    if direction or intensity:
        lines.append(f"- 光影：{direction}；{intensity}".rstrip("；"))
    if temperature or contrast:
        lines.append(f"- 色调：{temperature}；{contrast}".rstrip("；"))
    return "\n".join(lines)


def build_constraint_block(negative: dict) -> str:
    """Negative constraints are already well-formed '不得…' sentences; keep them verbatim.

    Compliance replacement is intentionally NOT applied here, because these lines
    list forbidden tokens (e.g. 'logo') that must stay readable as prohibitions.
    """
    lines = ["【约束词】"]
    for category, items in (negative or {}).items():
        if not isinstance(items, list):
            continue
        joined = "；".join(str(i).strip() for i in items)
        if joined:
            lines.append(f"- {category}约束：{joined}")
    return "\n".join(lines)


def build_sound_block(shot: dict) -> str:
    sound = shot.get("sound_and_music", "").strip()
    subtitle = shot.get("subtitle_function", "").strip()
    parts = []
    if sound:
        parts.append(sound)
    if subtitle:
        parts.append(f"字幕：{subtitle}")
    body = "，".join(parts) if parts else "无对白，环境音自然"
    return f"【声音】{body}"


def build_reference_block(spec: dict) -> str:
    return "【参考】@视频1 原视频参考运镜与拍摄视角；可按需补充角色图/场景图参考"


def build_standard_prompt(spec: dict) -> str:
    global_data = spec.get("global", {})
    shots = spec.get("shots", [])
    shot0 = shots[0] if shots else {}

    # Positive sections go through sensitive-word compliance; the negative
    # constraint block is kept verbatim (see build_constraint_block).
    positive = "\n\n".join([
        build_style_line(global_data, spec.get("source", {})),
        build_subject_block(global_data),
        build_scene_block(global_data),
        build_timeline(spec, global_data.get("narrative_logic", "")),
        build_lens_block(shot0),
        build_light_block(global_data),
        build_sound_block(shot0),
        build_reference_block(spec),
    ])
    positive_clean, _ = compliance_replace(positive)
    constraint = build_constraint_block(spec.get("negative_constraints", {}))
    return positive_clean + "\n\n" + constraint + "\n"


# ---------------------------------------------------------------------------
# English standard prompt (best effort, mirrors the Chinese structure)
# ---------------------------------------------------------------------------

def _segment_en_by_markers(en_text: str, start: float, markers: list["re.Match[str]"]) -> str:
    """Slice the English prose from the marker matching `start` up to the next marker."""
    if not en_text or not markers:
        return ""
    for i, m in enumerate(markers):
        nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", m.group(0))]
        if nums and abs(nums[0] - start) < 0.01:
            begin = m.start()
            end_pos = markers[i + 1].start() if i + 1 < len(markers) else len(en_text)
            return en_text[begin:end_pos].strip().rstrip(";；,， ")
    return ""


def build_standard_prompt_en(spec: dict) -> str:
    global_data = spec.get("global", {})
    source = spec.get("source", {})
    shots = spec.get("shots", [])
    shot0 = shots[0] if shots else {}
    ar = aspect_ratio(int(source.get("width", 0)), int(source.get("height", 0)))
    duration = float(source.get("duration_seconds", 0.0))

    style = f"【Style】AI beauty-filter selfie style, {_fmt_ts(round(duration, 1))}s, {ar}, confident declaration mood"

    subject = "[Subject]\n" + (global_data.get("subject_archetype", "") or "")
    scene = "[Scene]\n" + (global_data.get("environment", "") or "") + "； " + (global_data.get("time_and_weather", "") or "")

    en_pos = (spec.get("english_prompt", {}) or {}).get("positive", "") or ""
    # Locate the prose chunk after the timeline marker, then cut at each time marker.
    marker_pat = re.compile(
        r"\d+(?:\.\d+)?\s*-\s*\d+(?:\.\d+)?\s*s|about\s+\d+(?:\.\d+)?\s+to\s+\d+(?:\.\d+)?\s+seconds|"
        r"\d+(?:\.\d+)?\s+to\s+\d+(?:\.\d+)?\s+seconds|from\s+about\s+\d+(?:\.\d+)?\s+to\s+\d+(?:\.\d+)?\s+seconds",
        re.I,
    )
    markers = list(marker_pat.finditer(en_pos))

    timeline_lines = ["[Timeline]"]
    for shot in shots:
        jingbie = shot_size_to_jingbie(shot.get("shot_size", ""))
        yunjing = camera_motion_to_yunjing(shot.get("camera_motion", ""))
        for seg in _parse_time_segments(shot):
            marker = f"{_fmt_ts(seg['start'])}-{_fmt_ts(seg['end'])}"
            snippet = _segment_en_by_markers(en_pos, seg["start"], markers)
            if snippet:
                timeline_lines.append(f"{marker}s: {jingbie}, {yunjing}, {snippet}")
            else:
                timeline_lines.append(f"{marker}s: {jingbie}, {yunjing}")

    sound = "[Sound] " + (shot0.get("sound_and_music", "") or "no dialogue")
    ref = "[Reference] @video1 original clip for camera motion and selfie angle"
    neg = "[Constraints]\n" + (spec.get("english_prompt", {}) or {}).get("negative", "") or ""

    return "\n\n".join([style, subject, scene, "\n".join(timeline_lines), sound, ref, neg]) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert a reverse-engineered SPEC into a standard Seedance 2.0 storyboard prompt.")
    parser.add_argument("spec", help="path to the analysis SPEC.json")
    parser.add_argument("--output-dir", required=True, help="directory for the standard prompt outputs")
    parser.add_argument("--target", default="seedance", choices=("seedance",), help="target standard format (seedance only for now)")
    parser.add_argument("--overwrite", action="store_true", help="overwrite existing outputs")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    spec_path = Path(args.spec)
    if not spec_path.is_file():
        print(f"ERROR: spec not found: {spec_path}", file=sys.stderr)
        return 1
    out_dir = Path(args.output_dir)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"ERROR: cannot create output dir: {exc}", file=sys.stderr)
        return 1
    if not args.overwrite and any(out_dir.glob("standard_prompt*")):
        print(f"ERROR: outputs exist in {out_dir}; pass --overwrite to replace", file=sys.stderr)
        return 1

    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"ERROR: cannot read spec: {exc}", file=sys.stderr)
        return 1

    zh = build_standard_prompt(spec)
    en = build_standard_prompt_en(spec)
    # Guard: the model-facing prompt must be pure natural-language semantics.
    warn_engineering_units(zh, "中文标准提示词")
    warn_engineering_units(en, "English standard prompt")
    # build_standard_prompt already applied compliance to the positive sections;
    # recompute hits for the report by diffing against the un-cleaned positive part.
    positive_unclean = "\n\n".join([
        build_style_line(spec.get("global", {}), spec.get("source", {})),
        build_subject_block(spec.get("global", {})),
        build_scene_block(spec.get("global", {})),
        build_timeline(spec, spec.get("global", {}).get("narrative_logic", "")),
        build_lens_block(spec.get("shots", [{}])[0] if spec.get("shots") else {}),
        build_light_block(spec.get("global", {})),
        build_sound_block(spec.get("shots", [{}])[0] if spec.get("shots") else {}),
        build_reference_block(spec),
    ])
    _, hits = compliance_replace(positive_unclean)

    (out_dir / "standard_prompt_seedance_zh.txt").write_text(zh, encoding="utf-8")
    (out_dir / "standard_prompt_seedance_en.txt").write_text(en, encoding="utf-8")
    structured = {
        "target": args.target,
        "style_line": build_style_line(spec.get("global", {}), spec.get("source", {})),
        "subject_block": build_subject_block(spec.get("global", {})),
        "scene_block": build_scene_block(spec.get("global", {})),
        "timeline": build_timeline(spec, spec.get("global", {}).get("narrative_logic", "")),
        "lens_block": build_lens_block(spec.get("shots", [{}])[0] if spec.get("shots") else {}),
        "light_block": build_light_block(spec.get("global", {})),
        "sound_block": build_sound_block(spec.get("shots", [{}])[0] if spec.get("shots") else {}),
        "reference_block": build_reference_block(spec),
        "constraint_block": build_constraint_block(spec.get("negative_constraints", {})),
        "compliance_hits": hits,
    }
    (out_dir / "standard_prompt_seedance.json").write_text(
        json.dumps(structured, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    if hits:
        print(f"compliance: replaced {len(hits)} sensitive token(s)")
    print(str((out_dir / 'standard_prompt_seedance_zh.txt').resolve()))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
