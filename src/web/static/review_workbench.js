(function () {
  "use strict";

  const form = document.querySelector(".review-editor");
  const typeSelect = form.querySelector('[name="question_type_code"]');
  const optionEditor = form.querySelector('[data-structure="options"]');
  const subquestionEditor = form.querySelector('[data-structure="subquestions"]');
  const subquestionWarning = form.querySelector("[data-subquestion-label-warning]");
  const fillWarning = form.querySelector("[data-fill-option-warning]");
  const preview = form.querySelector("[data-preview-content]");
  const comparison = document.querySelector(".source-recognition-comparison");
  const advanced = document.querySelector(".advanced-review-editor");
  const feedback = document.querySelector("[data-inline-feedback]");
  let previewTimer;
  let activeEdit;

  function syncFullEditor(field, index, value) {
    if (field === "stem_markdown") {
      form.querySelector('[name="stem_markdown"]').value = value;
      return;
    }
    const prefix = field === "option_content" ? "option" : "subquestion";
    const source = form.querySelector(`[name="${prefix}_source_index"][value="${index}"]`);
    if (source) source.closest(".structured-row").querySelector(`[name="${field}"]`).value = value;
  }

  function finishInlineEdit(edit, value, payload) {
    edit.target.dataset.inlineValue = value;
    edit.target.textContent = value;
    edit.editor.replaceWith(edit.target);
    activeEdit = undefined;
    document.querySelectorAll("[data-review-version]").forEach(function (input) {
      input.value = String(payload.version);
    });
    document.querySelectorAll("[data-review-status]").forEach(function (node) {
      node.textContent = payload.status_name;
    });
    document.querySelectorAll(".quick-review").forEach(function (node) {
      node.classList.remove("approved");
    });
    syncFullEditor(payload.field, payload.index, value);
    schedulePreview();
    feedback.hidden = false;
    feedback.textContent = payload.message;
    if (window.MathJax && typeof window.MathJax.typesetPromise === "function") {
      window.MathJax.typesetPromise([edit.target]).catch(function () {});
    }
  }

  async function saveInlineEdit() {
    const edit = activeEdit;
    if (!edit || edit.saving) return;
    edit.saving = true;
    edit.editor.querySelectorAll("button").forEach(function (button) {
      button.disabled = true;
    });
    edit.error.textContent = "";
    const params = new URLSearchParams();
    params.set("csrf_token", comparison.dataset.csrfToken);
    params.set("version", document.querySelector("[data-review-version]").value);
    params.set("field", edit.target.dataset.inlineField);
    params.set("value", edit.input.value);
    if (edit.target.dataset.inlineIndex !== undefined) {
      params.set("index", edit.target.dataset.inlineIndex);
    }
    try {
      const response = await fetch(comparison.dataset.inlineUrl, {
        method: "POST",
        headers: {"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
        body: params.toString(),
      });
      const payload = await response.json();
      if (!response.ok || !payload.ok) throw new Error(payload.error || "保存失败，请重试");
      finishInlineEdit(edit, edit.input.value, payload);
    } catch (error) {
      edit.error.textContent = error.message || "保存失败，请重试";
      edit.saving = false;
      edit.editor.querySelectorAll("button").forEach(function (button) {
        button.disabled = false;
      });
      edit.input.focus();
    }
  }

  function cancelInlineEdit() {
    if (!activeEdit || activeEdit.saving) return;
    const edit = activeEdit;
    edit.editor.replaceWith(edit.target);
    activeEdit = undefined;
    edit.target.focus();
  }

  function startInlineEdit(target) {
    if (activeEdit) return;
    const editor = element("div", undefined, "inline-editor");
    const input = document.createElement("textarea");
    input.value = target.dataset.inlineValue;
    input.maxLength = target.dataset.inlineField === "stem_markdown" ? 20000 : 10000;
    input.rows = target.dataset.inlineField === "stem_markdown" ? 4 : 2;
    input.setAttribute("aria-label", target.getAttribute("aria-label"));
    const actions = element("div", undefined, "inline-editor-actions");
    const save = actionButton("保存", "data-inline-save");
    const cancel = actionButton("取消", "data-inline-cancel");
    const error = element("p", "", "inline-edit-error");
    error.setAttribute("role", "alert");
    actions.append(save, cancel);
    editor.append(input, actions, error);
    target.replaceWith(editor);
    activeEdit = {target: target, editor: editor, input: input, error: error, saving: false};
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
  }

  if (comparison) {
    comparison.addEventListener("click", function (event) {
      const openAdvanced = event.target.closest("[data-open-advanced]");
      const save = event.target.closest("[data-inline-save]");
      const cancel = event.target.closest("[data-inline-cancel]");
      const trigger = event.target.closest("[data-inline-field]");
      if (openAdvanced) {
        advanced.open = true;
        document.getElementById(openAdvanced.dataset.openAdvanced).focus();
      } else if (save) {
        saveInlineEdit();
      } else if (cancel) {
        cancelInlineEdit();
      } else if (trigger && comparison.contains(trigger)) {
        startInlineEdit(trigger);
      }
    });
    comparison.addEventListener("keydown", function (event) {
      const trigger = event.target.closest('[role="button"][data-inline-field]');
      if (trigger && (event.key === "Enter" || event.key === " ")) {
        event.preventDefault();
        startInlineEdit(trigger);
      } else if (activeEdit && event.target === activeEdit.input && event.key === "Escape") {
        event.preventDefault();
        cancelInlineEdit();
      } else if (activeEdit && event.target === activeEdit.input && event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        saveInlineEdit();
      }
    });
  }

  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  }

  function hiddenInput(name, value) {
    const input = document.createElement("input");
    input.type = "hidden";
    input.name = name;
    input.value = value;
    return input;
  }

  function actionButton(text, attribute, value) {
    const button = element("button", text);
    button.type = "button";
    button.setAttribute(attribute, value === undefined ? "" : value);
    return button;
  }

  function labelWithControl(text, control) {
    const label = element("label", text);
    label.append(control);
    return label;
  }

  function nextOptionCode() {
    const used = new Set(Array.from(form.querySelectorAll('[name="option_code"]'), input => input.value.trim()));
    for (let code = 65; code <= 90; code += 1) {
      const candidate = String.fromCharCode(code);
      if (!used.has(candidate)) return candidate;
    }
    return "选项";
  }

  function createOptionRow() {
    const row = element("div", undefined, "structured-row");
    row.append(hiddenInput("option_source_index", ""), hiddenInput("option_order", ""));
    const code = document.createElement("input");
    code.name = "option_code";
    code.value = nextOptionCode();
    code.maxLength = 16;
    code.required = true;
    const content = document.createElement("textarea");
    content.name = "option_content";
    content.maxLength = 10000;
    const actions = element("div", undefined, "row-actions");
    actions.append(actionButton("上移", "data-move", "up"), actionButton("下移", "data-move", "down"), actionButton("删除", "data-remove"));
    row.append(labelWithControl("选项标识", code), labelWithControl("选项内容", content), actions);
    return row;
  }

  function createSubquestionRow() {
    const row = element("div", undefined, "structured-row");
    row.dataset.originalLabel = "";
    row.dataset.labelTitle = "";
    row.append(hiddenInput("subquestion_source_index", ""), hiddenInput("subquestion_order", ""));
    row.append(element("span", "", "subquestion-number"));
    const content = document.createElement("textarea");
    content.name = "subquestion_content";
    content.maxLength = 10000;
    const actions = element("div", undefined, "row-actions");
    actions.append(actionButton("上移", "data-move", "up"), actionButton("下移", "data-move", "down"), actionButton("删除", "data-remove"));
    row.append(labelWithControl("小问题干", content), actions);
    return row;
  }

  function updateOrders(editor) {
    const rows = Array.from(editor.querySelectorAll(".structured-row"));
    rows.forEach((row, index) => {
      const order = row.querySelector('[name$="_order"]');
      if (order) order.value = String(index + 1);
      const number = row.querySelector(".subquestion-number");
      if (number) number.textContent = row.dataset.labelTitle || `第${index + 1}项`;
      const up = row.querySelector('[data-move="up"]');
      const down = row.querySelector('[data-move="down"]');
      if (up) up.disabled = index === 0;
      if (down) down.disabled = index === rows.length - 1;
    });
  }

  function syncType() {
    const choice = typeSelect.value === "single_choice" || typeSelect.value === "multiple_choice";
    optionEditor.hidden = !choice;
    subquestionEditor.hidden = typeSelect.value !== "solution";
    fillWarning.hidden = !(typeSelect.value === "fill_blank" && optionEditor.querySelector(".structured-row"));
    schedulePreview();
  }

  function appendTextItem(list, lead, content) {
    const item = element("li");
    const strong = element("strong", lead);
    item.append(strong, document.createTextNode(content));
    list.append(item);
  }

  function parseSubquestionLabel(label) {
    const match = /^(（(\d+)）|\((\d+)\))(?:（(i{1,3}|iv|v|vi{0,3}|ix|x)）|\((i{1,3}|iv|v|vi{0,3}|ix|x)\))?$/i.exec(label);
    if (!match) return null;
    const mainLabel = match[1];
    return {
      mainNumber: Number(match[2] || match[3]),
      mainLabel: mainLabel,
      child: (match[4] || match[5] || "").toLowerCase(),
      childLabel: label.slice(mainLabel.length),
    };
  }

  function groupSubquestions(rows) {
    const romanOrders = {i: 1, ii: 2, iii: 3, iv: 4, v: 5, vi: 6, vii: 7, viii: 8, ix: 9, x: 10};
    const groups = [];
    const byNumber = new Map();
    let expectedMain = 1;
    for (const row of rows) {
      const parsed = parseSubquestionLabel(row.dataset.originalLabel || "");
      if (!parsed) return null;
      let group = byNumber.get(parsed.mainNumber);
      if (!group) {
        if (parsed.mainNumber !== expectedMain) return null;
        group = {mainLabel: parsed.mainLabel, parent: null, children: [], childOrder: 0};
        groups.push(group);
        byNumber.set(parsed.mainNumber, group);
        expectedMain += 1;
      } else if (parsed.mainNumber !== expectedMain - 1) {
        return null;
      }
      const entry = {row: row, parsed: parsed};
      if (!parsed.child) {
        if (group.parent || group.children.length) return null;
        group.parent = entry;
      } else {
        if (romanOrders[parsed.child] !== group.childOrder + 1) return null;
        group.childOrder += 1;
        group.children.push(entry);
      }
    }
    return groups;
  }

  function appendSubquestionPreview(container) {
    const rows = Array.from(subquestionEditor.querySelectorAll(".structured-row"));
    const groups = groupSubquestions(rows);
    const list = element("ol");
    if (!groups) {
      rows.forEach((row, index) => {
        appendTextItem(
          list,
          `${row.dataset.originalLabel || `第${index + 1}项`}：`,
          row.querySelector('[name="subquestion_content"]').value,
        );
      });
    } else {
      groups.forEach(group => {
        const main = element("li");
        const mainLabel = element("strong", group.mainLabel);
        main.append(mainLabel);
        if (group.parent) {
          main.append(document.createTextNode(group.parent.row.querySelector('[name="subquestion_content"]').value));
        }
        if (group.children.length) {
          const children = element("ol");
          group.children.forEach(entry => {
            appendTextItem(children, entry.parsed.childLabel, entry.row.querySelector('[name="subquestion_content"]').value);
          });
          main.append(children);
        }
        list.append(main);
      });
    }
    container.append(list);
  }

  function renderPreview() {
    preview.replaceChildren();
    const stem = element("p", form.querySelector('[name="stem_markdown"]').value);
    preview.append(stem);
    if (typeSelect.value === "single_choice" || typeSelect.value === "multiple_choice") {
      const list = element("ol");
      optionEditor.querySelectorAll(".structured-row").forEach(row => {
        appendTextItem(list, `${row.querySelector('[name="option_code"]').value}：`, row.querySelector('[name="option_content"]').value);
      });
      preview.append(list);
    } else if (typeSelect.value === "solution") {
      appendSubquestionPreview(preview);
    }
    if (window.MathJax && typeof window.MathJax.typesetPromise === "function") {
      window.MathJax.typesetPromise([preview]).catch(function () {});
    }
  }

  function schedulePreview() {
    window.clearTimeout(previewTimer);
    previewTimer = window.setTimeout(renderPreview, 250);
  }

  form.addEventListener("click", function (event) {
    const addOption = event.target.closest("[data-add-option]");
    const addSubquestion = event.target.closest("[data-add-subquestion]");
    const remove = event.target.closest("[data-remove]");
    const move = event.target.closest("[data-move]");
    if (addOption) optionEditor.querySelector(".structured-list").append(createOptionRow());
    if (addSubquestion) subquestionEditor.querySelector(".structured-list").append(createSubquestionRow());
    if (remove) remove.closest(".structured-row").remove();
    if (move) {
      const row = move.closest(".structured-row");
      const sibling = move.dataset.move === "up" ? row.previousElementSibling : row.nextElementSibling;
      if (sibling) row.parentElement.insertBefore(move.dataset.move === "up" ? row : sibling, move.dataset.move === "up" ? sibling : row);
      if (move.closest('[data-structure="subquestions"]')) subquestionWarning.hidden = false;
    }
    updateOrders(optionEditor);
    updateOrders(subquestionEditor);
    syncType();
  });
  form.addEventListener("input", schedulePreview);
  typeSelect.addEventListener("change", syncType);
  updateOrders(optionEditor);
  updateOrders(subquestionEditor);
  syncType();
  renderPreview();
}());
