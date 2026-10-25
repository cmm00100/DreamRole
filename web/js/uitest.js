/* 前端自检。只在 ?uitest=1 时被 app.js 动态加载，正常使用时一个字节都不执行。
 *
 * 为什么要有这个：`@` 补全、命令面板、图投影这些逻辑很难靠截图验证 ——
 * 截图只能证明「画出来了」，证明不了「按下去对不对」。而它们又都是纯函数，
 * 正好可以在浏览器里直接喂输入、看输出。跑法见 scripts/ui_check.py。
 *
 * 结果写到 document.documentElement.dataset.uitest，由无头浏览器 dump 出来。
 */
(async () => {
  const pass = [];
  const fail = [];

  function check(name, condition, detail) {
    if (condition) pass.push(name);
    else fail.push(name + (detail ? ' :: ' + detail : ''));
  }

  const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  async function waitUntil(match, timeout) {
    const deadline = performance.now() + timeout;
    while (performance.now() < deadline) {
      if (match()) return true;
      await pause(50);
    }
    return match();
  }

  function fakeInput(value, caret) {
    const node = document.createElement('textarea');
    node.value = value;
    node.setSelectionRange(caret === undefined ? value.length : caret, caret === undefined ? value.length : caret);
    return node;
  }

  /* --- @ 补全的判据 --- */
  const withAt = atQuery(fakeInput('看看 @src/ma'));
  check('@ 能被识别', !!withAt && withAt.query === 'src/ma', JSON.stringify(withAt));
  check('@ 的起点指对了', !!withAt && withAt.start === 3, withAt && withAt.start);

  const plain = atQuery(fakeInput('没有引用的普通一句话'));
  check('没有 @ 时不弹', plain === null, JSON.stringify(plain));

  const finished = atQuery(fakeInput('@src/main.py 后面加了空格'));
  check('@ 后面有空格就不再补全', finished === null, JSON.stringify(finished));

  const midLine = atQuery(fakeInput('第一句 @a.md 第二句 @b', 17));
  check('一行里第二个 @ 也能识别', !!midLine && midLine.query === 'b', JSON.stringify(midLine));

  /* --- 卡片折叠：一张卡 = 一次思考 + 它触发的所有动作/结果 --- */
  const events = [
    { id: 'u1', type: 'user', stage: 0, content: '帮我看看这个项目', payload: {} },
    { id: 'm1', type: 'notice', stage: 1, payload: { stage_marker: true }, stage_title: '第一步' },
    { id: 't1', type: 'think', stage: 1, stage_title: '第一步', content: '先看看目录结构。', payload: {} },
    { id: 'a1', type: 'action', stage: 1, parent_id: 't1', tool: 'list_dir',
      payload: { arguments: { path: 'src' } } },
    { id: 'r1', type: 'result', stage: 1, parent_id: 'a1', content: 'a.py\nb.py', payload: { ok: true } },
    { id: 'm2', type: 'notice', stage: 2, payload: { stage_marker: true }, stage_title: '第二步' },
    { id: 'a2', type: 'action', stage: 2, parent_id: 'r1', tool: 'read_file',
      payload: { arguments: { path: 'src/a.py' } } },
  ];
  const cards = GraphView.buildCards(events);
  check('想法和它触发的动作折成一张卡', cards.length === 3,
    cards.map((card) => card.kind + '@' + card.stage).join(','));
  check('一张卡里动作和结果配成了一行',
    cards[1].rows.length === 1 && !!cards[1].rows[0].result && !cards[2].rows[0].result,
    JSON.stringify(cards[1].rows.map((row) => [row.action && row.action.id, row.result && row.result.id])));
  check('阶段标题跟着阶段走',
    cards[1].stageTitle === '第一步' && cards[0].stageTitle === '准备',
    cards.map((card) => card.stageTitle).join(' / '));
  check('阶段标记自己不进卡片',
    cards.every((card) => card.events.every((ev) => !(ev.payload && ev.payload.stage_marker))));
  check('卡上带的是最有信息量的那个参数', cards[1].rows[0].hint === 'src', cards[1].rows[0].hint);

  /* --- 转义 --- */
  check('尖括号被转义', escapeHtml('<b>x</b>') === '&lt;b&gt;x&lt;/b&gt;', escapeHtml('<b>x</b>'));
  check('Markdown 不认原始 HTML', markdown('<img src=x onerror=1>').indexOf('<img') < 0,
    markdown('<img src=x onerror=1>'));

  /* --- 命令面板 --- */
  let paletteOk = true;
  try {
    Palette.open('');
    paletteOk = Palette.isOpen() && document.querySelectorAll('#palList .palItem').length > 0;
    Palette.close();
    paletteOk = paletteOk && !Palette.isOpen();
  } catch (err) {
    paletteOk = false;
    check('命令面板没抛异常', false, err.message);
  }
  check('命令面板能开能关且有条目', paletteOk);

  /* --- 待办 --- */
  state.todos = [
    { text: '读代码', status: 'done' },
    { text: '改一处', status: 'doing' },
  ];
  renderTodos();
  const todoBox = document.querySelector('#todosHost');
  check('待办渲染出来了', !!todoBox && todoBox.querySelectorAll('.todoItem').length === 2,
    todoBox ? todoBox.innerHTML.length : -1);
  check('完成的待办被划掉了', !!todoBox.querySelector('.todoItem.done'));

  /* 收不回去的 bug：这块以前只有「待办清空」才隐藏，
     一轮跑完之后 6/6 就那样钉在输入框上方，点它也没反应。 */
  state.todos = [
    { text: '读代码', status: 'done' },
    { text: '改一处', status: 'done' },
  ];
  renderTodos();
  check('全部做完后自动收成一行', todoBox.classList.contains('folded')
    && todoBox.querySelectorAll('.todoItem').length === 0);
  check('收起来时还看得见进度', /待办 2\/2/.test(todoBox.textContent), todoBox.textContent);

  todoBox.querySelector('.todosHead').click();
  check('点头部能展开回去', !todoBox.classList.contains('folded')
    && todoBox.querySelectorAll('.todoItem').length === 2);
  todoBox.querySelector('.todosHead').click();
  check('再点一下又收起来', todoBox.classList.contains('folded'));

  state.todos = [
    { text: '读代码', status: 'done' },
    { text: '改一处', status: 'doing' },
  ];
  renderTodos();
  /* 手动收起过就是「他要它收着」—— 下一格进度不该把它顶开，
     数字和进度条还在更新就够了。替他做主才是最烦人的那种「贴心」。 */
  check('手动收起后新进度不顶开它', todoBox.classList.contains('folded')
    && /待办 1\/2/.test(todoBox.textContent), todoBox.textContent);

  todoBox.querySelector('.todoX').click();
  check('点 × 能把整块收掉', todoBox.classList.contains('hidden'));
  state.todos = [
    { text: '读代码', status: 'done' },
    { text: '改一处', status: 'done' },
    { text: '再验一次', status: 'doing' },
  ];
  renderTodos();
  check('待办一变它就自己回来', !todoBox.classList.contains('hidden'));

  state.todos = [];
  renderTodos();
  check('清空待办后整块隐藏', todoBox.classList.contains('hidden'));

  /* --- 过程图的镜头（取景 / 缩放 / 拖动）-------------------------------- */

  const host = document.createElement('div');
  host.style.cssText = 'position:fixed;left:0;top:0;width:600px;height:400px;';
  document.body.appendChild(host);

  let opened = null;
  const layout = GraphView.render(host, cards, { onOpen: (card) => { opened = card; } });

  const svg = host.querySelector('svg.gCanvas');
  check('图画出来了', !!svg);
  const viewBoxOf = () => (svg ? svg.getAttribute('viewBox').split(' ').map(Number) : [0, 0, 0, 0]);

  check('每张卡都画出来了', host.querySelectorAll('.gCard').length === cards.length,
    `${host.querySelectorAll('.gCard').length} vs ${cards.length}`);
  check('每一轮画一条泳道', host.querySelectorAll('.gStage').length === layout.lanes.length,
    `${host.querySelectorAll('.gStage').length} vs ${layout.lanes.length}`);
  check('泳道口写着第几轮', !!host.querySelector('.gLaneTag')
    && /第 1 轮/.test(host.querySelector('.gLaneTag').textContent),
    host.querySelector('.gLaneTag') && host.querySelector('.gLaneTag').textContent);
  check('连线条数对得上', host.querySelectorAll('.gEdge').length === cards.length - 1,
    host.querySelectorAll('.gEdge').length);
  check('小地图在，并且框出了视野', !!host.querySelector('.gMiniCard')
    && !!host.querySelector('.gMiniView') && !!host.querySelector('.gMiniTtl'));
  check('图例把六种节点都列全了', host.querySelectorAll('.gLegendItem').length === 6,
    host.querySelectorAll('.gLegendItem').length);
  check('节点上只有标题和副行两行字', !!host.querySelector('.gCard .gHead')
    && !!host.querySelector('.gCard .gLine'), host.querySelector('.gCard .gHead') && host.querySelector('.gCard .gHead').textContent);

  const [vx, vy, vw, vh] = viewBoxOf();
  check('取景的宽高比跟画布一致', Math.abs(vw / vh - 600 / 400) < 0.02, `${vw}×${vh}`);
  check('取景把整张图框住了', vw >= layout.width - 1, `${vw} vs ${layout.width}`);
  /* 图比视野小的时候贴左上角（设计稿里第一条泳道就长在画布左上），
     比视野大的时候才居中。这里图比 600 宽，所以左边距是 18 而不是居中。 */
  check('取景给左上留了边', Math.abs(vx + 18) < 3, vx);

  const z0 = GraphView.getZoom();
  svg.dispatchEvent(new WheelEvent('wheel', { deltaY: -180, clientX: 300, clientY: 200, bubbles: true }));
  await pause(420);
  check('滚轮能放大', GraphView.getZoom() > z0, `${z0} → ${GraphView.getZoom()}`);

  const before = viewBoxOf()[0];
  svg.dispatchEvent(new PointerEvent('pointerdown', { pointerId: 1, button: 0, clientX: 300, clientY: 200 }));
  svg.dispatchEvent(new PointerEvent('pointermove', { pointerId: 1, clientX: 220, clientY: 200 }));
  svg.dispatchEvent(new PointerEvent('pointerup', { pointerId: 1, clientX: 220, clientY: 200 }));
  await pause(320);
  check('能拖着平移', viewBoxOf()[0] > before + 10, `${before} → ${viewBoxOf()[0]}`);

  /* 背景点阵是「拖动有没有真的在动」的参照物：它不跟着走，画面就像浮在静止的纸上 */
  const back = host.querySelector('.gBackdrop');
  const backAt = back ? back.style.transform : '';
  svg.dispatchEvent(new PointerEvent('pointerdown', { pointerId: 2, button: 0, clientX: 300, clientY: 200 }));
  svg.dispatchEvent(new PointerEvent('pointermove', { pointerId: 2, clientX: 312, clientY: 209 }));
  svg.dispatchEvent(new PointerEvent('pointerup', { pointerId: 2, clientX: 312, clientY: 209 }));
  check('背景点阵跟着镜头走', !!back && back.style.transform !== backAt,
    `${backAt} → ${back ? back.style.transform : '没有背景层'}`);

  GraphView.fit();
  /* 取景是动画过去的，等它稳下来再断言。
     无头浏览器里 rAF 会被虚拟时间「省着给」，所以不能睡固定时长就断定 —— 要轮询。 */
  const settled = await waitUntil(
    () => Math.abs(viewBoxOf()[0] + 18) < 6, 2500);
  check('「回到整张图」真的回得去', settled,
    JSON.stringify({ vb: viewBoxOf().slice(0, 3), ...GraphView.debug() }));

  /* 点卡片 = pointerdown + pointerup 落在同一张卡上。
     这里**故意不用** MouseEvent('click')：真浏览器里 pointerdown 一旦抢了指针，
     后面的 click 会被改派到 svg 上，卡片永远收不到 —— 上一版就是这么「测过了、
     但真机上点不开」。所以要测的是指针这条路。 */
  let pointerSeq = 20;
  function tap(el, x, y) {
    pointerSeq += 1;
    const at = {
      pointerId: pointerSeq, button: 0, clientX: x || 300, clientY: y || 200, bubbles: true,
    };
    el.dispatchEvent(new PointerEvent('pointerdown', at));
    el.dispatchEvent(new PointerEvent('pointerup', at));
  }

  const cardEls = () => Array.from(host.querySelectorAll('.gCard'));

  tap(cardEls()[0]);
  check('点一下卡片就打开', cardEls()[0].classList.contains('open'), opened && opened.id);
  check('打开的回调拿到了那张卡', !!opened && opened.id === cards[0].id);

  tap(cardEls()[2]);
  check('同时只能开一张', cardEls().filter((el) => el.classList.contains('open')).length === 1
    && cardEls()[2].classList.contains('open'));

  tap(cardEls()[2]);
  check('再点一次就收起',
    cardEls().every((el) => !el.classList.contains('open')) && opened === null);

  tap(cardEls()[0]);
  tap(svg);
  check('点空白处也收起', cardEls().every((el) => !el.classList.contains('open')),
    cardEls().filter((el) => el.classList.contains('open')).length);

  /* --- 悬停高亮上下游 ------------------------------------------------
     设计稿 A5-2：鼠标停在节点上，它的上下游要亮出来。不亮的话，
     「这个工具是为了什么」只能靠点开一个一个看。 */
  cardEls()[0].dispatchEvent(new PointerEvent('pointerenter'));
  await pause(30);
  /* 淡下去的是「跟它没关系的那些」—— 它自己和直接连着的那一两张不淡，
     所以这里不能要求「除了它全淡」。 */
  check('悬停一张卡，跟它没关系的卡淡下去',
    cardEls().filter((el) => el.classList.contains('faded')).length >= 1
    && !cardEls()[0].classList.contains('faded'),
    cardEls().filter((el) => el.classList.contains('faded')).length);
  check('和它直接相连的那张被标成「附近」',
    cardEls().some((el) => el.classList.contains('near')),
    cardEls().filter((el) => el.classList.contains('near')).length);
  cardEls()[0].dispatchEvent(new PointerEvent('pointerleave'));
  await pause(30);
  check('鼠标移开全部恢复',
    cardEls().every((el) => !el.classList.contains('faded') && !el.classList.contains('near')));

  /* --- 粒度：一步一个节点 / 一轮一个节点 -------------------------------- */
  const stepCount = cardEls().length;
  GraphView.setDetail('round');
  const joined = await waitUntil(() => cardEls().length > 0 && cardEls().length < stepCount, 2000);
  check('切到「轮次」，节点变少', joined, `${stepCount} → ${cardEls().length}`);
  check('收起来的节点写着「第 N 轮」',
    cardEls().every((el) => /第\s*\d+\s*轮/.test(el.textContent)),
    cardEls().map((el) => el.textContent.slice(0, 12)).join('|'));
  GraphView.setDetail('step');
  const backToStep = await waitUntil(() => cardEls().length === stepCount, 2000);
  check('切回「步骤」，一张不少地回来', backToStep, cardEls().length);

  /* --- 回放：拖时间轴 --------------------------------------------------- */
  const tlHost = document.createElement('div');
  tlHost.className = 'graphTimeline';
  tlHost.style.cssText = 'position:fixed;left:0;bottom:0;width:600px;height:56px;';
  document.body.appendChild(tlHost);
  let locatedEvent = null;
  GraphView.render(host, cards, {
    timelineHost: tlHost,
    onOpen: (card) => { opened = card; },
    onLocate: (event) => { locatedEvent = event; },
  });
  const bars = tlHost.querySelector('.tlBars');
  check('时间轴把每一步画成一根柱子', !!bars && bars.children.length === cards.length,
    bars ? bars.children.length : '没有柱子');
  if (bars) {
    const rect = bars.getBoundingClientRect();
    const at = (ratio) => new PointerEvent('pointerdown', {
      pointerId: 91, button: 0, bubbles: true,
      clientX: rect.left + rect.width * ratio, clientY: rect.top + 10,
    });
    bars.dispatchEvent(at(0.35));
    await pause(40);
    check('拖时间轴进入回放（有「回放到 …」）', !!tlHost.querySelector('.tlScrub'),
      tlHost.textContent.slice(0, 40));
    check('回放时后面那几步被压暗',
      cardEls().some((el) => el.classList.contains('dim')),
      cardEls().filter((el) => el.classList.contains('dim')).length);
    check('回放时旁边有个「回到现在」', !!tlHost.querySelector('.tlBack'));
    const comeBack = tlHost.querySelector('.tlBack');
    if (comeBack) {
      comeBack.click();
      await pause(40);
      check('点「回到现在」压暗全部撤销',
        !tlHost.querySelector('.tlScrub')
        && cardEls().every((el) => !el.classList.contains('dim')));
    }
  }

  /* --- 双击节点 = 跳到会话流那一条（A5-1）------------------------------- */
  cardEls()[0].dispatchEvent(new MouseEvent('dblclick', { bubbles: true }));
  check('双击节点带着那一条事件去定位',
    !!locatedEvent && locatedEvent.id === cards[0].events[0].id,
    locatedEvent && locatedEvent.id);

  /* --- 检查器要的两行：耗时 / token ------------------------------------ */
  const cost = GraphView.costOf(cards[0]);
  check('能算出这一步的耗时', typeof cost.sec === 'number' && cost.sec >= 0, cost.sec);
  check('耗时写成中文', /秒|毫秒|—/.test(GraphView.secText(cost.sec)),
    GraphView.secText(cost.sec));
  const withUsage = GraphView.costOf({
    ts: 100, endTs: 103,
    events: [{ payload: { usage: { total: 1234 } } }],
  });
  check('事件里带着 usage 就累加进 token', withUsage.tokens === 1234, withUsage.tokens);
  check('没有 token 的节点算作 0（界面写「未记」，不是编一个数）',
    GraphView.costOf({ ts: 1, endTs: 2, events: [] }).tokens === 0);

  /* 增量更新：再来一个事件，老卡片必须原封不动地留在原地。
     这是「过程图不抽搐」的根 —— 一旦整张重画，所有卡片都会重新淡入一次。 */
  const keep = host.querySelector('.gCard');
  const keepId = keep.dataset.id;
  const grown = GraphView.buildCards(events.concat([
    { id: 'r2', type: 'result', stage: 2, parent_id: 'a2', content: 'print(1)', payload: { ok: true } },
  ]));
  GraphView.render(host, grown, { onOpen: (card) => { opened = card; } });
  const same = host.querySelector(`.gCard[data-id="${keepId}"]`);
  check('再来一个事件不整张重画',
    same === keep && host.querySelectorAll('.gCard').length === grown.length,
    `${same === keep} / ${host.querySelectorAll('.gCard').length} vs ${grown.length}`);
  check('结果回来了，那一行就从「…」变成 ok',
    !!host.querySelector('.gCard[data-id="c-t1"] .gRowState')
    && host.querySelector('.gCard[data-id="c-t1"] .gRowState').textContent !== '…',
    host.querySelector('.gCard[data-id="c-t1"] .gRowState')
      && host.querySelector('.gCard[data-id="c-t1"] .gRowState').textContent);

  /* --- 泳道 / 节点类型 / 工具栏筛选 ---------------------------------- */

  const twoRounds = GraphView.buildCards(events.concat([
    { id: 'u2', type: 'user', stage: 2, content: '再看一眼', payload: {} },
  ]));
  check('第二句话开新一轮', !!twoRounds[3] && twoRounds[3].round === 2,
    twoRounds.map((card) => card.round).join(','));
  check('节点类型分得清用户和工具', twoRounds[0].type === 'user' && twoRounds[1].type === 'tool',
    twoRounds.map((card) => card.type).join(','));
  check('写文件的步骤标成产出',
    GraphView.buildCards([].concat(events, [
      { id: 'a9', type: 'action', stage: 2, tool: 'write_file',
        payload: { arguments: { path: 'x.md' } } },
    ])).pop().type === 'out');

  GraphView.render(host, grown, { onOpen: () => {} });
  const before9 = host.querySelectorAll('.gCard').length;
  const tools9 = grown.filter((card) => card.type === 'tool').length;
  GraphView.setFilter('tool', false);
  const hidden9 = host.querySelectorAll('.gCard').length;
  GraphView.setFilter('tool', true);
  check('筛掉工具之后图上真没有工具节点',
    tools9 > 0 && hidden9 === before9 - tools9 && host.querySelectorAll('.gCard').length === before9,
    `${before9} − ${tools9} → ${hidden9} → ${host.querySelectorAll('.gCard').length}`);

  const tl = document.createElement('div');
  document.body.appendChild(tl);
  GraphView.render(host, grown, { timelineHost: tl });
  check('底部时间轴一个节点一根柱子',
    tl.querySelectorAll('.tlBars i').length === grown.length,
    tl.querySelectorAll('.tlBars i').length);
  check('时间轴上有播放键', !!tl.querySelector('.tlPlay'));
  tl.querySelector('.tlPlay').click();
  check('播放键能暂停流光', GraphView.isFlow() === false && host.classList.contains('noflow'));
  tl.querySelector('.tlPlay').click();
  check('再点一下继续', GraphView.isFlow() === true && !host.classList.contains('noflow'));
  tl.remove();

  host.remove();

  /* --- 工作区：没有它，对话走不通 ------------------------------------- */

  const keptProject = state.project;
  state.project = '';
  renderFeed();
  updateComposer();
  renderProject();

  check('没有工作区时，对话区变成「先选目录」',
    !!document.querySelector('#feed .noWorkspace'),
    document.querySelector('#feed') ? document.querySelector('#feed').innerHTML.slice(0, 60) : '');
  check('没有工作区时发送键是灰的', document.querySelector('#sendBtn').disabled === true);
  check('顶栏直说「未设置工作区」',
    (document.querySelector('#bcPath').textContent || '').indexOf('未设置') >= 0,
    document.querySelector('#bcPath').textContent);
  check('顶栏那个胶囊变成警示样式',
    document.querySelector('#projectBtn').classList.contains('warn'));

  const wsBtn = document.querySelector('#feed .noWorkspace .nwBtn');
  check('那块里有一个「选择工作区」的出口', !!wsBtn && wsBtn.textContent.indexOf('选择') >= 0,
    wsBtn && wsBtn.textContent);
  check('那块把规矩三条列全了',
    (document.querySelectorAll('#feed .noWorkspace .wsRules li').length) === 3,
    document.querySelectorAll('#feed .noWorkspace .wsRules li').length);

  /* 路径缩写：以前把盘符丢了，不同盘上同名目录缩出来一模一样 */
  check('缩写保留盘符',
    pathTail('C:\\work\\demo\\app', 2) === 'C:\\…\\demo\\app',
    pathTail('C:\\work\\demo\\app', 2));
  check('不同盘的同名目录缩出来不一样',
    pathTail('C:\\a\\b\\demo', 1) !== pathTail('D:\\c\\d\\demo', 1),
    pathTail('C:\\a\\b\\demo', 1) + ' vs ' + pathTail('D:\\c\\d\\demo', 1));

  state.project = keptProject;
  renderFeed();
  updateComposer();
  renderProject();
  check('设回工作区之后，对话区恢复',
    !document.querySelector('#feed .noWorkspace')
    && !document.querySelector('#projectBtn').classList.contains('warn'));

  /* 选目录弹窗。
     自己画文件浏览器那条路已经撤了 —— 它只能在当前盘里上下走，
     换盘要一层层退到根目录。现在走系统对话框。 */
  await pickProject();
  const modalText = document.querySelector('#modalCard').textContent;
  check('弹窗里有三条规矩', document.querySelectorAll('#modalCard .wsRules li').length === 3,
    document.querySelectorAll('#modalCard .wsRules li').length);
  check('说清了「现在用的是」哪',
    /现在用的是/.test(modalText) && modalText.indexOf(keptProject) >= 0);
  const wsPrimary = document.querySelector('#modalCard .modalFoot .primary');
  check('主要出口是「浏览这台电脑…」',
    !!wsPrimary && wsPrimary.textContent.indexOf('浏览') === 0,
    wsPrimary && wsPrimary.textContent);
  /* 这个按钮不能点：它会在服务端真的弹出系统对话框，然后一直挂着等人选。
     自检里点了就等于把测试挂死。只验它存在、文案对。 */

  const chip = document.querySelector('#modalCard .dirChip');
  if (chip) {
    const chipPath = chip.title;
    /* 点「最近用过」= 直接设为工作区并关窗。
       临时把 cid 摘掉，这样 applyWorkspace 只改本地 state，不去动演示会话。 */
    const keptCid = state.cid;
    state.cid = null;
    chip.click();
    await pause(300);
    check('点「最近用过」直接定为工作区，弹窗关掉',
      !Modal.isOpen() && state.project === chipPath, state.project);
    state.cid = keptCid;
  }
  Modal.close();

  /* 额外授权目录（通行证）：工作区之外还能去哪。
     这里只验界面和「没建会话时先攒着」那一段 ——
     真加目录要走系统对话框，点一下就会把测试挂住（那一条由
     scripts/pick_folder_check.py 单独验收）。 */
  await pickProject();
  check('弹窗里有一块「还能去的地方」',
    !!document.querySelector('#modalCard .wsGrantsHead'));
  check('空的时候说清了「现在只能在工作区里活动」',
    /只能在工作区/.test(document.querySelector('#modalCard').textContent));
  const addGrantBtn = Array.from(document.querySelectorAll('#modalCard .wsGrantsFoot button'))
    .find((btn) => btn.textContent.indexOf('添加位置') >= 0);
  check('有一个「添加位置…」的出口', !!addGrantBtn,
    document.querySelector('#modalCard .wsGrantsFoot') &&
      document.querySelector('#modalCard .wsGrantsFoot').textContent);
  check('空的时候不出现「全部撤掉」',
    !Array.from(document.querySelectorAll('#modalCard .wsGrantsFoot button'))
      .some((btn) => btn.textContent.indexOf('全部撤掉') >= 0));

  {
    const keptGrants = state.grants;
    const keptCid2 = state.cid;
    /* 摘掉 cid：这样点按钮只改本地 state，不去打接口 ——
       自检不该顺手改掉演示会话的授权。 */
    state.cid = null;
    state.grants = [{ path: 'D:////work////share', mode: 'ro', missing: false }];
    await pickProject();
    check('有一张通行证时就画出来了',
      document.querySelectorAll('#modalCard .grantRow').length === 1,
      document.querySelectorAll('#modalCard .grantRow').length);

    const modeBtn = document.querySelector('#modalCard .grantMode');
    check('默认那档写的是「只读」', !!modeBtn && modeBtn.textContent === '只读',
      modeBtn && modeBtn.textContent);
    if (modeBtn) {
      modeBtn.click();
      await pause(150);
      const after = document.querySelector('#modalCard .grantMode');
      check('点一下就切到「可写」', state.grants[0].mode === 'rw', state.grants[0]);
      check('切完按钮上的字跟着变', !!after && after.textContent === '可写',
        after && after.textContent);
      check('可写那档有自己的样子（不是只靠颜色深浅暗示）',
        !!after && after.classList.contains('rw'));
    }

    const delBtn = document.querySelector('#modalCard .grantDel');
    if (delBtn) {
      delBtn.click();
      await pause(150);
      check('点 × 就撤掉了', state.grants.length === 0, state.grants);
      check('撤完回到空态', !document.querySelector('#modalCard .grantRow'));
    }
    Modal.close();
    state.grants = keptGrants;
    state.cid = keptCid2;
  }

  check('关掉弹窗之后不残留', !document.querySelector('#modalCard .wsRules'));

  /* --- 左侧功能区 ------------------------------------------------------ */

  /* 跳栏统一走这个 helper：它点的就是用户会点的那一行。
     左栏改成一列之后，「更多」里的那几栏要先展开才有点击目标 ——
     直接 `Sidebar.show()` 会绕过入口本身，那这几栏到底有没有入口就没人验了。 */
  const expandMore = () => {
    const more = document.querySelector('#nav .nvMore');
    if (more && !/收起/.test(more.textContent)) more.click();
  };
  const navRows = () => Array.from(document.querySelectorAll('#nav .nv'));
  /* 「画完了」不能只看「有子节点」：插件页先画 tab 条、再画内容，
     看到 tab 条就放行的话，后面的断言会在半成品上跑。 */
  const painted = () => {
    const host = document.querySelector('#panelHost');
    if (!host || host.textContent.includes('读取中') || !host.children.length) return false;
    const box = host.querySelector('.pgBody');
    return box ? box.children.length > 0 : true;
  };
  const goSection = async (id) => {
    // 设置走底部齿轮（它不进导航，一天点不了几次）。其它每一栏 —— 包括会话 ——
    // 都点真的那一行：绕过入口直接调 Sidebar.show() 的话，「这一栏有没有入口」
    // 就永远没人验，而那正是上面那条「用技能之后回不到会话」的漏洞。
    if (id === 'settings') {
      document.querySelector('#gearBtn').click();
      await pause(140);
      return true;
    }
    if (!document.querySelector('#nav .nv[data-sec="' + id + '"]')) expandMore();
    const row = document.querySelector('#nav .nv[data-sec="' + id + '"]');
    if (!row) {
      /* 专家 / 技能 / 工具 / 连接器已经合进「插件」页的三格里，左栏没有它们
         自己的行。深链、命令面板、老代码都还按 id 走，所以这里退回 show()。
         「它们真的没有独立入口」那件事由下面那条断言单独盯 —— 这里放行，
         不等于承认左栏还能多出四行。 */
      Sidebar.show(id);
      await waitUntil(painted, 6000);
      return true;
    }
    row.click();
    await waitUntil(painted, 6000);
    return true;
  };

  /* 栏数不写死。写死过一次，加栏之后这条断言变成了「必须正好 12 个」——
     它挡的不是 bug，是加栏本身。现在按「左栏声明了几栏」来对。
     「搜索」不算在导航里：它的入口是顶上那个搜索框（点开就是命令面板），
     一上一下两个入口，用户得先想「这俩是不是一个东西」。 */
  expandMore();
  const declared = Sidebar.sections().filter((s) => !s.hidden && s.id !== 'search').length;
  check('左栏每个功能都有入口', navRows().length === declared,
    `${navRows().length}/${declared}`);
  check('搜索的入口在顶上那个搜索框里',
    !!document.getElementById('searchBtn'),
    '没有 #searchBtn');
  check('左栏标了名字（不是一排看不懂的图标）',
    navRows().every((button) => (button.querySelector('.nvLbl') || {}).textContent),
    navRows().filter((b) => !(b.querySelector('.nvLbl') || {}).textContent).length);
  check('一级导航带计数（不是空架子）',
    navRows().some((row) => row.querySelector('.nvCnt')), navRows().length);
  /* 「会话」必须是导航第一行：它曾经不在导航里，于是点了别的栏之后
     树被面板顶掉、没有任何入口能点回来。 */
  check('导航第一行是「会话」',
    (navRows()[0] || {}).dataset && navRows()[0].dataset.sec === 'sessions',
    navRows()[0] ? navRows()[0].dataset.sec : '没有第一行');

  const panelHost = document.querySelector('#panelHost');
  const broken = [];
  for (const button of navRows()) {
    const id = button.dataset.sec;
    if (!id || id === 'sessions') continue;   // 会话那一栏画的是树，不是面板
    button.click();
    const painted = await waitUntil(
      () => !panelHost.classList.contains('hidden')
        && panelHost.children.length > 0
        && !panelHost.textContent.includes('读取中'), 4000);
    if (!painted) broken.push(id + '（没画出来）');
    else if (/加载失败|读不出来|连不上/.test(panelHost.textContent)) {
      broken.push(id + '（' + panelHost.textContent.slice(0, 40) + '）');
    }
  }
  check('左栏每一栏都能真的画出来', broken.length === 0, broken.join(' | '));

  /* --- 插件页：三格 ------------------------------------------------------
     专家 / 技能 / 工具 / 连接器原来是左栏四行，现在合成「插件」一页三格：
     插件（工具 + 连接器）/ Skill / 专家。设计稿的说法是它们本来就是同一件事
     的三层，摊成四行会把会话树挤到只剩一条。 */
  const navIds = navRows().map((row) => row.dataset.sec);
  check('左栏不再有专家 / 技能 / 工具 / 连接器这四行',
    !['experts', 'skills', 'tools', 'connectors'].some((id) => navIds.includes(id)),
    navIds.join(','));
  check('左栏有一行「插件」', navIds.includes('plugins'), navIds.join(','));

  await goSection('plugins');
  await waitUntil(() => panelHost.querySelectorAll('.pgTab').length >= 3, 6000);
  const pluginTabIds = Array.from(panelHost.querySelectorAll('.pgTab')).map((b) => b.dataset.tab);
  check('插件页有三格：插件 / Skill / 专家',
    ['plugins', 'skills', 'experts'].every((id) => pluginTabIds.includes(id)),
    pluginTabIds.join(','));
  check('三格都带计数',
    Array.from(panelHost.querySelectorAll('.pgTab .pgCnt')).every((n) => /\d/.test(n.textContent)));
  check('插件页顶部有一行指标', panelHost.querySelectorAll('.pgMetric').length >= 3,
    panelHost.querySelectorAll('.pgMetric').length);
  const toolFold = await waitUntil(() => panelHost.querySelectorAll('.pgGroup').length > 0, 6000);
  check('默认停在「插件」格（工具分组画出来了）',
    toolFold && panelHost.textContent.includes('连接器'),
    panelHost.textContent.slice(0, 80));

  /* 点 tab 就是切到那一格的内容，而且左栏高亮还留在「插件」上 ——
     合成一页之后，里面换格不该把左栏也换走。 */
  const skillTab = Array.from(panelHost.querySelectorAll('.pgTab'))
    .find((b) => b.dataset.tab === 'skills');
  if (skillTab) {
    skillTab.click();
    await waitUntil(() => panelHost.textContent.includes('新建技能'), 6000);
  }
  check('点「Skill」格换成技能面板', panelHost.textContent.includes('新建技能'),
    panelHost.textContent.slice(0, 60));
  check('换格之后左栏高亮还在「插件」上',
    (document.querySelector('#nav .nv[data-sec="plugins"]') || {}).classList
      ? document.querySelector('#nav .nv[data-sec="plugins"]').classList.contains('on') : false);

  const expertTab = Array.from(panelHost.querySelectorAll('.pgTab'))
    .find((b) => b.dataset.tab === 'experts');
  if (expertTab) {
    expertTab.click();
    await waitUntil(() => panelHost.querySelectorAll('.pgCard').length > 0, 6000);
  }
  check('点「专家」格换成专家面板',
    panelHost.querySelectorAll('.pgCard').length > 0,
    panelHost.textContent.slice(0, 200));

  await goSection('plugins');

  /* --- 自动化页：指标 / 筛选 / 卡片 --------------------------------------
     以前这一栏下面是「列表 + 一坨新建表单」：任务一多，表单被越推越远，
     而它永远占着一屏的三分之一。现在列表是列表，新建是弹窗。 */
  await goSection('automation');
  await waitUntil(() => panelHost.querySelectorAll('.auRow, .pgEmpty').length > 0, 6000);
  const autoCards = () => Array.from(panelHost.querySelectorAll('.auRow'));
  check('自动化页画出了任务卡', autoCards().length >= 2, autoCards().length);
  check('任务卡上有开关', !!panelHost.querySelector('.auRow .toggle'));
  check('任务卡左边有频率图标', !!panelHost.querySelector('.auRow .auTrg svg'));
  check('卡片上写了「下次」和「已跑」', /下次/.test(panelHost.textContent)
    && /已跑/.test(panelHost.textContent), panelHost.textContent.slice(0, 70));
  check('顶部有指标（启用中 / 已跑总次数 / 下次触发）',
    panelHost.textContent.includes('启用中') && panelHost.textContent.includes('下次触发'));

  /* --- 三个 tab：自动化 / 运行记录 / 模板 --------------------------------
     设计稿 automation-page-v1 的头下面就是这一排。少一格的话，「这个任务昨晚
     到底跑了没有」在界面上没有任何地方能回答。 */
  const autoTabs = () => Array.from(panelHost.querySelectorAll('.pgTabs .pgTab'));
  check('自动化页有三个 tab', autoTabs().length === 3, autoTabs().length);
  check('三个 tab 是「自动化 / 运行记录 / 模板」',
    ['自动化', '运行记录', '模板'].every((label, index) =>
      autoTabs()[index] && autoTabs()[index].textContent.includes(label)),
    autoTabs().map((n) => n.textContent).join('|'));
  check('默认停在「自动化」', /自动化/.test((autoTabs()[0] || {}).textContent || ''));

  const runsTab = autoTabs().find((one) => /运行记录/.test(one.textContent));
  if (runsTab) runsTab.click();
  const runsShown = await waitUntil(
    () => panelHost.querySelectorAll('.auRun, .pgEmpty').length > 0, 6000);
  check('点「运行记录」换成记录视图',
    runsShown && panelHost.querySelectorAll('.auRow').length === 0,
    panelHost.textContent.slice(0, 60));
  if (panelHost.querySelectorAll('.auRun').length) {
    const firstRun = panelHost.querySelector('.auRun');
    check('每条记录上有时间 / 状态', /\d{2}-\d{2}\s\d{2}:\d{2}/.test(firstRun.textContent)
      && /开了会话|跳过|启动失败/.test(firstRun.textContent), firstRun.textContent);
  }

  const tplTab = autoTabs().find((one) => /模板/.test(one.textContent));
  if (tplTab) tplTab.click();
  const tplShown = await waitUntil(() => panelHost.querySelectorAll('.auTpl').length > 0, 6000);
  check('点「模板」列出模板卡', tplShown, panelHost.querySelectorAll('.auTpl').length);
  const useTpl = panelHost.querySelector('.auTpl .pgBtn');
  check('每张模板卡都有「用这个模板」', !!useTpl && /用这个模板/.test(useTpl.textContent));
  if (useTpl) {
    useTpl.click();
    const opened = await waitUntil(
      () => !document.querySelector('#modal').classList.contains('hidden'), 4000);
    check('点「用这个模板」打开的是新建表单', opened
      && /新建自动化/.test(document.querySelector('#modalCard').textContent),
      document.querySelector('#modalCard').textContent.slice(0, 40));
    const promptBox = document.querySelector('#modalCard textarea, #modalCard .edPrompt');
    check('模板的提示词已经填进去了',
      !!promptBox && promptBox.value.trim().length > 10,
      promptBox ? promptBox.value.slice(0, 30) : '没有输入框');
    Modal.close();
  }

  const listTab = autoTabs().find((one) => /自动化/.test(one.textContent));
  if (listTab) listTab.click();
  await waitUntil(() => panelHost.querySelectorAll('.auRow').length > 0, 6000);

  const autoChips = () => Array.from(panelHost.querySelectorAll('.pgFilters .pgChip'));
  check('筛选条有四档（全部 / 定时 / 间隔 / 已暂停）', autoChips().length === 4, autoChips().length);

  const pausedChip = autoChips().find((one) => /已暂停/.test(one.textContent));
  const beforeFilter = autoCards().length;
  if (pausedChip) pausedChip.click();
  const onlyPaused = await waitUntil(() => {
    const list = Array.from(panelHost.querySelectorAll('.auRow'));
    return list.length > 0 && list.length < beforeFilter
      && list.every((one) => one.classList.contains('off'));
  }, 4000);
  check('点「已暂停」只剩暂停的那几条', onlyPaused,
    Array.from(panelHost.querySelectorAll('.auRow')).length);

  const allChip = autoChips().find((one) => /全部/.test(one.textContent));
  if (allChip) allChip.click();
  const restored = await waitUntil(() => {
    const list = Array.from(panelHost.querySelectorAll('.auRow'));
    return list.length === beforeFilter;
  }, 4000);
  check('点回「全部」，任务都回来了', restored,
    Array.from(panelHost.querySelectorAll('.autoCard')).length);

  /* 新建是弹窗，不是列表下面那坨常驻表单 */
  const newAuto = Array.from(panelHost.querySelectorAll('button'))
    .find((one) => /新建自动化/.test(one.textContent));
  check('自动化页有「新建自动化」', !!newAuto);
  if (newAuto) {
    newAuto.click();
    const opened = await waitUntil(
      () => !document.querySelector('#modal').classList.contains('hidden'), 4000);
    check('点「新建自动化」弹出表单', opened);
    const card = document.querySelector('#modalCard');
    const mode = () => Array.from(card.querySelectorAll('.edSeg.on')).map((n) => n.textContent);
    check('表单分成三段（什么时候跑 / 让谁做什么 / 安全与预算）',
      card.querySelectorAll('.edSec').length === 3
      && /什么时候跑/.test(card.textContent)
      && /让谁做什么/.test(card.textContent)
      && /安全与预算/.test(card.textContent),
      card.textContent.slice(0, 80));
    check('每段前面有段号', card.querySelectorAll('.edSec .edN').length === 3);

    const trig = Array.from(card.querySelectorAll('.edSec')[0].querySelectorAll('.edSeg'));
    check('触发方式有三档（按时间 / 按间隔 / 手动）', trig.length === 3,
      trig.map((n) => n.textContent).join(','));
    check('默认是「按时间」', mode()[0] === '按时间', mode().join(','));

    /* 换触发方式，下面那行输入框要跟着换长相 ——
       「每天 09:00」和「每 60 分钟」不是同一组格子，塞在一起只会让人填错。 */
    trig[1].click();
    await pause(60);
    const intervalRow = card.querySelectorAll('.row3')[0];
    check('切到「按间隔」只剩一个分钟格',
      intervalRow.querySelectorAll('.edSlot:not(.hidden)').length === 1,
      intervalRow.querySelectorAll('.edSlot:not(.hidden)').length);
    trig[2].click();
    await pause(60);
    check('切到「手动」时排班那行整个不显示了',
      card.querySelectorAll('.row3')[0].querySelectorAll('.edSlot:not(.hidden)').length === 0,
      card.querySelectorAll('.row3')[0].querySelectorAll('.edSlot:not(.hidden)').length);
    check('「手动」要说清楚它不排班', /不排班/.test(card.textContent));
    trig[0].click();
    await pause(60);

    const perm = Array.from(card.querySelectorAll('.edSec')[2].querySelectorAll('.edSeg'));
    check('权限有三档（只读 / 工作区内改 / 完全权限）', perm.length === 3,
      perm.map((n) => n.textContent).join(','));
    check('默认停在工作区内改（不是一上来就全权）', mode().includes('工作区内改'), mode().join(','));

    const addSkill = card.querySelector('.sc.add');
    check('能挂技能', !!addSkill);
    if (addSkill) {
      addSkill.click();
      const listed = await waitUntil(() => card.querySelectorAll('.edPickItem').length > 0, 4000);
      check('点开能选到技能', listed, card.querySelectorAll('.edPickItem').length);
      if (listed) {
        card.querySelectorAll('.edPickItem')[0].click();
        await pause(60);
        check('选了的变成一枚绿色 chip', card.querySelectorAll('.sc.sk').length === 1,
          card.querySelectorAll('.sc.sk').length);
        card.querySelector('.sc.sk i').click();
        await pause(60);
        check('点 × 能拿掉', card.querySelectorAll('.sc.sk').length === 0);
      }
    }

    const footBtns = Array.from(card.querySelectorAll('.modalFoot button')).map((n) => n.textContent);
    check('底部有取消 / 试运行一次 / 创建并启用', footBtns.length === 3, footBtns.join(','));
    Modal.close();
  }

  await goSection('plugins');

  /* 记忆那一栏必须看得见三层 —— 少一层，用户就以为「偏好/AI 没记住」 */
  await goSection('memory');
  await waitUntil(() => panelHost.querySelectorAll('.sItem').length >= 3, 4000);
  const memKinds = Array.from(panelHost.querySelectorAll('.sItem .sT')).map((n) => n.textContent);
  check('记忆栏列出项目 / 偏好 / 今日三层',
    memKinds.some((t) => t.includes('项目')) && memKinds.some((t) => t.includes('偏好'))
    && memKinds.some((t) => t.includes('每日')), memKinds.join(','));

  /* 记忆栏得自己说清楚「它正在记」，否则「记忆里怎么多了我没写的东西」会变成新疑问 */
  check('记忆栏写明了自动记忆的档位', /自动记忆：/.test(panelHost.textContent),
    panelHost.textContent.slice(0, 60));

  /* --- 上下文与记忆：设置里真的能选 -------------------------------------
     以前写记忆只有一条入口：模型主动调 memory_write，而那个工具在标准档还要点确认，
     所以「记忆一直是空的」。上下文窗口也是按模型名猜的，猜大了接口直接 400。
     这里验三件事：三个下拉都在、选项齐全、**选完真的存进去了**
     —— 只验「界面上有个下拉」是最没用的那种断言，改了不生效它照样绿。 */
  await goSection('settings');
  await waitUntil(() => panelHost.querySelectorAll('select.select').length >= 3, 6000);

  const selectOf = (label) => {
    const field = Array.from(panelHost.querySelectorAll('.field'))
      .find((node) => ((node.querySelector('label') || {}).textContent || '').includes(label));
    return field ? field.querySelector('select.select') : null;
  };
  const valuesOf = (sel) => (sel ? Array.from(sel.options).map((o) => o.value) : []);

  check('设置里有「上下文与记忆」这一节', panelHost.textContent.includes('上下文与记忆'));

  const windowSel = selectOf('模型上下文窗口');
  check('上下文窗口有 6 档（含「自动」）',
    !!windowSel && valuesOf(windowSel).join(',') === ',32k,64k,128k,200k,1m',
    valuesOf(windowSel).join(','));
  check('窗口默认停在「自动」', !!windowSel && windowSel.value === '',
    windowSel && windowSel.value);

  const ratioSel = selectOf('什么时候开始压缩');
  check('压缩阈值有三档', !!ratioSel && valuesOf(ratioSel).length === 3,
    valuesOf(ratioSel).join(','));
  check('压缩阈值默认 72%', !!ratioSel && Math.abs(Number(ratioSel.value) - 0.72) < 0.001,
    ratioSel && ratioSel.value);

  const memorySel = selectOf('自动记忆');
  check('自动记忆有三档（关 / 每日 / 每日 + 项目）',
    !!memorySel && valuesOf(memorySel).join(',') === 'off,daily,smart',
    valuesOf(memorySel).join(','));
  /* 默认必须是 smart。默认 off 等于「加了功能但默认不生效」，用户的问题原封不动。 */
  check('自动记忆默认开着（smart）', !!memorySel && memorySel.value === 'smart',
    memorySel && memorySel.value);

  if (windowSel && memorySel) {
    windowSel.value = '128k';
    memorySel.value = 'daily';
    const saveBtn = Array.from(panelHost.querySelectorAll('button.primary'))
      .find((node) => node.textContent.trim() === '保存');
    check('设置页有「保存」按钮', !!saveBtn);
    if (saveBtn) saveBtn.click();
    /* 存完 Sidebar 会整块重画，重画后读的是**服务端回来的值**。
       所以这条过了，说明「改的值真的落盘了」，而不只是 select 被改了一下。 */
    const saved = await waitUntil(() => {
      const mem = selectOf('自动记忆');
      const win = selectOf('模型上下文窗口');
      return !!mem && mem.value === 'daily' && !!win && win.value === '128k';
    }, 8000);
    check('改完保存，重画之后还是新值（真存进服务端了）', saved,
      `${selectOf('自动记忆') && selectOf('自动记忆').value} / `
      + `${selectOf('模型上下文窗口') && selectOf('模型上下文窗口').value}`);

    /* 再改回去。这条不只是收拾现场 —— 顺手证明它不是单向的（能选死一个值就再也改不回来）。 */
    const memBack = selectOf('自动记忆');
    const winBack = selectOf('模型上下文窗口');
    if (memBack && winBack) {
      memBack.value = 'smart';
      winBack.value = '';
      const again = Array.from(panelHost.querySelectorAll('button.primary'))
        .find((node) => node.textContent.trim() === '保存');
      if (again) again.click();
    }
    const restored = await waitUntil(() => {
      const mem = selectOf('自动记忆');
      const win = selectOf('模型上下文窗口');
      return !!mem && mem.value === 'smart' && !!win && win.value === '';
    }, 8000);
    check('能再改回「自动 / smart」（不是只能单向）', restored);
  }

  /* 专家栏要有内置的几个 —— 空列表等于这个功能不存在 */
  await goSection('experts');
  await waitUntil(() => panelHost.querySelectorAll('.pgCard').length > 0, 4000);
  const expertCount = panelHost.querySelectorAll('.pgCard').length;
  check('专家栏有内置专家', expertCount >= 5, expertCount);

  /* --- 工具 ------------------------------------------------------------ */

  /* 工具越加越多，一屏铺开就没人找得到东西了。这里验的不是「有几个」，
     而是「还按组折着、新加的组没被漏掉」—— 那才是加工具时最容易坏的地方。 */
  await goSection('tools');
  await waitUntil(() => panelHost.querySelectorAll('.pgGroup').length > 0, 5000);
  const foldNames = Array.from(panelHost.querySelectorAll('.pgGroupH .pgGroupName'))
    .map((node) => node.textContent.trim());
  check('工具栏按组折叠', foldNames.length >= 8, foldNames.length);
  check('每组的标题是人话（不是英文 key）',
    foldNames.every((name) => /[\u4e00-\u9fa5]/.test(name)), foldNames.join(','));
  check('新加的「系统」「时间」两组都在',
    foldNames.includes('系统') && foldNames.includes('时间'), foldNames.join(','));
  const toolRows = panelHost.querySelectorAll('.pgCards .pgCard').length;
  check('工具总数超过 50 个', toolRows >= 50, toolRows);
  check('工具栏里能搜到新工具',
    ['read_many', 'diff_text', 'csv_query', 'now'].every(
      (name) => panelHost.textContent.includes(name)));

  /* --- 工具开关：点一下不能把界面弄死 -------------------------------------
     真实踩过的坑：`onclick: async (event) => { ...await...; event.currentTarget... }`。
     `event.currentTarget` 只在派发期间有值，await 一过就是 null，于是「点一下开关」
     直接抛 TypeError，而全局兜底把整块界面盖成了报错页 —— 用户以为软件坏了。
     所以这里既要验「开关真的翻了」，也要验「没有报错误」。 */
  const firstToggle = panelHost.querySelector('.pgCards .pgCard button.toggle');
  check('工具栏里拿得到开关按钮', !!firstToggle);
  if (firstToggle) {
    /* 按**工具名**定位，不按节点定位。
       这里原来是 `const firstToggle = ...; const nowOn = () => firstToggle.classList...`，
       结果偶发地永远看不见翻转：点一下之后工具栏会整块重画，
       手里那个 `firstToggle` 早就 detached 了，`classList` 定在那儿不会变，
       于是一条「等 3 秒条件成立」的断言也会红，而且红得毫无规律。
       **持有一个会被重画的 DOM 节点，等于持有一份快照 —— 却拿它当实时状态用。**
       现在每次重查，名字取自点击前那一刻（只有名字是稳定的）。 */
    const toolName = (firstToggle.closest('.pgCard').querySelector('.pcName') || {})
      .textContent.trim();
    const toggleOf = () => Array.from(panelHost.querySelectorAll('.pgCards .pgCard'))
      .find((row) => {
        const nm = row.querySelector('.pcName');
        return nm && nm.textContent.trim().startsWith(toolName);
      })?.querySelector('button.toggle') || null;
    const wasOn = firstToggle.classList.contains('on');
    const nowOn = () => {
      const node = toggleOf();
      return !!node && node.classList.contains('on');
    };
    firstToggle.click();
    /* 这里原来是 `await pause(600)`。机器忙的时候接口还没回来，600ms 到点就断言，
       于是同一条代码第一次红、第二次绿 —— 一条会自己变色的断言，比没有断言更糟：
       以后真出问题时，没人分得清「又 flake 了」还是「真坏了」。
       改成等条件成立（上限 3s），超时照样算失败。 */
    const flipped = await waitUntil(() => nowOn() === !wasOn, 3000);
    check('点一下开关，状态真的翻了', flipped, `was=${wasOn} now=${nowOn()}`);
    check('点开关不会蹦出整页报错（await 后读 currentTarget 的坑）',
      !document.getElementById('fatalBox'),
      (document.getElementById('fatalBox') || {}).textContent);
    check('点开关也不该报异步错误',
      !document.getElementById('errBar'),
      (document.getElementById('errBar') || {}).textContent);
    // 翻回去，别把后面的断言搞成「少了一个工具」
    const back1 = toggleOf();
    if (back1) back1.click();
    const back = await waitUntil(() => nowOn() === wasOn, 3000);
    check('关掉再打开，状态能回来', back, nowOn());
  }

  /* --- 运行期错误：一条带子，不是整页红屏 -------------------------------
     兜底分两层：启动阶段没界面，画满屏是对的；界面已经在用了还盖住它，
     等于因为一个按钮出错就说软件坏了。这里制造一次真实的未处理拒绝。 */
  check('界面已就绪（兜底该走运行期分支）', window.__dreamroleReady === true,
    String(window.__dreamroleReady));
  window.dispatchEvent(new PromiseRejectionEvent('unhandledrejection', {
    promise: Promise.reject(new Error('自检制造的运行期错误')),
    reason: new Error('自检制造的运行期错误'),
  }));
  await pause(300);
  const errBar = document.getElementById('errBar');
  check('运行期出错走顶部错误带', !!errBar, document.body.textContent.slice(-80));
  check('运行期出错不再盖住整个界面', !document.getElementById('fatalBox'));
  check('错误带说得出是哪一类错',
    !!errBar && /未处理的异步错误/.test(errBar.textContent), errBar ? errBar.textContent.slice(0, 80) : '');
  const ebDetail = errBar ? errBar.querySelector('.ebDetail') : null;
  check('错误带默认收起（不挡内容）',
    !!ebDetail && getComputedStyle(ebDetail).display === 'none', ebDetail ? getComputedStyle(ebDetail).display : '');
  if (errBar) {
    errBar.querySelector('.ebMore').click();
    await pause(150);
    check('点「详情」能展开技术细节',
      getComputedStyle(errBar.querySelector('.ebDetail')).display !== 'none');
    errBar.querySelector('.ebClose').click();
    await pause(150);
    check('能关掉错误带', !document.getElementById('errBar'));
  }

  /* --- 智能体团队 ------------------------------------------------------
     这一页不是「助理列表 + 群列表」两段，而是三列：频道 / 对话 / 手上的活。
     所以验的是三列都在、都拿到了数据 —— 只有一列时，另外两列是空的也看不出。 */
  await goSection('assistants');
  await waitUntil(() => panelHost.querySelectorAll('.tm-wrap').length > 0, 8000);
  check('团队页是三列（频道 / 对话 / 右列）',
    !!panelHost.querySelector('.tm-chans') && !!panelHost.querySelector('.tm-chat')
    && !!panelHost.querySelector('.tm-side'));
  const chanRows = panelHost.querySelectorAll('.tm-chans .tm-ch').length;
  check('左列列出了群和私聊', chanRows >= 4, chanRows);
  check('左列有成员', panelHost.querySelectorAll('.tm-chans .tm-mem').length >= 1);
  check('中列有输入框', !!panelHost.querySelector('.tm-input'));
  const sideCards = panelHost.querySelectorAll('.tm-side .tm-rc').length;
  check('右列有卡（进行中 / 会话概况）', sideCards >= 1, sideCards);

  const plusNew = panelHost.querySelector('.tm-chans .tm-plus');
  check('左列有「＋」能建群', !!plusNew);
  if (plusNew) {
    plusNew.click();
    const opened = await waitUntil(
      () => !!document.querySelector('#modalCard .pickChip'), 3000);
    check('点 ＋ 弹出建群表单（成员是可点的胶囊）', opened);
    Modal.close();
    await pause(120);
  }

  /* 第二个「＋」在建智能体那一行：新人不能是一张空白表单 */
  const plusBot = panelHost.querySelectorAll('.tm-chans .tm-plus')[1];
  if (plusBot) {
    plusBot.click();
    const formOk = await waitUntil(
      () => document.querySelectorAll('#modalCard .starterChip').length >= 3, 3000);
    check('新建智能体给了起手式（不是一张空白表单）', formOk);
    check('智能体表单有「人设」这一栏',
      document.querySelectorAll('#modalCard .fieldRow').length >= 3,
      document.querySelectorAll('#modalCard .fieldRow').length);
    Modal.close();
    await pause(120);
  }

  /* --- 安全 ------------------------------------------------------------ */

  await goSection('security');
  await waitUntil(() => panelHost.textContent.includes('审计'), 4000);
  const ruleRows = panelHost.querySelectorAll('.listItem').length;
  check('安全栏列出了规则', ruleRows >= 5, ruleRows);
  check('安全栏给了哈希链的校验结论',
    /链是完整的|链断了/.test(panelHost.textContent));
  check('出站打码有开关',
    Array.from(panelHost.querySelectorAll('.switchRow')).some(
      (row) => row.textContent.includes('出站打码')));

  /* --- 运行时 + 通道 --------------------------------------------------- */

  await goSection('runtime');
  await waitUntil(() => panelHost.textContent.includes('通道'), 4000);
  const rtRows = panelHost.querySelectorAll('.rtRow').length;
  check('运行时栏把探到的运行时列出来了', rtRows >= 6, rtRows);
  check('三个通道都在（桌面 / 命令行 / 本地接口）',
    ['桌面窗口', '命令行', '本地接口'].every((name) => panelHost.textContent.includes(name)));
  check('外部接口给了令牌',
    !!panelHost.querySelector('.tokenVal')
    && (panelHost.querySelector('.tokenVal').textContent || '').length >= 24);
  /* 「新会话」现在常驻（在哪一栏都可能想开一条，而且设计稿里它就钉在最上面）；
     跟着栏切换而消失的只有「树自己的那两枚」和树本身。 */
  check('不在会话栏时，会话树和它的两个按钮收起来',
    document.querySelector('#traceList').classList.contains('hidden')
    && document.querySelector('#showArchived').classList.contains('hidden')
    && document.querySelector('#newSessionTop').classList.contains('hidden'));
  check('「新会话」不跟着栏切换消失',
    !document.querySelector('#newSession').classList.contains('hidden'));

  await goSection('sessions');
  await pause(150);
  check('切回会话栏时列表回来了',
    !document.querySelector('#traceList').classList.contains('hidden')
    && panelHost.classList.contains('hidden'));
  check('会话栏才显示「已归档」',
    !document.querySelector('#showArchived').classList.contains('hidden'));

  /* --- 归档的会话真的翻得出来 -------------------------------------------
     接口默认 `archived=0`，前端不显式带参数就永远拿不到归档会话 ——
     「显示已归档」点下去等于没点，而且列表里还是空的，看起来像会话被吞了。
     这几条盯的就是那次漏传：点开要有行、再点要收回去、文案要跟着走。 */
  const archBtn = document.querySelector('#showArchived');
  const archCount = () => document.querySelectorAll('#traceList .trace.arch').length;
  check('默认不显示已归档的会话', archCount() === 0, archCount());
  archBtn.click();
  const archShown = await waitUntil(() => archCount() > 0, 4000);
  check('点「显示已归档」后归档的会话出现在列表里', archShown, archCount());
  check('归档的行带「已归档」标签',
    !!document.querySelector('#traceList .trace.arch .tag'));
  /* 认标题而不是只数个数：数出来 >0 也可能是一条残留的旧节点，
     认到种子会话的标题才说明这批数据真是接口给的。 */
  check('翻出来的是那条归档了的种子会话',
    (document.querySelector('#traceList .trace.arch .tTitle') || {}).textContent === '归档了但还在的会话',
    (document.querySelector('#traceList .trace.arch .tTitle') || {}).textContent);
  check('开关打开后按钮点亮了（它现在是一枚图标，不写字）',
    archBtn.classList.contains('on') && /收起/.test(archBtn.title), archBtn.title);
  archBtn.click();
  const archGone = await waitUntil(() => archCount() === 0, 4000);
  check('再点一下归档的会话又收起来', archGone, archCount());
  check('收起后按钮灭掉，title 也说回来',
    !archBtn.classList.contains('on') && /显示已归档/.test(archBtn.title), archBtn.title);

  /* --- 会话树：一棵树，默认 + 按工作区分组 -------------------------------
     以前是「工作区列表 + 会话列表」两摊，同一条会话在两处各出现一次，而且
     工作区那摊是另存的，点进去还可能是空的。现在只有一棵树：顶上「默认」
     装没选过目录的会话，下面按工作区分组，分组是**从会话自己记的目录算出来的**。

     这几条盯的是树本身的结构：分组在、计数和组里真实条数一致、「默认」在、
     点一下收得起来。计数那条最容易错 —— 它曾经等于「这个工作区里所有会话」，
     而归档开关一开一关，屏幕上的行数会变，数字不变就成了两套说法。 */
  const wsRows = () => Array.from(document.querySelectorAll('#traceList .wsRow'));
  check('会话树里有分组行', wsRows().length >= 3, wsRows().length);

  const labelOf = (row) => ((row.querySelector('.wsNm') || {}).textContent || '');
  const wsLabels = wsRows().map(labelOf);
  check('没选过工作区的会话进了「默认」那一组', wsLabels.includes('默认'), wsLabels.join(' | '));
  check('第一个分组是「默认」（没选目录的会话最常见，不该先翻过一堆项目）',
    wsLabels[0] === '默认', wsLabels[0]);

  /* 每个分组上的数字，必须等于它下面紧跟的会话行条数。 */
  const miscount = wsRows().map((row) => {
    const shown = Number((row.querySelector('.wsCnt') || {}).textContent || 0);
    let real = 0;
    let node = row.nextElementSibling;
    while (node && !node.classList.contains('wsRow')) {
      if (node.classList.contains('trace')) real += 1;
      node = node.nextElementSibling;
    }
    return { label: labelOf(row), shown, real };
  }).filter((one) => one.shown !== one.real);
  check('分组上的数字和组里真实的条数一致', miscount.length === 0, JSON.stringify(miscount));

  /* 收起：点分组行 → 组里的会话行消失；再点 → 回来。
     两次都要重新查节点 —— renderTraceTree 是整棵重画的，旧引用点第二次会落空。 */
  const groupOf = (name) => wsRows().find((row) => labelOf(row) === name);
  const traceCount = () => document.querySelectorAll('#traceList .trace').length;
  const beforeFold = traceCount();
  const defaultRow = groupOf('默认');
  check('「默认」那一组在页面上（下面要拿它验收起）', !!defaultRow);
  if (defaultRow) {
    defaultRow.click();
    const folded = await waitUntil(() => traceCount() < beforeFold, 3000);
    check('点分组行能收起这一组', folded, `${beforeFold} → ${traceCount()}`);
    check('收起之后箭头改成 ▸',
      /▸/.test((groupOf('默认') || {}).textContent || ''), (groupOf('默认') || {}).textContent);
    const again = groupOf('默认');
    if (again) again.click();
    const unfolded = await waitUntil(() => traceCount() === beforeFold, 3000);
    check('再点一下又展开', unfolded, `${traceCount()}/${beforeFold}`);
  }

  /* --- 会话行：一个「⋯」收住全部动作 ------------------------------------
     以前这里只有一个 ✕，置顶/归档/改名想加都加不进去。
     验的是「菜单真的弹出来、四件事都在」。

     这几条**不写成 if 里的断言**：条件不成立时把断言跳过，测试会安静地
     少跑几条，看起来还是全绿 —— 那比红更危险。所以缺东西就让它红。 */
  const firstTrace = document.querySelector('#traceList .trace');
  check('会话行还在', !!firstTrace);
  const moreBtn = firstTrace ? firstTrace.querySelector('.more') : null;
  check('会话行只有一个「更多」，不再是一整排图标',
    !!moreBtn && !(firstTrace && firstTrace.querySelector('.del')),
    firstTrace ? firstTrace.innerHTML.slice(0, 60) : '没有会话行');

  if (moreBtn) {
    moreBtn.click();
    await pause(120);
  }
  const menu = document.querySelector('.traceMenu');
  check('点「⋯」弹出菜单', !!menu);
  const labels = menu ? Array.from(menu.querySelectorAll('.tmItem')).map((n) => n.textContent) : [];
  check('菜单里有置顶 / 重命名 / 归档 / 删除',
    ['置顶', '重命名', '归档', '删除'].every((want) => labels.some((text) => text.includes(want))),
    labels.join(','));

  /* 删除要点开确认框，而且框里必须说清代价 —— 不说代价的删除框等于骗人 */
  const delItem = menu ? Array.from(menu.querySelectorAll('.tmItem'))
    .find((n) => n.textContent.includes('删除')) : null;
  if (delItem) {
    delItem.click();
    await waitUntil(
      () => !document.querySelector('#modal').classList.contains('hidden'), 4000);
  }
  const modalCard = document.querySelector('#modalCard');
  check('删除会先弹影响面确认框',
    !document.querySelector('#modal').classList.contains('hidden') && /删除/.test(modalCard.textContent),
    modalCard.textContent.slice(0, 60));
  check('确认框说清了会作废什么',
    /改动记录|不影响磁盘/.test(modalCard.textContent), modalCard.textContent.slice(0, 90));
  Modal.close();
  await pause(100);
  check('关掉之后菜单也不再挂着', !document.querySelector('.traceMenu'));

  /* --- 删除工作区：三个安全设计一个都不能少（设计稿 tree-actions C 段）---
     这一条是真会删掉整个项目的路，所以验的不是「弹窗长得对不对」，
     而是「有没有拦住手滑」：默认禁用、打错还是禁用、打对才亮。 */
  /* 必须挑**有真实路径**的那一组：「默认」那一组装的是没选过目录的会话，
     它本来就不该有「删除工作区」（没有目录可删）。 */
  const withPath = wsRows().find((row) => row.querySelector('.wsOps')
    && /[:\\/]/.test(row.getAttribute('title') || ''));
  check('工作区行上有悬停动作', !!withPath);
  if (withPath) {
    const ops = withPath.querySelector('.wsOps');
    const moreWs = Array.from(ops.querySelectorAll('.opb')).find((n) => n.textContent === '⋯');
    if (moreWs) {
      moreWs.click();
      await pause(120);
    }
    const wsMenu = document.querySelector('.traceMenu');
    const wsLabels = wsMenu
      ? Array.from(wsMenu.querySelectorAll('.tmItem')).map((n) => n.textContent) : [];
    check('工作区菜单里有「删除工作区…」',
      wsLabels.some((text) => text.includes('删除工作区')), wsLabels.join(','));

    const killItem = wsMenu
      ? Array.from(wsMenu.querySelectorAll('.tmItem')).find((n) => n.textContent.includes('删除工作区'))
      : null;
    if (killItem) {
      killItem.click();
      await waitUntil(() => !document.querySelector('#modal').classList.contains('hidden'), 3000);
    }
    const card = document.querySelector('#modalCard');
    check('删除工作区会先弹确认框', /删除工作区/.test(card.textContent), card.textContent.slice(0, 40));
    check('框里写的是真实绝对路径', !!card.querySelector('.wsDelPath')
      && /[:\\/]/.test((card.querySelector('.wsDelPath') || {}).textContent || ''),
      (card.querySelector('.wsDelPath') || {}).textContent);
    check('框里列出了要一起删掉的对话', !!card.querySelector('.wsDelSessions'));
    check('框里提醒了先导出', /先.*导出|回收站/.test(card.textContent));

    const kill = Array.from(card.querySelectorAll('.modalFoot button'))
      .find((n) => /删除工作区/.test(n.textContent));
    check('删除键默认是禁用的', !!kill && kill.disabled === true);
    const typed = card.querySelector('input.input');
    if (typed) {
      typed.value = '随便打一个不对的';
      typed.dispatchEvent(new Event('input', { bubbles: true }));
      await pause(50);
      check('名字打错还是禁用的', !!kill && kill.disabled === true);
      /* 打的就是弹窗自己要求的那一串（标题里那个名字），
         而不是测试自己猜的路径末段 —— 猜错了红的是测试，不是产品。 */
      const want = ((card.querySelector('h2') || {}).textContent || '')
        .replace(/^删除工作区「/, '').replace(/」？$/, '');
      typed.value = want;
      typed.dispatchEvent(new Event('input', { bubbles: true }));
      await pause(50);
      check('名字打对了才亮起来', !!kill && kill.disabled === false, `${want} / ${typed.value}`);
    }
    Modal.close();
    await pause(80);
  }

  /* --- 改动栏：对话 → 产物 → 阶段 的三级下钻 ---------------------------
     这一栏以前是「改动」和「检查点」两个入口，退回按钮两边各有一份。
     现在只有一条路径：先站到某条对话上，再站到某个文件上，最后站到某个阶段上。
     验的就是这条路真的能走通、走回去，以及「当前阶段」是禁用的。 */

  await goSection('changes');
  await waitUntil(() => panelHost.querySelector('.crumbs')
    && panelHost.querySelectorAll('.drillBody > .listItem').length > 0, 8000);

  check('改动栏能画出来（面包屑 + 至少一行）',
    !!panelHost.querySelector('.crumbs')
      && panelHost.querySelectorAll('.drillBody > .listItem').length > 0,
    panelHost.textContent.slice(0, 80));
  check('第一级是「对话」这一层',
    (panelHost.querySelector('.crumbs') || {}).textContent === '对话',
    (panelHost.querySelector('.crumbs') || {}).textContent);
  check('说明里点明了往下点能看到什么',
    /点开一条对话|挑一个阶段/.test(panelHost.textContent), panelHost.textContent.slice(0, 90));

  /* 进入第二级 */
  const threadRow = panelHost.querySelector('.drillBody > .listItem.clickable');
  check('对话行整行可点，并且带一个「往下」的箭头',
    !!threadRow && !!threadRow.querySelector('.arrow'),
    threadRow ? threadRow.innerHTML.slice(0, 70) : '没有对话行');
  if (threadRow) threadRow.click();
  await waitUntil(() => panelHost.querySelectorAll('.drillBody > .listItem.clickable').length > 0
    && panelHost.querySelectorAll('.crumbs > *').length >= 3, 8000);

  check('点开对话能看到它写出去的文件',
    panelHost.querySelectorAll('.drillBody > .listItem.clickable').length > 0,
    panelHost.textContent.slice(0, 90));
  /* 面包屑的格子 = 可点的层 + 当前层。第二级是「对话 › 标题」三个格子
     （两个之间的那个 › 也算一格），所以数格子比数 .crumb 靠谱 ——
     当前层故意不是按钮，数 .crumb 会少一个。 */
  check('面包屑长了一截（说明可以往回走）',
    panelHost.querySelectorAll('.crumbs > *').length >= 3,
    panelHost.querySelectorAll('.crumbs > *').length);
  check('文件行报得出有几个阶段',
    /个阶段/.test(panelHost.textContent), panelHost.textContent.slice(0, 90));

  /* 进入第三级 */
  const fileRow = panelHost.querySelector('.drillBody > .listItem.clickable');
  if (fileRow) fileRow.click();
  await waitUntil(() => panelHost.querySelectorAll('.stage').length > 0, 8000);

  const stages = panelHost.querySelectorAll('.stage');
  check('点开产物能看到改动时间线', stages.length > 0, stages.length);
  check('时间线里有「最初」那一层', !!panelHost.querySelector('.stage.origin'));
  check('时间线里点得出「当前」在哪一阶段',
    !!panelHost.querySelector('.stage.on'),
    panelHost.textContent.slice(0, 90));
  check('每个阶段都说得出多大', /字节|KB|MB/.test(panelHost.textContent));

  /* 当前阶段不能再退回 —— 灰不是装饰，是判定结果 */
  const onStage = panelHost.querySelector('.stage.on');
  const onRestore = onStage
    ? Array.from(onStage.querySelectorAll('button.mini')).find((b) => /就在这里/.test(b.textContent))
    : null;
  check('当前阶段的按钮写着「就在这里」而且是禁用的',
    !!onRestore && onRestore.disabled,
    onStage ? onStage.textContent.slice(0, 90) : '没有 on 阶段');
  check('「最初」那层看不了 diff（没有「改之前」可比）',
    (() => {
      const origin = panelHost.querySelector('.stage.origin');
      const btn = origin
        ? Array.from(origin.querySelectorAll('button.mini')).find((b) => /diff/.test(b.textContent))
        : null;
      return !!btn && btn.disabled;
    })(), '');

  /* 面包屑回退 */
  const firstCrumb = panelHost.querySelector('.crumb');
  if (firstCrumb) firstCrumb.click();
  // 等「第一级真的画回来了」再断言：光看面包屑会撞上重画的中间态
  // （面包屑是同步画的，列表还在等接口）。
  await waitUntil(() => panelHost.querySelectorAll('.crumbs > *').length === 1
    && panelHost.querySelectorAll('.drillBody > .listItem').length > 0, 8000);
  check('面包屑能一路退回对话层',
    panelHost.querySelectorAll('.crumbs > *').length === 1,
    panelHost.querySelectorAll('.crumbs > *').length);

  /* 删掉的对话：单独一折、标出失效、给一个清理入口 */
  check('对话已删除的改动单独成折',
    panelHost.textContent.includes('对话已删除'), panelHost.textContent.slice(0, 90));
  check('失效那一折里有清理入口',
    /全清掉/.test(panelHost.textContent), '');

  /* --- 右上角：产物 --- */

  check('右上角不再是「工具 / 技能 / 设置」抽屉',
    !document.getElementById('drawerBtn') && !!document.getElementById('artifactsBtn'));

  const artBtn = document.getElementById('artifactsBtn');
  const artPanel = document.getElementById('artPanel');
  artBtn.click();
  const artOpened = await waitUntil(
    () => !artPanel.classList.contains('hidden')
      && artPanel.querySelectorAll('.artItem').length > 0, 5000);
  check('点一下就能看到这个对话的产物', artOpened,
    artPanel.textContent.slice(0, 80));

  const artRows = Array.from(artPanel.querySelectorAll('.artItem'));
  const artText = artRows.map((node) => node.textContent).join(' | ');
  check('列出来的是这一条会话写过的文件', artText.includes('dreamrole-demo.txt'), artText.slice(0, 120));
  check('已经删掉的文件标成「已不在」', artText.includes('已不在'), artText.slice(0, 120));
  /* 只有一条时，下面那条「数字和条数一致」是空转（1 对 1 恒成立）。
     先把「样本够不够」单独验一遍，免得一条永远为真的断言混在里面。 */
  check('产物有不止一条（下面那条断言才有意义）', artRows.length >= 2, `只有 ${artRows.length} 条`);

  /* 数字和列表对不上是最容易让人不信的一类错 —— 这一条就是盯它 */
  const artBadge = document.getElementById('artCount');
  check('按钮上的数字和列表条数一致',
    artBadge.textContent === String(artRows.length),
    `${artBadge.textContent || '(空)'} vs ${artRows.length}`);

  artBtn.click();
  await pause(150);
  check('再点一次就收起', artPanel.classList.contains('hidden'));

  /* --- 中栏头 / 右栏 / 输入区 -----------------------------------------
   *
   * 这三块原来都是「一条工具栏」：什么都塞得下，什么都看不出来。
   * 照设计稿改成三块有主语的区域之后，最容易出的是两种错：
   *   1. 换了结构但没接上数据（标题空着、卡片空着）；
   *   2. 加高了以后把底下的东西挤出窗口（状态条被切掉一截，肉眼几乎看不出）。
   */

  /* 先回到对话页：中栏头、右栏、输入区只在对话页存在。
     面板页占的是中栏，输入区整块收起来 —— 不回这一趟，下面量到的全是 0。 */
  await goSection('sessions');
  await waitUntil(() => document.getElementById('composerHint')
    .getBoundingClientRect().height > 0, 5000);

  const chead = document.querySelector('.chead');
  check('中栏有一条头（面包屑 + 标题），不再是一条细条',
    !!chead && !!chead.querySelector('.bc') && !!chead.querySelector('.ti'),
    chead ? chead.textContent.slice(0, 40) : '没有 .chead');
  const ti = document.getElementById('chatTitle');
  check('标题不是空的', !!ti && ti.textContent.trim().length > 0, ti ? ti.textContent : '');

  const rp = document.getElementById('rpanel');
  const rcCards = Array.from(document.querySelectorAll('#rcards .rc'));
  check('右栏是常驻的一列，不是抽屉', !!rp && !rp.classList.contains('min'));
  check('右栏里有卡片', rcCards.length >= 1, rcCards.length);
  /* 每张卡都得有一个标题 —— 没有标题的卡等于一坨不知道是什么的清单 */
  check('每张卡都说得出自己叫什么',
    rcCards.every((card) => !!card.querySelector('.rcT')),
    rcCards.filter((card) => !card.querySelector('.rcT')).length + ' 张没标题');

  /* 顶上那排图标：只看某一类。点了要真的只剩那一类，点回「全部」要都回来。 */
  const planTab = document.querySelector('.rtab[data-card="plan"]');
  const planCards = document.querySelectorAll('#rcards .rc[data-card="plan"]').length;
  if (planTab && planCards) {
    planTab.click();
    const only = rcCards.filter((card) => !card.classList.contains('hidden'));
    check('点「计划」那一格，只剩计划那张卡',
      only.length > 0 && only.every((card) => card.dataset.card === 'plan'), only.length);
    document.querySelector('.rtab[data-card="all"]').click();
    check('点回「全部」，卡片都回来',
      rcCards.every((card) => !card.classList.contains('hidden')), rcCards.length);
  }

  /* 收起 / 展开：两个方向都得走通。只能收不能开，是最常见的坑。 */
  document.getElementById('rpanelFold').click();
  check('点收起，右栏让出位置', rp.classList.contains('min'));
  document.getElementById('rpanelBtn').click();
  check('中栏头那枚方按钮能把它再打开', !rp.classList.contains('min'));

  /* 输入区：一块盒子套两行（说话 + 动作 / 三个胶囊），账在盒子外面。 */
  const inputBox = document.querySelector('.composerBox');
  check('输入区是一整块（不是一行工具栏）',
    !!inputBox && !!inputBox.querySelector('#input') && !!inputBox.querySelector('.cbar'));
  check('内核 / 思考档 / 权限三枚胶囊在输入框里，不在顶栏',
    !!document.getElementById('engineBtn')
      && !!document.getElementById('thinkBtn')
      && !!document.getElementById('permBtn')
      && inputBox.contains(document.getElementById('engineBtn'))
      && inputBox.contains(document.getElementById('permBtn')),
    '胶囊还在顶栏');

  /* --- 指令条上的 @ chip（设计稿 12 号文档 A3 已选态）--------------------
     打出来的 @ 角色 / @ 文件 要在上面那行变成彩色 chip，点 × 就从句子里摘掉。
     两处必须是同一份真相：芯片点掉之后，输入框里那个 @ 也得没了。 */
  const typedInto = document.getElementById('input');
  typedInto.value = '看看 @src/main.py，按 @standard 的说法过一遍';
  typedInto.dispatchEvent(new Event('input', { bubbles: true }));
  const refChips = () => Array.from(document.querySelectorAll('#refChips .refChip'));
  check('打了 @ 就冒出 chip', refChips().length === 2, refChips().length);
  check('文件那种是「文件」色，角色那种是角色色',
    refChips().some((node) => node.classList.contains('file'))
    && refChips().some((node) => node.classList.contains('role')),
    refChips().map((node) => node.className).join('|'));
  if (refChips().length) {
    const first = refChips()[0];
    const killed = first.querySelector('button');
    check('每个 chip 上都有 ×', !!killed);
    if (killed) {
      const before = typedInto.value;
      killed.click();
      check('点 × 把那一处 @ 从句子里摘掉',
        !typedInto.value.includes('@src/main.py') && typedInto.value !== before,
        typedInto.value);
      check('摘掉之后 chip 也跟着少一个', refChips().length === 1, refChips().length);
    }
  }
  typedInto.value = '';
  typedInto.dispatchEvent(new Event('input', { bubbles: true }));
  check('句子清空，chip 也收起来', refChips().length === 0
    && document.getElementById('refChips').classList.contains('hidden'));

  /* 底部状态条被窗口切掉一截，是「加高一行」最典型的隐性代价：
     截图里看着像贴着底边，实际已经出界，点都点不到。 */
  const footRect = document.querySelector('.composer').getBoundingClientRect();
  check('输入区和底部状态条没有被窗口切掉',
    footRect.bottom <= window.innerHeight + 1,
    `底边 ${Math.round(footRect.bottom)} / 窗口 ${window.innerHeight}`);
  const barRect = document.getElementById('composerHint').getBoundingClientRect();
  check('底部状态条整条都在窗口里',
    barRect.bottom <= window.innerHeight + 1 && barRect.height > 0,
    `底边 ${Math.round(barRect.bottom)} 高 ${Math.round(barRect.height)}`);

  /* --- 顶栏菜单 / 模式胶囊 / 「＋」菜单 --------------------------------- */

  const bar = document.querySelector('.topbar');
  check('顶栏是一条 34px 的菜单栏',
    !!bar && Math.round(bar.getBoundingClientRect().height) === 34,
    bar ? bar.getBoundingClientRect().height : '没有 .topbar');
  const menuLabels = $$('.menuBtn').map((button) => button.textContent.trim());
  check('三个菜单都在：应用 / 编辑 / 视图',
    menuLabels.join('/') === '应用/编辑/视图', menuLabels.join('/'));

  const viewMenu = document.getElementById('menuView');
  document.querySelector('.menuBtn[data-menu="view"]').click();
  check('点「视图」弹出了菜单', !viewMenu.classList.contains('hidden'));
  document.querySelector('.menuBtn[data-menu="view"]').click();
  check('再点一下收回去', viewMenu.classList.contains('hidden'));

  /* 视图切换摆在中栏头上，不在菜单里了：它是「这一屏看什么」，
     跟标题是同一件事，埋进二级菜单等于让人先找一遍。 */
  const viewSeg = document.getElementById('viewSwitch');
  check('中栏头上有「会话流 / 流程图」两格',
    !!viewSeg && viewSeg.querySelectorAll('button').length === 2,
    viewSeg ? viewSeg.textContent.trim() : '没有');
  check('视图切换不在顶栏菜单里了', !viewMenu.querySelector('#viewSwitch'));
  check('默认选中会话流', viewSeg.querySelector('.vw.on')
    && viewSeg.querySelector('.vw.on').dataset.view === 'chat');
  /* 切到流程图之后，中栏头还在不在 —— 这一条是「回不来」的守门员：
     中栏头原来长在会话流那一屏里，一切都到过程图，头连同那两个切换格一起消失，
     于是就再也切不回对话页了（只能靠刷新）。 */
  viewSeg.querySelector('.vw[data-view="graph"]').click();
  const headStill = document.querySelector('.chead');
  const backBtn = document.querySelector('#viewSwitch .vw[data-view="chat"]');
  check('切到流程图后中栏头还在',
    !!headStill && headStill.getBoundingClientRect().height > 10);
  check('切到流程图后还能切回会话流', !!backBtn && backBtn.getBoundingClientRect().height > 10,
    backBtn ? backBtn.getBoundingClientRect().height : '没有');
  if (backBtn) backBtn.click();
  check('点了「会话流」就真回到对话页',
    !document.getElementById('viewChat').classList.contains('hidden')
    && document.getElementById('viewGraph').classList.contains('hidden'));

  /* 模式：胶囊上写的就是当前那一个，点开是同一批按钮 */
  const pill = document.getElementById('modePill');
  const modes = (state.boot && state.boot.modes) || [];
  check('模式胶囊上写着一个模式名',
    !!pill && modes.some((one) => pill.textContent.includes(one.label)), pill ? pill.textContent : '');
  pill.click();
  const modePop = document.getElementById('modePop');
  check('点模式胶囊弹出模式选择', !modePop.classList.contains('hidden'));
  check('弹出的是真的那几个模式按钮', modePop.querySelectorAll('#modeSwitch button').length === modes.length);
  const wrong = modes.find((one) => one.id !== state.mode);
  if (wrong) {
    modePop.querySelectorAll('#modeSwitch button')[modes.indexOf(wrong)].click();
    await pause(60);
    check('选了另一个模式，胶囊跟着改口',
      pill.textContent.includes(wrong.label), pill.textContent);
    // 切回去，别把后面的断言带跑偏
    pill.click();
    modePop.querySelectorAll('#modeSwitch button')[modes.indexOf(modes.find((one) => one.id === 'craft'))].click();
    await pause(60);
    closeMenus();
  }

  /* 「＋」：技能、文件都收进这里了，输入框上只剩两个圆 */
  document.getElementById('skillBtn').click();
  const plus = document.getElementById('plusPop');
  check('点「＋」弹出菜单', !plus.classList.contains('hidden'));
  const plusText = plus.textContent || '';
  check('＋ 菜单里有「挂 Skill」和「文件」', plusText.includes('挂 Skill') && plusText.includes('文件'), plusText);
  check('＋ 菜单说明了这些都能直接打出来', /@ 文件/.test(plusText), plusText.slice(-40));
  /* 设计稿 12 号文档 C 表一共八项，一项都不能少 ——
     少的那几项，用户会以为「这个功能没做」，而不是「藏到别处了」。 */
  const plusItems = Array.from(plus.querySelectorAll('.mi')).map((n) => n.textContent.trim());
  const wanted = ['文件', '图片 / 截图', '挂 Skill', '选专家', '工作区文件…',
    '引用会话 / 消息', '从提示词库插入…', '存为自动化…'];
  const missing = wanted.filter((name) => !plusItems.some((line) => line.includes(name)));
  check('＋ 菜单八项齐全', missing.length === 0, '缺：' + missing.join('、'));
  check('八项之外还留了一条分隔线', plus.querySelectorAll('.sep').length >= 1,
    plus.querySelectorAll('.sep').length);

  /* 工作区文件… 和 提示词库… 点开得有东西弹出来，不能点了没反应 */
  const wsItem = Array.from(plus.querySelectorAll('.mi'))
    .find((n) => n.textContent.includes('工作区文件'));
  if (wsItem) {
    wsItem.click();
    const opened = await waitUntil(
      () => !document.getElementById('modal').classList.contains('hidden'), 3000);
    check('「工作区文件…」点开真的弹选择器', opened);
    const rowsIn = await waitUntil(
      () => document.querySelectorAll('#modalCard .rfRow').length > 0, 3000);
    check('选择器里列出了工作区的文件', rowsIn,
      document.querySelectorAll('#modalCard .rfRow').length);
    Modal.close();
  }

  document.getElementById('skillBtn').click();
  await pause(40);
  const libItem = Array.from(plus.querySelectorAll('.mi'))
    .find((n) => n.textContent.includes('提示词库'));
  if (libItem) {
    libItem.click();
    const opened = await waitUntil(
      () => !document.getElementById('modal').classList.contains('hidden'), 3000);
    check('「从提示词库插入…」点开真的弹库', opened);
    Modal.close();
  }

  document.getElementById('skillBtn').click();
  await pause(40);
  document.body.click();
  await pause(40);
  check('点别处收起 ＋ 菜单', plus.classList.contains('hidden'));

  /* 占位层：空着的时候可见，一打字就收 */
  const ph = document.getElementById('phHint');
  const field = document.getElementById('input');
  field.value = '';
  updateComposer();
  check('输入框空着时占位层是显示的', !ph.classList.contains('hidden'));
  check('占位层把 @ / & 染成黄字（用 b 包起来）', ph.querySelectorAll('b').length >= 3,
    ph.querySelectorAll('b').length);
  check('没有输入时「提示词优化」是暗的', !document.getElementById('optBtn').classList.contains('live'));
  field.value = '测试一句';
  dispatchEvent(new Event('input'));
  updateComposer();
  check('打了字之后占位层收起来', ph.classList.contains('hidden'));
  check('有输入时「提示词优化」点亮了', document.getElementById('optBtn').classList.contains('live'));

  /* 优化浮层：原文和改写**并排**。只给「优化后」一栏的话，用户没有参照，
     判断不了它改了什么 —— 而「用这个」是不可撤销地覆盖输入框。 */
  field.value = '把这个项目里所有的 TODO 找出来，按文件分组';
  dispatchEvent(new Event('input'));
  document.getElementById('optBtn').click();
  const cmpOk = await waitUntil(
    () => document.querySelectorAll('#optPanel .optPane').length === 2, 5000);
  check('「提示词优化」是左右对照（原文 / 优化后）', cmpOk,
    document.querySelectorAll('#optPanel .optPane').length);
  check('改法是一排可点的胶囊（不是下拉框）',
    document.querySelectorAll('#optSegs .optSeg').length >= 1
      && !document.querySelector('#optPanel select'),
    document.querySelectorAll('#optSegs .optSeg').length);
  closeOptimize();
  field.value = '';
  dispatchEvent(new Event('input'));
  updateComposer();

  /* --- 群聊：每句话都得标是谁说的 ------------------------------------- */

  try {
    const madeA = await API.createAssistant({ name: '甲', emoji: '🅰' });
    const madeB = await API.createAssistant({ name: '乙', emoji: '🅱' });
    if (madeA.ok && madeB.ok) {
      const madeGroup = await API.createGroup({
        name: '自检群',
        members: [madeA.data.assistant.id, madeB.data.assistant.id],
        rule: 'round_robin',
      });
      check('能建出一个群', madeGroup.ok, madeGroup.error);

      if (madeGroup.ok) {
        await AppFacade.openWith({ group: madeGroup.data.group.id });
        await waitUntil(() => state.cid && state.members.length === 2, 5000);
        check('进群聊后知道自己在群里', state.members.length === 2, state.members.length);
        check('顶栏挂了群名牌', !!document.querySelector('#speakerChip .speakerChip.group'),
          document.getElementById('speakerChip').className);

        await API.send(state.cid, '各位说说该先做什么');

        /* 自检页面在 still 模式下不接 SSE（截图要可复现），
           所以这里轮询服务端，再拿真实事件重画一次。
           这样验的是「服务端真产出带署名的回答 + 前端真能把署名画出来」两件事。 */
        let snapshot = null;
        for (let i = 0; i < 80 && !snapshot; i += 1) {
          await pause(400);
          const got = await API.getSession(state.cid);
          const rows = (got.ok && got.data.events) || [];
          const spoken = rows.filter(
            (e) => e.type === 'answer' && e.payload && e.payload.member);
          if (rows.length && !got.data.running && spoken.length >= 2) snapshot = rows;
        }
        check('群里两位成员都发了言', !!snapshot,
          snapshot ? '' : '等了两轮也没等到带署名的回答');

        if (snapshot) {
          state.events = snapshot;
          renderFeed();
          const chips = Array.from(document.querySelectorAll('#feed .row .speaker .sn'))
            .map((node) => node.textContent);
          check('群里的回答都标了是谁说的', chips.length >= 2, chips.join(','));
          check('标的是群成员的名字',
            chips.every((name) => name === '甲' || name === '乙'), chips.join(','));
        }

        /* 成员能移出去 —— 用户原话是「智能团成员没有删除功能」。
           这里只验入口和成员表长什么样，**真的不点那个「移出群」**：
           点了这只群就少一个人，后面几条断言全得跟着改。真删人由
           scripts/test_http.py 在接口层验（它用的是自己造的群）。 */
        await goSection('assistants');
        await waitUntil(() => panelHost.querySelectorAll('.tm-chans .tm-ch').length > 0, 8000);
        const selfGroup = Array.from(panelHost.querySelectorAll('.tm-chans .tm-ch'))
          .find((node) => node.textContent.includes('自检群'));
        if (selfGroup) {
          selfGroup.click();
          await pause(200);
        }
        await waitUntil(() => panelHost.querySelectorAll('.tm-chans .tm-mem').length === 2, 6000);
        const memRows = Array.from(panelHost.querySelectorAll('.tm-chans .tm-mem'));
        const memXs = memRows.map((node) => node.querySelector('.tm-memx')).filter(Boolean);
        check('群成员每一行后面都有一枚「移出」',
          memRows.length > 0 && memXs.length === memRows.length,
          memRows.length + ' 行 / ' + memXs.length + ' 枚');
        check('右栏写着这个频道在哪个目录干活（点一下能打开）',
          !!panelHost.querySelector('.tm-side .tm-path'),
          panelHost.querySelector('.tm-side')
            && panelHost.querySelector('.tm-side').textContent.slice(0, 40));

        const memberBtn = Array.from(panelHost.querySelectorAll('.tm-chead .tm-cb'))
          .find((node) => node.textContent === '成员');
        check('频道头上有个「成员」按钮', !!memberBtn);
        if (memberBtn) {
          memberBtn.click();
          await pause(250);
          const mmRows = Array.from(document.querySelectorAll('#modalCard .mmRow'));
          check('点开是成员表，一行一个人', mmRows.length >= 2, mmRows.length);
          const inRows = mmRows.filter((row) => row.textContent.includes('移出群'));
          check('在群里的人，每行都有「移出群」', inRows.length === 2,
            inRows.length + ' 行可移出 / 共 ' + mmRows.length + ' 行');
          check('还没进来的人给的是「拉进来」（不是同一个按钮）',
            mmRows.every((row) => row.textContent.includes('移出群')
              || row.textContent.includes('拉进来')),
            mmRows.map((row) => row.textContent.slice(0, 10)).join('|'));
          check('成员表里说清了「至少要两个人」',
            /至少要两个人/.test(document.querySelector('#modalCard').textContent));
          check('每个人旁边还有「删角色」（移出群 ≠ 删角色，两个按钮）',
            mmRows.every((row) => row.textContent.includes('删角色')),
            mmRows.map((row) => row.textContent.slice(-6)).join('|'));
          Modal.close();
          await pause(120);
        }

        /* 私聊那一栏：改人设 + 删掉 TA，还有右栏的「屋子」路径 */
        const dmRow = Array.from(panelHost.querySelectorAll('.tm-chans .tm-ch'))
          .find((node) => node.textContent.trim().indexOf('@') === 0);
        if (dmRow) {
          dmRow.click();
          await pause(250);
          const dmBtns = Array.from(panelHost.querySelectorAll('.tm-chead .tm-cb'))
            .map((node) => node.textContent.trim());
          check('私聊头上能改人设，也能删掉这个角色',
            dmBtns.includes('改人设') && dmBtns.includes('删掉 TA'), dmBtns.join('|'));
        }

        // 收拾干净，别把自检数据留在这台机器上
        await API.deleteGroup(madeGroup.data.group.id);
      }
      await API.deleteAssistant(madeA.data.assistant.id);
      await API.deleteAssistant(madeB.data.assistant.id);
    }
  } catch (err) {
    // 这一段挂了不该把前面几十条结论一起抹掉 —— 自检脚本自己崩掉，
    // 最坏的结果是「一条都看不到」，那比红一条更难查。
    check('群聊自检跑得下去', false, err && err.message);
  }

  /* --- 群聊表单：走几轮 + 每人分工 -------------------------------------
   * 后端把「别人的话翻成 user」「没有补充就 [skip]」都改好了，但如果表单里
   * 定不了轮次、也定不了分工，下一轮还是三份雷同的话 —— 用户看到的现象不变。
   * 这一节验的是：这两件事在界面上真的能设，而且设完真的进库。 */
  try {
    const pack = [];
    for (const who of ['甲', '乙', '丙']) {
      const got = await API.createAssistant({ name: who, emoji: '🙂' });
      if (got.ok) pack.push(got.data.assistant.id);
    }

    if (pack.length >= 3) {
      await goSection('assistants');
      await waitUntil(() => panelHost.querySelectorAll('.tm-chans .tm-plus').length >= 1, 8000);
      const plus = panelHost.querySelector('.tm-chans .tm-sec .tm-plus');
      check('团队页的「＋」还在（建群的入口）', !!plus);

      plus.click();
      await pause(350);

      const card = document.querySelector('#modalCard');
      const fields = Array.from((card || document).querySelectorAll('.fieldRow'));
      const labelOf = (row) => {
        const node = row.querySelector('.fieldLabel');
        return node ? node.textContent.trim() : '';
      };
      const roundsRow = fields.find((row) => labelOf(row) === '走几轮');
      const rolesRow = fields.find((row) => labelOf(row) === '分工');
      check('建群表单里有「走几轮」这一栏', !!roundsRow);
      check('建群表单里有「分工」这一栏', !!rolesRow);

      const roundSel = roundsRow && roundsRow.querySelector('select');
      const opts = roundSel ? Array.from(roundSel.options) : [];
      check('轮次给的是几档可选，不是一个数字', opts.length >= 2, opts.length);
      check('每一档都写着人话，不是光一个「2」',
        opts.length > 0 && opts.every((one) => /轮\s*·\s*\S/.test(one.textContent)),
        opts.map((one) => one.textContent).join(' | '));
      /* 默认必须是 2。1 轮虽然也把别人的话翻成了 user（技术上看得见），
         但每人只有一次开口机会，说出来的还是「我对这件事的判断」——
         那正是用户投诉的形状，不能继续当默认。 */
      check('默认 2 轮 —— 建群就是「说完互相接一次」',
        !!roundSel && roundSel.value === '2', roundSel && roundSel.value);

      /* 上限不写死在前端：后端 MAX_ROUNDS 改了，这里跟着变。
         真跑 99 轮会把一次对话耗成天价，所以选项数必须等于上限。 */
      const listed = await API.groups();
      const cap = (listed.ok && listed.data.max_rounds) || 0;
      check('下拉的档数等于后端给的轮次上限（没写死）', cap > 0 && cap === opts.length,
        `${cap} vs ${opts.length}`);

      const roleRows = () => (rolesRow ? Array.from(rolesRow.querySelectorAll('.roleRow')) : []);
      check('选了两个人，就摆两行分工', roleRows().length === 2, roleRows().length);
      check('每一行分工都写着这是谁',
        roleRows().length > 0 && roleRows().every((row) => !!row.querySelector('.roleWho')),
        roleRows().map((row) => row.textContent.slice(0, 8)).join('|'));

      // 勾上第三个成员 → 分工跟着多一行（paintRoles 真的被调到了）
      const boxes = Array.from(card.querySelectorAll('.pickChip input[type=checkbox]'));
      const spare = boxes.find((one) => !one.checked);
      if (spare) {
        spare.checked = true;
        spare.dispatchEvent(new Event('change', { bubbles: true }));
        await pause(150);
      }
      check('勾进一个人，分工立刻多一行', roleRows().length === 3, roleRows().length);

      // 全勾掉 → 不摆空行，只说一句
      boxes.forEach((one) => {
        one.checked = false;
        one.dispatchEvent(new Event('change', { bubbles: true }));
      });
      await pause(150);
      check('一个人都没选时不摆一排空框，只说一句提示',
        roleRows().length === 0 && !!rolesRow.querySelector('.roleEmpty'),
        roleRows().length);

      // 回到两个人，各写一句分工，再挑 2 轮
      boxes[0].checked = true;
      boxes[1].checked = true;
      boxes.forEach((one) => one.dispatchEvent(new Event('change', { bubbles: true })));
      await pause(150);

      const words = ['负责挑漏洞', '负责收口'];
      roleRows().forEach((row, i) => {
        const input = row.querySelector('input');
        input.value = words[i] || '';
        input.dispatchEvent(new Event('input', { bubbles: true }));
      });
      roundSel.value = '2';
      roundSel.dispatchEvent(new Event('change', { bubbles: true }));

      const nameInput = fields.find((row) => labelOf(row) === '群名').querySelector('input');
      nameInput.value = '自检带轮次';
      const build = Array.from(card.querySelectorAll('button'))
        .find((node) => node.textContent.trim() === '建群');
      check('表单底部有「建群」', !!build);
      if (build) build.click();
      await pause(700);

      const after = await API.groups();
      const made = ((after.ok && after.data.items) || []).find((one) => one.name === '自检带轮次');
      check('表单填的东西真建成了群', !!made, after.error);
      check('轮次存进去了', !!made && Number(made.rounds) === 2, made && made.rounds);
      check('分工按人存进去了', !!made && Object.keys(made.roles || {}).length === 2,
        made && JSON.stringify(made.roles));
      check('存的分工就是填的那两句',
        !!made && Object.values(made.roles || {}).join('|') === words.join('|'),
        made && Object.values(made.roles || {}).join('|'));

      if (made) await API.deleteGroup(made.id);
    }
    for (const id of pack) await API.deleteAssistant(id);
  } catch (err) {
    check('群表单自检跑得下去', false, err && err.message);
  }

  /* --- 团队页发消息：人留在这间屋子里 -----------------------------------
   * 用户原话：「在群聊里发消息，页面就会自动跳出到会话页面，很突兀」。
   * 这里验两件事：发完不跳；想追过程的时候才跳（「看过程 →」）。
   * 状态条的三态直接往团队页那条回调里递 —— still 模式下没有事件流，
   * 真跑一轮看不到，但状态条本身对不对是可以验的。 */
  try {
    const pairs = [];
    for (const who of ['丁', '戊']) {
      const got = await API.createAssistant({ name: who, emoji: '🙂' });
      if (got.ok) pairs.push(got.data.assistant.id);
    }
    const madeGroup = pairs.length === 2
      ? await API.createGroup({ name: '落脚点', members: pairs, rule: 'round_robin', rounds: 2 })
      : { ok: false };

    if (madeGroup.ok) {
      const gid = madeGroup.data.group.id;
      await goSection('assistants');
      await waitUntil(() => panelHost.querySelectorAll('.tm-chans .tm-ch').length > 0, 8000);
      const chan = Array.from(panelHost.querySelectorAll('.tm-chans .tm-ch'))
        .find((node) => node.textContent.includes('落脚点'));
      if (chan) {
        chan.click();
        await pause(400);
      }

      const roomInput = document.querySelector('.tm-input');
      check('团队页有个能打字的框', !!roomInput);
      roomInput.value = '在群里说一句，看它走不走';
      document.querySelector('.tm-send').click();
      await waitUntil(() => !!state.cid && state.group === gid, 10000);

      check('这句话真发出去了（会话建起来了）', !!state.cid && state.group === gid, state.group);
      check('发完还留在团队页 —— 这就是「不突兀」',
        Panels.current() === 'assistants', Panels.current());
      check('团队页也没被藏起来',
        !panelHost.classList.contains('hidden'), panelHost.className);

      const bar = document.querySelector('.tm-run');
      check('房间里有一条状态条（在跑 / 等你说可以都写在这儿）', !!bar);

      /* 三态：在跑 / 要点头 / 闲着。
         走真正的 handleStream 而不是直接喂团队页 —— 这样验的还有一件事：
         同一条流上的消息到底有没有被递到团队页去。 */
      const keep = { running: state.running, beat: state.beat, pending: state.pending };
      handleStream({ kind: 'state', running: true });
      handleStream({ kind: 'toolbeat', tool: 'read_file', label: '戊', seconds: 7 });
      check('跑起来时它写着「正在跑」和秒数',
        /正在跑/.test(bar.textContent) && /7s/.test(bar.textContent), bar.textContent);
      check('想追过程时有「看过程 →」这颗出口',
        Array.from(bar.querySelectorAll('.tm-runb')).some((node) => node.textContent.includes('看过程')),
        bar.textContent);
      check('也给了「停下」（不用回会话页才能按住它）',
        Array.from(bar.querySelectorAll('.tm-runb')).some((node) => node.textContent === '停下'),
        bar.textContent);

      handleStream({ kind: 'pending', request: { id: 'uitest-ask', tool: 'write_file', arguments: {} } });
      check('有一步要点头时，状态条改成「等你说可以」',
        /等你说可以/.test(bar.textContent), bar.textContent);
      check('这会儿的出口是「去点头 →」（不是「看过程」）',
        Array.from(bar.querySelectorAll('.tm-runb')).some((node) => node.textContent.includes('去点头')));

      handleStream({ kind: 'pending_gone', id: 'uitest-ask' });
      handleStream({ kind: 'state', running: false });
      check('闲下来时状态条收起来，不白占一行', bar.classList.contains('off'), bar.className);

      /* 新的一句话长出来 —— 「留在团队页」的前提是它在别处也看得见。 */
      const before = document.querySelectorAll('.tm-msgs .tm-m2').length;
      handleStream({
        kind: 'event',
        event: {
          id: 'uitest-team-say', type: 'answer', ts: Math.floor(Date.now() / 1000),
          content: '这条只在这间屋子里出现', payload: { member: { id: pairs[0], name: '丁', emoji: '🙂' } },
        },
      });
      check('跑起来之后新的一句就长在房间里（不用切页面）',
        document.querySelectorAll('.tm-msgs .tm-m2').length > before,
        before + ' → ' + document.querySelectorAll('.tm-msgs .tm-m2').length);
      check('长出来的那句带着说话人',
        Array.from(document.querySelectorAll('.tm-msgs .tm-m2')).some((node) =>
          node.textContent.includes('这条只在这间屋子里出现') && node.textContent.includes('丁')));

      state.running = keep.running;
      state.beat = keep.beat;
      state.pending = keep.pending;

      /* 出口：点了才走。
         判据是那一栏真的被让出来了（团队页收起、会话页亮出来），
         不是 Panels 自己的 current —— 那是「上次画过哪一页」的记账。 */
      await AppFacade.gotoSession(state.cid);
      await pause(300);
      check('「看过程」这类出口，点了才切到会话页',
        Sidebar.active() === 'sessions' && panelHost.classList.contains('hidden'),
        Sidebar.active() + ' / ' + panelHost.className);

      await API.deleteGroup(gid);
    }
    for (const id of pairs) await API.deleteAssistant(id);
  } catch (err) {
    check('团队页发消息自检跑得下去', false, err && err.message);
  }

  /* --- 老群还停在「各说一句就散会」时，得当场说一声 ---------------------
   * 只改后端不管用：用户不知道要去「改规则」里找那个下拉，打开一看还是
   * 三个人各说一段，结论就是「根本没改」。所以这条提示必须出现在现象旁边。 */
  try {
    const ids = [];
    for (const who of ['己', '庚']) {
      const got = await API.createAssistant({ name: who, emoji: '🙂' });
      if (got.ok) ids.push(got.data.assistant.id);
    }
    const thin = ids.length === 2
      ? await API.createGroup({ name: '一轮群', members: ids, rule: 'round_robin', rounds: 1 })
      : { ok: false };

    if (thin.ok) {
      const gid = thin.data.group.id;
      await goSection('assistants');
      await waitUntil(() => panelHost.querySelectorAll('.tm-chans .tm-ch').length > 0, 8000);
      const chan = Array.from(panelHost.querySelectorAll('.tm-chans .tm-ch'))
        .find((node) => node.textContent.includes('一轮群'));
      if (chan) {
        chan.click();
        await pause(400);
      }
      const strip = panelHost.querySelector('.tm-thin');
      check('1 轮的群会被当场指出来（不是让人自己去设置里找）', !!strip,
        panelHost.textContent.slice(0, 60));
      check('提示说的是「各说一句就散会」，不是一堆规则名',
        !!strip && /各说一句就散会/.test(strip.textContent), strip && strip.textContent);
      const fix = strip && strip.querySelector('.tm-thinb');
      check('旁边有一颗一键改的按钮', !!fix && /互相接/.test(fix.textContent),
        fix && fix.textContent);

      if (fix) {
        fix.click();
        await pause(900);
        const after = await API.groups();
        const saved = ((after.ok && after.data.items) || []).find((one) => one.id === gid);
        check('点一下就真的改成 2 轮了', !!saved && Number(saved.rounds) === 2,
          saved && saved.rounds);
        check('改完这条提示就消失（不留一句已经做过的事）',
          !panelHost.querySelector('.tm-thin'));
      }
      await API.deleteGroup(gid);
    }
    for (const id of ids) await API.deleteAssistant(id);
  } catch (err) {
    check('一轮群提示自检跑得下去', false, err && err.message);
  }

  /* --- 技能：面板里能建、输入栏能选 --- */
  try {
    /* 1. 技能面板必须有「新建技能」入口。之前技能是只能看不能加的，
       而「让 agent 自己加」加进去的又是临时目录 —— 两件事都不该再发生。 */
    Panels.open('skills');
    /* 等的是「按钮 + 卡片都在」，不是「有按钮就行」。
       技能现在长在插件页的 Skill 格里，而「＋ 新建技能」在**页头**：
       页头是同步画的，卡片要等接口回来。只等按钮，就会在内容还是空的
       时候开始断言 —— 之前这里就是这么红过一次的。 */
    await waitUntil(() => Array.from(document.querySelectorAll('#panelHost button'))
      .some((node) => node.textContent.includes('新建技能'))
      && document.querySelectorAll('#panelHost .pgCard').length > 0, 6000);
    const panelButtons = Array.from(document.querySelectorAll('#panelHost button'))
      .map((node) => node.textContent.trim());
    check('技能面板有「新建技能」入口',
      panelButtons.some((text) => text.includes('新建技能')), panelButtons.join('|'));
    check('技能条目分得清「自己的」和「内置」',
      Array.from(document.querySelectorAll('#panelHost .pcVer, #panelHost .pgCard .badge'))
        .some((node) => ['内置', '自己的'].includes(node.textContent.trim())), '');

    /* 2. 输入栏的技能按钮：能开、能选、能取消 */
    const pop = $('#skillPop');
    if (!pop.classList.contains('hidden')) closeSkillPop();
    await toggleSkillPop();
    await waitUntil(() => document.querySelectorAll('#skillPop .skillItem').length > 0, 4000);
    check('技能浮层能打开', !pop.classList.contains('hidden'), '');

    let rows = Array.from(document.querySelectorAll('#skillPop .skillItem'));
    check('浮层里列出了技能', rows.length >= 2, rows.length);
    const firstName = rows.length ? rows[0].querySelector('.nm').textContent : '';
    if (rows.length) rows[0].click();
    check('点一下技能就选中了', state.skillPick.includes(firstName), state.skillPick.join(','));
    check('输入框上出现了 chip',
      Array.from(document.querySelectorAll('#skillChips .skillChip'))
        .some((chip) => chip.textContent.includes(firstName)), '');
    check('「技能」按钮点亮了', $('#skillBtn').classList.contains('on'), '');

    /* 再点同一项要能取消。这里必须重新查一次节点：点完会重画一遍列表，
       拿旧节点再点，测的是「已经不在页面上的那个元素」。 */
    rows = Array.from(document.querySelectorAll('#skillPop .skillItem'));
    if (rows.length) rows[0].click();
    check('再点一下能取消', !state.skillPick.includes(firstName), state.skillPick.join(','));
    check('取消之后 chip 没了',
      !Array.from(document.querySelectorAll('#skillChips .skillChip'))
        .some((chip) => chip.textContent.includes(firstName)), '');
    closeSkillPop();
    check('点开外的东西能收起浮层', pop.classList.contains('hidden'), '');

    /* 2b. 开关 —— 用户原话「技能没有开关」。
       关掉要同时做到三件事：卡片变灰、浮层里不再列它、磁盘上的文件还在。
       第三条这里验不了（浏览器拿不到磁盘），由 selftest 验。 */
    Panels.open('skills');
    await waitUntil(() => document.querySelectorAll('#panelHost .pgCard .toggle').length > 0, 6000);
    const skillToggles = Array.from(document.querySelectorAll('#panelHost .pgCard .toggle'));
    check('技能卡上都有开关', skillToggles.length > 0, skillToggles.length);
    check('开关默认都是开着的',
      skillToggles.every((node) => node.classList.contains('on')),
      skillToggles.filter((node) => !node.classList.contains('on')).length + ' 个是关的');

    const firstSkillCard = skillToggles[0].closest('.pgCard');
    const toggledName = firstSkillCard.querySelector('.pcName').textContent;
    skillToggles[0].click();
    /* 点完会重画整栏（那条「N 个已关」得跟着出现），所以要等状态而不是等那个节点 */
    await waitUntil(() => {
      const card = Array.from(document.querySelectorAll('#panelHost .pgCard'))
        .find((node) => node.querySelector('.pcName')
          && node.querySelector('.pcName').textContent === toggledName);
      return card && card.classList.contains('off');
    }, 5000);
    const offCard = Array.from(document.querySelectorAll('#panelHost .pgCard'))
      .find((node) => node.querySelector('.pcName')
        && node.querySelector('.pcName').textContent === toggledName);
    check('点一下开关，这张卡就压暗了', !!offCard && offCard.classList.contains('off'), '');
    check('关掉之后标签写着「已关」',
      !!offCard && !!offCard.querySelector('.badge.state')
      && offCard.querySelector('.badge.state').textContent === '已关',
      offCard && offCard.querySelector('.badge.state') && offCard.querySelector('.badge.state').textContent);
    check('关掉之后「用这个」点不动（它现在是关的）',
      !!offCard && !!offCard.querySelector('.pcMini.dim'),
      offCard && offCard.querySelectorAll('.pcMini').length);
    check('出现了一条「N 个已关」和「全部打开」',
      !!document.querySelector('#panelHost .pgOffBar'),
      document.querySelector('#panelHost .pgOffBar') &&
        document.querySelector('#panelHost .pgOffBar').textContent);

    /* 关掉的技能不该出现在输入栏的技能浮层里 —— 列出来再点一下说「已关闭」，
       等于把「我说了不要用」变成一次打断。 */
    if (!pop.classList.contains('hidden')) closeSkillPop();
    await toggleSkillPop();
    await waitUntil(() => !pop.classList.contains('hidden'), 4000);
    check('关掉之后浮层里就没有它了',
      !Array.from(pop.querySelectorAll('.skillItem .nm'))
        .some((node) => node.textContent === toggledName),
      Array.from(pop.querySelectorAll('.skillItem .nm')).map((n) => n.textContent).join(','));
    check('浮层底下会说一句「N 个已关」',
      /个已关/.test(pop.textContent), pop.textContent.slice(-60));
    closeSkillPop();

    /* 全打开，把状态还原 —— 自检不该在用户机器上留下「有一堆技能是关的」 */
    const reopen = Array.from(document.querySelectorAll('#panelHost .pgOffBar button'))[0];
    if (reopen) {
      reopen.click();
      await waitUntil(() => !document.querySelector('#panelHost .pgOffBar'), 5000);
      check('「全部打开」之后那条提示就没了',
        !document.querySelector('#panelHost .pgOffBar'), '');
    }

    /* 3. 跑着的时候也得能开新对话 —— 这是用户报的那条「一个对话在跑时无法建立新对话」。
       注意要同时验 state.running 被清掉：只删掉那句拦截，输入框还挂着「停下」，
       表现依然是「开不了新对话」。 */
    state.running = true;
    updateComposer();
    const sendHiddenWhileRunning = $('#sendBtn').classList.contains('hidden');
    newSession();
    check('跑着的时候也能新建对话', state.cid === null, state.cid);
    check('新建之后 running 跟着清掉', state.running === false, state.running);
    check('新建之后发送按钮回来了',
      !$('#sendBtn').classList.contains('hidden') && sendHiddenWhileRunning, '');
    state.skillPick = [];
    renderSkillChips();
  } catch (err) {
    check('技能自检跑得下去', false, err && err.message);
  }

  /* --- 底层内核：role1 / role2 -----------------------------------------
     两套内核的能力边界不一样（role2 没有审批卡、技能是 DSH 自己那套）。
     界面上必须一眼看得出「这条会话是谁在跑」，否则用户会觉得
     「同一句话它怎么这次不问我了」。 */
  try {
    check('顶栏有一枚内核胶囊', !!$('#engineBtn'));
    check('新会话时胶囊说的是「下一条用哪个」',
      /Role 1|Role 2/.test($('#engineLabel').textContent), $('#engineLabel').textContent);

    $('#engineBtn').click();
    await waitUntil(() => document.querySelectorAll('#modalCard .engCard').length >= 2, 3000);
    const engCards = Array.from(document.querySelectorAll('#modalCard .engCard'));
    check('选择器里有两张卡（两套底层）', engCards.length === 2, engCards.length);
    check('每张卡都写明了工具归谁', engCards.every((card) => /工具：/.test(card.textContent)),
      engCards.map((card) => card.textContent.slice(0, 24)).join(' | '));
    check('每张卡都写明了审批方式', engCards.every((card) => /审批：/.test(card.textContent)));
    /* 这句话是这一整套设计的核心承诺：绑死。不写清楚，用户会以为随时能换，
       换完发现模型看不懂前面的话，再来问「怎么坏了」。 */
    check('说明里写清「建好就绑死」',
      /绑死|换不了|新会话/.test($('#modalCard').textContent),
      ($('#modalCard').textContent || '').slice(0, 60));

    if (engCards.length >= 2) {
      engCards[1].click();
      const switched = await waitUntil(() => /Role 2/.test($('#engineLabel').textContent), 2000);
      check('点了 role2，胶囊跟着变', switched, $('#engineLabel').textContent);
      check('选完弹窗自己关掉', $('#modal').classList.contains('hidden'));
      state.engine = 'role1';
      renderEngine();
    }
  } catch (err) {
    check('内核选择跑得下去', false, err && err.message);
  }

  const engTags = Array.from(document.querySelectorAll('#traceList .trace .tag.eng'));
  check('会话行上那个 ROLE 标签画出来了', engTags.length > 0, engTags.length);
  check('标签上写的是 role1 / role2（不是一串英文 key）',
    engTags.every((tag) => /^role[12]$/.test(tag.textContent.trim())),
    engTags.slice(0, 3).map((tag) => tag.textContent).join(','));

  /* --- 输出 / 日志抽屉（设计稿 dialogs-v1 C 段）-------------------------
     这一条常驻在输入区上面，折叠时只有 34px。它最容易出的事不是「画得不好」，
     而是「展开之后把输入区挤出窗口」—— 那条 34px 的带子一旦变高，
     挤走的正是用户正在打字的地方。 */
  try {
    /* 前面的用例可能把左栏切到了某个面板页。面板页是一整屏，抽屉在那儿是藏着的
       （`.main.page .logDock`），所以先回到会话页再测。 */
    Sidebar.show('sessions');
    await pause(120);

    const dockBar = $('#dockBar');
    check('底下有一条日志抽屉', !!dockBar);
    check('折叠时它只有 34px',
      Math.round(dockBar.getBoundingClientRect().height) === 34,
      Math.round(dockBar.getBoundingClientRect().height));
    check('折叠时也写着有几条日志', /日志\s*\d+\s*条/.test($('#dockSum').textContent),
      $('#dockSum').textContent);
    check('折叠时正文是藏着的', $('#dockBody').classList.contains('hidden'));

    dockBar.click();
    const opened = await waitUntil(() => !$('#dockBody').classList.contains('hidden'), 2000);
    check('点一下就展开', opened);
    check('展开后 aria 也跟着改', dockBar.getAttribute('aria-expanded') === 'true');

    const tabs = $$('#dockHead .dockTab');
    check('展开后有日志 / 任务 / 审批三个格', tabs.length === 3,
      tabs.map((tab) => tab.textContent).join(' | '));
    check('默认停在「日志」', !!tabs[0] && tabs[0].classList.contains('on'), tabs[0] && tabs[0].textContent);

    // 日志是异步拉的，等它回来
    await waitUntil(() => $$('#dockLogs .dockLg').length > 0, 4000);
    const rows = $$('#dockLogs .dockLg');
    check('日志里真有行（刚才那次工具判定被记下来了）', rows.length > 0, rows.length);
    check('每行都有时间 / 来源 / 内容三段',
      rows.every((row) => $('.tm', row) && $('.src', row) && $('.msg', row)));
    check('时间写成 HH:MM:SS',
      rows.every((row) => /^\d{2}:\d{2}:\d{2}$/.test($('.tm', row).textContent.trim())),
      rows[0] && $('.tm', rows[0]).textContent);
    check('放行和待批长得不一样（待批是黄的）',
      rows.some((row) => row.classList.contains('warn')) || rows.every((row) => !row.classList.contains('warn')),
      rows.map((row) => row.className).join(','));

    /* 展开之后窗口里还得装得下输入区 —— 抽屉最高 240px，挤走的应该是对话流。 */
    const cbox = $('.composer').getBoundingClientRect();
    check('抽屉展开后输入区还在窗口里', cbox.bottom <= window.innerHeight + 1,
      `底边 ${Math.round(cbox.bottom)} / 窗口 ${window.innerHeight}`);

    check('来源能做筛选', $$('#dockFilters [data-src]').length > 0);
    check('级别能做筛选', $$('#dockFilters [data-level]').length === 3);

    tabs[2].click();
    await pause(60);
    check('点「审批」就切到审批', Dock.snapshot().tab === 'appr', Dock.snapshot().tab);
    check('审批页只留需要你点头的那几条',
      Dock.snapshot().shown <= Dock.snapshot().logs,
      `${Dock.snapshot().shown} / ${Dock.snapshot().logs}`);

    tabs[1].click();
    await pause(60);
    check('点「任务」就切到任务', Dock.snapshot().tab === 'jobs');
    check('任务页把定时任务列出来', $$('#dockLogs .dockLg').length === Dock.snapshot().jobs,
      `${$$('#dockLogs .dockLg').length} / ${Dock.snapshot().jobs}`);

    tabs[0].click();
    await pause(60);
    $('#dockFold').click();
    await pause(60);
    check('点「收起」就收回去', $('#dockBody').classList.contains('hidden'));
    check('收起后折叠条还在', Math.round($('#dockBar').getBoundingClientRect().height) === 34);
  } catch (err) {
    check('日志抽屉跑得下去', false, err && err.message);
  }

  /* --- 默认权限：一处设定，新对话自动带上 -------------------------------
     「不想一个一个添加」是用户的原话。这一节只验界面：真加目录要走系统
     对话框（点一下会把自检挂住），加完之后「新会话带没带上」由
     scripts/test_http.py 在接口层验。 */
  try {
    Panels.open('settings');
    await waitUntil(() => /所有新对话都能去的地方/.test(panelHost.textContent), 6000);
    check('设置里有一节「默认权限」',
      /默认权限/.test(panelHost.textContent), panelHost.textContent.slice(0, 40));
    check('这一节写清了它管的是「所有新对话」',
      /所有新对话都能去的地方/.test(panelHost.textContent));
    check('空的时候说清了「新对话只会待在工作区里」',
      /只会待在自己的工作区/.test(panelHost.textContent));
    const gFoot = Array.from(panelHost.querySelectorAll('.wsGrantsFoot button'))
      .map((node) => node.textContent.trim());
    check('有「添加位置…」的出口', gFoot.some((t) => t.includes('添加位置')), gFoot.join('|'));
    check('空的时候不出现「全部撤掉」', !gFoot.some((t) => t.includes('全部撤掉')), gFoot.join('|'));
    check('写清了「只对之后新建的对话生效」',
      /之后新建/.test(panelHost.textContent));
    check('也写清了「已经在的对话不会被它改」',
      /不会动它|按它自己那份来/.test(panelHost.textContent));
  } catch (err) {
    check('默认权限那一节跑得下去', false, err && err.message);
  }

  /* --- 停下：不能报「已停」，要报「正在停」；长命令要有动静 ---------------
     用户的原话：「显示已停下但是对话框仍处于不可用状态」。
     病根有三层（阻塞的 communicate、零事件、门闩还占着），后端那两层由
     scripts/selftest.py 验。这里只验界面这一层：
       · 心跳消息要真的显示出来 —— 否则「半天没动静」就还是没动静；
       · 按下停下之后按钮要变成「正在停…」并且不能再点；
       · 这一轮真收尾（running=false）之后，这两个状态都得清干净。 */
  try {
    const hint = $('#usageHint');
    const stopBtn = $('#stopBtn');

    state.project = state.project || 'D:/selfcheck';
    state.running = true;
    state.cancelling = false;
    state.beat = null;
    updateComposer();
    check('刚开跑时状态条写的是「正在跑」', /正在跑/.test(hint.textContent), hint.textContent);

    /* 心跳消息走的是真的那条分发路径，不是直接改 state —— 要验的是
       「后端推来的 toolbeat 能不能走到界面上」，中间断一环就等于没做。 */
    handleStream({ kind: 'toolbeat', tool: 'run_command', label: '跑命令',
      seconds: 12, tail: 'Downloading torch-2.4.0-cp312.whl' });
    check('心跳到了界面上（写着已经跑了多少秒）', /12s/.test(hint.textContent), hint.textContent);
    check('心跳里带着工具名', /跑命令|run_command/.test(hint.textContent), hint.textContent);

    /* 按下停下：先摆界面再去请求。这里只调状态那一半 —— `stop()` 会打网络，
       自检不该依赖服务端。 */
    state.cancelling = true;
    updateComposer();
    check('按下停下之后按钮写「正在停…」', /正在停/.test(stopBtn.textContent), stopBtn.textContent);
    check('正在停的时候按钮点不动（不然连按三次只是刷三个一样的请求）',
      stopBtn.disabled === true, stopBtn.disabled);
    check('状态条也换了话，不再说「正在跑」', /正在停/.test(hint.textContent), hint.textContent);
    check('并且说明为什么要等（正在杀进程）',
      /杀进程|已跑/.test(hint.textContent), hint.textContent);

    /* 这一轮真的收尾了 —— 两个状态都得清，否则下一条会话会挂着「正在停…」 */
    handleStream({ kind: 'state', cid: state.cid || 'uitest', running: false });
    check('收尾之后「正在停」清掉了', state.cancelling === false, state.cancelling);
    check('收尾之后心跳也清了（不然下一条会话一开始就显示上一轮的 12s）',
      state.beat === null, JSON.stringify(state.beat));
    updateComposer();
    check('按钮回到「停下」并且能点', /^停下$/.test(stopBtn.textContent) && !stopBtn.disabled,
      stopBtn.textContent + '/' + stopBtn.disabled);

    /* 还在跑的时候按回车，从前是静默 return —— 那就是用户说的「对话框用不了」。
       现在至少要说一句为什么。 */
    state.running = true;
    updateComposer();
    state.cancelling = false;
    updateComposer();
    check('跑着的时候发送按钮是收起的（点「停下」才对）',
      $('#sendBtn').classList.contains('hidden'), '');
    state.running = false;
    state.beat = null;
    updateComposer();
  } catch (err) {
    check('停下那一节跑得下去', false, err && err.message);
  }

  /* --- 结果 --- */
  const summary = `通过 ${pass.length} · 失败 ${fail.length}`;
  document.documentElement.dataset.uitest = fail.length ? 'fail' : 'pass';
  document.documentElement.dataset.uitestSummary = summary;
  if (fail.length) document.documentElement.dataset.uitestFailures = fail.join(' | ');

  const box = document.createElement('pre');
  box.id = 'uitestReport';
  box.style.cssText = 'position:fixed;inset:0;z-index:9999;background:#0e1014;color:#e7e9ee;' +
    'padding:24px;font:13px/1.7 monospace;overflow:auto;margin:0';
  box.textContent = [
    'DreamRole 前端自检 · ' + summary,
    '',
    ...pass.map((name) => '  [ok]   ' + name),
    ...fail.map((name) => '  [FAIL] ' + name),
  ].join('\n');
  document.body.appendChild(box);
})();
