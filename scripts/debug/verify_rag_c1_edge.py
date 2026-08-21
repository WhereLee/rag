# -*- coding: utf-8 -*-
"""C1 边界测试：空文档 / 纯表格 / 无标题 / 超长切分 / 去重 / 缩进 / 图片独立块。
用法: python scripts/debug/verify_rag_c1_edge.py（需在项目根目录执行）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "rag-python" / "src"))

from ingest.chunker import TARGET_CHARS, chunk_nodes
from ingest.parser.base import DocumentNode, rows_to_markdown

fail = 0


def check(name, cond, detail=""):
    global fail
    if cond:
        print(f"[OK] {name}")
    else:
        print(f"[FAIL] {name} -> {detail}")
        fail += 1


def P(text, **meta):
    return DocumentNode("paragraph", text, meta)


def H(text, level=1):
    return DocumentNode("heading", text, {"level": level})

# 1. 空文档 → 0 块
check("空文档 0 块", chunk_nodes([]) == [])

# 2. 纯标题文档（无正文）→ 0 块
check("纯标题 0 块", chunk_nodes([H("甲", 1), H("乙", 2)]) == [])

# 3. 纯表格文档：26 行（表头+25 数据行）→ 3 组，每组表头保留
rows = [["列A", "列B"]] + [[f"v{i}", str(i)] for i in range(25)]
md = rows_to_markdown(rows)
chunks = chunk_nodes([DocumentNode("table", md, {})])
check("纯表格 3 组", len(chunks) == 3, f"groups={len(chunks)}")
if len(chunks) == 3:
    hdr = chunks[0].content.splitlines()[0]
    check("表格组间表头保留", all(c.content.splitlines()[0] == hdr for c in chunks))
    check("表格组类型", all(c.chunk_type == "table" for c in chunks))

# 4. 无 heading 文档 → heading_path 全空
chunks = chunk_nodes([P("短段落一"), P("短段落二"), P("短段落三")])
check("无标题路径为空", all(c.heading_path == "" for c in chunks))

# 5. 超长段落（2000 字句号分隔）→ 每块 ≤700 且带 overlap
long_text = "".join(f"第{i}个完整句子中的内容说明。" for i in range(120))
chunks = chunk_nodes([P(long_text)])
check("超长切多块", len(chunks) >= 3, f"chunks={len(chunks)}")
check("超长每块不超 700", all(c.chars <= 700 for c in chunks), [c.chars for c in chunks])
if len(chunks) >= 2:
    check("超长块重叠 50 字符",
          chunks[1].content.startswith(chunks[0].content[-50:]))

# 6. 无标点超长串（单句硬切）→ 每块 ≤500
chunks = chunk_nodes([P("无标点内容" * 200)])  # 1000 字无标点
check("硬切每块不超 500", all(c.chars <= 500 for c in chunks), [c.chars for c in chunks])

# 7. 重复内容去重（长段落独立成块）
dup = "完全相同的段落内容。" * 15
chunks = chunk_nodes([P(dup), P("不一样的段落内容。" * 15), P(dup)])
check("重复块去重", len(chunks) == 2, f"chunks={len(chunks)}")

# 8. code 缩进保留（真实缩进代码按行切）
code = "def main():\n    conn = connect()\n    result = conn.query()\n    return result\n" * 60
chunks = chunk_nodes([DocumentNode("code", code, {})])
check("code 多块", len(chunks) >= 2, f"chunks={len(chunks)}")
check("code 缩进保留", all("    conn = connect()" in c.content for c in chunks))

# 9. 图片独立成块（VLM 描述即内容，不合并）
chunks = chunk_nodes([P("短段"), DocumentNode("image", "架构图：网关连接数据库", {}), P("尾段")])
check("图片独立块", [c.chunk_type for c in chunks] == ["paragraph", "image", "paragraph"],
      [c.chunk_type for c in chunks])

# 10. 纯标点/空白剔除
chunks = chunk_nodes([P("......"), P("　　"), P("【】——"), P("有效内容" * 30)])
check("纯标点剔除", len(chunks) == 1 and chunks[0].content == "有效内容" * 30,
      [c.content[:20] for c in chunks])

# 11. 段落合并被打断（list 插入中间）
chunks = chunk_nodes([P("短一"), DocumentNode("list", "项一\n项二", {}), P("短二")])
check("合并被打断", [c.chunk_type for c in chunks] == ["paragraph", "list", "paragraph"])

# 12. 表格不跨节点合并（表格后段落独立）
chunks = chunk_nodes([DocumentNode("table", rows_to_markdown([["a"], ["b"]]), {}), P("短段")])
check("表格后段落独立", chunks[-1].chunk_type == "paragraph" and len(chunks) == 2)

print(f"\nC1 edge: {'PASS' if fail == 0 else f'{fail} FAILED'}")
sys.exit(1 if fail else 0)
