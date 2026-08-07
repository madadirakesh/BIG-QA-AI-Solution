const reporter = require('cucumber-html-reporter');
const fs = require('fs');
const path = require('path');

let resultDir = process.env.RESULT_DIR;
if (!resultDir) {
    // Find the latest timestamped folder that actually contains a JSON report.
    const resultsPath = process.env.RESULTS_ROOT || path.join(process.cwd(), 'Results');
    const folders = fs.readdirSync(resultsPath)
        .filter(f => fs.statSync(path.join(resultsPath, f)).isDirectory())
        .sort((a, b) => fs.statSync(path.join(resultsPath, b)).mtimeMs - fs.statSync(path.join(resultsPath, a)).mtimeMs);
    const folderWithJson = folders.find((folder) =>
        fs.existsSync(path.join(resultsPath, folder, 'cucumber_report.json'))
    );
    resultDir = folderWithJson ? path.join(resultsPath, folderWithJson) : resultsPath;
}

const jsonPath = path.join(resultDir, 'cucumber_report.json');

// ARCHITECTURAL CHECK: Prevent crash if tests didn't run
if (!fs.existsSync(jsonPath)) {
    console.error("\u274c Execution Error: The JSON report was not generated. Check your test logs above!");
    process.exit(0); // Exit cleanly so you can see the actual error
}

const options = {
    theme: 'bootstrap',
    jsonFile: jsonPath,
    output: path.join(resultDir, 'cucumber_report.html'),
    reportSuiteAsScenarios: true,
    scenarioTimestamp: true,
    launchReport: true
};

reporter.generate(options);
