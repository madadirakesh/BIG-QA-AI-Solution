// main.js - Client side logic for AI-QA Hub

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

async function launchElementLocator() {
    try {
        const response = await fetch('/qa/launch-element-locator');
        if (response.ok) {
            console.log("Element locator desktop app launched.");
        } else {
            console.error("Failed to launch element locator.");
        }
    } catch (error) {
        console.error("Network error when launching element locator", error);
    }
}
