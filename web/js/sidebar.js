/* 左栏：一列到底。
 *
 * 以前是「图标条 + 内容列」两段：左边一排只有图标的按钮，右边一列内容。
 * 结果是「这软件到底有哪些功能」得靠悬停一个个认，而且同一件事有两个入口
 * （图标条点一次、内容列里再点一次）。
 *
 * 现在按设计稿改成一条竖列：
 *
 *   品牌行 ─────────────────────────  （版本号 + 宽度）
 *   ＋ 新会话  Ctrl N
 *   ⌕ 搜索会话、命令、文件  Ctrl K
 *   一级导航（带计数）
 *   会话树 / 某一栏的内容
 *   ⚙ 设置 · 我
 *
 * 为什么「一级导航」只放七个：左栏一共 288px 宽、900px 高，把十四栏全平铺开
 * 就没有地方放会话树了 —— 而会话树是这一栏里用得最多的东西。
 * 剩下七栏收在「更多」后面，点一下全展开，位置不变、不另开一层。
 *
 * 「会话」这一行（导航第一项）曾经被省掉，理由是「树就长在导航下面，何必再点
 * 一次」。结果点了「技能」之后树被面板顶掉，而没有任何一行能点回去 ——
 * 那条路只有一个走法叫「重启软件」。现在它排第一，和树头那个「会话」并存：
 * 设计稿里本来也是两个，一个是「切到哪一栏」，一个是「这一栏的标题」。
 */

const Sidebar = (() => {
  let ctx = null;
  let current = 'sessions';
  /* 插件页当前点亮的是哪一格（experts / skills / tools / connectors）。
     空字符串表示「插件」那一格。它只对挂在插件页下面的栏有意义。 */
  let sub = '';
  let moreOpen = false;

  /* 表单多的栏（设置、连接器）硬压在原宽度里，输入框会被挤成一条缝。
     记在这里而不是写进 DOM，切走再切回来还是用户上次定的宽度。 */
  const widened = new Set();

  /* 栏目标。`hidden: true` 表示它不单独占导航一行 —— 现在只有「设置」和
     「插件页里的三个 tab」是这种。`parent` 说明它挂在谁下面：点这些 id
     会落到插件页，并把对应的 tab 点亮，这样深链、命令面板、老代码里的
     `Panels.open('skills')` 全都还能用，不用改一串调用点。 */
  const SECTIONS = [
    { id: 'sessions', label: '会话', nav: false, width: 288 },
    { id: 'search', label: '搜索', tab: 'search', width: 288 },
    { id: 'plugins', label: '插件', tab: 'plugins', width: 320 },
    { id: 'automation', label: '自动化', tab: 'schedules', width: 288 },
    { id: 'assistants', label: '智能体团队', tab: 'assistants', width: 288 },
    { id: 'changes', label: '改动', tab: 'changes', width: 300 },
    { id: 'experts', label: '专家', tab: 'experts', width: 320, hidden: true, parent: 'plugins' },
    { id: 'skills', label: '技能', tab: 'skills', width: 320, hidden: true, parent: 'plugins' },
    { id: 'tools', label: '工具', tab: 'tools', width: 320, hidden: true, parent: 'plugins' },
    { id: 'connectors', label: '连接器', tab: 'mcp', width: 320, hidden: true, parent: 'plugins' },
    { id: 'software', label: '软件', tab: 'software', width: 300 },
    { id: 'memory', label: '记忆', tab: 'memory', width: 300 },
    { id: 'security', label: '安全', tab: 'security', width: 320 },
    { id: 'runtime', label: '运行时', tab: 'runtime', width: 320 },
    { id: 'usage', label: '用量', tab: 'usage', width: 288 },
    { id: 'settings', label: '设置', tab: 'settings', width: 340, hidden: true },
  ];

  /* 一级导航就四行，照设计稿：会话 / 插件 / 自动化 / 智能体团队。
   *
   * 「搜索」不再占一行：上面那个搜索框本来就是它（点开是命令面板），
   * 一上一下两个入口，用户得先想「这俩是不是一个东西」。
   * 「改动」挪进「更多」：它是「回头看一眼」的动作，不是一天点几次的路。
   * 设置走底部齿轮 —— 它一天点不了几次，占一格太贵。 */
  const PRIMARY = ['sessions', 'plugins', 'automation', 'assistants'];
  const SECONDARY = ['changes', 'software', 'memory', 'security', 'runtime', 'usage'];

  /* 每一项前面那个小图标。用 SVG 而不是 emoji：emoji 在不同机器上长得不一样，
     一列里混着苹果脸和安卓脸，看着像拼的。 */
  const ICONS = {
    sessions: '<path d="M4 6h16M4 12h16M4 18h10" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>',
    plugins: '<path d="M9 3v4M15 3v4M7 7h10v3.5a5 5 0 01-10 0zM12 15.5V21" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>',
    search: '<circle cx="10.5" cy="10.5" r="5.5" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M15 15l4.5 4.5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>',
    experts: '<circle cx="12" cy="8" r="3.6" fill="none" stroke="currentColor" stroke-width="1.7"/><path d="M4.5 20c1.4-3.6 4-5.4 7.5-5.4S18.1 16.4 19.5 20" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/>',
    skills: '<path d="M6 4h5v5H6zM13 4h5v5h-5zM6 11h5v5H6zM13 11h5v5h-5z" fill="none" stroke="currentColor" stroke-width="1.6"/>',
    automation: '<circle cx="12" cy="13" r="7" fill="none" stroke="currentColor" stroke-width="1.7"/><path d="M12 9.5V13l2.6 1.8M9 3h6" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/>',
    assistants: '<circle cx="8.5" cy="9" r="3" fill="none" stroke="currentColor" stroke-width="1.7"/><circle cx="16.5" cy="10.5" r="2.4" fill="none" stroke="currentColor" stroke-width="1.7"/><path d="M2.5 20c.9-3.2 3.2-4.8 6-4.8s5.1 1.6 6 4.8M14 15.4c2.4.2 4.2 1.6 4.9 4.6" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/>',
    changes: '<path d="M7 4v16M17 4v16M7 9h5M7 15h5" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/>',
    tools: '<path d="M14.5 4.5a4.5 4.5 0 00-6 6L4 15v4h4l4.5-4.5a4.5 4.5 0 006-6l-2.8 2.8-2.2-2.2z" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/>',
    connectors: '<path d="M9 3v5M15 3v5M7 8h10v3a5 5 0 01-10 0zM12 16v5" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>',
    software: '<rect x="3.2" y="4.2" width="7.6" height="7.6" rx="1.6" fill="none" stroke="currentColor" stroke-width="1.6"/><rect x="13.2" y="4.2" width="7.6" height="7.6" rx="1.6" fill="none" stroke="currentColor" stroke-width="1.6"/><rect x="3.2" y="14.2" width="7.6" height="7.6" rx="1.6" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M17 14.4v7M13.6 18h6.8" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>',
    memory: '<path d="M6 4h11a2 2 0 012 2v14H8a2 2 0 01-2-2z" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/><path d="M6 17h13M10 8h5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>',
    security: '<path d="M12 3l7 3v6c0 4.2-2.9 7.4-7 9-4.1-1.6-7-4.8-7-9V6z" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/><path d="M8.8 12.2l2.2 2.2 4.2-4.4" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>',
    runtime: '<rect x="3" y="4" width="18" height="6" rx="1.6" fill="none" stroke="currentColor" stroke-width="1.7"/><rect x="3" y="14" width="18" height="6" rx="1.6" fill="none" stroke="currentColor" stroke-width="1.7"/><circle cx="7" cy="7" r="1" fill="currentColor"/><circle cx="7" cy="17" r="1" fill="currentColor"/>',
    usage: '<path d="M5 19V11M10 19V6M15 19v-5M20 19V9" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>',
  };

  function init(app) {
    ctx = app;
    $('#newSession').addEventListener('click', () => app.newSession());
    const top = $('#newSessionTop');
    if (top) top.addEventListener('click', () => app.newSession());
    const search = $('#searchBtn');
    if (search) search.addEventListener('click', () => Palette.open(''));
    const gear = $('#gearBtn');
    if (gear) gear.addEventListener('click', () => show('settings'));
    const me = $('#meBtn');
    if (me) {
      me.addEventListener('click', () => {
        // 这里原来什么都没有。点一下至少得说清楚「这是本地软件」，
        // 否则一枚头像按钮点下去毫无反应，用户会以为坏了。
        toast('DreamRole 跑在这台机器上：会话、记忆、改动都在本地数据目录里');
      });
    }
    const wide = $('#sideWide');
    if (wide) {
      wide.addEventListener('click', () => {
        if (widened.has(current)) widened.delete(current);
        else widened.add(current);
        applyWidth(byId(current));
      });
    }
    renderNav();
  }

  function byId(id) {
    return SECTIONS.find((section) => section.id === id) || SECTIONS[0];
  }

  /* 每栏后面的数字。**必须有出处**：编一个数字比不显示更糟 ——
     用户会拿它当准数，然后发现对不上。所以拿不到就空着。 */
  function counts() {
    const boot = (ctx && ctx.state && ctx.state.boot) || {};
    const state = (ctx && ctx.state) || {};
    return {
      skills: (boot.skills || []).length,
      tools: (boot.tools || []).length,
      connectors: Object.keys(boot.mcp || {}).length,
      automation: (boot.schedules || []).length,
      assistants: (boot.assistants || []).length,
      changes: (state.changes || []).length,
      memory: (boot.memory || []).length,
      /* 「会话」这个数要和树里真的画出来的一行一条对得上：开关没开的时候
         归档的会话不在树上，那就别把它算进来 —— 数出来 12、树里 9 条，
         用户只会怀疑自己点错了什么。 */
      /* 「插件」这一格 = 工具 + 连接器。它们在左栏合成了一行，
         数字也得合成一个 —— 分开算的话这一行写哪个都不对。 */
      plugins: (boot.tools || []).length + Object.keys(boot.mcp || {}).length,
      sessions: (state.traces || []).filter((one) => state.showArchived || !one.archived).length,
      traces: (state.traces || []).length,
    };
  }

  function navRow(section, count) {
    const on = section.id === current;
    const node = h('div.nv' + (on ? '.on' : ''), {
      'data-sec': section.id,
      title: section.label,
      onclick: () => show(section.id),
    },
      h('span.nvIc', { html: '<svg viewBox="0 0 24 24" width="14" height="14">'
        + (ICONS[section.id] || '') + '</svg>' }),
      h('span.nvLbl', section.label),
      count ? h('span.nvCnt', String(count)) : null);
    return node;
  }

  function renderNav() {
    const host = $('#nav');
    if (!host) return;
    host.innerHTML = '';
    const n = counts();
    PRIMARY.forEach((id) => {
      const section = byId(id);
      host.appendChild(navRow(section, n[id] || 0));
    });

    const rest = SECONDARY.filter((id) => !(byId(id) || {}).hidden);
    const more = h('div.nvMore', {
      onclick: () => {
        moreOpen = !moreOpen;
        renderNav();
      },
    },
      h('span.nvLbl', moreOpen ? '收起' : '更多'),
      h('span.nvCaret', moreOpen ? '▴' : '▾'));
    host.appendChild(more);
    if (moreOpen) {
      rest.forEach((id) => host.appendChild(navRow(byId(id), n[id] || 0)));
    } else if (rest.includes(current)) {
      // 当前停在「更多」里的某一栏时，这一栏得露出来 —— 否则点到哪儿了看不出来
      host.appendChild(navRow(byId(current), n[current] || 0));
    }
  }

  function applyWidth(section) {
    // 展开一次多给 120px：够放下一排「路径 + 按钮」，又不至于把对话区压没
    const base = section.width || 288;
    const width = widened.has(section.id) ? base + 120 : base;
    const side = $('#side');
    if (side) side.style.width = width + 'px';
    const wide = $('#sideWide');
    if (wide) {
      wide.title = widened.has(section.id) ? '收回到原来的宽度' : '把这一栏放宽一点';
      wide.classList.toggle('on', widened.has(section.id));
    }
  }

  function show(id) {
    const section = byId(id);
    /* 挂在插件页下面的那几栏（专家 / 技能 / 工具 / 连接器）不单独占导航行：
       点它们等于「打开插件页 + 点亮对应 tab」。`current` 记父栏，`sub` 记具体
       是哪一格 —— 重画时两样都要用，否则点完技能再点一次别的，tab 会跳回第一个。 */
    const parent = section.parent ? SECTIONS.find((one) => one.id === section.parent) : null;
    current = parent ? parent.id : section.id;
    sub = parent ? section.id : '';

    document.querySelectorAll('#nav .nv').forEach((node) => {
      node.classList.toggle('on', node.dataset.sec === current);
    });
    // 「更多」里的某一栏被选中时，确保它被展开着（否则高亮的那行会消失）
    if (SECONDARY.includes(current) && !moreOpen) {
      moreOpen = true;
    }
    renderNav();

    const isSessions = current === 'sessions';
    $('#sideTitle').textContent = isSessions ? '会话' : (parent || section).label;
    $('#traceList').classList.toggle('hidden', !isSessions);
    /* 「新会话」不跟着栏切换而隐藏：它是一个全局动作（在哪一栏都可能想开一条），
       而且设计稿里它就常驻在最上面。跟着藏起来会让左栏的高度跳一下。 */
    $('#newSessionTop').classList.toggle('hidden', !isSessions);
    $('#showArchived').classList.toggle('hidden', !isSessions);

    /* 面板页占**中栏**，不再塞进左栏。
       原来这一栏会跟着页面变宽（288 → 340），于是「左栏是导航」这件事就没了：
       插件页一打开，导航、会话树、页面挤在同一列里，页面还只有 320px 宽。
       设计稿里左栏始终是那一条，页面换的是中间。 */
    /* 左栏的宽度只跟「用户有没有把它放宽」有关，不再跟着页面变 */
    applyWidth(byId('sessions'));
    const host = $('#panelHost');
    host.classList.toggle('hidden', isSessions);
    const main = $('#main');
    if (main) main.classList.toggle('page', !isSessions);
    /* 打开一页面板时，右栏那列会话卡（交付物 / 计划 / 本会话）就先让位：
       它讲的是「当前这条对话」，而这一屏已经不是那条对话了。
       团队页/插件页各自有自己的右列，两列并排会变成两条同名的边。 */
    const right = $('#rpanel');
    if (right) right.classList.toggle('hidden', !isSessions);
    if (isSessions) {
      if (ctx && ctx.renderTraces) ctx.renderTraces();
      return;
    }
    if (typeof Panels !== 'undefined') Panels.renderInto(host, (parent || section).tab, sub);
  }

  /* 按「栏的 tab 名」跳转，给 Panels.open() 和深链用（?panel=settings）。
     从 tab 名找不到时什么都不做 —— 猜一个最像的，比不跳更让人糊涂。 */
  function showByTab(tab) {
    const section = SECTIONS.find((one) => one.tab === tab);
    if (section) show(section.id);
  }

  /* 重画当前栏。
   *
   * 这里**不能**走 show(current)：show() 会把 sub 清成 ''，于是在插件页里
   * 改完一个技能、面板一刷新，tab 就从「Skill」跳回「插件」——
   * 用户会以为自己的操作把它踢回去了。 */
  function refresh() {
    renderNav();
    if (current === 'sessions') return;
    const host = $('#panelHost');
    if (host && typeof Panels !== 'undefined') {
      Panels.renderInto(host, byId(current).tab, sub);
    }
    const right = $('#rpanel');
    if (right) right.classList.add('hidden');
  }

  function active() {
    return current;
  }

  return {
    init, show, showByTab, refresh, active, renderNav,
    sections: () => SECTIONS.map((s) => ({ ...s })),
  };
})();
