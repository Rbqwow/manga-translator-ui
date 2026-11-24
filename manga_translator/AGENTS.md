# manga_translator/AGENTS.md — 核心引擎、服务端与 MCP 子系统指南

适用于对 `manga_translator/` 下核心翻译流水线、CLI 模式、FastAPI Web 服务端与 MCP 服务的改动。全局规则见根目录 `AGENTS.md`。

## 模块划分
- **入口与配置**：`__main__.py`、`args.py`、`config.py`（`Config` 及全部阶段枚举）、`manga_translator.py`（`MangaTranslator` 主编排）、`image_formats.py`、`runtime_paths.py` / `runtime_files.py`。
- **处理阶段**：`detection/`、`ocr/`、`textline_merge/`、`translators/`、`mask_refinement/`、`inpainting/`、`rendering/`、`upscaling/`、`colorization/`。
- **运行模式与并发**：`mode/`（`local.py`, `subprocess_manager.py`, `ws.py`, `share.py`）、`utils/concurrent_pipeline.py`、`utils/failure_policy.py`、`utils/batch_skip.py`、`utils/curl_cffi_transport.py`、`utils/system_proxy.py`。
- **Web 服务端 (`server/`)**：FastAPI 主入口 `server/main.py`，核心逻辑在 `server/core/`（含 `task_manager.py` 的 `_ModelUsageGate`、`logging_manager.py`、`download_ticket_service.py`、`config_manager.py`、`permission_service_v2.py`、`api_key_policy.py`），路由在 `server/routes/`，管理后台 JS 在 `server/static/js/admin/`。
- **本地 0-API MCP (`mcp/`)**：`__init__.py`（惰性 `__getattr__`）、`batch_adapter.py`、`job_manager.py`、`protocol_models.py`、`server_app.py`（FastMCP `/mcp`）、`skill_template.md`。

## 子系统核心不变量
1. **Windows Torch/Qt 初始化顺序**：`__main__.py` 与 `server/main.py` 必须在任何触发 `rendering/text_render/`（依赖 PyQt6）的导入之前先 `import torch`（避免 `c10.dll` WinError 1114）。
2. **惰性加载与轻量导入边界**：`manga_translator/mcp/__init__.py`、`batch_adapter.py`、`job_manager.py` 顶层不得直接导入 `cv2`、`numpy`、`PIL`、`torch`、`mcp`、`uvicorn`、`starlette`；`ocr/` 与 `colorization/` 工厂函数维持按需导入实现类与模型缓存。
3. **模型卸载与内存上界**：
   - 模型释放统一经 `utils/model_cleanup.py:cleanup_model_caches*`（检查卸载钩子签名并包含 `utils/mangalens_detector.py:_default_detector`；`translator_cache` 刻意不纳入 `MODEL_CACHES`）。
   - Web 端调用 `task_manager.reset_global_translator()` 必须经 `_ModelUsageGate` 排他锁，并发翻译进行中返回 409/busy 而非在任务中途卸载权重。
   - `ConcurrentPipeline` 依赖 `self._page_slots = BoundedSemaphore(...)` 限制在途页面数；`utils/panel/worker.py` 在独立一次性子进程中执行分格检测并带超时强杀。
4. **MCP 安全与 0-API 策略**：`server_app.py` 必须保持 `TransportSecuritySettings(enable_dns_rebinding_protection=True)` 与仅回环 `allowed_hosts`/`allowed_origins`；`batch_adapter.py:find_api_backend_conflicts()` 必须在 0-API 作业启动前拦截启用的外部 API OCR/渲染/上色后端。
5. **富文本与编码边界**：遵循 `rendering/RICH_TEXT_RENDERING.md`；图片跨 Unicode 路径写入走 `utils/generic.py:imwrite_unicode`；libcurl 证书/文件边界走 `utils/curl_cffi_transport.py:_encode_curl_file_path`（Windows ACP）；无控制台进程容忍 `sys.stdout is None`。
6. **服务端全局 Logger 副作用（测试常见坑）**：`server/core/logging_manager.py` 会将 `logging.getLogger('manga_translator').propagate` 设为 `False` 并挂载 `WebLogHandler`。测试若触发导入 `server.main` 且后续断言根 logger 捕获 `manga_translator.*` 日志，需注意隔离恢复该 `propagate` 属性。
7. **Web 服务端隔离与权限**：
   - `server/request_extraction.py` 按请求构造独立 `MangaTranslator` 实例（仅共享模型缓存），并发请求间不得共享可变策略/上下文；`to_pil_image` 必须拒绝远程 HTTP/HTTPS URL 防 SSRF（HTTP 422）。
   - 3 层权限继承 `global -> group -> user` 与 API Key 策略由 `permission_service_v2.py`、`api_key_policy.py` 强制执行；下载票据保持 5 分钟 TTL + `resolve_path_within()` 防路径穿越。
   - 批量 ZIP 跳过单页编码异常（全页失败仍抛错）；图片编码脱离 FastAPI 事件循环；跳过页与无文本页序列化（`to_json.py`）保持可区分。
8. **翻译器、提示词、术语表与传输**：
   - 自定义 Prompt 优先级高于内置与断行 Prompt；启用 `use_builtin_system_prompt=False` 但无有效自定义指令时记录告警并回退内置；MCP 与 API 提示词及 `extract_glossary` 策略保持一致。
   - 术语表统一 `aliases[].translations[]` delta 协议且严禁覆写 `overwrite: false`；多 Key 8 组 30 槽位原子重排；API 状态指纹（`make_endpoint_status_key`）不得包含明文密钥。
   - `curl_cffi` 固定 `trust_env=False`（每请求经 `system_proxy.py` 解析代理）、本地端点禁用 `impersonate="chrome"`、构建传输前按 `latin-1` 校验 API Key、不覆写浏览器伪装请求头。
9. **图像格式与无损导出**：`manga_translator/image_formats.py` 为扩展名/格式单一事实源；无损 AVIF 走 `pylibheif` AOM（`chroma=444`、`lossless-alpha=True`、显式 16-bit/float 转 8-bit、EXIF 282/283/296 保存 DPI、缺编码器时不覆盖目标文件）；无损 WebP 严禁组合 `method=6` 与 `quality=100` 且保留透明像素下 RGB。
10. **排版与 CV 管线**：横排行距与竖排 TCY 高度不计入描边宽度；字号偏移/缩放/上下限/固定字号仅在排版与避让后应用一次；波斯语/阿拉伯语保留逻辑顺序及 ZWNJ（`U+200C`）/ZWJ（`U+200D`）；气泡蒙版随当前原图传递（禁用全局 `id(img)` 缓存）；分块修复中 `mask_raw` 留给纯色气泡采样而 `mask` 用于修复；ONNX CUDA 提供程序不传默认 `device_id`；MangaLens 长图模式限制单全图蒙版内存并惰性导入 YOLO；超分子进程失败须在 `finally` 清理临时目录。

## 聚焦验证入口
- 并发与隔离、MCP、Web 服务端、富文本与渲染、翻译器/代理/术语表、无损导出与 CV 管线的聚焦测试命令统一维护在 `doc/harness/testing.md` 第 3 节。
