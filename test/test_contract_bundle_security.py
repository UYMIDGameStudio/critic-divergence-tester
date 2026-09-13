"""Exact-byte bundles reject stale objects, ambiguous JSON, and type substitution."""
import copy
import json
import unittest

import argument_contracts as contracts
from test.test_argument_contracts import base, encoded, parent


def project(title="Reviewed object"):
    return {**base("argument-project", "P1", "immutable", "human-confirmed"),
            "project_id": "P1", "title": title}


def legacy_bundle():
    value = project("English 简体 繁體 Straße français 日本語 Русский Latīna æ œ")
    # Preserve a UTF-8 BOM and arbitrary whitespace as part of the original
    # parent digest, while comparing parsed objects without reserializing links.
    project_bytes = b"\xef\xbb\xbf" + encoded(value)
    document = {**base("argument-document", "D1", "immutable", "human-confirmed", parents=[{
        "role": "project", "artifact": "argument-project", "sha256": contracts.sha256_bytes(project_bytes)}]),
        "project_id": "P1", "document_id": "D1", "title": value["title"]}
    version = {**base("document-version", "V1", "immutable", "human-confirmed", parents=[parent("document", "argument-document", document)]),
        "project_id": "P1", "document_id": "D1", "version_id": "V1", "parent_version": None,
        "source": {"name": "historical.md", "relative_path": "source/historical.md", "sha256": "a" * 64}}
    return [(value, project_bytes), (document, encoded(document)), (version, encoded(version))]


class ContractBundleSecurityTests(unittest.TestCase):
    def test_historical_utf8_bundle_keeps_exact_hashes_and_inputs(self):
        entries = legacy_bundle()
        before = copy.deepcopy(entries)
        self.assertEqual(contracts.validate_contract_bundle(entries), [])
        self.assertEqual(entries, before)
        # Repeated identical dependencies are legitimate when combining chains.
        self.assertEqual(contracts.validate_contract_bundle(entries + [entries[0]]), [])

    def test_supplied_object_is_bound_to_the_actual_original_bytes(self):
        value = project()
        variants = [encoded(project("Different source object")), b"not JSON", b"{}", b"[]", b"null"]
        for data in variants:
            with self.subTest(data=data[:50]):
                self.assertTrue(contracts.validate_contract_bundle([(value, data)]))

    def test_cross_artifact_bytes_cannot_impersonate_a_parent(self):
        value = project()
        foreign = {**base("argument-document", "D9", "immutable", "human-confirmed", parents=[{
            "role": "project", "artifact": "argument-project", "sha256": "a" * 64}]),
            "project_id": "P9", "document_id": "D9", "title": "A different artifact type"}
        foreign_bytes = encoded(foreign)
        child = {**base("argument-document", "D1", "immutable", "human-confirmed", parents=[{
            "role": "project", "artifact": "argument-project", "sha256": contracts.sha256_bytes(foreign_bytes)}]),
            "project_id": "P1", "document_id": "D1", "title": "Child"}
        errors = contracts.validate_contract_bundle([(value, foreign_bytes), (child, encoded(child))])
        self.assertTrue(any("does not match" in error for error in errors))
        self.assertTrue(any("not present" in error for error in errors))

    def test_duplicate_keys_at_root_and_inside_provenance_are_rejected(self):
        value = project()
        raw = encoded(value)
        variants = [raw.replace(b'"schema_version": 1', b'"schema_version": 0, "schema_version": 1'),
                    raw.replace(b'"producer": "test"', b'"producer": "other", "producer": "test"')]
        for data in variants:
            self.assertEqual(json.loads(data), value)
            self.assertTrue(contracts.validate_contract_bundle([(value, data)]))

    def test_nonfinite_numbers_overflow_and_invalid_unicode_are_rejected(self):
        value = project()
        raw = encoded(value)
        variants = [raw.replace(b'"schema_version": 1', b'"schema_version": ' + token)
                    for token in (b"NaN", b"Infinity", b"-Infinity", b"1e999")]
        variants.extend([b"\xff", json.dumps(value).encode("utf-16"),
                         raw.replace(b'"Reviewed object"', b'"\\ud800"'),
                         raw.replace(b'"Reviewed object"', b'"\\udfff"')])
        for data in variants:
            with self.subTest(data=data[:45]):
                self.assertTrue(contracts.validate_contract_bundle([(value, data)]))
        surrogate_value = project("\ud800")
        surrogate_bytes = json.dumps(surrogate_value, ensure_ascii=True).encode("utf-8")
        self.assertTrue(contracts.validate_contract_bundle([(surrogate_value, surrogate_bytes)]))

    def test_python_numeric_coercion_cannot_make_different_objects_equal(self):
        value = project()
        for token in (b"true", b"1.0"):
            data = encoded(value).replace(b'"schema_version": 1', b'"schema_version": ' + token)
            self.assertEqual(json.loads(data), value)  # Python's ordinary == is insufficient.
            errors = contracts.validate_contract_bundle([(value, data)])
            self.assertTrue(any("does not match" in error for error in errors))

    def test_nonbytes_and_malformed_entry_shapes_return_errors(self):
        value = project()
        for data in (None, "text", 1, bytearray(encoded(value)), memoryview(encoded(value))):
            with self.subTest(data=type(data).__name__):
                self.assertTrue(contracts.validate_contract_bundle([(value, data)]))
        for entries in (None, "not entries", {}, [None], [(value,)], [(value, b"{}", "extra")]):
            with self.subTest(entries=repr(entries)[:45]):
                self.assertTrue(contracts.validate_contract_bundle(entries))

    def test_invalid_action_is_not_used_by_the_acceptance_interlock(self):
        action = {**base("revision-action", "RA1", "immutable", "human-confirmed"),
                  "action_id": "RA1", "adjudication_id": "AD1", "target_claim": "V1:C1",
                  "action_type": "narrow_claim", "text": "A proposed change"}
        action["parents"] = None
        errors = contracts.validate_contract_bundle([(action, encoded(action))])
        self.assertTrue(errors)
        self.assertTrue(any("parents" in error for error in errors))

    def test_invalid_parent_artifact_cannot_satisfy_a_child_hash(self):
        entries = legacy_bundle()
        invalid_project = copy.deepcopy(entries[0][0])
        invalid_project["title"] = ""
        data = encoded(invalid_project)
        document = copy.deepcopy(entries[1][0])
        document["parents"][0]["sha256"] = contracts.sha256_bytes(data)
        errors = contracts.validate_contract_bundle([(invalid_project, data), (document, encoded(document))])
        self.assertTrue(any("title" in error for error in errors))
        self.assertTrue(any("not present" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
