# Guia do operador e do apresentador

Este laboratório demonstra a rotação de um client secret com decisão externa ao Red Hat build of Keycloak (RHBK), distribuição por Secrets Kubernetes imutáveis e rollout explícito de uma aplicação M2M. Use um CRC local reservado para estudos. O recurso `client-secret-rotation` é Technology Preview; uma instância RHBK neste ambiente não demonstra alta disponibilidade nem prontidão para produção.

Os comandos são executados na raiz do clone. As saídas geradas em `evidence/` descrevem somente a execução local. Este guia não inclui credenciais, identidades de cluster ou resultados de outro ambiente.

## 1. Requisitos e versão

- Git, Make, Python 3.12 ou superior, `crc`, `oc` e Podman funcionando para o login interativo no registry e a extração do bundle oficial. Quando disponível, Skopeo é usado para extrair o bundle sem criar um container. As aplicações usam somente a biblioteca padrão do Python; não é necessário instalar pacotes com `pip`.
- CRC com o preset OpenShift e nó `arm64`. O código e os digests deste laboratório estão fixados para essa arquitetura e recusam outro tipo de nó. O suporte do CRC a outras arquiteturas não torna este pacote automaticamente compatível com elas.
- Acesso de administrador ao CRC, DNS `*.apps-crc.testing` funcional e armazenamento `crc-csi-hostpath-provisioner` disponível.
- Conta e autenticação autorizadas para obter as imagens oficiais em `registry.redhat.io`, além do pull secret exigido pelo CRC. O mirror local cobre RHBK, a base Python, PostgreSQL e Operator.
- Capacidade para RHBK, PostgreSQL, builds, duas réplicas da aplicação e um pod extra durante o rollout. Um perfil de referência é 6 CPUs e 18 GiB para a VM; não é um mínimo garantido. Verifique capacidade livre no host e preserve outros workloads.

Instale e prepare o CRC conforme a [documentação oficial](https://crc.dev/docs/installing/). Em um ambiente novo, o fluxo oficial começa com `crc setup`, `crc start` e configuração do `oc` via `crc oc-env`; o [guia inicial do CRC](https://crc.dev/docs/getting-started/) descreve a autenticação. O pull secret solicitado por `crc start` é um arquivo privado.

Esta edição do código fixa RHBK **26.6.9** e valida a versão efetiva no bootstrap. Consulte `versions.lock.yaml` e os digests dos manifests. Não substitua a imagem por `latest`: atualizar a versão exige rever o lock, as imagens, a validação do bootstrap e executar novamente os ensaios. “Última versão” é uma verificação pontual, não uma propriedade permanente do repositório.

## 2. Clonar e preparar os arquivos privados

Clone a distribuição pública:

```sh
git clone https://github.com/csfreitas/rhbk-client-secret-rotation.git rhbk-client-secret-rotation
cd rhbk-client-secret-rotation
umask 077
mkdir -p .private
chmod 700 .private
make test
```

Os testes unitários não acessam o cluster e não substituem os ensaios com RHBK. O clone deve iniciar sem evidências ou credenciais de outra instalação. O preparador recusa um namespace preexistente sem a identidade registrada por este checkout; não o use para adotar ou reinstalar por cima de outro laboratório.

Se o kubeconfig do `oc` já possui um contexto **administrativo local** chamado `crc-admin`, exporte somente esse contexto com o preparador. Ajuste o caminho de origem e o nome do contexto se sua instalação do CRC usar outros valores:

```sh
python3 scripts/setup.py configure \
  --kubeconfig "$HOME/.kube/config" --context crc-admin
oc --kubeconfig=.private/crc.kubeconfig config view --minify \
  -o jsonpath='{.clusters[0].cluster.server}'
```

A última linha deve mostrar somente `https://api.crc.testing:6443`. O preparador valida essa API e grava `.private/crc.kubeconfig` com modo `0600`. Se o contexto não existir, conclua primeiro a autenticação administrativa descrita pelo CRC e repita a seleção do contexto correto. Não copie o kubeconfig inteiro de outros ambientes e não desabilite a verificação TLS.

Prepare a autenticação do registry em arquivo, usando o prompt do Podman. A variável contém somente o caminho, não uma senha:

```sh
export REGISTRY_AUTH_FILE="$PWD/.private/registry-auth.json"
podman login --authfile "$REGISTRY_AUTH_FILE" registry.redhat.io
chmod 600 "$REGISTRY_AUTH_FILE"
```

Se usar apenas Skopeo no host, substitua `podman login` por `skopeo login` com os mesmos argumentos. Não use senha em argumento, em `export` ou em histórico de comandos. Em outro terminal, repita somente o `export` do caminho. O pull secret do CRC e este arquivo do host atendem consumidores diferentes; configurar um não garante a autenticação do outro.

| Arquivo privado | Como é obtido |
|---|---|
| `.private/crc.kubeconfig` | `setup.py configure`, com arquivo de origem e contexto administrativo local explícitos |
| `.private/registry-auth.json` | `podman login --authfile`, sem senha no comando |
| `.private/route-ca.crt` | Preparação/implantação lê apenas a CA pública do ingress do CRC |
| `.private/rotation-admin.secret` | Bootstrap obtém a credencial da identidade própria do laboratório e grava com modo `0600` |
| `.private/runtime.json` | Bootstrap grava URLs e caminhos privados para os ensaios; não deve ser editado com credenciais em linha |

Não é necessário obter ou copiar manualmente o valor de `catalogo-api`. A inicialização e o controlador usam Secrets Kubernetes. A identidade administrativa periódica é diferente do superadministrador usado na preparação do realm.

## 3. Instalar antes de iniciar o relógio da aplicação

```sh
make preflight
make setup
make mirror
make deploy
make build
```

`preflight` verifica a API local e a arquitetura, e registra a identidade deste CRC para as próximas operações. `setup` cria o namespace exclusivo, obtém as CRDs do bundle oficial e instala somente as ausentes; preserva as existentes compatíveis. Também habilita a Route padrão do registry local quando necessário e grava a CA pública do ingress. Essas duas preparações de cluster são para o CRC de estudos; não se aplicam a um cluster compartilhado de produção.

O mirror mantém os digests oficiais; TLS continua validado. `deploy` instala Operator, banco e RHBK no namespace `rhbk-rotation-lab` e aguarda a infraestrutura. `build` aguarda os dois Builds terminarem e fixa os novos digests da aplicação e do controlador nos manifests locais. Não inicialize workloads com o marcador de imagem `build-required`: ele deve ter sido substituído pelos digests produzidos.

Antes do bootstrap, confira a infraestrutura:

```sh
oc --kubeconfig=.private/crc.kubeconfig -n rhbk-rotation-lab get pods,builds
oc --kubeconfig=.private/crc.kubeconfig -n rhbk-rotation-lab \
  rollout status deployment/rhbk-operator --timeout=180s
oc --kubeconfig=.private/crc.kubeconfig -n rhbk-rotation-lab \
  rollout status deployment/postgresql --timeout=180s
oc --kubeconfig=.private/crc.kubeconfig -n rhbk-rotation-lab \
  rollout status statefulset/rhbk --timeout=300s
```

Prossiga somente com os Builds `Complete` e workloads prontos. `Pending`, `ImagePullBackOff`, falha de TLS ou pressão de memória são bloqueios reais. Resolva a causa antes de iniciar os prazos; repetir bootstrap não corrige um pull de imagem.

### Opcional antes da demonstração: teste completo de expiração

Este é o melhor momento para o ensaio longo: a aplicação principal ainda não foi inicializada. Prepare somente realm, política e administrador, depois instale RBAC e o CronJob suspenso:

```sh
python3 scripts/bootstrap.py --realm-only
oc --kubeconfig=.private/crc.kubeconfig -n rhbk-rotation-lab \
  apply --server-side --field-manager=rhbk-rotation-lab -f deploy/rbac/
oc --kubeconfig=.private/crc.kubeconfig -n rhbk-rotation-lab \
  apply --server-side --field-manager=rhbk-rotation-lab \
  -f deploy/workloads/rotation-controller.json
make test-post-expiration
```

Esse bloco é para a primeira instalação. Não reaplique o CronJob inicial sobre uma execução existente. O ensaio exige agendamento suspenso e nenhum Job ativo, cria clients descartáveis no realm do laboratório e compara:

- POST antecipado, próximo do vencimento e após o vencimento;
- metadados de S1 e S2 antes/depois do POST;
- autenticações novas em ambos os lados dos vencimentos original, rotacionado e novo;
- client de controle sem política de expiração.

Reserve dezenas de minutos e mantenha o host e o CRC acordados. Leia `evidence/post-expiration/summary.json` e `report.md`; os eventos JSONL preservam as medições. Um POST com HTTP 200 ou um valor diferente de secret não prova renovação do prazo. `FAIL` e `BLOCKED` não são aprovação, e erro de rede/TLS não prova expiração.

### Iniciar a aplicação

```sh
make bootstrap
oc --kubeconfig=.private/crc.kubeconfig -n rhbk-rotation-lab \
  rollout status deployment/m2m-probe --timeout=90s
make baseline
```

Agora começa o TTL do client principal. O baseline deve confirmar duas réplicas da mesma geração, com novas autenticações bem-sucedidas. Inicie logo a demonstração ou os ciclos automáticos; a geração inicial expira mesmo que ninguém execute outra ação.

## 4. Configuração que será demonstrada

| Parâmetro | Padrão e fonte |
|---|---|
| Realm / client da aplicação | `lab-secret-rotation` / `catalogo-api` |
| Identidade administrativa periódica | `rotation-admin`, fora da política de expiração do alvo |
| TTL principal / anterior / janela de atualização | 300 s / 120 s / 60 s em `realm/profile.json` |
| Seleção da política | Papel marcador `lab-secret-rotation`, em `realm/policy.json` |
| Frequência de avaliação | CronJob a cada minuto; intervalo mínimo de rotação de 180 s |
| Espera / orçamento de rollout / margem | 45 s / 60 s / 15 s, no manifest do controlador |
| Aplicação | Duas réplicas, grant novo a cada 2 s, secret lido uma vez ao iniciar |
| Rollout | `maxUnavailable: 0`, `maxSurge: 1`; referência explícita ao Secret da nova geração |

Mantenha esses valores para reproduzir os ensaios. Alterações de TTL exigem coerência entre política, controlador e parâmetros de teste. O controlador verifica a janela efetiva retornada pelo RHBK; a soma dos tempos configurados, sozinha, não garante que ainda existe tempo suficiente.

## 5. Demonstração curta: uma rotação

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
```

O comando aguarda a conclusão, incluindo a rejeição comprovada da credencial antiga. Não dispare outra rotação enquanto ele trabalha. Use este roteiro de apresentação:

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

## 6. Demonstração automática: três ciclos reais

Com estado terminal, nenhuma execução ativa e aplicação saudável:

```sh
make baseline
make test-periodic
```

O teste retoma o CronJob uma vez, observa a aplicação e aguarda ao menos três ciclos novos. Não execute comandos de rotação, recuperação ou alteração da política durante essa janela. O limite padrão é 960 segundos, incluindo a parada controlada.

Uma aprovação exige correlação entre cada geração, seu Job criado pelo CronJob, POST, conclusão, rejeição de S1 e autenticação de S2. Um Job que decidiu não rotacionar não conta como ciclo. Ao terminar, o teste suspende o agendamento, aguarda Jobs ativos e confirma a suspensão persistida.

O caminho do resumo é impresso e fica em `evidence/periodic/`. Ele separa falhas da aplicação de rejeições esperadas nas sondas antigas. Os totais por pod são limites inferiores observados: requisições entre a última amostra e a exclusão do pod podem não aparecer. Não transforme “zero falhas observadas” em garantia de disponibilidade geral.

Para manter a automação fora desse teste, use `make automation-start`, acompanhe com `make watch` e finalize com `make automation-stop`. Não misture esse modo interativo com uma execução T06 em andamento.

## 7. Parar, retomar e recuperar

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

## 8. Ensaios adicionais e interpretação

Execute um cenário por vez, com baseline saudável e agendamento suspenso. Os ensaios operacionais deixam o CronJob suspenso:

| Comando | Objetivo |
|---|---|
| `make test-delayed-rollout` | Reter S1 além da convivência, observar falha esperada e recuperar S2 |
| `python3 tests/integration/operations.py T10` | Concorrência entre execução agendada e manual, protegida por Lease |
| `make test-recovery` | Falha após o POST e reconciliação sem repetir a rotação |
| `python3 tests/integration/operations.py T12` | Falha na escrita do Secret e no rollout |
| `python3 tests/integration/operations.py T16` | Rollback dentro e fora da validade, seguido da restauração de S2 |
| `make test-put-comparison` | Comparar atualização por PUT com o comportamento do POST |

T07 e T16 provocam falhas deliberadas no laboratório. Seus erros esperados não devem ser somados à medição de continuidade de uma demonstração normal. Após ensaios longos, confirme se a geração ainda está válida antes do próximo comando.

O contrato de saída distingue `PASS`, `FAIL`, `BLOCKED`, `NOT_RUN` e `NOT_APPLICABLE`. Leia a razão e os eventos; a existência de um arquivo ou o sucesso de um teste unitário não prova execução integrada. A rejeição de credencial só é aceita com controle temporal, sucesso prévio e controle positivo da credencial nova.

## 9. Limpeza e limites de acesso

Suspenda a automação, aguarde Jobs e preserve as coletas locais necessárias. Para revisar e depois remover os recursos exclusivos do laboratório:

```sh
python3 scripts/cleanup.py --dry-run
make cleanup
```

A limpeza valida a identidade registrada e o ownership; remove o namespace `rhbk-rotation-lab`, incluindo seus Secrets e PVC, e os dois objetos globais dedicados à leitura de ingress. Ela não executa `crc delete`, não remove CRDs compartilhadas e não restaura ou altera outros projetos. As CRDs oficiais e a Route padrão do registry habilitadas por `make setup` permanecem no CRC; podem ser compartilhadas por outros workloads. A limpeza registra apenas os recursos exclusivos do laboratório como removíveis.

O controlador acessa credenciais do namespace e pode criar pods; o namespace inteiro é a fronteira de confiança. Não coloque recursos de outros projetos ali. A sonda M2M não recebe token Kubernetes. Os relatórios filtram campos sensíveis, mas os arquivos privados, kubeconfig, pull secrets e dumps administrativos nunca devem ser publicados.

Para iniciar outra instalação após remover o laboratório, use um clone novo e seu próprio kubeconfig. Não reutilize a identidade registrada de um CRC que foi recriado.
