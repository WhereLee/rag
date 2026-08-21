#!/bin/bash
# 部署辅助：复现解析子进程，抓真实报错
set -e
echo "== find blob file =="
find /opt/rag/data -name "*.txt" -o -name "*.pdf" 2>/dev/null | head -5
BLOB=$(find /opt/rag/data -name "5d924adef4e34957b9379bd7bc7f1883*" 2>/dev/null | head -1)
echo "BLOB=$BLOB"
if [ -z "$BLOB" ]; then
  BLOB=$(find /opt/rag/data -name "*.txt" 2>/dev/null | head -1)
fi
echo "== run child script =="
cd /opt/rag/rag-python/src
/opt/rag/rag-python/.venv/bin/python -c "
import sys, importlib
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')
sys.path.insert(0, '/opt/rag/rag-python/src')
path, cls_name = '$BLOB', 'TxtMdParser'
mod = importlib.import_module('ingest.parser')
parser = getattr(mod, cls_name)()
nodes = parser.parse(path)
print('PARSED_OK nodes=', len(nodes))
" 2>&1 | tail -20
echo "== DONE =="
