/* 「这个对话产出了什么」—— 右上角那个按钮打开的东西。
 *
 * 它替代的是原来那个「工具 / 技能 / 设置」抽屉：那三个在左侧栏本来就有，
 * 两处入口指向同一批内容，结果是人得先想「我该从哪边进」。
 * 而现在这个位置回答的是另一个问题 —— 不是「这东西怎么配」，是
 * 「我们刚才到底做出了什么」。
 *
 * 数据只有一处来源：后端 /api/session/{cid}/artifacts，它从轨迹和改动历史里读。
 * 前端不自己拼、不自己缓存判断 —— 缓存一份「产物清单」的后果是
 * 面板上写着有、盘上其实没了，这种不一致比不做还糟。
 */

const Artifacts = (() => {
  let ctx = null;
  let busy = false;

  const LABEL = { created: '新建', modified: '修改', written: '写过', shot: '截图' };
  const BADGE = { created: 'acc', modified: '', written: '', shot: 'info' };
  const IMAGE = new Set(['png', 'jpg', 'jpeg', 'webp', 'gif', 'bmp']);

  function init(app) {
    ctx = app;
  }

  function host() {
    return $('#artPanel');
  }

  function isOpen() {
    const node = host();
    return !!node && !node.classList.contains('hidden');
  }

  function close() {
    if (host()) host().classList.add('hidden');
  }

  function toggle() {
    if (isOpen()) close();
    else open();
  }

  function open() {
    if (!host()) return;
    host().classList.remove('hidden');
    render();
  }

  /* 侧栏上的数字：不打开也要知道「这一轮有没有产出东西」。 */
  async function sync() {
    const badge = $('#artCount');
    if (!badge) return;
    const cid = ctx && ctx.state ? ctx.state.cid : '';
    if (!cid) {
      badge.classList.add('hidden');
      return;
    }
    const result = await API.artifacts(cid);
    const total = result.ok ? (result.data.total || 0) : 0;
    badge.textContent = total ? String(total) : '';
    badge.classList.toggle('hidden', !total);
  }

  function bytes(size) {
    const n = Number(size) || 0;
    if (!n) return '';
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
    return (n / 1048576).toFixed(1) + ' MB';
  }

  function iconOf(item) {
    const ext = String(item.ext || '').slice(0, 4) || '·';
    const kind = IMAGE.has(String(item.ext || '')) ? 'img' : 'txt';
    return h('span.artIco.' + kind, ext);
  }

  async function render() {
    const panel = host();
    if (!panel) return;
    panel.innerHTML = '';

    const cid = ctx && ctx.state ? ctx.state.cid : '';
    panel.appendChild(h('div.drawerHead',
      h('span.t', '这个对话的产物'),
      h('span.artTotal', { id: 'artTotal' }, ''),
      h('div.grow'),
      h('button.icon', {
        onclick: () => render(),
        title: '重新读一遍',
      }, h('span', { html: '<svg viewBox="0 0 24 24" width="14" height="14"><path d="M20 12a8 8 0 11-2.3-5.6M20 4v4h-4" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>' })),
      h('button.icon', { onclick: close, title: '收起' }, '✕')
    ));

    const body = h('div.drawerBody');
    panel.appendChild(body);

    if (!cid) {
      body.appendChild(h('div.empty.artEmpty',
        h('div', '还没有打开会话'),
        h('div.artSub', '打开一条会话，这里会列出它写进磁盘的所有文件。')
      ));
      return;
    }

    body.appendChild(h('div.empty', '读取中…'));
    const result = await API.artifacts(cid);
    body.innerHTML = '';
    if (!result.ok) {
      body.appendChild(h('div.empty', result.error));
      return;
    }

    const data = result.data || {};
    const items = data.items || [];
    const counts = data.counts || {};
    const total = data.total || 0;

    const badge = $('#artCount');
    if (badge) {
      badge.textContent = total ? String(total) : '';
      badge.classList.toggle('hidden', !total);
    }

    if (!items.length) {
      body.appendChild(h('div.empty.artEmpty',
        h('div', '这个对话还没产出文件'),
        h('div.artSub', '它写文件（write_file / edit_file）或截图之后，会一条条出现在这里。'),
        h('div.artSub', '只有「嘴上说说」的对话，确实什么都不会有。')
      ));
      return;
    }

    const parts = [];
    ['created', 'modified', 'written', 'shot'].forEach((key) => {
      if (counts[key]) parts.push(LABEL[key] + ' ' + counts[key]);
    });
    body.appendChild(h('div.artSum',
      h('span.artSumT', parts.join(' · ')),
      h('div.grow'),
      ctx && ctx.state && ctx.state.project
        ? h('span.artWhere', { title: ctx.state.project }, lastTwo(ctx.state.project))
        : null
    ));

    const list = h('div.artList');
    body.appendChild(list);

    items.forEach((item) => {
      const row = h('div.artItem' + (item.exists ? '' : '.gone'));
      row.appendChild(h('div.artTop',
        iconOf(item),
        h('span.artName', { title: item.path }, item.name),
        h('span.tag' + (BADGE[item.kind] ? '.' + BADGE[item.kind] : ''), LABEL[item.kind] || '产物'),
        item.exists ? null : h('span.tag.bad', '已不在')
      ));

      row.appendChild(h('div.artPath', { title: item.path }, item.rel || item.path));

      const meta = [];
      if (item.turns && item.turns.length) meta.push('第 ' + item.turns.join('、') + ' 轮');
      if (item.writes > 1) meta.push('写了 ' + item.writes + ' 次');
      if (item.ts) meta.push(relTime(item.ts));
      const size = bytes(item.size);
      if (size) meta.push(size);
      row.appendChild(h('div.artMeta', meta.join(' · ')));

      const acts = h('div.artActs');
      if (item.has_diff) {
        acts.appendChild(h('button.mini', {
          onclick: () => showDiff(cid, item),
        }, '看 diff'));
      }
      if (IMAGE.has(String(item.ext || '')) && item.exists) {
        acts.appendChild(h('button.mini', {
          onclick: () => showImage(item),
        }, '预览'));
      }
      if (item.exists) {
        acts.appendChild(h('button.mini', {
          onclick: async () => {
            const res = await API.reveal(item.path);
            if (!res.ok) toast(res.error, 'bad');
          },
        }, '位置'));
      }
      acts.appendChild(h('button.mini', {
        onclick: async () => {
          try {
            await navigator.clipboard.writeText(item.path);
            toast('路径已复制', 'good');
          } catch {
            toast('复制不了，路径是：' + item.path, 'bad');
          }
        },
      }, '复制路径'));
      row.appendChild(acts);

      list.appendChild(row);
    });

    body.appendChild(h('div.artFoot',
      '只列 write_file / edit_file 写过的文件和截图。run_command 里间接生成的东西看不到 —— '
      + '它的参数里没有「写到哪」，硬猜只会给出一个看着完整其实漏了的清单。'
    ));
  }

  function lastTwo(path) {
    const parts = String(path || '').split(/[\\/]/).filter(Boolean);
    return parts.slice(-2).join('/') || String(path || '');
  }

  async function showDiff(cid, item) {
    const text = await API.artifactDiff(cid, item.path);
    Modal.open({
      title: item.name,
      subtitle: '这条会话从头到尾对它做了什么',
      body: h('pre.code', text || '（读不到 diff）'),
      wide: true,
      actions: [{ label: '知道了', kind: 'primary', onClick: () => Modal.close() }],
    });
  }

  function showImage(item) {
    Modal.open({
      title: item.name,
      subtitle: item.rel,
      body: h('div.artShot',
        h('img', { src: '/api/artifact/raw?path=' + encodeURIComponent(item.path), alt: item.name })
      ),
      wide: true,
      actions: [{ label: '知道了', kind: 'primary', onClick: () => Modal.close() }],
    });
  }

  return { init, open, close, toggle, isOpen, render, sync };
})();
