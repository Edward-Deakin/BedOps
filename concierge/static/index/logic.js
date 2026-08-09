const log = document.getElementById('log');
const form = document.getElementById('chat-form');
const input = document.getElementById('chat-input');

function appendMessage(text, role) {
    const el = document.createElement('div');
    el.className = `chat-msg ${role}`;
    el.textContent = text;
    log.appendChild(el);
    log.scrollTop = log.scrollHeight;
    return el;
}

appendMessage("Hi! I'm the BedOps concierge. Try: create a world called Skyline pin 4242", 'agent');

form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const message = input.value.trim();
    if (!message) return;

    appendMessage(message, 'user');
    input.value = '';
    input.disabled = true;

    const pending = appendMessage('...', 'agent pending');

    try {
        const response = await fetch('/api/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ message }),
        });
        const data = await response.json();
        pending.textContent = data.reply || "Sorry, I didn't get a reply.";
        pending.classList.remove('pending');
    } catch (error) {
        pending.textContent = `Connection error: ${error.message}`;
        pending.classList.remove('pending');
    } finally {
        input.disabled = false;
        input.focus();
    }
});
