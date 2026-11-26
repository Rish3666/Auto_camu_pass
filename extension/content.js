/**
 * Content Script injected into https://student.camu.in/*
 * Handles login (if needed), extracts student details from the logged-in session/profile,
 * and fills/submits the Gate Pass form.
 */

(() => {
  if (globalThis.__autoCamuLoaded) return;
  globalThis.__autoCamuLoaded = true;

  // Centralized DOM selectors for student.camu.in.
  // Update these if your institution's Camu portal uses different attributes or labels.
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

  function parseStorageObjects(storage) {
    const parsed = [];
    for (let i = 0; i < storage.length; i++) {
      const key = storage.key(i);
      const raw = storage.getItem(key);
      if (!raw || (!raw.startsWith('{') && !raw.startsWith('['))) continue;
      try {
        parsed.push({ key, data: JSON.parse(raw) });
      } catch (_) {}
    }
    return parsed;
  }

  /**
   * Extract student details (name, studentId, rollNo, email, phone, parentPhone, etc.)
   * directly from the logged-in Camu session (localStorage/sessionStorage + profile DOM).
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
    };

    const allStored = [
      ...parseStorageObjects(window.localStorage),
      ...parseStorageObjects(window.sessionStorage),
    ];

    const pick = (obj, keys) => {
      if (!obj || typeof obj !== 'object') return '';
      for (const k of keys) {
        if (obj[k] && typeof obj[k] === 'string') return obj[k].trim();
        if (obj[k] && typeof obj[k] === 'number') return String(obj[k]);
      }
      return '';
    };

    for (const { data } of allStored) {
      const candidates = Array.isArray(data) ? data : [data, data?.data, data?.user, data?.student, data?.profile];
      for (const obj of candidates) {
        if (!obj || typeof obj !== 'object') continue;
        details.name ||= pick(obj, ['FNa', 'Name', 'name', 'StuNa', 'studentName', 'fullName']);
        details.studentId ||= pick(obj, ['StuID', 'stuId', 'studentId', '_id', 'id']);
        details.rollNo ||= pick(obj, ['RollNo', 'rollNo', 'RegNo', 'regNo', 'AdmNo']);
        details.email ||= pick(obj, ['Email', 'email', 'StuEmail']);
        details.phone ||= pick(obj, ['MoNo', 'mobile', 'phone', 'Phone', 'ContactNo']);
        details.parentPhone ||= pick(obj, ['FaMoNo', 'MoMoNo', 'parentPhone', 'EmergencyNo']);
        details.institutionId ||= pick(obj, ['InId', 'inId', 'institutionId']);
      }
    }

    // Fallback: scrape visible label/value pairs on /v2/profile DOM
    const bodyText = document.body?.innerText || '';
    if (!details.email) {
      const emailMatch = bodyText.match(/[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}/);
      if (emailMatch) details.email = emailMatch[0];
    }
    if (!details.phone) {
      const phoneMatch = bodyText.match(/\b[6-9]\d{9}\b/);
      if (phoneMatch) details.phone = phoneMatch[0];
    }

    return details;
  }

  async function handleSubmission(payload) {
    const { username, password, passType, reason, outTime, inTime, dateStr, dryRun } = payload;

    // 1. Login if password field is visible
    const pwField = document.querySelector(SELECTORS.passwordInput);
    if (pwField && pwField.offsetParent !== null) {
      const userField = document.querySelector(SELECTORS.usernameInput);
      if (!userField) {
        return { ok: false, message: 'Login page detected, but username field not found.' };
      }
      setInputValue(userField, username);
      setInputValue(pwField, password);

      const loginBtn = findClickableByText(['Login', 'Sign In', 'Submit']) || document.querySelector('button[type="submit"]');
      if (!loginBtn) {
        return { ok: false, message: 'Could not find Login button.' };
      }
      loginBtn.click();
      await sleep(3500);

      const stillOnLogin = document.querySelector(SELECTORS.passwordInput);
      if (stillOnLogin && stillOnLogin.offsetParent !== null) {
        return { ok: false, message: 'Login failed (check credentials or OTP/CAPTCHA).' };
      }
    }

    // 2. Fetch required student details from the logged-in Camu session/profile
    await sleep(1000);
    const profile = extractStudentDetailsFromSession();

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
})();
