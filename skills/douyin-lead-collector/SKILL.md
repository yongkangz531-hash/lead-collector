---
name: douyin-lead-collector
description: 抖音评论采集（CDP 真实浏览器）：登录墙、视频页、滚轮加载。
---

# 抖音评论采集（CDP 真实浏览器）

复用 `xhs-lead-collector` 的架构（guard/store/control/score + Electron 壳 + 8788 服务 + 9333 CDP），下面只记抖音不同的地方。

## 登录

- 抖音搜索**必须登录**，未登录只返回几条并弹「登录后即可搜索更多精彩视频」+ 二维码。首页 `/jingxuan` 不登录也能看。
- 判断：`document.body.innerText.indexOf('扫码登录')>=0`。
- 让用户在壳子的左窗格扫码一次即可，cookie 存 persist 分区。

## 页面结构（已验证）

- 搜索：`https://www.douyin.com/search/<kw>?type=video`，卡片是 `a[href*="/video/"]`，href 形如 `//www.douyin.com/video/<aweme_id>`，`target="_blank"`。
- 直接打开 `https://www.douyin.com/video/<id>` 可用（**和小红书不同，不需要 xsec_token**），这是点击失败时的可靠兜底。
- 视频页：标题在 `h1`（不是 `[data-e2e=video-desc]`，那是浮层里的）；点赞 `[data-e2e=video-player-digg]`；评论数从正文正则 `全部评论\((\d+)\)` 取。
- 评论项：`[data-e2e="comment-item"]`，容器 `[data-e2e="comment-list"]`，打开评论按钮 `[data-e2e="feed-comment-icon"]`。
- 评论内容按 `innerText` 按行解析最稳：第 1 行昵称 →（可能有 `...`）→ 正文 → `1年前·福建` 形态的「时间·IP」→ 点赞数 → `分享`/`回复` → `展开N条回复`。时间行正则 `/(刚刚|秒前|分钟前|小时前|天前|周前|月前|年前)/` 且含 `·`。uid 取 `a[href*="/user/"]` 里 `/user/` 之后的部分（sec_uid）。

## 评论加载：先能打开面板，再谈滚多少

**打开评论面板（已验证的唯一可靠路径）**

1. 先关首屏引导层：找到 innerText 含「我知道了」的元素点掉。引导层盖住右栏，不关掉后面所有点击都落空（表现：点了评论图标，`comment-list` 仍不存在）。
2. `[data-e2e=feed-comment-icon]` 页面上有**两个**：一个在浮层里（视口内），一个在背景 feed 里（y≈1500，视口外）。选 rect 落在视口内的那个。
3. 鼠标点在图标**外框中心无效**（实测点完 `comment-list` 仍为 false）；对该图标的 `svg` 子元素派发 `click` 事件才打开：`icon.querySelector('svg').dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true,view:window}))` → 面板出现、一次出 10 条。
4. 打开浮层：对卡片内的 `img` 派发 click（`a` 是 `target=_blank`，点 `a` 会走新窗口），URL 变成 `?modal_id=<id>` 即成功。

**滚轮（能拿到多少算多少，别死磕）**

- JS 改 `comment-list` 自身的 `scrollTop` 无效（`overflow:visible`、`scrollHeight == clientHeight`），整页 `window.scrollY` 恒 0。真正能滚的是祖先里的 `.parent-route-container`（`scrollTop` 可改、内容会动）。注意它是**虚拟列表**：滚完新条目会换出来、旧条目被卸载，`comment-item` 数量可能一直是 8-10 条不动 —— 拿它当进度会误判「没加载」，必须累计去重后再数。
- `Input.dispatchMouseEvent type=mouseWheel` 的坐标**必须在视口内**。落在视口外的坐标（如列表 rect.y≈2340、y≈2815）是空操作：连滚 6 次 `comment-item` 数量不变。之前记的「不用夹到视口内、9→51→56」是不可复现的个例，别当基准；按视口夹取 `y = min(max(rect.y+200, 150), innerHeight-80)` 再滚，并且**每次滚完重新量 rect**（滚动后列表会移出视口）。
- 在浮层评论抽屉里，`[data-e2e=comment-item]` 节点存在但 `innerText` 为 `''`（虚拟列表占位），按 innerText 解析一律得 0 条 —— 不要在抽屉里解析。
- 可靠读数在**整页视频页** `/video/<id>`：innerText 按行解析正常；浮层抽屉里的节点 `innerText` 为空，别在那里解析。
- 单次抽取只拿到**当前渲染**的条目，必须做「抽取 → 滚可滚祖先 → 等 2 秒 → 再抽取」的累计去重循环（键 `uid + 前 24 字`，5 轮）。评论多的视频这样能到 50-60 条，实测一轮 3 条视频共 165 条。抽出来只有 4-9 条时，先对照视频表里的「评论数」——多半是这条视频本身就少，不是采不到，别去改滚轮参数。
- 单条视频取满 80 条就收手（`MAXC`），不在一个页面上赖太久。
- ⚠️ `mouseWheel` 的 CDP 调用**经常不返回回执**，必须 fire-and-forget（只 send 不 wait），否则每条 timeout 25 秒。

## 与小红书彻底隔离（用户明确要求）

同一台机器上两套采集台必须各自独立，否则会互相覆盖登录态和数据：

| | 小红书 | 抖音 |
|---|---|---|
| 服务端口 | 8788 | **8789** |
| CDP 调试端口 | 9333 | **9334** |
| 业务目录 | `工具\xhs` | `工具\dy` |
| Electron 壳 / userData | `xhs-desktop` / `xhs-lead-desk` | `dy-desktop` / `douyin-lead-desk` |
| 会话分区 | `persist:xhs` | `persist:dy` |

- CDP 端口从环境变量读（`DY_CDP_PORT`，默认 9334），别写死 9333 去抢小红书的浏览器。
- 每个壳只对**自己的** partition 扫码一次，登录态就留在各自 userData 里，不要跨壳拷 Cookie / `Local State`。

### 克隆后端必查：`store.note_seen` 是谓词函数

从小红书复制 `app.py` 到新项目时，`skip=store.note_seen` 传进来的是 `note_id -> bool` 的函数。
写成 `if c["id"] not in skip` 会抛 `argument of type 'function' is not iterable`，整轮采集在「搜索页打开中…」后直接失败。正确写法：

```python
if callable(skip):
    cards = [c for c in cards if not skip(c["id"])]
else:
    cards = [c for c in cards if c["id"] not in set(skip or [])]
```

### 克隆后端必查：`collect2.run()` 返回 `(rows, notes_meta)`

调用点写成 `rows = asyncio.run(collect2.run(...))` 会把元组喂给 `apply_filters`，抛 `'list' object has no attribute 'get'` —— 症状极具迷惑性：日志已经把评论采完（如「完成：212 条评论」），但线索表是 0、界面报「出错」。
必须 `rows, _notes_meta = asyncio.run(...)`。克隆后端时把所有调用点的**解包形状**逐个对齐，别只测采集函数本身。

## 下载公开视频（存 MP4）必须先有登录态

- 未登录用浏览器打开 `https://www.douyin.com/video/<id>` 只会落到「验证码中间页」（`document.title` 是 `🐴 验证码中间页`），`<video>` 存在但 `src` 为空；首页推荐流不登录能看，**单个视频页不给播**。
- yt-dlp 即使带上有效 `cookies.txt` 仍可能报 `Fresh cookies (not necessarily logged in) are needed`（extractor 的 web 详情接口变了），**别把它当唯一路径**，试一次不通就换下面的注入法。
- 老接口同样已废：`iesdouyin.com/web/api/v2/aweme/iteminfo/` 回 `encrypt_data_miss`，`/aweme/v1/web/aweme/detail/` 回 403 `Blocked by ArgusSecurityPlugin Uifid Not Found`——都要 a_bogus 签名，别手搓。

**能跑通的注入法（实测出片）**

1. 起自己的 Chrome（独立 profile + `--remote-debugging-port`），`Network.setCookies` 把 `cookies.txt` 里的 douyin cookie 推入
2. **先开 `https://www.douyin.com/` 预热 5-6 秒**，再导航到 `/video/<id>`——直接进详情页即使已登录也常是空壳（`document.title` 空、`<video>` 不解码）
3. 对页面上最大的 `<video>` 调 `play()`（静音），触发真实流请求
4. 取地址两处按序试：`window.player.definitionList`（每项 `url[0].src`，取 size 最大）→ 退回 `performance.getEntriesByType('resource')` 里匹配 `douyinvod.*video/tos` 的最后一条
5. Node `fetch` / curl 带 `Referer: https://www.douyin.com/` 下载；产物 < 50 KB 视为失败（拿到占位或错误页）

- 判成功**不要看 `document.title`**：它经常为空而页面其实已渲染，以「抓到播放地址 + 落盘字节数」为准；抓不到再打诊断（`location.href` + body 前 120 字）。
- 网页端只给一档清晰度（`definitionList` 常只有一项，实测 576×1024、43 秒、2.58 MB）。要 1080p 只能用 App 下载且带水印——先跟用户讲明，别承诺无水印高清。
- **不要试图从 Chrome 的 cookie 库里取**：Chrome ≥136 的 cookie 全部是 `v20` App-Bound 加密（`select substr(hex(encrypted_value),1,3) from cookies` 全是 v20），`--cookies-from-browser chrome` 报 `Failed to decrypt with DPAPI`；`sqlite3` 用 `immutable=1` 也打不开（Chrome 独占锁，`Device or resource busy`）；复制 profile 再用 CDP `Storage.getCookies` 返回 0 条。这是 Chrome 故意设的安全边界，绕它既徒劳又越界。
- 正确路径只要一样东西：让用户在**登录态浏览器**里用「Get cookies.txt LOCALLY」扩展导出 `cookies.txt`，放进工具目录，脚本优先读它。cookie 过期（下载报 Fresh cookies）就让他重导一次。
- 关掉用户的 Chrome 去拷 cookie 库属于会丢他标签页/未存数据的操作，必须事先征得同意；单次被拒就停，不要换写法重试。
- **「技能装好」≠「下载能用」**：第三方/平台下载接口必须拿真链接跑通、拿到落盘文件和字节数，再对外说已部署；"脚本能调通"只能说明调用链没断。

## 接比特浏览器（BitBrowser）环境

可选路线，单账号不需要。API 与坑见 `references/bitbrowser-profiles.md`。

## Electron 壳的两个必须项

- 必须拦自定义协议：页面里的 `bitbrowser://` 等推广链接被点中会让 Windows 弹「获取打开此'bitbrowser'链接的应用 / 你的电脑没有可打开此链接的应用」并抢焦点，表现为「软件用不了」。只挂 `will-navigate` **不够**（iframe 内跳转和重定向会漏），三个都要挂：

```js
const guard = (e, url) => { if (!/^https?:|^about:|^devtools:|^data:/.test(url || '')) e.preventDefault(); };
wc.on('will-navigate', guard);
wc.on('will-frame-navigate', (e) => guard(e, e.url));
wc.on('will-redirect', guard);
wc.setWindowOpenHandler(({ url }) => {
  if (!/^https?:/.test(url)) return { action: 'deny' };
  if (/(douyin|xiaohongshu)\.com/.test(url)) setTimeout(() => { try { wc.loadURL(url); } catch (e) {} }, 0);
  return { action: 'deny' };
});
```

- `setWindowOpenHandler` 里 `loadURL` 必须放进 `setTimeout(...,0)`：在 handler 同步调用会被 deny 取消，表现为点卡片毫无反应、URL 不变（诊断特征：`modal-video-container` 为 false、URL 仍是搜索页）。
- 同一份 `blockSchemes` 要抄给**所有**壳（开发壳 + 打包后的 `resources\app\main.js`），漏一个那个入口就还会弹协议框。

## 控制台自检（自建 UI 的静默失败）

界面上的 `$` 是 `querySelector` 的短名，**必须带 `#`**：写成 `$("login")` 得到 `null`，`textContent` 赋值抛异常又被 `catch` 吞掉，界面永远停在初始文案（「检查中…」）。诊断不要靠猜，在页面里跑：

```python
# CDP Runtime.evaluate，注意 awaitPromise=True
"loadLogin().then(()=>document.getElementById('login').textContent).catch(e=>'ERR '+e.message)"
```

- 状态数字不自己动 = 页面没有轮询。收尾前确认有首帧 `refresh()` 且挂了 `setInterval(refresh, 2000)`。
- 新增的 GET 接口要加在 `do_GET` 分支；误加进 `do_POST` 的表现是 `{"error":"not found"}`，而 POST 测试一切正常。
- 登录探测只读当前 target 的 DOM，**不要 `Page.navigate`**：探测一次就把用户正在看的页面跳走了。
- 单账号时把比特环境入口整行删掉，别留一个「比特里没有可用环境」的空下拉 —— 用户会以为软件坏了。
- 风控要显性化：把三档节奏（保守/标准/很慢）落到 `guard` 的 `min_gap` / `max_gap` / `long_rest` 并持久化，界面常显「本小时 x/30 条 · 单轮≤15 · 冷却：还剩 N 分钟」。用户看到限制才敢点「开始采集」。

## 节奏（比小红书更保守）

篇间 8-15 秒（保守档 16-26、很慢档 25-40），每 4 条插一次 35-100 秒长休；单轮 ≤15 条视频、每小时 ≤30 条、23:00-07:00 不采、连续失败 2 次退避 30 分钟、两轮之间冷却 10 分钟。抖音对自动化的判定比小红书更紧，写操作（关注/评论）暂不做。
