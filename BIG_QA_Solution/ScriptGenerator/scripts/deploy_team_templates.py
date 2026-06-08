import sqlite3
import os
import json

# Standalone Seeder Script generated for the BIG-QA Team
# Run this to populate your local database with standardized project templates.
#
# All templates now live on disk under the `templates/` directory next to this file, where each
# subdirectory is one template stored as real source files (pom.xml, *.java, *.ts, *.feature,
# requirements.txt, etc.) plus a metadata.json. This is the place to add or edit templates —
# real files keep IDE syntax highlighting and avoid Python string-escape pain.
#
# Two sources are still merged at runtime for flexibility:
#   1. The `templates/` directory (preferred — every template lives here now).
#   2. TEMPLATES_DATA below (inline Python list — currently empty; available for a quick one-off).
#
# Both sources are deduplicated by (tool, language, framework). Files-on-disk templates win on
# tie, so a directory with matching metadata overrides an inline entry.

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "local_database.db")
TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")

TEMPLATES_DATA = []  # Intentionally empty. All templates now live on disk under
#                      templates/<name>/ (see the module header). This list remains so the
#                      merge logic below keeps working and a future quick one-off can still
#                      be added inline if ever needed.


def _load_filesystem_templates(templates_dir):
    """
    Walks ``templates_dir`` and converts each subdirectory into the same
    ``{"metadata": {...}, "files": [...]}`` shape used by the inline TEMPLATES_DATA list.

    Layout convention for each template directory:
      templates/<some_name>/
        metadata.json                  -- tool, language, framework, description, default_run_commands
        <any file tree>                -- ingested verbatim; file paths become relative to this dir.

    Skipped entries:
      - metadata.json itself (it is parsed into the metadata block, not stored as a file).
      - Editor / OS noise: .DS_Store, __pycache__, .pyc.

    Binary detection: files are read as bytes and decoded as UTF-8; if decoding fails we mark
    is_binary=1 and store an empty content string (matches the existing inline contract — see
    BootstrapperEngine.generate_project, which currently no-ops on is_binary files anyway).
    """
    discovered = []
    if not os.path.isdir(templates_dir):
        return discovered

    for entry in sorted(os.listdir(templates_dir)):
        tpl_root = os.path.join(templates_dir, entry)
        if not os.path.isdir(tpl_root):
            continue

        meta_path = os.path.join(tpl_root, "metadata.json")
        if not os.path.isfile(meta_path):
            print(f"  ! Skipping '{entry}': missing metadata.json")
            continue

        with open(meta_path, "r", encoding="utf-8") as f:
            raw_meta = json.load(f)
        # Drop any leading-underscore explanatory keys (e.g. "_comment") so they do not leak
        # into the ProjectTemplates row.
        metadata = {k: v for k, v in raw_meta.items() if not k.startswith("_")}

        files = []
        for dirpath, _dirnames, filenames in os.walk(tpl_root):
            for fname in filenames:
                if fname in ("metadata.json", ".DS_Store") or fname.endswith(".pyc"):
                    continue
                if "__pycache__" in dirpath:
                    continue

                abs_path = os.path.join(dirpath, fname)
                # Store paths relative to the template root, using forward slashes so the
                # scaffolded project layout is identical on Windows and Unix.
                rel_path = os.path.relpath(abs_path, tpl_root).replace(os.sep, "/")

                try:
                    with open(abs_path, "r", encoding="utf-8") as f:
                        content = f.read()
                    is_binary = 0
                except UnicodeDecodeError:
                    content = ""
                    is_binary = 1

                files.append({
                    "file_path": rel_path,
                    "file_content": content,
                    "is_binary": is_binary,
                })

        # Stable file order so re-seeds produce identical DB state.
        files.sort(key=lambda f: f["file_path"])
        discovered.append({"metadata": metadata, "files": files})
        print(f"  + Loaded template from disk: {entry} -> "
              f"{metadata.get('tool')}/{metadata.get('language')}/{metadata.get('framework')} "
              f"({len(files)} files)")

    return discovered


def _merge_templates(inline, from_disk):
    """
    Combines the two template sources. Disk templates win on (tool, language, framework) tie
    so an on-disk override silently replaces the inline definition.
    """
    by_key = {}
    for tpl in inline:
        m = tpl["metadata"]
        by_key[(m["tool"], m["language"], m["framework"])] = tpl
    for tpl in from_disk:
        m = tpl["metadata"]
        by_key[(m["tool"], m["language"], m["framework"])] = tpl
    return list(by_key.values())


def seed():
    print(f"Connecting to database at: {DB_PATH}")
    # Pull in any filesystem-backed templates before opening the DB connection so failures
    # while reading the disk tree are reported up-front without leaving a stale transaction.
    disk_templates = _load_filesystem_templates(TEMPLATES_DIR)
    templates = _merge_templates(TEMPLATES_DATA, disk_templates)

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ProjectTemplates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tool TEXT NOT NULL,
                language TEXT NOT NULL,
                framework TEXT NOT NULL,
                description TEXT,
                default_run_commands TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS TemplateFiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                template_id INTEGER,
                file_path TEXT NOT NULL,
                file_content TEXT,
                is_binary INTEGER DEFAULT 0,
                FOREIGN KEY (template_id) REFERENCES ProjectTemplates (id)
            )
        """)

        print("Seeding templates...")
        # Iterate the merged list (inline + on-disk). See _merge_templates above.
        for t in templates:
            meta = t["metadata"]
            cursor.execute(
                "SELECT id FROM ProjectTemplates WHERE tool=? AND language=? AND framework=?",
                (meta["tool"], meta["language"], meta["framework"])
            )
            exists = cursor.fetchone()
            if exists:
                template_id = exists[0]
                print(f"  - Template {meta['tool']}/{meta['language']} exists. Updating metadata...")
                cursor.execute(
                    "UPDATE ProjectTemplates SET description=?, default_run_commands=? WHERE id=?",
                    (meta["description"], meta.get("default_run_commands", ""), template_id)
                )
                # Refresh files for existing templates
                cursor.execute("DELETE FROM TemplateFiles WHERE template_id=?", (template_id,))
                for f in t["files"]:
                    cursor.execute(
                        "INSERT INTO TemplateFiles (template_id, file_path, file_content, is_binary) VALUES (?, ?, ?, ?)",
                        (template_id, f["file_path"], f["file_content"], f["is_binary"])
                    )
                continue

            cursor.execute(
                "INSERT INTO ProjectTemplates (tool, language, framework, description, default_run_commands) VALUES (?, ?, ?, ?, ?)",
                (meta["tool"], meta["language"], meta["framework"], meta["description"], meta.get("default_run_commands", ""))
            )
            new_id = cursor.lastrowid

            print(f"  + Ingesting {meta['tool']}/{meta['language']} (ID: {new_id})")
            for f in t["files"]:
                cursor.execute(
                    "INSERT INTO TemplateFiles (template_id, file_path, file_content, is_binary) VALUES (?, ?, ?, ?)",
                    (new_id, f["file_path"], f["file_content"], f["is_binary"])
                )

        conn.commit()
        print("\nSuccess: Database seeded with team templates.")
    except Exception as e:
        conn.rollback()
        print(f"\nError during seeding: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    seed()