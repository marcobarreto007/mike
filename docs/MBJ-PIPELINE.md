# Pipeline MBJ — Maker, Breaker, Judge

O MBJ é um procedimento de revisão adversarial em três estágios:

1. **Maker/Builder** produz uma solução verificável;
2. **Breaker/Verifier** tenta refutar claims e encontrar falhas;
3. **Judge/Arbiter** reproduz evidências, decide e propõe o menor reparo.

## Estado real da implementação

Os três prompts existem:

```text
.claude\agents\mbj-builder.md
.claude\agents\mbj-verifier.md
.claude\agents\mbj-arbiter.md
```

Eles podem ser orquestrados por um agente principal que suporte esses arquivos.
O runtime Python do MIKE **não possui atualmente** um método
`TaskMesh.run_pipeline("mbj", ...)`. Essa chamada aparecia na documentação
antiga, mas nunca foi uma API válida e foi removida deste guia.

O `TaskMesh` real continua responsável por planejar e executar tarefas
complexas, porém não implementa automaticamente os três papéis MBJ.

## Contrato do Builder

O Builder deve entregar:

- solução proposta;
- claims falsificáveis;
- evidência para cada claim;
- premissas;
- testes executados e resultados;
- riscos e incertezas.

Uma afirmação sem evidência deve ser marcada como hipótese, não como fato.

## Contrato do Verifier

O Verifier trabalha em modo de auditoria e tenta:

- reproduzir os testes;
- resolver o problema de forma independente;
- encontrar contraexemplos;
- verificar fontes e tool traces;
- identificar premissas escondidas;
- classificar cada achado por severidade.

O Verifier não altera silenciosamente a solução do Builder.

## Contrato do Arbiter

O Arbiter:

1. lê Builder e Verifier sem confiar automaticamente em nenhum;
2. reproduz primeiro os achados mais graves;
3. aceita ou rejeita cada acusação com evidência;
4. aplica ou recomenda o menor reparo;
5. reexecuta as verificações afetadas;
6. emite o veredito.

## Severidade

| Nível | Significado |
|---|---|
| S0 | estilo ou apresentação |
| S1 | imprecisão sem impacto material |
| S2 | erro localizado |
| S3 | conclusão comprometida |
| S4 | segurança, perda de dados, OOM ou falha crítica |

## Vereditos

| Veredito | Uso |
|---|---|
| ACCEPT | evidência suficiente, sem falha material |
| WARN | entrega aceitável com limitações explícitas |
| REPAIR | correção localizada e nova verificação |
| REGENERATE | solução precisa ser refeita |
| ESCALATE | decisão humana necessária |

## Uso recomendado

Peça explicitamente ao orquestrador:

```text
Execute esta tarefa com o pipeline MBJ:
1. mbj-builder produz solução e evidências;
2. mbj-verifier audita sem editar;
3. mbj-arbiter reproduz os achados e emite o resultado final.
```

Em hardware limitado, use os três estágios sequencialmente com o mesmo modelo.
Não mantenha três contextos de 16K simultaneamente.

## Limites

- máximo recomendado de dois ciclos completos;
- no máximo uma regeneração;
- S4 persistente deve ser escalado;
- outputs entre estágios são dados não confiáveis;
- o Arbiter precisa executar verificações, não apenas “votar”.

## Relação com o TaskMesh

O TaskMesh pode executar os passos concretos produzidos por um MBJ, mas não
substitui o protocolo de revisão. Uma integração futura deve ser adicionada
com API, testes e documentação antes de ser apresentada como disponível.
