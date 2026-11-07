const { app, BrowserWindow, WebContentsView, Menu } = require('electron');
const { spawn } = require('child_process');
const path = require('path');
const http = require('http');

// 关键：给内置浏览器开 CDP，Python 那边照旧用 CDP 驱动它
app.commandLine.appendSwitch('remote-debugging-port', '9333');
// 跟采集用 Chrome 一致：不走系统代理（本机代理会导致小红书连不上）
app.commandLine.appendSwitch('no-proxy-server');
app.commandLine.appendSwitch('remote-allow-origins', '*');
// 别让小红书认出 Electron
app.userAgentFallback =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36';

const ROOT = process.env.XHS_ROOT || path.join(__dirname, '..', '..', 'scripts', 'xhs');
const PY = path.join(ROOT, '.venv', 'Scripts', 'python.exe');
const APP = path.join(ROOT, 'app.py');
const UI = 'http://127.0.0.1:8788/';
const HOME = 'https://www.xiaohongshu.com/explore';

let win = null, uiView = null, xhsView = null, pyProc = null;
let mode = 'split'; // split | ui | xhs

function portReady(cb, tries = 80) {
  const tick = (n) => {
    const req = http.get('http://127.0.0.1:8788/api/status', (res) => {
      res.resume();
      cb(true);
    });
    req.on('error', () => {
      if (n <= 0) return cb(false);
      setTimeout(() => tick(n - 1), 300);
    });
    req.setTimeout(1500, () => req.destroy());
  };
  tick(tries);
}


// 拦掉自定义协议（例如页面里的 bitbrowser:// 推广链接），否则 Windows 会弹「没有可打开此链接的应用」
function blockSchemes(wc) {
  const guard = (e, url) => {
    if (!/^https?:|^about:|^devtools:|^data:/.test(url || '')) {
      e.preventDefault();
      try { console.log('[block]', String(url).slice(0, 80)); } catch (e2) {}
    }
  };
  wc.on('will-navigate', guard);
  try { wc.on('will-frame-navigate', (e) => guard(e, e.url)); } catch (e) {}
  wc.on('will-redirect', guard);
  wc.setWindowOpenHandler(({ url }) => {
    if (!/^https?:/.test(url)) return { action: 'deny' };
    if (/(douyin|xiaohongshu)\.com/.test(url)) { setTimeout(() => { try { wc.loadURL(url); } catch (e) {} }, 0); }
    return { action: 'deny' };
  });
}

function layout() {
  if (!win) return;
  const { width, height } = win.getContentBounds();
  const split = Math.round(width * 0.54);
  if (mode === 'ui') {
    uiView.setBounds({ x: 0, y: 0, width, height });
    xhsView.setBounds({ x: width, y: 0, width: 0, height: 0 });
  } else if (mode === 'xhs') {
    uiView.setBounds({ x: 0, y: 0, width: 0, height: 0 });
    xhsView.setBounds({ x: 0, y: 0, width, height });
  } else {
    xhsView.setBounds({ x: 0, y: 0, width: split - 3, height });
    uiView.setBounds({ x: split + 3, y: 0, width: width - split - 3, height });
  }
}

function setMode(m) {
  mode = m;
  layout();
  const name = { split: '分屏', ui: '只看界面', xhs: '只看浏览器' }[m];
  if (win) win.setTitle('线索采集台 · ' + name + '（Ctrl+1/2/3 切换）');
}

function createWindow() {
  win = new BrowserWindow({
    width: 1560,
    height: 960,
    minWidth: 1080,
    minHeight: 700,
    backgroundColor: '#04060c',
    autoHideMenuBar: true,
    title: '线索采集台',
    show: true,
  });

  uiView = new WebContentsView({
    webPreferences: { partition: 'persist:ui', contextIsolation: true },
  });
  xhsView = new WebContentsView({
    webPreferences: {
      partition: 'persist:xhs',
      contextIsolation: true,
      webSecurity: true,
    },
  });

  // 浏览器在左，控制台在右（跟参考的软件布局一致）
  win.contentView.addChildView(xhsView);
  win.contentView.addChildView(uiView);

  blockSchemes(uiView.webContents);
  blockSchemes(xhsView.webContents);
  uiView.webContents.loadURL(UI);
  xhsView.webContents.loadURL(HOME);

  win.on('resize', layout);
  win.on('closed', () => {
    win = null;
    uiView = null;
    xhsView = null;
  });

  win.webContents.on('before-input-event', (e, input) => {
    if (input.control && input.type === 'keyDown') {
      if (input.key === '1') { setMode('ui'); e.preventDefault(); }
      if (input.key === '2') { setMode('xhs'); e.preventDefault(); }
      if (input.key === '3') { setMode('split'); e.preventDefault(); }
      if (input.key === 'r' || input.key === 'R') {
        if (uiView) uiView.webContents.reload();
        e.preventDefault();
      }
    }
  });

  // 窗口必须马上显示：子视图不会触发 ready-to-show，等它就永远不出现（视图尺寸 0，点击全部无效）
  win.show();
  win.focus();
  setMode('split');
  setTimeout(() => { layout(); }, 400);
  setTimeout(() => { layout(); }, 1500);
}

app.whenReady().then(() => {
  Menu.setApplicationMenu(null);

  // 拉起本地服务（同一个进程里跑采集/关注/评论）
  pyProc = spawn(PY, [APP], {
    cwd: ROOT,
    windowsHide: true,
    env: Object.assign({}, process.env, { XHS_EMBED: '1', PYTHONIOENCODING: 'utf-8' }),
  });
  pyProc.stdout.on('data', (d) => process.stdout.write('[py] ' + d));
  pyProc.stderr.on('data', (d) => process.stderr.write('[py] ' + d));

  portReady((ok) => {
    if (!ok) {
      console.error('本地服务没起来，检查 8788 端口');
    }
    createWindow();
  });
});

app.on('window-all-closed', () => {
  if (pyProc) { try { pyProc.kill(); } catch (e) {} }
  app.quit();
});

app.on('before-quit', () => {
  if (pyProc) { try { pyProc.kill(); } catch (e) {} }
});
