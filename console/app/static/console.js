/* Console shared behaviors: readable JSON rendering + modal close. */

const _jsonEscape = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;");

for (const pre of document.querySelectorAll("pre.snapshot.json")) {
  const raw = pre.textContent;
  pre.innerHTML = raw.replace(
    /("(?:\\.|[^"\\])*")(\s*:)?|(-?\d+(?:\.\d+)?)|\b(true|false|null)\b/g,
    (m, str, colon, num, lit) => {
      if (str) {
        if (colon) return `<span class="json-key">${_jsonEscape(str)}</span>${colon}`;
        let value;
        try { value = JSON.parse(str); } catch { value = str.slice(1, -1); }
        const display = _jsonEscape(String(value))
          .replace(/\\n/g, "\n")
          .replace(/\\t/g, "\t")
          .replace(/\\"/g, '"')
          .replace(/\\\\/g, "\\")
          .replace(/\n/g, "<br>");
        return `<span class="json-str">"${display}"</span>`;
      }
      if (num) return `<span class="json-num">${num}</span>`;
      return `<span class="json-lit">${lit}</span>`;
    }
  );
}

for (const el of document.querySelectorAll("[data-close]")) {
  el.addEventListener("click", () => {
    document.getElementById(el.dataset.close)?.classList.add("hidden");
  });
}

/* Row links: the whole row navigates to data-href. */
for (const row of document.querySelectorAll("tr[data-href]")) {
  row.addEventListener("click", (ev) => {
    if (ev.target.closest("a, button, input, select, textarea")) return;
    window.location.href = row.dataset.href;
  });
}

/* File pickers: hidden input + visible read-only display + Browse button. */
for (const btn of document.querySelectorAll("button[data-browse]")) {
  const file = document.getElementById(btn.dataset.browse);
  if (!file) continue;
  const display = btn.closest(".input-group")?.querySelector("input[readonly]");
  btn.addEventListener("click", () => file.click());
  file.addEventListener("change", () => {
    if (display) display.value = file.files.length ? file.files[0].name : "";
  });
}

/* Confirm modal: buttons with data-confirm-* pause their form submit. */
{
  const modal = document.getElementById("confirm-modal");
  let pendingForm = null;
  const close = () => modal?.classList.add("hidden");
  for (const btn of document.querySelectorAll("button[data-confirm-title]")) {
    btn.addEventListener("click", (ev) => {
      ev.preventDefault();
      if (!modal) return;
      pendingForm = btn.closest("form");
      document.getElementById("confirm-title").textContent = btn.dataset.confirmTitle;
      document.getElementById("confirm-message").textContent = btn.dataset.confirmMessage || "";
      const action = document.getElementById("confirm-action");
      action.textContent = btn.dataset.confirmLabel || "确认";
      const inputField = document.getElementById("confirm-input-field");
      const needsInput = Boolean(btn.dataset.confirmInput);
      inputField.classList.toggle("hidden", !needsInput);
      if (needsInput) {
        inputField.querySelector(".field-label").textContent = btn.dataset.confirmInput;
        document.getElementById("confirm-input").value = "";
      }
      modal.classList.remove("hidden");
      if (needsInput) document.getElementById("confirm-input").focus();
    });
  }
  document.getElementById("confirm-action")?.addEventListener("click", () => {
    const input = document.getElementById("confirm-input");
    if (pendingForm && !input.closest(".field").classList.contains("hidden")) {
      const target = pendingForm.querySelector("[data-confirm-field]");
      if (target) target.value = input.value.trim();
    }
    pendingForm?.submit();
    close();
  });
}

/* ── Select: Radix-style trigger + popover over the native select ───── */

const _chevronSvg = `<svg class="c-select-chevron" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M4 6l4 4 4-4"/></svg>`;
const _checkSvg = `<svg class="check" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M3.5 8.5l3 3 6-7"/></svg>`;

function initCustomSelect(native) {
  if (native.dataset.customSelect) return;
  native.dataset.customSelect = "1";
  native.classList.add("c-select-native");

  const wrap = document.createElement("div");
  wrap.className = "c-select";
  native.parentNode.insertBefore(wrap, native);
  wrap.appendChild(native);

  const trigger = document.createElement("button");
  trigger.type = "button";
  trigger.className = "c-select-trigger";
  trigger.innerHTML = `<span class="c-select-value"></span>${_chevronSvg}`;
  wrap.appendChild(trigger);

  const pop = document.createElement("div");
  pop.className = "c-select-pop hidden";
  wrap.appendChild(pop);

  const valueEl = trigger.querySelector(".c-select-value");

  const renderValue = () => {
    const opt = native.options[native.selectedIndex];
    valueEl.textContent = opt ? opt.textContent : "";
    valueEl.classList.toggle("placeholder", !opt || !opt.value);
  };

  const renderItems = () => {
    pop.innerHTML = "";
    for (const child of native.children) {
      if (child.tagName === "OPTGROUP") {
        const label = document.createElement("div");
        label.className = "c-select-group-label";
        label.textContent = child.label;
        pop.appendChild(label);
        for (const opt of Array.from(child.children)) pop.appendChild(makeItem(opt));
      } else {
        pop.appendChild(makeItem(child));
      }
    }
  };

  const makeItem = (opt) => {
    const item = document.createElement("div");
    item.className = "c-select-item" + (opt.selected ? " selected" : "");
    item.innerHTML = `<span>${opt.textContent}</span>${_checkSvg}`;
    item.addEventListener("click", () => {
      native.value = opt.value;
      native.dispatchEvent(new Event("change", { bubbles: true }));
      renderValue();
      for (const el of pop.querySelectorAll(".c-select-item")) {
        el.classList.toggle("selected", el === item);
      }
      close();
    });
    return item;
  };

  const open = () => {
    renderItems();
    pop.classList.remove("hidden");
  };
  const close = () => pop.classList.add("hidden");

  trigger.addEventListener("click", (ev) => {
    ev.stopPropagation();
    pop.classList.contains("hidden") ? open() : close();
  });
  document.addEventListener("click", (ev) => {
    if (!wrap.contains(ev.target)) close();
  });
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") close();
  });
  native.addEventListener("change", renderValue);

  renderValue();
}

for (const sel of document.querySelectorAll("select")) initCustomSelect(sel);
