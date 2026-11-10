# OpenShift com RHBK existente

Este caminho instala a aplicação de demonstração e o controlador externo em um namespace novo. O RHBK, seu Operator, banco, ingress e registry já devem estar disponíveis. O instalador não altera esses componentes nem o realm: importa um client preparado pelo operador e confirma seu principal antes de distribuir a primeira geração.

A adaptação foi verificada por testes unitários com APIs simuladas. A instalação em outro OpenShift ainda não foi executada. Os resultados do CRC em [resultados.md](resultados.md) não constituem execução deste caminho.

## Contrato e pré-requisitos

- RHBK 26.x com `client-secret-rotation` habilitado pelo responsável pelo servidor. Informe a build completa, por exemplo `26.6.9.redhat-00002`. O verificador compara `/admin/serverinfo` com esse valor. Somente a build 26.6.9 foi exercitada no laboratório CRC; outras versões ficam como não executadas e precisam de homologação. O recurso é Technology Preview na linha 26.6.
- Um realm dedicado ao ensaio, um client M2M dedicado e uma identidade administrativa independente. Não importar um client já distribuído para aplicações existentes.
- Python 3.12 ou superior, `oc`, um kubeconfig administrativo explicitamente escolhido e seu contexto. São necessárias leituras do namespace `kube-system` e dos nós, criação do namespace do ensaio e gestão dos recursos locais desse namespace. Nenhum ClusterRole/SCC é criado pelo fluxo.
- Dois containers construídos a partir deste checkout: `apps/m2m-probe` e `apps/rotation-controller`. Forneça referências completas por digest, acessíveis pelo cluster. `architecture` admite `amd64` ou `arm64`; use imagens que suportem a arquitetura escolhida, inclusive as imagens base dos Containerfiles. O instalador verifica a existência de nós compatíveis e adiciona `nodeSelector`; não inspeciona os binários da imagem.
- Certificado/CA real que valida a URL HTTPS do RHBK. Não usar `curl -k`, trust-all ou certificado obtido de conexão não autenticada. O kubeconfig também deve validar o certificado do API server.
- Relógios do host, OpenShift e RHBK sincronizados. A tolerância padrão é de cinco segundos; este instalador não executa comandos dentro do RHBK para medir seu relógio.
- Um controlador e a aplicação demo por namespace. A aplicação tem dois pods, container `probe`, label `app.kubernetes.io/name=m2m-probe`, endpoint `/status` na porta 8080 e um Secret imutável com `client-id`, `client-secret` e `generation`. Este contrato não representa um adaptador automático para qualquer workload.

## Imagens da aplicação

Os Containerfiles exigem `PYTHON_IMAGE`, sem valor padrão. Escolha uma imagem Python por digest compatível com a arquitetura de destino. Se construir localmente, defina `PYTHON_IMAGE`, `PROBE_IMAGE_TAG` e `CONTROLLER_IMAGE_TAG` com referências reais de seu registry e use, por exemplo para AMD64:

```bash
podman build --platform linux/amd64 --build-arg PYTHON_IMAGE="$PYTHON_IMAGE" -t "$PROBE_IMAGE_TAG" apps/m2m-probe
podman build --platform linux/amd64 --build-arg PYTHON_IMAGE="$PYTHON_IMAGE" -t "$CONTROLLER_IMAGE_TAG" apps/rotation-controller
```

Para ARM64, use `--platform linux/arm64` e uma base correspondente. Publique as imagens no registry autorizado de seu ambiente pelo procedimento habitual e coloque no JSON os **digests publicados**, não as tags locais. O código não escolhe um digest ARM64 para instalações genéricas. Também é possível consumir imagens desse mesmo código que sua organização já tenha construído.

## Preparar o client no RHBK

Faça esta etapa com a identidade administrativa que já gerencia o servidor, na Admin Console, em um realm dedicado. O instalador não precisa armazenar essa credencial privilegiada.

1. Crie o client `rotation-demo-m2m`: protocolo OpenID Connect, habilitado, autenticação do client ligada, método **Client ID and Secret**, service account habilitada, fluxos interativos e direct access grants desabilitados. Mantenha as permissões do serviço limitadas ao que será demonstrado.
2. Na aba **Roles do próprio client**, crie `external-rotation-managed`. Não se trata de atribuir esse papel à service account: a condição seleciona o client que **define** o papel.
3. Em **Realm settings → Client policies → Profiles**, crie `external-rotation-profile`, com apenas o executor `secret-rotation`. Para acompanhar o arquivo de exemplo, use `Secret expiration=315360000`, `Rotated secret expiration=120` e `Remaining expiration time for rotation during update=0`. Esses valores são configuráveis; o perfil e o JSON precisam coincidir.
4. Em **Policies**, crie `external-rotation-policy`, habilitada, com uma condição `client-roles`, `roles=["external-rotation-managed"]`, sem lógica negativa. Associe somente `external-rotation-profile`. Crie entradas individuais: não envie um PUT com uma coleção contendo apenas essas entradas sobre uma coleção existente. Outros perfis e políticas devem ser preservados. O importador recusa uma segunda política de rotação habilitada no mesmo realm, pois não consegue comprovar sua ausência de interferência.
5. Crie `rotation-admin` como client confidencial com service account. Não crie nele o papel marcador. Habilite apenas o fluxo de service account. Configure `Full Scope Allowed=false`; atribua à service account os papéis `realm-management/manage-clients`, `view-clients`, `query-clients` e `view-realm`, e permita esses mesmos papéis no role scope do dedicated client scope. Para as verificações de instalação (`configure` e `apply`), acrescente também `realm-management/view-system` na service account e no role scope. Esse papel adicional permite ler a build do servidor; pode ser removido de ambos após a instalação, pois o controlador não consulta `/admin/serverinfo`. Uma reexecução do importador voltará a exigir essa leitura. O instalador não concede papéis automaticamente. São permissões administrativas sobre clients do realm; esse é um motivo para usar um realm dedicado.
6. Se a build exigir o client scope `kc-admin-api` para a Admin REST API, adicione-o como default client scope **somente a `rotation-admin`**. Isso foi necessário na build 26.6.9 usada no CRC. A verificação testa as operações administrativas reais; HTTP 403/401 é bloqueio de permissão, não expiração.
7. No client M2M dedicado, **depois de ativar a política**, use **Credentials → Regenerate** uma única vez. Essa preparação deve acontecer antes de qualquer consumidor usar o client. Equivale ao POST `/admin/realms/{realm}/clients/{uuid}/client-secret`; o RHBK gera o valor e estabelece os timestamps. Alterar apenas o perfil não atualiza o prazo de um principal existente.
8. Guarde o novo principal e o secret de `rotation-admin` em arquivos privados locais. Aguarde a convivência de 120 s mais a tolerância antes da importação. O importador recusa um anterior ainda válido. Se a resposta da geração se perder, leia o principal atual em Credentials/GET autenticado: **não repita Regenerate para tentar recuperar a resposta**.

Use os nomes escolhidos nos campos correspondentes do arquivo de configuração. A API usa UUID no caminho de client; o instalador resolve o UUID por `client_id` e exige um resultado exato. Ele verifica os perfis, a condição, o papel no client, a exclusão do administrador, a igualdade entre o principal retornado pelo servidor e o arquivo importado, os timestamps de criação/expiração e uma autenticação M2M com HTTP 200. Não faz POST `/client-secret`, PUT de client ou edição de atributos internos.

A documentação do produto descreve [client secret rotation, sua política e service accounts](https://docs.redhat.com/en/documentation/red_hat_build_of_keycloak/26.6/html/server_administration_guide/assembly-managing-clients_server_administration_guide). `remaining=0` elimina a janela antecipada para atualizações dinâmicas; o POST administrativo continua podendo forçar uma rotação.

A necessidade de `view-system` foi identificada por análise do [endpoint upstream 26.6.0](https://github.com/keycloak/keycloak/blob/26.6.0/services/src/main/java/org/keycloak/services/resources/admin/info/ServerInfoAdminResource.java#L125-L140): uma identidade do próprio realm pode receber HTTP 200 sem `systemInfo.version`. Isso não comprova equivalência binária com cada build RHBK. Neste caminho genérico ainda não executado, a resposta real é a autoridade: ausência da versão, papel indisponível ou API incompatível bloqueiam a importação. Não ampliar para `manage-realm` ou administrador do master para contornar esse bloqueio.

## Arquivos de entrada e revisão

Crie os arquivos sensíveis com permissão 0600. Um modo de digitar/colar valores sem colocá-los no histórico, argumentos ou saída do terminal é:

```bash
umask 077
mkdir -p .private
python3 - <<'PY'
from getpass import getpass
from pathlib import Path
for name in ('admin-input.secret', 'initial-input.secret'):
    path = Path('.private') / name
    value = getpass('Valor para ' + name + ': ')
    path.write_text(value)
    path.chmod(0o600)
PY
cp config/openshift.example.json .private/openshift-input.json
```

Edite `.private/openshift-input.json` com os caminhos absolutos do kubeconfig, CA e dois arquivos de secret, o contexto, URL esperada do API server, namespace **ainda inexistente**, URL/realm/clients e digests reais das imagens. O exemplo contém marcadores deliberadamente inválidos no lugar de digests. Não os substitua por tags. Também confira os nomes de perfil, política e papel.

O arquivo de exemplo usa `principal_ttl=315360000` (3650 dias), rotação externa elegível após 300 s e convivência de 120 s, reproduzindo os parâmetros da demonstração CRC. A instalação genérica continua não executada. A demonstração observa a rotação e o fim da convivência; não espera o vencimento do principal. Os 45 s de espera, 60 s de rollout e 15 s de margem precisam caber nessa convivência. O Cron consulta a cada minuto; 300 s é o limiar, não uma promessa de disparo com precisão de um segundo. A primeira elegibilidade conta desde a criação real do principal, não desde a instalação. Se omitir o campo `principal_ttl`, o código usa 86400 s (um dia); por isso mantenha os parâmetros explícitos no JSON e no perfil.

Um TTL como 315360000 s continua finito e deve ser explicitamente configurado tanto no perfil RHBK quanto no JSON. Confira a expiração efetiva antes de usá-lo: versões upstream antigas usam timestamps de 32 bits, e a correção posterior não comprova automaticamente o comportamento de cada build RHBK. Veja [a correção upstream](https://github.com/keycloak/keycloak/commit/fc078e7ce76ca9bc5687b8576f30f8806a52dee0). O importador exige TTL armazenado compatível; não espera anos para provar o vencimento.

```bash
python3 scripts/openshift.py configure --config .private/openshift-input.json
python3 scripts/openshift.py render
```

`configure` faz apenas leituras remotas e solicitações de token. Exporta exclusivamente o contexto escolhido para `.private/openshift.kubeconfig`, copia CA/credenciais privadas e registra URL esperada, UID do cluster e um identificador de instalação. Não usa o contexto ambiente. Uma configuração já registrada não é sobrescrita silenciosamente; preserve o diretório privado como parte do estado operacional desse checkout.

Revise `evidence/openshift/resources.json`: contém os manifests de workloads/RBAC, digests, namespace e nomes configurados, sem valores de credenciais. Os Secrets, o ConfigMap de CA e o estado inicial serão criados pelo comando de implantação. Se houver GitOps gerenciando esse namespace, pare e ajuste a fonte de verdade antes de instalar recursos concorrentes.

## Implantar e operar

O comando abaixo é a ação explícita de implantação. Cria o namespace por `create`, registra seu UID e cria somente os recursos desse namespace. Não instala Operator, banco, RHBK, Route ou recursos de nuvem.

```bash
python3 scripts/openshift.py apply
```

As evidências desse caminho ficam em `evidence/openshift/`, separadas das evidências CRC.

A geração `v0001` contém o principal importado, com três chaves no Secret imutável; `rotation-state` registra o mesmo client, prefixo e geração. O Cron inicia suspenso. `APPLIED` significa que os recursos foram criados; ainda não significa que os pods estão prontos. Retries de instalação conferem os campos gerenciados dos recursos existentes da mesma instalação; divergências bloqueiam a operação. Uma geração em operação nunca é reinicializada. Se o resultado da criação do namespace se perder, a intenção persistida e o marcador exclusivo permitem recuperar seu UID por leitura; um namespace sem essa intenção não é adotado.

Se o registry exigir autenticação, os nomes em `image_pull_secrets` devem existir nesse namespace. Após sua criação pelo instalador, um operador autorizado pode provisionar o Secret de pull a partir de arquivo privado (`oc create secret generic NOME --type=kubernetes.io/dockerconfigjson --from-file=.dockerconfigjson=ARQUIVO`, usando explicitamente o kubeconfig/contexto/namespace configurados). Não passe a senha do registry em argumento. Os pods podem ficar aguardando esse Secret até a configuração do acesso; conclua isso antes do baseline. O instalador não publica imagens nem concede permissões de pull em outros projetos.

```bash
python3 scripts/openshift.py check
python3 scripts/observe.py baseline --environment openshift
python3 scripts/observe.py status --environment openshift
python3 scripts/control.py rotate-once --environment openshift
python3 scripts/observe.py collect --environment openshift
```

O baseline precisa confirmar os dois pods e autenticações novas. O ciclo manual deve registrar sucesso do principal anterior, um único POST de geração, distribuição por Secret novo, espera com o anterior, rollout controlado, rejeição do anterior após a convivência e autenticação do principal novo. Compare os metadados do estado com o perfil; não use apenas o código OAuth como prova de vencimento. Credenciais e tokens não devem ser colados em issues ou relatórios.

```bash
python3 scripts/control.py resume --environment openshift
python3 scripts/observe.py watch --environment openshift --output auto
```

Para parar, finalize o observador e suspenda a automação em outro comando:

```bash
python3 scripts/control.py suspend --environment openshift
python3 scripts/observe.py status --environment openshift
python3 scripts/observe.py history --environment openshift
```

Confirme a suspensão e a conclusão de eventual ciclo ativo. Se o controlador reportar resultado incerto após POST, use `reconcile` para a mesma geração; não dispare outra rotação. A aplicação principal e as sondas do secret antigo devem ser observadas separadamente: falhas esperadas nas sondas antigas não são falhas dos pods principais.

Os testes `tests/integration/post_expiration.py` e `periodic.py` originais continuam vinculados ao laboratório CRC. Não os aponte para outro cluster e não execute a suíte que aguarda expiração do principal com TTL de anos. Para este caminho, a sequência de baseline, rotação manual, collect e observação periódica é a validação operacional disponível. Não existe limpeza genérica automática: primeiro suspenda, confirme os UIDs/recursos do ensaio e siga o procedimento de remoção aprovado pelo responsável pelo cluster e pelo realm.
