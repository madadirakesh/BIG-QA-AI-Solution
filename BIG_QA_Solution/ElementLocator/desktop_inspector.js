/* desktop_inspector.js */

// Initialize QWebChannel for communication with Python backend
if (typeof qt !== 'undefined' && typeof QWebChannel !== 'undefined') {
    new QWebChannel(qt.webChannelTransport, function (channel) {
        window.pybridge = channel.objects.pybridge;
        console.log("QWebChannel connected.");
    });
} else {
    console.error("QWebChannel or qt is not defined. The bridge will not work.");
}

// Only setup once per frame
if (!window.desktopInspectorInitialized) {
    window.desktopInspectorInitialized = true;
    window.desktopInspectorActive = false;
    window.currentHighlightedElement = null;

    let highlightBox = document.getElementById('pyqt-highlight-box');
    if (!highlightBox) {
        highlightBox = document.createElement('div');
        highlightBox.id = 'pyqt-highlight-box';
        highlightBox.style.setProperty('position', 'fixed', 'important');
        highlightBox.style.setProperty('border', '3px solid #ff0055', 'important');
        highlightBox.style.setProperty('background-color', 'rgba(255, 0, 85, 0.2)', 'important');
        highlightBox.style.setProperty('pointer-events', 'none', 'important');
        highlightBox.style.setProperty('z-index', '9999999', 'important');
        highlightBox.style.setProperty('display', 'none', 'important');
        highlightBox.style.setProperty('box-sizing', 'border-box', 'important');
        highlightBox.style.setProperty('margin', '0', 'important');
        (document.body || document.documentElement).appendChild(highlightBox);
    }

    window.activateDesktopInspector = function () {
        if (!window.desktopInspectorActive) {
            document.addEventListener('mouseover', handleMouseOver, true);
            document.addEventListener('mouseout', handleMouseOut, true);
            document.addEventListener('click', handleClick, true);
            window.desktopInspectorActive = true;
        }
    };

    window.deactivateDesktopInspector = function () {
        if (window.desktopInspectorActive) {
            document.removeEventListener('mouseover', handleMouseOver, true);
            document.removeEventListener('mouseout', handleMouseOut, true);
            document.removeEventListener('click', handleClick, true);
            highlightBox.style.setProperty('display', 'none', 'important');
            window.desktopInspectorActive = false;
        }
    };

    function handleMouseOver(e) {
        if (!window.desktopInspectorActive) return;
        const target = e.target;
        if (target === highlightBox) return;

        e.stopPropagation();
        window.currentHighlightedElement = target;

        const rect = target.getBoundingClientRect();
        highlightBox.style.setProperty('display', 'block', 'important');
        highlightBox.style.setProperty('top', rect.top + 'px', 'important');
        highlightBox.style.setProperty('left', rect.left + 'px', 'important');
        highlightBox.style.setProperty('width', rect.width + 'px', 'important');
        highlightBox.style.setProperty('height', rect.height + 'px', 'important');
    }

    function handleMouseOut(e) {
        if (!window.desktopInspectorActive) return;
        highlightBox.style.setProperty('display', 'none', 'important');
        window.currentHighlightedElement = null;
    }

    function handleClick(e) {
        if (!window.desktopInspectorActive) return;
        e.preventDefault();
        e.stopPropagation();

        const el = window.currentHighlightedElement || e.target;
        const locators = generateLocators(el);

        let textC = el.textContent ? el.textContent : '';
        const id = el.getAttribute('id');
        const name = el.getAttribute('name');
        let nameHint = id || name || textC.substring(0, 15).replace(/[^a-zA-Z0-9]/g, '');
        if (!nameHint) nameHint = (el.tagName || 'ELEM').toLowerCase();

        const outerHtml = (el.outerHTML || '').substring(0, 2000);
        const pTitle = document.title || 'MyPage';
        const payload = locators.map(l => ({ ...l, nameHint, outerHtml, pageTitle: pTitle }));

        console.log("Locators generated:", payload);

        // Send back to Python via pybridge
        if (window.pybridge && typeof window.pybridge.receive_payload === 'function') {
            window.pybridge.receive_payload(JSON.stringify(payload));
        } else {
            console.error("window.pybridge not found or receive_payload is not a function.");
        }
    }

    function generateLocators(el) {
        const list = [];
        const id = el.getAttribute('id');
        const name = el.getAttribute('name');
        const tagName = el.tagName.toLowerCase();

        // 1. ID
        if (id) {
            list.push({ type: 'ID', value: id });
            list.push({ type: 'CSS', value: `#${id}` });
            list.push({ type: 'XPath', value: `//${tagName}[@id='${id}']` });
        }

        // 2. Name
        if (name) {
            list.push({ type: 'Name', value: name });
            list.push({ type: 'CSS', value: `[name='${name}']` });
            list.push({ type: 'XPath', value: `//${tagName}[@name='${name}']` });
        }

        if (tagName) {
            list.push({ type: 'Tag Name', value: tagName });
        }

        const testId = el.getAttribute('data-testid');
        if (testId) {
            list.push({ type: 'Test ID', value: testId });
            list.push({ type: 'getByTestId', value: testId });
        }

        const className = el.getAttribute('class');
        if (className && typeof className === 'string') {
            const classes = className.split(/\s+/).filter(c => c.length > 0);
            if (classes.length > 0) {
                list.push({ type: 'CSS', value: `${tagName}.${classes.join('.')}` });
            }
        }

        // 3. Link text
        if (tagName === 'a' && el.textContent.trim().length > 0) {
            const t = el.textContent.trim();
            list.push({ type: 'Link Text', value: t });
            list.push({ type: 'Partial Link', value: t.substring(0, Math.min(15, t.length)) });
            list.push({ type: 'XPath', value: `//a[contains(text(), '${t}')]` });
        }

        // 4. Semantic bindings for Playwright
        const role = el.getAttribute('role');
        if (role) { list.push({ type: 'getByRole', value: role }); }

        const text = el.textContent.trim();
        if (text.length > 0 && text.length < 40) {
            list.push({ type: 'getByText', value: text });
        }

        const ariaLabel = el.getAttribute('aria-label');
        if (ariaLabel) { list.push({ type: 'getByLabel', value: ariaLabel }); }

        const placeholder = el.getAttribute('placeholder');
        if (placeholder) {
            list.push({ type: 'getByPlaceholder', value: placeholder });
            list.push({ type: 'XPath', value: `//${tagName}[@placeholder='${placeholder}']` });
        }

        const alt = el.getAttribute('alt');
        if (alt) { list.push({ type: 'getByAltText', value: alt }); }

        const title = el.getAttribute('title');
        if (title) { list.push({ type: 'getByTitle', value: title }); }

        // 5. Absolute / Smart Xpath Fallback
        list.push({ type: 'XPath', value: createAbsoluteXPath(el) });

        return list;
    }

    function createAbsoluteXPath(element) {
        if (!element || element.nodeType !== 1) return '';
        if (element.id) return `//*[@id="${element.id}"]`;
        const path = [];
        while (element && element.nodeType === 1) {
            let index = 1;
            let sibling = element.previousSibling;
            while (sibling) {
                if (sibling.nodeType === 1 && sibling.tagName === element.tagName) {
                    index++;
                }
                sibling = sibling.previousSibling;
            }
            const tagName = element.tagName.toLowerCase();
            path.unshift(`${tagName}[${index}]`);
            element = element.parentNode;
        }
        return path.length ? '/' + path.join('/') : '';
    }

    window.freezePage = function (duration) {
        console.log(`[Element Locator] Freezing page for ${duration / 1000}s`);

        const overlay = document.createElement('div');
        overlay.style.cssText = `
            position: fixed; top: 10px; right: 10px; padding: 10px 20px;
            background: #6366f1; color: white; border-radius: 8px;
            z-index: 1000000; font-family: sans-serif; font-weight: bold;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
            pointer-events: none;
        `;
        (document.body || document.documentElement).appendChild(overlay);

        let timeLeft = duration / 1000;
        overlay.innerText = `❄️ Frozen: ${timeLeft}s`;

        const timer = setInterval(() => {
            timeLeft--;
            overlay.innerText = `❄️ Frozen: ${timeLeft}s`;
            if (timeLeft <= 0) {
                clearInterval(timer);
                if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
            }
        }, 1000);

        const eventsToSuppress = ['blur', 'focusout', 'mouseleave'];
        const handler = (e) => {
            e.stopImmediatePropagation();
            e.preventDefault();
        };

        eventsToSuppress.forEach(evt => {
            window.addEventListener(evt, handler, true);
            document.addEventListener(evt, handler, true);
        });

        setTimeout(() => {
            eventsToSuppress.forEach(evt => {
                window.removeEventListener(evt, handler, true);
                document.removeEventListener(evt, handler, true);
            });
            console.log("[Element Locator] Page unfrozen");
        }, duration);
    };

    window.highlightElementByLocator = function (type, value) {
        let el = null;
        try {
            if (type === 'ID') {
                el = document.getElementById(value);
            } else if (type === 'Name') {
                el = document.getElementsByName(value)[0];
            } else if (type === 'CSS') {
                el = document.querySelector(value);
            } else if (type === 'Tag Name') {
                el = document.getElementsByTagName(value)[0];
            } else if (type === 'Link Text') {
                const anchors = Array.from(document.getElementsByTagName('a'));
                el = anchors.find(a => a.textContent.trim() === value);
            } else if (type === 'XPath') {
                el = document.evaluate(value, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
            } else if (type.startsWith('getBy')) {
                // Approximate for Playwright semantic locators
                if (type === 'getByTestId') el = document.querySelector(`[data-testid='${value}']`);
                else if (type === 'getByRole') el = document.querySelector(`[role='${value}']`);
                else if (type === 'getByPlaceholder') el = document.querySelector(`[placeholder='${value}']`);
                else if (type === 'getByLabel') {
                    const label = Array.from(document.getElementsByTagName('label')).find(l => l.textContent.trim() === value);
                    if (label && label.htmlFor) el = document.getElementById(label.htmlFor);
                } else if (type === 'getByText') {
                    el = Array.from(document.querySelectorAll('*')).find(n => n.children.length === 0 && n.textContent.trim() === value);
                }
            }

            if (el) {
                el.scrollIntoView({ behavior: 'smooth', block: 'center' });
                const rect = el.getBoundingClientRect();
                highlightBox.style.setProperty('display', 'block', 'important');
                highlightBox.style.setProperty('top', rect.top + 'px', 'important');
                highlightBox.style.setProperty('left', rect.left + 'px', 'important');
                highlightBox.style.setProperty('width', rect.width + 'px', 'important');
                highlightBox.style.setProperty('height', rect.height + 'px', 'important');
                highlightBox.style.setProperty('border', '4px solid #00ff00', 'important'); // Green for manual highlight
                highlightBox.style.setProperty('background-color', 'rgba(0, 255, 0, 0.1)', 'important');

                setTimeout(() => {
                    highlightBox.style.setProperty('border', '3px solid #ff0055', 'important');
                    highlightBox.style.setProperty('background-color', 'rgba(255, 0, 85, 0.2)', 'important');
                    if (!window.desktopInspectorActive) {
                        highlightBox.style.setProperty('display', 'none', 'important');
                    }
                }, 2000);
            } else {
                console.warn("[Element Locator] Element not found for highlight:", type, value);
            }
        } catch (e) {
            console.error("[Element Locator] Highlight error:", e);
        }
    };
}
