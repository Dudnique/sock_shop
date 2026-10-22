#!/usr/bin/env sh
#
# One-shot Grafana importer for the docker-compose monitoring stack.
#
# Credentials come from the environment (GRAFANA_USER / GRAFANA_PASSWORD, filled
# in by docker-compose.monitoring.yml from ${GRAFANA_ADMIN_*}). Nothing is baked
# in any more: this script used to POST to `http://admin:foobar@grafana:3000`,
# i.e. a password committed to git (FIXES.md, section 12).
#
# NOTE: keep these loops as real shell loops. The Kubernetes twin of this script
# lived in a YAML FOLDED block (">"), which joins lines with spaces: "fi" +
# "done" became "fidone" and a "#" comment swallowed the rest - while the Job
# still reported success and imported nothing.

: "${GRAFANA_PASSWORD:?GRAFANA_PASSWORD is not set (export GRAFANA_ADMIN_PASSWORD)}"
: "${GRAFANA_USER:=admin}"

sleep 3

# Import data sources
for file in *-datasource.json; do
  if [ -e "$file" ]; then
    echo "importing $file" &&
    curl --silent --fail --show-error \
      --user "$GRAFANA_USER:$GRAFANA_PASSWORD" \
      --request POST http://grafana:3000/api/datasources \
      --header "Content-Type: application/json" \
      --header "Accept: application/json" \
      --data-binary "@$file";
    echo "";
  fi
done

# Import dashboards
for file in *-dashboard.json; do
  if [ -e "$file" ]; then
    echo "importing $file" &&
    curl --silent --fail --show-error \
      --user "$GRAFANA_USER:$GRAFANA_PASSWORD" \
      --request POST http://grafana:3000/api/dashboards/import \
      --header "Content-Type: application/json" \
      --header "Accept: application/json" \
      --data-binary "@$file";
    echo "";
  fi
done

