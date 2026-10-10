# Standard prompt conversion

`scripts/convert_to_standard.py` turns a reverse-engineered `SPEC.json` into the
**standard Seedance 2.0 storyboard-prompt layout** expected by many generation
platforms. It is a pure-stdlib transform: it does not re-analyze the video, it only
reshapes the structured fields already in the SPEC.

## Why this step exists

The compile step emits a faithful bilingual description. That text is good for humans
but is not what a platform like Seedance 2.0 wants to consume:

- the platform expects **[时间轴]** with **景别 + 运镜/转场 + 画面元素运动 + 声音** on every segment;
- it scores prompts higher when the eight-module skeleton is present
  (**风格 / 主体描述 / 场景环境 / 动作行为 / 镜头语言 / 光影氛围 / 风格参考 / 约束词**);
- high-risk tokens (真实/真人/实拍, 品牌/logo, 裸露, 暴力与不良引导类) raise audit-rejection risk
  and should be rewritten to compliant phrasing before submission.

## How it maps SPEC fields

| Standard section | Source field(s) |
|---|---|
| 【风格】 | `global.visual_style` + `source.duration_seconds` + frame aspect ratio |
| 【主体描述】 | `global.subject_archetype`, `identity_anchors`, `wardrobe_material_anchors` |
| 【场景环境】 | `global.environment`, `time_and_weather`, `foreground_midground_background` |
| 【时间轴】 | per-shot `subject_action` time markers → 景别 / 运镜 / 画面元素运动 / 声音 |
| 【镜头语言】 | `shots[].shot_size`, `camera_motion`, `camera_angle`, `camera_speed` |
| 【光影氛围】 | `global.lighting` |
| 【声音】 | `shots[].sound_and_music`, `subtitle_function` |
| 【参考】 | `source.sha256` (original clip as motion/angle reference) |
| 【约束词】 | `negative_constraints` (kept verbatim) |

## Compliance behavior

The compliance table lives inside the script (`COMPLIANCE_REPLACEMENTS`). It applies
**only to the positive sections**; the `【约束词】` block is copied verbatim because it
intentionally lists forbidden tokens (e.g. `logo`) that must remain readable as
prohibitions. The structured JSON output reports every replaced token under
`compliance_hits` so the change is auditable.

## Usage

```text
python scripts/convert_to_standard.py SPEC.json --output-dir OUTPUT [--target seedance] [--overwrite]
```

Outputs: `standard_prompt_seedance_zh.txt`, `standard_prompt_seedance_en.txt`,
`standard_prompt_seedance.json`.
