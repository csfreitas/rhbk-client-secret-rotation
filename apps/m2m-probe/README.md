# Sonda M2M de geração fixa

`probe.py` utiliza somente a biblioteca padrão do Python. `CLIENT_SECRET`, `CLIENT_ID` e `GENERATION` são lidos uma única vez, na inicialização. Uma nova requisição `client_credentials` é feita em cada tentativa; o token retornado é descartado. Não existe acesso à Admin API, à API Kubernetes, recarga de credenciais ou fallback.

O endereço `TOKEN_URL` deve ser HTTPS e o arquivo `TLS_CA_FILE` contém a CA da Route. Redirecionamentos são recusados. O código OAuth é registrado somente se estiver em uma lista permitida; descrições de erro, respostas completas, credenciais, tokens e cabeçalhos nunca são publicados. Problemas TLS, rede, timeout, resposta inválida e rejeição OAuth são diferenciados.

A sonda serve em 8080:

- `/healthz`: processo e heartbeat da execução; rejeição de credencial não falha a liveness.
- `/readyz`: aguarda o primeiro sucesso de autenticação. Após esse sucesso, erros de autenticação são observados nas métricas e não removem o pod do Service. Isso permite medir o cenário de secret expirado sem reinícios ou mascaramento por readiness.
- `/status`: geração, pod, contadores, último HTTP, último resultado e epoch do último sucesso. O controlador exige sucesso recente por pod antes de concluir um rollout.
- `/metrics`: tentativas, sucessos, falhas e último resultado por pod, geração e função.
- `/`: painel simples do pod que atende à conexão. Não agrega réplicas; `make watch` acompanha o conjunto.

O Deployment possui duas réplicas, `maxUnavailable: 0`, `maxSurge: 1` e quatro segundos mínimos de prontidão. Cada novo pod referencia explicitamente um Secret imutável versionado. A ServiceAccount recebe acesso de pull de imagens no namespace, e o token Kubernetes não é montado na sonda.

`PROBE_ROLE=old` identifica a sonda do secret anterior. Seus resultados são separados das métricas da aplicação principal. O controlador cria essa sonda clonando o PodTemplate; não é necessário um manifest separado.

`SIGTERM` e `SIGINT` encerram o laço de forma normal. O timeout de requisição padrão de cinco segundos cabe nos dez segundos concedidos pelo Deployment para término.

Para visualizar um pod específico:

```sh
oc --kubeconfig .private/crc.kubeconfig -n rhbk-rotation-lab port-forward pod/NOME_DO_POD 8080:8080
```

Abra <http://127.0.0.1:8080>. A interface não fornece comandos de rotação e não possui Route pública.

A imagem usa base Python por digest e nenhum pacote instalado por `pip`. O runtime suporta UID arbitrário, sistema de arquivos somente para leitura e execução sem privilégios. Os testes unitários não validam o comportamento do RHBK; essa comprovação depende dos ensaios integrados no CRC.
