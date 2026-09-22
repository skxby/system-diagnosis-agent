#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""作品 3 · 趋势与预测 —— trend.py

**为什么这是运维工具最有价值的一半**：
"磁盘 91%，warning" 这种告警，值班的人看一眼就放到明天了 ——
"**按当前速度，/ 还有 4.2 小时到 95%**" 这句话才逼人现在动手。
所以趋势不是花活，它是把"当前值告警"升级成"**可行动的时间预算**"。

═══════════════════════════════════════════════════════════════════
四条设计纪律（都是这一路踩出来的同类教训）
═══════════════════════════════════════════════════════════════════

**① 样本不够就不给预测 —— 宁可说"不知道"**
    要 n≥3 个点、时间跨度 ≥10 分钟、且窗口内变化 ≥1 个百分点，
    才输出预测。否则明确回"样本不足"，绝不拿两个点连条线就当趋势。
    （和"缺数据不许报假健康""读不到 ≠ 没有异常"是同一条纪律：
     **运维工具最贵的错是让人信了一个编出来的数**。）

**② 预测必须带可信度**
    输出斜率、R²、窗口区间与样本数。值班的人要能判断"这条预测靠不靠得住"，
    而不是只看到一个"4.2 小时"。

**③ 历史只存"要算趋势的指标"**
    不存整份快照：省空间、免去把主机细节写进历史（很多是敏感信息）、读起来快。

**④ 已经超阈值的不重复预报**
    磁盘已经 96% 了，`DISK_USED_HIGH` 已经报了 —— 趋势规则**不再**报"预计 0 小时后到 95%"。
    重复告警是让人关掉告警的第一步。
"""
import os
import json
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_HISTORY = os.path.join(HERE, "snapshots", "history.jsonl")
KEEP_PER_HOST = 500                  # 每台主机保留多少条（无界增长的文件早晚会变成新问题）

# 趋势规则的阈值（与 collect.THRESHOLDS 分开：一个是"现状"，一个是"速度"）
TREND_THRESHOLDS = {
    "min_points": 3,
    "min_span_seconds": 600,          # 10 分钟
    "min_change_pct": 1.0,            # 窗口内至少变化 1 个百分点，否则算噪声
    "disk_target_pct": 95.0,
    "inode_target_pct": 95.0,
    "mem_target_pct": 5.0,            # 可用内存掉到这个数
    "thermal_target_c": 85.0,
    "eta_critical_hours": 6.0,
    "eta_warning_hours": 24.0,
    "jump_pct": 5.0,                 # 相邻两次采样之间跳变
}


# ══════════════════════════════════════════════════════════════════
# 历史存储（JSONL，按主机）
# ══════════════════════════════════════════════════════════════════
def extract_metrics(snap):
    """从快照里抽出**要算趋势的指标**（紧凑字典 —— 见纪律③）"""
    m = {"disks": {}, "mem_available_pct": None, "load1_per_cpu": None, "max_celsius": None}
    for d in (snap.get("disks") or {}).get("items", []):
        if d.get("available"):
            m["disks"][d["mount"]] = {"used_pct": d.get("used_pct"),
                                      "inode_used_pct": d.get("inode_used_pct")}
    if (mem := snap.get("memory") or {}).get("available"):
        m["mem_available_pct"] = mem.get("available_pct")
    if (ld := snap.get("load") or {}).get("available"):
        m["load1_per_cpu"] = ld.get("load1_per_cpu")
    if (th := snap.get("thermal") or {}).get("available"):
        m["max_celsius"] = th.get("max_celsius")
    return m


def append_sample(snap, worst_severity="ok", path=DEFAULT_HISTORY):
    """把本次体检的指标追加进历史。返回写入的记录。"""
    rec = {"ts": snap.get("collected_at") or time.strftime("%Y-%m-%d %H:%M:%S"),
           "ts_epoch": time.time(), "host": snap.get("host") or "?",
           "worst": worst_severity, "metrics": extract_metrics(snap)}
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    prune(path, rec["host"])
    return rec


def prune(path, host, keep=KEEP_PER_HOST):
    """每台主机只留最近 keep 条 —— **历史文件无界增长本身就是个运维问题**（磁盘被自己的监控吃满）"""
    recs = load(path)
    if len([r for r in recs if r.get("host") == host]) <= keep:
        return
    kept, seen = [], {}
    for r in reversed(recs):
        h = r.get("host")
        seen[h] = seen.get(h, 0) + 1
        if seen[h] <= keep:
            kept.append(r)
    with open(path, "w", encoding="utf-8") as f:
        for r in reversed(kept):
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def load(path=DEFAULT_HISTORY, host=None, last_n=None):
    """读历史。**坏行跳过而不是整份报废**（文件被截断/手改过都不该让工具崩）。"""
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue                      # 坏行跳过，不让它毁掉整份历史
            if host and rec.get("host") != host:
                continue
            out.append(rec)
    return out[-last_n:] if last_n else out


# ══════════════════════════════════════════════════════════════════
# 线性拟合与预测
# ══════════════════════════════════════════════════════════════════
def linear_fit(points):
    """最小二乘拟合 [(t_seconds, y)] → (slope, intercept, r2)。

    r2 不是装饰：**它决定这条预测能不能拿出来给人看**。
    """
    n = len(points)
    if n < 2:
        return None
    mt = sum(p[0] for p in points) / n
    my = sum(p[1] for p in points) / n
    sxx = sum((p[0] - mt) ** 2 for p in points)
    if sxx == 0:
        return None
    sxy = sum((p[0] - mt) * (p[1] - my) for p in points)
    slope = sxy / sxx
    intercept = my - slope * mt
    ss_tot = sum((p[1] - my) ** 2 for p in points)
    ss_res = sum((p[1] - (slope * p[0] + intercept)) ** 2 for p in points)
    r2 = 1.0 if ss_tot == 0 else max(0.0, 1 - ss_res / ss_tot)
    return slope, intercept, r2


def forecast(points, target, th=None):
    """按线性趋势预测"何时到达 target"。

    返回 dict：{ok, reason?, eta_seconds?, slope_per_hour?, r2?, span_seconds?, points?}
    **不满足条件时 ok=False 且给出 reason** —— 调用方必须把这个原因写进报告，
    不能把"样本不足"吞掉当成"没问题"。
    """
    th = {**TREND_THRESHOLDS, **(th or {})}
    pts = [(t, y) for t, y in points if y is not None]
    if len(pts) < th["min_points"]:
        return {"ok": False, "reason": f"样本不足（{len(pts)} < {th['min_points']} 个点）"}
    span = pts[-1][0] - pts[0][0]
    if span < th["min_span_seconds"]:
        return {"ok": False, "reason": f"时间跨度过短（{span / 60:.1f} 分钟 < "
                                       f"{th['min_span_seconds'] / 60:.0f} 分钟）"}
    fit = linear_fit(pts)
    if not fit:
        return {"ok": False, "reason": "时间戳无变化，拟合不出斜率"}
    slope, intercept, r2 = fit
    change = slope * span
    if abs(change) < th["min_change_pct"]:
        return {"ok": False, "reason": f"窗口内只变化 {change:+.2f}%，在噪声范围内"}
    if slope == 0:
        return {"ok": False, "reason": "斜率为 0"}
    eta = (target - intercept) / slope - pts[-1][0]      # 相对最后一个采样点
    info = {"ok": True, "eta_seconds": eta, "slope_per_hour": slope * 3600,
            "r2": round(r2, 3), "span_seconds": span, "points": len(pts),
            "change_in_window": round(change, 2)}
    if eta < 0:
        info["ok"] = False
        info["reason"] = f"趋势方向远离阈值（斜率 {slope * 3600:+.2f}/小时）"
    return info


def _series(recs, getter):
    """从历史记录里抽一条时间序列。以**第一条记录的时间**为 t=0（避免大 epoch 造成数值误差）"""
    out = []
    if not recs:
        return out
    t0 = recs[0].get("ts_epoch") or 0
    for r in recs:
        t = r.get("ts_epoch")
        if t is None:
            continue
        out.append((t - t0, getter(r)))
    return out


# ══════════════════════════════════════════════════════════════════
# 趋势规则：产出与 collect.evaluate() 同构的发现
# ══════════════════════════════════════════════════════════════════
def evaluate_trends(recs, current=None, th=None):
    """历史序列 → 趋势发现（severity/subject/evidence/suggestion 与现有规则一致，可直接合并）。

    `current` 传本次的快照指标：用来**避免对已经超阈值的项再报一次预测**（纪律④）。
    """
    th = {**TREND_THRESHOLDS, **(th or {})}
    found = []
    if len(recs) < 2:
        return found

    def add(rule, severity, subject, evidence, suggestion):
        found.append({"rule": rule, "severity": severity, "subject": subject,
                      "evidence": evidence, "suggestion": suggestion})

    def eta_text(sec):
        if sec < 3600:
            return f"{sec / 60:.0f} 分钟"
        if sec < 86400:
            return f"{sec / 3600:.1f} 小时"
        return f"{sec / 86400:.1f} 天"

    def severity_by_eta(sec):
        if sec <= th["eta_critical_hours"] * 3600:
            return "critical"
        if sec <= th["eta_warning_hours"] * 3600:
            return "warning"
        return None                      # 还早，不报（避免"狼来了"）

    cur_metrics = extract_metrics(current) if current else {}

    # ── 磁盘与 inode 的填满预测 ──────────────────────────
    mounts = {m for r in recs for m in (r.get("metrics", {}).get("disks") or {})}
    for mount in sorted(mounts):
        cur_d = (cur_metrics.get("disks") or {}).get(mount, {})
        for key, rule, target_key in (("used_pct", "DISK_FILL_FORECAST", "disk_target_pct"),
                                      ("inode_used_pct", "INODE_FILL_FORECAST", "inode_target_pct")):
            series = _series(recs, lambda r, m=mount, k=key: ((r.get("metrics", {}).get("disks") or {})
                                                              .get(m, {}) or {}).get(k))
            latest = [y for _, y in series if y is not None]
            if not latest:
                continue
            target = th[target_key]
            if latest[-1] >= target:
                continue                 # 纪律④：已经超了 → 现状规则已经报过，别再预报
            fc = forecast(series, target, th)
            if not fc.get("ok"):
                continue                 # 样本不足/噪声大 → 不出预测（原因在 --trend 详情里能看）
            sev = severity_by_eta(fc["eta_seconds"])
            if not sev:
                continue
            add(rule, sev, f"{mount}",
                f"按当前速度（{fc['slope_per_hour']:+.2f} 百分点/小时，R²={fc['r2']}，"
                f"{fc['points']} 个点/{fc['span_seconds'] / 60:.0f} 分钟）预计 "
                f"**{eta_text(fc['eta_seconds'])}** 后达到 {target:.0f}%（现值 {latest[-1]:.1f}%）",
                "在到达前处理：清理/扩容/轮转；先看是不是某个文件在快速增长")

    # ── 可用内存持续下降 ─────────────────────────────────
    if cur_metrics.get("mem_available_pct") is not None:
        series = _series(recs, lambda r: r.get("metrics", {}).get("mem_available_pct"))
        latest = [y for _, y in series if y is not None]
        if latest and latest[-1] > th["mem_target_pct"]:
            fc = forecast(series, th["mem_target_pct"], {**th, "min_change_pct": 0.5})
            if fc.get("ok"):
                sev = severity_by_eta(fc["eta_seconds"])
                if sev:
                    add("MEM_DEGRADING", sev, "available memory",
                        f"可用内存 {latest[-1]:.1f}%，按当前速度（{fc['slope_per_hour']:+.2f} 百分点/小时，"
                        f"R²={fc['r2']}）预计 {eta_text(fc['eta_seconds'])} 后降到 {th['mem_target_pct']:.0f}%",
                        "找增长中的进程（可能是泄漏）；准备扩容或限制")

    # ── 温度上升 ─────────────────────────────────────────
    if cur_metrics.get("max_celsius") is not None:
        series = _series(recs, lambda r: r.get("metrics", {}).get("max_celsius"))
        latest = [y for _, y in series if y is not None]
        if latest and latest[-1] < th["thermal_target_c"]:
            fc = forecast(series, th["thermal_target_c"], {**th, "min_change_pct": 1.0})
            if fc.get("ok"):
                sev = severity_by_eta(fc["eta_seconds"])
                if sev:
                    add("THERMAL_RISING", sev, "SoC 温度",
                        f"当前 {latest[-1]:.1f}°C，按趋势（{fc['slope_per_hour']:+.2f}°C/小时，R²={fc['r2']}）"
                        f"预计 {eta_text(fc['eta_seconds'])} 后到 {th['thermal_target_c']:.0f}°C",
                        "查散热/风道/降频；板子上过热会先降频（表现为变慢）再重启")

    # ── 跳变检测（比斜率更即时：日志暴涨、批量写入）───────
    if len(recs) >= 2:
        prev, cur = recs[-2], recs[-1]
        pd_, cd = prev.get("metrics", {}).get("disks") or {}, cur.get("metrics", {}).get("disks") or {}
        for mount in sorted(set(pd_) & set(cd)):
            a, b = (pd_[mount] or {}).get("used_pct"), (cd[mount] or {}).get("used_pct")
            if a is not None and b is not None and (b - a) >= th["jump_pct"]:
                add("DISK_SUDDEN_JUMP", "warning", mount,
                    f"两次采样之间从 {a:.1f}% 跳到 {b:.1f}%（+{b - a:.1f} 个百分点）—— "
                    f"不是缓慢增长，是**有东西在猛写**",
                    "立刻查大文件增长（日志暴涨/核心转储/临时文件）；比趋势预测更紧急")

    order = {"critical": 0, "warning": 1, "info": 2}
    found.sort(key=lambda x: order.get(x["severity"], 9))
    return found


def explain(recs, th=None):
    """把"为什么没给预测"讲清楚 —— 不给预测时也要有交代（可解释性）。"""
    th = {**TREND_THRESHOLDS, **(th or {})}
    lines = [f"历史样本：{len(recs)} 条" +
             (f"（{recs[0].get('ts')} ~ {recs[-1].get('ts')}）" if recs else "")]
    if not recs:
        return "\n".join(lines + ["还没有历史 —— 这次体检会作为第一条记录"])
    for mount in sorted({m for r in recs for m in (r.get("metrics", {}).get("disks") or {})}):
        series = _series(recs, lambda r, m=mount: ((r.get("metrics", {}).get("disks") or {})
                                                   .get(m, {}) or {}).get("used_pct"))
        fc = forecast(series, th["disk_target_pct"], th)
        lines.append(f"  {mount} 磁盘趋势：{'可预测' if fc.get('ok') else '不给预测（' + fc.get('reason', '') + '）'}")
    return "\n".join(lines)
