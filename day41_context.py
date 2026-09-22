#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""作品 3 · 上下文工程实验台 —— day41_context.py

**问题**：日志是 3672 行 / 286 KB ≈ **121k token**（实测校准）。整份塞进 prompt 是不可能的。
**实验**：同一个诊断任务，五种上下文策略，量化对比"花多少 token"和"答案对不对"。

    S0 naive    把命中行**全塞进去** —— 朴素做法，上下文有多大是多大
    S1 budget   预算感知装填（2000 token 预算 + 明确标记省略）
    S2 compact  先分块压缩成事实摘要（多花调用，换更小的上下文）
    S3 subagent 每个子任务交给**独立上下文**的子 agent，只回传 100 字结论
    S4 notes    S3 + 结论落盘；**第二轮追问**只注入相关笔记（不重读原文）

质量判据一律程序判（客观特征，不请模型当判官 —— Day 37 的规矩）：
    ✓ 必须提到 DB 故障：5432 / connection refused / 连接被拒
    ✓ 必须提到磁盘：磁盘 / 空间 / No space
    ✓ 不许编造：报告里出现 OOM / 内存溢出导致 / 段错误 即判挂
    · 加分：提到丢了 2048 行（证据缺失这件事本身）

每个策略都报 **真实 token（API usage）**，估算器只用来做决策 —— 两者不混用。

跑法：
    python day41_context.py                 # 全部（约 20-25 次调用）
    python day41_context.py --only S0,S1    # 只跑两条
"""
import os
import re
import sys
import json
import time
import argparse

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)

from dotenv import load_dotenv
load_dotenv(os.path.join(HERE, ".env"))

import day32_function_calling as d32
import context as ctx

LOG = "big/app_3day.log"                       # 相对**沙箱根**（logs/）—— 工具层就是这么解析的
LOG_FS = os.path.join(d32.ALLOWED_ROOT, LOG)   # 直接读文件时用绝对路径，别把两种约定混用
QUESTION = "分析这份应用日志，判断根因并给出处置建议"

# 每个策略给最终报告节点的**证据预算**（token）
EVIDENCE_BUDGET = 2000

SUBTASKS = [
    ("数据库连接类故障", "ERROR [db]"),
    ("磁盘空间类故障", "No space left"),
    ("日志/进程异常", "degraded"),
    ("证据完整性", "dropped"),
]

REPORT_SYS = ("你是 SRE 主管。只依据给定材料回答，材料里没有的就直说不知道。"
              "输出中文，250 字内，含根因、处置、局限。")
SUBAGENT_SYS = ctx.SUBAGENT_SYS


# ══════════════════════════════════════════════════════════════════
# 仪表：真实 token 一律来自 API usage
# ══════════════════════════════════════════════════════════════════
class Meter:
    def __init__(self, name):
        self.name = name
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.max_prompt = 0
        self.seconds = 0.0
        self.usage = []

    def sink(self):
        return self.usage

    def collect(self):
        # ⚠️ 必须**原地清空**：写成 self.usage = [] 会重新绑定列表，
        # 而 llm 闭包还握着旧列表 → 之后的调用全部计不到账（实测踩到过：S3 显示 0 次调用）
        for u in self.usage:
            self.calls += 1
            self.prompt_tokens += u["prompt_tokens"]
            self.completion_tokens += u["completion_tokens"]
            self.max_prompt = max(self.max_prompt, u["prompt_tokens"])
        self.usage.clear()


def judge(report):
    """程序判质量：客观特征 + 负向断言（Day 38 的三层判据搬到上下文实验里）"""
    checks = {
        "DB 故障": any(k in report for k in ("5432", "connection refused", "连接被拒", "连接拒绝")),
        "磁盘故障": any(k in report for k in ("磁盘", "空间", "No space")),
        "不编造 OOM": not any(k in report for k in ("OutOfMemory", "内存溢出", "段错误", "segfault")),
        "提到丢 2048 行（加分）": "2048" in report,
    }
    must = ["DB 故障", "磁盘故障", "不编造 OOM"]
    return all(checks[k] for k in must), checks


def report_once(llm, meter, evidence_label, evidence_text, extra_notes=""):
    user = (f"问题：{QUESTION}\n\n"
            f"{extra_notes}"
            f"【{evidence_label}】\n{evidence_text}")
    t0 = time.perf_counter()
    out = llm(REPORT_SYS, user)
    meter.seconds += time.perf_counter() - t0
    meter.collect()
    return out


# ══════════════════════════════════════════════════════════════════
# 五个策略
# ══════════════════════════════════════════════════════════════════
def s0_naive(llm, meter):
    """朴素做法：把命中行尽量全塞 —— 上下文有多大是多大"""
    raw = d32.search_log(LOG, "ERROR", max_hits=800)
    rep = report_once(llm, meter, "检索命中（原样，未做预算控制）", raw)
    return rep, {"evidence_tokens_est": ctx.estimate_tokens(raw),
                 "evidence_chars": len(raw), "说明": "max_hits=800，全塞"}


def s1_budget(llm, meter):
    """预算感知装填：按预算挑，并明确告诉模型省了什么"""
    raw = d32.search_log(LOG, "ERROR", max_hits=800)
    body, st = ctx.fit_snippets([ctx.Snippet(f"{LOG} 检索命中", raw, 1.0)],
                                max_tokens=EVIDENCE_BUDGET)
    rep = report_once(llm, meter, "检索命中（预算内装填）", body)
    return rep, {"evidence_tokens_est": st["used_tokens"], "fit": st,
                 "说明": f"预算 {EVIDENCE_BUDGET} token"}


def s2_compact(llm, meter):
    """压缩：分块摘要 → 递进合并，再把摘要当证据"""
    raw = d32.search_log(LOG, "ERROR", max_hits=800)
    summary, st = ctx.compact_text(raw, llm, block_tokens=2500, target_tokens=600)
    meter.collect()
    rep = report_once(llm, meter, "压缩后的事实摘要", summary)
    return rep, {"evidence_tokens_est": ctx.estimate_tokens(summary), "compact": st,
                 "说明": f"压缩 {st['blocks']} 块 → {st['output_tokens_est']} token"}


def s3_subagent(llm, meter):
    """子 agent：独立上下文取证，只回传结论 —— 父级上下文只增加"结论"，不增加"证据" """
    conclusions, ev_tokens, ret_tokens = [], 0, 0
    for goal, keyword in SUBTASKS:
        raw = d32.search_log(LOG, keyword, max_hits=200)
        meter.collect()
        sub_llm = llm                      # usage 统一由 meter 收
        conc, st = ctx.run_subagent(goal, raw, sub_llm, max_evidence_tokens=3000, label=keyword)
        meter.collect()
        conclusions.append((goal, conc))
        ev_tokens += st["evidence_tokens_est"]
        ret_tokens += st["returned_tokens_est"]
    body = "\n".join(f"[{g}] {c}" for g, c in conclusions)
    rep = report_once(llm, meter, "子 agent 结论（证据已被隔离在子上下文里）", body)
    info = {"evidence_tokens_est": ctx.estimate_tokens(body),
            "子agent证据token合计": ev_tokens, "子agent回传token合计": ret_tokens,
            "说明": f"{len(SUBTASKS)} 个子 agent，证据{ev_tokens}→回传{ret_tokens} token"}
    return rep, info, conclusions


def s4_notes(llm, meter, store, conclusions):
    """外部记忆：结论落盘 → 第二轮追问只注入相关笔记（**复用 S3 的子结论，不重复取证**）"""
    for goal, conc in conclusions:
        store.add(f"{goal}：{conc}", kind="finding", tags=["诊断", "3day", goal])
    recalled = store.recall("处置 清理 磁盘 数据库 重启 时间线", k=6)
    notes = store.render(recalled)
    rep = report_once(llm, meter, "（本轮无原始证据）",
                      "本轮是同一事件上的追问：上一轮结论以笔记形式提供，**不重新读取日志**。",
                      extra_notes=notes + "\n")
    return rep, {"evidence_tokens_est": ctx.estimate_tokens(notes),
                 "笔记条数": len(store.all()), "召回条数": len(recalled),
                 "说明": "第二轮只注入笔记，零原文"}


# ══════════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None, help="只跑指定策略，如 S0,S1")
    args = ap.parse_args()

    if not os.path.exists(LOG_FS):
        print(f"找不到 {LOG_FS} —— 先跑：python make_biglog.py")
        return 1

    with open(LOG_FS, "r", encoding="utf-8") as f:
        raw_lines = f.readlines()
    total_chars = sum(len(x) for x in raw_lines)
    print("=" * 92)
    print(f"上下文工程实验：{LOG}")
    print(f"日志规模：{len(raw_lines)} 行 / {total_chars / 1024:.0f} KB / "
          f"**约 {total_chars / 2.43 / 1000:.0f}k token**（按实测 2.43 字符/token）")
    print(f"证据预算（S1/S2/S3）：{EVIDENCE_BUDGET} token ｜ 任务：{QUESTION}")
    print("=" * 92)

    client = d32.make_client()
    # 每次实验用**新的事件 id**：笔记按事件隔离，避免上一轮的笔记污染这一轮
    incident = f"exp-{time.strftime('%H%M%S')}"
    store = ctx.NoteStore(incident=incident)
    print(f"笔记事件 id：{incident}（按事件隔离，跨轮不污染）")

    runs = [("S0", "naive 全塞", s0_naive), ("S1", "预算装填", s1_budget),
            ("S2", "压缩", s2_compact), ("S3", "子 agent", s3_subagent)]
    only = {x.strip().upper() for x in args.only.split(",")} if args.only else None

    rows = []
    conclusions = []
    for key, title, fn in runs:
        if only and key not in only:
            continue
        meter = Meter(key)
        llm = ctx.make_llm(client, d32.MODEL, usage_sink=meter.sink(), label=key)
        print(f"\n▶ {key} · {title}")
        t0 = time.perf_counter()
        try:
            out = fn(llm, meter)
            rep, info = out[0], out[1]
            if key == "S3":
                conclusions = out[2]
        except Exception as e:
            print(f"  ❌ {type(e).__name__}: {e}")
            continue
        wall = time.perf_counter() - t0
        ok, checks = judge(rep)
        rows.append((key, title, meter, wall, ok, checks, info))
        print(f"  调用 {meter.calls} 次｜真实 prompt token {meter.prompt_tokens}"
              f"（单次最大 {meter.max_prompt}）｜输出 {meter.completion_tokens}｜墙钟 {wall:.1f}s")
        print(f"  质量：{'✅ 通过' if ok else '❌ 不通过'}  " +
              "｜".join(f"{k}{'✔' if v else '✘'}" for k, v in checks.items()))
        print(f"  证据：{info.get('说明', '')}")
        print(f"  报告摘录：{rep.replace(chr(10), ' ')[:150]}…")

    if (not only or "S4" in only) and conclusions:
        print("\n▶ S4 · 外部记忆（在 S3 之后：第二轮追问，只注入笔记）")
        meter = Meter("S4")
        llm = ctx.make_llm(client, d32.MODEL, usage_sink=meter.sink(), label="S4")
        rep, info = s4_notes(llm, meter, store, conclusions)
        ok, checks = judge(rep)
        rows.append(("S4", "外部记忆追问", meter, 0.0, ok, checks, info))
        print(f"  调用 {meter.calls} 次｜真实 prompt token {meter.prompt_tokens}"
              f"（单次最大 {meter.max_prompt}）")
        print(f"  质量：{'✅ 通过' if ok else '❌ 不通过'}  " +
              "｜".join(f"{k}{'✔' if v else '✘'}" for k, v in checks.items()))
        print(f"  证据：{info.get('说明', '')}（笔记 {info.get('笔记条数')} 条，召回 {info.get('召回条数')} 条）")
        print(f"  报告摘录：{rep.replace(chr(10), ' ')[:150]}…")

    # ── 汇总表 ────────────────────────────────────────────────
    print("\n" + "=" * 92)
    print("汇总（token 全部是 API 返回的真实值）")
    print("=" * 92)
    print(f"{'策略':<6}{'调用':>4}{'prompt token':>13}{'单次最大':>9}{'输出':>7}"
          f"{'墙钟':>7}{'质量':>6}  证据策略")
    print("-" * 92)
    for key, title, m, wall, ok, checks, info in rows:
        print(f"{key:<6}{m.calls:>4}{m.prompt_tokens:>13}{m.max_prompt:>9}"
              f"{m.completion_tokens:>7}{wall:>6.1f}s{'✅' if ok else '❌':>6}  {info.get('说明','')[:34]}")
    print("-" * 92)
    if rows:
        base = rows[0]
        print(f"对比基准 S0：prompt token {base[2].prompt_tokens}、单次最大 {base[2].max_prompt}")
        for key, title, m, wall, ok, checks, info in rows[1:]:
            if base[2].prompt_tokens:
                cut = (1 - m.prompt_tokens / base[2].prompt_tokens) * 100
                print(f"  {key}：prompt token {'↓' if cut > 0 else '↑'}{abs(cut):.0f}%"
                      f"（单次最大 {m.max_prompt}）｜质量 {'保持' if ok else '下降'}")
    print("\n读法：S0 的代价体现在**单次最大 prompt**（每次都重发整段证据）；"
          "\n      S2/S3 的代价体现在**调用次数**（多花调用换更小的上下文）；"
          "\n      S4 展示的是'记住'而不是'重读'——零原文进场。")

    out = os.path.join(HERE, "context_experiment.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump([{ "strategy": k, "title": t, "calls": m.calls,
                     "prompt_tokens": m.prompt_tokens, "max_prompt_tokens": m.max_prompt,
                     "completion_tokens": m.completion_tokens, "wall_s": round(w, 1),
                     "quality_pass": ok, "checks": c, "info": i}
                   for k, t, m, w, ok, c, i in rows], f, ensure_ascii=False, indent=2)
    print(f"\n明细已写入：{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
