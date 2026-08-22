from scripts import catalog_drift as drift


def test_truncated_openapi_is_salvaged_and_classified_without_fuzzy_matching():
    text = ('{"openapi":"3.0.0","paths":{'
            '"/live":{"get":{}},"/wrong-method":{"post":{}},'
            '"/restored":{"get":{}}},"components":')
    paths, mode = drift.parse_spec(text)
    assert mode.startswith("salvaged-json")
    assert set(paths) == {"/live", "/wrong-method", "/restored"}

    endpoints = [
        {"id": "p.live", "path": "/live", "method": "GET"},
        {"id": "p.dead", "path": "/missing", "method": "GET"},
        {"id": "p.method", "path": "/wrong-method", "method": "GET"},
        {"id": "p.ack", "path": "/retired", "method": "GET", "status": "retired"},
        {"id": "p.restored", "path": "/restored", "method": "GET", "status": "retired"},
    ]
    result = drift.compare("p", endpoints, paths)
    assert [ep["id"] for ep in result.ok] == ["p.live"]
    assert [ep["id"] for ep in result.dead_path] == ["p.dead"]
    assert [(ep["id"], methods) for ep, methods in result.method_rot] == [
        ("p.method", ["POST"]),
    ]
    assert [ep["id"] for ep in result.acknowledged] == ["p.ack"]
    assert [ep["id"] for ep in result.restored] == ["p.restored"]
    assert result.failed
