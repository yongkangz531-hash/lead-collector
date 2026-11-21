#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""抖音评论采集（真实鼠标点击 + 滚轮加载版）

和小红书的区别：
  1. 抖音搜索必须登录，未登录只给看几条
  2. 视频页可以直接进 /video/<id>（不需要 xsec_token）
  3. 评论在右栏，要靠滚轮（CDP mouseWheel）才会继续加载，滚轮坐标必须在视口内
  4. 卡片链接是 /video/<id>，用户主页是 /user/<sec_uid>

采集深度（2026-11 实测）：
  一条 543 条评论的视频，每滚一轮加载约 10 条，滚 12 轮拿到 117 条。
  所以「轮次」直接决定采到多少，默认按目标条数跑，最多 MAX_ROUNDS 轮。
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

MAX_ROUNDS = 30          # 单条视频最多滚这么多轮（每轮约 10 条）
MAXC_HARD = 400          # 单条视频评论硬上限，防止超大热门视频占满时间
DEFAULT_TARGET = 120     # 默认目标条数


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

    async def wheel(self, sid, x, y, dy):
        await self.fire("Input.dispatchMouseEvent", sid, type="mouseWheel",
                        x=int(x), y=int(y), deltaX=0, deltaY=int(dy), modifiers=0)

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


def _num(s):
    """把 1.2万 / 3.4万 / 123 变成整数，用于过滤"""
    s = (s or "").strip()
    if not s:
        return 0
    try:
        if s.endswith("万"):
            return int(float(s[:-1]) * 10000)
        if s.endswith("w"):
            return int(float(s[:-1]) * 10000)
        return int(float(s))
    except Exception:
        return 0


async def _open_comments(cdp, sid, log):
    """打开评论面板；已经在页面上就直接返回 True"""
    n = await cdp.val(sid, "document.querySelectorAll('[data-e2e=comment-item]').length") or 0
    if n:
        return True
    await cdp.val(sid, "__dyCloseTip()")
    for attempt in range(3):
        how = await cdp.val(sid, "__dyOpenComments()")
        if how in ("svg", "el"):
            for _ in range(12):
                await asyncio.sleep(0.7)
                n = await cdp.val(sid, "document.querySelectorAll('[data-e2e=comment-item]').length") or 0
                if n:
                    return True
        # 兜底：真鼠标点图标
        box = await cdp.val(sid, "window.__dyOpenComments?1:0")
        rect = await cdp.val(sid, """(function(){var es=document.querySelectorAll('[data-e2e=feed-comment-icon]');
            for(var i=0;i<es.length;i++){var r=es[i].getBoundingClientRect();
            if(r.height>0&&r.top>=0&&r.top<window.innerHeight)return JSON.stringify({x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)});}
            return '';})()""")
        if rect:
            try:
                d = json.loads(rect)
                await cdp.mouse(sid, d["x"], d["y"])
            except Exception:
                pass
            for _ in range(8):
                await asyncio.sleep(0.7)
                n = await cdp.val(sid, "document.querySelectorAll('[data-e2e=comment-item]').length") or 0
                if n:
                    return True
        await asyncio.sleep(1.2)
    return (await cdp.val(sid, "document.querySelectorAll('[data-e2e=comment-item]').length") or 0) > 0


async def _collect_video(cdp, sid, log, target=DEFAULT_TARGET, expand=1):
    """当前在视频页：打开评论 → 滚轮加载 → 累计去重抽取"""
    await cdp.val(sid, DY_JS)
    ok = await _open_comments(cdp, sid, log)
    if not ok:
        log("    评论面板没打开，尽量抽当前页面的内容")
    info = {}
    try:
        info = json.loads(await cdp.val(sid, "__dyNoteInfo()") or "{}")
    except Exception:
        info = {}

    seen = {}
    limit = min(int(target or DEFAULT_TARGET), MAXC_HARD)
    stall = 0
    for i in range(MAX_ROUNDS):
        if control.is_stopped():
            break
        if expand and i % 3 == 0:
            try:
                await cdp.val(sid, f"__dyExpand(4)")
            except Exception:
                pass
        raw = await cdp.val(sid, "__dyExtract()")
        before = len(seen)
        try:
            for r in json.loads(raw or "[]"):
                seen[r.get("uid", "") + "|" + (r.get("text") or "")[:24]] = r
        except Exception:
            pass
        if len(seen) >= limit:
            break
        if len(seen) == before:
            stall += 1
            if stall >= 3:
                break
        else:
            stall = 0
        box = await cdp.val(sid, "__dyCommentBox()")
        if box:
            try:
                d = json.loads(box)
                await cdp.wheel(sid, d["x"], d["y"], random.randint(600, 1000))
            except Exception:
                await cdp.val(sid, "__dyScrollPage()")
        else:
            await cdp.val(sid, "window.scrollBy(0, 800)")
        if not await _wait(random.uniform(1.7, 2.6)):
            break
    # 收尾再抽一次
    raw = await cdp.val(sid, "__dyExtract()")
    try:
        for r in json.loads(raw or "[]"):
            seen[r.get("uid", "") + "|" + (r.get("text") or "")[:24]] = r
    except Exception:
        pass
    return list(seen.values()), info


def _in_date_range(d, frm, to):
    """d/frm/to 都是 YYYY-MM-DD；空表示不限"""
    if not d:
        return True
    if frm and d < frm:
        return False
    if to and d > to:
        return False
    return True


def _tags(title):
    import re
    return " ".join(re.findall(r"#([^\s#]{1,20})", title or ""))[:60]


def _rows_from(cmts, kw, title, nid, note_url, extra=None):
    rows = []
    for r in cmts:
        row = {"kw": kw, "platform": "抖音", "note": title, "note_id": nid,
               "note_url": note_url, "name": r["name"], "text": r["text"],
               "like": r.get("like", ""),
               "user_url": "https://www.douyin.com/user/" + (r.get("uid") or ""),
               "ip": r.get("ip", ""), "ctime": r.get("ctime", "")}
        if r.get("author"):
            row["is_author"] = 1
        if extra:
            row.update(extra)
        rows.append(row)
    return rows


async def _search_cards(cdp, sid, kw, need, log):
    """搜索页：往下滚，直到卡片够 need*2 或滚到底"""
    search_url = "https://www.douyin.com/search/" + urllib.parse.quote(kw) + "?type=video"
    await cdp.call("Page.navigate", sid, url=search_url)
    log(f"搜索「{kw}」…")
    await asyncio.sleep(1.0)
    await cdp.val(sid, DY_JS)
    if not await _wait(random.uniform(7.0, 9.5)):
        return [], search_url
    await cdp.val(sid, "window.scrollTo(0,0)")
    await asyncio.sleep(1.0)

    seen = {}
    stall = 0
    for _ in range(10):
        raw = await cdp.val(sid, "__dyCards()")
        before = len(seen)
        try:
            for c in json.loads(raw or "[]"):
                seen[c["id"]] = c
        except Exception:
            pass
        if len(seen) >= max(need * 2, 8):
            break
        if len(seen) == before:
            stall += 1
            if stall >= 2:
                break
        else:
            stall = 0
        await cdp.val(sid, "window.scrollBy(0, 900)")
        if not await _wait(random.uniform(1.4, 2.2)):
            break
    return list(seen.values()), search_url


async def run(kw: str, max_notes: int, log=print, skip=None,
              kw2: str = "", date_from: str = "", date_to: str = "",
              target: int = DEFAULT_TARGET, expand: int = 1,
              min_likes: int = 0, min_comments: int = 0):
    rows, notes_meta = [], []
    skip = skip or set()
    full_kw = (kw + " " + kw2).strip() if kw2 else kw
    async with websockets.connect(browser_ws(), max_size=2 ** 26) as ws:
        cdp = CDP(ws)
        tid, sid = await attach_page(cdp, "https://www.douyin.com/")
        cards, search_url = await _search_cards(cdp, sid, full_kw, max_notes, log)
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
        cards = cards[:max(8, int(max_notes) * 4)]      # 多取一些，被日期/门槛过滤掉还有余量
        log(f"搜索页：{len(cards)} 个视频候选（目标 {max_notes} 条）")

        done = 0
        for idx, c in enumerate(cards):
            if control.is_stopped():
                log("已停止采集")
                break
            if done >= max_notes:
                break
            nid = c["id"]
            log(f"  {idx + 1}/{len(cards)} 打开 {nid[-8:]}")
            ok = False
            for attempt in range(2):
                await cdp.call("Page.navigate", sid, url=f"https://www.douyin.com/video/{nid}")
                if await _wait(random.uniform(6.5, 8.5)):
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

            try:
                info = json.loads(await cdp.val(sid, "__dyNoteInfo()") or "{}")
            except Exception:
                info = {}
            pdate = info.get("date") or ""
            if not _in_date_range(pdate, date_from, date_to):
                log(f"    发布时间 {pdate or '未知'} 不在筛选范围内，跳过")
                continue
            likes, cmt_n = _num(info.get("likes")), _num(info.get("comments"))
            if min_likes and likes < min_likes:
                log(f"    点赞 {info.get('likes')} < {min_likes}，跳过")
                continue
            if min_comments and cmt_n < min_comments:
                log(f"    评论 {info.get('comments')} < {min_comments}，跳过")
                continue

            cmts, info2 = await _collect_video(cdp, sid, log, target=target, expand=expand)
            if info2:
                info.update({k: v for k, v in info2.items() if v})
            title = (info.get("title") or "").replace("\n", " ")[:60]
            note_url = f"https://www.douyin.com/video/{nid}"
            store.upsert_note({"id": nid, "title": title or nid, "desc": "", "url": note_url,
                               "comments": info.get("comments", ""), "likes": info.get("likes", ""),
                               "collects": info.get("collects", ""), "shares": info.get("shares", ""),
                               "author": info.get("author", ""), "author_uid": info.get("author_uid", ""),
                               "tags": _tags(title), "date": info.get("date", ""),
                               "kw": f"{kw} {kw2}".strip()})
            guard.note_taken(1)
            done += 1
            notes_meta.append({"id": nid, "title": title, "total": len(cmts), "date": info.get("date", ""),
                               "author": info.get("author", ""), "likes": info.get("likes", ""),
                               "comments": info.get("comments", "")})
            log(f"  {idx + 1}/{len(cards)} 《{title[:18]}》 {pdate} 评论 {len(cmts)} 条"
                f"（页面显示 {info.get('comments') or '?'}）")
            rows.extend(_rows_from(cmts, f"{kw} {kw2}".strip(), title, nid, note_url,
                                   {"author": info.get("author", ""),
                                    "vdate": info.get("date", ""),
                                    "vlikes": info.get("likes", ""),
                                    "vcomments": info.get("comments", ""),
                                    "kw2": kw2}))

            # 回搜索页，接着下一篇
            await cdp.call("Page.navigate", sid, url=search_url)
            await asyncio.sleep(1.0)
            await cdp.val(sid, DY_JS)
            if not await _wait(random.uniform(6.0, 8.0)):
                break
            await cdp.val(sid, "window.scrollTo(0,0)")
            await asyncio.sleep(1.0)

    OUT_DIR.mkdir(exist_ok=True)
    safe = "".join(ch for ch in full_kw if ch not in '\\/:*?"<>|').strip() or "kw"
    fp = OUT_DIR / f"real_{safe}.json"
    fp.write_text(json.dumps({"kw": full_kw, "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                              "notes": notes_meta, "rows": rows}, ensure_ascii=False), encoding="utf-8")
    log(f"完成：{len(rows)} 条评论 / {len(notes_meta)} 个视频 -> {fp.name}")
    return rows, notes_meta


async def run_note(url: str, log=print, target: int = DEFAULT_TARGET, expand: int = 1):
    rows, notes_meta = [], []
    async with websockets.connect(browser_ws(), max_size=2 ** 26) as ws:
        cdp = CDP(ws)
        tid, sid = await attach_page(cdp, url)
        await cdp.call("Page.navigate", sid, url=url)
        if not await _wait(random.uniform(6.0, 8.0)):
            return rows, notes_meta
        cmts, info = await _collect_video(cdp, sid, log, target=target, expand=expand)
        nid = url.split("/video/")[-1].split("?")[0] if "/video/" in url else ""
        title = (info.get("title") or nid).replace("\n", " ")[:60]
        store.upsert_note({"id": nid, "title": title, "desc": "", "url": url,
                           "comments": info.get("comments", ""), "likes": info.get("likes", ""),
                           "collects": info.get("collects", ""), "shares": info.get("shares", ""),
                           "author": info.get("author", ""), "author_uid": info.get("author_uid", ""),
                           "tags": _tags(title), "date": info.get("date", ""), "kw": "单条"})
        guard.note_taken(1)
        notes_meta.append({"id": nid, "title": title, "total": len(cmts),
                           "date": info.get("date", ""), "author": info.get("author", "")})
        rows = _rows_from(cmts, "单条", title, nid, url,
                          {"author": info.get("author", ""), "vdate": info.get("date", "")})
        log(f"单条完成：《{title[:18]}》 {info.get('date', '')} 评论 {len(rows)} 条"
            f"（页面显示 {info.get('comments') or '?'}）")
    return rows, notes_meta


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("kw")
    ap.add_argument("--notes", type=int, default=3)
    ap.add_argument("--target", type=int, default=DEFAULT_TARGET)
    ap.add_argument("--kw2", default="")
    a = ap.parse_args()
    asyncio.run(run(a.kw, a.notes, target=a.target, kw2=a.kw2))
