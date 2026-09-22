#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""作品 3 · 第 9 周 Day 33 —— 把 Day 32 的诊断工具包成 MCP server

三条设计要点（比代码重要）：

  1. **import 复用，不复制**
     工具实现和沙箱校验全部来自 day32_function_calling。复制一份等于开一个
     没有测试保护的分叉 —— 这里 import 的话，test_day32.py 那 24 项继续替
     这个 server 站岗。

  2. **docstring 就是工具描述**
     MCP 里函数的 docstring 会原样成为模型看到的 description。
     Day 32 的结论「description 就是 prompt」在这里换了个落点，杠杆没变。
     （也可以用 @mcp.tool(description=...) 显式覆盖 docstring。）

  3. **stdout 是协议通道**
     stdio 传输下，任何 print() 都会往协议流里掺垃圾，客户端直接解析失败。
     日志一律走 stderr。day32 的 execute_tool 里那行 print 就是雷 ——
     所以这里只 import 工具函数，绝不 import execute_tool。

跑法：
    python server.py            # stdio 传输，等客户端来连
    mcp dev server.py           # 用 Inspector 调试（需 Node 环境）

看已注册的工具（不启服务、不需要 Node）：
    python -c "import asyncio, server; print([t.name for t in asyncio.run(server.mcp.list_tools())])"
"""
import os

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

import day32_function_calling as d32
import collect as collect_mod                      # 真机取证（只读）

mcp = MCPServer(
    name="sysdiag",
    title="系统诊断工具",
    instructions=(
        "只读的系统诊断工具集：列出可读的日志文件、读日志尾部、按关键词搜索日志、"
        "查看磁盘占用。不修改任何文件。所有路径都限制在数据目录内，越界会被拒绝。"
    ),
)


# ══════════════════════════════════════════════════
# 错误处理：统一把「预料之中的失败」转成 ToolError
# ══════════════════════════════════════════════════
def _safe_call(fn, *args, **kwargs):
    """调底层工具，把该给模型看到的信息包成 ToolError。

    **这是本文件最容易写错的一处，因为 SDK 把错误分两类，处理方式天差地别**：

      ToolError（我预料到了）→ 服务端打一行 INFO、**无 traceback**；
                               模型收到 is_error=True + 我写的这条消息
      其他任何异常（崩了）    → 被 SDK 包成 UnexpectedToolError、**打 traceback**；
                               模型只收到一句 "Error executing tool X" —— 没有任何信息

    所以 catch 的宽窄直接决定模型能不能自我纠正。
    「把错误当结果喂回模型」只说了一半 —— **后半句是这条消息必须带足够信息**。
    （第一版 read_log 只 catch ValueError，FileNotFoundError 就逃了出去，
     模型拿到一句空消息、没法改参数重试。Inspector 里一眼看出来了。）
    """
    try:
        return fn(*args, **kwargs)
    except ValueError as e:
        # 路径越界 / lines 非法 —— 参数错了，原样告知
        raise ToolError(str(e)) from e
    except FileNotFoundError as e:
        # 最常见的失败。只说「文件不存在」模型还是不知道该改成什么，
        # 把它能用的文件名一并给出 —— 这是「工具是 Agent 的眼睛」用在报错上。
        # 注意不显式回显绝对路径（那是服务端内部信息）。
        names = sorted(os.listdir(d32.ALLOWED_ROOT))
        raise ToolError(f"文件不存在。数据目录下现有：{', '.join(names)}") from e
    except OSError as e:
        # 权限不足、是个目录、磁盘错误等
        raise ToolError(f"读取失败（{type(e).__name__}）：{e}") from e


# ══════════════════════════════════════════════════
# 工具 1 / 样例：无参 + 有默认值的参数
# ══════════════════════════════════════════════════
@mcp.tool()
def list_files(pattern: str = "*") -> str:
    """列出数据目录下可读的文件，支持通配符（如 *.log）。

    排查问题的第一步：先看有哪些日志可读，再决定读哪个。
    没有这个工具，就只能猜文件名。
    """
    return _safe_call(d32.list_files, pattern)


# ══════════════════════════════════════════════════
# 工具 2 / 样例：带路径参数（要处理越界）
# ══════════════════════════════════════════════════
@mcp.tool()
def read_log(path: str, lines: int = 20) -> str:
    """读日志文件的最后 N 行，返回带行号的内容。

    适合还没有明确关键词时，先看日志尾部概况。
    path 是相对数据目录的路径，例如 sample_app.log。
    """
    return _safe_call(d32.read_log, path, lines)


# ══════════════════════════════════════════════════
# 工具 3 / 留给你补
# ══════════════════════════════════════════════════
@mcp.tool()
def search_log(path: str, keyword: str, max_hits: int = 5) -> str:
    """在日志里按关键词搜索，返回命中行与行号。

    适合在有明确的路径和明确的搜索要求时，直接对相应的日志进行搜索。
    path 用相对数据目录的路径，如 sample_app.log
    keyword是要搜索的关键词，如 ERROR、磁盘
    max_hits是最多返回几条，默认 5
    """
    return _safe_call(d32.search_log, path, keyword, max_hits)


# ══════════════════════════════════════════════════
# 工具 4 / 留给你补
# ══════════════════════════════════════════════════
@mcp.tool()
def read_log_range(path: str, start: int, end: int = 0) -> str:
    """按行号区间读日志，一次最多 400 行。

    不知道怎么搜关键词时，先取一段看看 —— 取证是按需的，不要预加载整份日志。
    end 省略时返回 start 起的 50 行。
    """
    return _safe_call(d32.read_log_range, path, start, end or None)


@mcp.tool()
def system_snapshot() -> str:
    """对**运行本工具的主机**做一次只读体检：磁盘（含 inode）、内存、负载、温度、
    OOM 计数、fd 用量、TIME_WAIT、PSI 压力、cgroup 限额。

    纯只读、不起子进程、不改系统状态。平台缺的维度会明确标注"未采集到"——
    看不到就说看不到，不会假装健康。返回体检文本 + 确定性规则发现。
    """
    snap = collect_mod.snapshot()
    return collect_mod.render(snap, collect_mod.evaluate(snap))


@mcp.tool()
def top_files_by_size(top_n: int = 5, min_mb: int = 1) -> str:
    """找出数据目录里最大的几个文件 —— 用来回答"是哪个文件把盘吃满的"。

    只允许扫数据目录（沙箱内），遍历有条目上限与深度上限，权限失败静默跳过。
    """
    info = collect_mod.top_files_by_size(d32.ALLOWED_ROOT, top_n=int(top_n), min_mb=int(min_mb))
    if not info["top"]:
        return f"数据目录下没有超过 {min_mb} MB 的文件（已扫描 {info['scanned']} 个文件）"
    lines = [f"{f['mb']:>8.1f} MB  {f['path']}" for f in info["top"]]
    return f"数据目录里最大的 {len(lines)} 个文件：\n" + "\n".join(lines)


@mcp.tool()
def check_disk() -> str:
    """查看数据目录所在磁盘的占用情况。
    """
    return _safe_call(d32.check_disk)


# ══════════════════════════════════════════════════
# 资源（了解三个原语的区别用，可删）
# ══════════════════════════════════════════════════
@mcp.resource("sysdiag://logs")
def readable_logs() -> str:
    """数据目录下所有 .log 文件，每行一个。

    资源 vs 工具的区别：
      工具 = 模型自己决定调（它看 description 选）
      资源 = 客户端主动读（像读文件，模型不参与决策）
    这个区别面试常问。
    """
    return d32.list_files("*.log")


if __name__ == "__main__":
    mcp.run()          # 默认 transport='stdio'
