// REQ-20261003-098：单源 AI 短视频拆条 6 阶段管线（前端）
(function() {
  if (window.__slirnShortVideoBound) return;
  window.__slirnShortVideoBound = true;

  var API = '/slirn/api';

  function postJSON(url, payload) {
    return fetch(url, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      credentials: 'same-origin',
      body: JSON.stringify(payload || {})
    }).then(function(r) {
      return r.json().catch(function() {
        return {ok: false, error: 'HTTP ' + r.status};
      });
    }).catch(function(e) {
      return {ok: false, error: String(e)};
    });
  }

  function postForm(url, formData) {
    return fetch(url, {
      method: 'POST',
      credentials: 'same-origin',
      body: formData
    }).then(function(r) {
      return r.json().catch(function() {
        return {ok: false, error: 'HTTP ' + r.status};
      });
    }).catch(function(e) {
      return {ok: false, error: String(e)};
    });
  }

  function root() { return document.getElementById('slirn-tab-short-video'); }

  function setHTML(html) {
    var r = root();
    if (!r) return;
    r.innerHTML = html || '<div class="slirn-empty">加载失败</div>';
  }

  function toast(msg, type) {
    if (window.slirnToast) return window.slirnToast(msg, type || 'success');
    console.log(msg);
  }

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  // ---------- 阶段已用时 ticker（REQ-20261004-UX） ----------
  // 每个 [data-stage-elapsed] 元素带 data-started-at（ISO）。每秒刷新一次文本。
  // 用 setInterval（不是 setTimeout 链，参见 REQ-093 经验）。
  function _fmtElapsed(sec) {
    if (sec < 0 || !isFinite(sec)) return '0:00';
    var h = Math.floor(sec / 3600);
    var m = Math.floor((sec % 3600) / 60);
    var s = Math.floor(sec % 60);
    if (h > 0) return h + ':' + (m < 10 ? '0' + m : m) + ':' + (s < 10 ? '0' + s : s);
    return m + ':' + (s < 10 ? '0' + s : s);
  }
  function _parseIsoSafe(s) {
    if (!s) return 0;
    var t = Date.parse(s);
    return isNaN(t) ? 0 : t;
  }
  function _tickElapsed() {
    var nodes = document.querySelectorAll('[data-stage-elapsed]');
    if (!nodes.length) return;
    var now = Date.now();
    nodes.forEach(function(el) {
      var started = _parseIsoSafe(el.getAttribute('data-started-at'));
      if (!started) { el.textContent = '0:00'; return; }
      el.textContent = _fmtElapsed((now - started) / 1000);
    });
  }
  function startElapsedTicker() {
    if (window.__slirnElapsedTicker) return;
    _tickElapsed();  // 先跑一次，避免空白 1 秒
    window.__slirnElapsedTicker = setInterval(_tickElapsed, 1000);
  }
  function stopElapsedTickerIfIdle() {
    if (!window.__slirnElapsedTicker) return;
    if (!document.querySelector('[data-stage-elapsed]')) {
      clearInterval(window.__slirnElapsedTicker);
      window.__slirnElapsedTicker = null;
    }
  }

  function innerCtx() {
    var inner = document.getElementById('slirn-short-video-inner');
    if (!inner) return null;
    return {
      taskId: inner.getAttribute('data-task-id') || '',
      projectId: inner.getAttribute('data-project-id') || ''
    };
  }

  // ---------- 列表 / 创建 / 打开 / 删除 ----------

  // 刷新保持当前页面（2026-10-12 修订）：记住短视频 tab 里最后打开的项目；
  // 「返回项目列表」/删除项目会清空记忆。router.js 刷新后恢复短视频 tab 时，
  // slirnShortVideoLoad 会按记忆直接重进项目编辑页而不是列表。
  var SV_VIEW_KEY = 'slirn.sv.view';
  function _saveSvView(taskId, projectId) {
    try {
      localStorage.setItem(SV_VIEW_KEY,
        JSON.stringify({t: taskId || '', p: projectId || ''}));
    } catch (e) { /* localStorage 不可用 → 仅失去记忆功能 */ }
  }
  function _readSvView() {
    try { return JSON.parse(localStorage.getItem(SV_VIEW_KEY) || 'null'); }
    catch (e) { return null; }
  }

  // ===== REQ-20261022-144：执行日志（短视频页）=====
  var svLogsState = {
    kinds: [], statuses: [], auto: 'any', keyword: '',
    page: 1, pageSize: 20,
  };

  function _svLogsList() { return document.getElementById('slirn-sv-logs-list'); }

  function _svLogsReadFilters() {
    svLogsState.kinds = [];
    svLogsState.statuses = [];
    document.querySelectorAll('.slirn-chip[data-sv-log-kind].active').forEach(function (b) {
      svLogsState.kinds.push(b.getAttribute('data-sv-log-kind') || '');
    });
    document.querySelectorAll('.slirn-chip[data-sv-log-status].active').forEach(function (b) {
      svLogsState.statuses.push(b.getAttribute('data-sv-log-status') || '');
    });
    var kw = document.querySelector('[data-sv-logs-keyword]');
    svLogsState.keyword = kw ? (kw.value || '') : '';
  }

  function _svLogsRenderItem(it) {
    var status = it.status || '';
    var badge = status === 'success' ? '✅ 成功'
              : status === 'failed' ? '❌ 失败'
              : status === 'running' ? '⏳ 运行中' : '· ' + status;
    var kindLabel = it.description || it.kind || '';
    var dur = (it.duration_ms != null && it.duration_ms !== undefined)
      ? ' · 耗时 ' + (it.duration_ms / 1000).toFixed(1) + 's' : '';
    var autoTag = it.auto ? ' · ⚙ 自动' : '';
    var err = it.error
      ? '<div class="slirn-detail-sub" style="color:#ff8d8d;">❌ ' + esc(it.error) + '</div>' : '';
    return '<div class="slirn-sv-recent-item" style="cursor:default;">'
      + '<div class="slirn-sv-recent-info">'
      + '<div style="font-size:13px;">' + badge + ' · ' + esc(kindLabel) + dur + autoTag + '</div>'
      + '<div class="slirn-detail-sub">' + esc(it.started_at_iso || '') + '</div>'
      + err
      + '</div></div>';
  }

  function _svLogsRenderPager(r) {
    var pager = document.getElementById('slirn-sv-logs-pager');
    if (!pager) return;
    var total = r.total || 0;
    var pageSize = r.page_size || svLogsState.pageSize;
    var totalPages = r.total_pages || Math.max(1, Math.ceil(total / pageSize));
    var page = r.page || 1;
    svLogsState.page = page;
    svLogsState.pageSize = pageSize;
    pager.setAttribute('data-page', String(page));
    pager.setAttribute('data-page-size', String(pageSize));
    pager.setAttribute('data-total', String(total));
    var pe = pager.querySelector('[data-bind="page"]');
    var tpe = pager.querySelector('[data-bind="total-pages"]');
    var te = pager.querySelector('[data-bind="total"]');
    if (pe) pe.textContent = String(page);
    if (tpe) tpe.textContent = String(totalPages);
    if (te) te.textContent = String(total);
    pager.querySelectorAll('.slirn-pager-btn').forEach(function (b) {
      var act = b.getAttribute('data-sv-pager');
      var dis = (act === 'first' || act === 'prev') ? page <= 1
              : (act === 'next' || act === 'last') ? page >= totalPages : false;
      b.disabled = !!dis;
    });
  }

  function svLoadLogs() {
    var list = _svLogsList();
    if (!list) return;
    var tid = list.getAttribute('data-task-id') || '';
    if (!tid) return;
    _svLogsReadFilters();
    var countEl = document.querySelector('[data-sv-logs-count]');
    if (countEl) countEl.textContent = '加载中…';
    postJSON(API + '/list_logs', {
      task_id: tid,
      kinds: svLogsState.kinds,
      statuses: svLogsState.statuses,
      keyword: svLogsState.keyword,
      auto: svLogsState.auto || 'any',
      // REQ-20261028-NNN：长/短视频日志分流 — 短视频页只看短视频记录
      flow: 'short',
      offset: (Math.max(1, svLogsState.page) - 1) * svLogsState.pageSize,
      limit: svLogsState.pageSize,
    }).then(function (r) {
      if (!r || !r.ok) {
        if (countEl) countEl.textContent = '加载失败';
        list.innerHTML = '<div class="slirn-form-hint slirn-logs-empty">加载失败：'
          + esc((r && r.error) || '未知错误') + '</div>';
        return;
      }
      var items = r.items || [];
      list.innerHTML = items.length
        ? items.map(_svLogsRenderItem).join('')
        : '<div class="slirn-form-hint slirn-logs-empty">没有符合条件的日志记录</div>';
      _svLogsRenderPager(r);
      if (countEl) countEl.textContent = (r.total || 0) + ' 条';
    }).catch(function (err) {
      if (countEl) countEl.textContent = '加载失败';
      list.innerHTML = '<div class="slirn-form-hint slirn-logs-empty">加载失败：'
        + esc(String(err)) + '</div>';
    });
  }

  function svOpenLogs() {
    switchSvStage('logs');
    svLogsState.page = 1;
    svLoadLogs();
  }

  function switchSvStage(stageKey) {
    var root = document.getElementById('slirn-short-video-inner');
    if (!root || !stageKey) return;
    root.querySelectorAll('.slirn-sv-stage-nav').forEach(function(el) {
      var active = el.getAttribute('data-stage-key') === stageKey;
      el.classList.toggle('active', active);
      el.setAttribute('aria-current', active ? 'step' : 'false');
    });
    root.querySelectorAll('.slirn-sv-stage-pane').forEach(function(el) {
      el.classList.toggle('active', el.getAttribute('data-stage-key') === stageKey);
    });
    // REQ-20261013-116：用户主动点 tab → 记下当前阶段，openProject 重渲染后
    // 按此恢复（不依赖 server focus_key 自动算到「第一个未 done 的阶段」）。
    try { localStorage.setItem(SV_STAGE_KEY, stageKey); } catch (e) {}
  }

  // REQ-20261013-116：短视频同步长视频「完成后自动打开下一阶段界面」开关
  // （参考 REQ-20260918-046 long video 实现 .slirn-wb-autonext）。
  // - 开关默认关：保持 server focus_key 自动跳下阶段的原行为
  // - 用户勾选：当前焦点阶段完成时自动切下一阶段 + toast
  // - 用户取消勾选：当前阶段完成时保持在原页面（用户手动切）
  var SV_STAGE_KEY = 'slirn.sv.currentStage';
  var SV_AUTONEXT_KEY = 'slirn.sv.autonext';

  function _svAutoNextOn() {
    try { return localStorage.getItem(SV_AUTONEXT_KEY) === '1'; }
    catch (err) { return false; }
  }
  function _saveSvAutoNext(on) {
    try { localStorage.setItem(SV_AUTONEXT_KEY, on ? '1' : ''); }
    catch (err) {}
  }
  function _readSvStage() {
    try { return localStorage.getItem(SV_STAGE_KEY) || ''; }
    catch (err) { return ''; }
  }
  function applySvAutoNextState() {
    var el = document.getElementById('slirn-sv-autonext');
    if (el) el.checked = _svAutoNextOn();
  }
  // 改 checkbox → 写 localStorage + toast 提示
  document.addEventListener('change', function(e) {
    if (e.target && e.target.id === 'slirn-sv-autonext') {
      _saveSvAutoNext(!!e.target.checked);
      toast(e.target.checked
        ? '✅ 已开启：当前阶段完成后自动打开下一阶段界面'
        : '↩️ 已关闭：阶段完成后保持在原页面（需手动切下一阶段）');
    }
  });
  // openProject 渲染后调用：对比 prevDone 快照，若「当前焦点阶段刚完成 + 开关开
  // + 没到底」则切下一阶段。toast 同步长视频风格。
  function svAutoNextMaybe(prevDone, hadSv, prevActive) {
    if (!hadSv || !_svAutoNextOn() || !prevActive) return;
    var stages = [];
    document.querySelectorAll('#slirn-short-video-inner .slirn-sv-stage-nav').forEach(function(s) {
      stages.push(s.getAttribute('data-stage-key') || '');
    });
    var justDone = '';
    stages.forEach(function(pk) {
      var nav = document.querySelector(
        '#slirn-short-video-inner .slirn-sv-stage-nav[data-stage-key="' + pk + '"]');
      if (nav && nav.classList.contains('done') && !prevDone[pk] && pk === prevActive) {
        justDone = pk;
      }
    });
    if (!justDone) return;
    var idx = stages.indexOf(justDone);
    if (idx < 0 || idx + 1 >= stages.length) return;  // 最后一个阶段完成 → 不切
    switchSvStage(stages[idx + 1]);
    toast('✅ 「' + justDone + '」完成 — 已自动打开下一阶段界面'
      + '（可关掉阶段上方的「完成后自动打开下一阶段界面」开关改为手动切）');
  }

  function loadList(optScope) {
    // REQ-20261012-099 修订四：scope（我的任务/全部任务，仅 admin 有 radio）。
    // 不传时读当前选中 radio；都没有 → 交给后端按登录态决定。
    var scope = optScope || '';
    if (!scope) {
      var sR = document.querySelector('input[name="slirn-sv-scope"]:checked');
      if (sR) scope = sR.value;
    }
    _saveSvView('', '');
    postJSON(API + '/short_video_list', {scope: scope || 'all'}).then(function(r) {
      if (r && r.ok) setHTML(r.html);
      else setHTML('<div class="slirn-empty">' + esc((r && r.error) || '加载失败') + '</div>');
      stopElapsedTickerIfIdle();
    });
  }
  window.slirnShortVideoLoad = function(forceList) {
    if (forceList) return loadList();
    var v = _readSvView();
    if (v && v.t && v.p) openProject(v.t, v.p);
    else loadList();
  };

  function openProject(taskId, projectId) {
    _saveSvView(taskId, projectId);
    // REQ-20261013-116：渲染前快照（哪些阶段已完成 + 用户当前所在阶段）——
    // 渲染后对比实现「当前阶段刚完成 → 按开关决定是否切下一阶段」。
    var prevDone = {}, prevActive = '';
    var hadSv = !!document.querySelector('#slirn-short-video-inner .slirn-sv-stage-nav');
    if (hadSv) {
      document.querySelectorAll('#slirn-short-video-inner .slirn-sv-stage-nav.done').forEach(function(s) {
        prevDone[s.getAttribute('data-stage-key') || ''] = 1;
      });
      document.querySelectorAll('#slirn-short-video-inner .slirn-sv-stage-pane').forEach(function(p) {
        if (p.classList.contains('active')) {
          prevActive = p.getAttribute('data-stage-key') || '';
        }
      });
    }
    postJSON(API + '/short_video_get', {task_id: taskId, project_id: projectId}).then(function(r) {
      if (r && r.ok) setHTML(r.html);
      else {
        // 打不开（项目被删 / 无权限 / 会话过期）→ 清记忆并回列表，
        // 避免刷新后反复卡在打不开的视图上
        _saveSvView('', '');
        toast((r && r.error) || '打开失败', 'error');
        loadList();
        return;
      }
      startElapsedTicker();
      initSubtitleSortAll();
      initStage4SortAll(); // REQ-20261019-210：Stage 4 字幕行拖拽排序
      // 修订九：restore/rerun/删除/拼接都走 openProject 重渲染 —— 渲染完
      // 恢复用户上次选中的短视频页签（该条已删则回默认第一条）
      _restoreHlTab(taskId, projectId);
      // REQ-20261018-191：恢复 上传素材/背景图区域检测 折叠状态（最后
      // 操作是收起 → 重渲染后仍收起）
      _restoreCollapsePrefs();
      // REQ-20261013-116：① 恢复「自动打开下一阶段」开关状态 ② 按开关决定是否
      // 切下一阶段（只在「当前焦点阶段刚完成」时触发；只是首次进入或手动点
      // tab 不会触发）。最后用用户记忆的 stage 覆盖 server focus_key 默认
      // 焦点（避免 server 把焦点算到「第一个未 done 阶段」抢走用户当前页）。
      applySvAutoNextState();
      svAutoNextMaybe(prevDone, hadSv, prevActive);
      var saved = _readSvStage();
      if (saved) {
        // 校验：saved 必须是 server 渲染出来的 6 个 stage 之一
        var exists = document.querySelector(
          '#slirn-short-video-inner .slirn-sv-stage-pane[data-stage-key="' + saved + '"]');
        if (exists) switchSvStage(saved);
      }
      // REQ-20261019-222：Stage 4 行编辑保存后的滚动锚点恢复（放在页签/
      // 焦点恢复之后 — display:none 的 pane 里 offsetTop 全是 0）
      _svStage4ApplyScrollAnchor(taskId, projectId);
      // REQ-20261022-143：挂载「流程配置」面板（读配置 + 拉一次状态）
      if (window.slirnShortVideoFlowMount) {
        try { window.slirnShortVideoFlowMount(); } catch (e) {}
      }
    });
  }

  // ---------- 修订九：Stage 3 多条短视频逐条页签 ----------

  function _svProjKey() {
    var el = document.getElementById('slirn-short-video-inner');
    if (!el) return '';
    return (el.getAttribute('data-task-id') || '') + '/' +
           (el.getAttribute('data-project-id') || '');
  }

  function switchHlTab(idx) {
    var want = String(idx || '');
    var hit = false;
    Array.prototype.forEach.call(
      document.querySelectorAll('.slirn-stage3-hl-tab, .slirn-stage4-hl-tab'),
      function(t) {
        var on = String(t.getAttribute('data-hl-index') || '') === want;
        if (on) hit = true;
        t.classList.toggle('active', on);
      });
    Array.prototype.forEach.call(
      document.querySelectorAll('.slirn-stage3-hl-pane, .slirn-stage4-hl-pane'),
      function(p) {
        p.classList.toggle('active', String(p.getAttribute('data-hl-index') || '') === want);
      });
    // 只在确实命中时记忆（连同项目 key，换项目不串页签）
    if (hit) window._slirnSvHlTab = {k: _svProjKey(), hl: want};
  }

  function _restoreHlTab(taskId, projectId) {
    var mem = window._slirnSvHlTab;
    if (!mem || !mem.hl) return;
    if (mem.k !== String(taskId) + '/' + String(projectId)) return;
    var tab = document.querySelector(
      '.slirn-stage3-hl-tab[data-hl-index="' + mem.hl + '"], ' +
      '.slirn-stage4-hl-tab[data-hl-index="' + mem.hl + '"]');
    if (tab) switchHlTab(mem.hl);
    else window._slirnSvHlTab = null;   // 该条已删除 → 回默认第一条
  }

  // ---------- REQ-20261018-191：折叠状态记忆（上传素材 / 背景图区域检测） ----------
  // server 默认渲染展开；用户收起过则记到 localStorage，openProject 每次重渲染
  // 后恢复到最后操作状态（跨刷新）。只认 data-collapse-key 标记的 details，
  // 不影响其它 <details>。toggle 老引擎不冒泡 → capture 兜底。
  var SV_COLLAPSE_PREFIX = 'slirnCollapse:';

  function _restoreCollapsePrefs() {
    Array.prototype.forEach.call(
      document.querySelectorAll('details[data-collapse-key]'),
      function(d) {
        var v = null;
        try {
          v = localStorage.getItem(SV_COLLAPSE_PREFIX + d.getAttribute('data-collapse-key'));
        } catch (e) { /* localStorage 不可用 → 仅失去记忆功能 */ }
        // 只有存过才覆盖 server 默认（新用户零干扰）；恢复写回同值，幂等
        if (v === '0' || v === '1') d.open = v === '1';
      });
  }

  document.addEventListener('toggle', function(e) {
    var k = e.target && e.target.getAttribute &&
            e.target.getAttribute('data-collapse-key');
    if (!k) return;
    try {
      localStorage.setItem(SV_COLLAPSE_PREFIX + k, e.target.open ? '1' : '0');
    } catch (err) { /* 同上：记忆失效不崩 */ }
  }, true);

  function createProject() {
    var taskId = (document.getElementById('slirn-sv-create-task') || {}).value || '';
    var name = (document.getElementById('slirn-sv-create-name') || {}).value || '';
    if (!taskId) return toast('请选择来源任务', 'error');
    postJSON(API + '/short_video_create', {task_id: taskId, name: name}).then(function(r) {
      if (r && r.ok) {
        toast('项目已创建');
        openProject(taskId, r.project.id);
      } else toast((r && r.error) || '创建失败', 'error');
    });
  }

  function deleteProject(btn) {
    var taskId = btn.getAttribute('data-task-id') || '';
    var projectId = btn.getAttribute('data-project-id') || '';
    if (!window.confirm('确认删除该项目及其所有短视频产物？')) return;
    postJSON(API + '/short_video_delete', {task_id: taskId, project_id: projectId}).then(function(r) {
      if (r && r.ok) loadList();
      else toast((r && r.error) || '删除失败', 'error');
    });
  }

  // ---------- 上传素材（仍可用） ----------

  function uploadMaterial() {
    var ctx = innerCtx(); if (!ctx) return;
    var fileEl = document.getElementById('slirn-sv-upload-file');
    var file = fileEl && fileEl.files && fileEl.files[0];
    if (!file) return toast('请选择素材文件', 'error');
    var fd = new FormData();
    fd.append('task_id', ctx.taskId);
    fd.append('project_id', ctx.projectId);
    fd.append('kind', 'video');
    fd.append('file', file);
    toast('上传中…');
    postForm(API + '/short_video_upload_material', fd).then(function(r) {
      if (r && r.ok) { toast('素材已上传'); openProject(ctx.taskId, ctx.projectId); }
      else toast((r && r.error) || '上传失败', 'error');
    });
  }

  // ---------- 6 阶段管线 ----------

  function withCtx(action, run) {
    var ctx = innerCtx();
    if (!ctx) { toast('页面未打开', 'error'); return; }
    run(ctx);
    return ctx;
  }

  function stage1Select() {
    var ctx = innerCtx(); if (!ctx) return;
    var sel = document.getElementById('slirn-stage1-source');
    var mat = sel ? sel.value : '';
    if (!mat) return toast('请先选一个视频素材', 'error');
    toast('正在选源…');
    postJSON(API + '/short_video_select_source', {
      task_id: ctx.taskId, project_id: ctx.projectId, material_id: mat
    }).then(function(r) {
      if (r && r.ok) {
        toast('已选源');
        if (r.html) setHTML(r.html);
        else openProject(ctx.taskId, ctx.projectId);
      } else toast((r && r.error) || '选源失败', 'error');
    });
  }

  function stage2Run(btn) {
    var ctx = innerCtx(); if (!ctx) return;
    if (btn && btn.disabled) return;
    // REQ-20261016-125：防重入双保险（闭包标志 + 按钮 disabled），
    // 提取是 5-30 分钟的阻塞调用，必须能看见进度且不能误点第二次。
    if (window._slirnSvStage2Running) {
      toast('字幕提取正在执行中，请勿重复点击', 'warn'); return;
    }
    window._slirnSvStage2Running = true;

    // REQ-20261015-122：模型/语言不再由用户选择，固定 paraformer-zh（后端默认值）。
    var hotwords = (document.getElementById('slirn-stage2-hotwords') || {}).value || '';
    var status = document.getElementById('slirn-sv-stage2-status');
    var msg = status ? status.querySelector('.slirn-sv-stage2-msg') : null;
    var bar = status ? status.querySelector('.slirn-sv-stage2-bar') : null;
    var setBtn = function(txt, dis) { if (btn) { btn.textContent = txt; btn.disabled = !!dis; } };
    var show = function(t, pct) {
      if (status) status.style.display = '';
      if (msg) { msg.style.display = ''; msg.textContent = t; }
      if (bar && pct != null) bar.style.width = pct + '%';
    };
    var t0 = Date.now();
    var elapsedTxt = function() { return '已耗时 ' + _fmtElapsed((Date.now() - t0) / 1000); };

    setBtn('🎧 提取中…', true);
    show('⏳ 启动 funasr 模型（约 10-30 秒）… · ' + elapsedTxt(), 4);
    toast('funasr 字幕提取中…（短视频一般 10-60 秒，进度见下方状态卡）');

    // 阶段文字 + 真实已耗时（1s 节奏）。识别是大头且时长随视频长度浮动，
    // 进度条按已耗时缓慢爬升、90% 封顶等后端响应，完成时一次性跳 100%。
    var timer = setInterval(function() {
      var sec = (Date.now() - t0) / 1000;
      if (sec < 20) show('⏳ 加载 funasr 模型… · ' + elapsedTxt(), Math.min(8, 4 + sec * 0.2));
      else if (sec < 60) show('⏳ 提取音频（ffmpeg 单声道 16kHz）… · ' + elapsedTxt(), 10 + (sec - 20) * 0.3);
      else show('⏳ 语音识别中（最耗时一步，视频越长越久）… · ' + elapsedTxt(), 20 + Math.min(70, (sec - 60) / 60 * 7));
    }, 1000);

    postJSON(API + '/short_video_stage2_run', {
      task_id: ctx.taskId, project_id: ctx.projectId,
      hotwords: hotwords, sd_switch: 'no'
    }).then(function(r) {
      clearInterval(timer);
      var sec = Math.round((Date.now() - t0) / 1000);
      if (r && r.ok) {
        var st = r.stage2 || {};
        var chars = st.srt_chars || 0;
        if (bar) { bar.style.width = '100%'; bar.classList.add('slirn-progress-bar--done'); }
        show('✅ 字幕提取完成：' + chars + ' 字 · 总耗时 ' + _fmtElapsed(sec), 100);
        setBtn('字幕提取', false);
        toast('Stage 2 提取完成（' + _fmtElapsed(sec) + '）');
        openProject(ctx.taskId, ctx.projectId);
      } else {
        var e = (r && ((r.stage2 && r.stage2.error) || r.error)) || 'Stage 2 提取失败';
        if (bar) { bar.style.width = '100%'; bar.classList.add('slirn-progress-bar--failed'); }
        show('❌ ' + e + ' · 总耗时 ' + _fmtElapsed(sec), 100);
        setBtn('字幕提取', false);
        toast(e, 'error');
      }
    }).catch(function(err) {
      clearInterval(timer);
      if (bar) { bar.style.width = '100%'; bar.classList.add('slirn-progress-bar--failed'); }
      show('❌ 网络错误：' + (err && err.message || err), 100);
      setBtn('提取字幕', false);
      toast('网络错误：' + (err && err.message || err), 'error');
    }).then(function() {
      window._slirnSvStage2Running = false;
    });
  }

  // ---------- REQ-20261012-099：Stage 3 解析短视频内容 ----------

  function _stage3PromptText() {
    var ta = document.getElementById('slirn-stage3-prompt');
    return ta ? String(ta.value || '').trim() : '';
  }

  function _stage3UseImage() {
    var cb = document.getElementById('slirn-stage3-use-image');
    // 无复选框（没上传过图）→ false；有则按勾选
    return !!(cb && cb.checked);
  }

  // REQ-20261015-120：读时长区间输入（[下限, 上限] 秒）；输入框缺失/非法
  // → null（后端回退到上次运行区间或默认 60-90）。只做最基本的反转保护，
  // 完整钳制由后端 _norm_duration_range 兜底。
  function _stage3DurationRange() {
    var mn = document.getElementById('slirn-stage3-dmin');
    var mx = document.getElementById('slirn-stage3-dmax');
    if (!mn || !mx) return [null, null];
    var a = parseFloat(mn.value), b = parseFloat(mx.value);
    if (isNaN(a) || isNaN(b)) return [null, null];
    if (a > b) { var t = a; a = b; b = t; }
    return [a, b];
  }

  // REQ-20261015-121：系统提示词模板按当前三个输入框的值渲染。
  // 占位符与后端 stage3_system_prompt() 同一套（{n}/{dmin}/{dmax}），
  // 公式双端一致：值取整展示（与后端 {dmin:.0f} 口径一致）。
  // REQ-20261016-127：去掉「行数 × 平均 2s」折算类占位符
  // （{dmin2}/{dmax2}/{ex_sec}/{ex_lines}）。
  function _stage3RenderSystemPrompt(tmpl) {
    var dur = _stage3DurationRange();
    var dmin = dur[0], dmax = dur[1];
    if (!dmin || !dmax) { dmin = 60; dmax = 90; }
    if (dmax < dmin) { var t = dmin; dmin = dmax; dmax = t; }
    var nEl = document.getElementById('slirn-stage3-n');
    var n = parseInt(nEl ? nEl.value : '', 10);
    if (!n || n < 1) n = 4;
    dmin = Math.round(dmin); dmax = Math.round(dmax);
    var vals = { n: n, dmin: dmin, dmax: dmax };
    var out = tmpl;
    Object.keys(vals).forEach(function(k) {
      out = out.split('{' + k + '}').join(String(vals[k]));
    });
    return out;
  }

  // REQ-20261013-111：Stage 3 解析 — 进度卡 + 防重入 + 阶段文字式自估算
  function stage3Run(btn) {
    var ctx = innerCtx(); if (!ctx) return;
    // 防重入：闭包标志 + 按钮 disabled 双保险
    if (btn && btn.disabled) return;
    if (window._slirnSvStage3Running) {
      toast('解析正在执行中，请勿重复点击', 'warn'); return;
    }
    window._slirnSvStage3Running = true;
    // REQ-099 修订：快速模板 UI 已移除，template 固定默认值（仅作后端兜底链）
    var template = 'hook_first';
    var n = parseInt((document.getElementById('slirn-stage3-n') || {}).value || '4', 10);
    // REQ-20261015-120：时长区间（秒）直接取用户设置，后端拼提示词并按此核查
    var dur = _stage3DurationRange();
    var prompt = _stage3PromptText();
    var useImage = _stage3UseImage();

    var status = document.getElementById('slirn-sv-stage3-status');
    var msg = status ? status.querySelector('.slirn-sv-stage3-msg') : null;
    var bar = status ? status.querySelector('.slirn-sv-stage3-bar') : null;
    var setBtn = function(txt, dis) {
      if (btn) { btn.textContent = txt; btn.disabled = !!dis; }
    };
    var show = function(t, pct) {
      if (status) status.style.display = '';
      if (msg) { msg.style.display = ''; msg.textContent = t; }
      if (bar && pct != null) bar.style.width = pct + '%';
    };

    setBtn('🔍 解析中…', true);
    show('⏳ 正在启动解析（请稍候，单次多模态大模型调用可能 10s-3min）…', 5);
    toast('内容提取中…（字幕' + (useImage ? ' + 截图' : '') + ' + 提示词发给 AI）');

    // 阶段进度（每 3s 推一档；后端单次调用耗时方差大，自估算保 UX 流畅）
    var stages = [
      { pct: 15, txt: '⏳ 准备字幕 + 提示词…' },
      { pct: 35, txt: useImage ? '⏳ OCR 解析截图…' : '⏳ 构造请求载荷…' },
      { pct: 60, txt: '⏳ 调用大模型（单次多模态，可能耗时较久）…' },
      { pct: 85, txt: '⏳ 解析模型响应（分组 + 校验）…' }
    ];
    var stIdx = 0;
    var stageTimer = setInterval(function() {
      if (stIdx < stages.length) {
        show(stages[stIdx].txt, stages[stIdx].pct);
        stIdx++;
      } else {
        // 卡在 90% 等待响应
        show('⏳ 仍在等待大模型响应（请耐心，可能 1-3 分钟）…', 90);
      }
    }, 3000);

    postJSON(API + '/short_video_stage3_run', {
      task_id: ctx.taskId, project_id: ctx.projectId,
      template: template, n_clips: n,
      prompt: prompt, use_image: useImage,
      duration_min_sec: dur[0], duration_max_sec: dur[1]
    }).then(function(r) {
      clearInterval(stageTimer);
      if (r && r.ok) {
        if (bar) {
          bar.style.width = '100%';
          bar.classList.add('slirn-progress-bar--done');
        }
        var st = r.stage3 || {};
        var hlCount = (st.highlights || []).length || (st.count || 0);
        show('✅ 解析完成：共 ' + hlCount + ' 条短视频片段', 100);
        setBtn('🔍 解析', false);
        toast('解析完成');
        openProject(ctx.taskId, ctx.projectId);
      } else {
        if (bar) {
          bar.style.width = '100%';
          bar.classList.add('slirn-progress-bar--failed');
        }
        show('❌ ' + ((r && r.error) || 'Stage 3 解析失败'), 100);
        setBtn('🔍 解析', false);
        toast((r && r.error) || 'Stage 3 失败', 'error');
      }
    }).catch(function(err) {
      clearInterval(stageTimer);
      if (bar) {
        bar.style.width = '100%';
        bar.classList.add('slirn-progress-bar--failed');
      }
      show('❌ 网络错误：' + (err && err.message || err), 100);
      setBtn('🔍 解析', false);
      toast('网络错误：' + (err && err.message || err), 'error');
    }).then(function() {  // finally
      window._slirnSvStage3Running = false;
    });
  }

  function stage3RerunOne(btn) {
    var ctx = innerCtx(); if (!ctx) return;
    var hlIndex = parseInt(btn.getAttribute('data-hl-index') || '0', 10);
    if (!hlIndex) return toast('片段序号无效', 'error');
    var title = '';
    var row = btn.closest ? btn.closest('.slirn-stage3-hl-row') : null;
    var titleEl = row ? row.querySelector('.slirn-stage3-hl-title') : null;
    if (titleEl) title = titleEl.value || '';
    if (!window.confirm('针对「' + (title || '#' + hlIndex) + '」单独重新解析？\n'
        + '该片段的时间段与字幕内容将被替换，下游 Stage 4/5/6/7 重置。')) return;
    toast('片段 #' + hlIndex + ' 重新解析中…');
    // REQ-20261015-120：单条重解析同样带当前时长区间设置
    var dur = _stage3DurationRange();
    postJSON(API + '/short_video_stage3_rerun_one', {
      task_id: ctx.taskId, project_id: ctx.projectId,
      hl_index: hlIndex,
      prompt: _stage3PromptText(),
      use_image: _stage3UseImage(),
      duration_min_sec: dur[0], duration_max_sec: dur[1]
    }).then(function(r) {
      if (r && r.ok) { toast('片段 #' + hlIndex + ' 已更新'); openProject(ctx.taskId, ctx.projectId); }
      else toast((r && r.error) || '重新解析失败', 'error');
    });
  }

  // REQ-099 修订：保存默认 / 存入列表后，用服务端返回的 history_html 原地
  // 刷新历史下拉（不重渲染整页 —— 文本域里用户正在编辑的内容不能丢），
  // 并自动展开让其立即看到新条目与 ⭐ 默认标记。
  function _refreshStage3History(historyHtml) {
    if (!historyHtml) return;
    var list = document.querySelector('.slirn-stage3-hist-list');
    if (list) list.innerHTML = historyHtml;
    var box = document.querySelector('.slirn-stage3-prompts-section');
    if (box) box.open = true;
  }

  function stage3SaveDefault() {
    var prompt = _stage3PromptText();
    if (!prompt) return toast('提示词为空，无法设为默认', 'error');
    postJSON(API + '/short_video_save_default_prompt', { prompt: prompt })
      .then(function(r) {
        if (r && r.ok) {
          _refreshStage3History(r.history_html);
          toast('已设为默认提示词（列表中 ⭐ 标出）');
        } else toast((r && r.error) || '保存失败', 'error');
      });
  }

  function stage3SaveHistory() {
    var prompt = _stage3PromptText();
    if (!prompt) return toast('提示词为空，无法存入列表', 'error');
    _postPromptHistory(prompt, false);
  }

  function _postPromptHistory(prompt, confirmOverwrite) {
    postJSON(API + '/short_video_prompt_history_save', {
      prompt: prompt, confirm: !!confirmOverwrite
    }).then(function(r) {
      if (!r || !r.ok) return toast((r && r.error) || '保存失败', 'error');
      if (r.status === 'need_confirm') {
        // 已满 10 条：由用户决定是否覆盖最旧一条
        if (window.confirm('提示词列表已满 ' + (r.max || 10) + ' 条。\n'
            + '继续保存将覆盖最旧的一条，是否覆盖？')) {
          _postPromptHistory(prompt, true);
        } else {
          toast('已取消保存');
        }
        return;
      }
      _refreshStage3History(r.history_html);
      toast('已存入提示词列表');
    });
  }

  // 历史条目：点击展开 / 收起完整内容（与「载入」分开，先看再决定）
  function stage3ToggleHist(btn) {
    var item = btn.closest ? btn.closest('.slirn-stage3-hist-item') : null;
    var full = item ? item.querySelector('.slirn-stage3-hist-full') : null;
    if (full) full.hidden = !full.hidden;
  }

  function stage3LoadPrompt(btn) {
    var ta = document.getElementById('slirn-stage3-prompt');
    if (!ta) return;
    var text = btn.getAttribute('data-prompt') || '';
    if (!text) return;
    if (ta.value && ta.value.trim() && !window.confirm(
        '文本域已有内容，用这条历史提示词覆盖它？')) return;
    ta.value = text;
    toast('已载入历史提示词');
  }

  // 修订十：一键引用系统级提示词（内置在系统里，独立于默认/历史两级存储；
  // 填入文本域后可继续编辑，再按需「设为默认」——覆盖确认逻辑与「载入」一致）。
  // REQ-20261015-121：优先用 data-prompt-tmpl 模板 + 当前输入框值渲染
  // （片段数 / 每条时长上下限动态代入）；模板缺失时回退 data-prompt 默认全文。
  function stage3UseSystemPrompt(btn) {
    var ta = document.getElementById('slirn-stage3-prompt');
    if (!ta) return;
    var tmpl = btn.getAttribute('data-prompt-tmpl') || '';
    var text = tmpl ? _stage3RenderSystemPrompt(tmpl)
      : (btn.getAttribute('data-prompt') || '');
    if (!text) return;
    if (ta.value && ta.value.trim() && !window.confirm(
        '文本域已有内容，用系统提示词覆盖它？')) return;
    ta.value = text;
    toast('已引用系统提示词（条数 / 时长上下限按当前设置代入）');
  }

  function stage3ClearPrompt() {
    var ta = document.getElementById('slirn-stage3-prompt');
    if (!ta) return;
    if (ta.value && !window.confirm('清空当前提示词文本域？')) return;
    ta.value = '';
  }

  // REQ-20261016-128：从历史列表删除一条；若是默认则同步清空默认标记。
  function stage3DeletePrompt(btn) {
    if (!btn) return;
    var prompt = btn.getAttribute('data-prompt') || '';
    var stage = btn.getAttribute('data-stage') || 'stage3';
    if (!prompt) return toast('该条目无 prompt 文本', 'error');
    if (!window.confirm('从历史列表删除这条提示词？\n（若是默认将同时清除默认标记）')) return;
    postJSON(API + '/short_video_prompt_history_delete', {
      prompt: prompt, stage: stage
    }).then(function(r) {
      if (!r || !r.ok) return toast((r && r.error) || '删除失败', 'error');
      _refreshStage3History(r.history_html);
      toast(r.toast || '已删除');
    });
  }

  function stage3UploadImage() {
    var ctx = innerCtx(); if (!ctx) return;
    var fileEl = document.getElementById('slirn-stage3-image-file');
    var file = fileEl && fileEl.files && fileEl.files[0];
    if (!file) return toast('请先选择截图文件', 'error');
    var fd = new FormData();
    fd.append('task_id', ctx.taskId);
    fd.append('project_id', ctx.projectId);
    fd.append('file', file);
    toast('截图上传中…');
    postForm(API + '/short_video_stage3_upload_image', fd).then(function(r) {
      if (r && r.ok) { toast('截图已上传'); openProject(ctx.taskId, ctx.projectId); }
      else toast((r && r.error) || '上传失败', 'error');
    });
  }

  function stage3RemoveImage() {
    var ctx = innerCtx(); if (!ctx) return;
    if (!window.confirm('移除当前截图？之后解析将不再携带图片。')) return;
    postJSON(API + '/short_video_stage3_remove_image', {
      task_id: ctx.taskId, project_id: ctx.projectId
    }).then(function(r) {
      if (r && r.ok) { toast('截图已移除'); openProject(ctx.taskId, ctx.projectId); }
      else toast((r && r.error) || '移除失败', 'error');
    });
  }

  // REQ-20261013-111：Stage 4 重新优化 — 进度卡 + 防重入 + N/M 计数式自估算
  function stage4OptimizeRun(btn) {
    // REQ-20261013-100 + REQ-20261016-146：Stage 4 内容优化 — LLM 按标题逐条
    // 优化 Stage 3 字幕（按语义切分长行 / 修正与音频不符的文字 / 删除无用行），
    // 只用内置系统提示词（无自定义提示词），结果写 Stage 4 自己的
    // optimized_highlights.json（与 Stage 3 原表分离）；
    // 删除行保留展示（🗑 LLM已删）。完成后刷新页面。
    var ctx = innerCtx(); if (!ctx) return;
    // 防重入：闭包标志 + 按钮 disabled 双保险
    if (btn && btn.disabled) return;
    if (window._slirnSvStage4Running) {
      toast('内容优化正在执行中，请勿重复点击', 'warn'); return;
    }
    window._slirnSvStage4Running = true;

    // N = highlights.length（从 DOM tab 数量取，因为 innerCtx 不带 project）
    var total = document.querySelectorAll('.slirn-stage4-hl-tab').length || 1;

    var status = document.getElementById('slirn-sv-stage4-status');
    var msg = status ? status.querySelector('.slirn-sv-stage4-msg') : null;
    var bar = status ? status.querySelector('.slirn-sv-stage4-bar') : null;
    var setBtn = function(txt, dis) {
      if (btn) { btn.textContent = txt; btn.disabled = !!dis; }
    };
    var show = function(t) {
      if (status) status.style.display = '';
      if (msg) { msg.style.display = ''; msg.textContent = t; }
    };

    setBtn('✨ 重新优化中…', true);
    show('⏳ 正在启动内容优化…');
    toast('内容优化中（每条短视频调一次大模型）…');

    // 前端自估算进度（每 1500ms 递增 idx 1/2/3.../total）
    var idx = 0;
    var pollTimer = setInterval(function() {
      if (idx < total) idx++;
      var pct = Math.round(idx / total * 100);
      if (bar) bar.style.width = pct + '%';
      show('⏳ 正在优化第 ' + idx + '/' + total + ' 条字幕（每条约 5-30s，请耐心等待）');
    }, 1500);

    postJSON(API + '/short_video_stage4_optimize', {
      task_id: ctx.taskId, project_id: ctx.projectId
    }).then(function(r) {
      clearInterval(pollTimer);
      if (r && r.ok) {
        var st = r.stage4_optimize || {};
        var hls = st.highlights || [];
        var fixedTot = 0, splitTot = 0, delTot = 0;
        hls.forEach(function(h) {
          fixedTot += h.fixed || 0; splitTot += h.split || 0; delTot += h.deleted || 0;
        });
        if (bar) {
          bar.style.width = '100%';
          bar.classList.add('slirn-progress-bar--done');
        }
        show('✅ 内容优化完成：修改 ' + fixedTot + ' 行 · 切分 ' + splitTot
          + ' 行 · 删除 ' + delTot + ' 行');
        setBtn('✨ 重新优化', false);
        toast('内容优化完成：修改 ' + fixedTot + ' 行 · 切分 ' + splitTot
              + ' 行 · 删除 ' + delTot + ' 行');
        openProject(ctx.taskId, ctx.projectId);
      } else {
        if (bar) {
          bar.style.width = '100%';
          bar.classList.add('slirn-progress-bar--failed');
        }
        show('❌ ' + ((r && r.error) || 'Stage 4 内容优化失败'));
        setBtn('✨ 重新优化', false);
        toast((r && r.error) || 'Stage 4 内容优化失败', 'error');
      }
    }).catch(function(err) {
      clearInterval(pollTimer);
      if (bar) {
        bar.style.width = '100%';
        bar.classList.add('slirn-progress-bar--failed');
      }
      show('❌ 网络错误：' + (err && err.message || err));
      setBtn('✨ 重新优化', false);
      toast('网络错误：' + (err && err.message || err), 'error');
    }).then(function() {  // finally
      window._slirnSvStage4Running = false;
    });
  }

  // REQ-20261017-155：Stage 4 单条重新优化 — 只对该条从 Stage 3 原表重跑
  // LLM（其余条保留既有优化结果），粗剪及之后作废。与全量共用
  // _slirnSvStage4Running 防重入 + #slirn-sv-stage4-status 进度卡。
  function stage4OptimizeOne(btn) {
    var ctx = innerCtx(); if (!ctx) return;
    if (btn && btn.disabled) return;
    if (window._slirnSvStage4Running) {
      return toast('内容优化正在执行中，请勿重复点击', 'warn');
    }
    var hl = parseInt(btn.getAttribute('data-hl-index') || '0', 10);
    if (!hl) return toast('片段序号无效', 'error');
    var card = btn.closest ? btn.closest('.slirn-stage4-clip') : null;
    var titleEl = card ? card.querySelector('.slirn-stage4-clip-title') : null;
    var title = titleEl ? (titleEl.textContent || '').trim() : '';
    if (!window.confirm('对「' + (title || '#' + hl) + '」单独重新优化？\n'
        + '该条字幕将从 Stage 3 原表重新生成（其余条不变），粗剪及之后作废。')) return;
    window._slirnSvStage4Running = true;

    var status = document.getElementById('slirn-sv-stage4-status');
    var msg = status ? status.querySelector('.slirn-sv-stage4-msg') : null;
    var bar = status ? status.querySelector('.slirn-sv-stage4-bar') : null;
    var setBtn = function(txt, dis) { if (btn) { btn.textContent = txt; btn.disabled = !!dis; } };
    var show = function(t) {
      if (status) status.style.display = '';
      if (msg) { msg.style.display = ''; msg.textContent = t; }
    };

    setBtn('🔄 优化中…', true);
    show('⏳ 片段 #' + hl + ' 正在重新优化（每条约 5-30s，请耐心等待）');
    toast('片段 #' + hl + ' 重新优化中…');
    var t0 = Date.now();
    var pollTimer = setInterval(function() {
      if (bar) bar.style.width = Math.min(90, Math.round((Date.now() - t0) / 300)) + '%';
    }, 1500);

    postJSON(API + '/short_video_stage4_optimize', {
      task_id: ctx.taskId, project_id: ctx.projectId, hl_index: hl
    }).then(function(r) {
      clearInterval(pollTimer);
      if (r && r.ok) {
        var st = r.stage4_optimize || {};
        var h = (st.highlights || []).filter(function(x) {
          return parseInt(x.index || 0, 10) === hl;
        })[0] || {};
        if (bar) {
          bar.style.width = '100%';
          bar.classList.add('slirn-progress-bar--done');
        }
        show('✅ 片段 #' + hl + ' 已重新优化：修改 ' + (h.fixed || 0)
             + ' 行 · 切分 ' + (h.split || 0) + ' 行 · 删除 ' + (h.deleted || 0) + ' 行');
        setBtn('🔄 重新优化', false);
        toast('✅ 片段 #' + hl + ' 已重新优化');
        openProject(ctx.taskId, ctx.projectId);
      } else {
        if (bar) {
          bar.style.width = '100%';
          bar.classList.add('slirn-progress-bar--failed');
        }
        show('❌ ' + ((r && r.error) || '片段 #' + hl + ' 重新优化失败'));
        setBtn('🔄 重新优化', false);
        toast((r && r.error) || '片段 #' + hl + ' 重新优化失败', 'error');
      }
    }).catch(function(err) {
      clearInterval(pollTimer);
      if (bar) {
        bar.style.width = '100%';
        bar.classList.add('slirn-progress-bar--failed');
      }
      show('❌ 网络错误：' + (err && err.message || err));
      setBtn('🔄 重新优化', false);
      toast('网络错误：' + (err && err.message || err), 'error');
    }).then(function() {  // finally
      window._slirnSvStage4Running = false;
    });
  }

  // ---------- REQ-20261013-108：Stage 4 手动编辑 + 预览 + 初剪 ----------
  // （REQ-20261016-146：提示词 UI 全套移除 — 优化只用内置系统提示词）

  // REQ-20261017-153：✏️ 修改文字 / ✂️ 剪词 — 原文保留，行下方内嵌输入框
  // （替代 window.prompt）。mode='edit' 改文字；'trim' 剪词（预览/粗剪切视频）。
  // ---------- REQ-20261019-222：Stage 4 行编辑保存后保持滚动位置 ----------
  // 修改/剪词保存（💾/回车）、✖ 还原、↩️ 恢复整行都走 openProject 整体重渲染，
  // 滚动容器 .slirn-stage4-clip-subs 是新建元素 → scrollTop 归零，正在编辑的
  // 行滚出视口。编辑前记锚点（该行相对滚动容器视口的偏移），渲染完把该行
  // 滚回原位置（不回首行）。
  var _svStage4ScrollAnchor = null; // {key, hl, seg, rel} — 一次性

  function _svStage4MarkScroll(unitEl) {
    // unitEl = 行或其内嵌输入框（都带 data-hl-index + data-seg-id）
    if (!unitEl) return;
    var hl = unitEl.getAttribute('data-hl-index') || '';
    var seg = unitEl.getAttribute('data-seg-id') || '';
    if (!hl || !seg) return;
    var ctx = innerCtx(); if (!ctx) return;
    var card = unitEl.closest('.slirn-stage4-clip');
    var box = card ? card.querySelector('.slirn-stage4-clip-subs') : null;
    var row = card ? card.querySelector(
      '.slirn-stage4-sub-row[data-hl-index="' + hl + '"][data-seg-id="' + seg + '"]') : null;
    if (!box || !row) return;
    _svStage4ScrollAnchor = {
      key: ctx.taskId + '/' + ctx.projectId, hl: hl, seg: seg,
      rel: row.offsetTop - box.scrollTop,
    };
  }

  function _svStage4ApplyScrollAnchor(taskId, projectId) {
    var a = _svStage4ScrollAnchor;
    _svStage4ScrollAnchor = null; // 一次性：用过即清（项目不符也清）
    if (!a || a.key !== taskId + '/' + projectId) return;
    var row = document.querySelector(
      '.slirn-stage4-hl-pane.active .slirn-stage4-sub-row'
      + '[data-hl-index="' + a.hl + '"][data-seg-id="' + a.seg + '"]');
    if (!row || row.offsetParent === null) return; // pane 隐藏（非 stage4 焦点）
    var box = row.closest('.slirn-stage4-clip-subs');
    if (!box) return;
    box.scrollTop = Math.max(0, row.offsetTop - a.rel);
  }

  function stage4OpenEditbox(btn, mode) {
    var row = btn.closest ? btn.closest('.slirn-stage4-sub-row') : null;
    if (!row) return;
    var next = row.nextElementSibling;
    var old = next && next.classList
      && next.classList.contains('slirn-stage4-sub-editbox') ? next : null;
    if (old && old.getAttribute('data-mode') === mode) {
      var cur = old.querySelector('.slirn-stage4-sub-input');
      if (cur) { cur.focus(); cur.select(); }
      return;
    }
    if (old) old.remove();
    var orig = row.getAttribute('data-orig-text') || '';
    var eff = row.getAttribute('data-eff-text') || orig;
    var hl = row.getAttribute('data-hl-index') || '0';
    var sid = row.getAttribute('data-seg-id') || '0';
    var box = document.createElement('div');
    box.className = 'slirn-stage4-sub-editbox';
    box.setAttribute('data-mode', mode);
    box.setAttribute('data-seg-id', sid);
    box.setAttribute('data-hl-index', hl);
    box.innerHTML =
      '<span class="slirn-stage4-sub-editbox-label">'
      + (mode === 'trim' ? '剪词后' : '修改后') + '</span>'
      + '<input type="text" class="slirn-stage4-sub-input" value="' + esc(eff)
      + '" data-orig-text="' + esc(orig) + '">'
      + '<button type="button" class="slirn-btn-mini slirn-btn-mini-primary" '
      + 'data-action="sv-stage4-line-save" title="保存（预览/粗剪用修改后文字）">💾</button>'
      + '<button type="button" class="slirn-btn-mini" '
      + 'data-action="sv-stage4-line-discard" title="还原为原文（去掉本条修改）">✖</button>';
    row.insertAdjacentElement('afterend', box);
    var inp = box.querySelector('.slirn-stage4-sub-input');
    if (inp) {
      inp.focus();
      inp.select();
      // Enter 保存 / Esc 关闭（未保存的新框直接移除）
      inp.addEventListener('keydown', function(ev) {
        if (ev.key === 'Enter') {
          ev.preventDefault();
          stage4LineSave(inp);
        } else if (ev.key === 'Escape') {
          ev.preventDefault();
          if (!box.getAttribute('data-persisted')) box.remove();
          else inp.blur();
        }
      });
    }
  }

  // 整行替换 text（fix）— REQ-20261017-153：打开行下方输入框
  function stage4LineEdit(btn) {
    stage4OpenEditbox(btn, 'edit');
  }

  // 💾 保存输入框（edit → line_edit；trim → resplit 剪词）
  function stage4LineSave(el) {
    var box = el.closest ? el.closest('.slirn-stage4-sub-editbox') : null;
    if (!box) return;
    var inp = box.querySelector('.slirn-stage4-sub-input');
    if (!inp) return;
    var mode = box.getAttribute('data-mode') || 'edit';
    var val = String(inp.value || '').trim();
    var orig = inp.getAttribute('data-orig-text') || '';
    if (!val) return toast('文字不能为空（要还原请点 ✖）', 'error');
    if (val === orig)
      return toast('与原文相同 = 未修改（要还原请点 ✖）', 'warn');
    var ctx = innerCtx(); if (!ctx) return;
    _svStage4MarkScroll(box); // REQ-20261019-222：保存后重渲染保持本行滚动位置
    var url = mode === 'trim'
      ? '/short_video_stage4_resplit' : '/short_video_stage4_line_edit';
    postJSON(API + url, {
      task_id: ctx.taskId, project_id: ctx.projectId,
      hl_index: parseInt(box.getAttribute('data-hl-index') || '0', 10),
      src_index: parseInt(box.getAttribute('data-seg-id') || '0', 10),
      text: val
    }).then(function(r) {
      if (r && r.ok) {
        toast(r.toast || '已保存 — 预览/粗剪将使用修改后文字');
        openProject(ctx.taskId, ctx.projectId);
      } else toast((r && r.error) || '保存失败', 'error');
    });
  }

  // ✖ 还原（去掉本条修改；未保存的新框就地移除）
  function stage4LineDiscard(el) {
    var box = el.closest ? el.closest('.slirn-stage4-sub-editbox') : null;
    if (!box) return;
    if (!box.getAttribute('data-persisted')) { box.remove(); return; }
    var mode = box.getAttribute('data-mode') || 'edit';
    var ctx = innerCtx(); if (!ctx) return;
    var hl = parseInt(box.getAttribute('data-hl-index') || '0', 10);
    var sid = parseInt(box.getAttribute('data-seg-id') || '0', 10);
    _svStage4MarkScroll(box); // REQ-20261019-222：还原重渲染保持本行滚动位置
    var done = function(r) {
      if (r && r.ok) {
        toast(r.toast || '已还原');
        openProject(ctx.taskId, ctx.projectId);
      } else toast((r && r.error) || '还原失败', 'error');
    };
    if (mode === 'trim') {
      // 已剪行还原 = unsplit（REQ-20261016-147 恢复整行路径）
      postJSON(API + '/short_video_stage4_unsplit', {
        task_id: ctx.taskId, project_id: ctx.projectId,
        hl_index: hl, src_index: sid
      }).then(done);
    } else {
      postJSON(API + '/short_video_stage4_line_edit', {
        task_id: ctx.taskId, project_id: ctx.projectId,
        hl_index: hl, src_index: sid, text: '', remove: true
      }).then(done);
    }
  }

  // 整行删除/恢复（toggle）
  function stage4LineToggle(btn) {
    var row = btn.closest ? btn.closest('.slirn-stage4-sub-row') : null;
    if (!row) return;
    var hl = parseInt(row.getAttribute('data-hl-index') || '0', 10);
    var sid = parseInt(row.getAttribute('data-seg-id') || '0', 10);
    if (!hl || !sid) return;
    var ctx = innerCtx(); if (!ctx) return;
    postJSON(API + '/short_video_stage4_line_toggle', {
      task_id: ctx.taskId, project_id: ctx.projectId,
      hl_index: hl, src_index: sid
    }).then(function(r) {
      if (!r || !r.ok) return toast((r && r.error) || '操作失败', 'error');
      var marked = !!r.marked;
      row.setAttribute('data-deleted', marked ? '1' : '0');
      row.classList.toggle('slirn-stage4-sub-row-deleted', marked);
      // 切按钮图标与 title
      var btn2 = row.querySelector('[data-action="sv-stage4-line-toggle"]');
      if (btn2) {
        btn2.textContent = marked ? '✚' : '🗑️';
        btn2.setAttribute('title', marked ? '恢复本行' : '删除本行');
      }
      // 增删 badge
      var actions = row.querySelector('.slirn-stage4-sub-actions');
      var oldBadge = row.querySelector('.slirn-sv-badge-failed');
      if (oldBadge) oldBadge.remove();
      if (marked && actions) {
        var b = document.createElement('span');
        b.className = 'slirn-sv-badge slirn-sv-badge-failed';
        b.textContent = '🗑 已删';
        actions.insertBefore(b, actions.firstChild);
      }
      toast(marked ? '已删除' : '已恢复');
    });
  }

  // REQ-20261016-147：✂️ 重新切分 = 行内剪词 — 新文字 = 原文去掉无用词；
  // 预览/粗剪时删掉的字对应的视频片段会被自动切掉。改回原文 = 恢复整行。
  // REQ-20261017-153：改走行下方内嵌输入框（原文保留可见）。
  function stage4LineResplit(btn) {
    stage4OpenEditbox(btn, 'trim');
  }

  // REQ-20261016-147：↩️ 恢复整行（取消剪词）
  function stage4LineUnsplit(btn) {
    var row = btn.closest ? btn.closest('.slirn-stage4-sub-row') : null;
    if (!row) return;
    var hl = parseInt(row.getAttribute('data-hl-index') || '0', 10);
    var sid = parseInt(row.getAttribute('data-seg-id') || '0', 10);
    if (!hl || !sid) return;
    var ctx = innerCtx(); if (!ctx) return;
    _svStage4MarkScroll(row); // REQ-20261019-222：恢复整行重渲染保持本行滚动位置
    postJSON(API + '/short_video_stage4_unsplit', {
      task_id: ctx.taskId, project_id: ctx.projectId,
      hl_index: hl, src_index: sid
    }).then(function(r) {
      if (r && r.ok) {
        toast('已恢复整行（取消剪词）');
        openProject(ctx.taskId, ctx.projectId);
      } else toast((r && r.error) || '恢复失败', 'error');
    });
  }

  // ---------- REQ-20261019-210：Stage 4 段内顺序手工调整 ----------
  // ↑/↓ 推行 + 整行拖拽（HTML5 drag-and-drop，同 Stage 3 模板）。
  // 每次调整后立即 POST line_reorder 自动保存；顺序 = 下一阶段（粗剪合成）
  // 的字幕顺序 + 视频剪切拼接顺序（后端 _apply_manual_state 同口径消费）。
  // split 子行（data-split-from）与 LLM 删行（data-llm-deleted）不参与排序。

  function _stage4SortableRows(card) {
    var out = [];
    card.querySelectorAll('.slirn-stage4-sub-row').forEach(function(row) {
      if (row.hasAttribute('data-llm-deleted')) return;
      if (row.hasAttribute('data-split-from')) return;
      out.push(row);
    });
    return out;
  }

  function _stage4CollectOrder(card) {
    // 白名单口径：卡片内可排序行的 data-src-index 按当前 DOM 序收集
    // （src_index=0 是真实行 — LLM 行未映射 raw.srt 时为 0，须参与排序；
    //   NaN 自动被 >= 0 过滤）
    return _stage4SortableRows(card).map(function(row) {
      return parseInt(row.getAttribute('data-src-index') || '0', 10);
    }).filter(function(n) { return n >= 0; });
  }

  function _stage4Renumber(card) {
    // #N 按当前 DOM 序重编（跳过 LLM 删行 — 它们固定显示「—」）
    var n = 0;
    card.querySelectorAll('.slirn-stage4-sub-row').forEach(function(row) {
      if (row.hasAttribute('data-llm-deleted')) return;
      n += 1;
      var no = row.querySelector('.slirn-stage4-sub-no');
      if (no) no.textContent = '#' + n;
    });
  }

  function _stage4AfterUnit(el) {
    // 该行「单元」的末尾引用（行 + 紧随的内嵌输入框）→ 下移插入点
    var sid = el.getAttribute('data-seg-id');
    var next = el.nextElementSibling;
    if (sid && next && next.classList
        && next.classList.contains('slirn-stage4-sub-editbox')
        && next.getAttribute('data-seg-id') === sid) {
      return next;
    }
    return el;
  }

  function _stage4MoveRowUnit(row, ref) {
    // 行 + 它的内嵌输入框（.slirn-stage4-sub-editbox 同 seg-id）作为整体移动
    var parent = row.parentNode;
    parent.insertBefore(row, ref);
    var sid = row.getAttribute('data-seg-id');
    if (!sid) return;
    var box = null;
    card_of(row).querySelectorAll('.slirn-stage4-sub-editbox').forEach(function(b) {
      if (b.getAttribute('data-seg-id') === sid) box = b;
    });
    if (box) parent.insertBefore(box, row.nextSibling);
  }

  function card_of(el) {
    return el.closest ? el.closest('.slirn-stage4-clip') : null;
  }

  function _stage4SaveOrder(card, revert) {
    // 自动保存：POST line_reorder；失败回滚 DOM（乐观更新模式）
    var hl = parseInt(card.getAttribute('data-hl-index') || '0', 10);
    if (!hl) return;
    var order = _stage4CollectOrder(card);
    var ctx = innerCtx();
    if (!ctx) { if (revert) revert(); return; }
    postJSON(API + '/short_video_stage4_line_reorder', {
      task_id: ctx.taskId, project_id: ctx.projectId,
      hl_index: hl, order: order
    }).then(function(r) {
      if (r && r.ok) {
        _stage4Renumber(card);
        toast('顺序已保存（粗剪合成将按此顺序剪切拼接）');
      } else {
        if (revert) revert();
        _stage4Renumber(card);
        toast((r && r.error) || '顺序保存失败', 'error');
      }
    }).catch(function() {
      if (revert) revert();
      _stage4Renumber(card);
      toast('顺序保存失败（网络错误）', 'error');
    });
  }

  function stage4LineMove(btn) {
    // ↑/↓ 推行：与相邻可排序行交换（跳过 split 子行 / LLM 删行的展示位）
    var card = btn.closest ? btn.closest('.slirn-stage4-clip') : null;
    var row = btn.closest ? btn.closest('.slirn-stage4-sub-row') : null;
    if (!card || !row) return;
    var dir = btn.getAttribute('data-dir') || 'up';
    var sortable = _stage4SortableRows(card);
    var pos = sortable.indexOf(row);
    if (pos < 0) return;
    var neighbor = dir === 'up' ? sortable[pos - 1] : sortable[pos + 1];
    if (!neighbor) return; // 已在边界
    if (dir === 'up') {
      _stage4MoveRowUnit(row, neighbor);
      _stage4SaveOrder(card, function() {
        _stage4MoveRowUnit(row, _stage4AfterUnit(neighbor).nextSibling);
      });
    } else {
      _stage4MoveRowUnit(row, _stage4AfterUnit(neighbor).nextSibling);
      _stage4SaveOrder(card, function() {
        _stage4MoveRowUnit(row, neighbor);
      });
    }
  }

  function _getSt4DragCtx() {
    return window._slirnSt4DragCtx ||
      (window._slirnSt4DragCtx = { srcRow: null });
  }

  function bindStage4Sort(card) {
    // 卡内可排序行注册拖拽（幂等；数据变化由 initStage4SortAll 兜底重绑）
    _stage4SortableRows(card).forEach(function(el) {
      if (el.getAttribute('data-sort-bound') === '1') return;
      el.setAttribute('data-sort-bound', '1');
      el.addEventListener('dragstart', function(ev) {
        var ctx = _getSt4DragCtx();
        ctx.srcRow = el;
        try {
          ev.dataTransfer.setData('text/plain',
            String(el.getAttribute('data-src-index') || ''));
        } catch (e) {}
        ev.dataTransfer.effectAllowed = 'move';
        el.classList.add('slirn-stage4-sub-row-dragging');
      });
      el.addEventListener('dragend', function() {
        el.classList.remove('slirn-stage4-sub-row-dragging');
        card.querySelectorAll('.slirn-stage4-sub-row-drop-target')
          .forEach(function(x) { x.classList.remove('slirn-stage4-sub-row-drop-target'); });
        _getSt4DragCtx().srcRow = null;
      });
      el.addEventListener('dragover', function(ev) {
        ev.preventDefault(); // 不阻止则 drop 不触发（REQ-160 教训）
        ev.dataTransfer.dropEffect = 'move';
        el.classList.add('slirn-stage4-sub-row-drop-target');
      });
      el.addEventListener('dragleave', function() {
        el.classList.remove('slirn-stage4-sub-row-drop-target');
      });
      el.addEventListener('drop', function(ev) {
        ev.preventDefault();
        var ctx = _getSt4DragCtx();
        var src = ctx.srcRow;
        if (!src || src === el) return;
        var srcCard = src.closest('.slirn-stage4-clip');
        if (srcCard !== card) return; // 不跨卡片拖
        // 乐观 DOM 更新：移到目标行之前（同 Stage 3 口径）；失败回滚
        var next = src.nextElementSibling;
        _stage4MoveRowUnit(src, el);
        _stage4SaveOrder(card, function() {
          _stage4MoveRowUnit(src, next);
        });
      });
    });
  }

  function initStage4SortAll() {
    document.querySelectorAll('.slirn-stage4-clip').forEach(bindStage4Sort);
  }

  // REQ-20261019-214：Stage 4 新预览 — 原视频逐段 seek 播放 + 行高亮。
  // 旧「拼一段预览 mp4 + 卡片内嵌 video」整体移除（生成慢、占版面）；现从
  // /short_video_stage4_preview 拿播放列表（源视频 URL + 按行分组的窗口，
  // 行序/窗口与粗剪一致），浮窗在**原视频**上逐段跳播：一行（含剪词行
  // keep 窗口）播完自动 seek 到下一行起点；播放到的行在行列表高亮 +
  // 滚动跟随；点击任意未删行从该行起播（列表在浮窗后面仍可见可点）。
  var _st4Gen = 0;  // 驱动代数：新预览/换浮窗内容时旧驱动自停

  function stage4Preview(btn) {
    var card = btn.closest ? btn.closest('.slirn-stage4-clip') : null;
    if (!card) return;
    var hl = parseInt(card.getAttribute('data-hl-index') || '0', 10);
    if (!hl) return;
    var ctx = innerCtx(); if (!ctx) return;
    var popup = ensureSvVideoPopup();
    // toggle：同一卡片再点 = 关（与 Stage 5 预览体验一致）
    if (!popup.hidden && popup.getAttribute('data-popup-key') === ('st4-' + hl)) {
      _svPopupClose(popup);
      return;
    }
    _st4StartFromRow(ctx, card, hl, null);
  }

  // 取播放列表并起播；fromRow = data-eff-idx（null = 从头播）
  function _st4StartFromRow(ctx, card, hl, fromRow) {
    postJSON(API + '/short_video_stage4_preview', {
      task_id: ctx.taskId, project_id: ctx.projectId, hl_index: hl
    }).then(function(r) {
      if (!r || !r.ok) return toast((r && r.error) || '预览失败', 'error');
      _st4OpenPlayer({
        card: card, hl: hl,
        lines: (r && r.lines) || [],
        videoUrl: (r && r.video_url) || '',
        title: '内容优化预览 #' + hl,
      }, fromRow);
    });
  }

  function _st4OpenPlayer(pl, fromRow) {
    if (!pl.videoUrl) return toast('源视频不可用', 'error');
    var popup = ensureSvVideoPopup();
    var v = popup.querySelector('video');
    if (!v) return;
    popup.setAttribute('data-popup-key', 'st4-' + pl.hl);
    popup.querySelector('.slirn-sv-video-popup-title').textContent = pl.title;
    popup.hidden = false;
    _svPopupShowMedia(popup, 'video');
    _svPopupFitVideo(popup, v);  // 元数据未到先按 9:16 预设摆位
    // 扁平化：seg = {s, e, row}（秒；row = data-eff-idx）
    var segs = [];
    pl.lines.forEach(function(ln) {
      (ln.segs || []).forEach(function(sg) {
        if (sg && isFinite(sg[0]) && isFinite(sg[1]) && sg[1] > sg[0]) {
          segs.push({s: sg[0] / 1000, e: sg[1] / 1000, row: ln.row_idx});
        }
      });
    });
    if (!segs.length) return toast('没有可播放的字幕段', 'warn');
    var startIdx = 0;
    if (fromRow !== null && fromRow !== undefined) {
      for (var i = 0; i < segs.length; i++) {
        if (segs[i].row === fromRow) { startIdx = i; break; }
      }
    }
    _st4Drive(popup, v, pl.card, segs, startIdx, pl.videoUrl);
  }

  // 逐段驱动（expectedSeek 模式沿用 REQ-20261013-113 教训：自动跳段前先
  // 标记，seeking 时自跳吞掉、用户拖进度条才走收养逻辑）。
  // 关闭浮窗 / 其他入口换内容会 removeAttribute('src')+load() → 'emptied'
  // → 本驱动自清（监听随 video 元素常驻，必须显式摘除 + 清高亮）。
  function _st4Drive(popup, v, card, segs, startIdx, url) {
    var gen = ++_st4Gen;
    var segIdx = startIdx;
    var expectedSeek = false;
    var done = false;

    function rowEl(row) {
      return card.querySelector(
        '.slirn-stage4-sub-row[data-eff-idx="' + row + '"]');
    }
    function clearHL() {
      card.querySelectorAll('.slirn-stage4-sub-row-playing').forEach(function(x) {
        x.classList.remove('slirn-stage4-sub-row-playing');
      });
    }
    function highlight() {
      clearHL();
      var row = rowEl(segs[segIdx].row);
      if (row) {
        row.classList.add('slirn-stage4-sub-row-playing');
        if (row.scrollIntoView) row.scrollIntoView({block: 'nearest'});
      }
    }
    function segContaining(t) {
      for (var i = 0; i < segs.length; i++) {
        if (t >= segs[i].s - 0.05 && t <= segs[i].e + 0.05) return i;
      }
      return -1;
    }
    function onSeeking() {
      if (gen !== _st4Gen) return cleanup();
      if (expectedSeek) { expectedSeek = false; return; }
      // 用户拖进度条 → 收养所在段（不在任何段里 → 起点晚于 t 的下一段）
      var t = v.currentTime;
      var i = segContaining(t);
      if (i < 0) {
        for (var j = 0; j < segs.length; j++) {
          if (segs[j].s > t) { i = j; break; }
        }
      }
      if (i < 0) { done = true; v.pause(); clearHL(); return; }
      if (i !== segIdx) { segIdx = i; highlight(); }
    }
    function onTime() {
      if (gen !== _st4Gen) return cleanup();
      if (done) return;
      if (v.currentTime >= segs[segIdx].e) {
        segIdx++;
        if (segIdx >= segs.length) { done = true; v.pause(); return; }
        expectedSeek = true;
        try { v.currentTime = segs[segIdx].s; } catch (e) { expectedSeek = false; }
        highlight();
      }
    }
    function onEmptied() { cleanup(); }
    function cleanup() {
      v.removeEventListener('timeupdate', onTime);
      v.removeEventListener('seeking', onSeeking);
      v.removeEventListener('emptied', onEmptied);
      clearHL();
    }
    // REQ-20261019-218：load() 的 media reset 在 Chrome 里把 'emptied' 异步
    // （task 队列）派发——同步挂上的 timeupdate/seeking/emptied 监听会被这个
    // 迟到的 emptied 当成「浮窗被换内容」立刻自杀（实测：监听挂上即被
    // cleanup 摘除，只剩 onMeta 完成起播 seek，之后一路线性播完不跳段）。
    // 改到 onMeta（loadedmetadata）里挂：它必然晚于 reset 的 emptied，
    // 且早于驱动播放产生的任何 timeupdate/seeking。
    v.src = url;
    v.load();
    var onMeta = function() {
      if (gen !== _st4Gen) return cleanup();
      _svPopupFitVideo(popup, v);  // 元数据到达 → 贴真实比例（REQ-188）
      v.addEventListener('timeupdate', onTime);
      v.addEventListener('seeking', onSeeking);
      v.addEventListener('emptied', onEmptied);
      highlight();
      expectedSeek = true;
      try { v.currentTime = segs[segIdx].s; } catch (e) { expectedSeek = false; }
      v.play().catch(function() {});
    };
    if (v.readyState >= 1) onMeta();
    else v.addEventListener('loadedmetadata', onMeta, {once: true});
  }

  function stage4Run() {
    var ctx = innerCtx(); if (!ctx) return;
    toast('粗剪合成中…');
    postJSON(API + '/short_video_stage4_run', {
      task_id: ctx.taskId, project_id: ctx.projectId
    }).then(function(r) {
      if (r && r.ok) { toast('Stage 5 完成'); openProject(ctx.taskId, ctx.projectId); }
      else toast((r && r.error) || 'Stage 5 失败', 'error');
    });
  }

  // REQ-20261017-155：Stage 5 合成轮询公共函数 — 全量 stage5Export 与单条
  // stage5ExportOne 共用（1500ms setInterval 查 job 到 done/failed；按钮文案
  // 经 cfg 注入，复用同一张 #slirn-sv-stage5-status 状态卡）。
  function _pollCoarseJob(btn, tid, pid, cfg) {
    var status = document.getElementById('slirn-sv-stage5-status');
    var msg = status ? status.querySelector('.slirn-sv-stage5-msg') : null;
    var bar = status ? status.querySelector('.slirn-progress-bar') : null;
    var show = function(t) { if (msg) { msg.style.display = ''; msg.textContent = t; } };
    var setBtn = function(txt, dis) { if (btn) { btn.textContent = txt; btn.disabled = !!dis; } };
    var timer = setInterval(function() {
      postJSON(API + '/short_video_stage5_export_status', {
        task_id: tid, project_id: pid
      }).then(function(s) {
        if (!s || !s.ok || !s.job) return;
        var j = s.job;
        if (j.state === 'running') {
          var pct = Math.max(1, Math.min(99, Math.round(j.progress || 0)));
          setBtn(cfg.running + pct + '%', true);
          if (bar) bar.style.width = pct + '%';
          show('⏳ ' + (j.stage || '合成中') + ' ' + pct + '% — 可继续其它操作，完成后自动显示预览');
          return;
        }
        clearInterval(timer);
        if (j.state === 'done') {
          setBtn(cfg.idle, false);
          if (bar) bar.style.width = '100%';
          show(cfg.doneMsg);
          toast(cfg.doneToast || '✅ 完成');
          // REQ-20261019-225：合成产物已替换 —— 停开着的 Stage 5 预览浮窗
          // （浮窗内容只在打开时取一次，不关会一直显示旧 SRT/旧 mp4，
          // 看似「Stage 4 删行没生效」）。重新打开即取最新（_cacheBust）。
          var _pp = document.getElementById('slirn-sv-video-popup');
          if (_pp && !_pp.hidden
              && String(_pp.getAttribute('data-popup-key') || '')
                  .indexOf('s5-') === 0) {
            _svPopupClose(_pp);
            toast('粗剪已更新，预览窗口已关闭 — 请重新打开查看最新字幕');
          }
          openProject(tid, pid);
        } else {
          setBtn(cfg.idle, false);
          show('❌ ' + (j.error || '合成失败'));
          toast('❌ ' + (j.error || '合成失败'), 'error');
        }
      });
    }, 1500);
  }

  // REQ-20261013-103：Stage 5 粗剪异步导出（仿长视频 compose_rough + setInterval 轮询）
  function stage5Export(btn) {
    var ctx = innerCtx(); if (!ctx) return;
    var tid = btn.getAttribute('data-task-id') || ctx.taskId;
    var pid = btn.getAttribute('data-project-id') || ctx.projectId;
    var setBtn = function(txt, dis) { btn.textContent = txt; btn.disabled = !!dis; };
    var status = document.getElementById('slirn-sv-stage5-status');
    var msg = status ? status.querySelector('.slirn-sv-stage5-msg') : null;
    var show = function(t) { if (msg) { msg.style.display = ''; msg.textContent = t; } };
    setBtn('🎬 合成中…', true);
    show('⏳ 正在启动合成…');
    postJSON(API + '/short_video_stage5_export', {
      task_id: tid, project_id: pid
    }).then(function(r) {
      if (!r || !r.ok) {
        setBtn('🎬 粗剪合成', false);
        show('');
        if (msg) msg.style.display = 'none';
        toast('❌ ' + (r && r.error ? r.error : '启动失败'), 'error');
        return;
      }
      toast(r.toast || '🎬 粗剪已启动');
      _pollCoarseJob(btn, tid, pid, {
        running: '🎬 合成中… ',
        idle: '🎬 重新合成',
        doneMsg: '✅ 粗剪成片已生成 — 下方预览每条 clip',
        doneToast: '✅ 粗剪完成'
      });
    });
  }

  // REQ-20261017-155：Stage 5 单条重新合成 — 只对该 clip 重跑粗剪
  // （hl_index 单条模式端点；其余条产物保留，精剪阶段作废重跑）。
  function stage5ExportOne(btn) {
    var ctx = innerCtx(); if (!ctx) return;
    if (btn && btn.disabled) return;
    var tid = btn.getAttribute('data-task-id') || ctx.taskId;
    var pid = btn.getAttribute('data-project-id') || ctx.projectId;
    var idx = parseInt(btn.getAttribute('data-clip-index') || '0', 10);
    if (!idx) return toast('片段序号无效', 'error');
    if (!window.confirm('对片段 #' + idx + ' 单独重新合成？\n'
        + '该条粗剪成片将被替换（其余条保留），精剪阶段会作废重跑。')) return;
    btn.disabled = true;
    btn.textContent = '🔄 合成中…';
    toast('片段 #' + idx + ' 重新合成中…');
    postJSON(API + '/short_video_stage5_export', {
      task_id: tid, project_id: pid, hl_index: idx
    }).then(function(r) {
      if (!r || !r.ok) {
        btn.disabled = false;
        btn.textContent = '🔄 重新合成';
        toast('❌ ' + (r && r.error ? r.error : '启动失败'), 'error');
        return;
      }
      toast('🎬 片段 #' + idx + ' 已启动重新合成');
      _pollCoarseJob(btn, tid, pid, {
        running: '🔄 合成中… ',
        idle: '🔄 重新合成',
        doneMsg: '✅ 片段 #' + idx + ' 已重新合成 — 下方可预览',
        doneToast: '✅ 片段 #' + idx + ' 合成完成'
      });
    });
  }
  // REQ-20261013-103：Stage 5 删除（仿 compose_rough_delete）
  function stage5Delete(btn) {
    var ctx = innerCtx(); if (!ctx) return;
    var tid = btn.getAttribute('data-task-id') || ctx.taskId;
    var pid = btn.getAttribute('data-project-id') || ctx.projectId;
    if (!window.confirm('删除「粗剪成品」（' + (btn.getAttribute('data-count') || 'N') + ' 条 mp4 + srt）？\n'
        + '删除后精剪阶段也会重置，需重新合成。')) return;
    btn.disabled = true;
    postJSON(API + '/short_video_stage5_delete', {
      task_id: tid, project_id: pid
    }).then(function(r) {
      btn.disabled = false;
      if (!r || !r.ok) {
        toast('❌ ' + (r && r.error ? r.error : '删除失败'), 'error');
        return;
      }
      toast(r.toast || '🗑️ 已删除');
      openProject(tid, pid);
    });
  }
  // 连续 SRT 复制（从预览面板 <pre> 读，或服务端拼）
  function stage5SubsCopy(btn) {
    var ctx = innerCtx(); if (!ctx) return;
    var tid = btn.getAttribute('data-task-id') || ctx.taskId;
    var pid = btn.getAttribute('data-project-id') || ctx.projectId;
    postJSON(API + '/short_video_stage5_preview_subs', {
      task_id: tid, project_id: pid
    }).then(function(r) {
      if (!r || !r.ok || !r.srt) {
        toast('❌ ' + (r && r.error ? r.error : '获取 SRT 失败'), 'error');
        return;
      }
      var srt = r.srt;
      var done = function() { toast('📋 已复制 ' + r.clips + ' 段连续 SRT'); };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(srt).then(done, function() { _svFallbackCopy(srt, done); });
      } else {
        _svFallbackCopy(srt, done);
      }
    });
  }
  function _svFallbackCopy(text, cb) {
    var ta = document.createElement('textarea');
    ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); cb && cb(); } catch (e) { toast('❌ 复制失败', 'error'); }
    document.body.removeChild(ta);
  }
  // 连续 SRT 下载
  function stage5SubsDownload(btn) {
    var ctx = innerCtx(); if (!ctx) return;
    var tid = btn.getAttribute('data-task-id') || ctx.taskId;
    var pid = btn.getAttribute('data-project-id') || ctx.projectId;
    btn.disabled = true;
    postJSON(API + '/short_video_stage5_preview_subs', {
      task_id: tid, project_id: pid
    }).then(function(r) {
      btn.disabled = false;
      if (!r || !r.ok || !r.srt) {
        toast('❌ ' + (r && r.error ? r.error : '获取 SRT 失败'), 'error');
        return;
      }
      var blob = new Blob([r.srt], {type: 'text/plain;charset=utf-8'});
      var url = URL.createObjectURL(blob);
      var a = document.createElement('a');
      a.href = url; a.download = 'short_video_coarse_' + tid + '_' + pid + '.srt';
      document.body.appendChild(a); a.click();
      setTimeout(function() { URL.revokeObjectURL(url); document.body.removeChild(a); }, 100);
      toast('⬇️ 已下载 ' + r.clips + ' 段连续 SRT');
    });
  }
  // 单条 clip 的 SRT 复制（按 coarse_srt 文件路径）
  // REQ-20261019-227：行内「📋 复制 SRT」按钮按需求移除（stage5ClipSubsCopy
  // 随路由一并删除；顶部「连续 SRT」复制/下载两键保留不动）。

  // 📝 提取字幕 / 🔄 重新提取 — 按合成后的视频（coarse_NN.mp4）跑 ASR +
  // LLM 整理（REQ-228），产物 subs 四件套；两按钮共用此函数
  function stage5SubsExtract(btn) {
    var ctx = innerCtx(); if (!ctx) return;
    var tid = btn.getAttribute('data-task-id') || ctx.taskId;
    var pid = btn.getAttribute('data-project-id') || ctx.projectId;
    var idx = parseInt(btn.getAttribute('data-clip-index') || '0', 10);
    if (!idx) return toast('片段序号无效', 'error');
    var label0 = btn.textContent;  // 失败恢复各自的按钮文字（提取/重新提取）
    btn.setAttribute('data-label0', label0);
    btn.disabled = true;
    btn.textContent = '⏳ 提取中…';
    toast('📝 片段 #' + idx + ' 字幕提取中（按成片跑 ASR + LLM 整理，需稍候）');
    postJSON(API + '/short_video_subs_extract', {
      task_id: tid, project_id: pid, clip_index: idx
    }).then(function(r) {
      if (!r || !r.ok) {
        btn.disabled = false;
        btn.textContent = label0;
        return toast('❌ ' + (r && r.error ? r.error : '启动失败'), 'error');
      }
      if (r.llm_ready === false) {
        toast('⚠️ 未配置大模型：只出 ASR 原始断句，不做断句+标点整理');
      }
      _pollSubsJob(btn, tid, pid, idx);
    });
  }

  // 提取进度轮询（setInterval，仿 _pollCoarseJob；完成 → 重渲染 + 自动展开）
  function _pollSubsJob(btn, tid, pid, idx) {
    var timer = setInterval(function() {
      postJSON(API + '/short_video_subs_status', {
        task_id: tid, project_id: pid, clip_index: idx
      }).then(function(s) {
        if (!s || !s.ok || !s.job) return;
        var j = s.job;
        if (j.state === 'running') {
          if (btn) {
            btn.textContent = '⏳ ' + (j.stage || '提取中') +
              (j.elapsed_s ? ' ' + j.elapsed_s + 's' : '…');
          }
          return;
        }
        clearInterval(timer);
        if (j.state === 'done') {
          toast('✅ 片段 #' + idx + ' 字幕提取完成' +
                (j.refined ? '（已断句+加标点）' : '（ASR 原样，未整理）'));
          openProject(tid, pid);
          _subsAutoExpand(idx, 0);
        } else {
          if (btn) {  // 失败回各自的按钮文字（提取字幕/重新提取，REQ-229）
            btn.disabled = false;
            btn.textContent = btn.getAttribute('data-label0') || '📝 提取字幕';
          }
          toast('❌ ' + (j.error || '提取失败'), 'error');
        }
      });
    }, 2000);
  }

  // 重渲染后自动展开该条编辑器（innerHTML 异步落位，重试找切换按钮）
  function _subsAutoExpand(idx, attempt) {
    if (attempt > 10) return;
    setTimeout(function() {
      var t = document.querySelector(
        '.slirn-stage5-subs-btn[data-action="sv-stage5-subs-toggle"][data-clip-index="' + idx + '"]');
      if (t) { stage5SubsToggle(t); return; }
      _subsAutoExpand(idx, attempt + 1);
    }, 500);
  }

  // 📝 字幕 — 展开/收起本条字幕编辑器（首次展开拉 cues 现渲染）
  function stage5SubsToggle(btn) {
    var row = btn.closest ? btn.closest('.slirn-stage5-row') : null;
    var box = row ? row.querySelector('.slirn-stage5-subs-editor') : null;
    if (!box) return;
    if (!box.hidden) { box.hidden = true; return; }
    box.hidden = false;
    if (box.getAttribute('data-loaded') === '1') return;
    var ctx = innerCtx(); if (!ctx) return;
    var idx = parseInt(box.getAttribute('data-clip-index') || '0', 10);
    box.innerHTML = '<div class="slirn-sv-muted">字幕加载中…</div>';
    postJSON(API + '/short_video_subs_get', {
      task_id: ctx.taskId, project_id: ctx.projectId, clip_index: idx
    }).then(function(r) {
      if (!r || !r.ok) {
        box.innerHTML = '<div class="slirn-sv-muted">字幕读取失败</div>';
        return toast('❌ ' + (r && r.error ? r.error : '读取失败'), 'error');
      }
      if (!r.has_subs) {
        // 服务端自愈清了 subs_srt 字段 → 重渲染回到「提取字幕」按钮态
        box.hidden = true;
        openProject(ctx.taskId, ctx.projectId);
        return toast('字幕文件不存在，请重新提取', 'warn');
      }
      _renderStage5SubsEditor(box, r.cues || []);
      box.setAttribute('data-loaded', '1');
    });
  }

  function _subsFmtTime(ms) {
    var s = Math.max(0, Math.floor(Number(ms || 0) / 1000));
    var m = Math.floor(s / 60); s = s % 60;
    return m + ':' + (s < 10 ? '0' : '') + s;
  }

  // 编辑器渲染：每 cue 一行（时间 + 原文（改过才显示，灰字删除线）+ 输入框 + ↩）
  function _renderStage5SubsEditor(box, cues) {
    var rowsHtml = cues.map(function(c) {
      var mod = !!c.modified;
      return '<div class="slirn-stage5-subs-cuerow' + (mod ? ' is-modified' : '') + '"'
        + ' data-cue="' + c.i + '">'
        + '<span class="slirn-stage5-subs-time">' + _subsFmtTime(c.start_ms)
        + ' → ' + _subsFmtTime(c.end_ms) + '</span>'
        + (mod ? '<span class="slirn-stage5-subs-orig"'
          + ' title="原文（保留在 asr 版，↩ 可还原）">' + esc(c.orig) + '</span>' : '')
        + '<input type="text" class="slirn-stage5-subs-input" value="' + esc(c.text) + '"'
        + ' data-orig="' + esc(c.orig) + '">'
        + (mod ? '<button type="button" class="slirn-btn-mini"'
          + ' data-action="sv-stage5-subs-restore" title="还原为原文">↩</button>' : '')
        + '</div>';
    }).join('');
    var n = cues.length;
    var m = cues.filter(function(c) { return c.modified; }).length;
    box.innerHTML =
      '<div class="slirn-stage5-subs-head"><span class="slirn-sv-muted">共 ' + n + ' 条'
      + (m ? ' · 已改 ' + m + ' 条（灰字为原文，↩ 还原）'
           : ' · 改字后下载/精剪用新文字；原文保留，↩ 可还原')
      + '</span></div>'
      + '<div class="slirn-stage5-subs-rows">' + rowsHtml + '</div>';
    // 输入交互：Enter = 立即失焦保存；change（失焦且值变）= 保存
    Array.prototype.forEach.call(
      box.querySelectorAll('.slirn-stage5-subs-input'), function(inp) {
        inp.setAttribute('data-saved', inp.value);
        inp.addEventListener('keydown', function(ev) {
          if (ev.key === 'Enter') { ev.preventDefault(); inp.blur(); }
        });
        inp.addEventListener('change', function() {
          _subsSaveInput(box, inp);
        });
      });
  }

  function _subsSaveInput(box, inp) {
    var val = String(inp.value || '');
    if (val === (inp.getAttribute('data-saved') || '')) return;  // 值未变
    inp.setAttribute('data-saved', val);
    var rowEl = inp.closest('.slirn-stage5-subs-cuerow');
    if (!rowEl) return;
    var ctx = innerCtx(); if (!ctx) return;
    postJSON(API + '/short_video_subs_save', {
      task_id: ctx.taskId, project_id: ctx.projectId,
      clip_index: parseInt(box.getAttribute('data-clip-index') || '0', 10),
      cue_index: parseInt(rowEl.getAttribute('data-cue') || '0', 10),
      text: val
    }).then(function(r) {
      if (!r || !r.ok) {
        inp.setAttribute('data-saved', '');  // 失败允许重试
        return toast('❌ ' + (r && r.error ? r.error : '保存失败'), 'error');
      }
      _subsRowApplyState(rowEl, inp, r.action === 'restored');
      toast(r.toast || '已保存');
    });
  }

  // 行状态就地刷新（改过 → 原文 + ↩ 出现；还原 → 撤掉），不整块重渲染
  function _subsRowApplyState(rowEl, inp, restored) {
    var orig = inp.getAttribute('data-orig') || '';
    rowEl.classList.toggle('is-modified', !restored);
    var old = rowEl.querySelector('.slirn-stage5-subs-orig');
    if (!restored && !old) {
      var sp = document.createElement('span');
      sp.className = 'slirn-stage5-subs-orig';
      sp.title = '原文（保留在 asr 版，↩ 可还原）';
      sp.textContent = orig;
      rowEl.insertBefore(sp, inp);
    }
    var btn = rowEl.querySelector('[data-action="sv-stage5-subs-restore"]');
    if (!restored && !btn) {
      btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'slirn-btn-mini';
      btn.setAttribute('data-action', 'sv-stage5-subs-restore');
      btn.title = '还原为原文';
      btn.textContent = '↩';
      rowEl.appendChild(btn);
    }
    if (restored) {
      if (old) old.remove();
      if (btn) btn.remove();
      inp.value = orig;
    }
  }

  // ↩ 还原本条（input 回原文 + 服务端删 edits key 重物化）
  function stage5SubsRestore(btn) {
    var rowEl = btn.closest ? btn.closest('.slirn-stage5-subs-cuerow') : null;
    var box = btn.closest ? btn.closest('.slirn-stage5-subs-editor') : null;
    if (!rowEl || !box) return;
    var inp = rowEl.querySelector('.slirn-stage5-subs-input');
    if (!inp) return;
    var ctx = innerCtx(); if (!ctx) return;
    inp.value = inp.getAttribute('data-orig') || '';
    inp.setAttribute('data-saved', inp.value);
    postJSON(API + '/short_video_subs_save', {
      task_id: ctx.taskId, project_id: ctx.projectId,
      clip_index: parseInt(box.getAttribute('data-clip-index') || '0', 10),
      cue_index: parseInt(rowEl.getAttribute('data-cue') || '0', 10),
      text: inp.value
    }).then(function(r) {
      if (!r || !r.ok) return toast('❌ ' + (r && r.error ? r.error : '还原失败'), 'error');
      _subsRowApplyState(rowEl, inp, r.action === 'restored');
      toast(r.toast || '已还原为原文');
    });
  }

  // 单条 clip 的 SRT 下载（直接 <a download>）
  // REQ-20261019-227：优先成片字幕 subs_NN.srt（ASR+改字版，kind=stage5），
  // 未提取过才落粗剪自产 SRT（kind=stage4）
  function stage5ClipSubsDownload(btn) {
    var ctx = innerCtx(); if (!ctx) return;
    var tid = btn.getAttribute('data-task-id') || ctx.taskId;
    var pid = btn.getAttribute('data-project-id') || ctx.projectId;
    var idx = parseInt(btn.getAttribute('data-clip-index') || '0', 10);
    if (!idx) return;
    postJSON(API + '/short_video_get', {task_id: tid, project_id: pid}).then(function(r) {
      if (!r || !r.ok) return toast('读取项目失败', 'error');
      var coarse = ((r.project || {}).pipeline || {}).stage5_coarse || {};
      var hl = (coarse.highlights || []).find(function(h) { return h.index === idx; }) || {};
      var rel = hl.subs_srt || hl.coarse_srt;
      if (!rel) return toast('该 clip 没有 SRT', 'error');
      var kind = hl.subs_srt ? 'stage5' : 'stage4';
      var srtName = (String(rel).split('/').pop() || '').split('\\').pop();
      // REQ-20261017-162：磁盘产物必须走自家文件端点
      var url = API + '/short_video_file?task_id=' + encodeURIComponent(tid) +
            '&project_id=' + encodeURIComponent(pid) +
            '&kind=' + kind + '&name=' + encodeURIComponent(srtName);
      var a = document.createElement('a');
      // REQ-20261019-225：下载也要 bust（download 属性已定文件名，_ts 不影响）
      a.href = _cacheBust(url); a.download = srtName;
      document.body.appendChild(a); a.click();
      setTimeout(function() { document.body.removeChild(a); }, 100);
      toast('⬇️ 已下载 #' + idx + (hl.subs_srt ? ' SRT（含改字）' : ' SRT'));
    });
  }

  // REQ-20261016-132：Stage 5 视频预览浮窗（不遮父页 + 可拖动 + 可调大小）
  // 单一浮窗（lazy 创建在 document.body），同一时刻只能开 1 个；点 × 或再点
  // 同一按钮关闭。拖动通过 title-bar mousedown；调大小通过 CSS resize 句柄。
  // REQ-20261018-171：浮窗改三媒体通用（video / img / audio）——
  // 视频沿用原逻辑，图片（封面/背景/头像）与音频（附加音频）共用同一浮窗。
  function _svPopupShowMedia(popup, kind) {
    ['video', 'img', 'audio'].forEach(function(tag) {
      var el = popup.querySelector(tag);
      if (!el) return;
      if (tag === kind) { el.style.display = ''; return; }
      el.style.display = 'none';
      // 切媒体必须停掉旧的（否则音频切走后还在后台播）
      try {
        el.removeAttribute('src');
        if (tag !== 'img') { el.pause(); el.load(); }
      } catch (err) { /* 未加载时 load 无害 */ }
    });
    // REQ-20261019-215：字幕条只属于 Stage 5 预览 —— 换媒体一律收起
    // （video 的 emptied 会让字幕驱动自清，这里只管视觉）
    var subs = popup.querySelector('.slirn-sv-video-popup-subs');
    if (subs) { subs.hidden = true; subs.innerHTML = ''; }
  }
  function _svPopupClose(popup) {
    popup.hidden = true;
    var v = popup.querySelector('video');
    if (v) { v.pause(); v.removeAttribute('src'); v.load(); }
    var im = popup.querySelector('img');
    if (im) im.removeAttribute('src');
    var au = popup.querySelector('audio');
    if (au) { au.pause(); au.removeAttribute('src'); au.load(); }
    var subs = popup.querySelector('.slirn-sv-video-popup-subs');
    if (subs) { subs.hidden = true; subs.innerHTML = ''; }
  }
  function ensureSvVideoPopup() {
    var el = document.getElementById('slirn-sv-video-popup');
    if (el) return el;
    el = document.createElement('div');
    el.id = 'slirn-sv-video-popup';
    el.className = 'slirn-sv-video-popup';
    el.hidden = true;
    el.innerHTML =
      '<div class="slirn-sv-video-popup-head">' +
        '<span class="slirn-sv-video-popup-title">预览</span>' +
        '<button type="button" class="slirn-sv-video-popup-close" ' +
        'aria-label="关闭" title="关闭">×</button>' +
      '</div>' +
      '<div class="slirn-sv-video-popup-body">' +
        '<video controls preload="metadata"></video>' +
        '<img alt="预览图" style="display:none;">' +
        '<audio controls preload="metadata" style="display:none;"></audio>' +
      '</div>' +
      '<div class="slirn-sv-video-popup-subs" hidden></div>' +
      '<div class="slirn-sv-video-popup-resize" aria-hidden="true"></div>';
    document.body.appendChild(el);
    // 关闭按钮
    el.querySelector('.slirn-sv-video-popup-close').addEventListener('click', function() {
      _svPopupClose(el);
    });
    // ESC 关闭
    el.addEventListener('keydown', function(e) {
      if (e.key === 'Escape') {
        _svPopupClose(el);
      }
    });
    // 拖动（按 title bar）
    bindSvPopupDrag(el);
    return el;
  }

  // REQ-20261019-217：head/close 选择器参数化 —— 视频浮窗与字幕弹层共用
  function bindSvPopupDrag(el, headSel, closeSel) {
    headSel = headSel || '.slirn-sv-video-popup-head';
    closeSel = closeSel || '.slirn-sv-video-popup-close';
    var head = el.querySelector(headSel);
    if (!head) return;
    var dragging = false, offX = 0, offY = 0;
    head.addEventListener('mousedown', function(e) {
      if (e.target.closest(closeSel)) return;
      dragging = true;
      var rect = el.getBoundingClientRect();
      offX = e.clientX - rect.left;
      offY = e.clientY - rect.top;
      // 转成 left/top 定位（拖动 + 调大小都依赖绝对坐标）
      el.style.left = rect.left + 'px';
      el.style.top = rect.top + 'px';
      el.style.right = 'auto';
      el.style.bottom = 'auto';
      el.style.width = rect.width + 'px';
      el.style.height = rect.height + 'px';
      el.setAttribute('tabindex', '-1');
      el.focus({preventScroll: true});
      e.preventDefault();
    });
    document.addEventListener('mousemove', function(e) {
      if (!dragging) return;
      var w = el.offsetWidth, h = el.offsetHeight;
      var maxX = Math.max(0, window.innerWidth - w);
      var maxY = Math.max(0, window.innerHeight - h);
      var nx = Math.max(0, Math.min(maxX, e.clientX - offX));
      var ny = Math.max(0, Math.min(maxY, e.clientY - offY));
      el.style.left = nx + 'px';
      el.style.top = ny + 'px';
    });
    document.addEventListener('mouseup', function() {
      dragging = false;
    });
  }

  // REQ-20261018-188：预览浮窗定位/尺寸 —— 左上角 + 宽高比跟随视频。
  // 视频元数据（videoWidth/videoHeight）到达后在可用空间内等比取最大尺寸
  // （标题栏高度另加）；元数据未到时先按 9:16 预设摆位（本项目产物以竖屏
  // 为主），到达后再贴一次真实比例 —— 不读 hidden 态的 offsetWidth（REQ-137
  // 教训），全部用已知数值推。
  function _svPopupPlaceTopLeft(popup) {
    popup.style.left = '16px';
    popup.style.top = '80px';
    popup.style.right = 'auto';
    popup.style.bottom = 'auto';
  }

  function _svPopupFitVideo(popup, v) {
    var vw = v && v.videoWidth, vh = v && v.videoHeight;
    if (!vw || !vh) { vw = 9; vh = 16; }
    var head = popup.querySelector('.slirn-sv-video-popup-head');
    var headH = (head && !popup.hidden && head.offsetHeight) || 34;
    // REQ-20261019-215：字幕条展开时视频区让位（subsH 只在浮窗可见时量得到，
    // 隐藏态 offsetHeight=0 —— REQ-137 教训），整窗高仍 ≤600
    var subs = popup.querySelector('.slirn-sv-video-popup-subs');
    var subsH = (subs && !subs.hidden && !popup.hidden && subs.offsetHeight) || 0;
    // REQ-20261019-213：视频预览浮窗上限收紧 —— 宽 ≤400、整窗高 ≤600
    // （高含标题栏，视频区 = 600 - headH）；小窗口仍优先可用空间防写穿屏
    var maxW = Math.max(320, Math.min(400, window.innerWidth - 32));
    var maxH = Math.max(200,
      Math.min(600 - headH - subsH, window.innerHeight - 96 - headH - subsH));
    var w = maxW, h = Math.round(w * vh / vw);
    if (h > maxH) { h = maxH; w = Math.round(h * vw / vh); }
    popup.style.width = Math.max(320, w) + 'px';
    popup.style.height = (Math.max(180, h) + headH + subsH) + 'px';
    _svPopupPlaceTopLeft(popup);
  }

  // 打开视频浮窗的统一入口：先按当前已知信息摆位，元数据到达后按真实
  // REQ-20261019-225：产物文件被重跑替换后 URL 不变 —— 浏览器 HTTP 缓存/
  // 已开浮窗会一直拿旧内容（看似「删行没生效」）。取视频/字幕一律附加
  // _ts 时间戳破缓存（文件端点对多余 query 参数无感）；纯展示的下载链接
  // 不加，保持下载文件名干净。
  function _cacheBust(url) {
    var u = String(url || '');
    if (!u || u.indexOf('javascript:') === 0) return u;
    return u + (u.indexOf('?') >= 0 ? '&' : '?') + '_ts=' + Date.now();
  }

  // 宽高比重贴（每次打开都重置到左上角 + 跟随本条视频的比例）
  function _svPopupOpenVideo(popup, v, url) {
    popup.hidden = false;
    _svPopupShowMedia(popup, 'video');
    v.removeAttribute('src');
    v.load();
    _svPopupFitVideo(popup, v);
    v.addEventListener('loadedmetadata', function onMeta() {
      v.removeEventListener('loadedmetadata', onMeta);
      if (!popup.hidden) _svPopupFitVideo(popup, v);
    });
    v.src = _cacheBust(url);
    v.load();
    v.play().catch(function() {});
  }

  // REQ-20261019-232：Stage 6 成片预览 —— 卡片内嵌播放区移除（区域太小、
  // 全屏太大），改「▶ 预览成片」按钮走共享视频浮窗（标题栏拖动 +
  // CSS resize: both 调大小，REQ-132/213 基建原样复用）。
  function svStage6FinalPreview(btn) {
    if (!btn) return;
    var url = btn.getAttribute('data-url') || '';
    if (!url) return toast('请先导出最终视频', 'warn');
    var clipIndex = parseInt(btn.getAttribute('data-clip-index') || '0', 10);
    var popup = ensureSvVideoPopup();
    // 二次点击同一 clip → 关闭（toggle 行为，与 Stage 5 预览一致）
    if (!popup.hidden && popup.getAttribute('data-popup-key') === ('s6f-' + clipIndex)) {
      _svPopupClose(popup);
      return;
    }
    popup.setAttribute('data-popup-key', 's6f-' + clipIndex);
    popup.querySelector('.slirn-sv-video-popup-title').textContent =
      '成片 #' + clipIndex + '（精剪混编）';
    _svPopupOpenVideo(popup, popup.querySelector('video'), url);
  }

  // REQ-20261019-233：Stage 6 导出合成标题后的封面 —— 同步 ffmpeg 单帧
  // 渲染落 stage6/cover_NN.png，回来直接触发下载（同源 a[download]）。
  function stage6CoverExport(btn) {
    var ctx = innerCtx(); if (!ctx) return;
    var idx = parseInt(btn.getAttribute('data-clip-index') || '0', 10);
    if (!idx) return;
    var t0 = btn.textContent;
    btn.disabled = true;
    btn.textContent = '🖼 导出中…';
    postJSON(API + '/short_video_stage6_cover_export', {
      task_id: ctx.taskId, project_id: ctx.projectId, index: idx
    }).then(function(r) {
      btn.disabled = false;
      btn.textContent = t0;
      if (!r || !r.ok) return toast((r && r.error) || '封面导出失败', 'error');
      toast(r.toast || '🖼 封面已导出');
      if (r.file_url) {
        var a = document.createElement('a');
        a.href = r.file_url;
        a.download = 'cover_' + idx + '.png';
        document.body.appendChild(a);
        a.click();
        a.remove();
      }
    }).catch(function() {
      btn.disabled = false;
      btn.textContent = t0;
      toast('封面导出失败（网络错误）', 'error');
    });
  }

  // REQ-20261016-142：Stage 6 「▶ 预览」按修改后字幕拼接播放
  // REQ-20261016-148：字幕优化阶段移除 → 拼接预览端点删除。
  // 预览对象回归粗剪 mp4 本身：读按钮 data-url 直接浮窗播放。
  function svStage5Preview(btn) {
    if (!btn) return;
    var url = btn.getAttribute('data-url') || '';
    if (!url) return toast('请先完成粗剪合成', 'warn');
    var clipIndex = parseInt(btn.getAttribute('data-clip-index') || '0', 10);
    var popup = ensureSvVideoPopup();
    // 二次点击同一 clip → 关闭（toggle 行为，沿用 REQ-132 体验）
    // REQ-20261017-158：toggle 键改 data-popup-key，与素材文件名查看互不干扰
    if (!popup.hidden && popup.getAttribute('data-popup-key') === ('s5-' + clipIndex)) {
      _svPopupClose(popup);
      return;
    }
    popup.setAttribute('data-popup-key', 's5-' + clipIndex);
    popup.querySelector('.slirn-sv-video-popup-title').textContent =
      '预览 #' + clipIndex + '（粗剪合成片段）';
    var v = popup.querySelector('video');
    // REQ-20261018-188：左上角 + 宽高比跟随视频（统一入口）
    _svPopupOpenVideo(popup, v, url);
    // REQ-20261019-215：浮窗下方字幕条 —— 拉取粗剪 SRT，跟随播放滚动 + 当前行高亮
    var srtUrl = btn.getAttribute('data-srt-url') || '';
    var subs = popup.querySelector('.slirn-sv-video-popup-subs');
    if (!srtUrl || !subs) return;
    subs.hidden = false;
    subs.innerHTML = '<div class="slirn-sv-subs-cue">字幕加载中…</div>';
    _svPopupFitVideo(popup, v); // 面板展开后重算视频区（整窗高仍 ≤600）
    fetch(_cacheBust(srtUrl)).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.text();
    }).then(function (text) {
      // 异步回来时浮窗可能已关/换内容 —— 按 toggle 键兜底
      if (popup.hidden || popup.getAttribute('data-popup-key') !== ('s5-' + clipIndex)) return;
      var cues = _parseSrtCues(text);
      if (!cues.length) {
        subs.hidden = true; subs.innerHTML = '';
        _svPopupFitVideo(popup, v);
        return;
      }
      _renderSvSubsPanel(subs, cues);
      _svPopupFitVideo(popup, v);
      _driveSvSubs(v, subs, cues);
    }).catch(function () {
      if (popup.hidden || popup.getAttribute('data-popup-key') !== ('s5-' + clipIndex)) return;
      subs.hidden = true; subs.innerHTML = '';
      _svPopupFitVideo(popup, v);
      toast('字幕加载失败（仅播放视频）', 'warn');
    });
  }

  // REQ-20261019-215：解析粗剪 SRT → [{start, end, text}]（秒，按 start 升序）
  function _parseSrtCues(text) {
    var cues = [];
    var blocks = String(text || '').replace(/\r/g, '').split(/\n\n+/);
    var tsRe = /(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})/;
    for (var i = 0; i < blocks.length; i++) {
      var lines = blocks[i].split('\n');
      for (var j = 0; j < lines.length; j++) {
        var m = tsRe.exec(lines[j]);
        if (!m) continue;
        var start = (+m[1]) * 3600 + (+m[2]) * 60 + (+m[3])
          + parseInt((m[4] + '00').slice(0, 3), 10) / 1000;
        var end = (+m[5]) * 3600 + (+m[6]) * 60 + (+m[7])
          + parseInt((m[8] + '00').slice(0, 3), 10) / 1000;
        var txt = lines.slice(j + 1).join(' ').trim();
        if (end > start && txt) cues.push({ start: start, end: end, text: txt });
        break; // 一个块只认第一条时间轴行
      }
    }
    cues.sort(function (a, b) { return a.start - b.start; });
    return cues;
  }

  // REQ-20261019-215：字幕条渲染（时间码 + 文本；空 SRT 占位）
  function _renderSvSubsPanel(subs, cues) {
    if (!cues.length) {
      subs.innerHTML = '<div class="slirn-sv-subs-cue">（无字幕）</div>';
      return;
    }
    var html = '';
    for (var i = 0; i < cues.length; i++) {
      var c = cues[i];
      var mm = Math.floor(c.start / 60), ss = Math.floor(c.start % 60);
      var time = mm + ':' + (ss < 10 ? '0' : '') + ss;
      html += '<div class="slirn-sv-subs-cue" data-cue="' + i + '">'
        + '<span class="slirn-sv-subs-cue-time">' + time + '</span>'
        + '<span class="slirn-sv-subs-cue-text">' + esc(c.text) + '</span>'
        + '</div>';
    }
    subs.innerHTML = html;
  }

  // REQ-20261019-215：字幕驱动 —— timeupdate 找当前 cue，高亮 + 面板内居中滚动
  // （不用 scrollIntoView：它会连页外层滚动容器一起滚 —— REQ-215 设计决策）
  var _svSubsGen = 0;
  function _driveSvSubs(v, subs, cues) {
    var gen = ++_svSubsGen;
    var cur = -1;
    function cueAt(t) {
      for (var i = 0; i < cues.length; i++) {
        if (t >= cues[i].start && t < cues[i].end) return i;
      }
      return -1;
    }
    function cleanup() {
      v.removeEventListener('timeupdate', onTime);
      v.removeEventListener('emptied', cleanup);
      subs.querySelectorAll('.slirn-sv-subs-cue-active').forEach(function (el) {
        el.classList.remove('slirn-sv-subs-cue-active');
      });
    }
    function onTime() {
      if (gen !== _svSubsGen) return cleanup();
      var i = cueAt(v.currentTime || 0);
      if (i === cur) return;
      cur = i;
      var el = subs.querySelector('.slirn-sv-subs-cue[data-cue="' + i + '"]');
      if (!el) return;
      subs.querySelectorAll('.slirn-sv-subs-cue-active').forEach(function (x) {
        x.classList.remove('slirn-sv-subs-cue-active');
      });
      el.classList.add('slirn-sv-subs-cue-active');
      // REQ-20261019-217：滚动目标必须按面板坐标系算 —— 元素的 offsetTop 量的是
      // 最近 positioned 祖先（fixed 浮窗根，含标题栏+视频区高度），直接拿去当
      // 面板 scrollTop 会把高亮条滚出可见区。rect 差值 + 当前 scrollTop 才是
      // 面板内真实位置。
      var delta = el.getBoundingClientRect().top - subs.getBoundingClientRect().top;
      var top = delta + subs.scrollTop - (subs.clientHeight - el.offsetHeight) / 2;
      try {
        subs.scrollTo({ top: Math.max(0, top), behavior: 'smooth' });
      } catch (e) {
        subs.scrollTop = Math.max(0, top);
      }
    }
    v.addEventListener('timeupdate', onTime);
    v.addEventListener('emptied', cleanup); // 浮窗关闭/换媒体 → 驱动自清
    onTime();
  }

  // REQ-20261017-158：Stage 6 素材文件名点击查看 —— 视频（浮窗播放）
  function svClipViewVideo(btn) {
    if (!btn) return;
    var url = btn.getAttribute('data-url') || '';
    if (!url) return toast('文件不存在，无法查看', 'warn');
    var name = btn.getAttribute('title') || '视频';
    var popup = ensureSvVideoPopup();
    var key = 'view-' + url;
    if (!popup.hidden && popup.getAttribute('data-popup-key') === key) {
      _svPopupClose(popup);
      return;
    }
    popup.setAttribute('data-popup-key', key);
    popup.querySelector('.slirn-sv-video-popup-title').textContent =
      name.replace('（点击查看）', '');
    var v = popup.querySelector('video');
    // REQ-20261018-188：左上角 + 宽高比跟随视频（统一入口）
    _svPopupOpenVideo(popup, v, url);
  }

  // REQ-20261018-171：公共素材「👁 查看」—— 图片（封面/背景/头像）
  // 与音频（附加音频）浮窗预览，共用视频浮窗（可拖动、可调大小）。
  function svMatPreview(btn) {
    if (!btn) return;
    var url = btn.getAttribute('data-url') || '';
    if (!url) return toast('文件不存在，无法查看', 'warn');
    var isAudio = (btn.getAttribute('data-media') || 'image') === 'audio';
    var tag = isAudio ? 'audio' : 'img';
    var name = (btn.getAttribute('title') || '素材预览')
      .replace(/^查看已上传的\s*/, '');
    var popup = ensureSvVideoPopup();
    var key = 'mat-' + url;
    if (!popup.hidden && popup.getAttribute('data-popup-key') === key) {
      _svPopupClose(popup);
      return;
    }
    popup.setAttribute('data-popup-key', key);
    popup.querySelector('.slirn-sv-video-popup-title').textContent = name;
    popup.hidden = false;
    _svPopupShowMedia(popup, tag);
    var media = popup.querySelector(tag);
    // 音频无画面，用紧凑尺寸；图片沿用/首次设定尺寸
    if (isAudio || !popup.style.width) {
      var w = isAudio ? 380 : Math.min(640, Math.round(window.innerWidth * 0.6));
      var h = isAudio ? 170 : Math.min(560, Math.round(window.innerHeight * 0.6));
      popup.style.width = w + 'px';
      popup.style.height = h + 'px';
      popup.style.left = Math.max(8, window.innerWidth - w - 24) + 'px';
      popup.style.top = '80px';
    }
    media.removeAttribute('src');
    media.src = url;
    if (isAudio) {
      media.load();
      media.play().catch(function() {});  // 点击即用户手势，autoplay 允许
    }
  }

  // REQ-20261017-158：字幕文件名点击查看 —— 拉取 srt 文本弹层展示完整内容
  // （REQ-20261016-139 删过 sv-preview-srt 路由，当时按钮已删；REQ-157 的
  //  「查看 srt」按钮渲染了却无路由 → 本次一并补回）
  function svPreviewSrt(btn) {
    if (!btn) return;
    var url = btn.getAttribute('data-url') || '';
    if (!url) return toast('文件不存在，无法查看', 'warn');
    var popup = ensureSvSrtPopup();
    var pre = popup.querySelector('pre');
    var title = popup.querySelector('.slirn-sv-srt-popup-title');
    if (title) title.textContent = (btn.getAttribute('title') || '字幕')
      .replace('（点击查看）', '') || '字幕';
    pre.textContent = '加载中…';
    popup.hidden = false;
    fetch(url).then(function(r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.text();
    }).then(function(text) {
      pre.textContent = text || '（空文件）';
    }).catch(function(e) {
      pre.textContent = '读取失败: ' + e.message;
    });
  }

  function ensureSvSrtPopup() {
    var el = document.getElementById('slirn-sv-srt-popup');
    if (el) return el;
    el = document.createElement('div');
    el.id = 'slirn-sv-srt-popup';
    el.className = 'slirn-sv-srt-popup';
    el.hidden = true;
    el.innerHTML =
      '<div class="slirn-sv-srt-popup-head">' +
        '<span class="slirn-sv-srt-popup-title">字幕</span>' +
        '<button type="button" class="slirn-sv-srt-popup-close" ' +
          'aria-label="关闭" title="关闭">×</button>' +
      '</div>' +
      '<pre></pre>';
    document.body.appendChild(el);
    el.querySelector('.slirn-sv-srt-popup-close').addEventListener('click', function() {
      el.hidden = true;
    });
    // REQ-20261019-217：补拖拽（此前 head 只有 cursor:move 样式没绑事件）
    bindSvPopupDrag(el, '.slirn-sv-srt-popup-head', '.slirn-sv-srt-popup-close');
    return el;
  }

  // REQ-20261016-148：Stage 6 字幕优化阶段整体移除 — _renderStage6Rows/
  // _renderStage6Words/_stage6RowSave/stage6SaveDecisions/stage6Resplit/
  // stage6Unsplit/stage6BatchSearch/stage6BatchReplace/_escHtml 及
  // 行级采纳/拒绝委托监听随之删除。

  // REQ-20261013-105：Stage 7 精剪 v1 完整版
  // REQ-20261017-157：公共素材和参数设置区只有一份 — materials 只收 4 个公共槽；
  // 字体/画布/音频输入按 data-key / data-font-key 实际类名收集（此前收集选择器
  // 与 UI 类名不符，font/layout/audio 静默丢）。
  function _collectStage7Params() {
    var params = {
      materials: {}, font: {}, output: {}, audio: {}, preview: {}, layout: {},
      mentor: {}, title: {}, cover_title: {},
    };
    // materials（公共 4 槽：cover/bg/audio/avatar；
    // REQ-20261017-163 reference 移除；REQ-20261017-164 avatar 加入）
    document.querySelectorAll('.slirn-sv-mat-input').forEach(function(inp) {
      var slot = inp.getAttribute('data-mat-slot');
      if (slot) params.materials[slot] = inp.value || '';
    });
    // REQ-20261017-163：背景图说明（textarea → materials.bg_desc）
    document.querySelectorAll('.slirn-sv-mat-desc').forEach(function(ta) {
      var slot = ta.getAttribute('data-desc-slot');
      if (slot) params.materials[slot + '_desc'] = ta.value || '';
    });
    // REQ-20261017-164：导师文字（昵称/标题 文字+字体+大小）；
    // REQ-20261018-182：+ 颜色 / 描边（enabled 开关 / 颜色 / 宽度）
    var MENTOR_NUM_KEYS = ['nickname_size', 'title_size',
                           'nickname_outline_width', 'title_outline_width',
                           'title_line_spacing'];
    var MENTOR_BOOL_KEYS = ['nickname_outline_enabled', 'title_outline_enabled'];
    document.querySelectorAll('[data-mentor-key]').forEach(function(inp) {
      var key = inp.getAttribute('data-mentor-key');
      if (!key) return;
      if (MENTOR_BOOL_KEYS.indexOf(key) >= 0) {
        params.mentor[key] = !!inp.checked;
      } else if (MENTOR_NUM_KEYS.indexOf(key) >= 0) {
        params.mentor[key] = parseInt(inp.value, 10) || 0;
      } else {
        params.mentor[key] = inp.value || '';
      }
    });
    // REQ-20261018-177：视频标题（文字+字体+颜色+大小+阴影）；
    // 标题区域 X/Y/W/H 走下面 [data-key] 通用口径进 params.layout.title
    var TITLE_NUM_KEYS = ['size', 'shadow_opacity', 'shadow_depth', 'line_spacing'];
    document.querySelectorAll('[data-title-key]').forEach(function(inp) {
      var key = inp.getAttribute('data-title-key');
      if (!key) return;
      if (key === 'bold') {
        params.title[key] = !!inp.checked;
      } else if (TITLE_NUM_KEYS.indexOf(key) >= 0) {
        params.title[key] = parseFloat(inp.value) || 0;
      } else {
        params.title[key] = inp.value || '';
      }
    });
    // REQ-20261018-185：封面标题样式（data-ct-key；字体/颜色/大小/阴影，
    // 与视频标题独立）；封面标题区域 X/Y/W/H + enabled 走 [data-key] /
    // .slirn-fine-enabled 通用口径进 params.layout.cover_title
    var CT_NUM_KEYS = ['size', 'shadow_opacity', 'shadow_depth', 'line_spacing'];
    document.querySelectorAll('[data-ct-key]').forEach(function(inp) {
      var key = inp.getAttribute('data-ct-key');
      if (!key) return;
      if (key === 'bold') {
        params.cover_title[key] = !!inp.checked;
      } else if (CT_NUM_KEYS.indexOf(key) >= 0) {
        params.cover_title[key] = parseFloat(inp.value) || 0;
      } else {
        params.cover_title[key] = inp.value || '';
      }
    });
    // font：data-font-key（sel/num/chk/color/slider 全在此口径下）
    var FONT_NUM_KEYS = ['size', 'stroke_width', 'bg_opacity', 'bg_radius', 'offset_ms'];
    document.querySelectorAll('[data-font-key]').forEach(function(inp) {
      var key = inp.getAttribute('data-font-key');
      if (!key) return;
      if (key === 'bold' || key === 'bg_enabled') {
        params.font[key] = !!inp.checked;
      } else if (FONT_NUM_KEYS.indexOf(key) >= 0) {
        params.font[key] = parseFloat(inp.value) || 0;
      } else {
        params.font[key] = inp.value;
      }
    });
    // data-key（画布 X/Y/W/H + cover.duration + audio.*）：
    // video.x/subtitle.x/bg.x → layout 组；cover.duration → layout.cover；
    // audio.volume_db/fade_in_sec/fade_out_sec → audio 组（秒→毫秒）
    document.querySelectorAll('[data-key]').forEach(function(inp) {
      var key = inp.getAttribute('data-key');
      if (!key || key.indexOf('.') < 0) return;
      var head = key.split('.')[0], tail = key.split('.').slice(1).join('.');
      if (inp.type === 'checkbox') {
        // REQ-20261018-174：带点号布尔键（video.crop_aspect_lock）→ layout 组；
        // 此前 parseFloat 选不中 checkbox，勾选状态不落盘
        if (head === 'audio') return;
        var gchk = params.layout[head];
        if (!gchk || typeof gchk !== 'object') gchk = params.layout[head] = {};
        gchk[tail] = !!inp.checked;
        return;
      }
      var v = parseFloat(inp.value);
      if (isNaN(v)) return;
      if (head === 'audio') {
        if (tail === 'fade_in_sec') params.audio.fade_in_ms = Math.round(v * 1000);
        else if (tail === 'fade_out_sec') params.audio.fade_out_ms = Math.round(v * 1000);
        else params.audio[tail] = v;
      } else {
        // video/subtitle/bg 直接是组名；cover.duration 特判进 layout.cover
        var grp = params.layout[head];
        if (!grp || typeof grp !== 'object') grp = params.layout[head] = {};
        if (head === 'cover') grp.duration = v;
        else grp[tail] = v;
      }
    });
    // REQ-20261018-189：视频适配方式选择器已移除 —— 视频区域固定语义
    // 「截取的视频自动适应区域宽高」，不再收集适配键
    // REQ-20261018-173：启用复选框（data-key="video" 等无点号键，上面的
    // [data-key] 数值循环 parseFloat 选不中 → 此前保存根本不落盘）
    document.querySelectorAll('.slirn-fine-enabled[data-key]').forEach(function(chk) {
      var key = chk.getAttribute('data-key');
      if (!key) return;
      var grp = params.layout[key];
      if (!grp || typeof grp !== 'object') grp = params.layout[key] = {};
      grp.enabled = !!chk.checked;
    });
    // REQ-20261018-173：导师头像形状（circle / rect）
    var sh = document.querySelector('.slirn-sv-avatar-shape');
    if (sh) params.layout.avatar_shape = sh.value || 'circle';
    // output
    var res = document.querySelector('.slirn-sv-output-resolution');
    if (res) params.output.resolution = res.value || '720p';
    // audio.enabled
    var ae = document.querySelector('.slirn-sv-audio-enabled');
    if (ae) params.audio.enabled = !!ae.checked;
    // preview：每条处理区各有一个时长输入，取第一个作默认
    var pd = document.querySelector('.slirn-sv-preview-duration');
    if (pd) params.preview.duration_sec = parseInt(pd.value, 10) || 5;
    return params;
  }
  // 保存 6 组子字段
  function stage7SaveParams(btn, silent) {
    var ctx = innerCtx(); if (!ctx) return;
    var params = _collectStage7Params();
    // REQ-20261018-176：返回 Promise（预览前先落盘参数再合成，防竞态）；
    // silent=true 不弹保存 toast（预览链路内部使用）
    return postJSON(API + '/short_video_stage7_save_params', {
      task_id: ctx.taskId, project_id: ctx.projectId, params: params
    }).then(function(r) {
      if (silent) return r;
      if (r && r.ok) toast(r.toast || '💾 已保存');
      else toast('❌ ' + (r && r.error ? r.error : '保存失败'), 'error');
      return r;
    });
  }
  // REQ-20261018-173：参数区全量自动保存 —— 值 / 复选框 / 下拉改动即存
  // （input 防抖 800ms、focusout 立即落盘、change 兜底盖 select/checkbox）。
  // bg_desc 有 REQ-20261018-165 专用单字段通道，不在此重复触发；
  // 文件上传走自己的端点（type=file 排除）。
  // REQ-20261018-194：保存成功在「✓ 已自动保存」小字之外再弹轻量 toast
  // （用户实报「没有提示，好像就没有自动保存」——无反馈无法确认已落盘，
  // 且导出前看不到保存是否已完成）；不重渲染（防打断输入焦点）不变。
  var _svParamsAutosaveTimer = null;
  function _stage7AutosaveNow() {
    if (_svParamsAutosaveTimer) {
      clearTimeout(_svParamsAutosaveTimer);
      _svParamsAutosaveTimer = null;
    }
    var ctx = innerCtx(); if (!ctx) return;
    postJSON(API + '/short_video_stage7_save_params', {
      task_id: ctx.taskId, project_id: ctx.projectId,
      params: _collectStage7Params()
    }).then(function(r) {
      var hint = document.querySelector('[data-autosave-hint]');
      if (r && r.ok) {
        if (hint) hint.textContent = '✓ 已自动保存 ' +
          new Date().toTimeString().slice(0, 8);
        toast('💾 参数已自动保存');
      } else {
        if (hint) hint.textContent = '⚠ 自动保存失败';
        toast('❌ ' + ((r && r.error) || '参数自动保存失败'), 'error');
      }
    });
  }
  function _stage7AutosaveSchedule() {
    if (_svParamsAutosaveTimer) clearTimeout(_svParamsAutosaveTimer);
    _svParamsAutosaveTimer = setTimeout(_stage7AutosaveNow, 800);
  }
  function _isStage7ParamTarget(el) {
    if (!el || el.type === 'file' || !el.classList) return false;
    if (el.classList.contains('slirn-sv-mat-desc')) return false;  // REQ-165 专用通道
    return el.classList.contains('slirn-sv-mat-input') ||
           el.hasAttribute('data-mentor-key') ||
           el.hasAttribute('data-title-key') ||
           el.hasAttribute('data-ct-key') ||
           el.hasAttribute('data-font-key') ||
           el.hasAttribute('data-key') ||
           el.classList.contains('slirn-sv-avatar-shape') ||
           el.classList.contains('slirn-sv-output-resolution') ||
           el.classList.contains('slirn-sv-audio-enabled');
  }
  // REQ-20261019-204：透明度滑块 ↔ 数字输入 双向联动（数字在滑块左侧，
  // 可手输）。数字带同一 data-*-key → 联动后两值一致，collect 以文档序
  // 后者（滑块）为准；程序赋值不触发 input 事件 → 无递归。
  document.addEventListener('input', function(e) {
    var t = e.target;
    if (!t || !t.classList || !t.closest) return;
    var row = t.closest('.slirn-fine-slider-row');
    if (!row) return;
    var num = row.querySelector('.slirn-fine-num');
    var rng = row.querySelector('.slirn-fine-font-slider');
    if (!num || !rng) return;
    if (t === rng) num.value = rng.value;
    else if (t === num) rng.value = num.value;
  });
  document.addEventListener('input', function(e) {
    if (_isStage7ParamTarget(e.target)) _stage7AutosaveSchedule();
  });
  document.addEventListener('change', function(e) {
    if (_isStage7ParamTarget(e.target)) _stage7AutosaveSchedule();
  });
  document.addEventListener('focusout', function(e) {
    if (_svParamsAutosaveTimer && _isStage7ParamTarget(e.target)) {
      _stage7AutosaveNow();
    }
  });

  // REQ-20261018-174：视频源裁剪对齐长视频 —— 预设按钮 + 16:9 宽高联动 +
  // 比例显示（此前 UI 只有预设死按钮和占位文案，X/Y/W/H 无处可填）。
  // crop_* 以源画面 1920×1080 设计空间为准；联动改的是另一个输入框的值
  // （程序赋值不触发 input 事件 → 无递归），autosave collect 落盘时两个都读。
  function _svCropInput(k) {
    return document.querySelector('.slirn-fine-num[data-key="video.' + k + '"]');
  }
  function _svUpdateCropAspect() {
    var w = parseFloat((_svCropInput('crop_w') || {}).value) || 0;
    var h = parseFloat((_svCropInput('crop_h') || {}).value) || 0;
    var el = document.querySelector('.slirn-sv-crop-aspect-val');
    if (el) el.textContent = w > 0 ? (h / w).toFixed(3) : '—';
  }
  function _svSyncCropAspect(from) {
    var wInp = _svCropInput('crop_w');
    var hInp = _svCropInput('crop_h');
    if (!wInp || !hInp) return;
    if (from === 'w') {
      hInp.value = Math.max(1, Math.round((parseFloat(wInp.value) || 0) * 9 / 16));
    } else {
      wInp.value = Math.max(1, Math.round((parseFloat(hInp.value) || 0) * 16 / 9));
    }
  }
  // REQ-20261018-198：按视频区域宽高比锁定 —— 基准取显示区域 W/H
  // （如 1080×624 → 0.578）。改 crop 宽/高，另一边按区域比例自动算；
  // 区域 W/H 手改后基准实时变（同时按新比例重算 crop 高，保一致性）。
  // 程序赋值不触发 input 事件 → 无递归；autosave collect 两边都读。
  function _svRegionInput(k) {
    return document.querySelector('.slirn-fine-num[data-key="video.' + k + '"]');
  }
  function _svCropLocks() {
    return {
      link169: document.querySelector('.slirn-sv-crop-aspect-link'),
      linkRegion: document.querySelector('.slirn-sv-crop-region-link')
    };
  }
  function _svSyncCropRegion(from) {
    var wInp = _svCropInput('crop_w');
    var hInp = _svCropInput('crop_h');
    var rw = parseFloat((_svRegionInput('w') || {}).value) || 0;
    var rh = parseFloat((_svRegionInput('h') || {}).value) || 0;
    if (!wInp || !hInp || !(rw > 0) || !(rh > 0)) return;
    if (from === 'w') {
      hInp.value = Math.max(1, Math.round(
        (parseFloat(wInp.value) || 0) * rh / rw));
    } else {
      wInp.value = Math.max(1, Math.round(
        (parseFloat(hInp.value) || 0) * rw / rh));
    }
  }
  function _svUpdateRegionAspect() {
    var rw = parseFloat((_svRegionInput('w') || {}).value) || 0;
    var rh = parseFloat((_svRegionInput('h') || {}).value) || 0;
    var el = document.querySelector('.slirn-sv-region-aspect-val');
    if (el) el.textContent = rw > 0 ? (rh / rw).toFixed(3) : '—';
  }
  document.addEventListener('input', function(e) {
    var t = e.target;
    if (!t || !t.classList || !t.classList.contains('slirn-fine-num')) return;
    var key = t.getAttribute('data-key') || '';
    if (key !== 'video.crop_w' && key !== 'video.crop_h') return;
    var locks = _svCropLocks();
    if (locks.linkRegion && locks.linkRegion.checked)
      _svSyncCropRegion(key === 'video.crop_w' ? 'w' : 'h');
    else if (locks.link169 && locks.link169.checked)
      _svSyncCropAspect(key === 'video.crop_w' ? 'w' : 'h');
    _svUpdateCropAspect();
  });
  // 显示区域 W/H 手改：更新基准显示；区域锁开着则按新比例重算 crop 高
  document.addEventListener('input', function(e) {
    var t = e.target;
    if (!t || !t.classList || !t.classList.contains('slirn-fine-num')) return;
    var key = t.getAttribute('data-key') || '';
    if (key !== 'video.w' && key !== 'video.h') return;
    _svUpdateRegionAspect();
    var locks = _svCropLocks();
    if (locks.linkRegion && locks.linkRegion.checked) _svSyncCropRegion('w');
  });
  document.addEventListener('change', function(e) {
    var t = e.target;
    if (!t || !t.classList) return;
    var is169 = t.classList.contains('slirn-sv-crop-aspect-link');
    var isRegion = t.classList.contains('slirn-sv-crop-region-link');
    if (!is169 && !isRegion) return;
    // REQ-20261018-198：两把锁互斥（16:9 与区域比是两个不同基准，
    // 同时勾选会打架；勾一把自动取消另一把，collect 落盘同步生效）
    var locks = _svCropLocks();
    if (is169 && t.checked && locks.linkRegion) locks.linkRegion.checked = false;
    if (isRegion && t.checked && locks.link169) locks.link169.checked = false;
    if (t.checked) {
      if (isRegion) _svSyncCropRegion('w');  // 开锁即按当前宽重算高
      else _svSyncCropAspect('w');
    }
    _svUpdateCropAspect();
  });
  // REQ-20261018-179：圆形头像 W↔H 联动。渲染以区域短边为直径（正圆），
  // 单改宽度圆不变（min(w,h)=h）→ 圆形时改一边另一边跟着同步；
  // 矩形不联动。程序赋值不触发 input 事件 → 无递归；collect 落盘两边都读。
  function _svAvatarShapeSel() {
    return document.querySelector('.slirn-sv-avatar-shape');
  }
  function _svAvatarInput(k) {
    return document.querySelector('.slirn-fine-num[data-key="avatar.' + k + '"]');
  }
  function _svAvatarLinkSync(from) {
    var wInp = _svAvatarInput('w'), hInp = _svAvatarInput('h');
    if (!wInp || !hInp) return;
    var v = parseFloat((from === 'w' ? wInp : hInp).value);
    if (!(v > 0)) return;
    (from === 'w' ? hInp : wInp).value = Math.max(1, Math.round(v));
  }
  document.addEventListener('input', function(e) {
    var t = e.target;
    if (!t || !t.classList || !t.classList.contains('slirn-fine-num')) return;
    var key = t.getAttribute('data-key') || '';
    if (key !== 'avatar.w' && key !== 'avatar.h') return;
    var sel = _svAvatarShapeSel();
    if (!sel || sel.value === 'circle') _svAvatarLinkSync(key === 'avatar.w' ? 'w' : 'h');
  });
  document.addEventListener('change', function(e) {
    var t = e.target;
    if (!t || !t.classList ||
        !t.classList.contains('slirn-sv-avatar-shape')) return;
    if (t.value === 'circle') _svAvatarLinkSync('w');  // 切回圆形即按宽对齐高
  });
  function svCropPreset(btn) {
    var preset = (btn && btn.getAttribute('data-preset')) || '';
    // 与长视频 fineCropPreset 同表：1:1 居中 = (1920-1080)/2 = 420 起
    var defaults = {
      'full': { crop_x: 0,   crop_y: 0, crop_w: 1920, crop_h: 1080 },
      '16x9': { crop_x: 0,   crop_y: 0, crop_w: 1920, crop_h: 1080 },
      '1x1':  { crop_x: 420, crop_y: 0, crop_w: 1080, crop_h: 1080 },
    }[preset];
    if (!defaults) return;
    ['crop_x', 'crop_y', 'crop_w', 'crop_h'].forEach(function(k) {
      var inp = _svCropInput(k);
      if (inp) inp.value = defaults[k];
    });
    _svUpdateCropAspect();
    _stage7AutosaveSchedule();
    toast('✅ 已应用「' + (preset === 'full' ? '全幅' :
      preset === '16x9' ? '16:9 居中' : '1:1 居中') + '」');
  }
  // REQ-20261018-167：导出参数为 JSON 文件（对齐长视频 fine-export-params：
  // 先保存表单 → 服务端取全量参数 → Blob 触发下载；素材文件本体不进文件）
  // REQ-20261018-194：保存必须**等落盘再导出**——此前 fire-and-forget，
  // 导出请求可能跑在保存前 → 导出旧参数
  function stage7ExportParams(btn) {
    var ctx = innerCtx(); if (!ctx) return;
    var old = btn.textContent;
    btn.disabled = true;
    btn.textContent = '⏳ 导出中…';
    Promise.resolve(stage7SaveParams(btn, true)).then(function() {
      return postJSON(API + '/short_video_stage7_params_export', {
        task_id: ctx.taskId, project_id: ctx.projectId
      });
    }).then(function(r) {
      btn.disabled = false;
      btn.textContent = old;
      if (!r || !r.ok) return toast('❌ ' + (r && r.error ? r.error : '导出失败'), 'error');
      try {
        var blob = new Blob([r.content], {type: r.mime || 'application/json'});
        var url = URL.createObjectURL(blob);
        var a = document.createElement('a');
        a.href = url;
        a.download = r.filename || 'sv_fine_params.json';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
        toast('✅ 参数已导出到 ' + a.download);
      } catch (e) {
        toast('❌ 触发下载失败: ' + e.message, 'error');
      }
    });
  }
  // REQ-20261018-167：从 JSON 文件导入参数（对齐长视频 fine-import-params：
  // 动态 file input → FileReader → POST 覆盖当前组；不动素材文件路径）
  function stage7ImportParams(btn) {
    var ctx = innerCtx(); if (!ctx) return;
    var old = btn.textContent;
    btn.disabled = true;
    var input = document.createElement('input');
    input.type = 'file';
    input.accept = '.json,application/json';
    input.style.display = 'none';
    input.addEventListener('change', function(e) {
      var file = e.target.files && e.target.files[0];
      if (!file) { btn.disabled = false; return; }
      // 64KB 上限（实际参数远低于此）
      if (file.size > 64 * 1024) {
        toast('❌ 文件过大（>' + (file.size / 1024).toFixed(1) + 'KB），拒绝导入', 'error');
        btn.disabled = false;
        return;
      }
      var reader = new FileReader();
      reader.onload = function(ev) {
        btn.textContent = '⏳ 导入中…';
        postJSON(API + '/short_video_stage7_params_import', {
          task_id: ctx.taskId, project_id: ctx.projectId, content: ev.target.result
        }).then(function(r) {
          btn.disabled = false;
          btn.textContent = old;
          if (!r || !r.ok) return toast('❌ ' + (r && r.error ? r.error : '导入失败'), 'error');
          toast(r.toast || '✅ 已导入参数');
          openProject(ctx.taskId, ctx.projectId);  // 刷新面板反映新参数
        });
      };
      reader.readAsText(file, 'utf-8');
    });
    input.click();
  }
  // 异步导出（仿 Stage 5 setInterval 轮询）
  // REQ-20261017-157：按钮在每条处理区内 — 带 index 只渲染该条
  function stage7Export(btn) {
    var ctx = innerCtx(); if (!ctx) return;
    var idx = parseInt(btn.getAttribute('data-clip-index') || '0', 10);
    var setBtn = function(t, d) { btn.textContent = t; btn.disabled = !!d; };
    var status = document.getElementById('slirn-sv-stage7-status');
    var msg = status ? status.querySelector('.slirn-sv-stage7-msg') : null;
    var bar = status ? status.querySelector('.slirn-progress-bar') : null;
    var show = function(t) { if (msg) { msg.style.display = ''; msg.textContent = t; } };
    // REQ-20261018-194：先等参数落盘再启动导出——此前 fire-and-forget，
    // 保存 POST 与导出 POST 竞态，导出可能用旧参数开渲（用户实报
    // 「有些参数修改之后没有立即生效」的根因）
    setBtn(idx ? '🎬 导出中… #' + idx : '🎬 导出中…', true);
    show('⏳ 正在启动…');
    Promise.resolve(stage7SaveParams(btn, true)).then(function() {
      return postJSON(API + '/short_video_stage7_export', {
        task_id: ctx.taskId, project_id: ctx.projectId, index: idx
      });
    }).then(function(r) {
      if (!r || !r.ok) {
        setBtn('💾 导出最终视频', false);
        show('');
        if (msg) msg.style.display = 'none';
        return toast('❌ ' + (r && r.error ? r.error : '启动失败'), 'error');
      }
      toast(r.toast || '🎬 精剪已启动');
      var timer = setInterval(function() {
        postJSON(API + '/short_video_stage7_export_status', {
          task_id: ctx.taskId, project_id: ctx.projectId
        }).then(function(s) {
          if (!s || !s.ok || !s.job) return;
          var j = s.job;
          if (j.state === 'running') {
            var pct = Math.max(1, Math.min(99, Math.round(j.progress || 0)));
            setBtn('🎬 导出中… ' + pct + '%', true);
            if (bar) bar.style.width = pct + '%';
            show('⏳ ' + (j.stage || '导出中') + ' ' + pct + '%');
            return;
          }
          clearInterval(timer);
          if (j.state === 'done') {
            setBtn('💾 重新导出', false);
            if (bar) bar.style.width = '100%';
            show('✅ 精剪完成');
            toast('✅ 精剪完成');
            openProject(ctx.taskId, ctx.projectId);
          } else {
            setBtn('💾 重新导出', false);
            show('❌ ' + (j.error || '失败'));
            toast('❌ ' + (j.error || '失败'), 'error');
          }
        });
      }, 2000);
    });
  }
  // 预览（按 index 调 ffmpeg 切段）
  // REQ-20261017-157：时长输入在每条处理区内 — 就地取值
  function stage7Preview(btn) {
    var ctx = innerCtx(); if (!ctx) return;
    var idx = parseInt(btn.getAttribute('data-clip-index') || '0', 10);
    if (!idx) return;
    var card = btn.closest ? btn.closest('.slirn-stage6-clip') : null;
    var scope = card || document;
    var dur = parseInt(((scope.querySelector('.slirn-sv-preview-duration') || {}).value) || '5', 10);
    btn.disabled = true;
    // REQ-20261018-176：预览=按当前区域参数混编合成 —— 参数区自动保存有
    // 800ms 防抖，可能尚未落盘；先同步保存一次再请求预览，确保用最新参数渲染
    Promise.resolve(stage7SaveParams(btn, true)).then(function() {
      return postJSON(API + '/short_video_stage7_preview', {
        task_id: ctx.taskId, project_id: ctx.projectId, index: idx, duration_sec: dur
      });
    }).then(function(r) {
      btn.disabled = false;
      if (!r || !r.ok) return toast('❌ ' + (r && r.error ? r.error : '预览失败'), 'error');
      toast(r.toast || '🎬 预览已生成');
      // REQ-20261017-162：改走 /slirn/api/short_video_file（kind=stage6）。
      // ⚠️ Gradio 文件路由只服务经其上传的文件，ffmpeg 磁盘产物
      // 直接 403 —— 旧实现就是黑屏 0:00 的根因。
      var url = r.file_url;
      if (!url) {
        var fname = String(r.preview_path || '').split(/[\\/]/).pop();
        if (!fname) return toast('❌ 预览地址缺失', 'error');
        url = API + '/short_video_file?task_id=' + encodeURIComponent(ctx.taskId) +
              '&project_id=' + encodeURIComponent(ctx.projectId) +
              '&kind=stage6&name=' + encodeURIComponent(fname);
      }
      // 弹共享视频浮窗（REQ-132/158 同款：可拖动、可调大小）
      var popup = ensureSvVideoPopup();
      popup.setAttribute('data-popup-key', 's6p-' + idx);
      popup.querySelector('.slirn-sv-video-popup-title').textContent =
        '精剪预览 #' + idx + '（截取 ' + dur + 's）';
      var v = popup.querySelector('video');
      // REQ-20261018-188：左上角 + 宽高比跟随视频（统一入口）
      _svPopupOpenVideo(popup, v, url);
    });
  }
  // REQ-20261017-157：单条处理区 — 标题就地保存 + 视频/字幕手动上传
  function stage6TitleSave(inp) {
    var ctx = innerCtx(); if (!ctx) return;
    var idx = parseInt(inp.getAttribute('data-clip-index') || '0', 10);
    if (!idx) return;
    postJSON(API + '/short_video_stage7_clip_save', {
      task_id: ctx.taskId, project_id: ctx.projectId,
      index: idx, title: inp.value || ''
    }).then(function(r) {
      if (r && r.ok) toast('🏷️ 标题已保存');
      else toast('❌ 标题保存失败', 'error');
    });
  }
  function stage6ClipUpload(inp) {
    var ctx = innerCtx(); if (!ctx) return;
    var idx = parseInt(inp.getAttribute('data-clip-index') || '0', 10);
    var slot = inp.getAttribute('data-mat-slot') || '';
    var f = inp.files && inp.files[0];
    if (!idx || !slot || !f) return;
    var fd = new FormData();
    fd.append('task_id', ctx.taskId);
    fd.append('project_id', ctx.projectId);
    fd.append('index', String(idx));
    fd.append('slot', slot);
    fd.append('file', f);
    inp.disabled = true;
    postForm(API + '/short_video_stage7_clip_upload', fd).then(function(r) {
      inp.disabled = false;
      inp.value = '';
      if (r && r.ok) {
        toast(r.toast || '✅ 已上传');
        openProject(ctx.taskId, ctx.projectId);
      } else {
        toast('❌ ' + ((r && r.error) || '上传失败'), 'error');
      }
    });
  }
  // REQ-20261018-183：清除单条 clip 手动素材覆盖 —— 视频/字幕「♻️ 继承」
  // （回退 Stage 5）与 bg/audio「🗑 删除」（回退公共素材）共用一个端点
  function stage6ClipClear(btn) {
    var ctx = innerCtx(); if (!ctx) return;
    var idx = parseInt(btn.getAttribute('data-clip-index') || '0', 10);
    var slot = btn.getAttribute('data-mat-slot') || '';
    if (!idx || !slot) return;
    btn.disabled = true;
    postJSON(API + '/short_video_stage7_clip_clear', {
      task_id: ctx.taskId, project_id: ctx.projectId,
      index: idx, slot: slot
    }).then(function(r) {
      btn.disabled = false;
      if (r && r.ok) {
        toast(r.toast || '♻️ 已恢复继承');
        openProject(ctx.taskId, ctx.projectId);
      } else {
        toast('❌ ' + ((r && r.error) || '操作失败'), 'error');
      }
    });
  }

  // REQ-20261017-159：公共素材上传（cover/bg/audio，全部 clip 共用）
  // REQ-20261017-160：抽共享核心 svMatUploadFile —— 点击选择文件与拖拽
  // 放置两条入口走同一套上传逻辑。
  function svMatUploadFile(slot, f, srcEl) {
    if (!slot || !f) return;
    var ctx = innerCtx(); if (!ctx) return;
    var fd = new FormData();
    fd.append('task_id', ctx.taskId);
    fd.append('project_id', ctx.projectId);
    fd.append('slot', slot);
    fd.append('file', f);
    if (srcEl) srcEl.disabled = true;
    postForm(API + '/short_video_stage7_material_upload', fd).then(function(r) {
      if (srcEl) {
        srcEl.disabled = false;
        srcEl.value = '';
      }
      if (r && r.ok) {
        toast(r.toast || '✅ 已上传');
        openProject(ctx.taskId, ctx.projectId);
      } else {
        toast('❌ ' + ((r && r.error) || '上传失败'), 'error');
      }
    });
  }
  function svMatUpload(inp) {
    svMatUploadFile(inp.getAttribute('data-mat-slot') || '',
                    inp.files && inp.files[0], inp);
  }
  // REQ-20261017-160：整行拖拽放置区 —— 文件拖到 .slirn-sv-mat-row 上
  // 高亮（虚线框），松手即上传到该行槽位。
  document.addEventListener('dragover', function(e) {
    var row = e.target && e.target.closest ?
      e.target.closest('.slirn-sv-mat-row') : null;
    if (!row) return;
    e.preventDefault();  // 允许放置
    if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy';
    row.classList.add('slirn-sv-mat-over');
  });
  document.addEventListener('dragleave', function(e) {
    var row = e.target && e.target.closest ?
      e.target.closest('.slirn-sv-mat-row') : null;
    if (!row) return;
    // 子元素间移动不算离行（dragleave 会在每个子元素上冒一次）
    if (e.relatedTarget && row.contains(e.relatedTarget)) return;
    row.classList.remove('slirn-sv-mat-over');
  });
  document.addEventListener('drop', function(e) {
    var row = e.target && e.target.closest ?
      e.target.closest('.slirn-sv-mat-row') : null;
    if (!row) return;
    e.preventDefault();  // 阻止浏览器直接打开文件
    row.classList.remove('slirn-sv-mat-over');
    var f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
    if (!f) return;
    svMatUploadFile(row.getAttribute('data-slot') || '', f, null);
  });
  // REQ-20261018-180：标题/导师头衔多行 textarea —— 输入时自动长高
  // （初始高度由服务端 rows 按存量行数给；此处只管打字过程中的长高）
  document.addEventListener('input', function(e) {
    var t = e.target;
    if (!t || t.tagName !== 'TEXTAREA' || !t.classList) return;
    if (!t.classList.contains('slirn-stage6-clip-title') &&
        !t.classList.contains('slirn-fine-font-text')) return;
    t.style.height = 'auto';
    t.style.height = (t.scrollHeight + 2) + 'px';
  });
  document.addEventListener('change', function(e) {
    var t = e.target;
    if (!t || !t.classList) return;
    if (t.classList.contains('slirn-stage6-clip-title')) return stage6TitleSave(t);
    if (t.classList.contains('slirn-stage6-clip-upload')) return stage6ClipUpload(t);
    if (t.classList.contains('slirn-sv-mat-upload')) return svMatUpload(t);
    // REQ-20261018-166：检测算法切换 → 阈值行显隐（仅 pixel 用）；
    // 阈值 select 联动手输框
    if (t.classList.contains('slirn-sv-bg-detect-algo')) {
      var row = document.querySelector('.slirn-fine-bg-detect-threshold-row');
      if (row) row.style.display = t.value === 'pixel' ? 'inline-flex' : 'none';
      return;
    }
    if (t.classList.contains('slirn-sv-bg-detect-threshold-sel')) {
      var num = document.querySelector('.slirn-sv-bg-detect-threshold-num');
      if (num) num.value = t.value;
      return;
    }
  });
  // REQ-20261018-165：背景图说明自动保存 —— 输入停 600ms（或 blur）即把
  // bg_desc 单字段 POST 到 save_params（端点按组浅合并，不碰其他参数；
  // 此前只在点「导出参数」时随全量收集落盘，中途刷新会丢）。
  var _svDescSaveTimer = null;
  function _svDescSaveNow() {
    var ctx = innerCtx(); if (!ctx) return;
    var ta = document.querySelector('.slirn-sv-mat-desc[data-desc-slot="bg"]');
    if (!ta) return;
    postJSON(API + '/short_video_stage7_save_params', {
      task_id: ctx.taskId, project_id: ctx.projectId,
      params: {materials: {bg_desc: ta.value || ''}}
    }).then(function(r) {
      if (r && r.ok) toast('📝 说明已自动保存');
      else toast('❌ ' + ((r && r.error) || '背景图说明自动保存失败'), 'error');
    });
  }
  document.addEventListener('input', function(e) {
    var t = e.target;
    if (!t || !t.classList ||
        !t.classList.contains('slirn-sv-mat-desc')) return;
    if (_svDescSaveTimer) clearTimeout(_svDescSaveTimer);
    _svDescSaveTimer = setTimeout(_svDescSaveNow, 600);
  });
  document.addEventListener('blur', function(e) {
    var t = e.target;
    if (!t || !t.classList ||
        !t.classList.contains('slirn-sv-mat-desc')) return;
    if (_svDescSaveTimer) { clearTimeout(_svDescSaveTimer); _svDescSaveTimer = null; }
    _svDescSaveNow();
  }, true);  // blur 不冒泡，捕获阶段接

  // bg 检测（REQ-20261018-166 对齐长视频：算法可选 + 阈值仅 pixel 用）
  function stage7DetectBg(btn) {
    var ctx = innerCtx(); if (!ctx) return;
    var algoSel = document.querySelector('.slirn-sv-bg-detect-algo');
    var algo = (algoSel && algoSel.value) || 'center_expand';
    var thrNum = document.querySelector('.slirn-sv-bg-detect-threshold-num');
    var thrSel = document.querySelector('.slirn-sv-bg-detect-threshold-sel');
    var thr = (thrNum && thrNum.value) || (thrSel && thrSel.value) || 250;
    btn.disabled = true;
    postJSON(API + '/short_video_stage7_detect_bg', {
      task_id: ctx.taskId, project_id: ctx.projectId,
      algorithm: algo, threshold: parseInt(thr, 10) || 250
    }).then(function(r) {
      btn.disabled = false;
      if (!r || !r.ok) return toast('❌ ' + (r && r.error ? r.error : '检测失败'), 'error');
      toast(r.toast || '🔍 检测完成');
      openProject(ctx.taskId, ctx.projectId);
    });
  }
  // 清空检测缓存
  function stage7ClearDetect(btn) {
    var ctx = innerCtx(); if (!ctx) return;
    btn.disabled = true;
    postJSON(API + '/short_video_stage7_detect_bg_clear', {
      task_id: ctx.taskId, project_id: ctx.projectId
    }).then(function(r) {
      btn.disabled = false;
      if (!r || !r.ok) return toast('❌ ' + (r && r.error ? r.error : '清空失败'), 'error');
      toast(r.toast || '🗑 已清空检测缓存');
      openProject(ctx.taskId, ctx.projectId);
    });
  }
  // 应用 detected 到 layout.video
  function stage7ApplyDetected(btn) {
    var ctx = innerCtx(); if (!ctx) return;
    btn.disabled = true;
    postJSON(API + '/short_video_stage7_apply_detected', {
      task_id: ctx.taskId, project_id: ctx.projectId
    }).then(function(r) {
      btn.disabled = false;
      if (!r || !r.ok) return toast('❌ ' + (r && r.error ? r.error : '填充失败'), 'error');
      toast(r.toast || '✅ 已填充');
      openProject(ctx.taskId, ctx.projectId);
    });
  }
  // AI 智能布局
  function stage7ParseLayout(btn) {
    var ctx = innerCtx(); if (!ctx) return;
    // REQ-20261017-163：参考图槽移除 —— AI 布局输入改背景图 + 背景图说明
    var bg = (document.querySelector('.slirn-sv-mat-input[data-mat-slot="bg"]') || {}).value || '';
    var bgDesc = (document.querySelector('.slirn-sv-mat-desc[data-desc-slot="bg"]') || {}).value || '';
    btn.disabled = true;
    postJSON(API + '/short_video_stage7_parse_layout', {
      task_id: ctx.taskId, project_id: ctx.projectId,
      bg_path: bg, bg_desc: bgDesc
    }).then(function(r) {
      btn.disabled = false;
      if (!r || !r.ok) return toast('❌ ' + (r && r.error ? r.error : 'AI 布局失败'), 'error');
      toast(r.toast || '🤖 AI 布局已应用');
      openProject(ctx.taskId, ctx.projectId);
    });
  }
  // BGM 列表（弹窗选）
  function stage7ListBgm(btn) {
    var ctx = innerCtx(); if (!ctx) return;
    postJSON(API + '/short_video_stage7_list_bgm', {}).then(function(r) {
      if (!r || !r.ok) return toast('❌ 获取 BGM 失败', 'error');
      var bgms = r.bgms || [];
      if (!bgms.length) return toast('❌ 暂无系统 BGM', 'error');
      var opts = bgms.map(function(b, i) {
        return (i + 1) + '. ' + (b.label || b.filename) + (b.available ? '' : ' (缺失)');
      }).join('\n');
      var pick = window.prompt('选 BGM（输入序号）：\n' + opts, '1');
      var n = parseInt(pick, 10);
      if (!n || n < 1 || n > bgms.length) return;
      var b = bgms[n - 1];
      if (!b.available) return toast('❌ BGM 文件缺失', 'error');
      postJSON(API + '/short_video_stage7_select_bgm', {
        task_id: ctx.taskId, project_id: ctx.projectId, bgm_id: b.id
      }).then(function(r2) {
        if (r2 && r2.ok) { toast(r2.toast || '🎵 已选'); openProject(ctx.taskId, ctx.projectId); }
        else toast('❌ ' + (r2 && r2.error ? r.error : '选 BGM 失败'), 'error');
      });
    });
  }
  // ---- 模板 6 端点 ----
  function stage7ProfilesList(btn) {
    postJSON(API + '/short_video_stage7_profiles_list', {}).then(function(r) {
      if (!r || !r.ok) return toast('❌ 列表失败', 'error');
      var sel = document.querySelector('.slirn-sv-profile-select');
      if (!sel) return;
      sel.innerHTML = '<option value="">-- 选模板 --</option>' +
        (r.profiles || []).map(function(p) {
          return '<option value="' + p.id + '">' + p.name + '</option>';
        }).join('');
      toast('📋 共 ' + (r.profiles || []).length + ' 个模板');
    });
  }
  function stage7ProfilesSave(btn) {
    var name = (document.querySelector('.slirn-sv-profile-name') || {}).value || '';
    if (!name.trim()) return toast('请输入模板名', 'error');
    var params = _collectStage7Params();
    postJSON(API + '/short_video_stage7_profiles_save', {
      name: name, params: params
    }).then(function(r) {
      if (r && r.ok) { toast(r.toast || '💾 已保存'); stage7ProfilesList(btn); }
      else toast('❌ ' + (r && r.error ? r.error : '保存失败'), 'error');
    });
  }
  function stage7ProfilesApply(btn) {
    var ctx = innerCtx(); if (!ctx) return;
    var sel = document.querySelector('.slirn-sv-profile-select');
    var pid = sel ? sel.value : '';
    if (!pid) return toast('先选模板', 'error');
    postJSON(API + '/short_video_stage7_profiles_apply', {
      task_id: ctx.taskId, project_id: ctx.projectId, profile_id: pid
    }).then(function(r) {
      if (r && r.ok) { toast(r.toast || '📋 已应用'); openProject(ctx.taskId, ctx.projectId); }
      else toast('❌ ' + (r && r.error ? r.error : '应用失败'), 'error');
    });
  }
  function stage7ProfilesRename(btn) {
    var sel = document.querySelector('.slirn-sv-profile-select');
    var pid = sel ? sel.value : '';
    if (!pid) return toast('先选模板', 'error');
    var opt = sel.querySelector('option[value="' + pid + '"]');
    var newName = window.prompt('新名字', opt ? opt.textContent : '');
    if (!newName) return;
    postJSON(API + '/short_video_stage7_profiles_rename', {
      profile_id: pid, new_name: newName
    }).then(function(r) {
      if (r && r.ok) { toast(r.toast || '✏️ 已重命名'); stage7ProfilesList(btn); }
      else toast('❌ ' + (r && r.error ? r.error : '重命名失败'), 'error');
    });
  }
  function stage7ProfilesExport(btn) {
    var sel = document.querySelector('.slirn-sv-profile-select');
    var pid = sel ? sel.value : '';
    if (!pid) return toast('先选模板', 'error');
    postJSON(API + '/short_video_stage7_profiles_export', {
      profile_id: pid
    }).then(function(r) {
      if (!r || !r.ok) return toast('❌ 导出失败', 'error');
      var blob = new Blob([JSON.stringify(r.profile, null, 2)],
                          {type: 'application/json'});
      var url = URL.createObjectURL(blob);
      var a = document.createElement('a');
      a.href = url; a.download = (r.profile.name || 'template') + '.json';
      document.body.appendChild(a); a.click();
      setTimeout(function() { URL.revokeObjectURL(url); document.body.removeChild(a); }, 100);
      toast('📤 已导出');
    });
  }
  function stage7ProfilesDelete(btn) {
    var sel = document.querySelector('.slirn-sv-profile-select');
    var pid = sel ? sel.value : '';
    if (!pid) return toast('先选模板', 'error');
    if (!window.confirm('确定删除该模板？')) return;
    postJSON(API + '/short_video_stage7_profiles_delete', {
      profile_id: pid
    }).then(function(r) {
      if (r && r.ok) { toast(r.toast || '🗑️ 已删除'); stage7ProfilesList(btn); }
      else toast('❌ 删除失败', 'error');
    });
  }


  // REQ-20261016-148：Stage 6 字幕优化阶段整体移除 — stage5Asr/
  // _renderStage6PaneAfterAsr/_renderSrtRowsClient/_parseSrtEntriesClient/
  // stage6AutoSave/stage6LineEdit/stage6LineToggle/stage6LineResplit/
  // stage6RebuildSrt/_fmtSrtTime/stage6Run 随之删除（预览改读 data-url）。

  // ---------- 预览小窗（SRT / 视频） ----------

  function onPreviewKey(ev) {
    if (ev.key === 'Escape') closePreview();
  }
  function closePreview() {
    var ov = document.getElementById('slirn-sv-preview-overlay');
    if (!ov) return;
    ov.querySelectorAll('video, audio').forEach(function(m) { try { m.pause(); } catch (e) {} });
    ov.remove();
    document.removeEventListener('keydown', onPreviewKey);
  }
  // 修订七：预览窗口可拖动位置 + 可调整大小（用户验收）。
  // 拖动 = 标题栏 mousedown 把卡片从 flex 居中切到绝对定位后跟手移动；
  // 调整大小 = 右下角把柄改 width（视频 max-width:100% 自动跟随），有持久化 key 则记忆。
  function _enableWinDrag(win, head) {
    head.addEventListener('mousedown', function(ev) {
      if (ev.button !== 0) return;
      if (ev.target.closest && ev.target.closest('button')) return;
      ev.preventDefault();
      var rect = win.getBoundingClientRect();
      var parent = win.offsetParent || document.body;
      var prect = parent.getBoundingClientRect();
      var baseL = rect.left - prect.left, baseT = rect.top - prect.top;
      win.style.position = 'absolute';
      win.style.margin = '0';
      win.style.left = baseL + 'px';
      win.style.top = baseT + 'px';
      var x0 = ev.clientX, y0 = ev.clientY;
      function move(e2) {
        var l = Math.max(0, Math.min(prect.width - 80, baseL + e2.clientX - x0));
        var t = Math.max(0, Math.min(prect.height - 40, baseT + e2.clientY - y0));
        win.style.left = l + 'px';
        win.style.top = t + 'px';
      }
      function up() {
        document.removeEventListener('mousemove', move);
        document.removeEventListener('mouseup', up);
      }
      document.addEventListener('mousemove', move);
      document.addEventListener('mouseup', up);
    });
  }
  function _enableWinResize(win, handle, opts) {
    opts = opts || {};
    var minW = opts.minW || 300;
    if (opts.persistKey) {
      try {
        var saved = JSON.parse(localStorage.getItem(opts.persistKey) || 'null');
        if (saved && saved.width > minW) {
          win.style.width = saved.width + 'px';
          win.style.maxWidth = 'none';
        }
      } catch (e) { /* ignore */ }
    }
    handle.addEventListener('mousedown', function(ev) {
      if (ev.button !== 0) return;
      ev.preventDefault();
      ev.stopPropagation();
      var startW = win.getBoundingClientRect().width;
      var x0 = ev.clientX;
      win.style.maxWidth = 'none';  // 解除 max-width 上限，允许拉大
      function move(e2) {
        var w = Math.max(minW, Math.min(window.innerWidth - 24, startW + e2.clientX - x0));
        win.style.width = w + 'px';
      }
      function up() {
        document.removeEventListener('mousemove', move);
        document.removeEventListener('mouseup', up);
        if (opts.persistKey) {
          try {
            localStorage.setItem(opts.persistKey, JSON.stringify({ width: win.getBoundingClientRect().width }));
          } catch (e) { /* ignore */ }
        }
      }
      document.addEventListener('mousemove', move);
      document.addEventListener('mouseup', up);
    });
  }

  function previewByUrl(url, name, body) {
    closePreview();
    if (!url) return;
    var ov = document.createElement('div');
    ov.className = 'slirn-modal-overlay';
    ov.id = 'slirn-sv-preview-overlay';
    ov.innerHTML =
      '<div class="slirn-modal-card slirn-sv-preview-card" role="dialog" aria-modal="true">' +
        '<div class="slirn-sv-preview-head">' +
          '<div class="slirn-sv-preview-title">' + esc(name || '预览') + '</div>' +
          '<button class="slirn-btn-mini" data-action="sv-preview-close" title="关闭 (Esc)">✕</button>' +
        '</div>' +
        '<div class="slirn-sv-preview-body">' + body + '</div>' +
        '<div class="slirn-sv-preview-resize" title="拖动调整窗口大小">◢</div>' +
      '</div>';
    ov.addEventListener('click', function(ev2) {
      if (ev2.target === ov) closePreview();
    });
    document.body.appendChild(ov);
    document.addEventListener('keydown', onPreviewKey);
    var card = ov.querySelector('.slirn-sv-preview-card');
    _enableWinDrag(card, ov.querySelector('.slirn-sv-preview-head'));
    _enableWinResize(card, ov.querySelector('.slirn-sv-preview-resize'), { minW: 300 });
  }

  // REQ-20261016-139：移除 previewSrtByUrl（「查看 ASR 字幕」按钮已删除）
  // 字幕直接显示在按钮下方（per-row 编辑器），不需要再弹窗看

  function previewVideo(btn) {
    var url = btn.getAttribute('data-url') || '';
    var name = btn.getAttribute('data-name') || '';
    previewByUrl(url, name, '<video controls autoplay playsinline src="' + esc(url) + '"></video>');
  }

  // REQ-20261004-UX：Stage 3 高亮片段预览 —— 浏览器侧 seek 到
  // start_ms 起点、timeupdate 到 end_ms 自动暂停。
  // **v2：可拖动悬浮窗口**，无遮盖层，用户可同时看 Stage 3 字幕行比对。
  function stage3PreviewHl(btn) {
    var url = btn.getAttribute('data-src-url') || '';
    if (!url) return toast('请先在 Stage 1 选源视频', 'error');
    var startMs = parseInt(btn.getAttribute('data-start-ms') || '0', 10) || 0;
    var endMs = parseInt(btn.getAttribute('data-end-ms') || '0', 10) || 0;
    var title = btn.getAttribute('data-title') || '预览片段';

    // 取父 hl-row 的 data-sub-text（每条 highlight 的完整字幕文本）
    var row = btn.closest ? btn.closest('.slirn-stage3-hl-row') : null;
    var subText = (row && row.getAttribute('data-sub-text')) || '';

    closeFloatingPreview();  // 防止多个悬浮窗叠加
    var fp = document.createElement('div');
    fp.className = 'slirn-sv-floating-player';
    fp.id = 'slirn-sv-floating-player';
    fp.innerHTML =
      '<div class="slirn-sv-fp-head">' +
        '<span class="slirn-sv-fp-title">🎬 ' + esc(title) + '</span>' +
        '<button class="slirn-sv-fp-close" data-action="sv-fp-close" title="关闭 (Esc)">✕</button>' +
      '</div>' +
      '<div class="slirn-sv-fp-body">' +
        '<video id="slirn-sv-fp-video" class="slirn-sv-fp-video" ' +
          'controls autoplay playsinline preload="auto" src="' + esc(url) + '"></video>' +
        '<div class="slirn-sv-fp-subs" id="slirn-sv-fp-subs"></div>' +
      '</div>' +
      '<div class="slirn-sv-fp-resize" title="拖动调整窗口大小">◢</div>';
    document.body.appendChild(fp);

    // 字幕块：从 data-sub-text 还原成行
    var subsEl = fp.querySelector('#slirn-sv-fp-subs');
    var lines = subText.split('\n').filter(function(l) { return l.trim(); });
    if (lines.length === 0) {
      subsEl.innerHTML = '<div class="slirn-sv-fp-subs-empty">无字幕</div>';
    } else {
      // 解析 `[src] text`，渲染成 <div class="slirn-sv-fp-sub">
      var html = '';
      for (var i = 0; i < lines.length; i++) {
        var line = lines[i];
        var m = line.match(/^\[(\d+)\]\s*(.*)$/);
        if (m) {
          html += '<div class="slirn-sv-fp-sub">' +
                  '<span class="slirn-sv-fp-sub-src">[' + esc(m[1]) + ']</span>' +
                  esc(m[2]) + '</div>';
        } else {
          html += '<div class="slirn-sv-fp-sub">' + esc(line) + '</div>';
        }
      }
      subsEl.innerHTML = html;
    }

    // 拖动支持：header 即拖把；持久化位置到 localStorage
    var head = fp.querySelector('.slirn-sv-fp-head');
    var storageKey = 'slirn.sv.fp.pos';
    try {
      var saved = JSON.parse(localStorage.getItem(storageKey) || 'null');
      if (saved && typeof saved.left === 'number' && typeof saved.top === 'number') {
        fp.style.left = saved.left + 'px';
        fp.style.top = saved.top + 'px';
        fp.style.right = 'auto';
      }
    } catch (e) { /* 持久化失败忽略 */ }

    var dragState = null;
    head.addEventListener('mousedown', function(ev) {
      if (ev.target.closest && ev.target.closest('[data-action="sv-fp-close"]')) return;
      ev.preventDefault();
      var rect = fp.getBoundingClientRect();
      dragState = {
        startX: ev.clientX,
        startY: ev.clientY,
        baseLeft: rect.left,
        baseTop: rect.top,
      };
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });
    function onMove(ev) {
      if (!dragState) return;
      var dx = ev.clientX - dragState.startX;
      var dy = ev.clientY - dragState.startY;
      var left = Math.max(0, Math.min(window.innerWidth - 80, dragState.baseLeft + dx));
      var top = Math.max(0, Math.min(window.innerHeight - 40, dragState.baseTop + dy));
      fp.style.left = left + 'px';
      fp.style.top = top + 'px';
      fp.style.right = 'auto';
    }
    function onUp() {
      if (!dragState) return;
      try {
        var rect = fp.getBoundingClientRect();
        localStorage.setItem(storageKey, JSON.stringify({ left: rect.left, top: rect.top }));
      } catch (e) { /* ignore */ }
      dragState = null;
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
    }

    // 修订七：右下角把柄调窗口大小（宽度驱动，视频跟随），记忆到 localStorage
    _enableWinResize(fp, fp.querySelector('.slirn-sv-fp-resize'), {
      minW: 280, persistKey: 'slirn.sv.fp.size',
    });

    // 关闭按钮（事件冒泡到全局 click 分发，但这里直接绑，省一次查找）
    fp.querySelector('[data-action="sv-fp-close"]').addEventListener('click', function() {
      closeFloatingPreview();
    });

    // Esc 关闭：注册一次性监听（多个悬浮窗叠加时只关最上层——目前已强制唯一）
    var onEsc = function(ev) {
      if (ev.key === 'Escape') {
        closeFloatingPreview();
        document.removeEventListener('keydown', onEsc);
      }
    };
    document.addEventListener('keydown', onEsc);

    // video 逐段播放引擎（修订八）：按 data-segments 的切割窗逐段 seek 播放，
    // 与拼接产物一致；无 segments 时退回旧的「组起点连播到组终点」。
    var v = fp.querySelector('#slirn-sv-fp-video');
    if (!v) return;
    var segs = null;
    try {
      var raw = btn.getAttribute('data-segments');
      if (raw) {
        var parsed = JSON.parse(raw);
        if (Object.prototype.toString.call(parsed) === '[object Array]' && parsed.length) {
          segs = parsed.filter(function(sg) {
            return sg && isFinite(sg[0]) && isFinite(sg[1]) && sg[1] > sg[0];
          }).map(function(sg) { return [sg[0] / 1000, sg[1] / 1000]; });
          if (!segs.length) segs = null;
        }
      }
    } catch (e) { segs = null; }
    var startSec = (segs ? segs[0][0] : startMs / 1000);
    var cleared = false;
    var segIdx = 0;
    var manual = false;   // 用户自己拖进度条 → 停止自动逐段推进
    // REQ-20261013-113：标记「下一次 seeking 是我自己发起的自动跳转」——
    // 旧版用「t 跳出现段」启发式判定手动，越界就把 manual 置 true，导致
    // 行序重排后 segs 顺序≠时间序时第一段就 false-trigger，之后再也不跳段。
    // 现在：自动跳转前先把 expectedSeek=true；seeking 触发时若为 true，
    // 说明是我们自己跳的，吞掉这次事件；只有 user 真的拖进度条才会置 manual。
    var expectedSeek = false;
    function onSeeking() {
      if (cleared) return;
      if (expectedSeek) { expectedSeek = false; return; }
      if (!segs) return;
      if (segIdx >= segs.length) return;
      var t = v.currentTime;
      var cur = segs[segIdx];
      if (t < cur[0] - 0.05 || t > cur[1] + 0.05) manual = true;
    }
    function onTime() {
      if (cleared || manual) return;
      if (segs) {
        if (v.currentTime >= segs[segIdx][1]) {
          segIdx++;
          if (segIdx >= segs.length) {
            v.pause();
            cleared = true;
            v.removeEventListener('timeupdate', onTime);
            v.removeEventListener('seeking', onSeeking);
          } else {
            expectedSeek = true;
            try { v.currentTime = segs[segIdx][0]; } catch (e) { expectedSeek = false; /* ignore */ }
          }
        }
      } else if (v.currentTime >= endSecFallback()) {
        v.pause();
        cleared = true;
        v.removeEventListener('timeupdate', onTime);
      }
    }
    function endSecFallback() {
      return Math.max(startMs / 1000 + 0.05, endMs / 1000);
    }
    function onLoaded() {
      expectedSeek = true;
      try {
        v.currentTime = startSec;
        v.play().catch(function(){});
      } catch (e) { expectedSeek = false; /* seek 偶发 NotSupported 忽略 */ }
      v.addEventListener('timeupdate', onTime);
      if (segs) v.addEventListener('seeking', onSeeking);
    }
    if (v.readyState >= 1) {
      onLoaded();
    } else {
      v.addEventListener('loadedmetadata', onLoaded, { once: true });
    }
  }

  // 关悬浮窗：拿掉 DOM 并暂停 video
  function closeFloatingPreview() {
    var fp = document.getElementById('slirn-sv-floating-player');
    if (!fp) return;
    fp.querySelectorAll('video, audio').forEach(function(m) { try { m.pause(); } catch (e) {} });
    fp.remove();
  }

  // ---------- REQ-20261004-stage3-restructure：字幕行拖拽排序 ----------
  // 用 HTML5 drag-and-drop API；drop 后立刻调 stage3_reorder_subtitles 端点。
  // 视觉反馈：拖动中 opacity:0.4；目标行上方加高亮蓝线。

  function _getDragCtx() {
    return window._slirnSvDragCtx || (window._slirnSvDragCtx = { srcEl: null, hlIndex: null, srcIndex: null });
  }

  function bindSubtitleSort(container) {
    // container 是 .slirn-stage3-hl-preview（一个 highlight 内的字幕行容器）
    var hlIndex = container.getAttribute('data-hl-index');
    if (!hlIndex) return;
    // 给每个 .slirn-stage3-hl-sub 注册拖拽事件（一次性注册；后续 DOM 变化由 initSubtitleSortAll 兜底）
    var subs = container.querySelectorAll('.slirn-stage3-hl-sub');
    subs.forEach(function(el) {
      if (el.getAttribute('data-sort-bound') === '1') return;
      el.setAttribute('data-sort-bound', '1');
      el.addEventListener('dragstart', function(ev) {
        var ctx = _getDragCtx();
        ctx.srcEl = el;
        ctx.hlIndex = hlIndex;
        ctx.srcIndex = el.getAttribute('data-src-index');
        try { ev.dataTransfer.setData('text/plain', String(ctx.srcIndex || '')); } catch (e) {}
        ev.dataTransfer.effectAllowed = 'move';
        el.classList.add('slirn-stage3-hl-sub-dragging');
      });
      el.addEventListener('dragend', function() {
        el.classList.remove('slirn-stage3-hl-sub-dragging');
        container.querySelectorAll('.slirn-stage3-hl-sub-drop-target').forEach(function(x) {
          x.classList.remove('slirn-stage3-hl-sub-drop-target');
        });
        var ctx = _getDragCtx();
        ctx.srcEl = null;
      });
      el.addEventListener('dragover', function(ev) {
        ev.preventDefault();
        ev.dataTransfer.dropEffect = 'move';
        el.classList.add('slirn-stage3-hl-sub-drop-target');
      });
      el.addEventListener('dragleave', function() {
        el.classList.remove('slirn-stage3-hl-sub-drop-target');
      });
      el.addEventListener('drop', function(ev) {
        ev.preventDefault();
        var ctx = _getDragCtx();
        if (!ctx.srcEl || !ctx.srcIndex) return;
        var targetSrc = el.getAttribute('data-src-index');
        if (String(targetSrc) === String(ctx.srcIndex)) return; // 原地拖动忽略
        // 立即在 DOM 上重排（乐观更新）—— 失败时 revert
        var movedEl = ctx.srcEl;
        var parent = movedEl.parentNode;
        parent.insertBefore(movedEl, el); // 移到目标之前
        // 调端点持久化
        var root = document.getElementById('slirn-short-video-inner');
        var taskId = root ? (root.getAttribute('data-task-id') || '') : '';
        var projectId = root ? (root.getAttribute('data-project-id') || '') : '';
        if (!taskId || !projectId) {
          showToast('缺少 task_id / project_id，拖拽未持久化', 'error');
          return;
        }
        fetch('/slirn/api/short_video_stage3_reorder_subtitles', {
          method: 'POST',
          credentials: 'include',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            task_id: taskId,
            project_id: projectId,
            hl_index: parseInt(ctx.hlIndex || '0', 10),
            src_index: parseInt(ctx.srcIndex || '0', 10),
            before_src_index: parseInt(targetSrc || '0', 10),
          })
        }).then(function(r) { return r.json().then(function(j) { return { ok: r.ok, body: j }; }); })
          .then(function(data) {
            if (!data.ok) {
              // revert DOM
              parent.insertBefore(movedEl, el.nextSibling);
              showToast('字幕重排失败：' + (data.body && data.body.error || '未知错误'), 'error');
            } else {
              // 同步刷新「↩️ 还原顺序」按钮状态：行序变了，禁用态可能已失效
              // （per slirn-real-time-vs-persistent-state：拖拽改 server 状态后
              // 必须就地同步 UI 状态，不能只靠下次 openProject 重渲染）
              _refreshRestoreBtn(parseInt(ctx.hlIndex || '0', 10));
              showToast('字幕顺序已更新（下游 Stage 4-7 已重置）', 'ok');
            }
          })
          .catch(function(err) {
            parent.insertBefore(movedEl, el.nextSibling);
            showToast('字幕重排失败：' + err, 'error');
          });
      });
    });
  }

  function initSubtitleSortAll() {
    // 给当前可见的所有 .slirn-stage3-hl-preview 容器绑定拖拽（幂等）
    document.querySelectorAll('.slirn-stage3-hl-preview').forEach(bindSubtitleSort);
  }

  // REQ-20261013-115：拖拽完成后，必须就地刷新「↩️ 还原顺序」按钮的禁用态。
  // 否则用户看到行序已乱但按钮仍 disabled（被 server 旧渲染的 HTML 锁住），
  // 体验上等同「按钮失灵」。计算口径与 short_video_ui.py:571 的
  // is_original = nos == sorted(nos) 一致（读每个 .slirn-stage3-hl-no 节点的
  // 文本号 = dict['no']）。
  function _refreshRestoreBtn(hlIndex) {
    if (!hlIndex) return;
    var btn = document.querySelector(
      '.slirn-stage3-hl-row[data-hl-index="' + hlIndex + '"] ' +
      '[data-action="sv-stage3-restore-order"]'
    );
    if (!btn) return;
    var rows = document.querySelectorAll(
      '.slirn-stage3-hl-preview[data-hl-index="' + hlIndex + '"] ' +
      '.slirn-stage3-hl-sub'
    );
    var nos = [];
    rows.forEach(function(r) {
      var noEl = r.querySelector('.slirn-stage3-hl-no');
      if (!noEl) return;
      var m = (noEl.textContent || '').match(/(\d+)/);
      if (m) nos.push(parseInt(m[1], 10));
    });
    var isOriginal = nos.length > 0 && nos.every(function(n, i, arr) {
      return i === 0 || n >= arr[i - 1];
    });
    if (isOriginal) {
      btn.setAttribute('disabled', 'disabled');
      btn.setAttribute('title', '当前已是大模型最初顺序');
    } else {
      btn.removeAttribute('disabled');
      btn.setAttribute('title', '把该条短视频的字幕行还原为大模型最初给出的顺序（编号不变）');
    }
  }

  // ---------- REQ-20261012-099 修订六：还原字幕顺序（回到大模型最初顺序） ----------
  // 修订五的「片段行 ⇕ 拖拽拼接」已随「N 组 = N 条独立短视频」语义移除；
  // 组间没有拼接顺序，组内行拖拽（bindSubtitleSortAll）保留。
  function stage3RestoreOrder(btn) {
    var ctx = innerCtx(); if (!ctx) return;
    var hlIndex = parseInt(btn.getAttribute('data-hl-index') || '0', 10);
    if (!hlIndex) { toast('缺少片段编号，未执行还原', 'error'); return; }
    postJSON(API + '/short_video_stage3_restore_order', {
      task_id: ctx.taskId, project_id: ctx.projectId, hl_index: hlIndex
    }).then(function(r) {
      if (r && r.ok) {
        toast(r.toast || ('#' + hlIndex + ' 已还原为大模型最初顺序'));
        openProject(ctx.taskId, ctx.projectId);
      } else toast((r && r.error) || '还原顺序失败', 'error');
    });
  }

  // 修订八：删除一条短视频 —— 必须先弹确认框，用户确认后才真正删除
  function stage3RemoveHl(btn) {
    var ctx = innerCtx(); if (!ctx) return;
    var hlIndex = parseInt(btn.getAttribute('data-hl-index') || '0', 10);
    if (!hlIndex) { toast('缺少片段编号，未执行删除', 'error'); return; }
    var row = btn.closest ? btn.closest('.slirn-stage3-hl-row') : null;
    var titleEl = row ? row.querySelector('.slirn-stage3-hl-title') : null;
    var title = titleEl ? (titleEl.value || titleEl.textContent || '').trim() : '';
    var msg = '确定要删除短视频 #' + hlIndex +
      (title ? '「' + title + '」' : '') + ' 吗？删除后需要重新解析才能恢复。';
    if (!window.confirm(msg)) return;
    postJSON(API + '/short_video_stage3_remove_hl', {
      task_id: ctx.taskId, project_id: ctx.projectId, hl_index: hlIndex
    }).then(function(r) {
      if (r && r.ok) {
        toast(r.toast || ('#' + hlIndex + ' 已删除'));
        openProject(ctx.taskId, ctx.projectId);
      } else toast((r && r.error) || '删除失败', 'error');
    });
  }

  // ---------- 列表过滤 / 搜索（REQ-20261012-099 修订四，对齐长视频列表） ----------

  // 「我的任务 / 全部任务」radio 切换 → 按新 scope 重拉列表
  document.addEventListener('change', function(e) {
    if (e.target && e.target.name === 'slirn-sv-scope') {
      loadList(e.target.value);
    }
  });

  // 搜索框 client-side 过滤：按短视频任务名（项目名）/ 任务 ID / 项目 ID 匹配卡片
  function _filterSvCards(query) {
    var q = (query || '').trim().toLowerCase();
    var grid = document.querySelector('.slirn-sv-grid');
    if (!grid) return;
    var matchedCount = 0;
    grid.querySelectorAll('.slirn-sv-card').forEach(function(card) {
      var tid = (card.getAttribute('data-task-id') || '').toLowerCase();
      var pid = (card.getAttribute('data-project-id') || '').toLowerCase();
      var headEl = card.querySelector('.slirn-sv-card-head strong');
      var mutedEl = card.querySelector('.slirn-sv-muted');
      var name = headEl ? (headEl.textContent || '').toLowerCase() : '';
      var meta = mutedEl ? (mutedEl.textContent || '').toLowerCase() : '';
      var match = !q || (tid + ' ' + pid + ' ' + name + ' ' + meta).indexOf(q) >= 0;
      card.style.display = match ? '' : 'none';
      if (match) matchedCount++;
    });
    var existing = grid.querySelector('.slirn-search-empty');
    if (existing) existing.remove();
    if (q && matchedCount === 0) {
      var empty = document.createElement('div');
      empty.className = 'slirn-empty slirn-search-empty';
      empty.style.gridColumn = '1/-1';
      empty.innerHTML = '<div class="slirn-empty-icon">🔍</div>' +
        '<div class="slirn-empty-text">没有匹配「' + esc(q) + '」的短视频任务</div>';
      grid.appendChild(empty);
    }
  }
  // 事件代理挂监听（列表重渲染会换掉 input 元素，不能直接绑）
  document.addEventListener('input', function(e) {
    if (e.target && e.target.id === 'slirn-sv-search') {
      _filterSvCards(e.target.value);
    }
  });

  // REQ-20261019-214：点 Stage 4 字幕行 → 从该行起预览播放。行内按钮/内嵌
  // 输入框的点击不触发；删除行（data-deleted）与 LLM 删除行（data-llm-deleted）
  // 不在播放列表里，点了不打扰。拖拽排序 drop 后不产生 click，不冲突。
  document.addEventListener('click', function(ev) {
    if (!ev.target.closest) return;
    var row = ev.target.closest('.slirn-stage4-sub-row');
    if (!row) return;
    if (ev.target.closest('[data-action]')) return;
    if (ev.target.closest('.slirn-stage4-sub-editbox')) return;
    if (row.getAttribute('data-deleted') === '1'
        || row.getAttribute('data-llm-deleted') === '1') return;
    var idx = parseInt(row.getAttribute('data-eff-idx') || '', 10);
    if (!isFinite(idx)) return;
    var card = row.closest('.slirn-stage4-clip');
    var hl = card ? parseInt(card.getAttribute('data-hl-index') || '0', 10) : 0;
    var ctx = innerCtx();
    if (!card || !hl || !ctx) return;
    _st4StartFromRow(ctx, card, hl, idx);
  });

  // ---------- 点击分发 ----------

  document.addEventListener('click', function(ev) {
    // REQ-20261022-144：日志分页按钮（data-sv-pager）
    var pg = ev.target.closest ? ev.target.closest('[data-sv-pager]') : null;
    if (pg) {
      ev.preventDefault();
      var act = pg.getAttribute('data-sv-pager');
      var pagerEl = document.getElementById('slirn-sv-logs-pager');
      var totalN = parseInt((pagerEl && pagerEl.getAttribute('data-total')) || '0', 10);
      var totalPagesN = Math.max(1, Math.ceil(totalN / svLogsState.pageSize));
      if (act === 'first') svLogsState.page = 1;
      else if (act === 'prev') svLogsState.page = Math.max(1, svLogsState.page - 1);
      else if (act === 'next') svLogsState.page = Math.min(totalPagesN, svLogsState.page + 1);
      else if (act === 'last') svLogsState.page = totalPagesN;
      svLoadLogs();
      return;
    }
    // REQ-20261022-144：日志筛选 chip（类型 / 状态 / 模式）
    var chip = ev.target.closest ? ev.target.closest('[data-sv-log-kind], [data-sv-log-status], [data-sv-log-auto]') : null;
    if (chip) {
      ev.preventDefault();
      if (chip.hasAttribute('data-sv-log-kind')) {
        var ck = chip.getAttribute('data-sv-log-kind');
        var ix = svLogsState.kinds.indexOf(ck);
        if (ix >= 0) svLogsState.kinds.splice(ix, 1); else svLogsState.kinds.push(ck);
        chip.classList.toggle('active');
      } else if (chip.hasAttribute('data-sv-log-status')) {
        var cs = chip.getAttribute('data-sv-log-status');
        var ix2 = svLogsState.statuses.indexOf(cs);
        if (ix2 >= 0) svLogsState.statuses.splice(ix2, 1); else svLogsState.statuses.push(cs);
        chip.classList.toggle('active');
      } else if (chip.hasAttribute('data-sv-log-auto')) {
        svLogsState.auto = chip.getAttribute('data-sv-log-auto') || 'any';
        var cbox = chip.closest('.slirn-logs-filter');
        if (cbox) {
          cbox.querySelectorAll('.slirn-chip[data-sv-log-auto]').forEach(function (b) {
            b.classList.toggle('active', b === chip);
          });
        }
      }
      svLogsState.page = 1;
      svLoadLogs();
      return;
    }
    var btn = ev.target.closest ? ev.target.closest('[data-action]') : null;
    if (!btn) return;
    var action = btn.getAttribute('data-action');
    if (!action || action.indexOf('sv-') !== 0) return;
    ev.preventDefault();

    // 列表 / 创建 / 打开
    if (action === 'sv-new') {
      // 「➕ 新建短视频任务」（从 hero 挪来）：滚动到创建表单并聚焦项目名
      var createSec = document.getElementById('slirn-sv-create');
      if (createSec && createSec.scrollIntoView) {
        createSec.scrollIntoView({behavior: 'smooth', block: 'center'});
      }
      var nameInput = document.getElementById('slirn-sv-create-name');
      if (nameInput && nameInput.focus) nameInput.focus();
      return;
    }
    if (action === 'sv-stage-nav') {
      return switchSvStage(btn.getAttribute('data-stage-key') || '');
    }
    // REQ-20261022-144：执行日志
    if (action === 'sv-logs-open') { return svOpenLogs(); }
    if (action === 'sv-logs-refresh') { svLogsState.page = 1; return svLoadLogs(); }
    if (action === 'sv-logs-clear-kinds') {
      svLogsState.kinds = [];
      document.querySelectorAll('.slirn-chip[data-sv-log-kind].active').forEach(function (b) {
        b.classList.remove('active');
      });
      svLogsState.page = 1; return svLoadLogs();
    }
    if (action === 'sv-logs-clear-statuses') {
      svLogsState.statuses = [];
      document.querySelectorAll('.slirn-chip[data-sv-log-status].active').forEach(function (b) {
        b.classList.remove('active');
      });
      svLogsState.page = 1; return svLoadLogs();
    }
    if (action === 'sv-refresh') return loadList();
    if (action === 'sv-create') return createProject();
    if (action === 'sv-open') {
      return openProject(btn.getAttribute('data-task-id'), btn.getAttribute('data-project-id'));
    }
    if (action === 'sv-back') return loadList();
    if (action === 'sv-delete') return deleteProject(btn);

    // 上传（Stage 1 选源前可上传）
    if (action === 'sv-upload') return uploadMaterial();

    // 6 阶段
    if (action === 'sv-stage1-select') return stage1Select();
    if (action === 'sv-stage2-run') return stage2Run(btn);
    if (action === 'sv-stage3-run') return stage3Run(btn);
    if (action === 'sv-stage3-restore-order') return stage3RestoreOrder(btn);
    if (action === 'sv-stage3-preview') return stage3PreviewHl(btn);
    if (action === 'sv-stage4-optimize-run') return stage4OptimizeRun(btn);
    // REQ-20261013-108：Stage 4 手动编辑 / 预览 / 跳 Stage 5
    // （REQ-20261016-146：提示词 6 个 action 已随提示词 UI 一并移除）
    if (action === 'sv-stage4-line-edit') return stage4LineEdit(btn);
    if (action === 'sv-stage4-line-resplit') return stage4LineResplit(btn);
    // REQ-20261017-153：内嵌输入框 💾 保存 / ✖ 还原
    if (action === 'sv-stage4-line-save') return stage4LineSave(btn);
    if (action === 'sv-stage4-line-discard') return stage4LineDiscard(btn);
    if (action === 'sv-stage4-line-unsplit') return stage4LineUnsplit(btn);
    if (action === 'sv-stage4-line-toggle') return stage4LineToggle(btn);
    // REQ-20261019-210：↑/↓ 推行（拖拽走 bindStage4Sort 事件，不经派发器）
    if (action === 'sv-stage4-line-move') return stage4LineMove(btn);
    if (action === 'sv-stage4-preview') return stage4Preview(btn);
    // REQ-20261017-155：Stage 4 / Stage 5 单条重跑（已优化→单条重新优化；
    // 已合成→单条重新合成）
    if (action === 'sv-stage4-optimize-one') return stage4OptimizeOne(btn);
    if (action === 'sv-stage5-export-one') return stage5ExportOne(btn);
    if (action === 'sv-stage3-rerun') return stage3RerunOne(btn);
    if (action === 'sv-stage3-remove-hl') return stage3RemoveHl(btn);
    if (action === 'sv-stage3-hl-tab') return switchHlTab(btn.getAttribute('data-hl-index'));
    if (action === 'sv-stage4-hl-tab') return switchHlTab(btn.getAttribute('data-hl-index'));
    if (action === 'sv-stage3-save-default') return stage3SaveDefault();
    if (action === 'sv-stage3-save-history') return stage3SaveHistory();
    if (action === 'sv-stage3-load-prompt') return stage3LoadPrompt(btn);
    if (action === 'sv-stage3-use-system-prompt') return stage3UseSystemPrompt(btn);
    if (action === 'sv-stage3-toggle-hist') return stage3ToggleHist(btn);
    if (action === 'sv-stage3-clear-prompt') return stage3ClearPrompt();
    if (action === 'sv-stage3-delete-prompt') return stage3DeletePrompt(btn);
    if (action === 'sv-stage3-upload-image') return stage3UploadImage();
    if (action === 'sv-stage3-remove-image') return stage3RemoveImage();
    if (action === 'sv-stage4-run') return stage4Run();
    // REQ-20261016-148：字幕优化阶段移除 — sv-stage5-asr / sv-stage6-line-* /
    // sv-stage6-run 路由随之删除
    // REQ-20261013-103：Stage 5 粗剪 异步导出/删除/连续 SRT/单条 SRT
    if (action === 'sv-stage5-export') return stage5Export(btn);
    if (action === 'sv-stage5-delete') return stage5Delete(btn);
    if (action === 'sv-stage5-subs-copy') return stage5SubsCopy(btn);
    if (action === 'sv-stage5-subs-download') return stage5SubsDownload(btn);
    // REQ-20261019-227：行内「📋 复制 SRT」（sv-stage5-clip-subs-copy）按需求
    // 移除；新增成片字幕提取 / 展开 / 还原三个路由
    if (action === 'sv-stage5-subs-extract') return stage5SubsExtract(btn);
    if (action === 'sv-stage5-subs-reextract') return stage5SubsExtract(btn);
    if (action === 'sv-stage5-subs-toggle') return stage5SubsToggle(btn);
    if (action === 'sv-stage5-subs-restore') return stage5SubsRestore(btn);
    if (action === 'sv-stage5-clip-subs-download') return stage5ClipSubsDownload(btn);
    // REQ-20261016-132：Stage 5 视频预览浮窗
    if (action === 'sv-stage5-preview') return svStage5Preview(btn);
    if (action === 'sv-stage6-final-preview') return svStage6FinalPreview(btn);
    if (action === 'sv-stage6-cover-export') return stage6CoverExport(btn);
    // REQ-20261016-148：Stage 6 字幕优化 5 个路由（save-decisions/resplit/
    // unsplit/batch-search/batch-replace）随阶段移除删除
    // REQ-20261013-105：Stage 7 精剪 v1 完整版（6 素材 + 字体 + BGM + 预览 + 异步 + 模板 + 检测 + AI 布局）
    if (action === 'sv-stage7-export') return stage7Export(btn);
    if (action === 'sv-stage7-save-params') return stage7SaveParams(btn);
    if (action === 'sv-stage7-crop-preset') return svCropPreset(btn);
    // REQ-20261018-167：参数 JSON 文件导出/导入
    if (action === 'sv-stage7-params-export') return stage7ExportParams(btn);
    if (action === 'sv-stage7-params-import') return stage7ImportParams(btn);
    if (action === 'sv-stage7-preview') return stage7Preview(btn);
    if (action === 'sv-stage7-detect-bg') return stage7DetectBg(btn);
    if (action === 'sv-stage7-detect-bg-clear') return stage7ClearDetect(btn);
    if (action === 'sv-stage7-apply-detected') return stage7ApplyDetected(btn);
    if (action === 'sv-stage7-parse-layout') return stage7ParseLayout(btn);
    if (action === 'sv-stage7-list-bgm') return stage7ListBgm(btn);
    if (action === 'sv-stage7-profiles-list') return stage7ProfilesList(btn);
    if (action === 'sv-stage7-profiles-save') return stage7ProfilesSave(btn);
    if (action === 'sv-stage7-profiles-apply') return stage7ProfilesApply(btn);
    if (action === 'sv-stage7-profiles-rename') return stage7ProfilesRename(btn);
    if (action === 'sv-stage7-profiles-export') return stage7ProfilesExport(btn);
    if (action === 'sv-stage7-profiles-delete') return stage7ProfilesDelete(btn);
    // REQ-20261017-158：Stage 6 素材文件名点击查看（视频浮窗 / 字幕文本弹层）
    if (action === 'sv-stage6-view-video') return svClipViewVideo(btn);
    if (action === 'sv-preview-srt') return svPreviewSrt(btn);
    // REQ-20261018-171：公共素材查看（图片/音频浮窗预览）
    if (action === 'sv-mat-preview') return svMatPreview(btn);
    // REQ-20261018-183：单条素材 继承/删除（清覆盖回退，共用端点）
    if (action === 'sv-stage6-clip-inherit') return stage6ClipClear(btn);
    if (action === 'sv-stage6-clip-mat-del') return stage6ClipClear(btn);

    // 预览 / 素材
    if (action === 'sv-preview') return previewVideo(btn);
    if (action === 'sv-preview-close') return closePreview();
  });
})();
