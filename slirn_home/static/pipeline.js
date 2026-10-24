// Slirn 流程配置 + 自动执行 — REQ-20260918-047（v4：全局停止阶段下拉 + 每阶段从本阶段起跑）
//
// 本批次更新（REQ-20260921-NNN — schema 同步 + UI 简化 + range_enabled UI）：
// - TEMPLATES.default_tpl / 半自动 / 全自动 3 套模板去掉 fine_cut.cover_image/
//   bg_image/bgm 死字段（v2 用户反馈：handler 不读这几个字段，实际素材路径
//   在 fc.json 的 materials.{cover,bg,audio}.path，由工作台第 6 阶段详情页
//   上传写入）。流程配置面板 fine_cut 区段只保留「跑不跑 / 跑哪段 / 用哪份
//   参数」3 类决策。
// - fine_cut UI 改 HH:MM:SS 输入（pattern 校验），内部存秒（preview_start/
//   duration）；新增「按区间导出」checkbox = range_enabled 门控 —— 不勾 =
//   全片（保留默认意图），勾上 = 用下面两个时间导；range_enabled=False
//   时输入框显示但 disabled（让用户看到「会导 10 分钟」默认但不会被误触发）。
// - renderStageForm 加精剪素材维护位置说明（明确告诉用户素材在工作台
//   第 6 阶段页维护，不要在这里上传；video/subtitle 由上游产物自动获取）。
// - _pipePanelPopulateDeps 去掉 bgm 下拉填充逻辑（v2：cfg.fine_cut 已无 bgm
//   字段，UI 也无 data-bgm-select 元素，端点保留由工作台侧引用）。
//
// 设计要点：
// - 整个面板用 <details> 包裹，summary 是头部（含标题 + 模板 + 全局停止阶段下拉 + 主操作 + 折叠箭头）
// - 5 个阶段每个内部又是一个 <details open>，可独立折叠
// - 顶部「完成到哪个阶段停」下拉：6 个选项（5 阶段后停 + 不停跑到底），替换 v3 的 per-stage checkbox
// - 每阶段 summary 右侧加「▶ 从本阶段开始往下执行」按钮，调 /pipeline_run?since=<stage_key>
// - 模板切换：3 套内置模板对应不同顶层 stop_after
// - checkbox click 委托用 closest('label') 早退，确保不被外层 click 抢
//
// 与 router.js 互不依赖：复用其 postJSON / toast / refreshDetail 等 window.* 全局即可。
(function() {
  if (window.__slirnPipelineBound) return;
  window.__slirnPipelineBound = true;

  var SLIRN_API = '/slirn/api';

  // ---- stage label/cfg schema ----
  // REQ-20260921-NNN：6 阶段（fine_cut = 精剪合成 / 最终导出视频；assets 不进 pipeline）。
  var STAGES = [
    {key: 'subtitle_generation', label: '字幕生成', desc: 'FunASR 识别视频字幕；勾选「区分说话人」可识别人员编号'},
    {key: 'subtitle_review', label: '字幕修订', desc: '大模型分析字幕；可设「自动关联人员ID」「默认接受所有建议」'},
    {key: 'rough_cut', label: '切分修剪', desc: '按修订决策带入保留/更正段；可设「自动关联人员ID」「删除某些说话人」'},
    {key: 'rough_compose', label: '粗剪合成', desc: '按切分保留区间用上游 VideoClipper 合成粗剪成片（必做）'},
    {key: 'optimize', label: '优化字幕', desc: '重新识别粗剪成片字幕，提取不明确字词；可设「全部接受替换」'},
    {key: 'fine_cut', label: '精剪合成', desc: '最终导出视频：封面图/背景图/背景音乐（可选）+ 设置参数模板 + 全篇或区间'}
  ];
  var STAGE_KEYS = STAGES.map(function(s) { return s.key; });
  var STAGE_LABELS = {};
  STAGES.forEach(function(s) { STAGE_LABELS[s.key] = s.label; });

  // ---- 3 套内置模板（v5：顶层 run_mode + stop_after；null = 跑到底）----
  // REQ-20260918-049：subtitle_review 加 rigor 字段
  // REQ-20260921-NNN：加 run_mode + fine_cut（默认 enabled=false）+ link_person_ids
  // 2026-09-30 用户要求复选框默认全选：speaker_diarization 三套模板统一 true
  //   （旧 false 与新默认矛盾，且 semi/full 的 link_person_ids:true 依赖 sd 开启
  //   才有说话人字段可关联）。模板间的差异保留在 accept_all / link_person_ids。
  var TEMPLATES = {
    // 人工全审：字幕修订后停（让用户审 LLM 建议；默认严谨性 = medium）
    default_tpl: {
      label: '人工全审（字幕修订后停）',
      config: {
        subtitle_generation: {speaker_diarization: true},
        subtitle_review: {accept_all_suggestions: false, skip_categories: [], rigor: 'medium', link_person_ids: false},
        rough_cut: {delete_speakers: [], default_decision: 'keep', link_person_ids: false},
        rough_compose: {},
        optimize: {accept_all_replacements: false},
        fine_cut: {enabled: false, params_source: 'current', preview_start: 0.0, duration: null},
        run_mode: 'stop_after',
        stop_after: 'subtitle_review'
      }
    },
    // 半自动：粗剪合成后停（让人看完粗剪再决定后续；严谨性 = medium）
    semi: {
      label: '半自动（粗剪合成后停）',
      config: {
        subtitle_generation: {speaker_diarization: true},
        subtitle_review: {accept_all_suggestions: true, skip_categories: [], rigor: 'medium', link_person_ids: true},
        rough_cut: {delete_speakers: [], default_decision: 'keep', link_person_ids: true},
        rough_compose: {},
        optimize: {accept_all_replacements: false},
        fine_cut: {enabled: false, params_source: 'current', preview_start: 0.0, duration: null},
        run_mode: 'stop_after',
        stop_after: 'rough_compose'
      }
    },
    // 全自动：跑到底（严谨性 = medium — 全自动模式建议用户手工调高）
    full: {
      label: '全自动（跑到底）',
      config: {
        subtitle_generation: {speaker_diarization: true},
        subtitle_review: {accept_all_suggestions: true, skip_categories: [], rigor: 'medium', link_person_ids: true},
        rough_cut: {delete_speakers: [], default_decision: 'keep', link_person_ids: true},
        rough_compose: {},
        optimize: {accept_all_replacements: true},
        fine_cut: {enabled: false, params_source: 'current', preview_start: 0.0, duration: null},
        run_mode: 'to_end',
        stop_after: null
      }
    }
  };

  // ---- 工具 ----
  function postJSON(url, payload) {
    return fetch(url, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload || {})
    }).then(function(r) { return r.json(); }).catch(function(e) { return {ok: false, error: String(e)}; });
  }
  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&').replace(/</g, '<').replace(/>/g, '>')
      .replace(/"/g, '"').replace(/'/g, '&#39;');
  }
  function toast(msg, type) {
    if (window.slirnToast) window.slirnToast(msg, type || 'success');
  }
  function curTaskId() {
    var panel = document.getElementById('slirn-pipe-panel');
    return panel ? (panel.getAttribute('data-task-id') || '') : '';
  }

  // ---- 阶段字段 ID 命名 ----
  function fieldId(stageKey, fieldName) { return 'slirn-pipe-' + stageKey + '-' + fieldName; }

  // ---- 渲染「全局停止阶段」下拉 option 列表 ----
  function renderFlowStopOptions(selectedValue) {
    var opts = '<option value="">不停（跑到底）</option>';
    STAGES.forEach(function(s) {
      var sel = (selectedValue === s.key) ? ' selected' : '';
      opts += '<option value="' + s.key + '"' + sel + '>'
            + escapeHtml(s.label) + '后停</option>';
    });
    return opts;
  }

  // ---- 单个 stage 的表单（v5：加 link_person_ids / fine_cut 配置）----
  // REQ-20260921-NNN：fine_cut 是新增的第 6 阶段
  function renderStageForm(stage, stageCfg) {
    if (stage.key === 'subtitle_generation') {
      var sdOn = stageCfg.speaker_diarization ? ' checked' : '';
      return '<div class="slirn-pipe-form">'
        + '<div class="slirn-pipe-desc">' + escapeHtml(stage.desc) + '</div>'
        + '<label class="slirn-pipe-field">'
        + '<input type="checkbox" id="' + fieldId(stage.key, 'sd') + '"' + sdOn + '> 区分说话人（多人视频需要；单人视频可关以减少误分）'
        + '</label>'
        + '</div>';
    }
    if (stage.key === 'subtitle_review') {
      var aa = stageCfg.accept_all_suggestions ? ' checked' : '';
      var skip = (stageCfg.skip_categories || []).join(',');
      var lp = stageCfg.link_person_ids ? ' checked' : '';
      // REQ-20260918-049：4 卡 rigor picker（与工作台 _render_rigor_picker 同源；
      // pipe 通道对 custom 档降级为 medium，desc 标注清楚避免误导）
      var rigorVal = stageCfg.rigor || 'medium';
      var rigorCards = [
        {v: 'high',   t: '高 · 严格打磨', d: '逐句精修到成品口播稿水平：语气词、口癖、重复、无意义寒暄全部处理',
         e: '「嗯呃，那个，就是说，我们今天讲一下神经网络」→ 「我们今天讲一下神经网络」'},
        {v: 'medium', t: '中 · 意思正确即可', d: '只处理影响理解的问题：明显口误、连续重复、明显不通顺的句子做小幅修剪；小语气词可保留',
         e: '「嗯，我们今天讲一下神经网络」→ 整行保留（小语气词「嗯」不影响意思）'},
        {v: 'low',    t: '低 · 只去严重问题', d: '最大限度保留原文：只处理大段纯重复和意思混乱的句子，语气词、轻微口误都保留',
         e: '「所以我们所以我们所以我们看到」→ 「所以我们看到」（只去掉连续重复）'},
        {v: 'custom', t: '自 · 自定义', d: 'pipe 通道暂不支持（需在工作台「字幕修订」有独立 textarea 才能填提示词）；选此项时服务端自动降级为 medium',
         e: '可在工作台自定义严谨性级别后再回 pipe-panel 选择 high/medium/low'}
      ];
      var cardsHtml = '<div class="slirn-pipe-subhead">分析严谨性级别（决定大模型挑毛病的严格程度）：</div>'
        + '<div class="slirn-rigor-cards slirn-pipe-rigor">';
      rigorCards.forEach(function(c) {
        var checked = (rigorVal === c.v) ? ' checked' : '';
        cardsHtml += '<label class="slirn-rigor-card" title="' + escapeHtml(c.d) + '">'
          + '<input type="radio" name="slirn-rev-rigor" value="' + c.v + '"' + checked + ' />'
          + '<span class="slirn-rigor-card-title">' + escapeHtml(c.t) + '</span>'
          + '<span class="slirn-rigor-card-desc">' + escapeHtml(c.d) + '</span>'
          + '<span class="slirn-rigor-card-example">例：' + escapeHtml(c.e) + '</span>'
          + '</label>';
      });
      cardsHtml += '</div>';
      return '<div class="slirn-pipe-form">'
        + '<div class="slirn-pipe-desc">' + escapeHtml(stage.desc) + '</div>'
        + cardsHtml
        + '<label class="slirn-pipe-field">'
        + '<input type="checkbox" id="' + fieldId(stage.key, 'accept-all') + '"' + aa + '> 默认接受所有建议（跑完后自动 save_revision 全量 accept）'
        + '</label>'
        // REQ-20260921-NNN：自动关联人员ID（调 /rev_speaker_link；不依赖 save_revision 全接受）
        + '<label class="slirn-pipe-field">'
        + '<input type="checkbox" id="' + fieldId(stage.key, 'link-person') + '"' + lp + '> 自动关联人员ID（调 /rev_speaker_link — 字幕生成时需开启「区分说话人」）'
        + '</label>'
        + '<label class="slirn-pipe-field"><span>跳过建议类别（逗号分隔，空 = 全接受）：</span>'
        + '<input type="text" id="' + fieldId(stage.key, 'skip-cats') + '" value="' + escapeHtml(skip) + '" placeholder="delete,review">'
        + '</label>'
        + '</div>';
    }
    if (stage.key === 'rough_cut') {
      var delSpk = (stageCfg.delete_speakers || []).join(',');
      var defDec = stageCfg.default_decision || 'keep';
      var lp2 = stageCfg.link_person_ids ? ' checked' : '';
      return '<div class="slirn-pipe-form">'
        + '<div class="slirn-pipe-desc">' + escapeHtml(stage.desc) + '</div>'
        + '<label class="slirn-pipe-field"><span>删除某些说话人全部记录（逗号分隔的人员 ID）：</span>'
        + '<input type="text" id="' + fieldId(stage.key, 'del-spk') + '" value="' + escapeHtml(delSpk) + '" placeholder="2,5">'
        + '</label>'
        + '<label class="slirn-pipe-field"><span>默认决策（修订阶段遗留未决策时）：</span>'
        + '<select id="' + fieldId(stage.key, 'def-dec') + '">'
        + '<option value="keep"' + (defDec === 'keep' ? ' selected' : '') + '>保留</option>'
        + '<option value="delete"' + (defDec === 'delete' ? ' selected' : '') + '>删除</option>'
        + '</select></label>'
        // REQ-20260921-NNN：自动关联人员ID（仅在 delete_speakers 为空时单独生效）
        + '<label class="slirn-pipe-field">'
        + '<input type="checkbox" id="' + fieldId(stage.key, 'link-person') + '"' + lp2 + '> 自动关联人员ID（仅当上面「删除说话人」为空时生效 — 调 /cut_speaker_link）'
        + '</label>'
        + '</div>';
    }
    if (stage.key === 'rough_compose') {
      return '<div class="slirn-pipe-form">'
        + '<div class="slirn-pipe-desc">' + escapeHtml(stage.desc) + '</div>'
        + '<div class="slirn-pipe-hint">粗剪合成无需额外选项；按切分保留区间拼接。必做阶段（REQ-20260918-045）。</div>'
        + '</div>';
    }
    if (stage.key === 'optimize') {
      var aaR = stageCfg.accept_all_replacements ? ' checked' : '';
      return '<div class="slirn-pipe-form">'
        + '<div class="slirn-pipe-desc">' + escapeHtml(stage.desc) + '</div>'
        + '<label class="slirn-pipe-field">'
        + '<input type="checkbox" id="' + fieldId(stage.key, 'accept-rep') + '"' + aaR + '> 默认接受所有替换（跑完后自动 save_optimize_subtitle 全量 applied）'
        + '</label>'
        + '</div>';
    }
    // REQ-20260921-NNN：精剪合成（最终导出视频）— 6 阶段最后一站
    // v2 用户反馈：去掉 cover_image/bg_image/bgm 输入框（都是死字段 —— handler 不读；
    // 实际素材路径在 fc.json 的 materials.{cover,bg,audio}.path，由工作台第 6 阶段详情页
    // 上传写入）。流程配置面板只保留「跑不跑」+「跑哪段」+「用哪份参数」3 类决策。
    if (stage.key === 'fine_cut') {
      var fc0 = stageCfg || {};
      // REQ-20260921-NNN-radio-mode：精剪合成导出方式改单选卡片组（二选一）。
      //   - 「出整个片」(full) → enabled=true, range_enabled=false
      //   - 「按区间导出一段」(range) → enabled=true, range_enabled=true（默认）
      // 设计要求：要么导全片，要么导一段看效果。要彻底跳过 fine_cut 走
      // 顶层 stop_after="optimize" 配置，不在本 radio 表达。
      // 预选规则（覆盖 stageCfg）：
      //   - enabled && !range_enabled → full
      //   - 其他（默认 / legacy 脏状态 / disabled-and-range-true）→ range
      var exportMode = 'range';
      if (fc0.enabled === true && fc0.range_enabled === false) {
        exportMode = 'full';
      }
      var paramsSrc = fc0.params_source || 'current';
      // REQ-20260921-NNN-v4：UI 改 HH:MM:SS，内部仍是秒。
      // 默认 start=00:00:00 / duration=00:10:00；range 模式下输入框 enable，
      // full 模式下 disabled（全片导没必要配区间）。
      var startStr = secondsToHms(fc0.preview_start != null ? Number(fc0.preview_start) : 0);
      var durStr   = secondsToHms(fc0.duration != null ? Number(fc0.duration) : 600);
      var rangeDisabled = exportMode === 'range' ? '' : ' disabled';
      return '<div class="slirn-pipe-form">'
        + '<div class="slirn-pipe-desc">' + escapeHtml(stage.desc) + '</div>'
        // REQ-20260921-NNN-radio-mode：导出方式单选卡片组（复用 .slirn-rigor-card 样式）
        + '<div class="slirn-pipe-subhead">导出方式（必选其一；选「按区间导出一段」= 默认 10 分钟可快速看效果）：</div>'
        + '<div class="slirn-rigor-cards slirn-pipe-export-mode-cards">'
        + '<label class="slirn-rigor-card slirn-pipe-export-mode-card" title="导出整个粗剪成片，1-3 小时重编码（按视频长度）">'
        + '<input type="radio" name="slirn-pipe-fc-export-mode" value="full"' + (exportMode === 'full' ? ' checked' : '') + '>'
        + '<span class="slirn-rigor-card-title">🎬 出整个片</span>'
        + '<span class="slirn-rigor-card-desc">导出全片（1-3 小时重编码）</span>'
        + '<span class="slirn-rigor-card-example">适合最终成品交付；耗时较长。</span>'
        + '</label>'
        + '<label class="slirn-rigor-card slirn-pipe-export-mode-card" title="导出起止时间内的片段，1-10 分钟重编码（按区间长度）">'
        + '<input type="radio" name="slirn-pipe-fc-export-mode" value="range"' + (exportMode === 'range' ? ' checked' : '') + '>'
        + '<span class="slirn-rigor-card-title">✂️ 按区间导出一段</span>'
        + '<span class="slirn-rigor-card-desc">按下面 HH:MM:SS 起点 + 时长导出（默认 10 分钟）</span>'
        + '<span class="slirn-rigor-card-example">适合快速看效果、交付片段。</span>'
        + '</label>'
        + '</div>'
        // REQ-20260921-NNN：素材说明 —— 明确告诉用户素材在哪维护，哪些自动获取
        + '<div class="slirn-pipe-hint slirn-pipe-fine-cut-materials-note">'
        + '<b>📦 素材维护位置：</b>精剪合成的所有素材（封面 / 背景 / 背景音乐 / 视频 / 字幕）都在 '
        + '<b>本任务的「第 6 阶段 · 精剪合成」详情页</b>里维护，不要在这里上传。'
        + '<br>· <b>粗剪视频</b> + <b>字幕文件</b>：自动从上游产物获取，无需上传。'
        + '<br>· <b>封面图</b> / <b>背景图</b>：仅在精剪合成页勾选启用时使用，可选。'
        + '<br>· <b>背景音乐</b>：可选项，可不传。'
        + '</div>'
        // 设置参数模板（下拉，loadPanel 时填充）
        // REQ-20260921-NNN：参数模板选择 — 若本任务已有 fc.json（精剪参数），
        // 可以用「当前参数」；若没有，必须选模板或在精剪合成页「导入参数」。
        // 具体的 disable 逻辑在 loadPanel 末尾根据 fc.json 是否存在动态处理。
        + '<label class="slirn-pipe-field"><span>设置参数（=精剪面板保存的全局模板）：</span>'
        + '<select id="' + fieldId(stage.key, 'params-src') + '" data-params-select>'
        + '<option value="current"' + (paramsSrc === 'current' ? ' selected' : '') + '>当前参数</option>'
        + '</select></label>'
        + '<div class="slirn-pipe-hint slirn-pipe-fine-cut-params-note" data-fine-cut-params-note>'
        + '>若本任务已保存过精剪参数（<code>fine_compose.json</code>），可选「当前参数」；'
        + '否则必须选模板，或去精剪合成详情页「导入参数」。</div>'
        // REQ-20260921-NNN-v4：导出区间（HH:MM:SS）。range 模式时 enable，full 时 disabled。
        + '<label class="slirn-pipe-field"><span>导出起点（HH:MM:SS，0=全篇）：</span>'
        + '<input type="text" id="' + fieldId(stage.key, 'start') + '" data-range-input'
        + ' value="' + escapeHtml(startStr) + '" placeholder="00:00:00"'
        + ' pattern="^\\d{1,}:[0-5]\\d:[0-5]\\d$"' + rangeDisabled + '>'
        + '</label>'
        + '<label class="slirn-pipe-field"><span>导出时长（HH:MM:SS，默认 10 分钟）：</span>'
        + '<input type="text" id="' + fieldId(stage.key, 'dur') + '" data-range-input'
        + ' value="' + escapeHtml(durStr) + '" placeholder="00:10:00"'
        + ' pattern="^\\d{1,}:[0-5]\\d:[0-5]\\d$"' + rangeDisabled + '>'
        + '</label>'
        + '<div class="slirn-pipe-hint">⚠ 启用「自动最终导出」+「按区间导出」后，会从起点开始按指定时长导出。视频全长在任务详情里看。</div>'
        + '</div>';
    }
    return '<div class="slirn-pipe-form"></div>';
  }

  // ---- 整个面板渲染（v4：details 包裹整个面板 + 头部下拉 + 每阶段 run-since 按钮）----
  // REQ-20260918-049：data.history 透传进来，按钮文案自适应（智能续跑 / 从头跑）
  function renderPanel(taskId, data) {
    var panel = document.getElementById('slirn-pipe-panel');
    if (!panel) return;
    panel.setAttribute('data-task-id', taskId);
    var cfg = (data && data.config) || {};
    var updatedAt = (data && data.updated_at) || '';
    var history = (data && data.history) || [];

    // v4 兼容：v3 per-stage boolean / v2 per-stage string 字段丢弃；顶层 stop_after 提升
    cfg = normalizeCfg(cfg);
    var flowStopValue = cfg.stop_after || '';

    // REQ-20260918-049：智能续跑按钮（自适应文案）
    var sinceKey = computeNextSince(history);
    var runBtnHtml = sinceKey
      ? '<button type="button" class="slirn-btn slirn-btn-primary slirn-btn-sm slirn-pipe-run"'
        + ' data-action="pipe-run" data-since="' + sinceKey + '"'
        + ' title="上次跑到了「' + escapeHtml(STAGE_LABELS[sinceKey] || sinceKey) + '」之前；点此从该处开始（跳过已完成阶段）">'
        + '▶ 续跑 (从「' + escapeHtml(STAGE_LABELS[sinceKey] || sinceKey) + '」开始)</button>'
      : '<button type="button" class="slirn-btn slirn-btn-primary slirn-btn-sm slirn-pipe-run"'
        + ' data-action="pipe-run"'
        + ' title="保存并按当前配置顺序执行所有阶段">▶ 从头跑</button>';

    // REQ-20260921-NNN：清理所有阶段产物按钮 — 加在「字幕生成」section 上方
    // 两次确认（按钮旁 warning 文案 + onClick 内 confirm 弹窗）。
    var resetBlockHtml =
      '<div class="slirn-pipe-reset-block">'
      + '<button type="button" class="slirn-btn slirn-btn-danger slirn-btn-sm slirn-pipe-reset"'
      + ' data-action="pipe-reset-stages"'
      + ' title="删除本任务所有阶段的生成产物（subtitle.json / revision.json / cutlist.json / rough_compose.mp4 / optimize_subtitle.json / fine_compose.json / fine_export*.mp4 等），并清空内存中的 stages_done 与 execution_history。原视频 / 时间截取 / 热词 / 任务 metadata 保留。">'
      + '🧹 清理所有阶段产物</button>'
      + '<div class="slirn-pipe-reset-warning">'
      + '⚠ 清理后：字幕 / 修订 / 切分 / 粗剪 / 优化 / 精剪合成 6 个阶段的所有产物都会被删除，'
      + '内存中的「已完成阶段」标记也会清空。下次跑流程会从头开始。'
      + '</div>'
      + '</div>';

    var sectionsHtml = resetBlockHtml + STAGES.map(function(stage, idx) {
      return '<details class="slirn-pipe-section" data-pipe-section="' + stage.key + '" open>'
        + '<summary class="slirn-pipe-section-summary">'
        + '<span class="slirn-pipe-section-num">' + (idx + 1) + '</span>'
        + '<span class="slirn-pipe-section-label">' + escapeHtml(stage.label) + '</span>'
        + '<span class="slirn-pipe-section-hint">' + escapeHtml(stage.desc) + '</span>'
        + '<button type="button" class="slirn-btn slirn-btn-primary slirn-btn-sm slirn-pipe-stage-run"'
        + ' data-action="pipe-run-since" data-since="' + stage.key + '"'
        + ' title="保存并从本阶段开始往后执行（跳过之前所有阶段）">▶ 从本阶段开始</button>'
        + '</summary>'
        + '<div class="slirn-pipe-section-body">' + renderStageForm(stage, cfg[stage.key] || {}) + '</div>'
        + '</details>';
    }).join('');

    panel.innerHTML =
      '<details class="slirn-pipe-panel-details">'
      + '<summary class="slirn-pipe-head">'
      + '<span class="slirn-pipe-title">⚙ 流程配置</span>'
      + '<span class="slirn-pipe-updated">'
      + (updatedAt ? '最近保存：' + escapeHtml(updatedAt) : '尚未保存')
      + '</span>'
      + '<span class="slirn-pipe-head-acts">'
      // REQ-20260921-NNN：run_mode 顶层下拉（to_end / stop_after）
      // - to_end 模式：flow-stop 下拉禁用（强制跑完所有阶段）
      // - stop_after 模式：flow-stop 下拉启用（用户选择停在哪）
      + '<label class="slirn-pipe-run-mode-label">'
      + '运行模式：'
      + '<select class="slirn-pipe-run-mode" id="slirn-pipe-run-mode" data-pipe-action="run-mode">'
      + '<option value="stop_after"' + (cfg.run_mode !== 'to_end' ? ' selected' : '') + '>停在指定阶段</option>'
      + '<option value="to_end"' + (cfg.run_mode === 'to_end' ? ' selected' : '') + '>一键跑到底</option>'
      + '</select>'
      + '</label>'
      + '<label class="slirn-pipe-flow-stop-label">'
      + '完成到哪个阶段停：'
      + '<select class="slirn-pipe-flow-stop" id="slirn-pipe-flow-stop"' + (cfg.run_mode === 'to_end' ? ' disabled' : '') + '>'
      + renderFlowStopOptions(flowStopValue)
      + '</select>'
      + '</label>'
      + '<select class="slirn-pipe-template" id="slirn-pipe-template" data-pipe-action="template">'
      + '<option value="default_tpl">内置：人工全审</option>'
      + '<option value="semi">内置：半自动</option>'
      + '<option value="full">内置：全自动</option>'
      // REQ-20260930-092：用户模板（fp:<id>）由 _populateFlowTemplates 动态插入到
      // 「另存为模板」之前；整套面板参数（复选框选中/不选中、单选、输入、运行模式）
      // 可命名保存、跨任务复用
      + '<option value="fp:_save_as">💾 将当前配置存为模板…</option>'
      + '<option value="custom">自定义</option>'
      + '</select>'
      + '<button type="button" class="slirn-btn-mini slirn-pipe-tpl-del"'
      + ' data-action="pipe-flow-tpl-del" hidden'
      + ' title="删除当前选中的流程模板（不影响已应用的任务）">🗑</button>'
      + '<button type="button" class="slirn-btn slirn-btn-danger slirn-btn-sm" data-action="pipe-stop">⏹ 停止</button>'
      + '<button type="button" class="slirn-btn slirn-btn-sm" data-action="pipe-save">💾 保存配置</button>'
      + '<button type="button" class="slirn-btn slirn-btn-sm" data-action="pipe-status-show" hidden title="显示状态条（点状态条 ▾ 收起后点此恢复）">📊 状态</button>'
      + runBtnHtml
      + '</span>'
      + '</summary>'
      + '<div class="slirn-pipe-sections">' + sectionsHtml + '</div>'
      // REQ-20260921-NNN-outputs-browser：一站式查看本任务所有产物
      // （video / subtitle / audio / json），取代用户自己翻目录或记区间导出文件名
      // （fine_export_t*_d*.mp4）。默认收起，点开才拉。
      + '<details class="slirn-pipe-outputs-panel" data-pipe-outputs-panel open>'
      + '<summary class="slirn-pipe-outputs-summary">📦 产物浏览器（视频 / 字幕 / JSON / 音频）</summary>'
      + '<div class="slirn-pipe-outputs-body" data-pipe-outputs-body>'
      + '<div class="slirn-pipe-outputs-toolbar">'
      + '<button type="button" class="slirn-btn slirn-btn-sm slirn-pipe-outputs-refresh"'
      + ' data-action="pipe-outputs-refresh" title="调用 /slirn/api/list_outputs 重新列出本任务所有产物">'
      + '🔄 刷新产物列表</button>'
      // REQ-20260921-NNN-outputs-browser-v2：按类别筛选 chip 行（默认「全部」）。
      // chip 行由 _renderOutputsList 在拿到 items 后动态填充（按数据里出现的 kind 生成），
      // 这样空 chip 不会出现误导用户。
      + '<div class="slirn-pipe-outputs-filter" data-pipe-outputs-filter hidden></div>'
      + '</div>'
      + '<div class="slirn-pipe-outputs-list" data-pipe-outputs-list>'
      + '<div class="slirn-pipe-outputs-empty">点击「🔄 刷新产物列表」查看本任务 outputs/ 下的所有产物。</div>'
      + '</div>'
      + '</div>'
      + '</details>'
      + '</details>';
    // REQ-20260918-049 v2：渲染完后同步「📊 状态」按钮可见性（status hidden 时显示）
    refreshStatusToggleBtn();
    // REQ-20260930-092：渲染后拉用户流程模板填下拉 + 按当前 config 回显匹配
    // （覆盖 loadPanel / 套用模板 / 重置 三种重渲染路径 — 此前模板下拉从不回显）
    try { _populateFlowTemplates(); } catch (e) { /* 静默：模板拉取失败不挡主流程 */ }
  }

  // ============================================================
  // REQ-20260930-092：流程配置·用户模板（跨任务复用）+ 面板自动保存
  // 红框内全部参数（含每个复选框的选中/不选中）：
  //   1) 可「💾 存为模板」命名复用（tasks/_global_flow_profiles.json）
  //   2) 任何改动 800ms 防抖自动写入本任务 pipeline.json（无需点💾保存）
  // ============================================================
  var _FLOW_TPL_PROFILES = [];
  // 最近一次「套用」的模板 value（'fp:<id>' / 内置 key）。
  // 套用是确定性事实（不等深比较）；用户手改面板任何控件 → 置 null 回「自定义」。
  var _tplAppliedValue = null;

  // 深子集比较：sub 的每个键在 sup 中都存在且递归相等。
  // 内置模板 config 是部分字段（duration:null / 无 range_enabled），
  // readCurrentConfig() 重建的是完整 config → 用子集而非全等，
  // 用户模板（后端 validate_config 规范化的完整形态）子集匹配 == 全等。
  function _cfgContains(sup, sub) {
    if (sup === sub) return true;
    if (!sub || typeof sub !== 'object') return sup === sub;
    if (!sup || typeof sup !== 'object') return false;
    var keys = Object.keys(sub);
    for (var i = 0; i < keys.length; i++) {
      var k = keys[i];
      if (!(k in sup) || !_cfgContains(sup[k], sub[k])) return false;
    }
    return true;
  }

  // 当前面板应显示的模板 value：套用标记 > 用户模板(子集匹配) > 内置(子集匹配) > custom
  function _matchTemplateValue(cfg) {
    if (_tplAppliedValue) return _tplAppliedValue;
    for (var i = 0; i < _FLOW_TPL_PROFILES.length; i++) {
      if (_cfgContains(cfg, _FLOW_TPL_PROFILES[i].config)) return 'fp:' + _FLOW_TPL_PROFILES[i].id;
    }
    var tkeys = Object.keys(TEMPLATES);
    for (var j = 0; j < tkeys.length; j++) {
      if (_cfgContains(cfg, TEMPLATES[tkeys[j]].config)) return tkeys[j];
    }
    return 'custom';
  }

  // 删除按钮仅当选中用户模板时可见
  function _syncTplDelBtn() {
    var sel = document.getElementById('slirn-pipe-template');
    var btn = document.querySelector('#slirn-pipe-panel [data-action="pipe-flow-tpl-del"]');
    if (!btn) return;
    var v = sel ? String(sel.value || '') : '';
    btn.hidden = !(v.indexOf('fp:') === 0 && v !== 'fp:_save_as');
  }

  // 拉用户模板列表并重建下拉 options；selectValue 强制选中（如刚保存的新模板），
  // 缺省按 readCurrentConfig() 深比较回显
  function _populateFlowTemplates(selectValue) {
    var sel = document.getElementById('slirn-pipe-template');
    if (!sel) return Promise.resolve();
    return fetch(SLIRN_API + '/list_flow_profiles', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({})
    }).then(function(r) { return r.json(); }).then(function(j) {
      _FLOW_TPL_PROFILES = (j && j.ok && j.profiles) ? j.profiles : [];
      var html = ''
        + '<option value="default_tpl">内置：人工全审</option>'
        + '<option value="semi">内置：半自动</option>'
        + '<option value="full">内置：全自动</option>';
      _FLOW_TPL_PROFILES.forEach(function(p) {
        html += '<option value="fp:' + escapeHtml(p.id) + '">📋 ' + escapeHtml(p.name) + '</option>';
      });
      html += '<option value="fp:_save_as">💾 将当前配置存为模板…</option>'
        + '<option value="custom">自定义</option>';
      sel.innerHTML = html;
      var want = selectValue || _matchTemplateValue(readCurrentConfig());
      sel.value = want;
      if (sel.value !== want) sel.value = _matchTemplateValue(readCurrentConfig());
      _syncTplDelBtn();
    }).catch(function(e) { console.warn('[flow-tpl] 模板列表拉取失败', e); });
  }

  // ---- 自动保存：面板内任何参数改动即落盘本任务 pipeline.json ----
  var _autoSaveTimer = null;
  function _autoSaveSchedule() {
    if (_autoSaveTimer) clearTimeout(_autoSaveTimer);
    _autoSaveTimer = setTimeout(_autoSaveNow, 800);
  }
  function _autoSaveNow() {
    if (_autoSaveTimer) { clearTimeout(_autoSaveTimer); _autoSaveTimer = null; }
    var panel = document.getElementById('slirn-pipe-panel');
    var taskId = panel ? panel.getAttribute('data-task-id') : null;
    if (!taskId) return;
    var cfg = readCurrentConfig();
    if (!cfg) return;
    postJSON(SLIRN_API + '/pipeline_save', {task_id: taskId, config: cfg}).then(function(r) {
      if (r && r.ok) {
        var u = panel.querySelector('.slirn-pipe-updated');
        if (u && r.saved_at) u.textContent = '最近保存：' + r.saved_at + '（自动）';
        // 套用模板后的落盘 → 维持下拉选中该模板；手改路径已同步置「自定义」，
        // 这里不再重匹配（避免改动不影响模板字段的参数时把下拉翻回内置模板）
        if (_tplAppliedValue) {
          var sel = document.getElementById('slirn-pipe-template');
          if (sel && sel.value !== 'fp:_save_as') sel.value = _tplAppliedValue;
        }
        _syncTplDelBtn();
      } else {
        console.warn('[pipe-autosave] 保存失败', r && r.error);
      }
    }).catch(function(e) { console.warn('[pipe-autosave]', e); });
  }

  // ---- v4 兼容：把 v3 per-stage boolean / v2 per-stage string 字段丢弃；顶层 stop_after 保留 ----
  function normalizeCfg(cfg) {
    if (!cfg || typeof cfg !== 'object') return cfg;
    // 顶层 stop_after 已经就是 v4 权威字段，无需迁移
    // 阶段内旧字段（v3 boolean / v2 string）会被 validate_config 在 backend 丢弃；
    // 这里也预处理以保持前端 config 一致
    STAGE_KEYS.forEach(function(k) {
      var s = cfg[k];
      if (s && typeof s === 'object' && 'stop_after' in s) {
        delete s.stop_after;
      }
    });
    return cfg;
  }

  // ---- REQ-20260921-NNN v2：填充 fine_cut 下拉（仅设置参数模板）----
  // loadPanel 末尾调用 _pipePanelPopulateDeps(taskId)。
  // - params：从 /list_fine_global_profiles 拿模板；追加「当前参数」/「导入 JSON」选项
  // REQ-20260921-NNN：当 /pipeline_get 返回 has_fc_json=false 时，「当前参数」禁用
  // （没 fc.json 可用「当前」），并把面板提示文案动态改成「必须选模板或去精剪页导入」。
  // v2 用户反馈：bgm 是死字段（已从 cfg.fine_cut 移除；BGM 由工作台第 6 阶段
  // 详情页「🎵 背景音乐」上传到 fc.json 的 materials.audio.path），所以这里
  // 不再调任何 bgm 端点填下拉 —— 没有 data-bgm-select 元素。
  var _pipePanelPopulateDeps = async function(taskId, hasFcJson) {
    var paramSel = document.querySelector('select[data-params-select]');
    var fcEnabled = !!document.getElementById(fieldId('fine_cut', 'enabled')) &&
                    !!document.getElementById(fieldId('fine_cut', 'enabled')).checked;
    if (paramSel) {
      var curParam = paramSel.value || 'current';
      try {
        var r2 = await fetch('/slirn/api/list_fine_global_profiles', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({})
        });
        var j2 = await r2.json();
        paramSel.innerHTML = '';
        // REQ-20260921-NNN：has_fc_json=false 时，「当前参数」禁用
        // （灰色 + title 解释为什么不能用）。
        var curOpt = document.createElement('option');
        curOpt.value = 'current';
        if (hasFcJson === false) {
          curOpt.textContent = '当前参数（未保存 — 不可用）';
          curOpt.disabled = true;
          curOpt.title = '本任务尚未保存精剪参数（fine_compose.json 不存在）；' +
                         '「当前参数」不能用。请选模板或去精剪合成详情页「导入参数」。';
        } else {
          curOpt.textContent = '当前参数（用面板保存的）';
        }
        if (curParam === 'current' && hasFcJson !== false) curOpt.selected = true;
        paramSel.appendChild(curOpt);
        var profs = (j2 && j2.profiles) ? j2.profiles : [];
        profs.forEach(function(p) {
          var opt = document.createElement('option');
          var id = p.id || p.name || '';
          opt.value = 'template:' + id;
          opt.textContent = '📋 模板：' + (p.name || id);
          if (curParam === 'template:' + id) opt.selected = true;
          paramSel.appendChild(opt);
        });
        var importOpt = document.createElement('option');
        importOpt.value = 'import'; importOpt.textContent = '📥 导入 JSON（手动）';
        if (curParam === 'import') importOpt.selected = true;
        paramSel.appendChild(importOpt);
        // 如果「当前参数」不可用 + 之前选了 current，回落到第一个 template（或 import）
        if (hasFcJson === false && paramSel.value === 'current') {
          paramSel.value = paramSel.querySelector('option:not([disabled])').value || 'import';
        }
      } catch (e) { console.warn('[pipe-params-load]', e); }
    }
    // REQ-20260921-NNN：动态更新精剪参数提示文案 + 启用 checkbox 时高亮「未保存」警告
    var paramsNote = document.querySelector('[data-fine-cut-params-note]');
    if (paramsNote) {
      if (hasFcJson === false) {
        paramsNote.innerHTML = '⚠ 本任务<strong>尚未保存过精剪参数</strong>（' +
          '<code>fine_compose.json</code> 不存在）。「当前参数」不可用，请在此处' +
          '<strong>选择模板</strong>，或去「第 6 阶段 · 精剪合成」详情页' +
          '<strong>「导入参数」</strong>。';
        paramsNote.style.color = '#c0392b';
      } else {
        paramsNote.innerHTML = '>若本任务已保存过精剪参数（<code>fine_compose.json</code>），可选「当前参数」；' +
          '否则必须选模板，或去精剪合成详情页「导入参数」。';
        paramsNote.style.color = '';
      }
    }
    // REQ-20260921-NNN-radio-mode：单选卡片切换 HH:MM:SS 输入框 disabled 状态。
    // 选「按区间导出一段」= enable inputs（用户要配起点 + 时长）；选「出整个片」=
    // disable inputs（全片导没必要配区间）。无需联动 enabled — radio 已经保证
    // 二选一语义（不可能漏勾），enabled 在 readCurrentConfig 里从 radio 派生。
    var exportModeRadios = document.querySelectorAll('input[name="slirn-pipe-fc-export-mode"]');
    if (exportModeRadios.length) {
      exportModeRadios.forEach(function(radio) {
        radio.addEventListener('change', function() {
          if (!radio.checked) return;
          var inputs = document.querySelectorAll('[data-range-input]');
          inputs.forEach(function(i) { i.disabled = radio.value !== 'range'; });
        });
      });
    }
  };

  // ---- 表单 → config ----
  function _val(id, fallback) {
    var el = document.getElementById(id);
    return el ? el.value : fallback;
  }
  function _checked(id, fallback) {
    var el = document.getElementById(id);
    return el ? !!el.checked : fallback;
  }
  // REQ-20260921-NNN-v4：HH:MM:SS ↔ 秒（双向）。
  // - secondsToHms(0)    = "00:00:00"
  // - secondsToHms(600)  = "00:10:00"
  // - secondsToHms(9669) = "02:41:09"
  // - hmsToSeconds("00:30:00") = 1800；解析失败返回 null（让调用方决定兜底）
  function _pad2(n) { return n < 10 ? '0' + n : String(n); }
  function secondsToHms(sec) {
    var s = Math.max(0, Math.floor(Number(sec) || 0));
    var h = Math.floor(s / 3600);
    var m = Math.floor((s % 3600) / 60);
    var ss = s % 60;
    return _pad2(h) + ':' + _pad2(m) + ':' + _pad2(ss);
  }
  function hmsToSeconds(str) {
    if (str == null) return null;
    var s = String(str).trim();
    if (!s) return null;
    var m = s.match(/^(\d{1,3}):([0-5]\d):([0-5]\d)$/);
    if (!m) return null;
    return parseInt(m[1], 10) * 3600 + parseInt(m[2], 10) * 60 + parseInt(m[3], 10);
  }
  function readCurrentConfig() {
    var panel = document.getElementById('slirn-pipe-panel');
    if (!panel) return null;
    var cfg = {};
    // 2026-09-30 用户要求：复选框默认全选 — DOM 元素缺失时的 _checked 回退值
    // 与后端 default_config 新默认（True）对齐
    cfg.subtitle_generation = {
      speaker_diarization: _checked(fieldId('subtitle_generation', 'sd'), true)
    };
    cfg.subtitle_review = {
      accept_all_suggestions: _checked(fieldId('subtitle_review', 'accept-all'), true),
      // REQ-20260921-NNN：自动关联人员ID（后端 handler_subtitle_review 末尾触发 /rev_speaker_link）
      link_person_ids: _checked(fieldId('subtitle_review', 'link-person'), true),
      skip_categories: _val(fieldId('subtitle_review', 'skip-cats'), '').split(',').map(function(x){return x.trim();}).filter(Boolean),
      // REQ-20260918-049：从 radio cards 读 rigor（pipe 通道对 custom 档降级为 medium，
      // 与后端 handler_subtitle_review 校验一致）
      rigor: (function() {
        var el = document.querySelector('input[name="slirn-rev-rigor"]:checked');
        var v = el ? el.value : 'medium';
        return (v === 'high' || v === 'medium' || v === 'low') ? v : 'medium';
      })()
    };
    cfg.rough_cut = {
      delete_speakers: _val(fieldId('rough_cut', 'del-spk'), '').split(',').map(function(x){return parseInt(x.trim(), 10);}).filter(function(x){return !isNaN(x);}),
      default_decision: _val(fieldId('rough_cut', 'def-dec'), 'keep') || 'keep',
      // REQ-20260921-NNN：自动关联人员ID（与 delete_speakers 互斥，handler 优先 delete_speakers）
      link_person_ids: _checked(fieldId('rough_cut', 'link-person'), true)
    };
    cfg.rough_compose = {};
    cfg.optimize = {
      accept_all_replacements: _checked(fieldId('optimize', 'accept-rep'), true)
    };
    // REQ-20260921-NNN-radio-mode：精剪合成改单选卡片组（默认 range）。
    //   - 「出整个片」→ enabled=true, range_enabled=false（全片，1-3h）
    //   - 「按区间导出一段」→ enabled=true, range_enabled=true（默认 10min）
    // 没有第三「跳过」选项 — 要跳过 fine_cut 走顶层 stop_after="optimize"。
    // 单选物理保证二选一，无需任何 enabled/range_enabled 联动。
    var fcModeRadio = document.querySelector('input[name="slirn-pipe-fc-export-mode"]:checked');
    var fcMode = fcModeRadio ? String(fcModeRadio.value || '') : '';
    var fcEnabled = (fcMode === 'full' || fcMode === 'range');
    var fcRange = fcMode === 'range';
    var fcParams = _val(fieldId('fine_cut', 'params-src'), 'current') || 'current';
    var fcStartRaw = _val(fieldId('fine_cut', 'start'), '00:00:00');
    var fcDurRaw = _val(fieldId('fine_cut', 'dur'), '00:10:00');
    var fcStartSec = hmsToSeconds(fcStartRaw);
    var fcDurSec = hmsToSeconds(fcDurRaw);
    // HH:MM:SS 解析失败 → 兜底（start=0, duration=600）并 toast 提示
    if (fcStartSec == null) {
      fcStartSec = 0;
      try { window.slirnToast && window.slirnToast('导出起点格式不对（应为 HH:MM:SS），已用 00:00:00 兜底', 'warn'); } catch (e) {}
    }
    if (fcDurSec == null) {
      fcDurSec = 600;  // 10 分钟兜底
      try { window.slirnToast && window.slirnToast('导出时长格式不对（应为 HH:MM:SS），已用 00:10:00 兜底', 'warn'); } catch (e) {}
    }
    // v5 顶层 run_mode + stop_after：
    // - run_mode="to_end" → 服务端 validate_config 强制 stop_after=None
    // - run_mode="stop_after" → stop_after 由用户选（"" → null = 跑到结尾但不停）
    var runMode = _val('slirn-pipe-run-mode', 'stop_after');
    cfg.run_mode = (runMode === 'to_end' || runMode === 'stop_after') ? runMode : 'stop_after';
    cfg.fine_cut = {
      enabled: fcEnabled,
      params_source: fcParams,
      range_enabled: fcRange,
      preview_start: fcStartSec,
      duration: fcDurSec
    };
    var flowStop = _val('slirn-pipe-flow-stop', '');
    cfg.stop_after = flowStop || null;
    return cfg;
  }

  // ---- REQ-20260918-049：智能续跑 + 阶段状态回显 helper ----
  // 从 history 最后一条 summary 算出「下一该跑的 stage key」。
  // history 顺序：append_history 是 append 到尾部，最新 summary = history[-1]（非 reverse）。
  // 返回 null 表示「从头跑」（从未跑过 / 首阶段就挂 / 5 阶段全跑完）。
  function computeNextSince(history) {
    if (!Array.isArray(history) || history.length === 0) return null;
    var last = history[history.length - 1];
    if (!last || typeof last !== 'object') return null;
    var done = Array.isArray(last.stages_done) ? last.stages_done : [];
    // 全跑完 → 从头跑
    if (done.length >= STAGE_KEYS.length) return null;
    // 取 STAGE_KEYS 里最后一个已被 done 覆盖的索引 + 1 = 下个该跑
    var lastIdx = -1;
    for (var i = 0; i < done.length; i++) {
      var ix = STAGE_KEYS.indexOf(done[i]);
      if (ix > lastIdx) lastIdx = ix;
    }
    // v2 REQ-20260921-NNN-skip-since：上次若显式带 since 且 since 比 done 末
    // 尾还靠后（典型场景：fine_cut enabled=False 被跳过 → stages_done=[]，
    // 但 since=fine_cut），下一次应继续从 since 处跑。否则按钮变成「从字幕生成开
    // 始」，误导且每次点都从头跑前 4 阶段浪费时间。
    if (typeof last.since === 'string' && last.since) {
      var sinceIdx = STAGE_KEYS.indexOf(last.since);
      if (sinceIdx >= 0 && sinceIdx > lastIdx) return last.since;
    }
    return STAGE_KEYS[lastIdx + 1] || null;
  }

  // 从 status 对象推导已完成的 stage key 列表：
  // 1) summary.stages_done（已完成 run 才有）→ 2) log 里 '✅ X 完成' 标记（running 中）
  function deriveStagesDone(st) {
    if (!st) return [];
    if (st.summary && Array.isArray(st.summary.stages_done)) {
      return st.summary.stages_done.slice();
    }
    var done = [];
    var seen = {};
    var log = Array.isArray(st.log) ? st.log : [];
    for (var i = 0; i < log.length; i++) {
      var e = log[i] || {};
      var k = e.stage, m = e.msg || '';
      if (!k || seen[k]) continue;
      // 通用判定：✅/完成 字样视为该阶段已完成
      if (m.indexOf('✅') >= 0 && m.indexOf('完成') >= 0) {
        seen[k] = 1;
        done.push(k);
      }
    }
    return done;
  }
  // REQ-20260921-NNN-skip-warn：单独推导跳过的 stage key 列表，让徽章区分
  // 「✓ 已完成（真跑通）」vs「⚠️ 已跳过（cfg.enabled=False 之类，没真做）」
  // — 之前两者都显示 ✅，用户以为 fine_cut 跑完了实际 fine_export.mp4 根本没生成。
  function deriveStagesSkipped(st) {
    if (!st) return [];
    if (st.summary && Array.isArray(st.summary.stages_skipped)) {
      return st.summary.stages_skipped.slice();
    }
    // running 中：log 里 '跳过' 字样视为该阶段被跳过
    var sk = [];
    var seen = {};
    var log = Array.isArray(st.log) ? st.log : [];
    for (var i = 0; i < log.length; i++) {
      var e = log[i] || {};
      var k = e.stage, m = e.msg || '';
      if (!k || seen[k]) continue;
      if (m.indexOf('跳过') >= 0) {
        seen[k] = 1;
        sk.push(k);
      }
    }
    return sk;
  }

  // 单个 stage 当前状态：error / running / done / pending / skipped
  // REQ-20260921-NNN-skip-warn：skipped 优先于 done（不会两者同时）——
  // 语义上「跳过」不是「完成」。UI 徽章渲染 ⚠️ 已跳过（黄）区别 ✓ 已完成（绿）。
  function stageStateOf(stageKey, st, doneSet, skipSet, currentStage) {
    if (!st) return 'pending';
    if (st.state === 'error' && currentStage === stageKey) return 'error';
    if (st.state === 'running' && currentStage === stageKey) return 'running';
    if (skipSet.indexOf(stageKey) >= 0) return 'skipped';
    if (doneSet.indexOf(stageKey) >= 0) return 'done';
    return 'pending';
  }

  // 格式化时长（秒 → "X秒" / "X分Y秒" / "X时Y分"）
  function fmtElapsed(seconds) {
    seconds = Math.max(0, Math.floor(seconds || 0));
    if (seconds < 60) return seconds + '秒';
    if (seconds < 3600) return Math.floor(seconds / 60) + '分' + (seconds % 60) + '秒';
    return Math.floor(seconds / 3600) + '时' + Math.floor((seconds % 3600) / 60) + '分';
  }

  // 给每个 stage section 加状态徽章（⏳/✅/⏸/❌）+ 当前阶段百分比 + 已耗时。
  // 接入 pollStatus（running 中轮询）和 loadPanel（idle 终态读 history）两路径。
  function updateStageBadges(st) {
    if (!st) return;
    var doneSet = deriveStagesDone(st);
    // REQ-20260921-NNN-skip-warn：也读 skipped，用于徽章区分真跑通 vs 跳过
    var skipSet = deriveStagesSkipped(st);
    var currentStage = st.current_stage || null;
    STAGES.forEach(function(stage, idx) {
      var sec = document.querySelector(
        '#slirn-pipe-panel .slirn-pipe-section[data-pipe-section="' + stage.key + '"]'
      );
      if (!sec) return;
      var s = stageStateOf(stage.key, st, doneSet, skipSet, currentStage);
      sec.setAttribute('data-pipe-state', s);

      // num 圆形：done → ✓，running → ⏳，skipped → ⚠，其他 → 原始序号
      var num = sec.querySelector('.slirn-pipe-section-num');
      if (num) {
        num.textContent = s === 'done' ? '✓'
          : s === 'running' ? '⏳'
          : s === 'skipped' ? '⚠'
          : String(idx + 1);
      }

      // 徽章：在 label 后插/更新
      var lbl = sec.querySelector('.slirn-pipe-section-label');
      if (!lbl) return;
      var badge = sec.querySelector('.slirn-pipe-section-state');
      if (!badge) {
        badge = document.createElement('span');
        // 插在 label 之后（hint 之前）
        if (lbl.nextSibling) {
          lbl.parentNode.insertBefore(badge, lbl.nextSibling);
        } else {
          lbl.parentNode.appendChild(badge);
        }
      }
      badge.className = 'slirn-pipe-section-state slirn-pipe-section-state-' + (
        s === 'running' ? 'running' :
        s === 'done'    ? 'done'    :
        s === 'skipped' ? 'skipped' :
        s === 'error'   ? 'error'   : 'pending'
      );
      var pctTxt = '';
      if (s === 'running' && st.percent != null) {
        pctTxt = ' · ' + Math.round(Number(st.percent || 0)) + '%';
      }
      var elapsedTxt = '';
      if (s === 'running' && st.started_at) {
        elapsedTxt = ' · 已耗时 ' + fmtElapsed((Date.now() / 1000) - Number(st.started_at));
      }
      var textMap = {
        running: '⏳ 运行中' + pctTxt + elapsedTxt,
        done:    '✅ 已完成',
        // REQ-20260921-NNN-skip-warn：徽章文案区别于 done（避免用户误以为跑完了）
        skipped: '⚠️ 已跳过',
        error:   '❌ 出错',
        pending: '⏸ 待执行'
      };
      badge.textContent = textMap[s] || s;
    });
  }

  // ---- 状态条 ----
  var pipeStatusTimer = null;
  // 状态条位置持久化 key（per-task，跨 session 保留）
  function _statusPosKey() {
    var t = curTaskId() || 'global';
    return 'slirn-pipe-status-pos-' + t;
  }
  function _restoreStatusPos(box) {
    if (!box) return;
    try {
      var raw = localStorage.getItem(_statusPosKey());
      if (!raw) return;
      var p = JSON.parse(raw);
      if (typeof p.top === 'number')   box.style.top   = p.top + 'px';
      if (typeof p.left === 'number')  box.style.left  = p.left + 'px';
      // 用 left/top 定位后清掉 right，避免冲突
      box.style.right = 'auto';
      box.style.bottom = 'auto';
    } catch (e) { /* localStorage 不可用时静默 */ }
  }
  function _saveStatusPos(box) {
    if (!box) return;
    try {
      var rect = box.getBoundingClientRect();
      localStorage.setItem(_statusPosKey(), JSON.stringify({
        top: Math.round(rect.top),
        left: Math.round(rect.left)
      }));
    } catch (e) { /* 静默 */ }
  }
  // 状态条拖动：head 是手柄（除了 collapse 按钮）
  function _bindStatusDrag(box) {
    if (!box || box.__dragBound) return;
    var head = box.querySelector('.slirn-pipe-status-head');
    if (!head) return;  // 没有 head 就不要标记，等 renderStatusBar 写入后再绑
    box.__dragBound = true;
    var startX = 0, startY = 0, startLeft = 0, startTop = 0, dragging = false;
    head.addEventListener('pointerdown', function(ev) {
      // 拖手柄排除：collapse 按钮本身（它有 data-action）
      if (ev.target.closest('[data-action]')) return;
      dragging = true;
      startX = ev.clientX;
      startY = ev.clientY;
      var rect = box.getBoundingClientRect();
      startLeft = rect.left;
      startTop = rect.top;
      // 用 left/top 定位（清掉 right/bottom）
      box.style.left = startLeft + 'px';
      box.style.top = startTop + 'px';
      box.style.right = 'auto';
      box.style.bottom = 'auto';
      try { head.setPointerCapture(ev.pointerId); } catch (e) {}
      ev.preventDefault();
    });
    head.addEventListener('pointermove', function(ev) {
      if (!dragging) return;
      var dx = ev.clientX - startX;
      var dy = ev.clientY - startY;
      var newLeft = Math.max(0, Math.min(window.innerWidth - 100,  startLeft + dx));
      var newTop  = Math.max(0, Math.min(window.innerHeight - 40, startTop  + dy));
      box.style.left = newLeft + 'px';
      box.style.top = newTop + 'px';
    });
    var _endDrag = function(ev) {
      if (!dragging) return;
      dragging = false;
      _saveStatusPos(box);
      try { head.releasePointerCapture(ev.pointerId); } catch (e) {}
    };
    head.addEventListener('pointerup', _endDrag);
    head.addEventListener('pointercancel', _endDrag);
  }
  function showStatus(taskId) {
    var box = document.getElementById('slirn-pipe-status');
    if (!box) return;
    box.hidden = false;
    box.setAttribute('data-task-id', taskId);
    _restoreStatusPos(box);
    _bindStatusDrag(box);
    refreshStatusToggleBtn();
    if (pipeStatusTimer) { clearInterval(pipeStatusTimer); pipeStatusTimer = null; }
    pipeStatusTimer = setInterval(function() { pollStatus(taskId); }, 1500);
    pollStatus(taskId);
  }
  function hideStatus() {
    var box = document.getElementById('slirn-pipe-status');
    if (!box) return;
    box.hidden = true;
    if (pipeStatusTimer) { clearInterval(pipeStatusTimer); pipeStatusTimer = null; }
    box.innerHTML = '';
    refreshStatusToggleBtn();
  }
  // pipe-panel 头部「📊 显示状态」按钮的可见性（仅 hidden 时显示）
  function refreshStatusToggleBtn() {
    var box = document.getElementById('slirn-pipe-status');
    var btn = document.querySelector('#slirn-pipe-panel [data-action="pipe-status-show"]');
    if (!btn) return;
    btn.hidden = !(box && box.hidden);
  }

  // ---- REQ-20260921-NNN-outputs-browser：产物文件浏览器 ----
  // 渲染单条产物项（含视频预览 / 字幕文本预览 / JSON 折叠 / 音频 / 其他文件下载）
  function _renderOutputItem(item) {
    var sizeKb = (item.size / 1024).toFixed(1);
    var sizeMb = (item.size / 1024 / 1024).toFixed(2);
    var sizeText = item.size > 1024 * 1024 ? (sizeMb + ' MB') : (sizeKb + ' KB');
    var kindEmoji = {
      video: '🎬', subtitle: '📝', audio: '🎵',
      json: '📋', image: '🖼', log: '📜', other: '📄'
    }[item.kind] || '📄';
    var scopeTag = item.scope === 'task_root' ? '（任务根）' : '';
    // previewable=true → 给 <video>/<audio>/文本容器；否则只给下载按钮
    var previewHtml = '';
    if (item.kind === 'video' && item.previewable) {
      previewHtml = '<video controls preload="metadata" class="slirn-pipe-outputs-video" '
        + 'src="' + escapeHtml(item.url) + '"></video>';
    } else if (item.kind === 'audio' && item.previewable) {
      previewHtml = '<audio controls preload="metadata" class="slirn-pipe-outputs-audio" '
        + 'src="' + escapeHtml(item.url) + '"></audio>';
    } else if (item.kind === 'subtitle' && item.previewable) {
      // 字幕预览用 fetch 抓文本内容（前 30 行）显示在 <pre>
      previewHtml = '<details class="slirn-pipe-outputs-sub-details">'
        + '<summary class="slirn-pipe-outputs-sub-summary">查看字幕内容</summary>'
        + '<pre class="slirn-pipe-outputs-sub-pre" data-pipe-outputs-sub-pre '
        + 'data-sub-url="' + escapeHtml(item.url) + '">（点击展开后加载…）</pre>'
        + '</details>';
    } else if (item.kind === 'json' && item.previewable) {
      previewHtml = '<details class="slirn-pipe-outputs-json-details">'
        + '<summary class="slirn-pipe-outputs-json-summary">查看 JSON 内容</summary>'
        + '<pre class="slirn-pipe-outputs-json-pre" data-pipe-outputs-json-pre '
        + 'data-json-url="' + escapeHtml(item.url) + '">（点击展开后加载…）</pre>'
        + '</details>';
    } else if (item.kind === 'image' && item.previewable) {
      previewHtml = '<img class="slirn-pipe-outputs-img" src="' + escapeHtml(item.url) + '" alt="">';
    }
    var mtimeText = item.mtime ? _formatMtime(item.mtime) : '';
    return '<div class="slirn-pipe-outputs-item" data-pipe-outputs-item '
      + 'data-kind="' + escapeHtml(item.kind) + '">'
      + '<div class="slirn-pipe-outputs-item-head">'
      + '<span class="slirn-pipe-outputs-emoji">' + kindEmoji + '</span>'
      + '<span class="slirn-pipe-outputs-label">' + escapeHtml(item.label) + '</span>'
      + '<span class="slirn-pipe-outputs-name">' + escapeHtml(item.name) + scopeTag + '</span>'
      + (mtimeText ? '<span class="slirn-pipe-outputs-mtime" title="文件最后修改时间">🕒 ' + mtimeText + '</span>' : '')
      + '<span class="slirn-pipe-outputs-size">' + sizeText + '</span>'
      + '<a class="slirn-btn slirn-btn-sm slirn-pipe-outputs-dl" '
      + 'href="' + escapeHtml(item.url) + '" download="' + escapeHtml(item.name) + '">⬇ 下载</a>'
      + '</div>'
      + '<div class="slirn-pipe-outputs-item-body">' + previewHtml + '</div>'
      + '</div>';
  }

  // REQ-20260921-NNN-outputs-browser-v2：缓存全量 items + 当前筛选条件。
  // 切 chip 不重新拉接口，只是从 _outputsAllItems 过滤后重新渲染。
  var _outputsAllItems = [];
  var _outputsCurrentFilter = '__all__';
  var _KIND_LABELS = {
    video: '🎬 视频',
    subtitle: '📝 字幕',
    audio: '🎵 音频',
    image: '🖼 图片',
    json: '📋 JSON',
    log: '📜 日志',
    other: '📄 其他',
  };
  // 把 list_outputs 返回的 items 渲染到列表区
  function _renderOutputsList(items) {
    var box = document.querySelector('[data-pipe-outputs-list]');
    var filterBox = document.querySelector('[data-pipe-outputs-filter]');
    if (!box) return;
    _outputsAllItems = items || [];
    if (!_outputsAllItems.length) {
      if (filterBox) { filterBox.hidden = true; filterBox.innerHTML = ''; }
      _outputsCurrentFilter = '__all__';
      box.innerHTML = '<div class="slirn-pipe-outputs-empty">'
        + '该任务还没有任何产物（先跑流程或单独执行各阶段）。</div>';
      return;
    }
    // 统计每个 kind 的数量（chip 上展示）
    var counts = {video: 0, subtitle: 0, audio: 0, image: 0, json: 0, log: 0, other: 0};
    _outputsAllItems.forEach(function(it) {
      var k = counts[it.kind] != null ? it.kind : 'other';
      counts[k] += 1;
    });
    // 渲染 chip 行（全部 + 每个有产物的 kind）
    if (filterBox) {
      var chipHtml = '';
      chipHtml += '<button type="button" class="slirn-pipe-outputs-chip" '
        + 'data-action="pipe-outputs-filter" data-pipe-outputs-chip="__all__" '
        + 'data-filter="__all__" '
        + (_outputsCurrentFilter === '__all__' ? 'data-active="1"' : '') + '>'
        + '📦 全部（' + _outputsAllItems.length + '）</button>';
      Object.keys(_KIND_LABELS).forEach(function(kind) {
        if (!counts[kind]) return;  // 空 kind 不出 chip（避免误点空筛）
        chipHtml += '<button type="button" class="slirn-pipe-outputs-chip" '
          + 'data-action="pipe-outputs-filter" data-pipe-outputs-chip="' + kind + '" '
          + 'data-filter="' + kind + '" '
          + (_outputsCurrentFilter === kind ? 'data-active="1"' : '') + '>'
          + _KIND_LABELS[kind] + '（' + counts[kind] + '）</button>';
      });
      filterBox.innerHTML = chipHtml;
      filterBox.hidden = false;
    }
    // 切到当前激活 kind 的 items
    var visible = _outputsCurrentFilter === '__all__'
      ? _outputsAllItems.slice()
      : _outputsAllItems.filter(function(it) { return it.kind === _outputsCurrentFilter; });
    if (!visible.length) {
      box.innerHTML = '<div class="slirn-pipe-outputs-empty">该类别下没有产物。</div>';
      return;
    }
    // 按 kind 分组显示（视频 / 字幕 / 音频 / JSON / 其他）
    var groups = {video: [], subtitle: [], audio: [], json: [], image: [], log: [], other: []};
    visible.forEach(function(it) {
      var k = groups[it.kind] ? it.kind : 'other';
      groups[k].push(it);
    });
    var order = [
      ['video', '🎬 视频'],
      ['subtitle', '📝 字幕'],
      ['audio', '🎵 音频'],
      ['image', '🖼 图片'],
      ['json', '📋 JSON 元数据'],
      ['log', '📜 日志'],
      ['other', '📄 其他'],
    ];
    var html = '';
    order.forEach(function(pair) {
      var kind = pair[0], title = pair[1];
      if (!groups[kind].length) return;
      html += '<div class="slirn-pipe-outputs-group" data-outputs-kind="' + kind + '">'
        + '<div class="slirn-pipe-outputs-group-title">' + title + '（' + groups[kind].length + '）</div>';
      groups[kind].forEach(function(it) {
        html += _renderOutputItem(it);
      });
      html += '</div>';
    });
    box.innerHTML = html;
    // 绑定字幕 / JSON 折叠懒加载（避免一次性下载所有）
    box.querySelectorAll('[data-pipe-outputs-sub-details]').forEach(function(d) {
      d.addEventListener('toggle', function() {
        if (!d.open) return;
        var pre = d.querySelector('[data-pipe-outputs-sub-pre]');
        if (!pre || pre.dataset.loaded === '1') return;
        var url = pre.dataset.subUrl;
        fetch(url).then(function(r) {
          if (!r.ok) { pre.textContent = '加载失败: HTTP ' + r.status; return; }
          return r.text();
        }).then(function(txt) {
          if (txt == null) return;
          // 只显示前 200 行，避免长字幕卡顿
          var lines = txt.split('\n');
          pre.textContent = lines.length > 200
            ? lines.slice(0, 200).join('\n') + '\n…（共 ' + lines.length + ' 行，已截断）'
            : txt;
          pre.dataset.loaded = '1';
        }).catch(function(e) { pre.textContent = '加载失败: ' + e; });
      });
    });
    box.querySelectorAll('[data-pipe-outputs-json-details]').forEach(function(d) {
      d.addEventListener('toggle', function() {
        if (!d.open) return;
        var pre = d.querySelector('[data-pipe-outputs-json-pre]');
        if (!pre || pre.dataset.loaded === '1') return;
        var url = pre.dataset.jsonUrl;
        fetch(url).then(function(r) {
          if (!r.ok) { pre.textContent = '加载失败: HTTP ' + r.status; return; }
          return r.text();
        }).then(function(txt) {
          if (txt == null) return;
          // JSON 美化（如果可解析）
          try {
            var obj = JSON.parse(txt);
            txt = JSON.stringify(obj, null, 2);
          } catch (e) { /* 保留原文 */ }
          // 截断（> 200KB 不全展开）
          if (txt.length > 200 * 1024) {
            txt = txt.slice(0, 200 * 1024) + '\n…（已截断，共 ' + txt.length + ' 字符）';
          }
          pre.textContent = txt;
          pre.dataset.loaded = '1';
        }).catch(function(e) { pre.textContent = '加载失败: ' + e; });
      });
    });
  }
  // 应用筛选（仅切 chip，不需要重新拉接口）
  function _applyOutputsFilter(kind) {
    _outputsCurrentFilter = kind || '__all__';
    _renderOutputsList(_outputsAllItems);
  }
  // 把 mtime（epoch 秒）格式化为「YYYY-MM-DD HH:MM」本地时间
  function _formatMtime(epoch) {
    if (!epoch || isNaN(epoch)) return '';
    var d = new Date(epoch * 1000);
    var pad = function(n) { return n < 10 ? '0' + n : '' + n; };
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate())
      + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
  }

  // 点击「🔄 刷新产物列表」时调 /slirn/api/list_outputs
  // 防并发：用一个 ongoingTasksR 标记，refreshOutputsList 进行中再点会忽略。
  // 5s 超时（fetch 没返回 → 报「加载失败: 超时」而不是永远「加载中…」）。
  var _outputsRefreshBusy = false;
  async function refreshOutputsList() {
    var panel = document.getElementById('slirn-pipe-panel');
    if (!panel) return;
    var taskId = panel.getAttribute('data-task-id');
    if (!taskId) return;
    if (_outputsRefreshBusy) return;
    _outputsRefreshBusy = true;
    var box = document.querySelector('[data-pipe-outputs-list]');
    if (box) {
      box.innerHTML = '<div class="slirn-pipe-outputs-empty">加载中…</div>';
    }
    try {
      var r = await _postJSONWithTimeout(SLIRN_API + '/list_outputs', {task_id: taskId}, 5000);
      if (!r || !r.ok) {
        if (box) box.innerHTML = '<div class="slirn-pipe-outputs-empty">'
          + '加载失败: ' + (r && r.error ? escapeHtml(r.error) : '未知错误') + '</div>';
        return;
      }
      var payload = r.html || {};
      _renderOutputsList(payload.items || []);
    } catch (e) {
      if (box) box.innerHTML = '<div class="slirn-pipe-outputs-empty">'
        + '加载失败: ' + escapeHtml(String(e && e.message || e)) + '</div>';
    } finally {
      _outputsRefreshBusy = false;
    }
  }
  // postJSON + 超时（fetch 没在 ms 毫秒内返回就 reject，AbortController 取消）
  function _postJSONWithTimeout(url, payload, ms) {
    var ctrl = ('AbortController' in window) ? new AbortController() : null;
    var timer = setTimeout(function() { if (ctrl) ctrl.abort(); }, ms || 5000);
    var p = fetch(url, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload || {}),
      signal: ctrl ? ctrl.signal : undefined,
    });
    return p.then(function(r) { clearTimeout(timer); return r.json(); })
      .catch(function(e) {
        clearTimeout(timer);
        // AbortError → 包装成「超时」错误消息
        if (e && (e.name === 'AbortError' || e.code === 20)) {
          return Promise.reject(new Error('请求超时（>5s）'));
        }
        return {ok: false, error: String(e)};
      });
  }
  function pollStatus(taskId) {
    postJSON(SLIRN_API + '/pipeline_status', {task_id: taskId}).then(function(r) {
      if (!r || !r.ok) return;
      var prevRunning = (pollStatus._lastState === 'running');
      var nowRunning = (r.state === 'running');
      pollStatus._lastState = r.state;
      renderStatusBar(r);
      // REQ-20260918-049：每个 stage section 加徽章回显
      updateStageBadges(r);
      if (!nowRunning) {
        if (pipeStatusTimer) { clearInterval(pipeStatusTimer); pipeStatusTimer = null; }
        // 终态再画一次（确保徽章显示最终态而非中间态）
        updateStageBadges(r);
        // REQ-20260918-049 v3：终态后刷新头部「续跑」按钮（让按钮反映最新 stages_done）
        refreshRunBtnFromStatus(r);
        // REQ-20260921-NNN-outputs-browser：只在 running → 终态（边沿触发）才自动 refresh，
        // 否则每次 poll 都会刷新一次（idle 状态下也会调，浪费请求 + 可能堆积）。
        if (prevRunning && !nowRunning && document.querySelector('[data-pipe-outputs-list]')) {
          refreshOutputsList();
        }
      }
    });
  }
  // REQ-20260918-049 v3：终态后从 status 推导 stages_done，更新头部按钮（避免按钮停在「从头跑」误判）
  function refreshRunBtnFromStatus(r) {
    var btn = document.querySelector('#slirn-pipe-panel [data-action="pipe-run"]');
    if (!btn) return;
    var done = deriveStagesDone(r);
    var fakeHistory = [{stages_done: done, status: r.state}];
    var sinceKey = computeNextSince(fakeHistory);
    if (sinceKey) {
      btn.setAttribute('data-since', sinceKey);
      btn.textContent = '▶ 续跑 (从「' + (STAGE_LABELS[sinceKey] || sinceKey) + '」开始)';
      btn.setAttribute('title', '上次跑到了「' + (STAGE_LABELS[sinceKey] || sinceKey) + '」之前；点此从该处开始（跳过已完成阶段）');
    } else {
      btn.removeAttribute('data-since');
      btn.textContent = '▶ 从头跑';
      btn.setAttribute('title', '保存并按当前配置顺序执行所有阶段');
    }
  }
  function renderStatusBar(st) {
    var box = document.getElementById('slirn-pipe-status');
    if (!box) return;
    var stageLabel = st.current_stage ? (STAGE_LABELS[st.current_stage] || st.current_stage) : '—';
    var pct = Math.max(0, Math.min(100, Number(st.percent || 0)));
    var log = Array.isArray(st.log) ? st.log : [];
    var last3 = log.slice(-3).map(function(e) {
      var lvl = e.level || 'info';
      var sign = lvl === 'error' ? '❌' : (lvl === 'ok' ? '✅' : (lvl === 'warn' ? '⚠️' : '•'));
      return '<div class="slirn-pipe-status-log-row slirn-pipe-status-log-' + lvl + '">'
        + '<span class="slirn-pipe-status-log-sign">' + sign + '</span>'
        + '<span class="slirn-pipe-status-log-msg">' + escapeHtml(e.msg || '') + '</span></div>';
    }).join('');
    var stateLabel = ({
      running: '运行中',
      done: '已完成',
      error: '出错',
      stopped: '已停止',
      idle: '空闲'
    })[st.state] || st.state;
    // REQ-20260921-NNN-skip-warn：state=done 但含 skipped 时改文案为「已完成（含跳过）」
    // 并显示橙色（不再绿），避免用户在 status 顶部误以为全部真跑通
    var skipSet = deriveStagesSkipped(st);
    var stateClass = st.state;
    if (st.state === 'done' && skipSet.length > 0) {
      stateLabel = '已完成（含 ' + skipSet.length + ' 个跳过）';
      stateClass = 'done-with-skip';
    }
    box.innerHTML =
      '<div class="slirn-pipe-status-head" title="拖动此处移动状态条">'
      + '<span class="slirn-pipe-status-state slirn-pipe-status-state-' + stateClass + '">'
      + (st.state === 'running' ? '⏳'
        : (st.state === 'done' && skipSet.length > 0) ? '⚠'
        : (st.state === 'done' ? '✅'
        : (st.state === 'error' ? '❌'
        : (st.state === 'stopped' ? '⏹' : '•'))))
      + ' ' + escapeHtml(stateLabel) + '</span>'
      + '<span class="slirn-pipe-status-stage">当前阶段：' + escapeHtml(stageLabel) + '</span>'
      + '<progress class="slirn-pipe-status-bar" max="100" value="' + pct + '"></progress>'
      + '<button type="button" class="slirn-pipe-status-collapse" data-action="pipe-status-collapse" title="收起状态条（在流程配置头部按钮恢复显示）">▾</button>'
      + '</div>'
      + (last3 ? '<div class="slirn-pipe-status-log">' + last3 + '</div>' : '')
      + (st.error ? '<div class="slirn-pipe-status-err">' + escapeHtml(st.error) + '</div>' : '');
    // 重渲染时重新绑拖动事件（innerHTML 替换后旧 listener 不在了）
    _bindStatusDrag(box);
  }

  // ---- 入口 ----
  function loadPanel(taskId) {
    // REQ-20260930-092：切任务重置模板态与自动保存防抖（防上个任务的
    // 套用标记/待写定时器串到新任务）
    _tplAppliedValue = null;
    if (_autoSaveTimer) { clearTimeout(_autoSaveTimer); _autoSaveTimer = null; }
    postJSON(SLIRN_API + '/pipeline_get', {task_id: taskId}).then(function(r) {
      if (!r || !r.ok) {
        toast('流程配置读取失败：' + (r && r.error || '未知错误'), 'error');
        return;
      }
      // REQ-20260918-049：透传 history 给 renderPanel（智能按钮文案 + 兜底徽章）
      renderPanel(taskId, {
        config: r.config,
        updated_at: r.updated_at,
        history: r.history || []
      });
      // 状态条 + 阶段徽章回显
      if (r.status && r.status.state === 'running') {
        showStatus(taskId);
        // running 时立刻画一次徽章（不等下一轮 poll）
        updateStageBadges(r.status);
      } else if (r.status) {
        // idle 态但内存有 job（如刚 stop 完）→ 用 last status 画徽章
        updateStageBadges(r.status);
      } else {
        // 没内存 job 但有 history → 用 history 最后一条做兜底
        var last = (r.history || []).slice(-1)[0];
        if (last) {
          updateStageBadges({
            state: 'idle',
            current_stage: null,
            log: [],
            summary: last
          });
        }
      }
      // REQ-20260920-082：loadPanel 完成时触发默认 BGM 列表加载。
      // 修复 REQ-20260920-078 遗留 bug：fineDefaultBgmLoad 函数存在但从未被调用，
      // 导致下拉只有「— 不选 —」一个 option。
      if (typeof window.fineDefaultBgmLoad === 'function') {
        try { window.fineDefaultBgmLoad(); } catch (e) { console.warn('[bgm-load]', e); }
      }
      // REQ-20260921-NNN：填充 fine_cut 阶段的背景音乐 + 设置参数模板下拉
      // （hasFcJson：服务端 has_fc_json，控制「当前参数」是否可用）
      try { _pipePanelPopulateDeps(taskId, !!r.has_fc_json); } catch (e) { console.warn('[pipe-deps-load]', e); }
      // REQ-20260920-084：检查是否有 in-flight export job，有则挂回进度条
      // （页面刷新 / 服务重启后自动恢复进度显示）
      try {
        fetch(SLIRN_API + '/active_export_for_task?task_id=' + encodeURIComponent(taskId))
          .then(function(r) { return r.json(); })
          .then(function(j) {
            if (!j || !j.ok || !j.job) return;
            if (j.job.state !== 'queued' && j.job.state !== 'running') return;
            var btn = document.getElementById('slirn-fine-export-btn');
            if (btn && typeof startFineExportInline === 'function') {
              startFineExportInline(taskId, j.job.job_id, btn);
              if (j.job.source === 'disk' && j.job.warning) {
                toast('⚠ ' + j.job.warning, 'warn');
              }
            }
          })
          .catch(function(e) { console.warn('[active_export_for_task]', e); });
      } catch (e) { /* 静默：不影响主流程 */ }
    });
  }
  function saveConfig(taskId) {
    var cfg = readCurrentConfig();
    if (!cfg) { toast('未读取到配置', 'error'); return; }
    postJSON(SLIRN_API + '/pipeline_save', {task_id: taskId, config: cfg}).then(function(r) {
      if (r && r.ok) {
        toast(r.toast || '⚙️ 已保存');
        var panel = document.getElementById('slirn-pipe-panel');
        if (panel) {
          // REQ-20260930-092：后端 pipeline_save 返回的是 saved_at（此前误读
          // updated_at → 「最近保存」时间戳从不更新）；模板下拉按当前配置重匹配，
          // 而非一律强制「自定义」（恰好等于某模板时保持回显）
          var sel = document.getElementById('slirn-pipe-template');
          if (sel) sel.value = _matchTemplateValue(cfg);
          _syncTplDelBtn();
          var u = panel.querySelector('.slirn-pipe-updated');
          if (u && (r.saved_at || r.updated_at)) {
            u.textContent = '最近保存：' + (r.saved_at || r.updated_at);
          }
        }
      } else {
        toast('保存失败：' + (r && r.error || '未知错误'), 'error');
      }
    });
  }
  function runPipeline(taskId, since) {
    var cfg = readCurrentConfig();
    if (!cfg) { toast('未读取到配置', 'error'); return; }
    // 运行前先保存（确保服务端拿到的是表单最新值）
    postJSON(SLIRN_API + '/pipeline_save', {task_id: taskId, config: cfg}).then(function(s) {
      if (!s || !s.ok) {
        toast('保存失败，无法启动：' + (s && s.error || '未知错误'), 'error');
        return;
      }
      var payload = {task_id: taskId};
      if (since) payload.since = since;
      postJSON(SLIRN_API + '/pipeline_run', payload).then(function(r) {
        // REQ-20260921-NNN：精剪合成预检失败 → 服务端返回 started=False + preflight 详情
        // 必须在「!r.ok 早退」之前检查，否则前端只会显示 toast「启动失败：未知错误」
        // （服务端 _ok(ok=False) 没带 error 字段），预检详情被吞掉。
        if (r && r.preflight && !r.preflight.ok) {
          var pf = r.preflight || {};
          var matMiss = (pf.materials && pf.materials.missing) || [];
          var par = pf.parameters || {};
          var detail = [];
          if (par.reason) detail.push('· 参数：' + par.reason);
          if (matMiss.length) {
            detail.push('· 素材：缺少 ' + matMiss.map(function(m){return m.label;}).join('、'));
          }
          toast((r.toast || '精剪合成预检失败') + (detail.length ? '\n\n' + detail.join('\n') : ''),
                'error');
          return;
        }
        if (!r || !r.ok) { toast('启动失败：' + (r && r.error || '未知错误'), 'error'); return; }
        if (!r.started) {
          toast(r.toast || '已在运行');
          return;
        }
        var msg = since ? ('▶ 已从「' + (STAGE_LABELS[since] || since) + '」开始') : '▶ 已启动';
        toast(r.toast || msg);
        showStatus(taskId);
      });
    });
  }
  function stopPipeline(taskId) {
    postJSON(SLIRN_API + '/pipeline_stop', {task_id: taskId}).then(function(r) {
      if (r && r.ok) {
        toast(r.toast || '⏹ 已请求停止');
      } else {
        toast('停止失败：' + (r && r.error || '未知错误'), 'error');
      }
    });
  }

  // ---- REQ-20260921-NNN：清理所有阶段产物（两次确认）----
  function resetStages(taskId) {
    if (!taskId) { toast('缺少 task_id', 'error'); return; }
    // 第一次确认：解释会删哪些东西
    var firstConfirm = window.confirm(
      '🧹 确认要清理本任务所有阶段的生成产物吗？\n\n'
      + '会删除以下产物：\n'
      + '• 字幕生成：subtitle.json\n'
      + '• 字幕修订：revision.json + rev_speaker_link.json\n'
      + '• 切分修剪：cutlist.json + cut_speaker_link.json\n'
      + '• 粗剪合成：rough_compose.mp4\n'
      + '• 优化字幕：optimize_subtitle.json（+ 旧流程 fine_revision.json）\n'
      + '• 精剪合成：fine_compose.json + fine_export*.mp4 + fine_preview*.mp4\n'
      + '• 内存中的「已完成阶段」标记 + 执行历史 + 任务状态回退到 DRAFT\n\n'
      + '会保留：原视频 / 时间截取 / 热词 / 任务 metadata。'
    );
    if (!firstConfirm) return;
    // 第二次确认：最后一道
    var secondConfirm = window.confirm(
      '⚠ 最后确认：此操作不可撤销。\n\n'
      + '真的要清理全部阶段产物吗？\n'
      + '（点取消则保留，下次跑流程会从上次完成的地方继续）'
    );
    if (!secondConfirm) return;
    // 真的发了
    postJSON(SLIRN_API + '/pipeline_reset_stages', {task_id: taskId}).then(function(r) {
      if (r && r.ok) {
        toast(r.toast || ('🧹 已清理 ' + (r.deleted || []).length + ' 个产物'));
        // 刷新面板：让 stages_done / history 全部重新加载（清空后）
        loadPanel(taskId);
        // REQ-20260921-NNN：服务端把 t.status 也降回 DRAFT 后，工作台顶部
        // 「阶段」tab 的绿色对号（_wb_stage_states 看 t.status + 磁盘产物）
        // 必须整页重渲才消失。loadPanel 只刷流程配置面板，不刷 wb —— 显式
        // 触发 openWorkbench 重拉整张 wb HTML（router.js: window.slirnOpenWorkbench）。
        if (typeof window.slirnOpenWorkbench === 'function') {
          try { window.slirnOpenWorkbench(taskId); } catch (e) {}
        }
      } else {
        toast('清理失败：' + (r && r.error || '未知错误'), 'error');
      }
    });
  }

  // ---- 全局 click 委托 ----
  // v4：每阶段 summary 内有「▶ 从本阶段开始」按钮（data-action="pipe-run-since"），
  // 不放 data-pipe-action，因此不会被 closest('button[data-pipe-action]') 早退抢走。
  document.addEventListener('click', function(ev) {
    var t = ev.target;
    if (!t || !t.getAttribute) return;
    // label / input / select / option / textarea 内部点击 → 早退（让浏览器原生行为生效）
    if (t.closest && t.closest('label,input,select,option,textarea,button[data-pipe-action]')) {
      return;
    }
    // details summary（折叠/展开）→ 浏览器原生处理
    if (t.tagName === 'SUMMARY') return;

    var tid = t.getAttribute('data-task-id') || curTaskId();
    var action = t.getAttribute('data-action');
    if (!action) return;
    if (action === 'pipe-save') { ev.preventDefault(); saveConfig(tid); return; }
    // REQ-20260930-092：删除当前选中的用户流程模板（仅删库，不动面板/任务配置）
    if (action === 'pipe-flow-tpl-del') {
      ev.preventDefault();
      var tplSel = document.getElementById('slirn-pipe-template');
      var tv = tplSel ? String(tplSel.value || '') : '';
      if (tv.indexOf('fp:') !== 0 || tv === 'fp:_save_as') return;
      var delId = tv.slice(3);
      var delProf = null;
      for (var di = 0; di < _FLOW_TPL_PROFILES.length; di++) {
        if (_FLOW_TPL_PROFILES[di].id === delId) { delProf = _FLOW_TPL_PROFILES[di]; break; }
      }
      if (!delProf) return;
      if (!window.confirm('确认删除流程模板「' + delProf.name + '」？不影响已套用它的任务。')) return;
      postJSON(SLIRN_API + '/delete_flow_profile', {profile_id: delId}).then(function(r) {
        if (r && r.ok) {
          toast(r.toast || '🗑 已删除流程模板');
          _tplAppliedValue = null;
          _populateFlowTemplates('custom');
        } else {
          toast('删除失败：' + (r && r.error || '未知错误'), 'error');
        }
      });
      return;
    }
    if (action === 'pipe-run') {
      ev.preventDefault();
      // REQ-20260918-049：智能按钮 data-since 在按钮上（renderPanel 按 history 自适应生成）
      var since = t.getAttribute('data-since') || null;
      runPipeline(tid, since);
      return;
    }
    if (action === 'pipe-run-since') {
      ev.preventDefault();
      var since = t.getAttribute('data-since') || null;
      runPipeline(tid, since);
      return;
    }
    if (action === 'pipe-stop') { ev.preventDefault(); stopPipeline(tid); return; }
    if (action === 'pipe-reset-stages') { ev.preventDefault(); resetStages(tid); return; }
    if (action === 'pipe-open') { ev.preventDefault(); loadPanel(tid); return; }
    if (action === 'pipe-status-collapse') {
      ev.preventDefault();
      hideStatus();
      return;
    }
    if (action === 'pipe-status-show') {
      ev.preventDefault();
      // 重新显示状态条：恢复保存的位置并立即拉一次状态
      var box = document.getElementById('slirn-pipe-status');
      if (!box) return;
      box.hidden = false;
      _restoreStatusPos(box);
      _bindStatusDrag(box);
      refreshStatusToggleBtn();
      // 立即拉一次 + 起轮询
      var t = box.getAttribute('data-task-id') || tid;
      if (t) {
        if (pipeStatusTimer) { clearInterval(pipeStatusTimer); pipeStatusTimer = null; }
        pipeStatusTimer = setInterval(function() { pollStatus(t); }, 1500);
        pollStatus(t);
      }
      return;
    }
    // REQ-20260921-NNN-outputs-browser：刷新产物列表
    if (action === 'pipe-outputs-refresh') {
      ev.preventDefault();
      refreshOutputsList();
      return;
    }
    // REQ-20260921-NNN-outputs-browser-v2：按 kind 筛选（chip 行）
    if (action === 'pipe-outputs-filter') {
      ev.preventDefault();
      _applyOutputsFilter(t.getAttribute('data-filter'));
      return;
    }
  });

  // ---- 模板切换（内置/用户/另存为）+ run_mode 切换 + 自动保存 ----
  document.addEventListener('change', function(ev) {
    var t = ev.target;
    if (!t || !t.getAttribute) return;
    if (t.id === 'slirn-pipe-template') {
      var panel = document.getElementById('slirn-pipe-panel');
      if (!panel) return;
      var tplKey = t.value;
      var taskId = panel.getAttribute('data-task-id');
      // REQ-20260930-092：💾 把当前整套面板参数存为命名模板（面板不动，仅存库）
      if (tplKey === 'fp:_save_as') {
        var name = window.prompt('模板名（保存红框内当前全部参数 — 复选框选中/不选中、单选、输入、运行模式 — 任何任务可套用）：', '');
        if (!name || !name.trim()) { _populateFlowTemplates(); return; }
        postJSON(SLIRN_API + '/save_flow_profile', {
          task_id: taskId, name: name.trim(), config: readCurrentConfig()
        }).then(function(r) {
          if (r && r.ok) {
            toast(r.toast || '✅ 模板已保存');
            _populateFlowTemplates('fp:' + (r.profile && r.profile.id));
          } else {
            toast('保存模板失败：' + (r && r.error || '未知错误'), 'error');
            _populateFlowTemplates();
          }
        });
        return;
      }
      // REQ-20260930-092：套用用户模板 → 重渲染面板 + 自动落盘本任务 + 回选该模板
      if (tplKey.indexOf('fp:') === 0) {
        var pid = tplKey.slice(3);
        var prof = null;
        for (var i = 0; i < _FLOW_TPL_PROFILES.length; i++) {
          if (_FLOW_TPL_PROFILES[i].id === pid) { prof = _FLOW_TPL_PROFILES[i]; break; }
        }
        if (!prof) {
          toast('模板不存在（可能已删除），已刷新列表', 'error');
          _populateFlowTemplates();
          return;
        }
        renderPanel(taskId, {config: prof.config, updated_at: '模板 ' + prof.name, _fromTemplate: true});
        toast('已载入流程模板：' + prof.name);
        _tplAppliedValue = 'fp:' + pid;
        _populateFlowTemplates('fp:' + pid);
        _autoSaveNow();  // 套用即落盘（自动保存口径）
        return;
      }
      if (tplKey !== 'custom' && TEMPLATES[tplKey]) {
        var cfg = TEMPLATES[tplKey].config;
        renderPanel(taskId, {config: cfg, updated_at: '模板 ' + TEMPLATES[tplKey].label, _fromTemplate: true});
        toast('已载入模板：' + TEMPLATES[tplKey].label);
        _tplAppliedValue = tplKey;
        _populateFlowTemplates(tplKey);
        _autoSaveNow();
      }
      return;
    }
    // REQ-20260921-NNN：run_mode 切换 — to_end → 禁用 flow-stop；stop_after → 启用
    if (t.id === 'slirn-pipe-run-mode') {
      var flowSel = document.getElementById('slirn-pipe-flow-stop');
      if (!flowSel) return;
      if (t.value === 'to_end') {
        flowSel.value = '';  // 强制选「不停（跑到底）」
        flowSel.disabled = true;
        toast('已切到「一键跑到底」：流程会跑完全部 6 阶段');
      } else {
        flowSel.disabled = false;
        toast('已切到「停在指定阶段」：从下面下拉选择停在哪个阶段');
      }
      _tplAppliedValue = null;  // 用户手改 → 脱离模板态
      var rs = document.getElementById('slirn-pipe-template');
      if (rs) rs.value = 'custom';
      _syncTplDelBtn();
      _autoSaveSchedule();
      return;
    }
    // REQ-20260930-092：自动保存 — 面板内任何 input/select/textarea 改动即存。
    // 模板下拉在上面分支已全部 return，不会进这里；程序性改 DOM 不触发 change，
    // 不会自触发循环。手改 → 清套用标记、下拉回「自定义」。
    if (t.closest && t.closest('#slirn-pipe-panel') &&
        t.matches && t.matches('input, select, textarea')) {
      _tplAppliedValue = null;
      var asSel = document.getElementById('slirn-pipe-template');
      if (asSel) asSel.value = 'custom';
      _syncTplDelBtn();
      _autoSaveSchedule();
    }
  });
  // 文本类参数（热词类别 / 删除说话人 / 导出起点 / 时长等）：input 事件 + 防抖，
  // 不等失焦才存
  document.addEventListener('input', function(ev) {
    var t = ev.target;
    if (!t || !t.closest || !t.matches) return;
    if (t.closest('#slirn-pipe-panel') &&
        t.matches('input[type="text"], textarea')) {
      _tplAppliedValue = null;
      var tiSel = document.getElementById('slirn-pipe-template');
      if (tiSel) tiSel.value = 'custom';
      _syncTplDelBtn();
      _autoSaveSchedule();
    }
  });

  // ---- 切任务时清空旧状态条 ----
  window.addEventListener('slirn:before-task-switch', function() {
    hideStatus();
  });

  // ---- 工作台加载时自动渲染面板 ----
  function tryMount(taskId) {
    var panel = document.getElementById('slirn-pipe-panel');
    if (!panel) return false;
    var cur = panel.getAttribute('data-task-id') || '';
    if (cur === taskId && panel.innerHTML.trim()) return true;
    panel.setAttribute('data-task-id', taskId);
    loadPanel(taskId);
    return true;
  }
  window.slirnPipelineMount = tryMount;

  // 兜底：监听 DOM 变化，捕获路由异步插入的情况
  var observer = new MutationObserver(function() {
    var panel = document.getElementById('slirn-pipe-panel');
    if (!panel) return;
    var tid = panel.getAttribute('data-task-id') || '';
    if (!tid) return;
    if (!panel.innerHTML.trim()) tryMount(tid);
  });
  if (document.body) {
    observer.observe(document.body, {childList: true, subtree: true});
  } else {
    document.addEventListener('DOMContentLoaded', function() {
      observer.observe(document.body, {childList: true, subtree: true});
    });
  }

  window.slirnPipelineHideStatus = hideStatus;
  // REQ-20260918-049：把徽章更新函数暴露到 window（E2E 测试用，可手动触发刷新）
  window.slirnPipelineUpdateStageBadges = updateStageBadges;
})();