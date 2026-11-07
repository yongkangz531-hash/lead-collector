#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""极简 CDP 客户端：连上本机 Chrome（--remote-debugging-port=9333），
在当前页面执行 JS 并打印结果。

用法：
  uv run --with websockets cdp_eval.py "document.title"
  uv run --with websockets cdp_eval.py --goto "https://..." "document.title"
"""
import asyncio, json, sys, urllib.request

import websockets

PORT = 9333

def page_ws():
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=5) as r:
        targets = json.load(r)
    pages = [t for t in targets if t.get("type") == "page"]
    if not pages:
        raise SystemExit("没有可用的 page target")
    return pages[0]["webSocketDebuggerUrl"]


async def main():
    args = sys.argv[1:]
    goto = None
    if args and args[0] == "--goto":
        goto = args[1]
        args = args[2:]
    expr = args[0] if args else "document.title"

    async with websockets.connect(page_ws(), max_size=32 * 1024 * 1024) as ws:
        mid = 0

        async def call(method, **params):
            nonlocal mid
            mid += 1
            await ws.send(json.dumps({"id": mid, "method": method, "params": params}))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("id") == mid:
                    return msg

        if goto:
            await call("Page.navigate", url=goto)
            await asyncio.sleep(3.0)

        res = await call("Runtime.evaluate",
                         expression=expr, returnByValue=True, awaitPromise=True)
        val = res.get("result", {}).get("result", {}).get("value")
        if val is None:
            print(json.dumps(res, ensure_ascii=False)[:1500])
        else:
            print(val if isinstance(val, str) else json.dumps(val, ensure_ascii=False))


asyncio.run(main())
