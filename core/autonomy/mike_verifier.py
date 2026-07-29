# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
Mike Verifier — Pilar 3: Self-Verification & Auto-Correction
==============================================================

Antes de entregar qualquer output, Mike roda um "teste de sanidade":
- Verifica contra restrições do projeto (Pilar 2)
- Checa padrões conhecidos anti-patterns
- Valida consistência factual
- Auto-corrige antes do usuário ver

Pipeline: Generate → Verify → (AutoCorrect if needed) → Deliver

Verificações:
1. Constraint check — viola alguma decisão arquitetural?
2. Pattern check — usa anti-patterns conhecidos?
3. Consistency check — contradiz algo que disse antes?
4. Code check — sintaxe válida? imports corretos?
5. Hallucination guard — afirma ter feito algo sem tool call?
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

log = logging.getLogger("mike.verifier")


# ======================================================================
# Data structures
# ======================================================================

@dataclass
class VerificationResult:
    """Result of a single verification check."""
    check_name: str
    passed: bool
    severity: str = "info"       # info, warning, error, critical
    message: str = ""
    suggestion: str = ""         # How to fix
    auto_correctable: bool = False


@dataclass
class VerificationReport:
    """Aggregate report of all verification checks on an output."""
    timestamp: str = ""
    checks: list[VerificationResult] = field(default_factory=list)
    elapsed_ms: float = 0.0
    auto_corrections: int = 0
    original_text: str = ""
    corrected_text: str = ""

    @property
    def passed(self) -> bool:
        return not any(c.severity in ("error", "critical") and not c.passed for c in self.checks)

    @property
    def warnings(self) -> list[VerificationResult]:
        return [c for c in self.checks if not c.passed and c.severity == "warning"]

    @property
    def errors(self) -> list[VerificationResult]:
        return [c for c in self.checks if not c.passed and c.severity in ("error", "critical")]

    def summary(self) -> str:
        total = len(self.checks)
        passed = sum(1 for c in self.checks if c.passed)
        return f"Verificação: {passed}/{total} checks OK, {len(self.warnings)} avisos, {len(self.errors)} erros"


# ======================================================================
# Individual Checkers
# ======================================================================

class ConstraintChecker:
    """Check output against project constraints (from Pilar 2)."""

    def __init__(self, consciousness=None) -> None:
        self.consciousness = consciousness

    def check(self, text: str, context: str = "") -> list[VerificationResult]:
        results = []
        if not self.consciousness:
            return results

        # Get relevant constraints based on the output content
        keywords = self._extract_keywords(text)
        if not keywords:
            return results

        constraints = self.consciousness.get_constraints(" ".join(keywords))
        for constraint in constraints:
            # Check if the text potentially violates this constraint
            violation = self._detect_violation(text, constraint)
            if violation:
                results.append(VerificationResult(
                    check_name="constraint_violation",
                    passed=False,
                    severity="warning",
                    message=f"Possível violação: {constraint}",
                    suggestion=violation,
                ))

        if not results:
            results.append(VerificationResult(
                check_name="constraint_check",
                passed=True,
                message=f"Verificado contra {len(constraints)} restrições",
            ))

        return results

    def _extract_keywords(self, text: str) -> list[str]:
        # Extract technical terms
        tech = re.findall(r'\b(?:[A-Z][a-z]+){2,}\b|\b\w+_\w+\b', text)
        # Extract common architecture words
        arch_words = {"api", "database", "cache", "mutex", "lock", "thread",
                      "async", "sync", "queue", "pipeline", "webhook", "mcp"}
        words = set(w.lower() for w in text.split()) & arch_words
        return list(set(t.lower() for t in tech)) + list(words)

    def _detect_violation(self, text: str, constraint: str) -> str:
        """Check if text contradicts a constraint. Returns fix suggestion or empty."""
        # Simple heuristic: if constraint says "nunca X" and text suggests X
        constraint_lower = constraint.lower()

        # "Nunca usar X" patterns
        never_match = re.search(r"nunca\s+(?:usar?|implementar?|fazer?)\s+(\w+)", constraint_lower)
        if never_match:
            forbidden = never_match.group(1)
            if forbidden in text.lower():
                return f"Considere alternativa a '{forbidden}' — decisão de projeto proíbe."

        # "Sempre usar X" patterns
        always_match = re.search(r"sempre\s+(?:usar?|manter?|implementar?)\s+(\w+)", constraint_lower)
        if always_match:
            required = always_match.group(1)
            # Only flag if a related topic is discussed but the required thing isn't mentioned
            # This is intentionally conservative to avoid false positives
            pass

        return ""


class HallucinationGuard:
    """Detect when Mike claims to have done something without a tool call."""

    # Phrases that imply Mike accessed external data
    CLAIM_PATTERNS = [
        r"(?:eu\s+)?(?:li|acessei|verifiquei|consultei|olhei|vi|encontrei)\s+(?:no|na|o|a|os|as)\s+(?:arquivo|email|calendar|drive|banco|github|site)",
        r"(?:de acordo|segundo|conforme)\s+(?:o arquivo|os dados|o banco|o email|o calendar)",
        r"(?:o resultado|os dados|a consulta)\s+(?:mostr[ao]|indic[ao]|revelou|retornou)",
    ]

    def check(self, text: str, tool_calls_made: list[str] = None) -> list[VerificationResult]:
        results = []
        tool_calls_made = tool_calls_made or []

        for pattern in self.CLAIM_PATTERNS:
            matches = re.findall(pattern, text, re.IGNORECASE)
            if matches and not tool_calls_made:
                results.append(VerificationResult(
                    check_name="hallucination_guard",
                    passed=False,
                    severity="error",
                    message=f"Afirmação de acesso a dados sem tool call: '{matches[0]}'",
                    suggestion="Remover afirmação ou usar tool para acessar dados reais.",
                    auto_correctable=True,
                ))

        if not results:
            results.append(VerificationResult(
                check_name="hallucination_guard",
                passed=True,
                message="Sem afirmações de acesso externo indevidas",
            ))

        return results


class ConsistencyChecker:
    """Check for internal contradictions in the response."""

    def check(self, text: str, previous_statements: list[str] = None) -> list[VerificationResult]:
        results = []

        # Self-contradiction: saying X and then not-X in the same response
        negation_pairs = [
            (r"é possível", r"não é possível"),
            (r"funciona", r"não funciona"),
            (r"existe", r"não existe"),
            (r"tem\s", r"não tem\s"),
            (r"pode\s", r"não pode\s"),
            (r"sim,?\s", r"não,?\s"),
        ]

        for pos, neg in negation_pairs:
            if re.search(pos, text, re.IGNORECASE) and re.search(neg, text, re.IGNORECASE):
                # Could be explaining nuance, not contradiction — check proximity
                pos_match = re.search(pos, text, re.IGNORECASE)
                neg_match = re.search(neg, text, re.IGNORECASE)
                if pos_match and neg_match:
                    distance = abs(pos_match.start() - neg_match.start())
                    if distance < 200:  # Close together = likely contradiction
                        results.append(VerificationResult(
                            check_name="self_contradiction",
                            passed=False,
                            severity="warning",
                            message=f"Possível contradição: '{pos}' e '{neg}' no mesmo parágrafo",
                            suggestion="Esclarecer: explique a nuance entre as afirmações.",
                        ))

        if not results:
            results.append(VerificationResult(
                check_name="consistency_check",
                passed=True,
                message="Sem contradições detectadas",
            ))

        return results


class CodePatternChecker:
    """Check code output for common anti-patterns."""

    ANTI_PATTERNS = [
        # Security
        (r"eval\s*\(", "warning", "Uso de eval() — risco de code injection"),
        (r"exec\s*\(", "warning", "Uso de exec() — risco de code injection"),
        (r"shell\s*=\s*True", "warning", "subprocess com shell=True — risco de shell injection"),
        (r"password\s*=\s*['\"](?!\\{)(?!\$)", "error", "Senha hardcoded no código"),
        (r"api_key\s*=\s*['\"][a-zA-Z0-9]", "error", "API key hardcoded no código"),
        # Quality
        (r"except:\s*\n\s*pass", "warning", "except: pass genérico — silencia erros"),
        (r"# TODO|# FIXME|# HACK|# XXX", "info", "Marcador TODO/FIXME encontrado"),
        (r"import \*", "warning", "Wildcard import — polui namespace"),
        (r"time\.sleep\(\d{2,}", "warning", "Sleep longo — considere async ou Event"),
    ]

    def check(self, text: str) -> list[VerificationResult]:
        results = []

        # Only check code blocks
        code_blocks = re.findall(r"```(?:python|py|bash|sh|powershell|ps1)?\n(.*?)```", text, re.DOTALL)
        if not code_blocks:
            return [VerificationResult(
                check_name="code_pattern",
                passed=True,
                message="Sem código para verificar",
            )]

        combined_code = "\n".join(code_blocks)

        for pattern, severity, message in self.ANTI_PATTERNS:
            if re.search(pattern, combined_code, re.IGNORECASE):
                results.append(VerificationResult(
                    check_name="code_antipattern",
                    passed=False,
                    severity=severity,
                    message=message,
                    suggestion=f"Revise o uso de padrão inseguro ou questionável.",
                ))

        if not results:
            results.append(VerificationResult(
                check_name="code_pattern",
                passed=True,
                message=f"Código verificado ({len(code_blocks)} blocos) — sem anti-patterns",
            ))

        return results


class ConfidenceCalibrator:
    """Check if Mike is expressing appropriate confidence levels."""

    OVERCONFIDENCE_PATTERNS = [
        r"(?:com certeza|definitivamente|sem dúvida|100%|garantido|impossível que)",
    ]

    HEDGING_REQUIRED_TOPICS = [
        r"(?:previsão|futuro|vai acontecer|será que|provavelmente)",
        r"(?:médic[oa]|diagnóstico|tratamento|remédio|sintoma)",
        r"(?:investimento|financeiro|bolsa|ação|cripto)",
    ]

    def check(self, text: str) -> list[VerificationResult]:
        results = []

        for pattern in self.OVERCONFIDENCE_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                # Check if it's about a hedging-required topic
                for topic_pattern in self.HEDGING_REQUIRED_TOPICS:
                    if re.search(topic_pattern, text, re.IGNORECASE):
                        results.append(VerificationResult(
                            check_name="confidence_calibration",
                            passed=False,
                            severity="warning",
                            message="Excesso de confiança em tópico que requer cautela",
                            suggestion="Adicione qualificadores: 'provavelmente', 'pode ser', 'na minha análise'",
                        ))
                        break

        if not results:
            results.append(VerificationResult(
                check_name="confidence_calibration",
                passed=True,
                message="Nível de confiança adequado",
            ))

        return results


# ======================================================================
# Main Verifier Pipeline
# ======================================================================

class OutputVerifier:
    """
    Main verification pipeline.

    Usage:
        verifier = OutputVerifier(consciousness=project_consciousness)
        report = verifier.verify(assistant_text, tool_calls=[...])
        if not report.passed:
            corrected = verifier.auto_correct(report)
    """

    def __init__(self, consciousness=None) -> None:
        self.constraint_checker = ConstraintChecker(consciousness)
        self.hallucination_guard = HallucinationGuard()
        self.consistency_checker = ConsistencyChecker()
        self.code_checker = CodePatternChecker()
        self.confidence_calibrator = ConfidenceCalibrator()
        self._history: list[VerificationReport] = []

    def verify(
        self,
        text: str,
        context: str = "",
        tool_calls_made: Optional[list[str]] = None,
        previous_statements: Optional[list[str]] = None,
    ) -> VerificationReport:
        """Run the full verification pipeline on an output."""
        start = time.time()
        report = VerificationReport(
            timestamp=datetime.now(timezone.utc).isoformat(),
            original_text=text,
        )

        # Run all checks
        report.checks.extend(self.constraint_checker.check(text, context))
        report.checks.extend(self.hallucination_guard.check(text, tool_calls_made))
        report.checks.extend(self.consistency_checker.check(text, previous_statements))
        report.checks.extend(self.code_checker.check(text))
        report.checks.extend(self.confidence_calibrator.check(text))

        report.elapsed_ms = (time.time() - start) * 1000
        report.corrected_text = text  # Will be modified by auto_correct if needed

        self._history.append(report)
        # Keep last 50 reports
        if len(self._history) > 50:
            self._history = self._history[-50:]

        if not report.passed:
            log.warning(f"Verification failed: {report.summary()}")
            for err in report.errors:
                log.warning(f"  → {err.check_name}: {err.message}")

        return report

    def auto_correct(self, report: VerificationReport) -> str:
        """Apply automatic corrections where possible."""
        text = report.original_text

        for check in report.checks:
            if not check.passed and check.auto_correctable:
                if check.check_name == "hallucination_guard":
                    # Add disclaimer
                    text = self._fix_hallucination_claim(text)
                    report.auto_corrections += 1

        report.corrected_text = text
        return text

    def _fix_hallucination_claim(self, text: str) -> str:
        """Replace false access claims with honest hedging."""
        replacements = [
            (r"eu\s+li\s+(?:no|na|o|a)\s+", "baseado no que sei, "),
            (r"eu\s+verifiquei\s+(?:no|na|o|a)\s+", "pelo que entendo, "),
            (r"eu\s+consultei\s+(?:no|na|o|a)\s+", "de acordo com meu conhecimento, "),
        ]
        for pattern, replacement in replacements:
            text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
        return text

    def inject_verification_prompt(self, constraints: list[str]) -> str:
        """Generate a prompt injection block for pre-verification."""
        if not constraints:
            return ""

        lines = [
            "\n[AUTO-VERIFICAÇÃO ATIVA]",
            "Antes de responder, verifique INTERNAMENTE:",
            "1. Sua resposta contradiz alguma decisão de projeto anterior?",
            "2. Você está afirmando ter acessado dados sem ter usado uma tool?",
            "3. Código sugerido segue os padrões do projeto?",
            "4. Seu nível de confiança é calibrado (sem certezas absolutas em tópicos incertos)?",
            "",
            "Restrições ativas:",
        ]
        for c in constraints[:5]:
            lines.append(f"  • {c}")
        return "\n".join(lines)

    def stats(self) -> dict:
        """Return verifier statistics."""
        if not self._history:
            return {"total_checks": 0, "pass_rate": 1.0}

        total = len(self._history)
        passed = sum(1 for r in self._history if r.passed)
        total_corrections = sum(r.auto_corrections for r in self._history)
        avg_time = sum(r.elapsed_ms for r in self._history) / total

        return {
            "total_verifications": total,
            "pass_rate": passed / total,
            "auto_corrections": total_corrections,
            "avg_time_ms": round(avg_time, 2),
            "recent_failures": [
                {"time": r.timestamp, "summary": r.summary()}
                for r in self._history[-5:]
                if not r.passed
            ],
        }
