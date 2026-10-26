#!/usr/bin/env bash
# Issue automático ante fallo de la ejecución SEMANAL de real-data.yml (spec ft_datasets
# RF-10 bis, plan §3.8.1). Solo usa el GITHUB_TOKEN del workflow (GH_TOKEN, permisos
# issues: write + contents: read) a través de la CLI `gh`.
#
# Uso: real_data_issue.sh <receta> <exit de materialize> <outcome del paso de tests>
#
# - Tipo (prioridad): exit 3 -> download; 5 -> drift; 6 -> unlocked; exit 0 y tests
#   'failure' -> tests; cualquier otro caso -> infra.
# - De-duplicación: si hay un issue ABIERTO con las etiquetas `real-data` y
#   `real-data:<tipo>` se comenta; si no, se abre `[real-data] <tipo>: <receta>`.
# - Cuerpo: enlace a la ejecución + reports/verify.md (+ resumen de reports/pytest.txt
#   para `tests`).
# - Un fallo al gestionar el issue se registra como ::warning:: y NO cambia el resultado
#   del job (ya está en rojo por el paso original): el script siempre sale con 0.
set -uo pipefail

RECIPE="${1:?receta}"
MATERIALIZE_EXIT="${2:-}"
TESTS_OUTCOME="${3:-}"
REPORTS_DIR="${REAL_DATA_REPORTS_DIR:-reports}"

classify() {
  case "$MATERIALIZE_EXIT" in
    3) echo "download" ;;
    5) echo "drift" ;;
    6) echo "unlocked" ;;
    0)
      if [ "$TESTS_OUTCOME" = "failure" ]; then echo "tests"; else echo "infra"; fi
      ;;
    *) echo "infra" ;;
  esac
}

build_body() {
  local kind="$1" body_file="$2"
  local run_url="${GITHUB_SERVER_URL:-https://github.com}/${GITHUB_REPOSITORY:-unknown}/actions/runs/${GITHUB_RUN_ID:-0}"
  {
    echo "Fallo **${kind}** de la ejecución semanal de datos reales (receta \`${RECIPE}\`)."
    echo
    echo "- Ejecución: ${run_url}"
    echo "- Fecha (UTC): $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "- Exit de \`ft_datasets materialize\`: \`${MATERIALIZE_EXIT:-n/a}\` · tests: \`${TESTS_OUTCOME:-n/a}\`"
    echo
    if [ -s "${REPORTS_DIR}/verify.md" ]; then
      cat "${REPORTS_DIR}/verify.md"
    else
      echo "_Sin informe de verificación (\`${REPORTS_DIR}/verify.md\` no existe: fallo temprano)._"
    fi
    if [ "$kind" = "tests" ]; then
      echo
      echo "#### Tests fallidos (\`pytest -m real_data\`)"
      echo
      echo '```'
      if [ -s "${REPORTS_DIR}/pytest.txt" ]; then
        grep -E '^(FAILED|ERROR) |^=+ .*(failed|error).* =+$' "${REPORTS_DIR}/pytest.txt" | tail -n 50
      else
        echo "(sin reports/pytest.txt)"
      fi
      echo '```'
    fi
  } > "$body_file"
}

main() {
  local kind label body_file number
  kind="$(classify)"
  label="real-data:${kind}"
  body_file="$(mktemp)"
  build_body "$kind" "$body_file"
  echo "real-data issue: tipo=${kind} receta=${RECIPE}"

  gh label create "real-data" --color "B60205" --description "Fallos de la suite de datos reales" --force || return 1
  gh label create "$label" --color "D93F0B" --description "Fallo real-data de tipo ${kind}" --force || return 1

  number="$(gh issue list --state open --label "real-data" --label "$label" \
    --json number --jq '.[0].number // empty' --limit 1)" || return 1
  if [ -n "$number" ]; then
    echo "Comentando el issue abierto #${number}"
    gh issue comment "$number" --body-file "$body_file" || return 1
  else
    echo "Abriendo issue nuevo"
    gh issue create --title "[real-data] ${kind}: ${RECIPE}" \
      --label "real-data" --label "$label" --body-file "$body_file" || return 1
  fi
}

main || echo "::warning::No se pudo gestionar el issue de real-data (el fallo original se mantiene)"
exit 0
