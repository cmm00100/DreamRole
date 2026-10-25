/* 过程图：把事件流折叠成「卡片」，按阶段排成几列。
 *
 * 这条思路来自 ComfyUI 那类节点图：**一个节点是一个「步骤」，不是一个事件**。
 * 一次思考 + 它触发的所有工具调用 + 那些调用返回的结果，合成一张卡；
 * 你说的话是一张卡，它的答复是一张卡。于是「这一屏上有几件事」和
 * 「这一步在干什么」对得上 —— 事件级的图会把一次 read_file 拆成
 * 「动作」「结果」两个节点，看十步就是二十个节点，全是噪音。
 *
 * 布局规则：**列是阶段，行是同一阶段里的先后**。
 * 同阶段的卡从上往下连，跨阶段从右往左连，全部是流动的虚线 —— 那是 ComfyUI 的手感。
 * 阶段本身是**拖不动的虚线背景带**：它不是节点，是画在下面的坐标纸。
 *
 * ---------------------------------------------------------------------------
 * 关于动画和手感，几个决定：
 *
 * 1. **不做「每次重画整张图」**。按 id 做增删改，只有**新来的**那张卡有入场动画。
 * 2. **镜头是「视野框」不是缩放重排**。viewBox 当镜头，缩放平移都不碰 DOM 结构。
 * 3. **拖动 1:1、滚轮平滑、松手有惯性**。拖动实时跟手（等一帧都算迟），
 *    滚轮带阻尼，松手减速。
 * 4. **卡片能点开**。点一次展开、再点一次收起，同时只能开一张 ——
 *    层层叠起来的抽屉，最后没人知道自己点的是哪张。
 */
const GraphView = (() => {
  // ---------------------------------------------------------------- 几何
  /* 尺寸全部照设计稿 v2 来：节点 172 宽、10 圆角、左边一条 3px 色条，
     标题 10.5、副行 9 —— 一屏能摆下四列，再多就换行。 */
  const CARD_W = 172;
  const CARD_H = 46;
  const NODE_PAD_L = 12;
  const NODE_STEP_X = 244;      // 设计稿 10 → 300 → 540 → 776，取 244 的等距
  const NODE_STEP_Y = 62;
  const PER_ROW = 4;

  const LANE_X = 18;            // 泳道左右各留 18
  const LANE_Y = 22;            // 画布顶到第一条泳道
  const LANE_TOP_PAD = 34;      // 泳道顶到第一排节点
  const LANE_BOTTOM_PAD = 14;
  const LANE_GAP = 20;          // 泳道之间

  const MINI_W = 150;
  const MINI_H = 74;
  const MINI_PAD = 5;

  const MIN_Z = 0.1;
  const MAX_Z = 2.2;
  const EDGE_DRAW_MS = 560;
  const DRAG_SLOP = 4;          // 移动不到这么多像素，算点击不算拖动
  const FOLLOW_IDLE = 5000;     // 用户最后一次操作之后多久，才允许自动跟镜头
  const TWEEN_HALF_LIFE = 45;   // 镜头补间：每过这么多毫秒，剩下的距离走一半
  const TWEEN_MAX_MS = 900;     // 兜底：再怎么样也在这么久之内落到终点

  /* 节点类型 → 左边色条 / 图标 / 标题前缀。设计稿里就是六种：
     用户、助手、工具、子代理、审批、产出。 */
  const NODE_META = {
    user: { label: '用户输入', icon: '◉', grad: 'gUser', flat: '#4a78e0' },
    assist: { label: '助手', icon: '✳', grad: 'gAssist', flat: '#9aa1aa' },
    tool: { label: '工具', icon: '⚙', grad: 'gTool', flat: '#2f9d8e' },
    sub: { label: '子代理', icon: '⬡', grad: 'gSub', flat: '#8f6fe0' },
    appr: { label: '审批', icon: '⚠', grad: 'gAppr', flat: '#d1a52f' },
    out: { label: '产出', icon: '▤', grad: 'gOut', flat: '#4fa96b' },
  };

  /* 这几种工具会把卡片的类型「顶上去」：一次写入比一次读取更值得被看见。
     优先级从高到低 —— 一张卡里既有子代理又有写入时，它首先是子代理。 */
  const SUB_TOOL = /^(task|subagent|delegate|spawn_agent|agent)$/i;
  const APPR_TOOL = /^(approve|confirm|ask_permission|request_approval)$/i;
  const WRITE_TOOL = /write|edit|patch|create|save_file|move|copy|mkdir|delete|remove/i;

  const EVENT_LABEL = {
    user: '用户', think: '思考', action: '动作', result: '结果', answer: '回答', notice: '提示',
  };

  const BAD = /出错|错误|Error|ERROR|Traceback/;

  const state = {
    // 数据
    cards: [],
    layout: null,
    host: null,
    onOpen: null,          // 卡片开合的回调，界面那边用它画抽屉
    onLocate: null,        // 双击节点的回调：跳到会话流里那一条
    openId: null,
    hoverId: null,         // 鼠标停在哪张卡上 —— 它的上下游要高亮
    scrub: null,           // 时间轴拖到哪儿（null = 跟着「现在」）
    detail: 'step',        // 粒度：step 一步一个节点 / round 一轮一个节点
    running: false,
    follow: true,
    hidden: {},            // 工具条上关掉的节点类型，比如「审批」
    flow: true,            // 流光：连线上的流动虚线要不要动
    timeline: null,        // 底部那条活动带（HTML，不归镜头管）
    // DOM
    svg: null,
    backdrop: null,        // 跟着镜头走的点阵层
    viewport: null,        // 唯一的 <g>，镜头作用在它的 transform 上
    laneLayer: null,
    edgeLayer: null,
    cardLayer: null,
    cardEls: new Map(),    // id -> { g, wrap, x, y, sig }
    laneEls: new Map(),    // round -> { g, rect, tag }
    edgeEls: new Map(),    // "a>b" -> { path, cross }
    mini: null,            // { svg, inner, view, s, ox, oy }
    // 镜头
    cam: { x: 0, y: 0, z: 1 },
    target: { x: 0, y: 0, z: 1 },
    fitted: false,
    fitFor: null,          // 上次取景时的画布尺寸，用来判断「窗口是不是变了」
    // 交互
    drag: null,
    velocity: { x: 0, y: 0, t: 0 },
    userMovedAt: -1e9,
    raf: 0,                // rAF 句柄（不是「有没有在跑」的开关，那个是 pending）
    pending: false,        // 已经排了一帧，还没跑到
    watch: 0,              // 兜底落地的定时器
    last: 0,               // 上一帧的时间戳，用来算帧间隔
    observer: null,
    onZoom: null,
  };

  const still = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // ---------------------------------------------------------------- 文本

  const WIDE = /[\u2e80-\u9fff\u3000-\u303f\uff00-\uffef\uac00-\ud7af]/;

  /* 中文按两个宽度算，英文按一个 —— 否则中英混排的截断会一边长一边短 */
  function units(text) {
    let total = 0;
    for (const char of String(text || '')) total += WIDE.test(char) ? 2 : 1;
    return total;
  }

  function cut(text, maxUnits) {
    let out = '';
    let used = 0;
    for (const char of String(text || '')) {
      const width = WIDE.test(char) ? 2 : 1;
      if (used + width > maxUnits) return out;
      out += char;
      used += width;
    }
    return out;
  }

  function clip(text, maxUnits) {
    const flat = String(text || '').replace(/\s+/g, ' ').trim();
    if (units(flat) <= maxUnits) return flat;
    return cut(flat, Math.max(1, maxUnits - 1)) + '…';
  }

  // ---------------------------------------------------------------- 卡片

  /* 把事件流折叠成卡片：一张卡 = 一个步骤（一次思考 + 它触发的所有动作/结果）。
     每一句话开一个新「轮次」—— 泳道是按轮次分的，一轮一条。 */
  function buildCards(events) {
    const cards = [];
    const titles = {};
    let cur = null;
    let round = 0;

    const startStep = (event) => {
      cur = {
        id: 'c-' + event.id,
        kind: 'step',
        stage: Number(event.stage || 0),
        stageTitle: '',
        round: Math.max(1, round),
        text: '',
        rows: [],
        events: [],
      };
      cards.push(cur);
      return cur;
    };

    for (const event of events || []) {
      const stage = Number(event.stage || 0);
      const payload = event.payload || {};
      if (event.stage_title) titles[stage] = event.stage_title;

      // 阶段标记只当列标题：它自己不进卡片，后面的事件已经带上了新的 stage
      if (event.type === 'notice' && payload.stage_marker) continue;

      /* 阶段换了就必须新开一张卡。否则一个阶段里的动作会一路折进上一阶段的卡里，
         图上只剩一列，几个阶段连成一个都不见了。 */
      if (cur && cur.stage !== stage) cur = null;

      if (event.type === 'user' || event.type === 'answer') {
        if (event.type === 'user') round += 1;
        cur = {
          id: 'c-' + event.id,
          kind: event.type,
          stage,
          stageTitle: '',
          round: Math.max(1, round),
          text: event.content || '',
          rows: [],
          events: [event],
        };
        cards.push(cur);
        continue;
      }

      if (event.type === 'think') {
        startStep(event);
        cur.text = event.content || '';
        cur.events.push(event);
        continue;
      }

      if (event.type === 'action') {
        if (!cur || cur.kind !== 'step') startStep(event);
        cur.rows.push({ action: event, result: null, note: null, hint: hintOf(event) });
        cur.events.push(event);
        continue;
      }

      if (event.type === 'result') {
        const row = cur && cur.kind === 'step' ? cur.rows[cur.rows.length - 1] : null;
        if (row && !row.result) {
          row.result = event;
          cur.events.push(event);
        } else {
          startStep(event);
          cur.rows.push({ action: null, result: event, note: null, hint: '' });
          cur.events.push(event);
        }
        continue;
      }

      if (event.type === 'notice') {
        if (!cur || cur.kind !== 'step') startStep(event);
        cur.rows.push({ action: null, result: null, note: event, hint: '' });
        cur.events.push(event);
      }
    }

    for (const card of cards) {
      card.stageTitle = card.stageTitle
        || titles[card.stage]
        || (card.stage === 0 ? '准备' : `阶段 ${card.stage + 1}`);
      const first = card.events[0] || {};
      const last = card.events[card.events.length - 1] || first;
      card.ts = Number(first.ts || 0);
      card.endTs = Number(last.ts || card.ts);
      card.type = typeOf(card);
    }
    return cards;
  }

  /* 粒度「轮次」：把一轮里的所有节点收成一个。
   *
   * 为什么需要它：一段跑了四十步的对话，图上就是四十张卡连成一条长蛇 ——
   * 「它一共做了几轮、每轮大概干了什么」这个最常问的问题反而看不出来。
   * 收成一轮一张之后，长会话也不会把画布撑爆。点开还是能看到这一轮的全部步骤
   * （抽屉里列的是 events，不是卡片）。
   */
  function mergeRounds(cards) {
    const byRound = new Map();
    for (const card of cards) {
      const key = card.round;
      let one = byRound.get(key);
      if (!one) {
        one = {
          id: 'r-' + key,
          kind: 'step',
          merged: true,
          round: card.round,
          stage: card.stage,
          stageTitle: card.stageTitle,
          text: '',
          userText: '',
          answerText: '',
          rows: [],
          events: [],
          ts: card.ts,
          endTs: card.endTs,
        };
        byRound.set(key, one);
      }
      one.rows.push(...card.rows);
      one.events.push(...card.events);
      one.ts = Math.min(one.ts, card.ts);
      one.endTs = Math.max(one.endTs, card.endTs);
      one.stage = Math.min(one.stage, card.stage);
      if (card.kind === 'user' && !one.userText) one.userText = card.text;
      if (card.kind === 'answer' && !one.answerText) one.answerText = card.text;
      if (!one.text) one.text = card.text || '';
    }
    const list = [];
    for (const one of byRound.values()) {
      one.type = typeOf(one);
      list.push(one);
    }
    return list;
  }

  /* 一张卡花了多久、用了多少 token。
   *
   * 耗时是**真的**：取这一串事件的首尾时间戳。
   * token 也是真的，但只有一部分节点有 —— 一次模型调用产生的那个动作 / 那一条答复
   * 会领走这次调用的用量（见 `session.emit`）。没有的就返回 0，界面写「—」，
   * 绝不按整轮总数摊到每一步上：那是编的。
   */
  function costOf(card) {
    const start = Number(card.ts || 0);
    const end = Number(card.endTs || start);
    const sec = start && end ? Math.max(0, end - start) : 0;
    let tokens = 0;
    for (const event of card.events || []) {
      const usage = (event.payload && event.payload.usage) || null;
      if (usage && usage.total) tokens += Number(usage.total) || 0;
    }
    return { sec, tokens };
  }

  function secText(sec) {
    if (!sec) return '—';
    if (sec < 1) return `${Math.round(sec * 1000)} 毫秒`;
    if (sec < 60) return `${sec < 10 ? sec.toFixed(1) : Math.round(sec)} 秒`;
    const m = Math.floor(sec / 60);
    return `${m} 分 ${Math.round(sec - m * 60)} 秒`;
  }

  /* 一张卡是「哪一种节点」。优先级：子代理 > 审批 > 产出 > 工具。
     一张卡里既有子代理又有写入时，它首先是子代理 —— 派出去这件事比写了哪个文件大。 */
  function typeOf(card) {
    if (card.kind === 'user') return 'user';
    if (card.kind === 'answer') return 'assist';
    const tools = card.rows.filter((row) => row.action).map((row) => String(toolName(row.action)));
    if (tools.some((tool) => SUB_TOOL.test(tool))) return 'sub';
    if (card.rows.some((row) => row.note) || tools.some((tool) => APPR_TOOL.test(tool))) return 'appr';
    if (tools.some((tool) => WRITE_TOOL.test(tool))) return 'out';
    if (tools.length) return 'tool';
    return 'assist';
  }

  /* 节点标题：设计稿里是「图标 + 一句话」，比如「⚙ pwsh」。 */
  function headOf(card) {
    const meta = NODE_META[card.type] || NODE_META.assist;
    /* 粒度是「轮次」时，这个节点代表的是一整轮 —— 标题就该是「第 3 轮」，
       写成「⚙ pwsh」等于把一整轮错认成一次工具调用。 */
    if (card.merged) return `${meta.icon} 第 ${card.round} 轮`;
    if (card.type === 'tool') {
      const row = card.rows.find((item) => item.action);
      return `${meta.icon} ${cut(row ? toolName(row.action) : '工具', 9)}`;
    }
    if (card.type === 'sub') {
      const row = card.rows.find((item) => item.action && SUB_TOOL.test(toolName(item.action)));
      const who = row ? cut(toolName(row.action), 7) : '';
      return `${meta.icon} 子代理${who ? ' · ' + who : ''}`;
    }
    if (card.type === 'out') {
      const writes = card.rows.filter((item) => item.action && WRITE_TOOL.test(toolName(item.action)));
      return `${meta.icon} 产出${writes.length > 1 ? ` ${writes.length} 处` : ''}`;
    }
    if (card.type === 'appr') return `${meta.icon} 审批 · 写入`;
    if (card.type === 'user') return `${meta.icon} 用户输入`;
    return `${meta.icon} 助手`;
  }

  /* 副行：一行放得下的、最有信息量的那点东西。 */
  function lineOf(card) {
    const squash = (text) => String(text || '').replace(/\s+/g, ' ').trim();
    if (card.merged) {
      const steps = card.rows.length;
      const first = card.rows[0];
      const head = card.userText ? squash(card.userText) : (card.answerText ? squash(card.answerText) : '');
      return cut(head || `${steps} 步`, 20) + (steps ? ` · ${steps} 步` : '');
    }
    if (card.kind === 'user' || card.kind === 'answer') {
      return cut(squash(card.text), 22) || '（空）';
    }
    if (!card.rows.length) return cut(squash(card.text), 22) || '思考';
    const first = card.rows[0];
    const head = first.hint
      || (first.note ? squash(first.note.content) : '')
      || (first.action ? toolName(first.action) : '动作');
    const more = card.rows.length > 1 ? ` ＋${card.rows.length - 1}` : '';
    return cut(head, 20) + more;
  }

  /* 状态角标：✓ / … / × / 已批 / 待批。 */
  function statusOf(card) {
    if (card.kind !== 'step') return { text: '✓', bad: false };
    let pending = 0;
    let bad = 0;
    for (const row of card.rows) {
      if (row.note) {
        bad += 1;
        continue;
      }
      if (!row.result) {
        pending += 1;
        continue;
      }
      if (row.result.payload && row.result.payload.ok === false) bad += 1;
      else if (BAD.test(String(row.result.content || ''))) bad += 1;
    }
    if (card.type === 'appr') {
      if (bad) return { text: '已批', bad: false };
      return pending ? { text: '待批', bad: false, wait: true } : { text: '已批', bad: false };
    }
    if (bad) return { text: '×', bad: true };
    if (pending) return { text: '…', bad: false, wait: true };
    return { text: '✓', bad: false };
  }

  /* 一张卡上最有信息量的那个参数。全铺出来是噪音，一个都不写又等于没说。 */
  function hintOf(event) {
    const args = (event.payload && event.payload.arguments) || {};
    for (const key of ['path', 'command', 'query', 'url', 'pattern', 'title', 'name', 'text']) {
      const value = args[key];
      if (typeof value === 'string' && value.trim()) return cut(value.trim(), 18);
    }
    if (Array.isArray(args.paths) && args.paths.length) {
      const first = cut(String(args.paths[0]), 14);
      return args.paths.length > 1 ? `${first} 等 ${args.paths.length} 项` : first;
    }
    if (typeof args.content === 'string' && args.content) {
      return `写 ${args.content.length} 字符`;
    }
    return '';
  }

  function metaOf(card) {
    if (!card) return NODE_META.assist;
    return NODE_META[card.type] || NODE_META.assist;
  }

  function toolName(action) {
    const payload = action.payload || {};
    return payload.label || action.tool || '工具';
  }

  /* 节点高度是固定的：标题 + 一行副文案。完整内容在右边「节点详情」里看，
     摆到画布上反而会把图撑成一片字海 —— 图上要看的是「有几步、怎么走的」。 */
  function cardHeight() {
    return CARD_H;
  }

  // ---------------------------------------------------------------- 布局

  /* 泳道 = 一轮。一条道里从左往右排，排满四列换一行。
     为什么不按「阶段」分列：设计和真实使用里，读图的人问的是
     「这一轮它干了什么」，而不是「所有轮的第三步横向比起来怎么样」。 */
  function compute(cards) {
    const visible = cards.filter((card) => !state.hidden[card.type]);
    const byRound = new Map();
    for (const card of visible) {
      const key = Number(card.round) || 1;
      if (!byRound.has(key)) byRound.set(key, []);
      byRound.get(key).push(card);
    }
    const rounds = [...byRound.keys()].sort((a, b) => a - b);

    const placed = new Map();
    const lanes = [];
    const laneW = (PER_ROW - 1) * NODE_STEP_X + CARD_W + LANE_X * 2;
    let y = LANE_Y;
    let lastCard = visible.length ? visible[visible.length - 1] : null;

    rounds.forEach((round, index) => {
      const list = byRound.get(round);
      const rows = Math.max(1, Math.ceil(list.length / PER_ROW));
      const h = LANE_TOP_PAD + (rows - 1) * NODE_STEP_Y + CARD_H + LANE_BOTTOM_PAD;
      const laneTop = y;

      list.forEach((card, i) => {
        const col = i % PER_ROW;
        const row = Math.floor(i / PER_ROW);
        placed.set(card.id, {
          x: LANE_X + col * NODE_STEP_X,
          y: laneTop + LANE_TOP_PAD + row * NODE_STEP_Y,
          w: CARD_W,
          h: CARD_H,
          card,
        });
      });

      const live = Boolean(lastCard) && list.includes(lastCard) && state.running;
      lanes.push({
        round,
        index: index + 1,
        stage: round,                 // 老字段：自检和抽屉还读它
        title: list[0] ? list[0].stageTitle : '',
        x: 0,
        y: laneTop,
        w: laneW,
        h,
        live,
        done: !live,
        from: list[0] ? list[0].ts : 0,
        to: list[list.length - 1] ? list[list.length - 1].endTs : 0,
        count: list.length,
      });
      y = laneTop + h + LANE_GAP;
    });

    /* 连线：相邻两张卡一条。跨轮的那条是虚线 —— 递进和「同一条道里往前走」
       是两种关系，画成一个样子就分不出来了。 */
    const edges = [];
    for (let i = 1; i < visible.length; i += 1) {
      const from = visible[i - 1];
      const to = visible[i];
      if (!placed.has(from.id) || !placed.has(to.id)) continue;
      edges.push({ source: from.id, target: to.id, cross: from.round !== to.round });
    }

    return {
      cards: visible,
      lanes,
      bands: lanes,                  // 老名字，别的地方还在用
      edges,
      placed,
      stages: rounds.length,
      width: laneW,
      height: Math.max(LANE_Y * 2 + CARD_H, y - LANE_GAP + LANE_Y * 0.4),
    };
  }

  // ---------------------------------------------------------------- SVG 小工具

  const SVG_NS = 'http://www.w3.org/2000/svg';

  function svgEl(tag, attrs, text) {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === null || value === undefined) continue;
      node.setAttribute(key, value);
    }
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  // ---------------------------------------------------------------- 镜头

  function viewSize() {
    const host = state.host;
    return {
      w: Math.max(1, host ? host.clientWidth : 1),
      h: Math.max(1, host ? host.clientHeight : 1),
    };
  }

  function apply() {
    const svg = state.svg;
    if (!svg) return;
    const { w, h } = viewSize();
    const cam = state.cam;
    const vw = w / cam.z;
    const vh = h / cam.z;
    svg.setAttribute('viewBox', `${cam.x - vw / 2} ${cam.y - vh / 2} ${vw} ${vh}`);
    // 镜头越远，线越细得看不见 —— 反过来补偿一点，让不同缩放下观感一致
    svg.style.setProperty('--inv-zoom', String(Math.max(0.6, Math.min(2.2, 1 / cam.z))));
    paintBackdrop();
    paintMiniView();
    if (state.onZoom) state.onZoom(cam.z);
  }

  /* 背景点阵跟着镜头走。
     不跟着走的话，拖动时内容在动、纸不动，看着像画面在纸上滑，很容易看晕。
     只改 transform，位移对格子间距取模 —— 格子是重复的，挪一整格与不挪一样。 */
  const GRID = 20;

  function paintBackdrop() {
    const back = state.backdrop;
    if (!back) return;
    const cam = state.cam;
    const wrap = (v) => ((v % GRID) + GRID) % GRID;
    const tx = -wrap(cam.x * cam.z);
    const ty = -wrap(cam.y * cam.z);
    back.style.transform = `translate3d(${tx.toFixed(1)}px, ${ty.toFixed(1)}px, 0)`;
    // 拉得很远的时候，密密麻麻的格子会变成噪点，淡一点
    back.style.opacity = String(Math.max(0.35, Math.min(1, cam.z + 0.25)));
  }

  function settled() {
    const cam = state.cam;
    const to = state.target;
    return Math.abs(to.x - cam.x) < 0.25
      && Math.abs(to.y - cam.y) < 0.25
      && Math.abs(to.z - cam.z) < 0.0006;
  }

  function stopWatch() {
    if (!state.watch) return;
    clearTimeout(state.watch);
    state.watch = 0;
  }

  function stopRaf() {
    if (state.raf) cancelAnimationFrame(state.raf);
    state.raf = 0;
    state.pending = false;
  }

  function snap() {
    state.cam.x = state.target.x;
    state.cam.y = state.target.y;
    state.cam.z = state.target.z;
    stopWatch();
    apply();
  }

  /* 叫醒补间。镜头平时是睡的，只有目标变了才被叫一次。 */
  function wake() {
    // 已经有一帧在路上了，它读的是最新的 target，不用再排
    if (state.pending) return;
    if (still()) {
      snap();
      return;
    }
    state.last = 0;
    state.pending = true;
    state.raf = requestAnimationFrame(tick);
    /* 兜底：rAF 并不是「一定会来」。后台标签页会被挂起，无头浏览器的虚拟
       时钟会把帧饿着 —— 这两种情况下画面会永远停在半路，而没人等得到它。 */
    stopWatch();
    state.watch = setTimeout(() => {
      state.watch = 0;
      stopRaf();
      if (!settled()) snap();
    }, TWEEN_MAX_MS);
  }

  function tick(now) {
    state.pending = false;
    state.raf = 0;
    if (settled()) {
      snap();
      return;
    }
    const cam = state.cam;
    const to = state.target;
    const last = state.last || now;
    state.last = now;
    /* 帧间隔按实际时间算，最长按 64ms 算：从别的标签页切回来时时间差可能是
       好几秒，那一下应该「到」，不该慢动作补一遍。 */
    const dt = Math.min(64, Math.max(0, now - last)) || 16;
    /* 帧率无关的指数逼近：每过 TWEEN_HALF_LIFE 毫秒，剩下的距离走一半。
       写成「每帧走 26%」的话，同一份代码在 144Hz 屏上会比 60Hz 快 2.4 倍。 */
    const k = 1 - Math.pow(0.5, dt / TWEEN_HALF_LIFE);
    cam.x += (to.x - cam.x) * k;
    cam.y += (to.y - cam.y) * k;
    cam.z += (to.z - cam.z) * k;
    apply();
    state.pending = true;
    state.raf = requestAnimationFrame(tick);
  }

  function clampZ(z) {
    return Math.min(MAX_Z, Math.max(MIN_Z, z));
  }

  function fit(animate) {
    if (!state.svg || !state.layout) return false;
    const { w, h } = viewSize();
    // 视图还没显示出来（切到对话页时）就先不取景 —— 否则会把镜头算到 0×0 上
    if (w < 8 || h < 8) return false;
    const box = state.layout;
    const z = clampZ(Math.min(w / box.width, h / box.height) * 0.94);
    const vw = w / z;
    const vh = h / z;
    /* 哪一维比视野小，就在那一维贴左上角，而不是居中。
       设计稿里第一条泳道就长在画布左上角（18, 22）—— 图是从左上往里读的，
       居中会让一个短会话飘在屏幕正中间，四周全是空白。 */
    const pad = 18;
    const x = vw > box.width + pad * 2 ? -pad + vw / 2 : box.width / 2;
    const y = vh > box.height + pad * 2 ? -pad + vh / 2 : box.height / 2;
    const want = { x, y, z: Math.min(z, 1.1) };
    state.fitFor = { w, h };
    if (animate === false) {
      state.cam = { ...want };
      state.target = { ...want };
      apply();
      // 取景之后如果还开着卡，别让它又躲回抽屉底下
      revealOpen();
      return true;
    }
    state.target = want;
    wake();
    revealOpen();
    return true;
  }

  function toLayout(px, py) {
    const { w, h } = viewSize();
    const cam = state.cam;
    return { x: cam.x + (px - w / 2) / cam.z, y: cam.y + (py - h / 2) / cam.z };
  }

  /* 滚轮：以光标为锚点缩放。锚点不动的缩放，手感才对。 */
  function zoomAt(px, py, factor) {
    const { w, h } = viewSize();
    const anchor = toLayout(px, py);
    const z = clampZ(state.target.z * factor);
    state.target.z = z;
    state.target.x = anchor.x - (px - w / 2) / z;
    state.target.y = anchor.y - (py - h / 2) / z;
    if (still()) {
      state.cam.x = state.target.x;
      state.cam.y = state.target.y;
      state.cam.z = state.target.z;
      apply();
      return;
    }
    wake();
  }

  /* 新卡跑到视野外了就把镜头挪过去 —— 但只在用户最近没动过的时候。
     用户正在自己拖的时候硬拽镜头，是最讨人厌的一种「智能」。 */
  function follow(placed) {
    if (!state.follow) return;
    if (performance.now() - state.userMovedAt < FOLLOW_IDLE) return;
    const { w, h } = viewSize();
    const cam = state.cam;
    const vw = w / cam.z;
    const vh = h / cam.z;
    const margin = 40 / cam.z;
    const left = cam.x - vw / 2;
    const top = cam.y - vh / 2;
    const right = cam.x + vw / 2;
    const bottom = cam.y + vh / 2;
    if (placed.x > left + margin && placed.x + placed.w < right - margin
      && placed.y > top + margin && placed.y + placed.h < bottom - margin) {
      return;
    }
    state.target.x = placed.x + placed.w / 2;
    state.target.y = placed.y + placed.h / 2;
    wake();
  }

  // ---------------------------------------------------------------- 构建

  function buildSvg(host) {
    host.textContent = '';

    const back = document.createElement('div');
    back.className = 'gBackdrop';

    const svg = svgEl('svg', { class: 'gCanvas', preserveAspectRatio: 'xMidYMid meet' });
    const defs = svgEl('defs');

    // 卡片底：极淡的竖向渐变，纯色看着是「一块板」，渐变才像「一张卡」
    const body = svgEl('linearGradient', { id: 'gBodyGrad', x1: 0, y1: 0, x2: 0, y2: 1 });
    body.appendChild(svgEl('stop', { offset: '0%', 'stop-color': '#21252c' }));
    body.appendChild(svgEl('stop', { offset: '100%', 'stop-color': '#1b1e24' }));
    defs.appendChild(body);

    // 每条泳道左上角那条色条，六种类型六套渐变（照设计稿取色）
    const BARS = {
      gUser: ['#7aa7ff', '#4a78e0'],
      gAssist: ['#c3c9d1', '#9aa1aa'],
      gTool: ['#63d3c2', '#2f9d8e'],
      gSub: ['#c3a8ff', '#8f6fe0'],
      gAppr: ['#f0cd72', '#d1a52f'],
      gOut: ['#8ee0a6', '#4fa96b'],
    };
    for (const [id, stops] of Object.entries(BARS)) {
      const grad = svgEl('linearGradient', { id, x1: 0, y1: 0, x2: 0, y2: 1 });
      grad.appendChild(svgEl('stop', { offset: '0%', 'stop-color': stops[0] }));
      grad.appendChild(svgEl('stop', { offset: '100%', 'stop-color': stops[1] }));
      defs.appendChild(grad);
    }
    const hot = svgEl('linearGradient', { id: 'gHot', x1: 0, y1: 0, x2: 1, y2: 0 });
    hot.appendChild(svgEl('stop', { offset: '0%', 'stop-color': '#3d63dd' }));
    hot.appendChild(svgEl('stop', { offset: '100%', 'stop-color': '#8fb6ff' }));
    defs.appendChild(hot);
    svg.appendChild(defs);

    const viewport = svgEl('g');
    const laneLayer = svgEl('g');
    const edgeLayer = svgEl('g');
    const cardLayer = svgEl('g');
    viewport.appendChild(laneLayer);
    viewport.appendChild(edgeLayer);
    viewport.appendChild(cardLayer);
    svg.appendChild(viewport);

    /* 小地图：设计稿里它不是一块裸图，是一张带标题的卡 —— 光看图不知道能拖 */
    const miniCard = document.createElement('div');
    miniCard.className = 'gMiniCard';
    miniCard.appendChild(h('div.gMiniTtl', '小地图 · 可拖拽'));
    const miniBox = document.createElement('div');
    miniBox.className = 'gMiniBox';
    const mini = svgEl('svg', { class: 'gMini', viewBox: `0 0 ${MINI_W} ${MINI_H}` });
    const miniInner = svgEl('g');
    const miniView = svgEl('rect', { class: 'gMiniView', rx: 3 });
    mini.appendChild(miniInner);
    mini.appendChild(miniView);
    miniBox.appendChild(mini);
    miniCard.appendChild(miniBox);

    host.appendChild(back);
    host.appendChild(svg);
    host.appendChild(buildLegend());
    host.appendChild(miniCard);

    state.svg = svg;
    state.backdrop = back;
    state.viewport = viewport;
    state.laneLayer = laneLayer;
    state.edgeLayer = edgeLayer;
    state.cardLayer = cardLayer;
    state.cardEls.clear();
    state.laneEls.clear();
    state.edgeEls.clear();
    state.mini = { svg: mini, inner: miniInner, view: miniView, s: 1, ox: 0, oy: 0 };
    host.classList.toggle('noflow', !state.flow);

    bindDrag(svg);
    bindMini(mini);
    observeResize(host);
  }

  /* 图例：六种节点 + 三种线，摆在画布左下角。颜色是这张图的语法，
     不解释一遍，第一次看的人只能猜。 */
  function buildLegend() {
    const box = h('div.gLegend');
    box.appendChild(h('div.gLegendTtl', '图例'));
    const grid = h('div.gLegendGrid');
    [['user', '用户输入'], ['assist', '助手步骤'], ['tool', '工具调用'],
      ['sub', '子代理'], ['appr', '审批'], ['out', '产出']].forEach(([type, name]) => {
      const meta = NODE_META[type];
      grid.appendChild(h('span.gLegendItem',
        h('i', { style: `background:linear-gradient(${meta.flat},${meta.flat}aa)` }),
        name));
    });
    box.appendChild(grid);
    box.appendChild(h('div.gLegendFoot',
      h('span', '—— 顺序'),
      h('span.purple', '- - 委派 / 跨轮'),
      h('span.blue', '✦ 实时执行')));
    return box;
  }

  function teardown() {
    if (state.observer) {
      state.observer.disconnect();
      state.observer = null;
    }
    stopRaf();
    stopWatch();
    state.last = 0;
    state.svg = null;
    state.backdrop = null;
    state.viewport = null;
    state.laneLayer = null;
    state.edgeLayer = null;
    state.cardLayer = null;
    state.cardEls.clear();
    state.laneEls.clear();
    state.edgeEls.clear();
    state.mini = null;
    state.layout = null;
    state.fitted = false;
    state.fitFor = null;
    state.drag = null;
  }

  function observeResize(host) {
    if (state.observer) state.observer.disconnect();
    if (typeof ResizeObserver === 'undefined') return;
    state.observer = new ResizeObserver(() => {
      const { w, h } = viewSize();
      const fitFor = state.fitFor;
      const drifted = !fitFor || Math.abs(fitFor.w - w) > 24 || Math.abs(fitFor.h - h) > 24;
      // 窗口大小变了、而且用户最近没自己动过镜头 —— 重新铺满
      if (!state.fitted || (drifted && performance.now() - state.userMovedAt > 1500)) {
        if (fit(false)) state.fitted = true;
        return;
      }
      apply();
    });
    state.observer.observe(host);
  }

  // ---------------------------------------------------------------- 泳道

  /* 泳道是画在节点下面的一层：它不是节点，是「这一轮」的底盘。
     标签直接写在道口左上（设计稿那样），不做胶囊 —— 一条道上没有别的东西要抢位置。 */
  function drawLanes(layout) {
    const layer = state.laneLayer;
    if (!layer) return;
    const alive = new Set();

    layout.lanes.forEach((lane, order) => {
      alive.add(lane.round);
      let entry = state.laneEls.get(lane.round);
      if (!entry) {
        const g = svgEl('g', { class: 'gStage' });
        const rect = svgEl('rect', { class: 'gLane', rx: 12 });
        const tag = svgEl('text', { class: 'gLaneTag', x: 12, y: 8 });
        g.appendChild(rect);
        g.appendChild(tag);
        layer.appendChild(g);
        // 新出现的道淡入一下，老的道都不动 —— 否则每加一轮，所有轮都闪一遍
        if (!still()) {
          g.classList.add('gIn');
          g.style.animationDelay = `${Math.min(order * 40, 240)}ms`;
        }
        entry = { g, rect, tag };
        state.laneEls.set(lane.round, entry);
      }

      entry.g.classList.toggle('live', lane.live);
      entry.rect.setAttribute('x', lane.x);
      entry.rect.setAttribute('y', lane.y);
      entry.rect.setAttribute('width', lane.w);
      entry.rect.setAttribute('height', lane.h);
      entry.tag.setAttribute('x', 12);
      entry.tag.setAttribute('y', 9);
      // 标签贴的是「道顶往下 16」这个位置，不是 SVG 的 y —— 用 transform 挪更省事
      entry.tag.setAttribute('transform', `translate(0, ${lane.y + 14})`);

      const clock = clockOf(lane.from);
      const parts = [`第 ${lane.index} 轮`];
      if (lane.title) parts.push(lane.title);
      if (clock) parts.push(clock);
      parts.push(lane.live ? '进行中' : '已完成');
      const text = clip(parts.join(' · '), 40);
      if (entry.tag.textContent !== text) entry.tag.textContent = text;
    });

    for (const [round, entry] of state.laneEls) {
      if (alive.has(round)) continue;
      entry.g.remove();
      state.laneEls.delete(round);
    }
  }

  function clockOf(ts) {
    if (!ts) return '';
    return new Date(ts * 1000).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
  }

  // ---------------------------------------------------------------- 连线

  /* 同一排里相邻的两张：一条横线。换排、换轮：走一条「总线」——
     先往下走到两条泳道之间的缝，横过去，再下到下一张的顶边。
     为什么不用一条大弧：跨轮时目标在左边，弧线要横穿整块画布、压过一堆节点，
     看着像「这里断了」。肘形线一眼就是「这一轮结束，接下一轮」，不用解释。 */
  function edgePath(edge, layout) {
    const from = layout.placed.get(edge.source);
    const to = layout.placed.get(edge.target);
    if (!from || !to) return null;
    const y1 = from.y + from.h / 2;
    const y2 = to.y + to.h / 2;
    if (Math.abs(y1 - y2) < 1) {
      return `M${from.x + from.w} ${y1} L${to.x} ${y2}`;
    }
    const x1 = from.x + from.w / 2;
    const by = from.y + from.h;
    const x2 = to.x + 14;
    const ty = to.y;
    const gap = by + Math.max(9, (ty - by) / 2);
    if (Math.abs(x2 - x1) < 1) return `M${x1} ${by} L${x2} ${ty}`;
    const dir = x2 > x1 ? 1 : -1;
    const r = 6;
    return `M${x1} ${by} L${x1} ${gap - r}`
      + ` Q${x1} ${gap} ${x1 + dir * r} ${gap}`
      + ` L${x2 - dir * r} ${gap}`
      + ` Q${x2} ${gap} ${x2} ${gap + r}`
      + ` L${x2} ${ty}`;
  }

  function drawEdges(layout) {
    const layer = state.edgeLayer;
    const alive = new Set();
    let added = 0;
    const runningId = runningCardId(layout);

    for (const edge of layout.edges) {
      const key = `${edge.source}>${edge.target}`;
      alive.add(key);
      const d = edgePath(edge, layout);
      if (!d) continue;

      let entry = state.edgeEls.get(key);
      if (!entry) {
        const path = svgEl('path', { d, class: 'gEdge' + (edge.cross ? ' cross' : '') });
        layer.appendChild(path);
        entry = { path, cross: edge.cross, spark: null };
        state.edgeEls.set(key, entry);
        drawIn(path, added);
        added += 1;
      } else if (entry.path.getAttribute('d') !== d) {
        entry.path.setAttribute('d', d);
      }

      /* 「热」的那条：正在跑的那一步，以及它连出去的那条。
         光点顺着这条线走（design 里的 ✦），是这一屏唯一一处「在动的地方」。 */
      const hot = Boolean(state.running) && runningId
        && (edge.source === runningId || edge.target === runningId);
      entry.path.classList.toggle('hot', hot);
      if (hot && !entry.spark && !still()) {
        const spark = svgEl('circle', { class: 'gSpark', r: 3 });
        const motion = svgEl('animateMotion', { dur: '1.6s', repeatCount: 'indefinite', path: d });
        spark.appendChild(motion);
        layer.appendChild(spark);
        entry.spark = spark;
      } else if (!hot && entry.spark) {
        entry.spark.remove();
        entry.spark = null;
      } else if (hot && entry.spark) {
        const motion = entry.spark.querySelector('animateMotion');
        if (motion) motion.setAttribute('path', d);
      }
    }

    for (const [key, entry] of state.edgeEls) {
      if (alive.has(key)) continue;
      entry.path.remove();
      if (entry.spark) entry.spark.remove();
      state.edgeEls.delete(key);
    }
  }

  /* 连线「长出来」：把总长度归一化成 1，让 dashoffset 从 1 走到 0。
     为什么用定时器收尾而不是 animationend：开了「减少动态效果」的人不会触发动画事件，
     那样 pathLength 会一直留着，虚线就变实线了。 */
  function drawIn(path, order) {
    if (still()) return;
    path.setAttribute('pathLength', '1');
    path.style.strokeDasharray = '1';
    path.classList.add('gDraw');
    const delay = Math.min(order * 24, 220);
    path.style.animationDelay = `${delay}ms`;
    setTimeout(() => {
      path.classList.remove('gDraw');
      path.removeAttribute('pathLength');
      path.style.strokeDasharray = '';
      path.style.animationDelay = '';
    }, EDGE_DRAW_MS + delay + 60);
  }

  // ---------------------------------------------------------------- 节点

  function signature(card, height) {
    const st = statusOf(card);
    return `${height}|${card.type}|${headOf(card)}|${lineOf(card)}|${st.text}`;
  }

  function makeCard(placed, order) {
    const g = svgEl('g', { class: 'gCard' });
    g.dataset.id = placed.card.id;
    g.dataset.kind = placed.card.kind;

    /* 光晕和进度环挂在 g 上（不是 wrap 里）：wrap 有自己的入场/悬停动画，
       套在一起会让光晕跟着抖，而光晕的意思是「它还在跑」，不该抖。 */
    const halo = svgEl('rect', {
      class: 'gHalo', x: -10, y: -10, width: placed.w + 20, height: placed.h + 20, rx: 16,
    });
    const halo2 = svgEl('rect', {
      class: 'gHalo d2', x: -10, y: -10, width: placed.w + 20, height: placed.h + 20, rx: 16,
    });
    halo.style.display = 'none';
    halo2.style.display = 'none';
    g.appendChild(halo);
    g.appendChild(halo2);

    const wrap = svgEl('g', { class: 'gWrap' });
    wrap.style.transformOrigin = `${placed.w / 2}px ${placed.h / 2}px`;
    g.appendChild(wrap);

    if (!still()) {
      wrap.classList.add('gIn');
      wrap.style.animationDelay = `${Math.min(order * 28, 280)}ms`;
    }

    /* 悬停高亮上下游：这张卡是「为了什么」才出现的、又「带出了什么」。
       点开一张卡也能看到同样的路径，但那是先点再看；看一眼不用点。 */
    g.addEventListener('pointerenter', () => setHover(placed.card.id));
    g.addEventListener('pointerleave', () => setHover(null));

    return {
      g, wrap, halo, halo2, x: NaN, y: NaN, sig: '', card: placed.card,
    };
  }

  /* 高亮 hoverId 的上下游。只动 class，不重排 —— 鼠标划过不该让图抖一下。 */
  function setHover(id, force) {
    if (state.hoverId === id && !force) return;
    state.hoverId = id;
    const near = new Set();
    if (id) {
      near.add(id);
      for (const key of state.edgeEls.keys()) {
        const [from, to] = key.split('>');
        if (from === id) near.add(to);
        if (to === id) near.add(from);
      }
    }
    for (const [key, entry] of state.edgeEls) {
      const [from, to] = key.split('>');
      entry.path.classList.toggle('nearEdge', Boolean(id) && (from === id || to === id));
      entry.path.classList.toggle('faded', Boolean(id) && from !== id && to !== id);
    }
    for (const [cardId, entry] of state.cardEls) {
      entry.g.classList.toggle('near', Boolean(id) && cardId !== id && near.has(cardId));
      entry.g.classList.toggle('faded', Boolean(id) && !near.has(cardId));
    }
  }

  /* 一个节点就三样东西：左边一条色条、标题、副行。
     想读全文的人在右边「节点详情」里读 —— 画布上堆字，图就没了。 */
  function paintCard(entry, placed) {
    const card = placed.card;
    const meta = NODE_META[card.type] || NODE_META.assist;
    const w = placed.w;
    const h = placed.h;
    const wrap = entry.wrap;
    wrap.textContent = '';

    wrap.appendChild(svgEl('rect', { class: 'gCardBg', width: w, height: h, rx: 10, fill: 'url(#gBodyGrad)' }));
    wrap.appendChild(svgEl('rect', {
      class: 'gBar', x: 0, y: 8, width: 3, height: Math.max(6, h - 16), rx: 2,
      fill: `url(#${meta.grad})`,
    }));

    wrap.appendChild(svgEl('text', { class: 'gHead', x: NODE_PAD_L, y: 19 }, headOf(card)));
    wrap.appendChild(svgEl('text', { class: 'gLine', x: NODE_PAD_L, y: 33 }, lineOf(card)));

    const st = statusOf(card);
    const shown = st.text;
    const bw = Math.max(15, units(shown) * 5.1 + 9);
    const badge = svgEl('g', { class: 'gBadge' + (st.bad ? ' bad' : ''), transform: `translate(${w - 9 - bw}, 8)` });
    badge.appendChild(svgEl('rect', { class: 'gBadgeBg', width: bw, height: 13, rx: 4 }));
    badge.appendChild(svgEl('text', {
      class: 'gRowState' + (st.bad ? ' bad' : ''),
      x: bw / 2,
      y: 9.6,
      'text-anchor': 'middle',
    }, shown));
    wrap.appendChild(badge);
  }

  function paintOpen() {
    for (const [id, entry] of state.cardEls) {
      entry.g.classList.toggle('open', id === state.openId);
    }
    for (const [key, entry] of state.edgeEls) {
      const [source, target] = key.split('>');
      entry.path.classList.toggle('picked', source === state.openId || target === state.openId);
    }
  }

  /* 正在跑的那张卡：光晕 + 转圈 + 「运行中」。同时只能有一张。 */
  function runningCardId(layout) {
    if (!state.running || !layout.cards.length) return null;
    return layout.cards[layout.cards.length - 1].id;
  }

  function paintRunning(layout) {
    const id = runningCardId(layout);
    for (const [cardId, entry] of state.cardEls) {
      const on = cardId === id;
      entry.g.classList.toggle('running', on);
      if (entry.halo) entry.halo.style.display = on ? '' : 'none';
      if (entry.halo2) entry.halo2.style.display = on ? '' : 'none';
    }
  }

  function drawCards(layout) {
    const layer = state.cardLayer;
    const alive = new Set();
    let added = 0;
    let newest = null;

    for (const placed of layout.placed.values()) {
      const id = placed.card.id;
      alive.add(id);
      let entry = state.cardEls.get(id);
      if (!entry) {
        entry = makeCard(placed, added);
        layer.appendChild(entry.g);
        state.cardEls.set(id, entry);
        added += 1;
        newest = placed;
      }
      // 卡片对象每次重算都是新的，闭包里必须拿最新的那个
      entry.card = placed.card;

      const sig = signature(placed.card, placed.h);
      if (entry.sig !== sig) {
        paintCard(entry, placed);
        entry.sig = sig;
        /* 抽屉里看的正是这张卡，而它刚长出了新内容（结果回来了、又调了一个工具）。
           不重画抽屉的话，它会一直停在「…」上 —— 屏幕上明明已经 ok 了。 */
        if (id === state.openId && state.onOpen) state.onOpen(placed.card);
      }
      if (entry.x !== placed.x || entry.y !== placed.y) {
        entry.x = placed.x;
        entry.y = placed.y;
        entry.g.setAttribute('transform', `translate(${placed.x},${placed.y})`);
      }
    }

    for (const [id, entry] of state.cardEls) {
      if (alive.has(id)) continue;
      entry.g.remove();
      state.cardEls.delete(id);
    }

    paintOpen();
    paintRunning(layout);
    paintScrub(layout);
    /* 重画会把连线重建一遍，悬停的高亮得重新盖上去 */
    if (state.hoverId) setHover(state.hoverId, true);
    if (newest && !state.scrub) follow(newest);
  }

  // ---------------------------------------------------------------- 小地图

  function paintMini(layout) {
    const map = state.mini;
    if (!map) return;
    const s = Math.min(
      (MINI_W - MINI_PAD * 2) / Math.max(1, layout.width),
      (MINI_H - MINI_PAD * 2) / Math.max(1, layout.height),
    );
    const ox = (MINI_W - layout.width * s) / 2;
    const oy = (MINI_H - layout.height * s) / 2;
    map.s = s;
    map.ox = ox;
    map.oy = oy;

    map.inner.textContent = '';
    for (const lane of layout.lanes) {
      map.inner.appendChild(svgEl('rect', {
        class: 'gMiniBand',
        x: (ox + lane.x * s).toFixed(1),
        y: (oy + lane.y * s).toFixed(1),
        width: (lane.w * s).toFixed(1),
        height: (lane.h * s).toFixed(1),
        rx: 2,
      }));
    }
    for (const placed of layout.placed.values()) {
      const meta = NODE_META[placed.card.type] || NODE_META.assist;
      map.inner.appendChild(svgEl('rect', {
        x: (ox + placed.x * s).toFixed(1),
        y: (oy + placed.y * s).toFixed(1),
        width: Math.max(2, placed.w * s).toFixed(1),
        height: Math.max(1.5, placed.h * s).toFixed(1),
        rx: 1.5,
        fill: meta.flat,
        opacity: 0.85,
      }));
    }
    paintMiniView();
  }

  function paintMiniView() {
    const map = state.mini;
    if (!map || !state.layout) return;
    const { w, h } = viewSize();
    const cam = state.cam;
    const vw = w / cam.z;
    const vh = h / cam.z;
    map.view.setAttribute('x', (map.ox + (cam.x - vw / 2) * map.s).toFixed(1));
    map.view.setAttribute('y', (map.oy + (cam.y - vh / 2) * map.s).toFixed(1));
    map.view.setAttribute('width', Math.max(4, vw * map.s).toFixed(1));
    map.view.setAttribute('height', Math.max(4, vh * map.s).toFixed(1));
  }

  function bindMini(mini) {
    const move = (event) => {
      const map = state.mini;
      if (!map || !state.layout) return;
      const rect = mini.getBoundingClientRect();
      const scaleX = MINI_W / Math.max(1, rect.width);
      const scaleY = MINI_H / Math.max(1, rect.height);
      const lx = (event.clientX - rect.left) * scaleX;
      const ly = (event.clientY - rect.top) * scaleY;
      const x = (lx - map.ox) / map.s;
      const y = (ly - map.oy) / map.s;
      // 小地图是「直接跳」，不走补间 —— 你要的是「到那儿」，不是「慢慢漂过去」
      state.cam.x = x;
      state.cam.y = y;
      state.target.x = x;
      state.target.y = y;
      state.userMovedAt = performance.now();
      apply();
    };

    mini.addEventListener('pointerdown', (event) => {
      event.preventDefault();
      event.stopPropagation();
      try {
        mini.setPointerCapture(event.pointerId);
      } catch {
        /* 合成事件没有活跃指针，忽略 */
      }
      move(event);
    });
    mini.addEventListener('pointermove', (event) => {
      if (!mini.hasPointerCapture || !mini.hasPointerCapture(event.pointerId)) return;
      move(event);
    });
    mini.addEventListener('pointerup', (event) => {
      try {
        mini.releasePointerCapture(event.pointerId);
      } catch {
        /* 指针已经没了，忽略 */
      }
    });
  }

  // ---------------------------------------------------------------- 交互

  function toggleCard(card) {
    // 同一张卡再点一次 = 收起。要的就是「同时只能开一个」，而不是层层叠。
    state.openId = state.openId === card.id ? null : card.id;
    paintOpen();
    if (state.onOpen) state.onOpen(state.openId ? card : null);
    if (state.openId) revealCard(card);
  }

  /* 抽屉从右边盖过来，会把最右那一列的卡挡住 —— 开了卡却看不见是哪张，等于没开。
     所以打开时如果卡被挡住，就让镜头往左挪一点，把它让出来。
     算的是 target 不是 cam：取景正在补间的时候，终点才是那张卡最终会待的地方。 */
  function revealCard(card) {
    const layout = state.layout;
    if (!layout) return;
    const placed = layout.placed.get(card.id);
    if (!placed) return;
    const { w, h } = viewSize();
    // 视图还没显示出来时算出来的位移会离谱到把镜头甩飞，宁可不挪
    if (w < 8 || h < 8) return;
    const drawer = document.getElementById('cardDrawer');
    const cover = drawer && !drawer.classList.contains('hidden') ? drawer.offsetWidth : 0;
    if (!cover) return;
    const to = state.target;
    const right = (placed.x + placed.w - to.x) * to.z + w / 2;
    const limit = w - cover - 24;
    if (right <= limit) return;
    to.x += (right - limit) / to.z;
    if (still()) {
      state.cam.x = to.x;
      apply();
      return;
    }
    wake();
  }

  /* 视图从「藏起来」变成「显示出来」时补一次让位。
     打开卡片那一刻如果根本没显示，offsetWidth 是 0，量不到抽屉宽度。 */
  function revealOpen() {
    const layout = state.layout;
    if (!layout || !state.openId) return;
    const placed = layout.placed.get(state.openId);
    if (placed) revealCard(placed.card);
  }

  function closeCard() {
    if (!state.openId) return;
    state.openId = null;
    paintOpen();
    if (state.onOpen) state.onOpen(null);
  }

  /* 一次「点一下」（不是拖）落在谁身上。
     为什么不用 click 事件判：pointerdown 时如果就 setPointerCapture，浏览器会把
     之后的 pointerup **和 click 一起重定向到 svg**，卡片上的 click 永远收不到 ——
     合成事件测不出来（合成事件不会真的捕获指针），但真鼠标上就是「点卡片没反应」。
     所以判据放在 pointerup 上，看 pointerdown 时的 target。 */
  function tapAt(target) {
    const el = target && typeof target.closest === 'function'
      ? target.closest('.gCard')
      : null;
    const entry = el ? state.cardEls.get(el.dataset.id) : null;
    if (entry && entry.card) toggleCard(entry.card);
    else closeCard();                       // 点空白处 = 收起
  }

  function bindDrag(svg) {
    svg.addEventListener('pointerdown', (event) => {
      if (event.button !== 0) return;
      state.drag = {
        id: event.pointerId,
        target: event.target,               // 记住落在谁身上，松手时用它判点击
        startX: event.clientX,
        startY: event.clientY,
        camX: state.cam.x,
        camY: state.cam.y,
        moved: false,
        captured: false,
        t: performance.now(),
      };
      state.velocity = { x: 0, y: 0, t: 0 };
      svg.classList.add('grabbing');
    });

    svg.addEventListener('pointermove', (event) => {
      const drag = state.drag;
      if (!drag || drag.id !== event.pointerId) return;
      const dx = event.clientX - drag.startX;
      const dy = event.clientY - drag.startY;
      if (!drag.moved && Math.hypot(dx, dy) < DRAG_SLOP) return;

      /* 真的拖起来了才抢指针。抢早了会把点击一起吞掉（见上面 tapAt 的注释），
         抢晚了指针移出画布就收不到 move 事件 —— 就在这一步抢。 */
      if (!drag.captured) {
        try {
          svg.setPointerCapture(event.pointerId);
          drag.captured = true;
        } catch {
          /* 合成事件没有活跃指针，忽略 */
        }
      }

      const now = performance.now();
      const dt = Math.max(1, now - drag.t);
      const cam = state.cam;
      // 拖动必须实时跟手：这里直接写 cam，不走补间
      const nx = drag.camX - dx / cam.z;
      const ny = drag.camY - dy / cam.z;
      state.velocity = { x: ((nx - cam.x) / dt) * 16, y: ((ny - cam.y) / dt) * 16, t: now };
      cam.x = nx;
      cam.y = ny;
      state.target.x = nx;
      state.target.y = ny;
      drag.t = now;
      drag.moved = true;
      state.userMovedAt = now;
      svg.classList.add('dragging');
      apply();
    });

    const release = (event, cancelled) => {
      const drag = state.drag;
      if (!drag || drag.id !== event.pointerId) return;
      state.drag = null;
      svg.classList.remove('grabbing', 'dragging');
      if (drag.captured) {
        try {
          svg.releasePointerCapture(event.pointerId);
        } catch {
          /* 指针已经没了，忽略 */
        }
      }
      if (cancelled) return;
      if (!drag.moved) {
        tapAt(drag.target);
        return;
      }
      // 松手留一点惯性：把当前速度当作目标，补间自然会减速停下
      const idle = performance.now() - state.velocity.t;
      if (idle < 90) {
        state.target.x = state.cam.x + state.velocity.x * 6;
        state.target.y = state.cam.y + state.velocity.y * 6;
        wake();
      }
    };

    svg.addEventListener('pointerup', (event) => release(event, false));
    svg.addEventListener('pointercancel', (event) => release(event, true));

    svg.addEventListener('wheel', (event) => {
      event.preventDefault();
      const rect = svg.getBoundingClientRect();
      const factor = Math.exp(-event.deltaY * 0.0016);
      zoomAt(event.clientX - rect.left, event.clientY - rect.top, factor);
      state.userMovedAt = performance.now();
    }, { passive: false });

    /* 双击空白 = 回到整张图。双击卡片 = 跳到会话流里那一条（设计稿 A5-1：
       点节点看详情、双击跳过去，两边共享同一串事件）。 */
    svg.addEventListener('dblclick', (event) => {
      const onCard = event.target && typeof event.target.closest === 'function'
        ? event.target.closest('.gCard')
        : null;
      if (onCard) {
        event.preventDefault();
        const card = state.cards.find((one) => one.id === onCard.dataset.id);
        const first = card && (card.events || [])[0];
        if (first && state.onLocate) state.onLocate(first);
        return;
      }
      event.preventDefault();
      state.userMovedAt = -1e9;
      fit(true);
    });

  }

  // ---------------------------------------------------------------- 时间轴

  /* 底部那条：每个节点一根柱子，高的是「这一步更重」。
     它不承担精确计时（事件流本身没有耗时字段），承担的是「这段对话的节奏」——
     一根柱子密集的地方就是它真正在干活的地方。 */
  function paintTimeline(host, layout) {
    if (!host) return;
    host.textContent = '';

    const play = h('button.tlPlay', { title: '暂停 / 继续流光' }, state.flow ? '❚❚' : '▶');
    play.addEventListener('click', () => {
      setFlow(!state.flow);
      play.textContent = state.flow ? '❚❚' : '▶';
      host.classList.toggle('paused', !state.flow);
    });

    const wrap = h('div.tlWrap');
    const first = layout.cards[0];
    const last = layout.cards[layout.cards.length - 1];
    wrap.appendChild(h('div.tlHead',
      h('span', `${clockOf(first && first.ts) || '开始'} · 第 1 轮`),
      h('span', `第 ${layout.lanes.length} 轮 · ${clockOf(last && last.endTs) || ''}`.trim()),
      h('span', state.running ? '现在' : '已结束')));

    const bars = h('div.tlBars');
    const runningId = runningCardId(layout);
    for (const card of layout.cards) {
      const kind = card.id === runningId ? 'live'
        : card.type === 'sub' ? 'sub'
          : (card.type === 'tool' || card.type === 'out') ? 'hot' : '';
      const weight = card.rows && card.rows.length
        ? Math.min(1, 0.34 + card.rows.length * 0.2)
        : 0.42;
      bars.appendChild(h(kind ? `i.${kind}` : 'i', {
        style: `height:${Math.round(Math.max(18, weight * 100))}%`,
        title: `${headOf(card)} · ${lineOf(card)}`,
      }));
    }
    wrap.appendChild(bars);
    wrap.appendChild(h('div.tlCursor'));

    /* 拖动回放（设计稿 A5-5）。
     *
     * 拖到某一根柱子 = 把画面还原成「那一步之前它看到的图」：之后的节点压暗，
     * 正好停在那一刻的那个高亮。它不是录像 —— 事件流里本来就没有中间态，
     * 这就是「按时间点还原状态」能做到的最准确的样子。
     *
     * 只在按下之后才跟手：鼠标路过时间轴不该改画面。
     */
    let dragging = false;
    const pick = (event) => {
      const rect = bars.getBoundingClientRect();
      if (!rect.width) return;
      const ratio = Math.max(0, Math.min(0.999, (event.clientX - rect.left) / rect.width));
      const index = Math.floor(ratio * layout.cards.length);
      const card = layout.cards[index];
      if (card) setScrub(card.id);
    };
    bars.addEventListener('pointerdown', (event) => {
      dragging = true;
      // 合成事件（自检里那种）没有真指针，捕获会抛 —— 抓不住就不抓，不影响取点
      try { bars.setPointerCapture(event.pointerId); } catch (err) { /* 没有这个指针 */ }
      pick(event);
    });
    bars.addEventListener('pointermove', (event) => {
      if (dragging) pick(event);
    });
    bars.addEventListener('pointerup', (event) => {
      dragging = false;
      try { bars.releasePointerCapture(event.pointerId); } catch (err) { /* 已经松了 */ }
    });

    if (state.scrub) {
      wrap.appendChild(h('button.tlBack', {
        title: '回到最新那一刻',
        onclick: () => setScrub(null),
      }, '↺ 回到现在'));
    }

    host.appendChild(play);
    host.appendChild(wrap);
    host.classList.toggle('paused', !state.flow);
    host.classList.toggle('scrubbing', Boolean(state.scrub));
    const mark = state.scrub
      ? layout.cards.find((card) => card.id === state.scrub)
      : null;
    host.title = mark
      ? `回放到 ${clockOf(mark.endTs || mark.ts) || '这一步'} —— 灰色的是那一刻还没发生的`
      : '';
    const label = host.querySelector('.tlScrub');
    if (label) label.remove();
    if (mark) {
      wrap.appendChild(h('div.tlScrub',
        `回放到 ${clockOf(mark.endTs || mark.ts) || headOf(mark)}`));
    }
  }

  /* 回放位置记的是**节点 id**，不是时间戳。
     时间戳能算出「那一刻」，但一串事件里可能好几条同一秒（甚至没有 ts 的合成数据），
     按时间比大小会让「停在第三步」变成「停在前三步全亮」。按顺序比没有这个问题。*/
  function setScrub(cardId) {
    state.scrub = cardId || null;
    if (state.layout) {
      paintScrub(state.layout);
      paintTimeline(state.timeline, state.layout);
    }
  }

  /* 压暗「那一刻之后」的节点。只在回放时生效，平时一张都不暗。 */
  function paintScrub(layout) {
    const order = new Map();
    layout.cards.forEach((card, index) => order.set(card.id, index));
    const stop = state.scrub == null ? -1 : order.get(state.scrub);
    for (const [id, entry] of state.cardEls) {
      const index = order.has(id) ? order.get(id) : 0;
      const future = stop >= 0 && index > stop;
      entry.g.classList.toggle('dim', future);
    }
  }

  function setFlow(on) {
    state.flow = Boolean(on);
    if (state.host) state.host.classList.toggle('noflow', !state.flow);
  }

  // ---------------------------------------------------------------- 入口

  function render(host, cards, options) {
    const opts = options || {};
    const prevHost = state.host;
    state.host = host;
    if (opts.onOpen !== undefined) state.onOpen = opts.onOpen;
    if (opts.onLocate !== undefined) state.onLocate = opts.onLocate;
    if (opts.running !== undefined) state.running = Boolean(opts.running);
    if (opts.follow !== undefined) state.follow = Boolean(opts.follow);
    if (opts.timelineHost !== undefined) state.timeline = opts.timelineHost;
    /* 原始卡片一律留着：切粒度、开关筛选项都从这份重算，
       而不是在上一次的结果上再收一遍（收两遍就再也回不去「步骤」了）。 */
    state.allCards = cards || [];

    const shaped = state.detail === 'round' ? mergeRounds(state.allCards) : state.allCards;
    const layout = compute(shaped);
    state.layout = layout;
    state.cards = layout.cards;

    if (!layout.cards.length) {
      teardown();
      state.host = host;
      host.textContent = '';
      host.appendChild(h('div.empty',
        '这里会把「怎么一步步做成的」画成一张图。一轮一条泳道，点节点看全文。'));
      paintTimeline(state.timeline, layout);
      return layout;
    }

    // 开着的卡没了（换了会话 / 重跑），抽屉也要收起来
    if (state.openId && !layout.placed.has(state.openId)) {
      state.openId = null;
      if (state.onOpen) state.onOpen(null);
    }

    /* 判断是「同一张图又长了几张卡」还是「换了一张图」：
       只要新数据里有一个 id 是手上有的，就是同一张图，可以增量更新。 */
    const sameGraph = state.svg && prevHost === host
      && layout.cards.some((card) => state.cardEls.has(card.id));
    if (!sameGraph) {
      teardown();
      state.host = host;
      state.layout = layout;
      buildSvg(host);
    }

    drawLanes(layout);
    drawEdges(layout);
    drawCards(layout);
    paintMini(layout);
    paintTimeline(state.timeline, layout);

    if (!state.fitted) {
      if (fit(false)) state.fitted = true;
    } else {
      apply();
    }
    return layout;
  }

  return {
    render,
    buildCards,
    metaOf,
    headOf,
    lineOf,
    statusOf,
    eventLabel: (event) => EVENT_LABEL[event && event.type] || (event && event.type) || '',
    fit() {
      state.userMovedAt = -1e9;
      fit(true);
    },
    setZoom(next) {
      const z = clampZ(next);
      state.target.z = z;
      if (still()) {
        state.cam.z = z;
        apply();
      } else {
        wake();
      }
      return z;
    },
    /* 工具条上的「工具 / 子代理 / 审批」：关掉就真不画，不是把它画淡 */
    setFilter(type, on) {
      state.hidden[type] = !on;
      if (state.host && state.allCards) render(state.host, state.allCards, {});
      return { ...state.hidden };
    },
    filters: () => ({ ...state.hidden }),
    setFlow,
    isFlow: () => state.flow,
    getZoom: () => state.cam.z,
    get openId() { return state.openId; },
    get cards() { return state.cards; },
    close: closeCard,
    toggle: toggleCard,
    setHover: (id) => setHover(id, true),
    revealOpen,
    /* 工具条上的「粒度」：step 一步一个节点 / round 一轮一个节点。
       重画走同一条 render()，原始卡片一直留着，所以来回切不会越收越少。 */
    setDetail(mode) {
      state.detail = mode === 'round' ? 'round' : 'step';
      state.fitted = false;
      if (state.host && state.allCards) render(state.host, state.allCards, {});
      return state.detail;
    },
    get detail() { return state.detail; },
    setScrub,
    get scrub() { return state.scrub; },
    costOf,
    secText,
    width: CARD_W,
    set onZoom(fn) { state.onZoom = fn; },
    /* 给自检用的只读快照。别在界面代码里用 —— 它不是接口，是探针。 */
    debug: () => ({
      cam: { ...state.cam },
      target: { ...state.target },
      pending: state.pending,
      raf: state.raf,
      open: state.openId,
    }),
  };
})();
