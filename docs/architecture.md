# Arquitetura: as camadas e o que cada uma não pode importar

Este documento é **verificado por máquina**: `tests/test_architecture_boundaries.py` lê a lista de
camadas do contrato em `pyproject.toml` e falha se alguma não aparecer aqui. O diagrama não pode
divergir do contrato.

## As camadas

```text
┌──────────────────────────────┐
│  jobsearch_agent             │  orquestra a candidatura: prepare → fill →
│  (loop, browser, gate,       │  gate de challenge → submit → observe.
│   relay do operador)         │  Pode importar tudo abaixo.
└───────────┬──────────────────┘
            │  pode importar
            ▼
┌──────────────────────────────┐
│  challenge_resolution        │  contratos + orquestrador executável
│  (orquestrador, journal,     │  (limites, rounds, journal, proveniência),
│   validador, Protocols,      │  validador de observação, engine/estratégias e
│   capability matrix)         │  capability matrix. NÃO importa jobsearch_agent
│                              │  e não alcança a fronteira de escrita
│                              │  (AuthorizedWrite / SubmissionIntent /
│                              │  NetworkWriteGuard). Contratos SÍNCRONOS:
│                              │  o produto e a API do Playwright são síncronos.
└───────────┬──────────────────┘
            │  pode importar
            ▼
┌──────────────────────────────┐
│  challenge_guard             │  percepção pura (detecta, classifica, observa,
│  (dependência externa,       │  decide) e NUNCA resolve. É o fundo da pilha:
│   fixada por SHA)            │  não importa nenhuma das camadas de cima.
└──────────────────────────────┘
```

## As regras, e onde cada uma é verificada

| # | Regra | Anel que verifica |
| --- | --- | --- |
| 1 | `jobsearch_agent` pode importar as camadas de baixo | import-linter (camadas) |
| 2 | `challenge_resolution` não importa `jobsearch_agent` | import-linter + AST + runtime |
| 3 | `challenge_resolution` não alcança `browser`/`submission`/`submission_browser` | import-linter |
| 4 | execução (relay do operador, handoff) não depende do motor de resolução | import-linter |
| 4b | só `challenge_integration.py` e `challenge_strategies.py` importam o pacote de resolução | AST walker + invariantes |
| 5 | o handoff assíncrono não fala com o browser (direto, não transitivo) | import-linter |
| 6 | `challenge_guard` não importa nenhuma das camadas de cima | AST sobre o artefato instalado |
| 7 | nomes proibidos não aparecem como código, `__all__`, anotação ou campo de dataclass | AST walker |
| 8 | nenhum módulo proibido entra em `sys.modules` ao importar o pacote de resolução (inclusive import dinâmico) | runtime scanner |
| 9 | o agente não reexporta internals dos pacotes protegidos | AST walker |

## Onde cada handoff vive

Há dois caminhos de intervenção humana, com papéis distintos — e nomes distintos de propósito:

- **handoff assíncrono** (`jobsearch_agent.handoff`): o agente prepara o pacote (currículo + respostas
  aprovadas + proveniência) e a pessoa conclui a candidatura **no navegador dela**. O agente registra
  o relato e só então procura evidência independente (ADR 0005/0006). Não toca browser.
- **relay do operador** (`jobsearch_agent.live_view`): a pessoa age **na sessão viva** durante a
  janela do gate, com o controle de envio desabilitado e clique sobre ele recusado. É execução, não
  decisão — por isso não importa o motor de resolução (regra 4).

## Os quatro anéis

| Anel | Ferramenta | Comando |
| --- | --- | --- |
| 1 — dependências entre pacotes | import-linter | `lint-imports` |
| 2 — tipos, campos e nomes proibidos | AST walker | `pytest tests/test_architecture_boundaries.py` |
| 3 — anotações | mypy strict | `mypy` (config em `pyproject.toml`) |
| 4 — imports dinâmicos | runtime scanner | `pytest tests/test_runtime_module_isolation.py` |
| 5 — contratos não vacuosos | selfcheck | `pytest tests/test_contracts_selfcheck.py` |

O anel 5 existe porque um contrato que nunca falha não testa nada: ele introduz uma violação
deliberada e exige que o anel correspondente a detecte.
