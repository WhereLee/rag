# -*- coding: utf-8 -*-
"""R1 正常测试：txt（UTF-8/GBK）/ md 全结构 / 清洗规则端到端。
用法: python scripts/debug/verify_rag_r1_normal.py（需在项目根目录执行）
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "rag-python" / "src"))

from ingest.clean.cleaner import clean_nodes
from ingest.parser.txt_md import TxtMdParser

fail = 0


def check(name, cond, detail=""):
    global fail
    if cond:
        print(f"[OK] {name}")
    else:
        print(f"[FAIL] {name} -> {detail}")
        fail += 1


tmp = Path(tempfile.mkdtemp(prefix="rag_r1_n_"))

# 1. UTF-8 txt 分段
p = tmp / "a.txt"
p.write_text("第一段。\n第二行。\n\n第二段。\n\n\n第三段。", encoding="utf-8")
nodes = TxtMdParser().parse(p)
check("UTF-8 txt 分段", [n.type for n in nodes] == ["paragraph"] * 3, nodes)

# 2. GBK txt
p = tmp / "gbk.txt"
p.write_bytes("中文GBK编码文件".encode("gbk"))
nodes = TxtMdParser().parse(p)
check("GBK 编码回退", nodes and nodes[0].text == "中文GBK编码文件", nodes)

# 3. CRLF 文件（Windows 生成）
p = tmp / "crlf.txt"
p.write_bytes("第一行\r\n第二行\r\n\r\n第二段\r\n".encode("utf-8"))
nodes = TxtMdParser().parse(p)
check("CRLF 换行统一后分段", len(nodes) == 2 and nodes[0].text == "第一行\n第二行", nodes)

# 4. md 全结构
md = """# 项目说明

这是一段介绍。

## 特性

- 特性一
- 特性二

```python
def hello():
    return "hi"
```

| 名称 | 值 |
| --- | --- |
| A | 1 |
"""
p = tmp / "doc.md"
p.write_text(md, encoding="utf-8")
nodes = TxtMdParser().parse(p)
types = [n.type for n in nodes]
check("md 结构顺序", types == ["heading", "paragraph", "heading", "list", "list", "code", "table"], types)
h1 = nodes[0]
check("标题层级", h1.meta.get("level") == 1 and h1.text == "项目说明", h1)

# 5. 清洗端到端：脏文本 → 干净
p = tmp / "dirty.txt"
p.write_text("  第一段\u3000含全角\u00a0空格。\n\n\n第二段\x00带控制字符。\r\n", encoding="utf-8")
raw_nodes = TxtMdParser().parse(p)
cleaned = clean_nodes(raw_nodes)
t0 = cleaned[0]
check("全角/不间断空格归一", "含全角 空格" in t0.text, repr(t0.text))
check("控制字符剔除", "\x00" not in cleaned[1].text, repr(cleaned[1].text))
check("连续空行压缩", len(cleaned) == 2, len(cleaned))
check("清洗标记记录", "space" in t0.meta.get("cleaned_flags", []), t0.meta)

# 6. code 缩进保留
p = tmp / "code.md"
p.write_text("```\n    indent  keep\n```", encoding="utf-8")
nodes = clean_nodes(TxtMdParser().parse(p))
check("code 缩进保留", "    indent  keep" in nodes[0].text, repr(nodes[0].text))

print("")
if fail == 0:
    print("===== R1 正常测试全部通过 =====")
else:
    print(f"===== 有 {fail} 项失败 =====")
    sys.exit(1)
