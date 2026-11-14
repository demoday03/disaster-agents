// 세 화면(주민·봉사자·담당자) 공통 보조 기능. 화면 코드가 목록을 다시 그려도 알아서 따라간다.
//  - data-fold="N" : 목록이 길면 앞의 N개만 보이고 나머지는 'M건 더 보기'로 펼친다(표는 tbody의 줄 단위).
//  - data-sync="이름" : 같은 이름의 <select>는 한 곳에서 고르면 다른 곳도 같은 값이 된다(가구·신고·구역을 화면마다 다시 고르지 않게).
//  - details.acc : 접고 펼치는 묶음(아코디언) 모양.
//  - UI.urgentBar : 긴급 공지 띠. 접으면 한 줄('긴급 공지 N건 · 펼치기')만 남고, 새 긴급 공지가 오면 다시 펼친다.
//  - 버튼 쿨타임 : 누른 버튼·보낸 양식은 1.5초 동안 다시 눌러도 무시한다(연타로 신고·승인·요청이 겹치지 않게).
//  - 접속 환경 : 서버에 기기 종류(스마트폰·태블릿·노트북/PC)를 묻고 <html data-device>에 적는다. 컴퓨터면 '전화는 휴대폰으로',
//    'GPS가 부정확할 수 있어요'를 보여 주고, 카카오톡 등 앱 안에서 열렸으면 '다른 브라우저로 열기'를 안내한다.
(function () {
  const css = `
.fold-hide{display:none!important}
.fold-more{display:block;width:100%;margin:8px 0 0!important;background:#F1F4F9!important;color:#1F5FD1!important;border:1px dashed #BFC9D8!important;font-weight:700;border-radius:10px;padding:10px 14px!important;min-height:44px;cursor:pointer;text-align:center}
.fold-more:hover{background:#E8F0FD!important;filter:none}
details.acc{border:1px solid #D5DCE7;border-radius:12px;background:#fff;margin:10px 0;overflow:hidden}
details.acc>summary{list-style:none;cursor:pointer;display:flex;align-items:center;gap:10px;padding:14px 16px;font-weight:700;min-height:52px}
details.acc>summary::-webkit-details-marker{display:none}
details.acc>summary::after{content:"";margin-left:auto;flex:none;width:10px;height:10px;border-right:2.5px solid #5A6880;border-bottom:2.5px solid #5A6880;transform:rotate(45deg) translate(-2px,-2px);transition:transform .15s}
details.acc[open]>summary::after{transform:rotate(-135deg) translate(-2px,-2px)}
details.acc>summary .sum-sub{font-weight:500;color:#5A6880;font-size:.88rem}
details.acc>.acc-body{padding:0 16px 16px}
details.acc[open]>summary{border-bottom:1px solid #E7ECF3;margin-bottom:12px}
details.desc{margin:0 0 10px;color:#5A6880;font-size:.9rem}
details.desc>summary{cursor:pointer;font-weight:600;color:#3B4A63;display:inline-block;padding:2px 0}
details.desc>p{margin:6px 0 0}
button.cooling{opacity:.6;cursor:progress}
[data-urgentbar]:not([hidden]){display:flex;align-items:center;gap:8px 12px;flex-wrap:wrap}
[data-urgentbar] .ann-body{flex:1 1 220px;min-width:0}
[data-urgentbar] .ann-body p{margin:4px 0 0}
[data-urgentbar].ann-folded{flex-wrap:nowrap}
[data-urgentbar] .ann-line{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;cursor:pointer;font-size:1rem}
.ann-tg{margin:0!important;background:#fff!important;color:#A3182A!important;border:1.5px solid #F0B7BE!important;border-radius:99px!important;min-height:36px!important;padding:4px 14px!important;font:inherit;font-size:.9rem!important;font-weight:700;cursor:pointer;white-space:nowrap}

.pc-only{display:none}html[data-device=pc] .pc-only{display:block}html[data-device=pc] .not-pc{display:none}
.env-note{margin:8px 0 0;padding:8px 12px;border-radius:10px;background:#FFF6E0;color:#6B4A00;font-size:.9rem;line-height:1.45}
.env-bar{position:sticky;top:0;z-index:50;display:flex;gap:8px 12px;align-items:center;flex-wrap:wrap;padding:10px 14px;background:#FFF6E0;color:#4A3500;border-bottom:1px solid #F0D58A;font-size:.92rem}
.env-bar span{flex:1 1 220px}.env-bar a,.env-bar button{margin:0!important;min-height:36px;padding:4px 12px!important;border-radius:99px;font:inherit;font-weight:700;font-size:.88rem;cursor:pointer}
.env-bar a{background:#FEE500;color:#191600;text-decoration:none;display:inline-flex;align-items:center}.env-bar button{background:#fff!important;color:#4A3500!important;border:1px solid #E2C46B!important}
@media(prefers-reduced-motion:reduce){details.acc>summary::after{transition:none}}`;
  const st = document.createElement('style');
  st.textContent = css;
  document.head.appendChild(st);

  // ---------------------------------------------------------------- 긴 목록 접기
  function items(c) {
    const body = c.querySelector(':scope > table > tbody, :scope tbody');
    const xs = body ? [...body.children] : [...c.children];
    return xs.filter(x => !x.hidden && x.tagName !== 'SCRIPT' && x.tagName !== 'H3');
  }
  function applyFold(c) {
    const n = Number(c.dataset.fold) || 5, xs = items(c);
    let btn = c._foldBtn;
    if (xs.length <= n) {
      xs.forEach(x => x.classList.remove('fold-hide'));
      if (btn) btn.hidden = true;
      return;
    }
    const open = c.dataset.foldOpen === '1';
    xs.forEach((x, i) => x.classList.toggle('fold-hide', !open && i >= n));
    if (!btn) {
      btn = c._foldBtn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'fold-more';
      btn.addEventListener('click', () => {
        c.dataset.foldOpen = c.dataset.foldOpen === '1' ? '' : '1';
        applyFold(c);
        if (c.dataset.foldOpen !== '1') c.scrollIntoView({block: 'nearest'});
      });
      c.insertAdjacentElement('afterend', btn);
    }
    btn.hidden = false;
    btn.setAttribute('aria-expanded', String(open));
    btn.textContent = open ? '접기' : `${xs.length - n}건 더 보기 (전체 ${xs.length}건)`;
  }
  function watchFold(c) {
    if (c._foldWatch) return;
    c._foldWatch = new MutationObserver(() => applyFold(c));
    c._foldWatch.observe(c, {childList: true, subtree: true, attributes: true, attributeFilter: ['hidden']});
    applyFold(c);
  }

  // ---------------------------------------------------------------- 같은 값 입력 맞추기
  const LAST = {};
  function syncFrom(sel) {
    const g = sel.dataset.sync, v = sel.value;
    if (!v) return;
    LAST[g] = v;
    document.querySelectorAll(`select[data-sync="${g}"]`).forEach(o => {
      if (o === sel || o.value === v || ![...o.options].some(x => x.value === v)) return;
      o.value = v;
      o.dispatchEvent(new Event('change', {bubbles: true}));
    });
  }
  function watchSync(sel) {
    if (sel._syncWatch) return;
    sel.addEventListener('change', () => syncFrom(sel));
    // 목록을 새로 채우면(innerHTML) 마지막으로 고른 값을 다시 고른다
    sel._syncWatch = new MutationObserver(() => {
      const v = LAST[sel.dataset.sync];
      if (v && sel.value !== v && [...sel.options].some(x => x.value === v)) {
        sel.value = v;
        sel.dispatchEvent(new Event('change', {bubbles: true}));
      }
    });
    sel._syncWatch.observe(sel, {childList: true});
  }

  // ---------------------------------------------------------------- 펼친 상태 기억
  // 목록을 15초마다 다시 그려도, data-key가 같은 <details>는 사용자가 펼쳐 둔 그대로 둔다
  const OPEN = {};
  document.addEventListener('toggle', e => { const d = e.target; if (d.dataset && d.dataset.key) OPEN[d.dataset.key] = d.open; }, true);
  new MutationObserver(ms => {
    for (const m of ms) for (const n of m.addedNodes) {
      if (n.nodeType !== 1) continue;
      const ds = n.matches?.('details[data-key]') ? [n] : n.querySelectorAll ? n.querySelectorAll('details[data-key]') : [];
      ds.forEach(d => { if (d.dataset.key in OPEN) d.open = OPEN[d.dataset.key]; });
    }
  }).observe(document.documentElement, {childList: true, subtree: true});

  // ---------------------------------------------------------------- 버튼 쿨타임(연타 방지)
  // 화면 이동·접기·대결 답 고르기처럼 빨리 눌러도 되는 버튼은 뺀다(data-nocool로 뺄 수도 있다)
  const COOL_MS = 1500;
  const NO_COOL = '.menu button, #tabs button, .fold-more, #types button, .seg button, [data-nocool], [data-edit], [data-editclose]';
  document.addEventListener('click', e => {
    const b = e.target.closest && e.target.closest('button');
    if (!b || b.matches(NO_COOL)) return;
    if (b.dataset.cooling) { e.preventDefault(); e.stopImmediatePropagation(); return; }
    b.dataset.cooling = '1';
    b.classList.add('cooling');
    setTimeout(() => { delete b.dataset.cooling; b.classList.remove('cooling'); }, COOL_MS);
  }, true);
  document.addEventListener('submit', e => {   // Enter 키로 보내는 경우까지
    const f = e.target;
    if (f.dataset.cooling) { e.preventDefault(); e.stopImmediatePropagation(); return; }
    f.dataset.cooling = '1';
    setTimeout(() => { delete f.dataset.cooling; }, COOL_MS);
  }, true);

  // ---------------------------------------------------------------- 긴급 공지 띠 접기
  // 긴급 정보가 사라지지 않게 접어도 한 줄은 남긴다. 접은 공지 ID를 이 기기에 기억해, 새 긴급 공지가 오면 다시 펼친다
  const FOLD_KEY = 'annFolded';
  const getFolded = () => { try { return JSON.parse(localStorage.getItem(FOLD_KEY) || '[]'); } catch (e) { return []; } };
  const setFolded = ids => { try { localStorage.setItem(FOLD_KEY, JSON.stringify(ids.slice(-50))); } catch (e) { /* 저장 불가 */ } };
  const escH = t => String(t ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'})[c]);
  // urgent: [{id, text}] (급한 순), more: {href, label} 또는 null, many: 펼쳤을 때 여러 건을 모두 보여 줄지
  function urgentBar(el, urgent, more, many) {
    el.dataset.urgentbar = '1';
    el.hidden = !urgent.length;
    if (!urgent.length) return;
    const ids = urgent.map(a => a.id), folded = getFolded(), isFolded = ids.every(id => folded.includes(id));
    el.classList.toggle('ann-folded', isFolded);
    const link = more ? ` <a href="${escH(more.href)}">${escH(more.label)}</a>` : '';
    el.innerHTML = isFolded
      ? `<div class="ann-line" title="${escH(urgent[0].text)}">📢 긴급 공지${urgent.length > 1 ? ` ${urgent.length}건` : ''} · ${escH(urgent[0].text)}</div>` +
        '<button type="button" class="ann-tg" data-nocool aria-expanded="false">펼치기 ▾</button>'
      : `<div class="ann-body">📢 긴급 공지${many ? urgent.slice(0, 3).map(a => `<p>${escH(a.text)}</p>`).join('')
          : ` · ${escH(urgent[0].text)}${urgent.length > 1 ? ` (외 ${urgent.length - 1}건)` : ''}`}${link}</div>` +
        '<button type="button" class="ann-tg" data-nocool aria-expanded="true">접기 ▴</button>';
    const toggle = () => {
      setFolded(isFolded ? getFolded().filter(x => !ids.includes(x)) : [...new Set([...getFolded(), ...ids])]);
      urgentBar(el, urgent, more, many);
    };
    el.querySelector('.ann-tg').addEventListener('click', toggle);
    el.querySelector('.ann-line')?.addEventListener('click', toggle);   // 접힌 줄을 눌러도 펼친다
  }

  function scan(root = document) {
    root.querySelectorAll('[data-fold]').forEach(watchFold);
    root.querySelectorAll('select[data-sync]').forEach(watchSync);
  }
  // ---------------------------------------------------------------- 접속 환경(스마트폰·태블릿·노트북/PC)
  let devTouch = 0;
  try { devTouch = navigator.maxTouchPoints || 0; } catch (e) { /* 오래된 브라우저 */ }
  const clientHints = () => ({touch: devTouch, w: screen.width, h: screen.height});
  const env = fetch(`/api/public/env?p=${encodeURIComponent(location.pathname)}&touch=${devTouch}&w=${screen.width}&h=${screen.height}`)
    .then(r => r.ok ? r.json() : Promise.reject()).then(d => d.device).catch(() => null);
  function applyEnv(d) {
    if (!d) return;
    document.documentElement.dataset.device = d.key;
    if (d.inapp) document.documentElement.dataset.inapp = '1';
    if (location.pathname.startsWith('/admin')) return;   // 담당자 화면은 안내하지 않는다
    if (d.key === 'pc') {   // 컴퓨터: 전화 버튼이 안 걸릴 수 있다(첫 번째 전화 버튼 묶음 아래에 한 번)
      const tel = [...document.querySelectorAll('a[href^="tel:"]')].find(a => !a.closest('header'));
      const box = tel && (tel.closest('[data-callgroup]') || tel.parentElement);
      if (box && !box.querySelector('.env-note') && !(box.nextElementSibling || {}).classList?.contains('env-note')) {
        const n = document.createElement('p');
        n.className = 'env-note';
        n.textContent = '💻 컴퓨터에서는 전화가 걸리지 않을 수 있어요. 위험하면 휴대폰으로 119·112에 바로 전화하세요.';
        box.after(n);
      }
    }
    if (d.inapp && !sessionStorageGet('envbar')) {   // 카카오톡·인스타그램 등 앱 안 브라우저
      const bar = document.createElement('div');
      bar.className = 'env-bar';
      bar.setAttribute('role', 'note');
      const kakao = /카카오톡/.test(d.browser) && /Android/.test(d.os);
      bar.innerHTML = `<span>💬 ${escH(d.browser)} 안에서 열렸어요. 위치 보내기·알림·저장이 잘 안 될 수 있어요. ` +
        `${kakao ? '아래 버튼으로' : '오른쪽 위 ⋮ 또는 ··· 를 누르고 \'다른 브라우저로 열기\'로'} 열어 주세요.</span>` +
        (kakao ? `<a href="kakaotalk://web/openExternal?url=${encodeURIComponent(location.href)}">브라우저로 열기</a>` : '') +
        '<button type="button" data-nocool>닫기</button>';
      bar.querySelector('button').addEventListener('click', () => { bar.remove(); sessionStorageSet('envbar', '1'); });
      document.body.prepend(bar);
    }
  }
  function sessionStorageGet(k) { try { return sessionStorage.getItem(k); } catch (e) { return null; } }
  function sessionStorageSet(k, v) { try { sessionStorage.setItem(k, v); } catch (e) { /* 저장 불가 */ } }
  env.then(d => {
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => applyEnv(d));
    else applyEnv(d);
  });

  window.UI = {scan, applyFold, urgentBar, env, clientHints};
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => scan());
  else scan();
})();
