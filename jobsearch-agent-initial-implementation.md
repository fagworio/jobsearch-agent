# jobsearch-agent — Documento Inicial de Implementação

## 1. Visão do projeto

`jobsearch-agent` é um agente local para busca, análise, preparação e envio assistido/autônomo de candidaturas a vagas de emprego.

O projeto deve ser executável e orquestrável pelo Hermes, mas sua arquitetura não pode depender exclusivamente do Hermes. O núcleo deve ser modular, testável e reutilizável por CLI, API ou outros agentes no futuro.

O objetivo do MVP não é apenas "enviar currículo". O sistema deve:

1. descobrir vagas;
2. normalizar os dados;
3. analisar requisitos;
4. medir compatibilidade com o perfil do candidato;
5. detectar idioma e contexto da vaga;
6. gerar currículo específico para cada vaga;
7. gerar carta de apresentação quando necessário;
8. responder perguntas conhecidas com base em fatos validados;
9. decidir se a candidatura pode ser enviada automaticamente;
10. preencher e enviar formulários em ATS/sites compatíveis;
11. interromper e solicitar intervenção humana quando houver risco ou falta de informação;
12. registrar todo o histórico;
13. acompanhar respostas recebidas;
14. aprender com execuções anteriores sem alterar fatos do candidato.

O princípio central é:

> O sistema possui um Career Profile canônico e gera materiais específicos para cada vaga. O currículo não é um arquivo fixo.

---

# 2. Nome e identidade do projeto

Nome:

```text
jobsearch-agent
```

Nome do pacote Python sugerido:

```text
jobsearch_agent
```

CLI inicial:

```bash
jobsearch-agent
```

Exemplos futuros:

```bash
jobsearch-agent search
jobsearch-agent analyze <job-id>
jobsearch-agent prepare <job-id>
jobsearch-agent apply <job-id>
jobsearch-agent run
jobsearch-agent status
```

---

# 3. Objetivos do MVP

O MVP deve possuir capacidade real ponta a ponta:

```text
Search
  ↓
Normalize
  ↓
Analyze
  ↓
Score
  ↓
Generate Resume
  ↓
Validate
  ↓
Generate Application Materials
  ↓
Safety Gate
  ↓
Apply
  ↓
Track
```

O MVP é considerado funcional somente quando for capaz de:

- receber uma vaga real;
- identificar o idioma;
- analisar requisitos;
- comparar com o Career Profile;
- criar uma estratégia de currículo;
- gerar um currículo em português ou inglês;
- validar que nenhum fato foi inventado;
- renderizar o currículo;
- preparar respostas para o formulário;
- preencher um ATS suportado;
- parar corretamente quando encontrar informação desconhecida;
- registrar a candidatura;
- permitir retomada posterior.

---

# 4. Princípios arquiteturais

## 4.1 Núcleo independente de site

Nenhuma regra de negócio principal deve ficar acoplada a:

- LinkedIn;
- Greenhouse;
- Lever;
- Ashby;
- Workday;
- SmartRecruiters;
- páginas de carreira específicas;
- Hermes;
- Playwright.

O núcleo deve operar sobre modelos internos normalizados.

Exemplo:

```text
Raw Greenhouse Job
        ↓
Greenhouse Adapter
        ↓
Normalized Job
        ↓
Core
```

ou:

```text
Generic Career Page
        ↓
Generic Adapter
        ↓
Normalized Job
        ↓
Core
```

---

## 4.2 API-first, browser-second

Sempre preferir:

1. API pública/documentada;
2. endpoint estruturado;
3. JSON/JSON-LD;
4. HTML estruturado;
5. adapter DOM específico;
6. Playwright genérico;
7. reasoning visual/LLM;
8. intervenção humana.

Não usar navegador para tarefas que podem ser resolvidas por API ou parsing determinístico.

---

## 4.3 LLM onde existe ambiguidade

Não usar LLM para:

- ler um campo conhecido;
- clicar em botão conhecido;
- validar tipos;
- deduplicar por chave determinística;
- salvar estado;
- decidir se um arquivo existe;
- executar regras simples.

Usar LLM para:

- interpretar requisitos;
- relacionar experiência com vaga;
- definir posicionamento do currículo;
- adaptar redação;
- gerar respostas abertas fundamentadas;
- interpretar campos desconhecidos;
- classificar mensagens de recrutadores;
- resolver variações semânticas.

---

## 4.4 Nenhum fato inventado

Toda informação factual do candidato deve ter origem rastreável.

Separar:

```text
FACT
INFERENCE
GENERATED_TEXT
USER_APPROVED_ANSWER
```

O agente pode reescrever fatos.

O agente não pode criar fatos.

Exemplo proibido:

```text
Career Profile:
"Experiência com otimização de WordPress"

Generated Resume:
"Reduced page load time by 63%"
```

A métrica não existe, portanto o texto deve falhar no validation gate.

---

## 4.5 Falhar com segurança

Quando houver:

- CAPTCHA;
- MFA;
- login inesperado;
- autorização de trabalho desconhecida;
- pergunta legal desconhecida;
- salário fora das regras;
- informação pessoal ausente;
- declaração factual não validada;
- aceite contratual inesperado;
- formulário não reconhecido;
- mudança relevante no site;

o agente deve entrar em estado:

```text
NEEDS_HUMAN
```

Nunca tentar contornar CAPTCHA, MFA ou mecanismos de segurança.

---

# 5. Arquitetura de alto nível

```text
                         HERMES
                            │
                            │
                    Jobsearch Agent
                            │
          ┌─────────────────┼─────────────────┐
          │                 │                 │
       Discovery        Intelligence       Tracking
          │                 │                 │
          ↓                 ↓                 ↓
     Source Adapters    Job Analyzer        Email
          │             Fit Engine          Events
          │             Language            Status
          │                 │
          └───────┬─────────┘
                  ↓
               Job Queue
                  ↓
          Resume Strategy Engine
                  ↓
          Dynamic Resume Engine
                  ↓
        Application Materials
                  ↓
          Validation / Gates
                  ↓
              ATS Router
                  ↓
       ┌──────────┼───────────┐
       │          │           │
  Greenhouse    Lever       Ashby
       │          │           │
       └──────────┴──────┬────┘
                         ↓
                 Browser Executor
                         ↓
                   Submission
                         ↓
                 Application DB
```

---

# 6. Estrutura inicial do repositório

```text
jobsearch-agent/
├── README.md
├── pyproject.toml
├── .env.example
├── .gitignore
├── Makefile
├── docs/
│   ├── architecture.md
│   ├── domain-model.md
│   ├── adapters.md
│   ├── resume-engine.md
│   ├── safety-gates.md
│   └── decisions/
│
├── src/
│   └── jobsearch_agent/
│       ├── __init__.py
│       │
│       ├── cli/
│       │   ├── main.py
│       │   └── commands/
│       │
│       ├── config/
│       │   ├── settings.py
│       │   └── loader.py
│       │
│       ├── domain/
│       │   ├── job.py
│       │   ├── profile.py
│       │   ├── resume.py
│       │   ├── application.py
│       │   ├── answer.py
│       │   └── enums.py
│       │
│       ├── profile/
│       │   ├── loader.py
│       │   ├── validator.py
│       │   ├── facts.py
│       │   └── repository.py
│       │
│       ├── discovery/
│       │   ├── service.py
│       │   ├── normalizer.py
│       │   ├── deduplicator.py
│       │   └── sources/
│       │       ├── base.py
│       │       ├── greenhouse.py
│       │       ├── lever.py
│       │       ├── ashby.py
│       │       ├── jobspy.py
│       │       └── generic.py
│       │
│       ├── analysis/
│       │   ├── language.py
│       │   ├── requirements.py
│       │   ├── fit.py
│       │   └── strategy.py
│       │
│       ├── resume/
│       │   ├── strategist.py
│       │   ├── selector.py
│       │   ├── generator.py
│       │   ├── localizer.py
│       │   ├── keyword_alignment.py
│       │   ├── fact_validator.py
│       │   ├── ats_validator.py
│       │   └── renderer/
│       │       ├── base.py
│       │       ├── docx.py
│       │       └── pdf.py
│       │
│       ├── materials/
│       │   ├── cover_letter.py
│       │   └── answers.py
│       │
│       ├── applications/
│       │   ├── service.py
│       │   ├── router.py
│       │   ├── gates.py
│       │   ├── state_machine.py
│       │   └── ats/
│       │       ├── base.py
│       │       ├── greenhouse.py
│       │       ├── lever.py
│       │       ├── ashby.py
│       │       └── generic.py
│       │
│       ├── browser/
│       │   ├── session.py
│       │   ├── executor.py
│       │   ├── forms.py
│       │   └── screenshots.py
│       │
│       ├── tracking/
│       │   ├── service.py
│       │   ├── email.py
│       │   └── classifier.py
│       │
│       ├── learning/
│       │   ├── answers.py
│       │   ├── fields.py
│       │   └── ats.py
│       │
│       ├── orchestration/
│       │   ├── pipeline.py
│       │   └── hermes.py
│       │
│       └── persistence/
│           ├── db.py
│           ├── models.py
│           └── repositories/
│
├── profile/
│   ├── career_profile.yaml
│   ├── locked_facts.yaml
│   ├── preferences.yaml
│   └── answers.yaml
│
├── templates/
│   ├── resume/
│   │   └── ats/
│   └── cover_letter/
│
├── data/
│   ├── jobs/
│   ├── applications/
│   ├── browser_profiles/
│   └── artifacts/
│
└── tests/
    ├── unit/
    ├── integration/
    ├── fixtures/
    └── e2e/
```

---

# 7. Career Profile

O Career Profile é a fonte canônica do candidato.

Exemplo:

```yaml
identity:
  name: Demo Candidate

professional_summary:
  canonical: >
    WordPress developer with extensive experience in custom themes,
    plugins, WooCommerce and integrations.

experience:
  - id: experience_001
    company: example
    role: wordpress_developer
    start_date: 2020-01
    end_date: null
    facts:
      - fact_001
      - fact_002

skills:
  wordpress:
    years: 12+
    level: advanced

  shopify:
    years: 3
    level: advanced

languages:
  portuguese:
    level: native

  english:
    level: advanced

preferences:
  remote: true
```

---

# 8. Locked Facts

Criar uma camada separada para afirmações factualizadas.

Exemplo:

```yaml
facts:
  fact_001:
    type: experience
    statement:
      pt-BR: "Desenvolvimento de plugins personalizados para WordPress."
      en-US: "Development of custom WordPress plugins."
    tags:
      - wordpress
      - php
      - plugin-development

  fact_002:
    type: technology
    statement:
      pt-BR: "Experiência com WooCommerce."
      en-US: "Experience with WooCommerce."
    tags:
      - woocommerce
```

Requisitos:

- IDs estáveis;
- versionamento;
- origem opcional;
- idioma opcional;
- tags;
- métricas somente quando conhecidas;
- nunca gerar métricas automaticamente.

---

# 9. Modelo de vaga normalizado

Toda fonte deve produzir um `Job`.

Modelo mínimo:

```python
Job:
    id
    source
    external_id
    company
    title
    description
    location
    country
    remote_type
    employment_type
    salary
    currency
    language
    ats
    url
    posted_at
    discovered_at
    requirements
    preferred_requirements
    raw_payload
```

O `raw_payload` deve ser preservado para auditoria.

---

# 10. Deduplicação

Implementar desde o MVP.

Prioridade de chaves:

```text
1. source + external_id
2. ats + company + external_id
3. canonical_url
4. company + normalized_title + normalized_location
5. description_hash
```

O sistema deve reconhecer a mesma vaga encontrada por múltiplas fontes.

---

# 11. Language Analyzer

Responsável por identificar:

```text
language
locale
country
market
```

MVP obrigatório:

```text
pt-BR
en-US
```

Fallback:

```text
en
pt
unknown
```

Regras:

- não depender somente do domínio;
- analisar título + descrição;
- permitir override;
- registrar confiança;
- idioma da candidatura deve seguir o idioma principal da vaga, salvo regra configurada.

---

# 12. Job Requirements Analyzer

Extrair e estruturar:

```text
required_skills
preferred_skills
years_of_experience
education
language_requirements
location_requirements
work_authorization
employment_type
technologies
responsibilities
seniority
salary
domain
```

O resultado precisa ser estruturado e persistido.

---

# 13. Fit Engine

O Fit Engine responde:

> Esta vaga é relevante para o Career Profile?

Não responde:

> Podemos enviar automaticamente?

Essas decisões devem ser separadas.

Exemplo:

```json
{
  "score": 91,
  "required_match": 0.95,
  "preferred_match": 0.72,
  "experience_match": 1.0,
  "language_match": 1.0,
  "location_match": 1.0,
  "matched_skills": [
    "WordPress",
    "PHP",
    "WooCommerce"
  ],
  "missing_required": [],
  "missing_preferred": [
    "Pantheon"
  ],
  "explanation": []
}
```

O score deve ser explicável.

Não implementar apenas contagem de keywords.

---

# 14. Dynamic Resume Engine — obrigatório no MVP

O currículo deve ser gerado para cada vaga.

Fluxo:

```text
Career Profile
      +
Locked Facts
      +
Job
      +
Requirements
      ↓
Resume Strategy
      ↓
Fact Selection
      ↓
Experience Selection
      ↓
Localization
      ↓
Keyword Alignment
      ↓
Generation
      ↓
Fact Validation
      ↓
ATS Validation
      ↓
Render
```

---

# 15. Resume Strategy Engine

Antes de gerar o currículo, produzir uma estratégia estruturada.

Exemplo:

```yaml
target_role: Senior WordPress Engineer
language: en-US

positioning:
  primary: wordpress_platform_engineer

focus:
  - custom plugin development
  - WooCommerce
  - REST APIs
  - performance
  - integrations

secondary:
  - React
  - Gutenberg
  - Docker

deprioritize:
  - Shopify
  - generic frontend work

keywords:
  - WordPress
  - PHP
  - WooCommerce
  - REST API
  - Git
  - Docker

max_pages: 2
template: ats
```

A estratégia deve ser salva junto da candidatura.

---

# 16. Experience Selector

Cada experiência/fato recebe relevância para a vaga.

Exemplo:

```json
{
  "fact_id": "fact_001",
  "relevance": 0.96,
  "reason": "Direct match with custom WordPress plugin requirement."
}
```

Usar a relevância para:

- decidir o que incluir;
- decidir o que resumir;
- ordenar bullets;
- reduzir conteúdo pouco relacionado.

Nunca remover fatos obrigatórios de identificação do currículo.

---

# 17. Localização do currículo

O sistema não deve apenas traduzir texto.

Deve adaptar linguagem profissional.

Exemplo canônico:

```text
custom WordPress plugin development
```

PT-BR:

```text
Desenvolvimento de plugins personalizados para WordPress.
```

EN-US:

```text
Built and maintained custom WordPress plugins.
```

A frase pode mudar.

O fato não pode mudar.

---

# 18. Keyword Alignment

Depois da primeira geração:

```text
Job Requirements
       vs
Generated Resume
```

Calcular:

- required keyword coverage;
- preferred keyword coverage;
- missing factual skills;
- excessive repetition.

Se uma skill exigida existe no perfil mas ficou ausente, o sistema pode ajustar o currículo.

Se a skill não existe no perfil, nunca adicioná-la apenas para aumentar cobertura.

---

# 19. Fact Validator

Todo claim factual relevante produzido pelo Resume Engine deve ser rastreável.

Estrutura desejada:

```json
{
  "claim": "Built custom WooCommerce integrations.",
  "supported_by": [
    "fact_021",
    "fact_035"
  ],
  "valid": true
}
```

Claim sem suporte:

```json
{
  "claim": "Improved conversion by 35%.",
  "supported_by": [],
  "valid": false
}
```

Resultado:

```text
RESUME_VALIDATION_FAILED
```

O documento não pode seguir para candidatura.

---

# 20. ATS Resume Validator

Validar:

- texto extraível;
- estrutura de uma coluna por padrão;
- headings previsíveis;
- ausência de tabelas complexas;
- ausência de texto dentro de imagens;
- ordem semântica correta;
- datas consistentes;
- contatos presentes;
- tamanho máximo;
- máximo de páginas configurável;
- caracteres compatíveis.

Templates iniciais:

```text
ats
minimal
```

No MVP, `ats` deve ser o padrão.

---

# 21. Formatos de saída

MVP:

```text
DOCX
PDF
Plain Text
```

Guardar também uma representação intermediária estruturada.

Exemplo:

```json
{
  "header": {},
  "summary": "",
  "skills": [],
  "experience": [],
  "education": []
}
```

Essa representação deve ser a fonte dos renderers.

Não gerar PDF diretamente a partir de texto solto.

---

# 22. Cover Letter Engine

Gerar somente quando:

- exigida;
- opcional e habilitada por configuração;
- considerada relevante pela política.

Idioma segue a vaga.

Também deve obedecer ao Fact Validator.

---

# 23. Application Answers

Banco reutilizável:

```yaml
answers:
  work_authorization_br:
    question_patterns:
      - "Você possui autorização para trabalhar no Brasil?"
    answer:
      pt-BR: "Sim."
      en-US: "Yes."
    approved: true
```

Hierarquia:

```text
Exact Answer
   ↓
Semantic Match
   ↓
Career Profile
   ↓
Grounded Generation
   ↓
Unknown?
   ↓
NEEDS_HUMAN
```

Respostas aprovadas pelo usuário podem entrar no knowledge base.

---

# 24. Safety Gate

Separar `fit_score` de `application_readiness`.

Uma vaga pode ter:

```text
fit_score = 97
```

e mesmo assim:

```text
application_readiness = false
```

Exemplo:

```json
{
  "fit_score": 97,
  "resume_valid": true,
  "unknown_questions": 2,
  "salary_conflict": false,
  "work_authorization_known": true,
  "captcha": false,
  "ready": false,
  "decision": "NEEDS_ANSWER"
}
```

Estados possíveis:

```text
AUTO_APPLY
READY_FOR_REVIEW
NEEDS_ANSWER
NEEDS_LOGIN
NEEDS_MFA
NEEDS_CAPTCHA
UNSUPPORTED_FORM
REJECT
```

---

# 25. Application State Machine

Estados iniciais:

```text
DISCOVERED
    ↓
NORMALIZED
    ↓
ANALYZED
    ↓
SCORED
    ├── REJECTED
    ↓
QUALIFIED
    ↓
RESUME_STRATEGY_READY
    ↓
RESUME_READY
    ↓
MATERIALS_READY
    ↓
APPLICATION_READY
    ├── NEEDS_HUMAN
    ↓
APPLYING
    ↓
SUBMITTED
    ↓
AWAITING_RESPONSE
    ├── INTERVIEW
    ├── REJECTED
    ├── FOLLOW_UP
    ├── OFFER
    └── NO_RESPONSE
```

Todas as transições devem ser persistidas.

O pipeline deve ser retomável.

---

# 26. ATS Adapter Contract

Criar interface comum.

Exemplo conceitual:

```python
class ATSAdapter(Protocol):
    def can_handle(self, job: Job) -> bool: ...
    def inspect(self, job: Job) -> ApplicationForm: ...
    def prepare(self, context: ApplicationContext) -> PreparedApplication: ...
    def validate(self, application: PreparedApplication) -> ValidationResult: ...
    def submit(self, application: PreparedApplication) -> SubmissionResult: ...
```

Cada ATS deve implementar seu próprio adapter.

---

# 27. ATS suportados no MVP

Prioridade:

```text
1. Greenhouse
2. Lever
3. Ashby
4. Generic HTML/Playwright
```

Depois do MVP:

```text
5. Workday
6. SmartRecruiters
7. iCIMS
8. outros
```

Workday não deve bloquear a entrega do MVP.

---

# 28. Generic Site Adapter

O sistema deve ser extensível para sites desconhecidos.

Pipeline:

```text
URL
 ↓
Detect ATS
 ↓
Known ATS?
 ├── yes → specific adapter
 └── no
      ↓
 Structured HTML inspection
      ↓
 Form schema extraction
      ↓
 Generic field mapper
      ↓
 confidence sufficient?
      ├── yes → prepare
      └── no → NEEDS_HUMAN
```

Nunca assumir que todo site tem os mesmos campos.

---

# 29. Form Schema

Representação interna:

```python
ApplicationForm:
    fields: list[ApplicationField]

ApplicationField:
    id
    name
    label
    type
    required
    options
    semantic_type
    confidence
```

`semantic_type` pode ser:

```text
first_name
last_name
full_name
email
phone
location
linkedin
github
portfolio
resume
cover_letter
salary_expectation
work_authorization
visa_sponsorship
open_question
unknown
```

---

# 30. Browser Layer

Usar Playwright para execução.

Responsabilidades:

- abrir páginas;
- reutilizar sessão;
- preencher campos;
- anexar arquivos;
- avançar etapas;
- registrar screenshots;
- detectar mudanças;
- capturar confirmação.

Não colocar lógica de carreira ou currículo dentro da camada browser.

---

# 31. Sessões persistentes

Estrutura:

```text
data/browser_profiles/
├── greenhouse/
├── lever/
├── ashby/
└── generic/
```

Sessões devem ser:

- locais;
- ignoradas pelo Git;
- isoladas por domínio/ATS;
- recriáveis.

Credenciais nunca podem ser salvas no repositório.

---

# 32. Hermes Integration

O Hermes deve atuar como orquestrador.

Exemplos:

```text
Procure vagas de Senior WordPress Developer publicadas
nas últimas 48 horas.

Aceite vagas em português ou inglês.

Gere um currículo específico para cada vaga.

Candidate automaticamente somente quando todos os gates
estiverem aprovados.
```

Outro:

```text
Analise as vagas encontradas hoje e prepare as que tiverem
fit acima de 80, mas não envie nenhuma.
```

O Hermes não deve precisar conhecer detalhes dos adapters.

Expor ferramentas de alto nível:

```text
search_jobs
analyze_job
prepare_application
apply_job
list_applications
resume_application
track_responses
```

---

# 33. Persistência

MVP:

```text
SQLite
```

Arquitetura deve permitir migração futura para:

```text
PostgreSQL
```

Entidades mínimas:

```text
jobs
job_sources
job_analysis
job_fit
resume_strategies
resumes
applications
application_events
answers
facts
ats_sessions
tracking_events
```

---

# 34. Estrutura de artefatos por candidatura

Exemplo:

```text
data/applications/
└── <application-id>/
    ├── job.json
    ├── raw-job.json
    ├── analysis.json
    ├── fit.json
    ├── resume-strategy.json
    ├── resume.json
    ├── resume.docx
    ├── resume.pdf
    ├── cover-letter.md
    ├── answers.json
    ├── form-schema.json
    ├── submission.json
    └── screenshots/
```

Isso deve permitir auditoria completa.

---

# 35. Tracking de respostas

MVP deve prever interface para tracking.

Primeira implementação:

```text
Gmail
```

Classificações:

```text
APPLICATION_CONFIRMATION
RECRUITER_MESSAGE
INTERVIEW
TECHNICAL_CHALLENGE
REJECTION
OFFER
FOLLOW_UP
UNKNOWN
```

Uma mensagem classificada como `UNKNOWN` não deve alterar estado automaticamente sem segurança suficiente.

---

# 36. Learning Engine

O sistema pode aprender execução, nunca fatos pessoais sem aprovação.

Permitido aprender:

```text
question → approved answer
ATS → field selector
ATS → field semantic mapping
company → ATS behavior
site → successful navigation strategy
```

Não permitido aprender automaticamente:

```text
invented experience
invented skill
invented salary preference
invented work authorization
invented personal data
```

---

# 37. Configuração de autonomia

Criar níveis configuráveis.

Exemplo:

```yaml
autonomy:
  search: auto
  analyze: auto
  generate_resume: auto
  generate_cover_letter: auto
  fill_forms: auto
  submit:
    mode: gated
```

Modos sugeridos:

```text
manual
review
gated
auto
```

No MVP, o default de `submit` deve ser:

```text
review
```

Durante desenvolvimento e testes:

```text
manual
```

---

# 38. Dry Run

Obrigatório desde o primeiro fluxo E2E.

Exemplo:

```bash
jobsearch-agent apply <job-id> --dry-run
```

Dry run deve:

- abrir a vaga;
- analisar formulário;
- gerar currículo;
- gerar respostas;
- preencher quando seguro;
- NÃO clicar no submit final;
- gerar relatório;
- salvar screenshots.

---

# 39. Observabilidade

Cada execução deve gerar logs estruturados.

Exemplo:

```json
{
  "event": "resume_generated",
  "job_id": "123",
  "resume_id": "456",
  "language": "en-US"
}
```

Não registrar:

- senhas;
- tokens;
- cookies;
- documentos pessoais sensíveis em texto desnecessário.

---

# 40. Testes

## Unitários

Cobrir:

- normalização;
- deduplicação;
- language detection;
- fit score;
- resume strategy;
- fact validator;
- keyword alignment;
- safety gates;
- state transitions.

## Integração

Cobrir:

- Greenhouse;
- Lever;
- Ashby;
- persistência;
- renderização DOCX/PDF;
- adapters.

## Fixtures

Manter HTML/JSON sanitizado de formulários reais.

```text
tests/fixtures/
├── greenhouse/
├── lever/
├── ashby/
└── generic/
```

## E2E

Executar preferencialmente em:

```text
dry-run
```

Nunca depender de submissão real para CI.

---

# 41. Definition of Done geral

Uma funcionalidade só está concluída quando:

- possui contrato claro;
- possui testes;
- possui tratamento de erro;
- não depende de estado global oculto;
- é persistível quando necessário;
- respeita o state machine;
- respeita safety gates;
- não introduz fatos novos;
- possui logs;
- permite retomada quando aplicável.

---

# 42. Roadmap do MVP

## MVP-001 — Bootstrap

- criar projeto Python;
- pyproject;
- lint;
- formatter;
- type checking;
- test runner;
- config;
- logging;
- CLI inicial.

Aceite:

```bash
jobsearch-agent --help
```

funciona.

---

## MVP-002 — Domain Model

Criar modelos:

```text
Job
CareerProfile
Fact
JobAnalysis
FitResult
ResumeStrategy
Resume
Application
ApplicationEvent
ApplicationForm
ApplicationField
```

---

## MVP-003 — Career Profile

- loader YAML;
- schema;
- validation;
- versioning;
- fixture realista;
- locked facts.

---

## MVP-004 — Persistence

- SQLite;
- migrations;
- repositories;
- job/application history.

---

## MVP-005 — Job Discovery Base

- SourceAdapter;
- DiscoveryService;
- raw payload;
- normalizer.

---

## MVP-006 — Greenhouse Discovery

- descoberta;
- parsing;
- normalização;
- testes com fixture.

---

## MVP-007 — Lever Discovery

Mesmo contrato do Greenhouse.

---

## MVP-008 — Ashby Discovery

Mesmo contrato.

---

## MVP-009 — Deduplication

Implementar regras de identidade de vaga.

---

## MVP-010 — Language Analyzer

Suporte:

```text
pt-BR
en-US
```

---

## MVP-011 — Requirements Analyzer

Extrair estrutura completa da vaga.

---

## MVP-012 — Fit Engine

- score;
- breakdown;
- explanation;
- hard blockers.

---

## MVP-013 — Resume Strategy Engine

Gerar estratégia estruturada por vaga.

---

## MVP-014 — Experience/Fact Selector

Selecionar fatos relevantes com score.

---

## MVP-015 — Dynamic Resume Generator

Gerar representação estruturada do currículo.

Obrigatório:

```text
PT-BR
EN-US
```

---

## MVP-016 — Resume Localization

Localização profissional, não tradução literal.

---

## MVP-017 — Keyword Alignment

Cobertura de termos sem inventar skills.

---

## MVP-018 — Fact Validator

Bloquear claims sem suporte.

Este item é gate obrigatório.

---

## MVP-019 — ATS Resume Validator

Validar compatibilidade estrutural.

---

## MVP-020 — Resume Renderer

Gerar:

```text
DOCX
PDF
TXT
```

---

## MVP-021 — Cover Letter

- PT-BR;
- EN-US;
- fact validation.

---

## MVP-022 — Q&A Knowledge Base

- respostas aprovadas;
- matching;
- fallback;
- NEEDS_HUMAN.

---

## MVP-023 — Application State Machine

Persistir todas as transições.

---

## MVP-024 — Safety Gate

Separar fit de readiness.

---

## MVP-025 — ATS Base Contract

Criar interface comum para adapters de candidatura.

---

## MVP-026 — Greenhouse Application Adapter

Primeiro fluxo real de candidatura.

Começar por:

```text
inspect
prepare
validate
dry-run
```

Submit real somente após dry-run confiável.

---

## MVP-027 — Lever Application Adapter

Mesmo fluxo.

---

## MVP-028 — Ashby Application Adapter

Mesmo fluxo.

---

## MVP-029 — Browser Session Manager

- Playwright;
- sessões persistentes;
- screenshots;
- isolation.

---

## MVP-030 — Generic Form Inspector

Detectar e representar formulários desconhecidos.

---

## MVP-031 — Generic Field Mapper

Mapear semanticamente campos conhecidos.

Confidence baixo:

```text
NEEDS_HUMAN
```

---

## MVP-032 — Dry Run E2E

Executar:

```text
job → resume → form → fill → stop before submit
```

---

## MVP-033 — Submission

Permitir submit controlado somente com gates aprovados.

---

## MVP-034 — Application History

Registrar:

```text
vaga
currículo
estratégia
respostas
formulário
timestamps
resultado
```

---

## MVP-035 — Gmail Tracking

Detectar respostas relacionadas a candidaturas.

---

## MVP-036 — Hermes Tool Interface

Expor:

```text
search_jobs
analyze_job
prepare_application
apply_job
resume_application
track_responses
```

---

## MVP-037 — Learning Base

Persistir:

- Q&A aprovado;
- field mappings;
- ATS behavior;
- navigation hints.

---

## MVP-038 — CLI Workflow

Exemplo:

```bash
jobsearch-agent run \
  --query "Senior WordPress Developer" \
  --languages en-US,pt-BR \
  --min-fit 80 \
  --mode review
```

---

## MVP-039 — Audit Report

Gerar por execução:

```text
jobs found
jobs deduplicated
jobs qualified
resumes generated
applications prepared
applications submitted
human interventions
failures
```

---

## MVP-040 — MVP Hardening

Antes de considerar MVP completo:

- revisar segurança;
- revisar dados sensíveis;
- revisar retries;
- revisar idempotência;
- revisar resume claims;
- revisar estados;
- revisar duplicidade;
- executar E2E repetido.

---

# 43. Pós-MVP

## Fase A — Mais ATS

Adicionar:

```text
Workday
SmartRecruiters
iCIMS
BambooHR
Workable
Teamtailor
Recruitee
```

Sempre via adapters.

---

## Fase B — Mais fontes

Adicionar fontes sem alterar o Core.

---

## Fase C — Mais idiomas

Arquitetura deve permitir:

```text
es-ES
es-LATAM
fr-FR
de-DE
```

Sem alterar Resume Engine central.

Adicionar pacote/localização.

---

## Fase D — Dashboard

Possível frontend:

```text
Next.js
```

O dashboard consumirá o mesmo Core/API.

Funções:

- vagas;
- score;
- currículo;
- candidaturas;
- status;
- revisão humana;
- métricas.

---

## Fase E — Analytics

Medir:

```text
applications
responses
interviews
rejections
offers
response rate
interview rate
```

Também correlacionar:

```text
resume strategy
job type
ATS
language
company type
```

Não declarar causalidade sem evidência.

---

## Fase F — Adaptive Resume Intelligence

Aprender quais estratégias tendem a gerar mais respostas.

Nunca permitir que otimização ultrapasse os limites dos fatos reais.

---

# 44. Extensibilidade obrigatória

Qualquer novo ATS deve poder ser adicionado sem editar o Fit Engine ou Resume Engine.

Qualquer nova fonte deve poder ser adicionada sem editar ApplicationService.

Qualquer novo idioma deve poder ser adicionado sem reimplementar toda geração.

Qualquer novo renderer deve implementar contrato comum.

Qualquer nova estratégia de tracking deve implementar interface própria.

---

# 45. Regras para Codex durante implementação

1. Antes de alterar arquitetura, inspecionar o repositório atual.
2. Não criar abstrações sem uso concreto.
3. Implementar primeiro contratos simples e testáveis.
4. Manter o Core independente de Playwright e Hermes.
5. Não inserir regras específicas de site em serviços genéricos.
6. Criar adapter quando comportamento variar por ATS.
7. Nunca gerar fatos ausentes no Career Profile.
8. Toda decisão automática importante deve ser auditável.
9. Toda operação destrutiva ou irreversível precisa de gate.
10. CAPTCHA/MFA nunca devem ser contornados.
11. Preferir parsing/API determinístico a browser.
12. Browser deve ser a última camada operacional.
13. Nenhum submit real deve entrar em teste automatizado.
14. Implementar dry-run antes de submit real.
15. Estados devem ser persistentes e retomáveis.
16. Toda nova feature deve incluir testes.
17. Não adicionar frameworks pesados sem necessidade comprovada.
18. SQLite no MVP; evitar dependência prematura de infraestrutura.
19. Preservar possibilidade de execução local.
20. Hermes é integração/orquestração, não o domínio.

---

# 46. Primeira sequência recomendada de implementação

Primeiro ciclo:

```text
MVP-001
MVP-002
MVP-003
MVP-004
```

Depois:

```text
MVP-005
MVP-006
MVP-009
MVP-010
MVP-011
MVP-012
```

Depois implementar o núcleo diferencial do projeto:

```text
MVP-013
MVP-014
MVP-015
MVP-016
MVP-017
MVP-018
MVP-019
MVP-020
```

Somente depois entrar em candidatura automática:

```text
MVP-022+
```

O Dynamic Resume Engine deve estar funcional antes do primeiro submit real.

---

# 47. Primeira milestone

## Milestone: Analyze + Generate

Entrada:

```text
URL de vaga
+
Career Profile
```

Saída:

```text
Normalized Job
Job Analysis
Fit Score
Resume Strategy
Resume PT-BR ou EN-US
DOCX
PDF
Validation Report
```

Essa milestone deve funcionar sem Playwright e sem envio de candidatura.

Ela valida a parte mais importante do produto antes de introduzir automação de browser.

---

# 48. Segunda milestone

## Milestone: Prepare Application

Entrada:

```text
Job aprovado
```

Saída:

```text
resume
cover letter
answers
form schema
application readiness
```

Sem submit.

---

# 49. Terceira milestone

## Milestone: Dry Run

Executar automaticamente em Greenhouse/Lever/Ashby:

```text
open
inspect
fill
attach
validate
stop before final submit
```

---

# 50. Quarta milestone

## Milestone: Controlled Submit

Permitir submit real quando:

```text
fit gate = approved
resume gate = approved
facts gate = approved
application gate = approved
user autonomy policy = allows submit
```

---

# 51. Resultado esperado do MVP

Ao final, o seguinte fluxo deve funcionar:

```text
Hermes:
"Procure vagas de Senior WordPress Developer das últimas 48 horas,
em português ou inglês, com fit mínimo de 80. Prepare e envie
somente as que passarem por todos os gates."

jobsearch-agent:
1. busca vagas
2. deduplica
3. analisa
4. pontua
5. detecta idioma
6. gera estratégia
7. gera currículo específico
8. valida fatos
9. gera carta/respostas
10. inspeciona ATS
11. verifica readiness
12. envia quando permitido
13. registra tudo
14. acompanha resposta
```

O sistema deve conseguir adicionar novos sites e ATS no futuro sem reconstruir o núcleo.

---

# 52. Regra final de projeto

Quando houver conflito entre:

```text
automação
versus
correção / segurança / rastreabilidade
```

priorizar:

```text
correção
segurança
rastreabilidade
```

A meta não é maximizar o número bruto de candidaturas.

A meta é executar candidaturas relevantes, corretas, rastreáveis e adaptadas à vaga.
