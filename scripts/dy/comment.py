#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""抖音评论（CDP 驱动真实窗口）

流程：打开视频页 → 把评论输入条滚进视口 → 真实鼠标点开编辑器（Draft.js）
      → 逐字打（真按键，不是 insertText）→ 回车发送 → 回读编辑器确认已发出。

为什么要逐字打：抖音评论框是 Draft.js，execCommand/insertText 能写进去，
但写完编辑器立刻被销毁（组件拿不到合法的 editorState，等于没输入）。
按真键逐字输入时编辑器一直保持打开，和人工操作一致。

节奏（写操作，比采集保守）：
  每条之间 gap 秒（默认 120-300 秒随机）
  每天最多 daily_cap 条
  同一条线索只评一次（台账 comments.json）
  连续失败 3 次自动停
"""
from __future__ import annotations

import asyncio
import json
import random

import websockets

import control
import dy_collect as dc
import store

FAIL_LIMIT = 3
DEFAULT_GAP = (120, 300)
EDIT_WAIT = 8.0

# 找到「留下你的精彩评论吧」那条输入栏，滚到视口 y≈150，返回中心坐标
FIND_BAR = """(function(){
  var bar=null;
  [].slice.call(document.querySelectorAll('div')).forEach(function(e){
    if(bar) return;
    var t=(e.innerText||'').trim();
    if(!/留下你的精彩评论|善语结善缘|说点什么/.test(t)) return;
    if(e.children.length>2) return;
    var r=e.getBoundingClientRect();
    if(r.height<14||r.height>70||r.width<100) return;
    bar=e;
  });
  if(!bar) return 'no-bar';
  var sc=document.querySelector('.parent-route-container.route-scroll-container')||document.scrollingElement;
  var r=bar.getBoundingClientRect();
  sc.scrollTop = sc.scrollTop - (150 - r.top);
  r=bar.getBoundingClientRect();
  return JSON.stringify({x:Math.round(r.x+r.width/2), y:Math.round(r.y+r.height/2),
                         w:Math.round(r.width), h:Math.round(r.height)});
})()"""

ED_STATE = """(function(){
  var e=document.querySelector('.public-DraftEditor-content');
  if(!e) return JSON.stringify({ed:false, txt:''});
  return JSON.stringify({ed:true, txt:(e.innerText||'').replace(/\\u200b/g,'').trim()});
})()"""

FIRST_COMMENT = """(function(){
  var it=document.querySelector('[data-e2e=comment-item]');
  return it ? (it.innerText||'').replace(/\\s+/g,' ').trim().slice(0,90) : '';
})()"""

BLOCK_JS = """(function(){
  var t=(document.body&&document.body.innerText)||'';
  if(/扫码登录|登录后查看/.test(t)) return 'login';
  if(/安全验证|滑块|操作过于频繁|请求太频繁/.test(t)) return 'verify';
  return '';
})()"""


async def _click(cdp, sid, x, y):
    await cdp.call("Input.dispatchMouseEvent", sid, type="mouseMoved", x=x, y=y)
    await asyncio.sleep(random.uniform(0.05, 0.15))
    for ev in ("mousePressed", "mouseReleased"):
        await cdp.call("Input.dispatchMouseEvent", sid, type=ev, x=x, y=y,
                       button="left", clickCount=1)
        await asyncio.sleep(random.uniform(0.04, 0.1))


async def _type_like_human(cdp, sid, text):
    """逐字真按键输入（Draft.js 只认这个）"""
    for ch in text:
        await cdp.call("Input.dispatchKeyEvent", sid, type="keyDown", key=ch,
                       text=ch, unmodifiedText=ch)
        await cdp.call("Input.dispatchKeyEvent", sid, type="keyUp", key=ch)
        await asyncio.sleep(random.uniform(0.07, 0.16))


async def _press_enter(cdp, sid):
    for t in ("keyDown", "keyUp"):
        await cdp.call("Input.dispatchKeyEvent", sid, type=t, key="Enter", code="Enter",
                       windowsVirtualKeyCode=13, nativeVirtualKeyCode=13,
                       text="\r" if t == "keyDown" else "")


async def _comment_one(cdp, sid, note_url, text, log) -> str:
    await cdp.call("Page.navigate", sid, url=note_url)
    await asyncio.sleep(random.uniform(5.5, 7.5))

    block = await cdp.val(sid, BLOCK_JS)
    if block == "login":
        return "没登录，先扫码"
    if block == "verify":
        return "页面出验证了，先停手"

    pos = await cdp.val(sid, FIND_BAR)
    if not pos or pos == "no-bar":
        return "没找到评论框（视频可能关了评论）"
    p = json.loads(pos)
    await asyncio.sleep(random.uniform(0.4, 0.8))
    await _click(cdp, sid, p["x"], p["y"])

    opened = False
    for _ in range(int(EDIT_WAIT / 0.4)):
        await asyncio.sleep(0.4)
        st = json.loads(await cdp.val(sid, ED_STATE) or "{}")
        if st.get("ed"):
            opened = True
            break
    if not opened:
        return "评论输入框没点开"

    await cdp.val(sid, "(function(){var e=document.querySelector('.public-DraftEditor-content');"
                       "if(e){e.focus();}return 1;})()")
    await asyncio.sleep(random.uniform(0.2, 0.5))
    await _type_like_human(cdp, sid, text)
    await asyncio.sleep(random.uniform(0.5, 0.9))

    st = json.loads(await cdp.val(sid, ED_STATE) or "{}")
    if not (st.get("txt") or "").strip():
        return "字没打进评论框"
    await _press_enter(cdp, sid)
    await asyncio.sleep(3.0)

    after = json.loads(await cdp.val(sid, ED_STATE) or "{}")
    if (after.get("txt") or "").strip():
        return "已填好但没发出去（输入框还留着字）"
    return "ok"


async def run(targets, phrases, log=print, gap=DEFAULT_GAP, daily_cap: int = 20):
    stat = {"ok": 0, "skipped": 0, "failed": 0, "done": 0, "total": len(targets),
            "log": [], "running": True}
    words = [str(w).strip() for w in (phrases or []) if str(w).strip()]
    if not words:
        words = ["这个细节说得挺实在", "收藏了，正好用得上"]

    if store.comments_today() >= daily_cap:
        log(f"今天已经评论 {store.comments_today()} 条，到日上限 {daily_cap} 了")
        stat["running"] = False
        return stat

    async with websockets.connect(dc.browser_ws(), max_size=2 ** 26) as ws:
        cdp = dc.CDP(ws)
        tid, sid = await dc.attach_page(cdp, "https://www.douyin.com/")
        fails = 0
        for i, t in enumerate(targets):
            if control.is_stopped():
                log("已停止评论")
                break
            if store.comments_today() >= daily_cap:
                log(f"到日上限 {daily_cap} 条，停下")
                break
            key = t.get("key") or ""
            name = (t.get("name") or "").strip()
            note_url = (t.get("note_url") or "").strip()
            if not note_url or not key:
                stat["skipped"] += 1
                continue
            if store.is_commented(key):
                stat["skipped"] += 1
                log(f"  {name} 已经评过，跳过")
                continue
            text = " ".join(random.choice(words).split())
            log(f"  {i + 1}/{len(targets)} 评论 {name}：{text[:20]}…")
            try:
                res = await _comment_one(cdp, sid, note_url, text, log)
            except Exception as e:
                res = f"出错：{type(e).__name__} {str(e)[:60]}"
            if res == "ok":
                store.mark_commented(key, name, text)
                stat["ok"] += 1
                fails = 0
                log("    已发出")
            else:
                stat["failed"] += 1
                fails += 1
                log("    " + res)
                if res in ("页面出验证了，先停手", "没登录，先扫码"):
                    log("停：先把验证/登录处理掉")
                    break
                if fails >= FAIL_LIMIT:
                    log("连续失败，先停下（检查是不是被限制了）")
                    break
            stat["done"] = i + 1
            if i + 1 < len(targets):
                await asyncio.sleep(random.uniform(*gap))
    stat["running"] = False
    return stat
