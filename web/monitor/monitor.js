(function () {
  var runs = [];
  var selectedId = null;
  var selectedStep = null;
  var lastSeq = 0;
  var settingsLoaded = false;

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

  function renderRuns() {
    var ul = $("run-list");
    ul.innerHTML = runs.map(function (r) {
      var cls = r.id === selectedId ? "sel" : "";
      return "<li class=\"" + cls + "\" data-id=\"" + r.id + "\">" +
        "<span class=\"k\">" + (r.route || r.kind || "run") + "</span> · " +
        (r.status || "") +
        "<span class=\"s\">" + (r.input || "").slice(0, 80) + " · " + ago(r.ts) +
        (r.model ? " · " + r.model : "") + "</span></li>";
    }).join("") || "<li class=\"s\">no runs yet — send a command</li>";
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
      (run.say ? " · " + run.say.slice(0, 60) : "");
    ol.innerHTML = (run.steps || []).map(function (s, i) {
      var cls = (selectedStep === i ? "sel " : "") + (s.ok === false ? "fail" : (s.ok ? "ok" : ""));
      var lat = s.latency_s != null ? " " + s.latency_s.toFixed(2) + "s" : "";
      return "<li class=\"" + cls + "\" data-i=\"" + i + "\">" +
        s.stage + " · " + s.name + lat +
        (s.error ? " · " + s.error : "") + "</li>";
    }).join("");
  }

  function showStep(i) {
    var run = findRun(selectedId);
    if (!run || !run.steps || !run.steps[i]) return;
    selectedStep = i;
    var s = run.steps[i];
    $("io-meta").textContent = s.stage + " / " + s.name +
      (s.model ? " · " + s.model : "") +
      (s.latency_s != null ? " · " + s.latency_s.toFixed(3) + "s" : "");
    $("io-in").textContent = pretty(s.input);
    $("io-out").textContent = pretty(s.output) +
      (s.error ? "\n\nerror: " + s.error : "") +
      (s.extra && Object.keys(s.extra).length ? "\n\nextra: " + pretty(s.extra) : "");
    renderSteps();
  }

  function highlightFlow(run) {
    var stages = ["input", "asr", "intent", "llm", "tool", "say"];
    var last = {};
    (run && run.steps || []).forEach(function (s) { last[s.stage] = s; });
    stages.forEach(function (st) {
      var el = document.querySelector(".flow .stage[data-stage=\"" + st + "\"]");
      if (!el) return;
      el.classList.toggle("on", !!last[st]);
      el.classList.toggle("bad", !!(last[st] && last[st].ok === false));
    });
    $("st-input").textContent = run ? (run.input || "—").slice(0, 80) : "—";
    $("st-asr").textContent = last.asr ? String(last.asr.output || last.asr.name).slice(0, 60) : "whisper";
    $("st-intent").textContent = last.intent ? last.intent.name : "keyword / jev / llm";
    $("st-llm").textContent = last.llm ? (last.llm.model || last.llm.name) : "—";
    $("st-tool").textContent = last.tool ? last.tool.name + (last.tool.ok === false ? " ✕" : "") : "—";
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

  function tickPipeline() {
    fetch("/api/pipeline").then(function (r) { return r.json(); }).then(function (d) {
      pill($("conn-pill"), "LIVE", "ok");
      pill($("busy-pill"), d.agent && d.agent.busy ? "BUSY" : "IDLE", d.agent && d.agent.busy ? "warn" : "ok");
      var route = (d.trace && d.trace.runs && d.trace.runs[0] && d.trace.runs[0].route) || "—";
      pill($("route-pill"), "ROUTE " + route, "active");
      var model = (d.models && d.models.llm && d.models.llm.name) || "—";
      pill($("model-pill"), model, d.models && d.models.llm && d.models.llm.ok ? "ok" : "warn");
      $("uptime").textContent = "up " + Math.floor(d.uptime_s || 0) + "s";
      if (d.trace) {
        lastSeq = d.trace.seq || lastSeq;
        mergeRuns(d.trace.runs);
        renderRuns();
        renderSteps();
        highlightFlow(findRun(selectedId) || (d.trace.runs && d.trace.runs[0]));
      }
    }).catch(function () {
      pill($("conn-pill"), "OFFLINE", "bad");
    });
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

  $("run-list").addEventListener("click", function (ev) {
    var li = ev.target.closest("li");
    if (!li || !li.getAttribute("data-id")) return;
    selectedId = li.getAttribute("data-id");
    selectedStep = null;
    renderRuns();
    renderSteps();
    highlightFlow(findRun(selectedId));
    var run = findRun(selectedId);
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
      flash(d.ok ? "queued: " + text : (d.error || "failed"), !d.ok);
      $("try-text").value = "";
    });
  });

  loadSettings();
  tickPipeline();
  setInterval(tickPipeline, 800);
})();
