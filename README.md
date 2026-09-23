# jobsearch-agent

Agente local para descobrir, analisar e preparar candidaturas com currículo específico por vaga.

Esta milestone implementa **Analyze + Generate** e a base da Submission Boundary: ingestão de uma
vaga, análise, fit, estratégia de currículo, validação factual, saída TXT/DOCX/PDF, `SubmissionIntent`,
autorização explícita, duplicate guard, estados pós-tentativa e executor controlado Greenhouse. LinkedIn
continua manual e o dry-run nunca submete.

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
PYTHONPATH=src python3 -m jobsearch_agent.cli profile readiness
PYTHONPATH=src python3 -m jobsearch_agent.cli run --json-file tests/fixtures/jobs/greenhouse.json --profile profile/career_profile.yaml --facts profile/locked_facts.yaml --db data/jobsearch.db --artifacts data/applications

# cria/retoma a candidatura persistida, sem browser e sem submit
PYTHONPATH=src python3 -m jobsearch_agent.cli application prepare <job-id> --db data/jobsearch.db
PYTHONPATH=src python3 -m jobsearch_agent.cli application resume <application-id> --db data/jobsearch.db
PYTHONPATH=src python3 -m jobsearch_agent.cli application status <application-id> --db data/jobsearch.db

# cria/consulta o Review Snapshot; não envia dados
PYTHONPATH=src python3 -m jobsearch_agent.cli application review <application-id>

# autoriza uma SubmissionIntent já revisada; ainda não faz POST
PYTHONPATH=src python3 -m jobsearch_agent.cli application authorize-submit <intent-id>

# executa somente uma SubmissionIntent Greenhouse autorizada; payload fica em arquivo local
PYTHONPATH=src python3 -m jobsearch_agent.cli application submit <application-id> \
  --intent-id <intent-id> --payload-json /secure/payload.json \
  --form-fingerprint <fingerprint> --resume-sha256 <sha256> \
  --answers-fingerprint <fingerprint>

# dry-run somente local: preenche/valida o snapshot e termina antes de submit
PYTHONPATH=src python3 -m jobsearch_agent.cli dry-run <application-id> \
  --provider linkedin --html-file tests/fixtures/linkedin/easy-apply-single.html \
  --db data/jobsearch.db --artifacts data/applications

# inspeção LinkedIn somente em HTML local; não abre navegador nem acessa rede
jobsearch-agent linkedin inspect <job-id> --html-file tests/fixtures/linkedin/easy-apply-single.html
```

Para uma vaga pública:

```bash
jobsearch-agent run --url https://boards.greenhouse.io/example/jobs/123

# preflight público somente leitura: não preenche e não submete
jobsearch-agent preflight --url https://boards.greenhouse.io/example/jobs/123

# após ingestão, verifica fit e prontidão do perfil; não abre browser
jobsearch-agent precheck <job-id>
```

`profile readiness` separa dados essenciais ausentes (`missing_required`) de dados opcionais ausentes
(`missing_optional`), sem imprimir valores pessoais. O perfil demo pode ser usado para análise e
geração, mas a política bloqueia seu uso em public dry-run fill. `precheck <job-id>` mantém Fit,
Profile e Policy separados; dados opcionais só viram necessários quando o anúncio os exigir
explicitamente. `BLOCKED_FIT`, `NEEDS_PROFILE_DATA` ou `DEMO_PROFILE_BLOCKED` impede iniciar o dry-run;
`dry-run <application-id>` só inicia quando a Application está em `READY_FOR_REVIEW`, `READY_TO_APPLY` ou `REVIEW_REACHED`;
Applications em `DRAFT`, `SUBMITTED`, `SUBMIT_FAILED` ou `SUBMIT_UNKNOWN` são bloqueadas antes da
inspeção do snapshot.
Quando o dry-run começa em `READY_FOR_REVIEW`, sua conclusão local registra a transição explícita
`READY_FOR_REVIEW -> REVIEW_REACHED`; isso representa apenas que a etapa de revisão foi alcançada,
nunca autorização ou submissão. O `ReviewSnapshot` persistido deve conter `form_fingerprint` e
`answers_fingerprint` além do SHA256 do currículo e do destino. `authorize-submit` compara os cinco
vínculos com o `SubmissionIntent` e rejeita snapshots incompletos ou divergentes.
País, nome preferido e fuso horário precisam ser informados explicitamente: não são derivados de
localização ou nome.

Dados reais de candidato ficam nos arquivos locais ignorados pelo Git
`profile/career_profile.local.yaml`, `profile/locked_facts.local.yaml` e
`profile/preferences.local.yaml`. Quando presentes, esses arquivos são selecionados como defaults;
os YAMLs sem sufixo `.local` continuam como fixtures demonstrativas. Não remova a proteção do
`.gitignore` nem publique os arquivos locais, pois podem conter contato e histórico profissional.

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
- `DryRunExecutionPlan` mantém `STOP_BEFORE_SUBMIT`; `LiveApplicationPlan` permite apenas
  `fill`/`upload`/`advance` e termina em `REQUIRE_SUBMIT_AUTHORIZATION`. Submissão não é uma
  `ExecutionAction`.
- `SubmissionIntent` vincula uma autorização expirável à aplicação, vaga, provider, destino,
  fingerprint do formulário, SHA-256 do currículo e fingerprint das respostas. A tentativa é
  persistida antes da rede; confirmação vira `SUBMITTED`, timeout vira `SUBMIT_UNKNOWN` e tentativas
  duplicadas são bloqueadas.
- `LiveNetworkPolicy` é específica por provider e bloqueia método, origem, caminho ou estágio
  inesperados. `authorize-submit` apenas muda o estado para `SUBMIT_AUTHORIZED`; não envia dados.
- O Browser Dry Run valida opções, checkboxes e artefatos de upload antes de gerar um
  `ExecutionPlan`; o plano contém somente `fill`/`upload` e termina em `STOP_BEFORE_SUBMIT`.
- O comando `preflight` abre uma URL pública apenas para validar navegação, provider, root,
  bindings, capability issues e política de rede; ele nunca gera `ExecutionPlan` nem preenche
  controles.
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
- LinkedIn Easy Apply permanece fora da automação ativa: a referência EasyApplyJobsBot foi auditada,
  mas o agente não acessa, inspeciona, preenche nem navega páginas autenticadas do LinkedIn. A
  política publicada pelo LinkedIn proíbe automação de atividade por software de terceiros. Ver
  [ADR 0001](docs/adr/0001-linkedin-reference-and-automation-boundary.md) e
  [auditoria da referência](docs/references/easyapplyjobsbot-audit.md).
- O inspector offline `jobsearch_agent.linkedin.inspector.LinkedInInspector` aceita apenas snapshots HTML
  locais e classifica `EASY_APPLY`, `EXTERNAL_APPLY`, `ALREADY_APPLIED`, `UNAVAILABLE` e
  `AUTH_REQUIRED`. Ele reutiliza `ApplicationForm`/`FormBindings`, marca perguntas de experiência
  com `semantic_type=experience_years` e nunca abre página, responde, navega ou envia.
- `jobsearch_agent.linkedin.session.LinkedInSession` representa somente o handoff manual de uma
  sessão local: guarda `profile_path`, classifica `NEEDS_LOGIN`, `NEEDS_MFA`, `NEEDS_CAPTCHA` e
  `AUTHENTICATED_MANUAL`, e não possui campos ou métodos para senha, código, login, navegação ou
  submit. O usuário informa credenciais diretamente no navegador visível.
- `first_name` e `last_name` só são preenchidos quando existem como atributos explícitos no perfil;
  o agente não divide automaticamente um nome composto. Quando configurados, seus
  `identity_fact_ids` também entram na proveniência da resposta. Campos de localização atual e
  localização preferida para relocação possuem semânticas distintas.
- Country, preferred first name, timezone, telefone, LinkedIn e GitHub são resolvidos somente a
  partir de atributos explícitos. Preferências de timezone aceitam um valor explícito `timezone`
  ou uma única entrada em `timezones`; readiness não infere timezone a partir da cidade.
- O `DryRunApplicationOrchestrator` executa o ciclo bounded `inspect → resolve → Safety Gate →
  plan → fill`. Quando o DOM muda, ele re-inspeciona o formulário, resolve novamente as respostas
  e gera um novo plano; fingerprints repetidos e excesso de ciclos interrompem com status explícito.
- A milestone Prepare Application não abre browser nem envia candidaturas.
- A CLI imprime JSON por padrão para ser consumida pelo Hermes.
