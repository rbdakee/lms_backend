// Замер вместимости слотов и снимки previews/ в headless Chromium.
// playwright берётся из NODE_PATH, браузер — из CHROMIUM (или свой у playwright).
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const [mode, out] = process.argv.slice(2);

// Сколько знаков помещается в слот: текст из обычных слов урока, для bullets —
// max_items пунктов одной длины. Ряды таблицы и пар обязаны быть в одну строку.
function measureAll() {
  const words = ('сегодня мы узнаем как правильно говорить о себе и друзьях составим короткие ' +
    'предложения прочитаем текст ответим на вопросы учителя и проверим себя в паре').split(' ');
  const text = n => {
    let s = '';
    for (let i = 0; s.length < n; i++) s += (s ? ' ' : '') + words[i % words.length];
    return s.slice(0, n).trim();
  };
  const res = {};
  for (const el of document.querySelectorAll('.in[data-slot]')) {
    const key = el.closest('.slide').dataset.key;
    const box = el.parentElement;
    const cs = getComputedStyle(box);
    const avail = box.clientHeight - parseFloat(cs.paddingTop) - parseFloat(cs.paddingBottom);
    const k = +el.dataset.items;
    const oneline = el.dataset.oneline === '1';
    const proto = el.firstElementChild.cloneNode(true);
    const gap = proto.style.marginBottom;
    const fits = n => {
      el.innerHTML = '';
      for (let i = 0; i < k; i++) {
        const p = proto.cloneNode(true);
        p.lastChild.textContent = text(n);
        p.style.marginBottom = i < k - 1 ? gap : '0';
        el.appendChild(p);
      }
      if (el.scrollHeight > avail + 1 || el.scrollWidth > el.clientWidth + 1) return false;
      if (!oneline) return true;
      const line = parseFloat(getComputedStyle(el.firstElementChild).fontSize) * 1.2;
      return [...el.children].every(p => p.getBoundingClientRect().height <= line * 1.3);
    };
    let lo = 1, hi = 400;
    while (lo < hi) {
      const m = Math.ceil((lo + hi) / 2);
      if (fits(m)) lo = m; else hi = m - 1;
    }
    (res[key] ||= {})[el.dataset.slot] = lo;
  }
  return res;
}

(async () => {
  const browser = await chromium.launch({ executablePath: process.env.CHROMIUM || undefined });
  const page = await browser.newPage({ viewport: { width: 1280, height: 720 }, deviceScaleFactor: 1.5 });
  if (mode === 'measure') {
    await page.goto('file://' + path.resolve(out, 'measure.html'));
    const res = await page.evaluate(measureAll);
    fs.writeFileSync(path.resolve(out, 'measure.json'), JSON.stringify(res, null, 1));
  } else {
    const dir = path.resolve(out, 'html');
    const pv = path.resolve(out, 'previews');
    fs.mkdirSync(pv, { recursive: true });
    for (const f of fs.readdirSync(dir).filter(f => f.endsWith('.html'))) {
      await page.goto('file://' + path.join(dir, f));
      await page.screenshot({ path: path.join(pv, f.replace('.html', '.png')) });
    }
  }
  await browser.close();
})();
