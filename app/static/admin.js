'use strict';

(() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const demo = new URLSearchParams(location.search).get('demo') === '1';
  const state = { session: null, services: [], clients: [], settings: {}, view: 'services' };
  const kinds = { cloudflare: 'Cloudflare', ssh: 'SSH', alist: 'Alist', github: 'GitHub', docker: '镜像仓库' };
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
    toast('这是凭据库界面演示，不读取或保存任何真实凭据。', 'info');
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
    $('#auth-title').textContent = changePassword ? '设置你的管理密码' : '私人凭据库';
    $('#auth-description').textContent = changePassword ? '首次使用，请先替换初始密码，再进入私人凭据库。' : '集中保存账号、密码和密钥，按范围授权客户端读取。';
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
    const titles = { services: '凭据管理', clients: '客户端授权', settings: '访问设置' };
    $('#current-view-label').textContent = titles[state.view];
    document.title = `${titles[state.view]} · 私人凭据库`;
    content.replaceChildren();
    if (state.view === 'services') renderServices();
    if (state.view === 'clients') renderClients();
    if (state.view === 'settings') renderSettings();
  }

  function pageHeading(title, description, action) {
    return node('div', { class: 'page-heading' }, node('div', {}, node('p', { class: 'eyebrow' }, 'YOUR PRIVATE CREDENTIALS'), node('h1', {}, title), node('p', {}, description)), action);
  }

  function stat(label, value, note) {
    return node('div', { class: 'stat-card' }, node('span', { class: 'stat-label' }, label), node('strong', { class: 'stat-number' }, value), node('span', { class: 'stat-note' }, note));
  }

  function meta(label, value) {
    return node('div', { class: 'meta-row' }, node('span', {}, label), node('strong', {}, value || '未设置'));
  }

  function renderServices() {
    content.append(pageHeading('账号与密钥，一处保存', '按名称管理凭据，授权你的客户端在需要时通过 MCP 读取。', button('＋ 添加凭据', () => serviceDialog(), 'primary')));
    content.append(node('div', { class: 'quick-add-services', 'aria-label': '按类型添加凭据' },
      button('＋ 添加 Cloudflare', () => serviceDialog(null, 'cloudflare'), 'secondary'),
      button('＋ 添加 SSH', () => serviceDialog(null, 'ssh'), 'secondary'),
      button('＋ 添加 Alist', () => serviceDialog(null, 'alist'), 'secondary'),
      button('＋ 添加 GitHub', () => serviceDialog(null, 'github'), 'secondary'),
      button('＋ 添加 Docker', () => serviceDialog(null, 'docker'), 'secondary'),
    ));
    const secretCount = state.services.reduce((total, item) => total + Object.values(item.credentials || {}).filter(Boolean).length, 0);
    content.append(node('div', { class: 'stats-grid' },
      stat('凭据条目', state.services.length, '按服务类型归类保存'),
      stat('密码与密钥', secretCount, '仅显示是否已保存'),
      stat('已授权客户端', state.clients.length, '按范围读取，随时撤销'),
    ));
    content.append(node('div', { class: 'notice info credential-disclosure' }, node('strong', {}, 'MCP 会返回所选凭据'), node('span', {}, '客户端调用 get_credentials 时，选中的账号、密码或密钥值会返回给该调用客户端。请在「客户端授权」中设置可读取范围。')));
    content.append(node('div', { class: 'section-heading' }, node('div', {}, node('h2', {}, '已保存的凭据'), node('p', {}, 'Cloudflare、SSH、Alist、GitHub 与镜像仓库')), node('span', { class: 'badge neutral' }, `${state.services.length} 个条目`)));
    if (!state.services.length) {
      content.append(node('div', { class: 'empty-state' }, node('div', { class: 'empty-icon', 'aria-hidden': 'true' }, '＋'), node('h3', {}, '添加你的第一条凭据'), node('p', {}, '保存服务地址、账号与密码或密钥，之后按名称读取。'), button('添加凭据', () => serviceDialog(), 'primary')));
      return;
    }
    const grid = node('div', { class: 'service-grid' });
    for (const service of state.services) {
      const cfg = service.config || {};
      const creds = service.credentials || {};
      const verification = service.last_test;
      const validationText = !verification ? '尚未验证' : verification.needs_host_trust ? '等待核对主机指纹' : `${verification.ok ? '最近验证通过' : '最近验证未通过'} · ${prettyDate(verification.at)}`;
      const hasSecret = Object.values(creds).some(Boolean);
      const icon = { cloudflare: 'CF', ssh: '>_', alist: 'A', github: 'GH', docker: 'DK' }[service.kind] || '·';
      const title = service.kind === 'docker' ? (cfg.registry === 'ghcr.io' ? 'GHCR 凭据' : 'Docker Hub 凭据') : (kinds[service.kind] || service.kind);
      const details = node('div', { class: 'service-meta' });
      if (service.kind === 'cloudflare') {
        if (cfg.auth_method === 'global_key') {
          details.append(meta('凭据类型', 'Global API Key'), meta('账号邮箱', cfg.email), meta('总控制密钥', creds.global_api_key ? '已保存' : '未填写'));
        } else details.append(meta('凭据类型', 'API Token'), meta('Token', creds.token ? '已保存' : '未填写'));
      } else if (service.kind === 'ssh') {
        const authMethod = cfg.auth_method || (creds.private_key ? 'key' : 'password');
        details.append(meta('服务器', `${cfg.host || '未设置'}:${cfg.port || 22}`), meta('用户名', cfg.username), meta('凭据类型', authMethod === 'key' ? 'SSH 私钥' : '账号密码'), meta(authMethod === 'key' ? '私钥' : '密码', (authMethod === 'key' ? creds.private_key : creds.password) ? '已保存' : '未填写'));
      } else if (service.kind === 'alist') {
        details.append(meta('服务地址', cfg.url), meta('用户名', cfg.username), meta('登录密码', creds.password ? '已保存' : '未填写'));
      } else if (service.kind === 'github') {
        details.append(meta('GitHub 用户名', cfg.owner), meta('个人访问令牌', creds.token ? 'PAT · 已保存' : 'PAT · 未填写'));
      } else if (service.kind === 'docker') {
        details.append(meta('镜像仓库', cfg.registry === 'ghcr.io' ? 'ghcr.io' : 'docker.io'), meta('登录用户名', cfg.username), meta('命名空间', cfg.namespace), meta('访问 Token', creds.token ? '已保存' : '未填写'));
      }
      grid.append(node('article', { class: 'service-card' },
        node('div', { class: 'service-head' }, node('span', { class: `service-icon ${service.kind}`, 'aria-hidden': 'true' }, icon), node('div', { class: 'service-title' }, node('h3', {}, service.alias), node('span', { class: 'service-alias' }, title)), node('span', { class: `badge ${hasSecret ? 'success' : 'neutral'}` }, hasSecret ? '已保存凭据' : '仅基本信息')),
        node('p', { class: 'service-description' }, service.description || '私人凭据条目'), details,
        node('div', { class: 'service-foot' }, node('div', { class: 'credential-status' }, node('span', { class: 'credential-note' }, `${service.kind}/${service.alias}`), node('span', { class: `verification-note ${verification?.ok ? 'success' : verification ? 'pending' : ''}`, title: verification?.message || '' }, validationText)), node('div', { class: 'card-actions' }, button('测试凭据', event => {
          if (demo) return demoNotice();
          busy(event.currentTarget, () => testCredential(service));
        }, 'secondary small'), button('编辑', () => serviceDialog(service), 'ghost small'), button('删除', () => deleteService(service), 'ghost danger small'))),
      ));
    }
    content.append(grid, node('p', { class: 'hint validation-hint' }, '后台「测试凭据」仅验证认证是否可用，不保证所有业务权限。MCP 仅提供凭据列表和读取。'), node('div', { class: 'notice info connection-tip' }, node('strong', {}, '按客户端设置读取范围'), node('span', {}, '可授权全部凭据，也可以只授权指定的条目。'), button('管理授权 →', () => setView('clients'), 'ghost small')));
  }

  async function testCredential(service, body = {}) {
    if (demo) { demoNotice(); return false; }
    const result = await api(`/api/services/${encodeURIComponent(service.kind)}/${encodeURIComponent(service.alias)}/test`, { method: 'POST', body });
    service.last_test = { ...result, at: result.at || new Date().toISOString() };
    if (state.view === 'services') setView('services');
    if (result.needs_host_trust) {
      if (typeof result.fingerprint !== 'string' || !result.fingerprint) throw new Error('未取得有效的 SSH 主机指纹，请重试。');
      const fingerprint = result.fingerprint;
      const confirm = button('已核对，信任并测试登录', event => {
        busy(event.currentTarget, async () => {
          const complete = await testCredential(service, { trust_fingerprint: fingerprint });
          if (complete && modal.open) modal.close();
        });
      }, 'primary');
      openModal('核对 SSH 主机指纹', node('div', {},
        node('p', {}, result.message || '首次验证这台服务器，需要先核对其 SSH 主机指纹。'),
        meta('服务器', `${service.config?.host || service.alias}:${service.config?.port || 22}`),
        meta('公钥类型', result.key_type || 'SSH'),
        node('pre', { class: 'code-block fingerprint-value' }, fingerprint),
        node('p', { class: 'hint' }, '请通过 NAS 本机控制台或其他可信渠道核对以上指纹。确认一致后，点击按钮保存信任并继续验证登录凭据。'),
      ), [button('取消', () => modal.close()), confirm]);
      return false;
    }
    toast(`${result.message || (result.ok ? '凭据验证通过。' : '凭据验证未通过。')} 仅验证认证可用，不保证全部业务权限。`, result.ok ? 'success' : 'error');
    return true;
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
    return field(`${label}${present ? ' · 已保存' : ''}`, control, hint || (present ? '编辑时不回显原值；留空保留，填写新值替换。授权客户端可通过 MCP 读取。' : '保存后，授权客户端可通过 MCP 获取此凭据值。'));
  }

  function serviceDialog(service = null, initialKind = 'cloudflare') {
    const form = node('form', { id: 'service-form' });
    const type = node('select', { name: 'kind', disabled: Boolean(service) }, Object.entries(kinds).map(([value, label]) => node('option', { value, selected: value === (service?.kind || initialKind) }, label)));
    const alias = textInput('alias', service?.alias || '', { required: true, disabled: Boolean(service), pattern: '(?!.*[\\u115F\\u1160\\u3164\\uFFA0])[\\p{L}\\p{N}][\\p{L}\\p{N}_\\-]{0,63}', placeholder: '例如：家里NAS、香港服务器', title: '名称为 1～64 个字，以文字或数字开头，可含短横线和下划线，不含空格。' });
    const description = textInput('description', service?.description || '', { maxlength: 200, placeholder: '例如：家里飞牛的 root 账号，用于查看服务' });
    if (demo) form.append(node('div', { class: 'notice info' }, '演示模式：可以查看所有配置项，密码和密钥字段仅作展示，不录入或保存真实凭据。'));
    const typeButtons = [];
    if (!service) {
      const choices = node('div', { class: 'service-type-choices', 'aria-label': '选择凭据类型' });
      for (const [value, label, detail] of [['cloudflare', 'Cloudflare', 'Token / 总控制密钥'], ['ssh', 'SSH', '密码 / 私钥'], ['alist', 'Alist', '账号密码'], ['github', 'GitHub', '用户名 + PAT'], ['docker', '镜像仓库', 'Docker Hub / GHCR']]) {
        const choice = button('', () => { type.value = value; renderFields(); }, 'secondary service-type-choice', { 'aria-pressed': type.value === value ? 'true' : 'false' });
        choice.append(node('strong', {}, label), node('span', {}, detail));
        typeButtons.push([choice, value]);
        choices.append(choice);
      }
      form.append(choices);
    }
    form.append(node('div', { class: 'field-grid' }, field('凭据类型', type), field('凭据名称', alias, service ? '名称保存后固定，已有客户端授权会继续使用这个名称。补充说明可在备注中修改。' : '给凭据起个好记的名字，方便你和 AI 查找。支持中文、英文、数字及短横线、下划线；以文字或数字开头，最多 64 个字，不含空格。')), field('备注（选填）', description, '补充用途或说明，不是登录用户名。'));
    const dynamic = node('div', { class: 'service-fields' });
    form.append(dynamic);
    let controls = {};

    function renderFields() {
      for (const [choice, value] of typeButtons) {
        choice.classList.toggle('active', type.value === value);
        choice.setAttribute('aria-pressed', type.value === value ? 'true' : 'false');
      }
      const sectionTitles = { cloudflare: 'Cloudflare · 账号与密钥', ssh: 'SSH · 地址、账号与凭据', alist: 'Alist · 地址与账号密码', github: 'GitHub · 用户名与访问令牌', docker: '镜像仓库 · 账号与访问令牌' };
      dynamic.replaceChildren(node('div', { class: 'section-divider' }, sectionTitles[type.value]));
      const cfg = service?.config || {};
      const creds = service?.credentials || {};
      controls = {};
      if (type.value === 'cloudflare') {
        controls.auth_method = node('select', { name: 'auth_method' }, node('option', { value: 'api_token' }, 'API Token'), node('option', { value: 'global_key' }, 'Global API Key（总控制密钥）'));
        controls.auth_method.value = cfg.auth_method || 'api_token';
        controls.email = textInput('email', cfg.email || '', { type: 'email', autocomplete: 'off', placeholder: 'Cloudflare 账号邮箱' });
        const tokenFields = secretInput('API Token', 'token', creds.token);
        const globalFields = node('div', {}, field('Cloudflare 账号邮箱', controls.email), secretInput('Global API Key（总控制密钥）', 'global_api_key', creds.global_api_key));
        const syncMethod = () => {
          const isGlobal = controls.auth_method.value === 'global_key';
          tokenFields.hidden = isGlobal;
          globalFields.hidden = !isGlobal;
          controls.email.required = isGlobal;
          controls.email.disabled = !isGlobal;
        };
        controls.auth_method.addEventListener('change', syncMethod);
        syncMethod();
        dynamic.append(field('保存哪种凭据', controls.auth_method), tokenFields, globalFields);
      } else if (type.value === 'ssh') {
        controls.host = textInput('host', cfg.host || '', { required: true, placeholder: '192.168.1.10 或服务器域名' });
        controls.port = textInput('port', cfg.port || 22, { type: 'number', min: 1, max: 65535, required: true });
        controls.username = textInput('username', cfg.username || 'root', { required: true, autocomplete: 'off', placeholder: '例如 root' });
        controls.auth_method = node('select', { name: 'auth_method' }, node('option', { value: 'password' }, '账号密码'), node('option', { value: 'key' }, 'SSH 私钥'));
        controls.auth_method.value = cfg.auth_method || (creds.private_key ? 'key' : 'password');
        dynamic.append(node('div', { class: 'field-grid' }, field('服务器地址', controls.host), field('SSH 端口', controls.port), field('用户名', controls.username), field('凭据方式', controls.auth_method)));
        const keyFields = node('div', {}, secretInput('SSH 私钥', 'private_key', creds.private_key, true), secretInput('私钥口令（可选）', 'key_passphrase', creds.key_passphrase));
        const passwordFields = secretInput('SSH 登录密码', 'password', creds.password);
        const syncAuth = () => { keyFields.hidden = controls.auth_method.value !== 'key'; passwordFields.hidden = controls.auth_method.value !== 'password'; };
        controls.auth_method.addEventListener('change', syncAuth);
        syncAuth();
        dynamic.append(keyFields, passwordFields);
      } else if (type.value === 'alist') {
        controls.url = textInput('url', cfg.url || '', { type: 'url', required: true, placeholder: 'http://192.168.1.10:5244' });
        controls.username = textInput('username', cfg.username || '', { required: true, autocomplete: 'off', placeholder: 'Alist 用户名' });
        dynamic.append(field('Alist 服务地址', controls.url), field('Alist 用户名', controls.username), secretInput('Alist 登录密码', 'password', creds.password));
      } else if (type.value === 'github') {
        controls.owner = textInput('owner', cfg.owner || '', { required: true, autocomplete: 'off', placeholder: '例如 example-user' });
        dynamic.append(field('GitHub 用户名', controls.owner), secretInput('个人访问令牌（PAT）', 'token', creds.token));
      } else if (type.value === 'docker') {
        controls.registry = node('select', { name: 'registry' }, node('option', { value: 'docker.io' }, 'Docker Hub · docker.io'), node('option', { value: 'ghcr.io' }, 'GitHub Container Registry · ghcr.io'));
        controls.registry.value = cfg.registry || 'docker.io';
        controls.username = textInput('username', cfg.username || '', { required: true, autocomplete: 'off', placeholder: 'Docker Hub 用户名' });
        controls.namespace = textInput('namespace', cfg.namespace || '', { required: true, placeholder: '例如 example-user 或组织名' });
        controls.registry.addEventListener('change', () => { controls.username.placeholder = controls.registry.value === 'ghcr.io' ? 'GitHub 用户名' : 'Docker Hub 用户名'; });
        controls.username.placeholder = controls.registry.value === 'ghcr.io' ? 'GitHub 用户名' : 'Docker Hub 用户名';
        controls.username.addEventListener('blur', () => { if (!controls.namespace.value.trim()) controls.namespace.value = controls.username.value.trim(); });
        dynamic.append(field('镜像仓库', controls.registry), node('div', { class: 'field-grid' }, field('仓库登录用户名', controls.username), field('命名空间', controls.namespace, '所属的用户名或组织名。')), secretInput('仓库访问 Token', 'token', creds.token));
      }
    }
    type.addEventListener('change', renderFields);
    renderFields();
    const errorLine = node('p', { class: 'field-error', role: 'alert', hidden: true });
    form.append(errorLine, node('p', { class: 'hint' }, 'MCP 取凭据时，会把选中的凭据值返回给有权限的调用客户端。'));
    const submit = node('button', { class: 'button primary', type: 'submit', form: 'service-form' }, '保存凭据');
    form.addEventListener('submit', event => {
      event.preventDefault();
      if (demo) return demoNotice();
      errorLine.hidden = true;
      busy(submit, async () => {
        try {
          const config = {};
          const secrets = {};
          for (const [name, control] of Object.entries(controls)) {
            if (name === 'email' && controls.auth_method.value !== 'global_key') continue;
            config[name] = name === 'port' ? Number(control.value) : control.value.trim();
          }
          let allowedSecrets;
          if (type.value === 'cloudflare') allowedSecrets = config.auth_method === 'global_key' ? ['global_api_key'] : ['token'];
          else if (['github', 'docker'].includes(type.value)) allowedSecrets = ['token'];
          else if (type.value === 'alist') allowedSecrets = ['password'];
          else allowedSecrets = config.auth_method === 'key' ? ['private_key', 'key_passphrase'] : ['password'];
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
          toast('凭据已保存。授权客户端可按名称读取。');
        } catch (error) { errorLine.textContent = message(error); errorLine.hidden = false; }
      });
    });
    openModal(service ? `编辑凭据 · ${service.alias}` : '添加凭据', form, [button('取消', () => modal.close()), submit]);
  }

  function confirmDialog(title, description, action, label = '确认删除') {
    const confirm = button(label, event => {
      if (demo) return demoNotice();
      busy(event.currentTarget, async () => { await action(); modal.close(); });
    }, 'danger');
    openModal(title, node('p', {}, description), [button('取消', () => modal.close()), confirm]);
  }

  function deleteService(service) {
    confirmDialog('删除凭据条目', `删除「${service.alias}」后，客户端将无法再从本库读取这条凭据。`, async () => {
      await api(`/api/services/${encodeURIComponent(service.kind)}/${encodeURIComponent(service.alias)}`, { method: 'DELETE' });
      state.services = state.services.filter(item => !(item.kind === service.kind && item.alias === service.alias));
      setView('services');
      toast('凭据条目已删除。');
    });
  }

  function scopeDescription(allowedServices) {
    if (!Array.isArray(allowedServices) || !allowedServices.length) return '未授权任何凭据';
    if (allowedServices.includes('*')) return '全部凭据（包含今后新增的条目）';
    return allowedServices.join('、');
  }

  function renderClients() {
    content.append(pageHeading('授权客户端读取凭据', '为每个客户端创建独立密钥，并选择它可以读取的条目。', button('＋ 创建客户端密钥', clientDialog, 'primary')));
    content.append(node('div', { class: 'notice info credential-disclosure' }, node('strong', {}, '授权范围决定客户端可以取得哪些凭据值'), node('span', {}, 'get_credentials 会把选中凭据的明文返回给调用客户端。客户端可以在该范围内重复读取，直到你撤销授权。')));
    const endpointValue = endpoint();
    const copyEndpoint = button('复制地址', event => copy(endpointValue, event.currentTarget), 'secondary small');
    content.append(node('section', { class: 'panel' }, node('div', { class: 'panel-heading' }, node('div', {}, node('h2', {}, 'MCP 连接地址'), node('p', {}, '支持 Streamable HTTP 的客户端可使用此地址。')), copyEndpoint), node('div', { class: 'panel-body' }, node('code', { class: 'code-block endpoint-value' }, endpointValue), node('p', { class: 'hint' }, '使用客户端专用 Bearer Token；管理后台密码不能作为客户端密钥。'), node('div', { class: 'mcp-tool-list' }, node('p', {}, node('code', {}, 'list_credentials'), '：列出授权范围内的凭据名称和基本信息。'), node('p', {}, node('code', {}, 'get_credentials'), '：读取选中条目的账号、密码、密钥或 Token 值。')))));
    const list = node('div', { class: 'client-list' });
    for (const client of state.clients) {
      list.append(node('div', { class: 'client-row' }, node('div', { class: 'client-icon', 'aria-hidden': 'true' }, '⌘'), node('div', { class: 'client-details' }, node('h3', {}, client.name), node('p', {}, `创建于 ${prettyDate(client.created_at)} · 最近使用 ${prettyDate(client.last_used)}`), node('p', { class: 'client-scope' }, `读取范围：${scopeDescription(client.allowed_services)}`)), node('span', { class: 'badge success' }, '已授权'), button('撤销', () => confirmDialog('撤销客户端授权', `撤销「${client.name}」后，该客户端将无法再使用此密钥从凭据库读取内容。`, async () => {
        await api(`/api/clients/${encodeURIComponent(client.id)}`, { method: 'DELETE' });
        state.clients = state.clients.filter(item => item.id !== client.id);
        setView('clients');
        toast('客户端授权已撤销。');
      }, '撤销授权'), 'ghost danger small')));
    }
    if (!state.clients.length) list.append(node('div', { class: 'empty-state compact' }, node('h3', {}, '还没有客户端授权'), node('p', {}, '创建一个密钥，并选择允许读取的凭据。')));
    content.append(node('section', { class: 'panel' }, node('div', { class: 'panel-heading' }, node('h2', {}, '已授权客户端'), node('span', { class: 'badge neutral' }, `${state.clients.length} 个`)), list));
    const snippet = `[mcp_servers.my_nas]\nurl = ${JSON.stringify(endpointValue)}\nbearer_token_env_var = "NAS_MCP_TOKEN"`;
    content.append(node('section', { class: 'panel' }, node('div', { class: 'panel-heading' }, node('div', {}, node('h2', {}, '桌面客户端配置示例'), node('p', {}, '将 NAS_MCP_TOKEN 设置为创建的客户端密钥，保存后即可复用。')), button('复制配置', event => copy(snippet, event.currentTarget), 'secondary small')), node('div', { class: 'panel-body' }, node('pre', { class: 'code-block' }, snippet), node('p', { class: 'hint' }, '需要 OAuth 的客户端还需配置兼容的外部身份提供方；开启设置本身不会完成登录接入。'))));
  }

  function clientDialog() {
    const form = node('form', { id: 'client-form' });
    const name = textInput('name', '', { required: true, maxlength: 100, placeholder: '例如我的 Mac · Codex' });
    form.append(field('客户端名称', name, '取一个容易辨认的名称，方便以后撤销授权。'));
    const allowAll = node('input', { type: 'checkbox', name: 'all_credentials', checked: true });
    const selections = [];
    const scopeList = node('div', { class: 'scope-picker', hidden: true });
    for (const service of state.services) {
      const value = `${service.kind}/${service.alias}`;
      const checkbox = node('input', { type: 'checkbox', name: 'allowed_services', value, disabled: true });
      selections.push(checkbox);
      scopeList.append(node('label', { class: 'scope-option' }, checkbox, node('span', {}, node('strong', {}, service.alias), node('code', {}, value))));
    }
    if (!state.services.length) scopeList.append(node('p', { class: 'hint' }, '尚无凭据条目。请先添加条目，或选择允许读取全部凭据。'));
    allowAll.addEventListener('change', () => {
      scopeList.hidden = allowAll.checked;
      for (const checkbox of selections) checkbox.disabled = allowAll.checked;
    });
    form.append(node('div', { class: 'section-divider' }, '凭据读取范围'), node('label', { class: 'check-line scope-all' }, allowAll, node('span', {}, node('strong', {}, '允许读取全部凭据'), node('span', { class: 'hint' }, '包含当前条目和今后新增的条目。取消勾选可指定范围。'))), scopeList, node('div', { class: 'notice info' }, '该客户端调用 get_credentials 时，获准条目中的凭据值会返回给它。'));
    const errorLine = node('p', { class: 'field-error', role: 'alert', hidden: true });
    form.append(errorLine);
    const submit = node('button', { type: 'submit', form: 'client-form', class: 'button primary' }, '生成客户端密钥');
    form.addEventListener('submit', event => {
      event.preventDefault();
      errorLine.hidden = true;
      const allowed_services = allowAll.checked ? ['*'] : selections.filter(item => item.checked).map(item => item.value);
      if (!allowed_services.length) {
        errorLine.textContent = '请至少选择一条凭据，或勾选允许读取全部凭据。';
        errorLine.hidden = false;
        return;
      }
      if (demo) return demoNotice();
      busy(submit, async () => {
        const client = await api('/api/clients', { method: 'POST', body: { name: name.value.trim(), allowed_services } });
        const scope = client.allowed_services || allowed_services;
        state.clients.push({ id: client.id, name: client.name, allowed_services: scope, created_at: new Date().toISOString(), last_used: null });
        setView('clients');
        const token = String(client.token || '');
        const body = node('div', {}, node('div', { class: 'notice info' }, '客户端密钥仅显示一次。关闭前请复制并保存到客户端的认证配置中。'), field('客户端密钥', node('textarea', { class: 'token-box', readonly: true, value: token, rows: 3, spellcheck: false }), '此密钥用于连接凭据库；授权范围见下方。'), button('复制密钥', event => copy(token, event.currentTarget), 'primary'), node('div', { class: 'section-divider' }, '授权范围'), node('p', { class: 'scope-summary' }, scopeDescription(scope)), node('p', { class: 'hint' }, 'get_credentials 会将获准条目中的选中凭据值返回给调用客户端。'), node('div', { class: 'section-divider' }, '连接方式'), meta('MCP 地址', endpoint()), node('p', { class: 'hint' }, '传输类型选择 Streamable HTTP，认证选择 Bearer Token，保存本次生成的客户端密钥。'));
        openModal('客户端密钥已生成', body, button('我已保存，关闭', () => modal.close(), 'secondary'));
      });
    });
    openModal('创建客户端授权', form, [button('取消', () => modal.close()), submit]);
  }

  function lines(value) { return value.split(/[\n,]/).map(item => item.trim()).filter(Boolean); }

  function renderSettings() {
    content.append(pageHeading('设置访问入口', 'MCP 访问地址、允许的主机名与可选的外部 OAuth 验证。'));
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
    oauthFields.append(node('div', { class: 'notice info' }, '此功能需要已配置好的外部 OAuth 身份提供方。打开开关不会自动完成网页登录或创建授权服务。'), node('div', { class: 'field-grid' }, field('Issuer URL', controls.issuer_url), field('MCP 资源地址', controls.resource_server_url), field('Audience', controls.audience), field('JWKS URL', controls.jwks_url)), field('允许的用户 Subject', controls.allowed_subjects, '每行一个。列出的 OAuth 身份视为库所有者，可读取全部凭据；需要限制到指定条目时，请使用客户端 Token。'), field('必需的 Scopes', controls.required_scopes, '用逗号分隔，必须与身份提供方授予的权限一致。'));
    form.append(node('section', { class: 'panel' }, node('div', { class: 'panel-heading' }, node('div', {}, node('h2', {}, 'OAuth 连接'), node('p', {}, '供需要 OAuth 登录的网页或其他客户端使用。'))), node('div', { class: 'panel-body' }, node('label', { class: 'check-line' }, enabled, node('span', {}, node('strong', {}, '启用外部 OAuth 验证'), node('span', { class: 'hint' }, '已有 Bearer Token 的桌面客户端仍可继续使用。'))), oauthFields)));
    const submit = node('button', { class: 'button primary', type: 'submit' }, '保存访问设置');
    const resultLine = node('div', { class: 'notice success', role: 'status', hidden: true });
    form.append(resultLine, node('div', { class: 'actions' }, node('span', { class: 'hint' }, '部分访问设置需要重启服务后生效；容器部署可通过 Compose 重启。'), submit));
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
        resultLine.textContent = result.restart_required ? '设置已保存。请重启服务使访问设置生效；容器部署可在 Compose 管理中重启。' : '访问设置已保存。';
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
      { id: 'demo-mac', name: '我的 Mac · Codex', allowed_services: ['*'], created_at: '2026-10-24T08:00:00Z', last_used: null },
      { id: 'demo-laptop', name: '工作笔记本', allowed_services: ['github/personal-github', 'docker/ghcr-images'], created_at: '2026-10-24T09:00:00Z', last_used: null },
    ];
    state.services = [
      { kind: 'cloudflare', alias: 'personal-cloud', description: '个人 Cloudflare 账号总控制密钥', config: { auth_method: 'global_key', email: 'example-user@example.com' }, credentials: { global_api_key: true } },
      { kind: 'ssh', alias: 'home-nas', description: '家里的飞牛 NAS · root 账号密码', config: { host: '192.0.2.10', port: 22, username: 'root', auth_method: 'password' }, credentials: { password: true } },
      { kind: 'alist', alias: 'family-files', description: '家庭 Alist 账号密码', config: { url: 'https://alist.example.com', username: 'example-user' }, credentials: { password: true } },
      { kind: 'github', alias: 'personal-github', description: '个人 GitHub 账号与 PAT', config: { owner: 'example-user' }, credentials: { token: true } },
      { kind: 'docker', alias: 'dockerhub-images', description: 'Docker Hub 登录凭据', config: { registry: 'docker.io', username: 'example-user', namespace: 'example-user' }, credentials: { token: true } },
      { kind: 'docker', alias: 'ghcr-images', description: 'GitHub Container Registry 登录凭据', config: { registry: 'ghcr.io', username: 'example-user', namespace: 'example-user' }, credentials: { token: true } },
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
