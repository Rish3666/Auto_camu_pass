/**
 * Content Script injected into https://student.camu.in/*
 * 1. Automatically syncs student profile details whenever Camu is open & logged in.
 * 2. Handles login (if needed) and fills/submits the Gate Pass form.
 */

(() => {
  if (globalThis.__autoCamuLoaded) return;
  globalThis.__autoCamuLoaded = true;

  const SELECTORS = {
    usernameInput: 'input[type="text"], input[name="Email"], input[name="username"], input[placeholder*="User" i], input[placeholder*="Email" i]',
    passwordInput: 'input[type="password"]',
    gatepassNavTexts: ['Gate Pass', 'Outpass', 'Gatepass'],
    applyButtonTexts: ['Apply', 'New', 'Add', 'Request'],
    passTypeSelect: 'select[name*="type" i], [formcontrolname*="type" i], mat-select',
    reasonInput: 'textarea, input[name*="reason" i], input[placeholder*="Reason" i]',
    outTimeInput: 'input[name*="out" i], input[placeholder*="Out" i]',
    inTimeInput: 'input[name*="in" i], input[placeholder*="In" i]',
    studentNameInput: 'input[name*="name" i], input[placeholder*="Student Name" i]',
    studentIdInput: 'input[name*="roll" i], input[name*="reg" i], input[name*="stuId" i], input[placeholder*="Roll" i], input[placeholder*="ID" i]',
    phoneInput: 'input[name*="mobile" i], input[name*="phone" i], input[placeholder*="Phone" i], input[placeholder*="Mobile" i]',
    parentPhoneInput: 'input[name*="parent" i], input[name*="emergency" i], input[placeholder*="Parent" i]',
    submitButtonTexts: ['Submit', 'Save', 'Apply'],
  };

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  function isLoginScreenVisible() {
    const pw = document.querySelector(SELECTORS.passwordInput);
    return Boolean(pw && pw.offsetParent !== null);
  }

  function setInputValue(el, value) {
    if (!el || !value) return;
    el.focus();
    const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
    if (setter) {
      setter.call(el, value);
    } else {
      el.value = value;
    }
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  }

  function findClickableByText(texts) {
    const candidates = Array.from(document.querySelectorAll('a, button, [role="button"], li, span'));
    for (const target of texts) {
      const lower = target.toLowerCase();
      const match = candidates.find((el) => {
        const txt = (el.innerText || el.textContent || '').trim().toLowerCase();
        return txt === lower || txt.includes(lower);
      });
      if (match) return match;
    }
    return null;
  }

  async function waitForSelector(selector, timeoutMs = 10000) {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      const el = document.querySelector(selector);
      if (el && el.offsetParent !== null) return el;
      await sleep(250);
    }
    return null;
  }

  function walkObjects(root, visitor, depth = 0, seen = new Set()) {
    if (!root || typeof root !== 'object' || depth > 5 || seen.has(root)) return;
    seen.add(root);
    if (Array.isArray(root)) {
      for (const item of root) walkObjects(item, visitor, depth + 1, seen);
      return;
    }
    visitor(root);
    for (const val of Object.values(root)) {
      if (val && typeof val === 'object') walkObjects(val, visitor, depth + 1, seen);
    }
  }

  function parseStorageValues(storage) {
    const values = [];
    for (let i = 0; i < storage.length; i++) {
      const key = storage.key(i);
      const raw = storage.getItem(key);
      if (!raw) continue;
      if (raw.startsWith('{') || raw.startsWith('[')) {
        try {
          values.push(JSON.parse(raw));
        } catch (_) {}
      }
    }
    return values;
  }

  /**
   * Extract student details recursively from Camu's storage + visible profile DOM.
   */
  function extractStudentDetailsFromSession() {
    const details = {
      name: '',
      studentId: '',
      rollNo: '',
      email: '',
      phone: '',
      parentPhone: '',
      institutionId: '',
      syncedAt: new Date().toISOString(),
    };

    const pick = (obj, keys) => {
      for (const k of keys) {
        const v = obj[k];
        if (typeof v === 'string' && v.trim()) return v.trim();
        if (typeof v === 'number') return String(v);
      }
      return '';
    };

    const roots = [
      ...parseStorageValues(window.localStorage),
      ...parseStorageValues(window.sessionStorage),
    ];

    for (const root of roots) {
      walkObjects(root, (obj) => {
        details.name ||= pick(obj, ['FNa', 'StuNa', 'studentName', 'fullName', 'Name', 'name']);
        details.studentId ||= pick(obj, ['StuID', 'stuId', 'studentId', '_id']);
        details.rollNo ||= pick(obj, ['RollNo', 'rollNo', 'RegNo', 'regNo', 'AdmNo', 'AdmnNo']);
        details.email ||= pick(obj, ['Email', 'email', 'StuEmail', 'EmailId']);
        details.phone ||= pick(obj, ['MoNo', 'mobile', 'phone', 'Phone', 'ContactNo', 'MobileNo']);
        details.parentPhone ||= pick(obj, ['FaMoNo', 'MoMoNo', 'parentPhone', 'EmergencyNo']);
        details.institutionId ||= pick(obj, ['InId', 'inId', 'institutionId']);
      });
    }

    // Scrape visible profile DOM text (e.g. /v2/profile cards/tables)
    const bodyText = document.body?.innerText || '';
    if (!details.email) {
      const emailMatch = bodyText.match(/[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}/);
      if (emailMatch) details.email = emailMatch[0];
    }
    if (!details.phone) {
      const phoneMatch = bodyText.match(/\b[6-9]\d{9}\b/);
      if (phoneMatch) details.phone = phoneMatch[0];
    }
    if (!details.rollNo) {
      const rollMatch = bodyText.match(/(?:Roll\s*No|Reg(?:ister)?\s*No|Admission\s*No)[:\s]+([A-Z0-9-]{4,20})/i);
      if (rollMatch) details.rollNo = rollMatch[1].trim();
    }
    if (!details.name) {
      const heading = document.querySelector('h1, h2, h3, .profile-name, [class*="student-name" i]');
      const txt = heading?.innerText?.trim() || '';
      if (txt && txt.length >= 2 && txt.length <= 60 && !/camu|profile|login/i.test(txt)) {
        details.name = txt;
      }
    }

    return details;
  }

  function hasUsefulProfile(profile) {
    return Boolean(profile && (profile.name || profile.rollNo || profile.studentId || profile.email || profile.phone));
  }

  async function syncProfileWithRetry() {
    if (isLoginScreenVisible()) return null;

    for (let attempt = 0; attempt < 6; attempt++) {
      const profile = extractStudentDetailsFromSession();
      if (hasUsefulProfile(profile)) {
        try {
          await chrome.runtime.sendMessage({ action: 'PROFILE_SYNCED', profile });
        } catch (_) {}
        return profile;
      }
      await sleep(1500);
    }
    return null;
  }

  async function handleSubmission(payload) {
    const { username, password, passType, reason, outTime, inTime, dateStr, dryRun } = payload;

    // 1. Login if password field is visible
    if (isLoginScreenVisible()) {
      const userField = document.querySelector(SELECTORS.usernameInput);
      const pwField = document.querySelector(SELECTORS.passwordInput);
      if (!userField || !pwField) {
        return { ok: false, message: 'Login page detected, but input fields were not found.' };
      }
      if (!username || !password) {
        return { ok: false, message: 'Camu is logged out. Save credentials or log in on Camu first.' };
      }
      setInputValue(userField, username);
      setInputValue(pwField, password);

      const loginBtn = findClickableByText(['Login', 'Sign In', 'Submit']) || document.querySelector('button[type="submit"]');
      if (!loginBtn) {
        return { ok: false, message: 'Could not find Login button.' };
      }
      loginBtn.click();
      await sleep(3500);

      if (isLoginScreenVisible()) {
        return { ok: false, message: 'Login failed (check credentials or OTP/CAPTCHA).' };
      }
    }

    // 2. Fetch required student details from the logged-in Camu session/profile
    const profile = (await syncProfileWithRetry()) || extractStudentDetailsFromSession();

    // 3. Navigate to Gate Pass section
    let navEl = findClickableByText(SELECTORS.gatepassNavTexts);
    if (!navEl) {
      await sleep(2000);
      navEl = findClickableByText(SELECTORS.gatepassNavTexts);
    }
    if (!navEl) {
      return {
        ok: false,
        profile,
        message: 'Logged in, but could not find Gate Pass / Outpass menu item on Camu.',
      };
    }
    navEl.click();
    await sleep(2000);

    // 4. Open Apply form if there is an Apply/New button
    const applyBtn = findClickableByText(SELECTORS.applyButtonTexts);
    if (applyBtn) {
      applyBtn.click();
      await sleep(1200);
    }

    // 5. Auto-fill student details fetched from login if those fields exist on the form (and are empty)
    const nameEl = document.querySelector(SELECTORS.studentNameInput);
    if (nameEl && !nameEl.value && profile.name) setInputValue(nameEl, profile.name);

    const idEl = document.querySelector(SELECTORS.studentIdInput);
    if (idEl && !idEl.value && (profile.rollNo || profile.studentId)) {
      setInputValue(idEl, profile.rollNo || profile.studentId);
    }

    const phoneEl = document.querySelector(SELECTORS.phoneInput);
    if (phoneEl && !phoneEl.value && profile.phone) setInputValue(phoneEl, profile.phone);

    const parentPhoneEl = document.querySelector(SELECTORS.parentPhoneInput);
    if (parentPhoneEl && !parentPhoneEl.value && profile.parentPhone) {
      setInputValue(parentPhoneEl, profile.parentPhone);
    }

    // 6. Fill Gate Pass fields
    const selectEl = document.querySelector(SELECTORS.passTypeSelect);
    if (selectEl && selectEl.tagName === 'SELECT') {
      const opt = Array.from(selectEl.options).find(
        (o) => o.text.trim().toLowerCase().includes(passType.toLowerCase())
      );
      if (opt) {
        selectEl.value = opt.value;
        selectEl.dispatchEvent(new Event('change', { bubbles: true }));
      }
    }

    const reasonEl = await waitForSelector(SELECTORS.reasonInput, 4000);
    if (reasonEl) setInputValue(reasonEl, reason);

    const outEl = document.querySelector(SELECTORS.outTimeInput);
    if (outEl) setInputValue(outEl, `${dateStr} ${outTime}`);

    const inEl = document.querySelector(SELECTORS.inTimeInput);
    if (inEl) setInputValue(inEl, `${dateStr} ${inTime}`);

    if (dryRun) {
      return {
        ok: true,
        profile,
        message: `Dry-run filled form for ${dateStr} (${outTime}–${inTime}). Review the opened Camu tab.`,
      };
    }

    // 7. Submit
    const submitBtn = document.querySelector('button[type="submit"]') || findClickableByText(SELECTORS.submitButtonTexts);
    if (!submitBtn) {
      return { ok: false, profile, message: 'Form filled, but Submit button was not found.' };
    }
    submitBtn.click();
    await sleep(2500);

    return {
      ok: true,
      profile,
      message: `Gate pass submitted for ${dateStr} (${outTime}–${inTime}).`,
    };
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message.action === 'SYNC_PROFILE') {
      (async () => {
        if (isLoginScreenVisible()) {
          sendResponse({ ok: false, needsLogin: true, message: 'Please log in to Camu in this tab.' });
          return;
        }
        const profile = await syncProfileWithRetry();
        if (profile) {
          sendResponse({ ok: true, profile, message: 'Profile synced from Camu.' });
        } else {
          sendResponse({ ok: false, message: 'Could not detect student details on this page yet.' });
        }
      })();
      return true;
    }

    if (message.action === 'SUBMIT_GATEPASS') {
      (async () => {
        try {
          const res = await handleSubmission(message.payload);
          sendResponse(res);
        } catch (err) {
          sendResponse({ ok: false, message: err?.message || String(err) });
        }
      })();
      return true;
    }
  });

  // Automatically sync whenever the user opens Camu while logged in
  syncProfileWithRetry();
})();
