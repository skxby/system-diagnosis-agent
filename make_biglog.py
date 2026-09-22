#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成"真实规模"的日志 fixture —— make_biglog.py

为什么要造这个：现在诊断的是 30 行的 `sample_app.log`，**上下文工程完全没用武之地**
（一行塞得下，怎么装都是对的）。真实运维场景里日志是几万行、几 MB，
"把命中行全塞进 prompt"这条朴素路线会当场炸掉 —— 上下文工程就是在解决这件事。

本生成器造一个**三天窗口、8000 行、约 600KB** 的应用日志，里面埋着：

    真故障（要能挖出来）
      · DB 10.20.31.7:5432 连接被拒（03:12 起，周期性复现）
      · 根分区空间持续下降（41% → 1.2%）→ "No space left on device"
      · 日志 fallback 缓冲溢出，丢了 2048 行（**证据缺失这件事本身也要被报告出来**）

    噪声（要能忽略）
      · 每 30 秒一条心跳 INFO（占绝大多数行）
      · 周期性 404 / TLS 证书告警（与本次故障无关的域）
      · GC 停顿、cron、发布记录

    陷阱（不许编造）
      · 全文**没有** OutOfMemory / 段错误 —— 评测里断言"报告不许出现 OOM 断言"，
        谁在报告里写"内存溢出导致"就是幻觉

输出确定性（固定随机种子），所以 fixture 可复现、git diff 友好。

跑法：
    python make_biglog.py                    # 写 logs/big/app_3day.log
    python make_biglog.py --lines 20000      # 更大
"""
import os
import sys
import random
import argparse
from datetime import datetime, timedelta

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "logs", "big")
HOSTS = ["10.20.31.7:5432", "10.20.31.8:5432"]
APPS = ["order-api", "user-api", "report-worker"]

# 故障时间线（相对窗口起点的分钟数）
T_DB_FIRST = 3 * 60 + 12          # 03:12 首次连接被拒
T_DISK_START = 5 * 60             # 05:00 开始掉空间
T_LOST = 9 * 60 + 41              # 09:41 fallback 溢出丢 2048 行
T_PAGE = 10 * 60 + 5              # 10:05 呼叫值班


def build(lines):
    rnd = random.Random(20260924)          # 固定种子 → 可复现
    start = datetime(2026, 9, 24, 0, 0, 0)
    out = []
    heartbeat_every = max(1, lines // 900)  # 心跳占大头，保证"噪声淹没信号"

    for i in range(lines):
        # 时间：整个窗口线性铺开（3 天）
        ts = start + timedelta(seconds=int(i * 3 * 24 * 3600 / lines))
        minute = int((ts - start).total_seconds() // 60)
        t = ts.strftime("%Y-%m-%d %H:%M:%S")

        # ── 心跳/常规噪声（多数行）──────────────────────────
        if i % heartbeat_every == 0:
            out.append(f"{t} INFO  [health] heartbeat ok app={rnd.choice(APPS)} "
                       f"latency={rnd.randint(2, 40)}ms conn_pool={rnd.randint(3, 10)}")
            continue
        roll = rnd.random()
        if roll < 0.10:
            out.append(f"{t} INFO  [http] GET /api/v1/{rnd.choice(['orders', 'users', 'reports'])} "
                       f"200 {rnd.randint(3, 120)}ms")
        elif roll < 0.14:
            out.append(f"{t} WARN  [http] GET /static/{rnd.choice(['a', 'b', 'c'])}.png 404 "
                       f"ua=scanner/{rnd.randint(1, 9)}")
        elif roll < 0.16:
            out.append(f"{t} WARN  [tls] certificate for cdn.example-{rnd.randint(1, 3)}.net "
                       f"expires in {rnd.randint(3, 20)} days")
        elif roll < 0.18:
            out.append(f"{t} INFO  [gc] young collection pause={rnd.randint(5, 60)}ms "
                       f"heap={rnd.randint(200, 900)}MB")
        elif roll < 0.19:
            out.append(f"{t} INFO  [cron] daily job {rnd.choice(['rotate', 'cleanup', 'report'])} "
                       f"finished rc=0")
        elif roll < 0.20:
            out.append(f"{t} INFO  [deploy] rollout step {rnd.randint(1, 5)}/5 app={rnd.choice(APPS)}")

        # ── 真故障 1：DB 连接被拒（周期性复现）──────────────
        if minute >= T_DB_FIRST and rnd.random() < 0.05:
            host = rnd.choice(HOSTS)
            out.append(f"{t} WARN  [db] connection attempt 1/3 timeout after 3000ms host={host}")
            out.append(f"{t} ERROR [db] connection attempt 3/3 failed: connection refused host={host}")
            if rnd.random() < 0.3:
                out.append(f"{t} ERROR [main] failed to init database pool, entering degraded mode")

        # ── 真故障 2：磁盘空间持续下降 ───────────────────────
        if minute >= T_DISK_START:
            pct = max(1.2, 41.0 - (minute - T_DISK_START) / 60 * 3.2)
            if i % max(1, lines // 240) == 0:
                level = "WARN " if pct < 10 else "INFO "
                out.append(f"{t} {level} [disk] free space on / : {pct:.1f}%")
            if pct < 6 and rnd.random() < 0.03:
                out.append(f"{t} ERROR [log] failed to append to /var/log/app/access.log: "
                           f"No space left on device")
            if pct < 4 and rnd.random() < 0.02:
                out.append(f"{t} ERROR [cache] failed to persist session cache: "
                           f"No space left on device")

        # ── 关键：证据丢失这件事本身 ─────────────────────────
        if minute == T_LOST:
            out.append(f"{t} ERROR [log] fallback buffer overflow, dropped 2048 lines")
            out.append(f"{t} WARN  [log] log loss detected, timeline gap 09:41-10:02")
        if minute == T_PAGE:
            out.append(f"{t} INFO  [ops] paging on-call engineer, incident opened")

        # ── 次生现象（500/503）───────────────────────────────
        if minute >= T_DB_FIRST and rnd.random() < 0.01:
            out.append(f"{t} ERROR [http] POST /api/v1/order 500 {rnd.randint(900, 2500)}ms")
        if minute >= T_DISK_START and rnd.random() < 0.005:
            out.append(f"{t} ERROR [main] service unhealthy, /health returns 503")

    return out[:lines], start, ts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lines", type=int, default=8000)
    ap.add_argument("--out", default=os.path.join(OUT_DIR, "app_3day.log"))
    args = ap.parse_args()

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    rows, start, end = build(args.lines)
    with open(args.out, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(rows) + "\n")

    size_kb = os.path.getsize(args.out) / 1024
    errs = sum(1 for r in rows if " ERROR " in r)
    print(f"已生成：{args.out}")
    print(f"  行数 {len(rows)}｜体积 {size_kb:.0f} KB｜窗口 {start:%Y-%m-%d %H:%M} ~ {end:%m-%d %H:%M}"
          f"｜ERROR 行 {errs}")
    print(f"  埋的真故障：DB connection refused（03:12 起）、磁盘 41%→1.2%、丢 2048 行；"
          f"全文无 OOM（防幻觉断言用）")


if __name__ == "__main__":
    main()
