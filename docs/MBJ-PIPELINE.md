# MIKE — Pipeline MBJ (Maker → Breaker → Judge)

## Visão Geral

O MBJ é um protocolo de verificação adversarial em 3 estágios. Nenhum agente confia em outro. Toda afirmação é atacada. Toda decisão exige evidência.

```
┌──────────────────────────────────────────────────────────────┐
│                    MBJ PIPELINE                              │
│                                                              │
│  ┌──────────────────┐                                       │
│  │  PROVER-BUILDER  │  Cientista criativo                   │
│  │  temp: 0.4-0.7    │  Output: solução + claims + testes    │
│  └────────┬─────────┘                                       │
│           │ solução congelada                                │
│  ┌────────▼─────────┐                                       │
│  │  ADVERSARIAL     │  Promotor paranoico                   │
│  │  VERIFIER        │  Output: relatório de falhas          │
│  │  temp: 0.2-0.4    │  8 frentes de ataque simultâneas     │
│  └────────┬─────────┘                                       │
│           │ relatório de falhas                              │
│  ┌────────▼─────────┐                                       │
│  │  ARBITER-        │  Juiz conservador                     │
│  │  REPAIRER        │  Output: veredito + patches mínimos   │
│  │  temp: 0.0-0.2    │  Reproduz testes críticos            │
│  └────────┬─────────┘                                       │
│           │                                                  │
│     ┌─────▼──────┐                                          │
│     │  VEREDITO   │                                         │
│     │  ACCEPT     │→ entrega                                │
│     │  WARN       │→ entrega com notas                      │
│     │  REPAIR     │→ patch → re-verifica áreas alteradas    │
│     │  REGENERATE │→ volta ao Builder (máx 1x)              │
│     │  ESCALATE   │→ intervenção humana                     │
│     └────────────┘                                          │
│                                                              │
│  Limite: 2 ciclos completos, 1 REGENERATE máximo             │
│  Se S4 persistir → ESCALATE_TO_HUMAN                        │
└──────────────────────────────────────────────────────────────┘
```

## Os 3 Agentes

### 1. Prover-Builder (`mbj-builder`)
**Personalidade**: Cientista/engenheiro criativo
**Temperatura**: 0.4–0.7
**Modelo**: Opus (alto raciocínio)

**Função**: Resolver a tarefa e produzir um **pacote verificável**:
- Solução proposta
- Claims (afirmações falsificáveis com evidências)
- Premissas assumidas
- Testes executados
- Cálculos
- Incertezas declaradas

**Regra**: Toda afirmação sem evidência é rejeitada automaticamente.

### 2. Adversarial-Verifier (`mbj-verifier`)
**Personalidade**: Promotor paranoico e auditor técnico
**Temperatura**: 0.2–0.4
**Modelo**: Opus (máximo esforço)
**Ferramentas**: Read-only (não escreve código)

**8 Frentes de Ataque**:
| Frente | Função |
|--------|--------|
| A. Claim Extractor | Extrai cada afirmação verificável |
| B. Threat-Model Router | Classifica falhas (FACTUAL, LOGIC, CALC, SOURCE, etc.) |
| C. Independent Solver | Resolve ANTES de ler o Builder |
| D. Counterexample Engine | Procura o menor contraexemplo |
| E. Evidence Auditor | Abre e verifica fontes |
| F. Tool-Trace Auditor | Audita chamadas de ferramentas |
| G. Metamorphic Testing | Modifica input e verifica coerência |
| H. Severity System | Classifica S0 (estilo) a S4 (crítico) |

**Regra Suprema**: Todo FAIL precisa de evidência, teste, contradição ou contraexemplo.

### 3. Arbiter-Repairer (`mbj-arbiter`)
**Personalidade**: Juiz conservador e objetivo
**Temperatura**: 0.0–0.2
**Modelo**: Opus (alto raciocínio)

**Processo**:
1. Lê Builder + Verifier (sem confiar em nenhum)
2. Reproduz testes críticos (S4 → S3 → S2)
3. Decide quais acusações procedem
4. Aplica o menor reparo possível
5. Re-verifica áreas alteradas
6. Emite veredito final

## Sistema de Severidade

| Nível | Nome | Exemplo |
|-------|------|---------|
| **S0** | Estilístico | Wording, formatação |
| **S1** | Imprecisão leve | Detalhe menor, não afeta conclusão |
| **S2** | Erro localizado | Uma claim errada, outras ok |
| **S3** | Conclusão comprometida | Solução final está errada |
| **S4** | Falha crítica | Segurança, OOM, perda de dados |

## Tipos de Falha (Threat Model)

| Código | Tipo | Descrição |
|--------|------|-----------|
| FACTUAL | Erro factual | Informação objectivamente errada |
| LOGIC | Erro lógico | Conclusão não segue das premissas |
| CALC | Cálculo incorreto | Erro aritmético |
| SOURCE | Fonte inválida | Evidência não sustenta a afirmação |
| OUTDATED | Desatualizado | Tech/versão obsoleta |
| HIDDEN_PREMISE | Premissa escondida | Assume algo sem declarar |
| INCOMPAT | Incompatibilidade | Não funciona no HW/SW alvo |
| TOOL_ERR | Erro de ferramenta | Flag inválida, output ignorado |
| REQ_MISS | Requisito ignorado | Esqueceu constraint importante |
| SECURITY | Vulnerabilidade | Falha de segurança |
| RIGHT_WRONG_REASON | Certo pelo motivo errado | Resposta certa, raciocínio inválido |

## Uso no MIKE

### Via Claude Code (Agentes)
```
/swat-lead "Usa o pipeline MBJ para verificar a configuração do llama-server"
```

O swat-lead orquestra os 3 agentes em sequência.

### Via TaskMesh (Autónomo)
```python
from core.orchestration import TaskMesh

mesh = TaskMesh()
result = mesh.run_pipeline("mbj", task="Otimizar VRAM para RTX 2070")
# Internamente: Builder → Verifier → Arbiter
```

### Em Hardware Limitado (8GB)
Os 3 estágios usam o MESMO modelo sequencialmente:
1. Carrega prompt do Builder → executa → guarda output → liberta contexto
2. Carrega prompt do Verifier + output do Builder → executa → liberta
3. Carrega prompt do Arbiter + outputs anteriores → executa → emite veredito

Não é necessário ter 3 modelos em simultâneo.

## Proteções

1. **Contextos isolados** — cada estágio tem contexto limpo
2. **Output congelado** — Verifier não pode editar o output do Builder
3. **Prompt injection protection** — output do Builder é tratado como dados não confiáveis
4. **Model diversity** — idealmente usar modelos de famílias diferentes para cada estágio
5. **Deterministic Arbiter** — temperatura 0 para decisões críticas

## Ficheiros

| Ficheiro | Agente |
|----------|--------|
| `.claude/agents/mbj-builder.md` | Prover-Builder |
| `.claude/agents/mbj-verifier.md` | Adversarial-Verifier |
| `.claude/agents/mbj-arbiter.md` | Arbiter-Repairer |

## Referência

Arquitetura baseada no protocolo Maker→Breaker→Judge para verificação adversarial de outputs de LLMs, com 8 frentes de ataque independentes, sistema de severidade S0-S4, e proteção contra manipulação e vieses de juízes LLM.
