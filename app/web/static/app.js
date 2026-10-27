(function () {
  "use strict";

  var csrfMeta = document.querySelector('meta[name="csrf-token"]');
  var csrf = csrfMeta ? csrfMeta.getAttribute("content") || "" : "";
  var page = document.body ? document.body.getAttribute("data-page") : "";

  var ERROR_TEXT = {
    bad_message: "消息为空或超过长度限制。",
    turn_in_progress: "上一轮还在处理，请稍后再试。",
    server_busy: "当前访问人数较多，请稍后再试。",
    history_too_long: "会话记录太长，请点击“新会话”后再继续。",
    daily_limit: "今天全站模型额度已用完，请明天再试。",
    model_unavailable: "模型服务暂时不可用，请稍后再试。",
    model_error: "本轮处理失败，请稍后重试或新建会话。",
    workspace_unavailable: "你的数据工作区暂时不可用，请稍后再试。",
    login_required: "登录已失效，请重新申请。",
    access_expired: "访问已到期，请重新申请。",
    csrf_failed: "页面校验失败，请刷新后重试。",
    payload_too_large: "请求内容太大。",
    not_ready: "申请还没有批准。",
    claim_denied: "无法确认这条申请，请重新提交。"
  };

  function messageFor(data, fallback) {
    if (!data) {
      return fallback || "请求失败，请稍后再试。";
    }
    if (data.error && typeof data.error === "string") {
      return data.error;
    }
    if (data.code && ERROR_TEXT[data.code]) {
      return ERROR_TEXT[data.code];
    }
    return fallback || "请求失败，请稍后再试。";
  }

  async function apiFetch(url, options) {
    var settings = options || {};
    var headers = Object.assign(
      { Accept: "application/json" },
      settings.headers || {}
    );
    var isForm = typeof FormData !== "undefined" && settings.body instanceof FormData;
    if (settings.body && !isForm) {
      headers["Content-Type"] = "application/json";
    }
    if (settings.method && settings.method.toUpperCase() !== "GET") {
      headers["X-CSRF-Token"] = csrf;
    }
    var response;
    try {
      response = await fetch(url, {
        method: settings.method || "GET",
        headers: headers,
        body: settings.body,
        credentials: "same-origin",
        cache: "no-store"
      });
    } catch (error) {
      return { response: { ok: false, status: 0 }, data: null };
    }
    var data = null;
    try {
      data = await response.json();
    } catch (error) {
      data = null;
    }
    return { response: response, data: data };
  }

  function formatDuration(totalSeconds) {
    var seconds = Math.max(0, Math.floor(totalSeconds));
    var hours = Math.floor(seconds / 3600);
    var minutes = Math.floor((seconds % 3600) / 60);
    var secs = seconds % 60;
    function two(value) {
      return value < 10 ? "0" + value : String(value);
    }
    return two(hours) + ":" + two(minutes) + ":" + two(secs);
  }

  function setupCopyButtons() {
    document.querySelectorAll("[data-copy-target]").forEach(function (button) {
      button.addEventListener("click", async function () {
        var target = document.getElementById(
          button.getAttribute("data-copy-target")
        );
        if (!target) {
          return;
        }
        try {
          await navigator.clipboard.writeText(target.value);
          button.textContent = "已复制";
          window.setTimeout(function () {
            button.textContent = "复制";
          }, 1600);
        } catch (error) {
          target.focus();
          target.select();
          button.textContent = "请手动复制";
        }
      });
    });
  }

  function initPendingPage() {
    var root = document.getElementById("pending-page");
    if (!root) {
      return;
    }
    var statusEl = document.getElementById("pending-status");
    var hintEl = document.getElementById("pending-hint");
    var retryEl = document.getElementById("pending-retry");
    var stopped = false;
    var claiming = false;
    var labels = {
      pending: "当前状态：等待审批",
      approved: "已批准，正在进入……",
      rejected: "申请已被拒绝。",
      expired: "申请已过期。",
      email_failed: "审批邮件发送失败。",
      none: "没有找到申请记录。"
    };

    function showTerminal(status) {
      stopped = true;
      if (retryEl) {
        retryEl.classList.remove("hidden");
      }
      if (hintEl) {
        hintEl.textContent = "你可以重新提交申请，开发者会收到新的审批邮件。";
      }
      if (statusEl) {
        statusEl.textContent = labels[status] || "申请当前不可用。";
      }
    }

    async function claim() {
      if (claiming || stopped) {
        return;
      }
      claiming = true;
      try {
        var result = await apiFetch(root.getAttribute("data-claim-url"), {
          method: "POST"
        });
        if (result.response.ok && result.data && result.data.ok) {
          window.location.assign(result.data.redirect || "/");
          return;
        }
        if (result.response.status === 401) {
          showTerminal("expired");
          return;
        }
        if (result.response.status === 403) {
          showTerminal("none");
          return;
        }
        if (result.response.status === 409) {
          claiming = false;
          window.setTimeout(poll, 2500);
          return;
        }
        if (statusEl) {
          statusEl.textContent = messageFor(result.data, "领取访问失败，请重试。");
        }
      } catch (error) {
        if (statusEl) {
          statusEl.textContent = "网络暂时不可用，正在重试……";
        }
      }
      claiming = false;
      if (!stopped) {
        window.setTimeout(poll, 3000);
      }
    }

    async function poll() {
      if (stopped) {
        return;
      }
      try {
        var response = await fetch(root.getAttribute("data-status-url"), {
          headers: { Accept: "application/json" },
          credentials: "same-origin",
          cache: "no-store"
        });
        if (!response.ok) {
          throw new Error("status request failed");
        }
        var data = await response.json();
        var status = data.status || "none";
        root.setAttribute("data-status", status);
        if (status === "approved") {
          if (statusEl) {
            statusEl.textContent = labels.approved;
          }
          if (retryEl) {
            retryEl.classList.add("hidden");
          }
          if (hintEl) {
            hintEl.textContent = "正在为你建立访问会话……";
          }
          claim();
          return;
        }
        if (status === "pending") {
          if (statusEl) {
            statusEl.textContent = labels.pending;
          }
          window.setTimeout(poll, 3000);
          return;
        }
        showTerminal(status);
      } catch (error) {
        if (statusEl) {
          statusEl.textContent = "正在等待审批，网络暂时不可用……";
        }
        window.setTimeout(poll, 5000);
      }
    }

    if (root.getAttribute("data-status") === "approved") {
      claim();
    } else {
      poll();
    }
  }

  function initChatPage() {
    var root = document.getElementById("chat-page");
    if (!root) {
      return;
    }
    var form = document.getElementById("chat-form");
    var input = document.getElementById("message-input");
    var sendButton = document.getElementById("send-button");
    var countEl = document.getElementById("char-count");
    var statusEl = document.getElementById("chat-status");
    var messagesEl = document.getElementById("message-list");
    var sessionList = document.getElementById("session-list");
    var countdownEl = document.getElementById("access-countdown");
    var resetButton = document.getElementById("reset-button");
    var logoutButton = document.getElementById("logout-button");
    var maxChars = Number(root.getAttribute("data-max-input-chars")) || 2000;
    var remaining = Number(root.getAttribute("data-remaining-seconds")) || 0;
    var busy = false;
    var expired = false;
    var welcomeText =
      "你好，我是门店运营助手。可以帮你拼场、查预约、看风险和写文案。\n" +
      "直接说你要处理的事情，或者点左侧快捷提问。";

    function setStatus(text, isError) {
      if (!statusEl) {
        return;
      }
      statusEl.textContent = text || "";
      statusEl.classList.toggle("error-text", Boolean(isError));
    }

    function scrollToBottom() {
      if (messagesEl) {
        messagesEl.scrollTop = messagesEl.scrollHeight;
      }
    }

    function addMessage(role, text) {
      var article = document.createElement("article");
      article.className = "message " + role;
      var bubble = document.createElement("div");
      bubble.className = "bubble";
      bubble.textContent = text;
      article.appendChild(bubble);
      if (messagesEl) {
        messagesEl.appendChild(article);
      }
      scrollToBottom();
      return bubble;
    }

    function renderTrace(bubble, trace) {
      if (!Array.isArray(trace) || trace.length === 0) {
        return;
      }
      var details = document.createElement("details");
      details.className = "trace";
      var summary = document.createElement("summary");
      summary.textContent = "本轮工具调用（" + trace.length + "）";
      details.appendChild(summary);
      var list = document.createElement("ol");
      list.className = "trace-list";
      trace.forEach(function (item) {
        var entry = document.createElement("li");
        var name = item && item.tool ? String(item.tool) : "unknown";
        var args = item && item.args ? String(item.args) : "";
        var state = "结果未知";
        if (item && item.ok === true) {
          state = "成功";
        } else if (item && item.ok === false) {
          state = "失败";
          if (item.error_code) {
            state += "：" + String(item.error_code);
          }
        }
        entry.textContent = name + "（" + args + "）— " + state;
        entry.className =
          item && item.ok === false ? "trace-bad" : item && item.ok ? "trace-ok" : "";
        list.appendChild(entry);
      });
      details.appendChild(list);
      if (bubble) {
        bubble.appendChild(details);
      }
    }

    function updateCountdown() {
      if (countdownEl) {
        countdownEl.textContent = formatDuration(remaining);
      }
      if (remaining <= 0 && !expired) {
        expire();
      }
    }

    // 服务端 choices → 气泡按钮。
    // quick_reply 可直接点击重发；resolve 业务按钮需要 App 端
    // Bearer 令牌（/api/app/*），网页 Cookie 会话不可达，诚实降级为提示行，
    // 不伪装成可点击（详见 README「对话按钮」小节）。
    function renderChoices(bubble, choices) {
      if (!bubble || !Array.isArray(choices) || choices.length === 0) {
        return;
      }
      var wrap = document.createElement("div");
      wrap.className = "choices";
      choices.forEach(function (group) {
        var buttons = group && Array.isArray(group.buttons) ? group.buttons : [];
        buttons.forEach(function (button) {
          if (!button || typeof button !== "object") {
            return;
          }
          var action = button.action ? String(button.action) : "";
          var label = button.label ? String(button.label) : action;
          if (!label) {
            return;
          }
          if (action === "quick_reply") {
            var text =
              button.payload && button.payload.text
                ? String(button.payload.text)
                : "";
            if (!text) {
              return;
            }
            var el = document.createElement("button");
            el.type = "button";
            el.className = "choice-button";
            el.textContent = label;
            el.addEventListener("click", function () {
              if (busy || expired || !input) {
                return;
              }
              input.value = text;
              updateCharCount();
              sendMessage();
            });
            wrap.appendChild(el);
          } else {
            var note = document.createElement("span");
            note.className = "choice-note";
            note.textContent = label + "（请在 App 内操作）";
            wrap.appendChild(note);
          }
        });
      });
      if (wrap.childNodes.length > 0) {
        bubble.appendChild(wrap);
        scrollToBottom();
      }
    }

    function expire() {
      expired = true;
      setStatus("访问已到期，正在返回申请页……", true);
      if (sendButton) {
        sendButton.disabled = true;
      }
      if (input) {
        input.disabled = true;
      }
      window.setTimeout(function () {
        window.location.assign("/login?expired=1");
      }, 900);
    }

    function handleUnauthorized(data) {
      var code = data && data.code ? data.code : "";
      if (code === "access_expired") {
        expire();
      } else {
        window.location.assign("/login");
      }
    }

    function updateCharCount() {
      if (input && countEl) {
        countEl.textContent = input.value.length + " / " + maxChars;
      }
    }

    function renderSessions(sessions) {
      if (!sessionList) {
        return;
      }
      while (sessionList.firstChild) {
        sessionList.removeChild(sessionList.firstChild);
      }
      if (!Array.isArray(sessions) || sessions.length === 0) {
        var empty = document.createElement("p");
        empty.className = "muted empty-hint";
        empty.textContent = "暂无未来场次。你可以在对话里让助手录入。";
        sessionList.appendChild(empty);
        return;
      }
      sessions.forEach(function (item) {
        var card = document.createElement("article");
        card.className = "session-card";
        var title = document.createElement("div");
        title.className = "session-title";
        title.textContent = item.script_name || "未命名剧本";
        var time = document.createElement("div");
        time.className = "muted";
        time.textContent = item.start_time || "";
        var meta = document.createElement("div");
        meta.className = "session-meta";
        var type = document.createElement("span");
        type.textContent = item.script_type || "未分类";
        var booked = document.createElement("span");
        booked.textContent =
          "已约 " +
          (item.booked_seats || 0) +
          " / " +
          (item.total_seats || 0);
        var left = document.createElement("span");
        left.textContent = "剩 " + (item.remaining_seats || 0);
        meta.appendChild(type);
        meta.appendChild(booked);
        meta.appendChild(left);
        card.appendChild(title);
        card.appendChild(time);
        card.appendChild(meta);
        sessionList.appendChild(card);
      });
    }

    async function refreshMe() {
      try {
        var result = await apiFetch("/api/me");
        if (result.response.status === 401) {
          handleUnauthorized(result.data);
          return;
        }
        if (result.response.ok && result.data) {
          if (typeof result.data.remaining_seconds === "number") {
            remaining = result.data.remaining_seconds;
            updateCountdown();
          }
          renderSessions(result.data.sessions || []);
        }
      } catch (error) {
        // 侧栏刷新失败不打断对话；下一轮或下次刷新会再试。
      }
    }

    async function sendMessage() {
      if (busy || expired || !input) {
        return;
      }
      var text = input.value.trim();
      if (!text) {
        setStatus("请先输入内容。", true);
        input.focus();
        return;
      }
      if (text.length > maxChars) {
        setStatus("单条消息不能超过 " + maxChars + " 字。", true);
        return;
      }
      busy = true;
      addMessage("user", text);
      input.value = "";
      updateCharCount();
      if (sendButton) {
        sendButton.disabled = true;
        sendButton.textContent = "处理中";
      }
      setStatus("正在处理，模型可能需要几十秒……", false);
      try {
        var result = await apiFetch("/api/chat", {
          method: "POST",
          body: JSON.stringify({ message: text })
        });
        if (result.response.status === 401) {
          handleUnauthorized(result.data);
          return;
        }
        if (!result.response.ok || !result.data || !result.data.ok) {
          setStatus(
            messageFor(result.data, "本轮处理失败，请稍后重试。"),
            true
          );
          return;
        }
        var bubble = addMessage(
          "assistant",
          result.data.answer || "模型没有返回文本。"
        );
        renderTrace(bubble, result.data.trace);
        renderChoices(bubble, result.data.choices);
        if (typeof result.data.remaining_seconds === "number") {
          remaining = result.data.remaining_seconds;
          updateCountdown();
        }
        setStatus("", false);
        refreshMe();
      } catch (error) {
        setStatus("网络暂时不可用，请检查连接后重试。", true);
      } finally {
        busy = false;
        if (sendButton && !expired) {
          sendButton.disabled = false;
          sendButton.textContent = "发送";
        }
        if (input && !expired) {
          input.focus();
        }
      }
    }

    async function resetSession() {
      if (expired) {
        return;
      }
      if (!window.confirm("开始新会话会清空当前对话记录，确定吗？")) {
        return;
      }
      if (resetButton) {
        resetButton.disabled = true;
      }
      try {
        var result = await apiFetch("/api/reset", { method: "POST" });
        if (result.response.status === 401) {
          handleUnauthorized(result.data);
          return;
        }
        if (!result.response.ok || !result.data || !result.data.ok) {
          setStatus(messageFor(result.data, "新会话创建失败。"), true);
          return;
        }
        if (messagesEl) {
          while (messagesEl.firstChild) {
            messagesEl.removeChild(messagesEl.firstChild);
          }
        }
        addMessage("assistant", welcomeText);
        setStatus("已开始新会话。", false);
      } catch (error) {
        setStatus("网络暂时不可用，请稍后重试。", true);
      } finally {
        if (resetButton) {
          resetButton.disabled = false;
        }
      }
    }

    async function logout() {
      if (logoutButton) {
        logoutButton.disabled = true;
      }
      try {
        await apiFetch("/logout", { method: "POST" });
      } catch (error) {
        // 即使网络失败也回到申请页；本地页面不应该继续持有内容。
      }
      window.location.assign("/login");
    }

    if (form) {
      form.addEventListener("submit", function (event) {
        event.preventDefault();
        sendMessage();
      });
    }
    if (input) {
      input.addEventListener("input", updateCharCount);
      input.addEventListener("keydown", function (event) {
        if (
          event.key === "Enter" &&
          !event.shiftKey &&
          !event.isComposing
        ) {
          event.preventDefault();
          sendMessage();
        }
      });
    }
    document.querySelectorAll("[data-prompt]").forEach(function (button) {
      button.addEventListener("click", function () {
        if (!input || expired) {
          return;
        }
        input.value = button.getAttribute("data-prompt") || "";
        updateCharCount();
        input.focus();
      });
    });
    if (resetButton) {
      resetButton.addEventListener("click", resetSession);
    }
    if (logoutButton) {
      logoutButton.addEventListener("click", logout);
    }

    updateCharCount();
    updateCountdown();
    window.setInterval(function () {
      if (!expired) {
        remaining = Math.max(0, remaining - 1);
        updateCountdown();
      }
    }, 1000);
    refreshMe();
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    if (text !== undefined && text !== null) {
      node.textContent = String(text);
    }
    return node;
  }

  function clear(node) {
    if (node) {
      node.replaceChildren();
    }
  }

  function setText(node, text, isError) {
    if (!node) {
      return;
    }
    node.textContent = text || "";
    node.classList.toggle("error-text", Boolean(isError));
  }

  function jsonPost(url, payload, method) {
    return apiFetch(url, {
      method: method || "POST",
      body: JSON.stringify(payload || {})
    });
  }

  var toastTimer = null;
  function toast(text, isError) {
    if (!text) {
      return;
    }
    var node = document.getElementById("app-toast");
    if (!node) {
      node = el("div", "app-toast");
      node.id = "app-toast";
      node.setAttribute("role", "status");
      document.body.appendChild(node);
    }
    node.textContent = String(text);
    node.classList.toggle("app-toast--error", Boolean(isError));
    node.classList.add("app-toast--show");
    if (toastTimer) {
      window.clearTimeout(toastTimer);
    }
    toastTimer = window.setTimeout(function () {
      node.classList.remove("app-toast--show");
    }, 3600);
  }

  async function logout() {
    var button = document.getElementById("logout-button");
    if (button) {
      button.disabled = true;
    }
    try {
      await apiFetch("/logout", { method: "POST" });
    } catch (error) {
      // 网络失败也离开当前页面，避免本地继续持有内容。
    }
    window.location.assign("/login");
  }

  function initShell() {
    var button = document.getElementById("logout-button");
    if (!button || button.getAttribute("data-bound") === "1") {
      return;
    }
    button.setAttribute("data-bound", "1");
    button.addEventListener("click", logout);
  }

  function initResourcesPage() {
    var root = document.querySelector("[data-resources-page]");
    if (!root) {
      return;
    }
    var roomForm = document.getElementById("room-form");
    var dmForm = document.getElementById("dm-form");
    var importForm = document.getElementById("resource-import-form");
    var importStatus = document.getElementById("resource-import-status");
    var demoButton = document.getElementById("seed-demo-button");

    function numberOrNull(value) {
      return value === "" ? null : Number(value);
    }

    function payloadFrom(form, fields) {
      var payload = {};
      fields.forEach(function (name) {
        var input = form.querySelector('[name="' + name + '"]');
        if (!input) {
          return;
        }
        if (input.type === "checkbox") {
          payload[name] = input.checked;
        } else if (input.type === "number") {
          payload[name] = numberOrNull(input.value);
        } else {
          payload[name] = input.value.trim() || null;
        }
      });
      return payload;
    }

    if (roomForm) {
      roomForm.addEventListener("submit", async function (event) {
        event.preventDefault();
        var button = roomForm.querySelector('button[type="submit"]');
        if (button) {
          button.disabled = true;
        }
        var result = await jsonPost("/api/resources/rooms", payloadFrom(roomForm, [
          "room_id", "name", "capacity", "tags", "hourly_cost",
          "buffer_minutes", "active", "notes"
        ]));
        if (button) {
          button.disabled = false;
        }
        if (result.response.ok && result.data && result.data.ok) {
          window.location.reload();
          return;
        }
        setText(importStatus, messageFor(result.data, "保存房间失败。"), true);
      });
    }

    if (dmForm) {
      dmForm.addEventListener("submit", async function (event) {
        event.preventDefault();
        var button = dmForm.querySelector('button[type="submit"]');
        if (button) {
          button.disabled = true;
        }
        var result = await jsonPost("/api/resources/dms", payloadFrom(dmForm, [
          "dm_id", "name", "skills", "hourly_cost",
          "max_sessions_per_day", "active", "notes"
        ]));
        if (button) {
          button.disabled = false;
        }
        if (result.response.ok && result.data && result.data.ok) {
          window.location.reload();
          return;
        }
        setText(importStatus, messageFor(result.data, "保存 DM 失败。"), true);
      });
    }

    document.querySelectorAll("[data-edit-room]").forEach(function (button) {
      button.addEventListener("click", function () {
        if (!roomForm) {
          return;
        }
        roomForm.querySelector("#room-id").value = button.getAttribute("data-edit-room") || "";
        roomForm.querySelector("#room-name").value = button.getAttribute("data-name") || "";
        roomForm.querySelector("#room-capacity").value = button.getAttribute("data-capacity") || "";
        roomForm.querySelector("#room-buffer").value = button.getAttribute("data-buffer") || "15";
        roomForm.querySelector("#room-tags").value = button.getAttribute("data-tags") || "";
        roomForm.querySelector("#room-cost").value = button.getAttribute("data-cost") || "";
        roomForm.querySelector("#room-notes").value = button.getAttribute("data-notes") || "";
        roomForm.querySelector("#room-active").checked =
          button.getAttribute("data-active") === "true";
        roomForm.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    });

    document.querySelectorAll("[data-edit-dm]").forEach(function (button) {
      button.addEventListener("click", function () {
        if (!dmForm) {
          return;
        }
        dmForm.querySelector("#dm-id").value = button.getAttribute("data-edit-dm") || "";
        dmForm.querySelector("#dm-name").value = button.getAttribute("data-name") || "";
        dmForm.querySelector("#dm-skills").value = button.getAttribute("data-skills") || "";
        dmForm.querySelector("#dm-cost").value = button.getAttribute("data-cost") || "";
        dmForm.querySelector("#dm-limit").value = button.getAttribute("data-limit") || "";
        dmForm.querySelector("#dm-notes").value = button.getAttribute("data-notes") || "";
        dmForm.querySelector("#dm-active").checked =
          button.getAttribute("data-active") === "true";
        dmForm.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    });

    // DM 请假（WP13B）：添加走 confirm 收集日期+备注；删除是 chip 上的 ×。
    document.querySelectorAll("[data-add-leave]").forEach(function (button) {
      button.addEventListener("click", async function () {
        var dmId = button.getAttribute("data-add-leave");
        var dmName = button.getAttribute("data-dm-name") || "";
        var day = window.prompt("给「" + dmName + "」登记请假日期（YYYY-MM-DD）：");
        if (!day) {
          return;
        }
        day = String(day).trim();
        if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) {
          toast("日期格式应为 YYYY-MM-DD。", true);
          return;
        }
        var note = window.prompt("备注（可留空，例如：事假）：") || "";
        var result = await jsonPost("/api/resources/dm-leaves", {
          dm_id: dmId,
          day: day,
          note: note
        });
        if (result.response.ok && result.data && result.data.ok) {
          window.location.reload();
          return;
        }
        toast(messageFor(result.data, "登记请假失败。"), true);
      });
    });
    document.querySelectorAll("[data-remove-leave]").forEach(function (button) {
      button.addEventListener("click", async function () {
        var leaveId = button.getAttribute("data-remove-leave");
        if (!window.confirm("删除这条请假记录？")) {
          return;
        }
        var result = await apiFetch(
          "/api/resources/dm-leaves/" + encodeURIComponent(leaveId),
          { method: "DELETE" }
        );
        if (result.response.ok && result.data && result.data.ok) {
          window.location.reload();
          return;
        }
        toast(messageFor(result.data, "删除请假失败。"), true);
      });
    });

    if (demoButton) {
      demoButton.addEventListener("click", async function () {
        if (!window.confirm("载入演示房间、DM、班次和阶段模板？已有资源不会被覆盖。")) {
          return;
        }
        demoButton.disabled = true;
        var result = await jsonPost("/api/resources/demo", { confirm: true });
        demoButton.disabled = false;
        if (result.response.ok && result.data && result.data.ok) {
          window.location.reload();
          return;
        }
        setText(importStatus, messageFor(result.data, "载入演示资源失败。"), true);
      });
    }

    if (importForm) {
      var confirmButton = null;
      importForm.addEventListener("submit", async function (event) {
        event.preventDefault();
        var fileInput = document.getElementById("resource-file");
        if (!fileInput || !fileInput.files || !fileInput.files[0]) {
          setText(importStatus, "请选择 CSV 文件。", true);
          return;
        }
        var kind = document.getElementById("resource-kind").value;
        var data = new FormData();
        data.append("file", fileInput.files[0]);
        data.append("kind", kind);
        data.append("confirm", "false");
        setText(importStatus, "正在校验…");
        var result = await apiFetch("/api/resources/import", {
          method: "POST",
          body: data
        });
        if (!result.response.ok || !result.data || !result.data.ok) {
          setText(importStatus, messageFor(result.data, "导入校验失败。"), true);
          return;
        }
        var info = result.data;
        clear(importStatus);
        importStatus.appendChild(el("span", null,
          "共 " + info.total_rows + " 行，可导入 " + info.valid_rows + " 行，错误 " + info.errors.length + " 行。"));
        if (info.errors.length) {
          var list = el("ul", "plain-list");
          info.errors.slice(0, 8).forEach(function (item) {
            list.appendChild(el("li", null, "第 " + item.row + " 行：" + item.error));
          });
          importStatus.appendChild(list);
        }
        if (confirmButton) {
          confirmButton.remove();
          confirmButton = null;
        }
        if (info.valid_rows > 0) {
          confirmButton = el("button", "primary", "确认写入 " + info.valid_rows + " 行");
          confirmButton.type = "button";
          confirmButton.addEventListener("click", async function () {
            var confirmData = new FormData();
            confirmData.append("file", fileInput.files[0]);
            confirmData.append("kind", kind);
            confirmData.append("confirm", "true");
            var written = await apiFetch("/api/resources/import", {
              method: "POST",
              body: confirmData
            });
            if (written.response.ok && written.data && written.data.ok) {
              window.location.reload();
              return;
            }
            setText(importStatus, messageFor(written.data, "写入失败。"), true);
          });
          importStatus.appendChild(confirmButton);
        }
      });
    }

    // 剧本筛选 + 分页（WP13A）：改条件 → /api/scripts → 只重绘 tbody。
    var scriptsPanel = document.querySelector("[data-scripts-panel]");
    if (!scriptsPanel) {
      return;
    }
    var scriptsTbody = document.getElementById("scripts-tbody");
    var scriptsCount = document.getElementById("scripts-count");
    var scriptsPrev = document.getElementById("scripts-prev");
    var scriptsNext = document.getElementById("scripts-next");
    var scriptsPage = 1;
    var scriptsQueryTimer = null;

    function scriptsQuery() {
      var params = [];
      scriptsPanel.querySelectorAll("[data-filter]").forEach(function (input) {
        var value = String(input.value || "").trim();
        if (value) {
          params.push(encodeURIComponent(input.getAttribute("data-filter")) +
            "=" + encodeURIComponent(value));
        }
      });
      params.push("page=" + scriptsPage);
      return "/api/scripts?" + params.join("&");
    }

    function renderScriptsPage(payload) {
      clear(scriptsTbody);
      var items = payload.items || [];
      if (!items.length) {
        var emptyRow = el("tr");
        var cell = el("td", "empty-state", "没有符合条件的剧本。");
        cell.colSpan = 6;
        emptyRow.appendChild(cell);
        scriptsTbody.appendChild(emptyRow);
      }
      items.forEach(function (script) {
        var row = el("tr");
        row.appendChild(el("td", null, script.script_name || ""));
        row.appendChild(el("td", null, script.script_type || "未分类"));
        row.appendChild(el("td", null, script.total_seats || "—"));
        row.appendChild(el("td", null,
          script.duration_minutes || "缺省 240"));
        row.appendChild(el("td", null, script.list_price || "—"));
        var sourceCell = el("td");
        sourceCell.appendChild(el("span",
          "chip " + (script.source === "公共库" ? "warn" : "ok"),
          script.source || "私有"));
        row.appendChild(sourceCell);
        scriptsTbody.appendChild(row);
      });
      scriptsPage = payload.page || 1;
      if (scriptsCount) {
        scriptsCount.textContent = "共 " + (payload.total || 0) +
          " 部 · 第 " + scriptsPage + " / " + (payload.pages || 1) + " 页";
      }
      if (scriptsPrev) {
        scriptsPrev.disabled = scriptsPage <= 1;
      }
      if (scriptsNext) {
        scriptsNext.disabled = scriptsPage >= (payload.pages || 1);
      }
    }

    function loadScriptsPage() {
      return apiFetch(scriptsQuery()).then(function (result) {
        if (!result.response.ok || !result.data || !result.data.ok) {
          toast(messageFor(result.data, "剧本列表加载失败。"), true);
          return;
        }
        renderScriptsPage(result.data);
      });
    }

    scriptsPanel.querySelectorAll("[data-filter]").forEach(function (input) {
      var eventName = input.tagName === "SELECT" ? "change" : "input";
      input.addEventListener(eventName, function () {
        scriptsPage = 1;
        if (scriptsQueryTimer) {
          window.clearTimeout(scriptsQueryTimer);
        }
        // 搜索框防抖 300ms；下拉/数字立即查。
        var delay = input.getAttribute("data-filter") === "q" ? 300 : 0;
        scriptsQueryTimer = window.setTimeout(loadScriptsPage, delay);
      });
    });
    if (scriptsPrev) {
      scriptsPrev.addEventListener("click", function () {
        if (scriptsPage > 1) {
          scriptsPage -= 1;
          loadScriptsPage();
        }
      });
    }
    if (scriptsNext) {
      scriptsNext.addEventListener("click", function () {
        scriptsPage += 1;
        loadScriptsPage();
      });
    }
  }

  function initSchedulePage() {
    var root = document.querySelector("[data-schedule-page]");
    if (!root) {
      return;
    }
    var form = document.getElementById("session-form");
    var resultBox = document.getElementById("session-result");
    var timeBoardShell = document.getElementById("time-board");

    // ---------- 两块看板共享一次首屏 GET /api/schedule ----------
    // 之后各自的刷新仍发全新请求（fresh），避免拿到过期快照。
    // include_scripts=0：Web 看板不用剧本全表（公共库 5800+ 行），省掉大段序列化。
    var schedulePromise = null;
    // 最近一次成功渲染的看板数据：建场时用来判断「这场是不是本来就有」。
    var lastScheduleData = null;
    function rememberSchedule(result) {
      if (result && result.response && result.response.ok &&
          result.data && result.data.ok) {
        lastScheduleData = result.data;
      }
      return result;
    }
    function loadSchedule(fresh) {
      if (!schedulePromise || fresh) {
        schedulePromise = apiFetch("/api/schedule?include_scripts=0").then(rememberSchedule);
      }
      return schedulePromise;
    }

    // 共享刷新：一次 GET 把同一份 payload 分发给两块看板原地重绘，
    // 不再整页 reload。单调 token 保证乱序返回的旧响应不会盖掉新数据。
    var refreshToken = 0;
    async function refreshSchedule() {
      refreshToken += 1;
      var token = refreshToken;
      var result = await loadSchedule(true);
      if (token !== refreshToken) {
        return; // 已有更新的刷新在途：丢弃这次（可能过期）的响应。
      }
      if (!result.response.ok || !result.data || !result.data.ok) {
        toast("排期数据刷新失败，请刷新页面重试。", true);
        return;
      }
      var timeBoard = document.getElementById("time-board");
      var statusBoard = document.getElementById("status-board");
      if (timeBoard && typeof timeBoard._render === "function") {
        timeBoard._render(result.data);
      }
      if (statusBoard && typeof statusBoard._render === "function") {
        statusBoard._render(result.data);
      }
    }

    // ---------- 纯日历运算（无时区；工具栏日期与看板共用） ----------
    var WEEKDAYS = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];
    var MONTH_DAYS = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];

    function pad2(value) {
      return (value < 10 ? "0" : "") + value;
    }

    function isLeap(year) {
      return (year % 4 === 0 && year % 100 !== 0) || year % 400 === 0;
    }

    function daysInMonth(year, month) {
      if (month === 2 && isLeap(year)) {
        return 29;
      }
      return MONTH_DAYS[month - 1];
    }

    function parseDay(dateStr) {
      return {
        y: Number(dateStr.slice(0, 4)),
        m: Number(dateStr.slice(5, 7)),
        d: Number(dateStr.slice(8, 10))
      };
    }

    function addDays(dateStr, delta) {
      var parts = parseDay(dateStr);
      var day = parts.d + delta;
      var month = parts.m;
      var year = parts.y;
      while (day > daysInMonth(year, month)) {
        day -= daysInMonth(year, month);
        month += 1;
        if (month > 12) {
          month = 1;
          year += 1;
        }
      }
      while (day < 1) {
        month -= 1;
        if (month < 1) {
          month = 12;
          year -= 1;
        }
        day += daysInMonth(year, month);
      }
      return year + "-" + pad2(month) + "-" + pad2(day);
    }

    function weekdayOf(dateStr) {
      // Sakamoto 算法：纯公历运算，与机器时区无关。
      var parts = parseDay(dateStr);
      var table = [0, 3, 2, 5, 0, 3, 5, 1, 4, 6, 2, 4];
      var year = parts.y - (parts.m < 3 ? 1 : 0);
      var index = (year + Math.floor(year / 4) - Math.floor(year / 100) +
        Math.floor(year / 400) + table[parts.m - 1] + parts.d) % 7;
      return WEEKDAYS[index];
    }

    function summaryFor(data) {
      var lines = [];
      if (data.status === "promoted" && data.session) {
        lines.push("已保存：" + (data.session.start_time || "") + " 《" + (data.session.script_name || "") + "》");
        var assign = data.auto_assign || {};
        if (assign.status === "assigned" && assign.assigned && assign.assigned.length) {
          var item = assign.assigned[0];
          lines.push("自动排到：" + (item.room_name || item.room_id || "房间待定") +
            " / " + (item.dm_name || item.dm_id || "DM 待定"));
        } else if (assign.status === "conflict") {
          lines.push("场次已保存，但资源冲突，尚未分配。");
          (assign.conflicts || []).forEach(function (conflict) {
            lines.push("冲突：" + (conflict.message || conflict.reason || "资源不足"));
            (conflict.options || []).forEach(function (option) {
              lines.push("可选：" + (option.label || option.action || option));
            });
          });
        } else if (assign.status === "resources_not_configured") {
          lines.push("还没有房间或 DM，先到资源页建档。");
        }
      } else if (data.status === "incomplete" || data.status === "needs_confirmation") {
        lines.push(data.suggested_question || "信息还不完整。");
      }
      (data.warnings || []).forEach(function (warning) {
        lines.push(warning);
      });
      return lines;
    }

    function renderResult(data, isError) {
      if (!resultBox) {
        return;
      }
      resultBox.classList.remove("hidden");
      clear(resultBox);
      resultBox.classList.toggle("error-text", Boolean(isError));
      summaryFor(data).forEach(function (line) {
        resultBox.appendChild(el("p", null, line));
      });
    }

    // ---------- 紧凑建场卡片：全程按钮点选，不需要键盘 ----------
    // 硬必填（剧本全名 + 开场时间）齐全即 POST /api/sessions 直接建场；
    // 房间/DM 默认「自动分配」，也可在卡片里手动指定。
    var picker = { scriptName: "", day: "", hour: 19, minute: 0 };
    var dayRow = document.getElementById("session-days");
    var hourRow = document.getElementById("session-hours");
    var minuteRow = document.getElementById("session-minutes");
    var searchInput = document.getElementById("session-script-search");
    var resultsBox = document.getElementById("script-results");
    var selectedBox = document.getElementById("script-selected");
    var submitButton = document.getElementById("session-submit");
    var searchTimer = null;
    var searchToken = 0;
    // 剧本人数筛选（筛剧本用，与「场次人数」输入框是两回事）。
    var playerFilter = null;
    var PLAYER_FILTERS = [
      { label: "全部", value: null },
      { label: "4 人", value: 4 },
      { label: "5 人", value: 5 },
      { label: "6 人", value: 6 },
      { label: "7 人", value: 7 },
      { label: "8 人", value: 8 },
      { label: "9 人及以上", value: "9+" }
    ];

    function windowStartDay() {
      var attr = timeBoardShell ? timeBoardShell.getAttribute("data-window-start") : "";
      return String(attr || "").slice(0, 10);
    }

    // 页面根节点的 data-as-of 是服务端的“现在”；业务时间一律和它比较，
    // 不用 new Date()，避开时区/机器时间。
    function asOfText() {
      return String(root.getAttribute("data-as-of") || "").slice(0, 16);
    }

    function windowEndText() {
      return String(
        timeBoardShell ? timeBoardShell.getAttribute("data-window-end") : ""
      ).slice(0, 16);
    }

    // 默认开场时间：今天 19:00；若已早于 data-as-of 则顺延到明天 19:00。
    function defaultStartText() {
      var asOf = asOfText();
      var date = asOf.slice(0, 10) || windowStartDay();
      if (!date) {
        return "";
      }
      var candidate = date + " 19:00";
      if (asOf && candidate < asOf) {
        candidate = addDays(date, 1) + " 19:00";
      }
      return candidate;
    }

    function makeChip(text, active, onClick) {
      var button = document.createElement("button");
      button.type = "button";
      button.className = "chip-button" + (active ? " chip--active" : "");
      button.textContent = text;
      button.addEventListener("click", onClick);
      return button;
    }

    function dayLabel(dateStr, offset) {
      if (offset === 0) {
        return "今天";
      }
      if (offset === 1) {
        return "明天";
      }
      if (offset === 2) {
        return "后天";
      }
      var parts = parseDay(dateStr);
      return weekdayOf(dateStr) + " " + parts.m + "/" + parts.d;
    }

    function buildDayChips(extraDay) {
      if (!dayRow) {
        return;
      }
      clear(dayRow);
      var base = windowStartDay();
      if (!base) {
        return;
      }
      var dates = [];
      for (var i = 0; i < 7; i += 1) {
        dates.push({ date: addDays(base, i), offset: i });
      }
      if (extraDay && !dates.some(function (item) { return item.date === extraDay; })) {
        // 看板空位可能落在常规 7 天外（次日凌晨）：补一颗按钮，不让预填丢时间。
        dates.push({ date: extraDay, offset: 3 + dates.length });
      }
      dates.forEach(function (item) {
        dayRow.appendChild(makeChip(dayLabel(item.date, item.offset),
          picker.day === item.date, function () {
            picker.day = item.date;
            buildDayChips();
            refreshSubmit();
          }));
      });
    }

    function buildHourChips(extraHour) {
      if (!hourRow) {
        return;
      }
      clear(hourRow);
      var hours = [];
      for (var hour = 10; hour <= 23; hour += 1) {
        hours.push(hour);
      }
      if (extraHour != null && hours.indexOf(extraHour) < 0) {
        // 看板业务轴是 06:00–26:00：凌晨夜场补一颗按钮。
        hours.push(extraHour);
        hours.sort(function (a, b) { return a - b; });
      }
      hours.forEach(function (hour) {
        hourRow.appendChild(makeChip(pad2(hour), picker.hour === hour, function () {
          picker.hour = hour;
          buildHourChips();
          refreshSubmit();
        }));
      });
    }

    function buildMinuteChips() {
      if (!minuteRow) {
        return;
      }
      clear(minuteRow);
      [0, 15, 30, 45].forEach(function (minute) {
        minuteRow.appendChild(makeChip(":" + pad2(minute), picker.minute === minute,
          function () {
            picker.minute = minute;
            buildMinuteChips();
            refreshSubmit();
          }));
      });
    }

    function startTimeText() {
      if (!picker.day) {
        return "";
      }
      return picker.day + " " + pad2(picker.hour) + ":" + pad2(picker.minute);
    }

    function refreshSubmit() {
      if (!submitButton) {
        return;
      }
      var startText = startTimeText();
      var problem = "";
      if (startText) {
        var asOf = asOfText();
        var end = windowEndText();
        if (asOf && startText < asOf) {
          problem = "这个时间已经过去（当前 " + asOf + "），请改选开场时间。";
        } else if (end && startText >= end) {
          problem = "超出看板范围（至 " + end + "），请改选开场时间。";
        }
      }
      submitButton.disabled = !picker.scriptName || !startText || Boolean(problem);
      setText(document.getElementById("create-status"), problem, false);
    }

    // 剧本人数筛选 chips：精确 N → seats_min=seats_max=N；9+ → seats_min=9。
    function buildPlayerChips() {
      var row = document.getElementById("session-script-players");
      if (!row) {
        return;
      }
      clear(row);
      PLAYER_FILTERS.forEach(function (filter) {
        row.appendChild(makeChip(filter.label, playerFilter === filter.value,
          function () {
            playerFilter = filter.value;
            buildPlayerChips();
            runScriptSearch();
          }));
      });
    }

    var createDialog = document.getElementById("create-dialog");

    // 建场卡片：页头「新增场次」与看板空位点击共用。
    // startText 缺省时用默认 19:00（今天已过则明天）；所有输入先复位。
    function openCreateDialog(startText) {
      picker.scriptName = "";
      playerFilter = null;
      if (searchInput) {
        searchInput.value = "";
      }
      if (resultsBox) {
        clear(resultsBox);
        resultsBox.classList.add("hidden");
      }
      var typeSelect = document.getElementById("session-script-type");
      if (typeSelect) {
        typeSelect.value = "";
      }
      var seatsInput = document.getElementById("session-seats");
      if (seatsInput) {
        seatsInput.value = "";
      }
      var priceInput = document.getElementById("session-price");
      if (priceInput) {
        priceInput.value = "";
      }
      var roomSelect = document.getElementById("session-room");
      if (roomSelect) {
        roomSelect.value = "";
      }
      var dmSelect = document.getElementById("session-dm");
      if (dmSelect) {
        dmSelect.value = "";
      }
      if (resultBox) {
        clear(resultBox);
        resultBox.classList.add("hidden");
        resultBox.classList.remove("error-text");
      }
      setText(document.getElementById("create-status"), "", false);
      var more = createDialog ? createDialog.querySelector(".more-fields") : null;
      if (more) {
        more.open = false;
      }
      var start = String(startText || "").slice(0, 16);
      if (!/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/.test(start)) {
        start = defaultStartText();
      }
      var date = start.slice(0, 10);
      var hour = Number(start.slice(11, 13));
      var minute = Number(start.slice(14, 16));
      var hourValid = hour >= 0 && hour <= 23;
      picker.day = date;
      picker.hour = hourValid ? hour : 19;
      picker.minute = [0, 15, 30, 45].indexOf(minute) >= 0
        ? minute
        : Math.min(45, Math.max(0, Math.round(minute / 15) * 15));
      var base = windowStartDay();
      var inWindow = false;
      if (base) {
        for (var i = 0; i < 7; i += 1) {
          if (addDays(base, i) === date) {
            inWindow = true;
            break;
          }
        }
      }
      buildDayChips(inWindow ? "" : date);
      buildHourChips(hourValid ? hour : 19);
      buildMinuteChips();
      buildPlayerChips();
      renderSelectedScript();
      refreshSubmit();
      if (createDialog && typeof createDialog.showModal === "function") {
        createDialog.showModal();
      }
      if (searchInput) {
        searchInput.focus();
      }
    }

    function renderSelectedScript() {
      if (!selectedBox) {
        return;
      }
      clear(selectedBox);
      if (!picker.scriptName) {
        return;
      }
      selectedBox.appendChild(el("span", "chip ok", "《" + picker.scriptName + "》"));
      var clearButton = document.createElement("button");
      clearButton.type = "button";
      clearButton.className = "chip-button";
      clearButton.textContent = "×";
      clearButton.setAttribute("aria-label", "取消已选剧本");
      clearButton.addEventListener("click", function () {
        picker.scriptName = "";
        if (searchInput) {
          searchInput.value = "";
        }
        renderSelectedScript();
        refreshSubmit();
      });
      selectedBox.appendChild(clearButton);
    }

    function setScript(name) {
      picker.scriptName = name || "";
      if (searchInput) {
        searchInput.value = "";
      }
      if (resultsBox) {
        clear(resultsBox);
        resultsBox.classList.add("hidden");
      }
      renderSelectedScript();
      refreshSubmit();
    }

    function renderScriptResults(items, total) {
      if (!resultsBox) {
        return;
      }
      clear(resultsBox);
      if (!items || !items.length) {
        resultsBox.appendChild(el("p", "muted",
          "没有匹配的剧本，先到剧本库建档再回来。"));
        resultsBox.classList.remove("hidden");
        return;
      }
      items.forEach(function (row) {
        var name = String(row.script_name || "").trim();
        if (!name) {
          return;
        }
        var rowButton = document.createElement("button");
        rowButton.type = "button";
        rowButton.className = "script-result";
        rowButton.appendChild(el("strong", "", name));
        var meta = [row.script_type,
          row.total_seats ? row.total_seats + " 人" : "",
          row.duration_minutes ? row.duration_minutes + " 分" : "",
          row.source || ""].filter(Boolean).join(" · ");
        if (meta) {
          rowButton.appendChild(el("span", "muted", meta));
        }
        rowButton.addEventListener("click", function () {
          setScript(name);
          fillEmptyFields(row);
        });
        resultsBox.appendChild(rowButton);
      });
      if (Number(total) > items.length) {
        resultsBox.appendChild(el("p", "muted",
          "共 " + total + " 条，仅显示前 " + items.length + " 条。"));
      }
      resultsBox.classList.remove("hidden");
    }

    // 选中剧本时只补空着的场次人数/原价，不覆盖用户已填的值。
    function fillEmptyFields(row) {
      var seatsInput = document.getElementById("session-seats");
      var priceInput = document.getElementById("session-price");
      if (seatsInput && !String(seatsInput.value).trim() && row.total_seats != null) {
        seatsInput.value = String(row.total_seats);
      }
      if (priceInput && !String(priceInput.value).trim() && row.list_price != null) {
        priceInput.value = String(row.list_price);
      }
    }

    function runScriptSearch() {
      var query = searchInput ? searchInput.value.trim() : "";
      var typeSelect = document.getElementById("session-script-type");
      var scriptType = typeSelect ? typeSelect.value : "";
      var params = [];
      if (query) {
        params.push("q=" + encodeURIComponent(query));
      }
      if (scriptType) {
        params.push("type=" + encodeURIComponent(scriptType));
      }
      if (playerFilter === "9+") {
        params.push("seats_min=9");
      } else if (typeof playerFilter === "number") {
        params.push("seats_min=" + playerFilter);
        params.push("seats_max=" + playerFilter);
      }
      if (!params.length) {
        // 关键字为空且没有类型/人数筛选：不拉全库。
        if (resultsBox) {
          clear(resultsBox);
          resultsBox.classList.add("hidden");
        }
        return;
      }
      params.push("page_size=10");
      searchToken += 1;
      var token = searchToken;
      apiFetch("/api/scripts?" + params.join("&"))
        .then(function (result) {
          if (token !== searchToken) {
            return; // 过期响应：以最后一次输入为准。
          }
          if (!result.response.ok || !result.data || !result.data.ok) {
            if (resultsBox) {
              clear(resultsBox);
              resultsBox.appendChild(el("p", "muted", "剧本搜索失败，请稍后重试。"));
              resultsBox.classList.remove("hidden");
            }
            return;
          }
          renderScriptResults((result.data.items || []).slice(0, 10),
            result.data.total);
        });
    }

    if (searchInput) {
      searchInput.addEventListener("input", function () {
        if (searchTimer) {
          window.clearTimeout(searchTimer);
        }
        searchTimer = window.setTimeout(runScriptSearch, 300);
      });
      searchInput.addEventListener("keydown", function (event) {
        if (event.key !== "Enter") {
          return;
        }
        // 搜索框里回车只触发搜索，不提交建场表单。
        event.preventDefault();
        if (searchTimer) {
          window.clearTimeout(searchTimer);
        }
        runScriptSearch();
      });
    }

    var scriptTypeSelect = document.getElementById("session-script-type");
    if (scriptTypeSelect) {
      scriptTypeSelect.addEventListener("change", runScriptSearch);
    }

    function numberOrNull(value) {
      return value === "" || value == null ? null : Number(value);
    }

    // 初始状态：默认 19:00；未选剧本前提交按钮保持禁用。
    // 打开卡片时 openCreateDialog 会按入场参数完整复位，这里只是首屏占位。
    var initialDay = windowStartDay();
    if (initialDay) {
      picker.day = initialDay;
    }
    buildDayChips();
    buildHourChips();
    buildMinuteChips();
    buildPlayerChips();
    renderSelectedScript();
    refreshSubmit();

    var addSessionButton = document.getElementById("add-session-button");
    if (addSessionButton) {
      addSessionButton.addEventListener("click", function () {
        openCreateDialog("");
      });
    }

    var createCancel = document.getElementById("create-cancel");
    if (createCancel && createDialog) {
      createCancel.addEventListener("click", function () {
        createDialog.close();
      });
    }

    if (form) {
      form.addEventListener("submit", async function (event) {
        event.preventDefault();
        var startText = startTimeText();
        if (!picker.scriptName || !startText) {
          return;
        }
        if (submitButton) {
          submitButton.disabled = true;
        }
        var roomSelect = document.getElementById("session-room");
        var dmSelect = document.getElementById("session-dm");
        // 场次 ID 由「门店+剧本+开场时间」决定：同一剧本同一时间再建一次是
        // 合并（更新）而不是新增。先记下当前看板已有的 ID，好在提示里说清楚。
        var knownIds = {};
        ((lastScheduleData || {}).sessions || []).forEach(function (row) {
          knownIds[row.session_id] = true;
        });
        var result = await jsonPost("/api/sessions", {
          script_name: picker.scriptName,
          start_time: startText,
          total_seats: numberOrNull((document.getElementById("session-seats") || {}).value),
          list_price: numberOrNull((document.getElementById("session-price") || {}).value),
          room_id: roomSelect ? roomSelect.value : "",
          dm_id: dmSelect ? dmSelect.value : ""
        });
        refreshSubmit();
        if (!result.response.ok || !result.data || !result.data.ok) {
          // 失败保持弹窗打开，错误显示在卡片里。
          renderResult({ warnings: [messageFor(result.data, "建场失败。")] }, true);
          return;
        }
        var data = result.data;
        var manual = data.manual_assign || {};
        var session = data.session || {};
        var merged = !!(session.session_id && knownIds[session.session_id]);
        var mergeNote = merged
          ? "这个时间已有《" + (session.script_name || picker.scriptName) +
            "》场次（同一剧本同一开场时间算同一场），本次已合并更新，没有新建。"
          : "";
        if (createDialog) {
          createDialog.close();
        }
        if (merged) {
          // 没有新建：措辞不能再写“已建场”，否则店长会以为多了一场。
          toast(mergeNote, true);
        }
        if (manual.status === "applied") {
          toast(merged
            ? "手动指定的房间/DM 已生效。"
            : "已建场，手动指定的房间/DM 已生效。");
        } else if (manual.status === "failed") {
          // 场次已保留、自动分配结果也保留，但必须把“手动未生效”说清楚。
          var prefix = merged ? "手动指定未生效：" : "已建场，但手动指定未生效：";
          toast(manual.message
            ? prefix + manual.message + "；已保留自动分配结果。"
            : prefix + "已保留自动分配结果。", true);
        } else if (!merged) {
          toast("已建场：" + (session.start_time || startText) +
            " 《" + (session.script_name || picker.scriptName) + "》");
          (data.warnings || []).forEach(function (warning) {
            toast(warning, true);
          });
        }
        await refreshSchedule();
      });
    }

    // ---------- 审核弹窗：补齐未分配 / 优化方案都「先看后写」 ----------
    var previewDialog = document.getElementById("preview-dialog");
    var previewItems = []; // 可勾选改动：[{session_id, before, after, checkbox}]

    function describeResource(resource) {
      var room = (resource || {}).room_name || (resource || {}).room_id;
      var dm = (resource || {}).dm_name || (resource || {}).dm_id;
      if (!room && !dm) {
        return "未分配";
      }
      return "房间 " + (room || "未分配") + " / DM " + (dm || "未分配");
    }

    function updatePreviewApply() {
      var applyButton = document.getElementById("preview-apply");
      if (!applyButton) {
        return;
      }
      var count = previewItems.filter(function (item) {
        return item.checkbox.checked;
      }).length;
      applyButton.textContent = "应用所选 (" + count + ")";
      applyButton.disabled = count === 0;
    }

    function appendMetaLine(text, kind) {
      var meta = document.getElementById("preview-meta");
      if (!meta) {
        return;
      }
      meta.appendChild(el("p", kind || "muted", text));
    }

    // 每条改动一行 checkbox（默认勾选）+ 原值/新值；勾选状态驱动「应用所选」。
    function appendProposalRow(row) {
      var list = document.getElementById("preview-list");
      if (!list) {
        return;
      }
      var label = document.createElement("label");
      label.className = "preview-row";
      var checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = true;
      label.appendChild(checkbox);
      var body = el("span", "preview-row-body");
      body.appendChild(el("strong", null,
        "《" + (row.script_name || row.session_id) + "》" + (row.start || "")));
      body.appendChild(el("span", "preview-before",
        "原：" + describeResource(row.before)));
      body.appendChild(el("span", "preview-after",
        "新：" + describeResource(row.after)));
      label.appendChild(body);
      list.appendChild(label);
      var entry = {
        session_id: row.session_id,
        before: row.before || {},
        after: row.after || {},
        checkbox: checkbox
      };
      previewItems.push(entry);
      checkbox.addEventListener("change", updatePreviewApply);
    }

    function renderPartialRows(partial) {
      (partial || []).forEach(function (item) {
        appendMetaLine("《" + (item.script_name || item.session_id) + "》" +
          (item.start || "") + "：已分配" +
          (item.missing === "room" ? " DM、缺房间" : "房间、缺 DM") +
          "。补齐不会移动已有资源，请用「生成优化方案」处理。", "notice warn");
      });
    }

    function renderConflictRows(conflicts) {
      (conflicts || []).forEach(function (conflict) {
        appendMetaLine("冲突（不会直接写入）：" +
          (conflict.message || conflict.reason || "资源不足"), "notice warn");
      });
    }

    function openPreviewDialog(title, summaryText, fillList) {
      previewItems = [];
      var list = document.getElementById("preview-list");
      var meta = document.getElementById("preview-meta");
      clear(list);
      clear(meta);
      setText(document.getElementById("preview-title"), title, false);
      setText(document.getElementById("preview-summary"), summaryText, false);
      setText(document.getElementById("preview-status"), "", false);
      fillList();
      updatePreviewApply();
      var selectAll = document.getElementById("preview-select-all");
      var selectNone = document.getElementById("preview-select-none");
      if (selectAll) {
        selectAll.disabled = !previewItems.length;
      }
      if (selectNone) {
        selectNone.disabled = !previewItems.length;
      }
      if (previewDialog && typeof previewDialog.showModal === "function") {
        previewDialog.showModal();
      }
    }

    var autoButton = document.getElementById("auto-assign-button");
    if (autoButton) {
      autoButton.addEventListener("click", async function () {
        autoButton.disabled = true;
        // 只算不写：preview 默认即 true，写入统一走 /api/schedule/apply。
        var result = await jsonPost("/api/schedule/auto-assign", { preview: true });
        autoButton.disabled = false;
        if (!result.response.ok || !result.data || !result.data.ok) {
          toast(messageFor(result.data, "生成补齐预览失败。"), true);
          return;
        }
        var data = result.data;
        var proposals = data.proposals || [];
        openPreviewDialog("补齐未分配",
          "以下是将要写入的改动（默认全选，可逐条取消）。只写房间/DM，不改开场时间、不取消场次。",
          function () {
            var list = document.getElementById("preview-list");
            if (!proposals.length) {
              list.appendChild(el("p", "empty-state",
                "没有可补齐的场次：所有场次都已经有房间和 DM。"));
              return;
            }
            proposals.forEach(function (row) {
              appendProposalRow(row);
            });
            appendMetaLine("共 " + proposals.length + " 场待补齐；另有 " +
              (data.unchanged || []).length + " 场已完整，保持不变。");
            renderPartialRows(data.partial);
            renderConflictRows(data.conflicts);
            (data.warnings || []).forEach(function (warning) {
              appendMetaLine(warning);
            });
          });
      });
    }

    var planButton = document.getElementById("plan-button");
    if (planButton) {
      planButton.addEventListener("click", async function () {
        planButton.disabled = true;
        var result = await jsonPost("/api/schedule/plan", {});
        planButton.disabled = false;
        if (!result.response.ok || !result.data || !result.data.ok) {
          toast(messageFor(result.data, "生成优化方案失败。"), true);
          return;
        }
        var data = result.data;
        var proposals = data.proposals || [];
        var changed = proposals.filter(function (row) {
          return row.changed === true;
        });
        var unchangedCount = proposals.length - changed.length;
        openPreviewDialog("生成优化方案",
          (data.optimal
            ? "已求得当前范围内的最优方案。"
            : "搜索达到上限，以下是当前可行解，不保证全局最优。") +
          " 勾选确认后才会写入；系统不改开场时间、不取消场次。",
          function () {
            var list = document.getElementById("preview-list");
            if (!changed.length) {
              list.appendChild(el("p", "empty-state",
                "没有需要写入的改动：当前分配已是可求解范围内的结果。"));
            } else {
              changed.forEach(function (row) {
                appendProposalRow(row);
              });
            }
            if (unchangedCount > 0) {
              appendMetaLine("另有 " + unchangedCount + " 场保持不变（已锁定）。");
            }
            renderPartialRows(data.partial);
            (data.shortages || []).forEach(function (item) {
              appendMetaLine("《" + (item.script_name || item.session_id) + "》：" +
                (item.message || item.reason || "资源不足"), "notice warn");
            });
            (data.warnings || []).forEach(function (warning) {
              appendMetaLine(warning);
            });
          });
      });
    }

    var previewCancel = document.getElementById("preview-cancel");
    if (previewCancel && previewDialog) {
      previewCancel.addEventListener("click", function () {
        previewDialog.close();
      });
    }

    var previewSelectAll = document.getElementById("preview-select-all");
    if (previewSelectAll) {
      previewSelectAll.addEventListener("click", function () {
        previewItems.forEach(function (item) {
          item.checkbox.checked = true;
        });
        updatePreviewApply();
      });
    }

    var previewSelectNone = document.getElementById("preview-select-none");
    if (previewSelectNone) {
      previewSelectNone.addEventListener("click", function () {
        previewItems.forEach(function (item) {
          item.checkbox.checked = false;
        });
        updatePreviewApply();
      });
    }

    var previewApply = document.getElementById("preview-apply");
    if (previewApply) {
      previewApply.addEventListener("click", async function () {
        var selected = previewItems.filter(function (item) {
          return item.checkbox.checked;
        });
        if (!selected.length) {
          setText(document.getElementById("preview-status"),
            "请至少勾选一条改动。", true);
          return;
        }
        previewApply.disabled = true;
        setText(document.getElementById("preview-status"), "正在写入……", false);
        // 应用的就是用户勾选的那几条原样提交；服务端重新校验后一次性写入。
        // expect_* 与预览时的原值比对，变了就 409 preview_stale，一条都不写。
        var assignments = selected.map(function (item) {
          return {
            session_id: item.session_id,
            room_id: item.after.room_id || "",
            dm_id: item.after.dm_id || "",
            expect_room_id: item.before.room_id || "",
            expect_dm_id: item.before.dm_id || ""
          };
        });
        var result = await jsonPost("/api/schedule/apply", {
          assignments: assignments,
          confirm: true
        });
        updatePreviewApply();
        if (!result.response.ok || !result.data || !result.data.ok) {
          // 服务端全有或全无：一条都没写，保持弹窗打开让用户重新生成。
          setText(document.getElementById("preview-status"),
            messageFor(result.data, "应用失败，未写入任何改动。") +
            "请重新生成预览后再试。", true);
          return;
        }
        if (previewDialog) {
          previewDialog.close();
        }
        toast("已应用 " + assignments.length + " 条房间/DM 分配。");
        await refreshSchedule();
      });
    }

    var runDialog = document.getElementById("run-dialog");
    var runSessionId = null;
    var runCancel = document.getElementById("run-cancel");
    if (runCancel && runDialog) {
      runCancel.addEventListener("click", function () {
        runDialog.close();
      });
    }
    var runCreate = document.getElementById("run-create");
    if (runCreate) {
      runCreate.addEventListener("click", async function () {
        if (!runSessionId) {
          return;
        }
        runCreate.disabled = true;
        var result = await jsonPost("/api/runs", {
          session_id: runSessionId,
          dm_id: document.getElementById("run-dm").value || null,
          mode: document.getElementById("run-mode").value
        });
        runCreate.disabled = false;
        var box = document.getElementById("run-code-result");
        if (!box) {
          return;
        }
        box.classList.remove("hidden");
        if (result.response.ok && result.data && result.data.ok) {
          box.textContent = "场控码：" + result.data.code +
            "（有效期至 " + (result.data.code_expires_at || "—") + "，只显示这一次）";
        } else {
          box.classList.add("error-text");
          box.textContent = messageFor(result.data, "生成场控码失败。");
        }
      });
    }

    var assignDialog = document.getElementById("assign-dialog");
    var assignSessionId = null;
    var assignCancel = document.getElementById("assign-cancel");
    if (assignCancel && assignDialog) {
      assignCancel.addEventListener("click", function () {
        assignDialog.close();
      });
    }
    var assignConfirm = document.getElementById("assign-confirm");
    if (assignConfirm) {
      assignConfirm.addEventListener("click", async function () {
        if (!assignSessionId) {
          return;
        }
        var room = document.getElementById("assign-room").value || null;
        var dm = document.getElementById("assign-dm").value || null;
        if (!room && !dm) {
          setText(document.getElementById("assign-status"), "至少选择房间或 DM。", true);
          return;
        }
        var result = await jsonPost("/api/schedule/assign", {
          session_id: assignSessionId,
          room_id: room,
          dm_id: dm
        });
        if (result.response.ok && result.data && result.data.ok) {
          // 成功：关弹窗 + toast + 一次共享刷新，不整页 reload。
          assignDialog.close();
          toast("手工调整已保存。");
          await refreshSchedule();
          return;
        }
        setText(document.getElementById("assign-status"),
          messageFor(result.data, "调整失败。"), true);
      });
    }

    // ---- 时间看板（WP10）--------------------------------------------------
    // 客户端建板：Jinja 只出空壳，这里 fetch /api/schedule 后渲染 7 列时间轴。
    // 拖拽用 Pointer Events + CSSOM 定位（CSP 禁服务端 style 属性，
    // 运行时 el.style.top = ... 合法）。时间一律做 "YYYY-MM-DD HH:MM"
    // 字符串运算，不用 new Date() 解析，避开时区换算。
    function initTimeBoard() {
      var board = document.getElementById("time-board");
      if (!board) {
        return;
      }

      var AXIS_START_MIN = 360;   // 业务轴 06:00
      var AXIS_END_MIN = 1560;    // 26:00（次日 02:00）
      var PX_PER_MIN = 0.75;      // 20 小时 × 0.75 = 900px 列高
      var SNAP_MIN = 15;          // 拖拽落点按 15 分钟吸附
      var AXIS_HEIGHT = (AXIS_END_MIN - AXIS_START_MIN) * PX_PER_MIN;
      var MIN_CARD_H = 100;       // 卡片最小高度，保证按钮可点
      var CARD_BOTTOM_GAP = 44;   // 贴底时给卡片留出的可视余量

      var sessionCache = {};      // session_id → 场次载荷（回弹/重绘用）
      var moveBusy = {};          // session_id → PATCH 在途，期间禁止再拖同一张卡
      var drag = null;            // 当前拖拽状态机
      var suppressClick = false;  // 拖拽结束后的合成 click 先吞掉

      // 看板读契约 = GET /api/schedule 的窗口：拖拽不允许写到窗口外，
      // 否则场次会在下次 fetchBoard 时从板上消失且拖不回来。
      function windowStartText() {
        return board.getAttribute("data-window-start") || "";
      }

      function windowEndText() {
        return board.getAttribute("data-window-end") || "";
      }

      function withinWindow(startText) {
        if (!startText) {
          return false;
        }
        var start = windowStartText();
        var end = windowEndText();
        if (start && startText < start) {
          return false;
        }
        if (end && startText >= end) {
          return false;
        }
        return true;
      }

      // ---------- 时间 ↔ 位置 ----------
      // 场次时刻 → 所属业务列 + 轴内偏移。凌晨 0–5 点算前一天的夜场。
      // 日历运算（pad2/addDays 等）在 initSchedulePage 作用域，与建场工具栏共用。
      function businessPlacement(startStr) {
        var date = String(startStr || "").slice(0, 10);
        var minutes = Number(String(startStr).slice(11, 13)) * 60 +
          Number(String(startStr).slice(14, 16));
        if (minutes < AXIS_START_MIN) {
          date = addDays(date, -1);
          minutes += 1440;
        }
        var offset = minutes - AXIS_START_MIN;
        if (offset < 0) {
          offset = 0;
        }
        if (offset > AXIS_END_MIN - AXIS_START_MIN) {
          offset = AXIS_END_MIN - AXIS_START_MIN;
        }
        return { date: date, top: offset * PX_PER_MIN, minutes: minutes };
      }

      function shortTime(startStr) {
        return String(startStr || "").slice(11, 16);
      }

      // ---------- 渲染 ----------
      var loadedOnce = false;
      function fetchBoard() {
        var promise = loadSchedule(loadedOnce);
        loadedOnce = true;
        return promise.then(function (result) {
          if (!result.response.ok || !result.data || !result.data.ok) {
            clear(board);
            board.appendChild(el("p", "empty-state",
              "看板加载失败，请刷新页面重试。"));
            return;
          }
          renderBoard(result.data);
        });
      }

      function buildColumns(payload) {
        var baseDate = (board.getAttribute("data-window-start") || "").slice(0, 10);
        var dates = [];
        var seen = {};
        if (baseDate) {
          for (var i = 0; i < 7; i += 1) {
            var date = addDays(baseDate, i);
            dates.push(date);
            seen[date] = true;
          }
        }
        (payload.sessions || []).forEach(function (session) {
          var start = session.start_time || "";
          if (start.length < 10) {
            return;
          }
          var businessDate = businessPlacement(start).date;
          if (!seen[businessDate]) {
            seen[businessDate] = true;
            dates.push(businessDate);
          }
        });
        dates.sort();
        return dates;
      }

      function renderBoard(payload) {
        clear(board);
        sessionCache = {};
        var sessions = payload.sessions || [];
        sessions.forEach(function (session) {
          sessionCache[session.session_id] = session;
        });

        var grid = el("div", "kanban-grid");

        var axis = el("div", "kanban-axis");
        axis.appendChild(el("div", "kanban-axis-head"));
        var scale = el("div", "kanban-axis-scale");
        scale.style.height = AXIS_HEIGHT + "px";
        for (var hour = 6; hour <= 26; hour += 1) {
          var tick = el("div", "kanban-axis-tick", pad2(hour % 24) + ":00");
          tick.style.top = ((hour * 60 - AXIS_START_MIN) * PX_PER_MIN) + "px";
          scale.appendChild(tick);
        }
        axis.appendChild(scale);
        grid.appendChild(axis);

        buildColumns(payload).forEach(function (date) {
          var column = el("div", "kanban-col");
          column.setAttribute("data-date", date);
          var head = el("div", "kanban-col-head");
          head.appendChild(el("span", "kanban-col-date", date.slice(5)));
          head.appendChild(el("span", "kanban-col-week", weekdayOf(date)));
          column.appendChild(head);
          var body = el("div", "kanban-col-body");
          body.style.height = AXIS_HEIGHT + "px";
          for (var lineHour = 6; lineHour <= 26; lineHour += 1) {
            var line = el("div", "kanban-hour-line");
            line.style.top = ((lineHour * 60 - AXIS_START_MIN) * PX_PER_MIN) + "px";
            body.appendChild(line);
          }
          column.appendChild(body);
          grid.appendChild(column);
        });

        board.appendChild(grid);

        if (!sessions.length) {
          board.appendChild(el("p", "empty-state",
            "这个时间段没有场次。点空位弹出建场卡片，或点右上角「新增场次」。"));
          return;
        }
        sessions.forEach(function (session) {
          var body = bodyForDate(businessPlacement(session.start_time).date);
          if (!body) {
            return;
          }
          var card = buildCard(session);
          body.appendChild(card);
          positionCard(card, session.start_time, session.duration_minutes);
        });
      }

      function bodyForDate(date) {
        return board.querySelector(
          '.kanban-col[data-date="' + date + '"] .kanban-col-body');
      }

      function buildActions(card, info) {
        var box = card.querySelector(".kanban-card-actions");
        if (!box) {
          box = el("div", "kanban-card-actions");
          card.appendChild(box);
        }
        clear(box);
        if (info.hasRoom) {
          var roomButton = el("button", "secondary small", "换房");
          roomButton.type = "button";
          roomButton.setAttribute("data-change-room", info.id);
          box.appendChild(roomButton);
        }
        if (info.hasDm) {
          var dmButton = el("button", "secondary small", "换 DM");
          dmButton.type = "button";
          dmButton.setAttribute("data-change-dm", info.id);
          box.appendChild(dmButton);
        }
        var runButton = el("button", "primary small", "场控");
        runButton.type = "button";
        runButton.title = "生成场控码";
        runButton.setAttribute("data-create-run", info.id);
        box.appendChild(runButton);
      }

      function buildCard(session) {
        var statusText = session.status || "待开场";
        var card = el("article", "kanban-card");
        card.setAttribute("data-session", session.session_id);
        card.setAttribute("data-status", statusText);
        if (statusText === "已取消") {
          // 已取消：只做展示，不可拖、不给操作按钮（纠错走状态看板）。
          card.classList.add("kanban-card--cancelled");
        }

        var grip = el("div", "kanban-card-grip");
        grip.appendChild(el("span", "kanban-card-time", shortTime(session.start_time)));
        grip.appendChild(el("span", "kanban-card-dur",
          (session.duration_minutes || 240) + " 分"));
        card.appendChild(grip);

        card.appendChild(el("strong", "kanban-card-title",
          session.script_name || "未命名"));

        var meta = el("div", "kanban-card-meta");
        meta.textContent = (session.room_name || "待排") + " / " +
          (session.dm_name || "待排");
        card.appendChild(meta);

        var status = el("div", "kanban-card-status");
        var chip;
        if (statusText === "已取消") {
          chip = el("span", "chip", "已取消");
        } else if (statusText === "已开场") {
          chip = el("span", "chip info", "已开场");
        } else {
          chip = el("span", "chip " +
            (session.room_id && session.dm_id ? "ok" : "warn"),
            session.room_id && session.dm_id ? "已排" : "待排");
        }
        status.appendChild(chip);
        var seats = el("span", "muted kanban-card-seats",
          "已约 " + (session.booked_seats || 0) + " / " +
          (session.total_seats != null ? session.total_seats : "?"));
        status.appendChild(seats);
        card.appendChild(status);

        if (statusText !== "已取消") {
          buildActions(card, {
            id: session.session_id,
            hasRoom: Boolean(session.room_id),
            hasDm: Boolean(session.dm_id)
          });
        }
        card.appendChild(el("div", "kanban-chips"));
        return card;
      }

      // 高度按时长走；内容（如冲突 chips）更高时只增不减，避免裁切。
      function fitCard(card, durationMinutes) {
        var minutes = Number(durationMinutes);
        if (!(minutes > 0)) {
          minutes = 240;
        }
        var target = Math.max(minutes * PX_PER_MIN, MIN_CARD_H);
        card.style.height = target + "px";
        var needed = card.scrollHeight;
        if (needed > target) {
          card.style.height = needed + "px";
        }
      }

      // 同列时间重叠的卡片分道并排，避免完全盖住彼此。
      function relayoutColumn(body) {
        var cards = body.querySelectorAll(".kanban-card");
        var items = [];
        var index;
        for (index = 0; index < cards.length; index += 1) {
          var item = cards[index];
          var top = Number(item.style.top) || 0;
          var height = Number(item.style.height) || MIN_CARD_H;
          items.push({ node: item, top: top, height: height });
        }
        items.sort(function (a, b) { return a.top - b.top; });
        var lanes = []; // 每道的最后底边
        items.forEach(function (entry) {
          var lane = 0;
          while (lane < lanes.length && lanes[lane] > entry.top) {
            lane += 1;
          }
          if (lane === lanes.length) {
            lanes.push(0);
          }
          entry.lane = lane;
          lanes[lane] = entry.top + entry.height + 4;
        });
        var total = Math.max(lanes.length, 1);
        items.forEach(function (entry) {
          if (total === 1) {
            entry.node.style.left = "";
            entry.node.style.right = "";
            return;
          }
          entry.node.style.left =
            "calc(4px + " + entry.lane + " * (100% - 8px) / " + total + ")";
          entry.node.style.right =
            "calc(4px + " + (total - 1 - entry.lane) + " * (100% - 8px) / " + total + ")";
        });
      }

      function positionCard(card, startStr, durationMinutes) {
        var place = businessPlacement(startStr);
        var body = bodyForDate(place.date);
        if (!body) {
          body = board.querySelector(".kanban-col-body");
        }
        if (!body) {
          return;
        }
        if (card.parentNode !== body) {
          body.appendChild(card);
        }
        fitCard(card, durationMinutes);
        // 先定高度再夹取：贴底时整张卡（含按钮/chips）都留在 900px 内。
        var height = Number(card.style.height) || MIN_CARD_H;
        var top = Math.min(place.top, AXIS_HEIGHT - height);
        if (top < 0) {
          top = 0;
        }
        card.style.top = top + "px";
        relayoutColumn(body);
      }

      function renderChips(card, conflicts) {
        var box = card.querySelector(".kanban-chips");
        if (!box) {
          box = el("div", "kanban-chips");
          card.appendChild(box);
        }
        clear(box);
        var seen = {};
        (conflicts || []).forEach(function (conflict) {
          (conflict.options || []).forEach(function (option) {
            var type = option.type || "";
            var key = type + "|" + (option.target_id || "") + "|" +
              (option.new_start || "");
            if (seen[key]) {
              return;
            }
            seen[key] = true;
            var label = option.label || type;
            var short = label.length > 20 ? label.slice(0, 19) + "…" : label;
            if ((type === "switch_room" || type === "switch_dm") && option.target_id) {
              var apply = el("button", "kanban-chip kanban-chip--btn", short);
              apply.type = "button";
              apply.title = label;
              apply.setAttribute("data-resolve-choice", type);
              apply.setAttribute("data-target-id", option.target_id);
              if (option.move_session_id) {
                // 建议换的是另一场：resolve 必须作用在被移动的那张卡上。
                apply.setAttribute("data-move-session", option.move_session_id);
              }
              box.appendChild(apply);
            } else if (type === "shift_time" && option.new_start) {
              var shift = el("button",
                "kanban-chip kanban-chip--btn kanban-chip--suggest",
                "建议改到 " + option.new_start.slice(11, 16));
              shift.type = "button";
              shift.title = label;
              shift.setAttribute("data-shift-to", option.new_start);
              box.appendChild(shift);
            } else {
              var info = el("span", "kanban-chip kanban-chip--info", short);
              info.title = label;
              box.appendChild(info);
            }
          });
        });
        fitCard(card, (sessionCache[card.getAttribute("data-session")] ||
          {}).duration_minutes);
      }

      // ---------- 拖拽状态机 ----------
      function cancelPendingDrag() {
        if (!drag) {
          return;
        }
        if (drag.timer) {
          window.clearTimeout(drag.timer);
        }
        try {
          board.releasePointerCapture(drag.pointerId);
        } catch (error) {
          // 指针可能已释放，忽略。
        }
        drag = null;
      }

      function beginDrag() {
        if (!drag || drag.started) {
          return;
        }
        drag.started = true;
        if (drag.timer) {
          window.clearTimeout(drag.timer);
          drag.timer = null;
        }
        drag.card.classList.add("kanban-card--dragging");
        // 拖拽中接管手势（页面/看板不再滚动）；CSSOM 写法符合 CSP。
        drag.card.style.touchAction = "none";
        var ghost = drag.card.cloneNode(true);
        ghost.classList.remove("kanban-card--dragging");
        ghost.classList.add("kanban-ghost");
        ghost.style.position = "fixed";
        ghost.style.left = drag.rect.left + "px";
        ghost.style.top = drag.rect.top + "px";
        ghost.style.width = drag.rect.width + "px";
        ghost.style.height = drag.rect.height + "px";
        ghost.style.pointerEvents = "none";
        var label = el("div", "kanban-ghost-time", "");
        ghost.appendChild(label);
        drag.ghost = ghost;
        drag.timeLabel = label;
        document.body.appendChild(ghost);
      }

      function bodyAt(x, y) {
        var target = document.elementFromPoint(x, y);
        if (!target || !target.closest) {
          return null;
        }
        return target.closest(".kanban-col-body");
      }

      function dropInfoAt(x, y, body) {
        var rect = body.getBoundingClientRect();
        var rawOffset = (y - rect.top) / PX_PER_MIN;
        var snapped = Math.round(rawOffset / SNAP_MIN) * SNAP_MIN;
        var maxOffset = AXIS_END_MIN - AXIS_START_MIN;
        if (snapped < 0) {
          snapped = 0;
        }
        if (snapped > maxOffset) {
          snapped = maxOffset;
        }
        var total = AXIS_START_MIN + snapped;           // 360..1560
        var columnDate = body.parentNode.getAttribute("data-date");
        var dayShift = Math.floor(total / 1440);        // 26:00 落到次日
        var inDay = total % 1440;
        var hour = Math.floor(inDay / 60);
        var minute = inDay % 60;
        var date = columnDate;
        if (dayShift > 0) {
          date = addDays(columnDate, dayShift);
        }
        var timeText = pad2(hour) + ":" + pad2(minute);
        return {
          start: date + " " + timeText,
          label: date.slice(5) + " " + timeText
        };
      }

      function moveGhost(x, y) {
        if (!drag || !drag.ghost) {
          return;
        }
        drag.ghost.style.left = (x - drag.grabDX) + "px";
        drag.ghost.style.top = (y - drag.grabDY) + "px";
      }

      function highlightTarget(x, y) {
        var body = bodyAt(x, y);
        if (!body) {
          return; // 指针在列头/轴上：保留上一次的高亮
        }
        if (drag.targetBody && drag.targetBody !== body) {
          drag.targetBody.classList.remove("kanban-col-body--active");
        }
        body.classList.add("kanban-col-body--active");
        drag.targetBody = body;
        var info = dropInfoAt(x, y, body);
        drag.dropStart = info.start;
        if (drag.timeLabel) {
          drag.timeLabel.textContent = info.label;
        }
      }

      function endDragState(state) {
        if (state.timer) {
          window.clearTimeout(state.timer);
        }
        if (state.ghost && state.ghost.parentNode) {
          state.ghost.parentNode.removeChild(state.ghost);
        }
        state.card.classList.remove("kanban-card--dragging");
        state.card.style.touchAction = "";
        if (state.targetBody) {
          state.targetBody.classList.remove("kanban-col-body--active");
        }
        try {
          board.releasePointerCapture(state.pointerId);
        } catch (error) {
          // 已释放则忽略。
        }
      }

      function onPointerDown(event) {
        suppressClick = false; // 新手势开始，解除上一次拖拽的 click 吞并
        if (!event.target || !event.target.closest) {
          return;
        }
        if (event.pointerType === "mouse" && event.button !== 0) {
          return;
        }
        var card = event.target.closest("[data-session]");
        if (!card || !board.contains(card)) {
          return;
        }
        // 已取消的场次不参与拖拽（纠错走状态看板/对话）。
        if (card.getAttribute("data-status") === "已取消") {
          return;
        }
        var busyId = card.getAttribute("data-session");
        if (moveBusy[busyId]) {
          return; // 上一笔改期还没落库：忽略新手势，避免并发写。
        }
        // 按钮/chip 走正常点击，不进入拖拽。
        if (event.target.closest("button, a, input, select")) {
          return;
        }
        if (drag) {
          return;
        }
        var rect = card.getBoundingClientRect();
        drag = {
          pointerId: event.pointerId,
          card: card,
          sessionId: card.getAttribute("data-session"),
          startX: event.clientX,
          startY: event.clientY,
          grabDX: event.clientX - rect.left,
          grabDY: event.clientY - rect.top,
          rect: rect,
          started: false,
          armed: false,
          timer: null,
          pointerType: event.pointerType,
          dropStart: null,
          targetBody: null,
          ghost: null
        };
        if (event.pointerType === "mouse") {
          // 鼠标：位移超过 6px 才算拖拽，避免误触。
          drag.armed = false;
        } else {
          // 触摸/笔：长按 250ms 才进入拖拽，给点击和滚动留机会。
          drag.armed = false;
          drag.timer = window.setTimeout(function () {
            if (drag && !drag.started) {
              drag.armed = true;
            }
          }, 250);
        }
        try {
          board.setPointerCapture(event.pointerId);
        } catch (error) {
          // 某些浏览器不支持时退化为普通监听。
        }
      }

      function onPointerMove(event) {
        if (!drag || event.pointerId !== drag.pointerId) {
          return;
        }
        var dx = event.clientX - drag.startX;
        var dy = event.clientY - drag.startY;
        var distance = Math.sqrt(dx * dx + dy * dy);
        if (!drag.started) {
          if (drag.pointerType === "mouse") {
            if (distance < 6) {
              return;
            }
            drag.armed = true;
          } else if (!drag.armed) {
            if (distance > 10) {
              // 长按前先移动：放弃本次拖拽，不拦截页面滚动意图。
              cancelPendingDrag();
            }
            return;
          }
          if (!drag.armed) {
            return;
          }
          beginDrag();
        }
        if (!drag || !drag.started) {
          return;
        }
        event.preventDefault();
        moveGhost(event.clientX, event.clientY);
        highlightTarget(event.clientX, event.clientY);
      }

      function finishDrag() {
        var state = drag;
        drag = null;
        if (!state) {
          return;
        }
        var started = state.started;
        var dropStart = state.dropStart;
        var sessionId = state.sessionId;
        var card = state.card;
        endDragState(state);
        if (!started) {
          return; // 按下后没到拖拽阈值：当普通点击。
        }
        // 合成 click 会在 pointerup 后补发：记旗标吞掉，
        // 下一次 pointerdown 或 100ms 后自动解除。
        suppressClick = true;
        window.setTimeout(function () {
          suppressClick = false;
        }, 100);
        var cached = sessionCache[sessionId] || {};
        var originalStart = cached.start_time;
        if (!dropStart || !originalStart || dropStart === originalStart) {
          return; // 没落到新时间：卡片本来就没动。
        }
        if (!withinWindow(dropStart)) {
          // 落点在看板读取窗口外：下一次 fetchBoard 会看不见它。
          toast("不能排到看板窗口之外（" +
            windowStartText().slice(0, 16) + " 起）。", true);
          positionCard(card, originalStart, cached.duration_minutes);
          return;
        }
        // 乐观移动：先落到新位置，请求成功后用响应回读校正。
        positionCard(card, dropStart, cached.duration_minutes);
        moveSession(card, sessionId, dropStart);
      }

      function onPointerUp(event) {
        if (!drag || event.pointerId !== drag.pointerId) {
          return;
        }
        finishDrag();
      }

      function onPointerCancel(event) {
        if (!drag || event.pointerId !== drag.pointerId) {
          return;
        }
        var state = drag;
        drag = null;
        endDragState(state); // 浏览器接管手势：只清理，不发请求。
      }

      // ---------- 落库 / 回弹 ----------
      async function moveSession(card, sessionId, newStart) {
        if (moveBusy[sessionId]) {
          return; // 同一场并发只保留第一笔。
        }
        moveBusy[sessionId] = true;
        // 乐观写入缓存：拖拽期间的重复比较都要看到新时间；
        // 失败时回滚到 prevStart。
        var before = sessionCache[sessionId] || {};
        var prevStart = before.start_time;
        var optimistic = {};
        var key;
        for (key in before) {
          if (Object.prototype.hasOwnProperty.call(before, key)) {
            optimistic[key] = before[key];
          }
        }
        optimistic.start_time = newStart;
        sessionCache[sessionId] = optimistic;
        try {
          var result = await jsonPost(
            "/api/sessions/" + encodeURIComponent(sessionId),
            { start_time: newStart },
            "PATCH"
          );
          if (!result.response.ok || !result.data || !result.data.ok) {
            // 失败回弹：缓存与卡片都回到拖拽前。
            if (prevStart) {
              sessionCache[sessionId] = before;
              positionCard(card, prevStart, before.duration_minutes);
            } else {
              delete sessionCache[sessionId];
            }
            toast(messageFor(result.data, "改期失败，卡片已回到原位。"), true);
            return;
          }
          var data = result.data;
          var updated = data.session || {};
          var newId = updated.session_id || sessionId;
          if (newId !== sessionId) {
            delete sessionCache[sessionId];
            card.setAttribute("data-session", newId);
          }
          // 响应里的 session 没有时长/名称，合并进缓存再重绘。
          updated.duration_minutes = updated.duration_minutes ||
            before.duration_minutes;
          updated.room_name = updated.room_name || before.room_name;
          updated.dm_name = updated.dm_name || before.dm_name;
          updated.booked_seats = updated.booked_seats != null
            ? updated.booked_seats : before.booked_seats;
          sessionCache[newId] = updated;

        positionCard(card, updated.start_time || newStart,
          updated.duration_minutes);
        var timeLabel = card.querySelector(".kanban-card-time");
        if (timeLabel) {
          timeLabel.textContent = shortTime(updated.start_time || newStart);
        }
        var seatsLabel = card.querySelector(".kanban-card-seats");
        if (seatsLabel) {
          seatsLabel.textContent = "已约 " + (updated.booked_seats || 0) +
            " / " + (updated.total_seats != null ? updated.total_seats : "?");
        }
        var auto = data.auto_assign || {};
        var assigned = auto.assigned && auto.assigned[0];
        if (assigned) {
          // 自动重排成功：刷新房间/DM 展示与按钮。
          var meta = card.querySelector(".kanban-card-meta");
          if (meta) {
            meta.textContent = (assigned.room_name || assigned.room_id || "待排") +
              " / " + (assigned.dm_name || assigned.dm_id || "待排");
          }
          var chip = card.querySelector(".kanban-card-status .chip");
          if (chip) {
            chip.textContent = "已排";
            chip.classList.remove("warn");
            chip.classList.add("ok");
          }
          buildActions(card, {
            id: newId,
            hasRoom: Boolean(assigned.room_id),
            hasDm: Boolean(assigned.dm_id)
          });
        }
        renderChips(card, auto.conflicts || []);
        if (data.session_id_changed) {
          toast("场次编号已更新");
        }
        (data.warnings || []).forEach(function (warning) {
          toast(warning);
        });
        if (auto.status === "conflict" && !(auto.conflicts || []).length) {
          toast("新时间资源紧张，尚未完全分配。", true);
        }
        } finally {
          delete moveBusy[sessionId];
        }
      }

      // ---------- 冲突建议 chips 的动作 ----------
      async function handleResolve(card, sessionId, choice, targetId) {
        var result = await jsonPost("/api/schedule/resolve", {
          session_id: sessionId,
          choice: choice,
          target_id: targetId || null
        });
        if (result.response.ok && result.data && result.data.ok) {
          if (result.data.status === "applied" || result.data.status === "kept") {
            toast("已按建议调整分配。");
          } else {
            toast(messageFor(result.data,
              result.data.message || "该建议暂时无法应用。"), true);
          }
          await refreshSchedule();
          return;
        }
        toast(messageFor(result.data, "处理建议失败。"), true);
      }

      async function handleShift(card, sessionId, newStart) {
        if (!withinWindow(newStart)) {
          toast("不能排到看板窗口之外（" +
            windowStartText().slice(0, 16) + " 起）。", true);
          return;
        }
        if (!window.confirm("改到 " + newStart.slice(11, 16) +
          "？开场时间只会因你的确认而改动。")) {
          return;
        }
        var cached = sessionCache[sessionId] || {};
        positionCard(card, newStart, cached.duration_minutes);
        await moveSession(card, sessionId, newStart);
      }

      // ---------- 板级事件（容器委托；场次改名后依然生效） ----------
      board.addEventListener("click", function (event) {
        if (suppressClick) {
          suppressClick = false;
          event.preventDefault();
          event.stopPropagation();
          return;
        }
        if (!event.target || !event.target.closest) {
          return;
        }
        var card = event.target.closest("[data-session]");
        if (!card || !board.contains(card)) {
          // 空位点击：弹出建场卡片并带上日期/时间（15 分钟吸附）。
          var slotBody = event.target.closest(".kanban-col-body");
          if (slotBody) {
            var slotInfo = dropInfoAt(event.clientX, event.clientY, slotBody);
            openCreateDialog(slotInfo.start);
          }
          return;
        }
        var sessionId = card.getAttribute("data-session");

        var resolveButton = event.target.closest("[data-resolve-choice]");
        if (resolveButton) {
          // 建议可能指向另一场（move_session_id）：作用在那一场上。
          var moveSessionId = resolveButton.getAttribute("data-move-session");
          handleResolve(card, moveSessionId || sessionId,
            resolveButton.getAttribute("data-resolve-choice"),
            resolveButton.getAttribute("data-target-id"));
          return;
        }
        var shiftButton = event.target.closest("[data-shift-to]");
        if (shiftButton) {
          handleShift(card, sessionId, shiftButton.getAttribute("data-shift-to"));
          return;
        }
        if (event.target.closest("[data-change-room], [data-change-dm]")) {
          assignSessionId = sessionId;
          if (assignDialog && typeof assignDialog.showModal === "function") {
            assignDialog.showModal();
          }
          return;
        }
        if (event.target.closest("[data-create-run]")) {
          runSessionId = sessionId;
          var runResult = document.getElementById("run-code-result");
          if (runResult) {
            runResult.classList.add("hidden");
            runResult.textContent = "";
          }
          if (runDialog && typeof runDialog.showModal === "function") {
            runDialog.showModal();
          }
        }
      });

      board.addEventListener("pointerdown", onPointerDown);
      board.addEventListener("pointermove", onPointerMove);
      board.addEventListener("pointerup", onPointerUp);
      board.addEventListener("pointercancel", onPointerCancel);

      // refreshSchedule 用同一份 payload 原地重绘本板（不再整页刷新）。
      board._render = renderBoard;
      fetchBoard();
    }

    // ---------- 状态看板：待开场 / 已开场 / 已取消 ----------
    function initStatusBoard() {
      var board = document.getElementById("status-board");
      if (!board) {
        return;
      }
      var COLUMNS = ["待开场", "已开场", "已取消"];
      var cache = {};        // session_id → 场次载荷
      var drag = null;
      var suppressClick = false;
      var busy = {};         // id → 转换在途
      var loadedOnce = false;

      function fetchBoard() {
        var promise = loadSchedule(loadedOnce);
        loadedOnce = true;
        return promise.then(function (result) {
          if (!result.response.ok || !result.data || !result.data.ok) {
            clear(board);
            board.appendChild(el("p", "empty-state",
              "状态看板加载失败，请刷新页面重试。"));
            return;
          }
          render(result.data);
        });
      }

      function columnBody(status) {
        return board.querySelector(
          '.status-col[data-status="' + status + '"] .status-col-body');
      }

      function buildCard(item) {
        var card = el("article", "status-card");
        card.setAttribute("data-session", item.session_id);
        card.setAttribute("data-status", item.status || "待开场");
        var head = el("div", "resource-head");
        head.appendChild(el("strong", "", item.script_name || "未命名"));
        head.appendChild(el("span", "chip " + (item.status === "已取消" ? "" :
          (item.status === "已开场" ? "info" : "ok")), item.status || "待开场"));
        card.appendChild(head);
        card.appendChild(el("p", "muted mono", item.start_time || ""));
        card.appendChild(el("p", "muted",
          "已约 " + (item.booked_seats || 0) + " / " +
          (item.total_seats != null ? item.total_seats : "?")));
        return card;
      }

      function render(payload) {
        clear(board);
        cache = {};
        var grid = el("div", "status-grid");
        COLUMNS.forEach(function (status) {
          var col = el("div", "status-col");
          col.setAttribute("data-status", status);
          var head = el("div", "status-col-head");
          head.appendChild(el("span", "", status));
          var count = (payload.sessions || []).filter(function (session) {
            return (session.status || "待开场") === status;
          }).length;
          head.appendChild(el("span", "status-col-count", String(count)));
          col.appendChild(head);
          col.appendChild(el("div", "status-col-body"));
          grid.appendChild(col);
        });
        board.appendChild(grid);
        (payload.sessions || []).forEach(function (session) {
          cache[session.session_id] = session;
          var body = columnBody(session.status || "待开场");
          if (body) {
            body.appendChild(buildCard(session));
          }
        });
        if (!(payload.sessions || []).length) {
          board.appendChild(el("p", "empty-state",
            "没有场次。点「新增场次」或看板空位建一场，或在对话里录入。"));
        }
      }

      // ---------- 拖拽换列（Pointer Events；与时间看板同套手势阈值） ----------
      function cancelPendingDrag() {
        if (!drag) {
          return;
        }
        if (drag.timer) {
          window.clearTimeout(drag.timer);
        }
        drag = null;
      }

      function beginDrag() {
        if (!drag || drag.started) {
          return;
        }
        drag.started = true;
        if (drag.timer) {
          window.clearTimeout(drag.timer);
          drag.timer = null;
        }
        drag.card.classList.add("kanban-card--dragging");
        var ghost = drag.card.cloneNode(true);
        ghost.classList.remove("kanban-card--dragging");
        ghost.classList.add("kanban-ghost");
        ghost.style.position = "fixed";
        ghost.style.left = drag.rect.left + "px";
        ghost.style.top = drag.rect.top + "px";
        ghost.style.width = drag.rect.width + "px";
        ghost.style.pointerEvents = "none";
        drag.ghost = ghost;
        document.body.appendChild(ghost);
      }

      function colAt(x, y) {
        var target = document.elementFromPoint(x, y);
        if (!target || !target.closest) {
          return null;
        }
        return target.closest(".status-col");
      }

      function highlightTarget(x, y) {
        var col = colAt(x, y);
        if (drag.targetCol && drag.targetCol !== col) {
          drag.targetCol.classList.remove("status-col--active");
        }
        if (col) {
          col.classList.add("status-col--active");
        }
        drag.targetCol = col;
      }

      function endDragState(state) {
        if (state.timer) {
          window.clearTimeout(state.timer);
        }
        if (state.ghost && state.ghost.parentNode) {
          state.ghost.parentNode.removeChild(state.ghost);
        }
        state.card.classList.remove("kanban-card--dragging");
        if (state.targetCol) {
          state.targetCol.classList.remove("status-col--active");
        }
      }

      function onPointerDown(event) {
        suppressClick = false;
        if (!event.target || !event.target.closest) {
          return;
        }
        if (event.pointerType === "mouse" && event.button !== 0) {
          return;
        }
        var card = event.target.closest(".status-card");
        if (!card || !board.contains(card)) {
          return;
        }
        if (event.target.closest("button, a, input, select")) {
          return;
        }
        if (drag) {
          return;
        }
        var id = card.getAttribute("data-session");
        if (busy[id]) {
          return;
        }
        var rect = card.getBoundingClientRect();
        drag = {
          pointerId: event.pointerId,
          card: card,
          id: id,
          fromStatus: card.getAttribute("data-status"),
          startX: event.clientX,
          startY: event.clientY,
          rect: rect,
          started: false,
          armed: false,
          timer: null,
          pointerType: event.pointerType,
          targetCol: null,
          ghost: null
        };
        if (event.pointerType === "mouse") {
          drag.armed = false;
        } else {
          drag.armed = false;
          drag.timer = window.setTimeout(function () {
            if (drag && !drag.started) {
              drag.armed = true;
            }
          }, 250);
        }
        try {
          board.setPointerCapture(event.pointerId);
        } catch (error) {
          // 某些浏览器不支持时退化为普通监听。
        }
      }

      function onPointerMove(event) {
        if (!drag || event.pointerId !== drag.pointerId) {
          return;
        }
        var dx = event.clientX - drag.startX;
        var dy = event.clientY - drag.startY;
        var distance = Math.sqrt(dx * dx + dy * dy);
        if (!drag.started) {
          if (drag.pointerType === "mouse") {
            if (distance < 6) {
              return;
            }
            drag.armed = true;
          } else if (!drag.armed) {
            if (distance > 10) {
              cancelPendingDrag();
            }
            return;
          }
          if (!drag.armed) {
            return;
          }
          beginDrag();
        }
        if (!drag || !drag.started) {
          return;
        }
        event.preventDefault();
        if (drag.ghost) {
          drag.ghost.style.left = (event.clientX - drag.rect.width / 2) + "px";
          drag.ghost.style.top = (event.clientY - 20) + "px";
        }
        highlightTarget(event.clientX, event.clientY);
      }

      function finishDrag() {
        var state = drag;
        drag = null;
        if (!state) {
          return;
        }
        var started = state.started;
        var target = state.targetCol;
        endDragState(state);
        if (!started) {
          return;
        }
        suppressClick = true;
        window.setTimeout(function () {
          suppressClick = false;
        }, 100);
        if (!target) {
          return;
        }
        var toStatus = target.getAttribute("data-status");
        if (!toStatus || toStatus === state.fromStatus) {
          return;
        }
        convert(state, toStatus);
      }

      // ---------- 状态转换（全部走既有端点；每次转换后全量重绘） ----------
      function convert(state, toStatus) {
        var from = state.fromStatus;
        if (from === "已取消" && toStatus === "已开场") {
          toast("已取消的场次不能直接标记已开场，请先恢复待开场。", true);
          return;
        }
        var note = toStatus === "已开场"
          ? "\n注意：场控计时与剧情推进仍归 DM 端控制，这里只改场次状态。"
          : "";
        if (!window.confirm("把「" + (cache[state.id] || {}).script_name +
          "」从" + from + "改为" + toStatus + "？" + note)) {
          return;
        }
        changeStatus(state.id, toStatus);
      }

      function withBusy(id, work) {
        if (busy[id]) {
          return Promise.resolve();
        }
        busy[id] = true;
        return work().then(function () {
          delete busy[id];
          refreshSchedule();
        }, function () {
          delete busy[id];
          refreshSchedule();
        });
      }

      function changeStatus(sessionId, toStatus) {
        withBusy(sessionId, function () {
          return jsonPost(
            "/api/sessions/" + encodeURIComponent(sessionId),
            { status: toStatus },
            "PATCH"
          ).then(function (result) {
            if (result.response.ok && result.data && result.data.ok) {
              toast("状态已更新为" + toStatus + "。");
            } else {
              toast(messageFor(result.data, "改状态失败。"), true);
            }
          });
        });
      }

      board.addEventListener("click", function (event) {
        if (suppressClick) {
          suppressClick = false;
          event.preventDefault();
          event.stopPropagation();
        }
      });
      board.addEventListener("pointerdown", onPointerDown);
      board.addEventListener("pointermove", onPointerMove);
      board.addEventListener("pointerup", finishDrag);
      board.addEventListener("pointercancel", function (event) {
        if (!drag || event.pointerId !== drag.pointerId) {
          return;
        }
        var state = drag;
        drag = null;
        endDragState(state);
      });

      // refreshSchedule 用同一份 payload 原地重绘本板（不再整页刷新）。
      board._render = render;
      fetchBoard();
    }

    initTimeBoard();
    initStatusBoard();

    // ---------- 看板 tab 切换 ----------
    var tabButtons = document.querySelectorAll("[data-board-tab]");
    var timeBoard = document.getElementById("time-board");
    var statusBoardEl = document.getElementById("status-board");
    var boardHint = document.getElementById("board-hint");
    tabButtons.forEach(function (button) {
      button.addEventListener("click", function () {
        var which = button.getAttribute("data-board-tab");
        tabButtons.forEach(function (other) {
          var active = other === button;
          other.classList.toggle("board-tab--active", active);
          other.setAttribute("aria-selected", active ? "true" : "false");
        });
        if (timeBoard) {
          timeBoard.classList.toggle("hidden", which !== "time");
        }
        if (statusBoardEl) {
          statusBoardEl.classList.toggle("hidden", which !== "status");
          if (which === "status") {
            // 切到状态看板时共享刷新：另一块看板可能刚做过转换。
            refreshSchedule();
          }
        }
        if (boardHint) {
          boardHint.textContent = which === "time"
            ? "拖动卡片改期，按 15 分钟对齐"
            : "拖动卡片换状态；转换前会请你确认";
        }
      });
    });
  }

  function initImportsPage() {
    var root = document.querySelector("[data-imports-page]");
    if (!root) {
      return;
    }
    var quickForm = document.getElementById("quick-booking-form");
    if (quickForm) {
      quickForm.addEventListener("submit", async function (event) {
        event.preventDefault();
        var sessionId = document.getElementById("quick-session").value;
        var seats = Number(document.getElementById("quick-seats").value);
        var ref = "quick-" + (
          window.crypto && window.crypto.randomUUID
            ? window.crypto.randomUUID()
            : String(Date.now()) + "-" + String(Math.random()).slice(2)
        );
        var statusEl = document.getElementById("quick-status");
        var result = await jsonPost("/api/bookings/quick", {
          session_id: sessionId,
          seats: seats,
          client_ref: ref,
          player_name: document.getElementById("quick-name").value.trim() || null
        });
        if (result.response.ok && result.data && result.data.ok) {
          setText(statusEl, "已记 " + seats + " 人。");
          window.setTimeout(function () {
            window.location.reload();
          }, 700);
          return;
        }
        setText(statusEl, messageFor(result.data, "记单失败。"), true);
      });
    }

    var previewForm = document.getElementById("dianping-preview-form");
    var commitButton = document.getElementById("dianping-commit");
    var fileInput = document.getElementById("dianping-file");
    var resultBox = document.getElementById("dianping-result");

    function renderPreview(data, errorText) {
      if (!resultBox) {
        return;
      }
      resultBox.classList.remove("hidden");
      clear(resultBox);
      if (errorText) {
        resultBox.appendChild(el("p", "error-text", errorText));
        return;
      }
      resultBox.appendChild(el("p", null,
        "共 " + data.total_rows + " 行，可导入 " + data.valid_rows + " 行；数据截止 " +
        (data.data_as_of || "—")));
      if (data.stale) {
        resultBox.appendChild(el("p", "notice warn", "数据已过期，建议重新导出后再导入。"));
      }
      (data.errors || []).slice(0, 8).forEach(function (item) {
        resultBox.appendChild(el("p", "error-text", "错误：" + (item.error || item)));
      });
      (data.unmatched || []).forEach(function (item) {
        resultBox.appendChild(el("p", "notice warn",
          "未匹配场次：" + (item.script_name || item.session_id || item)));
      });
      (data.over_capacity || []).forEach(function (item) {
        resultBox.appendChild(el("p", "notice warn",
          "超容量，整场拒绝：" + (item.script_name || item.session_id || item)));
      });
      (data.warnings || []).forEach(function (warning) {
        resultBox.appendChild(el("p", "muted", warning));
      });
    }

    function formData(confirm) {
      var data = new FormData();
      data.append("file", fileInput.files[0]);
      var asOf = document.getElementById("dianping-as-of").value;
      if (asOf) {
        data.append("data_as_of", asOf);
      }
      data.append("confirm", confirm ? "true" : "false");
      return data;
    }

    if (previewForm) {
      previewForm.addEventListener("submit", async function (event) {
        event.preventDefault();
        if (!fileInput.files || !fileInput.files[0]) {
          renderPreview({}, "请选择订单文件。");
          return;
        }
        if (commitButton) {
          commitButton.disabled = true;
        }
        var result = await apiFetch("/api/imports/dianping/preview", {
          method: "POST",
          body: formData(false)
        });
        if (!result.response.ok || !result.data || !result.data.ok) {
          renderPreview({}, messageFor(result.data, "预览失败。"));
          return;
        }
        renderPreview(result.data, null);
        if (commitButton) {
          commitButton.disabled = false;
        }
      });
    }

    if (commitButton) {
      commitButton.addEventListener("click", async function () {
        if (!fileInput.files || !fileInput.files[0]) {
          return;
        }
        if (!window.confirm("确认导入？超容量的场次会被整场拒绝。")) {
          return;
        }
        commitButton.disabled = true;
        var result = await apiFetch("/api/imports/dianping/commit", {
          method: "POST",
          body: formData(true)
        });
        if (!result.response.ok || !result.data || !result.data.ok) {
          renderPreview({}, messageFor(result.data, "导入失败。"));
          commitButton.disabled = false;
          return;
        }
        var data = result.data;
        if (resultBox) {
          resultBox.classList.remove("hidden");
          clear(resultBox);
          resultBox.appendChild(el("p", null,
            "导入完成：新增 " + data.imported + "，更新 " + data.updated +
            "，取消 " + data.cancelled + "，未变化 " + data.unchanged +
            "，拒绝 " + data.rejected + "。"));
          (data.warnings || []).forEach(function (warning) {
            resultBox.appendChild(el("p", "muted", warning));
          });
        }
      });
    }
  }

  function restageRows(container) {
    if (!container) {
      return;
    }
    Array.prototype.forEach.call(container.querySelectorAll("[data-stage-row]"), function (row, index) {
      var seq = row.querySelector(".stage-seq");
      if (seq) {
        seq.textContent = String(index + 1);
      }
    });
  }

  function stageRowFromTemplate(template, values) {
    var fragment = template.content.cloneNode(true);
    var row = fragment.querySelector("[data-stage-row]");
    if (!row) {
      return null;
    }
    if (values) {
      row.querySelector('[data-field="name"]').value = values.name || "";
      row.querySelector('[data-field="stage_type"]').value = values.stage_type || "custom";
      row.querySelector('[data-field="duration_minutes"]').value = values.duration_minutes || 30;
      row.querySelector('[data-field="warn_before_seconds"]').value =
        values.warn_before_seconds === undefined || values.warn_before_seconds === null
          ? 60
          : values.warn_before_seconds;
      row.querySelector('[data-field="cue_text"]').value = values.cue_text || "";
      row.querySelector('[data-field="prompt_text"]').value = values.prompt_text || "";
      var scene = values.hardware_scene;
      row.querySelector('[data-field="hardware_scene"]').value =
        scene ? JSON.stringify(scene) : "";
    }
    return row;
  }

  function initStagesPage() {
    var root = document.querySelector("[data-stages-page]");
    if (!root) {
      return;
    }
    var form = document.getElementById("stages-form");
    if (!form) {
      return;
    }
    var container = document.getElementById("stage-rows");
    var template = document.getElementById("stage-row-template");
    var statusEl = document.getElementById("stages-status");
    var scriptId = form.getAttribute("data-script-id");

    function addRow(values) {
      var row = stageRowFromTemplate(template, values);
      if (row) {
        container.appendChild(row);
        restageRows(container);
      }
    }

    var addButton = document.getElementById("add-stage-button");
    if (addButton) {
      addButton.addEventListener("click", function () {
        addRow(null);
      });
    }

    container.addEventListener("click", function (event) {
      var target = event.target;
      if (!target || !target.getAttribute) {
        return;
      }
      var row = target.closest("[data-stage-row]");
      if (!row) {
        return;
      }
      if (target.hasAttribute("data-remove-row")) {
        row.remove();
        restageRows(container);
        return;
      }
      var move = target.getAttribute("data-move");
      if (move === "up" && row.previousElementSibling) {
        container.insertBefore(row, row.previousElementSibling);
        restageRows(container);
      } else if (move === "down" && row.nextElementSibling) {
        container.insertBefore(row.nextElementSibling, row);
        restageRows(container);
      }
    });

    var sampleButton = document.getElementById("apply-sample-button");
    if (sampleButton) {
      sampleButton.addEventListener("click", async function () {
        var result = await apiFetch("/api/stages?script_id=" + encodeURIComponent(scriptId));
        if (!result.response.ok || !result.data || !result.data.ok) {
          setText(statusEl, messageFor(result.data, "读取示例失败。"), true);
          return;
        }
        var sample = result.data.sample || [];
        clear(container);
        sample.forEach(addRow);
        setText(statusEl, "示例已填入，点击保存后才会生效。");
      });
    }

    form.addEventListener("submit", async function (event) {
      event.preventDefault();
      var rows = Array.prototype.slice.call(container.querySelectorAll("[data-stage-row]"));
      var stages = [];
      var invalid = null;
      rows.forEach(function (row, index) {
        var name = row.querySelector('[data-field="name"]').value.trim();
        if (!name) {
          invalid = "第 " + (index + 1) + " 个阶段缺名称。";
          return;
        }
        var sceneText = row.querySelector('[data-field="hardware_scene"]').value.trim();
        var scene = null;
        if (sceneText) {
          try {
            scene = JSON.parse(sceneText);
          } catch (error) {
            invalid = "第 " + (index + 1) + " 个阶段的硬件场景不是合法 JSON。";
            return;
          }
        }
        stages.push({
          seq: index + 1,
          name: name,
          stage_type: row.querySelector('[data-field="stage_type"]').value,
          duration_minutes: Number(row.querySelector('[data-field="duration_minutes"]').value),
          warn_before_seconds: Number(row.querySelector('[data-field="warn_before_seconds"]').value),
          cue_text: row.querySelector('[data-field="cue_text"]').value.trim() || null,
          prompt_text: row.querySelector('[data-field="prompt_text"]').value.trim() || null,
          hardware_scene: scene,
          active: true
        });
      });
      if (invalid) {
        setText(statusEl, invalid, true);
        return;
      }
      if (!stages.length) {
        setText(statusEl, "至少需要一个阶段。", true);
        return;
      }
      if (!window.confirm("保存后只影响下一场；进行中的场控不会变化。")) {
        return;
      }
      var result = await jsonPost("/api/stages", {
        script_id: scriptId,
        stages: stages,
        confirm: true
      });
      if (result.response.ok && result.data && result.data.ok) {
        setText(statusEl, "已保存，模板版本 v" + (result.data.version || "—") + "。");
        window.setTimeout(function () {
          window.location.reload();
        }, 900);
        return;
      }
      setText(statusEl, messageFor(result.data, "保存失败。"), true);
    });
  }

  function updateRunTimers() {
    document.querySelectorAll("[data-run-remaining]").forEach(function (node) {
      var remaining = Number(node.getAttribute("data-run-remaining"));
      var overtime = Number(node.getAttribute("data-run-overtime")) || 0;
      var status = node.getAttribute("data-run-status");
      if (status === "paused") {
        node.textContent = "暂停 " + formatDuration(remaining);
      } else if (overtime > 0) {
        node.textContent = "超时 +" + formatDuration(overtime);
      } else if (node.getAttribute("data-run-remaining") !== "") {
        node.textContent = formatDuration(remaining);
      } else {
        node.textContent = status === "pending" ? "未开始" : "—";
      }
    });
    var timer = document.getElementById("run-timer");
    if (timer) {
      var left = Number(timer.getAttribute("data-remaining"));
      var over = Number(timer.getAttribute("data-overtime")) || 0;
      var state = timer.getAttribute("data-status");
      if (state === "active" && over === 0) {
        timer.textContent = formatDuration(left);
      } else if (over > 0) {
        timer.textContent = "+" + formatDuration(over);
        timer.classList.add("overtime");
      } else if (state === "paused") {
        timer.textContent = formatDuration(left);
      } else if (state === "pending") {
        timer.textContent = "等待开始";
      } else {
        timer.textContent = state === "completed" ? "已结束" : "—";
      }
    }
  }

  async function refreshRunCards() {
    var cards = document.querySelectorAll("[data-run-card]");
    if (!cards.length) {
      return;
    }
    var result = await apiFetch("/api/runs");
    if (!result.response.ok || !result.data || !result.data.ok) {
      return;
    }
    var byId = {};
    (result.data.runs || []).forEach(function (item) {
      if (item && item.run) {
        byId[item.run.run_id] = item.run;
      }
    });
    cards.forEach(function (card) {
      var run = byId[card.getAttribute("data-run-card")];
      if (!run) {
        return;
      }
      var remaining = card.querySelector("[data-run-remaining]");
      if (remaining) {
        remaining.setAttribute("data-run-remaining",
          run.remaining_seconds === null || run.remaining_seconds === undefined
            ? ""
            : run.remaining_seconds);
        remaining.setAttribute("data-run-overtime", run.overtime_seconds || 0);
        remaining.setAttribute("data-run-status", run.status);
      }
      var chip = card.querySelector(".chip");
      if (chip) {
        chip.textContent = run.status;
        chip.className = "chip " + (run.status === "active" ? "ok" : run.status === "paused" ? "warn" : "");
      }
    });
    updateRunTimers();
  }

  function initRunsPage() {
    if (!document.querySelector("[data-runs-page]")) {
      return;
    }
    updateRunTimers();
    window.setInterval(updateRunTimers, 1000);
    window.setInterval(refreshRunCards, 10000);
  }

  async function refreshRunDetail(runId) {
    var result = await apiFetch("/api/runs/" + encodeURIComponent(runId));
    if (!result.response.ok || !result.data || !result.data.ok) {
      return;
    }
    var data = result.data;
    var run = data.run || {};
    var timer = document.getElementById("run-timer");
    if (timer) {
      timer.setAttribute("data-status", run.status || "");
      timer.setAttribute("data-remaining",
        run.remaining_seconds === null || run.remaining_seconds === undefined
          ? ""
          : run.remaining_seconds);
      timer.setAttribute("data-overtime", run.overtime_seconds || 0);
      timer.classList.remove("overtime");
    }
    var stateText = document.getElementById("run-state-text");
    if (stateText) {
      if (run.paused) {
        stateText.textContent = "已暂停";
      } else if (run.overtime_seconds) {
        stateText.textContent = "已超时";
      } else if (run.status === "active") {
        stateText.textContent = "进行中";
      } else {
        stateText.textContent = run.status || "";
      }
    }
    var summary = document.getElementById("run-stage-summary");
    if (summary) {
      var stages = data.stages || [];
      var name = data.current_stage ? data.current_stage.name : "";
      summary.textContent = "当前阶段 " + (run.current_seq || "—") + " / " + stages.length +
        (name ? " · " + name : "") +
        " · 暂停累计 " + (run.paused_total_seconds || 0) + " 秒";
    }
    var items = document.querySelectorAll(".stage-timeline .stage-item");
    (data.stages || []).forEach(function (stage, index) {
      var item = items[index];
      if (!item) {
        return;
      }
      item.className = "stage-item " + (stage.state || "");
      var meta = item.querySelector(".muted");
      if (meta) {
        meta.textContent = (stage.duration_minutes || "—") + " 分钟 · " + (stage.state || "");
      }
    });
    updateRunTimers();
  }

  function initRunDetailPage() {
    var root = document.querySelector("[data-run-detail]");
    if (!root) {
      return;
    }
    updateRunTimers();
    window.setInterval(updateRunTimers, 1000);
    var runId = root.getAttribute("data-run-id");
    window.setInterval(function () {
      refreshRunDetail(runId);
    }, 10000);
    var regen = document.getElementById("regen-code-button");
    if (regen) {
      regen.addEventListener("click", async function () {
        if (!window.confirm("重新生成会让旧场控码和旧 DM 令牌立即失效，当前阶段保留。继续？")) {
          return;
        }
        regen.disabled = true;
        var result = await jsonPost("/api/runs/" + encodeURIComponent(runId) + "/code", {});
        regen.disabled = false;
        var panel = document.getElementById("run-code-panel");
        var value = document.getElementById("run-code-value");
        if (result.response.ok && result.data && result.data.ok && panel && value) {
          value.value = result.data.code || "";
          panel.classList.remove("hidden");
          return;
        }
        window.alert(messageFor(result.data, "生成失败。"));
      });
    }
    var revoke = document.getElementById("revoke-token-button");
    if (revoke) {
      revoke.addEventListener("click", async function () {
        if (!window.confirm("撤销后 DM 需要新场控码才能重新进入。继续？")) {
          return;
        }
        var result = await jsonPost("/api/runs/" + encodeURIComponent(runId) + "/revoke", {});
        if (result.response.ok && result.data && result.data.ok) {
          window.location.reload();
          return;
        }
        window.alert(messageFor(result.data, "撤销失败。"));
      });
    }
  }

  initShell();
  setupCopyButtons();
  if (page === "pending") {
    initPendingPage();
  } else if (page === "chat") {
    initChatPage();
  } else if (page === "resources") {
    initResourcesPage();
  } else if (page === "schedule") {
    initSchedulePage();
  } else if (page === "imports") {
    initImportsPage();
  } else if (page === "stages") {
    initStagesPage();
  } else if (page === "runs") {
    initRunsPage();
  } else if (page === "run-detail") {
    initRunDetailPage();
  }
})();
