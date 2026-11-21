const { contextBridge, ipcRenderer } = require('electron');

// 只暴露三个模式切换，别的什么都不给页面
contextBridge.exposeInMainWorld('xhsShell', {
  setMode: (m) => ipcRenderer.send('xhs-mode', m),
  getMode: () => ipcRenderer.send('xhs-mode-get'),
  onMode: (cb) => ipcRenderer.on('xhs-mode-state', (e, m) => cb(m)),
});
