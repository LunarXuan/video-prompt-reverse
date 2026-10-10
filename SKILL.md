---
name: video-prompt-reverse
description: "Reverse-engineer a supplied video into a shot-by-shot generation specification, displayed evidence frames, and paired Chinese-English prompt packages. Use for requests such as 视频反推提示词, 视频转提示词, 从视频生成提示词, 视频逆向提示词, 逐镜头反推, 分镜拆解并生成提示词, 反推视频 prompt, or video-to-prompt; also use for Veo, Sora, Kling, Wan, generic prompt adaptation, and bounded generate-compare-optimize loops. Do not use for ordinary video summaries or basic transcription."
---

# Video Prompt Reverse

Turn a reference video into an executable, evidence-linked generation package. Distinguish semantic recreation from exact reconstruction: a text prompt can preserve scene logic, action, camera language, pacing, and style, but cannot guarantee frame-identical output. Escalate to reference frames or control signals when identity, geometry, or motion fidelity matters.

## Invocation cues

Treat “视频反推提示词” and semantically similar requests as direct requests to use this skill. Common equivalents include “视频转提示词”, “从视频生成提示词”, “视频逆向提示词”, “逐镜头反推”, “分镜拆解并生成提示词”, “反推视频 prompt”, and “video-to-prompt”. The user may append a target model, such as Veo, Sora, Kling, Wan, or Seedance, and may request iterative similarity optimization.

## Choose the mode

- **Prompt**: analyze the video and produce two language-specific packages: one Chinese block containing its positive and negative prompts, followed by one English block containing its positive and negative prompts.
- **Adapt**: also compile model-native prompts for one or more of `veo`, `sora`, `kling`, `wan`, `seedance`, or `generic`.
- **Optimize**: when a callable target generator and evaluator are available, generate bounded candidate batches, score them against the reference, and revise the prompt.

If the target model is absent, use `generic`. If the requested fidelity is unclear, default to semantic and cinematic similarity, not identity or pixel reconstruction.

## Analyze the source

1. Inspect the source without altering it. Record its SHA-256, duration, frame rate, resolution, audio presence, and sampling timestamps. Use `scripts/probe_video.py` when `ffprobe` and `ffmpeg` are available.
2. Measure motion before describing it. Run `scripts/analyze_motion.py` (needs `numpy` and `opencv`; install with `pip install -r requirements.txt`) and read `motion_report.md`. Extracted frames alone cannot establish motion: sub-pixel continuous movement is invisible between adjacent frames and only appears once accumulated, so never call a shot still, nor attribute a slow pan, tilt, or reframing to it, without this measurement.
3. Sample scene boundaries, regular temporal coverage, and high-motion intervals. From those analysis frames, select a concise final evidence set that covers opening states, action or expression changes, high-motion events, transitions, and ending states. Use 4-12 evidence frames by default, include at least one representative frame per shot when practical, and use timestamped contact sheets when more than 12 frames are essential. Final evidence must be unedited source frames, not generated reconstructions. For videos longer than 30 seconds, analyze scene-aligned chunks; use short 3-10 second subclips for dense action, then reconcile them into one timeline.
4. Inspect enough frames to distinguish subject motion from camera motion. Use audio or a transcript when sound, speech cadence, or editing beats affect the result.
5. Convert observations into mechanisms. Frame difference, optical flow, and hotspot maps are **observations**, never actions. For each moving region infer what body mechanics produced the signal: which region drives it, which joints follow, whether it repeats, and how the force is applied. Write "the body keeps a gentle cyclic vertical bounce driven by the hips, the knees softly absorbing it", not "the hip region moves 1.05 px/frame". Always separate net drift from accumulated path: a low reciprocity ratio `R = |net| / path` means oscillation and forbids describing the shot as a pan, tilt, or dolly. Copy `motion_analysis` and `action_mechanics` from the analyzer into the spec, then confirm the mechanism you accept:

   ```text
   python scripts/analyze_motion.py VIDEO --output-dir DIR --analysis-fps 30 \
     --probe-manifest DIR/video_manifest.json --confirm-hypothesis h_vertical_bounce
   ```

   Leave `status` at `auto_hypothesis` when you cannot decide, and never write an unconfirmed hypothesis into a prompt as fact.
6. Build the structured record in [references/analysis-contract.md](references/analysis-contract.md). Record each selected frame in `visual_evidence` with its shot, timestamp, source path, role, and bilingual captions. Write the semantically aligned English positive and negative prompts into `english_prompt`; do not rely on the compiler to machine-translate Chinese. Validate the record with `scripts/validate_spec.py` before compiling prompts.
7. Mark uncertain or inferred attributes explicitly. Do not invent lens focal length, lighting equipment, off-screen action, or dialogue when the evidence does not support them. Record monocular depth ambiguity, occlusion, and low-texture regions as limitations that lower confidence.

When the user asks to learn only the visual method, replace specific people, brands, copyrighted dialogue, logos, and story facts with functional archetypes. Preserve shot logic, spatial relations, timing, action mechanics, lighting, materials, and editing grammar.

## Compile the deliverables

Read [references/model-adapters.md](references/model-adapters.md) only for requested target models. Run:

```text
python scripts/compile_prompt.py SPEC.json --target generic --output-dir OUTPUT
```

For every request, return:

- a chronological shot table with shot number, exact interval, size, angle, composition, camera motion, action and expression, lighting, audio cue, transition, and confidence;
- narrative and pacing logic, visual invariants, and shot-to-shot continuity anchors;
- an inline visual-evidence gallery in the same final response, placed immediately before the prompt blocks; display every frame selected in `visual_evidence` with its shot ID and timestamp, using absolute local image paths copied into the compiled output directory;
- keep dense analysis-only frames out of the final response. If more than 12 final frames are needed, display one or more timestamped contact sheets rather than flooding the response;
- exactly two directly copyable fenced blocks, in this order: first a Chinese block containing `中文正面提示词` followed by `中文负面提示词`; then an English block containing `English Positive Prompt` followed by `English Negative Prompt`;
- keep each language's positive and negative prompts together: the primary artifacts are `prompt_zh.txt` and `prompt_en.txt`; never split a language's positive and negative prompts into separate copy blocks or make the user assemble them manually;
- make the English positive and negative prompts semantically faithful to the Chinese pair, preserving timing, spatial relations, continuity, audio intent, and uncertainty without adding unsupported detail;
- apply requested model-native conventions to both language versions; auxiliary compatibility files must not replace the two language-specific copy surfaces;
- negative constraints grouped by identity, anatomy, wardrobe, environment, motion, camera, and rendering;
- a reproducibility manifest containing hashes for both language packages, their four sections, and every displayed evidence image, plus model/profile version, duration, aspect ratio, seeds when known, and unresolved uncertainties.

Do not claim that a prompt is the source video's original prompt. Call it a reverse-engineered generation specification.

## Convert to a standard prompt

The compiled prompts are descriptive prose; many target platforms (notably Seedance 2.0) expect a stricter shape. Run the converter to reformat a SPEC into the standard storyboard-prompt layout and rewrite it through the sensitive-word compliance table.

```text
python scripts/convert_to_standard.py SPEC.json --output-dir OUTPUT --overwrite
```

The converter produces:

- `standard_prompt_seedance_zh.txt` — standard Seedance 2.0 prompt in Chinese (primary deliverable)
- `standard_prompt_seedance_en.txt` — same structure in English
- `standard_prompt_seedance.json` — structured sections plus a `compliance_hits` report

Standard layout (see [references/standard-prompt-conversion.md](references/standard-prompt-conversion.md)):

- every segment in `[时间轴]` carries **景别 + 运镜/转场 + 画面元素运动 + 声音**;
- the eight-module formula is preserved: **[风格] + [主体描述] + [场景环境] + [动作行为] + [镜头语言] + [光影氛围] + [风格参考] + [约束词]**;
- the compliance pass replaces high-risk tokens (真实/真人/实拍, 品牌/logo, 裸露, 暴力与不良引导类) with compliant phrasing **only in positive sections**; negative constraints are kept verbatim so prohibitions stay readable.

## Optimize against a target model

Read [references/optimization-loop.md](references/optimization-loop.md) before running external generation. Optimization requires one locked target model/version and explicit generator/evaluator command adapters. Preserve every candidate and score; do not delete weak or failed results.

Use `scripts/closed_loop.py` for bounded orchestration. Stop at the first of: target score reached, maximum iterations reached, repeated plateau, generator failure, evaluator failure, or a required external decision. Never switch model versions, seeds, aspect ratio, duration, or evaluator weights mid-run without starting a new experiment record.

If prompt-only optimization plateaus on identity, layout, or motion, report the limitation and recommend a first/last frame, reference image/video, pose, depth, optical flow, or trajectory-control workflow. Do not disguise control-conditioned reconstruction as prompt-only success.
