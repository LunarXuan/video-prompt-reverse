# Analysis and output contract

Use this contract for the internal analysis record. The record is evidence for prompt compilation, not a prose summary.

## Analysis principles

- Follow time order and cut boundaries.
- Separate observed facts from inferred cinematography.
- Separate subject motion, camera motion, and editing transitions.
- Track invariants across shots: subject archetype, silhouette, wardrobe palette, props, screen direction, relative positions, light direction, palette, and material response.
- For method-only transformation, abstract specific people, brands, logos, dialogue, and story facts while retaining their functional role.
- Never conclude "the camera did not move" or "the shot is static" from adjacent frames alone. Sub-pixel continuous motion is invisible frame to frame and only appears when accumulated, so measure it with `scripts/analyze_motion.py` before describing any shot as still.
- Copy dialogue only when the user explicitly requests transcription and the content is appropriate to reproduce. Otherwise record `speech_function`, cadence, emotion, and approximate duration.

## Required JSON shape

```json
{
  "schema_version": "1.2",
  "source": {
    "path": "reference.mp4",
    "sha256": "...",
    "duration_seconds": 8.0,
    "fps": 24.0,
    "width": 1280,
    "height": 720,
    "has_audio": true,
    "sampling_timestamps": [0.0, 1.5, 3.0, 5.0, 7.8]
  },
  "intent": {
    "content_policy": "transform-only",
    "target_models": ["generic"],
    "fidelity": "cinematic"
  },
  "global": {
    "subject_archetype": "",
    "identity_anchors": [],
    "wardrobe_material_anchors": [],
    "environment": "",
    "foreground_midground_background": "",
    "time_and_weather": "",
    "lighting": {
      "type": "",
      "direction": "",
      "intensity": "",
      "color_temperature": "",
      "contrast": ""
    },
    "palette_and_materials": "",
    "visual_style": "",
    "narrative_logic": "",
    "pacing_logic": "",
    "continuity_invariants": []
  },
  "shots": [
    {
      "id": "S01",
      "start": 0.0,
      "end": 2.5,
      "shot_size": "medium close-up",
      "camera_angle": "eye level",
      "focal_length": "inferred normal lens",
      "composition": "",
      "depth_of_field": "",
      "opening_frame": "",
      "subject_action": "",
      "motion_trajectory": "",
      "expression_change": "",
      "camera_motion": "",
      "camera_speed": "",
      "lighting_change": "",
      "ending_frame": "",
      "transition_in": "cut",
      "transition_out": "cut",
      "speech_function": "",
      "subtitle_function": "",
      "sound_and_music": "",
      "effects": "",
      "continuity_anchors": [],
      "confidence": 0.8,
      "evidence_timestamps": [0.0, 1.2, 2.4]
    }
  ],
  "visual_evidence": [
    {
      "id": "E01",
      "shot_id": "S01",
      "timestamp": 0.0,
      "path": "/absolute/path/to/extracted/frame.jpg",
      "role": "opening",
      "caption_zh": "S01起始画面",
      "caption_en": "Opening frame of S01"
    }
  ],
  "negative_constraints": {
    "identity": [],
    "anatomy": [],
    "wardrobe_props": [],
    "environment": [],
    "motion_physics": [],
    "camera_editing": [],
    "rendering": []
  },
  "english_prompt": {
    "positive": "A complete English positive prompt semantically aligned with the compiled Chinese positive prompt.",
    "negative": "A complete English negative prompt semantically aligned with all grouped Chinese negative constraints."
  },
  "motion_analysis": {},
  "action_mechanics": {},
  "uncertainties": []
}
```

`motion_analysis` and `action_mechanics` are optional (see below); leave them out only when motion analysis was impossible, and record that omission in `uncertainties`.
```

## Shot table columns

Use: `镜号 | 时间/时长 | 景别 | 机位/角度 | 焦段/景深 | 构图 | 起始画面 | 主体动作/表情 | 运镜/速度 | 光线/色彩/材质 | 字幕/语言功能 | 音效/BGM | 结束画面 | 转场/剪辑 | 置信度`.

If a field is not observable, write `无法从视频可靠判断` rather than filling it from convention.

## Visual evidence contract

`visual_evidence` is the curated display set, not every frame used during analysis. Each item must identify an existing unedited source-extracted image, its exact timestamp, the shot it supports, its evidence role, and concise Chinese and English captions. Use stable IDs such as `E01`, `E02`, and keep chronological order.

Select 4-12 items by default. Cover opening and ending states plus the action, expression, camera, or transition changes that materially support the reverse-engineered prompt. Include at least one item per shot when practical. For long or highly cut videos, a timestamped contact sheet may count as one display item when showing every image individually would overwhelm the response.

Compilation copies selected images into `evidence_frames/`, writes `evidence_gallery.md` and `visual_evidence.json`, and records each image hash in the reproducibility manifest. In the final response, display every curated item inline with an absolute path from the compiled output directory. Put the gallery immediately before the Chinese and English prompt blocks. Do not display generated substitutes or omit a curated image after claiming it as evidence.

## Motion analysis and action mechanics

Static frames cannot establish motion. Run `scripts/analyze_motion.py` after probing and before writing the spec. It emits two layers:

- **`motion_analysis`** — observations only: multi-interval frame differences, dense optical flow, grid hotspots, ROI displacement series, silhouette tracking, rigid-fit inlier ratio, and the reciprocity ratio `R = |net displacement| / accumulated path length`.
- **`action_mechanics`** — candidate body mechanisms that could have produced those observations, each with supporting and opposing evidence and a confidence score.

Rules that follow from this split:

- Frame difference and optical flow are **observations**. Never copy them into a prompt as an action ("local motion 4.2 px/frame" is not an action).
- Always separate **net drift** from **accumulated path**. `R <= 0.25` means reciprocating motion (oscillation), `R >= 0.65` means unidirectional travel, and values between are mixed. A low `R` forbids describing the shot as a pan, tilt, or dolly.
- Judge whole-body motion on **absolute** signals (silhouette position, area, limb width). In a selfie the body carries the camera, so residual flow with the global motion removed also removes a genuine body bounce.
- Infer the **mechanism**: which region drives the movement, which joints follow, whether the cycle repeats, and what force produces it. Example of a well-formed conclusion: "the body keeps a gentle cyclic vertical bounce driven by the hips, with the knees softly absorbing it", not "the hip region moves 1.05 px/frame".
- Every hypothesis carries `support`, `against`, and `confidence`. Monocular depth ambiguity, occlusion, and low texture must lower confidence and be recorded as limitations.
- `status` is `auto_hypothesis` until a human runs `--confirm-hypothesis`. **Unconfirmed hypotheses must not be written into prompts as fact.** Compilation only injects mechanism clauses when `status` is `analyst_confirmed`.

Both objects are optional so existing specs keep compiling. When present, `scripts/validate_spec.py` checks their structure, and `scripts/compile_prompt.py` emits `action_mechanics.md` plus a hash in the reproducibility manifest.

```json
{
  "motion_analysis": {
    "version": "1.0",
    "sampling": {"analysis_fps": 30.0, "frame_count": 166, "size": [320, 568]},
    "frame_difference": {"1": 5.32, "5": 16.18, "15": 14.56, "30": 19.21},
    "global": {"flow_mean_px_per_frame": 1.10, "reciprocity": 0.14, "character": "reciprocating"},
    "rois": [{"id": "head", "local_mean_px_per_frame": 1.75, "cycle": {"cyclic": false}}],
    "silhouette": {"head_top_range_px": 33, "head_top_trend_px_per_s": 0.04,
                   "cycle": {"head_top": {"cyclic": true, "hz": 2.17, "amplitude_px": 15.6}}},
    "rigid_fit": {"inlier_ratio": 0.13},
    "limitations": ["monocular depth ambiguity caps front-back claims"]
  },
  "action_mechanics": {
    "status": "analyst_confirmed",
    "primary_id": "h_vertical_bounce",
    "hypotheses": [
      {"id": "h_vertical_bounce", "verdict": "supported", "confidence": 0.85,
       "chain": [{"region": "hip", "role": "driver", "joint_action": "ant_post_push_pull"},
                 {"region": "knee", "role": "follower", "joint_action": "flex_extend"}],
       "support": ["head-top row oscillates at 2.17 Hz"], "against": ["hip drive stays indirect"],
       "prompt_hint_zh": "身体随胯部前后推拉做持续、轻微的上下起伏",
       "prompt_hint_en": "the body keeps a gentle continuous vertical bounce driven by the hips"}
    ]
  }
}
```

## Bilingual final-prompt package

First write the Chinese positive prompt in this order:

1. format, duration, aspect ratio, and overall visual method;
2. anonymized subject and immutable visual anchors;
3. environment, depth layers, time, light, palette, and materials;
4. chronological shot blocks with opening state, action path, expression, camera movement, ending state, and transition;
5. pacing, effects, environmental sound, and music intent;
6. continuity requirements.

Keep negative constraints out of the positive prompt. Compile the final copy surfaces as exactly two files and two fenced blocks in this exact order:

1. Chinese block/file `prompt_zh.txt`: `中文正面提示词`, then `中文负面提示词`.
2. English block/file `prompt_en.txt`: `English Positive Prompt`, then `English Negative Prompt`.

The English pair must be a faithful semantic rendering of the Chinese pair, not a shorter summary. Preserve measurable timing, subject and camera trajectories, audio intent, continuity anchors, and explicit uncertainties. Do not add details unsupported by the evidence. Store the English pair in `english_prompt.positive` and `english_prompt.negative` before validation and compilation. Never split one language's positive and negative prompts across separate copy blocks. Keep Chinese and English in separate blocks.

Prefer measurable timing and spatial relations over decorative adjectives. Preserve no more detail than the source supports.
