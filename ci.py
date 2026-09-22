#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""作品 3 · 本地 CI（回归门禁）—— ci.py

分档思路和评测的 L1/L2 一致：**能免费跑的永远免费跑，要花钱的按需跑**。

    L1（默认）—— 免费、秒级到分钟级、不要 key：
        ① 语法编译全项目         最便宜、最该先查（一行 typo 就该在这里死）
        ② 图能搭起来（--graph）   抓 import / 节点注册 / 类型这类"跑一下就现形"的问题
        ③ 预算离线测试（26 项）   含并发超卖、死锁回归、重试记账
        ④ 评测 L1 离线自检        判据表自洽 + fixture 在不在
        ⑤ 工具层回归（pytest）    用**旧 venv** 的解释器跑（两套解释器是已知代价）
    L2（--tier l2）—— 要 key、要钱：
        ⑥ 在线评测（默认只跑免费用例，可用 --only/--runs 调整）

**为什么要有这个东西**：口头约定"记得跑测试"等于没有约定。
门禁的意义是让**机器**回答"能不能提交"，而不是靠人记 —— 所以我把它接到了 git pre-commit 上。

用法：
    python ci.py                 # L1（任何 python 都能跑它，见下面的解释器探测）
    python ci.py --tier l2 --only G03,G07,G09
    python ci.py --list
    python ci.py --verbose       # 失败时打出完整输出
"""
import os
import re
import sys
import json
import time
import argparse
import subprocess

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT = os.path.dirname(os.path.abspath(__file__))
os.chdir(PROJECT)
REPORT = os.path.join(PROJECT, "ci_report.json")

OK, FAIL, SKIP = "ok", "fail", "skip"


# ══════════════════════════════════════════════════════════════════
# 解释器探测：**ci.py 自己只用标准库**，所以任何 python 都能跑它；
# 每一步要用的解释器再按需找（两套 venv 的现实：编排层一套、工具层一套）
# ══════════════════════════════════════════════════════════════════
def _runs(py, module):
    """这个解释器能 import 到指定模块吗（不能就说明找错了）"""
    if not py or not os.path.exists(py):
        return False
    try:
        r = subprocess.run([py, "-c", f"import {module}"], capture_output=True, timeout=60)
        return r.returncode == 0
    except Exception:
        return False


def find_python(module, env_var, candidates):
    """按 环境变量 → 候选路径 → 当前解释器 的顺序找一个能 import 目标模块的解释器"""
    tries = [os.environ.get(env_var)] + candidates + [sys.executable]
    for py in tries:
        if _runs(py, module):
            return py
    return None


def agent_python():
    """编排层（langgraph / mcp / openai）—— 项目向上两级就是 .venv-agent"""
    up1 = os.path.dirname(PROJECT)
    up2 = os.path.dirname(up1)
    return find_python("langgraph", "CI_PY_AGENT", [
        os.path.join(up2, ".venv-agent", "Scripts", "python.exe"),
        os.path.join(up2, "Scripts", "python.exe"),
        os.path.join(up1, "Scripts", "python.exe"),
    ])


def tool_python():
    """工具层 + 老测试（mcp / pytest / openai）—— 项目上一级就是 .venv"""
    up1 = os.path.dirname(PROJECT)
    up2 = os.path.dirname(up1)
    return find_python("pytest", "CI_PY_OLD", [
        os.path.join(up1, "Scripts", "python.exe"),
        os.path.join(up2, ".venv", "Scripts", "python.exe"),
    ])


# ══════════════════════════════════════════════════════════════════
# 每一步 = 一条命令 + 一条"怎么判定通过"的规则
# ══════════════════════════════════════════════════════════════════
def run_step(name, cmd, expect, timeout=900):
    """跑一步，返回 (结果, 摘要, 耗时, 输出)"""
    t0 = time.perf_counter()
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        out = (r.stdout or b"").decode("utf-8", "replace") + \
              (r.stderr or b"").decode("utf-8", "replace")
        code = r.returncode
    except subprocess.TimeoutExpired:
        return FAIL, f"超时（>{timeout}s）", time.perf_counter() - t0, ""
    except Exception as e:
        return FAIL, f"{type(e).__name__}: {e}", time.perf_counter() - t0, ""

    dt = time.perf_counter() - t0
    kind, pattern, summary = expect
    if kind == "rc":
        good = (code == 0)
        detail = summary if good else f"退出码 {code}"
    else:                                   # 关键字判定（比只看退出码更精确）
        m = re.search(pattern, out)
        good = m is not None
        detail = summary.format(*m.groups()) if (good and m.groups()) else \
                 (summary if good else f"没找到「{pattern}」")
    return (OK if good else FAIL), detail, dt, out


def l1_steps(py_agent, py_tool):
    steps = [
        ("语法编译全项目", [py_agent, "-m", "compileall", "-q", PROJECT], ("rc", None, "全部 .py 编译通过")),
        ("图能搭起来", [py_agent, "day36_graph_mcp.py", "--graph"],
         ("kw", r"graph TD", "mermaid 图已生成（import/注册/类型都过）")),
        ("预算离线测试", [py_agent, "test_budget.py"],
         ("kw", r"(\d+) 项通过，0 项失败", "{} 项通过（并发超卖/死锁回归/重试记账）")),
        ("评测 L1 离线自检", [py_agent, "eval_agent.py", "--offline"],
         ("kw", r"L1 结论：全部通过", "判据表自洽 + fixture 齐备")),
    ]
    if py_tool:
        steps.append(("工具层回归测试", [py_tool, "-m", "pytest", "test_day32.py", "-q"],
                      ("kw", r"(\d+) passed", "{} 项通过（day32 工具层）")))
    return steps


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", choices=["l1", "l2", "all"], default="l1")
    ap.add_argument("--only", default=None, help="L2 只跑指定评测用例，如 G03,G07,G09")
    ap.add_argument("--runs", type=int, default=1, help="L2 每条用例跑几次")
    ap.add_argument("--verbose", action="store_true", help="失败时打出完整输出")
    ap.add_argument("--quiet", action="store_true", help="只在失败时输出（给 git hook 用）")
    ap.add_argument("--list", action="store_true", help="列出所有步骤")
    args = ap.parse_args()

    py_agent, py_tool = agent_python(), tool_python()

    if args.list:
        print(f"项目：{PROJECT}")
        print(f"编排解释器：{py_agent or '（找不到：需要能 import langgraph）'}")
        print(f"工具解释器：{py_tool or '（找不到：需要能 import pytest）'}")
        print("\nL1（免费）：语法编译 / 图能搭起来 / 预算离线测试 / 评测离线自检 / 工具层回归")
        print("L2（要 key）：在线评测（默认 --only G03,G07,G09 —— 全是 0~1 次调用的便宜用例）")
        return 0

    steps = l1_steps(py_agent, py_tool)
    if args.tier in ("l2", "all"):
        eval_cmd = [py_agent or sys.executable, "eval_agent.py"]
        if args.only:
            eval_cmd += ["--only", args.only]
        else:
            eval_cmd += ["--only", "G03,G07,G09"]     # 默认只跑便宜的那几条
        if args.runs > 1:
            eval_cmd += ["--runs", str(args.runs)]
        steps = steps + [("在线评测（花小钱）", eval_cmd,
                          ("kw", r"通过率：(\d+/\d+)", "通过率 {}"))]

    if not args.quiet:
        print("=" * 78)
        print(f"作品 3 · 本地 CI（tier={args.tier}）   {PROJECT}")
        print("=" * 78)
        if not py_agent:
            print("⚠️  找不到能 import langgraph 的解释器：请设环境变量 CI_PY_AGENT")
        if not py_tool:
            print("⚠️  找不到能 import pytest 的解释器：工具层回归会被**跳过**（设 CI_PY_OLD 可指定）")

    results, t_all = [], time.perf_counter()
    for name, cmd, expect in steps:
        if name == "工具层回归测试" and not py_tool:
            results.append({"step": name, "result": SKIP, "detail": "没有 pytest 解释器", "seconds": 0})
            if not args.quiet:
                print(f"  ⏭  {name}：跳过（没有 pytest 解释器）")
            continue
        if not py_agent and cmd[0] not in (py_tool,):
            results.append({"step": name, "result": SKIP, "detail": "没有 langgraph 解释器", "seconds": 0})
            continue
        if not args.quiet:
            print(f"  ⏳ {name} …", end="", flush=True)
        res, detail, dt, out = run_step(name, cmd, expect)
        results.append({"step": name, "result": res, "detail": detail,
                        "seconds": round(dt, 1)})
        mark = {"ok": "✅", "fail": "❌", "skip": "⏭"}[res]
        if args.quiet:
            if res == FAIL:
                print(f"{mark} {name}：{detail}")
        else:
            print(f"\r  {mark} {name}：{detail}（{dt:.1f}s）")
        if res == FAIL and args.verbose and out:
            print("    " + "-" * 70)
            print("\n".join("    " + ln for ln in out.strip().splitlines()[-25:]))

    total = time.perf_counter() - t_all
    failed = [r for r in results if r["result"] == FAIL]
    skipped = [r for r in results if r["result"] == SKIP]

    if not args.quiet:
        print("-" * 78)
        print(f"结果：{len(results) - len(failed) - len(skipped)} 通过 / "
              f"{len(failed)} 失败 / {len(skipped)} 跳过，总计 {total:.1f}s")
    if failed:
        print(f"\n❌ 门禁不放行（{len(failed)} 步失败）：")
        for r in failed:
            print(f"   · {r['step']}：{r['detail']}")
        print("   排查：python ci.py --verbose")
    elif not args.quiet:
        print("✅ 门禁放行")

    with open(REPORT, "w", encoding="utf-8") as f:
        json.dump({"tier": args.tier, "when": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "total_seconds": round(total, 1), "results": results},
                  f, ensure_ascii=False, indent=2)
    if not args.quiet:
        print(f"报告：{REPORT}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
