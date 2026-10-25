/* 命令面板（Ctrl+K）。
 *
 * 只做一件事：**把「我要去哪儿」变成一个输入框**。
 * 会话多了以后，侧栏那点宽度装不下；模式、面板、导出这些动作散在三个角落，
 * 记快捷键比记位置容易。所以这里把三类东西合并成一张列表：
 * 动作（切模式、开面板、导出）、会话（按标题搜）、以及最近会话的正文命中。
 *
 * 不做的事：不做模糊匹配库。子序列匹配三十行就写完了，
 * 引一个库反而要处理「谁先谁后」和中文分词的坑。
 */
const Palette = (() => {
  let deps = null;
  let items = [];
  let index = 0;
  let searchToken = 0;

  function isOpen() {
    const node = document.getElementById('palette');
    return !!node && !node.classList.contains('hidden');
  }

  function close() {
    const node = document.getElementById('palette');
    if (node) node.classList.add('hidden');
  }

  function actions() {
    const list = [];
    const modes = (deps.state.boot && deps.state.boot.modes) || [];
    modes.forEach((mode) => {
      list.push({
        kind: '模式',
        text: `切到「${mode.label}」· ${mode.hint}`,
        hint: mode.id,
        run: () => deps.switchMode(mode.id),
      });
    });

    /* 左栏那十二个功能进面板：功能一多，靠数图标找比靠打字找慢得多。
       以前这里还有一组「面板」（抽屉），抽屉没了就不要留 —— 同一个去处列两遍，
       选哪个都行反而让人以为它们不一样。 */
    (deps.sections ? deps.sections() : []).forEach((section, position) => {
      list.push({
        kind: '左栏',
        text: `左栏切到「${section.label}」`,
        hint: position < 9 ? 'Ctrl+' + (position + 1) : '',
        run: () => deps.showSection(section.id),
      });
    });

    list.push(
      { kind: '动作', text: '看这个对话产出了哪些文件', hint: 'Alt+A', run: () => deps.artifacts() },
      { kind: '动作', text: '新开一个会话', hint: 'Ctrl+N', run: () => deps.newSession() },
      { kind: '动作', text: '重跑上一句（重试）', hint: 'Alt+R', run: () => deps.retry() },
      { kind: '动作', text: '导出这次会话（Markdown）', hint: 'md', run: () => deps.exportAs('md') },
      { kind: '动作', text: '导出这次会话（单页 HTML）', hint: 'html', run: () => deps.exportAs('html') },
      { kind: '动作', text: '切到过程图', hint: 'G', run: () => deps.switchView('graph') },
      { kind: '动作', text: '切回对话', hint: 'C', run: () => deps.switchView('chat') },
      { kind: '动作', text: '换工作目录', hint: '', run: () => deps.pickProject() },
      { kind: '动作', text: '优化输入框里这句话', hint: 'Alt+O', run: () => deps.optimize() }
    );
    return list;
  }

  /* 子序列匹配：q 的每个字符按顺序出现就算命中，连续命中给更高分 */
  function score(text, query) {
    const lower = text.toLowerCase();
    const target = query.toLowerCase();
    let at = 0;
    let hits = 0;
    let streak = 0;
    let best = 0;
    for (const char of target) {
      const found = lower.indexOf(char, at);
      if (found < 0) return -1;
      streak = found === at ? streak + 1 : 0;
      best = Math.max(best, streak);
      hits += 1;
      at = found + 1;
    }
    return hits * 10 + best * 12 - (lower.length - target.length) * 0.06;
  }

  function render() {
    const host = document.getElementById('palList');
    host.innerHTML = '';
    if (!items.length) {
      host.appendChild(h('div.empty', { style: { padding: '16px 12px' } }, '没有匹配的东西'));
      return;
    }
    items.forEach((item, position) => {
      const node = h('div.palItem' + (position === index ? '.on' : ''),
        { onmousedown: (event) => { event.preventDefault(); pick(position); } },
        h('span.kind', item.kind || '会话'),
        h('span.txt', item.text),
        item.hint ? h('span.hint', item.hint) : null
      );
      host.appendChild(node);
    });
    const active = host.children[index];
    if (active && active.scrollIntoView) active.scrollIntoView({ block: 'nearest' });
  }

  function compose(query) {
    const all = actions();
    if (!query.trim()) {
      const recent = (deps.state.traces || []).slice(0, 6).map((trace) => ({
        kind: '会话',
        text: trace.title || '未命名',
        hint: relTime(trace.created),
        run: () => deps.openTrace(trace.cid),
      }));
      items = all.concat(recent);
      index = 0;
      render();
      return;
    }

    const q = query.trim();
    const scored = [];
    all.forEach((item) => {
      const value = score(item.text, q);
      if (value >= 0) scored.push({ ...item, value: value + 6 });
    });
    (deps.state.traces || []).forEach((trace) => {
      const value = score(trace.title || '', q);
      if (value >= 0) {
        scored.push({
          kind: '会话', text: trace.title || '未命名',
          hint: relTime(trace.created), value,
          run: () => deps.openTrace(trace.cid),
        });
      }
    });
    scored.sort((a, b) => b.value - a.value);
    items = scored.map((item) => ({ ...item }));
    index = 0;
    render();

    // 标题里找不到时再翻正文。翻正文要读文件，所以单独一趟、并且防抖掉过期的结果
    searchBody(q, ++searchToken);
  }

  async function searchBody(query, token) {
    const result = await API.search(query);
    if (token !== searchToken || !isOpen()) return;
    if (!result.ok) return;
    const extra = (result.data || [])
      .filter((hit) => hit.where === 'body')
      .slice(0, 5)
      .map((hit) => ({
        kind: '正文',
        text: (hit.snippet || hit.title || '').slice(0, 90),
        hint: hit.title || '',
        run: () => deps.openTrace(hit.cid),
      }));
    if (!extra.length) return;
    items = items.concat(extra);
    render();
  }

  function pick(position) {
    const item = items[position];
    if (!item) return;
    close();
    setTimeout(() => item.run(), 0);
  }

  function open(prefill) {
    const node = document.getElementById('palette');
    if (!node) return;
    node.classList.remove('hidden');
    const input = document.getElementById('palInput');
    input.value = prefill || '';
    compose(input.value);
    input.focus();
    input.select();
  }

  function init(dependencies) {
    deps = dependencies;
    const input = document.getElementById('palInput');
    const node = document.getElementById('palette');

    input.addEventListener('input', () => compose(input.value));
    input.addEventListener('keydown', (event) => {
      if (event.key === 'ArrowDown') {
        event.preventDefault();
        if (items.length) { index = (index + 1) % items.length; render(); }
      } else if (event.key === 'ArrowUp') {
        event.preventDefault();
        if (items.length) { index = (index - 1 + items.length) % items.length; render(); }
      } else if (event.key === 'Enter') {
        event.preventDefault();
        pick(index);
      } else if (event.key === 'Escape') {
        event.preventDefault();
        close();
      }
    });

    node.addEventListener('click', (event) => {
      if (event.target === node) close();
    });
  }

  return { init, open, close, isOpen };
})();
