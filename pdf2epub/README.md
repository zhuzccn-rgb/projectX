# pdf2epub — Manning 风格技术书籍 PDF → EPUB 转换器

专为《Build a Large Language Model (From Scratch)》MEAP 版打造的转换工具，
也可用于其他 Manning 排版风格（Verdana 正文 / Consolas 代码 / 灰底代码框）的 PDF 书籍。

## 功能

- **去除页眉页脚冗余**：自动移除页码、`© Manning Publications Co. To comment go to liveBook`
  和 `Licensed to ... <邮箱>` 授权水印（版权页正文保留）
- **图文准确**：按阅读顺序提取全部内嵌图片（自动去重），图片与图注一一对应
- **代码块精准还原**：识别等宽字体 + 灰底区域，完整保留缩进与对齐注释，
  输出 `<pre><code>`，长行自动软换行，适合手机阅读
- **结构化章节**：按 PDF 书签切分章节，生成三级嵌套导航目录（带页内锚点跳转）
- **智能排版**：
  - 识别 H1–H4 标题（章 / 节 / 小节 / 边栏标题）
  - 图注（Figure / Listing / Table）斜体小字样式
  - 灰底侧边栏（练习、提示框）还原为圆角灰框
  - 行内代码、斜体、粗体、上标保留
  - 自动处理行尾断字（dehyphenation）与跨页段落合并

## 安装

```bash
pip install -r requirements.txt
```

## 使用

```bash
# 基本用法
python pdf2epub.py input.pdf output.epub

# 只转换部分页面（调试用，页码从 0 开始）
python pdf2epub.py input.pdf output.epub --start 21 --end 64

# 验证输出质量（页脚残留 / 代码块 / 图片完整性检查）
python verify_epub.py output.epub
```

## 输出说明

- 章节文件：`EPUB/text/ch-XX.xhtml`（按一级目录切分）
- 图片：`EPUB/images/`（PNG/JPEG 原图，MD5 去重）
- 导航：EPUB3 nav + NCX 双目录，兼容新旧阅读器
- 封面：自动渲染 PDF 首页为封面

## 已知限制

- 表格会按普通段落文本保留（内容不丢，但无表格结构）
- 针对 Manning 排版字体规则调优；用于其他出版社 PDF 时可能需要调整
  `pdf2epub.py` 中的字体分类规则（`classify_text_block`）
