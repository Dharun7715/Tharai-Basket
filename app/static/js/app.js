document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('form').forEach((form) => {
    form.addEventListener('submit', () => {
      const button = form.querySelector('button[type="submit"], button:not([type])');
      if (button && !button.disabled && !form.classList.contains('workflow-form')) {
        button.dataset.originalText = button.textContent;
        button.textContent = 'Please wait…';
        button.disabled = true;
        window.setTimeout(() => { button.disabled = false; button.textContent = button.dataset.originalText || 'Submit'; }, 5000);
      }
    });
  });
});


// Voice search: progressive enhancement using the browser's Web Speech API.
document.addEventListener('DOMContentLoaded', () => {
  const search = document.querySelector('#shop-search');
  if (!search || !('webkitSpeechRecognition' in window || 'SpeechRecognition' in window)) return;
  const form = search.closest('form');
  const button = document.createElement('button');
  button.type = 'button'; button.className = 'tb-voice-search';
  button.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="2" width="6" height="12" rx="3"></rect><path d="M5 10a7 7 0 0 0 14 0M12 17v5m-4 0h8"></path></svg>';
  button.setAttribute('aria-label', 'Search using your voice');
  button.setAttribute('title', 'Voice search');
  search.parentElement.appendChild(button);
  button.addEventListener('click', () => {
    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    const recognition = new SpeechRecognition();
    recognition.lang = document.documentElement.lang === 'ta' ? 'ta-IN' : 'en-IN';
    recognition.interimResults = false; recognition.maxAlternatives = 1;
    button.disabled = true; button.classList.add('is-listening'); button.setAttribute('aria-label', 'Listening…');
    recognition.onresult = event => { search.value = event.results[0][0].transcript; form.requestSubmit(); };
    recognition.onerror = () => { button.setAttribute('aria-label', 'Voice search failed. Try again'); };
    recognition.onend = () => { button.disabled = false; button.classList.remove('is-listening'); button.setAttribute('aria-label', 'Search using your voice'); };
    recognition.start();
  });
});

// Lightweight language preference toggle. Stored locally in the browser only.
document.addEventListener('DOMContentLoaded', () => {
  const key = 'tharai-language';
  const lang = localStorage.getItem(key) || 'en';
  document.documentElement.lang = lang === 'ta' ? 'ta' : 'en';
  const toggle = document.createElement('button');
  toggle.type = 'button'; toggle.className = 'nav-link'; toggle.textContent = lang === 'ta' ? 'English' : 'தமிழ்';
  toggle.setAttribute('aria-label', 'Switch website language');
  document.querySelector('.nav-actions')?.appendChild(toggle);
  toggle.addEventListener('click', () => {
    const next = (localStorage.getItem(key) || 'en') === 'en' ? 'ta' : 'en';
    localStorage.setItem(key, next);
    document.documentElement.lang = next === 'ta' ? 'ta' : 'en';
    location.reload();
  });
});


// Progressive Web App installation (Android/desktop prompt + iPhone/iPad guide).
document.addEventListener('DOMContentLoaded', () => {
  let deferredInstallPrompt = null;
  const isIOS = /iphone|ipad|ipod/i.test(navigator.userAgent) ||
    (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  const isStandalone = () => window.matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;
  if (isStandalone()) return;

  const actions = document.querySelector('.nav-actions');
  if (!actions) return;

  const installButton = document.createElement('button');
  installButton.type = 'button';
  installButton.className = 'install-app-btn';
  installButton.textContent = 'Install App';
  installButton.setAttribute('aria-label', 'Install Tharai Basket app');
  installButton.hidden = true;
  actions.insertBefore(installButton, actions.firstChild);

  const help = document.createElement('div');
  help.className = 'install-help-backdrop';
  help.hidden = true;
  help.innerHTML = `
    <section class="install-help-card" role="dialog" aria-modal="true" aria-labelledby="install-help-title">
      <button type="button" class="install-help-close" aria-label="Close">×</button>
      <div class="install-help-icon">🛒</div>
      <h2 id="install-help-title">Install Tharai Basket</h2>
      <p class="install-help-intro">Add Tharai Basket to your home screen for quick access like an app.</p>
      <div class="install-help-steps"></div>
      <button type="button" class="btn btn-full install-help-done">Got it</button>
    </section>`;
  document.body.appendChild(help);
  const steps = help.querySelector('.install-help-steps');

  function showHelp() {
    if (isIOS) {
      steps.innerHTML = '<p><b>1.</b> Open this website in <b>Safari</b>.</p><p><b>2.</b> Tap the <b>Share</b> icon at the bottom.</p><p><b>3.</b> Choose <b>Add to Home Screen</b>, then tap <b>Add</b>.</p>';
    } else {
      steps.innerHTML = '<p><b>1.</b> Open the browser menu (⋮).</p><p><b>2.</b> Tap <b>Install app</b> or <b>Add to Home screen</b>.</p><p><b>3.</b> Confirm the installation.</p>';
    }
    help.hidden = false;
    document.body.classList.add('install-help-open');
  }
  function closeHelp() {
    help.hidden = true;
    document.body.classList.remove('install-help-open');
  }
  help.querySelector('.install-help-close').addEventListener('click', closeHelp);
  help.querySelector('.install-help-done').addEventListener('click', closeHelp);
  help.addEventListener('click', event => { if (event.target === help) closeHelp(); });

  if (isIOS) installButton.hidden = false;
  window.addEventListener('beforeinstallprompt', event => {
    event.preventDefault();
    deferredInstallPrompt = event;
    installButton.hidden = false;
  });
  installButton.addEventListener('click', async () => {
    if (deferredInstallPrompt) {
      deferredInstallPrompt.prompt();
      await deferredInstallPrompt.userChoice;
      deferredInstallPrompt = null;
      installButton.hidden = true;
    } else {
      showHelp();
    }
  });
  window.addEventListener('appinstalled', () => {
    installButton.hidden = true;
    closeHelp();
  });
});
