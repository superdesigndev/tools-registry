"""Skill-directory scanner + payload builder (`treg upload skills`).

Pure logic over synthetic skill dirs — no DB, no network. Covers: classification (contract /
generated-from-script / generated-from-file / recipe_only), the env-var gap, base_url + binding from a
script's `API=`, contract writing, and build_payload for all three kinds.
"""
from __future__ import annotations

from treg import skills as sk


def _skill(tmp_path, name, *, skillmd="# s\n", script=None, secret=None, treg=None):
    d = tmp_path / name
    d.mkdir()
    (d / "SKILL.md").write_text(skillmd)
    if script:
        (d / f"{name}.py").write_text(script)
    if secret:
        sd = d / ".secrets"; sd.mkdir()
        (sd / secret[0]).write_text(secret[1])
    if treg:
        import json
        (d / "treg.json").write_text(json.dumps(treg))
    return d


def test_recipe_only_for_knowledge_skill(tmp_path):
    _skill(tmp_path, "seo-writer", skillmd="# SEO writer\nWrite blogs.\n")
    [d] = sk.scan_skills(str(tmp_path))
    assert d.kind == "recipe_only" and d.base_url is None


def test_generated_from_script_env(tmp_path):
    _skill(tmp_path, "render", script='API = "https://api.render.com/v1"\nimport os\nk=os.environ.get("RENDER_API_KEY")\n')
    [d] = sk.scan_skills(str(tmp_path), env_names={"RENDER_API_KEY"})
    assert d.kind == "generated" and d.base_url == "https://api.render.com/v1"
    assert d.secrets[0]["env"] == "RENDER_API_KEY" and not d.gaps
    assert d.bindings[0]["format"] == "Bearer {secret}"


def test_generated_env_gap_when_key_absent(tmp_path):
    _skill(tmp_path, "vercel", script='API = "https://api.vercel.com"\nimport os\nt=os.environ.get("VERCEL_TOKEN")\n')
    [d] = sk.scan_skills(str(tmp_path), env_names=set())   # key NOT in env
    assert d.kind == "generated" and any("VERCEL_TOKEN" in g for g in d.gaps)


def test_generated_from_local_secret_file(tmp_path):
    _skill(tmp_path, "intercom", script='BASE = "https://api.intercom.io"\n', secret=("token", "abc123"))
    [d] = sk.scan_skills(str(tmp_path))
    assert d.kind == "generated" and d.base_url == "https://api.intercom.io"
    assert d.secrets[0]["file"] == ".secrets/token" and not d.gaps


def test_existing_contract_used_verbatim(tmp_path):
    _skill(tmp_path, "gsc", treg={"name": "gsc", "base_url": "https://searchconsole.googleapis.com",
                                   "secrets": [{"file": ".secrets/token.json", "name": "gsc", "kind": "oauth"}],
                                   "bindings": [{"secret": "gsc", "injector": "oauth"}]})
    [d] = sk.scan_skills(str(tmp_path))
    assert d.kind == "contract" and d.base_url == "https://searchconsole.googleapis.com"


def test_write_contract_only_for_generated(tmp_path):
    _skill(tmp_path, "render", script='API = "https://api.render.com/v1"\nimport os\nos.environ.get("RENDER_API_KEY")\n')
    [d] = sk.scan_skills(str(tmp_path), env_names={"RENDER_API_KEY"})
    path = sk.write_contract(d)
    assert path and (tmp_path / "render" / "treg.json").exists()
    # re-writing is a no-op without force
    assert sk.write_contract(d) is None


def test_build_payload_recipe_only(tmp_path):
    _skill(tmp_path, "seo", skillmd="# SEO\nrecipe body\n")
    [d] = sk.scan_skills(str(tmp_path))
    p = sk.build_payload(d, {})
    assert p["tools"] == [] and p["secrets"] == [] and "recipe body" in p["recipe"]


def test_build_payload_env_secret_reads_value(tmp_path):
    _skill(tmp_path, "render", script='API = "https://api.render.com/v1"\nimport os\nos.environ.get("RENDER_API_KEY")\n')
    [d] = sk.scan_skills(str(tmp_path), env_names={"RENDER_API_KEY"})
    p = sk.build_payload(d, {"RENDER_API_KEY": "secret-value"})
    assert p["secrets"][0]["value"] == "secret-value"
    assert p["tools"][0]["base_url"] == "https://api.render.com/v1"


def test_build_payload_file_secret_reads_file(tmp_path):
    _skill(tmp_path, "intercom", script='BASE = "https://api.intercom.io"\n', secret=("token", "file-token"))
    [d] = sk.scan_skills(str(tmp_path))
    p = sk.build_payload(d, {})
    assert p["secrets"][0]["value"] == "file-token"


def test_reading_PATH_is_not_a_credential(tmp_path):
    # BUG: "PAT" in "PATH" flagged os.environ["PATH"] as an auth var. A skill that only reads PATH is a
    # knowledge skill, not an API tool.
    _skill(tmp_path, "helper", script='import os\np = os.environ.get("PATH")\n')
    [d] = sk.scan_skills(str(tmp_path))
    assert d.kind == "recipe_only"


def test_doc_url_is_not_used_as_base_url(tmp_path):
    _skill(tmp_path, "svc", script='API = "https://docs.example.com"\nimport os\nos.environ.get("SVC_API_KEY")\n')
    [d] = sk.scan_skills(str(tmp_path), env_names={"SVC_API_KEY"})
    assert d.base_url != "https://docs.example.com"   # doc host rejected


def test_colliding_secret_file_stems_get_unique_names(tmp_path):
    d = tmp_path / "multi"; d.mkdir(); (d / "SKILL.md").write_text("# m\n")
    sd = d / ".secrets"; sd.mkdir()
    (sd / "token.json").write_text('{"token":"a"}'); (sd / "token.txt").write_text("b")
    [det] = sk.scan_skills(str(tmp_path))
    names = [s["name"] for s in det.secrets]
    assert len(names) == len(set(names))   # no duplicate local_name


def test_contract_health_and_examples_carried(tmp_path):
    _skill(tmp_path, "svc", treg={"name": "svc", "base_url": "https://api.svc.com",
                                   "secrets": [], "bindings": [{"secret": "x"}],
                                   "health": {"path": "ping"}, "examples": [{"method": "GET", "path": "me"}]})
    [d] = sk.scan_skills(str(tmp_path))
    p = sk.build_payload(d, {})
    assert p["tools"][0]["health_check"] == {"path": "ping"}
    assert p["tools"][0]["examples"] == [{"method": "GET", "path": "me"}]


def test_single_skill_dir_is_scannable(tmp_path):
    d = tmp_path / "solo"; d.mkdir(); (d / "SKILL.md").write_text("# solo\n")
    dets = sk.scan_skills(str(d))          # point AT the skill, not its parent
    assert len(dets) == 1 and dets[0].name == "solo"


def test_readme_only_subdir_is_not_a_skill(tmp_path):
    (tmp_path / "docs").mkdir(); (tmp_path / "docs" / "README.md").write_text("# docs\n")
    (tmp_path / "real").mkdir(); (tmp_path / "real" / "SKILL.md").write_text("# real\n")
    names = {d.name for d in sk.scan_skills(str(tmp_path))}
    assert names == {"real"}               # docs/ (README only) is not a phantom skill


def test_shell_script_skill_is_detected(tmp_path):
    d = tmp_path / "sh-skill"; d.mkdir(); (d / "SKILL.md").write_text("# sh\n")
    (d / "run.sh").write_text('curl -H "Authorization: Bearer $RENDER_API_KEY" https://api.render.com/v1/x\n')
    [det] = sk.scan_skills(str(tmp_path), env_names={"RENDER_API_KEY"})
    assert det.kind == "generated" and det.secrets[0]["env"] == "RENDER_API_KEY"


def test_contract_flags_absent_env_and_missing_file(tmp_path):
    _skill(tmp_path, "a", treg={"name": "a", "base_url": "https://api.a.com",
                                "secrets": [{"env": "A_KEY", "name": "a", "kind": "env"}],
                                "bindings": [{"secret": "a"}]})
    [d] = sk.scan_skills(str(tmp_path), env_names=set())   # A_KEY not present
    assert d.kind == "contract" and any("A_KEY" in g for g in d.gaps)


def test_build_payload_rejects_sourceless_secret(tmp_path):
    _skill(tmp_path, "x", script='API = "https://api.x.com"\n')
    [d] = sk.scan_skills(str(tmp_path))
    d.secrets = [{"name": "x", "kind": "env"}]   # neither file nor env
    import pytest
    with pytest.raises(ValueError, match="file|env|source"):
        sk.build_payload(d, {})
