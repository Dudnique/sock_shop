# Monitoring

> **English** | [Русский](README.ru.md)

First create the monitoring namespace using the `00-monitoring-ns.yaml` file:

`$ kubectl create -f 00-monitoring-ns.yaml`


### Prometheus

To deploy simply apply all the prometheus manifests (01-10) in any order:

`kubectl apply $(ls *-prometheus-*.yaml | awk ' { print " -f " $1 } ')`

The prometheus server will be exposed on Nodeport `31090`.

### Grafana

First create the `grafana-admin` Secret: the Deployment and the import Job read the
admin credentials from it, so the pods do not start without it.
`examples/grafana-admin-secret.example.yaml` is a ready-made template.

`kubectl -n monitoring create secret generic grafana-admin --from-literal=admin-user=admin --from-literal=admin-password='<some-long-random-string>'`

Then apply the grafana manifests from 20 to 22:

`kubectl apply $(ls *-grafana-*.yaml | awk ' { print " -f " $1 }'  | grep -v grafana-import)`

Once the grafana pod is in the Running state apply the `23-grafana-import-dash-batch.yaml` manifest to import the Dashboards:

`kubectl apply -f 23-grafana-import-dash-batch.yaml`

The Service is `ClusterIP` and is **not** published on a host port: as a NodePort
`31300` Grafana was reachable on every interface of the host with the default
`admin`/`admin`. It is served by the same Traefik entrypoint as the application and
distinguished by the HTTP `Host` header (see `30-grafana-ingress.yaml`) - one port,
several backends, the same idea as `server_name` in nginx:

`127.0.0.1 grafana.localhost` in `C:\Windows\System32\drivers\etc\hosts` -> `http://grafana.localhost:8080`

No hosts entry (or no admin rights)? Use the tunnel:

`kubectl port-forward -n monitoring svc/grafana 3000:80` -> `http://localhost:3000`
