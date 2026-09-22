#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""作品 3 · 第 11 周 —— **作品 3 可演示入口**：LangGraph 图 + MCP 工具（一个入口，两种工具后端）

════════════════════════════════════════════════════════════════════════
这一版把前几天的三块拼成一个能演示的东西：

    Day 32  工具层（读日志/搜日志/查磁盘 + 路径防护 + 回归测试）
    Day 33  MCP server（工具独立成进程，走 JSON-RPC）
    Day 34  编排模式（orchestrator-workers + 程序网关；"该由程序拿的确定性不让给模型"）
    Day 35  LangGraph 图（fan-out / barrier / interrupt 人机确认 / checkpointer 续跑）

**合成要证明的那件事：换工具通道，不改图。**

    ToolBox（协议）──┬── DirectTools  直接 import day32 的函数（同进程）
                     └── McpTools     走 MCP 协议（子进程 + JSON-RPC）

节点里只有一行不同：`await TOOLS.search_log(...)`。图、边、条件、中断点、checkpointer 全部不动。

════════════════════════════════════════════════════════════════════════
两个进程、两套依赖 —— 这是"进程 + 协议"解耦最实在的好处（也是本文件的一个发现）：

    编排进程  .venv-agent\\Scripts\\python.exe   langgraph 1.2.12 / langchain 1.4.2
    工具进程  .venv\\Scripts\\python.exe         mcp 2.2.0 / openai / dotenv

两者之间只认 JSON-RPC，谁也不 import 谁的包。代价是：**起进程有开销**（本文件会实测出来）。

════════════════════════════════════════════════════════════════════════
跑法（**用 .venv-agent 的解释器**，因为图跑在这儿）：

    $py = ".venv-agent\\Scripts\\python.exe"
    & $py day36_graph_mcp.py --graph                 # 只画图，不花钱
    & $py day36_graph_mcp.py --tools mcp             # ★ 默认：图 + MCP（完整链路，含人机确认）
    & $py day36_graph_mcp.py --tools direct          # 对照：图 + 直接函数调用
    & $py day36_graph_mcp.py --clean                 # 健康日志 → 网关 0 次调用收工
    & $py day36_graph_mcp.py --stage 1               # 跑到人机确认处暂停并退出
    & $py day36_graph_mcp.py --stage 2               # 新进程续跑同一 thread
"""
import os
import re
import sys
import json
import time
import uuid
import argparse
import asyncio
import operator
from typing import Annotated, TypedDict

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_HERE = os.path.dirname(os.path.abspath(__file__))

from dotenv import load_dotenv
load_dotenv(os.path.join(_HERE, ".env"))

import day32_function_calling as d32                       # 只给 DirectTools 用
import trace_util                                          # 追踪插座（本地 JSONL / Langfuse）
import budget as budget_mod                                # 运行时预算（刹车）
from langgraph.graph import StateGraph, START, END
from langgraph.types import Send, Command, interrupt
from langgraph.checkpoint.memory import InMemorySaver

MODEL = d32.MODEL
LOG_FILE = "sample_app.log"
CLEAN_LOG = "fixtures/quiet.log"
GATE_MIN_ERRORS = 3
CHECKPOINT_DB = os.path.join(_HERE, "checkpoints_mcp.sqlite")
QUESTION = "分析这份应用日志，判断根因并给出处置建议"

# MCP server 的解释器：项目目录的上一级就是那个装了 mcp 的老 venv
# （编排进程和工具进程可以有两套依赖 —— 这正是协议解耦的意义）
_SERVER_PYTHON = os.environ.get(
    "MCP_SERVER_PYTHON",
    os.path.join(os.path.dirname(_HERE), "Scripts", "python.exe"))

SIDE_EFFECT_ACTIONS = [
    "清理 /var/log/app 下 7 天前的旧日志以释放空间",
    "重启 app 服务（会中断在途请求）",
    "重启 postgresql 实例",
]


# ══════════════════════════════════════════════════════════════════
# 仪表：LLM 按节点计费；工具按"通道"计费（顺带量出 MCP 的协议开销）
# ══════════════════════════════════════════════════════════════════
LLM_METRICS = {}
TOOL_METRICS = {"calls": 0, "seconds": 0.0, "names": {}}
_CLIENT = None
RUN_ID = uuid.uuid4().hex[:6]        # 每个进程一个 run 号：跨进程续跑时靠它区分"哪一段是谁跑的"
TRACER = trace_util.NullTracer()     # 由 main() 按参数覆盖
# 运行时预算：CLI 与评测都会换一份新的（**每条用例/每次运行一份账**，不跨用例累计）
BUDGET = budget_mod.Budget(label="默认")


def llm(node, messages, json_mode=False):
    """**同步**的 LLM 调用 —— 在异步节点里必须用 asyncio.to_thread 包起来跑。

    为什么这点不能糊弄：异步事件循环里放同步阻塞调用，会把 4 个并行分支**变成串行** ——
    图看着是并行，实际排队。这是"用了 async 却没得到并发"最常见的写法。
    """
    m = LLM_METRICS.setdefault(node, {"calls": 0, "seconds": 0.0, "chars": 0,
                                      "tokens_in": 0, "tokens_out": 0})
    kwargs = {"model": MODEL, "messages": messages, "temperature": 0.0}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    prompt_chars = sum(len(x.get("content") or "") for x in messages)

    # ── 重试循环：**每次尝试都重新占一个预算名额** ──────────────────
    # 为什么不能省：重试是预算的隐形放大器。SDK 默认重试 2 次、且那两次不经过我们的账本 ——
    # 实测"给了 2s 超时、7.5s 才抛"。现在改成自己重试，于是每次尝试都被记一次账。
    for attempt in range(1, LLM_MAX_ATTEMPTS + 1):
        est, call_timeout = BUDGET.reserve_llm(prompt_chars, label=f"llm:{node}#{attempt}")
        settled, result = False, None
        # ⚠️ 预留必须**无条件释放**：并行分支里一个抛出后，兄弟任务会被 asyncio **取消**，
        #    而 CancelledError 继承 BaseException、**不是 Exception** —— `except Exception` 抓不到它，
        #    预留就会永久挂着（实测账本里出现 `934(预留)` 漏账）。
        #    这跟 C 里"资源获取后必须 goto cleanup / RAII"是同一件事：释放不能依赖正常路径。
        try:
            with TRACER.span(f"llm:{node}" + ("" if attempt == 1 else f"#重试{attempt - 1}"),
                             as_type="generation", model=MODEL, json_mode=json_mode,
                             prompt_chars=prompt_chars, attempt=attempt,
                             call_timeout_s=round(call_timeout, 1)) as sp:
                t0 = time.perf_counter()
                try:
                    # ★ 剩余预算就是**本次调用的超时上限**：在途的那一单也能被截断。
                    #   （SDK 重试已关，所以超时是真超时，不会偷偷 ×3）
                    resp = _CLIENT.chat.completions.create(**kwargs, timeout=call_timeout)
                except Exception as e:
                    name = type(e).__name__
                    can_retry = name in RETRYABLE and attempt < LLM_MAX_ATTEMPTS
                    sp.set(error=name, retry=can_retry,
                           elapsed_s=round(time.perf_counter() - t0, 1))
                    if can_retry:
                        print(f"    ↻ [{node}] 第 {attempt} 次失败（{name}），"
                              f"在预算内重试（剩余 {BUDGET.remaining_seconds():.1f}s）")
                    elif name in RETRYABLE:
                        raise budget_mod.CallTimeout(
                            f"[{node}] 调用失败且重试用尽（{name}，单次上限 "
                            f"{call_timeout:.0f}s）", BUDGET.snapshot()) from e
                    else:
                        raise
                else:
                    dt = time.perf_counter() - t0
                    choice = resp.choices[0]
                    text = choice.message.content or ""
                    usage = getattr(resp, "usage", None)
                    tokens_in = getattr(usage, "prompt_tokens", 0) or 0
                    tokens_out = getattr(usage, "completion_tokens", 0) or 0
                    sp.set(tokens_in=tokens_in, tokens_out=tokens_out,
                           out_chars=len(text), finish_reason=choice.finish_reason)
                    result = (text, tokens_in, tokens_out, dt, choice.finish_reason)

            if result is None:              # 本尝试失败但可重试 → 下一轮（finally 已退坑）
                continue
            text, tokens_in, tokens_out, dt, finish_reason = result
            BUDGET.settle_llm(est, tokens_in, tokens_out, dt)      # 结算：差额退回
            settled = True
            m["calls"] += 1
            m["seconds"] += dt
            m["chars"] += len(text)
            m["tokens_in"] += tokens_in
            m["tokens_out"] += tokens_out
            if finish_reason != "stop":                 # 静默错误守卫（Day 32）
                print(f"    ⚠️  [{node}] finish_reason={finish_reason}")
            return text
        finally:
            if not settled:                             # 取消 / 超时 / 任何异常：把坑退回去
                BUDGET.settle_llm(est, 0, 0, 0.0)


async def llm_async(node, messages, json_mode=False):
    return await asyncio.to_thread(llm, node, messages, json_mode)


async def llm_json_async(node, messages):
    raw = (await llm_async(node, messages, json_mode=True)).strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", raw, re.S)
    if fence:
        raw = fence.group(1).strip()
    s, e = raw.find("{"), raw.rfind("}")
    if s != -1 and e != -1:
        raw = raw[s:e + 1]
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"[{node}] 不是合法 JSON：{raw[:200]}") from exc


# ══════════════════════════════════════════════════════════════════
# 工具插座：同一个接口，两种后端 —— **这一步就是"合成"的全部**
# ══════════════════════════════════════════════════════════════════
class DirectTools:
    """后端 A：同进程直接调函数。快，但工具与编排**同一个依赖树**、同一个进程。"""

    name = "direct"

    async def search_log(self, path, keyword, max_hits=5):
        return d32.search_log(path, keyword, max_hits)

    async def read_log(self, path, lines=20):
        return d32.read_log(path, lines)

    async def list_files(self, pattern="*"):
        return d32.list_files(pattern)

    async def check_disk(self):
        return d32.check_disk()


class McpTools:
    """后端 B：走 MCP 协议（子进程 + JSON-RPC）。

    三个要点：
      1. `call_tool` 返回的是 CallToolResult，内容在 `result.content` 里，要自己拍平成文本
      2. `is_error=True` 是**服务端预料之中的失败**（Day 33：ToolError 会走这条路），
         消息原样带回来 —— 要把它当**结果**交给上层，别当异常炸掉（Day 32 坑表 #11）
      3. 同一个 ClientSession 上并发发请求是 SDK 支持的（JSON-RPC 有 id 区分），
         所以 4 个并行分支可以共用一个会话 —— 本文件实测验证了这一点
    """

    name = "mcp"

    def __init__(self, session):
        self.session = session

    @staticmethod
    def _flatten(result):
        return "\n".join(str(getattr(c, "text", c)) for c in result.content)

    async def _call(self, tool, args):
        res = await self.session.call_tool(tool, args)
        text = self._flatten(res)
        if getattr(res, "is_error", False):
            # 预料之中的失败：带信息返回（而不是抛），上层照样能拿到原因
            raise RuntimeError(f"[MCP 工具返回错误] {text}")
        return text

    async def search_log(self, path, keyword, max_hits=5):
        return await self._call("search_log", {"path": path, "keyword": keyword, "max_hits": max_hits})

    async def read_log(self, path, lines=20):
        return await self._call("read_log", {"path": path, "lines": lines})

    async def list_files(self, pattern="*"):
        return await self._call("list_files", {"pattern": pattern})

    async def check_disk(self):
        return await self._call("check_disk", {})


TOOLS = DirectTools()          # 由 main() 按 --tools 覆盖


TOOL_FAIL_PREFIX = "[工具执行失败]"
RETRYABLE = ("APITimeoutError", "APIConnectionError", "Timeout", "ReadTimeout", "InternalServerError")
LLM_MAX_ATTEMPTS = 2          # 1 次正常 + 1 次重试（**每一次都要占预算名额**）


def make_client(default_timeout=120.0):
    """自己造 client，和 day32 的配置一致，但**关掉 SDK 的自动重试**（`max_retries=0`）。

    为什么这条很关键（实测）：
      我给单次调用传 `timeout=2`，结果 **7.5s 才抛超时** —— 因为 openai SDK 默认重试 2 次，
      2s × 3 次 + 退避 ≈ 7.5s。落到 day36 里就是：按剩余预算给了 5.9s，实际跑了 **21.3s**。
    ➡️ 两个后果：① "单次调用超时"变成"最多 3 倍时间"，直接击穿墙钟预算；
                 ② **这三次重试不经过我们的预算账本** —— 是账本外的隐形放大器。
    所以：SDK 层关掉重试，重试**由我们自己写**（每次重试都重新 reserve 一个名额）。
    """
    import httpx
    from openai import OpenAI
    return OpenAI(
        base_url="https://api.deepseek.com",
        api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
        http_client=httpx.Client(trust_env=False, timeout=default_timeout),
        max_retries=0,                     # ★ 见 docstring
        timeout=default_timeout,
    )



async def tool_call(name, **kwargs):
    """所有工具调用的唯一出口：计次 + 计时（把 MCP 的协议开销量出来）+ 留一个 span。

    ⚠️ **工具失败不能抛出去**（Day 32 坑表 #11）：抛出去 = 图当场崩、模型没机会纠错。
    这里统一转成一条**带信息的结果字符串**往下走，让上层决定"是重试、是报告、还是收工"。

    预算检查放在最前面：连工具调用次数也要有上限（一次诊断可能搜十几个关键词）。
    """
    BUDGET.reserve_tool(name)
    with TRACER.span(f"tool:{name}", as_type="tool", channel=TOOLS.name,
                     args=str(kwargs)[:120]) as sp:
        t0 = time.perf_counter()
        try:
            text = await getattr(TOOLS, name)(**kwargs)
        except Exception as e:
            text = f"{TOOL_FAIL_PREFIX} {type(e).__name__}: {e}"
            sp.set(error=type(e).__name__)
            TRACER.event("tool_error", tool=name, kind=type(e).__name__, args=str(kwargs)[:80])
        dt = time.perf_counter() - t0
        sp.set(result_chars=len(text))
    TOOL_METRICS["calls"] += 1
    TOOL_METRICS["seconds"] += dt
    TOOL_METRICS["names"][name] = TOOL_METRICS["names"].get(name, 0) + 1
    return text


# ══════════════════════════════════════════════════════════════════
# 状态与图：和 Day 35 完全一样 —— 这就是"换通道不改图"的字面意思
# ══════════════════════════════════════════════════════════════════
class DiagState(TypedDict, total=False):
    question: str
    log_file: str
    error_count: int          # scan 写的：程序数出来的，不是模型说的（-1 = 读不到文件）
    scan_error: str           # 读不到日志时的原因 —— **失败要显式记录，不能伪装成"0 条异常"**
    plan: list
    findings: Annotated[list, operator.add]
    decision: dict
    report: str


class WorkerState(TypedDict):
    question: str
    log_file: str
    task: dict


async def scan(state: DiagState):
    """程序网关：数 ERROR 行数。数数用的是**工具**，但判定走不走由代码决定。

    ⚠️ 这里有个容易写错的判据：**"读不到文件"和"没有异常"是两回事**。
    工具失败时绝不能数出 0 条然后说"未见异常" —— 那是**假阴性**，
    比报错更危险（运维会以为系统健康）。所以失败时置 -1 并显式记录原因。
    """
    log = state.get("log_file", LOG_FILE)
    raw = await tool_call("search_log", path=log, keyword="ERROR", max_hits=500)
    if raw.startswith(TOOL_FAIL_PREFIX):
        print(f"  [scan/{TOOLS.name}] ❌ 读不到日志：{raw}")
        TRACER.event("gate", error_count=-1, failed=True, reason=raw[:120])
        return {"error_count": -1, "scan_error": raw}
    count = len([ln for ln in raw.splitlines() if re.match(r"^\d+:", ln)])
    print(f"  [scan/{TOOLS.name}] 程序数出 ERROR 行数 = {count}（阈值 {GATE_MIN_ERRORS}）")
    TRACER.event("gate", error_count=count, threshold=GATE_MIN_ERRORS,
                 passed=count >= GATE_MIN_ERRORS)
    return {"error_count": count}


def route_after_scan(state: DiagState):
    """条件边：走 plan 还是直接收工 —— 判据在**代码**里，模型没有投票权。"""
    if state.get("error_count", 0) < 0:      # 读不到 → 不猜，直接收工
        return "end"
    return "plan" if state["error_count"] >= GATE_MIN_ERRORS else "end"


async def plan(state: DiagState):
    data = await llm_json_async("plan", [
        {"role": "system", "content": "你是诊断任务的编排者。只输出 JSON，不要自己回答。"},
        {"role": "user", "content":
            f'把「{state["question"]}」拆成 2-4 个可独立验证的子任务，输出 JSON：'
            f'{{"subtasks": [{{"id": 1, "goal": "...", "keyword": "去日志里搜的关键词"}}]}}'},
    ])
    subtasks = data.get("subtasks", [])[:4]
    print(f"  [plan] 拆出 {len(subtasks)} 个子任务：{[t.get('keyword') for t in subtasks]}")
    return {"plan": subtasks}


def fan_out(state: DiagState):
    return [Send("investigate", {"question": state["question"],
                                 "log_file": state.get("log_file", LOG_FILE),
                                 "task": t}) for t in state["plan"]]


_BRANCH_SPAN = {}          # 分支重叠的证据：起止时刻都记下来


async def investigate(state: WorkerState):
    task = state["task"]
    keyword = task.get("keyword", "")
    t0 = time.perf_counter()
    # 证据由程序取（工具通道），模型只做归纳 —— Day 34 的核心结论
    evidence = await tool_call("search_log", path=state["log_file"], keyword=keyword, max_hits=6)
    conclusion = await llm_async("investigate", [
        {"role": "system", "content": "你是 SRE，只依据给定证据回答，证据不足就直说。中文，80 字内。"},
        {"role": "user", "content":
            f"子任务：{task.get('goal')}\n证据（search_log('{keyword}')）：\n{evidence}"},
    ])
    _BRANCH_SPAN[keyword] = (t0, time.perf_counter())
    return {"findings": [{"id": task.get("id"), "goal": task.get("goal"),
                          "keyword": keyword, "conclusion": conclusion}]}


_CONFIRM_RUNS = 0


async def confirm(state: DiagState):
    """人机确认：`interrupt()` 之前**不要**放副作用 —— resume 时节点会从头重跑。"""
    global _CONFIRM_RUNS
    _CONFIRM_RUNS += 1
    print(f"  [confirm] 节点第 {_CONFIRM_RUNS} 次进入")
    TRACER.event("interrupt", confirm_runs=_CONFIRM_RUNS,
                 proposed_actions=len(SIDE_EFFECT_ACTIONS))
    decision = interrupt({
        "ask": "以下处置动作带有副作用，需要你批准",
        "evidence": [f.get("conclusion", "") for f in state.get("findings", [])],
        "proposed_actions": SIDE_EFFECT_ACTIONS,
    })
    print(f"  [confirm] 人的决定：{decision}")
    TRACER.event("decision", approved=decision.get("approved") if isinstance(decision, dict) else decision,
                 note=(decision or {}).get("note") if isinstance(decision, dict) else None)
    return {"decision": decision}


def route_after_confirm(state: DiagState):
    return "report" if (state.get("decision") or {}).get("approved") else "end"


async def report(state: DiagState):
    decision = state.get("decision") or {}
    bundle = "\n".join(f"- [{f.get('keyword')}] {f.get('conclusion')}" for f in state["findings"])
    text = await llm_async("report", [
        {"role": "system", "content": "你是 SRE 主管，输出中文，250 字内，含根因、处置、局限。"},
        {"role": "user", "content":
            f"子任务结论：\n{bundle}\n\n"
            f"已获人工批准的动作：{json.dumps(decision.get('approved', []), ensure_ascii=False)}\n"
            f"人的备注：{decision.get('note', '')}\n\n"
            f"请给最终报告，并明确哪些动作尚未获批执行。"},
    ])
    return {"report": text}


def build_graph(checkpointer):
    """★ 注意：除了外面套的 traced_node（观测是横切的，不侵入节点体），
    这张图和 Day 35 **逐行相同** —— 工具通道换了、追踪开了，图一个字没改。"""
    b = StateGraph(DiagState)
    wrap = trace_util.traced_node
    b.add_node("scan", wrap(TRACER, "scan", scan))
    b.add_node("plan", wrap(TRACER, "plan", plan))
    # investigate 是 Send 并行分支：把关键字记进 span，报告里才看得出"哪一支慢"
    b.add_node("investigate", wrap(TRACER, "investigate", investigate,
                                   attrs_fn=lambda s: {"keyword": (s.get("task") or {}).get("keyword")}))
    b.add_node("confirm", wrap(TRACER, "confirm", confirm))
    b.add_node("report", wrap(TRACER, "report", report))
    b.add_edge(START, "scan")
    b.add_conditional_edges("scan", route_after_scan, {"plan": "plan", "end": END})
    b.add_conditional_edges("plan", fan_out, ["investigate"])
    b.add_edge("investigate", "confirm")
    b.add_conditional_edges("confirm", route_after_confirm, {"report": "report", "end": END})
    b.add_edge("report", END)
    return b.compile(checkpointer=checkpointer)


# ══════════════════════════════════════════════════════════════════
# 跑法
# ══════════════════════════════════════════════════════════════════
def _banner(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def show_interrupt(out):
    intrs = out.get("__interrupt__") or ()
    for i in intrs:
        print("  ⏸  图停在 confirm，等人审批：")
        print("     " + json.dumps(getattr(i, "value", i), ensure_ascii=False,
                                  indent=2).replace("\n", "\n     "))
    return bool(intrs)


async def show_state(app, config):
    # 异步 checkpointer 下必须用 aget_state（同步版会直接报错告诉你换异步 —— 实测）
    snap = await app.aget_state(config)
    print(f"  状态快照：next={snap.next}  键={sorted(k for k in snap.values if not k.startswith('__'))}")
    return snap


def metrics_table():
    print("\n" + "-" * 78)
    print(f"{'节点':<14}{'LLM 调用':>9}{'LLM 耗时':>11}{'输出字':>9}")
    print("-" * 78)
    for node, m in LLM_METRICS.items():
        print(f"{node:<14}{m['calls']:>9}{m['seconds']:>10.1f}s{m['chars']:>9}")
    total = sum(m["calls"] for m in LLM_METRICS.values())
    print("-" * 78)
    print(f"合计 {total} 次 LLM 调用")

    if TOOL_METRICS["calls"]:
        avg = TOOL_METRICS["seconds"] / TOOL_METRICS["calls"] * 1000
        print(f"工具通道 = {TOOLS.name}：{TOOL_METRICS['calls']} 次调用，"
              f"合计 {TOOL_METRICS['seconds'] * 1000:.0f} ms，平均 **{avg:.1f} ms/次**"
              f"（{TOOL_METRICS['names']}）")

    if _BRANCH_SPAN:
        t_base = min(t0 for t0, _ in _BRANCH_SPAN.values())
        lens = [(k, t1 - t0) for k, (t0, t1) in _BRANCH_SPAN.items()]
        span = max(t1 for _, t1 in _BRANCH_SPAN.values()) - t_base
        total = sum(l for _, l in lens)
        slowest = max(l for _, l in lens)
        # 判据要用**最慢的那一支**比：全并行时跨度 ≈ 最慢分支；
        # 串行时跨度 ≈ 各分支之和。拿 span/total 比会在"一个分支特别慢"时误判。
        verdict = ("**确实并行**" if span <= slowest * 1.10
                   else f"⚠️ 串行/部分串行（跨度 {span:.2f}s 明显超过最慢分支 {slowest:.2f}s）")
        print(f"并行分支：{len(lens)} 个分支合计 {total:.2f}s，最慢一支 {slowest:.2f}s，"
              f"实际跨度 {span:.2f}s → {verdict}")
        print("           每支：" + "，".join(f"{k}={l:.2f}s" for k, l in lens))


async def degrade(app, config, exc):
    """**降级报告**：预算刹车之后，用已有证据出一份"没跑完的交代"。

    三条设计要点：
      1. 报告由**程序模板**拼装，**刹车之后不再调用模型** —— 否则刹车本身就漏了
      2. 证据从 **checkpointer 的状态快照**里取（`aget_state`）—— 这正是"落盘"的第二次收益：
         跨进程/中途失败都还能把已经拿到的部分交出去
      3. 把降级报告**写回状态**（`aupdate_state`）—— 之后任何进程查看这个 thread 都能看到
         "这次为什么没跑完"，而不是一只停在 confirm 的空壳
    """
    snap = await app.aget_state(config)
    values = getattr(snap, "values", {}) or {}
    findings = values.get("findings") or []
    plan = values.get("plan") or []
    tools_used = exc.snapshot.get("tool_calls", "?")
    lines = [f"【降级报告 · 预算刹车】{exc.reason}", ""]
    if findings:
        lines.append(f"已收集到的结论（{len(findings)}/{len(plan) or len(findings)} 条子任务）：")
        for f in findings:
            lines.append(f"  · [{f.get('keyword')}] {f.get('conclusion')}")
    elif plan:
        # 说清"取证发生了、只是归纳被拦下" —— 不然读者会以为什么都没做
        lines.append(f"已收集到的结论：0/{len(plan)} 条 —— 工具已取证 {tools_used} 次，"
                     f"但归纳调用被预算拦下，**结论未形成**。")
    else:
        lines.append("已收集到的结论：无（刹车发生在规划之前）。")
    lines += ["", "未完成：归因汇总与人机确认（预算不足，未执行）。",
              "", BUDGET.report(),
              "说明：本报告由程序模板生成 —— **刹车之后不再调用模型**，否则刹车本身就是个漏洞。"]
    report = "\n".join(lines)

    TRACER.event("budget_exceeded", reason=exc.reason[:160],
                 kind=type(exc).__name__, **{k: v for k, v in exc.snapshot.items()
                                             if k not in ("blocked",)})
    try:
        await app.aupdate_state(config, {"report": report})     # 写回：让后来的人看到原因
    except Exception as e:
        print(f"  （降级报告写回状态失败，不影响输出：{type(e).__name__}）")
    _banner("⚠️ 预算刹车 —— 降级报告（未调用模型）")
    print(report)
    return report


async def drive(app, config, approve, stage):
    """执行：stage=1 停在人机确认；stage=2 续跑；stage=0 一条龙。

    整段套一个根 span（diag-run）：这样 trace 里所有节点/LLM/工具都挂在同一个盒子下面，
    跨进程时也是"同一个 trace、两个 run"—— 一眼看出断点前后各发生了什么。
    """
    try:
        with TRACER.span("diag-run", stage=stage, thread=config["configurable"]["thread_id"]):
            await _drive(app, config, approve, stage)
    except budget_mod.BudgetExceeded as e:
        await degrade(app, config, e)          # 两种刹车（拒下一单 / 截断在途）都走这里
    if LLM_METRICS or TOOL_METRICS["calls"]:   # 有数据就打（关掉追踪也要能看）
        metrics_table()
    print("\n" + "-" * 78)
    print("预算账本（刹车状态也是可观测的一部分，不能只看到「跑得慢」）：")
    print("  " + BUDGET.report().replace("\n", "\n  "))


async def _drive(app, config, approve, stage):
    if stage == 2:
        snap = await show_state(app, config)
        if "confirm" not in (snap.next or ()):
            print("  这个 thread 没停在 confirm（先跑 --stage 1）。")
            return
        out = await app.ainvoke(Command(resume=approve), config)
        _banner("最终报告（第二个进程产出）")
        print(out.get("report"))
        return

    payload = {"question": QUESTION, "log_file": _LOG_FILE, "findings": []}
    out = await app.ainvoke(payload, config)

    if not show_interrupt(out):
        _banner("图直接跑完（没停）—— scan 网关把它挡在门外了")
        if out.get("scan_error"):
            print(f"  ❌ 无法读取日志：{out['scan_error']}")
            print("     （**读不到 ≠ 没有异常**：这里必须显式报告失败，不能报「系统健康」）")
        else:
            print(f"  error_count={out.get('error_count')} 条 ≤ 阈值 {GATE_MIN_ERRORS} → 未见异常")
        return

    await show_state(app, config)
    if stage == 1:
        print("\n  进程退出。图和状态已在 checkpoints_mcp.sqlite 里 ——")
        print("  接着跑：--stage 2")
        return

    print(f"\n  ▶ 人审批（{'自动' if approve else '手动'}）：{approve}")
    out = await app.ainvoke(Command(resume=approve), config)
    _banner("最终报告")
    print(out.get("report"))
    print(f"\n  [confirm] 节点共执行 {_CONFIRM_RUNS} 次（>1 是正常的：resume 会重跑节点）")


def reset_metrics():
    """清空进程内指标 —— 给评测用。

    连续跑多个 case 时必须隔离，否则数据互相污染（这也是"指标是模块级 dict"带来的可测性：
    外部能清空它；若写成函数内局部变量，评测就插不进来了 —— Day 32 #4「可测性的根源是接缝」）。
    """
    global _CONFIRM_RUNS
    LLM_METRICS.clear()
    TOOL_METRICS.update({"calls": 0, "seconds": 0.0, "names": {}})
    _BRANCH_SPAN.clear()
    _CONFIRM_RUNS = 0


async def diagnose(app, config, question, log_file, approve=None):
    """headless 跑一次诊断 —— **CLI 与评测共用同一条路径**。

    为什么这一点重要：评测脚本若自己写一套调用逻辑，你测的就不是上线的那套
    （"评测跑了 A、上线跑的是 B"是评测里最常见的假绿）。
    返回 {"interrupted": bool, "final": state, "degraded": bool}。
    """
    try:
        out = await app.ainvoke({"question": question, "log_file": log_file, "findings": []}, config)
        interrupted = bool(out.get("__interrupt__"))
        if interrupted:
            out = await app.ainvoke(
                Command(resume=approve or {"approved": SIDE_EFFECT_ACTIONS[:1],
                                           "note": "评测自动批准（只读动作）"}), config)
        return {"interrupted": interrupted, "final": out, "degraded": False}
    except budget_mod.BudgetExceeded as e:
        report = await degrade(app, config, e)
        return {"interrupted": False, "final": {"report": report}, "degraded": True,
                "budget": e.snapshot}


async def run(args):
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    config = {"configurable": {"thread_id": args.thread}}
    # 用 Langfuse 后端时，顺手把 LangChain/LangGraph 的官方回调也挂上：
    # 这样框架内部的步骤（节点进出、消息流转）由它自己上报，不用我们逐个埋点。
    if TRACER.backend == "langfuse":
        try:
            from langfuse.langchain import CallbackHandler
            config["callbacks"] = [CallbackHandler()]
            print("  [trace] 已挂 LangChain 回调（langfuse.langchain.CallbackHandler）")
        except Exception as e:
            print(f"  [trace] LangChain 回调挂载失败（不影响主流程）：{type(e).__name__}: {e}")
    approve = {"approved": SIDE_EFFECT_ACTIONS[:2],
               "note": "批准清日志 + 重启 app，不动数据库"}
    if args.stage == 1:
        approve = None

    async with AsyncSqliteSaver.from_conn_string(CHECKPOINT_DB) as saver:
        app = build_graph(saver)
        # 注意：这里必须按**参数**判断，不能看 TOOLS.name ——
        # 此刻 TOOLS 还是模块默认值（DirectTools），照它判断会永远走 else 分支。
        if args.tools == "mcp":
            from mcp import ClientSession
            from mcp.client.stdio import StdioServerParameters, stdio_client
            params = StdioServerParameters(command=_SERVER_PYTHON, args=[os.path.join(_HERE, "server.py")])
            print(f"  启动 MCP server：{_SERVER_PYTHON} server.py（子进程）")
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()                     # 握手：换协议版本与能力
                    tools = await session.list_tools()
                    print(f"  握手完成：服务端暴露工具 {[t.name for t in tools.tools]}")
                    globals()["TOOLS"] = McpTools(session)
                    await drive(app, config, approve, args.stage)
        else:
            globals()["TOOLS"] = DirectTools()
            await drive(app, config, approve, args.stage)


_LOG_FILE = LOG_FILE


def main():
    global _CLIENT, _LOG_FILE, TRACER, BUDGET
    ap = argparse.ArgumentParser()
    ap.add_argument("--tools", choices=["direct", "mcp"], default="mcp",
                    help="工具通道：direct=同进程；mcp=走协议（默认）")
    ap.add_argument("--clean", action="store_true", help="用健康日志，验证程序网关")
    ap.add_argument("--graph", action="store_true", help="只打印 mermaid 图，不花钱")
    ap.add_argument("--stage", type=int, choices=[0, 1, 2], default=0,
                    help="0=一条龙；1=停在人机确认；2=续跑")
    ap.add_argument("--thread", default=None,
                    help="thread_id（checkpointer 的'卷宗编号'）。**复用同一 thread 会继承历史状态**，"
                         "所以普通运行默认每次自动新开一单；--stage 1/2 必须显式传同一个名字")
    ap.add_argument("--no-trace", action="store_true", help="关掉追踪（不做任何埋点）")
    ap.add_argument("--trace-report", nargs="?", const="", default=None, metavar="THREAD",
                    help="只打印追踪报告（读 traces/<thread>.jsonl），不调用模型；不带名字则取最新的")
    ap.add_argument("--max-calls", type=int, default=10, help="运行时预算：总 LLM 调用次数上限")
    ap.add_argument("--max-tokens", type=int, default=20000, help="运行时预算：总 token 上限（预估+结算）")
    ap.add_argument("--max-tools", type=int, default=12, help="运行时预算：总工具调用次数上限")
    ap.add_argument("--max-seconds", type=float, default=240.0,
                    help="运行时预算：墙钟总时长上限（同时作为单次调用的超时上限）")
    args = ap.parse_args()

    if args.graph:
        print(build_graph(InMemorySaver()).get_graph().draw_mermaid())
        return

    if args.trace_report is not None:
        if args.trace_report:
            path = os.path.join(trace_util.TRACE_DIR, f"{args.trace_report}.jsonl")
        else:
            files = [os.path.join(trace_util.TRACE_DIR, f)
                     for f in os.listdir(trace_util.TRACE_DIR)
                     if f.endswith(".jsonl")] if os.path.isdir(trace_util.TRACE_DIR) else []
            if not files:
                print("traces/ 下还没有 .jsonl —— 先跑一次（默认就开着追踪）")
                return
            path = max(files, key=os.path.getmtime)
        trace_util.report(path)
        return

    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("错误：.env 里没有 DEEPSEEK_API_KEY")
        return
    if args.tools == "mcp" and not os.path.exists(_SERVER_PYTHON):
        print(f"错误：找不到 MCP server 的解释器 {_SERVER_PYTHON}（可用环境变量 MCP_SERVER_PYTHON 指定）")
        return

    _LOG_FILE = CLEAN_LOG if args.clean else LOG_FILE
    _CLIENT = make_client()
    # 普通运行：每次新开一个 thread（否则上一次的 report/decision 会被当成"已有状态"继承进来）
    # stage 1/2：必须共用同一个 thread，那是"同一个案子的两次到场"
    if args.thread:
        thread = args.thread
    elif args.stage in (1, 2):
        thread = "works3-hitl"
    else:
        thread = f"works3-{int(time.time())}"
    args.thread = thread

    TRACER = (trace_util.NullTracer(thread, RUN_ID) if args.no_trace
              else trace_util.make_tracer(thread, RUN_ID))
    BUDGET = budget_mod.Budget(max_llm_calls=args.max_calls, max_tokens=args.max_tokens,
                               max_tool_calls=args.max_tools, max_seconds=args.max_seconds,
                               label=f"{args.tools}/{thread}")
    _banner(f"作品 3 · 图 + {args.tools.upper()} 工具"
            f"（日志 = {_LOG_FILE}，thread = {thread}，run = {RUN_ID}）")
    print(f"  预算：LLM ≤ {args.max_calls} 次 / 工具 ≤ {args.max_tools} 次 / "
          f"token ≤ {args.max_tokens} / 墙钟 ≤ {args.max_seconds:.0f}s"
          f"（单次调用超时 = 剩余预算，最大 120s）")
    asyncio.run(run(args))

    TRACER.flush()
    if getattr(TRACER, "path", None):
        print(f"\n  [trace] 轨迹文件：{TRACER.path}")
        print(f"  [trace] 看报告：--trace-report {thread}")
    url = TRACER.trace_url()
    if url:
        print(f"  [trace] Langfuse：{url}")


if __name__ == "__main__":
    main()
