(function () {
  var afterTs = 0;
  var connected = false;
  var allowMotion = false;
  var micRec = null;

  function $(id) { return document.getElementById(id); }
  function fmt(n, d) {
    if (n === null || n === undefined || n === "") return "—";
    if (typeof n === "number") return n.toFixed(d === undefined ? 2 : d);
    return String(n);
  }
  function ago(ts) {
    if (!ts) return "—";
    var s = (Date.now() / 1000) - ts;
    if (s < 1.5) return "now";
    if (s < 60) return s.toFixed(0) + "s";
    return Math.floor(s / 60) + "m";
  }

  function post(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    }).then(function (r) { return r.json(); });
  }

  function kv(el, rows) {
    el.innerHTML = rows.map(function (row) {
      return "<b>" + row[0] + "</b><span>" + row[1] + "</span>";
    }).join("");
  }

  function pill(el, text, cls) {
    el.textContent = text;
    el.className = "pill" + (cls ? " " + cls : "");
  }

  function renderHealth(health) {
    var grid = $("health-grid");
    var keys = ["camera", "perception", "safety", "motors", "llm", "whisper", "memory", "typesafe"];
    grid.innerHTML = keys.map(function (k) {
      var h = (health || {})[k] || {};
      var on = !!h.online;
      var extra = "";
      if (h.fps !== undefined) extra = " " + fmt(h.fps, 1) + "fps";
      if (h.latency_s !== undefined) extra = " " + fmt(h.latency_s, 1) + "s";
      return '<div class="hitem ' + (on ? "on" : "off") + '"><span class="n">' + k.toUpperCase() +
        "</span>" + (on ? "ONLINE" : "DOWN") + extra + "<div>" + (h.detail || "") + "</div></div>";
    }).join("");
  }

  function renderBins(bins) {
    var el = $("bins");
    bins = bins || [];
    el.innerHTML = bins.map(function (v) {
      var h = Math.max(8, Math.min(100, v * 100));
      return "<i style='--h:" + h + "%'></i>";
    }).join("");
  }

  function renderObjects(objs) {
    var el = $("objects");
    var list = Object.keys(objs || {}).map(function (k) { return objs[k]; });
    if (!list.length) { el.innerHTML = "<li>no objects</li>"; return; }
    el.innerHTML = list.map(function (o) {
      return "<li>" + o.id + " · " + o.class + " · c=" + fmt(o.confidence, 2) +
        " · " + o.certainty + "</li>";
    }).join("");
  }

  function renderMemory(mem) {
    var el = $("memory");
    var notes = (mem && mem.recent) || [];
    if (!notes.length) { el.innerHTML = "<li>no notes</li>"; return; }
    el.innerHTML = notes.map(function (n) {
      return "<li>" + ago(n.timestamp) + " · " + (n.tags || []).join(",") +
        "<br/>" + (n.content || "").slice(0, 160) + "</li>";
    }).join("");
  }

  function renderEvents(events) {
    var el = $("events");
    events = events || [];
    if (events.length) afterTs = events[events.length - 1].timestamp;
    var bad = { EmergencyStop: 1, TaskFailed: 1, NavigationBlocked: 1, CommandRejected: 1 };
    var warn = { PerceptionStale: 1, LlmUnavailable: 1, ObstacleDetected: 1 };
    el.innerHTML = events.slice().reverse().map(function (e) {
      var cls = bad[e.type] ? "bad" : (warn[e.type] ? "warn" : "");
      var d = new Date(e.timestamp * 1000);
      var hh = d.toTimeString().slice(0, 8);
      return '<li class="' + cls + '">' + hh + " " + e.type + " · " + e.source + "</li>";
    }).join("");
  }

  function setMotionUi(on) {
    allowMotion = !!on;
    var btn = $("btn-motion");
    if (on) {
      pill($("motion-pill"), "MOTION ON", "ok");
      if (btn) {
        btn.textContent = "LOCK WHEELS";
        btn.className = "on";
      }
    } else {
      pill($("motion-pill"), "MOTION LOCKED", "warn");
      if (btn) {
        btn.textContent = "UNLOCK WHEELS";
        btn.className = "";
      }
    }
  }

  function render(s) {
    connected = true;
    pill($("conn-pill"), "CONNECTED", "ok");
    if (s.estop) pill($("estop-pill"), "E-STOP ACTIVE", "bad");
    else pill($("estop-pill"), "E-STOP CLEAR", "ok");
    setMotionUi(!!s.allow_motion);
    var task = s.task || {};
    pill($("task-pill"), task.state || "IDLE", task.state === "FAILED" ? "bad" : "");
    $("uptime").textContent = "up " + Math.floor(s.uptime_s || 0) + "s";

    var cam = s.camera || {};
    var perc = s.perception || {};
    var robot = s.robot || {};
    $("cam-backend").textContent = cam.backend || perc.backend || "—";
    $("cam-fps").textContent = fmt(cam.fps || perc.fps, 1) + " fps";
    $("cam-lat").textContent = fmt(perc.latency_ms, 0) + " ms";
    var servo = s.servo || {};
    if (servo.enabled) {
      $("cam-pose").textContent = "pan " + fmt(robot.camera_pan, 0) + "° · tilt " + fmt(robot.camera_tilt, 0) + "°";
    } else {
      $("cam-pose").textContent = "camera fixed";
    }

    var obs = s.obstacles || {};
    $("hud-block").textContent = "FRONT " + (obs.front_m == null ? "—" : fmt(obs.front_m, 2) + "m") +
      (obs.blocked ? "  BLOCKED" : "");
    $("hud-nav").textContent = "NAV " + ((s.navigation || {}).mode || "idle");

    kv($("robot-kv"), [
      ["motors", (robot.hardware || {}).motors],
      ["servo", servo.enabled ? ((robot.hardware || {}).servo || "gimbal") : "fixed"],
      ["left", fmt(robot.left_speed, 2)],
      ["right", fmt(robot.right_speed, 2)],
      ["estop", robot.estop ? "YES" : "no"],
      ["motion", s.allow_motion ? "enabled" : "LOCKED (charging)"],
      ["pose x,y", fmt((robot.pose || {}).x, 2) + ", " + fmt((robot.pose || {}).y, 2)]
    ]);
    var host = ((s.health || {}).host_metrics) || ((s.health || {}).host) || {};
    kv($("perc-kv"), [
      ["depth", fmt(perc.depth_fps, 1) + " fps"],
      ["detect", fmt(perc.detect_fps, 1) + " fps"],
      ["fresh", JSON.stringify(s.freshness || {})],
      ["CPU load", fmt(host.load1, 2)],
      ["RAM avail", host.mem_avail_mb ? fmt(host.mem_avail_mb, 0) + " MB" : "—"],
      ["temp", host.temp_c ? fmt(host.temp_c, 1) + " C" : "—"],
      ["GPU", host.gpu_pct != null ? fmt(host.gpu_pct, 0) + "%" : "—"]
    ]);
    renderHealth(s.health);
    renderBins(obs.bins);
    renderObjects(s.objects);
    kv($("task-kv"), [
      ["name", task.name || "—"],
      ["state", task.state || "IDLE"],
      ["action", task.action || "—"],
      ["progress", fmt(task.progress, 2)]
    ]);
    var agent = s.agent || {};
    $("agent-say").textContent = agent.say || "idle";
    var dec = $("decisions");
    dec.innerHTML = (agent.decisions || []).slice().reverse().map(function (d) {
      return "<li>" + (d.kind || "") + " · " + (d.say || "") + "</li>";
    }).join("");
    $("world-pre").textContent = JSON.stringify({
      obstacles: obs,
      navigation: s.navigation,
      freshness: s.freshness,
      objects: s.objects
    }, null, 2);
    renderMemory(s.memory);
  }

  function tick() {
    fetch("/api/status").then(function (r) { return r.json(); }).then(render).catch(function () {
      connected = false;
      pill($("conn-pill"), "DISCONNECTED", "bad");
    });
    fetch("/api/events?limit=40&after=" + afterTs).then(function (r) { return r.json(); })
      .then(function (d) { if (d.events && d.events.length) renderEvents(d.events); })
      .catch(function () {});
  }

  document.querySelectorAll("[data-act]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      post("/api/manual", { action: btn.getAttribute("data-act"), duration: 0.8 });
    });
  });
  function toggleMotion() {
    post("/api/motion", { allow: !allowMotion }).then(function (d) {
      if (d && d.ok) setMotionUi(!!d.allow_motion);
    }).catch(function () {});
  }
  $("btn-estop").addEventListener("click", function () { post("/api/estop", {}); });
  $("btn-clear-estop").addEventListener("click", function () { post("/api/estop/clear", {}); });
  $("btn-motion").addEventListener("click", toggleMotion);
  $("motion-pill").addEventListener("click", toggleMotion);
  $("cmd-form").addEventListener("submit", function (ev) {
    ev.preventDefault();
    var text = $("cmd-text").value.trim();
    if (!text) return;
    post("/api/command", { text: text });
    $("cmd-text").value = "";
  });
  $("audio-file").addEventListener("change", function (ev) {
    var f = ev.target.files && ev.target.files[0];
    if (!f) return;
    uploadAudio(f, f.name || "speech.wav");
  });

  function setVoice(msg, rec) {
    var st = $("voice-status");
    var btn = $("btn-mic");
    if (st) st.textContent = msg || "";
    if (btn) {
      if (rec) btn.classList.add("rec");
      else btn.classList.remove("rec");
      btn.textContent = rec ? "REC" : "MIC";
    }
  }

  function encodeWav(samples, sampleRate) {
    var n = samples.length;
    var buf = new ArrayBuffer(44 + n * 2);
    var view = new DataView(buf);
    function str(off, s) {
      for (var i = 0; i < s.length; i++) view.setUint8(off + i, s.charCodeAt(i));
    }
    str(0, "RIFF");
    view.setUint32(4, 36 + n * 2, true);
    str(8, "WAVE");
    str(12, "fmt ");
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);
    view.setUint16(22, 1, true);
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate * 2, true);
    view.setUint16(32, 2, true);
    view.setUint16(34, 16, true);
    str(36, "data");
    view.setUint32(40, n * 2, true);
    var off = 44;
    for (var i = 0; i < n; i++) {
      var s = Math.max(-1, Math.min(1, samples[i]));
      view.setInt16(off, s < 0 ? s * 0x8000 : s * 0x7fff, true);
      off += 2;
    }
    return new Blob([buf], { type: "audio/wav" });
  }

  function downsample(buf, fromRate, toRate) {
    if (fromRate === toRate) return buf;
    var ratio = fromRate / toRate;
    var outLen = Math.max(1, Math.round(buf.length / ratio));
    var out = new Float32Array(outLen);
    for (var i = 0; i < outLen; i++) {
      var start = Math.floor(i * ratio);
      var end = Math.min(buf.length, Math.floor((i + 1) * ratio));
      var sum = 0, c = 0;
      for (var j = start; j < end; j++) { sum += buf[j]; c++; }
      out[i] = c ? sum / c : buf[Math.min(start, buf.length - 1)];
    }
    return out;
  }

  function uploadAudio(blob, name) {
    setVoice("uploading…", false);
    var fd = new FormData();
    fd.append("file", blob, name || "speech.wav");
    fetch("/api/whisper", { method: "POST", body: fd }).then(function (r) { return r.json(); })
      .then(function (d) {
        var text = (d && (d.text || d.error)) || "ok";
        setVoice(text, false);
        if (d && d.text) $("cmd-text").value = d.text;
      })
      .catch(function (e) { setVoice(String(e), false); });
  }

  function startMic() {
    if (micRec) return;
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      setVoice("mic not available", false);
      return;
    }
    navigator.mediaDevices.getUserMedia({ audio: true }).then(function (stream) {
      var Ctx = window.AudioContext || window.webkitAudioContext;
      var ctx = new Ctx();
      var src = ctx.createMediaStreamSource(stream);
      var node = ctx.createScriptProcessor(4096, 1, 1);
      var mute = ctx.createGain();
      mute.gain.value = 0;
      var chunks = [];
      node.onaudioprocess = function (ev) {
        chunks.push(new Float32Array(ev.inputBuffer.getChannelData(0)));
      };
      src.connect(node);
      node.connect(mute);
      mute.connect(ctx.destination);
      micRec = { stream: stream, ctx: ctx, node: node, src: src, mute: mute, chunks: chunks };
      setVoice("listening… hold MIC", true);
    }).catch(function (e) {
      setVoice("mic denied: " + e, false);
    });
  }

  function stopMic() {
    if (!micRec) return;
    var rec = micRec;
    micRec = null;
    try { rec.node.disconnect(); } catch (e) {}
    try { rec.src.disconnect(); } catch (e) {}
    try { rec.mute.disconnect(); } catch (e) {}
    try { rec.stream.getTracks().forEach(function (t) { t.stop(); }); } catch (e) {}
    var total = 0;
    rec.chunks.forEach(function (c) { total += c.length; });
    var merged = new Float32Array(total);
    var off = 0;
    rec.chunks.forEach(function (c) { merged.set(c, off); off += c.length; });
    var rate = rec.ctx.sampleRate || 44100;
    try { rec.ctx.close(); } catch (e) {}
    if (merged.length < rate * 0.25) {
      setVoice("too short", false);
      return;
    }
    var pcm = downsample(merged, rate, 16000);
    uploadAudio(encodeWav(pcm, 16000), "speech.wav");
  }

  (function bindMic() {
    var btn = $("btn-mic");
    if (!btn) return;
    btn.addEventListener("mousedown", function (ev) { ev.preventDefault(); startMic(); });
    btn.addEventListener("mouseup", function (ev) { ev.preventDefault(); stopMic(); });
    btn.addEventListener("mouseleave", function () { if (micRec) stopMic(); });
    btn.addEventListener("touchstart", function (ev) { ev.preventDefault(); startMic(); }, { passive: false });
    btn.addEventListener("touchend", function (ev) { ev.preventDefault(); stopMic(); });
    btn.addEventListener("touchcancel", function () { if (micRec) stopMic(); });
    btn.addEventListener("click", function (ev) { ev.preventDefault(); });
  })();

  setInterval(tick, 1000);
  tick();
})();
