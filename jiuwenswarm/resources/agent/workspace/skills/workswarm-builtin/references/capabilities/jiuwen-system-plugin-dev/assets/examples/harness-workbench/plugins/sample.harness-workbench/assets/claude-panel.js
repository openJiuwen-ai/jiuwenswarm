(function () {
  var node = document.getElementById("payload");
  if (!node) return;
  var data;
  try {
    data = JSON.parse(node.textContent || "{}");
  } catch (error) {
    document.getElementById("root").innerHTML = "<p class='muted'>Panel payload invalid</p>";
    return;
  }
  var root = document.getElementById("root");
  var html = [];

  function esc(text) {
    return String(text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  if (data.files && data.files.length) {
    html.push("<section class='section'><h2>Workspace</h2><div class='tree'>");
    data.files.slice(0, 24).forEach(function (path) {
      html.push(esc(path) + "<br>");
    });
    html.push("</div></section>");
  } else if (data.empty && data.empty.files) {
    html.push("<section class='section'><h2>Workspace</h2><p class='empty'>" + esc(data.empty.files) + "</p></section>");
  }

  var activities = data.activities || [];
  if (activities.length) {
    html.push("<section class='section'><h2>Tool log</h2><div class='log'>");
    activities.slice(0, 12).forEach(function (item) {
      var line = item.label + (item.target ? " " + item.target : "");
      if (item.error) line += " (failed)";
      html.push("<div class='row'>" + esc(line) + "</div>");
    });
    html.push("</div></section>");
  }

  var diffs = [];
  activities.forEach(function (item) {
    (item.diff || []).forEach(function (line) {
      diffs.push(line);
    });
  });
  if (diffs.length) {
    html.push("<section class='section'><h2>Diff</h2>");
    diffs.slice(0, 16).forEach(function (line) {
      var cls = line.kind === "add" ? "add" : "del";
      var sign = line.kind === "add" ? "+" : "-";
      html.push("<div class='" + cls + "'>" + esc(sign + " " + line.text) + "</div>");
    });
    html.push("</section>");
  }

  if (data.plan && data.plan.length) {
    html.push("<section class='section'><h2>Plan</h2><div class='log'>");
    data.plan.slice(0, 8).forEach(function (line) {
      html.push("<div class='row'>" + esc(line) + "</div>");
    });
    html.push("</div></section>");
  }

  if (!html.length) {
    html.push("<p class='empty'>" + esc((data.empty && data.empty.activity) || "Waiting for tool activity…") + "</p>");
  }

  html.push("<section class='section'><h2>Context</h2><p class='muted'>Harness workbench panel</p><div class='meter'><i></i></div></section>");
  root.innerHTML = html.join("");
})();
