#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""本地台账：线索去重、已采笔记、作者、任务记录。

全部落在 data/ 下，纯 JSON/JSONL，可随时手改或删除。
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

DATA = Path(__file__).parent / "data"
DATA.mkdir(exist_ok=True)
LEADS = DATA / "leads.jsonl"
SEEN = DATA / "seen_notes.json"
RUNS = DATA / "runs.jsonl"
AUTHORS = DATA / "authors.json"

_LCK = threading.Lock()


def _read_json(fp: Path, default):
    try:
        return json.loads(fp.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(fp: Path, obj):
    tmp = fp.with_suffix(fp.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(fp)


def lead_key(r: dict) -> str:
    raw = f"{r.get('note','')}|{r.get('name','')}|{r.get('text','')}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def add_leads(rows, kw: str, job: str = ""):
    """写入线索，按内容哈希去重；返回真正新增的条数"""
    with _LCK:
        seen = {lead_key(x) for x in _iter_leads()} if LEADS.exists() else set()
        new = 0
        with LEADS.open("a", encoding="utf-8") as f:
            for r in rows:
                k = lead_key(r)
                if k in seen:
                    continue
                seen.add(k)
                rec = dict(r)
                rec.update({"kw": kw, "job": job, "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "key": k})
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                new += 1
        return new


def _iter_leads():
    if not LEADS.exists():
        return
    with LEADS.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue


def all_leads(kw: str | None = None, limit: int = 800):
    rows = []
    for r in _iter_leads():
        if kw and r.get("kw") != kw:
            continue
        rows.append(r)
    rows.reverse()
    return rows[:limit]


def note_seen(note_id: str) -> bool:
    seen = _read_json(SEEN, {})
    return note_id in seen


def mark_note(note_id: str, title: str = "", kw: str = ""):
    with _LCK:
        seen = _read_json(SEEN, {})
        seen[note_id] = {"t": time.strftime("%Y-%m-%d %H:%M:%S"), "title": title[:60], "kw": kw}
        if len(seen) > 5000:
            drop = sorted(seen.items(), key=lambda kv: kv[1].get("t", ""))[:1000]
            for k, _ in drop:
                seen.pop(k, None)
        _write_json(SEEN, seen)


def note_count() -> int:
    return len(_read_json(SEEN, {}))


def add_run(rec: dict):
    with _LCK:
        with RUNS.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def recent_runs(n: int = 30):
    if not RUNS.exists():
        return []
    lines = RUNS.read_text(encoding="utf-8").strip().split("\n")
    out = []
    for line in lines[-n:]:
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    out.reverse()
    return out


def add_author(name: str, note: str = ""):
    """记录出现过的作者（从评论昵称/笔记反查），用于「作者」视角看线索"""
    if not name:
        return
    with _LCK:
        a = _read_json(AUTHORS, {})
        rec = a.get(name) or {"name": name, "first": time.strftime("%Y-%m-%d %H:%M:%S"), "notes": []}
        if note and note not in rec["notes"]:
            rec["notes"] = (rec["notes"] + [note])[-20:]
        rec["last"] = time.strftime("%Y-%m-%d %H:%M:%S")
        a[name] = rec
        _write_json(AUTHORS, a)


def authors():
    a = _read_json(AUTHORS, {})
    return sorted(a.values(), key=lambda x: x.get("last", ""), reverse=True)


def notes_this_hour() -> int:
    """本小时已采篇数（以台账里笔记时间为准，比内存计数可靠）"""
    prefix = time.strftime("%Y-%m-%d %H")
    seen = _read_json(SEEN, {})
    n = 0
    for v in seen.values():
        if str(v.get("t", ""))[:13] == prefix:
            n += 1
    return n


# ---------- 关注台账 ----------
FOLLOWED = DATA / "followed.json"
ACCOUNT = DATA / "account.json"


def account():
    return _read_json(ACCOUNT, {})


def save_account(info: dict):
    with _LCK:
        _write_json(ACCOUNT, info)


def followed() -> dict:
    return _read_json(FOLLOWED, {})


def is_followed(uid: str) -> bool:
    return uid in followed()


def mark_followed(uid: str, name: str = "", url: str = ""):
    with _LCK:
        d = _read_json(FOLLOWED, {})
        d[uid] = {"t": time.strftime("%Y-%m-%d %H:%M:%S"), "name": name[:30], "url": url}
        _write_json(FOLLOWED, d)


# ---------- 评论台账 ----------
COMMENTED = DATA / "commented.json"


def commented() -> dict:
    return _read_json(COMMENTED, {})


def is_commented(key: str) -> bool:
    return key in commented()


def mark_commented(key: str, name: str = "", phrase: str = ""):
    with _LCK:
        d = _read_json(COMMENTED, {})
        d[key] = {"t": time.strftime("%Y-%m-%d %H:%M:%S"), "name": name[:30], "phrase": phrase[:60]}
        _write_json(COMMENTED, d)


def last_phrase() -> str:
    d = _read_json(COMMENTED, {})
    if not d:
        return ""
    last = sorted(d.values(), key=lambda v: v.get("t", ""))
    return last[-1].get("phrase", "") if last else ""


def comments_today() -> int:
    d = _read_json(COMMENTED, {})
    day = time.strftime("%Y-%m-%d")
    return sum(1 for v in d.values() if str(v.get("t", ""))[:10] == day)


LIMIT = DATA / "comment_limit.json"


def mark_limited(minutes: int = 30):
    with _LCK:
        _write_json(LIMIT, {"until": time.time() + minutes * 60,
                            "t": time.strftime("%Y-%m-%d %H:%M:%S")})


def limit_left_min() -> int:
    d = _read_json(LIMIT, {})
    left = int(d.get("until", 0) - time.time())
    return max(0, (left + 59) // 60)


# ---------- 作品（笔记）表 ----------
NOTES = DATA / "notes.jsonl"


def upsert_note(n: dict):
    """按 note_id 覆盖写入作品信息（同一篇只留一条最新的）"""
    with _LCK:
        rows = []
        if NOTES.exists():
            with NOTES.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        continue
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        nid = n.get("id") or n.get("note_id") or ""
        kept = [r for r in rows if (r.get("id") or "") != nid]
        rec = dict(n)
        rec.update({"id": nid, "ts": now})
        kept.append(rec)
        with NOTES.open("w", encoding="utf-8") as f:
            for r in kept:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")


def notes(limit: int = 500):
    if not NOTES.exists():
        return []
    out = []
    with NOTES.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    out.sort(key=lambda x: x.get("ts", ""), reverse=True)
    return out[:limit]
