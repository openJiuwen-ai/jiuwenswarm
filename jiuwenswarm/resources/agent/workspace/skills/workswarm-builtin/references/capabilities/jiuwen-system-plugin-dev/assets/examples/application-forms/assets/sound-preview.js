// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
(() => {
  'use strict';
  const zh = navigator.language.startsWith('zh');
  document.documentElement.lang = zh ? 'zh' : 'en';
  document.title = document.getElementById('title').textContent = zh ? '切换音效试听' : 'Preview transition sounds';
  const error = document.getElementById('error');
  const player = window.WorkSwarmTransitionSound.create({ onError: value => {
    console.error(value); error.hidden = false;
    error.textContent = zh ? '音效无法播放，请返回设置页重新打开试听。' : 'The sound could not be played. Reopen this preview from settings.';
  } });
  try {
    const config = JSON.parse(decodeURIComponent(location.hash.slice(1)));
    player.configure(config);
    document.getElementById('volume').textContent = zh ? `当前音量：${Math.round(config.volume * 100)}%` : `Volume: ${Math.round(config.volume * 100)}%`;
    for (const [id, label] of [['compact', zh ? '进入形态' : 'Enter form'], ['workspace', zh ? '返回工作台' : 'Return to workspace']]) {
      const button = document.getElementById(id); button.textContent = label;
      button.onclick = () => { error.hidden = true; void player.play(id); };
    }
  } catch (value) {
    console.error(value); error.hidden = false;
    error.textContent = zh ? '试听地址无效，请从设置页打开。' : 'Invalid preview link. Open it from settings.';
    for (const id of ['compact', 'workspace']) document.getElementById(id).disabled = true;
  }
  window.addEventListener('pagehide', () => player.dispose(), { once: true });
})();
