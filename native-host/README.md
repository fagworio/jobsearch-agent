# Native Messaging host

O host é deliberadamente limitado ao transporte e ao handshake inicial. Ele
responde `HELLO` e `PING`; não recebe cookies, executa JavaScript nem contém
regras de ATS ou de candidatura.

Para instalar, use um executável que rode o módulo Python no ambiente do
projeto e o ID da extensão carregada no Chrome:

```bash
./native-host/install.sh EXTENSION_ID /absolute/path/to/job-agent-v2-native-host
```

Para Chromium, use `JOB_AGENT_V2_BROWSER_DIR=chromium` antes do comando.

O executável precisa escrever somente frames Native Messaging em stdout. Logs
devem ir para stderr.

## Perfil persistente do Agent

Para abrir uma página com a extensão já carregada em um perfil dedicado e
persistente do Chrome:

    ./native-host/run-chrome-v2.sh \
      https://job-boards.greenhouse.io/givedirectly/jobs/4738257005

O perfil fica, por padrão, em ~/.config/job-agent-v2, fora do repositório.
Faça o login manualmente nesse Chrome na primeira execução. Os cookies e a
sessão ficam salvos nesse perfil e serão reutilizados nas próximas execuções;
se o Greenhouse expirar a sessão, o Chrome pedirá login novamente. A extensão
não lê nem armazena sua senha.

Para escolher outro local persistente, use:

    JOB_AGENT_V2_CHROME_PROFILE=/caminho/seguro ./native-host/run-chrome-v2.sh
