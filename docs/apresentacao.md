# Roteiro de apresentação: caminho feliz

Use o ambiente já instalado e duas janelas de terminal na raiz do repositório. Reserve 10–15 minutos para observar duas gerações; confirme os tempos reais no histórico. Este roteiro não instala nem reinicializa o laboratório.

## 1. Explique a proposta em 30 segundos

> A credencial principal tem uma validade longa, mas a automação externa decide quando trocá-la. Ela pede ao RHBK uma nova secret, distribui essa credencial em um Secret versionado e atualiza os pods gradualmente. Durante 120 segundos, a antiga e a nova funcionam juntas. Depois, a antiga é rejeitada e a aplicação continua com a nova, mantendo o mesmo client_id.

Mostre a [arquitetura](diagramas/arquitetura.png) e a [sequência](diagramas/sequencia.png).

| Conceito | Valor desta demonstração | O que explicar |
|---|---|---|
| Validade principal | 315360000 s, ou 3650 dias | É o limite de validade da credencial. Não é a frequência de troca nem validade infinita. |
| Decisão externa | Mínimo de 300 s; Cron avalia a cada minuto | O RHBK não agenda a troca. O intervalo medido entre dois POSTs foi aproximadamente 6 minutos. |
| Convivência | 120 s após o POST | É o tempo disponível para os consumidores migrarem. O prazo de 2036 da antiga é substituído pelo prazo curto. |
| Distribuição | Secret imutável por geração + rollout | Criar a nova secret no RHBK, sozinho, não atualiza a aplicação. |

Os 10 anos foram escolhidos para testar uma grande margem diante de atrasos da automação. Um atraso pode ultrapassar a data planejada de troca sem expirar a principal, desde que ainda esteja dentro do vencimento real. Para outro uso, dimensione a validade, a cadência e a convivência segundo o risco e o tempo dos consumidores; o exemplo não recomenda guardar a mesma secret por dez anos.

## 2. Confirme o ambiente e a aplicação

Escolha os comandos do ambiente instalado; os comandos sem prefixo são exclusivos do exemplo CRC.

| Ação | CRC | OpenShift configurado |
|---|---|---|
| Conferir destino | `make preflight` | `python3 scripts/openshift.py check` |
| Consultar estado | `make status` | `make openshift-status` |
| Confirmar novas autenticações nos dois pods | `make baseline` | `make openshift-baseline` |
| Observar e gravar evidências | `make watch` | `make openshift-watch` |
| Habilitar automação | `make automation-start` | `make openshift-start` |
| Histórico de rotações | `make history` | `make openshift-history` |
| Suspender automação | `make automation-stop` | `make openshift-stop` |
| Coletar resultado | `make collect` | `make openshift-collect` |

O baseline deve terminar em `PASS`, com duas réplicas estáveis emitindo novas requisições de token. Se uma troca já estiver em andamento, aguarde `COMPLETE` antes de repetir o baseline. Falha de autenticação, `BLOCKED`, imagem indisponível ou falta de status exigem investigação antes da apresentação; não use recuperação ou bootstrap para esconder uma falha.

## 3. Abra a tabela e acompanhe um ciclo

No terminal 1, execute `make watch` (ou `make openshift-watch`). A tabela mostra fase, geração, automação e, por pod, HTTP, sucessos, falhas e atualidade da autenticação. O JSON completo fica no arquivo indicado no início.

No terminal 2, consulte o estado. Se a automação já estiver `ATIVA`, acompanhe. Se estiver `SUSPENSA`, com estado `IDLE`/`COMPLETE` e baseline saudável, habilite-a com o comando da tabela. Um intervalo já vencido pode provocar a primeira troca na próxima avaliação.

| Momento observado | Fala sugerida |
|---|---|
| Principal com HTTP 200 | “Cada pod solicita um token novo a cada dois segundos. Estamos verificando o uso atual da credencial.” |
| `HOLDING_OLD` | “O RHBK já gerou a nova secret. Por 45 segundos, preservamos os consumidores antigos para tornar a convivência visível.” |
| `ROLLING_OUT` | “O controlador distribuiu um Secret versionado e alterou sua referência no Deployment. Novos pods carregam a nova geração.” |
| `VERIFYING` | “Os novos pods autenticam. O teste aguarda o vencimento da antiga para verificar sua rejeição.” |
| `COMPLETE` | “A geração nova continua válida. A antiga foi rejeitada e a troca terminou.” |

Observe HTTP 200 e sucessos aumentando nos pods principais. Os contadores pertencem ao processo e recomeçam em novos pods. Durante o rollout pode haver três pods, pois a estratégia usa `maxUnavailable: 0` e `maxSurge: 1`. `ready=true` sozinho não prova autenticação: a tabela distingue uma amostra recente de status ausente ou desatualizado.

A sonda antiga é separada da aplicação. Seu HTTP 401 após o prazo é esperado e não deve ser somado às falhas dos consumidores principais. Uma sonda encerrada sem status não prova expiração por si só.

## 4. Mostre a distribuição e os prazos

Depois de uma geração chegar a `COMPLETE`, execute `make history` (ou `make openshift-history`). Espere TTL de **3650 dias**, convivência de **120 segundos** e antiga `REJEITADA`. `NORMAL` distingue a troca de uma recuperação de indisponibilidade; não identifica, sozinho, o Job que originou o POST.

No CRC, liste somente metadados dos Secrets:

```sh
oc --kubeconfig=.private/crc.kubeconfig -n rhbk-rotation-lab \
  get secrets -l app.kubernetes.io/part-of=rhbk-secret-rotation-lab \
  -o custom-columns='NOME:.metadata.name,IMUTAVEL:.immutable'
```

No caminho OpenShift, use `.private/openshift.kubeconfig` e o namespace registrado na sua configuração. Mostre os nomes terminados em `vNNNN`, sem exibir os valores das credenciais.

Para ilustrar o teste já executado, mostre estes dados, identificados como referência de **10/11/2026**, horário de São Paulo:

| Antes/depois do POST das 13:01:11 | Vencimento |
|---|---|
| Principal anterior, antes da troca | 07/11/2036 12:56:59 |
| A mesma secret, agora anterior | 10/11/2026 13:03:11 |
| Nova principal | 07/11/2036 13:01:11 |

> O POST reiniciou os 3650 dias da nova secret. A antiga ganhou apenas os 120 segundos de convivência. A validade longa e a frequência externa de rotação são controles diferentes.

## 5. Termine a demonstração

Ctrl+C encerra apenas a visualização. Se quiser manter o caminho feliz rodando, deixe a automação ativa, o host acordado e o cluster disponível. Para encerrar as rotações, use o comando de suspensão da tabela. Ele não cancela um Job já iniciado; aguarde `COMPLETE` e automação `SUSPENSA`, então execute a coleta.

Suspender não desliga a aplicação: ela continua com a credencial atual até seu vencimento real. Não reaplique manifests iniciais, não rode `make bootstrap` e não use `make recover-expired` como passos do caminho feliz. Se uma operação retornar resultado desconhecido, preserve a coleta e reconcilie a intenção existente antes de considerar outro POST.

O painel web é opcional. No CRC, `oc --kubeconfig=.private/crc.kubeconfig -n rhbk-rotation-lab port-forward --address=127.0.0.1 service/m2m-probe 8080:8080` abre acesso em <http://127.0.0.1:8080/>. Ele mostra um pod; o encaminhamento pode terminar quando esse pod for substituído. Use a tabela para acompanhar todas as réplicas.

## Como apresentar o alcance

A feature é Technology Preview no RHBK 26.6 usado no teste. O Keycloak comunitário 26.8 a promoveu a supported; não atribua esse suporte automaticamente ao RHBK. Os resultados mostram validade armazenada, autenticações reais e rollout neste laboratório. Não são uma medição de dez anos, garantia de indisponibilidade zero ou homologação de outro cluster. Consulte os [resultados completos](resultados.md) e o [guia OpenShift](openshift.md).
