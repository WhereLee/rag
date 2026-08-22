#!/bin/bash
# 提取 SSE meta 事件 + 引用 + delta 文本
F=/tmp/qa_smoke.txt
echo "== meta 事件 =="
grep -o '{"type":"meta"[^}]*}' $F | head -1
echo "== citations =="
grep -o '"citations":\[[^]]*\]' $F | head -1
echo "== delta 拼接 =="
grep -o '"type":"delta","text":"[^"]*"' $F | sed 's/.*"text":"//;s/"$//' | tr -d '\n' | head -c 400
echo ""
echo "== DONE =="
