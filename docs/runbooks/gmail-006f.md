# Runbook: Gmail read-only e o gate de 006F

Este runbook é do **JSA-CONF-006F** (acesso real, somente leitura) e prepara o
**JSA-CONF-007** (a primeira confirmação real de candidatura por e-mail). Ele não
confirma candidatura nenhuma: o único efeito é estabelecer e verificar o acesso.

## Antes de começar

O agente nunca pede senha da conta. O acesso é OAuth com um único escopo,
`https://www.googleapis.com/auth/gmail.readonly` — não marca como lido, não move,
não exclui e não envia. Um `token.json` cujos escopos registrados passem disso é
recusado pelo próprio código, e uma autorização mais ampla é rejeitada **antes**
de o token ser gravado.

## 1. Google Cloud

```text
Gmail API                     habilitada no projeto
OAuth consent screen          configurado
OAuth Client                  tipo "Desktop app"   (é o que usa InstalledAppFlow)
Test Users                    sua conta adicionada, se o app estiver em Testing
```

## 2. Credencial local

```bash
mkdir -p ~/.config/jobsearch-agent/gmail
chmod 700 ~/.config/jobsearch-agent/gmail
# baixar o JSON do OAuth Client como:
#   ~/.config/jobsearch-agent/gmail/client_secret.json
chmod 600 ~/.config/jobsearch-agent/gmail/client_secret.json
```

O diretório fica fora do repositório por padrão. Se apontar
`JOBSEARCH_GMAIL_DIR`/`--gmail-dir` para dentro do projeto, o `.gitignore` cobre
`client_secret.json`, `token.json` e `gmail/` como defesa em profundidade.

## 3. Grupo opcional

```bash
pip install --index-url https://pypi.org/simple \
  'google-api-python-client>=2.100,<3.0' 'google-auth-oauthlib>=1.2,<2.0'
```

O núcleo não depende dessas bibliotecas: nenhum import do Google acontece no
carregamento do CLI, e há teste que prova isso.

## 4. Autorizar

```bash
jobsearch-agent integrations gmail authorize
```

Abre o consentimento no browser normal, recebe o callback em `localhost` e grava
`token.json` com `0600`. **Só estabelece acesso.** Não confirma candidatura.

## 5. Os seis gates

| # | gate | como verificar |
|---|------|----------------|
| 1 | authorize real concluído | o comando acima terminou com `authorized: true` |
| 2 | `status` mostra só metadados | `jobsearch-agent integrations gmail status` — sem valor de token |
| 3 | `token.json` em `0600` | `stat -c '%a %n' ~/.config/jobsearch-agent/gmail/token.json` |
| 4 | escopo exatamente `gmail.readonly` | o `status` imprime `scopes: ["https://www.googleapis.com/auth/gmail.readonly"]` |
| 5 | listagem real funciona | `jobsearch-agent integrations gmail check` |
| 6 | nenhum conteúdo logado | o `check` responde `messages_read: 0` e imprime só contagens |

O gate 5/6 usa `messages.list`, que devolve **apenas ids**: nenhum assunto,
remetente ou corpo entra no processo nem no stdout, e nenhum `get` é feito. É a
checagem mínima capaz de provar credencial, escopo e conectividade sem trazer
conteúdo para dentro do agente.

Se o `check` falhar com `ConfirmationSourceUnavailable`, a leitura não está
funcionando — e isso **não** é ausência de confirmação.

## 6. Sobre o refresh token expirar

Com o projeto OAuth em **External + Testing**, o Google expira o refresh token em
aproximadamente **7 dias** para escopos como `gmail.readonly`. Isso é
comportamento documentado do Google, não defeito do refresh implementado aqui.

O sintoma é o esperado e já tem mensagem própria:

```text
GmailAuthError: refresh token was refused; reauthorize with `integrations gmail authorize`
```

Ou seja: reautorizar resolve, e nada de estado de candidatura é afetado. Para uso
contínuo, o projeto OAuth precisa sair de Testing (app aprovado/publicado). Para o
teste pontual do 007, Testing é suficiente.

## 7. JSA-CONF-007: o teste decisivo

Ordem, sem atalho:

```text
CI&T permanece HANDOFF_IN_PROGRESS
        ↓
você envia manualmente no browser normal
        ↓
jobsearch-agent application report-manual-submit <application-id>
        ↓
AWAITING_SUBMISSION_CONFIRMATION
        ↓
jobsearch-agent application reconcile-confirmation <application-id>
        ↓
evidência persistida (e o que não bastou também)
        ↓
confidence >= 0.5 com corroboração  →  SUBMITTED
```

`report-manual-submit` **nunca** marca `SUBMITTED`: o relato cria a incerteza.
Nenhum comando aceita fonte ou referência digitadas — a única evidência que conta
é a que um observador independente encontra.

O que observar no resultado:

```text
detected[]                  o que foi encontrado, com signals e confidence
accepted                    a evidência aceita, ou null
detail                      "observed but not accepted: ..." quando ficou abaixo do piso
```

Se a confirmação real não aparecer:

- a janela começa em `MANUAL_SUBMISSION_REPORTED` menos 15 minutos — um e-mail
  anterior a isso é ignorado de propósito;
- a frase de recebimento é porta, mas sozinha fica abaixo do piso: é preciso
  corroboração (origem no domínio do ATS, empresa ou referência da vaga);
- linguagem de recusa é veto: "obrigado pelo interesse… infelizmente" não é
  confirmação;
- a Application continua em `AWAITING_SUBMISSION_CONFIRMATION`, e reexecutar o
  `reconcile-confirmation` é seguro e idempotente.
