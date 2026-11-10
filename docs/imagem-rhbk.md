# Imagem RHBK otimizada e Operator via OLM

O [Dockerfile](../apps/rhbk/Dockerfile) prepara `client-secret-rotation`, PostgreSQL, health e métricas com `kc.sh build`. O [BuildConfig](../deploy/build/rhbk-buildconfig.json) executa esse build dentro do OpenShift. O resultado é usado por digest no `Keycloak` CR, com `startOptimized: true`; o Operator instalado pelo OLM reconcilia o servidor. Nenhum secret de client, senha de banco ou certificado entra na imagem.

**Build, migração OLM e execução otimizada foram verificados no CRC existente em 10/11/2026**, com RHBK `26.6.9.redhat-00002` e Operator `26.6.9-opr.1`. O servidor iniciou com `start --optimized`, sem nova augmentation, e a feature, health e métricas ficaram persistidas na imagem. A migração teve interrupção; o baseline e a rotação posterior foram aprovados, conforme [resultados](resultados.md). Uma instalação integral em CRC vazio e o exemplo em outro OpenShift abaixo continuam `NOT_RUN`. O helper `scripts/rhbk_image.py` permanece restrito ao CRC/ARM64; nesse ambiente, use a sequência de [operação](operacao.md).

## Build em outro OpenShift

Requer namespace dedicado já autorizado, Build API, registry interno, permissão de build e imagem base RHBK compatível com a arquitetura dos nós. Selecione uma versão compatível com o Operator e um digest oficial verificado. Substitua **todos** os marcadores:

```sh
RHBK_KUBECONFIG=/caminho/privado/kubeconfig
RHBK_CONTEXT=CONTEXTO_EXPLICITO
RHBK_NAMESPACE=NAMESPACE_DEDICADO
export RHBK_BASE_IMAGE=registry.redhat.io/rhbk/keycloak-rhel9@sha256:DIGEST_VERIFICADO
RHBK_AUTH_FILE=/caminho/privado/registry-auth.json

oc --kubeconfig "$RHBK_KUBECONFIG" --context "$RHBK_CONTEXT" \
  config view --minify -o jsonpath='{.clusters[0].cluster.server}{"\n"}'
oc --kubeconfig "$RHBK_KUBECONFIG" --context "$RHBK_CONTEXT" \
  -n "$RHBK_NAMESPACE" get namespace "$RHBK_NAMESPACE"
```

Confira API e namespace antes de escrever recursos. Mantenha a validação TLS. Se o builder já possui acesso ao registry, preserve sua configuração. Caso precise de autenticação adicional, obtenha o arquivo com `podman login --authfile "$RHBK_AUTH_FILE" registry.redhat.io` ou equivalente, por prompt; não passe senha em argumento. O bloco seguinte cria um Secret novo e o associa apenas ao builder:

```sh
oc --kubeconfig "$RHBK_KUBECONFIG" --context "$RHBK_CONTEXT" -n "$RHBK_NAMESPACE" \
  create secret generic rhbk-build-pull --type=kubernetes.io/dockerconfigjson \
  --from-file=.dockerconfigjson="$RHBK_AUTH_FILE"
oc --kubeconfig "$RHBK_KUBECONFIG" --context "$RHBK_CONTEXT" -n "$RHBK_NAMESPACE" \
  secrets link builder rhbk-build-pull --for=pull
```

Na raiz do checkout, prepare o BuildConfig local. O template não fixa namespace nem digest para outro cluster:

```sh
umask 077
mkdir -p .private
python3 - <<'PY'
import json, os, re
from pathlib import Path
base = os.environ['RHBK_BASE_IMAGE']
assert re.fullmatch(r'registry\.redhat\.io/rhbk/keycloak-rhel9@sha256:[0-9a-f]{64}', base), 'Informe um digest oficial real'
obj = json.loads(Path('deploy/build/rhbk-buildconfig.json').read_text())
obj['spec']['strategy']['dockerStrategy']['buildArgs'] = [{'name': 'RHBK_IMAGE', 'value': base}]
Path('.private/rhbk-buildconfig.json').write_text(json.dumps(obj, indent=2) + '\n')
PY
oc --kubeconfig "$RHBK_KUBECONFIG" --context "$RHBK_CONTEXT" -n "$RHBK_NAMESPACE" \
  create imagestream rhbk-optimized
oc --kubeconfig "$RHBK_KUBECONFIG" --context "$RHBK_CONTEXT" -n "$RHBK_NAMESPACE" \
  create -f .private/rhbk-buildconfig.json
oc --kubeconfig "$RHBK_KUBECONFIG" --context "$RHBK_CONTEXT" -n "$RHBK_NAMESPACE" \
  start-build rhbk-optimized --from-dir=apps/rhbk --follow --wait
```

`create` não adota recursos existentes. Se um nome já existir, revise seu proprietário e configuração antes de prosseguir. O contexto enviado contém somente o Dockerfile; não envie a raiz do repositório ou `.private/` ao build. Em cluster com arquiteturas mistas, acrescente ao BuildConfig `spec.nodeSelector.kubernetes.io/arch` com a arquitetura escolhida e assegure compatibilidade dos nós RHBK.

Confira o **Build exato** iniciado, substituindo `rhbk-optimized-N` pelo seu nome:

```sh
oc --kubeconfig "$RHBK_KUBECONFIG" --context "$RHBK_CONTEXT" -n "$RHBK_NAMESPACE" \
  get build rhbk-optimized-N \
  -o jsonpath='{.status.phase}{"\n"}{.status.outputDockerImageReference}{"\n"}{.status.output.to.imageDigest}{"\n"}'
```

Exija `Complete` e digest `sha256` preenchido. Forme a referência do mesmo repositório publicado substituindo a tag `:lab` por `@sha256:DIGEST_DO_BUILD`. Não use um digest de build anterior nem apenas a tag mutável. Se outro namespace consumir a imagem, o administrador precisa autorizar seu pull.

## Operator e CR

Na console, em **Operators → OperatorHub**, selecione o **Red Hat build of Keycloak Operator** do catálogo Red Hat. Escolha a versão/canal compatíveis, o namespace dedicado e aprovação **Manual**. Revise o InstallPlan, inclusive CRDs e escopo do OperatorGroup, antes de aprovar; prossiga somente com o CSV `Succeeded` em **Installed Operators**. CRDs são globais: uma instalação nova não deve alterar versões compartilhadas sem revisão. Para migrar um Operator manual, use um procedimento de transição específico; não inicie dois reconciliadores para o mesmo CR.

Na configuração completa do seu `Keycloak` CR, use estes campos; o trecho não é um manifest completo:

```yaml
spec:
  image: image-registry.openshift-image-registry.svc:5000/NAMESPACE_DEDICADO/rhbk-optimized@sha256:DIGEST_DO_BUILD
  startOptimized: true
```

Configure conexão de banco, referências a Secrets, hostname, TLS/HTTP e proxy no CR conforme sua topologia. As opções de build já estão na imagem; alterar uma delas exige novo build e novo digest. Não coloque senhas em `additionalOptions` ou no Dockerfile. Verifique readiness, versão efetiva, feature habilitada e autenticação antes de instalar a [demonstração de rotação](openshift.md).

Otimizar a imagem não altera o status Technology Preview de `client-secret-rotation` no RHBK 26.6. Referência oficial: [RHBK 26.6 — configuração e imagens otimizadas](https://docs.redhat.com/en/documentation/red_hat_build_of_keycloak/26.6/pdf/server_configuration_guide/Red_Hat_build_of_Keycloak-26.6-Server_Configuration_Guide-en-US.pdf).
