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
  button.type = 'button'; button.className = 'btn btn-small'; button.textContent = '🎙 Voice';
  button.setAttribute('aria-label', 'Search using your voice');
  search.parentElement.appendChild(button);
  button.addEventListener('click', () => {
    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    const recognition = new SpeechRecognition();
    recognition.lang = document.documentElement.lang === 'ta' ? 'ta-IN' : 'en-IN';
    recognition.interimResults = false; recognition.maxAlternatives = 1;
    button.disabled = true; button.textContent = 'Listening…';
    recognition.onresult = event => { search.value = event.results[0][0].transcript; form.requestSubmit(); };
    recognition.onerror = () => { button.textContent = '🎙 Try again'; };
    recognition.onend = () => { button.disabled = false; if (button.textContent === 'Listening…') button.textContent = '🎙 Voice'; };
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
