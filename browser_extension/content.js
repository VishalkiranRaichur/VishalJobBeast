/* Job Beast Companion: local-only ATS executor. See THIRD_PARTY_NOTICES.md. */
(() => {
  "use strict";
  console.log("[JOB_BEAST] CONTENT_SCRIPT_STARTED", location.href);

  const BRIDGE = "http://127.0.0.1:8765";
  const state = { packet: null, running: false, done: false, packetKey: "", diagnostics: new Set(), applyClicked: false, bridgeConnected: false };
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  try { setBadge({ script: "loaded", state: "CONTENT SCRIPT LOADED" }); } catch (error) { console.error("[JOB_BEAST] diagnostic badge failed", error); }
  const isTopFrame = () => window.top === window;
  const frameType = () => isTopFrame() ? "top" : "iframe";

  function badge() {
    if (!isTopFrame()) return null;
    let node = document.getElementById("jobbeast-diagnostic-badge");
    if (!node) {
      node = document.createElement("pre");
      node.id = "jobbeast-diagnostic-badge";
      Object.assign(node.style, { position: "fixed", top: "8px", right: "8px", zIndex: "2147483647", margin: "0", padding: "8px 10px", maxWidth: "360px", background: "#111827", color: "#d1fae5", border: "1px solid #10b981", borderRadius: "6px", font: "11px/1.35 monospace", whiteSpace: "pre-wrap", pointerEvents: "none", boxShadow: "0 2px 10px rgba(0,0,0,.25)" });
      (document.body || document.documentElement).appendChild(node);
    }
    return node;
  }

  function setBadge(patch = {}) {
    const node = badge();
    if (!node) return;
    const current = state.badge || {};
    Object.assign(current, patch);
    state.badge = current;
    node.textContent = [
      "JOB BEAST",
      `script: ${current.script || "loaded"}`,
      `bridge: ${current.bridge || "waiting"}`,
      `job: ${current.providerJob || "-"}`,
      `packet: ${current.internalJob || "-"}`,
      `adapter: ${current.adapter || "-"}`,
      `frame: ${frameType()}`,
      `form: ${current.form || "waiting"}`,
      `fields: ${current.fields || "0 / ?"}`,
      `state: ${current.state || "STARTING"}`
    ].join("\n");
  }

  async function diagnostic(type, details = {}, packet = null) {
    const event = { type, frame: frameType(), url: location.href, timestamp: new Date().toISOString(), ...details };
    const dedupeKey = `${type}:${event.url}`;
    if (state.diagnostics.has(dedupeKey)) return;
    try {
      const response = await fetch(`${BRIDGE}/diagnostics`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(event) });
      if (!response.ok) throw new Error(`diagnostic HTTP ${response.status}`);
      state.diagnostics.add(dedupeKey);
      if (type === "BRIDGE_CONNECTED") { state.bridgeConnected = true; setBadge({ bridge: "connected" }); }
    } catch (error) {
      state.diagnostics.delete(dedupeKey);
      if (type === "CONTENT_SCRIPT_STARTED" || type === "BRIDGE_CONNECTED") setBadge({ bridge: "connection failed" });
    }
    if (packet) {
      try { await fetch(`${BRIDGE}/application/${encodeURIComponent(packet.internal_job_id || packet.job_id)}/events`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(event) }); } catch (_) { /* diagnostic already attempted */ }
    }
  }

  function normalize(value) {
    return String(value || "")
      .replace(/[\u200b-\u200d\u2060\ufeff]/g, " ")
      .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
      .replace(/[_-]+/g, " ")
      .replace(/\s+/g, " ")
      .trim()
      .toLowerCase();
  }

  function humanize(value) { return normalize(value); }

  function labelParts(el) {
    const out = [];
    if (el.id) {
      const label = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (label) out.push(label.textContent || "");
    }
    const wrapping = el.closest("label");
    if (wrapping) out.push(wrapping.textContent || "");
    for (const id of (el.getAttribute("aria-labelledby") || "").split(/\s+/).filter(Boolean)) {
      const ref = document.getElementById(id);
      if (ref) out.push(ref.textContent || "");
    }
    for (const attr of ["aria-label", "placeholder", "name", "id", "data-automation-id", "data-fkit-id"]) {
      if (el.getAttribute(attr)) out.push(humanize(el.getAttribute(attr)));
    }
    // Some Greenhouse questions render a visible label in the field wrapper
    // without associating it with the input through label[for].
    const wrapper = el.closest(".field-wrapper, .application-question, .field, [data-qa]");
    if (wrapper) out.push(wrapper.innerText || wrapper.textContent || "");
    let parent = el.parentElement;
    for (let depth = 0; parent && depth < 3 && parent.tagName !== "FORM"; depth += 1, parent = parent.parentElement) {
      const text = (parent.innerText || parent.textContent || "").replace(/\s+/g, " ").trim();
      if (text && text.length <= 180) out.push(text);
    }
    let sibling = el.previousElementSibling;
    for (let depth = 0; sibling && depth < 2; depth += 1, sibling = sibling.previousElementSibling) {
      const text = (sibling.innerText || sibling.textContent || "").replace(/\s+/g, " ").trim();
      if (text && text.length <= 180) out.push(text);
    }
    return out;
  }

  function labelText(el) { return normalize(labelParts(el).join(" ")); }
  function visible(el) { return !!el && !!(el.offsetParent || el.getClientRects().length); }
  function isCombo(el) {
    return el.tagName === "SELECT" || el.getAttribute("role") === "combobox" ||
      ["aria-haspopup", "aria-autocomplete", "aria-expanded", "aria-controls", "list"].some((a) => el.hasAttribute(a)) ||
      !!el.closest('[role="combobox"], [role="listbox"]');
  }

  function controls() {
    return [...document.querySelectorAll("input, textarea, select")].filter((el) => visible(el) && !el.disabled && !el.readOnly);
  }

  function findField(patterns, { combo = false } = {}) {
    const regexes = patterns.map((p) => {
      if (!(p instanceof RegExp)) return new RegExp(p, "i");
      return new RegExp(p.source, p.flags.includes("i") ? p.flags : `${p.flags}i`);
    });
    const candidates = controls().filter((el) => combo ? isCombo(el) : !isCombo(el));
    return candidates
      .map((el) => ({ el, score: regexes.reduce((n, re) => n + (re.test(labelText(el)) ? 1 : 0), 0) }))
      .filter((x) => x.score)
      .sort((a, b) => b.score - a.score)[0]?.el || null;
  }

  function nativeSet(el, value) {
    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
    if (!setter) throw new Error(`No native value setter for ${labelText(el)}`);
    setter.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function nearbyError(el) {
    const group = el.closest(".field-wrapper, fieldset, [role=group], .input-wrapper, .select__container") || el.parentElement;
    const texts = [group?.innerText || "", ...[...document.querySelectorAll('[role=alert], [aria-live="assertive"]')].filter(visible).map((x) => x.innerText || "")];
    return texts.find((t) => /this field is required|required|invalid|error/i.test(t)) || "";
  }

  function verify(el, expected) {
    const actual = el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement ? el.value : el.value;
    const invalid = el.getAttribute("aria-invalid") === "true";
    const error = nearbyError(el);
    const sameText = actual.trim() === String(expected).trim();
    const samePhone = /phone|mobile|tel/.test(labelText(el)) && actual.replace(/\D/g, "") === String(expected).replace(/\D/g, "");
    return { label: labelText(el), expected_value: expected, actual_value: actual, verified: (sameText || samePhone) && !invalid && !error, error_text: error };
  }

  async function fillText(patterns, expected, fields) {
    if (expected == null || expected === "") return;
    let result = null;
    for (let attempt = 0; attempt < 3; attempt += 1) {
      const el = findField(patterns);
      if (!el) break;
      el.focus();
      nativeSet(el, String(expected));
      el.blur();
      await sleep(250 + attempt * 150);
      const current = findField(patterns) || el;
      result = verify(current, String(expected));
      if (result.verified) break;
    }
    fields.push(result || { label: patterns[0].toString(), expected_value: expected, actual_value: "", verified: false, error_text: "control not found" });
  }

  // First comma-separated segment of a location ("Springfield, Illinois, US" -> "springfield").
  function cityOf(value) {
    return normalize(String(value || "").split(",")[0] || "");
  }

  async function choose(patterns, expected, fields, optionPattern = expected) {
    if (expected == null || expected === "") return;
    const el = findField(patterns, { combo: true });
    if (!el) { fields.push({ label: patterns[0].toString(), expected_value: expected, actual_value: "", verified: false, error_text: "combobox not found" }); return; }
    const existing = renderedSelection(el);
    const expectedCity = cityOf(expected);
    const existingLocation = /location/i.test(patterns[0].toString()) && !!expectedCity && normalize(existing).startsWith(expectedCity);
    if (existingLocation) {
      fields.push({ label: patterns[0].toString(), expected_value: expected, actual_value: existing, verified: true, error_text: "", voluntary: false, required: true });
      return;
    }
    // Do not overwrite a Greenhouse city autocomplete while it is waiting for
    // a user-selected suggestion. The next poll will verify the same control.
    if (/location/i.test(patterns[0].toString())) {
      fields.push({ label: patterns[0].toString(), expected_value: expected, actual_value: existing, verified: false, error_text: "manual location suggestion selection required", voluntary: false, required: true });
      return;
    }
    const widget = el.closest(".select__control") || el;
    el.focus();
    widget.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
    widget.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
    widget.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    await sleep(100);
    if (el instanceof HTMLSelectElement) {
      const option = [...el.options].find((o) => o.textContent.trim() === String(expected).trim());
      if (option) { el.value = option.value; el.dispatchEvent(new Event("input", { bubbles: true })); el.dispatchEvent(new Event("change", { bubbles: true })); }
    } else {
      nativeSet(el, String(expected).split(",").slice(0, 2).join(",").trim());
      await sleep(400);
      const wanted = normalize(optionPattern);
      const option = [...document.querySelectorAll('[role="option"], .select__option, option')].find((x) => {
        const text = normalize(x.textContent || "");
        return visible(x) && (text === wanted || text.startsWith(wanted) || wanted.startsWith(text) || (!!cityOf(wanted) && text.startsWith(cityOf(wanted))));
      });
      if (option) {
        option.click();
        el.__jobbeastSelectedText = option.textContent || "";
      } else {
        // Greenhouse sometimes exposes the listbox only to its keyboard
        // interaction layer. This fallback is safe only because the rendered
        // selection is read back immediately below.
        el.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
        el.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
        await sleep(200);
      }
    }
    el.blur(); await sleep(150);
    const group = el.closest(".select__control, .select__container, [role=group]");
    const actual = el.__jobbeastSelectedText?.trim() || group?.innerText?.replace(/\s+/g, " ").trim() || el.value;
    const error = nearbyError(el);
    const countryCodeSelection = /country/i.test(patterns[0].toString()) && /\+1|united states/i.test(actual);
    const actualNormalized = normalize(actual);
    const expectedNormalized = normalize(String(expected));
    const locationEquivalent = /location/i.test(patterns[0].toString()) && !!cityOf(expectedNormalized) && actualNormalized.startsWith(cityOf(expectedNormalized));
    fields.push({ label: patterns[0].toString(), expected_value: expected, actual_value: actual, verified: (actualNormalized.includes(expectedNormalized) || locationEquivalent || countryCodeSelection) && !error, error_text: error, voluntary: false, required: true });
  }

  function voluntaryLabel(el) {
    const text = labelText(el);
    return /gender|hispanic|latino|ethnic|race|veteran|disab|voluntary|self.?identif|eeo/.test(text);
  }

  function declineOption(text) {
    return /decline\s+to\s+self[ -]?identify|i\s+(?:do\s+not|don't)\s+wish\s+to\s+answer|prefer\s+not\s+to\s+(?:say|disclose)|choose\s+not\s+to\s+disclose|decline/.test(normalize(text));
  }

  function voluntaryCategory(el) {
    const text = labelText(el);
    if (/hispanic|latino/.test(text)) return "hispanic_latino";
    if (/veteran/.test(text)) return "veteran_status";
    if (/disab/.test(text)) return "disability_status";
    if (/detail|national origin|ethnicity/.test(text)) return "race_detail";
    if (/race|ethnic/.test(text)) return "race";
    if (/gender/.test(text)) return "gender";
    return "";
  }

  function matchesVoluntaryOption(text, expected) {
    const actual = normalize(text);
    const wanted = normalize(expected);
    if (actual === wanted || actual.includes(wanted)) return true;
    if (wanted === "not a protected veteran") return /not.*protected.*veteran/.test(actual);
    if (wanted === "no disability") return /(?:no|do not have|don't have).*disab/.test(actual);
    return false;
  }

  async function chooseVoluntary(el, expected) {
    const wanted = normalize(expected);
    const widget = el.closest(".select__control") || el;
    el.focus();
    widget.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
    widget.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
    widget.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    await sleep(120);
    if (el instanceof HTMLSelectElement) {
      const option = [...el.options].find((item) => matchesVoluntaryOption(item.textContent || "", expected));
      if (!option) return false;
      el.value = option.value;
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
    } else {
      const option = [...document.querySelectorAll('[role="option"], .select__option, option')]
        .find((item) => visible(item) && matchesVoluntaryOption(item.textContent || "", expected));
      if (!option) return false;
      option.click();
    }
    el.blur();
    await sleep(150);
    return matchesVoluntaryOption(renderedSelection(el), expected);
  }

  function renderedSelection(el) {
    if (el instanceof HTMLSelectElement) return el.selectedOptions?.[0]?.textContent?.trim() || "";
    if (el.value?.trim()) return el.value.trim();
    let node = el;
    for (let i = 0; i < 6 && node; i += 1, node = node.parentElement) {
      const selected = node.querySelector?.('[aria-selected="true"], .select__single-value, .select__value-container [class*="singleValue"], [data-value]');
      if (selected?.textContent?.trim()) return selected.textContent.replace(/\s+/g, " ").trim();
    }
    return "";
  }

  async function chooseDecline(el) {
    const widget = el.closest(".select__control") || el;
    el.focus();
    widget.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
    widget.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
    widget.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    await sleep(120);
    if (el instanceof HTMLSelectElement) {
      const option = [...el.options].find((item) => declineOption(item.textContent || ""));
      if (!option) return false;
      el.value = option.value;
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
    } else {
      const option = [...document.querySelectorAll('[role="option"], .select__option, option')]
        .find((item) => visible(item) && declineOption(item.textContent || ""));
      if (!option) return false;
      option.click();
    }
    el.blur();
    await sleep(150);
    return declineOption(renderedSelection(el));
  }

  async function handleVoluntaryEEO(fields) {
    const candidates = controls().filter(voluntaryLabel).filter((el) => isCombo(el) || el.type === "radio" || el.type === "checkbox");
    const seen = new Set();
    for (const el of candidates) {
      const group = el.closest("fieldset, [role=group], .field-wrapper, .select__container") || el.parentElement;
      const key = group || el;
      if (seen.has(key)) continue;
      seen.add(key);
      const required = el.getAttribute("aria-required") === "true" || el.required === true;
      const label = labelText(el);
      let selected = renderedSelection(el);
      const category = voluntaryCategory(el);
      const expected = state.packet.voluntary_eeo?.[category] || "";
      if (expected) {
        const verified = await chooseVoluntary(el, expected);
        selected = renderedSelection(el);
        fields.push({ label, voluntary: true, required, selected_value: selected, expected_value: expected, actual_value: selected, verified, error_text: verified ? "" : "confirmed EEO option not found or not persisted" });
      } else if (!required) {
        fields.push({ label, voluntary: true, required: false, selected_value: selected === "Select..." ? "" : selected, expected_value: "", actual_value: selected === "Select..." ? "" : selected, verified: selected === "" || /^select/i.test(selected), error_text: "" });
        continue;
      } else {
        const verified = await chooseDecline(el);
        selected = renderedSelection(el);
        fields.push({ label, voluntary: true, required: true, selected_value: selected, expected_value: "Decline / non-disclosure option", actual_value: selected, verified, error_text: verified ? "" : "required voluntary field has no verified non-disclosure option" });
      }
    }
  }

  async function uploadResume(fields) {
    const input = document.querySelector("#resume") || [...document.querySelectorAll('input[type=file]')].find((x) => /resume/i.test(labelText(x)));
    if (!input) { fields.push({ label: "Resume/CV", expected_value: state.packet.resume.filename, actual_value: "", verified: false, error_text: "resume input not found" }); return; }
    const response = await fetch(`${BRIDGE}/application/${encodeURIComponent(state.packet.job_id)}/resume`);
    if (!response.ok) throw new Error(`resume fetch failed: ${response.status}`);
    const blob = await response.blob();
    const file = new File([blob], state.packet.resume.filename, { type: "application/pdf" });
    const transfer = new DataTransfer(); transfer.items.add(file); input.files = transfer.files;
    input.dispatchEvent(new Event("change", { bubbles: true })); input.dispatchEvent(new Event("input", { bubbles: true }));
    await sleep(400);
    const actual = input.files?.[0]?.name || "";
    const group = input.closest("[aria-labelledby], .file-upload");
    const ui = group?.innerText || "";
    fields.push({ label: "Resume/CV", expected_value: state.packet.resume.filename, actual_value: actual || ui, verified: actual === state.packet.resume.filename || ui.includes(state.packet.resume.filename), error_text: actual ? "" : "uploaded file not reflected", voluntary: false, required: true });
  }

  function answer(key) { return state.packet.answers?.[key]; }
  function answerAny(keys) {
    for (const key of keys) {
      const value = answer(key);
      if (value !== undefined && value !== null && value !== "" && value !== "UNKNOWN" && value !== "TODO" && value !== "REQUIRES_USER_INPUT") return value;
    }
    return undefined;
  }

  function recordRequiredIfPresent(patterns, fields, expectedValue, label) {
    const el = findField(patterns, { combo: false }) || findField(patterns, { combo: true });
    if (!el) return;
    if (expectedValue !== undefined && expectedValue !== null && expectedValue !== "") return;
    fields.push({ label: label || patterns[0].toString(), expected_value: "", actual_value: el.value || renderedSelection(el), verified: false, error_text: "verified answer not available" });
  }

  function pageIdentity(packet) {
    const target = new URL(packet.official_application_url);
    const here = new URL(location.href);
    const sameAtsFamily =
      (/greenhouse\.io$/i.test(here.hostname) && /greenhouse\.io$/i.test(target.hostname)) ||
      (/lever\.co$/i.test(here.hostname) && /lever\.co$/i.test(target.hostname)) ||
      (/ashbyhq\.com$/i.test(here.hostname) && /ashbyhq\.com$/i.test(target.hostname)) ||
      (/myworkday(?:jobs|site)?\.com$/i.test(here.hostname) && /myworkday(?:jobs|site)?\.com$/i.test(target.hostname));
    if (!sameAtsFamily && here.hostname !== target.hostname) return false;
    if (here.href.startsWith(target.href)) return true;

    // A stale tab from another Greenhouse employer must never consume the
    // active packet. Both job-board URLs carry a stable numeric requisition
    // id; require it to match when one is available. This prevents the
    // Astranis tab from being mistaken for a later Enova (or other) job.
    const targetJobId = target.pathname.match(/(?:jobs|job_app)[^/]*\/(\d+)/i)?.[1];
    const currentJobId = here.pathname.match(/(?:jobs|job_app)[^/]*\/(\d+)/i)?.[1];
    const frameContext = [location.href, document.referrer, ...(location.ancestorOrigins ? [...location.ancestorOrigins] : [])].join(" ");
    const contextHasTarget = targetJobId && new RegExp(`(?:^|[^0-9])${targetJobId}(?:[^0-9]|$)`).test(frameContext);
    if (targetJobId && currentJobId && targetJobId !== currentJobId) return false;
    // Greenhouse embedded job_app frames do not put the requisition id in
    // their own URL; accept them only when the referrer/ancestor context does.
    if (targetJobId && !currentJobId && !contextHasTarget) return false;

    const supportedHost = /(^|\.)greenhouse\.io$|(^|\.)lever\.co$|(^|\.)ashbyhq\.com$|(^|\.)myworkdayjobs\.com$|(^|\.)myworkday\.com$|(^|\.)myworkdaysite\.com$/i.test(here.hostname);
    return supportedHost;
  }

  function formSummary() {
    const elements = [...document.querySelectorAll("input, textarea, select, [role=combobox]")].filter(visible);
    const text = normalize(document.body?.innerText || "");
    const recognizable = ["first name", "last name", "email", "location (city)", "resume/cv"].filter((label) => text.includes(label)).length;
    return { count: elements.length, recognizable, found: elements.length > 0 && recognizable > 0 };
  }

  async function waitForApplicationForm(packet) {
    const started = Date.now();
    const applyButton = () => [...document.querySelectorAll("button, a")].find((el) => visible(el) && /^apply$/i.test((el.textContent || "").trim()));
    const check = async () => {
      const summary = formSummary();
      setBadge({ form: summary.found ? "found" : "waiting", fields: `${summary.count} / ?`, state: summary.found ? "FORM_FOUND" : "PAGE_CONNECTED" });
      if (summary.found) return summary;
      if (isTopFrame() && !state.applyClicked) {
        const button = applyButton();
        if (button) {
          state.applyClicked = true;
          await diagnostic("APPLY_CLICKED", { reason: "application form not mounted yet" }, packet);
          button.click();
        }
      }
      return null;
    };
    const immediate = await check();
    if (immediate) return immediate;
    return await new Promise((resolve) => {
      let finished = false;
      const finish = (value) => { if (finished) return; finished = true; observer.disconnect(); clearInterval(timer); resolve(value); };
      const observer = new MutationObserver(() => { check().then((value) => { if (value) finish(value); }); });
      observer.observe(document.documentElement, { childList: true, subtree: true, attributes: true });
      const timer = setInterval(() => {
        check().then((value) => { if (value) finish(value); else if (Date.now() - started >= 20000) finish(null); });
      }, 250);
      setTimeout(() => finish(null), 20500);
    });
  }

  async function fillGreenhouse() {
    const fields = [];
    const identity = state.packet.candidate_profile.identity || {};
    const contact = state.packet.candidate_profile.contact || {};
    const links = state.packet.candidate_profile.links || {};
    await fillText([/first.*name/], identity.first_name, fields);
    await fillText([/preferred.*first.*name/], answer("preferred_name") || identity.preferred_name, fields);
    await fillText([/last.*name/], identity.last_name, fields);
    await fillText([/e-?mail/], identity.email || contact.email, fields);
    await fillText([/^phone|\bphone\b/], identity.phone || contact.phone, fields);
    await choose([/^country/], answer("country") || identity.country || "United States", fields, "United States");
    const location = answer("candidate_location") || identity.candidate_location;
    if (location) await choose([/location.*city|candidate.*location/], location, fields, location);
    await uploadResume(fields);
    await fillText([/linkedin/], links.linkedin, fields);
    await fillText([/website|portfolio|personal.*site/], links.portfolio || links.website, fields);

    // Generic Greenhouse questions. These aliases are intentionally based on
    // the rendered label rather than employer-specific generated ids.
    const referral = answerAny(["referral_source", "application.referral_source"]);
    if (referral) await choose([/how did you hear|where did you learn|referral source/], referral, fields, referral);
    const authorized = answerAny(["work_preferences.work_authorization", "work_authorization"]);
    if (authorized !== undefined) await choose([/legally authorized to work|authorized to work in the u\.s\.?/], authorized === true || /^yes$/i.test(String(authorized)) ? "Yes" : "No", fields, authorized === true || /^yes$/i.test(String(authorized)) ? "Yes" : "No");
    const sponsorship = answerAny(["work_preferences.sponsorship_future", "sponsorship_future", "work_preferences.sponsorship_required"]);
    if (sponsorship !== undefined) await choose([/require .*sponsorship|sponsorship for employment visa|visa sponsorship/], sponsorship === true || /^yes$/i.test(String(sponsorship)) ? "Yes" : "No", fields, sponsorship === true || /^yes$/i.test(String(sponsorship)) ? "Yes" : "No");
    const graduationMonth = answerAny(["education.graduation_month"]);
    const graduationYear = answerAny(["education.graduation_year"]);
    if (graduationMonth && graduationYear) await fillText([/expected graduation|graduation date/], `${graduationMonth} ${graduationYear}`, fields);
    const major = answerAny(["education.major"]);
    if (major) await fillText([/field of study|major|discipline/], major, fields);
    await fillText([/favorite technology project|technology project and why/], answerAny(["favorite_technology_project"]), fields);
    recordRequiredIfPresent([/pursue a master|master.*after you graduate|graduate program after/], fields, answerAny(["education.plans_masters", "plans_masters"]), "plans to pursue a Master's program");
    recordRequiredIfPresent([/local to chicago|located in chicago|chicago for the summer/], fields, answerAny(["work_preferences.local_to_chicago"]), "local to Chicago for Summer 2027");
    await choose([/Astranis complies.*export regulations/], answer("export_control_eligibility.answer"), fields, answer("export_control_eligibility.answer"));
    await fillText([/most impressive.*accomplished/], answer("impressive_accomplishment"), fields);
    await choose([/confirm the season/], answer("season"), fields, answer("season"));
    await fillText([/able to join Astranis/], answer("start_availability"), fields);
    await fillText([/ending your internship/], answer("end_availability"), fields);
    await choose([/comfortable with working onsite/], answer("onsite_5_days_55_hours"), fields, answer("onsite_5_days_55_hours"));
    await fillText([/how did you hear about Astranis/], answer("referral_source"), fields);
    await choose([/consent to receive recruiting SMS/], answer("recruiting_sms_consent"), fields, answer("recruiting_sms_consent"));
    await handleVoluntaryEEO(fields);
    return fields;
  }

  function requiredErrors() {
    return [...document.querySelectorAll('[aria-required="true"], [required]')]
      .filter(visible)
      .filter((el) => !(voluntaryLabel(el) && el.getAttribute("aria-required") !== "true" && !el.required))
      .map((el) => nearbyError(el)).filter(Boolean);
  }

  async function report(result) {
    setBadge({ fields: `${result.verified_required_fields || 0} / ${result.required_field_count || "?"}`, state: result.status || "REPORTING" });
    await fetch(`${BRIDGE}/application/${encodeURIComponent(state.packet.job_id)}/result`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(result) });
  }

  async function run(packet) {
    if (state.running) return;
    if (!pageIdentity(packet)) {
      setBadge({ adapter: packet.ats_provider, internalJob: packet.internal_job_id || packet.job_id, providerJob: packet.provider_job_id, state: "PAGE_IDENTITY_REJECTED" });
      await diagnostic("PAGE_IDENTITY_REJECTED", { internal_job_id: packet.internal_job_id || packet.job_id, provider_job_id: packet.provider_job_id, company: packet.company, ats: packet.ats_provider }, packet);
      return;
    }
    state.running = true; state.packet = packet;
    setBadge({ adapter: packet.ats_provider, internalJob: packet.internal_job_id || packet.job_id, providerJob: packet.provider_job_id, state: "PAGE_CONNECTED" });
    const fields = [];
    try {
      await diagnostic("PAGE_IDENTITY_VERIFIED", { internal_job_id: packet.internal_job_id || packet.job_id, provider_job_id: packet.provider_job_id, company: packet.company, ats: packet.ats_provider }, packet);
      const form = await waitForApplicationForm(packet);
      if (!form) {
        setBadge({ form: "timeout", state: "FORM_NOT_FOUND" });
        await diagnostic("FORM_NOT_FOUND", { waited_ms: 20000 }, packet);
        await report({ status: "NEEDS_HUMAN", job_id: packet.job_id, current_url: location.href, reason: "FORM_NOT_FOUND after bounded readiness wait", unresolved_questions: [{ label: "Greenhouse application form", required: true, status: "unresolved" }] });
        return;
      }
      await diagnostic("FORM_FOUND", { controls: form.count, recognizable_labels: form.recognizable }, packet);
      setBadge({ form: "found", fields: `0 / ${form.count}`, state: "FILL_STARTED" });
      await diagnostic("FILL_STARTED", { controls: form.count }, packet);
      if (/greenhouse/i.test(packet.ats_provider) || /greenhouse\.io/.test(location.hostname)) fields.push(...await fillGreenhouse());
      else { await report({ status: "NEEDS_HUMAN", job_id: packet.job_id, reason: `ATS adapter not yet production-enabled: ${packet.ats_provider}` }); return; }
      await sleep(300);
      await diagnostic("VALIDATION_STARTED", { field_count: fields.length }, packet);
      const unresolved = fields.filter((f) => !f.verified && (f.required !== false || !f.voluntary));
      const errors = requiredErrors();
      const resume = fields.find((f) => f.label === "Resume/CV");
      const ready = unresolved.length === 0 && errors.length === 0 && resume?.verified === true && packet.duplicate !== true;
      const result = { status: ready ? (packet.submission_authorized ? "SUBMITTED" : "READY_TO_APPLY") : "NEEDS_INPUT", job_id: packet.job_id, current_url: location.href, fields, required_errors: errors, unresolved_questions: unresolved, resume_verified: resume?.verified === true, required_field_count: fields.filter((f) => f.required !== false && !f.voluntary).length, verified_required_fields: fields.filter((f) => f.required !== false && !f.voluntary && f.verified).length, optional_fields_skipped: fields.filter((f) => f.voluntary && f.required === false), voluntary_decline_selections: fields.filter((f) => f.voluntary && f.required && f.verified), duplicate: packet.duplicate === true, submission_authorized: packet.submission_authorized === true };
      if (ready && packet.submission_authorized) {
        const submit = [...document.querySelectorAll("button, input[type=submit]")].find((el) => /submit application/i.test(el.textContent || el.value || ""));
        if (!submit) { result.status = "NEEDS_HUMAN"; result.reason = "Submit control not found"; }
        else {
          submit.click(); await sleep(1500);
          const body = document.body.innerText.toLowerCase();
          const confirmation = /thank you for applying|application has been received|application submitted|successfully applied/.test(body);
          result.status = confirmation ? "SUBMITTED" : "NEEDS_HUMAN";
          result.confirmation_text = confirmation ? body.match(/thank you[^\n]*/i)?.[0] || "confirmation detected" : "submission confirmation not verified";
          if (confirmation) {
            const captured = await new Promise((resolve) => chrome.runtime.sendMessage({ type: "JOBBEAST_CAPTURE_SCREENSHOT" }, resolve));
            if (captured?.dataUrl) result.screenshot_data = captured.dataUrl;
          }
        }
      }
      await report(result); state.done = result.status === "SUBMITTED";
      await diagnostic(result.status === "SUBMITTED" ? "CONFIRMATION_FOUND" : "VALIDATION_RESULT", { status: result.status, field_count: fields.length, required_errors: errors.length, unresolved: unresolved.length }, packet);
    } catch (error) {
      setBadge({ state: "FAILED", fields: `${fields.filter((field) => field.verified).length} / ${fields.length || "?"}` });
      await diagnostic("EXECUTOR_ERROR", { error: String(error), field_count: fields.length }, packet);
      await report({ status: "NEEDS_HUMAN", job_id: packet.job_id, current_url: location.href, reason: String(error), fields });
    }
  }

  async function poll() {
    setBadge({ script: "loaded", state: "BRIDGE_CONNECTING" });
    try {
      const response = await fetch(`${BRIDGE}/application/current`, { cache: "no-store" });
      if (response.ok) {
        const packet = await response.json();
        setBadge({ bridge: "connected", adapter: packet.ats_provider, internalJob: packet.internal_job_id || packet.job_id, providerJob: packet.provider_job_id, state: "PACKET_RECEIVED" });
        await diagnostic("CONTENT_SCRIPT_STARTED");
        await diagnostic("BRIDGE_CONNECTED", { internal_job_id: packet.internal_job_id || packet.job_id, provider_job_id: packet.provider_job_id, company: packet.company, ats: packet.ats_provider }, packet);
        await diagnostic("PACKET_RECEIVED", { internal_job_id: packet.internal_job_id || packet.job_id, provider_job_id: packet.provider_job_id, company: packet.company, ats: packet.ats_provider }, packet);
        const key = `${packet.job_id}:${packet.submission_authorized === true}:${packet.dry_run === true}`;
        if (key !== state.packetKey) {
          state.packetKey = key; state.packet = packet; state.running = false; state.done = false;
        }
        await run(packet);
      }
    } catch (_) {
      setBadge({ bridge: "connection failed", state: "BRIDGE_CONNECTION_FAILED" });
      await diagnostic("BRIDGE_CONNECTION_FAILED");
    }
    if (!state.done) setTimeout(poll, 1000);
  }
  poll();
})();
