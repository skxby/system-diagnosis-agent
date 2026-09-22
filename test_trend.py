#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""趋势与预测的离线测试 —— test_trend.py（免费、秒级、不要 key）

**为什么趋势必须有测试**：它的 bug 形态是"**给出一个像模像样的错误时间**"。
值班的人会照它安排工作 —— 说"还有 4 小时"结果 20 分钟后满盘，比不报还坏。
所以这里的断言集中在三件事：
    ① 能算对（对手工构造的斜线，预测时间必须在容差内）
    ② **不该算的时候坚决不算**（样本不足 / 跨度太短 / 变化在噪声内 → 必须返回 ok=False 且给出原因）
    ③ 与现状规则的分工（已经超阈值的不再预报；跳变要单独抓）

跑法：.venv-agent\\Scripts\\python.exe test_trend.py
"""
import os
import sys
import json
import time
import shutil
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import trend as T

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}{('  —— ' + str(detail)) if detail else ''}")


def rec(offset_s, used=None, inode=None, mem=None, temp=None, mount="/"):
    """造一条历史记录：offset_s 是相对第一条的秒数"""
    now = 1700000000.0
    return {"ts": f"t+{offset_s}", "ts_epoch": now + offset_s, "host": "h1", "worst": "ok",
            "metrics": {"disks": {mount: {"used_pct": used, "inode_used_pct": inode}},
                        "mem_available_pct": mem, "load1_per_cpu": None, "max_celsius": temp}}


def main():
    t0 = time.perf_counter()

    # ── [1] 拟合本身要对 ──────────────────────────────────
    print("[1] 线性拟合")
    fit = T.linear_fit([(0, 10.0), (3600, 12.0), (7200, 14.0)])
    check("完美斜线：斜率 = 2/小时（换算后）", abs(fit[0] * 3600 - 2.0) < 1e-9, fit)
    check("完美斜线：R² = 1", abs(fit[2] - 1.0) < 1e-9)
    check("单点不给拟合", T.linear_fit([(0, 1.0)]) is None)
    check("时间戳相同不给拟合", T.linear_fit([(5, 1.0), (5, 2.0)]) is None)

    # ── [2] 能算对：磁盘线性填满 ───────────────────────────
    print("\n[2] 磁盘填满预测（能算对）")
    # 每 600s 涨 1 个百分点：90% → 95% 需要 5 个点间隔 = 3000s = 0.83h
    recs = [rec(i * 600, used=80.0 + i) for i in range(11)]     # 80% → 90%
    fc = T.forecast([(r["ts_epoch"] - recs[0]["ts_epoch"], r["metrics"]["disks"]["/"]["used_pct"])
                     for r in recs], 95.0)
    check("预测可达（ok=True）", fc.get("ok") is True, fc)
    check("预测时间正确（3000s ± 60s）", fc["ok"] and abs(fc["eta_seconds"] - 3000) < 60,
          f"{fc.get('eta_seconds')}s")
    check("斜率换算正确（6 百分点/小时）", abs(fc["slope_per_hour"] - 6.0) < 0.01, fc["slope_per_hour"])
    check("带 R² 与样本信息", fc["r2"] == 1.0 and fc["points"] == 11)
    found = T.evaluate_trends(recs, {"host": "h1"})
    rules = {f["rule"] for f in found}
    check("趋势规则触发 DISK_FILL_FORECAST", "DISK_FILL_FORECAST" in rules, sorted(rules))
    sev = [f["severity"] for f in found if f["rule"] == "DISK_FILL_FORECAST"][0]
    check("0.83 小时后到 95% → critical（<6h）", sev == "critical", sev)

    # ── [3] 不该算的时候坚决不算（本文件最重要的一组）────────
    print("\n[3] 不给预测的三种情形（都要有原因）")
    few = [rec(i * 600, used=50.0 + i) for i in range(2)]
    fc = T.forecast([(r["ts_epoch"] - few[0]["ts_epoch"], r["metrics"]["disks"]["/"]["used_pct"])
                     for r in few], 95.0)
    check("样本不足 → ok=False 且给原因", fc["ok"] is False and "样本不足" in fc["reason"], fc.get("reason"))
    short = [rec(i * 30, used=50.0 + i * 0.1) for i in range(5)]      # 跨度 120s < 600s
    fc = T.forecast([(r["ts_epoch"] - short[0]["ts_epoch"], r["metrics"]["disks"]["/"]["used_pct"])
                     for r in short], 95.0)
    check("跨度过短 → ok=False", fc["ok"] is False and "跨度" in fc["reason"], fc.get("reason"))
    noise = [rec(i * 600, used=50.0 + (0.01 if i % 2 else 0)) for i in range(6)]
    fc = T.forecast([(r["ts_epoch"] - noise[0]["ts_epoch"], r["metrics"]["disks"]["/"]["used_pct"])
                     for r in noise], 95.0)
    check("变化在噪声内 → ok=False", fc["ok"] is False, fc.get("reason"))
    check("三条都不许产出规则", T.evaluate_trends(few, None) == [] and T.evaluate_trends(short, None) == []
          and T.evaluate_trends(noise, None) == [])
    check("explain() 能讲清为什么没给预测", "不给预测" in T.explain(short))

    # ── [4] 分工：已经超阈值的不重复预报 ───────────────────
    print("\n[4] 与现状规则的分工")
    over = [rec(i * 600, used=96.0 + i * 0.1) for i in range(5)]
    rules = {f["rule"] for f in T.evaluate_trends(over, {"disks": {"items": [
        {"mount": "/", "available": True, "used_pct": 96.4}]}})}
    check("已超 95% → 趋势不报（现状规则已报过）", "DISK_FILL_FORECAST" not in rules, sorted(rules))

    # ── [5] 跳变检测 ──────────────────────────────────────
    print("\n[5] 跳变（比斜率更即时）")
    jump = [rec(0, used=40.0), rec(600, used=41.0), rec(1200, used=52.0)]
    rules = {f["rule"] for f in T.evaluate_trends(jump, None)}
    check("两次采样间 +11 点 → DISK_SUDDEN_JUMP", "DISK_SUDDEN_JUMP" in rules, sorted(rules))

    # ── [6] 内存与温度 ────────────────────────────────────
    print("\n[6] 内存下降 / 温度上升")
    mem = [rec(i * 600, used=10.0, mem=40.0 - i * 2) for i in range(8)]   # 40% → 26%
    found = T.evaluate_trends(mem, {"memory": {"available": True, "available_pct": 26.0}})
    check("可用内存持续下降 → MEM_DEGRADING",
          any(f["rule"] == "MEM_DEGRADING" for f in found), [f["rule"] for f in found])
    hot = [rec(i * 600, used=10.0, temp=60.0 + i * 1.5) for i in range(10)]
    found = T.evaluate_trends(hot, {"thermal": {"available": True, "max_celsius": 73.5}})
    check("温度上升 → THERMAL_RISING", any(f["rule"] == "THERMAL_RISING" for f in found),
          [f["rule"] for f in found])

    # ── [7] 历史存储：追加/按主机过滤/坏行容错/无界增长 ──────
    print("\n[7] 历史存储")
    tmp = tempfile.mkdtemp(prefix="trend_")
    try:
        path = os.path.join(tmp, "history.jsonl")
        snap = {"host": "board-1", "collected_at": "2026-09-24 10:00:00",
                "disks": {"available": True, "items": [{"mount": "/", "available": True, "used_pct": 88.0}]},
                "memory": {"available": True, "available_pct": 22.0}, "load": {}, "thermal": {}}
        r = T.append_sample(snap, "warning", path=path)
        check("追加成功且只存指标（不存整份快照）",
              "metrics" in r and "disks" in r["metrics"] and "kernel" not in r, list(r))
        T.append_sample({**snap, "host": "board-2"}, "ok", path=path)
        check("按主机过滤", len(T.load(path, host="board-1")) == 1 and len(T.load(path)) == 2)
        with open(path, "a", encoding="utf-8") as f:
            f.write("{坏行，被截断的\n\n")
        check("坏行/空行跳过而不是整份报废", len(T.load(path)) == 2)
        for i in range(8):
            T.append_sample({**snap, "collected_at": f"t{i}"}, "ok", path=path)
        T.prune(path, "board-1", keep=3)
        left = T.load(path, host="board-1")
        check("按主机裁剪到 keep 条（历史不许无界增长）", len(left) == 3, len(left))
        check("裁剪后保留的是最新的", left[-1]["ts"] == "t7", left[-1]["ts"])
        check("其它主机不受影响", len(T.load(path, host="board-2")) == 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 74)
    print(f"结果：{len(PASS)} 项通过，{len(FAIL)} 项失败（耗时 {time.perf_counter() - t0:.2f}s）")
    if FAIL:
        for f in FAIL:
            print(f"  ✗ {f}")
        sys.exit(1)
    print("趋势是纯数学 + 临时文件 —— 免费、可复现，已进 CI 免费档。")


if __name__ == "__main__":
    main()
