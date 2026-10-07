const reporter = require("cucumber-html-reporter");
const fs = require("fs");
const path = require("path");

const resultsDir = path.join(__dirname, "results");
const jsonFile = path.join(resultsDir, "cucumber_report.json");
const htmlFile = path.join(resultsDir, "cucumber_report.html");

if (!fs.existsSync(resultsDir)) {
  fs.mkdirSync(resultsDir, { recursive: true });
}

if (!fs.existsSync(jsonFile)) {
  console.error("Cucumber JSON report was not found:");
  console.error(jsonFile);
  process.exit(1);
}

console.log("Generating Cucumber HTML report...");
console.log(`JSON: ${jsonFile}`);
console.log(`HTML: ${htmlFile}`);

try {
  reporter.generate({
    theme: "bootstrap",
    jsonFile: jsonFile,
    output: htmlFile,
    reportSuiteAsScenarios: true,
    scenarioTimestamp: true,
    launchReport: false,
    name: "Creatio Cucumber Test Report",
    brandTitle: "Creatio Automation",
    metadata: {
      "Test Environment": "Test",
      "Browser": "Chromium",
      "Framework": "Playwright",
      "Language": "TypeScript"
    }
  });

  if (fs.existsSync(htmlFile)) {
    console.log("");
    console.log("HTML report generated successfully:");
    console.log(htmlFile);
  } else {
    console.error("");
    console.error("HTML report was not created.");
    process.exit(1);
  }
} catch (error) {
  console.error("");
  console.error("Failed to generate HTML report:");
  console.error(error);
  process.exit(1);
}
