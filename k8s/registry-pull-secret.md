# Registry pull secret setup

After namespace creation, copy the registry pull secret from stackmotive-prod:

```bash
kubectl -n stackmotive-prod get secret registry-docr-prod -o yaml | \
  sed 's/namespace: stackmotive-prod/namespace: sovereign-signal/' | \
  kubectl apply -f -
```

Then patch the service account:

```bash
kubectl -n sovereign-signal patch serviceaccount default \
  -p '{"imagePullSecrets": [{"name": "registry-docr-prod"}]}'
```
