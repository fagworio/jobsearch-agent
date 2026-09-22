# jobsearch-agent

Agente local para descobrir, analisar e preparar candidaturas com currículo específico por vaga.

Esta primeira milestone implementa **Analyze + Generate**: ingestão de uma vaga, análise, fit,
estratégia de currículo, validação factual e saída TXT/DOCX/PDF. Não há submissão de candidatura,
Playwright ou envio de dados para ATS.

## Uso rápido

Instale o pacote com Poetry (`poetry install`) e use o script `jobsearch-agent`. Durante o
desenvolvimento sem instalação, prefixe os comandos com `PYTHONPATH=src`.

As dependências principais incluem Pydantic, httpx, BeautifulSoup, Lingua, RapidFuzz e
python-docx. A integração JobSpy é opcional e pode ser instalada com o grupo de discovery
(`poetry install --with discovery`). Playwright permanece opcional e, nesta fase, só pode ser
usado para inspeção/dry-run; nenhuma API de submissão existe no executor. O código mantém um modo mínimo para executar fixtures em ambientes sem as
dependências opcionais instaladas; a instalação de produção deve usar o ambiente Poetry.

```bash
PYTHONPATH=src python3 -m jobsearch_agent.cli --help
PYTHONPATH=src python3 -m jobsearch_agent.cli profile validate --profile profile/career_profile.yaml --facts profile/locked_facts.yaml
PYTHONPATH=src python3 -m jobsearch_agent.cli run --json-file tests/fixtures/jobs/greenhouse.json --profile profile/career_profile.yaml --facts profile/locked_facts.yaml --db data/jobsearch.db --artifacts data/applications

# cria/retoma a candidatura persistida, sem browser e sem submit
PYTHONPATH=src python3 -m jobsearch_agent.cli application prepare <job-id> --db data/jobsearch.db
PYTHONPATH=src python3 -m jobsearch_agent.cli application resume <application-id> --db data/jobsearch.db
PYTHONPATH=src python3 -m jobsearch_agent.cli application status <application-id> --db data/jobsearch.db
```

Para uma vaga pública:

```bash
jobsearch-agent run --url https://boards.greenhouse.io/example/jobs/123
```

Com o grupo opcional de discovery instalado:

```bash
jobsearch-agent search --query "Senior WordPress Developer" --sites indeed,google --location Brazil
```

O provider sem configuração usa análise determinística e geração segura. Para interpretação LLM,
configure `JOBSEARCH_LLM_BASE_URL`, `JOBSEARCH_LLM_API_KEY` e `JOBSEARCH_LLM_MODEL`. O perfil
demonstrativo contém fatos fictícios e é bloqueado quando `--real-profile` é usado.

Preferências mutáveis são carregadas de `profile/preferences.yaml` (opção `--preferences`) e
vencem preferências legadas dentro de `career_profile.yaml`. Identidades fortes de vaga são
namespaced por fonte/ATS e podem consolidar registros; identidades fracas apenas criam entradas
auditáveis em `duplicate_candidates` para revisão. O SQLite aplica migrations versionadas e
transacionais automaticamente.

O registry de skills vive em [knowledge/skills.yaml](knowledge/skills.yaml), com uma cópia
empacotável em `src/jobsearch_agent/knowledge/skills.yaml`. Matching segue aliases exatos,
matching fuzzy e, somente quando configurado, enriquecimento semântico por LLM.

## Princípios

- O Career Profile e os locked facts são a fonte factual canônica.
- Todo claim gerado mantém `fact_id` rastreável.
- Falta de suporte factual bloqueia o artefato.
- O fit é separado da prontidão para candidatura.
- `Job` e `Application` possuem ciclos de vida independentes; interrupções como
  `NEEDS_ANSWER`, `NEEDS_LOGIN`, `NEEDS_MFA`, `NEEDS_CAPTCHA` e `UNSUPPORTED_FORM`
  são estados de domínio, não erros técnicos.
- `NEEDS_ARTIFACT` representa um upload ausente ou inválido e pode ser retomado depois da
  correção do artefato.
- `ApplicationPolicy` controla autonomia e segurança separadamente de `CandidatePreferences`.
- `READY_TO_APPLY` só é emitido com `ApplicationForm` conhecido e campos obrigatórios resolvidos;
  `submit: manual` continua exigindo revisão antes do envio.
- O Browser Dry Run valida opções, checkboxes e artefatos de upload antes de gerar um
  `ExecutionPlan`; o plano contém somente `fill`/`upload` e termina em `STOP_BEFORE_SUBMIT`.
- O ATS Inspector atual é somente leitura: produz `ApplicationForm` e `FormBindings` separados,
  sem seletores DOM no domínio e sem preencher controles.
- Cada plano registra o fingerprint do formulário e dos bindings; o executor pode revalidar o
  HTML atual antes de qualquer preenchimento e recusa snapshots obsoletos ou ambíguos.
- Uploads exigem `ApplicationForm.artifact_root`, permanecem dentro desse diretório e têm o
  conteúdo parseável verificado antes de entrar no plano de execução; o plano registra SHA-256
  e revalida o hash no limite do executor.
- Campos desabilitados são tratados como inativos até uma nova inspeção; falhas de resposta,
  opção, campo e artefato são reportadas com blockers distintos no Safety Gate.
- A sessão Playwright usa contexto isolado e bloqueia URLs não HTTP(S), locais, privadas,
  link-local ou reservadas, inclusive em redirecionamentos.
- O job opcional `browser-runtime` executa um fixture local em Chromium para provar inspeção,
  preenchimento, upload e screenshot sem qualquer submit.
- O contexto dry-run bloqueia POST/PUT/PATCH/DELETE, WebSocket, `sendBeacon` e submissões de
  formulário; o `NetworkWriteGuard` mantém evidência agregada por origem e hash de path, sem
  valores literais, corpos ou query strings. Cada mutação aguarda uma janela mínima de observação,
  estabilidade por `MutationObserver` e zero de leituras pendentes antes de revalidar o DOM.
  Instabilidades interrompem com `DOM_UNSTABLE` e alterações estruturais com `FORM_CHANGED`.
- O filler pode persistir um relatório redigido em `browser/`, com fingerprints, operações,
  screenshots e indicação explícita de que nenhuma escrita de rede foi permitida. O diretório de
  auditoria fica sob `artifact_root` com modo `0700`; seus arquivos ficam em `0600`.
- O `GreenhouseAdapter` permanece separado do `ATSInspector`: identifica a assinatura estrutural,
  encontra o root da aplicação e enriquece `ApplicationField` com semantic type, confiança e
  origem. Ele não responde perguntas nem executa ações de browser; campos customizados continuam
  `unknown` e podem bloquear no Safety Gate. Controles não representados pelo domínio viram
  `FormCapabilityIssue` e levam a `UNSUPPORTED_FORM`, enquanto autorização de trabalho exige
  jurisdição explícita e confiança mínima antes de ser respondida automaticamente.
- A milestone Prepare Application não abre browser nem envia candidaturas.
- A CLI imprime JSON por padrão para ser consumida pelo Hermes.
