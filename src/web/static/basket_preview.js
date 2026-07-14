(function () {
  "use strict";
  var form = document.querySelector("[data-basket-preview-form]");
  if (!form) return;

  var output = document.querySelector("[data-preview-output]");
  var status = document.querySelector("[data-preview-status]");
  var retry = document.querySelector("[data-preview-retry]");
  var timer = null;
  var activeController = null;
  var requestSequence = 0;

  function renderPreview() {
    var sequence = ++requestSequence;
    if (activeController) activeController.abort();
    activeController = new AbortController();
    status.textContent = "正在更新预览…";
    status.classList.remove("error");
    retry.hidden = true;

    fetch("/basket/preview", {
      method: "POST",
      body: new FormData(form),
      headers: {"Accept": "application/json"},
      signal: activeController.signal
    }).then(function (response) {
      return response.json().then(function (data) {
        if (!response.ok) throw new Error(data.error || "预览请求失败");
        return data;
      });
    }).then(function (data) {
      if (sequence !== requestSequence) return;
      output.innerHTML = data.html;
      status.textContent = "预览已更新";
      if (window.MathJax && window.MathJax.typesetPromise) {
        window.MathJax.typesetPromise([output]).catch(function () {
          status.textContent = "预览已更新，公式排版暂不可用";
        });
      }
    }).catch(function (error) {
      if (error.name === "AbortError" || sequence !== requestSequence) return;
      status.textContent = error.message || "预览失败";
      status.classList.add("error");
      retry.hidden = false;
    });
  }

  function schedulePreview() {
    window.clearTimeout(timer);
    timer = window.setTimeout(renderPreview, 180);
  }

  form.addEventListener("change", schedulePreview);
  retry.addEventListener("click", renderPreview);
  renderPreview();
}());
