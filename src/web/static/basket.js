(function () {
  "use strict";

  document.addEventListener("submit", async function (event) {
    var form = event.target.closest("form.basket-inline-form");
    if (!form) return;
    event.preventDefault();

    var button = form.querySelector('button[type="submit"]');
    var status = form.querySelector(".basket-status");
    if (!button || button.disabled) return;
    var oldLabel = button.textContent;
    button.disabled = true;
    button.textContent = "处理中…";
    if (status) { status.textContent = ""; status.classList.remove("error"); }

    try {
      var response = await fetch(form.action, {
        method: "POST",
        headers: {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
        body: new URLSearchParams(new FormData(form)).toString(),
        credentials: "same-origin"
      });
      var result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.error || "操作失败");
      button.textContent = result.label;
      button.classList.toggle("joined", result.in_basket);
      form.action = form.action.replace(/\/basket\/(?:add|remove)\//, "/basket/" + (result.in_basket ? "remove" : "add") + "/");
      var count = document.querySelector("[data-basket-count]");
      if (count) count.textContent = "选题篮（" + result.basket_count + "）";
      if (status) { status.textContent = result.in_basket ? "已加入" : "已移出"; setTimeout(function () { status.textContent = ""; }, 1500); }
    } catch (error) {
      button.textContent = oldLabel;
      if (status) { status.textContent = error.message || "操作失败，请重试"; status.classList.add("error"); }
    } finally {
      button.disabled = false;
    }
  });
}());
