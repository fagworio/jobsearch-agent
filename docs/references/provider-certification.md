# Certificação por provider: o que está provado, e onde a função central trava

- **Data:** 2026-09-24
- **Escopo:** resultado REAL dos dois primeiros providers externos exercitados ponta a ponta
  (Lever e Greenhouse). Nada aqui é projeção: cada linha vem de uma execução registrada no
  `journal` e nas tentativas persistidas.

## Vocabulário (fechado)

Certificar "E2E" sem distinguir os níveis esconde exatamente o que falta. Cada provider recebe um
nível por capacidade, e `E2E_CERTIFIED` só existe quando **todas** as linhas passam — incluindo a
aceitação pelo provedor e a confirmação observável:

| nível | o que prova |
| --- | --- |
| `DISCOVERY_SUPPORTED` | a vaga é encontrada e identificada no board |
| `INSPECTION_SUPPORTED` | o formulário real é inspecionado (campos, bindings, fingerprint) |
| `FILL_SUPPORTED` | todas as respostas obrigatórias são resolvidas e preenchidas no DOM |
| `UPLOAD_SUPPORTED` | o currículo chega ao storage do board (uma escrita autorizada) |
| `SUBMISSION_REACHED` | exatamente um POST autorizado de candidatura sai do browser |
| `PROVIDER_ACCEPTANCE` | o provedor **confirma** a candidatura (evidência observável) |
| `E2E_CERTIFIED` | todas as anteriores |

`PROVIDER_ACCEPTANCE` não é "o POST saiu": é haver evidência de candidatura **aceita** — resposta
2xx, marcador de confirmação ou evidência independente (ADR 0006). Sem isso, a Application **não**
vira `SUBMITTED`, e não pode ser descrita como registrada.

## Placar

```text
GREENHOUSE (Fueled — Full Stack Web Engineer, job 370ad5b81414ca5a)
  DISCOVERY_SUPPORTED     ok
  INSPECTION_SUPPORTED    ok
  FILL_SUPPORTED          ok    20 respostas, unanswered_required = []
  UPLOAD_SUPPORTED        ok    upload_writes_used = 1
  SUBMISSION_REACHED      ok    submission_writes = 1 (exatamente uma escrita)
  PROVIDER_ACCEPTANCE     FALHOU  confirmed_submission = false, sem status code
  E2E_CERTIFIED           NÃO

LEVER (CI&T — Senior Software Architect, job 0cd3d1467200558b)
  DISCOVERY_SUPPORTED     ok
  INSPECTION_SUPPORTED    ok
  FILL_SUPPORTED          ok
  UPLOAD_SUPPORTED        ok
  SUBMISSION_REACHED      ok    POST enviado após o humano resolver o desafio
  PROVIDER_ACCEPTANCE     FALHOU  recusa com a candidatura entregue
  E2E_CERTIFIED           NÃO
```

Evidências: [`2026-09-24-fueled-greenhouse-option-mismatch.md`](2026-09-24-fueled-greenhouse-option-mismatch.md)
e [`2026-09-24-lever-ciandt-antibot-refusal.md`](2026-09-24-lever-ciandt-antibot-refusal.md).

## O padrão que muda a prioridade

Dois ATS **diferentes** produziram o mesmo desfecho: o caminho automatizado preenche, sobe o
currículo e chega ao POST; o provedor não confirma. Em um deles um humano chegou a resolver o
desafio **dentro do browser automatizado** e o POST saiu — e ainda assim o provedor não confirmou.
Isso é consistente com o provedor classificando o **ambiente**, não apenas o token do desafio.

Consequências de desenho:

1. **Repetir o envio no browser automatizado não é o caminho padrão.** O humano resolver o desafio
   na mesma sessão automatizada não transforma aquele browser em um browser normal.
2. **Handoff para navegador normal é o caminho mais próximo de fechar a função central.** É o único
   que tira o ambiente automatizado da etapa inevitável.
3. **Nada de mascarar o ambiente.** Fora de escopo, por decisão registrada (ADR 0001/0003): não
   esconder `webdriver`, não alterar fingerprint, não forjar plugins/navegador, não extrair,
   reutilizar ou reinjetar token de desafio.

## Próximo ticket (proposto)

**`JSA-FINAL-MILE-001` — o handoff em navegador normal termina em `SUBMITTED` com evidência
independente.** Provar, de ponta a ponta e em provider real:

```text
agente prepara tudo (fill + upload + pacote de handoff)
-> humano conclui APENAS a etapa inevitável no navegador normal
-> agente procura evidência independente de confirmação (ADR 0006)
-> SUBMITTED, com a evidência anexada
```

Critérios de aceite propostos:

- `application handoff` produz pacote íntegro (`package_sha256`, cópia do currículo pelo conteúdo);
- o humano consegue concluir a candidatura **sem** o browser do agente;
- `report-manual-submit` sozinho **nunca** leva a `SUBMITTED` (o relato cria a incerteza, não a
  satisfaz);
- `reconcile-confirmation` encontra a evidência independente e só então a Application vira
  `SUBMITTED`, com a fonte registrada;
- a ausência de evidência é reportada como ausência — nunca como confirmação.

O retry no browser automatizado permanece existindo como operação **explícita e avançada**, para
diagnóstico ou quando houver evidência de que a recusa anterior não veio do ambiente.

## Cobertura por canal: o adaptador é pré-requisito, o ganho precisa de credencial

Uma leitura anterior deste material apresentou o canal de API como "60% → 0%" — como se escrever o
adaptador convertesse sozinho a taxa de falha em zero. A medição não sustenta essa moldura, e a
diferença importa para o próximo ticket.

O que foi **medido**, no provider real:

```text
GET  boards-api.greenhouse.io/v1/boards/fueledcareers/jobs/5428960008   -> 200
POST boards-api.greenhouse.io/v1/boards/fueledcareers/jobs/5428960008   -> 401 HTTP Basic: Access denied
```

Leitura correta das duas linhas:

```text
o adaptador de API e PRE-REQUISITO       sem ele a credencial nao serve para nada
o ganho depende da CREDENCIAL            sem ela, o cliente nao pode sequer ser exercitado
```

Ou seja: o que o código compra é a **capacidade** de usar um canal que não passa por browser,
desafio nem observação de POST. O que ele não compra é nenhuma redução de falha enquanto não existir
uma credencial declarada — e reduzir a falha é uma afirmação **empírica**, que só pode ser feita
depois de exercitar o endpoint autenticado. Até então, a cobertura do canal de API é **zero
observações**, não "zero falhas".

Por isso a política de canal é conservadora e verificável (`submission_policy.DomainPolicy`):

```text
canal API exige credencial DECLARADA; sem ela, degrada para BROWSER
LinkedIn / Indeed sao sempre HANDOFF
```

A degradação é o ponto: um erro de configuração não deve virar uma tentativa de escrita
sem credencial. E o `SubmissionRouter` (próximo ticket) traz o **próprio** guard — paralelo ao
`NetworkWriteGuard` do browser, não uma reutilização dele: o ciclo de vida é outro (não há página,
DOM, challenge ou trava de submit), e compartilhar o guard do browser acoplaria a contabilidade de
intenção/tentativa a uma fronteira que não existe nesse canal. Enquanto não houver credencial real,
esse router só pode ser exercitado com mocks; o número de cobertura permanece uma **projeção**, e
projeção não entra no placar acima.
