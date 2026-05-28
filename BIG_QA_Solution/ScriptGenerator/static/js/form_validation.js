'use strict';

/**
 * Shared form-validation helpers for every modal/form in the app.
 *
 * ────────────────────────────────────────────────────────────────────────────
 * CONVENTIONS
 * ────────────────────────────────────────────────────────────────────────────
 *   Each input has an id (e.g. "bsProjectName") and OPTIONALLY a sibling
 *   <small class="error-text" id="bsProjectNameError"></small> that will hold
 *   the inline error message. The CSS class names .is-invalid (on the input)
 *   and .show (on the error-text span) render the red border / red message.
 *   Those rules live alongside the rest of the page styles — see the
 *   "Bootstrapper modal: inline form validation" block in
 *   templates/script_developer.html for the canonical declarations.
 *
 *   Why this split (mechanics here, schema in the page):
 *     - Mechanics (red border, message swap, regex matching) are identical
 *       across every form, so they live in one place to keep look-and-feel
 *       consistent and shrink the per-page JS.
 *     - The schema (which fields are required, what regex to enforce, what
 *       the message says) is specific to each modal and lives next to that
 *       modal's submit handler so it stays discoverable for whoever is
 *       editing the form.
 *
 * ────────────────────────────────────────────────────────────────────────────
 * USAGE
 * ────────────────────────────────────────────────────────────────────────────
 *   // In your submit handler:
 *   const ok = validateForm(
 *     {
 *       myProjectName: { required: true, message: 'Project Name is required.' },
 *       myUrl: { pattern: /^https?:\/\/\S+/i, message: 'Use http:// or https://' }
 *     },
 *     { myProjectName: nameInput.value, myUrl: urlInput.value }
 *   );
 *   if (!ok) return;
 *
 *   // Clear a single field's error reactively (e.g. on input):
 *   clearFieldError('myProjectName');
 *
 *   // Clear several at once (e.g. when re-opening the modal):
 *   clearFieldErrors(['myProjectName', 'myUrl']);
 */


/**
 * Mark a single input as invalid: red border on the field, message under it.
 * Silently no-ops if the input or its error span isn't in the DOM, so callers
 * don't need defensive checks.
 *
 * @param {string} inputId  DOM id of the input element.
 * @param {string} message  Human-readable error to display under the input.
 */
function setFieldError(inputId, message) {
    var input = document.getElementById(inputId);
    if (input) {
        input.classList.add('is-invalid');
    }
    var err = document.getElementById(inputId + 'Error');
    if (err) {
        err.textContent = message;
        err.classList.add('show');
    }
}


/**
 * Reverse of setFieldError — clears red border and empties the error span.
 * Use this on the input's `oninput`/`onchange` so the error vanishes as soon
 * as the user starts fixing the field, rather than waiting for another submit.
 *
 * @param {string} inputId
 */
function clearFieldError(inputId) {
    var input = document.getElementById(inputId);
    if (input) {
        input.classList.remove('is-invalid');
    }
    var err = document.getElementById(inputId + 'Error');
    if (err) {
        err.textContent = '';
        err.classList.remove('show');
    }
}


/**
 * Bulk version of clearFieldError — useful when re-opening a modal or resetting
 * a form to a clean state.
 *
 * @param {Array<string>} inputIds
 */
function clearFieldErrors(inputIds) {
    if (!inputIds) return;
    inputIds.forEach(clearFieldError);
}


/**
 * Run a declarative validation pass over a set of fields.
 *
 * The caller provides a schema (the rules) and a values map (the current input
 * values, keyed by input id). For every invalid field, setFieldError() is
 * applied automatically. Valid fields get clearFieldError() so any leftover
 * red state from a previous submit is wiped.
 *
 * Rule shape (per field):
 *   {
 *     required: boolean,    // empty/whitespace-only value is rejected
 *     pattern : RegExp,     // value must match (only enforced when value is non-empty)
 *     message : string      // shown to the user; a generic fallback is used if omitted
 *   }
 *
 * Rules are checked in this order per field: required → pattern. The first failure
 * sets the message and skips later checks (so you don't see "required" and "bad format"
 * stacked on the same field).
 *
 * @param {Object<string, Object>} schema  fieldId -> rule
 * @param {Object<string, string>} values  fieldId -> current value
 * @returns {boolean} true if every field passes
 */
function validateForm(schema, values) {
    if (!schema) return true;
    values = values || {};

    var allValid = true;
    // Remember the FIRST failing field so we can focus + scroll to it after the loop.
    // We don't focus inside the loop because Object.keys() iteration order matches the
    // declaration order, so the first failure we record is the topmost form field —
    // which is the one the user expects to see jumped to.
    var firstInvalidId = null;

    Object.keys(schema).forEach(function (inputId) {
        var rule = schema[inputId] || {};
        var raw = values[inputId];
        var value = (typeof raw === 'string') ? raw.trim() : (raw == null ? '' : String(raw));

        // Reset any prior error so a previously-invalid-now-fixed field clears.
        clearFieldError(inputId);

        if (rule.required && !value) {
            setFieldError(inputId, rule.message || 'This field is required.');
            allValid = false;
            if (firstInvalidId === null) firstInvalidId = inputId;
            return;
        }
        // pattern is only enforced when the user actually typed something — that lets us
        // mark a field "optional but must be a valid URL when present" cleanly.
        if (rule.pattern && value && !rule.pattern.test(value)) {
            setFieldError(inputId, rule.message || 'Invalid value.');
            allValid = false;
            if (firstInvalidId === null) firstInvalidId = inputId;
        }
    });

    if (firstInvalidId !== null) {
        focusInvalidField(firstInvalidId);
    }
    return allValid;
}


/**
 * Focus + scroll the given input into view. Used by validateForm() after it finds the first
 * failing field, so the user immediately sees what they need to fix even on long forms where
 * the offending field might be off-screen.
 *
 * Notes:
 *   - For readonly inputs (e.g. the Save Location field that is only set via a Browse button),
 *     focus() still draws the browser's focus ring, so the visual cue is preserved even though
 *     the user can't type into it.
 *   - scrollIntoView({ block: 'center' }) centres the field rather than aligning it to the
 *     top — important inside a scrollable modal body where "top" would put the field flush
 *     against the modal header and clip the surrounding context.
 *   - Wrapped in try/catch because preventScroll (Safari) and smooth-scroll behaviours have
 *     historically varied across browsers; we never want a focus call to abort a submit
 *     handler.
 *
 * @param {string} inputId
 */
function focusInvalidField(inputId) {
    var input = document.getElementById(inputId);
    if (!input) return;
    try {
        // Centre the field first so the focus ring appears in a useful place, then focus so
        // the user sees the cursor inside it (when editable) or the focus ring (when readonly).
        if (typeof input.scrollIntoView === 'function') {
            input.scrollIntoView({ block: 'center', behavior: 'smooth' });
        }
        input.focus({ preventScroll: true });
    } catch (e) {
        // Older browsers may reject the options object — fall back to plain focus().
        try { input.focus(); } catch (_) { /* swallow */ }
    }
}
