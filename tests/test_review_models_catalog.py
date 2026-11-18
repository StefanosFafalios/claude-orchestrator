"""Offline test for the maintainer skill's catalog filter."""
from __future__ import annotations

import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / ".claude/skills/review-models/catalog.py"
_spec = importlib.util.spec_from_file_location("review_models_catalog", _PATH)
catalog = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(catalog)


def _m(id, prompt="0.000001", completion="0.000002", ctx=128000, params=("tools",), **extra):
    return {"id": id, "name": id, "context_length": ctx, "supported_parameters": list(params),
            "pricing": {"prompt": prompt, "completion": completion}, **extra}


def test_filter_models():
    models = [
        _m("a/pricey", prompt="0.000010", completion="0.000030"),
        _m("a/cheap", prompt="0.000001", completion="0.000005"),
        _m("a/notools", params=("temperature",)),
        _m("a/small", ctx=8000),
        _m("a/x:free"),
        _m("a/y:batch"),
        _m("~a/alias"),
        _m("a/zero", prompt="0"),
        _m("a/expired", expiration_date="2026-01-01"),
        _m("a/future", prompt="0.000002", completion="0.000002", expiration_date="2027-01-01"),
        _m("a/nullexp", prompt="0.000003", completion="0.000003", expiration_date=None),
    ]
    out = catalog.filter_models(models, today="2026-11-18")
    assert [m["id"] for m in out] == ["a/cheap", "a/future", "a/nullexp", "a/pricey"]
    cheap = out[0]
    assert cheap["prompt_usd_per_mtok"] == 1.0
    assert cheap["completion_usd_per_mtok"] == 5.0
    assert cheap["blended_usd_per_mtok"] == 2.0  # (3*1 + 5) / 4
    assert set(cheap) == {"id", "name", "context_length", "prompt_usd_per_mtok",
                          "completion_usd_per_mtok", "blended_usd_per_mtok"}
    assert catalog.filter_models({"data": models}, today="2026-11-18") == out
