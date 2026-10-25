'use strict';

(() => {
  const $ = selector => document.querySelector(selector);
  const request = new URLSearchParams(location.search).get('request');
  const state = { context: null, working: false };
  const kinds = { cloudflare: 'Cloudflare', ssh: 'SSH', alist: 'Alist', github: 'GitHub', docker: '镜像仓库' };

  function errorText(error) {
    return error instanceof Error ? error.message : '操作未完成，请重试。';
  }

  function showError(selector, error) {
    const target = $(selector);
    target.textContent = errorText(error);
    target.hidden = false;
  }

  async function api(path, options = {}) {
    const headers = { Accept: 'application/json' };
    if (options.body !== undefined) headers['Content-Type'] = 'application/json';
    if (options.method && state.context?.csrf_token) headers['X-CSRF-Token'] = state.context.csrf_token;
    let response;
    try {
      response = await fetch(path, { ...options, headers, credentials: 'same-origin', mode: 'same-origin', redirect: 'error', body: options.body === undefined ? undefined : JSON.stringify(options.body) });
    } catch {
      throw new Error('无法连接凭据库，请检查网络后重试。');
    }
    let data;
    try { data = await response.json(); } catch { throw new Error('凭据库返回了无法识别的响应，请重新发起授权。'); }
    if (!response.ok) {
      const detail = data.error || data.message || data.detail;
      throw new Error(typeof detail === 'string' ? detail : '授权请求未完成，请从客户端重新发起。');
    }
    return data;
  }

  function selectedServices() {
    if ($('input[name="scope_mode"][value="all"]').checked) return ['*'];
    return [...document.querySelectorAll('#credential-options input:checked')].map(input => input.value);
  }

  function updateSelection() {
    const all = $('input[name="scope_mode"][value="all"]').checked;
    const selection = selectedServices();
    $('#credential-picker').disabled = all || state.working;
    $('#selection-summary').textContent = all ? '将允许读取全部凭据，包括今后新增的条目。' : selection.length ? `已选择 ${selection.length} 条凭据。` : '请至少选择一条凭据。';
    $('#approve-submit').disabled = state.working || selection.length === 0;
  }

  function setWorking(working) {
    state.working = working;
    for (const control of document.querySelectorAll('#oauth-login-form button, #oauth-login-form input, #deny-submit, #reload-request, .oauth-scope-modes input')) control.disabled = working;
    $('#oauth-consent-form').setAttribute('aria-busy', String(working));
    updateSelection();
  }

  function renderContext(context) {
    state.context = context;
    const redirect = new URL(context.client.redirect_uri);
    if (!['http:', 'https:'].includes(redirect.protocol)) throw new Error('授权请求的回调地址无效，请重新发起授权。');
    $('#client-name').textContent = context.client.name || '未命名客户端';
    $('#client-domain').textContent = redirect.host;
    $('#client-domain').title = redirect.href;
    $('#oauth-client').hidden = false;
    $('#oauth-login').hidden = Boolean(context.authenticated);
    $('#oauth-setup').hidden = !context.authenticated || !context.must_change_password;
    $('#oauth-consent-form').hidden = !context.authenticated || Boolean(context.must_change_password);
    $('#credential-options').replaceChildren();
    $('input[name="scope_mode"][value="selected"]').checked = true;
    if (context.authenticated && !context.must_change_password) {
      if (typeof context.csrf_token !== 'string' || !context.csrf_token) throw new Error('登录状态无效，请从客户端重新发起授权。');
      const services = Array.isArray(context.services) ? context.services : [];
      for (const service of services) {
        if (typeof service.id !== 'string' || !service.id) continue;
        const label = document.createElement('label');
        label.className = 'scope-option';
        const input = document.createElement('input');
        input.type = 'checkbox'; input.name = 'allowed_services'; input.value = service.id;
        input.addEventListener('change', updateSelection);
        const text = document.createElement('span');
        const name = document.createElement('strong'); name.textContent = service.alias || service.id;
        const type = document.createElement('code'); type.textContent = `${kinds[service.kind] || service.kind} · ${service.id}`;
        text.append(name, type); label.append(input, text); $('#credential-options').append(label);
      }
      $('#empty-credentials').hidden = Boolean($('#credential-options').children.length);
    }
    updateSelection();
    if (!context.authenticated) $('#oauth-password').focus();
  }

  async function loadRequest() {
    $('#oauth-error').hidden = true;
    $('#oauth-loading').hidden = false;
    $('#oauth-login').hidden = true;
    $('#oauth-setup').hidden = true;
    $('#oauth-consent-form').hidden = true;
    try {
      if (!request || request.length > 2048) throw new Error('缺少有效的授权请求，请回到客户端重新发起。');
      const context = await api(`/api/oauth/request?request=${encodeURIComponent(request)}`);
      if (!context || typeof context.request_id !== 'string' || !context.request_id || !context.client || typeof context.client.redirect_uri !== 'string') throw new Error('授权请求无效，请回到客户端重新发起。');
      renderContext(context);
    } catch (error) {
      state.context = null;
      $('#oauth-client').hidden = true;
      $('#oauth-login').hidden = true;
      $('#oauth-setup').hidden = true;
      $('#oauth-consent-form').hidden = true;
      showError('#oauth-error', error);
    } finally {
      $('#oauth-loading').hidden = true;
    }
  }

  async function decide(approve) {
    if (state.working || !state.context?.authenticated || state.context.must_change_password) return;
    const allowed_services = approve ? selectedServices() : [];
    if (approve && !allowed_services.length) {
      showError('#decision-error', new Error('请至少选择一条凭据，或明确选择全部凭据。'));
      return;
    }
    $('#decision-error').hidden = true;
    setWorking(true);
    try {
      const result = await api('/api/oauth/decision', { method: 'POST', body: { request_id: state.context.request_id, approve, allowed_services } });
      const redirect = new URL(result.redirect_url);
      if (!['http:', 'https:'].includes(redirect.protocol)) throw new Error('凭据库返回了无效的跳转地址，请从客户端重新发起授权。');
      $('#oauth-status').textContent = approve ? '已同意授权，正在返回客户端…' : '已取消授权，正在返回客户端…';
      location.replace(redirect.href);
    } catch (error) {
      showError('#decision-error', error);
      setWorking(false);
    }
  }

  $('#oauth-login-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (state.working) return;
    $('#login-error').hidden = true;
    const password = $('#oauth-password').value;
    $('#oauth-password').value = '';
    setWorking(true);
    try {
      await api('/api/login', { method: 'POST', body: { password } });
      await loadRequest();
    } catch (error) {
      showError('#login-error', error);
    } finally {
      setWorking(false);
    }
  });
  $('#oauth-consent-form').addEventListener('submit', event => { event.preventDefault(); decide(true); });
  $('#deny-submit').addEventListener('click', () => decide(false));
  $('#reload-request').addEventListener('click', async () => { if (state.working) return; setWorking(true); try { await loadRequest(); } finally { setWorking(false); } });
  $('#close-login').addEventListener('click', () => { $('#oauth-status').textContent = '关闭此窗口即可结束授权，尚未授予任何权限。'; window.close(); });
  for (const input of document.querySelectorAll('input[name="scope_mode"]')) input.addEventListener('change', updateSelection);
  loadRequest();
})();
