"""
文件内容嗅探（magic bytes）：防伪装扩展名绕过类型白名单。

- 白名单校验的是"声称的类型"（后缀）
- 此处校验"实际内容"与后缀一致，二者不符则拒绝
- 文本类（md/txt）不做字节嗅探（允许任意 UTF-8 文本），但与二进制魔数冲突则拒绝
"""
import logging

logger = logging.getLogger("rag.magic")

# (magic_bytes, 说明) —— bytes 为前缀匹配
_MAGIC = {
    ".pdf": [(b"%PDF", "PDF 文档")],
    ".png": [(b"\x89PNG\r\n\x1a\n", "PNG 图片")],
    ".jpg": [(b"\xff\xd8\xff", "JPEG 图片")],
    ".jpeg": [(b"\xff\xd8\xff", "JPEG 图片")],
    ".docx": [(b"PK\x03\x04", "DOCX (zip 容器)")],
    ".webp": [(b"RIFF", "WebP 图片")],   # 实际需 +8 偏移 'WEBP'，此处宽松
}
# 二进制魔数集合：与文本类冲突检测用
_BINARY_SIGS = [b"\x89PNG", b"%PDF", b"\xff\xd8\xff", b"PK\x03\x04", b"MZ", b"\x7fELF"]


def assert_content_type(head: bytes, suffix: str, filename: str = ""):
    """校验文件头与声称类型一致；不一致抛 ValueError。

    head: 文件前 32 字节（最少应有）。
    """
    if not head:
        raise ValueError("文件内容为空")
    if suffix in (".md", ".txt"):
        # 文本类：与已知二进制魔数冲突则拒绝
        for sig in _BINARY_SIGS:
            if head.startswith(sig):
                raise ValueError(f"文件内容与文本类型不匹配（检测到二进制签名 {sig[:4]!r}），疑似伪装扩展名")
        return
    expected = _MAGIC.get(suffix)
    if expected is None:
        return
    for sig, label in expected:
        if head.startswith(sig):
            return
    raise ValueError(f"文件内容与扩展名({suffix})不匹配，疑似伪装文件"
                     + (f"（{filename}）" if filename else ""))