# Comportamento medido e limites

## Imagem otimizada e migração para OLM — 10/11/2026

Execução no CRC ARM64 existente, preservando o realm, as credenciais e o estado do laboratório. Os horários desta seção são de **America/Sao_Paulo (BRT)**. Não houve novo bootstrap.

| Verificação | Resultado real |
|---|---|
| Build da imagem RHBK | `rhbk-optimized-1` terminou `Complete`; a imagem publicada foi referenciada por digest no CR. |
| Operator via OLM | CSV `rhbk-operator.v26.6.9-opr.1` em `Succeeded` às 15:03, após aprovação manual do InstallPlan. |
| RHBK otimizado | Novo pod pronto às 15:06, versão `26.6.9.redhat-00002`, argumentos `start --optimized`. Não houve augmentation durante essa inicialização. |
| Configuração preparada no build | `client-secret-rotation` identificado como persistido; health e métricas habilitados. |
| PostgreSQL preservado | Comparação antes/depois confirmou os UIDs e as especificações de Deployment, Service e PVC; o pod manteve UID e contador de reinícios. |
| Outra instalação existente | A comparação confirmou seus UIDs e especificações inalterados. Isso é uma verificação de preservação, não um novo ensaio funcional daquela instalação. |
| Baseline após a migração | `PASS` às 15:07, com os dois pods M2M novamente em HTTP 200. Cada pod registrou 12 falhas durante a interrupção planejada do RHBK: **24 falhas no total**. |
| Rotação v0032 → v0033 após o deploy | **PASS**. POST às 15:08:51; TTL da nova principal de 315360000 s e vencimento da antiga às 15:10:51, após 120 s. |
| Distribuição e rollout após o deploy | 22 autenticações bem-sucedidas com a antiga durante aproximadamente 45 s de espera; rollout de 15:09:37 a 15:09:52, aproximadamente 15 s. |
| Conclusão da rotação | Antiga rejeitada com HTTP 401 `unauthorized_client` às 15:10:56, 125 s após o POST; nova com HTTP 200 logo depois. Estado `COMPLETE`, geração 33, às 15:10:56. |
| Verificação local do código | Suíte final com 279 testes aprovados; esse resultado não substitui execução integrada em outros ambientes. |

O build, o Operator OLM e o servidor otimizado foram exercitados no ambiente existente. **A migração não teve disponibilidade contínua**; as falhas durante a substituição da única réplica não podem ser omitidas nem misturadas com as janelas anteriores de rotação sem falhas. A instalação integral em um segundo CRC vazio e a implantação em outro OpenShift continuam `NOT_RUN`. Digests internos, UIDs e credenciais permanecem nas evidências locais, fora desta síntese pública.

## Validade longa e rotação externa — 10/11/2026

Ambiente: o mesmo CRC ARM64 com RHBK **26.6.9.redhat-00002**, sem SPI. Perfil nativo `expiration-period=315360000`, `rotated-expiration-period=120`, `remaining-rotation-period=0`. Controlador externo com `ROTATE_INTERVAL=300` e Cron `* * * * *`. Horários abaixo em **America/Sao_Paulo**.

| Evidência | Resultado real |
|---|---|
| Recuperação v0012 → v0013 | v0012 comprovadamente vencida; um POST gerou v0013, distribuiu o Secret e concluiu o rollout. A anterior foi invalidada explicitamente. Esta etapa não comprova convivência ou continuidade. |
| Criação e expiração v0013 | POST e criação em 10/11/2026 12:56:59; vencimento em 07/11/2036 12:56:59; diferença exata de 315360000 s. |
| Troca normal v0013 → v0014 | POST às 13:01:11. O vencimento da nova principal passou para 07/11/2036 13:01:11: avanço de 252 s sobre o prazo original. |
| Vencimento da antiga após a troca | v0013 passou de 07/11/2036 12:56:59 para 10/11/2026 13:03:11: 120 s após a criação da nova secret. |
| Autenticações no ciclo manual | 23 autenticações da antiga com HTTP 200 durante aproximadamente 45 s de espera; antiga rejeitada cerca de 125 s após o POST; nova com HTTP 200 antes e depois dessa rejeição. Estado COMPLETE, geração 14. |
| Duas gerações com a automação habilitada | POST da v0015 às 13:07:01 e da v0016 às 13:13:01; ambas com 315360000 s de validade e 120 s de convivência, concluídas. |
| Intervalo medido | 359,705 s entre os términos dos POSTs v0015/v0016. O limiar de 300 s mais a avaliação por minuto produziu aproximadamente 6 minutos. |
| Rejeição próxima ao vencimento | v0015 autenticou às 13:14:58, venceu às 13:15:01 e retornou HTTP 401 `unauthorized_client` às 13:15:02. |

O arquivo de acompanhamento analisado continha 184 snapshots entre 13:09:09 e 13:18:04. Houve 370 observações com status disponível de quatro UIDs de pods principais, todas com HTTP 200, contadores de falhas zero e reinícios zero. Um pod novo teve uma observação inicial sem status. O primeiro snapshot já encontrou v0015 concluída; seu rollout não foi acompanhado nessa janela. Os snapshots isolados não correlacionam cada POST ao UID do CronJob; essa comprovação mais forte pertence ao ensaio T06 anterior.

Esses dados confirmam o prazo **armazenado** de 3650 dias, sua renovação pelo POST e a convivência real curta. Não representam dez anos de observação nem garantem disponibilidade fora das amostras. A validade é finita; um prazo longo também amplia o tempo de uso de uma credencial exposta caso a automação e os controles operacionais falhem.

## Ensaios de expiração curta — 09/11/2026

Execução de referência em 09/11/2026: RHBK **26.6.9.redhat-00002**, Operator **26.6.9-opr.1**, OpenShift Local/CRC ARM64. Os tempos foram reduzidos para demonstração: principal de 300 s, anterior de 120 s, hold de 45 s e intervalo mínimo de decisão de 180 s.

O núcleo da solução e os testes de integração foram executados nesse laboratório. O preparo de um clone novo foi adaptado para este repositório e validado com testes locais; não foi feita uma segunda instalação em um CRC vazio. Os artefatos de cada nova execução ficam em `evidence/`, ignorado pelo Git. Não são publicados dados operacionais, credenciais ou logs do ambiente original.

### Resultados de referência

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

Mostre a [arquitetura](diagramas/arquitetura.png), depois a [sobreposição das credenciais](diagramas/sequencia.png) e execute o [roteiro de apresentação](apresentacao.md). Compare o sucesso dos pods principais com a rejeição esperada do probe antigo. Readiness do processo não comprova que sua autenticação mais recente passou.

`client-secret-rotation` é **Technology Preview**. Uma réplica RHBK e armazenamento local no CRC não validam HA, escala ou recuperação de desastre. Para avaliar produção, reveja suporte da feature, identidade administrativa, distribuição de credenciais, fronteira de confiança do namespace, persistência do estado e tempos reais de rollout.

As adaptações de portabilidade e a nova visualização de terminal são verificadas por testes locais. Não foram implantadas em outro OpenShift, e as novas imagens do controlador não foram aplicadas sobre o laboratório durante a atualização do repositório. O caminho genérico reutiliza um RHBK existente; não representa teste de instalação de Operator, banco ou infraestrutura em outros ambientes.

## Referências primárias

- [Guia de administração do RHBK 26.6](https://docs.redhat.com/en/documentation/red_hat_build_of_keycloak/26.6/html/server_administration_guide/assembly-managing-clients_server_administration_guide).
- [Keycloak comunitário 26.8: Client Secret Rotation supported](https://www.keycloak.org/2026/10/keycloak-2680-released), sem extrapolar essa classificação ao RHBK.
- [Admin REST API do RHBK 26.6](https://access.redhat.com/webassets/avalon/d/red_hat_build_of_keycloak-26.6/rest-api/).
- [Wrapper OIDC upstream 26.6.0](https://raw.githubusercontent.com/keycloak/keycloak/26.6.0/services/src/main/java/org/keycloak/protocol/oidc/OIDCClientSecretConfigWrapper.java): corrobora o ponto da NPE, sem afirmar identidade binária entre upstream e produto.
