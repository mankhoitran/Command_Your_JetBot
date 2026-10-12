(function () {
  var runs = [];
  var selectedId = null;
  var selectedStep = null;
  var selectedNode = null;
  var lastSeq = 0;
  var settingsLoaded = false;
  var motorHistory = [];
  var pipeline = { nodes: [], edges: [] };
  var stream = null;
  var pollTimer = null;

  function $(id) { return document.getElementById(id); }
  function pretty(v) {
    if (v === undefined || v === null || v === "") return "—";
    if (typeof v === "string") return v;
    try { return JSON.stringify(v, null, 2); } catch (e) { return String(v); }
  }
  function ago(ts) {
    if (!ts) return "";
    var s = (Date.now() / 1000) - ts;
    if (s < 1.5) return "now";
    if (s < 60) return s.toFixed(0) + "s";
    return Math.floor(s / 60) + "m";
  }
  function flash(msg, bad) {
    var el = $("flash");
    el.textContent = msg;
    el.className = "hint show" + (bad ? " bad" : "");
    setTimeout(function () { el.className = "hint"; }, 2800);
  }
  function post(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    }).then(function (r) { return r.json(); });
  }
  function pill(el, text, cls) {
    el.textContent = text;
    el.className = "pill" + (cls ? " " + cls : "");
  }
  function fmtNum(v, digits) {
    if (v === undefined || v === null || v === "") return "—";
    var n = Number(v);
    if (isNaN(n)) return String(v);
    return n.toFixed(digits == null ? 1 : digits);
  }
  function phaseLine(item) {
    if (!item) return "none";
    var left = item.left == null ? "?" : Number(item.left).toFixed(2);
    var right = item.right == null ? "?" : Number(item.right).toFixed(2);
    var bits = [item.phase || "?", "L " + left, "R " + right, item.source || ""];
    if (item.reason) bits.push(item.reason);
    bits.push(ago(item.ts));
    return bits.join(" · ");
  }
  function kv(el, rows) {
    el.innerHTML = rows.map(function (row) {
      return "<dt>" + row[0] + "</dt><dd>" + (row[1] == null || row[1] === "" ? "—" : row[1]) + "</dd>";
    }).join("");
  }

  function showView(name) {
    ["dashboard", "pipeline", "trace", "config"].forEach(function (v) {
      var view = $("view-" + v);
      if (view) view.classList.toggle("on", v === name);
    });
    Array.prototype.forEach.call(document.querySelectorAll(".tab"), function (btn) {
      btn.classList.toggle("on", btn.getAttribute("data-view") === name);
    });
  }

  function renderRuns() {
    var ul = $("run-list");
    ul.innerHTML = runs.map(function (r) {
      var cls = r.id === selectedId ? "sel" : "";
      return "<li class=\"" + cls + "\" data-id=\"" + r.id + "\">" +
        "<span class=\"k\">" + (r.route || r.kind || "run") + "</span> · " +
        (r.status || "") +
        "<span class=\"s\">" + (r.input || "").slice(0, 80) + " · " + ago(r.ts) +
        (r.corr_id ? " · " + r.corr_id : "") +
        (r.model ? " · " + r.model : "") + "</span></li>";
    }).join("") || "<li class=\"s\">no runs yet — queue a command</li>";
    $("run-meta").textContent = runs.length + " kept";
  }

  function findRun(id) {
    for (var i = 0; i < runs.length; i++) if (runs[i].id === id) return runs[i];
    return null;
  }

  function renderSteps() {
    var run = findRun(selectedId);
    var ol = $("steps");
    if (!run) {
      ol.innerHTML = "";
      $("sel-meta").textContent = "pick a run";
      return;
    }
    $("sel-meta").textContent = (run.source || "") + " · " + (run.status || "") +
      (run.corr_id ? " · " + run.corr_id : "") +
      (run.say ? " · " + run.say.slice(0, 60) : "");
    ol.innerHTML = (run.steps || []).map(function (s, i) {
      var cls = (selectedStep === i ? "sel " : "") + (s.ok === false ? "fail" : (s.ok ? "ok" : ""));
      var lat = s.latency_s != null ? " " + s.latency_s.toFixed(2) + "s" : "";
      return "<li class=\"" + cls + "\" data-i=\"" + i + "\">" +
        s.stage + " · " + s.name + lat +
        (s.error ? " · " + s.error : "") + "</li>";
    }).join("");
  }

  function motorForCorr(corrId) {
    if (!corrId) return [];
    return motorHistory.filter(function (row) { return row.corr_id === corrId; });
  }

  function renderMotorTrace(run) {
    var el = $("motor-trace");
    if (!run) { el.textContent = "select a run"; return; }
    var rows = motorForCorr(run.corr_id);
    if (!rows.length) {
      el.textContent = "no motor ledger for corr_id " + (run.corr_id || "—") +
        "\n(requested/accepted/applied are recorded only when Safety sees a command)";
      return;
    }
    el.textContent = rows.map(phaseLine).join("\n");
  }

  function showStep(i) {
    var run = findRun(selectedId);
    if (!run || !run.steps || !run.steps[i]) return;
    selectedStep = i;
    var s = run.steps[i];
    $("io-meta").textContent = s.stage + " / " + s.name +
      (s.model ? " · " + s.model : "") +
      (s.corr_id ? " · " + s.corr_id : "") +
      (s.latency_s != null ? " · " + s.latency_s.toFixed(3) + "s" : "");
    $("io-in").textContent = pretty(s.input);
    $("io-out").textContent = pretty(s.output) +
      (s.error ? "\n\nerror: " + s.error : "") +
      (s.extra && Object.keys(s.extra).length ? "\n\nextra: " + pretty(s.extra) : "");
    renderSteps();
  }

  function highlightFlow(run, motor) {
    var stages = ["input", "asr", "intent", "llm", "tool", "safety", "motors", "say"];
    var last = {};
    (run && run.steps || []).forEach(function (s) { last[s.stage] = s; });
    var corrRows = motorForCorr(run && run.corr_id);
    var lastMotor = corrRows.length ? corrRows[corrRows.length - 1] : null;
    stages.forEach(function (st) {
      var el = document.querySelector(".flow .stage[data-stage=\"" + st + "\"]");
      if (!el) return;
      var on = !!last[st];
      var bad = !!(last[st] && last[st].ok === false);
      if (st === "safety" || st === "motors") {
        on = on || !!lastMotor;
        bad = bad || (lastMotor && lastMotor.phase === "rejected");
      }
      el.classList.toggle("on", on);
      el.classList.toggle("bad", bad);
    });
    $("st-input").textContent = run ? (run.input || "—").slice(0, 80) : "—";
    $("st-asr").textContent = last.asr ? String(last.asr.output || last.asr.name).slice(0, 60) : "whisper";
    $("st-intent").textContent = last.intent ? last.intent.name : "keyword / jev / llm";
    $("st-llm").textContent = last.llm ? (last.llm.model || last.llm.name) : "—";
    $("st-tool").textContent = last.tool ? last.tool.name + (last.tool.ok === false ? " ✕" : "") : "—";
    $("st-safety").textContent = lastMotor ? (lastMotor.phase + (lastMotor.reason ? " " + lastMotor.reason : "")) : "—";
    $("st-motors").textContent = lastMotor && lastMotor.phase === "applied"
      ? ("L " + fmtNum(lastMotor.left, 2) + " R " + fmtNum(lastMotor.right, 2))
      : (lastMotor ? lastMotor.phase : "—");
    $("st-say").textContent = run && run.say ? run.say.slice(0, 80) : "—";
  }

  function mergeRuns(incoming) {
    var byId = {};
    runs.forEach(function (r) { byId[r.id] = r; });
    (incoming || []).forEach(function (r) { byId[r.id] = r; });
    runs = Object.keys(byId).map(function (k) { return byId[k]; });
    runs.sort(function (a, b) { return (b.ts || 0) - (a.ts || 0); });
    if (runs.length > 80) runs = runs.slice(0, 80);
    if (!selectedId && runs[0]) selectedId = runs[0].id;
  }

  function renderDashboard(d) {
    var dash = d.dashboard || {};
    var host = dash.host || {};
    kv($("host-kv"), [
      ["cpu", host.cpu_pct == null ? "—" : fmtNum(host.cpu_pct, 1) + "%"],
      ["load1", fmtNum(host.load1, 2)],
      ["ram used", host.mem_used_mb == null ? "—" : fmtNum(host.mem_used_mb, 0) + " MB"],
      ["ram avail", host.mem_avail_mb == null ? "—" : fmtNum(host.mem_avail_mb, 0) + " MB"],
      ["gpu", host.gpu_pct == null ? "—" : fmtNum(host.gpu_pct, 1) + "%"],
      ["temp", host.temp_c == null ? "—" : fmtNum(host.temp_c, 1) + " °C"]
    ]);
    $("host-meta").textContent = host.ts ? ago(host.ts) : "—";
    var q = dash.queues || {};
    kv($("queue-kv"), [
      ["agent pending", q.agent_pending],
      ["agent busy", q.agent_busy ? "yes" : "no"],
      ["events", (q.event_history || 0) + " / " + (q.event_history_limit || 0)],
      ["trace runs", (q.trace_runs || 0) + " / " + (q.trace_run_limit || 0)],
      ["scan", q.scan_running ? "running" : "idle"],
      ["errors kept", q.error_buffer]
    ]);
    var mot = dash.motor || {};
    kv($("motor-kv"), [
      ["requested", phaseLine(mot.last_requested)],
      ["accepted", phaseLine(mot.last_accepted)],
      ["applied", phaseLine(mot.last_applied)],
      ["rejected", phaseLine(mot.last_rejected)]
    ]);
    var task = dash.task || d.task || {};
    $("task-meta").textContent = task.state || "IDLE";
    kv($("task-kv"), [
      ["name", task.name || "none"],
      ["action", task.action || "—"],
      ["progress", task.progress == null ? "—" : task.progress],
      ["failure", task.failure_reason || "—"]
    ]);
    var tbody = $("loop-tbl").querySelector("tbody");
    tbody.innerHTML = (dash.loops || []).map(function (row) {
      var cls = row.stale ? "stale" : (row.overruns ? "error" : "");
      return "<tr class=\"" + cls + "\"><td>" + row.name + "</td><td>" + fmtNum(row.hz, 1) +
        "</td><td>" + fmtNum(row.target_hz, 1) + "</td><td>" + fmtNum(row.last_period_ms, 1) +
        "</td><td>" + fmtNum(row.max_period_ms, 1) + "</td><td>" + (row.overruns || 0) +
        "</td><td>" + (row.age_s == null ? "—" : fmtNum(row.age_s, 2) + "s") +
        "</td><td>" + (row.stale ? "STALE" : (row.ticks ? "live" : "idle")) + "</td></tr>";
    }).join("");
    var errs = dash.errors || [];
    $("err-meta").textContent = errs.length + " kept";
    $("err-list").innerHTML = errs.slice().reverse().map(function (e) {
      return "<li><span class=\"k\">" + (e.type || "error") + "</span> · " + (e.source || "") +
        "<span class=\"s\">" + (e.message || "") + " · " + ago(e.ts) +
        (e.corr_id ? " · " + e.corr_id : "") + "</span></li>";
    }).join("") || "<li class=\"s\">no recorded errors</li>";
  }

  function renderGraph(d) {
    pipeline = d.pipeline || pipeline;
    var nodes = pipeline.nodes || [];
    var edges = pipeline.edges || [];
    var html = nodes.map(function (n) {
      var cls = "gnode " + (n.status || "idle") + (selectedNode === n.id ? " sel" : "");
      return "<div class=\"" + cls + "\" data-id=\"" + n.id + "\">" +
        "<b><span class=\"dot\"></span>" + (n.label || n.id) + "</b>" +
        "<div class=\"gdet\">" + (n.status || "") + (n.detail ? " · " + n.detail : "") + "</div></div>";
    }).join("");
    html += "<div class=\"gedges\">" + edges.map(function (e) {
      return e.from + " → " + e.to;
    }).join(" · ") + "</div>";
    $("graph").innerHTML = html || "<div class=\"gdet\">no nodes</div>";
    $("life-list").innerHTML = nodes.map(function (n) {
      return "<li data-id=\"" + n.id + "\"><span class=\"k\">" + n.id + "</span> · " + (n.status || "") +
        "<span class=\"s\">" + (n.detail || "") + (n.last_ts ? " · " + ago(n.last_ts) : " · no event") + "</span></li>";
    }).join("");
    if (selectedNode) showNode(selectedNode);
  }

  function findNode(id) {
    var nodes = pipeline.nodes || [];
    for (var i = 0; i < nodes.length; i++) if (nodes[i].id === id) return nodes[i];
    return null;
  }

  function showNode(id) {
    selectedNode = id;
    var n = findNode(id);
    $("node-meta").textContent = n ? (n.label + " · " + (n.status || "")) : "click a node";
    $("node-io").textContent = n ? pretty(n) : "select a node";
    Array.prototype.forEach.call(document.querySelectorAll(".gnode"), function (el) {
      el.classList.toggle("sel", el.getAttribute("data-id") === id);
    });
  }

  function applySnapshot(d) {
    pill($("conn-pill"), "LIVE", "ok");
    pill($("busy-pill"), d.agent && d.agent.busy ? "BUSY" : "IDLE", d.agent && d.agent.busy ? "warn" : "ok");
    pill($("estop-pill"), d.estop ? "E-STOP" : (d.allow_motion ? "MOTION FREE" : "LOCKED"),
      d.estop ? "bad" : (d.allow_motion ? "ok" : "warn"));
    var route = (d.trace && d.trace.runs && d.trace.runs[0] && d.trace.runs[0].route) || "—";
    pill($("route-pill"), "ROUTE " + route, "active");
    $("uptime").textContent = "up " + Math.floor(d.uptime_s || 0) + "s";
    if (d.dashboard && d.dashboard.motor && d.dashboard.motor.history) {
      motorHistory = d.dashboard.motor.history;
    }
    renderDashboard(d);
    renderGraph(d);
    if (d.trace) {
      lastSeq = d.trace.seq || lastSeq;
      mergeRuns(d.trace.runs);
      renderRuns();
      renderSteps();
      var run = findRun(selectedId) || (d.trace.runs && d.trace.runs[0]);
      highlightFlow(run);
      renderMotorTrace(run);
    }
  }

  function startStream() {
    if (typeof EventSource === "undefined") {
      startPoll();
      return;
    }
    try {
      stream = new EventSource("/api/stream");
    } catch (e) {
      startPoll();
      return;
    }
    stream.onmessage = function (ev) {
      try { applySnapshot(JSON.parse(ev.data)); } catch (err) {}
    };
    stream.onerror = function () {
      pill($("conn-pill"), "RECONNECT", "warn");
      if (stream) {
        stream.close();
        stream = null;
      }
      startPoll();
    };
  }

  function startPoll() {
    if (pollTimer) return;
    function tick() {
      fetch("/api/pipeline").then(function (r) { return r.json(); }).then(applySnapshot).catch(function () {
        pill($("conn-pill"), "OFFLINE", "bad");
      });
    }
    tick();
    pollTimer = setInterval(tick, 1000);
  }

  function loadSettings() {
    fetch("/api/settings").then(function (r) { return r.json(); }).then(function (d) {
      settingsLoaded = true;
      $("prompt").value = d.system_prompt || "";
      $("prompt-meta").textContent = d.system_prompt_overridden ? "custom (live)" : "stock";
      $("llm-model").value = d.llm.model || "";
      $("llm-url").value = d.llm.base_url || "";
      $("llm-temp").value = d.llm.temperature;
      $("llm-tokens").value = d.llm.max_tokens;
      $("llm-think").checked = !!d.llm.enable_thinking;
      $("keywords").value = JSON.stringify(d.keyword_map || [], null, 2);
      $("intents").value = JSON.stringify(d.intent_tools || {}, null, 2);
      renderTools(d.tools || []);
    });
  }

  function renderTools(tools) {
    $("tools").innerHTML = tools.map(function (t) {
      return "<li data-name=\"" + t.name + "\">" +
        "<input type=\"checkbox\"" + (t.enabled ? " checked" : "") + "/>" +
        "<div><div class=\"name\">" + t.name + "</div>" +
        "<textarea data-doc=\"1\">" + String(t.doc || "")
          .replace(/&/g, "&amp;").replace(/</g, "&lt;") + "</textarea></div></li>";
    }).join("");
  }

  function collectTools() {
    var allowed = [];
    var enabled = {};
    var docs = {};
    Array.prototype.forEach.call($("tools").querySelectorAll("li"), function (li) {
      var name = li.getAttribute("data-name");
      var on = li.querySelector("input").checked;
      var doc = li.querySelector("textarea").value;
      allowed.push(name);
      enabled[name] = on;
      docs[name] = doc;
    });
    return { allowed_tools: allowed, tool_enabled: enabled, tool_docs: docs };
  }

  function apply(body, okMsg) {
    return post("/api/settings", body).then(function (d) {
      if (!d.ok) { flash(d.error || "apply failed", true); return; }
      flash(okMsg || "applied live");
      loadSettings();
    }).catch(function (e) { flash(String(e), true); });
  }

  $("tabs").addEventListener("click", function (ev) {
    var btn = ev.target.closest(".tab");
    if (!btn) return;
    showView(btn.getAttribute("data-view"));
  });
  $("graph").addEventListener("click", function (ev) {
    var el = ev.target.closest(".gnode");
    if (!el) return;
    showNode(el.getAttribute("data-id"));
  });
  $("life-list").addEventListener("click", function (ev) {
    var li = ev.target.closest("li");
    if (!li) return;
    showNode(li.getAttribute("data-id"));
  });
  $("run-list").addEventListener("click", function (ev) {
    var li = ev.target.closest("li");
    if (!li || !li.getAttribute("data-id")) return;
    selectedId = li.getAttribute("data-id");
    selectedStep = null;
    renderRuns();
    renderSteps();
    var run = findRun(selectedId);
    highlightFlow(run);
    renderMotorTrace(run);
    if (run && run.steps && run.steps.length) showStep(run.steps.length - 1);
  });
  $("steps").addEventListener("click", function (ev) {
    var li = ev.target.closest("li");
    if (!li) return;
    showStep(parseInt(li.getAttribute("data-i"), 10));
  });

  $("btn-apply-prompt").addEventListener("click", function () {
    apply({ system_prompt: $("prompt").value }, "prompt live");
  });
  $("btn-apply-llm").addEventListener("click", function () {
    apply({
      llm: {
        model: $("llm-model").value,
        base_url: $("llm-url").value,
        temperature: parseFloat($("llm-temp").value),
        max_tokens: parseInt($("llm-tokens").value, 10),
        enable_thinking: $("llm-think").checked
      }
    }, "llm live");
  });
  $("btn-apply-tools").addEventListener("click", function () {
    apply(collectTools(), "tools live");
  });
  $("btn-add-tool").addEventListener("click", function () {
    var name = ($("new-tool").value || "").trim();
    if (!name) return;
    var tools = collectTools();
    if (tools.allowed_tools.indexOf(name) >= 0) { flash("already listed", true); return; }
    tools.allowed_tools.push(name);
    tools.tool_enabled[name] = true;
    tools.tool_docs[name] = "";
    apply(tools, "tool added (handler must already exist to run)");
    $("new-tool").value = "";
  });
  $("btn-apply-kw").addEventListener("click", function () {
    try {
      apply({ keyword_map: JSON.parse($("keywords").value) }, "keywords live");
    } catch (e) { flash("keyword JSON: " + e, true); }
  });
  $("btn-apply-intent").addEventListener("click", function () {
    try {
      apply({ intent_tools: JSON.parse($("intents").value) }, "intents live");
    } catch (e) { flash("intent JSON: " + e, true); }
  });
  $("btn-reset").addEventListener("click", function () {
    if (!confirm("Reset prompt, tools, keywords to code defaults?")) return;
    post("/api/settings/reset", {}).then(function () {
      flash("reset to stock");
      loadSettings();
    });
  });
  $("try-form").addEventListener("submit", function (ev) {
    ev.preventDefault();
    var text = $("try-text").value.trim();
    if (!text) return;
    post("/api/command", { text: text }).then(function (d) {
      flash(d.ok ? "queued " + (d.corr_id || "") + ": " + text : (d.error || "failed"), !d.ok);
      $("try-text").value = "";
    });
  });

  loadSettings();
  startStream();
})();
