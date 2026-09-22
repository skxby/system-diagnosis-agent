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


def _venv_candidates():
    """两个 venv 可能出现的位置（**按"能 import 到目标模块"来辨别身份**，不靠名字猜）。

    为什么要列这么多：作品代码目前住在 `.venv\\作品3_诊断Agent`（venv 里面），
    一旦按"交付化"搬到 `E:\\ai学习\\作品3_诊断Agent`，相对层级就变了 ——
    这里提前把两种布局都列上，搬家时 CI 不会突然找不到解释器。
    """
    up1 = os.path.dirname(PROJECT)
    up2 = os.path.dirname(up1)
    out = []
    for base in (up2, up1, os.path.join(up2, ".venv"), os.path.join(up2, ".venv-agent")):
        for name in (".venv-agent", ".venv", ""):
            p = os.path.join(base, name, "Scripts", "python.exe") if name else \
                os.path.join(base, "Scripts", "python.exe")
            if p not in out:
                out.append(p)
    return out


def agent_python():
    """编排层（langgraph / mcp / openai）"""
    return find_python("langgraph", "CI_PY_AGENT", _venv_candidates())


def tool_python():
    """工具层 + 老测试（mcp / pytest / openai）"""
    return find_python("pytest", "CI_PY_OLD", _venv_candidates())


# ══════════════════════════════════════════════════════════════════
# 每一步 = 一条命令 + 一条"怎么判定通过"的规则
# ══════════════════════════════════════════════════════════════════
def run_step(name, cmd, expect, timeout=900):
    """跑一步，返回 (结果, 摘要, 耗时, 输出)

    ★ 必须给子进程**固定 UTF-8**（实测踩到的真问题，而且是"门禁自身"的 bug）：
    门禁的成败一度取决于"跑门禁的人有没有设 PYTHONIOENCODING" ——
    手动跑（设了 utf-8）12 步全绿；`git commit` 触发钩子（没设）时 5 步失败。
    根因：子进程按 Windows 控制台代码页（cp936）输出，而这里按 UTF-8 解码。
    两种失败形态都出现过：编码不上的字符直接抛 `UnicodeEncodeError`（✓/✗ 不在 GBK 里），
    编码得上的中文变成乱码 → 关键字匹配不上。
    ➡️ **门禁的结果不允许取决于环境变量**，所以在这一处一次性钉死。
    """
    t0 = time.perf_counter()
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, env=env)
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
        # 最便宜、最致命的一条放最前面：密钥/产物**一次都不许进暂存区**
        # （为什么要有它：我写 .gitignore 时踩过"行尾注释导致规则全失效"，
        #   差点把 .env 提交上去 —— 规则靠不住的时候，就再加一道机器检查）
        ("敏感文件不进暂存区", [py_agent, "ci.py", "--check-staged"],
         ("rc", None, "暂存区无密钥/checkpoints/traces/报告")),
        # ★ 门禁自身的自检（实测踩到）：门禁的成败一度取决于"跑门禁的人有没有设
        #   PYTHONIOENCODING" —— 手动跑全绿、git 钩子触发时 5 步失败。
        #   这里主动让子进程输出 ✓（GBK 编码不了它）：只有 run_step 真的给子进程
        #   固定了 UTF-8，这一步才会通过。**门禁的结果不允许取决于环境变量。**
        ("门禁不依赖控制台编码", [py_agent, "-c", "print('\u2713 编码自检 ok')"],
         ("kw", r"✓ 编码自检 ok", "✓ 在 GBK 控制台下也能正确输出（子进程已强制 UTF-8）")),
        ("语法编译全项目", [py_agent, "-m", "compileall", "-q", PROJECT], ("rc", None, "全部 .py 编译通过")),
        ("图能搭起来", [py_agent, "day36_graph_mcp.py", "--graph"],
         ("kw", r"graph TD", "mermaid 图已生成（import/注册/类型都过）")),
        ("预算离线测试", [py_agent, "test_budget.py"],
         ("kw", r"(\d+) 项通过，0 项失败", "{} 项通过（并发超卖/死锁回归/重试记账）")),
        ("真机取证与规则测试", [py_agent, "test_collect.py"],
         ("kw", r"(\d+) 项通过，0 项失败", "{} 项通过（阈值边界/缺数据不误报/只读性/dump 解析）")),
        ("沙箱安全边界测试", [py_agent, "test_sandbox.py"],
         ("kw", r"(\d+) 项通过，0 项失败", "{} 项通过（穿越/敏感文件名/危险根）")),
        ("趋势与预测测试", [py_agent, "test_trend.py"],
         ("kw", r"(\d+) 项通过，0 项失败", "{} 项通过（拟合精度/不给预测/历史裁剪）")),
        ("成本控制测试", [py_agent, "test_cost.py"],
         ("kw", r"(\d+) 项通过，0 项失败", "{} 项通过（指纹/缓存/分级/账本限额）")),
        ("告警收敛测试", [py_agent, "test_converge.py"],
         ("kw", r"(\d+) 项通过，0 项失败",
          "{} 项通过（完整性不变量/弱边不归组/不编根因/图自检/收敛真变短）")),
        ("因果图自检", [py_agent, "converge.py", "--check-graph"],
         ("kw", r"因果图自检通过", "无环、规则齐全、无反例矛盾（图错了归因就是错的）")),
        ("误报反馈与抑制测试", [py_agent, "test_feedback.py"],
         ("kw", r"(\d+) 项通过，0 项失败",
          "{} 项通过（不丢发现/严重度天花板/过期/过宽拦截/撤销/阈值建议）")),
        ("抑制规则自检", [py_agent, "feedback.py", "--check"],
         ("kw", r"抑制规则自检通过", "全部有理由、有有效期、范围合法（不许永久抑制）")),
        ("安全审计测试", [py_agent, "test_audit.py"],
         ("kw", r"(\d+) 项通过，0 项失败",
          "{} 项通过（fail-closed/不记内容不记密钥/哈希链查篡改/轮转有界）")),
        ("审计哈希链自检", [py_agent, "audit.py", "--verify"],
         ("kw", r"哈希链校验通过", "记录未被改动（改内容/删除/换序都会断链）")),
        # ★ 补上门禁的盲区：原来**没有任何一步 import server.py**，
        #   所以"用了 sys 却没 import sys"这种错能活 4 个提交（MCP server 一直是死的）。
        ("MCP server 能导入且工具齐全",
         [py_agent, "-c",
          "import asyncio, server; "
          "print('TOOLS=' + ','.join(sorted(t.name for t in "
          "asyncio.run(server.mcp.list_tools()))))"],
         ("kw", r"TOOLS=.*read_log.*system_snapshot",
          "7 个工具都注册上了（import 期就崩的错再也藏不住）")),
        ("评测 L1 离线自检", [py_agent, "eval_agent.py", "--offline"],
         ("kw", r"L1 结论：全部通过", "判据表自洽 + fixture 齐备")),
    ]
    if py_tool:
        steps.append(("工具层回归测试", [py_tool, "-m", "pytest", "test_day32.py", "-q"],
                      ("kw", r"(\d+) passed", "{} 项通过（day32 工具层）")))
    return steps


# 不许进仓的东西（正则）：密钥文件、checkpointer 落盘、轨迹、生成的报告
FORBIDDEN = [
    (r"(^|/)\.env$", "密钥文件 .env"),
    (r"checkpoints.*\.sqlite$", "checkpointer 落盘文件"),
    (r"(^|/)traces/", "轨迹/报告目录"),
    (r"eval_agent_report_.*\.csv$", "评测报告 CSV"),
    (r"ci_report\.json$", "CI 报告"),
]


def check_staged():
    """暂存区里有没有不该进仓的东西 —— 给 pre-commit 用的硬检查。"""
    try:
        r = subprocess.run(["git", "diff", "--cached", "--name-only"],
                           capture_output=True, timeout=60)
    except Exception as e:
        print(f"（不是 git 仓库或 git 不可用，跳过：{type(e).__name__}）")
        return 0
    if r.returncode != 0:
        print("（git 不可用，跳过）")
        return 0
    staged = (r.stdout or b"").decode("utf-8", "replace").splitlines()
    bad = []
    for path in staged:
        for pattern, what in FORBIDDEN:
            if re.search(pattern, path.replace("\\", "/")):
                bad.append(f"{path}（{what}）")
    if bad:
        print("❌ 暂存区出现不该进仓的文件：")
        for b in bad:
            print(f"   · {b}")
        print("   处置：git reset HEAD <文件>，并确认 .gitignore 规则真的生效"
              "（**注释要独占一行** —— 行尾注释会让整条规则失效）")
        return 1
    print(f"✓ 检查了 {len(staged)} 个暂存文件，无敏感文件")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", choices=["l1", "l2", "all"], default="l1")
    ap.add_argument("--only", default=None, help="L2 只跑指定评测用例，如 G03,G07,G09")
    ap.add_argument("--runs", type=int, default=1, help="L2 每条用例跑几次")
    ap.add_argument("--verbose", action="store_true", help="失败时打出完整输出")
    ap.add_argument("--quiet", action="store_true", help="只在失败时输出（给 git hook 用）")
    ap.add_argument("--list", action="store_true", help="列出所有步骤")
    ap.add_argument("--check-staged", action="store_true",
                    help="只检查暂存区有没有不该进仓的文件（给 pre-commit 用）")
    args = ap.parse_args()

    if args.check_staged:
        return check_staged()

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
