const FIELDS = ['enabled', 'passType', 'reason', 'outTime', 'inTime'];

function setBanner(text) {
  document.getElementById('status-banner').textContent = text;
}

function renderProfile(profile) {
  const avatarEl = document.getElementById('profile-avatar');
  const nameEl = document.getElementById('profile-name');
  const metaEl = document.getElementById('profile-meta');
  const syncBtn = document.getElementById('sync-btn');

  if (!profile || (!profile.studentId && !profile.name && !profile.email)) {
    avatarEl.textContent = '?';
    nameEl.textContent = 'Camu not synced';
    metaEl.textContent = 'Open Camu & log in to fetch details';
    syncBtn.textContent = 'Open Camu';
    return;
  }

  const displayName = profile.name || profile.email || 'Camu Student';
  avatarEl.textContent = displayName.charAt(0).toUpperCase();
  nameEl.textContent = displayName;
  const metaParts = [profile.rollNo || profile.studentId, profile.phone || profile.email].filter(Boolean);
  metaEl.textContent = metaParts.join(' · ') || 'Synced from Camu session';
  syncBtn.textContent = 'Re-sync';
}

function getLocalDateString(now = new Date()) {
  const year = now.getFullYear();
  const month = String(now.getMonth() + 1).padStart(2, '0');
  const day = String(now.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

function renderSubmittedPin(lastSubmittedDate) {
  const pin = document.getElementById('submitted-pin');
  const text = document.getElementById('submitted-pin-text');
  const isSubmittedToday = lastSubmittedDate === getLocalDateString();
  pin.className = `badge ${isSubmittedToday ? 'badge-submitted' : 'badge-pending'}`;
  text.textContent = isSubmittedToday ? 'Submitted' : 'Not submitted';
}

function renderScriptBadge(enabled) {
  const badge = document.getElementById('script-state-badge');
  badge.textContent = enabled ? 'ON' : 'OFF';
}

async function loadSettings() {
  const data = await chrome.storage.local.get([...FIELDS, 'lastStatus', 'fetchedProfile', 'lastSubmittedDate']);
  const isEnabled = data.enabled ?? true;
  document.getElementById('enabled').checked = isEnabled;
  renderScriptBadge(isEnabled);
  renderSubmittedPin(data.lastSubmittedDate);
  document.getElementById('passType').value = data.passType || 'Day Outing';
  document.getElementById('reason').value = data.reason || 'Outing';
  document.getElementById('outTime').value = data.outTime || '10:00';
  document.getElementById('inTime').value = data.inTime || '20:00';

  if (data.lastStatus) {
    setBanner(data.lastStatus);
  }
  renderProfile(data.fetchedProfile);

  // Also check Camu silently so manual submissions show as Submitted
  try {
    const live = await chrome.runtime.sendMessage({ action: 'CHECK_TODAY_STATUS' });
    if (live?.submittedToday) {
      renderSubmittedPin(getLocalDateString());
      if (live.source === 'camu_api') {
        setBanner(`Submitted in Camu (${getLocalDateString()})`);
      }
    }
  } catch (_) {}
}

async function toggleScript(event) {
  const enabled = event.target.checked;
  renderScriptBadge(enabled);
  await chrome.storage.local.set({ enabled });
  const res = await chrome.runtime.sendMessage({ action: 'TOGGLE_SCRIPT', enabled });
  setBanner(
    res?.message ||
      (enabled
        ? 'Script ON: checks on browser launch & every 15m on Thu & Fri.'
        : 'Script OFF: Thu & Fri auto-submit paused.')
  );
}

async function saveSettings(event) {
  event.preventDefault();
  const payload = {
    enabled: document.getElementById('enabled').checked,
    passType: document.getElementById('passType').value.trim() || 'Day Outing',
    reason: document.getElementById('reason').value.trim() || 'Outing',
    outTime: document.getElementById('outTime').value || '10:00',
    inTime: document.getElementById('inTime').value || '20:00',
  };

  await chrome.storage.local.set(payload);
  setBanner('Settings saved.');
}

async function openCamuAndSync() {
  const syncBtn = document.getElementById('sync-btn');
  syncBtn.disabled = true;
  setBanner('Opening Camu to sync profile...');
  try {
    const res = await chrome.runtime.sendMessage({ action: 'OPEN_CAMU_AND_SYNC' });
    if (res?.profile) renderProfile(res.profile);
    setBanner(res?.message || 'Open Camu & log in; details will sync automatically.');
  } catch (err) {
    setBanner(`Error: ${err.message}`);
  } finally {
    syncBtn.disabled = false;
  }
}

async function triggerRun(dryRun) {
  const dryBtn = document.getElementById('dry-run-btn');
  const subBtn = document.getElementById('submit-now-btn');
  dryBtn.disabled = true;
  subBtn.disabled = true;
  setBanner(dryRun ? 'Verifying session silently...' : 'Submitting gate pass silently...');

  try {
    const res = await chrome.runtime.sendMessage({ action: 'RUN_NOW', dryRun });
    if (res?.profile) renderProfile(res.profile);
    if (res?.submittedToday) renderSubmittedPin(getLocalDateString());
    setBanner(res?.message || 'Done.');
  } catch (err) {
    setBanner(`Error: ${err.message}`);
  } finally {
    dryBtn.disabled = false;
    subBtn.disabled = false;
  }
}

document.addEventListener('DOMContentLoaded', async () => {
  await loadSettings();
  document.getElementById('enabled').addEventListener('change', toggleScript);
  document.getElementById('settings-form').addEventListener('submit', saveSettings);
  document.getElementById('sync-btn').addEventListener('click', openCamuAndSync);
  document.getElementById('dry-run-btn').addEventListener('click', () => triggerRun(true));
  document.getElementById('submit-now-btn').addEventListener('click', () => triggerRun(false));
});
