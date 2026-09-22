#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""误报反馈与抑制的离线测试 —— test_feedback.py（免费、秒级、不要 key）

**为什么这个模块必须有测试**：它的 bug 形态是"**把真故障安静地放回去了**"。
没有报错、没有异常、输出看着更干净 —— 直到出事才发现监控早就哑了。
所以这里的断言集中在五件事：

  ① **抑制不是删除**：`len(保留) + len(被抑制) == len(输入)`，谁都不能静默消失
  ② **严重度天花板**：标了 warning 的误报，**不能**把同一对象的 critical 一起压掉
  ③ **范围与证据成比例**：exact 一个标记够；prefix/rule 必须显式 --wide 且至少 2 个标记
  ④ **会过期、可撤销、必有理由**：三条缺一条就是给自己埋雷
  ⑤ **过宽/失效要能从数据里看出来**：命中数进状态文件，健康检查据此报警

跑法：.venv\\Scripts\\python.exe test_feedback.py
"""
import os
import sys
import json
import time
import shutil
import tempfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import feedback as F

HERE = os.path.dirname(os.path.abspath(__file__))
PASS, FAIL = [], []
T = 1700000000.0        # 固定"现在"，让过期判定可复现


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}{('  —— ' + str(detail)) if detail else ''}")


def fnd(rule="DISK_USED_HIGH", severity="warning", subject="/var", evidence="已用 92%"):
    return {"rule": rule, "severity": severity, "subject": subject,
            "evidence": evidence, "suggestion": "do"}


def store_of(tmp):
    return F.FeedbackStore(os.path.join(tmp, "feedback.jsonl"))


def main():
    t0 = time.perf_counter()
    tmp = tempfile.mkdtemp(prefix="fbtest_")
    try:
        # ══════════════════════════════════════════════════════════════
        print("[1] 反馈存储：只追加、理由必填、宽匹配要显式")
        # ══════════════════════════════════════════════════════════════
        st = store_of(tmp)
        r1 = st.add("board-1", "DISK_USED_HIGH", "/var", "warning", reason="这个挂载点专门留 8% 给缓存")
        check("能记下一条反馈并给确定性 id", r1["id"].startswith("FB-") and len(r1["id"]) == 11, r1["id"])
        check("记录里带人与时间（可审计）", r1["by"] == "human" and r1["iso"])
        check("文件不存在的 store 读出来是空表（不崩）", F.FeedbackStore(
            os.path.join(tmp, "none.jsonl")).load() == [])
        try:
            st.add("h", "R", "/s", "warning", reason="   ")
            check("没有理由的反馈必须被拒绝", False, "居然记下来了")
        except ValueError as e:
            check("没有理由的反馈必须被拒绝", "理由" in str(e), str(e)[:40])
        try:
            st.add("h", "R", "/s", "warning", reason="r", match="rule")
            check("rule 级宽匹配不加 --wide 必须被拒绝", False, "居然通过了")
        except ValueError as e:
            check("rule 级宽匹配不加 --wide 必须被拒绝", "wide" in str(e), str(e)[:40])
        st.add("h", "R", "/s", "warning", reason="r", match="rule", wide=True)
        check("显式 wide 后允许宽匹配", True)
        for bad in (0, -1, F.MAX_TTL_HOURS + 1):
            try:
                st.add("h", "R", "/s2", "warning", reason="r", hours=bad)
                check(f"hours={bad} 必须被拒绝（不允许永久抑制）", False)
            except ValueError:
                pass
        check("hours 边界被挡住（不许永久抑制）", True)
        try:
            st.add("h", "R", "/s", "fatal", reason="r")
            check("非法 severity 被拒绝", False)
        except ValueError:
            check("非法 severity 被拒绝", True)

        # 坏行不能让整个工具瘫掉
        rp = os.path.join(tmp, "feedback.jsonl")
        with open(rp, "a", encoding="utf-8") as f:
            f.write("{坏行 not json\n")
        check("坏行被跳过而不是抛异常", len(st.load()) >= 2, len(st.load()))

        # ══════════════════════════════════════════════════════════════
        print("[2] 派生规则：范围越宽，要求的证据越多")
        # ══════════════════════════════════════════════════════════════
        s2 = F.FeedbackStore(os.path.join(tmp, "f2.jsonl"))
        s2.add("b1", "THERMAL_HIGH", "soc-thermal", "warning", reason="板子满载就这温度")
        rules, rejected = F.build_suppressions(s2.load(), now=T)
        check("exact：1 个标记就生效", len(rules) == 1 and rules[0]["marks"] == 1, rules)
        check("规则 id 是确定性的（可用来撤销）",
              rules[0]["sup_id"] == F.sup_id_for("b1", "THERMAL_HIGH", "soc-thermal", "exact"))
        check("重建两次得到同一个 id（撤销不会撤错）",
              F.build_suppressions(s2.load(), now=T)[0][0]["sup_id"] == rules[0]["sup_id"])

        s3 = F.FeedbackStore(os.path.join(tmp, "f3.jsonl"))
        s3.add("b1", "TCP_TIME_WAIT_HIGH", "net/tcp", "warning", reason="r1",
               match="prefix", wide=True)
        rules3, rej3 = F.build_suppressions(s3.load(), now=T)
        check("prefix：只有 1 个标记 → 不生效", rules3 == [], rules3)
        check("不生效的规则要给出原因（不能沉默）",
              rej3 and "至少 2" in rej3[0]["why"], rej3)
        s3.add("b1", "TCP_TIME_WAIT_HIGH", "net/tcp", "warning", reason="r2",
               match="prefix", wide=True)
        rules3, _ = F.build_suppressions(s3.load(), now=T)
        check("prefix：够 2 个标记 → 生效", len(rules3) == 1 and rules3[0]["marks"] == 2)

        rules_m, _ = F.build_suppressions(s2.load(), now=T, min_marks=3)
        check("调用方可以抬高证据门槛（生产环境要 3 个）", rules_m == [], rules_m)

        # ══════════════════════════════════════════════════════════════
        print("[3] 严重度天花板：不许把更严重的一起压掉")
        # ══════════════════════════════════════════════════════════════
        s4 = F.FeedbackStore(os.path.join(tmp, "f4.jsonl"))
        s4.add("b1", "DISK_USED_HIGH", "/var", "warning", reason="留了缓存空间")
        r4, _ = F.build_suppressions(s4.load(), now=T)
        check("天花板记录为创建时的严重度", r4[0]["ceiling"] == "warning", r4[0]["ceiling"])
        kept, sup = F.apply_suppressions(
            [fnd(severity="warning", evidence="92%"), fnd(severity="critical", evidence="97%")],
            "b1", r4, now=T)
        check("★ warning 被抑制", len(sup) == 1 and sup[0]["severity"] == "warning")
        check("★★ critical 照旧报（天花板挡住）", len(kept) == 1 and kept[0]["severity"] == "critical")
        check("不变量：保留 + 抑制 == 输入", len(kept) + len(sup) == 2)

        s5 = F.FeedbackStore(os.path.join(tmp, "f5.jsonl"))
        s5.add("b1", "X_RULE", "/s", "critical", reason="这条规则本身没意义")
        r5, _ = F.build_suppressions(s5.load(), now=T)
        # 注意：match 是 exact，所以只对同一个对象 /s 生效（我第一版把这条写成了 /s2，
        # 期望"也能压住" —— 那是**判据写错了**，不是代码错了。教训见笔记：
        # "判据 bug 会冤枉被测系统"，写期望时先问自己"按语义它应该被压吗"）
        kept5, sup5 = F.apply_suppressions([fnd("X_RULE", "critical", "/s"),
                                            fnd("X_RULE", "warning", "/s"),
                                            fnd("X_RULE", "warning", "/s2")], "b1", r5, now=T)
        check("天花板=critical 时能压住同对象上更轻的",
              len(sup5) == 2 and len(kept5) == 1, (len(sup5), len(kept5)))
        check("但 exact 仍然不越界到别的对象（/s2 照旧报）",
              [k["subject"] for k in kept5] == ["/s2"])
        check("但自检会**点名**它（压 critical 要有意为之）",
              any("天花板是 critical" in p for p in F.check_suppressions(r5)),
              F.check_suppressions(r5))

        # ══════════════════════════════════════════════════════════════
        print("[4] 匹配范围：exact / prefix / rule / host 隔离")
        # ══════════════════════════════════════════════════════════════
        se = F.FeedbackStore(os.path.join(tmp, "fe.jsonl"))
        se.add("b1", "BIG_FILE", "/var/log/app.log", "warning", reason="这个文件就是要留")
        re_, _ = F.build_suppressions(se.load(), now=T)
        keep, su = F.apply_suppressions([fnd("BIG_FILE", "warning", "/var/log/app.log"),
                                         fnd("BIG_FILE", "warning", "/var/log/other.log")],
                                        "b1", re_, now=T)
        check("exact 只压同一个对象", len(su) == 1 and len(keep) == 1, (len(keep), len(su)))

        sp = F.FeedbackStore(os.path.join(tmp, "fp.jsonl"))
        for i in (1, 2):
            sp.add("b1", "DISK_USED_HIGH", "/data", "warning", reason=f"r{i}",
                   match="prefix", wide=True)
        rp2, _ = F.build_suppressions(sp.load(), now=T)
        keep, su = F.apply_suppressions([fnd(subject="/data/a"), fnd(subject="/data/b/c"),
                                         fnd(subject="/datax")], "b1", rp2, now=T)
        check("prefix 压住前缀下的对象", {s["subject"] for s in su} == {"/data/a", "/data/b/c"},
              [s["subject"] for s in su])
        check("prefix **不会**误伤同前缀字面量（/datax 不算 /data/ 下）",
              [k["subject"] for k in keep] == ["/datax"])

        sr = F.FeedbackStore(os.path.join(tmp, "fr.jsonl"))
        for i in (1, 2):
            sr.add("b1", "FD_USAGE_HIGH", "file descriptors", "warning", reason=f"r{i}",
                   match="rule", wide=True)
        rr, _ = F.build_suppressions(sr.load(), now=T)
        keep, su = F.apply_suppressions([fnd("FD_USAGE_HIGH", "warning", "/x"),
                                         fnd("FD_USAGE_HIGH", "warning", "/y")], "b1", rr, now=T)
        check("rule 级压住该主机上同规则的所有对象", len(su) == 2 and not keep)

        keep, su = F.apply_suppressions([fnd("FD_USAGE_HIGH", "warning", "/x")],
                                        "OTHER-HOST", rr, now=T)
        check("★ 换一台机器就不生效（host 隔离）", not su and len(keep) == 1)

        # ══════════════════════════════════════════════════════════════
        print("[5] 会过期：当初是误报 ≠ 永远是误报")
        # ══════════════════════════════════════════════════════════════
        sx = F.FeedbackStore(os.path.join(tmp, "fx.jsonl"))
        sx.add("b1", "LOAD_HIGH", "load", "warning", reason="发布窗口内正常", hours=24)
        rx, _ = F.build_suppressions(sx.load(), now=T)
        created = rx[0]["created_ts"]
        check("过期时间 = 创建时间 + hours", rx[0]["expires_ts"] == created + 24 * 3600)
        _, su_now = F.apply_suppressions([fnd("LOAD_HIGH", "warning", "load")], "b1", rx, now=created + 60)
        check("未到期时正常抑制", len(su_now) == 1)
        keep_late, su_late = F.apply_suppressions([fnd("LOAD_HIGH", "warning", "load")], "b1",
                                                 rx, now=created + 25 * 3600)
        check("★ 到期后不再抑制（发现照旧报出来）", not su_late and len(keep_late) == 1)
        stats, notes = F.audit_suppressions(rx, state=F.SuppressionState(
            os.path.join(tmp, "st_x.json")), now=created + 25 * 3600)
        check("健康检查会说出「已过期，需重新确认」",
              any("已过期" in n and "重新标记" in n for n in notes), notes)

        # ══════════════════════════════════════════════════════════════
        print("[6] 可撤销：撤销必须有理由，且撤销后不生效")
        # ══════════════════════════════════════════════════════════════
        sv = F.FeedbackStore(os.path.join(tmp, "fv.jsonl"))
        sv.add("b1", "SWAP_PRESSURE", "swap", "warning", reason="这台机器 swap 常驻")
        rv, _ = F.build_suppressions(sv.load(), now=T)
        sid = rv[0]["sup_id"]
        try:
            sv.revoke(sup_id=sid, reason="")
            check("撤销也要理由", False)
        except ValueError:
            check("撤销也要理由（否则半年后没人知道为什么撤了）", True)
        sv.revoke(sup_id=sid, reason="换盘之后 swap 确实有问题了")
        rv2, rejv = F.build_suppressions(sv.load(), now=T)
        check("撤销后规则不再生效", rv2 == [], rv2)
        check("被撤销的规则会出现在「被拒绝」里并说明原因",
              any("已被撤销" in x["why"] for x in rejv), rejv)
        keep, su = F.apply_suppressions([fnd("SWAP_PRESSURE", "warning", "swap")], "b1", rv2, now=T)
        check("撤销后发现重新被报出来", len(keep) == 1 and not su)

        # ══════════════════════════════════════════════════════════════
        print("[7] 命中计数：让「过宽 / 失效」从数据里看出来")
        # ══════════════════════════════════════════════════════════════
        sc = F.FeedbackStore(os.path.join(tmp, "fc.jsonl"))
        sc.add("b1", "PSI_MEM_PRESSURE", "PSI memory", "warning", reason="这台机器常年在等内存")
        rc, _ = F.build_suppressions(sc.load(), now=T)
        state = F.SuppressionState(os.path.join(tmp, "state.json"))
        _, su = F.apply_suppressions([fnd("PSI_MEM_PRESSURE", "warning", "PSI memory",
                                         "avg60=31%")], "b1", rc, now=T)
        state.record(su, now=T, host="b1")
        state.record(su, now=T + 60, host="b1")
        st_data = state.load()
        check("命中次数跨运行累加", st_data[rc[0]["sup_id"]]["hits"] == 2,
              st_data[rc[0]["sup_id"]]["hits"])
        check("留下最近几次的样本（可复盘）",
              len(st_data[rc[0]["sup_id"]]["recent"]) == 2)
        for i in range(10):
            state.record(su, now=T + 120 + i, host="b1")
        check("最近样本有上界（状态文件不许无界增长）",
              len(state.load()[rc[0]["sup_id"]]["recent"]) == 3,
              len(state.load()[rc[0]["sup_id"]]["recent"]))
        with open(os.path.join(tmp, "state.json"), "w", encoding="utf-8") as f:
            f.write("{坏 json")
        check("状态文件坏掉 → 当没有，不拖垮主流程", state.load() == {})

        sb = F.FeedbackStore(os.path.join(tmp, "fb2.jsonl"))
        sb.add("b1", "DISK_USED_HIGH", "/data", "warning", reason="r")
        rb, _ = F.build_suppressions(sb.load(), now=T)
        stb = F.SuppressionState(os.path.join(tmp, "state2.json"))
        _, sub_ = F.apply_suppressions([fnd(subject="/data")], "b1", rb, now=T)
        for i in range(F.BROAD_HIT_WARN + 1):
            stb.record(sub_, now=T + i, host="b1")
        stats_b, notes_b = F.audit_suppressions(rb, stb, now=T + 100)
        check("★ 命中过多 → 点名「疑似过宽」，并建议改阈值而不是压制",
              any("疑似过宽" in n and "改阈值" in n for n in notes_b), notes_b)
        stats_new, notes_new = F.audit_suppressions(rb, F.SuppressionState(
            os.path.join(tmp, "state3.json")), now=T + 60)
        check("刚建的规则 0 命中**不**说它失效（否则提示会被无视）", notes_new == [], notes_new)
        _, notes_old = F.audit_suppressions(
            [{**rb[0], "created_ts": T - 8 * 86400, "expires_ts": T + 8 * 86400}],
            F.SuppressionState(os.path.join(tmp, "state4.json")), now=T)
        check("建立超过 7 天且 0 命中 → 提示可撤销",
              any("疑似失效" in n and "撤销" in n for n in notes_old), notes_old)

        # ══════════════════════════════════════════════════════════════
        print("[8] 阈值建议：比「逐个压制」更好的那条路")
        # ══════════════════════════════════════════════════════════════
        sg = F.FeedbackStore(os.path.join(tmp, "fg.jsonl"))
        for i, sub in enumerate(["/a", "/b", "/c"]):
            sg.add("b1", "DISK_USED_HIGH", sub, "warning", reason=f"这些盘都留了空间 r{i}")
        sg.add("b1", "THERMAL_HIGH", "soc", "warning", reason="单点，不构成系统性问题")
        sug = F.suggest_thresholds(sg.load(), min_marks=3)
        check("同一规则在 3 个不同对象上误报 → 建议调阈值",
              len(sug) == 1 and sug[0]["rule"] == "DISK_USED_HIGH"
              and sug[0]["distinct_subjects"] == 3, sug)
        check("建议里点名「改阈值而不是逐个压制」", "调阈值" in sug[0]["suggestion"])
        check("只有 1 个对象的规则不产生建议（避免噪音）",
              all(s["rule"] != "THERMAL_HIGH" for s in sug))

        # ══════════════════════════════════════════════════════════════
        print("[9] 自检与渲染")
        # ══════════════════════════════════════════════════════════════
        good, _ = F.build_suppressions(s2.load(), now=T)
        check("合法规则自检通过", F.check_suppressions(good) == [], F.check_suppressions(good))
        bad_rule = [{"sup_id": "SUP-bad", "rule": "R", "subject": "/s", "match": "rule",
                     "ceiling": "warning", "reason": "", "marks": 1,
                     "created_ts": T, "expires_ts": T}]
        probs = F.check_suppressions(bad_rule)
        check("无理由 / 无有效期 / 宽匹配证据不足 全部被抓到",
              any("没有理由" in p for p in probs)
              and any("没有有效期" in p for p in probs)
              and any("宽匹配" in p for p in probs), probs)

        txt = F.render_suppressed([{**fnd(), "suppressed_by": "SUP-x",
                                    "suppress_reason": "留了缓存空间", "suppress_expires": "2026-10-01",
                                    "suppress_match": "exact", "suppress_ceiling": "warning"}],
                                  good, total=10)
        check("已抑制区写明「抑制不等于删除」", "不等于删除" in txt, txt.splitlines()[0])
        check("每条都能回答被谁压住 / 为什么 / 何时到期",
              "SUP-x" in txt and "留了缓存空间" in txt and "2026-10-01" in txt)
        txt2 = F.render_suppressed([{**fnd(), "suppressed_by": "SUP-x", "suppress_reason": "r",
                                     "suppress_expires": "e", "suppress_match": "exact",
                                     "suppress_ceiling": "warning"}] * 6, good, total=10)
        check("★ 抑制率过高时运行时直接警告（先确认不是把监控关掉了）",
              "抑制率" in txt2 and "监控关掉" in txt2)
        check("空抑制区渲染为空串（不产出噪声）", F.render_suppressed([], good) == "")
        rt = F.render_rules(good, rejected=[{"fb_id": "FB-1", "why": "没有理由"}],
                            state=F.SuppressionState(os.path.join(tmp, "state5.json")), now=T)
        check("规则渲染里列出「被拒绝的标记」（不能沉默）", "被拒绝" in rt and "FB-1" in rt)

        # ══════════════════════════════════════════════════════════════
        print("[10] 真实 fixture 端到端：标记 → 抑制 → 退出码跟着变")
        # ══════════════════════════════════════════════════════════════
        import collect as C
        dump = os.path.join(HERE, "logs", "fixtures", "linux_dump_board.txt")
        if os.path.exists(dump):
            with open(dump, "r", encoding="utf-8", errors="replace") as f:
                snap = C.parse_linux_dump(f.read())
            fs = C.evaluate(snap)
            host = snap.get("host")
            sev_exit = {"critical": 2, "warning": 1, "info": 0}

            def exit_of(fl):
                return max((sev_exit.get(x["severity"], 0) for x in fl), default=0)

            before = exit_of(fs)
            target = [x for x in fs if x["rule"] == "TCP_TIME_WAIT_HIGH"]
            check("真实数据里确实有可标记的发现", bool(target), [x["rule"] for x in fs])
            sreal = F.FeedbackStore(os.path.join(tmp, "freal.jsonl"))
            sreal.add(host, "TCP_TIME_WAIT_HIGH", target[0]["subject"], "warning",
                      reason="这台板子跑 MQTT 长连接池，TIME_WAIT 高是设计如此")
            rreal, _ = F.build_suppressions(sreal.load(), now=T)
            kept, sup = F.apply_suppressions(fs, host, rreal, now=T)
            check("★ 真实数据：抑制后总额守恒（不丢发现）",
                  len(kept) + len(sup) == len(fs), f"{len(kept)}+{len(sup)} vs {len(fs)}")
            check("真实数据：被抑制的那条被标记来源", sup and sup[0]["suppressed_by"] == rreal[0]["sup_id"])
            check("真实数据：其它发现一条都没被误伤",
                  {x["rule"] for x in kept} == {x["rule"] for x in fs if x["rule"] != "TCP_TIME_WAIT_HIGH"},
                  sorted({x["rule"] for x in kept}))
            check("真实数据：退出码按「保留」计算", exit_of(kept) == before,
                  f"{before} -> {exit_of(kept)}（该条是 warning，改动不应升级/降级 critical）")
            keepc = [x for x in fs if x["rule"] != "DISK_INODE_HIGH"]
            check("（对照）去掉 critical 后退出码确实会变",
                  exit_of(keepc) < exit_of(fs) or exit_of(fs) == 2, "critical 存在则仍为 2")
        else:
            check("真实 fixture 存在", False, dump)

        # ══════════════════════════════════════════════════════════════
        print("[11] CLI：拒绝非法输入时退出码要能当门禁用")
        # ══════════════════════════════════════════════════════════════
        p1 = os.path.join(tmp, "cli1.jsonl")
        rc_bad = F._main(["--mark", "--rule", "R", "--subject", "/s", "--reason", "",
                          "--store", p1, "--state", os.path.join(tmp, "cs.json")])
        check("CLI 无理由标记 → 退出码 2", rc_bad == 2, rc_bad)
        rc_ok = F._main(["--mark", "--rule", "R", "--subject", "/s", "--reason", "测试理由",
                         "--store", p1, "--state", os.path.join(tmp, "cs.json")])
        check("CLI 正常标记 → 退出码 0", rc_ok == 0, rc_ok)
        rc_check = F._main(["--check", "--store", p1, "--state", os.path.join(tmp, "cs.json")])
        check("CLI --check 通过 → 退出码 0（可进 CI）", rc_check == 0, rc_check)
        rc_wide = F._main(["--mark", "--rule", "R", "--subject", "/s", "--reason", "r",
                           "--match", "rule", "--store", p1, "--state", os.path.join(tmp, "cs.json")])
        check("CLI 宽匹配不加 --wide → 退出码 2", rc_wide == 2, rc_wide)
        rc_rev = F._main(["--revoke", "SUP-nope", "--reason", "撤掉", "--store", p1,
                          "--state", os.path.join(tmp, "cs.json")])
        check("CLI 撤销成功 → 退出码 0", rc_rev == 0, rc_rev)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 74)
    print(f"结果：{len(PASS)} 项通过，{len(FAIL)} 项失败（耗时 {time.perf_counter() - t0:.2f}s）")
    if FAIL:
        for f in FAIL:
            print(f"  ✗ {f}")
        sys.exit(1)
    print("反馈与抑制是纯逻辑 + 临时文件 —— 免费、可复现，已进 CI 免费档。")


if __name__ == "__main__":
    main()
