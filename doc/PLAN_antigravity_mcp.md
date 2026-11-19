# Manga Translator UI 接入 Antigravity MCP 改造方案（Plan）

- **适用环境**：本地源码 `.venv` + 桌面 PyQt6 GUI（不涉及 PyInstaller 打包适配，不做独立无头 CLI 入口）
- **目标 Harness**：Google Antigravity（通过本地 Streamable HTTP 接入）
- **核心目标**：0 大模型 API Key、0 套餐反代风险、保留完整自动翻译流水线与桌面工程兼容性

---

## 一、背景、目标与非目标

### 1.1 目标

通过 **控制反转（Inversion of Control / Pull–Translate–Push）**，把所有大模型推理（翻译、看图纠错、AI `[BR]` 断句、术语表提取）交回 Antigravity 用户套餐的原生会话；`manga-translator-ui` 自身只作为**纯本地 CV / GPU / Qt 渲染与工程管理服务**运行在常规桌面 GUI 进程中：

1. **0 外部大模型 API**：不填写也不读取任何 OpenAI / Gemini API Key，不走任何反代，从根源消除套餐封号/反代风险。
2. **保留完整自动翻译流水线**：继续沿用原项目检测、OCR、文本行合并、分格排序、前后字典、过滤列表、`keep_lang`、蒙版细化、LaMa/局部修复、Qt 离屏渲染与 `balloon_fill` / HanLP 语义断行、跨页上下文、AI `[BR]` 断句、术语表 YAML 自动增量合并、译后质检与重修（`pending_redo`）、断点续传。
3. **双翻译模式 + 按需看图开关**：同时支持 **HQ 多模态看图** 与 **非 HQ 纯文本**，并在非 HQ 纯文本模式下提供独立的 **允许按需看图** 开关（可按页或局部裁剪拉取高清编号框图）。
4. **保留桌面 GUI 形态**：在现有 `FluentWindow` 主窗口内新增专属“MCP 服务”页面，内嵌配置开关、Antigravity 一键接入、任务进度条与实时日志面板；原有翻译设置、预设、提示词管理、术语表、文件列表与可视化编辑器全部保留互通。

### 1.2 非目标

- **不**通过 MCP 暴露应用设置 CRUD、`.env` 管理或可视化编辑器（画笔/调框/富文本）接口。
- **不**开发独立无头 CLI `mcp` 子命令或 `stdio` 传输模式。
- **不**修改 `packaging/` 做 PyInstaller 打包适配。
- **不**实现旧版 HTTP+SSE（`/sse`）传输（Antigravity 仅支持 `stdio` 与 Streamable HTTP，不支持 legacy HTTP+SSE）。

---

## 二、代码库现况审计：本地算力 vs 外部 API 边界

经排查，整条漫画翻译流水线绝大多数阶段都已有纯本地模型，真正依赖外部 API 的仅集中在 OCR、上色、翻译与渲染的 API 变体：

| 流水线阶段 | 纯本地后端（无 API） | 原 API 后端 | 源码位置 | MCP 0-API 策略 |
|---|---|---|---|---|
| **超分** `upscale` | `mangajanai` / `realcugan` / `esrgan` / `waifu2x` / `4xultrasharp` | 无 | [config.py](../manga_translator/config.py#L147-L153) | 100% 本地执行 |
| **上色** `colorizer` | `none` / `mc2` | `openai_colorizer` / `gemini_colorizer` | [config.py](../manga_translator/config.py#L107-L112), [model_api_colorizer.py](../manga_translator/colorization/model_api_colorizer.py#L49-L195) | 仅允许 `none` / `mc2`；禁用 API 图生图上色 |
| **检测** `detector` | `default`(DBNet) / `dbconvnext` / `ctd` / `craft` + `yolo_obb` + `mangalens` | 无 | [config.py](../manga_translator/config.py#L90-L97) | 100% 本地执行 |
| **OCR** `ocr` | `48px` / `48px_ctc` / `32px` / `mocr` / `paddleocr*` / `paddleocr_vl` / `hayai_ocr_v2` | `openai_ocr` / `gemini_ocr` | [config.py](../manga_translator/config.py#L113-L126), [model_api_ocr.py](../manga_translator/ocr/model_api_ocr.py#L40-L196) | 强制使用本地 OCR；由 HQ 看图或按需看图交由 Antigravity 纠错 |
| **合并/排序/过滤** | `textline_merge` + 分格排序 + `pre_dict` + `filter_list` + `keep_lang` | 无 | [manga_translator.py](../manga_translator/manga_translator.py#L5031-L5049) | 100% 本地执行 |
| **翻译 + 断句 + 术语表** | `sakura`(本地) / `none` / `original` | `openai` / `openai_hq` / `gemini` / `gemini_hq` | [config.py](../manga_translator/config.py#L127-L135), [openai_hq.py](../manga_translator/translators/openai_hq.py#L56-L499) | **核心接管目标：全部交给 Antigravity**（不初始化任何外部翻译客户端） |
| **蒙版细化** `mask_refinement` | 本地几何与模型气泡交集裁剪 | 无 | [manga_translator.py](../manga_translator/manga_translator.py#L3364-L3382) | 100% 本地执行 |
| **修复** `inpainter` | `default` / `lama_large` / `lama_mpe` / `flux2-klein` / `sd` / `original` / `none` | 无 | [config.py](../manga_translator/config.py#L98-L106) | 100% 本地执行 |
| **渲染** `render` | `default`（Qt 离屏渲染 + `balloon_fill` + HanLP `semantic_linebreak` + 替换规则/富文本） | `openai_renderer` / `gemini_renderer` | [config.py](../manga_translator/config.py#L59-L64), [model_api_renderer.py](../manga_translator/rendering/model_api_renderer.py#L50-L178) | 强制使用 `default` Qt 离屏渲染；禁用 API 图生图渲染 |

### 方案取舍记录

1. **为什么选“GUI 内嵌 Streamable HTTP + Pull–Translate–Push 控制反转”**：
   - Antigravity 原生支持 `"url": "http://127.0.0.1:<port>/mcp"` 的 Streamable HTTP 接入方式。
   - **彻底避开 `stdio` 污染风险**：[log.py](../manga_translator/utils/log.py#L57-L60) 默认把 `StreamHandler` 绑在 `sys.stdout` 并每次强制 `flush()`，且渲染等模块使用了 `tqdm` 与第三方库输出；无头 `stdio` 极易损坏 JSON-RPC 帧。
   - **彻底避开 Windows PyTorch / PyQt6 DLL 加载冲突与无头 Qt 渲染坑**：[main.py](../desktop_qt_ui/main.py#L66-L78) 已正确引导 `torch` DLL 与 `QApplication`；线程局部字体状态（[_fonts.py](../manga_translator/rendering/text_render/_fonts.py#L35-L102)）可直接复用。
   - **单进程热模型共享**：即便 Antigravity 拉起多个子 Agent 分块接力或并发翻，都连同一个本地端口、共享一份已加载的 GPU 模型，不会因启动多个进程导致爆显存。
   - **设置与编辑器无缝协同**：在现有 GUI 配好预设/术语表/字体后可直接跑 MCP；翻完后因同步落盘 `manga_translator_work/json/` 与 `inpainted/`，可直接在同窗口“编辑器视图”精修单气泡。
   - **注（启动顺序依赖）**：Streamable HTTP 需要先打开桌面 GUI 并开启 MCP 服务，Antigravity 才能发现工具；通过在 MCP 页面提供持久化的“随软件自启”开关缓解。
2. **为什么放弃 MCP Sampling（`sampling/createMessage`）**：
   - 主流 Coding Harness 对 `sampling/createMessage` 支持普遍不完整，且常伴随每次弹窗人工确认、多模态图像支持受限、单次工具调用超时（几十页漫画极易超时），以及 [ConcurrentPipeline._run_async_in_thread](../manga_translator/utils/concurrent_pipeline.py#L291-L322) 多线程独立事件循环跨线程桥接 MCP Session 极易死锁。
3. **为什么放弃直接套壳 `template+save_text` + `load_text`**：
   - [manga_translator.py](../manga_translator/manga_translator.py#L4045-L4047) 中 `load_text` 写死基于原图加载，丢失超分与上色底图；
   - [manga_translator.py](../manga_translator/manga_translator.py#L1206) 与 [manga_translator.py](../manga_translator/manga_translator.py#L1412) 直接耦合 `desktop_qt_ui.services.workflow_service`；
   - `load_text` 跳过 [manga_translator.py](../manga_translator/manga_translator.py#L5510-L5674) 译后处理（括号/引号、OpenCC、`post_dict`、幻觉重试、句号清理）、[manga_translator.py](../manga_translator/manga_translator.py#L5322-L5349) 译文过滤、`_validate_br_markers`、术语表提取与 HQ 编号框图生成；
   - 必须全部先导出再全部渲染，无法实现 GPU 与 LLM 的流水线并行重叠。

---

## 三、总体架构

```text
┌─────────────────────────────────────────────────────────────────────────┐
│ Google Antigravity (用户套餐原生会话 / Subagent 分块接力或并发 + Skill)    │
│  - 承担全部 LLM/VLM 推理：翻译、看图纠错、AI [BR] 断句、术语表提取          │
│  - 零外部 API Key / 零反代                                               │
└───────────────────────────────┬─────────────────────────────────────────┘
                                │ Streamable HTTP (http://127.0.0.1:8765/mcp)
┌───────────────────────────────▼─────────────────────────────────────────┐
│ Manga Translator UI (PyQt6 桌面主进程)                                   │
│                                                                         │
│  [GUI 主线程]                                                            │
│   ├─ 侧边栏新增 "MCP 服务" 页面 (启停/自启/模式/投递/进度/实时日志/一键接入) │
│   ├─ 复用设置、预设、提示词、术语表、主页文件列表、编辑器与状态栏            │
│   └─ MainWindow.closeEvent 活跃任务确认与优雅停服                         │
│                                                                         │
│  [MCP HTTP 服务线程 (uvicorn + FastMCP, SelectorEventLoop, log_config=None)] │
│   └─ 暴露 6~8 个精简工具，路由至 McpJobManager，通过 pyqtSignal 回推 UI     │
│                                                                         │
│  [单 GPU 优先队列线程 GpuPriorityWorker (CUDA/ONNX 串行，最低显存峰值)]    │
│   ├─ P0: 滑窗快满/收尾时优先修图 & 译文过滤触发的 pending_redo 重修         │
│   ├─ P1: READY 批次不足 max_parallel_batches 时优先做下一批检测+OCR+合并    │
│   ├─ P2: 已领出/已提交批次的常规 Mask Refinement + Inpainting             │
│   └─ P3: 滑窗有余量时的远端批次预取                                        │
│                                                                         │
│  [独立 CPU 编码/渲染区]                                                   │
│   ├─ 轻 CPU 预览池：绘制编号框图 draw_text_boxes_on_image + JPEG/WebP 压缩  │
│   └─ 单 RenderWorker 线程：Qt QPainter 离屏渲染、_revert_upscale、写盘、清理 │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 四、核心配置模型、模式状态机与 0-API 硬隔离

### 4.1 持久化配置 `McpSettings`

在 [config_models.py](../desktop_qt_ui/core/config_models.py#L284-L303) 的 `AppSettings` 中新增 `mcp: McpSettings = Field(default_factory=McpSettings)`（兼容缺少 `mcp` 键的现有 `config/config.json`）：

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `auto_start` | `bool` | `False` | 持久化开关：打开 GUI 时是否自动启动 MCP HTTP 服务 |
| `host` | `str` | `"127.0.0.1"` | 固定本机回环监听地址，避免局域网暴露 |
| `port` | `int` | `8765` | 本地 Streamable HTTP 监听端口 |
| `translation_mode` | `Literal["hq", "text"]` | `"hq"` | `"hq"`（HQ 多模态看图）或 `"text"`（非 HQ 纯文本） |
| `allow_on_demand_image` | `bool` | `True` | 在 `"text"` 模式下是否允许 Antigravity 按需拉取整页/局部预览图 |
| `image_delivery` | `Literal["auto", "mcp_image", "file_path"]` | `"auto"` | 图片投递方式：`auto` 优先内联，超预算自动回退文件路径 |
| `inline_image_budget_mb` | `float` | `2.0` | `auto` 模式下单批内联图片总编码体积上限（MB） |
| `preview_max_size` | `int` | `1024` | 编号框预览图长边上限像素 |
| `preview_format` | `Literal["jpeg", "webp", "png"]` | `"jpeg"` | 预览图压缩格式（JPEG quality=82 比 PNG 省 8~10 倍体积） |
| `preview_quality` | `int` | `82` | 有损格式压缩质量（1~100） |
| `max_parallel_batches` | `int` | `1` | 最大并行翻译批次数（范围 `1~4`；`1` 为串行连贯，≥2 为多子 Agent 并发） |
| `lease_timeout_seconds` | `int` | `180` | 批次租约超时秒数，超时自动释放回 `READY` 队列 |
| `models_idle_ttl` | `int` | `300` | MCP 任务空闲后自动卸载 GPU 模型的秒数（0 表示仅按任务结束开关清理） |
| `use_gui_files_by_default` | `bool` | `True` | `create_translation_job` 未传 `inputs` 时是否默认消费 GUI 主页文件列表 |

### 4.2 三档翻译与看图模式状态机

为防止“HQ 模式”与“按需看图开关”语义打架，交互与后端统一收口为 3 档明确行为：

1. **`HQ 多模态`（`translation_mode == "hq"`）**：
   - 每批自动附带所有页的编号框整页图；
   - UI 上“允许按需看图”开关自动置灰并注明“HQ 已默认每页看图”；
   - 仍允许 Antigravity 在小字模糊时调用 `get_page_preview(..., region_ids=..., high_res=True)` 拉取更高清局部放大图。
2. **`非 HQ 纯文本 + 允许按需看图`（`translation_mode == "text"` 且 `allow_on_demand_image == True`，日常主力推荐）**：
   - `next_translation_batch` 仅返回纯文本、方向、行/区域数、OCR 置信度 `prob` 及 `low_confidence_ids` 提示；
   - 遇到低置信度/乱码/拟声词/缺主语或说话人歧义时，Antigravity 主动调 `get_page_preview` 仅查看该页或指定气泡裁剪图。
3. **`非 HQ 纯文本 + 严格纯文本`（`translation_mode == "text"` 且 `allow_on_demand_image == False`）**：
   - 批次不传图片，且 `get_page_preview` 直接返回权限拒绝，做到最省 Token 与最快吞吐。

### 4.3 图片投递主通道与自动 Fallback

- 预览图不再沿用 [openai_hq.py](../manga_translator/translators/openai_hq.py#L37-L51) 的无损 PNG，默认改为 **JPEG / WebP（`max_size=1024, quality=82`）**，单页压至 ~80–150KB。
- **`auto`（默认）**：当 `batch_size` 正常（如 1~5 页）且单批总编码体积 ≤ `inline_image_budget_mb`（默认 `2.0 MB`）时，直接以标准 MCP `ImageContent` 内联返回；一旦某批图片体积超预算，自动落盘至 `manga_translator_work/mcp_preview/<job_id>/` 并返回 `file_path` + 提示调用 `read_file`。
- **`mcp_image` / `file_path`**：支持在 MCP 页面手工强制锁定任一通道（如遇到特定模型版本对内联 `ImageContent` 处理异常，切 `file_path` 立即兜底）。

### 4.4 0-API 硬隔离

- MCP 任务构建的 `Config` 快照**永不初始化任何外部翻译器客户端**，不调用 `prepare_translation`，不读取 `.env` 中的任何 API Key。
- 建任务前 & MCP 页面实时检查以下 4 个字段：`ocr.ocr`、`ocr.secondary_ocr`、`render.renderer`、`colorizer.colorizer`。
  - 只要任意一项被设为 `openai_*` 或 `gemini_*`，MCP 页面顶部亮出**黄色警告条**（附“跳转到设置”按钮），且 `create_translation_job` **直接拒绝执行**并返回需要切回本地模型的明确错误提示，物理杜绝意外调用外部 API。

---

## 五、MCP 工具集与协议契约

暴露 8 个职责单一的 MCP 工具（不暴露设置与编辑器接口；若需进一步精简上下文，可将 `get_job_status` 与 `cancel_job` 合并为 `manage_job`）：

| 工具名 | 核心参数 | 返回内容与关键行为 |
|---|---|---|
| `get_workspace_info()` | 无 | 只读摘要：当前预设、目标语言、`translation_mode`、`allow_on_demand_image`、`image_delivery`、`max_parallel_batches`、`batch_size`、`context_size`、AI `[BR]` 断句与术语表开关、GUI 文件列表数量/样例、输出目录、当前占用状态与 API 后端冲突告警。 |
| `create_translation_job(...)` | `inputs?`, `output_dir?`, `use_gui_files?`, `translation_mode?`, `allow_on_demand_image?`, `target_lang?`, `batch_size?`, `context_size?`, `overwrite?`, `retranslate_json?` | 检查 GPU 互斥与 0-API 约束；展开文件/目录/压缩包并执行 `plan_batch_inputs` 跳过已翻译页；加载断句提示词与自定义 HQ 提示词（复用 `_load_and_prepare_prompts` 与 `_build_system_prompt`）；启动后台队列并立即返回 `job_id`、总/待翻/跳过页数、`effective_mode`、`max_parallel_batches` 以及 **本任务一次性 `guidelines` 与完整术语表**（后续批次不重复发送）。 |
| `next_translation_batch(job_id, worker_id?)` | `job_id`, `worker_id?` | 从 `READY` 队列原子领取下一批（受 `max_parallel_batches` 与租约保护）。返回 `done`、`batch_id`、`lease_expires_in`、`progress`、`prev_context`、`glossary_delta`、`pages: [{page_index, filename, width, height, regions: [{id, text, original_region_count, direction, prob}], low_confidence_ids}]`，以及在 `hq` 模式下附加编号框预览图或路径。 |
| `get_page_preview(job_id, page_index, region_ids?, high_res?)` | `job_id`, `page_index`, `region_ids?`, `high_res?` | 仅当 `hq` 或 `text + allow_on_demand_image=True` 时放行。返回整页或按 `region_ids` 外扩裁剪的高清局部编号框图（`high_res=True` 放宽尺寸上限），同时自动为所在批次续期租约。 |
| `submit_translation_batch(job_id, batch_id, translations, new_terms?, return_next=True)` | `job_id`, `batch_id`, `translations: [{id, translation}]`, `new_terms?`, `return_next?` | 1) 校验 ID 完整、`[BR]` 断句（`_validate_br_markers`）、重复幻觉与目标语言比例；不通过则不落盘，直接返回 `{status: "retry_needed", errors: [{id, reason, original_region_count?}]}`。<br>2) 通过后执行 `_apply_post_translation_processing`、译文过滤、加锁合并 `new_terms` 到术语表 YAML、写入乱序安全历史。<br>3) 有过滤区域则提交 `P0 pending_redo` 重修，否则交付 `RenderWorker` 异步渲染落盘。<br>4) `return_next=True` 时不等待本批写盘完成，直接附带下一批（减少一半工具往返）；全本结束准备返回 `done=True` 时等待 `RenderWorker` 落盘收尾并汇报汇总。 |
| `release_batch(job_id, batch_id)` | `job_id`, `batch_id` | 子 Agent 中途失败或主动退出时归还租约，批次立即回到 `READY` 状态供接力。 |
| `get_job_status(job_id?)` | `job_id?` | 返回当前/指定任务进度、各阶段队列长度、租约状态、失败页明细与已产出路径。 |
| `cancel_job(job_id?)` | `job_id?` | 触发取消标志，安全排空/中止 GPU 与渲染队列、清理临时预览图、归还互斥锁。 |
| `unload_models()` | 无 | 检查无活跃任务后调用 [full_memory_cleanup](../desktop_qt_ui/utils/memory_cleanup.py#L58-L80) 立即释放 GPU 显存与物理内存。 |

---

## 六、批量与三层并行详细实现

### 6.1 第 1 层：批量切分与断点续传（`batch_size`）

1. **统一文件与压缩包展开**：复用 `FileService`、`archive_extractor` 与 `file_to_folder_map`，无论来自 Antigravity `inputs` 还是 GUI 文件列表，保持一致的相对输出目录结构。
2. **计划跳过与切批**：
   - 调用 [batch_skip.py](../manga_translator/utils/batch_skip.py#L197-L287) 的 `plan_batch_inputs` 与 `slice_batch_indices` 跳过已有输出页（`overwrite=False`）并在跳过页边界切分批次。
   - **源码修补点**：[batch_skip.py](../manga_translator/utils/batch_skip.py#L108-L122) 中 `context_eligible` 硬编码检查 `translator_type in {openai, gemini, openai_hq, gemini_hq}`，必须增加对 MCP 模式标记（如 `getattr(translator, "_mcp_context_enabled", False)`）的支持，否则断点续传无法从跳过页的 JSON 加载上下文，也不会在跳过页位置正确切批。
3. **空文本页自动旁路**：预处理后 `text_regions` 为空的纯插画/扉页直接在后台按 `skip_no_text` 策略完成或跳过，不消耗翻译配额；批打包器继续抓取后续有字页凑满 `batch_size`。
4. **连续全局编号**：参照 [manga_translator.py](../manga_translator/manga_translator.py#L6168-L6187)，批内多页共用连续气泡 ID `1..N`，与图像绘制的边框编号一一对应。

### 6.2 第 2 层：阶段级流水线并行（对应 `cli.batch_concurrent`）

#### 为什么采用“单 GPU 优先队列 + 单 Qt 渲染线程”而非原版双 GPU 线程？

原版 [ConcurrentPipeline](../manga_translator/utils/concurrent_pipeline.py#L67-L71) 同时运行 `Detection+OCR` 线程和 `Inpainting` 线程。但在单张显卡上，两个大模型同时前向传播并不能翻倍算力，反而会使两组中间激活张量同时驻留显存；在 Windows 下容易触发显存溢出到系统共享内存，导致速度暴跌。由于 MCP 模式下 Antigravity 每批推理需要 10~25 秒，这期间 GPU 完全空闲，**单 GPU 串行优先队列**能把全部检测/OCR/修图工作藏进 LLM 思考间隙，在最低显存峰值下跑满吞吐。

#### 关键路径优先的 GPU 调度策略（已修正优先级顺序）

| 优先级 | 任务 | 触发条件 | 设计原因 |
|---|---|---|---|
| **P0** | 已提交批次的修图 & `pending_redo` 重修 | 已提交待落盘页接近滑窗上限，或全部待翻页已预处理完成 | 尽快推进渲染以释放滑窗内存槽位，避免背压死锁并加速尾部收官 |
| **P1** | 下一批 **预处理（上色/超分/检测/OCR/合并）** | `READY` 队列中可领批次数 `< max_parallel_batches`（至少保持 1 批备货） | **关键路径优先**：确保 `submit(..., return_next=True)` 零等待拿到下一批，且多子 Agent 并发时瞬间全部领到任务，不因先修上一批的图而让大模型空等 |
| **P2** | 已领出 / 已提交批次的首次 **Mask Refinement + Inpainting** | `READY` 队列备货充足 | 充分利用 Antigravity 推理间隙把当前批和前批的图修好 |
| **P3** | 远端后续批次预取 | 滑窗信号量仍有余量 | 进一步平滑遇到大图/复杂页时的耗时抖动 |

#### 异步渲染、CPU 剥离与有界滑窗

1. **预览图绘制与压缩移出 GPU 线程**：`draw_text_boxes_on_image` 与 JPEG/WebP 压缩是纯 CPU 操作，在 `READY` 入队或按需 `get_page_preview` 时于轻 CPU 线程池完成，不占用 GPU 线程时间。
2. **独立单线程 `RenderWorker`**：
   - 固定单线程运行，保证 [_fonts.py](../manga_translator/rendering/text_render/_fonts.py#L35-L102) 线程局部字体缓存与离屏 Qt 渲染稳定；
   - 复用 [concurrent_pipeline.py](../manga_translator/utils/concurrent_pipeline.py#L730-L760) 的 `pending_redo` 逻辑（译文过滤掉区域后清空 `ctx.mask` 走 `P0` 重修）；
   - 渲染前备份 `inpainted_snapshot = np.copy(ctx.img_inpainted)`（[concurrent_pipeline.py](../manga_translator/utils/concurrent_pipeline.py#L1074-L1082)）供写入编辑器可用修补图；
   - 渲染后补齐 `await translator._revert_upscale(config, ctx)`（[manga_translator.py](../manga_translator/manga_translator.py#L5798)）确保超分还原尺寸正确；
   - 同步写入 `manga_translator_work/json/` 与 `inpainted/`，随后立即释放像素缓冲并归还滑窗槽位；
   - `submit_translation_batch(..., return_next=True)` **不等待磁盘编码**，直接返回下一批；仅最后一批提交后等待 `RenderWorker` 全部写盘落毕。
3. **有界背压**：复刻 [concurrent_pipeline.py](../manga_translator/utils/concurrent_pipeline.py#L80-L81) 的 `BoundedSemaphore(max_inflight_pages)`，`max_inflight_pages = batch_size * (max_parallel_batches + prefetch_batches + 1)`；当设置关闭 `batch_concurrent=False`（低内存模式）时，`prefetch_batches = 0` 且不做提前预修图。

#### 速度影响评估结论

稳态下每批耗时为 $\max(T_{\text{gpu}},\; T_{\text{llm}} / K,\; T_{\text{render}})$：
- **常规配置**：3 页一批 $T_{\text{gpu}} \approx 3\sim9\text{s}$，远小于单批 LLM 推理时间 $10\sim25\text{s}$，GPU 耗时完全被隐藏，**对总耗时影响 ≈ 0%**。
- **重 GPU 配置**（超分 / `mc2` / `paddleocr_vl` / `flux2-klein`）：单卡本来就无法双线程并行，单 GPU 线程避免显存溢出到共享内存，往往反而更快更稳。
- **纯 CPU 编码（如无损 WebP `compression_effort=100`）**：已由 `RenderWorker` 异步消化，不阻塞下一批翻译请求。

### 6.3 第 3 层：多批次翻译并行（`max_parallel_batches`，`1~4`）

1. **服务端租约状态机**：
   - 每个批次状态流转：`PENDING` → `PREPROCESSING` → `READY` → `LEASED(worker_id, leased_at)` → `COMMITTED` → `DONE` / `FAILED`。
   - `_batch_lock` 原子领批并控制同时 `LEASED` 数 ≤ `max_parallel_batches`；若超过 `lease_timeout_seconds`（默认 180s）未提交且未调用 `get_page_preview` 续期，自动回收回 `READY` 防止子 Agent 异常退出导致任务卡死。
2. **乱序安全上下文历史**：
   - 复刻 [concurrent_pipeline.py](../manga_translator/utils/concurrent_pipeline.py#L568-L595) 的 `_available_history` 设计，用 `dict[page_order, entries]` 按页序保存历史（包含 `resume_pages`）；任何批次领取时排除自身页码，取已完成的按页序排序后最近 `context_size` 页。
3. **术语表并发写入锁**：用进程级 `threading.Lock` 保护 [common.py](../manga_translator/translators/common.py#L3426) 的 `merge_glossary_to_file`。
4. **编排模式选择**：
   - **`max_parallel_batches = 1`（默认推荐）**：LLM 串行、GPU/渲染仍全程并行。剧情、人称、术语表前后承接最连贯；长篇（>12 页）靠 Antigravity Subagent **串行接力**（每子 Agent 处理 3~4 批后退出），既不爆上下文又不断剧情。
   - **`max_parallel_batches = 2~3`（极速并发模式）**：Skill 拉起多个 Subagent 同时领不同批并行翻译，适合短篇合集、单元剧或追求极限速度场景（需注意套餐短时限流压力）。

---

## 七、GUI 专属 “MCP 服务” 页面与现有界面协同

### 7.1 导航注册

- 在 [main_window.py](../desktop_qt_ui/ui/main_window.py#L139-L191) 的 `_register_main_interfaces` 与 [main_window.py](../desktop_qt_ui/ui/main_window.py#L877-L894) 的 `_refresh_navigation_texts` 新增 `"mcp"` 侧边栏条目（`FIF.ROBOT` 或 `FIF.DEVELOPER_TOOLS`）。
- 在 [view.py](../desktop_qt_ui/ui/main_page/view.py#L151-L170) 创建并托管 `self.mcp_page`，并在 `apply_fluent_theme` 同步刷新主题。

### 7.2 页面卡片布局（4 张 `CardWidget`）

1. **服务控制与接入卡片**：
   - 运行状态指示 + 本地端点 URL（`http://127.0.0.1:8765/mcp`）
   - 端口输入框 + **启动 / 停止服务**主按钮 + **随软件启动自动开启 MCP 服务**持久化 `ToggleSwitch`
   - 快捷接入按钮：`复制 MCP 配置`、`一键写入 Antigravity 配置`（安全合并 `~/.gemini/config/mcp_config.json`，保留用户原有其他 Server）、`安装/更新 Antigravity Skill`（写入 `~/.gemini/config/skills/manga-translate/SKILL.md`）
   - 底部动态黄色警告条：检测设置页是否存在 `openai_*` / `gemini_*` 后端，并提供“跳转到设置”按钮。
2. **MCP 翻译模式与策略卡片**：
   - 翻译模式：`HQ 多模态（每页看图）` / `非 HQ 纯文本`
   - `允许按需看图` `ToggleSwitch`（HQ 模式下自动置灰并展示提示文案；非 HQ 模式下生效）
   - 图片投递方式：`自动 (优先内联, 超限回退路径)` / `强制内联 (ImageContent)` / `本地临时路径 (read_file)`
   - 预览图参数（长边像素 `768 / 1024 / 1280`、格式 `JPEG / WebP / PNG`、质量）、最大并行翻译批次数（`1~4`）、模型空闲卸载 TTL（秒）、`默认使用主页文件列表`开关（全部即时持久化到 `config/config.json`）。
3. **当前任务与进度卡片**（样式对齐 [view.py](../desktop_qt_ui/ui/main_page/view.py#L188-L214)）：
   - 显示 Job ID、生效模式、按需看图触发次数、当前状态文案、计数与百分比 `ProgressBar`
   - 操作按钮：`停止 MCP 任务`、`打开输出目录`、`立即卸载显存`
4. **实时日志卡片**：
   - 顶部工具栏：级别筛选（`全部 / 仅 MCP / 警告及以上`）、`自动滚动`、`清空`、`打开日志目录`
   - 只读等宽字体日志文本框（上限 1000 行自动裁剪）；在 [log_service.py](../desktop_qt_ui/services/log_service.py#L26-L45) 的 `RecentLogHandler` 增加线程安全订阅回调，通过 `Qt.ConnectionType.QueuedConnection` 推至 UI，同时保持控制台和 `result/log_*.txt` 完整输出。

### 7.3 协同规则与关窗保护

- **GPU 互斥**：与 [app_logic.py](../desktop_qt_ui/app_logic.py#L1898-L1966) `start_backend_task` 双向检查；一方占卡时另一方给出明确提示并拒绝并发启动。
- **绝不弹模态弹窗**：MCP 任务报错/跳过/完成一律走结构化 MCP 响应 + 日志 + MCP 页面状态栏，绝不弹 `QMessageBox` 阻塞无人值守流水线。
- **完成后同步快照**：任务结束向主线程发信号触发 `MainWindow._request_main_file_snapshot()`，使编辑器与文件列表即时可见最新结果。
- **优雅退出**：在 [main_window.py](../desktop_qt_ui/ui/main_window.py#L895-L932) 的 `closeEvent` 增加 MCP 活跃任务检测，存在进行中任务时弹出确认框；确认退出时先取消 MCP 任务、通知 `uvicorn.Server.should_exit = True` 并 join 服务线程，再释放服务容器。

---

## 八、Antigravity 专属集成配置与 Skill 设计

### 8.1 `mcp_config.json` 配置（`~/.gemini/config/mcp_config.json`）

```json
{
  "mcpServers": {
    "manga-translator": {
      "url": "http://127.0.0.1:8765/mcp"
    }
  }
}
```

### 8.2 配套 Skill 设计（`~/.gemini/config/skills/manga-translate/SKILL.md`）

仓库内置模板 `manga_translator/mcp/antigravity_skill_template.md`，支持一键安装。Skill 规范以下行为：

1. **自动闭环**：收到翻译请求后，先调用 `get_workspace_info` 确认状态，再 `create_translation_job` → `next_translation_batch` → 严格遵循 `guidelines` 与术语表输出译文 → `submit_translation_batch(..., return_next=True)` 循环至 `done == true`，中途不反复打断用户确认。
2. **三档模式工作流**：
   - `hq`：逐页核对带彩色编号框的图与 OCR 原文，纠正错字并结合画面神态/动作翻译；
   - `text + allow_on_demand_image=True`：默认按文本与上文翻译；遇到 `low_confidence_ids`、乱码、拟声词、缺主语或对话归属不明时，调用 `get_page_preview(job_id, page_index, region_ids=...)` 看图后再翻；
   - `text + allow_on_demand_image=False`：纯文本批量直翻，不调用看图工具。
3. **局部重试自愈**：若 `submit_translation_batch` 返回 `retry_needed`，仅针对 `errors` 中列出的 `id`（如缺 `[BR]` 标记或复读幻觉）修正并立即重提。
4. **长漫画分块与并行调度**：
   - 待翻页 ≤ 12 页：当前对话直接完成；
   - 待翻页 > 12 页且 `max_parallel_batches == 1`：主 Agent 建好 `job_id` 后，每轮拉起一个干净的 Subagent 处理 3~4 批后退出并汇报进度，由主 Agent 接力直至 `done == true`，把前面几十页的图片 Token 彻底卸掉（服务端持久持有跨页上下文与术语表）；
   - `max_parallel_batches >= 2`：主 Agent 同时拉起最多 `max_parallel_batches` 个 Subagent 并行消费同一 `job_id`；Subagent 异常退出前务必调 `release_batch`。
5. **收尾**：任务结束后汇报成功/跳过/失败页统计与输出路径，按需调用 `unload_models`。

---

## 九、改动文件清单与分阶段施工计划

### A. 新增文件清单

| 文件路径 | 职责说明 |
|---|---|
| `manga_translator/mcp/__init__.py` | 包导出 |
| `manga_translator/mcp/protocol_models.py` | Pydantic 工具入参/出参模型 |
| `manga_translator/mcp/batch_adapter.py` | 单批预处理、编号框图绘制与 JPEG/WebP 压缩、局部裁剪、校验、后处理、过滤、术语合并、Qt 渲染落盘与 `_revert_upscale` 适配 |
| `manga_translator/mcp/job_manager.py` | `McpTranslationJob` / `McpJobManager`、`GpuPriorityWorker`、`RenderWorker`、租约管理、乱序安全上下文、有界滑窗背压与空闲 TTL 释放 |
| `manga_translator/mcp/server_app.py` | 基于 `FastMCP` 注册工具与 Streamable HTTP Starlette App；`uvicorn` 后台线程生命周期控制器 |
| `manga_translator/mcp/antigravity_skill_template.md` | `manga-translate` Skill 模板 |
| `desktop_qt_ui/services/mcp_service.py` | Qt 服务封装：桥接 `ConfigService` / `MainAppLogic` / `McpJobManager`、`pyqtSignal` UI 通知、0-API 检查、一键写 Antigravity 配置与 Skill |
| `desktop_qt_ui/ui/main_page/pages/mcp_page.py` | MCP 专属 Fluent 页面 UI 与实时日志面板 |
| `test/test_mcp_pipeline.py` | 状态机、校验重提、三档看图模式、`auto` 回退、0-API 拦截、`pending_redo`、术语锁与租约回收单测 |
| `test/test_mcp_server.py` | Streamable HTTP 端到端握手、`tools/list`、`tools/call` 与启停测试 |

### B. 现有文件最小改动清单

| 文件 | 改动位置与内容 |
|---|---|
| [pyproject.toml](../pyproject.toml#L16-L98) | 添加官方 `mcp` Python SDK 依赖（并校验与 `starlette==1.3.1` / `pydantic==2.11.7` / `anyio` 兼容性） |
| [manga_translator.py](../manga_translator/manga_translator.py#L5322-L5349) | 从 `_batch_translate_contexts` 抽离 `_filter_translated_regions(self, ctx, config)` 供 `batch_adapter` 复用 |
| [batch_skip.py](../manga_translator/utils/batch_skip.py#L108-L122) | 为 MCP 模式放开 `_skip_decision` 中 `context_eligible` 判定 |
| [config_models.py](../desktop_qt_ui/core/config_models.py#L284-L303) | 新增 `McpSettings` 并挂入 `AppSettings.mcp` |
| [log_service.py](../desktop_qt_ui/services/log_service.py#L26-L45) | 在 `RecentLogHandler` 增加线程安全订阅回调，供 MCP 页面实时日志框使用 |
| [services/__init__.py](../desktop_qt_ui/services/__init__.py#L92-L160) | 初始化注册 `mcp` 服务及其 `shutdown` 钩子 |
| [app_logic.py](../desktop_qt_ui/app_logic.py#L1898-L1966) | 增加与 MCP 任务的双向 GPU 互斥锁；提供线程安全的文件/压缩包扫描快照接口 |
| [view.py](../desktop_qt_ui/ui/main_page/view.py#L151-L170) | 创建 `mcp_page` 并接入主题切换刷新 |
| [main_window.py](../desktop_qt_ui/ui/main_window.py#L139-L191) | 注册侧边栏 `"mcp"` 导航、i18n 刷新与 [closeEvent](../desktop_qt_ui/ui/main_window.py#L895-L932) 活跃任务确认及停服 |
| `desktop_qt_ui/locales/{zh_CN,en_US}.json` | 补齐 MCP 页面的中英文国际化词条 |

### C. 分阶段施工顺序

1. **Phase 1 — 依赖验证与核心底层抽离**：`uv` 引入 `mcp` SDK 验证无冲突；修改 `batch_skip.py` 的 `context_eligible` 与抽离 `manga_translator.py` 的 `_filter_translated_regions`；在 `config_models.py` 加入 `McpSettings`。
2. **Phase 2 — MCP 流水线适配器与双后台队列引擎**：实现 `protocol_models.py`、`batch_adapter.py` 与 `job_manager.py`（先用 Mock 跑通三档模式、`pending_redo`、租约超时回收与乱序上下文测试）。
3. **Phase 3 — Streamable HTTP 服务端与 Antigravity Skill**：实现 `server_app.py`（`FastMCP` + `uvicorn`）、`antigravity_skill_template.md` 与 `test_mcp_server.py` 端到端协议测试。
4. **Phase 4 — Qt 服务层与专属 MCP UI 页面**：扩展 `log_service.py`、`app_logic.py` 互斥/快照、`services/__init__.py`、实现 `mcp_service.py`、`mcp_page.py`、接入 `view.py` / `main_window.py` 与 locales 文案。
5. **Phase 5 — Antigravity 真机联调与全链路验收**：实际通过 GUI 一键配置 Antigravity 并完成三档模式漫画测试。

---

## 十、关键工程暗坑汇总与验收清单

### 10.1 关键暗坑防线

1. **依赖兼容核查**：`pyproject.toml` 锁死了 `starlette==1.3.1`、`pydantic==2.11.7`、`uvicorn==0.35.0`；引入 `mcp` SDK 时需第一时间验证 `uv lock` 是否与现有依赖树（尤其 `starlette` / `anyio`）冲突，必要时选定兼容的 `mcp` 版本区间。
2. **`uvicorn` 日志劫持防护**：启动内嵌 `uvicorn.Config` 必须传入 `log_config=None`、`access_log=False`（或只走现有 `logging` root），并在后台线程使用 `SelectorEventLoop`（Windows），严禁覆盖 GUI 的队列日志与文件日志。
3. **跨线程读取 UI 状态**：`MainAppLogic.source_files`、`excluded_*` 等在 GUI 线程可变；MCP 收到 `create_translation_job` 时须经 `Qt` 线程安全通道拷贝**不可变快照**，而回写进度/状态/快照刷新必须全部通过 `pyqtSignal`（`QueuedConnection`）。
4. **Qt 渲染线程亲和性**：`RenderWorker` 固定使用单一常驻工作线程，确保 [_fonts.py](../manga_translator/rendering/text_render/_fonts.py#L35-L102) 的 `_thread_state` 缓存稳定且不触发跨线程 Qt 对象销毁异常。
5. **编辑器可用性与超分还原**：渲染前必须先保存 `inpainted_snapshot` 写 `inpainted/`；渲染后必须执行 `_revert_upscale`；`pending_redo` 重修前必须显式置 `ctx.mask = None`。
6. **临时预览图清理**：`file_path` / `auto` 回退写入 `manga_translator_work/mcp_preview/` 的临时图，在对应批次落盘成功、任务取消或关闭时及时清理，避免工作目录堆积垃圾文件。

### 10.2 测试与验收清单

- **依赖与基础回归**：`uv sync` 成功；原桌面 GUI 普通翻译与编辑器功能完全不受影响。
- **自动化测试（`pytest`）**：
  - `test_mcp_pipeline.py`：Mock 本地检测/OCR/修图/渲染，覆盖 `create -> next -> retry_needed -> submit(return_next=True) -> done`、三档看图模式、`auto` 超体积自动回退 `file_path`、API 后端拦截、`pending_redo`、`merge_glossary_to_file` 并发锁、租约超时回收、`resume_pages` 断点上下文加载。
  - `test_mcp_server.py`：使用官方 `mcp` Streamable HTTP client 完成握手、`tools/list`、`tools/call` 与启停线程无泄漏验证。
- **Antigravity 真机联调**：
  - GUI 启动 → MCP 页面开启服务（验证持久化自启开关重开软件有效）→ 一键写入 `mcp_config.json` 与 Skill → Antigravity 刷新确认工具上线；
  - 实测一组含多行气泡 `[BR]`、拟声词、纯图空白页、断点续传的漫画，分别跑通 **HQ 多模态**、**非 HQ + 按需看图**、**非 HQ 严格纯文本**、以及 **长篇子 Agent 分块**：
    - 验证成品图渲染/修图质量与原流水线一致；
    - 验证 `manga_translator_work/json/` 与 `inpainted/` 可在同窗口编辑器打开精修；
    - 验证进度条、实时日志、GPU 互斥、关窗确认与 `unload_models` 正常工作；
    - 全过程 0 外部大模型 API 调用。
