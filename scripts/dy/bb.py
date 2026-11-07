# -*- coding: utf-8 -*-
"""比特浏览器（BitBrowser）本地 API 客户端

官方本地接口默认在 http://127.0.0.1:54345
  POST /browser/list   列出环境
  POST /browser/open   {"id": "<环境id>"}  → data.ws 就是 CDP 地址
  POST /browser/close  {"id": "<环境id>"}
"""
import json
import urllib.request

API = "http://127.0.0.1:54345"


def _post(path: str, body: dict, timeout: float = 30):
    req = urllib.request.Request(
        API + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def alive() -> bool:
    try:
        _post("/browser/list", {"page": 0, "pageSize": 1}, timeout=5)
        return True
    except Exception:
        return False


def profiles():
    d = _post("/browser/list", {"page": 0, "pageSize": 100})
    out = []
    for b in (d.get("data") or {}).get("list", []):
        out.append({
            "id": b.get("id", ""),
            "name": b.get("name", ""),
            "country": b.get("lastCountry") or "",
            "status": b.get("status", 0),
            "platform": (b.get("platform") or "")[:40],
        })
    return out


def open_profile(bid: str, timeout: float = 420):
    d = _post("/browser/open", {"id": bid}, timeout=timeout)
    if not d.get("success"):
        raise RuntimeError(d.get("msg") or "打开环境失败")
    return d.get("data") or {}


def close_profile(bid: str):
    try:
        return _post("/browser/close", {"id": bid}, timeout=30)
    except Exception:
        return {}
