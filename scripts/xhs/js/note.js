(() => {
  const pick = (sels, root) => {
    for (const s of sels) {
      const e = (root || document).querySelector(s);
      if (e && e.innerText && e.innerText.trim()) return e.innerText.trim();
    }
    return '';
  };
  const comments = [...document.querySelectorAll('.comment-item, .parent-comment .comment-item, [class*="comment-item"]')];
  const rows = comments.map(c => {
    const name = pick(['.author .name', '.name', '.user-name', 'a.name'], c);
    const text = pick(['.content .note-text', '.note-text', '.content', 'span'], c);
    const like = pick(['.like-wrapper .count', '.count', '[class*="like"] .count'], c);
    let u = '';
    const a = c.querySelector('a[href*="/user/profile/"]');
    if (a) {
      const href = a.getAttribute('href') || '';
      if (/\/user\/profile\/[0-9a-f]{8,}/.test(href)) {
        u = href.startsWith('http') ? href : ('https://www.xiaohongshu.com' + href);
      }
    }
    const locEl = c.querySelector('.date .location, .location');
    const ip = locEl ? locEl.innerText.trim() : '';
    const tspan = c.querySelector('.date span:not(.location)');
    const ctime = tspan ? tspan.innerText.trim() : '';
    return { name: name.replace(/\s+/g, ' ').slice(0, 30), text: text.replace(/\s+/g, ' ').slice(0, 160),
             like: like, u: u, ip: ip.slice(0, 12), ctime: ctime.slice(0, 16) };
  }).filter(r => r.text && r.name && r.text.length > 3);

  const noteLikes = pick(['.interact-container .like-wrapper .count',
                          '.engage-bar .like-wrapper .count',
                          '#noteContainer .like-wrapper .count']);
  const noteTags = (function(){
    const a = [].slice.call(document.querySelectorAll('#detail-desc a, .note-content .tag'))
      .map(e => (e.innerText || '').trim()).filter(Boolean).slice(0, 6);
    return a.join(' ');
  })();
  const noteDate = pick(['.note-content .date', '#noteContainer .bottom-container .date', '.bottom-container .date']);
  const noteTitle = pick(['#detail-title', '.note-content .title', '.title', 'h1']);
  const noteText = pick(['#detail-desc', '.note-content .desc', '.desc']);
  return JSON.stringify({
    url: location.href,
    title: noteTitle.slice(0, 80),
    desc: noteText.replace(/\s+/g, ' ').slice(0, 120),
    likes: noteLikes.replace(/\s+/g, ''),
    tags: noteTags.slice(0, 40),
    date: noteDate.replace(/\s+/g, ' ').slice(0, 20),
    total: rows.length,
    comments: rows.slice(0, 40)
  });
})()
