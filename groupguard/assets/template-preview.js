import { renderMessageMarkdown, safeResourceUrl } from './message-markdown.js';

const SAMPLE_VALUES = {
  user_id: '10001', userid: '10001', raw_user_id: '10001', rawuserid: '10001',
  target_id: '10002', member_id: '10002', username: '示例用户', nickname: '示例用户',
  count: '3', failed: '0', minutes: '10', seconds: '60', limit: '10',
  reason: '触发群管理规则', error: '请求失败，请稍后重试', word: '示例词',
  names: '<@10002>', remaining: '10 分钟', action_text: '已撤回并禁言 10 分钟',
  punish: '撤回并禁言', retry_count: '1', failure_limit: '3', retry_text: '请稍后重试',
  a: '2', b: '3', op: '+', option: '5', option_index: '0', verify_id: 'preview',
  request_count: '1', request_rows: '1. 示例用户 · 等待审批', next_page: '', next_cursor: '',
  audit_count: '1', audit_rows: '刚刚 · 自动监管 · 处理成功', days: '30',
  member_count: '2', member_rows: '示例成员 · 10 分钟后解除', overflow: '',
  word_count: '2', word_rows: '1. 示例词\n2. 示例词条', entry_count: '1', entry_rows: '示例成员 · 生效中',
  index: '1', time: '刚刚', state: '成功', affected_count: '1', trace_short: 'preview',
  scope_text: '当前群', failed_text: '', failed_content: '', decision_text: '拒绝',
  global_mode: '关闭', display: '示例成员', expire_at: '10 分钟后', is_admin: '已授权',
  is_full_access: '已开启', allow_proactive_msg: '已开启',
};

export function substitutePreviewVariables(value, variables) {
  // Literal {{braces}} remain literal; unknown variables remain visible and editable.
  return String(value ?? '').replace(/\{\{/g, '\u0000').replace(/\}\}/g, '\u0001')
    .replace(/\{([a-zA-Z_]\w*)\}/g, (token, key) => Object.hasOwn(variables, key) ? String(variables[key]) : token)
    .replace(/\u0000/g, '{').replace(/\u0001/g, '}');
}

export function normalizePreviewButtons(buttons) {
  if (buttons == null) return [];
  if (!Array.isArray(buttons)) throw new Error('按钮应为 JSON 数组，修正后将显示按钮预览');
  const rows = buttons.every(Array.isArray) ? buttons : Array.from({ length: Math.ceil(buttons.length / 3) }, (_, i) => buttons.slice(i * 3, i * 3 + 3));
  if (rows.some(row => row.some(button => !button || typeof button !== 'object' || Array.isArray(button)))) {
    throw new Error('每个按钮应为包含 text / data / link 的对象');
  }
  return rows.filter(row => row.length);
}

function sampleValue(key) {
  if (Object.hasOwn(SAMPLE_VALUES, key)) return SAMPLE_VALUES[key];
  if (/_mark$/.test(key)) return '✅';
  if (/_count$/.test(key)) return '3';
  if (/_switch_(state|short)$/.test(key)) return '已开启';
  return `{${key}}`;
}

export function createTemplatePreview({ readTemplate, getBot, getGroup, variableGroups }) {
  const $ = id => document.getElementById(id);
  const overrides = Object.create(null);
  const descriptions = new Map(variableGroups.flatMap(group => group.items.map(([key, label]) => [key, label])));
  let mode = 'markdown';
  let signature = '';
  let rawDraft = false;
  let lastFocus = null;
  let copyTimer;

  function context(template) {
    const bot = getBot() || {}, group = getGroup() || {};
    const serialized = JSON.stringify(template);
    const keys = [...new Set(['user_id', 'username', 'target_id', 'group_id', 'bot_name', ...Array.from(serialized.matchAll(/(?<!\{)\{([a-zA-Z_]\w*)\}(?!\})/g), match => match[1])])];
    const values = Object.fromEntries(keys.map(key => [key, sampleValue(key)]));
    Object.assign(values, {
      group_id: group.group_id || '20001', groupid: group.group_id || '20001',
      bot_name: bot.name || 'Elaina', botname: bot.name || 'Elaina',
      appid: bot.appid || '102134274', botid: bot.appid || '102134274',
      bot_qq: bot.robot_qq || '10000', botqq: bot.robot_qq || '10000', selfid: bot.robot_qq || '10000',
    }, overrides);
    return { bot, keys, values };
  }

  function renderVariables(keys, values) {
    const next = keys.join('|');
    if (signature === next) return;
    signature = next;
    const fragment = document.createDocumentFragment();
    for (const key of keys) {
      const label = document.createElement('label');
      label.title = descriptions.get(key) || '模板变量';
      const code = document.createElement('code');
      code.textContent = `{${key}}`;
      const input = document.createElement('input');
      input.value = values[key];
      input.dataset.previewVariable = key;
      input.setAttribute('aria-label', `预览变量 ${key}`);
      label.append(code, input);
      fragment.append(label);
    }
    $('tpl-preview-variables').replaceChildren(fragment);
  }

  function renderButtons(template, values) {
    let buttons = template.buttons;
    if (template.button_mode === 'verify_options' && Array.isArray(buttons) && buttons[0]) {
      buttons = ['5', '4', '6', '1'].map((option, index) => ({ ...buttons[0], __previewOption: { option, option_index: String(index) } }));
    }
    const rows = normalizePreviewButtons(buttons);
    const fragment = document.createDocumentFragment();
    for (const row of rows) {
      const line = document.createElement('div');
      line.className = 'tpl-keyboard-row';
      for (const item of row) {
        const render = item.render_data || {}, action = item.action || {};
        const vars = { ...values, ...item.__previewOption };
        const button = document.createElement('button');
        button.type = 'button';
        const linked = item.link || action.type === 0;
        button.textContent = substitutePreviewVariables(item.text || render.label || render.visited_label || '未命名按钮', vars) + (linked ? ' ↗' : '');
        button.classList.toggle('primary', Number(item.style ?? render.style) === 1);
        button.title = `${substitutePreviewVariables(item.link || item.data || action.data || '', vars)} · 仅预览`;
        line.append(button);
      }
      fragment.append(line);
    }
    $('tpl-preview-keyboard').replaceChildren(fragment);
    $('tpl-preview-keyboard').hidden = mode === 'text' || !rows.length;
    $('tpl-preview-keyboard').classList.toggle('small-buttons', !!template.small_buttons);
  }

  function refresh({ raw = rawDraft, variables = true } = {}) {
    rawDraft = raw;
    let template;
    try {
      template = raw ? JSON.parse($('tpl-raw').value) : readTemplate();
      if (!template || typeof template !== 'object' || Array.isArray(template)) throw new Error('请先选择有效模板');
    } catch (error) {
      $('tpl-preview-warning').textContent = error.message;
      $('tpl-preview-warning').hidden = false;
      $('tpl-preview-content').textContent = '修正模板格式后将恢复预览';
      $('tpl-preview-keyboard').hidden = true;
      return;
    }
    const { bot, keys, values } = context(template);
    if (variables) renderVariables(keys, values);
    $('tpl-preview-name').textContent = bot.name || 'Elaina';
    $('tpl-preview-date').textContent = template.label || '消息预览';
    const avatar = $('tpl-preview-avatar');
    const src = safeResourceUrl(bot.avatar);
    avatar.hidden = !src;
    if (src && avatar.getAttribute('src') !== src) avatar.src = src;
    $('tpl-preview-avatar-fallback').hidden = !!src;
    let content = substitutePreviewVariables(template.content, values);
    if (template.at_user !== false) content = `<@${values.user_id}> ${content}`;
    const node = $('tpl-preview-content');
    if (!content.trim()) {
      node.textContent = '在编辑区输入内容，这里会同步显示消息效果';
      node.className = 'tpl-empty-content';
    } else if (mode === 'text') {
      node.textContent = content;
      node.className = 'tpl-plain-text';
    } else {
      node.innerHTML = renderMessageMarkdown(content, { [values.user_id]: values.username || '示例用户', [values.target_id]: '示例成员' });
      node.className = 'message-markdown';
    }
    let warning = '';
    try { renderButtons(template, values); } catch (error) {
      warning = error.message;
      $('tpl-preview-keyboard').hidden = true;
    }
    $('tpl-preview-warning').textContent = warning;
    $('tpl-preview-warning').hidden = !warning;
    $('tpl-preview-caption').textContent = `${raw ? '完整 JSON 草稿 · 应用到表单后可保存' : '实时预览当前表单'}${mode === 'text' ? ' · 纯文本模式不显示按钮' : ''}。动态数据使用示例变量，效果以实际 QQ 客户端为准。`;
  }

  const reset = () => { signature = ''; refresh({ raw: false }); };
  for (const id of ['tpl-content', 'tpl-buttons', 'tpl-label', 'tpl-msg-type', 'tpl-at-user', 'tpl-small-buttons']) {
    $(id).addEventListener('input', event => { if (!event.isComposing) refresh({ raw: false }); });
    $(id).addEventListener('change', () => refresh({ raw: false }));
  }
  $('tpl-raw').addEventListener('input', () => refresh({ raw: true }));
  $('tpl-preview-variables').addEventListener('input', event => {
    if (!event.target.dataset.previewVariable) return;
    overrides[event.target.dataset.previewVariable] = event.target.value;
    refresh({ variables: false });
  });
  $('tpl-preview-reset').addEventListener('click', () => {
    for (const key of Object.keys(overrides)) delete overrides[key];
    signature = ''; refresh();
  });
  for (const button of document.querySelectorAll('[data-preview-mode]')) button.addEventListener('click', () => {
    mode = button.dataset.previewMode;
    document.querySelectorAll('[data-preview-mode]').forEach(item => item.setAttribute('aria-pressed', String(item === button)));
    refresh();
  });
  $('tpl-preview-avatar').addEventListener('error', () => {
    $('tpl-preview-avatar').hidden = true;
    $('tpl-preview-avatar-fallback').hidden = false;
  });

  function openImage(url) {
    const src = safeResourceUrl(url);
    if (!src) return;
    lastFocus = document.activeElement;
    $('tpl-preview-image').src = src;
    $('tpl-preview-image-dialog').showModal();
  }
  function closeImage() {
    $('tpl-preview-image-dialog').close();
    $('tpl-preview-image').removeAttribute('src');
    lastFocus?.focus({ preventScroll: true });
  }
  $('tpl-preview-image-close').addEventListener('click', closeImage);
  $('tpl-preview-image-dialog').addEventListener('click', event => { if (event.target === event.currentTarget) closeImage(); });
  $('tpl-preview-image-dialog').addEventListener('cancel', event => { event.preventDefault(); closeImage(); });
  $('tpl-preview-content').addEventListener('click', async event => {
    const target = event.target.closest('[data-copy-code], [data-image-src], img.md-image');
    if (!target) return;
    if (target.matches('[data-copy-code]')) {
      try {
        await navigator.clipboard.writeText(target.closest('.md-code-block').querySelector('code').textContent);
        target.textContent = '已复制';
      } catch { target.textContent = '复制失败'; }
      clearTimeout(copyTimer);
      copyTimer = setTimeout(() => { target.textContent = '复制代码'; }, 1800);
    } else if (target.dataset.imageSrc) {
      const src = safeResourceUrl(target.dataset.imageSrc);
      if (!src) return;
      const image = document.createElement('img');
      Object.assign(image, { src, alt: target.dataset.imageAlt || '消息图片', className: 'md-image', referrerPolicy: 'no-referrer', tabIndex: 0 });
      image.setAttribute('role', 'button');
      image.setAttribute('aria-label', `预览图片：${image.alt}`);
      target.replaceWith(image);
      image.focus({ preventScroll: true });
    } else openImage(target.src);
  });
  $('tpl-preview-content').addEventListener('keydown', event => {
    if (event.target.matches('img.md-image') && ['Enter', ' '].includes(event.key)) {
      event.preventDefault(); openImage(event.target.src);
    }
  });
  $('tpl-preview-content').addEventListener('error', event => {
    if (!(event.target instanceof HTMLImageElement)) return;
    const fallback = document.createElement('span');
    fallback.className = 'md-image-error';
    fallback.textContent = `图片加载失败 · ${event.target.alt || '消息图片'}`;
    event.target.replaceWith(fallback);
  }, true);
  return { refresh: reset };
}
