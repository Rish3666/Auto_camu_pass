/**
 * Background Service Worker for Auto Camu Pass (Manifest V3).
 * Uses silent fetch() against Camu's REST APIs (no tabs opened).
 */

const ALARM_NAME = 'camu-schedule-check';
const CAMU_BASE = 'https://student.camu.in';
const CAMU_PROFILE_URL = `${CAMU_BASE}/v2/profile`;

function getLocalDateString(now = new Date()) {
  const year = now.getFullYear();
  const month = String(now.getMonth() + 1).padStart(2, '0');
  const day = String(now.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

function isAllowedWindow(now = new Date()) {
  // JS getDay(): 4 = Thursday, 5 = Friday
  const day = now.getDay();
  const hour = now.getHours();
  return (day === 4 || day === 5) && hour >= 9 && hour < 23;
}

async function ensureAlarm() {
  const existing = await chrome.alarms.get(ALARM_NAME);
  if (!existing) {
    await chrome.alarms.create(ALARM_NAME, { periodInMinutes: 15 });
  }
}

async function showNotification(title, message) {
  try {
    await chrome.notifications.create({
      type: 'basic',
      title,
      message,
    });
  } catch (err) {
    console.warn('Notification error:', err);
  }
}

async function waitForTabComplete(tabId, timeoutMs = 25000) {
  const tab = await chrome.tabs.get(tabId);
  if (tab.status === 'complete') return;

  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      reject(new Error('Timed out waiting for Camu tab to load.'));
    }, timeoutMs);

    function listener(updatedTabId, changeInfo) {
      if (updatedTabId === tabId && changeInfo.status === 'complete') {
        clearTimeout(timer);
        chrome.tabs.onUpdated.removeListener(listener);
        resolve();
      }
    }

    chrome.tabs.onUpdated.addListener(listener);
  });
}

async function openOrFocusCamuAndSync() {
  const existingTabs = await chrome.tabs.query({ url: `${CAMU_BASE}/*` });
  let tab;
  if (existingTabs.length > 0) {
    tab = existingTabs[0];
    await chrome.tabs.update(tab.id, { active: true });
    await chrome.windows.update(tab.windowId, { focused: true });
  } else {
    tab = await chrome.tabs.create({ url: CAMU_PROFILE_URL, active: true });
  }

  await waitForTabComplete(tab.id);
  await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    files: ['content.js'],
  });

  try {
    const res = await chrome.tabs.sendMessage(tab.id, { action: 'SYNC_PROFILE' });
    if (res?.profile) {
      await chrome.storage.local.set({ fetchedProfile: res.profile });
    }
    return res;
  } catch (err) {
    return { ok: false, message: err?.message || 'Camu tab opened. Log in to sync your profile.' };
  }
}

async function camuPost(path, body) {
  const init = {
    method: 'POST',
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'application/json',
    },
  };
  if (body !== null && body !== undefined) {
    init.body = JSON.stringify(body);
  }
  const resp = await fetch(`${CAMU_BASE}${path}`, init);
  const text = await resp.text();
  let json = null;
  try {
    json = JSON.parse(text);
  } catch (_) {}

  if (!resp.ok) {
    throw new Error(`HTTP ${resp.status} on ${path}: ${text.slice(0, 120)}`);
  }
  return json ?? text;
}

function findFirstArrayOfObjects(root, depth = 0) {
  if (!root || typeof root !== 'object' || depth > 5) return [];
  if (Array.isArray(root)) {
    return root.length > 0 && typeof root[0] === 'object' ? root : [];
  }
  for (const val of Object.values(root)) {
    const found = findFirstArrayOfObjects(val, depth + 1);
    if (found.length > 0) return found;
  }
  return [];
}

function resolvePassType(typesList, desiredLabel) {
  if (!typesList.length) return { id: '', label: '', allLabels: [] };

  const getLabel = (item) =>
    String(
      item.Nm || item.name || item.PassNm || item.passNm || item.label || item.title || item.PassTy || item.passTy || item.type || ''
    ).trim();

  const getId = (item) =>
    String(
      item._id || item.id || item.passTy || item.PassTy || item.code || item.Cd || item.value || getLabel(item) || ''
    ).trim();

  const allLabels = typesList.map(getLabel).filter(Boolean);
  const target = (desiredLabel || '').trim().toLowerCase();
  const match = typesList.find((item) => {
    const lbl = getLabel(item).toLowerCase();
    return target && lbl && (lbl === target || lbl.includes(target) || target.includes(lbl));
  });
  const chosen = match || typesList[0];
  return {
    id: getId(chosen),
    label: getLabel(chosen),
    allLabels,
  };
}

function buildUtcIso(dateStr, timeStr) {
  const dt = new Date(`${dateStr}T${timeStr || '10:00'}:00`);
  return Number.isNaN(dt.getTime()) ? `${dateStr}T04:30:00.000Z` : dt.toISOString();
}

async function runGatePassSubmission({ dryRun = false, manual = false } = {}) {
  const config = await chrome.storage.local.get([
    'enabled',
    'passType',
    'reason',
    'outTime',
    'inTime',
    'lastSubmittedDate',
    'fetchedProfile',
  ]);

  if (!manual && !config.enabled) {
    return { ok: false, skipped: true, message: 'Auto-submit is paused.' };
  }

  const profile = config.fetchedProfile;
  if (!profile?.studentId) {
    const msg = 'Camu profile not synced yet. Click "Open Camu" once while logged in.';
    await chrome.storage.local.set({ lastStatus: msg });
    return { ok: false, message: msg };
  }

  const todayStr = getLocalDateString();
  if (!manual && !dryRun && config.lastSubmittedDate === todayStr) {
    return { ok: true, skipped: true, message: `Already submitted today (${todayStr}).` };
  }

  try {
    // 1. Verify active Camu session & check for existing passes today
    const leavesRes = await camuPost('/api/Leave/getAllleave/', {
      StuID: profile.studentId,
      isFrmReact: true,
    });
    const existingLeaves = findFirstArrayOfObjects(leavesRes);
    const alreadyExists = existingLeaves.some((lv) =>
      String(lv.FrDt || lv.frdt || '').startsWith(todayStr)
    );
    if (alreadyExists && !dryRun) {
      const existsMsg = `Pass already exists in Camu for ${todayStr} (skipped duplicate).`;
      await chrome.storage.local.set({
        lastSubmittedDate: todayStr,
        lastStatus: existsMsg,
      });
      return { ok: true, skipped: true, message: existsMsg };
    }

    // 2. Fetch pass types and resolve passTy ID
    const typesRes = await camuPost('/api/gatepasses/fetch-gatepasses-types', null);
    const typesList = findFirstArrayOfObjects(typesRes);
    const resolvedType = resolvePassType(typesList, config.passType || 'Day Outing');

    if (!resolvedType.id) {
      const rawPreview = JSON.stringify(typesRes).slice(0, 140);
      throw new Error(`PASSTY_NOT_FOUND in fetch-gatepasses-types: ${rawPreview}`);
    }

    const outTime = config.outTime || '10:00';
    const inTime = config.inTime || '20:00';

    const payload = {
      Attachments: [],
      FrDt: `${todayStr}T00:00:00.000Z`,
      GateOutPass: true,
      HstleReq: 'true',
      InId: profile.institutionId || '',
      LvTy: 'Leave',
      PerId: profile.studentId,
      PerNm: profile.name || '',
      PerTy: 'Student',
      PtAb: '',
      Rson: '',
      Sson: '',
      ToDt: `${todayStr}T00:00:00.000Z`,
      atcmnt: [],
      frdt: buildUtcIso(todayStr, outTime),
      loggedInId: profile.loggedInId || profile.studentId,
      passTy: resolvedType.id,
      periods: [],
      rson: config.reason || 'Outing',
      timezoneOffSet: 330,
      todt: buildUtcIso(todayStr, inTime),
    };

    if (dryRun) {
      const typesNote = resolvedType.allLabels.length
        ? `types=[${resolvedType.allLabels.join(', ')}]`
        : `passTy="${resolvedType.id}"`;
      const dupNote = alreadyExists ? ' (note: pass already exists today)' : '';
      const msg = `Dry-run OK: using "${resolvedType.label || resolvedType.id}" (${typesNote})${dupNote}.`;
      await chrome.storage.local.set({
        lastStatus: msg,
        lastRunAt: new Date().toISOString(),
      });
      return { ok: true, message: msg, payloadPreview: payload };
    }

    // 3. Silent POST to /api/Leave/crtLv
    await camuPost('/api/Leave/crtLv', payload);

    const okMsg = `Submitted silently (${todayStr} ${outTime}–${inTime})`;
    await chrome.storage.local.set({
      lastSubmittedDate: todayStr,
      lastStatus: okMsg,
      lastRunAt: new Date().toISOString(),
    });
    await showNotification('Auto Camu Pass', okMsg);
    return { ok: true, message: okMsg };
  } catch (err) {
    const msg = err?.message || String(err);
    const friendly = /401|403|HTML|login/i.test(msg)
      ? 'Camu session expired. Click "Open Camu" to log in again.'
      : msg;
    await chrome.storage.local.set({
      lastStatus: `Failed: ${friendly}`,
      lastRunAt: new Date().toISOString(),
    });
    await showNotification('Auto Camu Pass — Failed', friendly);
    return { ok: false, message: friendly };
  }
}

async function checkScheduleAndRun() {
  if (!isAllowedWindow()) return;
  await runGatePassSubmission({ dryRun: false, manual: false });
}

chrome.runtime.onInstalled.addListener(async () => {
  const { enabled } = await chrome.storage.local.get('enabled');
  if (enabled === undefined) {
    await chrome.storage.local.set({ enabled: true });
  }
  await ensureAlarm();
  await checkScheduleAndRun();
});

chrome.runtime.onStartup.addListener(async () => {
  await ensureAlarm();
  await checkScheduleAndRun();
});

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === ALARM_NAME) {
    await checkScheduleAndRun();
  }
});

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  (async () => {
    if (message.action === 'PROFILE_SYNCED' && message.profile) {
      await chrome.storage.local.set({ fetchedProfile: message.profile });
      sendResponse({ ok: true });
      return;
    }

    if (message.action === 'OPEN_CAMU_AND_SYNC') {
      const result = await openOrFocusCamuAndSync();
      sendResponse(result);
      return;
    }

    if (message.action === 'TOGGLE_SCRIPT') {
      const enabled = Boolean(message.enabled);
      await chrome.storage.local.set({ enabled });
      if (enabled) {
        await ensureAlarm();
        await checkScheduleAndRun();
        sendResponse({
          ok: true,
          message: 'Script ON: checks on browser launch & every 15m on Thu & Fri.',
        });
      } else {
        sendResponse({ ok: true, message: 'Script OFF: Thu & Fri auto-submit paused.' });
      }
      return;
    }

    if (message.action === 'RUN_NOW') {
      const result = await runGatePassSubmission({
        dryRun: Boolean(message.dryRun),
        manual: true,
      });
      sendResponse(result);
      return;
    }

    sendResponse({ ok: false, message: 'Unknown action.' });
  })();
  return true;
});
