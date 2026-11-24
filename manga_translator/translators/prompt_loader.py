"""
统一的提示词文件加载器 / Unified prompt file loader

支持 YAML (.yaml/.yml) 和 JSON (.json) 格式，优先加载 YAML。
当 .yaml 和 .json 同名文件同时存在时，优先使用 .yaml。
"""

import json
import logging
import os
import threading
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger('manga_translator')
prompt_file_lock = threading.RLock()

# 缓存已加载的 yaml 模块
_yaml_module = None
_yaml_available = None


def _get_yaml():
    """延迟加载 yaml 模块，避免启动时报错"""
    global _yaml_module, _yaml_available
    if _yaml_available is None:
        try:
            import yaml
            _yaml_module = yaml
            _yaml_available = True
        except ImportError:
            _yaml_available = False
            logger.warning("PyYAML not installed. YAML prompt files will not be supported. Install with: pip install pyyaml")
    return _yaml_module if _yaml_available else None


def load_prompt_file(path: str) -> Optional[Dict[str, Any]]:
    """Load a JSON/YAML prompt without racing concurrent glossary updates."""
    # Glossary extraction can update the same prompt from several workers.
    with prompt_file_lock:
        return _load_prompt_file(path)


def _load_prompt_file(path: str) -> Optional[Dict[str, Any]]:
    """
    加载单个提示词文件（自动检测格式）

    Args:
        path: 文件路径（.yaml/.yml/.json）

    Returns:
        解析后的字典，加载失败返回 None
    """
    if not path or not os.path.exists(path):
        return None

    ext = os.path.splitext(path)[1].lower()

    try:
        with open(path, 'r', encoding='utf-8') as f:
            if ext in ('.yaml', '.yml'):
                yaml = _get_yaml()
                if yaml is None:
                    logger.error(f"Cannot load YAML file {path}: PyYAML not installed")
                    return None
                data = yaml.safe_load(f)
            elif ext == '.json':
                data = json.load(f)
            else:
                logger.warning(f"Unsupported prompt file format: {ext}")
                return None

        if not isinstance(data, dict):
            logger.warning(f"Prompt file {path} did not parse to a dict (got {type(data).__name__})")
            return None

        return data

    except Exception as e:
        logger.error(f"Failed to load prompt file {path}: {e}")
        return None


def resolve_prompt_path(base_dir: str, stem: str) -> Optional[str]:
    """
    根据文件名（不含扩展名）查找提示词文件，优先 YAML。

    查找顺序: .yaml → .yml → .json

    Args:
        base_dir: 目录路径
        stem: 文件名（不含扩展名），例如 "system_prompt_hq"

    Returns:
        找到的文件完整路径，找不到返回 None
    """
    for ext in ('.yaml', '.yml', '.json'):
        path = os.path.join(base_dir, stem + ext)
        if os.path.exists(path):
            return path
    return None


def load_prompt_by_stem(base_dir: str, stem: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    根据文件名（不含扩展名）加载提示词，优先 YAML。

    Args:
        base_dir: 目录路径
        stem: 文件名（不含扩展名）

    Returns:
        (data, path) - 加载的数据和实际文件路径，失败返回 (None, None)
    """
    path = resolve_prompt_path(base_dir, stem)
    if path is None:
        return None, None
    data = load_prompt_file(path)
    return data, path


def load_system_prompt_hq(dict_dir: str) -> str:
    """
    加载 HQ 系统提示词

    Args:
        dict_dir: dict/ 目录路径

    Returns:
        提示词文本，加载失败返回空字符串
    """
    data, path = load_prompt_by_stem(dict_dir, 'system_prompt_hq')
    if data is None:
        return ""
    prompt = data.get('system_prompt', '')
    if prompt and path:
        logger.debug(f"Loaded HQ system prompt from: {path}")
    return prompt


def load_system_prompt_hq_format(dict_dir: str, target_lang: str, extract_glossary: bool = False) -> str:
    """
    加载 HQ 输出格式提示词。

    Args:
        dict_dir: dict/ 目录路径
        target_lang: 目标语言名称
        extract_glossary: 是否要求额外输出 new_terms

    Returns:
        替换了占位符的提示词文本，加载失败返回空字符串
    """
    data, path = load_prompt_by_stem(dict_dir, 'system_prompt_hq_format')
    if data is None:
        return ""

    prompt = data.get('system_prompt_hq_format', '')
    if not prompt:
        return ""

    prompt = prompt.replace("{{{target_lang}}}", target_lang)

    if extract_glossary:
        optional_new_terms_rule = (
            '    -   The object MUST also contain a key "new_terms" which is a list of objects.\n'
            '    -   If no new terms are found, return `"new_terms": []`.\n'
            '    -   Each item in "new_terms" MUST have "original", "category", and "aliases".\n'
            '    -   "aliases" MUST be a list. Each alias MUST have "original" and "translations".\n'
            '    -   Each alias "translations" list MUST contain exactly one object with only "text".\n'
        )
        optional_new_terms_example_suffix = (
            ',\n'
            '  "new_terms": [\n'
            '    {\n'
            '      "original": "Excalibur",\n'
            '      "category": "Item",\n'
            '      "aliases": [\n'
            '        {\n'
            '          "original": "Excalibur",\n'
            '          "translations": [\n'
            f'            {{ "text": "<{target_lang} translation>" }}\n'
            '          ]\n'
            '        }\n'
            '      ]\n'
            '    }\n'
            '  ]\n'
        )
        optional_new_terms_final_instruction = ' Return "new_terms": [] when no new terms are found.'
    else:
        optional_new_terms_rule = ""
        optional_new_terms_example_suffix = ""
        optional_new_terms_final_instruction = ""

    prompt = prompt.replace("{{{optional_new_terms_rule}}}", optional_new_terms_rule)
    prompt = prompt.replace("{{{optional_new_terms_example_suffix}}}", optional_new_terms_example_suffix)
    prompt = prompt.replace("{{{optional_new_terms_final_instruction}}}", optional_new_terms_final_instruction)

    if path:
        logger.debug(f"Loaded HQ output format prompt from: {path}")
    return prompt


def load_line_break_prompt(dict_dir: str) -> Optional[Dict[str, Any]]:
    """
    加载 AI 断句提示词

    Args:
        dict_dir: dict/ 目录路径

    Returns:
        提示词字典（含 line_break_prompt 字段），加载失败返回 None
    """
    data, path = load_prompt_by_stem(dict_dir, 'system_prompt_line_break')
    if data and path:
        logger.debug(f"Loaded line break prompt from: {path}")
    return data


def load_glossary_extraction_prompt(dict_dir: str, target_lang: str) -> str:
    """
    加载术语提取提示词

    Args:
        dict_dir: dict/ 目录路径
        target_lang: 目标语言名称

    Returns:
        替换了占位符的提示词文本，加载失败返回空字符串
    """
    data, path = load_prompt_by_stem(dict_dir, 'glossary_extraction_prompt')
    if data is None:
        return ""
    prompt = data.get('glossary_extraction_prompt', '')
    if prompt:
        prompt = prompt.replace("{{{target_lang}}}", target_lang)
        if path:
            logger.debug(f"Loaded glossary extraction prompt from: {path}")
    return prompt


def load_glossary_output_format_prompt(dict_dir: str, target_lang: str) -> str:
    """
    兼容旧调用：加载开启术语提取时的 HQ 输出格式提示词。

    Args:
        dict_dir: dict/ 目录路径
        target_lang: 目标语言名称

    Returns:
        替换了占位符的提示词文本，加载失败返回空字符串
    """
    return load_system_prompt_hq_format(dict_dir, target_lang, extract_glossary=True)


def load_custom_prompt(path: str) -> Optional[Dict[str, Any]]:
    """
    加载用户自定义提示词文件（支持 .yaml/.yml/.json）

    如果传入的路径不存在，会尝试替换扩展名再找一次。
    例如传入 xxx.json 不存在，会尝试 xxx.yaml。

    Args:
        path: 用户指定的提示词文件路径

    Returns:
        解析后的字典，加载失败返回 None
    """
    if not path:
        return None

    # 直接路径存在
    if os.path.exists(path):
        return load_prompt_file(path)

    # 尝试替换扩展名
    base, ext = os.path.splitext(path)
    alt_exts = ['.yaml', '.yml', '.json']
    for alt_ext in alt_exts:
        if alt_ext != ext:
            alt_path = base + alt_ext
            if os.path.exists(alt_path):
                logger.info(f"Prompt file {path} not found, using {alt_path} instead")
                return load_prompt_file(alt_path)

    logger.warning(f"Custom prompt file not found: {path}")
    return None


def list_prompt_files(dict_dir: str, exclude_system: bool = True) -> list:
    """
    列出 dict/ 目录下的提示词文件

    Args:
        dict_dir: dict/ 目录路径
        exclude_system: 是否排除系统提示词文件

    Returns:
        文件名列表
    """
    system_stems = {
        'system_prompt_hq',
        'system_prompt_hq_format',
        'system_prompt_line_break',
        'glossary_extraction_prompt'
    }
    prompt_exts = {'.json', '.yaml', '.yml'}

    if not os.path.exists(dict_dir):
        return []

    files = []
    for f in os.listdir(dict_dir):
        ext = os.path.splitext(f)[1].lower()
        if ext not in prompt_exts:
            continue
        if exclude_system:
            stem = os.path.splitext(f)[0]
            if stem in system_stems:
                continue
        files.append(f)

    return sorted(files)


BUILTIN_TRANSLATION_PROMPTS: Tuple[Dict[str, Optional[str]], ...] = (
    {
        "stem": "system_prompt_hq",
        "field_key": "system_prompt",
        "default_filename": "system_prompt_hq.yaml",
        "config_key": "translator.use_builtin_system_prompt",
    },
    {
        "stem": "system_prompt_line_break",
        "field_key": "line_break_prompt",
        "default_filename": "system_prompt_line_break.yaml",
        "config_key": "render.disable_auto_wrap",
    },
    {
        "stem": "glossary_extraction_prompt",
        "field_key": "glossary_extraction_prompt",
        "default_filename": "glossary_extraction_prompt.yaml",
        "config_key": "translator.extract_glossary",
    },
    {
        "stem": "system_prompt_hq_format",
        "field_key": "system_prompt_hq_format",
        "default_filename": "system_prompt_hq_format.yaml",
        "config_key": None,
    },
)

BUILTIN_TRANSLATION_PROMPT_MAP: Dict[str, Dict[str, Optional[str]]] = {
    spec["stem"]: spec for spec in BUILTIN_TRANSLATION_PROMPTS if spec.get("stem")
}


def resolve_builtin_prompt_path(dict_dir: str, stem: str) -> str:
    """Resolve an existing built-in prompt file (.yaml/.yml/.json) or default .yaml path."""
    found = resolve_prompt_path(dict_dir, stem)
    if found:
        return os.path.normpath(found)
    return os.path.normpath(os.path.join(dict_dir, f"{stem}.yaml"))


def get_builtin_prompt_spec_by_path(file_path: Optional[str]) -> Optional[Dict[str, Optional[str]]]:
    """Return built-in translation prompt metadata if file_path matches a built-in stem."""
    if not file_path:
        return None
    stem = os.path.splitext(os.path.basename(file_path))[0]
    return BUILTIN_TRANSLATION_PROMPT_MAP.get(stem)


def load_builtin_prompt_text(path: str, field_key: str) -> str:
    """Load raw text for a built-in translation prompt file without expanding placeholders."""
    data = load_prompt_file(path)
    if isinstance(data, dict):
        value = data.get(field_key, "")
        if isinstance(value, str):
            return value
    return ""


def save_builtin_prompt_text(path: str, field_key: str, prompt_text: str) -> str:
    """Persist a built-in translation prompt file while preserving formatting and placeholders."""
    resolved_path = os.path.normpath(path)
    normalized = str(prompt_text or "").replace("\r\n", "\n").replace("\r", "\n")
    ext = os.path.splitext(resolved_path)[1].lower()

    with prompt_file_lock:
        parent = os.path.dirname(resolved_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        if ext == ".json":
            payload = {field_key: normalized}
            content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        else:
            if normalized == "":
                content = f'{field_key}: ""\n'
            elif normalized.endswith("\n\n"):
                lines = normalized[:-1].split("\n")
                yaml_lines = [f"{field_key}: |+2"]
                yaml_lines.extend(f"  {line}" if line else "" for line in lines)
                content = "\n".join(yaml_lines) + "\n"
            elif normalized.endswith("\n"):
                lines = normalized[:-1].split("\n")
                yaml_lines = [f"{field_key}: |2"]
                yaml_lines.extend(f"  {line}" if line else "" for line in lines)
                content = "\n".join(yaml_lines) + "\n"
            else:
                lines = normalized.split("\n")
                yaml_lines = [f"{field_key}: |-2"]
                yaml_lines.extend(f"  {line}" if line else "" for line in lines)
                content = "\n".join(yaml_lines) + "\n"

            yaml = _get_yaml()
            if yaml is not None:
                parsed = yaml.safe_load(content)
                if not isinstance(parsed, dict) or field_key not in parsed:
                    raise ValueError(f"Serialized YAML for built-in prompt '{field_key}' is invalid")

        with open(resolved_path, "w", encoding="utf-8") as f:
            f.write(content)

    return resolved_path


def ensure_builtin_prompt_file(path: str, field_key: str, default_text: str = "") -> str:
    """Create a built-in translation prompt file with default_text if missing."""
    resolved_path = os.path.normpath(path)
    if not os.path.exists(resolved_path):
        save_builtin_prompt_text(resolved_path, field_key, default_text)
    return resolved_path
