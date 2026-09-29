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

Para abrir uma página no perfil dedicado e persistente do Chrome:

    ./native-host/run-chrome-v2.sh \
      https://job-boards.greenhouse.io/givedirectly/jobs/4738257005

O perfil fica, por padrão, em ~/.config/job-agent-v2, fora do repositório.
Na primeira execução, o Chrome atual pode exigir uma instalação manual única:
abra chrome://extensions, ative o modo do desenvolvedor, escolha “Carregar
sem compactação” e selecione a pasta extension/ do projeto. Isso ocorre porque
as versões atuais do Chrome bloqueiam o carregamento silencioso de extensões
por linha de comando. Depois dessa instalação, ela permanece no perfil.

Abra também a tela de login do MyGreenhouse e faça o login manualmente nesse
Chrome:

    ./native-host/run-chrome-v2.sh https://my.greenhouse.io/users/sign_in

Os cookies e a sessão ficam salvos nesse perfil e serão reutilizados nas
próximas execuções; se o Greenhouse expirar a sessão, o Chrome pedirá login
novamente. A extensão apenas observa o estado visível da página e não lê nem
armazena sua senha, código de segurança ou cookies.

Com a aba de login ativa, a situação pode ser conferida pelo painel da
extensão ou pelo backend:

    PYTHONPATH=src python3 -m job_agent_v2.cli login-status

Depois do login, navegue nessa mesma janela para a página de candidatura e
abra o formulário “Easy Apply”. O inspector usa o diálogo visível
(`role="dialog"`, incluindo o formulário React com scroll interno) como root,
então `fill` usa a sessão já guardada para anexar o PDF e preencher os campos
e comboboxes dessa tela. `submit` continua sendo uma ação separada e
explicitamente autorizada.

O parâmetro JOB_AGENT_V2_USE_COMMAND_LINE_EXTENSION=1 fica disponível para
Chromium ou builds de Chrome que ainda aceitem esse mecanismo, mas não é
necessário para a instalação persistente do Chrome atual.

Para escolher outro local persistente, use:

    JOB_AGENT_V2_CHROME_PROFILE=/caminho/seguro ./native-host/run-chrome-v2.sh
