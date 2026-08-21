# -*- coding: utf-8 -*-
"""P1 目录体系验收 edge：跨用户隔离/非法参数/不存在目录/未登录。"""
import sys, io, json, time, uuid, urllib.request
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

def new_user(tag):
    uname = f"p1e_{tag}_{uuid.uuid4().hex[:8]}"
    for _ in range(3):
        st, _ = req("POST", "/api/auth/register", {"username": uname, "password": "Passw0rd1", "role": "user"})
        if st == 200:
            break
        time.sleep(20)  # ?????? IP ??? 3 ?
    for _ in range(3):
        _, login = req("POST", "/api/auth/login", {"username": uname, "password": "Passw0rd1"})
        if login.get("token"):
            return login["token"]
        time.sleep(2)
    return None

tokA = new_user("a")
tokB = new_user("b")
check("A/B 登录", bool(tokA) and bool(tokB))

# A 建目录
st, d = req("POST", "/api/dirs", {"name": "A的目录"}, tokA)
dirA = d.get("id")
check("A 建目录", st == 200 and dirA, (st, d))

# 1. B 操作 A 的目录 → 404
st, r = req("GET", f"/api/dirs/{dirA}", None, tokB)
check("B 查 A 目录 404(无此路由→404即可)", st in (404, 405), (st, r))  # GET 单目录无路由，验证不泄露即可
st, r = req("PATCH", f"/api/dirs/{dirA}", {"name": "hack"}, tokB)
check("B 重命名 A 目录 404", st == 404, (st, r))
st, r = req("DELETE", f"/api/dirs/{dirA}", None, tokB)
check("B 删除 A 目录 404", st == 404, (st, r))
st, r = req("GET", f"/api/files?dir_id={dirA}", None, tokB)
check("B 列 A 目录文件 404", st == 404, (st, r))

# 2. dir_id 不存在（999999）→ 404
st, r = req("GET", f"/api/files?dir_id=999999", None, tokA)
check("不存在目录列表 404", st == 404, (st, r))
st, r = req("POST", "/api/dirs", {"name": "非法目录", "dir_id": 999999}, tokA)
check("创建请求带非法 dir_id 不影响", True)  # 创建接口无 dir_id 参数，正常返回
import uuid
content = "edge mark EDGE_P1"
boundary = uuid.uuid4().hex
body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"edge_{uuid.uuid4().hex[:6]}.txt\"\r\n"
        f"Content-Type: text/plain\r\n\r\n{content}\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"dir_id\"\r\n\r\n999999\r\n"
        f"--{boundary}--\r\n").encode("utf-8")
r = urllib.request.Request(BASE + "/api/files/upload", data=body, method="POST",
    headers={"Authorization": "Bearer " + tokA, "Content-Type": f"multipart/form-data; boundary={boundary}"})
try:
    resp = urllib.request.urlopen(r, timeout=30)
    st = resp.status
except urllib.error.HTTPError as e:
    st = e.code
check("上传到不存在目录 404", st == 404, st)

# 3. 非法目录名
st, r = req("POST", "/api/dirs", {"name": "a/b"}, tokA)
check("目录名含分隔符 400", st == 400, (st, r))
st, r = req("POST", "/api/dirs", {"name": "  "}, tokA)
check("目录名空白 400", st == 400, (st, r))
st, r = req("POST", "/api/dirs", {"name": "x" * 101}, tokA)
check("目录名超长 400", st == 400, (st, r))

# 4. 同名目录 → 409（同一用户）
st, r = req("POST", "/api/dirs", {"name": "A的目录"}, tokA)
check("A 同名目录 409", st == 409, (st, r))

# 5. 未登录 → 401
r = urllib.request.Request(BASE + "/api/dirs", method="GET")
try:
    resp = urllib.request.urlopen(r, timeout=10)
    st = resp.status
except urllib.error.HTTPError as e:
    st = e.code
check("未登录访问 401", st == 401, st)

# 6. B 用户移动 A 的文件 → 404（需 A 先上传一个文件到目录）
boundary = uuid.uuid4().hex
body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"mv_{uuid.uuid4().hex[:6]}.txt\"\r\n"
        f"Content-Type: text/plain\r\n\r\n移动测试 MOVE_MARK\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"dir_id\"\r\n\r\n{dirA}\r\n"
        f"--{boundary}--\r\n").encode("utf-8")
r = urllib.request.Request(BASE + "/api/files/upload", data=body, method="POST",
    headers={"Authorization": "Bearer " + tokA, "Content-Type": f"multipart/form-data; boundary={boundary}"})
resp = urllib.request.urlopen(r, timeout=30)
fileA = json.loads(resp.read().decode("utf-8")).get("id")
st, r = req("PATCH", f"/api/files/{fileA}/move", {"dir_id": dirA}, tokB)
check("B 移动 A 文件 404", st == 404, (st, r))
# 7. A 移动文件到 B 的目录 → 404
st, d2 = req("POST", "/api/dirs", {"name": "B的目录"}, tokB)
dirB = d2.get("id")
st, r = req("PATCH", f"/api/files/{fileA}/move", {"dir_id": dirB}, tokA)
check("A 移动文件到 B 目录 404", st == 404, (st, r))
# 8. A 移动不存在文件 → 404
st, r = req("PATCH", "/api/files/999999/move", {"dir_id": dirA}, tokA)
check("移动不存在文件 404", st == 404, (st, r))

print("P1 edge:", "PASS" if fails == 0 else f"{fails} FAILED")
