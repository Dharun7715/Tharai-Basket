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
