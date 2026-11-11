#!/usr/bin/env bash
# Observe the existing controller. Never rotate, update or delete anything.
# Bash 3.2: no associative arrays, eval, sourced configuration or Python.
set +x
set +v
set -eo pipefail
umask 077

usage() {
    cat <<'HELP'
Uso: bash scripts/watch-secrets.sh --config ARQUIVO [--once | --duration SEGUNDOS] [--plain]
Observa Secrets versionados e executa grants novos de client_credentials.
Somente GET no Kubernetes e POST no endpoint /token; nenhuma rotação administrativa.
Configuração literal KEY=value, sem aspas nem expansão de variáveis.
Precedência: padrões < ambiente < arquivo explícito < argumentos.
--once coleta uma rodada; --duration 0 continua até Ctrl+C; --plain não redesenha a tela.
HELP
}
fail() { printf 'BLOQUEADO: %s\n' "$1" >&2; exit 1; }
WORK=''; REPORT_OPEN=0
cleanup() { if [[ -n "$WORK" && -d "$WORK" ]]; then rm -rf -- "$WORK"; fi; }
trap cleanup EXIT
trap 'printf "\nObservação interrompida.\n" >&2; exit 130' INT
trap 'exit 143' TERM

CONFIG=''; ONCE=0; PLAIN=0; CLI_DURATION=''
while [[ $# -gt 0 ]]; do
    case "$1" in
        --config) [[ $# -ge 2 ]] || fail '--config exige um arquivo'; CONFIG=$2; shift 2 ;;
        --duration) [[ $# -ge 2 ]] || fail '--duration exige segundos'; CLI_DURATION=$2; shift 2 ;;
        --once) ONCE=1; shift ;;
        --plain) PLAIN=1; shift ;;
        --help|-h) usage; exit 0 ;;
        *) fail 'argumento desconhecido; consulte --help' ;;
    esac
done
[[ -n "$CONFIG" && -f "$CONFIG" && -r "$CONFIG" ]] || fail 'informe --config com um arquivo legível'
[[ "$ONCE" -eq 0 || -z "$CLI_DURATION" ]] || fail '--once e --duration são alternativas'
for dependency in oc curl jq date mktemp chmod cmp; do
    command -v "$dependency" >/dev/null 2>&1 || fail "dependência ausente: $dependency"
done

# Defaults use exported environment values; explicit file entries override them.
KUBECONFIG=${KUBECONFIG-}; CONTEXT=${CONTEXT-}; EXPECTED_SERVER=${EXPECTED_SERVER-}
NAMESPACE=${NAMESPACE-}; STATE_CONFIGMAP=${STATE_CONFIGMAP:-rotation-state}
RHBK_URL=${RHBK_URL-}; REALM=${REALM-}; CLIENT_ID=${CLIENT_ID-}; CA_FILE=${CA_FILE-}
INTERVAL_SECONDS=${INTERVAL_SECONDS:-2}; DURATION_SECONDS=${DURATION_SECONDS:-0}
REPORT_FILE=${REPORT_FILE-}; WATCH_TZ=${WATCH_TZ:-America/Sao_Paulo}
SEEN_KEYS=' '
while IFS= read -r line || [[ -n "$line" ]]; do
    line=${line%$'\r'}
    [[ "$line" =~ ^[[:space:]]*$ || "$line" =~ ^[[:space:]]*# ]] && continue
    [[ "$line" =~ ^([A-Z][A-Z0-9_]*)=(.*)$ ]] || fail 'configuração exige KEY=value literal, sem espaços antes da chave'
    key=${BASH_REMATCH[1]}; value=${BASH_REMATCH[2]}
    case "$key" in
        KUBECONFIG|CONTEXT|EXPECTED_SERVER|NAMESPACE|STATE_CONFIGMAP|RHBK_URL|REALM|CLIENT_ID|CA_FILE|INTERVAL_SECONDS|DURATION_SECONDS|REPORT_FILE|WATCH_TZ) ;;
        *) fail 'chave desconhecida no arquivo de configuração' ;;
    esac
    [[ "$SEEN_KEYS" != *" $key "* ]] || fail 'chave repetida no arquivo de configuração'
    SEEN_KEYS="$SEEN_KEYS$key "
    printf -v "$key" '%s' "$value"
done < "$CONFIG"
[[ -z "$CLI_DURATION" ]] || DURATION_SECONDS=$CLI_DURATION
[[ -n "$KUBECONFIG" && -f "$KUBECONFIG" && -r "$KUBECONFIG" ]] || fail 'KUBECONFIG explícito precisa ser um arquivo legível'
[[ -n "$CONTEXT" && -n "$EXPECTED_SERVER" && -n "$RHBK_URL" && -n "$REALM" && -n "$CLIENT_ID" ]] || fail 'faltam contexto, servidor esperado ou identidade RHBK'
[[ "$NAMESPACE" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ && ${#NAMESPACE} -le 63 ]] || fail 'NAMESPACE inválido'
[[ "$STATE_CONFIGMAP" =~ ^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$ && ${#STATE_CONFIGMAP} -le 253 ]] || fail 'STATE_CONFIGMAP inválido'
[[ "$INTERVAL_SECONDS" =~ ^[0-9]+$ && ${#INTERVAL_SECONDS} -le 5 ]] || fail 'INTERVAL_SECONDS deve ser inteiro positivo'
[[ "$DURATION_SECONDS" =~ ^[0-9]+$ && ${#DURATION_SECONDS} -le 8 ]] || fail 'DURATION_SECONDS deve ser inteiro não negativo'
INTERVAL_SECONDS=$((10#$INTERVAL_SECONDS)); DURATION_SECONDS=$((10#$DURATION_SECONDS))
[[ "$INTERVAL_SECONDS" -ge 1 && "$INTERVAL_SECONDS" -le 86400 ]] || fail 'intervalo permitido: 1 a 86400 segundos'
[[ -z "$CA_FILE" || ( -f "$CA_FILE" && -r "$CA_FILE" ) ]] || fail 'CA_FILE não é um arquivo legível'
for url in "$EXPECTED_SERVER" "$RHBK_URL"; do
    [[ "$url" =~ ^https://[^/?#@[:space:]]+(/[^?#[:space:]]*)?$ ]] || fail 'URLs precisam usar HTTPS, sem usuário, query ou fragmento'
done
for identifier in "$CONTEXT" "$REALM" "$CLIENT_ID" "$WATCH_TZ"; do
    jq -en --arg v "$identifier" '$v|length>0 and length<=255 and (test("[\\x00-\\x1f\\x7f]")|not) and (test("^\\s|\\s$")|not)' >/dev/null || fail 'identificador contém espaços nas extremidades ou controles'
done
export TZ="$WATCH_TZ"
WORK=$(mktemp -d "${TMPDIR:-/tmp}/rhbk-secret-watch.XXXXXX") || fail 'não foi possível criar diretório temporário privado'
chmod 700 "$WORK"
printf '%s' "$CLIENT_ID" > "$WORK/client-id"
REALM_PATH=$(jq -rn --arg realm "$REALM" '$realm|@uri')
TOKEN_URL="${RHBK_URL%/}/realms/$REALM_PATH/protocol/openid-connect/token"
CLOCK_MARGIN=5
NS_UID=''; STATE_UID=''; INSTALLATION=''; STATE_OWNER=''
SECRET_NAMES=(); SECRET_UIDS=()
CURRENT_CYCLE=''; DISPLAY_CYCLE=baseline; OVERLAP=0; EXPIRY_PROVEN=0; ATTEMPTS=0; SUCCESSES=0
START=$(date +%s); ROUND=0

oc_read() {
    # No ambient context; stderr is private because API failures can include data.
    oc --kubeconfig "$KUBECONFIG" --context "$CONTEXT" --request-timeout=10s "$@" 2>"$WORK/oc-error"
}
timestamp() { date '+%Y-%m-%dT%H:%M:%S%z'; }
emit() {
    # Arguments are validated identifiers and fixed classifications, never bodies.
    local at=${7:-$(timestamp)} shown=$3
    if [[ "$1" == DIAGNOSTICO ]]; then
        printf '%s | DIAGNOSTICO %s\n' "$at" "$5"
    else
        if [[ ${#shown} -gt 28 ]]; then shown="${shown:0:13}...${shown: -12}"; fi
        printf '%-24s %-9s %-8s %-28s %-4s %-27s %s\n' "$at" "$1" "$2" "$shown" "$4" "$5" "$6"
    fi
    if [[ "$REPORT_OPEN" -eq 1 ]]; then
        printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$at" "$1" "$2" "$3" "$4" "$5" "$6" >&3
    fi
}
diagnostic() { emit DIAGNOSTICO - - - "$1" -; }

oc_read config view --minify -o json > "$WORK/kubeconfig.json" || fail 'falha ao ler o contexto explícito'
jq -e --arg server "$EXPECTED_SERVER" --arg context "$CONTEXT" '
    (.clusters|length)==1 and (.contexts|length)==1 and .contexts[0].name==$context and
    .clusters[0].cluster.server==$server and
    (.clusters[0].cluster["insecure-skip-tls-verify"] // false)==false
' "$WORK/kubeconfig.json" >/dev/null || fail 'servidor/contexto inesperado ou TLS Kubernetes desabilitado'
if [[ -n "$REPORT_FILE" ]]; then
    set -o noclobber
    if ! exec 3>"$REPORT_FILE"; then fail 'REPORT_FILE já existe ou não pode ser criado'; fi
    set +o noclobber
    chmod 600 "$REPORT_FILE"; REPORT_OPEN=1
    printf 'horario\tpapel\tgeracao\trecurso_secret\thttp\tresultado\tanterior_restante_s\n' >&3
fi

read_state() {
    local destination=$1 uid installation owner
    oc_read get namespace "$NAMESPACE" -o json > "$WORK/namespace.json" || fail 'falha ao consultar namespace'
    uid=$(jq -er '.metadata.uid|select(type=="string" and length>0)' "$WORK/namespace.json") || fail 'namespace sem UID'
    [[ -z "$NS_UID" || "$NS_UID" == "$uid" ]] || fail 'UID do namespace mudou; nova execução exige revisão'
    NS_UID=$uid
    oc_read get configmap "$STATE_CONFIGMAP" -n "$NAMESPACE" -o json > "$WORK/state-object.json" || fail 'falha ao ler o estado da automação'
    jq -e --arg name "$STATE_CONFIGMAP" --arg ns "$NAMESPACE" '
        .metadata.name==$name and .metadata.namespace==$ns and
        .metadata.labels["app.kubernetes.io/part-of"]=="rhbk-secret-rotation-lab" and
        (.metadata.labels["lab.rhbk/owner"]=="client-secret-rotation" or
         (.metadata.labels["lab.rhbk/installation"] // ""|length)>0)
    ' "$WORK/state-object.json" >/dev/null || fail 'ConfigMap de estado pertence a outro laboratório'
    uid=$(jq -er '.metadata.uid|select(type=="string" and length>0)' "$WORK/state-object.json") || fail 'estado sem UID'
    installation=$(jq -r '.metadata.labels["lab.rhbk/installation"] // ""' "$WORK/state-object.json")
    owner=$(jq -r '.metadata.labels["lab.rhbk/owner"] // ""' "$WORK/state-object.json")
    if [[ -n "$STATE_UID" ]]; then
        [[ "$STATE_UID" == "$uid" && "$INSTALLATION" == "$installation" && "$STATE_OWNER" == "$owner" ]] || fail 'UID ou proprietário do estado mudou'
    fi
    STATE_UID=$uid; INSTALLATION=$installation; STATE_OWNER=$owner
    jq -e '.data["state.json"]|fromjson|select(type=="object")' "$WORK/state-object.json" > "$destination" 2>"$WORK/jq-error" || fail 'state.json inválido'
    jq -e --arg client "$CLIENT_ID" --arg realm "$REALM" --arg ns "$NAMESPACE" '
        (.target_client_id // $client)==$client and (.realm // $realm)==$realm and
        (.namespace // $ns)==$ns and (.generation|type)=="number" and
        .generation>=1 and .generation<=2147483647 and .generation==(.generation|floor) and
        (.current_secret|type)=="string" and (.phase|type)=="string"
    ' "$destination" >/dev/null || fail 'estado não corresponde ao client, realm, namespace ou geração configurados'
}

model_state() {
    local input=$1 output=$2 now
    now=$(date +%s)
    jq -e --argjson now "$now" --argjson margin "$CLOCK_MARGIN" '
        def integer: type=="number" and .>=1 and .<=2147483647 and .==floor;
        def name: type=="string" and length<=253 and test("^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$");
        def generation_label($n): "v"+(("0000"+($n|tostring))|.[-([4,($n|tostring|length)]|max):]);
        . as $s | .current_secret as $current | generation_label(.generation) as $generation |
        ($s.secret_prefix // ($current|sub("-v[0-9]+$";""))) as $prefix |
        select(($current|name) and ($prefix|name) and $current==($prefix+"-"+$generation)) |
        (.post_metadata // {}) as $metadata |
        ($metadata["client.secret.creation.time"] // 0 | tonumber) as $created |
        ($metadata["client.secret.expiration.time"] // 0 | tonumber) as $expiry |
        ($metadata["client.secret.rotated.expiration.time"] // 0 | tonumber) as $rotated |
        ((["SECRET_STAGED","HOLDING_OLD","ROLLING_OUT","VERIFYING","COMPLETE","RETIRING_OLD"]|index($s.phase))!=null and
         ($s.cycle_id|type)=="string" and ($s.cycle_id|test("^[A-Za-z0-9._:-]+$")) and
         ($s.old_generation|integer) and ($s.next_generation|integer) and $s.next_generation==($s.old_generation+1) and
         ($s.old_secret|name) and ($s.next_secret|name) and
         $s.old_secret==($prefix+"-"+generation_label($s.old_generation)) and $s.next_secret==($prefix+"-"+generation_label($s.next_generation)) and
         (if $s.phase=="COMPLETE" then $current==$s.next_secret and $s.generation==$s.next_generation
          else $current==$s.old_secret and $s.generation==$s.old_generation end) and
         ($s.post_started_at // 0)>0 and ($s.intent_at // $s.post_started_at)<=$s.post_started_at and
         $created>=($s.post_started_at-$margin) and $created<=($now+$margin) and $expiry>$created and $rotated>0) as $pair |
        {mode:(if $pair then "pair" else "current" end), phase:$s.phase,
         cycle:(if $pair then $s.cycle_id else "baseline" end),
         current:$current,current_generation:$generation,
         old:(if $pair then $s.old_secret else "" end), new:(if $pair then $s.next_secret else "" end),
         old_generation:(if $pair then generation_label($s.old_generation) else "" end),
         new_generation:(if $pair then generation_label($s.next_generation) else "" end),
         deadline:(if $pair then ([$expiry,$rotated]|min|floor) else 0 end),
         created:$created,post_started:($s.post_started_at // 0),
         natural:(($s.recovery_mode // "")=="" and ($s.preexisting_outage // false)==false and ($s.old_retired_early // false)==false)}
    ' "$input" > "$output" 2>"$WORK/jq-error" || fail 'referências ou metadados do estado inválidos'
}

read_secret() {
    local name=$1 generation=$2 destination=$3 uid index
    oc_read get secret "$name" -n "$NAMESPACE" -o json --ignore-not-found > "$WORK/secret-object.json" || fail 'falha ao ler Secret exato; verifique acesso'
    [[ -s "$WORK/secret-object.json" ]] || return 2
    jq -e --arg name "$name" --arg ns "$NAMESPACE" --arg client "$CLIENT_ID" --arg gen "$generation" \
        --arg installation "$INSTALLATION" --arg owner "$STATE_OWNER" '
        .metadata.name==$name and .metadata.namespace==$ns and .immutable==true and
        .metadata.labels["app.kubernetes.io/part-of"]=="rhbk-secret-rotation-lab" and
        (.metadata.labels["lab.rhbk/owner"] // "")==$owner and
        ($installation=="" or .metadata.labels["lab.rhbk/installation"]==$installation) and
        (.data["client-id"]|@base64d)==$client and (.data.generation|@base64d)==$gen and
        (.data["client-secret"]|@base64d|length)>0 and
        (.metadata.labels["lab.rhbk/generation"] // $gen)==$gen
    ' "$WORK/secret-object.json" >/dev/null 2>"$WORK/jq-error" || fail 'Secret não é imutável ou diverge de client, geração ou proprietário'
    uid=$(jq -er '.metadata.uid|select(type=="string" and length>0)' "$WORK/secret-object.json") || fail 'Secret sem UID'
    for ((index=0; index<${#SECRET_NAMES[@]}; index++)); do
        if [[ "${SECRET_NAMES[$index]}" == "$name" ]]; then
            [[ "${SECRET_UIDS[$index]}" == "$uid" ]] || fail 'um Secret versionado foi recriado com outro UID'
            break
        fi
    done
    if [[ "$index" -eq "${#SECRET_NAMES[@]}" ]]; then SECRET_NAMES[$index]=$name; SECRET_UIDS[$index]=$uid; fi
    jq -j '.data["client-secret"]|@base64d' "$WORK/secret-object.json" > "$destination" 2>"$WORK/jq-error" || fail 'credencial codificada inválida'
    rm -f "$WORK/secret-object.json"
    return 0
}

authenticate() {
    local secret_file=$1 rc=0 oauth
    local options=(-q --silent --show-error --proto '=https' --max-redirs 0 --connect-timeout 3 --max-time 8
        --request POST --output "$WORK/response.json" --write-out '%{http_code}'
        --data-urlencode 'grant_type=client_credentials' --data-urlencode "client_id@$WORK/client-id"
        --data-urlencode "client_secret@$secret_file")
    [[ -z "$CA_FILE" ]] || options+=(--cacert "$CA_FILE")
    AUTH_CLOCK=$(date '+%s|%Y-%m-%dT%H:%M:%S%z')
    AUTH_START=${AUTH_CLOCK%%|*}; AUTH_STAMP=${AUTH_CLOCK#*|}
    HTTP=$(curl "${options[@]}" "$TOKEN_URL" 2>"$WORK/curl-error") || rc=$?
    AUTH_TIME=$(date +%s)
    ATTEMPTS=$((ATTEMPTS+1)); RESULT=ERRO_HTTP
    [[ "$HTTP" =~ ^[0-9]{3}$ ]] || HTTP=000
    if [[ "$rc" -ne 0 ]]; then
        HTTP=000; RESULT=ERRO_REDE_OU_TLS
    elif [[ "$HTTP" == 200 ]] && jq -e '.access_token|type=="string" and length>0' "$WORK/response.json" >/dev/null 2>&1; then
        RESULT=SUCESSO; SUCCESSES=$((SUCCESSES+1))
    elif [[ "$HTTP" == 400 || "$HTTP" == 401 ]]; then
        oauth=$(jq -r 'if .error=="invalid_client" or .error=="unauthorized_client" then .error else "desconhecido" end' "$WORK/response.json" 2>/dev/null) || oauth=desconhecido
        case "$oauth" in invalid_client|unauthorized_client) RESULT=$oauth ;; *) RESULT=ERRO_OAUTH_DESCONHECIDO ;; esac
    fi
    rm -f "$WORK/response.json"
}

sample_round() {
    local model_before model_after mode cycle cycle_key old new old_generation new_generation deadline natural remaining
    local old_http old_result old_time old_start old_stamp new_http new_result new_time new_stamp baseline baseline_gen
    read_state "$WORK/state-before.json"; model_state "$WORK/state-before.json" "$WORK/model-before.json"
    model_before=$(jq -cS . "$WORK/model-before.json")
    mode=$(jq -r .mode "$WORK/model-before.json"); cycle=$(jq -r .cycle "$WORK/model-before.json")
    cycle_key=$(jq -cS '{cycle,old,new,old_generation,new_generation,deadline,created,post_started,natural}' "$WORK/model-before.json")
    DISPLAY_CYCLE=$cycle
    if [[ "$cycle_key" != "$CURRENT_CYCLE" ]]; then
        CURRENT_CYCLE=$cycle_key; OVERLAP=0; EXPIRY_PROVEN=0
        diagnostic AGUARDANDO_NOVO_CICLO
    fi
    if [[ "$mode" == current ]]; then
        baseline=$(jq -r .current "$WORK/model-before.json"); baseline_gen=$(jq -r .current_generation "$WORK/model-before.json")
        if ! read_secret "$baseline" "$baseline_gen" "$WORK/current-secret"; then diagnostic AGUARDANDO_SECRET; return; fi
        authenticate "$WORK/current-secret"; old_http=$HTTP; old_result=$RESULT; old_stamp=$AUTH_STAMP
    else
        old=$(jq -r .old "$WORK/model-before.json"); new=$(jq -r .new "$WORK/model-before.json")
        old_generation=$(jq -r .old_generation "$WORK/model-before.json"); new_generation=$(jq -r .new_generation "$WORK/model-before.json")
        deadline=$(jq -r .deadline "$WORK/model-before.json"); natural=$(jq -r .natural "$WORK/model-before.json")
        if ! read_secret "$old" "$old_generation" "$WORK/old-secret" || ! read_secret "$new" "$new_generation" "$WORK/new-secret"; then
            rm -f "$WORK/old-secret" "$WORK/new-secret"
            diagnostic AGUARDANDO_SECRET; return
        fi
        if cmp -s "$WORK/old-secret" "$WORK/new-secret"; then fail 'gerações anterior e nova contêm a mesma credencial'; fi
        authenticate "$WORK/old-secret"; old_http=$HTTP; old_result=$RESULT; old_time=$AUTH_TIME; old_start=$AUTH_START; old_stamp=$AUTH_STAMP
        authenticate "$WORK/new-secret"; new_http=$HTTP; new_result=$RESULT; new_time=$AUTH_TIME; new_stamp=$AUTH_STAMP
    fi
    # Evidence belongs to a round only if the state remains the same throughout it.
    read_state "$WORK/state-after.json"; model_state "$WORK/state-after.json" "$WORK/model-after.json"
    model_after=$(jq -cS . "$WORK/model-after.json")
    rm -f "$WORK/current-secret" "$WORK/old-secret" "$WORK/new-secret"
    if [[ "$model_before" != "$model_after" ]]; then diagnostic INCONCLUSIVO_RODADA_DESCARTADA_ESTADO_MUDOU; return; fi
    if [[ "$mode" == current ]]; then
        emit ATUAL "$baseline_gen" "$baseline" "$old_http" "$old_result" - "$old_stamp"
        diagnostic AGUARDANDO_SECRET_NOVO; return
    fi
    remaining=$((deadline-new_time)); [[ "$remaining" -ge 0 ]] || remaining=0
    emit ANTERIOR "$old_generation" "$old" "$old_http" "$old_result" "$remaining" "$old_stamp"
    emit NOVA "$new_generation" "$new" "$new_http" "$new_result" "$remaining" "$new_stamp"
    if [[ "$natural" != true ]]; then diagnostic INCONCLUSIVO_RECUPERACAO_OU_INVALIDACAO; return; fi
    if [[ "$old_result" == SUCESSO && "$new_result" == SUCESSO && "$old_time" -lt "$deadline" && "$new_time" -lt "$deadline" ]]; then
        OVERLAP=1; diagnostic CONVIVENCIA_OBSERVADA
    elif [[ "$OVERLAP" -eq 1 && ( "$old_http" == 400 || "$old_http" == 401 ) && ( "$old_result" == invalid_client || "$old_result" == unauthorized_client ) &&
            "$old_start" -ge $((deadline+CLOCK_MARGIN)) && "$new_result" == SUCESSO ]]; then
        EXPIRY_PROVEN=1; diagnostic PASS_EXPIRACAO_OBSERVADA
    elif [[ "$OVERLAP" -eq 0 ]]; then diagnostic INCONCLUSIVO_JANELA_NAO_OBSERVADA
    else diagnostic AGUARDANDO_REJEICAO_CONCLUSIVA
    fi
}

while :; do
    if [[ "$ROUND" -gt 0 && "$DURATION_SECONDS" -gt 0 && $(($(date +%s)-START)) -ge "$DURATION_SECONDS" ]]; then break; fi
    ROUND=$((ROUND+1))
    if [[ -t 1 && "$PLAIN" -eq 0 ]]; then
        printf '\033[2J\033[H'
        printf 'Observação M2M | namespace=%s | client=%s | rodada=%s\n' "$NAMESPACE" "$CLIENT_ID" "$ROUND"
        printf 'Fuso=%s | Ctrl+C encerra somente o observador\n' "$WATCH_TZ"
    fi
    if [[ "$ROUND" -eq 1 || ( -t 1 && "$PLAIN" -eq 0 ) ]]; then
        printf '%-24s %-9s %-8s %-28s %-4s %-27s %s\n' horario papel geracao recurso_secret http resultado anterior_restante_s
    fi
    sample_round
    if [[ -t 1 && "$PLAIN" -eq 0 ]]; then
        printf '\nCiclo=%s | requisições=%s | sucessos=%s | convivência=%s | expiração=%s\n' "$DISPLAY_CYCLE" "$ATTEMPTS" "$SUCCESSES" "$OVERLAP" "$EXPIRY_PROVEN"
    fi
    [[ "$ONCE" -eq 0 ]] || break
    now=$(date +%s)
    [[ "$DURATION_SECONDS" -eq 0 || $((now-START)) -lt "$DURATION_SECONDS" ]] || break
    delay=$INTERVAL_SECONDS
    if [[ "$DURATION_SECONDS" -gt 0 && "$delay" -gt $((DURATION_SECONDS-now+START)) ]]; then delay=$((DURATION_SECONDS-now+START)); fi
    sleep "$delay"
done
