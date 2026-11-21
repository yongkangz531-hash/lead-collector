#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""抖音一键私信（CDP 驱动真实窗口）

流程：打开用户主页 → 点「私信」→ 在聊天输入框里打字 → 回车发送 → 回读输入框确认已发出。

节奏（写操作，比采集更保守）：
  每条之间 45-90 秒随机
  每天最多 20 条（可用 daily_cap 改）
  同一用户只发一次（台账 data/dm.json）
  连续失败 3 次自动停，避免被判定为脚本
只发文本，不带链接、不群发同样的话（词库里随机挑）。
"""
from __future__ import annotations

import asyncio
import json
import random
import time
import urllib.parse

import websockets

import control
import dy_collect as dc
import store

DEFAULT_GAP = (45, 90)
FAIL_LIMIT = 3
SEND_WAIT = 2.5


async def _type_text(cdp, sid, text):
    """往当前聚焦的输入框里输入文本（走 insertText，兼容 contenteditable）"""
    await cdp.call("Input.insertText", sid, text=text)


async def _press_enter(cdp, sid):
    for t in ("keyDown", "keyUp"):
        await cdp.call("Input.dispatchKeyEvent", sid, type=t, key="Enter", code="Enter",
                       windowsVirtualKeyCode=13, nativeVirtualKeyCode=13,
                       text="\r" if t == "keyDown" else "")


async def _send_one(cdp, sid, uid, name, text, log) -> str:
    url = f"https://www.douyin.com/user/{uid}"
    await cdp.call("Page.navigate", sid, url=url)
    await asyncio.sleep(random.uniform(4.5, 6.5))
    await cdp.val(sid, dy_collect_js())
    await cdp.val(sid, "__dyCloseTip()")

    how = await cdp.val(sid, "__dyDmOpen()")
    if how == "no-btn":
        return "没有私信入口（对方可能关闭了私信）"
    st = {}
    for _ in range(14):
        await asyncio.sleep(0.8)
        raw = await cdp.val(sid, "__dyDmCheck()")
        try:
            st = json.loads(raw or "{}")
        except Exception:
            st = {}
        if st.get("ok"):
            break
    if not st.get("ok"):
        return "私信窗口没打开"

    await cdp.val(sid, "__dyDmFocus()")
    await asyncio.sleep(random.uniform(0.3, 0.6))
    typed = json.loads(await cdp.val(sid, "__dyDmType(" + json.dumps(text) + ")") or "{}")
    if not (typed.get("text") or "").strip():
        await _type_text(cdp, sid, text)     # 兜底：CDP 直接插字
        await asyncio.sleep(0.6)
        typed = json.loads(await cdp.val(sid, "__dyDmCheck()") or "{}")
    await asyncio.sleep(random.uniform(0.5, 0.9))
    chk = json.loads(await cdp.val(sid, "__dyDmCheck()") or "{}")
    if not (chk.get("text") or "").strip():
        return "输入框没吃进文字"
    await _press_enter(cdp, sid)
    await asyncio.sleep(SEND_WAIT)
    after = json.loads(await cdp.val(sid, "__dyDmCheck()") or "{}")
    left = (after.get("text") or "").strip()
    if left:
        # 没发出去（有些账号要按发送按钮）
        return "已填好但没发出去（输入框还留着字）"
    return "ok"


def dy_collect_js():
    return dc.DY_JS


async def run(targets, messages, log=print, gap=DEFAULT_GAP, daily_cap: int = 20):
    """targets: [{'uid','name'}...]  messages: 词库 list"""
    stat = {"ok": 0, "skipped": 0, "failed": 0, "done": 0, "total": len(targets),
            "log": [], "running": True}
    msgs = [m for m in (messages or []) if str(m).strip()]
    if not msgs:
        msgs = ["看到你说的这个问题，我这有一份整理好的资料，需要的话我发你"]
    today = store.dm_today()
    if today >= daily_cap:
        log(f"今天已经发过 {today} 条，到日上限 {daily_cap} 了")
        stat["running"] = False
        return stat

    async with websockets.connect(dc.browser_ws(), max_size=2 ** 26) as ws:
        cdp = dc.CDP(ws)
        tid, sid = await dc.attach_page(cdp, "https://www.douyin.com/")
        fails = 0
        for i, t in enumerate(targets):
            if control.is_stopped():
                log("已停止私信")
                break
            if store.dm_today() >= daily_cap:
                log(f"到日上限 {daily_cap} 条，停下")
                break
            uid = (t.get("uid") or "").strip()
            name = (t.get("name") or "").strip()
            if not uid:
                stat["skipped"] += 1
                continue
            if store.is_dmd(uid):
                stat["skipped"] += 1
                log(f"  {name} 已经私信过，跳过")
                continue
            text = " ".join(random.choice(msgs).split())
            log(f"  {i + 1}/{len(targets)} 私信 {name}：{text[:24]}…")
            try:
                res = await _send_one(cdp, sid, uid, name, text, log)
            except Exception as e:
                res = f"出错：{type(e).__name__} {str(e)[:60]}"
            if res == "ok":
                store.mark_dm(uid, name, text)
                stat["ok"] += 1
                fails = 0
                log("    已发送")
            else:
                stat["failed"] += 1
                fails += 1
                log("    " + res)
                if fails >= FAIL_LIMIT:
                    log("连续失败，先停下（检查是不是被限制了）")
                    break
            stat["done"] = i + 1
            if i + 1 < len(targets):
                await asyncio.sleep(random.uniform(*gap))
    stat["running"] = False
    return stat


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("uid")
    ap.add_argument("text")
    a = ap.parse_args()
    print(asyncio.run(run([{"uid": a.uid, "name": "测试"}], [a.text])))
