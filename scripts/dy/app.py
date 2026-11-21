#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""抖音采集台 · 本地服务（采集 + 定时监控 + 台账 + 风控）

  python app.py    → http://127.0.0.1:8789

所有采集都经过 guard.py 的闸门：冷却、每小时上限、安静时段、失败退避。
线索落 data/leads.jsonl，按内容哈希去重；已采笔记记在 data/seen_notes.json。
"""
import asyncio
import os
import csv
import io
import json
import re
import threading
import time
import traceback
import websockets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import dy_collect as collect2
import bb
import comment as comment_mod
import control
import dm as dm_mod
import follow
import guard
import store
from score import score_rows, stats

HERE = Path(__file__).parent
CONF = HERE / "data" / "config.json"
PORT = int(os.environ.get("DY_PORT") or 8789)

DEFAULT_CONF = {
    "jobs": [{"kw": "装修", "kw2": [], "notes": 6, "every_min": 60, "enabled": False, "last_run": 0}],
    "filters": ["多少钱", "怎么", "求带", "报价", "价格", "在哪", "推荐", "新手", "货源", "想学"],
    "use_filter": False,
    "collect": {"min_likes": 0, "min_comments": 0, "include": [], "exclude": [],
                "city": "", "only_uid": True, "monitor_min": 30,
                "kw2": [], "date_from": "", "date_to": "",
                "target": 120, "expand": 1,
                "comment_gap": 45, "follow_gap": 6, "realtime_follow": False},
    "dm": {"messages": [
        "看到你在这条视频下面的评论，我这边正好有一份整理好的资料，需要的话我发你",
        "你说的这个情况我遇到过，能少走不少弯路，要不要我把那份清单发你",
        "这个问题我整理过一份对比，直接发你看？",
    ], "gap": 60, "daily_cap": 20},
    "replies": [
        "我之前也纠结过这个，后来找人算了一版，比想的便宜",
        "这个我整理过一份对比，需要的话跟我说",
        "价格差别主要在材料上，多问两家就清楚了",
        "我家刚做完，踩过两个坑，可以跟你说说",
    ],
}

BB = {"id": "", "name": "", "busy": False, "error": ""}
STATE = {"running": False, "step": "idle", "kw": "", "job": "", "notes_done": 0, "notes_total": 0,
         "log": [], "error": "", "started": 0, "elapsed": 0, "new_leads": 0, "paused": False}
LOCK = threading.Lock()

FSTATE = {"running": False, "log": [], "ok": 0, "skipped": 0, "failed": 0, "done": 0, "total": 0}
FLOCK = threading.Lock()

CSTATE = {"running": False, "log": [], "ok": 0, "skipped": 0, "failed": 0, "done": 0, "total": 0}
CLOCK = threading.Lock()
COMMENT_DAILY_CAP = 15

DSTATE = {"running": False, "log": [], "ok": 0, "skipped": 0, "failed": 0, "done": 0, "total": 0}
DLOCK = threading.Lock()


def dlog(msg):
    with DLOCK:
        DSTATE["log"].append(msg)
        DSTATE["log"][:] = DSTATE["log"][-120:]
    print(msg, flush=True)


def dm_worker(targets, messages, gap, daily_cap):
    control.reset()
    with DLOCK:
        DSTATE.update({"running": True, "log": [], "ok": 0, "skipped": 0, "failed": 0,
                       "done": 0, "total": len(targets)})
    try:
        res = asyncio.run(dm_mod.run(targets, messages, log=dlog,
                                     gap=(max(20, gap - 15), gap + 35), daily_cap=daily_cap))
        with DLOCK:
            DSTATE.update({k: res.get(k, DSTATE[k]) for k in ("ok", "skipped", "failed", "done", "total")})
            DSTATE["running"] = False
    except Exception as e:
        with DLOCK:
            DSTATE["running"] = False
            DSTATE["log"].append("出错：" + str(e)[:120])
        traceback.print_exc()


def clog(msg):
    with CLOCK:
        CSTATE["log"].append(msg)
        CSTATE["log"][:] = CSTATE["log"][-120:]
    print(msg, flush=True)


def comment_worker(targets, phrases):
    control.reset()
    with CLOCK:
        CSTATE.update({"running": True, "log": [], "ok": 0, "skipped": 0, "failed": 0,
                       "done": 0, "total": len(targets)})
    try:
        res = asyncio.run(comment_mod.run(targets, phrases, log=clog,
                                          daily_cap=COMMENT_DAILY_CAP))
        with CLOCK:
            CSTATE.update(res)
            CSTATE["running"] = False
            CSTATE["done"] = len(targets)
    except Exception as e:
        with CLOCK:
            CSTATE["running"] = False
            CSTATE["log"].append("出错：" + str(e)[:120])
        traceback.print_exc()


def flog(msg):
    with FLOCK:
        FSTATE["log"].append(msg)
        FSTATE["log"][:] = FSTATE["log"][-120:]
    print(msg, flush=True)


def follow_worker(targets):
    control.reset()
    with FLOCK:
        FSTATE.update({"running": True, "log": [], "ok": 0, "skipped": 0, "failed": 0,
                       "done": 0, "total": len(targets)})
    try:
        res = asyncio.run(follow.run(targets, log=flog))
        with FLOCK:
            FSTATE.update(res)
            FSTATE["running"] = False
            FSTATE["done"] = len(targets)
    except Exception as e:
        with FLOCK:
            FSTATE["running"] = False
            FSTATE["log"].append("出错：" + str(e)[:120])
        traceback.print_exc()


def account_worker():
    try:
        info = asyncio.run(follow.read_account(log=print))
        info["bound_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        if info.get("ok"):
            store.save_account(info)
            print("账号已绑定：" + info.get("nickname", ""), flush=True)
        else:
            print("没读到登录账号", flush=True)
    except Exception as e:
        print("读账号失败：" + str(e)[:120], flush=True)


def apply_filters(rows, conf):
    """按参数过滤：包含词 / 屏蔽词 / 地区 / 视频点赞下限 / 视频评论下限"""
    c = (conf.get("collect") or {})
    inc = [x for x in (c.get("include") or []) if x]
    exc = [x for x in (c.get("exclude") or []) if x]
    city = (c.get("city") or "").strip()
    min_likes = int(c.get("min_likes") or 0)
    min_c = int(c.get("min_comments") or 0)
    out = []
    for r in rows:
        t = r.get("text") or ""
        if inc and not any(k in t for k in inc):
            continue
        if exc and any(k in t for k in exc):
            continue
        if city and city not in (r.get("ip") or ""):
            continue
        if min_likes and collect2._num(r.get("vlikes")) < min_likes:
            continue
        if min_c and collect2._num(r.get("vcomments")) < min_c:
            continue
        out.append(r)
    return out


async def _open_in_browser(url):
    async with websockets.connect(collect2.browser_ws(), max_size=2 ** 26) as ws:
        cdp = collect2.CDP(ws)
        tid, sid = await collect2.attach_page(cdp, url)
        await cdp.call("Page.navigate", sid, url=url)


def note_worker(url, target=None, expand=None):
    t0 = time.time()
    control.reset()
    conf = load_conf()
    c = conf.get("collect") or {}
    target = int(target or c.get("target") or collect2.DEFAULT_TARGET)
    if expand is None:
        expand = c.get("expand")
    expand = int(expand if expand is not None else 1)
    with LOCK:
        STATE.update({"running": True, "kw": "单篇", "job": "单篇", "step": "打开视频",
                      "error": "", "log": [], "started": t0, "notes_done": 0,
                      "notes_total": 1, "new_leads": 0, "paused": False})
    try:
        guard.begin(1, force=True)
        rows, meta = asyncio.run(collect2.run_note(url, log=log, target=target, expand=expand))
        conf = load_conf()
        rows = apply_filters(rows, conf)
        scored = score_rows(rows)
        new = store.add_leads(scored, "单篇", "单篇")
        for r in scored:
            if r.get("note_id"):
                store.mark_note(r["note_id"], r.get("note", ""), "单篇")
        st = stats(scored)
        guard.ok()
        with LOCK:
            STATE["step"] = "完成"
            STATE["new_leads"] = new
        store.add_run({"t": time.strftime("%Y-%m-%d %H:%M:%S"), "job": "单篇", "kw": "单篇",
                       "notes": 1, "rows": len(scored), "new": new, "hi": st["hi"],
                       "sec": int(time.time() - t0), "ok": True})
        log(f"新增 {new} 条线索（单篇 {len(scored)} 条，高意向 {st['hi']}）")
    except Exception as e:
        guard.fail()
        with LOCK:
            STATE["step"] = "出错"
            STATE["error"] = f"{type(e).__name__}: {e}"
        log("出错：" + str(e)[:160])
    finally:
        with LOCK:
            STATE["running"] = False
            STATE["elapsed"] = int(time.time() - t0)


def ctime_days(s):
    """抖音评论时间多为相对时间：刚刚 / 3分钟前 / 2小时前 / 5天前 / 3月前 / 1年前。
    返回大约多少天前（未知返回 99999），用于「最近 N 天」筛选。"""
    s = (s or "").strip()
    if not s:
        return 99999
    if "刚刚" in s or "秒前" in s or "分钟前" in s or "小时前" in s:
        return 0
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        try:
            d = time.mktime(time.strptime(f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}", "%Y-%m-%d"))
            return max(0, int((time.time() - d) / 86400))
        except Exception:
            return 99999
    n = re.search(r"(\d+)", s)
    v = int(n.group(1)) if n else 1
    if "天前" in s:
        return v
    if "周前" in s:
        return v * 7
    if "月前" in s:
        return v * 30
    if "年前" in s:
        return v * 365
    return 99999


def ctime_min(s):
    """相对时间折算成分量（分钟），用于排序；未知给一个很大的数"""
    s = (s or "").strip()
    if not s:
        return 10 ** 9
    if "刚刚" in s or "秒前" in s:
        return 0
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        try:
            d = time.mktime(time.strptime(f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}", "%Y-%m-%d"))
            return int((time.time() - d) / 60)
        except Exception:
            return 10 ** 9
    n = re.search(r"(\d+)", s)
    v = int(n.group(1)) if n else 1
    if "分钟前" in s:
        return v
    if "小时前" in s:
        return v * 60
    if "天前" in s:
        return v * 1440
    if "周前" in s:
        return v * 7 * 1440
    if "月前" in s:
        return v * 30 * 1440
    if "年前" in s:
        return v * 365 * 1440
    return 10 ** 9


def load_conf():
    try:
        c = json.loads(CONF.read_text(encoding="utf-8"))
    except Exception:
        c = {}
    out = dict(DEFAULT_CONF)
    out.update(c or {})
    return out


def save_conf(c):
    CONF.parent.mkdir(exist_ok=True)
    CONF.write_text(json.dumps(c, ensure_ascii=False, indent=1), encoding="utf-8")


async def _login_probe():
    """只看当前抖音页面，不跳转、不刷新（避免打断用户正在看的内容）"""
    import urllib.request as _ur
    import dy_collect as _dc
    try:
        with _ur.urlopen("http://127.0.0.1:%d/json/list" % _dc.PORT, timeout=5) as r:
            tabs = [t for t in json.load(r) if t.get("type") == "page"]
    except Exception as e:
        return {"logged": False, "note": "连不上浏览器：" + str(e)[:40]}
    tabs = [t for t in tabs if "douyin.com" in (t.get("url") or "")]
    if not tabs:
        return {"logged": False, "note": "没有打开抖音页面"}
    async with websockets.connect(tabs[0]["webSocketDebuggerUrl"], max_size=2 ** 24) as ws:
        cdp = _dc.CDP(ws)
        raw = await cdp.val(None, """(function(){
          var t = (document.body && document.body.innerText) || '';
          var need = t.indexOf('扫码登录') >= 0;
          var name = '';
          var el = document.querySelector('[data-e2e="user-info-name"], [class*=nickname]');
          if (el) name = (el.innerText || '').trim().slice(0, 16);
          return JSON.stringify({need: need, name: name, url: location.href.slice(0, 50)});
        })()""")
        try:
            d = json.loads(raw or "{}")
        except Exception:
            d = {}
        logged = not d.get("need", True)
        return {"logged": logged, "nickname": d.get("name") or "",
                "url": d.get("url", ""),
                "note": "已登录" if logged else "未登录，请在左边窗口扫码"}


def log(msg):
    with LOCK:
        STATE["log"].append(f"{time.strftime('%H:%M:%S')}  {msg}")
        STATE["log"] = STATE["log"][-200:]
    print(msg, flush=True)


def worker(kw, notes, job="手动", force=False, kw2_list=None, date_from="", date_to="",
           target=None, expand=None):
    t0 = time.time()
    conf = load_conf()
    c = conf.get("collect") or {}
    kw2_list = [x.strip() for x in (kw2_list if kw2_list is not None else (c.get("kw2") or [])) if str(x).strip()]
    date_from = (date_from if date_from is not None else c.get("date_from") or "").strip()
    date_to = (date_to if date_to is not None else c.get("date_to") or "").strip()
    target = int(target or c.get("target") or collect2.DEFAULT_TARGET)
    if expand is None:
        expand = c.get("expand")
    expand = int(expand if expand is not None else 1)
    control.reset()
    with LOCK:
        STATE.update({"running": True, "kw": kw, "job": job, "step": "搜索", "error": "",
                      "log": [], "started": t0, "notes_done": 0, "notes_total": notes,
                      "new_leads": 0, "paused": False})
    try:
        allow = guard.begin(notes, force=force)
        log(f"风控允许本轮最多 {allow} 篇 · 每条视频目标 {target} 条评论"
            + (f" · 发布时间 {date_from or '不限'} ~ {date_to or '不限'}" if (date_from or date_to) else ""))

        combos = [""] + kw2_list if kw2_list else [""]
        if kw2_list:
            log("二级搜索词：" + "、".join(kw2_list) + f"（共 {len(combos)} 组搜索）")
        per = max(1, allow // len(combos)) if len(combos) > 1 else allow
        all_rows, all_meta = [], []
        for i, k2 in enumerate(combos):
            if control.is_stopped():
                break
            quota = allow if len(combos) == 1 else (per if i < len(combos) - 1 else max(1, allow - per * i))
            if quota <= 0:
                break
            with LOCK:
                STATE["step"] = f"搜索 {kw + ' ' + k2}".strip()
            try:
                rows, meta = asyncio.run(collect2.run(
                    kw, quota, log=log, skip=store.note_seen, kw2=k2,
                    date_from=date_from, date_to=date_to, target=target, expand=expand,
                    min_likes=int(c.get("min_likes") or 0), min_comments=int(c.get("min_comments") or 0)))
            except Exception as e:
                if len(combos) == 1:
                    raise
                log(f"二级词「{k2}」这轮出错，跳过：{str(e)[:80]}")
                continue
            all_rows.extend(rows)
            all_meta.extend(meta)
            with LOCK:
                STATE["notes_done"] = len(all_meta)
        rows, notes_meta = all_rows, all_meta

        rows = apply_filters(rows, conf)
        filters = [f for f in conf.get("filters", []) if f]
        if conf.get("use_filter", True) and filters:
            before = len(rows)
            rows = [r for r in rows if any(f in (r.get("text") or "") for f in filters)]
            log(f"关键词过滤：{before} → {len(rows)} 条")

        scored = score_rows(rows)
        new = store.add_leads(scored, kw, job)
        for r in scored:
            store.add_author(r.get("name", ""), r.get("note", ""))
            if r.get("note_id"):
                store.mark_note(r["note_id"], r.get("note", ""), kw)

        guard.ok()
        st = stats(scored)
        with LOCK:
            STATE["step"] = "完成"
            STATE["new_leads"] = new
        store.add_run({"t": time.strftime("%Y-%m-%d %H:%M:%S"), "job": job, "kw": kw,
                       "notes": len(notes_meta), "rows": len(scored), "new": new,
                       "hi": st["hi"], "sec": int(time.time() - t0), "ok": True})
        log(f"新增 {new} 条线索（本轮 {len(scored)} 条，高意向 {st['hi']}）")
    except Exception as e:
        paused = guard.fail()
        with LOCK:
            STATE["step"] = "出错"
            STATE["error"] = f"{type(e).__name__}: {e}"
        log("出错：" + str(e)[:160] + ("（已触发退避）" if paused else ""))
        traceback.print_exc()
        store.add_run({"t": time.strftime("%Y-%m-%d %H:%M:%S"), "job": job, "kw": kw,
                       "rows": 0, "new": 0, "ok": False, "err": str(e)[:120]})
    finally:
        with LOCK:
            STATE["running"] = False
            STATE["elapsed"] = int(time.time() - t0)


def scheduler():
    """每 20 秒检查一次定时任务"""
    while True:
        time.sleep(20)
        try:
            if STATE["running"]:
                continue
            conf = load_conf()
            for i, job in enumerate(conf.get("jobs", [])):
                if not job.get("enabled"):
                    continue
                every = max(15, int(job.get("every_min", 60)))
                if time.time() - job.get("last_run", 0) < every * 60:
                    continue
                if guard.block_reason():
                    continue
                conf["jobs"][i]["last_run"] = time.time()
                save_conf(conf)
                log(f"定时任务启动：{job['kw']}")
                threading.Thread(target=worker,
                                 args=(job["kw"], int(job.get("notes", 6)), job["kw"]),
                                 kwargs={"kw2_list": job.get("kw2") or None},
                                 daemon=True).start()
                break
        except Exception as e:
            print("调度器异常:", e, flush=True)


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, body: bytes, ctype="application/json; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def do_GET(self):
        p = self.path.split("?")[0]
        q = {k: v[0] for k, v in parse_qs(self.path.split("?", 1)[1]).items()} if "?" in self.path else {}

        if p in ("/", "/index.html"):
            f = HERE / "ui" / "console.html"
            if not f.exists():
                f = HERE / "ui" / "app.html"
            if not f.exists():
                return self._send(404, "界面缺失".encode(), "text/plain; charset=utf-8")
            return self._send(200, f.read_bytes(), "text/html; charset=utf-8")

        if p == "/api/status":
            with LOCK:
                snap = dict(STATE)
            if snap["running"] and snap["started"]:
                snap["elapsed"] = int(time.time() - snap["started"])
            snap["guard"] = guard.snapshot()
            snap["guard"]["hour_count"] = store.notes_this_hour()
            snap["total_leads"] = len(store.all_leads())
            snap["notes_seen"] = store.note_count()
            snap["bb"] = {"name": BB.get("name", ""), "busy": BB.get("busy", False),
                          "error": BB.get("error", ""),
                          "connected": bool(collect2.WS_OVERRIDE.get("ws"))}
            return self._json(snap)

        if p == "/api/leads":
            rows = store.all_leads(q.get("kw") or None, limit=int(q.get("limit", 800)))
            if q.get("intent"):
                rows = [r for r in rows if r.get("intent") == q["intent"]]
            if q.get("kw2"):
                rows = [r for r in rows if r.get("kw2") == q["kw2"]]
            # 时间筛选：field=ts(采集时间) / ctime(评论时间，按相对时间折算天数)
            field = q.get("field") or "ts"
            t_from = (q.get("from") or "").strip()
            t_to = (q.get("to") or "").strip()
            days = int(q.get("days") or 0)
            order = q.get("order") or "desc"
            if field == "ctime":
                if days:
                    rows = [r for r in rows if ctime_days(r.get("ctime")) <= days]
                rows.sort(key=lambda r: ctime_min(r.get("ctime")), reverse=(order == "asc"))
            else:
                if days:
                    cut = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
                    rows = [r for r in rows if str(r.get("ts", ""))[:10] >= cut]
                if t_from:
                    rows = [r for r in rows if str(r.get("ts", ""))[:10] >= t_from]
                if t_to:
                    rows = [r for r in rows if str(r.get("ts", ""))[:10] <= t_to]
                if order == "asc":
                    rows = list(reversed(rows))     # store.all_leads 返回的是倒序（最新在前）
            if q.get("q"):
                needle = q["q"].strip()
                rows = [r for r in rows if needle in (r.get("text") or "") or needle in (r.get("name") or "")]
            fu = []
            for url in (r.get("user_url") or "" for r in rows):
                if "/user/profile/" in url:          # 小红书
                    fu.append(url.split("/user/profile/")[1].split("?")[0])
                elif "/user/" in url:                # 抖音
                    fu.append(url.split("/user/")[1].split("?")[0].strip("/"))
            fl = store.followed()
            cm = store.commented()
            dmd = store.dm_all()
            return self._json({"rows": rows, "stats": stats(rows),
                               "followed": [u for u in set(fu) if u in fl],
                               "dmed": [u for u in set(fu) if u in dmd],
                               "commented": [k for k in (r.get("key") for r in rows) if k in cm]})

        if p == "/api/login":
            try:
                info = asyncio.run(_login_probe())
            except Exception as e:
                info = {"logged": False, "note": "检查失败：" + str(e)[:60]}
            return self._json(info)

        if p == "/api/csv":
            rows = store.all_leads(q.get("kw") or None, limit=100000)
            if q.get("intent"):
                rows = [r for r in rows if r.get("intent") == q["intent"]]
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["时间", "关键词", "昵称", "平台", "笔记", "评论", "意向", "需求点", "话术"])
            for r in rows:
                w.writerow([r.get("ts", ""), r.get("kw", ""), r.get("name", ""), r.get("platform", ""),
                            r.get("note", ""), r.get("text", ""), r.get("intent", ""),
                            r.get("need", ""), r.get("reply", "")])
            data = ("\ufeff" + buf.getvalue()).encode("utf-8")
            return self._send(200, data, "text/csv; charset=utf-8",
                              {"Content-Disposition": 'attachment; filename="dy_leads.csv"'})

        if p == "/api/config":
            return self._json(load_conf())

        if p == "/api/authors":
            return self._json({"rows": store.authors()[:60]})

        if p == "/api/account":
            return self._json({"account": store.account(), "followed": len(store.followed())})

        if p == "/api/bb/profiles":
            try:
                ps = bb.profiles()
                return self._json({"ok": True, "alive": True, "profiles": ps,
                                   "current": BB.get("name", ""), "error": BB.get("error", "")})
            except Exception as e:
                return self._json({"ok": False, "alive": bb.alive(), "profiles": [],
                                   "error": str(e)[:100], "current": BB.get("name", "")})

        if p == "/api/notes":
            return self._json({"rows": store.notes(int(q.get("limit", 300)))})

        if p == "/api/export":
            kind = q.get("type") or "notes"
            buf = io.StringIO()
            w = csv.writer(buf)
            if kind == "notes":
                w.writerow(["序号", "发布时间", "视频标题", "作者", "视频链接", "点赞", "评论", "收藏", "分享", "标签", "采集时间"])
                for i, n in enumerate(store.notes(100000), 1):
                    w.writerow([i, n.get("date", ""), n.get("title", ""), n.get("author", ""), n.get("url", ""),
                                n.get("likes", ""), n.get("comments", ""), n.get("collects", ""),
                                n.get("shares", ""), n.get("tags", ""), n.get("ts", "")])
            else:
                w.writerow(["序号", "昵称", "用户uid", "IP属地", "评论时间", "评论内容",
                            "意向", "需求点", "话术", "视频标题", "视频作者", "二级词",
                            "关注状态", "评论状态", "已私信"])
                fw = store.followed()
                cm = store.commented()
                dm = store.dm_all()
                for i, r in enumerate(store.all_leads(limit=100000), 1):
                    uid = (r.get("user_url") or "").split("/user/")[-1].split("?")[0].strip("/")
                    w.writerow([i, r.get("name", ""), uid, r.get("ip", ""), r.get("ctime", ""),
                                r.get("text", ""), r.get("intent", ""), r.get("need", ""),
                                r.get("reply", ""), r.get("note", ""), r.get("author", ""),
                                r.get("kw2", ""),
                                "已关注" if uid in fw else "", "已评论" if r.get("key") in cm else "",
                                "已私信" if uid in dm else ""])
            data = ("\ufeff" + buf.getvalue()).encode("utf-8")
            return self._send(200, data, "text/csv; charset=utf-8",
                              {"Content-Disposition": f'attachment; filename="dy_{kind}.csv"'})

        if p == "/api/comment/status":
            with CLOCK:
                snap = dict(CSTATE)
            snap["today"] = store.comments_today()
            snap["limit_left_min"] = store.limit_left_min()
            snap["cap"] = COMMENT_DAILY_CAP
            snap["bb"] = {"name": BB.get("name", ""), "busy": BB.get("busy", False),
                          "error": BB.get("error", ""),
                          "connected": bool(collect2.WS_OVERRIDE.get("ws"))}
            return self._json(snap)

        if p == "/api/follow/status":
            with FLOCK:
                snap = dict(FSTATE)
            snap["bb"] = {"name": BB.get("name", ""), "busy": BB.get("busy", False),
                          "error": BB.get("error", ""),
                          "connected": bool(collect2.WS_OVERRIDE.get("ws"))}
            return self._json(snap)

        if p == "/api/dm/status":
            with DLOCK:
                snap = dict(DSTATE)
            snap["today"] = store.dm_today()
            snap["total"] = store.dm_total()
            snap["cap"] = (load_conf().get("dm") or {}).get("daily_cap", 20)
            return self._json(snap)

        if p == "/api/runs":
            return self._json({"rows": store.recent_runs(40)})

        return self._send(404, b'{"error":"not found"}')

    def do_POST(self):
        p = self.path.split("?")[0]
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            try:
                body = json.loads(raw.decode("utf-8"))
            except UnicodeDecodeError:
                body = json.loads(raw.decode("gbk"))
        except Exception:
            return self._json({"error": "请求体不是 JSON"}, 400)

        if p == "/api/bb/open":
            bid = (body.get("id") or "").strip()
            name = (body.get("name") or "").strip()
            if not bid:
                return self._json({"error": "没选环境"}, 400)
            if BB.get("busy"):
                return self._json({"error": "正在打开上一个环境，稍等"}, 409)

            def _open():
                BB.update({"busy": True, "error": "", "id": bid, "name": name})
                log(f"正在打开比特环境：{name or bid}（首次可能要 1-3 分钟）")
                try:
                    d = bb.open_profile(bid)
                    ws = d.get("ws") or ""
                    if not ws:
                        raise RuntimeError("比特没有返回 CDP 地址")
                    collect2.WS_OVERRIDE["ws"] = ws
                    log(f"环境已连接：{name or bid}")
                except Exception as e:
                    BB["error"] = str(e)[:120]
                    collect2.WS_OVERRIDE["ws"] = ""
                    log("打开环境失败：" + str(e)[:120])
                finally:
                    BB["busy"] = False

            threading.Thread(target=_open, daemon=True).start()
            return self._json({"ok": True})

        if p == "/api/bb/close":
            try:
                bb.close_profile(BB.get("id") or "")
            except Exception:
                pass
            collect2.WS_OVERRIDE["ws"] = ""
            BB.update({"id": "", "name": "", "error": ""})
            log("已断开比特环境")
            return self._json({"ok": True})

        if p == "/api/collect":
            kw = (body.get("kw") or "").strip()
            notes = int(body.get("notes") or 6)
            if not kw:
                return self._json({"error": "关键词不能为空"}, 400)
            with LOCK:
                busy = STATE["running"]
                was_paused = STATE.get("paused")
            if busy:
                if not was_paused:
                    return self._json({"error": "正在采集，先暂停或停止"}, 409)
                # 暂停状态下换了关键词：停掉旧的，直接开新的
                control.stop()
                for _ in range(16):
                    time.sleep(0.5)
                    with LOCK:
                        if not STATE["running"]:
                            break
            reason = guard.block_reason(force=bool(body.get("force")), skip_cool=bool(body.get("skip_cool")))
            if reason:
                return self._json({"error": reason}, 429)
            kw2 = body.get("kw2")
            if isinstance(kw2, str):
                kw2 = [x.strip() for x in re.split(r"[,，\n]", kw2) if x.strip()]
            threading.Thread(target=worker,
                             args=(kw, notes, "手动", bool(body.get("force"))),
                             kwargs={"kw2_list": kw2 if isinstance(kw2, list) else None,
                                     "date_from": body.get("date_from"),
                                     "date_to": body.get("date_to"),
                                     "target": body.get("target"),
                                     "expand": body.get("expand")},
                             daemon=True).start()
            return self._json({"ok": True})

        if p == "/api/control":
            act = (body.get("action") or "").strip()
            if act == "pause":
                control.pause()
            elif act == "resume":
                control.resume()
            elif act == "stop":
                control.stop()
            elif act == "reset":
                control.reset()
            else:
                return self._json({"error": "action 只能是 pause / resume / stop"}, 400)
            with LOCK:
                STATE["paused"] = control.is_paused()
            return self._json({"ok": True, "state": control.state()})

        if p == "/api/login":
            # 读当前窗格的登录状态（不跳转、不刷新，只看 DOM）
            try:
                import asyncio as _aio
                info = _aio.run(_login_probe())
            except Exception as e:
                info = {"logged": False, "note": f"检查失败：{e}"}
            return self._json(info)

        if p == "/api/pace":
            mode = (body.get("mode") or "normal").lower()
            cfg = guard._load()
            if mode == "safe":
                cfg["min_gap"], cfg["max_gap"], cfg["long_rest"] = 16.0, 26.0, [60, 100]
            elif mode == "slow":
                cfg["min_gap"], cfg["max_gap"], cfg["long_rest"] = 25.0, 40.0, [90, 150]
            else:
                cfg["min_gap"], cfg["max_gap"], cfg["long_rest"] = 8.0, 15.0, [35, 60]
            guard._save(cfg)
            log(f"采集节奏已切换：{mode}（条间 {cfg['min_gap']:.0f}-{cfg['max_gap']:.0f} 秒）")
            return self._json({"ok": True, "mode": mode, "gap": [cfg["min_gap"], cfg["max_gap"]]})

        if p == "/api/account/refresh":
            threading.Thread(target=account_worker, daemon=True).start()
            return self._json({"ok": True})

        if p == "/api/follow":
            keys = body.get("keys") or []
            if not isinstance(keys, list) or not keys:
                return self._json({"error": "没有勾选任何人"}, 400)
            with FLOCK:
                if FSTATE["running"]:
                    return self._json({"error": "上一批还在关注中"}, 409)
            by_key = {r.get("key"): r for r in store.all_leads(limit=100000)}
            targets = []
            for k in keys:
                r = by_key.get(k)
                if not r:
                    continue
                url = r.get("user_url") or ""
                uid = ""
                if "/user/profile/" in url:          # 小红书
                    uid = url.split("/user/profile/")[1].split("?")[0]
                elif "/user/" in url:                # 抖音：/user/MS4wLjABAAAA...
                    uid = url.split("/user/")[1].split("?")[0].strip("/")
                if not uid:
                    continue
                targets.append({"uid": uid, "name": r.get("name", ""), "url": url})
            if not targets:
                return self._json({"error": "勾选的这些没有主页链接，先重新采集一批"}, 400)
            threading.Thread(target=follow_worker, args=(targets,), daemon=True).start()
            return self._json({"ok": True, "count": len(targets)})

        if p == "/api/open":
            url = (body.get("url") or "").strip()
            if not url:
                return self._json({"error": "地址为空"}, 400)
            try:
                asyncio.run(_open_in_browser(url))
                return self._json({"ok": True})
            except Exception as e:
                return self._json({"error": str(e)[:120]}, 500)

        if p == "/api/clear":
            with LOCK:
                if STATE["running"]:
                    return self._json({"error": "正在采集，先停止再清空"}, 409)
            import store as _st
            removed = 0
            try:
                if _st.LEADS.exists():
                    with _st.LEADS.open(encoding="utf-8") as f:
                        removed = sum(1 for line in f if line.strip())
                    _st.LEADS.write_text("", encoding="utf-8")
                if _st.NOTES.exists():
                    _st.NOTES.write_text("", encoding="utf-8")
                _st.SEEN.write_text("{}", encoding="utf-8")
            except Exception as e:
                return self._json({"error": f"清空失败：{e}"}, 500)
            log(f"已清空列表数据：删除 {removed} 条线索、作品表与已采记录")
            return self._json({"ok": True, "removed": removed})

        if p == "/api/collect_note":
            url = (body.get("url") or "").strip()
            if "/video/" not in url:
                return self._json({"error": "作品地址不对，应该是 https://www.douyin.com/video/..."}, 400)
            with LOCK:
                if STATE["running"]:
                    return self._json({"error": "正在跑别的任务"}, 409)
            threading.Thread(target=note_worker, args=(url,),
                             kwargs={"target": body.get("target"), "expand": body.get("expand")},
                             daemon=True).start()
            return self._json({"ok": True})

        if p == "/api/dm":
            keys = body.get("keys") or []
            if body.get("high_intent"):
                keys = [r.get("key") for r in store.all_leads(limit=100000)
                        if r.get("intent") == "高" and r.get("user_url")]
            if not keys:
                return self._json({"error": "没有可私信的对象（先勾选，或采到高意向线索）"}, 400)
            with DLOCK:
                if DSTATE["running"]:
                    return self._json({"error": "上一批私信还在发"}, 409)
            conf = load_conf()
            dmc = conf.get("dm") or {}
            cap = int(dmc.get("daily_cap") or 20)
            if store.dm_today() >= cap:
                return self._json({"error": f"今天已私信 {store.dm_today()} 条，到日上限 {cap} 了"}, 429)
            msgs = [m for m in (dmc.get("messages") or []) if str(m).strip()]
            if not msgs:
                return self._json({"error": "先在「私信内容」里写几句要发的话"}, 400)
            by_key = {r.get("key"): r for r in store.all_leads(limit=100000)}
            targets, seen_uid = [], set()
            for k in keys:
                r = by_key.get(k)
                if not r:
                    continue
                url = r.get("user_url") or ""
                uid = url.split("/user/")[-1].split("?")[0].strip("/") if "/user/" in url else ""
                if not uid or uid in seen_uid:
                    continue
                seen_uid.add(uid)
                targets.append({"uid": uid, "name": r.get("name", ""),
                                "text": r.get("text", ""), "reply": r.get("reply", "")})
            if not targets:
                return self._json({"error": "勾选的这些没有主页链接，先重新采集一批"}, 400)
            # 个性化：把对方原评论和话术接上，避免全是同一句话
            for t in targets:
                t["msg"] = ""
            threading.Thread(target=dm_worker,
                             args=(targets, msgs, int(dmc.get("gap") or 60), cap),
                             daemon=True).start()
            return self._json({"ok": True, "count": len(targets)})

        if p == "/api/comment":
            keys = body.get("keys") or []
            if body.get("high_intent"):
                with LOCK:
                    pass
                keys = [r.get("key") for r in store.all_leads(limit=100000)
                        if r.get("intent") == "高" and r.get("note_url")]
            if not keys:
                return self._json({"error": "没有可评论的目标（要带笔记链接的高意向线索）"}, 400)
            with CLOCK:
                if CSTATE["running"]:
                    return self._json({"error": "上一批评论还没跑完"}, 409)
            # 同一个账号 10 分钟内别既采集又评论，动作混着做最容易触发验证
            runs = store.recent_runs(1)
            if runs:
                try:
                    last = time.mktime(time.strptime(runs[0]["t"], "%Y-%m-%d %H:%M:%S"))
                    if time.time() - last < 600:
                        return self._json({"error": "刚采集完，等 10 分钟再评论（同一账号别同时做两种动作）"}, 429)
                except Exception:
                    pass
            left = store.limit_left_min()
            if left > 0:
                return self._json({"error": f"评论被平台限流，还要等 {left} 分钟（不是要扫码，是频率限制）"}, 429)
            if store.comments_today() >= COMMENT_DAILY_CAP:
                return self._json({"error": f"今天已评论 {store.comments_today()} 条，到上限了"}, 429)
            by_key = {r.get("key"): r for r in store.all_leads(limit=100000)}
            targets = []
            for k in keys:
                r = by_key.get(k)
                if not r or not r.get("note_url"):
                    continue
                if store.is_commented(k):
                    continue
                targets.append({"key": k, "note_url": r["note_url"], "name": r.get("name", ""),
                                "text": r.get("text", "")})
            if not targets:
                return self._json({"error": "勾选的都评论过了，或者缺笔记链接"}, 400)
            phrases = load_conf().get("replies") or []
            threading.Thread(target=comment_worker, args=(targets, phrases), daemon=True).start()
            return self._json({"ok": True, "count": len(targets)})

        if p == "/api/config":
            conf = load_conf()
            if "jobs" in body:
                conf["jobs"] = body["jobs"]
            if "collect" in body:
                cur = conf.get("collect") or {}
                cur.update(body["collect"] or {})
                conf["collect"] = cur
            if "dm" in body:
                cur = conf.get("dm") or {}
                cur.update(body["dm"] or {})
                conf["dm"] = cur
            if "replies" in body:
                conf["replies"] = [str(x).strip() for x in body["replies"] if str(x).strip()]
            if "filters" in body:
                conf["filters"] = [str(x).strip() for x in body["filters"] if str(x).strip()]
            if "use_filter" in body:
                conf["use_filter"] = bool(body["use_filter"])
            save_conf(conf)
            return self._json(conf)

        return self._json({"error": "not found"}, 404)

    def log_message(self, *a):
        pass


class Server(ThreadingHTTPServer):
    # Windows 上 allow_reuse_address=True 会让同一个端口被绑定多次，
    # 出现「新旧两个服务同时在线、请求随机落到其中一个」的怪问题（状态在采集中/待命之间乱跳）。
    allow_reuse_address = False
    daemon_threads = True


def _port_busy():
    import urllib.request as _ur
    try:
        with _ur.urlopen(f"http://127.0.0.1:{PORT}/api/status", timeout=1.5) as r:
            return r.status == 200
    except Exception:
        return False


if __name__ == "__main__":
    if _port_busy():
        print(f"已有采集服务在 {PORT} 端口运行，本进程退出（避免两个服务抢同一个端口）", flush=True)
        raise SystemExit(0)
    threading.Thread(target=scheduler, daemon=True).start()
    print(f"抖音采集台已启动 → http://127.0.0.1:{PORT}")
    Server(("127.0.0.1", PORT), H).serve_forever()
