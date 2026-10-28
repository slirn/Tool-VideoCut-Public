// Slirn 事件路由 — 走自定义 /slirn/api/* 同步路由，绕开 Gradio 队列
//
// 本文件以静态文件方式经 <script src> 注入（app.py 的 _build_head 注册引用）。
// 不能作为内联脚本放进 gr.HTML(head=...)：Gradio 6.17.3 的 head 传输链路会把
// 脚本文本里的反斜杠转义解码（\n -> 换行、\x20 -> 空格、\\ -> \、孤立的 \ 被删除），
// 含反斜杠的内联 JS 会变成非法语法而整体失效（2026-09-17 排查结论）。
//
// 本批次更新（REQ-20260921-NNN-shake-fix — UI 抖动修复）：
// - scrollIntoView 去掉 smooth：optWordFilterApply 在「行隐藏 + 加 hint → 列表
//   高度突变」期间平滑滚动可能被反复打断，视觉上「滚动条上下抖」。即时
//   scrollIntoView({block:'center'}) 一次到位更稳。
// - optInputOverflowCheck 改 ±50px 滞回（hysteresis）打破反馈环：原来用
//   ±2px 容差时，CSS `:has(.wrapped)` 把第 4 列从 260px 切到 minmax(360,1fr)
//   cw 跨度 ~100px；内容宽度落 (cw+2, cw+102) 死区时，wrapped 类反复切 →
//   grid 列宽不停变 → ResizeObserver 再触发 → 滚动条上下抖。50px 滞回让
//   wrap 触发 cw 涨 100px 后落点必在 unwrap 阈值另一侧，状态稳定。
(function() {
  // 幂等保护：同一页面被注入两次时避免重复绑定
  if (window.__slirnRouterBound) return;
  window.__slirnRouterBound = true;

  var SLIRN_API = '/slirn/api';

  function postJSON(url, payload) {
    return fetch(url, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      credentials: 'same-origin',                  // REQ-20260926-NNN：带 cookie 会话
      body: JSON.stringify(payload || {})
    }).then(function(r) {
      if (r.status === 401) {                     // REQ-20260926-NNN：401 → 弹登录 modal
        try { window.slirnShowLogin && window.slirnShowLogin(); } catch (e) {}
        return { ok: false, error: '未登录或会话已过期', _status: 401 };
      }
      if (!r.ok) {
        // 把真实状态码 + 文本带回去，便于排查「看起来 401 但其实是别的」
        return r.json().then(function(b) {
          return { ok: false, error: (b && b.error) || ('HTTP ' + r.status), _status: r.status };
        }).catch(function() { return { ok: false, error: 'HTTP ' + r.status, _status: r.status }; });
      }
      return r.json();
    }).catch(function(e) { return {ok: false, error: String(e), _status: 0}; });
  }

  function postForm(url, formData) {
    return fetch(url, {
      method: 'POST',
      credentials: 'same-origin',                  // REQ-20260926-NNN
      body: formData
    }).then(function(r) { return r.json(); }).catch(function(e) { return {ok: false, error: String(e)}; });
  }

  function getJSON(url) {
    return fetch(url, { credentials: 'same-origin' })
      .then(function(r) {
        if (r.status === 401) {
          try { window.slirnShowLogin && window.slirnShowLogin(); } catch (e) {}
          return { ok: false, user: null, error: '未登录' };
        }
        return r.json();
      })
      .catch(function(e) { return {ok: false, user: null, error: String(e)}; });
  }

  // ===== REQ-20260926-NNN：登录 / 当前用户 / 登出 =====

  function _setCurrentUserUI(user) {
    var box = document.getElementById('slirn-current-user');
    var loginBtn = document.getElementById('slirn-login-btn');
    var nameEl = document.getElementById('slirn-current-user-name');
    var usersBtn = document.getElementById('slirn-users-open-btn');
    if (!box || !loginBtn) return;
    if (user && user.username) {
      nameEl.textContent = user.username + (user.is_admin ? ' (admin)' : '');
      box.style.display = '';
      loginBtn.style.display = 'none';
      if (usersBtn) usersBtn.style.display = user.is_admin ? '' : 'none';
    } else {
      box.style.display = 'none';
      loginBtn.style.display = '';
      if (usersBtn) usersBtn.style.display = 'none';
    }
  }
  window.slirnShowLogin = function () {
    var m = document.getElementById('slirn-login-modal');
    if (m) m.style.display = '';
  };
  window.slirnHideLogin = function () {
    var m = document.getElementById('slirn-login-modal');
    if (m) m.style.display = 'none';
    var hint = document.getElementById('slirn-login-hint');
    if (hint) { hint.style.display = 'none'; hint.textContent = ''; }
  };
  function _refreshCurrentUser() {
    getJSON(SLIRN_API + '/auth/me').then(function(r) {
      var u = r && r.user;
      _setCurrentUserUI(u);
      // REQ-20260926-NNN 修复：页面加载即按登录态拉任务列表 + 仪表盘。
      // 初始 HTML 只有骨架（不再嵌全部任务）— 带 cookie 刷新页面必须重拉，
      // 否则普通用户会一直看到启动时烤进 HTML 的全量任务。
      if (u && u.username) {
        // 首次加载：admin → 全部，普通用户 → 我的（此后跟随 scope radio）
        try { _refreshTasksList(u.is_admin ? 'all' : 'mine'); } catch (e) {}
        try { _refreshDashboard(); } catch (e) {}
      } else {
        // 未登录：数据端点全部要求会话，弹登录框引导
        try { window.slirnShowLogin && window.slirnShowLogin(); } catch (e) {}
      }
    });
  }
  // 启动时拉一次当前用户
  try { _refreshCurrentUser(); } catch (e) {}

  // ===== REQ-20260926-NNN：用户管理 modal（仅 admin） =====

  function _usersModalMsg(msg, isErr) {
    var el = document.getElementById('slirn-users-msg');
    if (el) { el.textContent = msg || ''; el.style.color = isErr ? '#ef4444' : ''; }
  }
  function _renderUsersList(users) {
    var wrap = document.getElementById('slirn-users-list');
    if (!wrap) return;
    if (!users || !users.length) {
      wrap.innerHTML = '<div class="slirn-form-hint">暂无用户</div>';
      return;
    }
    wrap.innerHTML = users.map(function(u) {
      return '<div class="slirn-member-row">' +
        '<span class="slirn-member-name">' + escapeHtml(u.username) +
        (u.is_admin ? ' <span class="slirn-member-role">admin</span>' : '') +
        ' <span class="slirn-member-role">' + escapeHtml(u.display_name || '') + '</span></span>' +
        '<span>' +
        '<button class="slirn-btn-mini" data-action="users-reset-pw" data-username="' + escapeHtml(u.username) + '">改密码</button> ' +
        '<button class="slirn-btn-mini slirn-btn-mini-danger" data-action="users-delete" data-username="' + escapeHtml(u.username) + '">删除</button>' +
        '</span></div>';
    }).join('');
  }
  function _loadUsers() {
    getJSON(SLIRN_API + '/auth/users').then(function(r) {
      if (r && r.ok) _renderUsersList(r.users);
      else _usersModalMsg((r && r.error) || '拉取用户失败', true);
    });
  }
  function _showUsersModal() {
    var m = document.getElementById('slirn-users-modal');
    if (!m) return;
    _usersModalMsg('');
    m.style.display = '';
    _loadUsers();
  }
  function _hideUsersModal() {
    var m = document.getElementById('slirn-users-modal');
    if (m) m.style.display = 'none';
  }

  // ===== REQ-20261021-142：登录日志 modal（本人） =====
  // ===== REQ-20261022-145：帮助菜单（使用说明 + 导入默认参数）=====
  // 顶栏有两个下拉（设置 / 帮助），开合逻辑统一
  var SLIRN_TOP_MENUS = ['slirn-help-dropdown', 'slirn-settings-dropdown'];

  function _toggleTopMenu(ddId, force) {
    var dd = document.getElementById(ddId);
    if (!dd) return;
    var show = (force === undefined) ? dd.hasAttribute('hidden') : !!force;
    if (show) {
      // 开一个 → 关另一个（避免两个下拉同时展开）
      SLIRN_TOP_MENUS.forEach(function (id) {
        if (id !== ddId) _toggleTopMenu(id, false);
      });
    }
    if (show) dd.removeAttribute('hidden'); else dd.setAttribute('hidden', '');
    var wrap = dd.parentElement;
    var btn = wrap ? wrap.querySelector('[aria-haspopup]') : null;
    if (btn) btn.setAttribute('aria-expanded', show ? 'true' : 'false');
  }

  function _closeTopMenus() {
    SLIRN_TOP_MENUS.forEach(function (id) { _toggleTopMenu(id, false); });
  }

  function _toggleHelpMenu(force) { _toggleTopMenu('slirn-help-dropdown', force); }
  function _toggleSettingsMenu(force) { _toggleTopMenu('slirn-settings-dropdown', force); }
  function _closeHelpMenu() { _closeTopMenus(); }

  // REQ-20261022-145（用户反馈）：帮助文档以**独立页面**打开，**不要遮罩**
  // → 新标签页打开静态 HTML（文档自带完整样式，不受工作台主题影响）。
  function _showHelpDoc(docName, title) {
    var url = '/slirn/static/help/' + docName + '.html';
    var win = null;
    try {
      // 注意：不能带 'noopener' 特性串 —— 按规范带 noopener 时 window.open
      // **返回 null**，会被误判成「弹窗被拦截」而再走 location.href，
      // 结果原页面被导航 + 新开一页 = 弹出两个页面。
      // 帮助页是同源静态 HTML、无脚本，交给 opener 访问无风险。
      win = window.open(url, '_blank');
    } catch (e) {
      win = null;
    }
    if (!win) {
      // 真正被拦截（无句柄）→ 同标签页打开（仍是无遮罩的独立页面）
      window.location.href = url;
    }
  }

  function _importDefaultParams(kind, label) {
    if (!window.confirm('将用内置默认参数覆盖当前的' + label + '参数（素材不受影响）。\n确认继续？')) return;
    var body = { kind: kind };
    // 长视频按任务、短视频按项目
    // 注意：pipeline.js 的 curTaskId 在它自己的 IIFE 内，router.js 取不到
    // → 从工作台容器上读 data-task-id
    var wbHost = document.getElementById('slirn-tab-workbench-inner');
    var tid = wbHost ? (wbHost.getAttribute('data-task-id') || '') : '';
    var svHost = document.getElementById('slirn-short-video-inner');
    if (kind === 'short') {
      if (svHost) {
        body.task_id = svHost.getAttribute('data-task-id') || '';
        body.project_id = svHost.getAttribute('data-project-id') || '';
      }
      if (!body.task_id || !body.project_id) {
        toast('请先打开一个短视频项目，再导入短视频参数', 'error');
        return;
      }
    } else {
      body.task_id = tid || '';
      if (!body.task_id) {
        toast('请先打开一个长视频任务，再导入长视频参数', 'error');
        return;
      }
    }
    postJSON(SLIRN_API + '/import_default_params', body).then(function (r) {
      if (!r || !r.ok) {
        toast('导入失败：' + ((r && r.error) || '未知错误'), 'error');
        return;
      }
      toast((r && r.toast) || ('已导入' + label + '默认参数'));
      // 短视频导入会带回新 HTML → 直接替换项目区
      if (kind === 'short' && r.html) {
        var host = document.getElementById('slirn-short-video-inner');
        var wrap = host && host.parentElement;
        if (wrap) {
          wrap.innerHTML = r.html;
          if (window.slirnShortVideoFlowMount) window.slirnShortVideoFlowMount();
        }
      } else {
        // 长视频参数在任务工作台 → 刷新当前视图
        setTimeout(function () { location.reload(); }, 600);
      }
    });
  }

  function _showLoginLogModal() {
    var m = document.getElementById('slirn-loginlog-modal');
    if (!m) return;
    m.style.display = '';
    var list = document.getElementById('slirn-loginlog-list');
    if (list) list.innerHTML = '<div class="slirn-form-hint">加载中…</div>';
    getJSON(SLIRN_API + '/auth/login_log').then(function(r) {
      if (!list) return;
      if (!r || !r.ok) {
        list.innerHTML = '<div class="slirn-form-hint">加载失败：' + ((r && r.error) || '未知错误') + '</div>';
        return;
      }
      var rows = (r.events || []).map(function(e) {
        var badge = e.result === 'success' ? '✅ 成功'
                  : e.result === 'fail' ? '❌ 失败'
                  : '🚪 登出';
        var detail = e.detail ? ' — ' + e.detail : '';
        return '<div class="slirn-recent-item" style="cursor:default;">'
             + '<div class="slirn-recent-info">'
             + '<div style="font-size:13px;">' + badge + detail + '</div>'
             + '<div class="slirn-detail-sub">' + (e.time || '') + ' · IP ' + (e.ip || '—') + '</div>'
             + '</div></div>';
      });
      list.innerHTML = rows.length ? rows.join('')
        : '<div class="slirn-form-hint">暂无登录记录</div>';
    }).catch(function(err) {
      if (list) list.innerHTML = '<div class="slirn-form-hint">网络错误：' + err + '</div>';
    });
  }

  // ===== REQ-20260926-NNN：任务成员管理（编辑页折叠块） =====

  function _memberMsg(msg, isErr) {
    var el = document.getElementById('slirn-member-msg');
    if (el) { el.textContent = msg || ''; el.style.color = isErr ? '#ef4444' : ''; }
  }
  function _loadTaskMembers() {
    var sec = document.getElementById('slirn-members-section');
    if (!sec) return;
    var tid = sec.getAttribute('data-task-id') || '';
    var creator = sec.getAttribute('data-created-by') || '';
    getJSON(SLIRN_API + '/auth/tasks/' + encodeURIComponent(tid) + '/members').then(function(r) {
      var list = document.getElementById('slirn-member-list');
      if (!list) return;
      var rows = [];
      if (creator) {
        rows.push('<div class="slirn-member-row"><span class="slirn-member-name">' +
          escapeHtml(creator) + ' <span class="slirn-member-role">创建者</span></span><span></span></div>');
      }
      var members = (r && r.ok && r.members) ? r.members : [];
      members.forEach(function(name) {
        if (name === creator) return;
        rows.push('<div class="slirn-member-row"><span class="slirn-member-name">' +
          escapeHtml(name) + '</span>' +
          '<button class="slirn-btn-mini slirn-btn-mini-danger" data-action="member-remove" ' +
          'data-task-id="' + escapeHtml(tid) + '" data-username="' + escapeHtml(name) + '">移除</button></div>');
      });
      if (!rows.length) rows.push('<div class="slirn-form-hint">暂无成员</div>');
      list.innerHTML = rows.join('');
    });
  }
  window._loadTaskMembers = _loadTaskMembers;

  // REQ-20260926-NNN：按当前 scope（我的/全部）重拉任务列表 — 登录/登出/手动刷新共用。
  // optScope：首次加载（骨架里还没有 radio）由 _refreshCurrentUser 按角色显式传 —
  // admin → 'all'（老行为），普通用户 → 'mine'。
  function _refreshTasksList(optScope) {
    var scope = optScope || 'mine';
    if (!optScope) {
      try {
        var sR = document.querySelector('input[name="slirn-task-scope"]:checked');
        if (sR) scope = sR.value;
      } catch (e) {}
    }
    postJSON(SLIRN_API + '/refresh_tasks', {scope: scope})
      .then(function(r) { handleResp(r, 'slirn-tab-tasks'); });
  }
  window._refreshTasksList = _refreshTasksList;

  // REQ-20260926-NNN 修复：按当前用户拉仪表盘 — 初始 HTML 已骨架化，
  // 统计卡片 + 最近任务随登录态加载（普通用户只看自己的）
  function _refreshDashboard() {
    postJSON(SLIRN_API + '/dashboard', {})
      .then(function(r) { handleResp(r, 'slirn-tab-dashboard'); });
  }
  window._refreshDashboard = _refreshDashboard;

  function getInput(id) {
    var el = document.getElementById(id);
    return el ? el.value : '';
  }

  function toast(msg, type) {
    type = type || 'success';
    var existing = document.querySelector('.slirn-toast');
    if (existing) existing.remove();
    var t = document.createElement('div');
    t.className = 'slirn-toast ' + type;
    t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(function() { t.style.opacity = '0'; t.style.transition = 'opacity 0.3s'; }, 2400);
    setTimeout(function() { t.remove(); }, 2800);
  }
  window.slirnToast = toast;

  // 工作台加载等待弹窗（REQ-20260918-051）：任务列表点「剪辑」时弹出。
  // 内容多的任务接口耗时长，用户需要明确反馈（全屏半透明遮罩 + spinner +
  // 任务名）。Fetch 完成（成功 / 失败 / 超时）自动关闭；多次点击同一个
  // tid 不会叠出多个弹窗。
  var _wbLoaderTimer = null;
  var _wbLoaderTid = null;
  function showWbLoader(tid, label) {
    hideWbLoader();  // 去重：先关旧弹窗（避免叠层 + 状态错乱）
    _wbLoaderTid = tid || '';
    var ov = document.createElement('div');
    ov.className = 'slirn-wb-loader-overlay';
    ov.setAttribute('data-wb-loader-for', _wbLoaderTid);
    ov.innerHTML =
      '<div class="slirn-wb-loader-card">'
      + '<div class="slirn-wb-loader-spinner"></div>'
      + '<p class="slirn-wb-loader-title">加载工作台…</p>'
      + '<p class="slirn-wb-loader-task">' + escapeHtml(label || tid || '') + '</p>'
      + '<p class="slirn-wb-loader-hint">内容较多时可能需要几秒，请稍候</p>'
      + '</div>';
    // 阻止遮罩自身点击穿透到下层（用户可能误点关不掉）
    ov.addEventListener('click', function(ev) { ev.stopPropagation(); });
    document.body.appendChild(ov);
    // 60s 超时兜底（防接口挂死导致弹窗永远关不掉）
    _wbLoaderTimer = setTimeout(function() {
      var cur = document.querySelector('.slirn-wb-loader-overlay');
      if (cur) {
        cur.querySelector('.slirn-wb-loader-title').textContent = '加载超时';
        cur.querySelector('.slirn-wb-loader-hint').textContent = '已等待 60 秒仍未返回，请检查网络或刷新页面';
      }
    }, 60000);
  }
  function hideWbLoader() {
    if (_wbLoaderTimer) { clearTimeout(_wbLoaderTimer); _wbLoaderTimer = null; }
    _wbLoaderTid = null;
    var ov = document.querySelector('.slirn-wb-loader-overlay');
    if (ov) ov.remove();
  }
  window.slirnShowWbLoader = showWbLoader;
  window.slirnHideWbLoader = hideWbLoader;

  // 更新分类标题里"删除选中"按钮上的计数
  function updateDeleteCount(section) {
    if (!section) return;
    var btn = section.querySelector('[data-action="hw-cat-delete"]');
    if (!btn) return;
    var n = section.querySelectorAll('.slirn-hotword-cell.selected').length;
    btn.textContent = n > 0 ? ('🗑 删除选中 (' + n + ')') : '🗑 删除选中';
    btn.classList.toggle('has-selection', n > 0);
  }
  window.slirnUpdateDeleteCount = updateDeleteCount;

  // ========== 任务级热词「选择器」逻辑（新建任务页） ==========

  // 收集当前「选中」状态的公共库词（按 DOM 出现顺序去重）
  function collectPickedHotwords() {
    var seen = {};
    var out = [];
    var cells = document.querySelectorAll('#slirn-hw-picker .slirn-pick-cell.selected');
    cells.forEach(function(c) {
      var w = c.getAttribute('data-word');
      if (w && !seen[w]) { seen[w] = 1; out.push(w); }
    });
    return out;
  }

  // 收集「继承公共库所有热词」时按显示顺序的词列表（用于预览 chips）
  function collectAllPublicHotwords() {
    var seen = {};
    var out = [];
    document.querySelectorAll('#slirn-hw-picker .slirn-pick-cell').forEach(function(c) {
      var w = c.getAttribute('data-word');
      if (w && !seen[w]) { seen[w] = 1; out.push(w); }
    });
    return out;
  }

  // 解析手动输入的词（REQ-20260926-NNN：分隔符半角逗号/换行，空格不算分隔符）
  function parseManual(text) {
    if (!text) return [];
    return text.split(/[,\n，;；、]+/).map(function(s) { return s.trim(); }).filter(Boolean);
  }

  // REQ-20260926-NNN：渲染 AI 分析候选词网格（复用 .slirn-hotword-cell.slirn-pick-cell 样式）
  function renderAnalysisGrid(candidates) {
    var grid = document.getElementById('slirn-hw-analysis-grid');
    if (!grid) return;
    if (!candidates || !candidates.length) {
      grid.innerHTML = '<div class="slirn-hotword-cell empty">（无候选）</div>';
      return;
    }
    grid.innerHTML = candidates.map(function(c) {
      var w = String(c.word || '');
      var cat = String(c.category || '');
      var label = cat ? (w + ' <small style="opacity:.6">· ' + cat + '</small>') : w;
      return '<div class="slirn-hotword-cell slirn-pick-cell" data-word="'
        + escapeAttr(w) + '"><span class="slirn-cell-text">' + label + '</span></div>';
    }).join('');
  }
  function escapeAttr(s) {
    return String(s).replace(/&/g, '&amp;').replace(/"/g, '&quot;')
      .replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  // 渲染「最终生效热词」的 chips（继承 + 选中 + 手动，去重）
  function renderHwChips() {
    var box = document.getElementById('slirn-hw-chips');
    var sum = document.getElementById('slirn-hw-summary');
    if (!box) return;
    var inheritEl = document.getElementById('slirn-hw-inherit-all');
    var inherit = inheritEl && inheritEl.checked;
    var baseWords = inherit ? collectAllPublicHotwords() : collectPickedHotwords();
    var manualRaw = document.getElementById('slirn-hotwords-manual');
    var manual = parseManual(manualRaw ? manualRaw.value : '');

    var seen = {};
    var ordered = [];
    baseWords.forEach(function(w) {
      var k = w.trim();
      if (k && !seen[k]) { seen[k] = 1; ordered.push({w: k, src: inherit ? 'inherit' : 'pick'}); }
    });
    manual.forEach(function(w) {
      var k = w.trim();
      if (k && !seen[k]) { seen[k] = 1; ordered.push({w: k, src: 'manual'}); }
    });

    box.innerHTML = '';
    if (!ordered.length) {
      box.innerHTML = '<div class="slirn-hw-chip-empty">（还没有热词 — 勾选继承 / 从公共库选 / 或在下方输入）</div>';
    } else {
      ordered.forEach(function(o) {
        var chip = document.createElement('span');
        var cls = 'slirn-hw-chip src-' + o.src;
        chip.className = cls;
        chip.setAttribute('data-word', o.w);
        var label = o.src === 'inherit' ? '继承' : (o.src === 'pick' ? '已选' : '手动');
        var xBtn = (o.src === 'inherit')
          ? ''  // 继承来的词不可单独移除（要排除就关掉「继承」）
          : '<button class="slirn-hw-chip-x" data-action="hw-chip-remove" data-word="' + escapeAttr(o.w) + '" title="移除">✕</button>';
        chip.innerHTML = '<span class="slirn-hw-chip-label">' + label + '</span>' +
                         '<span class="slirn-hw-chip-text">' + escapeHtml(o.w) + '</span>' +
                         xBtn;
        box.appendChild(chip);
      });
    }
    if (sum) {
      sum.textContent = '共 ' + ordered.length + ' 个词' +
        (inherit ? ' · 来自继承全部公共库' : '');
    }
  }
  window.slirnRenderHwChips = renderHwChips;

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function(c) {
      return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c];
    });
  }
  function escapeAttr(s) {
    return escapeHtml(s);
  }

  // 切换继承勾选 → 控制选择器显示/隐藏，并刷新 chips
  function syncInheritVisibility() {
    var inheritEl = document.getElementById('slirn-hw-inherit-all');
    var wrap = document.getElementById('slirn-hw-picker-wrap');
    if (!inheritEl || !wrap) return;
    if (inheritEl.checked) {
      wrap.classList.add('slirn-inherit-mode');
    } else {
      wrap.classList.remove('slirn-inherit-mode');
    }
    renderHwChips();
  }
  window.slirnSyncInherit = syncInheritVisibility;

  // 初始化新建任务页的热词选择器（每次切换到该 tab 时调用一次）
  function initHotwordPicker() {
    var picker = document.getElementById('slirn-hw-picker');
    if (!picker) return;
    var inheritEl = document.getElementById('slirn-hw-inherit-all');
    var manualEl = document.getElementById('slirn-hotwords-manual');
    if (inheritEl && !inheritEl.dataset.slirnBound) {
      inheritEl.dataset.slirnBound = '1';
      inheritEl.addEventListener('change', syncInheritVisibility);
    }
    if (manualEl && !manualEl.dataset.slirnBound) {
      manualEl.dataset.slirnBound = '1';
      manualEl.addEventListener('input', renderHwChips);
    }
    // 点击 picker 单元切换选中（事件代理）
    if (!picker.dataset.slirnClickBound) {
      picker.dataset.slirnClickBound = '1';
      picker.addEventListener('click', function(ev) {
        var cell = ev.target.closest('.slirn-pick-cell');
        if (!cell) return;
        cell.classList.toggle('selected');
        renderHwChips();
      });
    }
    // REQ-20260926-NNN：AI 分析候选网格同样支持点击切换 .selected
    var analysisGrid = document.getElementById('slirn-hw-analysis-grid');
    if (analysisGrid && !analysisGrid.dataset.slirnClickBound) {
      analysisGrid.dataset.slirnClickBound = '1';
      analysisGrid.addEventListener('click', function(ev) {
        var cell = ev.target.closest('.slirn-pick-cell');
        if (!cell) return;
        cell.classList.toggle('selected');
      });
    }
    // chips 容器上的 ✕ 移除（事件代理）
    var chipsBox = document.getElementById('slirn-hw-chips');
    if (chipsBox && !chipsBox.dataset.slirnClickBound) {
      chipsBox.dataset.slirnClickBound = '1';
      chipsBox.addEventListener('click', function(ev) {
        var x = ev.target.closest('[data-action="hw-chip-remove"]');
        if (!x) return;
        ev.stopPropagation();
        var w = x.getAttribute('data-word') || '';
        // 找到对应 picker cell，去掉 .selected（picked 来源）
        var cell = picker.querySelector('.slirn-pick-cell[data-word="' + cssEscape(w) + '"]');
        if (cell) cell.classList.remove('selected');
        // 如果是手动来源，从 textarea 里去掉这个词
        var manualEl2 = document.getElementById('slirn-hotwords-manual');
        if (manualEl2 && manualEl2.value) {
          // REQ-20260926-NNN：按 parseManual 拆词后过滤 + 半角逗号重拼，
          // 多词热词（如 "machine learning"）不会再被空格切碎
          var entries = parseManual(manualEl2.value);
          manualEl2.value = entries.filter(function(e) { return e !== w; }).join(',');
        }
        renderHwChips();
      });
    }
    syncInheritVisibility();
  }

  function cssEscape(s) {
    if (window.CSS && CSS.escape) return CSS.escape(s);
    return String(s).replace(/(["\\.#:>+~*[\]()'])/g, '\\$1');
  }
  window.slirnInitHotwordPicker = initHotwordPicker;

  function secondsToHMS(s) {
    if (s == null || isNaN(s) || s < 0) return '';
    var ms = Math.round(s * 1000);
    var hh = Math.floor(ms / 3600000); ms = ms % 3600000;
    var mm = Math.floor(ms / 60000); ms = ms % 60000;
    var ss = Math.floor(ms / 1000); var mss = ms % 1000;
    return (hh<10?'0':'')+hh+':'+(mm<10?'0':'')+mm+':'+(ss<10?'0':'')+ss+'.'+(mss<10?'00':mss<100?'0':'')+mss;
  }
  window.slirnSecToHMS = secondsToHMS;

  // HH:MM:SS.mmm / HH:MM:SS → 秒数（用于客户端校验 start<end）
  // 返回 null 表示格式无效
  function hmsToSeconds(str) {
    if (!str) return null;
    var m = String(str).match(/^(\d+):(\d{1,2}):(\d{1,2})(?:\.(\d{1,3}))?$/);
    if (!m) return null;
    var h = parseInt(m[1], 10);
    var mi = parseInt(m[2], 10);
    var se = parseInt(m[3], 10);
    var ms = m[4] ? parseInt(m[4].padEnd(3, '0'), 10) : 0;
    if (mi >= 60 || se >= 60) return null;
    return h * 3600 + mi * 60 + se + ms / 1000;
  }

  // ===== 当前选中的视频路径（来自文件选择）=====
  window.slirnSelectedFile = '';

  // ===== Tab 切换 — 纯客户端 CSS toggle，HTML 已在初始 DOM 中 =====
  var TAB_BUTTONS = {
    'goto-dashboard': 'slirn-tab-dashboard',
    'goto-tasks':     'slirn-tab-tasks',
    'goto-create':    'slirn-tab-create',
    'goto-hotwords':  'slirn-tab-hotwords',
    'goto-short-video': 'slirn-tab-short-video',
    'goto-usage':     'slirn-tab-usage',
  };
  var ALL_TABS = ['slirn-tab-dashboard','slirn-tab-tasks','slirn-tab-create','slirn-tab-hotwords','slirn-tab-workbench','slirn-tab-short-video','slirn-tab-usage'];

  function showTab(targetCell, options) {
    ALL_TABS.forEach(function(id) {
      var el = document.getElementById(id);
      if (el) el.style.display = (id === targetCell) ? '' : 'none';
    });
    var detail = document.getElementById('slirn-tab-detail');
    if (detail) detail.style.display = 'none';
    // 记住最后所在 tab —— 刷新后恢复（见 _restoreLastTab）
    try { localStorage.setItem('slirn.lastTab', targetCell); } catch (e) {}
    // 切到「新建任务」时初始化热词选择器（刷新 chip 显示）；
    // 若上次是「编辑任务」占用了本页 → 重新拉取全新建页
    if (targetCell === 'slirn-tab-create') {
      try { window.slirnInitHotwordPicker && window.slirnInitHotwordPicker(); } catch (e) {}
      if (window.slirnEditTaskId) {
        window.slirnEditTaskId = null;
        postJSON(SLIRN_API + '/create_page', {}).then(function(r) {
          if (r && r.ok && r.html) {
            var c = document.getElementById('slirn-tab-create');
            if (c) c.innerHTML = r.html;
            initTaskEdit();  // 无 edit-state 时仅清状态
          }
        });
      }
    }
    if (targetCell === 'slirn-tab-short-video') {
      var forceShortVideoList = !!(options && options.forceShortVideoList);
      _callShortVideoLoadWhenReady(forceShortVideoList);
    }
    // 切到「Token 用量」页时刷新数据（每次进入都拉最新）
    if (targetCell === 'slirn-tab-usage') {
      loadUsagePage();
    }
    window.scrollTo({top: 0, behavior: 'smooth'});
  }

  // ===== 短视频脚本就绪等待（bug 修复：更新重启后自动弹出的页面列表永远「加载中」）=====
  // 三个 JS 是 Gradio 前端运行时用 createElement 动态插入 <head> 的 —— 动态脚本
  // 的 defer 属性不生效，执行顺序 = 网络加载完成顺序（竞态）。router.js 先于
  // short_video.js 执行时，showTab 里 window.slirnShortVideoLoad 还是 undefined，
  // 守卫调用被静默跳过 → 恢复 lastTab=短视频页时列表停在骨架「加载中…」永不刷新。
  // 这里改为就绪前轮询等待（100ms × 最多 100 次 ≈ 10s），就绪后立即调用。
  var _svLoadSeq = 0;
  function _callShortVideoLoadWhenReady(forceList) {
    var seq = ++_svLoadSeq;   // 后来的调用接管，旧的轮询作废（防重复加载）
    var tries = 0;
    (function tick() {
      if (seq !== _svLoadSeq) return;
      if (typeof window.slirnShortVideoLoad === 'function') {
        try { window.slirnShortVideoLoad(forceList); } catch (e) {}
        return;
      }
      if (++tries > 100) return;   // ~10s 未就绪 → 放弃（页面异常时不死循环）
      setTimeout(tick, 100);
    })();
  }

  // ===== 刷新保持当前页面（2026-10-12 修订）=====
  // 离开时停留在哪个主导航 tab，刷新后就回到那个 tab（等价于替用户点一次
  // 对应导航按钮，无上下文依赖）。仅恢复 5 个主导航 tab —— detail/workbench
  // 依赖导航链路带入的上下文，不在恢复范围。短视频 tab 额外由 short_video.js
  // 按记忆恢复到具体项目编辑页：项目页刷新不再跳回首页/列表。
  function _restoreLastTab() {
    var last = '';
    try { last = localStorage.getItem('slirn.lastTab') || ''; } catch (e) {}
    var navTabs = Object.keys(TAB_BUTTONS).map(function(k) { return TAB_BUTTONS[k]; });
    if (navTabs.indexOf(last) >= 0) showTab(last);
  }
  // 注意：router.js / short_video.js 都是 Gradio 运行时动态插入的脚本，defer
  // 不生效、相互执行顺序不保证 —— 恢复时 short_video.js 可能还没跑。
  // showTab 内部用 _callShortVideoLoadWhenReady 等它就绪后再调，这里无需再等。
  if (document.readyState === 'complete') _restoreLastTab();
  else document.addEventListener('DOMContentLoaded', _restoreLastTab);

  // REQ-20260920-087：任务列表搜索框过滤（client-side）
  // 监听 #slirn-task-search 的 input 事件，按 task_id / name / original_video 文本匹配卡片
  // 全空字符串 → 显示全部；非空 → 隐藏不匹配的卡片 + 显示「无匹配」空态
  function _filterTaskCards(query) {
    var q = (query || '').trim().toLowerCase();
    var grid = document.querySelector('.slirn-task-grid');
    if (!grid) return;
    var cards = grid.querySelectorAll('.slirn-task-card');
    var matchedCount = 0;
    cards.forEach(function(card) {
      if (!q) {
        card.style.display = '';
        matchedCount++;
        return;
      }
      var tid = (card.getAttribute('data-task-id') || '').toLowerCase();
      var nameEl = card.querySelector('.slirn-task-name');
      var videoEl = card.querySelector('.slirn-task-video');
      var name = nameEl ? (nameEl.textContent || '').toLowerCase() : '';
      var video = videoEl ? (videoEl.textContent || '').toLowerCase() : '';
      var hay = tid + ' ' + name + ' ' + video;
      var match = hay.indexOf(q) >= 0;
      card.style.display = match ? '' : 'none';
      if (match) matchedCount++;
    });
    // 处理「无匹配」空态
    var existing = grid.querySelector('.slirn-search-empty');
    if (existing) existing.remove();
    if (q && matchedCount === 0) {
      var empty = document.createElement('div');
      empty.className = 'slirn-empty slirn-search-empty';
      empty.style.gridColumn = '1/-1';
      empty.innerHTML = '<div class="slirn-empty-icon">🔍</div>' +
        '<div class="slirn-empty-text">没有匹配「' + q.replace(/[<>&"']/g, function(c) {
          return ({'<':'<','>':'>','&':'&','"':'"',"'":'&#39;'})[c];
        }) + '」的任务</div>';
      grid.appendChild(empty);
    }
  }

  // 用事件代理挂监听（避免重复绑定 — input 元素可能在刷新后被替换）
  document.addEventListener('input', function(e) {
    if (e.target && e.target.id === 'slirn-task-search') {
      _filterTaskCards(e.target.value);
    }
  });

  function refreshCell(cellId, html) {
    var cell = document.getElementById(cellId);
    if (!cell) return;
    if (html) cell.innerHTML = html;
  }

  // ===== 上传进度弹窗 =====
  function openUploadDialog(file) {
    var existing = document.getElementById('slirn-upload-modal');
    if (existing) existing.remove();
    var overlay = document.createElement('div');
    overlay.id = 'slirn-upload-modal';
    overlay.className = 'slirn-modal-overlay';
    var sizeMb = (file.size / 1024 / 1024).toFixed(2);
    overlay.innerHTML =
      '<div class="slirn-modal-card">' +
        '<div class="slirn-modal-title">⏳ 上传视频中…</div>' +
        '<div class="slirn-modal-filename">' + escapeHtml(file.name) + '（' + sizeMb + ' MB）</div>' +
        '<div class="slirn-progress-track"><div id="slirn-progress-bar" class="slirn-progress-bar"></div></div>' +
        '<div id="slirn-progress-text" class="slirn-progress-text">准备中…</div>' +
      '</div>';
    document.body.appendChild(overlay);
    return overlay;
  }
  function setUploadProgress(loaded, total) {
    var bar = document.getElementById('slirn-progress-bar');
    var text = document.getElementById('slirn-progress-text');
    if (!bar || !text) return;
    var pct = total > 0 ? Math.min(100, Math.round(loaded / total * 100)) : 0;
    bar.style.width = pct + '%';
    var sentMb = (loaded / 1024 / 1024).toFixed(2);
    var totalMb = (total / 1024 / 1024).toFixed(2);
    text.textContent = pct + '%  ·  ' + sentMb + ' / ' + totalMb + ' MB';
  }
  function closeUploadDialog() {
    var m = document.getElementById('slirn-upload-modal');
    if (m) m.remove();
  }

  // ===== 大模型设置弹窗（REQ-20260915-008：多厂商模型动态注册；REQ-20260916-001：协议 + 编辑）=====
  // 已注册模型列表（当前 ⭐ 高亮 + 设为当前/测试/编辑/删除）+ 表单（模型名/厂商/
  // 协议/Base URL/Key 环境变量名，添加与编辑共用）。Key 本身始终从系统环境变量
  // 读取，界面只填「环境变量名」。
  var LLM_EDIT_ID = null;   // 非空 = 表单处于编辑该模型状态
  var LLM_MODELS = [];      // 最近一次列表数据（编辑时回填表单用）
  function llmFormReset() {
    LLM_EDIT_ID = null;
    ['slirn-llm-in-id', 'slirn-llm-in-provider', 'slirn-llm-in-url', 'slirn-llm-in-env']
      .forEach(function(i) { var el = document.getElementById(i); if (el) el.value = ''; });
    var proto = document.getElementById('slirn-llm-in-proto');
    if (proto) proto.value = 'openai';
    // REQ-20260919-061 用户补充：重置时取消勾选「支持图片」
    var visChk = document.getElementById('slirn-llm-in-vision');
    if (visChk) visChk.checked = false;
    var t = document.getElementById('slirn-llm-form-title');
    if (t) t.textContent = '添加模型';
    var btn = document.getElementById('slirn-llm-add-btn');
    if (btn) btn.textContent = '➕ 添加';
    var cancel = document.getElementById('slirn-llm-cancel-btn');
    if (cancel) cancel.style.display = 'none';
  }
  function openLLMSettings() {
    var existing = document.getElementById('slirn-llm-modal');
    if (existing) existing.remove();
    var overlay = document.createElement('div');
    overlay.id = 'slirn-llm-modal';
    overlay.className = 'slirn-modal-overlay';
    overlay.innerHTML =
      '<div class="slirn-modal-card slirn-llm-card">' +
        '<div class="slirn-modal-title">⚙️ 大模型设置</div>' +
        '<div class="slirn-llm-subtitle">当前模型：<b id="slirn-llm-current">…</b></div>' +
        '<div class="slirn-llm-list" id="slirn-llm-list"></div>' +
        '<div class="slirn-llm-section">📊 Token 用量（输入 / 输出）</div>' +
        '<div id="slirn-llm-usage" class="slirn-llm-usage">…</div>' +
        '<button class="slirn-btn" data-action="llm-goto-usage" style="margin:6px 0 10px;">📊 打开用量统计页</button>' +
        '<div class="slirn-llm-section" id="slirn-llm-form-title">添加模型</div>' +
        '<div class="slirn-llm-form" id="slirn-llm-form">' +
          '<div class="slirn-llm-row"><span class="slirn-llm-label">模型名</span>' +
            '<input id="slirn-llm-in-id" class="slirn-llm-input" placeholder="如 deepseek-chat" /></div>' +
          '<div class="slirn-llm-row"><span class="slirn-llm-label">厂商</span>' +
            '<input id="slirn-llm-in-provider" class="slirn-llm-input" placeholder="如 DeepSeek / 阿里云百炼" /></div>' +
          '<div class="slirn-llm-row"><span class="slirn-llm-label">协议</span>' +
            '<select id="slirn-llm-in-proto" class="slirn-llm-input">' +
              '<option value="openai">OpenAI 兼容（{Base URL}/chat/completions）</option>' +
              '<option value="anthropic">Anthropic（{Base URL}/v1/messages）</option>' +
            '</select></div>' +
          '<div class="slirn-llm-row"><span class="slirn-llm-label">Base URL</span>' +
            '<input id="slirn-llm-in-url" class="slirn-llm-input" placeholder="https://api.deepseek.com/v1" /></div>' +
          '<div class="slirn-llm-row"><span class="slirn-llm-label">Key 环境变量</span>' +
            '<input id="slirn-llm-in-env" class="slirn-llm-input" placeholder="如 DEEPSEEK_API_KEY" /></div>' +
          '<div class="slirn-llm-row"><span class="slirn-llm-label">支持图片</span>' +
            '<label class="slirn-llm-chk"><input type="checkbox" id="slirn-llm-in-vision" /> ' +
            '可接收图片输入（多模态 — 精剪视频 AI 智能布局需此项）</label></div>' +
        '</div>' +
        '<div class="slirn-llm-tip">按所选协议调用（OpenAI 兼容 <code>{Base URL}/chat/completions</code> / Anthropic <code>{Base URL}/v1/messages</code>）；API Key 从上面填写的系统环境变量读取，界面不存储 Key。</div>' +
        '<div id="slirn-llm-test-result" class="slirn-llm-test-result"></div>' +
        '<div class="slirn-llm-actions">' +
          '<button class="slirn-btn slirn-btn-primary" data-action="llm-add" id="slirn-llm-add-btn">➕ 添加</button>' +
          '<button class="slirn-btn" data-action="llm-cancel-edit" id="slirn-llm-cancel-btn" style="display:none">取消编辑</button>' +
          '<button class="slirn-btn" data-action="llm-close">关闭</button>' +
        '</div>' +
      '</div>';
    overlay.addEventListener('click', function(e) {
      if (e.target === overlay) overlay.remove();  // 点遮罩关闭
    });
    document.body.appendChild(overlay);
    llmFormReset();
    fetch(SLIRN_API + '/llm_config').then(function(r) { return r.json(); })
      .then(function(r) {
        if (r && r.ok) renderLLMList(r.models || [], r.current || '');
      })
      .catch(function() {});
    renderLLMUsage();  // Token 用量（随弹窗打开刷新）
  }

  // ===== Token 用量账本渲染（数据来自服务端 llm_usage.record 的持久累计）=====
  function fmtTok(n) {
    n = n || 0;
    if (n >= 1000000) return (n / 1000000).toFixed(2) + 'M';
    if (n >= 1000) return (n / 1000).toFixed(1) + 'K';
    return String(n);
  }
  function renderLLMUsage(taskId) {
    postJSON(SLIRN_API + '/llm_usage', taskId ? {task_id: taskId} : {})
      .then(function(r) {
        var box = document.getElementById('slirn-llm-usage');
        if (!box) return;
        if (!r || !r.ok) { box.innerHTML = ''; return; }
        var u = r.usage || {};
        var total = u.total || {};
        if (!total.calls) {
          box.innerHTML = '<div class="slirn-form-hint">暂无记录 — 发起任何大模型调用后这里会出现输入 / 输出 token 统计</div>';
          return;
        }
        var est = total.estimated_calls
          ? '<span class="slirn-form-hint">（其中 ' + total.estimated_calls + ' 笔网关未回 usage，为字符估算）</span>' : '';
        var rows = Object.keys(u.models || {}).sort(function(a, b) {
          return ((u.models[b].prompt_tokens || 0) + (u.models[b].completion_tokens || 0))
               - ((u.models[a].prompt_tokens || 0) + (u.models[a].completion_tokens || 0));
        }).map(function(mid) {
          var m = u.models[mid];
          return '<div class="slirn-llm-row"><span class="slirn-llm-label">' + escapeHtml(mid) + '</span>' +
            '<span>输入 <b>' + fmtTok(m.prompt_tokens) + '</b> · 输出 <b>' + fmtTok(m.completion_tokens) +
            '</b> · ' + m.calls + ' 次</span></div>';
        }).join('');
        var tids = Object.keys(u.tasks || {}).sort(function(a, b) {
          return ((u.tasks[b].prompt_tokens || 0) + (u.tasks[b].completion_tokens || 0))
               - ((u.tasks[a].prompt_tokens || 0) + (u.tasks[a].completion_tokens || 0));
        }).slice(0, 5);
        var taskRows = tids.map(function(tid) {
          var t = u.tasks[tid];
          var stages = Object.keys(t.stages || {}).map(function(st) {
            return escapeHtml(st) + ' ' + fmtTok((t.stages[st].prompt_tokens || 0)
              + (t.stages[st].completion_tokens || 0));
          }).join(' · ');
          return '<div class="slirn-llm-row"><span class="slirn-llm-label">' + escapeHtml(tid) + '</span>' +
            '<span>输入 <b>' + fmtTok(t.prompt_tokens) + '</b> · 输出 <b>' + fmtTok(t.completion_tokens) +
            '</b>' + (stages ? '<br><span class="slirn-form-hint">' + stages + '</span>' : '') + '</span></div>';
        }).join('');
        box.innerHTML =
          '<div class="slirn-llm-row"><span class="slirn-llm-label"><b>总计</b></span>' +
            '<span>输入 <b>' + fmtTok(total.prompt_tokens) + '</b> · 输出 <b>' + fmtTok(total.completion_tokens) +
            '</b> · ' + total.calls + ' 次 ' + est + '</span></div>' + rows +
          (taskRows ? '<div class="slirn-form-hint" style="margin-top:6px;">最近消耗最大的任务（前 5）：</div>' + taskRows : '');
      }).catch(function() {});
  }

  // ===== 系统设置弹窗 — 大模型并发 worker 数（可探测建议值）=====
  function openSysSettings() {
    var existing = document.getElementById('slirn-sys-modal');
    if (existing) existing.remove();
    var overlay = document.createElement('div');
    overlay.id = 'slirn-sys-modal';
    overlay.className = 'slirn-modal-overlay';
    overlay.innerHTML =
      '<div class="slirn-modal-card" style="max-width:520px;">' +
        '<div class="slirn-modal-title">🛠 系统设置</div>' +
        '<div class="slirn-llm-row" style="margin-top:12px;">' +
          '<span class="slirn-llm-label">大模型并发<br>Worker 数</span>' +
          '<input id="slirn-sys-workers" type="number" min="1" max="8" step="1" class="slirn-llm-input" style="width:90px;" />' +
          '<button class="slirn-btn" data-action="sys-probe" id="slirn-sys-probe-btn">🔍 检测建议值</button>' +
        '</div>' +
        '<div class="slirn-form-hint" style="margin-top:4px;">' +
          '字幕修订等分批调用大模型时的并行请求数（1-8）。改完保存即生效（无需重启）。' +
          '「检测建议值」会对当前模型发约 20 个微小请求实测限流边界，约需 10-30 秒。' +
        '</div>' +
        '<div id="slirn-sys-probe-result" style="margin-top:8px;"></div>' +
        '<div class="slirn-llm-row" style="margin-top:14px;">' +
          '<span class="slirn-llm-label">上下文窗口<br>批量打包</span>' +
          '<button class="slirn-btn" data-action="sys-ctx-probe" id="slirn-sys-ctx-btn">📐 探测上下文</button>' +
        '</div>' +
        '<div class="slirn-form-hint" style="margin-top:4px;">' +
          '探测当前模型的上下文窗口后，字幕分批按「窗口 - 输出预留 - 余量」<b>整行打包</b>' +
          '（短行多装、长行少装，用满上下文、减少调用次数）。被拒的探测请求不计费，' +
          '首个成功的请求按实际 tokens 计（一般不超过一次普通调用）。' +
        '</div>' +
        '<div id="slirn-sys-ctx-result" style="margin-top:8px;"></div>' +
        '<div class="slirn-llm-actions" style="margin-top:14px;">' +
          '<button class="slirn-btn slirn-btn-primary" data-action="sys-save">💾 保存</button>' +
          '<button class="slirn-btn" data-action="sys-close">关闭</button>' +
        '</div>' +
      '</div>';
    overlay.addEventListener('click', function(e) {
      if (e.target === overlay) overlay.remove();
    });
    document.body.appendChild(overlay);
    fetch(SLIRN_API + '/settings').then(function(r) { return r.json(); })
      .then(function(r) {
        if (r && r.ok) {
          var el = document.getElementById('slirn-sys-workers');
          if (el) el.value = (r.settings || {}).llm_workers;
          var cc = r.context_cache || {};
          var keys = Object.keys(cc);
          if (keys.length) {
            var box = document.getElementById('slirn-sys-ctx-result');
            if (box) box.innerHTML = '<div class="slirn-form-hint">已缓存（点击「探测上下文」可刷新）：<br>' +
              keys.map(function(k) {
                return escapeHtml(k) + ' → ' + fmtTok(cc[k].tokens) + ' tokens（' +
                  escapeHtml(String(cc[k].at || '').replace('T', ' ')) + '）';
              }).join('<br>') + '</div>';
          }
        }
      }).catch(function() {});
  }

  function sysCtxProbe() {
    var box = document.getElementById('slirn-sys-ctx-result');
    var btn = document.getElementById('slirn-sys-ctx-btn');
    if (btn) { btn.disabled = true; btn.textContent = '探测中…'; }
    if (box) box.innerHTML = '<div class="slirn-form-hint">⏳ 正在探测上下文窗口（先查模型元数据，再发一个超长请求看网关报的上限，多数被拒请求不计费）…</div>';
    postJSON(SLIRN_API + '/llm_probe_ctx', {}).then(function(r) {
      if (btn) { btn.disabled = false; btn.textContent = '📐 探测上下文'; }
      if (!r || !r.ok) {
        if (box) box.innerHTML = '<div class="slirn-form-hint" style="color:#c0392b;">❌ ' + ((r && r.error) || '探测失败') + '</div>';
        return;
      }
      var cost = r.probe_cost_tokens
        ? '（本次计费请求约 ' + fmtTok(r.probe_cost_tokens) + ' tokens）' : '（全程被拒不计费）';
      if (box) box.innerHTML =
        '<div style="margin-top:4px;">' + escapeHtml(r.model || '') + ' 上下文 ≈ <b>' +
        fmtTok(r.context_tokens) + '</b> tokens（来源：' + escapeHtml(r.source || '') + '）' + cost + '</div>' +
        '<div style="margin-top:4px;">📦 分批打包上限：<b style="color:var(--accent-solid,#6366f1)">' +
        r.suggested_batch + '</b> 行/批 · ' + escapeHtml(r.note || '') + '</div>';
    }).catch(function(err) {
      if (btn) { btn.disabled = false; btn.textContent = '📐 探测上下文'; }
      if (box) box.innerHTML = '<div class="slirn-form-hint" style="color:#c0392b;">❌ 探测请求失败: ' + err + '</div>';
    });
  }

  function sysSettingsProbe() {
    var box = document.getElementById('slirn-sys-probe-result');
    var btn = document.getElementById('slirn-sys-probe-btn');
    if (btn) { btn.disabled = true; btn.textContent = '探测中…'; }
    if (box) box.innerHTML = '<div class="slirn-form-hint">⏳ 正在阶梯探测并发能力（2 → 4 → 8 路，每路发一个最小请求）…</div>';
    postJSON(SLIRN_API + '/llm_probe', {}).then(function(r) {
      if (btn) { btn.disabled = false; btn.textContent = '🔍 检测建议值'; }
      if (!r || !r.ok) {
        if (box) box.innerHTML = '<div class="slirn-form-hint" style="color:#c0392b;">❌ ' + ((r && r.error) || '探测失败') + '</div>';
        return;
      }
      var rows = (r.levels || []).map(function(l) {
        var color = l.ok === l.k ? '#16a34a' : '#c0392b';
        return '<tr><td>' + l.k + ' 路</td><td style="color:' + color + '">✓ ' + l.ok + '/' + l.k + '</td>' +
          '<td>' + (l.throttled ? '限流 ' + l.throttled : '-') + '</td>' +
          '<td>' + (l.failed ? '失败 ' + l.failed : '-') + '</td><td>' + (l.elapsed_ms / 1000).toFixed(1) + 's</td></tr>';
      }).join('');
      var sug = '<div style="margin-top:8px;">💡 建议值：<b style="font-size:16px;color:var(--accent-solid,#6366f1)">' +
        r.suggested + '</b> 路 · ' + escapeHtml(r.note || '') +
        ' <button class="slirn-btn" style="padding:2px 10px;" onclick="var e=document.getElementById(\'slirn-sys-workers\'); if(e){e.value=' +
        r.suggested + ';}">采用</button></div>';
      if (box) box.innerHTML =
        '<table class="slirn-sys-probe-table"><thead><tr><th>并发</th><th>成功</th><th>限流</th><th>失败</th><th>耗时</th></tr></thead>' +
        '<tbody>' + rows + '</tbody></table>' + sug;
    }).catch(function(err) {
      if (btn) { btn.disabled = false; btn.textContent = '🔍 检测建议值'; }
      if (box) box.innerHTML = '<div class="slirn-form-hint" style="color:#c0392b;">❌ 探测请求失败: ' + err + '</div>';
    });
  }

  function sysSettingsSave() {
    var el = document.getElementById('slirn-sys-workers');
    var v = parseInt((el && el.value) || '', 10);
    if (isNaN(v) || v < 1 || v > 8) {
      toast('并发 Worker 数必须是 1-8 的整数', 'error');
      return;
    }
    postJSON(SLIRN_API + '/settings/update', {llm_workers: v}).then(function(r) {
      if (r && r.ok) {
        toast(r.toast || '已保存');
        var m = document.getElementById('slirn-sys-modal');
        if (m) m.remove();
      } else {
        toast('❌ ' + ((r && r.error) || '保存失败'), 'error');
      }
    });
  }

  // ===== Token 用量独立页（⚙ → Token 用量）=====
  function loadUsagePage() {
    // 汇总
    postJSON(SLIRN_API + '/llm_usage', {}).then(function(r) {
      var box = document.getElementById('slirn-usage-summary');
      if (!box) return;
      if (!r || !r.ok) { box.innerHTML = ''; return; }
      var u = r.usage || {};
      var total = u.total || {};
      if (!total.calls) {
        box.innerHTML = '<div class="slirn-empty"><div class="slirn-empty-icon">📊</div>' +
          '<div class="slirn-empty-text">暂无记录 — 发起任何大模型调用后这里会出现统计</div></div>';
        return;
      }
      var est = total.estimated_calls
        ? '<span class="slirn-form-hint">（其中 ' + total.estimated_calls + ' 笔为字符估算）</span>' : '';
      var modelRows = Object.keys(u.models || {}).sort(function(a, b) {
        return ((u.models[b].prompt_tokens || 0) + (u.models[b].completion_tokens || 0))
             - ((u.models[a].prompt_tokens || 0) + (u.models[a].completion_tokens || 0));
      }).map(function(mid) {
        var m = u.models[mid];
        return '<tr><td>' + escapeHtml(mid) + '</td><td><b>' + fmtTok(m.prompt_tokens) + '</b></td>' +
          '<td><b>' + fmtTok(m.completion_tokens) + '</b></td><td>' + m.calls + '</td></tr>';
      }).join('');
      var taskRows = Object.keys(u.tasks || {}).sort(function(a, b) {
        return ((u.tasks[b].prompt_tokens || 0) + (u.tasks[b].completion_tokens || 0))
             - ((u.tasks[a].prompt_tokens || 0) + (u.tasks[a].completion_tokens || 0));
      }).map(function(tid) {
        var t = u.tasks[tid];
        var stages = Object.keys(t.stages || {}).map(function(st) {
          return escapeHtml(st) + ' ' + fmtTok((t.stages[st].prompt_tokens || 0)
            + (t.stages[st].completion_tokens || 0));
        }).join(' · ');
        return '<tr><td>' + escapeHtml(tid) + '</td><td><b>' + fmtTok(t.prompt_tokens) + '</b></td>' +
          '<td><b>' + fmtTok(t.completion_tokens) + '</b></td><td>' + t.calls + '</td>' +
          '<td class="slirn-form-hint">' + stages + '</td></tr>';
      }).join('');
      box.innerHTML =
        '<table class="slirn-sys-probe-table" style="max-width:420px;"><thead><tr><th></th><th>输入</th><th>输出</th><th>调用</th></tr></thead>' +
        '<tbody><tr><td><b>总计</b></td><td><b>' + fmtTok(total.prompt_tokens) + '</b></td>' +
        '<td><b>' + fmtTok(total.completion_tokens) + '</b></td><td>' + total.calls + ' ' + est + '</td></tr>' +
        modelRows + '</tbody></table>' +
        '<div class="slirn-form-hint" style="margin:10px 0 4px;">按任务（阶段括号内为该阶段输入+输出合计）：</div>' +
        '<table class="slirn-sys-probe-table"><thead><tr><th>任务</th><th>输入</th><th>输出</th><th>调用</th><th>阶段</th></tr></thead>' +
        '<tbody>' + taskRows + '</tbody></table>';
    }).catch(function() {});
    loadUsageDetail();
  }

  // 明细分页状态：page 从 1 起；size 每页条数；筛选条件每次从输入框现取
  var USAGE_STATE = {page: 1, size: 50};

  function usageFillSelect(id, options, allLabel) {
    var sel = document.getElementById(id);
    if (!sel) return;
    var cur = sel.value;
    sel.innerHTML = '<option value="">' + allLabel + '</option>' +
      (options || []).map(function(o) {
        return '<option' + (o === cur ? ' selected' : '') + '>' + escapeHtml(o) + '</option>';
      }).join('');
  }

  function loadUsageDetail() {
    var body = document.getElementById('slirn-usage-detail');
    if (!body) return;
    var gv = function(id) { return ((document.getElementById(id) || {}).value || '').trim(); };
    var payload = {limit: USAGE_STATE.size, offset: (USAGE_STATE.page - 1) * USAGE_STATE.size};
    ['task_id', 'model', 'stage', 'time_from', 'time_to'].forEach(function(k) {
      var v = gv(k === 'task_id' ? 'slirn-usage-filter' : 'slirn-usage-' + k);
      if (v) payload[k] = v;
    });
    ['dmin', 'dmax'].forEach(function(k) {
      var v = gv('slirn-usage-' + k);
      if (v !== '' && isFinite(v)) payload[k === 'dmin' ? 'dur_min_ms' : 'dur_max_ms'] = Math.round(parseFloat(v) * 1000);
    });
    body.innerHTML = '<div class="slirn-form-hint">⏳ 加载中…</div>';
    postJSON(SLIRN_API + '/llm_usage_log', payload).then(function(r) {
      if (!r || !r.ok) { body.innerHTML = ''; return; }
      // 下拉选项（来自日志中出现过的值；保留当前选中）
      usageFillSelect('slirn-usage-model', r.facets && r.facets.models, '模型（全部）');
      usageFillSelect('slirn-usage-stage', r.facets && r.facets.stages, '阶段（全部）');
      var recs = r.records || [];
      var total = r.total || 0;
      var pages = r.pages || Math.max(1, Math.ceil(total / USAGE_STATE.size));
      var rows = recs.map(function(x) {
        return '<tr><td>' + escapeHtml(String(x.ts || '').replace('T', ' ')) + '</td>' +
          '<td>' + escapeHtml(x.model || '') + '</td>' +
          '<td>' + escapeHtml(x.task_id || '-') + '</td>' +
          '<td>' + escapeHtml(x.stage || '') + '</td>' +
          '<td><b>' + fmtTok(x.prompt_tokens) + '</b>' + (x.estimated ? '<span class="slirn-form-hint">≈</span>' : '') + '</td>' +
          '<td><b>' + fmtTok(x.completion_tokens) + '</b></td>' +
          '<td>' + (x.elapsed_ms / 1000).toFixed(1) + 's</td></tr>';
      }).join('');
      var empty = recs.length ? '' : '<tr><td colspan="7" class="slirn-form-hint">无匹配记录</td></tr>';
      // 分页条：‹ 上一页 / 页码 / 下一页 › + 每页条数切换
      var sizeBtns = [50, 100, 200].map(function(s) {
        return '<button class="slirn-btn' + (s === USAGE_STATE.size ? ' slirn-btn-on' : '') +
          '" data-action="usage-page-size" data-size="' + s + '" style="padding:2px 8px; font-size:12px;">' + s + '</button>';
      }).join('');
      var prevDisabled = USAGE_STATE.page <= 1;
      var nextDisabled = USAGE_STATE.page >= pages;
      var pager =
        '<div class="slirn-usage-pager">' +
          '<span class="slirn-form-hint">共 ' + total + ' 笔 · 每页 </span>' + sizeBtns +
          '<button class="slirn-btn" data-action="usage-page-nav" data-page="' + (USAGE_STATE.page - 1) + '"' +
            (prevDisabled ? ' disabled' : '') + ' style="padding:2px 8px;">‹</button>' +
          '<span class="slirn-form-hint">第 ' + USAGE_STATE.page + ' / ' + pages + ' 页</span>' +
          '<button class="slirn-btn" data-action="usage-page-nav" data-page="' + (USAGE_STATE.page + 1) + '"' +
            (nextDisabled ? ' disabled' : '') + ' style="padding:2px 8px;">›</button>' +
          '<span class="slirn-form-hint" style="margin-left:8px;">≈ 为网关未报 usage 的字符估算</span>' +
        '</div>';
      body.innerHTML =
        '<table class="slirn-sys-probe-table"><thead><tr><th>时间</th><th>模型</th><th>任务</th><th>阶段</th>' +
        '<th>输入</th><th>输出</th><th>耗时</th></tr></thead><tbody>' + rows + empty + '</tbody></table>' + pager;
    }).catch(function() { body.innerHTML = ''; });
  }
  function renderLLMList(models, current) {
    LLM_MODELS = models;
    var cur = document.getElementById('slirn-llm-current');
    if (cur) cur.textContent = current || '（无）';
    var list = document.getElementById('slirn-llm-list');
    if (!list) return;
    if (!models.length) {
      list.innerHTML = '<div class="slirn-llm-empty">尚未注册模型，请在下方添加</div>';
      return;
    }
    list.innerHTML = models.map(function(m) {
      var isCur = m.id === current;
      var proto = m.protocol === 'anthropic' ? 'Anthropic' : 'OpenAI 兼容';
      // REQ-20260919-061 用户补充：UI 显式显示「多模态」徽章，一眼看清哪些能用图片
      var visionBadge = m.vision ? '<span class="slirn-llm-item-vision" title="支持图片输入（多模态）">🖼 多模态</span>' : '';
      return '<div class="slirn-llm-item' + (isCur ? ' current' : '') + '">' +
        '<div class="slirn-llm-item-head">' +
          '<span class="slirn-llm-item-name">' + (isCur ? '⭐ ' : '') + escapeHtml(m.id) +
            '<span class="slirn-llm-item-prov">' + escapeHtml(m.provider || '') + '</span>' +
            '<span class="slirn-llm-item-proto">' + proto + '</span>' +
            visionBadge + '</span>' +
          '<span class="slirn-llm-item-key ' + (m.key_present ? 'ok' : 'miss') + '">' +
            (m.key_present ? '✅ Key 已配置' : '❌ 未配置 ' + escapeHtml(m.api_key_env)) + '</span>' +
        '</div>' +
        '<div class="slirn-llm-item-url">' + escapeHtml(m.base_url) +
          ' <code>' + escapeHtml(m.api_key_env) + '</code></div>' +
        '<div class="slirn-llm-item-ops">' +
          (isCur
            ? '<span class="slirn-llm-item-cur">✅ 当前使用中</span>'
            : '<button class="slirn-btn slirn-btn-sm slirn-btn-primary" data-action="llm-use" data-id="' +
              escapeHtml(m.id) + '">⭐ 设为当前</button>') +
          '<button class="slirn-btn slirn-btn-sm" data-action="llm-test" data-id="' + escapeHtml(m.id) + '">测试</button>' +
          '<button class="slirn-btn slirn-btn-sm" data-action="llm-edit" data-id="' + escapeHtml(m.id) + '">编辑</button>' +
          '<button class="slirn-btn slirn-btn-sm" data-action="llm-remove" data-id="' + escapeHtml(m.id) + '">删除</button>' +
        '</div>' +
      '</div>';
    }).join('');
  }
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function(c) {
      return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
    });
  }

  // 上传文件到我们自己的端点 — 用 XHR 拿进度事件，弹窗一直显示
  // 不用 Gradio 内置 /gradio_api/upload：那个端点用 python_multipart，并发会截断大文件
  function uploadFile(file) {
    openUploadDialog(file);
    return new Promise(function(resolve, reject) {
      var fd = new FormData();
      fd.append('files', file);
      var xhr = new XMLHttpRequest();
      xhr.open('POST', SLIRN_API + '/upload_video');
      xhr.upload.onprogress = function(e) {
        if (e.lengthComputable) setUploadProgress(e.loaded, e.total);
        else setUploadProgress(0, file.size);
      };
      xhr.onload = function() {
        try {
          var j = JSON.parse(xhr.responseText);
          if (j && j.ok && j.path) {
            resolve(j.path);
          } else {
            reject(new Error((j && j.error) || ('Upload failed: ' + xhr.status)));
          }
        } catch (parseErr) {
          reject(new Error('Upload parse error: ' + parseErr));
        }
      };
      xhr.onerror = function() { reject(new Error('网络错误，上传失败')); };
      xhr.onabort = function() { reject(new Error('上传已取消')); };
      xhr.send(fd);
    });
  }

  // 业务回调：把 server 返回的 file_info / cut_done 渲染到 DOM
  function applyFileInfo(info) {
    if (!info) return;
    var el = document.getElementById('slirn-file-info-display');
    if (el) el.innerHTML = '<div class="slirn-status-msg">📁 ' + info.name + '（' + info.size_mb + ' MB · ' + info.duration + '）</div>';
    var card = document.getElementById('slirn-player-card');
    var v = document.getElementById('slirn-player');
    // Gradio 6 文件 API：/gradio_api/file=<urlencoded-path>
    if (card && v) { v.src = '/gradio_api/file=' + encodeURI(info.path); v.load(); card.style.display = ''; bindSpeedControl(v); }  // 倍速控件（REQ-20260916-014）
    // 把 seek 滑块的最大值设为视频时长（默认 HTML 写死 600，需要根据实际视频更新）
    var seek = document.getElementById('slirn-seek');
    if (seek && info.duration_seconds) {
      seek.max = info.duration_seconds;
      seek.value = 0;
    }
    // 更新时间显示 HH:MM:SS.mmm / HH:MM:SS.mmm
    var timeDisp = document.getElementById('slirn-time-display');
    if (timeDisp && info.duration) {
      timeDisp.textContent = '00:00:00.000 / ' + info.duration + '.000';
    }
  }
  function applyCutDone(info) {
    if (!info) return;
    var msg = document.getElementById('slirn-cut-msg');
    if (msg) msg.innerHTML = '<div class="slirn-status-msg">✅ 待剪辑视频已生成 ' + info.size_mb + ' MB</div>';
    var wrap = document.getElementById('slirn-cut-preview-wrap');
    var v = document.getElementById('slirn-cut-preview');
    if (wrap && v) { v.src = '/gradio_api/file=' + encodeURI(info.path); v.load(); wrap.style.display = ''; bindSpeedControl(v); }  // 倍速控件（REQ-20260916-014）
  }

  // ===== 字幕生成：后台轮询 + 详情刷新（REQ-20260915-001）=====
  function fmtElapsed(sec) {
    var m = Math.floor(sec / 60), s = sec % 60;
    return m > 0 ? (m + ' 分 ' + s + ' 秒') : (s + ' 秒');
  }

  function refreshDetail(tid) {
    postJSON(SLIRN_API + '/view_task', {task_id: tid}).then(function(r) {
      if (r && r.ok && r.html) {
        var d = document.getElementById('slirn-tab-detail');
        if (d) { d.innerHTML = r.html; d.style.display = ''; }
        bindSubPlayer();
        bindRevPlayer();
      }
    });
  }

  // ===== 编辑任务 / 剪辑工作台（REQ-20260915-003）=====
  function initTaskEdit() {
    var st = document.getElementById('slirn-edit-state');
    if (!st) { window.slirnEditTaskId = null; return; }
    window.slirnEditTaskId = st.getAttribute('data-task-id');
    window.slirnSelectedFile = st.getAttribute('data-video-path') || '';
    // 播放器：完整原视频 + 滑块全长
    var card = document.getElementById('slirn-player-card');
    var v = document.getElementById('slirn-player');
    var url = st.getAttribute('data-video-url');
    if (card && v && url) {
      v.src = url; v.load();
      card.style.display = '';
      bindSpeedControl(v);  // 倍速控件（REQ-20260916-014）
    }
    var durSec = parseFloat(st.getAttribute('data-duration-seconds')) || 0;
    var seek = document.getElementById('slirn-seek');
    if (seek && durSec > 0) { seek.max = durSec; seek.value = 0; }
    var td = document.getElementById('slirn-time-display');
    if (td && durSec > 0) {
      var ds = secondsToHMS(durSec);
      td.textContent = '00:00:00.000 / ' + ds + '.000';
    }
    try { window.slirnRenderHwChips && window.slirnRenderHwChips(); } catch (e) {}
    // REQ-20260926-NNN：编辑页 HTML 是动态注入的，initHotwordPicker 没机会跑，
    // → picker 点击切换 + 分析网格点击切换 .selected 都没挂上。手动补一次。
    try { initHotwordPicker(); } catch (e) {}
    // REQ-20260926-NNN：成员管理折叠块 — 编辑页注入后拉一次成员列表
    try { _loadTaskMembers(); } catch (e) {}
  }

  // REQ-20260926-NNN：编辑页/剪辑页 URL hash 持久化（互斥）：
  //   进入剪辑 → #wb=<tid>（同时清掉 #edit=）；进入编辑 → #edit=<tid>（清掉 #wb=）；
  //   离开（取消/返回列表）→ 清空 hash，避免刷新跳回任务页。
  function _setTaskPageHash(page, tid) {
    try {
      var h = '#' + page + '=' + encodeURIComponent(tid || '');
      history.replaceState(null, '', h);  // 整 hash 替换 → 自动清掉对侧
    } catch (e) {}
  }
  function _clearTaskPageHash() {
    try {
      // 用 pathname+search 替换（去掉 #...），刷新就不会再触发任务页恢复
      var u = (location && (location.pathname + location.search)) || '/';
      history.replaceState(null, '', u);
    } catch (e) {}
  }
  function _loadEditTaskHTML(tid) {
    // 提取编辑页 HTML 注入 + initTaskEdit（编辑页与 hash 恢复共用）
    return postJSON(SLIRN_API + '/edit_task', {task_id: tid}).then(function(r) {
      if (!(r && r.ok && r.html)) return r;
      var c = document.getElementById('slirn-tab-create');
      if (c) { c.innerHTML = r.html; c.style.display = ''; }
      ALL_TABS.forEach(function(id) {
        if (id !== 'slirn-tab-create') { var el = document.getElementById(id); if (el) el.style.display = 'none'; }
      });
      var dE = document.getElementById('slirn-tab-detail');
      if (dE) dE.style.display = 'none';
      try { initTaskEdit(); } catch (e) {}
      return r;
    });
  }

  function openWorkbench(tid) {
    // REQ-20260918-053：进 wb 时把 task_id 写到 URL hash，F5/Cmd+R 刷新仍在同一任务页
    if (tid) _setTaskPageHash('wb', tid);
    postJSON(SLIRN_API + '/workbench', {task_id: tid}).then(function(r) {
      hideWbLoader();  // REQ-20260918-051：无论成功失败都关掉等待弹窗
      if (r && r.ok && r.html) {
        var w = document.getElementById('slirn-tab-workbench');
        if (!w) return;
        // 刷新前快照（REQ-20260918-046）：哪些阶段已完成 + 用户当前所在面板 —
        // 供刷新后判断「当前阶段刚完成 → 自动进下一阶段」
        var prevDone = {}, prevActive = '';
        var hadWb = !!document.querySelector('#slirn-tab-workbench .slirn-wb-stage');
        if (hadWb) {
          document.querySelectorAll('#slirn-tab-workbench .slirn-wb-stage.done').forEach(function(s) {
            prevDone[s.getAttribute('data-pane') || ''] = 1;
          });
          document.querySelectorAll('#slirn-tab-workbench .slirn-wb-pane').forEach(function(p) {
            if (p.style.display !== 'none') prevActive = (p.id || '').replace('slirn-wb-pane-', '');
          });
        }
        // ===== REQ-20260918-047 v3：跨 innerHTML 替换保留 wb 内全部表单状态 =====
        // 用户反馈：wb 内任何复选框勾上后点其他地方 → 勾选被清空。
        // 根因：openWorkbench 走 w.innerHTML = r.html 整体替换 wb，wb 内
        // 所有 <input> / <select> / <textarea> 的用户当前状态（勾选 / 输入 /
        // 选中项）随旧 DOM 一起销毁。即使 pipe-panel 已 detach 保住，「阶段
        // 自动进入下一阶段」「字幕生成阶段区分说话人」「备注输入」「决策
        // 下拉」这些 wb 其他位置的表单仍然丢失。修复：替换前对 wb + pipe-panel
        // 内所有有 id 的表单做快照，替换 + bind/apply 状态恢复之后逐项
        // 回填用户的当前值，用户操作完整保留。
        function _snapInputs(root) {
          if (!root) return {};
          var snap = {};
          root.querySelectorAll('input, select, textarea').forEach(function(el) {
            if (!el.id) return;
            if (el.type === 'checkbox') snap[el.id] = {kind: 'cb', checked: el.checked};
            else if (el.type === 'radio') { if (el.checked) snap[el.id] = {kind: 'radio'}; }
            else snap[el.id] = {kind: 'val', value: el.value};
          });
          return snap;
        }
        function _restoreInputs(snap) {
          Object.keys(snap).forEach(function(id) {
            var el = document.getElementById(id);
            if (!el) return;  // 新 wb 里没这字段（数据驱动）→ 跳过
            var s = snap[id];
            if (s.kind === 'cb') el.checked = s.checked;
            else if (s.kind === 'radio') el.checked = true;
            else el.value = s.value;
          });
        }
        // 1) 快照 pipe-panel + pipe-status（它们 detach 后会保留，但保险起见也快照）
        var _pipePanel = document.getElementById('slirn-pipe-panel');
        var _pipeStatus = document.getElementById('slirn-pipe-status');
        var _panelSnap = _snapInputs(_pipePanel);
        var _statusSnap = _snapInputs(_pipeStatus);
        // 2) 快照 wb 内除 pipe-panel + pipe-status 之外的所有表单
        var _wbAllInputsSnap = {};
        if (w) {
          w.querySelectorAll('input, select, textarea').forEach(function(el) {
            if (!el.id) return;
            if (el === _pipePanel || el === _pipeStatus) return;
            if (_pipePanel && _pipePanel.contains(el)) return;
            if (_pipeStatus && _pipeStatus.contains(el)) return;
            if (el.type === 'checkbox') _wbAllInputsSnap[el.id] = {kind: 'cb', checked: el.checked};
            else if (el.type === 'radio') { if (el.checked) _wbAllInputsSnap[el.id] = {kind: 'radio'}; }
            else _wbAllInputsSnap[el.id] = {kind: 'val', value: el.value};
          });
        }
        // 3) detach pipe-panel + pipe-status
        if (_pipePanel) _pipePanel.parentNode.removeChild(_pipePanel);
        if (_pipeStatus) _pipeStatus.parentNode.removeChild(_pipeStatus);
        // 3b) 播放器清理（REQ-20260922-NNN）：浮层里的旧 wrap 不在 w 内，
        // innerHTML 换不掉它 — 不暂停的话旧视频会带着已失效的行引用/跳播
        // 序列继续裸播（已删除整条、列表外内容会原样播出去，列表播完也不停）。
        // 暂停 + 收起浮层；新播放器再播放时 vfShow 自动弹回
        var _vfL = document.getElementById('slirn-video-float');
        if (_vfL) {
          _vfL.querySelectorAll('video').forEach(function(vd) { try { vd.pause(); } catch (err) {} });
          _vfL.hidden = true;
        }
        if (w) w.querySelectorAll('video').forEach(function(vd) { try { vd.pause(); } catch (err) {} });
        w.innerHTML = r.html;
        ALL_TABS.forEach(function(id) { var el = document.getElementById(id); if (el) el.style.display = 'none'; });
        var d = document.getElementById('slirn-tab-detail');
        if (d) d.style.display = 'none';
        w.style.display = '';
        window.scrollTo({top: 0, behavior: 'smooth'});
        bindSubPlayer();
        bindRevPlayer();
        // 重渲染后旧行引用全部失效：试听状态清零（防跳播序列指向已移除的行）
        cutKeepSeq = null; cutKeepIdx = 0; cutKeepMode = null;
        bindCutPlayer();  // 切分修剪播放器（timeupdate 高亮/自动停/跳播 — REQ-20260916-011）
        var rcPv = document.getElementById('slirn-rc-player');
        if (rcPv) bindSpeedControl(rcPv);  // 粗剪成片预览倍速（REQ-20260916-014 同款）
        var optPv = revVis('slirn-opt-player');
        if (optPv) bindSpeedControl(optPv);  // 优化字幕行播放倍速（REQ-20260917-030）
        bindOptRows(tid);
        setupOptWordsPagination();  // REQ-20260918-050：词频列表分页（每次 wb 重渲后调）
        bindOptRowSearch();  // REQ-20260923-NNN 行搜索输入框（幂等重绑）
        bindOptBatchInputs();  // REQ-20260929-NNN 批量替换面板输入框（幂等重绑）
        optInputOverflowInit();  // REQ-20260918-058：替换输入框溢出检测 + 自动换行
        bindFineControls();  // REQ-20260919-061：精剪视频·素材合成器控件绑定（位置/缩放/字体/输出自动保存）
        bindFineSteppers();  // REQ-20260919-061：精剪视频·数值控件（number input + ▲▼）与滑块双向同步
        bindCropAspectLink();  // REQ-20260919-061a：crop_w / crop_h 按 16:9 联动（防变形）
        bindBgWhiteDetector();  // REQ-20260919-062：背景图白色区域检测（算法 + 阈值 + 4 角点 + 填充）
        bindFineMaterialPreviews();  // REQ-20260919-063：每个素材的 👁️ 预览按钮（图片/视频/音频/SRT）
        // REQ-20261028-NNN：工作台重渲后重拉「系统提供的 BGM」列表（选项虽已
        // 服务端直出，仍需刷新 available + 回显选中 — 此前唯一的填充时机是
        // pipeline loadPanel，同任务重渲被 slirnPipelineMount 短路 → 下拉被
        // innerHTML 替换后无人再填充，只剩「— 不选 —」占位）。
        if (typeof window.fineDefaultBgmLoad === 'function') {
          try { window.fineDefaultBgmLoad(); } catch (e) { console.warn('[bgm-load]', e); }
        }
        // 模板列表已搬到「📥 引用参数」modal 里 — wb 渲染时不主动拉，按需由 fineImportShow() 取
        var optSt = revVis('slirn-opt-status');  // 优化 job 还在跑 → 恢复轮询
        if (optSt && optSt.dataset.taskId && optSt.dataset.state === 'running')
          startOptPolling(optSt.dataset.taskId);
        // REQ-20260922-NNN 标记删除行：优化成片剪辑还在跑 → 恢复轮询
        var optCutSt = revVis('slirn-opt-cut-status');
        if (optCutSt && optCutSt.dataset.taskId && optCutSt.dataset.state === 'running')
          startOptCutPolling(optCutSt.dataset.taskId);
        applyWbStagesState();  // 恢复上次收起/展开（跨刷新保持 — REQ-20260916-002）
        applyRevRigorState();  // 上次选过的严谨性级别预填（REQ-20260916-003）
        applyWbAutoNextState();  // 自动进下一阶段开关回填（REQ-20260918-046）
        wbAutoNextMaybe(prevDone, hadWb, prevActive);  // 当前阶段刚完成 → 按设置跳下一阶段
        // 4) 恢复 wb 内非 pipe-panel / 非 pipe-status 的表单（apply* 状态回填之后，
        //    用户手动改的值会覆盖回填，这是预期的：用户最近的操作胜出）
        _restoreInputs(_wbAllInputsSnap);
        // 5) 把流程配置面板插回新的 wb-inner
        var _inner = document.getElementById('slirn-tab-workbench-inner');
        if (_inner) {
          // pipe-panel 紧跟 wb 顶部信息卡之后（在 slirn-wb-main 之前；位置与模板一致）
          // REQ-20260921-NNN：用 insertBefore(.slirn-wb-main) 而非 appendChild —
          // appendChild 会把节点搬到 _inner 末尾（流程配置跑到工作台最下边），
          // 与模板「status → panel → wb-main」的位置约定不一致。pipe-panel
          // 必须在 wb-main 之前才能保持「阶段切换时还在原地」的感觉。
          var _wbMainAnchor = _inner.querySelector(':scope > .slirn-wb-main');
          if (!_wbMainAnchor) _wbMainAnchor = null; // null = appendChild 兜底
          if (_pipeStatus && _pipeStatus.parentNode !== _inner) {
            if (_wbMainAnchor) _inner.insertBefore(_pipeStatus, _wbMainAnchor);
            else _inner.appendChild(_pipeStatus);
          }
          if (_pipePanel && _pipePanel.parentNode !== _inner) {
            if (_wbMainAnchor) _inner.insertBefore(_pipePanel, _wbMainAnchor);
            else _inner.appendChild(_pipePanel);
          }
          // ===== BUGFIX：拔掉 r.html 模板里的「空壳」重复元素 =====
          // 现象：用户切换任务（或工作台刷新时）出现两个「流程配置」面板 ——
          // 一个是当前任务的（新挂载，由 slirnPipelineMount 渲染），一个是上一个
          // 任务的（OLD 重新 attach，task-id 与内容都还是旧的）。根因：r.html
          // 模板自带空 <section id="slirn-pipe-panel">，又 appendChild 了 OLD
          // pipe-panel，DOM 里就同时有两个同 id 的元素。
          // 修复：保留 OLD（其内容已加载，跨刷新保住表单状态），删掉模板里的
          // 空壳。slirnPipelineMount 会按 OLD 当前的 data-task-id 与新 tid 比对，
          // 不同则 loadPanel(tid) 重新加载（任务切换场景），相同则保留表单（同
          // 任务刷新场景）。
          // 注意：querySelectorAll 必须用 'not-self' 类名筛除 OLD，否则会把自己
          // 也删掉。我们用「不在 _pipePanel/_pipeStatus 集合内」条件筛选。
          // BUGFIX：只有 OLD 存在时才清理空壳。首次进 wb（重启后）_pipePanel=null，
          // 此时「el !== null」恒为真 → 模板里的空 pipe-panel 会被误删，导致
          // 用户看到「流程配置没了」。必须用 _pipePanel || _pipeStatus 守住。
          if (_pipePanel || _pipeStatus) {
            var _toRemove = [];
            _inner.querySelectorAll('section#slirn-pipe-panel, div#slirn-pipe-status').forEach(function(el) {
              if (el !== _pipePanel && el !== _pipeStatus) _toRemove.push(el);
            });
            _toRemove.forEach(function(el) {
              try { el.parentNode.removeChild(el); } catch (err) {}
            });
          }
        }
        // 6) 恢复 pipe-panel / pipe-status 内表单状态（节点引用保住但保险起见也回填）
        _restoreInputs(_panelSnap);
        _restoreInputs(_statusSnap);
        // 流程配置面板挂载（REQ-20260918-047 v2）：工作台内常驻纵向面板
        // 面板若已存在（同 tid + 有 innerHTML）→ 不重新加载，状态保留
        if (window.slirnPipelineMount) window.slirnPipelineMount(tid);
        // 2026-10-28：合成进行中刷新页面 → 进度显示消失（轮询闭包随页面销毁）。
        // 工作台（重新）加载后查一次合成状态：running 则把按钮/状态行接回轮询，
        // 与点「合成」按钮同一条路径（服务端对在跑任务只回 job，不会重复起）。
        var _rcResumeBtn = document.getElementById('slirn-rc-compose');
        if (_rcResumeBtn && tid) {
          postJSON(SLIRN_API + '/compose_rough_status', {task_id: tid}).then(function(s) {
            if (!s || !s.ok || !s.job) return;
            var j = s.job;
            if (j.state !== 'running') return;
            var pct = Math.max(1, Math.min(99, Math.round(j.progress || 0)));
            _rcResumeBtn.textContent = '🎬 合成中… ' + pct + '%';
            _rcResumeBtn.disabled = true;
            var _st = document.getElementById('slirn-rc-status');
            if (_st) {
              _st.style.display = '';
              _st.textContent = '⏳ 合成进行中 ' + pct + '%（页面刷新后自动接续）— 完成后此处自动显示预览';
            }
            rcWatchJob(tid, _rcResumeBtn);
          });
        }
      } else if (r && r.error) {
        toast('❌ ' + r.error, 'error');
      }
    });
  }
  // REQ-20260921-NNN：暴露到 window，让其他模块（如 pipeline.js 清理所有阶段后）
  // 能强制刷新工作台（让 _wb_stage_states 重算，绿色对号立即消失）。
  window.slirnOpenWorkbench = openWorkbench;

  // ===== 阶段列表收起/展开（localStorage 记忆 — REQ-20260916-002）=====
  function applyWbStagesState() {
    var host = document.getElementById('slirn-tab-workbench-inner');
    if (!host) return;
    var collapsed = '';
    try { collapsed = localStorage.getItem('slirnWbStagesCollapsed') || ''; } catch (err) {}
    if (collapsed !== '0' && collapsed !== '1') {  // 脏值自愈：当作未收起（REQ-20260916-011）
      collapsed = '';
      try { localStorage.removeItem('slirnWbStagesCollapsed'); } catch (err) {}
    }
    host.classList.toggle('wb-stages-collapsed', collapsed === '1');
  }

  // ===== 完成后自动打开下一阶段界面（REQ-20260918-046 / REQ-20260921-NNN）=====
  // 阶段条上方的开关（localStorage 记忆，默认关）。
  // - 勾选：当前阶段完成时 → 自动切到下一阶段并提示
  // - 不勾选：当前阶段完成时 → 保持在原页面（用户可手动切下一阶段）
  function wbAutoNextOn() {
    try { return localStorage.getItem('slirnWbAutoNext') === '1'; } catch (err) { return false; }
  }
  function applyWbAutoNextState() {
    var el = document.getElementById('slirn-wb-autonext');
    if (el) el.checked = wbAutoNextOn();
  }
  document.addEventListener('change', function(e) {
    if (e.target && e.target.id === 'slirn-wb-autonext') {
      try {
        localStorage.setItem('slirnWbAutoNext', e.target.checked ? '1' : '');
      } catch (err) {}
      toast(e.target.checked ? '✅ 已开启：当前阶段完成后自动打开下一阶段界面'
                             : '↩️ 已关闭：阶段完成后保持在原页面（需手动切下一阶段）');
    }
  });
  function wbAutoNextMaybe(prevDone, hadWb, prevActive) {
    // 三重门：刷新过 wb + 用户开启开关 + 用户当前在某个阶段面板
    if (!hadWb || !wbAutoNextOn() || !prevActive) return;
    // REQ-20260918-053：仅迭代真正的流水线阶段（排除「执行日志」等视图阶段）
    var stages = [];
    document.querySelectorAll('#slirn-tab-workbench .slirn-wb-stage:not(.slirn-wb-stage-extra)').forEach(function(s) {
      stages.push(s.getAttribute('data-pane') || '');
    });
    var justDone = '';
    stages.forEach(function(pk) {
      var el = document.querySelector('#slirn-tab-workbench .slirn-wb-stage[data-pane="' + pk + '"]');
      if (el && el.classList.contains('done') && !prevDone[pk] && pk === prevActive) justDone = pk;
    });
    if (!justDone) return;
    var idx = stages.indexOf(justDone);
    if (idx < 0 || idx + 1 >= stages.length) {
      // 最后一个阶段完成 → 即使勾选也不切（已经到底了）
      return;
    }
    switchWbPane(stages[idx + 1]);
    var stageLabel = justDone;
    var stageEl = document.querySelector('#slirn-tab-workbench .slirn-wb-stage[data-pane="' + justDone + '"]');
    if (stageEl) {
      var titleEl = stageEl.querySelector('.slirn-wb-stage-title') || stageEl;
      stageLabel = (titleEl.textContent || '').trim() || justDone;
    }
    toast('✅ 「' + stageLabel + '」完成 — 已自动打开下一阶段界面（可关掉阶段上方的「完成后自动打开下一阶段界面」开关改为手动切）');
  }

  // ===== 严谨性级别：上次选择预填（不发起新分析也可见 — REQ-20260916-003）=====
  function applyRevRigorState() {
    if (!document.querySelector('input[name="slirn-rev-rigor"]:checked')) {
      var saved = '';
      try { saved = localStorage.getItem('slirnRevRigor') || ''; } catch (err) {}
      if (['high', 'medium', 'low', 'custom'].indexOf(saved) >= 0) {
        var el = document.querySelector('input[name="slirn-rev-rigor"][value="' + saved + '"]');
        if (el) el.checked = true;
      }
    }
    syncRigorCustomUI();  // 自定义档编辑区跟随（REQ-20260916-007）
  }

  // ===== 自定义严谨性（REQ-20260916-007/009）：编辑区展开 + 底稿/草稿预填 =====
  // 选中「自定义」才展开；textarea 首次展开预填 localStorage 草稿，无草稿用高档底稿
  // （data-prompt-high 与服务端 default_custom_prompt 回退逻辑同源），此后不再覆盖用户编辑
  function syncRigorCustomUI() {
    var wrap = document.getElementById('slirn-rigor-custom');
    if (!wrap) return;
    var sel = document.querySelector('input[name="slirn-rev-rigor"]:checked');
    var isCustom = !!(sel && sel.value === 'custom');
    wrap.style.display = isCustom ? '' : 'none';
    var ta = wrap.querySelector('.slirn-rigor-custom-text');
    if (isCustom && ta && !ta.dataset.slirnFilled) {
      var draft = '';
      try { draft = localStorage.getItem('slirnRevCustomPrompt') || ''; } catch (err) {}
      ta.value = draft || wrap.getAttribute('data-prompt-high') || '';
      ta.dataset.slirnFilled = '1';
    }
  }
  document.addEventListener('change', function(e) {
    if (e.target && e.target.name === 'slirn-rev-rigor') syncRigorCustomUI();
    // REQ-20260926-NNN：任务列表「我的 / 全部」radio 切换 → 自动刷新
    if (e.target && e.target.name === 'slirn-task-scope') {
      postJSON(SLIRN_API + '/refresh_tasks', {scope: e.target.value})
        .then(function(r) { handleResp(r, 'slirn-tab-tasks'); });
    }
    // 切分修剪·组级决策下拉（REQ-20260917-027）：鼠标改判与快捷键 K/D/S 同效；
    // 选「维持原状」=取消改判，选回当前值 = 无操作（区别于键盘的「再按同键取消」）
    if (e.target && e.target.classList && e.target.classList.contains('slirn-cut-actsel')) {
      var gAct = e.target.closest('.slirn-cut-group');
      if (gAct) {
        var nvAct = e.target.value || '';
        if (nvAct && (gAct.getAttribute('data-act') || '') === nvAct) return;
        cutApplyAct(gAct, nvAct);
      }
    }
    // 优化字幕·替换值被编辑（REQ-20260917-038）：编辑即视为明确处理 → 计入词进度
    if (e.target && e.target.classList && e.target.classList.contains('slirn-opt-after')) {
      optOccMarkReviewed(e.target.closest('.slirn-opt-occ'));
    }
    // 决策下拉变化 → 行 data-decision/data-final 跟随（实质口径），过滤
    // 计数/可见性实时刷新（REQ-20260916-010；未保存前纯前端，刷新即还原）
    if (e.target && e.target.classList && e.target.classList.contains('slirn-rev-select')) {
      var rowF = e.target.closest('.slirn-rev-row');
      if (rowF) {
        var dv = e.target.value || 'pending';
        var suggF = rowF.getAttribute('data-sugg') || '';
        var finF = '';
        if (dv === 'keep' || dv === 'delete' || dv === 'split' || dv === 'fix') finF = dv;
        else if (dv === 'accept' && (suggF === 'keep' || suggF === 'delete'
            || suggF === 'split' || suggF === 'fix')) finF = suggF;
        rowF.setAttribute('data-decision', dv);
        rowF.setAttribute('data-final', finF);
        // 决策改为切分/内容更正 → 展开详情并把行内输入框备好（REQ-20260917-026）：
        // 输入框语义跟随决策（split=切分修剪后内容 / fix=更正后内容），为空时从
        // 详情块的模型建议文本（✂️ 建议保留 / ✏️ 更正后）预填，直接可微调；
        // 切分修剪阶段自动取输入框值生效（cutlist_service._fix_target/_split_target）
        var noteF = rowF.querySelector('.slirn-rev-note-input');
        if (noteF && (dv === 'split' || dv === 'fix')) {
          noteF.placeholder = dv === 'fix' ? '更正后内容（可微调）' : '切分修剪后内容（可空）';
          if (!noteF.value.trim()) {
            var ktF = rowF.querySelector('.slirn-rev-keeptext');
            var mF = ktF && /「(.+)」/.exec(ktF.textContent || '');
            if (mF) noteF.value = mF[1];
          }
          rowF.classList.add('open');
          var tgF = rowF.querySelector('.slirn-rev-toggle');
          if (tgF) tgF.textContent = '▴';
          try {
            noteF.focus();
            noteF.setSelectionRange(noteF.value.length, noteF.value.length);
          } catch (err) {}
        }
        revFilterSync();
      }
    }
  });
  // 草稿实时保存（仅本机浏览器）— 不依赖点「分析」提交
  document.addEventListener('input', function(e) {
    if (e.target && e.target.classList &&
        e.target.classList.contains('slirn-rigor-custom-text')) {
      try { localStorage.setItem('slirnRevCustomPrompt', e.target.value); } catch (err) {}
    }
  });

  function switchWbPane(paneKey) {
    document.querySelectorAll('.slirn-wb-stage').forEach(function(s) {
      s.classList.toggle('active', s.getAttribute('data-pane') === paneKey);
    });
    document.querySelectorAll('.slirn-wb-pane').forEach(function(p) {
      p.style.display = (p.id === 'slirn-wb-pane-' + paneKey) ? '' : 'none';
    });
    // 面板显隐切换后过滤计数才可见：重算 chips/可见性（工作台初始打开时
    // 修订面板可能隐藏，revRows 取不到行 → 计数 0；REQ-20260916-010）
    revFilterSync();
    // REQ-20260918-053：首次进入执行日志面板时自动加载一次（无需手动点刷新）
    if (paneKey === 'logs' && !logsState._loaded) {
      loadLogs();
      logsState._loaded = true;
    }
  }

  // ===== REQ-20260918-053：执行日志面板（过滤 + 拉取 + 渲染）=====
  // 全局状态（同一时刻只看一个任务）：面板内 chip 切换 + 关键词输入 + 刷新按钮都改它
  // REQ-20260920-084：新增 timeFrom / timeTo / auto 过滤
  // REQ-20260920-086：新增 page / pageSize 分页
  var logsState = {
    kinds: [],        // [] = 不限（默认全选）
    statuses: [],     // [] = 不限
    keyword: '',
    timeFrom: '',     // ISO 8601；空 = 不限
    timeTo: '',       // ISO 8601；空 = 不限
    auto: 'any',      // 'any' | 'manual' | 'auto'
    page: 1,          // REQ-086：当前页（从 1 起）
    pageSize: 20,     // REQ-086：每页条数（默认 20，可改 10/50/100）
    _loaded: false,   // 首次进入是否已加载
  };

  function _logsReadFilters() {
    var box = document.getElementById('slirn-wb-pane-logs');
    if (!box) return;
    logsState.kinds = [];
    box.querySelectorAll('.slirn-chip[data-log-kind].active').forEach(function(b) {
      logsState.kinds.push(b.getAttribute('data-log-kind') || '');
    });
    logsState.statuses = [];
    box.querySelectorAll('.slirn-chip[data-log-status].active').forEach(function(b) {
      logsState.statuses.push(b.getAttribute('data-log-status') || '');
    });
    // REQ-20260920-084：读 auto chip
    var autoActive = box.querySelector('.slirn-chip[data-log-auto].active');
    logsState.auto = autoActive ? (autoActive.getAttribute('data-log-auto') || 'any') : 'any';
    // REQ-20260920-084：读时间段 chip
    var timeActive = box.querySelector('.slirn-chip[data-log-time].active');
    var timeRange = timeActive ? (timeActive.getAttribute('data-log-time') || '') : '';
    if (timeRange === 'today') {
      var d = new Date();
      d.setHours(0, 0, 0, 0);
      logsState.timeFrom = d.toISOString();
      logsState.timeTo = '';
    } else if (timeRange === '7d') {
      var d7 = new Date(Date.now() - 7 * 86400000);
      logsState.timeFrom = d7.toISOString();
      logsState.timeTo = '';
    } else if (timeRange === '30d') {
      var d30 = new Date(Date.now() - 30 * 86400000);
      logsState.timeFrom = d30.toISOString();
      logsState.timeTo = '';
    } else {
      logsState.timeFrom = '';
      logsState.timeTo = '';
    }
    var kw = document.getElementById('slirn-logs-keyword');
    logsState.keyword = kw ? (kw.value || '') : '';
  }

  function loadLogs() {
    var list = document.getElementById('slirn-logs-list');
    if (!list) return;
    var tid = list.getAttribute('data-task-id') || '';
    if (!tid) return;
    _logsReadFilters();
    var payload = {
      task_id: tid,
      kinds: logsState.kinds,
      statuses: logsState.statuses,
      keyword: logsState.keyword,
      // REQ-20260920-084：传 time_from / time_to / auto 给 list_logs
      time_from: logsState.timeFrom || '',
      time_to: logsState.timeTo || '',
      auto: logsState.auto || 'any',
      // REQ-20261028-NNN：长/短视频日志分流 — 工作台只看长视频记录
      flow: 'long',
      // REQ-20260920-086：分页 offset / limit（替代旧 limit=200）
      offset: (Math.max(1, logsState.page) - 1) * logsState.pageSize,
      limit: logsState.pageSize,
    };
    var countEl = document.querySelector('.slirn-logs-count');
    if (countEl) countEl.textContent = '加载中…';
    // REQ-20260920-084：改调 /slirn/api/list_logs（REQ-081 新端点；支持 time_from / auto）
    postJSON(SLIRN_API + '/list_logs', payload).then(function(r) {
      if (!r || !r.ok) {
        if (countEl) countEl.textContent = '加载失败';
        list.innerHTML = '<div class="slirn-form-hint slirn-logs-empty">加载失败：' + escapeHtml((r && r.error) || '未知错误') + '</div>';
        return;
      }
      _renderLogsList(list, r.items || []);
      _renderLogsPager(r);
      // REQ-086：countEl 显示「过滤后总条数」（不是当前页）
      if (countEl) countEl.textContent = (r.total || 0) + ' 条';
    }).catch(function(err) {
      if (countEl) countEl.textContent = '加载失败';
      list.innerHTML = '<div class="slirn-form-hint slirn-logs-empty">加载失败：' + escapeHtml(String(err)) + '</div>';
    });
  }

  // REQ-20260920-086：渲染分页导航（首页/上一页/下一页/末页 + 当前页/总页数/总条数）
  function _renderLogsPager(r) {
    var pager = document.getElementById('slirn-logs-pager');
    if (!pager) return;
    var total = r.total || 0;
    var pageSize = r.page_size || logsState.pageSize;
    var totalPages = r.total_pages || Math.max(1, Math.ceil(total / pageSize));
    var page = r.page || 1;
    pager.setAttribute('data-page', String(page));
    pager.setAttribute('data-page-size', String(pageSize));
    pager.setAttribute('data-total', String(total));
    var pageEl = pager.querySelector('[data-bind="page"]');
    var tpEl = pager.querySelector('[data-bind="total-pages"]');
    var totalEl = pager.querySelector('[data-bind="total"]');
    if (pageEl) pageEl.textContent = String(page);
    if (tpEl) tpEl.textContent = String(totalPages);
    if (totalEl) totalEl.textContent = String(total);
    // 同步 logsState.page / pageSize（防止 back/forward 不一致）
    logsState.page = page;
    logsState.pageSize = pageSize;
    // 翻页按钮 disabled 状态
    var btns = pager.querySelectorAll('.slirn-pager-btn');
    btns.forEach(function(b) {
      var act = b.getAttribute('data-pager');
      var disabled = (act === 'first' || act === 'prev')
        ? page <= 1
        : page >= totalPages;
      b.disabled = disabled;
    });
  }

  // REQ-20260918-053：阶段中文标签（与后端 KIND_LABELS 对齐，复制一份避免跨域/加载顺序问题）
  // REQ-20260920-084：补齐 REQ-074 / REQ-081 加的 6 个 kind
  var LOG_KIND_LABELS = {
    subtitle_generation: '字幕生成',
    subtitle_review: '字幕修订',
    rough_cut: '切分修剪',
    rough_cut_link_person: '关联人员ID',
    rough_compose: '粗剪合成',
    rough_compose_delete: '删除粗剪成品',
    optimize: '优化字幕',
    fine_ai_layout: 'AI 智能布局',
    fine_bg_detect: '检测区域',
    fine_preview: '生成预览',
    fine_export: '最终导出视频',
    short_video_ai: '短视频分镜',
    short_video_render: '短视频渲染',
  };
  function _formatLogsDuration(ms) {
    if (!ms || ms < 0) return '-';
    var s = Math.floor(ms / 1000);
    if (s < 60) return s + '秒';
    var m = Math.floor(s / 60);
    var rs = s % 60;
    if (m < 60) return m + '分' + rs + '秒';
    var h = Math.floor(m / 60);
    var rm = m % 60;
    return h + '时' + rm + '分';
  }
  function _renderLogsList(list, items) {
    if (!items.length) {
      list.innerHTML = '<div class="slirn-form-hint slirn-logs-empty">无符合条件的记录。点击「清除」放宽过滤试试。</div>';
      return;
    }
    var html = '';
    items.forEach(function(it) {
      var status = it.status || 'running';
      var badgeCls = status === 'success' ? 'slirn-exec-ok'
                   : status === 'failed' ? 'slirn-exec-fail'
                   : 'slirn-exec-running';
      var badgeText = status === 'success' ? '✅ 成功'
                    : status === 'failed' ? '❌ 失败'
                    : '⏳ 运行中';
      var kindLabel = LOG_KIND_LABELS[it.kind] || it.kind || '操作';
      var startIso = (it.started_at_iso || '').replace('T', ' ').slice(0, 19);
      var endIso = (it.finished_at_iso || '').replace('T', ' ').slice(0, 19) || '—';
      var duration = _formatLogsDuration(it.duration_ms);
      var extra = it.extra || {};
      var extraBits = [];
      if (extra.lines) extraBits.push(extra.lines + ' 行');
      if (extra.occurrences) extraBits.push(extra.occurrences + ' 处');
      if (extra.segments) extraBits.push(extra.segments + ' 段');
      if (extra.intervals) extraBits.push(extra.intervals + ' 区间');
      if (extra.del_speakers_count) extraBits.push('删除 ' + extra.del_speakers_count + ' 说话人');
      if (extra.speakers) extraBits.push(extra.speakers + ' 位说话人');
      if (extra.output_mb) extraBits.push(extra.output_mb + ' MB');
      var extraStr = extraBits.length ? ' · ' + extraBits.join(' / ') : '';
      var errHtml = it.error
        ? '<div class="slirn-exec-err">' + escapeHtml(String(it.error).slice(0, 300)) + '</div>'
        : '';
      html += '<div class="slirn-exec-row" data-status="' + escapeHtml(status) + '">'
        + '<span class="slirn-exec-idx">#' + escapeHtml(String(it.id || '').slice(-8)) + '</span>'
        + '<span class="slirn-exec-kind"><strong>' + escapeHtml(kindLabel) + '</strong>'
        + ' · ' + escapeHtml(startIso) + ' → ' + escapeHtml(endIso) + '</span>'
        + '<span class="slirn-exec-dur">' + escapeHtml(duration) + '</span> '
        + '<span class="' + badgeCls + '">' + badgeText + extraStr + '</span>'
        + errHtml
        + '</div>';
    });
    list.innerHTML = html;
  }

  // REQ-20261022-145：点击下拉外部或按 Esc → 关闭帮助菜单
  document.addEventListener('click', function(e) {
    var anyOpen = SLIRN_TOP_MENUS.some(function (id) {
      var d = document.getElementById(id);
      return d && !d.hasAttribute('hidden');
    });
    if (!anyOpen) return;
    var inMenu = e.target.closest
      ? e.target.closest('.slirn-topbar-menu')
      : null;
    if (!inMenu) _closeTopMenus();
  });
  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') _closeTopMenus();
  });

  // chip 点击：toggle .active 类 + 自动重查
  // REQ-20260920-084：data-log-auto / data-log-time chip 走单选（互斥），其他走多选
  // REQ-20260920-086：过滤变化时重置 page = 1；翻页按钮 click handler
  document.addEventListener('click', function(e) {
    var t = e.target;
    if (!t || !t.classList) return;
    if (t.classList.contains('slirn-chip') && (t.hasAttribute('data-log-kind') || t.hasAttribute('data-log-status'))) {
      t.classList.toggle('active');
      logsState.page = 1;  // REQ-086：过滤变化重置 page
      loadLogs();
      return;
    }
    // REQ-20260920-084：auto chip（单选）
    if (t.classList.contains('slirn-chip') && t.hasAttribute('data-log-auto')) {
      var boxA = document.getElementById('slirn-wb-pane-logs');
      if (!boxA) return;
      boxA.querySelectorAll('.slirn-chip[data-log-auto].active').forEach(function(b) {
        if (b !== t) b.classList.remove('active');
      });
      t.classList.toggle('active');
      logsState.page = 1;  // REQ-086
      loadLogs();
      return;
    }
    // REQ-20260920-084：time chip（单选）
    if (t.classList.contains('slirn-chip') && t.hasAttribute('data-log-time')) {
      var boxT = document.getElementById('slirn-wb-pane-logs');
      if (!boxT) return;
      boxT.querySelectorAll('.slirn-chip[data-log-time].active').forEach(function(b) {
        if (b !== t) b.classList.remove('active');
      });
      t.classList.toggle('active');
      logsState.page = 1;  // REQ-086
      loadLogs();
      return;
    }
    if (t.getAttribute && t.getAttribute('data-action') === 'logs-refresh') {
      e.preventDefault();
      loadLogs();
      return;
    }
    if (t.getAttribute && t.getAttribute('data-action') === 'logs-clear-kinds') {
      e.preventDefault();
      var box = document.getElementById('slirn-wb-pane-logs');
      if (!box) return;
      box.querySelectorAll('.slirn-chip[data-log-kind].active').forEach(function(b) { b.classList.remove('active'); });
      logsState.page = 1;  // REQ-086
      loadLogs();
      return;
    }
    if (t.getAttribute && t.getAttribute('data-action') === 'logs-clear-statuses') {
      e.preventDefault();
      var box = document.getElementById('slirn-wb-pane-logs');
      if (!box) return;
      box.querySelectorAll('.slirn-chip[data-log-status].active').forEach(function(b) { b.classList.remove('active'); });
      logsState.page = 1;  // REQ-086
      loadLogs();
      return;
    }
    // REQ-20260920-086：分页按钮 click（首页/上一页/下一页/末页）
    if (t.classList && t.classList.contains('slirn-pager-btn')) {
      var act = t.getAttribute('data-pager');
      var pager = document.getElementById('slirn-logs-pager');
      if (!pager || !act) return;
      if (t.disabled) return;  // disabled 按钮不响应
      var curPage = parseInt(pager.getAttribute('data-page') || '1', 10);
      var tp = parseInt(pager.querySelector('[data-bind="total-pages"]').textContent || '1', 10);
      if (act === 'first') logsState.page = 1;
      else if (act === 'prev') logsState.page = Math.max(1, curPage - 1);
      else if (act === 'next') logsState.page = Math.min(tp, curPage + 1);
      else if (act === 'last') logsState.page = Math.max(1, tp);
      loadLogs();
      return;
    }
  });

  // REQ-20260920-086：每页大小下拉 change 事件
  document.addEventListener('change', function(e) {
    var t = e.target;
    if (t && t.classList && t.classList.contains('slirn-pager-size')) {
      logsState.pageSize = parseInt(t.value || '20', 10);
      logsState.page = 1;  // 改 pageSize 重置 page
      loadLogs();
    }
  });

  // 关键词输入：300ms 防抖自动重查
  var _logsKwTimer = null;
  document.addEventListener('input', function(e) {
    var t = e.target;
    if (!t || t.id !== 'slirn-logs-keyword') return;
    if (_logsKwTimer) clearTimeout(_logsKwTimer);
    _logsKwTimer = setTimeout(function() {
      loadLogs();
    }, 300);
  });

  var subPollTimer = null;
  function startSubPolling(tid) {
    if (subPollTimer) { clearInterval(subPollTimer); subPollTimer = null; }
    var update = function() {
      postJSON(SLIRN_API + '/subtitle_status', {task_id: tid}).then(function(r) {
        if (!r || !r.ok) return;
        var j = r.job || {};
        var el = document.getElementById('slirn-asr-status');
        if (j.state === 'running') {
          if (el) {
            el.style.display = '';
            el.dataset.state = 'running';
            el.innerHTML = '⏳ ' + escapeHtml(j.stage || '处理中') + ' · 已耗时 ' + fmtElapsed(j.elapsed_s || 0);
          }
        } else {
          if (subPollTimer) { clearInterval(subPollTimer); subPollTimer = null; }
          if (j.state === 'done') {
            toast('✅ 字幕生成完成：' + (j.segments_count || 0) + ' 段');
            // 字幕完成时用户可能在工作台或详情页 — 刷新所在视图
            if (el && el.closest && el.closest('#slirn-tab-workbench')) { openWorkbench(tid); }
            else { refreshDetail(tid); }
          } else if (j.state === 'error') {
            if (el) {
              el.style.display = '';
              el.dataset.state = 'error';
              el.innerHTML = '❌ ' + escapeHtml(j.error || '生成失败');
            }
            toast('❌ 字幕生成失败', 'error');
          }
        }
      });
    };
    update();
    subPollTimer = setInterval(update, 2000);
  }

  // ===== 功能区折叠（REQ-20260918-044）=====
  // 各阶段顶部的说明/统计/快捷键/筛选/跳转/批量/搜索条：外包一层可折叠壳，
  // 点胶囊头收起只留一行、再点展开 — 用户按需留出空间、聚焦当前要做的事。
  // 折叠状态记在本机（按区独立记忆）。MutationObserver 兜底：面板（服务端
  // innerHTML）或条内容（JS 重渲染）更新后自动补壳，条内重渲染不破坏壳。
  function colState() {
    try { return JSON.parse(localStorage.getItem('slirnCols') || '{}'); } catch (err) { return {}; }
  }
  function colSet(key, off) {
    if (!key) return;
    var s = colState();
    if (off) s[key] = 1; else delete s[key];
    try { localStorage.setItem('slirnCols', JSON.stringify(s)); } catch (err) {}
  }
  function colWrap(bar, key, title) {
    if (!bar || bar.dataset.slirnCol !== undefined) return;
    bar.dataset.slirnCol = '1';
    var off = !!colState()[key];
    var wrap = document.createElement('div');
    wrap.className = 'slirn-col' + (off ? ' off' : '');
    wrap.setAttribute('data-col-key', key);
    var head = document.createElement('div');
    head.className = 'slirn-col-head';
    head.innerHTML = '<button type="button" class="slirn-col-btn" data-action="col-toggle"><span class="slirn-col-chev">'
      + (off ? '▸' : '▾') + '</span>' + title + '</button>';
    bar.parentNode.insertBefore(wrap, bar);
    wrap.appendChild(head);
    wrap.appendChild(bar);
  }
  function colPaneKey(bar, suffix) {
    var pane = bar.closest ? bar.closest('.slirn-wb-pane') : null;
    return ((pane && pane.id ? pane.id.replace('slirn-wb-pane-', '') : 'x') + ':' + suffix);
  }
  function colEnhance(root) {
    var scope = root || document;
    // 2026-09-29 自愈：内容条已被移除的「空壳」（动态 hint 删后残留的折叠壳）
    // 直接清掉 — 否则插删一次 hint 就多一个只有胶囊头的无用说明区（用户反馈：
    // 每次保存后堆出一列「说明」）。正常壳必有 head + ≥1 个内容元素。
    Array.prototype.forEach.call(scope.querySelectorAll('.slirn-col'), function(c) {
      var hasContent = false;
      for (var i = 0; i < c.children.length; i++) {
        if (!c.children[i].classList.contains('slirn-col-head')) { hasContent = true; break; }
      }
      if (!hasContent && c.parentNode) c.parentNode.removeChild(c);
    });
    // REQ-20260918-059：同一 pane 内所有 .slirn-form-hint 合并成单个折叠（不管相邻不相邻）
    // 旧实现只合并相邻 hint；被 opt-words / opt-list 等元素隔开时会各自成组，撑高页面。
    var paneMap = Object.create(null);
    Array.prototype.forEach.call(scope.querySelectorAll('.slirn-wb-pane .slirn-form-hint'), function(h) {
      if (h.dataset.slirnCol !== undefined) return;
      var pane = (h.closest && h.closest('.slirn-wb-pane')) || null;
      var pid = pane && pane.id ? pane.id : 'x';
      (paneMap[pid] = paneMap[pid] || []).push(h);
    });
    Object.keys(paneMap).forEach(function(pid) {
      var group = paneMap[pid];
      if (!group.length) return;
      var first = group[0];
      colWrap(first, colPaneKey(first, 'hint'), 'ℹ️ 说明');
      var wrap = first.parentNode;  // colWrap 已把 first 挪进 .slirn-col
      for (var i = 1; i < group.length; i++) {
        group[i].dataset.slirnCol = '1';
        wrap.appendChild(group[i]);
      }
    });
    Array.prototype.forEach.call(scope.querySelectorAll('.slirn-wb-pane .slirn-sub-meta'), function(m) {
      colWrap(m, colPaneKey(m, 'meta'), '📊 统计');
    });
    Array.prototype.forEach.call(scope.querySelectorAll('.slirn-wb-pane .slirn-rev-kbhint'), function(k) {
      colWrap(k, colPaneKey(k, 'kb'), '⌨ 快捷键');
    });
    var singles = [
      ['#slirn-rev-filter',       'rev:filter',  '🔎 筛选 / 跳转'],
      ['#slirn-rev-batch',        'rev:batch',   '🧮 批量修改'],
      ['#slirn-rev-search',       'rev:search',  '🔎 搜索定位'],
      ['#slirn-cut-spk-bar',      'cut:spk',     '👥 人员统计 / 查找'],
      ['#slirn-cut-batch',        'cut:batch',   '🧮 批量修改'],
      ['#slirn-cut-search',       'cut:search',  '🔎 搜索定位'],
      ['#slirn-opt-words',        'opt:words',   '🔤 词频列表'],
      ['.slirn-opt-word-filters', 'opt:wfilter', '🎚 词频筛选']
    ];
    singles.forEach(function(z) {
      Array.prototype.forEach.call(scope.querySelectorAll(z[0]), function(bar) {
        colWrap(bar, z[1], z[2]);
      });
    });
  }
  var colObs = new MutationObserver(function() {
    clearTimeout(colObs._t);
    colObs._t = setTimeout(function() { colEnhance(document); }, 60);  // 去抖：一次渲染多次 DOM 变更
  });
  colObs.observe(document.body, { childList: true, subtree: true });
  colEnhance(document);

  // ===== 视频浮层（REQ-20260918-043）=====
  // 无论哪个阶段：视频一开始播放就挪进可拖动的悬浮层 — 页面不再因插入
  // 播放器而整体位移，编辑信息不被顶开。✕ 只是收起（暂停），再次播放
  // 自动弹回；拖动位置记在本机。详情页/弹窗里的成片预览不搬（观看场景，
  // 不挤编辑区）。
  var vfLayer = null;
  function vfPlace(layer, x, y) {
    var w = layer.offsetWidth || 440;
    var minX = -(w - 76), maxX = window.innerWidth - 76;  // 标题条至少留 76px 可抓回
    var minY = 0, maxY = window.innerHeight - 44;
    layer.style.left = Math.round(Math.min(maxX, Math.max(minX, x))) + 'px';
    layer.style.top = Math.round(Math.min(maxY, Math.max(minY, y))) + 'px';
  }
  function vfPlaceSaved(layer) {
    var pos = null;
    try { pos = JSON.parse(localStorage.getItem('slirnVfPos') || 'null'); } catch (err) {}
    if (pos && typeof pos.l === 'number' && typeof pos.t === 'number') {
      vfPlace(layer, pos.l, pos.t);
    } else {
      vfPlace(layer, window.innerWidth - 464, 24);  // 默认右上角
    }
  }
  function vfDragBind(layer) {
    var bar = layer.querySelector('.slirn-vf-bar');
    if (!bar) return;
    bar.addEventListener('pointerdown', function(ev) {
      if (ev.target.closest('.slirn-vf-close')) return;  // 关闭按钮不触发拖动
      var r = layer.getBoundingClientRect();
      var ox = ev.clientX - r.left, oy = ev.clientY - r.top;
      var move = function(e2) { vfPlace(layer, e2.clientX - ox, e2.clientY - oy); };
      var up = function() {
        document.removeEventListener('pointermove', move);
        document.removeEventListener('pointerup', up);
        try {
          localStorage.setItem('slirnVfPos',
            JSON.stringify({ l: layer.offsetLeft, t: layer.offsetTop }));
        } catch (err) {}
      };
      document.addEventListener('pointermove', move);
      document.addEventListener('pointerup', up);
      ev.preventDefault();
    });
    // REQ-20260919-061a v7 用户反馈：浮动视频窗口可调整大小 — 用 ResizeObserver
    // 监听用户拖右下角 resize handle 改变尺寸，存到 localStorage 下次恢复。
    if (typeof ResizeObserver !== 'undefined') {
      var ro = new ResizeObserver(function(entries) {
        for (var i = 0; i < entries.length; i++) {
          var cr = entries[i].contentRect;
          try {
            var saved = JSON.parse(localStorage.getItem('slirnVfSize') || 'null') || {};
            saved.w = Math.round(cr.width);
            saved.h = Math.round(cr.height);
            localStorage.setItem('slirnVfSize', JSON.stringify(saved));
          } catch (err) {}
        }
      });
      ro.observe(layer);
    }
  }
  function vfRestoreSize(layer) {
    try {
      var s = JSON.parse(localStorage.getItem('slirnVfSize') || 'null');
      if (s && s.w > 0 && s.h > 0) {
        layer.style.width = s.w + 'px';
        layer.style.height = s.h + 'px';
      }
    } catch (err) {}
  }
  function vfEnsureLayer() {
    if (vfLayer && vfLayer.isConnected) return vfLayer;
    vfLayer = document.createElement('div');
    vfLayer.id = 'slirn-video-float';
    vfLayer.className = 'slirn-video-float';
    vfLayer.hidden = true;
    vfLayer.innerHTML = '<div class="slirn-vf-bar">'
      + '<span class="slirn-vf-grip">⠿</span><span class="slirn-vf-title">▶ 视频预览（可拖动）</span>'
      + '<button type="button" class="slirn-vf-close" data-action="vf-close" '
      + 'title="收起浮层（视频暂停；再次播放自动弹回）">✕</button></div>'
      + '<div class="slirn-vf-body"></div>';
    document.body.appendChild(vfLayer);
    vfDragBind(vfLayer);
    return vfLayer;
  }
  function vfShow(wrap) {
    if (!wrap) return;
    var layer = vfEnsureLayer();
    var body = layer.querySelector('.slirn-vf-body');
    if (wrap.parentNode !== body) {
      // 浮层里只留当前播放器：面板重渲染后的旧节点直接清掉
      Array.prototype.forEach.call(body.children, function(el) { if (el !== wrap) el.remove(); });
      body.appendChild(wrap);
    }
    wrap.style.display = '';
    layer.hidden = false;
    vfRestoreSize(layer);  // 恢复上次保存的尺寸（用户拖右下角 resize 调整过的）
    vfPlaceSaved(layer);  // 记忆位置；隐藏期间视口可能变化 → 重新夹取进屏
  }
  // 兜底：不走 playXxx 助手的视频（如粗剪成片预览）一播放也进浮层
  document.addEventListener('play', function(ev) {
    var v = ev.target;
    if (!v || v.tagName !== 'VIDEO' || !v.closest) return;
    if (!v.closest('.slirn-wb-pane')) return;  // 仅工作台阶段；详情页/弹窗预览原地不动
    var wrap = v.closest('.slirn-video-wrap');
    if (!wrap) {  // 无包裹 → 就地包一层再搬，原位不留洞
      wrap = document.createElement('div');
      wrap.className = 'slirn-video-wrap';
      v.parentNode.insertBefore(wrap, v);
      wrap.appendChild(v);
      bindSpeedControl(v);
    }
    vfShow(wrap);
  }, true);

  // ===== 字幕播放器：定位播放 + 播放高亮跟随 =====
  function playSubAt(tid, startMs) {
    var wrap = document.getElementById('slirn-sub-player-wrap');
    var v = document.getElementById('slirn-sub-player');
    if (!v) { toast('❌ 播放器未就绪', 'error'); return; }
    if (wrap) vfShow(wrap);
    if (wrap) wrap.style.display = '';
    if (!v.src) { v.src = SLIRN_API + '/video/' + encodeURIComponent(tid); v.load(); }
    var go = function() {
      try { v.currentTime = (startMs || 0) / 1000; } catch (err) {}
      var p = v.play();
      if (p && p.catch) p.catch(function() {});
    };
    if (v.readyState >= 1) go();
    else v.addEventListener('loadedmetadata', go, {once: true});
  }

  // ===== 倍速显示与控制（REQ-20260916-014）=====
  // 背景：全部代码无任何 playbackRate 写入（grep 证实），神秘变速来自 Chrome 原生视频
  // 菜单（右键/⋮「播放速度」）等外部途径——页面上原本无任何倍速显示，所以"不知道为
  // 什么变快变慢"。这里把真实速率显示出来并可调大/调小/一键回 1x；ratechange 监听
  // 捕获任何来源的变速——速率一变必可见，不再是黑盒。
  var SLIRN_RATE_STEPS = [0.5, 0.75, 1, 1.25, 1.5, 1.75, 2, 2.5, 3];
  var slirnRate = 1;  // 会话级倍速：播放器元素随面板重渲染重建（重建后浏览器重置 1x），重绑时重新应用
  function slirnRateLabel(r) { return parseFloat((+r || 1).toFixed(2)) + 'x'; }
  function bindSpeedControl(v) {
    if (!v) return;
    var wrap = (v.closest ? v.closest('.slirn-video-wrap') : null) || v.parentNode;
    if (!wrap || !wrap.querySelector) return;
    var ctl = wrap.querySelector('.slirn-speed');
    if (!ctl) {
      ctl = document.createElement('div');
      ctl.className = 'slirn-speed';
      ctl.innerHTML = '<span class="slirn-speed-cap">倍速</span>'
        + '<button type="button" class="slirn-btn slirn-btn-xs" data-rate-act="down" title="调慢（最低 0.5x）">−</button>'
        + '<button type="button" class="slirn-btn slirn-btn-xs slirn-speed-cur" data-rate-act="reset" title="点击恢复 1x">1x</button>'
        + '<button type="button" class="slirn-btn slirn-btn-xs" data-rate-act="up" title="调快（最高 3x）">＋</button>';
      wrap.appendChild(ctl);
      ctl.addEventListener('click', function(e) {
        var b = e.target.closest ? e.target.closest('[data-rate-act]') : null;
        if (!b) return;
        e.preventDefault();
        var r = +v.playbackRate || 1;
        var act = b.getAttribute('data-rate-act');
        if (act === 'reset') {
          r = 1;  // 调回原来的值 = 正常速度
        } else {
          // 阶梯调大/调小：先找当前速率最近的档位，再上下移动一格
          var near = 0;
          for (var i = 1; i < SLIRN_RATE_STEPS.length; i++) {
            if (Math.abs(SLIRN_RATE_STEPS[i] - r) < Math.abs(SLIRN_RATE_STEPS[near] - r)) near = i;
          }
          var nxt = Math.max(0, Math.min(SLIRN_RATE_STEPS.length - 1, near + (act === 'up' ? 1 : -1)));
          r = SLIRN_RATE_STEPS[nxt];
        }
        v.playbackRate = r;  // 触发 ratechange → 下方监听统一同步显示
      });
    }
    if (!ctl.dataset.ratebound) {
      ctl.dataset.ratebound = '1';
      // 任何来源的变速（本控件 / Chrome 原生菜单 / 扩展）→ 显示同步为真实速率
      v.addEventListener('ratechange', function() {
        slirnRate = +v.playbackRate || 1;
        var cur = ctl.querySelector('[data-rate-act="reset"]');
        if (cur) cur.textContent = slirnRateLabel(slirnRate);
      });
    }
    // 元素重渲染后浏览器重置为 1x → 重绑时把会话倍速应用回去（同值赋值不触发 ratechange，手动刷一次显示）
    try { v.playbackRate = slirnRate; } catch (err) {}
    var cur0 = ctl.querySelector('[data-rate-act="reset"]');
    if (cur0) cur0.textContent = slirnRateLabel(v.playbackRate);
  }

  // ===== SD 开关状态（REQ-20260917-029）：localStorage 记忆，重渲染/换视图后恢复 =====
  // 服务端默认渲染选中；详情页与工作台各一份同 id 复选框，apply 时同步全部实例
  function applySdSwitchState() {
    var saved = null;
    try { saved = localStorage.getItem('slirnSdSwitch'); } catch (e) {}
    if (saved === null) return;  // 用户没选过 → 保持服务端默认（开）
    var boxes = document.querySelectorAll('#slirn-sd-switch');
    for (var i = 0; i < boxes.length; i++) boxes[i].checked = saved === '1';
  }
  if (!window.__slirnSdBound) {
    window.__slirnSdBound = true;
    document.addEventListener('change', function(e) {
      var t = e.target;
      if (t && t.id === 'slirn-sd-switch') {
        try { localStorage.setItem('slirnSdSwitch', t.checked ? '1' : '0'); } catch (err) {}
      }
    });
  }

  function bindSubPlayer() {
    var v = document.getElementById('slirn-sub-player');
    var list = document.getElementById('slirn-sub-list');
    bindSpeedControl(v);  // 倍速显示与控制（REQ-20260916-014）
    if (v && list && !v.dataset.bound) {
      v.dataset.bound = '1';
      var rows = Array.prototype.slice.call(list.querySelectorAll('.slirn-sub-row'));
      var lastHit = -1;
      var setActive = function(idx) {
        for (var i = 0; i < rows.length; i++) rows[i].classList.toggle('active', i === idx);
        if (idx >= 0 && rows[idx] && rows[idx].scrollIntoView) rows[idx].scrollIntoView({block: 'nearest'});
      };
      v.addEventListener('timeupdate', function() {
        var tms = v.currentTime * 1000, hit = -1;
        for (var i = 0; i < rows.length; i++) {
          var s0 = parseInt(rows[i].getAttribute('data-start-ms'), 10) || 0;
          var e0 = parseInt(rows[i].getAttribute('data-end-ms'), 10) || 0;
          if (tms >= s0 && tms < e0) { hit = i; break; }
          if (s0 > tms) break;  // 行按时间有序，后面不可能命中
        }
        if (hit === -1 && lastHit >= 0) {
          // 两个字幕段之间的静音间隙：保持上一行高亮（校对视角更连续）
          var eh = parseInt(rows[lastHit].getAttribute('data-end-ms'), 10) || 0;
          var nh = (lastHit + 1 < rows.length)
            ? (parseInt(rows[lastHit + 1].getAttribute('data-start-ms'), 10) || 0)
            : Infinity;
          if (tms >= eh && tms < nh) hit = lastHit;
        }
        lastHit = hit;
        setActive(hit);
      });
    }
    // 详情（重新）打开时，若 job 还在跑 → 恢复轮询
    var st = document.getElementById('slirn-asr-status');
    if (st && st.dataset.taskId && st.dataset.state === 'running') startSubPolling(st.dataset.taskId);
    applySdSwitchState();  // SD 开关：重渲染后恢复上次选择（REQ-20260917-029）
  }

  // ===== 字幕修订：轮询 + 播放器（REQ-20260915-005，与字幕区同模式、独立 id）=====
  var revPollTimer = null;
  function startRevPolling(tid) {
    if (revPollTimer) { clearInterval(revPollTimer); revPollTimer = null; }
    var update = function() {
      postJSON(SLIRN_API + '/revise_status', {task_id: tid}).then(function(r) {
        if (!r || !r.ok) return;
        var j = r.job || {};
        var el = document.getElementById('slirn-rev-status');
        if (j.state === 'running') {
          if (el) {
            el.style.display = '';
            el.dataset.state = 'running';
            el.innerHTML = '⏳ ' + escapeHtml(j.stage || '分析中') + ' · 已耗时 ' + fmtElapsed(j.elapsed_s || 0);
          }
        } else {
          if (revPollTimer) { clearInterval(revPollTimer); revPollTimer = null; }
          if (j.state === 'done') {
            toast('✅ 大模型分析完成：' + (j.entries_count || 0) + ' 条建议');
            openWorkbench(tid);  // 刷新面板（建议列表 + 阶段态）
          } else if (j.state === 'error') {
            if (el) {
              el.style.display = '';
              el.dataset.state = 'error';
              el.innerHTML = '❌ ' + escapeHtml(j.error || '分析失败');
            }
            toast('❌ 大模型分析失败', 'error');
          }
        }
      });
    };
    update();
    revPollTimer = setInterval(update, 2000);
  }

  // 详情页与工作台可能同时持有修订区 DOM（隐藏 tab 不清空 innerHTML）→ 一律取
  // 「可见的」那个元素，避免 id 撞车时操作到隐藏播放器/列表（REQ-20260916-004 顺带修复）
  function revVis(id) {
    var els = document.querySelectorAll('#' + id);
    for (var i = 0; i < els.length; i++) { if (els[i].offsetParent) return els[i]; }
    return els[0] || null;
  }

  function playRevAt(tid, startMs) {
    var wrap = revVis('slirn-rev-player-wrap');
    var v = revVis('slirn-rev-player');
    if (!v) { toast('❌ 播放器未就绪', 'error'); return; }
    if (wrap) vfShow(wrap);
    if (!v.src) { v.src = SLIRN_API + '/video/' + encodeURIComponent(tid); v.load(); }
    var go = function() {
      try { v.currentTime = (startMs || 0) / 1000; } catch (err) {}
      var p = v.play();
      if (p && p.catch) p.catch(function() {});
    };
    if (v.readyState >= 1) go();
    else v.addEventListener('loadedmetadata', go, {once: true});
  }

  // 修订行连续跳播（REQ-20260917-028）：字幕之间常有无声/停顿空白，逐条连播时
  // 空白也一并播出来。点行后从该行起链式播放：本条到尾 → 直接跳到下一条起点
  // （段间空白不播），播完最后一条自动停。链随 timeupdate 维护（见 bindRevPlayer），
  // 用户回拖/前拖按时间自动重定位。
  var revPlaySeq = null, revPlayIdx = 0;
  function revPlayFrom(row) {
    if (!row) return false;
    var seq = [], started = false;
    revRows().forEach(function(r) {  // 全量行（时间序）：链按字幕轨推进，与筛选无关
      if (r === row) started = true;
      if (!started) return;
      var s = parseInt(r.getAttribute('data-start-ms'), 10) || 0;
      var e = parseInt(r.getAttribute('data-end-ms'), 10) || 0;
      if (e > s) seq.push({ s: s, e: e });
    });
    revPlaySeq = seq.length ? seq : null;
    revPlayIdx = 0;
    playRevAt(row.getAttribute('data-task-id') || '',
              revPlaySeq ? revPlaySeq[0].s : (parseInt(row.getAttribute('data-start-ms'), 10) || 0));
    return true;
  }

  // 切分修剪行定位播放（REQ-20260916-008）— 与修订行同模式，独立播放器防 id 撞车
  function playCutAt(tid, startMs) {
    var wrap = revVis('slirn-cut-player-wrap');
    var v = revVis('slirn-cut-player');
    if (!v) { toast('❌ 播放器未就绪', 'error'); return; }
    if (wrap) vfShow(wrap);
    if (!v.src) { v.src = SLIRN_API + '/video/' + encodeURIComponent(tid); v.load(); }
    var goCut = function() {
      try { v.currentTime = (startMs || 0) / 1000; } catch (err) {}
      var p = v.play();
      if (p && p.catch) p.catch(function() {});
    };
    if (v.readyState >= 1) goCut();
    else v.addEventListener('loadedmetadata', goCut, {once: true});
  }

  // ===== 切分修剪预览与决策（REQ-20260916-011 → 013 连续播放）=====
  // 行点击/↑↓ = 从该行起点连续播放（REQ-20260916-013：播完一条自动接下一条，
  // 不再段尾自停），播放中 active 高亮跟随（与字幕/修订阶段同款）；
  // 组头 ▶ 试听 = 本组 keep 子段成片口径跳播。
  // 快捷键与字幕修订阶段同键位（可自定义）：↑↓ 切换 · 空格 播/停 · R 重播 ·
  // K/D/S 对选中行所属字幕整条改判（保留/删除/切分，再按同键取消）。
  // 翻转/改判未保存纯前端，「💾 保存切分决策」落盘。
  var cutKeepSeq = null; // 试听跳播序列 [{s,e,row},…]（null = 不跳播）
  var cutKeepIdx = 0;    // 试听当前段下标（索引跟踪：只前进不回扫 — REQ-20260916-012）
  var cutKeepMode = null; // 跳播来源：'group' 组头试听（显示成片试听条）| 'row' 行级连续播放（REQ-20260916-015）
  // 执行口径的行保留判定（与 cutlist_service.effective_keep_units 对齐 — REQ-20260916-015）：
  // 组级改判 delete → 整条剔除；切分组子段按 mark（组改判 keep → 子段划分作废、整段保留）；
  // 整段组默认保留（改判 split 未重切前维持整段）。试听/连续播放只播保留内容 = 成片效果。
  function cutRowKept(row) {
    var g = row.closest('.slirn-cut-group');
    var act = g ? (g.getAttribute('data-act') || '') : '';
    if (act === 'delete') return false;
    if (row.classList.contains('sub')) {
      if (act === 'keep') return true;
      return (row.getAttribute('data-mark') || 'keep') === 'keep';
    }
    return true;
  }
  // 从 startRow 起到列表末尾的全部保留区间（时间序）— 行级连续播放的跳播序列：
  // 删除洞（子段 mark=delete）与改判删除的整条直接跳过不播
  function cutKeepAllFrom(startRow) {
    var seq = [], started = false;
    cutRows().forEach(function(r) {
      if (r === startRow) started = true;
      if (!started || !cutRowKept(r)) return;
      var s = parseInt(r.getAttribute('data-start-ms'), 10) || 0;
      var e = parseInt(r.getAttribute('data-end-ms'), 10) || 0;
      if (e > s) seq.push({ s: s, e: e, row: r });
    });
    return seq;
  }
  function cutRows() {
    var list = revVis('slirn-cut-list');
    if (!list || !list.offsetParent) return [];  // 面板不可见 → 快捷键整体不生效
    return Array.prototype.slice.call(list.querySelectorAll('.slirn-cut-row'));
  }
  function cutSelIndex(rows) {
    for (var i = 0; i < rows.length; i++) { if (rows[i].classList.contains('kbsel')) return i; }
    return -1;
  }
  function cutMarkSel(row) {
    cutRows().forEach(function(r) { r.classList.toggle('kbsel', r === row); });
    if (row && row.scrollIntoView) row.scrollIntoView({block: 'nearest'});
  }
  function cutSelectRow(idx, seek) {
    var rows = cutRows();
    if (!rows.length) return null;
    var cur = cutSelIndex(rows);
    if (cur < 0) idx = (idx < 0) ? rows.length - 1 : 0;  // 无选中：↓ 第一行，↑ 最后一行
    idx = Math.max(0, Math.min(rows.length - 1, idx));
    var row = rows[idx];
    cutMarkSel(row);
    if (seek) cutPreview(row);
    return row;
  }
  // 行级连续播放（REQ-20260916-013 起，REQ-20260916-015 改为保留内容跳播）：从本行起
  // 按执行口径连续播保留区间 — 播完一条 seek 下一条，删除洞/改判删除整条直接跳过不播
  // （= 成片效果），active 高亮随 timeupdate 跟随切换。点击的行若本身是删除内容，
  // 从其后的下一个保留区间播起。
  function cutPreview(row) {
    if (!row) return;
    cutAuditionBar(null);
    var tid = row.getAttribute('data-task-id') || '';
    var seq = cutKeepAllFrom(row);
    if (!seq.length) {
      // 本行起再无保留内容：不播（REQ-20260922-NNN）— 原行为是退化为定位裸播，
      // 会把其后已删除整条、列表外无字幕片段原样播出去，与「只播列表内保留
      // 内容」的执行口径矛盾（点中尾部已删除区域时用户看到的就是这个 bug）
      cutKeepSeq = null; cutKeepMode = null;
      cutAuditionBar(null);
      toast('这一条起没有保留内容（其后均已删除）— 不播放');
      return;
    }
    cutKeepSeq = seq;
    cutKeepIdx = 0;
    cutKeepMode = 'row';
    cutMarkSel(row);  // 选中停在点到的行（播放起点可能是其后的保留区间）— 键盘改判目标可预期
    playCutAt(tid, seq[0].s);
  }
  function cutReplayRow() {
    var rows = cutRows();
    var row = rows[cutSelIndex(rows)];
    if (!row) {
      var kmR = revKeysLoad();
      toast('⌨ 先用 ' + revKeyLabel(kmR.prev) + ' / ' + revKeyLabel(kmR.next) + ' 选择一个子段', 'error');
      return;
    }
    cutPreview(row);
  }
  function cutTogglePlay() {
    var v = revVis('slirn-cut-player');
    if (!v) return;
    if (!v.src) {  // 从未播放过：从选中行（或第一行）起点开播
      var rows = cutRows();
      cutPreview(rows[cutSelIndex(rows)] || rows[0]);
      return;
    }
    if (v.paused) {
      // 恢复播放也绝不裸播（REQ-20260922-NNN）：跳播序列没了（自动播完停住/
      // 组试听暂停退出）时重建全量保留序列，从当前位置所在（或其后第一个）
      // 保留段继续 — 裸播恢复会把已删除整条与列表外内容原样播出去
      if (!cutKeepSeq) {
        var tmsR = v.currentTime * 1000;
        var seqAll = cutKeepAllFrom(cutRows()[0]);
        if (!seqAll.length || tmsR >= seqAll[seqAll.length - 1].e) {
          var kmT = revKeysLoad();
          toast('后面没有保留内容了 — 用 ' + revKeyLabel(kmT.prev) + ' / '
            + revKeyLabel(kmT.next) + ' 选择一条再播');
          return;
        }
        cutKeepSeq = seqAll;
        cutKeepIdx = 0;
        while (cutKeepIdx < cutKeepSeq.length - 1 && tmsR >= cutKeepSeq[cutKeepIdx].e - 30) cutKeepIdx++;
        var segR = cutKeepSeq[cutKeepIdx];
        if (tmsR < segR.s - 500) { try { v.currentTime = segR.s / 1000; } catch (err) {} }
        cutKeepMode = 'row';
      }
      var p = v.play(); if (p && p.catch) p.catch(function() {}); }
    else {
      v.pause();
      if (cutKeepMode === 'group') { cutKeepSeq = null; cutAuditionBar(null); }  // 组试听：手动暂停即退出（REQ-20260916-012）
      // 行级连续播放：暂停保留跳播序列 — 恢复播放后继续跳过删除内容（REQ-20260916-015）
    }
  }
  // 组头 ▶ 试听：本组「执行口径」保留内容连续跳播 — 播完一段自动 seek 下一段
  // （跳变即真实剪辑效果：删掉洞后 keep 段首尾紧贴）。REQ-20260916-015 对齐
  // effective_keep_units：改判删除 → 成片没有这段；改判保留 → 整段一段（子段作废）；
  // 其余按子段 mark=keep。
  function cutPlayGroupKeep(g) {
    if (!g) return;
    var tid = g.getAttribute('data-task-id') || '';
    var act = g.getAttribute('data-act') || '';
    if (act === 'delete') {
      toast('本组已改判删除 — 成片中没有这段内容', 'error');
      return;
    }
    var seq = [];
    var subs = Array.prototype.slice.call(g.querySelectorAll('.slirn-cut-row.sub'));
    if (subs.length && act !== 'keep') {
      subs.forEach(function(r) {
        if ((r.getAttribute('data-mark') || 'keep') === 'keep') {
          var s = parseInt(r.getAttribute('data-start-ms'), 10) || 0;
          var e = parseInt(r.getAttribute('data-end-ms'), 10) || 0;
          if (e > s) seq.push({ s: s, e: e, row: r });
        }
      });
    } else {
      // 整段保留：改判 keep 的切分组（子段划分作废）或整段组 — 首行起点到末行终点一段
      var rowsG = Array.prototype.slice.call(g.querySelectorAll('.slirn-cut-row'));
      if (rowsG.length) {
        var s0 = parseInt(rowsG[0].getAttribute('data-start-ms'), 10) || 0;
        var e0 = parseInt(rowsG[rowsG.length - 1].getAttribute('data-end-ms'), 10) || 0;
        if (e0 > s0) seq.push({ s: s0, e: e0, row: rowsG[0] });
      }
    }
    if (!seq.length) { toast('本组没有保留子段（全部为删除洞）', 'error'); return; }
    cutKeepSeq = seq;
    cutKeepIdx = 0;  // 从第一段开播，一次到底（REQ-20260916-012）
    cutKeepMode = 'group';
    playCutAt(tid, seq[0].s);
    cutMarkSel(seq[0].row);
    var v = revVis('slirn-cut-player');
    if (v) {
      var p = v.play(); if (p && p.catch) p.catch(function() {});
      cutAuditionBar(v);  // 试听条：成片口径连续时间戳
    }
  }
  // 试听条（REQ-20260916-012）：成片时间戳 = 当前段之前全部 keep 段累计时长 + 段内偏移。
  // 跳播时视频原始时间戳会跳变；这条时间轴连续不回退，与当前试听进度严格一致。
  function cutFmtMS(ms) {
    var s = Math.floor(ms / 1000);
    var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = s % 60;
    var two = function(n) { return (n < 10 ? '0' : '') + n; };
    return (h ? h + ':' + two(m) : m) + ':' + two(ss);
  }
  function cutAuditionBar(v) {
    var wrap = document.getElementById('slirn-cut-player-wrap');
    if (!wrap) return;
    var bar = wrap.querySelector('.slirn-cut-audition');
    if (!cutKeepSeq || !v) { if (bar) bar.hidden = true; return; }
    if (!bar) {
      bar = document.createElement('div');
      bar.className = 'slirn-cut-audition';
      wrap.appendChild(bar);
    }
    var total = 0, before = 0, i;
    for (i = 0; i < cutKeepSeq.length; i++) total += Math.max(0, cutKeepSeq[i].e - cutKeepSeq[i].s);
    for (i = 0; i < cutKeepIdx; i++) before += Math.max(0, cutKeepSeq[i].e - cutKeepSeq[i].s);
    var seg = cutKeepSeq[cutKeepIdx];
    var cur = before + Math.max(0, Math.min(v.currentTime * 1000, seg.e) - seg.s);
    bar.hidden = false;
    bar.textContent = '🎧 试听（成片口径）' + cutFmtMS(cur) + ' / ' + cutFmtMS(total)
      + ' · 保留段 ' + (cutKeepIdx + 1) + '/' + cutKeepSeq.length
      + ' · 对应视频 ' + cutFmtMS(v.currentTime * 1000);
  }
  // 翻转子段标记（自动保存落盘；data-mark 与 data-mark-init 供「有手工修改」判断）
  function cutFlipMark(row) {
    var nw = (row.getAttribute('data-mark') || 'keep') === 'keep' ? 'delete' : 'keep';
    row.setAttribute('data-mark', nw);
    row.classList.toggle('mark-delete', nw === 'delete');
    var el = row.querySelector('.slirn-cut-mark');
    if (el) el.textContent = nw === 'keep' ? '✅ 保留' : '❌ 删除';
    toast(nw === 'keep' ? '已翻转为 ✅ 保留' : '已翻转为 ❌ 删除');
    cutAutoSave();
  }
  // ===== 字幕级改判 K/D/S（REQ-20260916-011 M3）：作用于选中行所属的字幕（组）=====
  // 空=维持原状 · delete=整条删 · keep=原切分不切了整段保留 · split=改为切分（展开编辑区）
  // REQ-20260917-027：核心抽 cutApplyAct，组头/整段行的决策下拉（鼠标）与键盘同效
  function cutActBadge(g, val) {
    var b = g.querySelector('[data-actsel]');
    if (b) b.value = val || '';  // ''=维持原状
  }
  function cutApplyAct(g, val) {
    cutCloseResplit();  // 任何改判动作先收起重切编辑区（改判 split 时再重开 — 防取消后残留）
    if (!val) {  // 维持原状 → 取消改判
      g.removeAttribute('data-act');
      cutActBadge(g, '');
      toast('已取消改判（维持原状）');
    } else {
      g.setAttribute('data-act', val);
      cutActBadge(g, val);
      if (val === 'split') {
        cutOpenResplit(g);  // 切分需要内容：展开编辑区（预填原文/当前切分后文字）
        toast('✂️ 已改判切分 — 填写切分后内容后点「重新切分」');
      } else {
        toast((val === 'keep' ? '✅ 已改判整条保留' : '❌ 已改判整条删除')
          + '（子段标记不再参与执行）');
      }
    }
    cutAutoSave();
  }
  function cutApplyDecision(val) {
    var rows = cutRows();
    var row = rows[cutSelIndex(rows)];
    if (!row) {
      var kmA = revKeysLoad();
      toast('⌨ 先用 ' + revKeyLabel(kmA.prev) + ' / ' + revKeyLabel(kmA.next) + ' 选择一条字幕，再按 '
        + revKeyLabel(kmA.keep) + ' / ' + revKeyLabel(kmA.del) + ' / ' + revKeyLabel(kmA.split) + ' 改判', 'error');
      return;
    }
    var g = row.closest('.slirn-cut-group');
    if (!g) return;
    if ((g.getAttribute('data-act') || '') === val) {  // 再按同键 → 取消，回到维持原状
      cutApplyAct(g, '');
      return;
    }
    cutApplyAct(g, val);
  }
  // ===== 单段重新切分（REQ-20260916-011 M3）：组头下行内展开编辑区 =====
  function cutOpenResplit(g) {
    if (!g) return;
    cutCloseResplit();
    var target = g.getAttribute('data-target') || g.getAttribute('data-orig-text') || '';
    var si = g.getAttribute('data-source-i') || '?';
    var box = document.createElement('div');
    box.className = 'slirn-cut-resplit';
    box.innerHTML =
      '<div class="slirn-cut-resplit-label">✂️ 重新切分第 ' + si + ' 条 — 修改「切分后内容」'
      + '（多写少写都行，对不上的字自动落进删除洞）：</div>'
      + '<input class="slirn-cut-resplit-input" type="text">'
      + '<button class="slirn-btn slirn-btn-xs" data-cut-act="resplit-go">✂️ 按新内容重新切分</button>'
      + '<button class="slirn-btn slirn-btn-xs" data-cut-act="resplit-cancel">取消</button>'
      + '<span class="slirn-cut-resplit-hint">重切会重置本段的手工翻转标记；切分后内容将回写修订决策（本条 → 切分）</span>';
    var head = g.querySelector('.slirn-cut-ghead') || g.querySelector('.slirn-cut-row');
    if (!head) return;
    head.parentNode.insertBefore(box, head.nextSibling);
    var inp = box.querySelector('.slirn-cut-resplit-input');
    inp.value = target.trim();
    inp.focus();
    try { inp.setSelectionRange(inp.value.length, inp.value.length); } catch (err) {}
  }
  function cutCloseResplit() {
    var b = document.querySelector('.slirn-cut-resplit');
    if (b && b.parentNode) b.parentNode.removeChild(b);
  }
  function cutDoResplit(g) {
    var inp = document.querySelector('.slirn-cut-resplit-input');
    if (!g || !inp) return;
    var val = inp.value.trim();
    if (!val) { toast('❌ 请填写切分后内容', 'error'); inp.focus(); return; }
    var tid = g.getAttribute('data-task-id') || '';
    postJSON(SLIRN_API + '/resplit_segment', {
      task_id: tid,
      source_i: parseInt(g.getAttribute('data-source-i'), 10),
      target_text: val,
    }).then(function(r) {
      if (r && r.ok) { toast(r.toast || '已按新内容重新切分'); openWorkbench(tid); }
      else if (r && r.error) toast('❌ ' + r.error, 'error');
    });
  }
  function cutHasManual() {  // 有手工修改？（翻转过 / 有字幕级改判）→ 重新执行前 confirm
    var flipped = cutRows().some(function(r) {
      return (r.getAttribute('data-mark') || '') !== (r.getAttribute('data-mark-init') || '');
    });
    var acted = !!document.querySelector('#slirn-cut-list .slirn-cut-group[data-act]');
    return flipped || acted;
  }
  // ===== 关联人员ID（REQ-20260917-031 / 033）：subtitle 段级 spk 按时间重叠对齐到切分行 =====
  // REQ-033：关联状态落盘 speaker_link.json（端点写）；面板重进时服务端直接渲染徽章 +
  // 统计条（容器 data-linked=1）。统计口径 = 仅计未删除记录（删除状态完全不计入，
  // 用于确认非主讲人员已清除）；删除改判走既有组 act / 子段 mark 口径，保存才落盘。
  function cutSpkRows() {
    return cutRows().filter(function(r) { return !!r.getAttribute('data-spk'); });
  }
  function cutSpkSetMark(row, mk) {  // 设置子段去留（幂等、不 toast — 批量改判用）
    row.setAttribute('data-mark', mk);
    row.classList.toggle('mark-delete', mk === 'delete');
    var el = row.querySelector('.slirn-cut-mark');
    if (el) el.textContent = (mk === 'keep' ? '✅ 保留' : '❌ 删除')
      + ((el.textContent || '').indexOf('✏️') >= 0 ? ' ✏️' : '');
  }
  function cutSpkStats() {  // 按 DOM 现算 → [{spk, count}]（spk 升序；count=未删除行数）
    var m = {};
    cutSpkRows().forEach(function(r) {
      var s = r.getAttribute('data-spk');
      if (!s) return;
      if (!(s in m)) m[s] = 0;  // 有行即入表 — 全删光也显示 0 条（清零反馈）
      if (cutRowKept(r)) m[s] += 1;  // 删除状态不计入统计（REQ-033）
    });
    return Object.keys(m).map(function(k) {
      return {spk: k, count: m[k]};
    }).sort(function(a, b) { return parseInt(a.spk, 10) - parseInt(b.spk, 10); });
  }
  function cutSpkBarRender() {  // 统计条（chips + 查找/导航/删除/重算），保留输入值
    var bar = document.getElementById('slirn-cut-spk-bar');
    if (!bar) return;
    var keepQ = '';
    var prevQ = bar.querySelector('#slirn-cut-spk-q');
    if (prevQ) keepQ = prevQ.value;
    var keepSkip = true;  // REQ-20260918-041：默认勾选「跳过已删除」
    var prevSkip = bar.querySelector('#slirn-cut-spk-skipdel');
    if (prevSkip) keepSkip = prevSkip.checked;
    var chips = cutSpkStats().map(function(c) {
      return '<span class="slirn-cut-spk-chip" data-action="cut-spk-chip" data-spk="' + c.spk + '"'
        + ' title="点击填入查找框（统计仅计未删除记录）">👤' + c.spk + ' · <b>' + c.count + '</b> 条</span>';
    }).join('');
    bar.innerHTML =
      '<div class="slirn-cut-spk-title">👥 人员统计（仅计未删除记录 · 关联已保存，重进任务自动显示）</div>'
      + '<div class="slirn-cut-spk-chips">' + chips + '</div>'
      + '<div class="slirn-cut-spk-find">按人员ID查找：'
      + '<input id="slirn-cut-spk-q" type="number" min="1" step="1" placeholder="如 2">'
      + '<label class="slirn-cut-spk-skiplbl" title="勾选后「上一条/下一条」只在未删除的记录间跳转">'
      + '<input id="slirn-cut-spk-skipdel" type="checkbox" checked>跳过已删除</label>'
      + '<button class="slirn-btn slirn-btn-xs" data-action="cut-spk-prev">⬆️ 上一条</button>'
      + '<button class="slirn-btn slirn-btn-xs" data-action="cut-spk-next">⬇️ 下一条</button>'
      + '<button class="slirn-btn slirn-btn-xs" data-action="cut-spk-delete">❌ 删除该人员全部记录</button>'
      + '<button class="slirn-btn slirn-btn-xs" data-action="cut-spk-recount">🧮 重新统计</button>'
      + '<span class="slirn-cut-spk-hint">统计只计未删除记录 — 删除非主讲人员后重算即可确认清零；删除改判需「💾 保存切分决策」落盘</span></div>';
    bar.style.display = '';
    bar.dataset.linked = '1';
    var q = bar.querySelector('#slirn-cut-spk-q');
    if (q) q.value = keepQ;
    var skipEl = bar.querySelector('#slirn-cut-spk-skipdel');
    if (skipEl) skipEl.checked = keepSkip;
  }
  // ===== 搜索定位（REQ-20260918-040）：内容片段输入 → 匹配行间循环定位 =====
  // 行上服务端渲染 data-text（显示文本）+ data-orig（切分整段行附原文，更正行搜原文可命中）；
  // 修订只在过滤后可见行中搜，切分全量行（无过滤）。
  function searchTextOf(row) {
    return ((row.getAttribute('data-text') || '') + '\n'
          + (row.getAttribute('data-orig') || '')).toLowerCase();
  }
  function searchQuery(boxId) {
    var q = ((document.getElementById(boxId) || {}).value || '').trim().toLowerCase();
    if (!q) { toast('❌ 请先输入要搜索的字幕内容', 'error'); return null; }
    return q;
  }
  function searchCountSync(boxId, rowsFn) {
    var cnt = document.getElementById(boxId.replace('-q', '-count'));
    if (!cnt) return;
    var q = ((document.getElementById(boxId) || {}).value || '').trim().toLowerCase();
    if (!q) { cnt.textContent = ''; return; }
    var n = rowsFn().filter(function(r) { return searchTextOf(r).indexOf(q) >= 0; }).length;
    cnt.textContent = n ? (n + ' 处') : '无匹配';
  }
  function searchNavTo(boxId, rowsFn, dir2, markSel) {
    var q = searchQuery(boxId);
    if (!q) return;
    var rows = rowsFn().filter(function(r) { return searchTextOf(r).indexOf(q) >= 0; });
    if (!rows.length) { toast('❌ 没有匹配「' + q + '」的字幕行'); return; }
    var cur = rows.filter(function(r) { return r.classList.contains('kbsel'); })[0];
    var idx = cur ? rows.indexOf(cur) : -1;
    var next = idx < 0 ? (dir2 > 0 ? 0 : rows.length - 1) : (idx + dir2 + rows.length) % rows.length;
    var row = rows[next];
    markSel(row);
    var idxTxt = (row.querySelector('.slirn-sub-idx') || {textContent: row.getAttribute('data-id') || ''}).textContent.trim();
    toast('🔎「' + q + '」第 ' + (next + 1) + '/' + rows.length + ' 条（序号 ' + idxTxt + '）');
    searchCountSync(boxId, rowsFn);
  }
  document.addEventListener('input', function(e) {
    if (!e.target) return;
    if (e.target.id === 'slirn-rev-search-q') searchCountSync('slirn-rev-search-q', revNavRows);
    else if (e.target.id === 'slirn-cut-search-q') searchCountSync('slirn-cut-search-q', cutRows);
  });
  document.addEventListener('keydown', function(e) {
    if (!e.target) return;
    if (e.target.id !== 'slirn-rev-search-q' && e.target.id !== 'slirn-cut-search-q') return;
    if (e.key === 'Enter') {
      if (e.target.id === 'slirn-rev-search-q')
        searchNavTo('slirn-rev-search-q', revNavRows, e.shiftKey ? -1 : 1, revMarkSel);
      else
        searchNavTo('slirn-cut-search-q', cutRows, e.shiftKey ? -1 : 1, cutMarkSel);
      e.preventDefault();
    }
  });
  function cutSpkLink(btn) {  // 👤 关联人员ID → POST cut_speaker_link → 注入徽章 + 统计条
    var tid = btn.getAttribute('data-task-id') || '';
    btn.disabled = true;
    btn.textContent = '⏳ 关联中…';
    postJSON(SLIRN_API + '/cut_speaker_link', {task_id: tid}).then(function(r) {
      btn.disabled = false;
      btn.textContent = '👤 关联人员ID';
      if (!(r && r.ok && r.link && r.link.available)) {
        toast('❌ ' + ((r && r.error) || '关联失败'), 'error');
        return;
      }
      var rowsMap = r.link.rows || {}, n = 0;
      cutRows().forEach(function(row) {
        var spk = rowsMap[row.getAttribute('data-id') || ''];
        if (spk) {
          row.setAttribute('data-spk', String(spk));
          var old = row.querySelector('.slirn-cut-spk');
          if (old) old.remove();
          var b = document.createElement('span');
          b.className = 'slirn-cut-spk';
          b.textContent = '👤' + spk;
          b.title = '人员 ' + spk + '（时间段重叠最大的字幕段说话人）';
          // REQ-20260917-036：徽章放序号之后、时间戳之前（同行不折行 — CSS 显式第2轨）
          var anchor = row.querySelector('.slirn-sub-idx');
          if (anchor && anchor.nextSibling) row.insertBefore(b, anchor.nextSibling);
          else row.appendChild(b);
          n += 1;
        } else {
          row.removeAttribute('data-spk');
          var ob = row.querySelector('.slirn-cut-spk');
          if (ob) ob.remove();
        }
      });
      cutSpkBarRender();
      btn.textContent = '🔄 重新关联人员ID';
      var barEl = document.getElementById('slirn-cut-spk-bar');
      if (barEl) barEl.dataset.linked = '1';  // 已关联标记（重新统计守卫用）
      toast('👥 已标注 ' + n + ' 行（' + (r.link.stats || []).length + ' 位人员）· 关联已保存，重进任务自动显示'
        + ' — 删除后记得「💾 保存切分决策」');
    }, function() {
      btn.disabled = false;
      btn.textContent = '👤 关联人员ID';
      toast('❌ 网络错误，请重试', 'error');
    });
  }
  function cutSpkQuery() {  // 查找框值（无统计条 → null 并提示）
    var q = document.getElementById('slirn-cut-spk-q');
    if (!q) { toast('先点「👤 关联人员ID」建立人员关联', 'error'); return null; }
    var v = (q.value || '').trim();
    if (!v) { toast('先输入人员ID（如 2，可点统计条快速填入）', 'error'); q.focus(); return null; }
    return v;
  }
  function cutSpkNav(dir) {  // 上一条/下一条：该人员行间循环跳转，kbsel 高亮 + 滚动定位
    var spk = cutSpkQuery();          // REQ-20260917-035：勾选「跳过已删除」→ 只在未删除记录间跳
    if (spk === null) return;
    var skipEl = document.getElementById('slirn-cut-spk-skipdel');
    var skipDel = !!(skipEl && skipEl.checked);
    var rows = cutSpkRows().filter(function(r) {
      return r.getAttribute('data-spk') === spk && (!skipDel || cutRowKept(r));
    });
    if (!rows.length) {
      var any = cutSpkRows().some(function(r) { return r.getAttribute('data-spk') === spk; });
      toast(any ? '👤' + spk + ' 的记录已全部删除 — 取消勾选「跳过已删除」可继续翻看'
                : '👤' + spk + ' 无匹配字幕记录', 'error');
      return;
    }
    var curRow = document.querySelector('#slirn-cut-list .slirn-cut-row.kbsel');
    var idx = rows.indexOf(curRow);  // 当前选中不在该人员行内 → 从头/尾起
    var next = idx < 0 ? (dir > 0 ? 0 : rows.length - 1)
                       : (idx + dir + rows.length) % rows.length;
    cutMarkSel(rows[next]);
    toast('👤' + spk + ' 第 ' + (next + 1) + '/' + rows.length + ' 条（编号 '
      + rows[next].getAttribute('data-id') + '）' + (skipDel ? ' · 已跳过删除' : ''));
  }
  function cutSpkDelete() {  // 删除该人员全部记录：整段 → 组 act=delete；子段 → mark=delete
    var spk = cutSpkQuery();
    if (spk === null) return;
    var rows = cutSpkRows().filter(function(r) { return r.getAttribute('data-spk') === spk; });
    if (!rows.length) { toast('👤' + spk + ' 无匹配字幕记录', 'error'); return; }
    if (!window.confirm('把人员 👤' + spk + ' 的 ' + rows.length + ' 条字幕记录全部改判删除？\n'
      + '（可逐条/逐组翻回，自动保存后影响成片）')) return;
    cutCloseResplit();
    var nWhole = 0, nSub = 0;
    rows.forEach(function(r) {
      var g = r.closest('.slirn-cut-group');
      if (!g) return;
      if (r.classList.contains('sub')) {
        if (g.getAttribute('data-act') === 'keep') {  // 整组保留改判会压住子段 mark → 先撤销
          g.removeAttribute('data-act');
          cutActBadge(g, '');
        }
        cutSpkSetMark(r, 'delete');
        nSub += 1;
      } else {
        g.setAttribute('data-act', 'delete');
        cutActBadge(g, 'delete');
        nWhole += 1;
      }
    });
    cutSpkBarRender();
    toast('❌ 👤' + spk + ' 已改判删除：整段 ' + nWhole + ' + 子段 ' + nSub
      + '（可翻回；其记录已不计入统计，点「🧮 重新统计」可确认清零）');
    cutAutoSave();
  }

  // ===== REQ-20260919-068：字幕修订阶段关联人员 ID =====
  // 模式与 cutSpk* 一致：行集合换为 rev rows；删除口径简化为 decision=delete。
  function revRowKept(row) {
    return (row.getAttribute('data-decision') || '') !== 'delete';
  }
  function revSpkRows() {  // 已关联的修订行（data-spk 不为空）
    return revRows().filter(function(r) { return !!r.getAttribute('data-spk'); });
  }
  function revSpkStats() {  // 按 DOM 现算 → [{spk, count}]（spk 升序；count=未删除行数）
    var m = {};
    revSpkRows().forEach(function(r) {
      var s = r.getAttribute('data-spk');
      if (!s) return;
      if (!(s in m)) m[s] = 0;
      if (revRowKept(r)) m[s] += 1;  // 删除决策不计入统计
    });
    return Object.keys(m).sort(function(a, b) { return parseInt(a, 10) - parseInt(b, 10); })
      .map(function(s) { return { spk: parseInt(s, 10), count: m[s] }; });
  }
  function revSpkBarRender() {  // 统计条（chips + 查找/导航/删除/重算），保留输入值
    var bar = document.getElementById('slirn-rev-spk-bar');
    if (!bar) return;
    var keepQ = '';
    var prevQ = bar.querySelector('#slirn-rev-spk-q');
    if (prevQ) keepQ = prevQ.value;
    var keepSkip = true;
    var prevSkip = bar.querySelector('#slirn-rev-spk-skipdel');
    if (prevSkip) keepSkip = prevSkip.checked;
    var chips = revSpkStats().map(function(c) {
      return '<span class="slirn-rev-spk-chip" data-action="rev-spk-chip" data-spk="' + c.spk + '"'
        + ' title="点击填入查找框（统计仅计未删除决策行）">👤' + c.spk + ' · <b>' + c.count + '</b> 条</span>';
    }).join('');
    bar.innerHTML =
      '<div class="slirn-rev-spk-title">👥 人员统计（仅计未删除决策行 · 关联已保存，重进任务自动显示）</div>'
      + '<div class="slirn-rev-spk-chips">' + chips + '</div>'
      + '<div class="slirn-rev-spk-find">按人员ID查找：'
      + '<input id="slirn-rev-spk-q" type="number" min="1" step="1" placeholder="如 2">'
      + '<label class="slirn-rev-spk-skiplbl" title="勾选后「上一条/下一条」只在未删除决策行间跳转">'
      + '<input id="slirn-rev-spk-skipdel" type="checkbox" checked>跳过已删除</label>'
      + '<button class="slirn-btn slirn-btn-xs" data-action="rev-spk-prev">⬆️ 上一条</button>'
      + '<button class="slirn-btn slirn-btn-xs" data-action="rev-spk-next">⬇️ 下一条</button>'
      + '<button class="slirn-btn slirn-btn-xs" data-action="rev-spk-delete">❌ 删除该人员全部记录</button>'
      + '<button class="slirn-btn slirn-btn-xs" data-action="rev-spk-recount">🧮 重新统计</button>'
      + '<span class="slirn-rev-spk-hint">统计只计未删除决策行 — 删除非主讲人员后重算即可确认清零；删除改判需「💾 保存修订决策」落盘</span></div>';
    bar.style.display = '';
    bar.dataset.linked = '1';
    var q = bar.querySelector('#slirn-rev-spk-q');
    if (q) q.value = keepQ;
    var skipEl = bar.querySelector('#slirn-rev-spk-skipdel');
    if (skipEl) skipEl.checked = keepSkip;
  }
  function revSpkLink(btn) {  // 👤 关联人员ID → POST rev_speaker_link → 注入徽章 + 统计条
    var tid = btn.getAttribute('data-task-id') || '';
    btn.disabled = true;
    btn.textContent = '⏳ 关联中…';
    postJSON(SLIRN_API + '/rev_speaker_link', {task_id: tid}).then(function(r) {
      btn.disabled = false;
      btn.textContent = '👤 关联人员ID';
      if (!(r && r.ok && r.link && r.link.available)) {
        toast('❌ ' + ((r && r.error) || '关联失败'), 'error');
        return;
      }
      var rowsMap = r.link.rows || {}, n = 0;
      revRows().forEach(function(row) {
        var spk = rowsMap[row.getAttribute('data-i') || ''];
        if (spk) {
          row.setAttribute('data-spk', String(spk));
          var badge = row.querySelector('.slirn-rev-spk');
          if (badge) badge.textContent = '👤' + spk;
          n += 1;
        } else {
          row.removeAttribute('data-spk');
          var ob = row.querySelector('.slirn-rev-spk');
          if (ob) ob.textContent = '';
        }
      });
      revSpkBarRender();
      btn.textContent = '🔄 重新关联人员ID';
      var barEl = document.getElementById('slirn-rev-spk-bar');
      if (barEl) barEl.dataset.linked = '1';
      toast('👥 已标注 ' + n + ' 行（' + (r.link.stats || []).length + ' 位人员）· 关联已保存，重进任务自动显示'
        + ' — 删除后记得「💾 保存修订决策」');
    }, function() {
      btn.disabled = false;
      btn.textContent = '👤 关联人员ID';
      toast('❌ 网络错误，请重试', 'error');
    });
  }
  function revSpkQuery() {  // 查找框值
    var q = document.getElementById('slirn-rev-spk-q');
    if (!q) { toast('先点「👤 关联人员ID」建立人员关联', 'error'); return null; }
    var v = (q.value || '').trim();
    if (!v) { toast('先输入人员ID（如 2，可点统计条快速填入）', 'error'); q.focus(); return null; }
    return v;
  }
  function revSpkNav(dir) {  // 上一条/下一条：修订行间循环跳转，kbsel 高亮 + 滚动定位
    var spk = revSpkQuery();
    if (spk === null) return;
    var skipEl = document.getElementById('slirn-rev-spk-skipdel');
    var skipDel = !!(skipEl && skipEl.checked);
    var rows = revSpkRows().filter(function(r) {
      return r.getAttribute('data-spk') === spk && (!skipDel || revRowKept(r));
    });
    if (!rows.length) {
      var any = revSpkRows().some(function(r) { return r.getAttribute('data-spk') === spk; });
      toast(any ? '👤' + spk + ' 的记录已全部删除 — 取消勾选「跳过已删除」可继续翻看'
                : '👤' + spk + ' 无匹配字幕记录', 'error');
      return;
    }
    var curRow = revRows().filter(function(r) { return r.classList.contains('kbsel'); })[0];
    var idx = rows.indexOf(curRow);
    var next = idx < 0 ? (dir > 0 ? 0 : rows.length - 1)
                       : (idx + dir + rows.length) % rows.length;
    revMarkSel(rows[next]);
    toast('👤' + spk + ' 第 ' + (next + 1) + '/' + rows.length + ' 条（序号 '
      + (rows[next].getAttribute('data-i') || '?') + '）' + (skipDel ? ' · 已跳过删除' : ''));
  }
  function revSpkDelete() {  // 删除该人员全部记录：行 decision=delete
    var spk = revSpkQuery();
    if (spk === null) return;
    var rows = revSpkRows().filter(function(r) { return r.getAttribute('data-spk') === spk; });
    if (!rows.length) { toast('👤' + spk + ' 无匹配字幕记录', 'error'); return; }
    if (!window.confirm('把人员 👤' + spk + ' 的 ' + rows.length + ' 条字幕记录全部决策改为「删除」？\n'
      + '（未保存 — 可逐条翻回；点「💾 保存修订决策」后落盘并影响成片）')) return;
    var n = 0;
    rows.forEach(function(r) {
      var sel = r.querySelector('.slirn-rev-select');
      if (sel) { sel.value = 'delete'; n += 1; }
      // 触发 select 的 change 事件让 JS 同步 UI 状态（badge 等）
      if (sel) sel.dispatchEvent(new Event('change', { bubbles: true }));
    });
    revSpkBarRender();
    toast('❌ 👤' + spk + ' 已改判删除 ' + n + ' 条（未保存 — 可翻回；其记录已不计入统计，点「🧮 重新统计」可确认清零）');
  }
  // 粗剪合成（REQ-20260916-016，可选步骤）：启动后台拼接 → 轮询进度 → 完成注入预览
  function rcFmtDur(sec) {
    var s = Math.floor(sec || 0), m = Math.floor(s / 60), h = Math.floor(m / 60);
    var two = function(n) { return (n < 10 ? '0' : '') + n; };
    return h ? h + ':' + two(m % 60) + ':' + two(s % 60) : m + ':' + two(s % 60);
  }
  function rcInjectPreview(tid, result) {
    var pane = document.getElementById('slirn-wb-pane-rough_compose');
    if (!pane) return;
    var old = document.getElementById('slirn-rc-player');  // 可能已浮层化（REQ-20260918-043），全文档找
    if (old && old.closest('.slirn-video-wrap')) {  // 已有预览 → 换源刷新即可
      old.src = '/slirn/api/video/' + encodeURIComponent(tid) + '?src=rough_compose&t=' + Date.now();
      old.load();
      var meta = pane.querySelector('.slirn-sub-meta:last-of-type');
      if (meta && result) meta.textContent = '🎞️ 粗剪成片 · ' + result.size_mb + ' MB · ' + rcFmtDur(result.duration);
      return;
    }
    var card = pane.querySelector('.slirn-card');
    if (!card) return;
    var wrap = document.createElement('div');
    wrap.className = 'slirn-video-wrap';
    wrap.style.marginTop = '12px';
    wrap.innerHTML = '<video id="slirn-rc-player" controls preload="metadata" src="'
      + '/slirn/api/video/' + encodeURIComponent(tid) + '?src=rough_compose"></video>';
    card.appendChild(wrap);
    var meta = document.createElement('div');
    meta.className = 'slirn-sub-meta';
    meta.style.marginTop = '8px';
    meta.textContent = result ? ('🎞️ 粗剪成片 · ' + result.size_mb + ' MB · ' + rcFmtDur(result.duration)) : '🎞️ 粗剪成片已生成';
    card.appendChild(meta);
    bindSpeedControl(wrap.querySelector('video'));  // 倍速控件（REQ-20260916-014）
  }
  // 2026-10-28：刷新左侧阶段条状态（轻量端点，不重拉整份工作台 HTML）。
  // 背景：阶段条是服务端渲染的，阶段 job 跑完后不刷新就一直停在「执行中」，
  // 用户表现为「这阶段明明做完了，却进不去下一阶段」。
  function refreshWbStageStates(tid, opts) {
    opts = opts || {};
    return postJSON(SLIRN_API + '/wb_stage_states', {task_id: tid}).then(function(r) {
      if (!r || !r.ok || !Array.isArray(r.states)) return null;
      var panes = r.panes || [];
      r.states.forEach(function(state, i) {
        var el = document.querySelector('#slirn-tab-workbench .slirn-wb-stage[data-pane="' + panes[i] + '"]');
        if (!el) return;
        el.classList.remove('done', 'current', 'pending');
        el.classList.add(state);
        var mark = el.querySelector('.slirn-wb-stage-mark');
        // done → ✓；current/pending → 阶段序号（与后端渲染口径一致）
        if (mark) mark.textContent = (state === 'done') ? '✓' : String(i);
      });
      // 聚焦到当前阶段（服务端算的 focus）
      if (typeof opts.focus !== 'undefined' && r.focus != null) {
        var paneKey = panes[r.focus];
        if (paneKey) {
          var target = document.querySelector('#slirn-tab-workbench .slirn-wb-stage[data-pane="' + paneKey + '"]');
          if (target) target.classList.add('active');
        }
      }
      return r;
    });
  }
  // 合成进度轮询（2026-10-28 从 rcCompose 抽出）：合成跑在服务端后台线程，
  // 刷新页面只丢前端显示——工作台重载后用同一套轮询把进度接回来。
  // btn._rcTimer 防重复起两个 interval（点按钮 + 自动接续撞车时先清旧的）。
  function rcWatchJob(tid, btn) {
    var status = document.getElementById('slirn-rc-status');
    var setBtn = function(txt, dis) { btn.textContent = txt; btn.disabled = !!dis; };
    var show = function(msg) { if (status) { status.style.display = ''; status.textContent = msg; } };
    if (btn._rcTimer) { clearInterval(btn._rcTimer); }
    btn._rcTimer = setInterval(function() {
      postJSON(SLIRN_API + '/compose_rough_status', {task_id: tid}).then(function(s) {
        if (!s || !s.ok || !s.job) return;
        var j = s.job;
        if (j.state === 'running') {
          var pct = Math.max(1, Math.min(99, Math.round(j.progress || 0)));
          setBtn('🎬 合成中… ' + pct + '%', true);
          show('⏳ 合成进行中 ' + pct + '% — 可继续其它操作，完成后此处自动显示预览');
          return;
        }
        clearInterval(btn._rcTimer); btn._rcTimer = null;
        if (j.state === 'done') {
          setBtn('🎬 重新合成粗剪视频', false);
          show('✅ 粗剪成片已生成 — 下方预览整体效果');
          toast('✅ 粗剪成片已生成');
          rcInjectPreview(tid, j.result);
          // 2026-10-28：刷新左侧阶段条（粗剪合成 → ✓ done，优化字幕 → current）
          // 并按「完成后自动打开下一阶段」开关切过去；不切时给出手动入口。
          refreshWbStageStates(tid, {focus: true}).then(function(st) {
            if (!st) return;
            var panes = st.panes || [];
            var focusPane = panes[st.focus];
            if (!focusPane || focusPane === 'rough_compose') return;
            if (wbAutoNextOn()) {
              switchWbPane(focusPane);
              toast('✅ 「粗剪合成」完成 — 已自动打开下一阶段界面');
            } else {
              var stageEl = document.querySelector('#slirn-tab-workbench .slirn-wb-stage[data-pane="' + focusPane + '"]');
              var label = focusPane;
              if (stageEl) {
                var titleEl = stageEl.querySelector('.slirn-wb-stage-title') || stageEl;
                label = (titleEl.textContent || '').trim() || focusPane;
              }
              show('✅ 粗剪成片已生成 — 下一阶段「' + label + '」已就绪，点左侧阶段条继续');
            }
          });
        } else {
          setBtn('🎬 重新合成粗剪视频', false);
          show('❌ ' + (j.error || '合成失败'));
          toast('❌ ' + (j.error || '合成失败'), 'error');
        }
      });
    }, 2000);
  }
  function rcCompose(btn) {
    var tid = btn.getAttribute('data-task-id') || '';
    var status = document.getElementById('slirn-rc-status');
    var setBtn = function(txt, dis) { btn.textContent = txt; btn.disabled = !!dis; };
    var show = function(msg) { if (status) { status.style.display = ''; status.textContent = msg; } };
    setBtn('🎬 合成中…', true);
    show('⏳ 正在启动合成…');
    postJSON(SLIRN_API + '/compose_rough', {task_id: tid}).then(function(r) {
      if (!r || !r.ok) {
        setBtn('🎬 合成粗剪视频', false);
        show('');
        if (status) status.style.display = 'none';
        toast('❌ ' + (r && r.error ? r.error : '启动失败'), 'error');
        return;
      }
      toast(r.toast || '🎬 合成已启动');
      rcWatchJob(tid, btn);
    });
  }
  // ===== 粗剪合成 · 删除 / 字幕 SRT 操作（REQ-20260916-019） =====
  function rcDelete(btn) {
    var tid = btn.getAttribute('data-task-id') || '';
    if (!tid) { toast('❌ 缺少 task_id', 'error'); return; }
    if (!window.confirm('删除「粗剪成片」（mp4 + 随片 srt 副产物）？\n'
        + '删除后需要重新合成才能预览效果（约 11 分钟）。')) return;
    btn.disabled = true;
    postJSON(SLIRN_API + '/compose_rough_delete', {task_id: tid}).then(function(r) {
      btn.disabled = false;
      if (!r || !r.ok) {
        toast('❌ ' + (r && r.error ? r.error : '删除失败'), 'error');
        return;
      }
      toast(r.toast || '🗑️ 已删除');
      openWorkbench(tid);  // 刷新工作台：删按钮消失、字幕预览保留（产物没了，按钮消失即可）
    });
  }
  function rcSubsCopy(btn) {
    var pane = btn.closest('.slirn-card');
    if (!pane) { toast('❌ 找不到字幕面板', 'error'); return; }
    var pre = pane.querySelector('pre.slirn-rc-subs-body');
    var srt = pre ? pre.textContent : '';
    if (!srt) { toast('❌ 没有可复制的字幕', 'error'); return; }
    var done = function() { toast('📋 已复制 ' + srt.split('\n\n').length + ' 段 SRT'); };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(srt).then(done, function() { fallbackCopy(srt, done); });
    } else {
      fallbackCopy(srt, done);
    }
  }
  function fallbackCopy(text, cb) {
    var ta = document.createElement('textarea');
    ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); cb && cb(); } catch (e) { toast('❌ 复制失败', 'error'); }
    document.body.removeChild(ta);
  }
  function rcSubsDownload(btn) {
    var tid = btn.getAttribute('data-task-id') || '';
    if (!tid) { toast('❌ 缺少 task_id', 'error'); return; }
    btn.disabled = true;
    postJSON(SLIRN_API + '/compose_rough_preview_subs', {task_id: tid}).then(function(r) {
      btn.disabled = false;
      if (!r || !r.ok || !r.srt) {
        toast('❌ ' + (r && r.error ? r.error : '获取 SRT 失败'), 'error');
        return;
      }
      var blob = new Blob([r.srt], {type: 'text/plain;charset=utf-8'});
      var url = URL.createObjectURL(blob);
      var a = document.createElement('a');
      a.href = url; a.download = 'rough_compose_subs_' + tid + '.srt';
      document.body.appendChild(a); a.click();
      setTimeout(function() { URL.revokeObjectURL(url); document.body.removeChild(a); }, 100);
      toast('⬇️ 已下载 ' + (r.lines || 0) + ' 行 SRT');
    });
  }
  // ===== 优化字幕（REQ-20260917-030）：轮询 + 词过滤 + 出现项采纳/编辑 + 保存 + SRT 下载 =====
  var optPollTimer = null;
  function startOptPolling(tid) {
    if (optPollTimer) { clearInterval(optPollTimer); optPollTimer = null; }
    var update = function() {
      postJSON(SLIRN_API + '/optimize_subtitle_status', {task_id: tid}).then(function(r) {
        if (!r || !r.ok) return;
        var j = r.job || {};
        var el = revVis('slirn-opt-status');
        if (j.state === 'running') {
          if (el) {
            el.style.display = '';
            el.dataset.state = 'running';
            el.innerHTML = '⏳ ' + escapeHtml(j.stage || '处理中')
              + (j.progress ? ' · ' + Math.round(j.progress) + '%' : '')
              + ' · 已耗时 ' + fmtElapsed(j.elapsed_s || 0);
          }
        } else {
          if (optPollTimer) { clearInterval(optPollTimer); optPollTimer = null; }
          if (j.state === 'done') {
            toast('✅ 优化字幕完成：识别 ' + (j.lines || 0) + ' 行 · 不明确 '
              + (j.occurrences || 0) + ' 处');
            openWorkbench(tid);  // 刷新面板（词频 + 出现项列表 + 阶段态）
          } else if (j.state === 'error') {
            if (el) {
              el.style.display = '';
              el.dataset.state = 'error';
              el.innerHTML = '❌ ' + escapeHtml(j.error || '优化失败');
            }
            toast('❌ 优化字幕失败', 'error');
          }
        }
      });
    };
    update();
    optPollTimer = setInterval(update, 2000);
  }
  function optStart(btn) {
    var tid = btn.getAttribute('data-task-id') || '';
    var payload = {task_id: tid};
    if (btn.getAttribute('data-has') === '1') {
      if (!window.confirm('重新优化将覆盖现有结果，并重置全部采纳决定。确定继续？')) return;
      payload.force = true;
    }
    postJSON(SLIRN_API + '/optimize_subtitle', payload).then(function(r) {
      if (r && r.ok) {
        toast(r.toast || '已开始优化');
        var el = revVis('slirn-opt-status');
        if (el) { el.dataset.state = 'running'; el.style.display = ''; el.innerHTML = '⏳ 已提交…'; }
        startOptPolling(tid);
      } else if (r && r.error) {
        toast('❌ ' + r.error, 'error');
      }
    });
  }
  // REQ-20260918-055：替换值输入框按 Enter → 自动标记为「已修正/已完成」
  // （采纳 + reviewed 标记 + 词行进度 +1 + 瞬时视觉反馈）
  function optOccEnterConfirm(inp, ev) {
    var occ = inp.closest('.slirn-opt-occ');
    if (!occ) return;
    var v = (inp.value || '').trim();
    if (!v) {
      // 替换值为空 → 阻止默认（避免吞掉换行/提交）+ toast 警告
      if (ev && ev.preventDefault) ev.preventDefault();
      toast('⚠️ 替换值为空，无法标记完成', 'warning');
      return;
    }
    // 阻止默认 Enter（避免在文本框里插入换行/触发表单提交）
    if (ev && ev.preventDefault) ev.preventDefault();
    // 1) 自动采纳（如果原本是 0）— 同点 ✓ 按钮效果
    if (occ.getAttribute('data-applied') !== '1') {
      occ.setAttribute('data-applied', '1');
      var tog = occ.querySelector('button.slirn-opt-toggle');
      if (tog) {
        tog.textContent = '✓';
        tog.title = '已采纳（保存时替换）';
      }
    }
    // 2) 标记 reviewed（幂等：optOccMarkReviewed 内部判 reviewed=1 直接 return）
    optOccMarkReviewed(occ);
    // 3) 瞬时视觉反馈（600ms 后移除）
    occ.classList.add('slirn-opt-occ-just-done');
    setTimeout(function() {
      if (occ && occ.classList) occ.classList.remove('slirn-opt-occ-just-done');
    }, 600);
    // 4) REQ-20260918-056：自动持久化（不刷面板，保留焦点）
    var inner = document.getElementById('slirn-tab-workbench-inner');
    var tid = inner ? (inner.getAttribute('data-task-id') || '') : '';
    if (tid) optAutoSave(tid);
  }
  function optOccToggle(btn) {  // 出现项采纳/不采纳（纯前端，保存时统一提交）
    var w = btn.closest('.slirn-opt-occ');
    if (!w) return;
    var now = w.getAttribute('data-applied') === '1' ? 0 : 1;
    w.setAttribute('data-applied', String(now));
    btn.textContent = now === 1 ? '✓' : '✕';
    btn.title = now === 1 ? '已采纳（保存时替换）' : '已不采纳（保留原文）';
    optOccMarkReviewed(w);  // REQ-038：明确处理过（无论采纳与否）→ 计入词进度
    // REQ-20260918-057A：与回车一致 — 切换后自动保存（含并发锁）
    var inner = document.getElementById('slirn-tab-workbench-inner');
    var tid = inner ? (inner.getAttribute('data-task-id') || '') : '';
    if (tid) optAutoSave(tid);
  }
  function optOccMarkReviewed(occEl) {  // REQ-038：occ 标记已处理 + 所属词行进度刷新
    if (!occEl || occEl.getAttribute('data-reviewed') === '1') return;
    occEl.setAttribute('data-reviewed', '1');
    optWordRowRefresh(occEl.getAttribute('data-word') || '');
    // 2026-09-29 用户反馈：处理完的字幕行保持绿色（防重复处理）。口径与词行
    // ✅ 一致：本行全部出现处都已 reviewed（采纳或不采纳都算）→ 行挂 occ-done。
    // 服务端 _line_html 用同一口径渲染初始态，这里负责交互过程中的实时传播。
    var row = occEl.closest ? occEl.closest('.slirn-opt-row') : null;
    if (row && !row.classList.contains('occ-done')) {
      var occs = row.querySelectorAll('.slirn-opt-occ');
      var all = occs.length > 0;
      for (var k = 0; k < occs.length; k++) {
        if (occs[k].getAttribute('data-reviewed') !== '1') { all = false; break; }
      }
      if (all) row.classList.add('occ-done');
    }
  }
  function optWordRowRefresh(word) {  // 重算词行 done 状态（x/y + 徽章 + data-done）
    if (!word) return;
    var row = document.querySelector('#slirn-opt-words .slirn-opt-word[data-word="' + cssEscape(word) + '"]');
    if (!row) return;
    var occs = document.querySelectorAll('.slirn-opt-occ[data-word="' + cssEscape(word) + '"]');
    var total = occs.length, done = 0;
    occs.forEach(function(o) { if (o.getAttribute('data-reviewed') === '1') done += 1; });
    row.setAttribute('data-done', done >= total && total > 0 ? '1' : '0');
    var prog = row.querySelector('.slirn-opt-word-prog');
    if (prog) prog.textContent = done + '/' + total;
    var badge = row.querySelector('.slirn-opt-word-badge');
    if (badge) {
      var ok = done >= total && total > 0;
      badge.textContent = ok ? '✅ 已完成' : '⬜ 未完成';
      badge.title = ok ? '该词全部出现处都已明确处理（采纳或不采纳）'
                       : '还有 ' + (total - done) + ' 处未处理 — 逐处切换 ✓/✕ 或编辑替换值即计为已处理';
    }
  }
  function optWordFilter(chip) {  // 点词 → 列出含该词的所有出现行 + 各上下 5 行；再点取消
    var word = chip.getAttribute('data-word') || '';
    var list = revVis('slirn-opt-list');
    if (!list || !word) return;
    var CONTEXT_RADIUS = 5;  // 上下文半径：目标行前后各 5 行 = 共 11 行上下文
    document.querySelectorAll('#slirn-opt-words .slirn-opt-chip').forEach(function(c) {
      c.classList.remove('active');
    });
    // 复用：同一词再点 → 清空上下文
    if (list.getAttribute('data-filter-word') === word) {
      list.removeAttribute('data-filter-word');
      list.removeAttribute('data-filter-mode');
      list.querySelectorAll('.slirn-opt-row').forEach(function(row) {
        row.style.display = '';
        row.classList.remove('slirn-opt-row-target');
      });
      // 同步移除上下文模式 hint
      var hintOld = document.getElementById('slirn-opt-filter-hint');
      if (hintOld) hintOld.remove();
      return;
    }
    list.setAttribute('data-filter-word', word);
    list.setAttribute('data-filter-mode', 'context');
    chip.classList.add('active');

    // 一次遍历同时做两件事：
    // 1) 找出所有含目标词的行（target）及其 segment id
    // 2) 算出 target id ± CONTEXT_RADIUS 内的所有 id（去重 set）
    var targetIds = [];
    var contextIdSet = {};
    var firstHit = null;
    list.querySelectorAll('.slirn-opt-row').forEach(function(row) {
      var ws = (row.getAttribute('data-words') || '').split('\n');
      var hit = ws.indexOf(word) >= 0;
      if (hit) {
        var rid = parseInt(row.getAttribute('data-id') || '0', 10) || 0;
        targetIds.push(rid);
        for (var d = -CONTEXT_RADIUS; d <= CONTEXT_RADIUS; d++) {
          contextIdSet[rid + d] = 1;
        }
        if (!firstHit) firstHit = row;
      }
    });

    // 应用显示/隐藏 + 标记 target 行（高亮 + 视觉上看得见）
    list.querySelectorAll('.slirn-opt-row').forEach(function(row) {
      var rid = parseInt(row.getAttribute('data-id') || '0', 10) || 0;
      var isTarget = targetIds.indexOf(rid) >= 0;
      row.classList.toggle('slirn-opt-row-target', isTarget);
      row.style.display = contextIdSet[rid] ? '' : 'none';
    });

    // 顶部插一条 hint 让用户知道这是「上下文模式」+ 隐藏了多少行
    var hintId = 'slirn-opt-filter-hint';
    var oldHint = document.getElementById(hintId);
    if (oldHint) oldHint.remove();
    if (targetIds.length > 0) {
      var hint = document.createElement('div');
      hint.id = hintId;
      hint.className = 'slirn-form-hint slirn-opt-filter-hint';
      // 2026-09-29 用户反馈：这是随点词不断插删的临时 hint，若被 colEnhance 包进
      // 「ℹ️ 说明」折叠壳，点下一个词删 hint 时壳会残留 — 每处理一个词（回车即
      // 自动保存）净增一个空说明区，越积越多。预先标记 slirnCol 让 colEnhance
      // 跳过：hint 始终原地显示，不产生壳（「说明」壳收起时也不至于把它藏没）。
      hint.dataset.slirnCol = '1';
      hint.innerHTML = '🔍 <b>' + escapeHtml(word) + '</b> 出现 <b>' + targetIds.length
        + '</b> 处，每处显示上下文 ±' + CONTEXT_RADIUS + ' 行（再点同一词可清除）';
      list.parentNode.insertBefore(hint, list);
    }

    // 滚到第一个出现处（保留原行为 — REQ-20260921-NNN-shake-fix：去掉 smooth，
    // 否则「行隐藏 + 加 hint → 列表高度突变」期间平滑滚动可能被反复打断，
    // 视觉上像「滚动条上下抖」。即时滚动一次到位更稳）
    if (firstHit) try { firstHit.scrollIntoView({block: 'center'}); } catch (err) {}
  }
  function optWordFilterBtn(btn) {  // REQ-038：词列表按处理状态过滤（全部/未完成/已完成）
    var mode = btn.getAttribute('data-mode') || 'all';
    // REQ-20260918-060：colEnhance 把 .slirn-opt-word-filters 和 #slirn-opt-words
    // 包成各自独立的 .slirn-col wrap（两个 wrap 是兄弟），导致旧实现
    // box = document.getElementById('slirn-opt-words') 找不到 filter 按钮、
    // 而 filter 按钮所在的 wrap 又找不到 .slirn-opt-word 行。
    // 修复：用最近的 opt-zone 容器（同时包含 filter 按钮 + 词行）。
    var scope = btn.closest && btn.closest('.slirn-card');
    if (!scope) scope = document.body;
    scope.querySelectorAll('button[data-action="opt-word-filter"]').forEach(function(b) {
      b.classList.toggle('active', b === btn);
    });
    scope.querySelectorAll('.slirn-opt-word').forEach(function(r) {
      var done = r.getAttribute('data-done') === '1';
      r.style.display = (mode === 'all' || (mode === 'done') === done) ? '' : 'none';
    });
    // REQ-20260918-050：过滤后重置到第 1 页 + 按可见项重新分页
    optWordsCurrentPage = 1;
    paginateOptWords();
    // REQ-20260918-057B：状态切换后联动文字过滤（AND 组合）
    optWordTextFilter();
  }
  // REQ-20260918-057B：词频文字过滤（与状态过滤 AND 组合 + 250ms 防抖）
  function optWordTextFilter() {
    // REQ-20260918-060：同 optWordFilterBtn — colEnhance 把 filters 和 words
    // 包成兄弟 wrap；用 .slirn-card 作为共同 scope 才能同时找到 input/clear/buttons/words。
    var inp = document.getElementById('slirn-opt-word-text');
    var clr = document.querySelector('.slirn-opt-word-text-clear');
    if (!inp) return;
    var scope = inp.closest && inp.closest('.slirn-card');
    if (!scope) scope = document.body;
    var q = (inp.value || '').trim().toLowerCase();
    if (clr) clr.style.display = q ? '' : 'none';
    // 当前激活的状态过滤 mode（同时尊重状态过滤）
    var modeBtn = scope.querySelector('button[data-action="opt-word-filter"].active');
    var mode = modeBtn ? (modeBtn.getAttribute('data-mode') || 'all') : 'all';
    scope.querySelectorAll('.slirn-opt-word').forEach(function(r) {
      var w = (r.getAttribute('data-word') || '').toLowerCase();
      var matchText = !q || w.indexOf(q) >= 0;
      var done = r.getAttribute('data-done') === '1';
      var matchMode = (mode === 'all' || (mode === 'done') === done);
      r.style.display = (matchText && matchMode) ? '' : 'none';
    });
    // REQ-20260918-050：过滤变化后重置页码 + 重新分页
    optWordsCurrentPage = 1;
    paginateOptWords();
  }
  function bindOptWordTextFilter() {  // 幂等 — wb 重渲后重绑 input 事件
    var inp = document.getElementById('slirn-opt-word-text');
    if (!inp || inp.dataset.bound) return;
    inp.dataset.bound = '1';
    var t = null;
    inp.addEventListener('input', function() {
      if (t) clearTimeout(t);
      t = setTimeout(optWordTextFilter, 250);
    });
  }
  // ========== REQ-20260919-061：精剪视频·四素材合成器 前端函数 ==========
  function fineEnableOutputBtns() {
    // 视频素材就绪后解除「生成预览/导出」主按钮 disabled（与卡内预览/详情
    // 按钮同批解 — 否则上传/自动获取成功后主按钮还灰着，用户以为没生效）。
    // title 与服务端渲染文案保持一致（_render_fine_cut_zone）。
    var titles = {
      'fine-preview': '渲染预览（ffmpeg overlay，时长 2–30 秒可调）',
      'fine-export': '导出完整视频到 outputs/fine_export.mp4'
    };
    Object.keys(titles).forEach(function(act) {
      var b = document.querySelector('[data-action="' + act + '"]');
      if (b && b.disabled) {
        b.disabled = false;
        b.title = titles[act];
      }
    });
  }
  function fineUpload(btn, kind) {
    var fileInput = document.getElementById('slirn-fine-file-' + kind);
    if (!fileInput || !fileInput.files || !fileInput.files[0]) {
      toast('❌ 请先选择文件');
      return;
    }
    var file = fileInput.files[0];
    var inner = document.getElementById('slirn-tab-workbench-inner');
    var tid = inner ? (inner.getAttribute('data-task-id') || '') : '';
    if (!tid) { toast('❌ 缺少 task_id'); return; }
    var fd = new FormData();
    fd.append('task_id', tid);
    fd.append('kind', kind);
    fd.append('file', file);
    btn.disabled = true;
    var oldText = btn.textContent;
    btn.textContent = '📤 上传中...';
    fetch('/slirn/api/upload_fine_material_form', { method: 'POST', body: fd })
      .then(function(r) { return r.json(); })
      .then(function(j) {
        btn.disabled = false;
        btn.textContent = oldText;
        if (j.ok) {
          // 更新 status + has-file 类
          var status = document.querySelector('[data-status-kind="' + kind + '"]');
          if (status) status.innerHTML = '✅ ' + (j.path || file.name).split(/[\\/]/).pop();
          var card = document.querySelector('.slirn-fine-upload-card[data-kind="' + kind + '"]');
          if (card) {
            card.classList.add('has-file');
            card.setAttribute('data-source', 'upload');
          }
          // REQ-20261028-NNN：audio 换成自定义上传后同步会话缓存，防止
          // fineDefaultBgmSyncFromFc 拿旧 BGM 路径错误回显选中旧曲目。
          if (kind === 'audio') window._slirnFineAudioPath = (j.path || file.name);
          // REQ-20260921-NNN：上传成功后解除预览 / 详情按钮的 disabled 状态
          // （之前只更新 status 文本 + 加 has-file 类，没去 disabled → 用户看到
          // 预览/详情按钮还是灰的，体验 bug）。逐个条件 DOM 选择器解掉 disabled。
          var pvBtn = card && card.querySelector('[data-action="fine-mat-preview"]');
          if (pvBtn) {
            pvBtn.disabled = false;
            pvBtn.title = '打开预览窗口（可缩放）';
          }
          var detBtn = card && card.querySelector('[data-action="fine-mat-detail"]');
          if (detBtn) {
            detBtn.disabled = false;
            detBtn.title = '查看完整路径 + 文件元数据';
          }
          // 视频素材就绪 → 同时解除卡外「生成预览/导出」主按钮
          if (kind === 'video') fineEnableOutputBtns();
          toast('✅ ' + (j.toast || '已上传'));
          // REQ-20260920-079：超大图片上传时提示用户「合成时自动缩放」
          if (j.warning) {
            setTimeout(function() { toast(j.warning); }, 600);
          }
        } else {
          toast('❌ ' + (j.error || '上传失败'));
        }
      }).catch(function(e) {
        btn.disabled = false; btn.textContent = oldText;
        toast('❌ 网络错误: ' + e.message);
      });
  }
  function fineSetSaveStatus(text, state) {
    // REQ-20260919-061 用户反馈：保存成功后更新状态指示器。
    var el = document.getElementById('slirn-fine-save-status');
    if (!el) return;
    el.textContent = text;
    el.setAttribute('data-state', state || 'idle');
    if (state === 'saving') {
      el.classList.remove('err');
    } else if (state === 'saved') {
      el.classList.remove('err');
    } else if (state === 'error') {
      el.classList.add('err');
    }
  }
  function _fineFormatNow() {
    var d = new Date();
    var pad = function(n) { return n < 10 ? '0' + n : '' + n; };
    return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
  }
  function fineSaveAll(showToast, asTemplate) {
    // showToast=true 仅在用户主动点 💾 按钮时显示；自动保存静默更新状态指示器。
    // asTemplate=true 时（用户主动点保存按钮）：如果「模板名」为空 → window.prompt 弹窗要求填，
    //   拿到名后调 save_fine_global_profile 另存为全局模板。
    // asTemplate=false（自动保存）→ 不写模板，避免一堆「未命名」垃圾数据。
    // 收集所有滑块 + 复选框 + 字体 + 输出，一次保存（避免多次请求）
    var inner = document.getElementById('slirn-tab-workbench-inner');
    var tid = inner ? (inner.getAttribute('data-task-id') || '') : '';
    if (!tid) return;
    // 若要求存模板：先解析模板名（用户主动点保存时；asTemplate 包含默认 false）
    if (asTemplate) {
      var _nameEl = document.getElementById('slirn-fine-profile-name');
      var _name = _nameEl ? _nameEl.value.trim() : '';
      if (!_name) {
        // 弹窗要求填名（maxlength=30 与后端规则对齐）
        _name = window.prompt(
          '请填写模板名（≤30 字，会作为全局参数模板保存）',
          ''
        );
        if (_name === null) {
          // 用户取消 → 不存模板，但 fine_compose 还是要存
          toast('已跳过保存模板，只保存当前参数');
        } else {
          _name = String(_name).trim();
          if (!_name) { toast('❌ 模板名不能为空'); return; }
          if (_name.length > 30) { _name = _name.slice(0, 30); }
          if (_nameEl) _nameEl.value = _name;
        }
      }
      var _profileName = _name || '';
    }
    fineSetSaveStatus('保存中…', 'saving');
    var layout = {};
    document.querySelectorAll('.slirn-fine-slider').forEach(function(s) {
      var key = s.getAttribute('data-key') || '';
      var parts = key.split('.');
      if (parts.length !== 2) return;
      var k1 = parts[0], k2 = parts[1];
      layout[k1] = layout[k1] || {};
      layout[k1][k2] = parseFloat(s.value);
    });
    document.querySelectorAll('.slirn-fine-enabled, [data-key][type="checkbox"]').forEach(function(c) {
      var k = c.getAttribute('data-key') || '';
      if (!k) return;
      var parts = k.split('.');
      // REQ-20260920-100：5 个 .slirn-fine-enabled checkbox（cover/bg/audio/video/subtitle）
      // 的 data-key 是单段（不带「.」），旧逻辑按 k.indexOf('.')<0 一刀跳过 → 勾选
      // 状态永远不写回 fc.json，刷新页面后回到原值。现统一按 k1 = 段1 处理：
      // 单段 .slirn-fine-enabled 自动落到 layout[k1].enabled；其它 [data-key][type=checkbox]
      // 必须是 section.field 形式（例：video.crop_aspect_lock），跳过非法格式。
      var k1 = parts[0];
      if (c.classList.contains('slirn-fine-enabled')) {
        // 5 元素 enable checkbox：cover/bg/audio/video/subtitle → layout[k1].enabled
        layout[k1] = layout[k1] || {};
        layout[k1].enabled = c.checked;
      } else {
        // 普通 [data-key] checkbox 必须是 section.field 形式
        if (parts.length !== 2) return;
        var k2 = parts[1];
        layout[k1] = layout[k1] || {};
        layout[k1][k2] = c.checked;
      }
    });
    // 收集所有 3 个端点 — Promise.all 一起完成再更新状态
    var promises = [];
    promises.push(
      fetch('/slirn/api/save_fine_layout?task_id=' + encodeURIComponent(tid), {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ task_id: tid, layout: layout })
      })
        .then(function(r) { return r.json(); })
        .then(function(j) {
          // （viewport 夹紧已移除：x/y/scale 按滑块原值保存，服务端不再改写）
          return j;
        })
    );
    // 字体
    var font = {};
    document.querySelectorAll('[data-font-key]').forEach(function(el) {
      var k = el.getAttribute('data-font-key');
      if (el.type === 'checkbox') font[k] = el.checked;
      else if (el.type === 'number') font[k] = parseInt(el.value, 10);
      else font[k] = el.value;
    });
    if (Object.keys(font).length > 0) {
      promises.push(
        fetch('/slirn/api/save_fine_font?task_id=' + encodeURIComponent(tid), {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ task_id: tid, font: font })
        }).then(function(r) { return r.json(); })
      );
    }
    // 输出
    var output = {};
    document.querySelectorAll('[data-output-key]').forEach(function(el) {
      output[el.getAttribute('data-output-key')] = el.value;
    });
    if (Object.keys(output).length > 0) {
      promises.push(
        fetch('/slirn/api/save_fine_output?task_id=' + encodeURIComponent(tid), {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ task_id: tid, output: output })
        }).then(function(r) { return r.json(); })
      );
    }
    // REQ-20260919-061 扩展：背景音乐（独立于 layout/font/output）
    var audio = {};
    document.querySelectorAll('[data-audio-key]').forEach(function(el) {
      var k = el.getAttribute('data-audio-key');
      // REQ-20260920-100：data-audio-key 也用于 checkbox（启用开关），不能 parseFloat
      if (el.type === 'checkbox') {
        audio[k] = el.checked;
      } else {
        audio[k] = parseFloat(el.value);
      }
    });
    // REQ-20260920-100：「🎵 启用背景音乐」checkbox 是 .slirn-fine-enabled 单段
    // data-key="audio"，不在上面 data-audio-key 选择器里。单独补一次：
    var audioEnaCb = document.querySelector('.slirn-fine-enabled[data-key="audio"]');
    if (audioEnaCb) {
      audio.enabled = audioEnaCb.checked;
    }
    if (Object.keys(audio).length > 0) {
      promises.push(
        fetch('/slirn/api/save_fine_audio?task_id=' + encodeURIComponent(tid), {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ task_id: tid, audio: audio })
        }).then(function(r) { return r.json(); })
      );
    }
    // REQ-20260921-NNN：生成预览参数（start_h/m/s + duration）作为「设置参数」保存
    // （与 layout/font/output/audio 同级，刷新工作台不丢；用户改完点 💾 即可）。
    var preview = {};
    document.querySelectorAll('[data-preview-key]').forEach(function(el) {
      var k = el.getAttribute('data-preview-key');
      var n = parseInt(el.value, 10);
      if (!isNaN(n)) preview[k] = n;
    });
    if (Object.keys(preview).length > 0) {
      promises.push(
        fetch('/slirn/api/save_fine_preview?task_id=' + encodeURIComponent(tid), {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ task_id: tid, preview: preview })
        }).then(function(r) { return r.json(); })
      );
    }
    Promise.all(promises).then(function(results) {
      // REQ-20260919-061 用户反馈：「保存全部」一直报错的根因 — 后端用 `{"ok":true}` 而不是
      // `{"code":0}`，原版写 `r.code !== 0` 会让每个成功响应都被判失败。改成按 `ok` 字段判定，
      // 错误信息也从 `error` 字段取（与 `_err` 返回结构对齐）。
      var failed = results.find(function(r) { return !r || r.ok !== true; });
      if (failed) {
        var msg = failed.error || '未知错误';
        fineSetSaveStatus('保存失败：' + msg, 'error');
        if (showToast) toast('❌ 保存失败: ' + msg, 'error');
        return;
      }
      // 模板保存：用户主动保存且模板名非空时
      if (asTemplate && _profileName) {
        fetch('/slirn/api/save_fine_global_profile', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ task_id: tid, name: _profileName })
        })
          .then(function(r) { return r.json(); })
          .then(function(j) {
            if (j && j.ok) {
              fineSetSaveStatus('上次保存：' + _fineFormatNow() + '（模板「' + _profileName + '」）', 'saved');
              if (showToast) toast('✅ 已保存参数 + 模板「' + _profileName + '」');
            } else {
              // fine_compose 已存好；模板保存失败 → 不算整个失败，提示用户
              fineSetSaveStatus('上次保存：' + _fineFormatNow() + '（模板保存失败）', 'saved');
              if (showToast) toast('⚠️ 参数已保存，但模板保存失败: ' + ((j && j.error) || '未知错误'));
            }
          })
          .catch(function(e) {
            if (showToast) toast('⚠️ 参数已保存，但模板保存网络错误: ' + e.message);
          });
      } else {
        fineSetSaveStatus('上次保存：' + _fineFormatNow(), 'saved');
        if (showToast) toast('✅ 已保存全部参数');
      }
    }).catch(function(e) {
      fineSetSaveStatus('保存失败：网络错误', 'error');
      if (showToast) toast('❌ 保存失败: 网络错误', 'error');
    });
  }
  function bindFineControls() {  // wb 重渲后调用 — 绑所有 fine_cut 控件
    var t = null;
    function debounceSave() {
      if (t) clearTimeout(t);
      t = setTimeout(fineSaveAll, 300);
    }
    function updateCropAspect() {
      var wx = document.getElementById('slirn-fine-video-crop_w');
      var hx = document.getElementById('slirn-fine-video-crop_h');
      var aspectEl = document.getElementById('slirn-fine-crop-aspect-val');
      if (!aspectEl || !wx || !hx) return;
      var w = parseFloat(wx.value) || 0;
      var h = parseFloat(hx.value) || 0;
      aspectEl.textContent = w > 0 ? (h / w).toFixed(3) : '0.000';
    }
    // REQ-20260919-062 v7：实时计算视频显示尺寸（crop_w × scale, crop_h × scale）
    function updateVideoDisp() {
      var cw = document.getElementById('slirn-fine-video-crop_w');
      var ch = document.getElementById('slirn-fine-video-crop_h');
      var sc = document.getElementById('slirn-fine-video-scale');
      var dispW = document.getElementById('slirn-fine-video-disp-w');
      var dispH = document.getElementById('slirn-fine-video-disp-h');
      var scalePct = document.getElementById('slirn-fine-video-scale-pct');
      var aspect = document.getElementById('slirn-fine-video-aspect');
      var cropWPct = document.getElementById('slirn-fine-video-crop-w-pct');
      var cropHPct = document.getElementById('slirn-fine-video-crop-h-pct');
      if (!cw || !ch || !sc) return;
      var w = parseFloat(cw.value) || 0;
      var h = parseFloat(ch.value) || 0;
      var s = parseFloat(sc.value) || 0;
      // REQ-20260923-NNN：与渲染 filter 同一公式 — 显示宽 = 1920 × scale，
      // 显示高 = 显示宽 × crop_h/crop_w（保持「视频源裁剪」比例，不再强制 16:9；
      // 旧读数 crop_w×scale / crop_h×scale 与实际渲染不一致）。
      var dw = Math.round(1920 * s);
      var dh = w > 0 ? Math.round(dw * h / w) : 0;
      if (dispW) dispW.textContent = String(dw);
      if (dispH) dispH.textContent = String(dh);
      if (scalePct) scalePct.textContent = (s * 100).toFixed(4);
      if (aspect) aspect.textContent = dw > 0 ? (dh / dw).toFixed(3) : '0.000';
      // REQ-20260919-062 v10：占背景图宽高百分比 = crop_w / 1920 × 100%,
      // crop_h / 1080 × 100%（背景图整个区域 = 设计空间 1920×1080）。
      if (cropWPct) cropWPct.textContent = (w / 1920 * 100).toFixed(2);
      if (cropHPct) cropHPct.textContent = (h / 1080 * 100).toFixed(2);
    }
    // REQ-20260919-062 v10：去掉了页面内的预览框（设计空间画布）— 不再需要
    // _fineApplyVideoLayout / _fineApplyVideoLayout ResizeObserver。预览只通过
    // 弹窗 .slirn-mat-preview-float（每个素材独立预览）进行。
    // REQ-20260919-062 v15：crop_w 变化时自动重算 scale = crop_w / 1920（保留 4 位小数）。
    // 共用公式：scale = crop_w / 1920；clamp 到 [0.1, 2.0]；toFixed(4) 取 4 位小数。
    // 不在这里 toast（避免每次拖滑块都弹提示）；保存走 bindFineControls 的 debounceSave。
    function _recomputeScaleFromCropW() {
      var cropWEl = document.getElementById('slirn-fine-video-crop_w');
      var scaleEl = document.getElementById('slirn-fine-video-scale');
      if (!cropWEl || !scaleEl) return;
      var cropW = parseInt(cropWEl.value, 10);
      if (!cropW || cropW <= 0) return;
      // scale = crop_w / 1920，保留 4 位小数
      var scale = Math.round((cropW / 1920) * 10000) / 10000;
      // clamp 到滑块范围
      var minS = parseFloat(scaleEl.min || '0.1');
      var maxS = parseFloat(scaleEl.max || '2.0');
      scale = Math.max(minS, Math.min(maxS, scale));
      // 同步 slider + number input（保留 4 位小数显示）
      var s = scale.toFixed(4);
      if (scaleEl.value !== s) {
        scaleEl.value = s;
        // 主动派发 input 事件，让 updateVideoDisp 立即刷新视频信息行
        scaleEl.dispatchEvent(new Event('input', { bubbles: true }));
      }
      var scaleNum = document.getElementById('slirn-fine-video-scale_num');
      if (scaleNum && scaleNum.value !== s) scaleNum.value = s;
    }
    document.querySelectorAll('.slirn-fine-slider, .slirn-fine-enabled, [data-font-key], [data-output-key]').forEach(function(el) {
      if (el.dataset.fineBound) return;
      el.dataset.fineBound = '1';
      var ev = (el.type === 'checkbox') ? 'change' : 'input';
      el.addEventListener(ev, function() {
        // REQ-20260919-061a：val 显示区已移除，输入框本身承担数值显示。
        if (el.classList.contains('slirn-fine-slider') || el.classList.contains('slirn-fine-font-slider')) {
          // REQ-20260919-061a v3 用户反馈：拖动滑块时对应 num 框数值要同步更新（之前
          // 只有 ▲▼ 路径同步了两边，slider input 路径漏写）。直接同步 value，不重派发
          // input 事件，避免无限递归；debounceSave 仍由本次事件触发。
          var num = document.getElementById(el.id + '_num');
          if (num && num.value !== el.value) num.value = el.value;
          // crop 矩形 w/h 变化时实时更新比例（仅 layout slider 才有 data-key）
          var k = el.getAttribute('data-key') || '';
          if (k === 'video.crop_w' || k === 'video.crop_h') updateCropAspect();
          // REQ-20260919-062 v7：video.crop_w/crop_h/scale 变化时刷新显示尺寸
          if (k === 'video.crop_w' || k === 'video.crop_h' || k === 'video.scale') {
            updateVideoDisp();
          }
          // REQ-20260919-062 v15：crop_w 变化时自动重算 scale（保留 4 位小数）。
          // scale 的 input 事件会被同 handler 处理 → 触发 updateVideoDisp + debounceSave，
          // 因此无需在这里单独调 fineSaveAll。
          if (k === 'video.crop_w') {
            _recomputeScaleFromCropW();
          }
        }
        debounceSave();
      });
    });
  }

  // ---------- REQ-20260919-061 扩展：全局参数模板 ----------
  function _fineTid() {
    var inner = document.getElementById('slirn-tab-workbench-inner');
    return inner ? (inner.getAttribute('data-task-id') || '') : '';
  }
  function bindFineSteppers() {
    // REQ-20260919-061a v7 用户反馈：num input 旁边的自定义 ▲▼ 按钮已移除（与浏览器
    // 原生 stepper 重复），所以这一步只剩 num 自身的双向同步逻辑：
    //   - number input（用浏览器原生 stepper 微调）→ 按 Enter 或失焦时 clamp + 写回
    //     slider，触发 bindFineControls 的 input 事件走 300ms 防抖自动保存
    function _readMinMaxStep(target) {
      var min = parseFloat(target.min);
      var max = parseFloat(target.max);
      var step = parseFloat(target.step) || 1;
      if (isNaN(min) || isNaN(max)) {
        // 兜底：若 min/max 没写（不应该发生），用 num 的 value 推断
        var cur = parseFloat(target.value) || 0;
        return { min: cur, max: cur, step: 1 };
      }
      return { min: min, max: max, step: step };
    }
    document.querySelectorAll('.slirn-fine-num[data-for]').forEach(function(num) {
      if (num.dataset.numBound) return;
      num.dataset.numBound = '1';
      // REQ-20260919-061a 用户反馈：直接修改输入框，按回车时才调整滑动条位置。
      // 键入过程中不修改 slider，避免 "1." 这种中间态被打断。
      function _commitNum() {
        var slider = document.getElementById(num.dataset.for);
        var src = slider || num;
        var b = _readMinMaxStep(src);
        var v = parseFloat(num.value);
        if (isNaN(v)) { num.value = src.value; return; }
        var clamped = Math.max(b.min, Math.min(b.max, v));
        num.value = String(clamped);
        if (slider && slider.value !== String(clamped)) {
          slider.value = String(clamped);
          // 触发 input 事件 → bindFineControls 走 300ms 防抖自动保存
          slider.dispatchEvent(new Event('input', { bubbles: true }));
        }
      }
      num.addEventListener('keydown', function(e) {
        if (e.key === 'Enter') {
          e.preventDefault();
          _commitNum();
          num.blur();  // 收起光标，让用户看到 slider 已就位
        }
      });
      num.addEventListener('blur', _commitNum);
    });
  }
  function bindCropAspectLink() {
    // REQ-20260919-061a 用户反馈：crop_w 和 crop_h 按 16:9 联动（防变形）。
    // 默认开启；用户可关闭（关后 1:1 等预设才能任意设 w=h）。
    var toggle = document.getElementById('slirn-fine-crop-aspect-link');
    var wEl = document.getElementById('slirn-fine-video-crop_w');
    var hEl = document.getElementById('slirn-fine-video-crop_h');
    var wNum = document.getElementById('slirn-fine-video-crop_w_num');
    var hNum = document.getElementById('slirn-fine-video-crop_h_num');
    if (!toggle || !wEl || !hEl) return;
    if (toggle.dataset.linkBound) return;
    toggle.dataset.linkBound = '1';
    var _syncing = false;
    function _setLinked(otherEl, otherNum, val) {
      // val 由调用方算好；同步写 slider + num（让两边都更新）
      var s = String(val);
      if (otherEl.value !== s) otherEl.value = s;
      if (otherNum && otherNum.value !== s) otherNum.value = s;
    }
    function _syncHfromW() {
      var w = parseFloat(wEl.value) || 0;
      var newH = Math.round(w * 9 / 16);
      _setLinked(hEl, hNum, newH);
    }
    function _syncWfromH() {
      var h = parseFloat(hEl.value) || 0;
      var newW = Math.round(h * 16 / 9);
      _setLinked(wEl, wNum, newW);
    }
    function _propagate(srcEl) {
      // 让被联动的一方也触发 input 事件 → bindFineControls 走防抖自动保存
      // + updateCropAspect 刷新比例显示
      srcEl.dispatchEvent(new Event('input', { bubbles: true }));
    }
    function _wrap(handler) {
      return function() {
        if (_syncing || !toggle.checked) return;
        _syncing = true;
        try { handler(); } finally { _syncing = false; }
      };
    }
    // slider input：拖动 w → 同步 h；拖动 h → 同步 w
    wEl.addEventListener('input', _wrap(function() {
      _syncHfromW(); _propagate(hEl);
    }));
    hEl.addEventListener('input', _wrap(function() {
      _syncWfromH(); _propagate(wEl);
    }));
    // toggle 切换：开 → 立刻按当前 w 重算 h；关 → 不动
    toggle.addEventListener('change', function() {
      if (toggle.checked) {
        _syncing = true;
        try { _syncHfromW(); _propagate(hEl); }
        finally { _syncing = false; }
      }
      // REQ-20260919-062 v18：把勾选状态持久化到 layout.video.crop_aspect_lock
      // （fineSaveAll 会从 [data-key][type=checkbox] 自动收集所有复选框）
      // 用 setTimeout 0 跳出当前 input 事件栈，避免与其他 input 事件循环
      setTimeout(function() { if (typeof fineSaveAll === 'function') fineSaveAll(); }, 0);
    });
  }

  // REQ-20260919-062：背景图白色区域检测
  // - 算法下拉（pixel / ai）+ 阈值下拉 + 阈值手动输入（双向同步）
  // - 检测按钮 → 调 /slirn/api/detect_bg_white_area → 填充 4 角点 + 宽高 + 中心 + 像素数
  // - 「填充到视频位置和裁剪」按钮 → 把结果写回 video.X/Y + video.crop_x/y/w/h
  var _bgDetectLastResult = null;  // 上次检测结果，供 apply 复用
  function bindBgWhiteDetector() {
    var detectBtn = document.querySelector('[data-action="fine-bg-detect"]');
    var applyBtn = document.querySelector('[data-action="fine-bg-detect-apply"]');
    var algoSel = document.getElementById('slirn-fine-bg-detect-algo');
    var thSel = document.getElementById('slirn-fine-bg-detect-threshold-sel');
    var thNum = document.getElementById('slirn-fine-bg-detect-threshold-num');
    var thRow = document.getElementById('slirn-fine-bg-detect-threshold-row');
    if (!detectBtn) return;

    // 算法切换 → 阈值控件仅 pixel 显示；ai/ai_color 隐藏
    function _syncAlgoUi() {
      var algo = (algoSel && algoSel.value) || 'pixel';
      if (thRow) thRow.style.display = (algo === 'pixel') ? '' : 'none';
    }
    if (algoSel) {
      algoSel.addEventListener('change', _syncAlgoUi);
      _syncAlgoUi();
    }

    // 阈值下拉 ↔ 手动输入 双向同步
    if (thSel && thNum) {
      thSel.addEventListener('change', function() {
        thNum.value = thSel.value;
      });
      thNum.addEventListener('input', function() {
        var v = parseInt(thNum.value, 10);
        if (!isNaN(v) && v >= 200 && v <= 255) {
          // 找匹配选项，没有就不动下拉
          var opt = thSel.querySelector('option[value="' + v + '"]');
          if (opt) thSel.value = String(v);
        }
      });
    }

    detectBtn.addEventListener('click', function() {
      var tid = detectBtn.dataset.taskId || _fineTid();
      var algorithm = (algoSel && algoSel.value) || 'ai_color';
      var threshold = (thNum && parseInt(thNum.value, 10)) || 250;
      detectBtn.disabled = true;
      var origText = detectBtn.textContent;
      detectBtn.textContent = '⏳ 检测中…';
      fetch(SLIRN_API + '/detect_bg_white_area', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ task_id: tid, algorithm: algorithm, threshold: threshold }),
      })
      .then(function(r) { return r.json(); })
      .then(function(j) {
        detectBtn.disabled = false;
        detectBtn.textContent = origText;
        if (!j || !j.ok) {
          toast('❌ 检测失败: ' + ((j && j.err) || '未知错误'), 'error');
          return;
        }
        _bgDetectLastResult = j;
        // 填充只读区
        var c = j.corners || {};
        var set = function(id, val) {
          var el = document.getElementById(id);
          if (el) el.textContent = val;
        };
        set('slirn-fine-bg-detect-tl',     '(' + (c.topleft     || [j.x, j.y]).join(', ') + ')');
        set('slirn-fine-bg-detect-tr',     '(' + (c.topright    || [j.x + j.width - 1, j.y]).join(', ') + ')');
        set('slirn-fine-bg-detect-bl',     '(' + (c.bottomleft  || [j.x, j.y + j.height - 1]).join(', ') + ')');
        set('slirn-fine-bg-detect-br',     '(' + (c.bottomright || [j.x + j.width - 1, j.y + j.height - 1]).join(', ') + ')');
        set('slirn-fine-bg-detect-wh',     j.width + ' × ' + j.height);
        set('slirn-fine-bg-detect-center', '(' + j.center_x + ', ' + j.center_y + ')');
        set('slirn-fine-bg-detect-pixels', (j.pixel_count || 0).toLocaleString());
        set('slirn-fine-bg-detect-native', j.image_native_w + ' × ' + j.image_native_h);
        // AI 颜色模式额外显示主色（带色块）
        var colorTextEl = document.getElementById('slirn-fine-bg-detect-color-text');
        var colorSwatchEl = document.getElementById('slirn-fine-bg-detect-color-swatch');
        if (j.detected_color && Array.isArray(j.detected_color) && j.detected_color.length === 3) {
          var rgb = j.detected_color;
          var hex = '#' + rgb.map(function(v) {
            var h = parseInt(v, 10).toString(16);
            return h.length === 1 ? '0' + h : h;
          }).join('');
          if (colorTextEl) colorTextEl.textContent = 'RGB(' + rgb.join(', ') + ') · ' + hex + ' (±' + (j.color_tolerance || 10) + ')';
          if (colorSwatchEl) {
            colorSwatchEl.style.backgroundColor = hex;
            colorSwatchEl.style.display = 'inline-block';
          }
        } else {
          if (colorTextEl) colorTextEl.textContent = '—（仅 pixel / ai_color 算法适用）';
          if (colorSwatchEl) colorSwatchEl.style.backgroundColor = 'transparent';
        }
        var resultEl = document.getElementById('slirn-fine-bg-detect-result');
        if (resultEl) resultEl.hidden = false;
        if (applyBtn) applyBtn.disabled = false;
        var algoLabel = algorithm === 'ai_color' ? '（AI 主色识别）' :
                        algorithm === 'ai'       ? '（AI bbox 识别）' : '（像素扫描）';
        toast('✅ 区域已检测 ' + algoLabel + ' — 左上 (' + j.x + ', ' + j.y + ')，宽 ' + j.width + '，高 ' + j.height, 'success');
      })
      .catch(function(err) {
        detectBtn.disabled = false;
        detectBtn.textContent = origText;
        toast('❌ 网络错误: ' + err, 'error');
      });
    });

    if (applyBtn) {
      applyBtn.addEventListener('click', function() {
        var r = _bgDetectLastResult;
        if (!r) {
          toast('⚠️ 请先点「🔍 检测区域」', 'warning');
          return;
        }
        // 逻辑：检测到的区域是用来展示视频的画布矩形。
        //   - 视频左上角 = 区域左上角 → video.X = r.x, video.Y = r.y
        //   - 区域宽高 = 从原视频截取的宽高 → video.crop_w = r.width, crop_h = r.height
        //   - 从原视频 (0,0) 起取这块矩形 → video.crop_x = 0, crop_y = 0
        //   - 缩放归 1.0，让裁剪后的视频刚好填满区域
        function _setSlider(sliderId, val) {
          var s = document.getElementById(sliderId);
          var n = document.getElementById(sliderId + '_num');
          if (s) {
            s.value = String(val);
            s.dispatchEvent(new Event('input', { bubbles: true }));
          }
          if (n) n.value = String(val);
        }
        _setSlider('slirn-fine-video-x', r.x);
        _setSlider('slirn-fine-video-y', r.y);
        _setSlider('slirn-fine-video-crop_x', 0);
        _setSlider('slirn-fine-video-crop_y', 0);
        _setSlider('slirn-fine-video-crop_w', r.width);
        _setSlider('slirn-fine-video-crop_h', r.height);
        _setSlider('slirn-fine-video-scale', 1.0);

        // 注意：填充只改滑块显示值，不落库、不设 viewport 限定 —
        // 用户手工微调 X/Y 后点「保存全部设置」时按调整值原样保存。
        toast('✅ 已填充：video 位置=(' + r.x + ', ' + r.y + ')，从原视频 (0,0) 截取 ' + r.width + '×' + r.height + '，缩放 1.00 填满区域；可手工调整后保存', 'success');
      });
    }
  }
  function _fineEscapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  // REQ-20260919-062 v5：把值同步到滑块上（不触发 input 事件，避免再触发自动保存死循环）
  function _fineSyncSlider(sliderId, val) {
    var s = document.getElementById(sliderId);
    var n = document.getElementById(sliderId + '_num');
    var cur = s ? parseFloat(s.value) : NaN;
    var target = parseFloat(val);
    if (s && !isNaN(target) && (isNaN(cur) || Math.abs(cur - target) >= 0.001)) {
      s.value = String(target);
    }
    if (n && !isNaN(target)) n.value = String(target);
  }

  // REQ-20260919-063 用户反馈：每个素材都要提供预览功能，并且可以缩放展示素材窗口的尺寸。
  // 实现：
  //   - 委托：[data-action="fine-mat-preview"] → fineMaterialPreview(tid, kind)
  //   - 弹出 .slirn-mat-preview-float 浮层（CSS resize: both；ResizeObserver 持久化尺寸）
  //   - 按 kind 渲染合适的内容：image → <img>；video → <video controls>；
  //     audio → <audio controls>；subtitle (.srt) → fetch 文本按行展示
  function bindFineMaterialPreviews() {
    // 绑定已委托给全局 action handler（见 router.js 主事件循环），
    // 此函数保留为 wb-setup 阶段的占位调用，便于将来扩展按需刷新。
  }

  var _matFloatDragOffset = null;  // 标题栏拖动用
  function fineMaterialPreview(tid, kind) {
    if (!tid || !kind) {
      toast('⚠️ 缺少任务 ID 或素材类型', 'warning');
      return;
    }
    // 已存在的浮层先关掉（同一 kind 再点 = 切内容；不同 kind = 关掉再开新的）
    var existing = document.getElementById('slirn-mat-preview-float');
    if (existing) existing.remove();

    var labels = {
      video: '🎬 粗剪视频',
      subtitle: '📝 字幕文件',
      cover: '🖼 封面图片',
      bg: '🎨 背景图片',
      reference: '🤖 参考位置关系图',
      audio: '🎵 背景音乐',
    };
    var label = labels[kind] || kind;
    var fileUrl = SLIRN_API + '/fine_material_file?task_id=' + encodeURIComponent(tid)
      + '&kind=' + encodeURIComponent(kind);

    // 容器（带 data-* 属性便于将来扩展）
    var flt = document.createElement('div');
    flt.id = 'slirn-mat-preview-float';
    flt.className = 'slirn-mat-preview-float';
    flt.setAttribute('data-kind', kind);
    flt.setAttribute('data-task-id', tid);
    // 默认位置：屏幕右侧偏下（不挡顶部状态栏）
    flt.style.top = '120px';
    flt.style.right = '40px';
    flt.style.left = 'auto';
    // 恢复之前保存的尺寸
    try {
      var saved = JSON.parse(localStorage.getItem('slirnMatPreviewSize') || 'null');
      if (saved && typeof saved.w === 'number' && typeof saved.h === 'number') {
        flt.style.width = saved.w + 'px';
        flt.style.height = saved.h + 'px';
      }
    } catch (err) { /* 静默忽略 */ }

    // 标题栏（可拖动 + 关闭）
    var header = document.createElement('div');
    header.className = 'slirn-mat-preview-header';
    var title = document.createElement('span');
    title.className = 'slirn-mat-preview-title';
    title.textContent = '预览 · ' + label;
    var closeBtn = document.createElement('button');
    closeBtn.className = 'slirn-mat-preview-close';
    closeBtn.setAttribute('aria-label', '关闭预览');
    closeBtn.textContent = '×';
    closeBtn.addEventListener('click', function() {
      var f = document.getElementById('slirn-mat-preview-float');
      if (f) f.remove();
    });
    header.appendChild(title);
    header.appendChild(closeBtn);

    // 内容区
    var body = document.createElement('div');
    body.className = 'slirn-mat-preview-body';

    // 按 kind 渲染
    if (kind === 'subtitle') {
      // SRT 文本：fetch 拿原文，按行展示
      body.innerHTML = '<div class="slirn-mat-preview-error">加载中…</div>';
      fetch(fileUrl)
        .then(function(r) {
          if (!r.ok) throw new Error('HTTP ' + r.status);
          return r.text();
        })
        .then(function(text) {
          body.innerHTML = '';
          var pre = document.createElement('pre');
          // 保留原始换行；前 500 行足够预览
          var lines = text.split(/\r?\n/);
          if (lines.length > 500) {
            lines = lines.slice(0, 500).concat(['...（共 ' + text.split(/\r?\n/).length + ' 行，已截断）']);
          }
          pre.textContent = lines.join('\n');
          body.appendChild(pre);
        })
        .catch(function(err) {
          body.innerHTML = '<div class="slirn-mat-preview-error">❌ 字幕加载失败: ' + _fineEscapeHtml(String(err)) + '</div>';
        });
    } else if (kind === 'video') {
      var v = document.createElement('video');
      v.src = fileUrl;
      v.controls = true;
      v.preload = 'metadata';
      body.appendChild(v);
    } else if (kind === 'audio') {
      var a = document.createElement('audio');
      a.src = fileUrl;
      a.controls = true;
      a.preload = 'metadata';
      body.appendChild(a);
    } else {
      // image 类型（cover/bg/reference）— 用 <img>，自带错误回退
      var img = document.createElement('img');
      img.src = fileUrl;
      img.alt = label;
      img.addEventListener('error', function() {
        body.innerHTML = '<div class="slirn-mat-preview-error">❌ 图片加载失败（文件可能已损坏或被占用）</div>';
      });
      body.appendChild(img);
    }

    flt.appendChild(header);
    flt.appendChild(body);
    document.body.appendChild(flt);

    // 拖动（标题栏 mousedown → mousemove 改 left/top）
    header.addEventListener('mousedown', function(ev) {
      // 仅主键；点击关闭按钮时不要触发拖动
      if (ev.button !== 0) return;
      if (ev.target === closeBtn) return;
      var rect = flt.getBoundingClientRect();
      _matFloatDragOffset = {
        dx: ev.clientX - rect.left,
        dy: ev.clientY - rect.top,
        // 解除 right: auto，改用 left/top 定位
        left: rect.left,
        top: rect.top,
      };
      flt.style.left = rect.left + 'px';
      flt.style.top = rect.top + 'px';
      flt.style.right = 'auto';
      ev.preventDefault();
    });
    document.addEventListener('mousemove', function(ev) {
      if (!_matFloatDragOffset) return;
      var nx = Math.max(0, ev.clientX - _matFloatDragOffset.dx);
      var ny = Math.max(0, ev.clientY - _matFloatDragOffset.dy);
      flt.style.left = nx + 'px';
      flt.style.top = ny + 'px';
    });
    document.addEventListener('mouseup', function() {
      _matFloatDragOffset = null;
    });

    // ResizeObserver：用户拖右下角调尺寸 → 持久化到 localStorage
    if (typeof ResizeObserver !== 'undefined') {
      var ro = new ResizeObserver(function(entries) {
        for (var i = 0; i < entries.length; i++) {
          var cr = entries[i].contentRect;
          try {
            localStorage.setItem('slirnMatPreviewSize', JSON.stringify({
              w: Math.round(cr.width),
              h: Math.round(cr.height),
            }));
          } catch (err) { /* 静默 */ }
        }
      });
      ro.observe(flt);
    }
  }

  // REQ-20260919-062 v11：生成预览/导出完成后，自动弹出合成预览窗口。
  // 复用 .slirn-mat-preview-float 样式（位置 + resize 行为一致），但 body
  // 直接放 <video src={url}>（不需要再 fetch /slirn/api/fine_material_file）。
  // v19 用户反馈：合成预览窗口无法拖动 + 拖动后位置不持久化。
  //   根因：openFinePreviewFloat 只绑了 mousedown，没绑 mousemove/mouseup，导致
  //   鼠标移动时浮窗纹丝不动。同时复用单例 _matFloatDragOffset 还会被
  //   fineMaterialPreview 的 mousemove handler 错位更新（闭包里的 flt 已 remove）。
  //   修复：把 _dragOffset 提到本函数闭包里（每个浮窗独立），完整 mousedown/move/up
  //   三件套，并在 mouseup 时把 left/top 持久化到 localStorage 的 slirnMatPreviewPos。
  function openFinePreviewFloat(url, title) {
    if (!url) return;
    var labels_title = title || '🎬 合成预览';
    // 已存在的浮层先关掉（避免叠加）
    var existing = document.getElementById('slirn-fine-preview-float');
    if (existing) existing.remove();
    var flt = document.createElement('div');
    flt.id = 'slirn-fine-preview-float';
    flt.className = 'slirn-mat-preview-float slirn-fine-preview-float';
    // 默认位置 + 尺寸（与单素材预览一致）
    flt.style.top = '120px';
    flt.style.right = '40px';
    flt.style.left = 'auto';
    try {
      var savedSize = JSON.parse(localStorage.getItem('slirnMatPreviewSize') || 'null');
      if (savedSize && savedSize.w && savedSize.h) {
        flt.style.width = savedSize.w + 'px';
        flt.style.height = savedSize.h + 'px';
      }
      // v19：恢复上次拖到的位置（left/top）
      var savedPos = JSON.parse(localStorage.getItem('slirnMatPreviewPos') || 'null');
      if (savedPos && typeof savedPos.left === 'number' && typeof savedPos.top === 'number') {
        flt.style.left = savedPos.left + 'px';
        flt.style.top = savedPos.top + 'px';
        flt.style.right = 'auto';
      }
    } catch (err) { /* 静默 */ }
    // 标题栏
    var header = document.createElement('div');
    header.className = 'slirn-mat-preview-header';
    var titleEl = document.createElement('span');
    titleEl.className = 'slirn-mat-preview-title';
    titleEl.textContent = labels_title;
    var closeBtn = document.createElement('button');
    closeBtn.className = 'slirn-mat-preview-close';
    closeBtn.setAttribute('aria-label', '关闭预览');
    closeBtn.textContent = '×';
    closeBtn.addEventListener('click', function() {
      var f = document.getElementById('slirn-fine-preview-float');
      if (f) f.remove();
    });
    header.appendChild(titleEl);
    header.appendChild(closeBtn);
    // body：放一个 <video controls autoplay src={url}>；video src 需要绝对 URL
    var body = document.createElement('div');
    body.className = 'slirn-mat-preview-body';
    var v = document.createElement('video');
    v.src = url;
    v.controls = true;
    v.autoplay = true;
    v.preload = 'metadata';
    v.style.width = '100%';
    v.style.height = '100%';
    body.appendChild(v);
    flt.appendChild(header);
    flt.appendChild(body);
    document.body.appendChild(flt);
    // v19：完整拖动三件套（mousedown / mousemove / mouseup）。
    // _dragOffset 放在闭包里，避免复用模块级 _matFloatDragOffset 被其它浮窗覆盖。
    var _dragOffset = null;
    header.addEventListener('mousedown', function(ev) {
      if (ev.button !== 0) return;
      if (ev.target === closeBtn) return;
      var rect = flt.getBoundingClientRect();
      _dragOffset = {
        dx: ev.clientX - rect.left,
        dy: ev.clientY - rect.top,
      };
      flt.style.left = rect.left + 'px';
      flt.style.top = rect.top + 'px';
      flt.style.right = 'auto';
      ev.preventDefault();
    });
    document.addEventListener('mousemove', function(ev) {
      if (!_dragOffset) return;
      var nx = Math.max(0, ev.clientX - _dragOffset.dx);
      var ny = Math.max(0, ev.clientY - _dragOffset.dy);
      flt.style.left = nx + 'px';
      flt.style.top = ny + 'px';
    });
    document.addEventListener('mouseup', function() {
      if (!_dragOffset) return;
      _dragOffset = null;
      // 持久化位置（mouseup 时记录当前 flt 的 left/top）
      try {
        var rect = flt.getBoundingClientRect();
        localStorage.setItem('slirnMatPreviewPos', JSON.stringify({
          left: Math.round(rect.left),
          top: Math.round(rect.top),
        }));
      } catch (err) { /* 静默 */ }
    });
    // 尺寸持久化（复用 slirnMatPreviewSize key）
    if (typeof ResizeObserver !== 'undefined') {
      var ro = new ResizeObserver(function(entries) {
        for (var i = 0; i < entries.length; i++) {
          var cr = entries[i].contentRect;
          try {
            localStorage.setItem('slirnMatPreviewSize', JSON.stringify({
              w: Math.round(cr.width),
              h: Math.round(cr.height),
            }));
          } catch (err) { /* 静默 */ }
        }
      });
      ro.observe(flt);
    }
  }

  // 由 export_fine_video 端点启动后台线程后，前端调 startFineExportInline：
  //   1. 把按钮变为「⏳ 导出中…」disabled
  //   2. 按钮右侧 #slirn-fine-export-status 显示状态文字 + 迷你进度条
  //   3. 1.5s 轮询 /slirn/api/render_status 更新进度
  //   4. 点击 status 元素本身 → 取消/下载/查看错误（三态对称交互）
  // 不弹任何模态框，不影响用户操作其他工作。
  function startFineExportInline(tid, jobId, btnEl) {
    var statusEl = document.getElementById('slirn-fine-export-status');
    if (!statusEl) return;
    if (!btnEl) btnEl = document.getElementById('slirn-fine-export-btn');

    setExportBtnState(btnEl, 'running');
    setExportInlineState(statusEl, 'running', 0, 0, 0, 0);

    // REQ-20260920-089：导出进行中显示独立取消按钮
    var cancelBtn = document.getElementById('slirn-fine-export-cancel-btn');
    if (cancelBtn) {
      cancelBtn.style.display = '';
      cancelBtn.setAttribute('data-state', 'running');
      cancelBtn.setAttribute('data-job-id', jobId);
      cancelBtn.disabled = false;
    }

    var _stateLabel = { queued: '排队中', running: '渲染中', done: '已完成', failed: '失败', cancelled: '已取消' };
    var _timer = null;

    // REQ-20260920-089：终态隐藏取消按钮
    var _hideCancelBtn = function() {
      var cb = document.getElementById('slirn-fine-export-cancel-btn');
      if (cb) {
        cb.style.display = 'none';
        cb.setAttribute('data-state', 'idle');
        cb.disabled = false;
      }
    };

    var _poll = function() {
      fetch('/slirn/api/render_status?job_id=' + encodeURIComponent(jobId))
        .then(function(r) { return r.json(); })
        .then(function(s) {
          if (!s.ok) {
            setExportInlineState(statusEl, 'failed', 0, 0, 0, 0);
            clearInterval(_timer); _timer = null;
            setExportBtnState(btnEl, 'failed');
            toast('❌ ' + (s.error || '查询失败'));
            return;
          }
          var st = s.state;
          // 映射后端 state 到 inline 状态元素
          var _map = { running: 'running', done: 'done', failed: 'failed', cancelled: 'idle', queued: 'running' };
          setExportInlineState(
            statusEl,
            _map[st] || 'running',
            s.progress_pct, s.elapsed_sec, s.eta_sec, s.speed_x,
            _stateLabel[st] || st
          );

          if (st === 'done') {
            clearInterval(_timer); _timer = null;
            setExportBtnState(btnEl, 'done', s.output_url);
            statusEl.setAttribute('data-output-url', s.output_url || '');
            _hideCancelBtn();
            toast('✅ 导出完成');
          } else if (st === 'failed') {
            clearInterval(_timer); _timer = null;
            setExportBtnState(btnEl, 'failed', null, s.error);
            statusEl.setAttribute('data-error', s.error || '未知错误');
            _hideCancelBtn();
          } else if (st === 'cancelled') {
            clearInterval(_timer); _timer = null;
            setExportBtnState(btnEl, 'idle');
            _hideCancelBtn();
          }
        })
        .catch(function(e) {
          // 网络抖动不立即报错，下一轮再试
          console.warn('[render_status]', e);
        });
    };
    _poll();
    _timer = setInterval(_poll, 1500);

    // 点击 status 元素本身 → 取消（running）或下载（done）或查看错误（failed）
    statusEl.onclick = function() {
      var st = statusEl.getAttribute('data-state');
      if (st === 'running' || st === 'cancelling') {
        if (!confirm('确认取消当前渲染？已生成的片段会被丢弃。')) return;
        setExportInlineState(statusEl, 'cancelling', null);
        fetch('/slirn/api/cancel_render', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ job_id: jobId })
        })
          .then(function(r) { return r.json(); })
          .then(function(j) {
            if (!j.ok) toast('❌ ' + (j.error || '取消失败'));
            else toast('⏹ 已发送取消信号');
          })
          .catch(function(e) { toast('❌ 网络错误: ' + e.message); });
      } else if (st === 'done') {
        var url = statusEl.getAttribute('data-output-url');
        if (url) window.open(url, '_blank');
      } else if (st === 'failed') {
        toast('❌ 渲染失败: ' + (statusEl.getAttribute('data-error') || '未知错误'));
      }
    };

    // 页面卸载时清理 interval（避免泄漏；job 后台继续跑，5min TTL 过期自动清理）
    var _unload = function() { if (_timer) { clearInterval(_timer); _timer = null; } };
    window.addEventListener('beforeunload', _unload);
    window.addEventListener('pagehide', _unload);
  }

  // REQ-20260920-077：更新 inline 状态元素的内容 + 进度条宽度。
  // state: 'idle' | 'running' | 'done' | 'failed' | 'cancelling'
  function setExportInlineState(el, state, pct, elapsed, eta, speed, label) {
    el.setAttribute('data-state', state);
    if (state === 'idle') {
      el.hidden = true;
      el.innerHTML = '';
      return;
    }
    el.hidden = false;
    var pctStr = (pct != null && !isNaN(pct)) ? pct.toFixed(1) + '%' : '';
    var elapsedStr = (elapsed != null && !isNaN(elapsed)) ? _fmtSec(elapsed) : '';
    var etaStr = (eta != null && eta >= 0) ? '剩 ≈ ' + _fmtSec(eta) : '';
    var speedStr = (speed != null && speed > 0) ? '×' + speed.toFixed(2) : '';
    if (state === 'done') {
      el.innerHTML = '✅ 已完成 · 下载'
        + (elapsedStr ? ' <span style="opacity:.65">· ' + elapsedStr + '</span>' : '');
    } else if (state === 'failed') {
      el.innerHTML = '❌ 失败 · 查看';
    } else if (state === 'cancelling') {
      el.innerHTML = '⏹ 取消中…';
    } else {
      // running
      el.innerHTML = '⏳ ' + (label || '渲染中')
        + ' <span class="slirn-fine-export-track">'
        +   '<span class="slirn-fine-export-bar" style="width:' + pctStr + '"></span>'
        + '</span> '
        + pctStr
        + (elapsedStr ? ' · 已用 ' + elapsedStr : '')
        + (etaStr ? ' · ' + etaStr : '')
        + (speedStr ? ' · ' + speedStr : '');
    }
  }

  // REQ-20260920-077：更新按钮的文本 + disabled + 点击行为。
  // state: 'idle' | 'running' | 'done' | 'failed' | 'cancelling'
  function setExportBtnState(btn, state, outputUrl, error) {
    if (!btn) return;
    btn.removeAttribute('data-state');
    if (state === 'idle') {
      btn.disabled = false;
      btn.textContent = '💾 导出最终视频';
      btn.onclick = null;  // 复用原 action handler 委托
      btn.removeAttribute('data-output-url');
      btn.removeAttribute('data-error');
    } else if (state === 'running' || state === 'cancelling') {
      btn.disabled = true;
      btn.textContent = state === 'cancelling' ? '⏹ 取消中…' : '⏳ 导出中…';
      btn.onclick = null;
    } else if (state === 'done') {
      // REQ-20260921-NNN 用户反馈：导出完成后没法重新导出（点按钮只下载）。
      //   改：按钮文本「🔄 重新导出」→ 不挂 onclick，让 action 委托 handler
      //   重新触发 fine-export 流程。下载入口改成点右侧的 status 元素（✅ 已完成 · 下载）。
      btn.disabled = false;
      btn.textContent = '🔄 重新导出';
      btn.removeAttribute('data-output-url');
      btn.removeAttribute('data-error');
      btn.onclick = null;  // 复用原 action handler 委托 → 重新走 export_fine_video
    } else if (state === 'failed') {
      btn.disabled = false;
      btn.textContent = '❌ 失败 · 重试';
      btn.setAttribute('data-error', error || '未知');
      btn.onclick = null;  // 复用原 action handler 自动重试
    }
  }

  function _fmtMs(ms) {
    if (!ms || ms <= 0) return '00:00:00';
    var s = Math.floor(ms / 1000);
    return _fmtSec(s);
  }
  function _fmtSec(sec) {
    if (sec == null || isNaN(sec) || sec < 0) return '00:00:00';
    var s = Math.floor(sec);
    var h = Math.floor(s / 3600);
    var m = Math.floor((s % 3600) / 60);
    var ss = s % 60;
    function pad(n) { return n < 10 ? '0' + n : '' + n; }
    return pad(h) + ':' + pad(m) + ':' + pad(ss);
  }

  function fineImportShow() {
    // REQ-20260919-061 用户反馈：点「📥 引用参数」→ 弹出 modal 列出已保存模板。
    // 每次打开都重新拉一次（模板可能已被其他任务/用户改动过）。
    var overlay = document.getElementById('slirn-fine-import-overlay');
    var list = document.getElementById('slirn-fine-import-list');
    if (!overlay || !list) return;
    overlay.hidden = false;
    list.innerHTML = '<div class="slirn-fine-profile-empty">加载中…</div>';
    fetch('/slirn/api/list_fine_global_profiles', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({})
    })
      .then(function(r) { return r.json(); })
      .then(function(j) {
        var profiles = (j && j.ok && j.profiles) ? j.profiles : [];
        if (!profiles.length) {
          list.innerHTML =
            '<div class="slirn-fine-profile-empty">暂无模板 — 在顶部「模板名」输入框填名字，点「💾 保存参数为模板」即可创建</div>';
          return;
        }
        list.innerHTML = profiles.map(function(p) {
          var ts = (p.saved_at || '').replace('T', ' ').slice(0, 16);
          return (
            '<div class="slirn-fine-import-row" data-profile-id="' + _fineEscapeHtml(p.id) + '">' +
              '<div class="slirn-fine-import-info">' +
                '<span class="slirn-fine-import-name">' + _fineEscapeHtml(p.name) + '</span>' +
                '<span class="slirn-fine-import-time">' + _fineEscapeHtml(ts) + '</span>' +
              '</div>' +
              '<div class="slirn-fine-import-ops">' +
                '<button class="slirn-btn slirn-btn-xs slirn-btn-primary" ' +
                  'data-action="fine-import-apply" data-profile-id="' + _fineEscapeHtml(p.id) + '">' +
                  '📥 应用</button>' +
                '<button class="slirn-btn slirn-btn-xs" ' +
                  'data-action="fine-import-export" data-profile-id="' + _fineEscapeHtml(p.id) + '" ' +
                  'title="下载该模板的参数为 JSON 文件（与精剪阶段「📤 导出参数」同口径）">' +
                  '📤 导出</button>' +
                '<button class="slirn-btn slirn-btn-xs" ' +
                  'data-action="fine-import-rename" data-profile-id="' + _fineEscapeHtml(p.id) + '">' +
                  '✏️ 改名</button>' +
                '<button class="slirn-btn slirn-btn-xs slirn-btn-danger" ' +
                  'data-action="fine-import-delete" data-profile-id="' + _fineEscapeHtml(p.id) + '">' +
                  '🗑 删除</button>' +
              '</div>' +
            '</div>'
          );
        }).join('');
      })
      .catch(function(e) {
        list.innerHTML =
          '<div class="slirn-fine-profile-empty">❌ 加载失败: ' + _fineEscapeHtml(e.message) + '</div>';
      });
  }
  function fineImportClose() {
    var overlay = document.getElementById('slirn-fine-import-overlay');
    if (overlay) overlay.hidden = true;
  }
  function fineProfileList() {
    // 拉全局模板列表 → 渲染到 #slirn-fine-profile-list
    fetch('/slirn/api/list_fine_global_profiles', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({})
    })
      .then(function(r) { return r.json(); })
      .then(function(j) {
        var box = document.getElementById('slirn-fine-profile-list');
        if (!box) return;
        var profiles = (j && j.ok && j.profiles) ? j.profiles : [];
        if (!profiles.length) {
          box.innerHTML = '<div class="slirn-fine-profile-empty">暂无模板 — 调好参数后在上面输入名保存</div>';
          return;
        }
        box.innerHTML = profiles.map(function(p) {
          var ts = (p.saved_at || '').replace('T', ' ').slice(0, 16);
          return (
            '<div class="slirn-fine-profile-row" data-profile-id="' + _fineEscapeHtml(p.id) + '">' +
              '<div class="slirn-fine-profile-info">' +
                '<span class="slirn-fine-profile-name">' + _fineEscapeHtml(p.name) + '</span>' +
                '<span class="slirn-fine-profile-time">' + _fineEscapeHtml(ts) + '</span>' +
              '</div>' +
              '<div class="slirn-fine-profile-ops">' +
                '<button class="slirn-btn slirn-btn-xs slirn-btn-primary" ' +
                  'data-action="fine-profile-apply" data-profile-id="' + _fineEscapeHtml(p.id) + '">' +
                  '📥 应用</button>' +
                '<button class="slirn-btn slirn-btn-xs" ' +
                  'data-action="fine-profile-rename" data-profile-id="' + _fineEscapeHtml(p.id) + '">' +
                  '✏️ 改名</button>' +
                '<button class="slirn-btn slirn-btn-xs" ' +
                  'data-action="fine-profile-delete" data-profile-id="' + _fineEscapeHtml(p.id) + '">' +
                  '🗑 删除</button>' +
              '</div>' +
            '</div>'
          );
        }).join('');
      })
      .catch(function(e) {
        toast('❌ 加载模板列表失败: ' + e.message, 'error');
      });
  }
  function fineProfileSave() {
    var tid = _fineTid();
    if (!tid) { toast('❌ 缺少 task_id', 'error'); return; }
    var inp = document.getElementById('slirn-fine-profile-name');
    var name = inp ? inp.value.trim() : '';
    if (!name) { toast('❌ 请先填写模板名', 'error'); return; }
    fetch('/slirn/api/save_fine_global_profile', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task_id: tid, name: name })
    })
      .then(function(r) { return r.json(); })
      .then(function(j) {
        if (j && j.ok) {
          toast(j.toast || '✅ 已保存');
          if (inp) inp.value = '';
          fineProfileList();
        } else {
          toast('❌ ' + ((j && j.error) || '保存失败'), 'error');
        }
      })
      .catch(function(e) {
        toast('❌ 网络错误: ' + e.message, 'error');
      });
  }
  function fineProfileApply(profileId, profileName) {
    var tid = _fineTid();
    if (!tid) { toast('❌ 缺少 task_id', 'error'); return; }
    // 弹窗确认覆盖
    slirnConfirm(
      '📥 应用模板「' + profileName + '」',
      '将覆盖当前任务的「位置/裁剪/字体/输出/音频」参数。\n素材文件（视频/封面/字幕/BGM）不受影响。\n\n确定继续？',
      function() {
        fetch('/slirn/api/apply_fine_global_profile', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ task_id: tid, profile_id: profileId })
        })
          .then(function(r) { return r.json(); })
          .then(function(j) {
            if (j && j.ok) {
              toast(j.toast || '✅ 已应用模板');
              // 替换 wb HTML → 所有控件（滑块/勾选/值显示）刷新
              var inner = document.getElementById('slirn-tab-workbench-inner');
              if (inner && j.html) {
                // 用临时容器解析新 HTML
                var tmp = document.createElement('div');
                tmp.innerHTML = j.html;
                var fresh = tmp.querySelector('#slirn-tab-workbench-inner');
                if (fresh) inner.innerHTML = fresh.innerHTML;
                // 重新绑 fine_cut 控件
                if (typeof bindFineControls === 'function') bindFineControls();
                if (typeof bindFineSteppers === 'function') bindFineSteppers();
                // 引用参数 modal 应用后自动关闭（避免残留旧状态）
                if (typeof fineImportClose === 'function') fineImportClose();
              }
            } else {
              toast('❌ ' + ((j && j.error) || '应用失败'), 'error');
            }
          })
          .catch(function(e) {
            toast('❌ 网络错误: ' + e.message, 'error');
          });
      }
    );
  }
  function fineProfileDelete(profileId) {
    if (!window.confirm('确定删除该模板？\n（不影响已应用此模板的任务）')) return;
    fetch('/slirn/api/delete_fine_global_profile', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ profile_id: profileId })
    })
      .then(function(r) { return r.json(); })
      .then(function(j) {
        if (j && j.ok) {
          toast(j.toast || '🗑 已删除');
          fineProfileList();
        } else {
          toast('❌ ' + ((j && j.error) || '删除失败'), 'error');
        }
      })
      .catch(function(e) {
        toast('❌ 网络错误: ' + e.message, 'error');
      });
  }
  function fineProfileRename(profileId, currentName) {
    var newName = window.prompt('新模板名（当前：「' + currentName + '」）', currentName);
    if (!newName || !newName.trim()) return;
    newName = newName.trim();
    if (newName === currentName) return;
    fetch('/slirn/api/rename_fine_global_profile', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ profile_id: profileId, name: newName })
    })
      .then(function(r) { return r.json(); })
      .then(function(j) {
        if (j && j.ok) {
          toast(j.toast || '✏️ 已重命名');
          fineProfileList();
        } else {
          toast('❌ ' + ((j && j.error) || '改名失败'), 'error');
        }
      })
      .catch(function(e) {
        toast('❌ 网络错误: ' + e.message, 'error');
      });
  }
  // 最小化确认弹窗：复用 .slirn-modal-overlay 样式
  function slirnConfirm(title, message, onOk) {
    var existing = document.getElementById('slirn-confirm-overlay');
    if (existing) existing.remove();
    var overlay = document.createElement('div');
    overlay.className = 'slirn-modal-overlay';
    overlay.id = 'slirn-confirm-overlay';
    overlay.innerHTML =
      '<div class="slirn-modal-card">' +
        '<div class="slirn-modal-title">' + _fineEscapeHtml(title) + '</div>' +
        '<div class="slirn-modal-filename">' + _fineEscapeHtml(message).replace(/\n/g, '<br>') + '</div>' +
        '<div style="display:flex; gap:10px; justify-content:center;">' +
          '<button class="slirn-btn" id="slirn-confirm-cancel">❌ 取消</button>' +
          '<button class="slirn-btn slirn-btn-primary" id="slirn-confirm-ok">✅ 确认覆盖</button>' +
        '</div>' +
      '</div>';
    document.body.appendChild(overlay);
    function close() { overlay.remove(); }
    document.getElementById('slirn-confirm-cancel').onclick = close;
    document.getElementById('slirn-confirm-ok').onclick = function() {
      close();
      if (typeof onOk === 'function') onOk();
    };
    overlay.addEventListener('click', function(e) {
      if (e.target === overlay) close();  // 点遮罩关闭
    });
  }

  // REQ-20260920-088：素材路径详情弹窗（完整路径 + 来源色块 + 文件元数据）
  function fineMatDetail(tid, kind) {
    if (!tid || !kind) {
      toast('⚠️ 缺少任务 ID 或素材类型', 'warning');
      return;
    }
    postJSON(SLIRN_API + '/material_info', {task_id: tid, kind: kind})
      .then(function(r) {
        if (!r || !r.ok) {
          toast('❌ 查询失败: ' + ((r && r.error) || '未知错误'), 'error');
          return;
        }
        _renderMatDetailModal(r);
        var modal = document.getElementById('slirn-mat-detail-modal');
        if (modal) modal.hidden = false;
      })
      .catch(function(e) {
        toast('❌ 网络错误: ' + (e && e.message ? e.message : e), 'error');
      });
  }

  function _renderMatDetailModal(info) {
    var body = document.getElementById('slirn-mat-detail-body');
    if (!body) return;
    var kindLabel = ({
      video: '🎬 视频',
      subtitle: '📝 字幕',
      cover: '🖼 封面',
      bg: '🎨 背景',
      reference: '🤖 参考',
      audio: '🎵 音频'
    })[info.kind] || info.kind;
    var sourceClass = info.source === 'auto' ? 'mat-source-auto'
      : info.source === 'upload' ? 'mat-source-upload'
      : info.source === 'default_bgm' ? 'mat-source-default'
      : 'mat-source-none';
    var sourceLabel = info.source_label || '未配置';
    var sizeStr;
    if (info.exists) {
      var mb = info.size_bytes / 1024 / 1024;
      if (mb >= 1) {
        sizeStr = mb.toFixed(2) + ' MB';
      } else {
        sizeStr = (info.size_bytes / 1024).toFixed(1) + ' KB';
      }
    } else {
      sizeStr = '—';
    }
    var html = '';
    // 顶部：类型 + 来源色块
    html += '<div class="slirn-mat-detail-header">';
    html += '<span class="slirn-mat-detail-kind">' + escapeHtml(kindLabel) + '</span>';
    html += '<span class="slirn-mat-detail-source ' + sourceClass + '">' + escapeHtml(sourceLabel) + '</span>';
    html += '</div>';
    // 表格
    html += '<table class="slirn-mat-detail-table">';
    html += '<tr><th>📁 物理路径</th><td>' +
      (info.exists
        ? escapeHtml(info.abs_path || '')
        : '<span class="slirn-warn">⚠️ 文件不存在</span>') +
      '</td></tr>';
    html += '<tr><th>🔗 fc.materials.path</th><td>' + escapeHtml(info.fc_path || '—') + '</td></tr>';
    html += '<tr><th>📊 文件大小</th><td>' + escapeHtml(sizeStr) + '</td></tr>';
    html += '<tr><th>🕒 最后修改</th><td>' + escapeHtml(info.mtime || '—') + '</td></tr>';
    html += '<tr><th>🔖 source 字段</th><td>' + escapeHtml(info.source || 'none') + '</td></tr>';
    html += '<tr><th>🎬 类型</th><td>' + escapeHtml(info.type || '—') + '</td></tr>';
    if (info.source === 'auto') {
      var upLabel = info.upstream_name || '—';
      var upExistsHtml = info.upstream_exists
        ? '<span class="slirn-ok">✅ 存在</span>'
        : '<span class="slirn-warn">⚠️ 已不存在</span>';
      html += '<tr><th>📥 上游产物</th><td>' + escapeHtml(upLabel) + ' ' + upExistsHtml + '</td></tr>';
    }
    html += '</table>';
    // 上游产物已删除时给个「重新自动获取」按钮
    if (info.source === 'auto' && !info.upstream_exists) {
      html += '<div class="slirn-mat-detail-actions">';
      html += '<button class="slirn-btn slirn-btn-primary" data-action="fine-source-auto" data-kind="' +
        escapeAttr(info.kind) + '">📥 重新自动获取</button>';
      html += '</div>';
    }
    body.innerHTML = html;
  }

  // REQ-20260920-088：模态框点击遮罩关闭 + ESC 关闭（独立绑定，避免与其它 modal 冲突）
  document.addEventListener('click', function(e) {
    var overlay = e.target;
    if (overlay && overlay.id === 'slirn-mat-detail-modal') {
      overlay.hidden = true;
    }
  });
  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
      var m = document.getElementById('slirn-mat-detail-modal');
      if (m && !m.hidden) m.hidden = true;
    }
  });

  function fineSourceAuto(btn, kind) {
    // REQ-20260919-061：把素材来源切到「自动获取上游产物」。
    // 设计：两个按钮常驻（手动上传 / 自动获取），点哪个就用哪个，无需切换按钮组。
    var inner = document.getElementById('slirn-tab-workbench-inner');
    var tid = inner ? (inner.getAttribute('data-task-id') || '') : '';
    if (!tid) { toast('❌ 缺少 task_id'); return; }
    btn.disabled = true;
    var oldText = btn.textContent;
    btn.textContent = '📥 获取中...';
    fetch('/slirn/api/auto_pick_upstream_material', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task_id: tid, kind: kind })
    })
      .then(function(r) { return r.json(); })
      .then(function(j) {
        btn.disabled = false; btn.textContent = oldText;
        if (j.ok) {
          // 局部刷新（不做整页/工作台刷新）：用服务端重渲染的单卡换掉对应
          // 素材卡（徽章/状态行/预览/详情按钮的 disabled 一次性到位）。
          // 点击事件全部委托 data-action，换卡后无需重绑。
          var card = document.querySelector('.slirn-fine-upload-card[data-kind="' + kind + '"]');
          if (card && j.card_html) {
            card.outerHTML = j.card_html;
          }
          // 视频素材就绪 → 解除卡外「生成预览/导出」主按钮
          if (kind === 'video') fineEnableOutputBtns();
          // 从素材详情弹窗里的「重新自动获取」进入时，弹窗信息已过期 → 关掉
          var modal = document.getElementById('slirn-mat-detail-modal');
          if (modal && !modal.hidden) modal.hidden = true;
          toast('✅ ' + (j.toast || '已自动从上游获取'));
        } else {
          toast('❌ ' + (j.error || '自动获取失败'));
        }
      }).catch(function(e) {
        btn.disabled = false; btn.textContent = oldText;
        toast('❌ 网络错误: ' + e.message);
      });
  }
  // （移除 fineSourceManual：上传按钮始终可用，无需切换）
  function fineCropPreset(btn, preset) {
    // REQ-20260919-061 用户补充：crop_* 用设计空间 1920×1080 像素。
    // 16:9 居中：在 1920×1080 设计空间裁出 1920×1080 矩形 = 全幅；这里改为裁
    // 一个 1920×1080 矩形居中（即全幅），但保留 UI 让用户直观看到比例。
    // 1:1 居中：从源视频居中裁一个 1080×1080 正方形（按设计空间最短边）。
    var defaults = {
      // 全幅：crop 矩形 = 整个 1920×1080（实际源视频全幅）
      'full': { crop_x: 0,    crop_y: 0,    crop_w: 1920, crop_h: 1080 },
      // 16:9 居中：矩形 = 整个 1920×1080（全幅本身就是 16:9，无需裁）
      '16x9': { crop_x: 0,    crop_y: 0,    crop_w: 1920, crop_h: 1080 },
      // 1:1 居中：边长 = 设计空间短边 1080，居中 → (540, 0, 1080, 1080)
      '1x1':  { crop_x: 420,  crop_y: 0,    crop_w: 1080, crop_h: 1080 },
    }[preset] || null;
    if (!defaults) return;
    ['crop_x', 'crop_y', 'crop_w', 'crop_h'].forEach(function(k) {
      var slider = document.getElementById('slirn-fine-video-' + k);
      if (slider) {
        slider.value = defaults[k];
      }
    });
    // 更新比例显示
    var aspectEl = document.getElementById('slirn-fine-crop-aspect-val');
    if (aspectEl && defaults.crop_w > 0) {
      aspectEl.textContent = (defaults.crop_h / defaults.crop_w).toFixed(3);
    }
    // 触发保存
    fineSaveAll();
    toast('✅ 已应用「' + (preset === 'full' ? '全幅' : preset === '16x9' ? '16:9 居中' : '1:1 居中') + '」');
  }

  // REQ-20260920-078：系统默认 BGM 列表 + 选择（一键选 5 个 lo-fi mp3 之一）。
  // 复用现有 fineSaveAll 自动保存机制（不弹 toast）。
  // REQ-20260920-082：暴露到 window 让 pipeline.js loadPanel 完成后调用。
  var _defaultBgmsCache = null;
  window.fineDefaultBgmLoad = async function fineDefaultBgmLoad() {
    var sel = document.getElementById('slirn-fine-default-bgm');
    if (!sel) return;
    if (sel.dataset.loaded === '1') return;
    try {
      var r = await fetch('/slirn/api/list_default_bgms', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: '{}',
      });
      var j = await r.json();
      if (!j.ok) return;
      // REQ-20260921-NNN：只列「文件确实存在」的可用 BGM（available=true）。
      // 缺失的不显示（避免选了之后 select_default_bgm 返回「文件不存在」错误）。
      _defaultBgmsCache = (j.bgms || []).filter(function(b) {
        return !!b.available;
      });
      var _allBgms = j.bgms || [];
      var _missingCount = _allBgms.length - _defaultBgmsCache.length;
      // 清空已有 options（保留「— 不选 —」）
      while (sel.options.length > 1) sel.remove(1);
      if (_defaultBgmsCache.length === 0) {
        // 一个可用的都没有 → 显示提示 option
        var opt0 = document.createElement('option');
        opt0.value = '';
        opt0.disabled = true;
        opt0.textContent = '（系统暂无可用 BGM，请上传自定义）';
        sel.appendChild(opt0);
      } else {
        _defaultBgmsCache.forEach(function(b) {
          var opt = document.createElement('option');
          opt.value = b.id;
          var mb = (b.size_bytes / 1048576).toFixed(1);
          opt.textContent = '🎵 ' + b.name + '（' + mb + ' MB）';
          sel.appendChild(opt);
        });
      }
      sel.dataset.loaded = '1';
      // 同步当前 fc.materials.audio.path → 对应 ID
      fineDefaultBgmSyncFromFc();
    } catch (e) {
      console.warn('[list_default_bgms]', e);
    }
  }

  function fineDefaultBgmSyncFromFc() {
    var sel = document.getElementById('slirn-fine-default-bgm');
    if (!sel) return;
    // 从页面状态推断：当前 audio 路径里若含 "<bgm_id>.mp3" 则选中
    var matPath = '';
    // 通过现有 audio 控件的 data-* 推断（materials.audio.path 由 save 时收集）
    // 简化：直接对比 _lastSavedAudioPath（保存回调里写入）
    if (window._slirnFineAudioPath) {
      var fname = String(window._slirnFineAudioPath).split('/').pop() || '';
      var match = _defaultBgmsCache && _defaultBgmsCache.find(function(b) {
        return fname === b.id + '.mp3';
      });
      sel.value = match ? match.id : '';
    } else if (sel.getAttribute('data-current-bgm')) {
      // REQ-20261028-NNN：刚加载页面（本会话还没选过 BGM，_slirnFineAudioPath
      // 未设）→ 用服务端渲染的当前标记回显，刷新后选中态不丢。
      sel.value = sel.getAttribute('data-current-bgm');
    }
  }

  async function fineDefaultBgmSelect(bgmId, tid) {
    if (!bgmId) {
      // 选「— 不选（清空）—」：不动 fc.materials.audio（保留已上传 BGM）；
      // 只清掉自动启用标记，不强制取消
      return;
    }
    try {
      var r = await fetch('/slirn/api/select_default_bgm', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({task_id: tid, bgm_id: bgmId}),
      });
      var j = await r.json();
      if (!j.ok) { toast('❌ ' + (j.error || '选择失败')); return; }
      toast(j.toast || '✅ 已选 BGM');
      // 自动勾选「启用背景音乐」checkbox
      var cb = document.querySelector('.slirn-fine-enabled[data-key="audio"]');
      if (cb && !cb.checked) {
        cb.checked = true;
        cb.dispatchEvent(new Event('change', {bubbles: true}));
      }
      // 触发 fineSaveAll 自动保存（后台静默，showToast=false, asTemplate=false）
      if (typeof fineSaveAll === 'function') {
        try { await fineSaveAll(false, false); } catch (e) { /* 静默 */ }
      }
      // 缓存当前路径，供 sync 用
      window._slirnFineAudioPath = 'materials/audio/' + bgmId + '.mp3';
      // REQ-20260921-NNN：把 audio 上传卡视觉切到「已提供 BGM」绿色状态
      // （等价于 has-file class —— 与上传成功的视觉效果一致，让用户立即看到
      // 「背景音乐这一框变成绿色 = 素材已就绪」）。同步更新 status / 解预览按钮 disabled。
      var audioCard = document.querySelector('.slirn-fine-upload-card[data-kind="audio"]');
      if (audioCard) {
        audioCard.classList.add('has-file');
        audioCard.setAttribute('data-source', 'default_bgm');
        var st = audioCard.querySelector('[data-status-kind="audio"]');
        if (st) st.innerHTML = '🎵 已选 BGM：' + (j.name || bgmId);
        var pvBtn = audioCard.querySelector('[data-action="fine-mat-preview"]');
        if (pvBtn) {
          pvBtn.disabled = false;
          pvBtn.title = '打开预览窗口（可缩放）';
        }
        var detBtn = audioCard.querySelector('[data-action="fine-mat-detail"]');
        if (detBtn) {
          detBtn.disabled = false;
          detBtn.title = '查看完整路径 + 文件元数据';
        }
        // 替换 source_badge：把原 auto/upload 徽章替换为「📦 系统 BGM」
        var badge = audioCard.querySelector('.slirn-fine-source-badge');
        if (badge) {
          badge.outerHTML = '<span class="slirn-fine-source-badge default_bgm">📦 系统 BGM</span>';
        } else {
          var label = audioCard.querySelector('.slirn-fine-upload-label');
          if (label) {
            var newBadge = document.createElement('span');
            newBadge.className = 'slirn-fine-source-badge default_bgm';
            newBadge.textContent = '📦 系统 BGM';
            label.appendChild(newBadge);
          }
        }
      }
    } catch (e) {
      toast('❌ 网络错误: ' + (e.message || e));
    }
  }

  // 委托 change 事件：用户在「📦 系统默认 BGM」下拉选某项
  document.addEventListener('change', function(e) {
    if (e.target && e.target.id === 'slirn-fine-default-bgm') {
      var _tid = e.target.getAttribute('data-task-id') || _fineTid();
      var val = e.target.value || '';
      fineDefaultBgmSelect(val, _tid);
    }
  });

  // REQ-20260919-062 v14 用户反馈：视频缩放 = 视频原裁剪宽度 / 背景图片宽度。
  // 公式 scale = crop_w / 1920。点「🎯 按裁剪宽度」按钮应用此公式。
  // 设计空间 bg_w = 1920；视频裁剪宽度 = crop_w（来自滑块）。
  // 限制：scale ∈ [0.1, 2.0]（与滑块 max/min 对齐）。
  function fineScaleAutoFromCrop() {
    var cropWEl = document.getElementById('slirn-fine-video-crop_w');
    var scaleEl = document.getElementById('slirn-fine-video-scale');
    if (!cropWEl || !scaleEl) {
      toast('❌ 找不到裁剪宽度 / 缩放滑块', 'error');
      return;
    }
    var cropW = parseInt(cropWEl.value, 10);
    if (!cropW || cropW <= 0) {
      toast('❌ 视频裁剪宽度无效', 'error');
      return;
    }
    // REQ-20260919-062 v15：保留 4 位小数（与自动重算路径一致）
    var scale = Math.round((cropW / 1920) * 10000) / 10000;
    // 限制到滑块范围 [0.1, 2.0]
    var minS = parseFloat(scaleEl.min || '0.1');
    var maxS = parseFloat(scaleEl.max || '2.0');
    scale = Math.max(minS, Math.min(maxS, scale));
    // 同步 slider + number input（用 _fineSyncSlider 一致化处理，保留 4 位）
    _fineSyncSlider('slirn-fine-video-scale', scale);
    // 触发保存 + 刷新视频信息行（updateVideoDisp 由滑块 change 事件触发）
    fineSaveAll();
    toast('✅ 已应用「按裁剪宽度」：scale = ' + scale.toFixed(4));
  }
  // ========== END REQ-20260919-061 前端函数 ==========
  function optFilterBtn(btn) {  // 只看有不明确字词的行 / 全部行
    var list = revVis('slirn-opt-list');
    if (!list) return;
    var only = list.classList.toggle('slirn-opt-only-occ');
    btn.setAttribute('data-shown', only ? '0' : '1');
    btn.textContent = only ? '📋 显示全部识别行'
      : (btn.getAttribute('data-all-text') || '🔍 只看有不明确字词的行');
  }
  // REQ-20260922-NNN 查找被删除的字幕：只看已删除的行（与 occ 筛选可叠加 = 交集）
  function optFilterDeletedBtn(btn) {
    var list = revVis('slirn-opt-list');
    if (!list) return;
    var only = list.classList.toggle('slirn-opt-only-deleted');
    btn.setAttribute('data-shown', only ? '0' : '1');
    btn.textContent = only ? '📋 显示全部识别行'
      : (btn.getAttribute('data-all-text') || '🗑️ 只看已删除的行');
  }
  // REQ-20260923-NNN 行搜索：字幕行文本筛选（纯前端）。命中范围 = 原文 +
  // 生效文本 + 整句替换 + 替换值 + 切分子段（不区分大小写）；子段命中 →
  // 父行（含全部子段）一并显示。opt-search-miss 类与 only-occ / only-deleted
  // 的 CSS 隐藏规则天然叠加 = 交集（与词筛选 chip 的行内 display 互不干扰）。
  function optRowSearch() {
    var list = revVis('slirn-opt-list');
    var inp = document.getElementById('slirn-opt-search');
    if (!list || !inp) return;
    var q = (inp.value || '').trim().toLowerCase();
    var clr = document.querySelector('.slirn-opt-search-clear');
    var cnt = document.getElementById('slirn-opt-search-count');
    if (clr) clr.style.display = q ? '' : 'none';
    list.classList.toggle('opt-searching', !!q);
    var hit = 0, total = 0;
    list.querySelectorAll('.slirn-opt-row[data-id]').forEach(function(row) {
      total++;
      var rid = row.getAttribute('data-id') || '';
      // 子段是父行的平级兄弟（data-parent 关联）— 命中判定并入父行的 haystack
      var subs = rid ? list.querySelectorAll(
        '.slirn-opt-subrow[data-parent="' + rid + '"]') : [];
      var hay = [row.getAttribute('data-orig-text') || '',
                 row.getAttribute('data-eff-text') || '',
                 row.getAttribute('data-full-text') || '',
                 row.getAttribute('data-split-target') || ''];
      row.querySelectorAll('.slirn-opt-after').forEach(function(o) {
        hay.push(o.value || o.getAttribute('value') || '');
      });
      subs.forEach(function(sr) {
        var st = sr.querySelector('.slirn-opt-subtext');
        if (st) hay.push(st.textContent || '');
      });
      var miss = !!q && hay.join('\n').toLowerCase().indexOf(q) < 0;
      row.classList.toggle('opt-search-miss', miss);
      subs.forEach(function(sr) { sr.classList.toggle('opt-search-miss', miss); });
      if (!miss) hit++;
    });
    if (cnt) cnt.textContent = q ? (hit + '/' + total + ' 行') : '';
  }
  function bindOptRowSearch() {  // 幂等 — wb 重渲后重绑 input 事件（镜像 bindOptWordTextFilter）
    var inp = document.getElementById('slirn-opt-search');
    if (!inp || inp.dataset.bound) return;
    inp.dataset.bound = '1';
    var t = null;
    inp.addEventListener('input', function() {
      if (t) clearTimeout(t);
      t = setTimeout(optRowSearch, 250);
    });
    inp.addEventListener('keydown', function(ev) {
      if (ev.key === 'Enter') {  // 立即应用 + 平滑跳到第一条命中行
        ev.preventDefault();
        if (t) clearTimeout(t);
        optRowSearch();
        var list = revVis('slirn-opt-list');
        var first = list && list.querySelector(
          '.slirn-opt-row[data-id]:not(.opt-search-miss)');
        if (first) first.scrollIntoView({block: 'center', behavior: 'smooth'});
      } else if (ev.key === 'Escape') {
        ev.preventDefault();
        inp.value = '';
        optRowSearch();
        inp.focus();
      }
    });
  }
  // REQ-20260922-NNN 查看最终字幕：应用替换 + 去掉删除行 = 成片将使用的字幕内容。
  // 2026-09-30 用户反馈：默认粗剪基让人误以为「显示的不是拼接后的字幕」—
  // 优化成片已生成时默认直接显示成片时间基（与 optimize_compose.mp4 对位），
  // 粗剪基降为切换项；无成片时仍回落粗剪基。
  function optFinalView(btn) {
    var tid = btn.getAttribute('data-task-id') || '';
    if (!tid) return;
    // cut 基是否可用 = 「▶️ 查看最终视频」按钮在 DOM（与服务端 cut_ready
    // 同源渲染；SRT 下载按钮已并入本弹窗 — REQ-20260923-NNN 去重）
    var hasCutBase = !!document.querySelector(
      '[data-action="opt-final-video"]');
    postJSON(SLIRN_API + '/optimized_srt', {task_id: tid, base: hasCutBase ? 'cut' : 'rough'})
      .then(function(r) {
        if (!r || !r.ok) {
          toast('❌ 获取最终字幕失败: ' + ((r && r.error) || '未知错误'), 'error');
          return;
        }
        _renderOptFinalModal(r.srt || '', r.lines || 0, hasCutBase, tid);
      });
  }
  function _renderOptFinalModal(srt, lines, hasCutBase, tid) {
    var existing = document.getElementById('slirn-opt-final-overlay');
    if (existing) existing.remove();
    var overlay = document.createElement('div');
    overlay.className = 'slirn-modal-overlay';
    overlay.id = 'slirn-opt-final-overlay';
    overlay.innerHTML =
      '<div class="slirn-modal-card slirn-opt-final-card">' +
        '<div class="slirn-modal-title">📄 最终字幕 · ' + lines + ' 行</div>' +
        '<div class="slirn-form-hint" style="margin:0 0 8px;">已应用生效替换并去掉标记删除行 = '
        + '成片将使用的字幕内容。' + (hasCutBase ? '时间基：' : '时间轴为原始（粗剪）时间基。')
        + '</div>' +
        (hasCutBase
          ? '<div class="slirn-opt-final-bases">'
            + '<button class="slirn-btn slirn-btn-sm" data-final-base="rough">粗剪时间基</button>'
            + '<button class="slirn-btn slirn-btn-sm" data-final-base="cut">优化成片时间基（已前移）</button>'
          + '</div>'
          : '') +
        // REQ-20260930-NNN 搜索确认：替换后的新词应能搜到、被替换的旧词应 0 处
        '<div class="slirn-opt-final-search">'
          + '<input type="text" id="slirn-opt-final-q" autocomplete="off" '
          + 'placeholder="🔍 搜索确认（新词应能找到 · 旧词应 0 处）…">'
          + '<span id="slirn-opt-final-qcount" class="slirn-opt-final-qcount"></span>'
          + '<button type="button" class="slirn-opt-final-qnav" id="slirn-opt-final-prev" '
          + 'title="上一个（Shift+Enter）">↑</button>'
          + '<button type="button" class="slirn-opt-final-qnav" id="slirn-opt-final-next" '
          + 'title="下一个（Enter）">↓</button>'
          + '<button type="button" class="slirn-opt-final-qnav" id="slirn-opt-final-qclear" '
          + 'title="清空搜索（Esc）">✕</button>'
        + '</div>' +
        '<pre class="slirn-opt-final-pre">' + escapeHtml(srt) + '</pre>' +
        '<div style="display:flex; gap:10px; justify-content:center;">'
          + '<button class="slirn-btn" id="slirn-opt-final-dl">⬇️ 下载当前时间基 SRT</button>'
          + '<button class="slirn-btn" id="slirn-opt-final-close">❌ 关闭</button></div>' +
      '</div>';
    document.body.appendChild(overlay);
    var close = function() { overlay.remove(); };
    document.getElementById('slirn-opt-final-close').onclick = close;
    var pre = overlay.querySelector('.slirn-opt-final-pre');
    // REQ-20260930-NNN 搜索确认：高亮全部命中 + 当前处深色 + 计数 + 上下导航。
    // 复用 optBatchFind（大小写不敏感全位置 + 折叠长度回退）；切片逐段 escape
    // 保证 XSS 安全；<mark> 不改 textContent → 下载仍取到干净 SRT 原文。
    var qInput = document.getElementById('slirn-opt-final-q');
    var qCount = document.getElementById('slirn-opt-final-qcount');
    var qIdx = 0;      // 当前命中（1 基；0 = 无命中）
    var qLast = null;  // 上次应用的查询词（Enter 只在新词时重定位，否则步进）
    function qApply(jump) {
      var text = pre.textContent || '';  // mark 不污染 textContent，重复调用安全
      var q = (qInput.value || '').trim();
      qLast = q;
      if (!q) {
        pre.innerHTML = escapeHtml(text);
        qCount.textContent = '';
        qCount.classList.remove('none');
        qIdx = 0;
        return;
      }
      var html = [], cur = 0;
      optBatchFind(text, q).forEach(function(p, i) {
        html.push(escapeHtml(text.slice(cur, p)));
        html.push('<mark class="slirn-opt-final-hit' + (i === 0 ? ' cur' : '') + '">'
                  + escapeHtml(text.slice(p, p + q.length)) + '</mark>');
        cur = p + q.length;
      });
      html.push(escapeHtml(text.slice(cur)));
      pre.innerHTML = html.join('');  // 重写即清除旧命中标记
      var n = pre.querySelectorAll('.slirn-opt-final-hit').length;
      qIdx = n ? 1 : 0;
      qCount.textContent = n ? ('1/' + n + ' 处') : '未找到 · 0 处';
      qCount.classList.toggle('none', !n);
      var m0 = pre.querySelector('.slirn-opt-final-hit.cur');
      if (m0 && jump) m0.scrollIntoView({block: 'center', behavior: 'smooth'});
    }
    function qStep(d) {  // ±1 循环步进（wrap-around），当前处深色 + 滚动跟随
      var ms = pre.querySelectorAll('.slirn-opt-final-hit');
      if (!ms.length) return;
      qIdx = ((qIdx - 1 + d) % ms.length + ms.length) % ms.length + 1;
      ms.forEach(function(m, i) { m.classList.toggle('cur', i === qIdx - 1); });
      qCount.textContent = qIdx + '/' + ms.length + ' 处';
      ms[qIdx - 1].scrollIntoView({block: 'center', behavior: 'smooth'});
    }
    var qTimer = null;
    qInput.addEventListener('input', function() {
      if (qTimer) clearTimeout(qTimer);
      qTimer = setTimeout(function() { qApply(true); }, 200);
    });
    qInput.addEventListener('keydown', function(ev) {
      if (ev.key === 'Enter') {
        ev.preventDefault();
        if (qTimer) clearTimeout(qTimer);
        // 新词 → 定位第 1 处；同词 → Enter 下一处 / Shift+Enter 上一处
        if ((qInput.value || '').trim() !== qLast) qApply(true);
        else qStep(ev.shiftKey ? -1 : 1);
      } else if (ev.key === 'Escape') {
        ev.preventDefault();
        qInput.value = '';
        qApply(false);
        qInput.focus();
      }
    });
    overlay.querySelector('#slirn-opt-final-next').onclick = function() { qStep(1); };
    overlay.querySelector('#slirn-opt-final-prev').onclick = function() { qStep(-1); };
    overlay.querySelector('#slirn-opt-final-qclear').onclick = function() {
      qInput.value = '';
      qApply(false);
      qInput.focus();
    };
    try { qInput.focus(); } catch (err) {}
    // 下载当前显示的字幕（时间基随弹窗内切换 — 原 ⬇️ 行内下载按钮并入此处）
    var curBase = hasCutBase ? 'cut' : 'rough';  // 下载文件名/提示随打开时的时间基（默认成片基）
    document.getElementById('slirn-opt-final-dl').onclick = function() {
      var pre2 = overlay.querySelector('.slirn-opt-final-pre');
      var blob = new Blob([pre2.textContent || ''], {type: 'text/plain;charset=utf-8'});
      var url = URL.createObjectURL(blob);
      var a = document.createElement('a');
      a.href = url;
      a.download = (curBase === 'cut' ? 'optimize_compose_subs_' : 'optimized_subs_')
        + tid + '.srt';
      document.body.appendChild(a); a.click();
      setTimeout(function() { URL.revokeObjectURL(url); document.body.removeChild(a); }, 100);
      toast('⬇️ 已下载 SRT' + (curBase === 'cut' ? '（优化成片时间基）' : '（粗剪时间基）'));
    };
    overlay.addEventListener('click', function(e) {
      if (e.target === overlay) close();  // 点遮罩关闭
    });
    if (hasCutBase) {
      var title = overlay.querySelector('.slirn-modal-title');
      var mark = function(baseBtn) {
        overlay.querySelectorAll('[data-final-base]').forEach(function(b) {
          b.classList.toggle('slirn-btn-primary', b === baseBtn);
        });
      };
      // 打开即成片基 → 初始高亮成片按钮（粗剪基是切换项）
      mark(overlay.querySelector('[data-final-base="cut"]'));
      overlay.querySelectorAll('[data-final-base]').forEach(function(b) {
        b.onclick = function() {
          if (b.classList.contains('slirn-btn-primary')) return;
          mark(b);
          var base = b.getAttribute('data-final-base');
          postJSON(SLIRN_API + '/optimized_srt', {task_id: tid, base: base})
            .then(function(r) {
              if (!r || !r.ok) {
                toast('❌ 切换时间基失败: ' + ((r && r.error) || ''), 'error');
                return;
              }
              pre.textContent = r.srt || '';
              curBase = base;  // 下载随切换（下载的是当前显示内容）
              title.textContent = '📄 最终字幕 · ' + (r.lines || 0) + ' 行';
              qApply(true);  // 时间基换了重放搜索（新时间轴上重新定位命中）
            });
        };
      });
    }
  }
  function optCollectDecisions() {  // REQ-20260918-056：共用 decisions 收集（前端 DOM = 磁盘状态镜像）
    var list = revVis('slirn-opt-list');
    if (!list) return [];
    var decisions = [];
    list.querySelectorAll('.slirn-opt-occ').forEach(function(w) {
      var inp = w.querySelector('input.slirn-opt-after');
      decisions.push({
        occ_id: parseInt(w.getAttribute('data-occ'), 10),
        applied: w.getAttribute('data-applied') === '1',
        after: inp ? inp.value.trim() : '',
        reviewed: w.getAttribute('data-reviewed') === '1'  // REQ-038：处理进度随保存落盘
      });
    });
    return decisions;
  }
  // REQ-20260922-NNN 整句替换：收集 data-full-edit="1" 行 → [{seg, text}]
  // （与 decisions 同口径：前端 DOM = 磁盘状态镜像，不传 = 清空）
  function optCollectLineEdits() {
    var list = revVis('slirn-opt-list');
    if (!list) return [];
    var edits = [];
    list.querySelectorAll('.slirn-opt-row[data-full-edit="1"]').forEach(function(row) {
      var text = row.getAttribute('data-full-text') || '';
      if (text.trim()) {
        edits.push({seg: parseInt(row.getAttribute('data-id'), 10), text: text});
      }
    });
    return edits;
  }
  // REQ-20260922-NNN 标记删除行：收集 data-deleted="1" 行 → [seg id]（升序去重）。
  // 保存后服务端据此把标记行时间段从粗剪成片剪除 → optimize_compose.mp4
  function optCollectLineMarks() {
    var list = revVis('slirn-opt-list');
    if (!list) return [];
    var seen = {};
    var marks = [];
    list.querySelectorAll('.slirn-opt-row[data-deleted="1"]').forEach(function(row) {
      var id = parseInt(row.getAttribute('data-id'), 10);
      if (!isNaN(id) && !seen[id]) { seen[id] = 1; marks.push(id); }
    });
    return marks.sort(function(a, b) { return a - b; });
  }
  // REQ-20260923-NNN 行内切分：收集切分子段标记 → {"<seg>": [删除子段索引…]}。
  // 全量快照口径：凡在 DOM 里有子段的切分行都给键（[] = 全保留）— 键缺失
  // 会被服务端回落到切分默认洞，用户"全部恢复保留"就丢了。
  function optCollectSplitMarks() {
    var list = revVis('slirn-opt-list');
    var out = {};
    if (!list) return out;
    list.querySelectorAll('.slirn-opt-subrow').forEach(function(row) {
      var pid = row.getAttribute('data-parent');
      if (!pid) return;
      if (!(pid in out)) out[pid] = [];
      if (row.getAttribute('data-mark') === 'delete') {
        var idx = parseInt(row.getAttribute('data-sub-idx'), 10);
        if (!isNaN(idx)) out[pid].push(idx);
      }
    });
    return out;
  }
  // REQ-20260918-056：回车后自动保存 — 含并发锁（连续 Enter 合并到 pending）
  var _optSaveInFlight = null;
  var _optSavePending = null;
  function optAutoSave(tid) {
    if (!tid) return;
    if (_optSaveInFlight) {
      // 已有请求在跑 → 把当前快照记为"完成后立即再发一次"
      _optSavePending = {tid: tid, decisions: optCollectDecisions(),
                         line_edits: optCollectLineEdits(),
                         line_marks: optCollectLineMarks(),
                         split_marks: optCollectSplitMarks()};
      return;
    }
    var decisions = optCollectDecisions();
    var line_edits = optCollectLineEdits();
    var line_marks = optCollectLineMarks();
    var split_marks = optCollectSplitMarks();
    _optSaveInFlight = postJSON(SLIRN_API + '/save_optimize_subtitle',
      {task_id: tid, decisions: decisions, line_edits: line_edits,
       line_marks: line_marks, split_marks: split_marks})
      .then(function(r) {
        if (r && r.ok) {
          toast('✅ 已自动保存', 'success');
        } else if (r && r.error) {
          toast('❌ 自动保存失败：' + r.error, 'error');
        }
      })
      .catch(function() {
        toast('❌ 自动保存失败（网络错误）', 'error');
      })
      .then(function() {
        _optSaveInFlight = null;
        // 若期间有 pending → 立即再发一次（捕获最后一次的状态）
        if (_optSavePending) {
          var p = _optSavePending; _optSavePending = null;
          optAutoSave(p.tid);
        }
      });
  }
  function optSave(btn) {
    var tid = btn.getAttribute('data-task-id') || '';
    var decisions = optCollectDecisions();
    var line_edits = optCollectLineEdits();
    var line_marks = optCollectLineMarks();
    var split_marks = optCollectSplitMarks();
    if (decisions.length === 0 && line_edits.length === 0 && line_marks.length === 0
        && Object.keys(split_marks).length === 0) {
      toast('❌ 无优化结果列表', 'error'); return;
    }
    postJSON(SLIRN_API + '/save_optimize_subtitle',
      {task_id: tid, decisions: decisions, line_edits: line_edits,
       line_marks: line_marks, split_marks: split_marks})
      .then(function(r) {
        if (r && r.ok) {
          toast(r.toast || '已确认保存');
          openWorkbench(tid);  // 刷新阶段态（done）+ 统计 + 对应关系
          // REQ-20260922-NNN 标记删除行（手动触发）：保存不触发剪辑 — 仅当
          // 🎬 按钮触发的剪辑恰在跑时续上轮询到完成
          if (r.cut && (r.cut.state === 'started' || r.cut.state === 'running')) {
            startOptCutPolling(tid);
          }
        } else if (r && r.error) {
          toast('❌ ' + r.error, 'error');
        }
      });
  }
  // ========== REQ-20260922-NNN 整句替换：行内编辑 ==========
  function optLineEdit(row) {  // ✏️ 进入编辑态；已有整句（↩️）= 取消整句并自动保存
    if (!row) return;
    if (row.classList.contains('editing')) return;
    if (row.getAttribute('data-full-edit') === '1') { optLineClear(row); return; }
    var textCol = row.querySelector('.slirn-fw-text');
    if (!textCol) return;
    row.classList.add('editing');
    // 编辑态预填：当前生效文本（保存过的整句/局部替换结果，无则原文）
    var prefill = row.getAttribute('data-eff-text') || row.getAttribute('data-orig-text') || '';
    var _splitBtn = row.getAttribute('data-split') === '1' ? '' :
      '<button class="slirn-btn slirn-btn-xs slirn-opt-line-btn" data-action="opt-line-resplit" '
      + 'title="行内切分：填「切分后内容」（保留想留的文字，对不上的字自动落进删除洞；子段可单独删除）">✂️</button>';
    textCol.innerHTML =
      optDelBtnHtml(row) + _splitBtn +
      '<button class="slirn-btn slirn-btn-xs slirn-opt-line-btn" data-action="opt-line-edit" ' +
      'title="整句替换：直接改写整行文本，覆盖本行局部替换">✏️</button>' +
      '<span class="slirn-opt-line-editbox">' +
      '<textarea class="slirn-opt-line-input" rows="2"></textarea>' +
      '<span class="slirn-opt-line-ops">' +
      '<button class="slirn-btn slirn-btn-xs slirn-btn-primary" data-action="opt-line-apply">✅ 应用整句</button>' +
      '<button class="slirn-btn slirn-btn-xs" data-action="opt-line-cancel">取消</button>' +
      '</span></span>';
    var ta = textCol.querySelector('textarea.slirn-opt-line-input');
    if (ta) {
      ta.value = prefill;
      ta.focus();
      try { ta.setSelectionRange(ta.value.length, ta.value.length); } catch (err) {}
    }
  }
  function optLineApply(row) {  // ✅ 应用：空/同原文 = 取消；否则置整句 + 重渲 + 自动保存
    if (!row || !row.classList.contains('editing')) return;
    var ta = row.querySelector('textarea.slirn-opt-line-input');
    var text = ta ? ta.value.trim() : '';
    var orig = row.getAttribute('data-orig-text') || '';
    row.classList.remove('editing');
    if (!text || text === orig) { optLineRender(row, false, ''); optLineAfterEdit(row); return; }
    row.setAttribute('data-full-edit', '1');
    row.setAttribute('data-full-text', text);
    optLineRender(row, true, text);
    optLineAfterEdit(row);
  }
  function optLineCancel(row) {  // 取消编辑态（不动已落盘的整句状态）
    if (!row || !row.classList.contains('editing')) return;
    row.classList.remove('editing');
    var hadFull = row.getAttribute('data-full-edit') === '1';
    optLineRender(row, hadFull, row.getAttribute('data-full-text') || '');
  }
  function optLineClear(row) {  // ↩️ 取消整句：清位 + 恢复原文渲染 + 自动保存
    row.classList.remove('editing');
    row.setAttribute('data-full-edit', '0');
    row.removeAttribute('data-full-text');
    optLineRender(row, false, '');
    optLineAfterEdit(row);
  }
  function optLineRender(row, hasFull, fullText) {
    // 按服务端 _line_html 同款结构重渲正文列 + occ 覆盖标注（纯前端镜像）
    var textCol = row.querySelector('.slirn-fw-text');
    if (!textCol) return;
    var rid = row.getAttribute('data-id') || '';
    var orig = row.getAttribute('data-orig-text') || '';
    var del = row.getAttribute('data-deleted') === '1';
    var btn = hasFull
      ? '<button class="slirn-btn slirn-btn-xs slirn-opt-line-btn" data-action="opt-line-edit" title="取消整句替换，恢复局部替换">↩️</button>'
      : '<button class="slirn-btn slirn-btn-xs slirn-opt-line-btn" data-action="opt-line-edit" title="整句替换：直接改写整行文本，覆盖本行局部替换">✏️</button>';
    var delBadge = del ? '<span class="slirn-opt-line-badge del">🗑️ 已标记删除</span>' : '';
    // REQ-20260923-NNN：未切分行保留 ✂️ 切分入口（切分行不走本函数 — 无整句编辑）
    var splitBtn = row.getAttribute('data-split') === '1' ? '' :
      '<button class="slirn-btn slirn-btn-xs slirn-opt-line-btn" data-action="opt-line-resplit" '
      + 'title="行内切分：填「切分后内容」（保留想留的文字，对不上的字自动落进删除洞；子段可单独删除）">✂️</button>';
    var body;
    if (hasFull) {
      body = delBadge + '<span class="slirn-opt-line-badge">✏️ 整句替换</span>' +
        '<span class="slirn-opt-line-new"></span>' +
        '<span class="slirn-opt-line-orig" title="原文"></span>';
    } else {
      body = delBadge + '<span class="slirn-fw-rawtext"></span>';
    }
    textCol.innerHTML = optDelBtnHtml(row) + splitBtn + btn + body;
    // textContent 赋值 = 天然转义（不用 innerHTML 拼用户文本）
    if (hasFull) {
      textCol.querySelector('.slirn-opt-line-new').textContent = fullText;
      textCol.querySelector('.slirn-opt-line-orig').textContent = orig;
      row.classList.add('full-edit');
    } else {
      textCol.querySelector('.slirn-fw-rawtext').textContent = orig;
      row.classList.remove('full-edit');
    }
    // occ chip 覆盖标注（与服务端渲染同款：overridden 类 + 提示行）
    var occCol = row.querySelector('.slirn-opt-col');
    if (occCol) {
      var hint = occCol.querySelector('.slirn-opt-occ-override-hint');
      if (hasFull && !hint) {
        hint = document.createElement('span');
        hint.className = 'slirn-opt-occ-override-hint';
        hint.textContent = '整句替换已覆盖本行局部替换';
        occCol.appendChild(hint);
      } else if (!hasFull && hint) {
        hint.parentNode.removeChild(hint);
      }
      occCol.querySelectorAll('.slirn-opt-occ').forEach(function(occ) {
        occ.classList.toggle('overridden', hasFull);
      });
    }
  }
  function optLineAfterEdit(row) {  // 整句状态变更后自动保存（沿用 REQ-20260918-056 锁）
    var inner = document.getElementById('slirn-tab-workbench-inner');
    var tid = inner ? (inner.getAttribute('data-task-id') || '') : '';
    if (tid) optAutoSave(tid);
  }
  // ========== REQ-20260922-NNN 标记删除行：🗑️/✚ 翻转 + 优化成片剪辑轮询 ==========
  function optDelBtnHtml(row) {  // 行首删除按钮镜像（编辑态/重渲正文列时保留）
    var del = row.getAttribute('data-deleted') === '1';
    return del
      ? '<button class="slirn-btn slirn-btn-xs slirn-opt-line-btn slirn-opt-del-btn" data-action="opt-line-delete" data-deleted="1" title="取消删除标记：本行恢复保留">✚</button>'
      : '<button class="slirn-btn slirn-btn-xs slirn-opt-line-btn slirn-opt-del-btn" data-action="opt-line-delete" data-deleted="0" title="标记删除：点「🎬 重新拼接视频」后本行时间段将从粗剪成片剪除，字幕时间轴自动前移">🗑️</button>';
  }
  function optLineDelete(row) {  // 🗑️ 标记 / ✚ 取消 + 行样式 + 徽章 + 自动保存
    if (!row) return;
    if (row.classList.contains('editing')) return;  // 整句编辑态不抢操作
    var to = row.getAttribute('data-deleted') !== '1';
    row.setAttribute('data-deleted', to ? '1' : '0');
    row.classList.toggle('line-deleted', to);
    var textCol = row.querySelector('.slirn-fw-text');
    if (textCol) {
      var btn = textCol.querySelector('.slirn-opt-del-btn');
      if (btn) {
        btn.setAttribute('data-deleted', to ? '1' : '0');
        btn.innerHTML = to ? '✚' : '🗑️';
        btn.title = to ? '取消删除标记：本行恢复保留'
          : '标记删除：点「🎬 重新拼接视频」后本行时间段将从粗剪成片剪除，字幕时间轴自动前移';
      }
      var badge = textCol.querySelector('.slirn-opt-line-badge.del');
      if (to && !badge) {
        badge = document.createElement('span');
        badge.className = 'slirn-opt-line-badge del';
        badge.textContent = '🗑️ 已标记删除';
        // 放在正文最前（✏️ 整句替换徽章之前 — 与服务端 _line_html 同构）
        var firstText = textCol.querySelector(
          '.slirn-opt-line-badge:not(.del), .slirn-fw-rawtext, .slirn-opt-line-new');
        textCol.insertBefore(badge, firstText || null);
      } else if (!to && badge) {
        badge.parentNode.removeChild(badge);
      }
    }
    if (to) {
      var s = ((parseInt(row.getAttribute('data-end-ms'), 10) || 0)
        - (parseInt(row.getAttribute('data-start-ms'), 10) || 0)) / 1000;
      toast('🗑️ 已标记删除 — 点「🎬 重新拼接视频」从成片剪除该行（约 ' + s.toFixed(1) + 's，字幕自动前移）');
    } else {
      toast('✚ 已取消删除标记', 'success');
    }
    optLineAfterEdit(row);  // 复用整句替换的自动保存锁（REQ-20260918-056）
  }
  // ========== REQ-20260923-NNN 行内切分：✂️ 切分 / 子段翻转 / ↩️ 取消 / 🔄 拼接字幕 ==========
  function optSubToggle(btn) {  // 子段 🗑️/✚：翻转 + 样式 + 自动保存（split_marks 全量快照）
    var row = btn.closest('.slirn-opt-subrow');
    if (!row) return;
    var toDel = row.getAttribute('data-mark') !== 'delete';
    row.setAttribute('data-mark', toDel ? 'delete' : 'keep');
    row.classList.toggle('line-deleted', toDel);
    btn.setAttribute('data-mark', toDel ? 'delete' : 'keep');
    btn.innerHTML = toDel ? '✚' : '🗑️';
    btn.title = toDel ? '恢复保留该子段'
      : '标记删除该子段：重新拼接时从成片剪除该时间段';
    if (toDel) {
      var s = ((parseInt(row.getAttribute('data-end-ms'), 10) || 0)
        - (parseInt(row.getAttribute('data-start-ms'), 10) || 0)) / 1000;
      toast('🗑️ 已删除该子段（约 ' + s.toFixed(1) + 's）— 🔄 重新拼接字幕 / 🎬 重新拼接视频生效');
    } else {
      toast('✚ 已恢复保留该子段', 'success');
    }
    optLineAfterEdit(row);
  }
  function optCloseResplit() {
    var b = document.querySelector('.slirn-opt-resplit');
    if (b && b.parentNode) b.parentNode.removeChild(b);
  }
  function optOpenResplit(row) {  // ✂️ 切分行内展开编辑区（镜像切分修剪 cutOpenResplit）
    if (!row) return;
    optCloseResplit();
    var rid = row.getAttribute('data-id') || '?';
    var target = row.getAttribute('data-split-target')
      || row.getAttribute('data-orig-text') || '';
    var box = document.createElement('div');
    box.className = 'slirn-cut-resplit slirn-opt-resplit';
    box.innerHTML =
      '<div class="slirn-cut-resplit-label">✂️ 切分第 ' + rid + ' 条 — 填「切分后内容」'
      + '（保留想留的文字；对不上的字自动落进删除洞，删除洞可在子段上 ✚ 恢复）：</div>'
      + '<input class="slirn-cut-resplit-input" type="text">'
      + '<button class="slirn-btn slirn-btn-xs" data-action="opt-resplit-go">✂️ 按内容切分</button>'
      + '<button class="slirn-btn slirn-btn-xs" data-action="opt-resplit-cancel">取消</button>'
      + '<span class="slirn-cut-resplit-hint">切分立即生效并重置本行子段标记；无字级时间戳时按文本占比估算（子段带 ⚠️）</span>';
    row.parentNode.insertBefore(box, row.nextSibling);
    var inp = box.querySelector('.slirn-cut-resplit-input');
    inp.value = target.trim();
    inp.focus();
    try { inp.setSelectionRange(inp.value.length, inp.value.length); } catch (err) {}
    inp.addEventListener('keydown', function(ev) {  // Enter 提交 / Esc 取消
      if (ev.key === 'Enter') { ev.preventDefault(); optDoResplit(); }
      else if (ev.key === 'Escape') { ev.preventDefault(); optCloseResplit(); }
    });
  }
  function optSwapRows(row, html) {  // 行组局部刷新：切分/取消切分后只换本行组
    // 2026-09-29 用户反馈：按内容切分后整页刷新会把用户踢出当前优化字幕工作
    // 环境（滚动/词筛选/焦点全丢）。服务端返回该 seg 重渲后的行组（父行 +
    // 子段行），这里原地替换 — 列表其余 DOM 不动，播放器/词频/筛选全部保留。
    if (!row || !html) return false;
    var list = row.closest('.slirn-opt-list');
    var rid = row.getAttribute('data-id') || '';
    var inner = document.getElementById('slirn-tab-workbench-inner');
    var tid = inner ? (inner.getAttribute('data-task-id') || '') : '';
    optCloseResplit();  // 收起切分编辑框（切分路径它还开着）
    // 旧子段行（data-parent 同 rid）先移除，再原地换父行 — 位置即原父行位置
    if (list && rid) {
      list.querySelectorAll('.slirn-opt-row[data-parent="' + rid + '"]').forEach(function(sr) {
        if (sr.parentNode) sr.parentNode.removeChild(sr);
      });
    }
    // 记住父行在列表视口中的偏移 → 换完恢复（用户视线不跳）
    var anchorTop = null;
    if (list) anchorTop = row.getBoundingClientRect().top - list.getBoundingClientRect().top;
    var tpl = document.createElement('div');
    tpl.innerHTML = html;
    var firstEl = null;
    while (tpl.firstChild) {
      var node = tpl.firstChild;
      row.parentNode.insertBefore(node, row);
      if (!firstEl && node.nodeType === 1) firstEl = node;
    }
    row.parentNode.removeChild(row);
    // 新行重绑：行点击定位播放（其余交互全是 data-action/input 全局委托）
    if (list && rid) {
      var bindAll = function(sel) {
        list.querySelectorAll(sel).forEach(function(nr) { optBindRowClick(nr, tid); });
      };
      bindAll('.slirn-opt-row[data-id="' + rid + '"]');
      bindAll('.slirn-opt-row[data-parent="' + rid + '"]');
    }
    // 替换值输入框溢出检测（data-wrap-bound 守卫，只对新增 input 生效）
    optInputOverflowInit();
    // 行文本筛选重放（新行组需要 opt-search-miss 标记；幂等）
    optRowSearch();
    // 恢复父行视口偏移（子段行增删改变了列表高度，补 scrollTop 差值）
    if (list && firstEl && anchorTop !== null) {
      var nowTop = firstEl.getBoundingClientRect().top - list.getBoundingClientRect().top;
      list.scrollTop += nowTop - anchorTop;
    }
    return true;
  }
  function optDoResplit() {
    var box = document.querySelector('.slirn-opt-resplit');
    var inp = box && box.querySelector('.slirn-cut-resplit-input');
    var row = box && box.previousElementSibling
      ? box.previousElementSibling.closest('.slirn-opt-row[data-id]') : null;
    if (!inp || !row) return;
    var val = inp.value.trim();
    if (!val) { toast('❌ 请填写切分后内容', 'error'); inp.focus(); return; }
    var inner = document.getElementById('slirn-tab-workbench-inner');
    var tid = inner ? (inner.getAttribute('data-task-id') || '') : '';
    postJSON(SLIRN_API + '/optimize_resplit', {
      task_id: tid,
      seg_id: parseInt(row.getAttribute('data-id'), 10),
      target_text: val,
    }).then(function(r) {
      if (r && r.ok) {
        toast(r.toast || '✂️ 已切分');
        // 2026-09-29 用户反馈：局部换行组，不再整页刷新；row_html 缺失兜底整刷
        if (!optSwapRows(row, r.row_html || '')) openWorkbench(tid);
      }
      else if (r && r.error) toast('❌ ' + r.error, 'error');
    });
  }
  function optUnsplit(row) {  // ↩️ 取消切分：恢复整行（子段标记一并清除）
    if (!row) return;
    var inner = document.getElementById('slirn-tab-workbench-inner');
    var tid = inner ? (inner.getAttribute('data-task-id') || '') : '';
    postJSON(SLIRN_API + '/optimize_unsplit', {
      task_id: tid,
      seg_id: parseInt(row.getAttribute('data-id'), 10),
    }).then(function(r) {
      if (r && r.ok) {
        toast(r.toast || '↩️ 已恢复整行');
        // 2026-09-29 用户反馈：与切分同款 — 局部换行组，row_html 缺失兜底整刷
        if (!optSwapRows(row, r.row_html || '')) openWorkbench(tid);
      }
      else if (r && r.error) toast('❌ ' + r.error, 'error');
    });
  }
  // ========== REQ-20260929-NNN 批量替换：查找（5 行上下文预览）→ 确认替换 ==========
  // 用户需求：批量替换之前要查到对应的词并显示出来，同时显示它所在字幕记录的
  // 前两行和后两行（每处命中共 5 行上下文），明确语境后再输入替换文字点确认。
  function optBatchTid() {
    var inner = document.getElementById('slirn-tab-workbench-inner');
    return inner ? (inner.getAttribute('data-task-id') || '') : '';
  }
  function optBatchOpen(btn) {  // 🔁 打开/收起面板（再点同按钮切换）
    var p = document.getElementById('slirn-opt-batchrep');
    if (!p) return;
    var show = p.style.display === 'none';
    p.style.display = show ? '' : 'none';
    if (show) {
      var w = document.getElementById('slirn-opt-batchrep-word');
      if (w) { w.focus(); w.select(); }
    }
  }
  function optBatchClose() {  // 收起 + 清结果（确认按钮状态复位，下次打开是全新现场）
    var p = document.getElementById('slirn-opt-batchrep');
    if (p) p.style.display = 'none';
    var box = document.getElementById('slirn-opt-batchrep-result');
    if (box) box.innerHTML = '';
    var apply = document.getElementById('slirn-opt-batchrep-apply');
    if (apply) apply.style.display = 'none';
    var go = document.getElementById('slirn-opt-batchrep-go');
    if (go) { go.disabled = false; go.textContent = '✅ 确认批量替换'; }
  }
  function optBatchFind(text, word) {  // 大小写不敏感找全部不重叠位置（镜像服务端口径）
    var lt = text.toLowerCase(), lw = word.toLowerCase();
    var out = [], i = 0, p;
    if (lt.length !== text.length) {  // 罕见折叠字符长度变化 → 退精确匹配
      while ((p = text.indexOf(word, i)) >= 0) { out.push(p); i = p + word.length; }
      return out;
    }
    while ((p = lt.indexOf(lw, i)) >= 0) { out.push(p); i = p + word.length; }
    return out;
  }
  function optBatchHi(text, word) {  // XSS 安全高亮：切片逐段 escape，mark 只包命中段
    var out = [], cur = 0;
    optBatchFind(text, word).forEach(function(p) {
      out.push(escapeHtml(text.slice(cur, p)));
      out.push('<mark class="slirn-opt-batchrep-hit">' +
               escapeHtml(text.slice(p, p + word.length)) + '</mark>');
      cur = p + word.length;
    });
    out.push(escapeHtml(text.slice(cur)));
    return out.join('');
  }
  function optBatchSearch(btn) {  // 🔍 查找：预览每处命中的 5 行上下文（不写盘）
    var tid = optBatchTid();
    var inp = document.getElementById('slirn-opt-batchrep-word');
    var word = inp ? (inp.value || '').trim() : '';
    if (!word) { toast('❌ 请输入要查找的词', 'error'); if (inp) inp.focus(); return; }
    var apply = document.getElementById('slirn-opt-batchrep-apply');
    if (apply) apply.style.display = 'none';  // 换词后旧预览对应的确认按钮先撤下
    btn.disabled = true; btn.textContent = '⏳ 查找中…';
    postJSON(SLIRN_API + '/opt_batch_search', {task_id: tid, word: word})
      .then(function(r) {
        btn.disabled = false; btn.textContent = '🔍 查找';
        if (r && r.ok) { optBatchRender(r.search || {}, word); }
        else { toast('❌ ' + ((r && r.error) || '查找失败'), 'error'); }
      })
      .catch(function() {
        btn.disabled = false; btn.textContent = '🔍 查找';
        toast('❌ 查找失败（网络错误）', 'error');
      });
  }
  var _OPT_BATCH_BLOCK_TXT = {
    full_edit: '✏️ 整句替换行 · 跳过（本行已被整句改写覆盖）',
    split: '✂️ 已切分行 · 跳过（替换会破坏子段对齐）',
    deleted: '🗑️ 已标记删除 · 跳过（该行将从成片剪除）',
  };
  function optBatchRender(s, word) {
    var box = document.getElementById('slirn-opt-batchrep-result');
    var apply = document.getElementById('slirn-opt-batchrep-apply');
    if (!box || !apply) return;
    var ms = s.matches || [];
    if (!ms.length) {  // 空态：明确「没找到」，不是没反应
      box.innerHTML = '<div class="slirn-opt-batchrep-empty">未找到「' +
                      escapeHtml(word) + '」— 换个词试试</div>';
      return;
    }
    var html = ['<div class="slirn-opt-batchrep-head">命中 <b>' + (s.hits || 0) +
                '</b> 处 / ' + (s.lines || 0) + ' 行 · 可替换 ' +
                (s.free_hits || 0) + ' 处（' + (s.eligible_lines || 0) + ' 行）' +
                ((s.blocked_lines || 0)
                  ? ' · 不可替换 ' + s.blocked_lines + ' 行（见标注）' : '') +
                '</div>'];
    ms.forEach(function(m) {
      html.push('<div class="slirn-opt-batchrep-item' +
                (m.free_positions && m.free_positions.length ? '' : ' blocked') + '">');
      (m.context || []).forEach(function(c) {
        var cur = c.seg === m.seg;
        html.push('<div class="slirn-opt-batchrep-ctx' + (cur ? ' cur' : '') +
                  (c.deleted ? ' del' : '') + '">' +
                  '<span class="slirn-opt-batchrep-ctx-idx">' + c.seg + '</span>' +
                  '<span class="slirn-opt-batchrep-ctx-txt">' +
                  (cur ? optBatchHi(c.text || '', word) : escapeHtml(c.text || '')) +
                  '</span></div>');
      });
      if (m.block) {
        html.push('<div class="slirn-opt-batchrep-badge">' +
                  (_OPT_BATCH_BLOCK_TXT[m.block] || '跳过') + '</div>');
      } else if (m.skipped_overlap) {
        html.push('<div class="slirn-opt-batchrep-badge">⚠️ ' + m.skipped_overlap +
                  ' 处与现有替换重叠，跳过（其余 ' + m.free_positions.length + ' 处替换）</div>');
      }
      html.push('</div>');
    });
    box.innerHTML = html.join('');
    // 确认区只在确有可替换处时出现（空态/全阻断都不给替换入口）
    apply.style.display = (s.free_hits || 0) > 0 ? '' : 'none';
  }
  function optBatchReplace(btn) {  // ✅ 确认批量替换：注入出现项 + 受影响行组局部刷新
    var tid = optBatchTid();
    var wInp = document.getElementById('slirn-opt-batchrep-word');
    var tInp = document.getElementById('slirn-opt-batchrep-to');
    var word = wInp ? (wInp.value || '').trim() : '';
    var to = tInp ? (tInp.value || '').trim() : '';
    if (!word) { toast('❌ 请输入要查找的词', 'error'); if (wInp) wInp.focus(); return; }
    if (!to) { toast('❌ 请输入替换后的文字', 'error'); if (tInp) tInp.focus(); return; }
    btn.disabled = true; btn.textContent = '⏳ 替换中…';
    postJSON(SLIRN_API + '/opt_batch_replace',
             {task_id: tid, word: word, replacement: to})
      .then(function(r) {
        if (r && r.ok) {
          toast(r.toast || '✅ 已批量替换');
          var rows = (r.rows || {});
          var list = revVis('slirn-opt-list');
          var miss = false;
          Object.keys(rows).forEach(function(rid) {
            var row = list && list.querySelector('.slirn-opt-row[data-id="' + rid + '"]');
            if (row && rows[rid] && optSwapRows(row, rows[rid])) return;
            miss = true;  // 行不在 DOM / row_html 缺失 → 记下兜底整刷
          });
          // 词频面板同步（新词 chip 可点选筛选；过滤条在之前零词时才需要补插）
          var w = r.words || {};
          var wl = document.getElementById('slirn-opt-words');
          if (wl && w.list !== undefined) wl.innerHTML = w.list;
          var wf = document.getElementById('slirn-opt-word-filters');
          if (w.filters) {
            if (wf) { wf.outerHTML = w.filters; }
            else if (wl) { wl.insertAdjacentHTML('beforebegin', w.filters); }
          }
          if (typeof setupOptWordsPagination === 'function') setupOptWordsPagination();
          if (miss) openWorkbench(tid);  // 兜底：整刷一次保证一致
          else optBatchClose();
        } else {
          btn.disabled = false; btn.textContent = '✅ 确认批量替换';
          toast('❌ ' + ((r && r.error) || '替换失败'), 'error');
        }
      })
      .catch(function() {
        btn.disabled = false; btn.textContent = '✅ 确认批量替换';
        toast('❌ 替换失败（网络错误）', 'error');
      });
  }
  function bindOptBatchInputs() {  // 键盘可用（镜像 bindOptRowSearch）：Enter 查找/替换、Esc 收起
    var w = document.getElementById('slirn-opt-batchrep-word');
    if (w && !w.dataset.bound) {
      w.dataset.bound = '1';
      w.addEventListener('keydown', function(ev) {
        if (ev.key === 'Enter') {
          ev.preventDefault();
          var b = document.getElementById('slirn-opt-batchrep-search-btn');
          if (b) optBatchSearch(b);
        } else if (ev.key === 'Escape') {
          ev.preventDefault(); w.value = ''; optBatchClose();
        }
      });
    }
    var t = document.getElementById('slirn-opt-batchrep-to');
    if (t && !t.dataset.bound) {
      t.dataset.bound = '1';
      t.addEventListener('keydown', function(ev) {
        if (ev.key === 'Enter') {
          ev.preventDefault();
          var b = document.getElementById('slirn-opt-batchrep-go');
          if (b) optBatchReplace(b);
        }
      });
    }
  }
  function optResplice(btn) {  // 🔄 重新拼接字幕：即时重算 optimize_compose.srt（不编码视频）
    var tid = btn.getAttribute('data-task-id') || '';
    if (!tid) return;
    btn.disabled = true;
    postJSON(SLIRN_API + '/optimize_resplice_subs', {task_id: tid}).then(function(r) {
      btn.disabled = false;
      if (r && r.ok) { if (r.toast) toast(r.toast); }
      else if (r && r.error) toast('❌ ' + r.error, 'error');
    }).catch(function() { btn.disabled = false; });
  }
  function optFinalVideoView(btn) {  // ▶️ 查看最终视频：播放器切到 optimize_compose
    var tid = btn.getAttribute('data-task-id') || '';
    var wrap = revVis('slirn-opt-player-wrap');
    var v = revVis('slirn-opt-player');
    if (!v || !tid) { toast('❌ 播放器未就绪', 'error'); return; }
    if (wrap) vfShow(wrap);
    _optFinalVideo = true;  // 高亮/跳播让位（时间轴已前移，行 ms 对不上）
    var url = SLIRN_API + '/video/' + encodeURIComponent(tid) + '?src=optimize_compose';
    if (v.src !== url) { v.src = url; v.load(); }
    try { v.currentTime = 0; } catch (err) {}
    var p = v.play();
    if (p && p.catch) p.catch(function() {});
    toast('🎬 正在播放优化成片（已剪除标记内容，时间轴已前移）— '
      + '点任意字幕行恢复粗剪时间基定位播放');
  }
  // ========== REQ-20260922-NNN 播放跳过：标记删除的行区间不播出（成片效果预览） ==========
  // 与切分修剪「保留内容跳播」同口径（见 cutKeepAllFrom）：播放 rough_compose 时
  // currentTime 落进任一已删除行区间 → 跳到该块末尾。每次 timeupdate 现读 DOM —
  // 播放中翻转 🗑️/✚ 即时生效；筛选（只看不明确行）不影响跳播集合（行仍在其位）。
  function optDeletedIntervals() {  // 已删除行 + 删除子段 [start,end] 升序 + 相邻/重叠合并
    var list = revVis('slirn-opt-list');
    if (!list) return [];
    var ivs = [];
    // REQ-20260923-NNN：删除子段（.slirn-opt-subrow[data-mark="delete"]）一并跳播
    list.querySelectorAll(
      '.slirn-opt-row[data-deleted="1"][data-start-ms], .slirn-opt-subrow[data-mark="delete"][data-start-ms]'
    ).forEach(function(row) {
      var s = parseInt(row.getAttribute('data-start-ms'), 10) || 0;
      var e = parseInt(row.getAttribute('data-end-ms'), 10) || 0;
      if (e > s) ivs.push([s, e]);
    });
    ivs.sort(function(a, b) { return a[0] - b[0]; });
    var out = [];
    ivs.forEach(function(iv) {  // 块间距 ≤1ms 视为连块（跳播一次跨过）
      if (out.length && iv[0] <= out[out.length - 1][1] + 1) {
        out[out.length - 1][1] = Math.max(out[out.length - 1][1], iv[1]);
      } else out.push([iv[0], iv[1]]);
    });
    return out;
  }
  function optSkipPastDeleted(ms) {  // 起点推进：落在删除块内 → 块尾后 1ms
    var ivs = optDeletedIntervals();
    for (var i = 0; i < ivs.length; i++) {
      if (ms >= ivs[i][0] && ms < ivs[i][1]) return ivs[i][1] + 1;
    }
    return ms;
  }
  function optSkipDeletedOnTick(v) {  // 播放中跳块（暂停不跳 — 用户可停在删除段内查看）
    if (!v || v.paused) return;
    if (_optFinalVideo) return;  // REQ-20260923-NNN：最终视频已物理剪除，无块可跳（跳了反而错位）
    var ivs = optDeletedIntervals();
    if (!ivs.length) return;
    var tms = (v.currentTime || 0) * 1000;
    for (var i = 0; i < ivs.length; i++) {
      if (tms >= ivs[i][0] && tms < ivs[i][1]) {
        try { v.currentTime = (ivs[i][1] + 1) / 1000; } catch (err) {}
        return;
      }
    }
  }
  var optCutPollTimer = null;
  // 2026-09-29 用户反馈：重新拼接视频缺执行时间/进度反馈 — 格式化 + 按钮/状态条助手
  function _optCutFmtSec(s) {  // 秒 → m:ss / h:mm:ss
    s = Math.max(0, Math.round(s || 0));
    var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = s % 60;
    var two = function(x) { return (x < 10 ? '0' : '') + x; };
    return (h ? h + ':' + two(m) : '' + m) + ':' + two(ss);
  }
  function _optCutElapsedTxt(startedAt) {  // 已耗时（job.started_at 是 epoch 秒，本机同钟）
    var t = parseFloat(startedAt);
    if (!t || !isFinite(t) || t <= 0) return '';
    return '已耗时 ' + _optCutFmtSec(Date.now() / 1000 - t);
  }
  function _optCutEtaTxt(startedAt, pct) {  // 预计剩余 = elapsed/pct×(100−pct)（进度太小不稳，不显示）
    var t = parseFloat(startedAt);
    if (!t || !isFinite(t) || t <= 0 || pct < 8) return '';
    var rem = (Date.now() / 1000 - t) * (100 - pct) / pct;
    if (!isFinite(rem) || rem < 2 || rem > 6 * 3600) return '';
    return '预计还需 ' + _optCutFmtSec(rem);
  }
  function optCutRekickBtn() {  // 当前工作台的 🎬 重新拼接视频按钮
    return document.querySelector('[data-action="opt-cut-rekick"]');
  }
  function optCutBtnRunning(pct, elapsedTxt) {
    // 按钮状态机（REQ-091 教训）：反馈要出现在用户点击处 — 状态条在词频列表
    // 上方、按钮在字幕列表末尾，长列表里状态条根本看不到；按钮文字随轮询
    // 每 2s 刷新「⏳ 拼接中… X% · 已耗时 m:ss」，期间禁用防重复触发。
    var b = optCutRekickBtn();
    if (!b) return;
    if (!b.dataset.origLabel) b.dataset.origLabel = b.textContent || '';
    b.disabled = true;
    b.textContent = '⏳ 拼接中… ' + Math.round(pct) + '%'
      + (elapsedTxt ? ' · ' + elapsedTxt : '');
  }
  function optCutBtnRestore() {
    var b = optCutRekickBtn();
    if (!b) return;
    b.disabled = false;
    if (b.dataset.origLabel) {
      b.textContent = b.dataset.origLabel;
      delete b.dataset.origLabel;
    }
  }
  function startOptCutPolling(tid) {  // 优化成片剪辑轮询：done → 刷新工作台（含产物提示条）
    if (optCutPollTimer) { clearInterval(optCutPollTimer); optCutPollTimer = null; }
    var update = function() {
      postJSON(SLIRN_API + '/optimize_cut_status', {task_id: tid}).then(function(r) {
        if (!r || !r.ok) return;
        var j = r.job || {};
        var el = revVis('slirn-opt-cut-status');
        if (j.state === 'running') {
          var pct = Math.max(0, Math.min(100, j.progress || 0));
          var elTxt = _optCutElapsedTxt(j.started_at);
          var etaTxt = _optCutEtaTxt(j.started_at, pct);
          if (el) {
            el.dataset.state = 'running';
            // 2026-09-29 用户反馈：执行时间 + 进度都要可见 — 文字行（% + 已耗时 +
            // ETA + 阶段）+ 内嵌可视化进度条，轮询每 2s 刷新
            el.innerHTML = '<div class="slirn-opt-cut-line">⏳ 视频重新拼接中 · '
              + Math.round(pct) + '%'
              + (elTxt ? ' · ' + elTxt : '') + (etaTxt ? ' · ' + etaTxt : '')
              + (j.stage ? ' · ' + escapeHtml(j.stage) : '')
              + ' — 完成后精剪合成自动使用新视频</div>'
              + '<div class="slirn-opt-cut-prog"><div class="slirn-opt-cut-prog-bar" style="width:'
              + pct + '%"></div></div>';
          }
          optCutBtnRunning(pct, elTxt);
        } else {
          if (optCutPollTimer) { clearInterval(optCutPollTimer); optCutPollTimer = null; }
          optCutBtnRestore();
          if (j.state === 'done') {
            var res = j.result || {};
            if (res.cleared) {
              toast('🧹 剪辑计划已空，优化成片产物已清理');
            } else if (!res.already) {
              var _subs = res.split_subs ? ' + ' + res.split_subs + ' 子段' : '';
              var _dur = res.elapsed || ((j.started_at && j.finished_at)
                ? j.finished_at - j.started_at : 0);
              toast('🎬 优化成片已生成：剪除 ' + ((res.marks || []).length) + ' 行'
                + _subs + ' · 删 ' + (res.deleted_sec || 0)
                + 's · 保留 ' + (res.kept_sec || 0) + 's · 耗时 '
                + _optCutFmtSec(_dur) + ' — 精剪合成将自动使用');
            }
            openWorkbench(tid);  // 刷新统计 + 剪辑状态条 + 精剪素材上游
          } else if (j.state === 'error') {
            if (el) {
              el.dataset.state = 'error';
              el.innerHTML = '⚠️ 视频重新拼接失败：' + escapeHtml(j.error || '未知错误')
                + ' — 点「🎬 重新拼接视频」可再次触发';
            }
            toast('❌ 视频重新拼接失败（精剪合成将回退使用粗剪成片）', 'error');
          }
        }
      });
    };
    update();
    optCutPollTimer = setInterval(update, 2000);
  }
  function optCutRekick(btn) {  // 🎬 重新拼接视频：按当前剪辑计划（🗑️ 整行 + 删除子段）手动触发（唯一剪辑入口）
    var tid = btn.getAttribute('data-task-id') || '';
    if (!tid) return;
    if (!btn.dataset.origLabel) btn.dataset.origLabel = btn.textContent || '';
    btn.disabled = true;
    btn.textContent = '⏳ 拼接中…';  // 立刻反馈点击已注册（REQ-091 教训：不等响应）
    // 兜底：状态条缺失（理论 rekick 前必有 pending 条；若 DOM 被 local 刷新换掉）
    // → 现场补一个在操作行下方，轮询有处可写
    var st = revVis('slirn-opt-cut-status');
    if (!st) {
      st = document.createElement('div');
      st.id = 'slirn-opt-cut-status';
      st.className = 'slirn-status-msg slirn-opt-cut-status';
      st.dataset.taskId = tid;
      var act = btn.closest('.slirn-task-actions');
      var host = act && act.parentNode ? act : btn.parentNode;
      host.insertBefore(st, (act || btn).nextSibling);
    }
    postJSON(SLIRN_API + '/optimize_cut_rekick', {task_id: tid}).then(function(r) {
      if (!r || !r.ok) {
        optCutBtnRestore();
        toast('❌ ' + ((r && r.error) || '触发失败'), 'error');
        return;
      }
      if (r.toast) toast(r.toast);
      var cut = r.cut || {};
      if (cut.state === 'started' || cut.state === 'running') {
        optCutBtnRunning(0, '');  // 保持禁用，轮询接管按钮文字（%/耗时）
        startOptCutPolling(tid);
      } else if (cut.state === 'cleared') {
        optCutBtnRestore();
        openWorkbench(tid);  // 清掉旧产物 → 刷新剪辑状态条
      }
    }).catch(function() { optCutBtnRestore(); });
  }
  // REQ-20260923-NNN 查看最终视频：播放器切到 optimize_compose（时间轴已前移）—
  // 行 ms 是粗剪时间基，高亮/跳播在此模式下暂停；点任意字幕行切回粗剪源
  var _optFinalVideo = false;
  var _optLastHit = -1;  // 上次播放命中行（REQ-20260925-NNN：命中行变化才居中，防每帧回拉）
  function playOptAt(tid, startMs) {  // 行定位播放（成片时间基 — ?src=rough_compose 源）
    var wrap = revVis('slirn-opt-player-wrap');
    var v = revVis('slirn-opt-player');
    if (!v) { toast('❌ 播放器未就绪', 'error'); return; }
    if (wrap) vfShow(wrap);
    if (!v.src || _optFinalVideo) {  // 从最终视频模式回来 → 换回粗剪源
      _optFinalVideo = false;
      v.src = SLIRN_API + '/video/' + encodeURIComponent(tid) + '?src=rough_compose';
      v.load();
    }
    // REQ-20260918-054：首次绑定 timeupdate + seeked 跟高亮（幂等）
    bindOptPlayerHighlight();
    _optLastHit = -1;  // 每次点行都立刻把命中行滚到中间（REQ-20260925-NNN）
    var goO = function() {
      // REQ-20260922-NNN：起点若落在已删除块内 → 推进到块尾后（点删除行 =
      // 从其后内容播起，与切分修剪「点删除内容播下一个保留区间」同口径）
      try { v.currentTime = optSkipPastDeleted(startMs || 0) / 1000; } catch (err) {}
      // REQ-20260918-054：跳转后立即按 currentTime 重算（不等首个 timeupdate）
      optPlayerHighlight(v);
      var p = v.play();
      if (p && p.catch) p.catch(function() {});
    };
    if (v.readyState >= 1) goO();
    else v.addEventListener('loadedmetadata', goO, {once: true});
  }
  // REQ-20260918-054：按 currentTime 重算当前播放行 + 应用 .active + 自动滚到视口
  // 单行点击语义，没有 cut/rev 那样的连续跳播链；只做"高亮跟随"。
  // 2026-09-29 用户反馈：①当前播放行要「始终」位于字幕区域垂直居中 —
  //   scrollIntoView({block:'center'}) 会连动外层页面滚动（行居中的是视口，
  //   不是 560px 的字幕框，且页面会被拽走）→ 改为只滚列表容器本身；
  // ②当前播放行同时置为选中态（kbsel，与 cut/rev 播放跟随同款）。
  function optCenterRowInList(list, row) {
    if (!list || !row) return;
    try {
      var lr = list.getBoundingClientRect(), rr = row.getBoundingClientRect();
      var delta = (rr.top + rr.height / 2) - (lr.top + lr.height / 2);
      if (Math.abs(delta) < 2) return;  // 已居中，不动
      list.scrollTop += delta;  // 浏览器自动钳制到 [0, scrollHeight - clientHeight]
    } catch (err) {}
  }
  function optPlayerHighlight(v) {
    if (_optFinalVideo) return;  // REQ-20260923-NNN：最终视频时间轴已前移，行 ms 对不上
    var list = revVis('slirn-opt-list');
    if (!v || !list) return;
    var rows = Array.prototype.slice.call(
      list.querySelectorAll('.slirn-opt-row[data-start-ms]'));
    if (rows.length === 0) return;
    var tms = (v.currentTime || 0) * 1000, hit = -1;
    for (var i = 0; i < rows.length; i++) {
      var s0 = parseInt(rows[i].getAttribute('data-start-ms'), 10) || 0;
      var e0 = parseInt(rows[i].getAttribute('data-end-ms'), 10) || 0;
      // 兜底：end_ms 缺失/≤start 时用下一行 start_ms 作为上界；
      // 最后一行（无下一行）兜底为 Infinity — 让此行包到结尾，避免
      // currentTime 越过末行 start_ms 时高亮丢失
      var eEff = (e0 > s0) ? e0 :
        (i + 1 < rows.length) ?
          (parseInt(rows[i + 1].getAttribute('data-start-ms'), 10) || (s0 + 1)) :
          Infinity;
      if (tms >= s0 && tms < eEff) { hit = i; break; }
      if (s0 > tms) break;
    }
    // REQ-20260925-NNN：命中行变化才滚到中间，避免同一行内每帧 timeupdate 反复
    // 回拉用户手动滚动
    var changed = (hit !== _optLastHit);
    _optLastHit = hit;
    for (var j = 0; j < rows.length; j++) {
      rows[j].classList.toggle('active', j === hit);
      // 选中态跟随播放（cut/rev 同款）：hit=-1 的间隙里保留上一选中行
      if (hit >= 0) rows[j].classList.toggle('kbsel', j === hit);
    }
    if (changed && hit >= 0 && rows[hit]) {
      optCenterRowInList(list, rows[hit]);
    }
  }
  function bindOptPlayerHighlight() {  // 幂等：video 替换后由 v.dataset 标记防重复
    var v = revVis('slirn-opt-player');
    if (!v || v.dataset.optHLBound) return;
    v.dataset.optHLBound = '1';
    v.addEventListener('timeupdate', function() { optPlayerHighlight(v); });
    v.addEventListener('seeked',    function() { optPlayerHighlight(v); });
    // REQ-20260922-NNN：同一绑定点挂删除块跳播（幂等随 optHLBound 一起）
    v.addEventListener('timeupdate', function() { optSkipDeletedOnTick(v); });
  }
  function optBindRowClick(row, tid) {  // 单行点击定位播放（局部换行后只绑新行）
    row.addEventListener('click', function(ev) {
      // REQ-20260922-NNN：textarea（整句编辑框）也让位
      if (ev.target && ev.target.closest('button,input,textarea')) return;
      // REQ-20260922-NNN：点已删除行 → 起播点自动推进到其后（成片效果），提示一句
      // REQ-20260923-NNN：删除子段同口径
      if (row.getAttribute('data-deleted') === '1'
          || (row.classList.contains('slirn-opt-subrow')
              && row.getAttribute('data-mark') === 'delete')) {
        toast('🗑️ 本段已删除 — 从其后内容播起（成片效果）');
      }
      playOptAt(tid, parseInt(row.getAttribute('data-start-ms'), 10) || 0);
    });
  }
  function bindOptRows(tid) {  // 行点击定位播放（输入框/按钮自身不触发）
    var list = revVis('slirn-opt-list');
    if (!list) return;
    list.querySelectorAll('.slirn-opt-row[data-start-ms]').forEach(function(row) {
      optBindRowClick(row, tid);
    });
  }
  // REQ-20260922-NNN 播放暂停/停止快捷键（仅优化字幕面板）
  function optActiveRow() {  // 当前高亮行（无高亮回退首行）
    var list = revVis('slirn-opt-list');
    if (!list) return null;
    return list.querySelector('.slirn-opt-row.active') ||
           list.querySelector('.slirn-opt-row[data-start-ms]');
  }
  function optTogglePlay() {  // 播放/暂停切换：无 src 时从当前行（否则首行）起播
    var v = revVis('slirn-opt-player');
    if (!v) { toast('❌ 播放器未就绪', 'error'); return; }
    if (v.paused) {
      if (!v.src) {
        var inner = document.getElementById('slirn-tab-workbench-inner');
        var tid = inner ? (inner.getAttribute('data-task-id') || '') : '';
        if (!tid) return;
        var row = optActiveRow();
        playOptAt(tid, row ? (parseInt(row.getAttribute('data-start-ms'), 10) || 0) : 0);
        return;  // playOptAt 内部已 play
      }
      var p = v.play();
      if (p && p.catch) p.catch(function() {});
    } else {
      v.pause();
    }
  }
  function optStopPlay() {  // 停止：暂停 + 回到当前高亮行开头（无高亮回 0）
    var v = revVis('slirn-opt-player');
    if (!v) return;
    v.pause();
    var row = optActiveRow();
    var startMs = row ? (parseInt(row.getAttribute('data-start-ms'), 10) || 0) : 0;
    try { v.currentTime = startMs / 1000; } catch (err) {}
    optPlayerHighlight(v);
  }
  // REQ-20260918-050：词频列表分页（每页 20 条 = 每行 2 条 × 10 行）
  var OPT_WORDS_PAGE_SIZE = 20;
  var optWordsCurrentPage = 1;
  function setupOptWordsPagination() {  // 初始化分页（每次 wb 重渲染后调）
    var box = document.getElementById('slirn-opt-words');
    if (!box) return;
    // 用 class 找 pager（不依赖位置：colEnhance 会把 box 包进 .slirn-col，
    // 那时 box.nextElementSibling 就不是 pager；用 querySelector 永远拿得到）
    var pager = document.querySelector('.slirn-opt-words-pager');
    if (!pager) {
      pager = document.createElement('div');
      pager.className = 'slirn-opt-words-pager';
      // 紧贴 box 后面插入（"行内 …删除线…" 提示之前）。即使 colEnhance 后续
      // 把 box 包进 .slirn-col，pager 仍在 slirn-card 层级、紧跟在 col 之后，
      // 视觉上仍是「词频列表正下方」，不会跑到字幕列表下方。
      box.parentNode.insertBefore(pager, box.nextSibling);
    }
    optWordsCurrentPage = 1;  // 重置页码（wb 重渲后用户期望从头看）
    paginateOptWords();
    // REQ-20260918-057B：wb 重渲后 input 是新元素，重绑 input 事件
    bindOptWordTextFilter();
  }
  // REQ-20260918-058：替换输入框溢出检测 — scrollWidth>clientWidth 时加 .wrapped
  function optInputOverflowCheck(input) {
    if (!input) return;
    var occ = input.closest('.slirn-opt-occ');
    if (!occ) return;
    var sw = input.scrollWidth || 0;
    var cw = input.clientWidth || 0;
    var isWrapped = occ.classList.contains('wrapped');
    // REQ-20260921-NNN-shake-fix：±50px 滞回（hysteresis）打破反馈环
    // 原来用 ±2px 容差时，`:has(.wrapped)` 把第 4 列从 260px 切到 minmax(360,1fr)，
    // cw 跨度 ~100px；内容宽度落在 (cw+2, cw+102) 这段死区时，wrapped 类会
    // 在「内容>当前 cw」/「内容<新 cw」之间反复切 → grid 列宽不停变 →
    // ResizeObserver 又触发再检测 → 滚动条上下抖。
    // 50px 滞回让 wrap 触发 cw 涨 100px 后，落点必在 unwrap 阈值另一侧，状态稳定。
    if (isWrapped) {
      // 当前 wrapped → 仅显著不溢出才解除（需要 cw-50 以上的空余）
      occ.classList.toggle('wrapped', sw > cw - 50);
    } else {
      // 当前未 wrapped → 显著溢出才标记（需要 cw+50 以上的溢出）
      occ.classList.toggle('wrapped', sw > cw + 50);
    }
  }
  function optInputOverflowInit() {  // wb 重渲后调一次：检测 + 绑 input 事件
    var list = document.getElementById('slirn-opt-list');
    if (!list) return;
    var inputs = list.querySelectorAll('.slirn-opt-occ input.slirn-opt-after');
    inputs.forEach(function(inp) {
      // 多重试：font 加载 + 容器布局完成前 scrollWidth/clientWidth 可能不准
      requestAnimationFrame(function() { optInputOverflowCheck(inp); });
      setTimeout(function() { optInputOverflowCheck(inp); }, 200);
      setTimeout(function() { optInputOverflowCheck(inp); }, 800);
      if (!inp.dataset.wrapBound) {
        inp.dataset.wrapBound = '1';
        var t = null;
        inp.addEventListener('input', function() {
          if (t) clearTimeout(t);
          t = setTimeout(function() { optInputOverflowCheck(inp); }, 100);
        });
        // ResizeObserver：父容器/window resize 后重测（fallback）
        if (window.ResizeObserver && !inp.dataset.roBound) {
          inp.dataset.roBound = '1';
          try {
            var ro = new ResizeObserver(function() {
              setTimeout(function() { optInputOverflowCheck(inp); }, 50);
            });
            ro.observe(inp);
          } catch (e) { /* ignore */ }
        }
      }
    });
  }
  function paginateOptWords() {
    var box = document.getElementById('slirn-opt-words');
    if (!box) return;
    var pager = document.querySelector('.slirn-opt-words-pager');
    var all = Array.prototype.slice.call(box.querySelectorAll('.slirn-opt-word'));
    // 过滤当前「应该被分页的」词：optWordFilterBtn 用 display:none 隐藏未选中词。
    // 注意不能用 slirn-opt-word-hidden 来过滤——分页自己也用它，否则翻页后会
    // 把「分页隐藏的词」从计数里去掉，total 会越翻越少，最终 total <= 20 触
    // 发「隐藏分页控件」分支、把分页器清空，再也翻不回去。
    var visible = all.filter(function(el) {
      return el.style.display !== 'none';
    });
    var total = visible.length;
    var pages = Math.max(1, Math.ceil(total / OPT_WORDS_PAGE_SIZE));
    if (optWordsCurrentPage > pages) optWordsCurrentPage = pages;

    // 隐藏非当前页
    visible.forEach(function(el, i) {
      var pageIdx = Math.floor(i / OPT_WORDS_PAGE_SIZE);
      el.classList.toggle('slirn-opt-word-hidden', pageIdx !== optWordsCurrentPage - 1);
    });

    // 更新分页控件（pager 用 class 找，colEnhance wrap 不影响）
    if (!pager) return;
    if (total <= OPT_WORDS_PAGE_SIZE) {
      pager.style.display = 'none';
      pager.innerHTML = '';
      return;
    }
    pager.style.display = '';
    pager.innerHTML =
      '<button type="button" data-action="opt-page-prev"' + (optWordsCurrentPage <= 1 ? ' disabled' : '') + '>‹ 上一页</button>'
      + '<span class="slirn-opt-page-info">第 ' + optWordsCurrentPage + ' / ' + pages + ' 页 · 共 ' + total + ' 个词</span>'
      + '<button type="button" data-action="opt-page-next"' + (optWordsCurrentPage >= pages ? ' disabled' : '') + '>下一页 ›</button>';
  }
  // 保存切分决策：全量收集子段 mark + 组级 action（改判 split 附切分后内容）→ 落盘
  // 收集逻辑抽成 cutCollectSave，供手动保存（cutSave）与自动保存（cutAutoSave）复用
  function cutCollectSave() {
    var marks = {};
    cutRows().forEach(function(r) {
      if (r.classList.contains('sub') && r.getAttribute('data-id'))
        marks[r.getAttribute('data-id')] = r.getAttribute('data-mark') || 'keep';
    });
    var acts = {}, targets = {}, badSplit = null;
    document.querySelectorAll('#slirn-cut-list .slirn-cut-group[data-act]').forEach(function(g) {
      acts[g.getAttribute('data-source-i') || ''] = g.getAttribute('data-act');
    });
    // 改判切分必须有切分后内容：组 data-target（重切/恢复）或当前编辑区输入值
    document.querySelectorAll('#slirn-cut-list .slirn-cut-group[data-act="split"]').forEach(function(g) {
      var tv = (g.getAttribute('data-target') || '').trim();
      if (!tv) {
        var inp = document.querySelector('.slirn-cut-resplit-input');
        if (inp && inp.closest('.slirn-cut-group') === g) tv = inp.value.trim();
      }
      if (!tv) badSplit = g.getAttribute('data-source-i');
      targets[g.getAttribute('data-source-i') || ''] = tv;
    });
    return {marks: marks, acts: acts, targets: targets, badSplit: badSplit};
  }
  function cutTaskId() {  // 当前工作台任务 id（自动保存不持有按钮/行引用时用）
    var wb = document.getElementById('slirn-tab-workbench-inner');
    return wb ? (wb.getAttribute('data-task-id') || '') : '';
  }
  // REQ-20260925-NNN：决策改判后台自动保存（静默，不 openWorkbench — 不断播放）
  // 仿 optAutoSave 的 in-flight 锁 + pending 快照，连续改判只留最后一次
  var _cutSaveInFlight = false;
  var _cutSavePending = false;
  function cutAutoSave() {
    var tid = cutTaskId();
    if (!tid) return;
    if (_cutSaveInFlight) { _cutSavePending = true; return; }
    var c = cutCollectSave();
    if (c.badSplit !== null) return;  // 改判 split 未填内容：静默跳过（重新切分/手动保存兜底）
    _cutSaveInFlight = true;
    postJSON(SLIRN_API + '/save_cut_decisions',
             {task_id: tid, manual_marks: c.marks, actions: c.acts, split_targets: c.targets})
      .then(function(r) {
        if (r && r.ok) toast('✅ 已自动保存', 'success');
        else if (r && r.error) toast('❌ 自动保存失败：' + r.error, 'error');
      })
      .catch(function() { toast('❌ 自动保存失败（网络错误）', 'error'); })
      .then(function() {
        _cutSaveInFlight = false;
        if (_cutSavePending) { _cutSavePending = false; cutAutoSave(); }
      });
  }
  function cutSave(btn) {  // 手动保存：落盘 + 刷新阶段态（进入优化字幕）— 仍 openWorkbench
    var tid = btn.getAttribute('data-task-id') || '';
    var c = cutCollectSave();
    if (c.badSplit !== null) {
      toast('❌ 第 ' + c.badSplit + ' 条已改判切分，但未填写切分后内容 — 按 S 或点 ✂️ 填写后重切', 'error');
      return;
    }
    postJSON(SLIRN_API + '/save_cut_decisions',
             {task_id: tid, manual_marks: c.marks, actions: c.acts, split_targets: c.targets})
      .then(function(r) {
        if (r && r.ok) { toast(r.toast || '切分决策已保存'); openWorkbench(tid); }
        else if (r && r.error) toast('❌ ' + r.error, 'error');
      });
  }
  // 播放器绑定：timeupdate 三合一 — 试听跳播 / 预播段尾自动停 / 高亮跟随
  function bindCutPlayer() {
    var v = revVis('slirn-cut-player');
    var list = revVis('slirn-cut-list');
    bindSpeedControl(v);  // 倍速显示与控制（REQ-20260916-014）
    if (v && list && !v.dataset.bound) {
      v.dataset.bound = '1';
      var rows = Array.prototype.slice.call(list.querySelectorAll('.slirn-cut-row'));
      var lastHit = -1;
      // ② 高亮跟随（与字幕/修订阶段同款 active：段间缝隙保持前一段亮）—
      // 抽成闭包供 timeupdate 与 seeked 共用（seek 落点即时高亮，REQ-20260922-NNN）
      var cutHL = function(tms) {
        var hit = -1;
        for (var i = 0; i < rows.length; i++) {
          var s0 = parseInt(rows[i].getAttribute('data-start-ms'), 10) || 0;
          var e0 = parseInt(rows[i].getAttribute('data-end-ms'), 10) || 0;
          if (tms >= s0 && tms < e0) { hit = i; break; }
          if (s0 > tms) break;
        }
        if (hit === -1 && lastHit >= 0) {
          var eh = parseInt(rows[lastHit].getAttribute('data-end-ms'), 10) || 0;
          var nh = (lastHit + 1 < rows.length)
            ? (parseInt(rows[lastHit + 1].getAttribute('data-start-ms'), 10) || 0)
            : Infinity;
          if (tms >= eh && tms < nh) hit = lastHit;
        }
        // REQ-20260925-NNN：选中态（kbsel）跟随播放 — 播放到哪条就把哪条置为选中，
        // 并滚到列表中间（命中行变化才滚，防每帧回拉用户手动滚动）。
        var changed = (hit !== lastHit);
        lastHit = hit;
        for (var j = 0; j < rows.length; j++) {
          rows[j].classList.toggle('active', j === hit);
          if (hit >= 0) rows[j].classList.toggle('kbsel', j === hit);
        }
        if (changed && hit >= 0 && rows[hit] && rows[hit].scrollIntoView) {
          rows[hit].scrollIntoView({block: 'center'});
        }
      };
      v.addEventListener('timeupdate', function() {
        var tms = v.currentTime * 1000;
        // ① 试听跳播（REQ-20260916-012 索引跟踪）：只看当前段 — 播过当前 keep
        // 段尾 → seek 下一段起点；播完最后一段 → 停。绝不从 0 重扫：旧行为里
        // 跳到下一段后 tms ≥ 前段尾恒成立，会立刻再跳、末组折返，段间无限
        // 乒乓反复播放且永不结束。
        // 序列来源（REQ-20260916-015）：组头试听（cutKeepMode='group'，显示成片
        // 试听条）或行级连续播放（'row'，只播保留内容、不显示试听条）。
        if (cutKeepSeq) {
          var seg = cutKeepSeq[cutKeepIdx];
          if (tms < seg.s - 500) {  // 用户回拖：重定位到 tms 所在段
            while (cutKeepIdx > 0 && tms < cutKeepSeq[cutKeepIdx].s) cutKeepIdx--;
            seg = cutKeepSeq[cutKeepIdx];
          }
          if (tms >= seg.e - 30) {
            if (cutKeepIdx + 1 < cutKeepSeq.length) {
              cutKeepIdx++;
              var nxt = cutKeepSeq[cutKeepIdx];
              try { v.currentTime = nxt.s / 1000; } catch (err) {}
              // REQ-20260925-NNN：跳播跳转本身不直接改选中/高亮，return 后由下一帧
              // timeupdate 的 cutHL 统一把 .active/.kbsel 移到实际播放行（选中跟随播放）
              if (cutKeepMode === 'group') cutAuditionBar(v);
              return;  // 跳转后的首个 timeupdate 再走高亮，防旧位置误亮
            }
            v.pause(); cutKeepSeq = null; cutKeepMode = null;
            cutAuditionBar(null);  // 播放完毕：试听结束收起
          } else {
            if (cutKeepMode === 'group') cutAuditionBar(v);  // 试听时间戳与当前段保持一致
          }
        }
        cutHL(tms);
      });
      // seek 落点即时处理（REQ-20260922-NNN）：跳播的 seek 与用户拖动条落下时
      // timeupdate 可能滞后一拍 — ① 跳播索引按落点绝对重定位（第一个「末尾在
      // 落点 30ms 后」的保留段 = 落点所在段或其后第一段；前拖/后拖/拖进删除洞
      // 都收敛，链式跳播自身触发的 seek 落点即目标段，重定位结果不变）；落点
      // 在首个保留段前 500ms 以外 → 直接吸到首段（片头无字幕内容不播）；
      // ② 高亮即时跟随（原实现要等下一个 timeupdate，跳变瞬间亮的是旧行）
      v.addEventListener('seeked', function() {
        var tmsS = v.currentTime * 1000;
        if (cutKeepSeq) {
          if (tmsS < cutKeepSeq[0].s - 500) {
            try { v.currentTime = cutKeepSeq[0].s / 1000; } catch (err) {}
            return;  // 吸附引发的 seeked 再走高亮
          }
          var i2 = 0;
          while (i2 < cutKeepSeq.length - 1 && tmsS >= cutKeepSeq[i2].e - 30) i2++;
          cutKeepIdx = i2;
        }
        cutHL(tmsS);
      });
    }
  }

  function bindRevPlayer() {
    var v = revVis('slirn-rev-player');
    var list = revVis('slirn-rev-list');
    bindSpeedControl(v);  // 倍速显示与控制（REQ-20260916-014）
    if (v && list && !v.dataset.bound) {
      v.dataset.bound = '1';
      revPlaySeq = null;  // 面板重建（新视频元素）→ 旧链作废，防 stale 行时间戳（REQ-20260917-028）
      var rows = Array.prototype.slice.call(list.querySelectorAll('.slirn-rev-row'));
      var lastHit = -1;
      var setActive = function(idx) {
        for (var i = 0; i < rows.length; i++) rows[i].classList.toggle('active', i === idx);
        if (idx >= 0 && rows[idx] && rows[idx].scrollIntoView) rows[idx].scrollIntoView({block: 'nearest'});
      };
      v.addEventListener('timeupdate', function() {
        var tms = v.currentTime * 1000, hit = -1;
        // ① 连续跳播（REQ-20260917-028）：本条到尾 → seek 下一条起点（段间空白
        // 不播）；回拖/前拖按时间重定位链位置；播完最后一条停
        if (revPlaySeq && revPlaySeq.length) {
          if (tms < revPlaySeq[revPlayIdx].s - 500) {
            while (revPlayIdx > 0 && tms < revPlaySeq[revPlayIdx].s) revPlayIdx--;
          } else {
            while (revPlayIdx + 1 < revPlaySeq.length && tms >= revPlaySeq[revPlayIdx + 1].s) revPlayIdx++;
          }
          var seg = revPlaySeq[revPlayIdx];
          if (tms >= seg.e - 30) {
            if (revPlayIdx + 1 < revPlaySeq.length) {
              revPlayIdx++;
              try { v.currentTime = revPlaySeq[revPlayIdx].s / 1000; } catch (err) {}
              return;  // 跳转后的首个 timeupdate 再走高亮，防旧位置误亮
            }
            v.pause(); revPlaySeq = null;  // 最后一条播完：自动停
          }
        }
        // ② 高亮跟随（与字幕/切分阶段同款 active：段间缝隙保持前一段亮）
        for (var i = 0; i < rows.length; i++) {
          var s0 = parseInt(rows[i].getAttribute('data-start-ms'), 10) || 0;
          var e0 = parseInt(rows[i].getAttribute('data-end-ms'), 10) || 0;
          if (tms >= s0 && tms < e0) { hit = i; break; }
          if (s0 > tms) break;
        }
        if (hit === -1 && lastHit >= 0) {
          var eh = parseInt(rows[lastHit].getAttribute('data-end-ms'), 10) || 0;
          var nh = (lastHit + 1 < rows.length)
            ? (parseInt(rows[lastHit + 1].getAttribute('data-start-ms'), 10) || 0)
            : Infinity;
          if (tms >= eh && tms < nh) hit = lastHit;
        }
        lastHit = hit;
        setActive(hit);
      });
    }
    // 工作台（重新）打开时，若 job 还在跑 → 恢复轮询
    var st = document.getElementById('slirn-rev-status');
    if (st && st.dataset.taskId && st.dataset.state === 'running') startRevPolling(st.dataset.taskId);
    applyRevKeysState();  // 提示条跟随自定义键位（localStorage — REQ-20260916-005）
    applyOptKeysState();  // 优化字幕提示条（REQ-20260922-NNN）
    revFilterSync();  // 过滤条：渲染 chips 计数 + 应用上次筛选（localStorage — REQ-20260916-010）
  }

  // ===== 字幕修订快捷键（REQ-20260916-004）：听 → 判 → 标记 → 下一条 =====
  // ↑↓ 选行（跳到行起点，播放态跟随）· 空格 播放/暂停 · R 重播本行
  // K 保留 / D 删除（标记后自动下一条）· S 切分（展开详情+聚焦内容输入，不跳行）
  // Esc 从切分修剪后内容输入框退回列表；输入框/下拉聚焦时不劫持按键
  function revRows() {
    var list = revVis('slirn-rev-list');
    if (!list || !list.offsetParent) return [];  // 列表不可见 → 快捷键整体不生效
    return Array.prototype.slice.call(list.querySelectorAll('.slirn-rev-row'));
  }
  function revSelIndex(rows) {
    for (var i = 0; i < rows.length; i++) { if (rows[i].classList.contains('kbsel')) return i; }
    return -1;
  }
  function revMarkSel(row) {
    revRows().forEach(function(r) { r.classList.toggle('kbsel', r === row); });
    if (row && row.scrollIntoView) row.scrollIntoView({block: 'nearest'});
  }
  function revSelectRow(idx, seek) {
    var rows = revNavRows();  // 过滤后可见行（REQ-20260916-010）：↑↓ 跳过被筛掉的行
    if (!rows.length) return null;
    var cur = revSelIndex(rows);
    if (cur < 0) idx = (idx < 0) ? rows.length - 1 : 0;  // 无选中：↓ 取第一行，↑ 取最后一行
    idx = Math.max(0, Math.min(rows.length - 1, idx));
    var row = rows[idx];
    revMarkSel(row);
    if (seek) {
      var v = revVis('slirn-rev-player');
      if (v && v.src) {  // 播放器加载过才跳（暂停时不强制播放，按空格续听）
        try { v.currentTime = (parseInt(row.getAttribute('data-start-ms'), 10) || 0) / 1000; } catch (err) {}
      }
    }
    return row;
  }
  function revTogglePlay() {
    var v = revVis('slirn-rev-player');
    if (!v) return;
    if (!v.src) {  // 从未播放过：从选中行（或第一行）起点开播（连续跳播 — REQ-20260917-028）
      var rows = revNavRows();
      var row = rows[revSelIndex(rows)] || rows[0];
      if (row) revPlayFrom(row);
      return;
    }
    if (v.paused) { var p = v.play(); if (p && p.catch) p.catch(function() {}); }
    else { v.pause(); }
  }
  function revReplayRow() {
    var rows = revNavRows();
    var row = rows[revSelIndex(rows)];
    if (!row) {
      var kmR = revKeysLoad();
      toast('⌨ 先用 ' + revKeyLabel(kmR.prev) + ' / ' + revKeyLabel(kmR.next) + ' 选择一条字幕', 'error');
      return;
    }
    revPlayFrom(row);  // 重播本条并续链（REQ-20260917-028：播完跳下一条，空白不播）
  }
  function revApplyDecision(val) {
    var rows = revNavRows();
    var idx = revSelIndex(rows);
    if (idx < 0) {
      var kmA = revKeysLoad();
      toast('⌨ 先用 ' + revKeyLabel(kmA.prev) + ' / ' + revKeyLabel(kmA.next) + ' 选择一条字幕，再按 '
        + revKeyLabel(kmA.keep) + ' / ' + revKeyLabel(kmA.del) + ' / ' + revKeyLabel(kmA.split) + ' 标记', 'error');
      return;
    }
    var row = rows[idx];
    var sel = row.querySelector('.slirn-rev-select');
    if (!sel) return;
    sel.value = val;
    sel.dispatchEvent(new Event('change', {bubbles: true}));
    if (val === 'split') {
      // 切分需要人工给出修剪后文本：展开详情块、光标移到输入框，不自动跳行（写完按 Esc 返回）
      row.classList.add('open');
      var tg = row.querySelector('.slirn-rev-toggle');
      if (tg) tg.textContent = '▴';
      var note = row.querySelector('.slirn-rev-note-input');
      if (note) {
        note.focus();
        try { note.setSelectionRange(note.value.length, note.value.length); } catch (err) {}
      }
      toast('✂️ 已标记切分 — 填写切分修剪后内容后按 ' + revKeyLabel(revKeysLoad().esc) + ' 返回列表');
    } else {
      revSelectRow(idx + 1, true);  // 保留/删除：标记即过，自动下一条
    }
  }

  // ===== 修订列表状态过滤（REQ-20260916-010）=====
  // 两个维度：建议状态（模型判定）/ 决策状态（用户选择）。决策维度按「实质
  // 类别」匹配 — accept 的实质 = 模型建议类别（与切分清单 _final_kind 同源）：
  // 筛「切分」时「采纳+建议切分」的行也命中。筛选只藏行不删行，
  // 「保存修订决策」仍收集全部行（revRows 不受过滤影响）。
  var REV_FILTER_SUGG = [
    ['keep', '保留'], ['delete', '删除'], ['split', '切分'],
    ['fix', '更正'], ['review', '复核'],
  ];
  var REV_FILTER_DEC = [
    ['pending', '未决策'], ['accept', '采纳建议'],
    ['keep', '保留'], ['delete', '删除'], ['split', '切分'], ['fix', '内容更正'],
  ];
  function revFilterLoad() {
    try { return JSON.parse(localStorage.getItem('slirnRevFilter') || 'null'); }
    catch (err) { return null; }
  }
  var revFilter = revFilterLoad() || { dim: 'sugg', val: 'all' };
  if (revFilter.dim !== 'sugg' && revFilter.dim !== 'dec') revFilter = { dim: 'sugg', val: 'all' };
  function revFilterSave() {
    try { localStorage.setItem('slirnRevFilter', JSON.stringify(revFilter)); } catch (err) {}
  }
  function revNavRows() {  // 过滤后仍可见的行（导航/标记走这里；保存收集仍用 revRows 全量）
    return revRows().filter(function(r) { return r.style.display !== 'none'; });
  }
  function revMatchKind(row, dim, val) {  // 统一匹配口径（筛选与跳转共用）
    if (val === 'all') return true;
    if (dim === 'sugg') return (row.getAttribute('data-sugg') || '') === val;
    if (val === 'pending' || val === 'accept')
      return (row.getAttribute('data-decision') || '') === val;
    return (row.getAttribute('data-final') || '') === val;  // 实质口径
  }
  function revFilterMatch(row) { return revMatchKind(row, revFilter.dim, revFilter.val); }
  function revFilterCountOf(rows, dim, val) {
    return rows.filter(function(r) { return revMatchKind(r, dim, val); }).length;
  }
  function revFilterRender() {
    var chips = revVis('slirn-rev-filter-chips');
    if (!chips) return;
    var rows = revRows();
    var defs = revFilter.dim === 'sugg' ? REV_FILTER_SUGG : REV_FILTER_DEC;
    var html = '<button type="button" class="slirn-rev-chip' + (revFilter.val === 'all' ? ' active' : '')
      + '" data-rev-filter-val="all">全部 ' + rows.length + '</button>';
    defs.forEach(function(d) {
      var n = revFilterCountOf(rows, revFilter.dim, d[0]);
      var solid = revFilter.dim === 'dec' && d[0] !== 'pending' && d[0] !== 'accept';
      html += '<button type="button" class="slirn-rev-chip' + (revFilter.val === d[0] ? ' active' : '')
        + '" data-rev-filter-val="' + d[0] + '"'
        + (solid ? ' title="实质口径：含「采纳建议」且建议为此类的行"' : '')
        + '>' + d[1] + ' ' + n + '</button>';
    });
    chips.innerHTML = html;
    document.querySelectorAll('[data-rev-filter-dim]').forEach(function(b) {
      b.classList.toggle('active', b.getAttribute('data-rev-filter-dim') === revFilter.dim);
    });
  }
  function revFilterApply() {
    if (!revVis('slirn-rev-filter')) return;
    var rows = revRows(), shown = 0;
    rows.forEach(function(r) {
      var ok = revFilterMatch(r);
      r.style.display = ok ? '' : 'none';
      if (ok) shown++;
    });
    var cnt = revVis('slirn-rev-filter-count');
    if (cnt) cnt.textContent = '显示 ' + shown + ' / ' + rows.length + ' 条';
  }
  function revFilterSync() { revFilterRender(); revFilterApply(); revJumpRenderSel(); }

  // ===== 批量改判（REQ-20260918-039）：起止序号 + 目标状态，确认后一次改一批 =====
  // 与单条修改同口径：纯前端状态（保存前可继续调整），落盘走各阶段既有保存按钮
  function batchRange(box, sId, eId) {  // 读起止输入 → {s,e} | null（非法先 toast 报错）
    var sEl = box.querySelector('#' + sId), eEl = box.querySelector('#' + eId);
    var s = parseFloat(sEl && sEl.value), e = parseFloat(eEl && eEl.value);
    if (!isFinite(s) || !isFinite(e) || s > e || s < 0) {
      toast('❌ 批量区间不正确：起止都要是数字，且 起 ≤ 止', 'error');
      return null;
    }
    return { s: s, e: e };
  }
  function revBatchApply() {  // 字幕修改：区间内行决策 → 目标（data-final 实质口径同步）
    var box = document.getElementById('slirn-rev-batch');
    if (!box) return;
    var rg = batchRange(box, 'slirn-rev-batch-start', 'slirn-rev-batch-end');
    if (!rg) return;
    var selT = box.querySelector('#slirn-rev-batch-sel');
    var tv = selT ? selT.value : '';
    if (!tv) { toast('❌ 请选择目标状态', 'error'); return; }
    var label = (selT && selT.options[selT.selectedIndex])
      ? selT.options[selT.selectedIndex].textContent : tv;
    var rows = [];
    var all = document.querySelectorAll('#slirn-rev-list .slirn-rev-row');
    for (var i = 0; i < all.length; i++) {
      var sel = all[i].querySelector('.slirn-rev-select');
      var si = parseInt(sel && sel.getAttribute('data-i'), 10);
      if (isFinite(si) && si >= rg.s && si <= rg.e) rows.push(all[i]);
    }
    if (!rows.length) {
      toast('❌ 区间 ' + rg.s + ' ～ ' + rg.e + ' 内没有字幕行', 'error');
      return;
    }
    if (!window.confirm('把第 ' + rg.s + ' ～ ' + rg.e + ' 条（共 ' + rows.length
        + ' 条）的决策改为「' + label + '」？\n（未保存 — 保存前可继续调整）')) return;
    rows.forEach(function(row) {
      var selR = row.querySelector('.slirn-rev-select');
      if (selR) selR.value = tv;
      var sugg = row.getAttribute('data-sugg') || '';
      var fin = '';  // 实质口径 — 与决策 change 委托同算法
      if (tv === 'keep' || tv === 'delete' || tv === 'split' || tv === 'fix') fin = tv;
      else if (tv === 'accept' && (sugg === 'keep' || sugg === 'delete'
          || sugg === 'split' || sugg === 'fix')) fin = sugg;
      row.setAttribute('data-decision', tv);
      row.setAttribute('data-final', fin);
      // split/fix：空备注从模型建议预填（与单条 change 同源；不展开不聚焦 — 批量不抢焦点）
      var note = row.querySelector('.slirn-rev-note-input');
      if (note && (tv === 'split' || tv === 'fix') && !note.value.trim()) {
        var kt = row.querySelector('.slirn-rev-keeptext');
        var m = kt && /「(.+)」/.exec(kt.textContent || '');
        if (m) note.value = m[1];
      }
    });
    revFilterSync();
    toast('🧮 已批量设置 ' + rows.length + ' 条 →「' + label + '」（未保存 — 记得保存修订决策）');
  }
  function cutBatchApply() {  // 切分修剪：整段行=组级决策 / 子段行=去留标记
    var box = document.getElementById('slirn-cut-batch');
    if (!box) return;
    var rg = batchRange(box, 'slirn-cut-batch-start', 'slirn-cut-batch-end');
    if (!rg) return;
    var selT = box.querySelector('#slirn-cut-batch-sel');
    var tv = selT ? selT.value : 'keep';
    var label = (selT && selT.options[selT.selectedIndex])
      ? selT.options[selT.selectedIndex].textContent : tv;
    // 行命中：行号数值（10 / 10.1）∈ 区间，或所属父段号 ∈ 区间
    //（"第 10 到 10 条"覆盖该段全部子段 — 与界面所见编号对应）
    var wholes = [], subs = [];
    var all = document.querySelectorAll('#slirn-cut-list .slirn-cut-row');
    for (var i = 0; i < all.length; i++) {
      var r = all[i];
      var id = parseFloat(r.getAttribute('data-id'));
      var src = parseInt(r.getAttribute('data-source-i'), 10);
      var hit = (isFinite(id) && id >= rg.s && id <= rg.e)
             || (isFinite(src) && src >= rg.s && src <= rg.e);
      if (!hit) continue;
      if (r.classList.contains('sub')) subs.push(r);
      else wholes.push(r);
    }
    var n = wholes.length + subs.length;
    if (!n) {
      toast('❌ 区间 ' + rg.s + ' ～ ' + rg.e + ' 内没有切分行', 'error');
      return;
    }
    var parts = [];
    if (wholes.length) parts.push('整段行 ' + wholes.length + ' 条（改组级决策）');
    if (subs.length) parts.push('子段行 ' + subs.length + ' 条（改去留标记）');
    if (!window.confirm('把第 ' + rg.s + ' ～ ' + rg.e + ' 条改为「' + label + '」？\n'
        + parts.join(' · ') + '\n（保存前可继续调整）')) return;
    wholes.forEach(function(r) {  // 整段行 → 组级决策（与 actsel 同效；静默不逐条 toast）
      var g = r.closest('.slirn-cut-group');
      if (!g) return;
      if (tv) g.setAttribute('data-act', tv);
      else g.removeAttribute('data-act');  // 维持原状 = 取消改判
      var b = g.querySelector('[data-actsel]');
      if (b) b.value = tv;
    });
    subs.forEach(function(r) {  // 子段行 → 去留（维持原状 = 回初始建议标记）
      cutSpkSetMark(r, tv || r.getAttribute('data-mark-init') || 'keep');
    });
    var spkBar = document.getElementById('slirn-cut-spk-bar');  // 仅计未删除口径跟随刷新
    if (spkBar && spkBar.getAttribute('data-linked') === '1') cutSpkBarRender();
    toast('🧮 已批量设置 ' + n + ' 条 →「' + label + '」');
    cutAutoSave();
  }

  // ===== 状态跳转（REQ-20260917-025）：独立状态选择列表 + 完整列表内跳上/下一条 =====
  // 跳转目标由「⬆/⬇ 按钮前」的状态下拉显式指定（与筛选 chips 完全解耦）：
  // 跳转时显示全部行——目标行的上下文语句可见，便于结合前后文决定处理；
  // 筛选 chips 仍是纯视图收窄（会藏行）。kbsel 高亮目标行并居中滚动；
  // 播放器已加载则定位到行起点（与 ↑↓ 键同源）。匹配口径与筛选项完全
  // 同源（revMatchKind）；选「全部」时退化为整表上一/下一条。
  var revJumpSel = null;  // {dim, val}：跳转目标状态（localStorage 持久化，与筛选独立）
  var revJumpIdx = -1;    // 上次跳转命中的行（revRows 全量下标）；-1 = 未跳过
  function revJumpSelLoad() {
    try {
      var s = JSON.parse(localStorage.getItem('slirnRevJump') || 'null');
      if (s && (s.dim === 'sugg' || s.dim === 'dec') && typeof s.val === 'string') return s;
    } catch (err) {}
    return null;
  }
  function revJumpSelSave() {
    try { localStorage.setItem('slirnRevJump', JSON.stringify(revJumpSel)); } catch (err) {}
  }
  function revJumpLabel() {
    if (!revJumpSel || revJumpSel.val === 'all')
      return (revJumpSel && revJumpSel.dim === 'sugg' ? '建议' : '决策') + ' · 全部';
    var defs = revJumpSel.dim === 'sugg' ? REV_FILTER_SUGG : REV_FILTER_DEC;
    for (var i = 0; i < defs.length; i++) if (defs[i][0] === revJumpSel.val)
      return (revJumpSel.dim === 'sugg' ? '建议 · ' : '决策 · ') + defs[i][1];
    return revJumpSel.val;
  }
  function revJumpRenderSel() {  // 渲染/恢复跳转状态下拉（面板重建后幂等）
    var sel = revVis('slirn-rev-jump-sel');
    if (!sel) return;
    if (!revJumpSel) revJumpSel = revJumpSelLoad() || { dim: 'dec', val: 'pending' };
    if (!sel.options.length) {
      var html = '<optgroup label="建议状态"><option value="sugg:all">建议 · 全部</option>';
      REV_FILTER_SUGG.forEach(function(d) {
        html += '<option value="sugg:' + d[0] + '">建议 · ' + d[1] + '</option>';
      });
      html += '</optgroup><optgroup label="决策状态"><option value="dec:all">决策 · 全部</option>';
      REV_FILTER_DEC.forEach(function(d) {
        html += '<option value="dec:' + d[0] + '">决策 · ' + d[1] + '</option>';
      });
      html += '</optgroup>';
      sel.innerHTML = html;
    }
    sel.value = revJumpSel.dim + ':' + revJumpSel.val;
    if (!sel.getAttribute('data-bound')) {  // change 监听只绑一次（面板重建后新元素重绑）
      sel.setAttribute('data-bound', '1');
      sel.addEventListener('change', function() {
        var p = (sel.value || 'dec:pending').split(':');
        revJumpSel = { dim: p[0], val: p[1] };
        revJumpSelSave();
        revJumpIdx = -1;  // 换跳转状态：锚点重置
      });
    }
  }
  function revJump(dir) {
    revJumpRenderSel();
    if (!revJumpSel) return;
    var rows = revRows();
    if (!rows.length) return;
    rows.forEach(function(r) { r.style.display = ''; });  // 显示全部：上下文可见
    var cnt = revVis('slirn-rev-filter-count');
    var matches = [];
    for (var i = 0; i < rows.length; i++)
      if (revMatchKind(rows[i], revJumpSel.dim, revJumpSel.val)) matches.push(i);
    if (!matches.length) {
      if (cnt) cnt.textContent = '「' + revJumpLabel() + '」无匹配 · 显示全部 ' + rows.length + ' 条';
      toast('没有「' + revJumpLabel() + '」状态的行', 'error');
      return;
    }
    // 起点：上次跳转行 → 键盘选中行 → 首条（下一条）/ 末条（上一条）
    var start = (revJumpIdx >= 0 && revJumpIdx < rows.length) ? revJumpIdx : revSelIndex(rows);
    var cur = matches.indexOf(start);
    var nextPos = cur >= 0 ? (cur + dir + matches.length) % matches.length
                           : (dir > 0 ? 0 : matches.length - 1);
    if (cur >= 0 && matches.length > 1 &&
        ((dir > 0 && nextPos === 0) || (dir < 0 && nextPos === matches.length - 1)))
      toast('已到「' + revJumpLabel() + '」的' + (dir > 0 ? '最后' : '第一') + '一条，继续将循环跳转');
    var idx = matches[nextPos];
    revJumpIdx = idx;
    revMarkSel(rows[idx]);
    if (rows[idx].scrollIntoView)
      rows[idx].scrollIntoView({block: 'center', behavior: 'smooth'});
    var v = revVis('slirn-rev-player');
    if (v && v.src) {  // 播放器加载过才定位（暂停时不强制播放，按空格续听）
      try { v.currentTime = (parseInt(rows[idx].getAttribute('data-start-ms'), 10) || 0) / 1000; } catch (err) {}
    }
    if (cnt) cnt.textContent = '跳转「' + revJumpLabel() + '」' + (nextPos + 1) + '/' + matches.length
      + ' · 显示全部 ' + rows.length + ' 条（上下文可见）';
  }

  // ===== 快捷键自定义（REQ-20260916-005）：localStorage 键 slirnRevKeys =====
  // 每个人习惯不同：点击提示条「⚙ 自定义」→ 点键帽 → 按新键。键位图持久化，
  // 提示条/引导 toast 跟随当前键位渲染；冲突拒绝、可恢复默认。
  var REV_KEY_ACTIONS = [
    { id: 'prev',   name: '上一条',                   def: 'arrowup' },
    { id: 'next',   name: '下一条',                   def: 'arrowdown' },
    { id: 'play',   name: '播放 / 暂停',               def: ' ' },
    { id: 'replay', name: '重播本行',                  def: 'r' },
    { id: 'keep',   name: '保留（标记后自动下一条）',   def: 'k' },
    { id: 'del',    name: '删除（标记后自动下一条）',   def: 'd' },
    { id: 'split',  name: '切分（展开详情并聚焦内容）', def: 's' },
    { id: 'esc',    name: '退出说明输入框',             def: 'escape' },
    // REQ-20260922-NNN：优化字幕专用（修订/切分 handler 反查到 stop 无分支 → no-op）
    { id: 'stop',   name: '停止（优化字幕：暂停并回到本行开头）', def: 'x' }
  ];
  var REV_KEY_ALLOWED = ['arrowup', 'arrowdown', 'arrowleft', 'arrowright', 'escape',
    'enter', 'backspace', 'delete', 'home', 'end', 'pageup', 'pagedown'];  // 单字符键另判
  // REQ-20260918-042：视频播放器原生快捷键（Chrome/Edge，视频获得焦点时生效）。
  // 应用键与之重叠 → 弹窗内亮 ⚠ 徽章 + 绑定时 toast 提醒（允许绑定，仅提醒）：
  // 列表可见时应用键优先，该键在视频聚焦时不再触发原生播放控制。
  var REV_VIDEO_KEYS = {
    ' ': '播放 / 暂停', 'k': '播放 / 暂停',
    'arrowleft': '后退 5 秒', 'arrowright': '前进 5 秒',
    'j': '后退 10 秒', 'l': '前进 10 秒',
    'arrowup': '音量 +', 'arrowdown': '音量 -',
    'm': '静音切换', 'f': '全屏切换', 'c': '字幕开关',
    '0': '跳到 0%', '1': '跳到 10%', '2': '跳到 20%', '3': '跳到 30%',
    '4': '跳到 40%', '5': '跳到 50%', '6': '跳到 60%', '7': '跳到 70%',
    '8': '跳到 80%', '9': '跳到 90%',
    'home': '回到开头', 'end': '跳到片尾'
  };
  // REQ-20261022-146：把动作表与「动作 → 当前键位」暴露给硬件按键模块，
  // 让硬件键能派发对应的合成 keydown（改绑键位后硬件键自动跟随）。
  function revKeysPublish() {
    window.slirnRevKeyActions = REV_KEY_ACTIONS.slice();
    window.slirnRevKeyMap = revKeysLoad();
  }
  function revKeysLoad() {
    var km = {};
    REV_KEY_ACTIONS.forEach(function(a) { km[a.id] = a.def; });
    try {
      var raw = JSON.parse(localStorage.getItem('slirnRevKeys') || 'null');
      if (raw && typeof raw === 'object') {
        REV_KEY_ACTIONS.forEach(function(a) {
          var v = raw[a.id];
          // 脏数据（非字符串/重复键）→ 该动作回退默认，先到先得
          if (typeof v === 'string' && v) {
            var used = REV_KEY_ACTIONS.some(function(b) { return km[b.id] === v; });
            if (!used) km[a.id] = v;
          }
        });
      }
    } catch (err) {}
    return km;
  }
  function revKeyLabel(k) {
    var map = { ' ': '空格', 'arrowup': '↑', 'arrowdown': '↓', 'arrowleft': '←',
      'arrowright': '→', 'escape': 'Esc', 'enter': 'Enter', 'backspace': '⌫',
      'delete': 'Del', 'home': 'Home', 'end': 'End', 'pageup': 'PgUp', 'pagedown': 'PgDn' };
    var s = map[k] || ((k && k.length === 1) ? k.toUpperCase() : k);
    return String(s).replace(/[&<>"']/g, function(c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  // 提示条跟随当前键位重渲染（详情页/工作台两份一并更新）
  function applyRevKeysState() {
    var km = revKeysLoad();
    document.querySelectorAll('.slirn-rev-kbhint').forEach(function(el) {
      el.innerHTML = '⌨ 快捷键：<kbd>' + revKeyLabel(km.prev) + '</kbd><kbd>'
        + revKeyLabel(km.next) + '</kbd> 上一条 / 下一条 · <kbd>' + revKeyLabel(km.play)
        + '</kbd> 播放 / 暂停 · <kbd>' + revKeyLabel(km.replay) + '</kbd> 重播本行 · <kbd>'
        + revKeyLabel(km.keep) + '</kbd> 保留 · <kbd>' + revKeyLabel(km.del)
        + '</kbd> 删除（标记后自动下一条）· <kbd>' + revKeyLabel(km.split)
        + '</kbd> 切分（展开详情聚焦内容） · <kbd>' + revKeyLabel(km.esc)
        + '</kbd> 退出输入框'
        + ' · <span class="slirn-revkeys-open" data-action="revkeys-open" role="button" tabindex="0">⚙ 自定义</span>';
    });
  }
  // REQ-20260922-NNN：优化字幕提示条（只引入 播放/暂停 + 停止 两键，键位跟随自定义）
  function applyOptKeysState() {
    var km = revKeysLoad();
    document.querySelectorAll('.slirn-opt-kbhint').forEach(function(el) {
      el.innerHTML = '⌨ 快捷键：<kbd>' + revKeyLabel(km.play) + '</kbd> 播放 / 暂停 · <kbd>'
        + revKeyLabel(km.stop) + '</kbd> 停止（暂停并回到本行开头）'
        + ' · <span class="slirn-revkeys-open" data-action="revkeys-open" role="button" tabindex="0">⚙ 自定义</span>';
    });
  }
  var revKeysRec = null;  // 正在录制换绑的动作 id（null = 未在录制）
  function revKeysModalOpen() { return !!document.getElementById('slirn-revkeys-modal'); }
  function revKeysOpenModal() {
    if (revKeysModalOpen()) return;
    var bd = document.createElement('div');
    bd.id = 'slirn-revkeys-modal';
    bd.className = 'slirn-revkeys-backdrop';
    bd.innerHTML = '<div class="slirn-revkeys-modal" role="dialog" aria-label="快捷键自定义">'
      + '<div class="slirn-revkeys-title">⌨ 快捷键自定义'
      + '<span class="slirn-revkeys-sub">点击键帽 → 按新键更换</span></div>'
      + '<div class="slirn-revkeys-rows"></div>'
      // REQ-20261022-146：硬件按键区（内容由 hardware_keys.js 渲染）
      + '<div class="slirn-revkeys-hw"></div>'
      + '<div class="slirn-revkeys-vlist"></div>'
      + '<div class="slirn-revkeys-foot">'
      + '<span class="slirn-revkeys-tip">Esc 取消录制 · 不支持组合键</span>'
      + '<button class="slirn-btn" data-action="revkeys-reset">↩️ 恢复默认</button>'
      + '<button class="slirn-btn slirn-btn-primary" data-action="revkeys-close">✅ 完成</button>'
      + '</div></div>';
    bd.addEventListener('click', function(ev) { if (ev.target === bd) revKeysCloseModal(); });
    document.body.appendChild(bd);
    revKeysRec = null;
    revKeysRenderRows();
    revKeysRenderVideo();
    revKeysPublish();
    // REQ-20261022-146：硬件按键区
    var hwBox = bd.querySelector('.slirn-revkeys-hw');
    if (hwBox && window.slirnHardwareKeys) {
      window.slirnHardwareKeys.renderInto(hwBox);
    }
  }
  function revKeysCloseModal() {
    var m = document.getElementById('slirn-revkeys-modal');
    if (m) m.remove();
    revKeysRec = null;
  }
  function revKeysRenderRows(flashConflict) {
    var box = document.querySelector('#slirn-revkeys-modal .slirn-revkeys-rows');
    if (!box) return;
    var km = revKeysLoad();
    var html = '';
    REV_KEY_ACTIONS.forEach(function(a) {
      var cls = 'slirn-revkeys-key';
      if (revKeysRec === a.id) cls += ' rec';
      if (flashConflict === a.id) cls += ' conflict';
      // REQ-20260918-042：当前键与视频播放键重叠 → 行内 ⚠ 徽章
      var vhit = REV_VIDEO_KEYS[km[a.id]];
      html += '<div class="slirn-revkeys-row"><span class="slirn-revkeys-name">' + a.name
        + (vhit ? '<i class="slirn-revkeys-vwarn">⚠ 视频键 · ' + vhit + '</i>' : '')
        + '</span><span class="' + cls + '" data-revkey="' + a.id + '">'
        + (revKeysRec === a.id ? '按新键…' : revKeyLabel(km[a.id])) + '</span></div>';
    });
    box.innerHTML = html;
  }
  // REQ-20260918-042：视频播放快捷键对照表（每个键什么意思 + 是否已被应用键占用）
  function revKeysRenderVideo(hlKey) {
    var box = document.querySelector('#slirn-revkeys-modal .slirn-revkeys-vlist');
    if (!box) return;
    var km = revKeysLoad();
    var html = '<div class="slirn-revkeys-vtitle">▶ 视频播放快捷键'
      + '<span class="slirn-revkeys-vsub">视频画面获得焦点时生效（Chrome/Edge 原生播放器）</span></div>'
      + '<div class="slirn-revkeys-vgrid">';
    Object.keys(REV_VIDEO_KEYS).forEach(function(k) {
      var usedBy = '';
      REV_KEY_ACTIONS.forEach(function(a) {
        if (!usedBy && km[a.id] === k) usedBy = a.name;
      });
      html += '<span class="slirn-revkeys-vitem' + (hlKey === k ? ' hl' : '')
        + (usedBy ? ' used' : '') + '"><kbd>' + revKeyLabel(k) + '</kbd>'
        + REV_VIDEO_KEYS[k]
        + (usedBy ? '<em>已绑：' + usedBy + '</em>' : '') + '</span>';
    });
    box.innerHTML = html + '</div>'
      + '<div class="slirn-revkeys-vnote">⚠ 把上表中的键绑成应用快捷键时：列表可见则应用动作优先，'
      + '该键在视频聚焦时不再触发原生播放控制 — 绑定会弹提醒，可自行权衡。</div>';
  }
  // 录制监听（capture）：弹窗打开时接管全部按键，列表快捷键让位
  document.addEventListener('keydown', function(e) {
    if (!revKeysModalOpen()) return;
    e.preventDefault();
    e.stopPropagation();
    if (e.ctrlKey || e.metaKey || e.altKey) {
      toast('❌ 不支持组合键（避免与浏览器/系统冲突）', 'error');
      return;
    }
    if (e.key === 'Shift' || e.key === 'Control' || e.key === 'Alt'
      || e.key === 'Meta' || e.key === 'Dead') return;  // 等待实际按键
    if (e.repeat) return;
    if (!revKeysRec) { if (e.key === 'Escape') revKeysCloseModal(); return; }
    // Esc 取消录制；但 esc 动作本身允许绑 Esc（否则永远绑不回去）
    if (e.key === 'Escape' && revKeysRec !== 'esc') {
      revKeysRec = null;
      revKeysRenderRows();
      return;
    }
    var k = (e.key || '').toLowerCase();
    var okChar = (k.length === 1 && /[\x20-\x7e]/.test(k));  // 可打印 ASCII 单键
    if (!okChar && REV_KEY_ALLOWED.indexOf(k) < 0) {
      toast('❌ 该键不可用作快捷键', 'error');
      return;
    }
    var km = revKeysLoad();
    for (var i = 0; i < REV_KEY_ACTIONS.length; i++) {
      var a = REV_KEY_ACTIONS[i];
      if (a.id !== revKeysRec && km[a.id] === k) {
        revKeysRenderRows(a.id);  // 冲突行闪红
        toast('❌ 「' + revKeyLabel(k) + '」已用于「' + a.name + '」，请换一个键', 'error');
        return;
      }
    }
    km[revKeysRec] = k;
    try { localStorage.setItem('slirnRevKeys', JSON.stringify(km)); } catch (err) {}
    revKeysPublish();  // REQ-20261022-146：硬件键跟随新的键位
    var boundName = '';
    REV_KEY_ACTIONS.forEach(function(a) { if (a.id === revKeysRec) boundName = a.name; });
    revKeysRec = null;
    revKeysRenderRows();
    revKeysRenderVideo(k);  // 对照表同步「已绑」标记
    applyRevKeysState();
    applyOptKeysState();  // 优化字幕提示条跟随（REQ-20260922-NNN）
    // REQ-20260918-042：与视频播放键重叠 → 提醒（允许绑定；列表可见时应用键优先）
    if (REV_VIDEO_KEYS[k]) {
      toast('⚠️ 「' + revKeyLabel(k) + '」是视频播放快捷键（' + REV_VIDEO_KEYS[k]
        + '）—「' + boundName + '」绑定后，视频聚焦时此键将优先执行应用动作', 'warning');
    }
  }, true);

  document.addEventListener('keydown', function(e) {
    if (revKeysModalOpen()) return;  // 自定义弹窗打开 → 录制监听器接管
    // 带修饰键的组合留给浏览器/系统
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    var k = (e.key || '').toLowerCase();
    var km = revKeysLoad();
    // 退回键：手动说明输入框 → 退回列表快捷键状态（焦点在输入框内也生效，键可改绑）
    if (k === km.esc) {
      var ae = document.activeElement;
      if (ae && ae.classList && ae.classList.contains('slirn-rev-note-input')) {
        ae.blur();
        e.preventDefault();
      }
      return;
    }
    // 文本输入中不劫持（下拉的方向键保留原生行为）
    var t = e.target;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)) return;
    var rows = revNavRows();  // 快捷键只作用于过滤后可见的行（REQ-20260916-010）
    if (!rows.length) return;  // 修订列表不可见/无行 → 快捷键不生效
    // 反查当前键位对应的动作（按动作表顺序，先定义者优先）
    var act = null;
    for (var i = 0; i < REV_KEY_ACTIONS.length; i++) {
      if (km[REV_KEY_ACTIONS[i].id] === k) { act = REV_KEY_ACTIONS[i].id; break; }
    }
    if (!act) return;
    // 长按只放行导航（快速滚动）；其余动作防连环触发
    if (e.repeat && act !== 'prev' && act !== 'next') return;
    e.preventDefault();
    if (act === 'prev') { revSelectRow(revSelIndex(rows) - 1, true); }
    else if (act === 'next') { revSelectRow(revSelIndex(rows) + 1, true); }
    else if (act === 'play') { revTogglePlay(); }
    else if (act === 'replay') { revReplayRow(); }
    else if (act === 'keep') { revApplyDecision('keep'); }
    else if (act === 'del') { revApplyDecision('delete'); }
    else if (act === 'split') { revApplyDecision('split'); }
  });

  // ===== 切分修剪快捷键（REQ-20260916-011）：与修订区共用键位/自定义，按可见面板分发 =====
  // ↑↓ 选行（跳段起点播放）· 空格 播放/暂停 · R 重播本段（播到段尾自动停）
  // K/D/S 字幕级改判（作用于选中行所属字幕：保留/删除/切分，再按同键取消）
  // 修订列表可见时本 handler 自然让位（cutRows 为空），两区互不抢键。
  document.addEventListener('keydown', function(e) {
    if (revKeysModalOpen()) return;  // 键位自定义弹窗打开 → 录制监听器接管
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    var k = (e.key || '').toLowerCase();
    var km = revKeysLoad();
    // Esc：退出重切编辑区（焦点在输入框内也生效，键可改绑）
    if (k === km.esc) {
      var aeC = document.activeElement;
      if (aeC && aeC.classList && aeC.classList.contains('slirn-cut-resplit-input')) {
        cutCloseResplit();
        e.preventDefault();
      }
      return;
    }
    var t = e.target;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)) return;
    var rows = cutRows();
    if (!rows.length) return;  // 切分面板不可见 → 快捷键不生效
    var act = null;
    for (var i = 0; i < REV_KEY_ACTIONS.length; i++) {
      if (km[REV_KEY_ACTIONS[i].id] === k) { act = REV_KEY_ACTIONS[i].id; break; }
    }
    if (!act || act === 'esc') return;
    if (e.repeat && act !== 'prev' && act !== 'next') return;  // 长按只放行导航
    e.preventDefault();
    if (act === 'prev') { cutSelectRow(cutSelIndex(rows) - 1, true); }
    else if (act === 'next') { cutSelectRow(cutSelIndex(rows) + 1, true); }
    else if (act === 'play') { cutTogglePlay(); }
    else if (act === 'replay') { cutReplayRow(); }
    else if (act === 'keep') { cutApplyDecision('keep'); }
    else if (act === 'del') { cutApplyDecision('delete'); }
    else if (act === 'split') { cutApplyDecision('split'); }
  });

  // ===== 优化字幕快捷键（REQ-20260922-NNN）：只引入 播放/暂停 + 停止 两键 =====
  // 复用修订/切分的自定义键位体系（slirnRevKeys + ⚙ 弹窗），其余动作
  // （↑↓/r/k/d/s）在本面板不劫持。修订/切分面板可见时对应 handler 接管，
  // 本 handler 的 opt-list 可见性检查互不抢键。
  document.addEventListener('keydown', function(e) {
    if (revKeysModalOpen()) return;  // 键位自定义弹窗打开 → 录制监听器接管
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    var t = e.target;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)) return;
    var optList = revVis('slirn-opt-list');
    if (!optList || !optList.offsetParent) return;  // 优化面板不可见 → 不生效
    var k = (e.key || '').toLowerCase();
    var km = revKeysLoad();
    if (!k || (k !== km.play && k !== km.stop)) return;  // 只匹配两键
    e.preventDefault();
    if (e.repeat && k === km.stop) return;  // 长按 stop 无意义
    if (k === km.play) { optTogglePlay(); }
    else { optStopPlay(); }
  });

  // REQ-20260918-055：优化字幕·替换值输入框回车 → 标记完成（采纳 + reviewed）
  // document 级委托 — wb 重渲染后元素替换不影响；严格过滤避免误吞其他 Enter
  document.addEventListener('keydown', function(e) {
    if (revKeysModalOpen()) return;  // 录制弹窗打开时让位
    if (e.ctrlKey || e.metaKey || e.altKey || e.shiftKey) return;
    if (e.key !== 'Enter') return;
    var ae = e.target;
    if (!ae || !ae.classList || !ae.classList.contains('slirn-opt-after')) return;
    optOccEnterConfirm(ae, e);
  });

  // REQ-20260922-NNN：整句替换编辑框 — Enter 应用 / Esc 取消（Shift+Enter 换行）
  // document 级委托，同上严格过滤；textarea 内的键不进面板快捷键（INPUT 让位）。
  document.addEventListener('keydown', function(e) {
    if (e.key !== 'Enter' && e.key !== 'Escape') return;
    var ae = e.target;
    if (!ae || !ae.classList || !ae.classList.contains('slirn-opt-line-input')) return;
    var row = ae.closest('.slirn-opt-row');
    if (!row) return;
    if (e.key === 'Escape') {
      e.preventDefault();
      optLineCancel(row);
      return;
    }
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.shiftKey) return;  // Shift+Enter = 换行（保留默认行为）
    e.preventDefault();
    optLineApply(row);
  });

  function handleResp(resp, refreshCellId) {
    if (!resp) { toast('❌ 无响应', 'error'); return; }
    if (!resp.ok) { toast('❌ ' + (resp.error || '操作失败'), 'error'); return; }
    if (resp.html) refreshCell(refreshCellId, resp.html);
    if (resp.file_info) applyFileInfo(resp.file_info);
    if (resp.cut_done) applyCutDone(resp.cut_done);
    if (resp.toast) toast(resp.toast);
  }

  // ===== 全局事件委托 =====
  document.addEventListener('click', function(e) {
    // 字幕行点击（行自身无 data-action，先于 data-action 委托处理）
    var subRow = e.target.closest('.slirn-sub-row');
    if (subRow) {
      e.preventDefault();
      var tidS = subRow.getAttribute('data-task-id') || '';
      var startMs = parseInt(subRow.getAttribute('data-start-ms'), 10) || 0;
      playSubAt(tidS, startMs);
      return;
    }

    // 修订行点击连续跳播（select/input/button 与详情块上的点击不触发）：
    // 本条播完自动跳下一条起点，段间空白不播（REQ-20260917-028）
    var revRow = e.target.closest('.slirn-rev-row');
    if (revRow && !e.target.closest('select, input, button, a, .slirn-rev-detail')) {
      e.preventDefault();
      revMarkSel(revRow);  // 鼠标与键盘共享「当前行」（REQ-20260916-004）
      revPlayFrom(revRow);
      return;
    }

    // 切分修剪（REQ-20260916-011）：mark 徽章点击=翻转 · 组头试听钮=keep 连续跳播 ·
    // 行点击=段级预播（播到段尾自动停）；旧「点行续播」由预播替代
    var cutMark = e.target.closest('.slirn-cut-mark');
    if (cutMark) {
      e.preventDefault();
      var rowM = cutMark.closest('.slirn-cut-row');
      if (rowM) { cutMarkSel(rowM); cutFlipMark(rowM); }
      return;
    }
    var cutListen = e.target.closest('[data-cut-act="play-keep"]');
    if (cutListen) {
      e.preventDefault();
      cutPlayGroupKeep(cutListen.closest('.slirn-cut-group'));
      return;
    }
    // 单段重新切分（REQ-20260916-011）：入口展开 / 执行 / 取消
    var cutRsg = e.target.closest('[data-cut-act="resplit"]');
    if (cutRsg) {
      e.preventDefault();
      cutOpenResplit(cutRsg.closest('.slirn-cut-group'));
      return;
    }
    var cutRsgGo = e.target.closest('[data-cut-act="resplit-go"]');
    if (cutRsgGo) {
      e.preventDefault();
      cutDoResplit(cutRsgGo.closest('.slirn-cut-group'));
      return;
    }
    var cutRsgNo = e.target.closest('[data-cut-act="resplit-cancel"]');
    if (cutRsgNo) {
      e.preventDefault();
      cutCloseResplit();
      return;
    }
    var cutRow = e.target.closest('.slirn-cut-row');
    if (cutRow && !e.target.closest('button, a, select')) {  // select=决策下拉（REQ-20260917-027），点它只开下拉不预播
      e.preventDefault();
      cutMarkSel(cutRow);  // 鼠标与键盘共享「当前行」
      cutPreview(cutRow);
      return;
    }

    // 快捷键自定义：键帽点击进入录制（REQ-20260916-005；键帽无 data-action，先于其判断）
    var rk = e.target.closest('[data-revkey]');
    if (rk) {
      e.preventDefault();
      revKeysRec = rk.getAttribute('data-revkey');
      revKeysRenderRows();
      return;
    }

    // 状态过滤：维度切换 / 筛选项点选（REQ-20260916-010；chips 无 data-action，先于其判断）
    // 跳转状态（REQ-20260917-025）独立于筛选：这里不再触碰跳转锚点
    var fdim = e.target.closest('[data-rev-filter-dim]');
    if (fdim) {
      e.preventDefault();
      revFilter.dim = fdim.getAttribute('data-rev-filter-dim');
      revFilter.val = 'all';
      revFilterSave();
      revFilterSync();
      return;
    }
    var fval = e.target.closest('[data-rev-filter-val]');
    if (fval) {
      e.preventDefault();
      revFilter.val = fval.getAttribute('data-rev-filter-val');
      revFilterSave();
      revFilterSync();
      return;
    }
    // 状态跳转（REQ-20260917-025）：按「状态下拉」所选状态在完整列表里跳上/下一条（上下文可见）
    var fjmp = e.target.closest('[data-rev-jump]');
    if (fjmp) {
      e.preventDefault();
      revJump(fjmp.getAttribute('data-rev-jump') === 'prev' ? -1 : 1);
      return;
    }

    var target = e.target.closest('[data-action]');
    if (!target) return;
    var action = target.getAttribute('data-action');
    e.preventDefault();

    if (action === 'rigor-prompt-preset') {
      // 自定义严谨性：载入/恢复 高/中/低 任一档底稿（REQ-20260916-009）；
      // 已有编辑内容且不同 → 确认后再覆盖；草稿同步覆盖
      var wrapR = document.getElementById('slirn-rigor-custom');
      var taR = wrapR ? wrapR.querySelector('.slirn-rigor-custom-text') : null;
      if (taR) {
        var keyR = target.getAttribute('data-preset') || 'high';
        var textR = wrapR.getAttribute('data-prompt-' + keyR) || '';
        var nameR = target.textContent.trim();
        if (taR.value && taR.value !== textR &&
            !window.confirm('载入底稿「' + nameR + '」会覆盖当前编辑内容。确定继续？')) return;
        taR.value = textR;
        try { localStorage.setItem('slirnRevCustomPrompt', textR); } catch (err) {}
        taR.focus();
        toast('已载入底稿「' + nameR + '」，可在此基础上修改');
      }
      return;
    }
    if (action === 'revkeys-open') {
      revKeysOpenModal();
      return;
    }
    if (action === 'revkeys-close') {
      revKeysCloseModal();
      return;
    }
    if (action === 'vf-close') {  // 视频浮层收起（REQ-20260918-043）：暂停 + 隐藏，再播放自动弹回
      var vfL = document.getElementById('slirn-video-float');
      if (vfL) {
        vfL.querySelectorAll('video').forEach(function(vv) { try { vv.pause(); } catch (err) {} });
        vfL.hidden = true;
      }
      return;
    }
    if (action === 'col-toggle') {  // 功能区折叠（REQ-20260918-044）：收起只留胶囊头，再点展开
      var colBox = target.closest('.slirn-col');
      if (colBox) {
        var colOff = !colBox.classList.contains('off');
        colBox.classList.toggle('off', colOff);
        colSet(colBox.getAttribute('data-col-key') || '', colOff);
        var chevEl = colBox.querySelector('.slirn-col-chev');
        if (chevEl) chevEl.textContent = colOff ? '▸' : '▾';
      }
      return;
    }
    if (action === 'rev-search-prev') { searchNavTo('slirn-rev-search-q', revNavRows, -1, revMarkSel); return; }
    if (action === 'rev-search-next') { searchNavTo('slirn-rev-search-q', revNavRows, 1, revMarkSel); return; }
    if (action === 'cut-search-prev') { searchNavTo('slirn-cut-search-q', cutRows, -1, cutMarkSel); return; }
    if (action === 'cut-search-next') { searchNavTo('slirn-cut-search-q', cutRows, 1, cutMarkSel); return; }
    if (action === 'revkeys-reset') {
      try { localStorage.removeItem('slirnRevKeys'); } catch (err) {}
      revKeysRec = null;
      revKeysRenderRows();
      revKeysRenderVideo();
      applyRevKeysState();
      applyOptKeysState();  // 优化字幕提示条恢复默认（REQ-20260922-NNN）
      toast('↩️ 快捷键已恢复默认键位');
      return;
    }

    // Tab 切换
    if (TAB_BUTTONS[action]) {
      // REQ-20260926-NNN：离开 wb/create 进非任务页 → 清 hash（防刷新跳回）
      if (TAB_BUTTONS[action] !== 'slirn-tab-workbench') {
        try { _clearTaskPageHash(); } catch (e) {}
      }
      showTab(TAB_BUTTONS[action], {
        forceShortVideoList: action === 'goto-short-video',
      });
      return;
    }

    if (action === 'refresh-tasks') {
      _refreshTasksList();
    }
    else if (action === 'refresh-wb') {
      // REQ-20260918-053 延伸：wb 内「🔄 刷新」按钮 — 不刷整页，仅重渲 wb 面板
      // 用户原话：「一刷新当前页面就回到首页，还得点列表，然后还得重新进来」
      // 配套：URL #wb= 已写入，F5 / Cmd+R 也能留在同一任务页
      var _tidR = target.getAttribute('data-task-id') || '';
      if (_tidR) openWorkbench(_tidR);
    }
    else if (action === 'view-task') {
      var tid = target.getAttribute('data-task-id') || '';
      postJSON(SLIRN_API + '/view_task', {task_id: tid}).then(function(r) {
        if (r && r.html) {
          var d = document.getElementById('slirn-tab-detail');
          if (d) { d.innerHTML = r.html; d.style.display = ''; }
          bindSubPlayer();
          bindRevPlayer();
        }
      });
    }
    else if (action === 'gen-subtitle') {
      var tidG = target.getAttribute('data-task-id') || '';
      // 同 id 复选框详情页/工作台各一份（两 tab 同在 DOM）→ 从点击按钮所在卡片内
      // 取开关，避免 getElementById 命中隐藏副本读到旧状态（REQ-20260917-029）
      var sdCard = target.closest ? target.closest('.slirn-card') : null;
      var sdEl = sdCard ? sdCard.querySelector('#slirn-sd-switch')
                        : document.getElementById('slirn-sd-switch');
      postJSON(SLIRN_API + '/gen_subtitle', {task_id: tidG, sd: sdEl ? sdEl.checked : true}).then(function(r) {
        if (r && r.ok) {
          toast(r.toast || '已开始生成');
          var el = document.getElementById('slirn-asr-status');
          if (el) { el.dataset.state = 'running'; el.style.display = ''; el.innerHTML = '⏳ 已提交…'; }
          startSubPolling(tidG);
        } else if (r && r.error) {
          toast('❌ ' + r.error, 'error');
        }
      });
    }
    else if (action === 'play-segment') {
      var tidP = target.getAttribute('data-task-id') || '';
      playSubAt(tidP, 0);
    }
    else if (action === 'play-rev-video') {
      // 连续跳播从第一条起（REQ-20260917-028）；无行兜底普通播放
      if (!revPlayFrom(revRows()[0] || null)) playRevAt(target.getAttribute('data-task-id') || '', 0);
    }
    else if (action === 'play-cut-video') {
      playCutAt(target.getAttribute('data-task-id') || '', 0);
    }
    else if (action === 'build-cutlist') {
      // 生成切分清单并完成本阶段（REQ-20260916-008）：落盘 + 推进 ROUGH_CUT_DONE，
      // openWorkbench 全刷新（阶段条 rough_cut → done、精剪字幕 → current）
      var tidC = target.getAttribute('data-task-id') || '';
      postJSON(SLIRN_API + '/build_cutlist', {task_id: tidC})
        .then(function(r) {
          if (r && r.ok) {
            toast(r.toast || '切分清单已生成');
            openWorkbench(tidC);
          } else if (r && r.error) {
            toast('❌ ' + r.error, 'error');
          }
        });
    }
    else if (action === 'save-cut-decisions') {
      // 保存切分决策（REQ-20260916-011）：翻转 manual_marks + 字幕级改判 actions
      cutSave(target);
    }
    else if (action === 'cut-spk-link') {
      // 关联人员ID（REQ-20260917-031）：时间段重叠对齐 spk → 徽章 + 统计条
      cutSpkLink(target);
    }
    else if (action === 'cut-spk-prev') {
      cutSpkNav(-1);
    }
    else if (action === 'cut-spk-next') {
      cutSpkNav(1);
    }
    else if (action === 'cut-spk-delete') {
      cutSpkDelete();
    }
    else if (action === 'cut-spk-chip') {
      // 统计条 chip 点击 → 填入查找框（服务端渲染 / JS 重渲染的 chips 都走此委托）
      var qEl = document.getElementById('slirn-cut-spk-q');
      if (qEl) { qEl.value = target.getAttribute('data-spk') || ''; qEl.focus(); }
    }
    else if (action === 'cut-spk-recount') {
      // 重新统计：按当前 DOM 去留现算（未保存改判也计入）；未关联过 → 引导
      var spkBarEl = document.getElementById('slirn-cut-spk-bar');
      if (!spkBarEl || spkBarEl.dataset.linked !== '1') {
        toast('先点「👤 关联人员ID」建立人员关联', 'error');
      } else {
        cutSpkBarRender();
        toast('🧮 已按当前去留状态重新统计');
      }
    }
    else if (action === 'rev-spk-link') {
      // 字幕修订阶段关联人员ID（REQ-20260919-068）：时间重叠 → 行徽章 + 统计条
      revSpkLink(target);
    }
    else if (action === 'rev-spk-prev') {
      revSpkNav(-1);
    }
    else if (action === 'rev-spk-next') {
      revSpkNav(1);
    }
    else if (action === 'rev-spk-delete') {
      revSpkDelete();
    }
    else if (action === 'rev-spk-chip') {
      // 统计条 chip 点击 → 填入查找框
      var revQEl = document.getElementById('slirn-rev-spk-q');
      if (revQEl) { revQEl.value = target.getAttribute('data-spk') || ''; revQEl.focus(); }
    }
    else if (action === 'rev-spk-recount') {
      // 重新统计：按当前 DOM 决策现算（未保存改判也计入）；未关联过 → 引导
      var revSpkBarEl = document.getElementById('slirn-rev-spk-bar');
      if (!revSpkBarEl || revSpkBarEl.dataset.linked !== '1') {
        toast('先点「👤 关联人员ID」建立人员关联', 'error');
      } else {
        revSpkBarRender();
        toast('🧮 已按当前决策状态重新统计');
      }
    }
    else if (action === 'rev-batch-apply') {
      revBatchApply();  // REQ-20260918-039：字幕修改批量改判
    }
    else if (action === 'cut-batch-apply') {
      cutBatchApply();  // REQ-20260918-039：切分修剪批量改判
    }
    else if (action === 'compose-rough') {
      // 粗剪合成（REQ-20260916-016，可选）：后台拼接保留区间成片
      rcCompose(target);
    }
    else if (action === 'compose-rough-delete') {
      // 删除粗剪成片（REQ-20260916-019）：confirm → POST → 刷新工作台
      rcDelete(target);
    }
    else if (action === 'compose-rough-subs-copy') {
      // 复制有效字幕 SRT（REQ-20260916-019）：从 <pre> 读 textContent
      rcSubsCopy(target);
    }
    else if (action === 'compose-rough-subs-download') {
      // 下载有效字幕 SRT（REQ-20260916-019）：拉端点拿 SRT → Blob 下载
      rcSubsDownload(target);
    }
    else if (action === 'optimize-start') {
      // 优化字幕（REQ-20260917-030）：成片重识别 + 大模型分析 → 轮询 → 刷新
      optStart(target);
    }
    else if (action === 'opt-occ-toggle') {
      optOccToggle(target);
    }
    else if (action === 'opt-line-edit') {
      // REQ-20260922-NNN 整句替换：✏️ 进入编辑态；↩️（已有整句）= 取消整句
      optLineEdit(target.closest('.slirn-opt-row'));
    }
    else if (action === 'opt-line-delete') {
      // REQ-20260922-NNN 标记删除行：🗑️ 标记 / ✚ 取消（保存后剪除出优化成片）
      optLineDelete(target.closest('.slirn-opt-row'));
    }
    else if (action === 'opt-line-resplit') {
      // REQ-20260923-NNN 行内切分：✂️ 展开切分编辑区（未切分行 = 首切，已切分行 = 重切）
      optOpenResplit(target.closest('.slirn-opt-row[data-id]'));
    }
    else if (action === 'opt-resplit-go') {
      optDoResplit();
    }
    else if (action === 'opt-resplit-cancel') {
      optCloseResplit();
    }
    else if (action === 'opt-line-unsplit') {
      // REQ-20260923-NNN 取消切分：恢复整行
      optUnsplit(target.closest('.slirn-opt-row[data-id]'));
    }
    else if (action === 'opt-sub-toggle') {
      // REQ-20260923-NNN 子段 🗑️/✚ 翻转（split_marks 全量快照自动保存）
      optSubToggle(target);
    }
    else if (action === 'opt-final-video') {
      optFinalVideoView(target);
    }
    else if (action === 'opt-resplice') {
      // REQ-20260923-NNN 🔄 重新拼接字幕：即时重算 optimize_compose.srt（不编码视频）
      optResplice(target);
    }
    else if (action === 'opt-cut-rekick') {
      // REQ-20260922-NNN 🎬 重新拼接视频：按当前剪辑计划手动触发优化成片剪辑
      optCutRekick(target);
    }
    else if (action === 'opt-line-apply') {
      optLineApply(target.closest('.slirn-opt-row'));
    }
    else if (action === 'opt-line-cancel') {
      optLineCancel(target.closest('.slirn-opt-row'));
    }
    else if (action === 'opt-word') {
      optWordFilter(target);
    }
    else if (action === 'opt-word-filter') {
      optWordFilterBtn(target);  // REQ-038：词列表按 处理完成 状态过滤
    }
    else if (action === 'opt-word-text-clear') {
      // REQ-20260918-057B：清空文字过滤
      var _inp = document.getElementById('slirn-opt-word-text');
      if (_inp) {
        _inp.value = '';
        optWordTextFilter();
        _inp.focus();
      }
    }
    else if (action === 'fine-upload') {
      // REQ-20260919-061：上传精剪视频素材
      fineUpload(target, target.getAttribute('data-kind') || '');
    }
    else if (action === 'fine-source-auto') {
      // REQ-20260919-061：从上游自动获取（与手动上传按钮并行可用）
      fineSourceAuto(target, target.getAttribute('data-kind') || '');
    }
    else if (action === 'fine-mat-preview') {
      // REQ-20260919-063：每个素材的预览按钮（图片/视频/音频/SRT）
      var _kind = target.getAttribute('data-kind') || '';
      var _tid = target.getAttribute('data-task-id') || _fineTid();
      if (typeof fineMaterialPreview === 'function') fineMaterialPreview(_tid, _kind);
    }
    else if (action === 'fine-mat-detail') {
      // REQ-20260920-088：素材路径详情按钮（弹窗显示完整路径 + 来源色块 + 文件元数据）
      var _mdKind = target.getAttribute('data-kind') || '';
      var _mdTid = target.getAttribute('data-task-id') || _fineTid();
      if (_mdKind && _mdTid && typeof fineMatDetail === 'function') {
        fineMatDetail(_mdTid, _mdKind);
      }
    }
    else if (action === 'mat-detail-close') {
      // REQ-20260920-088：关闭素材路径详情模态框
      var _mdModal = document.getElementById('slirn-mat-detail-modal');
      if (_mdModal) _mdModal.hidden = true;
    }
    else if (action === 'fine-crop-preset') {
      // REQ-20260919-061：视频源裁剪预设（全幅/16:9/1:1）
      fineCropPreset(target, target.getAttribute('data-preset') || '');
    }
    else if (action === 'fine-scale-auto') {
      // REQ-20260919-062 v14：缩放 = crop_w / 1920（视频原裁剪宽度 / 背景图片宽度）
      fineScaleAutoFromCrop();
    }
    else if (action === 'fine-save-all') {
      // REQ-20260919-061 用户反馈：手动保存参数 + 模板（顶部操作栏）
      // showToast=true（用户主动点） + asTemplate=true（若没填名会弹窗要求填）
      if (typeof fineSaveAll === 'function') fineSaveAll(true, true);
    }
    else if (action === 'fine-import-show') {
      // REQ-20260919-061 用户反馈：弹出引用参数 modal（从全局模板列表里选）
      if (typeof fineImportShow === 'function') fineImportShow();
    }
    else if (action === 'fine-import-close') {
      if (typeof fineImportClose === 'function') fineImportClose();
    }
    else if (action === 'fine-import-apply') {
      var _impPid = target.getAttribute('data-profile-id') || '';
      var _impRow = target.closest('.slirn-fine-import-row');
      var _impName = _impRow ? (_impRow.querySelector('.slirn-fine-import-name') || {}).textContent || '' : '';
      if (typeof fineProfileApply === 'function') fineProfileApply(_impPid, _impName);
    }
    else if (action === 'fine-import-delete') {
      var _impDelPid = target.getAttribute('data-profile-id') || '';
      if (typeof fineProfileDelete === 'function') fineProfileDelete(_impDelPid);
      // 删除后刷新 modal 列表
      if (typeof fineImportShow === 'function') fineImportShow();
    }
    else if (action === 'fine-import-rename') {
      var _impRenPid = target.getAttribute('data-profile-id') || '';
      var _impRenRow = target.closest('.slirn-fine-import-row');
      var _impRenName = _impRenRow ? (_impRenRow.querySelector('.slirn-fine-import-name') || {}).textContent || '' : '';
      if (typeof fineProfileRename === 'function') fineProfileRename(_impRenPid, _impRenName);
      if (typeof fineImportShow === 'function') fineImportShow();
    }
    else if (action === 'fine-import-export') {
      // REQ-20260919-070：导出单个全局模板参数为 JSON 文件。
      // 与「📤 导出参数」（任务级）同口径：后端返 {filename, content, mime} →
      // 前端 Blob + <a download> 触发下载。
      var _expBtn = target;
      var _expPid = _expBtn.getAttribute('data-profile-id') || '';
      var _expOldText = _expBtn.textContent;
      _expBtn.disabled = true;
      _expBtn.textContent = '⏳ 导出中...';
      fetch('/slirn/api/export_fine_global_profile', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({profile_id: _expPid})
      })
        .then(function(r) { return r.json(); })
        .then(function(j) {
          _expBtn.disabled = false;
          _expBtn.textContent = _expOldText;
          if (!j.ok) { toast('❌ ' + (j.error || '导出失败')); return; }
          try {
            var blob = new Blob([j.content], {type: j.mime || 'application/json'});
            var url = URL.createObjectURL(blob);
            var a = document.createElement('a');
            a.href = url;
            a.download = j.filename || ('fine_params_profile_' + _expPid + '.json');
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);
            toast('✅ 已导出模板参数 → ' + a.download);
          } catch (e) {
            toast('❌ 触发下载失败: ' + e.message);
          }
        })
        .catch(function(e) {
          _expBtn.disabled = false;
          _expBtn.textContent = _expOldText;
          toast('❌ 网络错误: ' + e.message);
        });
    }
    else if (action === 'fine-export-params') {
      // REQ-20260919-065：导出当前任务的精剪参数为 JSON 文件（不含素材二进制）
      var _b = target;
      var _tid = _b.getAttribute('data-task-id') || '';
      var _oldText = _b.textContent;
      _b.disabled = true;
      _b.textContent = '⏳ 导出中...';
      fetch('/slirn/api/export_fine_params', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({task_id: _tid})
      })
        .then(function(r) { return r.json(); })
        .then(function(j) {
          _b.disabled = false;
          _b.textContent = _oldText;
          if (!j.ok) {
            toast('❌ ' + (j.error || '导出失败'));
            return;
          }
          // 触发浏览器下载（Blob + <a download>）
          try {
            var blob = new Blob([j.content], {type: j.mime || 'application/json'});
            var url = URL.createObjectURL(blob);
            var a = document.createElement('a');
            a.href = url;
            a.download = j.filename || ('fine_params_' + _tid + '.json');
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);
            toast('✅ 参数已导出到 ' + a.download);
          } catch (e) {
            toast('❌ 触发下载失败: ' + e.message);
          }
        })
        .catch(function(e) {
          _b.disabled = false;
          _b.textContent = _oldText;
          toast('❌ 网络错误: ' + e.message);
        });
    }
    else if (action === 'fine-import-params') {
      // REQ-20260919-065：从 JSON 文件导入参数（覆盖当前参数；不动素材）
      var _b = target;
      var _tid = _b.getAttribute('data-task-id') || '';
      var _oldText = _b.textContent;
      _b.disabled = true;
      _b.textContent = '⏳ 选择文件中...';
      // 动态创建 input[type=file] 触发选择器
      var input = document.createElement('input');
      input.type = 'file';
      input.accept = '.json,application/json';
      input.style.display = 'none';
      input.addEventListener('change', function(e) {
        var file = e.target.files && e.target.files[0];
        if (!file) {
          _b.disabled = false;
          _b.textContent = _oldText;
          return;
        }
        // 64KB 上限（实际参数远低于此）
        if (file.size > 64 * 1024) {
          toast('❌ 文件过大（>' + (file.size / 1024).toFixed(1) + 'KB），拒绝导入');
          _b.disabled = false;
          _b.textContent = _oldText;
          return;
        }
        var reader = new FileReader();
        reader.onload = function(ev) {
          _b.textContent = '⏳ 导入中...';
          fetch('/slirn/api/import_fine_params', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({task_id: _tid, content: ev.target.result})
          })
            .then(function(r) { return r.json(); })
            .then(function(j) {
              _b.disabled = false;
              _b.textContent = _oldText;
              if (!j.ok) {
                toast('❌ ' + (j.error || '导入失败'));
                return;
              }
              var cnt = (j.applied_fields || []).length;
              toast('✅ 已从导入文件应用参数（' + cnt + ' 个字段）');
              // 刷新整个精剪面板，让滑块/输入框反映新参数
              if (typeof renderWorkbench === 'function') {
                renderWorkbench();
              } else if (typeof wbRefresh === 'function') {
                wbRefresh();
              }
            })
            .catch(function(err) {
              _b.disabled = false;
              _b.textContent = _oldText;
              toast('❌ 网络错误: ' + err.message);
            });
        };
        reader.onerror = function() {
          _b.disabled = false;
          _b.textContent = _oldText;
          toast('❌ 读取文件失败');
        };
        reader.readAsText(file, 'utf-8');
      });
      document.body.appendChild(input);
      input.click();
      document.body.removeChild(input);
    }
    else if (action === 'fine-ai-parse') {
      // REQ-20260919-061：调多模态模型解析参考位置关系图（Phase C）
      var _ai = target;
      _ai.disabled = true;
      var _aiText = _ai.textContent;
      _ai.textContent = '🤖 解析中...';
      var _tid = _ai.getAttribute('data-task-id') || '';
      fetch('/slirn/api/parse_reference_layout', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({task_id: _tid})
      })
        .then(function(r) { return r.json(); })
        .then(function(j) {
          _ai.disabled = false;
          _ai.textContent = _aiText;
          if (j.ok && j.layout) {
            // 同步填到滑块 + 复选框（x/y 是整数像素 → 不带小数；scale 浮点 → 2 位小数）
            ['video', 'subtitle', 'cover', 'bg'].forEach(function(k) {
              var v = j.layout[k];
              if (!v) return;
              ['x', 'y', 'scale'].forEach(function(axis) {
                var slider = document.getElementById('slirn-fine-' + k + '-' + axis);
                if (slider) {
                  slider.value = String(v[axis]);
                }
              });
              var ena = document.querySelector('.slirn-fine-enabled[data-key="' + k + '"]');
              if (ena) ena.checked = !!v.enabled;
            });
            toast('✅ AI 已生成布局');
          } else {
            toast('❌ ' + (j.error || '解析失败'));
          }
        }).catch(function(e) {
          _ai.disabled = false; _ai.textContent = _aiText;
          toast('❌ 网络错误: ' + e.message);
        });
    }
    else if (action === 'fine-preview' || action === 'fine-export') {
      // REQ-20260919-061 Phase B：调 ffmpeg 渲染
      // REQ-20260920-098：直接读参数卡 .slirn-fine-enabled checkbox 状态（覆盖 fc.json）
      function _readPerElementCombo() {
        var combo = {};
        document.querySelectorAll('.slirn-fine-enabled[data-key]').forEach(function(cb) {
          combo[cb.getAttribute('data-key')] = !!cb.checked;
        });
        return combo;
      }
      var _b = target;
      if (_b.disabled) return;
      var _isPreview = (action === 'fine-preview');
      var _tid = _b.getAttribute('data-task-id') || '';

      // REQ-20260919-074：导出最终视频走异步后台任务（1-3 小时不再超时）
      if (!_isPreview) {
        var _oldText2 = _b.textContent;
        _b.disabled = true;
        _b.textContent = '💾 启动导出...';
        fetch('/slirn/api/export_fine_video', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ task_id: _tid, combo: _readPerElementCombo() })
        })
          .then(function(r) { return r.json(); })
          .then(function(j) {
            _b.disabled = false; _b.textContent = _oldText2;
            if (!j.ok) { toast('❌ ' + (j.error || '启动失败')); return; }
            // REQ-20260920-077：改用 inline 状态元素（按钮旁），不弹模态框
            if (j.job_id && typeof startFineExportInline === 'function') {
              startFineExportInline(_tid, j.job_id, _b);
            } else {
              toast('✅ ' + (j.toast || '已启动'));
            }
          })
          .catch(function(e) {
            _b.disabled = false; _b.textContent = _oldText2;
            toast('❌ 网络错误: ' + e.message);
          });
        return;
      }

      // 预览：同步路径（≤30 秒，原逻辑不变）
      var _api = '/slirn/api/render_fine_preview';
      var _oldText = _b.textContent;
      _b.disabled = true;
      var _payload = { task_id: _tid };
      var _durEl = document.getElementById('slirn-fine-preview-duration');
      var _dur = parseFloat(_durEl && _durEl.value);
      if (!isNaN(_dur)) _payload.duration = _dur;
      // REQ-20260919-066：预览开始时间改为 时:分:秒 三段输入（默认 00:00:00）。
      var _hEl = document.getElementById('slirn-fine-preview-start-h');
      var _mEl = document.getElementById('slirn-fine-preview-start-m');
      var _sEl = document.getElementById('slirn-fine-preview-start-s');
      var _h = parseInt(_hEl && _hEl.value, 10);
      var _m = parseInt(_mEl && _mEl.value, 10);
      var _s = parseInt(_sEl && _sEl.value, 10);
      if (isNaN(_h) || _h < 0) _h = 0;
      if (isNaN(_m) || _m < 0) _m = 0;
      if (isNaN(_s) || _s < 0) _s = 0;
      if (_m > 59) _m = 59;
      if (_s > 59) _s = 59;
      var _totalSec = _h * 3600 + _m * 60 + _s;
      _payload.preview_start = _totalSec;
      var _timeStr = (_h < 10 ? '0' + _h : _h) + ':' +
                     (_m < 10 ? '0' + _m : _m) + ':' +
                     (_s < 10 ? '0' + _s : _s);
      var _durStr = isNaN(_dur) ? '10' : _dur;
      _b.textContent = '🎬 渲染中（' + _timeStr + ' 起 ' + _durStr + ' 秒）...';
      // REQ-20260920-098：把参数卡当前 5 个 checkbox 状态也带过去
      _payload.combo = _readPerElementCombo();
      fetch(_api, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(_payload)
      })
        .then(function(r) { return r.json(); })
        .then(function(j) {
          _b.disabled = false; _b.textContent = _oldText;
          if (j.ok) {
            if (j.url && typeof openFinePreviewFloat === 'function') {
              openFinePreviewFloat(j.url, '🎬 合成预览（前 ' + (j.preview_dur || '?') + ' 秒）');
            }
            var _empty = document.getElementById('slirn-fine-preview-empty');
            if (_empty) _empty.style.display = 'none';
            toast('✅ ' + (j.toast || '完成'));
          } else {
            toast('❌ ' + (j.error || '渲染失败'));
          }
        }).catch(function(e) {
          _b.disabled = false; _b.textContent = _oldText;
          toast('❌ 网络错误: ' + e.message);
        });
    }
    else if (action === 'fine-export-cancel') {
      // REQ-20260920-089：独立可见的取消按钮（不依赖 status 元素 click）
      if (!confirm('确认取消当前渲染？已生成的片段会被丢弃。')) return;
      var _ceBtn = document.getElementById('slirn-fine-export-cancel-btn');
      if (_ceBtn) { _ceBtn.disabled = true; _ceBtn.setAttribute('data-state', 'cancelling'); }
      // 从 status 元素读 job_id（兜底：也可能从取消按钮的 data-job-id 读）
      var _statusEl = document.getElementById('slirn-fine-export-status');
      var _jobId = (_ceBtn && _ceBtn.getAttribute('data-job-id')) ||
                   (_statusEl && _statusEl.getAttribute('data-job-id')) || '';
      if (!_jobId) {
        toast('❌ 找不到当前 job_id（可能已完成或已取消）');
        if (_ceBtn) { _ceBtn.disabled = false; _ceBtn.setAttribute('data-state', 'idle'); }
        return;
      }
      fetch('/slirn/api/cancel_render', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ job_id: _jobId })
      })
        .then(function(r) { return r.json(); })
        .then(function(j) {
          if (!j.ok) {
            toast('❌ ' + (j.error || '取消失败'));
            if (_ceBtn) { _ceBtn.disabled = false; _ceBtn.setAttribute('data-state', 'running'); }
          } else {
            toast('⏹ 已发送取消信号');
          }
        })
        .catch(function(e) {
          toast('❌ 网络错误: ' + e.message);
          if (_ceBtn) { _ceBtn.disabled = false; _ceBtn.setAttribute('data-state', 'running'); }
        });
    }
    else if (action === 'opt-page-prev' || action === 'opt-page-next') {
      // REQ-20260918-050：词频列表翻页
      if (action === 'opt-page-prev' && optWordsCurrentPage > 1) optWordsCurrentPage--;
      else if (action === 'opt-page-next') {
        var _box = document.getElementById('slirn-opt-words');
        if (_box) {
          // 计数不能用 hidden class（见 paginateOptWords 注释）— 仅算 filter 后的
          var _vis = Array.prototype.slice.call(_box.querySelectorAll('.slirn-opt-word'))
            .filter(function(el){ return el.style.display !== 'none'; });
          var _pages = Math.max(1, Math.ceil(_vis.length / OPT_WORDS_PAGE_SIZE));
          if (optWordsCurrentPage < _pages) optWordsCurrentPage++;
        }
      }
      paginateOptWords();
    }
    else if (action === 'opt-filter') {
      optFilterBtn(target);
    } else if (action === 'opt-filter-deleted') {
      optFilterDeletedBtn(target);
    } else if (action === 'opt-search-clear') {  // REQ-20260923-NNN 行搜索清空
      var _si = document.getElementById('slirn-opt-search');
      if (_si) { _si.value = ''; optRowSearch(); _si.focus(); }
    } else if (action === 'opt-batch-open') {  // REQ-20260929-NNN 批量替换面板
      optBatchOpen(target);
    } else if (action === 'opt-batch-close') {
      optBatchClose();
    } else if (action === 'opt-batch-search') {
      optBatchSearch(target);
    } else if (action === 'opt-batch-replace') {
      optBatchReplace(target);
    } else if (action === 'opt-final-view') {
      optFinalView(target);
    }
    else if (action === 'save-optimize') {
      optSave(target);
    }
    else if (action === 'rebuild-cutlist') {
      // 重新执行切分修剪（REQ-20260916-011）：按最新修订决策整单重算落盘，
      // 清除全部手工决策（与「重新分析=重置」同一哲学）；有手工修改先 confirm
      var tidRb = target.getAttribute('data-task-id') || '';
      if (cutHasManual() &&
          !window.confirm('重新执行将按最新修订决策整单重算，并清除全部手工决策（子段翻转与字幕级改判）。确定继续？')) return;
      postJSON(SLIRN_API + '/build_cutlist', {task_id: tidRb})
        .then(function(r) {
          if (r && r.ok) {
            toast(r.toast || '已按最新决策重新执行切分修剪');
            openWorkbench(tidRb);
          } else if (r && r.error) {
            toast('❌ ' + r.error, 'error');
          }
        });
    }
    else if (action === 'revise-subtitle') {
      var tidV = target.getAttribute('data-task-id') || '';
      // 严谨性级别必选（REQ-20260916-003）：先于覆盖确认 — 没选等级就不发请求
      var rigorEl = document.querySelector('input[name="slirn-rev-rigor"]:checked');
      if (!rigorEl) {
        toast('❌ 请先选择分析严谨性级别（高 / 中 / 低）', 'error');
        var det = document.getElementById('slirn-rev-rigor-box');
        if (det) det.open = true;
        var cards = document.querySelector('.slirn-rigor-cards');
        if (cards) cards.scrollIntoView({behavior: 'smooth', block: 'center'});
        return;
      }
      var payloadV = {task_id: tidV, rigor: rigorEl.value};
      if (rigorEl.value === 'custom') {  // 自定义档带上用户提示词（空白 → 服务端回退底稿）
        var wrapC = document.getElementById('slirn-rigor-custom');
        var taC = wrapC ? wrapC.querySelector('.slirn-rigor-custom-text') : null;
        payloadV.custom_prompt = taC ? taC.value : '';
      }
      try { localStorage.setItem('slirnRevRigor', rigorEl.value); } catch (err) {}
      if (target.getAttribute('data-has-revision') === '1') {
        if (!window.confirm('重新分析将覆盖现有建议，并重置全部手动决策。确定继续？')) return;
        payloadV.force = true;
      }
      postJSON(SLIRN_API + '/revise_subtitle', payloadV).then(function(r) {
        if (r && r.ok) {
          toast(r.toast || '已开始分析');
          var el = document.getElementById('slirn-rev-status');
          if (el) { el.dataset.state = 'running'; el.style.display = ''; el.innerHTML = '⏳ 已提交…'; }
          startRevPolling(tidV);
        } else if (r && r.error) {
          toast('❌ ' + r.error, 'error');
        }
      });
    }
    else if (action === 'save-revision') {
      var tidW = target.getAttribute('data-task-id') || '';
      var decisions = [];
      revRows().forEach(function(row) {  // 只收集可见列表（详情页+工作台同屏时防串数据）
        var sel = row.querySelector('.slirn-rev-select');
        var note = row.querySelector('.slirn-rev-note-input');
        if (sel) {
          decisions.push({
            i: parseInt(sel.getAttribute('data-i'), 10),
            decision: sel.value,
            user_note: note ? note.value : '',
          });
        }
      });
      if (!decisions.length) { toast('❌ 无可保存的决策行', 'error'); return; }
      postJSON(SLIRN_API + '/save_revision', {task_id: tidW, decisions: decisions})
        .then(function(r) {
          if (r && r.ok) {
            toast(r.toast || '已保存');
            openWorkbench(tidW);  // 刷新统计/阶段态
          } else if (r && r.error) {
            toast('❌ ' + r.error, 'error');
          }
        });
    }
    else if (action === 'wb-toggle-stages') {
      var hostW = document.getElementById('slirn-tab-workbench-inner');
      if (hostW) {
        var nowCollapsed = !hostW.classList.contains('wb-stages-collapsed');
        hostW.classList.toggle('wb-stages-collapsed', nowCollapsed);
        try { localStorage.setItem('slirnWbStagesCollapsed', nowCollapsed ? '1' : '0'); } catch (err) {}
      }
    }
    else if (action === 'show-login') {
      if (window.slirnShowLogin) window.slirnShowLogin();
      var u = document.getElementById('slirn-login-username');
      if (u) setTimeout(function() { u.focus(); }, 50);
    }
    else if (action === 'hide-login') {
      if (window.slirnHideLogin) window.slirnHideLogin();
    }
    else if (action === 'do-login') {
      var usr = getInput('slirn-login-username');
      var pw = getInput('slirn-login-password');
      if (!usr || !pw) {
        var hint = document.getElementById('slirn-login-hint');
        if (hint) {
          hint.textContent = '用户名和密码必填';
          hint.classList.remove('is-ok');
          hint.style.display = '';
        }
        return;
      }
      postJSON(SLIRN_API + '/auth/login', {username: usr, password: pw})
        .then(function(r) {
          if (r && r.ok) {
            if (window.slirnHideLogin) window.slirnHideLogin();
            _setCurrentUserUI(r.user);
            toast('✅ 已登录：' + r.user.username);
            // 登录后重拉任务列表 + 仪表盘（admin → 全部，普通用户 → 我的）
            try {
              _refreshTasksList(r.user.is_admin ? 'all' : 'mine');
              _refreshDashboard();
            } catch (e) {}
          } else {
            var hint = document.getElementById('slirn-login-hint');
            if (hint) {
              // 把真实状态码附在错误后，方便排查「401 vs 404 vs 5xx」
              var tag = r && r._status ? ' [HTTP ' + r._status + ']' : '';
              hint.textContent = '登录失败：' + (r.error || '未知错误') + tag;
              hint.classList.remove('is-ok');
              hint.style.display = '';
            }
          }
        });
    }
    else if (action === 'logout') {
      postJSON(SLIRN_API + '/auth/logout', {})
        .then(function() {
          toast('已登出');
          // REQ-20260926-NNN 修复：登出后整页刷新 — 把按用户过滤的任务/仪表盘
          // 内容从 DOM 彻底清掉（回到无数据骨架），比只重拉列表更干净；
          // 加载流程发现未登录会自动弹登录框
          try { _clearTaskPageHash(); } catch (e) {}
          setTimeout(function() { location.reload(); }, 400);
        });
    }
    // ===== REQ-20260926-NNN：用户管理 modal（admin） =====
    else if (action === 'users-open') {
      _showUsersModal();
    }
    else if (action === 'help-menu-toggle') {
      _toggleHelpMenu();
    }
    else if (action === 'settings-menu-toggle') {
      _toggleSettingsMenu();
    }
    else if (action === 'help-long') {
      _closeHelpMenu();
      _showHelpDoc('long_video', '📖 长视频使用说明');
    }
    else if (action === 'help-short') {
      _closeHelpMenu();
      _showHelpDoc('short_video', '📖 短视频使用说明');
    }
    else if (action === 'help-close') {
      var hm = document.getElementById('slirn-help-modal');
      if (hm) hm.style.display = 'none';
    }
    else if (action === 'import-long-params') {
      _closeHelpMenu();
      _importDefaultParams('long', '长视频');
    }
    else if (action === 'import-short-params') {
      _closeHelpMenu();
      _importDefaultParams('short', '短视频');
    }
    else if (action === 'loginlog-open') {
      _showLoginLogModal();
    }
    else if (action === 'loginlog-close') {
      var lm = document.getElementById('slirn-loginlog-modal');
      if (lm) lm.style.display = 'none';
    }
    else if (action === 'users-close') {
      _hideUsersModal();
    }
    else if (action === 'users-create') {
      var newName = getInput('slirn-users-new-name');
      var newPw = getInput('slirn-users-new-pw');
      var newAdmin = !!(document.getElementById('slirn-users-new-admin') || {}).checked;
      if (!newName || !newPw) { _usersModalMsg('用户名和密码必填', true); return; }
      postJSON(SLIRN_API + '/auth/users', {username: newName, password: newPw, is_admin: newAdmin})
        .then(function(r) {
          if (r && r.ok) {
            _usersModalMsg('✅ 已创建用户：' + r.user.username);
            var nEl = document.getElementById('slirn-users-new-name');
            var pEl = document.getElementById('slirn-users-new-pw');
            if (nEl) nEl.value = '';
            if (pEl) pEl.value = '';
            _loadUsers();
          } else {
            _usersModalMsg((r && r.error) || '创建失败', true);
          }
        });
    }
    else if (action === 'users-delete') {
      var delName = target.getAttribute('data-username') || '';
      if (!delName) return;
      if (!window.confirm('确定删除用户「' + delName + '」？其会话与任务成员关系将一并清除。')) return;
      fetch(SLIRN_API + '/auth/users/' + encodeURIComponent(delName), {
        method: 'DELETE', credentials: 'same-origin'
      }).then(function(r) { return r.json(); }).then(function(r) {
        if (r && r.ok) { _usersModalMsg('✅ 已删除：' + delName); _loadUsers(); }
        else _usersModalMsg((r && r.error) || '删除失败', true);
      }).catch(function(e) { _usersModalMsg(String(e), true); });
    }
    else if (action === 'users-reset-pw') {
      var pwName = target.getAttribute('data-username') || '';
      if (!pwName) return;
      var npw = window.prompt('给用户「' + pwName + '」设置新密码（≥4 位）：', '');
      if (!npw) return;
      postJSON(SLIRN_API + '/auth/users/change_password', {username: pwName, new_password: npw})
        .then(function(r) {
          if (r && r.ok) _usersModalMsg('✅ 已更新 ' + pwName + ' 的密码');
          else _usersModalMsg((r && r.error) || '更新失败', true);
        });
    }
    // ===== REQ-20260926-NNN：任务成员管理（编辑页） =====
    else if (action === 'member-add') {
      var secA = document.getElementById('slirn-members-section');
      var tidA = (target.getAttribute('data-task-id') ||
                  (secA ? secA.getAttribute('data-task-id') : '') || '');
      var addName = getInput('slirn-member-add-name');
      if (!tidA || !addName) { _memberMsg('用户名必填', true); return; }
      postJSON(SLIRN_API + '/auth/tasks/' + encodeURIComponent(tidA) + '/members', {username: addName})
        .then(function(r) {
          if (r && r.ok) {
            _memberMsg('✅ 已添加成员：' + addName);
            var inEl = document.getElementById('slirn-member-add-name');
            if (inEl) inEl.value = '';
            _loadTaskMembers();
          } else {
            _memberMsg((r && r.error) || '添加失败', true);
          }
        });
    }
    else if (action === 'member-remove') {
      var tidR = target.getAttribute('data-task-id') || '';
      var rmName = target.getAttribute('data-username') || '';
      if (!tidR || !rmName) return;
      if (!window.confirm('把「' + rmName + '」从该任务成员中移除？')) return;
      fetch(SLIRN_API + '/auth/tasks/' + encodeURIComponent(tidR) + '/members/' + encodeURIComponent(rmName), {
        method: 'DELETE', credentials: 'same-origin'
      }).then(function(r) { return r.json(); }).then(function(r) {
        if (r && r.ok) { _memberMsg('✅ 已移除：' + rmName); _loadTaskMembers(); }
        else _memberMsg((r && r.error) || '移除失败', true);
      }).catch(function(e) { _memberMsg(String(e), true); });
    }
    else if (action === 'rev-detail') {
      var rowD = target.closest('.slirn-rev-row');
      if (rowD) {
        var opened = rowD.classList.toggle('open');
        target.textContent = opened ? '▴' : '▾';
      }
    }
    else if (action === 'open-llm-settings') {
      openLLMSettings();
    }
    else if (action === 'open-sys-settings') {
      openSysSettings();
    }
    else if (action === 'sys-probe') {
      sysSettingsProbe();
    }
    else if (action === 'sys-ctx-probe') {
      sysCtxProbe();
    }
    else if (action === 'sys-save') {
      sysSettingsSave();
    }
    else if (action === 'sys-close') {
      var m = document.getElementById('slirn-sys-modal');
      if (m) m.remove();
    }
    else if (action === 'llm-goto-usage') {
      var lm = document.getElementById('slirn-llm-modal');
      if (lm) lm.remove();
      showTab('slirn-tab-usage');
    }
    else if (action === 'usage-page-filter') {
      USAGE_STATE.page = 1;
      loadUsageDetail();
    }
    else if (action === 'usage-page-reset') {
      ['slirn-usage-filter', 'slirn-usage-model', 'slirn-usage-stage', 'slirn-usage-from',
       'slirn-usage-to', 'slirn-usage-dmin', 'slirn-usage-dmax'].forEach(function(id) {
        var elx = document.getElementById(id);
        if (elx) elx.value = '';
      });
      USAGE_STATE.page = 1;
      loadUsageDetail();
    }
    else if (action === 'usage-page-nav') {
      var npage = parseInt(target.getAttribute('data-page') || '1', 10);
      if (isFinite(npage) && npage >= 1) {
        USAGE_STATE.page = npage;
        loadUsageDetail();
      }
    }
    else if (action === 'usage-page-size') {
      var nsize = parseInt(target.getAttribute('data-size') || '50', 10);
      if (isFinite(nsize) && nsize >= 1) {
        USAGE_STATE.size = nsize;
        USAGE_STATE.page = 1;
        loadUsageDetail();
      }
    }
    else if (action === 'llm-close') {
      var llmModal = document.getElementById('slirn-llm-modal');
      if (llmModal) llmModal.remove();
    }
    else if (action === 'llm-add') {
      var gv = function(elId) { return ((document.getElementById(elId) || {}).value || '').trim(); };
      var protoSel = document.getElementById('slirn-llm-in-proto');
      var visionChk = document.getElementById('slirn-llm-in-vision');
      var editing = LLM_EDIT_ID;  // 非空 = 当前是编辑模式 → 提交修改
      var payload = {
        id: gv('slirn-llm-in-id'), provider: gv('slirn-llm-in-provider'),
        base_url: gv('slirn-llm-in-url'), api_key_env: gv('slirn-llm-in-env'),
        protocol: protoSel ? protoSel.value : 'openai',
        vision: !!(visionChk && visionChk.checked),
      };
      postJSON(SLIRN_API + '/llm_config/' + (editing ? 'update' : 'add'), editing
        ? {id: editing, new_id: payload.id, provider: payload.provider,
           base_url: payload.base_url, api_key_env: payload.api_key_env,
           protocol: payload.protocol, vision: payload.vision}
        : payload).then(function(r) {
        if (r && r.ok) {
          toast(r.toast || (editing ? '已更新' : '已添加'));
          renderLLMList(r.models || [], r.current || '');
          llmFormReset();
        } else {
          toast('❌ ' + ((r && r.error) || '操作失败'), 'error');
        }
      });
    }
    else if (action === 'llm-edit') {
      var editId = target.getAttribute('data-id') || '';
      var m = LLM_MODELS.filter(function(x) { return x.id === editId; })[0];
      if (!m) return;
      LLM_EDIT_ID = editId;
      document.getElementById('slirn-llm-in-id').value = m.id;
      document.getElementById('slirn-llm-in-provider').value = m.provider || '';
      document.getElementById('slirn-llm-in-url').value = m.base_url || '';
      document.getElementById('slirn-llm-in-env').value = m.api_key_env || '';
      var protoSel2 = document.getElementById('slirn-llm-in-proto');
      if (protoSel2) protoSel2.value = (m.protocol === 'anthropic') ? 'anthropic' : 'openai';
      var visionEditChk = document.getElementById('slirn-llm-in-vision');
      if (visionEditChk) visionEditChk.checked = !!m.vision;
      var ft = document.getElementById('slirn-llm-form-title');
      if (ft) ft.textContent = '修改模型（' + editId + '）';
      var ab = document.getElementById('slirn-llm-add-btn');
      if (ab) ab.textContent = '💾 保存修改';
      var cb = document.getElementById('slirn-llm-cancel-btn');
      if (cb) cb.style.display = '';
      var formEl = document.getElementById('slirn-llm-form');
      if (formEl && formEl.scrollIntoView) formEl.scrollIntoView({block: 'nearest', behavior: 'smooth'});
    }
    else if (action === 'llm-cancel-edit') {
      llmFormReset();
    }
    else if (action === 'llm-use' || action === 'llm-remove') {
      var llmId = target.getAttribute('data-id') || '';
      postJSON(SLIRN_API + '/llm_config/' + action.slice(4), {id: llmId}).then(function(r) {
        if (r && r.ok) {
          toast(r.toast || '已更新');
          renderLLMList(r.models || [], r.current || '');
        } else {
          toast('❌ ' + ((r && r.error) || '操作失败'), 'error');
        }
      });
    }
    else if (action === 'llm-test') {
      var testId = target.getAttribute('data-id') || '';
      var testBox = document.getElementById('slirn-llm-test-result');
      if (testBox) { testBox.className = 'slirn-llm-test-result'; testBox.textContent = '⏳ 测试中（' + testId + ' 真实调用一次）…'; }
      postJSON(SLIRN_API + '/llm_test', {id: testId}).then(function(r) {
        if (!testBox) return;
        if (r && r.ok) {
          testBox.className = 'slirn-llm-test-result ok';
          testBox.textContent = '✅ 模型可用（' + r.model + ' · 耗时 '
            + (r.elapsed_s || 0).toFixed(1) + 's · 回复「' + (r.reply || '') + '」）';
        } else {
          testBox.className = 'slirn-llm-test-result err';
          testBox.textContent = '❌ ' + ((r && r.error) || '测试失败');
        }
      });
    }
    else if (action === 'close-detail') {
      var d = document.getElementById('slirn-tab-detail');
      if (d) d.style.display = 'none';
    }
    else if (action === 'delete-task') {
      // REQ-20260917-034：删除不可恢复 + 整目录真删 — 必须先确认（含任务名）
      var tid2 = target.getAttribute('data-task-id') || '';
      var name2 = target.getAttribute('data-task-name') || tid2;
      if (!window.confirm('🗑️ 确认删除任务「' + name2 + '」？\n\n'
          + '任务目录的全部文件（原视频链接、字幕、切分决策、合成产物等）'
          + '将从磁盘永久删除，不可恢复。')) return;
      // 释放正在预览的任务视频（Windows 下句柄未松开会 rmtree 失败）
      document.querySelectorAll('video').forEach(function(v) {
        try { v.pause(); v.removeAttribute('src'); v.load(); } catch (err) {}
      });
      postJSON(SLIRN_API + '/delete_task', {task_id: tid2}).then(function(r) {
        handleResp(r, 'slirn-tab-tasks');
        var d = document.getElementById('slirn-tab-detail');
        if (d) d.style.display = 'none';
      });
    }
    else if (action === 'edit-task') {
      var tidE = target.getAttribute('data-task-id') || '';
      // REQ-20260917-037：大任务编辑要读视频信息/热词，加载慢 — 点击即给加载态
      var origTextE = target.textContent;
      target.disabled = true;
      target.textContent = '⏳ 加载中…';
      var cEl = document.getElementById('slirn-tab-create');
      if (cEl) {
        cEl.innerHTML = '<div class="slirn-card" style="margin-top:16px;">'
          + '<div class="slirn-loading-box"><div class="slirn-spinner"></div>'
          + '<div>⏳ 正在加载任务（读取视频信息与热词，大视频稍慢）…</div></div></div>';
        cEl.style.display = '';
        ALL_TABS.forEach(function(id) {
          if (id !== 'slirn-tab-create') { var el = document.getElementById(id); if (el) el.style.display = 'none'; }
        });
      }
      var restoreTabsE = function() {
        target.disabled = false;
        target.textContent = origTextE;
        var tEl = document.getElementById('slirn-tab-tasks');
        var c2 = document.getElementById('slirn-tab-create');
        if (c2) c2.style.display = 'none';
        if (tEl) tEl.style.display = '';
      };
      _loadEditTaskHTML(tidE).then(function(r) {
        target.disabled = false;
        target.textContent = origTextE;
        if (r && r.ok) {
          // REQ-20260926-NNN：写 #edit= hash → F5/Cmd+R 留在编辑页（不进列表）
          _setTaskPageHash('edit', tidE);
        } else if (r && r.error) {
          restoreTabsE();
          toast('❌ ' + r.error, 'error');
        }
      }, function() {
        restoreTabsE();
        toast('❌ 网络错误，请重试', 'error');
      });
    }
    else if (action === 'open-workbench') {
      var tidW = target.getAttribute('data-task-id') || '';
      // REQ-20260918-051：内容多的任务接口耗时长，先弹等待动画再去拉接口
      var labelW = target.getAttribute('data-task-label') || tidW;
      showWbLoader(tidW, labelW);
      openWorkbench(tidW);
    }
    else if (action === 'wb-stage') {
      switchWbPane(target.getAttribute('data-pane') || '');
    }
    else if (action === 'update-task') {
      var tidU = target.getAttribute('data-task-id') || window.slirnEditTaskId || '';
      var sU = getInput('slirn-start-box'), eU = getInput('slirn-end-box');
      var nU = getInput('slirn-task-name');
      var manualU = getInput('slirn-hotwords-manual');
      var inheritElU = document.getElementById('slirn-hw-inherit-all');
      var inheritU = inheritElU ? !!inheritElU.checked : false;
      var pickedU = collectPickedHotwords();
      postJSON(SLIRN_API + '/update_task', {
        task_id: tidU, start: sU, end: eU, name: nU,
        inherit_public: inheritU, picked_words: pickedU, manual_words: manualU,
      }).then(function(r) {
        if (r && r.ok) {
          toast(r.toast || '✅ 已保存');
          // REQ-20260926-NNN：保存后原地刷新编辑页（不跳任务列表）；
          // 复用 _loadEditTaskHTML 重拉 HTML 注回 + 保留 #edit=<tid> hash。
          _loadEditTaskHTML(tidU).then(function(r2) {
            if (r2 && r2.ok) {
              _setTaskPageHash('edit', tidU);  // 保险：hash 已是 edit，刷一次确保不被覆盖
            }
          });
        } else if (r && r.error) {
          toast('❌ ' + r.error, 'error');
        }
      });
    }
    else if (action === 'set-start') {
      var seek = document.getElementById('slirn-seek');
      if (seek) document.getElementById('slirn-start-box').value = secondsToHMS(parseFloat(seek.value));
    }
    else if (action === 'set-end') {
      var seek2 = document.getElementById('slirn-seek');
      if (seek2) document.getElementById('slirn-end-box').value = secondsToHMS(parseFloat(seek2.value));
    }
    else if (action === 'cut-preview') {
      var s = getInput('slirn-start-box'), e2 = getInput('slirn-end-box');
      // 客户端校验：先看时间格式 + 开始<结束 再发请求
      var sSec = hmsToSeconds(s);
      var eSec = hmsToSeconds(e2);
      if (sSec == null) { toast('❌ 开始时间格式错误（应为 HH:MM:SS.mmm）', 'error'); return; }
      if (eSec == null) { toast('❌ 结束时间格式错误（应为 HH:MM:SS.mmm）', 'error'); return; }
      if (sSec >= eSec) { toast('❌ 开始时间必须小于结束时间（' + s + ' ≥ ' + e2 + '）', 'error'); return; }
      postJSON(SLIRN_API + '/cut_preview', {path: window.slirnSelectedFile, start: s, end: e2})
        .then(function(r) { handleResp(r, 'slirn-tab-create'); });
    }
    else if (action === 'create-task') {
      var s1 = getInput('slirn-start-box'), e1 = getInput('slirn-end-box');
      var n = getInput('slirn-task-name');
      var manual = getInput('slirn-hotwords-manual');
      var inheritEl = document.getElementById('slirn-hw-inherit-all');
      var inherit = inheritEl ? !!inheritEl.checked : false;
      var picked = collectPickedHotwords();
      postJSON(SLIRN_API + '/create_task', {
        path: window.slirnSelectedFile, start: s1, end: e1, name: n,
        inherit_public: inherit,
        picked_words: picked,
        manual_words: manual,
      }).then(function(r) {
        if (r && r.ok) {
          handleResp(r, 'slirn-tab-tasks');
          showTab('slirn-tab-tasks');
        }
      });
    }
    else if (action === 'cancel-create') {
      postJSON(SLIRN_API + '/cancel_create', {}).then(function(r) {
        _clearTaskPageHash();  // REQ-20260926-NNN：取消编辑 → 清 hash 防刷新跳回
        handleResp(r, 'slirn-tab-tasks');
        showTab('slirn-tab-tasks');
      });
    }
    else if (action === 'hw-add') {
      var w = getInput('slirn-hw-word'), cat = getInput('slirn-hw-category');
      postJSON(SLIRN_API + '/hw_add', {word: w, category: cat})
        .then(function(r) {
          if (r && r.ok) {
            // 清空词输入框（保留分类方便连续添加同一分类的词）
            var ta = document.getElementById('slirn-hw-word');
            if (ta) ta.value = '';
          }
          handleResp(r, 'slirn-tab-hotwords');
        });
    }
    else if (action === 'hw-analyze') {
      // REQ-20260926-NNN：调 LLM 提取热词 → 渲染候选网格
      var req = getInput('slirn-hw-analysis-requirement');
      var urlV = getInput('slirn-hw-analysis-url');
      var hintV = getInput('slirn-hw-analysis-hint');
      var status = document.getElementById('slirn-hw-analysis-status');
      if (status) status.textContent = '🔄 正在分析…';
      postJSON(SLIRN_API + '/hotword_extract',
        {requirement: req, url: urlV, page_hint: hintV})
        .then(function(r) {
          if (!(r && r.ok)) {
            if (status) status.textContent = '❌ ' + ((r && r.error) || '失败');
            return;
          }
          renderAnalysisGrid(r.candidates || []);
          if (status) status.textContent = '✅ 找到 ' + (r.candidates || []).length + ' 个候选';
        });
    }
    else if (action === 'hw-analysis-add-task') {
      // 选中候选 → 以半角逗号拼回手动 textarea（不走空格，防多词热词塌缩）
      var selA = [].slice.call(
        document.querySelectorAll('#slirn-hw-analysis-grid .slirn-pick-cell.selected')
      ).map(function(c) { return c.getAttribute('data-word'); });
      if (!selA.length) { toast('请先选中候选词'); return; }
      var taA = document.getElementById('slirn-hotwords-manual');
      if (taA) {
        var cur = parseManual(taA.value);
        var merged = cur.concat(selA.filter(function(w) { return cur.indexOf(w) < 0; }));
        taA.value = merged.join(',');
      }
      renderHwChips();
      toast('已加入任务：' + selA.length + ' 个');
    }
    else if (action === 'hw-analysis-add-public') {
      var selB = [].slice.call(
        document.querySelectorAll('#slirn-hw-analysis-grid .slirn-pick-cell.selected')
      ).map(function(c) { return c.getAttribute('data-word'); });
      if (!selB.length) { toast('请先选中候选词'); return; }
      var catB = window.prompt('分类（留空 = 默认）', '') || '';
      postJSON(SLIRN_API + '/hw_add', {words: selB, category: catB})
        .then(function(r) { handleResp(r, 'slirn-tab-hotwords'); });
    }
    else if (action === 'hw-delete-one') {
      // 阻止冒泡到 cell 上的潜在 click 监听
      if (e) { e.stopPropagation(); }
      var wd = target.getAttribute('data-word') || '';
      if (!wd) return;
      if (!confirm('删除热词「' + wd + '」？')) return;
      postJSON(SLIRN_API + '/hw_remove', {word: wd})
        .then(function(r) { handleResp(r, 'slirn-tab-hotwords'); });
    }
    else if (action === 'hw-cat-select-all' || action === 'hw-cat-invert' || action === 'hw-cat-delete') {
      // 找当前按钮所在的分类 section
      var section = target.closest('.slirn-category-section');
      if (!section) return;
      var cells = section.querySelectorAll('.slirn-hotword-cell:not(.empty)');
      if (action === 'hw-cat-select-all') {
        cells.forEach(function(c) { c.classList.add('selected'); });
        updateDeleteCount(section);
        // REQ-20260926-NNN：picker 也用同一 action — bulk 选后刷新 chips
        try { renderHwChips(); } catch (e) {}
      } else if (action === 'hw-cat-invert') {
        cells.forEach(function(c) { c.classList.toggle('selected'); });
        updateDeleteCount(section);
        try { renderHwChips(); } catch (e) {}
      } else if (action === 'hw-cat-delete') {
        var selected = section.querySelectorAll('.slirn-hotword-cell.selected');
        var words = [];
        selected.forEach(function(c) {
          var ww = c.getAttribute('data-word');
          if (ww) words.push(ww);
        });
        if (!words.length) {
          toast('请先用 全选 / 反选 勾选要删除的词', 'error');
          return;
        }
        if (!confirm('确认删除 ' + words.length + ' 个词？')) return;
        postJSON(SLIRN_API + '/hw_remove', {words: words})
          .then(function(r) { handleResp(r, 'slirn-tab-hotwords'); });
      }
    }
    else if (action === 'trigger-file') {
      // 动态创建一个临时 file input — 每次新创建才能避开 value 缓存（重选同一文件也能触发 change）
      var tmp = document.createElement('input');
      tmp.type = 'file';
      tmp.accept = '.mp4,.avi,.mkv,.mov,.webm,.ts,.mpeg,.m4v,.flv,.wmv,video/*';
      tmp.style.display = 'none';
      tmp.addEventListener('change', function() {
        var files = tmp.files;
        document.body.removeChild(tmp);
        if (!files || !files.length) return;
        var file = files[0];
        uploadFile(file).then(function(path) {
          closeUploadDialog();
          window.slirnSelectedFile = path;
          toast('✅ 上传完成，正在解析视频…');
          return postJSON(SLIRN_API + '/file_selected', {path: path});
        }).then(function(r) {
          closeUploadDialog();
          handleResp(r, 'slirn-tab-create');
        }).catch(function(err) {
          closeUploadDialog();
          toast('❌ 上传失败: ' + err, 'error');
        });
      });
      document.body.appendChild(tmp);
      tmp.click();
    }
    else if (action === 'url-download') {
      var uin = document.getElementById('slirn-url-input');
      var url = (uin && uin.value || '').trim();
      if (!url) { toast('请先填写视频 URL', 'error'); return; }
      startUrlDownload(url);
    }
    else if (action === 'url-download-cancel') {
      cancelUrlDownload(target.getAttribute('data-job-id'));
    }
  });

  // ===== 拖拽上传 — dropzone 区域支持把视频文件拖进来 =====
  function slirnDragUpload(file) {
    uploadFile(file).then(function(path) {
      closeUploadDialog();
      window.slirnSelectedFile = path;
      toast('✅ 上传完成，正在解析视频…');
      return postJSON(SLIRN_API + '/file_selected', {path: path});
    }).then(function(r) {
      closeUploadDialog();
      handleResp(r, 'slirn-tab-create');
    }).catch(function(err) {
      closeUploadDialog();
      toast('❌ 上传失败: ' + err, 'error');
    });
  }

  // ===== URL 下载 — 新建任务的视频来源之二：粘贴链接，服务端流式下载 =====
  var slirnUrlDl = null; // 当前轮询句柄 {id, timer}

  function fmtBytes(n) {
    if (n >= 1024 * 1024 * 1024) return (n / 1024 / 1024 / 1024).toFixed(2) + ' GB';
    if (n >= 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + ' MB';
    return Math.max(1, Math.round(n / 1024)) + ' KB';
  }

  function stopUrlDlPoll() {
    if (slirnUrlDl && slirnUrlDl.timer) clearInterval(slirnUrlDl.timer);
    slirnUrlDl = null;
    var b = document.getElementById('slirn-url-dl-btn');
    if (b) b.disabled = false;
  }

  function urlDlFail(msg) {
    stopUrlDlPoll();
    var box = document.getElementById('slirn-url-progress');
    if (box) {
      box.style.display = 'block';
      box.innerHTML = '<div class="slirn-form-hint" style="color:#c0392b;">❌ ' + msg + '</div>';
    }
  }

  function pollUrlDl() {
    if (!slirnUrlDl) return;
    var id = slirnUrlDl.id;
    postJSON(SLIRN_API + '/url_download_status', {job_id: id}).then(function(j) {
      if (!slirnUrlDl || slirnUrlDl.id !== id) return;   // 已被新下载顶掉
      var job = j.job || {};
      var box = document.getElementById('slirn-url-progress');
      if (!box) { stopUrlDlPoll(); return; }             // 页面已重渲染（进度条随 DOM 消失）
      if (job.state === 'running') {
        var rc = job.received_bytes || 0, tot = job.total_bytes;
        var pct = tot ? Math.floor(rc / tot * 100) : null;
        box.style.display = 'block';
        box.innerHTML =
          '<div class="slirn-form-hint">⬇ 正在下载 <b>' + escapeHtml(job.name || '…') +
          '</b> — ' + (pct !== null ? pct + '% · ' : '') + fmtBytes(rc) +
          (tot ? ' / ' + fmtBytes(tot) : '') + '</div>' +
          '<button class="slirn-btn" data-action="url-download-cancel" data-job-id="' +
          escapeAttr(id) + '" style="margin-top:6px;">✖ 取消下载</button>';
        return;
      }
      // 终态：先停轮询（done 分支的 handleResp 会整块重渲染，杀掉进度条 DOM）
      stopUrlDlPoll();
      if (job.state === 'done' && job.path) {
        box.innerHTML = '<div class="slirn-form-hint">✅ 下载完成，正在解析视频…</div>';
        window.slirnSelectedFile = job.path;
        postJSON(SLIRN_API + '/file_selected', {path: job.path}).then(function(r) {
          handleResp(r, 'slirn-tab-create');   // 与上传完成同链路：重渲染 + 回填播放器
        }).catch(function(err) {
          toast('❌ 解析失败: ' + err, 'error');
        });
      } else if (job.state === 'cancelled') {
        box.innerHTML = '<div class="slirn-form-hint">已取消下载</div>';
      } else {
        box.style.display = 'block';
        box.innerHTML = '<div class="slirn-form-hint" style="color:#c0392b;">❌ ' +
          escapeHtml(job.error || '下载失败') + '</div>';
      }
    }).catch(function(err) {
      urlDlFail('状态查询失败: ' + err);
    });
  }

  function startUrlDownload(url) {
    stopUrlDlPoll();  // 顶掉上一个未完成的轮询
    var box = document.getElementById('slirn-url-progress');
    if (box) {
      box.style.display = 'block';
      box.innerHTML = '<div class="slirn-form-hint">⬇ 正在建立下载…</div>';
    }
    var btn = document.getElementById('slirn-url-dl-btn');
    if (btn) btn.disabled = true;
    postJSON(SLIRN_API + '/url_download_start', {url: url}).then(function(j) {
      slirnUrlDl = {id: (j.job || {}).job_id, timer: null};
      slirnUrlDl.timer = setInterval(pollUrlDl, 1000);
      pollUrlDl();
    }).catch(function(err) {
      urlDlFail(err);
    });
  }

  function cancelUrlDownload(jobId) {
    if (!jobId) return;
    postJSON(SLIRN_API + '/url_download_cancel', {job_id: jobId}).then(function() {
      toast('已请求取消，正在终止…');
    }).catch(function(err) {
      toast('取消失败: ' + err, 'error');
    });
  }

  // URL 输入框回车 = 点下载；用量页筛选框回车 = 筛选
  document.addEventListener('keydown', function(e) {
    if (e.key !== 'Enter') return;
    var el = e.target;
    if (el && el.id === 'slirn-url-input') {
      e.preventDefault();
      var b = document.getElementById('slirn-url-dl-btn');
      if (b && !b.disabled) b.click();
    } else if (el && el.classList && el.classList.contains('slirn-usage-flt')) {
      e.preventDefault();
      USAGE_STATE.page = 1;
      loadUsageDetail();
    }
  });
  document.addEventListener('dragover', function(e) {
    var dz = e.target && e.target.closest && e.target.closest('.slirn-dropzone');
    if (!dz) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
    dz.classList.add('drag-over');
  });
  document.addEventListener('dragleave', function(e) {
    var dz = e.target && e.target.closest && e.target.closest('.slirn-dropzone');
    if (dz) {
      dz.classList.remove('drag-over');
    }
  });
  document.addEventListener('drop', function(e) {
    var dz = e.target && e.target.closest && e.target.closest('.slirn-dropzone');
    if (!dz) return;
    e.preventDefault();
    dz.classList.remove('drag-over');
    var files = e.dataTransfer && e.dataTransfer.files;
    if (!files || !files.length) {
      toast('⚠️ 未识别到文件，请拖入视频文件', 'warning');
      return;
    }
    var file = files[0];
    if (!file.type.startsWith('video/') && !/\.(mp4|avi|mkv|mov|webm|ts|mpeg|m4v|flv|wmv)$/i.test(file.name)) {
      toast('⚠️ 请拖入视频文件（mp4/avi/mkv/...）', 'warning');
      return;
    }
    slirnDragUpload(file);
  });

  // ===== 文件选择 — 改由 trigger-file 处理器动态创建 input 并就地监听 change =====
  // 不再用全局 change 监听，避免拖拽上传时双触发

  // ===== Slider / Video 同步 =====
  document.addEventListener('input', function(e) {
    if (e.target && e.target.id === 'slirn-seek') {
      var v = document.getElementById('slirn-player');
      if (v) v.currentTime = parseFloat(e.target.value);
    }
  });
  document.addEventListener('timeupdate', function(e) {
    if (e.target && e.target.id === 'slirn-player') {
      var v = e.target;
      var s = document.getElementById('slirn-seek');
      var d = document.getElementById('slirn-time-display');
      if (s) s.value = v.currentTime;
      if (d) d.textContent = secondsToHMS(v.currentTime) + ' / ' + secondsToHMS(v.duration);
      if (s && v.duration) s.max = v.duration;
    }
  });

  console.log('[slirn] router initialized (custom /slirn/api mode)');

  // ===== REQ-20260918-053 + REQ-20260926-NNN：URL hash 持久化 =====
  //   写入端：openWorkbench → #wb=<tid>（自动清 #edit=）
  //          edit-task click  / hash 恢复 → #edit=<tid>（自动清 #wb=）
  //   离开任务页（cancel/goto-*）→ 清空 hash，避免刷新跳回。
  // 读取端：DOMContentLoaded → 解析 hash → 切到对应 tab + 加载内容。
  function _restoreTaskPageFromHash() {
    var hash = (location && location.hash) ? location.hash : '';
    var m = hash.match(/^#(wb|edit)=(.+)$/);
    if (!m) return;
    var kind = m[1];
    var tid = decodeURIComponent(m[2] || '').trim();
    if (!tid) return;
    if (kind === 'wb') {
      try { showTab('slirn-tab-workbench'); } catch (e) {}
      setTimeout(function() { try { openWorkbench(tid); } catch (e) {} }, 0);
    } else if (kind === 'edit') {
      // 编辑页加载慢（要读视频信息+热词），先给加载态
      var cEl = document.getElementById('slirn-tab-create');
      if (cEl) {
        cEl.innerHTML = '<div class="slirn-card" style="margin-top:16px;">'
          + '<div class="slirn-loading-box"><div class="slirn-spinner"></div>'
          + '<div>⏳ 正在加载编辑页（读取视频信息与热词）…</div></div></div>';
        cEl.style.display = '';
        ALL_TABS.forEach(function(id) {
          if (id !== 'slirn-tab-create') { var el = document.getElementById(id); if (el) el.style.display = 'none'; }
        });
      }
      setTimeout(function() { try { _loadEditTaskHTML(tid); } catch (e) {} }, 0);
    }
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _restoreTaskPageFromHash);
  } else {
    try { _restoreTaskPageFromHash(); } catch (e) {}
  }

  // REQ-20261022-146：初始化即发布动作表与键位表 —— 硬件按键模块要在
  // **任何时候**都能取到「动作 → 当前键位」（此前只在打开快捷键弹窗时发布，
  // 导致平时按硬件键无效）。
  try { revKeysPublish(); } catch (e) {}
})();
