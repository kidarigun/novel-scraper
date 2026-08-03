#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""사용자가 저장한 HTML에서 읽을거리 본문만 텍스트로 정리하는 도구.

네트워크 요청이나 브라우저 제어는 하지 않는다. 접근 권한이 있는 페이지를 사용자가
직접 저장한 뒤 이 파일에 전달하는 용도다.
"""

import argparse
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path


NOISE_TAGS = {
    "script", "style", "noscript", "template", "iframe", "svg", "canvas",
    "nav", "header", "footer", "aside", "form", "button", "dialog",
}
BLOCK_TAGS = {
    "article", "main", "section", "div", "p", "pre", "blockquote", "li",
    "h1", "h2", "h3", "h4", "h5", "h6", "br",
}
NOISE_RE = re.compile(
    r"(?:^|[-_\s])(ad|ads|advert|advertisement|banner|cookie|consent|nav|navigation|"
    r"menu|footer|header|sidebar|breadcrumb|pagination|comment|related|recommend|share|social)"
    r"(?:$|[-_\s])",
    re.IGNORECASE,
)
CONTENT_RE = re.compile(
    r"(?:^|[-_\s])(content|article|post|entry|story|reading|reader|body)(?:$|[-_\s])",
    re.IGNORECASE,
)
JUNK_LINE_RE = re.compile(r"^\s*(광고|advertisement|ad)\s*$", re.IGNORECASE)


@dataclass
class _Candidate:
    priority: int
    pieces: list[str] = field(default_factory=list)


@dataclass
class _Frame:
    tag: str
    suppressed: bool
    candidate: _Candidate | None = None


def _attribute_text(attrs):
    values = []
    for key in ("id", "class", "role"):
        value = attrs.get(key)
        if value:
            values.append(value)
    return " ".join(values)


def _candidate_priority(tag, attrs):
    if tag == "article":
        return 100
    if attrs.get("role", "").lower() == "main":
        return 90
    if tag == "main":
        return 80
    if CONTENT_RE.search(_attribute_text(attrs)):
        return 60
    if tag == "body":
        return 1
    return 0


class _ArticleParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.frames: list[_Frame] = []
        self.candidates: list[_Candidate] = []

    def _is_suppressed(self):
        return any(frame.suppressed for frame in self.frames)

    def _active_candidates(self):
        if self._is_suppressed():
            return []
        return [frame.candidate for frame in self.frames if frame.candidate]

    def _append_break(self):
        for candidate in self._active_candidates():
            candidate.pieces.append("\n")

    def handle_starttag(self, tag, attrs_list):
        tag = tag.lower()
        attrs = dict(attrs_list)
        attr_text = _attribute_text(attrs)
        suppressed = self._is_suppressed() or tag in NOISE_TAGS or bool(NOISE_RE.search(attr_text))
        candidate = None
        if not suppressed:
            priority = _candidate_priority(tag, attrs)
            if priority:
                candidate = _Candidate(priority=priority)
                self.candidates.append(candidate)
        self.frames.append(_Frame(tag=tag, suppressed=suppressed, candidate=candidate))
        if tag in BLOCK_TAGS:
            self._append_break()

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag.lower() in BLOCK_TAGS:
            self._append_break()
        if self.frames:
            self.frames.pop()

    def handle_data(self, data):
        if not data or self._is_suppressed():
            return
        for candidate in self._active_candidates():
            candidate.pieces.append(data)


def _clean_pieces(pieces):
    text = "".join(pieces).replace("\xa0", " ")
    lines = []
    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line or JUNK_LINE_RE.fullmatch(line):
            continue
        if not lines or lines[-1] != line:
            lines.append(line)
    return "\n\n".join(lines).strip()


def extract_article_text(html):
    """HTML 문자열에서 가장 적합한 본문 후보의 정리된 텍스트를 반환한다."""
    parser = _ArticleParser()
    parser.feed(html)
    parser.close()

    best = ""
    best_score = -1
    for candidate in parser.candidates:
        text = _clean_pieces(candidate.pieces)
        if not text:
            continue
        score = candidate.priority * 1_000_000 + len(text)
        if score > best_score:
            best, best_score = text, score
    return best


def main():
    parser = argparse.ArgumentParser(description="저장한 HTML에서 본문 텍스트만 추출")
    parser.add_argument("html_file", help="사용자가 저장한 HTML 파일 경로")
    parser.add_argument("--out", help="출력 텍스트 파일 경로 (기본: 원본 파일명.txt)")
    args = parser.parse_args()

    source = Path(args.html_file)
    text = extract_article_text(source.read_text(encoding="utf-8", errors="replace"))
    if not text:
        raise SystemExit("본문 후보를 찾지 못했습니다.")

    target = Path(args.out) if args.out else source.with_suffix(".txt")
    target.write_text(text + "\n", encoding="utf-8")
    print(f"저장 완료: {target} ({len(text):,}자)")


if __name__ == "__main__":
    main()
