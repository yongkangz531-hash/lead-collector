# 采集台技能包（lead-collector-pack）

小红书 / 抖音 评论采集台的成套源码 + 桌面壳 + 经验文档。两套程序互不干扰，可同时开。

仓库：https://github.com/yongkangz531-hash/lead-collector

## 里面有什么

```
采集台-pack/
├── 一键安装.bat                 运行它就好（建 venv + 装 electron）
├── SKILL.md                     决策、安装、启动、三条铁律、节奏表
├── skills/
│   ├── xhs-lead-collector/      小红书单独技能（真实点击版）
│   └── douyin-lead-collector/   抖音单独技能（登录墙/虚拟列表/下载公开视频）
├── references/
│   ├── 01-小红书.md             真实点击、CDP、Electron 壳坑、故障对照
│   ├── 02-抖音.md               登录墙、评论面板、虚拟列表、下载公开视频
│   └── 03-风控与故障.md          参数落点、报错、写操作边界
├── scripts/
│   ├── requirements.txt         websockets==17.3
│   ├── setup.bat                一键建 venv（xhs + dy）
│   ├── xhs/                     app.py、collect2.py、guard.py、ui/、js/、启动.bat
│   └── dy/                      app.py、dy_collect.py、guard.py、ui/、js/
└── desktop/
    ├── xhs-desktop/             Electron 壳（main.js + package.json）
    └── dy-desktop/
```

## 装到新机器

1. 装 Python 3.11（勾 PATH）和 Node 20+（桌面壳才需要）
2. 双击 `一键安装.bat` —— 建 `xhs/.venv`、`dy/.venv`、装 websockets 和 electron
3. 起壳：`cd desktop\xhs-desktop && npx electron .`；或者直接走网页版

手动装（等价）：

```bat
cd scripts && setup.bat
cd ..\desktop\xhs-desktop && npm install
cd ..\dy-desktop && npm install
```

装到别的位置时，用环境变量指路：`set XHS_ROOT=D:\...\scripts\xhs`、`set DY_ROOT=D:\...\scripts\dy`。

## 起服务端口

| 服务 | 端口 | 改端口 | 健康检查 |
|---|---|---|---|
| 小红书 | 8788 | `set XHS_PORT=...` | `http://127.0.0.1:8788/api/status` |
| 抖音 | 8789 | `set DY_PORT=...` | `http://127.0.0.1:8789/api/status` |

`启动.bat` 会顺带拉起采集专用 Chrome（CDP 9333，`--proxy-server=direct`）。

## 数据在哪

全在两个 `data/` 下，纯文本 JSON/JSONL，可读可删：

```
data/leads.jsonl        线索（按内容哈希去重）
data/seen_notes.json    采过的笔记/视频（去重依据）
data/authors.json       出现过的作者
data/runs.jsonl         每轮记录
data/guard.json         风控计数（删掉等于清零冷却与上限）
```

**包本身不含任何 data/**——线索、账号、作者名单都是运行时产生的，不进技能包。

## 边界（写死在代码里，不是建议）

- 只读：不点赞、不关注、不私信
- 不绕验证码、不改指纹、不并发多账号
- 23:00-07:00 不采；连续失败自动退避
- 弹验证/限流就停手，人工过

## 许可

自用。平台页面结构与风控策略会变，出问题先看 `references/03-风控与故障.md` 的对照表。
