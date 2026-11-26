/**
 * Content Script injected into https://student.camu.in/*
 * Automatically extracts student details (StuID, InId, loggedInId, PerNm)
 * from the logged-in Camu session so background.js can submit via silent fetch().
 */

(() => {
  if (globalThis.__autoCamuLoaded) return;
  globalThis.__autoCamuLoaded = true;

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  function isLoginScreenVisible() {
    const pw = document.querySelector('input[type="password"]');
    return Boolean(pw && pw.offsetParent !== null);
  }

  function walkObjects(root, visitor, depth = 0, seen = new Set()) {
    if (!root || typeof root !== 'object' || depth > 6 || seen.has(root)) return;
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

  function extractStudentDetailsFromSession() {
    const details = {
      name: '',
      studentId: '',
      loggedInId: '',
      institutionId: '',
      rollNo: '',
      email: '',
      phone: '',
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
        details.name ||= pick(obj, ['PerNm', 'FNa', 'StuNa', 'studentName', 'fullName', 'Name', 'name']);
        details.studentId ||= pick(obj, ['StuID', 'stuId', 'PerId', 'studentId']);
        details.loggedInId ||= pick(obj, ['loggedInId', 'userId', 'usrId']);
        details.institutionId ||= pick(obj, ['InId', 'inId', 'institutionId']);
        details.rollNo ||= pick(obj, ['RollNo', 'rollNo', 'RegNo', 'regNo', 'AdmNo', 'AdmnNo']);
        details.email ||= pick(obj, ['Email', 'email', 'StuEmail', 'EmailId']);
        details.phone ||= pick(obj, ['MoNo', 'mobile', 'phone', 'Phone', 'ContactNo', 'MobileNo']);
      });
    }

    // Fallback: if loggedInId wasn't explicitly named, check top-level _id on user objects
    if (!details.loggedInId) {
      for (const root of roots) {
        walkObjects(root, (obj) => {
          if (!details.loggedInId && typeof obj._id === 'string' && obj._id !== details.studentId && obj._id.length === 24) {
            details.loggedInId = obj._id;
          }
        });
      }
    }

    // Scrape visible profile DOM text as fallback
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

  function hasUsefulProfile(profile) {
    return Boolean(profile && (profile.studentId || profile.name || profile.email));
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
  });

  // Automatically sync whenever Camu is open and logged in
  syncProfileWithRetry();
})();
