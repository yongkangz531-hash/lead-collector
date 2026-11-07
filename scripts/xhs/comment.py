#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""评论：从评论词库随机取一条，回复到指定客户的那条评论下面。

流程（全部真实鼠标）：
  打开笔记（链接自带 xsec_token）→ 按「昵称 + 评论前几个字」找到那条评论 →
  点它的回复图标 → 点底部输入框聚焦 → 输入文字 → 等发送按钮可点 → 点发送 →
  确认输入框已清空。
节奏：每条 12-20 秒，单批最多 10 条，同一条短语不连用，每天最多 30 条。
"""
import asyncio
import json
import random
import time

import websockets

import control
import store
from collect2 import CDP, browser_ws, attach_page, EMBED

FIND_JS = r"""
(function(){
  var wantName = %s, wantText = %s;
  var items = [].slice.call(document.querySelectorAll('.comment-item'));
  var norm = function(s){ return (s||'').replace(/\s+/g,'').slice(0, 12); };
  var wt = norm(wantText);
  var hit = null;
  for (var i=0;i<items.length;i++){
    var nm = items[i].querySelector('.author .name, .name');
    var ct = items[i].querySelector('.content .note-text, .content');
    if (!nm || !ct) continue;
    if (norm(nm.innerText).indexOf(norm(wantName)) >= 0 && norm(ct.innerText).indexOf(wt) >= 0) { hit = items[i]; break; }
  }
  if (!hit) {
    for (var j=0;j<items.length;j++){
      var c2 = items[j].querySelector('.content');
      if (c2 && norm(c2.innerText).indexOf(wt) >= 0) { hit = items[j]; break; }
    }
  }
  if (!hit) return JSON.stringify({ok:false, why:'没找到这条评论'});
  hit.scrollIntoView({block:'center'});
  var rp = hit.querySelector('.interactions .reply, .reply.icon-container, .reply');
  if (!rp) return JSON.stringify({ok:false, why:'评论没有回复按钮'});
  var r = rp.getBoundingClientRect();
  return JSON.stringify({ok:true, x:Math.round(r.x+r.width/2), y:Math.round(r.y+r.height/2)});
})()
"""

STATE_JS = r"""
(function(){
  var ed = document.querySelector('.engage-bar [contenteditable="true"], .engage-bar .content-input, .reply-input [contenteditable="true"]');
  var send = null;
  var btns = [].slice.call(document.querySelectorAll('.engage-bar button, .engage-bar .btn'));
  for (var i=0;i<btns.length;i++){
    if ((btns[i].innerText||'').replace(/\s+/g,'') === '发送') { send = btns[i]; break; }
  }
  var er = ed ? ed.getBoundingClientRect() : null;
  var sr = send ? send.getBoundingClientRect() : null;
  var body = document.body ? document.body.innerText : '';
  var needVerify = body.indexOf('扫码验证') >= 0 || body.indexOf('请求太频繁') >= 0 || body.indexOf('请一分钟后再试') >= 0;
  return JSON.stringify({
    needVerify: needVerify,
    hasEd: !!ed,
    txt: ed ? (ed.innerText || '').trim() : '',
    ph: ed ? (ed.getAttribute('placeholder') || '') : '',
    dis: send ? !!send.disabled : true,
    ex: er ? Math.round(er.x + er.width/2) : 0,
    ey: er ? Math.round(er.y + er.height/2) : 0,
    bx: sr ? Math.round(sr.x + sr.width/2) : 0,
    by: sr ? Math.round(sr.y + sr.height/2) : 0
  });
})()
"""


async def _click(cdp, sid, x, y):
    await cdp.call("Input.dispatchMouseEvent", sid, type="mouseMoved", x=x, y=y, button="none")
    await asyncio.sleep(0.25)
    for k in ("mousePressed", "mouseReleased"):
        await cdp.call("Input.dispatchMouseEvent", sid, type=k, x=x, y=y,
                       button="left", clickCount=1)


async def _wait(sec):
    end = time.time() + sec
    while True:
        if control.is_stopped():
            return False
        left = end - time.time()
        if left <= 0:
            return True
        await asyncio.sleep(min(0.3, left))


def pick_phrase(phrases, last=""):
    pool = [p.strip() for p in phrases if p.strip()]
    if not pool:
        return ""
    alt = [p for p in pool if p != last] or pool
    return random.choice(alt)


async def run(targets, phrases, log=print, max_batch=5):
    done = {"ok": 0, "skipped": 0, "failed": 0}
    if not phrases:
        log("评论词库是空的，先去设置页填几条")
        return done
    todo = [t for t in targets if t.get("note_url") and t.get("text")][:max_batch]
    if not todo:
        log("没有可评论的目标")
        return done
    log(f"准备评论 {len(todo)} 条（词库 {len(phrases)} 条，随机取）")

    async with websockets.connect(browser_ws(), max_size=2 ** 26) as ws:
        cdp = CDP(ws)
        if EMBED:
            tid, sid = await attach_page(cdp)
        else:
            tid = (await cdp.call("Target.createTarget", url="about:blank",
                                  newWindow=True))["result"]["targetId"]
            sid = (await cdp.call("Target.attachToTarget", targetId=tid,
                                  flatten=True))["result"]["sessionId"]
        last = store.last_phrase()
        keep_open = False
        try:
            await cdp.call("Page.bringToFront", sid)
            for i, t in enumerate(todo):
                if control.is_stopped():
                    log("已停止评论")
                    break
                if i and not await _wait(random.uniform(30, 60)):
                    log("已停止评论")
                    break

                await cdp.call("Page.navigate", sid, url=t["note_url"])
                await asyncio.sleep(random.uniform(5.0, 7.0))

                raw = await cdp.val(sid, FIND_JS % (json.dumps(t["name"][:12]),
                                                    json.dumps(t["text"][:10]))) or "{}"
                try:
                    hit = json.loads(raw)
                except Exception:
                    hit = {}
                if not hit.get("ok"):
                    done["failed"] += 1
                    log(f"  {i+1}/{len(todo)} {t['name']} {hit.get('why','定位失败')}，跳过")
                    continue

                # 1) 点这条评论的回复图标
                await _click(cdp, sid, hit["x"], hit["y"])
                await asyncio.sleep(random.uniform(1.2, 1.8))
                st = json.loads(await cdp.val(sid, STATE_JS) or "{}")
                if st.get("needVerify"):
                    keep_open = True
                    log("平台限流：弹了「请求太频繁」，这条没发出去。窗口给你留在前台，30 分钟后再试")
                    store.mark_limited(30)
                    done["failed"] += 1
                    break
                if not st.get("hasEd") or not st.get("ex"):
                    done["failed"] += 1
                    log(f"  {i+1}/{len(todo)} {t['name']} 回复框没出来，跳过")
                    continue

                # 2) 点输入框聚焦，再整段插入
                await _click(cdp, sid, st["ex"], st["ey"])
                await asyncio.sleep(0.5)
                phrase = pick_phrase(phrases, last)
                await cdp.call("Input.insertText", sid, text=phrase)
                await asyncio.sleep(random.uniform(0.9, 1.5))
                st = json.loads(await cdp.val(sid, STATE_JS) or "{}")
                if not st.get("txt"):
                    done["failed"] += 1
                    log(f"  {i+1}/{len(todo)} {t['name']} 文字没输进去，跳过")
                    continue

                # 3) 等按钮可点再点发送
                for _ in range(6):
                    if not st.get("dis") and st.get("bx"):
                        break
                    await asyncio.sleep(0.6)
                    st = json.loads(await cdp.val(sid, STATE_JS) or "{}")
                if st.get("dis") or not st.get("bx"):
                    done["failed"] += 1
                    log(f"  {i+1}/{len(todo)} {t['name']} 发送按钮一直是灰的，跳过")
                    continue

                await _click(cdp, sid, st["bx"], st["by"])
                await asyncio.sleep(random.uniform(2.0, 3.0))
                after = json.loads(await cdp.val(sid, STATE_JS) or "{}")
                if after.get("needVerify"):
                    keep_open = True
                    log("发送被拦：平台限流「请求太频繁」。窗口留在前台，30 分钟后再试")
                    store.mark_limited(30)
                    done["failed"] += 1
                    break
                if not (after.get("txt") or ""):
                    done["ok"] += 1
                    last = phrase
                    store.mark_commented(t.get("key", ""), t["name"], phrase)
                    log(f"  {i+1}/{len(todo)} {t['name']} 已回复：{phrase}")
                else:
                    done["failed"] += 1
                    log(f"  {i+1}/{len(todo)} {t['name']} 点了发送但没清空，可能被拦了")
        finally:
            if keep_open:
                try:
                    await cdp.call("Page.bringToFront", sid)
                except Exception:
                    pass
            elif not EMBED:
                try:
                    await cdp.call("Target.closeTarget", targetId=tid)
                except Exception:
                    pass
    return done
