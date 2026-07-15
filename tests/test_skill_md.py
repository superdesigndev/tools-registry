"""The official tools-registry skill: served {BASE}-templated at GET /skill.md, and installed
to ~/.claude/skills/tools-registry/ by install.sh (so one curl gives a machine the CLI + the skill)."""

from __future__ import annotations

from pathlib import Path

from treg import api as api_mod


async def test_skill_md_served_and_templated(clients):
    r = await clients.get("/skill.md")
    assert r.status_code == 200
    body = r.text
    assert body.startswith("---") and "name: tools-registry" in body  # loadable skill frontmatter
    assert "{BASE}" not in body                                        # fully templated
    assert "/call/https://api.intercom.io" in body                     # the passthrough teaching line
    assert "treg register" not in body                                 # the retired command must not resurface


def test_install_sh_installs_the_skill():
    sh = (Path(api_mod.__file__).parent / "web" / "install.sh").read_text()
    assert "$BASE/skill.md" in sh and ".claude/skills/tools-registry" in sh
