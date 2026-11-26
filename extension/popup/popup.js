const FIELDS = ['enabled', 'username', 'password', 'passType', 'reason', 'outTime', 'inTime'];

function setBanner(text) {
  document.getElementById('status-banner').textContent = text;
}

function setProfileBanner(profile) {
  const el = document.getElementById('profile-banner');
  if (!profile || (!profile.name && !profile.rollNo && !profile.studentId)) {
    el.style.display = 'none';
    return;
  }
  const parts = [profile.name, profile.rollNo || profile.studentId, profile.phone].filter(Boolean);
  el.textContent = `Camu Profile: ${parts.join(' • ')}`;
  el.style.display = 'block';
}

async function loadSettings() {
  const data = await chrome.storage.local.get([...FIELDS, 'lastStatus', 'fetchedProfile']);
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
  setProfileBanner(data.fetchedProfile);
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
  setBanner('Settings saved locally.');
}

async function triggerRun(dryRun) {
  const dryBtn = document.getElementById('dry-run-btn');
  const subBtn = document.getElementById('submit-now-btn');
  dryBtn.disabled = true;
  subBtn.disabled = true;
  setBanner(dryRun ? 'Running dry-run...' : 'Submitting gate pass...');

  try {
    const res = await chrome.runtime.sendMessage({ action: 'RUN_NOW', dryRun });
    setBanner(res?.message || 'Done.');
    if (res?.profile) setProfileBanner(res.profile);
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
  document.getElementById('dry-run-btn').addEventListener('click', () => triggerRun(true));
  document.getElementById('submit-now-btn').addEventListener('click', () => triggerRun(false));
});
