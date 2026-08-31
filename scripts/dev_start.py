# -*- coding: utf-8 -*-
"""IDE 终端开发启动：注入 .env 环境变量后前台运行服务（无弹窗、stdout 直连终端）。
用法（在 Qoder IDE 终端里）：
  python scripts\\dev_start.py java      # Java 8093（mvn spring-boot:run）
  python scripts\\dev_start.py python    # Python 8094（uvicorn agent_draft）
"""
import io
import os
import subprocess
import sys

ROOT = r"c:\Users\lrs\Desktop\py\rag\club-agent"


def load_env():
    """读取 club-agent/.env 注入子进程环境（数据库/JWT/LLM key 等）"""
    vars_map = {}
    p = os.path.join(ROOT, ".env")
    if not os.path.exists(p):
        print(f"[dev_start] 未找到 {p}，使用系统环境变量", file=sys.stderr)
        return dict(os.environ)
    for line in io.open(p, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        vars_map[k.strip()] = v.strip().strip('"')
    return dict(os.environ, **vars_map)


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "java"
    env = load_env()
    if which == "java":
        print("[dev_start] 启动 Java 8093（mvn spring-boot:run，Ctrl+C 停止）")
        subprocess.call("mvn spring-boot:run", shell=True, env=env,
                        cwd=os.path.join(ROOT, "java"))
    elif which == "python":
        print("[dev_start] 启动 Python 8094（uvicorn，Ctrl+C 停止）")
        subprocess.call(
            "python -m uvicorn agent_draft.main:app --app-dir src --host 127.0.0.1 --port 8094",
            shell=True, env=env, cwd=os.path.join(ROOT, "python"))
    else:
        print("用法: python dev_start.py java|python", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
