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
