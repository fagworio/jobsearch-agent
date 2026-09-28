# Evidência — inspeção Greenhouse pelo Chrome + extensão V2

Data: 2026-09-28  
Branch: `v2-minimal-core`  
Commit: `9b25ecb`

## Escopo

Foi usado o Chromium local com a extensão MV3 carregada como unpacked. A página
Greenhouse foi aberta em uma sessão temporária. O teste chamou o mesmo caminho
do service worker que o side panel usa: aba ativa → `tabs.sendMessage` → content
script → snapshot neutro.

Nenhum campo foi preenchido, nenhum arquivo foi enviado e nenhum botão de
submit foi acionado.

## Resultado observado

- Browser: Chromium 153.0.8010.47.
- URL: `https://job-boards.greenhouse.io/gitlab/jobs/8556658002`.
- Provider detectado: `greenhouse`.
- Page type: `application`.
- Formulário pronto: `true`.
- Campos encontrados: `30`.
- Challenge observado: `BLOCKING`.
- A extensão retornou `ok: true` no comando `INSPECT_FORM`.

## Interpretação

O resultado comprova o caminho de leitura no browser real e confirma que o
challenge é exposto ao backend como estado humano observável. Não comprova
fill, upload ou submit; esses gates exigem uma vaga escolhida conscientemente e
autorização para transmitir o currículo e enviar a candidatura.
