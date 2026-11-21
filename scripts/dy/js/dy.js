(function () {
  var NL = String.fromCharCode(10);
  if (window.__dyReady) return 'ready';
  window.__dyReady = 1;

  function lines(el) {
    return (el.innerText || '').split(NL).map(function (s) { return s.trim(); }).filter(function (s) { return s !== ''; });
  }

  function isTimeLine(s) {
    // 1年前·福建 / 3天前·广东 / 刚刚·北京
    if (s.indexOf('·') < 0) return false;
    return /(刚刚|秒前|分钟前|小时前|天前|周前|月前|年前)/.test(s);
  }

  function skipLine(s) {
    if (s === '...' || s === '作者' || s === '作者赞过' || s === '作者回复过') return true;
    if (/^展开\s*\d*\s*条?回复$/.test(s)) return true;
    return false;
  }

  // 一条评论：昵称 / 正文 / 时间·IP / 点赞
  function parseItem(it) {
    var L = lines(it);
    if (!L.length) return null;
    var name = L[0];
    if (name === '作者') return null;
    var ti = -1;
    for (var i = 1; i < L.length; i++) { if (isTimeLine(L[i])) { ti = i; break; } }
    var end = (ti > 0 ? ti : L.length);
    var content = [];
    for (var j = 1; j < end; j++) {
      var s = L[j];
      if (skipLine(s)) continue;
      if (s === '分享' || s === '回复') break;
      content.push(s);
    }
    var tline = ti > 0 ? L[ti] : '';
    var ip = tline.indexOf('·') >= 0 ? tline.split('·').pop().trim() : '';
    var ctime = tline.indexOf('·') >= 0 ? tline.split('·')[0].trim() : '';
    var like = '';
    for (var k = ti + 1; ti > 0 && k < Math.min(L.length, ti + 3); k++) {
      if (/^[0-9.]+[万千]?$/.test(L[k])) { like = L[k]; break; }
      if (L[k] === '分享' || L[k] === '回复') break;
    }
    var a = it.querySelector('a[href*="/user/"]');
    var uid = '';
    if (a) {
      var h = a.getAttribute('href') || '';
      uid = h.split('/user/')[1] || '';
      uid = uid.split('?')[0].split('/')[0];
    }
    var isAuthor = lines(it).indexOf('作者') >= 0 && ti > 0 && lines(it).slice(1, ti).indexOf('作者') >= 0;
    return {
      name: name, text: content.join(' ').slice(0, 300), ip: ip, ctime: ctime,
      like: like, uid: uid, author: isAuthor ? 1 : 0
    };
  }

  window.__dyExtract = function () {
    var items = document.querySelectorAll('[data-e2e="comment-item"]');
    var out = [];
    for (var i = 0; i < items.length; i++) {
      var r = parseItem(items[i]);
      if (r && r.name && r.text) out.push(r);
    }
    return JSON.stringify(out);
  };

  window.__dyNoteInfo = function () {
    function txt(sel) {
      var e = document.querySelector(sel);
      return e ? (e.innerText || '').trim() : '';
    }
    var body = document.body.innerText || '';
    var m = body.match(/全部评论\(([0-9.]+[万千]?)\)/) || body.match(/全部评论\s*([0-9.]+[万千]?)/)
      || body.match(/([0-9.]+[万千]?)\s*条评论/) || body.match(/评论\s*\(([0-9.]+[万千]?)\)/);
    var cnt = '';
    var ci = document.querySelector('[data-e2e="feed-comment-icon"]');
    if (ci) { var t2 = (ci.innerText || '').trim(); if (/^[0-9.]+[万千]?$/.test(t2)) cnt = t2; }
    var pub = txt('[data-e2e="detail-video-publish-time"]').replace(/^发布时间：/, '');
    var pdate = '';
    var pm = pub.match(/(\d{4})-(\d{1,2})-(\d{1,2})/);
    if (pm) pdate = pm[1] + '-' + ('0' + pm[2]).slice(-2) + '-' + ('0' + pm[3]).slice(-2);
    var uinfo = txt('[data-e2e="user-info"]');
    var uname = (uinfo.split(NL)[0] || '').trim();
    var alink = document.querySelector('[data-e2e="user-info"] a[href*="/user/"]') ||
      document.querySelector('a[href*="/user/"]:not([href*="/user/self"])');
    var auid = '';
    if (alink) auid = ((alink.getAttribute('href') || '').split('/user/')[1] || '').split('?')[0];
    var title = (txt('h1') || txt('[data-e2e="video-desc"]') || (document.title || '').replace(/\s*-\s*抖音.*$/, '')).slice(0, 120);
    return JSON.stringify({
      title: title, author: uname, author_uid: auid, date: pdate, publish: pub,
      likes: txt('[data-e2e="video-player-digg"]'),
      collects: txt('[data-e2e="video-player-collect"]'),
      shares: txt('[data-e2e="video-player-share"]'),
      comments: m ? m[1] : cnt,
      url: location.href
    });
  };

  window.__dyCards = function (wantIds) {
    var want = wantIds || null;
    var seen = {}, out = [];
    var as = document.querySelectorAll('a[href*="/video/"]');
    for (var i = 0; i < as.length; i++) {
      var a = as[i];
      var h = a.getAttribute('href') || '';
      var id = h.split('/video/')[1];
      if (!id) continue;
      id = id.split('?')[0].split('/')[0];
      if (seen[id]) continue;
      var r = a.getBoundingClientRect();
      if (r.width < 60 || r.height < 60) continue;
      seen[id] = 1;
      out.push({ id: id, y: Math.round(r.y) });
      if (out.length >= 60) break;
    }
    return JSON.stringify(out);
  };

  // 评论列表能滚的祖先（实测是 .parent-route-container）
  function scroller() {
    var l = document.querySelector('[data-e2e="comment-list"]');
    if (!l) return null;
    var p = l;
    for (var i = 0; i < 14 && p; i++) {
      if (p.scrollHeight > p.clientHeight + 30) return p;
      p = p.parentElement;
    }
    return null;
  }

  // 滚轮坐标：取评论列表自身矩形，夹到视口里（视口外的坐标 CDP 滚轮是空操作）
  window.__dyCommentBox = function () {
    var l = document.querySelector('[data-e2e="comment-list"]');
    if (!l) return '';
    var r = l.getBoundingClientRect();
    var sc = scroller();
    var x = Math.min(Math.max(Math.round(r.x + r.width / 2), 60), window.innerWidth - 60);
    var y = Math.min(Math.max(Math.round(r.y + Math.min(r.height / 2, 200)), 120), window.innerHeight - 100);
    return JSON.stringify({
      x: x, y: y, items: document.querySelectorAll('[data-e2e="comment-item"]').length,
      top: sc ? Math.round(sc.scrollTop) : -1, h: sc ? sc.scrollHeight : -1, vh: window.innerHeight
    });
  };

  // JS 兜底滚动（滚轮不通时用）
  window.__dyScrollPage = function () {
    var sc = scroller();
    if (!sc) return 'no-scroller';
    var before = sc.scrollTop;
    sc.scrollTop = before + Math.round(window.innerHeight * 0.75);
    return JSON.stringify({ moved: Math.round(sc.scrollTop - before), top: Math.round(sc.scrollTop) });
  };

  // 展开二级回复（能点开几条算几条）
  window.__dyExpand = function (limit) {
    var btns = document.querySelectorAll('button.comment-reply-expand-btn');
    var n = 0, total = 0;
    for (var i = 0; i < btns.length; i++) {
      var t = (btns[i].innerText || '').trim();
      if (!/^展开\s*\d+\s*条回复$/.test(t)) continue;
      total++;
      var r = btns[i].getBoundingClientRect();
      if (r.height < 1 || r.top < 40 || r.top > window.innerHeight - 80) continue;
      if (n >= (limit || 4)) continue;
      btns[i].click();
      n++;
    }
    return JSON.stringify({ clicked: n, total: total });
  };

  window.__dyCloseTip = function () {
    var all = document.querySelectorAll('button, div, span');
    for (var i = 0; i < all.length; i++) {
      var t = (all[i].innerText || '').trim();
      if (t === '我知道了' || t === '知道了') { all[i].click(); return 'closed'; }
    }
    return 'none';
  };

  // 打开评论面板：抖音页面上有两个图标（浮层里 / 背景里），选视口内那个，对 svg 派发 click
  window.__dyOpenComments = function () {
    var els = document.querySelectorAll('[data-e2e="feed-comment-icon"]');
    var pick = null;
    for (var i = 0; i < els.length; i++) {
      var r = els[i].getBoundingClientRect();
      if (r.height > 0 && r.top >= 0 && r.top < window.innerHeight) { pick = els[i]; break; }
    }
    if (!pick) return 'no-icon';
    var svg = pick.querySelector('svg');
    if (svg) {
      svg.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
      return 'svg';
    }
    pick.click();
    return 'el';
  };

  // ---------- 私信 ----------
  window.__dyDmOpen = function () {
    var btns = document.querySelectorAll('button');
    for (var i = 0; i < btns.length; i++) {
      if ((btns[i].innerText || '').trim() === '私信' && btns[i].getBoundingClientRect().height > 0) {
        btns[i].click();
        return 'clicked';
      }
    }
    return 'no-btn';
  };

  function dmEditor() {
    var eds = document.querySelectorAll('[contenteditable="true"], [contenteditable=""], .editor-kit-container');
    var cand = null;
    for (var i = 0; i < eds.length; i++) {
      var e = eds[i];
      var r = e.getBoundingClientRect();
      if (r.height < 10 || r.width < 40) continue;
      var ph = e.getAttribute('data-placeholder') || e.getAttribute('placeholder') || '';
      if (/发送消息|说点什么/.test(ph)) return e;   // 还没输入时靠 placeholder 认
      if (!cand) cand = e;                          // 输入之后 placeholder 会消失，退回第一个可见编辑框
    }
    return cand;
  }

  function clean(s) {
    return String(s || '').replace(/[\u200b\u200c\u200d\ufeff]/g, '').trim();
  }

  window.__dyDmFocus = function () {
    var e = dmEditor();
    if (!e) return 'no-editor';
    e.focus();
    try {
      var r = document.createRange();
      r.selectNodeContents(e);
      r.collapse(false);
      var s = window.getSelection();
      s.removeAllRanges();
      s.addRange(r);
    } catch (err) {}
    return 'ok';
  };

  window.__dyDmType = function (text) {
    var e = dmEditor();
    if (!e) return JSON.stringify({ ok: false, reason: 'no-editor' });
    e.focus();
    try {
      var r = document.createRange();
      r.selectNodeContents(e);
      var s = window.getSelection();
      s.removeAllRanges();
      s.addRange(r);
    } catch (err) {}
    try { document.execCommand('delete', false, null); } catch (err) {}
    var ok = false;
    try { ok = document.execCommand('insertText', false, text); } catch (err) { ok = false; }
    if (!ok || !clean(e.innerText)) {
      // 兜底：直接写节点内容并派发 input 事件（大部分编辑器会同步状态）
      e.innerText = text;
      e.dispatchEvent(new InputEvent('input', { bubbles: true, data: text, inputType: 'insertText' }));
    }
    return JSON.stringify({ ok: true, text: clean(e.innerText).slice(0, 120) });
  };

  window.__dyDmCheck = function () {
    var e = dmEditor();
    if (!e) return JSON.stringify({ ok: false, reason: 'no-editor' });
    return JSON.stringify({ ok: true, text: clean(e.innerText).slice(0, 120) });
  };
})();
