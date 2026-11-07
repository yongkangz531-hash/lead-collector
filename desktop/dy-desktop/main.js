const { app, BrowserWindow, WebContentsView, Menu } = require('electron');
const { spawn } = require('child_process');
const path = require('path');
const http = require('http');

// 抖音版：自己的 CDP 端口（9334）和自己的账号分区（persist:dy），跟小红书版互不影响
app.commandLine.appendSwitch('remote-debugging-port', '9334');
app.commandLine.appendSwitch('no-proxy-server');
app.commandLine.appendSwitch('remote-allow-origins', '*');
app.userAgentFallback =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36';

const PACKAGED = app.isPackaged;
const EXE_DIR = path.dirname(process.execPath);
const ROOT = process.env.DY_ROOT || (PACKAGED ? path.join(EXE_DIR, '服务') : path.join(__dirname, '..', '..', 'scripts', 'dy'));
const PY = PACKAGED
  ? path.join(ROOT, 'runtime', 'pythonw.exe')
  : path.join(ROOT, '.venv', 'Scripts', 'python.exe');
const APP = path.join(ROOT, 'app.py');
const UI = 'http://127.0.0.1:8789/';
const HOME = 'https://www.douyin.com/';

// 打包版单独的用户数据目录：登录态/缓存跟小红书版彻底分开
if (PACKAGED) {
  try { app.setPath('userData', path.join(process.env.APPDATA || EXE_DIR, 'douyin-lead-desk')); } catch (e) {}
}

let win = null, uiView = null, webView = null, pyProc = null;
let mode = 'split';

function portReady(cb, tries = 100) {
  const tick = (n) => {
    const req = http.get('http://127.0.0.1:8789/api/status', (res) => { res.resume(); cb(true); });
    req.on('error', () => {
      if (n <= 0) return cb(false);
      setTimeout(() => tick(n - 1), 300);
    });
    req.setTimeout(1500, () => req.destroy());
  };
  tick(tries);
}

// 拦掉自定义协议（bitbrowser:// 这类），并把外链留在本窗格内打开
function blockSchemes(wc) {
  const guard = (e, url) => {
    if (!/^https?:|^about:|^devtools:|^data:/.test(url || '')) {
      e.preventDefault();
      console.log('[block]', String(url).slice(0, 80));
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
  // 兜底：页内脚本改 location 触发的外部协议
  wc.on('did-start-navigation', (e) => {});
}

function layout() {
  if (!win) return;
  const { width, height } = win.getContentBounds();
  const split = Math.round(width * 0.54);
  if (mode === 'ui') {
    uiView.setBounds({ x: 0, y: 0, width, height });
    webView.setBounds({ x: width, y: 0, width: 0, height: 0 });
  } else if (mode === 'web') {
    uiView.setBounds({ x: 0, y: 0, width: 0, height: 0 });
    webView.setBounds({ x: 0, y: 0, width, height });
  } else {
    webView.setBounds({ x: 0, y: 0, width: split - 3, height });
    uiView.setBounds({ x: split + 3, y: 0, width: width - split - 3, height });
  }
}

function setMode(m) {
  mode = m;
  layout();
  const name = m === 'ui' ? '只看界面' : m === 'web' ? '只看浏览器' : '分屏';
  if (win) win.setTitle('抖音采集台 · ' + name + '（Ctrl+1/2/3 切换）');
}

function createWindow() {
  win = new BrowserWindow({
    width: 1560, height: 960, minWidth: 1080, minHeight: 700,
    backgroundColor: '#04060c', autoHideMenuBar: true, title: '抖音采集台', show: true,
  });

  uiView = new WebContentsView({ webPreferences: { partition: 'persist:dyui', contextIsolation: true } });
  webView = new WebContentsView({
    webPreferences: { partition: 'persist:dy', contextIsolation: true, webSecurity: true },
  });

  win.contentView.addChildView(webView);
  win.contentView.addChildView(uiView);

  blockSchemes(uiView.webContents);
  blockSchemes(webView.webContents);

  uiView.webContents.loadURL(UI);
  webView.webContents.loadURL(HOME);

  win.on('resize', layout);
  win.on('closed', () => { win = null; uiView = null; webView = null; });

  win.webContents.on('before-input-event', (e, input) => {
    if (input.control && input.type === 'keyDown') {
      if (input.key === '1') { setMode('ui'); e.preventDefault(); }
      if (input.key === '2') { setMode('web'); e.preventDefault(); }
      if (input.key === '3') { setMode('split'); e.preventDefault(); }
      if (input.key === 'r' || input.key === 'R') { if (uiView) uiView.webContents.reload(); e.preventDefault(); }
    }
  });

  win.show();
  win.focus();
  setMode('split');
  setTimeout(layout, 400);
  setTimeout(layout, 1500);
}

app.whenReady().then(() => {
  Menu.setApplicationMenu(null);
  pyProc = spawn(PY, [APP], {
    cwd: ROOT, windowsHide: true,
    env: Object.assign({}, process.env, { DY_EMBED: '1', DY_CDP_PORT: '9334', PYTHONIOENCODING: 'utf-8' }),
  });
  pyProc.stdout.on('data', (d) => process.stdout.write('[py] ' + d));
  pyProc.stderr.on('data', (d) => process.stderr.write('[py] ' + d));

  portReady((ok) => {
    if (!ok) console.error('本地服务没起来，检查 8789 端口');
    createWindow();
  });
});

app.on('window-all-closed', () => {
  if (pyProc) { try { pyProc.kill(); } catch (e) {} }
  app.quit();
});
app.on('before-quit', () => { if (pyProc) { try { pyProc.kill(); } catch (e) {} } });
