"""Bounded operational download diagnostics; never inputs to verdict/action logic."""

DOWNLOAD_ERROR_LABELS = {
    "quota_exceeded": "媒体容量不足，请扩容或核对保留策略",
    "unsafe_url": "媒体地址被安全检查拒绝",
    "timeout": "媒体下载超时",
    "network_error": "媒体连接或服务器响应失败",
    "size_exceeded": "单个媒体文件超过大小上限",
    "storage_error": "媒体文件写入失败，请检查磁盘与权限",
    "missing_url": "消息没有可用的媒体下载地址",
    "download_failed": "媒体下载失败，具体原因未确认",
}


def download_error_code(reason: str) -> str:
    if reason.startswith("磁盘配额"):
        return "quota_exceeded"
    if reason.startswith("URL被SSRF"):
        return "unsafe_url"
    if reason == "下载总时长超时" or reason.startswith("下载失败:") and reason.endswith("Timeout"):
        return "timeout"
    if reason.startswith("下载失败:"):
        return "network_error"
    if reason.startswith("文件超过"):
        return "size_exceeded"
    if reason.startswith("写入失败:"):
        return "storage_error"
    if reason == "无URL":
        return "missing_url"
    return "download_failed"


def safe_download_errors(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [code for code in value[:100] if isinstance(code, str) and code in DOWNLOAD_ERROR_LABELS]
