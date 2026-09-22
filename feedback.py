#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""作品 3 · 误报反馈与抑制 —— feedback.py

**为什么要有这个模块**（企业调研里"反馈闭环"那条，原话很难听但很准）：

    误报没人标记、工单随手关 → AI 不会越用越准，只会越用越吵。

一个只会告警、不会"被纠正"的巡检工具，在真实运维里活不过一个月：
值班的人先把告警静音，然后漏掉真故障。所以**必须能被人纠正**。

但"能抑制"和"乱抑制"只差一条线，而跨过这条线的代价是**漏掉真故障**。
所以本模块的每一条设计都在防"顺手压掉真问题"：

═══════════════════════════════════════════════════════════════════
七条不让步的纪律（每条都对应一个"看起来更省事但更坏"的做法）
═══════════════════════════════════════════════════════════════════
**① 抑制不是删除（最重要的那条）。**
   被压住的发现**照旧出现在输出里**，放在「已抑制」区，并写明**被哪条规则压住、为什么、什么时候到期**。
   更省事的做法是"压掉就当没看见" —— 那出事故时你无法回答"当时为什么没报"，
   而这恰恰是事后复盘要问的第一个问题。收敛模块同理：**只归组不删除**。
   不变量：`len(保留) + len(被抑制) == len(输入)`，进测试也进运行时自检。

**② 抑制有严重度天花板。**
   你标记的是"这个挂载点 92% 是正常的"，那就**只压住这条 warning**；
   它涨到 97%（critical）时**必须照样报**。更省事的做法是"按对象压" ——
   于是当初的容量规划变了、盘真满了，你什么都不知道。
   ➡️ 规则记住创建时的严重度，**任何更严重的发现都不受它影响**。

**③ 抑制默认会过期（默认 7 天）。**
   "当初是误报"不等于"永远是误报" —— 容量、业务量、阈值都会变。
   更省事的做法是永久抑制；后果是半年后没人记得为什么这条规则存在，
   而它正在安静地吃掉告警。到期后**不再抑制，并明确说"已过期，需重新确认"**。

**④ 抑制必须有理由、有来源、可撤销。**
   没有 `reason` 的抑制规则就是给自己埋雷；`--revoke` 用**确定性 id**（由匹配键推导），
   所以即使规则库重建过，撤销依然命中同一条。

**⑤ 匹配范围越宽，要求的证据越多。**
   `exact`（host+rule+subject 完全一致）一个人标记就够；
   `prefix`（整个挂载点前缀）和 `rule`（该主机整条规则）**必须 `--wide` 且至少 2 个人标记**。
   更省事的做法是默认给最宽的匹配 —— 那一周之后你的告警全哑了。

**⑥ 抑制要能被审计出"过宽"和"失效"。**
   每条规则记**命中次数**（跨运行累计）：命中多到离谱 → 疑似过宽（该改阈值，不是该压）；
   长期 0 命中 → 疑似失效（可以撤销）。抑制率过高时运行时直接警告。

**⑦ 不许让模型决定抑制。**
   模型擅长措辞，不擅长"这条该不该永远闭嘴"。抑制规则**只能来自人的显式标记**，
   且规则文本是人能读懂、能反驳的一行。模型在链路里只负责说清楚"这条被谁压住了"。

═══════════════════════════════════════════════════════════════════
数据分层（为什么反馈与规则分开存）
═══════════════════════════════════════════════════════════════════
    feedback.jsonl      **只追加**的人为事实：谁在什么时候把哪条发现标成误报、为什么
    ↓ 派生（可随时重建）
    抑制规则            由反馈聚合出来，带确定性 id、过期时间、证据数
    ↓ 运行
    suppression_state.json  命中计数（让"过宽/失效"从数据里看出来）

为什么要派生而不是直接存规则：人可以改主意（撤销/加标记），
**事实只追加、结论可重算** —— 这样审计链永远完整，也不会出现"规则和理由对不上"。
"""
import os
import re
import sys
import json
import time
import hashlib
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_FEEDBACK = os.path.join(HERE, "snapshots", "feedback.jsonl")
DEFAULT_STATE = os.path.join(HERE, "snapshots", "suppression_state.json")

SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}
RANK_SEVERITY = {v: k for k, v in SEVERITY_RANK.items()}

DEFAULT_TTL_HOURS = 168          # 7 天：够覆盖一个发布窗口/维护期，又不至于"永久埋雷"
MAX_TTL_HOURS = 24 * 365
MATCH_KINDS = ("exact", "prefix", "rule")
WIDE_MATCHES = ("prefix", "rule")   # 范围宽的两种，需要 --wide + 更多证据
BROAD_HIT_WARN = 50                 # 单条规则命中超过这个数 → 疑似过宽
SUPPRESS_RATE_WARN = 0.5            # 一次巡检里被压掉的比例超过这个 → 警告


def _now():
    return time.time()


def _iso(ts):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def severity_rank(s):
    return SEVERITY_RANK.get(s, 9)


# ══════════════════════════════════════════════════════════════════
# ① 反馈存储：只追加的人为事实
# ══════════════════════════════════════════════════════════════════
class FeedbackStore:
    """人给的反馈，**只追加**。所有"结论"（抑制规则）都从这些事实派生出来。

    为什么只追加：① 审计链完整（谁在什么时候说了什么，一条都不少）；
    ② 人改主意不需要改写历史（再追一条 revoke 即可）；③ 可重建、可回放。
    """

    def __init__(self, path=DEFAULT_FEEDBACK):
        self.path = path

    def load(self):
        """读全部记录。坏行跳过（**反馈文件坏一行不该让整个工具瘫掉**）。"""
        if not os.path.exists(self.path):
            return []
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
                if isinstance(rec, dict) and rec.get("kind"):
                    out.append(rec)
        return out

    def _append(self, rec):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec

    def add(self, host, rule, subject, severity, reason="", by="human",
            match="exact", hours=DEFAULT_TTL_HOURS, wide=False, action="false_positive"):
        """记一条误报（或其它反馈）。

        **理由必填**：没有理由的抑制规则半年后没人敢碰，等于埋雷。
        **宽匹配必须显式 --wide**：默认只允许最窄的 exact。
        """
        if not (reason or "").strip():
            raise ValueError("反馈必须给理由（reason）—— 没有理由的抑制规则半年后没人敢撤销")
        if match not in MATCH_KINDS:
            raise ValueError(f"match 只能是 {MATCH_KINDS}")
        if match in WIDE_MATCHES and not wide:
            raise ValueError(f"match={match} 影响范围大（{match} 级），必须显式 wide=True 才允许")
        if severity not in SEVERITY_RANK:
            raise ValueError(f"severity 非法：{severity}")
        hours = int(hours)
        if hours <= 0 or hours > MAX_TTL_HOURS:
            raise ValueError(f"hours 必须在 1~{MAX_TTL_HOURS} 之间 —— 抑制不允许永久生效")
        ts = _now()
        fb_id = "FB-" + hashlib.sha1(
            f"{ts:.6f}|{host}|{rule}|{subject}|{match}|{by}".encode("utf-8")).hexdigest()[:8]
        return self._append({
            "kind": "feedback", "id": fb_id, "ts": ts, "iso": _iso(ts),
            "action": action, "by": by, "host": host, "rule": rule, "subject": subject,
            "severity": severity, "match": match, "hours": hours, "reason": reason.strip(),
        })

    def revoke(self, sup_id=None, reason="", by="human", fb_id=None):
        """撤销：追一条撤销记录（不改历史）。`sup_id` 是**确定性**的，重建后依然命中。"""
        if not sup_id and not fb_id:
            raise ValueError("撤销要指定 sup_id（或 fb_id）")
        if not (reason or "").strip():
            raise ValueError("撤销也要给理由（否则半年后同样没人知道为什么撤了）")
        ts = _now()
        return self._append({"kind": "revoke", "ts": ts, "iso": _iso(ts),
                             "sup_id": sup_id, "fb_id": fb_id,
                             "reason": reason.strip(), "by": by})


def match_key(host, rule, subject, match):
    return f"{host}|{rule}|{subject}|{match}"


def sup_id_for(host, rule, subject, match):
    """抑制规则的**确定性 id**：由匹配键推导。

    为什么不用自增序号：规则是从反馈**派生**的，重建一次序号就会变，
    那样"撤销"会撤到别的规则上 —— 这是会把真故障放回来的 bug。
    """
    return "SUP-" + hashlib.sha1(match_key(host, rule, subject, match)
                                 .encode("utf-8")).hexdigest()[:10]


# ══════════════════════════════════════════════════════════════════
# ② 派生抑制规则
# ══════════════════════════════════════════════════════════════════
def build_suppressions(records, now=None, min_marks=None):
    """反馈事实 → 生效的抑制规则 + 被拒绝的（带原因）。

    返回 `(rules, rejected)`；两者都要给人看 —— **被拒绝的规则也要有交代**，
    否则人会以为"我标了它怎么还在报"，其实是标记本身不合法。
    """
    now = now if now is not None else _now()
    revoked = {r.get("sup_id") for r in records if r.get("kind") == "revoke" and r.get("sup_id")}
    groups, rejected = {}, []

    for r in records:
        if r.get("kind") != "feedback" or r.get("action") != "false_positive":
            continue
        host, rule, subject = r.get("host") or "", r.get("rule") or "", r.get("subject") or ""
        match = r.get("match") or "exact"
        if match not in MATCH_KINDS:
            rejected.append({"fb_id": r.get("id"), "why": f"match 非法：{match}"})
            continue
        if not (r.get("reason") or "").strip():
            rejected.append({"fb_id": r.get("id"), "why": "没有理由（无法审计）"})
            continue
        # 宽匹配需要更多证据：exact 一个人标记就够，prefix/rule 至少要 2 个
        need = 1 if match == "exact" else 2
        if min_marks is not None:
            need = max(need, int(min_marks))
        g = groups.setdefault(match_key(host, rule, subject, match), {
            "host": host, "rule": rule, "subject": subject, "match": match,
            "marks": [], "severities": []})
        g["marks"].append(r)
        g["severities"].append(r.get("severity", "warning"))

    rules = []
    for key, g in groups.items():
        sid = sup_id_for(g["host"], g["rule"], g["subject"], g["match"])
        if sid in revoked:
            rejected.append({"sup_id": sid, "why": "已被撤销（revoke）",
                             "host": g["host"], "rule": g["rule"], "subject": g["subject"]})
            continue
        # 证据门槛：exact 一个人标记就够；宽匹配（prefix/rule）至少要 2 个；
        # 调用方还可以用 min_marks 再抬高（比如"生产环境一律要 3 个"）
        need = 1 if g["match"] == "exact" else 2
        if min_marks is not None:
            need = max(need, int(min_marks))
        if len(g["marks"]) < need:
            rejected.append({"sup_id": sid,
                             "why": f"{g['match']} 级匹配需要至少 {need} 个标记，现在 {len(g['marks'])} 个"})
            continue
        first, last = g["marks"][0], g["marks"][-1]
        created = min(m.get("ts", now) for m in g["marks"])
        hours = min(int(m.get("hours") or DEFAULT_TTL_HOURS) for m in g["marks"])
        # 严重度天花板：只压住**创建时不比它更严重**的发现（更严重的照报）
        ceiling = min(g["severities"], key=severity_rank)
        rules.append({
            "sup_id": sid, "host": g["host"], "rule": g["rule"], "subject": g["subject"],
            "match": g["match"], "ceiling": ceiling, "marks": len(g["marks"]),
            "reason": last.get("reason", ""), "by": last.get("by", "human"),
            "created_ts": created, "created": _iso(created),
            "expires_ts": created + hours * 3600, "expires": _iso(created + hours * 3600),
            "hours": hours, "first_reason": first.get("reason", ""),
            "fb_ids": [m.get("id") for m in g["marks"]],
        })
    rules.sort(key=lambda r: (severity_rank(r["ceiling"]), r["rule"], r["subject"]))
    return rules, rejected


def is_suppressed(finding, host, rules, now=None):
    """这条发现被哪条规则压住？返回规则或 None。

    两个必须同时成立：① 匹配键命中；② **严重度不高于天花板**。
    第 ② 条是本模块的安全阀（见文件头纪律②）。
    """
    now = now if now is not None else _now()
    frule = finding.get("rule") or ""
    fsubj = str(finding.get("subject") or "")
    frank = severity_rank(finding.get("severity", "info"))
    for r in rules:
        if r["expires_ts"] <= now:
            continue                                  # 过期 → 不再抑制（纪律③）
        if r["rule"] != frule:
            continue
        if r["host"] and host and r["host"] != host:
            continue
        if r["match"] == "exact":
            if fsubj != str(r["subject"]):
                continue
        elif r["match"] == "prefix":
            if not (fsubj == r["subject"] or fsubj.startswith(str(r["subject"]).rstrip("/") + "/")):
                continue
        # match == "rule"：同主机同规则即可，不看对象
        if frank < severity_rank(r["ceiling"]):
            continue        # ★ 比标记时更严重 → 不受抑制，照样报
        return r
    return None


def apply_suppressions(findings, host, rules, now=None):
    """返回 `(kept, suppressed)`。**谁都没被删掉**，只是分了组。

    不变量：`len(kept) + len(suppressed) == len(findings)`。
    """
    now = now if now is not None else _now()
    kept, suppressed = [], []
    for f in findings or []:
        r = is_suppressed(f, host, rules, now)
        if r is None:
            kept.append(f)
        else:
            suppressed.append(dict(f, suppressed_by=r["sup_id"], suppress_reason=r["reason"],
                                   suppress_expires=r["expires"], suppress_match=r["match"],
                                   suppress_ceiling=r["ceiling"]))
    return kept, suppressed


def check_suppressions(rules, now=None):
    """自检：撤销/过宽/无理由/无过期/被压的严重度。**返回问题列表**（进 CI）。"""
    now = now if now is not None else _now()
    problems = []
    for r in rules:
        if not (r.get("reason") or "").strip():
            problems.append(f"{r['sup_id']} 没有理由（无法审计）")
        if r.get("expires_ts", 0) <= r.get("created_ts", 0):
            problems.append(f"{r['sup_id']} 没有有效期（抑制不允许永久生效）")
        if r.get("match") in WIDE_MATCHES and r.get("marks", 0) < 2:
            problems.append(f"{r['sup_id']} 是 {r['match']} 级宽匹配，却只有 {r.get('marks')} 个标记")
        if r.get("ceiling") == "critical":
            problems.append(f"{r['sup_id']} **天花板是 critical** —— 它会压住最严重的那一档，"
                            "请确认这是有意为之")
    return problems


# ══════════════════════════════════════════════════════════════════
# ③ 命中计数：让"过宽/失效"从数据里看出来
# ══════════════════════════════════════════════════════════════════
class SuppressionState:
    """跨运行累计每条抑制规则命中了几次、最后一次压的是什么。

    为什么必须记：抑制规则最大的风险不是"没用"，是"太有用" ——
    一条能吃掉所有磁盘告警的规则看起来"很干净"，实际是把监控关掉了。
    命中数让这件事**从数据里暴露**（而不是等人想起来去查）。
    """

    def __init__(self, path=DEFAULT_STATE):
        self.path = path

    def load(self):
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}                     # 状态坏了就当没有（不该拖垮主流程）

    def save(self, data):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)
        return data

    def record(self, suppressed, now=None, host="", keep_last=3):
        now = now if now is not None else _now()
        data = self.load()
        for s in suppressed or []:
            sid = s.get("suppressed_by")
            if not sid:
                continue
            e = data.setdefault(sid, {"hits": 0, "first_ts": now, "last_ts": now,
                                      "host": host, "rule": s.get("rule"),
                                      "subject": s.get("subject"), "recent": []})
            e["hits"] = int(e.get("hits", 0)) + 1
            e["last_ts"] = now
            e["last_iso"] = _iso(now)
            recent = e.setdefault("recent", [])
            recent.append({"iso": _iso(now), "subject": str(s.get("subject")),
                           "evidence": s.get("evidence", "")[:120]})
            e["recent"] = recent[-keep_last:]     # 只留最近几条，状态文件不许无界增长
        self.save(data)
        return data

    def stats(self, rules, now=None):
        now = now if now is not None else _now()
        data = self.load()
        out = []
        for r in rules:
            e = data.get(r["sup_id"], {})
            out.append({**{k: r[k] for k in ("sup_id", "rule", "subject", "match", "ceiling",
                                             "reason", "expires", "marks", "created_ts")},
                        "hits": e.get("hits", 0), "last_iso": e.get("last_iso"),
                        "last_ts": e.get("last_ts"), "recent": e.get("recent") or [],
                        "expired": r["expires_ts"] <= now})
        out.sort(key=lambda x: -x["hits"])
        return out


def audit_suppressions(rules, state=None, now=None, broad_hits=BROAD_HIT_WARN):
    """把抑制规则的健康状况说清楚：过宽 / 失效 / 已过期。**给人做决定用的。**"""
    now = now if now is not None else _now()
    stats = (state or SuppressionState()).stats(rules, now=now)
    notes = []
    for s in stats:
        if s["expired"]:
            notes.append(f"⏳ {s['sup_id']} 已过期（{s['expires']}）→ 不再抑制；要续期请重新标记")
        elif s["hits"] >= broad_hits:
            notes.append(f"⚠ {s['sup_id']} 命中 {s['hits']} 次（≥{broad_hits}）→ **疑似过宽**："
                         f"更像是阈值定得太紧，建议改阈值而不是压制（{s['rule']}）")
        elif s["hits"] == 0 and now - (s.get("created_ts") or now) > 7 * 86400:
            # 注意判据是"**创建**超过 7 天且一次都没命中"；
            # 刚建的规则 0 命中是正常的，不能一上来就说它失效（否则提示会被无视）
            notes.append(f"💤 {s['sup_id']} 建立已超过 7 天、0 命中 → 疑似失效，可以考虑撤销")
    return stats, notes


# ══════════════════════════════════════════════════════════════════
# ④ 阈值建议：比"逐个压制"更好的那条路
# ══════════════════════════════════════════════════════════════════
def suggest_thresholds(records, min_marks=3):
    """同一规则、**不同对象**上出现多次误报 → 更可能是阈值太紧，而不是每个对象都要压。

    为什么这条重要：逐个压制是"把一个系统性误报拆成 N 条抑制规则"，
    规则越积越多、没人看得懂；而调一次阈值就解决了。
    这也是企业调研里"阈值调整建议"那条的最低成本实现。
    """
    agg = {}
    for r in records:
        if r.get("kind") != "feedback" or r.get("action") != "false_positive":
            continue
        agg.setdefault((r.get("host") or "", r.get("rule") or ""), set()).add(str(r.get("subject")))
    out = []
    for (host, rule), subjects in agg.items():
        if len(subjects) >= min_marks:
            out.append({"host": host, "rule": rule, "distinct_subjects": len(subjects),
                        "subjects": sorted(subjects),
                        "suggestion": (f"{rule} 在 {len(subjects)} 个不同对象上都被标为误报 → "
                                       f"**先考虑调阈值**（THRESHOLDS / trend 参数），"
                                       f"而不是给每个对象各加一条抑制规则")})
    out.sort(key=lambda x: (-x["distinct_subjects"], x["rule"]))
    return out


# ══════════════════════════════════════════════════════════════════
# ⑤ 渲染
# ══════════════════════════════════════════════════════════════════
def render_suppressed(suppressed, rules, total=None):
    """「已抑制」区：**一条都不许静默消失**，每条都要能回答"为什么没报"。"""
    if not suppressed:
        return ""
    L = [f"【已抑制】{len(suppressed)} 条"
         + (f"（占总发现 {len(suppressed) / total * 100:.0f}%）" if total else "")
         + " —— 抑制**不等于删除**：这些仍在报告里，只是不进退出码"]
    for s in suppressed:
        L.append(f"  · {s['severity']} {s['rule']} @ {s['subject']}：{s.get('evidence', '')}")
        L.append(f"      被 {s['suppressed_by']} 压住（{s['suppress_match']}）｜原因："
                 f"{s['suppress_reason']}｜到期：{s['suppress_expires']}"
                 f"｜天花板：{s['suppress_ceiling']}")
    ids = sorted({s['suppressed_by'] for s in suppressed})
    if total and len(suppressed) / max(total, 1) >= SUPPRESS_RATE_WARN:
        L.append(f"  ⚠ 本次抑制率 {len(suppressed) / total * 100:.0f}%（≥{SUPPRESS_RATE_WARN * 100:.0f}%）"
                 f" —— **先确认这不是把监控关掉了**，再决定要不要收窄规则")
    L.append(f"  相关规则：{', '.join(ids)}｜审计：python feedback.py --explain")
    return "\n".join(L)


def render_rules(rules, rejected=None, state=None, now=None):
    now = now if now is not None else _now()
    L = [f"【抑制规则】生效 {len(rules)} 条"
         + (f"，被拒绝 {len(rejected)} 条" if rejected else "")]
    if not rules:
        L.append("  （还没有抑制规则 —— 误报可以用 --feedback-mark 标记）")
    for r in rules:
        flag = "⏳已过期" if r["expires_ts"] <= now else "✅生效中"
        L.append(f"  {r['sup_id']}｜{flag}｜{r['host']}／{r['rule']}／"
                 f"{r['subject']}（{r['match']}，天花板 {r['ceiling']}）")
        L.append(f"      理由：{r['reason']}｜{r['marks']} 个标记｜到期 {r['expires']}")
    if rejected:
        L.append("  被拒绝的标记（**不会生效，必须让人知道**）：")
        for x in rejected:
            L.append(f"    ✗ {x.get('fb_id') or x.get('sup_id')}：{x['why']}")
    if rules:
        stats, notes = audit_suppressions(rules, state, now=now)
        if notes:
            L.append("  健康检查：")
            for n in notes:
                L.append(f"    {n}")
    return "\n".join(L)


# ══════════════════════════════════════════════════════════════════
# CLI：给人用（标记/撤销/查看/自检）；主链路在 day42_live.py
# ══════════════════════════════════════════════════════════════════
def _main(argv=None):
    ap = argparse.ArgumentParser(description="误报反馈与抑制（可审计、可撤销、默认会过期）")
    ap.add_argument("--mark", action="store_true", help="标记一条误报")
    ap.add_argument("--rule", default=None, help="规则名，如 DISK_USED_HIGH")
    ap.add_argument("--subject", default=None, help="对象，如 /var")
    ap.add_argument("--host", default=None)
    ap.add_argument("--severity", default="warning", choices=sorted(SEVERITY_RANK))
    ap.add_argument("--reason", default=None, help="为什么是误报（**必填**）")
    ap.add_argument("--by", default="human")
    ap.add_argument("--match", default="exact", choices=list(MATCH_KINDS))
    ap.add_argument("--hours", type=int, default=DEFAULT_TTL_HOURS)
    ap.add_argument("--wide", action="store_true", help="允许 prefix/rule 级宽匹配（要更多证据）")
    ap.add_argument("--revoke", default=None, metavar="SUP_ID", help="撤销一条抑制规则")
    ap.add_argument("--list", action="store_true", help="列出生效的抑制规则")
    ap.add_argument("--check", action="store_true", help="自检（进 CI）")
    ap.add_argument("--store", default=DEFAULT_FEEDBACK)
    ap.add_argument("--state", default=DEFAULT_STATE)
    args = ap.parse_args(argv)

    store = FeedbackStore(args.store)
    if args.mark:
        # 不用 argparse 的 error()：它抛 SystemExit，既不好测、也不好被别的脚本复用；
        # 自己打印 + 返回码，语义更清楚，退出码还能直接当门禁用
        missing = [k for k, v in (("--rule", args.rule), ("--subject", args.subject),
                                  ("--reason", args.reason))
                   if v is None or str(v).strip() == ""]
        if missing:
            print(f"拒绝记录：--mark 缺少 {'、'.join(missing)}（**理由必填**，否则半年后没人敢撤销）")
            return 2
        try:
            rec = store.add(args.host or "", args.rule, args.subject, args.severity,
                            reason=args.reason, by=args.by, match=args.match,
                            hours=args.hours, wide=args.wide)
        except ValueError as e:
            print(f"拒绝记录：{e}")
            return 2
        print(f"已记录误报 {rec['id']}：{rec['rule']} @ {rec['subject']}"
              f"（{rec['match']}，{rec['hours']} 小时后过期）")
        rules, rejected = build_suppressions(store.load())
        print(render_rules(rules, rejected, SuppressionState(args.state)))
        sug = suggest_thresholds(store.load())
        if sug:
            print("\n【阈值建议】（比逐个压制更省事的那条路）")
            for s in sug:
                print(f"  · {s['suggestion']}")
        return 0
    if args.revoke:
        try:
            rec = store.revoke(sup_id=args.revoke, reason=args.reason or "", by=args.by)
        except ValueError as e:
            print(f"拒绝撤销：{e}")
            return 2
        print(f"已撤销 {rec['sup_id']}（记录于 {rec['iso']}）")
        return 0
    if args.check:
        rules, rejected = build_suppressions(store.load())
        problems = check_suppressions(rules)
        if problems:
            print("抑制规则自检**未通过**：")
            for p in problems:
                print(f"  ✗ {p}")
            return 2
        print(f"抑制规则自检通过：{len(rules)} 条生效，全部有理由、有有效期、范围合法")
        return 0
    rules, rejected = build_suppressions(store.load())
    print(render_rules(rules, rejected, SuppressionState(args.state)))
    sug = suggest_thresholds(store.load())
    if sug:
        print("\n【阈值建议】")
        for s in sug:
            print(f"  · {s['suggestion']}")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(_main())
