#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""预算刹车的离线测试 —— test_budget.py（**不调用任何模型**，秒级、100% 确定）

为什么预算这种东西一定要有离线测试：
  · 它的 bug 形态是"**超卖**"和"**死锁**" —— 都不会报错，只会让账不对 / 进程挂住
  · 而它在生产里的触发条件是**并发 + 满负载**，你没法靠手工点几下碰出来

跑法：
    .venv-agent\\Scripts\\python.exe test_budget.py
"""
import sys
import time
import threading

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from budget import Budget, BudgetExceeded


class NullTracerStub:
    """给 day36.llm() 用的空追踪（免得测试去写 trace 文件）。

    注意要产出一个**带 set() 的句柄** —— 真实的 trace_util.NullTracer 就是这么做的；
    测试桩偷懒用 nullcontext() 会 yield 出 None，然后把被测代码带崩（我第一次就踩了）。
    """

    backend = "none"

    class _Span:
        def set(self, **kwargs):
            pass

    def span(self, name, **attrs):
        from contextlib import contextmanager

        @contextmanager
        def cm():
            yield NullTracerStub._Span()

        return cm()

    def event(self, *a, **k):
        pass


PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}{('  —— ' + detail) if detail else ''}")


def expect_exceeded(fn, kind_word):
    """跑 fn，期望抛 BudgetExceeded 且原因里含关键字"""
    try:
        fn()
    except BudgetExceeded as e:
        return kind_word in str(e)
    return False


def main():
    t_all = time.perf_counter()

    # ── 1. 次数上限：预留即占坑 ──────────────────────────────
    print("[1] 次数上限")
    b = Budget(max_llm_calls=2, max_tokens=10**6, max_seconds=600)
    e1, _ = b.reserve_llm(100)
    e2, _ = b.reserve_llm(100)
    check("两次预留成功", b.llm_calls == 2)
    check("第三次被拒（次数）", expect_exceeded(lambda: b.reserve_llm(100), "次数"))
    b.settle_llm(e1, 50, 20, 1.0)
    b.settle_llm(e2, 50, 20, 1.0)
    check("结算后 token 累计正确", b.tokens_total == 140, f"tokens_total={b.tokens_total}")
    check("预留已清零", b.reserved_tokens == 0, f"reserved={b.reserved_tokens}")

    # ── 2. token 上限：先估后结，差额退回 ────────────────────
    print("\n[2] token 上限（预估 + 结算）")
    b = Budget(max_llm_calls=100, max_tokens=3000, max_seconds=600)
    est, _ = b.reserve_llm(2000)      # 估 2000/2 + 600 = 1600
    check("预留把估算计入占用", b.reserved_tokens == est, f"est={est}")
    # 再估一次就应当超（1600 + 至少 600 > 3000）
    check("第二次预估被拒（token）", expect_exceeded(lambda: b.reserve_llm(2000), "token"))
    b.settle_llm(est, 300, 120, 2.0)  # 实际只用了 420，远小于预留
    check("结算后占用按实际算", b.tokens_total == 420 and b.reserved_tokens == 0,
          f"total={b.tokens_total} reserved={b.reserved_tokens}")
    check("退回后又能继续调用", (b.reserve_llm(500) or True))

    # ── 3. 墙钟 + 单次调用超时（"能拒"还要"能截"）───────────
    print("\n[3] 墙钟与单次调用超时")
    b = Budget(max_llm_calls=100, max_tokens=10**6, max_seconds=30, default_call_timeout=120)
    _, timeout = b.reserve_llm(100)
    check("超时取剩余墙钟（<默认）", timeout <= 30, f"timeout={timeout}")
    b2 = Budget(max_llm_calls=100, max_tokens=10**6, max_seconds=1000, default_call_timeout=120)
    _, timeout2 = b2.reserve_llm(100)
    check("剩余充足时取默认上限", timeout2 == 120, f"timeout={timeout2}")
    b3 = Budget(max_llm_calls=100, max_tokens=10**6, max_seconds=0.5)
    check("墙钟用尽直接拒", expect_exceeded(lambda: b3.reserve_llm(100), "墙钟"))

    # ── 4. 工具次数 ──────────────────────────────────────────
    print("\n[4] 工具次数上限")
    b = Budget(max_llm_calls=10, max_tokens=10**6, max_tool_calls=2, max_seconds=600)
    b.reserve_tool("search_log")
    b.reserve_tool("search_log")
    check("第三次工具调用被拒", expect_exceeded(lambda: b.reserve_tool("search_log"), "工具"))

    # ── 5. ★并发超卖测试：锁到底有没有用 ────────────────────
    # 这是预算最核心的一条：check 和 +1 必须在同一把锁里。
    # 不加锁时典型结果是"20 个线程抢 5 个名额 → 成功 7~8 个"（超卖）。
    print("\n[5] 并发超卖（20 线程抢 5 个名额）")
    b = Budget(max_llm_calls=5, max_tokens=10**6, max_seconds=600)
    ok, denied = [], []

    def worker(i):
        try:
            b.reserve_llm(100)
            ok.append(i)
        except BudgetExceeded:
            denied.append(i)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("恰好 5 个成功（不超卖、不饿死）", len(ok) == 5, f"成功 {len(ok)} 个")
    check("其余 15 个被拒", len(denied) == 15, f"被拒 {len(denied)} 个")

    # ── 6. 死锁回归 ─────────────────────────────────────────
    # 曾经的写法：在**持锁**的代码块里调 snapshot()，而 snapshot() 自己也要锁
    # → threading.Lock 不可重入 → 进程挂住（不报错！）。
    # 上面 [1][2] 的"被拒"路径正好走的就是"持锁时构造快照"这段代码：
    # 若死锁还在，那几条会永久挂住。这里再加一条显式的：别的线程持锁时 snapshot 要能等到。
    print("\n[6] 死锁回归（持锁期间读快照）")
    b = Budget(max_llm_calls=1, max_tokens=10**6, max_seconds=600)
    got = {}

    def reader():
        got["snap"] = b.snapshot()

    b._lock.acquire()
    th = threading.Thread(target=reader)
    th.start()
    time.sleep(0.2)
    blocked_as_expected = th.is_alive()      # 应当被挡在锁外
    b._lock.release()
    th.join(timeout=2.0)
    check("读快照会等锁（不是死锁）", blocked_as_expected and not th.is_alive())
    check("拿到快照且内容完整", "llm_calls" in got.get("snap", {}))
    # 被拒后仍能拿快照（异常里带的就是它）
    try:
        b.reserve_llm(10)
        b.reserve_llm(10)
    except BudgetExceeded as e:
        check("异常快照里有被拒记录", e.snapshot["blocked"] and "次数" in e.reason,
              str(e)[:40])

    # ── 7. 降级报告不需要额外花钱 ───────────────────────────
    print("\n[7] 降级报告")
    b = Budget(max_llm_calls=0, max_tokens=10**6, max_seconds=600, label="诊断-紧预算")
    try:
        b.reserve_llm(100)
    except BudgetExceeded:
        pass
    rep = b.report()
    check("报告含预算快照与被拒原因", "预算：" in rep and "被拒" in rep)

    # ── 8. ★重试必须记账（注入假 client，完全确定性）────────
    # 这条测的是 day36.llm() 里的重试循环，不是 Budget 自己：
    #   · 每次尝试都要**重新占一个名额**（重试是预算的放大器）
    #   · 尝试失败也要把预留退回去
    #   · 次数用尽时，重试同样被预算拒绝（不是"重试免费"）
    print("\n[8] 重试记账（假 client 注入）")
    from types import SimpleNamespace
    import day36_graph_mcp as g36

    class APITimeoutError(Exception):      # 按**类名**匹配（day36 就是这么判的）
        pass

    def stub(fail_times, text="OK"):
        state = {"n": 0}

        def create(**kw):
            state["n"] += 1
            if state["n"] <= fail_times:
                raise APITimeoutError("模拟超时")
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=text),
                                         finish_reason="stop")],
                usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5))

        client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=create)))
        return client, state

    msgs = [{"role": "user", "content": "hi"}]
    g36.TRACER = NullTracerStub()

    # 8a. 两次都超时 → 抛 CallTimeout，且**两次尝试都记了账**
    g36.BUDGET = Budget(max_llm_calls=5, max_tokens=10**6, max_seconds=600)
    g36._CLIENT, st = stub(fail_times=2)
    try:
        g36.llm("t", msgs)
        check("8a 两次超时应抛 CallTimeout", False)
    except Exception as e:
        check("8a 两次超时抛 CallTimeout", type(e).__name__ == "CallTimeout", type(e).__name__)
    check("8a 两次尝试都计入账本", g36.BUDGET.llm_calls == 2, f"llm_calls={g36.BUDGET.llm_calls}")
    check("8a 预留已全部释放", g36.BUDGET.reserved_tokens == 0,
          f"reserved={g36.BUDGET.reserved_tokens}")

    # 8b. 第一次超时、第二次成功 → 返回文本，账上是 2 次调用
    g36.BUDGET = Budget(max_llm_calls=5, max_tokens=10**6, max_seconds=600)
    g36._CLIENT, st = stub(fail_times=1)
    out = g36.llm("t", msgs)
    check("8b 重试后成功返回", out == "OK", out)
    check("8b 重试也占名额（共 2 次）", g36.BUDGET.llm_calls == 2,
          f"llm_calls={g36.BUDGET.llm_calls}")
    check("8b token 按成功的这次结算", g36.BUDGET.tokens_total == 15,
          f"tokens={g36.BUDGET.tokens_total}")

    # 8c. 只剩 1 个名额：第一次用掉，**重试被预算拒绝**（重试不免费）
    g36.BUDGET = Budget(max_llm_calls=1, max_tokens=10**6, max_seconds=600)
    g36._CLIENT, st = stub(fail_times=1)
    try:
        g36.llm("t", msgs)
        check("8c 重试应被预算拒绝", False)
    except Exception as e:
        check("8c 重试被预算拒绝（BudgetExceeded）",
              type(e).__name__ == "BudgetExceeded", type(e).__name__)
    check("8c 账上只记了 1 次", g36.BUDGET.llm_calls == 1, f"llm_calls={g36.BUDGET.llm_calls}")

    print("\n" + "=" * 72)
    print(f"结果：{len(PASS)} 项通过，{len(FAIL)} 项失败（耗时 {time.perf_counter() - t_all:.2f}s）")
    if FAIL:
        for f in FAIL:
            print(f"  ✗ {f}")
        sys.exit(1)
    print("刹车逻辑本身就该是确定性的 —— 所以这些测试不花一分钱，也不需要 key。")


if __name__ == "__main__":
    main()
