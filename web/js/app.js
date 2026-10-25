/* 应用主逻辑。
 *
 * 全局只有一个状态对象 `state`，界面永远是它的投影。
 * 数据流：HTTP/SSE → state → render 函数 → DOM。没有反向的 DOM 读状态，
 * 所以不会出现「界面显示的和实际数据不一致」这种最难查的 bug。
 */

const state = {
  boot: null,
  cid: null,
  project: '',
  /* 这个会话额外授权的目录（工作区之外还能去的地方）。每项 {path, mode, missing}，
     mode 是 'ro'（只读）或 'rw'（可写）。跟着会话走，不跟程序走。 */
  grants: [],
  mode: 'craft',
  permission: 'standard',
  think: 'off',      // 深度思考档位 off|low|medium|high
  /* 底层用哪一套：role1 = DreamRole 自己的内核，role2 = 本机 DeepSeek Harness。
     它只决定**下一条新会话**用哪个；已经建好的会话在建的时候就绑死了，
     因为两边的事件格式不一样，中途换等于让模型读一半天书。 */
  engine: 'role1',
  engines: [],       // 两套底层的可用性（装没装、有没有 key），来自 /api/engines
  engineBound: '',   // 当前会话绑的那个引擎（打开会话时从快照里读）
  expert: '',        // 当前绑定的专家 id（空 = 不用专家）
  assistant: '',     // 当前会话属于哪个助理（空 = 普通会话）
  members: [],       // 群聊成员 id；非空就是群聊
  group: '',         // 群聊 id
  modeMeta: [],
  events: [],
  traces: [],
  graph: { cards: [], bands: [], edges: [] },
  cardOpen: null,
  pending: null,
  running: false,
  /* 「正在停下」和「正在跑」是两个状态，不能合成一个。
     按下停下之后服务端还要收尾（杀命令、退出门闩），这中间界面如果直接
     说「已停下」，用户就会去打字、按回车，然后什么也没发生 ——
     他说的「显示已停下但是对话框仍处于不可用状态」就是这一幕。 */
  cancelling: false,
  /* 长命令的心跳：{tool, label, seconds, tail}。装依赖那种一条命令跑几分钟的，
     没有它界面上只有「正在跑…」五个字一动不动，和卡死分不清。 */
  beat: null,
  stream: null,
  live: null,
  view: 'chat',
  templates: [],
  optimize: null,
  showArchived: false,
  nodeIndex: new Map(),
  nearBottom: true,
  todos: [],
  todoOpen: true,     // 待办那块是展开还是收起
  todoHidden: false,  // 用户点了 × ，整块先藏起来（待办内容一变就自动回来）
  todoUser: false,    // 用户手动开合过 —— 之后别再替他自动决定
  todoSig: '',        // 上一次渲染时的待办内容指纹，用来判断「变了没有」
  usage: null,
  jobs: null,        // 定时任务（右栏 / 中栏胶囊共用，5 秒缓存）
  rtab: 'all',       // 右栏只看哪一类：all|art|plan|run|ov
  rightOpen: true,   // 右栏是展开还是收起（记在 localStorage）
  at: null,          // @ 补全的当前状态 {start, query, items, index}
  editing: null,     // 正在改的那条用户消息 {index, eventId}
  skillPick: [],     // 输入栏里选中、只对这一轮生效的技能名
  skillList: [],     // 浮层里那份技能清单（每次打开都重拉）
};

/* ------------------------------------------------------------------ 工具 */

/* 过程图的数据不是在这里拼的 —— 折叠规则在 GraphView.buildCards 里，
   因为它和「卡片长什么样」是同一件事：一张卡 = 一次思考 + 它触发的动作和结果。
   分成两处写，迟早会出现「图上的卡和抽屉里的内容对不上」。 */

function labelOf(name) {
  return (state.boot && state.boot.labels && state.boot.labels[name]) || name || '';
}

const MODE_PERMISSION_LABEL = {
  readonly: '只读', standard: '标准', staged: '分阶段', full: '全权',
};

/* ------------------------------------------------------------------ 顶栏 */

function renderModes() {
  const host = $('#modeSwitch');
  host.innerHTML = '';
  const modes = (state.boot && state.boot.modes) || [];
  modes.forEach((mode) => {
    host.appendChild(h('button' + (mode.id === state.mode ? '.on' : ''), {
      title: mode.hint,
      onclick: () => { switchMode(mode.id); closeMenus(); },
    }, mode.label));
  });
  // 中栏头那枚胶囊跟着说当前模式 —— 它才是「我现在是哪种模式」的常驻答案
  const pill = $('#modePill');
  if (pill) {
    const now = modes.find((mode) => mode.id === state.mode);
    pill.textContent = (now && now.label) || '模式';
    pill.title = (now && now.hint) || '交互模式';
  }
}

function renderPermission() {
  const level = state.permission;
  $('#permLabel').textContent = MODE_PERMISSION_LABEL[level] || level;
  const dot = $('#permDot');
  dot.className = 'dot' + (level === 'readonly' ? ' warn' : level === 'full' ? ' bad' : '');
  $('#permBtn').title = '权限：' + (MODE_PERMISSION_LABEL[level] || level) + '（点击切换）';
}

/* 底层那枚胶囊。
 *
 * 它显示的是「**这一条会话**由谁在跑」，不是一个全局开关 —— 已经建好的会话
 * 换不了（事件格式不一样），所以顶栏上如实显示绑定的那个，而不是用户随手点的那个。
 * 会话还没建（新会话）时，它显示的是「下一条会用哪个」，点一下能改。
 */
const ENGINE_LABELS = { role1: 'Role 1', role2: 'Role 2' };

function engineRow(id) {
  return (state.engines || []).find((row) => row.id === id) || null;
}

function renderEngine() {
  const chip = $('#engineBtn');
  if (!chip) return;
  const bound = state.cid ? (state.engineBound || state.engine) : '';
  const shown = bound || state.engine;
  const row = engineRow(shown);
  $('#engineLabel').textContent = ENGINE_LABELS[shown] || shown;
  const dot = $('#engineDot');
  if (dot) {
    const ready = row ? row.ready : true;
    dot.className = 'engDot' + (ready ? '' : ' bad') + (shown === 'role2' ? ' dsh' : '');
  }
  const ready = row ? row.ready : true;
  chip.classList.toggle('on', shown === 'role2');
  chip.title = bound
    ? `这条会话绑定在 ${ENGINE_LABELS[shown] || shown}${ready ? '' : '（当前不可用：' + (row.note || '') + '）'}`
      + ' —— 建好就换不了，想换开一条新会话'
    : `${ENGINE_LABELS[shown] || shown} · ${row ? row.tools : ''}${ready ? '' : '（不可用：' + (row ? row.note : '') + '）'}`
      + ' —— 点一下换成另一个（对新会话生效）';
}

function openEnginePicker() {
  const rows = state.engines || [];
  const body = h('div.engPick');
  rows.forEach((row) => {
    const bound = state.cid ? (state.engineBound || state.engine) : '';
    const picked = bound ? bound === row.id : state.engine === row.id;
    body.appendChild(h('div.engCard' + (picked ? '.on' : '') + (row.ready ? '' : '.dim'), {
      onclick: () => {
        if (bound) return;                       // 绑死的会话，点了也不该有反应
        state.engine = row.id;
        Modal.close();
        renderEngine();
        toast('下一条新会话用 ' + (ENGINE_LABELS[row.id] || row.id), 'good');
      },
    },
      h('div.engHead',
        h('b', row.label),
        h('span.pill', row.ready ? '可用' : '不可用'),
        picked ? h('span.pill.on', '当前') : null),
      h('div.engMeta', '工具：' + (row.tools || '—')),
      h('div.engMeta', '审批：' + (row.approval || '—')),
      row.model ? h('div.engMeta', '模型：' + row.model) : null,
      row.path ? h('div.engPath', row.path) : null,
      row.note ? h('div.engNote', row.note) : null
    ));
  });

  Modal.open({
    title: '底层内核',
    subtitle: state.cid
      ? '这条会话已经绑定在 ' + (ENGINE_LABELS[state.engineBound || state.engine] || state.engine)
        + ' 上。两套内核的历史格式不一样，中途换会让模型看不懂前面的对话 —— 想换就开一条新会话。'
      : '决定**下一条**会话由谁在跑。建好之后就绑死，不再变。',
    body,
    actions: [{ label: '知道了', onClick: () => Modal.close() }],
  });
}

/* 思考等级的胶囊。它的存在感和权限那枚一样：这是**每一轮**都会影响钱和速度的开关，
   藏进设置里等于没人会去调。off 不点亮，免得看起来「正在思考」而其实没有。 */
const THINK_LABELS = { off: '不思考', low: '浅想', medium: '想一想', high: '深想' };

function renderThink() {
  const level = state.think || 'off';
  const chip = $('#thinkBtn');
  if (!chip) return;
  $('#thinkLabel').textContent = THINK_LABELS[level] || level;
  chip.classList.toggle('on', level !== 'off');
  const info = (state.boot && state.boot.thinking) || {};
  const supported = !!(info.support && Object.keys(info.support).length);
  chip.title = '深度思考：' + (THINK_LABELS[level] || level)
    + '（点击切换）' + (supported ? '' : '· 当前服务商可能不支持');
}

function renderProject() {
  const path = state.project || '';
  const chip = $('#projectBtn');
  if (!path) {
    if ($('#bcPath')) $('#bcPath').textContent = '未设置工作区 / 会话';
    chip.classList.add('warn');
    chip.title = '这个会话还没有工作区。点一下选一个目录，才能开始对话。';
    return;
  }
  chip.classList.remove('warn');
  /* 面包屑只留最后一段。它回答的是「我在哪个项目里」，
     完整路径放进 tooltip —— 挤进一行只会把「会话」两个字顶出去。 */
  if ($('#bcPath')) $('#bcPath').textContent = pathTail(path, 1) + ' / 会话';
  /* 顶栏塞不下授权列表，但「还有别的地方能去」这件事必须看得见 ——
     看不见的权限等于没有边界可言。数量放进 tooltip。 */
  const extra = (state.grants || []).length;
  chip.title = '工作区：' + path +
    (extra ? `，另外还能去 ${extra} 个授权目录` : '') +
    '（点一下换）';
}

/* 顶栏那个小标：当前是哪个专家在看这件事。
   不显示的话，「我明明换了专家它怎么还这么说话」会变成一个玄学问题。 */
function renderExpertChip() {
  const button = $('#expertBtn');
  if (!button) return;
  const list = (state.boot && state.boot.experts) || [];
  const item = state.expert ? list.find((one) => one.id === state.expert) : null;
  button.classList.toggle('hidden', !item);
  if (!item) return;
  $('#expertEmoji').textContent = item.emoji || '🧩';
  $('#expertName').textContent = item.name || '专家';
  button.title = (item.desc || item.name) + '（点击更换专家）';
}

/* ------------------------------------------------------------------ 侧栏 */

/* 归档开关只有一个入口。
 *
 * 之前是「点一下 → 改 state → 改自己的文案 → 重画」，文案和状态分散在
 * 两个地方各自维护；后来空态里也要放一个同样的入口，那种写法立刻就要复制一遍。
 * 现在统一走这里：状态、文案、深链、重画都只在这一处发生。 */
function setShowArchived(flag) {
  state.showArchived = !!flag;
  syncArchivedButton();
  renderTraces();
}

/* 按钮状态跟着开关走。它现在是一枚图标按钮（左栏树头部那个 ⇅），
   所以不写文案，只点亮 + 给个 title —— 往图标按钮里塞文字会把图标顶掉。 */
function syncArchivedButton() {
  const button = $('#showArchived');
  if (!button) return;
  button.classList.toggle('on', state.showArchived);
  button.title = state.showArchived ? '归档的也算进来了（点一下收起）' : '显示已归档的会话';
}

async function renderTraces() {
  /* 归档的会话要不要一起拿，是**这一句**决定的。
     接口默认只回未归档的（`archived=0`）—— 之前这里没传 archived，
     于是「显示已归档」点了等于没点：数据里本来就没有那些会话，前端再怎么过滤也变不出来。
     现在一律要全量、开关只决定画不画。多拿几条本地 JSON 不值钱，
     换回来的是空态能如实说「全归档了」，而不是把用户吓一跳的「还没有会话」。 */
  const result = await API.traces(null, true);
  const host = $('#traceList');
  syncArchivedButton();   // 每次重画都对齐一次，省得文案和状态各说各话
  host.innerHTML = '';

  const items = (result.ok ? result.data : []) || [];
  state.traces = items;
  /* 有会话在后台跑，就定期回头看一眼。
     没有这条，B 会话聊得正起劲，A 早就跑完了，而列表上还挂着「跑」——
     用户要么被骗，要么得一条条点开确认。只在真的有人跑的时候才轮询。 */
  scheduleTracePoll();
  /* 导航上「会话」后面那个数字跟着这一份走。不刷的话会出现「列表 12 条、
     导航写 3」—— 数字编不出来可以空着，但抖出来的错数比不写更糟。 */
  if (typeof Sidebar !== 'undefined' && Sidebar.renderNav) Sidebar.renderNav();
  const list = state.showArchived ? items : items.filter((item) => !item.archived);

  if (!list.length) {
    /* 「没有会话」和「有会话，但都归档了」是两件事。都写成「还没有会话」，
       用户就会以为归档把会话弄丢了 —— 而它其实就在开关后面。 */
    const hidden = items.filter((item) => item.archived).length;
    if (hidden) {
      host.appendChild(h('div.empty', { style: { padding: '22px 8px', fontSize: '12px' } },
        h('div', '全部 ' + hidden + ' 条会话都归档了'),
        h('div', { style: { marginTop: '8px' } },
          h('button.link', {
            style: { font: 'inherit', background: 'none', border: 0, cursor: 'pointer', color: 'var(--accent)', padding: 0 },
            onclick: () => setShowArchived(true),
          }, '显示已归档'))));
      return;
    }
    host.appendChild(h('div.empty', { style: { padding: '22px 8px', fontSize: '12px' } },
      '还没有会话'));
    syncArchivedButton();
    return;
  }

  renderTraceTree(host, list);
}

/* ------------------------------------------------------------------ 会话树
 *
 * 以前这里是三摊：工作区列表、会话列表、归档开关各管一段，同一条会话在两处
 * 出现过两次。设计稿那句需求是「工作区和未分组合成一个」—— 只留一棵树，顶上
 * 「默认」装没选过目录的会话，下面按工作区分组。
 *
 * 三个刻意的选择：
 *
 * 1. **分组是从会话里算出来的**，不另存一张工作区表。一条会话自己记着在哪个
 *    目录，树就照着分。另存一份的话，迟早出现「列表里有这个工作区、点进去
 *    空着」这种空壳 —— 它没人维护，只会越攒越多。
 * 2. **置顶的单独提到最上面一组。** 点了置顶就是要它排第一，藏在某个工作区
 *    里等于没置顶。
 * 3. **工作区的「⋯」里没有删除。** 设计稿那边写的是「删工作区连带删文件夹
 *    和全部对话」—— 那是一条真会让人丢掉整个项目的路，而左栏这棵树只是一层
 *    展示。这里的替代是「从列表里隐藏」：只影响这一栏，磁盘和会话都不动，
 *    而且在同一个菜单里随时放得回来。
 */

const WS_HIDE_KEY = 'dreamrole.hiddenWs';
const WS_FOLD_KEY = 'dreamrole.foldedWs';

/* 折叠 / 隐藏都记在 localStorage 里：它们是「我怎么看这一栏」的偏好，
   不是会话数据，没必要也不应该跟着会话落盘到服务端。 */
function readSet(key) {
  try {
    const raw = JSON.parse(localStorage.getItem(key) || '[]');
    return new Set(Array.isArray(raw) ? raw : []);
  } catch (err) {
    return new Set();
  }
}

function writeSet(key, set) {
  try {
    localStorage.setItem(key, JSON.stringify([...set]));
  } catch (err) {
    /* 无痕模式 / 存储配额满了都会抛。折叠状态丢了不影响任何功能，
       所以这里吞掉，不让一句 localStorage 把整棵树画不出来。 */
  }
}

function relMs(item) {
  return Date.parse(item.updated || item.created || '') || 0;
}

function wsGroups(items) {
  const hidden = readSet(WS_HIDE_KEY);
  const pinned = [];
  const buckets = new Map();

  items.forEach((item) => {
    if (item.pinned) {
      pinned.push(item);
      return;
    }
    const key = item.project || '';
    if (!buckets.has(key)) buckets.set(key, []);
    buckets.get(key).push(item);
  });

  const groups = [];
  if (pinned.length) {
    groups.push({ key: '#pin', label: '置顶', glyph: 'star', path: '', items: pinned, fixed: true });
  }

  const rows = [];
  let hiddenCount = 0;
  buckets.forEach((list, key) => {
    if (key && hidden.has(key)) {
      hiddenCount += 1;
      return;
    }
    /* 时间是这一组的「最近动过」，排序用它 —— 和会话行显示的是同一个时间，
       免得列表顺序和行上的「2 天前」各说各话。 */
    rows.push({
      key,
      label: key ? pathTail(key, 1) : '默认',
      glyph: key ? 'folder' : 'home',
      path: key,
      items: list,
      recent: list.reduce((max, one) => Math.max(max, relMs(one)), 0),
    });
  });

  /* 默认文件夹钉在最前：没选工作区的会话是最常见的一类，找它们不该先翻过
     七个项目。其余按最近活动倒序。 */
  rows.sort((a, b) => {
    if (!a.key || !b.key) return a.key ? 1 : -1;
    return b.recent - a.recent;
  });

  return { groups: groups.concat(rows), hiddenCount };
}

const WS_GLYPH = {
  star: 'M12 3.6l2.5 5.3 5.5.8-4 4 1 5.6-5-2.8-5 2.8 1-5.6-4-4 5.5-.8z',
  home: 'M4 11l8-7 8 7v8a1 1 0 0 1-1 1h-4v-6H9v6H5a1 1 0 0 1-1-1z',
  folder: 'M3 6.5h5.5l1.8 2H21V19H3z',
};

function glyph(path) {
  return {
    html: '<svg viewBox="0 0 24 24" width="12" height="12">'
      + '<path d="' + path + '" fill="none" stroke="currentColor" stroke-width="1.7" '
      + 'stroke-linecap="round" stroke-linejoin="round"/></svg>',
  };
}

/* 一行会话。
 *
 * 设计稿把它压成了一行：圆点 + 标题 + 时间，悬停时时间让位给「⋯」。
 * 我们把 ROLE 标签留在行上（这是用户明确要的）—— 两套底层的能力边界不一样
 * （role2 没有审批卡、技能是 DSH 自己那套），不标出来，用户会觉得
 * 「同一句话怎么这次它不问我了」。
 *
 * 「跑」不再是一个「跑」字，而是行首那颗点变绿脉动：宽度就这么点，一个汉字
 * 的位置足够放三个状态点，而颜色比文字更快被眼睛抓到。 */
function traceRow(item) {
  const role = item.engine === 'role2' ? 'role2' : 'role1';
  return h('div.trace' + (item.cid === state.cid ? '.on' : '') + (item.archived ? '.arch' : ''), {
    title: [
      item.title || '未命名',
      item.project || '没选工作区（在「默认」里）',
      '底层：' + (role === 'role2' ? 'DeepSeek Harness' : 'DreamRole 内核'),
      item.events ? item.events + ' 条事件' : '',
      '右键或点「⋯」：置顶 / 重命名 / 归档 / 删除',
    ].filter(Boolean).join('\n'),
    onclick: () => openTrace(item.cid),
    /* 右键也开菜单：这个列表用户会一直用，右键是最快的那只手 */
    oncontextmenu: (event) => {
      event.preventDefault();
      openTraceMenu(item, event.clientX, event.clientY);
    },
  },
    h('span.td' + (item.running ? '.run' : '')),
    h('span.tTitle', item.title || '未命名'),
    item.archived ? h('span.tag.archTag', '档') : null,
    h('span.tag.eng' + (role === 'role2' ? '.dsh' : ''), {
      title: role === 'role2' ? '底层：DeepSeek Harness' : '底层：DreamRole 内核',
    }, role),
    h('span.tm', relTime(item.updated || item.created)),
    /* 原来这里只有一个 ✕。删除、置顶、归档、改名挤在一行里，每个都只剩
       一个图标，点错一次就少一条会话。收进一个「⋯」。 */
    h('button.more', {
      title: '更多：置顶 / 重命名 / 归档 / 删除',
      onclick: (event) => {
        event.stopPropagation();
        const rect = event.currentTarget.getBoundingClientRect();
        openTraceMenu(item, rect.right - 8, rect.bottom + 4);
      },
    }, '⋯')
  );
}

/* 一行工作区。点整行是折叠 —— 那是这一行上最不伤人的动作：点错了最多
   展开/收起一次，不像「点错就新建一条会话」那样留下垃圾。 */
function wsRow(group, folded) {
  const shut = folded.has(group.key);
  return h('div.wsRow' + (group.fixed ? '.pinGroup' : ''), {
    title: group.fixed
      ? '置顶的会话都提在这里'
      : (group.path || '没选过工作区的会话都在这里') + (shut ? '（已收起）' : ''),
    onclick: () => toggleWsFold(group.key),
    oncontextmenu: (event) => {
      if (group.fixed || !group.path) return;
      event.preventDefault();
      openWsMenu(group, event.clientX, event.clientY);
    },
  },
    h('span.caret', shut ? '▸' : '▾'),
    h('span.wsIc', glyph(WS_GLYPH[group.glyph] || WS_GLYPH.folder)),
    h('span.wsNm', group.label),
    h('span.wsCnt', String(group.items.length)),
    group.fixed ? null : h('span.wsOps',
      h('button.opb', {
        title: group.path ? '在这个工作区里开一条新会话' : '新建会话（还没选工作区）',
        onclick: (event) => {
          event.stopPropagation();
          newSessionInWorkspace(group.path);
        },
      }, '＋'),
      h('button.opb', {
        title: '更多',
        onclick: (event) => {
          event.stopPropagation();
          const rect = event.currentTarget.getBoundingClientRect();
          openWsMenu(group, rect.right - 6, rect.bottom + 4);
        },
      }, '⋯')
    )
  );
}

function renderTraceTree(host, items) {
  const folded = readSet(WS_FOLD_KEY);
  const { groups, hiddenCount } = wsGroups(items);

  groups.forEach((group) => {
    host.appendChild(wsRow(group, folded));
    if (folded.has(group.key)) return;
    /* 组内也按最近活动排 —— 置顶的被提到上面那一组了，所以这里不用再管置顶。
       排序的是 buckets 里那几个新建的数组，不会动到 state.traces 的顺序。 */
    group.items.slice().sort((a, b) => relMs(b) - relMs(a))
      .forEach((item) => host.appendChild(traceRow(item)));
  });

  if (hiddenCount) {
    host.appendChild(h('button.newws.hiddenWs', {
      title: '把之前藏起来的工作区放回这一栏（磁盘上的文件一直没动）',
      onclick: () => {
        writeSet(WS_HIDE_KEY, new Set());
        renderTraces();
        toast('工作区都放回来了', 'good');
      },
    }, '还有 ' + hiddenCount + ' 个工作区藏着 · 放回来'));
  }

  host.appendChild(h('button.newws', {
    title: '选一个目录当新工作区（会话就开在那个目录里）',
    onclick: newWorkspace,
  }, '＋ 新建工作区'));
}

function toggleWsFold(key) {
  const folded = readSet(WS_FOLD_KEY);
  if (folded.has(key)) folded.delete(key);
  else folded.add(key);
  writeSet(WS_FOLD_KEY, folded);
  renderTraces();
}

/* 在这个工作区里开新会话：先清干净当前会话（否则工作区会被设到那条老会话
   头上），再把目录补上。applyWorkspace 在没有 cid 的时候只是把目录记在
   state 上，等第一句话发出去才随建会话一起落盘 —— 这里正好用得上。 */
async function newSessionInWorkspace(path) {
  newSession();
  if (path) await applyWorkspace(path);
  const input = $('#input');
  if (input) input.focus();
}

function newWorkspace() {
  newSession();
  pickProject();
}

function openWsMenu(group, x, y) {
  closeTraceMenu();
  const menu = h('div.traceMenu');
  const push = (label, onClick) => {
    menu.appendChild(h('button.tmItem', {
      onclick: (event) => {
        event.stopPropagation();
        closeTraceMenu();
        onClick();
      },
    }, label));
  };

  push(group.path ? '在这个工作区开新会话' : '新建会话（还没选工作区）',
    () => newSessionInWorkspace(group.path));
  /* 「默认」这一组没有路径 —— 它装的就是「没选过目录的会话」。
     对空路径调 reveal 会弹一个打不开的资源管理器，所以整条菜单项不出现，
     而不是出现之后点了报错。 */
  if (group.path) {
    push('在资源管理器里打开', () => API.reveal(group.path));
  }
  push('收起这一组', () => toggleWsFold(group.key));

  if (group.path) {
    menu.appendChild(h('div.tmSep'));
    push('从列表里隐藏（不动文件）', () => {
      const hidden = readSet(WS_HIDE_KEY);
      hidden.add(group.path);
      writeSet(WS_HIDE_KEY, hidden);
      renderTraces();
      toast('已从列表里隐藏，会话和磁盘上的文件都还在', 'good');
    });
    /* 设计稿 main-ui-tree-actions C 段。这一项之前是刻意不做的（只给「隐藏」），
       理由是「删工作区会连带删掉整个项目」。但躲开不等于解决 ——
       用户还是得自己开资源管理器删目录，然后回来面对一堆指向空目录的会话。
       现在照设计做，把危险摊开在弹窗里：真实路径、要一起删的对话名、
       必须手打一遍工作区名。 */
    menu.appendChild(h('div.tmSep'));
    const kill = h('button.tmItem.danger', {
      onclick: (event) => {
        event.stopPropagation();
        closeTraceMenu();
        confirmWorkspaceDelete(group);
      },
    }, '删除工作区…');
    menu.appendChild(kill);
  }

  document.body.appendChild(menu);
  traceMenuNode = menu;
  const box = menu.getBoundingClientRect();
  menu.style.left = Math.max(8, Math.min(x - box.width, window.innerWidth - box.width - 8)) + 'px';
  menu.style.top = Math.max(8, Math.min(y, window.innerHeight - box.height - 8)) + 'px';
}

/* 删除工作区（设计稿 main-ui-tree-actions C 段）。
 *
 * 三个安全设计，一个都不省：
 *   ① 弹窗里写出**真实绝对路径**和要一起删掉的会话名 —— 让人看清删的是什么；
 *   ② 必须手打一遍工作区名才点亮删除键 —— 手滑点不到；
 *   ③ 目录走系统回收站，不是 rm -rf —— 又一层。
 *
 * 还多一句设计稿里没有的：**先删会话，再移目录**。反过来的话，目录一旦移走，
 * 那些会话就变成一堆指向不存在路径的孤儿行，而它们本来可以一起消失。
 */
function confirmWorkspaceDelete(group) {
  const label = group.label || pathTail(group.path, 1);
  const sessions = (group.items || []).slice();
  const typed = h('input.input', { placeholder: label, autocomplete: 'off' });

  const box = h('div.wsDelBox',
    h('div.wsDelLabel', '将被移进回收站的文件夹'),
    h('div.wsDelPath', { title: group.path }, group.path),
    h('div.wsDelLabel', '将一并删除的对话（' + sessions.length + ' 条）'),
    h('div.wsDelSessions',
      sessions.length
        ? sessions.map((item) => h('div.wsDelSession', '· ' + (item.title || '未命名')))
        : h('div.wsDelNone', '这个工作区下没有对话')));

  const warn = h('div.wsDelWarn',
    h('span', '⚠'),
    h('span', '确认前先把要留的对话导出。文件夹会进系统回收站，'
      + '但回收站可能会被清空 —— 那不是一条能指望的退路。'));

  const actions = [
    { label: '取消', onClick: () => Modal.close() },
    {
      label: '删除工作区与全部对话',
      kind: 'danger',
      onMount: (button) => {
        button.disabled = true;
        button.dataset.arm = '0';
        typed.addEventListener('input', () => {
          const ok = typed.value.trim() === label;
          button.disabled = !ok;
          button.dataset.arm = ok ? '1' : '0';
        });
        button.addEventListener('click', async () => {
          if (typed.value.trim() !== label) return;
          button.disabled = true;
          button.textContent = '正在删…';
          const dead = sessions.map((item) => item.cid);
          let failed = 0;
          for (const cid of dead) {
            const res = await API.deleteTrace(cid);
            if (!res.ok) failed += 1;
            if (state.cid === cid) {
              state.cid = null;
              state.events = [];
              state.pending = null;
              state.running = false;
              state.cancelling = false;
              state.beat = null;
            }
          }
          const moved = await API.trashWorkspace(group.path);
          Modal.close();
          /* 本地那两张集合里还留着这个路径的话，下次同名目录再出现会被直接藏起来 */
          const hidden = readSet(WS_HIDE_KEY);
          const folded = readSet(WS_FOLD_KEY);
          hidden.delete(group.path);
          folded.delete(group.path);
          writeSet(WS_HIDE_KEY, hidden);
          writeSet(WS_FOLD_KEY, folded);

          renderFeed();
          renderPending();
          updateComposer();
          await renderTraces();
          if (typeof Sidebar !== 'undefined' && Sidebar.refresh) Sidebar.refresh();

          if (!moved.ok) {
            toast('对话已删，但文件夹没移走：' + (moved.error || '未知原因'), 'bad');
            return;
          }
          toast(failed
            ? `已移走文件夹，但有 ${failed} 条对话没删掉`
            : `已删除工作区「${label}」和它的 ${sessions.length} 条对话`, failed ? 'bad' : 'good');
        });
      },
    },
  ];

  Modal.open({
    title: '删除工作区「' + label + '」？',
    subtitle: '这会同时删掉磁盘上的这个文件夹，以及该工作区下的 '
      + sessions.length + ' 个对话。',
    width: 560,
    body: h('div.wsDelBody', box, warn,
      h('div.wsDelLabel', '请输入工作区名称以确认'),
      typed,
      h('div.wsDelHint', '名称要完全一致，删除键才会亮')),
    actions,
  });
}

/* ------------------------------------------------------------------ 会话菜单 */

let traceMenuNode = null;

function closeTraceMenu() {
  if (traceMenuNode) {
    traceMenuNode.remove();
    traceMenuNode = null;
  }
}

document.addEventListener('click', (event) => {
  if (traceMenuNode && !traceMenuNode.contains(event.target)) closeTraceMenu();
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') closeTraceMenu();
});

function openTraceMenu(item, x, y) {
  closeTraceMenu();
  const menu = h('div.traceMenu');
  const push = (label, onClick, danger) => {
    menu.appendChild(h('button.tmItem' + (danger ? '.danger' : ''), {
      onclick: (event) => {
        event.stopPropagation();
        closeTraceMenu();
        onClick();
      },
    }, label));
  };

  push(item.pinned ? '取消置顶' : '置顶', () => patchTrace(item, { pinned: !item.pinned }));
  push('重命名', () => renameTrace(item));
  push(item.archived ? '取消归档' : '归档', () => patchTrace(item, { archived: !item.archived }));
  menu.appendChild(h('div.tmSep'));
  push('删除', () => deleteTraceFlow(item), true);

  document.body.appendChild(menu);
  traceMenuNode = menu;
  // 贴着鼠标放，但别掉出窗口
  const box = menu.getBoundingClientRect();
  const left = Math.max(8, Math.min(x - box.width, window.innerWidth - box.width - 8));
  const top = Math.max(8, Math.min(y, window.innerHeight - box.height - 8));
  menu.style.left = left + 'px';
  menu.style.top = top + 'px';
}

async function patchTrace(item, payload) {
  const res = await API.patchTrace(item.cid, payload);
  if (!res.ok) return toast(res.error, 'bad');
  if ('archived' in payload) toast(payload.archived ? '已归档' : '已取消归档', 'good');
  if ('pinned' in payload) toast(payload.pinned ? '已置顶' : '已取消置顶', 'good');
  await renderTraces();
  if (typeof Sidebar !== 'undefined' && Sidebar.refresh) Sidebar.refresh();
}

function renameTrace(item) {
  const input = h('input', { value: item.title || '', placeholder: '给这条对话起个名字' });
  Modal.open({
    title: '重命名',
    subtitle: '名字只影响列表里怎么显示，不改动对话内容。',
    body: h('div.sField', input),
    actions: [
      { label: '取消', onClick: () => Modal.close() },
      {
        label: '保存', kind: 'primary',
        onClick: async () => {
          const title = input.value.trim();
          if (!title) return toast('名字不能是空的', 'bad');
          Modal.close();
          await patchTrace(item, { title });
        },
      },
    ],
  });
  setTimeout(() => input.focus(), 40);
}

/* 删除前先问代价。
 *
 * 为什么不能直接删：删掉一条会话会连带**删掉**它名下的改动记录和轮次记录，
 * 而那些东西是「把 agent 改过的文件退回去」的唯一凭据。用户点删除的时候
 * 手上没有任何信息判断代价，等他发现退不回去了，已经晚了。
 *
 * 记录是真删，不是搬去别处藏着 —— 所以这段确认文字必须说「一并删除」，
 * 不能说「失效」。说不清楚代价的确认框，等于没有确认。 */
async function deleteTraceFlow(item) {
  const res = await API.traceImpact(item.cid);
  if (!res.ok) return toast(res.error, 'bad');
  const impact = res.data || {};
  if (impact.running) return toast('这一轮还在跑，先停下再删', 'bad');

  const changes = Number(impact.changes || 0);
  const checkpoints = Number(impact.checkpoints || 0);
  const files = impact.files || [];
  const title = impact.title || item.title || '未命名';

  const body = h('div');
  if (changes || checkpoints) {
    body.appendChild(h('div.sNote', '会一并删除 ',
      h('b', changes + ' 条改动记录'),
      ' 和 ', h('b', checkpoints + ' 个轮次记录'),
      '。删掉之后，这些文件就再也没法退回之前的版本了。'));
    if (files.length) {
      body.appendChild(h('div.sNote', { style: { marginTop: '6px' } },
        '涉及：' + files.slice(0, 5).map((p) => p.split(/[\\/]/).pop()).join('、')
        + (files.length > 5 ? ' 等 ' + files.length + ' 个文件' : '')));
    }
  } else {
    body.appendChild(h('div.sNote', '这条对话没改过文件，删掉不影响磁盘上的任何东西。'));
  }
  body.appendChild(h('div.sNote', { style: { marginTop: '6px', opacity: 0.75 } },
    '轨迹文件会进回收站，不是彻底销毁 —— 但应用里不会再看到它。'));

  Modal.open({
    title: '删除「' + title + '」？',
    subtitle: '它名下的改动记录和轮次记录会一起删掉',
    body,
    actions: [
      { label: '取消', onClick: () => Modal.close() },
      {
        label: '删除', kind: 'danger',
        onClick: async () => {
          const done = await API.deleteTrace(item.cid);
          Modal.close();
          if (!done.ok) return toast(done.error, 'bad');
          if (state.cid === item.cid) {
            // 当前会话没了，界面不能停在一条已经不存在的数据上
            state.cid = null;
            state.events = [];
            state.pending = null;
            state.running = false;
            state.cancelling = false;
            state.beat = null;
            renderFeed();
            renderPending();
            updateComposer();
            if (Artifacts.isOpen()) Artifacts.close();
          }
          await renderTraces();
          // 改动栏正开着的话，里面少了一条对话，必须重画
          if (typeof Sidebar !== 'undefined' && Sidebar.refresh) Sidebar.refresh();
          const swept = (done.data && done.data.swept) || {};
          const count = Number(swept.changes || 0) + Number(swept.history || 0);
          toast(count ? `已删除，${count} 条记录一并清掉` : '已删除', 'good');
        },
      },
    ],
  });
}


/* ------------------------------------------------------------------ 会话 */

async function openTrace(cid) {
  if (state.cid === cid) {
    if (Artifacts.isOpen()) Artifacts.close();
    return;
  }
  if (state.stream) state.stream.close();
  state.stream = null;

  const result = await API.getSession(cid);
  if (!result.ok) return toast(result.error, 'bad');

  const data = result.data;
  state.cid = cid;
  // 这条会话没有工作区就是没有。不能退回上一条会话的目录 ——
  // 那样界面显示 A 目录、实际会话是空的，发出去只会收到 412。
  state.project = data.project || '';
  // 授权目录也是「这条会话记着的东西」，和 project 一起读、一起清
  state.grants = data.grants || [];
  state.mode = data.mode || state.mode;
  state.expert = data.expert || '';
  state.assistant = data.assistant || '';
  state.members = data.members || [];
  state.group = data.group || '';
  state.think = data.think || 'off';
  // 引擎是从快照里读的：一条老会话没有这个字段就是 role1。
  // 读它不只是为了显示标签 —— 顶栏那枚 chip 也得跟着变成这条会话真正在用的那个。
  state.engine = data.engine || 'role1';
  state.engineBound = state.engine;
  state.permission = data.effective || data.permission || state.permission;
  state.events = data.events || [];
  state.pending = (data.pending && data.pending[0]) || null;
  state.running = !!data.running;
  /* 换到另一条会话，「正在停」和心跳都属于上一条 —— 留着会把新会话的
     状态条写成「正在停下…」，而新会话根本没在停。 */
  state.cancelling = false;
  state.beat = null;
  state.todos = data.todos || [];
  // 换会话 = 换一份待办：上一条收着，不该连这一条一起收
  state.todoOpen = true;
  state.todoHidden = false;
  state.todoUser = false;
  state.todoSig = '';
  state.usage = data.usage || null;

  renderProject();
  renderModes();
  renderPermission();
  renderThink();
  renderEngine();
  renderExpertChip();
  renderFeed();
  renderTodos();
  renderChatBar();
  refreshGraph();
  renderPending();
  updateComposer();
  renderTraces();
  connectStream(cid, true);
  // 换会话了，右上角的产物数得跟着换 —— 留着上一条会话的数字是在骗人
  if (Artifacts.isOpen()) Artifacts.render();
  Artifacts.sync();
}

function connectStream(cid, fresh) {
  if (state.stream) state.stream.close();
  state.stream = null;

  /* 静态快照模式：带上 ?still=1 就不连事件流，只把已有事件画出来。
     截图、打印、或者用无头浏览器跑验收时用得上 —— 常开的 SSE 连接会让
     这类工具一直等「页面加载完成」。 */
  if (new URLSearchParams(location.search).has('still')) return;

  if (fresh) {
    state.events = [];
    state.nodeIndex.clear();
  }
  state.stream = API.stream(
    cid,
    (message) => handleStream(message),
    (alive) => {
      if (!alive) $('#usageHint').textContent = '连接断开，正在重连…';
    }
  );
}

function handleStream(message) {
  switch (message.kind) {
    case 'event': {
      const event = message.event;
      if (state.events.some((item) => item.id === event.id)) return;   // 重连时会补发历史
      state.events.push(event);
      if (event.type === 'reasoning') clearReasonLive();
      if (event.type === 'user' || event.type === 'think' || event.type === 'answer') clearLive();
      if (event.payload && event.payload.todos_update) {
        state.todos = event.payload.todos || [];
        renderTodos();
      }
      appendEvent(event);
      refreshGraph();
      renderChatBar();
      scheduleTraceRefresh();
      break;
    }
    case 'reset':
      /* 重试 / 改一句再发之后，服务端把后半个轨迹删了。
         本地不跟着删就会显示一堆「已经不存在」的内容。
         做法不是自己算该删到哪条，而是**重连一次事件流** ——
         服务端订阅时会补发当前全部历史，正好是我们要的那份。
         用重连而不是拉快照，是为了避免「拉快照的这段时间里新事件漏掉」。 */
      state.events = [];
      state.nodeIndex.clear();
      state.todos = [];
      state.editing = null;
      clearLive();
      renderFeed();
      renderTodos();
      refreshGraph();
      if (state.cid) connectStream(state.cid, false);
      break;
    case 'delta':
      // 开始说正文了，思维链就不该再挂着「正在想」
      if (reasonNode) clearReasonLive();
      showLive(message.text);
      break;
    case 'reason':
      showReasonLive(message.text);
      break;
    case 'toolbeat':
      /* 工具跑久了报一句「还在跑」。这不是事件，是脉搏：
         不写进轨迹、不占一行对话，只更新状态条那几个字。
         装依赖卡在「正在跑…」上一动不动，和真的卡死是分不清的。 */
      state.beat = {
        tool: message.tool || '',
        label: message.label || '',
        seconds: Number(message.seconds) || 0,
        tail: message.tail || '',
      };
      paintStatusHint();
      break;
    case 'pending':
      state.pending = message.request;
      renderPending();
      break;
    case 'pending_gone':
      if (state.pending && state.pending.id === message.id) {
        state.pending = null;
        renderPending();
      }
      break;
    case 'state': {
      const was = state.running;
      state.running = !!message.running;
      /* 这一轮真的收尾了，「正在停下」才算数。在这之前它一直是 true ——
         用户按了停下但命令还没被杀掉，那会儿让他发消息是没有意义的
         （服务端会用 409 挡回来），所以界面得先说「正在停」。 */
      if (!state.running) {
        state.cancelling = false;
        state.beat = null;
      }
      if (message.usage) state.usage = message.usage;
      updateComposer();
      renderChatBar();
      // 「正在跑」这件事在图上是一张会呼吸的卡，所以状态变了图也要重画
      if (state.view === 'graph') renderGraph();
      if (!state.running) {
        clearLive();
        renderTraces();
        /* 一轮跑完才会有新产物。只在「跑完」这个转折点上刷新，
           不是每次 state 事件都刷 —— 那样面板会在用户眼皮底下反复重建。 */
        if (was) {
          Artifacts.sync();
          if (Artifacts.isOpen()) Artifacts.render();
          /* 一轮刚做完，日志抽屉里一定多了几条（工具判定、审批）。
             收起状态下它是每 15 秒问一次，这里补一次实时的 —— 用户刚抬头看。 */
          if (typeof Dock !== 'undefined') Dock.refresh();
          /* 这一轮里 agent 很可能刚用 skill_create 写了个技能。面板正开着就立刻
             重画 —— 不然用户还得切走再切回来，而「明明加了却看不见」正是
             上一版最让人困惑的地方。 */
          if (typeof Sidebar !== 'undefined' && Sidebar.active() === 'skills') Sidebar.refresh();
        }
      }
      break;
    }
    default:
      break;
  }
}

const scheduleTraceRefresh = debounce(() => renderTraces(), 1200);

/* 后台还有会话在跑时，每 4 秒回来看一眼列表 —— 只为了那个「跑」标。
   不用 SSE 是因为 SSE 只订阅**当前这一条**会话；而这里要知道的是
   「另外几条有没有跑完」。一条会话跑完时用户在别的会话里，没人会通知他。 */
let tracePollTimer = null;

function scheduleTracePoll() {
  const anyRunning = (state.traces || []).some((item) => item.running);
  if (!anyRunning) {
    if (tracePollTimer) {
      clearTimeout(tracePollTimer);
      tracePollTimer = null;
    }
    return;
  }
  if (tracePollTimer) return;      // 已经排上了，别叠
  tracePollTimer = setTimeout(() => {
    tracePollTimer = null;
    if (!(state.traces || []).some((item) => item.running)) return;
    renderTraces();
  }, 4000);
}

/* ------------------------------------------------------------------ 对话流 */

function feedScrolled() {
  const feed = $('#feed');
  state.nearBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 80;
}

function scrollFeed(force) {
  const feed = $('#feed');
  if (force || state.nearBottom) feed.scrollTop = feed.scrollHeight;
}

function renderFeed() {
  const feed = $('#feed');
  feed.innerHTML = '';
  state.nodeIndex.clear();

  // 没有工作区就不给对话入口：这里不是「先聊着、等会儿再选目录」——
  // 它连文件都读不到，问它问题只会得到一段编出来的回答。
  if (!state.project) {
    feed.appendChild(noWorkspaceState());
    return;
  }
  if (!state.cid) {
    feed.appendChild(emptyState());
    return;
  }
  if (!state.events.length) {
    feed.appendChild(emptyState());
    return;
  }
  state.events.forEach((event) => appendEvent(event, true));
  scrollFeed(true);
}

/* 工作区的规矩只有这一份文案。
 *
 * 以前空态卡片和选目录弹窗各写各的：一边是「每个会话各自的」，一边是
 * 「这个目录只归这条会话用」。说的是同一件事，字面却不一样 ——
 * 同一件事两种说法，比说错更容易让人糊涂。
 * 收敛到这儿之后，以后改规矩只改一处。 */
function workspaceRules() {
  return h('ul.wsRules',
    h('li', h('b', '默认只在这个目录里'), '读写文件。目录外面的地方，要在这张弹窗里单独开一张「通行证」。'),
    h('li', h('b', '每个会话各自一个'), '，不会从上一条继承。新开的对话要重新选。'),
    h('li', h('b', '随时能换'), '：顶栏那个显示目录名的按钮就是。')
  );
}

/* 每个会话都有自己的工作区；没选之前，对话是被挡住的状态。
   这里要说清三件事：为什么挡住、它会动哪些文件、怎么解开。 */
function noWorkspaceState() {
  const box = h('div.empty.noWorkspace',
    h('div.nwIcon', '📁'),
    h('div.nwTitle', '这个会话还没有工作区'),
    h('div.nwText',
      'agent 只在一个目录里读写文件，所以得先告诉它「在哪干活」。'
    ),
    workspaceRules()
  );

  box.appendChild(h('button.primary.nwBtn', { onclick: () => pickProject() }, '选择工作区'));

  const recent = ((state.boot && state.boot.settings && state.boot.settings.recent_projects) || [])
    .filter(Boolean).slice(0, 4);
  if (recent.length) {
    box.appendChild(h('div.nwRecent',
      h('div.nwRecentTitle', '最近用过 · 点一下直接设为这条会话的工作区'),
      ...recent.map((path) => h('button.nwChip', {
        title: path,
        onclick: () => applyWorkspace(path),
      }, pathTail(path, 2)))
    ));
  }
  return box;
}

/* 路径缩写：盘符 + 省略号 + 最后几段。
 *
 * 原来是「…/最后两段」。问题在于它把**盘符**丢掉了，于是
 *   C:\Users\you\AppData\Local\Temp\a\demo  →  …/a/demo
 *   D:\work\a\demo                            →  …/a/demo
 * 两个完全不同的目录缩出来一模一样，用户没法判断点下去会去哪。
 * 盘符留着、中间省略，是最短又能定位的写法。 */
function pathTail(path, keep = 2) {
  const raw = String(path || '').trim();
  if (!raw) return '';
  const sep = raw.includes('\\') ? '\\' : '/';
  const head = (raw.match(/^[A-Za-z]:[\\/]?/) || [''])[0];
  const rest = raw.slice(head.length);
  const parts = rest.split(/[\\/]/).filter(Boolean);
  if (!parts.length) return head || raw;
  const tail = parts.slice(-keep).join(sep);
  return head + (parts.length > keep ? '…' + sep : '') + tail;
}

function shortPath(path) {
  return pathTail(path, 2);
}

function emptyState() {
  const tips = state.mode === 'teach'
    ? ['带我做一个留言板页面，从建文件开始一步步来', '教我看懂这个项目的目录结构', '我想学 git，用这个仓库当例子']
    : state.mode === 'ask'
      ? ['这个项目是干什么的？入口在哪', 'grep 一下哪里用了环境变量', '解释一下这个函数为什么要这么写']
      : ['帮我看看这个项目的结构，说一下从哪读起', '把 README 补一下，写清怎么跑起来', '这个报错是什么原因：……'];

  const box = h('div.empty',
    h('div', { style: { fontSize: '15px', color: 'var(--text-2)', marginBottom: '6px' } },
      state.mode === 'teach' ? '教学模式：一步一步来' : '说一句话，就开始做了'),
    h('div', { style: { fontSize: '12px' } }, '试试这些：')
  );
  tips.forEach((tip) => {
    box.appendChild(h('div', { style: { marginTop: '8px' } },
      h('button.ghost', {
        style: { fontSize: '12px' },
        onclick: () => { $('#input').value = tip; autoGrow($('#input')); $('#input').focus(); },
      }, tip)));
  });
  return box;
}

function avatarOf(type) {
  return { user: '你', reasoning: '思', think: '想', answer: '答', notice: '提' }[type] || '·';
}

function appendEvent(event, bulk) {
  if (state.nodeIndex.has(event.id)) return;
  const feed = $('#feed');
  if (feed.firstChild && feed.firstChild.classList && feed.firstChild.classList.contains('empty')) {
    feed.innerHTML = '';
  }

  if (event.type === 'action') {
    const card = renderAction(event);
    feed.appendChild(card);
    state.nodeIndex.set(event.id, card);
  } else if (event.type === 'result') {
    attachResult(event);
  } else if (event.payload && event.payload.gate && state.nodeIndex.has(event.parent_id)) {
    // 「等你确认」这件事发生在工具执行**之前**，所以要塞进那张卡片的顶上。
    // 单独起一行的话，读起来会变成「已经删完了 → 请你确认要不要删」。
    const card = state.nodeIndex.get(event.parent_id);
    const box = card.querySelector('.toolCard');
    const line = h('div.gateLine', h('span.i', '⏸'), event.content.replace(/，等你确认。$/, ''));
    box.insertBefore(line, box.querySelector('.toolOut'));
    state.nodeIndex.set(event.id, line);
  } else if (event.payload && event.payload.stage_marker) {
    feed.appendChild(renderStageDivider(event));
    state.nodeIndex.set(event.id, feed.lastChild);
  } else if (event.type === 'user') {
    /* 用户自己的话可以改。index 是「第几条用户消息」，服务端按这个找 ——
       它和事件下标不是一回事，中间夹着一堆工具事件。 */
    let index = 0;
    for (const item of state.events) {
      if (item.id === event.id) break;
      if (item.type === 'user') index += 1;
    }
    /* 用户气泡靠右。这是对话页最基本的一条分层规则：右边 = 我说的，
       左边 = 它说的。以前两边都挂头像、都靠左，读起来像两份并列的文档 ——
       而且头像在左会先把气泡往右推，长段落反而比回答还窄。 */
    const clock = event.ts
      ? new Date(event.ts * 1000).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
      : '';
    const row = h('div.row.user',
      h('div.stack',
        h('div.bubble', { html: escapeHtml(event.content).replace(/\n/g, '<br>') }),
        h('div.uMeta',
          h('span.uTime', clock),
          h('div.rowOps',
            h('button', { title: '改这句，然后从这里重来', onclick: () => startEdit(index, event) }, '改一下'),
            h('button', { title: '从这里另起一条新对话，这条留着', onclick: () => branchFrom(index) }, '分叉'),
            h('button', { title: '复制', onclick: () => copyText(event.content) }, '复制')
          )
        )
      )
    );
    feed.appendChild(row);
    state.nodeIndex.set(event.id, row);
  } else if (event.type === 'reasoning') {
    /* 思维链落成一行折叠。以前它和回答一样是一整块气泡 —— 回看一条长会话时，
       答案被一大段灰字压在下面，滚半天看不见结论。 */
    const node = reasonRow(event);
    feed.appendChild(node);
    state.nodeIndex.set(event.id, node);
  } else {
    /* 群聊里每句话都得标是谁说的。不标的话，屏幕上就是一串没有主语的
       观点，用户根本接不上「刚才那句是谁在反对谁」。 */
    const member = (event.payload && event.payload.member) || null;
    const stack = h('div.stack');
    if (member) {
      stack.appendChild(h('div.speaker',
        h('span.se', member.emoji || '🙂'),
        h('span.sn', member.name || '')));
    }
    stack.appendChild(h('div.bubble', { html: markdown(event.content) }));
    const row = h('div.row.' + event.type,
      h('div.avatar', member ? (member.emoji || '🙂') : avatarOf(event.type)),
      stack
    );
    feed.appendChild(row);
    state.nodeIndex.set(event.id, row);
    /* 轮次摘要：一个回合说到这儿就完了，给一行「✓ 已完成 · 用时 8 秒」。
       没有它的话，最后一条回答和「还在想」长得一样 —— 用户只能靠输入框里的
       按钮变没变去判断，那太间接了。群聊里每人各说一句，加上摘要会变成一串
       尾巴，所以只在单人会话里加。 */
    if (event.type === 'answer' && !member) {
      feed.appendChild(turnTail(event));
    }
  }
  if (!bulk) scrollFeed();
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast('已复制', 'good');
  } catch {
    toast('复制不了，浏览器不让', 'bad');
  }
}

/* 把用户那句话放回输入框里改，改完按 Enter 就是从这句重来。
   不做弹窗编辑：改长文本要的是输入框本身（它会自动长高、有 @ 补全）。 */
function startEdit(index, event) {
  const input = $('#input');
  input.value = event.content;
  autoGrow(input);
  updateComposer();
  input.focus();
  const end = input.value.length;
  input.setSelectionRange(end, end);
  state.editing = { index };
  toast('改完按 Enter，就从这句重来', 'good');
}

/* 这一轮完没完。
 *
 * 用「最后一条用户消息」的时间当起点：服务端的事件里本来就有 `ts`，
 * 而一轮的边界就是这两条之间。算不出来就不写用时 —— 写个 0 秒比不写更糟，
 * 它会被当成「一下就答完了」。 */
function turnTail(event) {
  const users = (state.events || []).filter((one) => one.type === 'user');
  const last = users.length ? users[users.length - 1] : null;
  const seconds = last && last.ts && event.ts ? Math.max(1, Math.round(event.ts - last.ts)) : 0;
  let spent = '';
  if (seconds) {
    spent = seconds < 60
      ? '用时 ' + seconds + ' 秒'
      : '用时 ' + Math.floor(seconds / 60) + ' 分 ' + (seconds % 60) + ' 秒';
  }
  /* 挂 `.row` 而不只是 `.turnTail`：对齐交给 `.row`（它的 padding + max-width
     就是正文那一套）。自己算一套边距的话，摘要行会和正文差十几个像素错开。 */
  return h('div.row.turnTail',
    h('span.ttOk', '✓ 已完成'),
    spent ? h('span', '· ' + spent) : null);
}

function renderStageDivider(event) {
  const stage = Number(event.stage || 0);
  const title = (event.content || '').replace(/^阶段\s*\d+\s*·\s*/, '');
  return h('div.stageDivider',
    h('div.line'),
    h('span.label', h('span.idx', '阶段 ' + stage), title),
    h('div.line')
  );
}

/* 工具卡左边那枚图标。以前是一个「做」字徽章 —— 一整屏下来十几个「做」，
   每个都占 26px，读起来像一份填空题。一枚 11px 的图标说同样的事，
   而省下的宽度全给了参数摘要（那才是用户真正想扫的东西）。 */
const TOOL_GLYPH = '<svg viewBox="0 0 24 24" width="11" height="11">'
  + '<path d="M14.5 4.5a4.5 4.5 0 00-6 6L4 15v4h4l4.5-4.5a4.5 4.5 0 006-6l-2.8 2.8-2.2-2.2z" '
  + 'fill="none" stroke="currentColor" stroke-width="1.9" stroke-linejoin="round"/></svg>';

/* 耗时怎么读：不到一秒的不写「0.0s」，写「快」。这个字比任何小数都准。 */
function fmtDur(ms) {
  if (ms < 900) return '很快';
  const seconds = ms / 1000;
  if (seconds < 10) return seconds.toFixed(1) + 's';
  if (seconds < 60) return Math.round(seconds) + 's';
  const minutes = Math.floor(seconds / 60);
  return minutes + 'm' + String(Math.round(seconds % 60)).padStart(2, '0') + 's';
}

function renderAction(event) {
  const payload = event.payload || {};
  const label = payload.label || labelOf(event.tool) || event.tool;
  const detail = payload.detail || '';

  /* 一行：图标 / 名称 / 参数（等宽，可以被截）/ 耗时 / ✓ */
  const head = h('div.toolHead',
    h('span.toolBadge', { html: TOOL_GLYPH }),
    h('span.toolName', label),
    h('span.toolArgs', detail),
    h('div.grow'),
    h('span.toolDur', ''),
    h('span.toolCaret', ''),
    h('span.toolState', '进行中…')
  );

  const output = h('pre.toolOut.hidden', '');
  // busy：卡片左边会有一道流动的光，直到结果回来。没有它的话，
  // 一个跑了十秒的命令和一条卡死的事件看起来一模一样。
  const card = h('div.toolCard.busy', head, output);
  /* 起点记在卡片自己身上，不放进 state：一条会话里可能有几十个调用并发，
     放在一个全局变量里，后开始的会把先开始的覆盖掉，算出来的耗时全是错的。 */
  card.dataset.start = String(Date.now());

  head.addEventListener('click', () => {
    if (!output.textContent) return;
    const open = output.classList.toggle('hidden');
    caret.textContent = open ? '▸' : '▾';
  });

  const caret = head.querySelector('.toolCaret');
  return h('div.toolRow', { dataset: { id: event.id } }, card);
}

function attachResult(event) {
  const parent = state.nodeIndex.get(event.parent_id);
  const payload = event.payload || {};
  const text = event.content || '';

  if (parent) {
    const output = parent.querySelector('.toolOut');
    const status = parent.querySelector('.toolState');
    const caret = parent.querySelector('.toolCaret');
    const card = parent.querySelector('.toolCard');
    if (card) card.classList.remove('busy');

    if (output) {
      output.textContent = text;
      // 短结果直接摊开；超过三行的先收起来，不然一次读文件就把整屏吃掉了
      const lines = text.split('\n').length;
      const big = lines > 3 || text.length > 260;
      output.classList.toggle('hidden', big);
      if (caret) caret.textContent = big ? '▸' : '▾';
    }
    if (status) {
      const ok = payload.ok !== false;
      /* 只留一个 ✓ / ✗。字数挪进 title —— 它是「想核对的时候才看」的信息，
         常年摆在一行末尾会把参数摘要挤掉一半。 */
      status.textContent = ok ? '✓' : '✗';
      status.title = ok ? `${text.length} 字` : '失败了，展开看原因';
      status.className = 'toolState ' + (ok ? 'ok' : 'bad');
    }
    const dur = parent.querySelector('.toolDur');
    const started = Number((card && card.dataset.start) || 0);
    if (dur && started) dur.textContent = fmtDur(Date.now() - started);
    return;
  }

  const row = h('div.row.notice',
    h('div.avatar', '果'),
    h('div.stack', h('div.bubble', markdown(text)))
  );
  $('#feed').appendChild(row);
  state.nodeIndex.set(event.id, row);
}

let liveNode = null;
function showLive(piece) {
  const feed = $('#feed');
  if (!liveNode) {
    clearLive();
    liveNode = h('div.row.think',
      h('div.avatar', '想'),
      h('div.stack', h('div.bubble.streaming', ''))
    );
    feed.appendChild(liveNode);
  }
  const bubble = liveNode.querySelector('.bubble');
  bubble.dataset.raw = (bubble.dataset.raw || '') + piece;
  bubble.innerHTML = markdown(bubble.dataset.raw);
  scrollFeed();
}

function clearLive() {
  if (liveNode) {
    liveNode.remove();
    liveNode = null;
  }
  clearReasonLive();
}

/* 思维链。它和回答是两件事：一边是它自己想明白的过程，一边是它决定告诉你的结论。
 *
 * 但「过程」不该占正文那么大地方 —— 以前它是一整块气泡，一条想得多的回答
 * 前面能压半屏灰字，用户每次都得先滚过它才看见答案。现在按设计稿压成一行：
 *   ✳ 已思考 8 秒 · 展开
 * 想看的人点开，不想看的人一个字都不用读。流式的时候这一行会跟着长字数，
 * 那是「它还在动」的证据 —— 静悄悄的一行会让人以为卡住了。 */
let reasonNode = null;
let reasonStart = 0;

function reasonLine(node) {
  return h('div.thinkLine', { onclick: () => toggleReason(node) },
    h('span.tlIc', '✳'),
    h('span.tlTx', '思考过程'),
    h('span.tlCaret', '▸'));
}

function toggleReason(node) {
  if (!node) return;
  const bubble = node.querySelector('.bubble.reason');
  if (!bubble) return;
  const shut = bubble.classList.toggle('hidden');
  node.classList.toggle('open', !shut);
  const caret = node.querySelector('.tlCaret');
  if (caret) caret.textContent = shut ? '▸' : '▾';
}

/* 从历史里读回来的推理事件，也压成同一行 —— 不然「这一轮」在实时和回看
   两种情况下长得不一样，用户会以为中间丢了一段。 */
function reasonRow(event) {
  const text = event.content || '';
  const ms = Number((event.payload || {}).ms || 0);
  const node = h('div.row.reasoning',
    h('div.stack',
      h('div.thinkLine', { onclick: () => toggleReason(node) },
        h('span.tlIc', '✳'),
        h('span.tlTx', ms ? '已思考 ' + Math.round(ms / 1000) + ' 秒 · 展开' : '思考过程 · 展开'),
        h('span.tlCaret', '▸')),
      h('div.bubble.reason.hidden', text)));
  return node;
}

function showReasonLive(piece) {
  const feed = $('#feed');
  if (!reasonNode) {
    reasonStart = Date.now();
    reasonNode = h('div.row.reasoning.streaming',
      h('div.stack',
        reasonLine(reasonNode),
        h('div.bubble.reason.hidden', '')
      )
    );
    /* reasonLine 拿到的是当时的 reasonNode（还是 null）—— 这一步之后再补上
       点击处理器，不然点开的是上一轮那一行。 */
    reasonNode.querySelector('.thinkLine').onclick = () => toggleReason(reasonNode);
    feed.appendChild(reasonNode);
  }
  const bubble = reasonNode.querySelector('.bubble.reason');
  bubble.dataset.raw = (bubble.dataset.raw || '') + piece;
  bubble.textContent = bubble.dataset.raw;
  const tx = reasonNode.querySelector('.tlTx');
  if (tx) tx.textContent = '正在想… ' + bubble.dataset.raw.length + ' 字';
  scrollFeed();
}

function clearReasonLive() {
  if (!reasonNode) return;
  const bubble = reasonNode.querySelector('.bubble.reason');
  const raw = (bubble && bubble.dataset.raw) || '';
  /* 一个字都没说的（模型直接给了答案）就别留一行空壳 ——
     「已思考 0 秒」比不显示更像 bug。 */
  if (!raw.trim()) {
    reasonNode.remove();
    reasonNode = null;
    return;
  }
  const seconds = Math.max(1, Math.round((Date.now() - reasonStart) / 1000));
  reasonNode.classList.remove('streaming');
  reasonNode.classList.remove('open');
  if (bubble) bubble.classList.add('hidden');
  const tx = reasonNode.querySelector('.tlTx');
  if (tx) tx.textContent = '已思考 ' + seconds + ' 秒 · 展开';
  const caret = reasonNode.querySelector('.tlCaret');
  if (caret) caret.textContent = '▸';
  /* 节点留在页面上，只把引用放掉：下一次思考会另起一行，
     而这一行现在是一条「已经想过什么」的历史。 */
  reasonNode = null;
}

/* ------------------------------------------------------------------ 待确认 */

function renderPending() {
  const host = $('#pendingHost');
  host.innerHTML = '';
  const request = state.pending;
  if (!request) return;

  const isBreak = request.kind === 'stage';
  const card = h('div.gate' + (isBreak ? '.break' : ''));

  if (isBreak) {
    card.appendChild(h('div.gateTitle', `第 ${request.stage} 阶段做完了，要进「${request.next_title}」吗？`));
    card.appendChild(h('div', {
      style: { fontSize: '12px', color: 'var(--text-2)' },
    }, '教学模式会在每个阶段之间停下来。你说继续，它才往下走。'));
  } else {
    card.appendChild(h('div.gateTitle', `要执行「${request.label || request.tool}」，需要你点头`));
    if (request.reason) {
      card.appendChild(h('div', { style: { fontSize: '12px', color: '#c2b48e' } }, request.reason));
    }
    if (request.detail) card.appendChild(h('div.gateDetail', request.detail));
  }

  const row = h('div.gateRow');
  (request.options || []).forEach((option) => {
    const kind = option.value === 'deny' || option.value === 'stop'
      ? 'bad'
      : option.value === 'allow' || option.value === 'continue' ? 'good' : '';
    row.appendChild(h('button.btn' + (kind ? '.' + kind : ''), {
      onclick: () => answerPending(option.value),
    }, option.label));
  });
  card.appendChild(row);
  host.appendChild(card);
}

async function answerPending(decision) {
  if (!state.pending || !state.cid) return;
  const id = state.pending.id;
  state.pending = null;
  renderPending();
  const result = await API.answer(state.cid, id, decision);
  if (!result.ok) toast(result.error, 'bad');
}

/* ------------------------------------------------------------------ 待办 */

function todosSignature(items) {
  return items.map((item) => (item.status || 'pending') + '|' + (item.text || '')).join('\n');
}

function renderTodos() {
  const host = $('#todosHost');
  const items = state.todos || [];
  host.innerHTML = '';
  if (!items.length) {
    host.classList.add('hidden');
    state.todoSig = '';
    state.todoOpen = true;
    state.todoHidden = false;
    state.todoUser = false;
    return;
  }

  /* 内容变了才重新决定「该展开还是收起」。
     判断放在这里而不是每帧算，是因为用户可能在中途自己收起来看别的东西，
     下一格进度一跳就把它顶开，等于白收。 */
  const sig = todosSignature(items);
  if (sig !== state.todoSig) {
    state.todoSig = sig;
    state.todoHidden = false;               // 待办一变，被他关掉的那块就该回来
    if (!state.todoUser) {
      // 全部做完了就自动收成一行：那时候它只剩「6/6」这一个信息，
      // 再占六行是把输入框往上顶，纯属占地方。
      state.todoOpen = !items.every((item) => item.status === 'done');
    }
  }
  if (state.todoHidden) {
    host.classList.add('hidden');
    return;
  }
  host.classList.remove('hidden');
  host.classList.toggle('folded', !state.todoOpen);

  const done = items.filter((item) => item.status === 'done').length;

  const toggle = () => {
    state.todoOpen = !state.todoOpen;
    state.todoUser = true;
    renderTodos();
  };

  host.appendChild(h('div.todosHead', {
    onclick: toggle,
    title: state.todoOpen ? '收起（Alt+T）' : '展开（Alt+T）',
  },
    h('span.caret'),
    h('span', `待办 ${done}/${items.length}`),
    h('span.bar', h('i', { style: { width: (done / items.length) * 100 + '%' } })),
    h('button.todoX', {
      title: '先收起来 —— 待办下次有变化时它会自己回来',
      onclick: (event) => {
        event.stopPropagation();
        state.todoHidden = true;
        renderTodos();
      },
    }, '×')
  ));

  if (!state.todoOpen) return;

  items.forEach((item) => {
    const mark = { done: 'x', doing: '~' }[item.status] || ' ';
    host.appendChild(h('div.todoItem.' + (item.status || 'pending'),
      h('span.bx', `[${mark}]`),
      h('span', item.text)
    ));
  });
}

function toggleTodos() {
  if (!(state.todos || []).length) return;
  if (state.todoHidden) {
    state.todoOpen = true;
    state.todoHidden = false;
    state.todoUser = true;
  } else {
    state.todoOpen = !state.todoOpen;
    state.todoUser = true;
  }
  renderTodos();
}

/* ------------------------------------------------------------------ 工具条 */

function renderChatBar() {
  const info = $('#chatInfo');
  const has = !!state.cid;
  $('#retryBtn').classList.toggle('hidden', !has);
  $('#exportBtn').classList.toggle('hidden', !has);

  /* 中栏头：上面是「在哪个工作区的哪条会话」，下面是标题。
     标题只从会话自己记着的 meta 里取 —— 不拿第一句话现拼。
     用户改过标题之后再打开，看到的应该是他改的那个名字。 */
  const mine = (state.traces || []).find((one) => one.cid === state.cid);
  const title = (mine && mine.title) || '新会话';
  const titleNode = $('#chatTitle');
  if (titleNode) titleNode.textContent = title;
  const crumb = $('#bcPath');
  if (crumb) {
    const where = state.project ? pathTail(state.project, 1) : '默认';
    crumb.textContent = where + ' / 会话';
    if (state.project) crumb.title = state.project;
    else crumb.removeAttribute('title');
  }
  renderChatPills();

  if (!has) {
    info.textContent = state.project ? '新会话 · 还没开始' : '先选一个工作区';
    renderRight();
    return;
  }
  const steps = state.events.filter((event) => event.type === 'user').length;
  const usage = state.usage || null;
  /* 左边是「这条会话把上下文用到哪儿了」，右边是「这一条会话的账」。
     以前两边都写「N 轮 · token」，同一个数字在一行里出现两次。 */
  const win = windowTokens();
  if (usage && usage.total) {
    const pct = Math.min(99, Math.round(usage.total / win * 100));
    info.textContent = `${steps} 轮 · 上下文 ${pct}%`;
    info.title = `已用 ${usage.total} / 窗口 ${win} token`;
  } else {
    info.textContent = `${steps} 轮`;
    info.title = '';
  }
  const ctx = $('#ctxHint');
  if (ctx) {
    ctx.textContent = usage && usage.total
      ? `上下文 ${Math.min(99, Math.round(usage.total / win * 100))}%`
      : '';
  }
  $('#usageHint').textContent = state.running || state.cancelling
    ? statusHintText()
    : (steps
      ? `本会话 ${steps} 轮${usage && usage.total ? ' · ' + fmtTok(usage.total) + ' tok' : ''}`
      : '');
  renderSpeakerChip();
  scheduleRight();
}

/* 上下文窗口：设置里选了就用选的，没选按 128k 估。
   「空 = 按模型名猜」那条规则在后端，这里只负责把百分比画出来。 */
function windowTokens() {
  const raw = String((state.boot && state.boot.settings
    && state.boot.settings.context_window) || '').toLowerCase();
  const match = /^(\d+(?:\.\d+)?)\s*([km])?$/.exec(raw);
  if (!match) return 128000;
  const n = Number(match[1]);
  return Math.round(n * (match[2] === 'm' ? 1000000 : match[2] === 'k' ? 1000 : 1));
}

function fmtTok(n) {
  const num = Number(n) || 0;
  return num >= 1000 ? (num / 1000).toFixed(1).replace(/\.0$/, '') + 'k' : String(num);
}

/* 标题右边那几枚胶囊。只有真的数得出来的才画：
   几个智能体在、计划走到第几步、几条自动化挂着。
   一个都没有的时候整条是空的 —— 不摆三枚「0」装作很热闹。 */
function renderChatPills() {
  const host = $('#chatPills');
  if (!host) return;
  host.innerHTML = '';
  const boot = state.boot || {};
  const bots = (boot.assistants || []).length;
  if (bots) {
    host.appendChild(h('button.cPill', {
      title: '本地注册的智能体（点开助理页）',
      onclick: () => Sidebar.show('assistants'),
    }, h('i.cPillDot'), `${bots} 个智能体`));
  }
  const todos = state.todos || [];
  if (todos.length) {
    const done = todos.filter((one) => one.status === 'done').length;
    host.appendChild(h('button.cPill', {
      title: '这一轮的计划进度（点开右栏计划卡）',
      onclick: () => setRightTab('plan'),
    }, `计划 ${done}/${todos.length}`));
  }
  const jobs = state.jobs || [];
  const on = jobs.filter((one) => one.enabled !== false).length;
  if (on) {
    host.appendChild(h('button.cPill', {
      title: '挂着的自动化任务（点开自动化页）',
      onclick: () => Sidebar.show('automation'),
    }, `${on} 个定时任务`));
  }
}

/* ------------------------------------------------------------------ 右栏
 *
 * 四张卡：交付物 / 计划·步骤 / 概览 / 本会话。
 *
 * 设计稿里右栏是**常驻**的一列，不是一个点一下从右边飘出来的抽屉。
 * 产物和计划是要一边干活一边瞄的东西 —— 每次都要点开看一眼、再点回去，
 * 等于没有。抽屉留着（顶栏那枚立方体还开着它），但常用信息在这儿。
 *
 * 每张卡都只放**这一屏真的知道**的东西：没有的整张卡不画，不摆一个
 * 「暂无数据」的空盒子占位 —— 那比不画更像坏了。
 */
let rightTimer = null;
function scheduleRight() {
  clearTimeout(rightTimer);
  // 一次回合里 renderChatBar 会被调很多次，每次去拉一遍产物接口是浪费
  rightTimer = setTimeout(() => { renderRight(); }, 160);
}

/* 右栏顶上那排图标：只看某一类。`all` 是默认，其余各留一张卡。
   选中的格子记在 state 里，重画之后还是同一格 —— 不然每来一条事件，
   用户刚点的「只看计划」就被弹回「全部」。 */
function setRightTab(kind) {
  state.rtab = kind || 'all';
  const head = $('#rhead');
  if (head) {
    Array.from(head.querySelectorAll('.rtab')).forEach((btn) => {
      btn.classList.toggle('on', btn.dataset.card === state.rtab);
    });
  }
  applyRightFilter();
}
function applyRightFilter() {
  const host = $('#rcards');
  if (!host) return;
  const kind = state.rtab || 'all';
  Array.from(host.children).forEach((card) => {
    card.classList.toggle('hidden', kind !== 'all' && card.dataset.card !== kind);
  });
}

/* 右栏整体收起。收起来之后靠中栏头那枚方按钮再打开 ——
   不能只剩「收起」这一个方向的动作，收进去就再也点不出来是最常见的坑。 */
function setRightOpen(open) {
  const panel = $('#rpanel');
  if (!panel) return;
  panel.classList.toggle('min', !open);
  state.rightOpen = !!open;
  try { localStorage.setItem('dreamrole.rightOpen', open ? '1' : '0'); } catch (err) { /* 隐身模式 */ }
  const btn = $('#rpanelBtn');
  if (btn) btn.classList.toggle('on', !!open);
}

function rc(kind, title, extra) {
  return h('div.rc', { 'data-card': kind },
    h('div.rcH', h('span.rcT', title), h('div.grow'), extra || null));
}

async function renderRight() {
  const host = $('#rcards');
  if (!host) return;
  host.innerHTML = '';

  /* 1. 交付物 —— 这条会话写过的文件。点卡片打开完整的产物抽屉。 */
  const art = rc('art', '交付物');
  host.appendChild(art);
  if (!state.cid) {
    art.appendChild(h('div.rcNote', '开一条会话之后，它写过的文件会一条条出现在这里。'));
  } else {
    const res = await API.artifacts(state.cid);
    const items = (res.ok && res.data && (res.data.items || res.data)) || [];
    const list = Array.isArray(items) ? items : [];
    if (!list.length) {
      art.appendChild(h('div.rcNote', '还没产出文件。它写文件或截图之后会出现在这儿。'));
    } else {
      list.slice(0, 3).forEach((item) => {
        art.appendChild(h('button.rcRow', {
          title: item.path,
          onclick: () => { if (typeof Artifacts !== 'undefined') Artifacts.open(); },
        },
          h('span.rcRowIc', { html: '<svg viewBox="0 0 24 24" width="12" height="12">'
            + '<path d="M6 3h7l5 5v13H6z" fill="none" stroke="currentColor" stroke-width="1.7" '
            + 'stroke-linejoin="round"/><path d="M13 3v5h5" fill="none" stroke="currentColor" '
            + 'stroke-width="1.7" stroke-linejoin="round"/></svg>' }),
          h('span.rcRowT', item.name || item.rel || '文件'),
          item.exists === false ? h('span.tag.bad', '已不在') : null));
      });
      art.appendChild(h('button.rcMore', {
        onclick: () => { if (typeof Artifacts !== 'undefined') Artifacts.open(); },
      }, list.length > 3 ? `还有 ${list.length - 3} 个 · 看全部` : '看全部'));
    }
  }

  /* 2. 计划 / 步骤 —— 这一轮它给自己排的活儿。没有就不画这张卡。 */
  const todos = state.todos || [];
  if (todos.length) {
    const done = todos.filter((one) => one.status === 'done').length;
    const plan = rc('plan', '计划 / 步骤', h('span.rcX', done + '/' + todos.length));
    plan.appendChild(h('div.rcBar', h('i', { style: { width: Math.round(done / todos.length * 100) + '%' } })));
    todos.slice(0, 5).forEach((one) => {
      const ok = one.status === 'done';
      plan.appendChild(h('div.rcStep' + (ok ? '.done' : ''),
        h('span.stDot', ok ? '✓' : '·'),
        h('span.stTx', one.title || one.text || '')));
    });
    if (todos.length > 5) plan.appendChild(h('div.rcNote', `还有 ${todos.length - 5} 步，在输入框上方`));
    host.appendChild(plan);
  }

  /* 3. 自动化 —— 挂着的定时任务和它们下次什么时候跑。
     这里不叫「后台 / 终端」：这台机器上没有常驻终端进程，硬画一个
     `pwd 空闲` 的假终端，比不画更坏。 */
  const jobs = await loadJobs();
  if (jobs.length) {
    const on = jobs.filter((one) => one.enabled !== false).length;
    const run = rc('run', '自动化', h('span.rcX', `${on} 个在跑`));
    jobs.slice(0, 3).forEach((job) => {
      const paused = job.enabled === false;
      run.appendChild(h('button.rcRow', {
        title: job.prompt || job.title || '',
        onclick: () => Sidebar.show('automation'),
      },
        h('span.rcRowT', job.title || '未命名任务'),
        h('span.rcRowV' + (paused ? '.off' : ''), paused ? '已暂停' : nextShort(job))));
    });
    if (jobs.length > 3) {
      run.appendChild(h('button.rcMore', { onclick: () => Sidebar.show('automation') },
        `还有 ${jobs.length - 3} 个 · 去自动化页`));
    }
    host.appendChild(run);
  }

  /* 4. 概览 —— 这台机器上一共多少会话、几个工作区。 */
  const traces = state.traces || [];
  const loose = traces.filter((one) => !one.project).length;
  const spaces = new Set(traces.map((one) => one.project).filter(Boolean));
  const overview = rc('ov', '概览');
  overview.appendChild(h('div.rcKv', h('span', '默认文件夹'), h('b', loose + ' 个对话')));
  overview.appendChild(h('div.rcKv', h('span', '工作区'), h('b', spaces.size + ' 个')));
  overview.appendChild(h('div.rcKv', h('span', '对话总计'), h('b', traces.length + ' 条')));
  host.appendChild(overview);

  /* 5. 本会话 —— 谁在跑、能动什么、在哪个目录。 */
  if (state.cid) {
    const here = rc('ov', '本会话');
    here.appendChild(h('div.rcKv', h('span', '内核'), h('b', engineRow(state.engine || 'role1'))));
    here.appendChild(h('div.rcKv', h('span', '权限'), h('b', (state.boot && state.boot.labels
      && state.boot.labels.permissions && state.boot.labels.permissions[state.permission]) || state.permission || '标准')));
    if (state.project) {
      here.appendChild(h('button.rcRow', { title: '换工作区', onclick: pickProject },
        h('span.rcRowT.rcMono', pathTail(state.project, 2))));
    } else {
      here.appendChild(h('div.rcKv', h('span', '工作区'), h('b', '还没选')));
    }
    host.appendChild(here);
  }

  applyRightFilter();
  // 定时任务是这里才拉到手的，胶囊画的时候可能还没数据 —— 补画一次。
  renderChatPills();
}

/* 定时任务列表：右栏和中栏胶囊都要用，但一秒拉一次没意义，缓存 5 秒。 */
let jobsAt = 0;
async function loadJobs() {
  const now = Date.now();
  if (state.jobs && now - jobsAt < 5000) return state.jobs;
  const res = await API.schedules();
  const list = (res.ok && res.data && (res.data.jobs || res.data)) || [];
  state.jobs = Array.isArray(list) ? list : [];
  jobsAt = now;
  return state.jobs;
}

/* 下次触发：这一格只有七八十像素，写全 `10-25 00:46` 只会被截成 `10-25 00...`。
   今天和明天的写时间，更远的写月/日，完整的留在 title 里。
   后端已经算好了 `next_label`（`%m-%d %H:%M`），直接拿它切，不重新解释一遍时间。 */
function nextShort(job) {
  if (!job) return '—';
  if (job.freq === 'interval') return `每 ${job.minutes || 30} 分`;
  const label = job.next_label || '';
  const match = /^(\d{2})-(\d{2}) (\d{2}):(\d{2})$/.exec(label);
  if (!match) return label || '待定';
  const now = new Date();
  const sameDay = Number(match[1]) === now.getMonth() + 1 && Number(match[2]) === now.getDate();
  return sameDay ? `${match[3]}:${match[4]}` : `${match[1]}/${match[2]}`;
}

/* 这条会话是谁在跟你说话。普通会话不显示 —— 不是每条对话都需要一个名牌。 */
function renderSpeakerChip() {
  const host = $('#speakerChip');
  if (!host) return;
  host.innerHTML = '';
  const boot = state.boot || {};
  if (state.members && state.members.length) {
    const group = (boot.groups || []).find((g) => g.id === state.group);
    const all = boot.assistants || [];
    const people = state.members
      .map((id) => all.find((a) => a.id === id))
      .filter(Boolean)
      .map((a) => a.emoji || '🙂');
    host.appendChild(h('div.speakerChip.group',
      h('span.emoji', people.join('')),
      h('span.name', (group && group.name) || '群聊'),
      h('span.note', `${state.members.length} 位`)
    ));
    host.classList.remove('hidden');
    return;
  }
  const own = (boot.assistants || []).find((a) => a.id === state.assistant);
  if (own) {
    host.appendChild(h('div.speakerChip',
      h('span.emoji', own.emoji || '🙂'),
      h('span.name', own.name),
      h('span.note', own.tagline || '助理')
    ));
    host.classList.remove('hidden');
    return;
  }
  host.classList.add('hidden');
}

async function retryLast() {
  if (!state.cid) return toast('还没有会话', 'bad');
  if (state.running) return toast('这一轮还在跑', 'bad');
  const result = await API.retry(state.cid);
  if (!result.ok) return toast(result.error, 'bad');
  toast('重跑上一句');
}

async function editMessage(index, text) {
  if (!state.cid) return;
  if (state.running) return toast('这一轮还在跑', 'bad');
  const result = await API.editTurn(state.cid, index, text);
  if (!result.ok) return toast(result.error, 'bad');
  toast('从这句重来');
}

/* 分叉：原来那条一个字都不动，另外开一条走到这里的。
   想试两种做法的时候用这个，别用「改一下」—— 那个是把原来那条删了重走。 */
async function branchFrom(index) {
  if (!state.cid) return;
  if (state.running) return toast('这一轮还在跑', 'bad');
  const result = await API.branch(state.cid, index);
  if (!result.ok) return toast(result.error, 'bad');
  await renderTraces();
  await openTrace(result.data.cid);
  toast('已经分出一条新对话');
}

function openExport() {
  if (!state.cid) return toast('还没有会话', 'bad');
  const body = h('div');
  const options = [
    { fmt: 'md', name: 'Markdown', desc: '带过程图，能贴进文档、PR 描述里' },
    { fmt: 'html', name: '单页 HTML', desc: '双击就能看，不依赖这个软件' },
    { fmt: 'mermaid', name: 'Mermaid', desc: '只有图，贴到任何支持它的地方' },
    { fmt: 'json', name: '原始事件流', desc: '给程序读的，包含每条事件的完整字段' },
  ];
  options.forEach((option) => {
    body.appendChild(h('div.listItem',
      h('div.t', option.name),
      h('div.d', option.desc),
      h('div.row2',
        h('button.mini', {
          onclick: () => {
            window.open(API.exportUrl(state.cid, option.fmt), '_blank');
            Modal.close();
          },
        }, '下载'),
        h('button.mini', {
          onclick: async () => {
            const text = await API.exportRaw(state.cid, option.fmt);
            try {
              await navigator.clipboard.writeText(text);
              toast('已复制', 'good');
            } catch {
              Modal.open({
                title: option.name,
                body: h('pre.code', text),
                wide: true,
                actions: [{ label: '关掉', kind: 'primary', onClick: () => Modal.close() }],
              });
            }
          },
        }, '复制')
      )
    ));
  });
  Modal.open({
    title: '导出这次会话',
    subtitle: '导出的是一份能直接读的东西，不是把 JSON 丢给你。',
    body,
    actions: [{ label: '关掉', onClick: () => Modal.close() }],
  });
}

/* ------------------------------------------------------------------ 过程图 */

function refreshGraph() {
  state.graph = { cards: GraphView.buildCards(state.events) };
  renderGraph();
}

/* ?card=2 直接把第 3 张卡打开。截图和「报错时说链接」都用得上：
   「你看这张卡」比「第三步那个卡片你点一下」准确得多。 */
let cardAutoOpened = false;

function autoOpenCard(layout) {
  if (cardAutoOpened || !layout) return;
  const raw = new URLSearchParams(location.search).get('card');
  if (raw === null) return;
  const card = (layout.cards || [])[Number(raw)];
  if (!card) return;
  cardAutoOpened = true;
  GraphView.toggle(card);
}

function renderGraph() {
  const host = $('#graphHost');
  if (!host) return null;
  const layout = GraphView.render(host, state.graph.cards || [], {
    running: state.running,
    follow: $('#followToggle') ? $('#followToggle').checked : true,
    timelineHost: $('#graphTimeline'),
    onOpen: (card) => renderCardDrawer(card),
    /* 双击节点 = 跳到会话流里那一条。两张视图看的是同一串事件，
       在图上认出「就是这一步」之后，最想干的事就是回去看它的上下文。 */
    onLocate: (event) => locateEvent(event),
  });

  const lanes = layout.lanes.length;
  const cards = layout.cards.length;
  $('#graphTitle').textContent = cards
    ? `${lanes} 轮 · ${cards} 个节点`
    : '还没有节点';
  autoOpenCard(layout);
  return layout;
}

/* 工具条上的四个筛选。关掉 = 这一类节点不画（连同它的连线一起让位），
   不是把它画淡 —— 画淡了图还是一样满，「筛掉」这件事就没意义了。 */
const GRAPH_FILTERS = [
  ['filterTool', 'tool'],
  ['filterSub', 'sub'],
  ['filterAppr', 'appr'],
  ['filterOut', 'out'],
];

function bindGraphFilters() {
  GRAPH_FILTERS.forEach(([id, type]) => {
    const button = document.getElementById(id);
    if (!button) return;
    button.addEventListener('click', () => {
      const on = !button.classList.contains('on');
      button.classList.toggle('on', on);
      GraphView.setFilter(type, on);
    });
  });
}

/* 图上滚轮缩放之后，右上角那个百分比也要跟着变 —— 否则你会以为自己还在 100% */
GraphView.onZoom = (z) => {
  const label = $('#zoomLabel');
  if (label) label.textContent = Math.round(z * 100) + '%';
};

/* 点开一个节点：右边那块「节点详情」列出这一步里的全部事件。
   动作的 arguments 原样铺出来（JSON 缩进），因为「它到底拿什么参数去做的」
   是这种情况唯一能查的东西 —— 概括成一句话就等于查不了。 */
const CARD_EVENT_LABEL = {
  user: '用户', think: '思考', action: '动作', result: '结果', answer: '回答', notice: '提示',
};

function renderCardDrawer(card) {
  const box = $('#cardDrawer');
  if (!box) return;
  // 这个节点可能已经开着了，只是内容又长了一截 —— 别把人家读到的位置顶回顶部
  const kept = box.classList.contains('hidden')
    ? 0
    : ((box.querySelector('.cardDrawerBody') || {}).scrollTop || 0);
  state.cardOpen = card || null;
  if (!card) {
    box.classList.add('hidden');
    box.innerHTML = '';
    return;
  }

  box.innerHTML = '';
  box.appendChild(h('div.cardDrawerHead',
    h('span.cardDrawerKind', '节点详情'),
    h('span.cardDrawerStage', card.stageTitle || ''),
    h('button.cardDrawerClose', {
      title: '关掉（Esc，或者再点一次那个节点）',
      onclick: () => GraphView.close(),
    }, '✕')
  ));

  const body = h('div.cardDrawerBody');
  const meta = GraphView.metaOf(card);
  const status = GraphView.statusOf ? GraphView.statusOf(card) : { text: '' };

  /* 头三行是「这是什么」，不是日志：
     一条读代码的节点真正想知道的就是「哪一轮、第几步、什么时候」。 */
  body.appendChild(h('div.insTitle',
    /* headOf 自带图标，这里别再拼一枚 —— 拼了就成「⚙ ⚙ 看目录」 */
    GraphView.headOf(card),
    status.text ? h('span.badge2' + (status.bad ? '.bad' : ''), status.text) : null));
  const first = (card.events || [])[0];
  body.appendChild(h('div.insSub',
    `第 ${card.round} 轮 · ${card.stageTitle || '—'}`
    + (first && first.ts
      ? ' · ' + new Date(first.ts * 1000).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
      : '')));
  body.appendChild(h('div.insKv', h('span', '节点'), h('b', meta.label)));
  if (card.kind === 'step') {
    body.appendChild(h('div.insKv', h('span', '动作'), h('b', `${card.rows.length} 个`)));
    body.appendChild(h('div.insKv', h('span', '事件'), h('b', `${card.events.length} 条`)));
  }
  /* 耗时与 token（设计稿 A3 检查器那两行）。
     耗时按事件时间戳首尾算，是真的；token 只有「领走了某次模型调用用量」的
     节点才有，没有就写「未记」—— 按整轮总数摊到每一步是编数据。 */
  const cost = GraphView.costOf(card);
  body.appendChild(h('div.insKv', h('span', '耗时'), h('b', GraphView.secText(cost.sec))));
  body.appendChild(h('div.insKv', h('span', 'token'),
    h('b', cost.tokens ? cost.tokens.toLocaleString('zh-CN') : '未记')));

  (card.events || []).forEach((event) => {
    const item = h('div.cardEvent');
    const head = h('div.cardEventHead',
      h('i.evDot.ev-' + event.type),
      CARD_EVENT_LABEL[event.type] || event.type
    );
    if (event.tool) head.appendChild(h('code', event.tool));
    item.appendChild(head);

    const args = event.payload && event.payload.arguments;
    if (event.type === 'action' && args && Object.keys(args).length) {
      item.appendChild(h('pre.cardEventPre', JSON.stringify(args, null, 2)));
    }
    if (event.content) item.appendChild(h('div.cardEventText', event.content));
    body.appendChild(item);
  });
  box.appendChild(body);
  body.scrollTop = kept;

  /* 复制命令：优先复制那条真的命令，没有命令就复制这次的参数。
     一个永远是灰的按钮不如不摆，所以没有可复制的东西时它就不出现。 */
  const copyable = (card.events || []).map((event) => {
    const args = (event.payload && event.payload.arguments) || {};
    if (typeof args.command === 'string' && args.command.trim()) return args.command;
    return Object.keys(args).length ? JSON.stringify(args, null, 2) : '';
  }).find(Boolean);

  if (first || copyable) {
    box.appendChild(h('div.cardDrawerFoot',
      first ? h('button.ghost', { onclick: () => locateEvent(first) }, '↗ 在会话流中定位') : null,
      copyable ? h('button.ghost', {
        onclick: async () => {
          try {
            await navigator.clipboard.writeText(copyable);
            toast('已复制', 'good');
          } catch (err) {
            toast('剪贴板被浏览器拦了，手动选一下那段代码', 'bad');
          }
        },
      }, '⧉ 复制命令') : null
    ));
  }
  box.classList.remove('hidden');
}

/* 图上看不明白，就跳回对话流里看上下文 —— 这两处看的是同一份事件 */
function locateEvent(event) {
  GraphView.close();
  if (state.view !== 'chat') switchView('chat');
  const target = state.nodeIndex.get(event.id);
  if (!target) {
    toast('这条在对话里找不到（可能被压缩掉了）', 'bad');
    return;
  }
  target.scrollIntoView({ block: 'center', behavior: 'smooth' });
  target.classList.add('sel');
  setTimeout(() => target.classList.remove('sel'), 1400);
}

/* ------------------------------------------------------------------ 视图 */

function switchView(view) {
  state.view = view;
  $('#viewChat').classList.toggle('hidden', view !== 'chat');
  $('#viewGraph').classList.toggle('hidden', view !== 'graph');
  $$('#viewSwitch button').forEach((button) => {
    button.classList.toggle('on', button.dataset.view === view);
  });
  // 抽屉是「图上的卡」的附属物，人走开了就该收起来
  if (view !== 'graph') GraphView.close();
  if (view === 'graph') {
    renderGraph();
    // 视图刚显示出来时量得到抽屉的真实宽度，这时候才谈得上「让它别挡住卡」
    GraphView.revealOpen();
  }
}

window.addEventListener('hashchange', () => {
  switchView(location.hash === '#graph' ? 'graph' : 'chat');
});

/* ------------------------------------------------------------------ 输入 */

/* ---- 这一轮用哪个技能 ----
 *
 * 为什么要有这个东西：技能一直只能靠模型自己从系统提示里挑，而用户说「用那个
 * 写周报的技能」时，模型得先 skill_load 读一遍、再照着做 —— 多绕一圈，还经常
 * 挑错。用户心里已经定了的事，界面上就该有个地方直接勾上。
 *
 * 选中的技能只作用于**一次发送**，发完就清。理由：技能会改模型的做法，
 * 一直挂在输入框上，用户过一会儿就忘了它还在，然后纳闷「今天它怎么老那样」。
 */

function renderSkillChips() {
  const host = $('#skillChips');
  const picks = state.skillPick || [];
  host.innerHTML = '';
  host.classList.toggle('hidden', !picks.length);
  $('#skillBtn').classList.toggle('on', !!picks.length);
  picks.forEach((name) => {
    host.appendChild(h('span.skillChip', name,
      h('button', { title: '这一轮不用它了', onclick: () => dropSkill(name) }, '×')));
  });
}

/* ---- 这句话里点了谁：@ 角色 / @ 专家 / @ 文件 ----
 *
 * 设计稿 12 号文档 A3 的「已选」态：打出来的 @ 变成彩色 chip，一眼能看出
 * 这一句派给了谁、引用了什么。它**不改文本** —— 输入框始终是那份唯一的真相，
 * chip 只是把里面认出来的东西摆到上面一行；点 × 就从文本里删掉那一处。
 *
 * 为什么不把 textarea 换成一串 chip 编辑器（很多 IM 那样）：那会把「光标、
 * 撤销、粘贴一段带 @ 的话」全部重做一遍，而用户真正需要的是「看清这一句
 * 点了谁」和「点错了能一键摘掉」，这两件事不需要换编辑器。
 */
const REF_CHIP_RE = /(^|\s)@([^\s@，。；、,;]+)/g;

function refMentions(text) {
  const out = [];
  const seen = new Set();
  REF_CHIP_RE.lastIndex = 0;
  let hit = REF_CHIP_RE.exec(String(text || ''));
  while (hit) {
    const name = hit[2].replace(/[.。:：!?！？]+$/, '');
    if (name && !seen.has(name)) {
      seen.add(name);
      out.push(name);
    }
    hit = REF_CHIP_RE.exec(String(text || ''));
  }
  return out;
}

function refKind(name) {
  if (/^expert[:：]/.test(name)) return 'expert';
  if (/[\\/]/.test(name) || /\.[A-Za-z0-9]{1,6}$/.test(name)) return 'file';
  return 'role';
}

function renderRefChips() {
  const host = $('#refChips');
  if (!host) return;
  const input = $('#input');
  const names = refMentions(input ? input.value : '');
  host.textContent = '';
  host.classList.toggle('hidden', !names.length);
  names.forEach((name) => {
    host.appendChild(h('span.refChip.' + refKind(name),
      h('span.refChipAt', '@'),
      h('span.refChipName', { title: name }, name),
      h('button', {
        title: '从这句话里摘掉',
        onclick: () => dropMention(name),
      }, '×')));
  });
}

function dropMention(name) {
  const input = $('#input');
  if (!input) return;
  const text = input.value;
  const at = text.indexOf('@' + name);
  if (at < 0) return renderRefChips();
  const after = text.slice(0, at) + text.slice(at + name.length + 1);
  input.value = after.replace(/\s{2,}/g, ' ').replace(/^\s+/, '');
  autoGrow(input);
  input.focus();
  renderRefChips();
  updateComposer();
}

function addSkill(name) {  if (!name) return;
  if (!state.skillPick.includes(name)) state.skillPick.push(name);
  renderSkillChips();
}

function dropSkill(name) {
  state.skillPick = state.skillPick.filter((one) => one !== name);
  renderSkillChips();
  if (!$('#skillPop').classList.contains('hidden')) paintSkillPop();
}

function closeSkillPop() {
  $('#skillPop').classList.add('hidden');
}

/* 按「技能」按钮的屏幕位置摆浮层。
 *
 * 不用 CSS 的 `bottom: 100%`：那个是相对 `.composer` 的，而 composer 是滚动容器，
 * 浮层伸出它的部分会被裁掉 —— 看得见矩形、点不到东西。fixed + 屏幕坐标没这个问题。
 */
function placeSkillPop() {
  const pop = $('#skillPop');
  const button = $('#skillBtn');
  if (!pop || !button) return;
  const rect = button.getBoundingClientRect();
  pop.style.width = Math.min(440, Math.max(280, window.innerWidth - 36)) + 'px';
  pop.style.right = Math.max(12, window.innerWidth - rect.right) + 'px';
  pop.style.bottom = Math.max(12, window.innerHeight - rect.top + 8) + 'px';
  // 别顶出屏幕：上面留一点给顶栏
  pop.style.maxHeight = Math.max(140, Math.min(320, rect.top - 70)) + 'px';
}

async function toggleSkillPop() {
  const pop = $('#skillPop');
  if (!pop.classList.contains('hidden')) {
    pop.classList.add('hidden');
    return;
  }
  pop.classList.remove('hidden');
  pop.innerHTML = '';
  pop.appendChild(h('div.empty', { style: { padding: '10px' } }, '读取技能…'));
  placeSkillPop();
  const result = await API.skills();
  state.skillList = result.ok ? result.data || [] : [];
  /* 关掉的技能不该再被选上。上一轮选完、然后去左栏关掉它，回来这里还挂着 ——
     那一轮的系统提示会写着「点名要用 X」，然后模型发现 X 是关的。
     所以拉回清单的同时把选中的也过一遍。 */
  const usable = state.skillList.filter((one) => one.enabled !== false).map((one) => one.name);
  const before = state.skillPick.length;
  state.skillPick = state.skillPick.filter((one) => usable.includes(one));
  if (state.skillPick.length !== before) renderSkillChips();
  // 拉数据的这段时间里用户可能已经把浮层关掉了 —— 关掉了就别再画进去
  if (pop.classList.contains('hidden')) return;
  paintSkillPop();
}

function paintSkillPop() {
  const pop = $('#skillPop');
  const all = state.skillList || [];
  /* 只列开着的。关掉的连名字都不出现 —— 列出来再点一下报「已关闭」，
     等于把「我说了不要用」变成一次打断。 */
  const items = all.filter((one) => one.enabled !== false);
  const offCount = all.length - items.length;
  pop.innerHTML = '';
  pop.appendChild(h('div.spHead',
    '这一轮用哪个技能',
    h('span.grow', { style: { flex: '1 1 auto' } }),
    state.skillPick.length ? h('span', state.skillPick.length + ' 个已选') : null));

  if (!items.length) {
    pop.appendChild(h('div.empty', { style: { padding: '12px 10px' } },
      all.length
        ? '技能都关掉了。到左栏「技能」里把要用的那个打开。'
        : '还没有技能。左栏「技能」里可以新建，也可以让 agent 用 skill_create 写一个。'));
    return;
  }

  items.forEach((skill) => {
    const on = state.skillPick.includes(skill.name);
    pop.appendChild(h('div.skillItem' + (on ? '.on' : ''), {
      /* 点完要重画这一行：`on` 是闭包里拍下来的快照，不重画的话
         连点两下会一直「加」，第二下取消不掉。 */
      onclick: () => {
        if (on) dropSkill(skill.name);
        else {
          addSkill(skill.name);
          paintSkillPop();
        }
      },
      title: skill.description || skill.name,
    },
      h('div.tick', on ? '✓' : ''),
      h('div', { style: { minWidth: '0' } },
        h('div.nm', skill.name),
        h('div.ds', skill.description || '（没有描述）'))));
  });

  if (offCount) {
    pop.appendChild(h('div.spFoot',
      offCount + ' 个已关的技能没列在这里 —— 左栏「技能」里能打开'));
  }
}

/* 点别处、按 Esc 都收起。浮层是「临时挑一下」，不是常驻面板。 */
document.addEventListener('click', (event) => {
  const pop = $('#plusPop');
  if (pop && !pop.classList.contains('hidden')
    && !pop.contains(event.target) && !$('#skillBtn').contains(event.target)) {
    closePlus();
  }
  const skill = $('#skillPop');
  if (!skill || skill.classList.contains('hidden')) return;
  if (skill.contains(event.target) || $('#skillBtn').contains(event.target)) return;
  /* 「挂 Skill」是从 ＋ 菜单里点的：这一次点击的 target 在菜单里，
     而技能浮层正是它刚打开的 —— 不豁免的话，同一次点击冒泡上来就把它关了。 */
  const menu = $('#plusPop');
  if (menu && menu.contains(event.target)) return;
  closeSkillPop();
});

/* ------------------------------------------------------------------ ＋ 菜单
 *
 * 设计稿里输入框右上角只有两枚圆：＋ 和发送。技能 / 优化 / 文件原来各占一枚，
 * 收进 ＋ 之后输入框上只剩「说话」和「发出去」两件事。
 *
 * 放什么：只放**往输入框里塞东西**的动作。每个动作都能用 @ / & 直接打出来，
 * 菜单只是「想不起来有这回事」时的入口。所以不放「打开设置」这类跳转。
 */
/* 「＋」菜单（设计稿 12 号文档 C 表，八项）。
 *
 * 原则写在设计里：菜单只放「往输入框里塞东西」的动作；每一项都能用
 * `@ / & / ⌘` 直接在输入框触发，菜单只是可发现性入口。
 *
 * 上一版这里只有四项，而且「文件」那一项其实只是往输入框打一个 `@` ——
 * 点了不弹任何东西。所以用户的第一句反馈是「加号里面的文件点不开」。
 * 现在每一项都真的通到一个选择器或者一个真动作。
 */
const PLUS_ITEMS = [
  { icon: '📎', label: '文件', key: '⌘U', run: () => Refs.pickFile(false) },
  { icon: '🖼', label: '图片 / 截图', key: '⌘⇧V', run: () => Refs.pickFile(true) },
  { icon: '♢', label: '挂 Skill', key: 'Alt+S', run: () => toggleSkillPop() },
  { icon: '◍', label: '选专家', key: '⌘@', run: () => Sidebar.show('experts') },
  { icon: '📁', label: '工作区文件…', key: '', run: () => Refs.pickWorkspaceFile() },
  { icon: '💬', label: '引用会话 / 消息', key: '', run: () => Refs.quoteSession() },
  { icon: '✨', label: '从提示词库插入…', key: '', run: () => Refs.insertPrompt() },
  /* 上面七项都是「往这句话里加东西」，这一项是把这句话**搬到别处去用**。
     一条分隔线把这件不一样的事分开。 */
  { sep: true },
  { icon: '🕐', label: '存为自动化…', key: '', run: () => saveAsSchedule() },
];

/* 「存为自动化…」：把输入框里这句话原样搬到自动化的表单里。
   搬的是**已经写好的那句话** —— 让用户到表单里再重打一遍，
   等于把「这句话值得定时跑」这个念头当场掐掉。 */
function saveAsSchedule() {
  const text = ($('#input') || {}).value || '';
  Panels.newSchedule(text.trim());
}

function insertPrefix(ch) {
  const input = $('#input');
  input.focus();
  const start = input.selectionStart == null ? input.value.length : input.selectionStart;
  const end = input.selectionEnd == null ? start : input.selectionEnd;
  input.value = input.value.slice(0, start) + ch + input.value.slice(end);
  input.selectionStart = start + ch.length;
  input.selectionEnd = input.selectionStart;
  // 交给 @ 补全那条路去弹候选：这里只负责把字符塞进去
  input.dispatchEvent(new Event('input', { bubbles: true }));
}

function placePlus() {
  const pop = $('#plusPop');
  const button = $('#skillBtn');
  if (!pop || !button) return;
  const rect = button.getBoundingClientRect();
  pop.style.left = Math.max(12, Math.min(window.innerWidth - 312, rect.left - 8)) + 'px';
  pop.style.bottom = Math.max(12, window.innerHeight - rect.top + 8) + 'px';
}

function closePlus() {
  const pop = $('#plusPop');
  if (pop) pop.classList.add('hidden');
}

function openPlus() {
  const pop = $('#plusPop');
  if (!pop) return;
  if (!pop.classList.contains('hidden')) { closePlus(); return; }
  pop.innerHTML = '';
  PLUS_ITEMS.forEach((item) => {
    if (item.sep) { pop.appendChild(h('div.sep')); return; }
    pop.appendChild(h('button.mi', {
      onclick: () => { closePlus(); item.run(); },
    },
      h('span.i', item.icon),
      h('span', item.label),
      item.key ? h('span.k', item.key) : null));
  });
  pop.appendChild(h('div.sep'));
  pop.appendChild(h('div.hint2', '这些都能在输入框里直接打：@ 文件 / & 技能 / / 命令'));
  pop.classList.remove('hidden');
  placePlus();
}

/* ------------------------------------------------------------------ 菜单栏
 *
 * 顶栏三枚（应用 / 编辑 / 视图）+ 中栏头两枚（模式 / ⋯）。同一时间只开一个：
 * 两个菜单同时挂着的时候，点哪儿都像点错了。
 *
 * 菜单位置用 fixed 算，不用 CSS 的 `left: 100%` —— `.topbar` 和 `.chead`
 * 都是 `overflow: hidden` 的容器，挂在里面的浮层会被裁掉半截。
 */
const MENUS = {
  app: '#menuApp',
  edit: '#menuEdit',
  view: '#menuView',
  mode: '#modePop',
  chat: '#chatMenu',
};

function closeMenus() {
  Object.values(MENUS).forEach((sel) => {
    const node = $(sel);
    if (node) node.classList.add('hidden');
  });
  $$('.menuBtn').forEach((button) => button.classList.remove('on'));
}

function openMenu(key, anchor) {
  const node = $(MENUS[key]);
  if (!node || !anchor) return;
  const wasOpen = !node.classList.contains('hidden');
  closeMenus();
  if (wasOpen) return;
  node.classList.remove('hidden');
  const rect = anchor.getBoundingClientRect();
  const box = node.getBoundingClientRect();
  node.style.left = Math.max(8, Math.min(rect.left, window.innerWidth - box.width - 8)) + 'px';
  node.style.top = Math.max(8, Math.min(rect.bottom + 4, window.innerHeight - box.height - 8)) + 'px';
  if (anchor.classList.contains('menuBtn')) anchor.classList.add('on');
}

/* 当前会话那条记录（重命名 / 归档 / 删除都要它） */
function currentTrace() {
  return (state.traces || []).find((one) => one.cid === state.cid) || null;
}

function bindChrome() {
  $$('.menuBtn').forEach((button) => {
    button.addEventListener('click', (event) => {
      event.stopPropagation();
      openMenu(button.dataset.menu, button);
    });
  });

  const paletteBtn = $('#paletteBtn');
  if (paletteBtn) paletteBtn.addEventListener('click', () => Palette.open(''));
  const cmdBtn = $('#cmdBtn');
  if (cmdBtn) cmdBtn.addEventListener('click', () => { closeMenus(); Palette.open(''); });
  const setBtn = $('#openSettingsBtn');
  if (setBtn) setBtn.addEventListener('click', () => { closeMenus(); Sidebar.show('settings'); });
  const about = $('#aboutBtn');
  if (about) {
    about.addEventListener('click', () => {
      closeMenus();
      const ver = (state.boot && state.boot.version) || '';
      toast(`DreamRole ${ver} · 本地运行，会话与记忆都在这台机器上`);
    });
  }

  /* 编辑：三条都是对输入框的真操作，不是装饰 */
  const undoBtn = $('#undoBtn');
  if (undoBtn) {
    undoBtn.addEventListener('click', () => {
      closeMenus();
      $('#input').focus();
      document.execCommand('undo');
    });
  }
  const clearBtn = $('#clearInputBtn');
  if (clearBtn) {
    clearBtn.addEventListener('click', () => {
      closeMenus();
      $('#input').value = '';
      autoGrow($('#input'));
      updateComposer();
    });
  }
  const copyBtn = $('#copyLastBtn');
  if (copyBtn) {
    copyBtn.addEventListener('click', async () => {
      closeMenus();
      const nodes = $$('#feed .a');
      const last = nodes[nodes.length - 1];
      if (!last) return toast('这条会话还没有回答', 'bad');
      try {
        await navigator.clipboard.writeText(last.innerText.trim());
        toast('最后一条回答已复制', 'good');
      } catch (err) {
        toast('复制失败：浏览器没给剪贴板权限', 'bad');
      }
    });
  }

  /* 视图：对话 / 过程图 + 两栏的开关 + 产物 */
  const sideBtn = $('#toggleSideBtn');
  if (sideBtn) {
    sideBtn.addEventListener('click', () => {
      closeMenus();
      const side = $('#side');
      if (side) side.classList.toggle('min');
    });
  }
  const rightItem = $('#toggleRightItem');
  if (rightItem) {
    rightItem.addEventListener('click', () => {
      closeMenus();
      setRightOpen(!!$('#rpanel') && $('#rpanel').classList.contains('min'));
    });
  }

  /* 中栏头：模式胶囊 + ⋯ */
  const modePill = $('#modePill');
  if (modePill) {
    modePill.addEventListener('click', (event) => {
      event.stopPropagation();
      openMenu('mode', modePill);
    });
  }
  const more = $('#moreBtn');
  if (more) {
    more.addEventListener('click', (event) => {
      event.stopPropagation();
      openMenu('chat', more);
    });
  }
  const artTop = $('#artifactsTopBtn');
  if (artTop) {
    artTop.addEventListener('click', () => {
      if (typeof Artifacts === 'undefined') return;
      Artifacts.isOpen() ? Artifacts.close() : Artifacts.open();
    });
  }
  const openArt = $('#openArtifactsBtn');
  if (openArt) {
    openArt.addEventListener('click', () => {
      closeMenus();
      if (typeof Artifacts !== 'undefined') Artifacts.open();
    });
  }
  const rename = $('#renameBtn');
  if (rename) {
    rename.addEventListener('click', () => {
      closeMenus();
      const item = currentTrace();
      if (!item) return toast('这条会话还没存下来', 'bad');
      renameTrace(item);
    });
  }
  const pickWs = $('#pickProjectBtn');
  if (pickWs) {
    pickWs.addEventListener('click', () => { closeMenus(); pickProject(); });
  }
  const archive = $('#archiveBtn');
  if (archive) {
    archive.addEventListener('click', async () => {
      closeMenus();
      const item = currentTrace();
      if (!item) return toast('这条会话还没存下来', 'bad');
      await patchTrace(item, { archived: !item.archived });
      if (item.archived === false || item.archived == null) newSession();
    });
  }
  const del = $('#deleteBtn');
  if (del) {
    del.addEventListener('click', () => {
      closeMenus();
      const item = currentTrace();
      if (!item) return toast('这条会话还没存下来', 'bad');
      deleteTraceFlow(item);
    });
  }

  /* 点空白处收起所有菜单。点菜单里面不算 —— 条目自己会关。 */
  document.addEventListener('click', (event) => {
    if (event.target.closest('.menuPop')) return;
    if (event.target.closest('.menuBtn') || event.target.closest('#modePill')
      || event.target.closest('#moreBtn')) return;
    closeMenus();
  });
  window.addEventListener('resize', closeMenus);
}

window.addEventListener('resize', () => {
  const pop = $('#skillPop');
  if (pop && !pop.classList.contains('hidden')) placeSkillPop();
});

function updateComposer() {
  const send = $('#sendBtn');
  const stop = $('#stopBtn');
  const hasWorkspace = !!state.project;
  send.classList.toggle('hidden', state.running);
  stop.classList.toggle('hidden', !state.running);
  send.disabled = !hasWorkspace || !$('#input').value.trim();
  send.title = hasWorkspace ? '发送（Enter）' : '先选一个工作区';

  /* 占位文字是一层真 DOM（`placeholder` 属性没法把 @ / & 染成黄字）。
     它只在输入框空着的时候露出来 —— 和真的 placeholder 一样。 */
  const ph = $('#phHint');
  const typed = !!$('#input').value.trim();
  if (ph) {
    ph.classList.toggle('hidden', typed);
    ph.innerHTML = hasWorkspace
      ? '发消息，或 <b>@ 选角色</b> / <b>/ 调命令</b> / <b>&amp; 挂技能</b> —— 回车发送'
      : '先选一个工作区，才能开始…';
  }
  // 没有字可改的时候，「提示词优化」是暗的 —— 它现在点不出任何东西
  $('#optBtn').classList.toggle('live', typed);
  /* 停下按钮按下去之后要变成「正在停」，并且不能再点第二下 ——
     连按三次除了刷三个一模一样的请求，什么也不会发生。 */
  stop.textContent = state.cancelling ? '正在停…' : '停下';
  stop.disabled = state.cancelling;
  /* 状态条右边的正文由 renderChatBar 写（它知道这一条会话跑了几轮）。
     这里只补三种「压倒一切」的情况：没工作区、正在停、正在跑。 */
  paintStatusHint();
}

/* 状态条上那句「现在在干什么」。三处会写它（renderChatBar / updateComposer /
   心跳），所以字只在一个地方算 —— 否则心跳刚写上的「已跑 12 秒」会被
   紧接着的一次 updateComposer 用「正在跑…」盖掉，看起来就像心跳没生效。 */
function statusHintText() {
  if (state.cancelling) {
    const beat = state.beat;
    return beat && beat.seconds
      ? `正在停下…（已跑 ${beat.seconds}s，正在杀进程）`
      : '正在停下…';
  }
  if (state.running) {
    const beat = state.beat;
    if (!beat || !beat.seconds) return '正在跑…';
    const who = beat.label || beat.tool || '';
    const tail = beat.tail ? ' · ' + String(beat.tail).slice(-28) : '';
    return `正在跑 ${beat.seconds}s${who ? ' · ' + who : ''}${tail}`;
  }
  return '';
}

function paintStatusHint() {
  const hint = $('#usageHint');
  if (!hint) return;
  if (!state.project) {
    hint.textContent = '未设置工作区';
    return;
  }
  /* 闲着的时候这行归 renderChatBar 写（「本会话 N 轮 · token」）。
     这里只在「正在跑 / 正在停」时插话，否则会把人家写的账盖成一个空串。 */
  if (!state.running && !state.cancelling) return;
  hint.textContent = statusHintText();
}

async function ensureSession() {
  if (state.cid) return true;
  if (!state.project) {
    await pickProject();
    // 取消选择 = 这一句不发。不能替用户挑一个目录然后照跑。
    if (!state.project) {
      toast('还没选工作区，这句话没有发出去', 'bad');
      updateComposer();
      return false;
    }
  }
  const result = await API.createSession({
    project: state.project, mode: state.mode,
    permission: state.permission, expert: state.expert || '',
    think: state.think || 'off',
    // 引擎在**建会话这一刻**定下来。服务端也认设置里的默认值，但用户刚在顶栏
    // 点过 role2 的话，就该按他点的来 —— 而不是被设置里的 role1 盖掉。
    engine: state.engine,
    // 会话还没建起来时加的授权先挂在这，这里一次性交给服务端
    grants: state.grants,
  });
  if (!result.ok) {
    toast(result.error, 'bad');
    return false;
  }
  state.cid = result.data.cid;
  // 服务端说了算：它可能因为 role2 不可用而退回 role1，界面得如实显示
  state.engine = result.data.engine || state.engine;
  state.engineBound = state.engine;
  renderEngine();
  state.grants = result.data.grants || state.grants;
  state.permission = result.data.effective || result.data.permission;
  renderPermission();
  connectStream(state.cid, true);
  renderFeed();
  renderTraces();
  return true;
}

async function send() {
  const input = $('#input');
  const text = input.value.trim();
  if (!text) return;
  /* 从前这里是 `if (!text || state.running) return;` —— 静默返回。
     用户看到的正是「输入框还能打字，按回车却什么也不发生」，像坏了。
     现在至少说一句为什么，并且把「正在停」和「还在跑」分开讲。 */
  if (state.cancelling) return toast('还在停，等它真的停下来再发');
  if (state.running) return toast('这一轮还在跑，先按「停下」或者等它跑完');

  // 正在「改一句」：这次发的不是新话，而是从那一句重来
  if (state.editing) {
    const index = state.editing.index;
    state.editing = null;
    input.value = '';
    autoGrow(input);
    updateComposer();
    renderRefChips();
    await editMessage(index, text);
    return;
  }

  if (!(await ensureSession())) return;

  input.value = '';
  autoGrow(input);
  updateComposer();
  renderRefChips();
  clearLive();
  state.nearBottom = true;

  const picked = state.skillPick.slice();
  const result = await API.send(state.cid, text, picked);
  if (!result.ok) {
    toast(result.error, 'bad');
    input.value = text;
    autoGrow(input);
    updateComposer();
    // 服务端说「没工作区」，就把选目录的弹窗直接顶上来 —— 光弹一句红字，
    // 用户还得自己找在哪设置。
    if (result.status === 412) pickProject();
    return;
  }
  /* 发出去了才清。发失败时留着，用户按 Enter 重发还是同一个意思。 */
  if (picked.length) {
    state.skillPick = [];
    renderSkillChips();
    closeSkillPop();
  }
}

async function stop() {
  if (!state.cid || state.cancelling) return;
  /* 先摆好界面，再去请求。顺序反了的话，用户按下去到服务端回话之间
     屏幕上一动不动 —— 又变成「按了没反应」。
     这里说的是「正在停」，不是「已停下」：服务端还得把命令杀掉、
     把门闩放开，那之后 `state: running=false` 到了才算真的停。 */
  state.cancelling = true;
  updateComposer();
  const result = await API.cancel(state.cid);
  if (!result.ok) {
    state.cancelling = false;
    updateComposer();
    return toast(result.error, 'bad');
  }
  // 服务端说它本来就没在跑（可能刚好跑完了）—— 那就不演「正在停」
  if (result.data && result.data.stopping === false) {
    state.cancelling = false;
    state.beat = null;
    updateComposer();
    renderChatBar();
    return toast('它已经停了');
  }
  toast('正在停下…');
}

/* ------------------------------------------------------------------ 模式 */

async function switchMode(mode) {
  state.mode = mode;
  renderModes();
  if (state.cid) {
    const result = await API.setMode(state.cid, mode);
    if (!result.ok) return toast(result.error, 'bad');
    state.permission = result.data.effective;
    renderPermission();
  } else {
    const meta = state.modeMeta.find((item) => item.id === mode);
    if (meta) state.permission = meta.permission;
    renderPermission();
  }
  if (!state.events.length) renderFeed();
}

async function switchPermission(level) {
  state.permission = level;
  renderPermission();
  if (state.cid) {
    const result = await API.setPermission(state.cid, level);
    if (!result.ok) return toast(result.error, 'bad');
    state.permission = result.data.effective;
    renderPermission();
  }
}

function openPermissionMenu() {
  const levels = (state.boot && state.boot.permissions && state.boot.permissions.levels) || [];
  const body = h('div');
  levels.forEach((level) => {
    body.appendChild(h('div.listItem',
      h('div.t', level.label, level.id === state.permission
        ? h('span.tag.acc', { style: { marginLeft: '6px' } }, '当前') : null),
      h('div.d', {
        readonly: true, 'data-id': level.id,
        style: { color: 'var(--muted)', fontSize: '11.5px' },
      }, level.id),
      h('div.row2', h('button.mini', {
        onclick: async () => { await switchPermission(level.id); Modal.close(); },
      }, '切到这一档'))
    ));
  });
  Modal.open({
    title: '权限档位',
    subtitle: '档位决定「什么时候停下来问你」。问答、规划模式会强制只读；教学模式会强制分阶段。',
    body,
    actions: [{ label: '关掉', onClick: () => Modal.close() }],
  });
}

function openThinkMenu() {
  const levels = (state.boot && state.boot.thinking && state.boot.thinking.levels) || [];
  const provider = (state.boot && state.boot.settings && state.boot.settings.provider) || '';
  const model = (state.boot && state.boot.settings && state.boot.settings.model) || '';
  const body = h('div');
  body.appendChild(h('div.hint', {
    style: { color: 'var(--faint)', fontSize: '11.5px', marginBottom: '10px', lineHeight: '1.6' },
  }, `当前服务商 ${provider} / ${model}。思考会变慢变贵，按任务难度调就好。`));
  levels.forEach((level) => {
    body.appendChild(h('div.listItem',
      h('div.t', level.label, level.id === state.think
        ? h('span.tag.acc', { style: { marginLeft: '6px' } }, '当前') : null),
      h('div.d', { style: { color: 'var(--muted)', fontSize: '11.5px' } }, level.hint),
      h('div.row2', h('button.mini', {
        onclick: async () => { await switchThink(level.id); Modal.close(); },
      }, level.id === state.think ? '就是这一档' : '切到这一档'))
    ));
  });
  Modal.open({
    title: '深度思考',
    subtitle: '打开之后，模型会先把思路写在「思」那条里，再给答案。'
      + '不是每家服务商都支持调深度：不支持的那家会照常回答，并在过程里说一句。',
    body,
    actions: [{ label: '关掉', onClick: () => Modal.close() }],
  });
}

async function switchThink(level) {
  state.think = level;
  renderThink();
  if (!state.cid) return;   // 还没开会话：只记住，等建会话时带上去
  const result = await API.setThink(state.cid, level);
  if (!result.ok) return toast(result.error, 'bad');
  if (result.data.note) toast(result.data.note, '');
}

/* ------------------------------------------------------------------ 提示词优化 */

/* 这一轮用的是哪套改法、改过几次。
   版本链不是装饰：同一句改两遍，用户得看得见「刚才那版多大、这版多大」，
   否则「再改一次」会让人以为什么都没发生。 */
let optTemplateId = '';
const optVersions = [];

/* 优化对象：这句话是要给谁看的。后端只拿它当「目标」提示，不换模板 ——
   模板决定结构，对象决定语气和判断标准。 */
const OPT_TARGETS = [
  ['user', '用户提示词'],
  ['system', '系统提示词'],
  ['skill', '技能描述'],
];

/* 期望效果：六个可多选的取舍。设计稿默认勾前两个（更清晰 / 结构化）——
   这两个几乎永远是对的，而「更短」「加示例」要看场合。 */
const OPT_GOALS = [
  ['clear', '更清晰'],
  ['structure', '结构化'],
  ['shorter', '更短'],
  ['constraints', '加约束'],
  ['examples', '加示例'],
  ['role', '角色化'],
];
let optTarget = 'user';
let optGoals = ['clear', 'structure'];

function paintOptTune() {
  const tHost = $('#optTarget');
  if (tHost) {
    tHost.innerHTML = '';
    OPT_TARGETS.forEach(([key, name]) => {
      tHost.appendChild(h('button.optSeg' + (optTarget === key ? '.on' : ''), {
        onclick: () => { optTarget = key; paintOptTune(); },
      }, name));
    });
  }
  const gHost = $('#optGoals');
  if (gHost) {
    gHost.innerHTML = '';
    OPT_GOALS.forEach(([key, name]) => {
      const on = optGoals.includes(key);
      gHost.appendChild(h('button.optGoal' + (on ? '.on' : ''), {
        onclick: () => {
          if (on) optGoals = optGoals.filter((one) => one !== key);
          else optGoals.push(key);
          paintOptTune();
          /* 点一下不重跑：多选是「攒要求」，跑一次要几秒，
             每点一下都发一次请求等于让用户为试选付费。改完点「再来一版」。 */
        },
      }, (on ? '✓ ' : '＋ ') + name));
    });
  }
}

async function runOptimize(template) {
  const input = $('#input');
  const text = input.value.trim();
  if (!text) return toast('先写点什么，再优化', 'bad');
  if (!state.boot || !state.boot.settings.has_key) {
    toast('优化提示词也要用模型，先去设置里填 API key', 'bad');
    Panels.open('settings');
    return;
  }

  optTemplateId = template || '';
  const panel = $('#optPanel');
  panel.classList.remove('hidden');
  $('#optOrig').textContent = text;
  $('#optOrigChars').textContent = text.length + ' 字';
  $('#optBody').textContent = '正在改…';
  $('#optNewChars').textContent = '';
  $('#optWarn').classList.add('hidden');
  $('#optJudge').classList.add('hidden');
  $('#optVar').classList.add('hidden');
  $('#optMeta').textContent = '';
  fillTemplates($('#optSegs'), optTemplateId);
  paintOptTune();

  const result = await API.optimize(text, template || '', optTarget, optGoals);
  if (!result.ok) {
    $('#optBody').textContent = '失败了：' + result.error;
    return;
  }
  const data = result.data;
  state.optimize = data;

  fillTemplates($('#optSegs'), data.template);
  $('#optBody').textContent = data.optimized || '（模型没给内容）';
  $('#optNewChars').textContent = (data.chars ? data.chars.after : 0) + ' 字';
  $('#optMeta').textContent =
    `${data.template_name} · ${data.chars.before} → ${data.chars.after} 字`;

  /* 原话里有 {{变量}} 就得说清楚它们没被动过 —— 那是模板的锚点，
     改掉它等于把这条提示词废了。只在真的存在时才说，别摆一句常驻的空话。 */
  const hasVars = /\{\{[^}]+\}\}/.test(text);
  $('#optVar').classList.toggle('hidden', !hasVars);

  const warn = $('#optWarn');
  warn.innerHTML = '';
  if ((data.warnings || []).length) {
    data.warnings.forEach((line) => warn.appendChild(h('div', '· ' + line)));
    warn.classList.remove('hidden');
  } else {
    warn.classList.add('hidden');
  }

  optVersions.push({ chars: (data.chars && data.chars.after) || 0, name: data.template_name });
  renderOptVer();
}

function renderOptVer() {
  const box = $('#optVer');
  if (!box) return;
  box.innerHTML = '';
  if (!optVersions.length) return;
  box.appendChild(document.createTextNode('版本 '));
  optVersions.forEach((item, index) => {
    if (index) box.appendChild(document.createTextNode(' → '));
    const label = 'v' + (index + 1) + ' · ' + item.chars + ' 字';
    if (index === optVersions.length - 1) box.appendChild(h('b', label));
    else box.appendChild(document.createTextNode(label));
  });
}

function showJudge(judge) {
  const box = $('#optJudge');
  if (!judge || !judge.ok) {
    box.innerHTML = '';
    box.classList.add('hidden');
    if (judge && judge.reason) toast(judge.reason);
    return;
  }
  box.innerHTML = '';
  box.appendChild(h('div.jHead',
    `${judge.winner_label}　原话 ${judge.scores.original} / 改写 ${judge.scores.optimized}`));

  const section = (title, items, bad) => {
    if (!items || !items.length) return;
    box.appendChild(h('div', { style: { marginTop: '6px', color: bad ? '#f0a9ab' : 'var(--text-2)' } }, title));
    const list = h('ul');
    items.forEach((item) => list.appendChild(h('li', item)));
    box.appendChild(list);
  };
  section('值得学的', judge.learnable, false);
  section('它自己加的（原文没有）', judge.overfit, true);
  section('和原话冲突', judge.conflicts, true);
  box.classList.remove('hidden');
}

async function askJudge() {
  if (!state.optimize) return;
  const box = $('#optJudge');
  box.innerHTML = '';
  box.appendChild(h('div', { style: { color: 'var(--muted)' } }, '评审中…'));
  box.classList.remove('hidden');
  const result = await API.compare(state.optimize.original, state.optimize.optimized);
  if (!result.ok) {
    box.classList.add('hidden');
    return toast(result.error, 'bad');
  }
  showJudge(result.data);
}

function closeOptimize() {
  $('#optPanel').classList.add('hidden');
  state.optimize = null;
}

/* 模板从下拉框改成一段胶囊：设计稿里它们是一排「可点的改法」，
   点一下就重跑。下拉框要先展开再看，多一次点击，而且看不出有哪几种。 */
function fillTemplates(host, active) {
  if (!host) return;
  host.innerHTML = '';
  (state.templates || []).forEach((template) => {
    host.appendChild(h('button.optSeg' + (template.id === (active || '') ? '.on' : ''), {
      title: template.description || template.name,
      'data-tpl': template.id,
      onclick: () => runOptimize(template.id),
    }, template.name));
  });
}

/* ------------------------------------------------------------------ @ 补全 */

/* 只在「光标前面那个 @ 后面还没空格」的时候弹。
   判据取的是光标到上一个空白之间的文本，所以一行里可以有好几个 @。 */
function atQuery(input) {
  const caret = input.selectionStart;
  const before = input.value.slice(0, caret);
  const hit = /(^|[\s（(])@([^\s@]*)$/.exec(before);
  if (!hit) return null;
  return { start: caret - hit[2].length - 1, query: hit[2] };
}

/* @ 浮层的位置：贴着输入框上方、和对话区同宽。
   用屏幕坐标（fixed）而不是 CSS 的 bottom:100%，原因见 .atPop 的样式注释。 */
function placeAtPop() {
  const pop = $('#atPop');
  const input = $('#input');
  if (!pop || !input) return;
  const rect = input.getBoundingClientRect();
  const width = Math.min(920, window.innerWidth - 36);
  pop.style.position = 'fixed';
  pop.style.width = width + 'px';
  pop.style.left = Math.max(12, rect.left) + 'px';
  pop.style.bottom = Math.max(12, window.innerHeight - rect.top + 6) + 'px';
  pop.style.maxHeight = Math.max(120, Math.min(232, rect.top - 70)) + 'px';
}

async function updateAtPop() {
  const input = $('#input');
  const found = atQuery(input);
  const pop = $('#atPop');
  if (!found) {
    state.at = null;
    pop.classList.add('hidden');
    return;
  }
  if (!state.project) {
    state.at = null;
    pop.classList.add('hidden');
    return;
  }

  const slash = found.query.lastIndexOf('/');
  const dir = slash >= 0 ? found.query.slice(0, slash + 1) : '';
  const prefix = slash >= 0 ? found.query.slice(slash + 1) : found.query;
  const target = dir ? state.project.replace(/[\\/]+$/, '') + '/' + dir.replace(/\/$/, '') : state.project;

  const result = await API.browse(target);
  if (!result.ok) {
    state.at = null;
    pop.classList.add('hidden');
    return;
  }
  const entries = (result.data && result.data.entries) || [];
  const lower = prefix.toLowerCase();
  const items = entries
    .filter((entry) => entry.name.toLowerCase().includes(lower))
    .slice(0, 60)
    .map((entry) => ({ name: entry.name, dir: entry.dir, path: dir + entry.name }));

  const keepIndex = state.at ? state.at.index : 0;
  state.at = {
    start: found.start,
    query: found.query,
    dir,
    items,
    index: items.length ? Math.min(keepIndex, items.length - 1) : 0,
  };

  pop.innerHTML = '';
  if (!items.length) {
    pop.appendChild(h('div.popH', h('span', '@ 选文件 / 目录'),
      h('span.popK', '0 项')));
    pop.appendChild(h('div.atItem', h('span.av', '·'), h('div.atMain',
      h('div.atName', '这个目录里没有匹配的'))));
    placeAtPop();
    pop.classList.remove('hidden');
    return;
  }
  /* 头顶那一行是「这是什么 + 怎么用键盘」。候选层没有它，第一次弹出来的人
     只能靠试：方向键能不能选？回车是确认还是换行？一句话说清，比任何提示都好。 */
  pop.appendChild(h('div.popH',
    h('span', '@ 选文件 / 目录'),
    h('span.popK', `${items.length} 项 · ↑↓ 选 · ↵ 确认`)));
  items.forEach((item, position) => {
    pop.appendChild(h('div.atItem' + (position === state.at.index ? '.on' : ''), {
      onmousedown: (event) => { event.preventDefault(); pickAt(position); },
    },
      h('span.av', item.dir ? '▸' : '·'),
      h('div.atMain',
        h('div.atName', item.name),
        h('div.atDesc', item.dir ? '目录' : '文件')),
      item.dir ? h('span.atK', '进入') : null
    ));
  });
  placeAtPop();
  pop.classList.remove('hidden');
}

function pickAt(position) {
  const input = $('#input');
  const at = state.at;
  if (!at || !at.items[position]) return;
  const item = at.items[position];
  const careted = input.selectionStart;
  // 选目录就继续往里钻：把 @dir/ 补上再弹一次
  const insert = item.dir ? item.path + '/' : item.path;
  const value = input.value.slice(0, at.start) + '@' + insert + input.value.slice(careted);
  input.value = value;
  const caret = at.start + 1 + insert.length;
  input.setSelectionRange(caret, caret);
  autoGrow(input);
  updateComposer();
  if (item.dir) updateAtPop();
  else {
    state.at = null;
    $('#atPop').classList.add('hidden');
  }
}

function moveAt(step) {
  const at = state.at;
  if (!at || !at.items.length) return;
  at.index = (at.index + step + at.items.length) % at.items.length;
  const pop = $('#atPop');
  /* 只数候选行。pop 里还有一行表头，按下标对会整体错一位 ——
     错一位的表现是「方向键选中了下面那一项」，最难看出来的一种。 */
  const rows = pop.querySelectorAll('.atItem');
  rows.forEach((node, position) => node.classList.toggle('on', position === at.index));
  const active = rows[at.index];
  if (active && active.scrollIntoView) active.scrollIntoView({ block: 'nearest' });
}

function atOpen() {
  return !!(state.at && state.at.items.length && !$('#atPop').classList.contains('hidden'));
}

/* ------------------------------------------------------------------ 命令面板 */

function openPalette() {
  Palette.open('');
}

/* ------------------------------------------------------------------ 选目录 */

/* 选工作区。
 *
 * 这里走过两版，都错在同一个地方：想自己画一个文件浏览器。
 *
 * 第一版是「当前路径一行灰字 + 一个目录列表」，点进去就等于改选，
 * 分不清「我在浏览」和「我已经选了」。第二版加了「选中的工作区」卡片，
 * 逻辑清楚了，但仍然只能在当前盘里上下走 —— 换盘要一层层退到根目录，
 * 用户的评价是「浏览就只能浏览 F 盘」。
 *
 * 现在的答案很简单：**别自己画**。Windows 自己有一个选文件夹的对话框
 * （左快速访问、上地址栏、此电脑里换盘、还能新建文件夹），所有程序都长这样，
 * 用户不用学。我们只负责把它弹出来、把结果接到会话上。
 *
 * 于是这个弹窗只剩下两条路，两条都是「点一下就定了」：
 *   最近用过 —— 一次点击；
 *   浏览这台电脑… —— 系统对话框里选中什么就是什么。
 * 没有「先选中再确认」这一步：系统对话框的那个「选择文件夹」就是确认。
 */
async function pickProject() {
  const recent = ((state.boot && state.boot.settings && state.boot.settings.recent_projects) || [])
    .filter(Boolean);
  const body = h('div');

  body.appendChild(workspaceRules());

  if (state.project) {
    body.appendChild(h('div.wsNow',
      h('span.wsNowLabel', '现在用的是'),
      h('span.wsNowPath', { title: state.project }, state.project)
    ));
  }

  if (recent.length) {
    const chips = h('div.dirRecent',
      h('div.dirRecentTitle', '最近用过 · 点一下直接设为这条会话的工作区'));
    recent.slice(0, 6).forEach((path) => {
      chips.appendChild(h('button.dirChip', {
        title: path,
        onclick: () => { Modal.close(); applyWorkspace(path); },
      }, pathTail(path, 2)));
    });
    body.appendChild(chips);
  }

  body.appendChild(h('div.wsBrowseHint',
    '「浏览这台电脑…」打开的是 Windows 自己的选文件夹对话框：',
    h('br'),
    '能换盘、能粘贴路径、里面就有「新建文件夹」。选完即生效。'
  ));

  body.appendChild(grantsSection());

  Modal.open({
    title: '选工作区',
    subtitle: '工作区是主目录。要它顺手能看别的目录，在下面单独开一张通行证。',
    body,
    actions: [
      { label: '取消', onClick: () => Modal.close() },
      {
        label: '浏览这台电脑…',
        kind: 'primary',
        onClick: () => browseNative(),
      },
    ],
  });
}

/* 弹系统对话框。返回的路径直接就定了 —— 用户在里面点「选择文件夹」
   本来就是一次确认，回来再让他点一次「确定」是多余的一步。 */
async function browseNative() {
  const result = await API.pickFolder('选择工作区');
  if (!result.ok) {
    toast(result.error || '打不开系统的选文件夹对话框', 'bad');
    return;
  }
  const path = result.data && result.data.path;
  if (!path) return;               // 用户按了取消，什么都不做
  Modal.close();
  await applyWorkspace(path);
}

/* ---------------------------------------------------------------------------
 * 额外授权目录（通行证）
 *
 * 工作区是主目录；这里列的是「另外也允许去一趟」的目录，每张带一个档：
 * 只读（能看不能改）/ 可写（和工作区一样）。
 *
 * 三个刻意的选择：
 * 1. **默认只读。** 绝大多数需求是「去看一眼那个目录里有什么」，
 *    读不破坏任何东西，写才值得多点一次。
 * 2. **跟着会话走。** 做成全局的话，你给 A 会话开了 D 盘，B 会话也悄悄带着，
 *    而界面上看不出来 —— 和以前那个静默默认的 data/projects 是同一个毛病。
 * 3. **它不越过安全底线。** 系统目录、私钥那几条是硬拒绝，授权了也进不去，
 *    所以这里不用再堆一层警告文字，说清楚「能去哪」就够了。
 * ------------------------------------------------------------------------- */

const GRANT_MAX = 12;

/* 新对话起手就带着的那几张 = 设置里勾的「全局通行证」。
 *
 * 这和工作区**不继承**是两件事，别混：工作区是「这条对话在哪里干活」，
 * 必须一条一次地显式选（继承会让用户在没同意的目录里被改文件）；
 * 全局通行证是用户在设置页里明确勾过的「这些地方我都允许」，
 * 勾一次就该处处生效 —— 不然每开一条新对话就要从头补一遍。
 *
 * 抄一份而不是共享一份：抄完之后归这条对话自己，谁也不会在跑到一半时被改。
 */
function defaultGrants() {
  const list = (state.boot && state.boot.global_grants) || [];
  return list.map((one) => ({
    path: one.path,
    mode: one.mode || 'ro',
    missing: !!one.missing,
    global: true,
  }));
}

/* 还没建会话时（工作区刚选、第一句还没发）先本地攒着。
   这一小段逻辑和服务端 grants.py 是同一套规则，看着像重复 —— 但它
   只在「cid 还不存在」的那几步用得上，一旦会话建起来就全部以服务端返回为准。 */
function applyLocalGrant(list, op, extra) {
  const path = String((extra && extra.path) || '').toLowerCase();
  let items = (list || []).map((one) => Object.assign({}, one));
  if (op === 'add') {
    const mode = (extra && extra.mode) || 'ro';
    const hit = items.find((one) => String(one.path).toLowerCase() === path);
    if (hit) hit.mode = mode;
    else items.push({ path: (extra && extra.path) || '', mode, missing: false });
  } else if (op === 'remove') {
    items = items.filter((one) => String(one.path).toLowerCase() !== path);
  } else if (op === 'mode') {
    items.forEach((one) => {
      if (String(one.path).toLowerCase() === path) one.mode = (extra && extra.mode) || 'ro';
    });
  } else if (op === 'clear') {
    items = [];
  }
  return items;
}

function grantsSection() {
  const box = h('div.wsGrants');

  async function send(op, extra) {
    if (state.cid) {
      const res = await API.updateGrants(state.cid, Object.assign({ op: op }, extra || {}));
      if (!res.ok) {
        toast(res.error || '改不了授权目录', 'bad');
        return;
      }
      state.grants = res.data.grants || [];
      if (res.data.note) toast(res.data.note, 'good');
    } else {
      state.grants = applyLocalGrant(state.grants, op, extra);
    }
    paint();
  }

  async function addGrant() {
    const res = await API.pickFolder('添加一个 agent 也能访问的目录');
    if (!res.ok) {
      toast(res.error || '打不开系统的选文件夹对话框', 'bad');
      return;
    }
    const path = res.data && res.data.path;
    if (!path) return;                       // 用户按了取消
    await send('add', { path: path, mode: 'ro' });
  }

  function paint() {
    box.innerHTML = '';
    const globalCount = state.grants.filter((one) => one.global).length;
    box.appendChild(h('div.wsGrantsHead',
      h('span', '还能去的地方'),
      h('span.wsGrantsCount',
        state.grants.length + '/' + GRANT_MAX + (globalCount ? ' · 全局 ' + globalCount : ''))
    ));

    if (!state.grants.length) {
      box.appendChild(h('div.wsGrantsEmpty', '还没有。agent 现在只能在工作区里活动。'));
    }

    state.grants.forEach((one) => {
      const writable = one.mode === 'rw';
      box.appendChild(h('div.grantRow', { title: one.path },
        h('span.grantPath',
          pathTail(one.path, 2),
          /* 「全局」这两个字是给「它从哪来的」一个交代：
             不然用户在这条对话里删掉一张，下条新对话又冒出来，会以为是 bug。 */
          one.global ? h('span.grantFrom', '全局') : null,
          one.missing ? h('span.grantGone', '目录不在了') : null),
        h('button.grantMode' + (writable ? '.rw' : ''), {
          title: writable ? '点一下改成只读' : '点一下改成可写',
          onclick: () => send('mode', { path: one.path, mode: writable ? 'ro' : 'rw' }),
        }, writable ? '可写' : '只读'),
        h('button.grantDel', {
          title: one.global ? '在这条对话里撤掉它（全局那份还在）' : '撤掉这张通行证',
          onclick: () => send('remove', { path: one.path }),
        }, '×')
      ));
    });

    const foot = h('div.wsGrantsFoot',
      h('button.mini', { onclick: addGrant }, '＋ 添加位置…'));
    if (state.grants.length) {
      foot.appendChild(h('button.mini.danger', { onclick: () => send('clear', {}) }, '全部撤掉'));
    }
    box.appendChild(foot);

    box.appendChild(h('div.wsGrantsHint',
      '只读的能看、不能改。要改就在上面切到「可写」。',
      h('br'),
      '标「全局」的那些是设置里勾过的，每条新对话都会有；在这里点 × 只影响这一条。',
      h('br'),
      '这张纸不越过底线：系统目录、私钥那类地方，授权了也仍然碰不了。'));
  }

  paint();
  return box;
}

/* 把某个目录设成这个会话的工作区。
 *
 * 会话已经存在 → 改会话的（后端会记一条 notice，让过程图里看得见）；
 * 还没建会话 → 先记在 state 上，等第一次发送时随 createSession 一起落库。
 * 两种情况的区别用户不该感知，但结果必须一致：**这条会话**用这个目录。
 */
async function applyWorkspace(path) {
  const target = String(path || '').trim();
  if (!target) return;

  if (state.cid) {
    const res = await API.setWorkspace(state.cid, target);
    if (!res.ok) return toast(res.error, 'bad');
    state.project = (res.data && res.data.project) || target;
  } else {
    state.project = target;
  }

  renderProject();
  renderFeed();
  updateComposer();
  toast('工作区：' + shortPath(state.project), 'good');
  $('#input').focus();
}

/* ------------------------------------------------------------------ 启动 */

async function bootstrap() {
  const result = await API.bootstrap();
  if (!result.ok) {
    document.body.innerHTML = '<div class="empty">连不上本地服务：' + escapeHtml(result.error) + '</div>';
    return;
  }
  const data = result.data;
  state.boot = data;
  state.modeMeta = data.modes || [];
  state.templates = data.templates || [];
  state.mode = (data.settings && data.settings.mode) || 'craft';
  state.permission = (data.settings && data.settings.permission) || 'standard';
  state.think = (data.settings && data.settings.think) || 'off';
  // 两套底层的可用性（装没装、key 有没有）。bootstrap 里带一份，省一次往返；
  // 但真正要「刚装完 DSH 就能用」还得靠 /api/engines —— 打开选择器时会重拉。
  state.engines = data.engines || [];
  state.engine = (data.settings && data.settings.engine) || 'role1';
  state.engineBound = '';
  /* 不从 settings 里预填工作区：那是「上次用的目录」，不是「这条会话选的」。
     预填之后用户以为自己没选，实际上已经在那个目录里跑了。
     真正的工作区等打开某条会话（它自己记着）或用户点选时才有。 */
  state.project = '';
  state.grants = defaultGrants();

  window.APP_LABELS = data.labels || {};
  $('#version').textContent = data.version || '';
  renderProject();
  renderModes();
  renderPermission();
  renderThink();
  renderEngine();
  renderExpertChip();
  renderFeed();
  /* 深链：?archived=1 连归档的会话一起列出来（截图 / 文档 / 分享位置用）。
     放在第一句 renderTraces 之前，是为了只画一次 —— 先画一遍再切开关的话，
     两次渲染的 await 谁先回来不一定，容易把同一行画两遍。 */
  state.showArchived = new URLSearchParams(location.search).has('archived');
  renderTraces();
  updateComposer();

  const params = new URLSearchParams(location.search);

  // 打开上次那条会话：用户重新打开软件时，最想看到的是「我昨天做到哪了」，
  // 而不是一张空白页加一句「说点什么」。
  // ?fresh=1 = 故意不自动打开，用来看「还没选工作区」那个状态（截图/排错用）。
  const last = (data.traces || [])[0];
  if (last && !params.has('fresh')) {
    await openTrace(last.cid);
  }

  /* 深链：?gran=round 直接切成「一轮一个节点」（截图 / 文档用）。
     要在 switchView 之前定好：先画一遍再切，画布会白闪一次。 */
  const demoGran = params.get('gran');
  if (demoGran) {
    GraphView.setDetail(demoGran);
    $$('#graphGran .tb').forEach((one) => {
      one.classList.toggle('on', one.dataset.detail === demoGran);
    });
  }
  if (location.hash === '#graph') switchView('graph');

  /* 深链：?demoChips=1 往输入框里放一句带 @ 的话，看 chip 长什么样（截图用） */
  if (params.has('demoChips')) {
    const box = $('#input');
    if (box) {
      box.value = '读一下 @src/main.py，按 @standard 的口径改，别动 @expert:review 的规矩';
      autoGrow(box);
      renderRefChips();
    }
  }

  /* 深链：?panel=automation&auTab=runs|tpl 直接落到自动化页的某一格。
     顺序很重要：**先定好停在哪一格，再打开面板**。反过来的话，面板会先按
     默认那一格画一遍，紧接着再被「切到 runs」重画一次，两遍内容交替写进
     同一个容器 —— 页头和 tab 条各出现两遍。 */
  const auTab = params.get('auTab');
  if (auTab) Panels.autoTab(auTab);

  // 深链：?panel=settings 直接把某个面板打开（文档、排错、截图都方便）
  const panel = params.get('panel');
  if (panel) Panels.open(panel);

  // 深链：?section=memory 直接切到左栏某一栏（截图与文档用）
  const section = params.get('section');
  if (section) Sidebar.show(section);

  /* 第一次打开、而且一条会话都没有：直接把选目录的弹窗顶上来。
     注意条件是「没有会话」，不是「没有工作区」—— 后者在打开旧会话之前
     永远成立，那样每次启动都会弹一次，把用户和自动化都拦在门口。 */
  if (!data.settings.has_key) {
    Panels.open('settings');
    toast('先配一下模型服务商和 API key', 'bad');
  } else if (!last && !params.has('fresh')) {
    pickProject();
  }

  // 深链：?pick=1 直接把选工作区的弹窗打开（截图/文档用）
  if (params.has('pick')) pickProject();
  // 深链：?artifacts=1 直接展开当前会话的产物（截图/文档用）
  if (params.has('artifacts')) Artifacts.open();
  // 深链：?dock=1 直接展开底下那条日志抽屉（截图/文档用）
  if (params.has('dock')) Dock.open();
  // 深链：?panel=automation&newSchedule=1 直接把新建自动化的表单顶上来
  if (params.has('newSchedule')) Panels.newSchedule();
  /* 深链：把「＋」菜单和它下面那四个选择器直接打开（截图 / 文档用） */
  if (params.has('plus')) openPlus();
  const picker = params.get('refPicker');
  if (picker === 'ws') Refs.pickWorkspaceFile();
  else if (picker === 'lib') Refs.insertPrompt();
  else if (picker === 'quote') Refs.quoteSession();
  if (params.has('wsDelete')) {
    if (!(state.traces || []).length) await renderTraces();
    /* 挑第一个真有路径的工作区分组 —— 「默认」那一组没有目录可删 */
    const group = wsGroups(state.traces || []).groups
      .find((one) => one.path && !one.fixed);
    if (group) confirmWorkspaceDelete(group);
  }
  // 前端自检：只在显式带 ?uitest=1 时才加载那段代码，正常使用不受影响
  if (params.has('uitest')) {
    const script = document.createElement('script');
    script.src = 'js/uitest.js';
    document.body.appendChild(script);
  }
}

function newSession() {
  /* 这里原来有一道「等这一轮跑完再开新的」的拦截，已经去掉。
     服务端本来就是按会话记 running 的（每条会话自己一把锁），一条会话在跑
     完全不妨碍另一条 —— 拦人的只有前端这一个判断，属于自己给自己上枷锁。
     现在切走就不再理会那条会话，它照样在后台跑完；列表上会标「跑」，
     点回去还能接着看（重连事件流时服务端会补发全部历史）。 */
  state.cid = null;
  state.events = [];
  /* 工作区不继承上一条会话。这是刻意的：目录一旦「默认沿用」，
     用户就会在某天发现它在自己没同意的目录里改了文件。要复用，
     选目录的弹窗里有「最近用过」，点一下就行。 */
  state.project = '';
  state.grants = defaultGrants();
  state.assistant = '';
  state.members = [];
  state.group = '';
  state.pending = null;
  state.todos = [];
  state.todoOpen = true;
  state.todoHidden = false;
  state.todoUser = false;
  state.todoSig = '';
  state.usage = null;
  state.editing = null;
  /* 这条必须清。running 是「当前这条会话」的状态，新会话还没开始跑；
     不清的话输入框会一直挂着「停下」，发送按钮是隐藏的 —— 看起来就是
     「开不了新对话」，哪怕拦截那句话已经删了。 */
  state.running = false;
  state.cancelling = false;
  state.beat = null;
  /* 新会话 = 引擎重新可选。上一条绑死的是上一条，这里把「已绑定」清掉，
     顶栏那枚胶囊才会回到「下一条用哪个」的语义。 */
  state.engineBound = '';
  state.nodeIndex.clear();
  if (state.stream) state.stream.close();
  state.stream = null;
  renderFeed();
  renderTodos();
  renderChatBar();
  refreshGraph();
  renderPending();
  renderProject();
  updateComposer();
  renderTraces();
  // 还没开始的会话没有产物：面板立刻空掉，计数也收起来
  if (Artifacts.isOpen()) Artifacts.render();
  Artifacts.sync();
  $('#input').focus();
}

function bind() {
  const input = $('#input');

  input.addEventListener('input', () => {
    autoGrow(input);
    updateComposer();
    updateAtPop();
    // 这句话里点了谁，跟着变（@ 角色 / @ 专家 / @ 文件）
    renderRefChips();
  });
  input.addEventListener('keydown', (event) => {
    // @ 补全开着的时候，方向键和 Enter 归补全管
    if (atOpen()) {
      if (event.key === 'ArrowDown') { event.preventDefault(); moveAt(1); return; }
      if (event.key === 'ArrowUp') { event.preventDefault(); moveAt(-1); return; }
      if (event.key === 'Enter' || event.key === 'Tab') {
        event.preventDefault();
        pickAt(state.at.index);
        return;
      }
      if (event.key === 'Escape') {
        event.preventDefault();
        state.at = null;
        $('#atPop').classList.add('hidden');
        return;
      }
    }
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      send();
    }
    if (event.key === 'Escape') {
      if (state.editing) {
        state.editing = null;
        toast('不改了');
      }
      closeOptimize();
    }
  });
  input.addEventListener('blur', () => {
    // 延迟关，否则点补全项的 mousedown 还没到就已经被关掉了
    setTimeout(() => {
      if (!atOpen()) return;
      state.at = null;
      $('#atPop').classList.add('hidden');
    }, 140);
  });
  input.addEventListener('scroll', feedScrolled);
  input.addEventListener('click', updateAtPop);

  $('#sendBtn').addEventListener('click', send);
  $('#stopBtn').addEventListener('click', stop);
  $('#feed').addEventListener('scroll', feedScrolled);

  $('#newSession').addEventListener('click', newSession);

  /* 这个绑定必须包一层。
     直接写 `addEventListener('click', pickProject)` 的话，点击事件对象会被当成
     第一个参数传进来 —— 而第一个参数现在正好是「从哪个目录开始」。
     结果就是卡片上显示 `[object PointerEvent]`，然后弹一句「打不开这个目录」。 */
  $('#projectBtn').addEventListener('click', () => pickProject());
  $('#permBtn').addEventListener('click', openPermissionMenu);
  $('#thinkBtn').addEventListener('click', openThinkMenu);
  $('#engineBtn').addEventListener('click', openEnginePicker);
  $('#expertBtn').addEventListener('click', () => Sidebar.show('experts'));
  /* 右上角那个按钮 = 「这个对话产出了什么」。
     它原来开的是「工具 / 技能 / 设置」抽屉 —— 那三样左栏本来就有，
     两个入口指向同一批内容只会让人先想「从哪边进」。 */
  $('#artifactsBtn').addEventListener('click', () => Artifacts.toggle());
  $('#showArchived').addEventListener('click', () => setShowArchived(!state.showArchived));

  $$('#viewSwitch button').forEach((button) => {
    button.addEventListener('click', () => switchView(button.dataset.view));
  });

  $('#zoomIn').addEventListener('click', () => {
    const z = GraphView.setZoom(GraphView.getZoom() * 1.22);
    $('#zoomLabel').textContent = Math.round(z * 100) + '%';
  });
  $('#zoomOut').addEventListener('click', () => {
    const z = GraphView.setZoom(GraphView.getZoom() / 1.22);
    $('#zoomLabel').textContent = Math.round(z * 100) + '%';
  });
  /* 点百分比 = 回到「整张图正好铺满」的那个视图。
     这也是滚轮/拖动玩丢了自己之后最需要的那个出口。 */
  $('#zoomLabel').addEventListener('click', () => {
    GraphView.fit();
  });
  $('#fitBtn').addEventListener('click', () => GraphView.fit());
  $('#followToggle').addEventListener('change', (event) => {
    renderGraph();
    if (event.target.checked) GraphView.fit();
  });
  /* 粒度：一步一个节点 / 一轮一个节点 */
  $$('#graphGran .tb').forEach((button) => {
    button.addEventListener('click', () => {
      const mode = GraphView.setDetail(button.dataset.detail || 'step');
      $$('#graphGran .tb').forEach((one) => {
        one.classList.toggle('on', one.dataset.detail === mode);
      });
      // 切完粒度整张图的宽高都变了，镜头得重新框一次
      GraphView.fit();
      const layout = renderGraph();
      if (layout) {
        $('#graphTitle').textContent = layout.cards.length
          ? `${layout.lanes.length} 轮 · ${layout.cards.length} 个节点`
          : '还没有节点';
      }
    });
  });
  bindGraphFilters();
  $('#copyMermaid').addEventListener('click', async () => {
    if (!state.cid) return toast('还没有会话', 'bad');
    const text = await API.mermaid(state.cid);
    try {
      await navigator.clipboard.writeText(text);
      toast('Mermaid 已复制，可以贴到支持它的地方', 'good');
    } catch {
      Modal.open({
        title: 'Mermaid',
        body: h('pre.code', text),
        actions: [{ label: '关掉', kind: 'primary', onClick: () => Modal.close() }],
      });
    }
  });

  $('#optBtn').addEventListener('click', () => runOptimize());
  $('#skillBtn').addEventListener('click', () => openPlus());
  $('#optClose').addEventListener('click', closeOptimize);
  $('#optRetry').addEventListener('click', () => runOptimize(optTemplateId));
  $('#optSave').addEventListener('click', async () => {
    if (!state.optimize || !state.optimize.optimized) return toast('还没有可存的东西', 'bad');
    await Refs.savePrompt(state.optimize.optimized, '优化后', {
      tags: ['优化'],
      title: String(state.optimize.original || '').split('\n')[0].slice(0, 30),
    });
  });
  $('#optApply').addEventListener('click', () => {
    if (!state.optimize) return;
    input.value = state.optimize.optimized;
    autoGrow(input);
    updateComposer();
    closeOptimize();
    input.focus();
  });

  $('#retryBtn').addEventListener('click', retryLast);
  $('#exportBtn').addEventListener('click', () => { closeMenus(); openExport(); });
  bindChrome();

  /* 右栏：顶上那排图标是「只看哪一类」，最右是收起。
     收起之后靠中栏头那枚方按钮再打开 —— 两个方向都能走通。 */
  const head = $('#rhead');
  if (head) {
    head.addEventListener('click', (event) => {
      const tab = event.target.closest('.rtab');
      if (tab) { setRightTab(tab.dataset.card); return; }
      if (event.target.closest('#rpanelFold')) setRightOpen(false);
    });
  }
  const fold = $('#rpanelBtn');
  if (fold) fold.addEventListener('click', () => setRightOpen(!!$('#rpanel') && $('#rpanel').classList.contains('min')));
  let rightWasOpen = true;
  try { rightWasOpen = localStorage.getItem('dreamrole.rightOpen') !== '0'; } catch (err) { /* 隐身模式 */ }
  setRightOpen(rightWasOpen);
  setRightTab('all');

  $('#modal').addEventListener('click', (event) => {
    if (event.target.id === 'modal') Modal.close();
  });

  document.addEventListener('keydown', (event) => {
    const meta = event.ctrlKey || event.metaKey;
    if (meta && (event.key === 'k' || event.key === 'K')) {
      event.preventDefault();
      Palette.isOpen() ? Palette.close() : Palette.open('');
      return;
    }
    /* Ctrl+1..9 直接切左栏。功能一多，靠鼠标在图标栏里找会变慢，
       而「第几栏」是有肌肉记忆的。 */
    if (meta && /^[1-9]$/.test(event.key) && !event.shiftKey) {
      /* 只数**看得见**的那几栏：`sections()` 里还挂着插件页下面的三个 tab
         （专家 / 技能 / 工具），它们不占导航行，算进编号会让 Ctrl+4 跳到一个
         界面上找不到的位置。 */
      const sections = Sidebar.sections().filter((one) => !one.hidden);
      const target = sections[Number(event.key) - 1];
      if (target) {
        event.preventDefault();
        Sidebar.show(target.id);
        return;
      }
    }
    if (event.key === 'Escape') {
      if (Palette.isOpen()) Palette.close();
      else if (Modal.isOpen()) Modal.close();
      else if (!$('#skillPop').classList.contains('hidden')) closeSkillPop();
      else if (state.cardOpen) GraphView.close();
      else if (!$('#optPanel').classList.contains('hidden')) closeOptimize();
      else if (Artifacts.isOpen()) Artifacts.close();
    }
    if (event.altKey && (event.key === 'o' || event.key === 'O')) {
      event.preventDefault();
      runOptimize();
    }
    if (event.altKey && (event.key === 's' || event.key === 'S')) {
      event.preventDefault();
      toggleSkillPop();
    }
    if (event.altKey && (event.key === 'r' || event.key === 'R')) {
      event.preventDefault();
      retryLast();
    }
    if (event.altKey && (event.key === 't' || event.key === 'T')) {
      event.preventDefault();
      toggleTodos();
    }
    /* Alt 系列是这个程序自己的「动作」键位（Alt+O 优化、Alt+R 重试），
       产物也放这儿，别去抢 Ctrl+P 那种浏览器已经占住的组合。 */
    if (event.altKey && (event.key === 'a' || event.key === 'A')) {
      event.preventDefault();
      Artifacts.toggle();
    }
  });

  // 优化面板里的「评审」按钮是动态插进去的，这里补一个入口
  $('#optPanel').addEventListener('click', (event) => {
    if (event.target.id === 'judgeBtn') askJudge();
  });
}

/* 对 Panels 暴露的最小接口，避免两边互相 import */
const AppFacade = {
  state,
  newSession,
  useText(text) {
    $('#input').value = text;
    autoGrow($('#input'));
    updateComposer();
    $('#input').focus();
  },
  openTrace,
  renderTraces,
  renderModes,
  /* 技能面板里的「用这个」：把技能挂到输入栏上，而不是往输入框里塞一句话。
     塞一句话的话，模型还得再 skill_load 一次，而且那句话会留在对话记录里。 */
  pickSkill(name) {
    addSkill(name);
    $('#input').focus();
    toast('这一轮会用「' + name + '」', 'good');
  },
  /* 技能被关掉时，把这一轮挂着的那枚 chip 摘下来。
     不摘的话系统提示里已经不报它了，可 chip 还亮着 —— 用户会以为它还在。 */
  dropSkill(name) {
    if (!state.skillPick.includes(name)) return;
    dropSkill(name);
    if (typeof Sidebar !== 'undefined' && Sidebar.active() === 'skills') Sidebar.refresh();
  },
  /* 助理 / 群聊入口：建一条会话再切过去。
     会话是唯一「能说话的东西」，所以助理不能自己开一个平行体系 ——
     点「聊」就是开一条带人设的会话，行为跟别的会话一模一样。
     这里原来也不许在「这一轮还在跑」的时候开，同样去掉了：
     开一条新会话和上一条跑不跑没有关系。 */
  async openWith(payload) {
    const result = await API.createSession(payload);
    if (!result.ok) return toast(result.error, 'bad');
    await renderTraces();
    await openTrace(result.data.cid);
    Sidebar.show('sessions');
    $('#input').focus();
    return result.data;
  },
  /* 团队页的输入框：开（或续）一条群聊/私聊，再把这句话发出去。
   *
   * 为什么不直接在这个页面里发：一轮群聊要真跑起来才有意义，而「跑」这件事
   * 的全部机制（流、权限门、停下、待办条）都在对话页里。团队页做完「派活」这一步，
   * 就交给对话页 —— 用户看到的是同一件事，不是一个只在团队页里活着的假聊天。
   */
  async sendIntoTeam(payload, text) {
    const made = await this.openWith(payload);
    if (!made) return false;
    $('#input').value = text;
    autoGrow($('#input'));
    updateComposer();
    await send();
    return true;
  },
  /* 面板里改了东西之后，让左栏按新数据重画一次 */
  refreshSidebar() {
    Sidebar.refresh();
    renderExpertChip();
  },
  refreshTools() {},
  async refreshBootstrap() {
    const result = await API.bootstrap();
    if (result.ok) {
      state.boot = result.data;
      state.templates = result.data.templates || [];
      renderPermission();
      renderExpertChip();
    }
  },
  get project() {
    return state.project;
  },
};

window.addEventListener('DOMContentLoaded', async () => {
  bind();
  Panels.init(AppFacade);
  Artifacts.init(AppFacade);
  Sidebar.init(AppFacade);
  Dock.init();
  Palette.init({
    state,
    switchMode,
    switchView,
    openTrace,
    newSession,
    retry: retryLast,
    exportAs: (fmt) => {
      if (!state.cid) return toast('还没有会话', 'bad');
      window.open(API.exportUrl(state.cid, fmt), '_blank');
    },
    pickProject,
    optimize: () => runOptimize(),
    artifacts: () => Artifacts.toggle(),
    sections: () => Sidebar.sections(),
    showSection: (id) => Sidebar.show(id),
  });
  await bootstrap();

  /* 界面真的画出来了才回这一句。
     上一句（index.html 里那个）只证明脚本开始跑了 —— 它可能在 bootstrap 里就断了，
     而窗口看起来完全正常。桌面外壳等的是这一句：等不到就换浏览器窗口打开。 */
  if (window.__dreamroleRendered) window.__dreamroleRendered();
  /* 从这一刻起，界面是「已经在用的界面」了。
     index.html 里那张红屏只负责启动阶段的错 —— 启动完还把整块界面盖成报错页，
     等于因为一个按钮出错就假装整个软件坏了。运行期的错改走顶部那条错误带。 */
  window.__dreamroleReady = true;
});
