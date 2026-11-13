// 관리자 화면 현장 운영 탭: ① 센서, ③ 안부, ⑤ 복구·지원, ⑥ 봉사, ⑦ 검증, ⑧ 물품.
// admin.html의 공통 함수($, api, escapeHtml, showError, loadApprovals, CSRF)를 그대로 쓴다.
// 화면에 넣는 값은 모두 escapeHtml을 거친다.
(() => {
  const E = escapeHtml;
  const post = (path, body) => api(path, {method: 'POST', body: JSON.stringify(body || {})});
  const chip = (text, cls = '') => `<span class="chip ${cls}">${E(text)}</span>`;
  const fmtTime = ts => ts ? new Date(ts * 1000).toLocaleTimeString('ko-KR') : '';
  const fmtDate = ts => ts ? new Date(ts * 1000).toLocaleString('ko-KR', {month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit'}) : '';
  const table = (heads, rows) => rows.length
    ? `<table class="tbl"><thead><tr>${heads.map(h => `<th>${E(h)}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table>`
    : '<div class="muted">항목이 없습니다.</div>';
  const options = (items, label = x => x, value = x => x) => items.map(x => `<option value="${E(value(x))}">${E(label(x))}</option>`).join('');
  const show = (id, html, isErr = false) => { const el = $(id); el.hidden = false; el.classList.toggle('err', isErr); el.innerHTML = html; };
  const fail = (id, e) => show(id, '오류: ' + E(e.message), true);
  const checked = name => [...document.querySelectorAll(`input[name=${name}]:checked`)].map(x => x.value);
  const STATE_CLS = {완료: 'ok', 안전: 'ok', 반려: 'bad', 격상: 'bad', 보류: 'warn', '지연 응답': 'warn', 재추천: 'warn'};
  let current = 'overview', META = null, refreshTimer = null, lastSensorApproval = null;

  // ---------------------------------------------------------------- 탭
  const LOADERS = {overview: () => { loadSituation(); }, notice: loadAnnouncements, supply: loadSupply, volunteer: loadVolunteer, recovery: loadRecovery,
                   welfare: loadChecks, sensor: () => { loadVerifyDocs(); loadSensorLive(); }, approvals: () => loadApprovals(),
                   dispatch: () => { loadDispatch(); loadControl(); loadShelterOps(); },
                   guidance: loadIncidents, system: () => { loadHealth(); loadAgents(); }};
  function go(tab) {
    current = tab;
    document.querySelectorAll('#tabs button').forEach(b => b.classList.toggle('on', b.dataset.go === tab));
    document.querySelectorAll('section[data-tab]').forEach(s => { s.hidden = s.dataset.tab !== tab; });
    clearInterval(refreshTimer);
    if (tab === 'welfare') refreshTimer = setInterval(loadChecks, 3000);   // 남은 시간·격상 자동 갱신
    if (tab === 'sensor') refreshTimer = setInterval(loadSensorLive, 2000);   // 게이트웨이 수신값 자동 갱신
    if (LOADERS[tab]) LOADERS[tab]();
    try { localStorage.setItem('adminTab', tab); } catch (e) { /* 저장 불가 환경 */ }
  }

  // 가구의 위치: 주민이 적은 주소, 없으면(시연 가구) 장소 설명
  const hhPlace = id => { const h = META && META.households.find(x => x.id === id); return h ? (h.address || place(h.zone)) : null; };

  // 대피소 운영(O6): 입소·퇴소 기록과 남은 자리
  async function loadShelterOps() {
    try {
      const d = await api('/api/dispatch');
      $('shelterOps').classList.remove('muted');
      $('shelterOps').innerHTML = d.shelters.map(s => {
        const pct = Math.round(100 * (s.capacity - s.capacity_left) / s.capacity);
        return `<div class="occ"><span class="nm"><b>${E(s.name)}</b> <span class="muted">${E(place(s.zone))} · ${E(s.type)}${s.amenities.length ? ' · ' + E(s.amenities.join(', ')) : ''}</span></span>` +
          `<span class="meter${s.capacity_left <= 0 ? ' full' : ''}" title="${pct}%"><span style="width:${Math.min(100, pct)}%"></span></span>` +
          `<span>${E(s.occupied)}명 입소 · 남은 자리 ${E(Math.max(0, s.capacity_left))}명</span>` +
          `<input type="number" class="occn" min="1" max="50" value="1" aria-label="${E(s.name)} 인원" data-occ-n="${E(s.id)}">명 ` +
          `<button type="button" data-occ="${E(s.id)}" data-dir="1">입소</button><button type="button" class="quiet" data-occ="${E(s.id)}" data-dir="-1">퇴소</button></div>`;
      }).join('');
      $('shelterOps').querySelectorAll('button[data-occ]').forEach(b => b.addEventListener('click', async () => {
        const n = Number($('shelterOps').querySelector(`input[data-occ-n="${CSS.escape(b.dataset.occ)}"]`).value);
        if (!Number.isInteger(n) || n < 1 || n > 50) { alert('인원은 1~50명 사이 숫자로 적어 주세요.'); return; }
        b.disabled = true;
        try { await post(`/api/shelters/${encodeURIComponent(b.dataset.occ)}/occupancy`, {delta: n * Number(b.dataset.dir)}); loadShelterOps(); loadDispatch(); }
        catch (err) { alert(err.message); b.disabled = false; }
      }));
    } catch (e) { showError('shelterOps', e); }
  }

  // 승인 대기열 개수 배지 + 처리 후 관련 탭 새로 고침
  const baseLoadApprovals = loadApprovals;
  loadApprovals = async function () {
    await baseLoadApprovals();
    await addDispatchControls();
    try {   // 첫 응답 전에는 숨겨 둔다(0이 잠깐 보였다가 바뀌지 않게)
      $('approvalCount').textContent = (await api('/api/approvals')).items.length;
      $('approvalCount').hidden = false;
    } catch (e) { /* 배지만 */ }
    if (current !== 'approvals' && LOADERS[current] && current !== 'dispatch') LOADERS[current]();
  };

  async function loadMeta() {
    if (META) return META;
    const r = await api('/api/recovery'), s = await api('/api/supply');
    META = {zones: r.zones, households: r.households, volunteers: s.volunteers, catalog: s.catalog};
    ZDESC = Object.fromEntries(META.zones.map(z => [z.id, (String(z.name).match(/\(([^)]+)\)/) || [, z.name])[1]]));
    VNAME = Object.fromEntries(META.volunteers.map(v => [v.id, v.name || v.id]));
    const zoneOpts = options(META.zones, z => place(z.id), z => z.id);
    document.querySelectorAll('select.zoneSel').forEach(sel => { sel.innerHTML = zoneOpts; });
    $('ckTarget').innerHTML = '<option value="all">동의한 모든 가구</option>' + options(META.zones, z => `${place(z.id)} 가구만`, z => z.id);
    const hhOpts = options(META.households, h => `${h.id} · ${h.address || place(h.zone)} · ${h.floor}${h.factors.length ? ' · ' + h.factors.join('·') : ''}`, h => h.id);
    ['sHousehold', 'caseHousehold', 'rpHousehold'].forEach(id => { $(id).innerHTML = hhOpts; });
    $('vHousehold').innerHTML = '<option value="">선택 안 함</option>' + hhOpts;
    const items = Object.keys(META.catalog);
    $('sItem').innerHTML = '<option value="">(메모에서 찾기)</option>' + options(items, i => `${i} (${META.catalog[i].category})`, i => i);
    $('dnItem').innerHTML = options([...items].sort((a, b) => a.localeCompare(b, 'ko')), i => `${i} (${META.catalog[i].category})`, i => i);   // 기부 품목은 가나다순
    const volOpts = options(META.volunteers, v => `${v.name || v.id} · ${v.address || place(v.zone)} · ${v.mode}`, v => v.id);
    $('dnDonor').innerHTML = volOpts; $('sosVol').innerHTML = volOpts;
    // 피해 신고서의 위치는 고른 가구의 위치로 채운다(같은 내용을 두 번 입력하지 않게)
    const fillRpPlace = () => { const h = META.households.find(x => x.id === $('rpHousehold').value); if (h) $('rpZone').value = h.address || place(h.zone); };
    $('rpHousehold').addEventListener('change', fillRpPlace); fillRpPlace();
    const syncUnits = () => { $('dnUnit').innerHTML = options(META.catalog[$('dnItem').value]?.units || []); };
    $('dnItem').addEventListener('change', syncUnits); syncUnits();
    return META;
  }

  // ---------------------------------------------------------------- 상황판
  // 화면 맨 위 위기 단계 띠: 담당자가 확정한 단계만 색으로 보여 준다(센서·신고만으로는 바뀌지 않음)
  function setBand(stage) {
    const level = stage.confirmed && ['주의', '경계', '위험'].includes(stage.level) ? stage.level : '정상';
    $('band').dataset.level = level;
    $('bandLevel').textContent = level;
    $('bandMeta').textContent = stage.confirmed
      ? `${(stage.hazards || []).join(', ') || '위험 유형 미상'} · ${fmtTime(stage.at)} 담당자 확정`
      : '확정된 위험 단계가 없습니다';
  }
  async function refreshBand() {
    try { setBand(await api('/api/stage')); } catch (e) { /* 띠만 */ }
    try { showWeather(await api('/api/weather')); } catch (e) { /* 날씨만 */ }
  }
  // 기상청 실제 날씨(참고 정보). 키가 없거나 인터넷이 끊기면 '확인 불가'로만 표시한다
  function showWeather(w) {
    const warn = (w.warnings || []).length;
    $('bandWx').innerHTML = !w.available ? '실제 날씨 확인 불가' :
      `거제 ${w.rain ? `1시간 강수 ${E(w.rain.mm_per_h)}mm(${E(w.rain.label)})` : '강수 확인 불가'}` +
      (warn ? `<span class="wxwarn">특보 ${warn}건</span>` : ' · 특보 없음');
    $('wxBox').classList.toggle('muted', !w.available);
    $('wxBox').innerHTML = !w.available ? E(w.reason) :
      `<div>${w.rain ? `<b>1시간 강수량 ${E(w.rain.mm_per_h)}mm</b> · ${E(w.rain.label)} → 강우 단계 ${E(w.rain.stage)} · 기온 ${E(w.rain.temp_c)}℃ <span class="muted">(${E(w.rain.at)} 관측)</span>` : E(w.rain_error)}</div>` +
      `<div>감시 에이전트가 지금 쓰는 강우 단계: <b>${E(w.applied_rain_stage)}</b></div>` +
      `<div style="margin-top:6px">${w.warnings === null ? E(w.warnings_error) : warn ? '발효 중인 특보(거제·경남 관련): ' + w.warnings.map(E).join(' / ') : '거제·경남에 발효 중인 특보 없음'}</div>` +
      `<div class="muted">출처: ${E(w.source)}</div>`;
  }

  // 공지: 담당자가 쓴 글을 주민·봉사자 화면 맨 위에 바로 띄운다(내리면 사라짐)
  async function loadAnnouncements() {
    try {
      await loadMeta();   // 구역 고르기 목록
      const d = await api('/api/announcements');
      const hide = $('annHideEnded').checked, rows = hide ? d.items.filter(a => a.active) : d.items, ended = d.items.length - d.items.filter(a => a.active).length;
      $('annList').classList.toggle('muted', !rows.length);
      $('annList').innerHTML = (rows.length ? table(['올린 때', '대상', '받는 곳', '내용', ''], rows.map(a =>
        `<tr${a.active ? '' : ' class="muted"'}><td>${E(fmtDate(a.at))}</td><td>${E(d.audiences[a.audience] || a.audience)}</td><td>${E(a.zone === 'all' ? '전체' : place(a.zone))}</td>` +
        `<td>${a.urgent ? chip('긴급', 'bad') + ' ' : ''}${E(a.text)}<div class="muted">${E(a.by)} 올림${a.active ? '' : ` · ${E(a.ended_by || '')} 내림`}</div></td>` +
        `<td>${a.active ? `<button type="button" class="quiet" data-ann-end="${E(a.id)}">내리기</button>` : '내림' + (a.ended_at ? '<div class="muted">' + E(fmtDate(a.ended_at)) + '</div>' : '')}</td></tr>`))
        : (hide && ended ? '게시 중인 공지가 없습니다.' : '아직 올린 공지가 없습니다.')) + (hide && ended ? `<p class="muted">내린 공지 ${ended}건은 숨겼습니다.</p>` : '');
    } catch (e) { showError('annList', e); }
  }

  async function loadSituation() {
    try {
      const [stage, checks, vol, sup] = await Promise.all([api('/api/stage'), api('/api/welfare/checks'), api('/api/volunteer'), api('/api/supply')]);
      const sos = vol.sos.map(s => `<div class="incident err"><b>SOS</b> · 봉사자 ${E(s.name || vn(s.volunteer))}${s.task ? ' · 작업 ' + E(s.task) : ''} · ${E(fmtTime(s.ts))} <button data-ack="${E(s.id)}" class="red">확인</button></div>`).join('');
      const esc = checks.checks.filter(c => c.state === '격상');
      const short = sup.forecast.filter(f => f.shortage);
      const waiting = sup.requests.filter(r => r.state === '검토').length;
      setBand(stage);
      $('situation').innerHTML = sos + '<dl class="sit">' +
        `<div><dt>위기 단계</dt><dd>${E(stage.confirmed ? stage.level : '정상')}<small>${stage.confirmed ? '담당자 확정' + (stage.hazards?.length ? ' · ' + E(stage.hazards.join(', ')) : '') : '확정된 단계 없음'}</small></dd></div>` +
        `<div><dt>안부 확인</dt><dd>격상 ${esc.length}가구<small>${esc.length ? E(esc.map(c => c.household).join(', ')) + ' · ' : ''}응답 대기 ${checks.counts['대기'] || 0}가구</small></dd></div>` +
        `<div><dt>물품</dt><dd>검토 대기 ${waiting}건<small>부족 예상 ${short.length ? E(short.map(f => f.item).join(', ')) : '없음'}</small></dd></div>` +
        `<div><dt>봉사</dt><dd>진행 중 ${vol.tasks.filter(t => t.state === '진행' || t.state === '투입승인').length}건<small>투입 중 봉사자 ${vol.volunteers.filter(v => v.busy).length}명</small></dd></div></dl>`;
      $('situation').querySelectorAll('button[data-ack]').forEach(b => b.addEventListener('click', async () => {
        try { await post(`/api/volunteer/sos/${encodeURIComponent(b.dataset.ack)}/ack`); loadSituation(); } catch (e) { alert(e.message); }
      }));
    } catch (e) { showError('situation', e); }
  }

  // ---------------------------------------------------------------- ⑧ 물품
  const REQ_ACTIONS = {
    검토: [['승인', 'review', {decision: '승인'}], ['보류', 'review', {decision: '보류'}, true], ['반려', 'review', {decision: '반려'}, true]],
    보류: [['재검토', 'review', {decision: '재검토'}]],
    승인: [['공급원 추천', 'propose', {}]],
    배치: [['전달 출발', 'deliver', {}]],
    이동: [['받았음', 'receipt', {received: true}], ['못 받았음', 'receipt', {received: false}]],
  };
  async function loadSupply() {
    try {
      await loadMeta();
      const d = await api('/api/supply');
      $('supplyRequests').innerHTML = table(['요청', '가구·위치', '품목·수량', '긴급도', '표시', '상태', '처리'], d.requests.map(r => {
        const acts = r.pending_approval ? `<span class="muted">배치 승인 대기 ${E(r.pending_approval)}</span>`
          : (REQ_ACTIONS[r.state] || []).map(([label, act, body, needReason]) =>
            `<button data-req="${E(r.id)}" data-act="${E(act)}" data-body='${E(JSON.stringify(body))}' ${needReason ? 'data-reason="1"' : ''} class="${label.includes('반려') || label.includes('못') ? 'gray' : ''}">${E(label)}</button>`).join('');
        const marks = [...r.flags.map(f => chip(f, 'warn')), ...r.urgency_reasons.map(u => chip(u)),
          r.route === 'manual_medicine' ? chip('의약품: 직접 처리', 'bad') : '', r.qty_unconfirmed ? chip('수량 확인 필요', 'warn') : '',
          r.merged_count > 1 ? chip(`요청 ${r.merged_count}건 병합`) : '',
          r.last_proposal?.procurement_needed ? chip(`부족 ${r.last_proposal.shortfall} · 외부 조달 필요`, 'bad') : ''].join('');
        return `<tr><td>${E(r.id)}</td><td>${r.household ? E(r.household) : '신고 ' + E(r.incident_id)} · ${E(hhPlace(r.household) || place(r.zone))}${r.review_reason ? ' ' + chip(r.review_reason, 'warn') : ''}</td><td>${E(r.item)} ${E(r.qty)}</td><td>${E(r.urgency)}</td><td>${marks}</td><td>${chip(r.state, STATE_CLS[r.state] || '')}</td><td>${acts}</td></tr>`;
      }));
      $('supplyRequests').querySelectorAll('button[data-req]').forEach(b => b.addEventListener('click', async () => {
        const body = JSON.parse(b.dataset.body);
        if (b.dataset.reason) { const r = prompt('사유를 입력하세요'); if (!r) return; body.reason = r; }
        b.disabled = true;
        try { await post(`/api/supply/requests/${encodeURIComponent(b.dataset.req)}/${b.dataset.act}`, body); await loadApprovals(); await loadSupply(); }
        catch (e) { alert(e.message); b.disabled = false; }
      }));
      const n = d.notice;
      $('supplyForecast').innerHTML = table(['품목', '가용 수량', '시간당 소비', '소진 예상', '근거'], d.forecast.map(f =>
        `<tr><td>${E(f.item)}</td><td>${E(f.qty)}</td><td>${E(f.rate_per_h)}</td><td>${f.hours_left === Infinity ? '-' : f.hours_left < 1 ? chip('지금 부족', 'bad') : E(f.hours_left) + '시간 뒤'} ${f.shortage && f.hours_left >= 1 ? chip('부족 예상', 'bad') : ''}</td><td>${E(f.rate_source)}</td></tr>`)) +
        (n.draft ? `<p><b>필요 물품 안내 초안</b> · 검증: ${chip(n.verification.verdict, n.verification.displayable ? 'ok' : 'warn')} ${n.verification.displayable ? '' : '<span class="muted">검증 통과 전이라 봉사자 화면에 내보내지 않습니다.</span>'}</p><div class="result">${E(n.draft)}</div>` : '<p class="muted">부족 예상 품목이 없어 안내 초안을 만들지 않았습니다.</p>');
      $('supplyStock').innerHTML = table(['ID', '장소', '품목', '수량', '예약', '유통기한'], d.stock.map(s =>
        `<tr><td>${E(s.id)}</td><td>${E(s.location_name)}</td><td>${E(s.item)} ${s.category === '의약품' ? chip('자동 배치 금지', 'bad') : ''}</td><td>${E(s.qty)}</td><td>${E(s.reserved || 0)}</td><td>${s.days_left == null ? '-' : s.days_left < 0 ? chip('기한 경과', 'bad') : E(s.days_left) + '일'}</td></tr>`));
      $('supplyDonations').innerHTML = table(['ID', '기부자', '품목', '수량', '상태', '처리'], d.donations.map(x =>
        `<tr><td>${E(x.id)}</td><td>${E(x.donor)}</td><td>${E(x.item)}</td><td>${E(x.qty)} ${E(x.unit || '')}</td><td>${chip(x.state, x.state === '접수 가능' ? 'ok' : x.state === '반려' ? 'bad' : 'warn')}</td><td>${x.state === '검수 대기' ? `<button data-don="${E(x.id)}" data-ok="1">접수 가능</button><button data-don="${E(x.id)}" class="gray">반려</button>` : ''}</td></tr>`));
      $('supplyDonations').querySelectorAll('button[data-don]').forEach(b => b.addEventListener('click', async () => {
        b.disabled = true;
        try { await post(`/api/supply/donations/${encodeURIComponent(b.dataset.don)}/inspect`, {accept: !!b.dataset.ok}); await loadSupply(); }
        catch (e) { alert(e.message); b.disabled = false; }
      }));
    } catch (e) { showError('supplyRequests', e); }
  }

  // ---------------------------------------------------------------- ⑥ 봉사
  const TASK_ACTIONS = {대기: [['다시 추천', 'recommend']], 투입승인: [['도착 체크인', 'checkin']], 진행: [['완료 보고', 'complete']]};
  async function loadVolunteer() {
    try {
      await loadMeta();
      const d = await api('/api/volunteer');
      const pend = Object.entries(d.safety).filter(([, v]) => v == null).length;
      $('volSafety').textContent = pend ? `안전 기준(연속 활동 시간·고온 등) ${pend}개가 공식 지침 확인 전이라 '기준 미확정'으로 두고 적용하지 않습니다.` : '';
      $('volTasks').innerHTML = table(['작업', '작업·위치', '필요', '추천·투입 인원', '상태', '처리'], [...d.tasks].reverse().map(t => {
        const rec = t.last_recommendation || {};
        const team = (t.team || rec.team || []).map(vn).join(', ') || '-';
        const acts = t.pending_approval ? `<span class="muted">투입 승인 대기 ${E(t.pending_approval)}</span>`
          : (TASK_ACTIONS[t.state] || []).map(([label, act]) => `<button data-task="${E(t.id)}" data-act="${act}">${E(label)}</button>`).join('');
        return `<tr><td>${E(t.id)}</td><td>${E(t.type)} · ${E(t.address || place(t.zone))}${t.household ? ' · ' + E(t.household) : ''}</td><td>${E(t.people)}명 · ${E(t.hours)}시간 · ${E((t.equipment || []).join(', ') || '장비 없음')}</td><td>${E(team)}${rec.shortfall ? ' ' + chip('부족 ' + rec.shortfall + '명', 'warn') : ''}</td><td>${chip(t.state, STATE_CLS[t.state] || '')}</td><td>${acts}</td></tr>`;
      }));
      $('volTasks').querySelectorAll('button[data-task]').forEach(b => b.addEventListener('click', async () => {
        b.disabled = true;
        try {
          const r = await post(`/api/volunteer/tasks/${encodeURIComponent(b.dataset.task)}/${b.dataset.act}`);
          if (r.case_update) alert(`복구 케이스 ${r.case_update.case}에 반영했습니다. 남은 작업: ${r.case_update.remaining.join(', ') || '없음'}`);
          await loadApprovals(); await loadVolunteer();
        } catch (e) { alert(e.message); b.disabled = false; }
      }));
      $('volSos').innerHTML = d.sos.map(s => `<div class="incident err"><b>SOS</b> · ${E(s.name || vn(s.volunteer))}${s.task ? ' · ' + E(s.task) : ''} · ${E(fmtTime(s.ts))} <button data-ack="${E(s.id)}" class="red">확인</button></div>`).join('');
      $('volSos').querySelectorAll('button[data-ack]').forEach(b => b.addEventListener('click', async () => {
        try { await post(`/api/volunteer/sos/${encodeURIComponent(b.dataset.ack)}/ack`); loadVolunteer(); } catch (e) { alert(e.message); }
      }));
      $('volList').innerHTML = table(['봉사자', '활동할 곳', '역량', '장비', '가능 시간', '참여 방식', '상태'], d.volunteers.map(v =>
        `<tr><td>${E(v.name || v.id)}</td><td>${E(v.address || place(v.zone))}</td><td>${E(v.skills.join(', '))}</td><td>${E(v.equipment.join(', ') || '-')}</td><td>${E(v.available_min)}분</td><td>${E(v.mode)}</td><td>${v.busy ? chip('투입 중', 'warn') : chip('대기', 'ok')}</td></tr>`));
    } catch (e) { showError('volTasks', e); }
  }

  // ---------------------------------------------------------------- ⑤ 복구·지원
  async function loadRecovery() {
    try {
      await loadMeta();
      const d = await api('/api/recovery');
      if (!d.doc_parser) $('docParserNote').textContent = '문서 파싱 API가 연결되지 않아(mock 모드) 지금은 텍스트(.txt) 파일만 읽을 수 있습니다. PDF·사진은 LLM_MODE=solar로 실행하세요.';
      $('caseList').innerHTML = table(['케이스', '가구·위치', '현재 단계', '이력', '봉사 작업', '처리'], d.cases.map(c => {
        const idx = d.stages.indexOf(c.stage), next = d.stages[idx + 1];
        const tasks = Object.entries(c.tasks || {}).map(([t, s]) => chip(`${t} ${s}`, s === '완료' ? 'ok' : '')).join('') || '-';
        return `<tr><td>${E(c.id)}</td><td>${E(c.household)} · ${E(hhPlace(c.household) || place(c.zone))}</td><td>${d.stages.map((s, i) => chip(s, i < idx ? 'ok' : i === idx ? 'warn' : '')).join('')}</td><td>${E(c.timeline.map(t => t.stage + (t.by ? '(' + t.by + ')' : '')).join(' → '))}</td><td>${tasks}</td><td>${next ? `<button data-case="${E(c.id)}" data-to="${E(next)}">${E(next)} 단계로</button>` : chip('완료', 'ok')}</td></tr>`;
      }));
      $('caseList').querySelectorAll('button[data-case]').forEach(b => b.addEventListener('click', async () => {
        b.disabled = true;
        try { await post(`/api/recovery/cases/${encodeURIComponent(b.dataset.case)}/advance`, {to: b.dataset.to}); loadRecovery(); } catch (e) { alert(e.message); b.disabled = false; }
      }));
      const tot = Object.fromEntries(d.damage_types.map(t => [t, Object.values(d.aggregate.by_zone).reduce((n, by) => n + (by[t] || 0), 0)]));
      $('surveyTable').innerHTML = `<p>입력 ${E(d.aggregate.total)}건 · ${d.damage_types.map(t => `${E(t)} ${E(tot[t])}건`).join(' · ')}</p>` +
        table(['입력 시각', '피해 위치', '피해 유형'], (d.surveys || []).map(x => `<tr><td>${E(fmtTime(x.ts))}</td><td>${E(x.address || place(x.zone))}</td><td>${E(x.damage_type)}</td></tr>`));
    } catch (e) { showError('caseList', e); }
  }

  // ---------------------------------------------------------------- ③ 안부
  async function loadChecks() {
    try {
      await loadMeta();
      const d = await api('/api/welfare/checks');
      const hh = META.households, target = $('ckTarget').value, inTarget = h => target === 'all' || h.zone === target;
      const ok = hh.filter(h => h.consent && inTarget(h)), vul = ok.filter(h => (h.factors || []).length);
      $('ckSummary').innerHTML = `<b>안부 확인을 보낼 가구: ${ok.length}곳</b> (${E(target === 'all' ? '전체' : place(target))} · 안부 확인에 동의한 가구) · 취약 가구 ${vul.length}곳은 대기 시간의 절반이 지나면 먼저 격상됩니다.` +
        `<br><span class="muted">주민이 직접 등록한 집 ${hh.filter(h => h.registered).length}곳 · 시연용 가구 ${hh.filter(h => !h.registered).length}곳 · 텔레그램과 연결한 주민은 휴대폰에서 '안전합니다' 버튼으로 답합니다. 자동 출동은 없습니다.</span>`;
      $('ckHhSub').textContent = `${hh.length}곳`;
      $('ckHhList').innerHTML = table(['가구', '위치', '집 형태', '취약 요인', '안부 동의'], hh.map(h =>
        `<tr><td>${E(h.id)}${h.registered ? ' ' + chip('주민 등록') : ''}</td><td>${E(h.address || place(h.zone))}</td><td>${E(h.floor || '-')}</td><td>${E((h.factors || []).join(', ') || '-')}</td><td>${h.consent ? chip('동의', 'ok') : chip('안 함')}</td></tr>`));
      const c = d.counts || {};
      $('ckCounts').innerHTML = d.checks.length ? [['격상', 'bad'], ['대기', 'warn'], ['지연 응답', 'warn'], ['안전', 'ok']].map(([k, cls]) => chip(`${k} ${c[k] || 0}`, cls)).join('') : '';
      if (!d.checks.length) $('checkList').innerHTML = '<p class="muted">아직 안부 확인을 시작하지 않았습니다. 위에서 대상과 시간을 고르고 시작하세요.</p>';
      if (d.escalated_now.length) $('checkResult').innerHTML = `<span class="err">방금 격상: ${E(d.escalated_now.join(', '))} (담당자 확인 필요, 자동 출동 없음)</span>`;
      if (d.checks.length) $('checkList').innerHTML = table(['가구', '위치', '취약 요인', '상태', '남은 시간', '처리'], d.checks.map(c =>
        `<tr><td>${E(c.household)}</td><td>${E(hhPlace(c.household) || place(c.zone))}</td><td>${E(c.factors.join(', ') || '-')}</td><td>${chip(c.state, STATE_CLS[c.state] || '')}</td><td>${c.seconds_left != null ? E(Math.max(0, c.seconds_left)) + '초' : '-'}</td><td>${['대기', '격상'].includes(c.state) ? `<button data-hh="${E(c.household)}">안전 확인 기록</button>` : ''}</td></tr>`));
      $('checkList').querySelectorAll('button[data-hh]').forEach(b => b.addEventListener('click', async () => {
        b.disabled = true;
        try { await post(`/api/welfare/checks/${encodeURIComponent(b.dataset.hh)}/respond`); loadChecks(); } catch (e) { alert(e.message); b.disabled = false; }
      }));
    } catch (e) { showError('checkList', e); }
  }

  // ---------------------------------------------------------------- ④ 통제 구간·배치 수정
  async function loadControl() {
    try {
      await loadMeta();
      const d = await api('/api/dispatch');
      $('controlList').innerHTML = table(['ID', '막힌 길', '막히는 곳', '상태', '처리'], d.control_zones.map(c =>
        `<tr><td>${E(c.id)}</td><td>${E(c.name)}</td><td>${E(c.between.map(place).join(' ↔ '))}</td><td>${c.active ? chip('통제 중', 'bad') : chip('해제', 'ok')}</td><td><button data-cz="${E(c.id)}" data-on="${c.active ? '' : '1'}" class="${c.active ? 'gray' : 'red'}">${c.active ? '통제 해제' : '통제 시작'}</button></td></tr>`));
      $('controlList').querySelectorAll('button[data-cz]').forEach(b => b.addEventListener('click', async () => {
        b.disabled = true;
        try { const r = await post(`/api/control-zones/${encodeURIComponent(b.dataset.cz)}/toggle`, {active: !!b.dataset.on}); reportAffected(r); loadControl(); loadDispatch(); }
        catch (e) { alert(e.message); b.disabled = false; }
      }));
    } catch (e) { showError('controlList', e); }
  }
  function reportAffected(r) {
    $('controlResult').innerHTML = r.affected.length
      ? `<span class="err">경로가 막힌 투입 중 배치: ${E(r.affected.map(a => `${a.incident_id}(${a.resource})`).join(', '))} · 배치를 해제하고 다시 추천하세요.</span>`
      : `${E(r.name)} ${r.active ? '통제를 시작했습니다' : '통제를 해제했습니다'}. 영향받는 투입 중 배치는 없습니다.`;
  }
  // 배치 승인 카드에 '다른 후보로 수정'·'직접 지정' 추가(제안서 5.7 담당자 결정)
  async function addDispatchControls() {
    let items, d;
    try { [items, d] = await Promise.all([api('/api/approvals'), api('/api/dispatch')]); } catch (e) { return; }
    for (const a of items.items.filter(x => x.kind.startsWith('dispatch_'))) {
      const card = document.querySelector(`#approvals .apcard[data-ap="${CSS.escape(a.id)}"] .apmore`);   // '자세히 · 다른 후보' 안에
      if (!card || card.querySelector('.dctl')) continue;
      const rec = a.item.recommendation, pool = [rec.top, ...(rec.alternatives || [])];
      const all = a.kind === 'dispatch_public'
        ? d.public.map(p => [p.id, `${p.id} ${p.name}${p.available ? '' : ' (투입 중)'}`])
        : d.shelters.map(s => [s.id, `${s.id} ${s.name} (남은 ${s.capacity_left})`]);
      card.insertAdjacentHTML('beforeend', `<div class="dctl" style="margin-top:8px;border-top:1px dashed #cbd3df;padding-top:8px">
        <label>다른 후보로 수정</label><select data-mod-sel>${pool.slice(1).map(r => `<option value="${E(r.id)}">${E(r.id)} ${E(r.name || '')} · 점수 ${E(r.score.toFixed(2))}</option>`).join('')}</select><button type="button" data-mod>선택 후보로 승인</button>
        <label>직접 지정</label><select data-dir-sel>${all.map(([id, label]) => `<option value="${E(id)}">${E(label)}</option>`).join('')}</select>
        <label style="font-weight:400"><input type="checkbox" data-override style="width:auto"> 제약 위반이어도 예외 승인(사유 필수)</label><button type="button" data-dir class="gray">직접 지정으로 승인</button></div>`);
      const reason = () => $('reason-' + a.id).value;
      const send = async (action, body) => {
        if (!reason().trim()) { alert('처리 사유를 입력해 주세요.'); return; }
        try { const r = await post(`/api/dispatch/${encodeURIComponent(a.id)}/${action}`, {...body, reason: reason()});
              if (r.warnings?.length) alert('예외 승인으로 기록했습니다: ' + r.warnings.join(', '));
              await loadApprovals(); loadDispatch(); }
        catch (e) { alert(e.message); }
      };
      const modSel = card.querySelector('[data-mod-sel]');
      if (!modSel.options.length) card.querySelector('[data-mod]').disabled = true;
      card.querySelector('[data-mod]').addEventListener('click', () => send('modify', {candidate: modSel.value}));
      card.querySelector('[data-dir]').addEventListener('click', () => send('direct', {candidate: card.querySelector('[data-dir-sel]').value,
                                                                                       override: card.querySelector('[data-override]').checked}));
    }
  }

  // ---------------------------------------------------------------- ⑦ 검증
  // 노드가 보내는 센서 이름 → 화면 이름·단위(모르는 이름은 그대로 보여 준다)
  const EXT_LABEL = {temp: ['온도', '℃'], temperature: ['온도', '℃'], t: ['온도', '℃'], hum: ['습도', '%'], humidity: ['습도', '%'], h: ['습도', '%'],
    gas: ['가스', ''], co2: ['이산화탄소', 'ppm'], co: ['일산화탄소', 'ppm'], smoke: ['연기', ''], flame: ['불꽃', ''], dust: ['미세먼지', '㎍/㎥'],
    pm25: ['초미세먼지', '㎍/㎥'], pm10: ['미세먼지', '㎍/㎥'], light: ['밝기', ''], lux: ['밝기', 'lx'], water: ['수위', ''], level: ['수위', ''],
    dist: ['거리', 'cm'], distance: ['거리', 'cm'], rain: ['강우', ''], wind: ['풍속', 'm/s'], pressure: ['기압', 'hPa'], press: ['기압', 'hPa'],
    sound: ['소리', ''], vibration: ['진동', ''], tilt: ['기울기', ''], soil: ['토양 수분', ''], battery: ['배터리', '%']};
  const extLabel = s => EXT_LABEL[String(s).toLowerCase()] || [s, ''];

  async function loadSensorLive() {
    try {
      const d = await api('/api/sensor/status');
      if (!d.gateway_enabled) {
        $('sensorLive').innerHTML = d.cloud
          ? '이 배포 사이트에는 마이크로비트 센서를 연결하지 않았습니다. 센서는 노트북 USB의 수신기(G)로 노트북 서버에만 들어오므로, 센서 시연은 노트북에서 실행한 서버(http://127.0.0.1:8765/admin)로 하세요. 이 사이트의 신고 대조는 \'확인 불가\'로 표시됩니다.'
          : '게이트웨이가 꺼져 있습니다. 서버를 SENSOR_GATEWAY_TOKEN 환경변수와 함께 실행하고 <code>python gateway/microbit_gateway.py</code>를 켜세요(docs/마이크로비트_게이트웨이.md).';
        return;
      }
      const age = s => s == null ? '-' : `${s}초 전`;
      const ev = d.last_eval, o = ev && ev.output;
      if (ev && ev.approval_id && ev.approval_id !== lastSensorApproval) { lastSensorApproval = ev.approval_id; loadApprovals(); }   // 대기열 숫자 갱신
      const via = d.link === '블루투스' ? '🔵 외부 블루투스 모듈' : d.link === 'USB' ? 'USB 케이블' : d.link === '가상' ? '가상 센서(시험)' : null;
      $('sensorLive').innerHTML =
        (via ? `<div class="sensor-path">경로: 노드 A·B(·C·D·E) ─무선─▶ <b>G 마이크로비트</b> ─${E(via)}─▶ 노트북 게이트웨이 ─▶ 서버 ${chip('G 연결: ' + via, d.last_batch_age_s != null && d.last_batch_age_s <= 30 ? 'ok' : 'bad')}</div>` : '') +
        `<div>노드: ${d.nodes.map(n => chip(`${n.node} ${n.online ? '연결' : '끊김'} · ${age(n.last_seen_age_s)}`, n.online ? 'ok' : 'bad')).join(' ')}` +
        ` · 받은 값 ${E(d.received)}개${d.rejected ? ` · 버린 값 ${E(d.rejected)}개` : ''} · 강우 단계 ${E(d.rain_stage)}</div>` +
        table(['센서', '최근 값', '임계값', '받은 때'], d.sensors.map(s =>
          `<tr><td>${E(s.key)}</td><td>${s.value == null ? '-' : chip(s.value, s.over ? 'bad' : 'ok')}</td><td>${E(s.threshold)}</td><td>${E(age(s.age_s))}</td></tr>`)) +
        (o ? `<div>최근 판정: ${chip(o.status, o.status === '확정후보' ? 'bad' : o.status === '보류' ? 'warn' : 'ok')} 제안 단계 <b>${E(o.level)}</b> · ${E(o.summary)}` +
             (ev.approval_id ? ` · 승인 대기열에 올림(${E(ev.approval_id)})` : ev.duplicate_of ? ' · 같은 단계가 이미 대기·확정 중' : '') + '</div>' +
             (o.false_alarm_log.length ? `<div class="muted">${E(o.false_alarm_log.join(' / '))}</div>` : '')
           : '<div class="muted">아직 받은 값이 없습니다. 게이트웨이를 실행하세요.</div>') +
        (d.calibrated ? '' : '<div class="muted">임계값 보정 전(임시값)</div>') +
        // C·D·E 노드의 온도·조도처럼 감시 설정에 없는 센서 값. 판정에는 쓰지 않고 표시만(값이 있을 때만 보인다)
        ((d.extra || []).length ? `<h3 style="margin:14px 0 4px">그 밖의 노드 센서 값 ${chip(`${d.extra.length}개`, 'ok')}</h3>` : '') +
        ((d.extra || []).length
          ? table(['노드', '센서', '최근 값', '받은 때', '상태'], d.extra.map(x => {
              const [name, unit] = extLabel(x.sensor);
              return `<tr><td>${E(x.node)}</td><td>${E(name)}${name !== x.sensor ? ` <span class="muted">(${E(x.sensor)})</span>` : ''}</td>` +
                `<td><b>${E(x.value)}</b>${unit ? ' ' + E(unit) : ''}</td><td>${E(age(x.age_s))}</td><td>${chip(x.online ? '수신 중' : '끊김', x.online ? 'ok' : 'bad')}</td></tr>`;
            })) + '<div class="muted">감시 설정(임계값)에 없는 센서라 위기 단계 판정에는 쓰지 않고 표시만 합니다.</div>'
          : '') +
        `<h3 style="margin:14px 0 4px">현장 버튼 신호${d.open_urgent ? ' ' + chip(`확인 안 한 급한 신호 ${d.open_urgent}건`, 'bad') : ''}</h3>` +
        table(['받은 때', '노드', '신호', '웹 반영', ''], d.events.map(x =>
          `<tr><td>${E(fmtTime(x.ts))}</td><td>${E(x.node)}</td><td>${x.urgent ? chip(x.label, x.open ? 'bad' : '') : E(x.label)}</td>` +
          `<td>${x.applied == null ? '<span class="muted">-</span>' : x.applied ? chip('반영', 'ok') + ' ' + E(x.note) : chip('반영 안 됨', 'warn') + ' ' + E(x.note)}</td>` +
          `<td>${x.open ? `<button type="button" data-ack-event="${E(x.id)}">확인</button>` : x.acked_by ? `<span class="muted">${E(x.acked_by)} 확인</span>` : ''}</td></tr>`));
    } catch (e) { showError('sensorLive', e); }
  }

  async function loadVerifyDocs() {
    if ($('vDocs').querySelector('input')) return;
    try {
      const d = await api('/api/verify/docs');
      $('vDocs').insertAdjacentHTML('beforeend', d.docs.map(doc => `<label title="${E(doc.text)}"><input type="checkbox" name="vDoc" value="${E(doc.id)}"> ${E(doc.id)} ${E(doc.text.slice(0, 24))}…</label>`).join('') +
        `<div class="muted" style="width:100%">Groundedness 모델: ${d.model_connected ? '연결됨' : '연결 안 됨(숫자 검사 뒤 담당자 확인으로 처리)'}</div>`);
    } catch (e) { showError('verifyResult', e); }
  }

  // ---------------------------------------------------------------- 폼 연결
  function bind() {
    document.querySelectorAll('#tabs button').forEach(b => b.addEventListener('click', () => go(b.dataset.go)));
    $('annForm').addEventListener('submit', async e => {   // 공지 올리기
      e.preventDefault();
      const b = e.submitter; if (b) b.disabled = true;
      try {
        await post('/api/announcements', {text: $('annText').value, audience: $('annAudience').value, zone: 'all', urgent: $('annUrgent').value === '1'});
        $('annText').value = ''; show('annResult', '공지를 올렸습니다. 주민·봉사자 화면에 15초 안에 나타납니다.'); loadAnnouncements();
      } catch (err) { fail('annResult', err); } finally { if (b) b.disabled = false; }
    });
    $('annList').addEventListener('click', async e => {   // 공지 내리기
      const b = e.target.closest('button[data-ann-end]');
      if (!b || !confirm('이 공지를 내릴까요? 주민·봉사자 화면에서 사라집니다.')) return;
      b.disabled = true;
      try { await post(`/api/announcements/${encodeURIComponent(b.dataset.annEnd)}/end`); loadAnnouncements(); }
      catch (err) { alert(err.message); b.disabled = false; }
    });
    $('sensorLive').addEventListener('click', async e => {   // 현장 SOS 등 급한 신호 '확인'
      const b = e.target.closest('button[data-ack-event]');
      if (!b) return;
      b.disabled = true;
      try { await post(`/api/sensor/events/${encodeURIComponent(b.dataset.ackEvent)}/ack`); loadSensorLive(); }
      catch (err) { alert(err.message); b.disabled = false; }
    });
    $('incidents').addEventListener('click', async e => {   // 신고 카드 안 '분류 저장'(O3)
      const b = e.target.closest('button[data-classify]');
      if (!b) return;
      const box = b.closest('.fix'), v = f => box.querySelector(`[data-f="${f}"]`).value;
      b.disabled = true;
      try {
        await post(`/api/incidents/${encodeURIComponent(b.dataset.classify)}/classify`,
          {type: v('type'), zone: v('zone'), urgency: Number(v('urgency'))});
        await loadIncidents();
      } catch (err) { alert(err.message); b.disabled = false; }
    });
    $('applyRain').addEventListener('click', async () => {
      try { const r = await post('/api/weather/apply-rain'); alert(`강우 단계를 ${r.applied_rain_stage}(으)로 적용했습니다 · 실제 1시간 강수 ${r.rain.mm_per_h}mm`); refreshBand(); }
      catch (err) { alert(err.message); }
    });
    $('makeReport').addEventListener('click', async () => {
      try { const r = await api('/api/report/daily'); $('dailyReport').hidden = false; $('dailyReport').textContent = r.text; }
      catch (err) { showError('dailyReport', err); $('dailyReport').hidden = false; }
    });
    $('refreshSupply').addEventListener('click', loadSupply);
    $('refreshVol').addEventListener('click', loadVolunteer);
    $('controlForm').addEventListener('submit', async e => {
      e.preventDefault();
      try { const r = await post('/api/control-zones', {name: $('czName').value, between: [$('czA').value, $('czB').value]});
            reportAffected(r); $('czName').value = ''; loadControl(); loadDispatch(); }
      catch (err) { $('controlResult').innerHTML = '<span class="err">오류: ' + E(err.message) + '</span>'; }
    });
    $('gZonesFill').addEventListener('click', async () => {
      try { const d = await api('/api/dispatch'); $('gZones').value = d.control_zones.filter(c => c.active).map(c => c.name).join(', '); }
      catch (err) { alert(err.message); }
    });
    ['Wl', 'Wh', 'Vb'].forEach(k => $('s' + k).addEventListener('input', () => { $('o' + k).textContent = $('s' + k).value; }));

    $('supplyForm').addEventListener('submit', async e => {
      e.preventDefault();
      const body = {household: $('sHousehold').value, people: Number($('sPeople').value), when: $('sWhen').value};
      checked('sFlag').forEach(f => { body[f] = true; });
      if ($('sItem').value) { body.item = $('sItem').value; body.qty = Number($('sQty').value); } else body.memo = $('sMemo').value;
      try {
        const r = await post('/api/supply/requests', body);
        show('supplyFormResult', r.requests.map(x => `${E(x.id)} · ${E(x.item)} ${E(x.qty)} · 긴급도 ${E(x.urgency)} · ${E(x.state)}${x.merged_count > 1 ? ' · 기존 요청과 병합' : ''}${x.route === 'manual_medicine' ? ' · 의약품은 담당자 직접 처리' : ''}`).join('<br>') +
          (body.memo && !r.ai_used ? `<div class="muted">메모는 AI 없이 카탈로그 키워드로 해석했습니다(${E(r.fallback_reason || '규칙')})</div>` : ''));
        $('sMemo').value = ''; loadSupply();
      } catch (err) { fail('supplyFormResult', err); }
    });
    $('donationForm').addEventListener('submit', async e => {
      e.preventDefault();
      const days = $('dnDays').value.trim();
      const body = {donor: $('dnDonor').value, item: $('dnItem').value, qty: Number($('dnQty').value), unit: $('dnUnit').value,
                    condition: $('dnCond').value === '파손·오염' ? '파손' : $('dnCond').value, days_left: days === '' ? null : Number(days), address: $('dnAddress').value};
      try { const r = await post('/api/supply/donations', body); show('donationResult', `${E(r.id)} 등록 · ${E(r.state)} (담당자 검수 후 매칭 대상)`); loadSupply(); }
      catch (err) { fail('donationResult', err); }
    });
    $('volForm').addEventListener('submit', async e => {
      e.preventDefault();
      const body = {text: $('vText').value};
      if ($('vAddress').value.trim()) body.address = $('vAddress').value.trim();
      if ($('vHousehold').value) body.household = $('vHousehold').value;
      if ($('vTemp').value !== '') body.temperature_c = Number($('vTemp').value);
      try {
        const r = await post('/api/volunteer/requests', body);
        show('volResult', r.note ? E(r.note) : r.tasks.map(t => `${E(t.id)} · ${E(t.type)} · ${t.pending_approval ? '투입 승인 대기열에 올림(' + E(t.pending_approval) + ')' : '추천 가능한 봉사자 없음 → 대기열'}${t.last_recommendation?.shortfall ? ' · 부족 ' + E(t.last_recommendation.shortfall) + '명' : ''}`).join('<br>'));
        await loadApprovals(); loadVolunteer();
      } catch (err) { fail('volResult', err); }
    });
    $('sosForm').addEventListener('submit', async e => {
      e.preventDefault();
      try { await post('/api/volunteer/sos', {volunteer: $('sosVol').value, task: $('sosTask').value || null}); loadVolunteer(); } catch (err) { alert(err.message); }
    });
    $('supportForm').addEventListener('submit', async e => {
      e.preventDefault();
      try {
        const r = await post('/api/recovery/support', {area: $('supArea').value, housing: $('supHousing').value, damage_types: checked('supDmg'),
                                                       long_term_evacuation: $('supLong').checked, lease_signed: $('supLease').checked});
        $('supportResult').innerHTML = (r.candidates.length ? table(['ID', '지원 항목', '내용', '출처', '상태'], r.candidates.map(c =>
          `<tr><td>${E(c.id)}</td><td>${E(c.title)}</td><td>${E(c.content)}</td><td>${E(c.source)}${c.as_of ? ' · ' + E(c.as_of) : ' · 기준일 확인 필요'}</td><td>${chip(c.status, c.status === '확인됨' ? 'ok' : 'warn')}</td></tr>`)) : '<p>해당 가능성이 있는 항목이 없습니다.</p>') +
          `<p class="muted">${E(r.notice)}${r.hidden.length ? ` · 확인 전이라 표시하지 않은 항목 ${r.hidden.length}개` : ''}</p>` +
          (r.verification.verdict ? `<p>안내 문장 검증: ${chip(r.verification.verdict, r.verification.displayable ? 'ok' : 'warn')} ${r.draft ? E(r.draft) : '<span class="muted">통과 전이라 문장으로 안내하지 않고 위 규칙 데이터만 보여 줍니다.</span>'}</p>` : '');
      } catch (err) { $('supportResult').innerHTML = '<span class="err">오류: ' + E(err.message) + '</span>'; }
    });
    const fillExtract = (r, prefix = '') => {
      const f = r.fields;
      if (f.zone) $('rpZone').value = place(f.zone);
      if (f.householder) $('rpHolder').value = f.householder;
      if (f.damage_type && [...$('rpDamage').options].some(o => o.value === f.damage_type)) $('rpDamage').value = f.damage_type;
      $('extractResult').innerHTML = prefix + (r.manual_form_required
        ? `<span class="err">추출하지 못한 필드(${E(r.missing.join(', '))})가 있어 아래 입력 폼으로 직접 채워 주세요.</span>${r.ai_used ? '' : ' <span class="muted">AI 미사용</span>'}`
        : '추출한 값을 아래 폼에 채웠습니다. 확인 후 초안을 만드세요.');
    };
    $('extractBtn').addEventListener('click', async () => {
      try { fillExtract(await post('/api/recovery/extract', {text: $('docText').value})); }
      catch (err) { $('extractResult').innerHTML = '<span class="err">오류: ' + E(err.message) + '</span>'; }
    });
    $('parseBtn').addEventListener('click', async () => {
      const file = $('docFile').files[0];
      if (!file) { $('extractResult').innerHTML = '<span class="err">파일을 먼저 고르세요.</span>'; return; }
      if (file.size > 5 * 1024 * 1024) { $('extractResult').innerHTML = '<span class="err">파일은 5MB 이하여야 합니다.</span>'; return; }
      const btn = $('parseBtn'); btn.disabled = true; $('extractResult').textContent = '파일을 읽는 중…';
      try {
        const data = await new Promise((ok, bad) => { const fr = new FileReader(); fr.onload = () => ok(String(fr.result).split(',')[1] || ''); fr.onerror = () => bad(new Error('파일을 읽지 못했습니다')); fr.readAsDataURL(file); });
        const r = await post('/api/recovery/parse', {filename: file.name, data_base64: data, consent_external: $('docConsent').checked});
        $('docText').value = r.text;
        fillExtract(r, `${r.parser === 'upstage' ? '업스테이지 문서 파싱으로' : '텍스트 파일로'} 읽었습니다${r.pages ? `(${E(r.pages)}쪽)` : ''}${r.truncated ? ' · 길어서 앞부분만 표시' : ''}. `);
      } catch (err) { $('extractResult').innerHTML = '<span class="err">오류: ' + E(err.message) + '</span>'; }
      finally { btn.disabled = false; }
    });
    $('reportForm').addEventListener('submit', async e => {
      e.preventDefault();
      try {
        const r = await post('/api/recovery/report', {household: $('rpHousehold').value, damage_types: [$('rpDamage').value],
                                                      fields: {zone: $('rpZone').value, householder: $('rpHolder').value, damage_type: $('rpDamage').value}});
        show('reportResult', `<b>${E(r.id)}</b> · ${E(r.status)}<br>증빙 체크리스트: ${r.checklist.map(c => chip(c)).join('')}<div class="muted">${E(r.notice)}</div>`);
      } catch (err) { fail('reportResult', err); }
    });
    $('caseForm').addEventListener('submit', async e => {
      e.preventDefault();
      try { await post('/api/recovery/cases', {household: $('caseHousehold').value}); loadRecovery(); } catch (err) { alert(err.message); }
    });
    $('surveyForm').addEventListener('submit', async e => {
      e.preventDefault();
      try { await post('/api/recovery/surveys', {address: $('svAddress').value, damage_type: $('svDamage').value}); loadRecovery(); } catch (err) { alert(err.message); }
    });
    $('ckTarget').addEventListener('change', loadChecks);
    $('annHideEnded').addEventListener('change', loadAnnouncements);
    $('checkForm').addEventListener('submit', async e => {
      e.preventDefault();
      try {
        const r = await post('/api/welfare/checks/start', {zone: $('ckTarget').value, timeout_s: Number($('ckTimeout').value)});
        $('checkResult').textContent = `${r.started.length}가구에 안전 확인을 요청했습니다(모의).` + (r.skipped_no_consent.length ? ` 동의하지 않은 ${r.skipped_no_consent.join(', ')}는 제외했습니다.` : '');
        loadChecks();
      } catch (err) { $('checkResult').innerHTML = '<span class="err">오류: ' + E(err.message) + '</span>'; }
    });
    $('refreshSensor').addEventListener('click', loadSensorLive);
    $('sensorForm').addEventListener('submit', async e => {
      e.preventDefault();
      const now = Date.now() / 1000, rows = [];
      for (let i = 0; i < 3; i++) {   // 최근 3초 동안 같은 값이 유지된 것으로 보냄(연속 3회 조건)
        const ts = now - 2 + i;
        rows.push({node: 'A', sensor: 'water_low', ts, value: Number($('sWl').value)}, {node: 'A', sensor: 'water_high', ts, value: Number($('sWh').value)},
                  {node: 'B', sensor: 'tilt', ts, value: $('sTilt').checked ? 1 : 0}, {node: 'B', sensor: 'vibration', ts, value: Number($('sVb').value)});
      }
      try {
        const r = await post('/api/monitor/evaluate', {now, rain_stage: Number($('sRain').value), readings: rows});
        const o = r.output;
        show('sensorResult', `판정: ${chip(o.status, o.status === '확정후보' ? 'bad' : o.status === '보류' ? 'warn' : 'ok')} 제안 단계 <b>${E(o.level)}</b><br>${E(o.summary)}` +
          (o.false_alarm_log.length ? `<div class="muted">${E(o.false_alarm_log.join(' / '))}</div>` : '') +
          (r.approval_id ? `<div>단계 확정 승인 대기열에 올림(${E(r.approval_id)}) · 승인해야 상황판의 위험 단계가 바뀝니다.</div>` : '') +
          (o.calibrated ? '' : '<div class="muted">임계값 보정 전(임시값)</div>'));
        await loadApprovals();
      } catch (err) { fail('sensorResult', err); }
    });
    $('verifyForm').addEventListener('submit', async e => {
      e.preventDefault();
      try {
        const r = await post('/api/verify', {draft: $('vDraft').value, doc_ids: checked('vDoc')});
        show('verifyResult', `판정 ${chip(r.verdict, r.verdict === 'grounded' ? 'ok' : r.verdict === 'notGrounded' ? 'bad' : 'warn')} → <b>${E(r.route)}</b><br>${E(r.reason)}<div class="muted">근거 문서: ${E(r.doc_ids.join(', ') || '없음')}</div>`);
      } catch (err) { fail('verifyResult', err); }
    });
  }

  // ---------------------------------------------------------------- 새 긴급 신고·SOS 알림
  // 담당자가 새로고침을 누르지 않아도 주민의 긴급 신고(긴급도 1)와 봉사자 SOS가 15초 안에 보이게 한다.
  // 새로 들어오면 화면 맨 위 빨간 띠·탭 제목·짧은 소리로 알린다. 입력 중일 때는 목록을 다시 그리지 않는다
  let seenUrgent = null, seenSos = null, seenAns = null, alertCount = 0, lastAlertId = null;
  const TITLE = document.title;
  const editing = () => !!document.activeElement?.closest?.('#incidents, #approvals, form') || !!document.querySelector('#incidents details.fix[open]');
  function beep() {
    try {
      const a = new (window.AudioContext || window.webkitAudioContext)(), o = a.createOscillator(), g = a.createGain();
      o.frequency.value = 880; g.gain.value = 0.15; o.connect(g); g.connect(a.destination); o.start();
      setTimeout(() => { o.stop(); a.close(); }, 450);
    } catch (e) { /* 소리를 못 내는 브라우저 */ }
  }
  function raiseAlert(lines) {
    alertCount += lines.length;
    const bar = $('urgentBar');
    bar.hidden = false;
    bar.innerHTML = `<span>🔴 <b>새 긴급 알림 ${alertCount}건</b> · ${lines.map(E).join(' · ')}</span>` +
      '<button type="button" id="urgentGo">상황판에서 보기</button><button type="button" class="ok2" id="urgentOk">확인함</button>';
    document.title = `🔴(${alertCount}) ${TITLE}`;
    // '상황판에서 보기': 방금 들어온 긴급 신고 카드로 바로 가서 깜빡여 보인다
    $('urgentGo').onclick = async () => {
      go('overview');
      await loadIncidents();
      const card = lastAlertId && document.querySelector(`#incidents .incident[data-id="${CSS.escape(lastAlertId)}"]`);
      if (card) { card.classList.remove('fold-hide'); card.scrollIntoView({block: 'center', behavior: 'smooth'}); card.classList.add('flash'); setTimeout(() => card.classList.remove('flash'), 4000); }
      else window.scrollTo({top: 0});
    };
    $('urgentOk').onclick = () => { alertCount = 0; bar.hidden = true; document.title = TITLE; };
    beep();
  }
  async function watch() {
    try {
      const [inc, vol, ap] = await Promise.all([api('/api/incidents'), api('/api/volunteer'), api('/api/approvals')]);
      const urgent = inc.items.map(x => x.output).filter(o => o.urgency === 1 && o.state !== '오접수');
      // 담당자가 되물은 질문에 신고자가 텔레그램으로 답하면 함께 알린다
      const answers = inc.items.flatMap(x => (x.output.questions || []).flatMap(q => (q.answers || []).map(a => ({id: `${x.output.id}:${q.id}:${a.at}`, iid: x.output.id, text: a.text}))));
      if (seenUrgent) {
        const fresh = urgent.filter(o => !seenUrgent.has(o.id)), freshAns = answers.filter(a => !seenAns.has(a.id));
        if (fresh.length) lastAlertId = fresh[0].id;
        else if (freshAns.length) lastAlertId = freshAns[0].iid;
        const lines = [...fresh.map(o => `${o.type} ${o.id}: ${String(o.text || '').slice(0, 40)}`),
                       ...vol.sos.filter(s => !seenSos.has(s.id)).map(s => `봉사자 SOS(${s.name || vn(s.volunteer)})`),
                       ...freshAns.map(a => `💬 ${a.iid} 신고자 답: ${String(a.text).slice(0, 40)}`)];
        if (lines.length) raiseAlert(lines);
      }
      seenUrgent = new Set(urgent.map(o => o.id));
      seenAns = new Set(answers.map(a => a.id));
      seenSos = new Set(vol.sos.map(s => s.id));
      $('approvalCount').textContent = ap.items.length;
      $('approvalCount').hidden = false;
      if (current === 'overview' && !editing()) { loadIncidents(); loadSituation(); }
    } catch (e) { /* 다음 차례에 다시 */ }
  }

  window.opsInit = async () => {
    bind();
    try { await loadMeta(); loadIncidents(); } catch (e) { /* 메타 정보 없이도 화면은 쓴다 */ }
    let saved = 'overview';
    try { saved = localStorage.getItem('adminTab') || 'overview'; } catch (e) { /* 저장 불가 환경 */ }
    go(document.querySelector(`#tabs button[data-go="${saved}"]`) ? saved : 'overview');
    loadApprovals();
    refreshBand();
    setInterval(refreshBand, 15000);   // 다른 담당자가 단계를 확정해도 띠가 따라 바뀌게
    watch();
    setInterval(watch, 15000);         // 새 신고·SOS를 새로고침 없이
  };
  if (window.ADMIN_READY) window.opsInit();
})();
