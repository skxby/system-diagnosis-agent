#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""作品 3 · 运行时预算（刹车）—— budget.py

Day 32 留下的洞：**只限轮数不等于限住成本**。一轮可能返回 N 个 tool_call，
再加并行分支、重试、评测多次运行 —— 账会滚雪球。所以要有四条硬上限：

    max_llm_calls   总 LLM 调用次数
    max_tokens      总 token（输入+输出）
    max_tool_calls  总工具调用次数
    max_seconds     墙钟总时长

═══════════════════════════════════════════════════════════════════
两个不那么直觉、但决定刹车灵不灵的点：
═══════════════════════════════════════════════════════════════════

**① 刹车要能"拒"，也要能"截"。**
    只在调用"之间"检查计数，最坏会超一整次调用 —— 实测单次 LLM 调用可以从 9.7s 飙到 99.9s。
    所以 reserve_llm() 顺带返回**本次调用允许的超时**（= min(剩余墙钟, 默认上限)），
    把它交给 HTTP 客户端（openai SDK 的 timeout 参数）→ 在途的调用也能被截断。

**② 预算是"该共享"的状态，必须加锁。**
    Day 37 的 span 栈必须**隔离**（contextvars），预算则相反 —— 它是全流程共享的一条账。
    并发分支同时"读-判断-加"会超卖，所以 check 和 +1 必须在同一把锁里。
    这就是**预留（reserve）语义**：调用前先占坑，调用后再结算实际用量。
    一句话：**该隔离的隔离，该共享的共享，两者都要显式设计**。

**③ token 只能"先估后结"**：调用前还不知道真实用量，只能按 prompt 长度估一个预留值，
    调用后按 usage 结算并把差额退回来。所以 token 预算是"近似硬"，
    代价是可能提前一点点刹车 —— 这比事后发现账单超了强。
"""
import time
import threading


class BudgetExceeded(Exception):
    """预算用尽。**不当作崩溃**：上层应该拿快照出一份"降级报告"，把已有证据交出去。"""

    def __init__(self, reason, snapshot):
        super().__init__(reason)
        self.reason = reason
        self.snapshot = snapshot


class CallTimeout(BudgetExceeded):
    """**第二种刹车**：不是账超了，而是"这一次调用跑太久了"，被剩余预算截断。

    为什么要分两种：
      · 拒绝下一单（BudgetExceeded）—— 发生在调用**之前**，账还是干净的
      · 截断在途的一单（CallTimeout）—— 发生在调用**进行中**，服务端可能已经算过一次
    处理方式一样（都走降级报告），但**原因要分清**，否则复盘时看不出是哪一类问题。
    """


# 一次调用按 prompt 字符数估 token 的保守系数（中文约 1 字 ≈ 0.6~1 token，这里按 0.5 估，
# 宁可估高一点：估高了会早刹车，估低了会超支）
CHARS_PER_TOKEN = 2.0
OUTPUT_RESERVE = 600          # 给输出留的余量（实测输出 200-500 token）


class Budget:
    def __init__(self, max_llm_calls=10, max_tokens=20000, max_tool_calls=12,
                 max_seconds=240.0, default_call_timeout=120.0, label="default"):
        self.label = label
        self.max_llm_calls = max_llm_calls
        self.max_tokens = max_tokens
        self.max_tool_calls = max_tool_calls
        self.max_seconds = max_seconds
        self.default_call_timeout = default_call_timeout

        self.started = time.perf_counter()
        self.llm_calls = 0
        self.tool_calls = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.reserved_tokens = 0        # 已预留未结算的部分（并发分支之间要算进去）
        self.blocked = []               # 被拒的记录：**刹车本身也要可观测**，否则你只会看到"跑得慢"
        self._lock = threading.Lock()

    # ── 内部：不加锁的纯读取 ────────────────────────────────
    @property
    def elapsed(self):
        return time.perf_counter() - self.started

    @property
    def tokens_total(self):
        return self.tokens_in + self.tokens_out

    def remaining_seconds(self):
        return self.max_seconds - self.elapsed

    def snapshot(self):
        with self._lock:
            return self._snapshot_unlocked()

    def _snapshot_unlocked(self):
        """**只在已持锁时调用**。

        ⚠️ 这里踩过一个雷：一开始我在持锁的代码块里直接调 `snapshot()`，
        而 `snapshot()` 自己也会加锁 → `threading.Lock` 不可重入 → **死锁**（进程挂住，不报错）。
        正解：拆一个"不加锁版本"给内部用。这类 bug 在单线程自测里根本跑不出来。
        """
        return {
            "label": self.label,
            "llm_calls": f"{self.llm_calls}/{self.max_llm_calls}",
            "tool_calls": f"{self.tool_calls}/{self.max_tool_calls}",
            "tokens": f"{self.tokens_total}+{self.reserved_tokens}(预留)/{self.max_tokens}",
            "elapsed_s": round(self.elapsed, 1),
            "max_seconds": self.max_seconds,
            "blocked": list(self.blocked),
        }

    def _deny_unlocked(self, kind, detail):
        self.blocked.append({"kind": kind, "detail": detail})
        raise BudgetExceeded(f"预算耗尽（{kind}）：{detail}", self._snapshot_unlocked())

    # ── LLM：调用前预留 + 拿到本次允许的超时 ─────────────────
    def reserve_llm(self, prompt_chars, label=""):
        """占坑。返回 (estimated_tokens, call_timeout_s)。超预算直接 raise。"""
        est = int(prompt_chars / CHARS_PER_TOKEN) + OUTPUT_RESERVE
        with self._lock:
            if self.llm_calls + 1 > self.max_llm_calls:
                self._deny_unlocked(
                    "次数", f"LLM 调用数 {self.llm_calls}+1 > 上限 {self.max_llm_calls}")
            if self.tokens_total + self.reserved_tokens + est > self.max_tokens:
                self._deny_unlocked(
                    "token",
                    f"预估 {self.tokens_total + self.reserved_tokens}+{est} > 上限 {self.max_tokens}")
            self.llm_calls += 1
            self.reserved_tokens += est
        # 墙钟：在锁外判（它单调递增，不需要锁）
        remaining = self.remaining_seconds()
        if remaining <= 1.0:
            with self._lock:
                self.blocked.append({"kind": "墙钟",
                                     "detail": f"剩余 {remaining:.1f}s ≤ 1s"})
            raise BudgetExceeded(f"预算耗尽（墙钟）：剩余 {remaining:.1f}s", self.snapshot())
        # ★ 关键：把剩余预算变成"本次调用的超时上限"，在途调用也能被截断
        return est, min(remaining, self.default_call_timeout)

    def settle_llm(self, estimated, tokens_in, tokens_out, seconds):
        """调用后结算：把预留换成真实用量（差额退回）。"""
        with self._lock:
            self.reserved_tokens = max(0, self.reserved_tokens - estimated)
            self.tokens_in += tokens_in
            self.tokens_out += tokens_out
        return {"tokens_in": tokens_in, "tokens_out": tokens_out, "seconds": seconds}

    # ── 工具：只计次（很快，不需要预留）────────────────────
    def reserve_tool(self, name=""):
        with self._lock:
            if self.tool_calls + 1 > self.max_tool_calls:
                self._deny_unlocked(
                    "工具次数", f"工具调用数 {self.tool_calls}+1 > 上限 {self.max_tool_calls}")
            self.tool_calls += 1
        remaining = self.remaining_seconds()
        if remaining <= 1.0:
            raise BudgetExceeded(f"预算耗尽（墙钟）：剩余 {remaining:.1f}s", self.snapshot())

    def report(self):
        """给降级报告用的一段人话（**不带任何模型调用** —— 刹车之后不该再花钱）。"""
        s = self.snapshot()
        lines = [f"预算：LLM {s['llm_calls']} 次 / 工具 {s['tool_calls']} 次 / "
                 f"token {s['tokens']} / 已用 {s['elapsed_s']}s（上限 {s['max_seconds']}s）"]
        for b in s["blocked"]:
            lines.append(f"  被拒：{b['detail']}")
        return "\n".join(lines)
