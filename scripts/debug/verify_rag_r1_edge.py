# -*- coding: utf-8 -*-
"""R1 边界测试：非法编码 / 空文件 / 纯 BOM / 嵌套标题 / 超长代码 / 密集特殊空白 / 控制字符。
用法: python scripts/debug/verify_rag_r1_edge.py（需在项目根目录执行）
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "rag-python" / "src"))

from ingest.clean.cleaner import clean_node
from ingest.parser.base import DocumentNode, ParseError
from ingest.parser.txt_md import TxtMdParser

fail = 0


def check(name, cond, detail=""):
    global fail
    if cond:
        print(f"[OK] {name}")
    else:
        print(f"[FAIL] {name} -> {detail}")
        fail += 1


tmp = Path(tempfile.mkdtemp(prefix="rag_r1_e_"))
parser = TxtMdParser()

# 1. 非法编码 → ParseError
p = tmp / "bad.txt"
p.write_bytes(b"\xff\xfe\x00\x81\x82\x90")
try:
    parser.parse(p)
    check("非法编码抛错", False, "未抛异常")
except ParseError:
    check("非法编码抛错", True)

# 2. 空文件 → 空节点列表
p = tmp / "empty.txt"
p.write_bytes(b"")
check("空文件", parser.parse(p) == [], parser.parse(p))

# 3. 纯 BOM 文件 → 空
p = tmp / "bom.txt"
p.write_bytes(b"\xef\xbb\xbf")
check("纯 BOM 文件", parser.parse(p) == [], parser.parse(p))

# 4. 无扩展名 → 按 txt 处理
p = tmp / "noext"
p.write_text("无扩展名内容", encoding="utf-8")
nodes = parser.parse(p)
check("无扩展名按 txt", len(nodes) == 1 and nodes[0].type == "paragraph", nodes)

# 5. 嵌套标题层级（# 到 ######）
p = tmp / "deep.md"
p.write_text("###### 六级标题", encoding="utf-8")
nodes = parser.parse(p)
check("六级标题", nodes[0].type == "heading" and nodes[0].meta["level"] == 6, nodes)

# 6. 超长代码块（含空行/特殊字符，不被误切）
code = "\n".join([f"line {i}  " for i in range(500)])
p = tmp / "longcode.md"
p.write_text(f"```\n{code}\n```", encoding="utf-8")
nodes = parser.parse(p)
check("超长代码块单节点", len(nodes) == 1 and nodes[0].type == "code", len(nodes))

# 7. 密集全角/不间断空格归一
n = clean_node(DocumentNode("paragraph", "　全角开头\u3000中间\u00a0结束　"))
check("密集特殊空白归一", n.text == "全角开头 中间 结束", repr(n.text))

# 8. 控制字符混入被剔除（含 \x00-\x1f 与零宽）
n = clean_node(DocumentNode("paragraph", "a\x00b\x1fc\u200bd\ufeffe"))
check("控制字符+零宽剔除", n.text == "abcde", repr(n.text))

# 9. 空行压缩边界：恰好 3 个空行 → 保留 1 个空行分隔
n = clean_node(DocumentNode("paragraph", "a\n\n\nb"))
check("三空行压缩", n.text == "a\n\nb", repr(n.text))

# 10. 表格式文本在非 table 节点也会被压缩空格（普通段落规则）
n = clean_node(DocumentNode("paragraph", "a   b | c   d"))
check("段落空格压缩", n.text == "a b | c d", repr(n.text))

# 11. 非法节点类型拒绝
try:
    DocumentNode("nope", "x")
    check("非法节点类型", False, "未抛异常")
except ValueError:
    check("非法节点类型", True)

# 12. 全角字符（非空白）不受影响
n = clean_node(DocumentNode("paragraph", "ＦＵＬＬＷＩＤＴＨ中文"))
check("全角字母保留", n.text == "ＦＵＬＬＷＩＤＴＨ中文", repr(n.text))

print("")
if fail == 0:
    print("===== R1 边界测试全部通过 =====")
else:
    print(f"===== 有 {fail} 项失败 =====")
    sys.exit(1)
