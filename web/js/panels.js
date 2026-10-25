/* 左侧功能区的内容：工具、技能、MCP、定时任务、改动记录、检查点、记忆、专家、设置。
 *
 * 这些块都遵循同一个模式：拉数据 → 列出来 → 就地改。
 * 不做二级页面，因为它本质上是「查一下、改一下」的地方，
 * 每多一层跳转，用户就多一次「我刚才在哪」。
 *
 * 这里只负责「把某一栏画出来」，不管它在哪 —— 宿主由 sidebar.js 给。
 * 以前右上也挂了一个抽屉，同一批内容两个入口，用户得先想「从哪边进」；
 * 现在右上角改成「这个对话的产物」，导航只剩左栏一条路（见 artifacts.js）。
 */

const Panels = (() => {
  let current = 'tools';
  let ctx = null;

  /* 切换栏再切回来时保住输入（搜索词、选中的记忆）—— 面板重画一次就把
     用户刚打的字清空，是最容易被骂的那种「小问题」。 */
  const searchState = { q: '' };
  const memoryState = { id: '' };
  let uid = 0;   // 同一栏可能被重画多次，id 得能分辨

  const BUILDERS = {
    search: buildSearch,
    plugins: buildPlugins,
    experts: buildExperts,
    assistants: buildTeam,
    schedules: buildSchedules,
    skills: buildSkills,
    tools: buildTools,
    mcp: buildMcp,
    changes: buildChanges,
    software: buildSoftware,
    memory: buildMemory,
    security: buildSecurity,
    runtime: buildRuntime,
    usage: buildUsage,
    settings: buildSettings,
  };

  function init(app) {
    ctx = app;
  }

  /* 「跳到某一栏」：外部（深链 ?panel=、没填 key 去设置、错误提示）都走这里。
     它切的是左栏，不再开一个浮层 —— 只有一处导航，位置感才不会丢。 */
  function open(tab) {
    if (!tab) return;
    current = tab;
    if (typeof Sidebar !== 'undefined' && Sidebar.showByTab) Sidebar.showByTab(tab);
  }

  /* 把某一栏画进容器。`sub` 只有插件页用：它下面挂着三格，父栏画完之后
     还得知道自己该点亮哪一格。 */
  let currentHost = null;
  /* 当前点亮的是哪一格（插件页的三个 tab 就靠它）。
     重画时必须把它带回去 —— 少了这一句，在 Skill 格里改完一个技能，
     页面会跳回「插件」格，用户以为自己的操作把它踢走了。 */
  let currentSub = '';

  /* 面板里改完东西要重画**这一页**，不是只刷左栏。
     以前只有 refresh()（刷左栏计数），在团队页切频道时整个页面不会动。 */
  function rerender() {
    if (currentHost) renderInto(currentHost, current, currentSub);
  }

  /* 同一个容器同一时刻只允许有一次重画在跑。
   *
   * renderInto 是 async 的：先清空、再 await 一次接口、回来再往里写。
   * 两个重画挨着发生（深链「打开面板」+「切到某一格」就是连着两步）时，
   * 两份内容会交替写进同一个容器 —— 页头、tab 条、空态各出现两遍。
   * 挡在外面的办法不是排队：排队会让「上一次还没画完就先清空一次」，
   * 中间那一瞬读 DOM 的调用（自检、真用户的眼睛）看到的是空壳。
   * 所以由调用方保证不重复发起：切 tab 之前先把状态定好，再让它画。
   */
  function renderInto(host, tab, sub) {
    return paint(host, tab, sub);
  }

  async function paint(host, tab, sub) {
    const target = tab || current;
    if (tab) current = tab;
    currentSub = sub || '';
    currentHost = host;
    host.classList.remove('flush');
    host.innerHTML = '';
    host.appendChild(h('div.empty', '读取中…'));
    const builder = BUILDERS[target];
    if (!builder) {
      host.innerHTML = '';
      host.appendChild(h('div.empty', '没有这一栏'));
      return;
    }
    try {
      host.innerHTML = '';
      await builder(host, currentSub);
    } catch (err) {
      host.innerHTML = '';
      host.appendChild(h('div.empty', '加载失败：' + err.message));
    }
  }

  /* 左栏那一栏的内容需要重画时用它（切完工具开关、删完记忆…）。 */
  function refresh() {
    if (typeof Sidebar !== 'undefined' && Sidebar.refresh) Sidebar.refresh();
  }


  // ------------------------------------------------------------- 插件页

  /* 插件页 = 三格：插件 / Skill / 专家。
   *
   * 为什么合成一页：它们在产品里本来就是同一件事的三层 —— 插件（工具 + 连接器）
   * 让能力有手脚，Skill 说这类活怎么干，专家决定谁来做。摊成左栏四格之后，
   * 一半的导航被它们占了，会话树只剩一小条。
   *
   * 版式照设计稿：头（标题 + 一句说明 + 一颗按钮）→ tab 条 → 指标格 →
   * 筛选 chips → 分组 + 两列卡片，右边自己带一列统计卡。
   *
   * 右列是这一页自己的，不是全局那条右栏 —— 打开面板页时全局右栏会收起
   * （见 sidebar.js），两列并排会变成两条同名的边。
   */
  const PLUGIN_TABS = [
    { id: 'plugins', sec: 'plugins', label: '插件', note: '让能力有手脚：工具与连接器 MCP' },
    { id: 'skills', sec: 'skills', label: 'Skill', note: '告诉模型「这类活怎么干」，用到时才读全文' },
    { id: 'experts', sec: 'experts', label: '专家', note: '决定「谁来做」：立场、工具边界、默认模型' },
  ];

  /* 分组名 → 图标。设计稿里每张卡左边有一个 28px 的图标块；
     没有它，五十多张卡就是一片纯文字，扫的时候眼睛没有落点。 */
  const PG_ICON = {
    文件: '📄', 执行: '⌨', 数据: '▤', 联网: '🌐', 系统: '⚙',
    时间: '◷', 桌面: '▣', 记忆: '◍', 清理: '⌫', 协作: '⬡', 其他: '◈',
  };

  /* 筛选和排序是「同一批数据的两种看法」，记在模块里 ——
     重画一次就把用户刚选的筛掉，是最容易被骂的那种小问题。 */
  let pgFilter = 'all';
  let pgSort = 'on';        // on = 启用的排前面；name = 按名字
  let pgTabSeen = '';       // 换 tab 时把筛选复位
  let pgAddMcp = null;      // 「接一个连接器」要能滚到表单并把它展开

  function pluginCounts() {
    const boot = (ctx && ctx.state && ctx.state.boot) || {};
    const tools = boot.tools || [];
    const skills = boot.skills || [];
    const mcp = Object.keys(boot.mcp || {});
    return {
      plugins: tools.length + mcp.length,
      skills: skills.length,
      experts: (boot.experts || []).length,
      tools: tools.length,
      toolsOn: tools.filter((one) => one.enabled).length,
      groups: new Set(tools.map((one) => one.group || '其他')).size,
      mcp: mcp.length,
      own: skills.filter((one) => one.editable).length,
      builtin: skills.filter((one) => !one.editable).length,
      readOnly: tools.filter((one) => one.read_only).length,
      defaultOff: tools.filter((one) => one.default_off).length,
    };
  }

  /* ---- 这一页的零件 ---- */

  function pgBadge(text, kind) {
    return h('span.badge' + (kind ? '.' + kind : ''), text);
  }

  /* 一张卡：图标块 + 名字/版本小字 + 一行描述 + 标签行 +（右边）开关或状态。
     所有格共用它 —— 三格长得一样，靠的是同一个函数，不是三份复制。 */
  function pgCard(opts) {
    return h('div.pgCard', opts.tool ? { dataset: { tool: opts.tool } } : null,
      h('div.pcIc', opts.icon || '◈'),
      h('div.pcMain',
        h('div.pcTtl',
          h('span.pcName', opts.name),
          opts.ver ? h('span.pcVer', opts.ver) : null),
        h('div.pcDesc', { title: opts.desc || '' }, opts.desc || ''),
        opts.meta || null),
      opts.right || null
    );
  }

  function pgStatCell(value, label, warn) {
    return h('div.pgMetric' + (warn ? '.warn' : ''),
      h('span.pgV', String(value)), h('span.pgK', label));
  }

  function pgStats(cells) {
    return h('div.pgStats', cells.map(([value, label, warn]) => pgStatCell(value, label, warn)));
  }

  /* 分组：一行小字 +（可折叠的）两列卡片。
     折叠不是为了省地方 —— 五十多个工具全铺开要滚很久，而「文件」之外的那几组
     用户心里本来就有数在哪一块。 */
  function pgGroup(name, count, collapsed, body) {
    const head = h('div.pgGroupH',
      h('span.pgGroupName', name),
      count ? h('span.gcount', '（' + count + '）') : null,
      h('span.grow'),
      h('span.garrow', collapsed ? '▸' : '▾'));
    const box = h('div.pgGroup' + (collapsed ? '.closed' : ''), head, body);
    head.addEventListener('click', () => {
      const closed = box.classList.toggle('closed');
      head.querySelector('.garrow').textContent = closed ? '▸' : '▾';
    });
    return box;
  }

  function pgFilters(chips, onPick) {
    const row = h('div.pgFilters');
    chips.forEach(([id, label]) => row.appendChild(
      h('button.pgChip' + (pgFilter === id ? '.on' : ''), {
        'data-filter': id,
        onclick: () => { pgFilter = id; onPick(); },
      }, label)
    ));
    row.appendChild(h('div.grow'));
    row.appendChild(h('button.pgSort', {
      title: '换一种排法',
      onclick: () => { pgSort = pgSort === 'on' ? 'name' : 'on'; onPick(); },
    }, pgSort === 'on' ? '按启用状态排序 ▾' : '按名字排序 ▾'));
    return row;
  }

  function pgSortItems(items) {
    const copy = items.slice();
    copy.sort((a, b) => {
      if (pgSort === 'name') return String(a.name).localeCompare(String(b.name));
      const gap = (b.enabled ? 1 : 0) - (a.enabled ? 1 : 0);
      return gap || String(a.name).localeCompare(String(b.name));
    });
    return copy;
  }

  function pgSideCard(title, rows) {
    return h('div.rc',
      h('div.rcH', h('span.rcT', title), h('span.grow')),
      rows.map(([key, value]) => h('div.pgKv', h('span', key), h('b', value))));
  }

  function pgShortPath(path) {
    const raw = String(path || '');
    if (raw.length <= 22) return raw || '未设置';
    return raw.slice(0, 6) + '…' + raw.slice(-12);
  }

  function renderPluginsSide(cards, n) {
    const st = (ctx && ctx.state) || {};
    cards.appendChild(pgSideCard('能力统计', [
      ['插件行', n.plugins + '（启用 ' + (n.toolsOn + n.mcp) + '）'],
      ['Skill', String(n.skills)],
      ['专家', n.experts + (st.expert ? '（当前 ' + st.expert + '）' : '')],
    ]));
    cards.appendChild(pgSideCard('工具分布', [
      ['分组', String(n.groups)],
      ['只读工具', String(n.readOnly)],
      ['默认关', String(n.defaultOff)],
      ['连接器', String(n.mcp)],
    ]));
    cards.appendChild(h('div.pgTip',
      '⚠ 关掉的工具不会出现在模型的可选列表里 —— 等于这个能力它根本没有。'
      + '改完立刻生效，下一轮就按新的走。'));
    cards.appendChild(pgSideCard('当前挂载', [
      ['底层', ENGINE_LABELS[st.engine] || 'Role 1'],
      ['专家', st.expert || '未选'],
      ['工作区', pgShortPath(st.project)],
      ['本轮 Skill', String((st.skillPick || []).length)],
    ]));
  }

  function renderSkillsSide(cards, n) {
    const st = (ctx && ctx.state) || {};
    const picked = st.skillPick || [];
    cards.appendChild(pgSideCard('能力统计', [
      ['全部', String(n.skills)],
      ['自己的', String(n.own)],
      ['内置', String(n.builtin)],
    ]));
    cards.appendChild(h('div.pgTip',
      '技能只在系统提示里报名字和一句话。模型觉得用得上，才会去读全文 —— '
      + '所以技能再多也不占上下文。'));
    cards.appendChild(pgSideCard('本轮挂着', picked.length
      ? picked.map((name) => [name, '已挂'])
      : [['这一轮', '没挂技能']]));
  }

  function renderExpertsSide(cards, n) {
    const st = (ctx && ctx.state) || {};
    cards.appendChild(pgSideCard('能力统计', [
      ['专家', String(n.experts)],
      ['当前', st.expert || '未选'],
    ]));
    cards.appendChild(h('div.pgTip',
      '专家换的是立场（先看什么、什么算做完），技能换的是能力。'
      + '绑在当前会话上；新会话会沿用上一次选的。'));
    cards.appendChild(pgSideCard('跟着专家走', [
      ['模式', modeLabel(st.mode) || st.mode],
      ['权限', st.permission],
    ]));
  }

  function openAddMcp() {
    if (typeof pgAddMcp === 'function') return pgAddMcp();
    toast('连接器那一段还没画出来，稍等一下', 'bad');
  }

  async function buildPlugins(body, sub) {
    const active = sub === 'skills' || sub === 'experts' ? sub : 'plugins';
    if (pgTabSeen !== active) { pgTabSeen = active; pgFilter = 'all'; pgSort = 'on'; }
    const n = pluginCounts();

    /* 整页自己排：左边内容列 + 右边统计列。
       `flush` 让宿主把内边距和滚动条交出来 —— 头和 tab 条要贴着边上那条线，
       套在 18px 的 padding 里就永远对不齐设计稿。 */
    body.classList.add('flush');
    const main = h('div.pgMain');
    const side = h('div.pgSide');
    body.appendChild(main);
    body.appendChild(side);

    /* ---- 头 ---- */
    const HEADS = {
      plugins: {
        title: '插件',
        line: h('div.pgSub', '三类能力都在这里管：', h('b', '插件'), '（功能扩展）· ',
          h('b', 'Skill'), '（操作手册）· ', h('b', '专家'), '（预配置角色）'),
        btn: { label: '＋ 接一个连接器', onClick: () => openAddMcp() },
      },
      skills: {
        title: 'Skill',
        line: h('div.pgSub', '技能只在系统提示里报', h('b', '名字和一句话'),
          '，模型觉得用得上才去读全文 —— 所以再多也不占上下文。'),
        btn: { label: '＋ 新建技能', onClick: () => openSkillEditor(null) },
      },
      experts: {
        title: '专家',
        line: h('div.pgSub', '专家决定', h('b', '「谁来做」'),
          '：立场、工具边界、默认模型。绑在当前会话上，新会话沿用上一次选的。'),
      },
    };
    const head = HEADS[active];
    main.appendChild(h('div.pgHead',
      h('div', h('h2', head.title), head.line),
      h('div.grow'),
      head.btn ? h('button.pgBtn', { onclick: head.btn.onClick }, head.btn.label) : null));

    /* ---- tab 条 ---- */
    const tabs = h('div.pgTabs');
    PLUGIN_TABS.forEach((tab) => {
      tabs.appendChild(h('button.pgTab' + (tab.id === active ? '.on' : ''), {
        'data-tab': tab.id,
        title: tab.note,
        onclick: () => {
          /* 点 tab = 跳到那一栏。走 Sidebar.show() 而不是自己重画 ——
             左栏高亮和标题只有一处说了算。 */
          if (typeof Sidebar !== 'undefined') Sidebar.show(tab.sec);
        },
      },
        h('span.pgLbl', tab.label),
        h('span.pgCnt', String(n[tab.id] || 0))));
    });
    main.appendChild(tabs);

    const page = h('div.pgBody');
    main.appendChild(page);
    const cards = h('div.pgSideCards');
    side.appendChild(cards);

    if (active === 'skills') {
      page.appendChild(pgStats([
        [n.skills, '全部'], [n.own, '自己的'], [n.builtin, '内置'],
        [((ctx && ctx.state && ctx.state.skillPick) || []).length, '本轮挂着'],
      ]));
      renderSkillsSide(cards, n);
      return buildSkills(page);
    }

    if (active === 'experts') {
      page.appendChild(pgStats([
        [n.experts, '全部'], [n.experts, '内置'],
        [(ctx && ctx.state && ctx.state.expert) ? 1 : 0, '当前已选'],
      ]));
      renderExpertsSide(cards, n);
      return buildExperts(page);
    }

    page.appendChild(pgStats([
      [n.toolsOn, '已启用'], [n.tools - n.toolsOn, '已禁用'],
      [n.readOnly, '只读'], [n.groups, '工具分组'], [n.mcp, '连接器'],
    ]));
    page.appendChild(pgFilters([['all', '全部'], ['on', '已启用'], ['off', '已禁用']], rerender));
    renderPluginsSide(cards, n);
    await buildTools(page);
    return buildMcp(page);
  }


  // ------------------------------------------------------------- 搜索

  async function buildSearch(body) {
    const input = h('input', {
      placeholder: '搜标题和正文…',
      value: searchState.q || '',
      autocomplete: 'off',
    });
    const results = h('div');
    let timer = null;

    body.appendChild(h('div.sField', input));
    body.appendChild(results);

    const paint = async () => {
      const q = input.value.trim();
      searchState.q = q;
      results.innerHTML = '';
      if (!q) {
        results.appendChild(h('div.sNote', '在全部会话的标题和正文里找。回车不用按，停下就搜。'));
        return;
      }
      const res = await API.search(q);
      if (!res.ok) return results.appendChild(h('div.empty', res.error));
      const hits = res.data || [];
      if (!hits.length) return results.appendChild(h('div.empty', '没有匹配的会话'));
      results.appendChild(h('div.sGroup', hits.length + ' 条命中'));
      hits.forEach((hit) => {
        const node = h('div.sHit',
          h('div', highlight(hit.title || '未命名', q)),
          hit.snippet ? h('div', highlight(hit.snippet, q)) : null,
          h('div.hCid', (hit.where === 'title' ? '标题命中' : '正文命中')
            + ' · ' + relTime(hit.updated || hit.created))
        );
        node.addEventListener('click', () => {
          if (ctx) ctx.openTrace(hit.cid);
        });
        results.appendChild(node);
      });
    };

    input.addEventListener('input', () => {
      clearTimeout(timer);
      timer = setTimeout(paint, 220);
    });
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') { clearTimeout(timer); paint(); }
    });
    if (!input.value) paint();
    else paint();
    setTimeout(() => input.focus(), 30);
  }

  function highlight(text, q) {
    const raw = String(text || '');
    const needle = String(q || '').trim();
    if (!needle) return raw;
    const at = raw.toLowerCase().indexOf(needle.toLowerCase());
    if (at < 0) return raw;
    return h('span', raw.slice(0, at),
      h('em', raw.slice(at, at + needle.length)),
      raw.slice(at + needle.length));
  }

  // ------------------------------------------------------------- 专家

  async function buildExperts(body) {
    const res = await API.experts();
    if (!res.ok) return body.appendChild(h('div.empty', res.error));
    const items = (res.data && res.data.items) || [];
    const active = (ctx && ctx.state && ctx.state.expert) || '';

    /* 「专家」这一格没有开关：换专家是**选一个**，不是开一个。
       所以右边放的是「换上 / 当前」，而不是设计稿里那枚 toggle ——
       一枚点了没反应的开关，比没有这一项更伤。 */
    const grid = h('div.pgCards');
    items.filter((item) => !item.hidden).forEach((item) => {
      const on = item.id === active;
      grid.appendChild(pgCard({
        icon: item.emoji || '🧩',
        name: item.name,
        ver: item.mode ? '默认 ' + modeLabel(item.mode) : (item.builtin ? '内置' : '自制'),
        desc: item.desc || '',
        meta: h('div.pcMeta',
          item.builtin ? pgBadge('内置') : pgBadge('自制'),
          on ? pgBadge('当前', 'new') : null),
        right: h('button.pgBtn.sm' + (on ? '.on' : ''), {
          onclick: () => applyExpert(on ? '' : item.id),
        }, on ? '当前' : '换上'),
      }));
    });
    body.appendChild(pgGroup('全部专家', items.filter((one) => !one.hidden).length, false, grid));

    body.appendChild(pgGroup('管理', 0, true, h('div.pgRow2',
      h('button.pgBtn', { onclick: () => openExpertEditor({}) }, '＋ 新建专家'),
      h('button.pgBtn', { onclick: () => applyExpert('') }, '不用专家')
    )));
  }

  function modeLabel(mode) {
    return { craft: '执行', plan: '规划', ask: '问答', teach: '带练' }[mode] || mode;
  }

  async function applyExpert(expertId) {
    if (!ctx) return;
    const state = ctx.state;
    state.expert = expertId || '';
    if (!state.cid) {
      toast(expertId ? '下个会话用这个专家' : '下个会话不用专家', 'good');
    } else {
      const res = await API.setExpert(state.cid, state.expert, true);
      if (!res.ok) return toast(res.error, 'bad');
      if (res.data) {
        state.mode = res.data.mode;
        state.permission = res.data.permission;
        ctx.renderModes();
      }
      toast(expertId ? '这个会话换成该专家' : '已取消专家', 'good');
    }
    if (ctx.refreshSidebar) ctx.refreshSidebar();
  }

  function openExpertEditor(item) {
    const name = h('input', { value: item.name || '', placeholder: '例如：代码审阅' });
    const emoji = h('input', { value: item.emoji || '🧩', placeholder: '一个表情' });
    const desc = h('input', { value: item.desc || '', placeholder: '一句话说明它怎么工作' });
    const mode = h('select',
      h('option', { value: '' }, '跟随当前模式'),
      ...['craft', 'plan', 'ask', 'teach'].map((id) =>
        h('option', { value: id, selected: item.mode === id }, modeLabel(id)))
    );
    const prompt = h('textarea', { rows: 8, placeholder: '这个身份怎么做事：先问什么、按什么顺序、什么算做完。' });
    prompt.value = item.prompt || '';

    Modal.open({
      title: item.id ? '改专家' : '新建专家',
      subtitle: '方法论是专家的本体 —— 只填名字不填做法，它就只是个称呼',
      body: h('div',
        h('div.sRow',
          h('div.sField', { style: { width: '64px' } }, h('label', '图标'), emoji),
          h('div.sField', { style: { flex: '1 1 auto' } }, h('label', '名字'), name)
        ),
        h('div.sField', h('label', '一句话'), desc),
        h('div.sField', h('label', '默认模式'), mode),
        h('div.sField', h('label', '方法论'), prompt),
        h('div.sNote', '自动注入系统提示。写清「先做什么、不做什么」，比写「你是一个资深专家」有用得多。')
      ),
      actions: [
        { label: '取消', onClick: () => Modal.close() },
        {
          label: '保存', kind: 'primary',
          onClick: async () => {
            const res = await API.saveExpert({
              id: item.id || '', name: name.value.trim(), emoji: emoji.value.trim(),
              desc: desc.value.trim(), mode: mode.value || null, prompt: prompt.value.trim(),
            });
            if (!res.ok) return toast(res.error, 'bad');
            if (res.data && res.data.ok === false) return toast(res.data.error, 'bad');
            Modal.close();
            toast('已保存', 'good');
            if (ctx.refreshSidebar) ctx.refreshSidebar();
          },
        },
      ],
    });
  }

  // ------------------------------------------------------------- 检查点

  // ------------------------------------------------------------- 记忆

  async function buildMemory(body) {
    /* 项目记忆必须跟着**当前会话的工作区**走。用全局的「上次那个目录」，
       会把 A 项目的约定显示在 B 项目的会话里，然后被当成真的注入提示词。 */
    const activeProject = (ctx && ctx.state && ctx.state.project) || '';
    const res = await API.memory(activeProject);
    if (!res.ok) return body.appendChild(h('div.empty', res.error));
    const items = (res.data && res.data.items) || [];
    const project = (res.data && res.data.project) || '';
    const selected = items.find((item) => item.id === memoryState.id) || items[0];

    body.appendChild(h('div.sNote',
      '三层：', h('b', '项目'), '只对这个目录生效，', h('b', '偏好'), '换项目也遵守，',
      h('b', '每日'), '是流水。注入提示词的只有项目记忆（最近 12 条）和偏好。'));

    /* 这一行是还债：以前这三层全靠模型主动调工具来写，而那个工具还要点确认，
       结果目录一直是空的。现在每轮结束自动记 —— 但用户得知道「它自己在记」，
       否则「记忆里怎么多了我没写的东西」会变成另一个疑问。 */
    const level = (ctx && ctx.state && ctx.state.boot && ctx.state.boot.settings
      && ctx.state.boot.settings.auto_memory) || 'smart';
    const levelText = level === 'off' ? '关闭（只有模型主动调工具时才记）'
      : level === 'daily' ? '每日记录 · 每轮自动记一笔'
        : '每日记录 + 项目记忆 · 动了文件还会让模型抽结论';
    body.appendChild(h('div.sNote', { style: { color: 'var(--faint)' } },
      h('b', '自动记忆：'), levelText, '。要改在「设置」里。'));

    if (!activeProject) {
      body.appendChild(h('div.sNote', { style: { color: 'var(--notice-ink)' } },
        '当前会话还没有工作区，所以「项目记忆」先记在「未指定」这一格。'));
    }

    items.forEach((item) => {
      const on = selected && item.id === selected.id;
      const kindTag = item.kind === 'daily' ? item.name
        : item.kind === 'pref' ? '全局' : '本项目';
      const node = h('div.sItem' + (on ? '.on' : '') + (item.exists ? '' : '.dim'),
        h('div.sT', item.label, h('span.pill', kindTag)),
        h('div.sM',
          h('span', item.lines ? item.lines + ' 条' : '还没有内容'),
          item.updated ? h('span', relTime(item.updated)) : null
        )
      );
      node.addEventListener('click', () => {
        memoryState.id = item.id;
        if (ctx.refreshSidebar) ctx.refreshSidebar();
      });
      body.appendChild(node);
    });

    if (!selected) return;

    const area = h('textarea.memText', { spellcheck: 'false' });
    const loadOne = async () => {
      area.value = '读取中…';
      const one = await API.memoryRead(selected.id, project);
      area.value = one.ok ? (one.data.text || '') : ('读不出来：' + (one.error || ''));
    };
    loadOne();

    const status = h('span.sNote', '');
    body.appendChild(h('div.sGroup', selected.label + ' · ' + selected.name));
    body.appendChild(area);
    body.appendChild(h('div.memFoot',
      status,
      h('div.grow'),
      h('button.sBtn', {
        onclick: async () => {
          const res2 = await API.memoryWrite({
            item: selected.id, text: area.value, mode: 'replace', project,
          });
          if (!res2.ok || (res2.data && res2.data.ok === false)) {
            return toast((res2.data && res2.data.error) || res2.error, 'bad');
          }
          status.textContent = '已保存';
          toast('已保存', 'good');
          if (ctx.refreshSidebar) setTimeout(() => ctx.refreshSidebar(), 400);
        },
      }, '保存'),
      h('button.sBtn', {
        onclick: async () => {
          const line = await promptLine('加一条记忆');
          if (!line) return;
          const res2 = await API.memoryWrite({ item: selected.id, text: line, project });
          if (!res2.ok || (res2.data && res2.data.ok === false)) {
            return toast((res2.data && res2.data.error) || res2.error, 'bad');
          }
          toast('已记下', 'good');
          loadOne();
          if (ctx.refreshSidebar) setTimeout(() => ctx.refreshSidebar(), 400);
        },
      }, '加一条'),
      selected.kind === 'daily' ? h('button.sBtn.danger', {
        onclick: async () => {
          const res2 = await API.memoryDelete(selected.id, project);
          if (!res2.ok || (res2.data && res2.data.ok === false)) {
            return toast((res2.data && res2.data.error) || res2.error, 'bad');
          }
          toast('已删除', 'good');
          memoryState.id = '';
          if (ctx.refreshSidebar) ctx.refreshSidebar();
        },
      }, '删除') : null
    ));
    body.appendChild(h('div.sNote.path', selected.path || ''));
  }

  function promptLine(title) {
    return new Promise((resolve) => {
      const input = h('textarea', { rows: 3, placeholder: '一句话结论，例如：测试命令是 pytest -q' });
      Modal.open({
        title,
        subtitle: '会自动加上时间戳。只记长期有效的约定，别记中间结果。',
        body: h('div.sField', input),
        actions: [
          { label: '算了', onClick: () => { Modal.close(); resolve(''); } },
          {
            label: '记下', kind: 'primary',
            onClick: () => { const text = input.value.trim(); Modal.close(); resolve(text); },
          },
        ],
      });
      setTimeout(() => input.focus(), 40);
    });
  }

  // ------------------------------------------------------------- 工具

  /* 一枚工具开关。抽出来是因为「先把节点抓出来再 await」这条不能写错 ——
     `event.currentTarget` 只在派发期间有值，`await` 一过就置为 null。
     写成 await 之后再读它，就是「点一下开关，整个界面变成一张报错页」。 */
  function toolToggle(tool) {
    const button = h('button.toggle' + (tool.enabled ? '.on' : ''), {
      title: tool.enabled ? '点击关闭' : '点击开启',
    });
    button.addEventListener('click', async () => {
      const next = !button.classList.contains('on');
      button.disabled = true;
      try {
        const res = await API.toggleTool(tool.name, next);
        if (!res.ok) return toast(res.error, 'bad');
        button.classList.toggle('on', next);
        button.title = next ? '点击关闭' : '点击开启';
        /* 卡片上的「已启用 / 已关」也是状态的一部分，得跟着换 ——
           只翻开关不翻标签，同一张卡上会同时写着「已关」和亮着的开关。 */
        const box = button.closest('.pgCard');
        const flag = box && box.querySelector('.badge.state');
        if (flag) {
          flag.className = 'badge state' + (next ? ' new' : ' off');
          flag.textContent = next ? '已启用' : '已关';
        }
        if (ctx) ctx.refreshTools();
      } finally {
        button.disabled = false;
      }
    });
    return button;
  }

  function toolCard(tool) {
    const group = tool.group || '其他';
    return pgCard({
      tool: tool.name,
      icon: PG_ICON[group] || '◈',
      name: tool.name,
      ver: group,
      desc: tool.description || '',
      meta: h('div.pcMeta',
        pgBadge('官方'),
        tool.read_only ? pgBadge('只读') : null,
        tool.default_off ? pgBadge('默认关') : null,
        h('span.badge.state' + (tool.enabled ? '.new' : '.off'), tool.enabled ? '已启用' : '已关')),
      right: toolToggle(tool),
    });
  }

  async function buildTools(body) {
    const result = await API.tools();
    if (!result.ok) return body.appendChild(h('div.empty', result.error));
    const list = result.data || [];

    /* 按「干什么」分组，而不是按「只读/会改」分。
       五十多个工具一屏铺下来，用户找的是「我要它能复制文件」，
       不是「我要一个非只读工具」—— 分组要对得上脑子里的问法。
       表里没有的组不会被丢掉，会顺到末尾（折叠着），加组不用改这里也能显示。 */
    const order = ['文件', '执行', '数据', '联网', '系统', '时间',
      '桌面', '记忆', '清理', '协作', '其他'];
    const buckets = new Map();
    list.forEach((tool) => {
      const key = tool.group || '其他';
      if (!buckets.has(key)) buckets.set(key, []);
      buckets.get(key).push(tool);
    });

    /* 只默认展开第一组。五十多个工具全铺开要滚很久，而「文件」之外的那几组
       用户心里本来就有数在哪一块，折着反而更好找。 */
    let seen = 0;
    const emit = (name, items) => {
      const shown = pgFilter === 'all' ? items
        : items.filter((tool) => (pgFilter === 'on' ? tool.enabled : !tool.enabled));
      if (!shown.length) return;
      const grid = h('div.pgCards');
      pgSortItems(shown).forEach((tool) => grid.appendChild(toolCard(tool)));
      body.appendChild(pgGroup(name, items.length, seen >= 1, grid));
      seen += 1;
    };
    order.forEach((name) => {
      const items = buckets.get(name);
      if (!items || !items.length) return;
      buckets.delete(name);
      emit(name, items);
    });
    buckets.forEach((items, name) => emit(name, items));

    /* 子代理也用卡片：它们本来就是「一组只读工具」，做成列表条会和上面
       五十张卡明显不是一套东西。 */
    const subs = ((ctx && ctx.state && ctx.state.boot && ctx.state.boot.subagents) || []);
    const grid = h('div.pgCards');
    subs.forEach((agent) => grid.appendChild(pgCard({
      icon: '⬡',
      name: agent.label,
      ver: agent.role,
      desc: agent.why || '',
      meta: h('div.pcMeta', pgBadge('子代理'), pgBadge('只读'), pgBadge('内置')),
    })));
    grid.appendChild(pgCard({
      icon: '⫶',
      name: '并行派发',
      ver: '最多 4 个',
      desc: '一次派 4 个互不相干的调查，并行跑完一起回来。用时是最慢那个，不是加起来。',
      meta: h('div.pcMeta', pgBadge('子代理'), pgBadge('只读'), pgBadge('内置')),
    }));
    body.appendChild(pgGroup('子代理', subs.length + 1, true, grid));
  }

  // ------------------------------------------------------------- 技能

  /* 一枚技能开关。和工具那枚是同一个零件、同一个尺寸 ——
     用户在这两页看到的是同一种「开 / 关」，不该长得不一样。 */
  function skillToggle(skill) {
    const on = skill.enabled !== false;
    const button = h('button.toggle' + (on ? '.on' : ''), {
      title: on ? '点一下停用它' : '点一下启用它',
    });
    button.addEventListener('click', async () => {
      /* 先抓节点再 await：currentTarget 只在派发期间有值，
         await 之后读它就是「点一下开关，整页变成一张报错」。 */
      const next = !button.classList.contains('on');
      button.disabled = true;
      try {
        const res = await API.toggleSkill(skill.name, next);
        if (!res.ok) return toast(res.error, 'bad');
        button.classList.toggle('on', next);
        button.title = next ? '点一下停用它' : '点一下启用它';
        const card = button.closest('.pgCard');
        if (card) card.classList.toggle('off', !next);
        const flag = card && card.querySelector('.badge.state');
        if (flag) {
          flag.className = 'badge state' + (next ? ' new' : ' off');
          flag.textContent = next ? '已启用' : '已关';
        }
        /* 用不用和开不开是两件事：关掉之后这一轮也别再挂着它 ——
           不然会出现「系统提示里没有它、这一轮却还带着它的正文」这种自相矛盾。 */
        if (!next && ctx && ctx.dropSkill) ctx.dropSkill(skill.name);
        /* 重画一次：上面那条「N 个已关 · 全部打开」只在真有被关掉的时候出现，
           不重画的话它会一直不出现，用户关完还得手动切一下页才看得到。 */
        refresh();
        toast(next
          ? '「' + skill.name + '」已启用，下一轮系统提示里就有它'
          : '「' + skill.name + '」已停用（正文还在磁盘上）', 'good');
      } finally {
        button.disabled = false;
      }
    });
    return button;
  }

  async function buildSkills(body) {
    const result = await API.skills();
    if (!result.ok) return body.appendChild(h('div.empty', result.error));
    const items = result.data || [];

    if (!items.length) {
      return body.appendChild(h('div.empty',
        '还没有技能。点右上「＋ 新建技能」写一个，或者让 agent 用 skill_create 帮你写。'));
    }

    body.appendChild(pgFilters([['all', '全部'], ['own', '自己的'], ['builtin', '内置']], rerender));

    const offList = items.filter((one) => one.enabled === false);
    if (offList.length) {
      body.appendChild(h('div.pgOffBar',
        h('span', offList.length + ' 个已关 · 它们不进系统提示，也不会出现在 @ 候选里'),
        h('div.grow', { style: { flex: '1 1 auto' } }),
        h('button.pcMini', {
          onclick: async () => {
            const res = await API.enableAllSkills();
            if (!res.ok) return toast(res.error, 'bad');
            toast('全打开了' + (res.data && res.data.opened ? '（' + res.data.opened + ' 个）' : ''), 'good');
            refresh();
          },
        }, '全部打开')));
    }

    const shown = items.filter((skill) => (pgFilter === 'all' ? true
      : (pgFilter === 'own' ? skill.editable : !skill.editable)));
    if (!shown.length) return body.appendChild(h('div.empty', '这一筛没有技能'));

    const grid = h('div.pgCards');
    shown.forEach((skill) => {
      const on = skill.enabled !== false;
      /* 开关放在卡的右边，和工具那页一模一样的位置。
         以前这里没有开关，理由写的是「技能只有用不用，没有开不开」——
         那是把「这一轮用不用」和「以后还能不能用」混成了一件。
         现在两个都在：右边开关管长期，标签行里的「用这个」管这一轮。 */
      const card = pgCard({
        icon: '◍',
        name: skill.name,
        ver: skill.editable ? '自己的' : '内置',
        desc: skill.description || '（没有描述）',
        meta: h('div.pcMeta',
          h('span.badge.state' + (on ? '.new' : '.off'), on ? '已启用' : '已关'),
          pgBadge(skill.chars + ' 字'),
          h('button.pcMini', {
            onclick: async () => {
              const detail = await API.readSkill(skill.name);
              if (!detail.ok) return toast(detail.error, 'bad');
              Modal.open({
                title: skill.name,
                subtitle: skill.path,
                body: h('pre.code', detail.data.text),
                actions: [{ label: '知道了', kind: 'primary', onClick: () => Modal.close() }],
              });
            },
          }, '看全文'),
          h('button.pcMini' + (on ? '' : '.dim'), {
            title: on ? '把这句话带上这个技能发出去' : '它现在是关的。先右边打开，再决定这一轮用不用',
            disabled: !on,
            onclick: () => {
              if (ctx && ctx.pickSkill) ctx.pickSkill(skill.name);
              else if (ctx) ctx.useText('请用 skill_load 载入技能「' + skill.name + '」然后按它说的做：');
            },
          }, '用这个'),
          skill.editable
            ? h('button.pcMini', { onclick: () => editSkill(skill) }, '改')
            : null,
          skill.editable
            ? h('button.pcMini.bad', { onclick: () => deleteSkill(skill) }, '删')
            : null),
        right: skillToggle(skill),
      });
      if (!on) card.classList.add('off');
      grid.appendChild(card);
    });
    body.appendChild(grid);

    body.appendChild(h('div.hint', {
      style: { color: 'var(--faint)', fontSize: '11.5px', marginTop: '10px', lineHeight: '1.7' },
    }, '开关只影响「它还给不给你用」：关掉的技能正文仍然在磁盘上，'
      + '随时能打开。真不想留了再用「删」。'));
  }

  /* 新建 / 改一个技能。
     改的做法是「读全文 → 把 frontmatter 拆掉当正文预填 → 保存时 overwrite」，
     不另做一套 diff 界面：技能就是一份 markdown，看到全文再改最直接。 */
  async function editSkill(skill) {
    const detail = await API.readSkill(skill.name);
    if (!detail.ok) return toast(detail.error, 'bad');
    let body = detail.data.text || '';
    if (body.startsWith('---')) {
      const parts = body.split('---', 2);
      if (parts.length >= 2) body = body.slice(3 + parts[1].length + 3).replace(/^\n+/, '');
    }
    openSkillEditor(skill, body);
  }

  function openSkillEditor(skill, presetBody) {
    const editing = !!skill;
    const name = h('input', {
      value: skill ? skill.name : '',
      placeholder: '例如：pdf-report（同时是文件夹名）',
      disabled: editing,   // 改名等于新建一个，容易把老的留在那儿，干脆不让改
    });
    const desc = h('input', {
      value: skill ? skill.description || '' : '',
      placeholder: '什么情况下该用它',
    });
    const code = h('textarea', {
      rows: 12,
      placeholder: '正文用 Markdown：步骤、命令、判据、踩过的坑。写上「怎么判断做完了」。',
    });
    code.value = presetBody || '';

    Modal.open({
      title: editing ? '改技能：' + skill.name : '新建技能',
      subtitle: '写的是「怎么做」——只写一句话概述，它下次帮不上任何忙',
      body: h('div',
        h('div.sField', h('label', '名字'), name),
        h('div.sField', h('label', '一句话说明'), desc),
        h('div.sField', h('label', '正文'), code),
        h('div.sNote', '存在数据目录的 skills/ 下，升级和卸载都不会丢。'
          + '名字同时是文件夹名，所以建好之后不能改。')
      ),
      actions: [
        { label: '取消', onClick: () => Modal.close() },
        {
          label: '保存', kind: 'primary',
          onClick: async () => {
            const res = await API.createSkill({
              name: name.value.trim(),
              description: desc.value.trim(),
              body: code.value,
              overwrite: editing,
            });
            if (!res.ok) return toast(res.error, 'bad');
            if (res.data && res.data.ok === false) return toast(res.data.error, 'bad');
            Modal.close();
            toast(editing ? '改好了' : '技能建好了，技能栏和下一轮系统提示里都会有它', 'good');
            refresh();
          },
        },
      ],
    });
    setTimeout(() => (editing ? desc : name).focus(), 40);
  }

  function deleteSkill(skill) {
    Modal.open({
      title: '删掉技能「' + skill.name + '」？',
      subtitle: '会进回收站，找得回来',
      body: h('div.sNote', '它只是从技能栏和系统提示里消失，不影响磁盘上任何别的文件。'),
      actions: [
        { label: '算了', onClick: () => Modal.close() },
        {
          label: '删掉', kind: 'danger',
          onClick: async () => {
            const res = await API.deleteSkill(skill.name);
            Modal.close();
            if (!res.ok) return toast(res.error, 'bad');
            toast('删了', 'good');
            refresh();
          },
        },
      ],
    });
  }

  // ------------------------------------------------------------- MCP

  async function buildMcp(body) {
    const result = await API.mcpList();
    const items = result.ok ? result.data || [] : [];

    const grid = h('div.pgCards');
    if (!items.length) {
      grid.appendChild(h('div.pgEmpty', '还没有接入 MCP 服务。下面「添加连接器」接一个。'));
    }

    items.forEach((server) => {
      const how = server.transport === 'http'
        ? server.url
        : (server.command + ' ' + (server.args || []).join(' ')).trim();

      grid.appendChild(pgCard({
        icon: server.transport === 'http' ? '🌐' : '⌁',
        name: server.name,
        ver: server.transport === 'http' ? 'HTTP' : '本地',
        desc: server.error || how || '',
        meta: h('div.pcMeta',
          server.connected
            ? pgBadge(server.tools + ' 个工具', 'new')
            : pgBadge(server.error ? '连不上' : '未连接', 'off'),
          server.connected
            ? h('button.pcMini', {
                onclick: async () => { await API.mcpClose(server.name); refresh(); },
              }, '断开')
            : h('button.pcMini', {
                onclick: async () => {
                  const res = await API.mcpConnect(server.name);
                  if (!res.ok || res.data?.ok === false) {
                    toast('连不上：' + ((res.data && res.data.error) || res.error), 'bad');
                  }
                  refresh();
                  if (ctx) ctx.refreshBootstrap();
                },
              }, '连接'),
          h('button.pcMini.bad', {
            onclick: async () => {
              await API.mcpDelete(server.name);
              refresh();
              if (ctx) ctx.refreshBootstrap();
            },
          }, '删除')),
      }));
    });
    body.appendChild(pgGroup('连接器（MCP）', items.length, items.length > 2, grid));

    const transport = h('select.sel',
      h('option', { value: 'stdio' }, '本地命令（stdio）'),
      h('option', { value: 'http' }, '远程服务（HTTP）')
    );
    const name = h('input.input', { placeholder: '名字，例如 filesystem' });
    const command = h('input.input', { placeholder: '启动命令，例如 npx' });
    const args = h('input.input', { placeholder: '参数，空格分隔，例如 -y @modelcontextprotocol/server-filesystem D:\\data' });
    const url = h('input.input', { placeholder: '例如 https://example.com/mcp' });
    const token = h('input.input', { placeholder: '可选：Authorization 的值，例如 Bearer xxx' });

    const stdioBox = h('div',
      h('div.field', h('label', '命令'), command),
      h('div.field', h('label', '参数'), args,
        h('div.hint', 'MCP 服务是本地的一个子进程，DreamRole 用标准输入输出跟它说话。这里填的就是怎么把它拉起来。')));
    const httpBox = h('div.hidden',
      h('div.field', h('label', '地址'), url,
        h('div.hint', 'Remote MCP 的 Streamable HTTP 入口。响应是 JSON 或 SSE 都能处理。')),
      h('div.field', h('label', '认证头'), token,
        h('div.hint', '有些服务要 token。填了会作为 Authorization 头发过去。')));

    transport.addEventListener('change', () => {
      const http = transport.value === 'http';
      httpBox.classList.toggle('hidden', !http);
      stdioBox.classList.toggle('hidden', http);
    });

    /* 表单收在一个折叠块里：这一页的主语是「已经有什么」，
       一进来就先看到三个输入框，会让人以为自己在填表。 */
    const form = h('div.pgForm',
      h('div.field', h('label', '传输方式'), transport),
      h('div.field', h('label', '名字'), name),
      stdioBox,
      httpBox,
      h('button.pgBtn.pri', {
        onclick: async () => {
          const useHttp = transport.value === 'http';
          if (!name.value.trim()) return toast('起个名字', 'bad');
          if (useHttp && !url.value.trim()) return toast('远程服务要填地址', 'bad');
          if (!useHttp && !command.value.trim()) return toast('本地服务要填启动命令', 'bad');
          const payload = { name: name.value.trim() };
          if (useHttp) {
            payload.url = url.value.trim();
            if (token.value.trim()) payload.headers = { Authorization: token.value.trim() };
          } else {
            payload.command = command.value.trim();
            payload.args = args.value.trim() ? args.value.trim().split(/\s+/) : [];
          }
          const res = await API.mcpAdd(payload);
          if (!res.ok) return toast(res.error || '加不上', 'bad');
          if (res.data && res.data.ok === false) toast('已保存，但没连上：' + res.data.error, 'bad');
          else toast('已接入', 'good');
          refresh();
          if (ctx) ctx.refreshBootstrap();
        },
      }, '添加并连接'));

    const addBox = pgGroup('＋ 添加连接器', 0, true, form);
    body.appendChild(addBox);
    /* 头那颗「接一个连接器」要能落到这里：展开 + 滚过去 + 光标放进名字框。
       一个滚不到、也不聚焦的按钮，点完给人的感觉是「它没反应」。 */
    pgAddMcp = () => {
      addBox.classList.remove('closed');
      const arrow = addBox.querySelector('.garrow');
      if (arrow) arrow.textContent = '▾';
      addBox.scrollIntoView({ block: 'center', behavior: 'smooth' });
      setTimeout(() => name.focus(), 220);
    };
  }

  // ------------------------------------------------------------- 定时

  /* 自动化 = 什么时候跑 + 让它做什么。
   *
   * 设计稿那页有四类触发（按时间 / 按事件 / 目标循环 / 手动）。这一版只有第一类
   * 的后端 —— 调度器本来就是按 daily / weekly / interval 排的。事件触发要挂文件
   * 监听和 webhook 端口，目标循环要一套「达成了没有」的判定，那都是新机制，
   * 不是多三个选项框。
   *
   * 所以这里只画真的能跑的东西。**不摆假开关**：一个点了没反应的「按事件触发」，
   * 比没有这一项更伤 —— 用户会从这一处开始不信任整页。
   */
  const AUTO_FILTERS = [
    { id: 'all', label: '全部' },
    { id: 'timed', label: '定时' },
    { id: 'interval', label: '间隔' },
    { id: 'paused', label: '已暂停' },
  ];
  let autoFilter = 'all';

  /* 三个 tab（设计稿 automation-page-v1 的头下面那排）：
   *   自动化  —— 有哪些任务、开着没有；
   *   运行记录 —— 它们真的跑过没有、跳过的原因；
   *   模板     —— 从一句「我想要什么」起步，而不是从一张空表单起步。
   *
   * 为什么非要「运行记录」单独一格：任务卡上的「已跑 12 次」只说明发生过，
   * 说不清昨晚那次到底开了会话、还是被「上一次没跑完」挡掉了。那才是要查的事。
   */
  const AUTO_TABS = [
    { id: 'list', label: '自动化', tab: 'schedules' },
    { id: 'runs', label: '运行记录', tab: 'scheduleRuns' },
    { id: 'tpl', label: '模板', tab: 'scheduleTemplates' },
  ];
  let autoTab = 'list';

  /* 模板不是「示例数据」，是四条真的会写进 schedules.json 的起点。
   * 每条都填好了触发方式和提示词 —— 点了之后还要在表单里看一遍、选工作区，
   * 所以它省掉的是「想清楚要写什么」，不是「确认」。 */
  const AUTO_TEMPLATES = [
    {
      id: 'daily-brief',
      name: '每日选题与发布',
      when: '每天 09:00',
      desc: '挑三条值得做的选题，各写一稿，配好图存到 out/ 下。',
      prompt: '看今天的热点，挑三条争议最大、最适合做成短内容的选题。'
        + '每条写一稿 500 字左右，配一张图，分别存到 out/ 下，文件名带上编号。',
      freq: 'daily', time: '09:00',
    },
    {
      id: 'weekly-report',
      name: '周一汇总上周改动',
      when: '每周一 09:30',
      desc: '把上周所有会话里的改动整理成一份周报。',
      prompt: '把上周所有会话里改过的文件整理成一份周报：按项目分组，'
        + '每条写清「改了什么、为什么」，存成 周报.md。',
      freq: 'weekly', weekday: 1, time: '09:30',
    },
    {
      id: 'self-check',
      name: '每两小时自检',
      when: '每 120 分钟',
      desc: '跑一遍自检，失败了把原因总结成一段话。',
      prompt: '跑一遍项目的自检，全部通过就只回一句「通过」；'
        + '有失败就把失败的用例、报错原文、最可能的原因总结成一段话。',
      freq: 'interval', minutes: 120,
    },
    {
      id: 'tidy-notes',
      name: '收件箱整理',
      when: '每天 21:00',
      desc: '把 inbox/ 里还没归档的笔记按主题分好类。',
      prompt: '看一下 inbox/ 里还没归档的笔记，按主题分成几组，'
        + '每组起一个能看懂的名字，写一份 index.md 说明每组是什么，先别删原文件。',
      freq: 'daily', time: '21:00',
    },
  ];

  function scheduleMatches(job, filter) {
    if (filter === 'paused') return !job.enabled;
    if (filter === 'timed') return job.freq === 'daily' || job.freq === 'weekly';
    if (filter === 'interval') return job.freq === 'interval';
    return true;
  }

  /* 三种频率三种图标。它们要能在一列里被认出来 —— 全是同一个表盘的话，
     扫列表时只能靠读「每天 09:00」才知道这一条是什么。 */
  function scheduleGlyph(freq) {
    const paths = {
      daily: '<path d="M12 7v5l3 2" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><circle cx="12" cy="12" r="8.4" fill="none" stroke="currentColor" stroke-width="1.7"/>',
      weekly: '<rect x="4" y="5.5" width="16" height="14.5" rx="2" fill="none" stroke="currentColor" stroke-width="1.7"/><path d="M8 3v4M16 3v4M4 10h16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/>',
      interval: '<path d="M4 12a8 8 0 1 0 2.4-5.7" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><path d="M4 4v4h4" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>',
    };
    return '<svg viewBox="0 0 24 24" width="13" height="13">' + (paths[freq] || paths.daily) + '</svg>';
  }

  /* 「下次触发」这一格只有 ~74px 宽，塞不下「10-25 00:46」。
     今天就写「00:46」，别的日子写「10/25」—— 想看准的，鼠标停上去有完整值。 */
  function nextShort(job) {
    if (!job || !job.next_run) return '—';
    const when = new Date(job.next_run * 1000);
    const pad = (n) => String(n).padStart(2, '0');
    if (when.toDateString() === new Date().toDateString()) {
      return pad(when.getHours()) + ':' + pad(when.getMinutes());
    }
    return (when.getMonth() + 1) + '/' + when.getDate();
  }

  async function buildSchedules(body) {
    const result = await API.schedules();
    if (!result.ok) return body.appendChild(h('div.empty', result.error || '读不出自动化'));
    const items = result.data || [];
    const runsResult = await API.scheduleRuns(80);
    const runRows = runsResult.ok ? (runsResult.data || []) : [];
    const live = items.filter((job) => job.enabled);
    const soon = live.filter((job) => job.next_run).sort((a, b) => a.next_run - b.next_run)[0];
    const totalRuns = items.reduce((sum, job) => sum + (job.runs || 0), 0);

    /* 版式照 DSH 设计稿 automation-page-v1：头（标题 + 说明 + 模板/新建）、
       三个 tab、四个 56px 指标格、筛选胶囊、一行一条的任务卡。 */
    body.appendChild(h('div.pgHead',
      h('div',
        h('div.pgTitle', '自动化'),
        h('div.pgSub', '到点它就在这个工作区里自己跑一轮，产出和普通对话一样留在会话里。')),
      h('div.grow'),
      h('button.pgBtn', {
        // 「从模板」不是新功能，只是换一个起点：先把提示词写出来，再让你改
        onclick: () => { autoTab = 'tpl'; rerender(); },
      }, '从模板'),
      h('button.pgBtn.pri', { onclick: () => openScheduleEditor(null) }, '＋ 新建自动化')));

    const tabs = h('div.pgTabs');
    AUTO_TABS.forEach((one) => {
      const count = one.id === 'list' ? items.length
        : one.id === 'runs' ? runRows.length : AUTO_TEMPLATES.length;
      tabs.appendChild(h('button.pgTab' + (autoTab === one.id ? '.on' : ''), {
        'data-tab': one.id,
        onclick: () => { autoTab = one.id; rerender(); },
      }, h('span', one.label), h('span.pgCnt', String(count))));
    });
    body.appendChild(tabs);

    if (autoTab === 'runs') return buildScheduleRuns(body, runRows, items);
    if (autoTab === 'tpl') return buildScheduleTemplates(body);

    body.appendChild(pgStats([
      [live.length + ' / ' + items.length, '启用中'],
      [String(totalRuns), '已跑总次数'],
      [nextShort(soon), '下次触发'],
      [String(items.length - live.length), '已暂停', items.length - live.length > 0],
    ]));

    /* 筛选条。计数写在标签上 —— 切到「已暂停」时先看见有几条，
       再决定要不要点；点进去才发现是空的，是白跑一趟。
       这里没复用 pgFilters：那个还带一个「按启用状态排序」，是插件页的排法，
       两页共用一份状态的话，在插件页点了排序，自动化页的胶囊也会跟着变。 */
    const chips = h('div.pgFilters');
    AUTO_FILTERS.forEach((one) => {
      const count = items.filter((job) => scheduleMatches(job, one.id)).length;
      chips.appendChild(h('button.pgChip' + (autoFilter === one.id ? '.on' : ''), {
        onclick: () => { autoFilter = one.id; rerender(); },
      }, h('span', one.label), h('span.pgCnt', String(count))));
    });
    body.appendChild(chips);

    const shown = items.filter((job) => scheduleMatches(job, autoFilter));
    if (!shown.length) {
      body.appendChild(h('div.pgEmpty', items.length
        ? '这一类里还没有任务。'
        : '还没有自动化。上面点「新建自动化」，或者让 agent 用 schedule 工具建一条。'));
      return;
    }

    const list = h('div.auList');
    shown.forEach((job) => list.appendChild(scheduleRow(job)));
    body.appendChild(list);
  }

  /* --- 运行记录 ---------------------------------------------------------
   *
   * 一行 = 一次真的动作。`state` 只有三种，颜色也只给三种：
   *   started  蓝   —— 开了会话，点得进去
   *   skipped  灰   —— 没开，右边写着为什么（「上一次还没跑完」「没有工作区」）
   *   failed   红   —— 开了但立刻炸了
   *
   * 不显示「耗时 / token」：那要等会话真跑完才知道，而现在这一条只记到
   * 「开了一轮」。摆一个恒为空的格子，比不摆更让人怀疑别处也是假的。
   */
  function buildScheduleRuns(body, runRows, items) {
    const alive = new Set(items.map((job) => job.id));
    if (!runRows.length) {
      body.appendChild(h('div.pgEmpty',
        '还没有运行记录。定时任务到点跑一次、或者你在任务卡上点「跑一次」，这里就会多一行。'));
      return;
    }
    const list = h('div.auRuns');
    runRows.forEach((row) => {
      const when = new Date((row.at || 0) * 1000);
      const pad = (n) => String(n).padStart(2, '0');
      const stamp = (when.getMonth() + 1) + '-' + pad(when.getDate())
        + ' ' + pad(when.getHours()) + ':' + pad(when.getMinutes());
      const label = { started: '开了会话', skipped: '跳过', failed: '启动失败' }[row.state]
        || row.state;
      const openable = row.state === 'started' && row.cid;
      list.appendChild(h('div.auRun' + (openable ? '.clickable' : ''),
        h('span.auRunWhen', stamp),
        h('span.auRunName', { title: row.title }, row.title || '未命名'),
        h('span.auRunWhy', row.why || '定时'),
        h('span.auRunState.' + row.state, label),
        h('span.auRunNote', { title: row.note }, row.note || ''),
        openable
          ? h('button.mini', { onclick: () => ctx && ctx.openTrace(row.cid) }, '看会话')
          : (alive.has(row.job) ? h('span.auRunGone', '') : h('span.auRunGone', '任务已删'))));
    });
    body.appendChild(list);
  }

  /* --- 模板 -------------------------------------------------------------
   *
   * 一张卡一个起点。点「用这个模板」= 打开新建表单并填好 —— 工作区还得你自己选，
   * 因为那是唯一一件它猜不出的事（猜错就是让它半夜在别人的目录里写东西）。
   */
  function buildScheduleTemplates(body) {
    const grid = h('div.auTpls');
    AUTO_TEMPLATES.forEach((tpl) => {
      grid.appendChild(h('div.auTpl',
        h('div.auTplTop',
          h('span.auTplName', tpl.name),
          h('span.auWhen', tpl.when)),
        h('div.auTplDesc', tpl.desc),
        h('div.auTplPrompt', { title: tpl.prompt }, tpl.prompt),
        h('div.auTplFoot',
          h('button.pgBtn.sm', {
            onclick: () => openScheduleEditor({
              title: tpl.name,
              prompt: tpl.prompt,
              freq: tpl.freq,
              time: tpl.time || '09:00',
              weekday: tpl.weekday || 1,
              minutes: tpl.minutes || 60,
              enabled: true,
              // 还没存盘的东西没有 id —— 编辑器按「有没有 id」判断是新建还是改
              preset: true,
            }),
          }, '用这个模板'))));
    });
    body.appendChild(grid);
  }

  /* 一条自动化：左边一枚 34px 的频率图标，中间「名字 + 频率 + 状态 + 一句话 + 三个数」，
     右边开关和三个动作。设计稿把动作收在右边一列，是因为它们都作用于这一条 ——
     铺到下面一行会让人先想「这行按钮属于谁」。 */
  function scheduleRow(job) {
    const toggle = async () => {
      await API.patchSchedule(job.id, { enabled: !job.enabled });
      rerender();
    };
    const remove = () => Modal.open({
      title: '删掉这条自动化？',
      subtitle: '只删这个定时任务，它以前跑出来的会话和产物都还在。',
      body: h('div.sNote', job.title || job.prompt),
      actions: [
        { label: '不删', onClick: () => Modal.close() },
        {
          label: '删掉', kind: 'primary',
          onClick: async () => {
            Modal.close();
            await API.delSchedule(job.id);
            toast('已删除', 'good');
            rerender();
          },
        },
      ],
    });

    return h('div.auRow' + (job.enabled ? '' : '.off'),
      h('span.auTrg.' + (job.freq === 'interval' ? 'event' : 'time'), { html: scheduleGlyph(job.freq) }),
      h('div.auMain',
        h('div.auTop',
          h('span.auName', job.title || '未命名'),
          h('span.auWhen', job.freq_label || job.freq),
          h('span.auState.' + (job.enabled ? 'ok' : 'pause'), job.enabled ? '启用中' : '已暂停')),
        h('div.auDesc', { title: job.prompt }, job.prompt),
        h('div.auMeta',
          h('span', '下次 ', h('b', job.next_label || '—')),
          h('span', '已跑 ', h('b', String(job.runs || 0)), ' 次'),
          job.project ? h('span', '工作区 ', h('b', pathTail(job.project, 1))) : null)),
      h('div.auActs',
        job.last_cid ? h('button.mini', {
          title: '打开它上一次跑出来的那条会话',
          onclick: () => ctx && ctx.openTrace(job.last_cid),
        }, '上次结果') : null,
        h('button.mini', { onclick: toggle }, job.enabled ? '暂停' : '启用'),
        h('button.mini.danger', { onclick: remove }, '删除'),
        h('button.toggle' + (job.enabled ? '.on' : ''), {
          title: job.enabled ? '点一下暂停这条' : '点一下重新启用',
          onclick: toggle,
        })));
  }

  /* 新建 / 编辑自动化（设计稿 automation-editor-v1 的 B 段）。
   *
   * 三段式：① 什么时候跑 ② 让谁做什么 ③ 无人值守的安全与预算。
   * 为什么要分段：这条表单填错任何一格，后果都是「半夜自己跑，跑错地方」，
   * 而那时候没人在旁边。分段 + 每段一句话，是让人在点保存之前
   * 还愿意把三件事各看一遍的唯一办法。
   *
   * 表单里每一格都真的会生效：触发方式、工作区、内核、权限、技能，
   * 五个字段一路传到 _fire 里那次 session.create / start。
   * 摆一格不生效的输入框，比不摆更糟 —— 用户会以为自己设好了。
   */
  function openScheduleEditor(job, seedPrompt) {
    const form = { skills: (job && Array.isArray(job.skills)) ? job.skills.slice() : [] };

    const title = h('input.input', {
      placeholder: '任务名，可留空（默认取第一句话）', value: (job && job.title) || '',
    });

    /* —— ① 什么时候跑 —— */
    const freq = (job && job.freq) || 'daily';
    const triggers = [
      ['daily', '按时间', '每天或每周的某个点'],
      ['interval', '按间隔', '从现在起每隔一段'],
      ['manual', '手动', '不排班，点一次跑一次'],
    ];
    const segs = h('div.edSegs');
    triggers.forEach(([key, name, tip]) => {
      segs.appendChild(h('button.edSeg' + (key === freq ? '.on' : ''), {
        title: tip,
        onclick: (event) => {
          [...segs.children].forEach((node) => node.classList.remove('on'));
          event.currentTarget.classList.add('on');
          segs.dataset.value = key;
          sync();
        },
      }, name));
    });
    segs.dataset.value = freq;

    const cadence = h('select.select',
      h('option', { value: 'daily' }, '每天'),
      h('option', { value: 'weekly' }, '每周'));
    const time = h('input.input', { value: (job && job.time) || '09:00', placeholder: 'HH:MM' });
    const weekday = h('select.select',
      ...['一', '二', '三', '四', '五', '六', '日'].map((label, index) =>
        h('option', { value: String(index + 1) }, '周' + label)));
    if (job && job.weekday) weekday.value = String(job.weekday);
    const minutes = h('input.input', {
      value: String((job && job.minutes) || 60), placeholder: '分钟', type: 'number', min: '5',
    });

    const timeSlot = h('div.edSlot', cadence);
    const hmSlot = h('div.edSlot', time);
    const weekSlot = h('div.edSlot', weekday);
    const minSlot = h('div.edSlot', minutes);
    const cronRow = h('div.row3', timeSlot, hmSlot, weekSlot, minSlot);
    const cronHint = h('div.edHint');

    /* —— ② 让谁做什么 —— */
    const engine = h('select.select', h('option', { value: '' }, '跟随默认内核'));
    (state.engines || []).forEach((row) => {
      engine.appendChild(h('option', { value: row.id }, row.label + (row.ready ? '' : '（不可用）')));
    });
    engine.value = (job && job.engine) || '';

    const project = h('input.input', {
      value: (job && job.project) || (ctx && ctx.state.project) || '',
      placeholder: '工作目录，必须填 —— 没有目录它会跳过这一轮',
    });

    const skillChips = h('div.edChips');
    const skillPicker = h('div.edPick.hidden');
    let skillList = null;

    const paintChips = () => {
      skillChips.innerHTML = '';
      form.skills.forEach((name) => {
        skillChips.appendChild(h('span.sc.sk', { title: '点 × 拿掉' },
          '♢ ' + name,
          h('i', {
            onclick: () => {
              form.skills = form.skills.filter((one) => one !== name);
              paintChips();
              if (!skillPicker.classList.contains('hidden')) paintPicker();
            },
          }, '×')));
      });
      skillChips.appendChild(h('span.sc.add', {
        onclick: async (event) => {
          event.stopPropagation();
          skillPicker.classList.toggle('hidden');
          if (skillPicker.classList.contains('hidden')) return;
          if (!skillList) {
            const res = await API.skills();
            skillList = (res.ok && (res.data || [])) || [];
            paintPicker();
          }
        },
      }, '＋ 挂技能'));
    };

    function paintPicker() {
      skillPicker.innerHTML = '';
      if (!skillList || !skillList.length) {
        skillPicker.appendChild(h('div.edPickEmpty', '还没有技能。左栏「技能」里能新建。'));
        return;
      }
      /* 关掉的技能不列。定时任务是「没人看着也要跑」的那种，
         给它挂一个已经停用的技能，到点只会得到一句「已关闭」。 */
      const usable = skillList.filter((item) => item.enabled !== false);
      if (!usable.length) {
        skillPicker.appendChild(h('div.edPickEmpty',
          '技能都被关掉了。左栏「技能」里把要用的那个打开。'));
        return;
      }
      usable.forEach((item) => {
        const on = form.skills.includes(item.name);
        skillPicker.appendChild(h('div.edPickItem' + (on ? '.on' : ''), {
          title: item.description || item.name,
          onclick: (event) => {
            event.stopPropagation();
            if (on) form.skills = form.skills.filter((one) => one !== item.name);
            else if (form.skills.length >= 12) return toast('最多挂 12 个', 'bad');
            else form.skills.push(item.name);
            paintChips();
            paintPicker();
          },
        },
          h('span.tick', on ? '✓' : ''),
          h('span.nm', item.name),
          h('span.ds', item.description || '')));
      });
    }

    const prompt = h('textarea.input.edTa', {
      placeholder: '到点要它做什么？写清楚「做什么、做到什么程度算完成」',
    });
    if (job && job.prompt) prompt.value = job.prompt;
    /* 从「＋ 菜单 → 存为自动化…」过来时，输入框里那句话原样带过来。
       带过来的不只是省一次输入：它证明「这句话值得定时跑」这个判断，
       是用户在输入框里就做出来的。 */
    else if (seedPrompt) prompt.value = String(seedPrompt);

    /* —— ③ 安全与预算 —— */
    const levels = [
      ['readonly', '只读', '只能看，不能改任何东西'],
      ['standard', '工作区内改', '写文件要点头，等不到就停在那儿'],
      ['full', '完全权限', '不问，直接做（仍然记账）'],
    ];
    const perms = h('div.edSegs');
    const curPerm = (job && job.permission) || 'standard';
    levels.forEach(([key, name, tip]) => {
      perms.appendChild(h('button.edSeg', {
        title: tip,
        onclick: (event) => {
          [...perms.children].forEach((node) => node.classList.remove('on'));
          event.currentTarget.classList.add('on');
          perms.dataset.value = key;
        },
      }, name));
      if (key === curPerm) perms.lastChild.classList.add('on');
    });
    perms.dataset.value = curPerm;

    /* 三段的联动：换触发方式，下面那行输入框跟着换长相 ——
       「每天 09:00」和「每 60 分钟」不是同一组格子，硬塞在一起只会让人填错。 */
    function sync() {
      const mode = segs.dataset.value;
      const daily = mode === 'daily';
      /* 三档各长各的样：
         按时间 = 「每天/每周 + 时刻（+ 周几）」，按间隔 = 只有一个分钟格，
         手动 = 一个格子都没有。留一个当前档位用不上的空格子，
         用户就会去填它，然后奇怪为什么没生效。 */
      timeSlot.classList.toggle('hidden', !daily);
      hmSlot.classList.toggle('hidden', !daily);
      weekSlot.classList.toggle('hidden', !(daily && cadence.value === 'weekly'));
      minSlot.classList.toggle('hidden', mode !== 'interval');
      cronHint.textContent = mode === 'manual'
        ? '不排班。它只是一句存下来的话，点「试运行一次」就立刻跑一遍。'
        : (mode === 'interval'
          ? '从现在起算，不是整点对齐。设 60 分钟就是建好之后每 60 分钟一次。'
          : '错过的时间点直接丢掉，只跑从现在起最近的那一次 —— 重启后不会把攒下的班次一次补完。');
    }
    cadence.addEventListener('change', sync);

    /* 点别处收起技能选择。用一次性监听，避免每开一次弹窗就多挂一个。 */
    const outside = (event) => {
      if (skillPicker.classList.contains('hidden')) return;
      if (skillPicker.contains(event.target)) return;
      if (event.target.closest && event.target.closest('.sc.add')) return;
      skillPicker.classList.add('hidden');
    };
    document.addEventListener('click', outside);

    function payload() {
      const mode = segs.dataset.value;
      return {
        title: title.value.trim() || prompt.value.trim().slice(0, 20) || '定时任务',
        prompt: prompt.value.trim(),
        freq: mode === 'daily' ? cadence.value : mode,
        time: time.value.trim() || '09:00',
        weekday: Number(weekday.value) || 1,
        minutes: Math.max(5, Number(minutes.value) || 60),
        project: project.value.trim(),
        engine: engine.value,
        permission: perms.dataset.value,
        skills: form.skills,
        enabled: true,
      };
    }

    async function save(thenRun) {
      const body = payload();
      if (!body.prompt) return toast('得说清楚到点做什么', 'bad');
      if (!body.project) return toast('得给它一个工作目录', 'bad');
      const res = (job && job.id)
        ? await API.patchSchedule(job.id, body)
        : await API.addSchedule(body);
      if (!res.ok) return toast(res.error || '存不上', 'bad');
      const id = (job && job.id) || (res.data && res.data.job && res.data.job.id)
        || (res.data && res.data.id);
      Modal.close();
      document.removeEventListener('click', outside);
      refresh();
      if (thenRun && id) {
        const ran = await API.runSchedule(id);
        toast(ran.ok ? '已经开跑了，去会话里看过程' : (ran.error || '跑不起来'), ran.ok ? 'good' : 'bad');
      } else {
        toast((job && job.id) ? '已保存' : '已创建', 'good');
      }
    }

    paintChips();
    sync();

    Modal.open({
      title: (job && job.id) ? '改这条自动化' : '新建自动化',
      subtitle: '到点它在你指定的工作区里自己跑一轮，产出和普通对话一样留在会话里。',
      width: 660,
      body: h('div.formCol.edForm',
        h('div.field', h('label', '名称'), title),

        h('div.edSec',
          h('div.edH', h('span.edN', '1'), '什么时候跑（触发）'),
          segs,
          cronRow,
          cronHint),

        h('div.edSec',
          h('div.edH', h('span.edN', '2'), '让谁做什么（动作）'),
          h('div.row3', h('div.edSlot', engine), h('div.edSlot', project)),
          skillChips,
          skillPicker,
          prompt),

        h('div.edSec',
          h('div.edH', h('span.edN', '3'), '无人值守的安全与预算'),
          perms,
          h('div.edHint', '到点没人在旁边。这里的档位就是它那一轮的权限 —— '
            + '选「只读」时它写不了文件，选「工作区内改」时它会停下来等人，等不到就停在那儿。'))),

      actions: [
        { label: '取消', onClick: () => { document.removeEventListener('click', outside); Modal.close(); } },
        { label: '试运行一次', onClick: () => save(true) },
        { label: (job && job.id) ? '保存并启用' : '创建并启用', kind: 'primary', onClick: () => save(false) },
      ],
    });
  }

  // ------------------------------------------------------------- 改动

  /* 改动记录**属于某一轮对话**，不是一堆全局的散装文件。
     所以这一栏按对话分区：当前对话的在上面，别的对话的折起来，
     对话已经没了的单独一折、只剩名字。
     为什么要有最后一折：那些记录还在磁盘上，但它们的归属没了 ——
     混在能点的行里，用户一定会点到，然后收到一句「所属对话已删除」。 */
  // ------------------------------------------------------------- 改动（三级下钻）
  //
  // 为什么是三级：退回这个动作的对象从来不是「一条改动记录」，而是
  // 「那个对话里的那个文件的那个阶段」。平铺成一张清单时，这三个问题被压成一行字，
  // 用户得自己在脑子里重建归属 —— 于是就有了「改动和对话没关联上」这个感受。
  //
  // 所以之前那两栏（改动 / 检查点）合成一栏：退回不再是一个独立功能，
  // 它是第三层里的一个动作 —— 你得先站到某个阶段上，才能说要退回哪一个。

  const drill = { level: 1, cid: '', path: '', title: '', name: '', alive: true };
  let changesHost = null;

  async function buildChanges(body) {
    changesHost = body;
    await paintChanges();
  }

  function backTo(level) {
    drill.level = level;
    if (level < 3) { drill.path = ''; drill.name = ''; }
    if (level < 2) { drill.cid = ''; drill.title = ''; }
    paintChanges();
  }

  async function paintChanges() {
    const host = changesHost;
    if (!host) return;
    host.innerHTML = '';
    host.appendChild(crumbBar());
    const box = h('div.drillBody', h('div.empty', '读取中…'));
    host.appendChild(box);
    try {
      if (drill.level === 1) await paintThreads(box);
      else if (drill.level === 2) await paintFiles(box);
      else await paintTimeline(box);
    } catch (err) {
      box.innerHTML = '';
      box.appendChild(h('div.empty', '加载失败：' + err.message));
    }
  }

  /* 面包屑就是「我在哪一层」的全部说明。不用返回键：返回键只告诉你能退，
     不告诉你退到哪；三颗可点的面包屑把整条路径摆在那儿。 */
  function crumbBar() {
    const bar = h('div.crumbs');
    const link = (label, level, title) => {
      const node = h('button.crumb', { title: title || '' }, label);
      node.addEventListener('click', () => backTo(level));
      return node;
    };
    bar.appendChild(link('对话', 1, '所有留下过改动的对话'));
    if (drill.level >= 2) {
      bar.appendChild(h('span.crSep', '›'));
      bar.appendChild(drill.level === 3
        ? link(drill.title || '这条对话', 2, '回到这条对话的产物')
        : h('span.crumbOn', drill.title || '这条对话'));
    }
    if (drill.level === 3) {
      bar.appendChild(h('span.crSep', '›'));
      bar.appendChild(h('span.crumbOn', drill.name || '这个文件'));
    }
    return bar;
  }

  function foldSection(title, collapsed, children) {
    const box = h('div.foldBox' + (collapsed ? '.closed' : ''));
    const head = h('div.foldHead',
      h('span.fhName', title),
      h('span.fhArrow', collapsed ? '▸' : '▾'));
    head.addEventListener('click', () => {
      const closed = box.classList.toggle('closed');
      head.querySelector('.fhArrow').textContent = closed ? '▸' : '▾';
    });
    box.appendChild(head);
    box.appendChild(h('div.foldBody', ...children));
    return box;
  }

  // ---- 第一层：有哪些对话

  async function paintThreads(box) {
    box.innerHTML = '';
    const res = await API.changeThreads();
    if (!res.ok) return box.appendChild(h('div.empty', res.error));
    const items = (res.data && res.data.items) || [];
    const live = (ctx && ctx.state && ctx.state.cid) || '';

    box.appendChild(h('div.hint', {
      style: 'color:var(--faint);font-size:11.5px;line-height:1.65;margin-bottom:10px',
    }, '点开一条对话，看它写出去的文件；再点开文件，就能挑一个阶段退回去。'));

    if (!items.length) {
      return box.appendChild(h('div.empty', '还没有对话改过文件。让它动一下文件，这里就会出现。'));
    }

    const alive = items.filter((item) => item.alive);
    const dead = items.filter((item) => !item.alive);

    alive.forEach((item) => box.appendChild(threadRow(item, item.cid === live)));

    if (dead.length) {
      box.appendChild(foldSection('对话已删除（' + dead.length + '）', true, [
        h('div.hint', {
          style: 'color:var(--faint);font-size:11.5px;line-height:1.65;padding:2px 2px 8px',
        }, '这些记录还能翻看，但不能再退回 —— 对话删了之后，一次改动就失去了'
          + '「是谁为了什么改的」这层意思。'),
        h('div.sRow', { style: 'margin-bottom:6px' },
          h('div.grow'),
          h('button.sBtn.danger', { onclick: purgeDead }, '全清掉')),
        ...dead.map((item) => threadRow(item, false)),
      ]));
    }
  }

  function purgeDead() {
    Modal.open({
      title: '清掉所有失效的改动记录？',
      subtitle: '对话已经删掉的那些记录会被真的删除，删完就找不回来了',
      body: h('div.sNote', '这一步只动账本，不碰磁盘上的文件 —— 你的文件一个都不会少。'),
      actions: [
        { label: '算了', onClick: () => Modal.close() },
        {
          label: '清掉', kind: 'danger',
          onClick: async () => {
            const res = await API.purgeChanges();
            Modal.close();
            if (!res.ok) return toast(res.error, 'bad');
            const data = res.data || {};
            toast('清掉了 ' + ((data.changes || 0) + (data.history || 0)) + ' 条记录', 'good');
            paintChanges();
            if (ctx.refreshSidebar) ctx.refreshSidebar();
          },
        },
      ],
    });
  }

  function threadRow(item, mine) {
    const node = h('div.listItem.clickable' + (item.alive ? '' : '.isDead'),
      h('div.t',
        item.title,
        mine ? h('span.pill.ok', '当前') : null,
        item.archived ? h('span.tag', '已归档') : null,
        item.alive ? null : h('span.tag.bad', '已失效')),
      h('div.d', item.files + ' 个文件 · ' + item.changes + ' 次改动'
        + (item.turns > 1 ? ' · ' + item.turns + ' 轮' : '')),
      h('div.row2',
        h('span.tag', relTime(item.last_ts)),
        item.revertible ? h('span.tag.acc', item.revertible + ' 条可退') : null,
        h('div.grow'),
        h('span.arrow', '›')));
    node.addEventListener('click', () => {
      drill.level = 2;
      drill.cid = item.cid;
      drill.title = item.title;
      drill.alive = item.alive;
      paintChanges();
    });
    return node;
  }

  // ---- 第二层：这条对话有哪些文件产物

  async function paintFiles(box) {
    box.innerHTML = '';
    const res = await API.changeThread(drill.cid);
    if (!res.ok) return box.appendChild(h('div.empty', res.error));
    const data = res.data || {};
    drill.title = data.title || drill.title;
    drill.alive = data.alive !== false;

    const files = data.files || [];
    const turns = data.turns || [];

    box.appendChild(h('div.drillHead',
      h('span', files.length + ' 个文件'),
      h('span.dot', '·'),
      h('span', files.reduce((sum, item) => sum + Number(item.changes || 0), 0) + ' 次改动'),
      drill.alive ? null : h('span.pill.bad', '对话已删除')));

    if (!drill.alive) {
      box.appendChild(h('div.whyLine', '这条对话已经删除，这里只剩记录，不能再退回。'));
    }

    if (!files.length) {
      box.appendChild(h('div.empty', '这条对话还没写下过文件。'));
    } else {
      files.forEach((item) => box.appendChild(fileRow(item)));
    }

    if (turns.length) {
      box.appendChild(foldSection('整轮回退（' + turns.length + ' 轮）', true,
        turns.map((item) => turnRow(item))));
    }
  }

  function fileRow(item) {
    const canOpen = true;
    const tags = [];
    if (item.exists === false) tags.push(h('span.tag.bad', '文件已不在'));
    if (item.drifted) tags.push(h('span.tag.warn', '被改过'));
    if (item.status === 'reverted') tags.push(h('span.tag', '已退回过'));

    const why = item.reason && !item.revertible
      ? h('div.whyLine', item.reason)
      : null;

    const node = h('div.listItem.clickable' + (canOpen ? '' : '.isDead'), {
      title: item.path,
    },
      h('div.t', item.name, ...tags),
      h('div.d.dirLine', item.dir || item.path),
      h('div.row2',
        h('span.tag', item.stages + ' 个阶段'),
        h('span.tag', relTime(item.last_ts)),
        h('div.grow'),
        h('span.arrow', '›')),
      why);
    node.addEventListener('click', () => {
      drill.level = 3;
      drill.path = item.path;
      drill.name = item.name;
      paintChanges();
    });
    return node;
  }

  function turnRow(item) {
    const alive = item.alive !== false && drill.alive;
    return h('div.cpItem' + (alive ? '' : '.isDead'),
      h('div.cpT',
        h('span', '第 ' + (item.turn || 0) + ' 轮'),
        h('span.pill', item.count + ' 个文件'),
        item.stacked ? h('span.pill.warn', '之后又改过') : null),
      h('div.cpF', ...(item.files || []).slice(0, 3).map((file) =>
        h('span', (file.existed ? '✎ ' : '＋ ') + file.name)),
        (item.files || []).length > 3 ? h('span', '…还有 ' + (item.files.length - 3) + ' 个') : null),
      h('div.sRow',
        h('div.grow'),
        h('button.sBtn.danger', {
          disabled: !alive,
          title: alive ? '' : '所属对话已删除',
          onclick: () => confirmTurnRestore(item),
        }, '退回这一轮之前')));
  }

  function confirmTurnRestore(item) {
    const names = (item.files || []).slice(0, 4).map((f) => f.name).join('、');
    Modal.open({
      title: '退回到第 ' + (item.turn || 0) + ' 轮之前？',
      subtitle: '会把这些文件改回那一轮开始前的内容：' + names
        + ((item.files || []).length > 4 ? ' 等 ' + item.count + ' 个' : ''),
      body: h('div.sNote',
        item.stacked
          ? '注意：这些文件在这之后又被改过。退回去会连带抹掉后面的改动。'
          : '这一步不能撤销（不过时间线上还留着每一段的 diff，可以照着改回来）。'),
      actions: [
        { label: '算了', onClick: () => Modal.close() },
        {
          label: '确认退回', kind: 'danger',
          onClick: async () => {
            const res = await API.restoreCheckpoint(item.id);
            Modal.close();
            if (!res.ok) return toast(res.error, 'bad');
            const payload = res.data || {};
            if (payload.failed && payload.failed.length) {
              toast('有 ' + payload.failed.length + ' 个文件没写回去', 'bad');
            } else {
              toast('已退回，还原了 ' + (payload.count || 0) + ' 个文件', 'good');
            }
            paintChanges();
            if (ctx.refreshSidebar) ctx.refreshSidebar();
          },
        },
      ],
    });
  }

  // ---- 第三层：这个文件的阶段，退回到哪一个

  async function paintTimeline(box) {
    box.innerHTML = '';
    const res = await API.changeTimeline(drill.cid, drill.path);
    if (!res.ok) return box.appendChild(h('div.empty', res.error));
    const data = res.data || {};
    drill.name = data.name || drill.name;
    const stages = data.stages || [];

    box.appendChild(h('div.drillHead',
      h('span', data.changes + ' 次改动'),
      h('span.dot', '·'),
      h('span', data.exists ? '文件在盘上' : '文件已不在盘上'),
      data.drifted ? h('span.pill.warn', '磁盘和记录对不上') : null));

    if (!drill.alive) {
      box.appendChild(h('div.whyLine', '这条对话已经删除，这里不能再退回。'));
    } else if (data.drifted) {
      box.appendChild(h('div.whyLine',
        '磁盘上现在的内容不属于任何一个阶段 —— 有别的东西改过它。退回会覆盖那些改动，要按 force 走。'));
    }
    if (data.truncated) {
      box.appendChild(h('div.whyLine', '更早的阶段已经被滚动清理，这里只留最近 ' + stages.length + ' 个。'));
    }
    if (!stages.length) {
      return box.appendChild(h('div.empty', '这个文件没有可回看的阶段。'));
    }

    // 倒序：最近的在最上面。时间线是用来「往回走」的，起点应该在眼前。
    [...stages].reverse().forEach((stage, index, all) => {
      box.appendChild(stageRow(data, stage, index === 0, all));
    });
  }

  function stageRow(data, stage, latest, all) {
    const isOrigin = stage.kind === 'origin';
    const isRestore = stage.kind === 'restore';
    const blocked = !drill.alive || (!data.revertible && !isOrigin);
    const same = stage.current;
    const previous = all.find((item) => item.stage === stage.stage - 1);

    const tags = [];
    if (same) tags.push(h('span.pill.ok', '当前'));
    if (latest && !same) tags.push(h('span.tag', '最新'));
    if (isRestore) tags.push(h('span.tag', '退回'));

    const why = same ? null
      : (blocked ? h('div.whyLine', data.reason || '这条对话已删除') : null);

    const node = h('div.stage' + (same ? '.on' : '') + (isOrigin ? '.origin' : ''),
      h('div.stageRail',
        h('span.stageDot'),
        h('span.stageLine')),
      h('div.stageBody',
        h('div.t',
          h('span.stageName', stage.label),
          ...tags),
        h('div.d',
          (isOrigin
            ? (stage.existed ? '这次对话碰它之前的样子' : '那时候还没有这个文件')
            : relTime(stage.ts) + (stage.turn ? ' · 第 ' + stage.turn + ' 轮' : ''))
          + ' · ' + sizeText(stage.bytes)),
        why,
        h('div.row2',
          h('button.mini', {
            disabled: isOrigin || !data.exists,
            title: isOrigin ? '最初没有「改之前」可比' : '看这一步改了什么',
            onclick: () => showStageDiff(stage, previous),
          }, '看 diff'),
          h('button.mini.danger', {
            disabled: same || blocked,
            title: same ? '已经在这个阶段了' : (blocked ? (data.reason || '这条对话已删除') : '把文件写回这个阶段'),
            onclick: () => confirmStageRestore(data, stage),
          }, same ? '就在这里' : '退回这里'))));
    return node;
  }

  function sizeText(bytes) {
    const value = Number(bytes) || 0;
    if (value >= 1024 * 1024) return (value / 1024 / 1024).toFixed(1) + ' MB';
    if (value >= 1024) return (value / 1024).toFixed(1) + ' KB';
    return value + ' 字节';
  }

  async function showStageDiff(stage, previous) {
    const res = await API.diff(drill.cid, drill.path, stage.stage);
    if (!res.ok) return toast(res.error, 'bad');
    Modal.open({
      title: drill.name || '这个文件',
      subtitle: '第 ' + stage.stage + ' 阶段 · ' + relTime(stage.ts)
        + (previous ? '（相对第 ' + previous.stage + ' 阶段）' : ''),
      body: h('pre.code', res.text),
      actions: [{ label: '知道了', kind: 'primary', onClick: () => Modal.close() }],
    });
  }

  function confirmStageRestore(data, stage) {
    const drifted = data.drifted;
    Modal.open({
      title: '退回到「' + stage.label + '」？',
      subtitle: (drill.name || '这个文件') + ' · 来自「' + (drill.title || '未命名对话') + '」',
      body: h('div',
        h('div.sNote', stage.stage === 0
          ? (stage.existed
            ? '会把这个文件写回这次对话碰它之前的内容。'
            : '这个文件在这次对话之前并不存在，退回等于把它移进回收站。')
          : '会把这个文件写回第 ' + stage.stage + ' 阶段的内容（' + sizeText(stage.bytes) + '）。'),
        drifted ? h('div.sNote.warn',
          '磁盘上的内容和任何一个阶段都对不上 —— 有别的东西改过它。现在退回，会把那些改动一起抹掉。') : null),
      actions: [
        { label: '算了', onClick: () => Modal.close() },
        {
          label: drifted ? '我知道，强制退回' : '退回',
          kind: 'danger',
          onClick: async () => {
            const res = await API.restoreStage(drill.cid, drill.path, stage.stage, drifted);
            Modal.close();
            if (!res.ok) return toast(res.error, 'bad');
            const payload = res.data || {};
            toast(payload.note || ('已退回到「' + stage.label + '」'), 'good');
            paintChanges();
            if (ctx.refreshSidebar) ctx.refreshSidebar();
          },
        },
      ],
    });
  }

  // ------------------------------------------------------------- 软件

  async function buildSoftware(body) {
    let query = '';
    let showing = 'app';

    const search = h('input', {
      placeholder: '找软件：微信 / 浏览器 / 记事本…',
      autocomplete: 'off',
    });
    const list = h('div');
    body.appendChild(h('div.sField', search));
    const tabs = h('div.segRow');
    body.appendChild(tabs);
    body.appendChild(list);

    const seg = (id, label) => {
      const node = h('button.segBtn' + (showing === id ? '.on' : ''), label);
      node.addEventListener('click', () => {
        showing = id;
        tabs.querySelectorAll('.segBtn').forEach((btn) => btn.classList.remove('on'));
        node.classList.add('on');
        paint();
      });
      return node;
    };
    tabs.appendChild(seg('app', '能打开的'));
    tabs.appendChild(seg('installed', '装了什么'));

    const paint = async () => {
      list.innerHTML = '';
      list.appendChild(h('div.empty', '读取中…'));
      if (showing === 'installed') return paintInstalled();
      const res = await API.software(query);
      if (!res.ok) return paintFail(res.error);
      const data = res.data || {};
      const items = data.items || [];
      list.innerHTML = '';
      if (!items.length) {
        return list.appendChild(h('div.empty', query
          ? '没有名字含「' + query + '」的软件。'
          : '没扫到可启动的软件。'));
      }
      list.appendChild(h('div.sGroup', data.total + ' 个可启动，这里 ' + items.length + ' 个'));
      items.forEach((item) => list.appendChild(softwareRow(item)));
      list.appendChild(h('div.sNote', {
        style: 'marginTop:10px',
      }, 'agent 用的是同一份清单。它要开哪个，会先在这里查名字，再走系统那一下启动。'));
    };

    const paintInstalled = async () => {
      const res = await API.softwareInstalled();
      if (!res.ok) return paintFail(res.error);
      const rows = ((res.data || {}).items) || [];
      list.innerHTML = '';
      if (!rows.length) return list.appendChild(h('div.empty', '注册表里没读到已安装的软件。'));
      list.appendChild(h('div.sGroup', rows.length + ' 个已安装'));
      rows.forEach((row) => {
        list.appendChild(h('div.listItem',
          h('div.t', row.name),
          h('div.d', [row.version ? 'v' + row.version : '', row.publisher || '']
            .filter(Boolean).join(' · ') || '—'),
          row.where ? h('div.d', row.where) : null));
      });
    };

    const paintFail = (message) => {
      list.innerHTML = '';
      list.appendChild(h('div.empty', message));
    };

    const softwareRow = (item) => {
      const source = { 'start-menu': '开始菜单', 'app-paths': '注册表', builtin: '内置' }[item.source] || item.source;
      const node = h('div.listItem',
        h('div.t', item.name, h('span.tag', source)),
        h('div.d', item.target),
        h('div.row2',
          h('div.grow'),
          h('button.sBtn', { onclick: () => openSoftware(item.name) }, '打开')));
      return node;
    };

    const openSoftware = async (name) => {
      toast('正在打开「' + name + '」…');
      const res = await API.openSoftware(name);
      if (!res.ok) return toast(res.error || '打不开', 'bad');
      toast((res.data && res.data.message) || '已启动', 'good');
    };

    let timer = null;
    search.addEventListener('input', () => {
      clearTimeout(timer);
      timer = setTimeout(() => { query = search.value.trim(); paint(); }, 200);
    });
    paint();
  }


  // ------------------------------------------------------------- 用量

  async function buildUsage(body) {
    const result = await API.usage();
    if (!result.ok) return body.appendChild(h('div.empty', result.error));
    const data = result.data || {};
    const total = data.total || {};

    body.appendChild(h('div.hint', {
      style: { color: 'var(--faint)', fontSize: '11.5px', marginBottom: '10px', lineHeight: '1.6' },
    }, '数据直接从每个会话的轨迹里汇总，不另存一份账本 —— 两边对不上是最难查的一类问题。'));

    const cards = [
      { k: '总 token', v: total.total || 0 },
      { k: '输入', v: total.prompt || 0 },
      { k: '输出', v: total.completion || 0 },
      { k: '模型调用', v: total.calls || 0 },
      { k: '会话数', v: data.sessions || 0 },
      { k: '累计耗时', v: (Number(total.seconds || 0) / 60).toFixed(1) + ' 分' },
    ];
    body.appendChild(h('div.usageGrid', cards.map((card) =>
      h('div.usageCard',
        h('div.k', card.k),
        h('div.v', typeof card.v === 'number' ? compactNumber(card.v) : card.v)))));

    const days = data.days || [];
    if (days.length) {
      body.appendChild(h('div.sectionTitle', '按天'));
      const peak = Math.max(...days.map((day) => day.total), 1);
      const bars = h('div.uBars');
      days.slice(-21).forEach((day) => {
        bars.appendChild(h('div.uBar',
          h('span.d', day.day.slice(5)),
          h('span.track', h('i', { style: { width: Math.max(2, (day.total / peak) * 100) + '%' } })),
          h('span.n', compactNumber(day.total))
        ));
      });
      body.appendChild(bars);
    }

    const modes = data.modes || {};
    const keys = Object.keys(modes);
    if (keys.length) {
      body.appendChild(h('div.sectionTitle', '按模式'));
      keys.forEach((key) => {
        body.appendChild(h('div.listItem',
          h('div.t', key),
          h('div.d', compactNumber(modes[key]) + ' token')));
      });
    }

    if (!Number(total.total || 0)) {
      body.appendChild(h('div.empty', { style: { marginTop: '16px' } },
        '还没有用量。跑一轮对话就会记下来。'));
    }
  }

  function compactNumber(value) {
    const number = Number(value) || 0;
    if (number >= 1e6) return (number / 1e6).toFixed(2) + 'M';
    if (number >= 1e4) return (number / 1e3).toFixed(1) + 'k';
    return String(number);
  }


  // ------------------------------------------------------------- 设置

  async function buildSettings(body) {
    const result = await API.settings();
    if (!result.ok) return body.appendChild(h('div.empty', result.error));
    const settings = result.data;
    const catalog = settings.provider_catalog || [];
    const providers = settings.providers || {};

    body.appendChild(h('div.sectionTitle', '模型'));

    const provider = h('select.select', ...catalog.map((item) =>
      h('option', { value: item.id, selected: item.id === settings.provider }, item.name)));

    const model = h('input.input', { value: settings.model || '', placeholder: '模型名' });
    const baseUrl = h('input.input', {
      value: (providers[settings.provider] || {}).base_url || settings.base_url || '',
      placeholder: 'https://…',
    });
    const apiKey = h('input.input', {
      value: '',
      type: 'password',
      placeholder: (providers[settings.provider] || {}).api_key_masked || '还没填',
    });

    /* datalist 必须被 input 的 list 属性指到才生效 —— 只把它放在旁边是没用的，
       浏览器完全不理。id 加序号是因为这一栏可能同时挂在左栏和抽屉里。 */
    const listId = 'modelOptions' + (++uid);
    model.setAttribute('list', listId);
    const modelList = h('datalist', { id: listId });
    const meta = catalog.find((item) => item.id === settings.provider) || {};

    function refreshModelList() {
      const currentMeta = catalog.find((item) => item.id === provider.value) || {};
      modelList.innerHTML = '';
      (currentMeta.models || []).forEach((name) => modelList.appendChild(h('option', { value: name })));
      baseUrl.value = (providers[provider.value] || {}).base_url || currentMeta.base_url || '';
      apiKey.placeholder = (providers[provider.value] || {}).api_key_masked || '还没填';
      if (!model.value || !(currentMeta.models || []).includes(model.value)) {
        model.value = (currentMeta.models || [])[0] || '';
      }
    }
    provider.addEventListener('change', refreshModelList);

    body.appendChild(h('div.field', h('label', '服务商'), provider,
      h('div.hint', meta.note || '全部走 OpenAI 兼容协议，换一家只是换地址和 key。')));
    body.appendChild(h('div.field', h('label', '模型'), model, modelList));
    body.appendChild(h('div.field', h('label', '接口地址'), baseUrl));
    body.appendChild(h('div.field', h('label', 'API Key'), apiKey,
      h('div.hint', '留空表示不改。key 只存在本机 settings.json 里，不会上传到任何地方。')));
    /* 用局部变量而不是 $('#optModel')：这一栏可能同时出现在左栏和抽屉里，
       同 id 会撞车，$() 只会拿到第一个 —— 于是从抽屉保存会写进左栏的值。 */
    const optModel = h('input.input', { value: settings.optimize_model || '', placeholder: '留空就用上面这个模型' });
    body.appendChild(h('div.field', h('label', '优化提示词用的模型（可留空）'), optModel));

    const testRow = h('div.row2', { style: { display: 'flex', gap: '8px', marginTop: '4px' } });
    testRow.appendChild(h('button.mini', {
      onclick: async (event) => {
        const button = event.currentTarget;
        button.textContent = '测试中…';
        button.disabled = true;
        const res = await API.testModel();
        button.textContent = '测试连通';
        button.disabled = false;
        if (!res.ok) return toast(res.error || '不通', 'bad');
        toast('通了：' + (res.data.reply || ''), 'good');
      },
    }, '测试连通'));
    testRow.appendChild(h('span.hint', {
      style: { color: 'var(--faint)', fontSize: '11px', alignSelf: 'center' },
    }, settings.has_key ? '已配置 key' : '还没配置 key'));
    body.appendChild(testRow);

    body.appendChild(h('div.sectionTitle', '行为'));

    const permission = h('select.select',
      h('option', { value: 'readonly', selected: settings.permission === 'readonly' }, '只读 · 只许看'),
      h('option', { value: 'standard', selected: settings.permission === 'standard' }, '标准 · 写文件要确认'),
      h('option', { value: 'staged', selected: settings.permission === 'staged' }, '分阶段 · 阶段交接才确认'),
      h('option', { value: 'full', selected: settings.permission === 'full' }, '全权 · 不再打断'));
    body.appendChild(h('div.field', h('label', '权限档位'), permission,
      h('div.hint', '注意：问答和规划模式会强制只读，教学模式会强制分阶段。')));

    const autoOptimize = h('input', { type: 'checkbox', checked: !!settings.auto_optimize });
    body.appendChild(h('div.switchRow',
      h('div.meta',
        h('div.nm', '发送前自动优化'),
        h('div.ds', '默认关闭。开着的时候按下发送会先改写再预览，确认后才发出去。')),
      h('label.toggle' + (settings.auto_optimize ? '.on' : ''), {
        style: { display: 'grid', placeItems: 'center' },
        onclick: (event) => {
          event.preventDefault();
          autoOptimize.checked = !autoOptimize.checked;
          event.currentTarget.classList.toggle('on', autoOptimize.checked);
        },
      })));

    const maxSteps = h('input.input', { value: String(settings.max_steps ?? 24) });
    body.appendChild(h('div.field', h('label', '单轮最多几步'), maxSteps,
      h('div.hint', '防止模型钻牛角尖停不下来。')));

    /* --- 上下文与记忆 ---------------------------------------------------
       这两件事放在一起，因为它们解决的是同一个问题的两头：
       窗口是「能装多少」，记忆是「装不下的部分里哪些值得留」。 */
    body.appendChild(h('div.sectionTitle', '上下文与记忆'));

    const WINDOWS = [
      ['', '自动 · 按模型名判断'],
      ['32k', '32K'],
      ['64k', '64K'],
      ['128k', '128K'],
      ['200k', '200K'],
      ['1m', '1M'],
    ];
    const windowSel = h('select.select', ...WINDOWS.map(([value, label]) =>
      h('option', { value: value, selected: (settings.context_window || '') === value }, label)));
    body.appendChild(h('div.field', h('label', '模型上下文窗口'), windowSel,
      /* 这里是界面文案，不是 markdown —— 写 `**宁可猜小**` 会在屏幕上原样露出一对星号。 */
      h('div.hint', '选小了压缩得更勤，选大了可能被接口拒（超过模型的真实上限）。'
        + '不确定就留在「自动」——它会按模型名猜，而且宁可猜小：猜大了是 400，猜小了只是多压一次。')));

    const RATIOS = [
      [0.6, '保守 · 用到 60% 就压缩'],
      [0.72, '默认 · 72%'],
      [0.85, '尽量晚 · 85%'],
    ];
    const ratioNow = Number(settings.compact_ratio ?? 0.72);
    const ratioSel = h('select.select', ...RATIOS.map(([value, label]) =>
      h('option', { value: String(value), selected: Math.abs(ratioNow - value) < 0.001 }, label)));
    body.appendChild(h('div.field', h('label', '什么时候开始压缩'), ratioSel,
      h('div.hint', '早期记录会被压成一段摘要，最近 10 条完整保留（工具调用链不能从中间截断，否则接口直接 400）。')));

    const MEMORY_LEVELS = [
      ['off', '关闭 · 只有模型自己想起来才记'],
      ['daily', '每日记录 · 每轮记一笔「做了什么」'],
      ['smart', '每日记录 + 项目记忆 · 动了文件还会抽结论'],
    ];
    const memoryLevel = settings.auto_memory || 'smart';
    const memorySel = h('select.select', ...MEMORY_LEVELS.map(([value, label]) =>
      h('option', { value: value, selected: memoryLevel === value }, label)));
    body.appendChild(h('div.field', h('label', '自动记忆'), memorySel,
      /* 这句话必须写清楚「会多花一次调用」——不然用户看到账单会不知道该怪谁 */
      h('div.hint', '每轮结束后把「做了什么」写进当日记录（不花钱）。'
        + '这一轮真动过文件时，再让模型抽 1~2 条结论进项目记忆 —— 那会多一次很小的模型调用。'
        + '以前只有模型主动调工具才记，而那个工具要你点确认，所以记忆一直是空的。')));

    /* 个性化：几句话，每一轮都注入。优先级高于模式和基础的规矩 ——
       用户写「别用 emoji」，比任何默认风格都该算数。 */
    const note = h('textarea.input', {
      rows: 4,
      placeholder: '例如：用简体中文；先给结论再给理由；不要写「好的，我来帮你」这种开场白。',
    });
    note.value = settings.personal_note || '';
    body.appendChild(h('div.field', h('label', '个性化要求'), note,
      h('div.hint', '每一轮都会带上。写「怎么说话、怎么做事」，比写「你是谁」有用。')));

    body.appendChild(h('div.field', h('label', '数据目录'),
      h('input.input', { value: settings.data_dir || '', readonly: true })));

    body.appendChild(h('button.primary', {
      style: { width: '100%', marginTop: '8px' },
      onclick: async () => {
        const payload = {
          provider: provider.value,
          model: model.value.trim(),
          optimize_model: optModel.value.trim(),
          permission: permission.value,
          auto_optimize: autoOptimize.checked,
          compact_ratio: Number(ratioSel.value) || 0.72,
          context_window: windowSel.value,
          auto_memory: memorySel.value,
          max_steps: Number(maxSteps.value) || 24,
          personal_note: note.value.trim(),
          providers: {
            [provider.value]: {
              base_url: baseUrl.value.trim(),
              api_key: apiKey.value,
              model: model.value.trim(),
            },
          },
        };
        const res = await API.saveSettings(payload);
        if (!res.ok) return toast(res.error || '存不下', 'bad');
        toast('已保存', 'good');
        if (ctx) await ctx.refreshBootstrap();
        refresh();
      },
    }, '保存'));

    body.appendChild(h('div.sectionTitle', '默认权限 · 新对话起手就带着'));
    body.appendChild(await globalGrantsBox());

    body.appendChild(h('div.sectionTitle', '关于'));
    body.appendChild(h('div.listItem',
      h('div.t', 'DreamRole ' + (settings.version || '')),
      h('div.d', '一个自己用的 agent。事件流是真相，图是投影。'),
      h('div.d', { style: { marginTop: '6px', fontFamily: 'var(--mono)', fontSize: '11px' } },
        '数据：' + (settings.data_dir || ''))
    ));
  }

  /* ---------------------------------------------------------------------
   * 全局通行证（默认权限）
   *
   * 用户的原话是「不想一个一个添加，设置全局权限」——确实是这么回事：
   * 工作区弹窗里那张表是**这一条会话**的，每开一条新对话就要从头补一遍，
   * 补到第十次就在骂人了。所以这里单独有一份「新对话起手就带着」。
   *
   * 三件必须写在明面上的事：
   * 1. **只影响之后新建的对话。** 已经在的那些按它自己那份来 ——
   *    「这条会话能去哪」不该在它跑到一半时自己变。
   * 2. **它能撤。** 每一行都有 ×，一时手滑开大了一点也不至于没救。
   * 3. **它不越过底线。** 系统目录、私钥那几条是硬拒绝，
   *    加进来也进不去（见 app/rules.py），所以这里不用再堆一层警告。
   * ------------------------------------------------------------------- */
  async function globalGrantsBox() {
    const box = h('div.wsGrants', { style: { marginTop: '0', borderTop: 'none', paddingTop: '0' } });
    let items = [];
    const MAX = 12;

    const res = await API.globalGrants();
    items = (res.ok && res.data && res.data.grants) || [];

    async function send(op, extra) {
      const answer = await API.updateGlobalGrants(Object.assign({ op: op }, extra || {}));
      if (!answer.ok) return toast(answer.error || '改不了全局权限', 'bad');
      items = (answer.data && answer.data.grants) || [];
      if (answer.data && answer.data.note) toast(answer.data.note, 'good');
      /* 左栏那份 bootstrap 里也带着 global_grants，改完要跟着变，
         不然「工作区弹窗」和「设置页」会各说各的。 */
      if (ctx) await ctx.refreshBootstrap();
      paint();
    }

    async function addOne() {
      const picked = await API.pickFolder('添加一个所有对话都能去的目录');
      if (!picked.ok) return toast(picked.error || '打不开系统的选文件夹对话框', 'bad');
      const path = picked.data && picked.data.path;
      if (!path) return;                 // 用户按了取消
      await send('add', { path: path, mode: 'ro' });
    }

    function paint() {
      box.innerHTML = '';
      box.appendChild(h('div.wsGrantsHead',
        h('span', '所有新对话都能去的地方'),
        h('span.wsGrantsCount', items.length + '/' + MAX)));

      if (!items.length) {
        box.appendChild(h('div.wsGrantsEmpty',
          '还没有。新对话只会待在自己的工作区里 —— 这是默认，也是最安全的那个默认。'));
      }

      items.forEach((one) => {
        const writable = one.mode === 'rw';
        box.appendChild(h('div.grantRow', { title: one.path },
          h('span.grantPath',
            shortPath(one.path),
            one.missing ? h('span.grantGone', '目录不在了') : null),
          h('button.grantMode' + (writable ? '.rw' : ''), {
            title: writable ? '点一下改成只读' : '点一下改成可写',
            onclick: () => send('mode', { path: one.path, mode: writable ? 'ro' : 'rw' }),
          }, writable ? '可写' : '只读'),
          h('button.grantDel', {
            title: '撤掉这张',
            onclick: () => send('remove', { path: one.path }),
          }, '×')));
      });

      const foot = h('div.wsGrantsFoot', h('button.mini', { onclick: addOne }, '＋ 添加位置…'));
      if (items.length) {
        foot.appendChild(h('button.mini.danger', {
          onclick: () => send('clear', {}),
        }, '全部撤掉'));
      }
      box.appendChild(foot);

      box.appendChild(h('div.wsGrantsHint',
        '只对「之后新建」的对话生效。已经在的对话按它自己那份来，改这里不会动它。',
        h('br'),
        '想给某一条对话单独多开一道门，还是在那条对话的「选工作区」里加。'));
    }

    paint();
    return box;
  }

  // ------------------------------------------------------------- 助理
  /* ------------------------------------------------------------ 智能体团队页
   *
   * 设计稿（`agent-team-page-v2`）把「助理」做成一间真屋子：左边是频道与成员，
   * 中间是这个频道的对话，右边是手上的活儿和预算。
   *
   * 和原来那张「助理卡片列表」的差别不在好看：列表只回答得了「有谁」，
   * 回答不了「他们刚才说了什么、这件事谁在做」。团队页的每一行都来自真数据 ——
   * 频道是群聊，成员是助理，消息是那个群最后一次会话里的事件。
   */
  let teamChan = '';

  function teamAvatar(bots, idOrIdx) {
    const idx = typeof idOrIdx === 'number'
      ? idOrIdx
      : Math.max(0, bots.findIndex((one) => one.id === idOrIdx));
    return 'a' + (idx % 4 + 1);
  }

  /* 取第一个「字」。不能用 `slice(0, 1)`：emoji 是代理对，切一半会留下一个
     孤立的半字符 —— 轻则显示成方块，重则让浏览器的 DOM 序列化直接崩掉
     （自检里 `--dump-dom` 就是这么挂的，报出来还是个看不出所以然的本机错误码，
     而且只有「有成员」的那一屏会挂）。 */
  function firstGlyph(text) {
    const chars = Array.from(String(text || '?'));
    return chars.length ? chars[0] : '?';
  }

  function teamTracesOf(traces, chan) {
    if (!chan) return [];
    if (chan.indexOf('dm:') === 0) {
      const aid = chan.slice(3);
      return traces.filter((one) => one.assistant === aid && !(one.members || []).length);
    }
    return traces.filter((one) => one.group === chan);
  }

  function hhmm(ts) {
    if (!ts) return '';
    const when = new Date(ts * 1000);
    if (Number.isNaN(when.getTime())) return '';
    const pad = (n) => String(n).padStart(2, '0');
    return pad(when.getHours()) + ':' + pad(when.getMinutes());
  }

  async function buildTeam(body) {
    const [aRes, tRes] = await Promise.all([API.assistants(), API.traces()]);
    if (!aRes.ok) return body.appendChild(h('div.empty', aRes.error || '读不出助理'));
    const data = aRes.data || {};
    const bots = data.items || [];
    const groups = data.groups || [];
    const rules = data.rules || {};
    const traces = (tRes.ok && tRes.data) || [];

    if (!bots.length && !groups.length) {
      body.appendChild(h('div.empty', '还没有智能体。先建一个 —— 它会有一间自己的屋子。'));
      body.appendChild(h('button.ghost.wideBtn', {
        onclick: () => openAssistantForm(data),
      }, '＋ 新建智能体'));
      return;
    }

    if (!teamChan) {
      teamChan = groups.length ? groups[0].id : 'dm:' + bots[0].id;
    }

    const chanExists = groups.some((one) => one.id === teamChan)
      || (teamChan.indexOf('dm:') === 0 && bots.some((one) => 'dm:' + one.id === teamChan));
    if (!chanExists) teamChan = groups.length ? groups[0].id : 'dm:' + bots[0].id;

    const mine = teamTracesOf(traces, teamChan);
    const group = groups.find((one) => one.id === teamChan) || null;
    const dmBot = group ? null : bots.find((one) => 'dm:' + one.id === teamChan) || null;
    const members = group ? (group.members || []).map((id) => bots.find((b) => b.id === id)).filter(Boolean)
      : (dmBot ? [dmBot] : []);

    // 团队页自己带三列，外层不许再给内边距/滚动条
    body.classList.add('flush');
    const wrap = h('div.tm-wrap');
    body.appendChild(wrap);

    /* ---- 左列：频道 + 成员 ---- */
    const chans = h('div.tm-chans');
    chans.appendChild(h('div.tm-sec',
      h('span', '频道'),
      h('button.tm-plus', {
        title: '建一个群',
        onclick: () => openGroupForm(data),
      }, '＋')));
    groups.forEach((one) => {
      const count = teamTracesOf(traces, one.id).length;
      chans.appendChild(h('button.tm-ch' + (one.id === teamChan ? '.on' : ''), {
        onclick: () => { teamChan = one.id; rerender(); },
      },
        h('span.h', '#'),
        h('span', one.name || '群聊'),
        count ? h('span.n', String(count)) : null));
    });
    if (!groups.length) {
      chans.appendChild(h('div.tm-hint', '还没有群。点上面的 ＋ 建一个。'));
    }

    chans.appendChild(h('div.tm-sec', { style: { marginTop: '6px' } },
      h('span', '私聊'),
      h('button.tm-plus', {
        title: '新建智能体',
        onclick: () => openAssistantForm(data),
      }, '＋')));
    bots.forEach((one) => {
      chans.appendChild(h('button.tm-ch' + ('dm:' + one.id === teamChan ? '.on' : ''), {
        onclick: () => { teamChan = 'dm:' + one.id; rerender(); },
      }, h('span.h', '@'), h('span', one.name)));
    });

    chans.appendChild(h('div.tm-sec', { style: { marginTop: '6px' } },
      h('span', '成员 ' + members.length),
      h('span', { style: { marginLeft: 'auto', color: '#4a5058' } }, '在线 ' + members.length)));
    members.forEach((one, idx) => {
      chans.appendChild(h('div.tm-mem', {
        title: one.tagline || one.name,
        onclick: () => { teamChan = 'dm:' + one.id; rerender(); },
      },
        h('span.av.' + teamAvatar(bots, one.id), firstGlyph(one.emoji || one.name)),
        h('span', one.name),
        /* 群频道里每个成员后面有一枚「移出」。
           以前这里只有名字和一个灰点 —— 想把谁从这个群里拿掉，得回去开
           「改规则」那张整表单。移出一个人是日常动作，不该要填一张表。 */
        group ? h('button.tm-memx', {
          title: '把「' + one.name + '」移出这个群',
          onclick: (event) => {
            event.stopPropagation();
            removeFromGroup(group, bots, one);
          },
        }, '×') : null,
        h('span.st.idle')));
    });

    /* ---- 中列：这个频道的对话 ---- */
    const chat = h('div.tm-chat');
    const avstack = h('div.tm-avstack');
    members.slice(0, 4).forEach((one) => {
      avstack.appendChild(h('i.av.' + teamAvatar(bots, one.id),
        firstGlyph(one.emoji || one.name)));
    });
    const head = h('div.tm-chead',
      h('div',
        h('div.t', group ? '# ' + (group.name || '群聊') : '@ ' + (dmBot ? dmBot.name : '')),
        h('div.s', group
          ? `${members.length} 名成员 · ${rules[group.rule] || group.rule || '轮转发言'}`
          : '私聊 · 只有它会回答')),
      avstack,
      h('div.grow'),
      group ? h('button.tm-cb', { onclick: () => openGroupForm(data, group) }, '改规则') : null,
      group ? h('button.tm-cb', { onclick: () => openMemberManager(data, group) }, '成员') : null,
      /* 私聊没有「群规则」也没有成员表 —— 只有一个人，那两枚按钮点开都是空表单。
         一条私聊真正能改的只有它的人设，能删的也只有它这个角色。 */
      group ? null : h('button.tm-cb', {
        onclick: () => openAssistantForm(data, dmBot || members[0]),
      }, '改人设'),
      group ? null : h('button.tm-cb.danger', {
        onclick: () => confirmDeleteAssistant(withStats(dmBot || members[0])),
      }, '删掉 TA'));
    chat.appendChild(head);

    const msgs = h('div.tm-msgs');
    chat.appendChild(msgs);

    const gutter = h('div.tm-gcomposer');
    const box = h('textarea.tm-input', {
      rows: 1,
      placeholder: group ? '发消息给团队，或 @ 成员 / / 派任务' : '发消息给 ' + (dmBot ? dmBot.name : ''),
    });
    gutter.appendChild(h('div.tm-gline1',
      box,
      h('button.tm-send', {
        title: '发送',
        onclick: async () => {
          const text = box.value.trim();
          if (!text) return;
          const payload = group ? { group: group.id } : { assistant: dmBot ? dmBot.id : '' };
          if (typeof ctx !== 'undefined' && ctx.sendIntoTeam) {
            const ok = await ctx.sendIntoTeam(payload, text);
            if (ok) box.value = '';
          }
        },
      }, '↑')));
    gutter.appendChild(h('div.tm-gline2',
      h('span.tm-gchip.to', group ? '发送给：全体' : '发送给：' + (dmBot ? dmBot.name : '')),
      h('span.tm-gchip', '/ 派任务'),
      h('span.tm-gchip', '& 挂技能'),
      h('span.grow', { style: { flex: '1 1 auto' } }),
      h('span', group ? '未被 @ 的成员不会启动' : ''),
      h('span', '⇧↵ 换行')));
    chat.appendChild(gutter);

    /* ---- 右列：手上的活儿 + 规则 + 预算 ---- */
    const side = h('div.tm-side');
    wrap.appendChild(chans);
    wrap.appendChild(chat);
    wrap.appendChild(side);

    /* 先把壳画出来，消息和「进行中」异步补 —— 一屏空白等接口是最难看的加载态 */
    msgs.appendChild(h('div.tm-hint', '读取最近的对话…'));

    const newest = mine.slice().sort((a, b) => (b.updated || 0) - (a.updated || 0))[0];
    let events = [];
    if (newest) {
      const res = await API.getSession(newest.cid);
      if (res.ok) events = (res.data && res.data.events) || [];
    }
    msgs.innerHTML = '';

    /* 系统行：这个频道从什么时候开始有事 */
    if (newest) {
      msgs.appendChild(h('div.tm-sys',
        h('span.l'), h('span', '最近一次 · ' + relTime(newest.updated || newest.created)), h('span.l')));
    } else {
      msgs.appendChild(h('div.tm-empty',
        group ? '这个群还没说过话。下面发一句，成员才会开口。'
          : '还没和它单独说过话。下面发一句试试。'));
    }

    let shown = 0;
    events.forEach((event) => {
      /* 群里成员的发言落盘时是 `answer`（普通会话里才叫 `assistant`）。
         只认 `assistant` 的话，群里永远只有「我」说的话 —— 看着像功能坏了，
         其实是一条事件被过滤掉了。 */
      if (!['user', 'assistant', 'answer'].includes(event.type)) return;
      if (!event.content) return;
      if (shown >= 40) return;
      const member = (event.payload && event.payload.member) || null;
      const isMine = event.type === 'user';
      const who = isMine ? '我' : (member ? member.name : (dmBot ? dmBot.name : '助手'));
      const botId = member ? member.id : (dmBot ? dmBot.id : '');
      shown += 1;
      msgs.appendChild(h('div.tm-m2' + (isMine ? '.mine' : ''),
        h('span.av.' + teamAvatar(bots, botId), isMine ? '我' : firstGlyph(member && member.emoji ? member.emoji : who)),
        h('div.c',
          h('div.top',
            h('span.who', who),
            h('span.tm', hhmm(event.ts || event.time)),
            (!isMine && !dmBot) ? h('span.tag3', '队友') : null),
          h('div.txt2', { html: markdown(event.content) }))));
    });

    /* 计划/步骤：事件流里最后一份待办就是「手上的活儿」 */
    let todos = [];
    events.forEach((event) => {
      if (event.type === 'todos' && event.payload && Array.isArray(event.payload.todos)) {
        todos = event.payload.todos;
      }
    });
    /* 右列照设计稿分四块：进行中 / 待办待审 / 提示 / 概况。
       数字全部来自这个频道最近那次会话的事件 —— 不另造一份「团队态」，
       两处账本迟早对不上，而对不上的时候没人知道该信哪边。 */
    const running = todos.filter((one) => ['doing', 'run', 'running', 'active'].includes(one.status));
    const waiting = todos.filter((one) =>
      ['pending', 'wait', 'waiting', 'blocked', 'review'].includes(one.status));
    const finished = todos.filter((one) => one.status === 'done');

    const taskCard = (title, rows, empty, dot) => {
      const card = h('div.tm-rc',
        h('div.tm-rh', h('span', title),
          h('div.grow', { style: { flex: '1 1 auto' } }),
          h('span.x', String(rows.length))));
      if (!rows.length) card.appendChild(h('div.tm-none', empty));
      rows.slice(0, 4).forEach((one) => {
        card.appendChild(h('div.tm-tk',
          h('span.d.' + dot),
          h('span.t', one.title || one.text || ''),
          h('span.own', one.owner || 'team')));
      });
      return card;
    };

    side.appendChild(taskCard('进行中', running, '手上没有在跑的活', 'run'));
    side.appendChild(taskCard('待办 / 待审', waiting, '没有排着的活', 'wait'));

    side.appendChild(h('div.tm-tip',
      '💡 群聊规则：默认「发送给全体」只广播不唤醒；只有被 @ 的成员会真正跑起来，'
      + '避免一条消息唤醒所有 agent 烧 token。'));

    const usage = (newest && newest.usage && newest.usage.total) || 0;
    const card = h('div.tm-rc',
      h('div.tm-rh', h('span', '会话概况'), h('div.grow', { style: { flex: '1 1 auto' } }),
        h('span.x', group ? '频道' : '私聊')));
    card.appendChild(h('div.tm-kv', h('span', '成员'), h('b', String(members.length))));
    card.appendChild(h('div.tm-kv', h('span', '会话'), h('b', String(mine.length))));
    /* 「屋子」= 这个频道现在在哪个目录里干活，点一下就打开资源管理器。
       没有它的话，「它把文件写哪了」这个问题在界面上没有答案 ——
       而这是用户最先想去看的东西。 */
    if (newest && newest.project) {
      card.appendChild(h('div.tm-kv',
        h('span', '屋子'),
        h('b.tm-path', {
          title: newest.project + '（点一下打开）',
          onclick: () => revealDir(newest.project),
        }, shortPath(newest.project))));
    }
    if (finished.length) {
      card.appendChild(h('div.tm-kv', h('span', '已完成'), h('b', String(finished.length))));
    }
    if (usage) {
      card.appendChild(h('div.tm-kv', h('span', '最近用量'), h('b', fmtTokens(usage))));
    }
    side.appendChild(card);
  }

  function fmtTokens(n) {
    const num = Number(n) || 0;
    if (num >= 1000000) return (num / 1000000).toFixed(1) + 'M';
    if (num >= 1000) return (num / 1000).toFixed(1) + 'k';
    return String(num);
  }

  /* 这里原来还有一张「助理卡片列表」（buildAssistants）。它已经被
     团队页取代了 —— 那页只回答得了「有谁」，回答不了「他们刚才说了什么、
     这件事谁在做」，而后者才是用户真正要的。留着一个没人调的版本，会把
     「删角色」「打开屋子」这些动作困在一条走不到的路径上（确实发生过一次），
     所以整段删掉，那几个动作搬进团队页：改人设 / 删掉 TA 在频道头上，
     打开屋子在右栏「会话概况」里。 */

  /* 助理表单。三个起手式不是模板库，是三个「不同的人」——
     空白表单最难填，给三个具体的样子，改两个字就能用。 */
  function openAssistantForm(data, record) {
    const editing = !!record;
    const name = h('input', { placeholder: '叫它什么', value: record ? record.name : '' });
    const emoji = h('input', { class: 'tiny', placeholder: '🙂', value: record ? record.emoji : '🙂' });
    const tagline = h('input', {
      placeholder: '一句话说清它管什么', value: record ? record.tagline || '' : '',
    });
    const persona = h('textarea', {
      rows: 8, placeholder: '它怎么说话、关心什么、不做什么…',
    });
    persona.value = record ? record.persona || '' : '';

    const box = h('div.formCol',
      h('div.fieldRow', h('div.fieldLabel', '名字'), name),
      h('div.fieldRow',
        h('div.fieldLabel', '图标'),
        h('div.row3', emoji, tagline)
      ),
      h('div.fieldRow', h('div.fieldLabel', '人设'), persona)
    );

    if (!editing) {
      const starters = h('div.starterRow');
      (data.starters || []).forEach((starter) => {
        starters.appendChild(h('button.starterChip', {
          onclick: () => {
            name.value = starter.name;
            emoji.value = starter.emoji;
            tagline.value = starter.tagline;
            persona.value = starter.persona;
            toast('照着「' + starter.name + '」填好了，随便改', 'good');
          },
        }, h('span', starter.emoji), starter.name));
      });
      box.appendChild(h('div.fieldRow',
        h('div.fieldLabel', '起手式'),
        starters));
    }

    Modal.open({
      title: editing ? '改「' + record.name + '」' : '新建一个助理',
      subtitle: editing ? '改完人设，下一次对话就按新的来' : '它会有一间自己的屋子：工作区、对话记录、记忆都分开',
      wide: true,
      body: box,
      actions: [
        { label: '算了', onClick: () => Modal.close() },
        {
          label: editing ? '保存' : '建好',
          kind: 'primary',
          onClick: async () => {
            const payload = {
              name: name.value.trim(),
              emoji: emoji.value.trim() || '🙂',
              tagline: tagline.value.trim(),
              persona: persona.value,
            };
            if (!payload.name) return toast('名字不能空着', 'bad');
            const res = editing
              ? await API.saveAssistant(record.id, payload)
              : await API.createAssistant(payload);
            if (!res.ok) return toast(res.error, 'bad');
            Modal.close();
            toast(editing ? '人设已更新' : '助理建好了', 'good');
            if (ctx) await ctx.refreshBootstrap();
            refresh();
          },
        },
      ],
    });
  }

  function openGroupForm(data, record) {
    const editing = !!record;
    const items = data.items || [];
    const rules = data.rules || {};
    if (items.length < 2) {
      return toast('先建两个助理，才够开一个群', 'bad');
    }
    const name = h('input', { placeholder: '比如「周会」', value: record ? record.name : '' });
    const topic = h('input', {
      placeholder: '这个群平时聊什么（可以留空）', value: record ? record.topic || '' : '',
    });
    const picked = new Set(record ? record.members : [items[0].id, items[1].id]);

    const picks = h('div.pickRow');
    items.forEach((assistant) => {
      const box = h('label.pickChip',
        h('input', {
          type: 'checkbox',
          checked: picked.has(assistant.id),
          onchange: (event) => {
            if (event.target.checked) picked.add(assistant.id);
            else picked.delete(assistant.id);
            paintHost();
          },
        }),
        h('span', (assistant.emoji || '🙂') + ' ' + assistant.name)
      );
      picks.appendChild(box);
    });

    const rule = h('select', Object.keys(rules).map((id) =>
      h('option', { value: id, selected: record && record.rule === id ? 'selected' : null }, rules[id])));

    const host = h('select',
      h('option', { value: '' }, '（第一个成员）'),
      items.map((assistant) =>
        h('option', {
          value: assistant.id,
          selected: record && record.host === assistant.id ? 'selected' : null,
        }, (assistant.emoji || '🙂') + ' ' + assistant.name)));

    function paintHost() {
      host.disabled = rule.value !== 'host';
    }
    rule.addEventListener('change', paintHost);
    paintHost();

    Modal.open({
      title: editing ? '改「' + record.name + '」' : '建一个群',
      subtitle: '发言规则决定这一轮谁说话 —— 规则是一等公民，不是提示词里写一句「请有序发言」',
      wide: true,
      body: h('div.formCol',
        h('div.fieldRow', h('div.fieldLabel', '群名'), name),
        h('div.fieldRow', h('div.fieldLabel', '成员'), picks),
        h('div.fieldRow', h('div.fieldLabel', '规则'), rule),
        h('div.fieldRow', h('div.fieldLabel', '主持人'), host),
        h('div.fieldRow', h('div.fieldLabel', '话题'), topic)
      ),
      actions: [
        { label: '算了', onClick: () => Modal.close() },
        {
          label: editing ? '保存' : '建群',
          kind: 'primary',
          onClick: async () => {
            const payload = {
              name: name.value.trim() || '新群聊',
              members: [...picked],
              rule: rule.value,
              host: rule.value === 'host' ? host.value : '',
              topic: topic.value.trim(),
            };
            if (payload.members.length < 2) return toast('至少要两个助理', 'bad');
            const res = editing
              ? await API.saveGroup(record.id, payload)
              : await API.createGroup(payload);
            if (!res.ok) return toast(res.error, 'bad');
            Modal.close();
            toast(editing ? '群规则已改' : '群建好了', 'good');
            if (ctx) await ctx.refreshBootstrap();
            refresh();
          },
        },
      ],
    });
  }

  /* 助理记录里 `stats` 是 /api/assistants 现算的，团队页拿到的 items 没有这一栏。
     删人的确认框要显示「留下的位置」，所以在这儿补一个最小的 stats —— 
     不是造假数据：workspace 本来就跟着 item 回来了。 */
  function withStats(record) {
    if (!record) return {};
    if (record.stats && record.stats.workspace) return record;
    return Object.assign({}, record, {
      stats: { workspace: record.workspace || record.dir || '' },
    });
  }

  function confirmDeleteAssistant(record) {
    Modal.open({
      title: '删掉「' + record.name + '」？',
      subtitle: '只删这个角色。它的工作区和对话记录都留在原地 —— 干了三个月活的东西，不该被一次误点带走。',
      body: h('div.formCol',
        h('div.hint', { style: { fontSize: '12px', color: 'var(--faint)', lineHeight: '1.7' } },
          '留下的位置：', h('br'),
          h('code', (record.stats && record.stats.workspace) || ''))
      ),
      actions: [
        { label: '算了', onClick: () => Modal.close() },
        {
          label: '删掉这个角色',
          kind: 'danger',
          onClick: async () => {
            const res = await API.deleteAssistant(record.id);
            if (!res.ok) return toast(res.error, 'bad');
            Modal.close();
            toast('已删除，文件都在', 'good');
            if (ctx) await ctx.refreshBootstrap();
            refresh();
          },
        },
      ],
    });
  }

  /* 把一个人从群里拿出来。
   *
   * 三件事必须说清楚，否则「移出」会变成一个让人后悔的按钮：
   * 1. **群至少两个人。** 剩一个就不是群了，是私聊 —— 那时该解散群。
   * 2. **它这个人还在。** 移出群不等于删角色，屋子、对话、人设一样不动。
   * 3. **正在跑的那一轮先别动。** 成员表和提示词是同一时刻读的，
   *    跑到一半换人，会出现「这轮按三个人算、下一句按两个人算」。
   */
  function removeFromGroup(group, bots, member) {
    const rest = (group.members || []).filter((id) => id !== member.id);
    if (rest.length < 2) {
      return toast('群里至少要留两个人。要把这个群收了，用「解散」', 'bad');
    }
    Modal.open({
      title: '把「' + member.name + '」移出「' + (group.name || '群聊') + '」？',
      subtitle: '只是不在这个群里说话。它这个人、它的屋子、它自己的对话都不动。',
      body: h('div.sNote', '剩下 ' + rest.length + ' 个人：'
        + rest.map((id) => {
          const bot = bots.find((one) => one.id === id);
          return (bot && bot.emoji ? bot.emoji + ' ' : '') + ((bot && bot.name) || id);
        }).join('、')),
      actions: [
        { label: '算了', onClick: () => Modal.close() },
        {
          label: '移出', kind: 'danger',
          onClick: async () => {
            const res = await API.saveGroup(group.id, { members: rest });
            Modal.close();
            if (!res.ok) return toast(res.error, 'bad');
            toast('已把「' + member.name + '」移出这个群', 'good');
            if (ctx) await ctx.refreshBootstrap();
            refresh();
          },
        },
      ],
    });
  }

  /* 成员表：这个群里有谁、还能拉谁进来、谁该走。
     一屏之内把「加人 / 移人」放在一起，因为它们本来就是同一个问题的两种答案。 */
  function openMemberManager(data, group) {
    const bots = data.items || [];
    let ids = (group.members || []).slice();
    const host = h('div.formCol');

    async function apply(next, note) {
      const res = await API.saveGroup(group.id, { members: next });
      if (!res.ok) return toast(res.error, 'bad');
      ids = (res.data && res.data.members) || next;
      group.members = ids.slice();
      toast(note, 'good');
      if (ctx) await ctx.refreshBootstrap();
      paint();          // 弹窗里这张表
      refresh();        // 后面那块「成员 N」也得跟着变
    }

    function row(bot, action) {
      return h('div.mmRow',
        h('span.mmAv', bot.emoji || '🙂'),
        h('div.mmWho',
          h('div.mmName', bot.name),
          h('div.mmTag', bot.tagline || '（还没写定位）')),
        action,
        /* 「移出群」和「删掉这个角色」是两件事，所以是两个按钮，不共用一个 ×：
           前者只是不在这个群里说话，后者是这个人真的没了。 */
        bot.builtin ? null : h('button.mini.danger', {
          title: '删掉「' + bot.name + '」这个角色（它的屋子留着）',
          onclick: () => confirmDeleteAssistant(withStats(bot)),
        }, '删角色'));
    }

    function paint() {
      host.innerHTML = '';
      host.appendChild(h('div.mmSec', '在这个群里 · ' + ids.length + ' 人'));
      ids.forEach((id) => {
        const bot = bots.find((one) => one.id === id);
        if (!bot) return;   // 角色已经删了，名单里的残留不画
        host.appendChild(row(bot, h('button.mini', {
          onclick: () => {
            const rest = ids.filter((one) => one !== id);
            if (rest.length < 2) return toast('群里至少要留两个人', 'bad');
            apply(rest, '已把「' + bot.name + '」移出');
          },
        }, '移出群')));
      });

      const away = bots.filter((one) => !ids.includes(one.id));
      if (away.length) {
        host.appendChild(h('div.mmSec', '还没进来的 · ' + away.length + ' 人'));
        away.forEach((bot) => {
          host.appendChild(row(bot, h('button.mini.acc', {
            onclick: () => apply(ids.concat([bot.id]), '已把「' + bot.name + '」拉进群'),
          }, '＋ 拉进来')));
        });
      } else {
        host.appendChild(h('div.mmSec', '所有智能体都在这个群里了'));
      }

      host.appendChild(h('div.mmNote',
        '群聊至少要两个人 —— 只剩一个的时候它就不是群，是私聊。',
        h('br'),
        '「移出群」只改这个群的名册：它的屋子、它自己的对话都还在，随时能再拉回来。',
        h('br'),
        '「删角色」是把这个人删掉 —— 屋子里的文件也留着，只是不再有这个角色。'));
    }

    paint();
    Modal.open({
      title: '「' + (group.name || '群聊') + '」的成员',
      subtitle: '谁在这个群里说话。加人、移人都在这里，不用去改整张规则表。',
      wide: true,
      body: host,
      actions: [{ label: '好了', kind: 'primary', onClick: () => Modal.close() }],
    });
  }

  async function revealDir(path) {
    if (!path) return;
    const res = await API.reveal(path);
    if (!res.ok) return toast(res.error || '打不开这个位置', 'bad');
  }

  // ------------------------------------------------------------- 安全

  async function buildSecurity(body) {
    const result = await API.security();
    if (!result.ok) return body.appendChild(h('div.empty', result.error));
    const data = result.data || {};
    const ruleInfo = data.rules || {};
    const auditInfo = data.audit || {};
    const dlpInfo = data.dlp || {};

    body.appendChild(h('div.hint', {
      style: { color: 'var(--faint)', fontSize: '11.5px', marginBottom: '10px', lineHeight: '1.6' },
    }, '三层分工：规则是底线（任何权限档位都不许），权限是偏好（你可以放松），审计是留证（只追加、带哈希链）。'
      + '顺序不能反 —— 底线不该因为点了「全权」而变成允许。'));

    // 出站打码
    body.appendChild(h('div.switchRow',
      h('div.meta',
        h('div.nm', '出站打码'),
        h('div.ds', '发给模型之前，把私钥、sk- 开头的 key、JWT、password=… 这类东西换成 ***。'
          + (dlpInfo.patterns ? `认 ${dlpInfo.patterns.length} 种。` : ''))
      ),
      h('button.toggle' + (dlpInfo.enabled ? '.on' : ''), {
        onclick: async (event) => {
          // 同上：await 之后 currentTarget 是 null
          const button = event.currentTarget;
          const next = !button.classList.contains('on');
          button.disabled = true;
          try {
            const res = await API.setDlp(next);
            if (!res.ok) return toast(res.error, 'bad');
            button.classList.toggle('on', next);
            toast(next ? '已打开出站打码' : '已关闭', 'good');
          } finally {
            button.disabled = false;
          }
        },
      })
    ));

    body.appendChild(h('div.sectionTitle', '规则'));
    const counts = ruleInfo.counts || {};
    body.appendChild(h('div.listItem',
      h('div.t', '共 ' + (counts.total || 0) + ' 条'),
      h('div.d', `底线 ${counts.deny || 0} 条（不许）· 待确认 ${counts.gate || 0} 条（先问一句）`)
    ));

    (ruleInfo.rules || []).forEach((rule) => {
      body.appendChild(h('div.listItem',
        h('div.t',
          rule.id,
          h('span.tag' + (rule.action === 'deny' ? '.bad' : ''), { style: { marginLeft: '6px' } },
            rule.action === 'deny' ? '不许' : '先问')),
        h('div.d', rule.why || ''),
        h('div.d', { style: { fontFamily: 'var(--mono)', fontSize: '11px', opacity: 0.75 } },
          rule.kind + '：' + (rule.patterns || []).slice(0, 4).join(' | ')
          + ((rule.patterns || []).length > 4 ? ' …' : '')),
        rule.user ? h('div.row2',
          h('button.mini.danger', {
            onclick: async () => {
              const res = await API.delRule(rule.id);
              if (!res.ok) return toast(res.error, 'bad');
              toast('规则已删', 'good');
              refresh();
            },
          }, '删掉这条')
        ) : null
      ));
    });

    body.appendChild(h('button.ghost.wideBtn', {
      style: { marginTop: '6px' },
      onclick: () => openRuleForm(),
    }, '＋ 加一条自己的规则'));

    body.appendChild(h('div.sectionTitle', '审计'));
    const stats = auditInfo.stats || {};
    const verify = auditInfo.verify || {};
    body.appendChild(h('div.usageGrid', [
      { k: '记录', v: stats.total || 0 },
      { k: '放行', v: (stats.by_verdict || {}).allow || 0 },
      { k: '拦下', v: (stats.by_verdict || {}).deny || 0 },
      { k: '问过', v: (stats.by_verdict || {}).gate || 0 },
    ].map((card) => h('div.usageCard', h('div.k', card.k), h('div.v', String(card.v))))));

    const chain = verify.ok
      ? '哈希链完整：' + (verify.total || 0) + ' 条，一条都没被动过'
      : '链在这里断了：第 ' + (verify.broken_at + 1) + ' 条 —— ' + (verify.why || '');
    body.appendChild(h('div.listItem',
      h('div.t', verify.ok ? '✓ 链是完整的' : '✗ 链断了'),
      h('div.d', chain),
      h('div.d', { style: { fontFamily: 'var(--mono)', fontSize: '11px', opacity: 0.7 } },
        stats.file || '')
    ));

    const entries = (auditInfo.entries || []).slice(-24).reverse();
    if (entries.length) {
      body.appendChild(h('div.sectionTitle', '最近发生过什么'));
      entries.forEach((entry) => {
        body.appendChild(h('div.auditRow',
          h('span.av' + (entry.verdict === 'deny' ? '.bad' : entry.verdict === 'gate' ? '.warn' : ''),
            entry.verdict === 'deny' ? '拦' : entry.verdict === 'gate' ? '问' : '行'),
          h('span.at', entry.actor + ' · ' + entry.action),
          h('span.ag', { title: entry.target || '' }, entry.target || ''),
          h('span.ats', relTime(entry.ts))
        ));
      });
    }

    body.appendChild(h('button.ghost.wideBtn', {
      style: { marginTop: '8px' },
      onclick: () => {
        Modal.open({
          title: '清空审计记录？',
          subtitle: '清空本身也会留一条 —— 否则「链突然变短」这件事永远解释不清。',
          actions: [
            { label: '算了', onClick: () => Modal.close() },
            {
              label: '清空',
              kind: 'danger',
              onClick: async () => {
                const res = await API.clearAudit();
                if (!res.ok) return toast(res.error, 'bad');
                Modal.close();
                toast('删掉 ' + (res.data.removed || 0) + ' 条', 'good');
                refresh();
              },
            },
          ],
        });
      },
    }, '清空审计记录'));
  }

  function openRuleForm() {
    const kind = h('select',
      h('option', { value: 'path' }, '路径 path'),
      h('option', { value: 'command' }, '命令 command'),
      h('option', { value: 'net' }, '网址 net'));
    const action = h('select',
      h('option', { value: 'deny' }, '直接不许'),
      h('option', { value: 'gate' }, '先问一句'));
    const patterns = h('textarea', { rows: 5 });
    patterns.placeholder = '一行一条。路径用 ** 通配，例如：\n**/config/prod.*\nC:/Windows/**';
    const why = h('input', { placeholder: '为什么要拦（会显示给我看）' });

    Modal.open({
      title: '加一条自己的规则',
      subtitle: '这是一道底线：写进这里的，任何权限档位都不会放行（除非你删掉它）。',
      wide: true,
      body: h('div.formCol',
        h('div.fieldRow', h('div.fieldLabel', '类型'), kind),
        h('div.fieldRow', h('div.fieldLabel', '动作'), action),
        h('div.fieldRow', h('div.fieldLabel', '匹配'), patterns),
        h('div.fieldRow', h('div.fieldLabel', '原因'), why)
      ),
      actions: [
        { label: '算了', onClick: () => Modal.close() },
        {
          label: '加上',
          kind: 'primary',
          onClick: async () => {
            const res = await API.addRule({
              kind: kind.value,
              action: action.value,
              patterns: patterns.value,
              why: why.value.trim() || '你加的规则',
            });
            if (!res.ok) return toast(res.error, 'bad');
            Modal.close();
            toast('规则已生效', 'good');
            refresh();
          },
        },
      ],
    });
  }

  // ------------------------------------------------------------- 运行时

  async function buildRuntime(body) {
    const [runtimeResult, channelResult] = await Promise.all([API.runtime(), API.channels()]);
    if (!runtimeResult.ok) return body.appendChild(h('div.empty', runtimeResult.error));
    const data = runtimeResult.data || {};
    const channels = channelResult.ok ? channelResult.data : { items: [] };

    body.appendChild(h('div.hint', {
      style: { color: 'var(--faint)', fontSize: '11.5px', marginBottom: '10px', lineHeight: '1.6' },
    }, '工具里不写死 `python` / `node`：写死的后果是用户机器上那个是微软商店的转发器，一调就弹商店。'
      + '启动后探一次，探到了记住，探不到就老实说这条命令现在不能跑。'));

    body.appendChild(h('div.listItem',
      h('div.t', '当前进程'),
      h('div.d', 'Python ' + ((data.this || {}).python || '?')
        + ' · ' + ((data.this || {}).platform || '')),
      h('div.d', { style: { fontFamily: 'var(--mono)', fontSize: '11px', opacity: 0.7 } },
        (data.this || {}).executable || '')
    ));

    (data.items || []).forEach((item) => {
      body.appendChild(h('div.rtRow' + (item.usable ? '' : '.off'),
        h('span.rtDot'),
        h('div.rtMeta',
          h('div.rtName', item.name,
            h('span.tag', { style: { marginLeft: '6px' } }, item.why || '')),
          h('div.rtPath', { title: item.path || '' },
            item.version ? item.version + ' — ' + shortPath(item.path) : (item.note || '没找到'))
        ),
        h('button.mini', {
          onclick: () => openRuntimePath(item),
        }, item.path ? '换路径' : '指定')
      ));
    });

    body.appendChild(h('button.ghost.wideBtn', {
      style: { marginTop: '6px' },
      onclick: async () => {
        const res = await API.redetect();
        if (!res.ok) return toast(res.error, 'bad');
        toast(res.data.summary || '已重探', 'good');
        refresh();
      },
    }, '重新探一遍'));

    body.appendChild(h('div.sectionTitle', '通道'));
    body.appendChild(h('div.hint', {
      style: { color: 'var(--faint)', fontSize: '11.5px', marginBottom: '8px', lineHeight: '1.6' },
    }, '同一套内核，从三个门进来：桌面窗口、命令行、本地接口。'
      + '一条会话被窗口建、被命令行接着说了两句，是正常用法 —— 所以每个请求都先认门，再干活。'));

    (channels.items || []).forEach((channel) => {
      const isApi = channel.id === 'api';
      body.appendChild(h('div.rtRow',
        h('span.rtDot' + (channel.enabled ? '' : '.idle')),
        h('div.rtMeta',
          h('div.rtName', channel.label),
          h('div.rtPath', channel.why || ''),
          h('div.rtPath', channel.count + ' 次请求'
            + (channel.errors ? '（' + channel.errors + ' 次出错）' : '')
            + (channel.last ? ' · 最后 ' + relTime(channel.last) : ' · 还没用过'))
        ),
        isApi
          ? h('button.mini' + (channel.enabled ? '' : '.acc'), {
            onclick: async () => {
              const res = await API.openApi(!channel.enabled);
              if (!res.ok) return toast(res.error, 'bad');
              toast(!channel.enabled ? '外部接口已开' : '外部接口已关', 'good');
              refresh();
            },
          }, channel.enabled ? '关掉' : '打开')
          : h('span.tag', '常开')
      ));
    });

    if (channels.token) {
      body.appendChild(h('div.tokenBox',
        h('div.tokenLabel', '外部访问令牌'),
        h('div.tokenVal', channels.token),
        h('div.row2',
          h('button.mini', {
            onclick: async () => {
              try {
                await navigator.clipboard.writeText(channels.token);
                toast('已复制', 'good');
              } catch {
                toast('复制不了，手动选一下', 'bad');
              }
            },
          }, '复制'),
          h('button.mini.danger', {
            onclick: async () => {
              const res = await API.rotateToken();
              if (!res.ok) return toast(res.error, 'bad');
              toast('换了一个新令牌，旧的立刻失效', 'good');
              refresh();
            },
          }, '换一个')
        ),
        h('div.tokenNote', '不是本机来的请求必须带 X-DreamRole-Token 这个头。本机回环地址不验 —— '
          + '每次开窗口都要输 token 的产品，用户第一件事是去关掉校验。')
      ));
    }
  }

  function openRuntimePath(item) {
    const input = h('input', {
      placeholder: '完整路径，例如 C:/Python312/python.exe',
      value: item.path || '',
    });
    Modal.open({
      title: '指定 ' + item.name + ' 的位置',
      subtitle: '留空就恢复自动探测。',
      body: h('div.formCol', h('div.fieldRow', h('div.fieldLabel', '路径'), input)),
      actions: [
        { label: '算了', onClick: () => Modal.close() },
        {
          label: '就这个',
          kind: 'primary',
          onClick: async () => {
            const res = await API.setRuntimePath(item.name, input.value.trim());
            if (!res.ok) return toast(res.error, 'bad');
            Modal.close();
            toast('已记住这个路径', 'good');
            refresh();
          },
        },
      ],
    });
  }

  function shortPath(path) {
    if (!path) return '';
    const parts = String(path).replace(/\\/g, '/').split('/').filter(Boolean);
    return parts.length > 3 ? '…/' + parts.slice(-2).join('/') : path;
  }

  return {
    init, open, renderInto, refresh, rerender,
    current() { return current; },
    /* 「＋」菜单 / 深链 / 自检用：直接把「新建自动化」的表单顶上来 */
    newSchedule(prompt) { openScheduleEditor(null, prompt || ''); },
    /* 深链与自检用：直接切到自动化页的某一格（list / runs / tpl） */
    autoTab(id) {
      if (!AUTO_TABS.some((one) => one.id === id)) return false;
      autoTab = id;
      /* 还没画过就别画：深链的顺序是「先定好停在哪一格，再打开面板」，
         这时候多画一次会和紧接着的第一次画撞车 —— 同一页内容写两遍。 */
      if (currentHost) rerender();
      return true;
    },
  };
})();
