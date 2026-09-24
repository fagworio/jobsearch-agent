# Real run: Fueled (Greenhouse) — a resposta declarada não é a opção do board

- **Date:** 2026-09-24
- **Board:** `https://job-boards.greenhouse.io/fueledcareers/jobs/5428960008`
- **Job:** `Full Stack Web Engineer` — Fueled (`370ad5b81414ca5a`, `external_id 5428960008`)
- **Applications:** `application-32566c3e6f9c0e36` (3ª execução), `application-…` (2ª execução)
- **Command:** `apply-to-completion 370ad5b81414ca5a --no-headless --db data/jobsearch.db`
- **Result:** `NEEDS_ANSWER`, `submission_attempted: false`, `submission_writes: 0`, `cycles: 1`,
  `questions_answered: 19`, `unanswered_required: ["What are your pronouns? *"]`

Nada foi enviado — o Safety Gate para ANTES do preenchimento quando falta resposta obrigatória.
Foi exatamente por isso que o defeito abaixo apareceu como telemetria mentirosa, e não como um
formulário enviado errado.

## O que a execução mostrou

```text
questions_answered: 19          unanswered_required: ["What are your pronouns? *"]
resume_sha256: 21dd96e8abe07ba064a60bf7f023f705ee7a5181a8996a0a427e1992a134c00d
unanswered_required não é a verdade do formulário: é o que o MODELO acha.
```

A 3ª execução dizia 19 respondidas. As opções reais do board
(`GET https://boards-api.greenhouse.io/v1/boards/fueledcareers/jobs/5428960008?questions=true`)
mostram que **seis** dessas respostas não existem como opção:

| Campo | Resposta registrada | Opções reais do board |
| --- | --- | --- |
| `question_18722965008` "How did you hear about this opportunity?" | `Linkedin` | Employee referral / Job board / Fueled Careers page / Search engine (Google, Bing, etc.) / Social media / Other |
| `question_18722968008` "…experience working in a digital agency or consulting firm?" | **texto gerado** (parágrafo de motivação) | 5 opções sobre anos de agência/consultoria |
| `question_18722970008` "At Fueled, we manage diverse project scopes…" | **texto gerado** | 5 opções sobre escopo de projetos |
| `question_18722971008` "select the region where you currently live" | `Latin America` | `Latin America (Mexico, Central America, South America & Caribbean)` |
| `question_18722972008` "…sponsorship … in the U.S.?" | `No` (= *"No - I am authorized to work in the U.S."*) | …/ **`Not applicable - I am located outside of the U.S.`** |
| `question_18722973008` "how much notice do you need…?" | `Immediately` | **`Available immediately`** / 2 weeks / 3–4 weeks / … |

Duas dessas são graves e não são a mesma coisa que "não casou o rótulo":

1. **`No` no sponsorship afirma work authorization nos EUA.** O candidato mora no Brasil e nunca
   declarou isso. A opção "No" do board é "No - I am authorized to work in the U.S. and will not
   require sponsorship or immigration support now or in the future."
2. **Texto gerado dentro de um combobox.** O widget só aceita as opções da lista; a redação não é uma
   delas, então o campo obrigatório ficaria vazio — e a telemetria dizia "respondida".

## Causa

- `_fill_value` **digitava a resposta antes de ler a lista**. O combobox do Greenhouse é
  react-select: digitar FILTRA as opções. "Immediately" não é prefixo de "Available immediately", a
  lista zerava e o preenchimento parava (ou o campo ficava vazio).
- O `binding` estático só conhece `aria-controls`/`aria-owns`; o react-select não preenche nenhum dos
  dois (batiza o listbox de `react-select-<id-do-input>-listbox`), então todo combobox do board caía
  em `UNSUPPORTED_COMBOBOX_LISTBOX_UNBOUND`.
- O resolvedor só mapeava para uma opção quando `field.options` vinha preenchido no HTML — e o
  typeahead do Greenhouse carrega as opções **depois do clique**. Sem opções, a resposta saía na
  forma canônica ("Immediately") ou ia para geração, inclusive em widget fechado.

## Correções (JSA-PROVIDER-CERT-001A, continuação)

- `options.py`: tabelas forma↔valor canônico (`notice_period`, `region`, `employment_type`,
  `referral_source`, `sponsorship`). Nada inventa fato: cada forma é outra maneira de escrever o
  mesmo valor declarado, e a evidência continua sendo a origem original.
- `browser.py`: **abrir → casar contra as opções REAIS → só então digitar**; descoberta do listbox
  pela convenção `react-select-<id>-listbox`; digitar usa o valor inteiro e, se nada aparecer, o
  primeiro componente antes da vírgula (o typeahead de cidade só responde a "Betim"). Quando nada
  casa: `OPTION_NOT_FOUND_COMBOBOX_OPTION` — falha alta, nunca grava parecido.
- `resolver.py`: widget fechado (`combobox`/`select`/`radio`) **nunca** recebe texto gerado
  (`closed_option_without_declared_answer`); a resposta de sponsorship é reescrita para a SITUAÇÃO do
  candidato quando a pergunta é sobre os EUA e ele mora fora (derivada de `profile.identity.country`,
  que entra na evidência).

## Verificação

```text
opção real do board            resposta                    resultado
Available immediately          Immediately (declarado)     casa pelo alias, sem digitar
Latin America (…Caribbean)     Latin America               casa por prefixo
Not applicable - I am located  (derivado do país)          casa exato
outside of the U.S.
Social media                   Social media (declarado)    casa exato
```

`tests/test_option_filling.py` fixa a ordem nova (inclusive o typeahead de cidade e a falha alta);
`tests/test_question_resolver.py` fixa a reescrita de sponsorship (fora dos EUA e dentro) e a guarda
de widget fechado. Suíte: 497 testes.

O que **não** está provado: nenhuma candidatura foi enviada a este board. O envio continua sendo o
próximo passo, e depende das respostas que só o candidato pode dar (pronomes, experiência em agência
e escopo de projetos).
