# Target-model adapters

These profiles are compilation guidance, not permanent API truth. Before emitting executable API parameters, verify the selected model version against its current official documentation. Keep prompt text separate from request parameters.

## Generic

- Produce semantically aligned natural-language Chinese and English positive prompts plus a time-coded shot list.
- Put durable subject and scene anchors before shot-specific actions.
- Express each shot as opening state, action path, camera path, ending state, and transition.
- Produce matching Chinese and English negative prompts. Keep each language's positive and negative prompts together, with Chinese and English in separate copy blocks as described by the analysis contract.
- Keep negative constraints separate from each positive prompt when the target supports them; otherwise append a concise avoidance clause inside the corresponding language section while retaining all four headings.

## Veo

- Write cinematic natural language with explicit subject, action, context, camera, style, temporal progression, and audio intent.
- Treat dialogue, sound effects, and ambience as first-class prompt elements when requested.
- Prefer one coherent 8-second unit per generation. For longer references, split on shot or narrative boundaries and plan extensions or editorial assembly.
- When visual continuity matters, recommend first/last-frame or reference-image conditioning instead of adding more adjectives.
- Current capability reference: https://ai.google.dev/gemini-api/docs/veo

## Sora

- Preserve the user's requested Sora adapter, but surface current availability warnings separately from the prompt.
- Compile a concise scene description with chronological action, camera choreography, lighting, texture, pacing, and audio intent.
- If generating through the legacy API, record model, duration, size, reference asset, and returned job ID. Do not pretend the API exposes a deterministic seed when it does not.
- As checked on 2026-09-04, the official Sora video API was marked deprecated and scheduled to shut down on 2026-09-24. Re-check before any execution.
- Current API reference: https://developers.openai.com/api/reference/typescript/resources/videos/methods/create

## Kling

- Use explicit multi-shot blocks when the selected Kling version supports storyboards. Include duration, shot size, perspective, narrative function, and camera movement per shot.
- Express subject and element references consistently; prefer reference assets for identity and environment continuity.
- Keep dialogue order, language, delivery, and audio cues explicit for audio-capable variants.
- Do not emit camera-control JSON or duration limits until the exact Kling endpoint and model version are verified; capabilities differ across versions and providers.
- Current official overview for Kling 3.0: https://ir.kuaishou.com/node/11216/pdf

## Wan

- Lead with dynamic content: subject action sequence, motion direction, expression change, and camera movement.
- Avoid spending the prompt budget repeating static details already fixed by an input image.
- Preserve and emphasize camera direction and temporal order.
- Alongside the required Chinese copy prompt, emit a compact model-native English prompt of at most 100 words when following the official Wan2.2 prompt-extension style.
- Record task (`t2v`, `i2v`, or `ti2v`), model/checkpoint, resolution, frame count, sampler, sampling steps, guidance scale, base seed, and whether prompt extension was used.
- Official prompt-extension guidance: https://github.com/Wan-Video/Wan2.2/blob/main/wan/utils/system_prompt.py
- Official inference parameters: https://github.com/Wan-Video/Wan2.2/blob/main/generate.py

## Seedance

- 以中文自然语言为主，按“主体/动作 → 场景/光线 → 运镜 → 风格/画质”组织；运镜用自然语言指令（如“固定机位微晃”“缓慢推进”“轻微摇移”），不要堆砌英文参数。
- 明确主体动作顺序、运动方向与表情变化；Seedance 对连续运动幅度敏感，宜用“持续轻微上下起伏”“手持往复微晃”等带程度与循环的词，避免把单镜头自拍微晃误写成“镜头缓慢下移”或“取景渐宽”。
- 单镜头自拍场景应写为“手持自拍式持续微晃，全片无完全静止的帧，整体净漂移小于 15px，取景范围基本不变”，而非一次性推移。
- t2v 与 i2v 均需记录：模型版本、时长（常用 6s/10s）、分辨率（720p/1080p）、画幅比（16:9/9:16）、参考图（i2v）与 seed。
- 记录关键执行参数与 warning 后再生成；生成前再次核对当前 Seedance 版本与 i2v 参考图支持情况。
- 官方参考：https://www.volcengine.com/product/video-generation

## Shared negative constraints

For every target, render the selected constraints in both Chinese and English and keep them semantically aligned. Place each negative prompt in the same language-specific block as its positive prompt; keep the Chinese and English blocks separate.

Tailor these to observed failure risks; do not blindly include every term:

- identity: face drift, age drift, hairstyle drift, inconsistent body proportions;
- anatomy: extra fingers or limbs, fused hands, broken joints, malformed facial features;
- wardrobe/props: color or texture changes, disappearing accessories, duplicated props;
- environment: background flicker, moving architecture, inconsistent shadows or reflections;
- motion/physics: teleportation, sliding feet, discontinuous trajectories, impossible contact, object penetration;
- camera/editing: accidental zoom, horizon roll, focus pumping, unmotivated cuts, reversed screen direction;
- rendering: temporal shimmer, texture boiling, ghosting, compression artifacts, subtitles, logos, watermarks.
