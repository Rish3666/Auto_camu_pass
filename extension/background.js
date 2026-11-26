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

function getWeekendDates(now = new Date()) {
  const day = now.getDay(); // 0=Sun..6=Sat
  const daysUntilFriday = day === 5 ? 0 : (5 - day + 7) % 7;
  const friday = new Date(now.getFullYear(), now.getMonth(), now.getDate() + daysUntilFriday);
  const sunday = new Date(friday.getFullYear(), friday.getMonth(), friday.getDate() + 2);
  return {
    fridayDateStr: getLocalDateString(friday),
    sundayDateStr: getLocalDateString(sunday),
  };
}

function isAllowedWindow(now = new Date()) {
  // JS getDay(): 4 = Thursday, 5 = Friday
  const day = now.getDay();
  const hour = now.getHours();
  return (day === 4 || day === 5) && hour >= 9 && hour < 23;
}

async function updateActionBadge(submittedToday) {
  try {
    await chrome.action.setBadgeText({ text: submittedToday ? 'OK' : '' });
    await chrome.action.setBadgeBackgroundColor({
      color: submittedToday ? '#16a34a' : '#52525b',
    });
  } catch (err) {
    console.warn('Badge error:', err);
  }
}

function isCancelledOrRejected(leaveObj) {
  if (!leaveObj || typeof leaveObj !== 'object') return false;
  if (leaveObj.isCncl === true || leaveObj.isCancelled === true || leaveObj.isDeleted === true) {
    return true;
  }
  const stFl = String(leaveObj.StFl || leaveObj.stFl || '').trim().toUpperCase();
  if (stFl && stFl !== 'A') {
    return true;
  }
  for (const [key, val] of Object.entries(leaveObj)) {
    if (/^(rson|reason|pernm|name|email|id|_id)$/i.test(key)) continue;
    if (typeof val === 'string' && /cancel|reject|declin|delet|withdraw|expir/i.test(val)) {
      return true;
    }
  }
  return false;
}

function isGatePassEntry(leaveObj) {
  if (!leaveObj) return false;
  if (leaveObj.GateOutPass === false || leaveObj.GateOutPass === 'false') return false;
  if (isCancelledOrRejected(leaveObj)) return false;
  return Boolean(
    leaveObj.GateOutPass === true ||
      leaveObj.GateOutPass === 'true' ||
      leaveObj.passTy ||
      leaveObj.PassTy ||
      leaveObj.passCd
  );
}

function isLeaveForDate(leaveObj, targetDateStr) {
  for (const raw of [leaveObj?.FrDt, leaveObj?.frdt]) {
    if (!raw) continue;
    const str = String(raw);
    if (str.startsWith(targetDateStr)) return true;
    const parsed = new Date(str);
    if (!Number.isNaN(parsed.getTime()) && getLocalDateString(parsed) === targetDateStr) {
      return true;
    }
  }
  return false;
}

async function checkCamuSubmittedToday() {
  const { fridayDateStr, sundayDateStr } = getWeekendDates();
  const { fetchedProfile, lastSubmittedDate } = await chrome.storage.local.get([
    'fetchedProfile',
    'lastSubmittedDate',
  ]);
  if (!fetchedProfile?.studentId) {
    const localMatch = lastSubmittedDate === fridayDateStr;
    await updateActionBadge(localMatch);
    return { submittedToday: localMatch, weekendDate: fridayDateStr, source: 'no_profile' };
  }

  try {
    const leavesRes = await camuPost('/api/Leave/getAllleave/', {
      StuID: fetchedProfile.studentId,
      isFrmReact: true,
    });
    const existingLeaves = findFirstArrayOfObjects(leavesRes);
    const foundWeekend = existingLeaves.some(
      (lv) => isGatePassEntry(lv) && isLeaveForDate(lv, fridayDateStr)
    );
    if (foundWeekend) {
      await chrome.storage.local.set({
        lastSubmittedDate: fridayDateStr,
        lastStatus: `Submitted for Fri ${fridayDateStr} → Sun ${sundayDateStr}`,
      });
      await updateActionBadge(true);
      return { submittedToday: true, weekendDate: fridayDateStr, source: 'camu_api' };
    }

    // Clear stale local submitted flag if pass was cancelled on Camu
    if (lastSubmittedDate === fridayDateStr) {
      await chrome.storage.local.set({
        lastSubmittedDate: '',
        lastStatus: 'Not submitted (cancelled or not found in Camu)',
      });
    }
    await updateActionBadge(false);
    return { submittedToday: false, weekendDate: fridayDateStr, source: 'camu_api' };
  } catch (_) {
    const localMatch = lastSubmittedDate === fridayDateStr;
    await updateActionBadge(localMatch);
    return { submittedToday: localMatch, weekendDate: fridayDateStr, source: 'error' };
  }
}

async function syncToolbarBadge() {
  await checkCamuSubmittedToday();
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

function resolvePassType(typesList, desiredLabel, existingLeaves = []) {
  const pastPass = existingLeaves.find((lv) => isGatePassEntry(lv) && (lv.passTy || lv.PassTy));
  const pastPassTy = pastPass ? String(pastPass.passTy || pastPass.PassTy).trim() : '';

  if (!typesList.length) {
    return {
      id: pastPassTy,
      label: pastPassTy,
      allLabels: [],
      rawSample: null,
      gateOutPassVal: pastPass?.GateOutPass ?? 'true',
      lvTyVal: pastPass?.LvTy || 'Leave',
    };
  }

  const getLabel = (item) =>
    String(
      item.Nm ||
        item.Name ||
        item.name ||
        item.code ||
        item.Code ||
        item.PassNm ||
        item.passNm ||
        item.Desc ||
        item.description ||
        item.label ||
        item.Title ||
        item.title ||
        item.PassTy ||
        item.passTy ||
        item.type ||
        ''
    ).trim();

  const getId = (item) =>
    String(
      item.passCd ||
        item.PassCd ||
        item.passTy ||
        item.PassTy ||
        item.Cd ||
        item._id ||
        item.id ||
        item.code ||
        item.Code ||
        item.value ||
        getLabel(item) ||
        ''
    ).trim();

  const allLabels = typesList.map((t) => `${getLabel(t) || '?'}:${getId(t)}`).filter(Boolean);
  const target = (desiredLabel || 'Week End Gate Pass').trim().toLowerCase();
  const weekendMatch = typesList.find(
    (item) =>
      String(item.passCd || '').toUpperCase() === 'WEEK_END_PASS' ||
      getLabel(item).toLowerCase().includes('week end')
  );
  const match =
    typesList.find((item) => {
      const lbl = getLabel(item).toLowerCase();
      const id = getId(item).toLowerCase();
      return target && ((lbl && (lbl === target || lbl.includes(target) || target.includes(lbl))) || id === target);
    }) || weekendMatch;
  const chosen = match || typesList[0];
  return {
    id: getId(chosen) || pastPassTy,
    label: getLabel(chosen) || pastPassTy,
    allLabels,
    rawSample: typesList[0],
    gateOutPassVal: pastPass?.GateOutPass ?? 'true',
    lvTyVal: pastPass?.LvTy || 'Leave',
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

  const { fridayDateStr, sundayDateStr } = getWeekendDates();
  if (!manual && !dryRun && config.lastSubmittedDate === fridayDateStr) {
    return { ok: true, skipped: true, message: `Already submitted for Fri ${fridayDateStr} → Sun ${sundayDateStr}.` };
  }

  try {
    // 1. Verify active Camu session & check for existing weekend gate pass (Friday out-date)
    const leavesRes = await camuPost('/api/Leave/getAllleave/', {
      StuID: profile.studentId,
      isFrmReact: true,
    });
    const existingLeaves = findFirstArrayOfObjects(leavesRes);
    const alreadyExists = existingLeaves.some(
      (lv) => isGatePassEntry(lv) && isLeaveForDate(lv, fridayDateStr)
    );
    if (alreadyExists && !dryRun) {
      const existsMsg = `Weekend gate pass already exists for Fri ${fridayDateStr} → Sun ${sundayDateStr}.`;
      await chrome.storage.local.set({
        lastSubmittedDate: fridayDateStr,
        lastStatus: existsMsg,
      });
      await updateActionBadge(true);
      return { ok: true, skipped: true, submittedToday: true, message: existsMsg };
    }

    // 2. Fetch pass types and resolve passTy ID
    const typesRes = await camuPost('/api/gatepasses/fetch-gatepasses-types', null);
    const typesList = findFirstArrayOfObjects(typesRes);
    const resolvedType = resolvePassType(typesList, config.passType || 'Week End Gate Pass', existingLeaves);

    if (!resolvedType.id) {
      const rawPreview = JSON.stringify(typesRes).slice(0, 140);
      throw new Error(`PASSTY_NOT_FOUND in fetch-gatepasses-types: ${rawPreview}`);
    }

    const outTime = config.outTime || '10:00';
    const inTime = config.inTime || '20:00';

    const payload = {
      Attachments: [],
      FrDt: `${fridayDateStr}T00:00:00.000Z`,
      GateOutPass: resolvedType.gateOutPassVal,
      HstleReq: 'true',
      InId: profile.institutionId || '',
      LvTy: resolvedType.lvTyVal,
      PerId: profile.studentId,
      PerNm: profile.name || '',
      PerTy: 'Student',
      PtAb: '',
      Rson: '',
      Sson: '',
      ToDt: `${sundayDateStr}T00:00:00.000Z`,
      atcmnt: [],
      frdt: buildUtcIso(fridayDateStr, outTime),
      loggedInId: profile.loggedInId || profile.studentId,
      passTy: resolvedType.id,
      periods: [],
      rson: config.reason || 'Outing',
      timezoneOffSet: 330,
      todt: buildUtcIso(sundayDateStr, inTime),
    };

    if (dryRun) {
      const msg = `Dry-run OK: ${resolvedType.label || resolvedType.id} (${resolvedType.id}) for Fri ${fridayDateStr} (${outTime}) → Sun ${sundayDateStr} (${inTime}).`;
      await chrome.storage.local.set({
        lastStatus: msg,
        lastRunAt: new Date().toISOString(),
      });
      return { ok: true, message: msg, payloadPreview: payload };
    }

    // 3. Silent POST to /api/Leave/crtLv
    await camuPost('/api/Leave/crtLv', payload);

    const okMsg = `Submitted: Fri ${fridayDateStr} (${outTime}) → Sun ${sundayDateStr} (${inTime})`;
    await chrome.storage.local.set({
      lastSubmittedDate: fridayDateStr,
      lastStatus: okMsg,
      lastRunAt: new Date().toISOString(),
    });
    await updateActionBadge(true);
    await showNotification('Auto Camu Pass', okMsg);
    return { ok: true, submittedToday: true, message: okMsg };
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
  await syncToolbarBadge();
  await checkScheduleAndRun();
});

chrome.runtime.onStartup.addListener(async () => {
  await ensureAlarm();
  await syncToolbarBadge();
  await checkScheduleAndRun();
});

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === ALARM_NAME) {
    await syncToolbarBadge();
    await checkScheduleAndRun();
  }
});

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  (async () => {
    if (message.action === 'PROFILE_SYNCED' && message.profile) {
      await chrome.storage.local.set({ fetchedProfile: message.profile });
      await checkCamuSubmittedToday();
      sendResponse({ ok: true });
      return;
    }

    if (message.action === 'CHECK_TODAY_STATUS') {
      const status = await checkCamuSubmittedToday();
      sendResponse(status);
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
