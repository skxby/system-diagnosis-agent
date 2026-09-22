#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""作品 3 · 真机运维入口 —— day42_live.py

**这是"能真干活"的那个入口**（另一个入口 `day36_graph_mcp.py` 面向日志诊断）。

    collect.snapshot()   只读采集（磁盘/inode/内存/负载/温度/OOM/fd/TIME_WAIT/PSI/cgroup）
    collect.evaluate()   确定性规则引擎 → 发现（**不花一分钱、可复现、可进 CI**）
    LLM                  只做**归因与排序**，在规则发现之上，不许编造新的严重项
    人机确认             处置动作要人批准（本工具**只给建议不执行** —— 执行层边界写清楚）
    退出码               0=健康 / 1=有 warning / 2=有 critical   ← 直接当监控插件用

为什么退出码要分级：运维工具的价值一半在"能进管道"。
`day42_live.py --json` 输出结构化结果，退出码直接给 Nagios/Prometheus textfile/Zabbix 这类采集器用。

跑法：
    python day42_live.py                        # 体检本机
    python day42_live.py --no-llm               # 只出规则结论（0 次模型调用）
    python day42_live.py --from snapshots/local_win.json   # 分析已有快照（换机器/存档）
    python day42_live.py --log-dir /var/log     # 顺带找出谁把盘吃满
    python day42_live.py --json out.json        # 结构化输出（接监控）
"""
import os
import sys
import json
import time
import uuid
import argparse

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)

from dotenv import load_dotenv
load_dotenv(os.path.join(HERE, ".env"))

import collect as C
import budget as budget_mod
import trace_util

SEVERITY_EXIT = {"critical": 2, "warning": 1, "info": 0}

ATTRIB_SYS = ("你是资深 SRE。下面给的是**程序采集到的事实**与**规则引擎的确定性发现**。"
              "你的任务只有三件：① 把发现按处置优先级排序；② 指出这些发现之间可能的因果关系；"
              "③ 明确写出证据不足、无法判断的地方。"
              "**硬约束**：不许引入规则发现里没有的严重项；若你有规则之外的猜测，必须标注为「推测」。"
              "引用发现时带上规则名（如 DISK_INODE_HIGH）。输出中文，300 字内。")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="from_file", default=None,
                    help="分析已有快照：.json → 本地快照；其它 → 当作 collect_linux.sh 的文本 dump 解析")
    ap.add_argument("--from-linux-dump", dest="linux_dump", default=None,
                    help="显式指定 Linux dump 文件（板子/VM 上 sh collect_linux.sh 的输出）")
    ap.add_argument("--log-dir", action="append", default=None, help="顺带扫大文件的目录（可多次）")
    ap.add_argument("--save", default=None, help="把本次快照存成 JSON")
    ap.add_argument("--json", dest="json_out", default=None, help="把结果写成结构化 JSON（接监控）")
    ap.add_argument("--no-llm", action="store_true", help="只出规则结论，不调用模型")
    ap.add_argument("--yes", action="store_true", help="自动批准处置建议（CI/巡检用）")
    ap.add_argument("--max-calls", type=int, default=4, help="运行时预算：LLM 调用上限")
    ap.add_argument("--max-seconds", type=float, default=120.0, help="运行时预算：墙钟上限")
    args = ap.parse_args()

    # ── 1. 采集（或读已有快照）───────────────────────────────
    t0 = time.time()
    if args.linux_dump or (args.from_file and not args.from_file.lower().endswith(".json")):
        path = args.linux_dump or args.from_file
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            snap = C.parse_linux_dump(f.read())
        src = f"Linux 采集 dump {path}"
    elif args.from_file:
        snap = C.load(args.from_file)
        src = f"快照文件 {args.from_file}"
    else:
        snap = C.snapshot(log_dirs=args.log_dir)
        src = "本机实时采集"
        if args.save:
            C.save(snap, args.save)
    findings = C.evaluate(snap)
    sev = max((SEVERITY_EXIT.get(f["severity"], 0) for f in findings), default=0)
    text = C.render(snap, findings)

    print("=" * 86)
    print(f"真机体检（{src}）｜主机 {snap.get('host')}｜{snap.get('collected_at')}")
    print("=" * 86)
    print(text)

    # ── 2. 结构化结果（监视器/告警管道要的是这个，不是散文）────
    result = {
        "host": snap.get("host"), "collected_at": snap.get("collected_at"),
        "source": src, "collect_seconds": snap.get("collect_seconds"),
        "findings": findings, "worst_severity": ["ok", "warning", "critical"][sev],
        "exit_code": sev, "report": None, "approved": [], "note": "",
    }

    if args.no_llm:
        result["report"] = "（--no-llm：只出规则结论）"
    elif not findings:
        # 健康时不叫模型 —— 省钱，而且"没有异常"本来就是程序结论（Day 34 的程序网关同一条理）
        result["report"] = "规则未发现异常（**只代表已采集到的维度**）。无需模型归因。"
        print("\n" + result["report"])
    else:
        if not os.environ.get("DEEPSEEK_API_KEY"):
            print("\n错误：.env 里没有 DEEPSEEK_API_KEY（或用 --no-llm）")
            return sev
        import day32_function_calling as d32
        import context as ctx

        budget = budget_mod.Budget(max_llm_calls=args.max_calls, max_tool_calls=4,
                                   max_seconds=args.max_seconds, label="live")
        meter = []
        llm = ctx.make_llm(d32.make_client(), d32.MODEL, usage_sink=meter, label="attrib")
        tracer = trace_util.make_tracer(f"live-{snap.get('host')}", uuid.uuid4().hex[:6])
        try:
            budget.reserve_llm(len(text))          # 预算准入：真机体检也要防跑飞
            with tracer.span("attribute", as_type="chain", host=snap.get("host")) as sp:
                report = llm(ATTRIB_SYS, text)
                sp.set(out_chars=len(report))
            tracer.flush()
        except budget_mod.BudgetExceeded as e:
            report = f"（预算刹车，未做归因：{e.reason}）"
        if meter:
            u = meter[-1]
            print(f"\n（归因调用：prompt {u['prompt_tokens']} token / 输出 {u['completion_tokens']} token，"
                  f"finish={u['finish_reason']}）")
        result["report"] = report
        print("\n" + "=" * 86)
        print("归因与处置优先级（在规则发现之上）")
        print("=" * 86)
        print(report)

    # ── 3. 人机确认：处置动作要人批（本工具只给建议，不执行）──────
    if findings:
        actions = [f"{f['severity'].upper()} {f['rule']} @ {f['subject']}：{f['suggestion']}"
                   for f in findings]
        print("\n" + "-" * 86)
        print("以下处置动作带有副作用，需要人批准（本工具**只列建议，不执行**）：")
        for a in actions:
            print(f"  · {a}")
        if args.yes:
            result["approved"] = actions
            result["note"] = "自动批准（--yes，巡检模式）"
            print("→ 已自动批准（--yes）。真实环境应接 Ansible/SSH 执行层，且执行层自己做二次校验。")
        else:
            print("→ 未批准：请在真实运维流程里走审批（本工具不提供执行能力，这是刻意的边界）。")

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"\n结构化结果：{args.json_out}")

    worst = result["worst_severity"]
    print(f"\n结论：{worst}（发现 {len(findings)} 条，用时 {time.time() - t0:.1f}s，退出码 {sev}）")
    return sev


if __name__ == "__main__":
    sys.exit(main())
