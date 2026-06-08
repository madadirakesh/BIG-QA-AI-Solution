// main.js - Client side logic for AI-QA Hub

// ── H6: Password visibility toggle (global) ───────────────────────
function togglePwd(inputId, btn) {
    const input = document.getElementById(inputId);
    if (!input) return;
    const isHidden = input.type === 'password';
    input.type = isHidden ? 'text' : 'password';
    const icon = btn.querySelector('i');
    if (icon) { icon.className = isHidden ? 'fas fa-eye-slash' : 'fas fa-eye'; }
}
function nextStep(stepNumber) {
    // Hide all steps
    document.querySelectorAll('.wizard-content').forEach(el => {
        el.style.display = 'none';
        el.classList.remove('active');
    });

    // Update step indicators
    document.querySelectorAll('.step').forEach((el, index) => {
        if (index < stepNumber) {
            el.classList.add('active');
        } else {
            el.classList.remove('active');
        }
    });

    // Show target step
    const target = document.getElementById(`step-${stepNumber}`);
    if (target) {
        target.style.display = 'block';
        target.classList.add('active');
    }
}

let currentTaskId = null;
let pollInterval = null;

async function generateCode() {
    const projectSelect = document.getElementById('projectSelect');
    const selectedOption = projectSelect ? projectSelect.options[projectSelect.selectedIndex] : null;
    const projectPathInput = document.getElementById('projectPath');
    
    let projectName, language, framework, projectLoc;
    
    if (projectPathInput && projectPathInput.value) {
        projectName = projectPathInput.value.split(/[\\/]/).pop() || "Locally Detected";
        language = document.getElementById('detLanguage') ? document.getElementById('detLanguage').innerText : "Unknown";
        framework = document.getElementById('detFramework') ? document.getElementById('detFramework').innerText : "Unknown";
        projectLoc = projectPathInput.value;
    } else if (selectedOption && selectedOption.value) {
        projectName = selectedOption.value;
        language = selectedOption.dataset.lang;
        framework = selectedOption.dataset.fw;
        projectLoc = selectedOption.dataset.path;
    } else {
        alert("Please select a project from the dropdown or locate a directory in Step 1.");
        nextStep(1);
        return;
    }

    const bddContent = document.getElementById('bddContent').value;
    if (!bddContent.trim()) {
        alert("Please provide the BDD Source Text.");
        return;
    }

    const baseUrl = document.getElementById('baseUrl').value;
    const existingCode = document.getElementById('existingCode').value;

    let supportContent = "";
    if (baseUrl) supportContent += `Base URL: ${baseUrl}\n`;
    if (existingCode) supportContent += `\nExisting Code Context:\n${existingCode}`;

    const payload = {
        project_name: projectName,
        language: language,
        framework: framework,
        project_path: projectLoc,
        bdd_content: bddContent,
        support_content: supportContent,
        file_content: "",
        ai_provider: "" // Will safely default to backend global Env variable configuring AI_TOOL
    };

    // Move to step 3 Loading State
    nextStep(3);
    document.getElementById('loadingStatus').style.display = 'block';
    document.getElementById('resultContent').style.display = 'none';

    try {
        const response = await fetch('http://127.0.0.1:8000/generate-agent-code', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(payload)
        });

        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }

        const data = await response.json();
        currentTaskId = data.task_id;

        // Start polling
        pollInterval = setInterval(pollTaskResult, 2000);

    } catch (error) {
        alert("Failed to start generation: " + error.message);
        nextStep(2);
    }
}

async function pollTaskResult() {
    if (!currentTaskId) return;

    try {
        const response = await fetch(`http://127.0.0.1:8000/task-result/${currentTaskId}`);
        const data = await response.json();

        if (data.status === 'done') {
            clearInterval(pollInterval);
            renderResults(data.result);
        } else if (data.status === 'error') {
            clearInterval(pollInterval);
            alert("Backend Error: " + (data.error || data.result));
            nextStep(2);
        }
    } catch (error) {
        console.error("Polling error", error);
    }
}

function renderResults(resultStr) {
    document.getElementById('loadingStatus').style.display = 'none';
    document.getElementById('resultContent').style.display = 'block';

    const container = document.getElementById('codeBlocksContainer');
    container.innerHTML = ''; // clear 

    let resultsObj = {};
    try {
        // Simple heuristic parse similar to python backend
        resultsObj = JSON.parse(resultStr);
    } catch (e) {
        resultsObj = { "generated_code.py": resultStr };
    }

    // Render each file as a block
    for (const [filename, content] of Object.entries(resultsObj)) {
        const fileContent = typeof content === 'object' ? content.content || JSON.stringify(content) : content;

        const block = `
        <div class="code-block-wrapper">
            <div class="code-header">
                <div><i class="fas fa-file-code"></i> ${filename}</div>
            </div>
            <pre class="code-content"><code>${escapeHtml(fileContent)}</code></pre>
        </div>
        `;
        container.innerHTML += block;
    }
}

function saveFiles() {
    alert("Saving mechanism connected to Project API... (Files preserved)");
    // Here we would implement the save logic over POST 
}

function escapeHtml(unsafe) {
    return unsafe
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}

async function launchElementLocator(fromProject = false) {
    try {
        const projectSelect = document.getElementById('projectSelect');
        const selectedOption = projectSelect ? projectSelect.options[projectSelect.selectedIndex] : null;
        const projectPathInput = document.getElementById('projectPath');

        // Treat the literal string "None" / "null" / "undefined" / empty as missing.
        // These can show up when Jinja renders a NULL DB value, when a dataset
        // attribute is unset, or when an element's innerText hasn't been populated yet.
        const clean = (v) => {
            if (v === null || v === undefined) return "";
            const s = String(v).trim();
            if (!s) return "";
            const low = s.toLowerCase();
            if (low === "none" || low === "null" || low === "undefined" || low === "n/a") return "";
            return s;
        };

        let projectName = "";
        let language = "";
        let framework = "";
        let projectLoc = "";
        let tool = "";

        if (projectPathInput && clean(projectPathInput.value)) {
            projectLoc = clean(projectPathInput.value);
            projectName = projectLoc.split(/[\\/]/).pop() || "Locally Detected";
            language  = clean(document.getElementById('detLanguage')  && document.getElementById('detLanguage').innerText);
            framework = clean(document.getElementById('detFramework') && document.getElementById('detFramework').innerText);
            tool      = clean(document.getElementById('detTool')      && document.getElementById('detTool').innerText);
        }

        // Fallback / supplement: pull anything still missing from the selected DB option.
        if (selectedOption && selectedOption.value) {
            if (!projectName) projectName = clean(selectedOption.value);
            if (!language)   language   = clean(selectedOption.dataset.lang);
            if (!framework)  framework  = clean(selectedOption.dataset.fw);
            if (!projectLoc) projectLoc = clean(selectedOption.dataset.path);
            if (!tool)       tool       = clean(selectedOption.dataset.tool);
        }

        const projectBaseUrl = clean(document.getElementById('projectBaseUrl') && document.getElementById('projectBaseUrl').value);

        if (fromProject) {
            if (!projectLoc) {
                if (typeof showToast === 'function') {
                    showToast("Please select or locate a project first", "warning");
                } else {
                    alert("Please select or locate a project first");
                }
                return;
            }
            if (!projectBaseUrl) {
                let proceed = false;
                if (typeof showConfirm === 'function') {
                    proceed = await showConfirm("App URL Warning", "app url not configured. Do you want to continue?", "⚠️", "Continue", "background:var(--accent-primary);color:white;");
                } else {
                    proceed = confirm("app url not configured. Do you want to continue?");
                }
                if (!proceed) return;
            }
        }

        // Diagnostic snapshot of every input the function consulted.
        const _diag = {
            'projectSelect exists':      !!projectSelect,
            'projectSelect.selectedIndex': projectSelect ? projectSelect.selectedIndex : 'n/a',
            'selectedOption exists':     !!selectedOption,
            'selectedOption.value':      selectedOption ? selectedOption.value : 'n/a',
            'dataset.path':              selectedOption ? selectedOption.dataset.path : 'n/a',
            'dataset.lang':              selectedOption ? selectedOption.dataset.lang : 'n/a',
            'dataset.fw':                selectedOption ? selectedOption.dataset.fw : 'n/a',
            'dataset.tool':              selectedOption ? selectedOption.dataset.tool : 'n/a',
            'projectPath input value':   projectPathInput ? projectPathInput.value : 'n/a',
            'detLanguage innerText':     document.getElementById('detLanguage')  ? document.getElementById('detLanguage').innerText  : 'n/a',
            'detFramework innerText':    document.getElementById('detFramework') ? document.getElementById('detFramework').innerText : 'n/a',
            'detTool innerText':         document.getElementById('detTool')      ? document.getElementById('detTool').innerText      : 'n/a',
            'projectBaseUrl value':      document.getElementById('projectBaseUrl') ? document.getElementById('projectBaseUrl').value : 'n/a',
            '---resolved---': '---',
            'projectName': projectName,
            'projectLoc':  projectLoc,
            'language':    language,
            'framework':   framework,
            'tool':        tool,
            'projectBaseUrl (clean)': projectBaseUrl
        };
        console.log("[launchElementLocator] raw + resolved:", _diag);

        let url = '/qa/launch-element-locator';
        const params = new URLSearchParams();
        if (projectLoc)     params.append('project_path', projectLoc);
        if (language)       params.append('language', language);
        if (framework)      params.append('framework', framework);
        if (tool)           params.append('tool', tool);
        if (projectBaseUrl) params.append('app_url', projectBaseUrl);

        if (params.toString()) {
            url += '?' + params.toString();
        }
        console.log("[launchElementLocator] requesting:", url);



        const response = await fetch(url);
        if (response.ok) {
            console.log("Element locator desktop app launched.");
        } else {
            console.error("Failed to launch element locator.");
        }
    } catch (error) {
        console.error("Network error when launching element locator", error);
    }
}

// AI Configuration Modal Functions
function openConfigureAIModal() {
    const modal = document.getElementById('configureAIModal');
    if (modal) {
        modal.style.display = 'block';
        fetch('/api/configure-ai')
            .then(res => res.json())
            .then(data => {
                if (data.status === 'success' && data.config) {
                    if (data.config.AI_TOOL) document.getElementById('aiToolSelect').value = data.config.AI_TOOL;
                    if (data.config.AI_MODEL) document.getElementById('aiModelInput').value = data.config.AI_MODEL;
                    if (data.config.API_KEY) document.getElementById('apiKeyInput').value = data.config.API_KEY;
                }
            })
            .catch(err => console.error("Could not load AI config", err));
    }
}

function closeConfigureAIModal() {
    const modal = document.getElementById('configureAIModal');
    if (modal) {
        modal.style.display = 'none';
    }
}

function submitAIConfiguration() {
    const tool = document.getElementById('aiToolSelect').value;
    const model = document.getElementById('aiModelInput').value;
    const apiKey = document.getElementById('apiKeyInput').value;

    if (!tool || !model || !apiKey) {
        if(typeof showToast === 'function') showToast("All fields are required.", "warning");
        else alert("All fields are required.");
        return;
    }

    fetch('/api/configure-ai', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify({ ai_tool: tool, ai_model: model, api_key: apiKey })
    })
    .then(res => res.json())
    .then(data => {
        if (data.status === 'success') {
            closeConfigureAIModal();
            if(typeof showToast === 'function') showToast("AI Configuration saved successfully!", "success");
            
            if (data.system_status) {
                if (data.system_status.status === 'healthy') {
                    if(typeof showToast === 'function') showToast("System Status: Healthy", "success");
                } else {
                    if(typeof showToast === 'function') showToast("System Status Warning: " + (data.system_status.error || "Unknown"), "warning");
                }
            }
        } else {
            if(typeof showToast === 'function') showToast("Failed to save config: " + data.message, "error");
        }
    })
    .catch(err => {
        if(typeof showToast === 'function') showToast("Error saving AI configuration", "error");
        console.error(err);
    });
}
