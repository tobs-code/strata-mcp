# -*- coding: utf-8 -*-
"""
Markdown chunking with overlapping windows for document ingestion.

Features:
- Overlapping chunk windows with configurable size/overlap
- Heading hierarchy propagation as context prefix
- Code block, table, and HTML block protection (never split)
- Image reference stripping / alt-text extraction
- YAML front matter parsing
- Sentence-splitting fallback for paragraphs exceeding chunk_size
- Token-count-based chunking via tiktoken
"""

import re
from typing import Any, Dict, List, Optional, Tuple

_IMAGE_PATTERN = re.compile(r'!\[([^\]]*)\]\([^)]*\)')
_FENCED_CODE_PATTERN = re.compile(r'```[\s\S]*?```')
_TABLE_PATTERN = re.compile(r'^\|.+\|\s*$', re.MULTILINE)
_HTML_BLOCK_PATTERN = re.compile(r'<(pre|table|div|blockquote|details)[\s>][\s\S]*?</\1>', re.IGNORECASE)
_HEADING_PATTERN = re.compile(r'^(#{1,6})\s+(.+)$', re.MULTILINE)
_FRONT_MATTER_PATTERN = re.compile(r'^---\s*\n(.*?)\n---', re.DOTALL)


def _find_protected_blocks(text: str) -> List[Tuple[int, int, str]]:
    blocks = []
    for m in _FENCED_CODE_PATTERN.finditer(text):
        blocks.append((m.start(), m.end(), "code"))
    for m in _HTML_BLOCK_PATTERN.finditer(text):
        start = m.start()
        end = m.end()
        if not any(s <= start < e for s, e, _ in blocks):
            blocks.append((start, end, "html"))
    table_starts = [(m.start(), m.end()) for m in _TABLE_PATTERN.finditer(text)]
    if table_starts:
        merged = [list(table_starts[0])]
        for s, e in table_starts[1:]:
            if s - merged[-1][1] <= 1:
                merged[-1][1] = e
            else:
                merged.append([s, e])
        for s, e in merged:
            if not any(bs <= s < be for bs, be, _ in blocks):
                blocks.append((s, e, "table"))
    blocks.sort(key=lambda x: x[0])
    return blocks


def _in_protected(pos: int, blocks: List[Tuple[int, int, str]]) -> bool:
    return any(s <= pos < e for s, e, _ in blocks)


def _strip_images(text: str) -> Tuple[str, List[Dict[str, Any]]]:
    images = []
    def repl(m):
        alt = m.group(1).strip()
        url_end = m.group(0).rfind(')')
        url = m.group(0)[m.group(0).find('(')+1:url_end]
        images.append({"alt": alt, "url": url})
        return alt if alt else ""
    return _IMAGE_PATTERN.sub(repl, text), images


def _parse_front_matter(text: str) -> Tuple[Optional[Dict[str, Any]], str]:
    m = _FRONT_MATTER_PATTERN.match(text)
    if not m:
        return None, text
    raw = m.group(1)
    rest = text[m.end():].strip()
    try:
        import yaml
        data = yaml.safe_load(raw)
        if isinstance(data, dict):
            return data, rest
    except Exception:
        pass
    lines = {}
    for line in raw.strip().split('\n'):
        if ':' in line:
            k, _, v = line.partition(':')
            lines[k.strip()] = v.strip()
    return lines if lines else None, rest


def _extract_headings(text: str) -> List[Tuple[int, int, str]]:
    headings = []
    for m in _HEADING_PATTERN.finditer(text):
        level = len(m.group(1))
        headings.append((m.start(), level, m.group(2).strip()))
    return headings


def _heading_context(pos: int, headings: List[Tuple[int, int, str]]) -> str:
    active = []
    for hpos, level, title in headings:
        if hpos > pos:
            break
        active = [t for t in active if t[0] < level]
        active.append((level, title))
    if not active:
        return ""
    return " > ".join(t for _, t in active)


def _split_sentences(text: str, base_start: int) -> List[Dict[str, Any]]:
    raw_sentences = re.split(r'(?<=[.!?])\s+', text)
    sentences = []
    pos = 0
    for s in raw_sentences:
        s = s.strip()
        if not s:
            pos += len(s) + 1
            continue
        actual_start = text.find(s, pos)
        if actual_start == -1:
            actual_start = pos
        end = actual_start + len(s)
        sentences.append({
            "text": s,
            "start": base_start + actual_start,
            "end": base_start + end,
            "protected": False,
        })
        pos = end
    return sentences


def _split_into_segments(text: str, blocks: List[Tuple[int, int, str]]) -> List[Dict[str, Any]]:
    paragraphs = re.split(r'\n\s*\n', text)
    segments = []
    i = 0
    for para in paragraphs:
        para = para.strip()
        if not para:
            i += 2
            continue
        start = text.find(para, i)
        if start == -1:
            start = i
        end = start + len(para)
        is_protected = _in_protected(start, blocks) or _in_protected(end - 1, blocks)
        segments.append({
            "text": para,
            "start": start,
            "end": end,
            "protected": is_protected,
        })
        i = end
    return segments


def _count_units(text: str, method: str, encoding: Any) -> int:
    if method == "token" and encoding:
        try:
            return len(encoding.encode(text))
        except Exception:
            return len(text)
    return len(text)


def chunk_markdown(
    text: str,
    chunk_size: int = 1500,
    overlap: int = 300,
    include_heading_context: bool = True,
    chunking_method: str = "char",
    encoding_name: str = "cl100k_base",
    strip_images: bool = True,
    parse_front_matter: bool = True,
) -> Dict[str, Any]:
    """
    Split markdown into overlapping chunks.

    Args:
        text: Raw markdown content
        chunk_size: Target size per chunk (chars or tokens)
        overlap: Overlap between consecutive chunks (chars or tokens)
        include_heading_context: Prepend heading tree to each chunk
        chunking_method: "char" (default) or "token" (requires tiktoken)
        encoding_name: tiktoken encoding name (default cl100k_base)
        strip_images: Replace image references with alt text / strip
        parse_front_matter: Extract YAML front matter into metadata

    Returns:
        Dict with keys: chunks (list), front_matter (dict or None), images (list)
    """
    result = {"chunks": [], "front_matter": None, "images": []}

    if not text or not text.strip():
        return result

    text = text.strip()

    if strip_images:
        text, images = _strip_images(text)
        result["images"] = images

    if parse_front_matter:
        fm, text = _parse_front_matter(text)
        result["front_matter"] = fm

    if not text.strip():
        return result

    encoding = None
    if chunking_method == "token":
        try:
            import tiktoken
            encoding = tiktoken.get_encoding(encoding_name)
        except Exception:
            chunking_method = "char"

    total_units = _count_units(text, chunking_method, encoding)
    if total_units <= chunk_size:
        headings = _extract_headings(text)
        ctx = _heading_context(total_units, headings) if include_heading_context else ""
        result["chunks"] = [{
            "index": 0,
            "text": text,
            "heading_context": ctx,
            "char_start": 0,
            "char_end": len(text),
            "total_chunks": 1,
        }]
        return result

    blocks = _find_protected_blocks(text)
    headings = _extract_headings(text) if include_heading_context else []
    segments = _split_into_segments(text, blocks)

    chunks = []
    current_segs = []
    current_units = 0

    def seg_units(seg: Dict[str, Any]) -> int:
        return _count_units(seg["text"], chunking_method, encoding)

    def emit_chunk(seg_list: List[Dict[str, Any]]) -> Dict[str, Any]:
        combined = "\n\n".join(s["text"] for s in seg_list)
        seg_start = seg_list[0]["start"]
        seg_end = seg_list[-1]["end"]
        ctx = _heading_context(seg_start, headings) if include_heading_context else ""
        return {
            "text": combined,
            "heading_context": ctx,
            "char_start": seg_start,
            "char_end": seg_end,
            "segments": seg_list,
        }

    i = 0
    while i < len(segments):
        seg = segments[i]
        su = seg_units(seg)
        gap = 2 if current_segs else 0

        if su > chunk_size and not seg.get("protected"):
            if current_segs:
                chunks.append(emit_chunk(current_segs))
                current_segs = []
                current_units = 0
            sents = _split_sentences(seg["text"], seg["start"])
            segments[i:i+1] = sents
            continue

        if current_units + gap + su > chunk_size and current_segs:
            chunk = emit_chunk(current_segs)
            chunks.append(chunk)

            overlap_end = chunk["char_end"]
            overlap_start = max(0, overlap_end - overlap)
            rebuild = []
            rebuild_units = 0
            for s in segments:
                if s["end"] <= overlap_start:
                    continue
                if s["start"] >= overlap_end:
                    break
                rebuild.append(s)
                rebuild_units += seg_units(s) + (2 if rebuild else 0)
            current_segs = list(rebuild)
            current_units = rebuild_units

        current_segs.append(seg)
        current_units += su + gap
        i += 1

    if current_segs:
        chunks.append(emit_chunk(current_segs))

    out = []
    for i, c in enumerate(chunks):
        out.append({
            "index": i,
            "text": c["text"],
            "heading_context": c["heading_context"],
            "char_start": c["char_start"],
            "char_end": c["char_end"],
            "total_chunks": len(chunks),
        })

    result["chunks"] = out
    return result
