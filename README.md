# jobsearch-agent

Agente local para descobrir, analisar e preparar candidaturas com currículo específico por vaga.

Esta milestone implementa **Analyze + Generate**, a base da Submission Boundary e o **preenchimento
ao vivo**: ingestão de uma vaga, análise, fit, estratégia de currículo, validação factual, saída
TXT/DOCX/PDF, `SubmissionIntent`, autorização explícita, duplicate guard, estados pós-tentativa e
executor live com browser guardado (Greenhouse e Lever). O comando `apply` abre a URL real da vaga,
inspeciona o formulário, resolve as respostas pelo Career Profile, preenche, revalida o DOM, sobe o
currículo e **para antes do submit** — a menos que `--submit` seja passado, quando uma única
submissão autorizada é executada pela própria página. LinkedIn continua manual: não existe executor
live para ele.

## Uso rápido

Instale o pacote com Poetry (`poetry install`) e use o script `jobsearch-agent`. Durante o
desenvolvimento sem instalação, prefixe os comandos com `PYTHONPATH=src`.

As dependências principais incluem Pydantic, httpx, BeautifulSoup, Lingua, RapidFuzz e
python-docx. A integração JobSpy é opcional e pode ser instalada com o grupo de discovery
(`poetry install --with discovery`). Playwright permanece opcional: sem ele o projeto roda
inspeção offline e geração de currículo. Com ele, o executor live inspeciona o formulário,
preenche, revalida o DOM, sobe o currículo e — somente com `--submit` — executa **um** POST de
submissão pela própria página, sempre sob a Submission Boundary. O código mantém um modo mínimo para
executar fixtures em ambientes sem as dependências opcionais instaladas; a instalação de produção
deve usar o ambiente Poetry.

```bash
PYTHONPATH=src python3 -m jobsearch_agent.cli --help
PYTHONPATH=src python3 -m jobsearch_agent.cli profile validate --profile profile/career_profile.yaml --facts profile/locked_facts.yaml
PYTHONPATH=src python3 -m jobsearch_agent.cli profile readiness
PYTHONPATH=src python3 -m jobsearch_agent.cli run --json-file tests/fixtures/jobs/greenhouse.json --profile profile/career_profile.yaml --facts profile/locked_facts.yaml --db data/jobsearch.db --artifacts data/applications

# cria/retoma a candidatura persistida, sem browser e sem submit
PYTHONPATH=src python3 -m jobsearch_agent.cli application prepare <job-id> --db data/jobsearch.db
PYTHONPATH=src python3 -m jobsearch_agent.cli application resume <application-id> --db data/jobsearch.db
PYTHONPATH=src python3 -m jobsearch_agent.cli application status <application-id> --db data/jobsearch.db

# monta o pacote de handoff humano e só então move para HANDOFF_IN_PROGRESS
PYTHONPATH=src python3 -m jobsearch_agent.cli application handoff <application-id> --db data/jobsearch.db

# registra o relato do envio manual; nunca marca SUBMITTED
PYTHONPATH=src python3 -m jobsearch_agent.cli application report-manual-submit <application-id> --db data/jobsearch.db

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

# aplica ao vivo SEM --submit: abre a URL da vaga, preenche, sobe o currículo e para antes do
# submit, sem nenhuma escrita de rede
PYTHONPATH=src python3 -m jobsearch_agent.cli apply <job-id> --db data/jobsearch.db

# a mesma execução, autorizando e executando UM POST controlado ao final
PYTHONPATH=src python3 -m jobsearch_agent.cli apply <job-id> --submit

# executa o POST de uma Application que já tem formulário e fingerprints persistidos
PYTHONPATH=src python3 -m jobsearch_agent.cli application submit <application-id> --intent-id <intent-id>

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

### Aplicação ao vivo (`apply`)

`apply <job-id>` usa a URL já persistida da vaga e exige que o ATS tenha adapter e política de rede.
Hoje isso vale para **Greenhouse e Lever**; os demais providers param antes, em
`UNSUPPORTED_PROVIDER`. O fluxo é:

```text
abre a URL em sessão guardada (escrita de rede bloqueada)
→ revela o formulário por um botão "Apply" não-submit
→ inspeciona o ApplicationForm e resolve as respostas pelo Career Profile
→ Safety Gate (fit, prontidão, answers grounded, artefatos)
→ preenche os campos e sobe o resume.pdf
→ avança etapas por botões type="button" Next/Continue, quando existirem
→ FILLED_REVIEW_REQUIRED
```

| Modo | O que faz | Termina em |
| --- | --- | --- |
| `apply` | inspeciona, preenche, sobe o currículo, revalida o DOM | `FILLED_REVIEW_REQUIRED`, com **zero** escritas de rede |
| `apply --submit` | cria e autoriza a `SubmissionIntent` e deixa a página executar **um** POST | `SUBMITTED`, `SUBMIT_FAILED`, `SUBMIT_UNKNOWN`, `NEEDS_HUMAN_CAPTCHA` ou `NEEDS_CAPTCHA` |

Sem `--submit`, o comando nunca escreve na rede: a sessão do browser bloqueia POST/PUT/PATCH/DELETE,
WebSocket e submit de formulário. Se um controle de avanço tentar uma escrita, a execução para com
`ADVANCE_BLOCKED_BY_NETWORK_POLICY` em vez de contornar a política.

Com `--submit`, a sessão do browser **permanece aberta**: o agente cria o `ReviewSnapshot`, cria e
autoriza a `SubmissionIntent` e deixa a **própria aplicação do board** enviar o formulário. Os boards
modernos do Greenhouse montam o pedido no cliente — `application/json` com
`g-recaptcha-enterprise-token`, `request_token`, `csrfToken` e `fingerprint` —, tokens efêmeros que
nenhum cliente HTTP externo reproduz. Reconstruir esse POST fora do browser é o que produzia
`400 Bad Request`.

A escrita continua sob a Submission Boundary: a intent é validada, o destino, o método e os
fingerprints têm de coincidir, a tentativa é persistida **antes** do clique, e o `NetworkWriteGuard`
é armado para **exatamente um** POST na origem e no caminho autorizados (`AuthorizedWrite`). Esgotada
a permissão, o guard volta a bloquear toda escrita. Nenhum token é forjado, extraído para replay ou
contornado. Se a página apresentar um desafio antes de qualquer escrita, a execução para em
`NEEDS_CAPTCHA` e nada sai do browser. Se a submissão **sair** e o provedor recusá-la por não
conseguir verificar o navegador, o desfecho é `NEEDS_HUMAN_CAPTCHA`: handoff humano explícito, com
evidência registrada. Disfarçar sinais de automação (`navigator.webdriver`, plugins, fingerprint)
está deliberadamente fora de escopo — ver ADR 0003.

O resultado é observado no browser — resposta do POST, mudança para o `confirmationPath` e DOM de
confirmação. Confirmação inequívoca vira `SUBMITTED`; escrita efetuada sem confirmação vira
`SUBMIT_UNKNOWN`; rejeição ou nenhuma escrita vira `SUBMIT_FAILED`. `SUBMIT_UNKNOWN` nunca é
reenviado automaticamente; `SUBMIT_FAILED` é definitivo e pode ser retomado por
`application retry-submit`. O executor HTTP (`GreenhouseSubmissionExecutor`) permanece como
implementação controlada para testes e servidores locais.

### Handoff humano: o pacote vem antes do estado

Quando o provedor recusa uma submissão que chegou a sair (`NEEDS_HUMAN_CAPTCHA`), a pessoa precisa
terminar o envio. `application handoff` monta, valida e persiste o **pacote** que ela precisa e só
então move a Application:

```bash
jobsearch-agent application handoff <application-id> --db data/jobsearch.db
```

```text
monta pacote -> valida pacote -> persiste pacote -> registra handoff_started -> HANDOFF_IN_PROGRESS
```

Se qualquer passo falhar — currículo ausente ou alterado depois da aprovação, respostas diferentes do
`ReviewSnapshot`, destino com query, falta de proveniência do desafio — a Application **continua** em
`NEEDS_HUMAN_CAPTCHA`. Não existe handoff sem material, e não existe estado órfão. O pacote é escrito
no diretório privado de artifacts como `handoff/<package_id>/package.json` mais uma **cópia** do
currículo, endereçado pelo conteúdo e coberto por `package_sha256`: material alterado depois gera
outro pacote, e o anterior continua auditável com exatamente o que foi aprovado. O journal de eventos
guarda só referência e tokens curtos — nunca respostas, currículo ou PII. A saída do comando é a
visão segura do pacote (sem respostas e sem caminhos absolutos).

A proveniência do desafio (provider, motivo no conjunto fechado do `challenge-guard` e id da sessão) é
gravada na tentativa recusada. Sem ela o pacote não é reconstruível e o comando falha em vez de
inventar o motivo. A forma que chega da biblioteca é a evidência **redigida**, com `sources` e
`signal_kinds`: é o que permite auditar a atribuição do provider em vez de gravá-la como fato — ver a
[nota do teste real na CI&T](docs/evidence/2026-09-24-lever-ciandt-antibot-refusal.md). Ver ADR 0005.

### O relato do envio manual não confirma nada

Depois de terminar a candidatura no seu navegador, o relato é um comando separado:

```bash
jobsearch-agent application report-manual-submit <application-id> --db data/jobsearch.db
```

Ele registra `MANUAL_SUBMISSION_REPORTED`, move `HANDOFF_IN_PROGRESS` para
`AWAITING_SUBMISSION_CONFIRMATION` e aponta para o pacote usado — **nunca** marca `SUBMITTED`. O
relato é o que cria a incerteza; não pode ser também o que a satisfaz. O pacote, a cópia do currículo
e o fingerprint das respostas não são reescritos por ele.

`SUBMITTED` exige `SubmissionConfirmationEvidence` de um observador independente:

```text
source       confirmation_email | provider_confirmation_page
             | provider_application_status | externally_verified_record
observed_at  ISO-8601 com fuso explícito, nunca no futuro
reference    identificador opaco da evidência (nunca o conteúdo)
provider     quem observou
confidence   [0,1] — abaixo do piso 0.5 a evidência é registrada e recusada
```

`user_report`, `manual_checkbox`, `free_text` e `handoff_completion` são **declarações**, não
evidência: não pertencem ao conjunto de fontes e são recusadas pelo nome. Não existe comando de CLI
para confirmar — enquanto não houver um observador real (status do provider ou e-mail de confirmação),
um comando que aceitasse fonte e referência digitadas seria exatamente o caminho de texto livre que o
ADR 0005 recusa.

### Quem produz a evidência: observador, não palavra-chave

A confirmação observável (`confirmation.py`, ADR 0006) não conhece Gmail: ela conhece um porto de
caixa e um observador.

```text
EmailSource.messages_since(since)      Gmail, fixture/backfill, qualquer caixa
        ↓
ConfirmationObserver.observe(application, *, since)
        ↓
SubmissionConfirmationEvidence         (persistida ANTES da decisão)
        ↓
assert_confirmation_evidence()         aceita → SUBMITTED · fraca → continua aguardando
```

A frase de recebimento é **porta, não prova**: sozinha ela pontua 0,35 e fica **abaixo** do piso de
0,50 — é registrada e recusada. Corroboração independente soma: origem no domínio do ATS, empresa,
referência da vaga, e a janela temporal (peso pequeno de propósito). Linguagem de recusa é **veto**:
"obrigado pelo interesse… infelizmente" não é confirmação.

A janela começa no `MANUAL_SUBMISSION_REPORTED` menos 15 minutos de tolerância, lida do journal — não
de um parâmetro, e nunca de todo o histórico da caixa: um e-mail antigo da mesma empresa não pode
confirmar uma candidatura nova.

O que fica persistido é a evidência, jamais a mensagem:

```json
{
  "source": "confirmation_email", "reference": "18f0a1b2c3d4e5f6",
  "provider": "email", "observed_at": "2026-09-24T18:12:04+00:00",
  "confidence": 0.6,
  "signals": ["application_confirmation_phrase", "ats_domain_match", "time_window_match"]
}
```

Assunto, corpo, remetente e nome de exibição existem só durante o matching. Um `Message-ID` RFC (que
pode carregar o domínio do remetente) vira digest; o id opaco da API do provedor é usado como está.

`fill_forms` na policy aceita `auto` ou `review`. Em `review` (default) o agente ainda preenche, mas o
Safety Gate termina em `READY_FOR_REVIEW`; em `auto` ele pode chegar a `READY_TO_APPLY`. Em nenhum dos
casos existe transição automática para `SUBMIT_AUTHORIZED`: o submit sempre exige autorização
explícita via `--submit` ou `application authorize-submit`.

Dados reais de candidato ficam nos arquivos locais ignorados pelo Git
`profile/career_profile.local.yaml`, `profile/locked_facts.local.yaml` e
`profile/preferences.local.yaml`. Quando presentes, esses arquivos são selecionados como defaults;
os YAMLs sem sufixo `.local` continuam como fixtures demonstrativas. Não remova a proteção do
`.gitignore` nem publique os arquivos locais, pois podem conter contato e histórico profissional.

Com o grupo opcional de discovery instalado:

```bash
jobsearch-agent search --query "Senior WordPress Developer" --sites indeed,google --location Brazil
```

A descoberta por ATS usa as APIs JSON públicas dos próprios boards — sem credenciais e sem
scraping:

```bash
jobsearch-agent discover --provider greenhouse --board gitlab --board stripe --query frontend
jobsearch-agent discover --provider ashby --board ramp --location remote --limit 20
jobsearch-agent discover --provider lever --board spotify --db data/jobsearch.db
```

Prefira `discover` a `search`: agregadores como Indeed e Google bloqueiam clientes automatizados
com frequência (respostas vazias ou degradadas), enquanto as APIs de board são endpoints
públicos e estáveis, publicados pelos ATS justamente para alimentar seus job boards. `discover`
persiste as vagas no SQLite, então `precheck`, `prepare` e `apply` funcionam na sequência.

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
- O fit é calculado sobre as skills reconhecidas pelo registry em
  [knowledge/skills.yaml](knowledge/skills.yaml). Um anúncio cujo requisito não foi extraído não
  recebe cobertura perfeita: sem nenhuma skill reconhecida, `required_match` é `0.0` e o critério
  `required_skills` fica `unknown`, em vez de virar um match de 100%. O score mede cobertura dentro
  do vocabulário conhecido; ele não é uma medida calibrada de relevância, e extração esparsa tende a
  superestimar a cobertura. Para requisitos em texto livre, configure o provider LLM
  (`JOBSEARCH_LLM_*`), que substitui `required_skills` por extração semântica grounded.
- `Job` e `Application` possuem ciclos de vida independentes; interrupções como
  `NEEDS_ANSWER`, `NEEDS_LOGIN`, `NEEDS_MFA`, `NEEDS_CAPTCHA`, `NEEDS_HUMAN_CAPTCHA` e
  `UNSUPPORTED_FORM` são estados de domínio, não erros técnicos.
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
- O `LiveApplicationOrchestrator` reusa o mesmo ciclo bounded contra a página real: navega, revela o
  formulário, preenche, sobe o currículo e para. Ele dirige uma `GuardedBrowserSession`, então
  nenhuma submissão é alcançável a partir dessa camada.
- O payload de submissão é derivado do `ApplicationForm` resolvido (`build_submission_payload`),
  mantendo as chaves `name` do DOM (por exemplo `job_application[first_name]`). Campo obrigatório sem
  valor bloqueia a submissão em vez de enviar uma candidatura incompleta, e artefato fora do
  `artifact_root` é recusado.
- O executor Greenhouse envia o PDF como parte multipart quando o formulário tem campo de currículo.
  Confirmação só é aceita por status reconhecido (JSON ou página HTML); redirect, erro e resposta
  ambígua permanecem `SUBMIT_UNKNOWN`, e uma segunda tentativa é bloqueada.
- A submissão autorizada não desliga o guard: `AuthorizedWrite` libera um único POST, na origem e no
  caminho que a `LiveNetworkPolicy` já exigia, e se esgota em seguida. Dry-run continua sem nenhuma
  escrita possível; a diferença entre os dois caminhos é a permissão one-shot, não a ausência de
  controle.
- `apply` só existe para providers com adapter, `ProviderProfile` e `LiveNetworkPolicy`. Cada
  provider declara em `providers.py` os hosts que a página carrega, o endpoint de candidatura, os
  rótulos do controle final e os marcadores de confirmação, e — quando o board monta o formulário a
  partir de uma API — as operações GraphQL read-only da fase de descoberta. Hoje: Greenhouse e Lever
  têm `apply` completo; o Ashby já tem **inspeção** autorizada (ADR 0004) e ainda não tem adapter nem
  política de submissão.
- Um `POST` de inspeção não é escrita: a autorização depende do conteúdo, não do método. O Ashby
  passa por `InspectionNetworkPolicy`, que exige operação na allowlist, documento comprovadamente
  `query` (nunca `mutation`) e vínculo exato com o board e a vaga corrente, com orçamento próprio —
  separado do orçamento de upload e de submissão. Ver ADR 0004.
- Greenhouse e Lever usam o mesmo `id="application-form"`; a escolha do adapter prioriza o host da
  URL e só cai na assinatura de HTML quando o host não é reconhecido.
- LinkedIn não possui executor live: a política da plataforma proíbe automação de atividade por
  software de terceiros.
- A milestone Prepare Application não abre browser nem envia candidaturas.
- A CLI imprime JSON por padrão para ser consumida pelo Hermes.
