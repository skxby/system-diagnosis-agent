#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""沙箱（安全边界）的离线测试 —— test_sandbox.py（免费、秒级、不要 key）

**为什么安全边界必须有测试**：它的 bug 形态是"该拦的没拦"或"该放的被拦" ——
两者都不报错：
  · 漏拦 → 模型把密钥读进上下文，一切看起来正常
  · 误拦 → 真机部署时读不到自己的日志，你会以为是工具不会用

覆盖：
    [1] 路径闸门：穿越 / 兄弟目录前缀 / 大小写 / 符号链接 一律拦，合法路径一律放
    [2] 文件名闸门（Day 43 新增）：**路径在沙箱内**但名字敏感（.env / 私钥 / sqlite）也拒
    [3] 可配置根：默认 → 环境变量 → set_root 的优先级；**危险根拒绝**
    [4] 根里含敏感文件时**当场告警**（不是等模型读走）
    [5] 两道闸门独立（去掉一个另一个仍然拦）
"""
import os
import sys
import shutil
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import day32_function_calling as d32

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}{('  —— ' + str(detail)) if detail else ''}")


def denied(path):
    try:
        d32._safe_path(path)
        return None
    except ValueError as e:
        return str(e)


def main():
    original = d32.ALLOWED_ROOT
    tmp = tempfile.mkdtemp(prefix="sandbox_")
    try:
        os.makedirs(os.path.join(tmp, "logs"), exist_ok=True)
        with open(os.path.join(tmp, "logs", "app.log"), "w", encoding="utf-8") as f:
            f.write("2026-09-24 03:12:07 ERROR db connection refused\n")
        for sensitive in (".env", "id_rsa", "server.pem", "cache.sqlite", "credentials.json"):
            with open(os.path.join(tmp, "logs", sensitive), "w", encoding="utf-8") as f:
                f.write("SECRET=should-never-be-read\n")
        # 外面的"秘密"：用来验证穿越
        with open(os.path.join(tmp, "outside.env"), "w", encoding="utf-8") as f:
            f.write("DEEPSEEK_API_KEY=sk-should-never-be-read\n")

        # ── [1] 路径闸门 ──────────────────────────────────
        print("[1] 路径闸门")
        root, warns = d32.set_root(os.path.join(tmp, "logs"), allow_dangerous=True)
        check("合法文件放行", d32._safe_path("app.log").endswith("app.log"))
        check("上一级穿越被拦", denied("../outside.env") is not None)
        check("深穿越被拦", denied("../../../../etc/passwd") is not None)
        check("兄弟目录前缀被拦（startswith 判法的经典漏洞）",
              denied("../logs_evil/x.log") is not None)
        check("绝对路径越界被拦", denied(os.path.join(tmp, "outside.env")) is not None)
        check("大小写变体仍能放行（realpath 归一化）",
              d32._safe_path(os.path.join(root.upper(), "app.log")) is not None)
        if os.name != "nt":                      # 符号链接测试需要权限，Windows 上跳过
            link = os.path.join(tmp, "logs", "escape")
            try:
                os.symlink(os.path.join(tmp, "outside.env"), link)
                check("指向外部的软链接被拦", denied("escape") is not None)
            except OSError:
                check("指向外部的软链接被拦（跳过：建不了链接）", True)

        # ── [2] 文件名闸门（新增）─────────────────────────
        print("\n[2] 文件名闸门（根内也拒）")
        for name in (".env", "id_rsa", "server.pem", "cache.sqlite"):
            msg = denied(name)
            check(f"根内敏感文件被拒：{name}", msg is not None and "敏感清单" in msg,
                  (msg or "")[:40])

        # ── [3] 可配置根 + 危险根 ──────────────────────────
        print("\n[3] 可配置根")
        check("set_root 生效", os.path.realpath(d32.ALLOWED_ROOT) == os.path.realpath(root))
        info = d32.root_info()
        check("root_info 报告当前根与黑名单", info["allowed_root"] == os.path.realpath(root)
              and ".env" in info["sensitive_denied"])
        for bad in ({os.path.sep, "C:\\", "E:\\"},):
            for b in bad:
                if os.path.exists(b):
                    try:
                        d32.set_root(b)
                        check(f"危险根被拒绝：{b}", False, "竟然接受了")
                    except ValueError as e:
                        check(f"危险根被拒绝：{b}", "沙箱" in str(e))
        check("不存在的根报错", _raises(lambda: d32.set_root(os.path.join(tmp, "nope"))))
        # 环境变量优先级（只在模块加载时生效 —— 这里验证 root_info 能报出来源）
        info2 = d32.root_info()
        check("root_info 能报'根来自环境变量吗'", isinstance(info2["from_env"], bool))

        # ── [4] 根里含敏感文件要当场告警 ───────────────────
        print("\n[4] 根里的敏感文件告警")
        root2, warns2 = d32.set_root(os.path.join(tmp, "logs"), allow_dangerous=True)
        check("发现了根里的敏感文件并告警", len(warns2) >= 3, warns2[:3])
        clean = os.path.join(tmp, "clean")
        os.makedirs(clean, exist_ok=True)
        _, warns3 = d32.set_root(clean, allow_dangerous=True)
        check("干净的根不告警", warns3 == [])

        # ── [5] 两道闸门独立 ──────────────────────────────
        print("\n[5] 闸门独立")
        d32.set_root(os.path.join(tmp, "logs"), allow_dangerous=True)
        check("路径闸门抓的是越界（与文件名无关）", "越界" in (denied("../outside.env") or ""))
        check("文件名闸门抓的是名字（与越界无关）", "敏感清单" in (denied(".env") or ""))
    finally:
        d32.ALLOWED_ROOT = original
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 74)
    print(f"结果：{len(PASS)} 项通过，{len(FAIL)} 项失败")
    if FAIL:
        for f in FAIL:
            print(f"  ✗ {f}")
        sys.exit(1)
    print("安全边界是纯逻辑 + 临时目录 —— 免费、可复现，已进 CI 免费档。")


def _raises(fn):
    try:
        fn()
        return False
    except Exception:
        return True


if __name__ == "__main__":
    main()
