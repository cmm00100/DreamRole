/* 界面基础件：元素构造、轻量 Markdown、提示条、弹层。
 *
 * 为什么自己写 Markdown 而不用现成库：模型输出里的 Markdown 只用得上
 * 加粗、行内代码、代码块、列表、标题这几种。引一个 30KB 的解析器，
 * 换来的是更多的 XSS 面和打包体积。这里先转义再加标签，从根上不认原始 HTML。
 */

const $ = (selector, root) => (root || document).querySelector(selector);
const $$ = (selector, root) => Array.from((root || document).querySelectorAll(selector));

/* h('div.row.on', {title: 'x'}, '文字', childNode) */
function h(spec, attrs, ...children) {
  const parts = String(spec).split('.');
  const tag = parts.shift() || 'div';
  const node = document.createElement(tag);
  for (const cls of parts) if (cls) node.classList.add(cls);

  if (attrs && typeof attrs === 'object' && !(attrs instanceof Node) && !Array.isArray(attrs)) {
    for (const [key, value] of Object.entries(attrs)) {
      if (value === null || value === undefined || value === false) continue;
      if (key === 'class') node.className += (node.className ? ' ' : '') + value;
      else if (key === 'dataset') Object.assign(node.dataset, value);
      else if (key === 'style' && typeof value === 'object') Object.assign(node.style, value);
      else if (key.startsWith('on') && typeof value === 'function') {
        node.addEventListener(key.slice(2).toLowerCase(), value);
      } else if (key === 'html') node.innerHTML = value;
      else if (key in node) node[key] = value;
      else node.setAttribute(key, value);
    }
  } else if (attrs !== undefined && attrs !== null) {
    children.unshift(attrs);
  }

  const append = (child) => {
    if (child === null || child === undefined || child === false) return;
    if (Array.isArray(child)) return child.forEach(append);
    if (child instanceof Node) return node.appendChild(child);
    node.appendChild(document.createTextNode(String(child)));
  };
  children.forEach(append);
  return node;
}

function escapeHtml(text) {
  return String(text == null ? '' : text)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/* 只认白名单标签的极简 Markdown。先转义、再插标签，顺序不能反。 */
function markdown(source) {
  if (!source) return '';
  const blocks = [];
  let text = String(source).replace(/\r\n/g, '\n');

  // 代码块先挖出来占位，避免里面的内容被后面的规则误伤
  text = text.replace(/```([a-zA-Z0-9_+-]*)\n?([\s\S]*?)```/g, (_m, lang, code) => {
    const index = blocks.length;
    blocks.push(
      `<pre class="code"><code>${escapeHtml(code.replace(/\n$/, ''))}</code></pre>`
    );
    return `\u0000BLOCK${index}\u0000`;
  });

  text = escapeHtml(text);

  text = text
    .replace(/^######\s+(.*)$/gm, '<h6>$1</h6>')
    .replace(/^#####\s+(.*)$/gm, '<h5>$1</h5>')
    .replace(/^####\s+(.*)$/gm, '<h4>$1</h4>')
    .replace(/^###\s+(.*)$/gm, '<h3>$1</h3>')
    .replace(/^##\s+(.*)$/gm, '<h2>$1</h2>')
    .replace(/^#\s+(.*)$/gm, '<h1>$1</h1>');

  text = text
    .replace(/`([^`\n]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[\s(])\*([^*\n]+)\*/g, '$1<em>$2</em>')
    .replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g,
      '<a href="$2" target="_blank" rel="noreferrer noopener">$1</a>');

  // 列表：连续的 - 或 1. 行各自成组
  text = text.replace(/(?:^[ \t]*[-*]\s+.*(?:\n|$))+/gm, (chunk) => {
    const items = chunk.trim().split('\n')
      .map((line) => `<li>${line.replace(/^[ \t]*[-*]\s+/, '')}</li>`).join('');
    return `<ul>${items}</ul>`;
  });
  text = text.replace(/(?:^[ \t]*\d+\.\s+.*(?:\n|$))+/gm, (chunk) => {
    const items = chunk.trim().split('\n')
      .map((line) => `<li>${line.replace(/^[ \t]*\d+\.\s+/, '')}</li>`).join('');
    return `<ol>${items}</ol>`;
  });

  text = text.replace(/\n{2,}/g, '</p><p>').replace(/\n/g, '<br>');

  text = `\u0000${text}\u0000`.replace(/(?:\u0000BLOCK(\d+)\u0000)/g, (_m, i) => blocks[Number(i)]);
  text = text.replace(/\u0000/g, '');
  return text;
}

function toast(message, kind) {
  const host = $('#toasts');
  if (!host) return;
  const node = h('div.toast' + (kind ? '.' + kind : ''), message);
  host.appendChild(node);
  setTimeout(() => {
    node.style.transition = 'opacity .25s, transform .25s';
    node.style.opacity = '0';
    node.style.transform = 'translateY(6px)';
    setTimeout(() => node.remove(), 260);
  }, kind === 'bad' ? 5200 : 3200);
}

const Modal = {
  open({ title, subtitle, body, actions, wide, width }) {
    const card = $('#modalCard');
    card.innerHTML = '';
    /* 宽度只有三档：默认、wide（860，看表格/对比用）、width（给一个具体像素）。
       需要具体像素的只有表单类弹窗 —— 表单的宽度是被「一行放几个格子」定死的，
       跟着内容自适应会变成一行一个，看起来很空。 */
    if (wide) card.style.width = 'min(860px, 94vw)';
    else if (width) card.style.width = `min(${Math.round(width)}px, 94vw)`;
    else card.style.width = '';
    if (title) card.appendChild(h('h2', title));
    if (subtitle) card.appendChild(h('p.sub', subtitle));
    if (body) card.appendChild(body);
    const foot = h('div.modalFoot');
    (actions || []).forEach((action) => {
      const button = h('button.' + (action.kind === 'primary' ? 'primary' : action.kind === 'danger' ? 'danger' : 'ghost'),
        { onclick: () => action.onClick && action.onClick(Modal) }, action.label);
      /* onMount：让调用方拿到这个按钮本身。
         只有一种地方需要它 —— 「必须手打名字才能点亮删除」这种**跟着输入变**的按钮。
         为此把整个 Modal 改成受控组件不值得，给一个按钮引用刚刚好。 */
      if (action.onMount) action.onMount(button);
      foot.appendChild(button);
    });
    if ((actions || []).length) card.appendChild(foot);
    $('#modal').classList.remove('hidden');
  },
  close() {
    $('#modal').classList.add('hidden');
    $('#modalCard').innerHTML = '';
  },
  isOpen() {
    return !$('#modal').classList.contains('hidden');
  },
};

function relTime(stamp) {
  if (!stamp) return '';
  const seconds = Math.max(0, Date.now() / 1000 - Number(stamp));
  if (seconds < 60) return '刚刚';
  if (seconds < 3600) return Math.floor(seconds / 60) + ' 分钟前';
  if (seconds < 86400) return Math.floor(seconds / 3600) + ' 小时前';
  if (seconds < 86400 * 7) return Math.floor(seconds / 86400) + ' 天前';
  const date = new Date(Number(stamp) * 1000);
  return `${date.getMonth() + 1}-${date.getDate()}`;
}

function clockTime(stamp) {
  if (!stamp) return '';
  const date = new Date(Number(stamp) * 1000);
  return `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`;
}

function shortNumber(value) {
  const n = Number(value) || 0;
  if (n < 1000) return String(n);
  if (n < 1000000) return (n / 1000).toFixed(n < 10000 ? 1 : 0) + 'k';
  return (n / 1000000).toFixed(1) + 'M';
}

/* 自动把 textarea 撑高到内容高度，最多到 maxHeight */
function autoGrow(textarea, maxHeight) {
  textarea.style.height = 'auto';
  const next = Math.min(textarea.scrollHeight, maxHeight || 190);
  textarea.style.height = next + 'px';
}

function debounce(fn, wait) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
}
