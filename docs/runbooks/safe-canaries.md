# Runbook: canaries seguros

Este runbook não autoriza submissões reais. Ele define como validar as duas
fronteiras sem compartilhar credenciais com o agente e sem testar candidaturas
reais no CI.

## Canary 001 — LinkedIn manual

Objetivo:

```text
sessão autenticada manualmente
→ inspeção sob orientação do usuário
→ preenchimento manual pelo usuário
→ Review reached
→ submit manual pelo usuário
```

Limites do agente:

- não recebe senha, token, cookie ou código MFA;
- não executa login, CAPTCHA, navegação, preenchimento, upload ou submit live;
- não usa `LinkedInSession` para controlar um browser;
- não executa `linkedin inspect` contra uma URL: o comando aceita somente HTML local;
- não altera `ApplicationState` para `SUBMITTED` com base em uma página LinkedIn.

Checklist humano:

- [ ] abrir uma janela visível do navegador;
- [ ] informar credenciais diretamente no LinkedIn;
- [ ] resolver MFA/CAPTCHA diretamente no LinkedIn;
- [ ] confirmar que o estado manual é `AUTHENTICATED_MANUAL`;
- [ ] selecionar uma única vaga de teste;
- [ ] confirmar que a vaga é Easy Apply;
- [ ] usar o currículo gerado pelo projeto;
- [ ] responder somente perguntas sustentadas pelo Career Profile/Locked Facts;
- [ ] parar em Review;
- [ ] revisar empresa, cargo, respostas e currículo;
- [ ] executar o clique final manualmente, se desejar;
- [ ] registrar o resultado fora de credenciais e fora do CI.

Evidência aceitável:

```text
REVIEW_REACHED
```

Não registrar screenshots, HTML ou logs contendo dados pessoais sem
redação. Nunca anexar senha ou código MFA a uma issue, artifact ou commit.

## Canary 002 — Submission Boundary controlada

Objetivo:

```text
1 Application
→ 1 ReviewSnapshot
→ 1 SubmissionIntent autorizada
→ 1 POST ao servidor local
→ 1 confirmação
→ SUBMITTED
```

Executar somente com o servidor de testes do repositório:

```bash
PATH="$PWD/.venv/bin:$PATH" make test-submission-runtime
```

Os testes cobrem sucesso, erro, timeout, redirect, destino inesperado,
fingerprint alterado e tentativa duplicada. Nenhum teste usa hostname de
empregador ou endpoint externo.

Critérios:

- a tentativa é persistida antes do POST;
- destino, origem, caminho, método e fingerprints coincidem com a política;
- `SUBMIT_UNKNOWN` não é reenviado automaticamente;
- segunda submissão é bloqueada;
- somente confirmação controlada produz `SUBMITTED`;
- resposta e evidência não persistem corpo arbitrário nem PII.

## Pré-condições comuns

Antes de qualquer canary:

```bash
PATH="$PWD/.venv/bin:$PATH" make test
PATH="$PWD/.venv/bin:$PATH" make compile
```

Não executar canary real a partir do CI. Não colocar credenciais em:

```text
.env
profile/*.yaml
GitHub Actions secrets para este fluxo
logs
screenshots
artifacts
commits
```

A autorização de submissão continua separada de `READY_TO_APPLY`:

```text
READY_TO_APPLY
→ Review Snapshot
→ SubmissionIntent
→ authorize-submit
→ submit
```
