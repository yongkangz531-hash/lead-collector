# -*- coding: utf-8 -*-
"""抖音版：人工选择性关注

规则（跟需求一致）：
  - 只关注你点了按钮的那个人，不自动关注、不批量瞎点
  - 真实鼠标点击页面上的「关注」按钮，不改页面脚本、不伪造请求
  - 已关注的跳过；每人之间随机间隔；单批上限 20 人
"""
import asyncio
import json
import random
import urllib.request

import websockets

import store
from dy_collect import CDP, attach_page, browser_ws, _wait

MAX_BATCH = 20

# 看按钮状态：返回 {text, x, y, found}
# 注意：抖音的「关注」按钮在窄窗口里经常被挤出右边（x > innerWidth），
# 直接按坐标点会点空。所以先 scrollIntoView 把按钮滚进视口再取坐标。
STATE_JS = """(function(){
  function pick() {
    var cands = ['[data-e2e="user-info-follow-btn"]', '[data-e2e="user-detail-follow-btn"]'];
    for (var i = 0; i < cands.length; i++) {
      var e = document.querySelector(cands[i]);
      if (e) return e;
    }
    var btns = [].slice.call(document.querySelectorAll('button, div[role="button"]'));
    for (var j = 0; j < btns.length; j++) {
      var t = (btns[j].innerText || '').trim();
      if (t === '关注' && !btns[j].closest('nav,header')) return btns[j];
    }
    return null;
  }
  var el = pick();
  if (!el) return JSON.stringify({found: false});
  try { el.scrollIntoView({block: 'center', inline: 'center'}); } catch (e) {}
  var r = el.getBoundingClientRect();
  var cx = Math.round(r.x + r.width / 2), cy = Math.round(r.y + r.height / 2);
  return JSON.stringify({
    found: true,
    text: (el.innerText || '').trim().slice(0, 8),
    x: cx, y: cy,
    inView: (cx > 2 && cx < innerWidth - 2 && cy > 2 && cy < innerHeight - 2),
    w: Math.round(r.width), h: Math.round(r.height)
  });
})()"""

BLOCK_JS = """(function(){
  var t = (document.body && document.body.innerText) || '';
  if (/扫码登录|登录后查看/.test(t)) return 'login';
  if (/安全验证|滑块|操作频繁|请求太频繁/.test(t)) return 'verify';
  return '';
})()"""


async def _mouse(cdp, sid, x, y):
    await cdp.call("Input.dispatchMouseEvent", sid, type="mouseMoved", x=x, y=y)
    await asyncio.sleep(0.18)
    await cdp.call("Input.dispatchMouseEvent", sid, type="mousePressed", x=x, y=y,
                   button="left", clickCount=1)
    await cdp.call("Input.dispatchMouseEvent", sid, type="mouseReleased", x=x, y=y,
                   button="left", clickCount=1)


async def _follow_one(cdp, sid, t):
    url = t.get("url") or ("https://www.douyin.com/user/" + (t.get("uid") or ""))
    await cdp.call("Page.navigate", sid, url=url)
    if not await _wait(random.uniform(5.5, 7.5)):
        return "fail", "页面没打开"

    blocked = await cdp.val(sid, BLOCK_JS)
    if blocked == "login":
        raise RuntimeError("抖音掉登录了，先去左边窗口扫码")
    if blocked == "verify":
        raise RuntimeError("平台要验证了（滑块/频繁），停手，过几小时再来")

    for attempt in range(3):
        st = json.loads(await cdp.val(sid, STATE_JS) or "{}")
        if not st.get("found"):
            if attempt < 2:
                await asyncio.sleep(1.5)
                continue
            return "fail", "没找到关注按钮"
        txt = st.get("text") or ""
        if "已关注" in txt or "互相关注" in txt:
            return "skip", "已经是关注状态"
        if st.get("w", 0) < 8 or st.get("h", 0) < 8 or not st.get("inView", True):
            await asyncio.sleep(1.2)
            continue
        await _mouse(cdp, sid, st["x"], st["y"])
        await asyncio.sleep(random.uniform(1.4, 2.2))
        st2 = json.loads(await cdp.val(sid, STATE_JS) or "{}")
        t2 = st2.get("text") or ""
        if "已关注" in t2 or "互相关注" in t2:
            return "ok", "已关注"
        if attempt == 2:
            return "fail", "点了没变化（" + (t2 or "?") + "）"
        await asyncio.sleep(1.0)
    return "fail", "多次尝试未成功"


async def run(targets, log=print, max_batch=MAX_BATCH):
    """targets: [{uid, name, url}]；返回 {ok, skipped, failed, log}"""
    done = {"ok": 0, "skipped": 0, "failed": 0, "log": []}

    def lg(msg):
        done["log"].append(msg)
        log(msg)

    todo = []
    for t in targets:
        uid = t.get("uid") or ""
        if not uid:
            continue
        if store.is_followed(uid):
            done["skipped"] += 1
            lg("  %s 关注过了，跳过" % (t.get("name") or ""))
            continue
        todo.append(t)
    todo = todo[:max_batch]
    if not todo:
        lg("没有要关注的人（都关注过了）")
        return done

    lg("准备关注 %d 人（你手动选的那几个）" % len(todo))
    async with websockets.connect(browser_ws(), max_size=2 ** 26) as ws:
        cdp = CDP(ws)
        tid, sid = await attach_page(cdp, "https://www.douyin.com/")
        for i, t in enumerate(todo, 1):
            name = t.get("name") or ""
            try:
                status, msg = await _follow_one(cdp, sid, t)
            except Exception as e:
                lg("  %d/%d %s 出错：%s" % (i, len(todo), name, str(e)[:60]))
                done["failed"] += 1
                if "登录" in str(e) or "验证" in str(e):
                    break
                continue
            if status == "ok":
                done["ok"] += 1
                store.mark_followed(t.get("uid", ""), name, t.get("url", ""))
                lg("  %d/%d %s 已关注" % (i, len(todo), name))
            elif status == "skip":
                done["skipped"] += 1
                store.mark_followed(t.get("uid", ""), name, t.get("url", ""))
                lg("  %d/%d %s %s" % (i, len(todo), name, msg))
            else:
                done["failed"] += 1
                lg("  %d/%d %s 失败：%s" % (i, len(todo), name, msg))
            if i < len(todo):
                await _wait(random.uniform(3.5, 6.5))
    lg("关注完成：成功 %d / 跳过 %d / 失败 %d" % (done["ok"], done["skipped"], done["failed"]))
    return done


async def read_account(log=print):
    """读当前抖音登录状态（不跳转、不改页面）"""
    from dy_collect import PORT
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/json/list" % PORT, timeout=5) as r:
            tabs = [t for t in json.load(r) if t.get("type") == "page"]
    except Exception as e:
        return {"ok": False, "nickname": "", "note": "连不上浏览器：" + str(e)[:40]}
    tabs = [t for t in tabs if "douyin.com" in (t.get("url") or "")]
    if not tabs:
        return {"ok": False, "nickname": "", "note": "没有抖音页面"}
    async with websockets.connect(tabs[0]["webSocketDebuggerUrl"], max_size=2 ** 24) as ws:
        cdp = CDP(ws)
        raw = await cdp.val(None, """(function(){
          var t = (document.body && document.body.innerText) || '';
          var need = t.indexOf('扫码登录') >= 0;
          var name = '';
          var el = document.querySelector('[data-e2e="user-info-name"], [class*=nickname]');
          if (el) name = (el.innerText || '').trim().slice(0, 16);
          return JSON.stringify({need: need, name: name});
        })()""")
        d = json.loads(raw or "{}")
        ok = not d.get("need", True)
        return {"ok": ok, "nickname": d.get("name") or "抖音账号",
                "note": "已登录" if ok else "未登录"}


def is_following(url):
    return False
