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

### Perguntas: uma decisão, nunca silêncio

O agente responde com um resolvedor único (`resolver.py`, ADR 0007) que **sempre** devolve uma
decisão explícita:

```text
RESOLVED      há resposta, com origem e suporte
NEEDS_HUMAN   a pergunta é legítima e falta fato — e o motivo vem junto
UNSUPPORTED   o formulário pede algo que o agente não sabe receber
```

A precedência é determinística, e geração é o último recurso:

```text
1 resposta exata já aprovada      5 reuso semântico de resposta aprovada
2 AnswerRule explícita            6 geração grounded
3 Career Profile / locked facts   7 NEEDS_HUMAN
4 CandidatePreferences
```

O `AnswerKnowledgeBase` não foi substituído: virou a primeira etapa do resolvedor.

O que entra no contexto é **material autorizado**, e cada item carrega a própria origem
(`profile.skills.wordpress`, `fact_…`, `preferences.work_authorization`). O gerador recebe esse
contexto e devolve estrutura — `answer`, `supported_by`, `confidence` — nunca texto solto. E o que
volta passa pelo `FactValidator`: número, valor monetário e entidade capitalizada precisam ter
respaldo no contexto, o que barra empregador, cargo, anos, certificação, métrica, tecnologia e
salário inventados.

Pergunta **factual** sem fato vira `NEEDS_HUMAN`; só pergunta **discursiva** pode ser escrita. E
autodeclaração e tema legal (deficiência, raça/etnia, veterano, antecedentes, visto, autorização de
trabalho) nunca são gerados: com fato explícito são respondidos, sem ele param.

Sem chave de LLM o produto continua respondendo: entra o gerador determinístico, que monta a resposta
apenas com itens do contexto autorizado. Com `JOBSEARCH_LLM_*` configurado, o `LLMAnswerProvider`
assume o mesmo protocolo. Qualidade de geração, ranking de contexto e memória entre candidaturas são
o JSA-QA-002.

### Um comando: da vaga ao desfecho

```bash
jobsearch-agent apply-to-completion <job-id> --submit
```

O loop único recebe **só o `job-id`** e conduz tudo: prepara o material, abre o
browser, inspeciona o formulário, resolve as respostas, preenche, faz upload,
cria e autoriza a intent, executa UM POST e observa o desfecho. Sem `--submit`
ele para na superfície de revisão.

```text
ApplicationLoopResult
  status · state · terminal · requires_action
  cycles · steps_completed · questions_answered · unanswered_required
  resume_sha256 · form_fingerprint · answers_fingerprint
  submission_attempted · submission_writes · phases
```

As decisões que importam ficam no resultado, não no terminal:

```text
SUBMITTED                        terminal=true                sucesso
ALREADY_SUBMITTED / NO_RESEND    terminal conforme o estado   nunca reenvia
NEEDS_ANSWER / NEEDS_CAPTCHA …   terminal=false, requires_action
SUBMIT_UNKNOWN / REJECTED …      terminal=true                sem retry automático
```

Rodar de novo depois de um desfecho **não abre browser e não envia nada**: a
porta de não-reenvio (`NO_RESEND_STATES`) para o loop antes de preparar material.
Uma Application em `AWAITING_SUBMISSION_CONFIRMATION` nunca é reenviada, mas
**não** é terminal — ela ainda chega a `SUBMITTED` por evidência independente.

O loop não conhece ATS: adapter, sessão, material e política de escrita entram
por `LoopRuntime`, montado por `pipeline.loop_runtime` em produção e por um
runtime sintético nos testes. A sequência de submissão existe em um só lugar
(`SubmissionCoordinator`), compartilhada com `apply_live`.

### Runtime de produção: o currículo nasce, é validado e é enviado

`tests/e2e/test_e2e_prod_001_production_runtime.py` prova a **integração com o produto que gera o
material**, e não só o motor de candidatura. A partir de uma vaga ingerida por `ingest` e de fixtures
de perfil sintéticas (carregadas pelos mesmos loaders de produção):

```text
prepare()  →  análise  →  estratégia  →  currículo dinâmico  →  validação factual
           →  TXT/DOCX/PDF  →  upload do PDF GERADO  →  POST  →  SUBMITTED
```

E a cadeia de bytes é medida ponta a ponta: o PDF que chega ao servidor é **byte a byte** o mesmo que
o `prepare()` gerou. O currículo também é conferido no conteúdo: contém os fatos relevantes da vaga e
não traz a experiência irrelevante do perfil (há mais fatos do que slots, então a seleção por
relevância é real).

O que o teste pode substituir é **somente o destino controlado** — qual adapter corresponde àquele
endereço, a sessão que permite loopback e a política de escrita restrita ao endpoint. Perfil, fatos,
preferências, respostas, material, `resume.pdf`, SHA, contexto, snapshot e intent vêm todos do
caminho de produção:

```python
runtime = loop_runtime(settings, adapter_resolver=..., session_factory=..., policy_factory=...,
                       allow_insecure_destination=True)
result = ApplicationLoop(database, runtime).run(job_id, submit=True)
```

Exige LibreOffice (para o PDF real); sem ele o teste é pulado, não falha.

### Multi-step: a autorização cobre a candidatura, não a última tela

Um formulário de várias etapas substitui o DOM a cada avanço. O `ApplicationForm` continua
significando **o que está na tela agora**; quem responde pela candidatura inteira é o
`ApplicationJourney`, que acumula cada etapa com fingerprint, URL e a decisão de cada campo.

```text
form_fingerprint      a superfície FINAL onde o Submit acontece
answers_fingerprint   TODAS as respostas aprovadas, de TODAS as etapas
```

Sem essa separação, o browser preencheria quatro telas e a `SubmissionIntent` validaria uma — sem
erro nenhum, com a auditoria errada. O `ReviewSnapshot` passa a ser montado do contrato acumulado, e
um campo que reaparece com **valor diferente** vira `CONTRACT_CONFLICT` em vez de "a última tela
vence".

Avanço de etapa continua local: `Next`/`Continue`/`Review` só são clicados como `type="button"`, e
qualquer escrita que tentem é bloqueada. Boards que salvam etapa por API exigem boundary própria
(JSA-LOOP-003), nunca um `POST` genérico durante o `Next`.

```bash
make test-browser-runtime      # E2E-001 (uma tela) e E2E-002 (cinco telas)
```

### E2E controlado: o POST real até `SUBMITTED`

`tests/e2e/test_e2e_001_controlled_submit.py` sobe um ATS controlado em loopback
(mesmo contrato de formulário de um provider suportado: `job_application[...]`,
`form#application_form`) e conduz o caminho real: Chromium abre a página, o
adapter inspeciona, as respostas vêm do perfil e das respostas aprovadas, o
currículo é anexado, o controle de envio é clicado e o POST multipart chega ao
servidor — que valida os campos obrigatórios e o PDF, responde com a página de
confirmação e só então a Application vira `SUBMITTED`.

As asserções são do **servidor**, não do agente: exatamente um POST, multipart,
PDF válido com o SHA do arquivo aprovado, nenhum campo obrigatório vazio, uma
única escrita autorizada e nenhuma bloqueada.

```bash
make test-browser-runtime      # exige Chromium (grupo opcional `browser`)
```

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

#### Qual é o caminho padrão quando o provedor não confirma

O POST ter saído **não** é a candidatura ter sido registrada. Quando a tentativa chega ao provedor e
ele não confirma (`NEEDS_HUMAN_CAPTCHA`), a ordem é:

```text
1. handoff para envio manual em navegador normal   <- padrão
2. retry explícito no browser automatizado         <- avançado, diagnóstico
3. encerrar mantendo a evidência
```

O handoff é o padrão porque tirar o ambiente automatizado da etapa inevitável é o único caminho que
não depende de o provedor reapreciar a mesma classificação de ambiente. Repetir
`Playwright + janela visível + humano resolvendo o desafio` não transforma aquele browser em um
navegador normal — e já se observou recusa **depois** de um humano resolver o desafio numa sessão
automatizada (ver [`docs/references/provider-certification.md`](docs/references/provider-certification.md)).

O retry continua existindo para diagnóstico, ou quando houver evidência de que a recusa anterior não
veio do ambiente. O que o agente **não** faz, em nenhum caminho: esconder `webdriver`, alterar
fingerprint, forjar plugins/navegador, ou extrair, reutilizar e reinjetar token de desafio. Um humano
pode interagir normalmente com um desafio apresentado; isso não é, e não é descrito como, solução
para fingerprinting.

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

### Gmail: acesso somente leitura, e falha não é ausência

A primeira implementação de caixa é o Gmail, com **least privilege** — o único escopo pedido é
`gmail.readonly`. O agente não marca como lido, não move, não exclui e não envia; um token cujos
escopos registrados passem disso é recusado, e uma autorização que conceda mais que readonly é
rejeitada **antes** de o token ser gravado.

```bash
pip install 'google-api-python-client>=2.100,<3.0' 'google-auth-oauthlib>=1.2,<2.0'   # grupo opcional
jobsearch-agent integrations gmail authorize   # consentimento no browser; só estabelece acesso
jobsearch-agent integrations gmail status      # resumo sem segredo
jobsearch-agent integrations gmail check       # chamada real, listando apenas ids
jobsearch-agent application reconcile-confirmation <application-id>
```

O passo a passo completo, com os seis gates de verificação, está no
[runbook do Gmail](docs/runbooks/gmail-006f.md). Um aviso que não é defeito: com o projeto OAuth em
**External + Testing**, o Google expira o refresh token em ~7 dias para escopos como `gmail.readonly`
— a resposta é reautorizar, e nenhum estado de candidatura é afetado.

Credenciais ficam em `~/.config/jobsearch-agent/gmail/` (`client_secret.json` e `token.json`),
diretório `0700` e arquivos `0600` — **verificados na leitura**, não apenas aplicados na escrita: um
token que o grupo ou o mundo podem ler é recusado. Nada disso entra em `Application.context`, journal,
`confirmation_evidence`, artifacts, log ou stdout. O override é `--gmail-dir` / `JOBSEARCH_GMAIL_DIR`.

A consulta usa `after:` só para reduzir o universo; a janela é decidida pelo `internalDate` da
mensagem, validado uma segunda vez pelo próprio sistema. E a distinção que sustenta o resto:

```text
401 · 5xx · timeout · refresh recusado  →  ConfirmationSourceUnavailable   (erro)
caixa vazia                             →  "no confirmation evidence observed"  (resultado)
```

Um erro da API nunca pode virar "não houve submissão": a Application não muda de estado e o operador
vê a falha, não uma conclusão.

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

### Operador humano sem alcançar o envio

Quando o gate espera alguém resolver o desafio, essa pessoa pode estar em outro lugar: os comandos
dela entram por uma fila thread-safe (`LiveViewRelay.offer`) e são executados **na thread que possui
a página** — a API síncrona do Playwright é presa à thread, então uma ponte de outro processo/thread
só pode *enfileirar*, nunca tocar a página direto.

Três travas independentes mantêm o envio fora de alcance, cada uma com teste:

| trava | o que impede |
| --- | --- |
| lockdown | o controle de envio fica `disabled` durante a janela do operador (medido na página) e é restaurado depois, sem tocar no que a própria página desabilitou |
| hit-test | clique que cai sobre um controle de envio é recusado (`click_on_submit_control`); clique que não acerta elemento algum também (`click_on_no_element`) |
| teclas | `Enter`, `Space` e `Tab` são recusados (`key_can_submit`): num formulário HTML eles acionam — ou levam o foco até — o botão de envio |

```bash
.venv/bin/python -m pytest tests/integration/test_operator_cannot_submit.py -q
```

O caso central é um operador que **só tenta sequestrar o envio**: mira o botão real (medido na
página), pressiona Enter/Tab/Space e produz **zero POSTs**. O outro caso é um operador que resolve o
desafio: um clique no widget e a candidatura segue, com exatamente uma escrita.

Limite declarado: restringir o clique ao *bounding box* do desafio exige geometria que o
`challenge-guard` v0.1.0 não publica (`ChallengeObservation` traz apenas `challenge_dimensions`).
Enquanto isso, a garantia é lockdown + hit-test + teclas — que não dependem de conhecer o provider.

### Challenge antes do POST: observar, esperar a pessoa, e então escrever

Com `ENABLE_CHALLENGE_RESOLUTION=true`, o loop passa a **olhar** o estado anti-bot antes de autorizar
a escrita:

```bash
ENABLE_CHALLENGE_RESOLUTION=true jobsearch-agent apply-to-completion <job-id> \
    --no-headless --submit --captcha-wait 120
```

O gate (`src/jobsearch_agent/challenge_gate.py`) pergunta ao `challenge-guard` se há desafio. Se
houver, ele **espera** — dentro de `--captcha-wait` segundos — que o guard passe a reportar
`resolved_externally`, que é o que acontece quando a pessoa resolve na janela visível. Ele não
clica, não digita, não copia token e não injeta nada: quem interage é a pessoa. Sem resolução dentro
do orçamento, o loop para em **`NEEDS_CAPTCHA`** (retomável) com **zero escritas** — nem intent, nem
tentativa registrada.

| situação | estado | escritas |
| --- | --- | --- |
| desafio antes do POST, resolvido a tempo | `SUBMITTED` | 1 |
| desafio antes do POST, ninguém resolve | `NEEDS_CAPTCHA` (retomável) | 0 |
| provedor recusa a submissão entregue | `NEEDS_HUMAN_CAPTCHA` | 1 |

`NEEDS_HUMAN_CAPTCHA` entrou em "nunca reenviar": uma submissão **foi entregue** e não confirmada,
então reabrir o browser sozinho não é permitido. Quem reabre é uma pessoa, com
`application retry-submit` — e é isso que a suíte `tests/integration/` prova, cenário por cenário,
contra o `challenge-guard` real num Chromium real:

```bash
make test-invariants      # ou: .venv/bin/python -m pytest tests/integration -m integration
```

Os invariantes rodam em todo PR no job `invariants` (`.github/workflows/runtime-gates.yml`):
`submission_writes <= 1` em todos os cenários, `resume_sha256`/`answers_fingerprint` imutáveis,
nenhuma tentativa afirmando entrega que o guard não fez, e a fronteira arquitetural preservada.

### Resolução de challenge: Fase 0 (contratos, e desligada)

O `challenge-guard` continua sendo **olhos** — detecta, classifica, observa e decide, e nunca
resolve. O pacote `challenge_resolution` é a camada de **mãos**, e ela só pode agir no que o agente
autorizar: o `ApplicationLoop` continua sendo o único que decide se o POST sai.

```text
guard      detect → classify → observe → decide
                 ↓
resolution orquestra tentativas (Engine → Strategy → Executor) e revalida
                 ↓
loop       revalida o formulário → autoriza UMA escrita → POST → confirma
```

A Fase 0 entrega **só contratos** e é invisível em runtime:

- `src/challenge_resolution/`: tipos, modelos imutáveis, proveniência, sessão, matriz de
  capability, protocols, erros e nomes canônicos de journal. Nenhuma estratégia, nenhuma emissão,
  nenhum toque em browser.
- A fronteira é verificada, não prometida: o pacote não importa `jobsearch_agent` e não alcança
  `AuthorizedWrite`, `SubmissionIntent` ou `NetworkWriteGuard` — é o que garante, por tipo, que
  resolver um challenge não consome o orçamento de submissão.
- Nenhum dataclass do pacote pode ter campo com material sensível: um campo com nome proibido
  derruba o **import** do módulo, antes de qualquer journal em produção.

```bash
ENABLE_CHALLENGE_RESOLUTION=false    # padrão; a Fase 0 não é importada pelo runtime
make test-architecture                # os quatro anéis de contenção
```

Os **quatro anéis** (mais o selfcheck) estão descritos em
[docs/architecture.md](docs/architecture.md) — dependências entre camadas (import-linter), nomes e
campos proibidos (AST), anotações (mypy strict, com `Any` explícito proibido no pacote protegido) e
imports dinâmicos (runtime). O quinto roda uma violação deliberada e exige que cada anel a detecte:
um contrato que nunca falha não testa nada. Tudo isso roda em todo PR no job `Architecture`.

Flags reservadas para as fases seguintes, todas desligadas: `ENABLE_CHALLENGE_HANDOFF`,
`ENABLE_CHALLENGE_EXTERNAL_RESOLVER`, `ENABLE_CHALLENGE_AUTO_SUBMIT_AFTER`.

O que existe desde a Fase 1: **orquestrador executável** (`ChallengeOrchestrator`, com limites de
rounds/tempo/duração, journal de cada transição e proveniência auditável), `Journal` (Protocol +
in-memory + null) e o validador de observação (`MonitorValidator`). Contratos são **síncronos** —
decisão registrada em `protocols.py`: o produto inteiro, a API do Playwright usada pelo agente e o
gate/relay de produção são síncronos, e um contrato `async` só seria executável movendo o loop (com
journal e banco) para uma thread dona da sessão.

O que **não** existe ainda: estratégia concreta de interação (a resolução de um checkbox por clique,
por exemplo), integração do orquestrador no `ApplicationLoop` (hoje o gate faz essa política inline)
e o executor real entregue ao engine.

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
