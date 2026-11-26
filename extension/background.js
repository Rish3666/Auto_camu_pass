/**
 * Background Service Worker for Auto Camu Pass (Manifest V3).
 * Uses chrome.alarms + chrome.storage.local (no ephemeral in-memory state).
 */

const ALARM_NAME = 'camu-tuesday-check';
const CAMU_URL = 'https://student.camu.in/v2/profile';

function getLocalDateString(now = new Date()) {
  const year = now.getFullYear();
  const month = String(now.getMonth() + 1).padStart(2, '0');
  const day = String(now.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

function isTuesdayWindow(now = new Date()) {
  const isTuesday = now.getDay() === 2;
  const hour = now.getHours();
  return isTuesday && hour >= 9 && hour < 23;
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
  const existingTabs = await chrome.tabs.query({ url: 'https://student.camu.in/*' });
  let tab;
  if (existingTabs.length > 0) {
    tab = existingTabs[0];
    await chrome.tabs.update(tab.id, { active: true });
    await chrome.windows.update(tab.windowId, { focused: true });
  } else {
    tab = await chrome.tabs.create({ url: CAMU_URL, active: true });
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

async function runGatePassSubmission({ dryRun = false, manual = false } = {}) {
  const config = await chrome.storage.local.get([
    'enabled',
    'username',
    'password',
    'passType',
    'reason',
    'outTime',
    'inTime',
    'lastSubmittedDate',
  ]);

  if (!manual && !config.enabled) {
    return { ok: false, skipped: true, message: 'Auto-submit is paused.' };
  }

  const todayStr = getLocalDateString();
  if (!manual && !dryRun && config.lastSubmittedDate === todayStr) {
    return { ok: true, skipped: true, message: `Already submitted today (${todayStr}).` };
  }

  let tab;
  try {
    tab = await chrome.tabs.create({ url: CAMU_URL, active: dryRun });
    await waitForTabComplete(tab.id);

    await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      files: ['content.js'],
    });

    const response = await chrome.tabs.sendMessage(tab.id, {
      action: 'SUBMIT_GATEPASS',
      payload: {
        username: config.username || '',
        password: config.password || '',
        passType: config.passType || 'Day Outing',
        reason: config.reason || 'Personal work',
        outTime: config.outTime || '10:00',
        inTime: config.inTime || '20:00',
        dateStr: todayStr,
        dryRun,
      },
    });

    const statusText = response?.message || 'Completed.';
    const baseUpdates = response?.profile ? { fetchedProfile: response.profile } : {};
    if (response?.ok) {
      const updates = {
        ...baseUpdates,
        lastStatus: dryRun ? `Dry-run OK (${todayStr})` : `Submitted (${todayStr})`,
        lastRunAt: new Date().toISOString(),
      };
      if (!dryRun) {
        updates.lastSubmittedDate = todayStr;
      }
      await chrome.storage.local.set(updates);
      await showNotification('Auto Camu Pass', updates.lastStatus);
    } else {
      await chrome.storage.local.set({
        ...baseUpdates,
        lastStatus: `Failed: ${statusText}`,
        lastRunAt: new Date().toISOString(),
      });
      await showNotification('Auto Camu Pass — Failed', statusText);
    }

    return response || { ok: false, message: 'No response from Camu page.' };
  } catch (err) {
    const msg = err?.message || String(err);
    await chrome.storage.local.set({
      lastStatus: `Error: ${msg}`,
      lastRunAt: new Date().toISOString(),
    });
    await showNotification('Auto Camu Pass — Error', msg);
    return { ok: false, message: msg };
  } finally {
    if (tab?.id && !dryRun) {
      try {
        await chrome.tabs.remove(tab.id);
      } catch (_) {}
    }
  }
}

async function checkScheduleAndRun() {
  if (!isTuesdayWindow()) return;
  await runGatePassSubmission({ dryRun: false, manual: false });
}

chrome.runtime.onInstalled.addListener(async () => {
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

function decodeRequestBody(requestBody) {
  if (!requestBody) return null;
  if (requestBody.formData) return requestBody.formData;
  if (requestBody.raw && requestBody.raw.length > 0) {
    try {
      const decoder = new TextDecoder('utf-8');
      const rawText = requestBody.raw.map((part) => (part.bytes ? decoder.decode(part.bytes) : '')).join('');
      const parsed = JSON.parse(rawText);
      if (parsed && typeof parsed === 'object') {
        for (const key of Object.keys(parsed)) {
          if (/pass|pwd|secret|token/i.test(key)) parsed[key] = '***REDACTED***';
        }
      }
      return parsed;
    } catch (_) {
      return '[binary/non-json body]';
    }
  }
  return null;
}

chrome.webRequest.onBeforeRequest.addListener(
  (details) => {
    if (details.type !== 'xmlhttprequest' && details.type !== 'fetch') return;
    (async () => {
      const { captureEnabled = false, capturedApis = [] } = await chrome.storage.local.get([
        'captureEnabled',
        'capturedApis',
      ]);
      if (!captureEnabled) return;

      const entry = {
        time: new Date().toISOString(),
        method: details.method,
        url: details.url,
        body: decodeRequestBody(details.requestBody),
      };
      const updated = [...capturedApis.slice(-39), entry];
      await chrome.storage.local.set({ capturedApis: updated });
    })();
  },
  { urls: ['https://student.camu.in/*'] },
  ['requestBody']
);

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

    if (message.action === 'TOGGLE_CAPTURE') {
      const enabled = Boolean(message.enabled);
      await chrome.storage.local.set({ captureEnabled: enabled });
      sendResponse({ ok: true, captureEnabled: enabled });
      return;
    }

    if (message.action === 'CLEAR_CAPTURED_APIS') {
      await chrome.storage.local.set({ capturedApis: [] });
      sendResponse({ ok: true });
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
