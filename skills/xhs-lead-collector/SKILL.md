---
name: xhs-lead-collector
description: 小红书评论采集：真实点击抓评论，风控节奏与去重台账。
---

# 小红书评论采集（真实点击版）

触发场景：从行业关键词→小红书笔记评论→线索表；或旧采集软件要重做。
不伪造签名、不破解接口、不绕验证码——只用用户自己已登录的浏览器。

## 铁律：必须真实鼠标点击

搜索结果卡片链接 **不带 `xsec_token`**，直接 `Page.navigate` 到 `/explore/<id>` 一律 404。
必须对卡片派发真实鼠标事件，前端路由才会把 token 拼上：

```python
for key in ("mousePressed", "mouseReleased"):
    await cdp.call("Input.dispatchMouseEvent", sid, type=key,
                   x=cx, y=cy, button="left", clickCount=1)
```

坐标来自卡片 `scrollIntoView({block:'center'})` 后的 `getBoundingClientRect()` 中心。

## 按 id 点击，不要按索引

先一次性抽全页卡片 `{id,title}`（href 正则 `explore/([0-9a-f]{12,})`），再逐个按 id 找卡片点击。
按索引点会在「后退→重新渲染」后错位，反复打开同一篇（日志里标题重复、评论 0 条）。

## 独立 Chrome + CDP

```
chrome.exe --remote-debugging-port=9333 --user-data-dir=<独立 profile> \
  --no-first-run --no-default-browser-check \
  --proxy-server="direct://" --proxy-bypass-list="*"
```

- 独立 profile 与主力浏览器隔离，登录一次即可复用。
- `--proxy-server=direct` 避免本机代理导致「IP存在风险 300012」。
- 建标签用 `Target.createTarget(url=<搜索页>, newWindow=True)` **直接带 URL**；
  先建 about:blank 再 Page.navigate 容易掉会话（`Runtime.evaluate: Session with given id not found`）。
- attach 后用 `Runtime.evaluate("1+1")` 验证会话，失败就重建标签（最多 3 次）。

## 页面流转

- 首屏：建标签到 `search_result?keyword=<kw>` → 等 6s → 滚动 3 次（1.4-2.2s 间隔）。
- 篇与篇之间：`history.back()` 回列表（比重新加载稳、也像真人），等 3.2-4.8s。
- 找不到目标卡片：先滚 6 次找；还不行 `Page.navigate` 重新搜一次再找。
- 每篇最多滚 3 次读评论；读「共 N 条评论」作为 total。

## 风控节奏（默认值，别调快）

| 项 | 值 |
|---|---|
| 篇间间隔 | 随机 6-9s，每 4 篇插入 25-45s 长休 |
| 单轮上限 | 15 篇（默认 6） |
| 每小时上限 | 30 篇 |
| 任务冷却 | 10 分钟 |
| 安静时段 | 23:00-07:00 不采 |
| 失败退避 | 连续 2 次失败停 30 分钟 |

只读：不点赞、不关注、不私信。所谓「自动关注留痕」是封号高发点，
改成「台账留痕」——记 `seen_notes` 去重，不重复打扰同一个人。

## 去重台账

`seen_notes.json` 记 `{note_id: {t,title,kw}}`，采集前用 `skip(note_id)` 过滤卡片，采完标记。
**打分函数别把 `note_id` 丢掉**——在 `score_rows()` 里重建 dict 时容易漏，
漏了就永远去不了重（同一篇反复被采）。

## 桌面内嵌版（Electron 壳）

- 窗口必须创建后立即 `win.show()`；子视图（WebContentsView）不会触发 `ready-to-show`，用 `once('ready-to-show', show)` 会让窗口永远隐藏 —— 症状是页面 `innerWidth/innerHeight = 0`、`document.hidden = true`，所有 `Input.dispatchMouseEvent` 静默失效（日志表现为「没打开目标笔记」或 404）。
- 排查首查 `innerWidth`：为 0 就是视图没布局，不是选择器问题。
- 一个窗口分两半：左 WebContentsView 装小红书，右装控制台页面；启动时 `app.commandLine.appendSwitch('remote-debugging-port','9333')`，Python 侧照旧走 CDP，只是改用 `attach_page()` 复用这个已有页面，不再 createTarget。
- 必须 `appendSwitch('no-proxy-server')`：本机系统代理会让 Electron 里小红书直接连不上，表现是整页白屏、`location.href` 变成 `chrome-error://chromewebdata/`。
- 别把外部 Chrome profile 的 `Local State` + `Cookies` 拷进 Electron：混进来之后整个网络栈坏掉（什么网站都打不开且不是 403）。正确做法是让用户在壳里扫一次码，登录态自己存在 `Partitions/<名字>` 里。
- `session.fromPartition('persist:xhs')` 存的 cookie 重启后仍在；换 `Local State` 就会丢。
- 小红书搜索卡片里有一个隐藏的 0×0 链接 `/explore/<id>`（无 xsec_token，点了必 404）；能点的是封面 `a.cover` → `/search_result/<id>?xsec_token=...`。取卡片坐标时必须挑「可见且面积最大」的链接，否则永远打不开笔记。
- 评论的 IP 属地和时间在同一个 `.date` 容器里：`.date .location` 是属地，`.date span:not(.location)` 是时间。
- 包一层 `.html` 里插 JS 时，`confirm()` 的多行文案别用真实换行——会直接让整段 script 语法错误、页面所有绑定失效（表现为参数框全空、时钟不走）。写完必须 `node --check`。

## 写操作（关注/评论）比采集更容易被拦

- 评论弹窗标题写「扫码验证」，内容其实是「请求太频繁，请一分钟后再试」+ 刷新按钮——**这是限流提示，不是要扫码**，别让用户白找二维码。
- 发送后判断成败：点完发送回读编辑框，文字清空 = 成功；仍留着 = 被拦。
- 检测到 `扫码验证` / `请求太频繁` 就立即停手，并把标签页留在前台（别 closeTarget），同时记一段冷却时间。
- 节奏：评论 30-60 秒/条、单批 5 条、每天 15 条；关注 3-6 秒/人、单批 20。
- 同一账号 10 分钟内别既采集又评论——两种动作混着做最快触发验证。
- 输入文字用 `Input.insertText`（点一下编辑框聚焦后）即可让 Vue 的发送按钮从 `btn submit gray` 变成可点。

## 常见故障

| 现象 | 原因 / 处理 |
|---|---|
| `WinError 10061` | Chrome 9333 没起，先拉起采集 Chrome |
| `Session with given id not found` | 标签被换掉，重建标签并直接带 URL |
| 打开后 `/404` | 没走真实点击，或登录态掉了 |
| 标题重复 / 评论 0 条 | 按索引点击错位，改按 id 点 |
| 跳过卡片却重复采集 | `score_rows` 丢了 `note_id` |
| 界面打不开 | 本地服务被误杀，隐藏窗口常驻重启 |
