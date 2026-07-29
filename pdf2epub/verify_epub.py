# -*- coding: utf-8 -*-
"""验证 EPUB: 页脚残留、代码块、图片、标题结构"""
import sys, io, zipfile, re
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

path = sys.argv[1] if len(sys.argv) > 1 else r"D:\Projectx\pdf2epub\test_sample.epub"
z = zipfile.ZipFile(path)

names = z.namelist()
xhtmls = [n for n in names if n.endswith((".xhtml", ".html"))]
imgs = [n for n in names if "/images/" in n or n.startswith("images/")]
print(f"文件数: {len(names)}, xhtml: {len(xhtmls)}, 图片: {len(imgs)}")
print("xhtml:", xhtmls)

all_text = ""
for n in xhtmls:
    all_text += z.read(n).decode("utf-8", errors="replace")

# 页脚残留检查
for pat in ["Manning Publications", "Licensed to", "liveBook", "149533107"]:
    cnt = all_text.count(pat)
    print(f"页脚残留 {pat!r}: {cnt} 处")

# 孤立页码检查: <p>纯数字</p>
nums = re.findall(r"<p>\s*\d+\s*</p>", all_text)
print(f"孤立页码段落: {len(nums)} 个 {nums[:5]}")

# 代码块
pres = re.findall(r"<pre><code>(.*?)</code></pre>", all_text, re.S)
print(f"\n代码块数: {len(pres)}")
if pres:
    print("--- 代码块样例 1 ---")
    print(pres[0][:400])
    if len(pres) > 3:
        print("--- 代码块样例 4 ---")
        print(pres[3][:400])

# 缩进保留检查
indented = [l for l in all_text.split("\n") if l.startswith("   ") or "  " in l[:6]]
print(f"\n含缩进代码行数(抽样): {len(indented)}")

# 标题结构
for tag in ("h1", "h2", "h3", "h4"):
    hs = re.findall(rf"<{tag}[^>]*>(.*?)</{tag}>", all_text, re.S)
    hs = [re.sub(r"<[^>]+>", "", h)[:55] for h in hs]
    print(f"{tag}: {len(hs)} -> {hs[:6]}")

# caption
caps = re.findall(r'<p class="caption">(.*?)</p>', all_text, re.S)
print(f"\ncaption: {len(caps)}")
for c in caps[:3]:
    print("  ", re.sub(r"<[^>]+>", "", c)[:90])

# 图片引用
refs = re.findall(r'<img src="\.\./images/([^"]+)"', all_text)
have = {i.split("/")[-1] for i in imgs}
print(f"\nimg 引用: {len(refs)}, 实际图片: {len(imgs)}, 缺失: {set(refs) - have}, 引用列表前5: {sorted(refs)[:5]}")

# 正文样例
m = re.search(r"<p>(The|As|In)[^<]{60,200}", all_text)
if m:
    print("\n正文样例:", m.group(0)[:180])

# sidebar
sbs = re.findall(r'<div class="sidebar">', all_text)
print("sidebar 数:", len(sbs))
