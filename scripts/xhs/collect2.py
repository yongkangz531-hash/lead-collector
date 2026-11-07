#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""小红书评论采集（真实鼠标点击版）

为什么是"点击"：搜索结果里的链接不带 xsec_token，直接跳 URL 会 404。
必须用真实鼠标事件点卡片，前端路由才会把 xsec_token 拼上，笔记才能打开。

风控节奏：
  搜索后滚动 3 次（每次 1.2-2s）
  每篇之间 6-9 秒
  每篇内滚动 3 次（每次 1.4-2.2s）
  默认只采 6 篇
"""
import argparse
import asyncio
import os
import json
import random
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import websockets

import guard
import control
import store

PORT = 9333
EMBED = os.environ.get("XHS_EMBED") == "1"
HERE = Path(__file__).parent
JS_DIR = HERE / "js"
OUT_DIR = HERE / "data"

NOTE_JS = (JS_DIR / "note.js").read_text(encoding="utf-8")


def browser_ws():
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=5) as r:
        return json.load(r)["webSocketDebuggerUrl"]


class CDP:
    def __init__(self, ws):
        self.ws = ws
        self.i = 0

    async def call(self, method, sid=None, wait=20, **params):
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

    async def val(self, sid, expr, wait=20):
        r = await self.call("Runtime.evaluate", sid, expression=expr,
                            returnByValue=True, awaitPromise=True, wait=wait)
        return r.get("result", {}).get("result", {}).get("value")


async def click_card(cdp, sid, index):
    """用真实鼠标点第 index 张卡片，返回是否点中"""
    box = await cdp.val(sid, f"""(function(){{
      var cs=document.querySelectorAll('section.note-item');
      var c=cs[{index}]; if(!c) return '';
      c.scrollIntoView({{block:'center'}});
      var r=c.getBoundingClientRect();
      return JSON.stringify({{x:Math.round(r.x+r.width/2), y:Math.round(r.y+r.height/2)}});
    }})()""")
    if not box:
        return False
    b = json.loads(box)
    for t in ("mousePressed", "mouseReleased"):
        await cdp.call("Input.dispatchMouseEvent", sid, type=t, x=b["x"], y=b["y"],
                       button="left", clickCount=1)
    return True


async def _wait(sec, allow_pause=True):
    """可被打断的等待：返回 False 表示收到停止；暂停期间不消耗时间"""
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
    """用软件里嵌着的那个浏览器标签（找不到才新建）"""
    tid = ""
    try:
        r = await cdp.call("Target.getTargets")
        for t in r["result"]["targetInfos"]:
            if t.get("type") == "page" and "xiaohongshu.com" in (t.get("url") or ""):
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
    return tid, sid


async def run(kw: str, max_notes: int, log=print, skip=None):
    rows, notes_meta = [], []
    async with websockets.connect(browser_ws(), max_size=2 ** 26) as ws:
        cdp = CDP(ws)

        async def open_tab(url="about:blank"):
            """直接建在目标页面的标签，attach 并验证会话可用"""
            for attempt in range(3):
                tid_ = (await cdp.call("Target.createTarget", url=url,
                                       newWindow=True))["result"]["targetId"]
                sid_ = (await cdp.call("Target.attachToTarget", targetId=tid_,
                                       flatten=True))["result"]["sessionId"]
                try:
                    await cdp.call("Page.bringToFront", sid_)
                    await cdp.val(sid_, "1+1")
                    return tid_, sid_
                except Exception:
                    try:
                        await cdp.call("Target.closeTarget", targetId=tid_)
                    except Exception:
                        pass
                    await asyncio.sleep(1.5)
            raise RuntimeError("连不上采集标签页，请关掉多余的 Chrome 窗口后重试")

        search_url = ("https://www.xiaohongshu.com/search_result?keyword="
                      + urllib.parse.quote(kw))
        if EMBED:
            tid, sid = await attach_page(cdp, search_url)
            await cdp.call("Page.navigate", sid, url=search_url)
            log("内置浏览器打开搜索页")
        else:
            tid, sid = await open_tab(search_url)
            log("打开搜索页")
        await asyncio.sleep(6)

        for _ in range(3):
            await cdp.val(sid, "window.scrollBy(0,900)")
            await asyncio.sleep(random.uniform(1.2, 2.0))

        cards = await cdp.val(sid, "document.querySelectorAll('section.note-item').length") or 0
        need_login = await cdp.val(sid, "document.body.innerText.indexOf('登录后查看搜索')>=0")
        log(f"搜索页：{cards} 篇笔记" + ("（需要登录）" if need_login else ""))
        if need_login:
            raise RuntimeError("浏览器没有登录态，请先在小红书窗口扫码登录")
        if not cards:
            raise RuntimeError("搜索页没有解析到笔记卡片")

        # 先记下这一页所有笔记 id + 标题，再按 id 精确点击（避免索引错位重复打开同一篇）
        raw_list = await cdp.val(sid, "".join([
            "(function(){var out=[];",
            "document.querySelectorAll('section.note-item').forEach(function(c){",
            "var a=c.querySelector('a[href*=\"/explore/\"]'); if(!a)return;",
            "var m=(a.getAttribute('href')||'').match(/explore\\/([0-9a-f]{12,})/); if(!m)return;",
            "var t=(c.innerText||'').split('\\n')[0]||'';",
            "out.push({id:m[1], title:t.slice(0,40)});});",
            "return JSON.stringify(out);})()"])) or "[]"
        targets = json.loads(raw_list)
        if skip:
            kept = [t for t in targets if not skip(t["id"])]
            if len(kept) < len(targets):
                log(f"跳过已采过的 {len(targets)-len(kept)} 篇")
            targets = kept
        targets = targets[:max_notes]
        total = len(targets)
        log(f"本轮目标 {total} 篇")

        for idx, t in enumerate(targets):
            # 暂停 / 停止 开关（在这一篇开始前生效）
            while control.is_paused() and not control.is_stopped():
                await asyncio.sleep(0.8)
            if control.is_stopped():
                log("已停止采集")
                break
            if not await _wait(guard.gap_seconds(idx)):
                log("已停止采集")
                break
            if idx:
                # 浏览器「后退」回搜索结果，比重新加载更稳、也更像真人
                try:
                    await cdp.val(sid, "history.back()")
                except Exception:
                    await cdp.call("Page.navigate", sid, url=search_url)
                if not await _wait(random.uniform(3.2, 4.8)):
                    log("已停止采集")
                    break
                await cdp.val(sid, "window.scrollBy(0,400)")
                await _wait(1.2)

            async def find_card(nid):
                # 只认「可见的」链接：小红书卡片里有一个 0x0 的 /explore 链接（没 token，点了会 404），
                # 真正能点的是 cover（/search_result/<id>?xsec_token=...），要挑面积最大的那个
                js = ("(function(){var want='" + nid + "';var hit=null;"
                      "document.querySelectorAll('section.note-item').forEach(function(c){"
                      "var cr=c.getBoundingClientRect();if(cr.width<40||cr.height<40)return;"
                      "c.querySelectorAll('a').forEach(function(a){"
                      "var r=a.getBoundingClientRect();if(r.width<20||r.height<20)return;"
                      "var h=a.getAttribute('href')||'';"
                      "if(h.indexOf('/explore/')<0&&h.indexOf('/search_result/')<0)return;"
                      "if(h.indexOf(want)<0)return;"
                      "var area=r.width*r.height;"
                      "if(!hit||area>hit.area)hit={a:a,r:r,area:area,c:c};});});"
                      "if(!hit)return '';"
                      "hit.c.scrollIntoView({block:'center'});"
                      "var r=hit.a.getBoundingClientRect();"
                      "var cx=Math.round(r.x+r.width/2),cy=Math.round(r.y+Math.min(r.height/2,80));"
                      "return JSON.stringify({x:Math.max(2,Math.min(cx,window.innerWidth-4)),"
                      "y:Math.max(2,Math.min(cy,window.innerHeight-4)),href:hit.a.getAttribute('href')||''});})()")
                for _try in range(8):
                    b = await cdp.val(sid, js)
                    if b:
                        return b
                    await cdp.val(sid, "window.scrollBy(0,700)")
                    await asyncio.sleep(1.2)
                return ""

            box = await find_card(t["id"])
            if not box:
                # 后退后这一屏没渲染到它，重新搜一次再找（仍受同样的节奏限制）
                await cdp.call("Page.navigate", sid, url=search_url)
                await asyncio.sleep(6.5)
                box = await find_card(t["id"])
            if not box:
                log(f"  {idx+1}/{total} 找不到卡片 {t['id'][:8]}，跳过")
                continue
            b = json.loads(box)
            await cdp.call("Input.dispatchMouseEvent", sid, type="mouseMoved", x=b["x"], y=b["y"])
            await asyncio.sleep(random.uniform(0.2, 0.4))
            await cdp.call("Input.dispatchMouseEvent", sid, type="mousePressed", x=b["x"], y=b["y"],
                           button="left", clickCount=1)
            await asyncio.sleep(random.uniform(0.06, 0.12))
            await cdp.call("Input.dispatchMouseEvent", sid, type="mouseReleased", x=b["x"], y=b["y"],
                           button="left", clickCount=1)
            if not await _wait(random.uniform(4.5, 6.5)):
                log("已停止采集")
                break

            OPEN_JS = ("!!document.querySelector('#noteContainer, .note-detail-mask, .note-scroller, "
                       "#detail-title, .note-content .title')")
            url = await cdp.val(sid, "location.href") or ""
            opened = (t["id"] in url) or bool(await cdp.val(sid, OPEN_JS))
            if not opened:
                # 第二次点击：稍微偏上一点再试一次
                await asyncio.sleep(random.uniform(1.2, 1.8))
                for key in ("mousePressed", "mouseReleased"):
                    await cdp.call("Input.dispatchMouseEvent", sid, type=key,
                                   x=b["x"], y=max(4, b["y"] - 25), button="left", clickCount=1)
                await asyncio.sleep(random.uniform(3.5, 5.0))
                url = await cdp.val(sid, "location.href") or ""
                opened = (t["id"] in url) or bool(await cdp.val(sid, OPEN_JS))
            if not opened and b.get("href"):
                # 兜底：卡片链接自带 xsec_token，直接打开
                hr = b["href"]
                full = hr if hr.startswith("http") else "https://www.xiaohongshu.com" + hr
                await cdp.call("Page.navigate", sid, url=full)
                await asyncio.sleep(random.uniform(4.5, 6.0))
                url = await cdp.val(sid, "location.href") or ""
                opened = bool(await cdp.val(sid, OPEN_JS))
            if "/404" in url:
                log(f"  {idx+1}/{total} 打开失败（404），跳过")
                continue
            if not opened:
                log(f"  {idx+1}/{total} 没打开目标笔记，跳过")
                continue

            for _ in range(3):
                await cdp.val(sid, "window.scrollBy(0, document.body.scrollHeight*0.6)")
                await asyncio.sleep(random.uniform(1.4, 2.2))

            if control.is_stopped():
                log("已停止采集")
                break
            try:
                raw = await cdp.val(sid, NOTE_JS)
                d = json.loads(raw)
            except Exception as e:
                log(f"  {idx+1}/{total} 解析失败：{str(e)[:60]}")
                continue

            nid = (d["url"].split("/explore/")[-1].split("?")[0]) if "/explore/" in d["url"] else ""
            guard.note_taken(1)
            log(f"  {idx+1}/{total} 《{d['title'][:24]}》 评论 {d['total']} 条")
            notes_meta.append({"id": nid, "title": d["title"], "desc": d["desc"],
                               "url": d["url"], "comments": d["total"],
                               "likes": d.get("likes", ""), "tags": d.get("tags", ""),
                               "date": d.get("date", ""), "kw": kw})
            try:
                store.upsert_note(notes_meta[-1])
            except Exception:
                pass
            for c in d["comments"]:
                rows.append({"kw": kw, "platform": "小红书", "note": d["title"],
                             "note_id": nid, "note_url": d["url"],
                             "name": c["name"], "text": c["text"], "like": c["like"],
                             "user_url": c.get("u", ""), "ip": c.get("ip", ""),
                             "ctime": c.get("ctime", "")})

        if not EMBED:
            await cdp.call("Target.closeTarget", targetId=tid)

    OUT_DIR.mkdir(exist_ok=True)
    fp = OUT_DIR / f"real_{kw}.json"
    fp.write_text(json.dumps({"kw": kw, "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                              "notes": notes_meta, "rows": rows},
                             ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"完成：{len(rows)} 条评论 / {len(notes_meta)} 篇笔记 -> {fp.name}")
    return rows


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("kw")
    ap.add_argument("--notes", type=int, default=6)
    a = ap.parse_args()
    try:
        asyncio.run(run(a.kw, a.notes))
    except Exception as e:
        print("失败：", type(e).__name__, str(e)[:200])
        sys.exit(1)

async def run_note(url, log=print):
    """采集单篇作品的评论（用于界面上的「进入作品 → 评论采集」）"""
    rows, notes_meta = [], []
    async with websockets.connect(browser_ws(), max_size=2 ** 26) as ws:
        cdp = CDP(ws)
        if EMBED:
            tid, sid = await attach_page(cdp, url)
        else:
            tid, sid = await open_tab(url)
        try:
            await cdp.call("Page.navigate", sid, url=url)
            await asyncio.sleep(random.uniform(5.0, 7.0))
            for _ in range(3):
                await cdp.val(sid, "window.scrollBy(0, document.body.scrollHeight*0.5)")
                await asyncio.sleep(random.uniform(1.4, 2.2))
            raw = await cdp.val(sid, NOTE_JS)
            d = json.loads(raw)
            nid = (d["url"].split("/explore/")[-1].split("?")[0]) if "/explore/" in d["url"] else ""
            guard.note_taken(1)
            notes_meta.append({"id": nid, "title": d["title"], "desc": d["desc"], "url": d["url"],
                               "comments": d["total"], "likes": d.get("likes", ""),
                               "tags": d.get("tags", ""), "date": d.get("date", ""), "kw": "单篇"})
            try:
                store.upsert_note(notes_meta[-1])
            except Exception:
                pass
            for c in d["comments"]:
                rows.append({"kw": "单篇", "platform": "小红书", "note": d["title"], "note_id": nid,
                             "note_url": d["url"], "name": c["name"], "text": c["text"],
                             "like": c["like"], "user_url": c.get("u", ""),
                             "ip": c.get("ip", ""), "ctime": c.get("ctime", "")})
            log(f"单篇完成：《{d['title'][:20]}》 评论 {d['total']} 条")
        finally:
            if not EMBED:
                try:
                    await cdp.call("Target.closeTarget", targetId=tid)
                except Exception:
                    pass
    return rows, notes_meta
