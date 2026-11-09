# RHBK client secret rotation — laboratório CRC

Automação externa de rotação de credenciais M2M em **Red Hat build of Keycloak 26.6.9**, com Secrets imutáveis versionados e rollout explícito no OpenShift Local. O controlador decide quando rotacionar; o RHBK gera a credencial e aplica os prazos; a aplicação conserva sua geração até ser atualizada.

![Arquitetura do laboratório](docs/diagramas/arquitetura.png)

O núcleo da aplicação, controlador e testes integrados foi executado em CRC ARM64: três ciclos periódicos observaram pelo menos **770 novas autenticações, 770 sucessos e nenhuma falha** da aplicação principal. Consulte os [resultados e seus limites](docs/resultados.md), incluindo a revisão do teste de expiração e o comparativo PUT bloqueado.

**A instalação de um clone em CRC vazio foi preparada e validada por testes locais, mas ainda não foi executada integralmente em um segundo CRC.** Os comandos abaixo são o caminho de reprodução, não uma nova comprovação integrada. `client-secret-rotation` é Technology Preview; este laboratório de uma réplica RHBK não homologa produção ou alta disponibilidade.

## Instalação

Use um CRC com nó **Linux/ARM64**, com acesso administrativo dedicado, `oc`, Python 3.12 ou superior e `skopeo` ou Podman funcionando no host. O CRC deve estar iniciado, com registry interno gerenciado e APIs OLM disponíveis. É necessário um arquivo de autenticação autorizado para `registry.redhat.io`. As imagens oficiais ficam fixadas em [versions.lock.yaml](versions.lock.yaml); o pin corresponde à versão verificada em 09/11/2026, sem promessa de ser a última versão em consultas futuras.

No diretório do clone, substitua os caminhos e o nome do contexto:

```sh
python3 scripts/setup.py configure \
  --kubeconfig /caminho/kubeconfig-crc \
  --context NOME_DO_CONTEXTO_CRC
make preflight
export REGISTRY_AUTH_FILE=/caminho/pull-secret.json
make setup
make mirror
make deploy
make build
make bootstrap
make baseline
```

`configure` grava somente o contexto explicitamente escolhido em `.private/crc.kubeconfig`, com permissão 0600. Os scripts recusam outra API além de `https://api.crc.testing:6443`, verificam o UID local e não usam o contexto global como alternativa. Um namespace já existente sem o registro de propriedade deste checkout é recusado.

`setup` obtém o bundle oficial, cria somente CRDs ausentes e verifica as existentes sem substituí-las. Prepara o namespace exclusivo, a Route do registry e a CA. `mirror` copia quatro imagens fixadas; `deploy` aguarda Operator, banco, RHBK e Route; `build` produz e fixa as imagens da aplicação e do controlador. A credencial inicial é criada somente no bootstrap, depois dessas etapas.

Leia o [guia de operação](docs/operacao.md) para requisitos, observação, demonstração, recuperação e limpeza. O setup não instala ou redimensiona o CRC, não desliga outras aplicações e não cria infraestrutura de nuvem.

## Executar e observar

O perfil de laboratório usa TTL principal de **300 s**, anterior de **120 s**, espera de **45 s** antes do rollout e elegibilidade externa a partir de **180 s**. O CronJob avalia a cada minuto; uma avaliação pode decidir aguardar.

```sh
# Observação do cluster; collect grava evidência local sanitizada.
make watch
make collect

# Altera estado: três ciclos agendados (~13 min), com suspensão ao finalizar.
make test-periodic
```

O ensaio exige uma geração válida, estado terminal e nenhum Job ativo. A aplicação faz um novo grant a cada 2 s por pod; a sonda da credencial anterior é contabilizada separadamente. Lease, estado persistido e verificação de linhagem permitem reconciliação sem repetir cegamente o POST.

Com o CronJob suspenso, o TTL continua correndo. Para uma geração comprovadamente vencida e estado terminal, `make recover-expired` executa uma recuperação administrativa explícita, registra a indisponibilidade anterior e deixa o agendamento suspenso. Ciclos pendentes exigem `make reconcile`; resultado ambíguo exige investigação. Consulte o guia antes dessas mutações.

## Verificação e escopo

```sh
make test                         # testes locais, sem cluster
make test-post-expiration         # ensaio temporizado; altera clients dedicados
make scan                         # compara artefatos com valores reais do lab; exige cluster
python3 scripts/cleanup.py --dry-run
make cleanup                      # exclui o namespace, seu PVC e RBAC global dedicado
```

A limpeza preserva CRDs oficiais e a configuração compartilhada da Route do registry. `.private/` e `evidence/` são locais e ignorados pelo Git. Este repositório contém uma seleção de código, testes, manifests e documentação; não inclui credenciais nem os relatórios operacionais brutos da execução original.

- [Operação e recuperação](docs/operacao.md)
- [Resultados e limitações](docs/resultados.md)
- [Sequência da rotação](docs/diagramas/sequencia.png)
- [Diagramas editáveis](docs/diagramas/arquitetura-rotacao-externa.drawio)
