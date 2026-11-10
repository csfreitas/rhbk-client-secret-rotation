# Rotação externa de client secrets no OpenShift

Base de demonstração para **Red Hat build of Keycloak**, com aplicação M2M contínua, Secrets imutáveis versionados e rollout controlado. A automação externa decide quando trocar; o RHBK gera a nova credencial; o OpenShift distribui e atualiza os consumidores. O `client_id` permanece igual. Não há SPI.

![Arquitetura da solução](docs/diagramas/arquitetura.png)

## O que foi validado

No CRC ARM64, com **RHBK 26.6.9.redhat-00002**, validamos dois perfis:

| Perfil | Principal | Anterior após POST | Decisão externa |
|---|---:|---:|---|
| Demonstração atual | 315360000 s — 3650 dias | 120 s | Intervalo mínimo de 300 s; avaliação a cada minuto |
| Ensaios de expiração | 300 s | 120 s | Prazos curtos para observar vencimentos reais |

O POST reiniciou a validade da nova secret. A antiga, que antes venceria em 2036, passou a vencer 120 segundos após a troca. A aplicação autenticou com a nova credencial depois da rejeição da antiga. Nos dois ciclos acompanhados em 10/11/2026, o intervalo observado entre POSTs foi **359,705 s, aproximadamente 6 minutos**. Não é um agendamento de 5 minutos exatos. Veja os [resultados e limites](docs/resultados.md).

Os 3650 dias são um prazo finito de demonstração, não uma recomendação universal de segurança. A periodicidade externa pode ser muito menor; suspender a automação não renova nem elimina o vencimento da secret atual.

## Escolha o ambiente

| Caminho | Requisitos e escopo | Validação |
|---|---|---|
| [OpenShift com RHBK existente](docs/openshift.md) | API/contexto explícitos, namespace dedicado, URL/CA, realm/client já provisionados e imagens compatíveis com os nós. Instala somente a automação e a aplicação de demonstração. | Configuração/renderização e proteções testadas localmente; implantação em outro cluster **não executada**. |
| [Exemplo completo no CRC](docs/operacao.md) | CRC Linux/ARM64; prepara Operator, PostgreSQL, RHBK e aplicações com versões fixadas. | Runtime e ensaios executados; instalação integral de um segundo CRC vazio **não executada**. |

O núcleo não exige hostname `crc.testing`, nó `crc`, ARM64 ou o `client_id` original. Esses valores ficam no exemplo CRC. No caminho OpenShift, contexto, API e UIDs são conferidos antes de operações; não há uso implícito do contexto global do `oc`.

O contrato atual é **uma aplicação M2M demonstrativa e um client por namespace**. Os nomes de recursos, realm, client, prefixo dos Secrets, imagens e prazos são configuráveis. Adaptar a distribuição para uma aplicação existente exige que ela carregue a nova credencial no rollout; esta base não altera Deployments arbitrários nem provisiona a infraestrutura do RHBK.

## Usar a demonstração já instalada no CRC

```sh
make preflight
make watch               # tabela legível; grava observações JSONL em evidence/
make automation-start    # altera estado: habilita a decisão periódica
make collect             # coleta local, sem rotacionar
```

No caminho OpenShift, use os comandos correspondentes `make openshift-watch`, `make openshift-baseline`, `make openshift-start`, `make openshift-stop` e `make openshift-collect`, após concluir seu [guia de configuração](docs/openshift.md).

O [roteiro de apresentação](docs/apresentacao.md) traz os comandos e o que explicar ao cliente. A tabela mostra os dois pods principais; as rejeições das sondas antigas não são somadas às falhas da aplicação. Ctrl+C para apenas a observação.

```sh
make automation-stop     # suspende novas rotações; um Job iniciado precisa terminar
make collect
```

## Configuração e verificações

- [Arquitetura e limites de portabilidade](docs/arquitetura.md).
- [Perfil longo](realm/profile.json): principal de 3650 dias, convivência de 120 s e janela de antecipação dinâmica igual a zero.
- [Perfil curto](realm/profile-short.json): ensaios de expiração principal, separados da apresentação.
- [Configuração OpenShift](config/openshift.example.json): exemplo sem credenciais; preencha os valores do ambiente.
- [Versões do exemplo CRC](versions.lock.yaml): verificadas em 09/11/2026, não são a seleção automática da versão mais recente.

```sh
make test                 # testes locais, sem cluster
make watch-json           # saída técnica; útil para integrações e análise
```

`make test-post-expiration` é um ensaio **CRC** que altera clients descartáveis e exige o perfil curto; o teste recusa o perfil longo para não aguardar anos. `make test-periodic` também é do CRC e correlaciona Jobs do Cron com pelo menos três ciclos. Não execute esses ensaios durante uma apresentação ou contra uma instalação OpenShift genérica.

Em RHBK 26.6, `client-secret-rotation` permanece [Technology Preview](https://docs.redhat.com/en/documentation/red_hat_build_of_keycloak/26.6/html/server_administration_guide/assembly-managing-clients_server_administration_guide). No [Keycloak comunitário 26.8](https://www.keycloak.org/2026/10/keycloak-2680-released), foi promovido a supported; isso não transfere suporte ao produto Red Hat. Os resultados deste laboratório não homologam produção, HA ou outras versões.

Arquivos privados, kubeconfigs e evidências brutas ficam em `.private/` e `evidence/`, ignorados pelo Git. O repositório contém código, configurações de exemplo, diagramas e sínteses dos resultados. Atualizar o checkout não altera imagens nem recursos em execução.
