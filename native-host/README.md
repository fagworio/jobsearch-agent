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
