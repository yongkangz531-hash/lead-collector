#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""风控闸门：所有采集请求都必须过这里。

我在这一层的取舍（写死在代码里，也写进说明）：
  1. 只读：只打开笔记读公开评论，不点赞、不关注、不发私信
     （原软件的"关注留痕"是写操作，最容易触发风控，我不做自动关注，只做记录）
  2. 单轮最多 15 篇；每小时最多 30 篇；两次任务之间至少冷却 10 分钟
  3. 每篇之间 6-9 秒随机；每 4 篇插一次 25-45 秒长休息
  4. 23:00-07:00 不跑（这个时间点抓评论不像真人）
  5. 连续 2 次失败 → 暂停 30 分钟（失败退避）
  6. 不并发：同一时刻只允许一个任务；不绕验证码、不伪造签名、不改指纹
"""
from __future__ import annotations

import json
import random
import threading
import time
from pathlib import Path

DATA = Path(__file__).parent / "data"
DATA.mkdir(exist_ok=True)
FP = DATA / "guard.json"
_LCK = threading.Lock()

DEFAULTS = {
    "min_gap": 6.0,            # 每篇之间最少秒
    "max_gap": 9.0,            # 每篇之间最多秒
    "long_rest_every": 4,      # 每 N 篇长休息一次
    "long_rest": [25, 45],     # 长休息区间
    "max_notes_run": 15,       # 单轮硬上限
    "hourly_cap": 30,          # 每小时硬上限
    "cooldown_min": 10,        # 两次任务之间冷却（分钟）
    "quiet": [23, 7],          # 安静时段（起, 止）
    "fail_pause_min": 30,      # 失败后退避（分钟）
    "fail_streak_limit": 2,    # 连续失败几次触发退避
}


def _load() -> dict:
    try:
        d = json.loads(FP.read_text(encoding="utf-8"))
    except Exception:
        d = {}
    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in d.items() if k in DEFAULTS or k in ("hour_bucket", "counts", "last_run", "fail_streak", "pause_until", "last_note_at")})
    return cfg


def _save(d: dict):
    tmp = FP.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(FP)


def _now() -> float:
    return time.time()


def in_quiet(d: dict | None = None) -> bool:
    d = d or _load()
    a, b = d["quiet"]
    h = time.localtime().tm_hour
    if a <= b:
        return a <= h < b
    return h >= a or h < b


def block_reason(force: bool = False, skip_cool: bool = False) -> str:
    """返回不能跑的原因；空字符串表示可以跑"""
    with _LCK:
        d = _load()
        if in_quiet(d) and not force:
            a, b = d["quiet"]
            return f"安静时段（{a}:00-{b}:00 不采集）"
        if d.get("pause_until", 0) > _now():
            left = int((d["pause_until"] - _now()) / 60) + 1
            return f"失败退避中，还要等 {left} 分钟"
        last = d.get("last_run", 0)
        cool = d["cooldown_min"] * 60
        if last and _now() - last < cool and not skip_cool:
            left = int((cool - (_now() - last)) / 60) + 1
            return f"冷却中，{left} 分钟后再跑"
        bucket = time.strftime("%Y%m%d%H")
        if d.get("hour_bucket") != bucket:
            d["hour_bucket"] = bucket
            d["hour_count"] = 0
            _save(d)
        if d.get("hour_count", 0) >= d["hourly_cap"]:
            return f"这一小时已采 {d['hour_count']} 篇，到硬上限了"
        return ""


def begin(notes: int, force: bool = False) -> int:
    """开始一轮，返回本轮允许的篇数"""
    with _LCK:
        d = _load()
        cap = min(notes, d["max_notes_run"])
        left_hour = d["hourly_cap"] - d.get("hour_count", 0)
        cap = max(1, min(cap, left_hour))
        d["last_run"] = _now()
        _save(d)
        return cap


def wait_gap(i: int):
    """第 i 篇之前的等待（i=0 不等待）"""
    d = _load()
    if i <= 0:
        return
    if i % d["long_rest_every"] == 0:
        lo, hi = d["long_rest"]
        time.sleep(random.uniform(lo, hi))
        return
    time.sleep(random.uniform(d["min_gap"], d["max_gap"]))


def note_taken(n: int = 1):
    with _LCK:
        d = _load()
        bucket = time.strftime("%Y%m%d%H")
        if d.get("hour_bucket") != bucket:
            d["hour_bucket"] = bucket
            d["hour_count"] = 0
        d["hour_count"] = d.get("hour_count", 0) + n
        d["last_note_at"] = _now()
        _save(d)


def ok():
    with _LCK:
        d = _load()
        d["fail_streak"] = 0
        _save(d)


def fail():
    with _LCK:
        d = _load()
        d["fail_streak"] = d.get("fail_streak", 0) + 1
        if d["fail_streak"] >= d["fail_streak_limit"]:
            d["pause_until"] = _now() + d["fail_pause_min"] * 60
            d["fail_streak"] = 0
            _save(d)
            return True
        _save(d)
        return False


def snapshot() -> dict:
    d = _load()
    return {
        "config": {k: d[k] for k in DEFAULTS},
        "hour_count": d.get("hour_count", 0),
        "last_run": d.get("last_run", 0),
        "pause_until": d.get("pause_until", 0),
        "quiet_now": in_quiet(d),
        "block": block_reason(),
    }


def gap_seconds(i: int) -> float:
    """第 i 篇之前该等多少秒（长休按 long_rest_every 插一次）"""
    d = _load()
    if i <= 0:
        return 0.0
    if i % d["long_rest_every"] == 0:
        lo, hi = d["long_rest"]
        return random.uniform(lo, hi)
    return random.uniform(d["min_gap"], d["max_gap"])
