/**
 * Content Script injected into https://student.camu.in/*
 * Handles login (if needed) and filling/submitting the Gate Pass form.
 */

(() => {
  if (globalThis.__autoCamuLoaded) return;
  globalThis.__autoCamuLoaded = true;

  // Centralized DOM selectors for student.camu.in.
  // Update these if your institution's Camu portal uses different attributes or labels.
  const SELECTORS = {
    usernameInput: 'input[type="text"], input[name="Email"], input[name="username"], input[placeholder*="User" i], input[placeholder*="Email" i]',
    passwordInput: 'input[type="password"]',
    loginButton: 'button[type="submit"], button:has-text("Login"), button[type="button"]',
    gatepassNavTexts: ['Gate Pass', 'Outpass', 'Gatepass'],
    applyButtonTexts: ['Apply', 'New', 'Add', 'Request'],
    passTypeSelect: 'select[name*="type" i], [formcontrolname*="type" i], mat-select',
    reasonInput: 'textarea, input[name*="reason" i], input[placeholder*="Reason" i]',
    outTimeInput: 'input[name*="out" i], input[placeholder*="Out" i]',
    inTimeInput: 'input[name*="in" i], input[placeholder*="In" i]',
    submitButtonTexts: ['Submit', 'Save', 'Apply'],
  };

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  function setInputValue(el, value) {
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

    // 2. Navigate to Gate Pass section
    let navEl = findClickableByText(SELECTORS.gatepassNavTexts);
    if (!navEl) {
      await sleep(2000);
      navEl = findClickableByText(SELECTORS.gatepassNavTexts);
    }
    if (!navEl) {
      return { ok: false, message: 'Could not find Gate Pass / Outpass menu item on Camu.' };
    }
    navEl.click();
    await sleep(2000);

    // 3. Open Apply form if there is an Apply/New button
    const applyBtn = findClickableByText(SELECTORS.applyButtonTexts);
    if (applyBtn) {
      applyBtn.click();
      await sleep(1200);
    }

    // 4. Fill form fields
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
        message: `Dry-run filled form for ${dateStr} (${outTime}–${inTime}). Review the opened Camu tab.`,
      };
    }

    // 5. Submit
    const submitBtn = document.querySelector('button[type="submit"]') || findClickableByText(SELECTORS.submitButtonTexts);
    if (!submitBtn) {
      return { ok: false, message: 'Form filled, but Submit button was not found.' };
    }
    submitBtn.click();
    await sleep(2500);

    return {
      ok: true,
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
