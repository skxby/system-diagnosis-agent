/* 共用前端工具：原生 JS，无框架、无构建工具（计划第 899 行）。
   鉴权完全依赖 httpOnly Cookie —— 这里不保存也不读取任何 token。 */

const API = {
  async request(method, path, { json, form } = {}) {
    const options = { method, credentials: 'same-origin', headers: {} };
    if (json !== undefined) {
      options.headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(json);
    }
    if (form !== undefined) {
      options.body = form; // FormData：不要自己设 Content-Type
    }
    const response = await fetch(path, options);
    if (response.status === 401) {
      // 未登录：送回登录页，并记下原本想去哪
      const next = encodeURIComponent(location.pathname + location.search);
      location.href = `/login?next=${next}`;
      throw new Error('未登录');
    }
    let payload = null;
    const text = await response.text();
    if (text) {
      try { payload = JSON.parse(text); } catch { payload = { detail: text }; }
    }
    if (!response.ok) {
      const detail = payload && payload.detail
        ? (typeof payload.detail === 'string' ? payload.detail : JSON.stringify(payload.detail))
        : `HTTP ${response.status}`;
      const error = new Error(detail);
      error.status = response.status;
      throw error;
    }
    return payload;
  },
  get(path) { return this.request('GET', path); },
  post(path, json) { return this.request('POST', path, { json }); },
  patch(path, json) { return this.request('PATCH', path, { json }); },
  upload(path, formData) { return this.request('POST', path, { form: formData }); },
};

function showError(message) {
  const box = document.getElementById('page-error');
  if (!box) { alert(message); return; }
  box.textContent = message;
  box.className = 'banner banner-error';
  box.hidden = false;
}
function clearError() {
  const box = document.getElementById('page-error');
  if (box) { box.hidden = true; }
}

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key === 'html') throw new Error('请使用 text 渲染内容');
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else if (value !== undefined && value !== null) node.setAttribute(key, value);
  }
  for (const child of [].concat(children)) {
    if (child === null || child === undefined) continue;
    node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
  }
  return node;
}

function fmtTime(value) {
  if (!value) return '—';
  const date = new Date(value);
  return isNaN(date) ? String(value) : date.toLocaleString();
}

function fmtCost(value) {
  const num = Number(value || 0);
  return `¥${num.toFixed(4)}`;
}

/* 四层语义：符号与文案与计划第 833-836 行的报告页渲染一致 */
const SEMANTICS = {
  fact:        { symbol: '✓', label: '确认', cls: 'fact' },
  inference:   { symbol: '→', label: '推测', cls: 'inference' },
  possibility: { symbol: '?', label: '可能', cls: 'possibility' },
  unknown:     { symbol: '−', label: '未知', cls: 'unknown' },
};
function semanticsOf(type) {
  return SEMANTICS[type] || { symbol: '−', label: type || '未知', cls: 'unknown' };
}

const STATUS_LABELS = {
  queued: '排队中', running: '运行中', completed: '已完成',
  partial_success: '部分成功', failed: '失败', timeout: '超时', cancelled: '已取消',
};
function statusPill(status) {
  const cls = status === 'completed' ? 'pill-ok'
    : (status === 'failed' || status === 'timeout') ? 'pill-bad'
    : status === 'partial_success' ? 'pill-medium'
    : 'pill-run';
  return el('span', { class: `pill ${cls}`, text: STATUS_LABELS[status] || status });
}

function severityPill(severity) {
  return el('span', { class: `pill pill-${severity || 'low'}`, text: severity || 'low' });
}

function queryParam(name) {
  return new URLSearchParams(location.search).get(name);
}

document.addEventListener('DOMContentLoaded', () => {
  const logout = document.getElementById('logout-btn');
  if (logout) {
    logout.addEventListener('click', async () => {
      // 清掉 Cookie：让服务端把它置空
      try {
        await API.post('/api/logout');
        location.href = '/login';
      } catch (error) { showError(`退出失败：${error.message}`); }
    });
  }
});
