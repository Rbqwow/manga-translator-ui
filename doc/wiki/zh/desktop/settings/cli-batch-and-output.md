---
title: CLI、批量与输出
description: 说明 General 设置和 local CLI 的批量、输出、设备与错误处理边界
pageId: desktop.settings.cli-batch-and-output
lang: zh-CN
outline: [2, 4]
lastUpdated: true
---

# CLI、批量与输出

内容包括 General 设置中 CLI、批量和输出字段，以及本地 `local` CLI 的显式覆盖；不覆盖 API 凭据、检测/OCR/翻译器/排版算法。配置文件和截图均须脱敏，不展示真实 key、token、用户名、私有绝对路径、用户图片或私有提示词。

## 这组设置控制什么 {#feature-boundary}

包含日志、错误隔离、GPU/ONNX、重试、翻译上下文、批次/流水线、格式/质量/覆盖、文本/JSON/TXT、原图目录、PSD/JSX、自定义 API 参数文件和翻译后模型清理。`context_size` 实际位于 Translation 页签；特殊工作流的按钮属于翻译工作区，这里仅说明其对批量的影响。

## 在桌面端修改 {#ui-operations}

打开“设置”并选择 General；数值是输入框，布尔项是开关，`format` 是下拉框。General 的布局来自 `settings_tab_layout.json` 的 `tab_custom_1`。修改会立即更新内存并合并写盘；导入配置/切换预设可能重建行。`use_custom_api_params` 旁的 `Edit` 是打开 JSON 的文件编辑动作，不是普通配置值。

本地 CLI 的正式入口来自 `manga_translator/args.py`：`python -m manga_translator local -i <脱敏输入> [-o <脱敏输出>]`。`-i/--input` 支持多个值；`--config`、`-v/--verbose`、`--overwrite`、`--use-gpu`、`--disable-onnx-gpu`、`--format`、`--batch-size`、`--attempts` 可覆盖配置，但**未传值不覆盖**。正式顶层子命令是 `local`、`web`、`ws`、`shared`；这里按 `local`。

## 参数

> 本页各参数的界面名称、存储键与默认值的对应关系，见[设置参数索引](../../reference/settings-index.md)。

#### 详细日志 {#cli-verbose}

“详细日志”开关位于“设置 → General”。Qt UI 启动时无论开关状态都会创建 `result/log_<时间戳>.txt`；开启后控制台会显示 DEBUG 级别信息，并为每张图片创建调试目录，保存输入图、检测/OCR、修复和渲染等调试缓存/中间文件。它不改变翻译结果，但额外文件可能很多并占用大量磁盘空间，建议仅在排查问题时开启。默认值：`false`。

#### 重试次数 {#cli-attempts}

“重试次数”是整数输入框。设置翻译请求失败后的重试次数，`-1` 表示无限重试。它只作用于翻译/API 请求层，不是检测、OCR 或渲染的通用重试，也不等同于高质量重试。默认值：`5`（在首次请求之外再重试 5 次）。

#### 重试耗尽后跳过 {#cli-skip-failed-after-retries}

位于“设置 → 通用 → 重试次数”之后，默认开启。开启后，请求先完成现有重试与 API 通道轮换，仍然失败才跳过对应图片并继续；不会额外重跑整个图片流程。普通翻译、高质量翻译、并发处理和使用 API 的 OCR、上色、渲染共用此策略。重试次数设为 `-1` 时仍会无限重试，不会因为开启此开关而提前跳过。

#### 失败跳过上限 {#cli-max-failure-skips}

仅在“重试耗尽后跳过”开启时生效。默认 `5`：整个任务因重试耗尽而累计跳过第 5 张图片时停止；`0` 表示不限制跳过数量。按图片累计，成功处理不会清零，失败批次中的每张受影响图片分别计数。已有输出、无文本等正常跳过不占用此额度。上限优先于“忽略错误”，用户取消始终立即停止。

结束日志和提示仅汇总因重试耗尽而跳过的数量、文件路径和原因，不列出因已有输出而跳过的文件；达到上限或提前失败时也会附上此前因重试耗尽跳过的文件。配置键为 `cli.skip_failed_after_retries` 和 `cli.max_failure_skips`。

#### 忽略错误 {#cli-ignore-errors}

“忽略错误”开关开启后，单张图片处理失败会记录并继续处理其余图片；它不会把失败变成成功，也不会吞掉取消操作。默认值：`false`。

#### 使用 GPU {#cli-use-gpu}

“使用 GPU”开关决定模型加载与推理是否使用 GPU，需要匹配的驱动、Torch/CUDA 和显存；开启不代表每个实现都会使用 GPU。默认值：`true`。

#### 禁用 ONNX GPU 加速 {#cli-disable-onnx-gpu}

“禁用 ONNX GPU 加速”开关只强制 ONNX 会话走 CPU，不关闭 Torch 的 CUDA；可以绕开 provider 冲突，但速度会变慢。默认值：`false`。

#### 上下文页数 {#cli-context-size}

“上下文页数”位于 Translation 页签，是整数输入框。翻译时使用最近的非空历史页构建上下文，`0` 或负数关闭。详细说明见[上下文与提示词](../translator/context-and-prompts.md)。

#### 批量大小 {#cli-batch-size}

“批量大小”是整数输入框，控制每次翻译提交的图片数量、并发队列上限和内存峰值；它不等于同时运行的图片数，特殊模式可能强制为 1。默认值：`3`。

#### 并发批量处理 {#cli-batch-concurrent}

“并发批量处理”开关开启后，检测+OCR、翻译、修复和排版通过队列流水线并行，翻译阶段使用多个独立线程同时处理批次。导入 TXT、仅翻译 JSON、导出原文/翻译、仅上色/超分/修复和替换翻译等特殊工作流会强制关闭并发。

```mermaid
flowchart LR
    subgraph Off["关闭 batch_concurrent"]
        O1["图 1：全部阶段"] --> O2["图 2：全部阶段"]
    end
    subgraph On["开启 batch_concurrent"]
        A["图 A：检测 + OCR"] --> B["图 A：翻译"]
        A --> C["图 B：同时检测 + OCR"]
        B --> D["图 A：修复 + 排版"]
    end
```

等待翻译的队列最多容纳 `batch_size × translation_concurrency` 张图片，队列满时检测阶段会等待，避免无限积压。高质量翻译器同样参与该流水线，二者并非互斥。默认值：`true`。

#### 翻译并发数 {#cli-translation-concurrency}

“翻译并发数”位于“设置 → General”，控制同时执行的翻译批次数，范围 `1–32`，默认 `3`，需要开启“并发批量处理”。例如批量大小 `3`、翻译并发数 `3`，会同时处理最多三个各含三张图片的翻译请求。任一请求完成后，其线程立即领取下一批，不等待其他请求。

回复可以乱序完成，结果仍对应原来的图片。每批使用发送时已经完成的页面作为上下文：不要求这些页在当前页之前，也不要求连续，只排除正在翻译中的批次自身；需要逐批继承完整译文上下文时设为 `1`。上下文对所有翻译器生效（含高质量与 Sakura），只是渲染形式不同。请求仍遵守翻译器配置的 RPM 限制。

CLI 示例：`python -m manga_translator local -i ./manga_folder --concurrent --batch-size 3 --translation-concurrency 3`。配置文件对应 `cli.translation_concurrency`。

#### 输出格式 {#cli-format}

“输出格式”是下拉框，位于“设置 → General”，决定保存图片时使用的扩展名。

- 不指定：保留原扩展名。
- `png`：PNG 格式。
- `jpg`/`jpeg`/`jfif`：JPEG 格式（需要 RGB 转换）。
- `webp`：WebP 格式，支持质量设置。
- `avif`：AVIF 格式，需要 Pillow 编解码支持。
- `bmp`：BMP 格式（需要 RGB 转换）。
- `tiff`/`tif`：TIFF 格式。
- `heic`/`heif`：HEIF 格式，需要编解码支持。

默认值：不指定。

#### 覆盖已存在文件 {#cli-overwrite}

“覆盖已存在文件”开关关闭时，跳过已经存在的图片输出，TXT/JSON 工作流也会检查对应文件；关闭后结果包含 skipped 项。默认值：`false`。

#### 跳过无文本图像 {#cli-skip-no-text}

“跳过无文本图像”开关开启后，检测/OCR 判定没有可翻译文本的图片会被标记为跳过（`skip_reason=no_text`），仍然写出图片与空的 JSON/TXT，但不再进入翻译和渲染。它只改变统计口径与摘要，不会省掉检测与 OCR（否则无从判断是否无文本）；这是正常分支，不计入失败，也不占用 `max_failure_skips` 额度。默认值：`true`。

#### 简单排序 {#force-simple-sort}

“简单排序”开启后不做分格检测，按从上到下、从右到左的顺序直接排列文本框；它省掉每页一次的分格检测子进程，但多栏页面的阅读顺序可能不准。命令行对应 `--force-simple-sort` / `--no-force-simple-sort`。默认值：`false`。

#### 图片可编辑 {#cli-save-text}

“图片可编辑”开关开启后，会导出包含区域、原文/译文、尺寸和渲染字段的 JSON 伴随数据；与“导出原文”组合时还会导出原文 TXT。默认值：`true`。

#### 图像保存质量 {#cli-save-quality}

“图像保存质量”是整数输入框，作用于图片、修复和编辑器保存；值越高通常文件越大，具体语义由编码器决定。默认值：`100`。

#### 无损图像保存 {#cli-save-lossless}

“无损图像保存”开关对 WebP、AVIF 和 HEIC/HEIF 启用无损编码，此时“图像保存质量”被忽略。AVIF 走 pylibheif 内置的 AOM 编码器，使用全范围 4:4:4 与可逆 RGB 存储，并保留源图 ICC。输出固定为 8 位；16 位或浮点输入会先量化到 8 位再编码（与 JPEG/BMP/HEIC/WebP 的既有行为一致），不会中断整批任务。默认值：`true`。

#### 压缩努力程度 {#cli-compression-effort}

“压缩努力程度”是 `0–100` 的整数输入框，作用于 PNG 和无损 WebP/AVIF/HEIC/HEIF。AVIF 将该值映射为 AOM 编码速度（9 到 0），HEIC/HEIF 映射为 x265 预设；`0` 最快、`100` 压缩率最高也最慢。清空或输入非法时恢复默认值 `100`。

#### 输出到原图目录 {#cli-save-to-source-dir}

“输出到原图目录”开关开启后，把结果写入原图旁的 `manga_translator_work/result`；关闭时使用输出目录并尽量保留相对结构。目录必须可写。默认值：`false`。

#### 导出可编辑PSD {#cli-export-editable-psd}

“导出可编辑PSD”开关开启后，最终阶段生成 Photoshop PSD/JSX；执行脚本需要 Photoshop，不能当作普通图片格式。默认值：`false`。

#### 仅生成PSD脚本 {#cli-psd-script-only}

“仅生成PSD脚本”开关开启后，只生成 JSX 脚本而不执行 Photoshop；脚本可能包含路径，不要直接分享。默认值：`false`。

#### 使用自定义API参数 {#use-custom-api-params}

“使用自定义API参数”开关开启后，读取 `config/custom_api_params.json` 为请求附加参数；旁边的 `Edit` 按钮打开该 JSON 文件。详细说明见[自定义请求参数](../api-management/custom-request-parameters.md)。

#### 翻译完成后卸载模型 {#app-unload-models-after-translation}

“翻译完成后卸载模型”开关开启后，桌面端在任务完成后主动卸载模型，降低常驻内存，但会增加下次任务的加载时间。默认值：`true`。

该开关只在桌面端生效：CLI 与服务器没有对应的"任务结束后卸载"实现，服务端改由管理员在清理面板触发模型释放。

#### Web 端的并发 {#web-concurrency}

`cli.batch_size` 与 `cli.use_gpu` 属于服务器端控制项，被列入 `server/routes/config.py#SERVER_HIDDEN_CONFIG_KEYS`，不会出现在 Web 配置表单中；Web 的整体并发度由服务器设置 `max_concurrent_tasks` 决定。

`cli.batch_concurrent`、`cli.translation_concurrency` 与 `cli.context_size` 已对 Web 可见，并且按请求生效：服务端为每个任务创建独立的 `MangaTranslator` 实例（模型权重与空闲计时仍是进程级共享），因此并发流水线、上下文和取消状态不会在请求之间互相污染。

全局默认来自服务器配置文件 `config/config.json` 的 `cli` 段（缺失时用内置默认：并发开启、翻译并发 `3`、上下文 `3`），Web 表单也显示这组值；请求里显式携带的值会覆盖它。单任务的 `translation_concurrency` 会被 `max_concurrent_tasks` 封顶，总在途请求约等于 `max_concurrent_tasks × translation_concurrency`，显存或 API 速率紧张时可在表单里关闭或调小。

仅超分、仅上色、仅修复、导入翻译渲染与两种导出端点现在真正按各自工作流执行（不再退化为普通翻译）；这些特殊工作流仍强制串行。

## 参数如何生效 {#runtime-behavior}

UI/配置文件进入 `AppSettings`/ConfigService，再进入内存配置、核心 `Config`、`MangaTranslator` 和导出消费者。`local` 在 `mode/local.py` 只把显式 CLI 值覆盖到 `cli`；桌面 `app_logic.py` 另外构造 `save_info`（输出目录、格式、覆盖和原图目录）。

```mermaid
flowchart TD
    U["General 或 local CLI 显式参数"] --> C["AppSettings / ConfigService"] --> M["核心 Config"]
    M --> P["检测 + OCR"] --> T["翻译：context_size / attempts"] --> R["修复 + 排版"] --> E["图片、JSON/TXT、PSD/JSX"]
    B{"batch_concurrent?"} -->|否| S["batch_size 普通批次"] --> P
    B -->|是且普通模式| Q["四阶段队列与背压"] --> P
```

`batch_size` 是翻译批量和并发翻译队列上限；`batch_concurrent` 是阶段流水线并行，不是 API 并发数。导入 TXT、JSON-only、导出原文/翻译、仅上色/超分/修复和替换翻译会关闭并发，以保证顺序与逐图文件回写。`context_size` 使用最近非空历史页构建消息；`attempts`、HQ 质量重试和 API 候选轮换是不同层次；取消不属于可忽略错误。

## 搭配使用时的注意事项 {#dependencies-and-conflicts}

GPU 后端需匹配驱动、Torch/CUDA/ONNX provider。增大批次、开启并发、增加上下文或输出高质量会增加资源/token/磁盘压力；OOM 应降低批量或关闭并发。关闭覆盖会跳过图片/TXT/JSON；模板和 JSON-only 依赖同名工作目录文件。PSD 执行依赖 Photoshop，JSX/JSON/TXT 可能泄露路径和文本，外发前清理。格式编码依赖 Pillow 及平台编解码器。
