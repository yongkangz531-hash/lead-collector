(() => {
  const cards = [...document.querySelectorAll('section.note-item')];
  const out = cards.map(c => {
    const a = c.querySelector('a[href*="/explore/"], a[href*="/search_result/"]');
    const titleEl = c.querySelector('.title, .footer .title, span');
    const txt = (c.innerText || '').split('\n').filter(s => s.trim());
    return {
      href: a ? a.getAttribute('href') : '',
      title: titleEl ? titleEl.innerText.trim() : (txt[0] || ''),
      author: txt[1] || '',
      likes: txt.find(t => /赞$|收藏$/.test(t)) || ''
    };
  }).filter(x => x.href);
  return JSON.stringify({
    title: document.title,
    needLogin: document.body.innerText.indexOf('登录后查看搜索') >= 0,
    count: out.length,
    notes: out.slice(0, 20)
  });
})()
