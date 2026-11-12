// 세 화면(주민·봉사자·담당자) 공통 보조 기능. 화면 코드가 목록을 다시 그려도 알아서 따라간다.
//  - data-fold="N" : 목록이 길면 앞의 N개만 보이고 나머지는 'M건 더 보기'로 펼친다(표는 tbody의 줄 단위).
//  - data-sync="이름" : 같은 이름의 <select>는 한 곳에서 고르면 다른 곳도 같은 값이 된다(가구·신고·구역을 화면마다 다시 고르지 않게).
//  - details.acc : 접고 펼치는 묶음(아코디언) 모양.
//  - 버튼 쿨타임 : 누른 버튼·보낸 양식은 1.5초 동안 다시 눌러도 무시한다(연타로 신고·승인·요청이 겹치지 않게).
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

  function scan(root = document) {
    root.querySelectorAll('[data-fold]').forEach(watchFold);
    root.querySelectorAll('select[data-sync]').forEach(watchSync);
  }
  window.UI = {scan, applyFold};
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => scan());
  else scan();
})();
