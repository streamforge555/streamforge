// ==UserScript==
// @name         Streamforge Tiny2 User Camera Control TEST
// @namespace    https://github.com/streamforge555
// @version      0.4.6
// @description  Scene 4かつNOWがTiny2カメラ操作権のリク主だけ、通常コメントの完全一致コマンドでTiny2を操作
// @match        https://stripchat.com/*
// @match        https://*.stripchat.com/*
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// ==/UserScript==

(() => {
    'use strict';

    /* ========================================================
     * 01. 基本設定
     * ======================================================== */
    const VERSION = '0.4.6';
    const SERVER = 'http://127.0.0.1:8765';
    const TOKEN = 'sf-tiny2-test-20260828';
    const COOLDOWN_MS = 700;
    const REQUEST_BUTTON_ID = 'sc-request-popup-button-v610';
    const MESSAGE_SELECTOR =
        '[data-message-id].message.regular-message.regular-public-message';
    const CONTROL_REQUEST_NAME = '🎥 Tiny2カメラ操作権';
    const REQUEST_STORAGE_KEY_PREFIX = 'stripchat-request-manager-v637-session';
    const SHARED_QUEUE_KEY_PREFIX = 'streamforge-stripchat-now-next-v1';

    const COMMANDS = Object.freeze({
        'c←': 'left',
        'c→': 'right',
        'c↑': 'up',
        'c↓': 'down',
        'c+': 'zoom_in',
        'c-': 'zoom_out'
    });

    /* ========================================================
     * 02. 状態
     * ======================================================== */
    const seenMessages = new Map();
    const requesterUserIdCache = new Map();
    let lastCommandTime = 0;
    let enabled = false;
    let desiredEnabled = false;
    let ready = false;
    let serverOk = false;
    let pendingStateActions = 0;
    let stateQueue = Promise.resolve();
    let toggleButton = null;
    let emergencyButton = null;
    let controlWrap = null;
    let activeBanner = null;

    /* ========================================================
     * 03. localhost通信
     * ======================================================== */
    function request(path, { quiet = false } = {}) {
        return new Promise((resolve) => {
            GM_xmlhttpRequest({
                method: 'GET',
                url: `${SERVER}${path}${path.includes('?') ? '&' : '?'}token=${encodeURIComponent(TOKEN)}`,
                timeout: 1200,
                onload: (response) => {
                    serverOk = response.status >= 200 && response.status < 500;
                    updateUi();
                    if (!quiet && response.status >= 400) {
                        console.warn('[Tiny2] HTTP', response.status, response.responseText);
                    }
                    resolve(response);
                },
                onerror: () => {
                    serverOk = false;
                    updateUi();
                    if (!quiet) console.warn('[Tiny2] Windows側controllerが起動していません');
                    resolve(null);
                },
                ontimeout: () => {
                    serverOk = false;
                    updateUi();
                    if (!quiet) console.warn('[Tiny2] localhost timeout');
                    resolve(null);
                }
            });
        });
    }

    function parseJsonResponse(response) {
        if (!response || response.status < 200 || response.status >= 300) return null;
        try {
            return JSON.parse(response.responseText || '{}');
        } catch (_) {
            return null;
        }
    }

    function applyServerStatus(payload, { allowStateUpdate = true } = {}) {
        if (!payload || typeof payload !== 'object') return;
        serverOk = true;
        if (allowStateUpdate && typeof payload.enabled === 'boolean') {
            enabled = payload.enabled;
            if (pendingStateActions === 0) desiredEnabled = enabled;
        }
        updateUi();
    }

    async function syncStatus({ quiet = true } = {}) {
        const response = await request('/status', { quiet });
        const payload = parseJsonResponse(response);
        if (payload) {
            applyServerStatus(payload, { allowStateUpdate: pendingStateActions === 0 });
            return true;
        }

        // controllerが終了・切断された時はブラウザ側を安全側でOFF表示へ。
        if (pendingStateActions === 0) {
            enabled = false;
            desiredEnabled = false;
            updateUi();
        }
        return false;
    }

    /* ========================================================
     * 04. StripChat通常コメント限定判定
     * ======================================================== */
    function normalizeCommandText(text) {
        return String(text || '')
            .replace(/\u00a0/g, ' ')
            .trim();
    }

    function extractCommentText(message) {
        if (!(message instanceof HTMLElement) || !message.matches(MESSAGE_SELECTOR)) {
            return '';
        }

        const body = message.querySelector('.message-body');
        if (!body) return '';

        const clone = body.cloneNode(true);
        for (const removable of clone.querySelectorAll([
            'button',
            '.message-more-menu',
            '.mute-button',
            '.message-username',
            '.username',
            '.username-userlevels',
            '[class*="message-username"]',
            '[class*="user-levels-username"]'
        ].join(','))) {
            removable.remove();
        }

        return normalizeCommandText(clone.textContent || '');
    }

    function messageFingerprint(message, text) {
        const messageId = String(message.getAttribute('data-message-id') || '').trim();
        if (!messageId || !text) return '';
        return `${messageId}|${text}`;
    }

    function rememberFingerprint(fingerprint) {
        if (!fingerprint) return false;
        if (seenMessages.has(fingerprint)) return false;

        seenMessages.set(fingerprint, Date.now());
        if (seenMessages.size > 2500) {
            const removeCount = seenMessages.size - 1800;
            let removed = 0;
            for (const key of seenMessages.keys()) {
                seenMessages.delete(key);
                removed += 1;
                if (removed >= removeCount) break;
            }
        }
        return true;
    }

    function normalizeIdentityName(value) {
        return String(value || '')
            .replace(/\u00a0/g, ' ')
            .trim()
            .replace(/^@/, '');
    }

    function getPageKey() {
        const pathname = decodeURIComponent(location.pathname || '/')
            .replace(/\/{2,}/g, '/')
            .replace(/\/$/, '') || '/';
        return pathname.toLowerCase();
    }

    function createRequestStorageKey() {
        return [
            REQUEST_STORAGE_KEY_PREFIX,
            encodeURIComponent(getPageKey())
        ].join(':');
    }

    function createSharedQueueKey() {
        return [
            SHARED_QUEUE_KEY_PREFIX,
            encodeURIComponent(getPageKey())
        ].join(':');
    }

    function loadNowControlRequest() {
        try {
            // リク管理が実際に表示へ使っているNOWスナップショットを正とする。
            // requestStoreの単純先頭を自前計算しないため、非表示条件や並び順とズレない。
            const queueRaw = localStorage.getItem(createSharedQueueKey());
            if (!queueRaw) return null;

            const queuePayload = JSON.parse(queueRaw);
            if (!queuePayload || !queuePayload.now) return null;
            if (String(queuePayload.pageKey || '').toLowerCase() !== getPageKey()) return null;

            const nowSnapshot = queuePayload.now;
            const nowId = String(nowSnapshot.id || '').trim();
            const nowText = normalizeCommandText(nowSnapshot.text || '');
            if (!nowId || nowText !== CONTROL_REQUEST_NAME) return null;

            // NOWスナップショットにはusernameが含まれないため、同じIDの保存リクから取得する。
            const raw = sessionStorage.getItem(createRequestStorageKey());
            if (!raw) return null;

            const payload = JSON.parse(raw);
            if (!payload || !Array.isArray(payload.requests)) return null;
            if (String(payload.pageKey || '').toLowerCase() !== getPageKey()) return null;

            const nowItem = payload.requests.find((item) => {
                return item && String(item.id || '') === nowId;
            }) || null;
            if (!nowItem || nowItem.completed === true) return null;

            const requestText = normalizeCommandText(nowItem.request || '');
            if (requestText !== CONTROL_REQUEST_NAME) return null;

            const username = normalizeIdentityName(nowItem.username || '');
            if (!username) return null;

            const storedUserId = /^\d+$/.test(String(nowItem.userId || ''))
                ? String(nowItem.userId)
                : '';

            return {
                requestId: nowId,
                username,
                usernameKey: username.toLowerCase(),
                storedUserId
            };
        } catch (error) {
            console.warn('[Tiny2] リク管理NOW読込失敗', error);
            return null;
        }
    }

    function extractCommentIdentity(message) {
        if (!(message instanceof HTMLElement) || !message.matches(MESSAGE_SELECTOR)) {
            return null;
        }

        const usernameElement = message.querySelector(
            '.user-levels-username-text, [id^="user-levels-name-"]'
        );
        if (!usernameElement) return null;

        const username = normalizeIdentityName(
            usernameElement.innerText || usernameElement.textContent || ''
        );
        if (!username) return null;

        const idText = String(usernameElement.id || '');
        const idMatch = idText.match(/^user-levels-name-(\d+)(?:-|$)/);
        const directUserId =
            usernameElement.getAttribute('data-user-id') ||
            usernameElement.getAttribute('data-userid') ||
            '';
        const userId = idMatch
            ? idMatch[1]
            : (/^\d+$/.test(String(directUserId)) ? String(directUserId) : '');

        return {
            username,
            usernameKey: username.toLowerCase(),
            userId
        };
    }

    function isAuthorizedRequester(message) {
        const nowRequest = loadNowControlRequest();
        if (!nowRequest) {
            console.log('[Tiny2] BLOCK: NOWはTiny2カメラ操作権ではありません');
            return false;
        }

        const identity = extractCommentIdentity(message);
        if (!identity || !identity.userId) {
            console.log('[Tiny2] BLOCK: コメント送信者userIdを取得できません');
            return false;
        }

        if (identity.usernameKey !== nowRequest.usernameKey) {
            console.log('[Tiny2] BLOCK: リク主以外', identity.username);
            return false;
        }

        if (
            nowRequest.storedUserId &&
            identity.userId !== nowRequest.storedUserId
        ) {
            console.log('[Tiny2] BLOCK: リク保存userId不一致', identity.userId);
            return false;
        }

        const cachedUserId = requesterUserIdCache.get(nowRequest.requestId) || '';
        if (cachedUserId && cachedUserId !== identity.userId) {
            console.log('[Tiny2] BLOCK: リク主userId不一致', identity.userId);
            return false;
        }

        if (!cachedUserId) {
            requesterUserIdCache.set(nowRequest.requestId, identity.userId);
            if (requesterUserIdCache.size > 200) {
                const firstKey = requesterUserIdCache.keys().next().value;
                if (firstKey) requesterUserIdCache.delete(firstKey);
            }
        }

        return true;
    }

    function sendCommand(command, rawText, messageId) {
        if (!enabled || !ready) return;

        const now = Date.now();
        if (now - lastCommandTime < COOLDOWN_MS) {
            console.log('[Tiny2] local cooldown:', command);
            return;
        }

        lastCommandTime = now;
        request(
            `/cmd?name=${encodeURIComponent(command)}` +
            `&source=stripchat&message_id=${encodeURIComponent(messageId || '')}`
        );
        console.log('[Tiny2] SEND:', command, rawText, messageId);
    }

    function processMessage(message) {
        if (!(message instanceof HTMLElement) || !message.matches(MESSAGE_SELECTOR)) return;

        const text = extractCommentText(message);
        const fingerprint = messageFingerprint(message, text);
        if (!fingerprint || !rememberFingerprint(fingerprint)) return;

        // OFF中やREADY前に来たコメントも「既読」にする。
        // 後からONにした時に過去コマンドが実行されるのを防ぐ。
        if (!ready || !enabled) return;

        const command = COMMANDS[text];
        if (!command) return;

        // Scene 4のONだけでは操作不可。
        // NOW先頭が「🎥 Tiny2カメラ操作権」で、そのリク主本人のコメントだけ通す。
        if (!isAuthorizedRequester(message)) return;

        sendCommand(command, text, message.getAttribute('data-message-id'));
    }

    function collectMessagesFromMutation(mutation) {
        const messages = new Set();

        if (mutation.type === 'attributes') {
            const target = mutation.target;
            if (target instanceof HTMLElement) {
                if (target.matches(MESSAGE_SELECTOR)) messages.add(target);
                const closest = target.closest(MESSAGE_SELECTOR);
                if (closest) messages.add(closest);
            }
        }

        if (mutation.type === 'characterData') {
            const parent = mutation.target?.parentElement;
            const closest = parent?.closest?.(MESSAGE_SELECTOR);
            if (closest) messages.add(closest);
        }

        for (const node of mutation.addedNodes || []) {
            if (node.nodeType === Node.TEXT_NODE) {
                const closest = node.parentElement?.closest?.(MESSAGE_SELECTOR);
                if (closest) messages.add(closest);
                continue;
            }
            if (!(node instanceof HTMLElement)) continue;
            if (node.matches(MESSAGE_SELECTOR)) messages.add(node);
            const closest = node.closest(MESSAGE_SELECTOR);
            if (closest) messages.add(closest);
            for (const message of node.querySelectorAll(MESSAGE_SELECTOR)) {
                messages.add(message);
            }
        }

        return messages;
    }

    /* ========================================================
     * 05. ON / OFF / 強制停止 / 状態直列化
     * ======================================================== */
    function enqueueStateAction(action) {
        pendingStateActions += 1;
        stateQueue = stateQueue
            .then(action)
            .catch((error) => {
                console.warn('[Tiny2] state action error', error);
            })
            .finally(() => {
                pendingStateActions = Math.max(0, pendingStateActions - 1);
                if (pendingStateActions === 0) desiredEnabled = enabled;
                updateUi();
            });
        return stateQueue;
    }

    function setControlEnabled(nextEnabled) {
        desiredEnabled = Boolean(nextEnabled);
        updateUi();

        return enqueueStateAction(async () => {
            const requested = Boolean(nextEnabled);
            const response = await request(
                `/state?enabled=${requested ? '1' : '0'}` +
                `&switch_scene=1&source=stripchat-ui`
            );
            const payload = parseJsonResponse(response);

            if (!payload) {
                enabled = false;
                desiredEnabled = false;
                updateUi();
                console.warn('[Tiny2] state sync failed -> local OFF');
                return false;
            }

            applyServerStatus(payload, { allowStateUpdate: true });
            console.log('[Tiny2]', enabled ? 'ON / Scene 4' : 'OFF / Scene 1');
            return true;
        });
    }

    function emergencyStop() {
        desiredEnabled = false;
        enabled = false;
        updateUi();

        return enqueueStateAction(async () => {
            const response = await request('/emergency?source=stripchat-ui');
            const payload = parseJsonResponse(response);
            if (payload) applyServerStatus(payload, { allowStateUpdate: true });
            else {
                enabled = false;
                desiredEnabled = false;
                updateUi();
            }
            console.warn('[Tiny2] EMERGENCY STOP / explicit ON can restart');
        });
    }

    /* ========================================================
     * 06. リク管理ボタン追従UI
     * ======================================================== */
    function positionControls() {
        if (!controlWrap) return;

        const requestButton = document.getElementById(REQUEST_BUTTON_ID);
        if (requestButton && requestButton.offsetParent !== null) {
            const rect = requestButton.getBoundingClientRect();
            const width = controlWrap.offsetWidth || 122;
            const idealLeft = rect.right + 8;
            const left = Math.max(8, Math.min(idealLeft, window.innerWidth - width - 8));

            controlWrap.style.left = `${Math.round(left)}px`;
            controlWrap.style.top = `${Math.round(Math.max(8, rect.top))}px`;
            controlWrap.style.right = 'auto';
            controlWrap.style.bottom = 'auto';
            controlWrap.dataset.anchor = 'request-manager';
            return;
        }

        controlWrap.style.left = 'auto';
        controlWrap.style.top = 'auto';
        controlWrap.style.right = '16px';
        controlWrap.style.bottom = '16px';
        controlWrap.dataset.anchor = 'fallback';
    }

    function createControls() {
        if (document.getElementById('streamforge-tiny2-controls')) return;

        controlWrap = document.createElement('div');
        controlWrap.id = 'streamforge-tiny2-controls';
        Object.assign(controlWrap.style, {
            position: 'fixed',
            zIndex: '2147483647',
            width: '122px',
            display: 'flex',
            flexDirection: 'column',
            gap: '5px',
            fontFamily: 'Meiryo, sans-serif'
        });

        toggleButton = document.createElement('button');
        toggleButton.id = 'streamforge-tiny2-toggle';
        Object.assign(toggleButton.style, {
            width: '122px',
            minHeight: '38px',
            padding: '7px 8px',
            borderRadius: '8px',
            border: '2px solid #777',
            background: '#171717',
            color: '#fff',
            fontSize: '12px',
            fontWeight: '700',
            fontFamily: 'Meiryo, sans-serif',
            cursor: 'pointer',
            boxShadow: '0 4px 14px rgba(0,0,0,.45)',
            whiteSpace: 'nowrap'
        });
        toggleButton.addEventListener('click', () => {
            setControlEnabled(!desiredEnabled);
        });

        emergencyButton = document.createElement('button');
        emergencyButton.id = 'streamforge-tiny2-emergency';
        emergencyButton.textContent = '■ 強制停止';
        Object.assign(emergencyButton.style, {
            width: '122px',
            minHeight: '31px',
            padding: '5px 8px',
            borderRadius: '8px',
            border: '2px solid #fff',
            background: '#c62828',
            color: '#fff',
            fontSize: '12px',
            fontWeight: '900',
            fontFamily: 'Meiryo, sans-serif',
            cursor: 'pointer',
            boxShadow: '0 4px 14px rgba(0,0,0,.45)'
        });
        emergencyButton.addEventListener('click', emergencyStop);

        controlWrap.append(toggleButton, emergencyButton);
        document.body.appendChild(controlWrap);
        positionControls();
        updateUi();
    }

    /* ========================================================
     * 07. キャスト画面中央の状態表示
     * ======================================================== */
    function createActiveBanner() {
        if (document.getElementById('streamforge-tiny2-active-banner')) return;

        activeBanner = document.createElement('div');
        activeBanner.id = 'streamforge-tiny2-active-banner';
        activeBanner.textContent = 'ユーザーTiny2コントロール中';
        Object.assign(activeBanner.style, {
            position: 'fixed',
            left: '50%',
            top: '50%',
            transform: 'translate(-50%, -50%)',
            zIndex: '2147483646',
            display: 'none',
            pointerEvents: 'none',
            padding: '14px 22px',
            borderRadius: '12px',
            border: '3px solid #ff3b30',
            background: 'rgba(0,0,0,.78)',
            color: '#fff',
            fontSize: '28px',
            fontWeight: '900',
            fontFamily: 'Meiryo, sans-serif',
            letterSpacing: '.04em',
            textAlign: 'center',
            whiteSpace: 'nowrap',
            boxShadow: '0 6px 28px rgba(0,0,0,.55)'
        });
        document.body.appendChild(activeBanner);
    }

    function updateUi() {
        if (toggleButton) {
            const dot = serverOk ? '●' : '×';
            const displayState = pendingStateActions > 0 ? desiredEnabled : enabled;
            toggleButton.textContent = displayState
                ? `🎥 Tiny2 ON ${dot}`
                : `🎥 Tiny2 OFF ${dot}`;
            toggleButton.style.borderColor = displayState
                ? '#37e56b'
                : (serverOk ? '#777' : '#e65050');
            toggleButton.style.boxShadow = displayState
                ? '0 0 14px rgba(55,229,107,.55)'
                : '0 4px 14px rgba(0,0,0,.45)';
            toggleButton.title = serverOk
                ? `Windows controller connected / v${VERSION}`
                : `tiny2_camera_v${VERSION}.py not detected`;
        }

        if (activeBanner) activeBanner.style.display = enabled ? 'block' : 'none';
        if (emergencyButton) emergencyButton.disabled = !enabled && pendingStateActions === 0;
    }

    /* ========================================================
     * 08. 新着DOM監視 / DOM再利用対策
     * ======================================================== */
    const observer = new MutationObserver((mutations) => {
        const messages = new Set();
        for (const mutation of mutations) {
            for (const message of collectMessagesFromMutation(mutation)) {
                messages.add(message);
            }
        }
        for (const message of messages) processMessage(message);
    });

    /* ========================================================
     * 09. 起動時・手動シーンキーとの同期
     * ======================================================== */
    async function forceInitialOff() {
        // ページ再読込でブラウザ表示だけOFFになる穴を防ぐ。
        // シーン自体は勝手に変えず、Windows側の操作許可だけ必ずOFFへ同期する。
        const response = await request(
            '/state?enabled=0&switch_scene=0&source=stripchat-load',
            { quiet: true }
        );
        const payload = parseJsonResponse(response);
        if (payload) applyServerStatus(payload, { allowStateUpdate: true });
        else {
            enabled = false;
            desiredEnabled = false;
            updateUi();
        }
    }

    function bestEffortPageHideOff() {
        try {
            GM_xmlhttpRequest({
                method: 'GET',
                url: `${SERVER}/state?enabled=0&switch_scene=0&source=stripchat-pagehide&token=${encodeURIComponent(TOKEN)}`,
                timeout: 500
            });
        } catch (_) {
            // unload中は送信できない場合がある。次回ロード時にも必ずOFF同期する。
        }
    }

    async function start() {
        createActiveBanner();
        createControls();

        // 先に監視を開始し、その直後に既存DOMを既読化する。
        // この順番なら起動直前に到着したコメントも取りこぼしにくく、
        // observer側ではfingerprint重複防止が働く。
        observer.observe(document.body, {
            childList: true,
            subtree: true,
            characterData: true,
            attributes: true,
            attributeFilter: ['data-message-id']
        });

        // 既に表示されている過去コメントは全て既読にする。
        for (const message of document.querySelectorAll(MESSAGE_SELECTOR)) {
            const text = extractCommentText(message);
            rememberFingerprint(messageFingerprint(message, text));
        }

        window.addEventListener('resize', positionControls, { passive: true });
        window.addEventListener('scroll', positionControls, { passive: true });
        window.addEventListener('pagehide', bestEffortPageHideOff, { capture: true });
        window.addEventListener('beforeunload', bestEffortPageHideOff, { capture: true });

        window.setInterval(positionControls, 700);

        await forceInitialOff();
        await syncStatus({ quiet: true });

        // Windows側は上段1/2/3/4を監視する。
        // 手動4でON、手動1/2/3でOFFになった状態をUIへ反映する。
        window.setInterval(() => {
            if (pendingStateActions === 0) syncStatus({ quiet: true });
        }, 500);

        ready = true;
        console.log(`[Tiny2 v${VERSION}] READY / Windows-side initial OFF synced / requester-only enabled`);
        console.log('Commands (strict): c← c→ c↑ c↓ c+ c- / authorized NOW requester only');
        console.log('Scenes: 1=Tiny3 OFF / 2=Tiny2 OFF / 3=MEET OFF / 4=Tiny2+Guide ON');
    }

    if (document.body) start();
    else window.addEventListener('DOMContentLoaded', start, { once: true });
})();
