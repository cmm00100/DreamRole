/* 输出 / 日志抽屉（设计稿 dialogs-v1.html 的 C 段）
 *
 * 为什么需要它：工具判定、定时任务、审批这三件事一直在后台发生，
 * 但它们各自只在自己那一屏露一下脸 —— 跑完了就消失，出了问题也没地方回看。
 * 于是「刚才那一下到底放行了没有」只能靠翻对话流，翻不到就只剩猜。
 *
 * 形态照设计稿：折叠时就是一行 34px，把「几件在跑、几条日志、最后一条是什么」
 * 写在脸上；展开才出现日志 / 任务 / 审批三个 tab。
 * 折叠态是有用的，所以它常驻，不是「出了问题才弹出来的浮层」。
 *
 * 数据来源都是现成的：日志 = 审计链（/api/security 的 entries），
 * 任务 = 定时任务（/api/schedules），审批 = 审计里 verdict 为 gate/skip/deny 的那几条。
 * 没有为这个抽屉新造一份状态 —— 造了就会和真实现场对不上。
 */

const Dock = (() => {
  const srcLabel = {
    agent: 'agent',
    user: 'user',
  };

  const state = {
    open: false,
    tab: 'logs',         // logs | jobs | appr
    level: 'all',        // all | warn | err
    /* 空集合 = 全部来源。用集合而不是数组，是因为点选是「多选开关」，
       数组每次都要 includes，写起来也更容易漏掉去重。 */
    sources: new Set(),
    follow: true,
    entries: [],
    jobs: [],
    armed: false,        // 「清空」按了第一下
    armTimer: null,
    timer: null,
  };

  const VERDICT_LEVEL = { deny: 'err', gate: 'warn', skip: 'warn', allow: '' };
  const VERDICT_TEXT = { deny: '拒绝', gate: '待批', skip: '没同意', allow: '放行' };

  function esc(text) {
    return String(text == null ? '' : text)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function clock(ts) {
    const d = new Date((Number(ts) || 0) * 1000);
    const pad = (n) => String(n).padStart(2, '0');
    return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  }

  function levelOf(entry) {
    return VERDICT_LEVEL[entry.verdict] || '';
  }

  function isAppr(entry) {
    return entry.verdict === 'gate' || entry.verdict === 'skip' || entry.verdict === 'deny';
  }

  /* 一条日志 = 谁 · 做了什么 · 对谁。三段都是现场字段，不做任何概括：
     概括过的日志在要查的时候一定少了那一截。 */
  function msgOf(entry) {
    const bits = [entry.action || ''];
    if (entry.target) bits.push(entry.target);
    if (entry.detail) bits.push(entry.detail);
    return bits.filter(Boolean).join(' · ');
  }

  function sources() {
    const seen = [];
    state.entries.forEach((entry) => {
      const name = entry.actor || 'agent';
      if (!seen.includes(name)) seen.push(name);
    });
    return seen;
  }

  function visibleEntries() {
    const base = state.tab === 'appr' ? state.entries.filter(isAppr) : state.entries;
    return base.filter((entry) => {
      if (!state.sources.size) return true;
      return state.sources.has(entry.actor || 'agent');
    }).filter((entry) => {
      if (state.level === 'all') return true;
      const level = levelOf(entry);
      if (state.level === 'warn') return level === 'warn';
      return level === 'err';
    });
  }

  function logRow(entry) {
    const level = levelOf(entry);
    const at = entry.actor || 'agent';
    return `<div class="dockLg${level ? ' ' + level : ''}" title="${esc(msgOf(entry))}">`
      + `<span class="tm">${clock(entry.ts)}</span>`
      + `<span class="src">${esc(srcLabel[at] || at)}</span>`
      + `<span class="msg">${esc(msgOf(entry))}</span>`
      + `<span class="v">${esc(VERDICT_TEXT[entry.verdict] || '')}</span>`
      + '</div>';
  }

  function jobRow(job) {
    const running = job.running;
    const off = job.enabled === false;
    const stateText = running ? '跑着' : (off ? '已暂停' : '等待');
    const cls = running ? ' warn' : (off ? '' : '');
    const when = job.next_label && job.next_label !== '—' ? job.next_label : '—';
    return `<div class="dockLg${cls}" title="${esc(job.prompt || '')}">`
      + `<span class="tm">${esc(when)}</span>`
      + `<span class="src">job</span>`
      + `<span class="msg">${esc(job.title || '定时任务')} · ${esc(job.freq_label || '')}</span>`
      + `<span class="v">${esc(stateText)}</span>`
      + '</div>';
  }

  /* ---------------------------------------------------------------- 画 */

  function paintSummary() {
    const bar = $('#dockSum');
    const live = $('#dockLive');
    if (!bar) return;
    const running = state.jobs.filter((job) => job.running).length;
    const last = state.entries[0];
    if (live) {
      live.classList.toggle('on', running > 0);
      live.classList.toggle('bad', !running && state.entries.some((e) => e.verdict === 'deny'));
    }
    const bits = [
      `运行中 <b>${running}</b>`,
      `日志 <b>${state.entries.length}</b> 条`,
    ];
    if (last) bits.push(`最后：${esc(msgOf(last)).slice(0, 80)}`);
    bar.innerHTML = bits.join(' · ');
  }

  function paintHead() {
    const host = $('#dockHead');
    if (!host) return;
    const logs = state.entries.length;
    const jobs = state.jobs.length;
    const appr = state.entries.filter(isAppr).length;
    const tabs = [['logs', '日志', logs], ['jobs', '任务', jobs], ['appr', '审批', appr]];
    host.innerHTML = tabs.map(([key, name, count]) =>
      `<button class="dockTab${state.tab === key ? ' on' : ''}" data-tab="${key}">`
      + `${name}<span class="n">${count}</span></button>`).join('')
      + `<span class="dockGrow"></span>`
      + `<button class="dockFold" id="dockFold" title="收起">收起 ∨</button>`;
    $$('.dockTab', host).forEach((button) => {
      button.addEventListener('click', () => {
        state.tab = button.dataset.tab;
        paintHead();
        paintFilters();
        paintBody();
      });
    });
    const fold = $('#dockFold', host);
    if (fold) fold.addEventListener('click', () => close());
  }

  function paintFilters() {
    const host = $('#dockFilters');
    if (!host) return;
    if (state.tab === 'jobs') { host.innerHTML = ''; return; }
    const levels = [['all', '全部'], ['warn', '警告'], ['err', '错误']];
    const chips = levels.map(([key, name]) =>
      `<button class="dockF${state.level === key ? ' on' : ''}" data-level="${key}">${name}</button>`);
    chips.push('<span class="dockGap"></span>');
    sources().forEach((name) => {
      chips.push(`<button class="dockF${state.sources.has(name) ? ' on' : ''}" data-src="${esc(name)}">`
        + `${esc(srcLabel[name] || name)}</button>`);
    });
    host.innerHTML = chips.join('');
    $$('[data-level]', host).forEach((button) => {
      button.addEventListener('click', () => {
        state.level = button.dataset.level;
        paintFilters();
        paintBody();
      });
    });
    $$('[data-src]', host).forEach((button) => {
      button.addEventListener('click', () => {
        const name = button.dataset.src;
        if (state.sources.has(name)) state.sources.delete(name);
        else state.sources.add(name);
        paintFilters();
        paintBody();
      });
    });
  }

  function paintBody() {
    const host = $('#dockLogs');
    if (!host) return;
    if (state.tab === 'jobs') {
      host.innerHTML = state.jobs.length
        ? state.jobs.map(jobRow).join('')
        : '<div class="dockEmpty">还没有定时任务。去自动化页建一个，到点它会自己跑。</div>';
      return;
    }
    const rows = visibleEntries();
    if (!rows.length) {
      host.innerHTML = state.tab === 'appr'
        ? '<div class="dockEmpty">没有需要你点头的事。</div>'
        : '<div class="dockEmpty">还没有记录。聊两句，这里就会有东西。</div>';
      return;
    }
    host.innerHTML = rows.map(logRow).join('');
    // 新的在最上面：跟随 = 把列表按回顶部，否则用户滚到下面看的时候会被顶回去
    if (state.follow) host.scrollTop = 0;
  }

  function paintFoot() {
    const host = $('#dockFoot');
    if (!host) return;
    if (state.tab === 'jobs') {
      host.innerHTML = '<span>' + state.jobs.filter((job) => job.enabled !== false).length
        + ' 个开着</span><span class="dockGrow"></span><span class="dockHint">在自动化页里改</span>';
      return;
    }
    host.innerHTML =
      `<button class="dockSwitch${state.follow ? ' on' : ''}" id="dockFollow">`
      + `<i></i>${state.follow ? '自动滚动开' : '自动滚动关'}</button>`
      + '<span class="dockGrow"></span>'
      + `<button id="dockClear" class="${state.armed ? 'arm' : ''}">`
      + `${state.armed ? '再点一次清空' : '清空'}</button>`
      + '<button id="dockCopy">复制全部</button>'
      + '<button id="dockExport">导出 .log</button>';
    $('#dockFollow', host).addEventListener('click', () => {
      state.follow = !state.follow;
      paintFoot();
      if (state.follow) $('#dockLogs').scrollTop = 0;
    });
    $('#dockClear', host).addEventListener('click', clearAll);
    $('#dockCopy', host).addEventListener('click', copyAll);
    $('#dockExport', host).addEventListener('click', exportLog);
  }

  function paint() {
    paintSummary();
    paintHead();
    paintFilters();
    paintBody();
    paintFoot();
  }

  /* ---------------------------------------------------------------- 动作 */

  /* 清空要按两下。审计链是「事后能证明没被动过」的东西，
     一下点没了就再也证明不了；但拦一个原生 confirm 又太重（窗口里那一套很难看）。
     折中：第一下把按钮变成「再点一次清空」，3 秒内不动就自己退回去。 */
  async function clearAll() {
    if (!state.armed) {
      state.armed = true;
      paintFoot();
      clearTimeout(state.armTimer);
      state.armTimer = setTimeout(() => { state.armed = false; paintFoot(); }, 3000);
      return;
    }
    state.armed = false;
    clearTimeout(state.armTimer);
    const res = await API.clearAudit();
    if (!res.ok) {
      toast('清不掉：' + res.error, 'bad');
      paintFoot();
      return;
    }
    state.entries = [];
    paint();
    toast('日志清空了');
  }

  function visibleText() {
    if (state.tab === 'jobs') {
      return state.jobs.map((job) =>
        `${job.next_label || '—'}\tjob\t${job.title}\t${job.freq_label}`).join('\n');
    }
    return visibleEntries().map((entry) =>
      `${clock(entry.ts)}\t${entry.actor}\t${msgOf(entry)}\t${entry.verdict}`).join('\n');
  }

  async function copyAll() {
    const text = visibleText();
    if (!text) return toast('这里还是空的');
    try {
      await navigator.clipboard.writeText(text);
      toast('复制了 ' + text.split('\n').length + ' 行');
    } catch {
      toast('浏览器不让写剪贴板', 'bad');
    }
  }

  function exportLog() {
    const text = visibleText();
    if (!text) return toast('这里还是空的');
    const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `dreamrole-${state.tab}.log`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
    toast('导出成 .log 了');
  }

  /* ---------------------------------------------------------------- 拉数据 */

  async function refresh() {
    const [sec, jobs] = await Promise.all([API.security(), API.schedules()]);
    if (sec.ok) {
      const audit = sec.data.audit || {};
      state.entries = Array.isArray(audit.entries) ? audit.entries : [];
    }
    if (jobs.ok) {
      const list = jobs.data.jobs || jobs.data;
      state.jobs = Array.isArray(list) ? list : [];
    }
    paint();
  }

  function open() {
    state.open = true;
    $('#dockBody').classList.remove('hidden');
    $('#dockBar').setAttribute('aria-expanded', 'true');
    const txt = $('#dockToggleTxt');
    if (txt) txt.textContent = '收起 ∨';
    refresh();
    /* 展开的时候才轮询：日志的价值在「正在发生」，
       收起来之后每几秒去问一次后端是白开的电费。 */
    clearInterval(state.timer);
    state.timer = setInterval(() => {
      if (document.hidden) return;
      refresh();
    }, 4000);
  }

  function close() {
    state.open = false;
    $('#dockBody').classList.add('hidden');
    $('#dockBar').setAttribute('aria-expanded', 'false');
    const txt = $('#dockToggleTxt');
    if (txt) txt.textContent = '展开 ∧';
    clearInterval(state.timer);
    state.timer = null;
  }

  function toggle() {
    if (state.open) close();
    else open();
  }

  function init() {
    const bar = $('#dockBar');
    if (!bar) return;
    bar.addEventListener('click', toggle);
    state.entries = [];
    state.jobs = [];
    paint();
    refresh();
    /* 每轮跑完再来问一次：收起状态下没有轮询，但「刚做完一件事」是最该更新的时刻。 */
    setInterval(() => {
      if (!state.open && !document.hidden) refresh();
    }, 15000);
  }

  return {
    init, open, close, toggle, refresh,
    get openAt() { return state.open; },
    get tab() { return state.tab; },
    /* 自检用：把当前状态摊出来，省得测试去猜 DOM */
    snapshot: () => ({
      open: state.open,
      tab: state.tab,
      level: state.level,
      follow: state.follow,
      logs: state.entries.length,
      jobs: state.jobs.length,
      appr: state.entries.filter(isAppr).length,
      shown: state.tab === 'jobs' ? state.jobs.length : visibleEntries().length,
    }),
  };
})();
