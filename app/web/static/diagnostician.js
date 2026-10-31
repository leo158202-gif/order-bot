/* 本机诊断师：读回持久任务；页面刷新不重新提交动作。 */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else {
    root.Diagnostician = api;
    const start = () => {
      const panel = root.document.getElementById('diagnostician');
      if (panel) api.mount({document: root.document, fetch: root.fetch.bind(root),
        crypto: root.crypto, FormData: root.FormData,
        setTimeout: root.setTimeout.bind(root), clearTimeout: root.clearTimeout.bind(root)});
      else if (root.document.getElementById('adminLogs'))
        api.mount({mode: 'logs', document: root.document, EventSource: root.EventSource});
    };
    if (root.document.readyState === 'loading') root.document.addEventListener('DOMContentLoaded', start);
    else start();
  }
})(typeof window !== 'undefined' ? window : globalThis, function () {
  'use strict';
  const ACTIVE = new Set(['queued', 'pending', 'running', 'prepared', 'validated', 'applying', 'cancelling']);
  const LABELS = {queued: '已排队，订单处理优先', pending: '等待处理', running: '正在检查',
    prepared: '方案已准备', validated: '验证已通过，等待执行', applying: '正在执行',
    cancelling: '正在停止，已完成的步骤会保留', done: '本次处理已完成',
    failed: '本次处理未完成，请查看原因', cancelled: '已停止，已完成的步骤会保留',
    needs_input: '需要补充信息或确认，请查看对话', parked: '缺少证据，已停止重复核查',
    partial: '已完成部分步骤，请查看结果'};
  const idOf = task => task && (task.task_id || task.id);
  const messageText = message => typeof message === 'string' ? message :
    String(message && (message.content || message.text || message.message) || '');

  function mount(env) {
    if (env.mode === 'logs') return mountLogs(env);
    const doc = env.document;
    const el = id => doc.getElementById(id);
    let session = null, csrf = null, busy = false, task = null, timer = null;
    let polls = 0, errors = 0, stoppedPolling = false, history = [];
    const requestId = () => env.crypto.randomUUID();
    const status = text => { el('doctorStatus').textContent = text; };
    const active = () => task && ACTIVE.has(task.status);

    function updateControls() {
      el('doctorSend').disabled = !session || busy || !!active();
      el('doctorUpload').disabled = !session || busy || !!active();
      el('doctorStop').disabled = !active() || task.status === 'cancelling';
    }
    function addMessage(role, content) {
      if (!content) return;
      const article = doc.createElement('article');
      article.className = 'doctor-message ' + (role === 'user' ? 'from-user' : 'from-doctor');
      const name = doc.createElement('div'); name.className = 'doctor-message-name';
      name.textContent = role === 'user' ? '我' : '诊断师';
      const body = doc.createElement('div'); body.className = 'doctor-message-body';
      body.textContent = content;
      article.append(name, body); el('doctorMessages').appendChild(article);
      el('doctorMessages').scrollTop = el('doctorMessages').scrollHeight;
    }
    function renderMessages(messages) {
      history = Array.isArray(messages) ? messages : [];
      el('doctorMessages').replaceChildren();
      if (!history.length) addMessage('assistant', '你好，我可以帮你查运行情况、解释功能和核对订单。指出某格有误时，请告诉我表格、日期 sheet、行列和正确写法；我会先定位，有冲突再向你确认。');
      history.forEach(message => addMessage(message.role, messageText(message)));
    }
    async function api(path, options = {}) {
      const response = await env.fetch('/api/diagnostician' + path, {
        credentials: 'same-origin', cache: 'no-store', ...options,
      });
      if (response.status === 401 || response.status === 403)
        throw new Error('请先到管理后台登录，再点“查看历史记录”恢复会话。');
      let data;
      try { data = await response.json(); }
      catch (_) { throw new Error('服务暂未返回有效结果，请稍后查看历史记录。'); }
      if (!response.ok) throw new Error(data.detail || data.message || data.error || '请求未完成，请查看运行状态。');
      return data;
    }
    const post = (path, data) => api(path, {method: 'POST', headers: {
      'Content-Type': 'application/json', 'X-CSRF-Token': csrf,
    }, body: JSON.stringify(data)});
    function clearPoll() { if (timer !== null) env.clearTimeout(timer); timer = null; }
    function schedule() {
      clearPoll();
      if (!active() || doc.hidden || stoppedPolling) return;
      if (polls >= 60) {
        stoppedPolling = true;
        status('任务仍在后台，本页已暂停查询。可点“查看历史记录”查看进度，不会重复执行。');
        return;
      }
      timer = env.setTimeout(poll, 2000);
    }
    function receiveTask(value) {
      const next = value.task || value;
      if (!idOf(next)) throw new Error('请求已提交但任务编号未返回，请查看历史记录，勿重复发送。');
      task = {...task, ...next, task_id: idOf(next)};
      status(LABELS[task.status] || '任务状态待确认，请查看历史记录。');
      if (task.message) status((LABELS[task.status] || '任务状态') + '：' + task.message);
      if (Array.isArray(value.messages)) renderMessages(value.messages);
      else if (!active()) {
        const result = task.result;
        const text = typeof result === 'string' ? result : result && (result.content || result.message || result.summary);
        if (text && task.task_id !== receiveTask.lastShown) {
          addMessage('assistant', String(text)); receiveTask.lastShown = task.task_id;
        }
      }
      updateControls(); schedule();
    }
    async function poll() {
      timer = null;
      if (doc.hidden || !active() || stoppedPolling) return;
      polls += 1;
      try {
        const data = await api('/tasks/' + encodeURIComponent(task.task_id));
        errors = 0; receiveTask(data);
        if (!active()) {
          // 只读回取持久会话，让工具结果与模型回复在刷新后保持一致。
          const finishedId = task.task_id;
          const saved = await api('/session');
          if (saved.messages && saved.messages.length) renderMessages(saved.messages);
          const continuation = (saved.tasks || []).find(item => idOf(item) !== finishedId && ACTIVE.has(item.status));
          if (continuation) {
            polls = 0; errors = 0; stoppedPolling = false;
            receiveTask(continuation);
          }
        }
      } catch (error) {
        errors += 1;
        if (errors >= 3) stoppedPolling = true;
        status(error.message + (stoppedPolling ? ' 已暂停自动查询，可点“查看历史记录”。' : ''));
        schedule();
      }
    }
    async function restore() {
      clearPoll(); stoppedPolling = false; polls = 0; errors = 0;
      try {
        const saved = await api('/session');
        session = saved.session_id; csrf = saved.csrf_token;
        if (!session || !csrf) throw new Error('会话未就绪，请先到管理后台登录。');
        renderMessages(saved.messages);
        task = null;
        const tasks = Array.isArray(saved.tasks) ? saved.tasks : [];
        const pending = tasks.find(item => ACTIVE.has(item.status));
        if (pending) receiveTask(pending);
        else status('可以提问，历史记录已恢复');
      } catch (error) { session = null; csrf = null; status(error.message); }
      updateControls();
    }
    async function send(event) {
      event.preventDefault();
      const message = el('doctorInput').value.trim();
      if (!message || busy || active() || !session) return;
      busy = true; updateControls(); status('正在提交，请稍候');
      const payload = {session_id: session, message, request_id: requestId()};
      try {
        const result = await post('/chat', payload);
        addMessage('user', message); el('doctorInput').value = '';
        polls = 0; errors = 0; stoppedPolling = false; receiveTask(result);
      } catch (error) {
        // 不自动重发；响应丢失时通过持久会话找回原任务。
        status(error.message + ' 如提交结果不明，请先查看历史记录。');
      } finally { busy = false; updateControls(); }
    }
    async function cancel() {
      if (!active() || task.status === 'cancelling') return;
      const taskId = task.task_id;
      task.status = 'cancelling'; updateControls(); status(LABELS.cancelling);
      try {
        const result = await post('/tasks/' + encodeURIComponent(taskId) + '/cancel', {
          session_id: session, request_id: requestId(),
        });
        receiveTask({...result, task_id: result.task_id || taskId});
      } catch (error) { status(error.message + ' 停止结果待确认，请查看历史记录。'); }
    }
    async function upload() {
      const file = el('doctorUpload').files[0];
      if (!file || busy || active() || !session) return;
      if (!/\.(xlsx|csv)$/i.test(file.name)) {
        status('请上传 .xlsx 或 .csv 金标；旧 .xls 请先另存为 .xlsx。');
        el('doctorUpload').value = ''; return;
      }
      if (file.size > 20 * 1024 * 1024) {
        status('文件超过 20MB，请拆成较小的 .xlsx 或 .csv 再上传。');
        el('doctorUpload').value = ''; return;
      }
      busy = true; updateControls(); status('正在上传金标，请稍候');
      const form = new env.FormData();
      form.append('file', file); form.append('session_id', session);
      form.append('request_id', requestId());
      try {
        const result = await api('/upload', {method: 'POST', headers: {'X-CSRF-Token': csrf}, body: form});
        addMessage('user', '上传金标：' + file.name);
        polls = 0; errors = 0; stoppedPolling = false; receiveTask(result);
      } catch (error) { status(error.message + ' 请先查看历史记录，避免重复上传。'); }
      finally { busy = false; el('doctorUpload').value = ''; updateControls(); }
    }
    el('doctorForm').addEventListener('submit', send);
    el('doctorStop').addEventListener('click', cancel);
    el('doctorUpload').addEventListener('change', upload);
    const historyButton = el('doctorHistory');
    if (historyButton) historyButton.addEventListener('click', restore);
    const goldButton = el('doctorLoadGold');
    if (goldButton) goldButton.addEventListener('click', async () => {
      const box = el('doctorGoldTable'); goldButton.disabled = true;
      try {
        const data = await api('/gold/reports'); box.replaceChildren();
        if (!data.reports.length) { box.textContent = '当前会话还没有金标对照结果。'; return; }
        for (const report of data.reports) {
          const caption = doc.createElement('p');
          const m = report.metrics || {}, first = report.first_metrics || {};
          const metric = value => value == null ? '待核' : String(value);
          caption.textContent = '已核查 ' + metric(report.attempts) + '/3 轮；首次一致 ' + metric(first.numerator) + '/' +
            metric(first.denominator) + '，本次一致 ' + metric(m.numerator) + '/' + metric(m.denominator) +
            '，来源待核 ' + (m.unknown || 0) + '。'; box.appendChild(caption);
          const table = doc.createElement('table'); table.style.width = '100%';
          for (const remark of report.remarks || []) {
            const row = doc.createElement('tr');
            for (const value of [remark.sheet || '日期待核', (remark.rows || []).join('、') || '无对应行',
              remark.order_id ? '订单 ' + remark.order_id : '仅金标/配对待核', remark.text]) {
              const cell = doc.createElement('td'); cell.textContent = String(value || '');
              cell.style.padding = '8px'; cell.style.borderBottom = '1px solid #ddd'; row.appendChild(cell);
            }
            table.appendChild(row);
          }
          box.appendChild(table);
        }
        const repairs = await api('/gold/repairs');
        for (const plan of repairs.plans || []) {
          const line = doc.createElement('p');
          line.textContent = '修正进度：' + plan.done + '/' + plan.total + ' 格，待明确 ' + plan.needs_input +
            ' 格，未确认 ' + plan.partial + ' 格，排队 ' + plan.queued + ' 格。';
          box.appendChild(line);
          for (const item of plan.needs_input_items || []) {
            const note = doc.createElement('p');
            note.textContent = '订单 ' + item.order_id + '，' + item.column + ' 列第 ' + (item.line_index + 1) +
              ' 个商品：' + item.reason;
            box.appendChild(note);
          }
          if (!plan.complete && !plan.cancelled) {
            const button = doc.createElement('button'); button.type = 'button';
            button.textContent = '继续这份已冻结修正';
            button.addEventListener('click', async () => {
              if (busy || active()) return;
              button.disabled = true;
              try {
                const response = await post('/gold/repairs/' + encodeURIComponent(plan.plan_id) + '/continue',
                  {session_id: session, request_id: requestId()});
                polls = 0; errors = 0; stoppedPolling = false; receiveTask(response);
              } catch (error) { status(error.message); }
              finally { button.disabled = false; }
            });
            box.appendChild(button);
          }
        }
      } catch (error) { box.textContent = error.message; }
      finally { goldButton.disabled = false; }
    });
    doc.querySelectorAll('[data-doctor-prompt]').forEach(button => {
      button.addEventListener('click', () => {
        el('doctorInput').value = button.dataset.doctorPrompt; el('doctorInput').focus();
      });
    });
    doc.addEventListener('visibilitychange', async () => {
      if (doc.hidden) clearPoll();
      else if (active() && !stoppedPolling) await poll();
    });
    updateControls();
    return {ready: restore()};
  }

  function mountLogs(env) {
    const doc = env.document, box = doc.getElementById('adminLogs');
    const button = doc.getElementById('logPause'), state = doc.getElementById('logState');
    let source = null, paused = false;
    function close() { if (source) source.close(); source = null; }
    function open() {
      if (paused || doc.hidden || source) return;
      // 每次连接返回最近历史；清屏后恢复，避免把历史一遍遍追加。
      box.replaceChildren();
      source = new env.EventSource('/admin/logs/stream');
      state.textContent = '实时接收中 · 最多保留 500 行';
      source.onmessage = event => {
        String(event.data).split(/\r?\n/).forEach(line => {
          const row = doc.createElement('div'); row.textContent = line; box.appendChild(row);
          while (box.children.length > 500) box.removeChild(box.firstChild);
        });
        box.scrollTop = box.scrollHeight;
      };
      source.onerror = () => {
        close(); paused = true; button.textContent = '继续接收';
        state.textContent = '连接已中断。请确认后台登录，再点“继续接收”。';
      };
    }
    button.addEventListener('click', () => {
      paused = !paused;
      if (paused) { close(); state.textContent = '已暂停接收'; }
      else open();
      button.textContent = paused ? '继续接收' : '暂停接收';
    });
    doc.addEventListener('visibilitychange', () => {
      if (doc.hidden) { close(); state.textContent = '页面在后台，已暂停接收'; }
      else open();
    });
    open();
    return {ready: Promise.resolve()};
  }
  return {mount};
});
