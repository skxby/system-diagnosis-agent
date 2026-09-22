#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""成本控制三件套的离线测试 —— test_cost.py（免费、秒级、不要 key）

**为什么成本控制必须有测试**：它的 bug 形态是"**悄悄多花钱**"或"**该省钱时没省**"：
  · 指纹算错 → 状态没变却每次重新归因（一天白花 288 次）
  · 缓存该失效没失效 → 拿旧结论糊弄新故障（**比多花钱严重得多**）
  · 限额算错 → 该刹没刹，或者该调时不调

覆盖：
    [1] 指纹：同组发现指纹相同（与顺序无关）；发现一变指纹就变；extra 参与计算
    [2] 缓存：miss → put → hit；TTL 过期后 miss；坏文件不炸；条数上限
    [3] 模型分级：无发现不调；warning→便宜档；critical/发现多→强档
    [4] 账本：记录/读回；一小时窗口过滤；限额拦截；不给单价就不算钱（诚实）
"""
import os
import sys
import json
import time
import shutil
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cost as C

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}{('  —— ' + str(detail)) if detail else ''}")


def f(rule, sev="warning", subject="/", evidence="已用 91%"):
    return {"rule": rule, "severity": sev, "subject": subject, "evidence": evidence,
            "suggestion": "固定文案"}


def main():
    t0 = time.perf_counter()

    # ── [1] 指纹 ──────────────────────────────────────────
    print("[1] 发现指纹")
    a = [f("DISK_USED_HIGH"), f("MEM_AVAILABLE_LOW")]
    b = [f("MEM_AVAILABLE_LOW"), f("DISK_USED_HIGH")]      # 顺序反了
    fa, fb = C.findings_fingerprint(a), C.findings_fingerprint(b)
    check("同一组发现（顺序无关）→ 同一指纹", fa == fb, f"{fa} vs {fb}")
    check("发现变化 → 指纹变化",
          C.findings_fingerprint([f("DISK_USED_HIGH", evidence="已用 96%")]) != fa)
    check("严重度变化 → 指纹变化",
          C.findings_fingerprint([f("DISK_USED_HIGH", sev="critical")]) != fa)
    check("extra（关键指标）参与计算",
          C.findings_fingerprint(a) != C.findings_fingerprint(a, extra={"load": 12.0}))
    check("空发现也有稳定指纹", C.findings_fingerprint([]) == C.findings_fingerprint([]))
    check("suggestion 不参与（它是模板文案）",
          C.findings_fingerprint([f("X")]) ==
          C.findings_fingerprint([{**f("X"), "suggestion": "另一段文案"}]))

    # ★ 抗抖动 vs 掩盖变化 —— 这条是实测踩出来的（缓存永远命不中）
    jitter1 = [f("MEM_AVAILABLE_LOW", "warning", "mem", "可用内存 13.2%")]
    jitter2 = [f("MEM_AVAILABLE_LOW", "warning", "mem", "可用内存 12.8%")]
    real1 = [f("DISK_USED_HIGH", "warning", "/", "已用 88.0%（剩余 12 GB）")]
    real2 = [f("DISK_USED_HIGH", "critical", "/", "已用 97.0%（剩余 0.9 GB）")]
    check("百分比正常抖动 → 指纹不变（抗抖动，监控才省得下来）",
          C.findings_fingerprint(jitter1) == C.findings_fingerprint(jitter2))
    check("百分比跨桶变化 → 指纹变化（不掩盖实质）",
          C.findings_fingerprint(real1) != C.findings_fingerprint(real2))
    check("IP/端口/计数保持精确（结构性事实）",
          C.findings_fingerprint([f("X", evidence="host=10.20.31.7:5432")]) !=
          C.findings_fingerprint([f("X", evidence="host=10.20.31.8:5432")]))
    check("bucket=0 时关闭量化（退回精确比较）",
          C.findings_fingerprint(jitter1, bucket=0) != C.findings_fingerprint(jitter2, bucket=0))

    tmp = tempfile.mkdtemp(prefix="cost_")
    try:
        # ── [2] 缓存 ──────────────────────────────────────
        print("\n[2] 归因缓存")
        cache = C.AttributionCache(path=os.path.join(tmp, "c.json"), ttl_seconds=60)
        fp = C.findings_fingerprint(a)
        check("未命中返回 None", cache.get(fp) is None)
        cache.put(fp, "这是归因文本", model="m1", tokens_in=800, tokens_out=300)
        hit = cache.get(fp)
        check("写入后命中，且带回原文", hit and hit["text"] == "这是归因文本")
        check("命中时报告年龄", hit and "age_seconds" in hit)
        check("统计命中/未命中次数", cache.stats()["hits"] == 1 and cache.stats()["misses"] == 1)
        cache2 = C.AttributionCache(path=os.path.join(tmp, "c.json"), ttl_seconds=0)
        check("TTL 过期 → 视为未命中（宁可重算）", cache2.get(fp) is None)
        with open(os.path.join(tmp, "c.json"), "w", encoding="utf-8") as fh:
            fh.write("{坏掉的 json")
        check("缓存文件损坏不炸（当没有）",
              C.AttributionCache(path=os.path.join(tmp, "c.json")).get(fp) is None)
        c3 = C.AttributionCache(path=os.path.join(tmp, "c3.json"), ttl_seconds=600)
        for i in range(C.CACHE_MAX_ENTRIES + 20):
            c3.put(f"fp{i:04d}", f"t{i}", tokens_in=1, tokens_out=1)
        with open(os.path.join(tmp, "c3.json"), encoding="utf-8") as fh:
            saved = json.load(fh)
        check(f"条数上限（不无界增长）：{len(saved)} ≤ {C.CACHE_MAX_ENTRIES}",
              len(saved) <= C.CACHE_MAX_ENTRIES, len(saved))
        check("裁剪保留最新的", saved.get(f"fp{C.CACHE_MAX_ENTRIES + 19:04d}") is not None)

        # ── [3] 模型分级 ──────────────────────────────────
        print("\n[3] 模型分级")
        check("无发现 → 不调模型", C.choose_model("ok", 0, "cheap", "strong") is None)
        check("只有 warning → 便宜档",
              C.choose_model("warning", 2, "cheap", "strong") == "cheap")
        check("critical → 强档",
              C.choose_model("critical", 1, "cheap", "strong") == "strong")
        check("发现很多（≥5）→ 强档",
              C.choose_model("warning", 5, "cheap", "strong") == "strong")
        check("阈值可调", C.choose_model("warning", 3, "cheap", "strong", complex_findings=3) == "strong")

        # ── [4] 成本账本 ──────────────────────────────────
        print("\n[4] 成本账本")
        lp = os.path.join(tmp, "cost.jsonl")
        led = C.CostLedger(path=lp, hourly_token_limit=1000)
        led.record("h1", calls=1, tokens_in=400, tokens_out=200, model="m")
        led.record("h1", calls=0, cached=True)
        check("记录能读回", len(led.load()) == 2)
        check("本小时 token 统计正确", led.tokens_last_hour() == 600, led.tokens_last_hour())
        check("限额内允许", led.allow(300)[0] is True)
        ok, reason = led.allow(500)
        check("超限额被拦，并给出原因", ok is False and "限额" in reason, reason[:44])
        # 手工塞一条“两小时前”的记录，验证窗口过滤
        with open(lp, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.time() - 7200, "host": "h1", "calls": 9,
                                 "tokens_in": 999999, "tokens_out": 0}) + "\n")
        check("一小时窗口外的记录不计入", led.tokens_last_hour() == 600, led.tokens_last_hour())
        check("无限额时永远允许", C.CostLedger(path=lp).allow(10 ** 9)[0] is True)
        s = led.summary()
        check("汇总：今日次数/调用/token 齐", s["runs_today"] == 3 and s["tokens_total_today"] > 0, s)
        check("未给单价 → 不算钱（诚实：不编造价格）",
              s["cost_today"] is None and "未给单价" in s["price_note"])
        s2 = led.summary(price_in=1.0, price_out=2.0)
        check("给了单价才算钱", s2["cost_today"] is not None and s2["cost_today"] > 0, s2["cost_today"])
        check("缓存命中次数被统计", s["cached_runs_today"] == 1, s["cached_runs_today"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 74)
    print(f"结果：{len(PASS)} 项通过，{len(FAIL)} 项失败（耗时 {time.perf_counter() - t0:.2f}s）")
    if FAIL:
        for x in FAIL:
            print(f"  ✗ {x}")
        sys.exit(1)
    print("成本控制是纯逻辑 + 临时文件 —— 免费、可复现，已进 CI 免费档。")


if __name__ == "__main__":
    main()
