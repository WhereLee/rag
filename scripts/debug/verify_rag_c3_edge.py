# -*- coding: utf-8 -*-
"""C3 边界测试：用户隔离 / 软删不可搜 / 空库 / rerank 降级 / 无匹配 / top_k 限制。
前置：网关(8082) + 新版 parse worker 已启动。用法: python scripts/debug/verify_rag_c3_edge.py
"""
import random
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "rag-python" / "src"))

import psycopg
import psycopg.rows
import requests

import config
from retrieval.retriever import retrieve

BASE = "http://localhost:8082"
fail = 0


def check(name, cond, detail=""):
    global fail
    if cond:
        print(f"[OK] {name}")
    else:
        print(f"[FAIL] {name} -> {detail}")
        fail += 1


def new_user(prefix="c3e"):
    u = f"{prefix}_{uuid.uuid4().hex[:10]}"
    h = {"X-Forwarded-For": f"198.51.100.{random.randint(10, 240)}"}
    r0 = requests.post(f"{BASE}/api/auth/register", json={"username": u, "password": "Passw0rd1"},
                       headers=h, timeout=10)
    assert r0.status_code == 200, f"register failed: {r0.status_code} {r0.text}"
    r = requests.post(f"{BASE}/api/auth/login", json={"username": u, "password": "Passw0rd1"},
                      headers=h, timeout=10).json()
    assert "token" in r, r
    return {"user": u, "headers": {"Authorization": f"Bearer {r['token']}"}}


def db():
    conn = psycopg.connect(config.PG_DSN, row_factory=psycopg.rows.dict_row)
    return conn


def upload(u, path, filename):
    with open(path, "rb") as f:
        r = requests.post(f"{BASE}/api/files/upload",
                          files={"file": (filename, f)}, headers=u["headers"], timeout=30)
    assert r.status_code == 200, f"upload failed: {r.status_code} {r.text}"
    return r.json()


def wait_task(file_id, timeout=120):
    deadline = time.time() + timeout
    with db() as conn:
        while time.time() < deadline:
            row = conn.execute(
                "SELECT status, chunk_count FROM parse_tasks WHERE file_id=%s",
                (file_id,)).fetchone()
            if row and row["status"] not in ("pending", "parsing"):
                return row
            time.sleep(2)
    return None


def user_id_of(file_id):
    with db() as conn:
        return conn.execute("SELECT user_id FROM user_file WHERE id=%s", (file_id,)).fetchone()["user_id"]


# ---------- 0. 准备：A 用户上传带独特标记的文档，B 用户空库 ----------
tmp = Path(__file__).resolve().parents[2] / "logs" / "c3e_tmp"
tmp.mkdir(exist_ok=True)
a = new_user("c3eA")
b = new_user("c3eB")
p = tmp / "秘密资料.txt"
p.write_text("机密文档内容：包含独特标记 SECRETTOKEN_9371。该文档仅供特定用户检索使用。"
             "文档主题为内部研发规划，涉及技术选型与进度安排。", encoding="utf-8")
f = upload(a, p, "秘密资料.txt")
t = wait_task(f["id"])
check("前置 success", t and t["status"] == "success", t)
uid_a = user_id_of(f["id"])

# ---------- 1. 用户隔离：B 搜不到 A 的块 ----------
with db() as conn:
    uid_b = conn.execute("SELECT id FROM kb_user WHERE username=%s", (b["user"],)).fetchone()["id"]
res_b = retrieve(uid_b, "SECRETTOKEN_9371", top_k=5)
check("用户隔离：B 无结果", len(res_b) == 0, [r.filename for r in res_b])
res_a = retrieve(uid_a, "SECRETTOKEN_9371", top_k=5)
check("A 自己可搜到", any(r.filename == "秘密资料.txt" for r in res_a),
      [r.filename for r in res_a])

# ---------- 2. rerank 降级：模型异常 → 返回 RRF 排序且标记 ----------
import retrieval.reranker as rr_mod
real_rerank = rr_mod.rerank
rr_mod.rerank = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("simulated rerank outage"))
try:
    res = retrieve(uid_a, "SECRETTOKEN_9371", top_k=5)
    check("rerank 降级仍返回结果", len(res) > 0, len(res))
    check("降级结果标记 reranked=False", all(not r.reranked for r in res))
finally:
    rr_mod.rerank = real_rerank

# ---------- 3. 无关查询：rerank 精排后不泄露敏感文件（库中混入无关文档，块数 > top_k） ----------
p2 = tmp / "团队活动.txt"
p2.write_text("团队建设活动记录：季度团建在郊外举行，包含徒步、烧烤与桌游环节。"
              "活动预算由行政统一申请，报销流程需要部门负责人审批。\n\n"
              "上季度团建选择在郊区营地举办，共有四十余名同事参加，大家分成四组进行徒步竞赛，"
              "优胜组获得了定制奖杯。晚餐安排了户外烧烤，食材由行政提前采购并确认了过敏清单。\n\n"
              "本次团建满意度调查显示，百分之九十以上的参与者给出了好评，建议后续增加亲子场次。", encoding="utf-8")
p3 = tmp / "食堂菜单.txt"
p3.write_text("本周食堂菜单：周一红烧肉配米饭，周二清蒸鱼配时蔬，周三牛肉面，"
              "周四番茄鸡蛋盖饭，周五饺子。用餐时间中午十一点半到一点。\n\n"
              "食堂近期新增了低油低盐窗口，同时提供粗粮主食选择，深受同事欢迎。"
              "每周菜单会提前在内部系统公布，并接受意见反馈以便调整。\n\n"
              "供应商由后勤部门定期招标确定，食材每日新鲜配送，厨房定期接受卫生检查。", encoding="utf-8")
f2 = upload(a, p2, "团队活动.txt")
f3 = upload(a, p3, "食堂菜单.txt")
check("无关文档前置 success",
      wait_task(f2["id"]) and wait_task(f2["id"])["status"] == "success"
      and wait_task(f3["id"]) and wait_task(f3["id"])["status"] == "success")
res_irrelevant = retrieve(uid_a, "火星殖民与外星生命的行星大气条件", top_k=5)
res_relevant = retrieve(uid_a, "内部研发规划与进度安排", top_k=5)
# 无关查询：低分剔除可能直接返回空（更严格，不泄露）；非空时 top1 分数必须显著低于相关查询
check("无关查询 top1 分数显著低于相关查询",
      (not res_irrelevant) or (res_relevant and res_irrelevant[0].score < res_relevant[0].score * 0.6),
      f"irr={res_irrelevant[0].score if res_irrelevant else 'EMPTY'} "
      f"rel={res_relevant[0].score if res_relevant else None}")

# ---------- 4. top_k 限制 ----------
res = retrieve(uid_a, "SECRETTOKEN_9371", top_k=3)
check("top_k=3 数量限制", len(res) <= 3, len(res))
res = retrieve(uid_a, "内部研发规划", top_k=10)
check("top_k=10 数量限制", len(res) <= 10, len(res))

# ---------- 5. 软删后不可搜 ----------
r = requests.delete(f"{BASE}/api/files/{f['id']}", headers=a["headers"], timeout=10)
check("软删接口 200", r.status_code == 200, r.text)
res = retrieve(uid_a, "SECRETTOKEN_9371", top_k=5)
check("软删文件不可搜", all(r.filename != "秘密资料.txt" for r in res),
      [r.filename for r in res])
with db() as conn:
    remains = conn.execute("SELECT count(*) AS n FROM rag_chunk WHERE file_id=%s",
                           (f["id"],)).fetchone()["n"]
check("块表未物理删除（查询期过滤）", remains > 0, remains)

# ---------- 6. 空库用户：检索返回空 ----------
res = retrieve(uid_b, "随便什么问题", top_k=5)
check("空库返回空", len(res) == 0)

print(f"\nC3 edge: {'PASS' if fail == 0 else f'{fail} FAILED'}")
sys.exit(1 if fail else 0)
