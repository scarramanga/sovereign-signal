#!/bin/bash
# Long-running kubectl port-forward for the sovereign-signal pod.
# Launched by ~/Library/LaunchAgents/com.sovereignsignal.portforward.plist
# with KeepAlive, so it is restarted automatically after reboots and whenever
# the connection drops (e.g. after a digest deploy rolls the pod).
#
# PATH must include /opt/homebrew/bin because the DigitalOcean kubeconfig
# authenticates via a `doctl` exec credential plugin. Under launchd the default
# PATH does not contain Homebrew, so without this kubectl fails with
# "exec: executable doctl not found".
export PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
export KUBECONFIG=/Users/andrewboss/.kube/config

exec /usr/local/bin/kubectl -n sovereign-signal port-forward deployment/sovereign-signal 8080:8000
