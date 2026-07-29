# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
Mike CoVe — Chain-of-Verification (arxiv 2301.12627)
=====================================================
Post-processing pipeline that reduces hallucination by:
1. Generating verification questions about factual claims
2. Answering each verification question independently
3. Checking consistency between original response and verified facts
4. Flagging or correcting inconsistent claims

Paper: Chain-of-Verification Reduces Hallucination in Large Language Models
Reduces hallucination by ~60% while maintaining response quality.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

from mike_config import env_bool

log = logging.getLogger("mike.cove")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

COVE_ENABLED = env_bool("MIKE_COVE_ENABLED", True)
COVE_MAX_QUESTIONS = int(os.getenv("MIKE_COVE_MAX_QUESTIONS", "5"))
COVE_TIMEOUT_SEC = int(os.getenv("MIKE_COVE_TIMEOUT_SEC", "45"))
COVE_CORRECT_THRESHOLD = float(os.getenv("MIKE_COVE_CORRECT_THRESHOLD", "0.5"))

# Factual claim indicators — trigger CoVe
FACT_PATTERNS = [
    r"\b\d{4}\b",                          # years
    r"\b\d+%\b",                           # percentages
    r"\bR\$\s*\d+",                        # currency (BRL)
    r"\bUS\$\s*\d+",                       # currency (USD)
    r"\b\d+\s*(?:km|m|kg|g|GB|MB|TB)\b",  # measurements
    r"\b(?:segundo|de acordo com|estudo|pesquisa|fonte)\b",
    r"\b(?:lançado|lançou|publicado|publicou)\s+em\b",
    r"\b(?:criado|criou|fundado|fundou)\s+em\b",
    r"\b(?:nasceu|morreu|faleceu)\s+em\b",
    r"\b(?:presidente|CEO|fundador|diretor)\s+(?:da|do|de)\b",
    r"\b(?:endereço|telefone|whatsapp|CEP)\b",
]


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------

@dataclass
class VerificationResult:
    """Result of a single verification question."""
    question: str
    original_claim: str
    verified_answer: str
    is_consistent: bool
    confidence: float = 0.5

    def to_dict(self) -> dict:
        return {
            "question": self.question,
            "original_claim": self.original_claim[:200],
            "verified_answer": self.verified_answer[:200],
            "is_consistent": self.is_consistent,
            "confidence": round(self.confidence, 2),
        }


@dataclass
class CoVeResult:
    """Full Chain-of-Verification result."""
    original_response: str
    verified_response: str
    verification_results: list[VerificationResult]
    total_claims: int
    consistent_claims: int
    corrected: bool
    elapsed_sec: float

    @property
    def consistency_score(self) -> float:
        if self.total_claims == 0:
            return 1.0
        return self.consistent_claims / self.total_claims

    def to_dict(self) -> dict:
        return {
            "original_response": self.original_response[:500],
            "verified_response": self.verified_response[:500],
            "verifications": [v.to_dict() for v in self.verification_results],
            "total_claims": self.total_claims,
            "consistent_claims": self.consistent_claims,
            "consistency_score": round(self.consistency_score, 2),
            "corrected": self.corrected,
            "elapsed_sec": round(self.elapsed_sec, 2),
        }


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

VERIFICATION_QUESTIONS_PROMPT = """Analise o texto abaixo e gere perguntas de verificacao para checar fatos especificos mencionados.
Foque em: datas, nomes, numeros, estatisticas, precos, locais, eventos historicos, claims tecnicas.

TEXTO: {response}

Gere ate {max_questions} perguntas de verificacao que possam confirmar ou refutar claims factuais no texto.
Cada pergunta deve ser objetiva e verificavel.

Responda SOMENTE com JSON puro, sem markdown:
{{
  "has_factual_claims": true,
  "questions": [
    {{"question": "pergunta especifica?", "claim": "trecho do texto original"}}
  ]
}}"""

VERIFICATION_ANSWER_PROMPT = """Responda a pergunta de verificacao abaixo de forma concisa e objetiva.
Baseie-se no seu conhecimento. Se nao souber, diga "NAO SEI".

PERGUNTA: {question}
CLAIM ORIGINAL: {claim}

Responda SOMENTE com o fato verificado, maximo 1 frase."""

CONSISTENCY_CHECK_PROMPT = """Compare a claim original com a resposta verificada. Elas sao consistentes?

CLAIM ORIGINAL: {original_claim}
RESPOSTA VERIFICADA: {verified_answer}

Responda SOMENTE com JSON:
{{
  "is_consistent": true,
  "confidence": 0.95,
  "explanation": "breve razao"
}}"""


def _parse_json_response(text: str) -> dict:
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
# ChainOfVerification
# ---------------------------------------------------------------------------

class ChainOfVerification:
    """Post-processing hallucination reduction via verification chain."""

    def __init__(
        self,
        generate_fn: Callable,
        log_fn: Optional[Callable] = None,
    ):
        self._generate = generate_fn
        self._log = log_fn or log.info

    @staticmethod
    def has_factual_claims(text: str) -> bool:
        """Quick check: does this text contain verifiable factual claims?"""
        for pattern in FACT_PATTERNS:
            if re.search(pattern, text):
                return True
        # Also check for numbers (potential facts)
        numbers = re.findall(r'\b\d+\b', text)
        if len(numbers) >= 3:
            return True
        return False

    async def verify(
        self,
        response_text: str,
        max_questions: int = COVE_MAX_QUESTIONS,
        timeout_sec: int = COVE_TIMEOUT_SEC,
        correct_threshold: float = COVE_CORRECT_THRESHOLD,
    ) -> CoVeResult:
        """Run full Chain-of-Verification on a response."""
        import time
        start_time = time.time()

        if not COVE_ENABLED or not self.has_factual_claims(response_text):
            return CoVeResult(
                original_response=response_text,
                verified_response=response_text,
                verification_results=[],
                total_claims=0,
                consistent_claims=0,
                corrected=False,
                elapsed_sec=0.0,
            )

        try:
            # Step 1: Generate verification questions
            questions = await asyncio.wait_for(
                self._generate_questions(response_text, max_questions),
                timeout=min(timeout_sec / 3, 20.0),
            )

            if not questions:
                return CoVeResult(
                    original_response=response_text,
                    verified_response=response_text,
                    verification_results=[],
                    total_claims=0,
                    consistent_claims=0,
                    corrected=False,
                    elapsed_sec=time.time() - start_time,
                )

            # Step 2: Answer each verification question
            verifications = await asyncio.wait_for(
                self._answer_and_check(questions, timeout_sec),
                timeout=timeout_sec,
            )

            # Step 3: Assess consistency
            consistent = sum(1 for v in verifications if v.is_consistent)
            total = len(verifications)
            consistency_ratio = consistent / max(total, 1)

            verified_response = response_text
            corrected = False

            # Step 4: If consistency is low, flag and optionally correct
            if consistency_ratio < correct_threshold and total > 0:
                verified_response = self._build_corrected_response(
                    response_text, verifications
                )
                corrected = True
                self._log(
                    f"[CoVe] Corrected response: {consistent}/{total} consistent "
                    f"({consistency_ratio:.0%})"
                )

            elapsed = time.time() - start_time
            self._log(
                f"[CoVe] {consistent}/{total} claims consistent "
                f"({consistency_ratio:.0%}) in {elapsed:.1f}s"
            )

            return CoVeResult(
                original_response=response_text,
                verified_response=verified_response,
                verification_results=verifications,
                total_claims=total,
                consistent_claims=consistent,
                corrected=corrected,
                elapsed_sec=elapsed,
            )

        except asyncio.TimeoutError:
            elapsed = time.time() - start_time
            self._log(f"[CoVe] Timeout after {elapsed:.1f}s")
            return CoVeResult(
                original_response=response_text,
                verified_response=response_text,
                verification_results=[],
                total_claims=0,
                consistent_claims=0,
                corrected=False,
                elapsed_sec=elapsed,
            )
        except Exception as exc:
            elapsed = time.time() - start_time
            self._log(f"[CoVe] Error: {exc}")
            return CoVeResult(
                original_response=response_text,
                verified_response=response_text,
                verification_results=[],
                total_claims=0,
                consistent_claims=0,
                corrected=False,
                elapsed_sec=elapsed,
            )

    async def _generate_questions(
        self, response: str, max_q: int
    ) -> list[dict]:
        """Generate verification questions from the response."""
        prompt = VERIFICATION_QUESTIONS_PROMPT.format(
            response=response[:2000],
            max_questions=max_q,
        )
        messages = [{"role": "user", "content": prompt}]
        result = await self._generate(messages)
        data = _parse_json_response(result.get("assistant_text", ""))
        if not data.get("has_factual_claims", False):
            return []
        return data.get("questions", [])[:max_q]

    async def _answer_and_check(
        self, questions: list[dict], timeout_sec: int
    ) -> list[VerificationResult]:
        """Answer each verification question and check consistency."""
        results = []
        for q in questions[:COVE_MAX_QUESTIONS]:
            try:
                # Answer the verification question
                answer_prompt = VERIFICATION_ANSWER_PROMPT.format(
                    question=q.get("question", ""),
                    claim=q.get("claim", ""),
                )
                answer_msg = [{"role": "user", "content": answer_prompt}]
                answer_result = await asyncio.wait_for(
                    self._generate(answer_msg),
                    timeout=min(timeout_sec / COVE_MAX_QUESTIONS, 15.0),
                )
                verified_answer = answer_result.get("assistant_text", "").strip()

                # Check consistency
                check_prompt = CONSISTENCY_CHECK_PROMPT.format(
                    original_claim=q.get("claim", ""),
                    verified_answer=verified_answer,
                )
                check_msg = [{"role": "user", "content": check_prompt}]
                check_result = await asyncio.wait_for(
                    self._generate(check_msg),
                    timeout=min(timeout_sec / COVE_MAX_QUESTIONS, 15.0),
                )
                check_data = _parse_json_response(
                    check_result.get("assistant_text", "")
                )

                results.append(VerificationResult(
                    question=q.get("question", ""),
                    original_claim=q.get("claim", ""),
                    verified_answer=verified_answer,
                    is_consistent=check_data.get("is_consistent", True),
                    confidence=float(check_data.get("confidence", 0.5)),
                ))
            except asyncio.TimeoutError:
                results.append(VerificationResult(
                    question=q.get("question", ""),
                    original_claim=q.get("claim", ""),
                    verified_answer="timeout",
                    is_consistent=True,  # assume true on timeout
                    confidence=0.3,
                ))
            except Exception as exc:
                self._log(f"[CoVe] Check failed: {exc}")

        return results

    def _build_corrected_response(
        self, original: str, verifications: list[VerificationResult]
    ) -> str:
        """Build a corrected response with inconsistency warnings."""
        inconsistent = [v for v in verifications if not v.is_consistent]
        if not inconsistent:
            return original

        notes = [
            "\n\n[NOTA: Verification detected potential inaccuracies]",
        ]
        for v in inconsistent:
            notes.append(
                f"- Claim: \"{v.original_claim[:150]}\"\n"
                f"  Verified: \"{v.verified_answer[:150]}\""
            )
        return original + "\n".join(notes)
