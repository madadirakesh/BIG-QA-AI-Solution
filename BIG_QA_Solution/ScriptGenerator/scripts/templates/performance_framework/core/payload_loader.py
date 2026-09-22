"""
payload_loader.py
------------------
Loads test data payloads from CSV, JSON, or XML files and hands them out to
Locust virtual users. Supports two selection strategies:

    - round_robin : each call advances to the next record (thread-safe)
    - random       : a random record is returned on each call

Usage inside a locustfile:

    from core.payload_loader import PayloadLoader

    loader = PayloadLoader("data/users.csv", strategy="round_robin")

    class MyUser(HttpUser):
        def on_start(self):
            self.record = loader.next()
"""

import csv
import json
import itertools
import random
import threading
import xml.etree.ElementTree as ET
from pathlib import Path

# Bumped whenever the loader's reading of a payload changes. The Payload
# Configuration dialog parses the file with the same rules to build its node
# dropdown, so utils/payload_parameterizer.ensure_payload_loader refreshes an
# older copy of this file in a project before generating a script against it.
LOADER_VERSION = 2


class PayloadLoader:
    def __init__(self, file_path, strategy="round_robin", xml_record_tag="record"):
        """
        file_path        : path to .csv, .json, or .xml file
        strategy          : "round_robin" or "random"
        xml_record_tag    : the repeating child tag name in an XML file that
                             represents a single record, e.g. <records><record>...</record></records>
        """
        self.file_path = Path(file_path)
        self.strategy = strategy
        self.xml_record_tag = xml_record_tag
        self._lock = threading.Lock()

        if not self.file_path.exists():
            raise FileNotFoundError(f"Payload file not found: {file_path}")

        ext = self.file_path.suffix.lower()
        if ext == ".csv":
            self.records = self._load_csv()
        elif ext == ".json":
            self.records = self._load_json()
        elif ext == ".xml":
            self.records = self._load_xml()
        else:
            raise ValueError(f"Unsupported payload format: {ext}. Use .csv, .json, or .xml")

        if not self.records:
            raise ValueError(f"No records found in payload file: {file_path}")

        self._cycle = itertools.cycle(self.records)

    def _load_csv(self):
        with open(self.file_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            return [row for row in reader]

    def _load_json(self):
        with open(self.file_path, encoding="utf-8") as f:
            data = json.load(f)
        # Accept either a top-level list of records, or {"records": [...]}
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "records" in data:
            return data["records"]
        if isinstance(data, dict):
            # A payload that wraps its records under a name of its own,
            # {"users": [{...}, {...}]}, is that list of records - not one
            # record whose single field is the whole list.
            wrapped = [value for value in data.values()
                       if isinstance(value, list) and value
                       and all(isinstance(item, dict) for item in value)]
            if len(wrapped) == 1:
                return wrapped[0]
        # Single object -> treat as one record
        return [data]

    def _load_xml(self):
        tree = ET.parse(self.file_path)
        root = tree.getroot()
        records = []
        for elem in root.findall(f".//{self.xml_record_tag}"):
            record = {}
            for child in elem:
                record[child.tag] = child.text
            # include attributes too, if any
            record.update(elem.attrib)
            records.append(record)
        return records

    def next(self):
        """Return the next record according to the configured strategy."""
        if self.strategy == "random":
            return random.choice(self.records)
        with self._lock:
            return next(self._cycle)

    def all(self):
        """Return every loaded record (read-only copy)."""
        return list(self.records)

    def __len__(self):
        return len(self.records)
