# Observar a convivência entre as secrets

Este observador acompanha **uma rotação feita pela automação que já está ativa**. Ele lê o estado do controlador e os Secrets versionados indicados nesse estado, e solicita tokens novos usando a credencial antiga e a nova. Assim você vê quando ambas autenticam e quando a antiga deixa de funcionar.

O observador não chama `POST /client-secret`, não altera a policy, não cria Jobs e não muda recursos do OpenShift. Não precisa de uma credencial administrativa do RHBK. As únicas chamadas ao RHBK são solicitações de token com `client_credentials`; não há cache de token.

## Preparar uma vez

Execute na raiz do checkout. Use o mesmo procedimento no CRC ou em outro OpenShift:

```sh
umask 077
mkdir -p .private
cp config/watch-secrets.example.env .private/watch-secrets.env
```

Edite o arquivo privado com os valores do ambiente. O arquivo contém nomes, URLs e caminhos; não coloque valores de secrets nele. Use `CHAVE=valor` literal, sem aspas e sem expressões como `$HOME`. Não use `source`: o script lê o arquivo sem executá-lo como shell.

| Parâmetro | O que preencher |
|---|---|
| `KUBECONFIG`, `CONTEXT` | Arquivo e contexto explícitos do cluster da demonstração. |
| `EXPECTED_SERVER` | URL esperada da API OpenShift. |
| `NAMESPACE` | Namespace da aplicação e do controlador de rotação. Pode ser diferente do namespace RHBK. |
| `STATE_CONFIGMAP` | ConfigMap que contém o `state.json` do controlador. |
| `RHBK_URL`, `REALM`, `CLIENT_ID` | Servidor HTTPS, realm e client M2M observados. |
| `CA_FILE` | Arquivo da CA confiável do RHBK. Vazio usa a confiança TLS padrão do sistema. |
| `INTERVAL_SECONDS` | Intervalo inteiro entre amostras, em segundos. Padrão: `2`. |
| `DURATION_SECONDS` | Duração em segundos. `0`, padrão, acompanha até Ctrl+C. |
| `WATCH_TZ` | Fuso dos horários mostrados. Padrão: `America/Sao_Paulo`. |
| `REPORT_FILE` | Caminho opcional de um TSV novo; vazio não grava relatório. |

São necessários Bash 3.2 ou superior, `oc`, `curl`, `jq`, acesso de leitura ao estado e aos Secrets versionados dessa instalação, e conectividade HTTPS com o RHBK. Os relógios do computador e do servidor precisam estar sincronizados para comparar autenticações com o vencimento registrado.

Os valores do arquivo de configuração têm precedência sobre variáveis de ambiente; opções de linha de comando têm precedência sobre o arquivo. O kubeconfig informado no arquivo prevalece sobre um `KUBECONFIG` já exportado no terminal. O contexto global não é escolhido implicitamente. A identidade escolhida precisa poder ler os objetos; o script não concede permissões. Embora os valores das secrets sejam lidos para autenticar, eles não aparecem na tabela nem no relatório. São exibidos somente nomes e gerações, horários, prazos e resultados.

No laboratório CRC anterior, os valores de referência são:

```text
CONTEXT=crc-admin
EXPECTED_SERVER=https://api.crc.testing:6443
NAMESPACE=rhbk-rotation-lab
STATE_CONFIGMAP=rotation-state
RHBK_URL=https://rhbk-rotation.apps-crc.testing
REALM=lab-secret-rotation
CLIENT_ID=catalogo-api
WATCH_TZ=America/Sao_Paulo
```

Preencha `KUBECONFIG` e, quando necessário, `CA_FILE` com os caminhos absolutos dos seus arquivos privados. Esses nomes são apenas a configuração do exemplo CRC; o observador não os exige para outros ambientes.

## Iniciar

Depois de configurar o arquivo:

```sh
make watch-secrets
```

Para usar outro arquivo:

```sh
make watch-secrets WATCH_CONFIG=/caminho/privado/watch-secrets.env
```

Para uma única rodada, ou dez minutos de observação sem redesenhar a tela:

```sh
bash scripts/watch-secrets.sh --config .private/watch-secrets.env --once --plain
bash scripts/watch-secrets.sh --config .private/watch-secrets.env --duration 600 --plain
```

O horário de cada linha é o início daquela solicitação de token. As duas chamadas são sequenciais e têm horários próprios. O intervalo configurado é a pausa entre rodadas, além do tempo das consultas e autenticações. O código de saída `0` indica que a observação terminou normalmente; a conclusão do teste está no diagnóstico, não nesse código.

Deixe a janela aberta até acompanhar uma troca completa. Na demonstração, a rotação se torna elegível depois de 300 segundos e o CronJob avalia a cada minuto; pode levar cerca de seis minutos até o próximo POST, além dos atrasos de agendamento. A convivência configurada é de 120 segundos. O observador usa o vencimento efetivo registrado pelo controlador, em vez de iniciar um contador próprio de 120 segundos.

Se a automação estiver suspensa, o observador continuará apenas acompanhando o estado disponível. Ele não a inicia e não força uma rotação para produzir resultados.

Se a API do cluster estiver indisponível, o observador informa o bloqueio; não inicia CRC, reinicia pods ou modifica configurações para contorná-lo. Resolva o acesso ao ambiente antes de repetir a observação. A nova validação integrada deste observador ainda não foi executada: o CRC estava indisponível durante a preparação.

## Interpretar o resultado

A tabela identifica o papel (`ANTERIOR`, `NOVA` ou `ATUAL`), a geração, o nome do Secret, HTTP, resultado e segundos restantes da antiga. Os horários incluem data e offset do fuso configurado. `ATUAL` indica a credencial atual quando ainda não há um par de rotação para acompanhar.

Durante uma troca normal, procure esta sequência:

1. A nova geração aparece no estado e seu Secret versionado fica disponível.
2. Antes de vencer o prazo da antiga, as duas credenciais obtêm HTTP 200 em solicitações novas de token.
3. Depois do vencimento, a antiga recebe rejeição de autenticação, enquanto a nova continua com HTTP 200.

O HTTP 401 da **antiga após seu vencimento** é esperado nesse teste. Ele não é uma falha da aplicação que já passou a usar a nova credencial. Falha de rede, TLS ou erro de servidor não comprova expiração.

O diagnóstico `PASS_EXPIRACAO_OBSERVADA` exige que esta execução tenha presenciado ambas com HTTP 200 antes do vencimento e, pelo menos cinco segundos depois dele, a rejeição da antiga (HTTP 400/401 com `invalid_client` ou `unauthorized_client`) com a nova ainda autenticando. O par precisa pertencer ao mesmo ciclo estável; histórico salvo não substitui essas observações. Se você começar depois que a antiga já venceu, verá o estado atual, mas isso sozinho não prova que as duas funcionaram ao mesmo tempo: o ciclo fica inconclusivo. Aguarde uma próxima rotação para observar a janela completa.

Exemplo ilustrativo de leitura, sem valores de credenciais:

| Momento | Anterior | Nova | Diagnóstico |
|---|---|---|---|
| Durante a convivência | HTTP 200 | HTTP 200 | `CONVIVENCIA_OBSERVADA` |
| Depois do vencimento e da margem de cinco segundos | HTTP 401 esperado | HTTP 200 | `PASS_EXPIRACAO_OBSERVADA`, se a convivência foi observada nessa execução |
| Observação iniciada depois do vencimento | HTTP 401 | HTTP 200 | `INCONCLUSIVO` |

`AGUARDANDO` significa que ainda falta uma condição para observar o par. Uma recuperação que invalida antecipadamente a credencial antiga não conta como prova de expiração natural.

O observador verifica credenciais e prazos. Ele não mede a disponibilidade de todos os pods nem substitui o acompanhamento do rollout por `make watch` ou `make openshift-watch`.

Validação em 11/11/2026: **304 testes locais aprovados**, incluindo dez cenários do shell com APIs simuladas. Um teste adicional usou `curl` real com HTTPS e CA explícita contra um servidor local de teste; ambas as credenciais fictícias autenticaram sem exposição de valores na saída ou no relatório. A tentativa com o CRC foi bloqueada pela indisponibilidade da API; esses resultados não representam uma nova rotação observada no RHBK real.

## Guardar a observação e encerrar

Se configurar `REPORT_FILE`, escolha um caminho privado para um arquivo **novo**. O relatório TSV usa permissão `0600`, contém somente dados da observação e não inclui secrets ou tokens. Não reutilize o nome de um relatório existente; use um nome por execução.

Para encerrar, pressione **Ctrl+C**. Isso para somente o observador: a automação e a aplicação continuam no estado em que estavam. Se quiser suspender a automação, faça essa operação separadamente pelo comando de controle correspondente ao seu ambiente.
