'use strict';

(() => {
  const banner = document.getElementById('captured-flag-banner');
  const visibleFlag = document.getElementById('visible-captured-flag');
  const popup = document.getElementById('flag-capture-dialog');
  const popupFlag = document.getElementById('flag-capture-value');
  const copyButton = document.getElementById('copy-captured-flag');
  if (!banner || !visibleFlag || !popup || !popupFlag || !copyButton) return;
  let flag = '';
  let copying = false;

  function render(value) {
    flag = typeof value === 'string' ? value : '';
    visibleFlag.textContent = flag;
    popupFlag.textContent = flag;
    banner.hidden = !flag;
    if (!flag && popup.open) popup.close();
  }

  function reveal(value) {
    render(value);
    if (!flag) return;
    popupFlag.textContent = flag;
    copyButton.textContent = 'Copy to clipboard';
    if (!popup.open) popup.showModal();
  }

  function fallbackCopy(value, dialog) {
    const input = document.createElement('textarea');
    input.value = value;
    input.setAttribute('readonly', '');
    input.setAttribute('aria-hidden', 'true');
    input.style.position = 'fixed';
    input.style.left = '-10000px';
    (dialog?.open ? dialog : document.body).append(input);
    try {
      input.focus({preventScroll:true});
      input.select();
      return document.execCommand('copy') === true;
    } finally {
      input.remove();
    }
  }

  async function copyAndClose(dialog = popup) {
    if (!flag || copying) return false;
    const value = flag;
    copying = true;
    copyButton.disabled = true;
    try {
      let copied = false;
      if (navigator.clipboard?.writeText) {
        try { await navigator.clipboard.writeText(value); copied = true; }
        catch { /* Plain HTTP and browser permissions may require selection copying. */ }
      }
      if (!copied) copied = fallbackCopy(value, dialog);
      if (!copied) throw new Error('Clipboard unavailable');
      if (dialog?.open) dialog.close();
      return true;
    } catch {
      copyButton.textContent = 'Copy failed — select the flag';
      return false;
    } finally {
      copying = false;
      copyButton.disabled = false;
    }
  }

  copyButton.addEventListener('click', () => copyAndClose());
  window.FlagPresentation = Object.freeze({render, reveal, copyAndClose});
})();
