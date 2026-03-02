const queue = [];
const dropzone = document.getElementById('dropzone');
const picker = document.getElementById('picker');
const queueEl = document.getElementById('queue');
const tpl = document.getElementById('itemTemplate');

function addFiles(files) {
  [...files].forEach((file) => {
    if (!file.type.startsWith('video/')) return;
    const item = { file, start: 0, end: null, mute: false };
    queue.push(item);
    renderItem(item);
  });
}

function renderItem(item) {
  const node = tpl.content.firstElementChild.cloneNode(true);
  const video = node.querySelector('video');
  const name = node.querySelector('.name');
  const start = node.querySelector('.start');
  const end = node.querySelector('.end');
  const mute = node.querySelector('.mute');

  const url = URL.createObjectURL(item.file);
  video.src = url;
  video.currentTime = 1;
  name.textContent = item.file.name;

  video.addEventListener('loadedmetadata', () => {
    item.end = Math.floor(video.duration * 10) / 10;
    end.value = item.end;
  });

  start.addEventListener('input', () => item.start = Number(start.value || 0));
  end.addEventListener('input', () => item.end = Number(end.value || 0));
  mute.addEventListener('change', () => item.mute = mute.checked);

  queueEl.appendChild(node);
}

dropzone.addEventListener('click', () => picker.click());
picker.addEventListener('change', (e) => addFiles(e.target.files));

dropzone.addEventListener('dragover', (e) => {
  e.preventDefault();
  dropzone.classList.add('active');
});
dropzone.addEventListener('dragleave', () => dropzone.classList.remove('active'));
dropzone.addEventListener('drop', (e) => {
  e.preventDefault();
  dropzone.classList.remove('active');
  addFiles(e.dataTransfer.files);
});

document.getElementById('startBtn').addEventListener('click', async () => {
  if (queue.length === 0) {
    alert('Add at least one video.');
    return;
  }

  const fd = new FormData();
  fd.append('target_mb', document.getElementById('targetMb').value || '50');
  fd.append('resolution', document.getElementById('resolution').value);
  fd.append('codec', document.getElementById('codec').value);

  const metadata = queue.map((item) => ({
    start: item.start || 0,
    end: item.end,
    mute: item.mute,
    source_path: item.file.path || null,
  }));

  queue.forEach((item) => fd.append('videos', item.file, item.file.name));
  fd.append('metadata', JSON.stringify(metadata));

  const res = await fetch('/api/compress', { method: 'POST', body: fd });
  const data = await res.json();
  if (!res.ok) {
    alert(data.error || 'Failed to start');
    return;
  }
  pollStatus();
});

document.getElementById('stopBtn').addEventListener('click', async () => {
  await fetch('/api/stop', { method: 'POST' });
});

let pollTimer = null;
function pollStatus() {
  if (pollTimer) return;
  pollTimer = setInterval(async () => {
    const res = await fetch('/api/status');
    const status = await res.json();
    document.getElementById('progressBar').style.width = `${status.progress || 0}%`;
    document.getElementById('statusText').textContent = status.message || 'Idle';

    if (!status.running) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }, 500);
}
