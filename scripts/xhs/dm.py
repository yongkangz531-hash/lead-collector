#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""小红书一键私信（CDP 驱动真实窗口）

流程：打开用户主页 → 点右上「发消息」(button.xhs-user-im-btn) → 页内弹出聊天窗
      → 在 .xhs-im-input-bar-editor 里输入 → 回车发送 → 回读输入框确认已发出。

实测要点：
  * 主页左上「发消息」(button.xhs-user-im-btn) 点下去是跳到 https://www.xiaohongshu.com/chat?openUid=<uid>，
    所以直接导航到这个地址最稳，省一次点击。
  * 输入框 .xhs-im-input-bar-editor 是普通 contenteditable，execCommand('insertText') 就能写进去并保持
    （不像抖音评论框是 Draft.js 需要逐字真按键）。
  * 页面上没有发送按钮（只有「表情」），发送 = 回车；发出去后输入框清空 + 消息出现在列表里。
  * 小红书限制：对方关注或回复你之前，只能发 1 条。发第二条会被平台拦。

节奏（写操作，保守）：条间 45-90 秒随机、每天最多 20 条、同一 uid 只发一次、连续失败 3 次停。
"""
from __future__ import annotations

import asyncio
import json
import random

import websockets

import control
import store
from collect2 import CDP, browser_ws, attach_page

DEFAULT_GAP = (45, 90)
FAIL_LIMIT = 3
SEND_WAIT = 2.6

OPEN_JS = """(function(){
  var b=document.querySelector('.xhs-user-im-btn');
  if(!b){
    var cands=[].slice.call(document.querySelectorAll('button,div[role=button],span,a'));
    for (var i=0;i<cands.length;i++){
      var t=(cands[i].innerText||'').trim();
      if(t==='发消息'||t==='私信'){ b=cands[i]; break; }
    }
  }
  if(!b) return 'no-btn';
  try { b.scrollIntoView({block:'center'}); } catch(e) {}
  var r=b.getBoundingClientRect();
  return JSON.stringify({x:Math.round(r.x+r.width/2), y:Math.round(r.y+r.height/2),
                         inView:(r.y>2&&r.y<innerHeight-2), t:(b.innerText||'').trim().slice(0,8)});
})()"""

STATE_JS = """(function(){
  var e=document.querySelector('.xhs-im-input-bar-editor');
  var win=document.querySelector('.xhs-im-chat-window');
  return JSON.stringify({ed:!!e, win:!!win, txt: e?((e.innerText||e.value||'').replace(/\\u200b/g,'').trim()):''});
})()"""

BLOCK_JS = """(function(){
  var t=(document.body&&document.body.innerText)||'';
  if(/扫码登录|登录后查看/.test(t)) return 'login';
  if(/安全验证|滑块|异常行为|操作频繁/.test(t)) return 'verify';
  return '';
})()"""


async def _click(cdp, sid, x, y):
    await cdp.call("Input.dispatchMouseEvent", sid, type="mouseMoved", x=x, y=y)
    await asyncio.sleep(random.uniform(0.05, 0.14))
    for ev in ("mousePressed", "mouseReleased"):
        await cdp.call("Input.dispatchMouseEvent", sid, type=ev, x=x, y=y,
                       button="left", clickCount=1)
        await asyncio.sleep(random.uniform(0.04, 0.09))


async def _type(cdp, sid, text):
    ok = await cdp.val(sid, """(function(){
      var e=document.querySelector('.xhs-im-input-bar-editor');
      if(!e) return 'no-editor';
      e.focus();
      try{
        var sel=window.getSelection(), r=document.createRange();
        r.selectNodeContents(e); sel.removeAllRanges(); sel.addRange(r);
        document.execCommand('delete');
        document.execCommand('insertText', false, %s);
      }catch(err){ return 'ERR '+err.message; }
      return (e.innerText||e.value||'').replace(/\\u200b/g,'').trim();
    })()""" % json.dumps(text))
    return ok or ""


async def _press_enter(cdp, sid):
    for t in ("keyDown", "keyUp"):
        await cdp.call("Input.dispatchKeyEvent", sid, type=t, key="Enter", code="Enter",
                       windowsVirtualKeyCode=13, nativeVirtualKeyCode=13,
                       text="\r" if t == "keyDown" else "")


async def _send_one(cdp, sid, uid, name, text) -> str:
    await cdp.call("Page.navigate", sid, url=f"https://www.xiaohongshu.com/chat?openUid={uid}")
    await asyncio.sleep(random.uniform(4.5, 6.5))

    block = await cdp.val(sid, BLOCK_JS)
    if block == "login":
        return "没登录，先扫码"
    if block == "verify":
        return "页面出验证了，先停手"

    st = {}
    for _ in range(20):
        st = json.loads(await cdp.val(sid, STATE_JS) or "{}")
        if st.get("ed"):
            break
        await asyncio.sleep(0.5)
    if not st.get("ed"):
        body = (await cdp.val(sid, "(document.body.innerText||'').slice(0,120).replace(/\\n+/g,' | ')")) or ""
        if "只能发送1条" in body or "私信" in body and "受限" in body:
            return "平台限制：对方没关注/回复前只能发 1 条"
        return "聊天页没打开（可能对方关闭了私信）"

    typed = await _type(cdp, sid, text)
    await asyncio.sleep(random.uniform(0.4, 0.8))
    if text[:4] not in (typed or ""):
        return "输入框没吃进文字"

    await _press_enter(cdp, sid)
    await asyncio.sleep(SEND_WAIT)

    after = json.loads(await cdp.val(sid, STATE_JS) or "{}")
    if (after.get("txt") or "").strip():
        return "已填好但没发出去（输入框还留着字）"
    shown = await cdp.val(sid, """(function(){
      var t=(document.body.innerText||'').replace(/\s+/g,'');
      return t.indexOf(%s) >= 0;
    })()""" % json.dumps("".join(text.split())))
    if not shown:
        return "输入框清空了，但消息没出现在聊天记录里（可能被拦）"
    return "ok"


async def run(targets, messages, log=print, gap=DEFAULT_GAP, daily_cap: int = 20):
    stat = {"ok": 0, "skipped": 0, "failed": 0, "done": 0, "total": len(targets),
            "log": [], "running": True}
    msgs = [str(m).strip() for m in (messages or []) if str(m).strip()]
    if not msgs:
        msgs = ["看到你说的这个问题，我这边正好有一份整理好的资料，需要的话发你"]

    if store.dm_today() >= daily_cap:
        log(f"今天已经发过 {store.dm_today()} 条，到日上限 {daily_cap} 了")
        stat["running"] = False
        return stat

    async with websockets.connect(browser_ws(), max_size=2 ** 26) as ws:
        cdp = CDP(ws)
        tid, sid = await attach_page(cdp, "https://www.xiaohongshu.com/explore")
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
            log(f"  {i + 1}/{len(targets)} 私信 {name}：{text[:22]}…")
            try:
                res = await _send_one(cdp, sid, uid, name, text)
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
