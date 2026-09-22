#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
作品 3 · 第 10 周 Day 34 —— M4 编排模式：workflow vs Agent
════════════════════════════════════════════════════════════════
本文件要读懂的只有一句话（《Building Effective Agents》主结论）：

    能用 workflow 解决，就不要上 agent。

**workflow 和 agent 的区别不在"有没有工具"，而在"控制流归谁定"：**

    workflow：LLM 与工具沿**预定义的代码路径**走 —— 走几步、什么时候停，
              是 **if / for / 表** 决定的（程序定，可预测、可测试、成本可控）
    agent   ：模型**自己决定**下一步调什么、要不要继续、几轮停
              （模型定，灵活，但成本/延迟/行为不可预测）

所以本文件的每个模式，都要标出「**确定性在哪一步**」——
这一栏才是这张表的重点，调用次数和耗时只是它的代价。

五种模式（对应原文 5 个小节）：
    1 prompt chaining     串行分解 + **程序网关**（代码决定要不要走下一步）
    2 routing             先分类 → 按**分派表**走不同下游（代码决定分派）
    3 parallelization     sectioning（拆分并行）/ voting（多次采样取多数）
    4 orchestrator-workers 编排者拆任务（模型定），证据靠工具取（程序定），再汇总
    5 evaluator-optimizer  生成 → 评测 → 不达标重写（**轮数上限必须写死**）

跑法：
    python day34_patterns.py                 # 跑全部，出对比表
    python day34_patterns.py --pattern route # 只跑一个（调试用）
    python day34_patterns.py --list          # 看有哪些模式
"""
import os
import re
import sys
import json
import time
from concurrent.futures import ThreadPoolExecutor

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# 复用 day32：工具实现、路径防护、client、模型名都只有一份。
# 复制一份 = 开一个没有测试保护的分叉（Day 33 的同一条理由）。
import day32_function_calling as d32

MODEL = d32.MODEL
LOG_FILE = "sample_app.log"
LOG_TEXT = d32.read_log(LOG_FILE, 30)          # 真实输入：一次读全，后面各模式共用

# 全体统一问题（原文说 workflow 的价值在于"任务可分解"，用它做基准最公平）
QUESTION = "分析这份应用日志，判断根因并给出处置建议"


# ══════════════════════════════════════════════════════════════════
# 仪表：每个模式花了多少次调用 / 多少秒 / 多少字符
# ══════════════════════════════════════════════════════════════════
# 为什么要记：面试问"成本怎么控"，workflow 相对 agent 的全部优势都要靠这几个数说清楚。
# 注意 DeepSeek 流式不返回 usage，所以这里一律走非流式。
class Meter:
    def __init__(self):
        self.reset()

    def reset(self):
        self.calls = 0
        self.seconds = 0.0
        self.prompt_chars = 0
        self.out_chars = 0
        self.truncated = 0          # finish_reason == length 的次数（Day 32 的静默错误）
        self.extra = ""             # 该模式自己补充的说明（如投票票型）

    def record(self, seconds, prompt_chars, out_chars, finish_reason):
        self.calls += 1
        self.seconds += seconds
        self.prompt_chars += prompt_chars
        self.out_chars += out_chars
        if finish_reason == "length":
            self.truncated += 1


METER = Meter()
CLIENT = None           # main() 里创建，避免 import 本文件就去连网


def chat(messages, temperature=0.0, json_mode=False, max_tokens=None):
    """单次 LLM 调用（不带工具）。所有模式的唯一出口 —— 计量就挂在这一处。

    temperature 默认 0：评测/对比必须可复现，不固定就没有可比性（Day 30 的规矩）。
    json_mode 有个 API 限制（Day 12 实测）：prompt 里必须出现 "json" 字样。
    """
    kwargs = {"model": MODEL, "messages": messages, "temperature": temperature}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    if max_tokens:
        kwargs["max_tokens"] = max_tokens

    t0 = time.perf_counter()
    resp = CLIENT.chat.completions.create(**kwargs)
    dt = time.perf_counter() - t0

    choice = resp.choices[0]
    text = choice.message.content or ""
    METER.record(dt, sum(len(m.get("content") or "") for m in messages),
                 len(text), choice.finish_reason)

    # finish_reason 的 5 个取值里，length / aborted / insufficient_system_resource
    # 都是"静默错误"：不报错，但内容是残缺的。不吭声就会污染整张对比表。
    if choice.finish_reason != "stop":
        print(f"    ⚠️  finish_reason={choice.finish_reason}（内容可能不完整）")
    return text


def chat_json(messages, **kw):
    """要结构化输出的调用：json_mode + 容错解析。

    模型时不时会用 ```json 包一层或加句前言，所以要剥壳（而不是相信它）。
    解析失败必须**抛**，不能静默返回空——空字典会一路传下去，最后表现为"答案很蠢"，
    根因却在一百行之前（Day 32 的静默错误同族）。
    """
    raw = chat(messages, json_mode=True, **kw)
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1:
        text = text[start:end + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"模型没有给出合法 JSON（{e}）：{raw[:200]}") from e


def timed(fn):
    """模式执行器：清空仪表 → 跑 → 记录结果。"""
    def wrapper(*args, **kwargs):
        METER.reset()
        t0 = time.perf_counter()
        try:
            result = fn(*args, **kwargs)
            error = ""
        except Exception as e:                     # 一个模式崩了不该毁掉整张表
            result, error = f"[失败] {type(e).__name__}: {e}", f"{type(e).__name__}"
        wall = time.perf_counter() - t0
        return {
            "answer": result, "error": error, "wall": wall,
            "calls": METER.calls, "seconds": METER.seconds,
            "prompt_chars": METER.prompt_chars, "out_chars": METER.out_chars,
            "truncated": METER.truncated, "extra": METER.extra,
        }
    return wrapper


# ══════════════════════════════════════════════════════════════════
# 模式 1 · prompt chaining —— 把任务拆成固定的几步，步与步之间放**程序网关**
# ══════════════════════════════════════════════════════════════════
# 确定性在哪一步：**网关**。异常数不足 2 条就停在第一步，这一步不花模型一分钱。
# 这也是它相对 agent 的核心优势：**明显没事的时候，成本是零**。
@timed
def pattern_chain():
    # 第 1 步：抽取（只做抽取，不让它下结论 —— 拆步的意义就是每次只干一件事）
    step1 = chat_json([
        {"role": "system", "content": "你是日志分析助手，只做抽取，不做推断。以 JSON 输出。"},
        {"role": "user", "content":
            f"从下面的日志里抽取所有异常事件，输出 JSON："
            f'{{"events": [{{"time": "...", "component": "...", "summary": "..."}}]}}\n\n日志：\n{LOG_TEXT}'},
    ])
    events = step1.get("events", [])

    # ── 程序网关：走不走第二步，由代码决定，不是模型 ──────────────────
    if len(events) < 2:
        return f"[网关拦截] 只抽到 {len(events)} 条异常，不足 2 条，**不进入归因步骤**（省一次调用）。"

    # 第 2 步：归因（输入是第 1 步的结构化结果 → 上游输出即下游输入，这是链式分解的要点）
    step2 = chat([
        {"role": "system", "content": "你是 SRE，输出中文，先给根因排序，再给处置建议。"},
        {"role": "user", "content":
            f"已知异常事件：{json.dumps(events, ensure_ascii=False)}\n"
            f"请判断根因（区分因果与并发）并给处置建议，200 字内。"},
    ])
    return f"抽取 {len(events)} 条异常 → 归因结果：\n{step2}"


# ══════════════════════════════════════════════════════════════════
# 模式 2 · routing —— 先分类，再按**分派表**走不同下游
# ══════════════════════════════════════════════════════════════════
# 确定性在哪一步：**分派表**（一个 dict）。
# 适用前提：输入能被干净地分成几类，且每类的处理方式**预先就知道**。
# 代价：分类错了整条链路都错 —— 单点错误会被放大（所以要让它输出 confidence）。
ROUTES = {
    "db":   "数据库连接类故障。重点看连接超时/拒绝、重试耗尽、降级模式。",
    "disk": "磁盘空间类故障。重点看剩余空间趋势、写失败、缓冲溢出丢数据。",
    "http": "服务接口类故障。重点看 5xx 比例、响应时间劣化、健康检查。",
    "other": "其他或无法归类。给出最保守的排查顺序。",
}


@timed
def pattern_route():
    decision = chat_json([
        {"role": "system", "content": "你是日志分类器。只输出 JSON，不要解释。"},
        {"role": "user", "content":
            f'把这批日志归入唯一一类，输出 JSON：{{"category": "db|disk|http|other", "confidence": 0.0-1.0}}\n\n'
            f"日志：\n{LOG_TEXT}"},
    ])
    category = decision.get("category", "other")
    conf = decision.get("confidence", 0)

    # 分派表兜底：模型给了表里没有的类别也不崩（不可信输入原则，Day 32 同一条）
    route_prompt = ROUTES.get(category, ROUTES["other"])

    answer = chat([
        {"role": "system", "content": "你是 SRE，输出中文，200 字内。"},
        {"role": "user", "content": f"专用视角：{route_prompt}\n日志：\n{LOG_TEXT}"},
    ])
    return f"分类={category}（自评置信度 {conf}）→ 走专用 prompt：\n{answer}"


# ══════════════════════════════════════════════════════════════════
# 模式 3a · parallelization · sectioning —— 拆分后并行，最后**由程序合并**
# ══════════════════════════════════════════════════════════════════
# 确定性在哪一步：**拆分规则和合并顺序**。模型只负责各自那一小片。
# 适用前提：子任务互不依赖（能并行）且**可以预先切开**。
SECTIONS = {                     # 拆分规则由代码持有，不是模型"决定"分几片
    "db":   r"\[db\]",
    "disk": r"\[disk\]|No space left",
    "http": r"\[http\]|\[main\]|\[log\]|\[cache\]",
}


@timed
def pattern_parallel_sectioning():
    lines = LOG_TEXT.splitlines()
    slices = {}
    for name, pat in SECTIONS.items():                   # 正则切分：纯程序，可单测
        picked = [ln for ln in lines if re.search(pat, ln, re.I)]
        if picked:
            slices[name] = "\n".join(picked)

    def summarize(item):
        name, text = item
        return name, chat([
            {"role": "system", "content": "你是 SRE，只归纳你看到的这段日志，50 字内，中文。"},
            {"role": "user", "content": f"组件：{name}\n日志片段：\n{text}"},
        ])

    with ThreadPoolExecutor(max_workers=len(slices)) as pool:
        parts = list(pool.map(summarize, slices.items()))

    # 合并**顺序由代码定**（SECTIONS 的键顺序），不靠模型自己排 —— 顺序即确定性
    order = list(SECTIONS)
    parts.sort(key=lambda p: order.index(p[0]))
    body = "\n".join(f"  · [{n}] {s}" for n, s in parts)
    return f"并行归纳 {len(parts)} 个组件（合并顺序由代码定）：\n{body}"


# ══════════════════════════════════════════════════════════════════
# 模式 3b · parallelization · voting —— 同一问题多次采样，**由代码数票**
# ══════════════════════════════════════════════════════════════════
# 确定性在哪一步：**计票规则**。
# ⚠️ 两个前提，缺一个投票就是自欺：
#   ① 答案必须**客观可判定**（能归到有限的桶里），散文没法投票；
#   ② 必须**temperature > 0** —— temperature=0 时三次回答一模一样，投票恒全票通过，
#      你测到的只是"API 幂等"，不是"模型的一致性"。
VOTE_OPTIONS = ["db", "disk", "http", "other"]


@timed
def pattern_parallel_voting(n=3):
    def one_vote(_):
        return chat_json([
            {"role": "system", "content": "你是日志分析器。只输出 JSON。"},
            {"role": "user", "content":
                f'这份日志的**首要根因**属于哪一类？输出 JSON：'
                f'{{"root_cause": "db|disk|http|other", "reason": "一句话"}}\n\n日志：\n{LOG_TEXT}'},
        ], temperature=1.0)                              # ← 必须 > 0，理由见上

    with ThreadPoolExecutor(max_workers=n) as pool:
        votes = list(pool.map(one_vote, range(n)))

    labels = [v.get("root_cause", "other") for v in votes]
    tally = {opt: labels.count(opt) for opt in VOTE_OPTIONS}
    winner = max(tally, key=tally.get)
    METER.extra = f"票型 {dict((k, v) for k, v in tally.items() if v)}"

    lines = "\n".join(f"  · 第 {i + 1} 次：{lab} —— {votes[i].get('reason', '')}"
                      for i, lab in enumerate(labels))
    return (f"{n} 次采样（temperature=1.0）：\n{lines}\n"
            f"  计票（代码数）→ 多数票：**{winner}**" +
            ("　（票数不集中，说明该问题连模型自己都不稳定）"
             if tally[winner] < n - 0.5 else ""))


# ══════════════════════════════════════════════════════════════════
# 模式 4 · orchestrator-workers —— 让模型拆活，但**证据由程序去取**
# ══════════════════════════════════════════════════════════════════
# 确定性在哪一步：**取证据**。编排者（模型）说什么关键字，程序就用 search_log 去搜 ——
# 模型只在"我没有的能力"上自由，凡是程序能确定做的事都不交给它。
# 这是本文件最值钱的一个模式：它把 agent 的灵活性和 workflow 的可控性缝在一起，
# 也正是第 11 周作品 3 要用的骨架。
@timed
def pattern_orchestrator():
    plan = chat_json([
        {"role": "system", "content": "你是诊断任务的编排者。只输出 JSON，不要自己回答。"},
        {"role": "user", "content":
            f'把「{QUESTION}」拆成 2-4 个可独立验证的子任务，输出 JSON：'
            f'{{"subtasks": [{{"id": 1, "goal": "...", "keyword": "去日志里搜的关键词"}}]}}\n'
            f"（keyword 必须出现在日志里，用于程序取证）"},
    ])
    subtasks = plan.get("subtasks", [])[:4]
    if not subtasks:
        return "[编排者没给出子任务] 计划阶段失败，直接退回单次调用。"

    def run_worker(st):
        # 证据不是模型说的，是**程序用 day32 的 search_log 取出来的**（确定性 + 可审计）
        evidence = d32.search_log(LOG_FILE, st.get("keyword", ""), max_hits=6)
        conclusion = chat([
            {"role": "system", "content": "你是 SRE，只依据给定证据回答，证据不足就直说。中文，80 字内。"},
            {"role": "user", "content":
                f"子任务：{st.get('goal')}\n证据（来自 search_log('{st.get('keyword')}')）：\n{evidence}"},
        ])
        return st, evidence, conclusion

    with ThreadPoolExecutor(max_workers=len(subtasks)) as pool:
        done = list(pool.map(run_worker, subtasks))

    # 汇总也是程序拼装：把「子任务 + 证据 + 结论」原样给汇总者，不让它复述事实、只让它综合
    bundle = "\n\n".join(
        f"[子任务 {st.get('id')}] {st.get('goal')}\n证据：{ev[:600]}\n结论：{cc}"
        for st, ev, cc in done)
    final = chat([
        {"role": "system", "content": "你是 SRE 主管，综合下属结论给最终判断。中文，250 字内。"},
        {"role": "user", "content": f"子任务结论如下：\n\n{bundle}\n\n请综合判断根因与处置建议。"},
    ])
    return f"编排出 {len(subtasks)} 个子任务（证据由程序取）→ 汇总：\n{final}"


# ══════════════════════════════════════════════════════════════════
# 模式 5 · evaluator-optimizer —— 生成 → 评测 → 不达标重写
# ══════════════════════════════════════════════════════════════════
# 确定性在哪一步：**停止条件**。标准在代码里，模型只提供分数和被吐槽的点。
# ⚠️ 三个必须知道的事：
#   ① **轮数上限必须写死** —— 否则"再改改"能无限循环（Day 32 的 max_rounds 同理）；
#   ② **同模型自评有 self-preference 偏差** —— 它偏爱自己的措辞，分数偏高。
#      生产里评测者应换模型/换 prompt 视角，或干脆用客观检查表；
#   ③ 评测者输出的必须是**可解析的结构化结果**，否则"好不好"又变成了模型说了算。
RUBRIC = ("1 是否指出数据库与磁盘两组故障并发；2 是否区分根因与次生现象；"
          "3 处置建议是否具体可执行（命令级）；4 是否声明日志有丢失（2048 行）的局限")
PASS_SCORE = 8


@timed
def pattern_evaluator_optimizer(max_rounds=2):
    draft = chat([
        {"role": "system", "content": "你是 SRE，输出中文，250 字内。"},
        {"role": "user", "content": f"{QUESTION}\n\n日志：\n{LOG_TEXT}"},
    ])
    history = [f"初稿（{len(draft)} 字）"]

    for round_no in range(1, max_rounds + 1):        # ← 上限写死，这是确定性的所在
        review = chat_json([
            {"role": "system", "content": "你是严格的技术评审，只输出 JSON。"},
            {"role": "user", "content":
                f'按评分标准给下面这份诊断报告打分，输出 JSON：'
                f'{{"score": 0-10, "missing": ["..."], "reason": "..."}}\n\n'
                f"评分标准：{RUBRIC}\n\n报告：\n{draft}"},
        ])
        score = review.get("score", 0)
        missing = review.get("missing", [])
        history.append(f"第 {round_no} 轮评分 {score} 分，缺：{'；'.join(missing) or '无'}")

        if score >= PASS_SCORE:                       # 达标的判据在代码里
            history.append("→ 达标，停止（不再多花一次调用）")
            break

        draft = chat([
            {"role": "system", "content": "你是 SRE，输出中文，250 字内，只重写全文。"},
            {"role": "user", "content":
                f"原报告：\n{draft}\n\n评审指出的缺失：{json.dumps(missing, ensure_ascii=False)}\n"
                f"请针对性补上，不要复述评审意见。"},
        ])
        history.append(f"重写第 {round_no} 版（{len(draft)} 字）")
    else:
        history.append(f"→ 达到上限 {max_rounds} 轮仍未达标，带着现有稿收工")

    return "\n".join(f"  · {h}" for h in history) + f"\n\n最终稿：\n{draft}"


# ══════════════════════════════════════════════════════════════════
# 注册表 + 对比表
# ══════════════════════════════════════════════════════════════════
# 「确定性在哪一步」这一列是手写的、不是跑出来的 —— 它是设计决策，不是性能指标。
PATTERNS = [
    ("chain",       "prompt chaining",      "程序网关（异常数<2 就停）", pattern_chain),
    ("route",       "routing",              "分派表 dict（分类由模型）",  pattern_route),
    ("sectioning",  "parallel·sectioning",  "切分正则 + 合并顺序",        pattern_parallel_sectioning),
    ("voting",      "parallel·voting",      "计票规则（需 temp>0）",      pattern_parallel_voting),
    ("orchestrator", "orchestrator-workers", "证据由程序 search_log 取",  pattern_orchestrator),
    ("evaluator",   "evaluator-optimizer",  "停止条件 + 轮数上限",        pattern_evaluator_optimizer),
]


def print_table(rows):
    print("\n" + "=" * 96)
    print("对比表 —— 调用次数 / 耗时是代价，「确定性在哪一步」才是这一栏的重点")
    print("=" * 96)
    print(f"{'模式':<14}{'调用':>4}{'LLM耗时':>9}{'墙钟':>8}{'prompt字':>9}{'输出字':>8}  确定性在哪一步")
    print("-" * 96)
    for name, label, where, r in rows:
        if r["error"]:
            print(f"{name:<14}{'--':>4}{'--':>9}{r['wall']:>7.1f}s{'--':>9}{'--':>8}  [失败] {r['answer'][:40]}")
            continue
        print(f"{name:<14}{r['calls']:>4}{r['seconds']:>8.1f}s{r['wall']:>7.1f}s"
              f"{r['prompt_chars']:>9}{r['out_chars']:>8}  {where}")
        if r["truncated"]:
            print(f"{'':<14}⚠️  有 {r['truncated']} 次生成被截断（finish_reason=length），该行数据不可信")
    print("-" * 96)

    ok = [r for _, _, _, r in rows if not r["error"]]
    if ok:
        print(f"合计 {sum(r['calls'] for r in ok)} 次调用、"
              f"{sum(r['seconds'] for r in ok):.1f}s LLM 时间、"
              f"{sum(r['prompt_chars'] for r in ok)} 字符 prompt。")
        print("备注：墙钟 < LLM 耗时 的说明该模式**并行**了（sectioning/voting/orchestrator 会这样）。")
        print(f"     调用次数最多的 {max(ok, key=lambda r: r['calls'])['calls']} 次 —— "
              f"最贵的不一定最好，看它把确定性放在哪一步。")


def main():
    global CLIENT

    if "--list" in sys.argv:
        for key, label, where, _ in PATTERNS:
            print(f"  {key:<14}{label:<24}确定性：{where}")
        return

    only = None
    if "--pattern" in sys.argv:
        only = sys.argv[sys.argv.index("--pattern") + 1]

    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("错误：.env 里没有 DEEPSEEK_API_KEY")
        return

    CLIENT = d32.make_client()          # trust_env=False，防系统代理吃掉请求

    rows = []
    for key, label, where, fn in PATTERNS:
        if only and key != only:
            continue
        print("\n" + "─" * 96)
        print(f"▶ {label}　（{key}）")
        print("─" * 96)
        r = fn()
        rows.append((key, label, where, r))
        print(r["answer"] if not r["error"] else r["answer"])
        if r["extra"]:
            print(f"    （{r['extra']}）")

    if rows:
        print_table(rows)
        print("\n读表提示：调用次数最多/耗时最长的那个，未必是最好的方案 ——"
              "\n先问「这一步的确定性在谁手里」，再问「这个不确定性我承担得起吗」。")


if __name__ == "__main__":
    main()
