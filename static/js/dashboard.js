/**
 * SeeQL Dashboard — Alpine.js components, HTMX handlers, and utilities
 */

window.SeeQL = window.SeeQL || {};

// ---------------------------------------------------------------------------
// Copy to clipboard utility
// ---------------------------------------------------------------------------

SeeQL.copyToClipboard = function(text, buttonEl) {
    text = text.trim();
    navigator.clipboard.writeText(text).then(function() {
        // Show checkmark briefly
        var originalHTML = buttonEl.innerHTML;
        buttonEl.innerHTML = '<svg class="w-4 h-4 text-green-600" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7"/></svg>';
        buttonEl.classList.add('opacity-100');
        setTimeout(function() {
            buttonEl.innerHTML = originalHTML;
            buttonEl.classList.remove('opacity-100');
        }, 1500);
    }).catch(function() {
        // Fallback for older browsers
        var textarea = document.createElement('textarea');
        textarea.value = text;
        textarea.style.position = 'fixed';
        textarea.style.opacity = '0';
        document.body.appendChild(textarea);
        textarea.select();
        document.execCommand('copy');
        document.body.removeChild(textarea);
    });
};

// ---------------------------------------------------------------------------
// HTMX event hooks
// ---------------------------------------------------------------------------

// After HTMX swaps content, re-initialize Alpine components on new DOM
document.body.addEventListener('htmx:afterSwap', function (evt) {
    if (evt.detail && evt.detail.target && window.Alpine) {
        Alpine.initTree(evt.detail.target);
    }
});

// Show loading indicator during HTMX requests
document.body.addEventListener('htmx:beforeRequest', function (evt) {
    var target = evt.detail.target;
    if (target) {
        target.style.opacity = '0.6';
    }
});

document.body.addEventListener('htmx:afterRequest', function (evt) {
    var target = evt.detail.target;
    if (target) {
        target.style.opacity = '1';
    }
});
