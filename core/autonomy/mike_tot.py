# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
Mike ToT — Tree of Thoughts (arxiv 2305.10601)
================================================
Structured reasoning via BFS/beam-search over intermediate thoughts.
For complex problems (math, code, planning, multi-step), explores
multiple reasoning paths and selects the best one.

Paper: Tree of Thoughts: Deliberate Problem Solving with Large Language Models
Achived 74% success rate on Game of 24 (vs 4% standard prompting).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

from mike_config import env_bool

log = logging.getLogger("mike.tot")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TOT_ENABLED = env_bool("MIKE_TOT_ENABLED", True)
TOT_MAX_DEPTH = int(os.getenv("MIKE_TOT_MAX_DEPTH", "5"))
TOT_BEAM_WIDTH = int(os.getenv("MIKE_TOT_BEAM_WIDTH", "3"))
TOT_NUM_CANDIDATES = int(os.getenv("MIKE_TOT_NUM_CANDIDATES", "5"))
TOT_SCORE_THRESHOLD = float(os.getenv("MIKE_TOT_SCORE_THRESHOLD", "0.3"))
TOT_TIMEOUT_SEC = int(os.getenv("MIKE_TOT_TIMEOUT_SEC", "60"))

# Keywords that trigger ToT
TOT_TRIGGER_KEYWORDS = [
    "resolva", "calcule", "implemente", "codifique", "planeje",
    "otimize", "debug", "arquitete", "desenhe", "projete",
    "explique passo a passo", "como resolver", "qual a melhor",
    "compare", "analise", "raciocine",
]


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------

@dataclass
class Thought:
    """A single thought node in the tree."""
    id: str
    content: str
    depth: int = 0
    score: float = 0.0
    parent_id: Optional[str] = None
    children: list[str] = field(default_factory=list)
    is_complete: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "content": self.content,
            "depth": self.depth,
            "score": round(self.score, 4),
            "parent_id": self.parent_id,
            "children": self.children,
            "is_complete": self.is_complete,
        }


@dataclass
class ToTResult:
    """Result of a Tree of Thoughts exploration."""
    success: bool
    best_path: list[Thought]
    best_score: float
    total_thoughts: int
    depth_reached: int
    elapsed_sec: float
    method: str  # "bfs" | "beam"

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "best_path": [t.to_dict() for t in self.best_path],
            "best_score": round(self.best_score, 4),
            "total_thoughts": self.total_thoughts,
            "depth_reached": self.depth_reached,
            "elapsed_sec": round(self.elapsed_sec, 2),
            "method": self.method,
        }


# ---------------------------------------------------------------------------
# Thought Generator & Evaluator
# ---------------------------------------------------------------------------

THOUGHT_GENERATION_PROMPT = """Voce esta resolvendo um problema passo a passo usando "Tree of Thoughts".

PROBLEMA: {problem}

CONTEXTO (pensamentos anteriores): {context}

Gere {num_candidates} pensamentos diferentes para avancar na solucao. Cada pensamento deve ser um passo concreto, diferente dos outros, e avancar em direcao a solucao.

Responda SOMENTE com JSON puro, sem markdown:
{{
  "thoughts": [
    {{"content": "pensamento 1", "reasoning": "por que esse passo"}},
    {{"content": "pensamento 2", "reasoning": "por que esse passo"}}
  ]
}}"""

THOUGHT_EVALUATION_PROMPT = """Avalie cada pensamento abaixo em relacao ao problema original.
De uma nota de 0.0 a 1.0 para cada um baseado em: relevancia, progresso, logica.

PROBLEMA: {problem}

PENSAMENTOS:
{thoughts_list}

Responda SOMENTE com JSON puro, sem markdown:
{{
  "scores": [
    {{"id": "id_1", "score": 0.85, "rationale": "breve explicacao"}},
    {{"id": "id_2", "score": 0.60, "rationale": "breve explicacao"}}
  ],
  "any_complete": false,
  "complete_id": null
}}"""


def _parse_json_response(text: str) -> dict:
    """Robust JSON extraction from LLM response."""
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass
    match = re.search(r'\{[\s\S]*\}', text)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return {}


# ---------------------------------------------------------------------------
# TreeOfThoughts
# ---------------------------------------------------------------------------

class TreeOfThoughts:
    """BFS/beam-search exploration of reasoning paths."""

    def __init__(
        self,
        generate_fn: Callable,
        log_fn: Optional[Callable] = None,
    ):
        self._generate = generate_fn
        self._log = log_fn or log.info
        self._thought_counter = 0

    def _next_id(self) -> str:
        self._thought_counter += 1
        return f"t{self._thought_counter}"

    @staticmethod
    def should_use_tot(user_text: str) -> bool:
        """Heuristic: should we use Tree of Thoughts for this query?"""
        if not TOT_ENABLED:
            return False
        text_lower = user_text.lower()
        # Trigger keywords
        if any(kw in text_lower for kw in TOT_TRIGGER_KEYWORDS):
            return True
        # Long queries with code/math indicators
        if len(user_text) > 150 and any(c in user_text for c in "{}[]()=+-*/%"):
            return True
        # Multi-step requests
        steps_markers = [r"\bprimeiro\b.*\bdepois\b", r"\bpasso\s+1\b", r"\bstep\s+1\b",
                         r"\b1\..*2\..*3\.", r"\bprimeiro\b.*\bsegundo\b.*\bterceiro\b"]
        for pattern in steps_markers:
            if re.search(pattern, text_lower, re.DOTALL):
                return True
        return False

    async def solve(
        self,
        problem: str,
        max_depth: int = TOT_MAX_DEPTH,
        beam_width: int = TOT_BEAM_WIDTH,
        num_candidates: int = TOT_NUM_CANDIDATES,
        score_threshold: float = TOT_SCORE_THRESHOLD,
        timeout_sec: int = TOT_TIMEOUT_SEC,
    ) -> ToTResult:
        """Solve a problem using Tree of Thoughts with beam search."""
        start_time = time.time()
        all_thoughts: dict[str, Thought] = {}
        current_layer: list[Thought] = []

        # Root thought — the problem itself
        root = Thought(
            id=self._next_id(),
            content=f"Problema: {problem[:300]}",
            depth=0,
            score=1.0,
        )
        all_thoughts[root.id] = root
        current_layer = [root]

        best_complete: Optional[Thought] = None
        depth_reached = 0

        try:
            for depth in range(1, max_depth + 1):
                depth_reached = depth
                if time.time() - start_time > timeout_sec:
                    self._log(f"[ToT] Timeout at depth {depth}")
                    break

                # Generate candidates for each thought in current layer
                all_candidates: list[Thought] = []
                for parent in current_layer:
                    if time.time() - start_time > timeout_sec:
                        break
                    candidates = await self._generate_candidates(
                        problem, parent, num_candidates
                    )
                    for c in candidates:
                        c.depth = depth
                        c.parent_id = parent.id
                        parent.children.append(c.id)
                        all_thoughts[c.id] = c
                        all_candidates.append(c)

                if not all_candidates:
                    break

                # Evaluate all candidates
                scored = await self._evaluate_candidates(problem, all_candidates)
                for thought_id, score, is_complete in scored:
                    t = all_thoughts.get(thought_id)
                    if t:
                        t.score = score
                        t.is_complete = is_complete
                        if is_complete and (best_complete is None or score > best_complete.score):
                            best_complete = t

                # Filter by score threshold, keep top beam_width
                valid = [t for t in all_candidates if t.score >= score_threshold]
                valid.sort(key=lambda t: t.score, reverse=True)
                current_layer = valid[:beam_width]

                if not current_layer:
                    # Relax threshold if nothing passed
                    all_candidates.sort(key=lambda t: t.score, reverse=True)
                    current_layer = all_candidates[:max(1, beam_width // 2)]
                    if not current_layer:
                        break

            # Build best path
            if best_complete:
                best_path = self._build_path(best_complete, all_thoughts)
            elif current_layer:
                # No complete solution — use highest scored leaf
                best_leaf = max(current_layer, key=lambda t: t.score)
                best_path = self._build_path(best_leaf, all_thoughts)
            else:
                best_path = [root]

            elapsed = time.time() - start_time
            success = best_complete is not None and best_complete.score >= 0.6

            self._log(
                f"[ToT] depth={depth_reached} thoughts={self._thought_counter} "
                f"score={best_path[-1].score:.3f} elapsed={elapsed:.1f}s success={success}"
            )

            return ToTResult(
                success=success,
                best_path=best_path,
                best_score=best_path[-1].score,
                total_thoughts=self._thought_counter,
                depth_reached=depth_reached,
                elapsed_sec=elapsed,
                method="beam",
            )

        except Exception as exc:
            elapsed = time.time() - start_time
            self._log(f"[ToT] Error: {exc}")
            return ToTResult(
                success=False,
                best_path=[root],
                best_score=0.0,
                total_thoughts=self._thought_counter,
                depth_reached=depth_reached,
                elapsed_sec=elapsed,
                method="beam",
            )

    async def _generate_candidates(
        self, problem: str, parent: Thought, num: int
    ) -> list[Thought]:
        """Generate candidate thoughts from a parent."""
        context = parent.content[:500]
        prompt = THOUGHT_GENERATION_PROMPT.format(
            problem=problem[:500],
            context=context,
            num_candidates=num,
        )
        messages = [{"role": "user", "content": prompt}]
        try:
            result = await asyncio.wait_for(
                self._generate(messages),
                timeout=30.0,
            )
            response_text = result.get("assistant_text", "")
            data = _parse_json_response(response_text)
            candidates = []
            for item in data.get("thoughts", [])[:num]:
                content = str(item.get("content", ""))
                if content.strip():
                    candidates.append(Thought(
                        id=self._next_id(),
                        content=content[:500],
                    ))
            return candidates
        except asyncio.TimeoutError:
            return []
        except Exception as exc:
            self._log(f"[ToT] Generate failed: {exc}")
            return []

    async def _evaluate_candidates(
        self, problem: str, candidates: list[Thought]
    ) -> list[tuple[str, float, bool]]:
        """Evaluate and score candidates. Returns [(thought_id, score, is_complete)]."""
        if not candidates:
            return []

        thoughts_list = "\n".join(
            f"  [{t.id}] {t.content[:200]}" for t in candidates
        )
        prompt = THOUGHT_EVALUATION_PROMPT.format(
            problem=problem[:500],
            thoughts_list=thoughts_list,
        )
        messages = [{"role": "user", "content": prompt}]
        try:
            result = await asyncio.wait_for(
                self._generate(messages),
                timeout=30.0,
            )
            response_text = result.get("assistant_text", "")
            data = _parse_json_response(response_text)

            score_map: dict[str, float] = {}
            for item in data.get("scores", []):
                tid = str(item.get("id", ""))
                score = float(item.get("score", 0.5))
                score_map[tid] = min(1.0, max(0.0, score))

            any_complete = bool(data.get("any_complete", False))
            complete_id = str(data.get("complete_id", "") or "")

            results = []
            for t in candidates:
                score = score_map.get(t.id, 0.3)
                is_complete = any_complete and t.id == complete_id
                results.append((t.id, score, is_complete))
            return results
        except asyncio.TimeoutError:
            return [(t.id, 0.3, False) for t in candidates]
        except Exception as exc:
            self._log(f"[ToT] Evaluate failed: {exc}")
            return [(t.id, 0.3, False) for t in candidates]

    def _build_path(
        self, leaf: Thought, all_thoughts: dict[str, Thought]
    ) -> list[Thought]:
        """Build path from root to leaf."""
        path = []
        current = leaf
        while current is not None:
            path.append(current)
            current = all_thoughts.get(current.parent_id) if current.parent_id else None
        path.reverse()
        return path

    def format_path_for_context(self, result: ToTResult) -> str:
        """Format the best reasoning path for injection into the chat context."""
        if not result.best_path:
            return ""
        lines = ["\n[RACIOCINIO ESTRUTURADO — Tree of Thoughts]"]
        for i, thought in enumerate(result.best_path):
            indent = "  " * min(thought.depth, 4)
            marker = "→" if thought.depth > 0 else "★"
            score_str = f" (score: {thought.score:.2f})" if thought.depth > 0 else ""
            lines.append(f"{indent}{marker} {thought.content[:300]}{score_str}")
        lines.append(f"\nMelhor caminho: {len(result.best_path)} passos, score final: {result.best_score:.2f}")
        return "\n".join(lines)
