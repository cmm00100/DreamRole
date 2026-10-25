/* 与后端说话的唯一入口。
 *
 * 所有请求都在这里兜住错误，返回 {ok, data, error} 这种统一形状。
 * 这样上层不用写 try/catch，也不会出现「一个接口报错，整页白掉」。
 */

const API = (() => {
  async function req(method, path, body) {
    const init = { method, headers: {} };
    if (body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    let response;
    try {
      response = await fetch(path, init);
    } catch (err) {
      return { ok: false, error: '连不上本地服务了：' + err.message };
    }

    const text = await response.text();
    let data = null;
    if (text) {
      try {
        data = JSON.parse(text);
      } catch {
        data = { raw: text };
      }
    }
    if (!response.ok) {
      const detail = (data && (data.error || data.detail)) || response.statusText;
      return { ok: false, error: String(detail), status: response.status, data };
    }
    return { ok: true, data };
  }

  const get = (p) => req('GET', p);
  const post = (p, b) => req('POST', p, b || {});
  const patch = (p, b) => req('PATCH', p, b || {});
  const del = (p) => req('DELETE', p);

  return {
    req,
    bootstrap: () => get('/api/bootstrap'),

    createSession: (payload) => post('/api/session', payload),
    /* 工作区是会话级的：换目录只影响这一条会话 */
    setWorkspace: (cid, project) => post(`/api/session/${cid}/workspace`, { project }),
    getSession: (cid) => get('/api/session/' + encodeURIComponent(cid)),
    /* skills 是这一轮点名要用的技能名。不传就和以前一样 —— 后端当没选过。 */
    send: (cid, text, skills) => post(`/api/session/${cid}/send`,
      skills && skills.length ? { text, skills } : { text }),
    answer: (cid, id, decision) => post(`/api/session/${cid}/answer`, { id, decision }),
    cancel: (cid) => post(`/api/session/${cid}/cancel`),
    setMode: (cid, mode) => post(`/api/session/${cid}/mode`, { mode }),
    setPermission: (cid, permission) => post(`/api/session/${cid}/permission`, { permission }),
    /* 深度思考档位：off / low / medium / high */
    setThink: (cid, level) => post(`/api/session/${cid}/think`, { level }),
    mermaid: (cid) => fetch(`/api/session/${cid}/mermaid`).then((r) => r.text()),

    /* 重来：把上一次尝试从轨迹里删掉，用同一句原话再跑一遍 */
    retry: (cid) => post(`/api/session/${cid}/retry`),
    /* 改一句再发：把第 index 条用户消息换掉，从那里重跑 */
    editTurn: (cid, index, text) => post(`/api/session/${cid}/edit`, { index, text }),
    /* 从第 index 条用户消息分叉出一个新会话，原来那条留着 */
    branch: (cid, index) => post(`/api/session/${cid}/branch`, { index }),

    /* 导出：直接走浏览器下载，不让前端自己拼文件 */
    exportUrl: (cid, fmt) =>
      `/api/session/${cid}/export?fmt=${encodeURIComponent(fmt)}&download=1`,
    exportRaw: (cid, fmt) =>
      fetch(`/api/session/${cid}/export?fmt=${encodeURIComponent(fmt)}&download=0`).then((r) => r.text()),

    search: (q) => get('/api/search?q=' + encodeURIComponent(q)),
    usage: () => get('/api/usage'),

    /* 会话列表。archived 决定「已归档的要不要一起拿」——
       接口默认不返回归档，所以不看归档时必须显式传 1。
       这个参数漏过一次：前端点了「显示已归档」，接口压根没给那些数据，
       于是按钮看起来是坏的（实际是空的）。 */
    traces: (project, archived) => {
      const query = [];
      if (project) query.push('project=' + encodeURIComponent(project));
      if (archived) query.push('archived=1');
      return get('/api/traces' + (query.length ? '?' + query.join('&') : ''));
    },
    patchTrace: (cid, payload) => patch('/api/traces/' + cid, payload),
    /* 删之前先问代价：会作废几条改动、几个检查点。删除框拿它说话 */
    traceImpact: (cid) => get(`/api/traces/${encodeURIComponent(cid)}/impact`),
    deleteTrace: (cid) => post(`/api/traces/${encodeURIComponent(cid)}/delete`),

    templates: () => get('/api/promptopt/templates'),
    /* goals：这一版想要什么（更清晰 / 更短 / 加示例…），叠在模板之上 */
    optimize: (text, template, target, goals) => post('/api/promptopt/optimize',
      { text, template, target: target || '', goals: goals || [] }),
    compare: (original, optimized) => post('/api/promptopt/compare', { original, optimized }),

    settings: () => get('/api/settings'),
    saveSettings: (payload) => post('/api/settings', payload),
    testModel: () => post('/api/settings/test'),

    tools: () => get('/api/tools'),
    toggleTool: (name, enabled) => post(`/api/tools/${name}/toggle`, { enabled }),
    skills: () => get('/api/skills'),
    readSkill: (name) => get('/api/skills/' + encodeURIComponent(name)),
    createSkill: (payload) => post('/api/skills', payload),
    deleteSkill: (name) => post('/api/skills/' + encodeURIComponent(name) + '/delete', {}),
    /* 开关：关掉不是删掉 —— 正文还在，只是不进系统提示、不进 @ 候选 */
    toggleSkill: (name, enabled) => post('/api/skills/' + encodeURIComponent(name) + '/toggle', { enabled }),
    enableAllSkills: () => post('/api/skills/enable-all', {}),

    /* 全局通行证：新会话起手就带着的那几张（设置页里改） */
    globalGrants: () => get('/api/grants/global'),
    updateGlobalGrants: (body) => post('/api/grants/global', body || {}),

    mcpList: () => get('/api/mcp'),
    mcpAdd: (payload) => post('/api/mcp', payload),
    mcpConnect: (name) => post(`/api/mcp/${name}/connect`),
    mcpClose: (name) => post(`/api/mcp/${name}/disconnect`),
    mcpDelete: (name) => post(`/api/mcp/${name}/delete`),

    schedules: () => get('/api/schedules'),
    scheduleRuns: (limit) => get('/api/schedules/runs' + (limit ? '?limit=' + limit : '')),
    addSchedule: (payload) => post('/api/schedules', payload),
    patchSchedule: (id, payload) => patch('/api/schedules/' + id, payload),
    delSchedule: (id) => del('/api/schedules/' + id),
    /* 试运行：和到点自动跑同一条路，会真的开一条会话 */
    runSchedule: (id) => post(`/api/schedules/${id}/run`),

    /* 改动记录：**每条都挂在某条对话下**。
       不传 cid 拿全量（每条自带 session_alive / blocked），传了就看那一条的。
       diff 和 revert 必须带 cid —— 同一个文件可能被两条对话先后改过，
       只给 path 的话，退回哪一次的账根本对不上。 */
    changes: (cid, includeDead) => get('/api/changes'
      + (cid ? '?cid=' + encodeURIComponent(cid) : '')
      + (includeDead === false ? (cid ? '&' : '?') + 'include_dead=0' : '')),
    changesSummary: (cid) => get('/api/changes/summary'
      + (cid ? '?cid=' + encodeURIComponent(cid) : '')),
    /* 三级下钻：对话 → 产物 → 阶段。三个接口各管一层，
       界面只负责把返回的东西按层摆开，不再自己拼归属关系。 */
    changeThreads: (includeDead) => get('/api/changes/threads'
      + (includeDead === false ? '?include_dead=0' : '')),
    changeThread: (cid) => get('/api/changes/thread?cid=' + encodeURIComponent(cid)),
    changeTimeline: (cid, path) => get('/api/changes/timeline?cid=' + encodeURIComponent(cid)
      + '&path=' + encodeURIComponent(path)),
    /* 走 req 而不是裸 fetch：409 的时候要能拿到后端那句「为什么退不了」。
       返回统一成 {ok, text, error} —— 纯文本接口混进 JSON 的返回形状里，
       上层迟早会拿 .raw 去当字符串用。 */
    diff: async (cid, path, stage) => {
      const res = await req('GET', '/api/changes/diff?cid=' + encodeURIComponent(cid)
        + '&path=' + encodeURIComponent(path)
        + (stage === undefined || stage === null ? '' : '&stage=' + encodeURIComponent(stage)));
      if (!res.ok) return { ok: false, error: res.error };
      return { ok: true, text: (res.data && res.data.raw) || '' };
    },
    restoreStage: (cid, path, stage, force) =>
      post('/api/changes/restore', { cid, path, stage, force: !!force }),
    purgeChanges: (cid) => post('/api/changes/purge', cid ? { cid } : {}),
    revert: (cid, path, force) => post('/api/changes/revert', { cid, path, force: !!force }),

    /* 本机软件：清单来自开始菜单 + 注册表，open 走的是 agent 那条同一个启动通道 */
    software: (query, refresh) => get('/api/software'
      + '?query=' + encodeURIComponent(query || '')
      + (refresh ? '&refresh=1' : '')),
    softwareInstalled: () => get('/api/software/installed'),
    openSoftware: (name) => post('/api/software/open', { name }),

    /* 产物：这一条会话写进磁盘的东西。diff 故意不走上面那个 ——
       /api/changes/diff 每个文件只留最后一次改动，换个会话再改一遍就成了别人的。 */
    artifacts: (cid) => get(`/api/session/${cid}/artifacts`),
    artifactDiff: (cid, path) =>
      fetch(`/api/session/${cid}/artifacts/diff?path=${encodeURIComponent(path)}`).then((r) => r.text()),
    reveal: (path) => post('/api/reveal', { path }),

    /* 记忆：三层共用一个接口，靠 item 前缀区分 */
    memory: (project) => get('/api/memory' + (project ? '?project=' + encodeURIComponent(project) : '')),
    memoryRead: (item, project) => get('/api/memory/item?item=' + encodeURIComponent(item)
      + (project ? '&project=' + encodeURIComponent(project) : '')),
    memoryWrite: (payload) => post('/api/memory/item', payload),
    memoryDelete: (item, project) => del('/api/memory/item?item=' + encodeURIComponent(item)
      + (project ? '&project=' + encodeURIComponent(project) : '')),

    /* 专家：id 可能是中文，路径一定要 encode */
    experts: () => get('/api/experts'),
    saveExpert: (payload) => post('/api/experts', payload),
    deleteExpert: (id) => del('/api/experts/' + encodeURIComponent(id)),
    setExpert: (cid, expert, applyMode) => post(`/api/session/${cid}/expert`, {
      expert, apply_mode: applyMode !== false,
    }),

    checkpoints: () => get('/api/checkpoints'),
    restoreCheckpoint: (id) => post('/api/checkpoints/restore', { id }),

    /* 助理：id 是 ascii 的，但仍然 encode —— 万一以后换成中文名当 id 也不会炸 */
    assistants: () => get('/api/assistants'),
    createAssistant: (payload) => post('/api/assistants', payload),
    saveAssistant: (aid, payload) => patch('/api/assistants/' + encodeURIComponent(aid), payload),
    deleteAssistant: (aid) => del('/api/assistants/' + encodeURIComponent(aid)),
    assistantStats: (aid) => get('/api/assistants/' + encodeURIComponent(aid) + '/stats'),

    groups: () => get('/api/groups'),
    createGroup: (payload) => post('/api/groups', payload),
    saveGroup: (gid, payload) => patch('/api/groups/' + encodeURIComponent(gid), payload),
    deleteGroup: (gid) => del('/api/groups/' + encodeURIComponent(gid)),

    /* 安全：底线规则 + 审计链 + 出站打码 */
    security: () => get('/api/security'),
    setDlp: (enabled) => post('/api/security/dlp', { enabled }),
    addRule: (payload) => post('/api/security/rules', payload),
    delRule: (id) => del('/api/security/rules/' + encodeURIComponent(id)),
    clearAudit: () => post('/api/security/audit/clear'),

    /* 运行时与通道 */
    runtime: () => get('/api/runtime'),
    redetect: () => post('/api/runtime/detect'),
    setRuntimePath: (name, path) => post('/api/runtime/path', { name, path }),
    channels: () => get('/api/channels'),
    openApi: (enabled) => post('/api/channels/api', { enabled }),
    rotateToken: () => post('/api/channels/token'),

    browse: (path) => get('/api/fs' + (path ? '?path=' + encodeURIComponent(path) : '')),
    mkdir: (parent, name) => post('/api/fs/mkdir', { parent, name }),
    /* 弹系统自带的选文件夹对话框。它会一直等到用户选完 ——
       所以这个请求可能挂几十秒，这是正常的，别设超时。
       */
    pickFolder: (title) => post('/api/fs/pick-folder' +
      (title ? '?title=' + encodeURIComponent(title) : ''), {}),
    /* 同一个系统对话框，选的是文件。images=1 只列常见图片格式。 */
    pickFile: (title, images) => post('/api/fs/pick-file'
      + '?title=' + encodeURIComponent(title || '选一个文件')
      + (images ? '&images=1' : ''), {}),
    /* 把目录移进回收站。整个程序里只有「删除工作区」会用，护栏在服务端。 */
    trashWorkspace: (path) => post('/api/workspace/trash', { path }),

    /* 提示词库（收藏）。只存文本，没有变量也没有版本 —— 那些在优化浮层里。 */
    prompts: () => get('/api/prompts'),
    savePrompt: (payload) => post('/api/prompts', payload || {}),
    patchPrompt: (id, payload) => patch('/api/prompts/' + encodeURIComponent(id), payload || {}),
    delPrompt: (id) => del('/api/prompts/' + encodeURIComponent(id)),

    /* 加 / 删 / 改档 / 清空「额外授权目录」。
       op = add | remove | mode | clear；add/mode 要带 path，add/mode 还要 mode。 */
    updateGrants: (cid, body) => post('/api/session/' + cid + '/grants', body || {}),

    /* SSE：返回一个可关闭的句柄。断线自动重连，重连后服务端会补发历史。 */
    stream(cid, onMessage, onStatus) {
      const source = new EventSource(`/api/session/${cid}/stream`);
      source.onopen = () => onStatus && onStatus(true);
      source.onerror = () => onStatus && onStatus(false);
      source.onmessage = (event) => {
        try {
          onMessage(JSON.parse(event.data));
        } catch (err) {
          console.warn('SSE 数据解析失败', err);
        }
      };
      return { close: () => source.close() };
    },
  };
})();
