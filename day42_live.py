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
import trend as trend_mod
import cost as cost_mod
import converge as conv_mod
import feedback as fb_mod
import audit as audit_mod
import budget as budget_mod
import trace_util

SEVERITY_EXIT = {"critical": 2, "warning": 1, "info": 0}

ATTRIB_SYS = ("你是资深 SRE。下面给的是**程序采集到的事实**与**规则引擎的确定性发现**。"
              "你的任务只有三件：① 把发现按处置优先级排序；② 指出这些发现之间可能的因果关系；"
              "③ 明确写出证据不足、无法判断的地方。"
              "**硬约束**：不许引入规则发现里没有的严重项；若你有规则之外的猜测，必须标注为「推测」。"
              "引用发现时带上规则名（如 DISK_INODE_HIGH）。输出中文，300 字内。")

# 收敛模式下喂模型的是「事件表」而不是平铺发现：**因果已经由程序里的因果图判定完了**。
# 为什么不给模型自己判因果：不可复现、不可审计，而且它会**自信地编出因果**。
ATTRIB_SYS_EVENTS = (
    "你是资深 SRE。下面给的是**程序收敛后的事件表**：一个故障一个事件；"
    "「根因」是程序用编码的因果图判定、机制上必然的；「候选根因」是未核验的；"
    "「并列」是同一资源上互不解释、要分别处理的问题。"
    "你的任务只有三件：① 按处置优先级排序这些事件；② 用运维语言说清「先做什么、为什么」；"
    "③ 指出证据不足、需要人工确认的地方。"
    "**硬约束**：不许改动程序给的因果（不许把并列写成连带，不许把候选根因写成确定根因）；"
    "不许引入事件表里没有的严重项；推测必须标注「推测」。输出中文，300 字内。")


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
    # ★ 默认值不是拍脑袋定的：下面是同一提示词、同一机器、真实 API 的实测（各 3~4 次）
    #     cap=2000 → 平均 6409 token/次，重试 3/4，平均 19.4s
    #     cap=3000 → 平均 5443 token/次，重试 2/4，平均 15.8s   ← 本次实测期望成本最低
    #     cap=4000 → 平均 6171 token/次，重试 1/3，失败那次白烧 4000（36s）
    #   结论：**上限不是越大越好** —— 失败一次的浪费就是该上限本身；推理 token 是弹性的，
    #   给得越大想得越久（实测同一提示词：cap 2000→推理 1118 字，cap 4000→推理 4131 字，
    #   而正文反而从 583 字降到 445 字）。所以上限要按实测调，而账本会记下白烧的量。
    ap.add_argument("--max-output-tokens", type=int, default=3000,
                    help="归因输出上限（实测默认 3000 期望成本最低；太小会空正文并触发重试，"
                         "重试要重发提示词+重想一遍，账本会记下白烧的 token）")
    ap.add_argument("--max-seconds", type=float, default=120.0, help="运行时预算：墙钟上限")
    ap.add_argument("--history", default=None, help="历史文件路径（默认 snapshots/history.jsonl）")
    ap.add_argument("--no-history", action="store_true", help="本次不写历史、也不做趋势分析")
    ap.add_argument("--trend", action="store_true", help="打印趋势详情（含'为什么没给预测'）")
    ap.add_argument("--llm-input", choices=["events", "full"], default="events",
                    help="喂模型的输入：events=收敛后的事件表（默认，更短且因果已判定）；full=原始快照+平铺发现")
    ap.add_argument("--explain-graph", action="store_true",
                    help="打印收敛用的因果图（给人审计/推翻），然后退出")
    # ── 成本控制（监控是反复跑的：单次便宜不够，要"重复也不花钱"）──
    ap.add_argument("--no-cache", action="store_true", help="不用归因缓存（强制重新归因）")
    ap.add_argument("--cache", default=None, help="归因缓存文件路径")
    ap.add_argument("--cache-ttl", type=int, default=1800, help="缓存有效期（秒，默认 30 分钟）")
    ap.add_argument("--model-cheap", default=None, help="便宜档模型名（仅 warning 时用）")
    ap.add_argument("--model-strong", default=None, help="强档模型名（critical 或发现≥5 时用）")
    ap.add_argument("--cost", default=None, help="成本账本文件路径")
    ap.add_argument("--hourly-token-limit", type=int, default=None,
                    help="每小时 token 限额：超了就不再调模型，只出规则结论")
    ap.add_argument("--price-in", type=float, default=0.0, help="输入单价（每百万 token，自行填）")
    ap.add_argument("--price-out", type=float, default=0.0, help="输出单价（每百万 token，自行填）")
    ap.add_argument("--quiet-if-ok", action="store_true",
                    help="无发现时只打一行（给 cron 用：配合缓存让巡检几乎零成本）")
    # ── 误报反馈与抑制（企业调研里的"反馈闭环"：误报没人标记 → 工具不会越用越准）──
    ap.add_argument("--feedback", default=None, help="反馈记录文件（默认 snapshots/feedback.jsonl）")
    ap.add_argument("--suppression-state", default=None, help="抑制命中计数文件")
    ap.add_argument("--no-suppress", action="store_true", help="本次忽略抑制规则（排查时用）")
    ap.add_argument("--min-marks", type=int, default=None,
                    help="抑制生效所需最少标记数（默认 exact=1、prefix/rule=2）")
    ap.add_argument("--mark-fp", action="append", default=None, metavar="RULE@SUBJECT",
                    help="把本次的这条发现标成误报（可多次）；先看报告再标")
    ap.add_argument("--fp-reason", default=None,
                    help="误报理由（**标记时必填**，否则半年后没人敢撤销）")
    ap.add_argument("--fp-match", default="exact", choices=["exact", "prefix", "rule"],
                    help="抑制范围：exact（默认，只压同一对象）/ prefix / rule")
    ap.add_argument("--fp-wide", action="store_true", help="允许 prefix/rule 宽匹配（要更多证据）")
    ap.add_argument("--fp-hours", type=int, default=168,
                    help="抑制有效期小时数（默认 7 天；**不允许永久抑制**）")
    # ── 安全审计（谁在何时读了什么；MCP server 那边写的是同一份日志）──
    ap.add_argument("--audit", default=None,
                    help="审计日志路径（默认 snapshots/audit.jsonl）")
    ap.add_argument("--no-audit", action="store_true",
                    help="本次不写审计（**默认不许关**；只在离线复现时用）")
    args = ap.parse_args()

    if args.mark_fp and not (args.fp_reason or "").strip():
        print("拒绝标记：--mark-fp 必须同时给 --fp-reason（**理由必填**，否则半年后没人敢撤销）")
        return 2

    # 因果图审计：让人能看、能质疑、能推翻我们的归因（比"AI 说的"更值得信任）
    if args.explain_graph:
        print(conv_mod.explain_graph())
        probs = conv_mod.check_graph()
        print("\n因果图自检：" + ("通过（无环、规则齐全、无反例矛盾）" if not probs
                              else "**未通过** " + "；".join(probs)))
        return 0

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

    # ── 趋势：把"当前值"升级成"可行动的时间预算" ──────────────────
    # 每次体检都写进历史（巡检的自然产物），再基于同主机历史做预测。
    history_path = args.history or trend_mod.DEFAULT_HISTORY
    trend_findings, recs = [], []
    if not args.no_history:
        trend_mod.append_sample(snap, ["ok", "warning", "critical"][sev], path=history_path)
        recs = trend_mod.load(history_path, host=snap.get("host"), last_n=200)
        trend_findings = trend_mod.evaluate_trends(recs, snap)
        if trend_findings:
            findings = sorted(findings + trend_findings,
                              key=lambda f: {"critical": 0, "warning": 1, "info": 2}
                              .get(f["severity"], 9))
            # ★ 预测也影响退出码：监控因此能**提前**报警，而不是等阈值被击穿
            sev = max(sev, max(SEVERITY_EXIT.get(f["severity"], 0) for f in trend_findings))

    # ── 1.4 误报反馈：人工标记的抑制规则（**不删除，只标注**）────────────
    # 顺序很重要：**先抑制、再收敛**。这样事件里不会混进已被人工确认的噪声，
    # 退出码也只由「保留」的发现决定；而被抑制的发现**照旧打印出来**（抑制 ≠ 删除）。
    fb_store = fb_mod.FeedbackStore(args.feedback or fb_mod.DEFAULT_FEEDBACK)
    sup_state = fb_mod.SuppressionState(args.suppression_state or fb_mod.DEFAULT_STATE)
    marked = []
    for sel in (args.mark_fp or []):
        marked.extend(_mark_one(fb_store, sel, args, snap, findings))
    sup_rules, sup_rejected = fb_mod.build_suppressions(fb_store.load(), min_marks=args.min_marks)
    kept, suppressed = ((list(findings), []) if args.no_suppress
                        else fb_mod.apply_suppressions(findings, snap.get("host"), sup_rules))
    # 运行时自检：抑制**绝对不能**让发现消失（这是本模块的头号纪律）
    if len(kept) + len(suppressed) != len(findings):
        raise RuntimeError(f"抑制模块破坏了完整性：{len(kept)} + {len(suppressed)} "
                           f"!= {len(findings)}（这是 bug，不是数据问题）")
    sev = max((SEVERITY_EXIT.get(f["severity"], 0) for f in kept), default=0)
    if suppressed:
        sup_state.record(suppressed, host=snap.get("host"))
    sup_stats, sup_notes = fb_mod.audit_suppressions(sup_rules, sup_state)
    text = C.render(snap, kept)

    # ── 1.45 安全审计：把"这次巡检读了一遍机器"记进审计日志 ──────────
    # 巡检本身也是一次**读取访问**，读的是 /proc、/sys 和日志目录 ——
    # 出了事要能回答"谁在什么时候读了什么"，所以它和 MCP 工具调用共用一份日志。
    audit_log = audit_mod.AuditLog(args.audit or audit_mod.DEFAULT_AUDIT,
                                   enabled=not args.no_audit)
    audit_rec = audit_log.record(
        "live_diagnose", args={"source": src, "log_dirs": args.log_dir or []},
        scope="read", target=(args.linux_dump or args.from_file or "本机实时采集"),
        result_summary=f"发现 {len(kept)} 条（抑制 {len(suppressed)} 条）",
        duration_ms=(time.time() - t0) * 1000,
        extra={"host": snap.get("host"), "worst": ["ok", "warning", "critical"][sev]})

    # ── 1.5 告警收敛：把"一次故障 N 条告警"收敛成"N 个事件 + 根因/连带" ──────
    # 企业调研里排第一的痛点不是"没有告警"，是"一次故障几十条告警，真信号被埋掉"。
    # 这一步**全是确定性计算、0 token**：因果由代码判定（可复现、可审计、可推翻），
    # 模型只在收敛后的结果上做措辞与优先级 —— 不让它自己推理因果。
    events, conv_report = conv_mod.converge(kept, snap)

    # 喂模型的输入：默认用**事件表**（更短，且因果已判定好）
    prompt_text = (conv_mod.render_for_llm(events, conv_report, snap=snap)
                   if args.llm_input == "events" else text)
    if suppressed:
        # 告诉模型"这些已被人工确认是误报"，免得它又去为它们出建议
        # （注意：是**告诉**它，不是让它判断 —— 抑制只能来自人的显式标记）
        prompt_text += (f"\n另有 {len(suppressed)} 条已被人工确认是误报并抑制"
                        f"（不要为它们出建议）："
                        + "、".join(f"{s['rule']}@{s['subject']}" for s in suppressed))
    attrib_sys = ATTRIB_SYS_EVENTS if args.llm_input == "events" else ATTRIB_SYS

    # ── 2. 成本控制：缓存 → 模型分级 → 小时限额 ─────────────────
    # 监控是反复跑的：这一步决定了"状态没变时会不会白花钱"。
    fp = cost_mod.findings_fingerprint(
        # 按**保留**的发现算指纹：抑制状态变了，归因也该重算
        kept, extra={"worst": ["ok", "warning", "critical"][sev],
                         "input": args.llm_input})   # ★ 输入模式不同 → 归因也不同，指纹必须区分
    cache = cost_mod.AttributionCache(args.cache or cost_mod.DEFAULT_CACHE, args.cache_ttl)
    ledger = cost_mod.CostLedger(args.cost or cost_mod.DEFAULT_LEDGER, args.hourly_token_limit)
    cheap = args.model_cheap or d32_model()
    strong = args.model_strong or d32_model()

    report, model_used, cached_hit, tokens_in, tokens_out, blocked = None, "", False, 0, 0, ""
    attempts, wasted = 0, 0
    cached_entry = None if args.no_cache else cache.get(fp)

    if args.no_llm:
        report = "（--no-llm：只出规则结论）"
    elif cached_entry:
        # ★ 状态没变 → 复用上次归因，0 次调用
        report, cached_hit = cached_entry["text"], True
    elif not findings:
        # 健康时不叫模型 —— 省钱，而且"没有异常"本来就是程序结论（Day 34 的程序网关同一条理）
        report = "规则未发现异常（**只代表已采集到的维度**）。无需模型归因。"
    else:
        model_used = cost_mod.choose_model(["ok", "warning", "critical"][sev], len(findings),
                                           cheap, strong)
        if not os.environ.get("DEEPSEEK_API_KEY"):
            report = "（.env 里没有 DEEPSEEK_API_KEY，跳过归因；规则结论不受影响）"
        else:
            import day32_function_calling as d32
            import context as ctx

            est = int(len(prompt_text) / 2.4) + 600   # 与 context.py 的保守估算一致
            allowed, why = ledger.allow(est)
            if not allowed:
                blocked = why                     # ★ 限额刹车：只出规则结论，不偷偷花钱
                report = f"（小时限额刹车，未做归因：{why}）"
            else:
                budget = budget_mod.Budget(max_llm_calls=args.max_calls, max_tool_calls=4,
                                           max_seconds=args.max_seconds, label="live")
                meter = []
                llm = ctx.make_llm(d32.make_client(), model_used, usage_sink=meter,
                                   label="attrib", max_tokens=args.max_output_tokens)
                tracer = trace_util.make_tracer(f"live-{snap.get('host')}", uuid.uuid4().hex[:6])
                try:
                    budget.reserve_llm(len(prompt_text))   # 运行时预算：防单次跑飞
                    with tracer.span("attribute", as_type="chain", host=snap.get("host"),
                                     model=model_used) as sp:
                        report = llm(attrib_sys, prompt_text)
                        sp.set(out_chars=len(report), input_mode=args.llm_input,
                               events=conv_report["events"])
                    tracer.flush()
                except budget_mod.BudgetExceeded as e:
                    report = f"（预算刹车，未做归因：{e.reason}）"
                if meter:
                    # ★ 汇总**全部尝试**，不是只取最后一次：
                    # 推理型模型上限偏小时会把预算烧在推理上、正文为空 → 自动放大重试，
                    # 那次是**真花了钱**的。只记 meter[-1] 就是成本工具自己漏记成本。
                    u = cost_mod.summarize_usage(meter)
                    attempts, wasted = u["attempts"], u["wasted_completion_tokens"]
                    tokens_in, tokens_out = u["prompt_tokens"], u["completion_tokens"]
                    model_used = f"{model_used}｜prompt {tokens_in} / 输出 {tokens_out} token"
                    if attempts > 1:
                        model_used += (f"（{attempts} 次尝试，含空正文重试，"
                                       f"白烧 {wasted} token）")
                    if not args.no_cache:
                        cache.put(fp, report, model=model_used,
                                  tokens_in=tokens_in, tokens_out=tokens_out)

    ledger.record(snap.get("host"), calls=0 if (cached_hit or blocked or args.no_llm) else 1,
                  tokens_in=tokens_in, tokens_out=tokens_out, cached=cached_hit,
                  model=model_used, seconds=time.time() - t0,
                  attempts=attempts, wasted=wasted)

    # ── 3. 结构化结果（监视器/告警管道要的是这个，不是散文）────
    result = {
        "host": snap.get("host"), "collected_at": snap.get("collected_at"),
        "source": src, "collect_seconds": snap.get("collect_seconds"),
        "findings": kept, "worst_severity": ["ok", "warning", "critical"][sev],
        "exit_code": sev, "report": report, "approved": [], "note": "",
        # 抑制信息：被抑制的发现**照旧在 JSON 里**，带来源/理由/到期（可审计）
        "suppressed": suppressed,
        "suppressions": {"active": sup_rules, "rejected": sup_rejected,
                         "marked_this_run": [m.get("id") for m in marked],
                         "stats": sup_stats, "health_notes": sup_notes},
        # 收敛结果：一个故障一个事件 + 根因/连带（接告警管道时**优先用这个**，不是平铺发现）
        "convergence": {"report": conv_report, "events": events},
        "cost": {"cached": cached_hit, "model": model_used, "blocked": blocked,
                 "tokens_in": tokens_in, "tokens_out": tokens_out,
                 "input_mode": args.llm_input, "input_chars": len(prompt_text),
                 "attempts": attempts, "wasted_tokens": wasted,
                 "ledger": ledger.summary(args.price_in, args.price_out)},
        "audit": {"seq": audit_rec.get("seq"),
                  "path": None if args.no_audit else audit_log.path,
                  "principal": audit_mod.IDENTITY,
                  "scopes": list(audit_mod.ScopePolicy.from_env().granted)},
        "trend": {"history_samples": len(recs),
                  "signals": [f for f in trend_findings],
                  "history_path": None if args.no_history else history_path},
    }

    # ── 4. 输出（安静模式：无发现时一行收工，给 cron 用）──────────
    if args.quiet_if_ok and sev == 0:
        # ⚠ 只打"发现 0 条"会让人以为真的什么都没检出 —— 被抑制的也算"看到过什么"。
        #   "看起来健康"和"确实健康"是两回事，输出里不许混为一谈。
        print(f"{snap.get('host')} OK｜发现 {len(kept)} 条"
              f"{f'（另 {len(suppressed)} 条已抑制）' if suppressed else ''}｜"
              f"{'缓存命中' if cached_hit else '未调模型'}｜token {tokens_in + tokens_out}")
    else:
        print("=" * 86)
        print(f"真机体检（{src}）｜主机 {snap.get('host')}｜{snap.get('collected_at')}")
        print("=" * 86)
        # 收敛放在最前面：值班的人先看"几个故障"，而不是先看"N 条告警"
        if events:
            print(conv_mod.render(events, conv_report))
            print("\n" + "-" * 86)
        if suppressed:
            print(fb_mod.render_suppressed(suppressed, sup_rules, total=len(findings)))
            print("\n" + "-" * 86)
        if sup_rules:
            print(fb_mod.render_rules(sup_rules, sup_rejected, sup_state))
            # ★ 危险信号必须在**运行时**就摆出来（只在 CLI 自检里报等于没人看）：
            #   比如"这条规则的天花板是 critical"——它能压住最严重的那一档，必须是有意为之
            sup_problems = fb_mod.check_suppressions(sup_rules)
            if sup_problems:
                print("  ⚠ 抑制规则自检有问题（**先确认这不是把监控关掉**）：")
                for p in sup_problems:
                    print(f"    · {p}")
            print("\n" + "-" * 86)
        print(text)
        if not args.no_history and (args.trend or trend_findings):
            print(f"\n【趋势】{trend_mod.explain(recs)}")
        print("\n" + "=" * 86)
        print("归因与处置优先级（在规则发现之上）")
        print("=" * 86)
        if cached_hit:
            print(f"⚡ 命中归因缓存（{cached_entry.get('age_seconds')} 秒前，"
                  f"指纹 {fp} 未变）→ **0 次模型调用**")
        print(report)
        print("\n" + cost_mod.render_cost(result["cost"]["ledger"]))
        if not args.no_audit:
            print(f"审计：seq #{audit_rec.get('seq')} 已记入 {audit_log.path}"
                  f"（主体 {audit_mod.IDENTITY}）")

        # ── 5. 人机确认：处置动作要人批（本工具只给建议，不执行）──────
        # ★ 动作**按事件去重**：一次故障一个动作，而不是 N 条告警 N 个动作 ——
        #   收敛的价值最后就落在这一张清单上（运维批的是故障，不是告警）。
        if kept:
            actions = []
            for e in events:
                head = f"{e['severity'].upper()} {e['event_id']} {e['title']}"
                if e["root"]:
                    actions.append(f"{head}｜根因 {e['root']['rule']} → {e['next_action']}")
                else:
                    actions.append(f"{head} → {e['next_action']}")
            print("\n" + "-" * 86)
            print(f"以下处置动作带有副作用，需要人批准（本工具**只列建议，不执行**）"
                  f"｜{len(findings)} 条发现（其中 {len(suppressed)} 条已抑制）"
                  f"收敛为 {len(actions)} 个待批动作：")
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
    print(f"\n结论：{worst}（发现 {len(findings)} 条"
          f"{f'，其中 {len(suppressed)} 条已抑制' if suppressed else ''}"
          f" → 事件 {len(events)} 个，"
          f"用时 {time.time() - t0:.1f}s，退出码 {sev}"
          f"{'｜缓存命中' if cached_hit else ''}{'｜限额刹车' if blocked else ''}）")
    return sev


def _mark_one(store, sel, args, snap, findings):
    """把一次 `--mark-fp RULE@SUBJECT` 落成反馈记录。

    只允许标记**本次确实出现过的发现**：手打一个不存在的规则名会让后续规则永远命中不了
    —— 那会表现为"标记了但什么都没压住"，最容易被误当成"已经生效了"。
    """
    rule, _, subject = sel.partition("@")
    hit = [f for f in findings
           if f.get("rule") == rule and (not subject or str(f.get("subject")) == subject)]
    if not hit:
        print(f"⚠ --mark-fp {sel} 在本次发现里找不到对应项 → **不记录**"
              f"（避免造出一条永远不生效的规则）")
        return []
    recs = []
    for f in hit:
        try:
            recs.append(store.add(snap.get("host") or "", rule, f.get("subject"),
                                  f.get("severity", "warning"), reason=args.fp_reason,
                                  match=args.fp_match, hours=args.fp_hours,
                                  wide=args.fp_wide))
            print(f"✓ 已记录误报 {recs[-1]['id']}：{rule} @ {f.get('subject')}"
                  f"（{args.fp_match}，{args.fp_hours} 小时后过期）")
        except ValueError as e:
            print(f"⚠ 拒绝记录 {sel}：{e}")
    return recs


def d32_model():
    """当前已核验的模型名（不写死"更强的型号"：供应商型号随时变，写死会变成文档里的谎言）"""
    import day32_function_calling as d32
    return d32.MODEL


if __name__ == "__main__":
    sys.exit(main())
