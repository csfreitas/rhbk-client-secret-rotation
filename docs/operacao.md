# Operação e manutenção do laboratório CRC existente

Este guia preserva os comandos do laboratório CRC já instalado e seus ensaios. **Para instalar RHBK em um ambiente novo, inclusive CRC, use a [instalação comum OpenShift](imagem-rhbk.md).** A configuração CRC é apenas um arquivo de parâmetros desse procedimento. Para adicionar aplicação e automação a uma instalação nova, continue pelo [guia da demonstração](openshift.md).

Os comandos abaixo dependem das identidades e arquivos privados do checkout que criou o laboratório anterior. Não use esses scripts para adotar recursos de outro checkout ou reproduzir a infraestrutura em outro cluster. A migração documentada aqui é um procedimento de manutenção, separado da instalação nova.

Build otimizado, migração OLM, deploy, baseline e rotação foram verificados nesse CRC em 10/11/2026. A substituição da única réplica RHBK causou 24 falhas de autenticação, seguidas de recuperação do baseline; veja [resultados](resultados.md). O recurso `client-secret-rotation` é Technology Preview no RHBK 26.6.

## Acesso e estado do laboratório

Na raiz do checkout existente, se precisar preparar novamente o acesso local, selecione explicitamente o contexto administrativo CRC:

```sh
python3 scripts/setup.py configure \
  --kubeconfig "$HOME/.kube/config" --context crc-admin
oc --kubeconfig=.private/crc.kubeconfig config view --minify \
  -o jsonpath='{.clusters[0].cluster.server}{"\n"}'
make preflight
make status
```

A API precisa ser `https://api.crc.testing:6443`. O preparador exporta somente esse contexto para `.private/crc.kubeconfig`; não altera recursos. Preserve `evidence/` e `.private/` do checkout, inclusive identidades, CA e credencial administrativa da automação. Não publique esses diretórios.

Para apenas observar ou apresentar, use `make watch`, `make baseline` e o [roteiro de apresentação](apresentacao.md). Não repita setup, deploy ou bootstrap para iniciar uma apresentação.

## Manutenção da imagem e do Operator

Os helpers CRC continuam fixados ao namespace, arquitetura ARM64 e versões do laboratório anterior. `make build-rhbk` constrói a imagem otimizada usando o mirror já verificado; `make deploy` exige o Operator OLM pronto e valida o banco existente antes de preservá-lo. A substituição da única réplica pode interromper autenticações. Suspenda a rotação, conclua Jobs ativos e guarde a coleta antes de qualquer troca.

### Migrar uma instalação existente com Operator manual

Use somente no checkout que já registra as identidades desse laboratório. Suspenda a automação e aguarde todos os Jobs; o estado precisa ser `IDLE` ou `COMPLETE`. Preserve a coleta antes da troca:

```sh
make automation-stop
make status
oc --kubeconfig=.private/crc.kubeconfig -n rhbk-rotation-lab get jobs
make collect
make build-rhbk
make operator-prepare
```

O build reutiliza o espelho oficial já existente por digest, sem exigir a tag `pinned`. Se a imagem não estiver disponível, pare e prepare o acesso ao registry; não adote ImageStreams de outro projeto. Não é necessário repetir `setup` ou criar novas credenciais nessa migração.

Consulte o plano apontado pela Subscription e revise o CSV, origem, recursos e CRDs compartilhadas antes de aprovar:

```sh
oc --kubeconfig=.private/crc.kubeconfig -n rhbk-rotation-lab \
  get subscription rhbk-operator -o jsonpath='{.status.installPlanRef.name}{"\n"}'
oc --kubeconfig=.private/crc.kubeconfig -n rhbk-rotation-lab \
  get installplan install-NOME_REAL -o yaml
```

A aprovação da migração exige o parâmetro explícito:

```sh
python3 scripts/operator_olm.py approve --plan install-NOME_REAL --migrate-manual
make operator-status
make deploy
make baseline
make collect
```

O helper registra a configuração anterior, para o Operator manual e aguarda seus pods terminarem antes de aprovar o plano. OLM passa a gerenciar o Operator; os recursos RHBK e banco não são apagados. **Não execute `make bootstrap` nessa migração**: a geração e o estado existentes devem ser preservados. Uma réplica RHBK pode interromper grants durante sua substituição; isso não é uma migração de disponibilidade contínua. Só retome `make automation-start` depois das verificações. Se a transição bloquear, preserve os registros de `evidence/operator-olm/`; não inicie um segundo Operator nem apague CRDs para contornar a falha.

### Opcional antes da demonstração: teste completo de expiração

Este ensaio altera o perfil do realm do laboratório. Execute somente em uma janela reservada para testes; não o execute durante uma apresentação. Suspenda a automação e confirme que todos os Jobs terminaram antes de mudar o perfil:

```sh
make automation-stop
make status
oc --kubeconfig=.private/crc.kubeconfig -n rhbk-rotation-lab get jobs
python3 scripts/bootstrap.py --realm-only --profile short
make test-post-expiration
python3 scripts/bootstrap.py --realm-only --profile long-lived
```

O ensaio usa RBAC e controlador já instalados; não reaplique os manifests iniciais sobre uma geração em execução. Com agendamento suspenso e nenhum Job ativo, ele cria clients descartáveis no realm do laboratório e compara:

- POST antecipado, próximo do vencimento e após o vencimento;
- metadados de S1 e S2 antes/depois do POST;
- autenticações novas em ambos os lados dos vencimentos original, rotacionado e novo;
- client de controle sem política de expiração.

Reserve dezenas de minutos e mantenha o host e o CRC acordados. Leia `evidence/post-expiration/summary.json` e `report.md`; os eventos JSONL preservam as medições. O ensaio exige o perfil curto e recusa o perfil de 3650 dias antes de criar clients. Um POST com HTTP 200 ou um valor diferente de secret não prova renovação do prazo. `FAIL` e `BLOCKED` não são aprovação, e erro de rede/TLS não prova expiração.

### Voltar à apresentação

Depois de restaurar o perfil longo, confirme o estado e a aplicação antes de retomar a automação:

```sh
make status
make baseline
```

O baseline deve confirmar duas réplicas da mesma geração, com novas autenticações bem-sucedidas. Alterar um perfil não recalcula o vencimento de uma geração já emitida. Se a aplicação precisar de recuperação, use a seção de recuperação abaixo; não use bootstrap para reinicializar o laboratório.

## Configuração que será demonstrada

| Parâmetro | Padrão e fonte |
|---|---|
| Realm / client da aplicação | `lab-secret-rotation` / `catalogo-api` |
| Identidade administrativa periódica | `rotation-admin`, fora da política de expiração do alvo |
| TTL principal / anterior / janela de atualização | 315360000 s / 120 s / 0 em `realm/profile.json` |
| Seleção da política | Papel marcador `lab-secret-rotation`, em `realm/policy.json` |
| Frequência de avaliação | CronJob a cada minuto; intervalo mínimo de rotação de 300 s; aproximadamente 6 min medidos |
| Espera / orçamento de rollout / margem | 45 s / 60 s / 15 s, no manifest do controlador |
| Aplicação | Duas réplicas, grant novo a cada 2 s, secret lido uma vez ao iniciar |
| Rollout | `maxUnavailable: 0`, `maxSurge: 1`; referência explícita ao Secret da nova geração |

Os prazos de validade, convivência e decisão externa são independentes. O perfil curto `realm/profile-short.json` (300/120/60) é reservado ao ensaio que aguarda o vencimento da principal. Alterações de TTL exigem coerência entre política, controlador e parâmetros de teste. O controlador verifica a janela efetiva retornada pelo RHBK; a soma dos tempos configurados, sozinha, não garante que ainda existe tempo suficiente.

`Remaining expiration time` é a janela anterior ao vencimento em que um UPDATE via Dynamic Client Registration pode provocar rotação; não é um timer. Com `0`, não há antecipação por essa janela; o POST administrativo pode forçar a troca, e a convivência de 120 s e o intervalo externo de 300 s continuam independentes.

O intervalo externo não é um temporizador exato: a avaliação pode ocorrer antes de completar 300 s desde o POST anterior e aguardar o próximo minuto. O perfil longo evita uma expiração precoce quando a automação atrasa, até o limite real da credencial. Não significa validade infinita.

## Demonstração curta: uma rotação

Em um terminal:

```sh
make watch
```

Em outro terminal, com baseline recém-confirmado:

```sh
make status
make rotate-once
make baseline
make collect
make history
```

O comando aguarda a conclusão, incluindo a rejeição comprovada da credencial antiga. Não dispare outra rotação enquanto ele trabalha. `make watch` mostra uma tabela e grava JSONL completo em um arquivo datado de `evidence/crc/`; `make watch-json` preserva a saída técnica. `make status` e `make history` são consultas, sem criação de Jobs. Use este roteiro de apresentação:

| Momento | O que mostrar e explicar |
|---|---|
| Antes do POST | Dois pods autenticam com a mesma geração. Cada tentativa solicita um token novo; não há cache mascarando a validade do secret. |
| Após o POST | RHBK gera S2 e informa os prazos. A decisão de executar o POST veio do controlador externo. |
| Espera | Os pods existentes conservam S1; criar S2 no servidor não troca a credencial na aplicação. |
| Distribuição e rollout | Surge um Secret imutável versionado. O controlador altera o PodTemplate explicitamente; novos pods iniciam com S2. |
| Conclusão | Os dois pods da nova geração autenticam. A sonda antiga, separada, demonstra a rejeição de S1 após o prazo efetivo. |

Para o painel web de um pod:

```sh
oc --kubeconfig=.private/crc.kubeconfig -n rhbk-rotation-lab \
  port-forward service/m2m-probe 8080:8080
```

Abra <http://127.0.0.1:8080/>. Esse painel mostra um pod escolhido pelo Service; não agrega as réplicas. O port-forward pode terminar quando esse pod for substituído; reabra-o. Use `make watch` para comparar todos os pods e separar a aplicação das sondas antigas.

## Demonstração automática: três ciclos reais

Com estado terminal, nenhuma execução ativa e aplicação saudável:

```sh
make baseline
make test-periodic
```

O teste retoma o CronJob uma vez, observa a aplicação e aguarda ao menos três ciclos novos. Não execute comandos de rotação, recuperação ou alteração da política durante essa janela. O limite padrão é 1800 segundos, incluindo a parada controlada; reserve até 30 minutos. A ampliação acomoda o intervalo externo de 300 s e os atrasos de agendamento. Essa alteração do orçamento foi validada localmente; uma nova execução T06 com o perfil longo ainda não foi realizada.

Uma aprovação exige correlação entre cada geração, seu Job criado pelo CronJob, POST, conclusão, rejeição de S1 e autenticação de S2. Um Job que decidiu não rotacionar não conta como ciclo. Ao terminar, o teste suspende o agendamento, aguarda Jobs ativos e confirma a suspensão persistida.

O caminho do resumo é impresso e fica em `evidence/periodic/`. Ele separa falhas da aplicação de rejeições esperadas nas sondas antigas. Os totais por pod são limites inferiores observados: requisições entre a última amostra e a exclusão do pod podem não aparecer. Não transforme “zero falhas observadas” em garantia de disponibilidade geral.

Para manter a automação fora desse teste, use `make automation-start`, acompanhe com `make watch` e finalize com `make automation-stop`. Não misture esse modo interativo com uma execução T06 em andamento.

## Parar, retomar e recuperar

```sh
make automation-stop
make status
make collect
```

Suspender o CronJob não cancela um Job já iniciado, não desliga os pods e não estende o TTL. Verifique a lista `active_jobs` da coleta e aguarde a conclusão antes de iniciar outro ensaio. Se a suspensão for recusada por uma Lease ocupada, deixe a execução atual terminar e repita a suspensão; não force a remoção da Lease.

| Situação encontrada | Próxima ação |
|---|---|
| Estado terminal e baseline saudável, com validade suficiente | `make automation-start` para retomar o modo periódico |
| Ciclo interrompido, estado não terminal e não bloqueado | `make reconcile`, depois `make baseline`; a intenção existente é retomada sem outro POST |
| Secret principal vencido após suspensão, estado `IDLE` ou `COMPLETE` | `make recover-expired`, `make baseline` e então `make automation-start` |
| Estado `BLOCKED`, linhagem ambígua ou identidade de cluster divergente | Preserve a coleta e investigue a causa; não apague estado, não modifique timestamps e não repita POST às cegas |

A recuperação explícita registra a indisponibilidade anterior, gera uma nova credencial, invalida a anterior e restaura a aplicação. Ela deixa o agendamento suspenso; não representa um ciclo normal com continuidade. Se houver um ciclo pendente, reconcilie esse ciclo antes de tentar uma recuperação nova.

`crc stop` interrompe a VM inteira. Antes, suspenda e conclua os Jobs. Após `crc start`, execute `make preflight` e `make status`; o tempo transcorrido pode ter vencido a credencial. Não use `make bootstrap` como reinicializador de geração: sua reexecução preserva a geração existente.

## Ensaios adicionais e interpretação

Execute um cenário por vez, com baseline saudável e agendamento suspenso. Os ensaios operacionais deixam o CronJob suspenso:

O comparativo PUT, assim como o teste completo de expiração por POST, exige `bootstrap.py --realm-only --profile short` antes do ensaio e restauração por `--profile long-lived` ao terminar. Ambos conferem o perfil real e recusam prazos que ultrapassem o orçamento do ensaio. O perfil curto não é necessário para as rotações normais demonstradas no roteiro.

| Comando | Objetivo |
|---|---|
| `make test-delayed-rollout` | Reter S1 além da convivência, observar falha esperada e recuperar S2 |
| `python3 tests/integration/operations.py T10` | Concorrência entre execução agendada e manual, protegida por Lease |
| `make test-recovery` | Falha após o POST e reconciliação sem repetir a rotação |
| `python3 tests/integration/operations.py T12` | Falha na escrita do Secret e no rollout |
| `python3 tests/integration/operations.py T16` | Rollback dentro e fora da validade, seguido da restauração de S2 |
| `make test-put-comparison` | Com perfil curto, comparar atualização por PUT com o comportamento do POST |

T07 e T16 provocam falhas deliberadas no laboratório. Seus erros esperados não devem ser somados à medição de continuidade de uma demonstração normal. Após ensaios longos, confirme se a geração ainda está válida antes do próximo comando.

O contrato de saída distingue `PASS`, `FAIL`, `BLOCKED`, `NOT_RUN` e `NOT_APPLICABLE`. Leia a razão e os eventos; a existência de um arquivo ou o sucesso de um teste unitário não prova execução integrada. A rejeição de credencial só é aceita com controle temporal, sucesso prévio e controle positivo da credencial nova.

## Limpeza e limites de acesso

Suspenda a automação, aguarde Jobs e preserve as coletas locais necessárias. Para revisar e depois remover os recursos exclusivos do laboratório:

```sh
python3 scripts/cleanup.py --dry-run
make cleanup
```

A limpeza valida a identidade registrada e o ownership; remove o namespace `rhbk-rotation-lab`, incluindo seus Secrets, PVC e recursos OLM locais, e os dois objetos globais dedicados à leitura de ingress, se existirem. Ela não executa `crc delete`, não remove CRDs compartilhadas e não restaura ou altera outros projetos. As CRDs instaladas pelo OLM e a Route padrão do registry preparada por `make setup` permanecem no CRC; podem ser compartilhadas por outros workloads. A limpeza registra apenas os recursos exclusivos do laboratório como removíveis.

O controlador acessa credenciais do namespace e pode criar pods; o namespace inteiro é a fronteira de confiança. Não coloque recursos de outros projetos ali. A sonda M2M não recebe token Kubernetes. Os relatórios filtram campos sensíveis, mas os arquivos privados, kubeconfig, pull secrets e dumps administrativos nunca devem ser publicados.

Para iniciar outra instalação após remover o laboratório, use um clone novo, seu próprio kubeconfig e a [instalação comum OpenShift](imagem-rhbk.md). Não reutilize a identidade registrada de um CRC que foi recriado.
