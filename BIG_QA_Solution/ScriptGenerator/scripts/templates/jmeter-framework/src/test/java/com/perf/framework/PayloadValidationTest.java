package com.perf.framework;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.apache.commons.csv.CSVFormat;
import org.apache.commons.csv.CSVParser;
import org.apache.commons.csv.CSVRecord;
import org.junit.jupiter.api.Assertions;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.MethodSource;

import javax.xml.XMLConstants;
import javax.xml.parsers.DocumentBuilderFactory;
import java.io.IOException;
import java.io.Reader;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.List;
import java.util.stream.Collectors;
import java.util.stream.Stream;

/**
 * Fails the build early if any file under Payloads/ is malformed,
 * so load tests never run against broken CSV, JSON or XML.
 */
class PayloadValidationTest {

    private static final Path PAYLOADS = Paths.get("Payloads");

    static Stream<Path> payloadFiles() throws IOException {
        try (Stream<Path> walk = Files.walk(PAYLOADS)) {
            List<Path> files = walk.filter(Files::isRegularFile)
                    .filter(p -> !p.getFileName().toString().startsWith("."))
                    .collect(Collectors.toList());
            return files.stream();
        }
    }

    @ParameterizedTest(name = "{0}")
    @MethodSource("payloadFiles")
    void payloadIsWellFormed(Path file) throws Exception {
        String name = file.getFileName().toString().toLowerCase();
        if (name.endsWith(".json")) {
            Assertions.assertNotNull(new ObjectMapper().readTree(file.toFile()));
        } else if (name.endsWith(".xml")) {
            DocumentBuilderFactory f = DocumentBuilderFactory.newInstance();
            f.setFeature(XMLConstants.FEATURE_SECURE_PROCESSING, true);
            f.setFeature("http://apache.org/xml/features/disallow-doctype-decl", true);
            Assertions.assertNotNull(f.newDocumentBuilder().parse(file.toFile()));
        } else if (name.endsWith(".csv")) {
            CSVFormat format = CSVFormat.DEFAULT.builder().setHeader().setSkipHeaderRecord(true).build();
            try (Reader in = Files.newBufferedReader(file, StandardCharsets.UTF_8);
                 CSVParser parser = format.parse(in)) {
                List<CSVRecord> records = parser.getRecords();
                Assertions.assertFalse(records.isEmpty(), "CSV has no data rows: " + file);
                for (CSVRecord r : records) {
                    Assertions.assertTrue(r.isConsistent(), "Inconsistent column count at line "
                            + r.getRecordNumber() + " in " + file);
                }
            }
        } else {
            Assertions.fail("Unsupported payload type (use .csv, .json, .xml): " + file);
        }
    }
}
