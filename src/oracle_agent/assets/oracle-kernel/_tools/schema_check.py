#!/usr/bin/env python3
"""schema_check.py -- a tiny, dependency-free JSON-Schema validator.

The spawned kernel cannot pull in ``jsonschema``; this module provides exactly
the subset the linter and record schemas use:

    validate(obj, schema) -> list[str]   # [] means valid

Supported keywords:
    type        (string|number|integer|boolean|object|array|null, or a list)
    required    (list of property names that must be present on an object)
    enum        (value must be one of the listed values)
    properties  (per-key sub-schemas for object values)
    items       (sub-schema applied to every element of an array)
    minimum     (numeric lower bound, inclusive)
    maximum     (numeric upper bound, inclusive)
    pattern     (regex that a string value must search-match)

Errors are returned as human-readable strings prefixed with a JSON-pointer-ish
path so callers (oracle_lint) can report file:field locations.

Stdlib only.
"""
from __future__ import annotations

import re

__all__ = ["validate"]


_TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    # JSON 'number' includes ints but NOT bools (bool is a subclass of int).
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "null": lambda v: v is None,
}


def _type_matches(value, type_spec) -> bool:
    types = type_spec if isinstance(type_spec, list) else [type_spec]
    for t in types:
        check = _TYPE_CHECKS.get(t)
        if check is None:
            # Unknown type names are treated permissively (don't false-fail).
            return True
        if check(value):
            return True
    return False


def _validate(obj, schema: dict, path: str, errors: list) -> None:
    if not isinstance(schema, dict):
        return

    # type
    if "type" in schema:
        if not _type_matches(obj, schema["type"]):
            errors.append(
                f"{path or '<root>'}: expected type {schema['type']!r}, got {type(obj).__name__}"
            )
            # If the type is wrong, deeper keyword checks are meaningless.
            return

    # enum
    if "enum" in schema:
        if obj not in schema["enum"]:
            errors.append(f"{path or '<root>'}: {obj!r} not in enum {schema['enum']!r}")

    # numeric bounds (only meaningful for real numbers, not bools)
    if isinstance(obj, (int, float)) and not isinstance(obj, bool):
        if "minimum" in schema and obj < schema["minimum"]:
            errors.append(f"{path or '<root>'}: {obj} < minimum {schema['minimum']}")
        if "maximum" in schema and obj > schema["maximum"]:
            errors.append(f"{path or '<root>'}: {obj} > maximum {schema['maximum']}")

    # pattern (strings only)
    if "pattern" in schema and isinstance(obj, str):
        try:
            if re.search(schema["pattern"], obj) is None:
                errors.append(
                    f"{path or '<root>'}: {obj!r} does not match pattern {schema['pattern']!r}"
                )
        except re.error as exc:
            errors.append(f"{path or '<root>'}: invalid pattern {schema['pattern']!r}: {exc}")

    # object: required + properties
    if isinstance(obj, dict):
        for req in schema.get("required", []) or []:
            if req not in obj:
                errors.append(f"{path or '<root>'}: missing required property {req!r}")
        props = schema.get("properties") or {}
        for key, subschema in props.items():
            if key in obj:
                child_path = f"{path}.{key}" if path else key
                _validate(obj[key], subschema, child_path, errors)

    # array: items
    if isinstance(obj, list) and "items" in schema:
        item_schema = schema["items"]
        for i, element in enumerate(obj):
            child_path = f"{path}[{i}]"
            _validate(element, item_schema, child_path, errors)


def validate(obj, schema: dict) -> list:
    """Validate ``obj`` against ``schema``. Returns a list of error strings;
    an empty list means the object is valid.
    """
    errors: list = []
    _validate(obj, schema, "", errors)
    return errors


if __name__ == "__main__":  # pragma: no cover
    import argparse
    import json
    import sys

    ap = argparse.ArgumentParser(description="validate a JSON object against a schema")
    ap.add_argument("--schema", required=True, help="path to a JSON schema file")
    ap.add_argument("--data", required=True, help="path to a JSON data file")
    args = ap.parse_args()
    with open(args.schema, encoding="utf-8") as f:
        schema = json.load(f)
    with open(args.data, encoding="utf-8") as f:
        data = json.load(f)
    errs = validate(data, schema)
    if errs:
        for e in errs:
            print(e, file=sys.stderr)
        sys.exit(1)
    print("ok")
