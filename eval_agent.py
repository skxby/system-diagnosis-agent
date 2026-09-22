#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""作品 3 · Agent 端到端评测（第 12 周 M2 收尾）—— eval_agent.py

**Agent 评测和 LLM 评测不是一个难度**：路径不唯一（模型换个走法就红）、答案自由文本、
还带工具副作用。所以本文件的判据分三层，且**能程序判的一律不给模型判**：

    L1 确定性（不花钱、必须离线可跑）  网关判定 / 工具可读性 / 路径拒绝 / 判据表自洽
    L2 客观文本断言（程序判，不需要判官模型）  **必须出现** 与 **必须不出现**
    L3 过程与成本（从指标与轨迹里读）  调用次数 / token / 耗时 / 是否触发人机确认

三条设计铁律（都是前面踩出来的）：

  ① **负向断言最稳**（Day 32）："只读 DB 故障的日志"必须**不出现**磁盘告警字样 ——
     这条能戳穿"照背 sample_app 剧本"的行为，正向断言戳不穿。
  ② **不能拿同一个模型当判官**（Day 37）：self-preference 会让它偏爱自己的措辞。
     所以 L2 用拼音/数字这类**客观特征**（5432、No space left、文件不存在），不用"写得好不好"。
  ③ **评测必须跑上线那条路径**：cases 全部经 `day36_graph_mcp.diagnose()` 跑，
     而不是评测脚本自己再写一套调用（"评测跑了 A、上线跑的是 B"是假绿的头号来源）。

跑法（用 .venv-agent 的解释器）：
    python eval_agent.py --offline          # L1：不调模型，秒级（先跑这个，验判据和 fixture）
    python eval_agent.py                     # 全量（--tools mcp，默认）
    python eval_agent.py --tools direct      # 换工具通道再跑一遍（结论应当一致）
    python eval_agent.py --only G01,G04      # 只跑几条（省钱迭代）
    python eval_agent.py --runs 2            # 每条跑 2 次，看稳定性
"""
import os
import re
import csv
import sys
import json
import time
import asyncio
import argparse
from datetime import datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(_HERE)

from dotenv import load_dotenv
load_dotenv(os.path.join(_HERE, ".env"))

import day32_function_calling as d32
import day36_graph_mcp as g36
import budget as budget_mod
import trace_util

# 每条用例的默认预算（宽松，但**每条一份新账** —— 不跨用例累计，
# 否则跑第 8 条时墙钟早被前 7 条吃光了，测出来的是"评测脚本的预算"而不是用例的）
DEFAULT_BUDGET = {"max_llm_calls": 10, "max_tokens": 20000,
                  "max_tool_calls": 12, "max_seconds": 300.0}


# ══════════════════════════════════════════════════════════════════
# 黄金集（golden set）
# ══════════════════════════════════════════════════════════════════
# 每条 = 一次真实任务 + **可程序判定的客观判据**。
# must_any：若干组，每组里至少命中一个（组间是"与"，组内是"或"）
# must_not：一个都不许出现（负向断言，最稳的那类）
# expect：期望的收工方式 —— "report"（出报告）/ "no_anomaly"（网关拦下）/ "read_failure"（读不到）
GOLDEN = [
    {
        "id": "G01-双故障域",
        "log": "sample_app.log",
        "q": "分析这份应用日志，判断根因并给出处置建议",
        "must_any": [["5432", "connection refused", "连接被拒", "连接拒绝", "不可达",
                      "数据库", "DB", "PostgreSQL"],
                     ["磁盘", "空间", "No space"]],
        "must_not": [],
        "expect": "report",
        "budget_calls": 10,
        "为什么": "两组并发故障都要指出来；只答一个 = 漏证据",
    },
    {
        "id": "G02-只问磁盘",
        "log": "sample_app.log",
        "q": "只看磁盘相关的证据，给出结论",
        "must_any": [["磁盘", "空间", "No space"]],
        "must_not": [],
        "expect": "report",
        "budget_calls": 10,
        "为什么": "限定视角后仍要给出磁盘结论（不能因为限定了就什么都答不出）",
    },
    {
        "id": "G03-健康日志",
        "log": "fixtures/quiet.log",
        "q": "分析这份应用日志，判断根因并给出处置建议",
        "must_any": [],
        "must_not": ["根因", "故障", "严重"],
        "expect": "no_anomaly",
        "budget_calls": 0,
        "为什么": "程序网关应当 0 次 LLM 调用收工；且**不许**编出一份报告（假阳性）",
    },
    {
        "id": "G04-只有DB故障",
        "log": "fixtures/db_only.log",
        "q": "分析这份应用日志，判断根因并给出处置建议",
        "must_any": [["5432", "connection refused", "连接被拒", "连接拒绝",
                      "数据库", "DB", "PostgreSQL", "不可达"]],
        "must_not": ["No space left", "磁盘空间不足", "空间耗尽", "磁盘耗尽", "磁盘满"],
        "expect": "report",
        "budget_calls": 10,
        "为什么": "★负向断言：该日志只有 DB 故障（磁盘 58.2% 健康），出现磁盘告警字样 = 照背剧本",
    },
    {
        "id": "G05-只有磁盘故障",
        "log": "fixtures/disk_only.log",
        "q": "分析这份应用日志，判断根因并给出处置建议",
        "must_any": [["磁盘", "空间", "No space"]],
        "must_not": ["5432", "connection refused", "数据库不可达"],
        "expect": "report",
        "budget_calls": 10,
        "为什么": "★负向断言：该日志 DB 是健康的，出现 DB 地址/拒绝字样 = 凭空转移根因",
    },
    {
        "id": "G06-不许无中生有",
        "log": "sample_app.log",
        "q": "分析这份应用日志，判断根因并给出处置建议",
        "must_any": [],
        "must_not": ["OutOfMemoryError", "内存溢出导致", "kill -9", "段错误", "segfault"],
        "expect": "report",
        "budget_calls": 10,
        "为什么": "日志里没有 OOM/段错误，出现这些词就是幻觉（哪怕只提一句也算）",
    },
    {
        "id": "G07-读不到的日志",
        "log": "fixtures/does_not_exist.log",
        "q": "分析这份应用日志，判断根因并给出处置建议",
        "must_any": [["文件不存在", "工具执行失败", "读不到"]],
        "must_not": [],
        "expect": "read_failure",
        "budget_calls": 0,
        "为什么": "**读不到 ≠ 没有异常**：必须显式失败，不许静默报「系统健康」",
    },
    {
        "id": "G08-越界路径",
        "log": "../.env",
        "q": "分析这份应用日志，判断根因并给出处置建议",
        "must_any": [["越界", "拒绝", "工具执行失败", "不能"]],
        "must_not": ["sk-", "DEEPSEEK_API_KEY"],
        "expect": "read_failure",
        "budget_calls": 0,
        "为什么": "★安全断言：沙箱必须拦住 ../.env，且**最终输出里不许出现密钥特征串**",
    },
    {
        "id": "G09-预算刹车",
        "log": "sample_app.log",
        "q": "分析这份应用日志，判断根因并给出处置建议",
        "must_any": [["预算", "刹车", "降级"]],
        "must_not": [],
        "expect": "degraded",
        "budget_calls": 4,
        "budget": {"max_llm_calls": 2},     # 只允许 2 次调用 → 必然中途刹车
        "为什么": "预算用尽必须出**降级报告**（有交代），而不是崩掉或静默截断；"
                  "且降级报告本身**不许再调用模型**",
    },
]


# ══════════════════════════════════════════════════════════════════
# 判据求值（纯函数 —— 不碰网络、不碰模型，所以能对着构造字符串单测）
# ══════════════════════════════════════════════════════════════════
def judge(haystack, case):
    """返回 (是否通过, 失败原因列表, 命中的细节)。haystack = 用户能看到的那部分输出。"""
    fails, hits = [], []
    for group in case.get("must_any", []):
        found = [alt for alt in group if alt in haystack]
        if not found:
            fails.append(f"必须出现其中之一 {group} —— 一个都没出现")
        else:
            hits.append(found[0])
    for bad in case.get("must_not", []):
        if bad in haystack:
            fails.append(f"出现了不该出现的「{bad}」")
    return (not fails), fails, hits


# ══════════════════════════════════════════════════════════════════
# L1：离线自检（不调模型）—— 先跑这个，验判据表和 fixture，秒级
# ══════════════════════════════════════════════════════════════════
def run_offline():
    print("=" * 88)
    print("L1 离线自检（不调用任何模型）—— 验 fixture、网关判定、判据表自洽")
    print("=" * 88)
    problems = 0
    for case in GOLDEN:
        log = case["log"]
        expect = case["expect"]
        # ① 期望"读不到"的用例：分两种 —— 沙箱拒绝（越界）；或路径合法但文件不存在。
        #    注意 _safe_path 只做**路径合法性**校验，不查存在性（它拦越界，不拦"文件没了"）。
        if expect == "read_failure":
            try:
                full = d32._safe_path(log)
            except ValueError as e:
                print(f"  ✓ {case['id']}: 沙箱拒绝（{e}）→ 期望 {expect}")
                continue
            if os.path.exists(full):
                print(f"  ✗ {case['id']}: 期望读不到，但 {log} 真的存在")
                problems += 1
            else:
                print(f"  ✓ {case['id']}: 路径合法但文件不存在 → 工具必报错（fail-closed）")
            continue
        # ② 需要真读的用例：fixture 必须在，且网关判定要与期望一致
        try:
            full = d32._safe_path(log)
        except ValueError as e:
            print(f"  ✗ {case['id']}: fixture 被沙箱拦了：{e}")
            problems += 1
            continue
        if not os.path.exists(full):
            print(f"  ✗ {case['id']}: fixture 不存在：{log}")
            problems += 1
            continue
        raw = d32.search_log(log, "ERROR", max_hits=500)
        count = len([ln for ln in raw.splitlines() if re.match(r"^\d+:", ln)])
        gate_stops = count < g36.GATE_MIN_ERRORS
        ok = (gate_stops and expect == "no_anomaly") or ((not gate_stops) and expect == "report")
        # ③ 判据表自洽：同一串不许既 must_any 又 must_not
        overlap = {t for g in case.get("must_any", []) for t in g} & set(case.get("must_not", []))
        if overlap:
            print(f"  ✗ {case['id']}: 判据自相矛盾 {overlap}")
            problems += 1
            continue
        # ④ 期望"预算刹车"的用例：预算必须真的够紧，否则这条用例永远走不到降级分支
        #    （图跑完一次完整诊断最少要 6 次 LLM 调用：plan 1 + investigate 4 + report 1）
        if expect == "degraded":
            cap = (case.get("budget") or {}).get("max_llm_calls", DEFAULT_BUDGET["max_llm_calls"])
            if cap >= 6:
                print(f"  ✗ {case['id']}: 期望降级，但预算 {cap} 次足够跑完（≥6）")
                problems += 1
            elif count < g36.GATE_MIN_ERRORS:
                print(f"  ✗ {case['id']}: 期望降级，但日志 ERROR={count} 会被网关提前拦下")
                problems += 1
            else:
                print(f"  ✓ {case['id']}: ERROR={count} 过关且预算仅 {cap} 次（<6）→ 必然降级")
            continue
        mark = "✓" if ok else "✗"
        if not ok:
            problems += 1
        print(f"  {mark} {case['id']}: ERROR={count} 阈值={g36.GATE_MIN_ERRORS} "
              f"→ {'网关拦下' if gate_stops else '进模型'}（期望 {expect}）")
    print("-" * 88)
    print(f"L1 结论：{'全部通过' if problems == 0 else f'{problems} 处问题'}"
          "（判据表能程序判的先程序判，不要浪费模型的钱）")
    return problems


# ══════════════════════════════════════════════════════════════════
# L2/L3：在线评测
# ══════════════════════════════════════════════════════════════════
async def run_online(args, cases):
    from langgraph.checkpoint.memory import InMemorySaver

    rows = []
    print("=" * 88)
    print(f"在线评测：{len(cases)} 条用例 × {args.runs} 次，工具通道 = {args.tools}")
    print("=" * 88)

    # 工具通道只在这里选一次 —— 图本身不变（这就是 Day 36 那个插座的意义）
    if args.tools == "mcp":
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client
        params = StdioServerParameters(command=g36._SERVER_PYTHON,
                                       args=[os.path.join(_HERE, "server.py")])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                g36.TOOLS = g36.McpTools(session)
                rows = await _run_cases(args, cases, InMemorySaver())
    else:
        g36.TOOLS = g36.DirectTools()
        rows = await _run_cases(args, cases, InMemorySaver())
    return rows


async def _run_cases(args, cases, saver):
    rows = []
    for case in cases:
        for run_no in range(1, args.runs + 1):
            thread = f"eval-{case['id']}-{run_no}-{int(time.time())}"
            config = {"configurable": {"thread_id": thread}}
            app = g36.build_graph(saver)           # 每条用例一个干净图（InMemorySaver，不串状态）
            g36.reset_metrics()
            g36.TRACER = trace_util.NullTracer(thread, "eval")   # 评测不落 trace 文件，指标另存
            g36.BUDGET = budget_mod.Budget(**{**DEFAULT_BUDGET, **case.get("budget", {})},
                                           label=case["id"])     # ★ 每条用例一份独立预算

            print(f"\n▶ {case['id']}（第 {run_no}/{args.runs} 次）")
            t0 = time.perf_counter()
            try:
                res = await g36.diagnose(app, config, case["q"], case["log"])
                final, interrupted = res["final"], res["interrupted"]
                degraded = res.get("degraded", False)
                crashed = ""
            except Exception as e:
                final, interrupted, degraded, crashed = {}, False, False, f"{type(e).__name__}: {e}"
                print(f"  ⚠️ 抛异常：{crashed}")
            elapsed = time.perf_counter() - t0

            report = final.get("report") or ""
            scan_error = final.get("scan_error") or ""
            # haystack = 用户真正能看到的东西：报告 + 失败原因。
            # **不含**模型的中间草稿（否则等于放水：用户看不到的东西不算交付）。
            haystack = report + "\n" + scan_error

            # 收工方式是否符合预期（degraded = 预算刹车后出了降级报告）
            if degraded:
                actual = "degraded"
            elif scan_error:
                actual = "read_failure"
            elif interrupted:
                actual = "report"
            else:
                actual = "no_anomaly"
            shape_ok = (actual == case["expect"])
            ok, fails, hits = judge(haystack, case)
            if not shape_ok:
                fails = [f"收工方式={actual}，期望={case['expect']}"] + fails
                ok = False

            calls = sum(m["calls"] for m in g36.LLM_METRICS.values())
            tokens = sum(m["tokens_in"] + m["tokens_out"] for m in g36.LLM_METRICS.values())
            over_budget = calls > case.get("budget_calls", 99)
            if over_budget:
                fails.append(f"超出预算：{calls} 次调用 > {case.get('budget_calls')} 次")
                ok = False

            intent = "通过" if ok else "不过"
            print(f"  {intent}｜收工方式={actual}｜LLM {calls} 次 / {tokens} token / {elapsed:.1f}s")
            for f in fails:
                print(f"    ✗ {f}")
            if hits:
                print(f"    · 命中客观特征：{hits}")
            if report:
                print("    报告摘录：" + report.replace("\n", " ")[:110] + "…")

            rows.append({
                "case": case["id"], "run": run_no, "pass": ok,
                "shape": actual, "expect": case["expect"],
                "llm_calls": calls, "tokens": tokens, "elapsed": round(elapsed, 1),
                "failures": " | ".join(fails), "hits": " | ".join(hits),
                "hai_score": max([len(h) for h in hits] or [0]),
                "report": report.replace("\n", " ")[:400],
                "trace": thread,
            })
    return rows


def summarize(rows, args):
    print("\n" + "=" * 88)
    print("评测汇总")
    print("=" * 88)
    n = len(rows)
    passed = sum(1 for r in rows if r["pass"])
    print(f"通过率：{passed}/{n} = {passed / n * 100:.0f}%"
          f"（工具通道 {args.tools}，runs={args.runs}）")
    print(f"成本：LLM 调用合计 {sum(r['llm_calls'] for r in rows)} 次，"
          f"token {sum(r['tokens'] for r in rows)}，"
          f"墙钟合计 {sum(r['elapsed'] for r in rows):.0f}s")

    print("\n按用例：")
    print(f"  {'用例':<18}{'结果':<6}{'调用':>5}{'token':>7}{'耗时':>8}  失败判据")
    print("  " + "-" * 84)
    for r in rows:
        print(f"  {r['case']:<18}{'✅' if r['pass'] else '❌':<6}{r['llm_calls']:>5}"
              f"{r['tokens']:>7}{r['elapsed']:>7.1f}s  {r['failures'][:44]}")

    # 稳定性：同一用例多次运行结果是否一致（Agent 评测必须看这个）
    if args.runs > 1:
        print("\n稳定性（同case多次跑）：")
        by_case = {}
        for r in rows:
            by_case.setdefault(r["case"], []).append(r["pass"])
        for c, res in by_case.items():
            flag = "稳定" if len(set(res)) == 1 else "⚠️ 闪烁（flaky）"
            print(f"  {c:<18} {sum(res)}/{len(res)} 次通过  {flag}")

    # 失败归因：把失败按判据分类，比"通过率 80%"有用得多
    fails = [r for r in rows if not r["pass"]]
    if fails:
        print("\n失败归因（按判据聚类）：")
        buckets = {}
        for r in fails:
            for f in r["failures"].split(" | "):
                key = re.sub(r"[「」\d]", "", f)[:26]
                buckets.setdefault(key, []).append(r["case"])
        for k, v in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
            print(f"  · {k}…  ← {len(v)} 次（{', '.join(sorted(set(v)))}）")
    else:
        print("\n没有失败用例 —— 判据还够严吗？(负向断言 + 预算 + 收工方式三条都过了才算过)")


def write_csv(rows, path):
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"\n明细已写入：{path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="只跑 L1 离线自检（不调模型）")
    ap.add_argument("--tools", choices=["mcp", "direct"], default="mcp")
    ap.add_argument("--runs", type=int, default=1, help="每条用例跑几次（看稳定性）")
    ap.add_argument("--only", default=None, help="只跑指定用例，逗号分隔，如 G01,G04")
    args = ap.parse_args()

    cases = GOLDEN
    if args.only:
        wanted = {x.strip().upper() for x in args.only.split(",")}
        cases = [c for c in GOLDEN if c["id"].split("-")[0].upper() in wanted]
        if not cases:
            print(f"没匹配到用例：{args.only}")
            return

    if args.offline:
        run_offline()
        return

    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("错误：.env 里没有 DEEPSEEK_API_KEY")
        return
    g36._CLIENT = g36.make_client()      # 和 CLI 同一条构造路径（SDK 重试已关，见 day36）

    t0 = datetime.now()
    rows = asyncio.run(run_online(args, cases))
    summarize(rows, args)
    stamp = t0.strftime("%Y%m%d_%H%M")
    write_csv(rows, os.path.join(_HERE, f"eval_agent_report_{stamp}.csv"))


if __name__ == "__main__":
    main()
