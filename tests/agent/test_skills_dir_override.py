"""Tests for the context-local skills-dir override (per-agent skills)."""

import json
import threading

from mangaba_agent.mangaba_constants import (
    get_skills_dir,
    reset_skills_dir_override,
    set_skills_dir_override,
)


def _make_skill(root, category, name):
    d = root / category / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: skill de teste {name}.\n---\n\n# {name}\n",
        encoding="utf-8",
    )
    return d


def test_override_set_and_reset(tmp_path):
    default = get_skills_dir()
    token = set_skills_dir_override(tmp_path / "skills")
    try:
        assert get_skills_dir() == tmp_path / "skills"
    finally:
        reset_skills_dir_override(token)
    assert get_skills_dir() == default


def test_override_is_thread_local(tmp_path):
    """Um override numa thread não vaza para outra (contexto por thread)."""
    seen = {}

    def worker(label, override):
        token = set_skills_dir_override(override) if override else None
        try:
            seen[label] = get_skills_dir()
        finally:
            if token is not None:
                reset_skills_dir_override(token)

    t1 = threading.Thread(target=worker, args=("com", tmp_path / "a"))
    t2 = threading.Thread(target=worker, args=("sem", None))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert seen["com"] == tmp_path / "a"
    assert seen["sem"] == get_skills_dir()


def test_skills_list_honors_override(tmp_path):
    profile_skills = tmp_path / "skills"
    _make_skill(profile_skills, "teste", "skill-so-do-profile")

    from tools.skills_tool import skills_list

    token = set_skills_dir_override(profile_skills)
    try:
        names = [s["name"] for s in json.loads(skills_list(category="teste"))["skills"]]
    finally:
        reset_skills_dir_override(token)
    assert "skill-so-do-profile" in names

    names_default = [
        s["name"] for s in json.loads(skills_list(category="teste")).get("skills", [])
    ]
    assert "skill-so-do-profile" not in names_default


def test_skills_prompt_honors_override(tmp_path):
    profile_skills = tmp_path / "skills"
    _make_skill(profile_skills, "teste", "skill-prompt-profile")

    from agent.prompt_builder import build_skills_system_prompt

    token = set_skills_dir_override(profile_skills)
    try:
        prompt = build_skills_system_prompt()
    finally:
        reset_skills_dir_override(token)
    assert "skill-prompt-profile" in prompt
    assert "skill-prompt-profile" not in build_skills_system_prompt()
