# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core" / "autonomy"))

from mike_skills import SkillRegistry, tool_pattern_matches


class WritingSkillRegistryTests(unittest.TestCase):
    def setUp(self):
        skills_dir = ROOT / "skills"
        self.registry = SkillRegistry(str(skills_dir))
        self.registry.load_all()

    def test_writing_skills_are_loaded(self):
        for name in [
            "writing_architect",
            "writing_character",
            "writing_drafter",
            "writing_editor",
        ]:
            self.assertIsNotNone(self.registry.get(name), name)

    def test_writing_packs_resolve(self):
        expected = [
            "writing_architect",
            "writing_character",
            "writing_drafter",
            "writing_editor",
        ]
        pack = self.registry.get_pack("writer_studio")
        self.assertIsNotNone(pack)
        self.assertEqual(pack.skills, expected)
        resolved = self.registry.resolve_pack("writer_studio")
        self.assertEqual([skill.name for skill in resolved], expected)

    def test_writing_skills_match_expected_tasks(self):
        cases = [
            ("crie o outline do livro e a estrutura de capitulos", "writing_architect"),
            ("desenvolva o antagonista e a motivacao do protagonista", "writing_character"),
            ("escreva uma cena de abertura com dialogo e tensao", "writing_drafter"),
            ("revise e reescreva este capitulo para melhorar ritmo e estilo", "writing_editor"),
        ]
        for task, expected in cases:
            match = self.registry.match_best(task, threshold=0.5)
            self.assertIsNotNone(match, task)
            skill, _score = match
            self.assertEqual(skill.name, expected)

    def test_qwen_replaces_external_second_opinion_skill(self):
        self.assertIsNotNone(self.registry.get("qwen_reasoning"))
        self.assertIsNone(self.registry.get("gemini_consult"))
        self.assertTrue(self.registry.coverage_summary()["all_skills_packed"])

    def test_legacy_patterns_resolve_to_current_manifest_names(self):
        self.assertTrue(
            tool_pattern_matches("workspace.read_file", "filesystem.read_text_file")
        )
        self.assertTrue(
            tool_pattern_matches("playwright.goto", "puppeteer.puppeteer_navigate")
        )
        self.assertTrue(
            tool_pattern_matches("browse_search", "web.search_and_cache")
        )

    def test_coverage_summary_reports_real_tool_readiness(self):
        registry = SkillRegistry(str(ROOT / "skills"))
        registry.load_all()
        manifest = [
            {"name": "filesystem.read_text_file"},
            {"name": "filesystem.write_file"},
            {"name": "sequential-thinking.sequentialthinking"},
        ]
        coverage = registry.coverage_summary(manifest)
        self.assertIn("tool_coverage", coverage)
        self.assertTrue(coverage["tool_coverage"]["qwen_reasoning"]["ready"])
