// 관리자 화면 현장 운영 탭: ① 센서, ③ 안부, ⑤ 복구·지원, ⑥ 봉사, ⑦ 검증, ⑧ 물품.
// admin.html의 공통 함수($, api, escapeHtml, showError, loadApprovals, CSRF)를 그대로 쓴다.
// 화면에 넣는 값은 모두 escapeHtml을 거친다.
(() => {
  const E = escapeHtml;
  const post = (path, body) => api(path, {method: 'POST', body: JSON.stringify(body || {})});
  const chip = (text, cls = '') => `<span class="chip ${cls}">${E(text)}</span>`;
  const fmtTime = ts => ts ? new Date(ts * 1000).toLocaleTimeString('ko-KR') : '';
  const table = (heads, rows) => rows.length
    ? `<table class="tbl"><thead><tr>${heads.map(h => `<th>${E(h)}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table>`
    : '<div class="muted">항목이 없습니다.</div>';
  const options = (items, label = x => x, value = x => x) => items.map(x => `<option value="${E(value(x))}">${E(label(x))}</option>`).join('');
  const show = (id, html, isErr = false) => { const el = $(id); el.hidden = false; el.classList.toggle('err', isErr); el.innerHTML = html; };
  const fail = (id, e) => show(id, '오류: ' + E(e.message), true);
  const checked = name => [...document.querySelectorAll(`input[name=${name}]:checked`)].map(x => x.value);
  const STATE_CLS = {완료: 'ok', 안전: 'ok', 반려: 'bad', 격상: 'bad', 보류: 'warn', '지연 응답': 'warn', 재추천: 'warn'};
  let current = 'overview', META = null, refreshTimer = null;

  // ---------------------------------------------------------------- 탭
  const LOADERS = {overview: loadSituation, supply: loadSupply, volunteer: loadVolunteer, recovery: loadRecovery,
                   welfare: loadChecks, sensor: loadVerifyDocs, approvals: () => loadApprovals(), dispatch: () => { loadDispatch(); loadControl(); },
                   guidance: loadIncidents};
  function go(tab) {
    current = tab;
    document.querySelectorAll('#tabs button').forEach(b => b.classList.toggle('on', b.dataset.go === tab));
    document.querySelectorAll('section[data-tab]').forEach(s => { s.hidden = s.dataset.tab !== tab; });
    clearInterval(refreshTimer);
    if (tab === 'welfare') refreshTimer = setInterval(loadChecks, 3000);   // 남은 시간·격상 자동 갱신
    if (LOADERS[tab]) LOADERS[tab]();
    try { localStorage.setItem('adminTab', tab); } catch (e) { /* 저장 불가 환경 */ }
  }

  // 승인 대기열 개수 배지 + 처리 후 관련 탭 새로 고침
  const baseLoadApprovals = loadApprovals;
  loadApprovals = async function () {
    await baseLoadApprovals();
    await addDispatchControls();
    try { $('approvalCount').textContent = (await api('/api/approvals')).items.length; } catch (e) { /* 배지만 */ }
    if (current !== 'approvals' && LOADERS[current] && current !== 'dispatch') LOADERS[current]();
  };

  async function loadMeta() {
    if (META) return META;
    const r = await api('/api/recovery'), s = await api('/api/supply');
    META = {zones: r.zones, households: r.households, volunteers: s.volunteers, catalog: s.catalog};
    const zoneOpts = options(META.zones, z => `${z.id} ${z.name}`, z => z.id);
    document.querySelectorAll('select.zoneSel').forEach(sel => { sel.innerHTML = zoneOpts; });
    $('ckZone').innerHTML = '<option value="all">전체</option>' + zoneOpts;
    const hhOpts = options(META.households, h => `${h.id} · ${h.zone} · ${h.floor}${h.factors.length ? ' · ' + h.factors.join('·') : ''}`, h => h.id);
    ['sHousehold', 'caseHousehold', 'rpHousehold'].forEach(id => { $(id).innerHTML = hhOpts; });
    $('vHousehold').innerHTML = '<option value="">선택 안 함</option>' + hhOpts;
    const items = Object.keys(META.catalog);
    $('sItem').innerHTML = '<option value="">(메모에서 찾기)</option>' + options(items, i => `${i} (${META.catalog[i].category})`, i => i);
    $('dnItem').innerHTML = options(items, i => `${i} (${META.catalog[i].category})`, i => i);
    const volOpts = options(META.volunteers, v => `${v.id} · ${v.zone} · ${v.mode}`, v => v.id);
    $('dnDonor').innerHTML = volOpts; $('sosVol').innerHTML = volOpts;
    const syncUnits = () => { $('dnUnit').innerHTML = options(META.catalog[$('dnItem').value]?.units || []); };
    $('dnItem').addEventListener('change', syncUnits); syncUnits();
    return META;
  }

  // ---------------------------------------------------------------- 상황판
  async function loadSituation() {
    try {
      const [stage, checks, vol, sup] = await Promise.all([api('/api/stage'), api('/api/welfare/checks'), api('/api/volunteer'), api('/api/supply')]);
      const sos = vol.sos.map(s => `<div class="incident err"><b>SOS</b> · 봉사자 ${E(s.volunteer)}${s.task ? ' · 작업 ' + E(s.task) : ''} · ${E(fmtTime(s.ts))} <button data-ack="${E(s.id)}" class="red">확인</button></div>`).join('');
      const esc = checks.checks.filter(c => c.state === '격상');
      const short = sup.forecast.filter(f => f.shortage);
      const waiting = sup.requests.filter(r => r.state === '검토').length;
      $('situation').innerHTML = sos +
        `<div>확정된 위험 단계: <b>${E(stage.level)}</b> ${stage.confirmed ? chip('담당자 확정 · ' + (stage.approved_by || ''), 'ok') : chip('미확정')}${stage.hazards?.length ? ' · ' + E(stage.hazards.join('·')) : ''}</div>` +
        `<div>안부 확인: 격상 ${esc.length}가구${esc.length ? ' (' + E(esc.map(c => c.household).join(', ')) + ')' : ''} · 대기 ${checks.counts['대기'] || 0}가구</div>` +
        `<div>물품: 검토 대기 요청 ${waiting}건 · 부족 예상 ${short.length ? E(short.map(f => f.item).join(', ')) : '없음'}</div>` +
        `<div>봉사: 진행 중 작업 ${vol.tasks.filter(t => t.state === '진행' || t.state === '투입승인').length}건 · 투입 중 봉사자 ${vol.volunteers.filter(v => v.busy).length}명</div>`;
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
      $('supplyRequests').innerHTML = table(['요청', '가구·구역', '품목·수량', '긴급도', '표시', '상태', '처리'], d.requests.map(r => {
        const acts = r.pending_approval ? `<span class="muted">배치 승인 대기 ${E(r.pending_approval)}</span>`
          : (REQ_ACTIONS[r.state] || []).map(([label, act, body, needReason]) =>
            `<button data-req="${E(r.id)}" data-act="${E(act)}" data-body='${E(JSON.stringify(body))}' ${needReason ? 'data-reason="1"' : ''} class="${label.includes('반려') || label.includes('못') ? 'gray' : ''}">${E(label)}</button>`).join('');
        const marks = [...r.flags.map(f => chip(f, 'warn')), ...r.urgency_reasons.map(u => chip(u)),
          r.route === 'manual_medicine' ? chip('의약품: 직접 처리', 'bad') : '', r.qty_unconfirmed ? chip('수량 확인 필요', 'warn') : '',
          r.merged_count > 1 ? chip(`요청 ${r.merged_count}건 병합`) : '',
          r.last_proposal?.procurement_needed ? chip(`부족 ${r.last_proposal.shortfall} · 외부 조달 필요`, 'bad') : ''].join('');
        return `<tr><td>${E(r.id)}</td><td>${E(r.household)} · ${E(r.zone)}</td><td>${E(r.item)} ${E(r.qty)}</td><td>${E(r.urgency)}</td><td>${marks}</td><td>${chip(r.state, STATE_CLS[r.state] || '')}</td><td>${acts}</td></tr>`;
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
        `<tr><td>${E(f.item)}</td><td>${E(f.qty)}</td><td>${E(f.rate_per_h)}</td><td>${f.hours_left === Infinity ? '-' : E(f.hours_left) + '시간'} ${f.shortage ? chip('부족 예상', 'bad') : ''}</td><td>${E(f.rate_source)}</td></tr>`)) +
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
      $('volTasks').innerHTML = table(['작업', '유형·구역', '필요', '추천·투입 인원', '상태', '처리'], [...d.tasks].reverse().map(t => {
        const rec = t.last_recommendation || {};
        const team = (t.team || rec.team || []).join(', ') || '-';
        const acts = t.pending_approval ? `<span class="muted">투입 승인 대기 ${E(t.pending_approval)}</span>`
          : (TASK_ACTIONS[t.state] || []).map(([label, act]) => `<button data-task="${E(t.id)}" data-act="${act}">${E(label)}</button>`).join('');
        return `<tr><td>${E(t.id)}</td><td>${E(t.type)} · ${E(t.zone)}${t.household ? ' · ' + E(t.household) : ''}</td><td>${E(t.people)}명 · ${E(t.hours)}시간 · ${E((t.equipment || []).join(', ') || '장비 없음')}</td><td>${E(team)}${rec.shortfall ? ' ' + chip('부족 ' + rec.shortfall + '명', 'warn') : ''}</td><td>${chip(t.state, STATE_CLS[t.state] || '')}</td><td>${acts}</td></tr>`;
      }));
      $('volTasks').querySelectorAll('button[data-task]').forEach(b => b.addEventListener('click', async () => {
        b.disabled = true;
        try {
          const r = await post(`/api/volunteer/tasks/${encodeURIComponent(b.dataset.task)}/${b.dataset.act}`);
          if (r.case_update) alert(`복구 케이스 ${r.case_update.case}에 반영했습니다. 남은 작업: ${r.case_update.remaining.join(', ') || '없음'}`);
          await loadApprovals(); await loadVolunteer();
        } catch (e) { alert(e.message); b.disabled = false; }
      }));
      $('volSos').innerHTML = d.sos.map(s => `<div class="incident err"><b>SOS</b> · ${E(s.volunteer)}${s.task ? ' · ' + E(s.task) : ''} · ${E(fmtTime(s.ts))} <button data-ack="${E(s.id)}" class="red">확인</button></div>`).join('');
      $('volSos').querySelectorAll('button[data-ack]').forEach(b => b.addEventListener('click', async () => {
        try { await post(`/api/volunteer/sos/${encodeURIComponent(b.dataset.ack)}/ack`); loadVolunteer(); } catch (e) { alert(e.message); }
      }));
      $('volList').innerHTML = table(['봉사자', '구역', '역량', '장비', '가능 시간', '참여 방식', '상태'], d.volunteers.map(v =>
        `<tr><td>${E(v.id)}</td><td>${E(v.zone)}</td><td>${E(v.skills.join(', '))}</td><td>${E(v.equipment.join(', ') || '-')}</td><td>${E(v.available_min)}분</td><td>${E(v.mode)}</td><td>${v.busy ? chip('투입 중', 'warn') : chip('대기', 'ok')}</td></tr>`));
    } catch (e) { showError('volTasks', e); }
  }

  // ---------------------------------------------------------------- ⑤ 복구·지원
  async function loadRecovery() {
    try {
      await loadMeta();
      const d = await api('/api/recovery');
      $('caseList').innerHTML = table(['케이스', '가구·구역', '현재 단계', '이력', '봉사 작업', '처리'], d.cases.map(c => {
        const idx = d.stages.indexOf(c.stage), next = d.stages[idx + 1];
        const tasks = Object.entries(c.tasks || {}).map(([t, s]) => chip(`${t} ${s}`, s === '완료' ? 'ok' : '')).join('') || '-';
        return `<tr><td>${E(c.id)}</td><td>${E(c.household)} · ${E(c.zone)}</td><td>${d.stages.map((s, i) => chip(s, i < idx ? 'ok' : i === idx ? 'warn' : '')).join('')}</td><td>${E(c.timeline.map(t => t.stage + (t.by ? '(' + t.by + ')' : '')).join(' → '))}</td><td>${tasks}</td><td>${next ? `<button data-case="${E(c.id)}" data-to="${E(next)}">${E(next)} 단계로</button>` : chip('완료', 'ok')}</td></tr>`;
      }));
      $('caseList').querySelectorAll('button[data-case]').forEach(b => b.addEventListener('click', async () => {
        b.disabled = true;
        try { await post(`/api/recovery/cases/${encodeURIComponent(b.dataset.case)}/advance`, {to: b.dataset.to}); loadRecovery(); } catch (e) { alert(e.message); b.disabled = false; }
      }));
      const zones = Object.keys(d.aggregate.by_zone).sort((a, b) => a.localeCompare(b, 'ko', {numeric: true}));
      $('surveyTable').innerHTML = `<p>입력 ${E(d.aggregate.total)}건</p>` + table(['구역', ...d.damage_types], zones.map(z =>
        `<tr><td>${E(z)}</td>${d.damage_types.map(t => `<td>${E(d.aggregate.by_zone[z][t] || 0)}</td>`).join('')}</tr>`));
    } catch (e) { showError('caseList', e); }
  }

  // ---------------------------------------------------------------- ③ 안부
  async function loadChecks() {
    try {
      await loadMeta();
      const d = await api('/api/welfare/checks');
      if (d.escalated_now.length) $('checkResult').innerHTML = `<span class="err">방금 격상: ${E(d.escalated_now.join(', '))} (담당자 확인 필요, 자동 출동 없음)</span>`;
      $('checkList').innerHTML = table(['가구', '구역', '취약 요인', '상태', '남은 시간', '처리'], d.checks.map(c =>
        `<tr><td>${E(c.household)}</td><td>${E(c.zone)}</td><td>${E(c.factors.join(', ') || '-')}</td><td>${chip(c.state, STATE_CLS[c.state] || '')}</td><td>${c.seconds_left != null ? E(Math.max(0, c.seconds_left)) + '초' : '-'}</td><td>${['대기', '격상'].includes(c.state) ? `<button data-hh="${E(c.household)}">안전 응답(노드 D 모의)</button>` : ''}</td></tr>`));
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
      $('controlList').innerHTML = table(['ID', '구간', '연결 구역', '상태', '처리'], d.control_zones.map(c =>
        `<tr><td>${E(c.id)}</td><td>${E(c.name)}</td><td>${E(c.between.join(' ↔ '))}</td><td>${c.active ? chip('통제 중', 'bad') : chip('해제', 'ok')}</td><td><button data-cz="${E(c.id)}" data-on="${c.active ? '' : '1'}" class="${c.active ? 'gray' : 'red'}">${c.active ? '통제 해제' : '통제 시작'}</button></td></tr>`));
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
      const card = document.querySelector(`#approvals button[data-resolve="yes"][data-id="${CSS.escape(a.id)}"]`)?.closest('.incident');
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
                    condition: $('dnCond').value === '파손·오염' ? '파손' : $('dnCond').value, days_left: days === '' ? null : Number(days), zone: $('dnZone').value};
      try { const r = await post('/api/supply/donations', body); show('donationResult', `${E(r.id)} 등록 · ${E(r.state)} (담당자 검수 후 매칭 대상)`); loadSupply(); }
      catch (err) { fail('donationResult', err); }
    });
    $('volForm').addEventListener('submit', async e => {
      e.preventDefault();
      const body = {text: $('vText').value, zone: $('vZone').value};
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
    $('extractBtn').addEventListener('click', async () => {
      try {
        const r = await post('/api/recovery/extract', {text: $('docText').value});
        const f = r.fields;
        if (f.zone) $('rpZone').value = f.zone;
        if (f.householder) $('rpHolder').value = f.householder;
        if (f.damage_type && [...$('rpDamage').options].some(o => o.value === f.damage_type)) $('rpDamage').value = f.damage_type;
        $('extractResult').innerHTML = r.manual_form_required
          ? `<span class="err">추출하지 못한 필드(${E(r.missing.join(', '))})가 있어 아래 입력 폼으로 직접 채워 주세요.</span>${r.ai_used ? '' : ' <span class="muted">AI 미사용</span>'}`
          : '추출한 값을 아래 폼에 채웠습니다. 확인 후 초안을 만드세요.';
      } catch (err) { $('extractResult').innerHTML = '<span class="err">오류: ' + E(err.message) + '</span>'; }
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
      try { await post('/api/recovery/surveys', {zone: $('svZone').value, damage_type: $('svDamage').value}); loadRecovery(); } catch (err) { alert(err.message); }
    });
    $('checkForm').addEventListener('submit', async e => {
      e.preventDefault();
      try {
        const r = await post('/api/welfare/checks/start', {zone: $('ckZone').value, timeout_s: Number($('ckTimeout').value)});
        $('checkResult').textContent = `${r.started.length}가구에 안전 확인을 요청했습니다(모의).` + (r.skipped_no_consent.length ? ` 동의하지 않은 ${r.skipped_no_consent.join(', ')}는 제외했습니다.` : '');
        loadChecks();
      } catch (err) { $('checkResult').innerHTML = '<span class="err">오류: ' + E(err.message) + '</span>'; }
    });
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

  window.opsInit = async () => {
    bind();
    let saved = 'overview';
    try { saved = localStorage.getItem('adminTab') || 'overview'; } catch (e) { /* 저장 불가 환경 */ }
    go(document.querySelector(`#tabs button[data-go="${saved}"]`) ? saved : 'overview');
    loadApprovals();
  };
  if (window.ADMIN_READY) window.opsInit();
})();
