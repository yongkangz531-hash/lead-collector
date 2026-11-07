#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""关注：只执行你勾选的人，不自动关注任何人。

流程：打开评论者主页（链接来自评论区，自带 xsec_token）→ 找到「关注」按钮 →
真实鼠标点击 → 回读按钮文字确认变成「已关注」→ 记账。
节奏 3-6 秒/人，单批最多 20 人，已关注的自动跳过。
"""
import asyncio
import json
import random
import urllib.request

import websockets

import control
import store
from collect2 import CDP, browser_ws, attach_page, EMBED

PROFILE_STATE_JS = r"""
(function(){
  var btns = [].slice.call(document.querySelectorAll('button, [class*="follow"], [class*="Follow"]'));
  var out = {name:'', text:'', x:0, y:0, found:false};
  var nm = document.querySelector('.user-name, .user-nickname, .info .name, h1');
  if (nm && nm.innerText) out.name = nm.innerText.trim().slice(0,30);
  for (var i=0;i<btns.length;i++){
    var b = btns[i], t = (b.innerText||'').replace(/\s+/g,'');
    if (!t) continue;
    if (t.indexOf('关注') < 0 && t.indexOf('回关') < 0) continue;
    var r = b.getBoundingClientRect();
    if (r.width < 8 || r.height < 8) continue;
    out.found = true; out.text = t;
    out.x = Math.round(r.x + r.width/2); out.y = Math.round(r.y + r.height/2);
    break;
  }
  return JSON.stringify(out);
})()
"""


async def _click(cdp, sid, x, y):
    for k in ("mousePressed", "mouseReleased"):
        await cdp.call("Input.dispatchMouseEvent", sid, type=k, x=x, y=y,
                       button="left", clickCount=1)


async def run(targets, log=print, max_batch=20):
    """targets: [{uid, name, url}]  已关注的会跳过，返回 {ok, skipped, failed}"""
    done = {"ok": 0, "skipped": 0, "failed": 0}
    todo = []
    for t in targets:
        uid = t.get("uid") or ""
        if not uid or not t.get("url"):
            continue
        if store.is_followed(uid):
            done["skipped"] += 1
            log(f"  {t.get('name','')} 已经关注过，跳过")
            continue
        todo.append(t)
    todo = todo[:max_batch]
    if not todo:
        log("没有需要关注的（都关注过了）")
        return done

    log(f"准备关注 {len(todo)} 人")

    async with websockets.connect(browser_ws(), max_size=2 ** 26) as ws:
        cdp = CDP(ws)
        if EMBED:
            tid, sid = await attach_page(cdp)
        else:
            tid = (await cdp.call("Target.createTarget", url="about:blank",
                                  newWindow=True))["result"]["targetId"]
            sid = (await cdp.call("Target.attachToTarget", targetId=tid,
                                  flatten=True))["result"]["sessionId"]
        try:
            await cdp.call("Page.bringToFront", sid)
            for i, t in enumerate(todo):
                while control.is_paused() and not control.is_stopped():
                    await asyncio.sleep(0.8)
                if control.is_stopped():
                    log("已停止关注")
                    break
                if i:
                    await asyncio.sleep(random.uniform(3.0, 6.0))

                await cdp.call("Page.navigate", sid, url=t["url"])
                await asyncio.sleep(random.uniform(3.5, 5.0))
                raw = await cdp.val(sid, PROFILE_STATE_JS) or "{}"
                try:
                    st = json.loads(raw)
                except Exception:
                    st = {}
                name = st.get("name") or t.get("name", "")
                txt = st.get("text", "")
                if not txt:
                    done["failed"] += 1
                    log(f"  {i+1}/{len(todo)} {name} 没找到关注按钮")
                    continue
                if "已关注" in txt or "互相关注" in txt:
                    store.mark_followed(t["uid"], name, t["url"])
                    done["skipped"] += 1
                    log(f"  {i+1}/{len(todo)} {name} 已经是关注状态")
                    continue

                await _click(cdp, sid, st["x"], st["y"])
                await asyncio.sleep(random.uniform(1.2, 2.0))
                raw2 = await cdp.val(sid, PROFILE_STATE_JS) or "{}"
                try:
                    st2 = json.loads(raw2)
                except Exception:
                    st2 = {}
                if "已关注" in (st2.get("text") or "") or "互相关注" in (st2.get("text") or ""):
                    store.mark_followed(t["uid"], name, t["url"])
                    done["ok"] += 1
                    log(f"  {i+1}/{len(todo)} {name} 关注成功")
                else:
                    done["failed"] += 1
                    log(f"  {i+1}/{len(todo)} {name} 点了但没变成已关注（可能需要验证）")
        finally:
            if not EMBED:
                try:
                    await cdp.call("Target.closeTarget", targetId=tid)
                except Exception:
                    pass
    return done


ACCOUNT_JS = r"""
(function(){
  try{
    var st = window.__INITIAL_STATE__ || {};
    var info = st.user && st.user.userInfo;
    var u = (info && (info._rawValue || info)) || {};
    if (u && (u.nickname || u.userId)) {
      return JSON.stringify({ok:true, nickname:u.nickname||'', user_id:u.userId||'',
                             red_id:u.redId||'', avatar:u.images||''});
    }
  }catch(e){}
  return JSON.stringify({ok:false, err:'state empty'});
})()
"""

DOM_ACCOUNT_JS = r"""
(function(){
  var nm = document.querySelector('.user .name, .side-bar .user .name, .nickname, .user-name');
  var a = document.querySelector('.user a[href*="/user/profile/"]');
  var id = '';
  if (a) { var m = (a.getAttribute('href')||'').match(/profile\/([0-9a-f]{8,})/); if (m) id = m[1]; }
  return JSON.stringify({ok: !!(nm && nm.innerText), nickname: nm ? nm.innerText.trim().slice(0,30) : '', user_id: id, avatar: ''});
})()
"""


async def read_account(log=print):
    """读取当前采集浏览器里登录的小红书账号"""
    async with websockets.connect(browser_ws(), max_size=2 ** 26) as ws:
        cdp = CDP(ws)
        tid = (await cdp.call("Target.createTarget", url="https://www.xiaohongshu.com/explore",
                              newWindow=True))["result"]["targetId"]
        sid = (await cdp.call("Target.attachToTarget", targetId=tid,
                              flatten=True))["result"]["sessionId"]
        info = {}
        try:
            await cdp.call("Page.bringToFront", sid)
            await asyncio.sleep(8)
            try:
                raw = await cdp.val(sid, ACCOUNT_JS) or "{}"
                info = json.loads(raw)
            except Exception:
                info = {}
            if not info.get("ok"):
                raw = await cdp.val(sid, DOM_ACCOUNT_JS) or "{}"
                info = json.loads(raw)
        finally:
            if not EMBED:
                try:
                    await cdp.call("Target.closeTarget", targetId=tid)
                except Exception:
                    pass
    return info
