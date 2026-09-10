/* Declarative forms with dirty tracking. A page posts only its own keys. */
import { h, toast } from "../ui.js";
import { icon } from "../icons.js";

/** Build one field element from a spec. */
export function fieldEl(spec) {
  const id = "f_" + spec.name;
  if (spec.type === "switch") {
    return h("label", { class: "switch-row", for: id },
      h("span", null, spec.label, spec.hint ? h("small", null, spec.hint) : null),
      h("input", { type: "checkbox", class: "switch", name: spec.name, id }));
  }
  let input;
  if (spec.type === "select") {
    input = h("select", { name: spec.name, id },
      spec.options.map((o) => h("option", { value: o.value ?? o }, o.label ?? o)));
  } else if (spec.type === "textarea") {
    input = h("textarea", { name: spec.name, id, rows: spec.rows || 4, spellcheck: "false", placeholder: spec.placeholder });
  } else if (spec.type === "password") {
    const inp = h("input", { type: "password", name: spec.name, id, placeholder: spec.placeholder, autocomplete: "off" });
    const eye = h("button", { type: "button", class: "btn btn-ghost btn-icon", title: "Show / hide",
      onClick: () => { const show = inp.type === "password"; inp.type = show ? "text" : "password"; eye.replaceChildren(icon(show ? "eyeOff" : "eye")); } },
      icon("eye"));
    input = h("div", { style: "display:flex;gap:6px;align-items:center" }, inp, eye);
  } else {
    input = h("input", {
      type: spec.type === "list" ? "text" : (spec.type || "text"), name: spec.name, id,
      placeholder: spec.placeholder, min: spec.min, max: spec.max, step: spec.step, autocomplete: "off",
      inputmode: spec.type === "number" ? "decimal" : null,
    });
  }
  return h("div", { class: "field", style: spec.width ? `max-width:${spec.width}` : null },
    h("label", { for: id }, spec.label),
    input,
    spec.hint ? h("div", { class: "field-hint" }, spec.hint) : null);
}

export function setValues(form, values, specs) {
  for (const s of specs) {
    const el = form.elements[s.name];
    if (!el) continue;
    const v = values ? values[s.name] : undefined;
    if (s.type === "switch") el.checked = !!v;
    else if (s.type === "list") el.value = (v || []).join(", ");
    else el.value = v ?? "";
  }
}

export function getValues(form, specs) {
  const out = {};
  for (const s of specs) {
    const el = form.elements[s.name];
    if (!el) continue;
    if (s.type === "switch") out[s.name] = el.checked;
    else if (s.type === "number") { if (el.value !== "") out[s.name] = Number(el.value); }   // blank = "leave as is", never 0
    else if (s.type === "list") out[s.name] = el.value.split(",").map((x) => x.trim()).filter(Boolean);
    else out[s.name] = el.value;
  }
  return out;
}

/**
 * settingsForm({ sections: [{title, hint, fields: [spec], after: node}], values, onSave(values) })
 * → { el, form, specs, setValues(values), dirty }
 * Renders a sticky "unsaved changes" bar with Save / Discard.
 */
export function settingsForm({ sections, values, onSave, saveLabel = "Save changes", grid = true }) {
  const specs = sections.flatMap((s) => s.fields || []);
  const form = h("form", { novalidate: true });
  const cards = sections.map((sec) => h("div", { class: "card" },
    sec.title ? h("div", { class: "card-head" }, h("h2", null, sec.title), sec.actions ? h("div", { class: "actions" }, sec.actions) : null) : null,
    sec.hint ? h("p", { class: "hint" }, sec.hint) : null,
    sec.before || null,
    (sec.fields || []).map(fieldEl),
    sec.after || null));
  const saveBtn = h("button", { type: "submit", class: "btn btn-primary", disabled: true }, saveLabel);
  const discardBtn = h("button", { type: "button", class: "btn btn-ghost", disabled: true }, "Discard");
  const msg = h("span", { class: "msg muted" }, "No unsaved changes");
  const bar = h("div", { class: "dirty-bar clean" }, msg, discardBtn, saveBtn);
  if (grid && cards.length > 1) form.append(h("div", { class: "grid grid-2" }, cards), bar);
  else form.append(...cards, bar);

  let saved = {};
  let dirty = false;
  function setDirty(on) {
    dirty = on;
    bar.classList.toggle("clean", !on);
    saveBtn.disabled = !on;
    discardBtn.disabled = !on;
    msg.textContent = on ? "You have unsaved changes" : "No unsaved changes";
    msg.className = on ? "msg" : "msg muted";
  }
  function apply(v) {
    saved = v || {};
    setValues(form, saved, specs);
    setDirty(false);
  }
  form.addEventListener("input", () => setDirty(true));
  form.addEventListener("change", () => setDirty(true));
  discardBtn.addEventListener("click", () => apply(saved));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    saveBtn.disabled = true;
    saveBtn.textContent = "Saving…";
    try {
      // post only what this form changed: a switch flipped elsewhere meanwhile
      // (the topbar's Trading switch, another device) must not be undone here
      const all = getValues(form, specs);
      const changed = {};
      for (const [k, v] of Object.entries(all)) if (JSON.stringify(v) !== JSON.stringify(saved ? saved[k] : undefined)) changed[k] = v;
      const next = await onSave(Object.keys(changed).length ? changed : all);
      apply(next || all);
      toast("Settings saved", "success");
    } catch (err) {
      toast(err.message, "error");
      setDirty(true);
    } finally {
      saveBtn.textContent = saveLabel;
      if (dirty) saveBtn.disabled = false;
    }
  });
  apply(values);
  return { el: form, form, specs, setValues: apply, isDirty: () => dirty };
}
