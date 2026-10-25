/* 「＋」菜单里那些「往输入框塞东西」的动作。
 *
 * 设计稿 12 号文档 C 表一共八项：文件 / 图片 / 挂 Skill / 选专家 /
 * 工作区文件… / 引用会话·消息 / 从提示词库插入… / 存为自动化…。
 *
 * 为什么值得单独一个文件：这八项里有一半要弹一个自己的选择器（文件、工作区、
 * 会话、提示词库），每个选择器都是「列一批东西 → 点一个 → 插进输入框」。
 * 把它们摊在 app.js 里，会淹没在会话流和输入区的代码中间 ——
 * 而它们其实是同一件事：**给这句话补充上下文**。
 *
 * 一条纪律：插进去的一律是 `@引用`，不是把内容直接贴进输入框。
 * 贴内容会让输入框变成一面墙，用户改不动；`@` 由后端 `_expand_refs` 展开，
 * 界面上只留一个短名字。
 */

const Refs = (() => {
  /* ------------------------------------------------------------ 插入 */

  function inputEl() {
    return document.getElementById('input');
  }

  /* 塞一段文本进去。前后各留一个空格，除非已经在行首/行尾 ——
     `@a.txt` 紧贴着前一个字时，后端那条 `REF_PATTERN` 会连前一个字一起吃掉。 */
  function insertText(text) {
    const input = inputEl();
    if (!input) return;
    input.focus();
    const start = input.selectionStart == null ? input.value.length : input.selectionStart;
    const end = input.selectionEnd == null ? start : input.selectionEnd;
    const before = input.value.slice(0, start);
    const after = input.value.slice(end);
    const lead = !before || /\s$/.test(before) ? '' : ' ';
    const tail = !after || /^\s/.test(after) ? '' : ' ';
    const chunk = lead + text + tail;
    input.value = before + chunk + after;
    const caret = start + chunk.length;
    input.selectionStart = caret;
    input.selectionEnd = caret;
    input.dispatchEvent(new Event('input', { bubbles: true }));
  }

  function workspaceRoot() {
    return (state && state.project) || '';
  }

  function norm(path) {
    return String(path || '').replace(/\\/g, '/').replace(/\/+$/, '');
  }

  /* 工作区里的文件写成相对路径，工作区外的写绝对路径。
     为什么：后端展开 `@` 时是按 `root / name` 找的，绝对路径会被判成
     「不在项目目录里」。所以能写成相对的就必须写成相对的。 */
  function refName(path) {
    const root = workspaceRoot();
    const full = norm(path);
    if (!root) return path;
    const base = norm(root);
    if (full.toLowerCase().startsWith(base.toLowerCase() + '/')) {
      return full.slice(base.length + 1);
    }
    return path;
  }

  function toastOutside(path) {
    const root = workspaceRoot();
    if (root && norm(path).toLowerCase().startsWith(norm(root).toLowerCase() + '/')) return;
    toast('这个文件在工作区外，只会带上路径（要它直接读内容，得先加一张通行证）');
  }

  /* ------------------------------------------------------------ 文件 */

  async function pickFile(images) {
    const res = await API.pickFile(images ? '选一张图片' : '选一个文件', !!images);
    if (!res.ok) return toast(res.error || '打不开文件对话框', 'bad');
    const path = res.data && res.data.path;
    if (!path) return;                       // 用户点了取消
    const name = refName(path);
    insertText('@' + name);
    toastOutside(path);
    toast(images ? '图片已经引用进来' : '文件已经引用进来', 'good');
  }

  /* 工作区文件：不走系统对话框，就在工作区里挑 —— 因为这一项的意思
     恰恰是「我只要这个项目里的东西」。从根目录起，可以往下走，点文件即引用。 */
  async function pickWorkspaceFile() {
    const root = workspaceRoot();
    if (!root) return toast('这条会话还没选工作区', 'bad');

    let cwd = root;
    const crumb = h('div.rfCrumb');
    const list = h('div.rfList');
    const search = h('input.input', { placeholder: '在这个目录里找…（只按名字过滤）' });
    let entries = [];

    function paint() {
      crumb.innerHTML = '';
      const rel = norm(cwd).slice(norm(root).length).replace(/^\//, '');
      const parts = rel ? rel.split('/') : [];
      crumb.appendChild(h('button.rfCrumbItem', {
        onclick: () => { cwd = root; load(root); },
      }, '工作区根'));
      let walk = root;
      parts.forEach((part) => {
        walk = walk + '/' + part;
        const target = walk;
        crumb.appendChild(h('span.rfSep', '/'));
        crumb.appendChild(h('button.rfCrumbItem', {
          onclick: () => { cwd = target; load(target); },
        }, part));
      });

      const keyword = search.value.trim().toLowerCase();
      const shown = entries.filter((one) => !keyword || one.name.toLowerCase().includes(keyword));
      list.innerHTML = '';
      if (!shown.length) {
        list.appendChild(h('div.empty', { style: { padding: '16px 4px', fontSize: '11px' } },
          keyword ? '这个目录里没有名字带「' + keyword + '」的' : '这个目录是空的'));
        return;
      }
      shown.slice(0, 300).forEach((entry) => {
        list.appendChild(h('button.rfRow' + (entry.dir ? '.dir' : ''), {
          title: entry.path,
          onclick: () => {
            if (entry.dir) { load(entry.path); return; }
            Modal.close();
            insertText('@' + refName(entry.path));
            toast('已引用 ' + entry.name, 'good');
          },
        },
          h('span.rfIc', entry.dir ? '▸' : '·'),
          h('span.rfName', entry.name),
          h('span.rfGo', entry.dir ? '进' : '引用')));
      });
    }

    async function load(path) {
      const res = await API.browse(path);
      if (!res.ok) {
        toast(res.error || '这个目录打不开', 'bad');
        return;
      }
      cwd = res.data.path || path;
      entries = (res.data.entries || []);
      paint();
    }

    search.addEventListener('input', paint);

    Modal.open({
      title: '从工作区挑一个文件',
      subtitle: '点文件即引用（写成相对路径），点目录进去。',
      width: 620,
      body: h('div.rfBody', crumb, search, list),
      actions: [{ label: '关了', onClick: () => Modal.close() }],
    });
    await load(root);
  }

  /* ------------------------------------------------------- 引用会话 */

  /* 引用一段历史：列出会话 → 看它的几条问答 → 点一条，以引用块的形式插进输入框。
     为什么是引用块而不是 `@`：会话不是文件，后端没有「展开一条会话」的通道；
     而用户要的往往就是那句话本身。 */
  async function quoteSession() {
    const res = await API.traces(null, true);
    const rows = ((res.ok && res.data) || []).filter((one) => one.cid !== state.cid);
    Modal.open({
      title: '引用一条会话里的话',
      subtitle: '选一条会话，再点其中一句话 —— 它会以引用块的形式插进输入框。',
      width: 640,
      body: h('div.rfBody',
        rows.length
          ? h('div.rfList', rows.slice(0, 60).map((row) =>
            h('button.rfRow', {
              title: row.project || '',
              onclick: () => openMessages(row),
            },
              h('span.rfIc', row.running ? '▮' : '·'),
              h('span.rfName', row.title || '未命名'),
              h('span.rfGo', relTime(row.updated || row.created)))))
          : h('div.empty', { style: { padding: '18px 4px' } }, '还没有别的会话可引用')),
      actions: [{ label: '关了', onClick: () => Modal.close() }],
    });
  }

  async function openMessages(row) {
    const res = await API.getSession(row.cid);
    if (!res.ok) return toast(res.error || '这条会话打不开', 'bad');
    const events = (res.data && res.data.events) || [];
    const picked = events
      .filter((one) => one.type === 'user' || one.type === 'answer')
      .filter((one) => (one.content || '').trim())
      .slice(-30);
    Modal.open({
      title: '引用 · ' + (row.title || '未命名'),
      subtitle: '点一句，它会带着出处插进输入框。',
      width: 680,
      body: h('div.rfBody',
        picked.length
          ? h('div.rfQuotes', picked.map((one) => h('button.rfQuote', {
            title: '点一下引用这句',
            onclick: () => {
              Modal.close();
              const who = one.type === 'user' ? '我' : '它';
              const body = String(one.content || '').trim().replace(/\r/g, '');
              const lines = body.split('\n').slice(0, 6).join('\n> ');
              insertText(`> 引自「${row.title || '未命名'}」· ${who}：${lines}`);
              toast('引用已插入', 'good');
            },
          },
            h('div.rfQuoteHead', (one.type === 'user' ? '我' : '它') + ' · ' + relTime(one.ts || one.at || '')),
            h('div.rfQuoteBody', String(one.content || '').trim().slice(0, 220)))))
          : h('div.empty', { style: { padding: '18px 4px' } }, '这条会话里没有可引用的问答')),
      actions: [
        { label: '换一条', onClick: () => quoteSession() },
        { label: '关了', onClick: () => Modal.close() },
      ],
    });
  }

  /* ------------------------------------------------------- 提示词库 */

  let cache = null;

  async function loadLibrary(force) {
    if (cache && !force) return cache;
    const res = await API.prompts();
    cache = (res.ok && (res.data.items || res.data || [])) || [];
    return cache;
  }

  function forget() {
    cache = null;
  }

  /* 收藏一句。`source` 记下它从哪儿来（输入框 / 优化后），
     因为「我自己写的」和「模型改出来的」回头看的用法不一样。 */
  async function savePrompt(text, source, extra) {
    const body = String(text || '').trim();
    if (!body) return toast('空的不收', 'bad');
    const opts = extra || {};
    const res = await API.savePrompt({
      title: opts.title || body.split('\n')[0].slice(0, 40),
      text: body,
      tags: opts.tags || [],
      source: source || '手写',
    });
    if (!res.ok) return toast(res.error || '存不进提示词库', 'bad');
    forget();
    toast('已存进提示词库', 'good');
    return res.data && res.data.item;
  }

  /* 提示词库（设计稿 D 段）：一个 560 宽的面板，头一行「共 N 条 · 管理」，
     下面是搜索 + 一行一条（标题 / 一句说明 / 标签 / 插入）。
     为什么行里只放一句说明：这个列表是用来**扫**的 —— 全显示正文会变成一屏
     散文，扫不出「哪一条是我要的」。真要细看，插进去就看见了。 */
  async function insertPrompt() {
    const items = await loadLibrary(true);
    const search = h('input.libSrch', { placeholder: '搜索标题、标签、内容' });
    const list = h('div.libList');
    const head = h('div.libH');
    let tag = '';
    let managing = false;

    function paintHead() {
      head.innerHTML = '';
      head.appendChild(h('span.libHIc', '✨'));
      head.appendChild(h('span.libHTitle', '提示词库'));
      head.appendChild(h('span.libHMeta', `共 ${items.length} 条`));
      head.appendChild(h('button.libHAct' + (managing ? '.on' : ''), {
        onclick: () => { managing = !managing; paintHead(); paint(); },
      }, managing ? '收起管理' : '管理'));
    }

    function paint() {
      const keyword = search.value.trim().toLowerCase();
      const shown = items.filter((one) => {
        if (tag && !(one.tags || []).includes(tag)) return false;
        if (!keyword) return true;
        return (one.title + ' ' + one.text + ' ' + (one.tags || []).join(' '))
          .toLowerCase().includes(keyword);
      });
      list.innerHTML = '';
      if (!shown.length) {
        list.appendChild(h('div.empty', { style: { padding: '18px 10px', fontSize: '11px' } },
          items.length ? '没有匹配的' : '提示词库还是空的。在输入框里写一句，'
            + '用「✨ 提示词优化」改完点「存为收藏」，或者点下面「存当前这句话」。'));
        return;
      }
      shown.forEach((one) => {
        const row = h('div.libIt',
          h('div.libItMain',
            h('div.libItT', one.title || '未命名'),
            h('div.libItD', one.text.replace(/\s+/g, ' ').slice(0, 96))),
          (one.tags && one.tags.length)
            ? h('span.libTag', '#' + one.tags[0]) : null,
          managing
            ? h('button.libDel', {
              title: '删掉这条收藏（只删收藏，不影响用过的会话）',
              onclick: async () => {
                const res = await API.delPrompt(one.id);
                if (!res.ok) return toast(res.error || '删不掉', 'bad');
                forget();
                const at = items.findIndex((x) => x.id === one.id);
                if (at >= 0) items.splice(at, 1);
                paintHead();
                paint();
                toast('删掉了');
              },
            }, '删')
            : null,
          h('button.libIns', {
            title: '插进输入框（插进去之后还能改）',
            onclick: () => { Modal.close(); insertText(one.text); toast('插进去了'); },
          }, '插入'));
        if (one.tags && one.tags.includes(tag)) row.classList.add('onTag');
        list.appendChild(row);
      });
    }

    const tagBar = h('div.plTags.bar');
    function paintTags() {
      const all = [];
      items.forEach((one) => (one.tags || []).forEach((name) => {
        if (!all.includes(name)) all.push(name);
      }));
      tagBar.innerHTML = '';
      if (!all.length) { tagBar.classList.add('hidden'); return; }
      tagBar.classList.remove('hidden');
      tagBar.appendChild(h('button.plTag' + (tag ? '' : '.on'), {
        onclick: () => { tag = ''; paintTags(); paint(); },
      }, '全部'));
      all.slice(0, 12).forEach((name) => {
        tagBar.appendChild(h('button.plTag' + (tag === name ? '.on' : ''), {
          onclick: () => { tag = tag === name ? '' : name; paintTags(); paint(); },
        }, '#' + name));
      });
    }

    search.addEventListener('input', paint);

    Modal.open({
      title: '从提示词库插入',
      subtitle: '收藏的是整段话，插进去之后还能接着改。',
      width: 560,
      body: h('div.rfBody', head, search, tagBar, list),
      actions: [
        {
          label: '存当前这句话',
          onClick: async () => {
            const text = (inputEl() || {}).value || '';
            if (!text.trim()) return toast('输入框是空的', 'bad');
            await savePrompt(text, '输入框');
            await insertPrompt();
          },
        },
        { label: '关了', onClick: () => Modal.close() },
      ],
    });
    paintHead();
    paintTags();
    paint();
  }

  return {
    insertText, refName, pickFile, pickWorkspaceFile, quoteSession,
    insertPrompt, savePrompt, forget,
    library: () => loadLibrary(true),
  };
})();
