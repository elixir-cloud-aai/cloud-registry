#!/bin/bash
SERVICES=(
  '{"name":"https://funnel.cloud.e-infra.cz","type":{"group":"org.ga4gh","artifact":"Funnel/OpenPBS","version":"1.1"},"version":"1.1","url":"https://funnel.cloud.e-infra.cz","organization":{"name":"Czechia","url":"https://funnel.cloud.e-infra.cz"},"environment":"online","description":"auth basic, and status testing"}'
  '{"name":"https://poiesis.dyn.cloud.e-infra.cz/","type":{"group":"org.ga4gh","artifact":"Poiesis","version":"1.1"},"version":"1.1","url":"https://poiesis.dyn.cloud.e-infra.cz/","organization":{"name":"Czechia","url":"https://poiesis.dyn.cloud.e-infra.cz/"},"environment":"online","description":"auth no, contact Viktoria Spisakova (ELIXIR-CZ), and status online"}'
  '{"name":"https://csc-tesk-noauth.rahtiapp.fi","type":{"group":"org.ga4gh","artifact":"TESK","version":"1.0"},"version":"1.0","url":"https://csc-tesk-noauth.rahtiapp.fi","organization":{"name":"Finland","url":"https://csc-tesk-noauth.rahtiapp.fi"},"environment":"online","description":"auth no, contact Alvaro Gonzalez?, and status testing"}'
  '{"name":"https://fip-86-50-228-254.kaj.poutavm.fi/","type":{"group":"org.ga4gh","artifact":"Funnel/Slurm","version":"1.0"},"version":"1.0","url":"https://fip-86-50-228-254.kaj.poutavm.fi/","organization":{"name":"Finland","url":"https://fip-86-50-228-254.kaj.poutavm.fi/"},"environment":"online","description":"auth basic, contact Alvaro Gonzales?, and status testing"}'
  '{"name":"https://poiesis.rahtiapp.fi/","type":{"group":"org.ga4gh","artifact":"Poiesis","version":"1.1"},"version":"1.1","url":"https://poiesis.rahtiapp.fi/","organization":{"name":"Finland","url":"https://poiesis.rahtiapp.fi/"},"environment":"online","description":"auth no, contact Alvaro Gonzalez (ELIXIR-FI), and status online"}'
  '{"name":"https://test-poiesis.rahtiapp.fi/","type":{"group":"org.ga4gh","artifact":"Poiesis","version":"1.1"},"version":"1.1","url":"https://test-poiesis.rahtiapp.fi/","organization":{"name":"Finland","url":"https://test-poiesis.rahtiapp.fi/"},"environment":"online","description":"auth no, contact Alvaro Gonzalez (ELIXIR-FI), and status online"}'
  '{"name":"https://spe4hd.tes.bihealth.org","type":{"group":"org.ga4gh","artifact":"Funnel/Slurm","version":"1.0"},"version":"1.0","url":"https://spe4hd.tes.bihealth.org","organization":{"name":"Germany","url":"https://spe4hd.tes.bihealth.org"},"environment":"online","description":"auth no, and status testing"}'
  '{"name":"https://tesk-eu.hypatia-comp.athenarc.gr","type":{"group":"org.ga4gh","artifact":"TESK","version":"1.0"},"version":"1.0","url":"https://tesk-eu.hypatia-comp.athenarc.gr","organization":{"name":"Greece","url":"https://tesk-eu.hypatia-comp.athenarc.gr"},"environment":"online","description":"auth no, and status testing"}'
)

for PAYLOAD in "${SERVICES[@]}"; do
  # The API returns the ID as a quoted string, use jq -r . to unquote it
  ID=$(curl -s -X POST "http://localhost:8081/ga4gh/registry/v1/services" -H "Content-Type: application/json" -d "$PAYLOAD" | jq -r .)
  echo "ID: $ID"
  curl -s -X GET "http://localhost:8081/ga4gh/registry/v1/services/$ID" | jq -c '{id, name, url, type, version, environment}'
done
