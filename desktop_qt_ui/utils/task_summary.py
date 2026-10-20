def build_skip_summary(items, translate):
    """Report only failures skipped after exhausted retries at task completion."""
    items = [item for item in items if item.get("skip_reason") == "retry_exhausted"]
    if not items:
        return ""
    lines = [translate(
        "task_skipped_summary",
        count=len(items),
    )]
    for item in items:
        path = item.get("original_path") or "Unknown"
        reason = " ".join(str(item.get("skip_message") or "").split())
        lines.append(f"• {path}: {reason}")
    return "\n".join(lines)
