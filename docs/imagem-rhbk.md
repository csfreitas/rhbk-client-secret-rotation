# Instalar RHBK no OpenShift

Este é o procedimento de instalação para **CRC e demais ambientes OpenShift**. O Dockerfile, os templates e os comandos são os mesmos; muda somente o arquivo de parâmetros. O fluxo instala o Operator pelo OLM, constrói a imagem otimizada no cluster e cria o servidor por um `Keycloak` CR.

Os [templates](../deploy/install/) usam APIs nativas do OpenShift. O [Dockerfile](../apps/rhbk/Dockerfile) prepara `client-secret-rotation`, PostgreSQL, health e métricas com `kc.sh build`. Nenhuma senha, client secret, certificado ou configuração de realm entra na imagem. A automação de rotação é instalada depois, pelo [guia da demonstração](openshift.md).

**Validação:** 294 testes locais aprovados, renderização dos dois exemplos e 12 recursos aceitos pelo `dry-run` do servidor CRC em 10/11/2026, sem criar recursos. Os componentes já foram executados no laboratório anterior, conforme [resultados](resultados.md). A instalação integral pelo procedimento comum abaixo em um ambiente novo ainda é `NOT_RUN`. Não execute essa instalação sobre o laboratório que já está em operação; para ele, consulte [manutenção CRC](operacao.md).

## 1. Escolher os parâmetros

Requisitos do ambiente:

- OpenShift com OLM, Build API, registry interno e Route disponíveis. O procedimento não habilita nem altera esses serviços globais.
- Namespace novo reservado ao servidor; acesso para criar seus recursos e instalar o Operator. A aprovação OLM pode alterar CRDs globais e exige revisão do administrador do cluster.
- Imagem base oficial RHBK, ou seu mirror autorizado, fixada por digest e compatível com a arquitetura selecionada. A versão precisa ser compatível com o Operator escolhido; os exemplos não consultam a última versão automaticamente.
- PostgreSQL acessível e suas credenciais em Secrets, ou o banco opcional de demonstração abaixo. DNS, capacidade e política de rede precisam permitir a comunicação.
- Hostname coberto pelo certificado do ingress, CA confiável e acesso autenticado às imagens do catálogo Red Hat. Esta topologia usa **Route com TLS edge**, HTTP entre router e RHBK e `xforwarded`. Outras topologias de TLS exigem ajustar o CR e a Route em conjunto.
- No computador do operador: Git, Make, Python 3.12+, `oc` compatível com o cluster e `curl`.

Na raiz do checkout:

```sh
umask 077
mkdir -p .private
cp config/rhbk-install.openshift.env .private/rhbk-install.env
```

Para CRC, use `config/rhbk-install.crc.env` como origem, no lugar do arquivo acima. Edite o arquivo privado com os valores reais. Não copie os dois exemplos por cima um do outro. Os campos principais são:

| Grupo | Parâmetros |
|---|---|
| Destino | `KUBECONFIG`, `CONTEXT`, `EXPECTED_SERVER`, `NAMESPACE` |
| Imagens e nós | `ARCHITECTURE`, `RHBK_BASE_IMAGE`, `BUILD_NAME` |
| Operator | `OPERATOR_GROUP`, `OPERATOR_CHANNEL`, `OPERATOR_CSV`, `OPERATOR_CATALOG`, `OPERATOR_CATALOG_NAMESPACE` |
| Servidor | `RHBK_NAME`, `RHBK_HOSTNAME`, `RHBK_INSTANCES`, recursos de CPU/memória, `RHBK_CA_FILE` |
| Banco | `DB_HOST`, `DB_PORT`, `DB_NAME`, nomes e chaves dos Secrets de usuário/senha |
| Bootstrap | `BOOTSTRAP_SECRET`, com chaves `username` e `password` |
| Banco opcional | `PG_NAME`, `PG_IMAGE`, `PG_STORAGE_CLASS`, `PG_STORAGE_SIZE` |

Use caminhos absolutos para kubeconfig e CA. O formato é `CHAVE=valor`, sem aspas, espaços ou expressões de shell; escolha caminhos sem espaços. O arquivo contém referências a Secrets, nunca seus valores. O digest de `RHBK_IMAGE`, imagem final otimizada, será obtido do Build; não use o digest da imagem base nesse campo.

```sh
make rhbk-render PARAMS=.private/rhbk-install.env
```

Esse comando é local e gera `.private/rhbk-install/operator.json` e `build.json`. Revise os nomes, namespace, imagem base e escopo do Operator antes de continuar. Os templates não criam recursos de nuvem.

Depois da validação, carregue somente o arquivo que você acabou de revisar e defina um comando que sempre usa seu contexto explícito:

```sh
. .private/rhbk-install.env
oc_target() {
  oc --kubeconfig "$KUBECONFIG" --context "$CONTEXT" -n "$NAMESPACE" "$@"
}

oc_target config view --minify \
  -o jsonpath='{.clusters[0].cluster.server}{"\n"}'
oc_target get namespace kube-system -o jsonpath='{.metadata.uid}{"\n"}'
oc_target get nodes -L kubernetes.io/arch
```

Compare a API com `EXPECTED_SERVER`, confirme o cluster e a presença de nós da arquitetura escolhida. O kubeconfig deve validar TLS. Em outro terminal, carregue novamente o arquivo e a função; não substitua esses comandos por `oc` com contexto ambiente.

## 2. Preparar namespace, banco e Secrets

```sh
oc_target create namespace "$NAMESPACE"
```

Se o namespace já existir, pare: este roteiro de instalação nova não adota recursos existentes. Um administrador pode preparar um namespace dedicado por outro processo, mas precisa revisar conflitos e propriedade antes de usar os manifests. Não use `apply` para contornar um erro de recurso já existente.

Provisione os Secrets de banco e bootstrap nesse namespace pelo processo autorizado do ambiente. Se quiser criar os Secrets dos exemplos, cujo usuário e senha do banco ficam no mesmo objeto, digite os valores em arquivos privados:

```sh
python3 - <<'PY'
from getpass import getpass
from pathlib import Path
for name in ('db-username', 'db-password', 'bootstrap-username', 'bootstrap-password'):
    path = Path('.private') / name
    path.write_text(getpass(name + ': '))
    path.chmod(0o600)
PY
```

Confira que `DB_USERNAME_SECRET` e `DB_PASSWORD_SECRET` têm o mesmo valor para este bloco; caso use Secrets separados, provisione cada um com sua chave correspondente:

```sh
oc_target create secret generic "$DB_USERNAME_SECRET" \
  --from-file="$DB_USERNAME_KEY=.private/db-username" \
  --from-file="$DB_PASSWORD_KEY=.private/db-password"
oc_target create secret generic "$BOOTSTRAP_SECRET" \
  --from-file=username=.private/bootstrap-username \
  --from-file=password=.private/bootstrap-password
```

Para um banco existente, confirme database, porta, credenciais e conectividade e siga à próxima seção. O servidor não cria o database externo.

**Banco opcional de demonstração:** em qualquer OpenShift, inclusive CRC, preencha `PG_IMAGE` por digest, StorageClass e tamanho do PVC. Configure `DB_HOST` igual a `PG_NAME`, porta 5432, e o usuário/database que o container PostgreSQL provisionará. O template usa os mesmos Secrets preparados acima. A imagem precisa estar acessível à ServiceAccount `default` desse namespace; se necessário, provisione um pull Secret e associe com `oc_target secrets link default NOME_DO_SECRET --for=pull` antes do deploy:

```sh
make rhbk-render-postgres PARAMS=.private/rhbk-install.env
oc_target create --dry-run=server -f .private/rhbk-install/postgres-demo.json
oc_target create -f .private/rhbk-install/postgres-demo.json
oc_target rollout status "deployment/$PG_NAME" --timeout=300s
oc_target get pvc
```

Aguarde o PVC `Bound` e o Deployment pronto. Esse PostgreSQL de uma réplica é uma dependência didática; não é uma solução de banco altamente disponível ou de backup.

## 3. Instalar o Operator pelo OLM

Antes da criação, o administrador deve conferir os Operators existentes e evitar dois reconciliadores sobre o mesmo namespace. CRDs Keycloak são globais, mesmo com OperatorGroup restrito ao namespace. O campo `startingCSV` indica a versão inicial desejada; **aprovação Manual** também é necessária para controlar planos posteriores. Consulte a [instalação oficial do Operator RHBK 26.6](https://docs.redhat.com/en/documentation/red_hat_build_of_keycloak/26.6/html/operator_guide/installation-).

```sh
oc_target get operatorgroups.operators.coreos.com --all-namespaces
oc_target get subscriptions.operators.coreos.com --all-namespaces
oc_target create --dry-run=server -f .private/rhbk-install/operator.json
oc_target create -f .private/rhbk-install/operator.json
oc_target get subscription rhbk-operator \
  -o jsonpath='{.status.installPlanRef.name}{"\n"}'
```

Aguarde o nome do InstallPlan aparecer. Copie o nome real, leia o plano completo e confira o CSV esperado, catálogo, permissões e impacto nas CRDs compartilhadas. Nenhum comando do projeto aprova esse plano automaticamente:

```sh
PLAN=install-NOME_REAL
oc_target get installplan "$PLAN" -o yaml
```

Depois da revisão, aprove somente esse plano:

```sh
oc_target patch installplan "$PLAN" --type=merge \
  -p '{"spec":{"approved":true}}'
oc_target wait "clusterserviceversion/$OPERATOR_CSV" \
  --for=create --timeout=300s
oc_target wait "clusterserviceversion/$OPERATOR_CSV" \
  --for=jsonpath='{.status.phase}'=Succeeded --timeout=600s
oc_target rollout status deployment/rhbk-operator --timeout=300s
oc_target get subscription rhbk-operator \
  -o jsonpath='{.status.installedCSV}{"\n"}'
```

O CSV instalado deve ser o configurado. Na console, selecione o namespace e abra **Operators → Installed Operators**. Se o catálogo resolver outra versão ou houver erro no plano, investigue antes de seguir. A migração de um Operator manual requer uma transição específica; este roteiro é de instalação nova.

## 4. Construir a imagem otimizada

```sh
oc_target create --dry-run=server -f .private/rhbk-install/build.json
oc_target create -f .private/rhbk-install/build.json
```

O manifest cria um builder dedicado, permissões locais de publicação/pull, ImageStream e BuildConfig. Se a imagem base exigir credencial além do acesso já configurado no cluster, use um arquivo privado obtido por `podman login --authfile` ou pelo processo da organização. Não passe senha como argumento. Crie um Secret novo e associe ao builder:

```sh
REGISTRY_AUTH_FILE=/caminho/privado/registry-auth.json
oc_target create secret generic rhbk-build-pull \
  --type=kubernetes.io/dockerconfigjson \
  --from-file=.dockerconfigjson="$REGISTRY_AUTH_FILE"
oc_target secrets link "$BUILD_NAME-builder" rhbk-build-pull --for=pull
```

Se o builder já consegue obter a imagem, omita esse bloco. Inicie uma vez e guarde o nome retornado:

```sh
BUILD_REF=$(oc_target start-build "$BUILD_NAME" --from-dir=apps/rhbk -o name)
printf '%s\n' "$BUILD_REF"
oc_target logs -f "$BUILD_REF"
oc_target wait "$BUILD_REF" --for=jsonpath='{.status.phase}'=Complete --timeout=900s
oc_target get "$BUILD_REF" \
  -o jsonpath='{.status.phase}{"\n"}{.status.outputDockerImageReference}{"\n"}{.status.output.to.imageDigest}{"\n"}'
```

O diretório `apps/rhbk` contém somente o Dockerfile. Não envie a raiz do checkout ou `.private` ao build. Se perder a sessão, recupere o nome em `oc_target get builds` e acompanhe **o mesmo Build**, sem iniciar outro para tentar recuperar a resposta.

Somente depois de `Complete`, obtenha a referência imutável daquele Build:

```sh
BUILD_OUTPUT=$(oc_target get "$BUILD_REF" -o jsonpath='{.status.outputDockerImageReference}')
BUILD_DIGEST=$(oc_target get "$BUILD_REF" -o jsonpath='{.status.output.to.imageDigest}')
RHBK_IMAGE="${BUILD_OUTPUT%:*}@${BUILD_DIGEST}"
make rhbk-render-server PARAMS=.private/rhbk-install.env RHBK_IMAGE="$RHBK_IMAGE"
```

Revise `.private/rhbk-install/keycloak.json`: `spec.image` precisa conter o digest concluído e `startOptimized` precisa ser `true`. Usar o digest evita que uma mudança posterior na tag altere a imagem escolhida. O build e o servidor são etapas separadas; alterar uma feature de build exige reconstruir a imagem. Veja [imagens customizadas com o Operator](https://docs.redhat.com/en/documentation/red_hat_build_of_keycloak/26.6/html/operator_guide/customizing-keycloak-).

## 5. Criar e verificar o servidor

```sh
oc_target create --dry-run=server -f .private/rhbk-install/keycloak.json
oc_target create -f .private/rhbk-install/keycloak.json
oc_target wait "keycloak/$RHBK_NAME" --for=condition=Ready --timeout=600s
oc_target rollout status "statefulset/$RHBK_NAME" --timeout=600s
oc_target get keycloak "$RHBK_NAME" \
  -o jsonpath='{.spec.image}{"\n"}{.spec.startOptimized}{"\n"}'
oc_target get pods -o wide
oc_target get route "$RHBK_NAME"
```

Confirme que todas as réplicas estão prontas e usam a imagem construída. Para conferir versão, modo de início e configuração de build sem exibir configurações completas:

```sh
oc_target exec "$RHBK_NAME-0" -- /opt/keycloak/bin/kc.sh --version
oc_target get statefulset "$RHBK_NAME" \
  -o jsonpath='{.spec.template.spec.containers[?(@.name=="keycloak")].args}{"\n"}'
oc_target exec "$RHBK_NAME-0" -- sh -c \
  '/opt/keycloak/bin/kc.sh show-config | grep -E "kc\.(features|health-enabled|metrics-enabled) ="'
curl --fail --silent --show-error --cacert "$RHBK_CA_FILE" \
  "https://$RHBK_HOSTNAME/realms/master/.well-known/openid-configuration" \
  -o .private/rhbk-install/discovery.json
```

Exija a versão planejada, `start --optimized`, a feature `client-secret-rotation` e health/métricas habilitados. O último comando precisa terminar sem erro TLS ou HTTP; use a CA confiável do ambiente, nunca `-k`. Readiness comprova a prontidão do workload; a demonstração posterior comprovará autenticação e rotação com clients reais.

Acesse `https://RHBK_HOSTNAME/admin/` com a credencial temporária de bootstrap e conclua a administração inicial conforme o procedimento do produto. Depois siga [aplicação e automação de rotação](openshift.md), criando um namespace separado para a demonstração. Não execute os scripts antigos de bootstrap CRC sobre esta instalação.

No RHBK 26.6, `client-secret-rotation` continua [Technology Preview](https://docs.redhat.com/en/documentation/red_hat_build_of_keycloak/26.6/html/server_administration_guide/assembly-managing-clients_server_administration_guide). A portabilidade dos manifests não representa homologação de produção, HA, novas versões ou todas as topologias possíveis de OpenShift.
