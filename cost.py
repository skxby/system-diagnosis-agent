#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""作品 3 · 成本控制三件套 —— cost.py

监控是**反复跑**的：每 5 分钟一次，一天 288 次。所以"单次便宜"不够，要"**重复也不花钱**"。

    ┌─ ① 归因缓存：状态没变就不重复花钱（指纹 + TTL）
    ├─ ② 模型分级：仅有 warning 用便宜档；critical/复杂才上强档；无发现直接不调
    └─ ③ 成本账本：跨运行记账 + 按小时限额刹车（超了只出规则结论，不偷偷花钱）

═══════════════════════════════════════════════════════════════════
两条诚实原则
═══════════════════════════════════════════════════════════════════
**① 不内置价格**：模型价格随时变，编一个"每百万 token X 元"就是骗人。
    账本只报 **token**；真的想算钱就传 `--price-in/--price-out` 自己填。

**② 缓存必须按"发现"做指纹，不是按时间**：时间没变但故障变了，必须重新归因；
    时间变了但状态一样，没必要再花钱。指纹取规则发现 + 关键指标摘要，
    所以 **任何影响结论的变化都会让指纹变**。
"""
import os
import time
import json
import hashlib

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CACHE = os.path.join(HERE, "snapshots", "attrib_cache.json")
DEFAULT_LEDGER = os.path.join(HERE, "snapshots", "cost.jsonl")
CACHE_MAX_ENTRIES = 200


# ══════════════════════════════════════════════════════════════════
# ① 归因缓存
# ══════════════════════════════════════════════════════════════════
def _quantize_percent(text, bucket):
    """把 evidence 里的**百分比**按桶量化（默认 5 个百分点一桶）。

    为什么必须做这件事（实测踩到）：指纹里放原始数字时，可用内存 13.2% → 12.8% 这种
    **正常抖动**就会让缓存永远命中不了 —— 监控每 5 分钟跑一次，等于每次都白花钱。

    为什么**只量化百分比**：
      · 百分比是抖动的来源（内存/负载/温度都在小幅飘）
      · IP、端口（10.20.31.7:5432）、计数（丢 2048 行、oom_kill 3 次）是**结构性事实**，
        它们一变通常意味着故障变了 → 必须让指纹变（量化它们反而会掩盖变化）
    ➡️ 一句话：**抗抖动，但不掩盖实质变化**；TTL 是第二道保险。
    """
    if not bucket:
        return text
    import re
    return re.sub(r"(\d+(?:\.\d+)?)(\s*%)",
                  lambda m: f"{int(float(m.group(1)) // bucket * bucket)}{m.group(2)}", text)


def findings_fingerprint(findings, extra=None, bucket=5):
    """把"这次到底发现了什么"压成一个指纹。

    参与计算的字段：rule / severity / subject / evidence（**不含** suggestion —— 它是模板固定文案），
    外加调用方给的 extra（比如关键指标摘要）。
    evidence 里的百分比先按 `bucket` 分桶（默认 5 个百分点）以**抗抖动**；
    IP/端口/计数保持精确，所以**任何实质变化都会改指纹 → 自动失效**。
    """
    parts = [f"{f.get('rule')}|{f.get('severity')}|{f.get('subject')}|"
             f"{_quantize_percent(f.get('evidence') or '', bucket)}"
             for f in (findings or [])]
    parts.sort()                                     # 顺序无关：同一组发现指纹相同
    if extra:
        parts.append(json.dumps(extra, ensure_ascii=False, sort_keys=True))
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()[:16]


class AttributionCache:
    """归因文本缓存。**只缓存模型输出**（规则/趋势结论本来就是免费重算的）。"""

    def __init__(self, path=DEFAULT_CACHE, ttl_seconds=1800):
        self.path = path
        self.ttl = ttl_seconds
        self.hits, self.misses = 0, 0

    def _load(self):
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}                                # 缓存坏了就当没有（缓存永远不该拖垮主流程）

    def _save(self, data):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        # 按时间留最近 N 条：缓存也不能无界增长
        if len(data) > CACHE_MAX_ENTRIES:
            keep = sorted(data.items(), key=lambda kv: kv[1].get("ts", 0), reverse=True)
            data = dict(keep[:CACHE_MAX_ENTRIES])
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)                   # 原子替换：避免半截文件

    def get(self, fingerprint):
        entry = self._load().get(fingerprint)
        if not entry:
            self.misses += 1
            return None
        if time.time() - entry.get("ts", 0) > self.ttl:
            self.misses += 1
            return None                              # 过期：宁可重算，也不用可能过时的结论
        self.hits += 1
        entry["age_seconds"] = round(time.time() - entry.get("ts", 0))
        return entry

    def put(self, fingerprint, text, model="", tokens_in=0, tokens_out=0):
        data = self._load()
        data[fingerprint] = {"ts": time.time(), "text": text, "model": model,
                             "tokens_in": tokens_in, "tokens_out": tokens_out}
        self._save(data)
        return data[fingerprint]

    def stats(self):
        return {"hits": self.hits, "misses": self.misses, "path": self.path, "ttl": self.ttl}


# ══════════════════════════════════════════════════════════════════
# ② 模型分级
# ══════════════════════════════════════════════════════════════════
def choose_model(severity, n_findings, cheap_model, strong_model,
                 complex_findings=5):
    """按"这次有多难"选模型。返回模型名，或 **None 表示不必调模型**。

    ⚠️ 这里**不写死任何"更强的模型名"** —— 供应商的型号与能力随时变，
    写死就会在半年后变成"文档里的谎言"。档位由调用方通过
    `--model-cheap/--model-strong` 给，默认都指向当前已核验的模型。
    """
    if not n_findings:
        return None                                  # 无发现 → 程序结论就够，别花钱
    if severity == "critical" or n_findings >= complex_findings:
        return strong_model
    return cheap_model


def summarize_usage(meter):
    """把 `ctx.make_llm` 的 usage_sink 汇总成**真实总量**（含重试烧掉的那部分）。

    ★ 实测踩到的记账 bug：原来只取 `meter[-1]`，也就是"最后一次尝试成功的那次"。
    但推理型模型在 `max_tokens` 偏小时会**把预算全烧在推理上、正文为空**，触发自动放大重试
    （见 context.make_llm 的空正文守卫）。于是账本里只有成功那次，
    **废掉的那次 2000 token 被悄悄漏掉了** —— 成本工具自己漏记成本，是最不能接受的 bug：
    它会让你以为省了，其实没省。

    ★ 附带实测（同一个提示词、同一台机器、真实 API）：
        max_tokens=2000 → 1 次尝试，推理 1118 字，正文 583 字，completion 1069
        max_tokens=4000 → 1 次尝试，推理 4131 字，正文 445 字，completion 2833
    推理 token 是**弹性**的：上限给得越大它想得越久，**正文反而更短**。
    所以上限不能"越大越安全"，得按任务实测调 —— 而调的依据就是这里汇总出来的数字。
    """
    if not meter:
        return {"attempts": 0, "prompt_tokens": 0, "completion_tokens": 0,
                "total_tokens": 0, "wasted_completion_tokens": 0, "wasted_ratio": 0.0,
                "reasoning_chars": 0, "content_chars": 0, "per_attempt": []}
    comp = sum(m.get("completion_tokens", 0) for m in meter)
    prom = sum(m.get("prompt_tokens", 0) for m in meter)
    wasted = sum(m.get("completion_tokens", 0) for m in meter
                 if not m.get("content_chars"))          # 正文为空 = 这一次白烧
    return {
        "attempts": len(meter),
        "prompt_tokens": prom, "completion_tokens": comp, "total_tokens": prom + comp,
        "wasted_completion_tokens": wasted,
        "wasted_ratio": round(wasted / comp, 3) if comp else 0.0,
        "reasoning_chars": sum(m.get("reasoning_chars", 0) for m in meter),
        "content_chars": meter[-1].get("content_chars", 0),
        "per_attempt": [{"attempt": m.get("attempt"), "max_tokens": m.get("max_tokens"),
                         "prompt_tokens": m.get("prompt_tokens"),
                         "completion_tokens": m.get("completion_tokens"),
                         "content_chars": m.get("content_chars"),
                         "reasoning_chars": m.get("reasoning_chars"),
                         "finish_reason": m.get("finish_reason")} for m in meter],
    }


# ══════════════════════════════════════════════════════════════════
# ③ 成本账本（跨运行）
# ══════════════════════════════════════════════════════════════════
class CostLedger:
    """每次体检记一条。用于：① 回答"这个工具一天花多少"；② **按小时限额刹车**。

    为什么必须有跨运行账本：单次预算（budget.py）只管一次运行；
    而监控是循环 —— 真正会失控的是"**每小时跑 12 次 × 每次多花 3 倍**"这种累积。
    """

    def __init__(self, path=DEFAULT_LEDGER, hourly_token_limit=None):
        self.path = path
        self.hourly_token_limit = hourly_token_limit

    def record(self, host, calls=0, tokens_in=0, tokens_out=0, cached=False,
               model="", seconds=0.0, attempts=0, wasted=0):
        rec = {"ts": time.time(), "iso": time.strftime("%Y-%m-%d %H:%M:%S"), "host": host,
               "calls": calls, "tokens_in": tokens_in, "tokens_out": tokens_out,
               "cached": bool(cached), "model": model, "seconds": round(seconds, 2),
               # ★ 重试也要记账：否则"白烧的 token"永远看不见（实测踩到过）
               "attempts": attempts, "wasted_tokens": wasted}
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec

    def load(self, since_seconds=None):
        if not os.path.exists(self.path):
            return []
        cutoff = time.time() - since_seconds if since_seconds else None
        out = []
        with open(self.path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if cutoff and rec.get("ts", 0) < cutoff:
                    continue
                out.append(rec)
        return out

    def tokens_last_hour(self):
        return sum(r["tokens_in"] + r["tokens_out"] for r in self.load(since_seconds=3600))

    def allow(self, estimated_tokens):
        """还能不能再调一次模型。返回 (bool, 原因)。"""
        if not self.hourly_token_limit:
            return True, ""
        used = self.tokens_last_hour()
        if used + estimated_tokens > self.hourly_token_limit:
            return False, (f"本小时已用 {used} token，再调需 {estimated_tokens}，"
                           f"超过限额 {self.hourly_token_limit}（只出规则结论，不偷偷花钱）")
        return True, ""

    def summary(self, price_in=0.0, price_out=0.0):
        day = self.load(since_seconds=86400)
        hour = self.load(since_seconds=3600)
        tin = sum(r["tokens_in"] for r in day)
        tout = sum(r["tokens_out"] for r in day)
        out = {"runs_today": len(day), "runs_last_hour": len(hour),
               "calls_today": sum(r["calls"] for r in day),
               "tokens_in_today": tin, "tokens_out_today": tout,
               "tokens_total_today": tin + tout,
               "cached_runs_today": sum(1 for r in day if r.get("cached")),
               "tokens_last_hour": sum(r["tokens_in"] + r["tokens_out"] for r in hour),
               # 重试浪费：账本要能回答"有多少 token 是白烧的"（老记录没这两列，按 0 算）
               "retry_runs_today": sum(1 for r in day if (r.get("attempts") or 0) > 1),
               "wasted_tokens_today": sum(r.get("wasted_tokens") or 0 for r in day)}
        # 只有调用方给了单价才算钱（不内置价格：供应商随时调价，编一个数是骗人）
        if price_in or price_out:
            out["cost_today"] = round(tin / 1e6 * price_in + tout / 1e6 * price_out, 4)
            out["price_note"] = f"按 输入 {price_in}/百万 · 输出 {price_out}/百万 估算"
        else:
            out["cost_today"] = None
            out["price_note"] = "未给单价：只统计 token（价格请用 --price-in/--price-out 自行填）"
        return out


def render_cost(summary):
    lines = [f"成本账本：今日 {summary['runs_today']} 次体检（其中缓存命中 "
             f"{summary['cached_runs_today']} 次）｜模型调用 {summary['calls_today']} 次｜"
             f"token {summary['tokens_total_today']}（本小时 {summary['tokens_last_hour']}）"]
    if summary.get("wasted_tokens_today"):
        lines.append(f"  ⚠ 其中重试白烧 {summary['wasted_tokens_today']} token"
                     f"（{summary['retry_runs_today']} 次运行触发了空正文重试）"
                     " —— 说明输出上限偏小，可调 --max-output-tokens 试")
    if summary["cost_today"] is not None:
        lines.append(f"  估算费用：{summary['cost_today']}（{summary['price_note']}）")
    else:
        lines.append(f"  {summary['price_note']}")
    return "\n".join(lines)
