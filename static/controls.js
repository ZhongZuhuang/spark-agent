/**
 * Spark Controls — 本项目自有控件库（无第三方依赖）
 * 增强原生 select / checkbox / number，保留 id 与 .value/.checked，
 * 业务代码（loadSettings/saveSettings）几乎不用改。
 *
 * 控件：
 *  - .sp-select     自定义下拉（size>=4 或 size 属性 → 列表框）
 *  - .sp-switch     开关
 *  - .sp-stepper    数字加减
 *  - .sp-tabs       顶栏 Tab（由 data-tab 驱动）
 */
(function () {
  "use strict";

  function el(tag, className, attrs) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (v != null) node.setAttribute(k, String(v));
      }
    }
    return node;
  }

  function optionsOf(select) {
    return Array.from(select.options).map((opt) => ({
      value: opt.value,
      label: opt.textContent || opt.value,
      disabled: !!opt.disabled,
      selected: opt.selected,
    }));
  }

  const SP = {
    /** Close all open popups (click-away / Esc). */
    closeAllSelects(except) {
      document.querySelectorAll(".sp-select.open").forEach((wrap) => {
        if (wrap === except) return;
        wrap.classList.remove("open");
        const menu = wrap.querySelector(".sp-select-popup");
        if (menu) menu.hidden = true;
        const trig = wrap.querySelector(".sp-select-trigger");
        if (trig) trig.setAttribute("aria-expanded", "false");
      });
    },

    refresh(select) {
      if (!select || !select.dataset.spDone) return;
      const kind = select.dataset.spKind;
      if (kind === "listbox") this._syncListbox(select);
      else this._syncDropdown(select);
    },

    enhanceAll(root) {
      const scope = root || document;
      scope.querySelectorAll("select:not([data-sp-done])").forEach((s) => this.enhanceSelect(s));
      scope.querySelectorAll('input[type="checkbox"]:not([data-sp-done])').forEach((c) => this.enhanceSwitch(c));
      scope.querySelectorAll('input[type="number"]:not([data-sp-done])').forEach((n) => this.enhanceStepper(n));
    },

    enhanceSelect(select) {
      if (select.dataset.spDone) return;
      const asList = select.size >= 4 || select.multiple;
      select.dataset.spDone = "1";
      select.dataset.spKind = asList ? "listbox" : "dropdown";
      select.setAttribute("aria-hidden", "true");
      select.tabIndex = -1;
      select.classList.add("sp-select-source");

      if (asList) this._buildListbox(select);
      else this._buildDropdown(select);
    },

    _buildDropdown(select) {
      const wrap = el("div", "sp-select", { "data-sp-for": select.id || "" });
      const trigger = el("button", "sp-select-trigger", {
        type: "button",
        "aria-haspopup": "listbox",
        "aria-expanded": "false",
      });
      const text = el("span", "sp-select-text");
      const caret = el("span", "sp-select-caret", { "aria-hidden": "true" });
      trigger.append(text, caret);

      const popup = el("div", "sp-select-popup", { role: "listbox", hidden: "" });
      wrap.append(trigger, popup);
      select.parentNode.insertBefore(wrap, select);
      wrap.appendChild(select);
      this._syncDropdown(select);

      trigger.addEventListener("click", (e) => {
        e.stopPropagation();
        const willOpen = !wrap.classList.contains("open");
        SP.closeAllSelects(wrap);
        wrap.classList.toggle("open", willOpen);
        popup.hidden = !willOpen;
        trigger.setAttribute("aria-expanded", String(willOpen));
      });

      popup.addEventListener("click", (e) => {
        const opt = e.target.closest(".sp-select-option");
        if (!opt || opt.dataset.disabled === "1") return;
        select.value = opt.dataset.value;
        select.dispatchEvent(new Event("change", { bubbles: true }));
        this._syncDropdown(select);
        wrap.classList.remove("open");
        popup.hidden = true;
        trigger.setAttribute("aria-expanded", "false");
      });
    },

    _syncDropdown(select) {
      const wrap = select.parentNode && select.parentNode.classList?.contains("sp-select")
        ? select.parentNode
        : select.closest(".sp-select");
      if (!wrap) return;
      const opts = optionsOf(select);
      const current = opts.find((o) => o.selected) || opts.find((o) => o.value === select.value);
      const text = wrap.querySelector(".sp-select-text");
      if (text) text.textContent = current ? current.label : "—";
      const popup = wrap.querySelector(".sp-select-popup");
      if (!popup) return;
      popup.innerHTML = "";
      for (const o of opts) {
        const item = el("div", "sp-select-option" + (o.selected ? " selected" : ""), {
          role: "option",
          "data-value": o.value,
        });
        if (o.disabled) item.dataset.disabled = "1";
        const label = el("span", "sp-select-option-label");
        label.textContent = o.label;
        const mark = el("span", "sp-select-option-check", { "aria-hidden": "true" });
        mark.textContent = o.selected ? "✓" : "";
        item.append(label, mark);
        popup.appendChild(item);
      }
    },

    _buildListbox(select) {
      const wrap = el("div", "sp-listbox", { "data-sp-for": select.id || "", role: "listbox" });
      select.parentNode.insertBefore(wrap, select);
      wrap.appendChild(select);
      this._syncListbox(select);
      wrap.addEventListener("click", (e) => {
        const item = e.target.closest(".sp-listbox-item");
        if (!item || item.dataset.disabled === "1") return;
        select.value = item.dataset.value;
        select.dispatchEvent(new Event("change", { bubbles: true }));
        this._syncListbox(select);
      });
    },

    _syncListbox(select) {
      const wrap = select.parentNode && select.parentNode.classList?.contains("sp-listbox")
        ? select.parentNode
        : select.closest(".sp-listbox");
      if (!wrap) return;
      const opts = optionsOf(select);
      // keep native select for value; render sibling list
      let list = wrap.querySelector(".sp-listbox-list");
      if (!list) {
        list = el("div", "sp-listbox-list");
        wrap.appendChild(list);
      }
      list.innerHTML = "";
      for (const o of opts) {
        const item = el("div", "sp-listbox-item" + (o.selected ? " selected" : ""), {
          role: "option",
          "data-value": o.value,
          "aria-selected": String(!!o.selected),
        });
        if (o.disabled) item.dataset.disabled = "1";
        const main = el("div", "sp-listbox-main");
        const name = el("div", "sp-listbox-name");
        name.textContent = o.label;
        const meta = el("div", "sp-listbox-meta");
        const parts = o.label.split("·").map((s) => s.trim());
        if (parts.length > 1) meta.textContent = parts.slice(1).join(" · ");
        main.append(name, meta);
        const mark = el("span", "sp-listbox-check", { "aria-hidden": "true" });
        mark.textContent = o.selected ? "✓" : "";
        item.append(main, mark);
        list.appendChild(item);
      }
    },

    enhanceSwitch(input) {
      if (input.dataset.spDone) return;
      input.dataset.spDone = "1";
      input.classList.add("sp-switch-source");
      // Expected markup: <label class="sp-switch"><input type="checkbox">…track…</label>
      let wrap = input.closest(".sp-switch");
      if (!wrap) {
        wrap = el("label", "sp-switch");
        input.parentNode.insertBefore(wrap, input);
        wrap.appendChild(input);
      }
      let track = wrap.querySelector(".sp-switch-track");
      if (!track) {
        track = el("span", "sp-switch-track");
        track.appendChild(el("span", "sp-switch-thumb"));
        wrap.appendChild(track);
      }
      const sync = () => wrap.classList.toggle("checked", !!input.checked);
      input.addEventListener("change", sync);
      wrap.addEventListener("click", (e) => {
        if (e.target === input) return;
        e.preventDefault();
        input.checked = !input.checked;
        input.dispatchEvent(new Event("change", { bubbles: true }));
        sync();
      });
      sync();
    },

    enhanceStepper(input) {
      if (input.dataset.spDone) return;
      input.dataset.spDone = "1";
      input.classList.add("sp-stepper-source");
      const wrap = el("div", "sp-stepper");
      const minus = el("button", "sp-stepper-btn", { type: "button", "aria-label": "减少" });
      minus.textContent = "−";
      const plus = el("button", "sp-stepper-btn", { type: "button", "aria-label": "增加" });
      plus.textContent = "+";
      input.parentNode.insertBefore(wrap, input);
      wrap.append(minus, input, plus);

      const clamp = (n) => {
        let v = n;
        const min = input.min !== "" ? Number(input.min) : -Infinity;
        const max = input.max !== "" ? Number(input.max) : Infinity;
        if (v < min) v = min;
        if (v > max) v = max;
        return v;
      };
      const step = () => (input.step !== "" && Number(input.step) ? Number(input.step) : 1);
      const emit = () => input.dispatchEvent(new Event("input", { bubbles: true }));

      minus.addEventListener("click", () => {
        input.value = String(clamp(Number(input.value || 0) - step()));
        emit();
      });
      plus.addEventListener("click", () => {
        input.value = String(clamp(Number(input.value || 0) + step()));
        emit();
      });
      input.addEventListener("change", () => {
        input.value = String(clamp(Number(input.value || 0)));
      });
    },
  };

  // Click-away / Esc for dropdowns
  document.addEventListener("click", () => SP.closeAllSelects());
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") SP.closeAllSelects();
  });

  window.SP = SP;
})();
