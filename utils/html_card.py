"""Convert simple HTML documents into Feishu card-friendly Markdown."""

from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser


class _Renderer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0
        self.list_stack: list[dict] = []
        self.link_stack: list[tuple[int, str]] = []
        self.in_pre = False
        self.in_code = False

    def _nl(self, count: int = 1) -> None:
        text = "".join(self.out)
        need = "\n" * count
        if not text.endswith(need):
            self.out.append(need)

    def _attrs(self, attrs) -> dict:
        return {str(k).lower(): str(v or "") for k, v in attrs}

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        attr = self._attrs(attrs)
        if tag in {"script", "style", "noscript", "svg"}:
            self.skip += 1
            return
        if self.skip:
            return
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._nl(2)
            self.out.append("**")
        elif tag in {"p", "div", "section", "article", "header", "footer", "blockquote"}:
            self._nl(2)
        elif tag == "br":
            self._nl(1)
        elif tag in {"strong", "b"}:
            self.out.append("**")
        elif tag in {"em", "i"}:
            self.out.append("*")
        elif tag == "code" and not self.in_pre:
            self.in_code = True
            self.out.append("`")
        elif tag == "pre":
            self._nl(2)
            self.in_pre = True
            self.out.append("```\n")
        elif tag in {"ul", "ol"}:
            self.list_stack.append({"tag": tag, "count": 0})
            self._nl(1)
        elif tag == "li":
            self._nl(1)
            if self.list_stack and self.list_stack[-1]["tag"] == "ol":
                self.list_stack[-1]["count"] += 1
                self.out.append(f"{self.list_stack[-1]['count']}. ")
            else:
                self.out.append("• ")
        elif tag == "a":
            self.link_stack.append((len(self.out), attr.get("href", "")))
        elif tag == "img":
            alt = attr.get("alt", "").strip() or "图片"
            src = attr.get("src", "").strip()
            self.out.append(f"[{alt}]({src})" if src else f"[{alt}]")
        elif tag == "tr":
            self._nl(1)
        elif tag in {"td", "th"}:
            if not "".join(self.out).endswith(("\n", " ")):
                self.out.append("　")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg"} and self.skip:
            self.skip -= 1
            return
        if self.skip:
            return
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.out.append("**")
            self._nl(2)
        elif tag in {"p", "div", "section", "article", "header", "footer", "blockquote", "table"}:
            self._nl(2)
        elif tag in {"strong", "b"}:
            self.out.append("**")
        elif tag in {"em", "i"}:
            self.out.append("*")
        elif tag == "code" and self.in_code:
            self.out.append("`")
            self.in_code = False
        elif tag == "pre" and self.in_pre:
            self.out.append("\n```")
            self._nl(2)
            self.in_pre = False
        elif tag in {"ul", "ol"} and self.list_stack:
            self.list_stack.pop()
            self._nl(1)
        elif tag == "li":
            self._nl(1)
        elif tag == "a" and self.link_stack:
            start, href = self.link_stack.pop()
            if href:
                label = "".join(self.out[start:]).strip()
                if label and href not in label:
                    del self.out[start:]
                    self.out.append(f"[{label}]({href})")
        elif tag in {"td", "th"}:
            self.out.append("　")

    def handle_data(self, data: str) -> None:
        if self.skip:
            return
        text = unescape(data)
        if self.in_pre:
            self.out.append(text.rstrip("\n"))
            return
        text = re.sub(r"\s+", " ", text)
        if text.strip():
            self.out.append(text)


def to_markdown(html: str) -> str:
    renderer = _Renderer()
    renderer.feed(html)
    renderer.close()
    text = "".join(renderer.out)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() or "（HTML 无可渲染文本内容）"
