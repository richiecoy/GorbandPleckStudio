/**
 * Gorb & Pleck Studio — Frontend utilities
 * Alpine.js handles most interactivity inline. This file provides shared helpers.
 */

// Toast notification system
document.addEventListener('alpine:init', () => {
    Alpine.store('toast', {
        messages: [],
        show(text, type = 'info', duration = 3000) {
            const id = Date.now();
            this.messages.push({ id, text, type });
            setTimeout(() => {
                this.messages = this.messages.filter(m => m.id !== id);
            }, duration);
        }
    });
});

// Global fetch wrapper with error handling
async function studioFetch(url, options = {}) {
    try {
        const resp = await fetch(url, {
            headers: { 'Content-Type': 'application/json' },
            ...options,
        });
        const data = await resp.json();
        if (!resp.ok) {
            const msg = data.detail || data.error || `HTTP ${resp.status}`;
            Alpine.store('toast')?.show(msg, 'error');
            return null;
        }
        return data;
    } catch (e) {
        Alpine.store('toast')?.show('Network error: ' + e.message, 'error');
        return null;
    }
}
