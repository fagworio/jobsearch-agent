# jobsearch-agent

Agente local para descobrir, analisar e preparar candidaturas com currículo específico por vaga.

Esta primeira milestone implementa **Analyze + Generate**: ingestão de uma vaga, análise, fit,
estratégia de currículo, validação factual e saída TXT/DOCX/PDF. Não há submissão de candidatura,
Playwright ou envio de dados para ATS.

## Uso rápido

Instale o pacote com Poetry (`poetry install`) e use o script `jobsearch-agent`. Durante o
desenvolvimento sem instalação, prefixe os comandos com `PYTHONPATH=src`.

```bash
PYTHONPATH=src python3 -m jobsearch_agent.cli --help
PYTHONPATH=src python3 -m jobsearch_agent.cli profile validate --profile profile/career_profile.yaml --facts profile/locked_facts.yaml
PYTHONPATH=src python3 -m jobsearch_agent.cli run --json-file tests/fixtures/jobs/greenhouse.json --profile profile/career_profile.yaml --facts profile/locked_facts.yaml --db data/jobsearch.db --artifacts data/applications
```

Para uma vaga pública:

```bash
jobsearch-agent run --url https://boards.greenhouse.io/example/jobs/123
```

O provider sem configuração usa análise determinística e geração segura. Para interpretação LLM,
configure `JOBSEARCH_LLM_BASE_URL`, `JOBSEARCH_LLM_API_KEY` e `JOBSEARCH_LLM_MODEL`. O perfil
demonstrativo contém fatos fictícios e é bloqueado quando `--real-profile` é usado.

## Princípios

- O Career Profile e os locked facts são a fonte factual canônica.
- Todo claim gerado mantém `fact_id` rastreável.
- Falta de suporte factual bloqueia o artefato.
- O fit é separado da prontidão para candidatura.
- A CLI imprime JSON por padrão para ser consumida pelo Hermes.
