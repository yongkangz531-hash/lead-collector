#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""抖音评论采集（真实鼠标点击版）

和小红书的区别：
  1. 抖音搜索必须登录，未登录只给看几条
  2. 点开视频是「浮层」（URL 加 modal_id），关掉要按 Esc
  3. 评论在右侧独立面板里，要滚动面板才会加载，所以更慢也更像人
  4. 卡片链接是 /video/<id>，用户主页是 /user/<sec_uid>

节奏（比小红书更保守）：
  搜索后滚动 3 次（1.5-2.5s）
  每篇之间 10-15 秒
  每篇评论面板滚动 4-6 次，每次 1.8-2.8 秒
"""
import argparse
import asyncio
import json
import os
import random
import time
import urllib.parse
import urllib.request
from pathlib import Path

import websockets

import control
import guard
import store

PORT = int(os.environ.get("DY_CDP_PORT", "9334"))
EMBED = os.environ.get("DY_EMBED", os.environ.get("XHS_EMBED")) == "1"
HERE = Path(__file__).parent
JS_DIR = HERE / "js"
OUT_DIR = HERE / "data"

DY_JS = (JS_DIR / "dy.js").read_text(encoding="utf-8")


# 用比特浏览器时，这里放它给的 CDP 地址；空表示用软件自己窗口
WS_OVERRIDE = {"ws": ""}


def browser_ws():
    if WS_OVERRIDE.get("ws"):
        return WS_OVERRIDE["ws"]
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=5) as r:
        return json.load(r)["webSocketDebuggerUrl"]


class CDP:
    def __init__(self, ws):
        self.ws = ws
        self.i = 0

    async def call(self, method, sid=None, wait=25, **params):
        self.i += 1
        mid = self.i
        msg = {"id": mid, "method": method, "params": params}
        if sid:
            msg["sessionId"] = sid
        await self.ws.send(json.dumps(msg))
        while True:
            m = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=wait))
            if m.get("id") == mid:
                if "error" in m:
                    raise RuntimeError(f"{method}: {m['error'].get('message')}")
                return m

    async def val(self, sid, expr, wait=25):
        r = await self.call("Runtime.evaluate", sid, expression=expr,
                            returnByValue=True, awaitPromise=True, wait=wait)
        return r.get("result", {}).get("result", {}).get("value")

    async def fire(self, method, sid=None, **params):
        """发出去不等待回执（滚轮这类高频事件用）"""
        self.i += 1
        msg = {"id": self.i, "method": method, "params": params}
        if sid:
            msg["sessionId"] = sid
        await self.ws.send(json.dumps(msg))

    async def mouse(self, sid, x, y):
        await self.call("Input.dispatchMouseEvent", sid, type="mouseMoved", x=x, y=y)
        await asyncio.sleep(random.uniform(0.15, 0.35))
        await self.call("Input.dispatchMouseEvent", sid, type="mousePressed", x=x, y=y,
                        button="left", clickCount=1)
        await asyncio.sleep(random.uniform(0.05, 0.12))
        await self.call("Input.dispatchMouseEvent", sid, type="mouseReleased", x=x, y=y,
                        button="left", clickCount=1)

    async def esc(self, sid):
        for t in ("keyDown", "keyUp"):
            await self.call("Input.dispatchKeyEvent", sid, type=t, key="Escape",
                            code="Escape", windowsVirtualKeyCode=27, nativeVirtualKeyCode=27)


async def _wait(sec, allow_pause=True):
    end = time.time() + sec
    while True:
        if control.is_stopped():
            return False
        if allow_pause and control.is_paused():
            await asyncio.sleep(0.3)
            end += 0.3
            continue
        left = end - time.time()
        if left <= 0:
            return True
        await asyncio.sleep(min(0.3, left))


async def attach_page(cdp, url=None):
    tid = ""
    try:
        r = await cdp.call("Target.getTargets")
        for t in r["result"]["targetInfos"]:
            if t.get("type") == "page" and "douyin.com" in (t.get("url") or ""):
                tid = t["targetId"]
                break
    except Exception:
        tid = ""
    if not tid:
        tid = (await cdp.call("Target.createTarget", url=url or "about:blank",
                              newWindow=False))["result"]["targetId"]
    sid = (await cdp.call("Target.attachToTarget", targetId=tid,
                          flatten=True))["result"]["sessionId"]
    await cdp.call("Page.bringToFront", sid)
    await cdp.val(sid, DY_JS)
    return tid, sid


async def _wait_url(cdp, sid, needle, timeout=14.0):
    """等 URL 里出现 needle"""
    end = time.time() + timeout
    while time.time() < end:
        u = await cdp.val(sid, "location.href") or ""
        if needle in u:
            return u
        await asyncio.sleep(0.6)
    return ""


async def _collect_video(cdp, sid, log):
    """当前在视频页：等评论渲染 → 滚几次 -> 抽取（只取顶层评论，抖音的回复要点开才出）"""
    await cdp.val(sid, DY_JS)
    for _ in range(10):
        n = await cdp.val(sid, "document.querySelectorAll('[data-e2e=comment-item]').length") or 0
        if n:
            break
        await asyncio.sleep(1.0)
    info = {}
    try:
        info = json.loads(await cdp.val(sid, "__dyNoteInfo()") or "{}")
    except Exception:
        info = {}
    seen = {}
    MAXC = 80   # 单条视频最多取 80 条评论，别在一页上待太久
    for i in range(5):
        raw = await cdp.val(sid, "__dyExtract()")
        try:
            for r in json.loads(raw or "[]"):
                seen[r["uid"] + "|" + r["text"][:24]] = r
        except Exception:
            pass
        if len(seen) >= MAXC:
            break
        await cdp.val(sid, "__dyScrollPage()")
        if not await _wait(random.uniform(1.8, 2.6)):
            break
    raw = await cdp.val(sid, "__dyExtract()")
    try:
        for r in json.loads(raw or "[]"):
            seen[r["uid"] + "|" + r["text"][:24]] = r
    except Exception:
        pass
    return list(seen.values()), info


async def run(kw: str, max_notes: int, log=print, skip=None):
    rows, notes_meta = [], []
    skip = skip or set()
    async with websockets.connect(browser_ws(), max_size=2 ** 26) as ws:
        cdp = CDP(ws)
        tid, sid = await attach_page(cdp, "https://www.douyin.com/")
        search_url = "https://www.douyin.com/search/" + urllib.parse.quote(kw) + "?type=video"
        await cdp.call("Page.navigate", sid, url=search_url)
        log("抖音搜索页打开中…")
        await asyncio.sleep(1.0)
        await cdp.val(sid, DY_JS)
        if not await _wait(random.uniform(8.0, 10.0)):
            log("已停止采集")
            return rows, notes_meta
        await cdp.val(sid, "window.scrollTo(0,0)")
        await asyncio.sleep(1.2)
        for _ in range(3):
            await cdp.val(sid, "window.scrollBy(0, 900)")
            if not await _wait(random.uniform(1.5, 2.5)):
                return rows, notes_meta

        cards = json.loads(await cdp.val(sid, "__dyCards()") or "[]")
        if not cards:
            need = await cdp.val(sid, "document.body.innerText.indexOf('扫码登录')>=0")
            if need:
                raise RuntimeError("浏览器没有登录态，请先在抖音窗口扫码登录")
            raise RuntimeError("搜索页没有解析到视频卡片")
        if callable(skip):
            cards = [c for c in cards if not skip(c["id"])]
        else:
            skip = set(skip or [])
            cards = [c for c in cards if c["id"] not in skip]
        cards = cards[:max_notes]
        log(f"搜索页：{len(cards)} 个视频待采")

        for idx, c in enumerate(cards):
            if control.is_stopped():
                log("已停止采集")
                break
            nid = c["id"]
            log(f"  {idx + 1}/{len(cards)} 打开 {nid[-8:]}")
            # 抖音视频页可以直接进（不需要 token），点卡片会走 target=_blank，反而不稳
            ok = False
            for attempt in range(2):
                await cdp.call("Page.navigate", sid, url=f"https://www.douyin.com/video/{nid}")
                if await _wait(random.uniform(7.0, 9.0)):
                    ok = True
                    break
                if control.is_stopped():
                    break
                log("    页面没打开，重试一次")
                await asyncio.sleep(2.0)
            if not ok:
                if control.is_stopped():
                    log("已停止采集")
                    break
                log("    这条跳过（页面一直没加载出来）")
                continue
            cmts, info = await _collect_video(cdp, sid, log)
            title = (info.get("title") or "").replace("\n", " ")[:60]
            note_url = f"https://www.douyin.com/video/{nid}"
            store.upsert_note({"id": nid, "title": title or nid, "desc": "", "url": note_url,
                               "comments": info.get("comments", ""), "likes": info.get("likes", ""),
                               "tags": "", "date": "", "kw": kw})
            guard.note_taken(1)
            notes_meta.append({"id": nid, "title": title, "total": len(cmts)})
            log(f"  {idx + 1}/{len(cards)} 《{title[:18]}》 评论 {len(cmts)} 条")
            for r in cmts:
                rows.append({"kw": kw, "platform": "抖音", "note": title, "note_id": nid,
                             "note_url": note_url, "name": r["name"], "text": r["text"],
                             "like": r.get("like", ""),
                             "user_url": "https://www.douyin.com/user/" + (r.get("uid") or ""),
                             "ip": r.get("ip", ""), "ctime": r.get("ctime", "")})

            # 回搜索页，接着下一篇
            await cdp.call("Page.navigate", sid, url=search_url)
            await asyncio.sleep(1.0)
            await cdp.val(sid, DY_JS)
            if not await _wait(random.uniform(7.0, 10.0)):
                break
            await cdp.val(sid, "window.scrollTo(0,0)")
            await asyncio.sleep(1.0)
            for _ in range(2):
                await cdp.val(sid, "window.scrollBy(0, 700)")
                await _wait(random.uniform(1.2, 2.0))

    OUT_DIR.mkdir(exist_ok=True)
    fp = OUT_DIR / f"real_{kw}.json"
    fp.write_text(json.dumps({"kw": kw, "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                              "notes": notes_meta, "rows": rows}, ensure_ascii=False), encoding="utf-8")
    log(f"完成：{len(rows)} 条评论 / {len(notes_meta)} 个视频 -> {fp.name}")
    return rows, notes_meta


async def run_note(url: str, log=print):
    rows, notes_meta = [], []
    async with websockets.connect(browser_ws(), max_size=2 ** 26) as ws:
        cdp = CDP(ws)
        tid, sid = await attach_page(cdp, url)
        await cdp.call("Page.navigate", sid, url=url)
        if not await _wait(random.uniform(6.0, 8.0)):
            return rows, notes_meta
        cmts, info = await _collect_video(cdp, sid, log)
        nid = url.split("/video/")[-1].split("?")[0] if "/video/" in url else ""
        title = (info.get("title") or nid).replace("\n", " ")[:60]
        store.upsert_note({"id": nid, "title": title, "desc": "", "url": url,
                           "comments": info.get("comments", ""), "likes": info.get("likes", ""),
                           "tags": "", "date": "", "kw": "单条"})
        guard.note_taken(1)
        notes_meta.append({"id": nid, "title": title, "total": len(cmts)})
        for r in cmts:
            rows.append({"kw": "单条", "platform": "抖音", "note": title, "note_id": nid,
                         "note_url": url, "name": r["name"], "text": r["text"],
                         "like": r.get("like", ""),
                         "user_url": "https://www.douyin.com/user/" + (r.get("uid") or ""),
                         "ip": r.get("ip", ""), "ctime": r.get("ctime", "")})
        log(f"单条完成：《{title[:18]}》 评论 {len(rows)} 条")
    return rows, notes_meta


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("kw")
    ap.add_argument("--notes", type=int, default=3)
    a = ap.parse_args()
    asyncio.run(run(a.kw, a.notes))
