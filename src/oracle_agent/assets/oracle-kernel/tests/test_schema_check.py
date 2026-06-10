#!/usr/bin/env python3
"""Tests for schema_check.py -- the tiny stdlib JSON-Schema validator.

Every supported keyword (type/required/enum/properties/items/minimum/maximum/
pattern) is exercised with both a passing and a failing object.
"""
from __future__ import annotations

from schema_check import validate


# ---------------------------------------------------------------------------
# type
# ---------------------------------------------------------------------------

def test_type_string():
    assert validate("hi", {"type": "string"}) == []
    assert validate(5, {"type": "string"}) != []


def test_type_integer_excludes_bool():
    assert validate(7, {"type": "integer"}) == []
    # bool is a subclass of int but must NOT validate as integer/number.
    assert validate(True, {"type": "integer"}) != []
    assert validate(True, {"type": "number"}) != []
    assert validate(True, {"type": "boolean"}) == []


def test_type_number_allows_int_and_float():
    assert validate(3, {"type": "number"}) == []
    assert validate(3.5, {"type": "number"}) == []
    assert validate("3", {"type": "number"}) != []


def test_type_object_array_null():
    assert validate({}, {"type": "object"}) == []
    assert validate([], {"type": "array"}) == []
    assert validate(None, {"type": "null"}) == []
    assert validate([], {"type": "object"}) != []


def test_type_union_list():
    schema = {"type": ["string", "null"]}
    assert validate("x", schema) == []
    assert validate(None, schema) == []
    assert validate(5, schema) != []


# ---------------------------------------------------------------------------
# required
# ---------------------------------------------------------------------------

def test_required_present_and_missing():
    schema = {"type": "object", "required": ["id", "title"]}
    assert validate({"id": "a", "title": "t"}, schema) == []
    errs = validate({"id": "a"}, schema)
    assert errs and any("title" in e for e in errs)


# ---------------------------------------------------------------------------
# enum
# ---------------------------------------------------------------------------

def test_enum_pass_and_fail():
    schema = {"enum": ["public", "internal", "confidential", "restricted", "secret"]}
    assert validate("confidential", schema) == []
    assert validate("topsecret", schema) != []


# ---------------------------------------------------------------------------
# minimum / maximum
# ---------------------------------------------------------------------------

def test_minimum_maximum():
    schema = {"type": "number", "minimum": 0, "maximum": 1}
    assert validate(0, schema) == []
    assert validate(1, schema) == []
    assert validate(0.5, schema) == []
    assert validate(-0.1, schema) != []
    assert validate(1.5, schema) != []


def test_confidence_field_pattern_like_finding():
    schema = {"type": "number", "minimum": 0.0, "maximum": 1.0}
    assert validate(0.85, schema) == []
    assert validate(2.0, schema) != []


# ---------------------------------------------------------------------------
# pattern
# ---------------------------------------------------------------------------

def test_pattern_pass_and_fail():
    schema = {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"}
    assert validate("2026-06-08", schema) == []
    assert validate("June 8", schema) != []


def test_pattern_search_semantics():
    # search-match: a pattern without anchors matches anywhere.
    schema = {"type": "string", "pattern": r"abc"}
    assert validate("xxabcxx", schema) == []
    assert validate("xyz", schema) != []


# ---------------------------------------------------------------------------
# properties (nested) + items (arrays)
# ---------------------------------------------------------------------------

def test_properties_nested():
    schema = {
        "type": "object",
        "required": ["company"],
        "properties": {
            "company": {
                "type": "object",
                "required": ["name"],
                "properties": {"name": {"type": "string"}},
            }
        },
    }
    assert validate({"company": {"name": "Acme"}}, schema) == []
    errs = validate({"company": {"name": 5}}, schema)
    assert errs and any("company.name" in e for e in errs)
    errs2 = validate({"company": {}}, schema)
    assert errs2 and any("name" in e for e in errs2)


def test_items_array():
    schema = {"type": "array", "items": {"type": "string"}}
    assert validate(["a", "b", "c"], schema) == []
    errs = validate(["a", 2, "c"], schema)
    assert errs and any("[1]" in e for e in errs)


def test_items_enum_array():
    schema = {"type": "array", "items": {"enum": ["read_only", "read_write", "unknown"]}}
    assert validate(["read_only", "unknown"], schema) == []
    assert validate(["read_only", "bogus"], schema) != []


# ---------------------------------------------------------------------------
# A realistic compound schema (note frontmatter shape) round-trip.
# ---------------------------------------------------------------------------

def test_compound_frontmatter_like_schema():
    schema = {
        "type": "object",
        "required": ["id", "type", "title", "created", "updated", "sensitivity", "status", "tags"],
        "properties": {
            "id": {"type": "string"},
            "type": {"type": "string"},
            "title": {"type": "string"},
            "sensitivity": {
                "enum": ["public", "internal", "confidential", "restricted", "secret"]
            },
            "tags": {"type": "array", "items": {"type": "string"}},
        },
    }
    good = {
        "id": "src-001",
        "type": "source",
        "title": "Q3 deck",
        "created": "2026-06-08",
        "updated": "2026-06-08",
        "sensitivity": "confidential",
        "status": "active",
        "tags": ["finance", "q3"],
    }
    assert validate(good, schema) == []

    bad = dict(good)
    bad["sensitivity"] = "ultra"
    bad["tags"] = ["ok", 7]
    del bad["status"]
    errs = validate(bad, schema)
    assert any("sensitivity" in e for e in errs)
    assert any("status" in e for e in errs)
    assert any("tags[1]" in e for e in errs)
