'use strict';

(() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const demo = new URLSearchParams(location.search).get('demo') === '1';
  const state = { session: null, services: [], clients: [], settings: {}, view: 'services' };
  const kinds = { cloudflare: 'Cloudflare', ssh: 'SSH 服务器', alist: 'Alist 文件' };
  const content = $('#content');
  const modal = $('#modal');

  function node(tag, attrs = {}, ...children) {
    const element = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value === undefined || value === null) continue;
      if (key === 'class') element.className = value;
      else if (key === 'text') element.textContent = value;
      else if (key.startsWith('on') && typeof value === 'function') element.addEventListener(key.slice(2), value);
      else if (['value', 'checked', 'selected', 'disabled', 'hidden', 'required', 'readOnly', 'multiple', 'spellcheck'].includes(key)) element[key] = value;
      else element.setAttribute(key, value);
    }
    for (const child of children.flat()) {
      if (child !== undefined && child !== null) element.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return element;
  }

  function button(label, action, style = 'secondary', attrs = {}) {
    return node('button', { type: 'button', class: `button ${style}`, onclick: action, ...attrs }, label);
  }

  function toast(message, style = 'success') {
    const item = node('div', { class: `toast ${style}`, role: style === 'error' ? 'alert' : 'status' }, message);
    $('#toast-region').append(item);
    setTimeout(() => item.remove(), 6000);
  }

  function demoNotice() {
    toast('这是界面演示。没有连接实际服务，也不会保存任何内容。', 'info');
  }

  function message(error) {
    return error instanceof Error ? error.message : '操作失败，请稍后重试。';
  }

  async function api(path, options = {}) {
    if (demo) throw new Error('演示模式不发送 API 请求。');
    const headers = { Accept: 'application/json', ...(options.headers || {}) };
    if (options.body !== undefined) headers['Content-Type'] = 'application/json';
    if (options.method && !['GET', 'HEAD'].includes(options.method)) {
      if (state.session?.csrf_token) headers['X-CSRF-Token'] = state.session.csrf_token;
    }
    let response;
    try {
      response = await fetch(path, { ...options, headers, credentials: 'same-origin',
        body: options.body === undefined ? undefined : JSON.stringify(options.body) });
    } catch {
      throw new Error('无法连接 NAS，请检查网络后重试。');
    }
    let data = {};
    if (response.status !== 204) {
      try { data = await response.json(); } catch { /* Handle below without exposing response HTML. */ }
    }
    if (!response.ok) {
      if (response.status === 401 && path !== '/api/login') {
        state.session = null;
        showAuth(false);
      }
      const detail = data.message || data.error || data.detail;
      throw new Error(typeof detail === 'string' ? detail : `请求失败（${response.status}），请重试。`);
    }
    return data;
  }

  async function busy(control, action) {
    if (control.disabled) return;
    control.disabled = true;
    control.setAttribute('aria-busy', 'true');
    try { await action(); }
    catch (error) { toast(message(error), 'error'); }
    finally { control.disabled = false; control.removeAttribute('aria-busy'); }
  }

  async function copy(text, control) {
    try {
      if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(text);
      else {
        const field = node('textarea', { value: text, class: 'clipboard-helper', 'aria-label': '复制内容' });
        (modal.open ? modal : document.body).append(field);
        field.select();
        const copied = document.execCommand('copy');
        field.remove();
        if (!copied) throw new Error('请手动选中并复制内容。');
      }
      const previous = control.textContent;
      control.textContent = '已复制';
      setTimeout(() => { if (control.isConnected) control.textContent = previous; }, 1600);
    } catch { toast('复制未完成，请手动选中内容复制。', 'error'); }
  }

  function endpoint() {
    const base = String(state.settings.public_url || location.origin).replace(/\/+$/, '');
    return base.endsWith('/mcp') ? base : `${base}/mcp`;
  }

  function prettyDate(value) {
    if (!value) return '尚未使用';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).format(date);
  }

  function showAuth(changePassword = false) {
    if (modal.open) modal.close();
    $('#initial-loading').hidden = true;
    $('#app-shell').hidden = true;
    $('#auth-screen').hidden = false;
    $('#login-form').hidden = changePassword;
    $('#password-form').hidden = !changePassword;
    $('#auth-title').textContent = changePassword ? '设置你的管理密码' : '连接你的服务';
    $('#auth-description').textContent = changePassword ? '首次使用，请先替换初始密码，再进入私人工作区。' : '登录私人管理后台，管理服务连接与客户端授权。';
    $('#first-login-note').hidden = changePassword;
    const input = changePassword ? $('#password-form input') : $('#login-password');
    input.focus();
  }

  async function loadWorkspace() {
    const [services, clients, settings] = await Promise.all([
      api('/api/services'), api('/api/clients'), api('/api/settings'),
    ]);
    state.services = services.services || [];
    state.clients = (clients.clients || []).filter(client => !client.revoked_at);
    state.settings = settings;
    showWorkspace();
  }

  function showWorkspace() {
    $('#initial-loading').hidden = true;
    $('#auth-screen').hidden = true;
    $('#app-shell').hidden = false;
    $('#demo-banner').hidden = !demo;
    $('#demo-chip').hidden = !demo;
    setView(state.view);
  }

  function setView(view) {
    state.view = ['services', 'clients', 'settings'].includes(view) ? view : 'services';
    for (const item of document.querySelectorAll('[data-view]')) {
      item.classList.toggle('active', item.dataset.view === state.view);
      if (item.dataset.view === state.view) item.setAttribute('aria-current', 'page');
      else item.removeAttribute('aria-current');
    }
    const titles = { services: '服务连接', clients: '客户端授权', settings: '访问设置' };
    $('#current-view-label').textContent = titles[state.view];
    document.title = `${titles[state.view]} · 我的 NAS MCP`;
    content.replaceChildren();
    if (state.view === 'services') renderServices();
    if (state.view === 'clients') renderClients();
    if (state.view === 'settings') renderSettings();
  }

  function pageHeading(title, description, action) {
    return node('div', { class: 'page-heading' }, node('div', {}, node('p', { class: 'eyebrow' }, 'YOUR PRIVATE CONNECTIONS'), node('h1', {}, title), node('p', {}, description)), action);
  }

  function stat(label, value, note) {
    return node('div', { class: 'stat-card' }, node('span', { class: 'stat-label' }, label), node('strong', { class: 'stat-number' }, value), node('span', { class: 'stat-note' }, note));
  }

  function meta(label, value) {
    return node('div', { class: 'meta-row' }, node('span', {}, label), node('strong', {}, value || '未设置'));
  }

  function renderServices() {
    content.append(pageHeading('所有服务，一处连接', '配置一次，之后在新对话中直接调用你的服务。', button('＋ 添加服务', () => serviceDialog(), 'primary')));
    content.append(node('div', { class: 'quick-add-services', 'aria-label': '按类型添加服务' },
      button('＋ 添加 Cloudflare', () => serviceDialog(null, 'cloudflare'), 'secondary'),
      button('＋ 添加 SSH', () => serviceDialog(null, 'ssh'), 'secondary'),
      button('＋ 添加 Alist', () => serviceDialog(null, 'alist'), 'secondary'),
    ));
    content.append(node('div', { class: 'stats-grid' },
      stat('已配置服务', state.services.length, '统一保存在这台 NAS'),
      stat('连接已验证', state.services.filter(item => item.last_test?.ok).length, '以最近一次测试结果为准'),
      stat('客户端密钥', state.clients.length, '分别授权，随时撤销'),
    ));
    content.append(node('div', { class: 'section-heading' }, node('div', {}, node('h2', {}, '我的连接'), node('p', {}, 'Cloudflare、服务器与文件服务')), node('span', { class: 'badge neutral' }, `${state.services.length} 个服务`)));
    if (!state.services.length) {
      content.append(node('div', { class: 'empty-state' }, node('div', { class: 'empty-icon', 'aria-hidden': 'true' }, '＋'), node('h3', {}, '添加你的第一个服务'), node('p', {}, '填入连接信息和凭据，然后让 AI 通过 MCP 使用它。'), button('添加服务', () => serviceDialog(), 'primary')));
      return;
    }
    const grid = node('div', { class: 'service-grid' });
    for (const service of state.services) {
      const cfg = service.config || {};
      const creds = service.credentials || {};
      const test = service.last_test;
      const status = !test ? ['neutral', '待检测'] : test.ok ? ['success', '连接正常'] : ['danger', '连接异常'];
      const icon = service.kind === 'cloudflare' ? 'CF' : service.kind === 'ssh' ? '>_' : 'A';
      const details = node('div', { class: 'service-meta' });
      if (service.kind === 'cloudflare') {
        details.append(meta('认证方式', creds.token ? 'API Token · 已保存' : '尚未配置 Token'), meta('可用操作', '域名与 DNS 查询'));
      } else if (service.kind === 'ssh') {
        const authMethod = cfg.auth_method || (creds.private_key ? 'key' : 'password');
        const authLabel = authMethod === 'key' ? (creds.private_key ? 'SSH 密钥 · 已保存' : 'SSH 密钥 · 未配置') : (creds.password ? '账号密码 · 已保存' : '账号密码 · 未配置');
        details.append(meta('服务器', `${cfg.host || '未设置'}:${cfg.port || 22}`), meta('用户名', cfg.username), meta('认证方式', authLabel), meta('预设检查', `${Object.keys(cfg.checks || {}).length} 项`));
      } else {
        details.append(meta('服务地址', cfg.url), meta('用户名', cfg.username), meta('认证方式', creds.password ? '账号密码 · 已保存' : '账号密码 · 未配置'), meta('访问目录', cfg.root_path || '/'));
      }
      const testButton = button('测试连接', event => {
        if (demo) return demoNotice();
        busy(event.currentTarget, async () => {
          const base = `/api/services/${encodeURIComponent(service.kind)}/${encodeURIComponent(service.alias)}`;
          const result = await api(`${base}/test`, { method: 'POST', body: {} });
          service.last_test = { ...result, at: new Date().toISOString() };
          toast(result.message || (result.ok ? '连接测试通过。' : '连接测试失败。'), result.ok ? 'success' : 'error');
          setView('services');
        });
      }, 'secondary small');
      const card = node('article', { class: 'service-card' },
        node('div', { class: 'service-head' }, node('span', { class: `service-icon ${service.kind}`, 'aria-hidden': 'true' }, icon), node('div', { class: 'service-title' }, node('h3', {}, kinds[service.kind] || service.kind), node('span', { class: 'service-alias' }, service.alias)), node('span', { class: `badge ${status[0]}` }, status[1])),
        node('p', { class: 'service-description' }, service.description || '已保存的私人服务连接'), details,
        node('div', { class: 'service-foot' }, node('span', { class: 'test-time', title: test?.message || '' }, test ? `上次检测 ${prettyDate(test.at)}` : '添加后可测试连接'), node('div', { class: 'card-actions' }, testButton, button('编辑', () => serviceDialog(service), 'ghost small'), button('删除', () => deleteService(service), 'ghost danger small'))),
      );
      grid.append(card);
    }
    content.append(grid, node('div', { class: 'notice info connection-tip' }, node('strong', {}, '下一步：授权你的 AI 客户端'), node('span', {}, '在「客户端授权」中创建专用密钥，保存 MCP 地址后即可复用连接。'), button('去授权 →', () => setView('clients'), 'ghost small')));
  }

  function openModal(title, body, footer) {
    if (modal.open) modal.close();
    modal.replaceChildren(node('div', { class: 'modal-header' }, node('div', {}, node('p', { class: 'eyebrow' }, 'PRIVATE MCP'), node('h2', { id: 'modal-title' }, title)), button('×', () => modal.close(), 'ghost icon modal-close', { 'aria-label': '关闭弹窗' })), node('div', { class: 'modal-body' }, body), footer ? node('div', { class: 'modal-footer' }, footer) : null);
    modal.showModal();
  }

  function field(label, control, hint) {
    return node('label', { class: 'field' }, node('span', { class: 'field-label' }, label), control, hint ? node('span', { class: 'hint' }, hint) : null);
  }

  function textInput(name, value = '', attrs = {}) {
    return node('input', { name, type: 'text', value, ...attrs });
  }

  function secretInput(label, name, present, multiline = false, hint = '') {
    const attrs = { name, autocomplete: 'off', spellcheck: false, disabled: demo,
      placeholder: demo ? '演示模式不录入真实凭据' : present ? '已保存，留空保留原值' : `输入${label}` };
    const control = multiline ? node('textarea', { ...attrs, rows: name === 'private_key' ? 5 : 3 }) : node('input', { ...attrs, type: 'password' });
    return field(`${label}${present ? ' · 已保存' : ''}`, control, hint || (present ? '已保存的凭据不会显示；填写新值可替换。' : '凭据仅用于此服务，不会返回给 AI 客户端。'));
  }

  function serviceDialog(service = null, initialKind = 'cloudflare') {
    const form = node('form', { id: 'service-form' });
    const type = node('select', { name: 'kind', disabled: Boolean(service) }, Object.entries(kinds).map(([value, label]) => node('option', { value, selected: value === (service?.kind || initialKind) }, label)));
    const alias = textInput('alias', service?.alias || '', { required: true, disabled: Boolean(service), pattern: '[a-zA-Z0-9_-]{1,64}', maxlength: 64, placeholder: '例如 home-nas' });
    const description = textInput('description', service?.description || '', { maxlength: 200, placeholder: '例如家里的 NAS、个人域名' });
    if (demo) form.append(node('div', { class: 'notice info' }, '演示模式：所有配置项都可以查看，密码字段仅作展示，不录入或保存真实凭据。'));
    const typeButtons = [];
    if (!service) {
      const choices = node('div', { class: 'service-type-choices', 'aria-label': '选择服务类型' });
      for (const [value, label, detail] of [['cloudflare', 'Cloudflare', 'API Token'], ['ssh', 'SSH 服务器', '用户名 + 密码 / 密钥'], ['alist', 'Alist', '账号密码登录']]) {
        const choice = button('', () => { type.value = value; renderFields(); }, 'secondary service-type-choice', { 'aria-pressed': type.value === value ? 'true' : 'false' });
        choice.append(node('strong', {}, label), node('span', {}, detail));
        typeButtons.push([choice, value]);
        choices.append(choice);
      }
      form.append(choices);
    }
    form.append(node('div', { class: 'field-grid' }, field('服务类型', type), field('连接别名', alias, '使用字母、数字、短横线或下划线。')), field('描述', description));
    const dynamic = node('div', { class: 'service-fields' });
    form.append(dynamic);
    let controls = {};

    function renderFields() {
      for (const [choice, value] of typeButtons) {
        choice.classList.toggle('active', type.value === value);
        choice.setAttribute('aria-pressed', type.value === value ? 'true' : 'false');
      }
      dynamic.replaceChildren(node('div', { class: 'section-divider' }, type.value === 'alist' ? 'Alist · 账号密码登录' : type.value === 'ssh' ? 'SSH · 服务器登录信息' : 'Cloudflare · API 认证'));
      const cfg = service?.config || {};
      const creds = service?.credentials || {};
      controls = {};
      if (type.value === 'cloudflare') {
        dynamic.append(secretInput('API Token', 'token', creds.token), node('p', { class: 'hint' }, '为需要查询的域名配置 Zone Read 和 DNS Read 权限。'));
      } else if (type.value === 'ssh') {
        controls.host = textInput('host', cfg.host || '', { required: true, placeholder: '192.168.1.10 或服务器域名' });
        controls.port = textInput('port', cfg.port || 22, { type: 'number', min: 1, max: 65535, required: true });
        controls.username = textInput('username', cfg.username || 'root', { required: true, autocomplete: 'off', placeholder: '例如 root' });
        controls.auth_method = node('select', { name: 'auth_method' }, node('option', { value: 'password' }, '账号密码登录'), node('option', { value: 'key' }, 'SSH 密钥登录'));
        controls.auth_method.value = cfg.auth_method || (creds.private_key ? 'key' : 'password');
        dynamic.append(node('div', { class: 'field-grid' }, field('服务器地址', controls.host), field('SSH 端口', controls.port), field('用户名', controls.username), field('认证方式', controls.auth_method)));
        const keyFields = node('div', {}, secretInput('私钥', 'private_key', creds.private_key, true), secretInput('私钥口令（可选）', 'key_passphrase', creds.key_passphrase));
        const passwordFields = secretInput('SSH 登录密码', 'password', creds.password);
        const syncAuth = () => { keyFields.hidden = controls.auth_method.value !== 'key'; passwordFields.hidden = controls.auth_method.value !== 'password'; };
        controls.auth_method.addEventListener('change', syncAuth);
        syncAuth();
        dynamic.append(keyFields, passwordFields, secretInput('主机公钥（known_hosts）', 'known_hosts', creds.known_hosts, true, '先通过可信渠道核对服务器主机指纹，再粘贴对应的 known_hosts 内容。'));
        controls.checks = node('textarea', { name: 'checks', rows: 5, spellcheck: false, value: Object.entries(cfg.checks || { system: 'uname -a', disk: 'df -h' }).map(([name, command]) => `${name} = ${command}`).join('\n'), placeholder: 'system = uname -a\ndisk = df -h' });
        dynamic.append(field('固定检查命令', controls.checks, '每行一条「名称 = 命令」。AI 只能调用这些名称；请配置你认可的查询命令。'));
      } else {
        controls.url = textInput('url', cfg.url || '', { type: 'url', required: true, placeholder: 'http://192.168.1.10:5244' });
        controls.username = textInput('username', cfg.username || '', { required: true, autocomplete: 'off', placeholder: 'Alist 用户名' });
        controls.root_path = textInput('root_path', cfg.root_path || '/', { required: true, placeholder: '/' });
        dynamic.append(field('Alist 服务地址', controls.url), field('Alist 用户名', controls.username), secretInput('Alist 登录密码', 'password', creds.password), field('允许访问的根目录', controls.root_path), node('p', { class: 'hint' }, '填写平时登录 Alist 的账号和密码，并确保账号可以读取目标目录。当前不支持动态二步验证码登录。'));
      }
    }
    type.addEventListener('change', renderFields);
    renderFields();
    const errorLine = node('p', { class: 'field-error', role: 'alert', hidden: true });
    form.append(errorLine);
    const submit = node('button', { class: 'button primary', type: 'submit', form: 'service-form' }, '保存连接');
    form.addEventListener('submit', event => {
      event.preventDefault();
      if (demo) return demoNotice();
      errorLine.hidden = true;
      busy(submit, async () => {
        try {
          const config = {};
          const secrets = {};
          for (const [name, control] of Object.entries(controls)) {
            if (name === 'checks') {
              config.checks = {};
              for (const line of control.value.split('\n').filter(line => line.trim())) {
                const match = line.match(/^\s*([a-zA-Z0-9_-]+)\s*=\s*(.+)$/);
                if (!match) throw new Error('检查命令格式应为「名称 = 命令」，每行一条。');
                if (Object.hasOwn(config.checks, match[1])) throw new Error('检查名称不能重复。');
                config.checks[match[1]] = match[2];
              }
            } else config[name] = name === 'port' ? Number(control.value) : control.value.trim();
          }
          const allowedSecrets = type.value === 'cloudflare' ? ['token'] : type.value === 'alist' ? ['password'] : config.auth_method === 'key' ? ['private_key', 'key_passphrase', 'known_hosts'] : ['password', 'known_hosts'];
          for (const name of allowedSecrets) {
            const input = form.elements.namedItem(name);
            if (input && input.value) secrets[name] = input.value;
          }
          await api('/api/services', { method: 'POST', body: { kind: type.value, alias: alias.value.trim(), description: description.value.trim(), config, secrets } });
          for (const name of allowedSecrets) { const input = form.elements.namedItem(name); if (input) input.value = ''; }
          modal.close();
          const data = await api('/api/services');
          state.services = data.services || [];
          setView('services');
          toast('服务连接已保存。可以测试连接了。');
        } catch (error) { errorLine.textContent = message(error); errorLine.hidden = false; }
      });
    });
    openModal(service ? `编辑 ${service.alias}` : '添加服务连接', form, [button('取消', () => modal.close()), submit]);
  }

  function confirmDialog(title, description, action, label = '确认删除') {
    const confirm = button(label, event => {
      if (demo) return demoNotice();
      busy(event.currentTarget, async () => { await action(); modal.close(); });
    }, 'danger');
    openModal(title, node('p', {}, description), [button('取消', () => modal.close()), confirm]);
  }

  function deleteService(service) {
    confirmDialog('删除服务连接', `删除「${service.alias}」后，AI 将无法再通过此连接访问它。远端服务本身不会被删除。`, async () => {
      await api(`/api/services/${encodeURIComponent(service.kind)}/${encodeURIComponent(service.alias)}`, { method: 'DELETE' });
      state.services = state.services.filter(item => !(item.kind === service.kind && item.alias === service.alias));
      setView('services');
      toast('服务连接已删除。');
    });
  }

  function renderClients() {
    content.append(pageHeading('把连接带进每次对话', '为每个 AI 客户端创建独立密钥，之后可以单独撤销。', button('＋ 创建客户端密钥', clientDialog, 'primary')));
    const endpointValue = endpoint();
    const copyEndpoint = button('复制地址', event => copy(endpointValue, event.currentTarget), 'secondary small');
    content.append(node('section', { class: 'panel' }, node('div', { class: 'panel-heading' }, node('div', {}, node('h2', {}, 'MCP 连接地址'), node('p', {}, '支持 Streamable HTTP 的客户端可使用此地址。')), copyEndpoint), node('div', { class: 'panel-body' }, node('code', { class: 'code-block endpoint-value' }, endpointValue), node('p', { class: 'hint' }, '使用客户端专用 Bearer Token 连接；管理后台密码不能作为客户端密钥。'))));
    const list = node('div', { class: 'client-list' });
    for (const client of state.clients) {
      list.append(node('div', { class: 'client-row' }, node('div', { class: 'client-icon', 'aria-hidden': 'true' }, '⌘'), node('div', { class: 'client-details' }, node('h3', {}, client.name), node('p', {}, `创建于 ${prettyDate(client.created_at)} · 最近使用 ${prettyDate(client.last_used)}`)), node('span', { class: 'badge success' }, '已授权'), button('撤销', () => confirmDialog('撤销客户端授权', `撤销「${client.name}」的密钥后，该客户端将无法继续调用你的 MCP。`, async () => {
        await api(`/api/clients/${encodeURIComponent(client.id)}`, { method: 'DELETE' });
        state.clients = state.clients.filter(item => item.id !== client.id);
        setView('clients');
        toast('客户端授权已撤销。');
      }, '撤销授权'), 'ghost danger small')));
    }
    if (!state.clients.length) list.append(node('div', { class: 'empty-state compact' }, node('h3', {}, '还没有客户端授权'), node('p', {}, '创建一个密钥，让你的桌面 AI 连接到 NAS。')));
    content.append(node('section', { class: 'panel' }, node('div', { class: 'panel-heading' }, node('h2', {}, '已授权客户端'), node('span', { class: 'badge neutral' }, `${state.clients.length} 个`)), list));
    const snippet = `[mcp_servers.my_nas]\nurl = ${JSON.stringify(endpointValue)}\nbearer_token_env_var = "NAS_MCP_TOKEN"`;
    content.append(node('section', { class: 'panel' }, node('div', { class: 'panel-heading' }, node('div', {}, node('h2', {}, '桌面客户端配置示例'), node('p', {}, 'Codex 可使用以下配置；将 NAS_MCP_TOKEN 设置为创建的客户端密钥。')), button('复制配置', event => copy(snippet, event.currentTarget), 'secondary small')), node('div', { class: 'panel-body' }, node('pre', { class: 'code-block' }, snippet), node('p', { class: 'hint' }, '网页 ChatGPT 的连接方式不同，需要在「访问设置」配置兼容的 OAuth 身份提供方。'))));
  }

  function clientDialog() {
    const form = node('form', { id: 'client-form' });
    const name = textInput('name', '', { required: true, maxlength: 100, placeholder: '例如我的 Mac · Codex' });
    form.append(field('客户端名称', name, '取一个容易辨认的名称，方便以后撤销授权。'));
    const submit = node('button', { type: 'submit', form: 'client-form', class: 'button primary' }, '生成密钥');
    form.addEventListener('submit', event => {
      event.preventDefault();
      if (demo) return demoNotice();
      busy(submit, async () => {
        const client = await api('/api/clients', { method: 'POST', body: { name: name.value.trim() } });
        state.clients.push({ id: client.id, name: client.name, created_at: new Date().toISOString(), last_used: null });
        setView('clients');
        const token = String(client.token || '');
        const body = node('div', {}, node('div', { class: 'notice info' }, '密钥仅在这里显示一次。关闭前请复制并保存到客户端的凭据配置中。'), field('客户端密钥', node('textarea', { class: 'token-box', readonly: true, value: token, rows: 3, spellcheck: false }), '不要把此密钥发进普通聊天消息。'), button('复制密钥', event => copy(token, event.currentTarget), 'primary'), node('div', { class: 'section-divider' }, '连接方式'), meta('MCP 地址', endpoint()), node('p', { class: 'hint' }, '传输类型选择 Streamable HTTP，认证选择 Bearer Token。将此密钥保存一次，以后复用该连接。'));
        openModal('客户端密钥已生成', body, button('我已保存，关闭', () => modal.close(), 'secondary'));
      });
    });
    openModal('创建客户端密钥', form, [button('取消', () => modal.close()), submit]);
  }

  function lines(value) { return value.split(/[\n,]/).map(item => item.trim()).filter(Boolean); }

  function renderSettings() {
    content.append(pageHeading('设置访问入口', '连接地址、允许的主机名，以及网页客户端需要的 OAuth 配置。'));
    const settings = state.settings;
    const oauth = settings.oauth || {};
    const form = node('form', { id: 'settings-form' });
    const url = textInput('public_url', settings.public_url || '', { type: 'url', placeholder: 'https://mcp.example.com' });
    const hosts = node('textarea', { name: 'allowed_hosts', rows: 3, value: (settings.allowed_hosts || []).join('\n'), placeholder: 'mcp.example.com\n192.168.1.10', spellcheck: false });
    form.append(node('section', { class: 'panel' }, node('div', { class: 'panel-heading' }, node('div', {}, node('h2', {}, '入口地址'), node('p', {}, '内网访问可留空，自动使用当前地址；公网填写已配置 HTTPS 的域名。'))), node('div', { class: 'panel-body' }, field('对外连接地址', url, '内网可留空；自定义对外地址请使用 HTTPS，例如 https://mcp.example.com。'), field('允许的 Host', hosts, '每行一个域名或 IP，不包含协议、路径或端口。'))));
    const enabled = node('input', { type: 'checkbox', name: 'oauth_enabled', checked: Boolean(settings.oauth) });
    const oauthFields = node('div', { class: 'oauth-fields', hidden: !settings.oauth });
    enabled.addEventListener('change', () => { oauthFields.hidden = !enabled.checked; });
    const controls = {
      issuer_url: textInput('issuer_url', oauth.issuer_url || '', { type: 'url', placeholder: 'https://auth.example.com/realms/nas' }),
      resource_server_url: textInput('resource_server_url', oauth.resource_server_url || endpoint(), { type: 'url', placeholder: 'https://mcp.example.com/mcp' }),
      audience: textInput('audience', oauth.audience || '', { placeholder: '身份提供方配置的 API audience' }),
      jwks_url: textInput('jwks_url', oauth.jwks_url || '', { type: 'url', placeholder: 'https://auth.example.com/.../certs' }),
      allowed_subjects: node('textarea', { name: 'allowed_subjects', rows: 2, value: (oauth.allowed_subjects || []).join('\n'), spellcheck: false, placeholder: '你的身份提供方用户 subject ID' }),
      required_scopes: textInput('required_scopes', (oauth.required_scopes || []).join(', '), { placeholder: '例如 mcp:read' }),
    };
    oauthFields.append(node('div', { class: 'notice info' }, '此功能需要已配置好的外部 OAuth 身份提供方。打开开关不会自动完成网页登录或创建授权服务。'), node('div', { class: 'field-grid' }, field('Issuer URL', controls.issuer_url), field('MCP 资源地址', controls.resource_server_url), field('Audience', controls.audience), field('JWKS URL', controls.jwks_url)), field('允许的用户 Subject', controls.allowed_subjects, '每行一个，只允许列出的身份访问。'), field('必需的 Scopes', controls.required_scopes, '用逗号分隔，必须与身份提供方授予的权限一致。'));
    form.append(node('section', { class: 'panel' }, node('div', { class: 'panel-heading' }, node('div', {}, node('h2', {}, 'OAuth 连接'), node('p', {}, '供需要 OAuth 登录的网页或其他客户端使用。'))), node('div', { class: 'panel-body' }, node('label', { class: 'check-line' }, enabled, node('span', {}, node('strong', {}, '启用外部 OAuth 验证'), node('span', { class: 'hint' }, '已有 Bearer Token 的桌面客户端仍可继续使用。'))), oauthFields)));
    const submit = node('button', { class: 'button primary', type: 'submit' }, '保存访问设置');
    const resultLine = node('div', { class: 'notice success', role: 'status', hidden: true });
    form.append(resultLine, node('div', { class: 'actions' }, node('span', { class: 'hint' }, '部分访问设置需要重启容器后生效。'), submit));
    form.addEventListener('submit', event => {
      event.preventDefault();
      if (demo) return demoNotice();
      busy(submit, async () => {
        let oauthPayload = null;
        if (enabled.checked) {
          for (const name of ['issuer_url', 'resource_server_url', 'audience', 'jwks_url']) {
            if (!controls[name].value.trim()) throw new Error(`请填写 OAuth 的 ${name}。`);
          }
          oauthPayload = Object.fromEntries(Object.entries(controls).map(([name, control]) => [name, ['allowed_subjects', 'required_scopes'].includes(name) ? lines(control.value) : control.value.trim()]));
        }
        const payload = { public_url: url.value.trim(), allowed_hosts: lines(hosts.value), oauth: oauthPayload };
        const result = await api('/api/settings', { method: 'PUT', body: payload });
        state.settings = payload;
        resultLine.textContent = result.restart_required ? '设置已保存。请在 NAS 的 Compose 管理中重启容器，使访问设置生效。' : '访问设置已保存。';
        resultLine.hidden = false;
        toast('访问设置已保存。');
      });
    });
    content.append(form);
  }

  function passwordDialog() {
    if (demo) return demoNotice();
    const form = node('form', { id: 'change-password-form' });
    const current = node('input', { type: 'password', autocomplete: 'current-password', required: true });
    const next = node('input', { type: 'password', autocomplete: 'new-password', minlength: 12, required: true });
    const confirm = node('input', { type: 'password', autocomplete: 'new-password', minlength: 12, required: true });
    form.append(field('当前密码', current), field('新管理密码', next, '至少 12 个字符。'), field('再次输入新密码', confirm));
    const submit = node('button', { type: 'submit', form: 'change-password-form', class: 'button primary' }, '更新密码');
    form.addEventListener('submit', event => {
      event.preventDefault();
      busy(submit, async () => {
        if (next.value !== confirm.value) throw new Error('两次输入的新密码不一致。');
        state.session = await api('/api/password', { method: 'POST', body: { current_password: current.value, new_password: next.value } });
        form.reset();
        modal.close();
        toast('管理密码已更新。');
      });
    });
    openModal('修改管理密码', form, [button('取消', () => modal.close()), submit]);
  }

  $('#login-form').addEventListener('submit', event => {
    event.preventDefault();
    if (demo) return demoNotice();
    const form = event.currentTarget;
    const errorLine = $('#login-error');
    errorLine.hidden = true;
    busy($('button', form), async () => {
      try {
        state.session = await api('/api/login', { method: 'POST', body: { password: $('#login-password').value } });
        form.reset();
        if (state.session.must_change_password) showAuth(true);
        else await loadWorkspace();
      } catch (error) { errorLine.textContent = message(error); errorLine.hidden = false; }
    });
  });

  $('#password-form').addEventListener('submit', event => {
    event.preventDefault();
    const form = event.currentTarget;
    const errorLine = $('#password-error');
    errorLine.hidden = true;
    busy($('button', form), async () => {
      try {
        if (form.elements.new_password.value !== form.elements.confirm_password.value) throw new Error('两次输入的新密码不一致。');
        state.session = await api('/api/password', { method: 'POST', body: { current_password: form.elements.current_password.value, new_password: form.elements.new_password.value } });
        form.reset();
        await loadWorkspace();
      } catch (error) { errorLine.textContent = message(error); errorLine.hidden = false; }
    });
  });

  for (const item of document.querySelectorAll('[data-view]')) item.addEventListener('click', () => setView(item.dataset.view));
  $('.brand').addEventListener('click', event => { event.preventDefault(); setView('services'); });
  $('#change-password').addEventListener('click', passwordDialog);
  $('#logout').addEventListener('click', event => {
    if (demo) return demoNotice();
    busy(event.currentTarget, async () => {
      await api('/api/logout', { method: 'POST', body: {} });
      state.session = null;
      state.services = [];
      state.clients = [];
      state.settings = {};
      showAuth(false);
    });
  });
  modal.addEventListener('click', event => { if (event.target === modal) { const rect = modal.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) modal.close(); } });
  modal.addEventListener('close', () => { if (!modal.open) modal.replaceChildren(); });

  function loadDemo() {
    state.session = { authenticated: true, must_change_password: false, csrf_token: '' };
    state.settings = { public_url: 'https://mcp.example.com', allowed_hosts: ['mcp.example.com'], oauth: null };
    state.clients = [
      { id: 'demo-mac', name: '我的 Mac · Codex', created_at: '2026-10-24T08:00:00Z', last_used: '2026-10-25T07:42:00Z' },
      { id: 'demo-laptop', name: '工作笔记本', created_at: '2026-10-24T09:00:00Z', last_used: null },
    ];
    state.services = [
      { kind: 'cloudflare', alias: 'personal-cloud', description: '个人域名与 DNS 解析', config: {}, credentials: { token: true }, last_test: { ok: true, message: '示例连接状态', at: '2026-10-25T07:40:00Z' } },
      { kind: 'ssh', alias: 'home-nas', description: '家里的飞牛 NAS · 使用 root 账号密码登录', config: { host: '192.0.2.10', port: 22, username: 'root', auth_method: 'password', checks: { system: 'uname -a', disk: 'df -h', containers: 'docker ps' } }, credentials: { password: true, known_hosts: true }, last_test: { ok: true, message: '示例连接状态', at: '2026-10-25T07:38:00Z' } },
      { kind: 'alist', alias: 'family-files', description: '家庭文件与归档目录', config: { url: 'https://alist.example.com', username: 'mcp-reader', root_path: '/' }, credentials: { password: true }, last_test: null },
    ];
    showWorkspace();
  }

  async function start() {
    if (demo) return loadDemo();
    try {
      state.session = await api('/api/session');
      if (!state.session.authenticated) showAuth(false);
      else if (state.session.must_change_password) showAuth(true);
      else await loadWorkspace();
    } catch (error) {
      showAuth(false);
      $('#login-error').textContent = message(error);
      $('#login-error').hidden = false;
    }
  }
  start();
})();
