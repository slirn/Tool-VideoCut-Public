/* REQ-20261022-146 — 硬件按键与系统功能关联。
 *
 * 目标：让硬件按键（宏键盘 / MIDI 控制器 / 打击垫）触发系统已有功能
 *      （播放暂停、保留、删除、切分、上一条/下一条…）。
 *
 * 设计要点（见 design/DESIGN-20261022-146）：
 *   硬件键 → 查映射 → 得到 actionId → 取该动作**当前**的键盘键位
 *         → 在 document 上派发**合成 KeyboardEvent**
 *         → 既有快捷键 handler 原样执行（动作语义/冲突/可见性全部复用）
 *   因此：用户改绑键盘键位后，硬件键自动跟随；新增动作自动可绑。
 *
 * 限制（UI 与文档中如实说明）：
 *   合成事件 isTrusted=false → 不能驱动浏览器**原生**播放器控件（<video> 的
 *   空格/方向键）。硬件键作用于**应用自有快捷键**（修订/切分列表等）。
 *
 * 设备分型：
 *   · USB 键盘型宏键盘 → 浏览器直接当键盘，**无需本模块**（已能用）
 *   · MIDI 控制器      → 本模块（Web MIDI）
 *   · Stream Deck 专有协议 → 需专用解码器（本期不做）
 */
(function () {
  'use strict';
  if (window.slirnHardwareKeys) return;

  var STORE_KEY = 'slirnHwKeyMap';
  var ACCESS = null;          // Web MIDI access 对象
  var devices = [];           // [{id, name, manufacturer}]
  var recTarget = null;       // 录制中的 actionId
  var listeners = [];         // 解除监听用

  // ---------- 存储 ----------

  function loadMap() {
    try {
      var raw = JSON.parse(localStorage.getItem(STORE_KEY) || '{}');
      return (raw && typeof raw === 'object') ? raw : {};
    } catch (e) { return {}; }
  }

  function saveMap(map) {
    try { localStorage.setItem(STORE_KEY, JSON.stringify(map || {})); } catch (e) {}
  }

  // ---------- 消息解析（纯函数，便于单测） ----------

  /**
   * 解析一条 MIDI 消息 → {kind:'note'|'cc', num, pressed} 或 null。
   * · note-on (0x90) 且力度 > 0 → 按下
   * · note-on 力度 0 → 等同 note-off（部分设备这样发）
   * · note-off (0x80) → 抬起（不触发）
   * · CC (0xB0) → 视为按下
   */
  function parseMidi(data) {
    if (!data || data.length < 2) return null;
    var status = data[0] & 0xf0;
    var num = data[1] & 0x7f;
    if (status === 0x90) {
      var vel = data.length > 2 ? (data[2] & 0x7f) : 0;
      if (vel === 0) return { kind: 'note', num: num, pressed: false };
      return { kind: 'note', num: num, pressed: true };
    }
    if (status === 0x80) return { kind: 'note', num: num, pressed: false };
    if (status === 0xb0) return { kind: 'cc', num: num, pressed: true };
    return null;
  }

  function hwKeyId(kind, num) { return kind + ':' + num; }

  function mapKey(deviceId, kind, num) { return deviceId + '|' + hwKeyId(kind, num); }

  // ---------- 动作表（与 router.js 的 REV_KEY_ACTIONS 对应） ----------

  function getActions() {
    // router.js 里的动作表在闭包内，这里按 id 兜底（名字用于 UI 展示）
    var fallback = [
      { id: 'prev', name: '上一条' },
      { id: 'next', name: '下一条' },
      { id: 'play', name: '播放 / 暂停' },
      { id: 'replay', name: '重播本行' },
      { id: 'keep', name: '保留（标记后自动下一条）' },
      { id: 'del', name: '删除（标记后自动下一条）' },
      { id: 'split', name: '切分（展开详情并聚焦内容）' },
      { id: 'esc', name: '退出说明输入框' },
      { id: 'stop', name: '停止（优化字幕）' }
    ];
    if (window.slirnRevKeyActions && window.slirnRevKeyActions.length) {
      return window.slirnRevKeyActions;
    }
    return fallback;
  }

  // 动作 → 当前键盘键位（读 router.js 的键位表；改绑后自动跟随）
  function actionKey(actionId) {
    if (window.slirnRevKeyMap && window.slirnRevKeyMap[actionId]) {
      return window.slirnRevKeyMap[actionId];
    }
    return null;
  }

  // ---------- 合成事件桥接 ----------

  function isEditableFocused() {
    var ae = document.activeElement;
    if (!ae) return false;
    var tag = ae.tagName;
    return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || !!ae.isContentEditable;
  }

  /**
   * 派发合成 keydown，触发应用既有快捷键。
   * 返回是否真的派发了。
   */
  function dispatchAction(actionId) {
    var key = actionKey(actionId);
    if (!key) return false;
    if (isEditableFocused()) return false;      // 与真实键盘一致：输入框中不劫持
    try {
      var ev = new KeyboardEvent('keydown', {
        key: key,
        bubbles: true,
        cancelable: true
      });
      document.dispatchEvent(ev);
      return true;
    } catch (e) {
      return false;
    }
  }

  // ---------- 设备接入 ----------

  function supported() {
    return typeof navigator !== 'undefined' && !!navigator.requestMIDIAccess;
  }

  function refreshDevices() {
    devices = [];
    if (!ACCESS || !ACCESS.inputs) return devices;
    ACCESS.inputs.forEach(function (input) {
      devices.push({
        id: input.id || input.name || 'midi-input',
        name: input.name || 'MIDI 设备',
        manufacturer: input.manufacturer || ''
      });
    });
    return devices;
  }

  function bindInput(input) {
    var onMsg = function (ev) {
      var data = ev && ev.data;
      var parsed = parseMidi(data);
      if (!parsed || !parsed.pressed) return;
      handlePressed(input.id || input.name || 'midi-input', parsed);
    };
    input.onmidimessage = onMsg;
    listeners.push({ input: input, handler: onMsg });
  }

  /** 硬件按下 → 录制态写入映射 / 正常态派发合成事件 */
  function handlePressed(deviceId, parsed) {
    var hwId = hwKeyId(parsed.kind, parsed.num);
    if (recTarget) {
      var map = loadMap();
      var dev = map[deviceId] || (map[deviceId] = {});
      // 同一动作的其它设备绑定保持不变；同一硬件键改绑到新动作时从旧动作移除
      Object.keys(map).forEach(function (did) {
        Object.keys(map[did]).forEach(function (k) {
          if (map[did][k] === recTarget && !(did === deviceId && k === hwId)) {
            delete map[did][k];
          }
        });
      });
      dev[hwId] = recTarget;
      saveMap(map);
      recTarget = null;
      notify();
      return;
    }
    var map2 = loadMap();
    var dev2 = map2[deviceId];
    if (!dev2) return;
    var actionId = dev2[hwId];
    if (!actionId) return;
    dispatchAction(actionId);
  }

  function connect() {
    if (!supported()) {
      return Promise.reject(new Error('当前浏览器不支持 Web MIDI（需 Chrome / Edge）'));
    }
    return navigator.requestMIDIAccess().then(function (access) {
      ACCESS = access;
      refreshDevices();
      listeners.forEach(function (l) {
        try { l.input.onmidimessage = null; } catch (e) {}
      });
      listeners = [];
      (access.inputs || []).forEach(bindInput);
      access.onstatechange = function () {
        refreshDevices();
        (access.inputs || []).forEach(bindInput);
        notify();
      };
      notify();
      return devices;
    });
  }

  // ---------- 绑定管理 ----------

  function setBinding(deviceId, hwId, actionId) {
    var map = loadMap();
    var dev = map[deviceId] || (map[deviceId] = {});
    dev[hwId] = actionId;
    saveMap(map);
    notify();
  }

  function clearAction(actionId) {
    var map = loadMap();
    Object.keys(map).forEach(function (did) {
      Object.keys(map[did]).forEach(function (k) {
        if (map[did][k] === actionId) delete map[did][k];
      });
      if (!Object.keys(map[did]).length) delete map[did];
    });
    saveMap(map);
    notify();
  }

  function bindingsOf(actionId) {
    var map = loadMap();
    var out = [];
    Object.keys(map).forEach(function (did) {
      Object.keys(map[did]).forEach(function (k) {
        if (map[did][k] === actionId) out.push({ deviceId: did, hwId: k });
      });
    });
    return out;
  }

  function hwLabel(hwId) {
    var parts = hwId.split(':');
    if (parts[0] === 'note') return '音符 ' + parts[1];
    if (parts[0] === 'cc') return '控制器 ' + parts[1];
    return hwId;
  }

  function deviceName(deviceId) {
    for (var i = 0; i < devices.length; i++) {
      if (devices[i].id === deviceId) return devices[i].name;
    }
    return deviceId;
  }

  // ---------- UI ----------

  var notify = function () {};
  function setNotifier(fn) { notify = fn || function () {}; }

  function renderInto(el) {
    if (!el) return;
    // 绑定/清除后自刷新（setBinding / clearAction 会调 notify）
    setNotifier(function () { renderInto(el); });
    var map = loadMap();
    var actions = getActions();
    var html = '<div class="slirn-hw-title">🎹 硬件按键'
      + '<span class="slirn-hw-sub">把硬件按键绑定到系统功能（播放/切分/删除…）</span></div>';

    if (!supported()) {
      html += '<div class="slirn-hw-note">当前浏览器不支持 Web MIDI（需 Chrome / Edge）。'
        + '若你的设备是 <b>USB 键盘型宏键盘</b>，无需任何配置 —— 浏览器会把它当键盘，'
        + '直接在下面「点击键帽 → 按新键」绑定即可。</div>';
    } else {
      var connected = devices.length > 0;
      html += '<div class="slirn-hw-bar">'
        + '<button class="slirn-btn slirn-btn-sm" data-hw-action="connect">🎹 连接设备</button>'
        + '<span class="slirn-hw-status">' + (connected
          ? '已连接：' + devices.map(function (d) { return d.name; }).join('、')
          : '未连接') + '</span></div>';
    }

    html += '<div class="slirn-hw-rows">';
    actions.forEach(function (a) {
      var bs = bindingsOf(a.id);
      var text = bs.length
        ? bs.map(function (b) { return deviceName(b.deviceId) + ' · ' + hwLabel(b.hwId); }).join('、')
        : '未绑定';
      html += '<div class="slirn-hw-row"><span class="slirn-hw-name">' + a.name + '</span>'
        + '<span class="slirn-hw-key">' + (recTarget === a.id ? '按下硬件键…' : text) + '</span>'
        + '<button class="slirn-btn-mini" data-hw-action="bind" data-hw-target="' + a.id + '">绑定</button>'
        + '<button class="slirn-btn-mini" data-hw-action="clear" data-hw-target="' + a.id + '">清除</button>'
        + '</div>';
    });
    html += '</div>';

    html += '<div class="slirn-hw-note">· 硬件键作用于<b>应用快捷键</b>（修订/切分列表的操作），'
      + '不驱动浏览器原生播放器控件。<br>'
      + '· 设备授权由浏览器管理；清理站点数据后需重新授权。<br>'
      + '· USB 键盘型设备无需配置，直接用上方键位绑定即可。</div>';

    el.innerHTML = html;

    el.querySelectorAll('[data-hw-action]').forEach(function (btn) {
      btn.addEventListener('click', function (ev) {
        ev.stopPropagation();
        var act = btn.getAttribute('data-hw-action');
        var target = btn.getAttribute('data-hw-target');
        if (act === 'connect') {
          connect().then(function () { renderInto(el); })
            .catch(function (e) {
              if (window.slirnToast) window.slirnToast('连接失败：' + e.message, 'error');
            });
        } else if (act === 'bind') {
          if (!supported()) {
            if (window.slirnToast) window.slirnToast('当前浏览器不支持 Web MIDI', 'error');
            return;
          }
          recTarget = target;
          renderInto(el);
        } else if (act === 'clear') {
          clearAction(target);
          recTarget = null;
          renderInto(el);
        }
      });
    });
  }

  // ---------- 对外接口 ----------

  window.slirnHardwareKeys = {
    supported: supported,
    connect: connect,
    renderInto: renderInto,
    loadMap: loadMap,
    saveMap: saveMap,
    setBinding: setBinding,
    clearAction: clearAction,
    bindingsOf: bindingsOf,
    parseMidi: parseMidi,
    dispatchAction: dispatchAction,
    hwKeyId: hwKeyId,
    devices: function () { return devices; },
    setNotifier: setNotifier,
    get recording() { return recTarget; },
    set recording(v) { recTarget = v; }
  };
})();
