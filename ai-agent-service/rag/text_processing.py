import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


INTERNAL_NOTE_PATTERNS = [
    re.compile(r"^\s*(内部备注|审批备注|仅内部|internal note|approval note)[:：].*$", re.IGNORECASE),
    re.compile(r"^\s*<!--.*?-->\s*$"),
]


@dataclass
class KnowledgeChunk:
    """清洗拆分后的知识片段，携带检索和风控所需 metadata。"""

    doc_name: str
    version: str
    paragraph: str
    collection: str
    business_scope: str
    heading_path: list[str] = field(default_factory=list)
    status: str = "PUBLISHED"
    risk_level: str = "low"
    answerable_intents: list[str] = field(default_factory=list)
    source_type: str = "official_policy"
    effective_time: datetime | None = None
    expire_time: datetime | None = None
    embedding_provider: str | None = None
    embedding_model: str | None = None
    embedding_dimension: int | None = None
    embedding_distance: str | None = None
    embedding_version: str | None = None
    chunk_index: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


def clean_knowledge_text(text: str) -> str:
    """清洗知识库原文，去除内部备注、页眉页脚和重复空白。"""
    text = _strip_metadata_block(text)
    lines: list[str] = []
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            lines.append("")
            continue
        if _is_noise_line(line):
            continue
        lines.append(re.sub(r"[ \t]+", " ", line))
    cleaned = "\n".join(lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


def _strip_metadata_block(text: str) -> str:
    """移除文档顶部 metadata 块，避免元数据被切成可引用正文。"""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    if not lines:
        return normalized
    start_index = 1 if lines[0].strip().startswith("# ") else 0
    while start_index < len(lines) and lines[start_index].strip() == "":
        start_index += 1
    if start_index >= len(lines) or lines[start_index].strip().lower() != "metadata:":
        return normalized
    end_index = start_index + 1
    while end_index < len(lines):
        stripped = lines[end_index].strip()
        if stripped.startswith("#"):
            break
        if stripped == "":
            end_index += 1
            break
        end_index += 1
    return "\n".join(lines[:1] + lines[end_index:] if lines[0].strip().startswith("# ") else lines[end_index:])


def split_into_chunks(
    text: str,
    *,
    doc_name: str,
    version: str,
    collection: str,
    business_scope: str,
    status: str = "PUBLISHED",
    risk_level: str = "low",
    answerable_intents: list[str] | None = None,
    source_type: str = "official_policy",
    max_chars: int = 800,
    overlap_chars: int = 80,
) -> list[KnowledgeChunk]:
    """按 Markdown 标题和语义段落拆分知识片段，保留标题路径。"""
    cleaned = clean_knowledge_text(text)
    heading_path: list[str] = []
    sections: list[tuple[list[str], str]] = []
    buffer: list[str] = []

    for line in cleaned.split("\n"):
        heading = re.match(r"^(#{1,6})\s+(.+)$", line)
        if heading:
            if buffer:
                sections.append((list(heading_path), "\n".join(buffer).strip()))
                buffer = []
            level = len(heading.group(1))
            heading_path = heading_path[: level - 1] + [heading.group(2).strip()]
            continue
        buffer.append(line)
    if buffer:
        sections.append((list(heading_path), "\n".join(buffer).strip()))

    chunks: list[KnowledgeChunk] = []
    for section_heading, section_text in sections:
        for paragraph in _semantic_windows(section_text, max_chars=max_chars, overlap_chars=overlap_chars):
            if not paragraph:
                continue
            chunks.append(
                KnowledgeChunk(
                    doc_name=doc_name,
                    version=version,
                    paragraph=paragraph,
                    collection=collection,
                    business_scope=business_scope,
                    heading_path=section_heading,
                    status=status,
                    risk_level=risk_level,
                    answerable_intents=answerable_intents or [],
                    source_type=source_type,
                    chunk_index=len(chunks),
                )
            )
    return chunks


def _semantic_windows(text: str, *, max_chars: int, overlap_chars: int) -> list[str]:
    """优先按空行和句末拆窗口，避免固定字符硬切。"""
    paragraphs = [item.strip() for item in re.split(r"\n\s*\n", text) if item.strip()]
    windows: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if len(paragraph) > max_chars:
            if current:
                windows.append(current.strip())
                current = ""
            windows.extend(_split_long_paragraph(paragraph, max_chars=max_chars, overlap_chars=overlap_chars))
            continue
        candidate = f"{current}\n\n{paragraph}".strip() if current else paragraph
        if len(candidate) <= max_chars:
            current = candidate
        else:
            windows.append(current.strip())
            current = _with_overlap(current, overlap_chars, paragraph)
    if current:
        windows.append(current.strip())
    return windows


def _split_long_paragraph(paragraph: str, *, max_chars: int, overlap_chars: int) -> list[str]:
    """长段落按中文句号等标点切分，保留少量 overlap。"""
    sentences = [item for item in re.split(r"(?<=[。！？；.!?;])", paragraph) if item.strip()]
    windows: list[str] = []
    current = ""
    for sentence in sentences:
        candidate = f"{current}{sentence}".strip()
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                windows.append(current)
            current = _with_overlap(current, overlap_chars, sentence)
    if current:
        windows.append(current)
    return windows


def _with_overlap(previous: str, overlap_chars: int, next_text: str) -> str:
    """给新窗口补一点上文，保持跨段语义连续。"""
    overlap = previous[-overlap_chars:] if previous and overlap_chars > 0 else ""
    return f"{overlap}{next_text}".strip()


def _is_noise_line(line: str) -> bool:
    """识别不应进入知识库的噪声行。"""
    if any(pattern.match(line) for pattern in INTERNAL_NOTE_PATTERNS):
        return True
    if re.match(r"^第\s*\d+\s*页\s*/\s*共\s*\d+\s*页$", line):
        return True
    if re.match(r"^-{3,}$", line):
        return True
    return False
