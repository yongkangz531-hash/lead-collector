(function () {
  var NL = String.fromCharCode(10);

  function lines(el) {
    return (el.innerText || '').split(NL).map(function (s) { return s.trim(); }).filter(function (s) { return s !== ''; });
  }

  function isTimeLine(s) {
    // 1年前·福建 / 3天前·广东 / 刚刚·北京 / 2024-01-02
    if (s.indexOf('·') < 0) return false;
    return /(刚刚|秒前|分钟前|小时前|天前|周前|月前|年前)/.test(s);
  }

  function parseItem(it) {
    var L = lines(it);
    if (!L.length) return null;
    var name = L[0];
    var ti = -1;
    for (var i = 1; i < L.length; i++) { if (isTimeLine(L[i])) { ti = i; break; } }
    var content = [];
    var end = (ti > 0 ? ti : L.length);
    for (var j = 1; j < end; j++) {
      var s = L[j];
      if (s === '...' || s === '展开' || s === '作者回复过' || s === '作者赞过') continue;
      if (s === '分享' || s === '回复') break;
      content.push(s);
    }
    var tline = ti > 0 ? L[ti] : '';
    var ip = tline.indexOf('·') >= 0 ? tline.split('·').pop().trim() : '';
    var like = '';
    if (ti > 0 && L[ti + 1] && /^[0-9.]+[万千]?$/.test(L[ti + 1])) like = L[ti + 1];
    var a = it.querySelector('a[href*="/user/"]');
    var uid = '';
    if (a) {
      var h = a.getAttribute('href') || '';
      uid = h.split('/user/')[1] || '';
      uid = uid.split('?')[0];
    }
    return { name: name, text: content.join(' ').slice(0, 200), ip: ip, ctime: tline.split('·')[0] || '', like: like, uid: uid };
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
    function txt(sel) { var e = document.querySelector(sel); return e ? (e.innerText || '').trim() : ''; }
    var body = document.body.innerText || '';
    var m = body.match(/全部评论\(([0-9.]+[万千]?)\)/) || body.match(/全部评论\s*([0-9.]+[万千]?)/)
         || body.match(/([0-9.]+[万千]?)\s*条评论/) || body.match(/评论\s*\(([0-9.]+[万千]?)\)/);
    var cnt = '';
    var ci = document.querySelector('[data-e2e="feed-comment-icon"]');
    if (ci) { var t2 = (ci.innerText || '').trim(); if (/^[0-9.]+[万千]?$/.test(t2)) cnt = t2; }
    return JSON.stringify({
      title: (txt('h1') || txt('[data-e2e="video-desc"]') || (document.title || '').replace(/\s*-\s*抖音.*$/, '')).slice(0, 80),
      author: txt('[data-e2e="feed-video-nickname"], [data-e2e="video-avatar"]'),
      likes: txt('[data-e2e="video-player-digg"]'),
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
      if (r.top < 60 || r.top > window.innerHeight - 60) continue;
      seen[id] = 1;
      out.push({ id: id, x: Math.round(r.x + r.width / 2), y: Math.round(r.y + Math.min(r.height / 2, 130)) });
      if (out.length >= 30) break;
    }
    return JSON.stringify(out);
  };

  window.__dyScrollPage = function () {
    var l = document.querySelector('[data-e2e="comment-list"]');
    var p = l || document.body;
    var sc = null;
    for (var i = 0; i < 12 && p; i++) {
      if (p.scrollHeight > p.clientHeight + 30) { sc = p; break; }
      p = p.parentElement;
    }
    if (!sc) return 'no-scroller';
    var before = sc.scrollTop;
    sc.scrollTop = before + 700;
    return JSON.stringify({ moved: Math.round(sc.scrollTop - before), top: Math.round(sc.scrollTop) });
  };

  window.__dyCommentBox = function () {
    var list = document.querySelector('[data-e2e="comment-list"]');
    if (!list) return '';
    var r = list.getBoundingClientRect();
    // 注意：不能把坐标夹到视口内，面板经常在视口下方，抖音照样会吃这个坐标上的滚轮
    var x = Math.round(r.x + r.width / 2);
    var y = Math.round(r.y + Math.max(r.height / 2, 200));
    return JSON.stringify({ x: x, y: y, h: Math.round(r.height), top: Math.round(r.y) });
  };

  window.__dyCloseTip = function () {
    var all = document.querySelectorAll('button, div, span');
    for (var i = 0; i < all.length; i++) {
      var t = (all[i].innerText || '').trim();
      if (t === '我知道了') { all[i].click(); return 'closed'; }
    }
    return 'none';
  };

  window.__dyOpenComments = function () {
    var el = document.querySelector('[data-e2e="feed-comment-icon"]');
    if (!el) return '';
    var r = el.getBoundingClientRect();
    return JSON.stringify({ x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2) });
  };
})();
