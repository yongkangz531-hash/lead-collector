# 比特浏览器（BitBrowser）本地 API 接入

用途：直接接管用户已在比特里登录好的环境（多账号 / 指纹隔离），而不是让他在我们自己的壳里反复扫码。

## 先判断值不值得接

- **单账号不要接**：自己的 Electron 窗格（国内 IP + 一次扫码）更快、更稳、少一个第三方依赖。
- 只有「一个平台要挂多个号」或「必须和环境指纹绑定」时才接。
- 代理是境外 IP 时别拿去跑抖音：抖音对非大陆 IP 容易触发验证 / 限流。优先建「不使用代理」的国内环境。

## 接口（默认 `http://127.0.0.1:54345`，客户端在跑才有）

```python
POST /browser/list   {"page":0,"pageSize":100}
  -> {"success":true,"data":{"totalNum":2,"list":[{"id":"...","name":"英国2_1","lastCountry":"GB","status":0}]}}
POST /browser/open   {"id":"<环境id>"}
  -> {"success":true,"data":{"ws":"ws://127.0.0.1:xxxxx/devtools/browser/...","http":"http://...","pid":1234}}
POST /browser/close  {"id":"<环境id>"}
```

`data.ws` 就是 CDP 地址，采集器直接连它即可（复用「attach 已有页面」的逻辑，别 createTarget）。

## 已验证的坑

- **`totalNum: 0` 不是故障**：客户端登录着、接口在听、列表空 —— 就是这个账号里真的没有环境。别去查端口、查鉴权、查版本，先让用户看一眼客户端界面有没有「新建窗口」。
- **`/browser/open` 可能几分钟不返回**（慢代理 + 首次启动）：放到后台线程里调、界面显示「打开中」，不要同步等；窗口其实已经在开了。
- **建环境接口要整套指纹**：`/browser/update` 传 `{name, proxyMethod:2}` 会报 `请传入 browserFingerPrint`；`/browser/create` 是 404。指纹参数几十个字段，猜错会建出坏环境 —— 让用户在客户端点「新建窗口」（顺手在高级设置里选「不使用代理」）更稳。
- **中文请求体别用 curl**：Windows 的 curl 发中文 JSON 会报 `Request body size did not match Content-Length`。用 Python `urllib.request` 发，或请求体全 ASCII。
- 客户端没启动时 `127.0.0.1:54345` 直接连不上；桌面快捷方式指向 `D:\bitbrowser\比特浏览器.exe`，用 `cmd /c start "" "<lnk>"` 拉起，等 20s 再查接口。
