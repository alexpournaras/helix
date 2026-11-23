"""Pydantic → provider-compatible JSON schema (PLAN B5.1 step 4, Part C C29).

The output is the subset that OpenAI strict Structured Outputs, Claude JSON outputs and Ollama
`format` all accept:
- `$defs` inlined (recursion is rejected);
- no `oneOf`, `not`, `allOf` (single-element `allOf` is unwrapped), no `anyOf` next to `type`/`properties`;
- every property required; properties that may be omitted become nullable via `anyOf [T, null]`;
- `additionalProperties: false` on every object; free-form maps are rejected;
- numeric, length, pattern, count and format constraints removed (Pydantic still enforces them,
  and :func:`constraint_notes` lists them for the prompt);
- `const` becomes a one-value `enum`; titles and defaults dropped.

:func:`prepare` undoes the nullable wrapping before Pydantic validation: a `null` for a field that has a
default but is not itself nullable is removed so the default applies.
"""

from __future__ import annotations

import copy
from typing import Any, get_args, get_origin

from pydantic import BaseModel

DROPPED = {
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "minLength", "maxLength",
    "pattern", "format", "minItems", "maxItems", "uniqueItems", "minProperties", "maxProperties", "default",
    "title", "examples", "discriminator", "readOnly", "writeOnly", "deprecated", "contentMediaType",
}
CONSTRAINTS = ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "minLength", "maxLength",
               "pattern", "format", "minItems", "maxItems", "uniqueItems")
FORBIDDEN = {"oneOf", "not", "if", "then", "else", "patternProperties", "prefixItems", "dependentSchemas",
             "dependentRequired", "propertyNames", "unevaluatedProperties", "contains"}


class UnsupportedSchema(ValueError):
    pass


def _resolve(ref: str, defs: dict) -> tuple[str, dict]:
    if not ref.startswith("#/$defs/"):
        raise UnsupportedSchema(f"Unsupported $ref {ref!r}.")
    name = ref.removeprefix("#/$defs/")
    if name not in defs:
        raise UnsupportedSchema(f"Unknown $ref {ref!r}.")
    return name, defs[name]


def _convert(node: Any, defs: dict, stack: tuple[str, ...], path: str) -> Any:
    if not isinstance(node, dict):
        return node
    node = dict(node)
    if "$ref" in node:
        name, target = _resolve(node.pop("$ref"), defs)
        if name in stack:
            raise UnsupportedSchema(f"Recursive schema at {path or '/'} ({name}).")
        merged = {**target, **{k: v for k, v in node.items() if k == "description"}}
        return _convert(merged, defs, stack + (name,), path)
    if "allOf" in node:
        parts = node.pop("allOf")
        if len(parts) != 1:
            raise UnsupportedSchema(f"allOf with several schemas at {path or '/'}.")
        merged = {**parts[0], **{k: v for k, v in node.items() if k == "description"}}
        return _convert(merged, defs, stack, path)
    bad = FORBIDDEN & node.keys()
    if bad:
        raise UnsupportedSchema(f"{', '.join(sorted(bad))} at {path or '/'} is not supported by every provider.")
    if "anyOf" in node and ({"type", "properties", "items", "enum"} & node.keys()):
        raise UnsupportedSchema(f"anyOf next to type/properties at {path or '/'}.")

    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in DROPPED or key == "$defs":
            continue
        if key == "const":
            out["enum"] = [value]
        elif key == "anyOf":
            out["anyOf"] = _dedupe([_convert(v, defs, stack, f"{path}/anyOf") for v in value])
        elif key == "items":
            if value is False or isinstance(value, list):
                raise UnsupportedSchema(f"Tuple arrays at {path or '/'} are not supported.")
            out["items"] = _convert(value, defs, stack, f"{path}[]")
        elif key == "properties":
            out["properties"] = {name: _convert(v, defs, stack, f"{path}/{name}") for name, v in value.items()}
        elif key == "additionalProperties":
            continue  # set below for objects
        else:
            out[key] = copy.deepcopy(value)

    if out.get("type") == "object" or "properties" in node:
        if node.get("additionalProperties") not in (None, False):
            raise UnsupportedSchema(f"Free-form maps at {path or '/'} are not supported; use a list of pairs.")
        props = out.setdefault("properties", {})
        out["type"] = "object"
        required = set(node.get("required", []))
        for name, schema in list(props.items()):
            if name not in required and not _nullable(schema):
                props[name] = {"anyOf": [schema, {"type": "null"}]}
        out["required"] = list(props)
        out["additionalProperties"] = False
    elif out.get("type") == "array" and "items" not in out:
        raise UnsupportedSchema(f"Array without items at {path or '/'}.")
    return out


def _dedupe(items: list[dict]) -> list[dict]:
    seen, out = [], []
    for item in items:
        if item not in seen:
            seen.append(item)
            out.append(item)
    return out


def _nullable(schema: dict) -> bool:
    if schema.get("type") == "null":
        return True
    if isinstance(schema.get("type"), list) and "null" in schema["type"]:
        return True
    return any(_nullable(s) for s in schema.get("anyOf", []))


def to_provider_schema(model: type[BaseModel]) -> dict:
    raw = model.model_json_schema(mode="validation")
    defs = raw.get("$defs", {})
    result = _convert(raw, defs, (model.__name__,) if raw.get("title") == model.__name__ else (), "")
    if result.get("type") != "object":
        raise UnsupportedSchema("The root of a structured output must be an object.")
    return result


def schema_name(model: type[BaseModel]) -> str:
    return "".join(ch for ch in model.__name__ if ch.isalnum() or ch in "_-")[:64] or "Output"


def constraint_notes(model: type[BaseModel]) -> list[str]:
    """Human-readable constraints removed from the provider schema, for the prompt (B5.1)."""
    raw = model.model_json_schema(mode="validation")
    defs = raw.get("$defs", {})
    notes: list[str] = []

    def walk(node: Any, path: str, stack: tuple[str, ...]) -> None:
        if not isinstance(node, dict):
            return
        if "$ref" in node:
            name, target = _resolve(node["$ref"], defs)
            if name not in stack:
                walk(target, path, stack + (name,))
            return
        found = [f"{k}={node[k]}" for k in CONSTRAINTS if k in node]
        if found:
            notes.append(f"{path or '(root)'}: {', '.join(found)}")
        for sub in node.get("allOf", []) + node.get("anyOf", []):
            walk(sub, path, stack)
        for name, sub in node.get("properties", {}).items():
            walk(sub, f"{path}.{name}" if path else name, stack)
        if isinstance(node.get("items"), dict):
            walk(node["items"], f"{path}[]", stack)

    walk(raw, "", ())
    return notes


def prepare(model: type[BaseModel], data: Any) -> Any:
    """Remove `null` values for fields that have a default but are not nullable, recursively."""
    if not isinstance(data, dict):
        return data
    out = dict(data)
    for name, field in model.model_fields.items():
        key = field.alias or name
        if key not in out:
            continue
        value = out[key]
        if value is None and not field.is_required() and not _annotation_nullable(field.annotation):
            del out[key]
            continue
        sub = _model_in(field.annotation)
        if sub is None:
            continue
        if isinstance(value, dict):
            out[key] = prepare(sub, value)
        elif isinstance(value, list):
            out[key] = [prepare(sub, v) if isinstance(v, dict) else v for v in value]
    return out


def _annotation_nullable(annotation: Any) -> bool:
    return annotation is type(None) or type(None) in get_args(annotation)


def _model_in(annotation: Any) -> type[BaseModel] | None:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    for arg in get_args(annotation):
        found = _model_in(arg)
        if found is not None:
            return found
    origin = get_origin(annotation)
    if isinstance(origin, type) and issubclass(origin, BaseModel):
        return origin
    return None
