// 모의 구역도(제안서 O1 구역 도식). 담당자 상황판과 시민 화면이 함께 쓴다.
// 구역(zones.json의 row·col)을 격자로 그리고, 위험 유형(물결=침수, 빗금=사면), 신고(긴급도 색 점),
// 대피소 남은 자리, 통제 구간(위아래 구역 사이 빨간 점선)을 올린다. 데이터는 부르는 쪽이 넘긴다.
(function () {
  const css = `
.zmap{--zw:#2F7BD9;--zs:#A0703A;position:relative;display:grid;gap:0;min-width:620px}
.zmap .zrow{display:grid;grid-template-columns:repeat(var(--cols),minmax(0,1fr));gap:8px}
.zmap .zgap{display:grid;grid-template-columns:repeat(var(--cols),minmax(0,1fr));gap:8px;height:22px;align-items:center}
.zmap .zgap span{display:flex;align-items:center;justify-content:center;gap:6px;font-size:.74rem;font-weight:700;color:#A3182A;white-space:nowrap;overflow:hidden}
.zmap .zgap span.on::before,.zmap .zgap span.on::after{content:"";flex:1;border-top:2px dashed #C8202F}
.zt{position:relative;display:flex;flex-direction:column;gap:4px;min-height:118px;padding:10px 11px;border:1px solid #CBD4E1;border-radius:10px;background:#fff;text-align:left;color:#16233B;font:inherit;font-weight:400;margin:0;cursor:default;overflow:hidden}
button.zt{cursor:pointer}button.zt:hover{border-color:#16233B;filter:none}button.zt[aria-pressed="true"]{outline:3px solid #16233B;outline-offset:1px}
.zt::before{content:"";position:absolute;inset:0;opacity:.55;pointer-events:none}
.zt.w::before{background:radial-gradient(14px 6px at 50% 100%,transparent 66%,rgba(47,123,217,.28) 70% 86%,transparent 90%) 0 0/22px 11px}
.zt.s::before{background:repeating-linear-gradient(135deg,rgba(160,112,58,.22) 0 2px,transparent 2px 9px)}
.zt.w.s::before{background:radial-gradient(14px 6px at 50% 100%,transparent 66%,rgba(47,123,217,.28) 70% 86%,transparent 90%) 0 0/22px 11px,repeating-linear-gradient(135deg,rgba(160,112,58,.2) 0 2px,transparent 2px 9px)}
.zt>*{position:relative}
.zt .zid{font-size:1.35rem;font-weight:800;line-height:1;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.zt .znm{font-size:.8rem;color:#3B4A63;line-height:1.3}
.zt .zinc{display:flex;flex-wrap:wrap;align-items:center;gap:3px;margin-top:auto;font-size:.78rem;font-weight:700}
.zt .dot{width:11px;height:11px;border-radius:50%;background:#9AA6B8;box-shadow:0 0 0 2px #fff}.zt .dot[data-u="1"]{background:#C8202F}.zt .dot[data-u="2"]{background:#E06A00}.zt .dot[data-u="3"]{background:#D9A400}
.zt .zsh{font-size:.76rem;color:#3B4A63;background:rgba(255,255,255,.85);border-radius:6px;padding:1px 5px;align-self:flex-start}.zt .zsh.full{color:#A3182A;font-weight:700}
.zt.hot{border-color:#C8202F;box-shadow:inset 0 0 0 1px #C8202F}.zt.hl{border-color:#1F5FD1;box-shadow:inset 0 0 0 2px #1F5FD1}
.zt.hot .zid{color:#A3182A}
@keyframes zpulse{0%{box-shadow:0 0 0 2px #fff,0 0 0 2px rgba(200,32,47,.6)}100%{box-shadow:0 0 0 2px #fff,0 0 0 9px rgba(200,32,47,0)}}
.zt .dot[data-u="1"]{animation:zpulse 1.6s ease-out infinite}
.zlegend{display:flex;flex-wrap:wrap;gap:4px 16px;margin-top:10px;font-size:.8rem;color:#5A6880}
.zlegend i{display:inline-block;width:18px;height:11px;border-radius:3px;vertical-align:-1px;margin-right:5px;border:1px solid #CBD4E1}
.zlegend .lw{background:radial-gradient(14px 6px at 50% 100%,transparent 66%,rgba(47,123,217,.45) 70% 86%,transparent 90%) 0 0/11px 6px}
.zlegend .ls{background:repeating-linear-gradient(135deg,rgba(160,112,58,.45) 0 2px,transparent 2px 5px)}
.zlegend .lc{border:0;border-top:2px dashed #C8202F;height:0;border-radius:0}
.zlegend .ld{width:10px;height:10px;border-radius:50%;background:#C8202F;border:0}
.zscroll{overflow-x:auto;padding-bottom:4px}
@media(prefers-reduced-motion:reduce){.zt .dot[data-u="1"]{animation:none}}`;
  const st = document.createElement('style');
  st.textContent = css;
  document.head.appendChild(st);

  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'})[c]);
  const short = name => (String(name).match(/\(([^)]+)\)/) || [, name])[1];

  // opts: {zones:[{id,name,risk,row,col}], shelters:[{zone,capacity_left}], controls:[{name,between,active}],
  //        incidents:[{zone,urgency}], highlight:Set, selected, onPick(zoneId)|null}
  window.renderZoneMap = function (el, opts) {
    const zones = opts.zones || [];
    const rows = Math.max(...zones.map(z => z.row)) + 1, cols = Math.max(...zones.map(z => z.col)) + 1;
    const at = {};
    zones.forEach(z => { at[z.row + ',' + z.col] = z; });
    const inc = {}, sh = {};
    (opts.incidents || []).forEach(i => { (inc[i.zone] = inc[i.zone] || []).push(i.urgency); });
    (opts.shelters || []).forEach(s => { (sh[s.zone] = sh[s.zone] || []).push(s); });
    const pos = Object.fromEntries(zones.map(z => [z.id, z]));
    const tile = z => {
      if (!z) return '<div></div>';
      const us = (inc[z.id] || []).sort((a, b) => (a ?? 9) - (b ?? 9));
      const left = (sh[z.id] || []).reduce((n, s) => n + Math.max(0, s.capacity_left), 0);
      const cls = ['zt', (z.risk || []).includes('침수') ? 'w' : '', (z.risk || []).includes('사면') ? 's' : '',
        us[0] === 1 ? 'hot' : '', opts.highlight?.has(z.id) ? 'hl' : ''].filter(Boolean).join(' ');
      const tag = opts.onPick ? 'button' : 'div';
      const label = `${z.name}. 위험 ${(z.risk || []).join('·') || '없음'}. 신고 ${us.length}건.` +
        (sh[z.id] ? ` 대피소 ${sh[z.id].length}곳, 남은 자리 ${left}명.` : '');
      return `<${tag} class="${cls}" data-zone="${esc(z.id)}"${opts.onPick ? ` type="button" aria-pressed="${opts.selected === z.id}"` : ''} aria-label="${esc(label)}">` +
        `<span class="zid">${esc(z.id)}</span><span class="znm">${esc(short(z.name))}</span>` +
        (sh[z.id] ? `<span class="zsh${left ? '' : ' full'}">대피소 ${sh[z.id].length} · ${left ? `${left}명 가능` : '가득 참'}</span>` : '') +
        `<span class="zinc">${us.slice(0, 8).map(u => `<span class="dot" data-u="${esc(u ?? '')}"></span>`).join('')}${us.length ? ` 신고 ${us.length}` : ''}</span></${tag}>`;
    };
    // 위아래로 붙은 두 구역 사이의 통제 구간만 격자 틈에 그린다(그 밖의 구간은 범례 아래 목록으로)
    const gapAt = {}, other = [];
    (opts.controls || []).filter(c => c.active !== false).forEach(c => {
      const [a, b] = (c.between || []).map(id => pos[id]);
      if (a && b && a.col === b.col && Math.abs(a.row - b.row) === 1) gapAt[Math.min(a.row, b.row) + ',' + a.col] = c.name;
      else other.push(c.name);
    });
    let html = `<div class="zscroll"><div class="zmap" style="--cols:${cols}">`;
    for (let r = 0; r < rows; r++) {
      html += `<div class="zrow">${Array.from({length: cols}, (_, c) => tile(at[r + ',' + c])).join('')}</div>`;
      if (r < rows - 1) html += `<div class="zgap" aria-hidden="true">${Array.from({length: cols}, (_, c) =>
        gapAt[r + ',' + c] ? `<span class="on">통제 ${esc(gapAt[r + ',' + c].replace(/\s*\(가상\)/, ''))}</span>` : '<span></span>').join('')}</div>`;
    }
    html += '</div></div><div class="zlegend"><span><i class="lw"></i>침수 위험</span><span><i class="ls"></i>사면 위험</span>' +
      (opts.incidents ? '<span><i class="ld"></i>신고(색=긴급도)</span>' : '') + '<span><i class="lc"></i>통제 구간</span>' +
      (other.length ? `<span>그 밖의 통제: ${esc(other.map(n => n.replace(/\s*\(가상\)/, '')).join(', '))}</span>` : '') +
      '<span>구역·대피소는 가상 데이터</span></div>';
    el.innerHTML = html;
    if (opts.onPick) el.querySelectorAll('button.zt').forEach(b => b.addEventListener('click', () => opts.onPick(b.dataset.zone)));
  };
})();
