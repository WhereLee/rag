# -*- coding: utf-8 -*-
"""P1 目录体系验收 normal：建目录→上传进目录→列表→移动→重命名→删除空目录→检索隔离。"""
import sys, io, json, time, urllib.request
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
BASE = "http://127.0.0.1:8082"
fails = 0

def req(method, path, body=None, token=None, raw=False):
    r = urllib.request.Request(BASE + path, method=method)
    if body is not None:
        r.data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        r.add_header("Content-Type", "application/json; charset=utf-8")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    try:
        resp = urllib.request.urlopen(r, timeout=30)
        return resp.status, (resp.read() if raw else json.loads(resp.read().decode("utf-8")))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}

def check(name, cond, detail=""):
    global fails
    print(("[OK] " if cond else "[FAIL] ") + name + ("" if cond else " -> " + str(detail)))
    if not cond: fails += 1

# 1. 注册/登录（幂等：已存在则直接登录）
uname = "p1_dir_user_" + time.strftime("%H%M%S")
pwd = "Passw0rd1"
req("POST", "/api/auth/register", {"username": uname, "password": pwd, "role": "user"})
_, login = req("POST", "/api/auth/login", {"username": uname, "password": pwd})
tok = login.get("token", "")
check("登录", bool(tok), login)

# 2. 建目录 dir1 / dir2
st, d1 = req("POST", "/api/dirs", {"name": "目录A"}, tok)
check("建目录A", st == 200 and "id" in d1, (st, d1))
dir1 = d1.get("id")
st, d2 = req("POST", "/api/dirs", {"name": "目录B"}, tok)
dir2 = d2.get("id")
check("建目录B", st == 200 and "id" in d2, (st, d2))

# 3. 同名目录 → 409
st, r = req("POST", "/api/dirs", {"name": "目录A"}, tok)
check("同名目录 409", st == 409, (st, r))

# 4. 上传 test.txt 到目录A
import uuid
content = "生产服务器最低配置为 4 核 8G 内存，磁盘 100G。目录隔离标记 P1_MARK_A。"
fname = f"p1_{uuid.uuid4().hex[:8]}.txt"
fd = json.dumps({}).encode()
boundary = uuid.uuid4().hex
body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{fname}\"\r\n"
        f"Content-Type: text/plain\r\n\r\n{content}\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"dir_id\"\r\n\r\n{dir1}\r\n"
        f"--{boundary}--\r\n").encode("utf-8")
r = urllib.request.Request(BASE + "/api/files/upload", data=body, method="POST",
    headers={"Authorization": "Bearer " + tok,
             "Content-Type": f"multipart/form-data; boundary={boundary}"})
resp = urllib.request.urlopen(r, timeout=30)
up = json.loads(resp.read().decode("utf-8"))
check("上传到目录A", resp.status == 200 and "id" in up, up)
file_id = up.get("id")

# 5. 等待解析完成（轮询最多 60s）
st_ok = False
for _ in range(30):
    st, lst = req("GET", f"/api/files?page=1&pageSize=20&dir_id={dir1}", None, tok)
    for it in lst.get("items", []):
        if it["id"] == file_id and it["parse_status"] in ("success", "partial"):
            st_ok = True
    if st_ok: break
    time.sleep(2)
check("文件解析成功", st_ok)

# 6. 列表隔离：目录A有文件、目录B为空
st, la = req("GET", f"/api/files?dir_id={dir1}", None, tok)
check("目录A列表含文件", len(la.get("items", [])) == 1, la.get("items"))
st, lb = req("GET", f"/api/files?dir_id={dir2}", None, tok)
check("目录B列表为空", len(lb.get("items", [])) == 0, lb.get("items"))
st, lall = req("GET", "/api/files", None, tok)
check("全量列表仍可用", len(lall.get("items", [])) >= 1)

# 7. 移动到目录B
st, r = req("PATCH", f"/api/files/{file_id}/move", {"dir_id": dir2}, tok)
check("移动文件到目录B", st == 200, (st, r))
st, la2 = req("GET", f"/api/files?dir_id={dir1}", None, tok)
st, lb2 = req("GET", f"/api/files?dir_id={dir2}", None, tok)
check("移动后目录A空/目录B有", len(la2.get("items", [])) == 0 and len(lb2.get("items", [])) == 1,
      (len(la2.get("items", [])), len(lb2.get("items", []))))

# 8. 重命名目录
st, r = req("PATCH", f"/api/dirs/{dir1}", {"name": "目录A-改"}, tok)
check("重命名目录", st == 200, (st, r))

# 9. 删空目录（先移走文件后目录B非空 → 409；新建空目录删除 → 200）
st, r = req("DELETE", f"/api/dirs/{dir2}", None, tok)
check("非空目录删除 409", st == 409, (st, r))
st, d3 = req("POST", "/api/dirs", {"name": "临时空目录"}, tok)
st, r = req("DELETE", f"/api/dirs/{d3.get('id')}", None, tok)
check("空目录删除 200", st == 200, (st, r))

# 10. 检索隔离（Python 直连检索层：目录B的块；全库含目录B块）
sys.path.insert(0, r"c:\Users\lrs\Desktop\py\rag\rag-python\src")
import config
from db import pg_store
row = pg_store.query_one("SELECT id AS user_id FROM kb_user WHERE username=%s", (uname,))
uid = row["user_id"]
from retrieval.retriever import retrieve
in_dir = retrieve(uid, "P1_MARK_A", top_k=3, dir_id=dir2)
check("目录限定检索命中", len(in_dir) >= 1 and any(c.file_id == file_id for c in in_dir), [(c.file_id, c.score) for c in in_dir])
wrong_dir = retrieve(uid, "P1_MARK_A", top_k=3, dir_id=dir1)
check("错误目录检索为空", len(wrong_dir) == 0, [c.file_id for c in wrong_dir])
all_hits = retrieve(uid, "P1_MARK_A", top_k=3)
check("全库检索仍命中", len(all_hits) >= 1, [c.file_id for c in all_hits])

print("P1 normal:", "PASS" if fails == 0 else f"{fails} FAILED")
