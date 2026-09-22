# -*- coding: utf-8 -*-
"""作品 3 · 第 10 周 Day 35 —— LangGraph 1.x：把 Day 34 的 orchestrator 骨架搬上图

为什么值得上框架（不是"框架更高级"，是这四件事手写循环做不到）：
  1. 控制流变成**数据结构**（节点 + 边）→ 能画出来给人看、能审、能 diff
  2. **checkpointer**：每步状态落盘 → 进程死了能从断点续跑（thread_id 是索引）
  3. **interrupt()**：图能停在半路等人 → 副作用工具的人机确认（Day 32 遗留③）落地
  4. 并行/汇合（fan-out + barrier）不用自己写线程池和 join

本图就是 Day 34 定下的架构（组合，不是单选）：

    START → scan（程序网关，0 次 LLM）─ 有异常? ─→ plan（模型拆子任务）
                                  └─ 无异常 ─→ END（一分钱不花）
    plan ──Send 并行──→ investigate × N（程序取证 + 模型归纳）── 汇合 ──→ confirm（人审批）
                                                                          ├─ 批准 → report → END
                                                                          └─ 不批准 → END

跑法（**注意解释器换了**，见文末"环境隔离"）：
    .venv-agent\\Scripts\\python.exe day35_langgraph.py               # 单进程跑完整链路（含 interrupt→resume）
    .venv-agent\\Scripts\\python.exe day35_langgraph.py --graph       # 只打印 mermaid 图，不花钱
    .venv-agent\\Scripts\\python.exe day35_langgraph.py --stage1      # 跑到 confirm 暂停就退出
    .venv-agent\\Scripts\\python.exe day35_langgraph.py --stage2      # **新进程**用同一 thread_id 续跑（真跨进程）
    .venv-agent\\Scripts\\python.exe day35_langgraph.py --clean       # 换健康日志跑：验证网关"0 次调用收工"
    .venv-agent\\Scripts\\python.exe day35_langgraph.py --prebuilt    # 对照：langchain 1.x create_agent 长什么样
"""
import os
import re
import sys
import json
import time
import sqlite3
import argparse
import operator
from typing import Annotated, TypedDict

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_HERE = os.path.dirname(os.path.abspath(__file__))

from dotenv import load_dotenv
load_dotenv(os.path.join(_HERE, ".env"))          # 显式指定：不依赖 cwd（Day 33 的教训）

import day32_function_calling as d32              # 工具层复用，不复制实现
from langgraph.graph import StateGraph, START, END
from langgraph.types import Send, Command, interrupt
from langgraph.checkpoint.memory import InMemorySaver

MODEL = d32.MODEL
LOG_FILE = "sample_app.log"
CLEAN_LOG = "fixtures/quiet.log"                  # 健康日志：用来验证网关分支
GATE_MIN_ERRORS = 3                               # 程序网关的阈值：ERROR 少于 3 条就当"没事"
CHECKPOINT_DB = os.path.join(_HERE, "checkpoints.sqlite")
QUESTION = "分析这份应用日志，判断根因并给出处置建议"

# 副作用动作：这些**不能**由模型自己执行，必须人批（Day 32 坑表 #13）
SIDE_EFFECT_ACTIONS = [
    "清理 /var/log/app 下 7 天前的旧日志以释放空间",
    "重启 app 服务（会中断在途请求）",
    "重启 postgresql 实例",
]


# ══════════════════════════════════════════════════════════════════
# 仪表：这一版按**节点**计费 —— 图上每一格花了多少钱要能指出来
# ══════════════════════════════════════════════════════════════════
METRICS = {}          # node -> {"calls": n, "seconds": s, "prompt_chars": c, "out_chars": o}
_CLIENT = None


def llm(node, messages, json_mode=False, temperature=0.0):
    """图里所有 LLM 调用的唯一出口。node 参数让成本能按节点归集。"""
    m = METRICS.setdefault(node, {"calls": 0, "seconds": 0.0, "prompt_chars": 0, "out_chars": 0})
    kwargs = {"model": MODEL, "messages": messages, "temperature": temperature}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    t0 = time.perf_counter()
    resp = _CLIENT.chat.completions.create(**kwargs)
    dt = time.perf_counter() - t0

    choice = resp.choices[0]
    text = choice.message.content or ""
    m["calls"] += 1
    m["seconds"] += dt
    m["prompt_chars"] += sum(len(x.get("content") or "") for x in messages)
    m["out_chars"] += len(text)

    # finish_reason 静默错误守卫（Day 32）：interrupt 之后的续跑如果内容残缺，
    # 报告会"看着正常但是错的"——这类错误比崩溃贵。
    if choice.finish_reason != "stop":
        print(f"    ⚠️  [{node}] finish_reason={choice.finish_reason}，内容可能不完整")
    return text


def llm_json(node, messages):
    """要结构化输出：json_mode + 剥壳 + **解析失败必须抛**（Day 34 同一条）"""
    raw = llm(node, messages, json_mode=True).strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", raw, re.S)
    if fence:
        raw = fence.group(1).strip()
    s, e = raw.find("{"), raw.rfind("}")
    if s != -1 and e != -1:
        raw = raw[s:e + 1]
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"[{node}] 模型没给出合法 JSON：{raw[:200]}") from exc


# ══════════════════════════════════════════════════════════════════
# 状态：图的"内存布局"。谁往里写、怎么写，由这里的 reducer 决定
# ══════════════════════════════════════════════════════════════════
# findings 用 operator.add 归约：并行分支各自 append，框架负责合并 ——
# 这就是"并行/汇合不用自己写线程池和 join"的具体含义。
class DiagState(TypedDict, total=False):
    question: str
    log_file: str
    error_count: int          # scan 写的：程序数出来的，不是模型说的
    plan: list                # plan 写的：子任务列表
    findings: Annotated[list, operator.add]
    decision: dict            # confirm 写的：人的审批结果
    report: str


class WorkerState(TypedDict):
    """并行 worker 的输入 schema（Send 的负载）——和整体 State 分开，避免并发写同一 channel。"""
    question: str
    log_file: str
    task: dict


# ══════════════════════════════════════════════════════════════════
# 节点 1 · scan —— **程序网关**：数 ERROR 行数，判定要不要往下走
# ══════════════════════════════════════════════════════════════════
# 这是 Day 34 的结论之一落地：明显没事时，成本是零。
# 注意计数用的是**工具**（d32.search_log，走沙箱校验），不是模型 —— 数字必须是程序数的。
def scan(state: DiagState):
    log = state.get("log_file", LOG_FILE)
    raw = d32.search_log(log, "ERROR", max_hits=500)
    count = len([ln for ln in raw.splitlines() if re.match(r"^\d+:", ln)])
    print(f"  [scan] 程序数出 ERROR 行数 = {count}（阈值 {GATE_MIN_ERRORS}）")
    return {"error_count": count}


def route_after_scan(state: DiagState):
    """条件边：走 plan 还是直接收工 —— 判据在**代码**里，模型没有投票权。"""
    return "plan" if state["error_count"] >= GATE_MIN_ERRORS else "end"


# ══════════════════════════════════════════════════════════════════
# 节点 2 · plan —— 让模型拆活（步骤事先不知道，这是唯一必须交给模型的决策）
# ══════════════════════════════════════════════════════════════════
def plan(state: DiagState):
    data = llm_json("plan", [
        {"role": "system", "content": "你是诊断任务的编排者。只输出 JSON，不要自己回答。"},
        {"role": "user", "content":
            f'把「{state["question"]}」拆成 2-4 个可独立验证的子任务，输出 JSON：'
            f'{{"subtasks": [{{"id": 1, "goal": "...", "keyword": "去日志里搜的关键词"}}]}}'},
    ])
    subtasks = data.get("subtasks", [])[:4]
    print(f"  [plan] 拆出 {len(subtasks)} 个子任务："
          f"{[t.get('keyword') for t in subtasks]}")
    return {"plan": subtasks}


def fan_out(state: DiagState):
    """Send = 动态 fan-out：并行分支数由**运行时的 plan 决定**（不是编译期写死的边）。

    这正是 workflow 与"图"的分界：边可以是数据驱动的，但驱动它的仍是代码。
    """
    return [Send("investigate", {"question": state["question"],
                                 "log_file": state.get("log_file", LOG_FILE),
                                 "task": t})
            for t in state["plan"]]


# ══════════════════════════════════════════════════════════════════
# 节点 3 · investigate —— 并行 worker：**程序取证 + 模型归纳**
# ══════════════════════════════════════════════════════════════════
# 证据不是模型"回忆"出来的，是程序用 search_log 取出来的（可审计、可复现）。
# 每个分支返回 {"findings": [一条]} —— operator.add 负责把这些列表合并。
def investigate(state: WorkerState):
    task = state["task"]
    keyword = task.get("keyword", "")
    evidence = d32.search_log(state["log_file"], keyword, max_hits=6)
    conclusion = llm("investigate", [
        {"role": "system", "content": "你是 SRE，只依据给定证据回答，证据不足就直说。中文，80 字内。"},
        {"role": "user", "content":
            f"子任务：{task.get('goal')}\n证据（search_log('{keyword}')）：\n{evidence}"},
    ])
    return {"findings": [{"id": task.get("id"), "goal": task.get("goal"),
                          "keyword": keyword, "conclusion": conclusion}]}


# ══════════════════════════════════════════════════════════════════
# 节点 4 · confirm —— 人机确认：**interrupt() 让图停在半路等人**
# ══════════════════════════════════════════════════════════════════
# 用 C 的直觉理解：这就是"图执行到一半，把现场存盘，返回一个中断码"。
# ⚠️ 最关键的一条（不看这条会写出重复扣款级别的 bug）：
#    **resume 时，节点会从头重新执行一遍**，interrupt() 之前的所有代码会再跑一次。
#    所以：副作用（写文件、发请求、扣费）必须放在 interrupt() **之后**，
#    或者做成幂等的。下面的 print 就是用来给你看"这个节点确实跑了两次"的。
_CONFIRM_RUNS = 0


def confirm(state: DiagState):
    global _CONFIRM_RUNS
    _CONFIRM_RUNS += 1
    print(f"  [confirm] 节点第 {_CONFIRM_RUNS} 次进入"
          f"{'（resume 会从头重跑，所以这个数字会变成 2）' if _CONFIRM_RUNS == 1 else ''}")

    # 把"打算执行的副作用动作"连同证据摘要交给人 —— interrupt 的载荷必须是可序列化的普通数据
    decision = interrupt({
        "ask": "以下处置动作带有副作用，需要你批准后才允许执行",
        "evidence": [f.get("conclusion", "") for f in state.get("findings", [])],
        "proposed_actions": SIDE_EFFECT_ACTIONS,
        "如何回答": "Command(resume={'approved': [...动作...], 'note': '备注'})",
    })
    print(f"  [confirm] 收到人的决定：{decision}")
    return {"decision": decision}


def route_after_confirm(state: DiagState):
    """条件边：人批了才出正式报告；一个动作都不批，就到此为止（图自己就停了）。"""
    approved = (state.get("decision") or {}).get("approved") or []
    return "report" if approved else "end"


# ══════════════════════════════════════════════════════════════════
# 节点 5 · report —— 综合 + 把"人批了什么"写进报告（决定权落纸面）
# ══════════════════════════════════════════════════════════════════
def report(state: DiagState):
    decision = state.get("decision") or {}
    bundle = "\n".join(f"- [{f.get('keyword')}] {f.get('conclusion')}" for f in state["findings"])
    text = llm("report", [
        {"role": "system", "content": "你是 SRE 主管，输出中文，250 字内，含根因、处置、局限。"},
        {"role": "user", "content":
            f"子任务结论：\n{bundle}\n\n"
            f"已获人工批准的动作：{json.dumps(decision.get('approved', []), ensure_ascii=False)}\n"
            f"人的备注：{decision.get('note', '')}\n\n"
            f"请给最终报告，并说明哪些动作尚未获批执行。"},
    ])
    return {"report": text}


# ══════════════════════════════════════════════════════════════════
# 搭图：控制流就是这几行 —— 它能被画出来，这就是"图"相对手写循环的全部意义
# ══════════════════════════════════════════════════════════════════
def build_graph(checkpointer):
    b = StateGraph(DiagState)
    b.add_node("scan", scan)
    b.add_node("plan", plan)
    b.add_node("investigate", investigate)
    b.add_node("confirm", confirm)
    b.add_node("report", report)

    b.add_edge(START, "scan")
    b.add_conditional_edges("scan", route_after_scan, {"plan": "plan", "end": END})
    # plan → 动态并行 investigate：分支数运行时才知道
    b.add_conditional_edges("plan", fan_out, ["investigate"])
    # 多条并行分支汇到一个节点 = 框架自带 barrier（等全部完成才开始 confirm）
    b.add_edge("investigate", "confirm")
    b.add_conditional_edges("confirm", route_after_confirm, {"report": "report", "end": END})
    b.add_edge("report", END)
    return b.compile(checkpointer=checkpointer)


def make_checkpointer(kind="memory"):
    """checkpointer 决定"断点存在哪"：

    memory → 只在进程内存里，**进程一退就没了**（只能演示，不能真续跑）
    sqlite → 落盘，**新进程用同一个 thread_id 就能接着跑**（这才是 HITL 的前提）
    """
    if kind == "sqlite":
        conn = sqlite3.connect(CHECKPOINT_DB, check_same_thread=False)
        from langgraph.checkpoint.sqlite import SqliteSaver
        return SqliteSaver(conn)
    return InMemorySaver()


# ══════════════════════════════════════════════════════════════════
# 跑法
# ══════════════════════════════════════════════════════════════════
def _banner(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def show_interrupt(out):
    """interrupt 的返回长这样：{'__interrupt__': (Interrupt(value=..., id=...),)}"""
    intrs = out.get("__interrupt__") or ()
    for i in intrs:
        payload = getattr(i, "value", i)
        print("  ⏸  图停在 confirm，等人审批。载荷：")
        print("     " + json.dumps(payload, ensure_ascii=False, indent=2).replace("\n", "\n     "))
    return bool(intrs)


def show_state(app, config):
    """checkpointer 的意义就在这：随时能把"这张图现在在哪、手里有什么"调出来。"""
    snap = app.get_state(config)
    print(f"  状态快照：next={snap.next}  已写入的键={sorted(k for k in snap.values if not k.startswith('__'))}")
    return snap


def metrics_table():
    print("\n" + "-" * 78)
    print(f"{'节点':<14}{'LLM 调用':>9}{'耗时':>10}{'prompt字':>10}{'输出字':>9}")
    print("-" * 78)
    for node, m in METRICS.items():
        print(f"{node:<14}{m['calls']:>9}{m['seconds']:>9.1f}s{m['prompt_chars']:>10}{m['out_chars']:>9}")
    total = sum(m["calls"] for m in METRICS.values())
    print("-" * 78)
    print(f"合计 {total} 次 LLM 调用；scan 节点 0 次（纯程序，这就是程序网关的价值）")


def run_single_process(clean=False):
    """单进程跑完整链路：invoke → 暂停 → resume（把"人"也演在同一进程里）"""
    _banner("① 单进程完整链路（图 → interrupt 暂停 → 人审批 → resume 续跑）")
    app = build_graph(make_checkpointer("sqlite"))
    config = {"configurable": {"thread_id": f"demo-{int(time.time())}"}}
    payload = {"question": QUESTION, "log_file": CLEAN_LOG if clean else LOG_FILE,
               "findings": []}   # findings 必须给初始值：reducer 通道从空列表开始

    out = app.invoke(payload, config)
    if not show_interrupt(out):
        _banner("图已直接跑完（没停）—— 说明 scan 网关把它挡在了门外")
        print(f"  error_count={out.get('error_count')}，report={out.get('report')}")
        metrics_table()
        return

    show_state(app, config)
    print("\n  ▶ 模拟人审批：只批准清日志，不动服务（这是 guardrails，不是客气话）")
    out = app.invoke(Command(resume={"approved": [SIDE_EFFECT_ACTIONS[0]],
                                     "note": "先只清日志观察，别动数据库和服务"}), config)
    _banner("最终报告")
    print(out.get("report"))
    print(f"\n  [confirm] 节点共执行 {_CONFIRM_RUNS} 次 —— 2 次是**正常**的：resume 会重跑节点")
    metrics_table()


def run_stage(which, thread):
    """跨进程续跑：两个独立进程，靠 SQLite 里的 checkpointer 接上"""
    app = build_graph(make_checkpointer("sqlite"))
    config = {"configurable": {"thread_id": thread}}

    if which == 1:
        _banner(f"① stage1：新起一次诊断（thread_id={thread}）→ 会在 confirm 处暂停并退出")
        out = app.invoke({"question": QUESTION, "log_file": LOG_FILE, "findings": []}, config)
        show_interrupt(out)
        print("\n  进程现在退出。**此时图和状态已经落在 checkpoints.sqlite 里了** ——")
        print("  接着跑：python day35_langgraph.py --stage2")
        show_state(app, config)
    else:
        _banner(f"② stage2：**另一个进程**读同一个 thread_id={thread} 续跑")
        snap = show_state(app, config)
        if "confirm" not in (snap.next or ()):
            print("  这个 thread 没有停在 confirm（可能已经跑完，或 stage1 没跑）。")
            print("  先跑 --stage1，或换一个 --thread 名字。")
            return
        out = app.invoke(Command(resume={"approved": SIDE_EFFECT_ACTIONS[:2],
                                         "note": "跨进程续跑：批准清日志 + 重启 app 服务，不动数据库"}), config)
        _banner("最终报告（由第二个进程产出）")
        print(out.get("report"))
        metrics_table()
        print("\n  注：METRICS 是进程内变量，所以这里只统计到 stage2 自己的调用；")
        print("      要看全量 trace 应该接 Langfuse（第 11 周）—— checkpointer 管状态，trace 管过程。")


def run_prebuilt():
    """对照实验：langchain 1.x 的 create_agent —— 框架给你的最小 agent 长什么样"""
    _banner("对照：create_agent（框架版）vs 手写 tool loop（Day 32）vs 图（本文件）")
    try:
        from langchain.agents import create_agent
        from langchain_core.tools import tool
        from langchain_openai import ChatOpenAI
        import httpx
    except Exception as exc:
        print(f"  跳过：{type(exc).__name__}: {exc}")
        return

    @tool
    def list_files(pattern: str = "*") -> str:
        """列出数据目录下的文件，仅在排查系统问题时调用。"""
        return d32.list_files(pattern)

    @tool
    def read_log(path: str, lines: int = 20) -> str:
        """读日志文件最后 N 行。适合没有明确关键词时先看尾部概况。"""
        return d32.read_log(path, lines)

    @tool
    def search_log(path: str, keyword: str, max_hits: int = 5) -> str:
        """在日志里按关键词搜索。已知要找什么（如 ERROR）时优先用它。"""
        return d32.search_log(path, keyword, max_hits)

    model = ChatOpenAI(model=MODEL, base_url="https://api.deepseek.com",
                       api_key=os.environ.get("DEEPSEEK_API_KEY", ""), temperature=0,
                       http_client=httpx.Client(trust_env=False, timeout=120))
    agent = create_agent(model, tools=[list_files, read_log, search_log],
                         system_prompt="你是 SRE，先取证再下结论，中文回答，200 字内。")

    t0 = time.perf_counter()
    result = agent.invoke({"messages": [{"role": "user", "content": QUESTION}]})
    dt = time.perf_counter() - t0

    msgs = result["messages"]
    tool_calls = sum(len(getattr(m, "tool_calls", []) or []) for m in msgs)
    print(f"  消息数 {len(msgs)}（含 system/ai/tool），工具调用 {tool_calls} 次，耗时 {dt:.1f}s")
    print("  最终回答：")
    print("  " + str(msgs[-1].content)[:600].replace("\n", "\n  "))
    print("\n  ⚠️ 注意：docstring 就是 description —— Day 32 的结论在这里换了个落点（同一条杠杆）。")
    print("     这个 agent 的**轮数、工具选择、何时停**全由模型决定：灵活，但成本与顺序不可预测。")
    print("     对比本文件：哪一步由程序定，是**写在图里的**，可以审、可以改、可以画。")


def main():
    global _CLIENT
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--graph", action="store_true", help="只打印 mermaid 图，不调用模型")
    ap.add_argument("--stage1", action="store_true", help="跑到 confirm 暂停并退出")
    ap.add_argument("--stage2", action="store_true", help="新进程续跑同一 thread")
    ap.add_argument("--clean", action="store_true", help="用健康日志跑，验证程序网关分支")
    ap.add_argument("--prebuilt", action="store_true", help="对照 create_agent")
    ap.add_argument("--thread", default="diag-hitl", help="stage1/stage2 共用的 thread_id")
    args = ap.parse_args()

    if args.graph:
        app = build_graph(make_checkpointer("memory"))
        print(app.get_graph().draw_mermaid())
        return

    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("错误：.env 里没有 DEEPSEEK_API_KEY")
        return

    if args.prebuilt:
        _CLIENT = d32.make_client()
        run_prebuilt()
        return

    _CLIENT = d32.make_client()
    if args.stage1 or args.stage2:
        run_stage(1 if args.stage1 else 2, args.thread)
    else:
        run_single_process(clean=args.clean)


if __name__ == "__main__":
    main()
