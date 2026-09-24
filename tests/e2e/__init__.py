"""E2E controlado (JSA-E2E-001).

Um ATS de teste, servido em loopback, que fala o **mesmo contrato de formulário**
de um provider suportado (`job_application[...]`, `form#application_form`,
`data-provider="greenhouse"`). Assim o caminho percorrido é o do produto —
adapter real, orquestrador real, boundary de submissão real, browser real — e o
que é controlado é só o *endpoint*, nunca o sucesso.

O que ele existe para provar, na ordem:

    página → inspeção → respostas → preenchimento → upload → POST real →
    servidor recebe → servidor responde → browser observa a confirmação →
    Application SUBMITTED

Nada de chamar classe interna para "simular sucesso": o POST atravessa HTTP de
verdade, carrega o PDF de verdade e o servidor valida os dois.
"""
