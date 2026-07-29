#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pdf2epub.py — Manning 风格技术书籍 PDF → EPUB 转换器

特性:
  - 自动去除页眉页脚冗余内容(页码、版权行、Licensed to 水印)
  - 按 PDF 书签目录切分章节, 生成 EPUB 导航目录(支持多级)
  - 识别代码块(等宽字体 + 灰底区域), 保留缩进, 输出 <pre><code>
  - 按阅读顺序提取内嵌图片, 并去重
  - 识别标题层级 / 图表标题 / 侧边栏灰框
  - 处理断字(行尾连字符)与跨页段落合并

用法:
  python pdf2epub.py <input.pdf> <output.epub>
"""

import argparse
import hashlib
import html
import os
import re
import sys
from dataclasses import dataclass, field

import fitz  # PyMuPDF
from ebooklib import epub

# ---------------------------------------------------------------- 字体规则

# 整段等宽字体 -> 代码块
MONO_FONTS = {"consolas", "menlo-regular", "menlo", "courier", "couriernewpsmt",
              "couriernewps-boldmt", "lucidaconsole", "dejavusansmono"}

# 正文行内等宽 -> <code>
def is_mono_font(font_name: str) -> bool:
    f = font_name.lower()
    return any(m in f for m in ("consolas", "menlo", "courier", "mono"))

def is_italic_font(font_name: str) -> bool:
    return "italic" in font_name.lower() or "oblique" in font_name.lower()

def is_bold_font(font_name: str) -> bool:
    f = font_name.lower()
    return "bold" in f or "demi" in f or "black" in f


# ---------------------------------------------------------------- 数据结构

@dataclass
class Block:
    kind: str            # 'para' | 'code' | 'h1'..'h4' | 'caption' | 'obj' | 'image'
    html: str = ""       # 文本类: 内联 HTML; code: 转义后的纯文本(每行)
    bbox: tuple = (0, 0, 0, 0)
    rect_id: int = -1    # 所属灰底区域编号 (-1 无)
    image_key: str = ""  # kind == 'image' 时的图片 key
    page: int = 0

@dataclass
class Rect:
    bbox: tuple
    is_code: bool        # 0.949 灰 = 代码底; 0.91 灰 = 侧边栏底


# ---------------------------------------------------------------- 工具函数

SOFT_SUFFIXES = (
    "ing", "ed", "er", "ers", "tion", "sion", "ment", "ness", "ly", "al",
    "ic", "ous", "ive", "ity", "ies", "es", "ism", "ist", "ful", "less",
    "able", "ible", "ence", "ance", "ent", "ants", "age", "ry", "or", "s",
)

def dehyphen_join(prev: str, nxt: str) -> str:
    """prev 以 '-' 结尾时的合并规则: 软连字符(排版断字)去掉 '-', 复合词保留."""
    rest = nxt.lstrip()
    if rest[:1].islower():
        w0 = re.split(r"[^a-zA-Z]", rest)[0].lower()
        if any(w0.startswith(s) for s in SOFT_SUFFIXES):
            return prev[:-1] + rest
    return prev + rest


def join_wrapped(prev: str, nxt: str) -> str:
    """合并两行正文: 处理行尾断字. prev/nxt 已 strip."""
    if not prev:
        return nxt
    if prev.endswith("-") and not prev.endswith(" -"):
        return dehyphen_join(prev, nxt)
    return prev + " " + nxt


def norm_text(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", t.lower())


# ---------------------------------------------------------------- PDF 解析

class PdfExtractor:
    def __init__(self, path: str):
        self.doc = fitz.open(path)
        self.images = {}       # key -> (bytes, ext)
        self._img_by_hash = {} # md5 -> key
        self._img_counter = 0

    # ---- 图片去重存储 ----
    def store_image(self, data: bytes, ext: str) -> str:
        h = hashlib.md5(data).hexdigest()
        if h in self._img_by_hash:
            return self._img_by_hash[h]
        self._img_counter += 1
        key = f"img-{self._img_counter:04d}.{ext}"
        self.images[key] = (data, ext)
        self._img_by_hash[h] = key
        return key

    # ---- 每页灰底区域 ----
    @staticmethod
    def page_rects(page) -> list:
        rects = []
        for d in page.get_drawings():
            fill = d.get("fill")
            if not fill:
                continue
            r, g, b = fill
            if abs(r - g) > 0.02 or abs(g - b) > 0.02:
                continue
            if not (0.85 <= r <= 0.97):
                continue
            rect = d["rect"]
            if rect.width < 250 or rect.height < 12:
                continue
            rects.append(Rect(bbox=tuple(rect), is_code=(r > 0.93)))
        return rects

    @staticmethod
    def rect_of(bbox, rects) -> int:
        cx = (bbox[0] + bbox[2]) / 2
        cy = (bbox[1] + bbox[3]) / 2
        for i, rc in enumerate(rects):
            x0, y0, x1, y1 = rc.bbox
            if x0 - 2 <= cx <= x1 + 2 and y0 - 2 <= cy <= y1 + 2:
                return i
        return -1

    # ---- 清洗一行中的 span: 去掉 Merriweather 空白占位符 / 行尾空白 span ----
    @staticmethod
    def clean_spans(line_spans) -> list:
        out = []
        n = len(line_spans)
        for i, s in enumerate(line_spans):
            if not s["text"]:
                continue
            if not s["text"].strip():
                font = s["font"].lower()
                # Merriweather 12pt 的 \xa0 是段落间距占位符; 行尾空白 span 无意义
                if font.startswith("merriweather") or (i == n - 1 and not is_mono_font(s["font"])):
                    continue
            out.append(s)
        return out

    # ---- 行内 span -> HTML ----
    @staticmethod
    def spans_html(spans, emphasis=True) -> str:
        parts = []
        for s in spans:
            t = html.escape(s["text"])
            if not t.strip():
                if t:
                    parts.append(t)
                continue
            font = s["font"]
            if is_mono_font(font):
                t = f"<code>{t}</code>"
            elif emphasis:
                if is_italic_font(font):
                    t = f"<em>{t}</em>"
                elif is_bold_font(font):
                    t = f"<strong>{t}</strong>"
            if s.get("flags", 0) & 1:  # 上标
                t = f"<sup>{t}</sup>"
            parts.append(t)
        return "".join(parts)

    # ---- 文本块分类 ----
    def classify_text_block(self, block) -> tuple:
        """返回 (kind, html_text). 无法分类返回 ('para', ...)."""
        lines = []
        fonts = set()
        sizes = []
        for line in block["lines"]:
            spans = self.clean_spans(line["spans"])
            if not spans:
                continue
            line_html = self.spans_html(spans)
            line_plain_html = self.spans_html(spans, emphasis=False)
            plain = "".join(s["text"] for s in spans)
            if not plain.strip():
                continue
            lines.append((line_html, line_plain_html, plain))
            for s in spans:
                fonts.add(s["font"])
                sizes.append(round(s["size"], 1))
        if not lines:
            return ("skip", "")

        all_mono = all(is_mono_font(f) for f in fonts)
        size = max(set(sizes), key=sizes.count) if sizes else 8.0
        font0 = next(iter(fonts)) if len(fonts) == 1 else ""
        full_text = " ".join(p for _, _, p in lines).strip()

        # 代码块: 保留原始文本与缩进(转义 HTML)
        if all_mono:
            code_text = "\n".join(html.escape(l[2].rstrip()) for l in lines)
            return ("code", code_text)

        # 标题/图注: 用无强调版行内 HTML(保留行内代码)
        if "FranklinGothic-Medium" in font0 and size >= 18 and len(fonts) == 1:
            return ("h1", lines[0][1] if len(lines) == 1 else "<br/>".join(l[1] for l in lines))
        if len(fonts) == 1 and "Demi" in font0:
            if 11.5 <= size <= 14.5:
                return ("h2", " ".join(l[1] for l in lines))
            if 9.8 <= size < 11.5:
                return ("h3", " ".join(l[1] for l in lines))
            if 7.0 <= size < 9.0:
                if re.match(r"^(Figure|Listing|Table)\s", full_text):
                    return ("caption", " ".join(l[1] for l in lines))
        if fonts == {"FranklinGothic-Medium"} and 8.5 <= size <= 9.5:
            letters = [c for c in full_text if c.isalpha()]
            if letters and sum(c.isupper() for c in letters) / len(letters) > 0.8 and len(full_text) > 3:
                return ("h4", " ".join(l[1] for l in lines))
            return ("obj", " ".join(l[1] for l in lines))

        # 正文段落: 合并自动换行(HTML 层, 断字规则看下一行纯文本)
        merged_html = lines[0][0]
        for (lh, _, lp) in lines[1:]:
            cur = merged_html.rstrip()
            if cur.endswith("-") and not cur.endswith(" -"):
                rest = lp.lstrip()
                if rest[:1].islower():
                    w0 = re.split(r"[^a-zA-Z]", rest)[0].lower()
                    if any(w0.startswith(s) for s in SOFT_SUFFIXES):
                        merged_html = cur[:-1] + lh.lstrip()
                        continue
                merged_html = cur + lh.lstrip()
            else:
                merged_html = cur + " " + lh.lstrip()
        return ("para", merged_html.strip())

    # ---- 单页提取 ----
    FOOTER_PAT = re.compile(r"©\s*Manning Publications|Licensed to|liveBook", re.I)

    def extract_page(self, pno: int) -> list:
        page = self.doc[pno]
        H = page.rect.height
        rects = self.page_rects(page)
        blocks = []
        for b in page.get_text("dict")["blocks"]:
            x0, y0, x1, y1 = b["bbox"]
            # 页脚区域
            if y0 > H - 58:
                continue
            if b["type"] == 1:  # 图片
                data = b.get("image")
                if not data:
                    continue
                ext = b.get("ext", "png").lower()
                if ext not in ("png", "jpeg", "jpg"):
                    # 统一转 png
                    pix = fitz.Pixmap(self.doc, b["xref"]) if "xref" in b else None
                    if pix is None:
                        continue
                    if pix.colorspace and pix.colorspace.n > 3:
                        pix = fitz.Pixmap(fitz.csRGB, pix)
                    data = pix.tobytes("png")
                    ext = "png"
                key = self.store_image(data, "jpg" if ext == "jpeg" else ext)
                blocks.append(Block(kind="image", bbox=b["bbox"],
                                    rect_id=self.rect_of(b["bbox"], rects),
                                    image_key=key, page=pno))
                continue

            # 文本块: 收集纯文本用于过滤
            plain_lines = []
            for line in b["lines"]:
                t = "".join(s["text"] for s in line["spans"])
                if t.strip():
                    plain_lines.append((t, line["bbox"]))
            if not plain_lines:
                continue
            full = " ".join(t for t, _ in plain_lines).strip()

            # 页码(顶部纯数字)
            if y1 < 46 and re.fullmatch(r"[0-9ivxlcdm]+", full.lower()):
                continue
            # 页脚文字(任何位置兜底)
            if self.FOOTER_PAT.search(full):
                continue
            # 纯空白占位块
            if not full.replace("\xa0", "").strip():
                continue

            kind, content = self.classify_text_block(b)
            if kind == "skip" or not content.strip():
                continue
            blocks.append(Block(kind=kind, html=content, bbox=b["bbox"],
                                rect_id=self.rect_of(b["bbox"], rects), page=pno))

        # 阅读顺序排序
        blocks.sort(key=lambda bl: (round(bl.bbox[1], 1), bl.bbox[0]))
        return blocks

    # ---- 块序列 -> HTML ----
    def blocks_to_html(self, blocks: list) -> str:
        out = []
        i = 0
        n = len(blocks)
        open_sidebar = None  # rect_id

        def close_sidebar():
            nonlocal open_sidebar
            if open_sidebar is not None:
                out.append("</div>")
                open_sidebar = None

        while i < n:
            bl = blocks[i]

            # 侧边栏分组: 属于同一个非代码灰底区域的连续块
            in_sidebar = bl.rect_id >= 0 and not self._rect_is_code.get((bl.page, bl.rect_id), False)
            sid = bl.rect_id if in_sidebar else None
            if sid != open_sidebar:
                close_sidebar()
                if sid is not None:
                    out.append('<div class="sidebar">')
                    open_sidebar = sid

            if bl.kind == "code":
                # 合并连续代码块(同 rect 或间隙小)
                codes = [bl.html]
                j = i + 1
                while j < n and blocks[j].kind == "code":
                    prev, cur = blocks[j - 1], blocks[j]
                    same_rect = cur.rect_id >= 0 and cur.rect_id == prev.rect_id
                    close_gap = cur.rect_id == prev.rect_id == -1 and \
                        (cur.bbox[1] - prev.bbox[3]) < 20
                    if not (same_rect or close_gap):
                        break
                    codes.append(cur.html)
                    j += 1
                out.append("<pre><code>" + "\n".join(codes) + "</code></pre>")
                i = j
                continue

            if bl.kind == "caption":
                caps = [bl.html]
                j = i + 1
                while j < n and blocks[j].kind == "caption" and \
                        blocks[j].rect_id == bl.rect_id and \
                        (blocks[j].bbox[1] - blocks[j - 1].bbox[3]) < 14:
                    caps.append(blocks[j].html)
                    j += 1
                out.append('<p class="caption">' + " ".join(caps) + "</p>")
                i = j
                continue

            if bl.kind == "image":
                out.append(f'<div class="figure"><img src="../images/{bl.image_key}" alt="figure"/></div>')
            elif bl.kind == "obj":
                out.append(f'<p class="obj">{bl.html}</p>')
            elif bl.kind in ("h1", "h2", "h3", "h4"):
                # 标题 id 由调用方后续分配
                out.append(f"<{bl.kind}>{bl.html}</{bl.kind}>")
            else:
                out.append(f"<p>{bl.html}</p>")
            i += 1

        close_sidebar()
        return "\n".join(out)

    # rect 是否代码底: (page, rect_id) -> bool
    _rect_is_code = {}

    def extract(self, start: int = 0, end: int = None):
        end = len(self.doc) if end is None else min(end, len(self.doc))
        page_blocks = {}
        self._rect_is_code = {}
        for pno in range(start, end):
            page = self.doc[pno]
            rects = self.page_rects(page)
            for rid, rc in enumerate(rects):
                self._rect_is_code[(pno, rid)] = rc.is_code
            page_blocks[pno] = self.extract_page(pno)
        return page_blocks


# ---------------------------------------------------------------- 跨页段落合并

TERMINAL_RE = re.compile(r'[.!?:;…”"\'\)\]\}»]\s*(</[a-z]+>)?\s*$')

def merge_across_pages(page_blocks: dict) -> list:
    """把分页处被截断的段落合并: 上一段未结束 + 下一段小写开头."""
    seq = []
    for pno in sorted(page_blocks):
        seq.extend(page_blocks[pno])
    merged = []
    for bl in seq:
        if (merged and bl.kind == "para" and merged[-1].kind == "para"
                and merged[-1].rect_id == -1 and bl.rect_id == -1):
            prev_html = merged[-1].html.rstrip()
            plain = re.sub(r"<[^>]+>", "", prev_html)
            nxt_plain = re.sub(r"<[^>]+>", "", bl.html).lstrip()
            if plain and not TERMINAL_RE.search(plain) and nxt_plain[:1].islower():
                # 断字处理
                if plain.endswith("-"):
                    first = re.split(r"[^a-zA-Z]", nxt_plain)[0].lower()
                    if any(first.startswith(s) for s in SOFT_SUFFIXES):
                        merged[-1].html = prev_html[:-1] + bl.html.lstrip()
                        continue
                    merged[-1].html = prev_html + bl.html.lstrip()
                    continue
                merged[-1].html = prev_html + " " + bl.html.lstrip()
                continue
        merged.append(bl)
    return merged


# ---------------------------------------------------------------- EPUB 构建

CSS = """
body { font-family: Georgia, 'Noto Serif', serif; line-height: 1.55; margin: 0 3%; }
p { margin: 0.55em 0; text-align: justify; }
h1, h2, h3, h4 { font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif;
                 line-height: 1.3; margin: 1.1em 0 0.5em; text-align: left; }
h1 { font-size: 1.5em; border-bottom: 2px solid #1a3a6b; padding-bottom: 0.25em; }
h2 { font-size: 1.25em; color: #1a3a6b; }
h3 { font-size: 1.1em; color: #1a3a6b; }
h4 { font-size: 1.0em; color: #333; text-transform: none; }
code { font-family: Consolas, 'Courier New', monospace; font-size: 0.88em;
       background: #f0f0f0; padding: 0 0.15em; border-radius: 3px; }
pre { font-family: Consolas, 'Courier New', monospace; font-size: 0.82em;
      background: #f6f6f6; border: 1px solid #e0e0e0; border-left: 3px solid #1a3a6b;
      padding: 0.6em 0.8em; margin: 0.8em 0; line-height: 1.4;
      white-space: pre-wrap; word-wrap: break-word; }
pre code { background: transparent; padding: 0; font-size: 1em; }
p.caption { font-size: 0.85em; color: #555; font-style: italic;
            text-align: left; margin: 0.35em 0 1.0em; }
div.figure { text-align: center; margin: 0.9em 0 0.3em; }
img { max-width: 100%; height: auto; }
div.sidebar { background: #f2f2f2; border: 1px solid #e2e2e2; border-radius: 6px;
              padding: 0.4em 0.9em; margin: 0.9em 0; }
p.obj { margin: 0.25em 0; }
""".strip()


def slugify(t: str, idx: int) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", t.lower()).strip("-")
    return f"{s[:40] or 'sec'}-{idx}"


def build_epub(pdf_path: str, out_path: str, start: int = 0, end: int = None):
    ext = PdfExtractor(pdf_path)
    doc = ext.doc
    meta = doc.metadata
    toc = doc.get_toc()
    npages = len(doc)
    end = npages if end is None else min(end, npages)

    page_blocks = ext.extract(start, end)
    blocks = merge_across_pages(page_blocks)

    # ---------------- 章节划分: 以 L1 目录为界 ----------------
    l1 = [(t, p) for l, t, p in toc if l == 1 and start <= p - 1 < end]
    if not l1:
        l1 = [(meta.get("title") or "Book", start + 1)]
    # (title, start_idx, end_idx) 0-based
    chapters = []
    for i, (t, p1) in enumerate(l1):
        s = p1 - 1
        e = (l1[i + 1][1] - 1) if i + 1 < len(l1) else end
        chapters.append((t, s, e))

    # 每页 -> 块列表 的起始位置映射, 用于把 blocks 分配到章节
    # blocks 带有 page 字段, 直接按 page 分组
    ch_blocks = [[] for _ in chapters]
    for bl in blocks:
        for ci, (_, s, e) in enumerate(chapters):
            if s <= bl.page < e:
                # 章节首页的特大标题块跳过(统一用 TOC 标题做 h1)
                if bl.page == s and bl.kind == "h1":
                    break
                ch_blocks[ci].append(bl)
                break

    # ---------------- 标题 anchor: TOC L2/L3 -> 标题 id ----------------
    # 页面上的 toc 条目 (页 -> [(lvl,title,idx)])
    toc_by_page = {}
    for idx, (l, t, p) in enumerate(toc):
        toc_by_page.setdefault(p - 1, []).append((l, t, idx))
    toc_ids = {}  # toc idx -> anchor id

    book = epub.EpubBook()
    title = meta.get("title") or os.path.basename(pdf_path)
    book.set_identifier(hashlib.md5(title.encode()).hexdigest())
    book.set_title(title)
    if meta.get("author"):
        book.add_author(meta["author"])
    book.set_language("en")

    # 样式
    style = epub.EpubItem(uid="style", file_name="style/style.css",
                          media_type="text/css", content=CSS.encode("utf-8"))
    book.add_item(style)

    # 图片
    for key, (data, e) in ext.images.items():
        mt = "image/jpeg" if e == "jpg" else f"image/{e}"
        book.add_item(epub.EpubItem(file_name=f"images/{key}", media_type=mt, content=data))

    # 封面: 渲染 PDF 首页
    try:
        pix = doc[0].get_pixmap(matrix=fitz.Matrix(2, 2))
        cover_bytes = pix.tobytes("jpg", jpg_quality=88)
        book.set_cover("cover.jpg", cover_bytes)
    except Exception as exc:
        print(f"[warn] 封面生成失败: {exc}")

    # ---------------- 生成章节 xhtml ----------------
    ch_items = []
    h_counter = 0
    for ci, ((ctitle, s, e), blks) in enumerate(zip(chapters, ch_blocks)):
        body_parts = [f"<h1>{html.escape(ctitle)}</h1>"]
        html_body = ext.blocks_to_html(blks)
        # 给 h2/h3 分配 id: 匹配 TOC
        def repl_heading(m):
            nonlocal h_counter
            tag, inner = m.group(1), m.group(2)
            plain = re.sub(r"<[^>]+>", "", inner)
            nplain = norm_text(plain)
            hid = None
            # 在该章页面范围匹配 toc 条目
            for pno in range(s, e):
                for (l, t, idx) in toc_by_page.get(pno, []):
                    if idx in toc_ids:
                        continue
                    nt = norm_text(t)
                    if nt and (nt in nplain or nplain.startswith(nt[:25])):
                        hid = f"toc-{idx}"
                        toc_ids[idx] = hid
                        break
                if hid:
                    break
            if hid is None:
                h_counter += 1
                hid = f"h-{h_counter}"
            return f'<{tag} id="{hid}">{inner}</{tag}>'

        html_body = re.sub(r"<(h[234])>(.*?)</\1>", repl_heading, html_body, flags=re.S)
        body_parts.append(html_body)

        fname = f"text/ch-{ci:02d}.xhtml"
        item = epub.EpubHtml(title=ctitle, file_name=fname, lang="en")
        item.content = (
            "<?xml version='1.0' encoding='utf-8'?>"
            "<!DOCTYPE html><html xmlns='http://www.w3.org/1999/xhtml'>"
            f"<head><title>{html.escape(ctitle)}</title>"
            "<link rel='stylesheet' type='text/css' href='../style/style.css'/></head>"
            f"<body>{''.join(body_parts)}</body></html>"
        ).encode("utf-8")
        item.add_item(style)
        book.add_item(item)
        ch_items.append(item)

    # ---------------- 导航 TOC ----------------
    nav = []
    for idx, (l, t, p) in enumerate(toc):
        p0 = p - 1
        if not (start <= p0 < end):
            continue
        # 找所属章节
        ci = None
        for k, (_, s, e) in enumerate(chapters):
            if s <= p0 < e:
                ci = k
                break
        if ci is None:
            continue
        link = ch_items[ci].file_name.split("/")[-1]
        if idx in toc_ids:
            link += f"#{toc_ids[idx]}"
        nav.append((l, epub.Link(f"text/{link}", t, f"nav-{idx}")))

    # 嵌套结构
    def nest(entries):
        root = []
        stack = [(0, root)]
        for l, link in entries:
            while len(stack) > l:
                stack.pop()
            node = (link, [])
            stack[-1][1].append(node)
            stack.append((l, node[1]))
        return tuple(root)

    book.toc = nest(nav)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["cover", "nav"] + ch_items

    epub.write_epub(out_path, book)
    return ext, chapters


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Manning 风格 PDF -> EPUB 转换器")
    ap.add_argument("input", help="输入 PDF 路径")
    ap.add_argument("output", help="输出 EPUB 路径")
    ap.add_argument("--start", type=int, default=0, help="起始页(0-based, 含)")
    ap.add_argument("--end", type=int, default=None, help="结束页(0-based, 不含)")
    args = ap.parse_args()

    print(f"[*] 解析: {args.input}")
    ext, chapters = build_epub(args.input, args.output, args.start, args.end)
    size = os.path.getsize(args.output) / 1024 / 1024
    print(f"[OK] 输出: {args.output}  ({size:.1f} MB)")
    print(f"     章节: {len(chapters)}, 图片: {len(ext.images)}")


if __name__ == "__main__":
    main()
