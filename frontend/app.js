/*
 * Everything the page does. Read it top to bottom, it follows the user's journey:
 *
 *   1. SEARCH       startSearch()  -> POST /agent/start, then GET /agent/stream (live steps)
 *   2. RESULTS      showResults()  -> cards with Maps / Call / Negotiate buttons
 *   3. NEGOTIATE    launchNegotiation() -> POST /agent/negotiate/start (LLM drafts the 1st message)
 *                   approveFirstMessage() -> POST /agent/approve (message is sent)
 *   4. CHAT         checkForReply() every 10 s -> GET /agent/check-reply
 *                   sendChatDraft() / skipChatDraft() / sendManualMessage() / endNegotiation()
 */

// The backend address. '' = the same server that serves this page (http://localhost:8000).
// If you host this page somewhere else, put the backend URL here, e.g. 'https://my-api.onrender.com'.
const API_BASE = '';

const POLL_EVERY_MS = 10000;

// One id per browser tab (survives page reloads, new tab = new session). Sent with every request,
// so all traces of this visit are grouped together in Langfuse (Sessions).
const SESSION_ID = (() => {
    try {
        let id = sessionStorage.getItem('sessionId');
        if (!id) { id = crypto.randomUUID(); sessionStorage.setItem('sessionId', id); }
        return id;
    } catch {
        return crypto.randomUUID();
    }
})();

let threadId = null;          // id of this conversation on the server
let pollTimer = null;         // setInterval handle while waiting for message replies
let selectedPlace = null;     // place name chosen in the negotiation form


// ============================================================
// Small helpers
// ============================================================

const $ = (id) => document.getElementById(id);

/** Make text safe to put inside HTML (place names, message texts, ... come from outside). */
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text ?? '';
    return div.innerHTML.replace(/"/g, '&quot;');
}

/** Call the backend. GET when there is no body, POST with JSON otherwise. Throws on errors. */
async function api(path, body) {
    const options = body === undefined ? { headers: { 'X-Session-Id': SESSION_ID } } : {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Session-Id': SESSION_ID },
        body: JSON.stringify(body),
    };
    const response = await fetch(API_BASE + path, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
        const detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
        throw new Error(detail || `HTTP ${response.status}`);
    }
    return data;
}

function timeNow() {
    return new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

/** Add a line to the agent log. `html` must already be escaped. type: info | success | warning | error */
function addLog(type, html) {
    const entry = document.createElement('div');
    entry.className = `log-entry ${type}`;
    entry.innerHTML = `<span class="log-time">[${new Date().toLocaleTimeString()}]</span> <span class="log-message">${html}</span>`;
    const log = $('logContainer');
    log.insertBefore(entry, $('thinking'));   // keep the "thinking" bubble at the bottom
    log.scrollTop = log.scrollHeight;
}

/** Show an animated "agent is working" bubble at the bottom of the log. setThinking(null) hides it. */
function setThinking(text) {
    $('thinking')?.remove();
    if (!text) return;
    const bubble = document.createElement('div');
    bubble.id = 'thinking';
    bubble.className = 'thinking-indicator';
    bubble.innerHTML = `<span>${escapeHtml(text)}</span>
        <div class="thinking-dots"><div class="dot"></div><div class="dot"></div><div class="dot"></div></div>`;
    $('logContainer').appendChild(bubble);
    $('logContainer').scrollTop = $('logContainer').scrollHeight;
}

/** Light up the step indicator: steps before `number` are done, `number` is active. */
function setStep(number) {
    document.querySelectorAll('.step').forEach((step, index) => {
        step.classList.toggle('completed', index + 1 < number);
        step.classList.toggle('active', index + 1 === number);
    });
}

function toolBadge(name, icon) {
    return `<div class="tool-usage"><span class="tool-icon">${icon}</span><span>Used tool: <strong>${name}</strong></span></div>`;
}


// ============================================================
// 1. SEARCH
// ============================================================

async function startSearch() {
    const query = $('userQuery').value.trim();
    if (!query) {
        $('userQuery').focus();
        return;
    }

    $('searchCard').hidden = true;
    $('workflowContainer').hidden = false;
    setStep(1);
    addLog('info', `📝 You asked: "${escapeHtml(query)}"`);
    setThinking('Understanding your request and searching Google...');

    try {
        const data = await api('/agent/start', { user_query: query });
        threadId = data.thread_id;

        const p = data.parsed;
        addLog('info', `🧠 Understood: <b>${escapeHtml(p.place_type)}</b> in <b>${escapeHtml(p.area)}, ${escapeHtml(p.city)}</b> — goal: ${escapeHtml(p.intent)}`);
        setStep(2);
        addLog('success', `${toolBadge('SerpStack (Google search)', '🔍')} Searched "${escapeHtml(data.search_text)}" → found <b>${data.places_found}</b> places`);

        if (data.places_found === 0) {
            setThinking(null);
            addLog('error', '❌ No places found. Try another area or wording (or check your SerpStack key / quota).');
            return;
        }

        setStep(3);
        streamSearch();
    } catch (error) {
        setThinking(null);
        addLog('error', `❌ ${escapeHtml(error.message)} (is the backend running?)`);
    }
}

/** Open a live connection; the server sends one event per finished graph step. */
function streamSearch() {
    setThinking('Ranking places by rating...');
    // EventSource cannot send headers, so the session id goes in the URL
    const source = new EventSource(`${API_BASE}/agent/stream?thread_id=${threadId}&session_id=${SESSION_ID}`);

    source.onmessage = (message) => {
        const event = JSON.parse(message.data);
        if (event.type === 'complete' || event.type === 'error') source.close();
        handleStreamEvent(event);
    };
    source.onerror = () => source.close();
}

function handleStreamEvent(event) {
    if (event.type === 'error') {
        setThinking(null);
        addLog('error', `❌ ${escapeHtml(event.error)}`);
        return;
    }

    if (event.type === 'complete') {
        setThinking(null);
        setStep(6);
        showResults(event.recommendations, event.all_places);
        return;
    }

    // event.type === 'step'
    if (event.node === 'rank_places') {
        if (event.show_all) {
            addLog('success', `📋 You asked for a list → showing all <b>${event.candidates.length}</b> places, best rated first`);
        } else {
            addLog('success', `📊 Top candidates by rating: <b>${event.candidates.map(escapeHtml).join(', ')}</b>`);
            setStep(4);
            setThinking('Reading Google reviews of the top candidates...');
        }
    } else if (event.node === 'fetch_reviews') {
        addLog('info', toolBadge('WebScraping.AI + LLM', '🤖') + renderReviews(event.reviews));
        setStep(5);
        setThinking('LLM is comparing the reviews to pick a winner...');
    } else if (event.node === 'pick_winner') {
        addLog('success', `<div class="streaming-text winner-summary">${escapeHtml(event.summary)}</div>`);
    }
}

function renderReviews(reviews) {
    let html = '<div class="review-analysis"><strong>Reviews found:</strong>';
    for (const [place, texts] of Object.entries(reviews)) {
        html += `<div class="review-item"><strong>🏢 ${escapeHtml(place)}</strong>`;
        texts.forEach((text, i) => {
            const short = text.length > 200 ? text.slice(0, 200) + '...' : text;
            html += `<p class="review-text">📄 ${i + 1}. "${escapeHtml(short)}"</p>`;
        });
        html += '</div>';
    }
    return html + '</div>';
}


// ============================================================
// 2. RESULTS
// ============================================================

function showResults(recommendations, allPlaces) {
    const best = recommendations[0];
    const others = allPlaces.filter((place) => !best || place.name !== best.name);

    let html = '';
    if (best) {
        html += `<h3 class="results-heading best">🏆 Top Recommended</h3>${placeCard(best, true)}`;
    }
    if (others.length) {
        html += `<h3 class="results-heading">🏪 All Places (${others.length})</h3>
                 <div class="results-grid">${others.map((place) => placeCard(place, false)).join('')}</div>`;
    }
    if (!html) {
        html = '<p class="empty">No matches found.</p>';
    }

    $('resultsContent').innerHTML = html;
    $('resultsContainer').hidden = false;
    $('resultsContainer').scrollIntoView({ behavior: 'smooth' });

    // Every "Negotiate" button remembers its place name in data-name
    document.querySelectorAll('[data-negotiate]').forEach((button) => {
        button.addEventListener('click', () => openNegotiationModal(button.dataset.name));
    });
    addLog('success', '🎉 Done! Pick a place below, or click Negotiate.');
}

function placeCard(place, isBest) {
    const mapsUrl = `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(place.address || place.name)}`;
    const phoneDigits = (place.phone || '').replace(/[^+\d]/g, '');

    return `
        <div class="result-card ${isBest ? 'best' : ''}">
            ${isBest ? '<div class="best-badge">⭐ BEST MATCH</div>' : ''}
            <h3>${escapeHtml(place.name)}</h3>
            <div class="result-info">
                <div class="result-info-item"><strong>⭐ Rating</strong> ${place.rating ?? 'N/A'}</div>
                <div class="result-info-item"><strong>💬 Reviews</strong> ${place.reviews_count || 'N/A'}</div>
                <div class="result-info-item">
                    <strong>📍 Location</strong> ${escapeHtml(place.address || 'N/A')}
                    <a class="inline-chip" href="${mapsUrl}" target="_blank" rel="noopener">Maps</a>
                </div>
                <div class="result-info-item">
                    <strong>📞 Contact</strong> ${escapeHtml(place.phone || 'N/A')}
                    ${phoneDigits ? `<a class="inline-chip" href="tel:${phoneDigits}">Call</a>` : ''}
                </div>
            </div>
            <button class="search-btn negotiate-btn" data-negotiate data-name="${escapeHtml(place.name)}">🤝 Negotiate for me</button>
        </div>`;
}


// ============================================================
// 3. NEGOTIATE: form -> LLM drafts first message -> human approves
// ============================================================

function openNegotiationModal(placeName) {
    selectedPlace = placeName;
    $('negotiationTarget').textContent = `Target: ${placeName}`;
    $('negotiationModal').hidden = false;
}

function closeNegotiationModal() {
    $('negotiationModal').hidden = true;
}

async function launchNegotiation() {
    const goal = $('negotiationGoal').value.trim();
    const price = $('targetPrice').value;
    if (!goal && !price) {
        $('negotiationGoal').focus();
        return;
    }
    closeNegotiationModal();

    setStep(6);
    addLog('info', `🤝 Starting negotiation with <b>${escapeHtml(selectedPlace)}</b>${price ? ` (target ₹${escapeHtml(price)})` : ''}`);
    setThinking('LLM is drafting the first message...');
    $('workflowContainer').scrollIntoView({ behavior: 'smooth' });

    try {
        const data = await api('/agent/negotiate/start', {
            thread_id: threadId,
            place_name: selectedPlace,
            target_price: price ? parseFloat(price) : null,
            goal: goal || null,
        });
        setThinking(null);
        showApprovalPanel(data);
    } catch (error) {
        setThinking(null);
        addLog('error', `❌ Could not start negotiation: ${escapeHtml(error.message)}`);
    }
}

function showApprovalPanel(data) {
    $('approvalTarget').textContent = data.phone
        ? `${data.place.name} (${data.phone})`
        : `${data.place.name} — ⚠️ no phone number! Set DEFAULT_TARGET_NUMBER in .env or the message will fail.`;
    $('approvalReasoning').textContent = data.reasoning;
    $('approvalMessage').value = data.planned_message;
    $('approvalPanel').hidden = false;
    $('approvalPanel').scrollIntoView({ behavior: 'smooth', block: 'center' });
}

async function approveFirstMessage() {
    $('approvalPanel').hidden = true;
    setThinking('Sending message...');
    try {
        const data = await api('/agent/approve', {
            thread_id: threadId,
            approved: true,
            edited_message: $('approvalMessage').value,
        });
        setThinking(null);
        if (data.status !== 'waiting_for_reply') {
            addLog('error', '❌ The message could not be sent. Check the Twilio settings and the server log.');
            return;
        }
        addLog('success', `📤 message sent to ${escapeHtml(data.place_name)}`);
        openChat(data.place_name, data.sent_message);
    } catch (error) {
        setThinking(null);
        addLog('error', `❌ ${escapeHtml(error.message)}`);
    }
}

async function abortNegotiation() {
    $('approvalPanel').hidden = true;
    addLog('warning', '❌ Negotiation cancelled — nothing was sent.');
    await api('/agent/approve', { thread_id: threadId, approved: false }).catch(() => {});
}


// ============================================================
// 4. CHAT: wait for replies, approve the agent's answers
// ============================================================

function openChat(placeName, firstMessage) {
    $('chatPlaceName').textContent = placeName;
    $('chatMessages').innerHTML = '';
    $('chatPanel').hidden = false;
    setChatEnabled(true);
    addChatMessage('agent', firstMessage);
    startPolling();
}

function addChatMessage(sender, text) {
    const isAgent = sender === 'agent';
    const bubble = document.createElement('div');
    bubble.className = `chat-message ${isAgent ? 'sent' : 'received'}`;
    bubble.innerHTML = `
        <div class="chat-sender">${isAgent ? '🤖 Your Agent' : '🏪 Shopkeeper'}</div>
        <div class="chat-bubble">
            <div class="chat-bubble-text">${escapeHtml(text)}</div>
            <div class="chat-bubble-meta"><span class="chat-bubble-time">${timeNow()}</span></div>
        </div>`;
    $('chatMessages').appendChild(bubble);
    $('chatMessages').scrollTop = $('chatMessages').scrollHeight;
}

function setChatStatus(text) {
    $('chatStatus').textContent = text;
}

function setChatEnabled(enabled) {
    $('chatInput').disabled = !enabled;
    $('chatSendButton').disabled = !enabled;
    $('endButton').disabled = !enabled;
}

function startPolling() {
    stopPolling();
    setChatStatus(`Waiting for reply... (checking every ${POLL_EVERY_MS / 1000} s)`);
    pollTimer = setInterval(checkForReply, POLL_EVERY_MS);
    setTimeout(checkForReply, 3000);
}

function stopPolling() {
    clearInterval(pollTimer);
    pollTimer = null;
}

async function checkForReply() {
    if (!pollTimer) return;   // polling was stopped meanwhile
    let data;
    try {
        data = await api(`/agent/check-reply?thread_id=${threadId}`);
    } catch (error) {
        setChatStatus('⚠️ Connection error, retrying...');
        return;
    }

    if (!data.has_reply) {
        if (data.status === 'waiting_for_reply') {
            setChatStatus(`Waiting for reply... (last check ${new Date().toLocaleTimeString()})`);
        } else {
            stopPolling();
        }
        return;
    }

    addChatMessage('shopkeeper', data.reply_text);
    addLog('info', `📩 Shopkeeper replied: "${escapeHtml(data.reply_text)}"`);
    stopPolling();

    if (data.status === 'continue') {
        setChatStatus('Reply received — review the agent\'s answer below');
        showChatDraft(data.planned_message, data.reasoning);
    } else if (data.status === 'deal_reached') {
        setChatStatus('🎉 Deal reached! You can still type a message.');
        addLog('success', '🎉 The agent thinks you have a deal!');
    } else if (data.status === 'rejected') {
        setChatStatus('The shop declined. You can still type a message.');
        addLog('warning', '🙁 The shop declined.');
    }
}

function showChatDraft(message, reasoning) {
    $('chatDraftMessage').value = message;
    $('chatDraftReasoning').textContent = reasoning ? `💭 ${reasoning}` : '';
    $('chatDraft').hidden = false;
}

async function sendChatDraft() {
    $('chatDraft').hidden = true;
    try {
        const data = await api('/agent/approve', {
            thread_id: threadId,
            approved: true,
            edited_message: $('chatDraftMessage').value,
        });
        if (data.status === 'waiting_for_reply') {
            addChatMessage('agent', data.sent_message);
            startPolling();
        } else {
            setChatStatus('❌ message could not be sent');
        }
    } catch (error) {
        setChatStatus(`❌ ${error.message}`);
    }
}

async function skipChatDraft() {
    $('chatDraft').hidden = true;
    await api('/agent/approve', { thread_id: threadId, approved: false }).catch(() => {});
    startPolling();
}

async function sendManualMessage() {
    const message = $('chatInput').value.trim();
    if (!message) return;
    $('chatInput').value = '';
    $('chatDraft').hidden = true;

    try {
        const data = await api('/agent/send-message', { thread_id: threadId, message });
        if (data.success) {
            addChatMessage('agent', message);
            startPolling();
        } else {
            setChatStatus('❌ message could not be sent');
        }
    } catch (error) {
        setChatStatus(`❌ ${error.message}`);
    }
}

async function endNegotiation() {
    if (!confirm('End this negotiation?')) return;
    stopPolling();
    $('chatDraft').hidden = true;
    setChatEnabled(false);
    $('chatSubtitle').textContent = '❌ Negotiation ended';
    setChatStatus('Negotiation ended');
    addLog('warning', '🛑 Negotiation ended by you');
    await api('/agent/terminate', { thread_id: threadId }).catch(() => {});
}


// ============================================================
// Session badge in the header
// ============================================================

$('sessionIdText').textContent = SESSION_ID;
$('sessionBadge').addEventListener('click', async () => {
    try {
        await navigator.clipboard.writeText(SESSION_ID);
        $('sessionCopyLabel').textContent = 'copied ✓';
    } catch {
        $('sessionCopyLabel').textContent = 'select & copy';
    }
    setTimeout(() => { $('sessionCopyLabel').textContent = 'click to copy'; }, 1500);
});
