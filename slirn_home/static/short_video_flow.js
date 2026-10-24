/* REQ-20261022-143 — 短视频「流程配置」面板 + 自动执行。
 *
 * 参照长视频工作台的流程配置范式（static/pipeline.js + pipeline_service.py）：
 * 面板集中各阶段参数 → 保存到服务端 → 点「执行」后台按阶段顺序自动跑到停点，
 * 前端只做「表单 ↔ config 映射 + 触发 + 轮询状态条」。
 *
 * 与长视频的差异：编排走服务端进程内直调模块（short_video_flow.py），
 * 不做 HTTP 回环 —— 短视频阶段函数是同步纯模块调用，无需会话透传/端口发现。
 */
(function () {
  if (window.__slirnShortVideoFlowBound) return;
  window.__slirnShortVideoFlowBound = true;

  var API = '/slirn/api';

  // 阶段表（与后端 short_video_flow.STAGE_ORDER 一致）
  var STAGES = [
    {key: 'stage2_extract', label: '字幕提取', desc: 'FunASR 识别源视频字幕（生成后续解析所需字幕）'},
    {key: 'stage3_analyze', label: '内容提取', desc: '按提示词解析出 N 条独立短视频片段'},
    {key: 'stage4_optimize', label: '内容优化', desc: '大模型切分长行 / 修正文字 / 删除冗余字幕行'},
    {key: 'stage5_coarse', label: '粗剪合成', desc: '按解析结果生成候选短视频片段（粗剪成片）'}
  ];
  var STAGE_KEYS = STAGES.map(function (s) { return s.key; });
  var STAGE_LABELS = {};
  STAGES.forEach(function (s) { STAGE_LABELS[s.key] = s.label; });

  var TIMPLATES = [
    {v: 'hook_first', t: '钩子优先', d: '第一条放最抓人的钩子'},
    {v: 'topic_cluster', t: '主题聚类', d: '按内容主题分成独立知识小卡'},
    {v: 'story_arc', t: '起承转合', d: '每条都是完整的小故事弧'}
  ];

  var pollTimer = null;
  var ctx = {taskId: '', projectId: ''};

  // ---------- 工具 ----------
  function postJSON(url, payload) {
    return fetch(url, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload || {})
    }).then(function (r) { return r.json(); })
      .catch(function (e) { return {ok: false, error: String(e)}; });
  }
  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&').replace(/</g, '<').replace(/>/g, '>')
      .replace(/"/g, '"').replace(/'/g, '&#39;');
  }
  function toast(msg, type) {
    if (window.slirnToast) window.slirnToast(msg, type || 'success');
  }
  function fieldId(stageKey, fieldName) { return 'slirn-sv-flow-' + stageKey + '-' + fieldName; }
  function _ctx() {
    // 上下文优先从面板自身读（服务端渲染时带上）——不依赖脚本执行顺序；
    // 回退到项目容器。
    var panel = document.getElementById('slirn-sv-flow-panel');
    var host = document.getElementById('slirn-short-video-inner');
    var el = panel || host;
    if (el) {
      ctx.taskId = el.getAttribute('data-task-id') || '';
      ctx.projectId = el.getAttribute('data-project-id') || '';
    }
    if ((!ctx.taskId || !ctx.projectId) && host) {
      ctx.taskId = ctx.taskId || (host.getAttribute('data-task-id') || '');
      ctx.projectId = ctx.projectId || (host.getAttribute('data-project-id') || '');
    }
    return ctx;
  }

  // ---------- 面板渲染 ----------
  function renderStageForm(stage, cfg) {
    if (stage.key === 'stage2_extract') {
      return '<div class="slirn-pipe-form">'
        + '<div class="slirn-pipe-desc">' + esc(stage.desc) + '</div>'
        + '<label class="slirn-pipe-field">识别模型'
        + '<select id="' + fieldId(stage.key, 'model') + '">'
        + '<option value="paraformer"' + (cfg.model !== 'sensevoice' && cfg.model !== 'fun-asr-nano' ? ' selected' : '') + '>paraformer（中文）</option>'
        + '<option value="sensevoice"' + (cfg.model === 'sensevoice' ? ' selected' : '') + '>sensevoice（多语种）</option>'
        + '<option value="fun-asr-nano"' + (cfg.model === 'fun-asr-nano' ? ' selected' : '') + '>fun-asr-nano</option>'
        + '</select></label>'
        + '<label class="slirn-pipe-field">语言'
        + '<select id="' + fieldId(stage.key, 'lang') + '">'
        + '<option value="zh"' + (cfg.lang !== 'en' ? ' selected' : '') + '>中文</option>'
        + '<option value="en"' + (cfg.lang === 'en' ? ' selected' : '') + '>英文</option>'
        + '</select></label>'
        + '<label class="slirn-pipe-field">热词（可选，逗号分隔）'
        + '<input id="' + fieldId(stage.key, 'hotwords') + '" value="' + esc(cfg.hotwords || '') + '" placeholder="半角逗号分隔"></label>'
        + '</div>';
    }
    if (stage.key === 'stage3_analyze') {
      var tplOpts = TIMPLATES.map(function (t) {
        return '<option value="' + t.v + '"' + (cfg.template === t.v ? ' selected' : '') + '>' + esc(t.t) + '</option>';
      }).join('');
      return '<div class="slirn-pipe-form">'
        + '<div class="slirn-pipe-desc">' + esc(stage.desc) + '</div>'
        + '<label class="slirn-pipe-field">提示词块（系统提示词模板）'
        + '<select id="' + fieldId(stage.key, 'template') + '">' + tplOpts + '</select>'
        + '<span class="slirn-pipe-hint">模板即系统提示词；下方「解析提示词」留空则用模板原文</span></label>'
        + '<label class="slirn-pipe-field">解析提示词（可选，覆盖系统提示词）'
        + '<textarea id="' + fieldId(stage.key, 'prompt') + '" rows="3" placeholder="留空 = 使用所选模板的系统提示词">'
        + esc(cfg.prompt || '') + '</textarea></label>'
        + '<label class="slirn-pipe-field">生成数量（条短视频）'
        + '<input type="number" min="1" max="10" id="' + fieldId(stage.key, 'n_clips') + '" value="' + esc(cfg.n_clips || 4) + '">'
        + '<span class="slirn-pipe-hint">⚠️ 人工设置</span></label>'
        + '<label class="slirn-pipe-field">每条时长下限（秒）'
        + '<input type="number" min="10" max="600" step="5" id="' + fieldId(stage.key, 'duration_min_sec') + '" value="' + esc(cfg.duration_min_sec || 60) + '">'
        + '<span class="slirn-pipe-hint">⚠️ 人工设置</span></label>'
        + '<label class="slirn-pipe-field">每条时长上限（秒）'
        + '<input type="number" min="20" max="900" step="5" id="' + fieldId(stage.key, 'duration_max_sec') + '" value="' + esc(cfg.duration_max_sec || 90) + '">'
        + '<span class="slirn-pipe-hint">⚠️ 人工设置</span></label>'
        + '<label class="slirn-pipe-field">'
        + '<input type="checkbox" id="' + fieldId(stage.key, 'use_image') + '"' + (cfg.use_image ? ' checked' : '') + '> 发送截图给 AI'
        + '</label>'
        + '</div>';
    }
    if (stage.key === 'stage4_optimize') {
      return '<div class="slirn-pipe-form">'
        + '<div class="slirn-pipe-desc">' + esc(stage.desc) + '</div>'
        + '<div class="slirn-pipe-hint">使用内置系统提示词，无可配置项。</div>'
        + '</div>';
    }
    // stage5_coarse
    return '<div class="slirn-pipe-form">'
      + '<div class="slirn-pipe-desc">' + esc(stage.desc) + '</div>'
      + '<label class="slirn-pipe-field">起始偏移（毫秒，叠加到片段起点）'
      + '<input type="number" min="0" max="600000" step="100" id="' + fieldId(stage.key, 'start_ost') + '" value="' + esc(cfg.start_ost || 0) + '"></label>'
      + '<label class="slirn-pipe-field">结束偏移（毫秒，叠加到片段终点）'
      + '<input type="number" min="0" max="600000" step="100" id="' + fieldId(stage.key, 'end_ost') + '" value="' + esc(cfg.end_ost != null ? cfg.end_ost : 100) + '"></label>'
      + '</div>';
  }

  function renderStopOptions(selected, suffix) {
    // suffix：终点下拉用「后停」（跑到该阶段为止）；起点下拉只写阶段名
    // （「内容提取后停」用在「从哪一阶段开始跑」上会被误解成停点）
    var tail = (suffix === undefined || suffix === null) ? '后停' : suffix;
    var opts = '';
    STAGES.forEach(function (s) {
      opts += '<option value="' + s.key + '"' + (selected === s.key ? ' selected' : '') + '>'
            + esc(s.label) + tail + '</option>';
    });
    return opts;
  }

  function renderPanel(cfg, history) {
    var panel = document.getElementById('slirn-sv-flow-panel');
    if (!panel) return;
    var sections = STAGES.map(function (s, i) {
      var sc = (cfg && cfg[s.key]) || {};
      var on = sc.enabled !== false;
      return '<details class="slirn-pipe-section" data-sv-flow-section="' + s.key + '" open>'
        + '<summary class="slirn-pipe-section-summary">'
        + '<span class="slirn-pipe-section-num">' + (i + 2) + '</span>'
        + '<span class="slirn-pipe-section-label">' + esc(s.label) + '</span>'
        + '<span class="slirn-pipe-section-hint">' + esc(s.desc) + '</span>'
        + '<span class="slirn-pipe-section-state" data-sv-flow-state="' + s.key + '">⏸ 待执行</span>'
        + '</summary>'
        + '<div class="slirn-pipe-section-body">'
        + '<label class="slirn-pipe-field"><input type="checkbox" id="' + fieldId(s.key, 'enabled') + '"' + (on ? ' checked' : '') + '> 启用此阶段</label>'
        + renderStageForm(s, sc)
        + '</div></details>';
    }).join('');

    var runMode = (cfg && cfg.run_mode) || 'stop_after';
    var stopAfter = (cfg && cfg.stop_after) || 'stage5_coarse';
    var since = (cfg && cfg.since) || 'stage2_extract';

    panel.innerHTML = '<details class="slirn-pipe-panel-details" open>'
      + '<summary class="slirn-pipe-head">'
      + '<span class="slirn-pipe-title">⚙ 流程配置（自动执行）</span>'
      + '<span class="slirn-pipe-head-acts">'
      + '<label class="slirn-pipe-run-mode-label">运行模式：'
      + '<select class="slirn-pipe-run-mode" id="slirn-sv-flow-run-mode">'
      + '<option value="stop_after"' + (runMode !== 'to_end' ? ' selected' : '') + '>停在指定阶段</option>'
      + '<option value="to_end"' + (runMode === 'to_end' ? ' selected' : '') + '>跑到粗剪合成为止</option>'
      + '</select></label>'
      + '<label class="slirn-pipe-flow-stop-label">完成到哪个阶段停：'
      + '<select class="slirn-pipe-flow-stop" id="slirn-sv-flow-stop">' + renderStopOptions(stopAfter) + '</select>'
      + '</label>'
      + '<label class="slirn-pipe-flow-stop-label">从哪一阶段开始跑：'
      + '<select class="slirn-pipe-flow-stop" id="slirn-sv-flow-since">' + renderStopOptions(since, '') + '</select>'
      + '</label>'
      + '<button type="button" class="slirn-btn slirn-btn-danger slirn-btn-sm" data-action="sv-flow-stop">⏹ 停止</button>'
      + '<button type="button" class="slirn-btn slirn-btn-sm" data-action="sv-flow-save">💾 保存配置</button>'
      + '<button type="button" class="slirn-btn slirn-btn-primary slirn-btn-sm" data-action="sv-flow-run">▶ 执行</button>'
      + '</span></summary>'
      + '<div class="slirn-pipe-sections">'
      + '<div class="slirn-pipe-hint">勾选要自动执行的阶段，确认「生成数量 / 时间上下限 / 提示词」后点「▶ 执行」，'
      + '系统会按顺序自动跑到所选阶段为止（不会自动进入「精剪混编」）。</div>'
      + sections
      + '</div></details>'
      + renderHistory(history);
  }

  // ---------- 读表单 → config ----------
  function _val(id, fallback) {
    var el = document.getElementById(id);
    if (!el) return fallback;
    var v = el.value;
    return (v === '' || v == null) ? fallback : v;
  }
  function _checked(id, fallback) {
    var el = document.getElementById(id);
    return el ? !!el.checked : fallback;
  }
  function _num(id, fallback) {
    var v = parseFloat(_val(id, fallback));
    return isNaN(v) ? fallback : v;
  }

  function readCurrentConfig() {
    var c = _ctx();
    if (!c.taskId || !c.projectId) return null;
    return {
      stage2_extract: {
        enabled: _checked(fieldId('stage2_extract', 'enabled'), true),
        model: _val(fieldId('stage2_extract', 'model'), 'paraformer'),
        lang: _val(fieldId('stage2_extract', 'lang'), 'zh'),
        hotwords: _val(fieldId('stage2_extract', 'hotwords'), ''),
        sd_switch: 'no'
      },
      stage3_analyze: {
        enabled: _checked(fieldId('stage3_analyze', 'enabled'), true),
        template: _val(fieldId('stage3_analyze', 'template'), 'hook_first'),
        n_clips: _num(fieldId('stage3_analyze', 'n_clips'), 4),
        duration_min_sec: _num(fieldId('stage3_analyze', 'duration_min_sec'), 60),
        duration_max_sec: _num(fieldId('stage3_analyze', 'duration_max_sec'), 90),
        use_image: _checked(fieldId('stage3_analyze', 'use_image'), true),
        prompt: _val(fieldId('stage3_analyze', 'prompt'), '')
      },
      stage4_optimize: {
        enabled: _checked(fieldId('stage4_optimize', 'enabled'), true)
      },
      stage5_coarse: {
        enabled: _checked(fieldId('stage5_coarse', 'enabled'), true),
        start_ost: _num(fieldId('stage5_coarse', 'start_ost'), 0),
        end_ost: _num(fieldId('stage5_coarse', 'end_ost'), 100)
      },
      run_mode: _val('slirn-sv-flow-run-mode', 'stop_after'),
      stop_after: _val('slirn-sv-flow-stop', 'stage5_coarse'),
      since: _val('slirn-sv-flow-since', 'stage2_extract')
    };
  }

  // ---------- 状态条 ----------
  function renderStatus(job) {
    var box = document.getElementById('slirn-sv-flow-status');
    if (!box) return;
    if (!job) { box.hidden = true; box.innerHTML = ''; return; }
    box.hidden = false;
    var state = job.state || 'idle';
    var stateLabel = {running: '⏳ 运行中', done: '✅ 已完成', error: '❌ 出错',
                      stopped: '⏸ 已停止', idle: '待执行'}[state] || state;
    var pct = Math.round((job.percent || 0));
    var logs = (job.logs || []).slice(-3).map(function (l) {
      var icon = l.level === 'error' ? '❌' : l.level === 'warn' ? '⚠️' : l.level === 'ok' ? '✅' : '•';
      return '<div class="slirn-pipe-status-log">' + icon + ' ' + esc(l.msg) + '</div>';
    }).join('');
    box.innerHTML = '<div class="slirn-pipe-status-head">'
      + '<span class="slirn-pipe-status-state slirn-pipe-status-state-' + esc(state) + '">' + esc(stateLabel) + '</span>'
      + '<span class="slirn-pipe-status-stage">当前阶段：' + esc(job.current_stage_label || '—') + '</span>'
      + '<progress class="slirn-pipe-status-bar" max="100" value="' + pct + '"></progress>'
      + '</div>'
      + (logs ? '<div class="slirn-pipe-status-log">' + logs + '</div>' : '')
      + (job.error ? '<div class="slirn-pipe-status-err">' + esc(job.error) + '</div>' : '');

    // 阶段徽章
    STAGES.forEach(function (s) {
      var el = document.querySelector('[data-sv-flow-state="' + s.key + '"]');
      if (!el) return;
      var done = (job.stages_done || []).indexOf(s.key) >= 0;
      var skip = (job.stages_skipped || []).indexOf(s.key) >= 0;
      if (job.current_stage === s.key && state === 'running') {
        el.textContent = '⏳ 运行中 ' + pct + '%';
      } else if (done) {
        el.textContent = '✅ 已完成';
      } else if (skip) {
        el.textContent = '⚠️ 已跳过';
      } else if (state === 'error') {
        el.textContent = '❌ 出错';
      } else {
        el.textContent = '⏸ 待执行';
      }
    });
  }

  function pollStatus() {
    var c = _ctx();
    postJSON(API + '/short_video_flow_status',
              {task_id: c.taskId, project_id: c.projectId}).then(function (r) {
      if (!r || !r.ok) return;
      renderStatus(r.job);
      if (r.running) {
        pollTimer = setTimeout(pollStatus, 1500);
      } else {
        pollTimer = null;
      }
    });
  }

  function stopPolling() {
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
  }

  // ---------- 动作 ----------
  function saveConfig(silent) {
    var c = _ctx();
    var cfg = readCurrentConfig();
    if (!cfg) { toast('未找到短视频项目上下文', 'error'); return null; }
    return postJSON(API + '/short_video_flow_save',
                    {task_id: c.taskId, project_id: c.projectId, config: cfg})
      .then(function (r) {
        if (!r || !r.ok) {
          if (!silent) toast('保存失败：' + (r && r.error || '未知错误'), 'error');
          return null;
        }
        if (!silent) toast('⚙️ 流程配置已保存');
        return r.config;
      });
  }

  function runFlow() {
    var c = _ctx();
    // 执行前二次确认（生成数量 / 时长 / 提示词 是需人工确认项）
    var cfg = readCurrentConfig();
    if (!cfg) { toast('未找到短视频项目上下文', 'error'); return; }
    var n = (cfg.stage3_analyze || {}).n_clips;
    var dmin = (cfg.stage3_analyze || {}).duration_min_sec;
    var dmax = (cfg.stage3_analyze || {}).duration_max_sec;
    var tplLabel = '（未选模板）';
    for (var i = 0; i < TIMPLATES.length; i++) {
      if (TIMPLATES[i].v === (cfg.stage3_analyze || {}).template) tplLabel = TIMPLATES[i].t;
    }
    var customPrompt = ((cfg.stage3_analyze || {}).prompt || '').trim();
    var msg = '即将自动执行：\n'
            + '· 生成数量：' + n + ' 条短视频\n'
            + '· 每条时长：' + dmin + '–' + dmax + ' 秒\n'
            + '· 提示词块：' + tplLabel + (customPrompt ? '（已自定义提示词，将覆盖模板）' : '（系统提示词原文）') + '\n'
            + '· 起始阶段：' + (STAGE_LABELS[cfg.since] || cfg.since) + '\n'
            + '· 终点阶段：' + (STAGE_LABELS[cfg.stop_after] || '跑到最后') + '\n\n确认执行？';
    if (!window.confirm(msg)) return;
    saveConfig(true).then(function (saved) {
      if (!saved) { toast('保存失败，无法启动', 'error'); return; }
      postJSON(API + '/short_video_flow_run',
               {task_id: c.taskId, project_id: c.projectId}).then(function (r) {
        if (!r || !r.ok) { toast('启动失败：' + (r && r.error || '未知错误'), 'error'); return; }
        if (!r.started) { toast(r.toast || '⏳ 流程已在运行中'); }
        else { toast(r.toast || '▶ 已启动自动执行'); }
        renderStatus(r.job);
        stopPolling();
        pollTimer = setTimeout(pollStatus, 800);
      });
    });
  }

  function stopFlow() {
    var c = _ctx();
    postJSON(API + '/short_video_flow_stop',
              {task_id: c.taskId, project_id: c.projectId}).then(function (r) {
      toast((r && r.toast) || '已请求停止', (r && r.ok) ? 'success' : 'error');
    });
  }

  function renderHistory(history) {
    if (!history || !history.length) return '';
    var rows = history.slice(0, 10).map(function (h) {
      var st = h.status || '';
      var icon = st === 'done' ? '✅' : st === 'error' ? '❌' : '⏸';
      var done = (h.stages_done || []).map(function (k) { return STAGE_LABELS[k] || k; }).join('、');
      var skip = (h.stages_skipped || []).length
        ? '（跳过 ' + h.stages_skipped.map(function (k) { return STAGE_LABELS[k] || k; }).join('、') + '）' : '';
      var t = h.finished_at ? new Date(h.finished_at * 1000).toLocaleString() : '';
      return '<div class="slirn-pipe-status-log">'
        + icon + ' ' + esc(t) + ' · ' + esc(done) + skip
        + (h.error ? ' · ' + esc(h.error) : '') + '</div>';
    }).join('');
    return '<details class="slirn-pipe-panel-details" open>'
      + '<summary class="slirn-pipe-head"><span class="slirn-pipe-title">🧾 执行历史（最近 10 次）</span></summary>'
      + '<div class="slirn-pipe-sections">' + rows + '</div></details>';
  }

  function loadPanel() {
    var c = _ctx();
    if (!c.taskId || !c.projectId) return;
    postJSON(API + '/short_video_flow_load',
              {task_id: c.taskId, project_id: c.projectId}).then(function (r) {
      if (!r || !r.ok) return;
      renderPanel(r.config || {}, r.history || []);
    });
  }

  // ---------- 事件绑定 ----------
  document.addEventListener('click', function (ev) {
    var t = ev.target.closest('[data-action]');
    if (!t) return;
    var action = t.getAttribute('data-action');
    if (action === 'sv-flow-run') { ev.preventDefault(); runFlow(); }
    else if (action === 'sv-flow-stop') { ev.preventDefault(); stopFlow(); }
    else if (action === 'sv-flow-save') { ev.preventDefault(); saveConfig(false); }
  });

  // ---------- 对外接口 ----------
  function mount() {
    var panel = document.getElementById('slirn-sv-flow-panel');
    if (!panel) return false;
    var c = _ctx();
    if (!c.taskId || !c.projectId) return false;
    if (panel.innerHTML.trim()) return true;   // 已渲染 → 不重复加载
    loadPanel();
    stopPolling();
    pollStatus();
    return true;
  }
  window.slirnShortVideoFlowMount = mount;

  // 兜底：short_video_flow.js 由 Gradio 动态注入（defer 不生效、执行顺序不保证），
  // 若它在 openProject 之后才执行，上面那次挂载会被跳过 —— 这里监听 DOM，
  // 面板出现且为空时自动补挂（镜像 pipeline.js 的 MutationObserver 做法）。
  function tryAutoMount() {
    var panel = document.getElementById('slirn-sv-flow-panel');
    if (!panel) return;
    if (panel.innerHTML.trim()) return;
    mount();
  }
  var observer = new MutationObserver(function () { tryAutoMount(); });
  function startObserver() {
    if (document.body) {
      observer.observe(document.body, {childList: true, subtree: true});
      tryAutoMount();
    }
  }
  if (document.body) startObserver();
  else document.addEventListener('DOMContentLoaded', startObserver);

  window.slirnShortVideoFlowUnmount = function () { stopPolling(); };
})();
