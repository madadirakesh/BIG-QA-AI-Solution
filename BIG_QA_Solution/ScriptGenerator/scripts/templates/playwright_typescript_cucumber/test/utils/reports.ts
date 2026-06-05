const reporter = require('cucumber-html-reporter');
const fs = require('fs');
const path = require('path');

let resultDir = process.env.RESULT_DIR;
if (!resultDir) {
    // Find the latest timestamped folder
    const resultsPath = path.join(process.cwd(), 'results');
    const folders = fs.readdirSync(resultsPath).filter(f => fs.statSync(path.join(resultsPath, f)).isDirectory());
    folders.sort((a, b) => fs.statSync(path.join(resultsPath, b)).mtime - fs.statSync(path.join(resultsPath, a)).mtime);
    resultDir = folders.length > 0 ? path.join('results', folders[0]) : 'results';
}

const jsonPath = path.join(process.cwd(), resultDir, 'cucumber_report.json');

// ARCHITECTURAL CHECK: Prevent crash if tests didn't run
if (!fs.existsSync(jsonPath)) {
    console.error("\u274c Execution Error: The JSON report was not generated. Check your test logs above!");
    process.exit(0); // Exit cleanly so you can see the actual error
}

const options = {
    theme: 'bootstrap',
    jsonFile: jsonPath,
    output: path.join(process.cwd(), resultDir, 'cucumber_report.html'),
    reportSuiteAsScenarios: true,
    scenarioTimestamp: true,
    launchReport: true
};

reporter.generate(options);