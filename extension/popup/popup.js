const FIELDS = ['enabled', 'username', 'password', 'passType', 'reason', 'outTime', 'inTime'];

function setBanner(text) {
  document.getElementById('status-banner').textContent = text;
}

function renderProfile(profile) {
  const avatarEl = document.getElementById('profile-avatar');
  const nameEl = document.getElementById('profile-name');
  const metaEl = document.getElementById('profile-meta');
  const syncBtn = document.getElementById('sync-btn');

  if (!profile || (!profile.name && !profile.rollNo && !profile.studentId && !profile.email)) {
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

async function loadSettings() {
  const data = await chrome.storage.local.get([
    ...FIELDS,
    'lastStatus',
    'fetchedProfile',
    'captureEnabled',
    'capturedApis',
  ]);
  document.getElementById('enabled').checked = data.enabled ?? true;
  document.getElementById('username').value = data.username || '';
  document.getElementById('password').value = data.password || '';
  document.getElementById('passType').value = data.passType || 'Day Outing';
  document.getElementById('reason').value = data.reason || 'Personal work';
  document.getElementById('outTime').value = data.outTime || '10:00';
  document.getElementById('inTime').value = data.inTime || '20:00';

  if (data.lastStatus) {
    setBanner(data.lastStatus);
  }
  renderProfile(data.fetchedProfile);
  renderCaptureState(Boolean(data.captureEnabled), (data.capturedApis || []).length);
}

function renderCaptureState(captureEnabled, count) {
  const toggleBtn = document.getElementById('capture-toggle-btn');
  const countEl = document.getElementById('capture-count');
  countEl.textContent = `(${count} captured)`;
  toggleBtn.textContent = captureEnabled ? 'Stop recording' : 'Start recording';
}

async function toggleCapture() {
  const { captureEnabled = false, capturedApis = [] } = await chrome.storage.local.get([
    'captureEnabled',
    'capturedApis',
  ]);
  const next = !captureEnabled;
  await chrome.runtime.sendMessage({ action: 'TOGGLE_CAPTURE', enabled: next });
  renderCaptureState(next, capturedApis.length);
  setBanner(next ? 'Recording Camu API requests...' : 'API recording stopped.');
}

async function copyApiLog() {
  const { capturedApis = [] } = await chrome.storage.local.get('capturedApis');
  if (capturedApis.length === 0) {
    setBanner('No API requests captured yet.');
    return;
  }
  await navigator.clipboard.writeText(JSON.stringify(capturedApis, null, 2));
  setBanner(`Copied ${capturedApis.length} API request(s) to clipboard.`);
}

async function clearApiLog() {
  const { captureEnabled = false } = await chrome.storage.local.get('captureEnabled');
  await chrome.runtime.sendMessage({ action: 'CLEAR_CAPTURED_APIS' });
  renderCaptureState(captureEnabled, 0);
  setBanner('Cleared captured API log.');
}

async function saveSettings(event) {
  event.preventDefault();
  const payload = {
    enabled: document.getElementById('enabled').checked,
    username: document.getElementById('username').value.trim(),
    password: document.getElementById('password').value,
    passType: document.getElementById('passType').value.trim() || 'Day Outing',
    reason: document.getElementById('reason').value.trim() || 'Personal work',
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
  setBanner(dryRun ? 'Running dry-run...' : 'Submitting gate pass...');

  try {
    const res = await chrome.runtime.sendMessage({ action: 'RUN_NOW', dryRun });
    if (res?.profile) renderProfile(res.profile);
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
  document.getElementById('settings-form').addEventListener('submit', saveSettings);
  document.getElementById('sync-btn').addEventListener('click', openCamuAndSync);
  document.getElementById('dry-run-btn').addEventListener('click', () => triggerRun(true));
  document.getElementById('submit-now-btn').addEventListener('click', () => triggerRun(false));
  document.getElementById('capture-toggle-btn').addEventListener('click', toggleCapture);
  document.getElementById('copy-log-btn').addEventListener('click', copyApiLog);
  document.getElementById('clear-log-btn').addEventListener('click', clearApiLog);
});
