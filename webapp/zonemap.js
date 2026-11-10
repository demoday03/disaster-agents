// 모의 구역 지도(제안서 O1 구역 도식). 담당자 상황판·주민·봉사자 화면이 함께 쓴다.
// 구역(zones.json의 row·col)을 지도처럼 그린다: 바탕에 하천·바다·길(구역 사이 틈)을 깔고, 그 위에 구역 칸을 올린다.
// 칸에는 위험 유형(물결=침수, 빗금=사면), 신고(긴급도 색 점), 대피소 남은 자리, 핀(우리 집·작업 등)을 표시하고,
// 통제 구간은 두 구역 사이 길 위에 빨간 점선으로 그린다. 화면이 좁으면 지도를 세로(2열)로 돌려 그린다.
// 데이터는 부르는 쪽이 넘긴다. 구역 고르기 입력(zonePicker)도 이 지도를 쓴다.
(function () {
  const css = `
.zm{position:relative;--g:12px;padding:calc(var(--g)/2);border-radius:14px;background:#E4ECDD;border:1px solid #CBD6C4;overflow:hidden}
.zm .zbg{position:absolute;inset:0;width:100%;height:100%;pointer-events:none}
.zm .zgrid{position:relative;display:grid;gap:var(--g)}
.zt{position:relative;display:flex;flex-direction:column;gap:3px;min-height:112px;padding:9px 10px;border:1px solid #B9C6B3;border-radius:10px;background:rgba(255,255,255,.9);text-align:left;color:#16233B;font:inherit;font-weight:400;margin:0;cursor:default;overflow:hidden;min-width:0}
.zm.compact .zt{min-height:62px;padding:6px 8px}
button.zt{cursor:pointer}button.zt:hover{border-color:#16233B;filter:none}button.zt[aria-pressed="true"]{outline:3px solid #1F5FD1;outline-offset:1px;background:#F1F6FF}
.zt::before{content:"";position:absolute;inset:0;opacity:.5;pointer-events:none}
.zt.w::before{background:radial-gradient(14px 6px at 50% 100%,transparent 66%,rgba(47,123,217,.28) 70% 86%,transparent 90%) 0 0/22px 11px}
.zt.s::before{background:repeating-linear-gradient(135deg,rgba(160,112,58,.22) 0 2px,transparent 2px 9px)}
.zt.w.s::before{background:radial-gradient(14px 6px at 50% 100%,transparent 66%,rgba(47,123,217,.28) 70% 86%,transparent 90%) 0 0/22px 11px,repeating-linear-gradient(135deg,rgba(160,112,58,.2) 0 2px,transparent 2px 9px)}
.zt>*{position:relative}
.zt .zid{font-size:1.15rem;font-weight:800;line-height:1.1;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.zm.compact .zt .zid{font-size:1rem}
.zt .znm{font-size:.78rem;color:#3B4A63;line-height:1.3}
.zt .zpins{display:flex;flex-wrap:wrap;gap:3px;margin-top:auto}
.zt .pin{display:inline-flex;align-items:center;gap:3px;font-size:.74rem;font-weight:700;line-height:1.35;border-radius:6px;padding:1px 6px;background:#fff;border:1px solid #CBD4E1;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.zt .pin.home{background:#1F5FD1;border-color:#1F5FD1;color:#fff}
.zt .pin.me{background:#16233B;border-color:#16233B;color:#fff}
.zt .pin.alert{background:#FBE3E5;border-color:#F0B7BE;color:#A3182A}
.zt .pin.task{background:#FFF1D6;border-color:#E8B400;color:#6B4400}
.zt .pin.shel{background:#E1F3EA;border-color:#9FD3B6;color:#145C3A}.zt .pin.shel.full{background:#FBE3E5;border-color:#F0B7BE;color:#A3182A}
.zt .zinc{display:flex;flex-wrap:wrap;align-items:center;gap:3px;font-size:.76rem;font-weight:700}
.zt .dot{width:11px;height:11px;border-radius:50%;background:#9AA6B8;box-shadow:0 0 0 2px #fff}.zt .dot[data-u="1"]{background:#C8202F}.zt .dot[data-u="2"]{background:#E06A00}.zt .dot[data-u="3"]{background:#D9A400}
.zt.hot{border-color:#C8202F;box-shadow:inset 0 0 0 1px #C8202F}.zt.hl{border-color:#1F5FD1;box-shadow:inset 0 0 0 2px #1F5FD1}.zt.dim{opacity:.55}
.zt.hot .zid{color:#A3182A}
@keyframes zpulse{0%{box-shadow:0 0 0 2px #fff,0 0 0 2px rgba(200,32,47,.6)}100%{box-shadow:0 0 0 2px #fff,0 0 0 9px rgba(200,32,47,0)}}
.zt .dot[data-u="1"]{animation:zpulse 1.6s ease-out infinite}
.zm .zctl{position:absolute;transform:translate(-50%,-50%);z-index:2;font-size:.7rem;font-weight:800;color:#A3182A;background:#fff;border:1.5px solid #C8202F;border-radius:99px;padding:0 7px;white-space:nowrap;pointer-events:none;max-width:46%;overflow:hidden;text-overflow:ellipsis}
.zm .zctl.off{color:#5A6880;border-color:#9AA6B8;border-style:dashed}
.zm .zlbl{position:absolute;z-index:1;font-size:.68rem;font-weight:700;color:#2F5F9E;pointer-events:none;letter-spacing:.02em}
.zlegend{display:flex;flex-wrap:wrap;gap:4px 14px;margin-top:8px;font-size:.78rem;color:#5A6880}
.zlegend i{display:inline-block;width:18px;height:11px;border-radius:3px;vertical-align:-1px;margin-right:5px;border:1px solid #CBD4E1}
.zlegend .lw{background:radial-gradient(14px 6px at 50% 100%,transparent 66%,rgba(47,123,217,.45) 70% 86%,transparent 90%) 0 0/11px 6px}
.zlegend .ls{background:repeating-linear-gradient(135deg,rgba(160,112,58,.45) 0 2px,transparent 2px 5px)}
.zlegend .lc{border:0;border-top:2px dashed #C8202F;height:0;border-radius:0}
.zlegend .lr{border:0;height:4px;background:#5B9BE0;border-radius:2px}
.zlegend .ld{width:10px;height:10px;border-radius:50%;background:#C8202F;border:0}
.zlegend .lsea{background:#9CC3EA;border-color:#9CC3EA}
.zlegend .lh{background:#E1F3EA;border-color:#9FD3B6}
.zpick-now{font-size:.9rem;margin:6px 0 0;color:#3B4A63}.zpick-now b{color:#16233B}
.zpick-all{margin:8px 0 0!important;padding:6px 12px!important;min-height:36px!important;background:#E3E8F0!important;color:#16233B!important;font-size:.88rem}
.zpick-all[aria-pressed="true"]{background:#1F5FD1!important;color:#fff!important}
@media(prefers-reduced-motion:reduce){.zt .dot[data-u="1"]{animation:none}}`;
  const st = document.createElement('style');
  st.textContent = css;
  document.head.appendChild(st);

  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'})[c]);
  const short = name => (String(name).match(/\(([^)]+)\)/) || [, name])[1];
  const zoneNo = z => String(z.name).match(/^\d+구역/)?.[0] || z.id;
  // 지도 칸 안에 들어가게 대피소 이름을 줄인다(전체 이름은 title과 목록에)
  const shelterShort = n => String(n).replace(/\s*\(가상\)/, '').replace(/^\d+구역\s*/, '').replace('초등학교', '초').replace('중학교', '중').replace('고등학교', '고')
    .replace('종합사회복지관', '복지관').replace(/\s*(체육관|강당|대회의실)$/, '');
  const ctlName = n => String(n).replace(/\s*\(가상\)/, '');

  // opts: {zones:[{id,name,risk,row,col}], shelters:[{zone,name,capacity_left}], controls:[{name,between,active}],
  //        incidents:[{zone,urgency}], pins:[{zone,label,kind}], highlight:Set, dim:Set, selected,
  //        onPick(zoneId)|null, compact, showInactive, legend(false면 숨김)}
  const MAPS = new Set();
  window.renderZoneMap = function (el, opts) {
    el._zmOpts = opts;
    MAPS.add(el);
    const zones = opts.zones || [];
    if (!zones.length) { el.textContent = '구역 정보를 불러오지 못했습니다.'; return; }
    const R = Math.max(...zones.map(z => z.row)) + 1, C = Math.max(...zones.map(z => z.col)) + 1;
    const width = el.clientWidth || el.parentElement?.clientWidth || 800;
    const portrait = R < C && width < (opts.compact ? 460 : 600);   // 좁은 화면은 지도를 세로로 돌린다
    el._zmPortrait = portrait;
    const W = portrait ? R : C, H = portrait ? C : R;
    // 격자 좌표(행, 열)를 그림 좌표(가로 x, 세로 y)로. 세로 지도는 행과 열을 맞바꾼다
    const P = (r, c) => portrait ? [r, c] : [c, r];
    const pt = (r, c) => P(r, c).join(',');
    const pos = Object.fromEntries(zones.map(z => [z.id, z]));
    const inc = {}, sh = {}, pins = {};
    (opts.incidents || []).forEach(i => { (inc[i.zone] = inc[i.zone] || []).push(i.urgency); });
    (opts.shelters || []).forEach(s => { (sh[s.zone] = sh[s.zone] || []).push(s); });
    (opts.pins || []).forEach(p => { (pins[p.zone] = pins[p.zone] || []).push(p); });

    // 바탕: 하천(7구역 상류 → 1구역 하류), 바다(9·10구역 아래), 구역 사이 길은 격자 틈으로 보인다
    const river = [[2, 1.55], [1.62, 1.35], [1.25, 1.12], [1, 0.95], [0.78, 0.6], [0.62, 0.25], [0.6, 0]];
    const riverD = 'M' + river.map(([r, c]) => pt(r, c)).join(' L');
    const sea = [[2, 2.85], [1.86, 3.1], [1.9, 3.5], [1.84, 3.9], [1.9, 4.3], [1.85, 4.7], [1.88, 5], [2, 5]].filter(([, c]) => c <= C);
    const seaD = R >= 2 && C >= 4 ? 'M' + sea.map(([r, c]) => pt(r, c)).join(' L') + ' Z' : '';
    const ctlSvg = [], ctlLbl = [], other = [];
    (opts.controls || []).filter(c => c.active !== false || opts.showInactive).forEach(c => {
      const [a, b] = (c.between || []).map(id => pos[id]);
      const on = c.active !== false;
      if (!a || !b || Math.abs(a.row - b.row) + Math.abs(a.col - b.col) !== 1) { if (on) other.push(ctlName(c.name)); return; }
      // 두 구역이 맞닿은 변(구역 사이 길)의 가운데 부분
      let s, e;
      if (a.row !== b.row) { const r = Math.max(a.row, b.row); s = [r, a.col + 0.18]; e = [r, a.col + 0.82]; }
      else { const cc = Math.max(a.col, b.col); s = [a.row + 0.18, cc]; e = [a.row + 0.82, cc]; }
      ctlSvg.push(`<line x1="${P(...s)[0]}" y1="${P(...s)[1]}" x2="${P(...e)[0]}" y2="${P(...e)[1]}" stroke="${on ? '#C8202F' : '#9AA6B8'}" stroke-width="${on ? 5 : 3}" stroke-dasharray="${on ? '7 5' : '3 5'}" stroke-linecap="round" vector-effect="non-scaling-stroke"/>`);
      const [mx, my] = P((s[0] + e[0]) / 2, (s[1] + e[1]) / 2);
      if (on) ctlLbl.push(`<span class="zctl${on ? '' : ' off'}" style="left:${(100 * mx / W).toFixed(2)}%;top:${(100 * my / H).toFixed(2)}%">${on ? '⛔ ' : ''}${esc(ctlName(c.name))}${on ? '' : ' (해제)'}</span>`);
    });
    const svg = `<svg class="zbg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true">` +
      (seaD ? `<path d="${seaD}" fill="#9CC3EA" opacity=".85"/>` : '') +
      `<path d="${riverD}" fill="none" stroke="#5B9BE0" stroke-width="9" stroke-linecap="round" stroke-linejoin="round" vector-effect="non-scaling-stroke" opacity=".9"/>` +
      ctlSvg.join('') + '</svg>';

    const tile = z => {
      const us = (inc[z.id] || []).sort((a, b) => (a ?? 9) - (b ?? 9));
      const shs = sh[z.id] || [];
      const left = shs.reduce((n, s) => n + Math.max(0, s.capacity_left), 0);
      const cls = ['zt', (z.risk || []).includes('침수') ? 'w' : '', (z.risk || []).includes('사면') ? 's' : '',
        us[0] === 1 ? 'hot' : '', opts.highlight?.has(z.id) ? 'hl' : '', opts.dim?.has(z.id) ? 'dim' : ''].filter(Boolean).join(' ');
      const tag = opts.onPick ? 'button' : 'div';
      const [x, y] = P(z.row, z.col);
      const label = `${z.name}. 위험 ${(z.risk || []).join('·') || '없음'}.` + (opts.incidents ? ` 신고 ${us.length}건.` : '') +
        (shs.length ? ` 대피소 ${shs.length}곳, 남은 자리 ${left}명.` : '') + (pins[z.id] || []).map(p => ` ${p.label}.`).join('');
      const pinHtml = (pins[z.id] || []).map(p => `<span class="pin ${esc(p.kind || '')}">${esc(p.label)}</span>`).join('') +
        (opts.compact
          ? (shs.length ? `<span class="pin shel${left ? '' : ' full'}">대피소 ${shs.length}</span>` : '')
          : shs.map(s => `<span class="pin shel${s.capacity_left > 0 ? '' : ' full'}" title="${esc(s.name)}">${esc(shelterShort(s.name))} ${s.capacity_left > 0 ? esc(s.capacity_left) + '명' : '가득'}</span>`).join(''));
      return `<${tag} class="${cls}" style="grid-row:${y + 1};grid-column:${x + 1}" data-zone="${esc(z.id)}"` +
        `${opts.onPick ? ` type="button" aria-pressed="${opts.selected === z.id}"` : ''} aria-label="${esc(label)}">` +
        `<span class="zid">${esc(zoneNo(z))}</span>${opts.compact ? '' : `<span class="znm">${esc(short(z.name))}</span>`}` +
        `<span class="zpins">${pinHtml}</span>` +
        (us.length ? `<span class="zinc">${us.slice(0, 8).map(u => `<span class="dot" data-u="${esc(u ?? '')}"></span>`).join('')} 신고 ${us.length}</span>` : '') +
        `</${tag}>`;
    };
    const marks = '';
    let html = `<div class="zm${opts.compact ? ' compact' : ''}${portrait ? ' portrait' : ''}">${svg}` +
      `<div class="zgrid" style="grid-template-columns:repeat(${W},minmax(0,1fr))">${zones.map(tile).join('')}</div>${ctlLbl.join('')}${marks}</div>`;
    if (opts.legend !== false && !opts.compact) {
      html += '<div class="zlegend"><span><i class="lw"></i>침수 위험</span><span><i class="ls"></i>사면 위험</span><span><i class="lr"></i>하천</span>' +
        (seaD ? '<span><i class="lsea"></i>바다</span>' : '') + ((opts.shelters || []).length ? '<span><i class="lh"></i>대피소 · 남은 자리</span>' : '') +
        (opts.incidents ? '<span><i class="ld"></i>신고(색=긴급도)</span>' : '') + '<span><i class="lc"></i>통제 구간(지나가지 마세요)</span>' + (opts.showInactive ? '<span><i class="lc" style="border-top-color:#9AA6B8"></i>해제된 구간</span>' : '') +
        (other.length ? `<span>그 밖의 통제: ${esc(other.join(', '))}</span>` : '') +
        '<span>구역·대피소는 가상 데이터</span></div>';
    }
    el.innerHTML = html;
    if (opts.onPick) el.querySelectorAll('button.zt').forEach(b => b.addEventListener('click', () => opts.onPick(b.dataset.zone)));
  };

  // 화면 폭이 바뀌어 가로/세로 지도가 달라져야 하면 같은 데이터로 다시 그린다
  let rt = null;
  window.addEventListener('resize', () => {
    clearTimeout(rt);
    rt = setTimeout(() => MAPS.forEach(el => {
      if (!el.isConnected) { MAPS.delete(el); return; }
      const o = el._zmOpts, R = Math.max(...(o.zones || [{row: 0}]).map(z => z.row)) + 1, C = Math.max(...(o.zones || [{col: 0}]).map(z => z.col)) + 1;
      const want = R < C && el.clientWidth < (o.compact ? 460 : 600);
      if (want !== el._zmPortrait) window.renderZoneMap(el, o);
    }), 150);
  });

  // 구역 고르기: <select>의 값을 지도에서 누른 구역으로 정한다(select는 숨겨 두고 값만 쓴다).
  // 같은 화면의 다른 입력과 값이 겹치지 않도록 select 하나가 정답이고, 지도는 그 select를 바꾸는 입력일 뿐이다.
  let ZONES_P = null;
  const loadZones = () => ZONES_P || (ZONES_P = fetch('/api/public/status').then(r => r.json()).then(d => d.zone_grid || []).catch(() => { ZONES_P = null; return []; }));
  window.zonePicker = function (select, cfg = {}) {
    if (select._zp) { select._zp.redraw(); return select._zp; }
    const box = document.createElement('div');
    box.className = 'zpick';
    select.insertAdjacentElement('afterend', box);
    select.hidden = true;
    const mapEl = document.createElement('div'), now = document.createElement('p');
    now.className = 'zpick-now';
    box.append(mapEl, now);
    let allBtn = null;
    const allOpt = () => [...select.options].find(o => o.value && !/^Z\d+$/.test(o.value));   // '전체' 같은 특별 값
    const redraw = async () => {
      const zones = cfg.zones || await loadZones();
      if (!zones.length) { select.hidden = false; box.hidden = true; return; }
      const v = select.value;
      window.renderZoneMap(mapEl, {zones, compact: true, selected: v, controls: cfg.controls || [], shelters: cfg.shelters || [],
        pins: typeof cfg.pins === 'function' ? cfg.pins() : (cfg.pins || []),
        onPick: z => { select.value = z; select.dispatchEvent(new Event('change', {bubbles: true})); redraw(); }});
      const a = allOpt();
      if (a && !allBtn) {
        allBtn = document.createElement('button');
        allBtn.type = 'button'; allBtn.className = 'zpick-all';
        allBtn.addEventListener('click', () => { select.value = a.value; select.dispatchEvent(new Event('change', {bubbles: true})); redraw(); });
        box.insertBefore(allBtn, now);
      }
      if (allBtn && a) { allBtn.textContent = a.textContent + ' 구역'; allBtn.setAttribute('aria-pressed', String(v === a.value)); }
      const opt = select.selectedOptions[0];
      now.innerHTML = v ? `고른 곳: <b>${esc(opt ? opt.textContent : v)}</b>` : '<span>지도에서 구역을 눌러 고르세요.</span>';
    };
    select.addEventListener('change', () => redraw());
    select._zp = {redraw, box};
    redraw();
    return select._zp;
  };
})();
