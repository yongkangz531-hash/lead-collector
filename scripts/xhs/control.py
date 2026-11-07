#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""采集/关注 的暂停、继续、停止开关（同进程内共享）"""
import threading

_LK = threading.Lock()
_S = {"paused": False, "stop": False}


def pause():
    with _LK:
        _S["paused"] = True


def resume():
    with _LK:
        _S["paused"] = False


def stop():
    with _LK:
        _S["stop"] = True
        _S["paused"] = False


def reset():
    with _LK:
        _S["paused"] = False
        _S["stop"] = False


def is_paused() -> bool:
    with _LK:
        return _S["paused"]


def is_stopped() -> bool:
    with _LK:
        return _S["stop"]


def state():
    with _LK:
        return {"paused": _S["paused"], "stop": _S["stop"]}


class Stopped(Exception):
    pass
