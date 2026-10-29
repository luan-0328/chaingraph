"""文档切分：把知识文档切成可检索的小块。

为什么按 Markdown 标题切，而不是定长硬切？
    知识库是"规则手册"体例（## 小节 = 一条规则）。按标题切能保证
    **一块 = 一条完整规则**，检索命中后直接喂给模型就是原文，不会把
    一条规则从中间截断。
    小节太长时再按段落/句子二次切分，并保留少量重叠（overlap）避免丢上下文。
"""

import re

DEFAULT_MAX_CHARS = 600
DEFAULT_OVERLAP = 60
MIN_CHARS = 40          # 太短的块没有信息量，跟相邻块合并


def split_markdown(
    content: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap: int = DEFAULT_OVERLAP,
) -> list[str]:
    """把 Markdown 文本切成块列表。"""
    sections = _split_by_heading(content)

    chunks: list[str] = []
    for section in sections:
        if len(section) <= max_chars:
            chunks.append(section)
            continue
        chunks.extend(_split_long_section(section, max_chars=max_chars, overlap=overlap))

    return _merge_short_chunks(chunks)


def _split_by_heading(content: str) -> list[str]:
    """按 Markdown 标题切成小节（标题跟着它下面的正文一起走）。"""
    lines = (content or "").splitlines()
    sections: list[str] = []
    buffer: list[str] = []

    for line in lines:
        if re.match(r"^#{1,3}\s+", line) and buffer:
            sections.append("\n".join(buffer).strip())
            buffer = [line]
        else:
            buffer.append(line)

    if buffer:
        sections.append("\n".join(buffer).strip())

    return [section for section in sections if section]


def _split_long_section(section: str, *, max_chars: int, overlap: int) -> list[str]:
    """长小节：先按空行切段落，段落还长就按句子切，最后按长度窗口兜底。"""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", section) if p.strip()]

    pieces: list[str] = []
    for paragraph in paragraphs:
        if len(paragraph) <= max_chars:
            pieces.append(paragraph)
            continue
        # 按中文句号/问号/分号断句
        sentences = re.split(r"(?<=[。！？；])", paragraph)
        current = ""
        for sentence in sentences:
            if len(current) + len(sentence) <= max_chars:
                current += sentence
            else:
                if current:
                    pieces.append(current)
                current = sentence
        if current:
            pieces.append(current)

    # 兜底：仍然超长的，按窗口 + 重叠硬切
    result: list[str] = []
    for piece in pieces:
        if len(piece) <= max_chars:
            result.append(piece)
            continue
        start = 0
        while start < len(piece):
            result.append(piece[start : start + max_chars])
            start += max_chars - overlap

    return result


def _merge_short_chunks(chunks: list[str], *, min_chars: int = MIN_CHARS) -> list[str]:
    """过短的块与相邻块合并（例如只有一个标题的行）。"""
    merged: list[str] = []
    for chunk in chunks:
        if merged and len(chunk) < min_chars:
            merged[-1] = f"{merged[-1]}\n\n{chunk}"
        else:
            merged.append(chunk)
    return merged
