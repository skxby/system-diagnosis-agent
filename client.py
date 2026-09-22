#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""作品 3 · Day 33 —— 最小 MCP 客户端：连上 server.py，列工具、调用、验沙箱

补的是「会造 ≠ 会消费」的另一半。链路：

    客户端进程 ──spawn──> python server.py（子进程）
               ──stdin/stdout──> JSON-RPC 协议消息
               握手 → 列工具 → 调用 → 收结果

对照你的嵌入式经验：这就是「起一个进程 + 走一条协议」，
只不过协议是 JSON-RPC，物理层是子进程的 stdin/stdout。

**为什么必须用 async**：MCP 的流天生是异步的——服务端要同时等消息、等客户端、
等工具跑完。SDK 只给异步 API，所以客户端也必须在事件循环里。

**为什么要 import day32 / server 两处都不依赖 cwd**：
客户端可能在任意工作目录下被启动（比如被 Claude Desktop 拉起），
所以服务端路径要自己算，不能靠 cwd。

跑法：
    python client.py
"""
import asyncio
import os
import sys

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

_HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(_HERE, "server.py")


def _dump(res):
    """把 CallToolResult 的内容拍平成人能读的样子"""
    text = "\n".join(str(getattr(c, "text", c)) for c in res.content)
    return f"is_error={res.is_error}\n{text}"


async def main():
    # 1) sys.executable 而不是写死 "python"：保证子进程用当前这个 venv 的解释器，
    #    否则子进程可能根本 import 不到 mcp。
    # 2) 绝对路径：不依赖 cwd。
    params = StdioServerParameters(command=sys.executable, args=[SERVER])

    # ── 两层 async with：本课的技术门槛 ──────────────────────
    # 别去背，理解成「资源的生命周期」，跟你写 C 时 open/close 配对是一回事：
    #   外层 stdio_client：进来时 spawn 子进程 + 建管道，出去时把它关掉、等它退出
    #   内层 ClientSession：进来时握手，出去时礼貌关闭会话
    # 之所以是 async 版，是因为「启动子进程」「关闭并回收」本身就是异步操作。
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()          # 握手：交换协议版本与能力

            print("=" * 62)
            print("[1] 服务端暴露的工具 —— 描述就是 server.py 里的 docstring")
            print("=" * 62)
            tools = await session.list_tools()
            for t in tools.tools:
                first = (t.description or "").strip().splitlines()[0]
                print(f"  · {t.name:12} {first}")

            print()
            print("=" * 62)
            print("[2] 资源（和工具不是一个原语：资源是客户端主动读）")
            print("=" * 62)
            rsrc = await session.list_resources()
            for r in rsrc.resources:
                print(f"  · {r.uri}  {r.name or ''}")

            print()
            print("=" * 62)
            print("[3] 正常调用 read_log")
            print("=" * 62)
            res = await session.call_tool("read_log", {"path": "sample_app.log", "lines": 3})
            print(_dump(res))

            print()
            print("=" * 62)
            print("[4] 越界调用 ../.env —— 沙箱必须在真实协议链路上也拦住")
            print("    （这一步同时回答那个悬念：ToolError 走协议时是")
            print("     变成 isError=True，还是直接抛异常？两种都打印出来看）")
            print("=" * 62)
            try:
                res = await session.call_tool("read_log", {"path": "../.env"})
                print("  → 没抛异常。返回结果：")
                print("  " + _dump(res).replace("\n", "\n  "))
            except Exception as e:
                print(f"  → 抛异常了：{type(e).__name__}: {e}")

            print()
            print("=" * 62)
            print("[5] 调一个没参数的工具 check_disk")
            print("=" * 62)
            res = await session.call_tool("check_disk", {})
            print(_dump(res))


if __name__ == "__main__":
    asyncio.run(main())
