/*
 * payload_handler.groovy  -  shared payload loader for JSR223 PreProcessors.
 *
 * Parameter : path of the payload relative to the Payloads/ folder, e.g. json/create_user.json
 * Sets vars : payload_body          -> file content with ${var} placeholders replaced
 *             payload_content_type  -> application/json | application/xml | text/csv
 *
 * Placeholders are filled from JMeter variables (for example CSV Data Set columns).
 * Values are escaped for the payload type (JSON string escaping, XML entity escaping).
 */
import groovy.json.JsonOutput
import groovy.json.JsonSlurper
import javax.xml.parsers.DocumentBuilderFactory
import java.nio.charset.StandardCharsets
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.Paths
import java.util.regex.Matcher
import java.util.regex.Pattern

String rel = (args != null && args.length > 0) ? args[0] : vars.get('payload.file')
if (!rel) {
    throw new IllegalArgumentException('payload_handler: no payload path given (set the script parameter)')
}

String base = props.getProperty('payloads.dir', 'Payloads')
Path path = Paths.get(base, rel)
if (!Files.exists(path)) {
    throw new FileNotFoundException("Payload not found: ${path.toAbsolutePath()}")
}

String ext = rel.substring(rel.lastIndexOf('.') + 1).toLowerCase()
Map<String, String> contentTypes = [json: 'application/json', xml: 'application/xml', csv: 'text/csv']
String contentType = contentTypes[ext]
if (contentType == null) {
    throw new IllegalArgumentException("Unsupported payload type '.${ext}'. Use .json, .xml or .csv")
}

def escape = { String s ->
    if (ext == 'json') {
        String j = JsonOutput.toJson(s)
        return j.substring(1, j.length() - 1)
    }
    if (ext == 'xml') {
        return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')
    }
    return s
}

String raw = new String(Files.readAllBytes(path), StandardCharsets.UTF_8)
Matcher m = Pattern.compile('\\$\\{([A-Za-z0-9_.]+)\\}').matcher(raw)
StringBuffer sb = new StringBuffer()
while (m.find()) {
    String value = vars.get(m.group(1))
    String replacement = (value != null) ? escape(value) : m.group(0)   // leave unknown placeholders untouched
    m.appendReplacement(sb, Matcher.quoteReplacement(replacement))
}
m.appendTail(sb)
String body = sb.toString()

// Fail fast if the substituted payload is not well formed
if (ext == 'json') {
    new JsonSlurper().parseText(body)
} else if (ext == 'xml') {
    DocumentBuilderFactory f = DocumentBuilderFactory.newInstance()
    f.setFeature('http://apache.org/xml/features/disallow-doctype-decl', true)
    f.newDocumentBuilder().parse(new ByteArrayInputStream(body.getBytes(StandardCharsets.UTF_8)))
}

vars.put('payload_body', body)
vars.put('payload_content_type', contentType)
log.debug("Loaded payload ${rel} (${body.length()} chars, ${contentType})")
