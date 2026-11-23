import unittest
from typing import Literal

from pydantic import BaseModel, Field

from app.research.strict import UnsupportedSchema, constraint_notes, prepare, to_provider_schema


class Effect(BaseModel):
    measure: str
    value: float | None = None


class Claim(BaseModel):
    """A claim."""
    claim: str = Field(max_length=400, description="Claim text.")
    sentence_ids: list[str] = Field(min_length=1, max_length=4)
    kind: Literal["finding", "background"]
    effect: Effect | None = None
    tags: list[str] = Field(default_factory=list)
    n: int | None = Field(default=None, ge=0)
    fixed: Literal["v1"] = "v1"


class ClaimSet(BaseModel):
    claims: list[Claim]


SNAPSHOT = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "description": "A claim.",
                "properties": {
                    "claim": {"type": "string", "description": "Claim text."},
                    "sentence_ids": {"type": "array", "items": {"type": "string"}},
                    "kind": {"type": "string", "enum": ["finding", "background"]},
                    "effect": {"anyOf": [
                        {"type": "object",
                         "properties": {"measure": {"type": "string"},
                                        "value": {"anyOf": [{"type": "number"}, {"type": "null"}]}},
                         "required": ["measure", "value"], "additionalProperties": False},
                        {"type": "null"}]},
                    "tags": {"anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "null"}]},
                    "n": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                    "fixed": {"anyOf": [{"type": "string", "enum": ["v1"]}, {"type": "null"}]},
                },
                "required": ["claim", "sentence_ids", "kind", "effect", "tags", "n", "fixed"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["claims"],
    "additionalProperties": False,
}


def walk(node, fn):
    fn(node)
    if isinstance(node, dict):
        for v in node.values():
            walk(v, fn)
    elif isinstance(node, list):
        for v in node:
            walk(v, fn)


class StrictTests(unittest.TestCase):
    def test_snapshot(self):
        self.assertEqual(to_provider_schema(ClaimSet), SNAPSHOT)

    def test_restrictions_hold_everywhere(self):
        schema = to_provider_schema(ClaimSet)

        def check(node):
            if not isinstance(node, dict):
                return
            for key in ("$ref", "$defs", "oneOf", "not", "allOf", "minLength", "maxLength", "minItems", "maxItems",
                        "minimum", "pattern", "format", "default", "title", "const"):
                self.assertNotIn(key, node)
            if node.get("type") == "object":
                self.assertIs(node["additionalProperties"], False)
                self.assertEqual(set(node["required"]), set(node["properties"]))
            if "anyOf" in node:
                self.assertFalse({"type", "properties"} & node.keys())

        walk(schema, check)

    def test_recursion_rejected(self):
        class Node(BaseModel):
            name: str
            children: list["Node"] = []

        with self.assertRaises(UnsupportedSchema):
            to_provider_schema(Node)

    def test_free_form_map_rejected(self):
        class Bag(BaseModel):
            values: dict[str, int]

        with self.assertRaises(UnsupportedSchema):
            to_provider_schema(Bag)

    def test_discriminated_union_rejected(self):
        class A(BaseModel):
            kind: Literal["a"]

        class B(BaseModel):
            kind: Literal["b"]

        class U(BaseModel):
            item: A | B = Field(discriminator="kind")

        with self.assertRaises(UnsupportedSchema):
            to_provider_schema(U)

    def test_constraint_notes_for_prompt(self):
        notes = constraint_notes(ClaimSet)
        self.assertIn("claims[].claim: maxLength=400", notes)
        self.assertIn("claims[].sentence_ids: minItems=1, maxItems=4", notes)
        self.assertIn("claims[].n: minimum=0", notes)

    def test_prepare_restores_defaults_for_nulls(self):
        raw = {"claims": [{"claim": "x", "sentence_ids": ["S1.P1.s1"], "kind": "finding", "effect": None,
                           "tags": None, "n": None, "fixed": None}]}
        parsed = ClaimSet.model_validate(prepare(ClaimSet, raw))
        claim = parsed.claims[0]
        self.assertEqual((claim.tags, claim.fixed, claim.effect, claim.n), ([], "v1", None, None))


if __name__ == "__main__":
    unittest.main()
