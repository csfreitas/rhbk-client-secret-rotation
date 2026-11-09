# Comportamento medido e limites

Execução de referência em 09/11/2026: RHBK **26.6.9.redhat-00002**, Operator **26.6.9-opr.1**, OpenShift Local/CRC ARM64. Os tempos foram reduzidos para demonstração: principal de 300 s, anterior de 120 s, hold de 45 s e intervalo mínimo de decisão de 180 s.

O núcleo da solução e os testes de integração foram executados nesse laboratório. O preparo de um clone novo foi adaptado para este repositório e validado com testes locais; não foi feita uma segunda instalação em um CRC vazio. Os artefatos de cada nova execução ficam em `evidence/`, ignorado pelo Git. Não são publicados dados operacionais, credenciais ou logs do ambiente original.

## Resultados de referência

| Verificação | Resultado observado |
|---|---|
| Três ciclos originados pelo CronJob | 770 autenticações novas da aplicação principal, 770 sucessos e nenhuma falha observada durante a janela de aproximadamente 13 minutos. Não é garantia de disponibilidade fora dessa janela. |
| Distribuição e rollout | Secrets imutáveis por geração; hold de 45 s; substituição dos dois pods em aproximadamente 12 s. O probe separado do secret anterior passou de sucesso para rejeição após o prazo. |
| POST antes, próximo e após o vencimento | A expiração de S2 foi aproximadamente o instante do POST + 300 s. S2 autenticou após a expiração original de S1 e foi rejeitado após sua própria expiração. |
| Controle sem política | O POST não atribuiu prazo de expiração; S2 continuou válido após o intervalo de referência. |
| Atraso deliberado no rollout | Produziu indisponibilidade esperada; a reconciliação retomou a geração já criada sem outro POST. |
| Concorrência e interrupção lógica | O Lease bloqueou o concorrente. Uma interrupção após POST e antes da distribuição foi reconciliada sem gerar outra credencial. Não foi um corte de energia ou perda real de pacote de rede. |
| Rollback | A geração anterior funcionou durante a convivência e foi rejeitada depois de vencida. |

Na execução original do teste de POST, o predicado aceitava apenas OAuth `invalid_client`. Esta build retornou HTTP 401 `unauthorized_client`. Uma revisão dos dados já coletados confirmou os controles temporais e positivos, preservando o registro original. O teste disponível aqui aceita ambas as respostas de rejeição; rede, TLS, timeout e HTTP 403 não comprovam expiração.

O **POST após vencimento revalidou S1 como secret anterior** durante 120 s. Por isso `make recover-expired` persiste S2, invalida explicitamente o anterior e faz o rollout imediato. Essa operação recupera uma aplicação já indisponível; não representa continuidade de um ciclo normal.

## POST e PUT têm papéis diferentes

O fluxo principal usa:

```text
POST /admin/realms/{realm}/clients/{uuid}/client-secret
```

O RHBK gera S2. A automação é externa porque decide quando executar, distribui o resultado e coordena os pods; ela não gera o valor desse secret.

No comparativo opcional, o teste gerou um valor aleatório externamente e o enviou no campo `secret` de uma atualização genérica do client:

```text
PUT /admin/realms/{realm}/clients/{uuid}
```

| Condição do PUT parcial | Resultado |
|---|---|
| Com política, antes do vencimento | O valor externo autenticou, S1 foi rejeitado e a expiração permaneceu igual à original. Não reproduziu a renovação do prazo observada no POST. |
| Com política, após o vencimento | HTTP 500; cenário bloqueado. O servidor registrou `NullPointerException` ao chamar `Map.put` sobre `ClientRepresentation.getAttributes()` nulo. O corpo enviado continha somente `secret`. |
| Sem política | O valor externo autenticou e não recebeu prazo de expiração. |

O 500 indica uma falha no tratamento dessa representação parcial no caminho exercitado. Não demonstra que credenciais geradas externamente sejam proibidas. Não se testou outro corpo ou workaround, nem se mediram efeitos persistidos após o erro; HTTP 500 não comprova rollback. Não use PUT como substituto do fluxo principal com base neste comparativo.

## Uso na apresentação

Mostre a [arquitetura](diagramas/arquitetura.png), depois a [sobreposição das credenciais](diagramas/sequencia.png) e execute o [roteiro do operador](operacao.md). Compare o sucesso dos pods principais com a rejeição esperada do probe antigo. Readiness do processo não comprova que sua autenticação mais recente passou.

`client-secret-rotation` é **Technology Preview**. Uma réplica RHBK e armazenamento local no CRC não validam HA, escala ou recuperação de desastre. Para avaliar produção, reveja suporte da feature, identidade administrativa, distribuição de credenciais, fronteira de confiança do namespace, persistência do estado e tempos reais de rollout.

## Referências primárias

- [Guia de administração do RHBK 26.6](https://docs.redhat.com/en/documentation/red_hat_build_of_keycloak/26.6/html/server_administration_guide/assembly-managing-clients_server_administration_guide).
- [Admin REST API do RHBK 26.6](https://access.redhat.com/webassets/avalon/d/red_hat_build_of_keycloak-26.6/rest-api/).
- [Wrapper OIDC upstream 26.6.0](https://raw.githubusercontent.com/keycloak/keycloak/26.6.0/services/src/main/java/org/keycloak/protocol/oidc/OIDCClientSecretConfigWrapper.java): corrobora o ponto da NPE, sem afirmar identidade binária entre upstream e produto.
