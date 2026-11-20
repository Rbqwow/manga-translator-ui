---
name: manga-translate
description: >-
  Translate manga chapters, folders, and archives using the local manga-translator
  MCP server without external LLM API keys. Use when the user asks to translate
  manga pages via Manga Translator UI or its local MCP service.
---

# Manga Translator MCP Workflow

All computer vision (upscaling, colorization, detection, OCR, inpainting) and Qt
rendering run locally inside **Manga Translator UI**. You perform all translation,
vision-assisted OCR correction, `[BR]` line-breaking, and glossary extraction
through the local MCP tools with zero external API keys.

## 1. Preflight & Job Creation

1. Call `get_workspace_info()` first:
   - If `api_conflicts` is non-empty, **stop immediately** and tell the user which
     pipeline settings (`ocr.ocr`, `ocr.secondary_ocr`, `render.renderer`,
     `colorizer.colorizer`) must be switched back to local models in Settings.
   - If `busy` is `true`, tell the user what is currently running (`busy_reason`).
   - Note `effective_mode`, `max_parallel_batches`, `batch_size`, `context_size`,
     `target_lang`, `ai_line_break_enabled`, `extract_glossary`, `gui_file_count`,
     and `output_dir`.
2. Call `create_translation_job(...)`:
   - Pass explicit parameters (`inputs`, `output_dir`, `target_lang`,
     `translation_mode`, `allow_on_demand_image`, `overwrite`, `retranslate_json`,
     etc.) only when requested by the user; otherwise rely on GUI workspace state.
   - Save the returned `job_id`, `guidelines`, and initial `glossary`. These are
     returned once per job and apply to every batch.
   - If `pending_pages == 0`, call `next_translation_batch(job_id)` once to confirm
     `done == true` and report the skipped pages summary.

## 2. Translation Loop (Closed Loop)

1. Call `next_translation_batch(job_id, worker_id=...)` to lease the first batch.
2. Translate every bubble in `pages[].regions` into `target_lang`, honoring:
   - `guidelines` (including custom prompts, tone, formatting, and `[BR]` rules)
   - `glossary` + incremental `glossary_delta`
   - `prev_context` (previous completed pages in reading order)
3. Respect `effective_mode`:
   - **`hq`**: Full-page numbered box images are provided with every batch.
     Cross-check OCR text against each numbered box in the image, fix OCR errors
     from visual context, and reflect speaker emotion/action in the translation.
   - **`text_on_demand`**: Text and confidence metadata are provided first.
     Whenever a region ID appears in `low_confidence_ids`, looks like garbled OCR,
     contains SFX/onomatopoeia, or lacks speaker/subject clarity, call
     `get_page_preview(job_id, page_index, region_ids=[...])` (set `high_res=True`
     for tiny or blurry text) before finalizing that batch.
   - **`text_only`**: Translate strictly from text and `prev_context`; do not call
     `get_page_preview`.
4. **Disk fallback images**: Whenever a page reports `image_delivery_mode == "file_path"`
   or `get_page_preview` returns `delivery_mode == "file_path"`, open or inspect
   `preview_path` / `file_path` using your host's local file/image viewing tool.
5. **Line breaks (`[BR]`) & Glossary**:
   - When `ai_line_break_enabled == true`: add explicit `[BR]` markers between
     lines for multi-line bubbles (`original_region_count >= 2`), and keep
     single-line bubbles (`original_region_count == 1`) free of `[BR]`.
   - When `extract_glossary == true`: return genuinely new recurring proper nouns
     in `new_terms` following the schema in `guidelines`.
6. Submit via `submit_translation_batch(job_id, batch_id, translations, new_terms=..., return_next=True)`:
   - Each entry in `translations` is `{"id": <int>, "translation": "<str>"}`.
   - Repeat directly with the returned `next_batch` until `done == true` without
     pausing to ask the user mid-book.
   - If `next_batch.waiting == true`, wait briefly or check `get_job_status(job_id)`
     and call `next_translation_batch` again.

## 3. Partial Self-Healing Retry

If `submit_translation_batch` returns `status == "retry_needed"`:
- Inspect `errors` (`missing_id`, `missing_br_marker`, `repetition_hallucination`,
  `target_language_ratio_low`, etc.).
- Repair only the problematic IDs (or batch language ratio) and immediately
  resubmit the complete batch to `submit_translation_batch`.

## 4. Long Manga & Subagent Orchestration

- **`pending_pages <= 12`**: Complete the entire loop directly in the main session.
- **`pending_pages > 12` and `max_parallel_batches == 1`** (Sequential Relay):
  - Create the job in the main session and keep `job_id` + `guidelines`.
  - If your host supports spawning subagents, dispatch clean sequential subagents
    passing `job_id`, `guidelines`, and current `glossary`, instructing each
    subagent to translate **3–4 batches** using `next_translation_batch` +
    `submit_translation_batch(..., return_next=True)` and stop (returning progress
    or `done=true`). Continue launching the next relay subagent until
    `done == true` so accumulated image tokens are discarded between chunks while
    the MCP server keeps `prev_context` and `glossary` intact.
  - If your host only supports a single session, continue processing batches
    sequentially in the main session, or start a fresh session and resume by
    passing the existing `job_id` to `next_translation_batch`.
- **`max_parallel_batches >= 2`** (Parallel Workers):
  - If your host supports spawning subagents, launch up to `max_parallel_batches`
    worker subagents concurrently, each with a distinct `worker_id` (e.g.,
    `"worker-1"`, `"worker-2"`), consuming the same `job_id`.
  - Every subagent must poll `next_translation_batch` with its **own** distinct
    `worker_id`, and a single worker must not poll again while it still holds a
    lease (submit the current batch or call `release_batch` first). Repeated
    polling under the same `worker_id` can hand that worker additional leases,
    and a worker juggling several batches may let an earlier lease expire and
    force rework.
  - If your host only supports a single session, process batches sequentially in
    the main session or resume with the same `job_id` in a new session.
  - Any worker that must exit early while holding a lease **must** call
    `release_batch(job_id, batch_id)` so another worker can resume immediately.

## 5. Completion & Cleanup

Once `done == true`:
- Report total, translated, skipped, and failed page counts plus `output_dir`
  (all rendered images, `manga_translator_work/json/`, and `inpainted/` sidecars
  are already saved for the desktop Editor View).
- Call `unload_models()` if the user wants GPU memory freed immediately.
